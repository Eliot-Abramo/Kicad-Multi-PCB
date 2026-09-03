"""
The assignment rules editor.

This is the guarantee that nobody has to open a JSON file to know -- or to
decide -- where a component lives. Rules are created, reordered, previewed, and
deleted here, with a live count of what each one claims and a list of the actual
references. A rule that looks right but is shadowed by an earlier one shows zero
matches, which is exactly the feedback that makes priority comprehensible.

"Suggest from sheets" is the fast path: a hierarchically organised schematic
gets a complete, correct assignment in one click.
"""

from typing import Callable, Optional

import wx

from ..core.config import RULE_KINDS, AssignRule
from ..core.netlist import SchComponent
from ..core.rules import match_counts, preview, rule_error, unclaimed_sheets
from .theme import apply_grid, apply_input, set_row_colors
from .widgets import (
    SPACING_MD,
    SPACING_SM,
    BaseDialog,
    ReadOnlyText,
    confirm,
    message,
)

KIND_LABELS = {
    "sheet": "Sheet path",
    "refrange": "Reference range",
    "regex": "Reference regex",
}

KIND_HELP = {
    "sheet": (
        "Matches a hierarchical sheet and everything under it.\n"
        "Example: /Power/  claims /Power/ and /Power/Regulators/.\n"
        "Wildcards work too: /*/Filters/"
    ),
    "refrange": (
        "Matches reference designators numerically.\n"
        "Example: R100-R199, U1, C10-C19\n"
        "A bare prefix like J claims every J.  R9 is inside R1-R10."
    ),
    "regex": (
        "A regular expression matched against the whole reference.\n"
        "Example: ^TP\\d+$ claims every test point."
    ),
}


