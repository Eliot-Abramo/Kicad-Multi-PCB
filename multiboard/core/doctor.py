"""
Preflight self-check with one-click repairs.

Most of v12's failure modes were diagnosable but not diagnosed: a missing
kicad-cli, a broken schematic link, an unresolvable library variable, a stale
lock file, or a corrupt generated footprint library all surfaced later as some
unrelated-looking error. Doctor turns each into a named check with a fix button.

Pure Python: it runs identically in the dialog and in ``multiboard doctor``.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..constants import BLOCK_LIB_NAME, BOARDS_DIR, TRASH_DIR
from . import kicad_env
from .config import ProjectConfig
from .pcb_scan import validate_footprint_library
from .project import board_dir_for, can_link, stale_locks, work_dir

OK = "ok"
INFO = "info"
WARN = "warn"
ERROR = "error"

_SEVERITY = {OK: 0, INFO: 1, WARN: 2, ERROR: 3}


@dataclass
class Check:
    """One diagnostic, optionally with a repair."""

    id: str
    level: str
    title: str
    detail: str = ""
    fix: Optional[Callable[[], str]] = None
    fix_label: str = ""

    @property
    def needs_attention(self) -> bool:
        return self.level in (WARN, ERROR)


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    @property
    def worst(self) -> str:
        return max((c.level for c in self.checks), key=lambda lv: _SEVERITY[lv], default=OK)

    def problems(self) -> list[Check]:
        return [c for c in self.checks if c.needs_attention]

    def fixable(self) -> list[Check]:
        return [c for c in self.problems() if c.fix is not None]

    def summary(self) -> str:
        errors = sum(1 for c in self.checks if c.level == ERROR)
        warnings = sum(1 for c in self.checks if c.level == WARN)
        if errors:
            return f"{errors} problem(s), {warnings} warning(s)"
        if warnings:
            return f"{warnings} warning(s)"
        return f"All {len(self.checks)} checks passed"

    def to_json(self) -> dict:
        return {
            "worst": self.worst,
            "summary": self.summary(),
            "checks": [
                {
                    "id": c.id,
                    "level": c.level,
                    "title": c.title,
                    "detail": c.detail,
                    "fixable": c.fix is not None,
                }
                for c in self.checks
            ],
        }

    def to_text(self) -> str:
        marks = {OK: "  ok  ", INFO: " info ", WARN: " warn ", ERROR: "ERROR "}
        lines = [f"Doctor: {self.summary()}", ""]
        for c in self.checks:
            lines.append(f"[{marks[c.level]}] {c.title}")
            if c.detail and c.level != OK:
                for line in c.detail.splitlines():
                    lines.append(f"           {line}")
        return "\n".join(lines) + "\n"


def run_all(root: Path, cfg: ProjectConfig, *, backend=None) -> Report:
    """Run every check. ``backend`` is optional so this works from the CLI."""
    report = Report()
    add = report.checks.append

    install = kicad_env.discover()
    add(_check_cli(install))
    add(_check_version_match(install, backend))
    add(_check_root_schematic(root, cfg))
    add(_check_link_capability(root))
    add(_check_links(root, cfg))
    add(_check_boards_exist(root, cfg))
    add(_check_board_paths(root, cfg))
    add(_check_block_library(root))
    add(_check_locks(root))
    add(_check_orphan_dirs(root, cfg))
    add(_check_rules(cfg))
    add(_check_cache_writable(root))
    add(_check_lxml())
    add(_check_trash(root))
    return report


# =============================================================================
# Checks
# =============================================================================


def _check_cli(install) -> Check:
    if install is None:
        return Check(
            "kicad_cli",
            ERROR,
            "kicad-cli not found",
            "Netlist export, DRC, and fabrication output all need it.\n\n"
            "Set the KICAD_CLI environment variable to its full path. Typical locations:\n"
            "  macOS   /Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli\n"
            "  Windows C:\\Program Files\\KiCad\\10.0\\bin\\kicad-cli.exe\n"
            "  Linux   /usr/bin/kicad-cli",
            fix=lambda: _redetect(),
            fix_label="Re-detect",
        )
    if install.cli is None:
        return Check(
            "kicad_cli",
            ERROR,
            f"{install.describe()} found, but kicad-cli was not",
            "Set KICAD_CLI to the kicad-cli binary inside your KiCad installation.",
            fix=lambda: _redetect(),
            fix_label="Re-detect",
        )

    from ..version import MAX_KICAD, MIN_KICAD

    if not (MIN_KICAD <= install.version[:2] <= MAX_KICAD):
        found = ".".join(str(p) for p in install.version)
        return Check(
            "kicad_cli",
            ERROR,
            f"kicad-cli reports version {found}, which is not supported",
            f"This plugin targets KiCad {MIN_KICAD[0]}.{MIN_KICAD[1]}-{MAX_KICAD[0]}.x.\n"
            f"Found: {install.cli}\n\n"
            "If you have KiCad 10 installed elsewhere, set KICAD_CLI to its "
            "kicad-cli and re-detect.",
            fix=lambda: _redetect(),
            fix_label="Re-detect",
        )

    return Check("kicad_cli", OK, f"kicad-cli found ({install.describe()})", str(install.cli))


def _check_version_match(install, backend) -> Check:
    if install is None or backend is None:
        return Check("version_match", INFO, "KiCad version match not checked")

    try:
        running = tuple(backend.version())
    except Exception:
        return Check("version_match", INFO, "Could not read the running KiCad version")

    if running[:2] != install.version[:2]:
        return Check(
            "version_match",
            WARN,
            "kicad-cli is a different KiCad version than this editor",
            f"Editor {'.'.join(map(str, running))}, "
            f"kicad-cli {'.'.join(map(str, install.version))}.\n"
            "Netlists and DRC reports may not match what the editor produces. "
            "Set KICAD_CLI to the matching binary.",
        )
    return Check("version_match", OK, f"KiCad {install.major_minor} throughout")


def _check_root_schematic(root: Path, cfg: ProjectConfig) -> Check:
    if not cfg.root_schematic:
        return Check(
            "root_schematic",
            ERROR,
            "No root schematic configured",
            "Every board is driven from one schematic. Set it in Settings.",
        )
    path = root / cfg.root_schematic
    if not path.exists():
        return Check(
            "root_schematic", ERROR, f"Root schematic missing: {cfg.root_schematic}", f"Expected at {path}"
        )
    return Check("root_schematic", OK, f"Root schematic: {cfg.root_schematic}")


def _check_link_capability(root: Path) -> Check:
    ok, detail = can_link(work_dir(root))
    if ok:
        return Check("link_capability", OK, f"Schematic linking works ({detail})")
    return Check(
        "link_capability",
        ERROR,
        "This location cannot hold linked schematics",
        detail + "\n\nCommon causes: the project is on a network drive; boards/ is on a "
        "different filesystem; Windows without Developer Mode or Administrator.",
    )


def _check_links(root: Path, cfg: ProjectConfig) -> Check:
    """Each board's schematic must be the same file as the root, not a copy."""
    import os

    if not cfg.root_schematic or not cfg.boards:
        return Check("links_valid", OK, "No board schematics to verify")

    source = root / cfg.root_schematic
    if not source.exists():
        return Check("links_valid", WARN, "Cannot verify links without the root schematic")

    broken = []
    for name, board in cfg.boards.items():
        d = board_dir_for(root, board.pcb_path)
        if d is None:
            continue
        dest = d / f"{d.name}.kicad_sch"
        if not dest.exists():
            broken.append(f"{name}: no schematic link")
            continue
        try:
            if not os.path.samefile(str(source), str(dest)):
                broken.append(f"{name}: schematic is a copy, not a link")
        except OSError as exc:
            broken.append(f"{name}: {exc}")

    if broken:
        return Check(
            "links_valid",
            WARN,
            f"{len(broken)} board schematic link(s) need repair",
            "\n".join(broken) + "\n\nA copied schematic drifts from the root and the boards stop agreeing.",
            fix=lambda: _repair_links(root, cfg),
            fix_label="Repair links",
        )
    return Check("links_valid", OK, f"All {len(cfg.boards)} board schematics are linked")


