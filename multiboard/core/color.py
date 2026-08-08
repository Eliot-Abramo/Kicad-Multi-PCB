"""
Colour maths, kept free of wx so it can be unit-tested on a headless runner.

The theme layer depends on these to guarantee two things v12 got wrong:

* Every accent colour meets WCAG AA against the background it is actually drawn
  on, in both light and dark mode. v12 hardcoded a light-mode palette, so its
  greys and pastels were invisible on a dark background.
* Row tints are produced by blending an accent *into the current background*
  rather than being fixed pastels. ``#FFF3E0`` reads as a warm cream on white
  and as nothing at all on ``#303030``.
"""

import colorsys

RGB = tuple[int, int, int]

MIN_CONTRAST_AA = 4.5
"""WCAG 2.1 AA for normal-size text."""

MIN_CONTRAST_AA_LARGE = 3.0
"""WCAG 2.1 AA for large or bold text, and for non-text indicators."""


def _channel_to_linear(c: float) -> float:
    c = c / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(rgb: RGB) -> float:
    """Relative luminance per WCAG 2.1, 0.0 (black) to 1.0 (white)."""
    r, g, b = (_channel_to_linear(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: RGB, b: RGB) -> float:
    """WCAG contrast ratio between two colours, 1.0 to 21.0."""
    la, lb = luminance(a), luminance(b)
    lo, hi = min(la, lb), max(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def is_dark(rgb: RGB) -> bool:
    """Whether a colour is dark enough to want light text on it."""
    return luminance(rgb) < 0.5


def blend(a: RGB, b: RGB, t: float) -> RGB:
    """Linear blend, ``t=0`` gives ``a`` and ``t=1`` gives ``b``."""
    t = min(1.0, max(0.0, t))
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


def tint(background: RGB, accent: RGB, strength: float = 0.14) -> RGB:
    """
    A background subtly shifted toward an accent.

    Used for row highlighting. Because it starts from the *actual* background it
    produces a warm cream on light and a warm charcoal on dark, both legible,
    from one call site.
    """
    return blend(background, accent, strength)


def ensure_contrast(fg: RGB, bg: RGB, minimum: float = MIN_CONTRAST_AA) -> RGB:
    """
    Lighten or darken ``fg`` until it meets ``minimum`` against ``bg``.

    A safety net for colours that are computed rather than chosen -- notably the
    automatic per-board colours, which are generated from a name hash and so
    cannot be hand-checked.
    """
    if contrast_ratio(fg, bg) >= minimum:
        return fg

    hue, light, sat = colorsys.rgb_to_hls(*(c / 255.0 for c in fg))
    target_lighter = luminance(bg) < 0.5

    best = fg
    for step in range(1, 51):
        delta = step / 50.0 * (1.0 if target_lighter else -1.0)
        nl = min(1.0, max(0.0, light + delta))
        cand = tuple(round(c * 255) for c in colorsys.hls_to_rgb(hue, nl, sat))
        best = cand  # type: ignore[assignment]
        if contrast_ratio(best, bg) >= minimum:
            return best
    return best


def to_hex(rgb: RGB) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def from_hex(value: str) -> RGB:
    """Parse ``#RRGGBB`` or ``RRGGBB``. Returns mid-grey on malformed input."""
    s = value.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        return (128, 128, 128)
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        return (128, 128, 128)


# =============================================================================
# Per-board identity colours
# =============================================================================

# Evenly spaced hues, skipping the yellow-green band where hue steps read as
# nearly the same colour. Ordered so the first few boards are maximally distinct,
# which is what matters -- most projects have three to six boards.
_BOARD_HUES = (205, 145, 25, 275, 340, 185, 60, 240, 110, 315)


def board_color(name: str, *, dark_mode: bool, index: int = -1) -> RGB:
    """
    A stable, distinguishable colour for a board.

    Board colour is how placement becomes readable at a glance: you learn
    "Power is teal" once and then recognise it in the component list, the board
    list, and every report without reading any text.

    ``index`` (a board's position in the sorted list) spaces the first boards
    apart deliberately; the name hash is the fallback so a colour survives a
    board being added or removed.
    """
    if index is not None and 0 <= index < len(_BOARD_HUES):
        hue = _BOARD_HUES[index]
    else:
        hue = _BOARD_HUES[_stable_hash(name) % len(_BOARD_HUES)]

    # Dark backgrounds need lighter, less saturated accents to stay readable;
    # light backgrounds need the opposite.
    lightness, saturation = (0.72, 0.55) if dark_mode else (0.38, 0.62)
    rgb = tuple(round(c * 255) for c in colorsys.hls_to_rgb(hue / 360.0, lightness, saturation))
    return rgb  # type: ignore[return-value]


def _stable_hash(text: str) -> int:
    """
    FNV-1a. Python's ``hash()`` is randomised per process, which would give a
    board a different colour on every launch.
    """
    h = 0x811C9DC5
    for ch in text.encode("utf-8"):
        h = ((h ^ ch) * 0x01000193) & 0xFFFFFFFF
    return h
