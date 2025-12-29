"""
Multi-Board PCB Manager - Professional UI Components
=====================================================

Production-quality wxPython dialogs with:
- Centered positioning
- Consistent sizing
- Professional styling
- Keyboard navigation
- Complete tooltips

Author: Eliot
License: MIT
"""

import wx
import shutil
import os
import subprocess
from pathlib import Path
from typing import Optional, Set, Dict, Any
from wx.lib.agw import ultimatelistctrl as ULC
from wx.lib.wordwrap import wordwrap
import wx.grid as gridlib

import pcbnew

from .config import BoardConfig, PortDef
from .manager import MultiBoardManager
from .constants import BOARDS_DIR


# =============================================================================
# Design System
# =============================================================================

class Colors:
    """Professional color palette."""
    BACKGROUND = wx.Colour(245, 246, 247)
    PANEL_BG = wx.Colour(255, 255, 255)
    HEADER_BG = wx.Colour(38, 50, 56)
    HEADER_FG = wx.Colour(255, 255, 255)
    ACCENT = wx.Colour(30, 136, 229)
    BORDER = wx.Colour(218, 220, 224)
    TEXT_PRIMARY = wx.Colour(32, 33, 36)
    TEXT_SECONDARY = wx.Colour(95, 99, 104)
    SUCCESS = wx.Colour(67, 160, 71)
    WARNING = wx.Colour(251, 140, 0)
    ERROR = wx.Colour(229, 57, 53)
    INFO_BG = wx.Colour(227, 242, 253)
    SELECTED = wx.Colour(232, 240, 254)


class Spacing:
    """Consistent spacing values."""
    XS = 4
    SM = 8
    MD = 12
    LG = 16
    XL = 24


