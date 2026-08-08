"""Rule matching, including the numeric-range case string comparison gets wrong."""

import pytest

from multiboard.core.config import AssignRule
from multiboard.core.netlist import SchComponent
from multiboard.core.rules import (
    apply_rules,
    match_counts,
    matches,
    natural_key,
    parse_refrange,
    preview,
    rule_error,
    split_ref,
    suggest_from_sheets,
    unclaimed_sheets,
)


def comp(ref="R1", sheet="/", **kw):
    return SchComponent(ref=ref, sheetpath=sheet, footprint="L:F", **kw)


# =============================================================================
# Sheet rules -- the primary mechanism
# =============================================================================


def test_sheet_rule_claims_the_sheet_and_its_children():
    rule = AssignRule("sheet", "/Power/", "Power")
    assert matches(rule, comp(sheet="/Power/"))
    assert matches(rule, comp(sheet="/Power/Regulators/"))
    assert not matches(rule, comp(sheet="/IO/"))


def test_sheet_rule_does_not_claim_a_prefix_sibling():
    """'/Power/' must not swallow '/PowerMonitor/'."""
    rule = AssignRule("sheet", "/Power/", "Power")
    assert not matches(rule, comp(sheet="/PowerMonitor/"))


def test_sheet_rule_tolerates_a_missing_leading_slash():
    assert matches(AssignRule("sheet", "Power/", "Power"), comp(sheet="/Power/"))


def test_sheet_rule_supports_globs():
    rule = AssignRule("sheet", "/*/Regulators/", "Power")
    assert matches(rule, comp(sheet="/Power/Regulators/"))
    assert not matches(rule, comp(sheet="/Power/Filters/"))


# =============================================================================
# Reference ranges -- numeric, not lexical
# =============================================================================


def test_r9_is_inside_r1_to_r10():
    """Lexically 'R9' > 'R10'; the numeric split is what makes this right."""
    rule = AssignRule("refrange", "R1-R10", "A")
    assert matches(rule, comp("R9"))
    assert matches(rule, comp("R10"))
    assert not matches(rule, comp("R11"))


def test_range_does_not_cross_prefixes():
    rule = AssignRule("refrange", "R1-R10", "A")
    assert not matches(rule, comp("C5"))


def test_multiple_terms():
    rule = AssignRule("refrange", "R100-R199, U1, C10-C19", "A")
    for ref in ("R100", "R199", "U1", "C15"):
        assert matches(rule, comp(ref)), ref
    for ref in ("R99", "R200", "U2", "C20"):
        assert not matches(rule, comp(ref)), ref


def test_bare_prefix_claims_every_matching_reference():
    rule = AssignRule("refrange", "J", "IO")
    assert matches(rule, comp("J1"))
    assert matches(rule, comp("J47"))
    assert not matches(rule, comp("R1"))


def test_shorthand_range_without_repeated_prefix():
    assert parse_refrange("R100-199") == [("R", 100, 199)]


def test_reversed_range_is_normalised():
    assert parse_refrange("R199-R100") == [("R", 100, 199)]


@pytest.mark.parametrize("ref,expected", [("R42", ("R", 42)), ("TP", ("TP", None)), ("", ("", None))])
def test_split_ref(ref, expected):
    assert split_ref(ref) == expected


# =============================================================================
# Regex
# =============================================================================


def test_regex_must_match_the_whole_reference():
    rule = AssignRule("regex", r"TP\d+", "Test")
    assert matches(rule, comp("TP1"))
    assert not matches(rule, comp("XTP1"))


def test_invalid_regex_is_quarantined_not_fatal():
    rule = AssignRule("regex", "R[", "A")
    assert matches(rule, comp("R1")) is False
    assert rule_error(rule) == "Invalid regular expression"


def test_rule_error_flags_missing_pieces():
    assert rule_error(AssignRule("sheet", "", "A")) == "Empty pattern"
    assert rule_error(AssignRule("sheet", "/x/", "")) == "No target board"
    assert rule_error(AssignRule("sheet", "/x/", "A")) is None


# =============================================================================
# Priority
# =============================================================================


def test_first_matching_rule_wins():
    rules = [AssignRule("sheet", "/Power/", "A"), AssignRule("sheet", "/Power/", "B")]
    assert apply_rules(rules, comp(sheet="/Power/")).rule.board == "A"


def test_disabled_rules_are_skipped():
    rules = [
        AssignRule("sheet", "/Power/", "A", enabled=False),
        AssignRule("sheet", "/Power/", "B"),
    ]
    assert apply_rules(rules, comp(sheet="/Power/")).rule.board == "B"


def test_preview_accounts_for_priority():
    """A rule shadowed by an earlier one claims nothing, and must show that."""
    rules = [AssignRule("refrange", "R1-R100", "A"), AssignRule("regex", r"R\d+", "B")]
    comps = {f"R{n}": comp(f"R{n}") for n in (1, 50, 200)}
    assert preview(rules, comps, 0) == ["R1", "R50"]
    assert preview(rules, comps, 1) == ["R200"]


def test_match_counts_are_per_rule_after_priority():
    rules = [AssignRule("refrange", "R1-R100", "A"), AssignRule("regex", r"R\d+", "B")]
    comps = {f"R{n}": comp(f"R{n}") for n in (1, 50, 200)}
    assert match_counts(rules, comps) == [2, 1]


# =============================================================================
# Suggestions
# =============================================================================


def test_suggests_one_rule_per_top_level_sheet_that_names_a_board():
    comps = {
        "R1": comp("R1", "/Power/"),
        "R2": comp("R2", "/IO/"),
        "R3": comp("R3", "/Unmatched/"),
    }
    suggested = suggest_from_sheets(comps, ["Power", "IO"])
    assert {(r.pattern, r.board) for r in suggested} == {("/Power/", "Power"), ("/IO/", "IO")}


def test_suggestion_matching_ignores_case_and_separators():
    comps = {"R1": comp("R1", "/Power Supply/")}
    assert suggest_from_sheets(comps, ["power_supply"])[0].board == "power_supply"


def test_unclaimed_sheets_surface_coverage_gaps():
    comps = {
        "R1": comp("R1", "/Power/"),
        "R2": comp("R2", "/IO/"),
        "R3": comp("R3", "/IO/"),
    }
    gaps = unclaimed_sheets(comps, [AssignRule("sheet", "/Power/", "Power")])
    assert gaps == [("/IO/", 2)]


def test_unplaceable_components_are_not_counted_as_gaps():
    comps = {"R1": SchComponent(ref="R1", sheetpath="/IO/", footprint="", dnp=True)}
    assert unclaimed_sheets(comps, []) == []


# =============================================================================
# Sorting
# =============================================================================


def test_natural_key_orders_r9_before_r10():
    assert sorted(["R10", "R9", "R1", "C2"], key=natural_key) == ["C2", "R1", "R9", "R10"]
