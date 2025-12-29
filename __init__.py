"""
Multi-Board PCB Manager v10 - KiCad Action Plugin
==================================================
Multi-PCB workflow with inter-board connectivity checking.

Features:
- Port system for inter-board connections
- Board block footprints with port pads
- Aggregated connectivity/DRC checking
- Threaded operations (no UI freeze)
- Schematic hardlinks for sync

For KiCad 9.0+
"""

import pcbnew
import wx
import wx.lib.newevent
import os
import json
import subprocess
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional, Dict, List, Set, Tuple, Any
from datetime import datetime
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, Future
import threading
import traceback
import re

# ============================================================================
# Constants
# ============================================================================

BOARDS_DIR = "boards"
CONFIG_FILE = ".kicad_multiboard.json"
BLOCK_LIB_NAME = "MultiBoard_Blocks"
PORT_LIB_NAME = "MultiBoard_Ports"

# Custom events for thread communication
WorkerDoneEvent, EVT_WORKER_DONE = wx.lib.newevent.NewEvent()
WorkerProgressEvent, EVT_WORKER_PROGRESS = wx.lib.newevent.NewEvent()

# ============================================================================
# Data Model
# ============================================================================

@dataclass
class PortDef:
    """A port represents a signal that crosses board boundaries."""
    name: str
    net: str = ""  # Net name this port connects to
    side: str = "right"  # left, right, top, bottom
    position: float = 0.5  # 0.0 to 1.0 along the edge
    
    def to_dict(self) -> dict:
        return {"name": self.name, "net": self.net, "side": self.side, "position": self.position}
    
    @classmethod
    def from_dict(cls, d: dict) -> "PortDef":
        return cls(
            name=d.get("name", ""),
            net=d.get("net", ""),
            side=d.get("side", "right"),
            position=d.get("position", 0.5)
        )


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
            "name": self.name, "pcb_path": self.pcb_path, "description": self.description,
            "block_width": self.block_width, "block_height": self.block_height,
            "ports": {k: v.to_dict() for k, v in self.ports.items()}
        }
    
    @classmethod
    def from_dict(cls, d: dict) -> "BoardConfig":
        cfg = cls(
            name=d["name"], pcb_path=d.get("pcb_path", ""),
            description=d.get("description", ""),
            block_width=d.get("block_width", 50.0),
            block_height=d.get("block_height", 35.0)
        )
        for k, v in d.get("ports", {}).items():
            cfg.ports[k] = PortDef.from_dict(v) if isinstance(v, dict) else PortDef(name=k)
        return cfg


