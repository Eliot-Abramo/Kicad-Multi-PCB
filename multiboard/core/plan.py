"""
Update planning -- what an Update would do, computed before anything is written.

v12's Update was a black box: you pressed it and found out afterwards what had
changed, from a summary count. There was no way to see that it was about to add
400 parts to the wrong board, and no way to stop it half-way.

Planning is pure. It runs entirely off the index and the netlist, so it needs no
pcbnew, completes instantly, and is identical whether it is driving the dialog
or ``cli sync --dry-run``.
"""

from dataclasses import dataclass, field
from typing import Optional

from .index import ComponentIndex, ComponentRecord, Status
from .netlist import SchComponent

ADD = "add"
UPDATE = "update"
REPLACE = "replace"
REMOVE = "remove"
SKIP = "skip"

ACTION_LABELS = {
    ADD: "Add",
    UPDATE: "Update",
    REPLACE: "Replace footprint",
    REMOVE: "Remove",
    SKIP: "Skip",
}

ACTION_ORDER = (ADD, REPLACE, UPDATE, REMOVE, SKIP)


@dataclass
class PlanItem:
    """One component's fate in a planned update."""

    ref: str
    action: str
    reason: str = ""
    before: str = ""
    after: str = ""
    enabled: bool = True
    footprint: str = ""
    value: str = ""

    @property
    def label(self) -> str:
        return ACTION_LABELS.get(self.action, self.action)

    @property
    def destructive(self) -> bool:
        """Actions that lose work if wrong, and so default to unchecked."""
        return self.action in (REMOVE, REPLACE)


@dataclass
class UpdatePlan:
    """Everything an Update on one board would do."""

    board: str
    items: list[PlanItem] = field(default_factory=list)
    conflicts: list[ComponentRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out = dict.fromkeys(ACTION_ORDER, 0)
        for item in self.items:
            out[item.action] = out.get(item.action, 0) + 1
        return out

    def enabled_items(self, action: Optional[str] = None) -> list[PlanItem]:
        return [i for i in self.items if i.enabled and (action is None or i.action == action)]

    def is_noop(self) -> bool:
        return not any(i.action != SKIP for i in self.items)

    def summary(self) -> str:
        counts = self.counts()
        parts = [f"{ACTION_LABELS[a]} {counts[a]}" for a in ACTION_ORDER if counts.get(a)]
        return ", ".join(parts) if parts else "Nothing to do"


def plan_update(
    index: ComponentIndex,
    board: str,
    *,
    include_unassigned: bool = True,
) -> UpdatePlan:
    """
    Work out what an Update on ``board`` would change.

    ``include_unassigned`` reproduces v12's behaviour of pulling in any component
    that is not already on another board. With rules configured, turning it off
    makes Update strictly obey intent -- which is what you want once assignment
    is deliberate, and is offered as a checkbox in the dialog.
    """
    plan = UpdatePlan(board=board)
    sch = index.schematic

    if not sch:
        plan.warnings.append("No schematic data. Run a refresh so the netlist can be exported first.")

    on_board: dict[str, ComponentRecord] = {}
    for rec in index.records():
        if board in rec.boards:
            on_board[rec.ref] = rec

    for ref in sorted(sch, key=_natural):
        comp = sch[ref]
        rec = index.get(ref)
        _plan_one(plan, board, comp, rec, on_board, include_unassigned)

    # Anything on this board that the schematic no longer knows about.
    for ref, rec in sorted(on_board.items(), key=lambda kv: _natural(kv[0])):
        if ref in sch:
            continue
        placement = next((p for p in rec.placements if p.board == board), None)
        plan.items.append(
            PlanItem(
                ref=ref,
                action=REMOVE,
                reason="Not present in the schematic",
                before=placement.fpid if placement else "",
                enabled=False,  # destructive: opt in, never opt out
                footprint=placement.fpid if placement else "",
            )
        )

    plan.conflicts = [r for r in index.conflicts() if board in r.boards or r.intent == board]
    return plan


def _plan_one(
    plan: UpdatePlan,
    board: str,
    comp: SchComponent,
    rec: Optional[ComponentRecord],
    on_board: dict[str, ComponentRecord],
    include_unassigned: bool,
) -> None:
    ref = comp.ref
    here = ref in on_board

    if not comp.placeable:
        if here:
            plan.items.append(
                PlanItem(
                    ref,
                    REMOVE,
                    f"{comp.skip_reason()} but still on this board",
                    before=comp.footprint,
                    enabled=False,
                )
            )
        else:
            plan.items.append(PlanItem(ref, SKIP, comp.skip_reason(), value=comp.value))
        return

    intent = rec.intent if rec else None
    elsewhere = [b for b in (rec.boards if rec else []) if b != board]

    if here:
        placement = next(p for p in on_board[ref].placements if p.board == board)
        if placement.fpid and comp.footprint and placement.fpid != comp.footprint:
            plan.items.append(
                PlanItem(
                    ref,
                    REPLACE,
                    "Footprint changed in the schematic",
                    before=placement.fpid,
                    after=comp.footprint,
                    enabled=False,  # replacing loses the existing placement's identity
                    footprint=comp.footprint,
                    value=comp.value,
                )
            )
        else:
            plan.items.append(
                PlanItem(
                    ref,
                    UPDATE,
                    "Refresh value and net assignment",
                    before=placement.fpid,
                    after=comp.footprint,
                    footprint=comp.footprint,
                    value=comp.value,
                )
            )
        return

    if elsewhere:
        plan.items.append(PlanItem(ref, SKIP, f"Placed on {', '.join(elsewhere)}", value=comp.value))
        return

    if intent == board:
        plan.items.append(
            PlanItem(ref, ADD, _why(rec), after=comp.footprint, footprint=comp.footprint, value=comp.value)
        )
        return

    if intent is None and include_unassigned:
        plan.items.append(
            PlanItem(
                ref,
                ADD,
                "Unassigned and not placed anywhere",
                after=comp.footprint,
                footprint=comp.footprint,
                value=comp.value,
            )
        )
        return

    if intent is None:
        plan.items.append(PlanItem(ref, SKIP, "Not assigned to any board", value=comp.value))
    else:
        plan.items.append(PlanItem(ref, SKIP, f"Assigned to {intent}", value=comp.value))


def _why(rec: Optional[ComponentRecord]) -> str:
    if rec is None or not rec.why:
        return "Assigned to this board"
    return f"Assigned to this board ({rec.why})"


def _natural(ref: str):
    from .rules import natural_key

    return natural_key(ref)


def format_plan(plan: UpdatePlan) -> str:
    """Plain-text rendering for the CLI's ``--dry-run``."""
    lines = [f"Update plan for board '{plan.board}': {plan.summary()}", ""]

    for action in ACTION_ORDER:
        items = [i for i in plan.items if i.action == action]
        if not items:
            continue
        lines.append(f"{ACTION_LABELS[action]} ({len(items)}):")
        for item in items[:200]:
            detail = f" - {item.reason}" if item.reason else ""
            mark = " " if item.enabled else "-"
            lines.append(f"  {mark} {item.ref}{detail}")
        if len(items) > 200:
            lines.append(f"    ... and {len(items) - 200} more")
        lines.append("")

    if plan.conflicts:
        lines.append(f"Conflicts touching this board ({len(plan.conflicts)}):")
        for rec in plan.conflicts[:50]:
            lines.append(f"  ! {rec.ref}: {Status.LABELS.get(rec.status, rec.status)} - {rec.hint()}")
        lines.append("")

    for warning in plan.warnings:
        lines.append(f"Warning: {warning}")

    return "\n".join(lines).rstrip() + "\n"
