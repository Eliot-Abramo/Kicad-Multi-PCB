"""
Multi-Board PCB Manager v4 - KiCad Action Plugin
=================================================
Hierarchical PCB workflow - like schematic sheets but for PCBs.

- Creates ONLY .kicad_pcb files (no new projects)
- Creates schematic symlinks so "Update from Schematic" works
- Root PCB shows block diagram of all sub-boards
- Draw semantic inter-board connections for ERC

Installation:
    Copy to: ~/.local/share/kicad/8.0/scripting/plugins/
"""

import pcbnew
import wx
import os
import json
import sys
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Set
from datetime import datetime
from dataclasses import dataclass, field, asdict


# ============================================================================
# Data Models
# ============================================================================

@dataclass
class SubBoardDefinition:
    """A sub-PCB board (like a hierarchical sheet)."""
    name: str
    pcb_filename: str
    layers: int = 4
    description: str = ""
    
    # Position in root PCB block diagram (in mm)
    block_x: float = 50.0
    block_y: float = 50.0
    block_width: float = 40.0
    block_height: float = 30.0
    
    # Assigned schematic sheets (paths like "/Power/")
    assigned_sheets: List[str] = field(default_factory=list)
    
    # Inter-board ports (connector pins that go to other boards)
    ports: Dict[str, dict] = field(default_factory=dict)  # name -> {direction, net, x_offset, y_offset}
    
    def to_dict(self) -> Dict:
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'SubBoardDefinition':
        return cls(**data)


@dataclass
class InterBoardConnection:
    """Semantic connection between two board ports."""
    id: str
    from_board: str
    from_port: str
    to_board: str
    to_port: str
    net_name: str = ""


