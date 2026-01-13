"""
Multi-Board PCB Manager - Configuration Data Model
==================================================

This module defines the JSON-serializable data structures persisted to disk
in .kicad_multiboard.json

Data Model Overview
-------------------

    ProjectConfig (top level)
    └── boards: Dict[str, BoardConfig]
            └── ports: Dict[str, PortDef]

A "board" represents a sub-PCB living under boards/<name>/<name>.kicad_pcb
All boards share the same root schematic via hardlinks/symlinks.

Ports
-----
Ports are "inter-board connection points":
- Conceptually: connector pins, flex tails, board-to-board headers
- In practice: they become pads on generated "block footprints"

The UI places ports on any edge using:
- side: left/right/top/bottom
- position: 0.0 → 1.0 along that edge

Author: Eliot Abramo
License: MIT
"""

from dataclasses import dataclass, field
from typing import Dict

from .constants import (
    CONFIG_VERSION,
    DEFAULT_BLOCK_HEIGHT,
    DEFAULT_BLOCK_WIDTH,
    DEFAULT_PORT_POSITION,
)


@dataclass
class PortDef:
    """
    Inter-board electrical connection point.

    Ports represent physical connections between boards (connectors,
    flex cables, etc.) and appear as pads on block footprints.

    Attributes
    ----------
    name : str
        Port identifier (e.g., "USB_D+", "VIN").
    net : str
        Associated net name. If empty, defaults to port name.
    side : str
        Board edge: "left", "right", "top", or "bottom".
    position : float
        Position along edge, 0.0 to 1.0 (0.5 = center).
    """

    name: str
    net: str = ""
    side: str = "right"
    position: float = DEFAULT_PORT_POSITION

    def to_dict(self) -> dict:
        """Serialize to dictionary for JSON storage."""
        return {
            "name": self.name,
            "net": self.net,
            "side": self.side,
            "position": self.position,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PortDef":
        """Deserialize from dictionary."""
        return cls(
            name=data.get("name", ""),
            net=data.get("net", ""),
            side=data.get("side", "right"),
            position=data.get("position", DEFAULT_PORT_POSITION),
        )


@dataclass
class BoardConfig:
    """
    Configuration for a single sub-board.

    Attributes
    ----------
    name : str
        Human-readable board name (e.g., "Power", "IO").
    pcb_path : str
        Relative path to the PCB file from project root.
    description : str
        Optional description of the board's purpose.
    block_width : float
        Width of the generated block footprint in mm.
    block_height : float
        Height of the generated block footprint in mm.
    ports : Dict[str, PortDef]
        Inter-board connection points.
    """

    name: str
    pcb_path: str
    description: str = ""
    block_width: float = DEFAULT_BLOCK_WIDTH
    block_height: float = DEFAULT_BLOCK_HEIGHT
    ports: Dict[str, PortDef] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialize to dictionary for JSON storage."""
        return {
            "name": self.name,
            "pcb_path": self.pcb_path,
            "description": self.description,
            "block_width": self.block_width,
            "block_height": self.block_height,
            "ports": {name: port.to_dict() for name, port in self.ports.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BoardConfig":
        """Deserialize from dictionary."""
        config = cls(
            name=data["name"],
            pcb_path=data.get("pcb_path", ""),
            description=data.get("description", ""),
            block_width=data.get("block_width", DEFAULT_BLOCK_WIDTH),
            block_height=data.get("block_height", DEFAULT_BLOCK_HEIGHT),
        )
        for port_name, port_data in data.get("ports", {}).items():
            if isinstance(port_data, dict):
                config.ports[port_name] = PortDef.from_dict(port_data)
            else:
                # Legacy format: just port name as string
                config.ports[port_name] = PortDef(name=port_name)
        return config


@dataclass
class ProjectConfig:
    """
    Top-level multiboard project configuration.

    Attributes
    ----------
    version : str
        Configuration file version for migration handling.
    root_schematic : str
        Filename of the root schematic (source of truth).
    root_pcb : str
        Filename of the optional root PCB.
    boards : Dict[str, BoardConfig]
        All sub-boards in the project, keyed by name.
    """

    version: str = CONFIG_VERSION
    root_schematic: str = ""
    root_pcb: str = ""
    boards: Dict[str, BoardConfig] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Serialize to dictionary for JSON storage."""
        return {
            "version": self.version,
            "root_schematic": self.root_schematic,
            "root_pcb": self.root_pcb,
            "boards": {name: board.to_dict() for name, board in self.boards.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ProjectConfig":
        """Deserialize from dictionary."""
        config = cls(
            version=data.get("version", CONFIG_VERSION),
            root_schematic=data.get("root_schematic", ""),
            root_pcb=data.get("root_pcb", ""),
        )
        for board_name, board_data in data.get("boards", {}).items():
            if isinstance(board_data, dict):
                config.boards[board_name] = BoardConfig.from_dict(board_data)
            else:
                # Legacy format: just board name
                config.boards[board_name] = BoardConfig(name=board_name, pcb_path="")
        return config
