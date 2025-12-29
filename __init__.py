"""
Multi-Board PCB Manager - KiCad Action Plugin
==============================================

A KiCad plugin for managing multiple PCBs from a single schematic.

OVERVIEW
--------
This plugin enables a hierarchical multi-board workflow where one "root"
schematic drives multiple sub-PCBs. Each sub-PCB can have its own stackup,
design rules, and manufacturing outputs while sharing the same schematic.

KEY FEATURES
------------
- Unified Schematic: One schematic, multiple PCBs. Edit once, see everywhere.
- Component Assignment: Place components on specific boards during update.
- Inter-Board Ports: Define connections between boards for connectivity checking.
- Block Footprints: Visual representations of sub-boards for assembly views.
- DRC Integration: Run design rule checks across all boards.
- Cross-Probing: Full schematic-to-PCB linkage maintained.

SCHEMATIC SYNCHRONIZATION
-------------------------
The plugin uses filesystem hardlinks to share schematic files. This means:

    ROOT/project.kicad_sch  ─┬─> Same file on disk
    boards/PSU/PSU.kicad_sch ─┘

Changes made to either path immediately affect both because they ARE the same
file. This is superior to copying because there's no sync delay and no version
conflicts.

LIMITATION: If you have the same schematic open in two KiCad windows, each
window caches the file in memory. You'll need to close and reopen to see
changes from the other window. This is how all applications behave with
file-based documents, not a plugin limitation.

USAGE
-----
1. Open your main PCB in KiCad
2. Run the plugin from Tools > External Plugins > Multi-Board Manager
3. Create new sub-boards (New button)
4. Add components to a board by selecting it and clicking Update
5. Open each sub-board's PCB for layout (double-click or Open button)
6. Define inter-board connections using Ports
7. Run Check to verify connectivity across all boards

REQUIREMENTS
------------
- KiCad 9.0 or later
- Python 3.9+ (included with KiCad)
- kicad-cli must be accessible (included with KiCad installation)

INSTALLATION
------------
Copy this folder to your KiCad plugins directory:
- Windows: %APPDATA%/kicad/9.0/scripting/plugins/
- Linux: ~/.local/share/kicad/9.0/scripting/plugins/
- macOS: ~/Library/Application Support/kicad/9.0/scripting/plugins/

LICENSE
-------
MIT License - See LICENSE file for details.

Author: Eliot
Version: 10.0
"""

__version__ = "10.0"
__author__ = "Eliot"

import os
import traceback

import pcbnew
import wx

from .dialogs import MainDialog


class MultiBoardPlugin(pcbnew.ActionPlugin):
    """
    KiCad Action Plugin for multi-board PCB management.
    
    Registers the plugin with KiCad and provides the Run() entry point
    that's called when the user activates the plugin.
    """
    
    def defaults(self):
        """Set plugin metadata shown in KiCad's plugin manager."""
        self.name = "Multi-Board Manager"
        self.category = "Project"
        self.description = "Manage multiple PCBs from one schematic"
        self.show_toolbar_button = True
        
        # Icon path (optional - place icon.png in plugin directory)
        icon_path = os.path.join(os.path.dirname(__file__), "icon.png")
        if os.path.exists(icon_path):
            self.icon_file_name = icon_path
    
    def Run(self):
        """
        Plugin entry point - called when user activates the plugin.
        
        Opens the main dialog if a PCB is currently loaded.
        """
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


# Register the plugin with KiCad
MultiBoardPlugin().register()
