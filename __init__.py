"""
Multi-Board PCB Manager v6 - KiCad Action Plugin
=================================================
Simple multi-PCB workflow: one schematic, multiple board layouts.

Uses kicad-cli for reliable netlist import (proper footprint loading).
Respects DNP and exclude_from_board attributes.

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

# ============================================================================
# Constants
# ============================================================================

BOARDS_DIR = "boards"
CONFIG_FILE = ".kicad_multiboard.json"

# ============================================================================
# Data Model - Simplified
# ============================================================================

@dataclass
class BoardConfig:
    """Configuration for a sub-board."""
    name: str
    pcb_path: str  # Relative path like "boards/MainBoard/MainBoard.kicad_pcb"
    description: str = ""
    
    def to_dict(self) -> dict:
        return {"name": self.name, "pcb_path": self.pcb_path, "description": self.description}
    
    @classmethod
    def from_dict(cls, d: dict) -> "BoardConfig":
        return cls(name=d["name"], pcb_path=d["pcb_path"], description=d.get("description", ""))


@dataclass  
class ProjectConfig:
    """Multi-board project configuration."""
    version: str = "6.0"
    root_schematic: str = ""
    root_pcb: str = ""
    boards: Dict[str, BoardConfig] = field(default_factory=dict)
    # Component assignment: ref -> board_name (empty = unassigned, goes to all boards)
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
            version=d.get("version", "6.0"),
            root_schematic=d.get("root_schematic", ""),
            root_pcb=d.get("root_pcb", ""),
            assignments=d.get("assignments", d.get("component_placement", {})),
        )
        for name, bd in d.get("boards", {}).items():
            if isinstance(bd, dict):
                # Handle both old and new format
                pcb_path = bd.get("pcb_path", bd.get("pcb_filename", ""))
                cfg.boards[name] = BoardConfig(
                    name=name,
                    pcb_path=pcb_path,
                    description=bd.get("description", "")
                )
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
        
        # Logging
        self.log_path = self.project_dir / "multiboard_debug.log"
        self.fault_path = self.project_dir / "multiboard_fault.log"
        self._init_logging()
        
        self._detect_root_files()
        self._load_config()
        
        self._log(f"Init: project_dir={self.project_dir}")
    
    def _find_project_root(self, start: Path) -> Path:
        """Walk up to find .kicad_multiboard.json or .kicad_pro"""
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
                self._detect_root_files()  # Refresh root files
            except Exception as e:
                self._log(f"Config load error: {e}")
    
    def save_config(self):
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.config.to_dict(), f, indent=2)
        except Exception as e:
            self._log(f"Config save error: {e}")
    
    # -------------------------------------------------------------------------
    # KiCad CLI
    # -------------------------------------------------------------------------
    
    def _find_kicad_cli(self) -> Optional[str]:
        """Find kicad-cli executable."""
        # Check PATH first
        exe = shutil.which("kicad-cli")
        if exe:
            return exe
        
        if os.name != "nt":
            return None
        
        # Windows: check common locations
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
            # Find version folders (9.0, 8.0, etc.)
            for ver_dir in sorted(base.iterdir(), reverse=True):
                cli = ver_dir / "bin" / "kicad-cli.exe"
                if cli.exists():
                    return str(cli)
        
        return None
    
    def _run_cli(self, args: List[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
        """Run kicad-cli with given arguments."""
        cli = self._find_kicad_cli()
        if not cli:
            raise FileNotFoundError("kicad-cli not found")
        
        cmd = [cli] + args
        self._log(f"CLI: {' '.join(cmd)}")
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(cwd or self.project_dir)
        )
        
        if result.returncode != 0:
            self._log(f"CLI stderr: {result.stderr}")
        
        return result
    
    # -------------------------------------------------------------------------
    # Board Management
    # -------------------------------------------------------------------------
    
    def create_board(self, name: str, description: str = "") -> Tuple[bool, str]:
        """Create a new sub-board."""
        if name in self.config.boards:
            return False, f"Board '{name}' already exists"
        
        # Sanitize name for filesystem
        safe_name = "".join(c if c.isalnum() or c in "_-" else "_" for c in name)
        
        board_dir = self.project_dir / BOARDS_DIR / safe_name
        pcb_path = board_dir / f"{safe_name}.kicad_pcb"
        rel_path = f"{BOARDS_DIR}/{safe_name}/{safe_name}.kicad_pcb"
        
        if pcb_path.exists():
            return False, f"PCB file already exists: {rel_path}"
        
        # Create directory
        board_dir.mkdir(parents=True, exist_ok=True)
        
        # Create empty PCB
        if not self._create_empty_pcb(pcb_path):
            return False, "Failed to create PCB file"
        
        # Add to config
        self.config.boards[name] = BoardConfig(
            name=name,
            pcb_path=rel_path,
            description=description
        )
        self.save_config()
        
        self._log(f"Created board: {name} at {rel_path}")
        return True, f"Created {rel_path}"
    
    def _create_empty_pcb(self, path: Path) -> bool:
        """Create a minimal empty PCB file."""
        content = '''(kicad_pcb
  (version 20240108)
  (generator "multiboard")
  (generator_version "9.0")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
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
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
)
'''
        try:
            path.write_text(content, encoding="utf-8")
            return True
        except Exception as e:
            self._log(f"Failed to create PCB: {e}")
            return False
    
    def remove_board(self, name: str) -> bool:
        """Remove board from config (doesn't delete files)."""
        if name in self.config.boards:
            del self.config.boards[name]
            # Clean up assignments
            self.config.assignments = {
                ref: b for ref, b in self.config.assignments.items() if b != name
            }
            self.save_config()
            return True
        return False
    
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
            
            if result.returncode == 0 and netlist_path.exists():
                return netlist_path
            
            self._log(f"Netlist export failed: {result.stderr}")
            return None
            
        except Exception as e:
            self._log(f"Netlist export error: {e}")
            return None
    
    def _parse_netlist(self, netlist_path: Path) -> Tuple[Dict, ET.Element]:
        """
        Parse netlist and extract component info.
        Returns: (components dict, root element)
        
        components = {ref: {"footprint": ..., "value": ..., "tstamp": ..., "dnp": bool, "exclude": bool}}
        """
        components = {}
        
        tree = ET.parse(netlist_path)
        root = tree.getroot()
        
        for comp in root.findall(".//components/comp"):
            ref = comp.get("ref", "")
            if not ref or ref.startswith("#"):
                continue
            
            footprint = (comp.findtext("footprint") or "").strip()
            value = (comp.findtext("value") or "").strip()
            tstamp = (comp.findtext("tstamp") or "").strip()
            
            # Check for DNP and exclude_from_board in multiple locations
            dnp = False
            exclude_from_board = False
            
            # 1. Check direct attributes on comp element
            if comp.get("dnp", "").lower() in ("yes", "true", "1", "dnp"):
                dnp = True
            if comp.get("exclude_from_board", "").lower() in ("yes", "true", "1"):
                exclude_from_board = True
            
            # 2. Check property elements (KiCad 8+ style)
            for prop in comp.findall("property"):
                prop_name = (prop.get("name") or "").lower().strip()
                prop_value = (prop.get("value") or "").lower().strip()
                
                if prop_name == "dnp" and prop_value in ("yes", "true", "1", "dnp"):
                    dnp = True
                elif prop_name in ("exclude_from_board", "exclude from board", "exclude_from_bom"):
                    if prop_value in ("yes", "true", "1"):
                        exclude_from_board = True
            
            # 3. Check fields element (older KiCad style)
            fields = comp.find("fields")
            if fields is not None:
                for field in fields.findall("field"):
                    field_name = (field.get("name") or "").lower().strip()
                    field_value = (field.text or "").lower().strip()
                    
                    if field_name == "dnp" and field_value in ("yes", "true", "1", "dnp"):
                        dnp = True
                    elif field_name in ("exclude_from_board", "exclude from board"):
                        if field_value in ("yes", "true", "1"):
                            exclude_from_board = True
            
            # 4. Check libsource attributes
            libsource = comp.find("libsource")
            if libsource is not None:
                if libsource.get("dnp", "").lower() in ("yes", "true", "1"):
                    dnp = True
            
            # 5. Check sheetpath for instance-level attributes  
            sheetpath = comp.find("sheetpath")
            if sheetpath is not None:
                if sheetpath.get("dnp", "").lower() in ("yes", "true", "1"):
                    dnp = True
            
            # 6. Check for "DNP" in value field (common convention)
            if value.upper() == "DNP" or value.upper().startswith("DNP "):
                dnp = True
            
            # 7. Check for no footprint assigned (effectively exclude from board)
            if not footprint or footprint.lower() in ("", "none", "virtual"):
                exclude_from_board = True
            
            components[ref] = {
                "footprint": footprint,
                "value": value,
                "tstamp": tstamp,
                "dnp": dnp,
                "exclude_from_board": exclude_from_board,
            }
        
        return components, root
    
    def _filter_netlist(
        self,
        netlist_path: Path,
        target_board: str,
        components: Dict,
        root: ET.Element
    ) -> Path:
        """
        Create filtered netlist for target board.
        
        Excludes:
        - Components with DNP = yes
        - Components with exclude_from_board = yes  
        - Components assigned to OTHER boards
        """
        filtered_path = netlist_path.with_suffix(".filtered.xml")
        
        # Find components to exclude
        exclude_refs = set()
        
        for ref, info in components.items():
            # Skip DNP components
            if info["dnp"]:
                exclude_refs.add(ref)
                self._log(f"Excluding {ref}: DNP")
                continue
            
            # Skip exclude_from_board components
            if info["exclude_from_board"]:
                exclude_refs.add(ref)
                self._log(f"Excluding {ref}: exclude_from_board")
                continue
            
            # Skip components assigned to other boards
            assigned = self.config.assignments.get(ref, "")
            if assigned and assigned != target_board:
                exclude_refs.add(ref)
                continue
        
        # Remove excluded components from XML
        components_elem = root.find("components")
        if components_elem is not None:
            to_remove = []
            for comp in components_elem.findall("comp"):
                if comp.get("ref") in exclude_refs:
                    to_remove.append(comp)
            for comp in to_remove:
                components_elem.remove(comp)
        
        # Also clean up nets that reference excluded components
        nets_elem = root.find("nets")
        if nets_elem is not None:
            for net in nets_elem.findall("net"):
                nodes_to_remove = []
                for node in net.findall("node"):
                    if node.get("ref") in exclude_refs:
                        nodes_to_remove.append(node)
                for node in nodes_to_remove:
                    net.remove(node)
        
        # Write filtered netlist
        tree = ET.ElementTree(root)
        tree.write(filtered_path, encoding="utf-8", xml_declaration=True)
        
        self._log(f"Filtered netlist: excluded {len(exclude_refs)} components")
        return filtered_path
    
    # -------------------------------------------------------------------------
    # Update PCB from Schematic
    # -------------------------------------------------------------------------
    
    def update_board(self, board_name: str) -> Tuple[bool, str]:
        """
        Update a sub-board from root schematic.
        
        Tries kicad-cli first (proper library access), falls back to pcbnew API.
        """
        board = self.config.boards.get(board_name)
        if not board:
            return False, f"Board '{board_name}' not found"
        
        pcb_path = self.project_dir / board.pcb_path
        if not pcb_path.exists():
            return False, f"PCB file not found: {board.pcb_path}"
        
        self._log(f"Updating board: {board_name}")
        
        # 1. Export netlist from root schematic
        netlist_path = self._export_netlist()
        if not netlist_path:
            return False, "Failed to export netlist from root schematic"
        
        try:
            # 2. Parse and filter netlist
            components, root = self._parse_netlist(netlist_path)
            filtered_path = self._filter_netlist(netlist_path, board_name, components, root)
            
            # 3. Try kicad-cli first (has proper library access)
            cli_success = False
            cli_error = ""
            
            try:
                # Try different CLI command variants
                for cli_args in [
                    ["pcb", "import", "netlist", "--netlist", str(filtered_path), str(pcb_path)],
                    ["pcb", "import-netlist", str(filtered_path), str(pcb_path)],
                ]:
                    result = self._run_cli(cli_args, cwd=self.project_dir)
                    if result.returncode == 0:
                        cli_success = True
                        break
                    cli_error = result.stderr
            except FileNotFoundError:
                cli_error = "kicad-cli not found"
            except Exception as e:
                cli_error = str(e)
            
            if not cli_success:
                # 4. Fallback: use pcbnew API with proper library table
                self._log(f"CLI failed ({cli_error}), using pcbnew API fallback")
                return self._update_board_via_api(board_name, pcb_path, filtered_path, components)
            
            # 5. Update assignments based on what's now on this board
            self._scan_board_components(board_name, pcb_path)
            
            # 6. Cleanup temp files
            self._cleanup_temp_files([netlist_path, filtered_path])
            
            self._log(f"Update successful for {board_name} via CLI")
            return True, f"Updated {board.pcb_path}\n\nUse File -> Revert to reload in editor."
                
        except Exception as e:
            self._log(f"Update error: {e}")
            import traceback
            self._log(traceback.format_exc())
            return False, f"Update failed: {e}"
    
    def _cleanup_temp_files(self, paths: List[Path]):
        """Clean up temporary files."""
        for p in paths:
            try:
                if p and p.exists():
                    p.unlink()
            except Exception:
                pass
    
    def _update_board_via_api(
        self, 
        board_name: str, 
        pcb_path: Path, 
        netlist_path: Path,
        components: Dict
    ) -> Tuple[bool, str]:
        """
        Fallback: Update board using pcbnew API with proper library table access.
        """
        self._log("Using pcbnew API for update")
        
        try:
            # Load the board
            board_obj = pcbnew.LoadBoard(str(pcb_path))
            if not board_obj:
                return False, "Failed to load PCB"
            
            # Get footprint library table (global + project)
            fp_table = None
            try:
                # Try to get combined table (global + project)
                fp_table = pcbnew.FOOTPRINT_LIB_TABLE.GetGlobalLibTable()
            except Exception:
                pass
            
            if fp_table is None:
                try:
                    # Alternative: get from project
                    fp_table = board_obj.GetProject().PcbFootprintLibs()
                except Exception:
                    pass
            
            # Parse filtered netlist
            tree = ET.parse(netlist_path)
            root = tree.getroot()
            
            # Get existing footprints
            existing = {}
            for fp in board_obj.GetFootprints():
                ref = fp.GetReference()
                if ref:
                    existing[ref] = fp
            
            added = 0
            updated = 0
            failed = 0
            failed_list = []
            
            # Process components from netlist
            for comp in root.findall(".//components/comp"):
                ref = comp.get("ref", "")
                if not ref or ref.startswith("#"):
                    continue
                
                fp_id = (comp.findtext("footprint") or "").strip()
                value = (comp.findtext("value") or "").strip()
                tstamp = (comp.findtext("tstamp") or "").strip()
                
                if not fp_id:
                    continue
                
                # Parse library:footprint
                if ":" in fp_id:
                    lib_nick, fp_name = fp_id.split(":", 1)
                else:
                    lib_nick, fp_name = "", fp_id
                
                if ref in existing:
                    # Update existing footprint
                    fp = existing[ref]
                    if value:
                        try:
                            fp.SetValue(value)
                        except Exception:
                            pass
                    # Update path for cross-probing
                    if tstamp:
                        self._set_footprint_path(fp, tstamp)
                    updated += 1
                else:
                    # Load and add new footprint
                    fp = self._load_footprint_with_table(fp_table, lib_nick, fp_name)
                    
                    if fp is None:
                        failed += 1
                        failed_list.append(f"{ref}: {fp_id}")
                        self._log(f"Failed to load: {ref} ({fp_id})")
                        continue
                    
                    # Set properties
                    try:
                        fp.SetReference(ref)
                    except Exception:
                        pass
                    try:
                        fp.SetValue(value)
                    except Exception:
                        pass
                    try:
                        fp.SetPosition(pcbnew.VECTOR2I(0, 0))
                    except Exception:
                        pass
                    
                    if tstamp:
                        self._set_footprint_path(fp, tstamp)
                    
                    try:
                        board_obj.Add(fp)
                        existing[ref] = fp
                        added += 1
                    except Exception as e:
                        failed += 1
                        self._log(f"Failed to add {ref}: {e}")
            
            # Assign nets
            self._assign_nets_from_netlist(board_obj, root, existing)
            
            # Save
            try:
                pcbnew.SaveBoard(str(pcb_path), board_obj)
            except Exception as e:
                return False, f"Failed to save: {e}"
            
            # Update assignments
            self._scan_board_components(board_name, pcb_path)
            
            # Cleanup
            self._cleanup_temp_files([netlist_path])
            
            msg = f"Updated {board_name}:\n• Added: {added}\n• Updated: {updated}\n• Failed: {failed}"
            if failed > 0 and failed_list:
                msg += f"\n\nFailed components (first 10):\n" + "\n".join(failed_list[:10])
            msg += "\n\nUse File -> Revert to reload."
            
            self._log(f"API update: added={added} updated={updated} failed={failed}")
            return True, msg
            
        except Exception as e:
            self._log(f"API update error: {e}")
            import traceback
            self._log(traceback.format_exc())
            return False, f"Update failed: {e}"
    
    def _load_footprint_with_table(
        self, 
        fp_table, 
        lib_nick: str, 
        fp_name: str
    ) -> Optional["pcbnew.FOOTPRINT"]:
        """Load footprint using library table."""
        
        # Method 1: Use FootprintLoad with library nickname
        try:
            fp = pcbnew.FootprintLoad(lib_nick, fp_name)
            if fp:
                return fp
        except Exception:
            pass
        
        # Method 2: Use library table if available
        if fp_table:
            try:
                # FindRow gets the library entry
                row = fp_table.FindRow(lib_nick)
                if row:
                    uri = row.GetFullURI(True)  # Expand env vars
                    fp = pcbnew.FootprintLoad(uri, fp_name)
                    if fp:
                        return fp
            except Exception:
                pass
        
        # Method 3: Try common KiCad library paths
        kicad_paths = self._get_kicad_footprint_paths()
        
        for base_path in kicad_paths:
            lib_path = base_path / f"{lib_nick}.pretty"
            if lib_path.exists():
                try:
                    fp = pcbnew.FootprintLoad(str(lib_path), fp_name)
                    if fp:
                        return fp
                except Exception:
                    pass
        
        # Method 4: Check project-local libraries
        local_lib = self.project_dir / f"{lib_nick}.pretty"
        if local_lib.exists():
            try:
                fp = pcbnew.FootprintLoad(str(local_lib), fp_name)
                if fp:
                    return fp
            except Exception:
                pass
        
        return None
    
    def _get_kicad_footprint_paths(self) -> List[Path]:
        """Get common KiCad footprint library paths."""
        paths = []
        
        # Environment variables
        for env_var in [
            "KICAD9_FOOTPRINT_DIR",
            "KICAD8_FOOTPRINT_DIR", 
            "KICAD7_FOOTPRINT_DIR",
            "KICAD_FOOTPRINT_DIR",
            "KICAD9_3RDPARTY_DIR",
        ]:
            val = os.environ.get(env_var)
            if val:
                paths.append(Path(val))
        
        # Common Windows paths
        if os.name == "nt":
            for base in [
                Path(os.environ.get("ProgramFiles", "C:/Program Files")),
                Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")),
            ]:
                for ver in ["9.0", "8.0", "7.0"]:
                    fp_dir = base / "KiCad" / ver / "share" / "kicad" / "footprints"
                    if fp_dir.exists():
                        paths.append(fp_dir)
        
        # Linux/Mac paths
        else:
            for fp_dir in [
                Path("/usr/share/kicad/footprints"),
                Path("/usr/local/share/kicad/footprints"),
                Path.home() / ".local/share/kicad/footprints",
            ]:
                if fp_dir.exists():
                    paths.append(fp_dir)
        
        return paths
    
    def _set_footprint_path(self, fp: "pcbnew.FOOTPRINT", tstamp: str):
        """Set footprint path for cross-probing."""
        try:
            path_str = f"/{tstamp}"
            if hasattr(pcbnew, "KIID_PATH"):
                fp.SetPath(pcbnew.KIID_PATH(path_str))
            else:
                fp.SetPath(path_str)
        except Exception:
            pass
    
    def _assign_nets_from_netlist(
        self, 
        board_obj: "pcbnew.BOARD", 
        netlist_root: ET.Element,
        footprints: Dict[str, "pcbnew.FOOTPRINT"]
    ):
        """Assign nets to pads based on netlist."""
        
        # Build net name -> NETINFO_ITEM map
        net_map = {}
        try:
            for name, net in board_obj.GetNetsByName().items():
                net_map[name] = net
        except Exception:
            pass
        
        def ensure_net(name: str):
            if name in net_map:
                return net_map[name]
            try:
                ni = pcbnew.NETINFO_ITEM(board_obj, name)
                board_obj.Add(ni)
                net_map[name] = ni
                return ni
            except Exception:
                return None
        
        # Parse nets from netlist
        for net_elem in netlist_root.findall(".//nets/net"):
            net_name = net_elem.get("name", "")
            if not net_name:
                continue
            
            ni = ensure_net(net_name)
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
    
    def _scan_board_components(self, board_name: str, pcb_path: Path):
        """Scan PCB and update component assignments."""
        try:
            board = pcbnew.LoadBoard(str(pcb_path))
            for fp in board.GetFootprints():
                ref = fp.GetReference()
                if ref and not ref.startswith("#"):
                    # Assign to this board
                    self.config.assignments[ref] = board_name
            self.save_config()
        except Exception as e:
            self._log(f"Scan error: {e}")
    
    # -------------------------------------------------------------------------
    # Component Status
    # -------------------------------------------------------------------------
    
    def get_all_schematic_components(self) -> Set[str]:
        """Get all component refs from schematic."""
        netlist = self._export_netlist()
        if not netlist:
            return set()
        
        try:
            components, _ = self._parse_netlist(netlist)
            # Only return components that are NOT excluded
            result = {
                ref for ref, info in components.items()
                if not info["dnp"] and not info["exclude_from_board"]
            }
            netlist.unlink()
            return result
        except Exception as e:
            self._log(f"Error getting schematic components: {e}")
            return set()
    
    def get_component_status(self) -> Tuple[Dict[str, str], Set[str]]:
        """
        Get component placement status.
        
        Returns: (placed: ref->board, unplaced: set of refs)
        """
        # Scan all boards
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
        
        # Update config
        self.config.assignments = placed
        self.save_config()
        
        # Get unplaced
        all_refs = self.get_all_schematic_components()
        unplaced = all_refs - set(placed.keys())
        
        return placed, unplaced


# ============================================================================
# UI Dialogs
# ============================================================================

class NewBoardDialog(wx.Dialog):
    """Simple dialog for creating a new board."""
    
    def __init__(self, parent, existing_names: Set[str]):
        super().__init__(parent, title="New Sub-Board", size=(400, 200))
        self.existing = existing_names
        self.result_name = ""
        self.result_desc = ""
        
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        
        # Name
        sizer.Add(wx.StaticText(panel, label="Board Name:"), 0, wx.ALL, 5)
        self.txt_name = wx.TextCtrl(panel)
        sizer.Add(self.txt_name, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        
        # Description
        sizer.Add(wx.StaticText(panel, label="Description (optional):"), 0, wx.ALL, 5)
        self.txt_desc = wx.TextCtrl(panel)
        sizer.Add(self.txt_desc, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        
        # Buttons
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_ok = wx.Button(panel, wx.ID_OK, "Create")
        btn_cancel = wx.Button(panel, wx.ID_CANCEL, "Cancel")
        btn_ok.Bind(wx.EVT_BUTTON, self.on_ok)
        btn_sizer.Add(btn_ok, 0, wx.ALL, 5)
        btn_sizer.Add(btn_cancel, 0, wx.ALL, 5)
        sizer.Add(btn_sizer, 0, wx.ALIGN_CENTER | wx.ALL, 10)
        
        panel.SetSizer(sizer)
    
    def on_ok(self, event):
        name = self.txt_name.GetValue().strip()
        if not name:
            wx.MessageBox("Please enter a board name.", "Error", wx.OK | wx.ICON_ERROR)
            return
        if name in self.existing:
            wx.MessageBox("Board name already exists.", "Error", wx.OK | wx.ICON_ERROR)
            return
        
        self.result_name = name
        self.result_desc = self.txt_desc.GetValue().strip()
        self.EndModal(wx.ID_OK)


class StatusDialog(wx.Dialog):
    """Component status dialog."""
    
    def __init__(self, parent, manager: MultiBoardManager):
        super().__init__(parent, title="Component Status", size=(600, 500))
        self.manager = manager
        
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        
        # Summary
        self.lbl_summary = wx.StaticText(panel, label="Scanning...")
        sizer.Add(self.lbl_summary, 0, wx.ALL, 10)
        
        # Lists
        nb = wx.Notebook(panel)
        
        # Placed tab
        placed_panel = wx.Panel(nb)
        ps = wx.BoxSizer(wx.VERTICAL)
        self.list_placed = wx.ListCtrl(placed_panel, style=wx.LC_REPORT)
        self.list_placed.InsertColumn(0, "Reference", width=100)
        self.list_placed.InsertColumn(1, "Board", width=200)
        ps.Add(self.list_placed, 1, wx.EXPAND | wx.ALL, 5)
        placed_panel.SetSizer(ps)
        nb.AddPage(placed_panel, "Placed")
        
        # Unplaced tab
        unplaced_panel = wx.Panel(nb)
        us = wx.BoxSizer(wx.VERTICAL)
        self.list_unplaced = wx.ListCtrl(unplaced_panel, style=wx.LC_REPORT)
        self.list_unplaced.InsertColumn(0, "Reference", width=150)
        us.Add(self.list_unplaced, 1, wx.EXPAND | wx.ALL, 5)
        unplaced_panel.SetSizer(us)
        nb.AddPage(unplaced_panel, "Unplaced")
        
        sizer.Add(nb, 1, wx.EXPAND | wx.ALL, 10)
        
        # Buttons
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_refresh = wx.Button(panel, label="Refresh")
        btn_refresh.Bind(wx.EVT_BUTTON, lambda e: self.refresh())
        btn_close = wx.Button(panel, wx.ID_CLOSE, "Close")
        btn_close.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        btn_sizer.Add(btn_refresh, 0, wx.ALL, 5)
        btn_sizer.AddStretchSpacer()
        btn_sizer.Add(btn_close, 0, wx.ALL, 5)
        sizer.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 10)
        
        panel.SetSizer(sizer)
        self.refresh()
    
    def refresh(self):
        placed, unplaced = self.manager.get_component_status()
        
        self.lbl_summary.SetLabel(
            f"Placed: {len(placed)} | Unplaced: {len(unplaced)} | Boards: {len(self.manager.config.boards)}"
        )
        
        self.list_placed.DeleteAllItems()
        for ref, board in sorted(placed.items()):
            idx = self.list_placed.InsertItem(self.list_placed.GetItemCount(), ref)
            self.list_placed.SetItem(idx, 1, board)
        
        self.list_unplaced.DeleteAllItems()
        for ref in sorted(unplaced):
            self.list_unplaced.InsertItem(self.list_unplaced.GetItemCount(), ref)


class MainDialog(wx.Dialog):
    """Main plugin dialog."""
    
    def __init__(self, parent, board: pcbnew.BOARD):
        super().__init__(
            parent,
            title="Multi-Board PCB Manager v6",
            size=(800, 550),
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
        title = wx.StaticText(panel, label="Multi-Board PCB Manager")
        font = title.GetFont()
        font.SetPointSize(14)
        font.SetWeight(wx.FONTWEIGHT_BOLD)
        title.SetFont(font)
        main.Add(title, 0, wx.ALL, 10)
        
        # Project info
        info = wx.FlexGridSizer(2, 2, 5, 20)
        info.Add(wx.StaticText(panel, label="Root Schematic:"))
        self.lbl_sch = wx.StaticText(panel, label=self.manager.config.root_schematic or "(not found)")
        info.Add(self.lbl_sch)
        info.Add(wx.StaticText(panel, label="Root PCB:"))
        self.lbl_pcb = wx.StaticText(panel, label=self.manager.config.root_pcb or "(not found)")
        info.Add(self.lbl_pcb)
        main.Add(info, 0, wx.LEFT | wx.BOTTOM, 10)
        
        # Board list
        main.Add(wx.StaticText(panel, label="Sub-Boards:"), 0, wx.LEFT | wx.TOP, 10)
        
        self.list = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.list.InsertColumn(0, "Name", width=120)
        self.list.InsertColumn(1, "PCB Path", width=280)
        self.list.InsertColumn(2, "Components", width=80)
        self.list.InsertColumn(3, "Description", width=200)
        main.Add(self.list, 1, wx.EXPAND | wx.ALL, 10)
        
        # Board buttons
        btn_row1 = wx.BoxSizer(wx.HORIZONTAL)
        
        self.btn_new = wx.Button(panel, label="New Board")
        self.btn_remove = wx.Button(panel, label="Remove")
        self.btn_open = wx.Button(panel, label="Open PCB")
        self.btn_update = wx.Button(panel, label="Update from Schematic")
        
        self.btn_new.Bind(wx.EVT_BUTTON, self.on_new)
        self.btn_remove.Bind(wx.EVT_BUTTON, self.on_remove)
        self.btn_open.Bind(wx.EVT_BUTTON, self.on_open)
        self.btn_update.Bind(wx.EVT_BUTTON, self.on_update)
        
        btn_row1.Add(self.btn_new, 0, wx.ALL, 3)
        btn_row1.Add(self.btn_remove, 0, wx.ALL, 3)
        btn_row1.AddSpacer(20)
        btn_row1.Add(self.btn_open, 0, wx.ALL, 3)
        btn_row1.Add(self.btn_update, 0, wx.ALL, 3)
        
        main.Add(btn_row1, 0, wx.LEFT, 7)
        
        # Separator
        main.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.ALL, 10)
        
        # Tools row
        btn_row2 = wx.BoxSizer(wx.HORIZONTAL)
        
        self.btn_status = wx.Button(panel, label="Component Status")
        self.btn_log = wx.Button(panel, label="Debug Log")
        btn_close = wx.Button(panel, label="Close")
        
        self.btn_status.Bind(wx.EVT_BUTTON, self.on_status)
        self.btn_log.Bind(wx.EVT_BUTTON, self.on_log)
        btn_close.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        
        btn_row2.Add(self.btn_status, 0, wx.ALL, 5)
        btn_row2.Add(self.btn_log, 0, wx.ALL, 5)
        btn_row2.AddStretchSpacer()
        btn_row2.Add(btn_close, 0, wx.ALL, 5)
        
        main.Add(btn_row2, 0, wx.EXPAND | wx.ALL, 5)
        
        panel.SetSizer(main)
        
        # Double-click to open
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self.on_open)
    
    def _refresh(self):
        self.list.DeleteAllItems()
        
        # Count components per board
        comp_counts = {}
        for ref, board in self.manager.config.assignments.items():
            comp_counts[board] = comp_counts.get(board, 0) + 1
        
        for name, board in self.manager.config.boards.items():
            idx = self.list.InsertItem(self.list.GetItemCount(), name)
            self.list.SetItem(idx, 1, board.pcb_path)
            self.list.SetItem(idx, 2, str(comp_counts.get(name, 0)))
            self.list.SetItem(idx, 3, board.description)
    
    def _get_selected(self) -> Optional[str]:
        idx = self.list.GetFirstSelected()
        if idx >= 0:
            return self.list.GetItemText(idx)
        return None
    
    def on_new(self, event):
        existing = set(self.manager.config.boards.keys())
        dlg = NewBoardDialog(self, existing)
        if dlg.ShowModal() == wx.ID_OK:
            ok, msg = self.manager.create_board(dlg.result_name, dlg.result_desc)
            if ok:
                wx.MessageBox(
                    f"Created board: {dlg.result_name}\n\n"
                    "Next: Select it and click 'Update from Schematic'",
                    "Success",
                    wx.OK | wx.ICON_INFORMATION
                )
                self._refresh()
            else:
                wx.MessageBox(msg, "Error", wx.OK | wx.ICON_ERROR)
        dlg.Destroy()
    
    def on_remove(self, event):
        name = self._get_selected()
        if not name:
            wx.MessageBox("Select a board first.", "Info", wx.OK | wx.ICON_INFORMATION)
            return
        
        if wx.MessageBox(
            f"Remove '{name}' from project?\n\n(PCB file will not be deleted)",
            "Confirm",
            wx.YES_NO | wx.ICON_QUESTION
        ) == wx.YES:
            self.manager.remove_board(name)
            self._refresh()
    
    def on_open(self, event):
        name = self._get_selected()
        if not name:
            wx.MessageBox("Select a board first.", "Info", wx.OK | wx.ICON_INFORMATION)
            return
        
        board = self.manager.config.boards.get(name)
        if not board:
            return
        
        pcb_path = self.manager.project_dir / board.pcb_path
        if not pcb_path.exists():
            wx.MessageBox(f"PCB not found: {board.pcb_path}", "Error", wx.OK | wx.ICON_ERROR)
            return
        
        try:
            if os.name == "nt":
                os.startfile(str(pcb_path))
            else:
                subprocess.Popen(["pcbnew", str(pcb_path)])
        except Exception as e:
            wx.MessageBox(f"Failed to open: {e}", "Error", wx.OK | wx.ICON_ERROR)
    
    def on_update(self, event):
        name = self._get_selected()
        if not name:
            wx.MessageBox("Select a board first.", "Info", wx.OK | wx.ICON_INFORMATION)
            return
        
        if wx.MessageBox(
            f"Update '{name}' from root schematic?\n\n"
            "Components with DNP or 'exclude from board' will be skipped.\n"
            "Components assigned to other boards will be skipped.",
            "Confirm",
            wx.YES_NO | wx.ICON_QUESTION
        ) != wx.YES:
            return
        
        busy = wx.BusyInfo("Updating from schematic...")
        ok, msg = self.manager.update_board(name)
        del busy
        
        if ok:
            wx.MessageBox(msg, "Update Complete", wx.OK | wx.ICON_INFORMATION)
        else:
            wx.MessageBox(msg, "Update Failed", wx.OK | wx.ICON_ERROR)
        
        self._refresh()
    
    def on_status(self, event):
        dlg = StatusDialog(self, self.manager)
        dlg.ShowModal()
        dlg.Destroy()
        self._refresh()
    
    def on_log(self, event):
        try:
            if os.name == "nt":
                os.startfile(str(self.manager.log_path))
            else:
                subprocess.Popen(["xdg-open", str(self.manager.log_path)])
        except Exception as e:
            wx.MessageBox(f"Failed to open log: {e}", "Error", wx.OK | wx.ICON_ERROR)


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
            wx.MessageBox("Please open a PCB first.", "Error", wx.OK | wx.ICON_ERROR)
            return
        
        dlg = MainDialog(None, board)
        dlg.ShowModal()
        dlg.Destroy()


MultiBoardPlugin().register()