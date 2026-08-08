"""
The command palette: type a reference, go to the component.

This is the direct answer to the workflow this plugin exists to fix -- opening
every PCB in turn and looking for a part by eye. Press Ctrl+P, type ``R42``, and
the answer is on screen before you finish typing; press Enter and KiCad's canvas
goes to it.

Search runs against the in-memory index, so it costs a few milliseconds even on
a ten-thousand-component design and genuinely runs per keystroke.
"""

from typing import Callable, Optional

import wx

from ..core.index import ComponentIndex, ComponentRecord, Status
from .theme import apply_input, get_theme
from .widgets import SPACING_MD, VirtualListCtrl

COMMANDS = [
    ("reindex", "Refresh index", "Re-read every board and the schematic"),
    ("doctor", "Run Doctor", "Check the project for problems"),
    ("rules", "Edit assignment rules", "Decide which components go on which board"),
    ("conflicts", "Show conflicts", "Components whose intent and placement disagree"),
    ("drc", "Run DRC on all boards", "Design rule check, every board"),
    ("xref", "Open cross-reference", "The full component table"),
]


class CommandPalette(wx.Dialog):
    """
    A frameless search overlay.

    Deliberately not a :class:`BaseDialog`: it has no title bar, no chrome, and
    closes on losing focus, so it needs its own lifecycle.
    """

    def __init__(
        self,
        parent,
        index: ComponentIndex,
        on_component: Callable[[ComponentRecord], None],
        on_command: Callable[[str], None],
        board_color: Optional[Callable[[str], wx.Colour]] = None,
    ):
        super().__init__(parent, style=wx.BORDER_SIMPLE | wx.FRAME_FLOAT_ON_PARENT, size=(760, 480))
        self.theme = get_theme()
        self.index = index
        self.on_component = on_component
        self.on_command = on_command
        self.board_color = board_color or (lambda _n: self.theme.accent)
        self._records: list[ComponentRecord] = []
        self._commands: list[tuple] = []

        self.SetBackgroundColour(self.theme.panel_bg)
        self._build()
        self.CentreOnParent()
        self._search("")

    def _build(self) -> None:
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.input = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
        self.input.SetFont(self.theme.font(1.3))
        self.input.SetHint("Find a component, or type > for commands")
        apply_input(self.input)
        self.input.Bind(wx.EVT_TEXT, lambda e: self._search(self.input.GetValue()))
        self.input.Bind(wx.EVT_TEXT_ENTER, lambda e: self._activate())
        sizer.Add(self.input, 0, wx.ALL | wx.EXPAND, SPACING_MD)

        self.hint = wx.StaticText(self, label="")
        self.hint.SetFont(self.theme.small_font())
        self.hint.SetForegroundColour(self.theme.text_muted)
        sizer.Add(self.hint, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, SPACING_MD)

        self.results = VirtualListCtrl(
            self,
            [
                ("Ref", 90),
                ("Value", 130),
                ("Board", 130),
                ("Status", 110),
                ("Sheet", 150),
                ("Footprint", 200),
            ],
        )
        self.results.Bind(wx.EVT_LIST_ITEM_ACTIVATED, lambda e: self._activate())
        sizer.Add(self.results, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, SPACING_MD)

        self.detail = wx.StaticText(self, label="")
        self.detail.SetFont(self.theme.small_font())
        self.detail.SetForegroundColour(self.theme.text_muted)
        sizer.Add(self.detail, 0, wx.ALL | wx.EXPAND, SPACING_MD)

        self.SetSizer(sizer)
        self.input.SetFocus()

        for widget in (self, self.input, self.results):
            widget.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self.results.Bind(wx.EVT_LIST_ITEM_SELECTED, lambda e: self._update_detail())

    # -- search ------------------------------------------------------------

    def _search(self, query: str) -> None:
        if query.startswith(">"):
            self._search_commands(query[1:].strip().lower())
            return

        self._commands = []
        hits = self.index.search(query, limit=200) if query else self._recent()
        self._records = [h.record for h in hits] if query else hits

        rows, colors = [], []
        for rec in self._records:
            boards = ", ".join(rec.boards) or (rec.intent or "-")
            rows.append(
                [
                    rec.ref,
                    rec.value,
                    boards,
                    Status.LABELS.get(rec.status, rec.status),
                    rec.sheet,
                    rec.footprint,
                ]
            )
            colors.append(self.theme.status_color(rec.status) if rec.is_conflict else None)

        self.results.set_rows(rows, colors)
        if rows:
            self.results.select(0)

        total = len(self.index.records())
        if not query:
            self.hint.SetLabel(
                f"{total} component(s) indexed. Type a reference, a value, or a filter "
                "like board:Power, status:duplicate, net:GND."
            )
        else:
            self.hint.SetLabel(f"{len(rows)} match(es) of {total}")
        self._update_detail()

    def _recent(self) -> list[ComponentRecord]:
        """With no query, lead with whatever needs attention."""
        conflicts = self.index.conflicts()
        if conflicts:
            return conflicts[:200]
        return self.index.records()[:200]

    def _search_commands(self, needle: str) -> None:
        self._records = []
        self._commands = [c for c in COMMANDS if not needle or needle in c[1].lower() or needle in c[0]]
        self.results.set_rows([[c[1], c[2], "", "", "", ""] for c in self._commands])
        if self._commands:
            self.results.select(0)
        self.hint.SetLabel("Commands")
        self.detail.SetLabel("")

    def _update_detail(self) -> None:
        index = self.results.selected_index()
        if index < 0 or index >= len(self._records):
            self.detail.SetLabel("")
            return
        rec = self._records[index]
        parts = [rec.hint()]
        if rec.why:
            parts.append(f"Assigned by {rec.why}.")
        if rec.placements:
            first = rec.placements[0]
            parts.append(f"At {first.position()} on {first.board} ({first.side}).")
        self.detail.SetLabel(" ".join(p for p in parts if p))

    # -- keys --------------------------------------------------------------

    def _on_key(self, event: wx.KeyEvent) -> None:
        key = event.GetKeyCode()
        if key == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
            return
        if key in (wx.WXK_DOWN, wx.WXK_UP):
            self._move(1 if key == wx.WXK_DOWN else -1)
            return
        if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self._activate()
            return
        if key in (wx.WXK_PAGEDOWN, wx.WXK_PAGEUP):
            self._move(10 if key == wx.WXK_PAGEDOWN else -10)
            return
        event.Skip()

    def _move(self, delta: int) -> None:
        count = self.results.GetItemCount()
        if not count:
            return
        current = max(0, self.results.selected_index())
        self.results.select(max(0, min(count - 1, current + delta)))
        self._update_detail()
        self.input.SetFocus()

    def _activate(self) -> None:
        index = self.results.selected_index()
        if index < 0:
            return

        if self._commands:
            command = self._commands[index][0]
            self.EndModal(wx.ID_OK)
            self.on_command(command)
            return

        if index < len(self._records):
            record = self._records[index]
            self.EndModal(wx.ID_OK)
            self.on_component(record)


def open_palette(parent, index, on_component, on_command, board_color=None) -> None:
    """Show the palette and destroy it afterwards."""
    dialog = CommandPalette(parent, index, on_component, on_command, board_color)
    try:
        dialog.ShowModal()
    finally:
        dialog.Destroy()
