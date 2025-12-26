"""
Multi-Board PCB Manager - KiCad Action Plugin
==============================================
Hierarchical multi-board management with inter-board semantic connections.

Architecture:
- ROOT: Main project view showing all sub-PCBs
- SUB-PCBs: Independent boards with their own stackup/config
- PORTS: Input/Output connection points for inter-board linking
- CONNECTIONS: Semantic links between ports (no physical traces, for ERC)

Installation:
    Copy this folder to your KiCad plugins directory:
    - Linux: ~/.local/share/kicad/8.0/scripting/plugins/
    - Windows: %APPDATA%/kicad/8.0/scripting/plugins/
    - macOS: ~/Library/Preferences/kicad/8.0/scripting/plugins/
"""

import pcbnew
import wx
import os
import json
import re
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple, Set
from datetime import datetime
from dataclasses import dataclass, field, asdict
from enum import Enum


# ============================================================================
# Data Models
# ============================================================================

class PortDirection(Enum):
    INPUT = "input"
    OUTPUT = "output"
    BIDIRECTIONAL = "bidirectional"


@dataclass
class BoardPort:
    """A connection point on a sub-PCB (like a hierarchical label)."""
    name: str
    direction: str  # "input", "output", "bidirectional"
    net_name: str  # The actual net on this board
    connector_ref: str = ""  # Reference designator of connector (e.g., "J1")
    pin_number: str = ""  # Pin on the connector
    description: str = ""
    
    def to_dict(self) -> Dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'BoardPort':
        return cls(**data)


@dataclass
class InterBoardConnection:
    """A semantic connection between two ports on different boards."""
    id: str
    source_board: str
    source_port: str
    target_board: str
    target_port: str
    signal_name: str = ""  # Human-readable signal name
    description: str = ""
    
    def to_dict(self) -> Dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'InterBoardConnection':
        return cls(**data)


@dataclass 
class SubPCB:
    """Represents an independent sub-PCB in the hierarchy."""
    name: str
    pcb_filename: str = ""
    description: str = ""
    
    # Board configuration
    layers: int = 4
    stackup_preset: str = "4-Layer Standard"
    design_rules_preset: str = "Standard (6/6 mil)"
    
    # Position in root view (for visualization)
    position_x: float = 0.0
    position_y: float = 0.0
    
    # Ports for inter-board connections
    ports: List[BoardPort] = field(default_factory=list)
    
    # Metadata
    created_date: str = field(default_factory=lambda: datetime.now().isoformat())
    modified_date: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_dict(self) -> Dict:
        d = asdict(self)
        d['ports'] = [p.to_dict() if isinstance(p, BoardPort) else p for p in self.ports]
        return d
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'SubPCB':
        ports_data = data.pop('ports', [])
        obj = cls(**data)
        obj.ports = [BoardPort.from_dict(p) if isinstance(p, dict) else p for p in ports_data]
        return obj
    
    def add_port(self, port: BoardPort):
        self.ports.append(port)
        self.modified_date = datetime.now().isoformat()
    
    def remove_port(self, port_name: str):
        self.ports = [p for p in self.ports if p.name != port_name]
        self.modified_date = datetime.now().isoformat()
    
    def get_port(self, name: str) -> Optional[BoardPort]:
        for p in self.ports:
            if p.name == name:
                return p
        return None


@dataclass
class MultiBoardProject:
    """Root project containing all sub-PCBs and their connections."""
    name: str = "Multi-Board Project"
    version: str = "2.0"
    
    # Sub-PCBs
    boards: Dict[str, SubPCB] = field(default_factory=dict)
    
    # Inter-board connections
    connections: List[InterBoardConnection] = field(default_factory=list)
    
    # Project metadata
    created_date: str = field(default_factory=lambda: datetime.now().isoformat())
    modified_date: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "version": self.version,
            "boards": {k: v.to_dict() for k, v in self.boards.items()},
            "connections": [c.to_dict() if isinstance(c, InterBoardConnection) else c for c in self.connections],
            "created_date": self.created_date,
            "modified_date": self.modified_date,
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'MultiBoardProject':
        obj = cls(
            name=data.get("name", "Multi-Board Project"),
            version=data.get("version", "2.0"),
            created_date=data.get("created_date", datetime.now().isoformat()),
            modified_date=data.get("modified_date", datetime.now().isoformat()),
        )
        
        boards_data = data.get("boards", {})
        obj.boards = {k: SubPCB.from_dict(v) for k, v in boards_data.items()}
        
        connections_data = data.get("connections", [])
        obj.connections = [InterBoardConnection.from_dict(c) for c in connections_data]
        
        return obj


class ProjectManager:
    """Manages the multi-board project configuration."""
    
    CONFIG_FILENAME = ".kicad_multiboard.json"
    
    def __init__(self, project_path: str):
        self.project_path = Path(project_path)
        self.config_file = self.project_path / self.CONFIG_FILENAME
        self.project: MultiBoardProject = MultiBoardProject()
        self.load()
    
    def load(self):
        """Load project from file."""
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r') as f:
                    data = json.load(f)
                self.project = MultiBoardProject.from_dict(data)
            except (json.JSONDecodeError, IOError) as e:
                print(f"Error loading project: {e}")
                self.project = MultiBoardProject()
    
    def save(self):
        """Save project to file."""
        self.project.modified_date = datetime.now().isoformat()
        try:
            with open(self.config_file, 'w') as f:
                json.dump(self.project.to_dict(), f, indent=2)
        except IOError as e:
            print(f"Error saving project: {e}")
    
    def add_board(self, board: SubPCB) -> bool:
        if board.name in self.project.boards:
            return False
        self.project.boards[board.name] = board
        self.save()
        return True
    
    def remove_board(self, name: str) -> bool:
        if name not in self.project.boards:
            return False
        
        # Remove all connections involving this board
        self.project.connections = [
            c for c in self.project.connections 
            if c.source_board != name and c.target_board != name
        ]
        
        del self.project.boards[name]
        self.save()
        return True
    
    def get_board(self, name: str) -> Optional[SubPCB]:
        return self.project.boards.get(name)
    
    def add_connection(self, conn: InterBoardConnection) -> bool:
        # Validate boards and ports exist
        src_board = self.get_board(conn.source_board)
        tgt_board = self.get_board(conn.target_board)
        
        if not src_board or not tgt_board:
            return False
        
        if not src_board.get_port(conn.source_port):
            return False
        if not tgt_board.get_port(conn.target_port):
            return False
        
        self.project.connections.append(conn)
        self.save()
        return True
    
    def remove_connection(self, conn_id: str) -> bool:
        original_len = len(self.project.connections)
        self.project.connections = [c for c in self.project.connections if c.id != conn_id]
        if len(self.project.connections) < original_len:
            self.save()
            return True
        return False
    
    def get_unconnected_ports(self) -> List[Tuple[str, BoardPort]]:
        """Find all ports that are not connected to anything."""
        connected_ports = set()
        for conn in self.project.connections:
            connected_ports.add((conn.source_board, conn.source_port))
            connected_ports.add((conn.target_board, conn.target_port))
        
        unconnected = []
        for board_name, board in self.project.boards.items():
            for port in board.ports:
                if (board_name, port.name) not in connected_ports:
                    unconnected.append((board_name, port))
        
        return unconnected
    
    def run_connectivity_check(self) -> List[str]:
        """Run ERC-like check on inter-board connections."""
        errors = []
        warnings = []
        
        # Check for unconnected output ports
        for board_name, port in self.get_unconnected_ports():
            if port.direction == "output":
                errors.append(f"ERROR: Unconnected output '{port.name}' on board '{board_name}'")
            elif port.direction == "input":
                warnings.append(f"WARNING: Unconnected input '{port.name}' on board '{board_name}'")
            else:
                warnings.append(f"WARNING: Unconnected port '{port.name}' on board '{board_name}'")
        
        # Check for direction mismatches
        for conn in self.project.connections:
            src_board = self.get_board(conn.source_board)
            tgt_board = self.get_board(conn.target_board)
            
            if src_board and tgt_board:
                src_port = src_board.get_port(conn.source_port)
                tgt_port = tgt_board.get_port(conn.target_port)
                
                if src_port and tgt_port:
                    # Output should connect to Input (or bidirectional)
                    if src_port.direction == "output" and tgt_port.direction == "output":
                        errors.append(
                            f"ERROR: Output-to-Output connection: "
                            f"{conn.source_board}.{conn.source_port} -> {conn.target_board}.{conn.target_port}"
                        )
                    elif src_port.direction == "input" and tgt_port.direction == "input":
                        errors.append(
                            f"ERROR: Input-to-Input connection: "
                            f"{conn.source_board}.{conn.source_port} -> {conn.target_board}.{conn.target_port}"
                        )
        
        return errors + warnings


