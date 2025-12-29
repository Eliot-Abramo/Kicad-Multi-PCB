"""
Multi-Board PCB Manager - Core Manager
=======================================

This module contains the MultiBoardManager class, which is the heart
of the plugin. It handles:

- Project configuration loading/saving
- Board creation and management
- Schematic synchronization via hardlinks
- Component placement tracking across boards
- Netlist export and parsing
- PCB updating from schematic
- Connectivity checking

SCHEMATIC SYNCHRONIZATION
-------------------------
The key innovation in this plugin is how it handles schematic sharing.
Instead of copying schematics to each board folder, we create HARDLINKS.

A hardlink makes two filenames point to the same underlying file data.
This means:
1. Editing "root/project.kicad_sch" and "boards/PSU/PSU.kicad_sch"
   modifies the SAME file - they are literally the same data on disk.
2. Changes made in any location are instantly available everywhere.
3. No synchronization code is needed - the filesystem handles it.

This is superior to copying because:
- No sync issues or version conflicts
- No wasted disk space
- No periodic refresh needed
- Works with hierarchical sheets automatically

NOTE: While the FILE is shared, if you have the schematic open in two
KiCad windows simultaneously, each window caches the file in memory.
You'll need to close/reopen to see changes made in the other window.
This is a KiCad limitation, not a plugin issue.

Author: Eliot
License: MIT
"""

import json
import os
import re
import subprocess
import shutil
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Set, Tuple, Any

import pcbnew

from .constants import (
    BOARDS_DIR,
    CONFIG_FILE,
    BLOCK_LIB_NAME,
    PORT_LIB_NAME,
    TEMP_NETLIST_NAME,
    DEBUG_LOG_NAME,
)
from .config import ProjectConfig, BoardConfig, PortDef