def _check_boards_exist(root: Path, cfg: ProjectConfig) -> Check:
    missing = [name for name, b in cfg.boards.items() if not b.pcb_path or not (root / b.pcb_path).exists()]
    if missing:
        return Check(
            "boards_exist",
            ERROR,
            f"{len(missing)} board PCB(s) missing",
            "\n".join(f"{n}: {cfg.boards[n].pcb_path or '(no path recorded)'}" for n in missing),
        )
    return Check("boards_exist", OK, f"{len(cfg.boards)} board(s) present")


def _check_board_paths(root: Path, cfg: ProjectConfig) -> Check:
    """
    A board whose path fails the safety guard cannot be deleted from the UI.

    v12 could reach a state where a board's ``pcb_path`` was empty, which made
    its delete target the project's *parent* directory.
    """
    bad = [name for name, b in cfg.boards.items() if b.pcb_path and board_dir_for(root, b.pcb_path) is None]
    empty = [name for name, b in cfg.boards.items() if not b.pcb_path]

    if empty or bad:
        return Check(
            "board_paths",
            WARN,
            "Some boards have unusable paths",
            "".join(
                [f"{n}: no PCB path recorded\n" for n in empty]
                + [f"{n}: {cfg.boards[n].pcb_path} is not inside boards/\n" for n in bad]
            )
            + "\nThese boards cannot be deleted from the UI, by design.",
        )
    return Check("board_paths", OK, "All board paths are well formed")


