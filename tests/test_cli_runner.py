# Copyright (c) 2026 Eliot Abramo
# SPDX-License-Identifier: MIT

"""
Running kicad-cli without freezing the editor.

Every invocation here used to be a ``subprocess.run`` on KiCad's GUI thread. A
netlist export runs on every refresh and takes seconds; a DRC takes minutes. For
all of it the window was frozen, the Cancel button was dead, and the operating
system offered to kill the application.

``sys.executable`` stands in for kicad-cli so these run identically on all three
platforms without shipping a fixture script.
"""

import sys
import time
from pathlib import Path

import pytest

from multiboard.core.cli_runner import CliUnavailable, run_cli
from multiboard.core.kicad_env import KicadInstall

PYTHON = KicadInstall(version=(10, 0), cli=Path(sys.executable))


def script(body: str) -> list:
    return ["-c", body]


SLEEPER = script("import time\nfor _ in range(40): time.sleep(0.05)")


def test_returns_output_and_exit_code(tmp_path):
    result = run_cli(PYTHON, script("print('hello'); raise SystemExit(0)"), cwd=tmp_path)
    assert result.ok
    assert result.stdout.strip() == "hello"
    assert result.returncode == 0


def test_non_zero_exit_is_a_failure_with_the_tool_s_own_words(tmp_path):
    result = run_cli(PYTHON, script("import sys; sys.stderr.write('boom\\n'); sys.exit(3)"), cwd=tmp_path)
    assert not result.ok
    assert result.returncode == 3
    assert "boom" in result.failure_text()


def test_ok_codes_lets_drc_report_violations_without_failing(tmp_path):
    """``pcb drc --exit-code-violations`` exits non-zero by design."""
    result = run_cli(PYTHON, script("raise SystemExit(5)"), cwd=tmp_path, ok_codes=(0, 5))
    assert result.ok and result.returncode == 5


def test_pump_is_called_repeatedly_while_the_child_runs(tmp_path):
    """This is the hook the UI uses to keep painting and stay cancellable."""
    seen = []
    result = run_cli(PYTHON, SLEEPER, cwd=tmp_path, pump=lambda elapsed: seen.append(elapsed) or False)

    assert result.ok
    assert len(seen) > 5, "the pump must fire many times across a two-second run"
    assert seen == sorted(seen), "elapsed time must be monotonic"


def test_pump_returning_true_cancels_promptly(tmp_path):
    start = time.monotonic()
    result = run_cli(PYTHON, SLEEPER, cwd=tmp_path, pump=lambda elapsed: elapsed > 0.2)

    assert result.cancelled
    assert not result.ok
    assert result.failure_text() == "Cancelled."
    assert time.monotonic() - start < 2.0, "cancel must not wait for the child to finish"


def test_timeout_kills_the_child(tmp_path):
    start = time.monotonic()
    result = run_cli(PYTHON, SLEEPER, cwd=tmp_path, timeout=0.2)

    assert result.timed_out and not result.ok
    assert "timed out" in result.failure_text()
    assert time.monotonic() - start < 2.0


def test_large_output_does_not_deadlock(tmp_path):
    """
    A poller that leaves pipes undrained hangs the moment a buffer fills.

    Real DRC reports are large enough to hit that, so output is spooled to a
    temporary file instead of a pipe. Without it this test never returns.
    """
    result = run_cli(
        PYTHON,
        script("import sys\nfor _ in range(200000): sys.stdout.write('x' * 80 + '\\n')"),
        cwd=tmp_path,
        timeout=60,
        pump=lambda _elapsed: False,
    )
    assert result.ok
    assert len(result.stdout) > 10_000_000


def test_missing_cli_raises_something_actionable(tmp_path):
    with pytest.raises(CliUnavailable, match="KICAD_CLI"):
        run_cli(None, ["version"], cwd=tmp_path)


def test_child_does_not_inherit_kicad_s_python_environment(tmp_path, monkeypatch):
    """
    KiCad's embedded Python exports PYTHONHOME and PYTHONPATH.

    A child kicad-cli that inherits them initialises against the wrong
    interpreter and dies with an opaque error -- a recurring failure mode for
    KiCad plugins, and invisible unless stderr is captured.
    """
    monkeypatch.setenv("PYTHONHOME", "/nonexistent/kicad/python")
    monkeypatch.setenv("PYTHONPATH", "/nonexistent/kicad/lib")

    result = run_cli(PYTHON, script("import os; print(os.environ.get('PYTHONHOME', 'unset'))"), cwd=tmp_path)
    assert result.ok, result.failure_text()
    assert result.stdout.strip() == "unset"
