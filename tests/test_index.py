"""
The three-layer ownership model.

One test per row of the reconciliation truth table, plus the cases v12 got
wrong: duplicate placements (silently overwritten), and per-board cache
invalidation (v12 threw the whole cache away on every keystroke).
"""

from pathlib import Path

import pytest
from conftest import make_netlist

from multiboard.core.config import AssignRule, BoardConfig, ProjectConfig
from multiboard.core.index import ComponentIndex, Origin, Status, classify
from multiboard.core.netlist import SchComponent

# =============================================================================
# classify(): the truth table
# =============================================================================


def _sch(ref="R1", **kw):
    return SchComponent(ref=ref, footprint=kw.pop("footprint", "L:F"), **kw)


def _place(board, ref="R1"):
    from multiboard.core.index import Placement

    return Placement(board=board, x_mm=0, y_mm=0, rot_deg=0, side="front", fpid="L:F")


def test_ok_when_intent_matches_single_placement():
    assert classify(_sch(), "Power", [_place("Power")]) == Status.OK


def test_misplaced_when_placed_on_a_different_board():
    assert classify(_sch(), "Power", [_place("IO")]) == Status.MISPLACED


def test_duplicate_outranks_everything():
    """v12 silently kept whichever board came last in dict order."""
    assert classify(_sch(), "Power", [_place("Power"), _place("IO")]) == Status.DUPLICATE


def test_adopt_when_placed_without_intent():
    assert classify(_sch(), None, [_place("Power")]) == Status.ADOPT


def test_todo_when_assigned_but_not_placed():
    assert classify(_sch(), "Power", []) == Status.TODO


def test_nowhere_when_neither_assigned_nor_placed():
    assert classify(_sch(), None, []) == Status.NOWHERE


def test_orphan_when_on_a_board_but_not_in_the_schematic():
    assert classify(None, None, [_place("Power")]) == Status.ORPHAN


@pytest.mark.parametrize(
    "kwargs",
    [{"dnp": True}, {"exclude_from_board": True}, {"footprint": ""}],
)
def test_skipped_for_unplaceable_components(kwargs):
    assert classify(_sch(**kwargs), "Power", []) == Status.SKIPPED


def test_orphan_beats_skipped_when_schematic_is_absent():
    assert classify(None, "Power", [_place("Power")]) == Status.ORPHAN


# =============================================================================
# Intent precedence
# =============================================================================


def _cfg(**kw):
    cfg = ProjectConfig(**kw)
    cfg.boards["Power"] = BoardConfig("Power", "boards/Power/Power.kicad_pcb")
    cfg.boards["IO"] = BoardConfig("IO", "boards/IO/IO.kicad_pcb")
    return cfg


def test_pin_beats_field_and_rule():
    from multiboard.core.index import resolve_intent

    cfg = _cfg(assignments={"R1": "Power"}, rules=[AssignRule("regex", "R1", "IO")])
    comp = _sch(fields={"MB_Board": "IO"})
    board, origin, detail = resolve_intent(comp, cfg, "R1")
    assert (board, origin) == ("Power", Origin.PIN)
    assert detail


def test_field_beats_rule():
    from multiboard.core.index import resolve_intent

    cfg = _cfg(rules=[AssignRule("regex", "R1", "IO")])
    comp = _sch(fields={"MB_Board": "Power"})
    board, origin, detail = resolve_intent(comp, cfg, "R1")
    assert (board, origin) == ("Power", Origin.FIELD)
    assert "MB_Board" in detail


def test_rule_applies_when_nothing_else_does():
    from multiboard.core.index import resolve_intent

    cfg = _cfg(rules=[AssignRule("sheet", "/Power/", "Power")])
    comp = _sch(sheetpath="/Power/")
    board, origin, detail = resolve_intent(comp, cfg, "R1")
    assert (board, origin) == ("Power", Origin.RULE)
    assert "rule 1" in detail  # provenance is explainable in the UI


