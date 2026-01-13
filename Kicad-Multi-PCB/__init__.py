"""
Multi-Board PCB Manager - KiCad Plugin
======================================

A KiCad Action Plugin for managing multiple PCB files that share a single
schematic source of truth.

How KiCad Sees This Module
--------------------------
KiCad loads Python plugins by importing modules in the plugins directory.
For Action Plugins, KiCad expects a subclass of pcbnew.ActionPlugin with
.register() called so it appears in the UI.

This file is intentionally minimal:
- Wires the plugin into KiCad
- Opens the main wx dialog (defined in dialogs.py)
- Catches exceptions and shows useful tracebacks

KiCad Integration Notes
-----------------------
1. Running inside KiCad: If you block the UI thread for too long, KiCad
   freezes. Long actions use progress dialogs + wx.Yield()

2. pcbnew.GetBoard(): Only works if a PCB editor window is active.
   If the user launches the plugin with no board open, we bail early.

3. Exception handling: A thrown exception in a KiCad plugin can leave
   the UI in a weird state. Always be defensive.

Author: Eliot Abramo
License: MIT
"""

__version__ = "12.0"
__author__ = "Eliot Abramo"

import os
import traceback

import pcbnew
import wx

from .dialogs import MainDialog


class MultiBoardPlugin(pcbnew.ActionPlugin):
    """KiCad Action Plugin for multi-board management."""

    def defaults(self):
        """Set plugin metadata (called by KiCad during discovery)."""
        self.name = "Multi-Board Manager"
        self.category = "Project"
        self.description = "Manage multiple PCBs from a single schematic"
        self.show_toolbar_button = True

        icon_path = os.path.join(os.path.dirname(__file__), "icon.png")
        if os.path.exists(icon_path):
            self.icon_file_name = icon_path

    def Run(self):
        """Plugin entry point (called when user clicks toolbar/menu)."""
        board = pcbnew.GetBoard()

        if not board:
            wx.MessageBox(
                "Please open a PCB file first.\n\n"
                "The Multi-Board Manager needs an active PCB to determine "
                "the project location.",
                "Multi-Board Manager",
                wx.ICON_ERROR,
            )
            return

        try:
            dialog = MainDialog(None, board)
            dialog.ShowModal()
            dialog.Destroy()
        except Exception as e:
            wx.MessageBox(
                f"An error occurred:\n\n{e}\n\n"
                f"Details:\n{traceback.format_exc()}",
                "Multi-Board Manager Error",
                wx.ICON_ERROR,
            )


# Register the plugin with KiCad
MultiBoardPlugin().register()
