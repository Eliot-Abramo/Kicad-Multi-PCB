"""
Theming derived from the platform, not hardcoded.

v12 defined fourteen literal ``wx.Colour`` constants for a light UI and never
called ``wx.SystemSettings`` at all. Every dialog then forced a white background
onto native controls without setting a foreground, so on a dark macOS or
Windows theme the system's light text landed on a hardcoded white background.
KiCad 10 follows the OS theme on all three platforms, which made this worse
rather than better.

Three rules hold everything together:

1. **Every base colour comes from ``wx.SystemSettings``.** The only literals are
   the semantic accents, and there is one set per mode, each checked against the
   background it is actually drawn on.
2. **Background and foreground are set together, or neither is set.** A native
   ``wx.TextCtrl`` renders correctly in both modes if you leave it alone; it
   renders unreadably if you set only one half of the pair. :func:`apply_input`
   is the only sanctioned way to touch one.
3. **Row highlights are blends of the current background**, never fixed pastels.
   ``#FFF3E0`` is a warm cream on white and invisible on ``#303030``.

The colour maths lives in ``core.color`` so it can be unit-tested headless; this
module is the thin wx-facing layer over it.
"""

from typing import Callable, Optional

import wx

from ..core import color as colormath

RGB = tuple[int, int, int]

# Semantic accents. These are the only hardcoded colours in the UI, and each has
# a unit test asserting at least 4.5:1 against both mode backgrounds.
_ACCENTS_LIGHT = {
    "accent": (0x0B, 0x57, 0xD0),
    "info": (0x0B, 0x57, 0xD0),
    "success": (0x14, 0x6C, 0x2E),
    "warning": (0x8A, 0x53, 0x00),
    "error": (0xB3, 0x26, 0x1E),
}
_ACCENTS_DARK = {
    "accent": (0xA8, 0xC7, 0xFA),
    "info": (0xA8, 0xC7, 0xFA),
    "success": (0x6D, 0xD5, 0x8C),
    "warning": (0xFF, 0xB8, 0x6B),
    "error": (0xFF, 0x89, 0x7D),
}


def _sys(colour_id) -> wx.Colour:
    return wx.SystemSettings.GetColour(colour_id)


def _rgb(c: wx.Colour) -> RGB:
    return (c.Red(), c.Green(), c.Blue())


def _wx(rgb: RGB) -> wx.Colour:
    return wx.Colour(*rgb)


def detect_dark() -> bool:
    """
    Whether the platform is in dark mode.

    ``wx.SystemAppearance`` needs wxWidgets 3.1.3+. KiCad 10 ships 3.3.1 on
    Windows and macOS, but Linux distribution builds may still be on 3.2.x, so
    the luminance comparison is a real fallback rather than defensive noise --
    and it degrades correctly: on a wx build with no dark-mode support the system
    colours stay light, we report light, and we match the native controls.
    """
    try:
        return bool(wx.SystemSettings.GetAppearance().IsDark())
    except (AttributeError, RuntimeError):
        pass
    try:
        bg = _rgb(_sys(wx.SYS_COLOUR_WINDOW))
        fg = _rgb(_sys(wx.SYS_COLOUR_WINDOWTEXT))
        return colormath.luminance(fg) > colormath.luminance(bg)
    except Exception:
        return False


