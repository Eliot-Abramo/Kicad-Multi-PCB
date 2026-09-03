# Copyright (c) 2026 Eliot Abramo
# SPDX-License-Identifier: MIT

"""
Performance characteristics that the UI depends on.

These are not micro-benchmarks. Each one pins a *complexity* claim that, when it
broke, produced a frozen editor rather than a slow one -- so they are written as
scaling ratios and generous ceilings rather than wall-clock thresholds, and they
pass on a slow CI runner while still failing the moment an accidental quadratic
comes back.

The one that matters most: a ``net:`` query used to scan every net and every
node once per component. On a ten-thousand-component design that measured
**11.5 seconds**, on KiCad's GUI thread, once per keystroke.
"""

import time

import pytest

from multiboard.core.config import BoardConfig, ProjectConfig
from multiboard.core.index import ComponentIndex, ComponentRecord, Placement, Status
from multiboard.core.netlist import SchComponent

BOARDS = ("Power", "Control", "IO")


def build_index(tmp_path, count: int) -> ComponentIndex:
    """An index of ``count`` components spread over three boards and many nets."""
    cfg = ProjectConfig()
    for name in BOARDS:
        cfg.boards[name] = BoardConfig(name=name, pcb_path=f"boards/{name}/{name}.kicad_pcb")

    index = ComponentIndex(tmp_path, cfg)
    records, nets, by_ref = {}, {}, {}
    for i in range(count):
        ref = f"R{i}"
        board = BOARDS[i % len(BOARDS)]
        records[ref] = ComponentRecord(
            ref=ref,
            sch=SchComponent(
                ref=ref, value="10k", footprint="Resistor_SMD:R_0402_1005Metric", sheetpath="/Power/"
            ),
            intent=board,
            placements=[
                Placement(
                    board=board,
                    x_mm=1.0,
                    y_mm=2.0,
                    rot_deg=0.0,
                    side="front",
                    fpid="Resistor_SMD:R_0402_1005Metric",
                )
            ],
            status=Status.OK,
        )
        signal = f"N{i % 800}"
        nets.setdefault(signal, []).append((board, ref, "1"))
        nets.setdefault("GND", []).append((board, ref, "2"))
        by_ref[ref] = frozenset({signal.lower(), "gnd"})

    index._install(records)
    index._nets, index._nets_by_ref = nets, by_ref
    return index


def elapsed(fn, reps: int = 3) -> float:
    """Best-of-``reps`` seconds, so a scheduling hiccup cannot fail the build."""
    fn()
    return min(_timed(fn) for _ in range(reps))


def _timed(fn) -> float:
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


# =============================================================================
# Search
# =============================================================================


def test_net_filter_is_not_quadratic(tmp_path):
    """
    Doubling the design must not quadruple the query.

    The reverse net index (``_nets_by_ref``) makes this linear. Reconstructing it
    per record -- which is what the forward map forced -- made it quadratic in
    components and linear again in nodes on top.
    """
    small = build_index(tmp_path, 2000)
    large = build_index(tmp_path, 4000)

    small_t = elapsed(lambda: small.search("net:GND"))
    large_t = elapsed(lambda: large.search("net:GND"))

    assert large_t < small_t * 3 + 0.05, (
        f"net: filter scaled {large_t / max(small_t, 1e-9):.1f}x for 2x the components "
        "-- the reverse net index is not being used"
    )


@pytest.mark.parametrize(
    "query",
    ["R42", "", "net:GND", "status:ok", "board:Power", "10k", "net:GND status:ok side:front"],
)
def test_every_query_shape_is_interactive(tmp_path, query):
    """No query may cost enough to be felt as a pause while typing."""
    index = build_index(tmp_path, 10_000)
    assert elapsed(lambda: index.search(query)) < 1.0


def test_records_sort_is_memoised(tmp_path):
    """
    One refresh_view() asks for records four times over; it must sort once.

    Not a timing assertion -- the same list object must come back.
    """
    index = build_index(tmp_path, 1000)
    assert index.records() is index.records()

    index._install(dict(index._records))
    assert index.records() is index.records(), "the memo must be rebuilt, then stable"


def test_search_orders_by_reference_naturally(tmp_path):
    """The heap selection must preserve what the full sort used to produce."""
    index = build_index(tmp_path, 1000)
    refs = [hit.record.ref for hit in index.search("status:ok", limit=12)]
    assert refs[:12] == [f"R{i}" for i in range(12)]


