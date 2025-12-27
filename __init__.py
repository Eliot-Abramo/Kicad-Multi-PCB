"""
Multi-Board PCB Manager v5 - KiCad Action Plugin
=================================================
Hierarchical PCB workflow with board block footprints.

Features:
- Board blocks as footprints with port pads (like hierarchical sheets)
- "Update from Root Schematic" via kicad-cli (not filename dependent)
- Component tracking: prevents duplicates across PCBs
- Double-click board block → Open PCB
- Auto-cleanup of KiCad's auto-generated project files

For KiCad 9.0 on Windows

Installation:
    Copy to: %APPDATA%/kicad/9.0/scripting/plugins/
"""

import pcbnew
import wx
import os
import json
import subprocess
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Set
from datetime import datetime
from dataclasses import dataclass, field, asdict
import uuid
import re


import sys
import faulthandler
import tempfile
# ============================================================================
# Constants
# ============================================================================

FOOTPRINT_LIB_NAME = "MultiBoard_Blocks"
BOARD_BLOCK_PREFIX = "BoardBlock_"
PORT_PAD_SIZE = 1.0  # mm
BLOCK_LINE_WIDTH = 0.3  # mm


BOARDS_DIR_NAME = "boards"
# ============================================================================
# Data Models
# ============================================================================

@dataclass
class PortDefinition:
    """A port on a sub-board (becomes a pad on the footprint)."""
    name: str
    direction: str = "bidir"  # input, output, bidir
    net_name: str = ""
    side: str = "right"  # left, right, top, bottom
    position: float = 0.5  # 0.0 to 1.0, relative position along the side
    
    def to_dict(self) -> dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict) -> 'PortDefinition':
        return cls(**data)


@dataclass
class SubBoardDefinition:
    """A sub-PCB board definition."""
    name: str
    pcb_filename: str
    layers: int = 4
    description: str = ""
    
    # Block appearance in root PCB
    block_width: float = 50.0  # mm
    block_height: float = 35.0  # mm
    block_x: float = 50.0  # position in root PCB
    block_y: float = 50.0
    
    # Ports (become pads on footprint)
    ports: Dict[str, PortDefinition] = field(default_factory=dict)
    
    # Footprint reference once placed
    footprint_ref: str = ""
    
    def to_dict(self) -> dict:
        d = asdict(self)
        d['ports'] = {k: v.to_dict() if isinstance(v, PortDefinition) else v 
                      for k, v in self.ports.items()}
        return d
    
    @classmethod
    def from_dict(cls, data: dict) -> 'SubBoardDefinition':
        ports_data = data.pop('ports', {})
        obj = cls(**data)
        obj.ports = {k: PortDefinition.from_dict(v) if isinstance(v, dict) else v 
                     for k, v in ports_data.items()}
        return obj


@dataclass
class MultiBoardProject:
    """Project configuration."""
    version: str = "5.0"
    root_schematic: str = ""
    root_pcb: str = ""
    
    boards: Dict[str, SubBoardDefinition] = field(default_factory=dict)
    
    # Track which components are placed on which board
    component_placement: Dict[str, str] = field(default_factory=dict)  # ref -> board_name
    
    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "root_schematic": self.root_schematic,
            "root_pcb": self.root_pcb,
            "boards": {k: v.to_dict() for k, v in self.boards.items()},
            "component_placement": self.component_placement,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'MultiBoardProject':
        obj = cls(
            version=data.get("version", "5.0"),
            root_schematic=data.get("root_schematic", ""),
            root_pcb=data.get("root_pcb", ""),
            component_placement=data.get("component_placement", {}),
        )
        for name, bdata in data.get("boards", {}).items():
            obj.boards[name] = SubBoardDefinition.from_dict(bdata)
        return obj


# ============================================================================
# Project Manager
# ============================================================================

