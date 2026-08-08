"""
DRC and fabrication output across every board.

Multi-board projects need per-board fab output and a way to check every board at
once. v12 had a DRC pass but discarded kicad-cli's exit code, deleted the report
on the success path only, and filtered port nets with a substring test on the
violation description.

New in KiCad 10 and used here: ``--exit-code-violations`` (a real CI signal) and
``kicad-cli pcb export stats``. ``--schematic-parity`` becomes meaningful in a
multi-board project because every board directory holds a linked copy of the
root schematic.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..constants import WORK_DIR
from .cli_runner import CliResult, run_cli
from .config import BoardConfig, ProjectConfig

DRC_VIOLATIONS_EXIT = (5,)
"""kicad-cli exits non-zero when --exit-code-violations finds something."""


@dataclass
class Violation:
    """One DRC finding."""

    type: str
    severity: str
    description: str
    board: str = ""

    @property
    def is_unconnected(self) -> bool:
        return "unconnected" in self.type.lower()


@dataclass
class DrcResult:
    """DRC outcome for one board."""

    board: str
    ok: bool = False
    violations: list[Violation] = field(default_factory=list)
    filtered: int = 0
    error: str = ""
    report_path: Optional[Path] = None

    @property
    def count(self) -> int:
        return len(self.violations)

    def by_severity(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for v in self.violations:
            out[v.severity] = out.get(v.severity, 0) + 1
        return out


def drc_dir(root: Path) -> Path:
    d = root / WORK_DIR / "drc"
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_drc(
    install,
    root: Path,
    board_name: str,
    pcb: Path,
    *,
    ports: Optional[list[str]] = None,
    parity: bool = False,
    timeout: float = 300.0,
) -> DrcResult:
    """
    Run DRC on one board and parse the JSON report.

    Nets that leave the board through a declared port are expected to look
    unconnected, so those violations are filtered out -- but the count of what
    was filtered is reported, rather than the violations silently disappearing.
    """
    result = DrcResult(board=board_name)
    out = drc_dir(root) / f"{board_name}.drc.json"
    out.unlink(missing_ok=True)

    args = ["pcb", "drc", "--format", "json", "-o", str(out), "--severity-all"]
    if parity:
        args.append("--schematic-parity")
    args.append(str(pcb))

    cli: CliResult = run_cli(install, args, cwd=root, timeout=timeout, ok_codes=(0, *DRC_VIOLATIONS_EXIT))
    if not cli.ok and not out.exists():
        result.error = cli.failure_text()
        return result

    try:
        data = json.loads(out.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        result.error = f"Could not read the DRC report: {exc}"
        return result

    port_nets = {p.lower() for p in (ports or []) if p}
    for raw in data.get("violations", []):
        violation = Violation(
            type=str(raw.get("type", "")),
            severity=str(raw.get("severity", "error")),
            description=str(raw.get("description", "")),
            board=board_name,
        )
        if violation.is_unconnected and _mentions_port(violation.description, port_nets):
            result.filtered += 1
            continue
        result.violations.append(violation)

    result.ok = True
    result.report_path = out
    return result


def _mentions_port(description: str, port_nets: set) -> bool:
    """
    Whether an unconnected-net violation concerns a declared port.

    Matched on word boundaries. A substring test -- which is what v12 used --
    lets a port named ``D`` suppress every violation mentioning any net whose
    name contains the letter d.
    """
    if not port_nets:
        return False
    import re

    tokens = set(re.findall(r"[A-Za-z0-9_+\-./]+", description.lower()))
    return bool(tokens & port_nets)


def run_drc_all(
    install,
    root: Path,
    cfg: ProjectConfig,
    *,
    parity: bool = False,
    progress: Optional[Callable[[int, str], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
) -> dict[str, DrcResult]:
    """DRC every board. Boards that error are reported, never omitted."""
    out: dict[str, DrcResult] = {}
    boards = list(cfg.boards.items())

    for i, (name, board) in enumerate(boards):
        if progress:
            progress(int(100 * i / max(len(boards), 1)), f"Checking {name}...")
        if cancel and cancel():
            break

        pcb = root / board.pcb_path if board.pcb_path else None
        if pcb is None or not pcb.exists():
            out[name] = DrcResult(board=name, error="PCB not found")
            continue

        ports = [p.effective_net() for p in board.ports.values()]
        out[name] = run_drc(install, root, name, pcb, ports=ports, parity=parity)

    if progress:
        progress(100, "Done")
    return out


def summarize_drc(results: dict[str, DrcResult]) -> str:
    total = sum(r.count for r in results.values())
    errored = [r.board for r in results.values() if r.error]
    parts = [f"{total} violation(s) across {len(results)} board(s)"]
    if errored:
        parts.append(f"{len(errored)} board(s) could not be checked")
    return "; ".join(parts)


def format_drc(results: dict[str, DrcResult], *, limit: int = 25) -> str:
    """Plain-text DRC report for the CLI and the report dialog."""
    lines = [summarize_drc(results), ""]
    for name in sorted(results):
        r = results[name]
        if r.error:
            lines.append(f"{name}: ERROR - {r.error}")
            continue
        note = f" ({r.filtered} port net(s) filtered)" if r.filtered else ""
        lines.append(f"{name}: {r.count} violation(s){note}")
        for v in r.violations[:limit]:
            lines.append(f"    [{v.severity}] {v.type}: {v.description}")
        if r.count > limit:
            lines.append(f"    ... and {r.count - limit} more (see {r.report_path})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# =============================================================================
# Fabrication output
# =============================================================================


def default_jobset() -> dict:
    """
    A starter ``.kicad_jobset``: gerbers, drill, BOM, position, and a 3D model.

    Written into a board directory the first time fab output is requested, then
    owned by the user and never overwritten -- so tuning it is a normal thing to
    do rather than something the plugin fights.
    """
    return {
        "meta": {"version": 1},
        "jobs": [
            {"type": "pcb_export_gerbers", "description": "Gerbers", "settings": {}},
            {"type": "pcb_export_drill", "description": "Drill files", "settings": {}},
            {"type": "pcb_export_pos", "description": "Placement", "settings": {}},
            {"type": "sch_export_bom", "description": "BOM", "settings": {}},
            {"type": "pcb_export_step", "description": "3D model", "settings": {}},
        ],
        "outputs": [],
    }


def jobset_path(root: Path, board: BoardConfig) -> Optional[Path]:
    d = (root / board.pcb_path).parent if board.pcb_path else None
    return (d / "fabrication.kicad_jobset") if d else None


def ensure_jobset(root: Path, board: BoardConfig) -> Optional[Path]:
    """Create the starter jobset if the board has none. Never overwrites."""
    path = jobset_path(root, board)
    if path is None:
        return None
    if not path.exists():
        from .cache import atomic_write_json

        atomic_write_json(path, default_jobset(), backup=False)
    return path


def run_fab(
    install,
    root: Path,
    board: BoardConfig,
    *,
    jobset: Optional[Path] = None,
    out_dir: Optional[Path] = None,
    timeout: float = 900.0,
) -> CliResult:
    """Build fabrication output for one board via ``kicad-cli jobset run``."""
    pcb = root / board.pcb_path
    project = pcb.with_suffix(".kicad_pro")
    job = jobset or ensure_jobset(root, board)
    destination = out_dir or (root / WORK_DIR / "fab" / board.name)
    destination.mkdir(parents=True, exist_ok=True)

    args = ["jobset", "run", "--file", str(job), "--output", str(destination), str(project)]
    return run_cli(install, args, cwd=root, timeout=timeout)


def board_stats(install, root: Path, pcb: Path, *, timeout: float = 120.0) -> dict:
    """
    Board area, layer count, and component counts.

    ``kicad-cli pcb export stats`` is new in KiCad 10 and gives the health
    report real numbers rather than a footprint count.
    """
    out = drc_dir(root) / f"{pcb.stem}.stats.json"
    out.unlink(missing_ok=True)
    result = run_cli(
        install,
        ["pcb", "export", "stats", "--format", "json", "-o", str(out), str(pcb)],
        cwd=root,
        timeout=timeout,
    )
    if not result.ok or not out.exists():
        return {}
    try:
        return json.loads(out.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
