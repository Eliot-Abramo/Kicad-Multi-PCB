# Copyright (c) 2026 Eliot Abramo
# SPDX-License-Identifier: MIT

"""
Pure-Python core of the Multi-Board PCB Manager.

**Nothing in this package may import ``pcbnew`` or ``wx``.**

That rule is enforced by ``tests/test_layering.py``, which AST-walks every module
here. It buys two things:

* The CLI runs anywhere -- CI runners, a bare Python, a container -- because the
  index, search, reconciliation, DRC, and fab paths need only file parsing and
  ``kicad-cli``.
* The eventual KiCad 11 port is a small delta. KiCad 11 removes the SWIG
  ``pcbnew`` bindings entirely, and roughly 85% of this plugin's value lives in
  here, untouched by that.

Everything that must reach into KiCad goes through ``multiboard.backend``.
"""
