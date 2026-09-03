"""
Supporting dialogs: Doctor, ports, new board, reports, and onboarding.

The onboarding wizard is the answer to "someone should be able to pick this up
without help". It confirms the source of truth, *actually tests* that schematic
linking works on this filesystem rather than assuming, offers one board per
top-level sheet, and proposes the assignment rules to match -- so a
hierarchically drawn project is fully set up before the user has typed anything.
"""

from pathlib import Path
from typing import Callable, Optional

import wx

from ..core.config import SIDES, AssignRule, BoardConfig, PortDef
from ..core.doctor import ERROR, INFO, OK, WARN, Check, Report
from ..core.project import can_link, detect_root_files, is_valid_board_name
from .theme import apply_input
from .widgets import (
    SPACING_LG,
    SPACING_MD,
    SPACING_SM,
    BaseDialog,
    ReadOnlyText,
    confirm,
    message,
)

# =============================================================================
# Doctor
# =============================================================================


class DoctorDialog(BaseDialog):
    """Diagnostics with a repair button per row."""

    def __init__(self, parent, report: Report, rerun: Callable[[], Report]):
        super().__init__(parent, "Doctor", size=(820, 600), min_size=(680, 480))
        self.report = report
        self._rerun = rerun
        self._build()
        self._refresh()

    def _build(self) -> None:
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.headline = wx.StaticText(self, label="")
        self.headline.SetFont(self.theme.title_font())
        sizer.Add(self.headline, 0, wx.ALL, SPACING_MD)

        split = wx.BoxSizer(wx.HORIZONTAL)

        self.list = wx.ListBox(self, style=wx.LB_SINGLE)
        self.list.SetFont(self.theme.body_font())
        apply_input(self.list)
        self.list.Bind(wx.EVT_LISTBOX, lambda e: self._show_detail())
        split.Add(self.list, 2, wx.EXPAND | wx.RIGHT, SPACING_MD)

        right = wx.BoxSizer(wx.VERTICAL)
        self.detail = ReadOnlyText(self, mono=False)
        right.Add(self.detail, 1, wx.EXPAND | wx.BOTTOM, SPACING_SM)
        self.fix_button = wx.Button(self, label="Fix")
        self.fix_button.Bind(wx.EVT_BUTTON, lambda e: self._fix_selected())
        self.fix_button.Disable()
        right.Add(self.fix_button, 0)
        split.Add(right, 3, wx.EXPAND)

        sizer.Add(split, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, SPACING_MD)

        tools = wx.BoxSizer(wx.HORIZONTAL)
        for label, handler in (
            ("Re-run", self._on_rerun),
            ("Fix all safe issues", self._fix_all),
            ("Copy report", self._copy),
        ):
            button = wx.Button(self, label=label)
            button.Bind(wx.EVT_BUTTON, lambda e, h=handler: h())
            tools.Add(button, 0, wx.RIGHT, SPACING_SM)
        tools.AddStretchSpacer()
        tools.Add(wx.Button(self, wx.ID_OK, "Close"), 0)
        sizer.Add(tools, 0, wx.ALL | wx.EXPAND, SPACING_MD)

        self.SetSizer(sizer)

    def _refresh(self) -> None:
        marks = {OK: "OK   ", INFO: "info ", WARN: "warn ", ERROR: "ERROR"}
        self.list.Clear()
        for check in self.report.checks:
            self.list.Append(f"{marks[check.level]}  {check.title}")

        worst = self.report.worst
        self.headline.SetLabel(self.report.summary())
        self.headline.SetForegroundColour(
            {
                OK: self.theme.success,
                INFO: self.theme.text,
                WARN: self.theme.warning,
                ERROR: self.theme.error,
            }[worst]
        )

        first_problem = next((i for i, c in enumerate(self.report.checks) if c.needs_attention), 0)
        if self.report.checks:
            self.list.SetSelection(first_problem)
        self._show_detail()

    def _selected(self) -> Optional[Check]:
        i = self.list.GetSelection()
        return self.report.checks[i] if 0 <= i < len(self.report.checks) else None

    def _show_detail(self) -> None:
        check = self._selected()
        if check is None:
            self.detail.SetValue("")
            self.fix_button.Disable()
            return
        self.detail.SetValue(f"{check.title}\n\n{check.detail}".rstrip())
        self.fix_button.Enable(check.fix is not None)
        self.fix_button.SetLabel(check.fix_label or "Fix")

    def _fix_selected(self) -> None:
        check = self._selected()
        if check is None or check.fix is None:
            return
        try:
            outcome = check.fix()
        except Exception as exc:
            message(self, f"The repair failed:\n\n{exc}", "Doctor", wx.ICON_ERROR)
            return
        message(self, outcome, check.fix_label or "Repair complete")
        self._on_rerun()

    def _fix_all(self) -> None:
        fixable = self.report.fixable()
        if not fixable:
            message(self, "Nothing here has an automatic repair.", "Doctor")
            return
        if not confirm(
            self,
            f"Run {len(fixable)} repair(s)?\n\n" + "\n".join(f"  {c.fix_label or c.title}" for c in fixable),
            "Fix all",
        ):
            return
        results = []
        for check in fixable:
            try:
                results.append(f"{check.title}: {check.fix()}")
            except Exception as exc:
                results.append(f"{check.title}: FAILED - {exc}")
        message(self, "\n".join(results), "Repairs complete")
        self._on_rerun()

    def _on_rerun(self) -> None:
        self.report = self._rerun()
        self._refresh()

    def _copy(self) -> None:
        if wx.TheClipboard.Open():
            try:
                wx.TheClipboard.SetData(wx.TextDataObject(self.report.to_text()))
            finally:
                wx.TheClipboard.Close()
        message(self, "Report copied to the clipboard.", "Doctor")


