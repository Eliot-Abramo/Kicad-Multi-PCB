"""
Board mutation -- the operations that need KiCad.

Everything read-only lives in :class:`multiboard.core.workspace.Workspace`. This
adds the three things that must write a board file: creating one, regenerating
its block footprint, and applying an update plan.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .backend import ApplyResult, BlockSpec, get_backend
from .constants import BLOCK_LIB_NAME, PORT_LIB_NAME
from .core.config import BoardConfig
from .core.netlist import netlist_path
from .core.plan import UpdatePlan
from .core.project import (
    SchematicLinkError,
    board_dir_for,
    find_hierarchical_sheets,
    is_pcb_open,
    link_file,
    safe_relative,
    sanitize_board_name,
    trash_board_dir,
)
from .core.workspace import Workspace


@dataclass
class BoardCreation:
    """Outcome of creating a board."""

    name: str
    pcb_path: str
    warnings: list[str]


class BoardBusy(Exception):
    """The board is open in a KiCad editor, so it must not be written."""


class MultiBoardManager:
    """Workspace plus the ability to write boards."""

    def __init__(self, project_dir: Path):
        self.ws = Workspace(project_dir)
        self.backend = get_backend()
        self._busy = False

    # Convenience passthroughs; the UI talks to the manager, not two objects.
    @property
    def root(self) -> Path:
        return self.ws.root

    @property
    def config(self):
        return self.ws.config

    @property
    def index(self):
        return self.ws.index

    # =====================================================================
    # Guards
    # =====================================================================

    def _require_free(self, name: str) -> Path:
        pcb = self.ws.board_pcb(name)
        if pcb is None:
            raise ValueError(f"Board '{name}' has no PCB path recorded.")
        if not pcb.exists():
            raise ValueError(f"Board '{name}' PCB not found: {pcb}")

        active = self.backend.active_board_path()
        if is_pcb_open(pcb, active):
            raise BoardBusy(
                f"'{name}' is open in KiCad.\n\n"
                "Close it first so the editor and this plugin do not write the same "
                "file. If KiCad crashed, Doctor can clear the stale lock."
            )
        return pcb

    # =====================================================================
    # Creating boards
    # =====================================================================

    def create_board(self, name: str, description: str = "") -> BoardCreation:
        """
        Create a sub-board: directory, project file, linked schematic, empty PCB.

        Rolls back completely on failure. v12's rollback removed only the
        ``.kicad_pcb`` and then tried ``rmdir``, which failed because the project
        and library-table files it had already written were still there -- so a
        failed creation left a half-built directory behind.
        """
        name = name.strip()
        if name in self.config.boards:
            raise ValueError(f"A board named '{name}' already exists.")

        dir_name = sanitize_board_name(name)
        board_dir = self.ws.boards_dir / dir_name
        if board_dir.exists():
            raise ValueError(
                f"The directory boards/{dir_name} already exists.\n"
                "Choose a different name, or import the existing board."
            )

        warnings: list[str] = []
        created_dir = False
        try:
            board_dir.mkdir(parents=True)
            created_dir = True

            pcb = board_dir / f"{dir_name}.kicad_pcb"
            self.backend.new_board(pcb)

            board = BoardConfig(
                name=name,
                pcb_path=pcb.relative_to(self.root).as_posix(),
                description=description,
            )

            self._setup_board_project(board, board_dir, dir_name)

            try:
                self.regenerate_block(board)
            except Exception as exc:
                # A missing block footprint is cosmetic; a missing board is not.
                warnings.append(f"Block footprint could not be generated: {exc}")

            self.config.boards[name] = board
            self.ws.save_config()
            self.ws._lib_paths = None
            return BoardCreation(name=name, pcb_path=board.pcb_path, warnings=warnings)

        except BaseException:
            if created_dir:
                import shutil

                shutil.rmtree(board_dir, ignore_errors=True)
            raise

    def _setup_board_project(self, board: BoardConfig, board_dir: Path, dir_name: str) -> None:
        """Write the sub-project file and link the schematic hierarchy."""
        source = self.ws.root_schematic()
        if source is None:
            raise SchematicLinkError(
                "No root schematic is configured, so this board cannot share one.\n"
                "Set the root schematic first."
            )

        (board_dir / f"{dir_name}.kicad_pro").write_text(
            json.dumps(
                {
                    "board": {"design_settings": {}},
                    "meta": {"filename": f"{dir_name}.kicad_pro", "version": 3},
                    "schematic": {},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        link_file(source, board_dir / f"{dir_name}.kicad_sch")

        # Hierarchical sheets, each validated before it is joined to a directory
        # we are about to write into.
        for relative in sorted(find_hierarchical_sheets(source)):
            child = safe_relative(source.parent, relative)
            dest = safe_relative(board_dir, relative)
            if child is None or dest is None or not child.exists():
                self.ws.log(f"Skipping unsafe or missing sheet reference: {relative!r}")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            link_file(child, dest)

        self._copy_lib_tables(board_dir)

    def _copy_lib_tables(self, board_dir: Path) -> None:
        """
        Copy the project library tables, resolving ``${KIPRJMOD}`` to the root.

        A sub-board's own KIPRJMOD points at its own directory, so an unresolved
        reference would look for the project's libraries inside boards/<name>/.
        """
        for table in ("fp-lib-table", "sym-lib-table"):
            source = self.root / table
            if not source.exists():
                continue
            try:
                content = source.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            content = content.replace("${KIPRJMOD}", self.root.as_posix())
            (board_dir / table).write_text(content, encoding="utf-8")

    def import_board(self, name: str, pcb_path: Path) -> BoardConfig:
        """Adopt a board directory that exists on disk but is not in the config."""
        rel = pcb_path.resolve().relative_to(self.root.resolve()).as_posix()
        if board_dir_for(self.root, rel) is None:
            raise ValueError(f"{rel} is not a board directory.\nBoards must live one level under boards/.")
        board = BoardConfig(name=name, pcb_path=rel)
        self.config.boards[name] = board
        self.ws.save_config()
        return board

    # =====================================================================
    # Deleting boards
    # =====================================================================

    def delete_board(self, name: str) -> Optional[Path]:
        """
        Remove a board: move its directory to the trash and drop it from config.

        Returns where it went, so the UI can tell the user how to get it back.

        Never ``shutil.rmtree``. v12 derived the directory from ``pcb_path``,
        guarded it with the substring test ``"boards" in str(path)``, and could
        therefore be pointed at the project's parent by an empty ``pcb_path``.
        """
        board = self.config.boards.get(name)
        if board is None:
            raise ValueError(f"Board '{name}' is not in this project.")

        pcb = self.ws.board_pcb(name)
        if pcb is not None and pcb.exists():
            self._require_free(name)

        board_dir = board_dir_for(self.root, board.pcb_path)
        trashed = None
        if board_dir is not None and board_dir.exists():
            trashed = trash_board_dir(self.root, board_dir)
        elif board.pcb_path:
            self.ws.log(
                f"Refusing to delete files for '{name}': "
                f"{board.pcb_path!r} is not a board directory. Removing config entry only."
            )

        self.config.forget_board(name)
        self.ws.save_config()
        self._drop_block_footprint(name)
        return trashed

    def _drop_block_footprint(self, name: str) -> None:
        path = self.root / f"{BLOCK_LIB_NAME}.pretty" / f"Block_{name}.kicad_mod"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def rename_board(self, old: str, new: str) -> None:
        """Rename a board, carrying assignments, rules, and its colour along."""
        new = new.strip()
        if new in self.config.boards:
            raise ValueError(f"A board named '{new}' already exists.")
        self.config.rename_board(old, new)
        self.ws.save_config()
        self._drop_block_footprint(old)
        board = self.config.boards.get(new)
        if board:
            try:
                self.regenerate_block(board)
            except Exception:
                pass

    # =====================================================================
    # Block footprints
    # =====================================================================

    def regenerate_block(self, board: BoardConfig) -> None:
        """Rewrite one board's block footprint and register the library."""
        spec = BlockSpec(
            name=board.name,
            width_mm=board.block_width,
            height_mm=board.block_height,
            ports=list(board.ports.values()),
            description=board.description or f"Board block: {board.name}",
        )
        self.backend.write_block_footprint(self.root / f"{BLOCK_LIB_NAME}.pretty", spec)
        self.ws.register_library(BLOCK_LIB_NAME, f"{BLOCK_LIB_NAME}.pretty")

    def regenerate_all_blocks(self) -> tuple[int, list[str]]:
        """
        Rewrite every block footprint. This is Doctor's repair for v12 damage.

        Every block footprint v12 ever wrote carries two stray closing parens and
        cannot be parsed by KiCad, so an upgraded project needs exactly this.
        """
        done, failed = 0, []
        for name, board in self.config.boards.items():
            try:
                self.regenerate_block(board)
                done += 1
            except Exception as exc:
                failed.append(f"{name}: {exc}")
        return done, failed

    def regenerate_port_markers(self) -> int:
        """
        Write a marker footprint for every declared port.

        v12 had this function but never called it from anywhere, so the
        ``MultiBoard_Ports`` library the README documents was never created.
        """
        names = {p.name for b in self.config.boards.values() for p in b.ports.values()}
        lib = self.root / f"{PORT_LIB_NAME}.pretty"
        for port_name in sorted(names):
            self.backend.write_port_footprint(lib, port_name)
        if names:
            self.ws.register_library(PORT_LIB_NAME, f"{PORT_LIB_NAME}.pretty")
        return len(names)

    # =====================================================================
    # Updating
    # =====================================================================

    def apply_update(
        self,
        board_name: str,
        plan: UpdatePlan,
        *,
        progress: Optional[Callable[[int, str], None]] = None,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> ApplyResult:
        """
        Write a reviewed plan to a board.

        Re-entrancy is blocked outright. v12 showed its progress dialog
        non-modally and pumped the event loop from inside the update, so a second
        Update could start while the first was mid-write -- two runs sharing one
        temp netlist and both calling SaveBoard on the same file.
        """
        if self._busy:
            raise BoardBusy("An update is already running.")

        pcb = self._require_free(board_name)
        netlist = netlist_path(self.root)
        if not netlist.exists():
            raise ValueError("No netlist is available. Refresh first so the schematic can be exported.")

        self._busy = True
        try:
            result = self.backend.apply_update(
                pcb,
                plan,
                netlist,
                lib_paths=self.ws.lib_paths(),
                progress=progress,
                cancel=cancel,
            )
        finally:
            self._busy = False

        if not result.cancelled:
            self.index.refresh(force=False, netlist=netlist)
        return result

    def relink_schematics(self) -> tuple[int, list[str]]:
        """Re-establish every board's schematic link. Doctor's repair."""
        source = self.ws.root_schematic()
        if source is None:
            return 0, ["No root schematic is configured."]

        repaired, failed = 0, []
        for name, board in self.config.boards.items():
            board_dir = board_dir_for(self.root, board.pcb_path)
            if board_dir is None:
                continue
            try:
                if link_file(source, board_dir / f"{board_dir.name}.kicad_sch") != "already":
                    repaired += 1
            except SchematicLinkError as exc:
                failed.append(f"{name}: {exc}")
        return repaired, failed
