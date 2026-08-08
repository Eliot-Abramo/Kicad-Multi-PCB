"""
The component cross-reference: every part, where it is, where it should be, why.

A first-class view rather than a buried dialog, because "where does this
component live?" is the question this tool exists to answer. Everything it shows
is derived from the index, so it is instant, and every assignment change made
here is intent only -- no PCB and no schematic is written.

The grid is virtual: a ten-thousand-component design renders in constant time.
v12's equivalent built real rows and truncated at 100 per board, so a component
past position 100 could not be seen at all.
"""

from typing import Callable, Optional

import wx
import wx.grid as gridlib

from ..core.index import ComponentIndex, ComponentRecord, Status
from ..core.rules import natural_key
from .theme import apply_grid, get_theme, set_row_colors
from .widgets import (
    SPACING_MD,
    SPACING_SM,
    Banner,
    FilterChips,
    SearchBox,
    message,
)

COLUMNS = [
    ("Ref", 90),
    ("Value", 120),
    ("Footprint", 190),
    ("Sheet", 130),
    ("Assigned", 110),
    ("Why", 170),
    ("Placed on", 120),
    ("Side", 55),
    ("X", 70),
    ("Y", 70),
    ("Status", 100),
]

MENU_JUMP = wx.ID_HIGHEST + 200
MENU_ASSIGN = wx.ID_HIGHEST + 201
MENU_ADOPT = wx.ID_HIGHEST + 202
MENU_CLEAR = wx.ID_HIGHEST + 203
MENU_COPY_REF = wx.ID_HIGHEST + 204
MENU_COPY_ROW = wx.ID_HIGHEST + 205
MENU_NET = wx.ID_HIGHEST + 206


class XrefTable(gridlib.GridTableBase):
    """Virtual table over a list of records."""

    def __init__(self):
        super().__init__()
        self.records: list[ComponentRecord] = []
        self.theme = get_theme()

    def GetNumberRows(self):
        return len(self.records)

    def GetNumberCols(self):
        return len(COLUMNS)

    def GetColLabelValue(self, col):
        return COLUMNS[col][0]

    def IsEmptyCell(self, row, col):
        return False

    def GetValue(self, row, col):
        if row >= len(self.records):
            return ""
        rec = self.records[row]
        first = rec.placements[0] if rec.placements else None
        return [
            rec.ref,
            rec.value,
            rec.footprint,
            rec.sheet,
            rec.intent or "",
            rec.why,
            ", ".join(rec.boards),
            first.side if first else "",
            f"{first.x_mm:.2f}" if first else "",
            f"{first.y_mm:.2f}" if first else "",
            Status.LABELS.get(rec.status, rec.status),
        ][col]

    def SetValue(self, row, col, value):
        return  # read-only


