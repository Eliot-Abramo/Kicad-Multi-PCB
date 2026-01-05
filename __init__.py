"""
Multi-Board PCB Manager - KiCad Plugin
=======================================

How KiCad sees this folder
--------------------------
KiCad loads Python plugins by importing the module in your plugins directory.
For Action Plugins, KiCad expects a subclass of `pcbnew.ActionPlugin` and it
expects you to call `.register()` so it shows up in the UI.

This file is intentionally tiny:
- It wires the plugin into KiCad.
- It opens the main wx dialog (defined in dialogs.py).
- It catches exceptions and shows a useful traceback instead of silently dying.

KiCad subtleties
-----------------------------------------------------
1) You’re running inside KiCad.
   If you block the UI thread for long, KiCad freezes. That’s why long actions
   use progress dialogs + occasional `wx.Yield()` elsewhere.

2) `pcbnew.GetBoard()` only works if a PCB editor window is active.
   If the user launches the plugin with no board open, we bail early.

3) Always be defensive with exceptions.
   A thrown exception in a KiCad plugin is not like a normal Python script —
   it can leave the UI in a weird state

Author: Eliot
License: MIT
"""

__version__ = "11.2"
__author__ = "Eliot"

import os
import traceback

import pcbnew
import wx

from .dialogs import MainDialog


class MultiBoardPlugin(pcbnew.ActionPlugin):
    """KiCad Action Plugin for multi-board management."""
    
    def defaults(self):
        # Called by KiCad when it discovers the plugin.
        # This is just metadata + icon wiring.
        """Plugin metadata."""
        self.name = "Multi-Board Manager"
        self.category = "Project"
        self.description = "Manage multiple PCBs from a single schematic"
        self.show_toolbar_button = True
        
        icon_path = os.path.join(os.path.dirname(__file__), "icon.png")
        if os.path.exists(icon_path):
            self.icon_file_name = icon_path
    
    def Run(self):
        # Called when the user clicks the toolbar button / menu entry.
        # We should keep this fast and user-friendly.
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
