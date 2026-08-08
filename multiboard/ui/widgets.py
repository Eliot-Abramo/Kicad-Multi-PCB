"""
Shared widgets and dialog plumbing.

Several v12 bugs were structural rather than cosmetic and are fixed here once:

* ``BaseDialog._on_char`` called ``EndModal`` unconditionally, which asserts when
  the dialog was shown non-modally. :class:`BaseDialog` checks ``IsModal`` first.
* Report dialogs were never destroyed. :func:`show_modal` makes that impossible.
* The main window's key handler ran before the focused text control saw the key,
  so Backspace in the filter box prompted to delete a board. :func:`typing_in_text`
  is the guard every key handler now starts with.
"""

from collections.abc import Sequence
from typing import Callable, Optional

import wx

from .theme import Theme, apply_chrome, apply_input, get_theme

SPACING_XS, SPACING_SM, SPACING_MD, SPACING_LG, SPACING_XL = 4, 8, 12, 16, 24


def typing_in_text(window: Optional[wx.Window] = None) -> bool:
    """
    Whether keyboard focus is in something that consumes ordinary keys.

    Every ``EVT_CHAR_HOOK`` handler must consult this before acting on a bare
    key. v12 did not, and mapped Backspace to "delete board" globally -- so its
    filter box could not be used at all.
    """
    focus = window or wx.Window.FindFocus()
    if focus is None:
        return False
    if isinstance(focus, (wx.TextCtrl, wx.ComboBox, wx.SearchCtrl, wx.SpinCtrl)):
        return True
    parent = focus.GetParent()
    return isinstance(parent, (wx.ComboBox, wx.SearchCtrl))


def show_modal(dialog: wx.Dialog) -> int:
    """Show a dialog modally and always destroy it."""
    try:
        return dialog.ShowModal()
    finally:
        dialog.Destroy()


def message(parent, text: str, caption: str = "Multi-Board Manager", icon: int = wx.ICON_INFORMATION) -> int:
    """A message box with a button flag actually set.

    v12 passed ``wx.ICON_ERROR`` as the style with no button flag OR-ed in at
    roughly twenty call sites, which works by luck on GTK and asserts elsewhere.
    """
    return wx.MessageBox(text, caption, wx.OK | icon, parent)


def confirm(parent, text: str, caption: str = "Confirm", icon: int = wx.ICON_WARNING) -> bool:
    return wx.MessageBox(text, caption, wx.YES_NO | wx.NO_DEFAULT | icon, parent) == wx.YES


class BaseDialog(wx.Dialog):
    """A dialog that themes itself and closes correctly whether modal or not."""

    def __init__(
        self,
        parent,
        title: str,
        size: tuple[int, int] = (720, 520),
        min_size: Optional[tuple[int, int]] = None,
        **kwargs,
    ):
        min_w, min_h = min_size or (420, 300)
        size = (max(size[0], min_w), max(size[1], min_h))
        style = kwargs.pop("style", wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        super().__init__(parent, title=title, size=size, style=style, **kwargs)

        self.theme: Theme = get_theme()
        self.SetMinSize((min_w, min_h))
        self.SetBackgroundColour(self.theme.panel_bg)
        self.SetForegroundColour(self.theme.text)
        self.SetFont(self.theme.body_font())

        if parent is None:
            self.CentreOnScreen()
        else:
            self.CentreOnParent()

        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)

    def _on_char_hook(self, event: wx.KeyEvent) -> None:
        if event.GetKeyCode() == wx.WXK_ESCAPE and not self._escape_consumed():
            self.dismiss()
            return
        event.Skip()

    def _escape_consumed(self) -> bool:
        """Override when Escape should do something else first."""
        return False

    def dismiss(self, code: int = wx.ID_CANCEL) -> None:
        """
        Close correctly regardless of how the dialog was shown.

        v12 called ``EndModal`` from a handler shared with a non-modal progress
        dialog, and separately called ``Destroy`` twice on the main window -- so
        every normal close produced a "wrapped C/C++ object has been deleted"
        error box.
        """
        if self.IsModal():
            self.EndModal(code)
        else:
            self.Close()

    def retheme(self) -> None:
        """Re-apply the palette after a system appearance change."""
        self.theme = get_theme()
        self.SetBackgroundColour(self.theme.panel_bg)
        self.SetForegroundColour(self.theme.text)
        _retheme_tree(self, self.theme)
        self.Refresh()


