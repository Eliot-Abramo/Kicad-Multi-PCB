"""
Accent legibility, checked arithmetically so it holds on platforms we cannot test.

The accents in ``ui/theme.py`` are the only hardcoded colours left in the UI.
Duplicating them here rather than importing keeps the test runnable without wx,
so it runs on every CI runner -- and it fails loudly if the two ever drift.
"""

import pytest

from multiboard.core.color import (
    MIN_CONTRAST_AA,
    MIN_CONTRAST_AA_LARGE,
    contrast_ratio,
    ensure_contrast,
    tint,
)

ACCENTS_LIGHT = {
    "accent": (0x0B, 0x57, 0xD0),
    "success": (0x14, 0x6C, 0x2E),
    "warning": (0x8A, 0x53, 0x00),
    "error": (0xB3, 0x26, 0x1E),
}
ACCENTS_DARK = {
    "accent": (0xA8, 0xC7, 0xFA),
    "success": (0x6D, 0xD5, 0x8C),
    "warning": (0xFF, 0xB8, 0x6B),
    "error": (0xFF, 0x89, 0x7D),
}

# The plausible extremes of each mode's window background. Light mode ranges from
# pure white to a GTK-ish off-white; dark mode from pure black to the lightest
# dark-mode surface any of the three platforms uses.
LIGHT_BACKGROUNDS = [(255, 255, 255), (250, 250, 250), (240, 240, 240)]
DARK_BACKGROUNDS = [(0, 0, 0), (30, 30, 30), (48, 48, 48), (60, 60, 60)]


def test_theme_accents_match_this_test():
    """Guard against the palette drifting away from what is verified here."""
    pytest.importorskip("wx")
    from multiboard.ui import theme

    assert {k: v for k, v in theme._ACCENTS_LIGHT.items() if k != "info"} == ACCENTS_LIGHT
    assert {k: v for k, v in theme._ACCENTS_DARK.items() if k != "info"} == ACCENTS_DARK


@pytest.mark.parametrize("name,rgb", sorted(ACCENTS_LIGHT.items()))
@pytest.mark.parametrize("bg", LIGHT_BACKGROUNDS)
def test_light_accents_meet_aa(name, rgb, bg):
    ratio = contrast_ratio(rgb, bg)
    assert ratio >= MIN_CONTRAST_AA, f"{name} on {bg} is only {ratio:.2f}:1"


@pytest.mark.parametrize("name,rgb", sorted(ACCENTS_DARK.items()))
@pytest.mark.parametrize("bg", DARK_BACKGROUNDS)
def test_dark_accents_meet_aa(name, rgb, bg):
    ratio = contrast_ratio(rgb, bg)
    assert ratio >= MIN_CONTRAST_AA, f"{name} on {bg} is only {ratio:.2f}:1"


@pytest.mark.parametrize("accent", list(ACCENTS_LIGHT.values()))
@pytest.mark.parametrize("bg", LIGHT_BACKGROUNDS + DARK_BACKGROUNDS)
def test_tinted_rows_stay_close_to_their_background(accent, bg):
    """
    A row tint must read as "this row is highlighted", not as a colour block --
    and the text colour drawn over it must still work.
    """
    tinted = tint(bg, accent, 0.14)
    assert contrast_ratio(tinted, bg) < 1.6, "tint is too strong to read as a highlight"


@pytest.mark.parametrize("bg", LIGHT_BACKGROUNDS + DARK_BACKGROUNDS)
def test_text_remains_legible_on_a_tinted_row(bg):
    from multiboard.core.color import is_dark

    text = (255, 255, 255) if is_dark(bg) else (0, 0, 0)
    for accent in list(ACCENTS_LIGHT.values()) + list(ACCENTS_DARK.values()):
        tinted = tint(bg, accent, 0.14)
        assert contrast_ratio(text, tinted) >= MIN_CONTRAST_AA


@pytest.mark.parametrize("bg", DARK_BACKGROUNDS)
def test_light_mode_accents_would_fail_on_dark(bg):
    """
    Documents *why* two palettes exist: v12's single light palette is
    unreadable in dark mode, which is the bug being fixed.
    """
    failures = [n for n, rgb in ACCENTS_LIGHT.items() if contrast_ratio(rgb, bg) < MIN_CONTRAST_AA]
    assert failures, "if this passes, the two-palette design is unnecessary"


def test_ensure_contrast_rescues_any_board_colour():
    """Board colours are generated from a hash, so they cannot be hand-checked."""
    from multiboard.core.color import board_color

    for bg in LIGHT_BACKGROUNDS + DARK_BACKGROUNDS:
        from multiboard.core.color import is_dark

        for i in range(12):
            c = board_color(f"Board{i}", dark_mode=is_dark(bg), index=i)
            fixed = ensure_contrast(c, bg, MIN_CONTRAST_AA_LARGE)
            assert contrast_ratio(fixed, bg) >= MIN_CONTRAST_AA_LARGE
