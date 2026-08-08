"""
Headless command line.

Imports ``multiboard.core`` and nothing else -- no ``pcbnew``, no ``wx``. That is
what lets it run on a CI runner, in a container, or in a pre-commit hook, and it
is checked by a test rather than left to discipline.

    multiboard where R42
    multiboard check --exit-code-conflicts
    multiboard drc --all --exit-code-violations
    multiboard xref --csv xref.csv

Exit codes are stable so scripts can branch on them:

==  =============================================
0   success
1   usage or runtime error
2   the thing asked for was not found
3   reconciliation conflicts exist
4   DRC violations exist
==  =============================================
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from .core.index import Status
from .core.rules import natural_key
from .core.workspace import Workspace
from .version import __version__

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_FOUND = 2
EXIT_CONFLICTS = 3
EXIT_VIOLATIONS = 4


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_OK

    if args.command == "version":
        print(f"multiboard {__version__}")
        return EXIT_OK

    try:
        ws = Workspace(Path(args.project))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if not ws.is_configured() and args.command not in ("doctor",):
        print(
            f"error: {args.project} is not a multi-board project (no .kicad_multiboard.json found).",
            file=sys.stderr,
        )
        return EXIT_ERROR

    handler = {
        "index": cmd_index,
        "where": cmd_where,
        "xref": cmd_xref,
        "check": cmd_check,
        "drc": cmd_drc,
        "fab": cmd_fab,
        "doctor": cmd_doctor,
        "sync": cmd_sync,
        "boards": cmd_boards,
    }[args.command]

    try:
        return handler(ws, args)
    except KeyboardInterrupt:
        return EXIT_ERROR
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        if args.verbose:
            import traceback

            traceback.print_exc()
        return EXIT_ERROR


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="multiboard",
        description="Manage multiple KiCad PCBs driven by a single schematic.",
    )
    parser.add_argument(
        "--project", "-C", default=".", metavar="DIR", help="project directory (default: current)"
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("index", help="rebuild the component index")
    p.add_argument("--json", action="store_true")
    p.add_argument("--force", action="store_true", help="ignore the cache")
    p.add_argument("--no-export", action="store_true", help="skip the netlist export (board data only)")

    p = sub.add_parser("where", help="find which board a component is on")
    p.add_argument("ref", help="reference designator, e.g. R42")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("xref", help="cross-reference every component")
    p.add_argument("--board", action="append", help="filter by board (repeatable)")
    p.add_argument("--status", action="append", help="filter by status (repeatable)")
    p.add_argument("--query", "-q", default="", help="search query")
    p.add_argument("--csv", metavar="FILE", help="write CSV (- for stdout)")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("check", help="report assignment conflicts")
    p.add_argument("--exit-code-conflicts", action="store_true", help="exit 3 when conflicts exist")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("drc", help="run DRC on boards")
    p.add_argument("--board", action="append")
    p.add_argument("--all", action="store_true")
    p.add_argument("--schematic-parity", action="store_true")
    p.add_argument("--exit-code-violations", action="store_true", help="exit 4 when violations exist")
    p.add_argument("--json", metavar="FILE")

    p = sub.add_parser("fab", help="build fabrication output")
    p.add_argument("--board", action="append")
    p.add_argument("--all", action="store_true")
    p.add_argument("--jobset", metavar="FILE")
    p.add_argument("--out", metavar="DIR")

    p = sub.add_parser("doctor", help="check the project for problems")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("sync", help="preview an update to a board")
    p.add_argument("board")
    p.add_argument(
        "--dry-run", action="store_true", default=True, help="show the plan (board mutation is GUI-only)"
    )
    p.add_argument(
        "--strict", action="store_true", help="only pull components explicitly assigned to this board"
    )
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("boards", help="list boards")
    p.add_argument("--json", action="store_true")

    sub.add_parser("version", help="print the version")
    return parser


def _refresh(ws: Workspace, args, *, quiet: bool = True):
    result = ws.refresh(
        force=getattr(args, "force", False),
        export=not getattr(args, "no_export", False),
    )
    if result.netlist_error and not quiet:
        print(f"warning: {result.netlist_error}", file=sys.stderr)
    return result


# =============================================================================
# Commands
# =============================================================================


def cmd_index(ws: Workspace, args) -> int:
    result = _refresh(ws, args, quiet=False)
    stats = result.stats

    if args.json:
        print(
            json.dumps(
                {
                    "components": stats.components,
                    "placed": stats.placed,
                    "conflicts": stats.conflicts,
                    "boards_scanned": stats.boards_scanned,
                    "boards_cached": stats.boards_cached,
                    "by_status": stats.by_status,
                    "by_board": stats.by_board,
                    "duration_s": round(stats.duration, 3),
                    "warnings": result.warnings,
                },
                indent=2,
            )
        )
        return EXIT_OK

    print(
        f"{stats.components} component(s), {stats.placed} placed, "
        f"{stats.conflicts} conflict(s) in {stats.duration:.2f}s"
    )
    print(f"  boards: {stats.boards_scanned} scanned, {stats.boards_cached} from cache")
    for status, n in sorted(stats.by_status.items(), key=lambda kv: -kv[1]):
        print(f"  {Status.LABELS.get(status, status):<12} {n}")
    for warning in result.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    return EXIT_OK


def cmd_where(ws: Workspace, args) -> int:
    """The question this whole tool exists to answer."""
    _refresh(ws, args)
    rec = ws.index.get(args.ref)

    if rec is None:
        print(f"{args.ref}: not found in this project", file=sys.stderr)
        return EXIT_NOT_FOUND

    if args.json:
        print(json.dumps(ws.index.to_json([rec])[0], indent=2))
        return EXIT_OK

    print(f"{rec.ref}  {rec.value}")
    print(f"  footprint  {rec.footprint or '-'}")
    print(f"  sheet      {rec.sheet or '-'}")
    print(f"  assigned   {rec.intent or '(nothing assigns it)'}" + (f"  [{rec.why}]" if rec.why else ""))

    if rec.placements:
        for p in rec.placements:
            print(f"  placed on  {p.board}  at {p.position()}  {p.rot_deg:g}deg  {p.side}")
    else:
        print("  placed on  (not placed on any board)")

    print(f"  status     {Status.LABELS.get(rec.status, rec.status)}")
    hint = rec.hint()
    if hint:
        print(f"             {hint}")
    return EXIT_OK


def cmd_xref(ws: Workspace, args) -> int:
    _refresh(ws, args)
    records = _filtered(ws, args)

    if args.json:
        print(json.dumps(ws.index.to_json(records), indent=2))
        return EXIT_OK

    if args.csv:
        if args.csv == "-":
            ws.index.write_csv(sys.stdout, records)
        else:
            path = Path(args.csv)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8", newline="") as fh:
                n = ws.index.write_csv(fh, records)
            print(f"Wrote {n} row(s) to {path}")
        return EXIT_OK

    width = max((len(r.ref) for r in records), default=6)
    print(f"{'REF'.ljust(width)}  {'BOARD':<14} {'ASSIGNED':<14} {'STATUS':<12} VALUE")
    for rec in records:
        print(
            f"{rec.ref.ljust(width)}  "
            f"{(', '.join(rec.boards) or '-'):<14} "
            f"{(rec.intent or '-'):<14} "
            f"{Status.LABELS.get(rec.status, rec.status):<12} "
            f"{rec.value}"
        )
    print(f"\n{len(records)} component(s)")
    return EXIT_OK


def _filtered(ws: Workspace, args) -> list:
    query_parts = []
    for board in getattr(args, "board", None) or []:
        query_parts.append(f"board:{board}")
    for status in getattr(args, "status", None) or []:
        query_parts.append(f"status:{status}")
    if getattr(args, "query", ""):
        query_parts.append(args.query)

    if not query_parts:
        return ws.index.records()
    hits = ws.index.search(" ".join(query_parts), limit=1_000_000)
    return sorted((h.record for h in hits), key=lambda r: natural_key(r.ref))


def cmd_check(ws: Workspace, args) -> int:
    result = _refresh(ws, args, quiet=False)
    conflicts = ws.index.conflicts()

    if args.json:
        print(
            json.dumps(
                {
                    "conflicts": ws.index.to_json(conflicts),
                    "count": len(conflicts),
                    "by_status": result.stats.by_status,
                },
                indent=2,
            )
        )
    elif not conflicts:
        print(
            f"No conflicts. {result.stats.components} component(s) across {len(ws.config.boards)} board(s)."
        )
    else:
        print(f"{len(conflicts)} conflict(s):\n")
        for rec in conflicts:
            print(f"  {rec.ref:<10} {Status.LABELS.get(rec.status, rec.status):<12} {rec.hint()}")

    if conflicts and args.exit_code_conflicts:
        return EXIT_CONFLICTS
    return EXIT_OK


def cmd_drc(ws: Workspace, args) -> int:
    from .core.fab import format_drc, run_drc

    names = _selected_boards(ws, args)
    if not names:
        print("error: no boards selected (use --all or --board NAME)", file=sys.stderr)
        return EXIT_ERROR

    results = {}
    for name in names:
        board = ws.config.boards[name]
        pcb = ws.board_pcb(name)
        if pcb is None or not pcb.exists():
            from .core.fab import DrcResult

            results[name] = DrcResult(board=name, error="PCB not found")
            continue
        ports = [p.effective_net() for p in board.ports.values()]
        results[name] = run_drc(ws.install, ws.root, name, pcb, ports=ports, parity=args.schematic_parity)

    if args.json:
        payload = {
            name: {
                "ok": r.ok,
                "error": r.error,
                "count": r.count,
                "filtered": r.filtered,
                "violations": [
                    {"type": v.type, "severity": v.severity, "description": v.description}
                    for v in r.violations
                ],
            }
            for name, r in results.items()
        }
        Path(args.json).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Wrote {args.json}")
    else:
        print(format_drc(results))

    if any(r.error for r in results.values()):
        return EXIT_ERROR
    if args.exit_code_violations and any(r.count for r in results.values()):
        return EXIT_VIOLATIONS
    return EXIT_OK


def cmd_fab(ws: Workspace, args) -> int:
    from .core.fab import run_fab

    names = _selected_boards(ws, args)
    if not names:
        print("error: no boards selected (use --all or --board NAME)", file=sys.stderr)
        return EXIT_ERROR

    failed = 0
    for name in names:
        board = ws.config.boards[name]
        print(f"Building {name}...")
        result = run_fab(
            ws.install,
            ws.root,
            board,
            jobset=Path(args.jobset) if args.jobset else None,
            out_dir=Path(args.out) / name if args.out else None,
        )
        if result.ok:
            print(f"  ok ({result.duration:.1f}s)")
        else:
            print(f"  FAILED: {result.failure_text()}", file=sys.stderr)
            failed += 1

    return EXIT_ERROR if failed else EXIT_OK


def cmd_doctor(ws: Workspace, args) -> int:
    report = ws.doctor()
    if args.json:
        print(json.dumps(report.to_json(), indent=2))
    else:
        print(report.to_text())
    from .core.doctor import ERROR

    return EXIT_ERROR if report.worst == ERROR else EXIT_OK


def cmd_sync(ws: Workspace, args) -> int:
    from .core.plan import format_plan

    if args.board not in ws.config.boards:
        print(f"error: no board named '{args.board}'", file=sys.stderr)
        return EXIT_NOT_FOUND

    _refresh(ws, args, quiet=False)
    plan = ws.plan_update(args.board, include_unassigned=not args.strict)

    if args.json:
        print(
            json.dumps(
                {
                    "board": plan.board,
                    "counts": plan.counts(),
                    "items": [
                        {
                            "ref": i.ref,
                            "action": i.action,
                            "reason": i.reason,
                            "before": i.before,
                            "after": i.after,
                            "enabled": i.enabled,
                        }
                        for i in plan.items
                    ],
                    "conflicts": [r.ref for r in plan.conflicts],
                },
                indent=2,
            )
        )
    else:
        print(format_plan(plan))
        print(
            "This is a preview. Board changes are applied from the plugin in KiCad,\n"
            "where you can review and deselect individual items."
        )
    return EXIT_OK


def cmd_boards(ws: Workspace, args) -> int:
    _refresh(ws, args)
    counts = ws.index.board_counts()
    open_boards = ws.open_boards()

    if args.json:
        print(
            json.dumps(
                {
                    name: {
                        "pcb_path": b.pcb_path,
                        "description": b.description,
                        "ports": sorted(b.ports),
                        "open": name in open_boards,
                        **counts.get(name, {}),
                    }
                    for name, b in sorted(ws.config.boards.items())
                },
                indent=2,
            )
        )
        return EXIT_OK

    if not ws.config.boards:
        print("No boards yet.")
        return EXIT_OK

    width = max(len(n) for n in ws.config.boards)
    print(f"{'BOARD'.ljust(width)}  PLACED  PENDING  CONFLICTS  PATH")
    for name, board in sorted(ws.config.boards.items()):
        c = counts.get(name, {})
        flag = " (open)" if name in open_boards else ""
        print(
            f"{name.ljust(width)}  {c.get('placed', 0):>6}  {c.get('pending', 0):>7}  "
            f"{c.get('conflicts', 0):>9}  {board.pcb_path}{flag}"
        )
    return EXIT_OK


def _selected_boards(ws: Workspace, args) -> list[str]:
    if getattr(args, "all", False):
        return sorted(ws.config.boards)
    names = []
    for name in getattr(args, "board", None) or []:
        if name not in ws.config.boards:
            print(f"warning: no board named '{name}'", file=sys.stderr)
            continue
        names.append(name)
    return names


if __name__ == "__main__":
    sys.exit(main())