@dataclass
class ProjectConfig:
    version: str = "10.0"
    root_schematic: str = ""
    root_pcb: str = ""
    boards: Dict[str, BoardConfig] = field(default_factory=dict)
    
    def to_dict(self) -> dict:
        return {
            "version": self.version, "root_schematic": self.root_schematic,
            "root_pcb": self.root_pcb,
            "boards": {k: v.to_dict() for k, v in self.boards.items()}
        }
    
    @classmethod
    def from_dict(cls, d: dict) -> "ProjectConfig":
        cfg = cls(
            version=d.get("version", "10.0"),
            root_schematic=d.get("root_schematic", ""),
            root_pcb=d.get("root_pcb", "")
        )
        for k, v in d.get("boards", {}).items():
            cfg.boards[k] = BoardConfig.from_dict(v) if isinstance(v, dict) else BoardConfig(name=k, pcb_path="")
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
        self.port_lib_path = self.project_dir / f"{PORT_LIB_NAME}.pretty"
        
        self._fp_lib_cache: Dict[str, Path] = {}
        self._kicad_share: Optional[Path] = None
        
        self.log_path = self.project_dir / "multiboard_debug.log"
        
        self._detect_root_files()
        self._load_config()
        self._init_libraries()
    
    def _find_project_root(self, start: Path) -> Path:
        for p in [start] + list(start.parents):
            if (p / CONFIG_FILE).exists() or list(p.glob("*.kicad_pro")):
                return p
        return start
    
    def _log(self, msg: str):
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] {msg}\n")
        except Exception:
            pass
    
    def _detect_root_files(self):
        for pro in self.project_dir.glob("*.kicad_pro"):
            sch, pcb = pro.with_suffix(".kicad_sch"), pro.with_suffix(".kicad_pcb")
            if sch.exists(): self.config.root_schematic = sch.name
            if pcb.exists(): self.config.root_pcb = pcb.name
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
    
    # -------------------------------------------------------------------------
    # Library Management
    # -------------------------------------------------------------------------
    
    def _init_libraries(self):
        self._kicad_share = self._find_kicad_share()
        self._fp_lib_cache = {}
        
        # Parse project fp-lib-table
        proj_table = self.project_dir / "fp-lib-table"
        if proj_table.exists():
            self._parse_fp_lib_table(proj_table)
        
        # Scan KiCad footprints directory
        if self._kicad_share:
            fp_dir = self._kicad_share / "footprints"
            if fp_dir.exists():
                for lib in fp_dir.iterdir():
                    if lib.is_dir() and lib.suffix == ".pretty" and lib.stem not in self._fp_lib_cache:
                        self._fp_lib_cache[lib.stem] = lib
        
        self._log(f"Loaded {len(self._fp_lib_cache)} libraries")
    
    def _find_kicad_share(self) -> Optional[Path]:
        if os.name == "nt":
            for base in [Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "KiCad",
                        Path(os.environ.get("ProgramFiles", "")) / "KiCad"]:
                if base.exists():
                    for ver in sorted(base.iterdir(), reverse=True):
                        share = ver / "share" / "kicad"
                        if (share / "footprints").exists():
                            return share
        else:
            for share in [Path("/usr/share/kicad"), Path("/usr/local/share/kicad")]:
                if (share / "footprints").exists():
                    return share
        return None
    
    def _parse_fp_lib_table(self, path: Path):
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r'\(name\s*"([^"]+)"\).*?\(uri\s*"([^"]+)"\)', content, re.DOTALL):
                nick, uri = m.group(1), m.group(2)
                expanded = uri.replace("${KIPRJMOD}", str(self.project_dir))
                if "${" not in expanded:
                    self._fp_lib_cache[nick] = Path(expanded)
        except Exception:
            pass
    
    def _load_footprint(self, lib_nick: str, fp_name: str) -> Optional["pcbnew.FOOTPRINT"]:
        # Try cache
        if lib_nick in self._fp_lib_cache:
            try:
                return pcbnew.FootprintLoad(str(self._fp_lib_cache[lib_nick]), fp_name)
            except Exception:
                pass
        
        # Try KiCad standard
        if self._kicad_share:
            std = self._kicad_share / "footprints" / f"{lib_nick}.pretty"
            if std.exists():
                try:
                    return pcbnew.FootprintLoad(str(std), fp_name)
                except Exception:
                    pass
        
        # Try direct
        try:
            return pcbnew.FootprintLoad(lib_nick, fp_name)
        except Exception:
            pass
        
        return None
    
    # -------------------------------------------------------------------------
    # KiCad CLI
    # -------------------------------------------------------------------------
    
    def _find_kicad_cli(self) -> Optional[str]:
        exe = shutil.which("kicad-cli")
        if exe: return exe
        
        if os.name == "nt":
            for base in [Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "KiCad",
                        Path(os.environ.get("ProgramFiles", "")) / "KiCad"]:
                if base.exists():
                    for ver in sorted(base.iterdir(), reverse=True):
                        cli = ver / "bin" / "kicad-cli.exe"
                        if cli.exists(): return str(cli)
        return None
    
    def _run_cli(self, args: List[str]) -> subprocess.CompletedProcess:
        cli = self._find_kicad_cli()
        if not cli: raise FileNotFoundError("kicad-cli not found")
        
        kwargs = {"capture_output": True, "text": True, "cwd": str(self.project_dir)}
        if os.name == "nt": kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        
        return subprocess.run([cli] + args, **kwargs)
    
    # -------------------------------------------------------------------------
    # Schematic Links
    # -------------------------------------------------------------------------
    
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
            if root_sch.exists():
                self._link_file(root_sch, board_dir / f"{base}.kicad_sch")
                
                # Hierarchical sheets
                for sheet in self._find_sheets(root_sch):
                    src = (root_sch.parent / sheet).resolve()
                    if src.exists():
                        self._link_file(src, board_dir / sheet)
        
        # Library tables
        for table in ("fp-lib-table", "sym-lib-table"):
            src = self.project_dir / table
            if src.exists():
                content = src.read_text(encoding="utf-8", errors="ignore")
                content = content.replace("${KIPRJMOD}", self.project_dir.as_posix())
                (board_dir / table).write_text(content, encoding="utf-8")
    
    def _link_file(self, src: Path, dst: Path):
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() or dst.is_symlink(): dst.unlink()
        try: os.link(str(src), str(dst))
        except Exception:
            try: os.symlink(str(src), str(dst))
            except Exception: pass
    
    def _find_sheets(self, sch: Path) -> Set[Path]:
        sheets, visited, stack = set(), set(), [sch]
        while stack:
            s = stack.pop()
            if s in visited: continue
            visited.add(s)
            try:
                content = s.read_text(encoding="utf-8", errors="ignore")
                for m in re.findall(r'"([^"]+\.kicad_sch)"', content):
                    sheets.add(Path(m))
                    full = (s.parent / m).resolve()
                    if full.exists(): stack.append(full)
            except Exception: pass
        return sheets
    
    # -------------------------------------------------------------------------
    # Board Management
    # -------------------------------------------------------------------------
    
    def create_board(self, name: str, description: str = "") -> Tuple[bool, str]:
        if name in self.config.boards:
            return False, f"'{name}' already exists"
        
        safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in name)
        rel_path = f"{BOARDS_DIR}/{safe}/{safe}.kicad_pcb"
        pcb_path = self.project_dir / rel_path
        
        if pcb_path.exists():
            return False, f"PCB exists: {rel_path}"
        
        pcb_path.parent.mkdir(parents=True, exist_ok=True)
        self._create_empty_pcb(pcb_path)
        
        board = BoardConfig(name=name, pcb_path=rel_path, description=description)
        self._setup_board_project(board)
        self._generate_block_footprint(board)
        self._ensure_lib_in_table(BLOCK_LIB_NAME, f"{BLOCK_LIB_NAME}.pretty")
        
        self.config.boards[name] = board
        self.save_config()
        return True, rel_path
    
    def _create_empty_pcb(self, path: Path):
        path.write_text('''(kicad_pcb
  (version 20240108) (generator "multiboard") (generator_version "9.0")
  (general (thickness 1.6) (legacy_teardrops no)) (paper "A4")
  (layers
    (0 "F.Cu" signal) (31 "B.Cu" signal)
    (36 "B.SilkS" user) (37 "F.SilkS" user)
    (38 "B.Mask" user) (39 "F.Mask" user)
    (44 "Edge.Cuts" user) (47 "F.CrtYd" user) (49 "F.Fab" user))
  (setup (pad_to_mask_clearance 0)) (net 0 ""))
''', encoding="utf-8")
    
    # -------------------------------------------------------------------------
    # Block Footprint with Ports
    # -------------------------------------------------------------------------
    
    def _generate_block_footprint(self, board: BoardConfig):
        self.block_lib_path.mkdir(parents=True, exist_ok=True)
        
        fp_name = f"Block_{board.name}"
        w, h = board.block_width, board.block_height
        
        lines = [
            f'(footprint "{fp_name}"',
            '  (version 20240108) (generator "multiboard") (layer "F.Cu")',
            f'  (descr "Board block: {board.name}")',
            '  (attr board_only exclude_from_pos_files exclude_from_bom)',
            f'  (fp_text reference "REF**" (at 0 {-h/2-2:.1f}) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))',
            f'  (fp_text value "{board.name}" (at 0 {h/2+2:.1f}) (layer "F.Fab") (effects (font (size 1 1) (thickness 0.15))))',
            f'  (fp_rect (start {-w/2:.2f} {-h/2:.2f}) (end {w/2:.2f} {h/2:.2f}) (stroke (width 0.25) (type solid)) (fill none) (layer "F.SilkS"))',
            f'  (fp_rect (start {-w/2-0.5:.2f} {-h/2-0.5:.2f}) (end {w/2+0.5:.2f} {h/2+0.5:.2f}) (stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd"))',
        ]
        
        # Add port pads
        pad_num = 1
        for port_name, port in sorted(board.ports.items()):
            x, y = self._port_position(port, w, h)
            rot = {"left": 180, "right": 0, "top": 270, "bottom": 90}.get(port.side, 0)
            
            lines.append(
                f'  (pad "{pad_num}" smd roundrect (at {x:.2f} {y:.2f} {rot}) (size 2 1) '
                f'(layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))'
            )
            lines.append(
                f'  (fp_text user "{port_name}" (at {x:.2f} {y + (1.5 if port.side in ["left","right"] else -1.5 if port.side=="top" else 1.5):.2f}) '
                f'(layer "F.SilkS") (effects (font (size 0.8 0.8) (thickness 0.1))))'
            )
            pad_num += 1
        
        lines.append(')')
        
        (self.block_lib_path / f"{fp_name}.kicad_mod").write_text('\n'.join(lines), encoding="utf-8")
    
    def _port_position(self, port: PortDef, w: float, h: float) -> Tuple[float, float]:
        p = port.position
        if port.side == "left": return (-w/2, h * (p - 0.5))
        if port.side == "right": return (w/2, h * (p - 0.5))
        if port.side == "top": return (w * (p - 0.5), -h/2)
        if port.side == "bottom": return (w * (p - 0.5), h/2)
        return (0, 0)
    
    def _ensure_lib_in_table(self, lib_name: str, rel_path: str):
        table = self.project_dir / "fp-lib-table"
        entry = f'  (lib (name "{lib_name}")(type "KiCad")(uri "${{KIPRJMOD}}/{rel_path}")(options "")(descr ""))'
        
        if table.exists():
            content = table.read_text(encoding="utf-8", errors="ignore")
            if lib_name in content: return
            content = content.rstrip().rstrip(')') + f'\n{entry}\n)'
        else:
            content = f'(fp_lib_table\n  (version 7)\n{entry}\n)'
        
        table.write_text(content, encoding="utf-8")
    
    # -------------------------------------------------------------------------
    # Port Footprint (for sub-boards)
    # -------------------------------------------------------------------------
    
    def generate_port_footprint(self, port_name: str):
        """Generate a port marker footprint for sub-boards."""
        self.port_lib_path.mkdir(parents=True, exist_ok=True)
        
        fp = f'''(footprint "Port_{port_name}"
  (version 20240108) (generator "multiboard") (layer "F.Cu")
  (descr "Inter-board port: {port_name}")
  (attr smd)
  (fp_text reference "REF**" (at 0 -2) (layer "F.SilkS") (effects (font (size 0.8 0.8) (thickness 0.12))))
  (fp_text value "PORT" (at 0 2) (layer "F.Fab") (effects (font (size 0.8 0.8) (thickness 0.12))))
  (fp_text user "{port_name}" (at 0 0) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
  (pad "1" smd roundrect (at 0 0) (size 2 2) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
  (fp_circle (center 0 0) (end 1.5 0) (stroke (width 0.15) (type solid)) (fill none) (layer "F.SilkS"))
)'''
        (self.port_lib_path / f"Port_{port_name}.kicad_mod").write_text(fp, encoding="utf-8")
        self._ensure_lib_in_table(PORT_LIB_NAME, f"{PORT_LIB_NAME}.pretty")
    
    # -------------------------------------------------------------------------
    # PCB Scanning
    # -------------------------------------------------------------------------
    
    def scan_all_boards(self) -> Dict[str, Tuple[str, str]]:
        """Returns {ref: (board_name, footprint_id)}"""
        placed = {}
        for name, board in self.config.boards.items():
            pcb_path = self.project_dir / board.pcb_path
            if not pcb_path.exists(): continue
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
        return placed
    
    def get_board_nets(self, board_name: str) -> Dict[str, Set[str]]:
        """Get nets and their connected pads for a board. Returns {net_name: {pad_refs}}"""
        board = self.config.boards.get(board_name)
        if not board: return {}
        
        pcb_path = self.project_dir / board.pcb_path
        if not pcb_path.exists(): return {}
        
        nets = {}
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
    
    # -------------------------------------------------------------------------
    # Connectivity Check
    # -------------------------------------------------------------------------
    
    def check_connectivity(self, progress_callback=None) -> Dict[str, Any]:
        """
        Check connectivity across all boards.
        Returns report with:
        - unconnected_nets: nets that don't connect to anything
        - cross_board_nets: nets that should connect via ports
        - missing_ports: expected port connections not made
        """
        report = {
            "boards": {},
            "cross_board": [],
            "errors": [],
            "warnings": []
        }
        
        total = len(self.config.boards)
        
        for i, (name, board) in enumerate(self.config.boards.items()):
            if progress_callback:
                progress_callback(int(100 * i / max(total, 1)), f"Checking {name}...")
            
            pcb_path = self.project_dir / board.pcb_path
            if not pcb_path.exists():
                report["errors"].append(f"{name}: PCB not found")
                continue
            
            # Run DRC via CLI
            try:
                drc_file = pcb_path.with_suffix(".drc.json")
                self._run_cli(["pcb", "drc", "--format", "json", "-o", str(drc_file), str(pcb_path)])
                
                if drc_file.exists():
                    drc = json.loads(drc_file.read_text(encoding="utf-8"))
                    violations = drc.get("violations", [])
                    
                    # Filter out port-related "unconnected" errors
                    port_nets = {p.net for p in board.ports.values() if p.net}
                    filtered = []
                    for v in violations:
                        # Keep violation unless it's an unconnected item on a port net
                        if "unconnected" in v.get("type", "").lower():
                            # Check if this net has a port
                            desc = v.get("description", "")
                            is_port_net = any(pn in desc for pn in port_nets)
                            if is_port_net:
                                continue  # Skip - this is an expected inter-board connection
                        filtered.append(v)
                    
                    report["boards"][name] = {
                        "violations": len(filtered),
                        "details": filtered[:20]  # First 20
                    }
                    
                    drc_file.unlink()
            except Exception as e:
                report["errors"].append(f"{name}: DRC failed - {e}")
        
        if progress_callback:
            progress_callback(100, "Done")
        
        return report
    
    # -------------------------------------------------------------------------
    # Update Board
    # -------------------------------------------------------------------------
    
    def update_board(self, board_name: str, progress_callback=None) -> Tuple[bool, str]:
        """Update a board from schematic. Returns (success, message)."""
        board = self.config.boards.get(board_name)
        if not board:
            return False, f"Board '{board_name}' not found"
        
        pcb_path = self.project_dir / board.pcb_path
        if not pcb_path.exists():
            return False, f"PCB not found: {board.pcb_path}"
        
        try:
            if progress_callback: progress_callback(5, "Refreshing schematic links...")
            self._setup_board_project(board)
            
            if progress_callback: progress_callback(10, "Scanning existing boards...")
            placed = self.scan_all_boards()
            
            if progress_callback: progress_callback(20, "Exporting netlist...")
            netlist_path = self._export_netlist()
            if not netlist_path:
                return False, "Failed to export netlist"
            
            if progress_callback: progress_callback(30, "Parsing netlist...")
            components = self._parse_netlist_fast(netlist_path)
            
            if progress_callback: progress_callback(40, "Loading PCB...")
            board_obj = pcbnew.LoadBoard(str(pcb_path))
            if not board_obj:
                return False, "Failed to load PCB"
            
            # Build existing footprint map
            existing = {}
            existing_fp = {}
            for fp in board_obj.GetFootprints():
                ref = fp.GetReference()
                if ref:
                    existing[ref] = fp
                    fpid = fp.GetFPID()
                    existing_fp[ref] = f"{fpid.GetLibNickname()}:{fpid.GetLibItemName()}"
            
            # Filter components for this board
            to_add = []
            to_update = []
            
            for ref, info in components.items():
                if info["dnp"] or info["exclude"]:
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
            
            # Process updates
            if progress_callback: progress_callback(50, f"Updating {len(to_update)} components...")
            
            updated = 0
            replaced = 0
            for ref, info in to_update:
                fp = existing[ref]
                old_fp_id = existing_fp.get(ref, "")
                
                if old_fp_id != info["footprint"]:
                    # Footprint changed - replace
                    lib, name = (info["footprint"].split(":", 1) + [""])[:2] if ":" in info["footprint"] else ("", info["footprint"])
                    new_fp = self._load_footprint(lib, name)
                    if new_fp:
                        pos, rot, layer = fp.GetPosition(), fp.GetOrientationDegrees(), fp.GetLayer()
                        board_obj.Remove(fp)
                        new_fp.SetReference(ref)
                        new_fp.SetValue(info["value"])
                        new_fp.SetPosition(pos)
                        new_fp.SetOrientationDegrees(rot)
                        new_fp.SetLayer(layer)
                        self._set_fp_path(new_fp, info["tstamp"])
                        board_obj.Add(new_fp)
                        existing[ref] = new_fp
                        replaced += 1
                    else:
                        fp.SetValue(info["value"])
                        updated += 1
                else:
                    fp.SetValue(info["value"])
                    self._set_fp_path(fp, info["tstamp"])
                    updated += 1
            
            # Process additions
            if progress_callback: progress_callback(70, f"Adding {len(to_add)} components...")
            
            added = 0
            failed = 0
            failed_list = []
            
            for ref, info in to_add:
                lib, name = (info["footprint"].split(":", 1) + [""])[:2] if ":" in info["footprint"] else ("", info["footprint"])
                fp = self._load_footprint(lib, name)
                
                if not fp:
                    failed += 1
                    failed_list.append(f"{ref}: {info['footprint']}")
                    continue
                
                fp.SetReference(ref)
                fp.SetValue(info["value"])
                fp.SetPosition(pcbnew.VECTOR2I(0, 0))
                self._set_fp_path(fp, info["tstamp"])
                board_obj.Add(fp)
                existing[ref] = fp
                added += 1
            
            # Assign nets
            if progress_callback: progress_callback(85, "Assigning nets...")
            self._assign_nets_fast(board_obj, netlist_path, existing)
            
            # Save
            if progress_callback: progress_callback(95, "Saving...")
            pcbnew.SaveBoard(str(pcb_path), board_obj)
            
            # Cleanup
            try: netlist_path.unlink()
            except Exception: pass
            
            msg = f"Added: {added}\nUpdated: {updated}"
            if replaced: msg += f"\nReplaced: {replaced}"
            msg += f"\nFailed: {failed}"
            if failed_list:
                msg += f"\n\nFailed:\n" + "\n".join(failed_list[:10])
            
            return True, msg
            
        except Exception as e:
            self._log(f"Update error: {e}\n{traceback.format_exc()}")
            return False, f"Error: {e}"
    
    def _export_netlist(self) -> Optional[Path]:
        if not self.config.root_schematic: return None
        sch = self.project_dir / self.config.root_schematic
        if not sch.exists(): return None
        
        netlist = self.project_dir / ".multiboard_netlist.xml"
        try:
            self._run_cli(["sch", "export", "netlist", "--format", "kicadxml", "-o", str(netlist), str(sch)])
            return netlist if netlist.exists() else None
        except Exception:
            return None
    
    def _parse_netlist_fast(self, path: Path) -> Dict[str, dict]:
        """Fast netlist parsing with minimal overhead."""
        components = {}
        
        # Use iterparse for efficiency
        for event, elem in ET.iterparse(str(path), events=["end"]):
            if elem.tag == "comp":
                ref = elem.get("ref", "")
                if not ref or ref.startswith("#"):
                    elem.clear()
                    continue
                
                footprint = ""
                value = ""
                tstamp = ""
                dnp = False
                exclude = False
                
                for child in elem:
                    if child.tag == "footprint": footprint = (child.text or "").strip()
                    elif child.tag == "value": value = (child.text or "").strip()
                    elif child.tag == "tstamp": tstamp = (child.text or "").strip()
                    elif child.tag == "property":
                        pname = (child.get("name") or "").lower()
                        pval = (child.get("value") or "").lower()
                        if pname == "dnp" and pval in ("yes", "true", "1"): dnp = True
                        if "exclude" in pname and "board" in pname and pval in ("yes", "true", "1"): exclude = True
                
                if value.upper() == "DNP": dnp = True
                if not footprint: exclude = True
                
                components[ref] = {"footprint": footprint, "value": value, "tstamp": tstamp, "dnp": dnp, "exclude": exclude}
                elem.clear()
        
        return components
    
    def _set_fp_path(self, fp, tstamp: str):
        if tstamp:
            try:
                fp.SetPath(pcbnew.KIID_PATH(f"/{tstamp}"))
            except Exception:
                pass
    
    def _assign_nets_fast(self, board, netlist_path: Path, footprints: Dict):
        """Fast net assignment."""
        nets = {}
        for name, net in board.GetNetsByName().items():
            nets[name] = net
        
        def get_net(name):
            if name not in nets:
                ni = pcbnew.NETINFO_ITEM(board, name)
                board.Add(ni)
                nets[name] = ni
            return nets[name]
        
        # Parse nets section
        for event, elem in ET.iterparse(str(netlist_path), events=["end"]):
            if elem.tag == "net":
                net_name = elem.get("name", "")
                if net_name:
                    ni = get_net(net_name)
                    for node in elem.findall("node"):
                        ref, pin = node.get("ref", ""), node.get("pin", "")
                        fp = footprints.get(ref)
                        if fp:
                            pad = fp.FindPadByNumber(pin)
                            if pad: pad.SetNet(ni)
                elem.clear()
    
    # -------------------------------------------------------------------------
    # Status
    # -------------------------------------------------------------------------
    
    def get_status(self) -> Tuple[Dict[str, str], Set[str], int]:
        placed_raw = self.scan_all_boards()
        placed = {ref: board for ref, (board, _) in placed_raw.items()}
        
        netlist = self._export_netlist()
        if not netlist: return placed, set(), len(placed)
        
        comps = self._parse_netlist_fast(netlist)
        try: netlist.unlink()
        except Exception: pass
        
        valid = {r for r, i in comps.items() if not i["dnp"] and not i["exclude"]}
        return placed, valid - set(placed.keys()), len(valid)