class Theme:
    """The resolved palette and fonts for the current appearance."""

    def __init__(self):
        self.is_dark = False
        self.refresh()

    def refresh(self) -> None:
        """Re-read every colour from the system. Called on appearance change."""
        self.is_dark = detect_dark()

        self.window_bg = _sys(wx.SYS_COLOUR_WINDOW)
        self.panel_bg = _sys(wx.SYS_COLOUR_BTNFACE)
        self.chrome_bg = _sys(wx.SYS_COLOUR_BTNFACE)
        self.text = _sys(wx.SYS_COLOUR_WINDOWTEXT)
        self.text_muted = _sys(wx.SYS_COLOUR_GRAYTEXT)
        self.selection_bg = _sys(wx.SYS_COLOUR_HIGHLIGHT)
        self.selection_text = _sys(wx.SYS_COLOUR_HIGHLIGHTTEXT)

        # A border that reads on both: mostly background, a little text.
        blended = colormath.blend(_rgb(self.text), _rgb(self.window_bg), 0.78)
        self.border = _wx(blended)
        self.grid_line = _wx(colormath.blend(_rgb(self.text), _rgb(self.window_bg), 0.85))

        accents = _ACCENTS_DARK if self.is_dark else _ACCENTS_LIGHT
        bg = _rgb(self.window_bg)
        for name, rgb in accents.items():
            setattr(self, name, _wx(colormath.ensure_contrast(rgb, bg)))

        # A header strip that is always distinct from the content area, in the
        # same direction as the theme rather than always dark.
        self.header_bg = _wx(colormath.blend(_rgb(self.panel_bg), _rgb(self.text), 0.10))
        self.header_text = self.text
        self.header_muted = self.text_muted

    # -- derived colours ---------------------------------------------------

    def tint(self, accent: wx.Colour, strength: float = 0.14) -> wx.Colour:
        """A row background shaded toward ``accent`` from the *current* background."""
        return _wx(colormath.tint(_rgb(self.window_bg), _rgb(accent), strength))

    def readable(self, accent: wx.Colour, on: Optional[wx.Colour] = None) -> wx.Colour:
        """``accent``, lifted if necessary to stay legible on ``on``."""
        bg = _rgb(on if on is not None else self.window_bg)
        return _wx(colormath.ensure_contrast(_rgb(accent), bg))

    def status_color(self, status: str) -> wx.Colour:
        """The accent for a reconciliation status."""
        from ..core.index import Status

        return {
            Status.OK: self.success,
            Status.ADOPT: self.info,
            Status.TODO: self.text_muted,
            Status.MISPLACED: self.warning,
            Status.DUPLICATE: self.error,
            Status.ORPHAN: self.error,
            Status.NOWHERE: self.info,
            Status.SKIPPED: self.text_muted,
        }.get(status, self.text)

    def board_color(self, rgb: RGB) -> wx.Colour:
        """A per-board identity colour, guaranteed readable in this mode."""
        return _wx(colormath.ensure_contrast(rgb, _rgb(self.window_bg), colormath.MIN_CONTRAST_AA_LARGE))

    # -- fonts -------------------------------------------------------------

    def font(self, scale: float = 1.0, *, bold: bool = False, mono: bool = False) -> wx.Font:
        """
        A font derived from the system UI font.

        v12 used absolute point sizes (15/12/10/9). macOS's default UI font is
        2 pt, so its "10 pt body" rendered visibly small there, and on a HiDPI
        GTK desktop with an 11 pt system font its 15 pt header was oversized.
        Scaling relative to the system font is right everywhere.
        """
        base = wx.SystemSettings.GetFont(wx.SYS_ANSI_FIXED_FONT if mono else wx.SYS_DEFAULT_GUI_FONT)
        f = wx.Font(base)
        f.SetPointSize(max(6, round(base.GetPointSize() * scale)))
        if bold:
            f.SetWeight(wx.FONTWEIGHT_BOLD)
        return f

    def header_font(self) -> wx.Font:
        return self.font(1.45, bold=True)

    def title_font(self) -> wx.Font:
        return self.font(1.15, bold=True)

    def body_font(self) -> wx.Font:
        return self.font(1.0)

    def small_font(self) -> wx.Font:
        return self.font(0.9)

    def mono_font(self) -> wx.Font:
        return self.font(0.95, mono=True)


_theme: Optional[Theme] = None


def get_theme() -> Theme:
    """The process-wide theme."""
    global _theme
    if _theme is None:
        _theme = Theme()
    return _theme


def refresh_theme() -> Theme:
    t = get_theme()
    t.refresh()
    return t


