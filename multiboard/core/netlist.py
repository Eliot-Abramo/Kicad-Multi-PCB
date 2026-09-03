# Copyright (c) 2026 Eliot Abramo
# SPDX-License-Identifier: MIT

"""
Schematic-side data, via ``kicad-cli sch export netlist --format kicadxml``.

Two v12 defects lived here, and one of them was serious:

* The parser read ``<tstamp>``. KiCad 6 and later emit ``<tstamps>`` (plural)
  inside ``<comp>``. So ``tstamp`` was always empty, ``_set_fp_path`` never
  fired, and **no footprint was ever linked back to its schematic symbol**.
  KiCad's own "Update PCB from Schematic" then treated every part as unlinked.
* ``_export_netlist`` never checked the return code and never checked the file's
  age. If an export failed while a stale netlist from a previous run was still
  on disk, the board was silently updated from the old data.

It also never captured ``<sheetpath>``, which is what the rules engine needs to
assign components by hierarchy.
"""

import xml.etree.ElementTree as ElementTree
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..constants import WORK_DIR
from .cli_runner import CliResult, run_cli

# Property names whose mere presence (empty value) means True. KiCad writes
# boolean properties with an empty value. v12 applied this quirk to *any*
# property whose name contained "exclude" and "board", which would misfire on a
# user field named e.g. "Excludes board rev".
BOOLEAN_PROPS = {
    "dnp",
    "exclude_from_board",
    "exclude_from_bom",
    "exclude_from_sim",
    "ki_dnp",
    "ki_exclude_from_board",
    "ki_exclude_from_bom",
}
TRUTHY = {"1", "yes", "true", "y", "on", "dnp"}


@dataclass(frozen=True)
class SchComponent:
    """One symbol as the schematic sees it."""

    ref: str
    value: str = ""
    footprint: str = ""
    lib_id: str = ""
    sheetpath: str = "/"
    path: str = ""
    dnp: bool = False
    exclude_from_board: bool = False
    exclude_from_bom: bool = False
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def placeable(self) -> bool:
        """Whether this component should ever appear on a board."""
        return bool(self.footprint) and not self.dnp and not self.exclude_from_board

    def skip_reason(self) -> str:
        """Why this component is not placeable, for the UI. Empty if it is."""
        if self.dnp:
            return "DNP"
        if self.exclude_from_board:
            return "Excluded from board"
        if not self.footprint:
            return "No footprint assigned"
        return ""

    def to_row(self) -> list:
        flags = int(self.dnp) | (int(self.exclude_from_board) << 1) | (int(self.exclude_from_bom) << 2)
        return [
            self.ref,
            self.value,
            self.footprint,
            self.lib_id,
            self.sheetpath,
            self.path,
            flags,
            self.fields,
        ]

    @classmethod
    def from_row(cls, row: list) -> "SchComponent":
        flags = int(row[6])
        return cls(
            ref=row[0],
            value=row[1],
            footprint=row[2],
            lib_id=row[3],
            sheetpath=row[4],
            path=row[5],
            dnp=bool(flags & 1),
            exclude_from_board=bool(flags & 2),
            exclude_from_bom=bool(flags & 4),
            fields=dict(row[7]),
        )


class NetlistError(Exception):
    """Netlist export or parse failed, with a message fit to show the user."""


class NetlistCancelled(NetlistError):
    """The user cancelled the export. Not a failure; nothing to report."""


def netlist_path(root: Path) -> Path:
    """Where exports are written -- inside the scratch dir, never the project root."""
    return root / WORK_DIR / "netlist.xml"


def export_netlist(
    install,
    root: Path,
    root_sch: Path,
    *,
    variant: str = "",
    timeout: float = 180.0,
    pump=None,
) -> Path:
    """
    Export the root schematic to a netlist and return its path.

    Every failure mode is checked, because a silently-stale netlist corrupts a
    board: the target is removed first, the exit code is inspected, the mtime is
    compared against the start of the run, and the result must actually parse.

    The one thing checked *before* removing the target is whether kicad-cli
    exists at all. Deleting a usable netlist and then discovering we cannot
    produce a replacement would turn a missing-toolchain problem into an empty
    component index.

    ``variant`` uses KiCad 10's ``--variant`` flag for design variants.
    """
    if not root_sch.exists():
        raise NetlistError(f"Root schematic not found: {root_sch}")

    if install is None or install.cli is None:
        raise NetlistError(
            "kicad-cli could not be located, so the schematic cannot be read.\n\n"
            "Set the KICAD_CLI environment variable to its full path, or run Doctor."
        )

    out = netlist_path(root)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)

    import time

    started = time.time()

    args = ["sch", "export", "netlist", "--format", "kicadxml", "-o", str(out)]
    if variant:
        args += ["--variant", variant]
    args.append(str(root_sch))

    result: CliResult = run_cli(install, args, cwd=root, timeout=timeout, pump=pump)
    if result.cancelled:
        raise NetlistCancelled("Netlist export was cancelled.")
    if not result.ok:
        raise NetlistError(f"Netlist export failed.\n\n{result.failure_text()}")

    if not out.exists():
        raise NetlistError("Netlist export reported success but produced no file.")

    # Guard against a stale file surviving from a previous run.
    if out.stat().st_mtime < started - 1.0:
        raise NetlistError(
            f"Netlist export produced a stale file. Check that kicad-cli can write to {out.parent}."
        )

    try:
        parse_netlist(out)
    except NetlistError as exc:
        raise NetlistError(f"Netlist export produced an unreadable file: {exc}") from exc

    return out


