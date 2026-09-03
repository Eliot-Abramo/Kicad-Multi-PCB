# Copyright (c) 2026 Eliot Abramo
# SPDX-License-Identifier: MIT

"""
The pcbnew API surface, exercised against the shapes a real KiCad returns.

Every case here is a real-KiCad behaviour that differed from the documented one.
The tests use a stub module because pcbnew cannot be imported outside KiCad, but
the stubs reproduce the exact failure observed.
"""

import sys
import types

import pytest


class SwigPyObjectLike:
    """
    An opaque SWIG wrapper.

    ``pcbnew.GetMajorMinorPatchTuple()`` is documented to return a tuple of ints.
    In a real KiCad 10 build it returns a ``SwigPyObject`` wrapping
    ``std::tuple``, which is *not* iterable -- so ``tuple(...)`` raises
    ``TypeError``. Catching only ``AttributeError`` let that escape and the
    plugin died on launch.
    """

    def __iter__(self):
        raise TypeError("'SwigPyObject' object is not iterable")

    def __repr__(self):
        return "<Swig Object of type 'std::tuple< int,int,int > *'>"


@pytest.fixture
def compat(monkeypatch):
    """A fresh compat module bound to a stub pcbnew, with caches cleared."""

    def _build(**attrs):
        mod = types.ModuleType("pcbnew")
        for name, value in attrs.items():
            setattr(mod, name, value)
        monkeypatch.setitem(sys.modules, "pcbnew", mod)

        for stale in [m for m in sys.modules if m.endswith("multiboard.compat")]:
            del sys.modules[stale]
        import multiboard.compat as compat_mod

        compat_mod._pcbnew = None
        compat_mod._version = None
        compat_mod._VERSION_TEXT = None
        compat_mod._probes.clear()
        return compat_mod

    return _build


def test_opaque_swig_tuple_falls_back_to_text(compat):
    """The exact failure seen on KiCad 10.0.x."""
    c = compat(
        GetMajorMinorPatchTuple=lambda: SwigPyObjectLike(),
        GetMajorMinorPatchVersion=lambda: "10.0.5",
    )
    assert c.kicad_version() == (10, 0, 5)


def test_a_real_tuple_is_used_when_offered(compat):
    c = compat(GetMajorMinorPatchTuple=lambda: (10, 0, 5))
    assert c.kicad_version() == (10, 0, 5)


def test_a_list_is_accepted(compat):
    c = compat(GetMajorMinorPatchTuple=lambda: [10, 0, 5])
    assert c.kicad_version() == (10, 0, 5)


def test_accessor_that_raises_is_skipped(compat):
    def boom():
        raise RuntimeError("not available in this build")

    c = compat(GetMajorMinorPatchTuple=boom, GetBuildVersion=lambda: "10.0.5-abc")
    assert c.kicad_version() == (10, 0, 5)


def test_a_string_returning_tuple_accessor_is_not_mistaken_for_digits(compat):
    """tuple("10.0.5") would give ('1','0','.',...) -- int() must reject it."""
    c = compat(
        GetMajorMinorPatchTuple=lambda: "10.0.5",
        GetMajorMinorPatchVersion=lambda: "10.0.5",
    )
    assert c.kicad_version() == (10, 0, 5)


def test_version_falls_through_every_textual_accessor(compat):
    c = compat(FullVersion=lambda: "10.0.5-1-gabc (KiCad)")
    assert c.kicad_version() == (10, 0, 5)


def test_unknown_version_is_empty_not_zero(compat):
    c = compat()
    assert c.kicad_version() == c.UNKNOWN_VERSION
    assert not c.kicad_version()


def test_unknown_version_does_not_block_the_plugin(compat):
    """
    pcbnew imported, so we are inside some KiCad. Refusing to run on a version
    we merely failed to parse would be worse than proceeding.
    """
    c = compat()
    c.require_supported()  # must not raise


def test_supported_version_passes(compat):
    c = compat(GetMajorMinorPatchVersion=lambda: "10.0.5")
    c.require_supported()


def test_kicad_9_is_rejected_with_a_readable_message(compat):
    c = compat(GetMajorMinorPatchVersion=lambda: "9.0.9")
    with pytest.raises(RuntimeError, match=r"requires KiCad 10\.0 or newer"):
        c.require_supported()


def test_kicad_11_is_rejected_because_swig_is_gone(compat):
    c = compat(GetMajorMinorPatchVersion=lambda: "11.0.0")
    with pytest.raises(RuntimeError, match="SWIG"):
        c.require_supported()


def test_version_text_is_kept_for_diagnostics(compat):
    c = compat(GetBuildVersion=lambda: "10.0.5-1-gabcdef")
    c.kicad_version()
    assert "10.0.5" in c.kicad_version_text()


def test_version_is_cached(compat):
    calls = []

    def once():
        calls.append(1)
        return "10.0.5"

    c = compat(GetMajorMinorPatchVersion=once)
    c.kicad_version()
    c.kicad_version()
    assert len(calls) == 1


def test_inproc_hint_reports_no_version_rather_than_zero(compat):
    """An unknown version must not read as "KiCad 0" to the discovery layer."""
    c = compat()
    assert c.inproc_hint().version is None


def test_capabilities_probe_never_raises(compat):
    c = compat(NewBoard=lambda p: None)
    caps = c.capabilities()
    assert caps["NewBoard"] is True
    assert caps["FocusOnItem"] is False
