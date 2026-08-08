"""
The cross-board component index.

This is what answers "where does R42 live?" without opening a single PCB.

Three layers
------------
**Intent** -- where a component *should* go. Explicit pin, then the ``MB_Board``
schematic field, then the first matching rule. Recorded with its provenance, so
the UI can always answer "why is R42 on Power?".

**Reality** -- where it *is*, read from the board files. Crucially this is a
*list* of placements, not a single value. v12 wrote ``placed[ref] = board`` while
looping over boards, so a component placed on two boards silently became
whichever board happened to be last in dict order. That also made the README's
documented "a component belongs to the first board it is placed on" rule false.

**Reconciliation** -- the difference between the two, classified into a
:class:`Status` that carries a suggested fix. There is no longer any winner-takes
-all rule; a duplicate is a reported conflict showing every board and position.
"""

import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..constants import INDEX_CACHE_FILE, SEARCH_RESULT_LIMIT
from ..version import __version__
from . import rules as rules_mod
from .cache import file_key, write_json_compact
from .config import ProjectConfig
from .netlist import SchComponent, parse_netlist
from .pcb_scan import PcbFootprint, PcbScan, scan_pcb_file
from .project import find_hierarchical_sheets, work_dir

CACHE_SCHEMA = 1


class Origin:
    """Where a component's intended board came from."""

    PIN = "pin"
    FIELD = "field"
    RULE = "rule"
    NONE = "none"


class Status:
    """Reconciliation outcome. Ordered roughly by how much it needs attention."""

    OK = "ok"
    """Intent matches reality, exactly one placement."""

    ADOPT = "adopt"
    """Placed, but no intent recorded. Offer: adopt the placement as intent."""

    TODO = "todo"
    """Assigned but not yet placed. Offer: update that board."""

    MISPLACED = "misplaced"
    """Assigned to one board, placed on a different one."""

    DUPLICATE = "duplicate"
    """Placed on more than one board."""

    ORPHAN = "orphan"
    """On a board but not in the schematic."""

    NOWHERE = "nowhere"
    """In the schematic, no intent, not placed anywhere."""

    SKIPPED = "skipped"
    """DNP, excluded from board, or no footprint -- not expected on any board."""

    CONFLICTS = (MISPLACED, DUPLICATE, ORPHAN)

    LABELS = {
        OK: "OK",
        ADOPT: "Unassigned",
        TODO: "Not placed",
        MISPLACED: "Misplaced",
        DUPLICATE: "Duplicate",
        ORPHAN: "Orphan",
        NOWHERE: "No home",
        SKIPPED: "Skipped",
    }

    HINTS = {
        OK: "Placed on its assigned board.",
        ADOPT: "Placed, but no rule or pin assigns it. Adopt the placement to make it intentional.",
        TODO: "Assigned to a board but not placed there yet. Run Update on that board.",
        MISPLACED: "Assigned to one board but physically placed on another.",
        DUPLICATE: "This reference exists on more than one board. Delete it from all but one.",
        ORPHAN: "On a board but absent from the schematic. It was probably deleted upstream.",
        NOWHERE: "Nothing assigns it and it is not placed. Add a rule or pin it to a board.",
        SKIPPED: "Not expected on any board.",
    }

    ACTIONS = {
        ADOPT: "Adopt placement as intent",
        TODO: "Update board",
        MISPLACED: "Reassign to where it is placed",
        DUPLICATE: "Show every placement",
        ORPHAN: "Remove from board",
        NOWHERE: "Assign to a board",
    }


@dataclass(frozen=True)
class Placement:
    """One physical placement of a component."""

    board: str
    x_mm: float
    y_mm: float
    rot_deg: float
    side: str
    fpid: str
    value: str = ""
    uuid: str = ""
    path: str = ""

    def position(self) -> str:
        return f"{self.x_mm:.2f}, {self.y_mm:.2f} mm"


