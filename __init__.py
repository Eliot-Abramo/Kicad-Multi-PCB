"""
Multi-Board PCB Manager - KiCad Plugin
=======================================

A professional KiCad plugin for managing multiple PCBs from a single schematic.

Features:
- Unified schematic across all boards (via hardlinks)
- Component assignment to specific boards
- Inter-board port connectivity checking
- Auto-packing of placed components
- Visual block footprints for assembly views

Requirements:
- KiCad 9.0+
- kicad-cli (included with KiCad)

Installation:
Copy this folder to your KiCad plugins directory:
- Windows: %APPDATA%/kicad/9.0/scripting/plugins/
- Linux: ~/.local/share/kicad/9.0/scripting/plugins/
- macOS: ~/Library/Application Support/kicad/9.0/scripting/plugins/

Author: Eliot
License: MIT
Version: 10.1
"""

__version__ = "10.2"
__author__ = "Eliot"

import os
import traceback

import pcbnew
import wx

from .dialogs import MainDialog


class MultiBoardPlugin(pcbnew.ActionPlugin):
    """KiCad Action Plugin for multi-board management."""
    
    def defaults(self):
        """Plugin metadata."""
        self.name = "Multi-Board Manager"
        self.category = "Project"
        self.description = "Manage multiple PCBs from a single schematic"
        self.show_toolbar_button = True
        
        icon_path = os.path.join(os.path.dirname(__file__), "icon.png")
        if os.path.exists(icon_path):
            self.icon_file_name = icon_path
    
    def Run(self):
        """Plugin entry point."""
        board = pcbnew.GetBoard()
        
        if not board:
            wx.MessageBox(
                "Please open a PCB file first.",
                "Multi-Board Manager",
                wx.ICON_ERROR
            )
            return
        
        try:
            dialog = MainDialog(None, board)
            dialog.ShowModal()
            dialog.Destroy()
        except Exception as e:
            wx.MessageBox(
                f"Error: {e}\n\n{traceback.format_exc()}",
                "Multi-Board Manager Error",
                wx.ICON_ERROR
            )


# Register plugin
MultiBoardPlugin().register()
