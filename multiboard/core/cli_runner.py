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
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..constants import CLI_TIMEOUT
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
    error: Optional[str] = None
    ok_codes: tuple[int, ...] = field(default=(0,), repr=False)

    @property
    def ok(self) -> bool:
        return self.returncode in self.ok_codes and not self.timed_out and self.error is None

    def failure_text(self) -> str:
        """A message fit to show a user, preferring kicad-cli's own words."""
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
) -> CliResult:
    """
    Run ``kicad-cli <args>``.

    ``ok_codes`` exists because ``pcb drc --exit-code-violations`` deliberately
    exits non-zero when it finds violations, which is a successful run for our
    purposes. Callers that care about violations read the JSON report.
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
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(cwd),
            timeout=timeout,
            env=child_env(),
            **kwargs,
        )
    except subprocess.TimeoutExpired:
        return CliResult(argv, -1, duration=time.monotonic() - start, timed_out=True, ok_codes=ok_codes)
    except OSError as exc:
        return CliResult(
            argv,
            -1,
            duration=time.monotonic() - start,
            error=f"Could not run kicad-cli: {exc}",
            ok_codes=ok_codes,
        )

    return CliResult(
        argv=argv,
        returncode=proc.returncode,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        duration=time.monotonic() - start,
        ok_codes=ok_codes,
    )
