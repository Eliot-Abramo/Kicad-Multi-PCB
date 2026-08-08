"""
Project layout, path safety, schematic linking, and lock detection.

Three of v12's defects were data-loss bugs living in this area, and all three
came from paths being trusted:

* ``_on_remove`` derived a board directory from ``pcb_path``; an empty
  ``pcb_path`` (reachable from a legacy config) made it the project's *parent*,
  and the only guard was the substring test ``"boards" in str(path)`` -- which
  any path containing the letters "boards" satisfies. Then ``shutil.rmtree``.
* ``_setup_board_project`` joined sheet paths lifted straight out of the
  schematic by regex. ``Path("/a/b") / "/etc/x.kicad_sch"`` is
  ``/etc/x.kicad_sch``, so an absolute reference made destination == source, and
  ``_link_file`` unlinked the destination first -- deleting the user's schematic.
* ``_link_file`` unlinked the existing link before knowing it could recreate it,
  so any transient failure lost a working board's schematic.

The functions here are the chokepoints that close all three.
"""

import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..constants import BOARDS_DIR, CONFIG_FILE, TRASH_DIR, WORK_DIR

RE_SHEET_REF = re.compile(r'\(sheet_file\s+"([^"]+\.kicad_sch)"\)|"([^"]+\.kicad_sch)"')


class SchematicLinkError(Exception):
    """A schematic could not be hardlinked or symlinked into a board directory."""


class UnsafePath(ValueError):
    """A path derived from file contents escaped the tree it must stay inside."""


# =============================================================================
# Names
# =============================================================================

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_board_name(name: str) -> str:
    """
    Turn a board name into a filesystem-safe directory name.

    One implementation, used by both the UI validator and the manager. v12 had
    two different ones, so ``"A B"`` was accepted by the dialog and became
    directory ``A_B`` -- as did ``"A/B"``, silently colliding.

    Accents are folded rather than stripped so ``Alimentación`` stays readable.
    """
    folded = unicodedata.normalize("NFKD", name)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    safe = _SAFE_CHARS.sub("_", folded).strip("._-")
    return safe or "board"


def is_valid_board_name(name: str) -> Optional[str]:
    """Return an error message if ``name`` is unusable as a board name, else None."""
    stripped = name.strip()
    if not stripped:
        return "Board name cannot be empty."
    if not any(c.isalnum() for c in stripped):
        return "Board name must contain at least one letter or digit."
    if len(stripped) > 64:
        return "Board name must be 64 characters or fewer."
    if sanitize_board_name(stripped).lower() in {"con", "prn", "aux", "nul", "trash"}:
        return f"'{stripped}' is a reserved name."
    return None


# =============================================================================
# Path safety
# =============================================================================


def safe_relative(base: Path, candidate: str) -> Optional[Path]:
    """
    Resolve ``candidate`` under ``base``, or None if it would escape.

    Rejects absolute paths, drive letters, UNC paths, and any ``..`` component.
    Applied to every path lifted out of a KiCad file before it is joined to a
    directory we are about to write into.
    """
    if not candidate:
        return None

    raw = candidate.replace("\\", "/").strip()
    if not raw or raw.startswith("/") or raw.startswith("//"):
        return None
    if len(raw) >= 2 and raw[1] == ":":  # C:\...
        return None

    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    if not parts:
        return None

    resolved = base.joinpath(*parts)
    try:
        resolved.relative_to(base) if not resolved.is_absolute() else None
        # Compare resolved forms so symlinks cannot be used to escape either.
        base_r = base.resolve()
        cand_r = resolved.resolve()
        cand_r.relative_to(base_r)
    except (ValueError, OSError):
        return None

    return resolved


def board_dir_for(root: Path, rel_pcb: str) -> Optional[Path]:
    """
    The directory to delete for a board, or None if the input is not trustworthy.

    Replaces v12's ``"boards" in str(path)`` substring check. A board directory
    must be *exactly one level* under ``<root>/boards`` and its PCB must actually
    be a ``.kicad_pcb``. Anything else -- notably an empty ``rel_pcb`` -- returns
    None and the caller refuses to delete.
    """
    if not rel_pcb or not rel_pcb.strip():
        return None

    rel = safe_relative(root, rel_pcb)
    if rel is None:
        return None
    if rel.suffix != ".kicad_pcb":
        return None

    try:
        boards_root = (root / BOARDS_DIR).resolve()
        parent = rel.resolve().parent
    except OSError:
        return None

    if parent == boards_root:
        return None  # PCB sitting directly in boards/, no directory of its own
    if parent.parent != boards_root:
        return None  # not exactly one level deep

    return parent