class ProjectManager:
    """Manages the multi-board project."""

    def _resolve_multiboard_root(self, start_dir: Path) -> Path:
        """
        Resolve the multi-board project root directory.

        If the plugin is launched from a sub-board folder (e.g. <root>/boards/<name>/),
        we walk up parents to find .kicad_multiboard.json. If not found, we fall back to start_dir.
        """
        cur = start_dir.resolve()
        for p in [cur, *cur.parents]:
            if (p / self.CONFIG_FILENAME).exists():
                return p
        return cur

    def _init_logging(self) -> None:
        self.debug_log_path = self.project_path / "multiboard_debug.log"
        self.fault_log_path = self.project_path / "multiboard_fault.log"
        try:
            # Ensure files exist
            self.debug_log_path.touch(exist_ok=True)
        except Exception:
            pass
        try:
            fh = open(self.fault_log_path, "a", encoding="utf-8")
            faulthandler.enable(file=fh, all_threads=True)
            # Keep handle alive
            self._fault_fh = fh
        except Exception:
            self._fault_fh = None

    def _log(self, msg: str) -> None:
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            line = f"[{ts}] {msg}\n"
            with open(self.debug_log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    def _kicad_cli_exe(self) -> Optional[str]:
        """
        Locate kicad-cli across typical installs.
        """
        exe = shutil.which("kicad-cli")
        if exe:
            return exe

        if os.name == "nt":
            # Common user install location:
            local = os.environ.get("LOCALAPPDATA")
            if local:
                cand = Path(local) / "Programs" / "KiCad" / "*" / "bin" / "kicad-cli.exe"
                hits = list(Path(local).glob(str(Path("Programs") / "KiCad" / "*" / "bin" / "kicad-cli.exe")))
                if hits:
                    return str(hits[0])

            # Program Files
            for env in ("ProgramFiles", "ProgramFiles(x86)"):
                base = os.environ.get(env)
                if not base:
                    continue
                hits = list(Path(base).glob(str(Path("KiCad") / "*" / "bin" / "kicad-cli.exe")))
                if hits:
                    return str(hits[0])

        return None

    def _run_kicad_cli(self, args: List[str]) -> subprocess.CompletedProcess:
        exe = self._kicad_cli_exe() or "kicad-cli"
        return subprocess.run([exe, *args], capture_output=True, text=True, cwd=str(self.project_path))

    def _link_file_no_copy(self, src_path: Path, dst_path: Path) -> Tuple[bool, str]:
        """Hardlink (preferred) or symlink (fallback). Never copy."""
        try:
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            if dst_path.exists():
                return True, "exists"
            try:
                os.link(str(src_path), str(dst_path))
                return True, "hardlink"
            except Exception as e_hard:
                try:
                    os.symlink(str(src_path), str(dst_path))
                    return True, "symlink"
                except Exception as e_sym:
                    return False, f"hardlink failed: {e_hard} | symlink failed: {e_sym}"
        except Exception as e:
            return False, str(e)

    def _extract_sheetfile_refs(self, sch_text: str) -> List[str]:
        """Extract hierarchical sheet file references from a .kicad_sch s-expression."""
        refs: List[str] = []

        # (property "Sheetfile" "power.kicad_sch")
        refs += re.findall(r'\(property\s+"Sheetfile"\s+"([^"]+\.kicad_sch)"', sch_text, flags=re.IGNORECASE)

        # (property (name "Sheetfile") (value "power.kicad_sch"))
        refs += re.findall(
            r'\(property\s*\(name\s+"Sheetfile"\)\s*\(value\s+"([^"]+\.kicad_sch)"\)\)',
            sch_text,
            flags=re.IGNORECASE
        )

        # (sheetfile "power.kicad_sch") fallback
        refs += re.findall(r'\(sheetfile\s+"([^"]+\.kicad_sch)"', sch_text, flags=re.IGNORECASE)

        # De-dup preserve order
        out: List[str] = []
        seen = set()
        for r0 in refs:
            if r0 not in seen:
                seen.add(r0)
                out.append(r0)
        return out

    def _collect_sheet_paths_recursive(self, root_sch_path: Path) -> Set[Path]:
        """Recursively collect all hierarchical sheet paths as written in the schematic."""
        collected: Set[Path] = set()
        stack: List[Path] = [root_sch_path.resolve()]
        visited: Set[Path] = set()

        while stack:
            sch = stack.pop()
            if sch in visited:
                continue
            visited.add(sch)

            try:
                txt = sch.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            for ref in self._extract_sheetfile_refs(txt):
                rel = Path(ref)
                collected.add(rel)
                # Resolve relative to current schematic folder
                next_path = (sch.parent / rel).resolve()
                if next_path.exists():
                    stack.append(next_path)

        return collected

    def _write_board_project_files(self, board_dir: Path, base_name: str) -> None:
        """Create minimal per-board .kicad_pro/.kicad_prl so opening PCB directly has a project context."""
        pro = board_dir / f"{base_name}.kicad_pro"
        prl = board_dir / f"{base_name}.kicad_prl"
        if not pro.exists():
            data = {
                "meta": {"filename": pro.name, "version": 1},
                "project": {}
            }
            try:
                pro.write_text(json.dumps(data, indent=2), encoding="utf-8")
            except Exception:
                pass
        if not prl.exists():
            try:
                prl.write_text("{}", encoding="utf-8")
            except Exception:
                pass

    def _write_board_tables(self, board_dir: Path) -> None:
        """Write fp-lib-table and sym-lib-table into the board folder with ${KIPRJMOD} resolved to root."""
        root_abs = self.project_path.as_posix()
        for table_name in ("fp-lib-table", "sym-lib-table"):
            src_table = self.project_path / table_name
            dst_table = board_dir / table_name
            if not src_table.exists():
                continue
            try:
                content = src_table.read_text(encoding="utf-8", errors="ignore")
                content = content.replace("${KIPRJMOD}", root_abs)
                dst_table.write_text(content, encoding="utf-8")
            except Exception:
                # fallback: try link
                self._link_file_no_copy(src_table, dst_table)

    def _ensure_board_miniproject(self, board: SubBoardDefinition, pcb_path: Path) -> Tuple[bool, str]:
        """Ensure per-board folder contains a schematic view that can load hierarchical sheets."""
        board_dir = pcb_path.parent
        base_name = pcb_path.stem

        self._write_board_project_files(board_dir, base_name)
        self._write_board_tables(board_dir)

        # Link root schematic as <base>.kicad_sch
        if not self.project.root_schematic:
            return False, "Root schematic not detected"
        root_sch = (self.project_path / self.project.root_schematic).resolve()
        if not root_sch.exists():
            return False, f"Root schematic missing: {root_sch}"

        board_sch = board_dir / f"{base_name}.kicad_sch"
        ok, why = self._link_file_no_copy(root_sch, board_sch)
        if not ok:
            return False, f"Failed to link root schematic: {why}"

        # Link all hierarchical sheets into board folder (same relative paths)
        sheet_rel_paths = self._collect_sheet_paths_recursive(root_sch)
        for rel in sheet_rel_paths:
            src_sheet = (root_sch.parent / rel).resolve()
            if not src_sheet.exists():
                return False, f"Root schematic references missing sheet: {rel}"
            dst_sheet = (board_dir / rel)
            ok, why = self._link_file_no_copy(src_sheet, dst_sheet)
            if not ok:
                return False, f"Failed to link sheet {rel}: {why}"

        return True, "ok"

    def _fp_lib_nickname_map(self) -> Dict[str, Path]:
        """Parse root fp-lib-table to map nickname -> .pretty folder path (best-effort)."""
        tbl = self.project_path / "fp-lib-table"
        mapping: Dict[str, Path] = {}
        if not tbl.exists():
            return mapping
        try:
            txt = tbl.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return mapping

        # Match: (lib (name "X")(type "KiCad")(uri "....pretty")...)
        for m in re.finditer(r'\(lib\s+\(name\s+"([^"]+)"\)\(type\s+"[^"]*"\)\(uri\s+"([^"]+)"\)', txt):
            nick = m.group(1)
            uri = m.group(2)

            # Expand common vars
            uri_exp = uri.replace("${KIPRJMOD}", self.project_path.as_posix())
            for envk in ("KICAD9_FOOTPRINT_DIR", "KICAD8_FOOTPRINT_DIR", "KICAD_FOOTPRINT_DIR"):
                val = os.environ.get(envk)
                if val:
                    uri_exp = uri_exp.replace(f"${{{envk}}}", Path(val).as_posix())

            # Handle file:// URIs
            if uri_exp.startswith("file://"):
                uri_exp = uri_exp[len("file://"):]

            p = Path(uri_exp)
            if p.suffix.lower() == ".pretty" or p.name.endswith(".pretty"):
                mapping[nick] = p
        return mapping

    def _load_footprint(self, lib_nick: str, fp_name: str) -> Optional["pcbnew.FOOTPRINT"]:
        """Load a footprint using KiCad tables, with a filesystem path fallback."""
        try:
            fp = pcbnew.FootprintLoad(lib_nick, fp_name)
            if fp:
                return fp
        except Exception:
            fp = None

        mapping = getattr(self, "_cached_fp_map", None)
        if mapping is None:
            mapping = self._fp_lib_nickname_map()
            self._cached_fp_map = mapping

        base = mapping.get(lib_nick)
        if base:
            try:
                fp = pcbnew.FootprintLoad(str(base), fp_name)
                if fp:
                    return fp
            except Exception:
                pass

        # Last resort: try project local pretty folder
        try:
            local_pretty = self.project_path / "lib" / f"{lib_nick}.pretty"
            if local_pretty.exists():
                fp = pcbnew.FootprintLoad(str(local_pretty), fp_name)
                if fp:
                    return fp
        except Exception:
            pass

        return None
    
    CONFIG_FILENAME = ".kicad_multiboard.json"
    
    def __init__(self, project_path: Path):
        self.invoked_path = project_path.resolve()
        self.project_path = self._resolve_multiboard_root(self.invoked_path)
        self.config_file = self.project_path / self.CONFIG_FILENAME
        self.footprint_lib_path = self.project_path / f"{FOOTPRINT_LIB_NAME}.pretty"
        self.project = MultiBoardProject()

        self._init_logging()
        self._log(f"ProjectManager init: invoked={self.invoked_path} root={self.project_path}")

        self._detect_root_files()
        self.load()
    
    def _detect_root_files(self):
        """Find the main .kicad_pro and associated files."""
        for f in self.project_path.glob("*.kicad_pro"):
            base_name = f.stem
            sch_file = f.with_suffix(".kicad_sch")
            pcb_file = f.with_suffix(".kicad_pcb")
            
            if sch_file.exists():
                self.project.root_schematic = sch_file.name
            if pcb_file.exists():
                self.project.root_pcb = pcb_file.name
            break
    
    def load(self):
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.project = MultiBoardProject.from_dict(data)
                self._detect_root_files()
            except (json.JSONDecodeError, IOError) as e:
                print(f"Error loading config: {e}")
    
    def save(self):
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.project.to_dict(), f, indent=2)
        except IOError as e:
            print(f"Error saving config: {e}")
    
    # -------------------------------------------------------------------------
    # Footprint Library Management
    # -------------------------------------------------------------------------
    
    def ensure_footprint_library(self) -> bool:
        """Create the footprint library folder if it doesn't exist."""
        if not self.footprint_lib_path.exists():
            try:
                self.footprint_lib_path.mkdir(parents=True)
                return True
            except IOError:
                return False
        return True
    
    def generate_board_footprint(self, board: SubBoardDefinition) -> Tuple[bool, str]:
        """Generate a valid .kicad_mod footprint for a board block (library footprint syntax)."""

        if not self.ensure_footprint_library():
            return False, "Could not create footprint library"

        fp_name = f"{BOARD_BLOCK_PREFIX}{board.name}"
        fp_path = self.footprint_lib_path / f"{fp_name}.kicad_mod"

        w = float(board.block_width)
        h = float(board.block_height)

        lines: List[str] = []
        lines.append(f'(footprint "{fp_name}"')
        lines.append('  (version 20240108)')
        lines.append('  (generator "multi_board_manager")')
        lines.append('  (layer "F.Cu")')
        lines.append(f'  (descr "Board block: {board.name} - {board.description}")')
        lines.append('  (tags "multiboard block")')
        lines.append('  (attr board_only exclude_from_pos_files exclude_from_bom)')

        # Reference / value are fp_text in library footprints (NOT property)
        lines.append(f'  (fp_text reference "MB" (at 0 {(-h/2 - 2):.3f} 0) (layer "F.SilkS")')
        lines.append('    (effects (font (size 1.5 1.5) (thickness 0.15)))')
        lines.append('  )')
        lines.append(f'  (fp_text value "{board.name}" (at 0 0 0) (layer "F.Fab")')
        lines.append('    (effects (font (size 2 2) (thickness 0.2)))')
        lines.append('  )')
        lines.append('  (fp_text user "${REFERENCE}" (at 0 0 0) (layer "F.Fab")')
        lines.append('    (effects (font (size 1 1) (thickness 0.12)))')
        lines.append('  )')

        # Hidden metadata as user text (legal in .kicad_mod)
        lines.append(f'  (fp_text user "PCB_File={board.pcb_filename}" (at 0 {(h/2 + 3):.3f} 0) (layer "F.Fab") hide')
        lines.append('    (effects (font (size 1 1) (thickness 0.1)))')
        lines.append('  )')
        lines.append(f'  (fp_text user "Layers={board.layers}" (at 0 {(h/2 - 3):.3f} 0) (layer "F.SilkS")')
        lines.append('    (effects (font (size 1.5 1.5) (thickness 0.15)))')
        lines.append('  )')

        # Outline
        lines.append(f'  (fp_rect (start {(-w/2):.3f} {(-h/2):.3f}) (end {(w/2):.3f} {(h/2):.3f})')
        lines.append(f'    (stroke (width {BLOCK_LINE_WIDTH}) (type solid))')
        lines.append('    (fill none)')
        lines.append('    (layer "F.SilkS")')
        lines.append('  )')
        # Fab fill
        lines.append(f'  (fp_rect (start {(-w/2):.3f} {(-h/2):.3f}) (end {(w/2):.3f} {(h/2):.3f})')
        lines.append('    (stroke (width 0.1) (type solid))')
        lines.append('    (fill solid)')
        lines.append('    (layer "F.Fab")')
        lines.append('  )')

        # Courtyard
        margin = 2.0
        lines.append(f'  (fp_rect (start {(-w/2 - margin):.3f} {(-h/2 - margin):.3f}) (end {(w/2 + margin):.3f} {(h/2 + margin):.3f})')
        lines.append('    (stroke (width 0.05) (type solid))')
        lines.append('    (fill none)')
        lines.append('    (layer "F.CrtYd")')
        lines.append('  )')

        # Pads for ports (no net assignments in library footprints)
        pad_num = 1
        for port_name, port in board.ports.items():
            px, py = self._calculate_port_position(board, port)

            pad_shape = "rect" if pad_num == 1 else "oval"
            lines.append(f'  (pad "{pad_num}" smd {pad_shape} (at {px:.3f} {py:.3f}) (size {PORT_PAD_SIZE:.3f} {PORT_PAD_SIZE:.3f})'
                         ' (layers "F.Cu" "F.Paste" "F.Mask"))')

            # Port label
            label_offset = 2.0 if port.side in ["right", "bottom"] else -2.0
            label_x = px + (label_offset if port.side in ["left", "right"] else 0.0)
            label_y = py + (label_offset if port.side in ["top", "bottom"] else 0.0)
            justify = "left" if port.side == "right" else "right" if port.side == "left" else "center"

            lines.append(f'  (fp_text user "{port_name}" (at {label_x:.3f} {label_y:.3f} 0) (layer "F.SilkS")')
            lines.append(f'    (effects (font (size 0.8 0.8) (thickness 0.1)) (justify {justify}))')
            lines.append('  )')

            pad_num += 1

        lines.append(')')

        try:
            fp_path.write_text("\n".join(lines), encoding="utf-8")
            self._log(f"Generated footprint: {fp_path}")
            return True, str(fp_path)
        except Exception as e:
            return False, str(e)

    def _calculate_port_position(self, board: SubBoardDefinition, port: PortDefinition) -> Tuple[float, float]:
        """Calculate port pad position based on side and relative position."""
        w = board.block_width
        h = board.block_height
        pos = port.position  # 0.0 to 1.0
        
        if port.side == "right":
            return (w/2, -h/2 + h * pos)
        elif port.side == "left":
            return (-w/2, -h/2 + h * pos)
        elif port.side == "top":
            return (-w/2 + w * pos, -h/2)
        else:  # bottom
            return (-w/2 + w * pos, h/2)
    
    def add_footprint_lib_to_project(self) -> bool:
        """Add the footprint library to the project's fp-lib-table."""
        
        fp_lib_table = self.project_path / "fp-lib-table"
        lib_entry = f'  (lib (name "{FOOTPRINT_LIB_NAME}")(type "KiCad")(uri "${{KIPRJMOD}}/{FOOTPRINT_LIB_NAME}.pretty")(options "")(descr "Multi-board block footprints"))'
        
        if fp_lib_table.exists():
            with open(fp_lib_table, 'r', encoding='utf-8') as f:
                content = f.read()
            
            if FOOTPRINT_LIB_NAME in content:
                return True  # Already added
            
            # Insert before closing paren
            content = content.rstrip().rstrip(')')
            content += f'\n{lib_entry}\n)'
        else:
            content = f'(fp_lib_table\n  (version 7)\n{lib_entry}\n)'
        
        try:
            with open(fp_lib_table, 'w', encoding='utf-8') as f:
                f.write(content)
            return True
        except IOError:
            return False
    
    # -------------------------------------------------------------------------
    # PCB File Management
    # -------------------------------------------------------------------------
    
    def create_sub_board_pcb(self, board: SubBoardDefinition) -> Tuple[bool, str]:
        """Create the sub-board PCB file and its per-board project context under boards/."""
        pcb_path = (self.project_path / board.pcb_filename).resolve()

        # Enforce boards/ layout if user provided only a filename
        try:
            rel = Path(board.pcb_filename)
            if len(rel.parts) == 1:  # just "X.kicad_pcb"
                safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in board.name)
                board.pcb_filename = f"{BOARDS_DIR_NAME}/{safe}/{safe}.kicad_pcb"
                pcb_path = (self.project_path / board.pcb_filename).resolve()
        except Exception:
            pass

        if pcb_path.exists():
            return False, f"PCB file already exists: {board.pcb_filename}"

        pcb_path.parent.mkdir(parents=True, exist_ok=True)

        # Create minimal PCB with correct layer count
        success = self._write_empty_pcb(pcb_path, board.layers)
        if not success:
            return False, "Failed to write PCB file"

        # Ensure per-board project files and schematic links so Eeschema loads sheets when opened from boards/<name>/
        ok, why = self._ensure_board_miniproject(board, pcb_path)
        if not ok:
            self._log(f"miniproject error: {why}")
            return False, why

        self._log(f"Created sub-board pcb: {pcb_path}")
        return True, f"Created {board.pcb_filename}"
    
    def _write_empty_pcb(self, filepath: Path, layer_count: int) -> bool:
        """Write an empty PCB file with specified copper layers."""
        
        # Build copper layer definitions
        copper_layers = []
        for i in range(layer_count):
            if i == 0:
                copper_layers.append('    (0 "F.Cu" signal)')
            elif i == layer_count - 1:
                copper_layers.append('    (31 "B.Cu" signal)')
            else:
                copper_layers.append(f'    ({i} "In{i}.Cu" signal)')
        
        layers_str = "\n".join(copper_layers)
        
        content = f'''(kicad_pcb
  (version 20240108)
  (generator "pcbnew")
  (generator_version "9.0")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
{layers_str}
    (32 "B.Adhes" user "B.Adhesive")
    (33 "F.Adhes" user "F.Adhesive")
    (34 "B.Paste" user)
    (35 "F.Paste" user)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (40 "Dwgs.User" user "User.Drawings")
    (41 "Cmts.User" user "User.Comments")
    (42 "Eco1.User" user "User.Eco1")
    (43 "Eco2.User" user "User.Eco2")
    (44 "Edge.Cuts" user)
    (45 "Margin" user)
    (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard")
    (48 "B.Fab" user)
    (49 "F.Fab" user)
    (50 "User.1" user)
    (51 "User.2" user)
    (52 "User.3" user)
    (53 "User.4" user)
    (54 "User.5" user)
    (55 "User.6" user)
    (56 "User.7" user)
    (57 "User.8" user)
    (58 "User.9" user)
  )
  (setup
    (pad_to_mask_clearance 0)
    (allow_soldermask_bridges_in_footprints no)
  )
  (net 0 "")
)
'''
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(content)
            return True
        except IOError:
            return False
    
    # -------------------------------------------------------------------------
    # Component Tracking
    # -------------------------------------------------------------------------
    
    def scan_placed_components(self) -> Dict[str, str]:
        """Scan all sub-PCBs to find which components are placed where."""
        placement: Dict[str, str] = {}

        for board_name, board in self.project.boards.items():
            pcb_path = (self.project_path / board.pcb_filename).resolve()
            if not pcb_path.exists():
                continue

            refs: List[str] = []
            try:
                b = pcbnew.LoadBoard(str(pcb_path))
                for fp in b.GetFootprints():
                    ref = fp.GetReference()
                    if not ref:
                        continue
                    # Skip board blocks by FPID (library id), not by reference text
                    try:
                        fpid = fp.GetFPID().GetLibItemName() if hasattr(fp, "GetFPID") else ""
                        libnick = fp.GetFPID().GetLibNickname() if hasattr(fp, "GetFPID") else ""
                        if libnick == FOOTPRINT_LIB_NAME and fpid.startswith(BOARD_BLOCK_PREFIX):
                            continue
                    except Exception:
                        # fallback: skip MB refs
                        if ref.startswith("MB"):
                            continue
                    refs.append(ref)
            except Exception:
                # fallback regex scan
                refs = self._get_footprint_refs_from_pcb(pcb_path)

            for ref in refs:
                placement[ref] = board_name

        self.project.component_placement = placement
        self.save()
        return placement

    def _get_footprint_refs_from_pcb(self, pcb_path: Path) -> List[str]:
        """Fallback parser for references from a PCB file."""
        try:
            content = pcb_path.read_text(encoding="utf-8", errors="ignore")
            pattern = r'\(footprint\s.*?\(property\s+"Reference"\s+"([^"]+)"'
            refs = re.findall(pattern, content, re.DOTALL)
            return refs
        except Exception:
            return []
    def get_unplaced_components(self) -> Set[str]:
        """Get components from schematic that aren't placed on any board."""
        
        # Get all components from schematic netlist
        all_components = self._get_schematic_components()
        
        # Get placed components
        self.scan_placed_components()
        placed = set(self.project.component_placement.keys())
        
        return all_components - placed
    
    def _get_schematic_components(self) -> Set[str]:
        """Get all component references from the root schematic."""
        
        components = set()
        
        if not self.project.root_schematic:
            return components
        
        # Generate netlist using kicad-cli
        netlist_path = self.project_path / ".temp_netlist.xml"
        sch_path = self.project_path / self.project.root_schematic
        
        try:
            result = subprocess.run(
                [(self._kicad_cli_exe() or "kicad-cli"), "sch", "export", "netlist", "--format", "kicadxml", "-o", str(netlist_path),
                 str(sch_path)],
                capture_output=True,
                text=True,
                cwd=str(self.project_path)
            )
            
            if result.returncode == 0 and netlist_path.exists():
                components = self._parse_netlist_components(netlist_path)
                netlist_path.unlink()  # Clean up
        except FileNotFoundError:
            print("kicad-cli not found")
        except Exception as e:
            print(f"Error generating netlist: {e}")
        
        return components
    
    def _parse_netlist_components(self, netlist_path: Path) -> Set[str]:
        """Parse component references from netlist XML."""
        components = set()
        
        try:
            tree = ET.parse(netlist_path)
            root = tree.getroot()
            
            for comp in root.findall('.//comp'):
                ref = comp.get('ref')
                if ref and not ref.startswith('#'):
                    components.add(ref)
        except ET.ParseError:
            pass
        
        return components
    
    # -------------------------------------------------------------------------
    # Update PCB from Root Schematic
    # -------------------------------------------------------------------------
    
    def update_pcb_from_root_schematic(self, board_name: str, open_board: Optional["pcbnew.BOARD"] = None) -> Tuple[bool, str]:
        """
        Update a sub-PCB from the root schematic, excluding components already placed on other boards.

        This implementation avoids `kicad-cli pcb import netlist` (can crash/restart KiCad on Windows),
        and instead applies the netlist through pcbnew's Python API.
        """
        board_def = self.project.boards.get(board_name)
        if not board_def:
            return False, "Board not found"

        pcb_path = (self.project_path / board_def.pcb_filename).resolve()
        if not pcb_path.exists():
            return False, f"PCB file not found: {board_def.pcb_filename}"

        if not self.project.root_schematic:
            return False, "Root schematic not detected"

        sch_path = (self.project_path / self.project.root_schematic).resolve()
        if not sch_path.exists():
            return False, f"Root schematic not found: {self.project.root_schematic}"

        # Ensure per-board schematic links exist (fixes sheet loading when opening from boards/<name>/)
        try:
            self._ensure_board_miniproject(board_def, pcb_path)
        except Exception as e:
            self._log(f"_ensure_board_miniproject failed: {e}")

        # 1) determine exclude set (components placed on other boards)
        placement = self.project.component_placement or {}
        exclude_refs: Set[str] = {ref for ref, bname in placement.items() if bname != board_name}

        # 2) export netlist (kicadxml)
        temp_netlist = self.project_path / ".temp_netlist.xml"
        try:
            exe = self._kicad_cli_exe() or "kicad-cli"
            result = subprocess.run(
                [exe, "sch", "export", "netlist", "--format", "kicadxml", "-o", str(temp_netlist), str(sch_path)],
                capture_output=True,
                text=True,
                cwd=str(self.project_path)
            )
            if result.returncode != 0 or not temp_netlist.exists():
                return False, f"Failed to generate netlist: {result.stderr.strip()}"
        except FileNotFoundError:
            return False, "kicad-cli not found (not in PATH)."
        except Exception as e:
            return False, f"Failed to generate netlist: {e}"

        # 3) filter netlist
        filtered = self._filter_netlist(temp_netlist, exclude_refs)

        # 4) load board object (use open board if it matches)
        board_obj: Optional["pcbnew.BOARD"] = None
        using_open = False
        try:
            if open_board and Path(open_board.GetFileName()).resolve() == pcb_path:
                board_obj = open_board
                using_open = True
            else:
                board_obj = pcbnew.LoadBoard(str(pcb_path))
        except Exception as e:
            return False, f"Failed to load PCB: {e}"

        if board_obj is None:
            return False, "Failed to load PCB (None)"

        # 5) parse netlist
        try:
            tree = ET.parse(filtered)
            root = tree.getroot()
        except Exception as e:
            return False, f"Failed to parse netlist XML: {e}"

        # map ref -> (fp_id, value, tstamp)
        comp_info: Dict[str, Tuple[str, str, str]] = {}
        for comp in root.findall(".//components/comp"):
            ref = comp.get("ref") or ""
            if not ref or ref in exclude_refs or ref.startswith("#"):
                continue
            fp_id = (comp.findtext("footprint") or "").strip()
            value = (comp.findtext("value") or "").strip()
            tstamp = (comp.findtext("tstamp") or comp.findtext("uuid") or "").strip()
            comp_info[ref] = (fp_id, value, tstamp)

        # existing footprints
        existing: Dict[str, "pcbnew.FOOTPRINT"] = {}
        try:
            for fp in board_obj.GetFootprints():
                r = fp.GetReference()
                if r:
                    existing[r] = fp
        except Exception:
            pass

        added = 0
        skipped_no_fp = 0
        failed_load = 0

        # 6) ensure footprints exist and set UUID path for cross-probing
        for ref, (fp_id, value, tstamp) in comp_info.items():
            if not fp_id:
                skipped_no_fp += 1
                continue

            fp_inst = existing.get(ref)
            if fp_inst is None:
                # footprint id like "LibNick:FootprintName"
                if ":" in fp_id:
                    lib_nick, fp_name = fp_id.split(":", 1)
                else:
                    lib_nick, fp_name = "", fp_id

                if not lib_nick:
                    skipped_no_fp += 1
                    continue

                fp_loaded = self._load_footprint(lib_nick, fp_name)
                if fp_loaded is None:
                    failed_load += 1
                    self._log(f"Footprint load failed for {ref}: {fp_id}")
                    continue

                try:
                    fp_loaded.SetReference(ref)
                except Exception:
                    pass
                try:
                    fp_loaded.SetValue(value)
                except Exception:
                    pass

                # Place at origin; user can arrange later
                try:
                    fp_loaded.SetPosition(pcbnew.VECTOR2I(0, 0))
                except Exception:
                    pass

                # UUID path link to schematic (enables cross-probing)
                if tstamp:
                    try:
                        if hasattr(pcbnew, "KIID_PATH"):
                            fp_loaded.SetPath(pcbnew.KIID_PATH(f"/{tstamp}"))
                        else:
                            fp_loaded.SetPath(f"/{tstamp}")
                    except Exception:
                        pass

                try:
                    board_obj.Add(fp_loaded)
                    existing[ref] = fp_loaded
                    added += 1
                except Exception as e:
                    self._log(f"Failed to add footprint {ref}: {e}")
                    failed_load += 1
                    continue
            else:
                # update value + ensure path set
                try:
                    if value:
                        fp_inst.SetValue(value)
                except Exception:
                    pass
                if tstamp:
                    try:
                        if hasattr(pcbnew, "KIID_PATH"):
                            fp_inst.SetPath(pcbnew.KIID_PATH(f"/{tstamp}"))
                        else:
                            fp_inst.SetPath(f"/{tstamp}")
                    except Exception:
                        pass

        # 7) nets assignment
        # Build net name -> net code mapping; create if missing
        net_by_name: Dict[str, "pcbnew.NETINFO_ITEM"] = {}
        try:
            # pcbnew API differs slightly; handle both
            if hasattr(board_obj, "GetNetsByName"):
                net_by_name = dict(board_obj.GetNetsByName())
        except Exception:
            net_by_name = {}

        def _ensure_net(name: str):
            if name in net_by_name:
                return net_by_name[name]
            try:
                ni = pcbnew.NETINFO_ITEM(board_obj, name)
                # Add to board
                board_obj.Add(ni)
                net_by_name[name] = ni
                return ni
            except Exception:
                return None

        # Parse netlist nets
        for net in root.findall(".//nets/net"):
            net_name = (net.findtext("name") or "").strip()
            if not net_name:
                continue
            ni = _ensure_net(net_name)
            if ni is None:
                continue

            for node in net.findall("node"):
                ref = node.get("ref") or ""
                pin = node.get("pin") or ""
                if not ref or not pin:
                    continue
                fp = existing.get(ref)
                if not fp:
                    continue
                try:
                    pad = fp.FindPadByNumber(pin)
                    if pad:
                        pad.SetNet(ni)
                except Exception:
                    continue

        # 8) save
        try:
            pcbnew.SaveBoard(str(pcb_path), board_obj)
        except Exception as e:
            return False, f"Failed to save PCB: {e}"

        # cleanup temp
        try:
            if temp_netlist.exists():
                temp_netlist.unlink()
        except Exception:
            pass
        try:
            if filtered.exists() and filtered != temp_netlist:
                filtered.unlink()
        except Exception:
            pass

        # update placement tracking
        try:
            self.scan_placed_components()
        except Exception as e:
            self._log(f"scan_placed_components failed: {e}")

        self._log(f"Update done board={board_name} added={added} skipped_no_fp={skipped_no_fp} failed_load={failed_load} open={using_open}")
        msg = f"Updated {board_def.pcb_filename}\nAdded: {added}\nSkipped (no footprint): {skipped_no_fp}\nFailed to load: {failed_load}"
        if exclude_refs:
            msg += f"\nExcluded (already on other boards): {len(exclude_refs)}"
        return True, msg

    def _filter_netlist(self, netlist_path: Path, exclude_refs: Set[str]) -> Path:
        """Create a filtered netlist excluding specified references."""
        
        filtered_path = netlist_path.with_suffix('.filtered.xml')
        
        try:
            tree = ET.parse(netlist_path)
            root = tree.getroot()
            
            # Find and remove excluded components
            components = root.find('components')
            if components is not None:
                to_remove = []
                for comp in components.findall('comp'):
                    ref = comp.get('ref')
                    if ref in exclude_refs:
                        to_remove.append(comp)
                
                for comp in to_remove:
                    components.remove(comp)
            
            tree.write(filtered_path, encoding='utf-8', xml_declaration=True)
            return filtered_path
            
        except Exception:
            # If filtering fails, return original
            return netlist_path
    
    # -------------------------------------------------------------------------
    # Cleanup Auto-Generated Files
    # -------------------------------------------------------------------------
    
    def cleanup_auto_generated_files(self) -> List[str]:
        """Remove auto-generated .kicad_pro and .kicad_sch files for sub-boards."""
        
        cleaned = []
        root_base = Path(self.project.root_schematic).stem if self.project.root_schematic else ""
        
        for board in self.project.boards.values():
            base_name = Path(board.pcb_filename).stem
            
            # Skip if this is the root project
            if base_name == root_base:
                continue
            
            # Check for auto-created project file
            pro_file = self.project_path / f"{base_name}.kicad_pro"
            if pro_file.exists():
                try:
                    pro_file.unlink()
                    cleaned.append(pro_file.name)
                except IOError:
                    pass
            
            # Check for auto-created schematic file (if it's not the root)
            sch_file = self.project_path / f"{base_name}.kicad_sch"
            if sch_file.exists() and sch_file.name != self.project.root_schematic:
                try:
                    sch_file.unlink()
                    cleaned.append(sch_file.name)
                except IOError:
                    pass
        
        return cleaned
    
    # -------------------------------------------------------------------------
    # Board Block Detection
    # -------------------------------------------------------------------------
    
    def get_board_from_footprint(self, footprint_ref: str) -> Optional[str]:
        """Get board name from a board block footprint reference."""
        
        # Load the root PCB
        if not self.project.root_pcb:
            return None
        
        root_pcb_path = self.project_path / self.project.root_pcb
        if not root_pcb_path.exists():
            return None
        
        try:
            board = pcbnew.LoadBoard(str(root_pcb_path))
            
            for fp in board.GetFootprints():
                if fp.GetReference() == footprint_ref:
                    # Check if it's a board block footprint
                    fp_name = fp.GetFPIDAsString()
                    if BOARD_BLOCK_PREFIX in fp_name:
                        # Get PCB_File property
                        pcb_file = fp.GetProperty("PCB_File")
                        if pcb_file:
                            # Find board by filename
                            for name, brd in self.project.boards.items():
                                if brd.pcb_filename == pcb_file:
                                    return name
                        
                        # Fallback: extract from footprint name
                        match = re.search(f'{BOARD_BLOCK_PREFIX}(.+)', fp_name.split(':')[-1])
                        if match:
                            return match.group(1)
        except Exception:
            pass
        
        return None
    
    def get_selected_board_block(self, pcb_board: pcbnew.BOARD) -> Optional[str]:
        """Get the board name if a board block footprint is selected."""
        
        # Get selected footprints
        for fp in pcb_board.GetFootprints():
            if fp.IsSelected():
                fp_name = fp.GetFPIDAsString()
                if BOARD_BLOCK_PREFIX in fp_name:
                    # Get PCB_File property
                    try:
                        pcb_file = fp.GetProperty("PCB_File")
                        if pcb_file:
                            for name, board in self.project.boards.items():
                                if board.pcb_filename == pcb_file:
                                    return name
                    except:
                        pass
                    
                    # Fallback: extract from footprint name
                    match = re.search(f'{BOARD_BLOCK_PREFIX}(.+)', fp_name.split(':')[-1])
                    if match:
                        return match.group(1)
        
        return None


