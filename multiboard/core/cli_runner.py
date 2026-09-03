# Copyright (c) 2026 Eliot Abramo
# SPDX-License-Identifier: MIT

"""
kicad-cli invocation.

v12's ``_run_cli`` never inspected the return code, had no timeout, and decoded
output with the locale codec. The consequences were all silent: a failed netlist
export produced no error, a hung kicad-cli froze KiCad permanently with no way
out, and a UTF-8 byte in the output raised ``UnicodeDecodeError`` from inside
the runner on a cp1252 Windows console.
"""

import os
import subprocess
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..constants import CLI_POLL_INTERVAL, CLI_TIMEOUT
from .kicad_env import KicadInstall, child_env

CREATE_NO_WINDOW = 0x08000000


class CliUnavailable(RuntimeError):
    """No kicad-cli could be located."""


@dataclass
class CliResult:
    """Outcome of one kicad-cli invocation. Never discarded by callers."""

    argv: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    timed_out: bool = False
    cancelled: bool = False
    error: Optional[str] = None
    ok_codes: tuple[int, ...] = field(default=(0,), repr=False)

    @property
    def ok(self) -> bool:
        return (
            self.returncode in self.ok_codes
            and not self.timed_out
            and not self.cancelled
            and self.error is None
        )

    def failure_text(self) -> str:
        """A message fit to show a user, preferring kicad-cli's own words."""
        if self.cancelled:
            return "Cancelled."
        if self.timed_out:
            return f"kicad-cli timed out after {self.duration:.0f}s: {' '.join(self.argv[1:4])}"
        if self.error:
            return self.error
        detail = (self.stderr or self.stdout).strip()
        if detail:
            # kicad-cli is verbose on success paths; the last lines carry the error.
            detail = "\n".join(detail.splitlines()[-6:])
            return f"kicad-cli exited {self.returncode}:\n{detail}"
        return f"kicad-cli exited {self.returncode}"


def run_cli(
    install: Optional[KicadInstall],
    args: Sequence[str],
    *,
    cwd: Path,
    timeout: float = CLI_TIMEOUT,
    ok_codes: tuple[int, ...] = (0,),
    pump: Optional[Callable[[float], bool]] = None,
) -> CliResult:
    """
    Run ``kicad-cli <args>``.

    ``ok_codes`` exists because ``pcb drc --exit-code-violations`` deliberately
    exits non-zero when it finds violations, which is a successful run for our
    purposes. Callers that care about violations read the JSON report.

    ``pump`` is called every :data:`CLI_POLL_INTERVAL` seconds with the elapsed
    time while the child runs; returning True kills it and reports
    ``cancelled``. That is how the plugin stays responsive: a netlist export
    takes seconds and a DRC can take minutes, and ``subprocess.run`` would block
    KiCad's GUI thread for all of it -- frozen window, dead Cancel button, and
    an operating-system "not responding" prompt on a slow project.

    Output goes to temporary files rather than pipes. Polling a process whose
    pipes you are not draining deadlocks as soon as it fills a pipe buffer, and
    a verbose DRC report will; a spool file has no such limit and behaves the
    same on all three platforms.
    """
    if install is None or install.cli is None:
        raise CliUnavailable(
            "kicad-cli could not be located.\n\n"
            "Set the KICAD_CLI environment variable to its full path, or run "
            "Doctor for a per-platform hint."
        )

    argv = [*install.spawn_prefix, str(install.cli), *args]
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = CREATE_NO_WINDOW

    start = time.monotonic()
    try:
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(argv, stdout=out, stderr=err, cwd=str(cwd), env=child_env(), **kwargs)
            timed_out, cancelled = _wait(proc, start, timeout, pump)
            out.seek(0)
            err.seek(0)
            return CliResult(
                argv=argv,
                returncode=proc.returncode if proc.returncode is not None else -1,
                stdout=out.read().decode("utf-8", "replace"),
                stderr=err.read().decode("utf-8", "replace"),
                duration=time.monotonic() - start,
                timed_out=timed_out,
                cancelled=cancelled,
                ok_codes=ok_codes,
            )
    except OSError as exc:
        return CliResult(
            argv,
            -1,
            duration=time.monotonic() - start,
            error=f"Could not run kicad-cli: {exc}",
            ok_codes=ok_codes,
        )


def _wait(proc, start: float, timeout: float, pump) -> tuple[bool, bool]:
    """Poll until the child exits, the timeout expires, or ``pump`` cancels."""
    while proc.poll() is None:
        elapsed = time.monotonic() - start
        if elapsed > timeout:
            _terminate(proc)
            return True, False
        if pump is not None and pump(elapsed):
            _terminate(proc)
            return False, True
        time.sleep(CLI_POLL_INTERVAL)
    return False, False


def _terminate(proc) -> None:
    """Stop a child, escalating to kill. Always leaves it reaped."""
    for stop in (proc.terminate, proc.kill):
        try:
            stop()
            proc.wait(timeout=5)
            return
        except (OSError, subprocess.SubprocessError):
            continue
