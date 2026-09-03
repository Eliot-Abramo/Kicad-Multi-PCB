"""
Update preview: see exactly what will change, before anything is written.

v12's Update was a black box. You pressed it, it worked for a while, and then it
told you how many components it had added. There was no way to notice it was
about to add four hundred parts to the wrong board, and no way to stop it.

Every row here has a checkbox. Destructive actions -- removals and footprint
replacements -- start unchecked, so the safe path is the default one and
accepting a deletion is always a deliberate act.
"""

from typing import Optional

import wx
import wx.grid as gridlib

from ..core.plan import ACTION_LABELS, ACTION_ORDER, REMOVE, REPLACE, SKIP, UpdatePlan
from .theme import apply_grid, set_row_colors
from .widgets import SPACING_MD, SPACING_SM, BaseDialog, ReadOnlyText

COLUMNS = [("", 34), ("Action", 20), ("Ref", 90), ("Reason", 300), ("From", 170), ("To", 170)]


class PlanDialog(BaseDialog):
    """Review an update plan and choose what to apply."""

    def __init__(self, parent, plan: UpdatePlan, *, allow_apply: bool = True):
        super().__init__(parent, f"Update '{plan.board}'", size=(960, 640), min_size=(780, 520))
        self.plan = plan
        self.applied = False
        self._rows = [i for i in plan.items if i.action != SKIP] + [i for i in plan.items if i.action == SKIP]
        self._build(allow_apply)
        self._refresh()

    def _build(self, allow_apply: bool) -> None:
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.summary = wx.StaticText(self, label=self.plan.summary())
        self.summary.SetFont(self.theme.title_font())
        sizer.Add(self.summary, 0, wx.ALL, SPACING_MD)

        explain = wx.StaticText(
            self,
            label=(
                "Nothing is written until you press Apply. Removals and footprint "
                "replacements start unchecked because they discard existing work."
            ),
        )
        explain.SetForegroundColour(self.theme.text_muted)
        explain.SetFont(self.theme.small_font())
        sizer.Add(explain, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, SPACING_MD)

        if self.plan.conflicts:
            warning = wx.StaticText(
                self,
                label=(
                    f"{len(self.plan.conflicts)} component(s) touching this board have "
                    "conflicting assignments. Resolve them in the Components view first "
                    "if this update looks wrong."
                ),
            )
            warning.SetForegroundColour(self.theme.warning)
            sizer.Add(warning, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, SPACING_MD)

        self.grid = gridlib.Grid(self)
        self.grid.CreateGrid(0, len(COLUMNS))
        for i, (label, width) in enumerate(COLUMNS):
            self.grid.SetColLabelValue(i, label)
            self.grid.SetColSize(i, width)
        self.grid.SetRowLabelSize(0)
        self.grid.EnableEditing(False)
        self.grid.SetSelectionMode(gridlib.Grid.SelectRows)
        self.grid.Bind(gridlib.EVT_GRID_CELL_LEFT_CLICK, self._on_click)
        sizer.Add(self.grid, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, SPACING_MD)

        tools = wx.BoxSizer(wx.HORIZONTAL)
        for label, handler in (
            ("Select all", lambda: self._set_all(True)),
            ("Select none", lambda: self._set_all(False)),
            ("Safe selection", self._safe_selection),
        ):
            button = wx.Button(self, label=label, style=wx.BU_EXACTFIT)
            button.Bind(wx.EVT_BUTTON, lambda e, h=handler: h())
            tools.Add(button, 0, wx.RIGHT, SPACING_SM)
        tools.AddStretchSpacer()

        self.selection_label = wx.StaticText(self, label="")
        self.selection_label.SetFont(self.theme.small_font())
        self.selection_label.SetForegroundColour(self.theme.text_muted)
        tools.Add(self.selection_label, 0, wx.ALIGN_CENTER_VERTICAL)
        sizer.Add(tools, 0, wx.ALL | wx.EXPAND, SPACING_MD)

        buttons = wx.StdDialogButtonSizer()
        self.apply = wx.Button(self, wx.ID_OK, "Apply")
        self.apply.Enable(allow_apply)
        self.apply.SetDefault()
        buttons.AddButton(self.apply)
        buttons.AddButton(wx.Button(self, wx.ID_CANCEL, "Cancel"))
        buttons.Realize()
        sizer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, SPACING_MD)

        self.SetSizer(sizer)
        apply_grid(self.grid, self.theme)

    def _refresh(self) -> None:
        if self.grid.GetNumberRows():
            self.grid.DeleteRows(0, self.grid.GetNumberRows())
        if self._rows:
            self.grid.AppendRows(len(self._rows))

        for row, item in enumerate(self._rows):
            selectable = item.action != SKIP
            self.grid.SetCellValue(row, 0, "x" if item.enabled and selectable else "")
            self.grid.SetCellValue(row, 1, ACTION_LABELS.get(item.action, item.action))
            self.grid.SetCellValue(row, 2, item.ref)
            self.grid.SetCellValue(row, 3, item.reason)
            self.grid.SetCellValue(row, 4, item.before)
            self.grid.SetCellValue(row, 5, item.after)

            accent = {
                REMOVE: self.theme.error,
                REPLACE: self.theme.warning,
            }.get(item.action)

            if item.action == SKIP:
                bg, fg = self.theme.window_bg, self.theme.text_muted
            elif accent is not None:
                bg = self.theme.tint(accent, 0.16)
                fg = self.theme.readable(self.theme.text, bg)
            else:
                bg, fg = self.theme.window_bg, self.theme.text
            set_row_colors(self.grid, row, len(COLUMNS), bg, fg)

        self._update_selection_label()

    def _update_selection_label(self) -> None:
        counts = {}
        for item in self.plan.items:
            if item.enabled and item.action != SKIP:
                counts[item.action] = counts.get(item.action, 0) + 1
        text = ", ".join(f"{ACTION_LABELS[a]} {counts[a]}" for a in ACTION_ORDER if counts.get(a))
        self.selection_label.SetLabel(f"Will apply: {text}" if text else "Nothing selected")
        self.apply.Enable(bool(counts))

    def _on_click(self, event) -> None:
        row = event.GetRow()
        if event.GetCol() == 0 and 0 <= row < len(self._rows):
            item = self._rows[row]
            if item.action != SKIP:
                item.enabled = not item.enabled
                self._refresh()
                return
        event.Skip()

    def _set_all(self, value: bool) -> None:
        for item in self.plan.items:
            if item.action != SKIP:
                item.enabled = value
        self._refresh()

    def _safe_selection(self) -> None:
        """Everything additive; nothing that discards existing work."""
        for item in self.plan.items:
            item.enabled = item.action not in (REMOVE, REPLACE, SKIP)
        self._refresh()