def trash_board_dir(root: Path, board_dir: Path) -> Path:
    """
    Move a board directory into ``boards/.trash/<name>-<timestamp>/``.

    Deletion is never ``shutil.rmtree``. A rename is atomic on the same
    filesystem, reversible by the user with a file manager, and cannot escape the
    project tree. Doctor offers "empty trash" once the user is confident.
    """
    trash = root / BOARDS_DIR / TRASH_DIR
    trash.mkdir(parents=True, exist_ok=True)
    (trash / ".gitignore").write_text("*\n", encoding="utf-8")

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = trash / f"{board_dir.name}-{stamp}"
    n = 1
    while dest.exists():
        n += 1
        dest = trash / f"{board_dir.name}-{stamp}-{n}"

    os.replace(str(board_dir), str(dest))
    return dest


# =============================================================================
# Project root
# =============================================================================


def find_project_root(start: Path) -> Path:
    """
    Walk upward from ``start`` to the multi-board project root.

    v12's fallback returned the first directory containing any ``*.kicad_pro``.
    Board directories contain one, so opening a sub-board with no config present
    made ``boards/Power/`` the "project root" and everything nested wrongly.
    We now skip any directory whose parent is named ``boards``.
    """
    start = start.resolve() if start.exists() else start
    chain = [start, *start.parents]

    for d in chain:
        if (d / CONFIG_FILE).exists():
            return d

    for d in chain:
        if (d / BOARDS_DIR).is_dir() and any(d.glob("*.kicad_pro")):
            return d

    fallback: Optional[Path] = None
    for d in chain:
        if d.parent.name == BOARDS_DIR:
            continue  # this is a sub-board directory, not a project root
        if any(d.glob("*.kicad_pro")):
            fallback = d  # keep going; prefer the outermost match
    return fallback or start


def detect_root_files(root: Path) -> list[dict]:
    """
    Candidate root projects, best first.

    v12 took the first ``*.kicad_pro`` in nondeterministic glob order, broke out
    of the loop even when it had no matching schematic, and then overwrote the
    persisted ``root_schematic`` on *every* load -- so a correct manual choice
    could not survive. This returns all candidates deterministically and lets
    onboarding ask when the answer is ambiguous.
    """
    out = []
    for pro in sorted(root.glob("*.kicad_pro")):
        sch = pro.with_suffix(".kicad_sch")
        pcb = pro.with_suffix(".kicad_pcb")
        out.append(
            {
                "project": pro.name,
                "schematic": sch.name if sch.exists() else "",
                "pcb": pcb.name if pcb.exists() else "",
                "has_schematic": sch.exists(),
            }
        )
    out.sort(key=lambda c: (not c["has_schematic"], c["project"]))
    return out


def work_dir(root: Path) -> Path:
    """Ensure and return the per-project scratch directory."""
    d = root / WORK_DIR
    d.mkdir(parents=True, exist_ok=True)
    gitignore = d / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n", encoding="utf-8")
    return d


# =============================================================================
# Schematic linking
# =============================================================================