@dataclass
class ComponentRecord:
    """Everything known about one reference designator."""

    ref: str
    sch: Optional[SchComponent] = None
    intent: Optional[str] = None
    intent_origin: str = Origin.NONE
    intent_detail: str = ""
    placements: list[Placement] = field(default_factory=list)
    status: str = Status.NOWHERE

    # -- convenience for the UI -------------------------------------------

    @property
    def value(self) -> str:
        if self.sch and self.sch.value:
            return self.sch.value
        # An orphan has no schematic side, so fall back to the value recorded on
        # the board itself rather than a fragment of the footprint name.
        return self.placements[0].value if self.placements else ""

    @property
    def footprint(self) -> str:
        if self.sch and self.sch.footprint:
            return self.sch.footprint
        return self.placements[0].fpid if self.placements else ""

    @property
    def sheet(self) -> str:
        return self.sch.sheetpath if self.sch else ""

    @property
    def board(self) -> Optional[str]:
        """Where it actually is, falling back to where it should be."""
        if self.placements:
            return self.placements[0].board
        return self.intent

    @property
    def boards(self) -> list[str]:
        return [p.board for p in self.placements]

    @property
    def is_conflict(self) -> bool:
        return self.status in Status.CONFLICTS

    @property
    def why(self) -> str:
        """Human-readable provenance of the intent, shown in its own column."""
        if self.intent_origin == Origin.PIN:
            return "pinned manually"
        if self.intent_origin == Origin.FIELD:
            return self.intent_detail or "schematic field"
        if self.intent_origin == Origin.RULE:
            return self.intent_detail
        return ""

    def hint(self) -> str:
        base = Status.HINTS.get(self.status, "")
        if self.status == Status.SKIPPED and self.sch:
            reason = self.sch.skip_reason()
            return f"{reason}. {base}" if reason else base
        if self.status == Status.MISPLACED:
            return f"Assigned to {self.intent}, placed on {', '.join(self.boards)}."
        if self.status == Status.DUPLICATE:
            return f"Placed on {', '.join(self.boards)}. Delete it from all but one."
        return base


@dataclass
class IndexStats:
    """Summary of a refresh, for the status bar and the CLI."""

    components: int = 0
    placed: int = 0
    boards_scanned: int = 0
    boards_cached: int = 0
    conflicts: int = 0
    duration: float = 0.0
    warnings: list[str] = field(default_factory=list)
    by_status: dict[str, int] = field(default_factory=dict)
    by_board: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class SearchHit:
    record: ComponentRecord
    score: int


# =============================================================================
# Reconciliation
# =============================================================================


def classify(sch: Optional[SchComponent], intent: Optional[str], placements: Sequence[Placement]) -> str:
    """
    The reconciliation truth table. Pure; one test per row.

    Order matters: duplicates outrank everything because they are the failure
    that silently corrupts a design, and orphans are checked before intent
    because a component absent from the schematic has no meaningful intent.
    """
    if len(placements) > 1:
        return Status.DUPLICATE

    if sch is None:
        return Status.ORPHAN

    if not sch.placeable:
        return Status.SKIPPED

    if not placements:
        return Status.TODO if intent else Status.NOWHERE

    placed_on = placements[0].board
    if intent is None:
        return Status.ADOPT
    return Status.OK if placed_on == intent else Status.MISPLACED


def resolve_intent(
    comp: Optional[SchComponent], cfg: ProjectConfig, ref: str
) -> tuple[Optional[str], str, str]:
    """
    ``(board, origin, detail)`` for a component's intended home.

    Precedence: explicit pin, then the schematic field, then rules. The field is
    read and never written -- a user who prefers keeping assignment in Eeschema
    can set ``MB_Board`` themselves and the index honours it, with zero risk of
    the plugin touching their schematic.
    """
    known = cfg.boards

    pinned = cfg.assignments.get(ref)
    if pinned and pinned in known:
        return pinned, Origin.PIN, "pinned manually"

    if comp is not None and cfg.board_field:
        raw = (comp.fields.get(cfg.board_field) or "").strip()
        if raw:
            if raw in known:
                return raw, Origin.FIELD, f"field {cfg.board_field} = {raw}"
            match = next((b for b in known if b.lower() == raw.lower()), None)
            if match:
                return match, Origin.FIELD, f"field {cfg.board_field} = {raw}"

    if comp is not None:
        hit = rules_mod.apply_rules(cfg.rules, comp)
        if hit and hit.rule.board in known:
            return hit.rule.board, Origin.RULE, hit.detail()

    return None, Origin.NONE, ""


