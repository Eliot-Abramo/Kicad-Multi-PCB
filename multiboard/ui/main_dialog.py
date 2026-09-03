# Copyright (c) 2026 Eliot Abramo
# SPDX-License-Identifier: MIT

"""
The main window: Boards and Components as peer views.

Components is a tab, not a buried dialog, because "where does this part live?"
is the question the tool exists to answer and it should never be more than one
click away. Ctrl+P gets there without even that.

Key handling starts with a focus check everywhere. v12 bound Backspace to
"delete board" on the dialog's ``EVT_CHAR_HOOK`` with no such check, so its
filter box prompted to delete a board as soon as you corrected a typo.
"""

from pathlib import Path
from typing import Optional

import wx

from ..core import focus as focus_mod
from ..core.doctor import ERROR, WARN
from ..core.index import ComponentRecord
from ..manager import BoardBusy, MultiBoardManager
from .dialogs import (
    DoctorDialog,
    NewBoardDialog,
    OnboardingDialog,
    PortsDialog,
    ReportDialog,
)
from .palette import open_palette
from .plan_dialog import ResultDialog, review_plan
from .rules_dialog import edit_rules
from .theme import apply_chrome, apply_grid, bind_theme_changes, get_theme, set_row_colors
from .widgets import (
    SPACING_LG,
    SPACING_MD,
    SPACING_SM,
    Badge,
    Banner,
    BaseDialog,
    SearchBox,
    StatusBar,
    confirm,
    message,
    run_with_progress,
    show_modal,
    typing_in_text,
)
from .xref_view import XrefPanel

BOARD_COLUMNS = [
    ("", 34),
    ("Board", 150),
    ("Placed", 70),
    ("Pending", 70),
    ("Conflicts", 75),
    ("Ports", 55),
    ("Description", 300),
    ("Path", 260),
]


