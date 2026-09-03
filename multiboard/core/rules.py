"""
Rule-based component-to-board assignment.

The point of rules is that assignment should be something you *declare once*,
not something you maintain per component. A design organised into hierarchical
sheets -- which is how most multi-board projects are already drawn -- needs one
rule per board, and every part added later inherits the right home automatically.

Rules produce *intent* only. Nothing here touches a PCB or a schematic.
"""

import fnmatch
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

from .config import AssignRule
from .netlist import SchComponent

_REF_SPLIT = re.compile(r"^([^\d]*)(\d*)$")

REGEX_CACHE_SIZE = 256
"""
How many compiled rule patterns to keep.

Bounded deliberately. The rules editor previews matches as you type, so this is
fed one entry per keystroke -- an unbounded dict grew a compiled pattern for
every prefix of every regex anyone ever typed, and never released one. 256 is
far more than the number of rules a project has, so the cache still never misses
in normal use.
"""


@dataclass(frozen=True)
class RuleMatch:
    """Which rule claimed a component, and how to explain that to the user."""

    index: int
    rule: AssignRule

    def detail(self) -> str:
        return f"rule {self.index + 1}: {self.rule.label().lower()} {self.rule.pattern}"


def split_ref(ref: str) -> tuple[str, Optional[int]]:
    """
    ``"R42"`` -> ``("R", 42)``; ``"TP"`` -> ``("TP", None)``.

    The numeric split is what makes ranges correct. Comparing refs as strings
    puts ``R9`` outside ``R1-R10``, which is the opposite of what anyone means.
    """
    m = _REF_SPLIT.match(ref.strip())
    if not m:
        return ref, None
    prefix, digits = m.group(1), m.group(2)
    return prefix, int(digits) if digits else None


def parse_refrange(spec: str) -> list[tuple[str, Optional[int], Optional[int]]]:
    """
    Parse ``"R100-R199, U1, C10-C19"`` into ``(prefix, low, high)`` terms.

    A bare prefix with no digits (``"J"``) matches every ref with that prefix.
    Malformed terms are skipped rather than raising, so a half-typed rule in the
    editor never breaks the index.
    """
    terms: list[tuple[str, Optional[int], Optional[int]]] = []
    for chunk in re.split(r"[,\s]+", spec.strip()):
        if not chunk:
            continue
        if "-" in chunk[1:]:
            lo_s, _, hi_s = chunk.partition("-")
            lo_p, lo_n = split_ref(lo_s)
            hi_p, hi_n = split_ref(hi_s)
            # "R100-199" is as natural to type as "R100-R199".
            if not hi_p:
                hi_p = lo_p
            if lo_p != hi_p or lo_n is None or hi_n is None:
                continue
            terms.append((lo_p, min(lo_n, hi_n), max(lo_n, hi_n)))
        else:
            prefix, num = split_ref(chunk)
            terms.append((prefix, num, num))
    return terms


@lru_cache(maxsize=REGEX_CACHE_SIZE)
def _compile(pattern: str) -> Optional["re.Pattern"]:
    """Compile and cache a regex; an invalid one is quarantined, not fatal."""
    try:
        return re.compile(pattern)
    except re.error:
        return None


def rule_error(rule: AssignRule) -> Optional[str]:
    """A message if the rule cannot work, for the editor to show inline."""
    if not rule.board:
        return "No target board"
    if not rule.pattern.strip():
        return "Empty pattern"
    if rule.kind == "regex" and _compile(rule.pattern) is None:
        return "Invalid regular expression"
    if rule.kind == "refrange" and not parse_refrange(rule.pattern):
        return "No valid reference terms"
    return None


def matches(rule: AssignRule, comp: SchComponent) -> bool:
    """Whether ``rule`` claims ``comp``."""
    if not rule.enabled or not rule.board or not rule.pattern:
        return False

    if rule.kind == "sheet":
        pattern = rule.pattern.strip()
        if not pattern.startswith("/"):
            pattern = "/" + pattern
        sheet = comp.sheetpath
        # "/Power/" should claim "/Power/" and everything under it, without the
        # user having to know to write a glob.
        if not any(ch in pattern for ch in "*?["):
            base = pattern if pattern.endswith("/") else pattern + "/"
            return sheet == base or sheet.startswith(base)
        return fnmatch.fnmatch(sheet, pattern) or fnmatch.fnmatch(sheet.rstrip("/"), pattern)

    if rule.kind == "refrange":
        prefix, num = split_ref(comp.ref)
        for term_prefix, lo, hi in parse_refrange(rule.pattern):
            if term_prefix != prefix:
                continue
            if lo is None:
                return True  # bare prefix: every ref with it
            if num is not None and lo <= num <= hi:
                return True
        return False

    if rule.kind == "regex":
        rx = _compile(rule.pattern)
        return bool(rx and rx.fullmatch(comp.ref))

    return False


def apply_rules(rules: list[AssignRule], comp: SchComponent) -> Optional[RuleMatch]:
    """First matching enabled rule, or None. Order is the user's priority."""
    for i, rule in enumerate(rules):
        if matches(rule, comp):
            return RuleMatch(index=i, rule=rule)
    return None


def preview(rules: list[AssignRule], components: dict[str, SchComponent], index: int) -> list[str]:
    """
    References the rule at ``index`` actually claims, accounting for priority.

    The rules editor shows this live. Accounting for priority matters: a rule
    that looks correct in isolation may claim nothing because an earlier rule
    already took everything, and that is invisible without this.
    """
    if index < 0 or index >= len(rules):
        return []
    out = []
    for ref in sorted(components, key=natural_key):
        match = apply_rules(rules, components[ref])
        if match and match.index == index:
            out.append(ref)
    return out


def match_counts(rules: list[AssignRule], components: dict[str, SchComponent]) -> list[int]:
    """How many components each rule claims, after priority. Shown per row."""
    counts = [0] * len(rules)
    for comp in components.values():
        match = apply_rules(rules, comp)
        if match:
            counts[match.index] += 1
    return counts


def suggest_from_sheets(components: dict[str, SchComponent], board_names: list[str]) -> list[AssignRule]:
    """
    Propose one sheet rule per top-level sheet that names an existing board.

    Matching is case-insensitive and ignores separators, so a ``/Power Supply/``
    sheet finds a ``Power_Supply`` board. This powers the "Suggest rules from
    sheets" button and the onboarding step -- for a hierarchically organised
    project it produces a complete, correct assignment in one click.
    """
    from .netlist import top_level_sheets

    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    by_norm = {norm(b): b for b in board_names}
    out: list[AssignRule] = []
    for sheet in top_level_sheets(components):
        board = by_norm.get(norm(sheet))
        if board:
            out.append(AssignRule(kind="sheet", pattern=f"/{sheet}/", board=board))
    return out


def unclaimed_sheets(components: dict[str, SchComponent], rules: list[AssignRule]) -> list[tuple[str, int]]:
    """
    ``(sheet, component_count)`` for sheets no rule covers, largest first.

    Surfaced in the rules editor so gaps in coverage are visible rather than
    something the user discovers when a board turns out to be missing parts.
    """
    counts: dict[str, int] = {}
    for comp in components.values():
        if not comp.placeable:
            continue
        if apply_rules(rules, comp) is None:
            counts[comp.sheetpath] = counts.get(comp.sheetpath, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def natural_key(ref: str):
    """Sort key putting ``R9`` before ``R10``."""
    prefix, num = split_ref(ref)
    return (prefix, num if num is not None else -1, ref)