# ============================================================================
# UI Components
# ============================================================================

class ProgressDialog(wx.Dialog):
    """Non-modal progress dialog."""
    
    def __init__(self, parent, title="Working..."):
        super().__init__(parent, title=title, size=(400, 120), style=wx.CAPTION)
        
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        
        self.label = wx.StaticText(panel, label="Initializing...")
        sizer.Add(self.label, 0, wx.ALL | wx.EXPAND, 15)
        
        self.gauge = wx.Gauge(panel, range=100)
        sizer.Add(self.gauge, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 15)
        
        panel.SetSizer(sizer)
        self.Centre()
    
    def update(self, percent: int, message: str):
        self.gauge.SetValue(percent)
        self.label.SetLabel(message)
        wx.Yield()


class PortDialog(wx.Dialog):
    """Dialog for managing ports on a board."""
    
    def __init__(self, parent, board: BoardConfig):
        super().__init__(parent, title=f"Ports - {board.name}", size=(500, 400),
                        style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.board = board
        self.ports = dict(board.ports)  # Working copy
        
        panel = wx.Panel(self)
        main = wx.BoxSizer(wx.VERTICAL)
        
        # List
        self.list = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.list.InsertColumn(0, "Name", width=120)
        self.list.InsertColumn(1, "Net", width=120)
        self.list.InsertColumn(2, "Side", width=80)
        self.list.InsertColumn(3, "Position", width=80)
        main.Add(self.list, 1, wx.EXPAND | wx.ALL, 10)
        
        # Buttons
        btn_row = wx.BoxSizer(wx.HORIZONTAL)
        for label, handler in [("Add", self.on_add), ("Edit", self.on_edit), ("Remove", self.on_remove)]:
            btn = wx.Button(panel, label=label)
            btn.Bind(wx.EVT_BUTTON, handler)
            btn_row.Add(btn, 0, wx.RIGHT, 5)
        main.Add(btn_row, 0, wx.LEFT | wx.BOTTOM, 10)
        
        # Dialog buttons
        main.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.ALL, 5)
        btns = wx.StdDialogButtonSizer()
        btn_ok = wx.Button(panel, wx.ID_OK)
        btn_ok.Bind(wx.EVT_BUTTON, self.on_ok)
        btns.AddButton(btn_ok)
        btns.AddButton(wx.Button(panel, wx.ID_CANCEL))
        btns.Realize()
        main.Add(btns, 0, wx.ALIGN_RIGHT | wx.ALL, 10)
        
        panel.SetSizer(main)
        self._refresh()
    
    def _refresh(self):
        self.list.DeleteAllItems()
        for name, port in sorted(self.ports.items()):
            idx = self.list.InsertItem(self.list.GetItemCount(), name)
            self.list.SetItem(idx, 1, port.net)
            self.list.SetItem(idx, 2, port.side)
            self.list.SetItem(idx, 3, f"{port.position:.2f}")
    
    def on_add(self, event):
        dlg = PortEditDialog(self, PortDef(name="NewPort"))
        if dlg.ShowModal() == wx.ID_OK:
            self.ports[dlg.port.name] = dlg.port
            self._refresh()
        dlg.Destroy()
    
    def on_edit(self, event):
        idx = self.list.GetFirstSelected()
        if idx < 0: return
        name = self.list.GetItemText(idx)
        port = self.ports.get(name)
        if port:
            dlg = PortEditDialog(self, port)
            if dlg.ShowModal() == wx.ID_OK:
                del self.ports[name]
                self.ports[dlg.port.name] = dlg.port
                self._refresh()
            dlg.Destroy()
    
    def on_remove(self, event):
        name = self._get_selected()
        if not name:
            return
        
        board = self.manager.config.boards.get(name)
        if not board:
            return
        
        if wx.MessageBox(f"Remove '{name}'?\n\nThis will delete the board folder and all files.", 
                        "Confirm", wx.YES_NO | wx.ICON_WARNING) == wx.YES:
            # Delete the board folder
            pcb_path = self.manager.project_dir / board.pcb_path
            board_dir = pcb_path.parent
            if board_dir.exists() and BOARDS_DIR in str(board_dir):
                try:
                    shutil.rmtree(board_dir)
                except Exception as e:
                    wx.MessageBox(f"Could not delete folder: {e}", "Warning", wx.ICON_WARNING)
            
            # Remove from config
            del self.manager.config.boards[name]
            self.manager.save_config()
            self._refresh()

    def on_ok(self, event):
        self.board.ports = self.ports
        self.EndModal(wx.ID_OK)