@dataclass
class MultiBoardProject:
    """Project configuration."""
    version: str = "4.0"
    root_schematic: str = ""  # The main schematic file
    root_pcb: str = ""  # The root PCB (block diagram)
    
    boards: Dict[str, SubBoardDefinition] = field(default_factory=dict)
    connections: List[InterBoardConnection] = field(default_factory=list)
    
    def to_dict(self) -> Dict:
        return {
            "version": self.version,
            "root_schematic": self.root_schematic,
            "root_pcb": self.root_pcb,
            "boards": {k: v.to_dict() for k, v in self.boards.items()},
            "connections": [asdict(c) for c in self.connections],
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'MultiBoardProject':
        obj = cls(
            version=data.get("version", "4.0"),
            root_schematic=data.get("root_schematic", ""),
            root_pcb=data.get("root_pcb", ""),
        )
        for name, bdata in data.get("boards", {}).items():
            obj.boards[name] = SubBoardDefinition.from_dict(bdata)
        for cdata in data.get("connections", []):
            obj.connections.append(InterBoardConnection(**cdata))
        return obj


class ProjectManager:
    """Manages the multi-board project."""
    
    CONFIG_FILENAME = ".kicad_multiboard.json"
    
    def __init__(self, project_path: Path):
        self.project_path = project_path
        self.config_file = project_path / self.CONFIG_FILENAME
        self.project = MultiBoardProject()
        
        # Auto-detect root schematic and PCB
        self._detect_root_files()
        self.load()
    
    def _detect_root_files(self):
        """Find the main .kicad_pro and associated files."""
        # Find the main project file
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
                with open(self.config_file, 'r') as f:
                    data = json.load(f)
                self.project = MultiBoardProject.from_dict(data)
                # Re-detect in case files changed
                self._detect_root_files()
            except (json.JSONDecodeError, IOError) as e:
                print(f"Error loading config: {e}")
    
    def save(self):
        try:
            with open(self.config_file, 'w') as f:
                json.dump(self.project.to_dict(), f, indent=2)
        except IOError as e:
            print(f"Error saving config: {e}")
    
    def create_sub_board(self, board: SubBoardDefinition) -> Tuple[bool, str]:
        """Create a new sub-board PCB file with schematic symlink."""
        
        pcb_path = self.project_path / board.pcb_filename
        sch_symlink = self.project_path / (Path(board.pcb_filename).stem + ".kicad_sch")
        
        # Check for existing files
        if pcb_path.exists():
            return False, f"PCB file already exists: {board.pcb_filename}"
        
        # 1. Create the PCB file (ONLY .kicad_pcb, no .kicad_pro)
        success = self._create_pcb_file(pcb_path, board.layers)
        if not success:
            return False, "Failed to create PCB file"
        
        # 2. Create schematic symlink so "Update from Schematic" works
        if not sch_symlink.exists() and self.project.root_schematic:
            success, msg = self._create_schematic_link(sch_symlink)
            if not success:
                return True, f"PCB created but schematic link failed: {msg}"
        
        # 3. Add to project
        self.project.boards[board.name] = board
        self.save()
        
        return True, f"Created {board.pcb_filename} with schematic link"
    
    def _create_pcb_file(self, filepath: Path, layer_count: int) -> bool:
        """Create an empty PCB file with specified layers."""
        
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
        
        # Minimal PCB content - NO project reference
        content = f'''(kicad_pcb
  (version 20240108)
  (generator "pcbnew")
  (generator_version "8.0")
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
        except IOError:
            return False
    
    def _create_schematic_link(self, link_path: Path) -> Tuple[bool, str]:
        """Create a symbolic link to the root schematic."""
        
        root_sch_path = self.project_path / self.project.root_schematic
        
        if not root_sch_path.exists():
            return False, f"Root schematic not found: {self.project.root_schematic}"
        
        try:
            # On Windows, might need admin rights or developer mode for symlinks
            if sys.platform == "win32":
                # Try symlink first
                try:
                    link_path.symlink_to(root_sch_path.name)
                    return True, "Symlink created"
                except OSError:
                    # Fallback: create a minimal redirect schematic
                    # This won't fully work but at least won't crash
                    return self._create_redirect_schematic(link_path, root_sch_path)
            else:
                # Unix - symlinks work normally
                link_path.symlink_to(root_sch_path.name)
                return True, "Symlink created"
                
        except Exception as e:
            return False, str(e)
    
    def _create_redirect_schematic(self, link_path: Path, target_path: Path) -> Tuple[bool, str]:
        """Windows fallback: copy the schematic (not ideal but works)."""
        import shutil
        try:
            shutil.copy2(target_path, link_path)
            return True, "Schematic copied (symlinks not available)"
        except Exception as e:
            return False, str(e)
    
    def delete_auto_created_project(self, board_name: str) -> bool:
        """Delete any auto-created .kicad_pro files for sub-boards."""
        board = self.project.boards.get(board_name)
        if not board:
            return False
        
        # KiCad auto-creates project files with same name as PCB
        pro_file = self.project_path / (Path(board.pcb_filename).stem + ".kicad_pro")
        
        if pro_file.exists():
            try:
                pro_file.unlink()
                return True
            except:
                return False
        return True
    
    def cleanup_auto_projects(self) -> List[str]:
        """Remove all auto-created project files for sub-boards."""
        cleaned = []
        for board in self.project.boards.values():
            pro_file = self.project_path / (Path(board.pcb_filename).stem + ".kicad_pro")
            if pro_file.exists():
                try:
                    pro_file.unlink()
                    cleaned.append(pro_file.name)
                except:
                    pass
        return cleaned
    
    def update_root_pcb_block_diagram(self) -> Tuple[bool, str]:
        """Update the root PCB with board blocks and connection lines."""
        
        if not self.project.root_pcb:
            return False, "No root PCB defined"
        
        root_pcb_path = self.project_path / self.project.root_pcb
        if not root_pcb_path.exists():
            return False, f"Root PCB not found: {self.project.root_pcb}"
        
        try:
            board = pcbnew.LoadBoard(str(root_pcb_path))
        except Exception as e:
            return False, f"Failed to load root PCB: {e}"
        
        # Use User.1 layer for block diagram
        block_layer = pcbnew.User_1
        
        # Clear existing block diagram elements (look for items with specific prefix in their text)
        # Note: This is tricky - for now, we'll just add new elements
        
        # Draw each sub-board as a rectangle with name
        for name, sub_board in self.project.boards.items():
            self._draw_board_block(board, sub_board, block_layer)
        
        # Draw connections
        for conn in self.project.connections:
            self._draw_connection(board, conn, block_layer)
        
        # Save
        try:
            pcbnew.SaveBoard(str(root_pcb_path), board)
            return True, "Block diagram updated in root PCB (User.1 layer)"
        except Exception as e:
            return False, f"Failed to save: {e}"
    
    def _draw_board_block(self, board: pcbnew.BOARD, sub_board: SubBoardDefinition, layer: int):
        """Draw a rectangle representing a sub-board."""
        
        x = pcbnew.FromMM(sub_board.block_x)
        y = pcbnew.FromMM(sub_board.block_y)
        w = pcbnew.FromMM(sub_board.block_width)
        h = pcbnew.FromMM(sub_board.block_height)
        
        # Draw rectangle
        rect = pcbnew.PCB_SHAPE(board)
        rect.SetShape(pcbnew.SHAPE_T_RECT)
        rect.SetStart(pcbnew.VECTOR2I(x, y))
        rect.SetEnd(pcbnew.VECTOR2I(x + w, y + h))
        rect.SetLayer(layer)
        rect.SetWidth(pcbnew.FromMM(0.3))
        board.Add(rect)
        
        # Add board name as text
        text = pcbnew.PCB_TEXT(board)
        text.SetText(sub_board.name)
        text.SetPosition(pcbnew.VECTOR2I(x + w // 2, y + h // 2))
        text.SetLayer(layer)
        text.SetTextSize(pcbnew.VECTOR2I(pcbnew.FromMM(2), pcbnew.FromMM(2)))
        text.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_CENTER)
        text.SetVertJustify(pcbnew.GR_TEXT_V_ALIGN_CENTER)
        board.Add(text)
        
        # Add layer count label
        layer_text = pcbnew.PCB_TEXT(board)
        layer_text.SetText(f"{sub_board.layers}L")
        layer_text.SetPosition(pcbnew.VECTOR2I(x + w // 2, y + h - pcbnew.FromMM(3)))
        layer_text.SetLayer(layer)
        layer_text.SetTextSize(pcbnew.VECTOR2I(pcbnew.FromMM(1.5), pcbnew.FromMM(1.5)))
        layer_text.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_CENTER)
        board.Add(layer_text)
        
        # Draw ports as small rectangles on edges
        for port_name, port_info in sub_board.ports.items():
            self._draw_port(board, sub_board, port_name, port_info, layer)
    
    def _draw_port(self, board: pcbnew.BOARD, sub_board: SubBoardDefinition, 
                   port_name: str, port_info: dict, layer: int):
        """Draw a port indicator on a board block."""
        
        base_x = pcbnew.FromMM(sub_board.block_x)
        base_y = pcbnew.FromMM(sub_board.block_y)
        
        x_off = pcbnew.FromMM(port_info.get('x_offset', 0))
        y_off = pcbnew.FromMM(port_info.get('y_offset', 0))
        
        px = base_x + x_off
        py = base_y + y_off
        
        # Small circle for port
        circle = pcbnew.PCB_SHAPE(board)
        circle.SetShape(pcbnew.SHAPE_T_CIRCLE)
        circle.SetCenter(pcbnew.VECTOR2I(px, py))
        circle.SetEnd(pcbnew.VECTOR2I(px + pcbnew.FromMM(1), py))
        circle.SetLayer(layer)
        circle.SetWidth(pcbnew.FromMM(0.2))
        board.Add(circle)
        
        # Port name
        text = pcbnew.PCB_TEXT(board)
        text.SetText(port_name)
        text.SetPosition(pcbnew.VECTOR2I(px + pcbnew.FromMM(2), py))
        text.SetLayer(layer)
        text.SetTextSize(pcbnew.VECTOR2I(pcbnew.FromMM(1), pcbnew.FromMM(1)))
        board.Add(text)
    
    def _draw_connection(self, board: pcbnew.BOARD, conn: InterBoardConnection, layer: int):
        """Draw a line between two board ports."""
        
        from_board = self.project.boards.get(conn.from_board)
        to_board = self.project.boards.get(conn.to_board)
        
        if not from_board or not to_board:
            return
        
        from_port = from_board.ports.get(conn.from_port, {})
        to_port = to_board.ports.get(conn.to_port, {})
        
        # Calculate positions
        x1 = pcbnew.FromMM(from_board.block_x + from_port.get('x_offset', from_board.block_width))
        y1 = pcbnew.FromMM(from_board.block_y + from_port.get('y_offset', from_board.block_height / 2))
        
        x2 = pcbnew.FromMM(to_board.block_x + to_port.get('x_offset', 0))
        y2 = pcbnew.FromMM(to_board.block_y + to_port.get('y_offset', to_board.block_height / 2))
        
        # Draw line
        line = pcbnew.PCB_SHAPE(board)
        line.SetShape(pcbnew.SHAPE_T_SEGMENT)
        line.SetStart(pcbnew.VECTOR2I(int(x1), int(y1)))
        line.SetEnd(pcbnew.VECTOR2I(int(x2), int(y2)))
        line.SetLayer(layer)
        line.SetWidth(pcbnew.FromMM(0.25))
        board.Add(line)
        
        # Add net name label at midpoint
        if conn.net_name:
            mx = (x1 + x2) // 2
            my = (y1 + y2) // 2
            
            text = pcbnew.PCB_TEXT(board)
            text.SetText(conn.net_name)
            text.SetPosition(pcbnew.VECTOR2I(int(mx), int(my) - pcbnew.FromMM(2)))
            text.SetLayer(layer)
            text.SetTextSize(pcbnew.VECTOR2I(pcbnew.FromMM(1), pcbnew.FromMM(1)))
            text.SetHorizJustify(pcbnew.GR_TEXT_H_ALIGN_CENTER)
            board.Add(text)


# ============================================================================
# Dialog Classes
# ============================================================================

class NewSubBoardDialog(wx.Dialog):
    """Dialog to create a new sub-board PCB."""
    
    def __init__(self, parent, project_mgr: ProjectManager):
        super().__init__(parent, title="New Sub-Board PCB", size=(500, 400))
        
        self.project_mgr = project_mgr
        self.board: Optional[SubBoardDefinition] = None
        
        self.init_ui()
    
    def init_ui(self):
        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        # Info text
        info = wx.StaticText(
            panel,
            label="Create a new sub-board PCB file.\n"
                  "This creates ONLY a .kicad_pcb file (no new project).\n"
                  "A schematic link will be created so 'Update from Schematic' works."
        )
        main_sizer.Add(info, 0, wx.ALL, 10)
        
        # Form
        grid = wx.FlexGridSizer(4, 2, 10, 10)
        grid.AddGrowableCol(1, 1)
        
        # Name
        grid.Add(wx.StaticText(panel, label="Board Name:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_name = wx.TextCtrl(panel)
        self.txt_name.Bind(wx.EVT_TEXT, self.on_name_changed)
        grid.Add(self.txt_name, 1, wx.EXPAND)
        
        # Filename (auto-generated)
        grid.Add(wx.StaticText(panel, label="PCB Filename:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_filename = wx.TextCtrl(panel)
        self.txt_filename.SetEditable(False)
        self.txt_filename.SetBackgroundColour(wx.Colour(240, 240, 240))
        grid.Add(self.txt_filename, 1, wx.EXPAND)
        
        # Layers
        grid.Add(wx.StaticText(panel, label="Copper Layers:"), 0, wx.ALIGN_CENTER_VERTICAL)
        layer_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.spin_layers = wx.SpinCtrl(panel, min=2, max=32, initial=4)
        layer_sizer.Add(self.spin_layers, 0)
        layer_sizer.Add(wx.StaticText(panel, label="  (2, 4, 6, 8...)"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(layer_sizer, 1)
        
        # Description
        grid.Add(wx.StaticText(panel, label="Description:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_desc = wx.TextCtrl(panel)
        grid.Add(self.txt_desc, 1, wx.EXPAND)
        
        main_sizer.Add(grid, 0, wx.EXPAND | wx.ALL, 20)
        
        # Schematic info
        sch_info = wx.StaticBox(panel, label="Schematic Association")
        sch_sizer = wx.StaticBoxSizer(sch_info, wx.VERTICAL)
        
        root_sch = self.project_mgr.project.root_schematic or "(not detected)"
        sch_label = wx.StaticText(panel, label=f"Root Schematic: {root_sch}")
        sch_sizer.Add(sch_label, 0, wx.ALL, 5)
        
        link_label = wx.StaticText(
            panel, 
            label="A symbolic link will be created so this PCB can\n"
                  "use 'Update PCB from Schematic' with the root schematic."
        )
        link_label.SetForegroundColour(wx.Colour(80, 80, 80))
        sch_sizer.Add(link_label, 0, wx.ALL, 5)
        
        main_sizer.Add(sch_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 20)
        
        main_sizer.AddStretchSpacer()
        
        # Buttons
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_sizer.AddStretchSpacer()
        
        self.btn_create = wx.Button(panel, label="Create Sub-Board")
        self.btn_cancel = wx.Button(panel, wx.ID_CANCEL, label="Cancel")
        
        self.btn_create.Bind(wx.EVT_BUTTON, self.on_create)
        
        btn_sizer.Add(self.btn_create, 0, wx.ALL, 5)
        btn_sizer.Add(self.btn_cancel, 0, wx.ALL, 5)
        
        main_sizer.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 10)
        
        panel.SetSizer(main_sizer)
        self.btn_create.SetDefault()
    
    def on_name_changed(self, event):
        name = self.txt_name.GetValue().strip()
        if name:
            # Generate filename from name
            safe_name = "".join(c if c.isalnum() or c in "_-" else "_" for c in name)
            self.txt_filename.SetValue(f"{safe_name}.kicad_pcb")
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
        
        filename = self.txt_filename.GetValue()
        if not filename:
            wx.MessageBox("Invalid filename.", "Error", wx.OK | wx.ICON_ERROR)
            return
        
        # Check if PCB file already exists
        pcb_path = self.project_mgr.project_path / filename
        if pcb_path.exists():
            wx.MessageBox(f"File already exists: {filename}", "Error", wx.OK | wx.ICON_ERROR)
            return
        
        self.board = SubBoardDefinition(
            name=name,
            pcb_filename=filename,
            layers=self.spin_layers.GetValue(),
            description=self.txt_desc.GetValue().strip(),
        )
        
        self.EndModal(wx.ID_OK)


class PortEditorDialog(wx.Dialog):
    """Dialog to edit ports on a sub-board."""
    
    def __init__(self, parent, board: SubBoardDefinition):
        super().__init__(parent, title=f"Edit Ports - {board.name}", size=(600, 450))
        
        self.board = board
        self.ports = dict(board.ports)  # Work on copy
        
        self.init_ui()
        self.refresh_list()
    
    def init_ui(self):
        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        info = wx.StaticText(
            panel,
            label="Define inter-board ports (connector pins that connect to other boards).\n"
                  "These appear on the block diagram in the root PCB."
        )
        main_sizer.Add(info, 0, wx.ALL, 10)
        
        # Port list
        self.list_ctrl = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.list_ctrl.InsertColumn(0, "Port Name", width=120)
        self.list_ctrl.InsertColumn(1, "Direction", width=100)
        self.list_ctrl.InsertColumn(2, "Net", width=120)
        self.list_ctrl.InsertColumn(3, "Position", width=100)
        main_sizer.Add(self.list_ctrl, 1, wx.EXPAND | wx.ALL, 10)
        
        # Buttons
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_add = wx.Button(panel, label="Add Port")
        self.btn_edit = wx.Button(panel, label="Edit")
        self.btn_remove = wx.Button(panel, label="Remove")
        
        self.btn_add.Bind(wx.EVT_BUTTON, self.on_add)
        self.btn_edit.Bind(wx.EVT_BUTTON, self.on_edit)
        self.btn_remove.Bind(wx.EVT_BUTTON, self.on_remove)
        
        btn_sizer.Add(self.btn_add, 0, wx.ALL, 5)
        btn_sizer.Add(self.btn_edit, 0, wx.ALL, 5)
        btn_sizer.Add(self.btn_remove, 0, wx.ALL, 5)
        main_sizer.Add(btn_sizer, 0, wx.ALL, 5)
        
        # Dialog buttons
        dialog_sizer = wx.BoxSizer(wx.HORIZONTAL)
        dialog_sizer.AddStretchSpacer()
        btn_ok = wx.Button(panel, wx.ID_OK)
        btn_cancel = wx.Button(panel, wx.ID_CANCEL)
        btn_ok.Bind(wx.EVT_BUTTON, self.on_ok)
        dialog_sizer.Add(btn_ok, 0, wx.ALL, 5)
        dialog_sizer.Add(btn_cancel, 0, wx.ALL, 5)
        main_sizer.Add(dialog_sizer, 0, wx.EXPAND | wx.ALL, 10)
        
        panel.SetSizer(main_sizer)
    
    def refresh_list(self):
        self.list_ctrl.DeleteAllItems()
        for name, info in self.ports.items():
            idx = self.list_ctrl.InsertItem(self.list_ctrl.GetItemCount(), name)
            self.list_ctrl.SetItem(idx, 1, info.get('direction', 'bidir'))
            self.list_ctrl.SetItem(idx, 2, info.get('net', ''))
            pos = f"({info.get('x_offset', 0)}, {info.get('y_offset', 0)})"
            self.list_ctrl.SetItem(idx, 3, pos)
    
    def on_add(self, event):
        dlg = SinglePortDialog(self, self.board)
        if dlg.ShowModal() == wx.ID_OK:
            self.ports[dlg.port_name] = dlg.port_info
            self.refresh_list()
        dlg.Destroy()
    
    def on_edit(self, event):
        idx = self.list_ctrl.GetFirstSelected()
        if idx < 0:
            return
        
        port_name = self.list_ctrl.GetItemText(idx)
        port_info = self.ports.get(port_name, {})
        
        dlg = SinglePortDialog(self, self.board, port_name, port_info)
        if dlg.ShowModal() == wx.ID_OK:
            if dlg.port_name != port_name:
                del self.ports[port_name]
            self.ports[dlg.port_name] = dlg.port_info
            self.refresh_list()
        dlg.Destroy()
    
    def on_remove(self, event):
        idx = self.list_ctrl.GetFirstSelected()
        if idx < 0:
            return
        
        port_name = self.list_ctrl.GetItemText(idx)
        del self.ports[port_name]
        self.refresh_list()
    
    def on_ok(self, event):
        self.board.ports = self.ports
        self.EndModal(wx.ID_OK)


class SinglePortDialog(wx.Dialog):
    """Dialog for adding/editing a single port."""
    
    def __init__(self, parent, board: SubBoardDefinition, 
                 port_name: str = "", port_info: dict = None):
        title = "Edit Port" if port_name else "Add Port"
        super().__init__(parent, title=title, size=(400, 300))
        
        self.board = board
        self.port_name = port_name
        self.port_info = port_info or {}
        
        self.init_ui()
    
    def init_ui(self):
        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        grid = wx.FlexGridSizer(5, 2, 10, 10)
        grid.AddGrowableCol(1, 1)
        
        # Port name
        grid.Add(wx.StaticText(panel, label="Port Name:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_name = wx.TextCtrl(panel, value=self.port_name)
        grid.Add(self.txt_name, 1, wx.EXPAND)
        
        # Direction
        grid.Add(wx.StaticText(panel, label="Direction:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.combo_dir = wx.ComboBox(panel, choices=["input", "output", "bidir"], style=wx.CB_READONLY)
        self.combo_dir.SetValue(self.port_info.get('direction', 'bidir'))
        grid.Add(self.combo_dir, 1, wx.EXPAND)
        
        # Net name
        grid.Add(wx.StaticText(panel, label="Net Name:"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_net = wx.TextCtrl(panel, value=self.port_info.get('net', ''))
        grid.Add(self.txt_net, 1, wx.EXPAND)
        
        # X offset (position on block)
        grid.Add(wx.StaticText(panel, label="X Offset (mm):"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.spin_x = wx.SpinCtrlDouble(panel, min=-100, max=200, initial=self.port_info.get('x_offset', self.board.block_width))
        grid.Add(self.spin_x, 0)
        
        # Y offset
        grid.Add(wx.StaticText(panel, label="Y Offset (mm):"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.spin_y = wx.SpinCtrlDouble(panel, min=-100, max=200, initial=self.port_info.get('y_offset', self.board.block_height / 2))
        grid.Add(self.spin_y, 0)
        
        main_sizer.Add(grid, 0, wx.EXPAND | wx.ALL, 20)
        
        # Position hint
        hint = wx.StaticText(
            panel,
            label="Position: (0,0) = top-left of block.\n"
                  f"Block size: {self.board.block_width} x {self.board.block_height} mm"
        )
        hint.SetForegroundColour(wx.Colour(100, 100, 100))
        main_sizer.Add(hint, 0, wx.LEFT | wx.RIGHT, 20)
        
        main_sizer.AddStretchSpacer()
        
        # Buttons
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_sizer.AddStretchSpacer()
        btn_ok = wx.Button(panel, wx.ID_OK)
        btn_cancel = wx.Button(panel, wx.ID_CANCEL)
        btn_ok.Bind(wx.EVT_BUTTON, self.on_ok)
        btn_sizer.Add(btn_ok, 0, wx.ALL, 5)
        btn_sizer.Add(btn_cancel, 0, wx.ALL, 5)
        main_sizer.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 10)
        
        panel.SetSizer(main_sizer)
    
    def on_ok(self, event):
        name = self.txt_name.GetValue().strip()
        if not name:
            wx.MessageBox("Please enter a port name.", "Error", wx.OK | wx.ICON_ERROR)
            return
        
        self.port_name = name
        self.port_info = {
            'direction': self.combo_dir.GetValue(),
            'net': self.txt_net.GetValue().strip(),
            'x_offset': self.spin_x.GetValue(),
            'y_offset': self.spin_y.GetValue(),
        }
        
        self.EndModal(wx.ID_OK)


class ConnectionEditorDialog(wx.Dialog):
    """Dialog to manage inter-board connections."""
    
    def __init__(self, parent, project_mgr: ProjectManager):
        super().__init__(parent, title="Inter-Board Connections", size=(700, 500))
        
        self.project_mgr = project_mgr
        self.connections = list(project_mgr.project.connections)
        
        self.init_ui()
        self.refresh_list()
    
    def init_ui(self):
        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        info = wx.StaticText(
            panel,
            label="Define semantic connections between board ports.\n"
                  "These are drawn as lines in the root PCB block diagram."
        )
        main_sizer.Add(info, 0, wx.ALL, 10)
        
        # Connection list
        self.list_ctrl = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.list_ctrl.InsertColumn(0, "From Board", width=120)
        self.list_ctrl.InsertColumn(1, "From Port", width=100)
        self.list_ctrl.InsertColumn(2, "→", width=30)
        self.list_ctrl.InsertColumn(3, "To Board", width=120)
        self.list_ctrl.InsertColumn(4, "To Port", width=100)
        self.list_ctrl.InsertColumn(5, "Net", width=120)
        main_sizer.Add(self.list_ctrl, 1, wx.EXPAND | wx.ALL, 10)
        
        # Buttons
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_add = wx.Button(panel, label="Add Connection")
        self.btn_remove = wx.Button(panel, label="Remove")
        
        self.btn_add.Bind(wx.EVT_BUTTON, self.on_add)
        self.btn_remove.Bind(wx.EVT_BUTTON, self.on_remove)
        
        btn_sizer.Add(self.btn_add, 0, wx.ALL, 5)
        btn_sizer.Add(self.btn_remove, 0, wx.ALL, 5)
        main_sizer.Add(btn_sizer, 0, wx.ALL, 5)
        
        # Dialog buttons
        dialog_sizer = wx.BoxSizer(wx.HORIZONTAL)
        dialog_sizer.AddStretchSpacer()
        btn_ok = wx.Button(panel, wx.ID_OK)
        btn_cancel = wx.Button(panel, wx.ID_CANCEL)
        btn_ok.Bind(wx.EVT_BUTTON, self.on_ok)
        dialog_sizer.Add(btn_ok, 0, wx.ALL, 5)
        dialog_sizer.Add(btn_cancel, 0, wx.ALL, 5)
        main_sizer.Add(dialog_sizer, 0, wx.EXPAND | wx.ALL, 10)
        
        panel.SetSizer(main_sizer)
    
    def refresh_list(self):
        self.list_ctrl.DeleteAllItems()
        for conn in self.connections:
            idx = self.list_ctrl.InsertItem(self.list_ctrl.GetItemCount(), conn.from_board)
            self.list_ctrl.SetItem(idx, 1, conn.from_port)
            self.list_ctrl.SetItem(idx, 2, "→")
            self.list_ctrl.SetItem(idx, 3, conn.to_board)
            self.list_ctrl.SetItem(idx, 4, conn.to_port)
            self.list_ctrl.SetItem(idx, 5, conn.net_name)
    
    def on_add(self, event):
        boards = self.project_mgr.project.boards
        if len(boards) < 2:
            wx.MessageBox("Need at least 2 boards.", "Error", wx.OK | wx.ICON_WARNING)
            return
        
        # Simple add dialog
        dlg = NewConnectionDialog(self, self.project_mgr)
        if dlg.ShowModal() == wx.ID_OK and dlg.connection:
            self.connections.append(dlg.connection)
            self.refresh_list()
        dlg.Destroy()
    
    def on_remove(self, event):
        idx = self.list_ctrl.GetFirstSelected()
        if idx >= 0:
            del self.connections[idx]
            self.refresh_list()
    
    def on_ok(self, event):
        self.project_mgr.project.connections = self.connections
        self.project_mgr.save()
        self.EndModal(wx.ID_OK)


class NewConnectionDialog(wx.Dialog):
    """Dialog to add a new connection."""
    
    def __init__(self, parent, project_mgr: ProjectManager):
        super().__init__(parent, title="New Connection", size=(500, 350))
        
        self.project_mgr = project_mgr
        self.connection: Optional[InterBoardConnection] = None
        
        self.init_ui()
    
    def init_ui(self):
        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        
        boards = list(self.project_mgr.project.boards.keys())
        
        # From section
        from_box = wx.StaticBox(panel, label="From")
        from_sizer = wx.StaticBoxSizer(from_box, wx.HORIZONTAL)
        
        from_sizer.Add(wx.StaticText(panel, label="Board:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        self.from_board = wx.ComboBox(panel, choices=boards, style=wx.CB_READONLY)
        self.from_board.Bind(wx.EVT_COMBOBOX, self.on_from_board_changed)
        from_sizer.Add(self.from_board, 1, wx.ALL, 5)
        
        from_sizer.Add(wx.StaticText(panel, label="Port:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        self.from_port = wx.ComboBox(panel, choices=[], style=wx.CB_READONLY)
        from_sizer.Add(self.from_port, 1, wx.ALL, 5)
        
        main_sizer.Add(from_sizer, 0, wx.EXPAND | wx.ALL, 10)
        
        # Arrow
        arrow = wx.StaticText(panel, label="↓ connects to ↓")
        arrow.SetFont(wx.Font(12, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        main_sizer.Add(arrow, 0, wx.ALIGN_CENTER | wx.ALL, 5)
        
        # To section
        to_box = wx.StaticBox(panel, label="To")
        to_sizer = wx.StaticBoxSizer(to_box, wx.HORIZONTAL)
        
        to_sizer.Add(wx.StaticText(panel, label="Board:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        self.to_board = wx.ComboBox(panel, choices=boards, style=wx.CB_READONLY)
        self.to_board.Bind(wx.EVT_COMBOBOX, self.on_to_board_changed)
        to_sizer.Add(self.to_board, 1, wx.ALL, 5)
        
        to_sizer.Add(wx.StaticText(panel, label="Port:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        self.to_port = wx.ComboBox(panel, choices=[], style=wx.CB_READONLY)
        to_sizer.Add(self.to_port, 1, wx.ALL, 5)
        
        main_sizer.Add(to_sizer, 0, wx.EXPAND | wx.ALL, 10)
        
        # Net name
        net_sizer = wx.BoxSizer(wx.HORIZONTAL)
        net_sizer.Add(wx.StaticText(panel, label="Net/Signal Name:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        self.txt_net = wx.TextCtrl(panel)
        net_sizer.Add(self.txt_net, 1, wx.ALL, 5)
        main_sizer.Add(net_sizer, 0, wx.EXPAND | wx.ALL, 10)
        
        main_sizer.AddStretchSpacer()
        
        # Buttons
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_sizer.AddStretchSpacer()
        btn_ok = wx.Button(panel, wx.ID_OK)
        btn_cancel = wx.Button(panel, wx.ID_CANCEL)
        btn_ok.Bind(wx.EVT_BUTTON, self.on_ok)
        btn_sizer.Add(btn_ok, 0, wx.ALL, 5)
        btn_sizer.Add(btn_cancel, 0, wx.ALL, 5)
        main_sizer.Add(btn_sizer, 0, wx.EXPAND | wx.ALL, 10)
        
        panel.SetSizer(main_sizer)
    
    def on_from_board_changed(self, event):
        board_name = self.from_board.GetValue()
        board = self.project_mgr.project.boards.get(board_name)
        if board:
            self.from_port.Set(list(board.ports.keys()))
            if board.ports:
                self.from_port.SetSelection(0)
    
    def on_to_board_changed(self, event):
        board_name = self.to_board.GetValue()
        board = self.project_mgr.project.boards.get(board_name)
        if board:
            self.to_port.Set(list(board.ports.keys()))
            if board.ports:
                self.to_port.SetSelection(0)
    
    def on_ok(self, event):
        if not all([self.from_board.GetValue(), self.from_port.GetValue(),
                    self.to_board.GetValue(), self.to_port.GetValue()]):
            wx.MessageBox("Please fill all fields.", "Error", wx.OK | wx.ICON_ERROR)
            return
        
        import uuid
        self.connection = InterBoardConnection(
            id=str(uuid.uuid4())[:8],
            from_board=self.from_board.GetValue(),
            from_port=self.from_port.GetValue(),
            to_board=self.to_board.GetValue(),
            to_port=self.to_port.GetValue(),
            net_name=self.txt_net.GetValue().strip()
        )
        
        self.EndModal(wx.ID_OK)


class MainDialog(wx.Dialog):
    """Main plugin dialog."""
    
    def __init__(self, parent, board: pcbnew.BOARD):
        super().__init__(
            parent,
            title="Multi-Board PCB Manager v4",
            size=(850, 650),
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
        
        # Project info
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
        
        # Sub-boards list
        list_label = wx.StaticText(self, label="Sub-Board PCBs:")
        main_sizer.Add(list_label, 0, wx.LEFT | wx.TOP, 10)
        
        self.board_list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.board_list.InsertColumn(0, "Board Name", width=150)
        self.board_list.InsertColumn(1, "PCB File", width=200)
        self.board_list.InsertColumn(2, "Layers", width=60)
        self.board_list.InsertColumn(3, "Ports", width=60)
        self.board_list.InsertColumn(4, "Description", width=200)
        main_sizer.Add(self.board_list, 1, wx.EXPAND | wx.ALL, 10)
        
        # Board buttons
        board_btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        self.btn_new = wx.Button(self, label="New Sub-Board")
        self.btn_edit_ports = wx.Button(self, label="Edit Ports")
        self.btn_remove = wx.Button(self, label="Remove")
        self.btn_open = wx.Button(self, label="Open PCB")
        
        board_btn_sizer.Add(self.btn_new, 0, wx.ALL, 3)
        board_btn_sizer.Add(self.btn_edit_ports, 0, wx.ALL, 3)
        board_btn_sizer.Add(self.btn_remove, 0, wx.ALL, 3)
        board_btn_sizer.AddStretchSpacer()
        board_btn_sizer.Add(self.btn_open, 0, wx.ALL, 3)
        
        main_sizer.Add(board_btn_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
        
        # Connection and diagram section
        conn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        self.btn_connections = wx.Button(self, label="Edit Inter-Board Connections")
        self.btn_update_diagram = wx.Button(self, label="Update Block Diagram in Root PCB")
        self.btn_cleanup = wx.Button(self, label="Cleanup Auto-Projects")
        
        conn_sizer.Add(self.btn_connections, 0, wx.ALL, 5)
        conn_sizer.Add(self.btn_update_diagram, 0, wx.ALL, 5)
        conn_sizer.AddStretchSpacer()
        conn_sizer.Add(self.btn_cleanup, 0, wx.ALL, 5)
        
        main_sizer.Add(conn_sizer, 0, wx.EXPAND | wx.ALL, 5)
        
        # Bottom buttons
        bottom_sizer = wx.BoxSizer(wx.HORIZONTAL)
        bottom_sizer.AddStretchSpacer()
        btn_close = wx.Button(self, wx.ID_CLOSE)
        bottom_sizer.Add(btn_close, 0, wx.ALL, 10)
        
        main_sizer.Add(bottom_sizer, 0, wx.EXPAND)
        
        self.SetSizer(main_sizer)
    
    def bind_events(self):
        self.btn_new.Bind(wx.EVT_BUTTON, self.on_new_board)
        self.btn_edit_ports.Bind(wx.EVT_BUTTON, self.on_edit_ports)
        self.btn_remove.Bind(wx.EVT_BUTTON, self.on_remove_board)
        self.btn_open.Bind(wx.EVT_BUTTON, self.on_open_pcb)
        self.btn_connections.Bind(wx.EVT_BUTTON, self.on_connections)
        self.btn_update_diagram.Bind(wx.EVT_BUTTON, self.on_update_diagram)
        self.btn_cleanup.Bind(wx.EVT_BUTTON, self.on_cleanup)
        self.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE), id=wx.ID_CLOSE)
    
    def refresh_all(self):
        self.board_list.DeleteAllItems()
        
        for name, board in self.project_mgr.project.boards.items():
            idx = self.board_list.InsertItem(self.board_list.GetItemCount(), name)
            self.board_list.SetItem(idx, 1, board.pcb_filename)
            self.board_list.SetItem(idx, 2, str(board.layers))
            self.board_list.SetItem(idx, 3, str(len(board.ports)))
            self.board_list.SetItem(idx, 4, board.description)
    
    def get_selected_board(self) -> Optional[str]:
        idx = self.board_list.GetFirstSelected()
        if idx >= 0:
            return self.board_list.GetItemText(idx)
        return None
    
    def on_new_board(self, event):
        dlg = NewSubBoardDialog(self, self.project_mgr)
        if dlg.ShowModal() == wx.ID_OK and dlg.board:
            success, msg = self.project_mgr.create_sub_board(dlg.board)
            if success:
                wx.MessageBox(msg, "Success", wx.OK | wx.ICON_INFORMATION)
                self.refresh_all()
            else:
                wx.MessageBox(msg, "Error", wx.OK | wx.ICON_ERROR)
        dlg.Destroy()
    
    def on_edit_ports(self, event):
        name = self.get_selected_board()
        if not name:
            wx.MessageBox("Please select a board.", "No Selection", wx.OK | wx.ICON_WARNING)
            return
        
        board = self.project_mgr.project.boards.get(name)
        if board:
            dlg = PortEditorDialog(self, board)
            if dlg.ShowModal() == wx.ID_OK:
                self.project_mgr.save()
                self.refresh_all()
            dlg.Destroy()
    
    def on_remove_board(self, event):
        name = self.get_selected_board()
        if not name:
            wx.MessageBox("Please select a board.", "No Selection", wx.OK | wx.ICON_WARNING)
            return
        
        if wx.MessageBox(
            f"Remove '{name}' from project?\n\n"
            "Note: PCB and schematic link files will NOT be deleted.",
            "Confirm",
            wx.YES_NO | wx.ICON_QUESTION
        ) == wx.YES:
            # Remove connections involving this board
            self.project_mgr.project.connections = [
                c for c in self.project_mgr.project.connections
                if c.from_board != name and c.to_board != name
            ]
            
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
        
        import subprocess
        try:
            subprocess.Popen(["pcbnew", str(pcb_path)])
        except Exception as e:
            wx.MessageBox(f"Failed to open pcbnew: {e}", "Error", wx.OK | wx.ICON_ERROR)
    
    def on_connections(self, event):
        dlg = ConnectionEditorDialog(self, self.project_mgr)
        dlg.ShowModal()
        dlg.Destroy()
    
    def on_update_diagram(self, event):
        success, msg = self.project_mgr.update_root_pcb_block_diagram()
        if success:
            wx.MessageBox(
                f"Block diagram updated!\n\n{msg}\n\n"
                "View User.1 layer in the root PCB to see the diagram.",
                "Success",
                wx.OK | wx.ICON_INFORMATION
            )
        else:
            wx.MessageBox(msg, "Error", wx.OK | wx.ICON_ERROR)
    
    def on_cleanup(self, event):
        cleaned = self.project_mgr.cleanup_auto_projects()
        if cleaned:
            wx.MessageBox(
                f"Removed {len(cleaned)} auto-created project files:\n\n" + "\n".join(cleaned),
                "Cleanup Complete",
                wx.OK | wx.ICON_INFORMATION
            )
        else:
            wx.MessageBox("No auto-created project files found.", "Cleanup", wx.OK | wx.ICON_INFORMATION)


# ============================================================================
# Plugin Registration
# ============================================================================

class MultiBoardPlugin(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "Multi-Board Manager"
        self.category = "Project Management"
        self.description = (
            "Hierarchical PCB management - like schematic sheets but for PCBs. "
            "Create sub-board PCBs that use the same root schematic."
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

# ```

# ## Key Features of v4

# **1. NO new KiCad projects created**
# - Only creates `.kicad_pcb` files
# - Creates schematic **symlinks** (e.g., `main_board.kicad_sch` → `SWIFT_board.kicad_sch`)
# - This makes "Update PCB from Schematic" work with your root schematic

# **2. Cleanup button**
# - If KiCad auto-creates `.kicad_pro` files when you open a sub-PCB, click "Cleanup Auto-Projects" to delete them

# **3. Block diagram in root PCB**
# - Click "Update Block Diagram in Root PCB"
# - Draws board rectangles and connection lines on **User.1 layer**
# - Define ports on each board (position on edges)
# - Draw connections between ports

# **4. Workflow**:
# ```
# 1. Open root PCB (SWIFT_board.kicad_pcb)
# 2. Open Multi-Board Manager
# 3. Click "New Sub-Board" → creates power_board.kicad_pcb + symlink
# 4. Add ports to each board (Edit Ports)
# 5. Add inter-board connections
# 6. Click "Update Block Diagram" → draws diagram on User.1
# 7. Open sub-PCB → "Update from Schematic" → pulls from root schematic
# 8. Delete components you don't need on that board
# 9. Generate Gerbers independently for each PCB