class Fonts:
    """Font configurations."""
    
    @staticmethod
    def header():
        return wx.Font(15, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
    
    @staticmethod
    def title():
        return wx.Font(12, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
    
    @staticmethod
    def body():
        return wx.Font(10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
    
    @staticmethod
    def small():
        return wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
    
    @staticmethod
    def mono():
        return wx.Font(9, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)


# =============================================================================
# Base Dialog
# =============================================================================

class BaseDialog(wx.Dialog):
    """Base dialog with common functionality."""
    
    def __init__(self, parent, title, size, **kwargs):
        # Ensure minimum size
        min_w, min_h = kwargs.pop('min_size', (400, 300))
        size = (max(size[0], min_w), max(size[1], min_h))
        
        style = kwargs.pop('style', wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        super().__init__(parent, title=title, size=size, style=style, **kwargs)
        
        self.SetMinSize((min_w, min_h))
        self.SetBackgroundColour(Colors.PANEL_BG)
        
        # Center on screen or parent
        self.CentreOnScreen() if parent is None else self.CentreOnParent()
        
        # Escape key closes dialog
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char)
    
    def _on_char(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self.EndModal(wx.ID_CANCEL)
        else:
            event.Skip()


# =============================================================================
# Custom Widgets
# =============================================================================
class IconButton(wx.Button):
    """Button with Unicode icon prefix."""
    
    ICONS = {
        'new': '+', 'delete': '×', 'open': '↗', 'refresh': '↻',
        'ports': '⇆', 'check': '✓', 'status': '☰', 'edit': '✎',
    }
    
    def __init__(self, parent, label, icon=None, **kwargs):
        if icon and icon in self.ICONS:
            label = f"{self.ICONS[icon]} {label}"
        super().__init__(parent, label=label, **kwargs)

class InfoBanner(wx.Panel):
    """Information banner with icon."""
    
    def __init__(self, parent, message, style='info'):
        super().__init__(parent)
        
        colors = {
            'info': (Colors.INFO_BG, Colors.ACCENT, 'ⓘ'),
            'warning': (wx.Colour(255, 243, 224), Colors.WARNING, '⚠'),
            'success': (wx.Colour(232, 245, 233), Colors.SUCCESS, '✓'),
        }
        bg, fg, icon = colors.get(style, colors['info'])
        
        self.SetBackgroundColour(bg)
        
        sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        icon_text = wx.StaticText(self, label=icon)
        icon_text.SetForegroundColour(fg)
        icon_text.SetFont(Fonts.title())
        sizer.Add(icon_text, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, Spacing.MD)
        
        msg_text = wx.StaticText(self, label=message)
        msg_text.SetForegroundColour(Colors.TEXT_PRIMARY)
        msg_text.SetFont(Fonts.body())
        msg_text.Wrap(600)
        sizer.Add(msg_text, 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, Spacing.MD)
        
        self.SetSizer(sizer)

class SectionHeader(wx.Panel):
    """Section header with title and subtitle."""
    
    def __init__(self, parent, title, subtitle=None):
        super().__init__(parent)
        self.SetBackgroundColour(Colors.PANEL_BG)
        
        sizer = wx.BoxSizer(wx.VERTICAL)
        
        title_text = wx.StaticText(self, label=title)
        title_text.SetFont(Fonts.title())
        title_text.SetForegroundColour(Colors.TEXT_PRIMARY)
        sizer.Add(title_text, 0, wx.BOTTOM, Spacing.XS)
        
        if subtitle:
            sub_text = wx.StaticText(self, label=subtitle)
            sub_text.SetFont(Fonts.small())
            sub_text.SetForegroundColour(Colors.TEXT_SECONDARY)
            sizer.Add(sub_text, 0)
        
        self.SetSizer(sizer)

class StatusIndicator(wx.Panel):
    """Status bar with icon and message."""
    
    def __init__(self, parent):
        super().__init__(parent)
        self.SetBackgroundColour(Colors.BACKGROUND)
        
        sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        self.icon = wx.StaticText(self, label="●")
        self.icon.SetForegroundColour(Colors.SUCCESS)
        sizer.Add(self.icon, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, Spacing.XS)
        
        self.text = wx.StaticText(self, label="Ready")
        self.text.SetFont(Fonts.small())
        self.text.SetForegroundColour(Colors.TEXT_SECONDARY)
        sizer.Add(self.text, 1, wx.ALL | wx.ALIGN_CENTER_VERTICAL, Spacing.XS)
        
        self.SetSizer(sizer)
    
    def set_status(self, message, status='ok'):
        colors = {'ok': Colors.SUCCESS, 'warning': Colors.WARNING,
                  'error': Colors.ERROR, 'working': Colors.ACCENT}
        self.icon.SetForegroundColour(colors.get(status, Colors.SUCCESS))
        self.text.SetLabel(message)
        self.Refresh()

# =============================================================================
# Progress Dialog
# =============================================================================
class ProgressDialog(BaseDialog):
    """Progress indicator dialog."""
    
    def __init__(self, parent, title="Working..."):
        super().__init__(parent, title, size=(500, 200), min_size=(400, 130),
                         style=wx.CAPTION | wx.STAY_ON_TOP)
        
        panel = wx.Panel(self)
        panel.SetBackgroundColour(Colors.PANEL_BG)
        sizer = wx.BoxSizer(wx.VERTICAL)
        
        self.label = wx.StaticText(panel, label="Initializing...")
        self.label.SetFont(Fonts.body())
        sizer.Add(self.label, 0, wx.ALL | wx.EXPAND, Spacing.LG)
        
        self.gauge = wx.Gauge(panel, range=100, size=(-1, 8))
        sizer.Add(self.gauge, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, Spacing.LG)
        
        self.percent = wx.StaticText(panel, label="0%")
        self.percent.SetFont(Fonts.small())
        self.percent.SetForegroundColour(Colors.TEXT_SECONDARY)
        sizer.Add(self.percent, 0, wx.ALL, Spacing.LG)
        
        panel.SetSizer(sizer)
        self.CentreOnParent() if parent else self.CentreOnScreen()
    
    def update(self, percent: int, message: str):
        self.gauge.SetValue(min(percent, 100))
        self.label.SetLabel(message)
        self.percent.SetLabel(f"{percent}%")
        wx.Yield()

# =============================================================================
# Port Dialogs
# =============================================================================
class PortEditDialog(BaseDialog):
    """Port configuration editor."""
    
    def __init__(self, parent, port: PortDef, existing_names: Set[str] = None):
        super().__init__(parent, "Port Configuration", size=(500, 400), min_size=(420, 300))
        
        self.port = port
        self.existing_names = existing_names or set()
        self.original_name = port.name
        
        self._build_ui()
    
    def _build_ui(self):
        panel = wx.Panel(self)
        panel.SetBackgroundColour(Colors.PANEL_BG)
        main = wx.BoxSizer(wx.VERTICAL)
        
        header = SectionHeader(panel, "Port Settings", "Define an inter-board connection point")
        main.Add(header, 0, wx.ALL | wx.EXPAND, Spacing.LG)
        
        form = wx.FlexGridSizer(4, 2, Spacing.SM, Spacing.LG)
        form.AddGrowableCol(1)
        
        # Name
        form.Add(self._label(panel, "Port Name"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_name = wx.TextCtrl(panel, value=self.port.name, size=(240, -1))
        self.txt_name.SetToolTip("Unique identifier (e.g., USB_DP, POWER_IN)")
        form.Add(self.txt_name, 1, wx.EXPAND)
        
        # Net
        form.Add(self._label(panel, "Net Name"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_net = wx.TextCtrl(panel, value=self.port.net, size=(240, -1))
        self.txt_net.SetToolTip("Schematic net this port connects to (e.g., +5V, USB_D+)")
        form.Add(self.txt_net, 1, wx.EXPAND)
        
        # Side
        form.Add(self._label(panel, "Board Edge"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.choice_side = wx.Choice(panel, choices=["Left", "Right", "Top", "Bottom"])
        self.choice_side.SetStringSelection(self.port.side.capitalize())
        self.choice_side.SetToolTip("Edge of the block footprint where this port appears")
        form.Add(self.choice_side, 0)
        
        # Position
        form.Add(self._label(panel, "Position"), 0, wx.ALIGN_CENTER_VERTICAL)
        pos_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.slider_pos = wx.Slider(panel, value=int(self.port.position * 100),
                                     minValue=0, maxValue=100, size=(180, -1))
        self.slider_pos.SetToolTip("Position along edge: 0% = start, 100% = end")
        pos_sizer.Add(self.slider_pos, 1, wx.EXPAND | wx.RIGHT, Spacing.SM)
        self.pos_label = wx.StaticText(panel, label=f"{int(self.port.position * 100)}%", size=(40, -1))
        pos_sizer.Add(self.pos_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(pos_sizer, 1, wx.EXPAND)
        
        self.slider_pos.Bind(wx.EVT_SLIDER, lambda e: self.pos_label.SetLabel(f"{self.slider_pos.GetValue()}%"))
        
        main.Add(form, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, Spacing.LG)
        main.AddStretchSpacer()
        
        # Buttons
        btn_sizer = self._create_buttons(panel, "Save")
        main.Add(btn_sizer, 0, wx.ALL | wx.EXPAND, Spacing.LG)
        
        panel.SetSizer(main)
        self.txt_name.SetFocus()
    
    def _label(self, parent, text):
        lbl = wx.StaticText(parent, label=text)
        lbl.SetFont(Fonts.body())
        lbl.SetForegroundColour(Colors.TEXT_SECONDARY)
        return lbl
    
    def _create_buttons(self, parent, ok_label="OK"):
        sizer = wx.BoxSizer(wx.HORIZONTAL)
        sizer.AddStretchSpacer()
        
        btn_cancel = wx.Button(parent, wx.ID_CANCEL, "Cancel")
        sizer.Add(btn_cancel, 0, wx.RIGHT, Spacing.SM)
        
        btn_ok = wx.Button(parent, wx.ID_OK, ok_label)
        btn_ok.SetDefault()
        btn_ok.Bind(wx.EVT_BUTTON, self._on_ok)
        sizer.Add(btn_ok, 0)
        
        return sizer
    
    def _on_ok(self, event):
        name = self.txt_name.GetValue().strip()
        
        if not name:
            wx.MessageBox("Port name is required.", "Validation", wx.ICON_WARNING)
            self.txt_name.SetFocus()
            return
        
        if name != self.original_name and name in self.existing_names:
            wx.MessageBox(f"Port '{name}' already exists.", "Validation", wx.ICON_WARNING)
            self.txt_name.SetFocus()
            return
        
        self.port = PortDef(
            name=name,
            net=self.txt_net.GetValue().strip(),
            side=self.choice_side.GetStringSelection().lower(),
            position=self.slider_pos.GetValue() / 100.0,
        )
        self.EndModal(wx.ID_OK)
class PortDialog(BaseDialog):
    """Port manager dialog."""
    
    def __init__(self, parent, board: BoardConfig):
        super().__init__(parent, f"Manage Ports — {board.name}",
                         size=(600, 480), min_size=(520, 400))
        
        self.board = board
        self.ports = dict(board.ports)
        self._build_ui()
        self._refresh_list()
    
    def _build_ui(self):
        panel = wx.Panel(self)
        panel.SetBackgroundColour(Colors.PANEL_BG)
        main = wx.BoxSizer(wx.VERTICAL)
        
        header = SectionHeader(panel, "Inter-Board Ports",
                                "Define electrical connection points between boards")
        main.Add(header, 0, wx.ALL | wx.EXPAND, Spacing.LG)
        
        # List
        self.list = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SIMPLE)
        self.list.InsertColumn(0, "Port Name", width=150)
        self.list.InsertColumn(1, "Connected Net", width=150)
        self.list.InsertColumn(2, "Edge", width=80)
        self.list.InsertColumn(3, "Position", width=80)
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, lambda e: self._on_edit(e))
        main.Add(self.list, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, Spacing.LG)
        
        # Actions
        action_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        btn_add = IconButton(panel, "Add Port", icon='new', size=(100, 32))
        btn_add.SetToolTip("Create a new inter-board port")
        btn_add.Bind(wx.EVT_BUTTON, self._on_add)
        action_sizer.Add(btn_add, 0, wx.RIGHT, Spacing.SM)
        
        btn_edit = IconButton(panel, "Edit", icon='edit', size=(80, 32))
        btn_edit.SetToolTip("Edit selected port (or double-click)")
        btn_edit.Bind(wx.EVT_BUTTON, self._on_edit)
        action_sizer.Add(btn_edit, 0, wx.RIGHT, Spacing.SM)
        
        btn_remove = IconButton(panel, "Remove", icon='delete', size=(90, 32))
        btn_remove.SetToolTip("Delete selected port")
        btn_remove.Bind(wx.EVT_BUTTON, self._on_remove)
        action_sizer.Add(btn_remove, 0)
        
        main.Add(action_sizer, 0, wx.ALL, Spacing.LG)
        
        main.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, Spacing.LG)
        
        # Dialog buttons
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_sizer.AddStretchSpacer()
        
        btn_cancel = wx.Button(panel, wx.ID_CANCEL, "Cancel")
        btn_sizer.Add(btn_cancel, 0, wx.RIGHT, Spacing.SM)
        
        btn_ok = wx.Button(panel, wx.ID_OK, "Apply Changes")
        btn_ok.SetDefault()
        btn_ok.Bind(wx.EVT_BUTTON, self._on_ok)
        btn_sizer.Add(btn_ok, 0)
        
        main.Add(btn_sizer, 0, wx.ALL | wx.EXPAND, Spacing.LG)
        
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
        if wx.MessageBox(f"Remove port '{name}'?", "Confirm Removal",
                         wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION) == wx.YES:
            del self.ports[name]
            self._refresh_list()
    
    def _on_ok(self, event):
        self.board.ports = self.ports
        self.EndModal(wx.ID_OK)

# =============================================================================
# New Board Dialog
# =============================================================================
class NewBoardDialog(BaseDialog):
    """New board creation dialog."""
    
    def __init__(self, parent, existing_names: Set[str]):
        super().__init__(parent, "Create New Board", size=(550, 500), min_size=(450, 280))
        
        self.existing = existing_names
        self.result_name = ""
        self.result_desc = ""
        self._build_ui()
    
    def _build_ui(self):
        panel = wx.Panel(self)
        panel.SetBackgroundColour(Colors.PANEL_BG)
        main = wx.BoxSizer(wx.VERTICAL)
        
        header = SectionHeader(panel, "New Sub-Board",
                                "Create a PCB that shares this project's schematic")
        main.Add(header, 0, wx.ALL | wx.EXPAND, Spacing.LG)
        
        form = wx.FlexGridSizer(2, 2, Spacing.SM, Spacing.LG)
        form.AddGrowableCol(1)
        
        # Name
        lbl_name = wx.StaticText(panel, label="Board Name")
        lbl_name.SetFont(Fonts.body())
        lbl_name.SetForegroundColour(Colors.TEXT_SECONDARY)
        form.Add(lbl_name, 0, wx.ALIGN_CENTER_VERTICAL)
        
        self.txt_name = wx.TextCtrl(panel, size=(280, -1))
        self.txt_name.SetToolTip("Unique name for this board (e.g., PowerSupply, MainBoard)")
        form.Add(self.txt_name, 1, wx.EXPAND)
        
        # Description
        lbl_desc = wx.StaticText(panel, label="Description")
        lbl_desc.SetFont(Fonts.body())
        lbl_desc.SetForegroundColour(Colors.TEXT_SECONDARY)
        form.Add(lbl_desc, 0, wx.ALIGN_TOP | wx.TOP, 4)
        
        self.txt_desc = wx.TextCtrl(panel, size=(280, 60), style=wx.TE_MULTILINE)
        self.txt_desc.SetToolTip("Optional: describe this board's purpose")
        form.Add(self.txt_desc, 1, wx.EXPAND)
        
        main.Add(form, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, Spacing.LG)
        
        info = InfoBanner(panel, "The new board will share the project schematic. \n"
                                  "Use Update to assign components to this board.", style='info')
        main.Add(info, 0, wx.ALL | wx.EXPAND, Spacing.LG)
        
        main.AddStretchSpacer()
        
        # Buttons
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_sizer.AddStretchSpacer()
        
        btn_cancel = wx.Button(panel, wx.ID_CANCEL, "Cancel")
        btn_sizer.Add(btn_cancel, 0, wx.RIGHT, Spacing.SM)
        
        btn_ok = wx.Button(panel, wx.ID_OK, "Create Board")
        btn_ok.SetDefault()
        btn_ok.Bind(wx.EVT_BUTTON, self._on_ok)
        btn_sizer.Add(btn_ok, 0)
        
        main.Add(btn_sizer, 0, wx.ALL | wx.EXPAND, Spacing.LG)
        
        panel.SetSizer(main)
        self.txt_name.SetFocus()
    
    def _on_ok(self, event):
        name = self.txt_name.GetValue().strip()
        
        if not name:
            wx.MessageBox("Please enter a board name.", "Validation", wx.ICON_WARNING)
            self.txt_name.SetFocus()
            return
        
        if name in self.existing:
            wx.MessageBox(f"Board '{name}' already exists.", "Validation", wx.ICON_WARNING)
            self.txt_name.SetFocus()
            return
        
        safe = "".join(c if c.isalnum() or c in "_-" else "" for c in name)
        if not safe:
            wx.MessageBox("Name must contain at least one letter or number.",
                          "Validation", wx.ICON_WARNING)
            self.txt_name.SetFocus()
            return
        
        self.result_name = name
        self.result_desc = self.txt_desc.GetValue().strip()
        self.EndModal(wx.ID_OK)

# =============================================================================
# Report Dialogs
# =============================================================================
class ConnectivityReportDialog(BaseDialog):
    """DRC and connectivity report."""
    
    def __init__(self, parent, report: Dict[str, Any]):
        super().__init__(parent, "Connectivity Report", size=(700, 550), min_size=(600, 450))
        
        self.report = report
        self._build_ui()
    
    def _build_ui(self):
        panel = wx.Panel(self)
        panel.SetBackgroundColour(Colors.PANEL_BG)
        main = wx.BoxSizer(wx.VERTICAL)
        
        errors = self.report.get("errors", [])
        boards = self.report.get("boards", {})
        total_violations = sum(b.get("violations", 0) for b in boards.values())
        
        # Status header
        if total_violations == 0 and len(errors) == 0:
            status_color, icon, title = Colors.SUCCESS, "✓", "All Checks Passed"
        elif len(errors) > 0:
            status_color, icon, title = Colors.ERROR, "✕", f"{len(errors)} Error(s)"
        else:
            status_color, icon, title = Colors.WARNING, "⚠", f"{total_violations} Violation(s)"
        
        header_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        icon_text = wx.StaticText(panel, label=icon)
        icon_text.SetFont(wx.Font(24, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        icon_text.SetForegroundColour(status_color)
        header_sizer.Add(icon_text, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, Spacing.MD)
        
        title_text = wx.StaticText(panel, label=title)
        title_text.SetFont(Fonts.header())
        title_text.SetForegroundColour(Colors.TEXT_PRIMARY)
        header_sizer.Add(title_text, 0, wx.ALIGN_CENTER_VERTICAL)
        
        main.Add(header_sizer, 0, wx.ALL, Spacing.LG)
        
        # Stats
        stats = wx.StaticText(panel,
            label=f"Boards checked: {len(boards)}  •  "
                  f"DRC violations: {total_violations}  •  "
                  f"Errors: {len(errors)}")
        stats.SetFont(Fonts.body())
        stats.SetForegroundColour(Colors.TEXT_SECONDARY)
        main.Add(stats, 0, wx.LEFT | wx.RIGHT, Spacing.LG)
        
        main.AddSpacer(Spacing.LG)
        
        # Details
        self.text = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.BORDER_SIMPLE)
        self.text.SetFont(Fonts.mono())
        self.text.SetBackgroundColour(wx.Colour(250, 250, 250))
        
        lines = []
        for board_name, data in boards.items():
            count = data.get('violations', 0)
            icon = "✓" if count == 0 else "⚠"
            lines.append(f"{icon} {board_name}: {count} violation(s)")
            for v in data.get("details", []):
                vtype = v.get('type', 'unknown')
                desc = v.get('description', '')[:70]
                lines.append(f"    • {vtype}: {desc}")
            lines.append("")
        
        if errors:
            lines.append("─── Errors ───")
            for e in errors:
                lines.append(f"✕ {e}")
        
        self.text.SetValue("\n".join(lines) if lines else "No issues found.")
        main.Add(self.text, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, Spacing.LG)
        
        # Close
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_sizer.AddStretchSpacer()
        btn_close = wx.Button(panel, wx.ID_CLOSE, "Close")
        btn_close.SetDefault()
        btn_close.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        btn_sizer.Add(btn_close, 0)
        main.Add(btn_sizer, 0, wx.ALL | wx.EXPAND, Spacing.LG)
        
        panel.SetSizer(main)

class StatusDialog(BaseDialog):
    """Component placement status."""
    
    def __init__(self, parent, manager: MultiBoardManager):
        super().__init__(parent, "Component Status", size=(640, 500), min_size=(550, 400))
        
        self.manager = manager
        self._build_ui()
        wx.CallAfter(self._load_data)
    
    def _build_ui(self):
        panel = wx.Panel(self)
        panel.SetBackgroundColour(Colors.PANEL_BG)
        main = wx.BoxSizer(wx.VERTICAL)
        
        self.header = SectionHeader(panel, "Component Placement", "Loading...")
        main.Add(self.header, 0, wx.ALL | wx.EXPAND, Spacing.LG)
        
        self.tree = wx.TreeCtrl(panel, style=wx.TR_DEFAULT_STYLE | wx.TR_HIDE_ROOT | wx.BORDER_SIMPLE)
        main.Add(self.tree, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, Spacing.LG)
        
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_sizer.AddStretchSpacer()
        btn_close = wx.Button(panel, wx.ID_CLOSE, "Close")
        btn_close.SetDefault()
        btn_close.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        btn_sizer.Add(btn_close, 0)
        main.Add(btn_sizer, 0, wx.ALL | wx.EXPAND, Spacing.LG)
        
        panel.SetSizer(main)
    
    def _load_data(self):
        placed, unplaced, total = self.manager.get_status()
        
        # Update header
        self.header.DestroyChildren()
        sizer = wx.BoxSizer(wx.VERTICAL)
        
        title = wx.StaticText(self.header, label="Component Placement")
        title.SetFont(Fonts.title())
        sizer.Add(title, 0, wx.BOTTOM, Spacing.XS)
        
        subtitle = wx.StaticText(self.header,
            label=f"Total: {total}  •  Placed: {len(placed)}  •  Unplaced: {len(unplaced)}")
        subtitle.SetFont(Fonts.small())
        subtitle.SetForegroundColour(Colors.TEXT_SECONDARY)
        sizer.Add(subtitle, 0)
        
        self.header.SetSizer(sizer)
        self.header.Layout()
        
        # Populate tree
        self.tree.DeleteAllItems()
        root = self.tree.AddRoot("Status")
        
        by_board: Dict[str, list] = {}
        for ref, board in placed.items():
            by_board.setdefault(board, []).append(ref)
        
        for board_name in sorted(by_board.keys()):
            refs = sorted(by_board[board_name])
            node = self.tree.AppendItem(root, f"✓ {board_name} ({len(refs)} components)")
            for ref in refs[:100]:
                self.tree.AppendItem(node, f"    {ref}")
            if len(refs) > 100:
                self.tree.AppendItem(node, f"    ... +{len(refs) - 100} more")
        
        if unplaced:
            node = self.tree.AppendItem(root, f"○ Unplaced ({len(unplaced)} components)")
            for ref in sorted(unplaced)[:100]:
                self.tree.AppendItem(node, f"    {ref}")
            if len(unplaced) > 100:
                self.tree.AppendItem(node, f"    ... +{len(unplaced) - 100} more")
        
        self.tree.ExpandAll()

# =============================================================================
# Main Dialog
# =============================================================================
class MainDialog(BaseDialog):
    """Primary plugin dialog."""
    
    def __init__(self, parent, pcb_board: "pcbnew.BOARD"):
        # Initialize before super().__init__ to get project name for title
        pcb_path = pcb_board.GetFileName()
        project_dir = Path(pcb_path).parent if pcb_path else Path.cwd()
        self.manager = MultiBoardManager(project_dir)
        
        super().__init__(parent, "Multi-Board Manager",
                         size=(1200, 800), min_size=(800, 550))
                
        self._build_ui()
        self._refresh_list()
        
        self.Bind(wx.EVT_CLOSE, self._on_close)
    
    def _build_ui(self):
        main = wx.BoxSizer(wx.VERTICAL)

        # ─────────────────────────────────────────────────────────────────────
        # Header
        # ─────────────────────────────────────────────────────────────────────
        header = wx.Panel(self)
        header.SetBackgroundColour(Colors.HEADER_BG)
        header_sizer = wx.BoxSizer(wx.HORIZONTAL)

        title_sizer = wx.BoxSizer(wx.VERTICAL)

        title = wx.StaticText(header, label="Multi-Board Manager")
        title.SetFont(Fonts.header())
        title.SetForegroundColour(Colors.HEADER_FG)
        title_sizer.Add(title, 0, wx.BOTTOM, Spacing.XS)

        subtitle = wx.StaticText(header, label=f"Project: {self.manager.project_dir.name}")
        subtitle.SetFont(Fonts.body())
        subtitle.SetForegroundColour(wx.Colour(176, 190, 197))
        title_sizer.Add(subtitle, 0)

        header_sizer.Add(title_sizer, 1, wx.ALL, Spacing.LG)

        self.board_count_badge = wx.StaticText(header, label="0 boards")
        self.board_count_badge.SetFont(Fonts.body())
        self.board_count_badge.SetForegroundColour(wx.Colour(176, 190, 197))
        header_sizer.Add(self.board_count_badge, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, Spacing.LG)

        header.SetSizer(header_sizer)
        main.Add(header, 0, wx.EXPAND)

        # ─────────────────────────────────────────────────────────────────────
        # Content
        # ─────────────────────────────────────────────────────────────────────
        content = wx.Panel(self)
        content.SetBackgroundColour(Colors.PANEL_BG)
        content_sizer = wx.BoxSizer(wx.VERTICAL)

        info = InfoBanner(content,
            "All boards share the same schematic. Edits made anywhere are instantly reflected everywhere.",
            style='info')
        content_sizer.Add(info, 0, wx.ALL | wx.EXPAND, Spacing.LG)

        # Board table (Grid = real wrapping + auto row heights)
        self.grid = gridlib.Grid(content)
        self.grid.CreateGrid(0, 5)
        self.grid.SetFont(Fonts.body())

        self.grid.SetRowLabelSize(0)              # hide row labels
        self.grid.EnableEditing(False)            # read-only
        self.grid.EnableGridLines(True)
        self.grid.SetSelectionMode(gridlib.Grid.SelectRows)

        # Column labels
        self.grid.SetColLabelValue(0, "Board")
        self.grid.SetColLabelValue(1, "Components")
        self.grid.SetColLabelValue(2, "Ports")
        self.grid.SetColLabelValue(3, "Description")
        self.grid.SetColLabelValue(4, "Path")

        # Initial widths (you can still resize; rows will auto-resize)
        self.grid.SetColSize(0, 140)
        self.grid.SetColSize(1, 120)
        self.grid.SetColSize(2, 70)
        self.grid.SetColSize(3, 450)
        self.grid.SetColSize(4, 300)

        # Selection + open behavior
        self.grid.Bind(gridlib.EVT_GRID_SELECT_CELL, self._on_grid_select)
        self.grid.Bind(gridlib.EVT_GRID_CELL_LEFT_DCLICK, self._on_open)
        self.grid.Bind(wx.EVT_SIZE, self._on_grid_size)

        content_sizer.Add(self.grid, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, Spacing.LG)

        # ─────────────────────────────────────────────────────────────────────
        # Toolbar
        # ─────────────────────────────────────────────────────────────────────
        toolbar = wx.Panel(content)
        toolbar.SetBackgroundColour(Colors.PANEL_BG)
        tb_sizer = wx.BoxSizer(wx.HORIZONTAL)

        # Board management
        btn_new = IconButton(toolbar, "New", icon='new', size=(80, 34))
        btn_new.SetToolTip("Create a new sub-board (shares project schematic)")
        btn_new.Bind(wx.EVT_BUTTON, self._on_new)
        tb_sizer.Add(btn_new, 0, wx.RIGHT, Spacing.SM)

        self.btn_remove = IconButton(toolbar, "Remove", icon='delete', size=(95, 34))
        self.btn_remove.SetToolTip("Delete the selected board and its files")
        self.btn_remove.Bind(wx.EVT_BUTTON, self._on_remove)
        self.btn_remove.Disable()
        tb_sizer.Add(self.btn_remove, 0, wx.RIGHT, Spacing.LG)

        # Separator
        tb_sizer.Add(wx.StaticLine(toolbar, style=wx.LI_VERTICAL), 0,
                    wx.EXPAND | wx.TOP | wx.BOTTOM | wx.RIGHT, Spacing.SM)
        tb_sizer.AddSpacer(Spacing.SM)

        # Board actions
        self.btn_open = IconButton(toolbar, "Open", icon='open', size=(80, 34))
        self.btn_open.SetToolTip("Open board project in KiCad (double-click also works)")
        self.btn_open.Bind(wx.EVT_BUTTON, self._on_open)
        self.btn_open.Disable()
        tb_sizer.Add(self.btn_open, 0, wx.RIGHT, Spacing.SM)

        self.btn_update = IconButton(toolbar, "Update", icon='refresh', size=(95, 34))
        self.btn_update.SetToolTip("Sync components from schematic to this board")
        self.btn_update.Bind(wx.EVT_BUTTON, self._on_update)
        self.btn_update.Disable()
        tb_sizer.Add(self.btn_update, 0, wx.RIGHT, Spacing.SM)

        self.btn_ports = IconButton(toolbar, "Ports", icon='ports', size=(80, 34))
        self.btn_ports.SetToolTip("Define inter-board connection ports")
        self.btn_ports.Bind(wx.EVT_BUTTON, self._on_ports)
        self.btn_ports.Disable()
        tb_sizer.Add(self.btn_ports, 0, wx.RIGHT, Spacing.SM)

        # NEW: edit description button
        self.btn_edit_desc = IconButton(toolbar, "Description", icon='edit', size=(120, 34))
        self.btn_edit_desc.SetToolTip("Edit the selected board description")
        self.btn_edit_desc.Bind(wx.EVT_BUTTON, self._on_edit_description)
        self.btn_edit_desc.Disable()
        tb_sizer.Add(self.btn_edit_desc, 0, wx.RIGHT, Spacing.LG)

        # Separator
        tb_sizer.Add(wx.StaticLine(toolbar, style=wx.LI_VERTICAL), 0,
                    wx.EXPAND | wx.TOP | wx.BOTTOM | wx.RIGHT, Spacing.SM)
        tb_sizer.AddSpacer(Spacing.SM)

        # Global actions
        btn_check = IconButton(toolbar, "Check All", icon='check', size=(105, 34))
        btn_check.SetToolTip("Run DRC and connectivity checks on all boards")
        btn_check.Bind(wx.EVT_BUTTON, self._on_check)
        tb_sizer.Add(btn_check, 0, wx.RIGHT, Spacing.SM)

        btn_status = IconButton(toolbar, "Status", icon='status', size=(85, 34))
        btn_status.SetToolTip("View component placement status across all boards")
        btn_status.Bind(wx.EVT_BUTTON, self._on_status)
        tb_sizer.Add(btn_status, 0)

        toolbar.SetSizer(tb_sizer)
        content_sizer.Add(toolbar, 0, wx.ALL, Spacing.LG)

        content.SetSizer(content_sizer)
        main.Add(content, 1, wx.EXPAND)

        # ─────────────────────────────────────────────────────────────────────
        # Footer
        # ─────────────────────────────────────────────────────────────────────
        footer = wx.Panel(self)
        footer.SetBackgroundColour(Colors.BACKGROUND)
        footer_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.status_bar = StatusIndicator(footer)
        footer_sizer.Add(self.status_bar, 1, wx.ALL | wx.EXPAND, Spacing.XS)

        btn_close = wx.Button(footer, label="Close", size=(90, 34))
        btn_close.Bind(wx.EVT_BUTTON, lambda e: self.Close())
        footer_sizer.Add(btn_close, 0, wx.ALL, Spacing.SM)

        footer.SetSizer(footer_sizer)
        main.Add(footer, 0, wx.EXPAND)

        self.SetSizer(main)

    def _refresh_list(self):
        # Clear all rows
        if self.grid.GetNumberRows() > 0:
            self.grid.DeleteRows(0, self.grid.GetNumberRows())

        placed = self.manager.scan_all_boards()
        counts: Dict[str, int] = {}
        for ref, (board, _) in placed.items():
            counts[board] = counts.get(board, 0) + 1

        current_board = self._get_current_board_name()

        boards = list(self.manager.config.boards.items())
        if boards:
            self.grid.AppendRows(len(boards))

        wrap_renderer = gridlib.GridCellAutoWrapStringRenderer()

        for row, (name, board) in enumerate(boards):
            display_name = f"  → {name}" if name == current_board else name

            self.grid.SetCellValue(row, 0, display_name)
            self.grid.SetCellValue(row, 1, str(counts.get(name, 0)))
            self.grid.SetCellValue(row, 2, str(len(board.ports)))
            self.grid.SetCellValue(row, 3, board.description or "—")
            self.grid.SetCellValue(row, 4, board.pcb_path or "")

            # Wrap only the long-text columns
            self.grid.SetCellRenderer(row, 3, wrap_renderer)
            self.grid.SetCellRenderer(row, 4, wrap_renderer)

            # Make every cell read-only (extra safety)
            for col in range(5):
                self.grid.SetReadOnly(row, col, True)

            # Highlight current board row
            if name == current_board:
                for col in range(5):
                    self.grid.SetCellBackgroundColour(row, col, Colors.SELECTED)

        # Recompute row heights based on current column widths and wrapped text
        self._autosize_grid_rows()

        total_boards = len(self.manager.config.boards)
        total_components = sum(counts.values())
        self.board_count_badge.SetLabel(f"{total_boards} board(s)")
        self.status_bar.set_status(f"{total_boards} board(s), {total_components} component(s) placed", 'ok')

        self._on_selection_changed(None)
    
    def _get_current_board_name(self) -> Optional[str]:
        """Determine if we're currently in a sub-board."""
        try:
            board = pcbnew.GetBoard()
            if board:
                pcb_path = Path(board.GetFileName())
                # Check if this PCB is one of our sub-boards
                for name, cfg in self.manager.config.boards.items():
                    board_pcb = self.manager.project_dir / cfg.pcb_path
                    try:
                        if pcb_path.resolve() == board_pcb.resolve():
                            return name
                    except Exception:
                        if pcb_path.name == board_pcb.name:
                            return name
        except Exception:
            pass
        return None
    
    def _get_selected_name(self) -> Optional[str]:
        rows = self.grid.GetSelectedRows()
        if rows:
            row = rows[0]
        else:
            row = self.grid.GetGridCursorRow()
            if row < 0 or row >= self.grid.GetNumberRows():
                return None

        name = self.grid.GetCellValue(row, 0)
        if name.startswith("  → "):
            name = name[4:]
        return name or None

    def _on_selection_changed(self, event):
        has_sel = bool(self.grid.GetSelectedRows()) or (0 <= self.grid.GetGridCursorRow() < self.grid.GetNumberRows())
        self.btn_remove.Enable(has_sel)
        self.btn_open.Enable(has_sel)
        self.btn_update.Enable(has_sel)
        self.btn_ports.Enable(has_sel)
        self.btn_edit_desc.Enable(has_sel)
    
    def _on_grid_select(self, event):
        row = event.GetRow()
        if row >= 0:
            self.grid.SelectRow(row)
        self._on_selection_changed(None)
        event.Skip()

    def _on_grid_size(self, event):
        event.Skip()
        wx.CallAfter(self._autosize_grid_rows)

    def _autosize_grid_rows(self):
        if not hasattr(self, "grid"):
            return
        if self.grid.GetNumberRows() <= 0:
            return
        # Auto-size rows based on wrapped content and current column widths
        self.grid.AutoSizeRows()
        self.grid.ForceRefresh()

    def _on_edit_description(self, event):
        name = self._get_selected_name()
        if not name:
            return

        board = self.manager.config.boards.get(name)
        if not board:
            return

        dlg = wx.TextEntryDialog(
            self,
            message=f"Edit description for '{name}':",
            caption="Edit Board Description",
            value=board.description or "",
            style=wx.OK | wx.CANCEL | wx.TE_MULTILINE
        )
        try:
            dlg.SetSize((560, 340))
            if dlg.ShowModal() != wx.ID_OK:
                return

            board.description = dlg.GetValue().strip()
            self.manager.save_config()
            self.status_bar.set_status(f"Updated description for '{name}'", 'ok')
            self._refresh_list()
        finally:
            dlg.Destroy()

    def _on_new(self, event):
        existing = set(self.manager.config.boards.keys())
        dlg = NewBoardDialog(self, existing)
        if dlg.ShowModal() == wx.ID_OK:
            self.status_bar.set_status("Creating board...", 'working')
            wx.Yield()
            
            success, msg = self.manager.create_board(dlg.result_name, dlg.result_desc)
            if success:
                self.status_bar.set_status(f"Created '{dlg.result_name}'", 'ok')
                wx.MessageBox(
                    f"Board '{dlg.result_name}' created successfully.\n\n"
                    "Use Update to assign and sync components from the schematic.",
                    "Board Created", wx.ICON_INFORMATION)
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
        
        result = wx.MessageBox(
            f"Delete board '{name}'?\n\n"
            "This will permanently remove:\n"
            "  • The board folder and all files\n"
            "  • All PCB layout work\n\n"
            "The shared schematic will NOT be affected.",
            "Confirm Deletion", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING)
        
        if result != wx.YES:
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
        self.manager._scan_cache = None  # Invalidate cache
        
        self.status_bar.set_status(f"Removed '{name}'", 'ok')
        self._refresh_list()
    
    def _on_open(self, event):
        """Open the board's KiCad project (not just PCB)."""
        name = self._get_selected_name()
        if not name:
            return
        
        board = self.manager.config.boards.get(name)
        if not board:
            return
        
        pcb_path = self.manager.project_dir / board.pcb_path
        pro_path = pcb_path.with_suffix(".kicad_pro")
        
        # Prefer opening the project file if it exists
        target = pro_path if pro_path.exists() else pcb_path
        
        if not target.exists():
            wx.MessageBox(f"File not found:\n{target}", "Error", wx.ICON_ERROR)
            return
        
        self.status_bar.set_status(f"Opening '{name}'...", 'working')
        
        try:
            if os.name == "nt":
                os.startfile(str(target))
            elif os.uname().sysname == "Darwin":
                subprocess.Popen(["open", str(target)])
            else:
                # Try kicad first, fall back to pcbnew
                try:
                    subprocess.Popen(["kicad", str(pro_path if pro_path.exists() else target)])
                except FileNotFoundError:
                    subprocess.Popen(["pcbnew", str(pcb_path)])
            
            self.status_bar.set_status(f"Opened '{name}'", 'ok')
        except Exception as e:
            self.status_bar.set_status("Open failed", 'error')
            wx.MessageBox(f"Could not open project:\n{e}", "Error", wx.ICON_ERROR)
    
    def _on_update(self, event):
        name = self._get_selected_name()
        if not name:
            return
        
        progress = ProgressDialog(self, f"Updating {name}")
        progress.Show()
        wx.Yield()  # Allow dialog to show
        self.status_bar.set_status(f"Updating '{name}'...", 'working')
        
        try:
            success, msg = self.manager.update_board(name,
                progress_callback=lambda p, m: (progress.update(p, m), wx.Yield()))
            
            progress.Destroy()
            
            if success:
                self.status_bar.set_status(f"Updated '{name}'", 'ok')
                # Add hint about P key if components were added
                if "Added:" in msg and not msg.startswith("Added: 0"):
                    msg += "\n\nTip: Select new components and press 'P' to pack them."
                wx.MessageBox(f"Update complete:\n\n{msg}", "Success", wx.ICON_INFORMATION)
            else:
                self.status_bar.set_status("Update failed", 'error')
                wx.MessageBox(msg, "Update Failed", wx.ICON_ERROR)
            
            self._refresh_list()
            
        except Exception as e:
            progress.Destroy()
            self.status_bar.set_status("Update failed", 'error')
            wx.MessageBox(str(e), "Error", wx.ICON_ERROR)
    
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
        if not self.manager.config.boards:
            wx.MessageBox("No boards to check.", "Info", wx.ICON_INFORMATION)
            return
        
        progress = ProgressDialog(self, "Checking Connectivity")
        progress.Show()
        wx.Yield()
        self.status_bar.set_status("Running checks...", 'working')
        
        try:
            report = self.manager.check_connectivity(
                progress_callback=lambda p, m: (progress.update(p, m), wx.Yield()))
            
            progress.Destroy()
            
            errors = len(report.get("errors", []))
            violations = sum(b.get("violations", 0) for b in report.get("boards", {}).values())
            
            if errors == 0 and violations == 0:
                self.status_bar.set_status('All checks passed', 'ok')
            elif errors > 0:
                self.status_bar.set_status(f'{errors} error(s)', 'error')
            else:
                self.status_bar.set_status(f'{violations} violation(s)', 'warning')
            
            ConnectivityReportDialog(self, report).ShowModal()
            
        except Exception as e:
            progress.Destroy()
            self.status_bar.set_status("Check failed", 'error')
            wx.MessageBox(str(e), "Error", wx.ICON_ERROR)
    
    def _on_status(self, event):
        StatusDialog(self, self.manager).ShowModal()
    
    def _on_close(self, event):
        self.Destroy()

    def _on_begin_label_edit(self, event):
        # This prevents the white in-place editor overlay from showing up
        event.Veto()
