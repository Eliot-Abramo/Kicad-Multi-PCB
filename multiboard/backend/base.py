"""
The boundary between pure logic and KiCad.

Everything that must touch ``pcbnew`` lives behind this interface. That is what
keeps ``multiboard.core`` importable in CI and what makes the eventual KiCad 11
port a bounded piece of work rather than a rewrite.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from ..core.config import PortDef


@dataclass
class BlockSpec:
    """A board block footprint: an outline, a label, and one pad per port."""

    name: str
    width_mm: float
    height_mm: float
    ports: list[PortDef] = field(default_factory=list)
    description: str = ""

    def corner_radius(self) -> float:
        return max(1.0, min(3.0, self.width_mm * 0.08, self.height_mm * 0.08))

    def port_position(self, port: PortDef) -> tuple[float, float]:
        """Millimetre coordinates of a port on the block outline."""
        p = min(1.0, max(0.0, port.position))
        w, h = self.width_mm, self.height_mm
        if port.side == "left":
            return (-w / 2, h * (p - 0.5))
        if port.side == "right":
            return (w / 2, h * (p - 0.5))
        if port.side == "top":
            return (w * (p - 0.5), -h / 2)
        if port.side == "bottom":
            return (w * (p - 0.5), h / 2)
        return (0.0, 0.0)

    def port_rotation(self, port: PortDef) -> float:
        return {"left": 180.0, "right": 0.0, "top": 270.0, "bottom": 90.0}.get(port.side, 0.0)


@dataclass
class ApplyResult:
    """Outcome of writing a planned update to a board."""

    added: int = 0
    updated: int = 0
    replaced: int = 0
    removed: int = 0
    failed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cancelled: bool = False

    def summary(self) -> str:
        bits = []
        for label, n in (
            ("Added", self.added),
            ("Updated", self.updated),
            ("Replaced", self.replaced),
            ("Removed", self.removed),
        ):
            if n:
                bits.append(f"{label}: {n}")
        if self.failed:
            bits.append(f"Failed: {len(self.failed)}")
        if self.cancelled:
            bits.append("Cancelled before saving")
        return "\n".join(bits) if bits else "No changes were needed."


class Backend:
    """
    Interface every backend implements.

    Methods raise ``NotImplementedError`` by default so a partial backend fails
    obviously rather than silently doing nothing.
    """

    name = "base"

    def version(self) -> tuple[int, ...]:
        raise NotImplementedError

    def capabilities(self) -> dict:
        return {}

    # -- boards ------------------------------------------------------------

    def new_board(self, path: Path) -> None:
        """Create an empty, correctly-formatted board file."""
        raise NotImplementedError

    def active_board_path(self) -> Optional[Path]:
        """The board open in this editor, if any."""
        return None

    def focus_reference(self, ref: str) -> bool:
        """Select and zoom to a component on the active board."""
        return False

    def refresh_ui(self) -> None:
        return None

    # -- footprint libraries ----------------------------------------------

    def write_block_footprint(self, lib_dir: Path, spec: BlockSpec) -> None:
        raise NotImplementedError

    def write_port_footprint(self, lib_dir: Path, port_name: str) -> None:
        raise NotImplementedError

    # -- the update pipeline ----------------------------------------------

    def apply_update(
        self,
        pcb_path: Path,
        plan,
        netlist_path: Path,
        *,
        lib_paths: dict,
        progress: Optional[Callable[[int, str], None]] = None,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> ApplyResult:
        raise NotImplementedError


def get_backend() -> Backend:
    """
    The backend for the current environment.

    Only the SWIG backend exists today. KiCad 10's IPC API cannot read
    schematics, cannot run headless, and explicitly refuses to open or switch
    documents, so it cannot host this plugin's workflow -- see
    ``ipc_backend.py`` for the specifics and what changes in KiCad 11.
    """
    from .swig_backend import SwigBackend

    return SwigBackend()
