"""
Multi-Board PCB Manager - wxPython UI
====================================

This file is all UI. No PCB surgery should happen here.

UI architecture
---------------
Design system:
  Colors / Spacing / Fonts
    A tiny “style guide” so the plugin looks consistent and not like random wx
    widgets thrown together.

BaseDialog:
  A common base that handles escape-to-close, minimum size, background color.

Custom widgets:
  - IconButton: gives buttons a little unicode icon (cheap but effective).
  - InfoBanner: “FYI” strip used for guidance messages.
  - SectionHeader: title + subtitle for dialog sections.
  - StatusIndicator: bottom status bar for the main dialog.
  - SearchBox: filter input with a clear button.

Dialogs:
  - PortEditDialog / PortDialog: edit inter-board ports for a board.
  - NewBoardDialog: create a sub-board (name + description).
  - HealthReportDialog: show project/board health info from the manager.
  - DiffViewDialog: compare board placements (refs present in each).
  - ConnectivityReportDialog: show DRC/connectivity results from kicad-cli.
  - StatusDialog: show placed/unplaced component refs.
  - MainDialog: the main app UI (table of boards + toolbar actions).

KiCad subtleties
--------------------------------------------------------
1) Plugin runs in KiCad’s UI process.
   If we do heavy work synchronously, the whole KiCad window freezes.
   That’s why long operations:
   - show a ProgressDialog
   - call wx.Yield() occasionally (so the UI redraws)

2) Don’t try to do pcbnew work in background threads.
   pcbnew is not thread-safe. You can use threads for pure Python work, but
   touching KiCad objects crashes.

3) Board open detection.
   Updating/deleting boards that are open is dangerous, so the UI disables those
   actions when the manager says a board is open (lock files).

Author: Eliot
License: MIT
"""

import wx
import shutil
import os
import subprocess
from pathlib import Path
from typing import Optional, Set, Dict, Any, List
import wx.grid as gridlib

import pcbnew

from .config import BoardConfig, PortDef
from .manager import MultiBoardManager
from .constants import BOARDS_DIR