def _etree():
    """lxml if available (roughly 3x faster), else the stdlib. One import site."""
    try:
        from lxml import etree

        return etree, True
    except ImportError:
        return ElementTree, False


def parse_netlist(path: Path) -> dict[str, SchComponent]:
    """Parse a kicadxml netlist into ``{ref: SchComponent}``."""
    etree, _ = _etree()
    try:
        root = etree.parse(str(path)).getroot()
    except Exception as exc:  # both libraries raise their own error types
        raise NetlistError(f"{path.name}: {exc}") from exc

    out: dict[str, SchComponent] = {}
    for elem in root.iter("comp"):
        comp = _parse_comp(elem)
        if comp is not None:
            out[comp.ref] = comp
    return out


def _parse_comp(elem) -> Optional[SchComponent]:
    ref = (elem.get("ref") or "").strip()
    if not ref or ref.startswith("#"):
        return None  # power and other virtual symbols

    value = footprint = lib_id = path = ""
    sheetpath = "/"
    dnp = exclude_board = exclude_bom = False
    fields: dict[str, str] = {}

    for child in elem:
        tag = child.tag
        if tag == "value":
            value = (child.text or "").strip()
        elif tag == "footprint":
            footprint = (child.text or "").strip()
        elif tag == "libsource":
            lib = child.get("lib") or ""
            part = child.get("part") or ""
            lib_id = f"{lib}:{part}" if lib else part
        elif tag == "sheetpath":
            sheetpath = (child.get("names") or "/").strip() or "/"
        elif tag in ("tstamps", "tstamp"):
            # KiCad 6+ writes <tstamps>; v12 only looked for <tstamp>, so this
            # was always empty and no footprint was ever linked to its symbol.
            path = (child.text or "").strip()
        elif tag == "property":
            name = (child.get("name") or "").strip()
            raw = (child.get("value") or "").strip()
            if not name:
                continue
            fields[name] = raw
            key = _norm(name)
            truth = _truthy(key, raw)
            if key in ("dnp", "ki_dnp"):
                dnp = dnp or truth
            elif key in ("exclude_from_board", "ki_exclude_from_board"):
                exclude_board = exclude_board or truth
            elif key in ("exclude_from_bom", "ki_exclude_from_bom"):
                exclude_bom = exclude_bom or truth
        elif tag == "fields":
            for f in child:
                name = (f.get("name") or "").strip()
                raw = (f.text or "").strip()
                if not name:
                    continue
                fields.setdefault(name, raw)
                key = _norm(name)
                if key in ("exclude_from_board", "ki_exclude_from_board"):
                    exclude_board = exclude_board or _truthy(key, raw)

    # A literal value of "DNP" is a widespread convention predating the flag.
    if value.strip().upper() == "DNP":
        dnp = True

    return SchComponent(
        ref=ref,
        value=value,
        footprint=footprint,
        lib_id=lib_id,
        sheetpath=_norm_sheetpath(sheetpath),
        path=_norm_path(path),
        dnp=dnp,
        exclude_from_board=exclude_board,
        exclude_from_bom=exclude_bom,
        fields=fields,
    )


def _norm(name: str) -> str:
    return name.lower().replace(" ", "_").replace("-", "_")


def _truthy(key: str, raw: str) -> bool:
    """Boolean property value, honouring KiCad's "present but empty means true"."""
    if raw == "":
        return key in BOOLEAN_PROPS
    return raw.lower() in TRUTHY


def _norm_sheetpath(sheet: str) -> str:
    """Normalise to a leading and trailing slash: ``/Power/``, root is ``/``."""
    s = sheet.strip().replace("\\", "/")
    if not s.startswith("/"):
        s = "/" + s
    if not s.endswith("/"):
        s += "/"
    return s


def _norm_path(path: str) -> str:
    """Normalise a KIID path to a single leading slash and no trailing slash."""
    p = path.strip()
    if not p:
        return ""
    return "/" + p.strip("/")


def iter_nets(path: Path) -> Iterator[tuple[str, list[tuple[str, str]]]]:
    """
    Stream ``(net_name, [(ref, pad), ...])`` from a netlist.

    Streaming matters: a large design's ``<nets>`` section dwarfs everything
    else, and the update pipeline only needs one net at a time.
    """
    etree, _is_lxml = _etree()
    try:
        # Both libraries accept a str path; v12 passed a Path to lxml's
        # iterparse and a str to the stdlib's, and the resulting TypeError was
        # not an ImportError so its `except ImportError` never caught it.
        context = etree.iterparse(str(path), events=("end",))
    except Exception as exc:
        raise NetlistError(f"{path.name}: {exc}") from exc

    for _event, elem in context:
        if elem.tag != "net":
            continue
        name = elem.get("name") or ""
        if name:
            nodes = [(n.get("ref") or "", n.get("pin") or "") for n in elem.iter("node") if n.get("ref")]
            yield name, nodes
        elem.clear()


def sheet_paths(components: dict[str, SchComponent]) -> list[str]:
    """Distinct sheet paths present, shallowest first. Drives rule suggestions."""
    seen = {c.sheetpath for c in components.values()}
    return sorted(seen, key=lambda s: (s.count("/"), s))


def top_level_sheets(components: dict[str, SchComponent]) -> list[str]:
    """
    Distinct first-level sheet names, e.g. ``["Power", "IO"]``.

    Onboarding uses this for "create one board per top-level sheet", which is by
    far the most common multi-board structure and turns setup into one click.
    """
    names = []
    for sheet in sheet_paths(components):
        parts = [p for p in sheet.split("/") if p]
        if parts and parts[0] not in names:
            names.append(parts[0])
    return names