# ============================================================================
# Stackup and Design Rule Presets
# ============================================================================

STACKUP_PRESETS = {
    "2-Layer Standard": {"layers": 2, "thickness": 1.6},
    "4-Layer Standard": {"layers": 4, "thickness": 1.6},
    "4-Layer Sig-Gnd-Pwr-Sig": {"layers": 4, "thickness": 1.6},
    "6-Layer Standard": {"layers": 6, "thickness": 1.6},
    "6-Layer High-Speed": {"layers": 6, "thickness": 1.6},
    "8-Layer Standard": {"layers": 8, "thickness": 1.6},
}

DESIGN_RULE_PRESETS = {
    "Standard (6/6 mil)": {"min_track": 0.15, "min_clearance": 0.15},
    "Fine Pitch (4/4 mil)": {"min_track": 0.1, "min_clearance": 0.1},
    "HDI (3/3 mil)": {"min_track": 0.075, "min_clearance": 0.075},
    "Relaxed (8/8 mil)": {"min_track": 0.2, "min_clearance": 0.2},
}


# ============================================================================
# Utility Functions  
# ============================================================================

def get_nets_from_board(board: pcbnew.BOARD) -> List[str]:
    """Extract all net names from a board."""
    nets = []
    for net in board.GetNetInfo().NetsByName():
        net_name = net
        if net_name and net_name != "":
            nets.append(net_name)
    return sorted(nets)


def get_connectors_from_board(board: pcbnew.BOARD) -> List[Tuple[str, str]]:
    """Extract connector references and their pads."""
    connectors = []
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        # Common connector prefixes
        if ref.startswith(('J', 'P', 'CN', 'CON', 'X')):
            connectors.append((ref, fp.GetValue()))
    return sorted(connectors)


def generate_connection_id() -> str:
    """Generate a unique connection ID."""
    import uuid
    return str(uuid.uuid4())[:8]


def create_empty_pcb(filepath: Path, layer_count: int = 4) -> bool:
    """Create an empty PCB file with the specified layer count."""
    try:
        board = pcbnew.BOARD()
        
        # Set copper layer count using the board's design settings
        # KiCad 8.0 API
        board.SetCopperLayerCount(layer_count)
        
        # Save the board
        pcbnew.SaveBoard(str(filepath), board)
        return True
    except Exception as e:
        print(f"Error creating PCB: {e}")
        # Try alternative method for different KiCad versions
        try:
            board = pcbnew.BOARD()
            # Alternative: use GetDesignSettings
            ds = board.GetDesignSettings()
            board.SetCopperLayerCount(layer_count)
            pcbnew.SaveBoard(str(filepath), board)
            return True
        except Exception as e2:
            print(f"Alternative method also failed: {e2}")
            # Last resort: create minimal kicad_pcb file manually
            return create_pcb_file_manual(filepath, layer_count)


def create_pcb_file_manual(filepath: Path, layer_count: int = 4) -> bool:
    """Create a PCB file manually by writing the s-expression format."""
    
    # Build layer definitions
    copper_layers = []
    for i in range(layer_count):
        if i == 0:
            copper_layers.append(f'    (0 "F.Cu" signal)')
        elif i == layer_count - 1:
            copper_layers.append(f'    ({31} "B.Cu" signal)')
        else:
            copper_layers.append(f'    ({i} "In{i}.Cu" signal)')
    
    layers_str = "\n".join(copper_layers)
    
    content = f'''(kicad_pcb
  (version 20240108)
  (generator "multi_board_manager")
  (generator_version "1.0")
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
    (pcbplotparams
      (layerselection 0x00010fc_ffffffff)
      (plot_on_all_layers_selection 0x0000000_00000000)
      (disableapertmacros no)
      (usegerberextensions no)
      (usegerberattributes yes)
      (usegerberadvancedattributes yes)
      (creategerberjobfile yes)
      (dashed_line_dash_ratio 12.000000)
      (dashed_line_gap_ratio 3.000000)
      (svgprecision 4)
      (plotframeref no)
      (viasonmask no)
      (mode 1)
      (useauxorigin no)
      (hpglpennumber 1)
      (hpglpenspeed 20)
      (hpglpendiameter 15.000000)
      (pdf_front_fp_property_popups yes)
      (pdf_back_fp_property_popups yes)
      (dxfpolygonmode yes)
      (dxfimperialunits yes)
      (dxfusepcbnewfont yes)
      (psnegative no)
      (psa4output no)
      (plotreference yes)
      (plotvalue yes)
      (plotfptext yes)
      (plotinvisibletext no)
      (sketchpadsonfab no)
      (subtractmaskfromsilk no)
      (outputformat 1)
      (mirror no)
      (drillshape 1)
      (scaleselection 1)
      (outputdirectory "")
    )
  )
  (net 0 "")
)
'''
    
    try:
        with open(filepath, 'w') as f:
            f.write(content)
        return True
    except IOError as e:
        print(f"Error writing PCB file: {e}")
        return False


# ============================================================================
# Dialog Classes
# ============================================================================

