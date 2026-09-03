# Copyright (c) 2026 Eliot Abramo
# SPDX-License-Identifier: MIT

"""
The architectural rule, enforced.

``multiboard.core`` must never import ``pcbnew`` or ``wx``. That single
constraint is what makes the CLI runnable in CI and what will make the KiCad 11
port a bounded change rather than a rewrite -- so it is checked mechanically
rather than by convention.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "multiboard" / "core"
FORBIDDEN_IN_CORE = {"pcbnew", "wx"}


def _imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0], node.lineno
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.split(".")[0], node.lineno


@pytest.mark.parametrize("path", sorted(CORE.glob("*.py")), ids=lambda p: p.name)
def test_core_never_imports_kicad_or_wx(path):
    offenders = [(mod, line) for mod, line in _imports(path) if mod in FORBIDDEN_IN_CORE]
    assert not offenders, (
        f"{path.relative_to(ROOT)} imports {offenders}. core/ must stay pure so the CLI runs without KiCad."
    )


@pytest.mark.parametrize("path", sorted(CORE.glob("*.py")), ids=lambda p: p.name)
def test_core_never_imports_the_backend(path):
    offenders = [(mod, line) for mod, line in _imports(path) if mod in {"backend", "ui"}]
    assert not offenders, f"{path.relative_to(ROOT)} reaches into {offenders}"


def test_cli_imports_without_pcbnew():
    """The guarantee the CI recipe depends on."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "import sys; sys.modules['pcbnew']=None; import multiboard.cli; print('ok')"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_package_import_survives_absent_pcbnew():
    """KiCad imports this package; so do the CLI and the tests."""
    import multiboard

    assert multiboard.__version__


def test_core_modules_have_module_docstrings():
    for path in sorted(CORE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert ast.get_docstring(tree), f"{path.name} has no module docstring"
