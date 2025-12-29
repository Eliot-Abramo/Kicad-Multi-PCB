"""
Multi-Board PCB Manager - Core Manager (Optimized)
===================================================

High-performance implementation with:
- Footprint caching
- Optimized XML parsing
- Parallel operations where safe
- Component auto-packing

Author: Eliot
License: MIT
"""

import json
import os
import re
import subprocess
import shutil
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Set, Tuple, Any
from concurrent.futures import ThreadPoolExecutor
import math

import pcbnew

from .constants import (
    BOARDS_DIR,
    CONFIG_FILE,
    BLOCK_LIB_NAME,
    PORT_LIB_NAME,
    TEMP_NETLIST_NAME,
    DEBUG_LOG_NAME,
    PACK_GRID_SPACING,
    PACK_MAX_PER_ROW,
)
from .config import ProjectConfig, BoardConfig, PortDef


# Pre-compiled regex patterns for performance
RE_FP_LIB_ENTRY = re.compile(r'\(name\s*"([^"]+)"\).*?\(uri\s*"([^"]+)"\)', re.DOTALL)
RE_SHEET_REF = re.compile(r'"([^"]+\.kicad_sch)"')


class FootprintResolver:
    """
    Footprint loading with library path resolution.
    
    Caches library paths (not footprints) to speed up lookups.
    Each footprint is loaded fresh to avoid KiCad SWIG issues.
    """
    
    def __init__(self):
        self._lib_paths: Dict[str, Path] = {}
        self._kicad_share: Optional[Path] = None
        self._failed: Set[str] = set()  # Cache failed lookups
    
    def set_lib_paths(self, paths: Dict[str, Path], kicad_share: Optional[Path]):
        """Set library path mappings."""
        self._lib_paths = paths
        self._kicad_share = kicad_share
        self._failed.clear()
    
    def load(self, lib_nick: str, fp_name: str) -> Optional[pcbnew.FOOTPRINT]:
        """
        Load a footprint from library.
        
        Each call loads a fresh footprint to avoid SWIG/Clone issues.
        """
        cache_key = f"{lib_nick}:{fp_name}"
        
        # Skip known failures
        if cache_key in self._failed:
            return None
        
        fp = self._try_load(lib_nick, fp_name)
        if fp is None:
            self._failed.add(cache_key)
        return fp
    
    def _try_load(self, lib_nick: str, fp_name: str) -> Optional[pcbnew.FOOTPRINT]:
        """Attempt to load footprint from various sources."""
        # Try project library path first
        if lib_nick in self._lib_paths:
            try:
                return pcbnew.FootprintLoad(str(self._lib_paths[lib_nick]), fp_name)
            except Exception:
                pass
        
        # Try KiCad standard library
        if self._kicad_share:
            std_path = self._kicad_share / "footprints" / f"{lib_nick}.pretty"
            if std_path.exists():
                try:
                    return pcbnew.FootprintLoad(str(std_path), fp_name)
                except Exception:
                    pass
        
        # Try direct loading (absolute path or global lib)
        try:
            return pcbnew.FootprintLoad(lib_nick, fp_name)
        except Exception:
            pass
        
        return None
    
    def clear(self):
        """Clear failed lookup cache."""
        self._failed.clear()


