#!/usr/bin/env python3
"""
Generate the plugin icons.

Three sizes are needed and they are not interchangeable:

* ``resources/icon.png`` at **exactly 64x64** -- the Plugin and Content Manager
  requires that size and rejects anything else.
* ``multiboard/icons/icon.png`` at 24x24 -- the KiCad toolbar button.
* ``multiboard/icons/icon@2x.png`` at 48x48 -- the HiDPI toolbar button.

The artwork is three offset board outlines with connection points between them,
which reads at 24 px and still says "several boards, linked" at 64 px. Drawn
rather than shipped as a binary so it can be regenerated and reviewed in a diff.

Run: python tools/make_icons.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent

# KiCad-ish green for the boards, a warm copper for the links.
BOARD_FILL = (30, 106, 80)
BOARD_EDGE = (126, 214, 176)
LINK = (240, 176, 84)
PAD = (250, 226, 180)


def draw(size: int) -> Image.Image:
    """Draw the icon at ``size`` px, supersampled 4x for clean edges."""
    scale = 4
    s = size * scale
    image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)

    unit = s / 64.0
    radius = max(2, int(3 * unit))
    edge = max(1, int(1.6 * unit))

    # Three boards, back to front, offset so the stack reads as depth.
    boards = [
        (6, 6, 40, 28),
        (18, 20, 52, 42),
        (10, 36, 44, 58),
    ]

    for i, (x0, y0, x1, y1) in enumerate(boards):
        box = [x0 * unit, y0 * unit, x1 * unit, y1 * unit]
        shade = 1.0 - i * 0.12
        fill = (*tuple(int(c * shade) for c in BOARD_FILL), 255)
        d.rounded_rectangle(box, radius=radius, fill=fill, outline=(*BOARD_EDGE, 255), width=edge)

        # A couple of pads per board so it reads as a PCB, not a card.
        for px, py in ((x0 + 5, y0 + 5), (x1 - 5, y1 - 5)):
            r = 1.6 * unit
            d.ellipse([px * unit - r, py * unit - r, px * unit + r, py * unit + r], fill=(*PAD, 255))

    # Links between the stacked boards: the point of the whole plugin.
    link_width = max(2, int(2.4 * unit))
    for (ax, ay), (bx, by) in (((34, 26), (34, 22)), ((22, 42), (22, 38))):
        d.line([ax * unit, ay * unit, bx * unit, by * unit], fill=(*LINK, 255), width=link_width)
    d.line([34 * unit, 24 * unit, 22 * unit, 40 * unit], fill=(*LINK, 200), width=max(1, int(1.6 * unit)))

    return image.resize((size, size), Image.LANCZOS)


def main() -> None:
    targets = [
        (ROOT / "resources" / "icon.png", 64),
        (ROOT / "multiboard" / "icons" / "icon.png", 24),
        (ROOT / "multiboard" / "icons" / "icon@2x.png", 48),
    ]
    for path, size in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        draw(size).save(path, "PNG", optimize=True)
        print(f"wrote {path.relative_to(ROOT)} ({size}x{size})")


if __name__ == "__main__":
    main()