def test_exact_reference_outranks_its_own_prefixes(tmp_path):
    index = build_index(tmp_path, 1000)
    assert index.search("R42")[0].record.ref == "R42"


# =============================================================================
# Board scanning
# =============================================================================


def realistic_board(path, footprints: int) -> None:
    """A board shaped like one KiCad 10 writes: graphics, effects, a 3D model."""
    body = ['(kicad_pcb (version 20260206) (generator "pcbnew")']
    for i in range(footprints):
        body.append(f"""  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu") (uuid "u{i}") (at {i % 300}.5 {i // 300}.25 0)
    (descr "Resistor SMD 0402") (tags "resistor") (path "/p{i}") (attr smd)
    (property "Reference" "R{i}" (at 0 -1 0) (layer "F.SilkS")
      (effects (font (size 0.7 0.7) (thickness 0.12))))
    (property "Value" "10k" (at 0 1 0) (layer "F.Fab")
      (effects (font (size 0.7 0.7) (thickness 0.12))))
    (fp_line (start -0.9 -0.4) (end 0.9 -0.4) (stroke (width 0.05) (type solid)) (layer "F.CrtYd"))
    (fp_poly (pts (xy -0.5 -0.2) (xy 0.5 -0.2) (xy 0.5 0.2)) (stroke (width 0.1) (type solid))
      (fill yes) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.48 0) (size 0.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net {i} "N{i % 800}") (pintype "passive"))
    (pad "2" smd roundrect (at 0.48 0) (size 0.5 0.6) (layers "F.Cu" "F.Paste" "F.Mask")
      (roundrect_rratio 0.25) (net 1 "GND") (pintype "passive"))
    (model "${{KICAD10_3DMODEL_DIR}}/R.wrl" (offset (xyz 0 0 0)) (scale (xyz 1 1 1)))
  )""")
    body.append(")")
    path.write_text("\n".join(body), encoding="utf-8")


def test_scan_reports_progress_so_the_caller_can_stay_alive(tmp_path):
    """
    A large board takes seconds; the caller must be able to pump its event loop.

    Without this the progress dialog stalls on the one step that takes the time.
    """
    from multiboard.core.pcb_scan import SCAN_YIELD_EVERY, scan_pcb_file

    board = tmp_path / "big.kicad_pcb"
    realistic_board(board, 1200)

    ticks = []
    scan = scan_pcb_file(board, on_progress=lambda done, total: ticks.append((done, total)))

    assert len(scan.footprints) == 1200
    assert len(ticks) >= 1200 // SCAN_YIELD_EVERY
    assert all(total == 1200 for _done, total in ticks)


def test_skipping_unread_tags_does_not_change_the_result(tmp_path):
    """
    The keep-filter is a speed change and must be nothing else.

    Every field the scanner exposes has to survive not parsing the 3D models,
    graphics and font settings that surround it.
    """
    from multiboard.core import sexpr
    from multiboard.core.pcb_scan import FOOTPRINT_TAGS, _parse_footprint

    board = tmp_path / "b.kicad_pcb"
    realistic_board(board, 40)
    text = board.read_text(encoding="utf-8")

    spans = list(sexpr.iter_spans(text, "footprint"))
    everything = [_parse_footprint(sexpr.parse_span(text, s, e)) for s, e in spans]
    filtered = [_parse_footprint(sexpr.parse_span(text, s, e, keep=FOOTPRINT_TAGS)) for s, e in spans]

    assert everything == filtered


def test_footprint_tags_covers_everything_the_parser_reads(tmp_path):
    """
    A guard against the trap this optimisation sets.

    Reading a new tag in ``_parse_footprint`` without adding it to
    ``FOOTPRINT_TAGS`` yields an empty field rather than an error -- silent, and
    only visible as missing data. This fails loudly instead.
    """
    import inspect
    import re

    from multiboard.core import pcb_scan

    source = inspect.getsource(pcb_scan._parse_footprint) + inspect.getsource(pcb_scan._read_ref_value)
    looked_up = set(re.findall(r'sexpr\.find(?:_all)?\([^,]+,\s*"([^"]+)"', source))
    assert looked_up, "the scraper found no lookups; it has drifted from the code it guards"

    missing = sorted(looked_up - pcb_scan.FOOTPRINT_TAGS)
    assert not missing, (
        f"_parse_footprint reads {missing} but FOOTPRINT_TAGS does not keep those nodes, "
        "so they are skipped during the scan and always come back empty"
    )