# =============================================================================
# The index
# =============================================================================


class ComponentIndex:
    """
    Cross-board component index.

    Everything here is pure Python -- no pcbnew, no wx -- so ``refresh`` runs on
    a worker thread and the whole class is usable from the CLI where pcbnew does
    not exist.
    """

    def __init__(self, root: Path, cfg: ProjectConfig):
        self.root = root
        self.cfg = cfg
        self._records: dict[str, ComponentRecord] = {}
        self._scans: dict[str, PcbScan] = {}
        self._sch: dict[str, SchComponent] = {}
        self._nets: dict[str, list[tuple[str, str, str]]] = {}
        self._stats = IndexStats()
        self._lock = threading.RLock()
        self._loaded_cache = False

    # -- accessors ---------------------------------------------------------

    @property
    def stats(self) -> IndexStats:
        return self._stats

    @property
    def schematic(self) -> dict[str, SchComponent]:
        """The schematic-side components, for the rules editor's live preview."""
        return self._sch

    def get(self, ref: str) -> Optional[ComponentRecord]:
        with self._lock:
            rec = self._records.get(ref)
            if rec is not None:
                return rec
            lowered = ref.strip().lower()
            for key, value in self._records.items():
                if key.lower() == lowered:
                    return value
            return None

    def records(self) -> list[ComponentRecord]:
        with self._lock:
            return sorted(self._records.values(), key=lambda r: rules_mod.natural_key(r.ref))

    def by_board(self, board: str) -> list[ComponentRecord]:
        return [r for r in self.records() if board in r.boards or r.intent == board]

    def conflicts(self) -> list[ComponentRecord]:
        return [r for r in self.records() if r.is_conflict]

    def board_counts(self) -> dict[str, dict[str, int]]:
        """``{board: {"placed": n, "pending": n, "conflicts": n}}`` for the board list."""
        out = {name: {"placed": 0, "pending": 0, "conflicts": 0} for name in self.cfg.boards}
        for rec in self.records():
            for b in rec.boards:
                if b in out:
                    out[b]["placed"] += 1
            if rec.status == Status.TODO and rec.intent in out:
                out[rec.intent]["pending"] += 1
            if rec.is_conflict:
                for b in set(rec.boards) | ({rec.intent} if rec.intent else set()):
                    if b in out:
                        out[b]["conflicts"] += 1
        return out

    def net(self, name: str) -> list[tuple[str, str, str]]:
        """``[(board, ref, pad), ...]`` carrying a net, across every board."""
        with self._lock:
            return list(self._nets.get(name, []))

    def net_names(self) -> list[str]:
        with self._lock:
            return sorted(self._nets)

    def board_warnings(self) -> list[str]:
        out: list[str] = []
        for scan in self._scans.values():
            out.extend(scan.warnings)
        return out

    # -- refresh -----------------------------------------------------------

    def refresh(
        self,
        *,
        force: bool = False,
        netlist: Optional[Path] = None,
        progress: Optional[Callable[[int, str], None]] = None,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> IndexStats:
        """
        Rebuild the index.

        Board scans are cached on ``(mtime_ns, size)``, so editing one board
        rescans exactly one board. The netlist half is keyed on the root
        schematic *and every hierarchical sheet*, so editing a sub-sheet
        correctly invalidates it.
        """
        import time

        started = time.monotonic()
        stats = IndexStats()

        def report(pct: int, msg: str) -> bool:
            if progress:
                progress(pct, msg)
            return bool(cancel and cancel())

        if not force and not self._loaded_cache:
            self._load_cache()

        # --- schematic side ------------------------------------------------
        if report(5, "Reading schematic..."):
            return self._stats

        sch: dict[str, SchComponent] = {}
        if netlist and netlist.exists():
            try:
                sch = parse_netlist(netlist)
            except Exception as exc:
                stats.warnings.append(f"Netlist: {exc}")
        elif self._sch:
            sch = self._sch  # keep what the cache gave us
        self._sch = sch

        # --- board side ----------------------------------------------------
        scans: dict[str, PcbScan] = {}
        boards = list(self.cfg.boards.items())
        for i, (name, board) in enumerate(boards):
            if report(10 + int(60 * i / max(len(boards), 1)), f"Scanning {name}..."):
                return self._stats

            if not board.pcb_path:
                stats.warnings.append(f"{name}: no PCB path recorded")
                continue
            pcb = self.root / board.pcb_path
            if not pcb.exists():
                stats.warnings.append(f"{name}: {board.pcb_path} not found")
                continue

            cached = self._scans.get(name)
            key = file_key(pcb)
            if not force and cached and key and (cached.mtime_ns, cached.size) == key:
                scans[name] = cached
                stats.boards_cached += 1
                continue

            scan = scan_pcb_file(pcb)
            scans[name] = scan
            stats.boards_scanned += 1
            stats.warnings.extend(scan.warnings)

        self._scans = scans

        # --- join ----------------------------------------------------------
        if report(75, "Reconciling..."):
            return self._stats

        records = self._build_records(sch, scans)
        nets = self._build_nets(scans)

        with self._lock:
            self._records = records
            self._nets = nets

        stats.components = len(records)
        stats.placed = sum(1 for r in records.values() if r.placements)
        stats.conflicts = sum(1 for r in records.values() if r.is_conflict)
        stats.duration = time.monotonic() - started
        for rec in records.values():
            stats.by_status[rec.status] = stats.by_status.get(rec.status, 0) + 1
            for b in rec.boards:
                stats.by_board[b] = stats.by_board.get(b, 0) + 1
        self._stats = stats

        report(95, "Saving index...")
        self._save_cache()
        report(100, "Done")
        return stats

    def reclassify(self) -> IndexStats:
        """
        Recompute intent and status without touching the filesystem.

        Editing a rule or pinning a component changes only the intent layer, and
        this makes that instant -- the rules editor can show live counts while
        the user types without any I/O at all.
        """
        with self._lock:
            for rec in self._records.values():
                intent, origin, detail = resolve_intent(rec.sch, self.cfg, rec.ref)
                rec.intent, rec.intent_origin, rec.intent_detail = intent, origin, detail
                rec.status = classify(rec.sch, intent, rec.placements)
            self._stats.conflicts = sum(1 for r in self._records.values() if r.is_conflict)
            self._stats.by_status = {}
            for rec in self._records.values():
                self._stats.by_status[rec.status] = self._stats.by_status.get(rec.status, 0) + 1
        return self._stats

    def _build_records(
        self, sch: dict[str, SchComponent], scans: dict[str, PcbScan]
    ) -> dict[str, ComponentRecord]:
        placements: dict[str, list[Placement]] = {}
        for board_name, scan in scans.items():
            for fp in scan.footprints:
                if not fp.counts_for_ownership:
                    continue
                placements.setdefault(fp.ref, []).append(_placement(board_name, fp))

        records: dict[str, ComponentRecord] = {}
        for ref in set(sch) | set(placements):
            comp = sch.get(ref)
            places = sorted(placements.get(ref, []), key=lambda p: p.board)
            intent, origin, detail = resolve_intent(comp, self.cfg, ref)
            records[ref] = ComponentRecord(
                ref=ref,
                sch=comp,
                intent=intent,
                intent_origin=origin,
                intent_detail=detail,
                placements=places,
                status=classify(comp, intent, places),
            )
        return records

    @staticmethod
    def _build_nets(scans: dict[str, PcbScan]) -> dict[str, list[tuple[str, str, str]]]:
        nets: dict[str, list[tuple[str, str, str]]] = {}
        for board_name, scan in scans.items():
            for fp in scan.footprints:
                if not fp.counts_for_ownership:
                    continue
                for pad, net in fp.pad_nets:
                    nets.setdefault(net, []).append((board_name, fp.ref, pad))
        return nets

    # -- search ------------------------------------------------------------

    def search(self, query: str, limit: int = SEARCH_RESULT_LIMIT) -> list[SearchHit]:
        """
        Rank records against a query. Fast enough to run on every keystroke.

        Grammar::

            r42                 free text
            board:Power         where it is (or is assigned)
            sheet:/Power/       hierarchical sheet prefix
            net:GND             connected to a net
            fp:0402             footprint substring
            status:duplicate    reconciliation status
            side:back  dnp:yes  origin:rule

        Filters AND across fields and OR within one field.
        """
        terms, free = _parse_query(query)
        results: list[SearchHit] = []

        for rec in self.records():
            if not self._passes(rec, terms):
                continue
            score = _score(rec, free) if free else 1
            if score > 0:
                results.append(SearchHit(rec, score))

        results.sort(key=lambda h: (-h.score, rules_mod.natural_key(h.record.ref)))
        return results[:limit]

    def _passes(self, rec: ComponentRecord, terms: dict[str, list[str]]) -> bool:
        for key, wanted in terms.items():
            low = [w.lower() for w in wanted]
            if key == "board":
                have = {b.lower() for b in rec.boards}
                if rec.intent:
                    have.add(rec.intent.lower())
                if not have & set(low):
                    return False
            elif key == "sheet":
                if not any(rec.sheet.lower().startswith(w.rstrip("*")) for w in low):
                    return False
            elif key == "status":
                if rec.status.lower() not in low:
                    return False
            elif key == "origin":
                if rec.intent_origin.lower() not in low:
                    return False
            elif key == "side":
                sides = {p.side for p in rec.placements}
                if not sides & set(low):
                    return False
            elif key == "fp":
                if not any(w in rec.footprint.lower() for w in low):
                    return False
            elif key == "value":
                if not any(w in rec.value.lower() for w in low):
                    return False
            elif key == "net":
                if not self._nets_for(rec.ref) & set(low):
                    return False
            elif key == "dnp":
                want = low[0] in ("yes", "true", "1")
                is_dnp = bool(rec.sch and rec.sch.dnp)
                if is_dnp != want:
                    return False
        return True

    def _nets_for(self, ref: str) -> set:
        with self._lock:
            return {net.lower() for net, nodes in self._nets.items() if any(r == ref for _, r, _ in nodes)}

    # -- export ------------------------------------------------------------

    CSV_COLUMNS = [
        "Reference",
        "Value",
        "Footprint",
        "Sheet",
        "Assigned",
        "Why",
        "Placed On",
        "Side",
        "X (mm)",
        "Y (mm)",
        "Rotation",
        "Status",
    ]

    def write_csv(self, fh, records: Optional[Iterable[ComponentRecord]] = None) -> int:
        """Write a cross-reference CSV. Returns the row count."""
        import csv

        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(self.CSV_COLUMNS)
        n = 0
        for rec in records if records is not None else self.records():
            first = rec.placements[0] if rec.placements else None
            writer.writerow(
                [
                    rec.ref,
                    rec.value,
                    rec.footprint,
                    rec.sheet,
                    rec.intent or "",
                    rec.why,
                    ", ".join(rec.boards),
                    first.side if first else "",
                    f"{first.x_mm:.3f}" if first else "",
                    f"{first.y_mm:.3f}" if first else "",
                    f"{first.rot_deg:g}" if first else "",
                    Status.LABELS.get(rec.status, rec.status),
                ]
            )
            n += 1
        return n

    def to_json(self, records: Optional[Iterable[ComponentRecord]] = None) -> list:
        """Machine-readable cross-reference, for ``cli xref --json``."""
        out = []
        for rec in records if records is not None else self.records():
            out.append(
                {
                    "ref": rec.ref,
                    "value": rec.value,
                    "footprint": rec.footprint,
                    "sheet": rec.sheet,
                    "assigned": rec.intent,
                    "why": rec.why,
                    "origin": rec.intent_origin,
                    "status": rec.status,
                    "placements": [
                        {
                            "board": p.board,
                            "x_mm": p.x_mm,
                            "y_mm": p.y_mm,
                            "rotation": p.rot_deg,
                            "side": p.side,
                            "footprint": p.fpid,
                        }
                        for p in rec.placements
                    ],
                }
            )
        return out

    # -- cache -------------------------------------------------------------

    def _cache_path(self) -> Path:
        return work_dir(self.root) / INDEX_CACHE_FILE

    def _sch_key(self) -> list:
        """Identity of the schematic side: the root plus every reachable sheet."""
        if not self.cfg.root_schematic:
            return []
        root_sch = self.root / self.cfg.root_schematic
        paths = [root_sch]
        try:
            paths += [root_sch.parent / rel for rel in sorted(find_hierarchical_sheets(root_sch))]
        except OSError:
            pass
        out = []
        for p in paths:
            key = file_key(p)
            if key:
                out.append([p.name, key[0], key[1]])
        return out

    def _load_cache(self) -> None:
        self._loaded_cache = True
        path = self._cache_path()
        try:
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if data.get("schema") != CACHE_SCHEMA or data.get("plugin") != __version__:
            return  # a schema or plugin change invalidates everything

        for name, entry in (data.get("boards") or {}).items():
            board = self.cfg.boards.get(name)
            if not board or not board.pcb_path:
                continue
            self._scans[name] = PcbScan.from_dict(self.root / board.pcb_path, entry)

        netlist = data.get("netlist") or {}
        if netlist.get("key") == self._sch_key():
            try:
                self._sch = {row[0]: SchComponent.from_row(row) for row in netlist.get("comps", [])}
            except (IndexError, TypeError, ValueError):
                self._sch = {}

    def _save_cache(self) -> None:
        try:
            payload = {
                "schema": CACHE_SCHEMA,
                "plugin": __version__,
                "boards": {name: scan.to_dict() for name, scan in self._scans.items()},
                "netlist": {
                    "key": self._sch_key(),
                    "comps": [c.to_row() for c in self._sch.values()],
                },
            }
            write_json_compact(self._cache_path(), payload)
        except OSError:
            pass  # a cache we cannot write is a slowdown, never an error


def _placement(board: str, fp: PcbFootprint) -> Placement:
    return Placement(
        board=board,
        x_mm=fp.x_mm,
        y_mm=fp.y_mm,
        rot_deg=fp.rot_deg,
        side=fp.side,
        fpid=fp.fpid,
        value=fp.value,
        uuid=fp.uuid,
        path=fp.path,
    )


# =============================================================================
# Query parsing and ranking
# =============================================================================

_FILTER_KEYS = {"board", "sheet", "net", "fp", "status", "side", "dnp", "origin", "value"}


def _parse_query(query: str) -> tuple[dict[str, list[str]], str]:
    """Split ``"board:Power r4"`` into ``({"board": ["Power"]}, "r4")``."""
    terms: dict[str, list[str]] = {}
    free: list[str] = []

    for token in query.split():
        key, sep, value = token.partition(":")
        if sep and key.lower() in _FILTER_KEYS and value:
            terms.setdefault(key.lower(), []).append(value)
        else:
            free.append(token)

    return terms, " ".join(free).strip().lower()


def _score(rec: ComponentRecord, needle: str) -> int:
    """
    Rank a record against free text. Higher is better; 0 excludes it.

    Exact reference beats a prefix beats a substring beats a value match, so
    typing "R42" puts R42 first even in a design with R420 through R429.
    """
    ref = rec.ref.lower()
    if ref == needle:
        return 1000
    if ref.startswith(needle):
        return 900 - min(len(ref) - len(needle), 99)
    if needle in ref:
        return 700

    value = rec.value.lower()
    if value == needle:
        return 600
    if needle in value:
        return 500

    if needle in rec.footprint.lower():
        return 300
    if needle in rec.sheet.lower():
        return 200
    if rec.intent and needle in rec.intent.lower():
        return 100
    return 0