def test_intent_pointing_at_a_deleted_board_is_ignored():
    from multiboard.core.index import resolve_intent

    cfg = _cfg(assignments={"R1": "Ghost"})
    board, origin, _ = resolve_intent(_sch(), cfg, "R1")
    assert (board, origin) == (None, Origin.NONE)


# =============================================================================
# End to end over real files
# =============================================================================


@pytest.fixture
def indexed(project: Path, make_board):
    cfg = ProjectConfig(root_schematic="demo.kicad_sch")
    cfg.boards["Power"] = BoardConfig(
        "Power",
        make_board(
            "Power",
            [
                {
                    "ref": "R1",
                    "value": "10k",
                    "fpid": "R:0402",
                    "x": 10,
                    "y": 20,
                    "pads": [("1", 3, "GND"), ("2", "VCC")],
                },
                {"ref": "U1", "value": "REG", "fpid": "U:SOT23"},
            ],
        ),
    )
    cfg.boards["IO"] = BoardConfig(
        "IO",
        make_board(
            "IO",
            [
                {"ref": "J1", "value": "USB", "fpid": "J:USB", "layer": "B.Cu"},
                {"ref": "R9", "value": "1k", "fpid": "R:0402"},
            ],
        ),
    )

    netlist = project / "net.xml"
    netlist.write_text(
        make_netlist(
            [
                {
                    "ref": "R1",
                    "value": "10k",
                    "footprint": "R:0402",
                    "sheet": "/Power/",
                    "nets": {"1": "GND", "2": "VCC"},
                },
                {"ref": "U1", "value": "REG", "footprint": "U:SOT23", "sheet": "/Power/"},
                {"ref": "J1", "value": "USB", "footprint": "J:USB", "sheet": "/IO/"},
                {"ref": "R9", "value": "1k", "footprint": "R:0402", "sheet": "/IO/"},
                {"ref": "C7", "value": "100n", "footprint": "C:0402", "sheet": "/Power/"},
                {"ref": "R99", "value": "DNP", "footprint": "R:0402", "sheet": "/IO/"},
            ]
        ),
        encoding="utf-8",
    )

    idx = ComponentIndex(project, cfg)
    idx.refresh(netlist=netlist)
    return idx


def test_index_finds_every_component(indexed):
    assert {r.ref for r in indexed.records()} == {"R1", "U1", "J1", "R9", "C7", "R99"}


def test_placement_carries_position_and_side(indexed):
    r1 = indexed.get("R1")
    assert r1.placements[0].board == "Power"
    assert (r1.placements[0].x_mm, r1.placements[0].y_mm) == (10.0, 20.0)
    assert indexed.get("J1").placements[0].side == "back"


def test_lookup_is_case_insensitive(indexed):
    assert indexed.get("r1") is indexed.get("R1")


def test_unassigned_but_placed_is_adopt(indexed):
    assert indexed.get("R1").status == Status.ADOPT


def test_unplaced_and_unassigned_is_nowhere(indexed):
    assert indexed.get("C7").status == Status.NOWHERE


def test_dnp_by_value_is_skipped(indexed):
    assert indexed.get("R99").status == Status.SKIPPED


def test_rules_make_placed_components_ok(indexed):
    indexed.cfg.rules = [
        AssignRule("sheet", "/Power/", "Power"),
        AssignRule("sheet", "/IO/", "IO"),
    ]
    indexed.reclassify()
    assert indexed.get("R1").status == Status.OK
    assert indexed.get("J1").status == Status.OK
    assert indexed.get("C7").status == Status.TODO  # assigned, awaiting placement
    assert indexed.get("R1").why.startswith("rule 1")


def test_rule_pointing_elsewhere_reports_misplaced(indexed):
    indexed.cfg.rules = [AssignRule("sheet", "/Power/", "IO")]
    indexed.reclassify()
    assert indexed.get("R1").status == Status.MISPLACED
    assert indexed.get("R1").hint() == "Assigned to IO, placed on Power."