class MainDialog(BaseDialog):
    """Boards, components, and everything reachable from them."""

    # -- construction ------------------------------------------------------

    @classmethod
    def open(cls, parent, pcb_board) -> None:
        """
        Build and show the window.

        A factory rather than a constructor because the manager does real
        filesystem work and must be built *before* the wx object exists. v12
        built it inside ``__init__`` ahead of ``super().__init__``, so a failure
        left a half-constructed ``wx.Dialog`` behind.
        """
        path = pcb_board.GetFileName() if pcb_board else ""
        project_dir = Path(path).parent if path else Path.cwd()

        try:
            manager = MultiBoardManager(project_dir)
        except Exception as exc:
            wx.MessageBox(
                f"Could not open the project at {project_dir}.\n\n{exc}",
                "Multi-Board Manager",
                wx.OK | wx.ICON_ERROR,
            )
            return

        dialog = cls(parent, manager)
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()

    def __init__(self, parent, manager: MultiBoardManager):
        super().__init__(parent, "Multi-Board Manager", size=(1240, 820), min_size=(940, 620))
        self.manager = manager
        self.ws = manager.ws
        self._board_rows: list[str] = []

        self._build()
        bind_theme_changes(self, self.retheme)
        self.Bind(wx.EVT_CHAR_HOOK, self._on_key)
        self.Bind(wx.EVT_CLOSE, lambda e: (self.dismiss(), None))

        wx.CallAfter(self._first_run)

    # -- layout ------------------------------------------------------------

    def _build(self) -> None:
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(self._header(), 0, wx.EXPAND)

        self.banner = Banner(self, "", "warning", "Open Doctor", self._on_doctor)
        self.banner.Hide()
        outer.Add(self.banner, 0, wx.ALL | wx.EXPAND, SPACING_SM)

        self.notebook = wx.Notebook(self)
        self.boards_page = self._boards_page(self.notebook)
        self.notebook.AddPage(self.boards_page, "Boards")

        self.xref = XrefPanel(
            self.notebook,
            self.ws.index,
            on_jump=self._jump_to,
            on_assign=self._assign,
            on_adopt=self._adopt,
            board_names=lambda: sorted(self.ws.config.boards),
            board_color=self._board_color,
        )
        self.notebook.AddPage(self.xref, "Components")
        outer.Add(self.notebook, 1, wx.ALL | wx.EXPAND, SPACING_SM)

        self.status = StatusBar(self)
        outer.Add(self.status, 0, wx.EXPAND)
        self.SetSizer(outer)

    def _header(self) -> wx.Panel:
        panel = wx.Panel(self)
        apply_chrome(panel, self.theme, background=self.theme.header_bg)
        sizer = wx.BoxSizer(wx.HORIZONTAL)

        titles = wx.BoxSizer(wx.VERTICAL)
        title = wx.StaticText(panel, label="Multi-Board Manager")
        title.SetFont(self.theme.header_font())
        title.SetForegroundColour(self.theme.header_text)
        titles.Add(title)

        self.subtitle = wx.StaticText(panel, label=str(self.ws.root))
        self.subtitle.SetFont(self.theme.small_font())
        self.subtitle.SetForegroundColour(self.theme.header_muted)
        titles.Add(self.subtitle, 0, wx.TOP, 2)
        sizer.Add(titles, 1, wx.ALL, SPACING_LG)

        find = wx.Button(panel, label="Find component  (Ctrl+P)")
        find.Bind(wx.EVT_BUTTON, lambda e: self._on_palette())
        find.SetToolTip("Search every board at once")
        sizer.Add(find, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, SPACING_SM)

        for label, handler, tip in (
            ("Rules", self._on_rules, "Decide which components belong on which board"),
            ("Doctor", self._on_doctor, "Check the project for problems"),
        ):
            button = wx.Button(panel, label=label)
            button.Bind(wx.EVT_BUTTON, lambda e, h=handler: h())
            button.SetToolTip(tip)
            sizer.Add(button, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, SPACING_SM)

        sizer.AddSpacer(SPACING_MD)
        panel.SetSizer(sizer)
        return panel

    def _boards_page(self, parent) -> wx.Panel:
        import wx.grid as gridlib

        panel = wx.Panel(parent)
        sizer = wx.BoxSizer(wx.VERTICAL)

        top = wx.BoxSizer(wx.HORIZONTAL)
        self.board_filter = SearchBox(panel, "Filter boards...", on_change=lambda _v: self._refresh_boards())
        top.Add(self.board_filter, 0, wx.RIGHT, SPACING_MD)
        self.open_badge = Badge(panel, "", self.theme.warning)
        self.open_badge.Hide()
        top.Add(self.open_badge, 0, wx.ALIGN_CENTER_VERTICAL)
        top.AddStretchSpacer()
        sizer.Add(top, 0, wx.ALL | wx.EXPAND, SPACING_SM)

        self.grid = gridlib.Grid(panel)
        self.grid.CreateGrid(0, len(BOARD_COLUMNS))
        for i, (label, width) in enumerate(BOARD_COLUMNS):
            self.grid.SetColLabelValue(i, label)
            self.grid.SetColSize(i, width)
        self.grid.SetRowLabelSize(0)
        self.grid.EnableEditing(False)
        self.grid.SetSelectionMode(gridlib.Grid.SelectRows)
        self.grid.Bind(gridlib.EVT_GRID_CELL_LEFT_DCLICK, lambda e: self._on_open())
        self.grid.Bind(gridlib.EVT_GRID_CELL_RIGHT_CLICK, self._on_board_menu)
        self.grid.Bind(gridlib.EVT_GRID_SELECT_CELL, lambda e: (self._sync_buttons(), e.Skip()))
        sizer.Add(self.grid, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, SPACING_SM)

        tools = wx.BoxSizer(wx.HORIZONTAL)
        self._buttons = {}
        for key, label, handler, needs_selection in (
            ("new", "New board...", self._on_new, False),
            ("open", "Open in KiCad", self._on_open, True),
            ("update", "Update...", self._on_update, True),
            ("ports", "Ports...", self._on_ports, True),
            ("delete", "Delete", self._on_delete, True),
            ("refresh", "Refresh", self._on_refresh, False),
            ("drc", "Run DRC", self._on_drc, False),
        ):
            button = wx.Button(panel, label=label)
            button.Bind(wx.EVT_BUTTON, lambda e, h=handler: h())
            if needs_selection:
                button.Disable()
                self._buttons[key] = button
            tools.Add(button, 0, wx.RIGHT, SPACING_SM)
        sizer.Add(tools, 0, wx.ALL, SPACING_SM)

        panel.SetSizer(sizer)
        apply_grid(self.grid, self.theme)
        return panel

    # -- lifecycle ---------------------------------------------------------

    def _first_run(self) -> None:
        """Onboard, consume any pending focus request, then load."""
        if not self.ws.is_configured():
            self._onboard()

        self._refresh(force=False)

        active = self.manager.backend.active_board_path()
        if active:
            board = self._board_for_path(active)
            if board:
                ref = focus_mod.take(self.ws.root, board)
                if ref:
                    self._reveal_here(ref)

    def _onboard(self) -> None:
        from ..core.netlist import top_level_sheets
        from ..core.project import detect_root_files

        detected = detect_root_files(self.ws.root)
        if not detected:
            return

        # Export once so the wizard can offer boards from real sheet names.
        sheets: list[str] = []
        candidate = next((c for c in detected if c["has_schematic"]), None)
        if candidate:
            self.ws.config.root_schematic = candidate["schematic"]
            self.ws.config.root_pcb = candidate["pcb"]
            try:
                result = self.ws.refresh(export=True)
                if not result.netlist_error:
                    sheets = top_level_sheets(self.ws.index.schematic)
            except Exception:
                sheets = []

        dialog = OnboardingDialog(self, self.ws.root, sheets, detected)
        try:
            if dialog.ShowModal() != wx.ID_OK or not dialog.result:
                return
            outcome = dialog.result
        finally:
            dialog.Destroy()

        self.ws.config.root_schematic = outcome["root_schematic"]
        self.ws.config.root_pcb = outcome["root_pcb"]
        self.ws.save_config()

        created, failed = [], []
        for name in outcome["boards"]:
            try:
                self.manager.create_board(name)
                created.append(name)
            except Exception as exc:
                failed.append(f"{name}: {exc}")

        if outcome["rules"]:
            self.ws.config.rules.extend(r for r in outcome["rules"] if r.board in self.ws.config.boards)
            self.ws.save_config()

        if failed:
            message(
                self, "Some boards could not be created:\n\n" + "\n".join(failed), "Setup", wx.ICON_WARNING
            )
        elif created:
            message(
                self,
                f"Created {len(created)} board(s): {', '.join(created)}.\n\n"
                "Open each one and press Update to pull its components in.",
                "Setup complete",
            )

    def _refresh(self, *, force: bool = False) -> None:
        """Re-index in the background behind a progress dialog."""

        def work(progress, cancelled):
            return self.ws.refresh(force=force, progress=progress, cancel=cancelled)

        self.status.set_status("Reading boards and schematic...", "working")
        result = run_with_progress(self, "Refreshing", work)

        self._refresh_boards()
        self.xref.refresh_view()

        if result.cancelled:
            # Cancelling is a normal outcome now that long steps are interruptible;
            # say so plainly rather than reporting stale counts as if they were fresh.
            self.status.set_status("Refresh cancelled - showing the previous index", "warning")
            return

        self._update_banner(result)

        stats = result.stats
        if stats:
            self.status.set_status(
                f"{stats.components} component(s), {stats.placed} placed, "
                f"{stats.conflicts} conflict(s)  -  {len(self.ws.config.boards)} board(s)",
                "warning" if stats.conflicts else "ok",
            )

    def _update_banner(self, result=None) -> None:
        if result is not None and result.netlist_error:
            self.banner.set_message(
                f"Schematic data is unavailable: {result.netlist_error.splitlines()[0]}",
                "error",
            )
            self.banner.Show()
            self.Layout()
            return

        report = self.ws.doctor(backend=self.manager.backend)
        problems = [c for c in report.checks if c.level == ERROR]
        warnings = [c for c in report.checks if c.level == WARN]
        if problems:
            self.banner.set_message(f"{problems[0].title}. Run Doctor to fix it.", "error")
            self.banner.Show()
        elif warnings:
            self.banner.set_message(
                f"{len(warnings)} thing(s) need attention: {warnings[0].title}.", "warning"
            )
            self.banner.Show()
        else:
            self.banner.Hide()
        self.Layout()

    def _refresh_boards(self) -> None:
        needle = self.board_filter.GetValue().strip().lower()
        counts = self.ws.index.board_counts()
        open_boards = self.ws.open_boards()
        current = self._current_board()

        rows = [
            (name, board)
            for name, board in sorted(self.ws.config.boards.items())
            if not needle or needle in name.lower() or needle in (board.description or "").lower()
        ]
        self._board_rows = [name for name, _ in rows]

        grid = self.grid
        grid.BeginBatch()
        try:
            if grid.GetNumberRows():
                grid.DeleteRows(0, grid.GetNumberRows())
            if rows:
                grid.AppendRows(len(rows))

            for row, (name, board) in enumerate(rows):
                c = counts.get(name, {})
                marker = "open" if name in open_boards else "here" if name == current else ""
                grid.SetCellValue(row, 0, marker)
                grid.SetCellValue(row, 1, name)
                grid.SetCellValue(row, 2, str(c.get("placed", 0)))
                grid.SetCellValue(row, 3, str(c.get("pending", 0)))
                grid.SetCellValue(row, 4, str(c.get("conflicts", 0)))
                grid.SetCellValue(row, 5, str(len(board.ports)))
                grid.SetCellValue(row, 6, board.description or "")
                grid.SetCellValue(row, 7, board.pcb_path)

                accent = self._board_color(name)
                bg = self.theme.tint(accent, 0.12)
                fg = self.theme.readable(self.theme.text, bg)
                set_row_colors(grid, row, len(BOARD_COLUMNS), bg, fg)
                grid.SetCellTextColour(row, 1, self.theme.readable(accent, bg))
                if c.get("conflicts"):
                    grid.SetCellTextColour(row, 4, self.theme.readable(self.theme.error, bg))
        finally:
            grid.EndBatch()
        grid.ForceRefresh()

        if open_boards:
            self.open_badge.set_label(f"{len(open_boards)} open in KiCad", self.theme.warning)
            self.open_badge.Show()
        else:
            self.open_badge.Hide()
        self.boards_page.Layout()
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        name = self._selected_board()
        open_boards = self.ws.open_boards() if name else set()
        for key, button in self._buttons.items():
            enabled = name is not None
            if key in ("update", "delete") and name in open_boards:
                enabled = False
            button.Enable(enabled)

    # -- helpers -----------------------------------------------------------

    def _board_color(self, name: str) -> wx.Colour:
        return self.theme.board_color(self.ws.board_color(name, dark_mode=self.theme.is_dark))

    def _selected_board(self) -> Optional[str]:
        rows = self.grid.GetSelectedRows()
        row = rows[0] if rows else self.grid.GetGridCursorRow()
        return self._board_rows[row] if 0 <= row < len(self._board_rows) else None

    def _current_board(self) -> Optional[str]:
        active = self.manager.backend.active_board_path()
        return self._board_for_path(active) if active else None

    def _board_for_path(self, path: Path) -> Optional[str]:
        for name in self.ws.config.boards:
            pcb = self.ws.board_pcb(name)
            if pcb is None:
                continue
            try:
                if pcb.resolve() == path.resolve():
                    return name
            except OSError:
                if pcb.name == path.name:
                    return name
        return None

    # -- keys --------------------------------------------------------------

    def _on_key(self, event: wx.KeyEvent) -> None:
        # This guard is the fix for v12's unusable filter box.
        if typing_in_text():
            event.Skip()
            return

        key = event.GetKeyCode()
        ctrl = event.ControlDown() or event.CmdDown()

        if ctrl and key == ord("P"):
            self._on_palette()
        elif ctrl and key == ord("F"):
            self.notebook.SetSelection(1)
            self.xref.search.SetFocus()
        elif ctrl and key == ord("N"):
            self._on_new()
        elif ctrl and key == ord("R"):
            self._on_rules()
        elif key == wx.WXK_F5:
            self._on_refresh()
        elif key == wx.WXK_ESCAPE:
            self.dismiss()
        elif key == wx.WXK_DELETE and self.notebook.GetSelection() == 0:
            # Delete only, never Backspace, and only from the boards grid.
            if self._grid_has_focus():
                self._on_delete()
            else:
                event.Skip()
        elif key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            if self.notebook.GetSelection() == 0 and self._grid_has_focus():
                self._on_open()
            else:
                event.Skip()
        else:
            event.Skip()

    def _grid_has_focus(self) -> bool:
        """
        Whether keyboard focus is in the boards grid.

        ``Grid.HasFocus()`` is not the question to ask: a wx.grid.Grid is a
        composite, and on GTK focus lives on its child GridWindow, so the plain
        check was always False there and Delete and Enter silently did nothing.
        """
        window = wx.Window.FindFocus()
        while window is not None:
            if window is self.grid:
                return True
            window = window.GetParent()
        return False

    # -- actions -----------------------------------------------------------

    def _on_palette(self) -> None:
        open_palette(self, self.ws.index, self._jump_to, self._run_command, self._board_color)

    def _run_command(self, command: str) -> None:
        {
            "reindex": lambda: self._on_refresh(),
            "doctor": self._on_doctor,
            "rules": self._on_rules,
            "conflicts": self._show_conflicts,
            "drc": self._on_drc,
            "xref": lambda: self.notebook.SetSelection(1),
        }.get(command, lambda: None)()

    def _show_conflicts(self) -> None:
        self.notebook.SetSelection(1)
        self.xref._only_conflicts()

    def _jump_to(self, record: ComponentRecord) -> None:
        """
        Take the user to a component.

        On the currently open board this selects and zooms to it. On any other
        board KiCad 10 gives us no way to drive a different document, so we leave
        a one-shot request and open the board; the plugin consumes it next time
        it starts with that board active.
        """
        if not record.placements:
            message(self, f"{record.ref} is not placed on any board.\n\n{record.hint()}", record.ref)
            return

        target = record.placements[0].board
        current = self._current_board()

        if target == current and self._reveal(record.ref):
            self.status.set_status(f"Selected {record.ref} on {target}", "ok")
            return

        if len(record.placements) > 1:
            boards = ", ".join(record.boards)
            message(
                self,
                f"{record.ref} is placed on {boards}.\n\nDelete it from all but one board, then try again.",
                "Duplicate placement",
                wx.ICON_WARNING,
            )
            return

        if not confirm(
            self,
            f"{record.ref} is on '{target}', which is not open.\n\n"
            "Open that board in KiCad and reveal the component?",
            "Go to component",
            wx.ICON_QUESTION,
        ):
            return

        focus_mod.request(self.ws.root, target, record.ref)
        self._open_board(target)

    def _reveal(self, ref: str) -> bool:
        """
        Point KiCad's canvas at ``ref``. Returns whether it is on the open board.

        The lookup is synchronous -- the caller needs the answer now -- but the
        canvas work is deferred. ``FocusOnItem`` runs a KiCad ``TOOL_MANAGER``
        action, and running one from inside a dialog's event handler, while a
        modal loop owns the event queue, is how a plugin takes the editor down.
        ``CallAfter`` puts it on the queue instead, so the current handler has
        fully returned before KiCad's canvas is touched.
        """
        if not self.manager.backend.has_reference(ref):
            return False
        wx.CallAfter(self.manager.backend.focus_reference, ref)
        return True

    def _reveal_here(self, ref: str) -> None:
        if self._reveal(ref):
            self.status.set_status(f"Revealed {ref} on this board", "ok")

    def _assign(self, refs: list[str], board: Optional[str]) -> None:
        changed = self.ws.assign(refs, board)
        self.xref.refresh_view()
        self._refresh_boards()
        where = f"assigned to {board}" if board else "unassigned"
        self.status.set_status(f"{changed} component(s) {where}", "ok")

    def _adopt(self, refs: list[str]) -> None:
        changed = self.ws.adopt_placements(refs)
        self.xref.refresh_view()
        self._refresh_boards()
        self.status.set_status(f"{changed} placement(s) adopted as intent", "ok")

    def _on_rules(self) -> None:
        rules = edit_rules(
            self,
            self.ws.config.rules,
            sorted(self.ws.config.boards),
            self.ws.index.schematic,
            self.ws.suggest_rules,
        )
        if rules is None:
            return
        self.ws.config.rules = rules
        self.ws.save_config()
        self.ws.reclassify()
        self.xref.refresh_view()
        self._refresh_boards()
        self.status.set_status(f"{len(rules)} assignment rule(s) applied", "ok")

    def _on_new(self) -> None:
        dialog = NewBoardDialog(self, sorted(self.ws.config.boards))
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            name, description = dialog.board_name, dialog.description
        finally:
            dialog.Destroy()

        try:
            outcome = self.manager.create_board(name, description)
        except Exception as exc:
            message(self, f"Could not create '{name}'.\n\n{exc}", "New board", wx.ICON_ERROR)
            return

        self._refresh(force=True)
        note = ("\n\n" + "\n".join(outcome.warnings)) if outcome.warnings else ""
        self.status.set_status(f"Created board '{name}'", "ok")
        if note:
            message(self, f"Board '{name}' created.{note}", "New board", wx.ICON_WARNING)

    def _on_open(self) -> None:
        name = self._selected_board()
        if name:
            self._open_board(name)

    def _open_board(self, name: str) -> None:
        """
        Launch KiCad on another board.

        The child gets ``child_env()``, not our own environment. KiCad's embedded
        Python exports PYTHONHOME and PYTHONPATH, and a KiCad started with those
        inherited tries to initialise against the wrong interpreter and dies with
        an opaque error -- the same trap ``core.kicad_env`` already documents for
        kicad-cli. It is also detached, so it survives this editor closing.
        """
        import platform
        import subprocess

        from ..core.kicad_env import child_env

        pcb = self.ws.board_pcb(name)
        if pcb is None or not pcb.exists():
            message(self, f"'{name}' has no PCB file on disk.", "Open", wx.ICON_ERROR)
            return

        project = pcb.with_suffix(".kicad_pro")
        target = project if project.exists() else pcb

        system = platform.system()
        if system == "Windows":
            commands = [["cmd", "/c", "start", "", str(target)]]
        elif system == "Darwin":
            commands = [["open", str(target)]]
        else:
            commands = [["kicad", str(target)], ["pcbnew", str(pcb)], ["xdg-open", str(target)]]

        detach = {"start_new_session": True} if system != "Windows" else {}
        failures = []
        for argv in commands:
            try:
                subprocess.Popen(argv, env=child_env(), **detach)
                self.status.set_status(f"Opening '{name}' in KiCad...", "working")
                return
            except (OSError, ValueError) as exc:
                failures.append(f"{argv[0]}: {exc}")

        message(
            self,
            f"Could not open '{name}'.\n\n" + "\n".join(failures) + f"\n\nThe file is at:\n{target}",
            "Open",
            wx.ICON_ERROR,
        )

    def _on_update(self) -> None:
        """Plan, review, then apply. Nothing is written before the review."""
        name = self._selected_board()
        if not name:
            return

        plan = self.ws.plan_update(name)
        if plan.is_noop():
            message(self, f"'{name}' is already in sync with the schematic.", "Update")
            return

        reviewed = review_plan(self, plan)
        if reviewed is None:
            return

        def work(progress, cancelled):
            return self.manager.apply_update(name, reviewed, progress=progress, cancel=cancelled)

        try:
            result = run_with_progress(self, f"Updating {name}", work)
        except BoardBusy as exc:
            message(self, str(exc), "Board is open", wx.ICON_WARNING)
            return
        except Exception as exc:
            message(self, f"The update failed.\n\n{exc}", "Update", wx.ICON_ERROR)
            return

        self._refresh(force=True)
        show_modal(ResultDialog(self, name, result))

    def _on_ports(self) -> None:
        name = self._selected_board()
        if not name:
            return
        board = self.ws.config.boards[name]

        dialog = PortsDialog(self, board)
        try:
            if dialog.ShowModal() != wx.ID_OK:
                return
            board.ports = dialog.ports
        finally:
            dialog.Destroy()

        self.ws.save_config()
        try:
            self.manager.regenerate_block(board)
            self.manager.regenerate_port_markers()
        except Exception as exc:
            message(
                self,
                f"Ports were saved, but the block footprint could not be regenerated.\n\n{exc}",
                "Ports",
                wx.ICON_WARNING,
            )
        self._refresh_boards()
        self.status.set_status(f"'{name}' now has {len(board.ports)} port(s)", "ok")

    def _on_delete(self) -> None:
        name = self._selected_board()
        if not name:
            return

        counts = self.ws.index.board_counts().get(name, {})
        placed = counts.get("placed", 0)
        if not confirm(
            self,
            f"Delete board '{name}'?\n\n"
            f"It has {placed} placed component(s).\n\n"
            "Its directory is moved to boards/.trash/ rather than erased, so you can "
            "recover it with a file manager.",
            "Delete board",
        ):
            return

        try:
            trashed = self.manager.delete_board(name)
        except BoardBusy as exc:
            message(self, str(exc), "Board is open", wx.ICON_WARNING)
            return
        except Exception as exc:
            message(self, f"Could not delete '{name}'.\n\n{exc}", "Delete", wx.ICON_ERROR)
            return

        self._refresh(force=True)
        where = f"\n\nMoved to:\n{trashed}" if trashed else ""
        self.status.set_status(f"Deleted board '{name}'", "ok")
        message(self, f"Board '{name}' was deleted.{where}", "Deleted")

    def _on_refresh(self) -> None:
        self._refresh(force=True)

    def _on_drc(self) -> None:
        from ..core.fab import format_drc, summarize_drc

        def work(progress, cancelled):
            return self.ws.run_drc(progress=progress, cancel=cancelled)

        results = run_with_progress(self, "Running DRC", work)
        if not results:
            message(self, "No boards to check.", "DRC")
            return

        total = sum(r.count for r in results.values())
        show_modal(
            ReportDialog(
                self,
                "Design rule check",
                format_drc(results),
                headline=summarize_drc(results),
                kind="error" if total else "success",
            )
        )

    def _on_doctor(self) -> None:
        report = self.ws.doctor(backend=self.manager.backend)
        show_modal(DoctorDialog(self, report, lambda: self.ws.doctor(backend=self.manager.backend)))
        self._refresh_boards()
        self._update_banner()

    def _on_board_menu(self, event) -> None:
        row = event.GetRow()
        if 0 <= row < len(self._board_rows):
            self.grid.SelectRow(row)
            self.grid.SetGridCursor(row, max(0, event.GetCol()))
        name = self._selected_board()
        if not name:
            return

        # Bound to the menu, so the handlers are released with it. Binding them
        # on the dialog left one set behind per right-click, for the life of the
        # window, each capturing the board name it was built for.
        locked = name in self.ws.open_boards()
        menu = wx.Menu()

        def on(label: str, handler, enabled: bool = True) -> None:
            item = menu.Append(wx.ID_ANY, label)
            item.Enable(enabled)
            menu.Bind(wx.EVT_MENU, lambda _e: handler(), id=item.GetId())

        on("Open in KiCad", self._on_open)
        on("Update...", self._on_update, not locked)
        on("Ports...", self._on_ports)
        menu.AppendSeparator()
        on("Rename...", lambda: self._on_rename(name))
        on("Edit description...", lambda: self._on_description(name))
        on("Build fabrication output", lambda: self._on_fab(name))
        menu.AppendSeparator()
        on("Copy path", lambda: self._copy_path(name))
        on("Delete", self._on_delete, not locked)

        self.grid.PopupMenu(menu)
        menu.Destroy()

    def _on_rename(self, name: str) -> None:
        with wx.TextEntryDialog(self, "New name for this board:", "Rename", name) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            new = dialog.GetValue().strip()
        if not new or new == name:
            return
        try:
            self.manager.rename_board(name, new)
        except Exception as exc:
            message(self, str(exc), "Rename", wx.ICON_ERROR)
            return
        self._refresh_boards()
        self.xref.refresh_view()

    def _on_description(self, name: str) -> None:
        board = self.ws.config.boards[name]
        with wx.TextEntryDialog(
            self,
            f"Description for '{name}':",
            "Description",
            board.description,
            style=wx.TE_MULTILINE | wx.OK | wx.CANCEL,
        ) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            board.description = dialog.GetValue().strip()
        self.ws.save_config()
        self._refresh_boards()

    def _on_fab(self, name: str) -> None:
        from ..core.fab import run_fab

        board = self.ws.config.boards[name]

        def work(progress, cancelled):
            progress(20, f"Building fabrication output for {name}...")

            def pump(elapsed: float) -> bool:
                progress(20, f"Building fabrication output for {name}... ({elapsed:.0f}s)")
                return bool(cancelled())

            return run_fab(self.ws.install, self.ws.root, board, pump=pump)

        result = run_with_progress(self, f"Fabrication: {name}", work)
        if result.ok:
            message(
                self,
                f"Fabrication output for '{name}' is ready.\n\n"
                f"See {self.ws.root / '.multiboard' / 'fab' / name}",
                "Fabrication complete",
            )
        else:
            message(self, f"Fabrication failed.\n\n{result.failure_text()}", "Fabrication", wx.ICON_ERROR)

    def _copy_path(self, name: str) -> None:
        pcb = self.ws.board_pcb(name)
        if pcb and wx.TheClipboard.Open():
            try:
                wx.TheClipboard.SetData(wx.TextDataObject(str(pcb)))
            finally:
                wx.TheClipboard.Close()
            self.status.set_status(f"Copied path to '{name}'", "ok")

    # -- theming -----------------------------------------------------------

    def retheme(self) -> None:
        """Re-apply everything after a system light/dark change."""
        super().retheme()
        self.theme = get_theme()
        apply_grid(self.grid, self.theme)
        self.xref.retheme()
        self.status.retheme()
        self.banner.retheme()
        self.subtitle.SetForegroundColour(self.theme.header_muted)
        self._refresh_boards()
        self.Refresh()
