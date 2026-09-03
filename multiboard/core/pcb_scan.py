# Copyright (c) 2026 Eliot Abramo
# SPDX-License-Identifier: MIT

"""
Read a ``.kicad_pcb`` as text and extract what the component index needs.

This replaces ``pcbnew.LoadBoard`` for every read-only query. LoadBoard also
builds the connectivity engine and design-rule state we never use, and costs
1-3 s per board -- which is why v12's board list froze KiCad for seconds on
every keystroke in its filter box.

Only the fields below are extracted; see :data:`FOOTPRINT_TAGS` for why that
distinction is worth about a factor of two.

Format variation this must survive
----------------------------------
* Reference and value live in ``(property "Reference" "R42")`` on KiCad 7+, but
  in ``(fp_text reference "R42")`` on 6 and earlier. Sub-boards in a real
  project predate upgrades, so both appear.
* **Board format 20251028 stopped serialising netcodes.** A pad's net is
  ``(net 3 "GND")`` in older files and may be ``(net "GND")`` in KiCad 10 files.
  Reading index 1 silently empties the net index on new files; we take the last
  atom, which is correct for both.
* ``(uuid "...")`` on KiCad 8+, ``(tstamp "...")`` on 6 and 7.
* DNP and the exclusion flags are bare atoms inside ``(attr ...)`` on the PCB
  side, but XML attributes on the netlist side -- the two parsers deliberately
  share no code.
"""

from dataclasses import dataclass, field
from pathlib import Path

from ..constants import MANAGED_REF_PREFIX
from . import sexpr

# Board file format versions we have explicitly reasoned about. A file newer
# than the last entry still scans -- we just record a warning, because a silent
# wrong answer is far worse than a visible "written by a newer KiCad".
KNOWN_FORMATS = {
    20211014: "KiCad 6",
    20221018: "KiCad 7",
    20240108: "KiCad 8",
    20241229: "KiCad 9",
    20260206: "KiCad 10",
}
NEWEST_KNOWN_FORMAT = 20260206

FOOTPRINT_TAGS = frozenset(
    {"at", "layer", "uuid", "tstamp", "path", "attr", "locked", "property", "fp_text", "pad", "net"}
)
"""
Exactly the child tags :func:`_parse_footprint` reads. Everything else is
stepped over unparsed.

This is where the scan time goes. A real KiCad 10 footprint spends almost all of
its bytes on things this module has no opinion about -- ``model``, ``fp_line``,
``fp_poly``, ``fp_arc``, ``descr``, ``tags``, ``effects``, ``zone_connect`` --
and building tuple trees for them, once per footprint, once per board, once per
refresh, was the single largest cost in the whole index.

Add a tag here when, and only when, :func:`_parse_footprint` starts reading it.
"""


@dataclass(frozen=True)
class PcbFootprint:
    """One footprint as it exists on a board."""

    ref: str
    value: str
    fpid: str
    x_mm: float = 0.0
    y_mm: float = 0.0
    rot_deg: float = 0.0
    layer: str = "F.Cu"
    uuid: str = ""
    path: str = ""
    dnp: bool = False
    exclude_from_bom: bool = False
    exclude_from_pos: bool = False
    board_only: bool = False
    locked: bool = False
    pad_nets: tuple[tuple[str, str], ...] = ()

    @property
    def side(self) -> str:
        return "back" if self.layer.startswith("B.") else "front"

    @property
    def is_managed(self) -> bool:
        """A footprint this plugin generated (block/port markers), not a real part."""
        return self.board_only or self.ref.startswith(MANAGED_REF_PREFIX)

    @property
    def counts_for_ownership(self) -> bool:
        """Whether this footprint means "this component lives on this board"."""
        return bool(self.ref) and not self.ref.startswith("#") and not self.is_managed