def test_duplicate_across_boards_is_reported_not_overwritten(project, make_board):
    """The defect this whole model exists to fix."""
    cfg = ProjectConfig(root_schematic="demo.kicad_sch")
    cfg.boards["A"] = BoardConfig("A", make_board("A", [{"ref": "R1", "fpid": "R:0402"}]))
    cfg.boards["B"] = BoardConfig("B", make_board("B", [{"ref": "R1", "fpid": "R:0402"}]))

    netlist = project / "n.xml"
    netlist.write_text(make_netlist([{"ref": "R1", "footprint": "R:0402"}]), encoding="utf-8")

    idx = ComponentIndex(project, cfg)
    idx.refresh(netlist=netlist)

    rec = idx.get("R1")
    assert rec.status == Status.DUPLICATE
    assert sorted(rec.boards) == ["A", "B"]
    assert idx.stats.conflicts == 1


def test_orphan_detected_when_schematic_drops_a_part(project, make_board):
    cfg = ProjectConfig(root_schematic="demo.kicad_sch")
    cfg.boards["A"] = BoardConfig("A", make_board("A", [{"ref": "R1", "fpid": "R:0402"}]))
    netlist = project / "n.xml"
    netlist.write_text(make_netlist([]), encoding="utf-8")

    idx = ComponentIndex(project, cfg)
    idx.refresh(netlist=netlist)
    assert idx.get("R1").status == Status.ORPHAN


def test_managed_footprints_are_not_components(project, make_board):
    """Generated block footprints must not appear as parts."""
    cfg = ProjectConfig(root_schematic="demo.kicad_sch")
    cfg.boards["A"] = BoardConfig(
        "A",
        make_board(
            "A",
            [
                {"ref": "R1", "fpid": "R:0402"},
                {"ref": "MB1", "fpid": "MultiBoard_Blocks:Block_IO", "attrs": ["board_only"]},
                {"ref": "#PWR01", "fpid": "power:GND"},
            ],
        ),
    )
    netlist = project / "n.xml"
    netlist.write_text(make_netlist([{"ref": "R1", "footprint": "R:0402"}]), encoding="utf-8")

    idx = ComponentIndex(project, cfg)
    idx.refresh(netlist=netlist)
    assert {r.ref for r in idx.records()} == {"R1"}


# =============================================================================
# Nets
# =============================================================================


def test_net_index_spans_boards_and_both_netcode_forms(indexed):
    gnd = indexed.net("GND")
    assert ("Power", "R1", "1") in gnd
    assert "VCC" in indexed.net_names()


# =============================================================================
# Search
# =============================================================================


def test_exact_reference_outranks_prefix_matches(project, make_board):
    cfg = ProjectConfig(root_schematic="demo.kicad_sch")
    cfg.boards["A"] = BoardConfig(
        "A", make_board("A", [{"ref": f"R{n}", "fpid": "R:0402"} for n in (4, 40, 41, 42)])
    )
    netlist = project / "n.xml"
    netlist.write_text(
        make_netlist([{"ref": f"R{n}", "footprint": "R:0402"} for n in (4, 40, 41, 42)]), encoding="utf-8"
    )

    idx = ComponentIndex(project, cfg)
    idx.refresh(netlist=netlist)
    assert idx.search("R4")[0].record.ref == "R4"
    assert idx.search("r42")[0].record.ref == "R42"


def test_filter_by_board(indexed):
    refs = {h.record.ref for h in indexed.search("board:IO")}
    assert refs == {"J1", "R9"}


def test_filter_by_status(indexed):
    assert {h.record.ref for h in indexed.search("status:nowhere")} == {"C7"}


def test_filter_by_sheet_and_net(indexed):
    assert {h.record.ref for h in indexed.search("sheet:/Power/")} >= {"R1", "U1", "C7"}
    assert {h.record.ref for h in indexed.search("net:GND")} == {"R1"}


