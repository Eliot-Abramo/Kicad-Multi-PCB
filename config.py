"""
Multi-Board PCB Manager - Data Models
======================================

Data structures for project configuration, serialized to JSON.

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
    Inter-board electrical connection point.
    
    Ports represent physical connections between boards (connectors,
    flex cables, etc.) and appear as pads on block footprints.
    """
    name: str
    net: str = ""
    side: str = "right"  # left, right, top, bottom
    position: float = DEFAULT_PORT_POSITION  # 0.0 to 1.0 along edge
    
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "net": self.net,
            "side": self.side,
            "position": self.position,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "PortDef":
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
    """
    name: str
    pcb_path: str
    description: str = ""
    block_width: float = DEFAULT_BLOCK_WIDTH
    block_height: float = DEFAULT_BLOCK_HEIGHT
    ports: Dict[str, PortDef] = field(default_factory=dict)
    
    def to_dict(self) -> dict:
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
                config.ports[port_name] = PortDef(name=port_name)
        return config


@dataclass
class ProjectConfig:
    """
    Top-level multiboard project configuration.
    """
    version: str = CONFIG_VERSION
    root_schematic: str = ""
    root_pcb: str = ""
    boards: Dict[str, BoardConfig] = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "root_schematic": self.root_schematic,
            "root_pcb": self.root_pcb,
            "boards": {name: board.to_dict() for name, board in self.boards.items()},
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "ProjectConfig":
        config = cls(
            version=data.get("version", CONFIG_VERSION),
            root_schematic=data.get("root_schematic", ""),
            root_pcb=data.get("root_pcb", ""),
        )
        for board_name, board_data in data.get("boards", {}).items():
            if isinstance(board_data, dict):
                config.boards[board_name] = BoardConfig.from_dict(board_data)
            else:
                config.boards[board_name] = BoardConfig(name=board_name, pcb_path="")
        return config
