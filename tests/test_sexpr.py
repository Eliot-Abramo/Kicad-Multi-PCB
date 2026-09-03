# Copyright (c) 2026 Eliot Abramo
# SPDX-License-Identifier: MIT

"""Scanner tolerance: the shapes real KiCad files actually take."""

import pytest

from multiboard.core import sexpr


def test_iter_spans_finds_top_level_nodes():
    text = '(kicad_pcb (footprint "A" (at 1 2)) (footprint "B" (at 3 4)))'
    spans = list(sexpr.iter_spans(text, "footprint"))
    assert len(spans) == 2
    assert text[spans[0][0] : spans[0][1]] == '(footprint "A" (at 1 2))'


def test_nested_footprints_are_not_yielded_at_depth_one():
    text = '(kicad_pcb (group (footprint "nested")) (footprint "top"))'
    got = [sexpr.atom(sexpr.parse_span(text, s, e)) for s, e in sexpr.iter_spans(text, "footprint")]
    assert got == ["top"]


def test_quoted_string_containing_parens():
    text = '(kicad_pcb (footprint "X" (property "Value" "10k (1%)")))'
    node = sexpr.parse_span(text, *next(iter(sexpr.iter_spans(text, "footprint"))))
    prop = sexpr.find_all(node, "property")[0]
    assert sexpr.atom(prop, 1) == "10k (1%)"


def test_escaped_quote_in_value():
    text = r'(kicad_pcb (footprint "X" (property "Reference" "U\"1")))'
    node = sexpr.parse_span(text, *next(iter(sexpr.iter_spans(text, "footprint"))))
    assert sexpr.atom(sexpr.find_all(node, "property")[0], 1) == 'U"1'


def test_crlf_and_bom():
    text = '﻿(kicad_pcb\r\n  (footprint "A"\r\n    (at 1 2)))'
    text = sexpr.strip_preamble(text)
    assert len(list(sexpr.iter_spans(text, "footprint"))) == 1


def test_non_ascii_reference():
    text = '(kicad_pcb (footprint "X" (property "Reference" "Ω1")))'
    node = sexpr.parse_span(text, *next(iter(sexpr.iter_spans(text, "footprint"))))
    assert sexpr.atom(sexpr.find_all(node, "property")[0], 1) == "Ω1"


def test_last_atom_handles_both_net_forms():
    """Board format 20251028 stopped serialising netcodes."""
    old = sexpr.parse('(net 3 "GND")')
    new = sexpr.parse('(net "GND")')
    assert sexpr.last_atom(old) == "GND"
    assert sexpr.last_atom(new) == "GND"


def test_scan_health_detects_truncation():
    balanced, depth = sexpr.scan_health('(kicad_pcb (footprint "A" (at 1 2')
    assert not balanced and depth > 0


def test_scan_health_detects_stray_close():
    """This is exactly the shape v12's block-footprint generator emitted."""
    v12 = (
        '(footprint "B" (fp_rect (start 0 0) (end 1 1)\n'
        '  (stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd"))\n'
        "  )\n)"
    )
    balanced, depth = sexpr.scan_health(v12)
    assert not balanced and depth < 0


def test_truncated_file_yields_what_it_has():
    text = '(kicad_pcb (footprint "A" (at 1 2)) (footprint "B" (at 3'
    spans = list(sexpr.iter_spans(text, "footprint"))
    assert len(spans) == 1  # the complete one; the truncated one is not yielded


@pytest.mark.parametrize(
    "value",
    ["plain", "has space", 'has "quote"', "has(paren)", "back\\slash", ""],
)
def test_quote_roundtrip(value):
    assert sexpr.unquote(sexpr.quote(value)) == value


def test_quote_leaves_simple_atoms_bare():
    assert sexpr.quote("F.Cu") == "F.Cu"
    assert sexpr.quote("a b") == '"a b"'