# =============================================================================
# Design System (Original)
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
    OPEN_BG = wx.Colour(255, 243, 224)  # Orange tint for open boards


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
        min_w, min_h = kwargs.pop('min_size', (400, 300))
        size = (max(size[0], min_w), max(size[1], min_h))
        
        style = kwargs.pop('style', wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        super().__init__(parent, title=title, size=size, style=style, **kwargs)
        
        self.SetMinSize((min_w, min_h))
        self.SetBackgroundColour(Colors.PANEL_BG)
        
        self.CentreOnScreen() if parent is None else self.CentreOnParent()
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
        'health': '♥', 'diff': '⇌', 'search': '⌕',
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


class SearchBox(wx.Panel):
    """Search/filter input box."""
    
    def __init__(self, parent, placeholder="Filter..."):
        super().__init__(parent)
        self.SetBackgroundColour(Colors.BACKGROUND)
        
        sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        icon = wx.StaticText(self, label="⌕")
        icon.SetFont(Fonts.body())
        icon.SetForegroundColour(Colors.TEXT_SECONDARY)
        sizer.Add(icon, 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, Spacing.SM)
        
        self.text = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER, size=(200, -1))
        self.text.SetHint(placeholder)
        self.text.SetFont(Fonts.body())
        sizer.Add(self.text, 1, wx.ALL | wx.EXPAND, Spacing.XS)
        
        self.clear_btn = wx.Button(self, label="×", size=(24, 24))
        self.clear_btn.SetToolTip("Clear filter")
        self.clear_btn.Bind(wx.EVT_BUTTON, self._on_clear)
        self.clear_btn.Hide()
        sizer.Add(self.clear_btn, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, Spacing.XS)
        
        self.SetSizer(sizer)
        self.text.Bind(wx.EVT_TEXT, self._on_text_changed)
    
    def _on_text_changed(self, event):
        self.clear_btn.Show(bool(self.text.GetValue()))
        self.Layout()
        event.Skip()
    
    def _on_clear(self, event):
        self.text.SetValue("")
        self.text.SetFocus()
    
    def GetValue(self):
        return self.text.GetValue()
    
    def Bind(self, event_type, handler):
        if event_type == wx.EVT_TEXT:
            self.text.Bind(event_type, handler)
        else:
            super().Bind(event_type, handler)


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
        
        form.Add(self._label(panel, "Port Name"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_name = wx.TextCtrl(panel, value=self.port.name, size=(240, -1))
        form.Add(self.txt_name, 1, wx.EXPAND)
        
        form.Add(self._label(panel, "Net Name"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_net = wx.TextCtrl(panel, value=self.port.net, size=(240, -1))
        form.Add(self.txt_net, 1, wx.EXPAND)
        
        form.Add(self._label(panel, "Board Edge"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.choice_side = wx.Choice(panel, choices=["Left", "Right", "Top", "Bottom"])
        self.choice_side.SetStringSelection(self.port.side.capitalize())
        form.Add(self.choice_side, 0)
        
        form.Add(self._label(panel, "Position"), 0, wx.ALIGN_CENTER_VERTICAL)
        pos_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.slider_pos = wx.Slider(panel, value=int(self.port.position * 100),
                                     minValue=0, maxValue=100, size=(180, -1))
        pos_sizer.Add(self.slider_pos, 1, wx.EXPAND | wx.RIGHT, Spacing.SM)
        self.pos_label = wx.StaticText(panel, label=f"{int(self.port.position * 100)}%", size=(40, -1))
        pos_sizer.Add(self.pos_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form.Add(pos_sizer, 1, wx.EXPAND)
        
        self.slider_pos.Bind(wx.EVT_SLIDER, lambda e: self.pos_label.SetLabel(f"{self.slider_pos.GetValue()}%"))
        
        main.Add(form, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, Spacing.LG)
        main.AddStretchSpacer()
        
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
            return
        if name != self.original_name and name in self.existing_names:
            wx.MessageBox(f"Port '{name}' already exists.", "Validation", wx.ICON_WARNING)
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
        
        header = SectionHeader(panel, "Inter-Board Ports", "Define electrical connection points between boards")
        main.Add(header, 0, wx.ALL | wx.EXPAND, Spacing.LG)
        
        self.list = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SIMPLE)
        self.list.InsertColumn(0, "Port Name", width=150)
        self.list.InsertColumn(1, "Connected Net", width=150)
        self.list.InsertColumn(2, "Edge", width=80)
        self.list.InsertColumn(3, "Position", width=80)
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, lambda e: self._on_edit(e))
        main.Add(self.list, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, Spacing.LG)
        
        action_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_add = IconButton(panel, "Add Port", icon='new', size=(100, 32))
        btn_add.Bind(wx.EVT_BUTTON, self._on_add)
        action_sizer.Add(btn_add, 0, wx.RIGHT, Spacing.SM)
        btn_edit = IconButton(panel, "Edit", icon='edit', size=(80, 32))
        btn_edit.Bind(wx.EVT_BUTTON, self._on_edit)
        action_sizer.Add(btn_edit, 0, wx.RIGHT, Spacing.SM)
        btn_remove = IconButton(panel, "Remove", icon='delete', size=(90, 32))
        btn_remove.Bind(wx.EVT_BUTTON, self._on_remove)
        action_sizer.Add(btn_remove, 0)
        main.Add(action_sizer, 0, wx.ALL, Spacing.LG)
        
        main.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, Spacing.LG)
        
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
        if not name or name not in self.ports:
            return
        other_names = set(self.ports.keys()) - {name}
        dlg = PortEditDialog(self, self.ports[name], existing_names=other_names)
        if dlg.ShowModal() == wx.ID_OK:
            del self.ports[name]
            self.ports[dlg.port.name] = dlg.port
            self._refresh_list()
        dlg.Destroy()
    
    def _on_remove(self, event):
        name = self._get_selected_name()
        if not name:
            return
        if wx.MessageBox(f"Remove port '{name}'?", "Confirm", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION) == wx.YES:
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
        
        header = SectionHeader(panel, "New Sub-Board", "Create a PCB that shares this project's schematic")
        main.Add(header, 0, wx.ALL | wx.EXPAND, Spacing.LG)
        
        form = wx.FlexGridSizer(2, 2, Spacing.SM, Spacing.LG)
        form.AddGrowableCol(1)
        
        lbl_name = wx.StaticText(panel, label="Board Name")
        lbl_name.SetFont(Fonts.body())
        lbl_name.SetForegroundColour(Colors.TEXT_SECONDARY)
        form.Add(lbl_name, 0, wx.ALIGN_CENTER_VERTICAL)
        self.txt_name = wx.TextCtrl(panel, size=(280, -1))
        form.Add(self.txt_name, 1, wx.EXPAND)
        
        lbl_desc = wx.StaticText(panel, label="Description")
        lbl_desc.SetFont(Fonts.body())
        lbl_desc.SetForegroundColour(Colors.TEXT_SECONDARY)
        form.Add(lbl_desc, 0, wx.ALIGN_TOP | wx.TOP, 4)
        self.txt_desc = wx.TextCtrl(panel, size=(280, 60), style=wx.TE_MULTILINE)
        form.Add(self.txt_desc, 1, wx.EXPAND)
        
        main.Add(form, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, Spacing.LG)
        
        info = InfoBanner(panel, "The new board will share the project schematic.\nUse Update to assign components.", style='info')
        main.Add(info, 0, wx.ALL | wx.EXPAND, Spacing.LG)
        main.AddStretchSpacer()
        
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
            return
        if name in self.existing:
            wx.MessageBox(f"Board '{name}' already exists.", "Validation", wx.ICON_WARNING)
            return
        safe = "".join(c if c.isalnum() or c in "_-" else "" for c in name)
        if not safe:
            wx.MessageBox("Name must contain at least one letter or number.", "Validation", wx.ICON_WARNING)
            return
        self.result_name = name
        self.result_desc = self.txt_desc.GetValue().strip()
        self.EndModal(wx.ID_OK)


# =============================================================================
# Health Report Dialog (NEW in v11)
# =============================================================================

class HealthReportDialog(BaseDialog):
    """Board health report."""
    
    def __init__(self, parent, report: Dict[str, Any]):
        super().__init__(parent, "Board Health Report", size=(700, 550), min_size=(600, 450))
        self.report = report
        self._build_ui()
    
    def _build_ui(self):
        panel = wx.Panel(self)
        panel.SetBackgroundColour(Colors.PANEL_BG)
        main = wx.BoxSizer(wx.VERTICAL)
        
        summary = self.report.get("summary", {})
        ok_count = summary.get("ok", 0)
        warn_count = summary.get("warning", 0)
        err_count = summary.get("error", 0)
        
        if err_count > 0:
            status_color, icon, title = Colors.ERROR, "✕", f"{err_count} Issue(s) Found"
        elif warn_count > 0:
            status_color, icon, title = Colors.WARNING, "⚠", f"{warn_count} Warning(s)"
        else:
            status_color, icon, title = Colors.SUCCESS, "✓", "All Boards Healthy"
        
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
        
        stats = wx.StaticText(panel,
            label=f"Project: {self.report.get('project', 'Unknown')}  •  "
                  f"Boards: {self.report.get('total_boards', 0)}  •  "
                  f"✓ {ok_count}  ⚠ {warn_count}  ✕ {err_count}")
        stats.SetFont(Fonts.body())
        stats.SetForegroundColour(Colors.TEXT_SECONDARY)
        main.Add(stats, 0, wx.LEFT | wx.RIGHT, Spacing.LG)
        main.AddSpacer(Spacing.LG)
        
        self.tree = wx.TreeCtrl(panel, style=wx.TR_DEFAULT_STYLE | wx.TR_HIDE_ROOT | wx.BORDER_SIMPLE)
        self.tree.SetFont(Fonts.body())
        
        root = self.tree.AddRoot("Report")
        for board_name, health in self.report.get("boards", {}).items():
            status = health.get("status", "ok")
            icon_char = {"ok": "✓", "warning": "⚠", "error": "✕"}.get(status, "?")
            is_open = "◉ " if health.get("is_open") else ""
            
            node = self.tree.AppendItem(root, f"{icon_char} {is_open}{board_name}")
            self.tree.AppendItem(node, f"Components: {health.get('components', 0)}")
            self.tree.AppendItem(node, f"Ports: {health.get('ports', 0)}")
            self.tree.AppendItem(node, f"Modified: {health.get('last_modified', 'Unknown')}")
            if health.get("is_open"):
                self.tree.AppendItem(node, "◉ Currently open in KiCad")
            if health.get("message"):
                self.tree.AppendItem(node, f"Note: {health['message']}")
        
        self.tree.ExpandAll()
        main.Add(self.tree, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, Spacing.LG)
        
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_sizer.AddStretchSpacer()
        btn_close = wx.Button(panel, wx.ID_CLOSE, "Close")
        btn_close.SetDefault()
        btn_close.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        btn_sizer.Add(btn_close, 0)
        main.Add(btn_sizer, 0, wx.ALL | wx.EXPAND, Spacing.LG)
        panel.SetSizer(main)


# =============================================================================
# Diff View Dialog (NEW in v11)
# =============================================================================

class DiffViewDialog(BaseDialog):
    """Compare two boards."""
    
    def __init__(self, parent, manager: MultiBoardManager):
        super().__init__(parent, "Compare Boards", size=(650, 500), min_size=(550, 400))
        self.manager = manager
        self._build_ui()
    
    def _build_ui(self):
        panel = wx.Panel(self)
        panel.SetBackgroundColour(Colors.PANEL_BG)
        main = wx.BoxSizer(wx.VERTICAL)
        
        header = SectionHeader(panel, "Board Comparison", "Compare component placement between two boards")
        main.Add(header, 0, wx.ALL | wx.EXPAND, Spacing.LG)
        
        select_sizer = wx.BoxSizer(wx.HORIZONTAL)
        boards = list(self.manager.config.boards.keys())
        
        select_sizer.Add(wx.StaticText(panel, label="Board 1:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, Spacing.SM)
        self.choice1 = wx.Choice(panel, choices=boards)
        if boards:
            self.choice1.SetSelection(0)
        select_sizer.Add(self.choice1, 1, wx.RIGHT, Spacing.LG)
        
        select_sizer.Add(wx.StaticText(panel, label="vs"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, Spacing.LG)
        
        select_sizer.Add(wx.StaticText(panel, label="Board 2:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, Spacing.SM)
        self.choice2 = wx.Choice(panel, choices=boards)
        if len(boards) > 1:
            self.choice2.SetSelection(1)
        select_sizer.Add(self.choice2, 1, wx.RIGHT, Spacing.LG)
        
        btn_compare = IconButton(panel, "Compare", icon='diff', size=(100, 32))
        btn_compare.Bind(wx.EVT_BUTTON, self._on_compare)
        select_sizer.Add(btn_compare, 0)
        main.Add(select_sizer, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, Spacing.LG)
        main.AddSpacer(Spacing.MD)
        
        self.results = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.BORDER_SIMPLE)
        self.results.SetFont(Fonts.mono())
        self.results.SetBackgroundColour(wx.Colour(250, 250, 250))
        self.results.SetValue("Select two boards and click Compare.")
        main.Add(self.results, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, Spacing.LG)
        
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_sizer.AddStretchSpacer()
        btn_close = wx.Button(panel, wx.ID_CLOSE, "Close")
        btn_close.SetDefault()
        btn_close.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        btn_sizer.Add(btn_close, 0)
        main.Add(btn_sizer, 0, wx.ALL | wx.EXPAND, Spacing.LG)
        panel.SetSizer(main)
    
    def _on_compare(self, event):
        board1 = self.choice1.GetStringSelection()
        board2 = self.choice2.GetStringSelection()
        
        if not board1 or not board2:
            self.results.SetValue("Please select two boards.")
            return
        if board1 == board2:
            self.results.SetValue("Please select different boards.")
            return
        
        diff = self.manager.get_board_diff(board1, board2)
        
        lines = [
            f"═══ Comparison: {board1} vs {board2} ═══",
            "",
            f"Components in {board1}: {diff['component_count'][board1]}",
            f"Components in {board2}: {diff['component_count'][board2]}",
            f"Common components: {len(diff['common'])}",
            "",
        ]
        
        if diff["only_in_1"]:
            lines.append(f"──── Only in {board1} ({len(diff['only_in_1'])}) ────")
            for ref in diff["only_in_1"][:50]:
                lines.append(f"  {ref}")
            if len(diff["only_in_1"]) > 50:
                lines.append(f"  ... +{len(diff['only_in_1']) - 50} more")
            lines.append("")
        
        if diff["only_in_2"]:
            lines.append(f"──── Only in {board2} ({len(diff['only_in_2'])}) ────")
            for ref in diff["only_in_2"][:50]:
                lines.append(f"  {ref}")
            if len(diff["only_in_2"]) > 50:
                lines.append(f"  ... +{len(diff['only_in_2']) - 50} more")
        
        self.results.SetValue("\n".join(lines))


# =============================================================================
# Connectivity Report Dialog
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
        
        stats = wx.StaticText(panel,
            label=f"Boards checked: {len(boards)}  •  "
                  f"DRC violations: {total_violations}  •  "
                  f"Errors: {len(errors)}")
        stats.SetFont(Fonts.body())
        stats.SetForegroundColour(Colors.TEXT_SECONDARY)
        main.Add(stats, 0, wx.LEFT | wx.RIGHT, Spacing.LG)
        main.AddSpacer(Spacing.LG)
        
        self.text = wx.TextCtrl(panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.BORDER_SIMPLE)
        self.text.SetFont(Fonts.mono())
        self.text.SetBackgroundColour(wx.Colour(250, 250, 250))
        
        lines = []
        for board_name, data in boards.items():
            count = data.get('violations', 0)
            icon_char = "✓" if count == 0 else "⚠"
            lines.append(f"{icon_char} {board_name}: {count} violation(s)")
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
        
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        btn_sizer.AddStretchSpacer()
        btn_close = wx.Button(panel, wx.ID_CLOSE, "Close")
        btn_close.SetDefault()
        btn_close.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CLOSE))
        btn_sizer.Add(btn_close, 0)
        main.Add(btn_sizer, 0, wx.ALL | wx.EXPAND, Spacing.LG)
        panel.SetSizer(main)


# =============================================================================
# Status Dialog
# =============================================================================

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
# Main Dialog (Updated in v11 with search, context menu, health, diff)
# =============================================================================

class MainDialog(BaseDialog):
    """Primary plugin dialog with search, context menu, and PCB open detection."""
    
    def __init__(self, parent, pcb_board: "pcbnew.BOARD"):
        pcb_path = pcb_board.GetFileName()
        project_dir = Path(pcb_path).parent if pcb_path else Path.cwd()
        self.manager = MultiBoardManager(project_dir)
        
        super().__init__(parent, "Multi-Board Manager",
                         size=(1200, 800), min_size=(900, 550))
        
        self._build_ui()
        self._refresh_list()
        
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
    
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

        # Search box row (NEW)
        search_row = wx.BoxSizer(wx.HORIZONTAL)
        self.search_box = SearchBox(content, "Filter boards...")
        self.search_box.Bind(wx.EVT_TEXT, self._on_search)
        search_row.Add(self.search_box, 0, wx.RIGHT, Spacing.LG)
        
        # Open boards indicator (NEW)
        self.open_indicator = wx.StaticText(content, label="")
        self.open_indicator.SetFont(Fonts.small())
        self.open_indicator.SetForegroundColour(Colors.WARNING)
        search_row.Add(self.open_indicator, 0, wx.ALIGN_CENTER_VERTICAL)
        
        search_row.AddStretchSpacer()
        content_sizer.Add(search_row, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND, Spacing.LG)

        # Board table (Grid) with Status column (NEW)
        self.grid = gridlib.Grid(content)
        self.grid.CreateGrid(0, 6)  # Added Status column
        self.grid.SetFont(Fonts.body())

        self.grid.SetRowLabelSize(0)
        self.grid.EnableEditing(False)
        self.grid.EnableGridLines(True)
        self.grid.SetSelectionMode(gridlib.Grid.SelectRows)

        # Column labels
        self.grid.SetColLabelValue(0, "Status")
        self.grid.SetColLabelValue(1, "Board")
        self.grid.SetColLabelValue(2, "Components")
        self.grid.SetColLabelValue(3, "Ports")
        self.grid.SetColLabelValue(4, "Description")
        self.grid.SetColLabelValue(5, "Path")

        # Column widths
        self.grid.SetColSize(0, 70)
        self.grid.SetColSize(1, 140)
        self.grid.SetColSize(2, 120)
        self.grid.SetColSize(3, 60)
        self.grid.SetColSize(4, 450)
        self.grid.SetColSize(5, 280)

        self.grid.Bind(gridlib.EVT_GRID_SELECT_CELL, self._on_grid_select)
        self.grid.Bind(gridlib.EVT_GRID_CELL_LEFT_DCLICK, self._on_open)
        self.grid.Bind(gridlib.EVT_GRID_CELL_RIGHT_CLICK, self._on_context_menu)
        self.grid.Bind(wx.EVT_SIZE, self._on_grid_size)

        content_sizer.Add(self.grid, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, Spacing.LG)

        # ─────────────────────────────────────────────────────────────────────
        # Toolbar
        # ─────────────────────────────────────────────────────────────────────
        toolbar = wx.Panel(content)
        toolbar.SetBackgroundColour(Colors.PANEL_BG)
        tb_sizer = wx.BoxSizer(wx.HORIZONTAL)

        btn_new = IconButton(toolbar, "New", icon='new', size=(80, 34))
        btn_new.SetToolTip("Create a new sub-board (Ctrl+N)")
        btn_new.Bind(wx.EVT_BUTTON, self._on_new)
        tb_sizer.Add(btn_new, 0, wx.RIGHT, Spacing.SM)

        self.btn_remove = IconButton(toolbar, "Remove", icon='delete', size=(95, 34))
        self.btn_remove.SetToolTip("Delete the selected board (Del)")
        self.btn_remove.Bind(wx.EVT_BUTTON, self._on_remove)
        self.btn_remove.Disable()
        tb_sizer.Add(self.btn_remove, 0, wx.RIGHT, Spacing.LG)

        tb_sizer.Add(wx.StaticLine(toolbar, style=wx.LI_VERTICAL), 0, wx.EXPAND | wx.TOP | wx.BOTTOM | wx.RIGHT, Spacing.SM)
        tb_sizer.AddSpacer(Spacing.SM)

        self.btn_open = IconButton(toolbar, "Open", icon='open', size=(80, 34))
        self.btn_open.SetToolTip("Open board in KiCad (Enter)")
        self.btn_open.Bind(wx.EVT_BUTTON, self._on_open)
        self.btn_open.Disable()
        tb_sizer.Add(self.btn_open, 0, wx.RIGHT, Spacing.SM)

        self.btn_update = IconButton(toolbar, "Update", icon='refresh', size=(95, 34))
        self.btn_update.SetToolTip("Sync components from schematic (F5)")
        self.btn_update.Bind(wx.EVT_BUTTON, self._on_update)
        self.btn_update.Disable()
        tb_sizer.Add(self.btn_update, 0, wx.RIGHT, Spacing.SM)

        self.btn_ports = IconButton(toolbar, "Ports", icon='ports', size=(80, 34))
        self.btn_ports.SetToolTip("Define inter-board connection ports")
        self.btn_ports.Bind(wx.EVT_BUTTON, self._on_ports)
        self.btn_ports.Disable()
        tb_sizer.Add(self.btn_ports, 0, wx.RIGHT, Spacing.SM)

        self.btn_edit_desc = IconButton(toolbar, "Description", icon='edit', size=(120, 34))
        self.btn_edit_desc.SetToolTip("Edit the selected board description")
        self.btn_edit_desc.Bind(wx.EVT_BUTTON, self._on_edit_description)
        self.btn_edit_desc.Disable()
        tb_sizer.Add(self.btn_edit_desc, 0, wx.RIGHT, Spacing.LG)

        tb_sizer.Add(wx.StaticLine(toolbar, style=wx.LI_VERTICAL), 0, wx.EXPAND | wx.TOP | wx.BOTTOM | wx.RIGHT, Spacing.SM)
        tb_sizer.AddSpacer(Spacing.SM)

        # NEW: Health and Compare buttons
        btn_health = IconButton(toolbar, "Health", icon='health', size=(90, 34))
        btn_health.SetToolTip("Board health report (Ctrl+H)")
        btn_health.Bind(wx.EVT_BUTTON, self._on_health)
        tb_sizer.Add(btn_health, 0, wx.RIGHT, Spacing.SM)

        btn_diff = IconButton(toolbar, "Compare", icon='diff', size=(100, 34))
        btn_diff.SetToolTip("Compare two boards")
        btn_diff.Bind(wx.EVT_BUTTON, self._on_diff)
        tb_sizer.Add(btn_diff, 0, wx.RIGHT, Spacing.SM)

        btn_check = IconButton(toolbar, "Check All", icon='check', size=(105, 34))
        btn_check.SetToolTip("Run DRC and connectivity checks")
        btn_check.Bind(wx.EVT_BUTTON, self._on_check)
        tb_sizer.Add(btn_check, 0, wx.RIGHT, Spacing.SM)

        btn_status = IconButton(toolbar, "Status", icon='status', size=(85, 34))
        btn_status.SetToolTip("View component placement status")
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
    
    def _on_key(self, event):
        key = event.GetKeyCode()
        ctrl = event.ControlDown() or event.CmdDown()
        
        if key == wx.WXK_ESCAPE:
            self.Close()
        elif key == wx.WXK_F5:
            name = self._get_selected_name()
            if name:
                self._on_update(None)
            else:
                self._refresh_list()
        elif key == wx.WXK_DELETE or key == wx.WXK_BACK:
            self._on_remove(None)
        elif key == wx.WXK_RETURN:
            self._on_open(None)
        elif ctrl and key == ord('N'):
            self._on_new(None)
        elif ctrl and key == ord('H'):
            self._on_health(None)
        elif ctrl and key == ord('F'):
            self.search_box.text.SetFocus()
        else:
            event.Skip()

    def _refresh_list(self):
        self.status_bar.set_status("Refreshing...", 'working')
        wx.Yield()
        
        self.manager._scan_cache = None
        self.manager._health_cache.clear()
        
        filter_text = self.search_box.GetValue().lower()
        
        if self.grid.GetNumberRows() > 0:
            self.grid.DeleteRows(0, self.grid.GetNumberRows())

        placed = self.manager.scan_all_boards()
        counts: Dict[str, int] = {}
        for ref, (board, _) in placed.items():
            counts[board] = counts.get(board, 0) + 1

        current_board = self._get_current_board_name()
        open_boards = self.manager.get_open_boards()

        boards = []
        for name, board in self.manager.config.boards.items():
            if filter_text:
                if filter_text not in name.lower() and filter_text not in (board.description or "").lower():
                    continue
            boards.append((name, board))
        
        if boards:
            self.grid.AppendRows(len(boards))

        wrap_renderer = gridlib.GridCellAutoWrapStringRenderer()

        for row, (name, board) in enumerate(boards):
            is_open = name in open_boards
            is_current = name == current_board
            
            if is_open:
                status = "◉ Open"
            elif is_current:
                status = "→ Current"
            else:
                status = "✓"

            self.grid.SetCellValue(row, 0, status)
            self.grid.SetCellValue(row, 1, name)
            self.grid.SetCellValue(row, 2, str(counts.get(name, 0)))
            self.grid.SetCellValue(row, 3, str(len(board.ports)))
            self.grid.SetCellValue(row, 4, board.description or "—")
            self.grid.SetCellValue(row, 5, board.pcb_path or "")

            self.grid.SetCellRenderer(row, 4, wrap_renderer)
            self.grid.SetCellRenderer(row, 5, wrap_renderer)

            for col in range(6):
                self.grid.SetReadOnly(row, col, True)

            if is_current:
                for col in range(6):
                    self.grid.SetCellBackgroundColour(row, col, Colors.SELECTED)
            elif is_open:
                for col in range(6):
                    self.grid.SetCellBackgroundColour(row, col, Colors.OPEN_BG)
                self.grid.SetCellTextColour(row, 0, Colors.WARNING)

        self._autosize_grid_rows()

        total_boards = len(self.manager.config.boards)
        total_components = sum(counts.values())
        self.board_count_badge.SetLabel(f"{total_boards} board(s)")
        
        if open_boards:
            self.open_indicator.SetLabel(f"◉ {len(open_boards)} board(s) open in KiCad")
        else:
            self.open_indicator.SetLabel("")
        
        self.status_bar.set_status(f"{total_boards} board(s), {total_components} component(s) placed", 'ok')
        self._on_selection_changed(None)

    def _autosize_grid_rows(self):
        if self.grid.GetNumberRows() <= 0:
            return
        self.grid.AutoSizeRows()
        self.grid.ForceRefresh()
    
    def _on_grid_size(self, event):
        event.Skip()
        if self.grid.GetNumberRows() <= 0:
            return
        self.grid.AutoSizeRows()
        self.grid.ForceRefresh()
    
    def _on_search(self, event):
        self._refresh_list()

    def _get_current_board_name(self) -> Optional[str]:
        try:
            board = pcbnew.GetBoard()
            if board:
                pcb_path = Path(board.GetFileName())
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
        name = self.grid.GetCellValue(row, 1)  # Board name is column 1
        return name or None

    def _on_grid_select(self, event):
        event.Skip()
        self._on_selection_changed(event)
    
    def _on_selection_changed(self, event):
        has_selection = self._get_selected_name() is not None
        self.btn_remove.Enable(has_selection)
        self.btn_open.Enable(has_selection)
        self.btn_update.Enable(has_selection)
        self.btn_ports.Enable(has_selection)
        self.btn_edit_desc.Enable(has_selection)
    
    def _on_context_menu(self, event):
        """Show context menu on right-click."""
        row = event.GetRow()
        if row >= 0:
            self.grid.SelectRow(row)
        
        name = self._get_selected_name()
        if not name:
            return
        
        menu = wx.Menu()
        menu.Append(101, "Open Board\tEnter")
        menu.Append(102, "Update from Schematic\tF5")
        menu.Append(103, "Configure Ports...")
        menu.Append(104, "Edit Description...")
        menu.AppendSeparator()
        menu.Append(105, "Health Report")
        menu.Append(106, "Copy Path")
        menu.AppendSeparator()
        menu.Append(107, "Delete Board...\tDel")
        
        board = self.manager.config.boards.get(name)
        if board:
            pcb_path = self.manager.project_dir / board.pcb_path
            if self.manager.is_pcb_open(pcb_path):
                menu.Enable(102, False)
                menu.Enable(107, False)
        
        self.Bind(wx.EVT_MENU, self._on_open, id=101)
        self.Bind(wx.EVT_MENU, self._on_update, id=102)
        self.Bind(wx.EVT_MENU, self._on_ports, id=103)
        self.Bind(wx.EVT_MENU, self._on_edit_description, id=104)
        self.Bind(wx.EVT_MENU, self._on_board_health, id=105)
        self.Bind(wx.EVT_MENU, self._on_copy_path, id=106)
        self.Bind(wx.EVT_MENU, self._on_remove, id=107)
        
        self.PopupMenu(menu)
        menu.Destroy()

    def _on_edit_description(self, event):
        name = self._get_selected_name()
        if not name:
            return
        board = self.manager.config.boards.get(name)
        if not board:
            return

        dlg = wx.TextEntryDialog(self, f"Edit description for '{name}':", "Edit Board Description",
                                  board.description or "", style=wx.OK | wx.CANCEL | wx.TE_MULTILINE)
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
                wx.MessageBox(f"Board '{dlg.result_name}' created.\n\nUse Update to assign components.", "Board Created", wx.ICON_INFORMATION)
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
        
        pcb_path = self.manager.project_dir / board.pcb_path
        if self.manager.is_pcb_open(pcb_path):
            wx.MessageBox(f"Board '{name}' is currently open in KiCad.\n\nPlease close it before deleting.", "Cannot Delete", wx.ICON_WARNING)
            return
        
        result = wx.MessageBox(f"Delete board '{name}'?\n\nThis will permanently remove:\n• The board folder and all files\n• All PCB layout work\n\nThe shared schematic will NOT be affected.",
                               "Confirm Deletion", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING)
        if result != wx.YES:
            return
        
        board_dir = pcb_path.parent
        if board_dir.exists() and BOARDS_DIR in str(board_dir):
            try:
                shutil.rmtree(board_dir)
            except Exception as e:
                wx.MessageBox(f"Could not delete folder:\n{e}", "Warning", wx.ICON_WARNING)
        
        del self.manager.config.boards[name]
        self.manager.save_config()
        self.manager._scan_cache = None
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
        pro_path = pcb_path.with_suffix(".kicad_pro")
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
        board = self.manager.config.boards.get(name)
        if board:
            pcb_path = self.manager.project_dir / board.pcb_path
            if self.manager.is_pcb_open(pcb_path):
                wx.MessageBox(f"Board '{name}' is currently open in KiCad.\n\nPlease close it before updating.", "Cannot Update", wx.ICON_WARNING)
                return
        
        progress = ProgressDialog(self, f"Updating {name}")
        progress.Show()
        wx.Yield()
        self.status_bar.set_status(f"Updating '{name}'...", 'working')
        
        try:
            success, msg = self.manager.update_board(name, progress_callback=lambda p, m: (progress.update(p, m), wx.Yield()))
            progress.Destroy()
            
            if success:
                self.status_bar.set_status(f"Updated '{name}'", 'ok')
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
    
    def _on_health(self, event):
        progress = ProgressDialog(self, "Checking Board Health")
        progress.Show()
        wx.Yield()
        self.status_bar.set_status("Checking health...", 'working')
        
        try:
            report = self.manager.get_full_health_report(progress_callback=lambda p, m: (progress.update(p, m), wx.Yield()))
            progress.Destroy()
            HealthReportDialog(self, report).ShowModal()
            self.status_bar.set_status("Ready", 'ok')
        except Exception as e:
            progress.Destroy()
            self.status_bar.set_status("Health check failed", 'error')
            wx.MessageBox(str(e), "Error", wx.ICON_ERROR)
    
    def _on_board_health(self, event):
        name = self._get_selected_name()
        if not name:
            return
        health = self.manager.get_board_health(name, force=True)
        report = {
            "project": self.manager.project_dir.name,
            "total_boards": 1,
            "boards": {name: health},
            "summary": {"ok": 0, "warning": 0, "error": 0},
        }
        report["summary"][health["status"]] = 1
        HealthReportDialog(self, report).ShowModal()
    
    def _on_diff(self, event):
        if len(self.manager.config.boards) < 2:
            wx.MessageBox("Need at least 2 boards to compare.", "Info", wx.ICON_INFORMATION)
            return
        DiffViewDialog(self, self.manager).ShowModal()
    
    def _on_check(self, event):
        if not self.manager.config.boards:
            wx.MessageBox("No boards to check.", "Info", wx.ICON_INFORMATION)
            return
        
        progress = ProgressDialog(self, "Checking Connectivity")
        progress.Show()
        wx.Yield()
        self.status_bar.set_status("Running checks...", 'working')
        
        try:
            report = self.manager.check_connectivity(progress_callback=lambda p, m: (progress.update(p, m), wx.Yield()))
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
    
    def _on_copy_path(self, event):
        name = self._get_selected_name()
        if not name:
            return
        board = self.manager.config.boards.get(name)
        if board:
            path = str(self.manager.project_dir / board.pcb_path)
            if wx.TheClipboard.Open():
                wx.TheClipboard.SetData(wx.TextDataObject(path))
                wx.TheClipboard.Close()
                self.status_bar.set_status("Path copied to clipboard", 'ok')
    
    def _on_close(self, event):
        self.Destroy()
