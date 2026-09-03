# Copyright (c) 2026 Eliot Abramo
# SPDX-License-Identifier: MIT

"""
Cross-board "reveal this component" handoff.

Clicking a component in the index should take you to it. When the component is
on the board already open in this KiCad instance, the backend can select and
zoom to it directly. When it is on a *different* board there is nothing we can
do in-process: KiCad 10's IPC API explicitly refuses to open or switch
documents, and there is no SWIG equivalent.

So we leave a note. Opening the target board launches KiCad on it; the next time
the plugin runs with that board active it consumes the note and reveals the
component. The request expires so an abandoned one cannot ambush the user days
later.
"""

import time
from pathlib import Path
from typing import Optional

from ..constants import FOCUS_REQUEST_MAX_AGE, PENDING_FOCUS_FILE
from .cache import atomic_write_json, read_json
from .project import work_dir


def request(root: Path, board: str, ref: str) -> None:
    """Record that ``ref`` should be revealed the next time ``board`` is active."""
    atomic_write_json(
        work_dir(root) / PENDING_FOCUS_FILE,
        {"board": board, "ref": ref, "ts": time.time()},
        backup=False,
    )


def take(root: Path, board: str, *, max_age: float = FOCUS_REQUEST_MAX_AGE) -> Optional[str]:
    """
    Consume a pending request for ``board``, returning the reference to reveal.

    Consumed on read -- a request fires exactly once. Returns None when there is
    no request, when it names a different board, or when it has expired.
    """
    path = work_dir(root) / PENDING_FOCUS_FILE
    if not path.exists():
        return None

    try:
        data, _ = read_json(path, recover=False)
    except Exception:
        path.unlink(missing_ok=True)
        return None

    path.unlink(missing_ok=True)

    if not isinstance(data, dict) or data.get("board") != board:
        return None
    if time.time() - float(data.get("ts", 0)) > max_age:
        return None

    ref = str(data.get("ref") or "")
    return ref or None


def clear(root: Path) -> None:
    """Drop any pending request. Used when the user cancels."""
    (work_dir(root) / PENDING_FOCUS_FILE).unlink(missing_ok=True)