# ============================================================================
# Dialog Classes
# ============================================================================

class NewSubBoardDialog(wx.Dialog):
    """Dialog to create a new sub-board."""
    
    def __init__(self, parent, project_mgr: ProjectManager):
        super().__init__(parent, title="New Sub-Board PCB", size=(500, 450))
        
        self.project_mgr = project_mgr
        self.board: Optional[SubBoardDefinition] = None
        
        self.init_ui()
    
    def init_ui(self):
        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        # Info
        info = wx.StaticText(
            panel,
            label="Create a new sub-board PCB.\n"
                  "A board block footprint will be created for the root PCB."
        )
        main_sizer.Add(info, 0, wx.ALL, 10)
        
        # Form
        grid = wx.FlexGridSizer(5, 2, 10, 10)
        grid.AddGrowableCol(1, 1)
        
        # Name
        grid.Add(wx.StaticText(panel, label="Board Name:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_name = wx.TextCtrl(panel)
        self.txt_name.Bind(wx.EVT_TEXT, self.on_name_changed)
        grid.Add(self.txt_name, 1, wx.EXPAND)
        
        # Filename
        grid.Add(wx.StaticText(panel, label="PCB Filename:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_filename = wx.TextCtrl(panel)
        self.txt_filename.SetBackgroundColour(wx.Colour(245, 245, 245))
        grid.Add(self.txt_filename, 1, wx.EXPAND)
        
        # Layers
        grid.Add(wx.StaticText(panel, label="Copper Layers:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.spin_layers = wx.SpinCtrl(panel, min=2, max=32, initial=4)
        grid.Add(self.spin_layers, 0)
        
        # Block size
        grid.Add(wx.StaticText(panel, label="Block Size (W x H mm):"), 0, wx.ALIGN_CENTER_VERTICAL)
        size_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.spin_width = wx.SpinCtrlDouble(panel, min=20, max=200, initial=50, inc=5)
        size_sizer.Add(self.spin_width, 0)
        size_sizer.Add(wx.StaticText(panel, label=" x "), 0, wx.ALIGN_CENTER_VERTICAL)
        self.spin_height = wx.SpinCtrlDouble(panel, min=15, max=150, initial=35, inc=5)
        size_sizer.Add(self.spin_height, 0)
        grid.Add(size_sizer, 0)
        
        # Description
        grid.Add(wx.StaticText(panel, label="Description:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_desc = wx.TextCtrl(panel)
        grid.Add(self.txt_desc, 1, wx.EXPAND)
        
        main_sizer.Add(grid, 0, wx.EXPAND | wx.ALL, 15)
        
        # Info about workflow
        workflow_box = wx.StaticBox(panel, label="What will be created")
        workflow_sizer = wx.StaticBoxSizer(workflow_box, wx.VERTICAL)
        
        workflow_text = wx.StaticText(
            panel,
            label="1. A new .kicad_pcb file with the specified layer count\n"
                  "2. A board block footprint for the root PCB diagram\n"
                  "3. Use 'Update from Root Schematic' to add components"
        )
        workflow_text.SetForegroundColour(wx.Colour(80, 80, 80))
        workflow_sizer.Add(workflow_text, 0, wx.ALL, 10)
        
        main_sizer.Add(workflow_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 15)
        
        main_sizer.AddStretchSpacer()
        
        # Buttons
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_sizer.AddStretchSpacer()
        
        self.btn_create = wx.Button(panel, label="Create")
        self.btn_cancel = wx.Button(panel, label="Cancel")
        
        self.btn_create.Bind(wx.EVT_BUTTON, self.on_create)
        self.btn_cancel.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CANCEL))
        
        btn_sizer.Add(self.btn_create, 0, wx.ALL, 5)
        btn_sizer.Add(self.btn_cancel, 0, wx.ALL, 5)
        
        main_sizer.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 10)
        
        panel.SetSizer(main_sizer)
        self.btn_create.SetDefault()
    
    def on_name_changed(self, event):
        name = self.txt_name.GetValue().strip()
        if name:
            safe_name = "".join(c if c.isalnum() or c in "_-" else "_" for c in name)
            self.txt_filename.SetValue(f"{BOARDS_DIR_NAME}/{safe_name}/{safe_name}.kicad_pcb")
        else:
            self.txt_filename.SetValue("")
    
    def on_create(self, event):
        name = self.txt_name.GetValue().strip()
        if not name:
            wx.MessageBox("Please enter a board name.", "Error", wx.OK | wx.ICON_ERROR)
            return
        
        if name in self.project_mgr.project.boards:
            wx.MessageBox("A board with this name already exists.", "Error", wx.OK | wx.ICON_ERROR)
            return
        
        filename = self.txt_filename.GetValue().strip()
        if not filename:
            wx.MessageBox("Invalid filename.", "Error", wx.OK | wx.ICON_ERROR)
            return
        
        # Calculate position for new board block
        existing_count = len(self.project_mgr.project.boards)
        block_x = 50 + (existing_count % 3) * 70
        block_y = 50 + (existing_count // 3) * 50
        
        self.board = SubBoardDefinition(
            name=name,
            pcb_filename=filename,
            layers=self.spin_layers.GetValue(),
            description=self.txt_desc.GetValue().strip(),
            block_width=self.spin_width.GetValue(),
            block_height=self.spin_height.GetValue(),
            block_x=block_x,
            block_y=block_y,
        )
        
        self.EndModal(wx.ID_OK)


class PortEditorDialog(wx.Dialog):
    """Dialog to edit ports on a sub-board."""
    
    def __init__(self, parent, board: SubBoardDefinition, project_mgr: ProjectManager):
        super().__init__(parent, title=f"Edit Ports - {board.name}", size=(700, 500))
        
        self.board = board
        self.project_mgr = project_mgr
        self.ports = {k: PortDefinition(**asdict(v)) if isinstance(v, PortDefinition) else PortDefinition.from_dict(v) 
                      for k, v in board.ports.items()}
        
        self.init_ui()
        self.refresh_list()
    
    def init_ui(self):
        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        # Info
        info = wx.StaticText(
            panel,
            label="Define inter-board ports. These become pads on the board block footprint.\n"
                  "Connect ports with traces in the root PCB to show inter-board connections."
        )
        main_sizer.Add(info, 0, wx.ALL, 10)
        
        # Port list
        self.list_ctrl = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.list_ctrl.InsertColumn(0, "Port Name", width=120)
        self.list_ctrl.InsertColumn(1, "Direction", width=80)
        self.list_ctrl.InsertColumn(2, "Net Name", width=120)
        self.list_ctrl.InsertColumn(3, "Side", width=70)
        self.list_ctrl.InsertColumn(4, "Position", width=70)
        main_sizer.Add(self.list_ctrl, 1, wx.EXPAND | wx.ALL, 10)
        
        # Port buttons
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        self.btn_add = wx.Button(panel, label="Add Port")
        self.btn_edit = wx.Button(panel, label="Edit Port")
        self.btn_remove = wx.Button(panel, label="Remove Port")
        
        self.btn_add.Bind(wx.EVT_BUTTON, self.on_add)
        self.btn_edit.Bind(wx.EVT_BUTTON, self.on_edit)
        self.btn_remove.Bind(wx.EVT_BUTTON, self.on_remove)
        
        btn_sizer.Add(self.btn_add, 0, wx.ALL, 5)
        btn_sizer.Add(self.btn_edit, 0, wx.ALL, 5)
        btn_sizer.Add(self.btn_remove, 0, wx.ALL, 5)
        
        main_sizer.Add(btn_sizer, 0, wx.LEFT, 5)
        
        # Separator
        main_sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.ALL, 10)
        
        # Dialog buttons
        dialog_btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        dialog_btn_sizer.AddStretchSpacer()
        
        self.btn_ok = wx.Button(panel, label="Save && Regenerate Footprint")
        self.btn_cancel = wx.Button(panel, label="Cancel")
        
        self.btn_ok.Bind(wx.EVT_BUTTON, self.on_ok)
        self.btn_cancel.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CANCEL))
        
        dialog_btn_sizer.Add(self.btn_ok, 0, wx.ALL, 5)
        dialog_btn_sizer.Add(self.btn_cancel, 0, wx.ALL, 5)
        
        main_sizer.Add(dialog_btn_sizer, 0, wx.EXPAND | wx.ALL, 10)
        
        panel.SetSizer(main_sizer)
    
    def refresh_list(self):
        self.list_ctrl.DeleteAllItems()
        for name, port in self.ports.items():
            idx = self.list_ctrl.InsertItem(self.list_ctrl.GetItemCount(), name)
            self.list_ctrl.SetItem(idx, 1, port.direction)
            self.list_ctrl.SetItem(idx, 2, port.net_name)
            self.list_ctrl.SetItem(idx, 3, port.side)
            self.list_ctrl.SetItem(idx, 4, f"{port.position:.2f}")
    
    def on_add(self, event):
        dlg = SinglePortDialog(self, self.board)
        if dlg.ShowModal() == wx.ID_OK and dlg.port:
            self.ports[dlg.port.name] = dlg.port
            self.refresh_list()
        dlg.Destroy()
    
    def on_edit(self, event):
        idx = self.list_ctrl.GetFirstSelected()
        if idx < 0:
            wx.MessageBox("Please select a port to edit.", "No Selection", wx.OK | wx.ICON_WARNING)
            return
        
        port_name = self.list_ctrl.GetItemText(idx)
        port = self.ports.get(port_name)
        
        if port:
            dlg = SinglePortDialog(self, self.board, port)
            if dlg.ShowModal() == wx.ID_OK and dlg.port:
                # Handle rename
                if dlg.port.name != port_name:
                    del self.ports[port_name]
                self.ports[dlg.port.name] = dlg.port
                self.refresh_list()
            dlg.Destroy()
    
    def on_remove(self, event):
        idx = self.list_ctrl.GetFirstSelected()
        if idx < 0:
            wx.MessageBox("Please select a port to remove.", "No Selection", wx.OK | wx.ICON_WARNING)
            return
        
        port_name = self.list_ctrl.GetItemText(idx)
        if wx.MessageBox(f"Remove port '{port_name}'?", "Confirm", wx.YES_NO | wx.ICON_QUESTION) == wx.YES:
            del self.ports[port_name]
            self.refresh_list()
    
    def on_ok(self, event):
        self.board.ports = self.ports
        
        # Regenerate footprint
        success, msg = self.project_mgr.generate_board_footprint(self.board)
        if success:
            self.project_mgr.save()
            wx.MessageBox(
                f"Ports saved and footprint regenerated.\n\n"
                f"Note: You may need to update the footprint in the root PCB\n"
                f"(delete old block, re-add from library).",
                "Success",
                wx.OK | wx.ICON_INFORMATION
            )
            self.EndModal(wx.ID_OK)
        else:
            wx.MessageBox(f"Failed to regenerate footprint: {msg}", "Error", wx.OK | wx.ICON_ERROR)


class SinglePortDialog(wx.Dialog):
    """Dialog for adding/editing a single port."""
    
    def __init__(self, parent, board: SubBoardDefinition, port: Optional[PortDefinition] = None):
        title = "Edit Port" if port else "Add Port"
        super().__init__(parent, title=title, size=(400, 350))
        
        self.board = board
        self.port: Optional[PortDefinition] = None
        self.edit_port = port
        
        self.init_ui()
        
        if port:
            self.populate_from_port(port)
    
    def init_ui(self):
        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        grid = wx.FlexGridSizer(5, 2, 10, 10)
        grid.AddGrowableCol(1, 1)
        
        # Port name
        grid.Add(wx.StaticText(panel, label="Port Name:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_name = wx.TextCtrl(panel)
        grid.Add(self.txt_name, 1, wx.EXPAND)
        
        # Direction
        grid.Add(wx.StaticText(panel, label="Direction:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.combo_dir = wx.ComboBox(
            panel, 
            choices=["input", "output", "bidir"],
            style=wx.CB_READONLY
        )
        self.combo_dir.SetSelection(2)
        grid.Add(self.combo_dir, 1, wx.EXPAND)
        
        # Net name
        grid.Add(wx.StaticText(panel, label="Net Name:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_net = wx.TextCtrl(panel)
        self.txt_net.SetHint("Optional - hierarchical label name")
        grid.Add(self.txt_net, 1, wx.EXPAND)
        
        # Side
        grid.Add(wx.StaticText(panel, label="Side:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.combo_side = wx.ComboBox(
            panel,
            choices=["left", "right", "top", "bottom"],
            style=wx.CB_READONLY
        )
        self.combo_side.SetSelection(1)  # right
        grid.Add(self.combo_side, 1, wx.EXPAND)
        
        # Position
        grid.Add(wx.StaticText(panel, label="Position (0-1):"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.spin_pos = wx.SpinCtrlDouble(panel, min=0, max=1, initial=0.5, inc=0.1)
        self.spin_pos.SetDigits(2)
        grid.Add(self.spin_pos, 0)
        
        main_sizer.Add(grid, 0, wx.EXPAND | wx.ALL, 20)
        
        # Help text
        help_text = wx.StaticText(
            panel,
            label="Position: 0.0 = start of side, 1.0 = end of side\n"
                  "Direction affects pad shape: input=rect, output=rounded, bidir=circle"
        )
        help_text.SetForegroundColour(wx.Colour(100, 100, 100))
        main_sizer.Add(help_text, 0, wx.LEFT | wx.RIGHT, 20)
        
        main_sizer.AddStretchSpacer()
        
        # Buttons
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_sizer.AddStretchSpacer()
        
        self.btn_add = wx.Button(panel, label="Add" if not self.edit_port else "Save")
        self.btn_cancel = wx.Button(panel, label="Cancel")
        
        self.btn_add.Bind(wx.EVT_BUTTON, self.on_add)
        self.btn_cancel.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CANCEL))
        
        btn_sizer.Add(self.btn_add, 0, wx.ALL, 5)
        btn_sizer.Add(self.btn_cancel, 0, wx.ALL, 5)
        
        main_sizer.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 10)
        
        panel.SetSizer(main_sizer)
        self.btn_add.SetDefault()
    
    def populate_from_port(self, port: PortDefinition):
        self.txt_name.SetValue(port.name)
        
        dir_map = {"input": 0, "output": 1, "bidir": 2}
        self.combo_dir.SetSelection(dir_map.get(port.direction, 2))
        
        self.txt_net.SetValue(port.net_name)
        
        side_map = {"left": 0, "right": 1, "top": 2, "bottom": 3}
        self.combo_side.SetSelection(side_map.get(port.side, 1))
        
        self.spin_pos.SetValue(port.position)
    
    def on_add(self, event):
        name = self.txt_name.GetValue().strip()
        if not name:
            wx.MessageBox("Please enter a port name.", "Error", wx.OK | wx.ICON_ERROR)
            return
        
        self.port = PortDefinition(
            name=name,
            direction=self.combo_dir.GetValue(),
            net_name=self.txt_net.GetValue().strip(),
            side=self.combo_side.GetValue(),
            position=self.spin_pos.GetValue()
        )
        
        self.EndModal(wx.ID_OK)


class ComponentStatusDialog(wx.Dialog):
    """Dialog showing component placement status across all boards."""
    
    def __init__(self, parent, project_mgr: ProjectManager):
        super().__init__(parent, title="Component Placement Status", size=(700, 500))
        
        self.project_mgr = project_mgr
        self.init_ui()
        self.refresh_data()
    
    def init_ui(self):
        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        # Summary
        self.summary_text = wx.StaticText(panel, label="")
        main_sizer.Add(self.summary_text, 0, wx.ALL, 10)
        
        # Notebook for tabs
        notebook = wx.Notebook(panel)
        
        # Tab 1: Placed components
        placed_panel = wx.Panel(notebook)
        placed_sizer = wx.BoxSizer(wx.VERTICAL)
        self.placed_list = wx.ListCtrl(placed_panel, style=wx.LC_REPORT)
        self.placed_list.InsertColumn(0, "Reference", width=100)
        self.placed_list.InsertColumn(1, "Board", width=150)
        placed_sizer.Add(self.placed_list, 1, wx.EXPAND | wx.ALL, 5)
        placed_panel.SetSizer(placed_sizer)
        notebook.AddPage(placed_panel, "Placed Components")
        
        # Tab 2: Unplaced components
        unplaced_panel = wx.Panel(notebook)
        unplaced_sizer = wx.BoxSizer(wx.VERTICAL)
        self.unplaced_list = wx.ListCtrl(unplaced_panel, style=wx.LC_REPORT)
        self.unplaced_list.InsertColumn(0, "Reference", width=100)
        self.unplaced_list.InsertColumn(1, "Status", width=150)
        unplaced_sizer.Add(self.unplaced_list, 1, wx.EXPAND | wx.ALL, 5)
        unplaced_panel.SetSizer(unplaced_sizer)
        notebook.AddPage(unplaced_panel, "Unplaced Components")
        
        main_sizer.Add(notebook, 1, wx.EXPAND | wx.ALL, 10)
        
        # Buttons
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_refresh = wx.Button(panel, label="Refresh")
        btn_refresh.Bind(wx.EVT_BUTTON, lambda e: self.refresh_data())
        btn_close = wx.Button(panel, label="Close")
        btn_close.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        
        btn_sizer.Add(btn_refresh, 0, wx.ALL, 5)
        btn_sizer.AddStretchSpacer()
        btn_sizer.Add(btn_close, 0, wx.ALL, 5)
        
        main_sizer.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 10)
        
        panel.SetSizer(main_sizer)
    
    def refresh_data(self):
        # Scan all PCBs
        self.project_mgr.scan_placed_components()
        placement = self.project_mgr.project.component_placement
        unplaced = self.project_mgr.get_unplaced_components()
        
        # Update summary
        self.summary_text.SetLabel(
            f"Total placed: {len(placement)} | Unplaced: {len(unplaced)} | "
            f"Boards: {len(self.project_mgr.project.boards)}"
        )
        
        # Update placed list
        self.placed_list.DeleteAllItems()
        for ref, board in sorted(placement.items()):
            idx = self.placed_list.InsertItem(self.placed_list.GetItemCount(), ref)
            self.placed_list.SetItem(idx, 1, board)
        
        # Update unplaced list
        self.unplaced_list.DeleteAllItems()
        for ref in sorted(unplaced):
            idx = self.unplaced_list.InsertItem(self.unplaced_list.GetItemCount(), ref)
            self.unplaced_list.SetItem(idx, 1, "Not placed on any board")


class MainDialog(wx.Dialog):
    """Main plugin dialog."""
    
    def __init__(self, parent, board: pcbnew.BOARD):
        super().__init__(
            parent,
            title="Multi-Board PCB Manager v5",
            size=(900, 700),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER
        )
        
        self.current_board = board
        
        board_path = board.GetFileName()
        if board_path:
            self.project_path = Path(board_path).parent
        else:
            self.project_path = Path.cwd()
        
        self.project_mgr = ProjectManager(self.project_path)
        
        self.init_ui()
        self.bind_events()
        self.refresh_all()
    
    def init_ui(self):
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        # Header
        header = wx.Panel(self)
        header_sizer = wx.BoxSizer(wx.VERTICAL)
        
        title = wx.StaticText(header, label="Multi-Board PCB Manager")
        font = title.GetFont()
        font.SetPointSize(14)
        font.SetWeight(wx.FONTWEIGHT_BOLD)
        title.SetFont(font)
        header_sizer.Add(title, 0, wx.ALL, 10)
        
        # Project info grid
        info_grid = wx.FlexGridSizer(2, 2, 5, 20)
        info_grid.Add(wx.StaticText(header, label="Root Schematic:"), 0)
        self.lbl_root_sch = wx.StaticText(header, label=self.project_mgr.project.root_schematic or "(not found)")
        info_grid.Add(self.lbl_root_sch, 0)
        info_grid.Add(wx.StaticText(header, label="Root PCB:"), 0)
        self.lbl_root_pcb = wx.StaticText(header, label=self.project_mgr.project.root_pcb or "(not found)")
        info_grid.Add(self.lbl_root_pcb, 0)
        header_sizer.Add(info_grid, 0, wx.LEFT | wx.BOTTOM, 10)
        
        header.SetSizer(header_sizer)
        main_sizer.Add(header, 0, wx.EXPAND)
        
        # Sub-boards section
        boards_label = wx.StaticText(self, label="Sub-Board PCBs:")
        main_sizer.Add(boards_label, 0, wx.LEFT | wx.TOP, 10)
        
        self.board_list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.board_list.InsertColumn(0, "Board Name", width=130)
        self.board_list.InsertColumn(1, "PCB File", width=180)
        self.board_list.InsertColumn(2, "Layers", width=55)
        self.board_list.InsertColumn(3, "Ports", width=50)
        self.board_list.InsertColumn(4, "Components", width=90)
        self.board_list.InsertColumn(5, "Description", width=200)
        main_sizer.Add(self.board_list, 1, wx.EXPAND | wx.ALL, 10)
        
        # Board action buttons
        board_btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        self.btn_new = wx.Button(self, label="New Sub-Board")
        self.btn_edit_ports = wx.Button(self, label="Edit Ports")
        self.btn_remove = wx.Button(self, label="Remove")
        self.btn_open = wx.Button(self, label="Open PCB")
        self.btn_update = wx.Button(self, label="Update from Root Schematic")
        
        board_btn_sizer.Add(self.btn_new, 0, wx.ALL, 3)
        board_btn_sizer.Add(self.btn_edit_ports, 0, wx.ALL, 3)
        board_btn_sizer.Add(self.btn_remove, 0, wx.ALL, 3)
        board_btn_sizer.AddSpacer(20)
        board_btn_sizer.Add(self.btn_open, 0, wx.ALL, 3)
        board_btn_sizer.Add(self.btn_update, 0, wx.ALL, 3)
        
        main_sizer.Add(board_btn_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 7)
        
        # Separator
        main_sizer.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.ALL, 10)
        
        # Tools section
        tools_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        self.btn_place_block = wx.Button(self, label="Place Board Block in Root PCB")
        self.btn_open_selected = wx.Button(self, label="Open Selected Board Block")
        self.btn_component_status = wx.Button(self, label="Component Status")
        self.btn_cleanup = wx.Button(self, label="Cleanup Auto-Files")
        self.btn_reload_pcb = wx.Button(self, label="Reload PCB (Revert)")
        self.btn_open_schematic = wx.Button(self, label="Open Board Schematic")
        self.btn_open_debug_log = wx.Button(self, label="Open Debug Log")
        
        tools_sizer.Add(self.btn_place_block, 0, wx.ALL, 5)
        tools_sizer.Add(self.btn_open_selected, 0, wx.ALL, 5)
        tools_sizer.AddSpacer(20)
        tools_sizer.Add(self.btn_component_status, 0, wx.ALL, 5)
        tools_sizer.Add(self.btn_open_schematic, 0, wx.ALL, 5)
        tools_sizer.Add(self.btn_reload_pcb, 0, wx.ALL, 5)
        tools_sizer.Add(self.btn_open_debug_log, 0, wx.ALL, 5)
        tools_sizer.AddStretchSpacer()
        tools_sizer.Add(self.btn_cleanup, 0, wx.ALL, 5)
        
        main_sizer.Add(tools_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 5)
        
        # Bottom buttons
        bottom_sizer = wx.BoxSizer(wx.HORIZONTAL)
        bottom_sizer.AddStretchSpacer()
        btn_close = wx.Button(self, label="Close")
        btn_close.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        bottom_sizer.Add(btn_close, 0, wx.ALL, 10)
        
        main_sizer.Add(bottom_sizer, 0, wx.EXPAND)
        
        self.SetSizer(main_sizer)
    
    def bind_events(self):
        self.btn_new.Bind(wx.EVT_BUTTON, self.on_new_board)
        self.btn_edit_ports.Bind(wx.EVT_BUTTON, self.on_edit_ports)
        self.btn_remove.Bind(wx.EVT_BUTTON, self.on_remove_board)
        self.btn_open.Bind(wx.EVT_BUTTON, self.on_open_pcb)
        self.btn_update.Bind(wx.EVT_BUTTON, self.on_update_from_schematic)
        
        self.btn_place_block.Bind(wx.EVT_BUTTON, self.on_place_block)
        self.btn_open_selected.Bind(wx.EVT_BUTTON, self.on_open_selected_block)
        self.btn_component_status.Bind(wx.EVT_BUTTON, self.on_component_status)
        self.btn_open_schematic.Bind(wx.EVT_BUTTON, self.on_open_board_schematic)
        self.btn_reload_pcb.Bind(wx.EVT_BUTTON, self.on_reload_pcb)
        self.btn_open_debug_log.Bind(wx.EVT_BUTTON, self.on_open_debug_log)
        self.btn_cleanup.Bind(wx.EVT_BUTTON, self.on_cleanup)
        
        # Double-click to open
        self.board_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_open_pcb)
    
    def refresh_all(self):
        self.board_list.DeleteAllItems()
        
        # Refresh component counts
        self.project_mgr.scan_placed_components()
        
        for name, board in self.project_mgr.project.boards.items():
            idx = self.board_list.InsertItem(self.board_list.GetItemCount(), name)
            self.board_list.SetItem(idx, 1, board.pcb_filename)
            self.board_list.SetItem(idx, 2, str(board.layers))
            self.board_list.SetItem(idx, 3, str(len(board.ports)))
            
            # Count components on this board
            comp_count = sum(1 for b in self.project_mgr.project.component_placement.values() if b == name)
            self.board_list.SetItem(idx, 4, str(comp_count))
            
            self.board_list.SetItem(idx, 5, board.description)
    
    def get_selected_board(self) -> Optional[str]:
        idx = self.board_list.GetFirstSelected()
        if idx >= 0:
            return self.board_list.GetItemText(idx)
        return None
    
    def on_new_board(self, event):
        dlg = NewSubBoardDialog(self, self.project_mgr)
        if dlg.ShowModal() == wx.ID_OK and dlg.board:
            board = dlg.board
            
            # Create PCB file
            success, msg = self.project_mgr.create_sub_board_pcb(board)
            if not success:
                wx.MessageBox(msg, "Error", wx.OK | wx.ICON_ERROR)
                dlg.Destroy()
                return
            
            # Generate footprint
            self.project_mgr.ensure_footprint_library()
            self.project_mgr.add_footprint_lib_to_project()
            
            fp_success, fp_msg = self.project_mgr.generate_board_footprint(board)
            
            # Add to project
            self.project_mgr.project.boards[board.name] = board
            self.project_mgr.save()
            
            self.refresh_all()
            
            result_msg = f"Created sub-board: {board.name}\n"
            result_msg += f"PCB file: {board.pcb_filename}\n"
            if fp_success:
                result_msg += f"Footprint: {FOOTPRINT_LIB_NAME}:{BOARD_BLOCK_PREFIX}{board.name}\n\n"
                result_msg += "Next steps:\n"
                result_msg += "1. Use 'Place Board Block in Root PCB' to add to diagram\n"
                result_msg += "2. Use 'Update from Root Schematic' to add components\n"
                result_msg += "3. Open the sub-PCB and layout your design"
            
            wx.MessageBox(result_msg, "Success", wx.OK | wx.ICON_INFORMATION)
        
        dlg.Destroy()
    
    def on_edit_ports(self, event):
        name = self.get_selected_board()
        if not name:
            wx.MessageBox("Please select a board.", "No Selection", wx.OK | wx.ICON_WARNING)
            return
        
        board = self.project_mgr.project.boards.get(name)
        if board:
            dlg = PortEditorDialog(self, board, self.project_mgr)
            if dlg.ShowModal() == wx.ID_OK:
                self.refresh_all()
            dlg.Destroy()
    
    def on_remove_board(self, event):
        name = self.get_selected_board()
        if not name:
            wx.MessageBox("Please select a board.", "No Selection", wx.OK | wx.ICON_WARNING)
            return
        
        if wx.MessageBox(
            f"Remove '{name}' from project?\n\n"
            "The PCB file will NOT be deleted.",
            "Confirm",
            wx.YES_NO | wx.ICON_QUESTION
        ) == wx.YES:
            del self.project_mgr.project.boards[name]
            self.project_mgr.save()
            self.refresh_all()
    
    def on_open_pcb(self, event):
        name = self.get_selected_board()
        if not name:
            wx.MessageBox("Please select a board.", "No Selection", wx.OK | wx.ICON_WARNING)
            return
        
        board = self.project_mgr.project.boards.get(name)
        if not board:
            return
        
        pcb_path = self.project_mgr.project_path / board.pcb_filename
        if not pcb_path.exists():
            wx.MessageBox(f"PCB file not found: {board.pcb_filename}", "Error", wx.OK | wx.ICON_ERROR)
            return
        
        try:
            subprocess.Popen(["pcbnew", str(pcb_path)])
        except Exception as e:
            wx.MessageBox(f"Failed to open pcbnew: {e}", "Error", wx.OK | wx.ICON_ERROR)
    
    def on_update_from_schematic(self, event):
        name = self.get_selected_board()
        if not name:
            wx.MessageBox("Please select a board.", "No Selection", wx.OK | wx.ICON_WARNING)
            return
        
        # Confirm
        if wx.MessageBox(
            f"Update '{name}' from root schematic?\n\n"
            "This will add all components that aren't already placed on other boards.\n"
            "Components already on this board will be updated.",
            "Confirm Update",
            wx.YES_NO | wx.ICON_QUESTION
        ) != wx.YES:
            return
        
        # Show progress
        busy = wx.BusyInfo("Updating from schematic...")
        
        # If the currently opened board is the one being updated, pass it so we update in-memory safely
        open_board = None
        try:
            target_path = (self.project_mgr.project_path / self.project_mgr.project.boards[name].pcb_filename).resolve()
            if self.current_board and Path(self.current_board.GetFileName()).resolve() == target_path:
                open_board = self.current_board
        except Exception:
            open_board = None

        success, msg = self.project_mgr.update_pcb_from_root_schematic(name, open_board=open_board)
        
        del busy
        
        if success:
            # Offer an immediate reload (Revert) so PCB editor refreshes from disk
            if wx.MessageBox(
                msg + "\n\nReload PCB now?",
                "Update Complete",
                wx.YES_NO | wx.ICON_INFORMATION
            ) == wx.YES:
                self.on_reload_pcb(None)
            self.refresh_all()
        else:
            wx.MessageBox(msg, "Update Failed", wx.OK | wx.ICON_ERROR)
    
    
    def _get_selected_or_current_board_name(self) -> Optional[str]:
        name = self.get_selected_board()
        if name:
            return name
        # Try infer from opened PCB filename
        try:
            fn = Path(self.current_board.GetFileName())
            stem = fn.stem
            if stem in self.project_mgr.project.boards:
                return stem
        except Exception:
            pass
        return None

    def on_open_board_schematic(self, event):
        """
        Open the per-board schematic file: boards/<BoardName>/<BoardName>.kicad_sch
        (this is a hardlink/symlink to the single root schematic).
        """
        name = self._get_selected_or_current_board_name()
        if not name:
            wx.MessageBox("Please select a board.", "No Selection", wx.OK | wx.ICON_WARNING)
            return
        board = self.project_mgr.project.boards.get(name)
        if not board:
            wx.MessageBox("Board not found in project config.", "Error", wx.OK | wx.ICON_ERROR)
            return

        pcb_path = (self.project_mgr.project_path / board.pcb_filename).resolve()
        board_dir = pcb_path.parent
        sch_path = board_dir / f"{pcb_path.stem}.kicad_sch"

        # Ensure links exist
        try:
            self.project_mgr._ensure_board_miniproject(board, pcb_path)
        except Exception:
            pass

        if not sch_path.exists():
            # fallback to root schematic
            sch_path = (self.project_mgr.project_path / self.project_mgr.project.root_schematic).resolve()

        try:
            os.startfile(str(sch_path))  # Windows
        except Exception as e:
            wx.MessageBox(f"Failed to open schematic: {e}", "Error", wx.OK | wx.ICON_ERROR)

    def on_open_debug_log(self, event):
        try:
            path = getattr(self.project_mgr, "debug_log_path", None)
            if not path:
                wx.MessageBox("Debug log path not available.", "Error", wx.OK | wx.ICON_ERROR)
                return
            os.startfile(str(path))
        except Exception as e:
            wx.MessageBox(f"Failed to open debug log: {e}", "Error", wx.OK | wx.ICON_ERROR)

    def _get_pcb_frame(self):
        # Best-effort: find a top-level window that looks like pcbnew's frame
        try:
            # Some KiCad builds expose pcbnew.GetFrame()
            if hasattr(pcbnew, "GetFrame"):
                fr = pcbnew.GetFrame()
                if fr:
                    return fr
        except Exception:
            pass

        try:
            for w in wx.GetTopLevelWindows():
                if hasattr(w, "GetBoard") or hasattr(w, "OnRevert") or hasattr(w, "Revert"):
                    return w
        except Exception:
            pass
        return None

    def on_reload_pcb(self, event):
        """
        Reload current PCB in the PCB editor (File -> Revert).
        """
        frame = self._get_pcb_frame()
        if not frame:
            wx.MessageBox("Could not find PCB editor window. Use File -> Revert.", "Reload", wx.OK | wx.ICON_WARNING)
            return
        try:
            # KiCad variants use different names
            if hasattr(frame, "Revert"):
                try:
                    frame.Revert()
                    return
                except Exception:
                    pass
            if hasattr(frame, "OnRevert"):
                frame.OnRevert(None)
                return
            # As a last resort, try menu command if exposed
            if hasattr(frame, "ProcessEvent"):
                # not guaranteed
                pass
        except Exception as e:
            wx.MessageBox(f"Reload failed: {e}", "Reload", wx.OK | wx.ICON_ERROR)
            return

        wx.MessageBox("Reload not available via API. Use File -> Revert.", "Reload", wx.OK | wx.ICON_WARNING)

    def on_place_block(self, event):
        name = self.get_selected_board()
        if not name:
            wx.MessageBox("Please select a board.", "No Selection", wx.OK | wx.ICON_WARNING)
            return
        
        board = self.project_mgr.project.boards.get(name)
        if not board:
            return
        
        # Ensure footprint exists
        self.project_mgr.generate_board_footprint(board)
        self.project_mgr.add_footprint_lib_to_project()
        
        fp_name = f"{FOOTPRINT_LIB_NAME}:{BOARD_BLOCK_PREFIX}{board.name}"
        
        wx.MessageBox(
            f"To place the board block:\n\n"
            f"1. In the root PCB, press 'A' to add footprint\n"
            f"2. Search for: {fp_name}\n"
            f"3. Place it on the board\n"
            f"4. Connect port pads with traces to show inter-board connections\n\n"
            f"The footprint is in library: {FOOTPRINT_LIB_NAME}",
            "Place Board Block",
            wx.OK | wx.ICON_INFORMATION
        )
    
    def on_open_selected_block(self, event):
        """Open the PCB for the currently selected board block in the root PCB."""
        
        # Check if we're in the root PCB
        current_file = Path(self.current_board.GetFileName()).name if self.current_board.GetFileName() else ""
        
        if current_file != self.project_mgr.project.root_pcb:
            wx.MessageBox(
                "This function works when you have the root PCB open.\n"
                "Select a board block footprint in the root PCB, then click this button.",
                "Info",
                wx.OK | wx.ICON_INFORMATION
            )
            return
        
        # Find selected board block
        board_name = self.project_mgr.get_selected_board_block(self.current_board)
        
        if board_name:
            board = self.project_mgr.project.boards.get(board_name)
            if board:
                pcb_path = self.project_mgr.project_path / board.pcb_filename
                if pcb_path.exists():
                    subprocess.Popen(["pcbnew", str(pcb_path)])
                else:
                    wx.MessageBox(f"PCB file not found: {board.pcb_filename}", "Error", wx.OK | wx.ICON_ERROR)
            else:
                wx.MessageBox(f"Board '{board_name}' not found in project.", "Error", wx.OK | wx.ICON_ERROR)
        else:
            wx.MessageBox(
                "No board block selected.\n\n"
                "In the root PCB, select a board block footprint (click on it),\n"
                "then click this button to open its PCB.",
                "No Selection",
                wx.OK | wx.ICON_WARNING
            )
    
    def on_component_status(self, event):
        dlg = ComponentStatusDialog(self, self.project_mgr)
        dlg.ShowModal()
        dlg.Destroy()
        self.refresh_all()
    
    def on_cleanup(self, event):
        cleaned = self.project_mgr.cleanup_auto_generated_files()
        
        if cleaned:
            wx.MessageBox(
                f"Removed {len(cleaned)} auto-generated files:\n\n" + "\n".join(cleaned),
                "Cleanup Complete",
                wx.OK | wx.ICON_INFORMATION
            )
        else:
            wx.MessageBox(
                "No auto-generated files found to clean up.",
                "Cleanup",
                wx.OK | wx.ICON_INFORMATION
            )


# ============================================================================
# Plugin Registration
# ============================================================================

class MultiBoardPlugin(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "Multi-Board Manager"
        self.category = "Project Management"
        self.description = (
            "Hierarchical multi-board PCB management. "
            "Create sub-PCBs that share the root schematic. "
            "Board blocks with ports for inter-board connections."
        )
        self.show_toolbar_button = True
        self.icon_file_name = os.path.join(os.path.dirname(__file__), "icon.png")
    
    def Run(self):
        board = pcbnew.GetBoard()
        if not board:
            wx.MessageBox("Please open a PCB file first.", "No Board", wx.OK | wx.ICON_ERROR)
            return
        
        dlg = MainDialog(None, board)
        dlg.ShowModal()
        dlg.Destroy()


MultiBoardPlugin().register()