# =============================================================================
# New board
# =============================================================================


class NewBoardDialog(BaseDialog):
    """Name and describe a new sub-board."""

    def __init__(self, parent, existing: list[str]):
        super().__init__(parent, "New board", size=(520, 340), min_size=(460, 300))
        self.existing = existing
        self.board_name = ""
        self.description = ""
        self._build()

    def _build(self) -> None:
        sizer = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(
            self,
            label=(
                "A sub-board is its own PCB sharing this project's schematic.\n"
                "It gets a directory under boards/ and a link to the root schematic."
            ),
        )
        intro.SetForegroundColour(self.theme.text_muted)
        sizer.Add(intro, 0, wx.ALL, SPACING_MD)

        sizer.Add(self._label("Name"), 0, wx.LEFT | wx.RIGHT, SPACING_MD)
        self.name = wx.TextCtrl(self)
        apply_input(self.name)
        self.name.Bind(wx.EVT_TEXT, lambda e: self._validate())
        sizer.Add(self.name, 0, wx.ALL | wx.EXPAND, SPACING_MD)

        sizer.Add(self._label("Description (optional)"), 0, wx.LEFT | wx.RIGHT, SPACING_MD)
        self.desc = wx.TextCtrl(self, style=wx.TE_MULTILINE, size=(-1, 70))
        apply_input(self.desc)
        sizer.Add(self.desc, 1, wx.ALL | wx.EXPAND, SPACING_MD)

        self.hint = wx.StaticText(self, label="")
        self.hint.SetFont(self.theme.small_font())
        sizer.Add(self.hint, 0, wx.LEFT | wx.RIGHT | wx.EXPAND, SPACING_MD)

        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, SPACING_MD)
        self.SetSizer(sizer)

        self.ok = self.FindWindowById(wx.ID_OK)
        self.ok.Bind(wx.EVT_BUTTON, self._on_ok)
        self.name.SetFocus()
        self._validate()

    def _label(self, text: str) -> wx.StaticText:
        label = wx.StaticText(self, label=text)
        label.SetForegroundColour(self.theme.text)
        return label

    def _validate(self) -> bool:
        from ..core.project import sanitize_board_name

        name = self.name.GetValue().strip()
        error = is_valid_board_name(name)
        if not error and name in self.existing:
            error = f"A board named '{name}' already exists."

        if error:
            self.hint.SetLabel(error if name else "Enter a name to continue.")
            self.hint.SetForegroundColour(self.theme.warning if name else self.theme.text_muted)
            self.ok.Disable()
            return False

        self.hint.SetLabel(f"Will create boards/{sanitize_board_name(name)}/")
        self.hint.SetForegroundColour(self.theme.text_muted)
        self.ok.Enable()
        return True

    def _on_ok(self, _event) -> None:
        if not self._validate():
            return
        self.board_name = self.name.GetValue().strip()
        self.description = self.desc.GetValue().strip()
        self.EndModal(wx.ID_OK)