class XrefPanel(wx.Panel):
    """Filter chips, a search box, and the table."""

    def __init__(
        self,
        parent,
        index: ComponentIndex,
        *,
        on_jump: Callable[[ComponentRecord], None],
        on_assign: Callable[[list[str], Optional[str]], None],
        on_adopt: Callable[[list[str]], None],
        board_names: Callable[[], list[str]],
        board_color: Callable[[str], wx.Colour],
    ):
        super().__init__(parent)
        self.theme = get_theme()
        self.index = index
        self.on_jump = on_jump
        self.on_assign = on_assign
        self.on_adopt = on_adopt
        self.board_names = board_names
        self.board_color = board_color
        self._visible: list[ComponentRecord] = []
        self._sort_col = 0
        self._sort_desc = False

        self._build()

    def _build(self) -> None:
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.banner = Banner(self, "", "warning", "Show conflicts", self._only_conflicts)
        self.banner.Hide()
        sizer.Add(self.banner, 0, wx.ALL | wx.EXPAND, SPACING_SM)

        top = wx.BoxSizer(wx.HORIZONTAL)
        self.search = SearchBox(
            self,
            "Search components (try board:Power or status:duplicate)",
            on_change=lambda _v: self.refresh_view(),
            size=(360, -1),
        )
        top.Add(self.search, 0, wx.RIGHT, SPACING_MD)

        self.count = wx.StaticText(self, label="")
        self.count.SetFont(self.theme.small_font())
        self.count.SetForegroundColour(self.theme.text_muted)
        top.Add(self.count, 1, wx.ALIGN_CENTER_VERTICAL)

        export = wx.Button(self, label="Export CSV...", style=wx.BU_EXACTFIT)
        export.Bind(wx.EVT_BUTTON, self._on_export)
        top.Add(export, 0, wx.LEFT, SPACING_SM)
        sizer.Add(top, 0, wx.ALL | wx.EXPAND, SPACING_SM)

        self.chips = FilterChips(self, self.refresh_view)
        sizer.Add(self.chips, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, SPACING_SM)

        self.table = XrefTable()
        self.grid = gridlib.Grid(self)
        self.grid.SetTable(self.table, True)
        self.grid.EnableEditing(False)
        self.grid.SetRowLabelSize(0)
        self.grid.SetSelectionMode(gridlib.Grid.SelectRows)
        for i, (_label, width) in enumerate(COLUMNS):
            self.grid.SetColSize(i, width)

        self.grid.Bind(gridlib.EVT_GRID_CELL_LEFT_DCLICK, self._on_activate)
        self.grid.Bind(gridlib.EVT_GRID_CELL_RIGHT_CLICK, self._on_context)
        self.grid.Bind(gridlib.EVT_GRID_LABEL_LEFT_CLICK, self._on_sort)
        sizer.Add(self.grid, 1, wx.ALL | wx.EXPAND, SPACING_SM)

        self.SetSizer(sizer)
        apply_grid(self.grid, self.theme)

    # -- data --------------------------------------------------------------

    def refresh_view(self) -> None:
        """Recompute chips and rows from the index. No I/O."""
        self._rebuild_chips()
        records = self._filtered()
        self._visible = self._sorted(records)
        self._populate()
        self._update_banner()

    def _rebuild_chips(self) -> None:
        records = self.index.records()
        counts: dict[str, int] = {}
        for rec in records:
            counts[rec.status] = counts.get(rec.status, 0) + 1

        chips = [("all", "All", len(records), None)]
        for key, label in (
            (Status.DUPLICATE, "Duplicates"),
            (Status.MISPLACED, "Misplaced"),
            (Status.ORPHAN, "Orphans"),
            (Status.TODO, "Not placed"),
            (Status.ADOPT, "Unassigned"),
            (Status.NOWHERE, "No home"),
            (Status.SKIPPED, "Skipped"),
        ):
            if counts.get(key):
                chips.append((f"status:{key}", label, counts[key], self.theme.status_color(key)))

        by_board = self.index.stats.by_board or {}
        for name in self.board_names():
            chips.append((f"board:{name}", name, by_board.get(name, 0), self.board_color(name)))

        self.chips.set_chips(chips)

    def _filtered(self) -> list[ComponentRecord]:
        selected = [k for k in self.chips.selected() if k != "all"]
        query_parts = list(selected)
        text = self.search.GetValue().strip()
        if text:
            query_parts.append(text)

        if not query_parts:
            return self.index.records()
        return [h.record for h in self.index.search(" ".join(query_parts), limit=1_000_000)]

    def _sorted(self, records: list[ComponentRecord]) -> list[ComponentRecord]:
        col = self._sort_col

        def key(rec: ComponentRecord):
            first = rec.placements[0] if rec.placements else None
            primary = [
                natural_key(rec.ref),
                rec.value.lower(),
                rec.footprint.lower(),
                rec.sheet.lower(),
                (rec.intent or "").lower(),
                rec.why.lower(),
                ", ".join(rec.boards).lower(),
                first.side if first else "",
                first.x_mm if first else 0.0,
                first.y_mm if first else 0.0,
                rec.status,
            ][col]
            # Refs always break ties naturally, so R9 precedes R10 everywhere.
            return (primary, natural_key(rec.ref)) if col else primary

        return sorted(records, key=key, reverse=self._sort_desc)

    def _populate(self) -> None:
        grid = self.grid
        previous = len(self.table.records)
        self.table.records = self._visible

        grid.BeginBatch()
        try:
            delta = len(self._visible) - previous
            if delta > 0:
                grid.ProcessTableMessage(
                    gridlib.GridTableMessage(self.table, gridlib.GRIDTABLE_NOTIFY_ROWS_APPENDED, delta)
                )
            elif delta < 0:
                grid.ProcessTableMessage(
                    gridlib.GridTableMessage(
                        self.table, gridlib.GRIDTABLE_NOTIFY_ROWS_DELETED, len(self._visible), -delta
                    )
                )
            grid.ProcessTableMessage(
                gridlib.GridTableMessage(self.table, gridlib.GRIDTABLE_REQUEST_VIEW_GET_VALUES)
            )

            for row, rec in enumerate(self._visible):
                self._paint_row(row, rec)
        finally:
            grid.EndBatch()
        grid.ForceRefresh()

        total = len(self.index.records())
        shown = len(self._visible)
        self.count.SetLabel(f"{shown} of {total} component(s)" if shown != total else f"{total} component(s)")

    def _paint_row(self, row: int, rec: ComponentRecord) -> None:
        """
        Colour a row by board identity, or by severity when it needs attention.

        Board colour is how placement becomes readable without reading: you learn
        "Power is teal" once and then recognise it everywhere.
        """
        theme = self.theme
        if rec.is_conflict:
            accent = theme.status_color(rec.status)
            bg = theme.tint(accent, 0.18)
            fg = theme.readable(theme.text, bg)
        elif rec.boards:
            accent = self.board_color(rec.boards[0])
            bg = theme.tint(accent, 0.10)
            fg = theme.readable(theme.text, bg)
        else:
            bg, fg = theme.window_bg, theme.text_muted

        set_row_colors(self.grid, row, len(COLUMNS), bg, fg)

        status_col = len(COLUMNS) - 1
        self.grid.SetCellTextColour(row, status_col, theme.readable(theme.status_color(rec.status), bg))

    def _update_banner(self) -> None:
        conflicts = self.index.stats.conflicts
        if conflicts:
            self.banner.set_message(f"{conflicts} component(s) have conflicting assignments.", "warning")
            self.banner.Show()
        else:
            self.banner.Hide()
        self.Layout()

    def _only_conflicts(self) -> None:
        self.chips.clear()
        self.search.SetValue("")
        for key, button, _ in self.chips._buttons:
            if key.startswith("status:") and key.split(":", 1)[1] in Status.CONFLICTS:
                button.SetValue(True)
        self.refresh_view()

    # -- interaction -------------------------------------------------------

    def selected_records(self) -> list[ComponentRecord]:
        rows = set(self.grid.GetSelectedRows())
        if not rows:
            cursor = self.grid.GetGridCursorRow()
            if 0 <= cursor < len(self._visible):
                rows = {cursor}
        return [self._visible[r] for r in sorted(rows) if r < len(self._visible)]

    def _on_sort(self, event) -> None:
        col = event.GetCol()
        if col < 0:
            return
        if col == self._sort_col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col, self._sort_desc = col, False
        self.refresh_view()

    def _on_activate(self, event) -> None:
        row = event.GetRow()
        if 0 <= row < len(self._visible):
            self.on_jump(self._visible[row])

    def _on_context(self, event) -> None:
        row = event.GetRow()
        if 0 <= row < len(self._visible) and row not in self.grid.GetSelectedRows():
            self.grid.SelectRow(row)
            self.grid.SetGridCursor(row, max(0, event.GetCol()))

        records = self.selected_records()
        if not records:
            return

        menu = wx.Menu()
        one = records[0] if len(records) == 1 else None

        if one is not None:
            menu.Append(MENU_JUMP, "Go to component\tEnter")
            menu.Enable(MENU_JUMP, bool(one.placements))
            menu.AppendSeparator()

        assign_menu = wx.Menu()
        self._assign_ids = {}
        for i, name in enumerate(self.board_names()):
            item_id = wx.ID_HIGHEST + 300 + i
            assign_menu.Append(item_id, name)
            self._assign_ids[item_id] = name
            self.Bind(wx.EVT_MENU, lambda e, n=name: self.on_assign([r.ref for r in records], n), id=item_id)
        label = "Assign to board" if len(records) == 1 else f"Assign {len(records)} to board"
        menu.AppendSubMenu(assign_menu, label)

        adoptable = [r for r in records if len(r.placements) == 1]
        menu.Append(MENU_ADOPT, f"Adopt placement as intent ({len(adoptable)})")
        menu.Enable(MENU_ADOPT, bool(adoptable))

        pinned = [r for r in records if r.intent is not None]
        menu.Append(MENU_CLEAR, "Clear assignment")
        menu.Enable(MENU_CLEAR, bool(pinned))

        menu.AppendSeparator()
        menu.Append(MENU_COPY_REF, "Copy reference(s)")
        menu.Append(MENU_COPY_ROW, "Copy row(s) as text")
        if one is not None:
            menu.Append(MENU_NET, "Show nets...")

        self.Bind(wx.EVT_MENU, lambda e: self.on_jump(one), id=MENU_JUMP)
        self.Bind(wx.EVT_MENU, lambda e: self.on_adopt([r.ref for r in adoptable]), id=MENU_ADOPT)
        self.Bind(wx.EVT_MENU, lambda e: self.on_assign([r.ref for r in records], None), id=MENU_CLEAR)
        self.Bind(wx.EVT_MENU, lambda e: _copy("\n".join(r.ref for r in records)), id=MENU_COPY_REF)
        self.Bind(wx.EVT_MENU, lambda e: self._copy_rows(records), id=MENU_COPY_ROW)
        if one is not None:
            self.Bind(wx.EVT_MENU, lambda e: self._show_nets(one), id=MENU_NET)

        self.PopupMenu(menu)
        menu.Destroy()

    def _copy_rows(self, records: list[ComponentRecord]) -> None:
        lines = ["\t".join(c[0] for c in COLUMNS)]
        for rec in records:
            row = self._visible.index(rec) if rec in self._visible else -1
            if row >= 0:
                lines.append("\t".join(self.table.GetValue(row, c) for c in range(len(COLUMNS))))
        _copy("\n".join(lines))

    def _show_nets(self, rec: ComponentRecord) -> None:
        lines = []
        for net in self.index.net_names():
            nodes = [n for n in self.index.net(net) if n[1] == rec.ref]
            for board, _ref, pad in nodes:
                lines.append(f"pad {pad}  {net}   (on {board})")
        message(
            self,
            "\n".join(lines) if lines else f"{rec.ref} has no nets recorded on any board.",
            f"Nets on {rec.ref}",
        )

    def _on_export(self, _event) -> None:
        with wx.FileDialog(
            self,
            "Export cross-reference",
            wildcard="CSV files (*.csv)|*.csv",
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT,
            defaultFile="component-xref.csv",
        ) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            path = dialog.GetPath()

        try:
            with open(path, "w", encoding="utf-8", newline="") as fh:
                n = self.index.write_csv(fh, self._visible)
        except OSError as exc:
            message(self, f"Could not write the file:\n\n{exc}", "Export failed", wx.ICON_ERROR)
            return
        message(self, f"Wrote {n} row(s) to\n{path}", "Export complete")

    def retheme(self) -> None:
        self.theme = get_theme()
        self.table.theme = self.theme
        apply_grid(self.grid, self.theme)
        self.banner.retheme()
        self.chips.retheme()
        self.count.SetForegroundColour(self.theme.text_muted)
        self.refresh_view()


def _copy(text: str) -> None:
    if wx.TheClipboard.Open():
        try:
            wx.TheClipboard.SetData(wx.TextDataObject(text))
        finally:
            wx.TheClipboard.Close()