def link_file(source: Path, dest: Path) -> str:
    """
    Link ``source`` to ``dest``, preferring a hardlink. Returns what it did.

    Ordering is the whole point:

    1. If they are already the same file, do nothing. v12 unlinked and relinked
       on every single update.
    2. Refuse outright if they resolve to the same path -- this is the check that
       stops a malformed sheet reference from deleting the user's schematic.
    3. Build the replacement at a temporary name, then ``os.replace`` it over the
       destination. **The destination is never removed until its replacement
       exists**, so a failure leaves the previous working state intact.
    """
    if not source.exists():
        raise SchematicLinkError(f"Source schematic not found: {source}")

    # Path identity must be checked BEFORE inode identity. os.path.samefile is
    # true both when the link already exists (fine, nothing to do) and when
    # `dest` literally *is* `source` (a malformed sheet reference). Returning
    # "already" for the second case would be safe but would silently hide the
    # fact that this board never got its own linked sheet.
    try:
        if dest.resolve() == source.resolve() and str(dest) == str(source):
            raise SchematicLinkError(
                f"Refusing to link {source.name} onto itself.\n"
                "The schematic references a sheet by an absolute or escaping path."
            )
    except OSError:
        pass

    try:
        if dest.exists() and os.path.samefile(str(source), str(dest)):
            return "already"
    except OSError:
        pass

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".mb-tmp")
    tmp.unlink(missing_ok=True)

    errors = []
    for kind, fn in (("hardlink", os.link), ("symlink", os.symlink)):
        try:
            fn(str(source), str(tmp))
            os.replace(str(tmp), str(dest))
            return kind
        except OSError as exc:
            errors.append(f"{kind}: {exc}")
            tmp.unlink(missing_ok=True)

    raise SchematicLinkError(
        f"Could not link {source.name} into {dest.parent.name}.\n\n"
        + "\n".join(errors)
        + "\n\nCommon causes: the project and boards/ are on different filesystems; "
        "Windows without Developer Mode or Administrator; a network drive."
    )


def can_link(directory: Path) -> tuple:
    """
    Probe whether linking actually works here. Returns ``(ok, detail)``.

    Onboarding runs this for real -- creating and deleting a test link -- rather
    than assuming, because the failure is platform- and filesystem-specific and
    the error a user gets otherwise arrives much later and out of context.
    """
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / ".mb-linkprobe"
    target = directory / ".mb-linkprobe-link"
    try:
        probe.write_text("probe", encoding="utf-8")
        try:
            os.link(str(probe), str(target))
            return True, "hardlink"
        except OSError as exc:
            hard_err = exc
        try:
            os.symlink(str(probe), str(target))
            return True, "symlink"
        except OSError as exc:
            return False, f"hardlink: {hard_err}; symlink: {exc}"
    except OSError as exc:
        return False, str(exc)
    finally:
        target.unlink(missing_ok=True)
        probe.unlink(missing_ok=True)


def find_hierarchical_sheets(root_sch: Path, _seen: Optional[set[Path]] = None) -> set[str]:
    """
    Relative paths of every sheet reachable from ``root_sch``.

    Returns strings relative to the root schematic's directory. Every one is
    passed through :func:`safe_relative` by the caller before use; this function
    does not itself guarantee safety, only reachability.
    """
    seen: set[Path] = _seen if _seen is not None else set()
    out: set[str] = set()
    base = root_sch.parent

    stack = [root_sch]
    while stack:
        current = stack.pop()
        try:
            current_r = current.resolve()
        except OSError:
            continue
        if current_r in seen or not current.exists():
            continue
        seen.add(current_r)

        try:
            content = current.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        for match in RE_SHEET_REF.finditer(content):
            ref = match.group(1) or match.group(2)
            if not ref or ref == current.name:
                continue
            child = safe_relative(base, ref)
            if child is None or not child.exists():
                continue
            try:
                out.add(child.relative_to(base).as_posix())
            except ValueError:
                continue
            stack.append(child)

    return out


# =============================================================================
# Lock files
# =============================================================================


def lock_paths(pcb: Path) -> list[Path]:
    """Lock files KiCad creates while a board is open."""
    return [pcb.with_name(f"~{pcb.name}.lck")]


def is_pcb_open(pcb: Path, active: Optional[Path] = None) -> bool:
    """
    Whether ``pcb`` is currently open in a KiCad editor.

    ``active`` is the board open in *this* pcbnew instance, if any.

    Unlike v12 this returns **False** when the check itself errors. Returning
    True on error meant an unreadable directory permanently blocked Update and
    Delete with no override; a false negative merely risks the operation being
    refused later by KiCad itself, which is the safer failure direction.
    """
    if active is not None:
        try:
            if pcb.resolve() == active.resolve():
                return True
        except OSError:
            if pcb.name == active.name:
                return True

    for lock in lock_paths(pcb):
        try:
            if lock.exists():
                return True
        except OSError:
            continue
    return False


def stale_locks(root: Path) -> list[Path]:
    """Every lock file under the project, for Doctor to offer to clear."""
    out: list[Path] = []
    boards = root / BOARDS_DIR
    if boards.is_dir():
        out.extend(sorted(boards.rglob("~*.lck")))
    out.extend(sorted(root.glob("~*.lck")))
    return out