def _check_block_library(root: Path) -> Check:
    """
    Detect the damage v12's footprint generator left behind.

    Its generator emitted two stray closing parens into every file it wrote, so
    every Block_*.kicad_mod in every project ever created with v12 is
    unparseable. Nothing reported it because nothing ever tried to read them.
    """
    lib = root / f"{BLOCK_LIB_NAME}.pretty"
    if not lib.is_dir():
        return Check("block_lib", OK, "No block footprint library yet")

    problems = validate_footprint_library(lib)
    if problems:
        return Check(
            "block_lib",
            WARN,
            f"{len(problems)} block footprint(s) are malformed",
            "\n".join(problems) + "\n\nBlock footprints written by version 12 of this plugin all carry "
            "stray closing parentheses and cannot be opened by KiCad.",
            fix=lambda: "Use Ports > Regenerate on each board, or Regenerate all blocks.",
            fix_label="Regenerate blocks",
        )
    count = len(list(lib.glob("*.kicad_mod")))
    return Check("block_lib", OK, f"{count} block footprint(s) parse correctly")


def _check_locks(root: Path) -> Check:
    locks = stale_locks(root)
    if locks:
        return Check(
            "locks",
            INFO,
            f"{len(locks)} board(s) currently open in KiCad",
            "\n".join(p.name for p in locks)
            + "\n\nOpen boards cannot be updated or deleted. If KiCad crashed, these "
            "lock files may be stale.",
            fix=lambda: _clear_locks(root),
            fix_label="Clear lock files",
        )
    return Check("locks", OK, "No boards are locked")


def _check_orphan_dirs(root: Path, cfg: ProjectConfig) -> Check:
    boards_dir = root / BOARDS_DIR
    if not boards_dir.is_dir():
        return Check("orphan_dirs", OK, "No boards directory yet")

    known = {board_dir_for(root, b.pcb_path) for b in cfg.boards.values() if b.pcb_path}
    orphans = [
        d.name
        for d in sorted(boards_dir.iterdir())
        if d.is_dir() and d.name != TRASH_DIR and d.resolve() not in known and any(d.glob("*.kicad_pcb"))
    ]
    if orphans:
        return Check(
            "orphan_dirs",
            WARN,
            f"{len(orphans)} board director(ies) not in the config",
            "\n".join(orphans) + "\n\nThey exist on disk but this project does not know "
            "about them. Import them, or move them out of boards/.",
        )
    return Check("orphan_dirs", OK, "No unmanaged board directories")