class PortEditorDialog(wx.Dialog):
    """Dialog for editing ports on a sub-PCB."""
    
    def __init__(self, parent, board: SubPCB, pcb_board: Optional[pcbnew.BOARD] = None):
        super().__init__(parent, title=f"Port Editor - {board.name}", size=(700, 500))
        
        self.board = board
        self.pcb_board = pcb_board
        self.ports = list(board.ports)  # Work on a copy
        
        self.init_ui()
        self.refresh_list()
    
    def init_ui(self):
        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        # Info text
        info = wx.StaticText(
            panel,
            label="Define INPUT/OUTPUT ports for inter-board connections.\n"
                  "Ports represent signals that connect to other boards (e.g., via connectors)."
        )
        main_sizer.Add(info, 0, wx.ALL, 10)
        
        # Port list
        self.list_ctrl = wx.ListCtrl(
            panel,
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN
        )
        self.list_ctrl.InsertColumn(0, "Port Name", width=120)
        self.list_ctrl.InsertColumn(1, "Direction", width=100)
        self.list_ctrl.InsertColumn(2, "Net Name", width=120)
        self.list_ctrl.InsertColumn(3, "Connector", width=80)
        self.list_ctrl.InsertColumn(4, "Pin", width=50)
        self.list_ctrl.InsertColumn(5, "Description", width=150)
        
        main_sizer.Add(self.list_ctrl, 1, wx.EXPAND | wx.ALL, 10)
        
        # Buttons for port management
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        self.btn_add = wx.Button(panel, label="Add Port")
        self.btn_edit = wx.Button(panel, label="Edit Port")
        self.btn_remove = wx.Button(panel, label="Remove Port")
        self.btn_auto = wx.Button(panel, label="Auto-detect from Connectors")
        
        self.btn_add.Bind(wx.EVT_BUTTON, self.on_add_port)
        self.btn_edit.Bind(wx.EVT_BUTTON, self.on_edit_port)
        self.btn_remove.Bind(wx.EVT_BUTTON, self.on_remove_port)
        self.btn_auto.Bind(wx.EVT_BUTTON, self.on_auto_detect)
        
        btn_sizer.Add(self.btn_add, 0, wx.ALL, 5)
        btn_sizer.Add(self.btn_edit, 0, wx.ALL, 5)
        btn_sizer.Add(self.btn_remove, 0, wx.ALL, 5)
        btn_sizer.AddStretchSpacer()
        btn_sizer.Add(self.btn_auto, 0, wx.ALL, 5)
        
        main_sizer.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 5)
        
        # Dialog buttons
        dialog_btn_sizer = wx.StdDialogButtonSizer()
        btn_ok = wx.Button(panel, wx.ID_OK)
        btn_cancel = wx.Button(panel, wx.ID_CANCEL)
        btn_ok.Bind(wx.EVT_BUTTON, self.on_ok)
        dialog_btn_sizer.AddButton(btn_ok)
        dialog_btn_sizer.AddButton(btn_cancel)
        dialog_btn_sizer.Realize()
        
        main_sizer.Add(dialog_btn_sizer, 0, wx.ALIGN_RIGHT | wx.ALL, 10)
        
        panel.SetSizer(main_sizer)
    
    def refresh_list(self):
        self.list_ctrl.DeleteAllItems()
        for i, port in enumerate(self.ports):
            idx = self.list_ctrl.InsertItem(i, port.name)
            self.list_ctrl.SetItem(idx, 1, port.direction)
            self.list_ctrl.SetItem(idx, 2, port.net_name)
            self.list_ctrl.SetItem(idx, 3, port.connector_ref)
            self.list_ctrl.SetItem(idx, 4, port.pin_number)
            self.list_ctrl.SetItem(idx, 5, port.description)
    
    def on_add_port(self, event):
        dlg = SinglePortDialog(self, self.pcb_board)
        if dlg.ShowModal() == wx.ID_OK and dlg.port:
            self.ports.append(dlg.port)
            self.refresh_list()
        dlg.Destroy()
    
    def on_edit_port(self, event):
        idx = self.list_ctrl.GetFirstSelected()
        if idx < 0:
            wx.MessageBox("Please select a port to edit.", "No Selection", wx.OK | wx.ICON_WARNING)
            return
        
        port = self.ports[idx]
        dlg = SinglePortDialog(self, self.pcb_board, port)
        if dlg.ShowModal() == wx.ID_OK and dlg.port:
            self.ports[idx] = dlg.port
            self.refresh_list()
        dlg.Destroy()
    
    def on_remove_port(self, event):
        idx = self.list_ctrl.GetFirstSelected()
        if idx < 0:
            wx.MessageBox("Please select a port to remove.", "No Selection", wx.OK | wx.ICON_WARNING)
            return
        
        del self.ports[idx]
        self.refresh_list()
    
    def on_auto_detect(self, event):
        """Auto-detect ports from connector footprints."""
        if not self.pcb_board:
            wx.MessageBox(
                "No PCB loaded. Open the board's PCB file first to auto-detect.",
                "No PCB",
                wx.OK | wx.ICON_WARNING
            )
            return
        
        connectors = get_connectors_from_board(self.pcb_board)
        if not connectors:
            wx.MessageBox("No connectors found (J*, P*, CN*, CON*, X*).", "No Connectors", wx.OK | wx.ICON_INFO)
            return
        
        # For each connector, create a bidirectional port
        added = 0
        for ref, value in connectors:
            # Check if port already exists
            existing_names = {p.name for p in self.ports}
            if ref not in existing_names:
                port = BoardPort(
                    name=ref,
                    direction="bidirectional",
                    net_name="",
                    connector_ref=ref,
                    description=f"Auto-detected: {value}"
                )
                self.ports.append(port)
                added += 1
        
        self.refresh_list()
        wx.MessageBox(f"Added {added} ports from connectors.", "Auto-detect Complete", wx.OK | wx.ICON_INFO)
    
    def on_ok(self, event):
        self.board.ports = self.ports
        self.board.modified_date = datetime.now().isoformat()
        self.EndModal(wx.ID_OK)


