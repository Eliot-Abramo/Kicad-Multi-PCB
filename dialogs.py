"""
Multi-Board PCB Manager - Professional UI Components
=====================================================

This module provides all wxPython dialog classes with a professional,
Altium-inspired design language:

- Clean visual hierarchy
- Consistent spacing and alignment  
- Informative tooltips
- Status feedback
- Intuitive workflow

Author: Eliot
License: MIT
"""

import wx
import shutil
import os
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Set, Dict, Any, List, Tuple

from .config import BoardConfig, PortDef
from .manager import MultiBoardManager
from .constants import BOARDS_DIR


# =============================================================================
# Design Constants
# =============================================================================

class Colors:
    """Professional gray theme color palette."""
    BACKGROUND = wx.Colour(245, 246, 247)
    PANEL_BG = wx.Colour(255, 255, 255)
    HEADER_BG = wx.Colour(55, 71, 79)
    HEADER_FG = wx.Colour(255, 255, 255)
    ACCENT = wx.Colour(33, 150, 243)
    ACCENT_HOVER = wx.Colour(25, 118, 210)
    BORDER = wx.Colour(218, 220, 224)
    TEXT_PRIMARY = wx.Colour(32, 33, 36)
    TEXT_SECONDARY = wx.Colour(95, 99, 104)
    SUCCESS = wx.Colour(52, 168, 83)
    WARNING = wx.Colour(251, 188, 4)
    ERROR = wx.Colour(234, 67, 53)
    INFO_BG = wx.Colour(232, 245, 253)
    INFO_BORDER = wx.Colour(144, 202, 249)


class Spacing:
    """Consistent spacing values."""
    MARGIN = 16
    PADDING = 12
    GAP = 8
    BUTTON_GAP = 6


# =============================================================================
# Custom Widgets
# =============================================================================

class IconButton(wx.Button):
    """Button with icon prefix using Unicode symbols."""
    
    ICONS = {
        'new': '+',
        'delete': '×',
        'open': '↗',
        'refresh': '↻',
        'ports': '⇌',
        'check': '✓',
        'status': '≡',
        'settings': '⚙',
        'info': 'i',
        'folder': '▤',
    }
    
    def __init__(self, parent, label, icon=None, **kwargs):
        if icon and icon in self.ICONS:
            label = f"{self.ICONS[icon]} {label}"
        super().__init__(parent, label=label, **kwargs)


class InfoPanel(wx.Panel):
    """Informational banner with icon and message."""
    
    def __init__(self, parent, message, style='info'):
        super().__init__(parent)
        
        if style == 'info':
            bg = Colors.INFO_BG
            icon = 'ⓘ'
        elif style == 'warning':
            bg = wx.Colour(255, 248, 225)
            icon = '⚠'
        else:
            bg = Colors.INFO_BG
            icon = 'ⓘ'
        
        self.SetBackgroundColour(bg)
        
        sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        icon_text = wx.StaticText(self, label=icon)
        icon_text.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT, 
                                   wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        sizer.Add(icon_text, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 10)
        
        msg_text = wx.StaticText(self, label=message)
        msg_text.SetForegroundColour(Colors.TEXT_PRIMARY)
        msg_text.Wrap(500)
        sizer.Add(msg_text, 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 10)
        
        self.SetSizer(sizer)