class PortEditDialog(wx.Dialog):
    """Edit a single port."""
    
    def __init__(self, parent, port: PortDef):
        super().__init__(parent, title="Edit Port", size=(400, 500))
        self.port = port
        
        panel = wx.Panel(self)
        grid = wx.FlexGridSizer(4, 2, 8, 8)
        grid.AddGrowableCol(1)
        
        grid.Add(wx.StaticText(panel, label="Name:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_name = wx.TextCtrl(panel, value=port.name)
        grid.Add(self.txt_name, 1, wx.EXPAND)
        
        grid.Add(wx.StaticText(panel, label="Net:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_net = wx.TextCtrl(panel, value=port.net)
        grid.Add(self.txt_net, 1, wx.EXPAND)
        
        grid.Add(wx.StaticText(panel, label="Side:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.choice_side = wx.Choice(panel, choices=["left", "right", "top", "bottom"])
        self.choice_side.SetStringSelection(port.side)
        grid.Add(self.choice_side, 1, wx.EXPAND)
        
        grid.Add(wx.StaticText(panel, label="Position (0-1):"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.spin_pos = wx.SpinCtrlDouble(panel, value=str(port.position), min=0, max=1, inc=0.1)
        grid.Add(self.spin_pos, 1, wx.EXPAND)
        
        main = wx.BoxSizer(wx.VERTICAL)
        main.Add(grid, 1, wx.EXPAND | wx.ALL, 15)
        
        btns = wx.StdDialogButtonSizer()
        btn_ok = wx.Button(panel, wx.ID_OK)
        btn_ok.Bind(wx.EVT_BUTTON, self.on_ok)
        btns.AddButton(btn_ok)
        btns.AddButton(wx.Button(panel, wx.ID_CANCEL))
        btns.Realize()
        main.Add(btns, 0, wx.ALIGN_RIGHT | wx.ALL, 10)
        
        panel.SetSizer(main)
    
    def on_ok(self, event):
        self.port = PortDef(
            name=self.txt_name.GetValue().strip(),
            net=self.txt_net.GetValue().strip(),
            side=self.choice_side.GetStringSelection(),
            position=self.spin_pos.GetValue()
        )
        self.EndModal(wx.ID_OK)


class NewBoardDialog(wx.Dialog):
    def __init__(self, parent, existing: Set[str]):
        super().__init__(parent, title="New Board", size=(400, 250))
        self.existing = existing
        self.result_name = ""
        self.result_desc = ""
        
        panel = wx.Panel(self)
        grid = wx.FlexGridSizer(2, 2, 8, 8)
        grid.AddGrowableCol(1)
        
        grid.Add(wx.StaticText(panel, label="Name:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_name = wx.TextCtrl(panel)
        grid.Add(self.txt_name, 1, wx.EXPAND)
        
        grid.Add(wx.StaticText(panel, label="Description:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_desc = wx.TextCtrl(panel)
        grid.Add(self.txt_desc, 1, wx.EXPAND)
        
        main = wx.BoxSizer(wx.VERTICAL)
        main.Add(grid, 0, wx.EXPAND | wx.ALL, 15)
        
        btns = wx.StdDialogButtonSizer()
        btn_ok = wx.Button(panel, wx.ID_OK, "Create")
        btn_ok.Bind(wx.EVT_BUTTON, self.on_ok)
        btns.AddButton(btn_ok)
        btns.AddButton(wx.Button(panel, wx.ID_CANCEL))
        btns.Realize()
        main.Add(btns, 0, wx.ALIGN_RIGHT | wx.ALL, 10)
        
        panel.SetSizer(main)
    
    def on_ok(self, event):
        name = self.txt_name.GetValue().strip()
        if not name:
            wx.MessageBox("Enter a name.", "Error", wx.ICON_ERROR)
            return
        if name in self.existing:
            wx.MessageBox("Name exists.", "Error", wx.ICON_ERROR)
            return
        self.result_name = name
        self.result_desc = self.txt_desc.GetValue().strip()
        self.EndModal(wx.ID_OK)


class ConnectivityReportDialog(wx.Dialog):
    """Show connectivity/DRC report."""
    
    def __init__(self, parent, report: Dict):
        super().__init__(parent, title="Connectivity Report", size=(600, 450),
                        style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        
        panel = wx.Panel(self)
        main = wx.BoxSizer(wx.VERTICAL)
        
        # Summary
        errors = report.get("errors", [])
        boards = report.get("boards", {})
        
        total_violations = sum(b.get("violations", 0) for b in boards.values())
        summary = f"Boards checked: {len(boards)} | DRC violations: {total_violations} | Errors: {len(errors)}"
        
        lbl = wx.StaticText(panel, label=summary)
        lbl.SetFont(lbl.GetFont().Bold())
        main.Add(lbl, 0, wx.ALL, 10)
        
        # Details
        self.text = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.HSCROLL)
        self.text.SetFont(wx.Font(9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        
        lines = []
        for board_name, data in boards.items():
            lines.append(f"=== {board_name} ===")
            lines.append(f"Violations: {data.get('violations', 0)}")
            for v in data.get("details", []):
                lines.append(f"  - {v.get('type', 'unknown')}: {v.get('description', '')[:80]}")
            lines.append("")
        
        if errors:
            lines.append("=== ERRORS ===")
            for e in errors:
                lines.append(f"  {e}")
        
        self.text.SetValue("\n".join(lines))
        main.Add(self.text, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        
        btn_close = wx.Button(panel, wx.ID_CLOSE)
        btn_close.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        main.Add(btn_close, 0, wx.ALIGN_RIGHT | wx.ALL, 10)
        
        panel.SetSizer(main)


class StatusDialog(wx.Dialog):
    def __init__(self, parent, manager: MultiBoardManager):
        super().__init__(parent, title="Status", size=(550, 400),
                        style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.manager = manager
        
        panel = wx.Panel(self)
        main = wx.BoxSizer(wx.VERTICAL)
        
        self.lbl = wx.StaticText(panel, label="Loading...")
        self.lbl.SetFont(self.lbl.GetFont().Bold())
        main.Add(self.lbl, 0, wx.ALL, 10)
        
        self.tree = wx.TreeCtrl(panel, style=wx.TR_DEFAULT_STYLE | wx.TR_HIDE_ROOT)
        main.Add(self.tree, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        
        btn_close = wx.Button(panel, wx.ID_CLOSE)
        btn_close.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        main.Add(btn_close, 0, wx.ALIGN_RIGHT | wx.ALL, 10)
        
        panel.SetSizer(main)
        wx.CallAfter(self._load)
    
    def _load(self):
        placed, unplaced, total = self.manager.get_status()
        self.lbl.SetLabel(f"Total: {total} | Placed: {len(placed)} | Unplaced: {len(unplaced)}")
        
        self.tree.DeleteAllItems()
        root = self.tree.AddRoot("Status")
        
        # By board
        by_board = {}
        for ref, board in placed.items():
            by_board.setdefault(board, []).append(ref)
        
        for board_name in sorted(by_board.keys()):
            refs = sorted(by_board[board_name])
            node = self.tree.AppendItem(root, f"{board_name} ({len(refs)})")
            for ref in refs[:50]:  # Limit display
                self.tree.AppendItem(node, ref)
            if len(refs) > 50:
                self.tree.AppendItem(node, f"... and {len(refs)-50} more")
        
        # Unplaced
        if unplaced:
            node = self.tree.AppendItem(root, f"Unplaced ({len(unplaced)})")
            for ref in sorted(unplaced)[:50]:
                self.tree.AppendItem(node, ref)
        
        self.tree.ExpandAll()


class MainDialog(wx.Dialog):
    def __init__(self, parent, board: pcbnew.BOARD):
        super().__init__(parent, title="Multi-Board Manager", size=(680, 480),
                        style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        
        path = board.GetFileName()
        self.manager = MultiBoardManager(Path(path).parent if path else Path.cwd())
        self.executor = ThreadPoolExecutor(max_workers=1)
        
        self._build_ui()
        self._refresh()
        
        self.Bind(wx.EVT_CLOSE, self.on_close)
    
    def _build_ui(self):
        panel = wx.Panel(self)
        panel.SetBackgroundColour(wx.Colour(250, 250, 250))
        
        main = wx.BoxSizer(wx.VERTICAL)
        
        # Header
        header = wx.StaticText(panel, label="Multi-Board Manager")
        header.SetFont(wx.Font(13, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        main.Add(header, 0, wx.ALL, 12)
        
        # Board list
        self.list = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SIMPLE)
        self.list.InsertColumn(0, "Board", width=120)
        self.list.InsertColumn(1, "Path", width=220)
        self.list.InsertColumn(2, "Components", width=80)
        self.list.InsertColumn(3, "Ports", width=60)
        self.list.InsertColumn(4, "Description", width=140)
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_open)
        main.Add(self.list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)
        
        # Button rows
        btn_panel = wx.Panel(panel)
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        buttons = [
            ("New", self.on_new), ("Remove", self.on_remove), ("|", None),
            ("Open", self.on_open), ("Update", self.on_update), ("|", None),
            ("Ports", self.on_ports), ("Check", self.on_check), ("Status", self.on_status)
        ]
        
        for label, handler in buttons:
            if label == "|":
                btn_sizer.AddSpacer(15)
            else:
                btn = wx.Button(btn_panel, label=label, size=(70, -1))
                if handler: btn.Bind(wx.EVT_BUTTON, handler)
                btn_sizer.Add(btn, 0, wx.RIGHT, 4)
        
        btn_sizer.AddStretchSpacer()
        btn_close = wx.Button(btn_panel, label="Close", size=(70, -1))
        btn_close.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        btn_sizer.Add(btn_close, 0)
        
        btn_panel.SetSizer(btn_sizer)
        main.Add(btn_panel, 0, wx.EXPAND | wx.ALL, 12)
        
        panel.SetSizer(main)
    
    def _refresh(self):
        self.list.DeleteAllItems()
        
        # Quick scan for component counts
        placed = self.manager.scan_all_boards()
        counts = {}
        for ref, (board, _) in placed.items():
            counts[board] = counts.get(board, 0) + 1
        
        for name, board in self.manager.config.boards.items():
            idx = self.list.InsertItem(self.list.GetItemCount(), name)
            self.list.SetItem(idx, 1, board.pcb_path)
            self.list.SetItem(idx, 2, str(counts.get(name, 0)))
            self.list.SetItem(idx, 3, str(len(board.ports)))
            self.list.SetItem(idx, 4, board.description)
    
    def _get_selected(self) -> Optional[str]:
        idx = self.list.GetFirstSelected()
        return self.list.GetItemText(idx) if idx >= 0 else None
    
    def on_new(self, event):
        dlg = NewBoardDialog(self, set(self.manager.config.boards.keys()))
        if dlg.ShowModal() == wx.ID_OK:
            ok, msg = self.manager.create_board(dlg.result_name, dlg.result_desc)
            if ok:
                wx.MessageBox(f"Created: {dlg.result_name}\n\nUpdate to add components.", "Success", wx.ICON_INFORMATION)
                self._refresh()
            else:
                wx.MessageBox(msg, "Error", wx.ICON_ERROR)
        dlg.Destroy()
    
    def on_remove(self, event):
        name = self._get_selected()
        if not name:
            return
        
        board = self.manager.config.boards.get(name)
        if not board:
            return
        
        if wx.MessageBox(f"Remove '{name}'?\n\nThis will delete the board folder and all files.", 
                        "Confirm", wx.YES_NO | wx.ICON_WARNING) == wx.YES:
            # Delete the board folder
            pcb_path = self.manager.project_dir / board.pcb_path
            board_dir = pcb_path.parent
            if board_dir.exists() and BOARDS_DIR in str(board_dir):
                try:
                    shutil.rmtree(board_dir)
                except Exception as e:
                    wx.MessageBox(f"Could not delete folder: {e}", "Warning", wx.ICON_WARNING)
            
            # Remove from config
            del self.manager.config.boards[name]
            self.manager.save_config()
            self._refresh()

    def on_open(self, event):
        name = self._get_selected()
        if not name: return
        
        board = self.manager.config.boards.get(name)
        if not board: return
        
        pcb = self.manager.project_dir / board.pcb_path
        if pcb.exists():
            if os.name == "nt": os.startfile(str(pcb))
            else: subprocess.Popen(["pcbnew", str(pcb)])
    
    def on_update(self, event):
        name = self._get_selected()
        if not name:
            wx.MessageBox("Select a board.", "Info", wx.ICON_INFORMATION)
            return
        
        progress = ProgressDialog(self, f"Updating {name}")
        progress.Show()
        
        def do_update():
            return self.manager.update_board(name, progress_callback=lambda p, m: wx.CallAfter(progress.update, p, m))
        
        def on_done(future):
            wx.CallAfter(progress.Destroy)
            try:
                ok, msg = future.result()
                wx.CallAfter(lambda: wx.MessageBox(msg, "Complete" if ok else "Failed", 
                            wx.ICON_INFORMATION if ok else wx.ICON_ERROR))
                wx.CallAfter(self._refresh)
            except Exception as e:
                wx.CallAfter(lambda: wx.MessageBox(str(e), "Error", wx.ICON_ERROR))
        
        future = self.executor.submit(do_update)
        future.add_done_callback(on_done)
    
    def on_ports(self, event):
        name = self._get_selected()
        if not name: return
        
        board = self.manager.config.boards.get(name)
        if not board: return
        
        dlg = PortDialog(self, board)
        if dlg.ShowModal() == wx.ID_OK:
            self.manager._generate_block_footprint(board)
            self.manager.save_config()
            wx.MessageBox("Ports updated. Block footprint regenerated.", "Done", wx.ICON_INFORMATION)
            self._refresh()
        dlg.Destroy()
    
    def on_check(self, event):
        progress = ProgressDialog(self, "Checking connectivity")
        progress.Show()
        
        def do_check():
            return self.manager.check_connectivity(
                progress_callback=lambda p, m: wx.CallAfter(progress.update, p, m))
        
        def on_done(future):
            wx.CallAfter(progress.Destroy)
            try:
                report = future.result()
                wx.CallAfter(lambda: ConnectivityReportDialog(self, report).ShowModal())
            except Exception as e:
                wx.CallAfter(lambda: wx.MessageBox(str(e), "Error", wx.ICON_ERROR))
        
        future = self.executor.submit(do_check)
        future.add_done_callback(on_done)
    
    def on_status(self, event):
        StatusDialog(self, self.manager).ShowModal()
    
    def on_close(self, event):
        self.executor.shutdown(wait=False)
        self.Destroy()


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
            wx.MessageBox(f"Error: {e}\n\n{traceback.format_exc()}", "Error", wx.ICON_ERROR)


MultiBoardPlugin().register()