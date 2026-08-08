"""
A multi-board project, without KiCad.

Everything the CLI needs lives here: config, the index, netlist export, library
resolution, DRC, fab, Doctor. Operations that must mutate a board file
(``create_board``, ``apply_update``) live in ``multiboard.manager``, which adds
the backend on top.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from ..constants import BLOCK_LIB_NAME, BOARDS_DIR, CONFIG_FILE, DEBUG_LOG_NAME
from . import kicad_env
from .config import load, save
from .index import ComponentIndex
from .netlist import NetlistError, export_netlist, netlist_path
from .project import find_project_root, work_dir

RE_LIB_ENTRY = re.compile(
    r'\(lib\s+\(name\s+"?([^")\s]+)"?\s*\)\s*\(type\s+"?([^")\s]+)"?\s*\)\s*\(uri\s+"?([^")]+)"?',
    re.IGNORECASE,
)
RE_VAR = re.compile(r"\$\{([A-Za-z0-9_]+)\}")


@dataclass
class RefreshResult:
    """Outcome of a full index refresh."""

    stats: object = None
    netlist_error: str = ""
    warnings: list[str] = None

    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []


class Workspace:
    """A project root plus its config, index, and resolved KiCad installation."""

    def __init__(self, project_dir: Path):
        self.root = find_project_root(Path(project_dir).expanduser())
        self.config_path = self.root / CONFIG_FILE
        self.config, self.config_warning = load(self.config_path)
        self.install = kicad_env.discover()
        self.index = ComponentIndex(self.root, self.config)
        self._lib_paths: Optional[dict[str, Path]] = None

        if not self.config.root_schematic:
            self._autodetect_root_files()

    # =====================================================================
    # Config
    # =====================================================================

    def save_config(self) -> None:
        save(self.config_path, self.config)

    def is_configured(self) -> bool:
        """Whether this project has been set up. Drives the onboarding wizard."""
        return self.config_path.exists()

    def _autodetect_root_files(self) -> None:
        """
        Fill in the root schematic only when it is not already known.

        v12 re-ran detection on *every* load and overwrote the stored value, so a
        correct manual choice could never survive, and it broke out of the loop
        on the first ``*.kicad_pro`` found in nondeterministic glob order.
        """
        from .project import detect_root_files

        candidates = detect_root_files(self.root)
        best = next((c for c in candidates if c["has_schematic"]), None)
        if best:
            self.config.root_schematic = best["schematic"]
            self.config.root_pcb = best["pcb"]

    def root_schematic(self) -> Optional[Path]:
        if not self.config.root_schematic:
            return None
        path = self.root / self.config.root_schematic
        return path if path.exists() else None

    # =====================================================================
    # Index
    # =====================================================================

    def refresh(
        self,
        *,
        force: bool = False,
        export: bool = True,
        progress: Optional[Callable[[int, str], None]] = None,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> RefreshResult:
        """
        Rebuild the index, exporting a fresh netlist first.

        A netlist export failure is reported but not fatal: the board half of the
        index is still useful, and telling the user "I can see what is on your
        boards but not what your schematic says" beats showing nothing.
        """
        out = RefreshResult()
        netlist = netlist_path(self.root)

        if export:
            sch = self.root_schematic()
            if sch is None:
                out.netlist_error = "No root schematic is configured, so schematic data is unavailable."
            else:
                if progress:
                    progress(2, "Exporting netlist...")
                try:
                    netlist = export_netlist(self.install, self.root, sch, variant=self.config.variant)
                except NetlistError as exc:
                    out.netlist_error = str(exc)
                except Exception as exc:
                    out.netlist_error = f"Netlist export failed: {exc}"

        out.stats = self.index.refresh(
            force=force,
            netlist=netlist if netlist.exists() else None,
            progress=progress,
            cancel=cancel,
        )
        out.warnings = list(out.stats.warnings) if out.stats else []
        if self.config_warning:
            out.warnings.insert(0, self.config_warning)
        return out

    def reclassify(self):
        """Recompute intent and status with no I/O. Instant."""
        return self.index.reclassify()

    # =====================================================================
    # Assignment (intent)
    # =====================================================================

    def assign(self, refs: list[str], board: Optional[str]) -> int:
        """
        Pin components to a board, or clear the pin when ``board`` is None.

        This writes intent only. No PCB and no schematic is touched, which is
        what makes bulk reassignment safe to experiment with.
        """
        changed = 0
        for ref in refs:
            if board is None:
                if self.config.assignments.pop(ref, None) is not None:
                    changed += 1
            elif self.config.assignments.get(ref) != board:
                self.config.assignments[ref] = board
                changed += 1

        if changed:
            self.save_config()
            self.reclassify()
        return changed

    def adopt_placements(self, refs: Optional[list[str]] = None) -> int:
        """
        Turn "it happens to be here" into "it belongs here".

        The natural migration path for a project built with v12, where placement
        was the only notion of ownership.
        """
        targets = (
            refs
            if refs is not None
            else [r.ref for r in self.index.records() if len(r.placements) == 1 and r.intent is None]
        )
        changed = 0
        for ref in targets:
            rec = self.index.get(ref)
            if not rec or len(rec.placements) != 1:
                continue
            if self.config.assignments.get(ref) != rec.placements[0].board:
                self.config.assignments[ref] = rec.placements[0].board
                changed += 1

        if changed:
            self.save_config()
            self.reclassify()
        return changed

    def suggest_rules(self):
        from .rules import suggest_from_sheets

        return suggest_from_sheets(self.index.schematic, list(self.config.boards))

    # =====================================================================
    # Footprint libraries
    # =====================================================================

    def lib_paths(self, *, refresh: bool = False) -> dict[str, Path]:
        """
        Every footprint library nickname the project can resolve.

        Two v12 defects are fixed here: the global ``fp-lib-table`` was never
        read (so PCM-installed and user-global libraries were invisible), and any
        URI still containing ``${`` after substituting ``KIPRJMOD`` was silently
        dropped -- which is exactly the shape of ``${KICAD10_FOOTPRINT_DIR}``.
        """
        if self._lib_paths is not None and not refresh:
            return self._lib_paths

        out: dict[str, Path] = {}
        variables = self._path_variables()

        tables = [self.root / "fp-lib-table"]
        if self.install and self.install.config_dir:
            tables.append(self.install.config_dir / "fp-lib-table")

        for table in tables:
            out.update(self._parse_lib_table(table, variables))

        # Stock libraries, so a fresh install resolves without any table at all.
        if self.install and self.install.share:
            footprints = self.install.share / "footprints"
            if footprints.is_dir():
                for pretty in sorted(footprints.glob("*.pretty")):
                    out.setdefault(pretty.stem, pretty)

        for lib in (BLOCK_LIB_NAME, "MultiBoard_Ports"):
            local = self.root / f"{lib}.pretty"
            if local.is_dir():
                out[lib] = local

        self._lib_paths = out
        return out

    def _path_variables(self) -> dict[str, str]:
        import os

        variables = {"KIPRJMOD": str(self.root)}
        if self.install:
            for key, value in kicad_env.env_lib_dirs(self.install.version).items():
                variables[key] = str(value)
        variables.update(os.environ)
        return variables

    def _parse_lib_table(self, path: Path, variables: dict[str, str]) -> dict[str, Path]:
        if not path.exists():
            return {}
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {}

        out: dict[str, Path] = {}
        for match in RE_LIB_ENTRY.finditer(content):
            nick, kind, uri = match.group(1), match.group(2), match.group(3)
            if kind.lower() not in ("kicad", ""):
                continue  # database and HTTP libraries have no directory to scan
            expanded = RE_VAR.sub(lambda m: variables.get(m.group(1), m.group(0)), uri)
            if "${" in expanded:
                self.log(f"Unresolved variable in fp-lib-table entry {nick!r}: {uri}")
                continue
            out[nick] = Path(expanded)
        return out

    def unresolved_libs(self) -> list[str]:
        """Library nicknames whose URI still contains an unknown variable."""
        variables = self._path_variables()
        out = []
        table = self.root / "fp-lib-table"
        if not table.exists():
            return out
        try:
            content = table.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return out
        for match in RE_LIB_ENTRY.finditer(content):
            expanded = RE_VAR.sub(lambda m: variables.get(m.group(1), m.group(0)), match.group(3))
            if "${" in expanded:
                out.append(f"{match.group(1)}: {match.group(3)}")
        return out

    def register_library(self, lib_name: str, relative_path: str) -> None:
        """
        Add a library to the project ``fp-lib-table``, idempotently.

        v12 tested for the library with ``if lib_name in content`` (so
        ``MultiBoard_Blocks`` matched ``MultiBoard_Blocks_old``) and then did
        ``content.rstrip().rstrip(")")``, which strips *every* trailing paren and
        corrupts a table whose last entry shares a line with the closing paren.
        """
        from . import sexpr

        table = self.root / "fp-lib-table"
        entry = (
            f'  (lib (name {sexpr.quote(lib_name)})(type "KiCad")'
            f'(uri "${{KIPRJMOD}}/{relative_path}")(options "")(descr ""))'
        )

        if not table.exists():
            table.write_text(f"(fp_lib_table\n  (version 7)\n{entry}\n)\n", encoding="utf-8")
            self._lib_paths = None
            return

        content = table.read_text(encoding="utf-8", errors="replace")

        for match in RE_LIB_ENTRY.finditer(content):
            if match.group(1) == lib_name:
                return  # exact nickname match, not a substring

        close = content.rfind(")")
        if close == -1:
            content = content.rstrip() + f"\n{entry}\n"
        else:
            content = content[:close].rstrip() + f"\n{entry}\n)" + content[close + 1 :]

        table.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
        self._lib_paths = None

    # =====================================================================
    # Boards
    # =====================================================================

    def board_pcb(self, name: str) -> Optional[Path]:
        board = self.config.boards.get(name)
        if not board or not board.pcb_path:
            return None
        return self.root / board.pcb_path

    def open_boards(self) -> set:
        """Board names currently locked by a KiCad editor."""
        from .project import is_pcb_open

        out = set()
        for name in self.config.boards:
            pcb = self.board_pcb(name)
            if pcb and is_pcb_open(pcb):
                out.add(name)
        return out

    def board_color(self, name: str, *, dark_mode: bool) -> tuple[int, int, int]:
        """Stable identity colour for a board, honouring any user override."""
        from .color import board_color, from_hex

        override = self.config.board_colors.get(name)
        if override:
            return from_hex(override)
        order = sorted(self.config.boards)
        index = order.index(name) if name in order else -1
        return board_color(name, dark_mode=dark_mode, index=index)

    # =====================================================================
    # Diagnostics
    # =====================================================================

    def doctor(self, *, backend=None):
        from .doctor import run_all

        return run_all(self.root, self.config, backend=backend)

    def run_drc(self, *, parity: bool = False, progress=None, cancel=None):
        from .fab import run_drc_all

        return run_drc_all(
            self.install,
            self.root,
            self.config,
            parity=parity,
            progress=progress,
            cancel=cancel,
        )

    def plan_update(self, board: str, *, include_unassigned: bool = True):
        from .plan import plan_update

        return plan_update(self.index, board, include_unassigned=include_unassigned)

    def log(self, message: str) -> None:
        """
        Append to the project's debug log.

        Opened per call, like v12, but only from paths that are not hot -- v12
        logged once per excluded component inside the netlist parse loop.
        """
        try:
            from datetime import datetime

            path = work_dir(self.root) / DEBUG_LOG_NAME
            if path.exists() and path.stat().st_size > 2_000_000:
                path.replace(path.with_suffix(".log.1"))
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"{datetime.now().isoformat(timespec='seconds')} {message}\n")
        except OSError:
            pass

    @property
    def boards_dir(self) -> Path:
        return self.root / BOARDS_DIR