class MultiBoardManager:
    """
    Main controller for multiboard project management.
    
    Optimized for performance with caching and minimal I/O.
    """
    
    def __init__(self, project_dir: Path):
        self.project_dir = self._find_project_root(project_dir)
        self.config_path = self.project_dir / CONFIG_FILE
        self.config = ProjectConfig()
        
        self.block_lib_path = self.project_dir / f"{BLOCK_LIB_NAME}.pretty"
        self.port_lib_path = self.project_dir / f"{PORT_LIB_NAME}.pretty"
        self.log_path = self.project_dir / DEBUG_LOG_NAME
        
        # Caches
        self._fp_resolver = FootprintResolver()
        self._fp_lib_paths: Dict[str, Path] = {}
        self._kicad_share: Optional[Path] = None
        self._kicad_cli: Optional[str] = None
        
        # Cached scan results (invalidated on update)
        self._scan_cache: Optional[Dict[str, Tuple[str, str]]] = None
        
        self._detect_root_files()
        self._load_config()
        self._init_libraries()
    
    # =========================================================================
    # Initialization
    # =========================================================================
    
    def _find_project_root(self, start: Path) -> Path:
        """
        Find the multiboard project root by searching for config file.
        
        This ensures sub-PCBs can see the full project hierarchy.
        """
        # First check if we're in a boards subdirectory
        for path in [start] + list(start.parents):
            if (path / CONFIG_FILE).exists():
                return path
            # Check if this looks like a board subdirectory
            if path.name == BOARDS_DIR:
                # Parent should be project root
                if (path.parent / CONFIG_FILE).exists():
                    return path.parent
            # Check if parent has boards directory (we might be inside boards/xxx/)
            if (path.parent / BOARDS_DIR).exists() and (path.parent / CONFIG_FILE).exists():
                return path.parent
        
        # Fall back to finding any .kicad_pro
        for path in [start] + list(start.parents):
            if list(path.glob("*.kicad_pro")):
                return path
        
        return start
    
    def _log(self, message: str):
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {message}\n")
        except Exception:
            pass
    
    def _detect_root_files(self):
        for pro_file in self.project_dir.glob("*.kicad_pro"):
            sch = pro_file.with_suffix(".kicad_sch")
            pcb = pro_file.with_suffix(".kicad_pcb")
            if sch.exists():
                self.config.root_schematic = sch.name
            if pcb.exists():
                self.config.root_pcb = pcb.name
            break
    
    def _load_config(self):
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    self.config = ProjectConfig.from_dict(json.load(f))
                self._detect_root_files()
            except Exception as e:
                self._log(f"Config load error: {e}")
    
    def save_config(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.config.to_dict(), f, indent=2)
    
    def _init_libraries(self):
        self._kicad_share = self._find_kicad_share()
        self._fp_lib_paths = {}
        
        # Parse project library table
        proj_table = self.project_dir / "fp-lib-table"
        if proj_table.exists():
            self._parse_fp_lib_table(proj_table)
        
        # Add KiCad standard libraries
        if self._kicad_share:
            fp_dir = self._kicad_share / "footprints"
            if fp_dir.exists():
                for lib_path in fp_dir.iterdir():
                    if lib_path.is_dir() and lib_path.suffix == ".pretty":
                        if lib_path.stem not in self._fp_lib_paths:
                            self._fp_lib_paths[lib_path.stem] = lib_path
        
        self._fp_resolver.set_lib_paths(self._fp_lib_paths, self._kicad_share)
        self._log(f"Initialized {len(self._fp_lib_paths)} footprint libraries")
    
    def _find_kicad_share(self) -> Optional[Path]:
        if os.name == "nt":
            bases = [
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "KiCad",
                Path(os.environ.get("ProgramFiles", "")) / "KiCad",
            ]
            for base in bases:
                if base.exists():
                    for ver in sorted(base.iterdir(), reverse=True):
                        share = ver / "share" / "kicad"
                        if (share / "footprints").exists():
                            return share
        else:
            for share in [Path("/usr/share/kicad"), Path("/usr/local/share/kicad"),
                          Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport")]:
                if (share / "footprints").exists():
                    return share
        return None
    
    def _parse_fp_lib_table(self, path: Path):
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
            for match in RE_FP_LIB_ENTRY.finditer(content):
                nick, uri = match.group(1), match.group(2)
                expanded = uri.replace("${KIPRJMOD}", str(self.project_dir))
                if "${" not in expanded:
                    self._fp_lib_paths[nick] = Path(expanded)
        except Exception:
            pass
    
    # =========================================================================
    # KiCad CLI (cached)
    # =========================================================================
    
    def _find_kicad_cli(self) -> Optional[str]:
        if self._kicad_cli:
            return self._kicad_cli
        
        exe = shutil.which("kicad-cli")
        if exe:
            self._kicad_cli = exe
            return exe
        
        if os.name == "nt":
            bases = [
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "KiCad",
                Path(os.environ.get("ProgramFiles", "")) / "KiCad",
            ]
            for base in bases:
                if base.exists():
                    for ver in sorted(base.iterdir(), reverse=True):
                        cli = ver / "bin" / "kicad-cli.exe"
                        if cli.exists():
                            self._kicad_cli = str(cli)
                            return self._kicad_cli
        return None
    
    def _run_cli(self, args: List[str]) -> subprocess.CompletedProcess:
        cli = self._find_kicad_cli()
        if not cli:
            raise FileNotFoundError("kicad-cli not found")
        
        kwargs = {"capture_output": True, "text": True, "cwd": str(self.project_dir)}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        
        return subprocess.run([cli] + args, **kwargs)
    
    # =========================================================================
    # Schematic Hardlinks
    # =========================================================================
    
    def _setup_board_project(self, board: BoardConfig):
        pcb_path = self.project_dir / board.pcb_path
        board_dir = pcb_path.parent
        base_name = pcb_path.stem
        
        board_dir.mkdir(parents=True, exist_ok=True)
        
        # Create .kicad_pro
        pro_file = board_dir / f"{base_name}.kicad_pro"
        if not pro_file.exists():
            pro_file.write_text(json.dumps({"meta": {"filename": pro_file.name}}, indent=2))
        
        # Link schematics
        if self.config.root_schematic:
            root_sch = self.project_dir / self.config.root_schematic
            if root_sch.exists():
                self._link_file(root_sch, board_dir / f"{base_name}.kicad_sch")
                for sheet_path in self._find_hierarchical_sheets(root_sch):
                    source = (root_sch.parent / sheet_path).resolve()
                    if source.exists():
                        self._link_file(source, board_dir / sheet_path)
        
        # Copy library tables with resolved paths
        for table_name in ("fp-lib-table", "sym-lib-table"):
            source = self.project_dir / table_name
            if source.exists():
                content = source.read_text(encoding="utf-8", errors="ignore")
                content = content.replace("${KIPRJMOD}", self.project_dir.as_posix())
                (board_dir / table_name).write_text(content, encoding="utf-8")
    
    def _link_file(self, source: Path, dest: Path):
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() or dest.is_symlink():
            dest.unlink()
        try:
            os.link(str(source), str(dest))
        except Exception:
            try:
                os.symlink(str(source), str(dest))
            except Exception:
                shutil.copy2(str(source), str(dest))
    
    def _find_hierarchical_sheets(self, schematic: Path) -> Set[Path]:
        sheets, visited, stack = set(), set(), [schematic]
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            try:
                content = current.read_text(encoding="utf-8", errors="ignore")
                for match in RE_SHEET_REF.findall(content):
                    sheet_path = Path(match)
                    sheets.add(sheet_path)
                    full_path = (current.parent / match).resolve()
                    if full_path.exists():
                        stack.append(full_path)
            except Exception:
                pass
        return sheets
    
    # =========================================================================
    # Board Management
    # =========================================================================
    
    def create_board(self, name: str, description: str = "") -> Tuple[bool, str]:
        if name in self.config.boards:
            return False, f"Board '{name}' already exists"
        
        safe_name = "".join(c if c.isalnum() or c in "_-" else "_" for c in name)
        rel_path = f"{BOARDS_DIR}/{safe_name}/{safe_name}.kicad_pcb"
        pcb_path = self.project_dir / rel_path
        
        if pcb_path.exists():
            return False, f"PCB already exists: {rel_path}"
        
        pcb_path.parent.mkdir(parents=True, exist_ok=True)
        self._create_empty_pcb(pcb_path)
        
        board = BoardConfig(name=name, pcb_path=rel_path, description=description)
        self._setup_board_project(board)
        self._generate_block_footprint(board)
        self._ensure_lib_in_table(BLOCK_LIB_NAME, f"{BLOCK_LIB_NAME}.pretty")
        
        self.config.boards[name] = board
        self.save_config()
        self._scan_cache = None  # Invalidate cache
        
        return True, rel_path
    
    def _create_empty_pcb(self, path: Path):
        pcb_content = '''(kicad_pcb
  (version 20240108) (generator "multiboard") (generator_version "9.0")
  (general (thickness 1.6) (legacy_teardrops no)) (paper "A4")
  (layers
    (0 "F.Cu" signal) (31 "B.Cu" signal)
    (36 "B.SilkS" user) (37 "F.SilkS" user)
    (38 "B.Mask" user) (39 "F.Mask" user)
    (44 "Edge.Cuts" user) (47 "F.CrtYd" user) (49 "F.Fab" user))
  (setup (pad_to_mask_clearance 0)) (net 0 ""))
'''
        path.write_text(pcb_content, encoding="utf-8")
    
    def _ensure_lib_in_table(self, lib_name: str, rel_path: str):
        table_path = self.project_dir / "fp-lib-table"
        entry = f'  (lib (name "{lib_name}")(type "KiCad")(uri "${{KIPRJMOD}}/{rel_path}")(options "")(descr ""))'
        
        if table_path.exists():
            content = table_path.read_text(encoding="utf-8", errors="ignore")
            if lib_name in content:
                return
            content = content.rstrip().rstrip(')') + f'\n{entry}\n)'
        else:
            content = f'(fp_lib_table\n  (version 7)\n{entry}\n)'
        
        table_path.write_text(content, encoding="utf-8")
    
    # =========================================================================
    # Block Footprints (Enhanced Visual Design)
    # =========================================================================
    
    def _generate_block_footprint(self, board: BoardConfig):
        """Generate a visually appealing block footprint with correct KiCad 9 syntax."""
        self.block_lib_path.mkdir(parents=True, exist_ok=True)
        
        fp_name = f"Block_{board.name}"
        w, h = board.block_width, board.block_height
        hw, hh = w/2, h/2
        
        lines = [
            f'(footprint "{fp_name}"',
            '  (version 20240108)',
            '  (generator "multiboard")',
            '  (generator_version "10.0")',
            '  (layer "F.Cu")',
            f'  (descr "Board block: {board.name}")',
            '  (attr board_only exclude_from_pos_files exclude_from_bom)',
        ]
        
        # Reference text
        lines.append(f'  (fp_text reference "REF**" (at 0 {-hh - 4:.3f}) (layer "F.SilkS")')
        lines.append('    (effects (font (size 1.2 1.2) (thickness 0.2)))')
        lines.append('  )')
        
        # Value text
        lines.append(f'  (fp_text value "{board.name}" (at 0 {hh + 4:.3f}) (layer "F.Fab")')
        lines.append('    (effects (font (size 1.2 1.2) (thickness 0.2)))')
        lines.append('  )')

        # --- Sexy rounded outlines (silk + fab) ---
        r = max(1.0, min(3.0, w * 0.08, h * 0.08))
        inv_sqrt2 = 1.0 / math.sqrt(2.0)

        def add_round_rect(layer: str, stroke_w: float, inset_mm: float = 0.0, stroke_type: str = "solid"):
            hw2 = hw - inset_mm
            hh2 = hh - inset_mm
            rr = max(0.5, min(r - inset_mm, hw2, hh2))
            if hw2 <= rr or hh2 <= rr:
                return

            # Edge segments
            lines.append(
                f'  (fp_line (start {-hw2 + rr:.3f} {-hh2:.3f}) (end {hw2 - rr:.3f} {-hh2:.3f})'
                f' (stroke (width {stroke_w:.3f}) (type {stroke_type})) (layer "{layer}"))'
            )
            lines.append(
                f'  (fp_line (start {hw2:.3f} {-hh2 + rr:.3f}) (end {hw2:.3f} {hh2 - rr:.3f})'
                f' (stroke (width {stroke_w:.3f}) (type {stroke_type})) (layer "{layer}"))'
            )
            lines.append(
                f'  (fp_line (start {hw2 - rr:.3f} {hh2:.3f}) (end {-hw2 + rr:.3f} {hh2:.3f})'
                f' (stroke (width {stroke_w:.3f}) (type {stroke_type})) (layer "{layer}"))'
            )
            lines.append(
                f'  (fp_line (start {-hw2:.3f} {hh2 - rr:.3f}) (end {-hw2:.3f} {-hh2 + rr:.3f})'
                f' (stroke (width {stroke_w:.3f}) (type {stroke_type})) (layer "{layer}"))'
            )

            # Corner arcs (quarter circles)
            # TL
            cx, cy = -hw2 + rr, -hh2 + rr
            mx, my = cx - rr * inv_sqrt2, cy - rr * inv_sqrt2
            lines.append(
                f'  (fp_arc (start {cx:.3f} {-hh2:.3f}) (mid {mx:.3f} {my:.3f}) (end {-hw2:.3f} {cy:.3f})'
                f' (stroke (width {stroke_w:.3f}) (type {stroke_type})) (layer "{layer}"))'
            )
            # TR
            cx, cy = hw2 - rr, -hh2 + rr
            mx, my = cx + rr * inv_sqrt2, cy - rr * inv_sqrt2
            lines.append(
                f'  (fp_arc (start {hw2:.3f} {cy:.3f}) (mid {mx:.3f} {my:.3f}) (end {cx:.3f} {-hh2:.3f})'
                f' (stroke (width {stroke_w:.3f}) (type {stroke_type})) (layer "{layer}"))'
            )
            # BR
            cx, cy = hw2 - rr, hh2 - rr
            mx, my = cx + rr * inv_sqrt2, cy + rr * inv_sqrt2
            lines.append(
                f'  (fp_arc (start {cx:.3f} {hh2:.3f}) (mid {mx:.3f} {my:.3f}) (end {hw2:.3f} {cy:.3f})'
                f' (stroke (width {stroke_w:.3f}) (type {stroke_type})) (layer "{layer}"))'
            )
            # BL
            cx, cy = -hw2 + rr, hh2 - rr
            mx, my = cx - rr * inv_sqrt2, cy + rr * inv_sqrt2
            lines.append(
                f'  (fp_arc (start {-hw2:.3f} {cy:.3f}) (mid {mx:.3f} {my:.3f}) (end {cx:.3f} {hh2:.3f})'
                f' (stroke (width {stroke_w:.3f}) (type {stroke_type})) (layer "{layer}"))'
            )

        # Outer rounded outline (silk)
        add_round_rect("F.SilkS", stroke_w=0.32, inset_mm=0.0, stroke_type="solid")
        # Inner dashed accent (silk)
        add_round_rect("F.SilkS", stroke_w=0.14, inset_mm=1.8, stroke_type="dash")
        # Fab outline (fab)
        add_round_rect("F.Fab", stroke_w=0.12, inset_mm=0.0, stroke_type="solid")

        # Pin-1 marker triangle (top-left)
        tri = 2.2
        px = -hw + 1.2
        py = -hh + 1.2
        lines.append(
            '  (fp_poly (pts '
            f'(xy {px:.3f} {py:.3f}) '
            f'(xy {px + tri:.3f} {py:.3f}) '
            f'(xy {px:.3f} {py + tri:.3f})'
            f') (stroke (width 0) (type solid)) (fill solid) (layer "F.SilkS"))'
        )

        # Board name in center - simple text without knockout
        lines.append(f'  (fp_text user "{board.name}" (at 0 0) (layer "F.SilkS")')
        lines.append('    (effects (font (size 2.5 2.5) (thickness 0.4) (bold yes)))')
        lines.append('  )')

        # Courtyard 
        lines.append(f'  (fp_rect (start {-hw - 1:.3f} {-hh - 1:.3f}) (end {hw + 1:.3f} {hh + 1:.3f})')
        lines.append('    (stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd")')
        lines.append('  )')
        
        # Fab layer outline with name
        lines.append(f'  (fp_rect (start {-hw:.3f} {-hh:.3f}) (end {hw:.3f} {hh:.3f})')
        lines.append('    (stroke (width 0.1) (type solid)) (fill none) (layer "F.Fab")')
        lines.append('  )')
        lines.append(f'  (fp_text user "${{REFERENCE}}" (at 0 0) (layer "F.Fab")')
        lines.append('    (effects (font (size 1.5 1.5) (thickness 0.2)))')
        lines.append('  )')
        
        # Port pads
        for port_name, port in sorted(board.ports.items()):
            x, y = self._calculate_port_position(port, w, h)
            rot = {"left": 180, "right": 0, "top": 270, "bottom": 90}.get(port.side, 0)
            pad_id = (port_name or "").strip() or "?"

            # SMD pad for port
            lines.append(f'  (pad "{pad_id}" smd roundrect (at {x:.3f} {y:.3f} {rot}) (size 3.6 1.7)')
            lines.append('    (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.28) (thermal_bridge_angle 45)')
            lines.append(f'    (pinfunction "{port_name}") (pintype "passive")')
            lines.append('  )')

            # Port label
            if port.side in ("left", "right"):
                label_x = x + (4 if port.side == "left" else -4)
                label_y = y
                label_rot = 0
            else:
                label_x = x
                label_y = y + (4 if port.side == "top" else -4)
                label_rot = 90
            
            lines.append(f'  (fp_text user "{port_name}" (at {label_x:.3f} {label_y:.3f} {label_rot}) (layer "F.SilkS")')
            lines.append('    (effects (font (size 1 1) (thickness 0.15)))')
            lines.append('  )')

            net_name = getattr(port, "net", "") or ""
            if net_name and net_name != port_name:
                lines.append(f'  (fp_text user "{net_name}" (at {label_x:.3f} {label_y + 1.4:.3f} {label_rot}) (layer "F.Fab")')
                lines.append('    (effects (font (size 0.9 0.9) (thickness 0.12)))')
                lines.append('  )')

        lines.append(')')
        
        fp_path = self.block_lib_path / f"{fp_name}.kicad_mod"
        fp_path.write_text('\n'.join(lines), encoding="utf-8")
    
    def _calculate_port_position(self, port: PortDef, w: float, h: float) -> Tuple[float, float]:
        p = port.position
        if port.side == "left":
            return (-w/2, h * (p - 0.5))
        elif port.side == "right":
            return (w/2, h * (p - 0.5))
        elif port.side == "top":
            return (w * (p - 0.5), -h/2)
        elif port.side == "bottom":
            return (w * (p - 0.5), h/2)
        return (0, 0)
    
    def generate_port_footprint(self, port_name: str):
        """Generate a port marker footprint."""
        self.port_lib_path.mkdir(parents=True, exist_ok=True)
        
        lines = [
            f'(footprint "Port_{port_name}"',
            '  (version 20240108)',
            '  (generator "multiboard")',
            '  (layer "F.Cu")',
            f'  (descr "Inter-board port: {port_name}")',
            '  (attr smd)',
            '  (fp_text reference "REF**" (at 0 -4) (layer "F.SilkS")',
            '    (effects (font (size 0.8 0.8) (thickness 0.12)))',
            '  )',
            '  (fp_text value "PORT" (at 0 4) (layer "F.Fab")',
            '    (effects (font (size 0.8 0.8) (thickness 0.12)))',
            '  )',
            f'  (fp_text user "{port_name}" (at 0 0) (layer "F.SilkS")',
            '    (effects (font (size 1 1) (thickness 0.15)))',
            '  )',
            '  (pad "1" smd roundrect (at 0 0) (size 2.5 2.5)',
            '    (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.2)',
            '  )',
            '  (fp_circle (center 0 0) (end 2 0)',
            '    (stroke (width 0.2) (type solid)) (fill none) (layer "F.SilkS")',
            '  )',
            '  (fp_circle (center 0 0) (end 1.5 0)',
            '    (stroke (width 0.1) (type solid)) (fill none) (layer "F.SilkS")',
            '  )',
            ')',
        ]
        
        (self.port_lib_path / f"Port_{port_name}.kicad_mod").write_text('\n'.join(lines), encoding="utf-8")
        self._ensure_lib_in_table(PORT_LIB_NAME, f"{PORT_LIB_NAME}.pretty")
    
    # =========================================================================
    # PCB Scanning (Optimized)
    # =========================================================================
    
    def scan_all_boards(self, force: bool = False) -> Dict[str, Tuple[str, str]]:
        """
        Scan all board PCBs for placed components.
        
        Returns: {ref: (board_name, footprint_id)}
        
        Results are cached until invalidated.
        """
        if not force and self._scan_cache is not None:
            return self._scan_cache
        
        placed = {}
        for name, board in self.config.boards.items():
            pcb_path = self.project_dir / board.pcb_path
            if not pcb_path.exists():
                continue
            try:
                pcb = pcbnew.LoadBoard(str(pcb_path))
                for fp in pcb.GetFootprints():
                    ref = fp.GetReference()
                    if ref and not ref.startswith("#") and not ref.startswith("MB_"):
                        fpid = fp.GetFPID()
                        fp_str = f"{fpid.GetLibNickname()}:{fpid.GetLibItemName()}"
                        placed[ref] = (name, fp_str)
            except Exception as e:
                self._log(f"Scan error {name}: {e}")
        
        self._scan_cache = placed
        return placed
    
    def get_board_nets(self, board_name: str) -> Dict[str, Set[str]]:
        board = self.config.boards.get(board_name)
        if not board:
            return {}
        
        pcb_path = self.project_dir / board.pcb_path
        if not pcb_path.exists():
            return {}
        
        nets: Dict[str, Set[str]] = {}
        try:
            pcb = pcbnew.LoadBoard(str(pcb_path))
            for fp in pcb.GetFootprints():
                ref = fp.GetReference()
                for pad in fp.Pads():
                    net_name = pad.GetNetname()
                    if net_name:
                        nets.setdefault(net_name, set()).add(f"{ref}.{pad.GetNumber()}")
        except Exception:
            pass
        return nets
    
    # =========================================================================
    # Connectivity Check
    # =========================================================================
    
    def check_connectivity(self, progress_callback=None) -> Dict[str, Any]:
        report = {"boards": {}, "cross_board": [], "errors": [], "warnings": []}
        total = len(self.config.boards)
        
        for i, (name, board) in enumerate(self.config.boards.items()):
            if progress_callback:
                progress_callback(int(100 * i / max(total, 1)), f"Checking {name}...")
            
            pcb_path = self.project_dir / board.pcb_path
            if not pcb_path.exists():
                report["errors"].append(f"{name}: PCB not found")
                continue
            
            try:
                drc_file = pcb_path.with_suffix(".drc.json")
                self._run_cli(["pcb", "drc", "--format", "json", "-o", str(drc_file), str(pcb_path)])
                
                if drc_file.exists():
                    drc = json.loads(drc_file.read_text(encoding="utf-8"))
                    violations = drc.get("violations", [])
                    
                    port_nets = {p.net for p in board.ports.values() if p.net}
                    filtered = []
                    for v in violations:
                        if "unconnected" in v.get("type", "").lower():
                            desc = v.get("description", "")
                            if any(pn in desc for pn in port_nets):
                                continue
                        filtered.append(v)
                    
                    report["boards"][name] = {"violations": len(filtered), "details": filtered[:20]}
                    drc_file.unlink()
            except Exception as e:
                report["errors"].append(f"{name}: DRC failed - {e}")
        
        if progress_callback:
            progress_callback(100, "Done")
        
        return report
    
    # =========================================================================
    # Board Update (Optimized)
    # =========================================================================
    
    def update_board(self, board_name: str, progress_callback=None) -> Tuple[bool, str]:
        """
        Update a board from schematic - optimized for speed.
        """
        board = self.config.boards.get(board_name)
        if not board:
            return False, f"Board '{board_name}' not found"
        
        pcb_path = self.project_dir / board.pcb_path
        if not pcb_path.exists():
            return False, f"PCB not found: {board.pcb_path}"
        
        try:
            # Step 1: Setup
            if progress_callback:
                progress_callback(2, "Refreshing schematic links...")
            self._setup_board_project(board)
            
            # Step 2: Scan existing boards (use cache if valid)
            if progress_callback:
                progress_callback(5, "Scanning boards...")
            placed = self.scan_all_boards()
            
            # Step 3: Export netlist
            if progress_callback:
                progress_callback(10, "Exporting netlist...")
            netlist_path = self._export_netlist()
            if not netlist_path:
                return False, "Failed to export netlist"
            
            # Step 4: Fast netlist parsing
            if progress_callback:
                progress_callback(20, "Parsing netlist...")
            components = self._parse_netlist_optimized(netlist_path)
            
            # Step 5: Load PCB
            if progress_callback:
                progress_callback(25, "Loading PCB...")
            pcb = pcbnew.LoadBoard(str(pcb_path))
            if not pcb:
                return False, "Failed to load PCB"
            
            # Build existing footprint map
            existing = {}
            existing_fp = {}
            for fp in pcb.GetFootprints():
                ref = fp.GetReference()
                if ref:
                    existing[ref] = fp
                    fpid = fp.GetFPID()
                    existing_fp[ref] = f"{fpid.GetLibNickname()}:{fpid.GetLibItemName()}"
            
            # Filter components for this board
            to_add = []
            to_update = []
            
            for ref, info in components.items():
                if info["skip"]:
                    continue
                
                # Skip if on another board
                if ref in placed:
                    placed_board, _ = placed[ref]
                    if placed_board != board_name:
                        continue
                
                if ref in existing:
                    to_update.append((ref, info))
                else:
                    to_add.append((ref, info))
            
            # Step 6: Update existing
            if progress_callback:
                progress_callback(35, f"Updating {len(to_update)} components...")
            
            updated, replaced = 0, 0
            for ref, info in to_update:
                fp = existing[ref]
                old_fp_id = existing_fp.get(ref, "")
                
                if old_fp_id != info["footprint"]:
                    lib, name = self._split_fpid(info["footprint"])
                    new_fp = self._fp_resolver.load(lib, name)
                    if new_fp:
                        pos, rot, layer = fp.GetPosition(), fp.GetOrientationDegrees(), fp.GetLayer()
                        pcb.Remove(fp)
                        new_fp.SetReference(ref)
                        new_fp.SetValue(info["value"])
                        new_fp.SetPosition(pos)
                        new_fp.SetOrientationDegrees(rot)
                        new_fp.SetLayer(layer)
                        self._set_fp_path(new_fp, info["tstamp"])
                        pcb.Add(new_fp)
                        existing[ref] = new_fp
                        replaced += 1
                    else:
                        fp.SetValue(info["value"])
                        updated += 1
                else:
                    fp.SetValue(info["value"])
                    self._set_fp_path(fp, info["tstamp"])
                    updated += 1
            
            # Step 7: Add new components
            if progress_callback:
                progress_callback(50, f"Adding {len(to_add)} components...")
            
            added, failed = 0, 0
            failed_list = []
            new_footprints = []
            
            total_to_add = len(to_add)
            for i, (ref, info) in enumerate(to_add):
                # Update progress every 10 components
                if progress_callback and i % 10 == 0 and total_to_add > 10:
                    pct = 50 + int(30 * i / total_to_add)
                    progress_callback(pct, f"Adding components ({i+1}/{total_to_add})...")
                
                lib, name = self._split_fpid(info["footprint"])
                fp = self._fp_resolver.load(lib, name)
                
                if not fp:
                    failed += 1
                    failed_list.append(f"{ref}: {info['footprint']}")
                    continue
                
                fp.SetReference(ref)
                fp.SetValue(info["value"])
                self._set_fp_path(fp, info["tstamp"])
                pcb.Add(fp)
                existing[ref] = fp
                new_footprints.append(fp)
                added += 1
            
            # Step 8: Pack new components
            if new_footprints and progress_callback:
                progress_callback(70, "Arranging components...")
            
            if new_footprints:
                self._pack_footprints(pcb, new_footprints)
            
            # Step 9: Assign nets
            if progress_callback:
                progress_callback(85, "Assigning nets...")
            self._assign_nets_optimized(pcb, netlist_path, existing)
            
            # Step 10: Save
            if progress_callback:
                progress_callback(95, "Saving...")
            pcbnew.SaveBoard(str(pcb_path), pcb)
            
            # Cleanup
            try:
                netlist_path.unlink()
            except Exception:
                pass
            
            self._scan_cache = None  # Invalidate cache
            
            msg = f"Added: {added}\nUpdated: {updated}"
            if replaced:
                msg += f"\nReplaced: {replaced}"
            if failed:
                msg += f"\nFailed: {failed}"
                if failed_list:
                    msg += "\n\n" + "\n".join(failed_list[:10])
            
            return True, msg
            
        except Exception as e:
            self._log(f"Update error: {e}\n{traceback.format_exc()}")
            return False, f"Error: {e}"
    
    def _export_netlist(self) -> Optional[Path]:
        if not self.config.root_schematic:
            return None
        sch = self.project_dir / self.config.root_schematic
        if not sch.exists():
            return None
        
        netlist = self.project_dir / TEMP_NETLIST_NAME
        try:
            self._run_cli(["sch", "export", "netlist", "--format", "kicadxml", "-o", str(netlist), str(sch)])
            return netlist if netlist.exists() else None
        except Exception:
            return None
    
    def _parse_netlist_optimized(self, path: Path) -> Dict[str, dict]:
        """
        Optimized netlist parsing with correct Exclude From Board detection.
        
        KiCad exports "Exclude from board" as a property where:
        - Name can be: "exclude_from_board", "Exclude from board", "ki_exclude_from_board"
        - Value "1", "yes", "true" means exclude
        - EMPTY VALUE "" also means TRUE (KiCad boolean property quirk)
        """
        components = {}
        
        # Try to use lxml if available (3-5x faster)
        try:
            from lxml import etree
            tree = etree.parse(str(path))
            root = tree.getroot()
            comp_elements = root.iter("comp")
            use_lxml = True
        except ImportError:
            import xml.etree.ElementTree as ET
            tree = ET.parse(str(path))
            root = tree.getroot()
            comp_elements = root.iter("comp")
            use_lxml = False
        
        for elem in comp_elements:
            ref = elem.get("ref", "")
            if not ref or ref.startswith("#"):
                continue
            
            footprint = ""
            value = ""
            tstamp = ""
            skip = False
            
            for child in elem:
                tag = child.tag
                if tag == "footprint":
                    footprint = (child.text or "").strip()
                elif tag == "value":
                    value = (child.text or "").strip()
                elif tag == "tstamp":
                    tstamp = (child.text or "").strip()
                elif tag == "property":
                    pname = (child.get("name") or "").strip()
                    pval = (child.get("value") or "").strip()
                    
                    # Normalize property name for comparison
                    pname_normalized = pname.lower().replace(" ", "_").replace("-", "_")
                    pval_lower = pval.lower()
                    
                    # Check DNP property
                    if pname_normalized == "dnp":
                        # DNP is true if value is yes/true/1/dnp OR if value is empty (boolean property)
                        if pval_lower in ("yes", "true", "1", "dnp") or pval == "":
                            skip = True
                    
                    # Check Exclude from Board - multiple possible property names
                    # KiCad uses "Exclude from board" which normalizes to "exclude_from_board"
                    # Also check for ki_exclude_from_board variant
                    if "exclude" in pname_normalized and "board" in pname_normalized:
                        # For boolean properties, empty string means TRUE
                        # Also accept yes/true/1
                        if pval == "" or pval_lower in ("yes", "true", "1"):
                            skip = True
                            self._log(f"Excluding {ref}: property '{pname}' = '{pval}'")
                
                elif tag == "fields":
                    # Also check <fields> section for older KiCad versions
                    for field in child:
                        fname = (field.get("name") or "").strip()
                        fval = (field.text or "").strip()
                        fname_normalized = fname.lower().replace(" ", "_").replace("-", "_")
                        fval_lower = fval.lower()
                        
                        if "exclude" in fname_normalized and "board" in fname_normalized:
                            if fval == "" or fval_lower in ("yes", "true", "1"):
                                skip = True
                                self._log(f"Excluding {ref}: field '{fname}' = '{fval}'")
            
            # Also check if value is "DNP"
            if value.upper() == "DNP":
                skip = True
            
            # No footprint means can't place
            if not footprint:
                skip = True
            
            components[ref] = {
                "footprint": footprint,
                "value": value,
                "tstamp": tstamp,
                "skip": skip,
            }
        
        return components
    
    def _split_fpid(self, fpid: str) -> Tuple[str, str]:
        if ":" in fpid:
            parts = fpid.split(":", 1)
            return parts[0], parts[1]
        return "", fpid
    
    def _set_fp_path(self, fp, tstamp: str):
        if tstamp:
            try:
                fp.SetPath(pcbnew.KIID_PATH(f"/{tstamp}"))
            except Exception:
                pass
    
    def _assign_nets_optimized(self, board, netlist_path: Path, footprints: Dict):
        """Optimized net assignment."""
        nets = {name: net for name, net in board.GetNetsByName().items()}
        
        def get_net(name: str):
            if name not in nets:
                ni = pcbnew.NETINFO_ITEM(board, name)
                board.Add(ni)
                nets[name] = ni
            return nets[name]
        
        try:
            from lxml import etree
            parser = etree.iterparse(netlist_path, events=["end"], tag="net")
            use_lxml = True
        except ImportError:
            import xml.etree.ElementTree as ET
            parser = ET.iterparse(str(netlist_path), events=["end"])
            use_lxml = False
        
        for event, elem in parser:
            if not use_lxml and elem.tag != "net":
                continue
            
            net_name = elem.get("name", "")
            if net_name:
                ni = get_net(net_name)
                for node in elem.findall("node"):
                    ref, pin = node.get("ref", ""), node.get("pin", "")
                    fp = footprints.get(ref)
                    if fp:
                        pad = fp.FindPadByNumber(pin)
                        if pad:
                            pad.SetNet(ni)
            elem.clear()
    
    def _pack_footprints(self, board, footprints: List):
        """
        Position new footprints for easy manual packing.
        
        Places components in a simple grid near the origin.
        User can then select all and press 'P' to use KiCad's 
        native Pack and Move Footprints tool for optimal arrangement.
        """
        if not footprints:
            return
        
        # Simple grid placement - fast and reliable
        grid_mm = PACK_GRID_SPACING
        cols = min(PACK_MAX_PER_ROW, max(1, int(len(footprints) ** 0.5) + 1))
        
        start_x = pcbnew.FromMM(50)
        start_y = pcbnew.FromMM(50)
        grid = pcbnew.FromMM(grid_mm)
        
        for i, fp in enumerate(footprints):
            col = i % cols
            row = i // cols
            x = start_x + col * grid
            y = start_y + row * grid
            fp.SetPosition(pcbnew.VECTOR2I(int(x), int(y)))
    
    # =========================================================================
    # Status
    # =========================================================================
    
    def get_status(self) -> Tuple[Dict[str, str], Set[str], int]:
        placed_raw = self.scan_all_boards()
        placed = {ref: board for ref, (board, _) in placed_raw.items()}
        
        netlist = self._export_netlist()
        if not netlist:
            return placed, set(), len(placed)
        
        comps = self._parse_netlist_optimized(netlist)
        try:
            netlist.unlink()
        except Exception:
            pass
        
        valid = {r for r, i in comps.items() if not i["skip"]}
        return placed, valid - set(placed.keys()), len(valid)