class MultiBoardManager:
    """
    Main controller for multiboard project management.
    
    This class orchestrates all operations for managing multiple PCBs
    that share a common schematic. It handles file operations, KiCad
    CLI integration, library management, and PCB updates.
    
    Usage:
        manager = MultiBoardManager(Path("/path/to/project"))
        manager.create_board("PowerSupply", "5V regulator module")
        manager.update_board("PowerSupply")
    
    Attributes:
        project_dir: Absolute path to the project root directory
        config_path: Path to the .kicad_multiboard.json file
        config: Current project configuration (ProjectConfig instance)
        block_lib_path: Path to the block footprint library
        port_lib_path: Path to the port footprint library
        log_path: Path to debug log file
    """
    
    def __init__(self, project_dir: Path):
        """
        Initialize the manager for a project directory.
        
        Args:
            project_dir: Path to any directory within the project.
                        The actual project root will be auto-detected.
        """
        # Find the actual project root (may be a parent directory)
        self.project_dir = self._find_project_root(project_dir)
        self.config_path = self.project_dir / CONFIG_FILE
        self.config = ProjectConfig()
        
        # Library paths for generated footprints
        self.block_lib_path = self.project_dir / f"{BLOCK_LIB_NAME}.pretty"
        self.port_lib_path = self.project_dir / f"{PORT_LIB_NAME}.pretty"
        
        # Debug logging
        self.log_path = self.project_dir / DEBUG_LOG_NAME
        
        # Caches for footprint library resolution
        self._fp_lib_cache: Dict[str, Path] = {}
        self._kicad_share: Optional[Path] = None
        
        # Initialize
        self._detect_root_files()
        self._load_config()
        self._init_libraries()
    
    # =========================================================================
    # Initialization and Configuration
    # =========================================================================
    
    def _find_project_root(self, start: Path) -> Path:
        """
        Find the project root directory by searching upward.
        
        Looks for either a .kicad_multiboard.json config file or
        a .kicad_pro project file.
        
        Args:
            start: Directory to start searching from
            
        Returns:
            Path to the project root directory
        """
        for path in [start] + list(start.parents):
            # Check for our config file
            if (path / CONFIG_FILE).exists():
                return path
            # Check for KiCad project file
            if list(path.glob("*.kicad_pro")):
                return path
        return start
    
    def _log(self, message: str):
        """
        Write a timestamped message to the debug log.
        
        Args:
            message: Log message to write
        """
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] {message}\n")
        except Exception:
            pass  # Logging should never cause failures
    
    def _detect_root_files(self):
        """
        Auto-detect the root schematic and PCB files.
        
        Looks for a .kicad_pro file and infers the schematic and
        PCB filenames from it.
        """
        for pro_file in self.project_dir.glob("*.kicad_pro"):
            schematic = pro_file.with_suffix(".kicad_sch")
            pcb = pro_file.with_suffix(".kicad_pcb")
            
            if schematic.exists():
                self.config.root_schematic = schematic.name
            if pcb.exists():
                self.config.root_pcb = pcb.name
            break
    
    def _load_config(self):
        """Load project configuration from disk."""
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    self.config = ProjectConfig.from_dict(json.load(f))
                # Re-detect root files in case they changed
                self._detect_root_files()
            except Exception as e:
                self._log(f"Config load error: {e}")
    
    def save_config(self):
        """Save project configuration to disk."""
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.config.to_dict(), f, indent=2)
    
    # =========================================================================
    # Library Management
    # =========================================================================
    
    def _init_libraries(self):
        """
        Initialize footprint library cache.
        
        Parses the project's fp-lib-table and scans KiCad's standard
        footprint directory to build a mapping of library nicknames
        to filesystem paths.
        """
        self._kicad_share = self._find_kicad_share()
        self._fp_lib_cache = {}
        
        # Parse project-specific library table
        project_table = self.project_dir / "fp-lib-table"
        if project_table.exists():
            self._parse_fp_lib_table(project_table)
        
        # Add KiCad's standard footprint libraries
        if self._kicad_share:
            fp_dir = self._kicad_share / "footprints"
            if fp_dir.exists():
                for lib_path in fp_dir.iterdir():
                    if lib_path.is_dir() and lib_path.suffix == ".pretty":
                        if lib_path.stem not in self._fp_lib_cache:
                            self._fp_lib_cache[lib_path.stem] = lib_path
        
        self._log(f"Loaded {len(self._fp_lib_cache)} footprint libraries")
    
    def _find_kicad_share(self) -> Optional[Path]:
        """
        Locate KiCad's shared data directory.
        
        This directory contains standard symbol and footprint libraries.
        Location varies by OS and installation method.
        
        Returns:
            Path to KiCad's share directory, or None if not found
        """
        if os.name == "nt":  # Windows
            # Check common Windows installation locations
            bases = [
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "KiCad",
                Path(os.environ.get("ProgramFiles", "")) / "KiCad",
            ]
            for base in bases:
                if base.exists():
                    # Get the newest version installed
                    for version_dir in sorted(base.iterdir(), reverse=True):
                        share = version_dir / "share" / "kicad"
                        if (share / "footprints").exists():
                            return share
        else:  # Linux/macOS
            for share in [Path("/usr/share/kicad"), Path("/usr/local/share/kicad")]:
                if (share / "footprints").exists():
                    return share
        return None
    
    def _parse_fp_lib_table(self, path: Path):
        """
        Parse a KiCad footprint library table file.
        
        Extracts library nickname to path mappings, expanding
        ${KIPRJMOD} to the project directory.
        
        Args:
            path: Path to the fp-lib-table file
        """
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
            
            # Match library entries: (name "...") ... (uri "...")
            pattern = r'\(name\s*"([^"]+)"\).*?\(uri\s*"([^"]+)"\)'
            for match in re.finditer(pattern, content, re.DOTALL):
                nickname = match.group(1)
                uri = match.group(2)
                
                # Expand KIPRJMOD variable
                expanded = uri.replace("${KIPRJMOD}", str(self.project_dir))
                
                # Only cache if we could resolve all variables
                if "${" not in expanded:
                    self._fp_lib_cache[nickname] = Path(expanded)
        except Exception:
            pass
    
    def _load_footprint(
        self, lib_nickname: str, footprint_name: str
    ) -> Optional["pcbnew.FOOTPRINT"]:
        """
        Load a footprint from a library.
        
        Tries multiple resolution strategies:
        1. Check the library cache (project + standard libraries)
        2. Try KiCad's standard library path
        3. Try direct loading (for absolute paths)
        
        Args:
            lib_nickname: Library name (e.g., "Resistor_SMD")
            footprint_name: Footprint name (e.g., "R_0402_1005Metric")
            
        Returns:
            Loaded FOOTPRINT object, or None if not found
        """
        # Try cached library path
        if lib_nickname in self._fp_lib_cache:
            try:
                return pcbnew.FootprintLoad(
                    str(self._fp_lib_cache[lib_nickname]),
                    footprint_name
                )
            except Exception:
                pass
        
        # Try KiCad standard library
        if self._kicad_share:
            std_path = self._kicad_share / "footprints" / f"{lib_nickname}.pretty"
            if std_path.exists():
                try:
                    return pcbnew.FootprintLoad(str(std_path), footprint_name)
                except Exception:
                    pass
        
        # Try direct loading (for absolute paths or fallback)
        try:
            return pcbnew.FootprintLoad(lib_nickname, footprint_name)
        except Exception:
            pass
        
        return None
    
    def _ensure_lib_in_table(self, lib_name: str, relative_path: str):
        """
        Ensure a library is registered in the project's fp-lib-table.
        
        If the library isn't already listed, adds it with a KIPRJMOD-relative
        path so it can be found when opening the project.
        
        Args:
            lib_name: Library nickname
            relative_path: Path relative to project root
        """
        table_path = self.project_dir / "fp-lib-table"
        entry = (
            f'  (lib (name "{lib_name}")(type "KiCad")'
            f'(uri "${{KIPRJMOD}}/{relative_path}")(options "")(descr ""))'
        )
        
        if table_path.exists():
            content = table_path.read_text(encoding="utf-8", errors="ignore")
            if lib_name in content:
                return  # Already registered
            # Append before closing paren
            content = content.rstrip().rstrip(')') + f'\n{entry}\n)'
        else:
            content = f'(fp_lib_table\n  (version 7)\n{entry}\n)'
        
        table_path.write_text(content, encoding="utf-8")
    
    # =========================================================================
    # KiCad CLI Integration
    # =========================================================================
    
    def _find_kicad_cli(self) -> Optional[str]:
        """
        Locate the kicad-cli executable.
        
        Returns:
            Path to kicad-cli, or None if not found
        """
        # Check PATH first
        exe = shutil.which("kicad-cli")
        if exe:
            return exe
        
        # Windows-specific paths
        if os.name == "nt":
            bases = [
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "KiCad",
                Path(os.environ.get("ProgramFiles", "")) / "KiCad",
            ]
            for base in bases:
                if base.exists():
                    for version_dir in sorted(base.iterdir(), reverse=True):
                        cli = version_dir / "bin" / "kicad-cli.exe"
                        if cli.exists():
                            return str(cli)
        return None
    
    def _run_cli(self, args: List[str]) -> subprocess.CompletedProcess:
        """
        Run a kicad-cli command.
        
        Args:
            args: Command-line arguments (without 'kicad-cli' prefix)
            
        Returns:
            CompletedProcess with command results
            
        Raises:
            FileNotFoundError: If kicad-cli is not found
        """
        cli = self._find_kicad_cli()
        if not cli:
            raise FileNotFoundError("kicad-cli not found in PATH")
        
        kwargs = {
            "capture_output": True,
            "text": True,
            "cwd": str(self.project_dir),
        }
        
        # Hide console window on Windows
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        
        return subprocess.run([cli] + args, **kwargs)
    
    # =========================================================================
    # Schematic Hardlink Management
    # =========================================================================
    
    def _setup_board_project(self, board: BoardConfig):
        """
        Set up the complete project structure for a sub-board.
        
        This creates/updates:
        1. The board directory structure
        2. A minimal .kicad_pro file
        3. Hardlinks to all schematic files (root + hierarchical sheets)
        4. Library tables with resolved paths
        
        IMPORTANT: Schematic hardlinks are the key to synchronization.
        After this runs, editing "boards/X/X.kicad_sch" or editing
        "root.kicad_sch" modifies the SAME underlying file.
        
        Args:
            board: Board configuration to set up
        """
        pcb_path = self.project_dir / board.pcb_path
        board_dir = pcb_path.parent
        base_name = pcb_path.stem
        
        # Create directory structure
        board_dir.mkdir(parents=True, exist_ok=True)
        
        # Create minimal .kicad_pro if it doesn't exist
        pro_file = board_dir / f"{base_name}.kicad_pro"
        if not pro_file.exists():
            pro_file.write_text(
                json.dumps({"meta": {"filename": pro_file.name}}, indent=2)
            )
        
        # Set up schematic hardlinks
        if self.config.root_schematic:
            root_sch = self.project_dir / self.config.root_schematic
            if root_sch.exists():
                # Link main schematic (with board-specific name)
                self._link_file(root_sch, board_dir / f"{base_name}.kicad_sch")
                
                # Link all hierarchical sheets (preserving their names)
                for sheet_path in self._find_hierarchical_sheets(root_sch):
                    source = (root_sch.parent / sheet_path).resolve()
                    if source.exists():
                        self._link_file(source, board_dir / sheet_path)
        
        # Copy library tables with resolved paths
        # We can't hardlink these because we need to modify ${KIPRJMOD} references
        for table_name in ("fp-lib-table", "sym-lib-table"):
            source = self.project_dir / table_name
            if source.exists():
                content = source.read_text(encoding="utf-8", errors="ignore")
                # Replace KIPRJMOD with absolute path to project root
                content = content.replace(
                    "${KIPRJMOD}",
                    self.project_dir.as_posix()
                )
                (board_dir / table_name).write_text(content, encoding="utf-8")
    
    def _link_file(self, source: Path, destination: Path):
        """
        Create a hardlink (or symlink as fallback).
        
        Hardlinks are preferred because they are more transparent to
        applications - the file appears to be a regular file at both
        locations. Symlinks are used as a fallback on filesystems that
        don't support hardlinks.
        
        Args:
            source: Original file path
            destination: New path that should point to the same file
        """
        destination.parent.mkdir(parents=True, exist_ok=True)
        
        # Remove existing link/file if present
        if destination.exists() or destination.is_symlink():
            destination.unlink()
        
        # Try hardlink first
        try:
            os.link(str(source), str(destination))
        except Exception:
            # Fall back to symlink
            try:
                os.symlink(str(source), str(destination))
            except Exception:
                pass  # Give up - user may need to manually copy
    
    def _find_hierarchical_sheets(self, schematic: Path) -> Set[Path]:
        """
        Find all hierarchical sheet files referenced by a schematic.
        
        Recursively follows sheet references to find all sub-sheets
        used in the design.
        
        Args:
            schematic: Path to the root schematic file
            
        Returns:
            Set of relative paths to all referenced schematic files
        """
        sheets = set()
        visited = set()
        stack = [schematic]
        
        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            
            try:
                content = current.read_text(encoding="utf-8", errors="ignore")
                
                # Find all sheet file references
                # Format in schematic: "filename.kicad_sch"
                for match in re.findall(r'"([^"]+\.kicad_sch)"', content):
                    sheet_path = Path(match)
                    sheets.add(sheet_path)
                    
                    # Add to stack for recursive processing
                    full_path = (current.parent / match).resolve()
                    if full_path.exists():
                        stack.append(full_path)
            except Exception:
                pass
        
        return sheets
    
    # =========================================================================
    # Board Management
    # =========================================================================
    
    def create_board(
        self, name: str, description: str = ""
    ) -> Tuple[bool, str]:
        """
        Create a new sub-board.
        
        This creates:
        1. A new board directory under boards/
        2. An empty PCB file
        3. Hardlinked schematic files
        4. Project and library table files
        5. A block footprint for the assembly view
        
        Args:
            name: Board name (used as directory and file name)
            description: Optional description
            
        Returns:
            Tuple of (success, message_or_path)
        """
        if name in self.config.boards:
            return False, f"Board '{name}' already exists"
        
        # Sanitize name for filesystem
        safe_name = "".join(
            c if c.isalnum() or c in "_-" else "_"
            for c in name
        )
        
        relative_path = f"{BOARDS_DIR}/{safe_name}/{safe_name}.kicad_pcb"
        pcb_path = self.project_dir / relative_path
        
        if pcb_path.exists():
            return False, f"PCB file already exists: {relative_path}"
        
        # Create directory and empty PCB
        pcb_path.parent.mkdir(parents=True, exist_ok=True)
        self._create_empty_pcb(pcb_path)
        
        # Create board configuration
        board = BoardConfig(
            name=name,
            pcb_path=relative_path,
            description=description,
        )
        
        # Set up project structure (hardlinks, etc.)
        self._setup_board_project(board)
        
        # Generate block footprint
        self._generate_block_footprint(board)
        self._ensure_lib_in_table(BLOCK_LIB_NAME, f"{BLOCK_LIB_NAME}.pretty")
        
        # Save configuration
        self.config.boards[name] = board
        self.save_config()
        
        return True, relative_path
    
    def _create_empty_pcb(self, path: Path):
        """
        Create an empty KiCad PCB file.
        
        Args:
            path: Where to create the PCB file
        """
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
    
    # =========================================================================
    # Block and Port Footprints
    # =========================================================================
    
    def _generate_block_footprint(self, board: BoardConfig):
        """
        Generate a block footprint representing the board.
        
        The block footprint is a rectangular shape with labeled pads
        at port locations. It can be placed on an assembly board to
        visualize the physical relationship between boards and check
        inter-board connectivity.
        
        Args:
            board: Board configuration
        """
        self.block_lib_path.mkdir(parents=True, exist_ok=True)
        
        fp_name = f"Block_{board.name}"
        width = board.block_width
        height = board.block_height
        
        # Build footprint definition
        lines = [
            f'(footprint "{fp_name}"',
            '  (version 20240108) (generator "multiboard") (layer "F.Cu")',
            f'  (descr "Board block: {board.name}")',
            '  (attr board_only exclude_from_pos_files exclude_from_bom)',
            
            # Reference text (above block)
            f'  (fp_text reference "REF**" (at 0 {-height/2 - 2:.1f}) '
            f'(layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))',
            
            # Value text (below block)
            f'  (fp_text value "{board.name}" (at 0 {height/2 + 2:.1f}) '
            f'(layer "F.Fab") (effects (font (size 1 1) (thickness 0.15))))',
            
            # Main outline rectangle
            f'  (fp_rect (start {-width/2:.2f} {-height/2:.2f}) '
            f'(end {width/2:.2f} {height/2:.2f}) '
            f'(stroke (width 0.25) (type solid)) (fill none) (layer "F.SilkS"))',
            
            # Courtyard rectangle
            f'  (fp_rect (start {-width/2 - 0.5:.2f} {-height/2 - 0.5:.2f}) '
            f'(end {width/2 + 0.5:.2f} {height/2 + 0.5:.2f}) '
            f'(stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd"))',
        ]
        
        # Add pads for each port
        pad_number = 1
        for port_name, port in sorted(board.ports.items()):
            x, y = self._calculate_port_position(port, width, height)
            rotation = {
                "left": 180,
                "right": 0,
                "top": 270,
                "bottom": 90,
            }.get(port.side, 0)
            
            # Port pad
            lines.append(
                f'  (pad "{pad_number}" smd roundrect '
                f'(at {x:.2f} {y:.2f} {rotation}) (size 2 1) '
                f'(layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))'
            )
            
            # Port label
            label_offset = 1.5 if port.side in ["left", "right"] else -1.5
            if port.side == "bottom":
                label_offset = 1.5
            lines.append(
                f'  (fp_text user "{port_name}" '
                f'(at {x:.2f} {y + label_offset:.2f}) '
                f'(layer "F.SilkS") (effects (font (size 0.8 0.8) (thickness 0.1))))'
            )
            
            pad_number += 1
        
        lines.append(')')
        
        # Write footprint file
        fp_path = self.block_lib_path / f"{fp_name}.kicad_mod"
        fp_path.write_text('\n'.join(lines), encoding="utf-8")
    
    def _calculate_port_position(
        self, port: PortDef, width: float, height: float
    ) -> Tuple[float, float]:
        """
        Calculate the X,Y position of a port on a block footprint.
        
        Args:
            port: Port definition
            width: Block width in mm
            height: Block height in mm
            
        Returns:
            Tuple of (x, y) coordinates in mm
        """
        position = port.position  # 0.0 to 1.0
        
        if port.side == "left":
            return (-width / 2, height * (position - 0.5))
        elif port.side == "right":
            return (width / 2, height * (position - 0.5))
        elif port.side == "top":
            return (width * (position - 0.5), -height / 2)
        elif port.side == "bottom":
            return (width * (position - 0.5), height / 2)
        return (0, 0)
    
    def generate_port_footprint(self, port_name: str):
        """
        Generate a port marker footprint for sub-boards.
        
        These can be placed on sub-boards at connector locations
        to mark where inter-board signals cross the board edge.
        
        Args:
            port_name: Name of the port
        """
        self.port_lib_path.mkdir(parents=True, exist_ok=True)
        
        fp_content = f'''(footprint "Port_{port_name}"
  (version 20240108) (generator "multiboard") (layer "F.Cu")
  (descr "Inter-board port: {port_name}")
  (attr smd)
  (fp_text reference "REF**" (at 0 -2) (layer "F.SilkS") (effects (font (size 0.8 0.8) (thickness 0.12))))
  (fp_text value "PORT" (at 0 2) (layer "F.Fab") (effects (font (size 0.8 0.8) (thickness 0.12))))
  (fp_text user "{port_name}" (at 0 0) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
  (pad "1" smd roundrect (at 0 0) (size 2 2) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))
  (fp_circle (center 0 0) (end 1.5 0) (stroke (width 0.15) (type solid)) (fill none) (layer "F.SilkS"))
)'''
        
        (self.port_lib_path / f"Port_{port_name}.kicad_mod").write_text(
            fp_content, encoding="utf-8"
        )
        self._ensure_lib_in_table(PORT_LIB_NAME, f"{PORT_LIB_NAME}.pretty")
    
    # =========================================================================
    # PCB Scanning
    # =========================================================================
    
    def scan_all_boards(self) -> Dict[str, Tuple[str, str]]:
        """
        Scan all board PCBs to find placed components.
        
        Returns:
            Dictionary mapping reference designator to
            (board_name, footprint_id) tuples.
            
        Example:
            {"R1": ("PowerSupply", "Resistor_SMD:R_0402_1005Metric"),
             "U1": ("MainBoard", "Package_SO:SOIC-8")}
        """
        placed = {}
        
        for name, board in self.config.boards.items():
            pcb_path = self.project_dir / board.pcb_path
            if not pcb_path.exists():
                continue
            
            try:
                pcb = pcbnew.LoadBoard(str(pcb_path))
                for footprint in pcb.GetFootprints():
                    ref = footprint.GetReference()
                    
                    # Skip invalid or internal references
                    if not ref or ref.startswith("#") or ref.startswith("MB_"):
                        continue
                    
                    fpid = footprint.GetFPID()
                    fp_string = f"{fpid.GetLibNickname()}:{fpid.GetLibItemName()}"
                    placed[ref] = (name, fp_string)
            except Exception as e:
                self._log(f"Scan error on {name}: {e}")
        
        return placed
    
    def get_board_nets(self, board_name: str) -> Dict[str, Set[str]]:
        """
        Get nets and their connected pads for a board.
        
        Args:
            board_name: Name of the board to analyze
            
        Returns:
            Dictionary mapping net names to sets of pad references
            (e.g., {"VCC": {"U1.4", "C1.1", "R5.2"}})
        """
        board = self.config.boards.get(board_name)
        if not board:
            return {}
        
        pcb_path = self.project_dir / board.pcb_path
        if not pcb_path.exists():
            return {}
        
        nets: Dict[str, Set[str]] = {}
        try:
            pcb = pcbnew.LoadBoard(str(pcb_path))
            for footprint in pcb.GetFootprints():
                ref = footprint.GetReference()
                for pad in footprint.Pads():
                    net_name = pad.GetNetname()
                    if net_name:
                        nets.setdefault(net_name, set()).add(
                            f"{ref}.{pad.GetNumber()}"
                        )
        except Exception:
            pass
        
        return nets
    
    # =========================================================================
    # Connectivity Checking
    # =========================================================================
    
    def check_connectivity(
        self, progress_callback=None
    ) -> Dict[str, Any]:
        """
        Check connectivity and run DRC on all boards.
        
        Uses kicad-cli to run DRC checks on each board, filtering out
        "unconnected" errors for nets that have ports (since those are
        expected to connect to other boards).
        
        Args:
            progress_callback: Optional callback(percent, message)
            
        Returns:
            Report dictionary with structure:
            {
                "boards": {
                    "BoardName": {"violations": count, "details": [...]}
                },
                "cross_board": [...],  # Inter-board connection issues
                "errors": [...],       # Processing errors
                "warnings": [...]      # Non-fatal issues
            }
        """
        report = {
            "boards": {},
            "cross_board": [],
            "errors": [],
            "warnings": [],
        }
        
        total_boards = len(self.config.boards)
        
        for index, (name, board) in enumerate(self.config.boards.items()):
            if progress_callback:
                percent = int(100 * index / max(total_boards, 1))
                progress_callback(percent, f"Checking {name}...")
            
            pcb_path = self.project_dir / board.pcb_path
            if not pcb_path.exists():
                report["errors"].append(f"{name}: PCB file not found")
                continue
            
            try:
                # Run DRC via CLI
                drc_output = pcb_path.with_suffix(".drc.json")
                self._run_cli([
                    "pcb", "drc",
                    "--format", "json",
                    "-o", str(drc_output),
                    str(pcb_path)
                ])
                
                if drc_output.exists():
                    drc_data = json.loads(
                        drc_output.read_text(encoding="utf-8")
                    )
                    violations = drc_data.get("violations", [])
                    
                    # Get nets that have ports (expected to be "unconnected")
                    port_nets = {
                        p.net for p in board.ports.values() if p.net
                    }
                    
                    # Filter out expected unconnected violations
                    filtered_violations = []
                    for violation in violations:
                        vtype = violation.get("type", "").lower()
                        if "unconnected" in vtype:
                            description = violation.get("description", "")
                            # Skip if this is a known port net
                            if any(pn in description for pn in port_nets):
                                continue
                        filtered_violations.append(violation)
                    
                    report["boards"][name] = {
                        "violations": len(filtered_violations),
                        "details": filtered_violations[:20],  # Limit for display
                    }
                    
                    # Clean up temporary file
                    drc_output.unlink()
            except Exception as e:
                report["errors"].append(f"{name}: DRC failed - {e}")
        
        if progress_callback:
            progress_callback(100, "Done")
        
        return report
    
    # =========================================================================
    # Board Update
    # =========================================================================
    
    def update_board(
        self, board_name: str, progress_callback=None
    ) -> Tuple[bool, str]:
        """
        Update a board PCB from the schematic.
        
        This is the main synchronization operation. It:
        1. Refreshes schematic hardlinks
        2. Exports a netlist from the schematic
        3. Parses the netlist to find all components
        4. Filters components to those assigned to this board
        5. Adds/updates footprints on the PCB
        6. Assigns nets to all pads
        
        A component is assigned to this board if:
        - It's not marked DNP or Exclude from Board
        - It's not already placed on a different board
        
        Args:
            board_name: Name of board to update
            progress_callback: Optional callback(percent, message)
            
        Returns:
            Tuple of (success, result_message)
        """
        board = self.config.boards.get(board_name)
        if not board:
            return False, f"Board '{board_name}' not found"
        
        pcb_path = self.project_dir / board.pcb_path
        if not pcb_path.exists():
            return False, f"PCB not found: {board.pcb_path}"
        
        try:
            # Step 1: Refresh schematic links
            if progress_callback:
                progress_callback(5, "Refreshing schematic links...")
            self._setup_board_project(board)
            
            # Step 2: Scan existing boards to find placed components
            if progress_callback:
                progress_callback(10, "Scanning existing boards...")
            placed_components = self.scan_all_boards()
            
            # Step 3: Export netlist
            if progress_callback:
                progress_callback(20, "Exporting netlist...")
            netlist_path = self._export_netlist()
            if not netlist_path:
                return False, "Failed to export netlist from schematic"
            
            # Step 4: Parse netlist
            if progress_callback:
                progress_callback(30, "Parsing netlist...")
            schematic_components = self._parse_netlist(netlist_path)
            
            # Step 5: Load the PCB
            if progress_callback:
                progress_callback(40, "Loading PCB...")
            pcb = pcbnew.LoadBoard(str(pcb_path))
            if not pcb:
                return False, "Failed to load PCB"
            
            # Build map of existing footprints on this board
            existing_footprints = {}
            existing_fp_ids = {}
            for fp in pcb.GetFootprints():
                ref = fp.GetReference()
                if ref:
                    existing_footprints[ref] = fp
                    fpid = fp.GetFPID()
                    existing_fp_ids[ref] = (
                        f"{fpid.GetLibNickname()}:{fpid.GetLibItemName()}"
                    )
            
            # Categorize components
            to_add = []
            to_update = []
            
            for ref, info in schematic_components.items():
                # Skip DNP and excluded components
                if info["dnp"] or info["exclude"]:
                    continue
                
                # Skip if placed on a different board
                if ref in placed_components:
                    placed_board, _ = placed_components[ref]
                    if placed_board != board_name:
                        continue
                
                if ref in existing_footprints:
                    to_update.append((ref, info))
                else:
                    to_add.append((ref, info))
            
            # Step 6: Update existing components
            if progress_callback:
                progress_callback(50, f"Updating {len(to_update)} components...")
            
            updated_count = 0
            replaced_count = 0
            
            for ref, info in to_update:
                fp = existing_footprints[ref]
                old_fp_id = existing_fp_ids.get(ref, "")
                
                if old_fp_id != info["footprint"]:
                    # Footprint changed - need to replace
                    lib, name = self._split_fpid(info["footprint"])
                    new_fp = self._load_footprint(lib, name)
                    
                    if new_fp:
                        # Preserve position and orientation
                        pos = fp.GetPosition()
                        rot = fp.GetOrientationDegrees()
                        layer = fp.GetLayer()
                        
                        # Remove old, add new
                        pcb.Remove(fp)
                        new_fp.SetReference(ref)
                        new_fp.SetValue(info["value"])
                        new_fp.SetPosition(pos)
                        new_fp.SetOrientationDegrees(rot)
                        new_fp.SetLayer(layer)
                        self._set_footprint_path(new_fp, info["tstamp"])
                        pcb.Add(new_fp)
                        
                        existing_footprints[ref] = new_fp
                        replaced_count += 1
                    else:
                        # Couldn't load new footprint, just update value
                        fp.SetValue(info["value"])
                        updated_count += 1
                else:
                    # Same footprint, update value and path
                    fp.SetValue(info["value"])
                    self._set_footprint_path(fp, info["tstamp"])
                    updated_count += 1
            
            # Step 7: Add new components
            if progress_callback:
                progress_callback(70, f"Adding {len(to_add)} components...")
            
            added_count = 0
            failed_count = 0
            failed_list = []
            
            for ref, info in to_add:
                lib, name = self._split_fpid(info["footprint"])
                fp = self._load_footprint(lib, name)
                
                if not fp:
                    failed_count += 1
                    failed_list.append(f"{ref}: {info['footprint']}")
                    continue
                
                fp.SetReference(ref)
                fp.SetValue(info["value"])
                fp.SetPosition(pcbnew.VECTOR2I(0, 0))  # At origin
                self._set_footprint_path(fp, info["tstamp"])
                pcb.Add(fp)
                
                existing_footprints[ref] = fp
                added_count += 1
            
            # Step 8: Assign nets
            if progress_callback:
                progress_callback(85, "Assigning nets...")
            self._assign_nets(pcb, netlist_path, existing_footprints)
            
            # Step 9: Save PCB
            if progress_callback:
                progress_callback(95, "Saving...")
            pcbnew.SaveBoard(str(pcb_path), pcb)
            
            # Cleanup temporary netlist
            try:
                netlist_path.unlink()
            except Exception:
                pass
            
            # Build result message
            msg = f"Added: {added_count}\nUpdated: {updated_count}"
            if replaced_count:
                msg += f"\nReplaced: {replaced_count}"
            msg += f"\nFailed: {failed_count}"
            if failed_list:
                msg += "\n\nFailed footprints:\n" + "\n".join(failed_list[:10])
            
            return True, msg
            
        except Exception as e:
            self._log(f"Update error: {e}\n{traceback.format_exc()}")
            return False, f"Error: {e}"
    
    def _export_netlist(self) -> Optional[Path]:
        """
        Export a netlist from the root schematic.
        
        Returns:
            Path to the temporary netlist file, or None on failure
        """
        if not self.config.root_schematic:
            return None
        
        schematic_path = self.project_dir / self.config.root_schematic
        if not schematic_path.exists():
            return None
        
        netlist_path = self.project_dir / TEMP_NETLIST_NAME
        
        try:
            self._run_cli([
                "sch", "export", "netlist",
                "--format", "kicadxml",
                "-o", str(netlist_path),
                str(schematic_path)
            ])
            return netlist_path if netlist_path.exists() else None
        except Exception:
            return None
    
    def _parse_netlist(self, path: Path) -> Dict[str, dict]:
        """
        Parse a KiCad XML netlist to extract component information.
        
        Uses iterparse for memory efficiency on large netlists.
        
        Args:
            path: Path to the netlist XML file
            
        Returns:
            Dictionary mapping reference designators to component info:
            {
                "R1": {
                    "footprint": "Resistor_SMD:R_0402",
                    "value": "10k",
                    "tstamp": "...",
                    "dnp": False,
                    "exclude": False
                }
            }
        """
        components = {}
        
        for event, elem in ET.iterparse(str(path), events=["end"]):
            if elem.tag != "comp":
                continue
            
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
                if child.tag == "footprint":
                    footprint = (child.text or "").strip()
                elif child.tag == "value":
                    value = (child.text or "").strip()
                elif child.tag == "tstamp":
                    tstamp = (child.text or "").strip()
                elif child.tag == "property":
                    prop_name = (child.get("name") or "").lower()
                    prop_value = (child.get("value") or "").lower()
                    
                    if prop_name == "dnp" and prop_value in ("yes", "true", "1"):
                        dnp = True
                    if ("exclude" in prop_name and "board" in prop_name and
                            prop_value in ("yes", "true", "1")):
                        exclude = True
            
            # Also check if value is literally "DNP"
            if value.upper() == "DNP":
                dnp = True
            
            # No footprint means we can't place it
            if not footprint:
                exclude = True
            
            components[ref] = {
                "footprint": footprint,
                "value": value,
                "tstamp": tstamp,
                "dnp": dnp,
                "exclude": exclude,
            }
            
            elem.clear()  # Free memory
        
        return components
    
    def _split_fpid(self, fpid: str) -> Tuple[str, str]:
        """
        Split a footprint ID into library and name.
        
        Args:
            fpid: Footprint ID (e.g., "Resistor_SMD:R_0402")
            
        Returns:
            Tuple of (library_nick, footprint_name)
        """
        if ":" in fpid:
            parts = fpid.split(":", 1)
            return parts[0], parts[1]
        return "", fpid
    
    def _set_footprint_path(self, fp, tstamp: str):
        """
        Set the schematic path on a footprint for cross-probing.
        
        Args:
            fp: Footprint object
            tstamp: Schematic timestamp/UUID
        """
        if tstamp:
            try:
                fp.SetPath(pcbnew.KIID_PATH(f"/{tstamp}"))
            except Exception:
                pass
    
    def _assign_nets(
        self, board, netlist_path: Path, footprints: Dict
    ):
        """
        Assign nets to footprint pads based on the netlist.
        
        Args:
            board: PCB board object
            netlist_path: Path to the netlist XML
            footprints: Dictionary mapping ref to footprint objects
        """
        # Build net info cache
        nets = {}
        for name, net in board.GetNetsByName().items():
            nets[name] = net
        
        def get_or_create_net(name: str):
            if name not in nets:
                net_info = pcbnew.NETINFO_ITEM(board, name)
                board.Add(net_info)
                nets[name] = net_info
            return nets[name]
        
        # Parse nets from netlist
        for event, elem in ET.iterparse(str(netlist_path), events=["end"]):
            if elem.tag != "net":
                continue
            
            net_name = elem.get("name", "")
            if not net_name:
                elem.clear()
                continue
            
            net_info = get_or_create_net(net_name)
            
            # Assign net to all connected pads
            for node in elem.findall("node"):
                ref = node.get("ref", "")
                pin = node.get("pin", "")
                
                fp = footprints.get(ref)
                if fp:
                    pad = fp.FindPadByNumber(pin)
                    if pad:
                        pad.SetNet(net_info)
            
            elem.clear()
    
    # =========================================================================
    # Status
    # =========================================================================
    
    def get_status(self) -> Tuple[Dict[str, str], Set[str], int]:
        """
        Get placement status across all boards.
        
        Returns:
            Tuple of:
            - placed: Dict mapping ref -> board_name for placed components
            - unplaced: Set of refs that exist in schematic but not placed
            - total: Total number of valid components in schematic
        """
        # Get all placed components
        placed_raw = self.scan_all_boards()
        placed = {ref: board for ref, (board, _) in placed_raw.items()}
        
        # Get all components from schematic
        netlist = self._export_netlist()
        if not netlist:
            return placed, set(), len(placed)
        
        components = self._parse_netlist(netlist)
        
        try:
            netlist.unlink()
        except Exception:
            pass
        
        # Find valid (non-DNP, non-excluded) components
        valid_refs = {
            ref for ref, info in components.items()
            if not info["dnp"] and not info["exclude"]
        }
        
        unplaced = valid_refs - set(placed.keys())
        
        return placed, unplaced, len(valid_refs)