def _retheme_tree(window: wx.Window, theme: Theme) -> None:
    """Walk a window tree asking anything themed to update itself."""
    for child in window.GetChildren():
        hook = getattr(child, "retheme", None)
        if callable(hook):
            hook()
        else:
            _retheme_tree(child, theme)


# =============================================================================
# Building blocks
# =============================================================================


class Badge(wx.Panel):
    """
    A small coloured chip: a board name, a status, a count.

    Drawn rather than composed from a coloured StaticText because macOS ignores
    ``SetForegroundColour`` on several native controls, and because a chip needs
    a rounded background that no stock widget provides.
    """

    def __init__(
        self,
        parent,
        label: str = "",
        color: Optional[wx.Colour] = None,
        *,
        filled: bool = False,
        padding: int = 6,
    ):
        super().__init__(parent, style=wx.TRANSPARENT_WINDOW)
        self.theme = get_theme()
        self._label = label
        self._color = color or self.theme.accent
        self._filled = filled
        self._padding = padding
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self._resize()

    def set_label(self, label: str, color: Optional[wx.Colour] = None) -> None:
        self._label = label
        if color is not None:
            self._color = color
        self._resize()
        self.Refresh()

    def retheme(self) -> None:
        self.theme = get_theme()
        self.Refresh()

    def _resize(self) -> None:
        dc = wx.ClientDC(self)
        dc.SetFont(self.theme.small_font())
        w, h = dc.GetTextExtent(self._label or " ")
        self.SetMinSize((w + self._padding * 2, h + 6))

    def _on_paint(self, _event) -> None:
        dc = wx.AutoBufferedPaintDC(self)
        w, h = self.GetSize()

        parent_bg = self.GetParent().GetBackgroundColour()
        dc.SetBackground(wx.Brush(parent_bg))
        dc.Clear()

        if self._filled:
            fill, text = self._color, _contrasting_text(self._color)
        else:
            fill, text = (
                self.theme.tint(self._color, 0.18),
                self.theme.readable(self._color, self.theme.tint(self._color, 0.18)),
            )

        dc.SetBrush(wx.Brush(fill))
        dc.SetPen(wx.Pen(self._color, 1))
        dc.DrawRoundedRectangle(0, 0, w, h, min(h // 2, 8))

        dc.SetFont(self.theme.small_font())
        dc.SetTextForeground(text)
        tw, th = dc.GetTextExtent(self._label)
        dc.DrawText(self._label, (w - tw) // 2, (h - th) // 2)


def _contrasting_text(background: wx.Colour) -> wx.Colour:
    from ..core.color import is_dark

    return (
        wx.Colour(255, 255, 255)
        if is_dark((background.Red(), background.Green(), background.Blue()))
        else wx.Colour(0, 0, 0)
    )


class Banner(wx.Panel):
    """A dismissable strip for a message that needs to stay visible."""

    def __init__(
        self,
        parent,
        text: str = "",
        kind: str = "info",
        action: Optional[str] = None,
        on_action: Optional[Callable] = None,
    ):
        super().__init__(parent)
        self.theme = get_theme()
        self._kind = kind
        self._on_action = on_action

        sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.label = wx.StaticText(self, label=text)
        self.label.SetFont(self.theme.body_font())
        sizer.Add(self.label, 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, SPACING_MD)

        self.button = None
        if action:
            self.button = wx.Button(self, label=action, style=wx.BU_EXACTFIT)
            self.button.Bind(wx.EVT_BUTTON, lambda e: on_action and on_action())
            sizer.Add(self.button, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, SPACING_SM)

        self.SetSizer(sizer)
        self.retheme()

    def set_message(self, text: str, kind: str = "info") -> None:
        self._kind = kind
        self.label.SetLabel(text)
        self.retheme()
        self.Show(bool(text))
        parent = self.GetParent()
        if parent:
            parent.Layout()

    def retheme(self) -> None:
        self.theme = get_theme()
        accent = {
            "info": self.theme.info,
            "warning": self.theme.warning,
            "error": self.theme.error,
            "success": self.theme.success,
        }.get(self._kind, self.theme.info)

        background = self.theme.tint(accent, 0.16)
        self.SetBackgroundColour(background)
        # Both halves, always: this is the pairing rule that v12 broke.
        self.label.SetForegroundColour(self.theme.readable(self.theme.text, background))
        self.Refresh()


class SearchBox(wx.SearchCtrl):
    """
    A search field that does not fight the platform.

    Left entirely to the native renderer -- no forced background -- which is the
    simplest way to be correct in both light and dark mode.
    """

    def __init__(self, parent, placeholder: str = "Search...", on_change=None, size=(280, -1)):
        super().__init__(parent, size=size, style=wx.TE_PROCESS_ENTER)
        self.SetDescriptiveText(placeholder)
        self.ShowSearchButton(True)
        self.ShowCancelButton(True)
        self.SetFont(get_theme().body_font())
        if on_change:
            self.Bind(wx.EVT_TEXT, lambda e: (on_change(self.GetValue()), e.Skip()))
            self.Bind(wx.EVT_SEARCHCTRL_CANCEL_BTN, lambda e: (self.SetValue(""), on_change("")))

    def retheme(self) -> None:
        self.SetFont(get_theme().body_font())


class StatusBar(wx.Panel):
    """A footer line with a state dot, a message, and an optional detail."""

    def __init__(self, parent):
        super().__init__(parent)
        self.theme = get_theme()
        sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.dot = Badge(self, "", self.theme.success, filled=True, padding=3)
        self.dot.SetMinSize((10, 10))
        sizer.Add(self.dot, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, SPACING_SM)

        self.label = wx.StaticText(self, label="Ready")
        self.label.SetFont(self.theme.small_font())
        sizer.Add(self.label, 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, SPACING_XS)

        self.SetSizer(sizer)
        apply_chrome(self, self.theme)

    def set_status(self, text: str, kind: str = "ok") -> None:
        color = {
            "ok": self.theme.success,
            "working": self.theme.accent,
            "warning": self.theme.warning,
            "error": self.theme.error,
        }.get(kind, self.theme.text_muted)
        self.dot.set_label("", color)
        self.label.SetLabel(text)
        self.label.SetForegroundColour(self.theme.text if kind == "ok" else color)
        self.Layout()

    def retheme(self) -> None:
        self.theme = get_theme()
        apply_chrome(self, self.theme)
        self.label.SetFont(self.theme.small_font())
        self.dot.retheme()


class FilterChips(wx.Panel):
    """
    A row of toggle chips with live counts.

    The primary way to slice the component list: "show me only the conflicts",
    "only this board". Composes with the search box rather than replacing it.
    """

    def __init__(self, parent, on_change: Callable[[], None]):
        super().__init__(parent)
        self.theme = get_theme()
        self._on_change = on_change
        self._buttons: list[tuple[str, wx.ToggleButton, Optional[wx.Colour]]] = []
        self.sizer = wx.WrapSizer(wx.HORIZONTAL)
        self.SetSizer(self.sizer)

    def set_chips(self, chips: Sequence[tuple[str, str, int, Optional[wx.Colour]]]) -> None:
        """``chips`` is ``(key, label, count, colour)``. Rebuilds the row."""
        selected = self.selected()
        self.sizer.Clear(delete_windows=True)
        self._buttons = []

        for key, label, count, colour in chips:
            text = f"{label} ({count})" if count is not None else label
            button = wx.ToggleButton(self, label=text, style=wx.BU_EXACTFIT)
            button.SetFont(self.theme.small_font())
            button.SetValue(key in selected)
            button.Bind(wx.EVT_TOGGLEBUTTON, lambda e: self._on_change())
            if colour is not None:
                button.SetForegroundColour(self.theme.readable(colour))
            self.sizer.Add(button, 0, wx.RIGHT | wx.BOTTOM, SPACING_XS)
            self._buttons.append((key, button, colour))

        self.Layout()

    def selected(self) -> list[str]:
        return [key for key, button, _ in self._buttons if button.GetValue()]

    def clear(self) -> None:
        for _, button, _ in self._buttons:
            button.SetValue(False)

    def retheme(self) -> None:
        self.theme = get_theme()
        for _, button, colour in self._buttons:
            button.SetFont(self.theme.small_font())
            if colour is not None:
                button.SetForegroundColour(self.theme.readable(colour))


class ReadOnlyText(wx.TextCtrl):
    """A read-only multi-line text area, themed as a matched pair."""

    def __init__(self, parent, value: str = "", *, mono: bool = True):
        super().__init__(parent, value=value, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP)
        self.SetFont(get_theme().mono_font() if mono else get_theme().body_font())
        apply_input(self)

    def retheme(self) -> None:
        apply_input(self)


class ProgressPanel(wx.Dialog):
    """
    A cancellable progress dialog that cannot be re-entered.

    v12 showed this non-modally and pumped the event loop with bare
    ``wx.Yield()``, so pressing Update again mid-update started a second run
    against the same board and the same temp netlist. ``wx.WindowDisabler``
    plus ``wx.SafeYield`` closes that; the cancel token means a long operation
    can also be abandoned without writing anything.
    """

    def __init__(self, parent, title: str = "Working..."):
        super().__init__(parent, title=title, size=(460, 170), style=wx.CAPTION | wx.SYSTEM_MENU)
        self.theme = get_theme()
        self._cancelled = False

        self.SetBackgroundColour(self.theme.panel_bg)
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.label = wx.StaticText(self, label="Starting...")
        self.label.SetFont(self.theme.body_font())
        self.label.SetForegroundColour(self.theme.text)
        sizer.Add(self.label, 0, wx.ALL | wx.EXPAND, SPACING_LG)

        self.gauge = wx.Gauge(self, range=100, size=(-1, 14))
        sizer.Add(self.gauge, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, SPACING_LG)

        self.cancel = wx.Button(self, wx.ID_CANCEL, "Cancel")
        self.cancel.Bind(wx.EVT_BUTTON, self._on_cancel)
        sizer.Add(self.cancel, 0, wx.ALL | wx.ALIGN_RIGHT, SPACING_LG)

        self.SetSizer(sizer)
        self.CentreOnParent()
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self.Bind(wx.EVT_CLOSE, self._on_close)

        self._disabler = wx.WindowDisabler(self)

    def _on_key(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self._on_cancel(None)
            return
        event.Skip()

    def _on_cancel(self, _event) -> None:
        self._cancelled = True
        self.label.SetLabel("Cancelling...")
        self.cancel.Disable()

    def _on_close(self, event) -> None:
        self._cancelled = True
        event.Skip()

    def cancelled(self) -> bool:
        return self._cancelled

    def update(self, percent: int, text: str = "") -> None:
        self.gauge.SetValue(max(0, min(100, int(percent))))
        if text:
            self.label.SetLabel(text)
        # SafeYield disables other windows for the duration, which is exactly the
        # property a bare wx.Yield() lacks.
        wx.SafeYield(self, onlyIfNeeded=True)

    def finish(self) -> None:
        self._disabler = None
        self.Destroy()


def run_with_progress(parent, title: str, work: Callable) -> object:
    """
    Run ``work(progress, cancelled)`` behind a progress dialog.

    ``work`` receives a ``progress(percent, message)`` callable and a
    ``cancelled()`` predicate it is expected to poll.
    """
    dialog = ProgressPanel(parent, title)
    dialog.Show()
    wx.SafeYield(dialog, onlyIfNeeded=True)
    try:
        return work(dialog.update, dialog.cancelled)
    finally:
        dialog.finish()


class VirtualListCtrl(wx.ListCtrl):
    """
    A report-mode list backed by a Python sequence.

    Virtual so a ten-thousand-component design costs nothing to display. v12's
    equivalent inserted real rows and truncated at 100 per board, which meant a
    component past position 100 was simply invisible.
    """

    def __init__(self, parent, columns: Sequence[tuple[str, int]], **kwargs):
        super().__init__(parent, style=wx.LC_REPORT | wx.LC_VIRTUAL | wx.LC_SINGLE_SEL, **kwargs)
        self._rows: list[Sequence[str]] = []
        self._colors: list[Optional[wx.Colour]] = []
        self.theme = get_theme()

        for i, (title, width) in enumerate(columns):
            self.InsertColumn(i, title, width=width)
        self.SetFont(self.theme.body_font())
        apply_input(self)

    def set_rows(self, rows: list[Sequence[str]], colors: Optional[list[Optional[wx.Colour]]] = None) -> None:
        self._rows = rows
        self._colors = colors or [None] * len(rows)
        self.SetItemCount(len(rows))
        if rows:
            self.EnsureVisible(0)
        self.Refresh()

    def OnGetItemText(self, item: int, col: int) -> str:
        try:
            return str(self._rows[item][col])
        except (IndexError, TypeError):
            return ""

    def OnGetItemAttr(self, item: int):
        color = self._colors[item] if item < len(self._colors) else None
        if color is None:
            return None
        attr = wx.ItemAttr()
        attr.SetTextColour(color)
        return attr

    def selected_index(self) -> int:
        return self.GetFirstSelected()

    def select(self, index: int) -> None:
        if 0 <= index < len(self._rows):
            self.Select(index)
            self.Focus(index)
            self.EnsureVisible(index)

    def retheme(self) -> None:
        self.theme = get_theme()
        apply_input(self)
        self.SetFont(self.theme.body_font())
