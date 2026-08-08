"""
Configuration data model (``.kicad_multiboard.json``), schema v3.

New in v3 relative to v12's "12.0":

* ``assignments`` and ``rules`` -- the *intent* layer of the ownership model.
  v12 had no intent at all: ownership was re-derived from PCB contents on every
  query, so you could not plan an assignment before placing a part, and a
  component placed on two boards was silently overwritten rather than reported.
* ``board_field`` -- a schematic symbol field the plugin READS for assignment.
  It never writes ``.kicad_sch``.
* ``board_colors`` -- stable per-board colour, so a board is recognisable at a
  glance everywhere in the UI.
* ``variant`` -- KiCad 10 design variant to export netlists against.
* ``schema`` is separate from ``plugin_version``. v12 conflated them.

The config is storage, not an interface. Everything in here is created and
edited from the GUI; a user should never need to open the file.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..constants import (
    DEFAULT_BLOCK_HEIGHT,
    DEFAULT_BLOCK_WIDTH,
    DEFAULT_BOARD_FIELD,
    DEFAULT_PORT_POSITION,
)
from ..version import CONFIG_SCHEMA, __version__
from .cache import atomic_write_json, read_json

SIDES = ("left", "right", "top", "bottom")
RULE_KINDS = ("sheet", "refrange", "regex")


@dataclass
class PortDef:
    """An inter-board connection point, rendered as a pad on the block footprint."""

    name: str
    net: str = ""
    side: str = "right"
    position: float = DEFAULT_PORT_POSITION

    def effective_net(self) -> str:
        """The net this port carries; falls back to the port name.

        v12's docstring promised this fallback but no code implemented it, so a
        port with an empty net was dropped from DRC filtering entirely.
        """
        return self.net or self.name

    def to_dict(self) -> dict:
        return {"name": self.name, "net": self.net, "side": self.side, "position": self.position}

    @classmethod
    def from_dict(cls, data: dict, key: str = "") -> "PortDef":
        side = str(data.get("side", "right")).lower()
        try:
            pos = float(data.get("position", DEFAULT_PORT_POSITION))
        except (TypeError, ValueError):
            pos = DEFAULT_PORT_POSITION
        return cls(
            name=str(data.get("name") or key),
            net=str(data.get("net", "")),
            side=side if side in SIDES else "right",
            position=min(1.0, max(0.0, pos)),
        )


@dataclass
class AssignRule:
    """
    A pattern that assigns matching components to a board.

    ``kind``:

    ``sheet``
        fnmatch against the component's hierarchical sheet path, e.g.
        ``/Power/*``. This is the primary mechanism -- anyone who already
        organises their schematic hierarchically gets their whole design
        assigned from one rule per board, and parts added later inherit
        automatically.
    ``refrange``
        ``R100-R199, U1, C10-C19``. Compares the numeric part numerically, so
        ``R9`` does not match ``R1-R10``.
    ``regex``
        ``re.fullmatch`` against the reference.
    """

    kind: str
    pattern: str
    board: str
    enabled: bool = True

    def label(self) -> str:
        return {"sheet": "Sheet", "refrange": "Refs", "regex": "Regex"}.get(self.kind, self.kind)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "pattern": self.pattern, "board": self.board, "enabled": self.enabled}

    @classmethod
    def from_dict(cls, data: dict) -> "AssignRule":
        kind = str(data.get("kind", "sheet"))
        return cls(
            kind=kind if kind in RULE_KINDS else "sheet",
            pattern=str(data.get("pattern", "")),
            board=str(data.get("board", "")),
            enabled=bool(data.get("enabled", True)),
        )


@dataclass
class BoardConfig:
    """One sub-board."""

    name: str
    pcb_path: str
    description: str = ""
    block_width: float = DEFAULT_BLOCK_WIDTH
    block_height: float = DEFAULT_BLOCK_HEIGHT
    ports: dict[str, PortDef] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "pcb_path": self.pcb_path,
            "description": self.description,
            "block_width": self.block_width,
            "block_height": self.block_height,
            "ports": {n: p.to_dict() for n, p in sorted(self.ports.items())},
        }

    @classmethod
    def from_dict(cls, data: dict, key: str = "") -> "BoardConfig":
        # v12 used data["name"], which raised KeyError on any hand-edited or
        # partially-written entry and took the whole config down with it.
        cfg = cls(
            name=str(data.get("name") or key),
            pcb_path=str(data.get("pcb_path", "")),
            description=str(data.get("description", "")),
            block_width=_f(data.get("block_width"), DEFAULT_BLOCK_WIDTH),
            block_height=_f(data.get("block_height"), DEFAULT_BLOCK_HEIGHT),
        )
        for pname, pdata in (data.get("ports") or {}).items():
            cfg.ports[pname] = (
                PortDef.from_dict(pdata, pname) if isinstance(pdata, dict) else PortDef(name=pname)
            )
        return cfg


@dataclass
class ProjectConfig:
    """Top-level multi-board project configuration."""

    schema: int = CONFIG_SCHEMA
    plugin_version: str = __version__
    root_schematic: str = ""
    root_pcb: str = ""
    variant: str = ""
    board_field: str = DEFAULT_BOARD_FIELD
    assignments: dict[str, str] = field(default_factory=dict)
    rules: list[AssignRule] = field(default_factory=list)
    board_colors: dict[str, str] = field(default_factory=dict)
    boards: dict[str, BoardConfig] = field(default_factory=dict)

    # -- board helpers ----------------------------------------------------

    def board_names(self) -> list[str]:
        return sorted(self.boards)

    def pcb_path(self, root: Path, board: str) -> Optional[Path]:
        b = self.boards.get(board)
        return (root / b.pcb_path) if b and b.pcb_path else None

    def rename_board(self, old: str, new: str) -> None:
        """Rename a board and carry every reference to it along."""
        if old not in self.boards or old == new:
            return
        cfg = self.boards.pop(old)
        cfg.name = new
        self.boards[new] = cfg
        self.assignments = {r: (new if b == old else b) for r, b in self.assignments.items()}
        for rule in self.rules:
            if rule.board == old:
                rule.board = new
        if old in self.board_colors:
            self.board_colors[new] = self.board_colors.pop(old)

    def forget_board(self, name: str) -> None:
        """Drop a board and every assignment and rule pointing at it."""
        self.boards.pop(name, None)
        self.board_colors.pop(name, None)
        self.assignments = {r: b for r, b in self.assignments.items() if b != name}
        self.rules = [r for r in self.rules if r.board != name]

    # -- serialisation ----------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "plugin_version": self.plugin_version,
            "root_schematic": self.root_schematic,
            "root_pcb": self.root_pcb,
            "variant": self.variant,
            "board_field": self.board_field,
            "assignments": dict(sorted(self.assignments.items())),
            "rules": [r.to_dict() for r in self.rules],
            "board_colors": dict(sorted(self.board_colors.items())),
            "boards": {n: b.to_dict() for n, b in sorted(self.boards.items())},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectConfig":
        data = migrate(data)
        cfg = cls(
            schema=int(data.get("schema", CONFIG_SCHEMA)),
            plugin_version=str(data.get("plugin_version", __version__)),
            root_schematic=str(data.get("root_schematic", "")),
            root_pcb=str(data.get("root_pcb", "")),
            variant=str(data.get("variant", "")),
            board_field=str(data.get("board_field") or DEFAULT_BOARD_FIELD),
            assignments={str(k): str(v) for k, v in (data.get("assignments") or {}).items()},
            rules=[AssignRule.from_dict(r) for r in (data.get("rules") or []) if isinstance(r, dict)],
            board_colors={str(k): str(v) for k, v in (data.get("board_colors") or {}).items()},
        )
        for name, bdata in (data.get("boards") or {}).items():
            cfg.boards[name] = (
                BoardConfig.from_dict(bdata, name) if isinstance(bdata, dict) else BoardConfig(name, "")
            )
            # Keep the dict key authoritative so a rename cannot drift.
            cfg.boards[name].name = name
        return cfg


def _f(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# =============================================================================
# Migration
# =============================================================================


def migrate(raw: dict) -> dict:
    """
    Bring any older config up to schema v3. Pure and idempotent.

    Existing users' projects keep working: v12 wrote ``"version": "12.0"`` with
    no ``schema`` key, and its boards carry no assignments or rules. We add the
    new keys with empty defaults, which reproduces exactly v12's behaviour
    (ownership derived purely from placement) until the user creates a rule.
    """
    if not isinstance(raw, dict):
        return {"schema": CONFIG_SCHEMA}

    data = dict(raw)

    if "schema" not in data:
        # v12 and earlier: a "version" string that conflated schema and plugin.
        data["schema"] = 3
        data.setdefault("plugin_version", str(data.pop("version", "12.0")))

    data.setdefault("assignments", {})
    data.setdefault("rules", [])
    data.setdefault("board_colors", {})
    data.setdefault("board_field", DEFAULT_BOARD_FIELD)
    data.setdefault("variant", "")

    boards = data.get("boards")
    if isinstance(boards, dict):
        fixed = {}
        for name, bdata in boards.items():
            fixed[name] = bdata if isinstance(bdata, dict) else {"name": name, "pcb_path": ""}
        data["boards"] = fixed
    else:
        data["boards"] = {}

    data["schema"] = CONFIG_SCHEMA
    return data


# =============================================================================
# Load / save
# =============================================================================


def load(path: Path) -> tuple[ProjectConfig, Optional[str]]:
    """
    Load a config. Returns ``(config, warning)``.

    A corrupt file recovers from the ``.bak`` and reports it, rather than v12's
    behaviour of logging quietly and continuing with an empty config -- which
    the user experienced as every board vanishing.
    """
    try:
        data, warning = read_json(path)
    except FileNotFoundError:
        return ProjectConfig(), None
    return ProjectConfig.from_dict(data), warning


def save(path: Path, cfg: ProjectConfig) -> None:
    """Atomically persist a config, keeping the previous version as ``.bak``."""
    cfg.plugin_version = __version__
    cfg.schema = CONFIG_SCHEMA
    atomic_write_json(path, cfg.to_dict())
