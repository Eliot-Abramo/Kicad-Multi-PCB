"""
Multi-Board PCB Manager v9 - KiCad Action Plugin
=================================================
Multi-PCB workflow: one schematic, multiple board layouts.

Assignment logic:
- Components are assigned by WHAT'S ACTUALLY PLACED on PCBs
- Update adds all components not already on another board
- Delete a component from a board = it becomes available again
- No manual assignment needed (but available for reservations)

Features:
- Scan-based assignment (reads actual PCB contents)
- Footprint change detection and replacement
- Schematic hardlinks (changes sync automatically)
- Board block footprints for root PCB visualization
- Respects DNP and exclude_from_board

For KiCad 9.0+
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
    name: str
    side: str = "right"
    position: float = 0.5

@dataclass
class BoardConfig:
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
    version: str = "9.0"
    root_schematic: str = ""
    root_pcb: str = ""
    boards: Dict[str, BoardConfig] = field(default_factory=dict)
    # Reserved assignments (optional - for reserving components before placing)
    # Most assignments are determined by scanning PCBs
    reservations: Dict[str, str] = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "root_schematic": self.root_schematic,
            "root_pcb": self.root_pcb,
            "boards": {k: v.to_dict() for k, v in self.boards.items()},
            "reservations": self.reservations,
        }
    
    @classmethod
    def from_dict(cls, d: dict) -> "ProjectConfig":
        cfg = cls(
            version=d.get("version", "9.0"),
            root_schematic=d.get("root_schematic", ""),
            root_pcb=d.get("root_pcb", ""),
            reservations=d.get("reservations", d.get("assignments", {})),
        )
        for name, bd in d.get("boards", {}).items():
            if isinstance(bd, dict):
                cfg.boards[name] = BoardConfig.from_dict(bd)
        return cfg


# ============================================================================
# Core Manager
# ============================================================================

class MultiBoardManager:
    
    def __init__(self, project_dir: Path):
        self.project_dir = self._find_project_root(project_dir)
        self.config_path = self.project_dir / CONFIG_FILE
        self.config = ProjectConfig()
        self.block_lib_path = self.project_dir / f"{BLOCK_LIB_NAME}.pretty"
        
        self._fp_lib_cache: Dict[str, Path] = {}
        
        self.log_path = self.project_dir / "multiboard_debug.log"
        self.fault_path = self.project_dir / "multiboard_fault.log"
        self._init_logging()
        
        self._detect_root_files()
        self._load_config()
        self._parse_fp_lib_table()
        
        self._log(f"Init: project={self.project_dir}, libs={len(self._fp_lib_cache)}")
    
    def _find_project_root(self, start: Path) -> Path:
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
        except Exception:
            pass
    
    def _log(self, msg: str):
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {msg}\n")
        except Exception:
            pass
    
    def _detect_root_files(self):
        for pro in self.project_dir.glob("*.kicad_pro"):
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
        self._fp_lib_cache = {}
        
        project_table = self.project_dir / "fp-lib-table"
        if project_table.exists():
            self._log(f"Parsing: {project_table}")
            self._parse_single_fp_table(project_table)
        
        global_table = self._find_global_fp_lib_table()
        if global_table and global_table.exists():
            self._log(f"Parsing: {global_table}")
            self._parse_single_fp_table(global_table)
        
        # Fallback: scan KiCad footprints directory
        kicad_share = self._find_kicad_share()
        if kicad_share:
            fp_dir = kicad_share / "footprints"
            if fp_dir.exists():
                self._log(f"Scanning KiCad footprints: {fp_dir}")
                count = 0
                for lib in fp_dir.iterdir():
                    if lib.is_dir() and lib.suffix == ".pretty":
                        nick = lib.stem
                        if nick not in self._fp_lib_cache:
                            self._fp_lib_cache[nick] = lib
                            count += 1
                self._log(f"  Found {count} standard libraries")
        
        self._log(f"Total libraries: {len(self._fp_lib_cache)}")
    
    def _find_global_fp_lib_table(self) -> Optional[Path]:
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
            ]:
                if path.exists():
                    return path
        return None
    
    def _parse_single_fp_table(self, table_path: Path):
        try:
            content = table_path.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            self._log(f"Failed to read {table_path}: {e}")
            return
        
        # Multiple regex patterns for different formats
        # Pattern 1: All on one line (project tables)
        # Pattern 2: May have newlines (global tables)
        patterns = [
            re.compile(r'\(lib\s*\(name\s*"([^"]+)"\).*?\(uri\s*"([^"]+)"\)', re.DOTALL),
            re.compile(r'\(lib\s+\(name\s+"([^"]+)"\)\s*\(type\s+"[^"]+"\)\s*\(uri\s+"([^"]+)"\)', re.DOTALL),
            re.compile(r'\(name\s+"([^"]+)"\).*?\(uri\s+"([^"]+)"\)', re.DOTALL),
        ]
        
        found = 0
        for pattern in patterns:
            for match in pattern.finditer(content):
                nick = match.group(1)
                uri = match.group(2)
                
                if nick in self._fp_lib_cache:
                    continue  # Already found
                
                expanded = self._expand_uri(uri)
                if expanded:
                    path = Path(expanded)
                    self._fp_lib_cache[nick] = path
                    exists = "OK" if path.exists() else "NOT FOUND"
                    self._log(f"  {nick} -> {path} [{exists}]")
                    found += 1
        
        if found == 0:
            self._log(f"  WARNING: No libraries parsed from {table_path.name}")
    
    def _expand_uri(self, uri: str) -> Optional[str]:
        result = uri
        result = result.replace("${KIPRJMOD}", str(self.project_dir))
        
        kicad_share = self._find_kicad_share()
        
        replacements = {}
        if kicad_share:
            for v in ["9", "8", "7", ""]:
                var = f"KICAD{v}_FOOTPRINT_DIR" if v else "KICAD_FOOTPRINT_DIR"
                replacements[f"${{{var}}}"] = str(kicad_share / "footprints")
        
        for var in ["KICAD9_FOOTPRINT_DIR", "KICAD8_FOOTPRINT_DIR", "KICAD_FOOTPRINT_DIR"]:
            val = os.environ.get(var)
            if val:
                replacements[f"${{{var}}}"] = val
        
        for var, val in replacements.items():
            result = result.replace(var, val)
        
        if result.startswith("file://"):
            result = result[7:]
        
        if "${" in result:
            return None
        
        return result
    
    def _find_kicad_share(self) -> Optional[Path]:
        """Find KiCad share directory with footprints."""
        candidates = []
        
        if os.name == "nt":
            # Windows paths
            local = os.environ.get("LOCALAPPDATA", "")
            pf = os.environ.get("ProgramFiles", "")
            
            for base in [local + "/Programs/KiCad", pf + "/KiCad"]:
                base = Path(base)
                if base.exists():
                    for ver in sorted(base.iterdir(), reverse=True):
                        share = ver / "share" / "kicad"
                        if share.exists() and (share / "footprints").exists():
                            candidates.append(share)
        else:
            # Linux/Mac
            for share in [
                Path("/usr/share/kicad"),
                Path("/usr/local/share/kicad"),
                Path.home() / ".local/share/kicad",
            ]:
                if share.exists() and (share / "footprints").exists():
                    candidates.append(share)
        
        if candidates:
            self._log(f"Found KiCad share: {candidates[0]}")
            return candidates[0]
        
        return None
    
    # -------------------------------------------------------------------------
    # KiCad CLI
    # -------------------------------------------------------------------------
    
    def _find_kicad_cli(self) -> Optional[str]:
        exe = shutil.which("kicad-cli")
        if exe:
            return exe
        
        if os.name == "nt":
            for base in [
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "KiCad",
                Path(os.environ.get("ProgramFiles", "")) / "KiCad",
            ]:
                if base.exists():
                    for ver_dir in sorted(base.iterdir(), reverse=True):
                        cli = ver_dir / "bin" / "kicad-cli.exe"
                        if cli.exists():
                            return str(cli)
        return None
    
    def _run_cli(self, args: List[str]) -> subprocess.CompletedProcess:
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
    # Schematic Linking
    # -------------------------------------------------------------------------
    
    def _collect_all_sheets(self, root_sch: Path) -> Set[Path]:
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
            
            for pat in [r'\(property\s+"Sheetfile"\s+"([^"]+\.kicad_sch)"',
                       r'\(sheetfile\s+"([^"]+\.kicad_sch)"']:
                for ref in re.findall(pat, content, re.IGNORECASE):
                    sheets.add(Path(ref))
                    full = (sch.parent / ref).resolve()
                    if full.exists():
                        stack.append(full)
        
        return sheets
    
    def _link_file(self, src: Path, dst: Path) -> bool:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            
            try:
                os.link(str(src), str(dst))
                return True
            except Exception:
                pass
            
            try:
                os.symlink(str(src), str(dst))
                return True
            except Exception:
                pass
            
            return False
        except Exception:
            return False
    
    def _setup_board_project(self, board: BoardConfig):
        pcb_path = self.project_dir / board.pcb_path
        board_dir = pcb_path.parent
        base = pcb_path.stem
        
        board_dir.mkdir(parents=True, exist_ok=True)
        
        # .kicad_pro
        pro = board_dir / f"{base}.kicad_pro"
        if not pro.exists():
            pro.write_text(json.dumps({"meta": {"filename": pro.name}}, indent=2))
        
        # Link schematics
        if self.config.root_schematic:
            root_sch = self.project_dir / self.config.root_schematic
            board_sch = board_dir / f"{base}.kicad_sch"
            
            if root_sch.exists():
                self._link_file(root_sch, board_sch)
                
                for sheet in self._collect_all_sheets(root_sch):
                    src = (root_sch.parent / sheet).resolve()
                    dst = board_dir / sheet
                    if src.exists():
                        self._link_file(src, dst)
        
        # Library tables
        self._setup_board_lib_tables(board_dir)
    
    def _setup_board_lib_tables(self, board_dir: Path):
        root_abs = self.project_dir.as_posix()
        
        for table in ("fp-lib-table", "sym-lib-table"):
            src = self.project_dir / table
            dst = board_dir / table
            
            if src.exists():
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
        if name in self.config.boards:
            return False, f"Board '{name}' already exists"
        
        safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in name)
        
        board_dir = self.project_dir / BOARDS_DIR / safe
        pcb_path = board_dir / f"{safe}.kicad_pcb"
        rel_path = f"{BOARDS_DIR}/{safe}/{safe}.kicad_pcb"
        
        if pcb_path.exists():
            return False, f"PCB already exists: {rel_path}"
        
        board_dir.mkdir(parents=True, exist_ok=True)
        
        if not self._create_empty_pcb(pcb_path):
            return False, "Failed to create PCB"
        
        board = BoardConfig(name=name, pcb_path=rel_path, description=description)
        self._setup_board_project(board)
        self._generate_block_footprint(board)
        self._add_block_lib_to_table()
        
        self.config.boards[name] = board
        self.save_config()
        
        self._log(f"Created: {name} at {rel_path}")
        return True, rel_path
    
    def _create_empty_pcb(self, path: Path) -> bool:
        content = '''(kicad_pcb
  (version 20240108)
  (generator "multiboard")
  (generator_version "9.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal) (31 "B.Cu" signal)
    (32 "B.Adhes" user) (33 "F.Adhes" user)
    (34 "B.Paste" user) (35 "F.Paste" user)
    (36 "B.SilkS" user) (37 "F.SilkS" user)
    (38 "B.Mask" user) (39 "F.Mask" user)
    (40 "Dwgs.User" user) (41 "Cmts.User" user)
    (42 "Eco1.User" user) (43 "Eco2.User" user)
    (44 "Edge.Cuts" user) (45 "Margin" user)
    (46 "B.CrtYd" user) (47 "F.CrtYd" user)
    (48 "B.Fab" user) (49 "F.Fab" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
)
'''
        try:
            path.write_text(content, encoding="utf-8")
            return True
        except Exception:
            return False
    
    # -------------------------------------------------------------------------
    # Board Block Footprint
    # -------------------------------------------------------------------------
    
    def _generate_block_footprint(self, board: BoardConfig) -> bool:
        self.block_lib_path.mkdir(parents=True, exist_ok=True)
        
        fp_name = f"Block_{board.name}"
        fp_path = self.block_lib_path / f"{fp_name}.kicad_mod"
        
        w, h = board.block_width, board.block_height
        
        content = f'''(footprint "{fp_name}"
  (version 20240108)
  (generator "multiboard")
  (layer "F.Cu")
  (descr "Board block: {board.name}")
  (attr board_only exclude_from_pos_files exclude_from_bom)
  (fp_text reference "MB_{board.name}" (at 0 {-h/2-2:.1f}) (layer "F.SilkS")
    (effects (font (size 1.5 1.5) (thickness 0.15))))
  (fp_text value "{board.name}" (at 0 0) (layer "F.Fab")
    (effects (font (size 2 2) (thickness 0.2))))
  (fp_rect (start {-w/2:.1f} {-h/2:.1f}) (end {w/2:.1f} {h/2:.1f})
    (stroke (width 0.3) (type solid)) (fill none) (layer "F.SilkS"))
  (fp_rect (start {-w/2-1:.1f} {-h/2-1:.1f}) (end {w/2+1:.1f} {h/2+1:.1f})
    (stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd"))
)
'''
        try:
            fp_path.write_text(content, encoding="utf-8")
            return True
        except Exception:
            return False
    
    def _add_block_lib_to_table(self):
        table = self.project_dir / "fp-lib-table"
        entry = f'  (lib (name "{BLOCK_LIB_NAME}")(type "KiCad")(uri "${{KIPRJMOD}}/{BLOCK_LIB_NAME}.pretty")(options "")(descr ""))'
        
        if table.exists():
            content = table.read_text(encoding="utf-8", errors="ignore")
            if BLOCK_LIB_NAME in content:
                return
            content = content.rstrip().rstrip(')')
            content += f'\n{entry}\n)'
        else:
            content = f'(fp_lib_table\n  (version 7)\n{entry}\n)'
        
        table.write_text(content, encoding="utf-8")
    
    # -------------------------------------------------------------------------
    # PCB Scanning - Determine what's placed where
    # -------------------------------------------------------------------------
    
    def scan_all_boards(self) -> Dict[str, Tuple[str, str]]:
        """
        Scan all board PCBs to find placed components.
        Returns: {ref: (board_name, footprint_id)}
        """
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
                        # Get footprint ID (lib:name)
                        fpid = fp.GetFPID()
                        lib = fpid.GetLibNickname() if hasattr(fpid, 'GetLibNickname') else ""
                        fpname = fpid.GetLibItemName() if hasattr(fpid, 'GetLibItemName') else ""
                        fp_str = f"{lib}:{fpname}" if lib else fpname
                        placed[ref] = (name, fp_str)
            except Exception as e:
                self._log(f"Scan error {name}: {e}")
        
        return placed
    
    # -------------------------------------------------------------------------
    # Netlist
    # -------------------------------------------------------------------------
    
    def _export_netlist(self) -> Optional[Path]:
        if not self.config.root_schematic:
            return None
        
        sch = self.project_dir / self.config.root_schematic
        if not sch.exists():
            return None
        
        netlist = self.project_dir / ".multiboard_netlist.xml"
        
        try:
            self._run_cli([
                "sch", "export", "netlist",
                "--format", "kicadxml",
                "-o", str(netlist),
                str(sch)
            ])
            return netlist if netlist.exists() else None
        except Exception:
            return None
    
    def _parse_netlist(self, path: Path) -> Tuple[Dict, ET.Element]:
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
            
            for prop in comp.findall("property"):
                pname = (prop.get("name") or "").lower()
                pval = (prop.get("value") or "").lower()
                if pname == "dnp" and pval in ("yes", "true", "1"):
                    dnp = True
                if "exclude" in pname and "board" in pname and pval in ("yes", "true", "1"):
                    exclude = True
            
            fields = comp.find("fields")
            if fields is not None:
                for f in fields.findall("field"):
                    fname = (f.get("name") or "").lower()
                    fval = (f.text or "").lower()
                    if fname == "dnp" and fval in ("yes", "true", "1"):
                        dnp = True
            
            if value.upper() == "DNP":
                dnp = True
            if not footprint:
                exclude = True
            
            components[ref] = {
                "footprint": footprint,
                "value": value,
                "tstamp": tstamp,
                "dnp": dnp,
                "exclude": exclude,
            }
        
        return components, root
    
    def _filter_netlist_for_board(self, path: Path, board_name: str, 
                                   components: Dict, root: ET.Element,
                                   placed: Dict[str, Tuple[str, str]]) -> Path:
        """
        Filter netlist for a specific board.
        Include: not placed anywhere OR placed on this board OR reserved for this board
        Exclude: placed on different board OR reserved for different board OR DNP/exclude
        """
        filtered = path.with_suffix(".filtered.xml")
        exclude_refs = set()
        
        for ref, info in components.items():
            # Always exclude DNP and exclude_from_board
            if info["dnp"] or info["exclude"]:
                exclude_refs.add(ref)
                continue
            
            # Check if placed on another board
            if ref in placed:
                placed_board, _ = placed[ref]
                if placed_board != board_name:
                    exclude_refs.add(ref)
                    continue
            
            # Check reservations
            reserved = self.config.reservations.get(ref, "")
            if reserved and reserved != board_name:
                exclude_refs.add(ref)
                continue
        
        # Remove from XML
        comps = root.find("components")
        if comps is not None:
            for comp in list(comps.findall("comp")):
                if comp.get("ref") in exclude_refs:
                    comps.remove(comp)
        
        nets = root.find("nets")
        if nets is not None:
            for net in nets.findall("net"):
                for node in list(net.findall("node")):
                    if node.get("ref") in exclude_refs:
                        net.remove(node)
        
        tree = ET.ElementTree(root)
        tree.write(filtered, encoding="utf-8", xml_declaration=True)
        
        included = len(components) - len(exclude_refs)
        self._log(f"Filter {board_name}: {included} included, {len(exclude_refs)} excluded")
        return filtered
    
    # -------------------------------------------------------------------------
    # Update PCB
    # -------------------------------------------------------------------------
    
    def update_board(self, board_name: str) -> Tuple[bool, str]:
        board = self.config.boards.get(board_name)
        if not board:
            return False, f"Board '{board_name}' not found"
        
        pcb_path = self.project_dir / board.pcb_path
        if not pcb_path.exists():
            return False, f"PCB not found: {board.pcb_path}"
        
        self._log(f"=== Updating: {board_name} ===")
        
        # Refresh schematic links
        self._setup_board_project(board)
        
        # Scan what's already placed on ALL boards
        placed = self.scan_all_boards()
        self._log(f"Scanned {len(placed)} placed components across all boards")
        
        # Export netlist
        netlist_path = self._export_netlist()
        if not netlist_path:
            return False, "Failed to export netlist"
        
        try:
            components, root = self._parse_netlist(netlist_path)
            filtered_path = self._filter_netlist_for_board(
                netlist_path, board_name, components, root, placed
            )
            
            # Load target board
            board_obj = pcbnew.LoadBoard(str(pcb_path))
            if not board_obj:
                return False, "Failed to load PCB"
            
            existing = {}
            existing_fp = {}
            for fp in board_obj.GetFootprints():
                ref = fp.GetReference()
                if ref:
                    existing[ref] = fp
                    fpid = fp.GetFPID()
                    lib = fpid.GetLibNickname() if hasattr(fpid, 'GetLibNickname') else ""
                    fpname = fpid.GetLibItemName() if hasattr(fpid, 'GetLibItemName') else ""
                    existing_fp[ref] = f"{lib}:{fpname}" if lib else fpname
            
            # Parse filtered netlist
            tree = ET.parse(filtered_path)
            froot = tree.getroot()
            
            added = 0
            updated = 0
            replaced = 0
            failed = 0
            failed_list = []
            
            for comp in froot.findall(".//components/comp"):
                ref = comp.get("ref", "")
                if not ref or ref.startswith("#"):
                    continue
                
                fp_id = (comp.findtext("footprint") or "").strip()
                value = (comp.findtext("value") or "").strip()
                tstamp = (comp.findtext("tstamp") or "").strip()
                
                if not fp_id:
                    continue
                
                lib_nick, fp_name = (fp_id.split(":", 1) + [""])[:2] if ":" in fp_id else ("", fp_id)
                
                if ref in existing:
                    fp = existing[ref]
                    old_fp_id = existing_fp.get(ref, "")
                    
                    # Check if footprint changed
                    if old_fp_id != fp_id:
                        self._log(f"Footprint change: {ref} {old_fp_id} -> {fp_id}")
                        # Load new footprint and replace
                        new_fp = self._load_footprint(lib_nick, fp_name)
                        if new_fp:
                            # Preserve position and rotation
                            pos = fp.GetPosition()
                            rot = fp.GetOrientationDegrees()
                            layer = fp.GetLayer()
                            
                            # Remove old, add new
                            board_obj.Remove(fp)
                            new_fp.SetReference(ref)
                            new_fp.SetValue(value)
                            new_fp.SetPosition(pos)
                            new_fp.SetOrientationDegrees(rot)
                            new_fp.SetLayer(layer)
                            self._set_fp_path(new_fp, tstamp)
                            board_obj.Add(new_fp)
                            existing[ref] = new_fp
                            replaced += 1
                        else:
                            self._log(f"Failed to replace: {ref}")
                            # Keep old footprint, just update value
                            try:
                                fp.SetValue(value)
                            except Exception:
                                pass
                            updated += 1
                    else:
                        # Same footprint, just update value
                        try:
                            fp.SetValue(value)
                        except Exception:
                            pass
                        self._set_fp_path(fp, tstamp)
                        updated += 1
                else:
                    # New component
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
                        self._log(f"Add error {ref}: {e}")
            
            # Assign nets
            self._assign_nets(board_obj, froot, existing)
            
            # Save
            pcbnew.SaveBoard(str(pcb_path), board_obj)
            
            # Cleanup
            for p in [netlist_path, filtered_path]:
                try:
                    p.unlink()
                except Exception:
                    pass
            
            self._log(f"Done: added={added} updated={updated} replaced={replaced} failed={failed}")
            
            msg = f"Updated {board_name}:\n\n"
            msg += f"• Added: {added}\n"
            msg += f"• Updated: {updated}\n"
            if replaced:
                msg += f"• Footprints replaced: {replaced}\n"
            msg += f"• Failed: {failed}"
            
            if failed_list:
                msg += f"\n\nFailed (first 10):\n" + "\n".join(failed_list[:10])
            
            msg += "\n\nReload PCB: File → Revert"
            
            return True, msg
            
        except Exception as e:
            self._log(f"Error: {e}\n{traceback.format_exc()}")
            return False, f"Failed: {e}"
    
    def _load_footprint(self, lib_nick: str, fp_name: str) -> Optional["pcbnew.FOOTPRINT"]:
        # Method 1: From cache
        if lib_nick in self._fp_lib_cache:
            lib_path = self._fp_lib_cache[lib_nick]
            try:
                fp = pcbnew.FootprintLoad(str(lib_path), fp_name)
                if fp:
                    return fp
            except Exception:
                pass
        
        # Method 2: Project local
        local = self.project_dir / f"{lib_nick}.pretty"
        if local.exists():
            try:
                fp = pcbnew.FootprintLoad(str(local), fp_name)
                if fp:
                    return fp
            except Exception:
                pass
        
        # Method 3: lib/lib_fp folder (common pattern)
        lib_fp = self.project_dir / "lib" / "lib_fp" / f"{lib_nick}.pretty"
        if lib_fp.exists():
            try:
                fp = pcbnew.FootprintLoad(str(lib_fp), fp_name)
                if fp:
                    return fp
            except Exception:
                pass
        
        # Method 4: KiCad standard library locations
        kicad_share = self._find_kicad_share()
        if kicad_share:
            std_lib = kicad_share / "footprints" / f"{lib_nick}.pretty"
            if std_lib.exists():
                try:
                    fp = pcbnew.FootprintLoad(str(std_lib), fp_name)
                    if fp:
                        return fp
                except Exception:
                    pass
        
        # Method 5: Direct load (might work if KiCad has tables loaded)
        try:
            fp = pcbnew.FootprintLoad(lib_nick, fp_name)
            if fp:
                return fp
        except Exception:
            pass
        
        return None
    
    def _set_fp_path(self, fp, tstamp: str):
        if not tstamp:
            return
        try:
            path = f"/{tstamp}"
            if hasattr(pcbnew, "KIID_PATH"):
                fp.SetPath(pcbnew.KIID_PATH(path))
            else:
                fp.SetPath(path)
        except Exception:
            pass
    
    def _assign_nets(self, board, netlist_root: ET.Element, footprints: Dict):
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
                ref, pin = node.get("ref", ""), node.get("pin", "")
                fp = footprints.get(ref)
                if fp:
                    try:
                        pad = fp.FindPadByNumber(pin)
                        if pad:
                            pad.SetNet(ni)
                    except Exception:
                        pass
    
    # -------------------------------------------------------------------------
    # Status
    # -------------------------------------------------------------------------
    
    def get_schematic_components(self) -> Dict[str, dict]:
        netlist = self._export_netlist()
        if not netlist:
            return {}
        
        try:
            comps, _ = self._parse_netlist(netlist)
            netlist.unlink()
            return comps
        except Exception:
            return {}
    
    def get_status(self) -> Tuple[Dict[str, str], Set[str], int]:
        """
        Returns: (placed_dict, unplaced_set, total_valid)
        placed_dict: {ref: board_name}
        """
        placed_raw = self.scan_all_boards()
        placed = {ref: board for ref, (board, _) in placed_raw.items()}
        
        all_comps = self.get_schematic_components()
        valid = {r for r, i in all_comps.items() if not i["dnp"] and not i["exclude"]}
        unplaced = valid - set(placed.keys())
        
        return placed, unplaced, len(valid)


# ============================================================================
# UI Dialogs
# ============================================================================

class NewBoardDialog(wx.Dialog):
    def __init__(self, parent, existing: Set[str]):
        super().__init__(parent, title="Create New Sub-Board", 
                        size=(450, 200),
                        style=wx.DEFAULT_DIALOG_STYLE)
        self.existing = existing
        self.result_name = ""
        self.result_desc = ""
        
        panel = wx.Panel(self)
        main = wx.BoxSizer(wx.VERTICAL)
        
        # Form
        grid = wx.FlexGridSizer(2, 2, 10, 10)
        grid.AddGrowableCol(1, 1)
        
        grid.Add(wx.StaticText(panel, label="Board Name:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_name = wx.TextCtrl(panel, size=(280, -1))
        grid.Add(self.txt_name, 1, wx.EXPAND)
        
        grid.Add(wx.StaticText(panel, label="Description:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_desc = wx.TextCtrl(panel, size=(280, -1))
        grid.Add(self.txt_desc, 1, wx.EXPAND)
        
        main.Add(grid, 0, wx.EXPAND | wx.ALL, 20)
        
        # Info
        info = wx.StaticText(panel, label="PCB will be created in: boards/<name>/<name>.kicad_pcb")
        info.SetForegroundColour(wx.Colour(100, 100, 100))
        main.Add(info, 0, wx.LEFT | wx.BOTTOM, 20)
        
        main.AddStretchSpacer()
        
        # Buttons
        btns = wx.StdDialogButtonSizer()
        btn_ok = wx.Button(panel, wx.ID_OK, "Create")
        btn_cancel = wx.Button(panel, wx.ID_CANCEL)
        btn_ok.Bind(wx.EVT_BUTTON, self.on_ok)
        btn_ok.SetDefault()
        btns.AddButton(btn_ok)
        btns.AddButton(btn_cancel)
        btns.Realize()
        main.Add(btns, 0, wx.EXPAND | wx.ALL, 15)
        
        panel.SetSizer(main)
        self.txt_name.SetFocus()
    
    def on_ok(self, event):
        name = self.txt_name.GetValue().strip()
        if not name:
            wx.MessageBox("Enter a board name.", "Error", wx.ICON_ERROR)
            return
        if name in self.existing:
            wx.MessageBox(f"'{name}' already exists.", "Error", wx.ICON_ERROR)
            return
        if not name[0].isalpha():
            wx.MessageBox("Name must start with a letter.", "Error", wx.ICON_ERROR)
            return
        
        self.result_name = name
        self.result_desc = self.txt_desc.GetValue().strip()
        self.EndModal(wx.ID_OK)


class StatusDialog(wx.Dialog):
    def __init__(self, parent, manager: MultiBoardManager):
        super().__init__(parent, title="Component Status", size=(650, 500),
                        style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.manager = manager
        
        panel = wx.Panel(self)
        main = wx.BoxSizer(wx.VERTICAL)
        
        self.lbl = wx.StaticText(panel, label="Scanning boards...")
        self.lbl.SetFont(self.lbl.GetFont().Bold())
        main.Add(self.lbl, 0, wx.ALL, 10)
        
        nb = wx.Notebook(panel)
        
        # By Board tab
        p0 = wx.Panel(nb)
        s0 = wx.BoxSizer(wx.VERTICAL)
        self.tree = wx.TreeCtrl(p0, style=wx.TR_DEFAULT_STYLE | wx.TR_HIDE_ROOT)
        s0.Add(self.tree, 1, wx.EXPAND | wx.ALL, 5)
        p0.SetSizer(s0)
        nb.AddPage(p0, "By Board")
        
        # Unplaced tab
        p2 = wx.Panel(nb)
        s2 = wx.BoxSizer(wx.VERTICAL)
        self.list_unplaced = wx.ListCtrl(p2, style=wx.LC_REPORT)
        self.list_unplaced.InsertColumn(0, "Reference", width=120)
        self.list_unplaced.InsertColumn(1, "Value", width=150)
        self.list_unplaced.InsertColumn(2, "Footprint", width=250)
        s2.Add(self.list_unplaced, 1, wx.EXPAND | wx.ALL, 5)
        p2.SetSizer(s2)
        nb.AddPage(p2, "Unplaced")
        
        main.Add(nb, 1, wx.EXPAND | wx.ALL, 10)
        
        btns = wx.BoxSizer(wx.HORIZONTAL)
        btn_refresh = wx.Button(panel, label="Refresh")
        btn_refresh.Bind(wx.EVT_BUTTON, lambda e: self.refresh())
        btn_close = wx.Button(panel, wx.ID_CLOSE)
        btn_close.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        btns.Add(btn_refresh, 0, wx.RIGHT, 10)
        btns.AddStretchSpacer()
        btns.Add(btn_close, 0)
        main.Add(btns, 0, wx.EXPAND | wx.ALL, 10)
        
        panel.SetSizer(main)
        wx.CallAfter(self.refresh)
    
    def refresh(self):
        placed, unplaced, total = self.manager.get_status()
        comps = self.manager.get_schematic_components()
        
        self.lbl.SetLabel(f"Total: {total} | Placed: {len(placed)} | Unplaced: {len(unplaced)}")
        
        # Tree by board
        self.tree.DeleteAllItems()
        root = self.tree.AddRoot("Boards")
        
        by_board = {}
        for ref, board in placed.items():
            if board not in by_board:
                by_board[board] = []
            by_board[board].append(ref)
        
        for board_name in sorted(by_board.keys()):
            refs = sorted(by_board[board_name])
            board_node = self.tree.AppendItem(root, f"{board_name} ({len(refs)} components)")
            for ref in refs:
                self.tree.AppendItem(board_node, ref)
        
        self.tree.ExpandAll()
        
        # Unplaced list
        self.list_unplaced.DeleteAllItems()
        for ref in sorted(unplaced):
            info = comps.get(ref, {})
            idx = self.list_unplaced.InsertItem(self.list_unplaced.GetItemCount(), ref)
            self.list_unplaced.SetItem(idx, 1, info.get("value", ""))
            self.list_unplaced.SetItem(idx, 2, info.get("footprint", ""))


class MainDialog(wx.Dialog):
    def __init__(self, parent, board: pcbnew.BOARD):
        super().__init__(parent, title="Multi-Board Manager v9",
                        size=(750, 520),
                        style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        
        self.pcb = board
        path = board.GetFileName()
        self.manager = MultiBoardManager(Path(path).parent if path else Path.cwd())
        
        self._build_ui()
        self._refresh()
    
    def _build_ui(self):
        panel = wx.Panel(self)
        main = wx.BoxSizer(wx.VERTICAL)
        
        # Header
        header = wx.BoxSizer(wx.HORIZONTAL)
        title = wx.StaticText(panel, label="Multi-Board Manager")
        title.SetFont(wx.Font(14, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        header.Add(title, 0, wx.ALIGN_CENTER_VERTICAL)
        main.Add(header, 0, wx.ALL, 10)
        
        # Info box
        info_box = wx.StaticBox(panel, label="How it works")
        info_sizer = wx.StaticBoxSizer(info_box, wx.VERTICAL)
        info_text = (
            "• Update adds all components NOT already on another board\n"
            "• Delete components in PCBnew to make them available for other boards\n"
            "• Schematic changes (values, footprints) sync automatically on update"
        )
        info_lbl = wx.StaticText(panel, label=info_text)
        info_lbl.SetForegroundColour(wx.Colour(60, 60, 60))
        info_sizer.Add(info_lbl, 0, wx.ALL, 5)
        main.Add(info_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        
        # Board list
        lbl = wx.StaticText(panel, label="Sub-Boards:")
        lbl.SetFont(lbl.GetFont().Bold())
        main.Add(lbl, 0, wx.LEFT, 10)
        
        self.list = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.list.InsertColumn(0, "Name", width=130)
        self.list.InsertColumn(1, "PCB Path", width=300)
        self.list.InsertColumn(2, "Placed", width=70)
        self.list.InsertColumn(3, "Description", width=150)
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
        
        row1.Add(self.btn_new, 0, wx.RIGHT, 5)
        row1.Add(self.btn_remove, 0, wx.RIGHT, 20)
        row1.Add(self.btn_open, 0, wx.RIGHT, 5)
        row1.Add(self.btn_update, 0)
        main.Add(row1, 0, wx.LEFT | wx.BOTTOM, 10)
        
        main.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.ALL, 5)
        
        # Row 2
        row2 = wx.BoxSizer(wx.HORIZONTAL)
        
        self.btn_status = wx.Button(panel, label="Status")
        self.btn_regen = wx.Button(panel, label="Regen Block")
        self.btn_log = wx.Button(panel, label="Log")
        btn_close = wx.Button(panel, label="Close")
        
        self.btn_status.Bind(wx.EVT_BUTTON, self.on_status)
        self.btn_regen.Bind(wx.EVT_BUTTON, self.on_regen)
        self.btn_log.Bind(wx.EVT_BUTTON, self.on_log)
        btn_close.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        
        row2.Add(self.btn_status, 0, wx.RIGHT, 5)
        row2.Add(self.btn_regen, 0, wx.RIGHT, 5)
        row2.Add(self.btn_log, 0)
        row2.AddStretchSpacer()
        row2.Add(btn_close, 0)
        main.Add(row2, 0, wx.EXPAND | wx.ALL, 10)
        
        panel.SetSizer(main)
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_open)
    
    def _refresh(self):
        self.list.DeleteAllItems()
        
        # Scan boards for component counts
        placed = self.manager.scan_all_boards()
        counts = {}
        for ref, (board, _) in placed.items():
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
                    f"PCB: {msg}\n\n"
                    "Click 'Update from Schematic' to add components.",
                    "Success", wx.ICON_INFORMATION
                )
                self._refresh()
            else:
                wx.MessageBox(msg, "Error", wx.ICON_ERROR)
        dlg.Destroy()
    
    def on_remove(self, event):
        name = self._get_selected()
        if not name:
            return
        if wx.MessageBox(f"Remove '{name}'?\n\nPCB files will NOT be deleted.", 
                        "Confirm", wx.YES_NO | wx.ICON_QUESTION) == wx.YES:
            del self.manager.config.boards[name]
            self.manager.save_config()
            self._refresh()
    
    def on_open(self, event):
        name = self._get_selected()
        if not name:
            return
        
        board = self.manager.config.boards.get(name)
        if not board:
            return
        
        pcb = self.manager.project_dir / board.pcb_path
        if not pcb.exists():
            wx.MessageBox(f"PCB not found: {board.pcb_path}", "Error", wx.ICON_ERROR)
            return
        
        try:
            if os.name == "nt":
                os.startfile(str(pcb))
            else:
                subprocess.Popen(["pcbnew", str(pcb)])
        except Exception as e:
            wx.MessageBox(f"Failed: {e}", "Error", wx.ICON_ERROR)
    
    def on_update(self, event):
        name = self._get_selected()
        if not name:
            wx.MessageBox("Select a board first.", "Info", wx.ICON_INFORMATION)
            return
        
        # Quick preview of what will happen
        placed = self.manager.scan_all_boards()
        comps = self.manager.get_schematic_components()
        
        on_this = sum(1 for r, (b, _) in placed.items() if b == name)
        on_other = sum(1 for r, (b, _) in placed.items() if b != name)
        available = len([r for r, i in comps.items() 
                        if not i["dnp"] and not i["exclude"] and r not in placed])
        
        msg = (
            f"Update '{name}'?\n\n"
            f"• Already on this board: {on_this} (will update)\n"
            f"• On other boards: {on_other} (excluded)\n"
            f"• Available to add: {available}\n\n"
            f"Footprint changes will be detected and replaced."
        )
        
        if wx.MessageBox(msg, "Confirm", wx.YES_NO | wx.ICON_QUESTION) != wx.YES:
            return
        
        wx.BeginBusyCursor()
        try:
            ok, result = self.manager.update_board(name)
        finally:
            wx.EndBusyCursor()
        
        if ok:
            wx.MessageBox(result, "Complete", wx.ICON_INFORMATION)
        else:
            wx.MessageBox(result, "Failed", wx.ICON_ERROR)
        
        self._refresh()
    
    def on_status(self, event):
        dlg = StatusDialog(self, self.manager)
        dlg.ShowModal()
        dlg.Destroy()
    
    def on_regen(self, event):
        name = self._get_selected()
        if not name:
            return
        
        board = self.manager.config.boards.get(name)
        if board:
            self.manager._generate_block_footprint(board)
            wx.MessageBox(f"Regenerated: {BLOCK_LIB_NAME}:Block_{name}", "Done", wx.ICON_INFORMATION)
    
    def on_log(self, event):
        try:
            if os.name == "nt":
                os.startfile(str(self.manager.log_path))
            else:
                subprocess.Popen(["xdg-open", str(self.manager.log_path)])
        except Exception:
            pass


# ============================================================================
# Plugin
# ============================================================================

class MultiBoardPlugin(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "Multi-Board Manager"
        self.category = "Project"
        self.description = "Manage multiple PCBs from one schematic"
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
            wx.MessageBox(f"Error: {e}", "Error", wx.ICON_ERROR)


MultiBoardPlugin().register()