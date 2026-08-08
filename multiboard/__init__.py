"""
Multi-Board PCB Manager - KiCad 10 Action Plugin
================================================

One schematic, many PCBs. See the README for the model.

Why this file is defensive
--------------------------
KiCad discovers plugins by importing every module under its plugins directory,
so anything raising at import time disappears silently from the menu with no
diagnostic. Equally, ``multiboard.cli`` and the test suite import this package
in environments where ``pcbnew`` does not exist at all. So registration is
opt-in: we import the plugin machinery only when pcbnew is present, and any
failure is recorded in :data:`IMPORT_ERROR` rather than raised.

Author: Eliot Abramo
License: MIT
"""

from .version import __version__

__author__ = "Eliot Abramo"
__all__ = ["IMPORT_ERROR", "MultiBoardPlugin", "__author__", "__version__"]

IMPORT_ERROR = None
"""Traceback text if registration failed, else None. Surfaced by Doctor."""

MultiBoardPlugin = None


def _register() -> None:
    """Register the Action Plugin, if we are running inside KiCad."""
    global IMPORT_ERROR, MultiBoardPlugin

    try:
        import pcbnew
    except ImportError:
        return  # CLI or test context; nothing to register

    import os
    import traceback

    try:
        import wx

        class _MultiBoardPlugin(pcbnew.ActionPlugin):
            """Entry point shown under Tools -> External Plugins."""

            def defaults(self):
                self.name = "Multi-Board Manager"
                self.category = "Project"
                self.description = "Manage multiple PCBs from a single schematic"
                self.show_toolbar_button = True
                icon = os.path.join(os.path.dirname(__file__), "icons", "icon.png")
                if os.path.exists(icon):
                    self.icon_file_name = icon
                    self.dark_icon_file_name = icon

            def Run(self):
                # Everything heavy is imported here, not at registration time.
                # A failure anywhere in the UI or backend would otherwise abort
                # registration, and the plugin would simply not appear in the
                # menu -- no error, nothing to search for. Registering first and
                # importing on demand turns that into a message you can read.
                try:
                    from .compat import install_hint, require_supported
                    from .ui.main_dialog import MainDialog
                except Exception as exc:
                    wx.MessageBox(
                        f"Multi-Board Manager could not load.\n\n{exc}\n\n{traceback.format_exc()}",
                        "Multi-Board Manager",
                        wx.OK | wx.ICON_ERROR,
                    )
                    return

                # Tell discovery which KiCad we are inside before anything looks.
                install_hint()

                try:
                    require_supported()
                except Exception as exc:
                    wx.MessageBox(str(exc), "Multi-Board Manager", wx.OK | wx.ICON_ERROR)
                    return

                board = pcbnew.GetBoard()
                if board is None:
                    wx.MessageBox(
                        "Open a PCB first.\n\n"
                        "Multi-Board Manager reads the active board to work out which "
                        "project you are in. Any board in the project will do.",
                        "Multi-Board Manager",
                        wx.OK | wx.ICON_INFORMATION,
                    )
                    return

                # The dialog is built by a factory so the (filesystem-touching)
                # manager is constructed before the wx object exists. v12 built
                # it inside __init__ ahead of super().__init__, which left a
                # half-constructed wx.Dialog behind on any failure.
                try:
                    MainDialog.open(None, board)
                except Exception as exc:
                    wx.MessageBox(
                        f"Multi-Board Manager could not start.\n\n{exc}\n\n{traceback.format_exc()}",
                        "Multi-Board Manager",
                        wx.OK | wx.ICON_ERROR,
                    )

        MultiBoardPlugin = _MultiBoardPlugin
        _MultiBoardPlugin().register()

    except Exception:
        IMPORT_ERROR = traceback.format_exc()


_register()