@dataclass
class PcbScan:
    """Result of scanning one board file."""

    path: Path
    mtime_ns: int = 0
    size: int = 0
    format_version: int = 0
    generator: str = ""
    footprints: list[PcbFootprint] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    truncated: bool = False

    def owned(self) -> list[PcbFootprint]:
        return [fp for fp in self.footprints if fp.counts_for_ownership]

    # -- cache round-trip --------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "mtime_ns": self.mtime_ns,
            "size": self.size,
            "format_version": self.format_version,
            "generator": self.generator,
            "warnings": self.warnings,
            "truncated": self.truncated,
            "fps": [
                [
                    fp.ref,
                    fp.value,
                    fp.fpid,
                    fp.x_mm,
                    fp.y_mm,
                    fp.rot_deg,
                    fp.layer,
                    fp.uuid,
                    fp.path,
                    int(fp.dnp)
                    | (int(fp.exclude_from_bom) << 1)
                    | (int(fp.exclude_from_pos) << 2)
                    | (int(fp.board_only) << 3)
                    | (int(fp.locked) << 4),
                    [list(pn) for pn in fp.pad_nets],
                ]
                for fp in self.footprints
            ],
        }

    @classmethod
    def from_dict(cls, path: Path, data: dict) -> "PcbScan":
        scan = cls(
            path=path,
            mtime_ns=int(data.get("mtime_ns", 0)),
            size=int(data.get("size", 0)),
            format_version=int(data.get("format_version", 0)),
            generator=str(data.get("generator", "")),
            warnings=list(data.get("warnings", [])),
            truncated=bool(data.get("truncated", False)),
        )
        for row in data.get("fps", []):
            flags = int(row[9])
            scan.footprints.append(
                PcbFootprint(
                    ref=row[0],
                    value=row[1],
                    fpid=row[2],
                    x_mm=row[3],
                    y_mm=row[4],
                    rot_deg=row[5],
                    layer=row[6],
                    uuid=row[7],
                    path=row[8],
                    dnp=bool(flags & 1),
                    exclude_from_bom=bool(flags & 2),
                    exclude_from_pos=bool(flags & 4),
                    board_only=bool(flags & 8),
                    locked=bool(flags & 16),
                    pad_nets=tuple(tuple(pn) for pn in row[10]),
                )
            )
        return scan


SCAN_YIELD_EVERY = 250
"""
Footprints between ``on_progress`` calls during a scan.

Small enough that a caller pumping an event loop stays responsive on the largest
boards -- twenty updates across a five-thousand-footprint board -- and large
enough that the callback costs nothing measurable.
"""


def scan_pcb_file(path: Path, on_progress=None) -> PcbScan:
    """
    Scan a board file. Never raises for malformed content -- it reports.

    ``on_progress(done, total)`` is called every :data:`SCAN_YIELD_EVERY`
    footprints. A large board takes seconds, and the caller is on KiCad's GUI
    thread, so this is the hook that lets it keep the window alive instead of
    going dark until the scan finishes.
    """
    try:
        st = path.stat()
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        scan = PcbScan(path=path)
        scan.warnings.append(f"Could not read {path.name}: {exc}")
        return scan

    text = sexpr.strip_preamble(raw)
    scan = PcbScan(path=path, mtime_ns=st.st_mtime_ns, size=st.st_size)

    balanced, depth = sexpr.scan_health(text)
    if not balanced:
        scan.truncated = depth > 0
        scan.warnings.append(
            f"{path.name} has unbalanced parentheses (depth {depth}); results may be incomplete."
        )

    scan.format_version, scan.generator = _read_header(text)
    if scan.format_version and scan.format_version > NEWEST_KNOWN_FORMAT:
        scan.warnings.append(
            f"{path.name} uses board format {scan.format_version}, newer than this "
            f"plugin knows about ({NEWEST_KNOWN_FORMAT}). Some fields may be missed."
        )

    spans = list(sexpr.iter_spans(text, "footprint"))
    total = len(spans)
    for done, (start, end) in enumerate(spans):
        try:
            node = sexpr.parse_span(text, start, end, keep=FOOTPRINT_TAGS)
            scan.footprints.append(_parse_footprint(node))
        except (sexpr.SexprError, IndexError, ValueError) as exc:
            scan.warnings.append(f"{path.name}: skipped a malformed footprint ({exc})")
        if on_progress is not None and done % SCAN_YIELD_EVERY == 0:
            on_progress(done, total)

    return scan


