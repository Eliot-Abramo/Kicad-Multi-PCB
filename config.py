"""
Multi-Board PCB Manager - Data Models
======================================

This module defines the data structures used to represent the
multiboard project configuration. These are serialized to/from
JSON for persistence.

The data model hierarchy is:
    ProjectConfig
        └── BoardConfig (one per sub-board)
                └── PortDef (inter-board connections)

Author: Eliot
License: MIT
"""

from dataclasses import dataclass, field
from typing import Dict

from .constants import (
    CONFIG_VERSION,
    DEFAULT_BLOCK_WIDTH,
    DEFAULT_BLOCK_HEIGHT,
    DEFAULT_PORT_POSITION,
)


@dataclass
class PortDef:
    """
    Represents an inter-board electrical connection point.
    
    Ports are placed at board edges and represent physical connections
    between boards (e.g., connectors, flex cables, wire harnesses).
    When placed on the block footprint, they appear as SMD pads that
    can be connected to nets, enabling inter-board connectivity
    checking.
    
    Attributes:
        name: Unique identifier for this port (e.g., "USB_DP", "POWER_IN")
        net: The net name this port connects to (e.g., "USB_D+", "+5V")
        side: Which edge of the board block the port appears on
              Valid values: "left", "right", "top", "bottom"
        position: Relative position along the edge (0.0 = start, 1.0 = end)
                  For left/right edges: 0.0 = top, 1.0 = bottom
                  For top/bottom edges: 0.0 = left, 1.0 = right
    
    Example:
        A USB data+ signal exiting the right side of the board:
        PortDef(name="USB_DP", net="USB_D+", side="right", position=0.3)
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
    Configuration for a single sub-board within the multiboard project.
    
    Each sub-board has its own PCB file, but shares the same schematic
    as all other boards (via hardlinks). Components are assigned to
    specific boards during the update process.
    
    Attributes:
        name: Human-readable board name (e.g., "PowerSupply", "MainBoard")
              Used as the key in ProjectConfig.boards
        pcb_path: Relative path from project root to the .kicad_pcb file
                  Typically "boards/{name}/{name}.kicad_pcb"
        description: Optional description of the board's purpose
        block_width: Width of the block footprint in mm
        block_height: Height of the block footprint in mm
        ports: Dictionary of port definitions, keyed by port name
    
    Example:
        BoardConfig(
            name="PSU",
            pcb_path="boards/PSU/PSU.kicad_pcb",
            description="5V/3.3V power supply module",
            block_width=40.0,
            block_height=30.0,
            ports={"VIN": PortDef(...), "VOUT_5V": PortDef(...)}
        )
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
        
        # Parse ports
        for port_name, port_data in data.get("ports", {}).items():
            if isinstance(port_data, dict):
                config.ports[port_name] = PortDef.from_dict(port_data)
            else:
                # Handle legacy format where port was just a name
                config.ports[port_name] = PortDef(name=port_name)
        
        return config


@dataclass
class ProjectConfig:
    """
    Top-level configuration for the multiboard project.
    
    This is the root configuration object that gets serialized to
    the .kicad_multiboard.json file in the project root. It contains
    references to the root schematic/PCB and all sub-board configurations.
    
    Attributes:
        version: Configuration file version for migration support
        root_schematic: Filename of the main project schematic
                       (e.g., "MyProject.kicad_sch")
        root_pcb: Filename of the main project PCB
                  (e.g., "MyProject.kicad_pcb")
        boards: Dictionary of sub-board configurations, keyed by board name
    
    The root schematic is the single source of truth for all boards.
    All sub-board folders contain hardlinks to this schematic, ensuring
    that edits made anywhere are immediately visible everywhere.
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
        
        # Parse boards
        for board_name, board_data in data.get("boards", {}).items():
            if isinstance(board_data, dict):
                config.boards[board_name] = BoardConfig.from_dict(board_data)
            else:
                # Handle malformed data gracefully
                config.boards[board_name] = BoardConfig(name=board_name, pcb_path="")
        
        return config