def review_plan(parent, plan: UpdatePlan, *, allow_apply: bool = True) -> Optional[UpdatePlan]:
    """
    Show the plan. Returns it with the user's selections, or None if cancelled.
    """
    dialog = PlanDialog(parent, plan, allow_apply=allow_apply)
    try:
        return plan if dialog.ShowModal() == wx.ID_OK else None
    finally:
        dialog.Destroy()


class ResultDialog(BaseDialog):
    """What an update actually did, including anything that failed."""

    def __init__(self, parent, board: str, result):
        super().__init__(parent, f"Updated '{board}'", size=(680, 440), min_size=(520, 340))
        sizer = wx.BoxSizer(wx.VERTICAL)

        headline = wx.StaticText(self, label=result.summary())
        headline.SetFont(self.theme.title_font())
        headline.SetForegroundColour(
            self.theme.warning if (result.failed or result.cancelled) else self.theme.success
        )
        sizer.Add(headline, 0, wx.ALL, SPACING_MD)

        details = []
        if result.cancelled:
            details.append("Cancelled before saving. The board file was not modified.")
        for warning in result.warnings:
            details.append(f"Warning: {warning}")
        if result.failed:
            details.append("")
            details.append("These components could not be placed:")
            details.extend(f"  {line}" for line in result.failed)
            details.append("")
            details.append(
                "A footprint that will not load is usually a library that is not "
                "registered for this project. Run Doctor to check the library table."
            )

        text = ReadOnlyText(self, "\n".join(details) or "No problems.", mono=False)
        sizer.Add(text, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, SPACING_MD)

        buttons = self.CreateStdDialogButtonSizer(wx.OK)
        sizer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, SPACING_MD)
        self.SetSizer(sizer)
