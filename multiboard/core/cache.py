"""
Atomic JSON persistence.

v12 wrote its config with a plain ``write_text``: truncate, then write. A crash
or a full disk mid-write left a zero-length ``.kicad_multiboard.json`` and there
was no backup, so the project's board list was simply gone. Every write in v13
goes through :func:`atomic_write_json`.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Optional


class ConfigCorrupt(Exception):
    """Raised when a JSON file and its backup are both unreadable."""


def atomic_write_json(path: Path, obj: Any, *, backup: bool = True, indent: Optional[int] = 2) -> None:
    """
    Write ``obj`` to ``path`` such that ``path`` is never partially written.

    Writes to a temporary file in the same directory (so ``os.replace`` stays on
    one filesystem and is therefore atomic), fsyncs it, copies the previous
    contents aside as ``.bak``, then replaces.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(obj, fh, indent=indent, sort_keys=True, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())

        if backup and path.exists():
            try:
                shutil.copy2(str(path), str(path.with_suffix(path.suffix + ".bak")))
            except OSError:
                pass  # a missing backup must not block the write itself

        os.replace(str(tmp), str(path))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def read_json(path: Path, *, recover: bool = True) -> tuple[Any, Optional[str]]:
    """
    Read JSON, falling back to the ``.bak`` written by :func:`atomic_write_json`.

    Returns ``(data, warning)``. ``warning`` is a human-readable string when the
    primary file was unusable and the backup was used instead, so the caller can
    surface it rather than silently continuing with recovered data. v12 caught
    the exception, logged it, and carried on with an empty config -- which
    presented as "all my boards disappeared".
    """
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        raise
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        if not recover:
            raise ConfigCorrupt(f"{path.name}: {exc}") from exc

    bak = path.with_suffix(path.suffix + ".bak")
    try:
        data = json.loads(bak.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise ConfigCorrupt(f"{path.name} is unreadable and no usable backup exists ({exc})") from exc

    return data, f"{path.name} was corrupt; recovered from {bak.name}"


def write_json_compact(path: Path, obj: Any) -> None:
    """Atomic write with no indentation, for machine-only files like the index cache."""
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(obj, fh, separators=(",", ":"), ensure_ascii=False)
        os.replace(str(tmp), str(path))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def file_key(path: Path) -> Optional[tuple[int, int]]:
    """
    ``(mtime_ns, size)`` identity for cache invalidation, or None if absent.

    mtime alone is not enough: a file rewritten within the same filesystem
    timestamp granularity would look unchanged. Pairing it with the size catches
    essentially every real edit at zero cost.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)