# =============================================================================
# Ports
# =============================================================================


class PortEditDialog(BaseDialog):
    """One inter-board connection point."""

    def __init__(self, parent, port: Optional[PortDef], taken: list[str]):
        super().__init__(parent, "Edit port" if port else "New port", size=(460, 340), min_size=(420, 300))
        self.taken = [t for t in taken if not port or t != port.name]
        self.port = PortDef(
            name=port.name if port else "",
            net=port.net if port else "",
            side=port.side if port else "right",
            position=port.position if port else 0.5,
        )
        self._build()

    def _build(self) -> None:
        sizer = wx.BoxSizer(wx.VERTICAL)
        grid = wx.FlexGridSizer(3, 2, SPACING_SM, SPACING_MD)
        grid.AddGrowableCol(1, 1)

        grid.Add(wx.StaticText(self, label="Name"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.name = wx.TextCtrl(self, value=self.port.name)
        apply_input(self.name)
        grid.Add(self.name, 1, wx.EXPAND)

        grid.Add(wx.StaticText(self, label="Net"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.net = wx.TextCtrl(self, value=self.port.net)
        self.net.SetHint("defaults to the port name")
        apply_input(self.net)
        grid.Add(self.net, 1, wx.EXPAND)

        grid.Add(wx.StaticText(self, label="Edge"), 0, wx.ALIGN_CENTER_VERTICAL)
        self.side = wx.Choice(self, choices=[s.title() for s in SIDES])
        self.side.SetSelection(SIDES.index(self.port.side))
        grid.Add(self.side, 1, wx.EXPAND)

        sizer.Add(grid, 0, wx.ALL | wx.EXPAND, SPACING_MD)

        sizer.Add(wx.StaticText(self, label="Position along that edge"), 0, wx.LEFT | wx.RIGHT, SPACING_MD)
        self.slider = wx.Slider(
            self,
            value=int(self.port.position * 100),
            minValue=0,
            maxValue=100,
            style=wx.SL_HORIZONTAL | wx.SL_LABELS,
        )
        sizer.Add(self.slider, 0, wx.ALL | wx.EXPAND, SPACING_MD)

        note = wx.StaticText(
            self,
            label=(
                "Ports document where a net leaves this board. They become pads on the "
                "generated block footprint, and their nets are excluded from "
                "'unconnected' DRC violations."
            ),
        )
        note.SetFont(self.theme.small_font())
        note.SetForegroundColour(self.theme.text_muted)
        note.Wrap(400)
        sizer.Add(note, 1, wx.ALL | wx.EXPAND, SPACING_MD)

        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, SPACING_MD)
        self.SetSizer(sizer)
        self.FindWindowById(wx.ID_OK).Bind(wx.EVT_BUTTON, self._on_ok)
        self.name.SetFocus()

    def _on_ok(self, _event) -> None:
        name = self.name.GetValue().strip()
        if not name:
            message(self, "A port needs a name.", "Port", wx.ICON_WARNING)
            return
        if name in self.taken:
            message(self, f"This board already has a port named '{name}'.", "Port", wx.ICON_WARNING)
            return
        self.port = PortDef(
            name=name,
            net=self.net.GetValue().strip(),
            side=SIDES[self.side.GetSelection()],
            position=self.slider.GetValue() / 100.0,
        )
        self.EndModal(wx.ID_OK)


class PortsDialog(BaseDialog):
    """The port list for one board."""

    def __init__(self, parent, board: BoardConfig):
        super().__init__(parent, f"Ports on '{board.name}'", size=(620, 460), min_size=(540, 400))
        self.ports: dict[str, PortDef] = {
            n: PortDef(p.name, p.net, p.side, p.position) for n, p in board.ports.items()
        }
        self._rows: list[str] = []
        self._build()
        self._refresh()

    def _build(self) -> None:
        sizer = wx.BoxSizer(wx.VERTICAL)

        self.list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        for i, (label, width) in enumerate([("Port", 150), ("Net", 170), ("Edge", 90), ("Position", 90)]):
            self.list.InsertColumn(i, label, width=width)
        self.list.SetFont(self.theme.body_font())
        apply_input(self.list)
        self.list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, lambda e: self._on_edit())
        sizer.Add(self.list, 1, wx.ALL | wx.EXPAND, SPACING_MD)

        tools = wx.BoxSizer(wx.HORIZONTAL)
        for label, handler in (
            ("Add...", self._on_add),
            ("Edit...", self._on_edit),
            ("Remove", self._on_remove),
        ):
            button = wx.Button(self, label=label)
            button.Bind(wx.EVT_BUTTON, lambda e, h=handler: h())
            tools.Add(button, 0, wx.RIGHT, SPACING_SM)
        sizer.Add(tools, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, SPACING_MD)

        buttons = self.CreateStdDialogButtonSizer(wx.OK | wx.CANCEL)
        sizer.Add(buttons, 0, wx.ALL | wx.ALIGN_RIGHT, SPACING_MD)
        self.SetSizer(sizer)

    def _refresh(self) -> None:
        """
        Redraw the list, remembering which key each row stands for.

        The row order and the key are captured together on purpose. Rows used to
        be located by re-sorting the dict at selection time while being *drawn*
        from ``port.name``; where a config's key and name differ -- which a
        hand-edited file or an older schema can produce -- Edit and Remove acted
        on a different port than the one highlighted.
        """
        self.list.DeleteAllItems()
        self._rows = sorted(self.ports, key=lambda k: self.ports[k].name.lower())
        for i, key in enumerate(self._rows):
            port = self.ports[key]
            self.list.InsertItem(i, port.name)
            self.list.SetItem(i, 1, port.effective_net())
            self.list.SetItem(i, 2, port.side.title())
            self.list.SetItem(i, 3, f"{port.position * 100:.0f}%")

    def _selected(self) -> Optional[str]:
        i = self.list.GetFirstSelected()
        return self._rows[i] if 0 <= i < len(self._rows) else None

    def _on_add(self) -> None:
        dialog = PortEditDialog(self, None, list(self.ports))
        if dialog.ShowModal() == wx.ID_OK:
            self.ports[dialog.port.name] = dialog.port
            self._refresh()
        dialog.Destroy()

    def _on_edit(self) -> None:
        name = self._selected()
        if name is None:
            return
        dialog = PortEditDialog(self, self.ports[name], list(self.ports))
        if dialog.ShowModal() == wx.ID_OK:
            del self.ports[name]
            self.ports[dialog.port.name] = dialog.port
            self._refresh()
        dialog.Destroy()

    def _on_remove(self) -> None:
        name = self._selected()
        if name and confirm(self, f"Remove port '{name}'?", "Remove port"):
            del self.ports[name]
            self._refresh()


# =============================================================================
# Reports
# =============================================================================


class ReportDialog(BaseDialog):
    """A monospace report: DRC, health, anything textual."""

    def __init__(self, parent, title: str, body: str, *, headline: str = "", kind: str = "info"):
        super().__init__(parent, title, size=(860, 600), min_size=(640, 440))
        sizer = wx.BoxSizer(wx.VERTICAL)

        if headline:
            label = wx.StaticText(self, label=headline)
            label.SetFont(self.theme.title_font())
            label.SetForegroundColour(
                {
                    "info": self.theme.text,
                    "success": self.theme.success,
                    "warning": self.theme.warning,
                    "error": self.theme.error,
                }.get(kind, self.theme.text)
            )
            sizer.Add(label, 0, wx.ALL, SPACING_MD)

        self.text = ReadOnlyText(self, body, mono=True)
        sizer.Add(self.text, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, SPACING_MD)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        copy = wx.Button(self, label="Copy")
        copy.Bind(wx.EVT_BUTTON, lambda e: self._copy(body))
        buttons.Add(copy, 0, wx.RIGHT, SPACING_SM)
        buttons.AddStretchSpacer()
        buttons.Add(wx.Button(self, wx.ID_OK, "Close"), 0)
        sizer.Add(buttons, 0, wx.ALL | wx.EXPAND, SPACING_MD)
        self.SetSizer(sizer)

    def _copy(self, body: str) -> None:
        if wx.TheClipboard.Open():
            try:
                wx.TheClipboard.SetData(wx.TextDataObject(body))
            finally:
                wx.TheClipboard.Close()


# =============================================================================
# Onboarding
# =============================================================================


class OnboardingDialog(BaseDialog):
    """
    First-run setup.

    Four steps, each of which removes a way v12 could leave a project subtly
    wrong: it confirms the source of truth instead of guessing from a
    nondeterministic glob; it *tests* schematic linking rather than discovering
    the failure later; it offers boards from the schematic's own structure; and
    it proposes matching rules so assignment is deliberate from the start.
    """

    def __init__(self, parent, root: Path, sheets: list[str], detected: list[dict]):
        super().__init__(parent, "Set up multi-board", size=(680, 560), min_size=(600, 480))
        self.root = root
        self.sheets = sheets
        self.detected = detected or detect_root_files(root)
        self.result: Optional[dict] = None
        self._page = 0
        self._build()
        self._show_page()

    def _build(self) -> None:
        self.sizer = wx.BoxSizer(wx.VERTICAL)

        self.title = wx.StaticText(self, label="")
        self.title.SetFont(self.theme.header_font())
        self.sizer.Add(self.title, 0, wx.ALL, SPACING_LG)

        self.body = wx.Panel(self)
        self.body_sizer = wx.BoxSizer(wx.VERTICAL)
        self.body.SetSizer(self.body_sizer)
        self.sizer.Add(self.body, 1, wx.LEFT | wx.RIGHT | wx.EXPAND, SPACING_LG)

        nav = wx.BoxSizer(wx.HORIZONTAL)
        self.back = wx.Button(self, label="Back")
        self.back.Bind(wx.EVT_BUTTON, lambda e: self._go(-1))
        nav.Add(self.back, 0, wx.RIGHT, SPACING_SM)
        nav.AddStretchSpacer()
        self.next = wx.Button(self, label="Next")
        self.next.Bind(wx.EVT_BUTTON, lambda e: self._go(1))
        nav.Add(self.next, 0, wx.RIGHT, SPACING_SM)
        cancel = wx.Button(self, wx.ID_CANCEL, "Cancel")
        nav.Add(cancel, 0)
        self.sizer.Add(nav, 0, wx.ALL | wx.EXPAND, SPACING_LG)

        self.SetSizer(self.sizer)

    # -- pages -------------------------------------------------------------

    def _show_page(self) -> None:
        self.body_sizer.Clear(delete_windows=True)
        builder = [self._page_source, self._page_linking, self._page_boards, self._page_rules][self._page]
        builder()
        self.back.Enable(self._page > 0)
        self.next.SetLabel("Finish" if self._page == 3 else "Next")
        self.body.Layout()
        self.Layout()

    def _para(self, text: str, *, muted: bool = True) -> None:
        label = wx.StaticText(self.body, label=text)
        if muted:
            label.SetForegroundColour(self.theme.text_muted)
        label.Wrap(560)
        self.body_sizer.Add(label, 0, wx.BOTTOM, SPACING_MD)

    def _page_source(self) -> None:
        self.title.SetLabel("1. Confirm the source of truth")
        self._para(
            "Every board in this project is driven by one schematic. Each board "
            "directory gets a link to it -- not a copy -- so they can never drift apart."
        )
        if not self.detected:
            self._para("No KiCad project was found in this directory.", muted=False)
            self.choice = None
            return

        self.choice = wx.Choice(
            self.body,
            choices=[f"{c['project']}  ->  {c['schematic'] or 'no schematic'}" for c in self.detected],
        )
        self.choice.SetSelection(0)
        self.body_sizer.Add(self.choice, 0, wx.EXPAND | wx.BOTTOM, SPACING_MD)

        if len(self.detected) > 1:
            self._para(
                f"{len(self.detected)} projects were found here. Pick the one whose "
                "schematic drives your boards."
            )

    def _page_linking(self) -> None:
        self.title.SetLabel("2. Check that linking works")
        ok, detail = can_link(self.root / "boards")
        if ok:
            self._para(
                f"Linking works here ({detail}). Board schematics will stay in perfect sync with the root.",
                muted=False,
            )
        else:
            label = wx.StaticText(
                self.body,
                label=(
                    "Schematic linking does not work in this location.\n\n"
                    f"{detail}\n\n"
                    "Common causes: the project is on a network drive, boards/ would be on "
                    "a different filesystem, or Windows needs Developer Mode or "
                    "Administrator. You can continue, but board schematics will not link."
                ),
            )
            label.SetForegroundColour(self.theme.warning)
            label.Wrap(560)
            self.body_sizer.Add(label, 0, wx.BOTTOM, SPACING_MD)

    def _page_boards(self) -> None:
        self.title.SetLabel("3. Create your boards")
        if self.sheets:
            self._para(
                "Your schematic has these top-level sheets. Creating one board per "
                "sheet is the usual multi-board structure, and it lets the next step "
                "assign every component automatically."
            )
            self.board_list = wx.CheckListBox(self.body, choices=self.sheets)
            for i in range(len(self.sheets)):
                self.board_list.Check(i, True)
            self.body_sizer.Add(self.board_list, 1, wx.EXPAND | wx.BOTTOM, SPACING_MD)
        else:
            self._para(
                "No hierarchical sheets were found, so there is nothing to suggest. "
                "You can create boards by hand once setup finishes."
            )
            self.board_list = None

    def _page_rules(self) -> None:
        self.title.SetLabel("4. Assign components automatically")
        selected = self._selected_boards()
        if selected:
            self._para(
                "One rule per sheet means every component already knows which board "
                "it belongs on -- including parts you add later. You can change all "
                "of this afterwards in Assignment rules."
            )
            preview = "\n".join(f"    /{s}/    ->    {s}" for s in selected)
            text = ReadOnlyText(self.body, preview, mono=True)
            self.body_sizer.Add(text, 1, wx.EXPAND | wx.BOTTOM, SPACING_MD)
            self.want_rules = wx.CheckBox(self.body, label="Create these rules")
            self.want_rules.SetValue(True)
            self.body_sizer.Add(self.want_rules, 0)
        else:
            self._para("No boards selected, so there is nothing to assign yet.")
            self.want_rules = None

    def _selected_boards(self) -> list[str]:
        widget = getattr(self, "board_list", None)
        if widget is None:
            return []
        return [self.sheets[i] for i in widget.GetCheckedItems()]

    # -- navigation --------------------------------------------------------

    def _go(self, delta: int) -> None:
        if delta > 0 and self._page == 3:
            self._finish()
            return
        if delta > 0 and self._page == 0 and self.choice is None:
            message(
                self, "There is no KiCad project here to set up.", "Nothing to configure", wx.ICON_WARNING
            )
            return

        if self._page == 0 and self.choice is not None:
            self._chosen = self.detected[self.choice.GetSelection()]
        if self._page == 2:
            self._boards = self._selected_boards()

        self._page = max(0, min(3, self._page + delta))
        self._show_page()

    def _finish(self) -> None:
        chosen = getattr(self, "_chosen", self.detected[0] if self.detected else {})
        boards = getattr(self, "_boards", [])
        rules = []
        if getattr(self, "want_rules", None) is not None and self.want_rules.GetValue():
            rules = [AssignRule("sheet", f"/{b}/", b) for b in boards]

        self.result = {
            "root_schematic": chosen.get("schematic", ""),
            "root_pcb": chosen.get("pcb", ""),
            "boards": boards,
            "rules": rules,
        }
        self.EndModal(wx.ID_OK)
