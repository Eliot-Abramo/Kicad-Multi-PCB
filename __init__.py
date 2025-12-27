"""
Multi-Board PCB Manager v7 - KiCad Action Plugin
=================================================
Multi-PCB workflow: one schematic, multiple board layouts.

Features:
- Proper footprint loading from project fp-lib-table
- Cross-probing works (schematic links in board folders)
- Board block footprints for root PCB visualization
- Respects DNP and exclude_from_board

For KiCad 9.0+ on Windows

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
from typing import Optional, Dict, List, Set, Tuple
from datetime import datetime
from dataclasses import dataclass, field
import faulthandler
import traceback
import re

# ============================================================================
# Constants
# ============================================================================

BOARDS_DIR = "boards"
CONFIG_FILE = ".kicad_multiboard.json"
BLOCK_LIB_NAME = "MultiBoard_Blocks"

# ============================================================================
# Data Model
# ============================================================================

@dataclass
class PortDef:
    """Port on a board block."""
    name: str
    side: str = "right"  # left, right, top, bottom
    position: float = 0.5  # 0-1 along the side

@dataclass
class BoardConfig:
    """Configuration for a sub-board."""
    name: str
    pcb_path: str
    description: str = ""
    block_width: float = 50.0
    block_height: float = 35.0
    ports: Dict[str, PortDef] = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "pcb_path": self.pcb_path,
            "description": self.description,
            "block_width": self.block_width,
            "block_height": self.block_height,
            "ports": {k: {"name": v.name, "side": v.side, "position": v.position} 
                      for k, v in self.ports.items()}
        }
    
    @classmethod
    def from_dict(cls, d: dict) -> "BoardConfig":
        cfg = cls(
            name=d["name"],
            pcb_path=d.get("pcb_path", d.get("pcb_filename", "")),
            description=d.get("description", ""),
            block_width=d.get("block_width", 50.0),
            block_height=d.get("block_height", 35.0),
        )
        for pname, pdata in d.get("ports", {}).items():
            if isinstance(pdata, dict):
                cfg.ports[pname] = PortDef(
                    name=pdata.get("name", pname),
                    side=pdata.get("side", "right"),
                    position=pdata.get("position", 0.5)
                )
        return cfg


@dataclass  
class ProjectConfig:
    """Multi-board project configuration."""
    version: str = "7.0"
    root_schematic: str = ""
    root_pcb: str = ""
    boards: Dict[str, BoardConfig] = field(default_factory=dict)
    assignments: Dict[str, str] = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "root_schematic": self.root_schematic,
            "root_pcb": self.root_pcb,
            "boards": {k: v.to_dict() for k, v in self.boards.items()},
            "assignments": self.assignments,
        }
    
    @classmethod
    def from_dict(cls, d: dict) -> "ProjectConfig":
        cfg = cls(
            version=d.get("version", "7.0"),
            root_schematic=d.get("root_schematic", ""),
            root_pcb=d.get("root_pcb", ""),
            assignments=d.get("assignments", d.get("component_placement", {})),
        )
        for name, bd in d.get("boards", {}).items():
            if isinstance(bd, dict):
                cfg.boards[name] = BoardConfig.from_dict(bd)
        return cfg


# ============================================================================
# Core Manager
# ============================================================================

class MultiBoardManager:
    """Manages multi-board project operations."""
    
    def __init__(self, project_dir: Path):
        self.project_dir = self._find_project_root(project_dir)
        self.config_path = self.project_dir / CONFIG_FILE
        self.config = ProjectConfig()
        self.block_lib_path = self.project_dir / f"{BLOCK_LIB_NAME}.pretty"
        
        # Footprint library cache
        self._fp_lib_cache: Dict[str, Path] = {}
        
        # Logging
        self.log_path = self.project_dir / "multiboard_debug.log"
        self.fault_path = self.project_dir / "multiboard_fault.log"
        self._init_logging()
        
        self._detect_root_files()
        self._load_config()
        self._parse_fp_lib_table()
        
        self._log(f"Init: project_dir={self.project_dir}")
    
    def _find_project_root(self, start: Path) -> Path:
        """Walk up to find project root."""
        for p in [start] + list(start.parents):
            if (p / CONFIG_FILE).exists():
                return p
            if list(p.glob("*.kicad_pro")):
                return p
        return start
    
    def _init_logging(self):
        try:
            self.log_path.touch(exist_ok=True)
            fh = open(self.fault_path, "a", encoding="utf-8")
            faulthandler.enable(file=fh, all_threads=True)
            self._fault_fh = fh
        except Exception:
            self._fault_fh = None
    
    def _log(self, msg: str):
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {msg}\n")
        except Exception:
            pass
    
    def _detect_root_files(self):
        """Find root schematic and PCB."""
        for pro in self.project_dir.glob("*.kicad_pro"):
            base = pro.stem
            sch = pro.with_suffix(".kicad_sch")
            pcb = pro.with_suffix(".kicad_pcb")
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
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.config.to_dict(), f, indent=2)
        except Exception as e:
            self._log(f"Config save error: {e}")
    
    # -------------------------------------------------------------------------
    # Footprint Library Table Parsing
    # -------------------------------------------------------------------------
    
    def _parse_fp_lib_table(self):
        """Parse fp-lib-table to get library nickname -> path mapping."""
        self._fp_lib_cache = {}
        
        # Parse project fp-lib-table
        project_table = self.project_dir / "fp-lib-table"
        if project_table.exists():
            self._parse_single_fp_table(project_table)
        
        # Parse global fp-lib-table
        global_table = self._find_global_fp_lib_table()
        if global_table and global_table.exists():
            self._parse_single_fp_table(global_table)
        
        self._log(f"Loaded {len(self._fp_lib_cache)} footprint libraries")
    
    def _find_global_fp_lib_table(self) -> Optional[Path]:
        """Find global fp-lib-table location."""
        if os.name == "nt":
            appdata = os.environ.get("APPDATA")
            if appdata:
                for ver in ["9.0", "8.0", "7.0"]:
                    p = Path(appdata) / "kicad" / ver / "fp-lib-table"
                    if p.exists():
                        return p
        else:
            home = Path.home()
            for path in [
                home / ".config/kicad/9.0/fp-lib-table",
                home / ".config/kicad/8.0/fp-lib-table",
                home / ".config/kicad/fp-lib-table",
            ]:
                if path.exists():
                    return path
        return None
    
    def _parse_single_fp_table(self, table_path: Path):
        """Parse a single fp-lib-table file."""
        try:
            content = table_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return
        
        # Match lib entries with flexible whitespace
        for match in re.finditer(r'\(lib\s+\(name\s+"([^"]+)"\)[^)]*\(uri\s+"([^"]+)"\)', content, re.DOTALL):
            nick = match.group(1)
            uri = match.group(2)
            
            expanded = self._expand_fp_uri(uri)
            if expanded:
                self._fp_lib_cache[nick] = Path(expanded)
    
    def _expand_fp_uri(self, uri: str) -> Optional[str]:
        """Expand environment variables in URI."""
        result = uri
        
        # ${KIPRJMOD}
        result = result.replace("${KIPRJMOD}", str(self.project_dir))
        
        # KiCad environment variables
        env_vars = [
            "KICAD9_FOOTPRINT_DIR", "KICAD8_FOOTPRINT_DIR", "KICAD7_FOOTPRINT_DIR",
            "KICAD_FOOTPRINT_DIR", "KICAD9_3RDPARTY_DIR", "KICAD_USER_DATA",
        ]
        
        for var in env_vars:
            val = os.environ.get(var)
            if val:
                result = result.replace(f"${{{var}}}", val)
        
        # Check for remaining unexpanded variables - try to find KiCad install
        if "${" in result:
            if os.name == "nt":
                for base in [
                    Path(os.environ.get("ProgramFiles", "")) / "KiCad",
                    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "KiCad",
                ]:
                    for ver in ["9.0", "8.0"]:
                        kicad_share = base / ver / "share" / "kicad"
                        if kicad_share.exists():
                            result = re.sub(r'\$\{KICAD\d*_FOOTPRINT_DIR\}', 
                                          str(kicad_share / "footprints"), result)
                            break
        
        # Remove file:// prefix
        if result.startswith("file://"):
            result = result[7:]
        
        return result if "${" not in result else None
    
    # -------------------------------------------------------------------------
    # KiCad CLI
    # -------------------------------------------------------------------------
    
    def _find_kicad_cli(self) -> Optional[str]:
        """Find kicad-cli executable."""
        exe = shutil.which("kicad-cli")
        if exe:
            return exe
        
        if os.name != "nt":
            return None
        
        search_paths = []
        local = os.environ.get("LOCALAPPDATA")
        if local:
            search_paths.append(Path(local) / "Programs" / "KiCad")
        for env in ("ProgramFiles", "ProgramFiles(x86)"):
            pf = os.environ.get(env)
            if pf:
                search_paths.append(Path(pf) / "KiCad")
        
        for base in search_paths:
            if not base.exists():
                continue
            for ver_dir in sorted(base.iterdir(), reverse=True):
                cli = ver_dir / "bin" / "kicad-cli.exe"
                if cli.exists():
                    return str(cli)
        return None
    
    def _run_cli(self, args: List[str]) -> subprocess.CompletedProcess:
        """Run kicad-cli."""
        cli = self._find_kicad_cli()
        if not cli:
            raise FileNotFoundError("kicad-cli not found")
        
        cmd = [cli] + args
        self._log(f"CLI: {' '.join(cmd)}")
        
        kwargs = {"capture_output": True, "text": True, "cwd": str(self.project_dir)}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        
        return subprocess.run(cmd, **kwargs)
    
    # -------------------------------------------------------------------------
    # Schematic Linking (for cross-probing)
    # -------------------------------------------------------------------------
    
    def _extract_sheet_refs(self, sch_content: str) -> List[str]:
        """Extract hierarchical sheet file references."""
        refs = []
        patterns = [
            r'\(property\s+"Sheetfile"\s+"([^"]+\.kicad_sch)"',
            r'\(sheetfile\s+"([^"]+\.kicad_sch)"',
        ]
        for pat in patterns:
            refs.extend(re.findall(pat, sch_content, re.IGNORECASE))
        return list(set(refs))
    
    def _collect_all_sheets(self, root_sch: Path) -> Set[Path]:
        """Recursively collect all schematic sheet paths."""
        sheets = set()
        visited = set()
        stack = [root_sch]
        
        while stack:
            sch = stack.pop()
            if sch in visited:
                continue
            visited.add(sch)
            
            try:
                content = sch.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            
            for ref in self._extract_sheet_refs(content):
                rel_path = Path(ref)
                sheets.add(rel_path)
                full_path = (sch.parent / rel_path).resolve()
                if full_path.exists():
                    stack.append(full_path)
        
        return sheets
    
    def _link_file(self, src: Path, dst: Path) -> bool:
        """Create hardlink or symlink."""
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            
            # Try hardlink first
            try:
                os.link(str(src), str(dst))
                return True
            except Exception:
                pass
            
            # Fall back to symlink
            try:
                os.symlink(str(src), str(dst))
                return True
            except Exception:
                pass
            
            # Last resort: copy
            shutil.copy2(str(src), str(dst))
            return True
            
        except Exception as e:
            self._log(f"Link failed {src} -> {dst}: {e}")
            return False
    
    def _setup_board_project(self, board: BoardConfig) -> bool:
        """Set up board folder with project files and schematic links."""
        pcb_path = self.project_dir / board.pcb_path
        board_dir = pcb_path.parent
        base_name = pcb_path.stem
        
        board_dir.mkdir(parents=True, exist_ok=True)
        
        # Create .kicad_pro
        pro_path = board_dir / f"{base_name}.kicad_pro"
        if not pro_path.exists():
            pro_data = {"meta": {"filename": pro_path.name, "version": 1}}
            try:
                pro_path.write_text(json.dumps(pro_data, indent=2), encoding="utf-8")
            except Exception:
                pass
        
        # Link root schematic as board schematic
        if self.config.root_schematic:
            root_sch = self.project_dir / self.config.root_schematic
            board_sch = board_dir / f"{base_name}.kicad_sch"
            
            if root_sch.exists():
                self._link_file(root_sch, board_sch)
                
                # Link all hierarchical sheets
                for sheet_rel in self._collect_all_sheets(root_sch):
                    src_sheet = (root_sch.parent / sheet_rel).resolve()
                    dst_sheet = board_dir / sheet_rel
                    if src_sheet.exists():
                        self._link_file(src_sheet, dst_sheet)
        
        # Copy/link fp-lib-table with adjusted paths
        self._setup_board_lib_tables(board_dir)
        
        return True
    
    def _setup_board_lib_tables(self, board_dir: Path):
        """Set up library tables in board folder."""
        root_abs = self.project_dir.as_posix()
        
        for table_name in ("fp-lib-table", "sym-lib-table"):
            src = self.project_dir / table_name
            dst = board_dir / table_name
            
            if not src.exists():
                continue
            
            try:
                content = src.read_text(encoding="utf-8", errors="ignore")
                content = content.replace("${KIPRJMOD}", root_abs)
                dst.write_text(content, encoding="utf-8")
            except Exception:
                pass
    
    # -------------------------------------------------------------------------
    # Board Management
    # -------------------------------------------------------------------------
    
    def create_board(self, name: str, description: str = "") -> Tuple[bool, str]:
        """Create a new sub-board."""
        if name in self.config.boards:
            return False, f"Board '{name}' already exists"
        
        safe_name = "".join(c if c.isalnum() or c in "_-" else "_" for c in name)
        
        board_dir = self.project_dir / BOARDS_DIR / safe_name
        pcb_path = board_dir / f"{safe_name}.kicad_pcb"
        rel_path = f"{BOARDS_DIR}/{safe_name}/{safe_name}.kicad_pcb"
        
        if pcb_path.exists():
            return False, f"PCB file already exists: {rel_path}"
        
        board_dir.mkdir(parents=True, exist_ok=True)
        
        if not self._create_empty_pcb(pcb_path):
            return False, "Failed to create PCB file"
        
        board = BoardConfig(name=name, pcb_path=rel_path, description=description)
        
        # Set up project files and schematic links
        self._setup_board_project(board)
        
        # Generate board block footprint
        self._generate_block_footprint(board)
        self._add_block_lib_to_table()
        
        self.config.boards[name] = board
        self.save_config()
        
        self._log(f"Created board: {name} at {rel_path}")
        return True, f"Created {rel_path}"
    
    def _create_empty_pcb(self, path: Path) -> bool:
        """Create empty PCB file."""
        content = '''(kicad_pcb
  (version 20240108)
  (generator "multiboard")
  (generator_version "9.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
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
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
)
'''
        try:
            path.write_text(content, encoding="utf-8")
            return True
        except Exception as e:
            self._log(f"Failed to create PCB: {e}")
            return False
    
    # -------------------------------------------------------------------------
    # Board Block Footprint Generation
    # -------------------------------------------------------------------------
    
    def _generate_block_footprint(self, board: BoardConfig) -> bool:
        """Generate board block footprint."""
        self.block_lib_path.mkdir(parents=True, exist_ok=True)
        
        fp_name = f"Block_{board.name}"
        fp_path = self.block_lib_path / f"{fp_name}.kicad_mod"
        
        w, h = board.block_width, board.block_height
        
        lines = [
            f'(footprint "{fp_name}"',
            '  (version 20240108)',
            '  (generator "multiboard")',
            '  (layer "F.Cu")',
            f'  (descr "Board block: {board.name}")',
            '  (attr board_only exclude_from_pos_files exclude_from_bom)',
            f'  (fp_text reference "MB" (at 0 {-h/2-2:.2f}) (layer "F.SilkS")',
            '    (effects (font (size 1.5 1.5) (thickness 0.15))))',
            f'  (fp_text value "{board.name}" (at 0 0) (layer "F.Fab")',
            '    (effects (font (size 2 2) (thickness 0.2))))',
            # Outline
            f'  (fp_rect (start {-w/2:.2f} {-h/2:.2f}) (end {w/2:.2f} {h/2:.2f})',
            '    (stroke (width 0.3) (type solid)) (fill none) (layer "F.SilkS"))',
            # Courtyard
            f'  (fp_rect (start {-w/2-1:.2f} {-h/2-1:.2f}) (end {w/2+1:.2f} {h/2+1:.2f})',
            '    (stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd"))',
            # PCB path as user text
            f'  (fp_text user "PCB={board.pcb_path}" (at 0 {h/2+2:.2f}) (layer "F.Fab") hide',
            '    (effects (font (size 0.8 0.8) (thickness 0.1))))',
        ]
        
        # Add port pads
        pad_num = 1
        for port_name, port in board.ports.items():
            px, py = self._calc_port_pos(board, port)
            lines.append(
                f'  (pad "{pad_num}" smd rect (at {px:.2f} {py:.2f}) '
                f'(size 1 1) (layers "F.Cu" "F.Paste" "F.Mask"))'
            )
            # Port label
            lx = px + (2 if port.side == "right" else -2 if port.side == "left" else 0)
            ly = py + (2 if port.side == "bottom" else -2 if port.side == "top" else 0)
            just = "left" if port.side == "right" else "right" if port.side == "left" else "center"
            lines.append(
                f'  (fp_text user "{port_name}" (at {lx:.2f} {ly:.2f}) (layer "F.SilkS")'
                f'\n    (effects (font (size 0.8 0.8) (thickness 0.1)) (justify {just})))'
            )
            pad_num += 1
        
        lines.append(')')
        
        try:
            fp_path.write_text('\n'.join(lines), encoding="utf-8")
            return True
        except Exception as e:
            self._log(f"Failed to generate footprint: {e}")
            return False
    
    def _calc_port_pos(self, board: BoardConfig, port: PortDef) -> Tuple[float, float]:
        """Calculate port position on block."""
        w, h = board.block_width, board.block_height
        p = port.position
        
        if port.side == "right":
            return (w/2, -h/2 + h * p)
        elif port.side == "left":
            return (-w/2, -h/2 + h * p)
        elif port.side == "top":
            return (-w/2 + w * p, -h/2)
        else:  # bottom
            return (-w/2 + w * p, h/2)
    
    def _add_block_lib_to_table(self):
        """Add block library to fp-lib-table."""
        table_path = self.project_dir / "fp-lib-table"
        lib_entry = f'  (lib (name "{BLOCK_LIB_NAME}")(type "KiCad")(uri "${{KIPRJMOD}}/{BLOCK_LIB_NAME}.pretty")(options "")(descr "Multi-board blocks"))'
        
        if table_path.exists():
            content = table_path.read_text(encoding="utf-8", errors="ignore")
            if BLOCK_LIB_NAME in content:
                return
            content = content.rstrip().rstrip(')')
            content += f'\n{lib_entry}\n)'
        else:
            content = f'(fp_lib_table\n  (version 7)\n{lib_entry}\n)'
        
        try:
            table_path.write_text(content, encoding="utf-8")
        except Exception:
            pass
    
    # -------------------------------------------------------------------------
    # Netlist Export and Filtering
    # -------------------------------------------------------------------------
    
    def _export_netlist(self) -> Optional[Path]:
        """Export netlist from root schematic."""
        if not self.config.root_schematic:
            return None
        
        sch_path = self.project_dir / self.config.root_schematic
        if not sch_path.exists():
            return None
        
        netlist_path = self.project_dir / ".multiboard_netlist.xml"
        
        try:
            result = self._run_cli([
                "sch", "export", "netlist",
                "--format", "kicadxml",
                "-o", str(netlist_path),
                str(sch_path)
            ])
            
            if netlist_path.exists():
                return netlist_path
            return None
        except Exception as e:
            self._log(f"Netlist export error: {e}")
            return None
    
    def _parse_netlist(self, path: Path) -> Tuple[Dict, ET.Element]:
        """Parse netlist XML."""
        tree = ET.parse(path)
        root = tree.getroot()
        components = {}
        
        for comp in root.findall(".//components/comp"):
            ref = comp.get("ref", "")
            if not ref or ref.startswith("#"):
                continue
            
            footprint = (comp.findtext("footprint") or "").strip()
            value = (comp.findtext("value") or "").strip()
            tstamp = (comp.findtext("tstamp") or "").strip()
            
            dnp = False
            exclude = False
            
            # Check various locations for DNP/exclude
            if comp.get("dnp", "").lower() in ("yes", "true", "1"):
                dnp = True
            
            for prop in comp.findall("property"):
                pname = (prop.get("name") or "").lower()
                pval = (prop.get("value") or "").lower()
                if pname == "dnp" and pval in ("yes", "true", "1"):
                    dnp = True
                if pname in ("exclude_from_board", "exclude from board") and pval in ("yes", "true", "1"):
                    exclude = True
            
            fields = comp.find("fields")
            if fields is not None:
                for f in fields.findall("field"):
                    fname = (f.get("name") or "").lower()
                    fval = (f.text or "").lower()
                    if fname == "dnp" and fval in ("yes", "true", "1"):
                        dnp = True
                    if fname in ("exclude_from_board",) and fval in ("yes", "true", "1"):
                        exclude = True
            
            if value.upper() == "DNP":
                dnp = True
            if not footprint or footprint.lower() in ("", "none", "virtual"):
                exclude = True
            
            components[ref] = {
                "footprint": footprint,
                "value": value,
                "tstamp": tstamp,
                "dnp": dnp,
                "exclude": exclude,
            }
        
        return components, root
    
    def _filter_netlist(self, path: Path, board_name: str, components: Dict, root: ET.Element) -> Path:
        """Create filtered netlist."""
        filtered_path = path.with_suffix(".filtered.xml")
        exclude_refs = set()
        
        for ref, info in components.items():
            if info["dnp"] or info["exclude"]:
                exclude_refs.add(ref)
                continue
            
            assigned = self.config.assignments.get(ref, "")
            if assigned and assigned != board_name:
                exclude_refs.add(ref)
        
        # Remove from XML
        comps_elem = root.find("components")
        if comps_elem is not None:
            for comp in list(comps_elem.findall("comp")):
                if comp.get("ref") in exclude_refs:
                    comps_elem.remove(comp)
        
        nets_elem = root.find("nets")
        if nets_elem is not None:
            for net in nets_elem.findall("net"):
                for node in list(net.findall("node")):
                    if node.get("ref") in exclude_refs:
                        net.remove(node)
        
        tree = ET.ElementTree(root)
        tree.write(filtered_path, encoding="utf-8", xml_declaration=True)
        
        self._log(f"Filtered: excluded {len(exclude_refs)} components")
        return filtered_path
    
    # -------------------------------------------------------------------------
    # Update PCB from Schematic
    # -------------------------------------------------------------------------
    
    def update_board(self, board_name: str) -> Tuple[bool, str]:
        """Update sub-board from schematic."""
        board = self.config.boards.get(board_name)
        if not board:
            return False, f"Board '{board_name}' not found"
        
        pcb_path = self.project_dir / board.pcb_path
        if not pcb_path.exists():
            return False, f"PCB not found: {board.pcb_path}"
        
        self._log(f"Updating: {board_name}")
        
        # Refresh schematic links
        self._setup_board_project(board)
        
        # Export netlist
        netlist_path = self._export_netlist()
        if not netlist_path:
            return False, "Failed to export netlist"
        
        try:
            components, root = self._parse_netlist(netlist_path)
            filtered_path = self._filter_netlist(netlist_path, board_name, components, root)
            
            # Load board (NOT the currently open one - load fresh copy)
            board_obj = pcbnew.LoadBoard(str(pcb_path))
            if not board_obj:
                return False, "Failed to load PCB"
            
            # Get existing footprints
            existing = {fp.GetReference(): fp for fp in board_obj.GetFootprints()}
            
            added = 0
            updated = 0
            failed = 0
            failed_list = []
            
            # Process components from filtered netlist
            tree = ET.parse(filtered_path)
            filtered_root = tree.getroot()
            
            for comp in filtered_root.findall(".//components/comp"):
                ref = comp.get("ref", "")
                if not ref or ref.startswith("#"):
                    continue
                
                fp_id = (comp.findtext("footprint") or "").strip()
                value = (comp.findtext("value") or "").strip()
                tstamp = (comp.findtext("tstamp") or "").strip()
                
                if not fp_id:
                    continue
                
                if ":" in fp_id:
                    lib_nick, fp_name = fp_id.split(":", 1)
                else:
                    lib_nick, fp_name = "", fp_id
                
                if ref in existing:
                    # Update existing
                    fp = existing[ref]
                    try:
                        fp.SetValue(value)
                    except Exception:
                        pass
                    self._set_fp_path(fp, tstamp)
                    updated += 1
                else:
                    # Load new footprint
                    fp = self._load_footprint(lib_nick, fp_name)
                    if not fp:
                        failed += 1
                        failed_list.append(f"{ref}: {fp_id}")
                        self._log(f"Failed to load: {ref} ({fp_id})")
                        continue
                    
                    try:
                        fp.SetReference(ref)
                        fp.SetValue(value)
                        fp.SetPosition(pcbnew.VECTOR2I(0, 0))
                        self._set_fp_path(fp, tstamp)
                        board_obj.Add(fp)
                        existing[ref] = fp
                        added += 1
                    except Exception as e:
                        failed += 1
                        self._log(f"Failed to add {ref}: {e}")
            
            # Assign nets
            self._assign_nets(board_obj, filtered_root, existing)
            
            # Save
            pcbnew.SaveBoard(str(pcb_path), board_obj)
            
            # Update assignments
            for ref in existing:
                self.config.assignments[ref] = board_name
            self.save_config()
            
            # Cleanup
            for p in [netlist_path, filtered_path]:
                try:
                    p.unlink()
                except Exception:
                    pass
            
            msg = f"Updated {board_name}:\n• Added: {added}\n• Updated: {updated}\n• Failed: {failed}"
            if failed_list:
                msg += f"\n\nFailed (first 10):\n" + "\n".join(failed_list[:10])
            msg += "\n\nReload the PCB (File → Revert) to see changes."
            
            self._log(f"Done: added={added} updated={updated} failed={failed}")
            return True, msg
            
        except Exception as e:
            self._log(f"Update error: {e}\n{traceback.format_exc()}")
            return False, f"Update failed: {e}"
    
    def _load_footprint(self, lib_nick: str, fp_name: str) -> Optional["pcbnew.FOOTPRINT"]:
        """Load footprint from library."""
        
        # Method 1: Direct load with nickname (works if KiCad tables loaded)
        try:
            fp = pcbnew.FootprintLoad(lib_nick, fp_name)
            if fp:
                return fp
        except Exception:
            pass
        
        # Method 2: Use our parsed fp-lib-table cache
        if lib_nick in self._fp_lib_cache:
            lib_path = self._fp_lib_cache[lib_nick]
            try:
                fp = pcbnew.FootprintLoad(str(lib_path), fp_name)
                if fp:
                    return fp
            except Exception:
                pass
        
        # Method 3: Try project-local library
        local_lib = self.project_dir / f"{lib_nick}.pretty"
        if local_lib.exists():
            try:
                fp = pcbnew.FootprintLoad(str(local_lib), fp_name)
                if fp:
                    return fp
            except Exception:
                pass
        
        # Method 4: Try common KiCad paths
        for fp_dir in self._get_kicad_fp_dirs():
            lib_path = fp_dir / f"{lib_nick}.pretty"
            if lib_path.exists():
                try:
                    fp = pcbnew.FootprintLoad(str(lib_path), fp_name)
                    if fp:
                        return fp
                except Exception:
                    pass
        
        return None
    
    def _get_kicad_fp_dirs(self) -> List[Path]:
        """Get KiCad footprint directories."""
        dirs = []
        
        # From environment
        for var in ["KICAD9_FOOTPRINT_DIR", "KICAD8_FOOTPRINT_DIR", "KICAD_FOOTPRINT_DIR"]:
            val = os.environ.get(var)
            if val:
                dirs.append(Path(val))
        
        # Windows paths
        if os.name == "nt":
            for base in [
                Path(os.environ.get("ProgramFiles", "C:/Program Files")),
                Path(os.environ.get("LOCALAPPDATA", "") + "/Programs"),
            ]:
                for ver in ["9.0", "8.0"]:
                    fp = base / "KiCad" / ver / "share" / "kicad" / "footprints"
                    if fp.exists():
                        dirs.append(fp)
        
        return dirs
    
    def _set_fp_path(self, fp: "pcbnew.FOOTPRINT", tstamp: str):
        """Set footprint path for cross-probing."""
        if not tstamp:
            return
        try:
            path_str = f"/{tstamp}"
            if hasattr(pcbnew, "KIID_PATH"):
                fp.SetPath(pcbnew.KIID_PATH(path_str))
            else:
                fp.SetPath(path_str)
        except Exception:
            pass
    
    def _assign_nets(self, board: "pcbnew.BOARD", netlist_root: ET.Element, footprints: Dict):
        """Assign nets to pads."""
        nets = {}
        try:
            for name, net in board.GetNetsByName().items():
                nets[name] = net
        except Exception:
            pass
        
        def get_net(name):
            if name in nets:
                return nets[name]
            try:
                ni = pcbnew.NETINFO_ITEM(board, name)
                board.Add(ni)
                nets[name] = ni
                return ni
            except Exception:
                return None
        
        for net_elem in netlist_root.findall(".//nets/net"):
            net_name = net_elem.get("name", "")
            if not net_name:
                continue
            
            ni = get_net(net_name)
            if not ni:
                continue
            
            for node in net_elem.findall("node"):
                ref = node.get("ref", "")
                pin = node.get("pin", "")
                fp = footprints.get(ref)
                if not fp:
                    continue
                try:
                    pad = fp.FindPadByNumber(pin)
                    if pad:
                        pad.SetNet(ni)
                except Exception:
                    pass
    
    # -------------------------------------------------------------------------
    # Component Status
    # -------------------------------------------------------------------------
    
    def get_component_status(self) -> Tuple[Dict[str, str], Set[str]]:
        """Get placed/unplaced components."""
        placed = {}
        
        for name, board in self.config.boards.items():
            pcb_path = self.project_dir / board.pcb_path
            if not pcb_path.exists():
                continue
            try:
                b = pcbnew.LoadBoard(str(pcb_path))
                for fp in b.GetFootprints():
                    ref = fp.GetReference()
                    if ref and not ref.startswith("#"):
                        placed[ref] = name
            except Exception:
                pass
        
        self.config.assignments = placed
        self.save_config()
        
        # Get all from schematic
        all_refs = set()
        netlist = self._export_netlist()
        if netlist:
            try:
                comps, _ = self._parse_netlist(netlist)
                all_refs = {r for r, i in comps.items() if not i["dnp"] and not i["exclude"]}
                netlist.unlink()
            except Exception:
                pass
        
        return placed, all_refs - set(placed.keys())


# ============================================================================
# UI Dialogs
# ============================================================================

class NewBoardDialog(wx.Dialog):
    def __init__(self, parent, existing: Set[str]):
        super().__init__(parent, title="New Sub-Board", size=(400, 180))
        self.existing = existing
        self.result_name = ""
        self.result_desc = ""
        
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        
        sizer.Add(wx.StaticText(panel, label="Board Name:"), 0, wx.ALL, 5)
        self.txt_name = wx.TextCtrl(panel)
        sizer.Add(self.txt_name, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        
        sizer.Add(wx.StaticText(panel, label="Description:"), 0, wx.ALL, 5)
        self.txt_desc = wx.TextCtrl(panel)
        sizer.Add(self.txt_desc, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        
        btns = wx.BoxSizer(wx.HORIZONTAL)
        btn_ok = wx.Button(panel, wx.ID_OK, "Create")
        btn_cancel = wx.Button(panel, wx.ID_CANCEL, "Cancel")
        btn_ok.Bind(wx.EVT_BUTTON, self.on_ok)
        btns.Add(btn_ok, 0, wx.ALL, 5)
        btns.Add(btn_cancel, 0, wx.ALL, 5)
        sizer.Add(btns, 0, wx.ALIGN_CENTER | wx.ALL, 10)
        
        panel.SetSizer(sizer)
    
    def on_ok(self, event):
        name = self.txt_name.GetValue().strip()
        if not name:
            wx.MessageBox("Enter a board name.", "Error", wx.ICON_ERROR)
            return
        if name in self.existing:
            wx.MessageBox("Name already exists.", "Error", wx.ICON_ERROR)
            return
        self.result_name = name
        self.result_desc = self.txt_desc.GetValue().strip()
        self.EndModal(wx.ID_OK)


class StatusDialog(wx.Dialog):
    def __init__(self, parent, manager: MultiBoardManager):
        super().__init__(parent, title="Component Status", size=(550, 450))
        self.manager = manager
        
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        
        self.lbl = wx.StaticText(panel, label="Loading...")
        sizer.Add(self.lbl, 0, wx.ALL, 10)
        
        nb = wx.Notebook(panel)
        
        p1 = wx.Panel(nb)
        s1 = wx.BoxSizer(wx.VERTICAL)
        self.list_placed = wx.ListCtrl(p1, style=wx.LC_REPORT)
        self.list_placed.InsertColumn(0, "Ref", width=100)
        self.list_placed.InsertColumn(1, "Board", width=200)
        s1.Add(self.list_placed, 1, wx.EXPAND | wx.ALL, 5)
        p1.SetSizer(s1)
        nb.AddPage(p1, "Placed")
        
        p2 = wx.Panel(nb)
        s2 = wx.BoxSizer(wx.VERTICAL)
        self.list_unplaced = wx.ListCtrl(p2, style=wx.LC_REPORT)
        self.list_unplaced.InsertColumn(0, "Ref", width=150)
        s2.Add(self.list_unplaced, 1, wx.EXPAND | wx.ALL, 5)
        p2.SetSizer(s2)
        nb.AddPage(p2, "Unplaced")
        
        sizer.Add(nb, 1, wx.EXPAND | wx.ALL, 10)
        
        btns = wx.BoxSizer(wx.HORIZONTAL)
        btn_refresh = wx.Button(panel, label="Refresh")
        btn_refresh.Bind(wx.EVT_BUTTON, lambda e: self.refresh())
        btn_close = wx.Button(panel, wx.ID_CLOSE, "Close")
        btn_close.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        btns.Add(btn_refresh, 0, wx.ALL, 5)
        btns.AddStretchSpacer()
        btns.Add(btn_close, 0, wx.ALL, 5)
        sizer.Add(btns, 0, wx.EXPAND | wx.ALL, 10)
        
        panel.SetSizer(sizer)
        wx.CallAfter(self.refresh)
    
    def refresh(self):
        placed, unplaced = self.manager.get_component_status()
        
        self.lbl.SetLabel(f"Placed: {len(placed)} | Unplaced: {len(unplaced)}")
        
        self.list_placed.DeleteAllItems()
        for ref, board in sorted(placed.items()):
            idx = self.list_placed.InsertItem(self.list_placed.GetItemCount(), ref)
            self.list_placed.SetItem(idx, 1, board)
        
        self.list_unplaced.DeleteAllItems()
        for ref in sorted(unplaced):
            self.list_unplaced.InsertItem(self.list_unplaced.GetItemCount(), ref)


class MainDialog(wx.Dialog):
    def __init__(self, parent, board: pcbnew.BOARD):
        super().__init__(
            parent,
            title="Multi-Board Manager v7",
            size=(750, 500),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER
        )
        
        self.pcb = board
        board_path = board.GetFileName()
        project_dir = Path(board_path).parent if board_path else Path.cwd()
        
        self.manager = MultiBoardManager(project_dir)
        
        self._init_ui()
        self._refresh()
    
    def _init_ui(self):
        panel = wx.Panel(self)
        main = wx.BoxSizer(wx.VERTICAL)
        
        # Header
        title = wx.StaticText(panel, label="Multi-Board Manager")
        font = title.GetFont()
        font.SetPointSize(14)
        font.SetWeight(wx.FONTWEIGHT_BOLD)
        title.SetFont(font)
        main.Add(title, 0, wx.ALL, 10)
        
        # Info
        info = wx.FlexGridSizer(2, 2, 5, 20)
        info.Add(wx.StaticText(panel, label="Root Schematic:"))
        self.lbl_sch = wx.StaticText(panel, label=self.manager.config.root_schematic or "(none)")
        info.Add(self.lbl_sch)
        info.Add(wx.StaticText(panel, label="Root PCB:"))
        self.lbl_pcb = wx.StaticText(panel, label=self.manager.config.root_pcb or "(none)")
        info.Add(self.lbl_pcb)
        main.Add(info, 0, wx.LEFT | wx.BOTTOM, 10)
        
        # Board list
        main.Add(wx.StaticText(panel, label="Sub-Boards:"), 0, wx.LEFT, 10)
        
        self.list = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.list.InsertColumn(0, "Name", width=120)
        self.list.InsertColumn(1, "PCB Path", width=260)
        self.list.InsertColumn(2, "Components", width=80)
        self.list.InsertColumn(3, "Description", width=180)
        main.Add(self.list, 1, wx.EXPAND | wx.ALL, 10)
        
        # Buttons row 1
        row1 = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_new = wx.Button(panel, label="New Board")
        self.btn_remove = wx.Button(panel, label="Remove")
        self.btn_open = wx.Button(panel, label="Open PCB")
        self.btn_update = wx.Button(panel, label="Update from Schematic")
        
        self.btn_new.Bind(wx.EVT_BUTTON, self.on_new)
        self.btn_remove.Bind(wx.EVT_BUTTON, self.on_remove)
        self.btn_open.Bind(wx.EVT_BUTTON, self.on_open)
        self.btn_update.Bind(wx.EVT_BUTTON, self.on_update)
        
        row1.Add(self.btn_new, 0, wx.ALL, 3)
        row1.Add(self.btn_remove, 0, wx.ALL, 3)
        row1.AddSpacer(20)
        row1.Add(self.btn_open, 0, wx.ALL, 3)
        row1.Add(self.btn_update, 0, wx.ALL, 3)
        main.Add(row1, 0, wx.LEFT, 7)
        
        main.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.ALL, 10)
        
        # Buttons row 2
        row2 = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_status = wx.Button(panel, label="Component Status")
        self.btn_regen = wx.Button(panel, label="Regenerate Block")
        self.btn_log = wx.Button(panel, label="Debug Log")
        btn_close = wx.Button(panel, label="Close")
        
        self.btn_status.Bind(wx.EVT_BUTTON, self.on_status)
        self.btn_regen.Bind(wx.EVT_BUTTON, self.on_regen_block)
        self.btn_log.Bind(wx.EVT_BUTTON, self.on_log)
        btn_close.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        
        row2.Add(self.btn_status, 0, wx.ALL, 5)
        row2.Add(self.btn_regen, 0, wx.ALL, 5)
        row2.Add(self.btn_log, 0, wx.ALL, 5)
        row2.AddStretchSpacer()
        row2.Add(btn_close, 0, wx.ALL, 5)
        main.Add(row2, 0, wx.EXPAND | wx.ALL, 5)
        
        panel.SetSizer(main)
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_open)
    
    def _refresh(self):
        self.list.DeleteAllItems()
        counts = {}
        for ref, board in self.manager.config.assignments.items():
            counts[board] = counts.get(board, 0) + 1
        
        for name, board in self.manager.config.boards.items():
            idx = self.list.InsertItem(self.list.GetItemCount(), name)
            self.list.SetItem(idx, 1, board.pcb_path)
            self.list.SetItem(idx, 2, str(counts.get(name, 0)))
            self.list.SetItem(idx, 3, board.description)
    
    def _get_selected(self) -> Optional[str]:
        idx = self.list.GetFirstSelected()
        return self.list.GetItemText(idx) if idx >= 0 else None
    
    def on_new(self, event):
        dlg = NewBoardDialog(self, set(self.manager.config.boards.keys()))
        if dlg.ShowModal() == wx.ID_OK:
            ok, msg = self.manager.create_board(dlg.result_name, dlg.result_desc)
            if ok:
                wx.MessageBox(
                    f"Created: {dlg.result_name}\n\n"
                    f"Board block footprint: {BLOCK_LIB_NAME}:Block_{dlg.result_name}\n\n"
                    "Next: Click 'Update from Schematic' to add components.",
                    "Success", wx.ICON_INFORMATION
                )
                self._refresh()
            else:
                wx.MessageBox(msg, "Error", wx.ICON_ERROR)
        dlg.Destroy()
    
    def on_remove(self, event):
        name = self._get_selected()
        if not name:
            wx.MessageBox("Select a board.", "Info", wx.ICON_INFORMATION)
            return
        if wx.MessageBox(f"Remove '{name}'?", "Confirm", wx.YES_NO | wx.ICON_QUESTION) == wx.YES:
            del self.manager.config.boards[name]
            self.manager.config.assignments = {
                r: b for r, b in self.manager.config.assignments.items() if b != name
            }
            self.manager.save_config()
            self._refresh()
    
    def on_open(self, event):
        name = self._get_selected()
        if not name:
            wx.MessageBox("Select a board.", "Info", wx.ICON_INFORMATION)
            return
        
        board = self.manager.config.boards.get(name)
        if not board:
            return
        
        pcb_path = self.manager.project_dir / board.pcb_path
        if not pcb_path.exists():
            wx.MessageBox(f"PCB not found: {board.pcb_path}", "Error", wx.ICON_ERROR)
            return
        
        try:
            if os.name == "nt":
                os.startfile(str(pcb_path))
            else:
                subprocess.Popen(["pcbnew", str(pcb_path)])
        except Exception as e:
            wx.MessageBox(f"Failed: {e}", "Error", wx.ICON_ERROR)
    
    def on_update(self, event):
        name = self._get_selected()
        if not name:
            wx.MessageBox("Select a board.", "Info", wx.ICON_INFORMATION)
            return
        
        if wx.MessageBox(
            f"Update '{name}' from schematic?\n\n"
            "Components with DNP or exclude_from_board will be skipped.",
            "Confirm", wx.YES_NO | wx.ICON_QUESTION
        ) != wx.YES:
            return
        
        # Run update with busy cursor
        wx.BeginBusyCursor()
        try:
            ok, msg = self.manager.update_board(name)
        finally:
            wx.EndBusyCursor()
        
        if ok:
            wx.MessageBox(msg, "Complete", wx.ICON_INFORMATION)
        else:
            wx.MessageBox(msg, "Failed", wx.ICON_ERROR)
        
        self._refresh()
    
    def on_status(self, event):
        dlg = StatusDialog(self, self.manager)
        dlg.ShowModal()
        dlg.Destroy()
        self._refresh()
    
    def on_regen_block(self, event):
        name = self._get_selected()
        if not name:
            wx.MessageBox("Select a board.", "Info", wx.ICON_INFORMATION)
            return
        
        board = self.manager.config.boards.get(name)
        if board:
            self.manager._generate_block_footprint(board)
            wx.MessageBox(
                f"Regenerated: {BLOCK_LIB_NAME}:Block_{name}\n\n"
                "If already placed in root PCB, delete and re-add it.",
                "Done", wx.ICON_INFORMATION
            )
    
    def on_log(self, event):
        try:
            if os.name == "nt":
                os.startfile(str(self.manager.log_path))
            else:
                subprocess.Popen(["xdg-open", str(self.manager.log_path)])
        except Exception as e:
            wx.MessageBox(f"Failed: {e}", "Error", wx.ICON_ERROR)


# ============================================================================
# Plugin Registration
# ============================================================================

class MultiBoardPlugin(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "Multi-Board Manager"
        self.category = "Project Management"
        self.description = "Manage multiple PCBs from a single schematic"
        self.show_toolbar_button = True
        self.icon_file_name = os.path.join(os.path.dirname(__file__), "icon.png")
    
    def Run(self):
        board = pcbnew.GetBoard()
        if not board:
            wx.MessageBox("Open a PCB first.", "Error", wx.ICON_ERROR)
            return
        
        try:
            dlg = MainDialog(None, board)
            dlg.ShowModal()
            dlg.Destroy()
        except Exception as e:
            wx.MessageBox(f"Plugin error: {e}\n\nSee multiboard_debug.log", "Error", wx.ICON_ERROR)


MultiBoardPlugin().register()