def test_filters_combine_with_free_text(indexed):
    assert {h.record.ref for h in indexed.search("board:Power 10k")} == {"R1"}


def test_filter_by_side(indexed):
    assert {h.record.ref for h in indexed.search("side:back")} == {"J1"}


# =============================================================================
# Caching
# =============================================================================


def test_second_refresh_uses_the_cache(indexed, project):
    stats = indexed.refresh(netlist=project / "net.xml")
    assert stats.boards_scanned == 0
    assert stats.boards_cached == 2


def test_touching_one_board_rescans_only_that_board(indexed, project):
    """v12 discarded the entire cache on every list refresh."""
    pcb = project / "boards" / "IO" / "IO.kicad_pcb"
    pcb.write_text(pcb.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    stats = indexed.refresh(netlist=project / "net.xml")
    assert stats.boards_scanned == 1
    assert stats.boards_cached == 1


def test_cache_survives_a_new_index_instance(indexed, project):
    fresh = ComponentIndex(project, indexed.cfg)
    stats = fresh.refresh(netlist=project / "net.xml")
    assert stats.boards_cached == 2
    assert fresh.get("R1") is not None


# =============================================================================
# Export
# =============================================================================


def test_csv_export_has_a_row_per_component(indexed):
    import io

    buf = io.StringIO()
    n = indexed.write_csv(buf)
    lines = buf.getvalue().strip().splitlines()
    assert n == 6
    assert len(lines) == 7  # header + rows
    assert lines[0].startswith("Reference,Value,Footprint")


def test_json_export_includes_placements(indexed):
    rows = {r["ref"]: r for r in indexed.to_json()}
    assert rows["R1"]["placements"][0]["board"] == "Power"
    assert rows["C7"]["placements"] == []


def test_board_counts_report_placed_and_pending(indexed):
    indexed.cfg.rules = [AssignRule("sheet", "/Power/", "Power")]
    indexed.reclassify()
    counts = indexed.board_counts()
    assert counts["Power"]["placed"] == 2
    assert counts["Power"]["pending"] == 1  # C7


# =============================================================================
# The reverse net index
#
# `net:` was O(records x nets x nodes) -- 11.5 seconds on a ten-thousand
# component design, per keystroke. See tests/test_perf.py for the scaling guard;
# these pin the behaviour.
# =============================================================================


def test_refresh_builds_the_reverse_net_index(project, make_board):
    from multiboard.core.config import BoardConfig, ProjectConfig
    from multiboard.core.index import ComponentIndex
    from multiboard.core.netlist import netlist_path

    rel = make_board("Power", [{"ref": "R1", "value": "10k", "pads": [("1", "GND"), ("2", "VCC")]}])
    cfg = ProjectConfig()
    cfg.boards["Power"] = BoardConfig(name="Power", pcb_path=rel)

    netlist = netlist_path(project)
    netlist.parent.mkdir(parents=True, exist_ok=True)
    netlist.write_text(make_netlist([{"ref": "R1", "value": "10k"}]), encoding="utf-8")

    index = ComponentIndex(project, cfg)
    index.refresh(netlist=netlist, force=True)

    assert index.nets_of("R1") == frozenset({"gnd", "vcc"})
    assert index.nets_of("R99") == frozenset()


def test_net_filter_is_case_insensitive(indexed):
    lower = {h.record.ref for h in indexed.search("net:gnd")}
    upper = {h.record.ref for h in indexed.search("net:GND")}
    assert lower and lower == upper


def test_lookup_falls_back_to_case_insensitive_match(indexed):
    any_ref = indexed.records()[0].ref
    assert indexed.get(any_ref.lower()) is indexed.get(any_ref)
    assert indexed.get("  " + any_ref.upper() + "  ") is indexed.get(any_ref)