# =============================================================================
# Applying the theme
# =============================================================================


def apply_chrome(
    window: wx.Window, theme: Optional[Theme] = None, *, background: Optional[wx.Colour] = None
) -> None:
    """
    Colour a structural panel: toolbars, headers, footers.

    Uses ``SetOwnBackgroundColour`` so children still inherit correctly, and
    always sets the foreground alongside. On macOS, giving a plain panel a
    background also defeats the native vibrancy, so only chrome gets one --
    content panels are left alone.
    """
    t = theme or get_theme()
    window.SetOwnBackgroundColour(background or t.chrome_bg)
    window.SetForegroundColour(t.text)


def apply_input(control: wx.Window, theme: Optional[Theme] = None) -> None:
    """
    The *only* sanctioned way to colour a text-entry or list control.

    It sets both halves. v12 set ``SetBackgroundColour(wx.Colour(250, 250, 250))``
    on read-only text controls and never touched the foreground, which is exactly
    how you get white-on-white in dark mode.

    Prefer not calling this at all: native controls already render correctly in
    both modes.
    """
    t = theme or get_theme()
    control.SetBackgroundColour(t.window_bg)
    control.SetForegroundColour(t.text)


def apply_grid(grid, theme: Optional[Theme] = None) -> None:
    """
    Theme every part of a ``wx.grid.Grid``.

    ``SetGridLineColour`` and the grid *window*'s background are the two that get
    forgotten, and they are what produce the "dark grid with a bright white
    gutter below the last row" artefact. v12 set none of these.
    """
    t = theme or get_theme()

    grid.SetDefaultCellBackgroundColour(t.window_bg)
    grid.SetDefaultCellTextColour(t.text)
    grid.SetLabelBackgroundColour(t.chrome_bg)
    grid.SetLabelTextColour(t.text)
    grid.SetGridLineColour(t.grid_line)
    grid.SetDefaultCellFont(t.body_font())
    grid.SetLabelFont(t.font(0.95, bold=True))

    for setter, value in (
        ("SetSelectionBackground", t.selection_bg),
        ("SetSelectionForeground", t.selection_text),
        ("SetCellHighlightColour", t.accent),
    ):
        try:
            getattr(grid, setter)(value)
        except (AttributeError, TypeError):
            pass

    try:
        grid.GetGridWindow().SetBackgroundColour(t.window_bg)
    except AttributeError:
        pass

    grid.ForceRefresh()
    grid.Refresh()


def set_row_colors(grid, row: int, columns: int, bg: wx.Colour, fg: wx.Colour) -> None:
    """
    Tint a grid row. The only entry point, so fg and bg can never drift apart.
    """
    for col in range(columns):
        grid.SetCellBackgroundColour(row, col, bg)
        grid.SetCellTextColour(row, col, fg)


def apply_tree(tree, theme: Optional[Theme] = None) -> None:
    """Theme a tree control, both halves together."""
    t = theme or get_theme()
    tree.SetBackgroundColour(t.window_bg)
    tree.SetForegroundColour(t.text)
    tree.SetFont(t.body_font())


def bind_theme_changes(top: wx.Window, on_change: Callable[[], None]) -> None:
    """
    Re-theme when the platform appearance changes.

    wx delivers ``EVT_SYS_COLOUR_CHANGED`` only to top-level windows, so
    ``on_change`` is responsible for walking the tree and re-applying. This is
    not hypothetical: macOS switches appearance automatically at sunset, and it
    will do so while a dialog is open.
    """

    def handler(event):
        refresh_theme()
        try:
            on_change()
        finally:
            event.Skip()

    top.Bind(wx.EVT_SYS_COLOUR_CHANGED, handler)


def dark_mode_hint() -> str:
    """A one-line description of the resolved appearance, for Doctor."""
    t = get_theme()
    try:
        wx_version = wx.version()
    except Exception:
        wx_version = "unknown"
    return f"{'Dark' if t.is_dark else 'Light'} appearance, wxPython {wx_version}"