class SectionHeader(wx.Panel):
    """Section header with title and optional subtitle."""
    
    def __init__(self, parent, title, subtitle=None):
        super().__init__(parent)
        self.SetBackgroundColour(Colors.PANEL_BG)
        
        sizer = wx.BoxSizer(wx.VERTICAL)
        
        title_text = wx.StaticText(self, label=title)
        title_text.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT,
                                    wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        title_text.SetForegroundColour(Colors.TEXT_PRIMARY)
        sizer.Add(title_text, 0, wx.BOTTOM, 2)
        
        if subtitle:
            sub_text = wx.StaticText(self, label=subtitle)
            sub_text.SetFont(wx.Font(9, wx.FONTFAMILY_DEFAULT,
                                      wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
            sub_text.SetForegroundColour(Colors.TEXT_SECONDARY)
            sizer.Add(sub_text, 0)
        
        self.SetSizer(sizer)


class StatusBar(wx.Panel):
    """Status bar showing sync status."""
    
    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundColour(Colors.BACKGROUND)
        
        sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        self.status_icon = wx.StaticText(self, label="●")
        self.status_icon.SetForegroundColour(Colors.SUCCESS)
        sizer.Add(self.status_icon, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        
        self.status_text = wx.StaticText(self, label="Ready")
        self.status_text.SetForegroundColour(Colors.TEXT_SECONDARY)
        self.status_text.SetFont(wx.Font(9, wx.FONTFAMILY_DEFAULT,
                                          wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        sizer.Add(self.status_text, 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 4)
        
        self.SetSizer(sizer)
    
    def set_status(self, message, status='ok'):
        """Update status display."""
        colors = {
            'ok': Colors.SUCCESS,
            'warning': Colors.WARNING,
            'error': Colors.ERROR,
            'working': Colors.ACCENT,
        }
        self.status_icon.SetForegroundColour(colors.get(status, Colors.SUCCESS))
        self.status_text.SetLabel(message)
        self.Refresh()


# =============================================================================
# Progress Dialog
# =============================================================================

class ProgressDialog(wx.Dialog):
    """Professional progress dialog."""
    
    def __init__(self, parent, title: str = "Working..."):
        super().__init__(
            parent,
            title=title,
            size=(450, 130),
            style=wx.CAPTION | wx.STAY_ON_TOP
        )
        
        self.SetBackgroundColour(Colors.PANEL_BG)
        
        panel = wx.Panel(self)
        panel.SetBackgroundColour(Colors.PANEL_BG)
        sizer = wx.BoxSizer(wx.VERTICAL)
        
        self.label = wx.StaticText(panel, label="Initializing...")
        self.label.SetFont(wx.Font(10, wx.FONTFAMILY_DEFAULT,
                                    wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        self.label.SetForegroundColour(Colors.TEXT_PRIMARY)
        sizer.Add(self.label, 0, wx.ALL | wx.EXPAND, Spacing.MARGIN)
        
        self.gauge = wx.Gauge(panel, range=100, size=(-1, 6))
        sizer.Add(self.gauge, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, 
                  Spacing.MARGIN)
        
        self.percent_label = wx.StaticText(panel, label="0%")
        self.percent_label.SetForegroundColour(Colors.TEXT_SECONDARY)
        sizer.Add(self.percent_label, 0, wx.LEFT | wx.BOTTOM, Spacing.MARGIN)
        
        panel.SetSizer(sizer)
        self.Centre()
    
    def update(self, percent: int, message: str):
        """Update progress display."""
        self.gauge.SetValue(min(percent, 100))
        self.label.SetLabel(message)
        self.percent_label.SetLabel(f"{percent}%")
        wx.Yield()


# =============================================================================
# Port Dialogs
# =============================================================================

class PortEditDialog(wx.Dialog):
    """Port configuration dialog."""
    
    def __init__(self, parent, port: PortDef, existing_names: Set[str] = None):
        super().__init__(
            parent,
            title="Port Configuration",
            size=(400, 300),
            style=wx.DEFAULT_DIALOG_STYLE
        )
        self.port = port
        self.existing_names = existing_names or set()
        self.original_name = port.name
        
        self.SetBackgroundColour(Colors.PANEL_BG)
        self._build_ui()
    
    def _build_ui(self):
        panel = wx.Panel(self)
        panel.SetBackgroundColour(Colors.PANEL_BG)
        main = wx.BoxSizer(wx.VERTICAL)
        
        header = SectionHeader(panel, "Port Settings",
                                "Define an inter-board connection point")
        main.Add(header, 0, wx.ALL | wx.EXPAND, Spacing.MARGIN)
        
        form = wx.FlexGridSizer(4, 2, Spacing.GAP, Spacing.MARGIN)
        form.AddGrowableCol(1)
        
        # Name
        form.Add(self._label(panel, "Name"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_name = wx.TextCtrl(panel, value=self.port.name)
        self.txt_name.SetToolTip("Unique identifier (e.g., USB_DP, POWER_IN)")
        form.Add(self.txt_name, 1, wx.EXPAND)
        
        # Net
        form.Add(self._label(panel, "Net"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_net = wx.TextCtrl(panel, value=self.port.net)
        self.txt_net.SetToolTip("Net name this port connects to")
        form.Add(self.txt_net, 1, wx.EXPAND)
        
        # Side
        form.Add(self._label(panel, "Edge"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.choice_side = wx.Choice(panel, choices=["Left", "Right", "Top", "Bottom"])
        self.choice_side.SetStringSelection(self.port.side.capitalize())
        self.choice_side.SetToolTip("Board edge where this port appears")
        form.Add(self.choice_side, 1, wx.EXPAND)
        
        # Position
        form.Add(self._label(panel, "Position"), 0, wx.ALIGN_CENTER_VERTICAL)
        pos_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.slider_pos = wx.Slider(
            panel, value=int(self.port.position * 100),
            minValue=0, maxValue=100, size=(140, -1)
        )
        self.slider_pos.SetToolTip("Position along edge (0% = start, 100% = end)")
        pos_sizer.Add(self.slider_pos, 1, wx.EXPAND | wx.RIGHT, Spacing.GAP)
        self.pos_label = wx.StaticText(panel, label=f"{self.port.position:.0%}")
        self.pos_label.SetMinSize((40, -1))
        pos_sizer.Add(self.pos_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(pos_sizer, 1, wx.EXPAND)
        
        self.slider_pos.Bind(wx.EVT_SLIDER, self._on_slider)
        
        main.Add(form, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, Spacing.MARGIN)
        main.AddStretchSpacer()
        
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_sizer.AddStretchSpacer()
        btn_cancel = wx.Button(panel, wx.ID_CANCEL, "Cancel")
        btn_sizer.Add(btn_cancel, 0, wx.RIGHT, Spacing.BUTTON_GAP)
        btn_ok = wx.Button(panel, wx.ID_OK, "Save")
        btn_ok.SetDefault()
        btn_ok.Bind(wx.EVT_BUTTON, self._on_ok)
        btn_sizer.Add(btn_ok, 0)
        main.Add(btn_sizer, 0, wx.ALL | wx.EXPAND, Spacing.MARGIN)
        
        panel.SetSizer(main)
    
    def _label(self, parent, text):
        lbl = wx.StaticText(parent, label=text)
        lbl.SetForegroundColour(Colors.TEXT_SECONDARY)
        return lbl
    
    def _on_slider(self, event):
        self.pos_label.SetLabel(f"{self.slider_pos.GetValue()}%")
    
    def _on_ok(self, event):
        name = self.txt_name.GetValue().strip()
        
        if not name:
            wx.MessageBox("Port name is required.", "Validation Error", wx.ICON_WARNING)
            self.txt_name.SetFocus()
            return
        
        if name != self.original_name and name in self.existing_names:
            wx.MessageBox(f"Port '{name}' already exists.", "Validation Error", wx.ICON_WARNING)
            self.txt_name.SetFocus()
            return
        
        self.port = PortDef(
            name=name,
            net=self.txt_net.GetValue().strip(),
            side=self.choice_side.GetStringSelection().lower(),
            position=self.slider_pos.GetValue() / 100.0,
        )
        self.EndModal(wx.ID_OK)


class PortDialog(wx.Dialog):
    """Port manager dialog."""
    
    def __init__(self, parent, board: BoardConfig):
        super().__init__(
            parent,
            title=f"Manage Ports — {board.name}",
            size=(520, 400),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER
        )
        self.board = board
        self.ports = dict(board.ports)
        
        self.SetBackgroundColour(Colors.PANEL_BG)
        self._build_ui()
        self._refresh_list()
    
    def _build_ui(self):
        panel = wx.Panel(self)
        panel.SetBackgroundColour(Colors.PANEL_BG)
        main = wx.BoxSizer(wx.VERTICAL)
        
        header = SectionHeader(panel, "Inter-Board Ports",
                                "Define connection points between boards")
        main.Add(header, 0, wx.ALL | wx.EXPAND, Spacing.MARGIN)
        
        self.list = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SIMPLE)
        self.list.InsertColumn(0, "Port Name", width=130)
        self.list.InsertColumn(1, "Connected Net", width=130)
        self.list.InsertColumn(2, "Edge", width=70)
        self.list.InsertColumn(3, "Position", width=70)
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, lambda e: self._on_edit(e))
        main.Add(self.list, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, Spacing.MARGIN)
        
        action_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_add = IconButton(panel, "Add Port", icon='new', size=(90, -1))
        btn_add.SetToolTip("Create a new port")
        btn_add.Bind(wx.EVT_BUTTON, self._on_add)
        action_sizer.Add(btn_add, 0, wx.RIGHT, Spacing.BUTTON_GAP)
        
        btn_edit = wx.Button(panel, label="Edit", size=(60, -1))
        btn_edit.Bind(wx.EVT_BUTTON, self._on_edit)
        action_sizer.Add(btn_edit, 0, wx.RIGHT, Spacing.BUTTON_GAP)
        
        btn_remove = IconButton(panel, "Remove", icon='delete', size=(80, -1))
        btn_remove.Bind(wx.EVT_BUTTON, self._on_remove)
        action_sizer.Add(btn_remove, 0)
        main.Add(action_sizer, 0, wx.ALL, Spacing.MARGIN)
        
        main.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, Spacing.MARGIN)
        
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_sizer.AddStretchSpacer()
        btn_cancel = wx.Button(panel, wx.ID_CANCEL, "Cancel")
        btn_sizer.Add(btn_cancel, 0, wx.RIGHT, Spacing.BUTTON_GAP)
        btn_ok = wx.Button(panel, wx.ID_OK, "Apply")
        btn_ok.SetDefault()
        btn_ok.Bind(wx.EVT_BUTTON, self._on_ok)
        btn_sizer.Add(btn_ok, 0)
        main.Add(btn_sizer, 0, wx.ALL | wx.EXPAND, Spacing.MARGIN)
        
        panel.SetSizer(main)
    
    def _refresh_list(self):
        self.list.DeleteAllItems()
        for name, port in sorted(self.ports.items()):
            idx = self.list.InsertItem(self.list.GetItemCount(), name)
            self.list.SetItem(idx, 1, port.net or "—")
            self.list.SetItem(idx, 2, port.side.capitalize())
            self.list.SetItem(idx, 3, f"{port.position:.0%}")
    
    def _get_selected_name(self) -> Optional[str]:
        idx = self.list.GetFirstSelected()
        return self.list.GetItemText(idx) if idx >= 0 else None
    
    def _on_add(self, event):
        dlg = PortEditDialog(self, PortDef(name=""), existing_names=set(self.ports.keys()))
        if dlg.ShowModal() == wx.ID_OK and dlg.port.name:
            self.ports[dlg.port.name] = dlg.port
            self._refresh_list()
        dlg.Destroy()
    
    def _on_edit(self, event):
        name = self._get_selected_name()
        if not name:
            return
        port = self.ports.get(name)
        if not port:
            return
        
        other_names = set(self.ports.keys()) - {name}
        dlg = PortEditDialog(self, port, existing_names=other_names)
        if dlg.ShowModal() == wx.ID_OK:
            del self.ports[name]
            self.ports[dlg.port.name] = dlg.port
            self._refresh_list()
        dlg.Destroy()
    
    def _on_remove(self, event):
        name = self._get_selected_name()
        if not name:
            return
        if wx.MessageBox(f"Remove port '{name}'?", "Confirm",
                         wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION) == wx.YES:
            del self.ports[name]
            self._refresh_list()
    
    def _on_ok(self, event):
        self.board.ports = self.ports
        self.EndModal(wx.ID_OK)


# =============================================================================
# New Board Dialog
# =============================================================================

class NewBoardDialog(wx.Dialog):
    """New board creation dialog."""
    
    def __init__(self, parent, existing_names: Set[str]):
        super().__init__(parent, title="Create New Board", size=(420, 260),
                         style=wx.DEFAULT_DIALOG_STYLE)
        self.existing = existing_names
        self.result_name = ""
        self.result_desc = ""
        
        self.SetBackgroundColour(Colors.PANEL_BG)
        self._build_ui()
    
    def _build_ui(self):
        panel = wx.Panel(self)
        panel.SetBackgroundColour(Colors.PANEL_BG)
        main = wx.BoxSizer(wx.VERTICAL)
        
        header = SectionHeader(panel, "New Sub-Board",
                                "Create a new PCB sharing this schematic")
        main.Add(header, 0, wx.ALL | wx.EXPAND, Spacing.MARGIN)
        
        form = wx.FlexGridSizer(2, 2, Spacing.GAP, Spacing.MARGIN)
        form.AddGrowableCol(1)
        
        lbl_name = wx.StaticText(panel, label="Board Name")
        lbl_name.SetForegroundColour(Colors.TEXT_SECONDARY)
        form.Add(lbl_name, 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_name = wx.TextCtrl(panel)
        self.txt_name.SetToolTip("Unique name (e.g., PowerSupply, MainBoard)")
        form.Add(self.txt_name, 1, wx.EXPAND)
        
        lbl_desc = wx.StaticText(panel, label="Description")
        lbl_desc.SetForegroundColour(Colors.TEXT_SECONDARY)
        form.Add(lbl_desc, 0, wx.ALIGN_TOP)
        self.txt_desc = wx.TextCtrl(panel, size=(-1, 50), style=wx.TE_MULTILINE)
        self.txt_desc.SetToolTip("Optional description")
        form.Add(self.txt_desc, 1, wx.EXPAND)
        
        main.Add(form, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, Spacing.MARGIN)
        
        info = InfoPanel(panel,
            "The new board shares the project schematic. "
            "Use Update to assign components.", style='info')
        main.Add(info, 0, wx.ALL | wx.EXPAND, Spacing.MARGIN)
        
        main.AddStretchSpacer()
        
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_sizer.AddStretchSpacer()
        btn_cancel = wx.Button(panel, wx.ID_CANCEL, "Cancel")
        btn_sizer.Add(btn_cancel, 0, wx.RIGHT, Spacing.BUTTON_GAP)
        btn_ok = wx.Button(panel, wx.ID_OK, "Create Board")
        btn_ok.SetDefault()
        btn_ok.Bind(wx.EVT_BUTTON, self._on_ok)
        btn_sizer.Add(btn_ok, 0)
        main.Add(btn_sizer, 0, wx.ALL | wx.EXPAND, Spacing.MARGIN)
        
        panel.SetSizer(main)
        self.txt_name.SetFocus()
    
    def _on_ok(self, event):
        name = self.txt_name.GetValue().strip()
        
        if not name:
            wx.MessageBox("Enter a board name.", "Validation Error", wx.ICON_WARNING)
            self.txt_name.SetFocus()
            return
        
        if name in self.existing:
            wx.MessageBox(f"Board '{name}' already exists.", "Validation Error", wx.ICON_WARNING)
            self.txt_name.SetFocus()
            return
        
        safe_name = "".join(c if c.isalnum() or c in "_-" else "" for c in name)
        if not safe_name:
            wx.MessageBox("Name must contain letters or numbers.", "Validation Error", wx.ICON_WARNING)
            self.txt_name.SetFocus()
            return
        
        self.result_name = name
        self.result_desc = self.txt_desc.GetValue().strip()
        self.EndModal(wx.ID_OK)


# =============================================================================
# Report Dialogs
# =============================================================================

class ConnectivityReportDialog(wx.Dialog):
    """Connectivity report dialog."""
    
    def __init__(self, parent, report: Dict[str, Any]):
        super().__init__(parent, title="Connectivity Report", size=(600, 480),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.report = report
        self.SetBackgroundColour(Colors.PANEL_BG)
        self._build_ui()
    
    def _build_ui(self):
        panel = wx.Panel(self)
        panel.SetBackgroundColour(Colors.PANEL_BG)
        main = wx.BoxSizer(wx.VERTICAL)
        
        errors = self.report.get("errors", [])
        boards = self.report.get("boards", {})
        total_violations = sum(b.get("violations", 0) for b in boards.values())
        
        if total_violations == 0 and len(errors) == 0:
            status_color, status_icon = Colors.SUCCESS, "✓"
            status_text = "All checks passed"
        elif len(errors) > 0:
            status_color, status_icon = Colors.ERROR, "✕"
            status_text = f"{len(errors)} error(s)"
        else:
            status_color, status_icon = Colors.WARNING, "⚠"
            status_text = f"{total_violations} violation(s)"
        
        header_sizer = wx.BoxSizer(wx.HORIZONTAL)
        icon = wx.StaticText(panel, label=status_icon)
        icon.SetFont(wx.Font(16, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        icon.SetForegroundColour(status_color)
        header_sizer.Add(icon, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 8)
        
        title = wx.StaticText(panel, label=status_text)
        title.SetFont(wx.Font(12, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        header_sizer.Add(title, 0, wx.ALIGN_CENTER_VERTICAL)
        main.Add(header_sizer, 0, wx.ALL, Spacing.MARGIN)
        
        stats = wx.StaticText(panel,
            label=f"Boards: {len(boards)}  •  Violations: {total_violations}  •  Errors: {len(errors)}")
        stats.SetForegroundColour(Colors.TEXT_SECONDARY)
        main.Add(stats, 0, wx.LEFT | wx.RIGHT, Spacing.MARGIN)
        main.AddSpacer(Spacing.MARGIN)
        
        self.text = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.BORDER_SIMPLE)
        self.text.SetFont(wx.Font(9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL))
        self.text.SetBackgroundColour(wx.Colour(250, 250, 250))
        
        lines = []
        for board_name, data in boards.items():
            count = data.get('violations', 0)
            icon = "✓" if count == 0 else "⚠"
            lines.append(f"{icon} {board_name}: {count} violation(s)")
            for v in data.get("details", []):
                lines.append(f"    • {v.get('type', 'unknown')}: {v.get('description', '')[:60]}")
            lines.append("")
        
        if errors:
            lines.append("─── Errors ───")
            for e in errors:
                lines.append(f"✕ {e}")
        
        self.text.SetValue("\n".join(lines) if lines else "No issues found.")
        main.Add(self.text, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, Spacing.MARGIN)
        
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_sizer.AddStretchSpacer()
        btn_close = wx.Button(panel, wx.ID_CLOSE, "Close")
        btn_close.SetDefault()
        btn_close.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        btn_sizer.Add(btn_close, 0)
        main.Add(btn_sizer, 0, wx.ALL | wx.EXPAND, Spacing.MARGIN)
        
        panel.SetSizer(main)


class StatusDialog(wx.Dialog):
    """Component placement status dialog."""
    
    def __init__(self, parent, manager: MultiBoardManager):
        super().__init__(parent, title="Component Status", size=(540, 420),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.manager = manager
        self.SetBackgroundColour(Colors.PANEL_BG)
        self._build_ui()
        wx.CallAfter(self._load_data)
    
    def _build_ui(self):
        panel = wx.Panel(self)
        panel.SetBackgroundColour(Colors.PANEL_BG)
        main = wx.BoxSizer(wx.VERTICAL)
        
        self.header = SectionHeader(panel, "Component Placement", "Loading...")
        main.Add(self.header, 0, wx.ALL | wx.EXPAND, Spacing.MARGIN)
        
        self.tree = wx.TreeCtrl(panel, style=wx.TR_DEFAULT_STYLE | wx.TR_HIDE_ROOT | wx.BORDER_SIMPLE)
        main.Add(self.tree, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, Spacing.MARGIN)
        
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_sizer.AddStretchSpacer()
        btn_close = wx.Button(panel, wx.ID_CLOSE, "Close")
        btn_close.SetDefault()
        btn_close.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        btn_sizer.Add(btn_close, 0)
        main.Add(btn_sizer, 0, wx.ALL | wx.EXPAND, Spacing.MARGIN)
        
        panel.SetSizer(main)
    
    def _load_data(self):
        placed, unplaced, total = self.manager.get_status()
        
        self.header.DestroyChildren()
        sizer = wx.BoxSizer(wx.VERTICAL)
        title = wx.StaticText(self.header, label="Component Placement")
        title.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        sizer.Add(title, 0, wx.BOTTOM, 4)
        subtitle = wx.StaticText(self.header,
            label=f"Total: {total}  •  Placed: {len(placed)}  •  Unplaced: {len(unplaced)}")
        subtitle.SetForegroundColour(Colors.TEXT_SECONDARY)
        sizer.Add(subtitle, 0)
        self.header.SetSizer(sizer)
        self.header.Layout()
        
        self.tree.DeleteAllItems()
        root = self.tree.AddRoot("Status")
        
        by_board: Dict[str, list] = {}
        for ref, board in placed.items():
            by_board.setdefault(board, []).append(ref)
        
        for board_name in sorted(by_board.keys()):
            refs = sorted(by_board[board_name])
            node = self.tree.AppendItem(root, f"✓ {board_name} ({len(refs)})")
            for ref in refs[:50]:
                self.tree.AppendItem(node, f"  {ref}")
            if len(refs) > 50:
                self.tree.AppendItem(node, f"  ... +{len(refs) - 50} more")
        
        if unplaced:
            node = self.tree.AppendItem(root, f"○ Unplaced ({len(unplaced)})")
            for ref in sorted(unplaced)[:50]:
                self.tree.AppendItem(node, f"  {ref}")
            if len(unplaced) > 50:
                self.tree.AppendItem(node, f"  ... +{len(unplaced) - 50} more")
        
        self.tree.ExpandAll()


# =============================================================================
# Main Dialog
# =============================================================================

class MainDialog(wx.Dialog):
    """
    Primary plugin dialog with professional design.
    """
    
    def __init__(self, parent, pcb_board: "pcbnew.BOARD"):
        super().__init__(parent, title="Multi-Board Manager", size=(760, 540),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        
        pcb_path = pcb_board.GetFileName()
        project_dir = Path(pcb_path).parent if pcb_path else Path.cwd()
        self.manager = MultiBoardManager(project_dir)
        self.executor = ThreadPoolExecutor(max_workers=1)
        
        self.SetBackgroundColour(Colors.BACKGROUND)
        self._build_ui()
        self._refresh_list()
        
        self.Bind(wx.EVT_CLOSE, self._on_close)
    
    def _build_ui(self):
        main = wx.BoxSizer(wx.VERTICAL)
        
        # Header
        header = wx.Panel(self)
        header.SetBackgroundColour(Colors.HEADER_BG)
        header_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        title_sizer = wx.BoxSizer(wx.VERTICAL)
        title = wx.StaticText(header, label="Multi-Board Manager")
        title.SetFont(wx.Font(14, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        title.SetForegroundColour(Colors.HEADER_FG)
        title_sizer.Add(title, 0, wx.BOTTOM, 2)
        
        subtitle = wx.StaticText(header, label=f"Project: {self.manager.project_dir.name}")
        subtitle.SetForegroundColour(wx.Colour(176, 190, 197))
        title_sizer.Add(subtitle, 0)
        header_sizer.Add(title_sizer, 1, wx.ALL, Spacing.MARGIN)
        
        board_count = len(self.manager.config.boards)
        badge = wx.StaticText(header, label=f"{board_count} board(s)")
        badge.SetForegroundColour(wx.Colour(176, 190, 197))
        header_sizer.Add(badge, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, Spacing.MARGIN)
        
        header.SetSizer(header_sizer)
        main.Add(header, 0, wx.EXPAND)
        
        # Content
        content = wx.Panel(self)
        content.SetBackgroundColour(Colors.PANEL_BG)
        content_sizer = wx.BoxSizer(wx.VERTICAL)
        
        info = InfoPanel(content,
            "All boards share the same schematic. Edits anywhere are reflected everywhere.",
            style='info')
        content_sizer.Add(info, 0, wx.ALL | wx.EXPAND, Spacing.MARGIN)
        
        # Board list
        self.list = wx.ListCtrl(content, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SIMPLE)
        self.list.InsertColumn(0, "Board", width=120)
        self.list.InsertColumn(1, "Components", width=85)
        self.list.InsertColumn(2, "Ports", width=55)
        self.list.InsertColumn(3, "Description", width=180)
        self.list.InsertColumn(4, "Path", width=200)
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_open)
        self.list.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_selection_changed)
        self.list.Bind(wx.EVT_LIST_ITEM_DESELECTED, self._on_selection_changed)
        content_sizer.Add(self.list, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, Spacing.MARGIN)
        
        # Buttons
        btn_panel = wx.Panel(content)
        btn_panel.SetBackgroundColour(Colors.PANEL_BG)
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        btn_new = IconButton(btn_panel, "New", icon='new', size=(70, 30))
        btn_new.SetToolTip("Create a new sub-board")
        btn_new.Bind(wx.EVT_BUTTON, self._on_new)
        btn_sizer.Add(btn_new, 0, wx.RIGHT, Spacing.BUTTON_GAP)
        
        self.btn_remove = IconButton(btn_panel, "Remove", icon='delete', size=(85, 30))
        self.btn_remove.SetToolTip("Delete selected board")
        self.btn_remove.Bind(wx.EVT_BUTTON, self._on_remove)
        self.btn_remove.Disable()
        btn_sizer.Add(self.btn_remove, 0, wx.RIGHT, Spacing.MARGIN)
        
        self.btn_open = IconButton(btn_panel, "Open", icon='open', size=(70, 30))
        self.btn_open.SetToolTip("Open board in KiCad")
        self.btn_open.Bind(wx.EVT_BUTTON, self._on_open)
        self.btn_open.Disable()
        btn_sizer.Add(self.btn_open, 0, wx.RIGHT, Spacing.BUTTON_GAP)
        
        self.btn_update = IconButton(btn_panel, "Update", icon='refresh', size=(85, 30))
        self.btn_update.SetToolTip("Sync components from schematic")
        self.btn_update.Bind(wx.EVT_BUTTON, self._on_update)
        self.btn_update.Disable()
        btn_sizer.Add(self.btn_update, 0, wx.RIGHT, Spacing.BUTTON_GAP)
        
        self.btn_ports = IconButton(btn_panel, "Ports", icon='ports', size=(70, 30))
        self.btn_ports.SetToolTip("Configure inter-board ports")
        self.btn_ports.Bind(wx.EVT_BUTTON, self._on_ports)
        self.btn_ports.Disable()
        btn_sizer.Add(self.btn_ports, 0, wx.RIGHT, Spacing.MARGIN)
        
        btn_check = IconButton(btn_panel, "Check All", icon='check', size=(95, 30))
        btn_check.SetToolTip("Run DRC on all boards")
        btn_check.Bind(wx.EVT_BUTTON, self._on_check)
        btn_sizer.Add(btn_check, 0, wx.RIGHT, Spacing.BUTTON_GAP)
        
        btn_status = IconButton(btn_panel, "Status", icon='status', size=(75, 30))
        btn_status.SetToolTip("View component placement")
        btn_status.Bind(wx.EVT_BUTTON, self._on_status)
        btn_sizer.Add(btn_status, 0)
        
        btn_panel.SetSizer(btn_sizer)
        content_sizer.Add(btn_panel, 0, wx.ALL, Spacing.MARGIN)
        
        content.SetSizer(content_sizer)
        main.Add(content, 1, wx.EXPAND)
        
        # Footer
        footer = wx.Panel(self)
        footer.SetBackgroundColour(Colors.BACKGROUND)
        footer_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        self.status_bar = StatusBar(footer)
        footer_sizer.Add(self.status_bar, 1, wx.ALL | wx.EXPAND, 4)
        
        btn_close = wx.Button(footer, label="Close", size=(75, 30))
        btn_close.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        footer_sizer.Add(btn_close, 0, wx.ALL, 8)
        
        footer.SetSizer(footer_sizer)
        main.Add(footer, 0, wx.EXPAND)
        
        self.SetSizer(main)
    
    def _refresh_list(self):
        self.list.DeleteAllItems()
        
        placed = self.manager.scan_all_boards()
        counts: Dict[str, int] = {}
        for ref, (board, _) in placed.items():
            counts[board] = counts.get(board, 0) + 1
        
        for name, board in self.manager.config.boards.items():
            idx = self.list.InsertItem(self.list.GetItemCount(), name)
            self.list.SetItem(idx, 1, str(counts.get(name, 0)))
            self.list.SetItem(idx, 2, str(len(board.ports)))
            self.list.SetItem(idx, 3, board.description or "—")
            self.list.SetItem(idx, 4, board.pcb_path)
        
        total_boards = len(self.manager.config.boards)
        total_components = sum(counts.values())
        self.status_bar.set_status(f"{total_boards} board(s), {total_components} component(s)", 'ok')
        self._on_selection_changed(None)
    
    def _get_selected_name(self) -> Optional[str]:
        idx = self.list.GetFirstSelected()
        return self.list.GetItemText(idx) if idx >= 0 else None
    
    def _on_selection_changed(self, event):
        has_sel = self.list.GetFirstSelected() >= 0
        self.btn_remove.Enable(has_sel)
        self.btn_open.Enable(has_sel)
        self.btn_update.Enable(has_sel)
        self.btn_ports.Enable(has_sel)
    
    def _on_new(self, event):
        existing = set(self.manager.config.boards.keys())
        dlg = NewBoardDialog(self, existing)
        if dlg.ShowModal() == wx.ID_OK:
            self.status_bar.set_status("Creating board...", 'working')
            success, msg = self.manager.create_board(dlg.result_name, dlg.result_desc)
            if success:
                self.status_bar.set_status(f"Created '{dlg.result_name}'", 'ok')
                wx.MessageBox(f"Board '{dlg.result_name}' created.\n\nUse Update to add components.",
                              "Success", wx.ICON_INFORMATION)
                self._refresh_list()
            else:
                self.status_bar.set_status("Creation failed", 'error')
                wx.MessageBox(msg, "Error", wx.ICON_ERROR)
        dlg.Destroy()
    
    def _on_remove(self, event):
        name = self._get_selected_name()
        if not name:
            return
        board = self.manager.config.boards.get(name)
        if not board:
            return
        
        if wx.MessageBox(
            f"Delete board '{name}'?\n\nThis removes the board folder and all files.\n"
            "The shared schematic is NOT affected.",
            "Confirm", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING) != wx.YES:
            return
        
        pcb_path = self.manager.project_dir / board.pcb_path
        board_dir = pcb_path.parent
        if board_dir.exists() and BOARDS_DIR in str(board_dir):
            try:
                shutil.rmtree(board_dir)
            except Exception as e:
                wx.MessageBox(f"Could not delete folder:\n{e}", "Warning", wx.ICON_WARNING)
        
        del self.manager.config.boards[name]
        self.manager.save_config()
        self.status_bar.set_status(f"Removed '{name}'", 'ok')
        self._refresh_list()
    
    def _on_open(self, event):
        name = self._get_selected_name()
        if not name:
            return
        board = self.manager.config.boards.get(name)
        if not board:
            return
        
        pcb_path = self.manager.project_dir / board.pcb_path
        if not pcb_path.exists():
            wx.MessageBox(f"PCB not found:\n{pcb_path}", "Error", wx.ICON_ERROR)
            return
        
        self.status_bar.set_status(f"Opening '{name}'...", 'working')
        if os.name == "nt":
            os.startfile(str(pcb_path))
        else:
            subprocess.Popen(["pcbnew", str(pcb_path)])
        self.status_bar.set_status(f"Opened '{name}'", 'ok')
    
    def _on_update(self, event):
        name = self._get_selected_name()
        if not name:
            return
        
        progress = ProgressDialog(self, f"Updating {name}")
        progress.Show()
        self.status_bar.set_status(f"Updating '{name}'...", 'working')
        
        def do_update():
            return self.manager.update_board(name,
                progress_callback=lambda p, m: wx.CallAfter(progress.update, p, m))
        
        def on_done(future):
            wx.CallAfter(progress.Destroy)
            try:
                success, msg = future.result()
                if success:
                    wx.CallAfter(lambda: self.status_bar.set_status(f"Updated '{name}'", 'ok'))
                    wx.CallAfter(lambda: wx.MessageBox(f"Update complete:\n\n{msg}", "Done", wx.ICON_INFORMATION))
                else:
                    wx.CallAfter(lambda: self.status_bar.set_status("Update failed", 'error'))
                    wx.CallAfter(lambda: wx.MessageBox(msg, "Failed", wx.ICON_ERROR))
                wx.CallAfter(self._refresh_list)
            except Exception as e:
                wx.CallAfter(lambda: self.status_bar.set_status("Update failed", 'error'))
                wx.CallAfter(lambda: wx.MessageBox(str(e), "Error", wx.ICON_ERROR))
        
        future = self.executor.submit(do_update)
        future.add_done_callback(on_done)
    
    def _on_ports(self, event):
        name = self._get_selected_name()
        if not name:
            return
        board = self.manager.config.boards.get(name)
        if not board:
            return
        
        dlg = PortDialog(self, board)
        if dlg.ShowModal() == wx.ID_OK:
            self.manager._generate_block_footprint(board)
            self.manager.save_config()
            self.status_bar.set_status(f"Updated ports for '{name}'", 'ok')
            self._refresh_list()
        dlg.Destroy()
    
    def _on_check(self, event):
        progress = ProgressDialog(self, "Checking Connectivity")
        progress.Show()
        self.status_bar.set_status("Running checks...", 'working')
        
        def do_check():
            return self.manager.check_connectivity(
                progress_callback=lambda p, m: wx.CallAfter(progress.update, p, m))
        
        def on_done(future):
            wx.CallAfter(progress.Destroy)
            try:
                report = future.result()
                errors = len(report.get("errors", []))
                violations = sum(b.get("violations", 0) for b in report.get("boards", {}).values())
                if errors == 0 and violations == 0:
                    status = ('All checks passed', 'ok')
                elif errors > 0:
                    status = (f'{errors} error(s)', 'error')
                else:
                    status = (f'{violations} violation(s)', 'warning')
                wx.CallAfter(lambda: self.status_bar.set_status(*status))
                wx.CallAfter(lambda: ConnectivityReportDialog(self, report).ShowModal())
            except Exception as e:
                wx.CallAfter(lambda: self.status_bar.set_status("Check failed", 'error'))
                wx.CallAfter(lambda: wx.MessageBox(str(e), "Error", wx.ICON_ERROR))
        
        future = self.executor.submit(do_check)
        future.add_done_callback(on_done)
    
    def _on_status(self, event):
        StatusDialog(self, self.manager).ShowModal()
    
    def _on_close(self, event):
        self.executor.shutdown(wait=False)
        self.Destroy()