def _check_rules(cfg: ProjectConfig) -> Check:
    from .rules import rule_error

    broken = [
        f"rule {i + 1} ({r.label().lower()} {r.pattern!r}): {err}"
        for i, r in enumerate(cfg.rules)
        if (err := rule_error(r))
    ]
    if broken:
        return Check(
            "rules",
            WARN,
            f"{len(broken)} assignment rule(s) are invalid",
            "\n".join(broken) + "\n\nInvalid rules never match, so components "
            "they were meant to claim end up unassigned.",
        )
    if not cfg.rules and cfg.boards:
        return Check(
            "rules",
            INFO,
            "No assignment rules configured",
            "Without rules, every component's board is whichever one you happen to "
            "place it on. Rules let you decide up front -- try 'Suggest from sheets'.",
        )
    return Check("rules", OK, f"{len(cfg.rules)} assignment rule(s) valid")


def _check_cache_writable(root: Path) -> Check:
    try:
        probe = work_dir(root) / ".writable"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return Check("cache_writable", OK, "Index cache is writable")
    except OSError as exc:
        return Check(
            "cache_writable",
            WARN,
            "Cannot write the index cache",
            f"{exc}\n\nSearches will still work but will be slower.",
        )


def _check_lxml() -> Check:
    try:
        import lxml  # noqa: F401

        return Check("lxml", OK, "lxml available (faster netlist parsing)")
    except ImportError:
        return Check(
            "lxml",
            INFO,
            "lxml not installed",
            "The standard library parser is used instead. Only noticeable on very large designs.",
        )


def _check_trash(root: Path) -> Check:
    trash = root / BOARDS_DIR / TRASH_DIR
    if not trash.is_dir():
        return Check("trash", OK, "Trash is empty")
    entries = [d for d in trash.iterdir() if d.is_dir()]
    if not entries:
        return Check("trash", OK, "Trash is empty")

    size = sum(f.stat().st_size for d in entries for f in d.rglob("*") if f.is_file())
    return Check(
        "trash",
        INFO,
        f"{len(entries)} deleted board(s) in the trash ({size // 1024} KB)",
        "Deleted boards are moved here rather than erased, so a mistake is recoverable.\n"
        + "\n".join(d.name for d in entries),
        fix=lambda: _empty_trash(root),
        fix_label="Empty trash",
    )


# =============================================================================
# Fixes
# =============================================================================


def _redetect() -> str:
    kicad_env.invalidate()
    install = kicad_env.discover(refresh=True)
    return f"Found {install.describe()}" if install else "Still could not find kicad-cli."


def _repair_links(root: Path, cfg: ProjectConfig) -> str:
    from .project import link_file

    source = root / cfg.root_schematic
    repaired, failed = 0, []
    for name, board in cfg.boards.items():
        d = board_dir_for(root, board.pcb_path)
        if d is None:
            continue
        try:
            if link_file(source, d / f"{d.name}.kicad_sch") != "already":
                repaired += 1
        except Exception as exc:
            failed.append(f"{name}: {exc}")

    if failed:
        return f"Repaired {repaired}; {len(failed)} failed:\n" + "\n".join(failed)
    return f"Repaired {repaired} schematic link(s)."


def _clear_locks(root: Path) -> str:
    removed = 0
    for lock in stale_locks(root):
        try:
            lock.unlink()
            removed += 1
        except OSError:
            pass
    return f"Removed {removed} lock file(s). Make sure those boards are closed in KiCad."


def _empty_trash(root: Path) -> str:
    import shutil

    trash = root / BOARDS_DIR / TRASH_DIR
    removed = 0
    for d in list(trash.iterdir()) if trash.is_dir() else []:
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return f"Permanently removed {removed} deleted board(s)."