def _read_header(text: str) -> tuple[int, str]:
    """``(version NNNNNNNN)`` and ``(generator ...)`` from the file head."""
    head = text[:4096]
    version, generator = 0, ""
    for tag, depth in (("version", 1), ("generator", 1)):
        for start, end in sexpr.iter_spans(head, tag, depth=depth):
            node = sexpr.parse_span(head, start, end)
            val = sexpr.atom(node)
            if tag == "version":
                try:
                    version = int(val)
                except ValueError:
                    pass
            else:
                generator = val
            break
    return version, generator


def _parse_footprint(node: sexpr.Node) -> PcbFootprint:
    fpid = sexpr.atom(node)

    at = sexpr.find(node, "at")
    x, y = sexpr.number(at, 0), sexpr.number(at, 1)
    rot = sexpr.number(at, 2)

    layer = sexpr.atom(sexpr.find(node, "layer"), default="F.Cu")

    # KiCad 8+ writes (uuid "..."); 6/7 wrote (tstamp "...").
    uuid = sexpr.atom(sexpr.find(node, "uuid")) or sexpr.atom(sexpr.find(node, "tstamp"))
    path = sexpr.atom(sexpr.find(node, "path"))

    ref, value = _read_ref_value(node)

    attr = sexpr.find(node, "attr")
    flags = set(sexpr.atoms(attr)) if attr else set()

    locked = "locked" in flags or bool(sexpr.find(node, "locked"))

    pad_nets: list[tuple[str, str]] = []
    for pad in sexpr.find_all(node, "pad"):
        number = sexpr.atom(pad)
        net = sexpr.find(pad, "net")
        if net is not None:
            # Last atom: correct for both (net 3 "GND") and (net "GND").
            name = sexpr.last_atom(net)
            if name:
                pad_nets.append((number, name))

    return PcbFootprint(
        ref=ref,
        value=value,
        fpid=fpid,
        x_mm=x,
        y_mm=y,
        rot_deg=rot,
        layer=layer,
        uuid=uuid,
        path=path,
        dnp="dnp" in flags,
        exclude_from_bom="exclude_from_bom" in flags,
        exclude_from_pos="exclude_from_pos_files" in flags,
        board_only="board_only" in flags,
        locked=locked,
        pad_nets=tuple(pad_nets),
    )


def _read_ref_value(node: sexpr.Node) -> tuple[str, str]:
    """Reference and value, from KiCad 7+ properties or the older fp_text form."""
    ref = value = ""

    for prop in sexpr.find_all(node, "property"):
        key = sexpr.atom(prop, 0)
        if key == "Reference":
            ref = sexpr.atom(prop, 1)
        elif key == "Value":
            value = sexpr.atom(prop, 1)

    if not ref or not value:
        for txt in sexpr.find_all(node, "fp_text"):
            kind = sexpr.atom(txt, 0)
            if kind == "reference" and not ref:
                ref = sexpr.atom(txt, 1)
            elif kind == "value" and not value:
                value = sexpr.atom(txt, 1)

    return ref, value


def nets_in_scan(scan: PcbScan) -> dict[str, list[tuple[str, str]]]:
    """``{net_name: [(ref, pad), ...]}`` for one board."""
    out: dict[str, list[tuple[str, str]]] = {}
    for fp in scan.footprints:
        if not fp.counts_for_ownership:
            continue
        for pad, net in fp.pad_nets:
            out.setdefault(net, []).append((fp.ref, pad))
    return out


def validate_footprint_library(lib_dir: Path) -> list[str]:
    """
    Problems in a generated ``.pretty``, as human-readable strings.

    This is what Doctor uses to detect the v12 block-footprint damage: its
    generator emitted two stray closing parens into every file it wrote, so
    every ``Block_*.kicad_mod`` in every project created with v12 is
    unparseable and the whole feature silently never worked.
    """
    problems: list[str] = []
    if not lib_dir.is_dir():
        return problems

    for mod in sorted(lib_dir.glob("*.kicad_mod")):
        try:
            text = sexpr.strip_preamble(mod.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            problems.append(f"{mod.name}: unreadable ({exc})")
            continue
        balanced, depth = sexpr.scan_health(text)
        if not balanced:
            kind = "truncated" if depth > 0 else f"{-depth} stray closing paren(s)"
            problems.append(f"{mod.name}: malformed ({kind})")
    return problems
