# Copyright (c) 2026 Eliot Abramo
# SPDX-License-Identifier: MIT

"""
IPC API backend -- a deliberate stub, kept as a record and a landing site.

KiCad 11 removes the SWIG ``pcbnew`` bindings this plugin is built on. The
replacement is the IPC API (protobuf over NNG, driven from Python by ``kipy``).
Porting is therefore not optional, only deferred.

Why it is deferred rather than done
-----------------------------------
As of KiCad 10.0.5, ``kipy`` 0.7.1 cannot do three things this plugin's core
workflow requires:

1. **No schematic access.** ``get_schematic()`` exists in the API surface but
   ``schematic_commands.proto`` on the 10.0 branch contains no messages. Our
   entire source of truth is the schematic.
2. **No headless operation.** The API only attaches to a running GUI instance,
   so nothing here would work in CI.
3. **No document or project switching.** The proto states this outright, with a
   comment that it is not planned soon because KiCad does not want API access to
   changing which project is open. Creating, opening, and updating *other*
   boards is the entire point of a multi-board manager.

What already survives the transition
------------------------------------
Everything in ``multiboard.core`` -- the index, search, reconciliation, rules,
planning, DRC, fab, doctor -- needs no pcbnew and no IPC. So does
``multiboard.cli``. Roughly 85% of the plugin is already portable; this file
covers the remaining 15%.

What this class will need to implement
--------------------------------------
* ``new_board`` -- via ``SaveCopyOfDocument`` from a template, or by writing the
  file directly (the format is stable and we already parse it).
* ``write_block_footprint`` / ``write_port_footprint`` -- ``CreateItems`` into a
  footprint document, or direct file emission through ``core.sexpr``.
* ``apply_update`` -- ``CreateItems`` / ``UpdateItems`` / ``DeleteItems`` inside
  a ``BeginCommit`` / ``EndCommit`` pair, which would be a genuine improvement:
  the user would get a single undoable transaction rather than a saved file.
* ``focus_reference`` -- ``AddToSelection`` plus ``RunAction`` for zoom.
* ``active_board_path`` -- ``GetOpenDocuments``.

Two features worth adopting at the same time: ``InjectDrcError`` to surface
cross-board conflicts as native DRC markers, and ``GetPluginSettingsPath`` for
persistent storage outside the project.
"""

from pathlib import Path
from typing import Optional

from .base import Backend, BlockSpec

UNAVAILABLE = (
    "The IPC API backend is not implemented yet.\n\n"
    "KiCad 10's IPC API cannot read schematics, cannot run headless, and cannot "
    "open or switch documents, so it cannot yet host a multi-board workflow. "
    "This plugin uses the SWIG pcbnew bindings, which KiCad 10 still supports."
)


class IpcBackend(Backend):
    """Placeholder. Every method raises with an explanation."""

    name = "ipc"

    def version(self) -> tuple[int, ...]:
        raise NotImplementedError(UNAVAILABLE)

    def new_board(self, path: Path) -> None:
        raise NotImplementedError(UNAVAILABLE)

    def active_board_path(self) -> Optional[Path]:
        return None

    def write_block_footprint(self, lib_dir: Path, spec: BlockSpec) -> None:
        raise NotImplementedError(UNAVAILABLE)

    def write_port_footprint(self, lib_dir: Path, port_name: str) -> None:
        raise NotImplementedError(UNAVAILABLE)

    def apply_update(self, *args, **kwargs):
        raise NotImplementedError(UNAVAILABLE)
