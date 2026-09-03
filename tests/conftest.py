# Copyright (c) 2026 Eliot Abramo
# SPDX-License-Identifier: MIT

"""
Shared fixtures.

The core suite deliberately runs on a bare Python: no pcbnew, no wx, no KiCad.
That is the same guarantee the CLI depends on, so keeping the tests honest here
keeps the CLI honest too. Only backend tests use ``fake_pcbnew``.
"""

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# =============================================================================
# File builders
# =============================================================================


def make_pcb(footprints, *, version: int = 20260206, generator: str = "pcbnew") -> str:
    """
    Build a ``.kicad_pcb`` body from simple dicts.

    Each footprint dict may carry: ref, value, fpid, x, y, rot, layer, uuid,
    path, attrs (list), pads (list of ``(number, net)`` or
    ``(number, netcode, net)`` -- the three-element form emits the pre-20251028
    ``(net 3 "GND")`` shape so both are exercised).
    """
    out = [f'(kicad_pcb\n  (version {version})\n  (generator "{generator}")']
    for fp in footprints:
        out.append(f'  (footprint "{fp.get("fpid", "Lib:Part")}"')
        out.append(f'    (layer "{fp.get("layer", "F.Cu")}")')
        out.append(f'    (uuid "{fp.get("uuid", "u-" + fp.get("ref", "x"))}")')
        out.append(f"    (at {fp.get('x', 0)} {fp.get('y', 0)} {fp.get('rot', 0)})")
        if fp.get("path"):
            out.append(f'    (path "{fp["path"]}")')
        if fp.get("attrs"):
            out.append(f"    (attr {' '.join(fp['attrs'])})")
        if fp.get("legacy_text"):
            out.append(f'    (fp_text reference "{fp.get("ref", "")}" (at 0 -1))')
            out.append(f'    (fp_text value "{fp.get("value", "")}" (at 0 1))')
        else:
            out.append(f'    (property "Reference" "{fp.get("ref", "")}" (at 0 -1 0))')
            out.append(f'    (property "Value" "{fp.get("value", "")}" (at 0 1 0))')
        for pad in fp.get("pads", []):
            if len(pad) == 3:
                number, code, net = pad
                out.append(f'    (pad "{number}" smd rect (at 0 0) (net {code} "{net}"))')
            else:
                number, net = pad
                out.append(f'    (pad "{number}" smd rect (at 0 0) (net "{net}"))')
        out.append("  )")
    out.append(")")
    return "\n".join(out)


def make_netlist(components) -> str:
    """
    Build a kicadxml netlist from simple dicts.

    Emits ``<tstamps>`` (plural), which is what KiCad 6+ actually writes and what
    v12's parser did not look for.
    """
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<export version="E">', "  <components>"]
    for c in components:
        parts.append(f'    <comp ref="{c["ref"]}">')
        parts.append(f"      <value>{c.get('value', '')}</value>")
        if c.get("footprint") is not None:
            parts.append(f"      <footprint>{c.get('footprint', 'Lib:Part')}</footprint>")
        parts.append(f'      <libsource lib="dev" part="{c.get("part", "R")}"/>')
        for name, value in (c.get("properties") or {}).items():
            parts.append(f'      <property name="{name}" value="{value}"/>')
        for name, value in (c.get("fields") or {}).items():
            parts.append(f'      <fields><field name="{name}">{value}</field></fields>')
        parts.append(f'      <sheetpath names="{c.get("sheet", "/")}" tstamps="/"/>')
        parts.append(f"      <tstamps>{c.get('tstamps', '/uuid-' + c['ref'])}</tstamps>")
        parts.append("    </comp>")
    parts.append("  </components>")

    parts.append("  <nets>")
    for name, nodes in (_collect_nets(components)).items():
        parts.append(f'    <net code="1" name="{name}">')
        for ref, pin in nodes:
            parts.append(f'      <node ref="{ref}" pin="{pin}"/>')
        parts.append("    </net>")
    parts.append("  </nets>")
    parts.append("</export>")
    return "\n".join(parts)


def _collect_nets(components):
    nets = {}
    for c in components:
        for pin, net in (c.get("nets") or {}).items():
            nets.setdefault(net, []).append((c["ref"], pin))
    return nets


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A minimal multi-board project skeleton."""
    (tmp_path / "demo.kicad_pro").write_text("{}", encoding="utf-8")
    (tmp_path / "demo.kicad_sch").write_text("(kicad_sch (version 20260306))", encoding="utf-8")
    (tmp_path / "boards").mkdir()
    return tmp_path


@pytest.fixture
def make_board(project):
    """Create ``boards/<name>/<name>.kicad_pcb`` with the given footprints."""

    def _make(name: str, footprints, **kwargs) -> str:
        d = project / "boards" / name
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.kicad_pcb").write_text(make_pcb(footprints, **kwargs), encoding="utf-8")
        return f"boards/{name}/{name}.kicad_pcb"

    return _make


@pytest.fixture
def fake_pcbnew(monkeypatch):
    """
    A stub ``pcbnew`` for backend tests only.

    Core tests must never request this: if a core module ever grows a pcbnew
    import, ``test_layering`` fails and this fixture must not be able to hide it.
    """
    mod = types.ModuleType("pcbnew")
    mod.GetMajorMinorPatchTuple = lambda: (10, 0, 5)
    mod.GetBuildVersion = lambda: "10.0.5"
    mod.Version = lambda: "10.0.5"

    class _ActionPlugin:
        def register(self):
            return None

    mod.ActionPlugin = _ActionPlugin
    mod.GetBoard = lambda: None
    monkeypatch.setitem(sys.modules, "pcbnew", mod)
    return mod
