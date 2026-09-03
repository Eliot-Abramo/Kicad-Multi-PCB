"""
Single source of truth for every version string in the project.

Nothing else in this repository may hardcode a version. ``tools/check_version.py``
validates ``metadata.json`` and the README against these values in CI, which is
what stops the four-way disagreement that v12 shipped with (README said "9.0+",
metadata.json said "8.0", ``__version__`` said "12.0", and generated files
claimed "9.0" and "10.0" in different places).
"""

__version__ = "2.0.0"

CONFIG_SCHEMA = 3
"""
Version of the ``.kicad_multiboard.json`` schema.

Deliberately decoupled from ``__version__``. v12 conflated the two, which meant
the config file version bumped on every plugin release whether or not the schema
had changed, and migration could never be keyed on it.
"""

MIN_KICAD = (10, 0)
"""Lowest supported KiCad major.minor."""

MAX_KICAD = (10, 99)
"""
Highest supported KiCad major.minor.

KiCad 11 removes the SWIG ``pcbnew`` bindings entirely (they were already dropped
from master in March 2026), so this plugin cannot load there. ``metadata.json``
carries the matching ``kicad_version_max`` so PCM never offers it to an 11 user.
"""


def version_tuple() -> tuple:
    """``__version__`` as a tuple of ints, for comparisons."""
    return tuple(int(p) for p in __version__.split("."))


def kicad_supported(version: tuple) -> bool:
    """True if a ``(major, minor, patch)`` KiCad version is in range."""
    return MIN_KICAD <= version[:2] <= MAX_KICAD