class SinglePortDialog(wx.Dialog):
    """Dialog for adding/editing a single port."""
    
    def __init__(self, parent, pcb_board: Optional[pcbnew.BOARD] = None, port: Optional[BoardPort] = None):
        title = "Edit Port" if port else "Add Port"
        super().__init__(parent, title=title, size=(450, 350))
        
        self.pcb_board = pcb_board
        self.port: Optional[BoardPort] = None
        self.edit_port = port
        
        self.init_ui()
        
        if port:
            self.populate_from_port(port)
    
    def init_ui(self):
        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        grid = wx.FlexGridSizer(6, 2, 10, 10)
        grid.AddGrowableCol(1, 1)
        
        # Port name
        grid.Add(wx.StaticText(panel, label="Port Name:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_name = wx.TextCtrl(panel)
        grid.Add(self.txt_name, 1, wx.EXPAND)
        
        # Direction
        grid.Add(wx.StaticText(panel, label="Direction:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.combo_direction = wx.ComboBox(
            panel,
            choices=["input", "output", "bidirectional"],
            style=wx.CB_READONLY
        )
        self.combo_direction.SetSelection(2)  # Default to bidirectional
        grid.Add(self.combo_direction, 1, wx.EXPAND)
        
        # Net name
        grid.Add(wx.StaticText(panel, label="Net Name:"), 0, wx.ALIGN_CENTER_VERTICAL)
        if self.pcb_board:
            nets = get_nets_from_board(self.pcb_board)
            self.combo_net = wx.ComboBox(panel, choices=nets)
        else:
            self.combo_net = wx.ComboBox(panel, choices=[])
        grid.Add(self.combo_net, 1, wx.EXPAND)
        
        # Connector reference
        grid.Add(wx.StaticText(panel, label="Connector Ref:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_connector = wx.TextCtrl(panel)
        self.txt_connector.SetHint("e.g., J1, P2, CN1")
        grid.Add(self.txt_connector, 1, wx.EXPAND)
        
        # Pin number
        grid.Add(wx.StaticText(panel, label="Pin Number:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_pin = wx.TextCtrl(panel)
        self.txt_pin.SetHint("e.g., 1, A1")
        grid.Add(self.txt_pin, 1, wx.EXPAND)
        
        # Description
        grid.Add(wx.StaticText(panel, label="Description:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_desc = wx.TextCtrl(panel)
        grid.Add(self.txt_desc, 1, wx.EXPAND)
        
        main_sizer.Add(grid, 0, wx.EXPAND | wx.ALL, 20)
        
        # Dialog buttons
        btn_sizer = wx.StdDialogButtonSizer()
        btn_ok = wx.Button(panel, wx.ID_OK)
        btn_cancel = wx.Button(panel, wx.ID_CANCEL)
        btn_ok.Bind(wx.EVT_BUTTON, self.on_ok)
        btn_sizer.AddButton(btn_ok)
        btn_sizer.AddButton(btn_cancel)
        btn_sizer.Realize()
        
        main_sizer.Add(btn_sizer, 0, wx.ALIGN_RIGHT | wx.ALL, 10)
        
        panel.SetSizer(main_sizer)
    
    def populate_from_port(self, port: BoardPort):
        self.txt_name.SetValue(port.name)
        
        dir_idx = ["input", "output", "bidirectional"].index(port.direction)
        self.combo_direction.SetSelection(dir_idx)
        
        self.combo_net.SetValue(port.net_name)
        self.txt_connector.SetValue(port.connector_ref)
        self.txt_pin.SetValue(port.pin_number)
        self.txt_desc.SetValue(port.description)
    
    def on_ok(self, event):
        name = self.txt_name.GetValue().strip()
        if not name:
            wx.MessageBox("Please enter a port name.", "Validation Error", wx.OK | wx.ICON_ERROR)
            return
        
        self.port = BoardPort(
            name=name,
            direction=self.combo_direction.GetValue(),
            net_name=self.combo_net.GetValue(),
            connector_ref=self.txt_connector.GetValue().strip(),
            pin_number=self.txt_pin.GetValue().strip(),
            description=self.txt_desc.GetValue().strip()
        )
        
        self.EndModal(wx.ID_OK)


class ConnectionEditorDialog(wx.Dialog):
    """Dialog for managing inter-board connections."""
    
    def __init__(self, parent, project_mgr: ProjectManager):
        super().__init__(parent, title="Inter-Board Connections", size=(800, 550))
        
        self.project_mgr = project_mgr
        self.init_ui()
        self.refresh_list()
    
    def init_ui(self):
        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        # Info
        info = wx.StaticText(
            panel,
            label="Define semantic connections between boards (like hierarchical labels).\n"
                  "These don't represent physical traces - they show logical signal flow for ERC."
        )
        main_sizer.Add(info, 0, wx.ALL, 10)
        
        # Connection list
        self.list_ctrl = wx.ListCtrl(
            panel,
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN
        )
        self.list_ctrl.InsertColumn(0, "Source Board", width=120)
        self.list_ctrl.InsertColumn(1, "Source Port", width=100)
        self.list_ctrl.InsertColumn(2, "→", width=30)
        self.list_ctrl.InsertColumn(3, "Target Board", width=120)
        self.list_ctrl.InsertColumn(4, "Target Port", width=100)
        self.list_ctrl.InsertColumn(5, "Signal", width=100)
        self.list_ctrl.InsertColumn(6, "Description", width=150)
        
        main_sizer.Add(self.list_ctrl, 1, wx.EXPAND | wx.ALL, 10)
        
        # Buttons
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        self.btn_add = wx.Button(panel, label="Add Connection")
        self.btn_remove = wx.Button(panel, label="Remove Connection")
        self.btn_check = wx.Button(panel, label="Run Connectivity Check")
        
        self.btn_add.Bind(wx.EVT_BUTTON, self.on_add_connection)
        self.btn_remove.Bind(wx.EVT_BUTTON, self.on_remove_connection)
        self.btn_check.Bind(wx.EVT_BUTTON, self.on_run_check)
        
        btn_sizer.Add(self.btn_add, 0, wx.ALL, 5)
        btn_sizer.Add(self.btn_remove, 0, wx.ALL, 5)
        btn_sizer.AddStretchSpacer()
        btn_sizer.Add(self.btn_check, 0, wx.ALL, 5)
        
        main_sizer.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 5)
        
        # Close button
        btn_close = wx.Button(panel, wx.ID_CLOSE)
        btn_close.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        main_sizer.Add(btn_close, 0, wx.ALIGN_RIGHT | wx.ALL, 10)
        
        panel.SetSizer(main_sizer)
    
    def refresh_list(self):
        self.list_ctrl.DeleteAllItems()
        for i, conn in enumerate(self.project_mgr.project.connections):
            idx = self.list_ctrl.InsertItem(i, conn.source_board)
            self.list_ctrl.SetItem(idx, 1, conn.source_port)
            self.list_ctrl.SetItem(idx, 2, "→")
            self.list_ctrl.SetItem(idx, 3, conn.target_board)
            self.list_ctrl.SetItem(idx, 4, conn.target_port)
            self.list_ctrl.SetItem(idx, 5, conn.signal_name)
            self.list_ctrl.SetItem(idx, 6, conn.description)
    
    def on_add_connection(self, event):
        dlg = NewConnectionDialog(self, self.project_mgr)
        if dlg.ShowModal() == wx.ID_OK and dlg.connection:
            if self.project_mgr.add_connection(dlg.connection):
                self.refresh_list()
            else:
                wx.MessageBox("Failed to add connection. Check that boards and ports exist.", 
                              "Error", wx.OK | wx.ICON_ERROR)
        dlg.Destroy()
    
    def on_remove_connection(self, event):
        idx = self.list_ctrl.GetFirstSelected()
        if idx < 0:
            wx.MessageBox("Please select a connection to remove.", "No Selection", wx.OK | wx.ICON_WARNING)
            return
        
        conn = self.project_mgr.project.connections[idx]
        self.project_mgr.remove_connection(conn.id)
        self.refresh_list()
    
    def on_run_check(self, event):
        results = self.project_mgr.run_connectivity_check()
        
        if not results:
            wx.MessageBox("✓ All ports are connected.\nNo errors or warnings.", 
                          "Connectivity Check", wx.OK | wx.ICON_INFORMATION)
        else:
            msg = "Connectivity Check Results:\n\n" + "\n".join(results)
            wx.MessageBox(msg, "Connectivity Check", wx.OK | wx.ICON_WARNING)


class NewConnectionDialog(wx.Dialog):
    """Dialog for creating a new inter-board connection."""
    
    def __init__(self, parent, project_mgr: ProjectManager):
        super().__init__(parent, title="New Inter-Board Connection", size=(500, 400))
        
        self.project_mgr = project_mgr
        self.connection: Optional[InterBoardConnection] = None
        
        self.init_ui()
    
    def init_ui(self):
        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        board_names = list(self.project_mgr.project.boards.keys())
        
        # Source section
        src_box = wx.StaticBox(panel, label="Source")
        src_sizer = wx.StaticBoxSizer(src_box, wx.HORIZONTAL)
        
        src_sizer.Add(wx.StaticText(panel, label="Board:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        self.combo_src_board = wx.ComboBox(panel, choices=board_names, style=wx.CB_READONLY)
        self.combo_src_board.Bind(wx.EVT_COMBOBOX, self.on_src_board_changed)
        src_sizer.Add(self.combo_src_board, 1, wx.ALL, 5)
        
        src_sizer.Add(wx.StaticText(panel, label="Port:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        self.combo_src_port = wx.ComboBox(panel, choices=[], style=wx.CB_READONLY)
        src_sizer.Add(self.combo_src_port, 1, wx.ALL, 5)
        
        main_sizer.Add(src_sizer, 0, wx.EXPAND | wx.ALL, 10)
        
        # Arrow indicator
        arrow = wx.StaticText(panel, label="↓ connects to ↓")
        arrow.SetFont(wx.Font(12, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        main_sizer.Add(arrow, 0, wx.ALIGN_CENTER | wx.ALL, 5)
        
        # Target section
        tgt_box = wx.StaticBox(panel, label="Target")
        tgt_sizer = wx.StaticBoxSizer(tgt_box, wx.HORIZONTAL)
        
        tgt_sizer.Add(wx.StaticText(panel, label="Board:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        self.combo_tgt_board = wx.ComboBox(panel, choices=board_names, style=wx.CB_READONLY)
        self.combo_tgt_board.Bind(wx.EVT_COMBOBOX, self.on_tgt_board_changed)
        tgt_sizer.Add(self.combo_tgt_board, 1, wx.ALL, 5)
        
        tgt_sizer.Add(wx.StaticText(panel, label="Port:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        self.combo_tgt_port = wx.ComboBox(panel, choices=[], style=wx.CB_READONLY)
        tgt_sizer.Add(self.combo_tgt_port, 1, wx.ALL, 5)
        
        main_sizer.Add(tgt_sizer, 0, wx.EXPAND | wx.ALL, 10)
        
        # Signal info
        info_box = wx.StaticBox(panel, label="Signal Information")
        info_sizer = wx.StaticBoxSizer(info_box, wx.VERTICAL)
        
        grid = wx.FlexGridSizer(2, 2, 5, 10)
        grid.AddGrowableCol(1, 1)
        
        grid.Add(wx.StaticText(panel, label="Signal Name:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_signal = wx.TextCtrl(panel)
        self.txt_signal.SetHint("e.g., UART_TX, SPI_CLK")
        grid.Add(self.txt_signal, 1, wx.EXPAND)
        
        grid.Add(wx.StaticText(panel, label="Description:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_desc = wx.TextCtrl(panel)
        grid.Add(self.txt_desc, 1, wx.EXPAND)
        
        info_sizer.Add(grid, 0, wx.EXPAND | wx.ALL, 10)
        main_sizer.Add(info_sizer, 0, wx.EXPAND | wx.ALL, 10)
        
        # Dialog buttons
        btn_sizer = wx.StdDialogButtonSizer()
        btn_ok = wx.Button(panel, wx.ID_OK)
        btn_cancel = wx.Button(panel, wx.ID_CANCEL)
        btn_ok.Bind(wx.EVT_BUTTON, self.on_ok)
        btn_sizer.AddButton(btn_ok)
        btn_sizer.AddButton(btn_cancel)
        btn_sizer.Realize()
        
        main_sizer.Add(btn_sizer, 0, wx.ALIGN_RIGHT | wx.ALL, 10)
        
        panel.SetSizer(main_sizer)
    
    def on_src_board_changed(self, event):
        board_name = self.combo_src_board.GetValue()
        board = self.project_mgr.get_board(board_name)
        if board:
            port_names = [p.name for p in board.ports]
            self.combo_src_port.Set(port_names)
            if port_names:
                self.combo_src_port.SetSelection(0)
    
    def on_tgt_board_changed(self, event):
        board_name = self.combo_tgt_board.GetValue()
        board = self.project_mgr.get_board(board_name)
        if board:
            port_names = [p.name for p in board.ports]
            self.combo_tgt_port.Set(port_names)
            if port_names:
                self.combo_tgt_port.SetSelection(0)
    
    def on_ok(self, event):
        src_board = self.combo_src_board.GetValue()
        src_port = self.combo_src_port.GetValue()
        tgt_board = self.combo_tgt_board.GetValue()
        tgt_port = self.combo_tgt_port.GetValue()
        
        if not all([src_board, src_port, tgt_board, tgt_port]):
            wx.MessageBox("Please select source and target board/port.", "Validation Error", wx.OK | wx.ICON_ERROR)
            return
        
        self.connection = InterBoardConnection(
            id=generate_connection_id(),
            source_board=src_board,
            source_port=src_port,
            target_board=tgt_board,
            target_port=tgt_port,
            signal_name=self.txt_signal.GetValue().strip(),
            description=self.txt_desc.GetValue().strip()
        )
        
        self.EndModal(wx.ID_OK)


class NewBoardDialog(wx.Dialog):
    """Dialog for creating a new sub-PCB."""
    
    def __init__(self, parent, project_mgr: ProjectManager, edit_board: Optional[SubPCB] = None):
        title = "Edit Sub-PCB" if edit_board else "New Sub-PCB"
        super().__init__(parent, title=title, size=(500, 500))
        
        self.project_mgr = project_mgr
        self.edit_board = edit_board
        self.board: Optional[SubPCB] = None
        
        self.init_ui()
        
        if edit_board:
            self.populate_from_board(edit_board)
    
    def init_ui(self):
        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        # Basic info
        info_box = wx.StaticBox(panel, label="Board Information")
        info_sizer = wx.StaticBoxSizer(info_box, wx.VERTICAL)
        
        grid = wx.FlexGridSizer(3, 2, 10, 10)
        grid.AddGrowableCol(1, 1)
        
        grid.Add(wx.StaticText(panel, label="Board Name:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_name = wx.TextCtrl(panel)
        self.txt_name.SetHint("e.g., MainBoard, PowerSupply, Interface")
        grid.Add(self.txt_name, 1, wx.EXPAND)
        
        grid.Add(wx.StaticText(panel, label="PCB Filename:"), 0, wx.ALIGN_CENTER_VERTICAL)
        filename_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.txt_filename = wx.TextCtrl(panel)
        self.btn_browse = wx.Button(panel, label="...", size=(30, -1))
        self.btn_browse.Bind(wx.EVT_BUTTON, self.on_browse)
        filename_sizer.Add(self.txt_filename, 1, wx.EXPAND)
        filename_sizer.Add(self.btn_browse, 0, wx.LEFT, 5)
        grid.Add(filename_sizer, 1, wx.EXPAND)
        
        grid.Add(wx.StaticText(panel, label="Description:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_desc = wx.TextCtrl(panel)
        grid.Add(self.txt_desc, 1, wx.EXPAND)
        
        info_sizer.Add(grid, 0, wx.EXPAND | wx.ALL, 10)
        main_sizer.Add(info_sizer, 0, wx.EXPAND | wx.ALL, 10)
        
        # Stackup
        stackup_box = wx.StaticBox(panel, label="Stackup")
        stackup_sizer = wx.StaticBoxSizer(stackup_box, wx.HORIZONTAL)
        
        stackup_sizer.Add(wx.StaticText(panel, label="Preset:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        self.combo_stackup = wx.ComboBox(
            panel,
            choices=list(STACKUP_PRESETS.keys()),
            style=wx.CB_READONLY
        )
        self.combo_stackup.SetSelection(1)  # 4-Layer Standard
        self.combo_stackup.Bind(wx.EVT_COMBOBOX, self.on_stackup_changed)
        stackup_sizer.Add(self.combo_stackup, 1, wx.ALL, 5)
        
        stackup_sizer.Add(wx.StaticText(panel, label="Layers:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        self.spin_layers = wx.SpinCtrl(panel, min=1, max=32, initial=4)
        stackup_sizer.Add(self.spin_layers, 0, wx.ALL, 5)
        
        main_sizer.Add(stackup_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        
        # Design rules
        rules_box = wx.StaticBox(panel, label="Design Rules")
        rules_sizer = wx.StaticBoxSizer(rules_box, wx.HORIZONTAL)
        
        rules_sizer.Add(wx.StaticText(panel, label="Preset:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        self.combo_rules = wx.ComboBox(
            panel,
            choices=list(DESIGN_RULE_PRESETS.keys()),
            style=wx.CB_READONLY
        )
        self.combo_rules.SetSelection(0)
        rules_sizer.Add(self.combo_rules, 1, wx.ALL, 5)
        
        main_sizer.Add(rules_sizer, 0, wx.EXPAND | wx.ALL, 10)
        
        # Existing PCB checkbox
        self.chk_existing = wx.CheckBox(panel, label="Use existing PCB file (don't create new)")
        main_sizer.Add(self.chk_existing, 0, wx.ALL, 10)
        
        # Add spacer to push buttons to bottom
        main_sizer.AddStretchSpacer()
        
        # Dialog buttons - using explicit buttons instead of StdDialogButtonSizer
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_sizer.AddStretchSpacer()
        
        self.btn_ok = wx.Button(panel, wx.ID_OK, label="OK")
        self.btn_cancel = wx.Button(panel, wx.ID_CANCEL, label="Cancel")
        
        self.btn_ok.Bind(wx.EVT_BUTTON, self.on_ok)
        self.btn_cancel.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CANCEL))
        
        btn_sizer.Add(self.btn_ok, 0, wx.ALL, 5)
        btn_sizer.Add(self.btn_cancel, 0, wx.ALL, 5)
        
        main_sizer.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 10)
        
        panel.SetSizer(main_sizer)
        
        # Set OK as default button
        self.btn_ok.SetDefault()
    
    def populate_from_board(self, board: SubPCB):
        self.txt_name.SetValue(board.name)
        self.txt_name.Disable()
        self.txt_filename.SetValue(board.pcb_filename)
        self.txt_desc.SetValue(board.description)
        self.spin_layers.SetValue(board.layers)
        
        # Match stackup preset
        if board.stackup_preset in STACKUP_PRESETS:
            idx = list(STACKUP_PRESETS.keys()).index(board.stackup_preset)
            self.combo_stackup.SetSelection(idx)
        
        # Match rules preset
        if board.design_rules_preset in DESIGN_RULE_PRESETS:
            idx = list(DESIGN_RULE_PRESETS.keys()).index(board.design_rules_preset)
            self.combo_rules.SetSelection(idx)
        
        self.chk_existing.SetValue(True)
        self.chk_existing.Disable()
    
    def on_browse(self, event):
        dlg = wx.FileDialog(
            self,
            "Select/Save PCB File",
            defaultDir=str(self.project_mgr.project_path),
            wildcard="KiCad PCB files (*.kicad_pcb)|*.kicad_pcb",
            style=wx.FD_SAVE
        )
        if dlg.ShowModal() == wx.ID_OK:
            filepath = Path(dlg.GetPath())
            try:
                rel_path = filepath.relative_to(self.project_mgr.project_path)
                self.txt_filename.SetValue(str(rel_path))
            except ValueError:
                self.txt_filename.SetValue(str(filepath))
        dlg.Destroy()
    
    def on_stackup_changed(self, event):
        preset_name = self.combo_stackup.GetValue()
        if preset_name in STACKUP_PRESETS:
            self.spin_layers.SetValue(STACKUP_PRESETS[preset_name]["layers"])
    
    def on_ok(self, event):
        name = self.txt_name.GetValue().strip()
        if not name:
            wx.MessageBox("Please enter a board name.", "Validation Error", wx.OK | wx.ICON_ERROR)
            return
        
        if not self.edit_board and name in self.project_mgr.project.boards:
            wx.MessageBox("A board with this name already exists.", "Validation Error", wx.OK | wx.ICON_ERROR)
            return
        
        filename = self.txt_filename.GetValue().strip()
        if not filename:
            filename = f"{name}.kicad_pcb"
        
        self.board = SubPCB(
            name=name,
            pcb_filename=filename,
            description=self.txt_desc.GetValue().strip(),
            layers=self.spin_layers.GetValue(),
            stackup_preset=self.combo_stackup.GetValue(),
            design_rules_preset=self.combo_rules.GetValue()
        )
        
        # Copy ports from edit board if editing
        if self.edit_board:
            self.board.ports = self.edit_board.ports
            self.board.created_date = self.edit_board.created_date
        
        # Create PCB file if needed
        if not self.chk_existing.GetValue():
            pcb_path = self.project_mgr.project_path / filename
            if not pcb_path.exists():
                if not create_empty_pcb(pcb_path, self.board.layers):
                    wx.MessageBox(
                        f"Warning: Could not create PCB file.\n"
                        f"You may need to create it manually.",
                        "Warning",
                        wx.OK | wx.ICON_WARNING
                    )
        
        self.EndModal(wx.ID_OK)

class MainDialog(wx.Dialog):
    """Main dialog for Multi-Board PCB Manager."""
    
    def __init__(self, parent, board: pcbnew.BOARD):
        super().__init__(
            parent,
            title="Multi-Board PCB Manager",
            size=(900, 650),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER
        )
        
        self.current_board = board
        
        # Determine project path
        board_path = board.GetFileName()
        if board_path:
            self.project_path = Path(board_path).parent
        else:
            self.project_path = Path.cwd()
        
        self.project_mgr = ProjectManager(self.project_path)
        
        self.init_ui()
        self.bind_events()
        self.refresh_board_list()
    
    def init_ui(self):
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        # Header
        header_panel = wx.Panel(self)
        header_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        title = wx.StaticText(header_panel, label="Multi-Board Project")
        title_font = title.GetFont()
        title_font.SetPointSize(14)
        title_font.SetWeight(wx.FONTWEIGHT_BOLD)
        title.SetFont(title_font)
        
        header_sizer.Add(title, 0, wx.ALL, 10)
        header_sizer.AddStretchSpacer()
        header_sizer.Add(
            wx.StaticText(header_panel, label=f"Project: {self.project_path.name}"),
            0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 10
        )
        
        header_panel.SetSizer(header_sizer)
        main_sizer.Add(header_panel, 0, wx.EXPAND)
        
        # Splitter for boards and info
        splitter = wx.SplitterWindow(self, style=wx.SP_3D | wx.SP_LIVE_UPDATE)
        
        # Left panel - Board list
        left_panel = wx.Panel(splitter)
        left_sizer = wx.BoxSizer(wx.VERTICAL)
        
        left_sizer.Add(wx.StaticText(left_panel, label="Sub-PCBs (Boards)"), 0, wx.ALL, 5)
        
        self.board_list = wx.ListCtrl(
            left_panel,
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN
        )
        self.board_list.InsertColumn(0, "Name", width=120)
        self.board_list.InsertColumn(1, "Layers", width=50)
        self.board_list.InsertColumn(2, "Ports", width=50)
        self.board_list.InsertColumn(3, "PCB File", width=150)
        
        left_sizer.Add(self.board_list, 1, wx.EXPAND | wx.ALL, 5)
        
        # Board buttons
        board_btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_add_board = wx.Button(left_panel, label="Add Board")
        self.btn_edit_board = wx.Button(left_panel, label="Edit")
        self.btn_remove_board = wx.Button(left_panel, label="Remove")
        
        board_btn_sizer.Add(self.btn_add_board, 0, wx.ALL, 2)
        board_btn_sizer.Add(self.btn_edit_board, 0, wx.ALL, 2)
        board_btn_sizer.Add(self.btn_remove_board, 0, wx.ALL, 2)
        
        left_sizer.Add(board_btn_sizer, 0, wx.ALL, 5)
        
        # More board actions
        action_btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_edit_ports = wx.Button(left_panel, label="Edit Ports")
        self.btn_open_pcb = wx.Button(left_panel, label="Open PCB")
        
        action_btn_sizer.Add(self.btn_edit_ports, 0, wx.ALL, 2)
        action_btn_sizer.Add(self.btn_open_pcb, 0, wx.ALL, 2)
        
        left_sizer.Add(action_btn_sizer, 0, wx.ALL, 5)
        
        left_panel.SetSizer(left_sizer)
        
        # Right panel - Connections and info
        right_panel = wx.Panel(splitter)
        right_sizer = wx.BoxSizer(wx.VERTICAL)
        
        right_sizer.Add(wx.StaticText(right_panel, label="Inter-Board Connections"), 0, wx.ALL, 5)
        
        self.conn_list = wx.ListCtrl(
            right_panel,
            style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN
        )
        self.conn_list.InsertColumn(0, "From", width=150)
        self.conn_list.InsertColumn(1, "→", width=30)
        self.conn_list.InsertColumn(2, "To", width=150)
        self.conn_list.InsertColumn(3, "Signal", width=100)
        
        right_sizer.Add(self.conn_list, 1, wx.EXPAND | wx.ALL, 5)
        
        # Connection buttons
        conn_btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_add_conn = wx.Button(right_panel, label="Add Connection")
        self.btn_remove_conn = wx.Button(right_panel, label="Remove")
        self.btn_manage_conn = wx.Button(right_panel, label="Manage All...")
        
        conn_btn_sizer.Add(self.btn_add_conn, 0, wx.ALL, 2)
        conn_btn_sizer.Add(self.btn_remove_conn, 0, wx.ALL, 2)
        conn_btn_sizer.Add(self.btn_manage_conn, 0, wx.ALL, 2)
        
        right_sizer.Add(conn_btn_sizer, 0, wx.ALL, 5)
        
        right_panel.SetSizer(right_sizer)
        
        splitter.SplitVertically(left_panel, right_panel)
        splitter.SetMinimumPaneSize(300)
        splitter.SetSashPosition(400)
        
        main_sizer.Add(splitter, 1, wx.EXPAND | wx.ALL, 5)
        
        # Bottom buttons
        bottom_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        self.btn_check = wx.Button(self, label="Run Connectivity Check")
        self.btn_export = wx.Button(self, label="Export Report")
        btn_close = wx.Button(self, wx.ID_CLOSE)
        
        bottom_sizer.Add(self.btn_check, 0, wx.ALL, 5)
        bottom_sizer.Add(self.btn_export, 0, wx.ALL, 5)
        bottom_sizer.AddStretchSpacer()
        bottom_sizer.Add(btn_close, 0, wx.ALL, 5)
        
        main_sizer.Add(bottom_sizer, 0, wx.EXPAND | wx.ALL, 5)
        
        self.SetSizer(main_sizer)
    
    def bind_events(self):
        # Board events
        self.btn_add_board.Bind(wx.EVT_BUTTON, self.on_add_board)
        self.btn_edit_board.Bind(wx.EVT_BUTTON, self.on_edit_board)
        self.btn_remove_board.Bind(wx.EVT_BUTTON, self.on_remove_board)
        self.btn_edit_ports.Bind(wx.EVT_BUTTON, self.on_edit_ports)
        self.btn_open_pcb.Bind(wx.EVT_BUTTON, self.on_open_pcb)
        
        # Connection events
        self.btn_add_conn.Bind(wx.EVT_BUTTON, self.on_add_connection)
        self.btn_remove_conn.Bind(wx.EVT_BUTTON, self.on_remove_connection)
        self.btn_manage_conn.Bind(wx.EVT_BUTTON, self.on_manage_connections)
        
        # Other events
        self.btn_check.Bind(wx.EVT_BUTTON, self.on_run_check)
        self.btn_export.Bind(wx.EVT_BUTTON, self.on_export_report)
        self.Bind(wx.EVT_BUTTON, self.on_close, id=wx.ID_CLOSE)
    
    def refresh_board_list(self):
        self.board_list.DeleteAllItems()
        for i, (name, board) in enumerate(self.project_mgr.project.boards.items()):
            idx = self.board_list.InsertItem(i, name)
            self.board_list.SetItem(idx, 1, str(board.layers))
            self.board_list.SetItem(idx, 2, str(len(board.ports)))
            self.board_list.SetItem(idx, 3, board.pcb_filename)
        
        self.refresh_connection_list()
    
    def refresh_connection_list(self):
        self.conn_list.DeleteAllItems()
        for i, conn in enumerate(self.project_mgr.project.connections):
            from_str = f"{conn.source_board}.{conn.source_port}"
            to_str = f"{conn.target_board}.{conn.target_port}"
            
            idx = self.conn_list.InsertItem(i, from_str)
            self.conn_list.SetItem(idx, 1, "→")
            self.conn_list.SetItem(idx, 2, to_str)
            self.conn_list.SetItem(idx, 3, conn.signal_name)
    
    def get_selected_board(self) -> Optional[str]:
        idx = self.board_list.GetFirstSelected()
        if idx >= 0:
            return self.board_list.GetItemText(idx)
        return None
    
    def on_add_board(self, event):
        dlg = NewBoardDialog(self, self.project_mgr)
        if dlg.ShowModal() == wx.ID_OK and dlg.board:
            self.project_mgr.add_board(dlg.board)
            self.refresh_board_list()
        dlg.Destroy()
    
    def on_edit_board(self, event):
        name = self.get_selected_board()
        if not name:
            wx.MessageBox("Please select a board to edit.", "No Selection", wx.OK | wx.ICON_WARNING)
            return
        
        board = self.project_mgr.get_board(name)
        if board:
            dlg = NewBoardDialog(self, self.project_mgr, edit_board=board)
            if dlg.ShowModal() == wx.ID_OK and dlg.board:
                # Update board properties
                board.description = dlg.board.description
                board.pcb_filename = dlg.board.pcb_filename
                board.layers = dlg.board.layers
                board.stackup_preset = dlg.board.stackup_preset
                board.design_rules_preset = dlg.board.design_rules_preset
                board.modified_date = datetime.now().isoformat()
                self.project_mgr.save()
                self.refresh_board_list()
            dlg.Destroy()
    
    def on_remove_board(self, event):
        name = self.get_selected_board()
        if not name:
            wx.MessageBox("Please select a board to remove.", "No Selection", wx.OK | wx.ICON_WARNING)
            return
        
        if wx.MessageBox(
            f"Remove board '{name}'?\n"
            "This will also remove all connections involving this board.\n"
            "(PCB file will NOT be deleted)",
            "Confirm Remove",
            wx.YES_NO | wx.ICON_QUESTION
        ) == wx.YES:
            self.project_mgr.remove_board(name)
            self.refresh_board_list()
    
    def on_edit_ports(self, event):
        name = self.get_selected_board()
        if not name:
            wx.MessageBox("Please select a board to edit ports.", "No Selection", wx.OK | wx.ICON_WARNING)
            return
        
        board = self.project_mgr.get_board(name)
        if board:
            # Try to load the board's PCB file for net info
            pcb_board = None
            pcb_path = self.project_mgr.project_path / board.pcb_filename
            if pcb_path.exists():
                try:
                    pcb_board = pcbnew.LoadBoard(str(pcb_path))
                except:
                    pass
            
            dlg = PortEditorDialog(self, board, pcb_board)
            if dlg.ShowModal() == wx.ID_OK:
                self.project_mgr.save()
                self.refresh_board_list()
            dlg.Destroy()
    
    def on_open_pcb(self, event):
        name = self.get_selected_board()
        if not name:
            wx.MessageBox("Please select a board.", "No Selection", wx.OK | wx.ICON_WARNING)
            return
        
        board = self.project_mgr.get_board(name)
        if not board:
            return
        
        pcb_path = self.project_mgr.project_path / board.pcb_filename
        if not pcb_path.exists():
            wx.MessageBox(
                f"PCB file not found: {pcb_path}",
                "File Not Found",
                wx.OK | wx.ICON_WARNING
            )
            return
        
        import subprocess
        try:
            subprocess.Popen(["pcbnew", str(pcb_path)])
        except FileNotFoundError:
            wx.MessageBox(
                f"Could not launch pcbnew.\nPlease open manually:\n{pcb_path}",
                "Launch Error",
                wx.OK | wx.ICON_ERROR
            )
    
    def on_add_connection(self, event):
        if len(self.project_mgr.project.boards) < 2:
            wx.MessageBox(
                "Need at least 2 boards to create a connection.",
                "Not Enough Boards",
                wx.OK | wx.ICON_WARNING
            )
            return
        
        # Check if any boards have ports
        has_ports = any(len(b.ports) > 0 for b in self.project_mgr.project.boards.values())
        if not has_ports:
            wx.MessageBox(
                "No ports defined on any board.\n"
                "Please add ports to your boards first using 'Edit Ports'.",
                "No Ports",
                wx.OK | wx.ICON_WARNING
            )
            return
        
        dlg = NewConnectionDialog(self, self.project_mgr)
        if dlg.ShowModal() == wx.ID_OK and dlg.connection:
            if self.project_mgr.add_connection(dlg.connection):
                self.refresh_connection_list()
            else:
                wx.MessageBox("Failed to add connection.", "Error", wx.OK | wx.ICON_ERROR)
        dlg.Destroy()
    
    def on_remove_connection(self, event):
        idx = self.conn_list.GetFirstSelected()
        if idx < 0:
            wx.MessageBox("Please select a connection to remove.", "No Selection", wx.OK | wx.ICON_WARNING)
            return
        
        conn = self.project_mgr.project.connections[idx]
        self.project_mgr.remove_connection(conn.id)
        self.refresh_connection_list()
    
    def on_manage_connections(self, event):
        dlg = ConnectionEditorDialog(self, self.project_mgr)
        dlg.ShowModal()
        dlg.Destroy()
        self.refresh_connection_list()
    
    def on_run_check(self, event):
        results = self.project_mgr.run_connectivity_check()
        
        if not results:
            wx.MessageBox(
                "✓ Connectivity Check Passed\n\n"
                "All ports are connected with correct directions.",
                "Connectivity Check",
                wx.OK | wx.ICON_INFORMATION
            )
        else:
            errors = [r for r in results if r.startswith("ERROR")]
            warnings = [r for r in results if r.startswith("WARNING")]
            
            msg = "Connectivity Check Results\n"
            msg += "=" * 40 + "\n\n"
            
            if errors:
                msg += f"ERRORS ({len(errors)}):\n"
                for e in errors:
                    msg += f"  {e}\n"
                msg += "\n"
            
            if warnings:
                msg += f"WARNINGS ({len(warnings)}):\n"
                for w in warnings:
                    msg += f"  {w}\n"
            
            wx.MessageBox(msg, "Connectivity Check", wx.OK | wx.ICON_WARNING)
    
    def on_export_report(self, event):
        """Export a text report of the multi-board project."""
        report = []
        report.append("=" * 60)
        report.append("MULTI-BOARD PROJECT REPORT")
        report.append("=" * 60)
        report.append(f"Project: {self.project_mgr.project.name}")
        report.append(f"Path: {self.project_mgr.project_path}")
        report.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report.append("")
        
        # Boards section
        report.append("-" * 60)
        report.append("BOARDS")
        report.append("-" * 60)
        
        for name, board in self.project_mgr.project.boards.items():
            report.append(f"\n[{name}]")
            report.append(f"  PCB File: {board.pcb_filename}")
            report.append(f"  Layers: {board.layers}")
            report.append(f"  Stackup: {board.stackup_preset}")
            report.append(f"  Design Rules: {board.design_rules_preset}")
            report.append(f"  Description: {board.description}")
            
            if board.ports:
                report.append(f"  Ports ({len(board.ports)}):")
                for port in board.ports:
                    report.append(f"    - {port.name} [{port.direction}] -> {port.net_name}")
        
        report.append("")
        
        # Connections section
        report.append("-" * 60)
        report.append("INTER-BOARD CONNECTIONS")
        report.append("-" * 60)
        
        if self.project_mgr.project.connections:
            for conn in self.project_mgr.project.connections:
                report.append(f"\n{conn.source_board}.{conn.source_port}")
                report.append(f"  -> {conn.target_board}.{conn.target_port}")
                if conn.signal_name:
                    report.append(f"  Signal: {conn.signal_name}")
        else:
            report.append("\nNo connections defined.")
        
        report.append("")
        
        # Connectivity check
        report.append("-" * 60)
        report.append("CONNECTIVITY CHECK")
        report.append("-" * 60)
        
        results = self.project_mgr.run_connectivity_check()
        if results:
            for r in results:
                report.append(f"  {r}")
        else:
            report.append("  ✓ All checks passed")
        
        report.append("")
        report.append("=" * 60)
        
        # Save report
        dlg = wx.FileDialog(
            self,
            "Save Report",
            defaultDir=str(self.project_mgr.project_path),
            defaultFile="multiboard_report.txt",
            wildcard="Text files (*.txt)|*.txt",
            style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT
        )
        
        if dlg.ShowModal() == wx.ID_OK:
            filepath = dlg.GetPath()
            try:
                with open(filepath, 'w') as f:
                    f.write("\n".join(report))
                wx.MessageBox(f"Report saved to:\n{filepath}", "Export Complete", wx.OK | wx.ICON_INFORMATION)
            except IOError as e:
                wx.MessageBox(f"Error saving report: {e}", "Error", wx.OK | wx.ICON_ERROR)
        
        dlg.Destroy()
    
    def on_close(self, event):
        self.project_mgr.save()
        self.EndModal(wx.ID_CLOSE)


# ============================================================================
# KiCad Plugin Registration
# ============================================================================

class MultiBoardPlugin(pcbnew.ActionPlugin):
    """KiCad Action Plugin for Multi-Board Management."""
    
    def defaults(self):
        self.name = "Multi-Board Manager"
        self.category = "Project Management"
        self.description = (
            "Hierarchical multi-board PCB management with inter-board connections. "
            "Define INPUT/OUTPUT ports on each board and connect them semantically."
        )
        self.show_toolbar_button = True
        self.icon_file_name = os.path.join(os.path.dirname(__file__), "icon.png")
    
    def Run(self):
        board = pcbnew.GetBoard()
        if not board:
            wx.MessageBox(
                "No board is currently open.\n"
                "Please open a PCB file first.",
                "No Board",
                wx.OK | wx.ICON_ERROR
            )
            return
        
        dlg = MainDialog(None, board)
        dlg.ShowModal()
        dlg.Destroy()


# Register the plugin
MultiBoardPlugin().register()