class RuleEditDialog(BaseDialog):
    """Create or edit one rule."""

    def __init__(
        self, parent, rule: Optional[AssignRule], boards: list[str], components: dict[str, SchComponent]
    ):
        super().__init__(parent, "Edit rule" if rule else "New rule", size=(560, 460), min_size=(480, 400))
        self.components = components
        self.rule = AssignRule(
            kind=rule.kind if rule else "sheet",
            pattern=rule.pattern if rule else "",
            board=rule.board if rule else (boards[0] if boards else ""),
            enabled=rule.enabled if rule else True,
        )
        self._build(boards)
        self._update_preview()

    def _build(self, boards: list[str]) -> None:
        sizer = wx.BoxSizer(wx.VERTICAL)
        grid = wx.FlexGridSizer(3, 2, SPACING_SM, SPACING_MD)
        grid.AddGrowableCol(1, 1)

        grid.Add(self._label("Match by"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.kind = wx.Choice(self, choices=[KIND_LABELS[k] for k in RULE_KINDS])
        self.kind.SetSelection(RULE_KINDS.index(self.rule.kind))
        self.kind.Bind(wx.EVT_CHOICE, lambda e: self._on_kind())
        grid.Add(self.kind, 1, wx.EXPAND)

        grid.Add(self._label("Pattern"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.pattern = wx.TextCtrl(self, value=self.rule.pattern)
        self.pattern.Bind(wx.EVT_TEXT, lambda e: self._update_preview())
        apply_input(self.pattern)
        grid.Add(self.pattern, 1, wx.EXPAND)

        grid.Add(self._label("Assign to"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.board = wx.Choice(self, choices=boards)
        if self.rule.board in boards:
            self.board.SetSelection(boards.index(self.rule.board))
        elif boards:
            self.board.SetSelection(0)
        grid.Add(self.board, 1, wx.EXPAND)

        sizer.Add(grid, 0, wx.ALL | wx.EXPAND, SPACING_MD)

        self.help = wx.StaticText(self, label="")
        self.help.SetFont(self.theme.small_font())
        self.help.SetForegroundColour(self.theme.text_muted)
        sizer.Add(self.help, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, SPACING_MD)

        self.summary = wx.StaticText(self, label="")
        self.summary.SetFont(self.theme.title_font())
        sizer.Add(self.summary, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, SPACING_MD)

        self.matches = ReadOnlyText(self, mono=True)
        sizer.Add(self.matches, 1, wx.ALL | wx.EXPAND, SPACING_MD)

        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, SPACING_MD)
        self.SetSizer(sizer)

        self.FindWindowById(wx.ID_OK).Bind(wx.EVT_BUTTON, self._on_ok)
        self._on_kind()

    def _label(self, text: str) -> wx.StaticText:
        label = wx.StaticText(self, label=text)
        label.SetForegroundColour(self.theme.text)
        return label

    def _on_kind(self) -> None:
        kind = RULE_KINDS[self.kind.GetSelection()]
        self.help.SetLabel(KIND_HELP[kind])
        self.Layout()
        self._update_preview()

    def _current(self) -> AssignRule:
        return AssignRule(
            kind=RULE_KINDS[self.kind.GetSelection()],
            pattern=self.pattern.GetValue().strip(),
            board=self.board.GetStringSelection(),
        )

    def _update_preview(self) -> None:
        """Show what this rule claims, live, as the pattern is typed."""
        rule = self._current()
        error = rule_error(rule)
        if error:
            self.summary.SetLabel(error)
            self.summary.SetForegroundColour(self.theme.warning)
            self.matches.SetValue("")
            return

        refs = preview([rule], self.components, 0)
        self.summary.SetLabel(f"Claims {len(refs)} component(s)")
        self.summary.SetForegroundColour(self.theme.success if refs else self.theme.text_muted)
        self.matches.SetValue(
            "\n".join(refs[:500]) + (f"\n... and {len(refs) - 500} more" if len(refs) > 500 else "")
        )

    def _on_ok(self, _event) -> None:
        rule = self._current()
        error = rule_error(rule)
        if error:
            message(self, error, "This rule cannot be used", wx.ICON_WARNING)
            return
        self.rule = rule
        self.EndModal(wx.ID_OK)


class RulesDialog(BaseDialog):
    """The rule list, with ordering, live counts, and coverage gaps."""

    def __init__(
        self,
        parent,
        rules: list[AssignRule],
        boards: list[str],
        components: dict[str, SchComponent],
        suggest: Callable[[], list[AssignRule]],
    ):
        super().__init__(parent, "Assignment rules", size=(900, 620), min_size=(760, 520))
        self.rules = [AssignRule(r.kind, r.pattern, r.board, r.enabled) for r in rules]
        self.boards = boards
        self.components = components
        self._suggest = suggest
        self._build()
        self._refresh()

    def _build(self) -> None:
        import wx.grid as gridlib

        sizer = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(
            self,
            label=(
                "Rules decide which board each component belongs on, before you place "
                "anything.\nThe first matching rule wins, so order matters."
            ),
        )
        intro.SetForegroundColour(self.theme.text_muted)
        sizer.Add(intro, 0, wx.ALL, SPACING_MD)

        body = wx.BoxSizer(wx.HORIZONTAL)

        self.grid = gridlib.Grid(self)
        self.grid.CreateGrid(0, 5)
        for i, (label, width) in enumerate(
            [("On", 40), ("Match by", 130), ("Pattern", 220), ("Board", 130), ("Claims", 70)]
        ):
            self.grid.SetColLabelValue(i, label)
            self.grid.SetColSize(i, width)
        self.grid.SetRowLabelSize(0)
        self.grid.EnableEditing(False)
        self.grid.SetSelectionMode(gridlib.Grid.SelectRows)
        self.grid.Bind(gridlib.EVT_GRID_SELECT_CELL, lambda e: (self._on_select(), e.Skip()))
        self.grid.Bind(gridlib.EVT_GRID_CELL_LEFT_DCLICK, lambda e: self._on_edit())
        self.grid.Bind(gridlib.EVT_GRID_CELL_LEFT_CLICK, self._on_click)
        body.Add(self.grid, 3, wx.EXPAND | wx.RIGHT, SPACING_MD)

        side = wx.BoxSizer(wx.VERTICAL)
        self.detail_title = wx.StaticText(self, label="Matched components")
        self.detail_title.SetFont(self.theme.title_font())
        side.Add(self.detail_title, 0, wx.BOTTOM, SPACING_SM)
        self.detail = ReadOnlyText(self, mono=True)
        side.Add(self.detail, 1, wx.EXPAND)
        body.Add(side, 2, wx.EXPAND)

        sizer.Add(body, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, SPACING_MD)

        self.gaps = wx.StaticText(self, label="")
        self.gaps.SetFont(self.theme.small_font())
        sizer.Add(self.gaps, 0, wx.ALL | wx.EXPAND, SPACING_MD)

        tools = wx.BoxSizer(wx.HORIZONTAL)
        for label, handler, primary in (
            ("Add...", self._on_add, False),
            ("Edit...", self._on_edit, False),
            ("Remove", self._on_remove, False),
            ("Move up", lambda: self._move(-1), False),
            ("Move down", lambda: self._move(1), False),
            ("Suggest from sheets", self._on_suggest, True),
        ):
            button = wx.Button(self, label=label)
            button.Bind(wx.EVT_BUTTON, lambda e, h=handler: h())
            tools.Add(button, 0, wx.RIGHT, SPACING_SM)
            if primary:
                button.SetToolTip("Create one rule per top-level schematic sheet that matches a board name.")
            else:
                setattr(self, f"_btn_{label.split('.')[0].lower().replace(' ', '_')}", button)
        sizer.Add(tools, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, SPACING_MD)

        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, SPACING_MD)
        self.SetSizer(sizer)

        apply_grid(self.grid, self.theme)

    # -- rendering ---------------------------------------------------------

    def _refresh(self) -> None:
        counts = match_counts(self.rules, self.components)

        if self.grid.GetNumberRows():
            self.grid.DeleteRows(0, self.grid.GetNumberRows())
        if self.rules:
            self.grid.AppendRows(len(self.rules))

        for row, rule in enumerate(self.rules):
            error = rule_error(rule)
            self.grid.SetCellValue(row, 0, "on" if rule.enabled else "off")
            self.grid.SetCellValue(row, 1, KIND_LABELS.get(rule.kind, rule.kind))
            self.grid.SetCellValue(row, 2, rule.pattern)
            self.grid.SetCellValue(row, 3, rule.board)
            self.grid.SetCellValue(row, 4, "-" if error else str(counts[row]))

            if error:
                bg = self.theme.tint(self.theme.error, 0.18)
                fg = self.theme.readable(self.theme.text, bg)
            elif not rule.enabled:
                bg, fg = self.theme.window_bg, self.theme.text_muted
            elif counts[row] == 0:
                bg = self.theme.tint(self.theme.warning, 0.14)
                fg = self.theme.readable(self.theme.text, bg)
            else:
                bg, fg = self.theme.window_bg, self.theme.text
            set_row_colors(self.grid, row, 5, bg, fg)

        self._refresh_gaps()
        self._on_select()

    def _refresh_gaps(self) -> None:
        gaps = unclaimed_sheets(self.components, self.rules)
        if not gaps:
            self.gaps.SetLabel("Every placeable component is claimed by a rule.")
            self.gaps.SetForegroundColour(self.theme.success)
            return

        total = sum(n for _s, n in gaps)
        shown = ", ".join(f"{sheet} ({n})" for sheet, n in gaps[:4])
        more = f" and {len(gaps) - 4} more" if len(gaps) > 4 else ""
        self.gaps.SetLabel(f"{total} component(s) are not claimed by any rule: {shown}{more}")
        self.gaps.SetForegroundColour(self.theme.warning)

    def _on_select(self) -> None:
        row = self._row()
        if row is None:
            self.detail.SetValue("")
            self.detail_title.SetLabel("Matched components")
            return

        rule = self.rules[row]
        error = rule_error(rule)
        if error:
            self.detail_title.SetLabel("Rule is invalid")
            self.detail.SetValue(error)
            return

        refs = preview(self.rules, self.components, row)
        self.detail_title.SetLabel(f"Claimed by rule {row + 1} ({len(refs)})")
        if refs:
            self.detail.SetValue("\n".join(refs[:1000]))
        else:
            self.detail.SetValue(
                "Nothing.\n\nEither the pattern matches no component, or an earlier "
                "rule already claimed everything it would match."
            )

    def _row(self) -> Optional[int]:
        rows = self.grid.GetSelectedRows()
        if rows:
            return rows[0]
        cursor = self.grid.GetGridCursorRow()
        return cursor if 0 <= cursor < len(self.rules) else None

    # -- actions -----------------------------------------------------------

    def _on_click(self, event) -> None:
        """Clicking the 'On' column toggles the rule."""
        if event.GetCol() == 0 and 0 <= event.GetRow() < len(self.rules):
            self.rules[event.GetRow()].enabled = not self.rules[event.GetRow()].enabled
            self._refresh()
            return
        event.Skip()

    def _on_add(self) -> None:
        if not self.boards:
            message(self, "Create a board first.", "No boards", wx.ICON_INFORMATION)
            return
        dialog = RuleEditDialog(self, None, self.boards, self.components)
        if dialog.ShowModal() == wx.ID_OK:
            self.rules.append(dialog.rule)
            self._refresh()
        dialog.Destroy()

    def _on_edit(self) -> None:
        row = self._row()
        if row is None:
            return
        dialog = RuleEditDialog(self, self.rules[row], self.boards, self.components)
        if dialog.ShowModal() == wx.ID_OK:
            self.rules[row] = dialog.rule
            self._refresh()
        dialog.Destroy()

    def _on_remove(self) -> None:
        row = self._row()
        if row is None:
            return
        rule = self.rules[row]
        if confirm(
            self,
            f"Remove the rule '{rule.pattern}' -> {rule.board}?\n\n"
            "Components it claimed will become unassigned unless another "
            "rule matches them.",
            "Remove rule",
        ):
            del self.rules[row]
            self._refresh()

    def _move(self, delta: int) -> None:
        row = self._row()
        if row is None:
            return
        target = row + delta
        if not (0 <= target < len(self.rules)):
            return
        self.rules[row], self.rules[target] = self.rules[target], self.rules[row]
        self._refresh()
        self.grid.SelectRow(target)
        self.grid.SetGridCursor(target, 0)

    def _on_suggest(self) -> None:
        suggested = self._suggest()
        if not suggested:
            message(
                self,
                "No schematic sheet matched a board name.\n\n"
                "This works when your top-level sheets are named after your boards, "
                "for example a /Power/ sheet and a board called Power.",
                "Nothing to suggest",
            )
            return

        existing = {(r.kind, r.pattern, r.board) for r in self.rules}
        fresh = [r for r in suggested if (r.kind, r.pattern, r.board) not in existing]
        if not fresh:
            message(self, "Those rules are already in the list.", "Nothing to add")
            return

        listing = "\n".join(f"  {r.pattern}  ->  {r.board}" for r in fresh)
        if confirm(self, f"Add {len(fresh)} rule(s)?\n\n{listing}", "Suggested rules", wx.ICON_QUESTION):
            self.rules.extend(fresh)
            self._refresh()


def edit_rules(parent, rules, boards, components, suggest) -> Optional[list[AssignRule]]:
    """Show the editor. Returns the new rule list, or None if cancelled."""
    dialog = RulesDialog(parent, rules, boards, components, suggest)
    try:
        return dialog.rules if dialog.ShowModal() == wx.ID_OK else None
    finally:
        dialog.Destroy()
