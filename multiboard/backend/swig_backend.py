"""
The pcbnew (SWIG) backend for KiCad 10.

Two v12 practices are deliberately abandoned here.

**Hand-written s-expressions.** v12 built ``.kicad_pcb`` and ``.kicad_mod`` files
by string concatenation. Its block-footprint generator emitted two stray closing
parens, so *every* block footprint it ever wrote was unparseable and the feature
silently never worked; its empty-board template hardcoded KiCad 8's format
version and omitted the ``F.Paste`` layer that its own port pads referenced.
Building through the API means the format is correct by construction, and stays
correct on the next KiCad release.

**Ignoring failures.** Footprints that failed to load were counted as updates.
Nets were assigned but never cleared, so a connection deleted in the schematic
survived on the pad forever. Both are fixed in :meth:`apply_update`.
"""

from pathlib import Path
from typing import Callable, Optional

from .. import compat
from ..constants import PACK_GRID_SPACING, PACK_MAX_PER_ROW, PACK_ORIGIN
from ..core import netlist as netlist_mod
from ..core import plan as plan_mod
from ..core.config import PortDef
from .base import ApplyResult, Backend, BlockSpec


class SwigBackend(Backend):
    """Everything that requires the pcbnew module."""

    name = "swig"

    def __init__(self):
        self._fp_cache: dict[str, object] = {}
        self._fp_failed: set = set()

    # =====================================================================
    # Version and environment
    # =====================================================================

    def version(self) -> tuple[int, ...]:
        return compat.kicad_version()

    def capabilities(self) -> dict:
        return compat.capabilities()

    def active_board_path(self) -> Optional[Path]:
        try:
            board = compat.pcbnew().GetBoard()
            name = board.GetFileName() if board else ""
            return Path(name) if name else None
        except Exception:
            return None

    def refresh_ui(self) -> None:
        p = compat.pcbnew()
        for fn in ("Refresh", "UpdateUserInterface"):
            try:
                getattr(p, fn)()
                return
            except AttributeError:
                continue

    def focus_reference(self, ref: str) -> bool:
        """
        Select and zoom to a component on the active board.

        This is the payoff of the whole index: click a row, and KiCad's canvas
        goes to the part. It only works for the board currently open -- KiCad 10
        offers no way to drive a different document, so the cross-board case goes
        through ``core.focus``'s handoff instead.
        """
        p = compat.pcbnew()
        try:
            board = p.GetBoard()
            if board is None:
                return False
            fp = board.FindFootprintByReference(ref)
            if fp is None:
                return False
            try:
                board.ClearAllNetCodes  # noqa: B018 - presence probe only
            except AttributeError:
                pass
            if compat.has("FocusOnItem"):
                p.FocusOnItem(fp)
            self.refresh_ui()
            return True
        except Exception:
            return False

    # =====================================================================
    # Board creation
    # =====================================================================

    def new_board(self, path: Path) -> None:
        """
        Create an empty board using KiCad's own writer.

        The point is to never state a format version ourselves. v12's template
        said ``(version 20240108) (generator_version "9.0")`` and declared nine
        layers; KiCad 10 writes ``20260206``, and the missing ``F.Paste`` /
        ``B.Paste`` layers broke the port pads the same file was meant to carry.
        """
        p = compat.pcbnew()
        path.parent.mkdir(parents=True, exist_ok=True)

        created = False
        if compat.has("NewBoard"):
            try:
                p.NewBoard(str(path))
                created = path.exists()
            except Exception:
                created = False

        if not created:
            board = p.CreateEmptyBoard()
            p.SaveBoard(str(path), board)

        self._ensure_standard_layers(path)

    def _ensure_standard_layers(self, path: Path) -> None:
        """Enable the full standard layer set, including both paste layers."""
        p = compat.pcbnew()
        try:
            board = p.LoadBoard(str(path))
            board.SetCopperLayerCount(2)
            for attempt in ("AllLayersMask", "AllTechMask"):
                mask = getattr(p.LSET, attempt, None)
                if mask is not None:
                    try:
                        board.SetEnabledLayers(mask())
                        break
                    except Exception:
                        continue
            p.SaveBoard(str(path), board)
        except Exception:
            # A board that exists but lacks a layer is recoverable in KiCad's
            # Board Setup; a board that does not exist is not. Never re-raise.
            pass

    # =====================================================================
    # Footprint libraries
    # =====================================================================

    def write_block_footprint(self, lib_dir: Path, spec: BlockSpec) -> None:
        """Generate and save a board block footprint."""
        p = compat.pcbnew()
        scratch = p.CreateEmptyBoard()
        fp = self._build_block(scratch, spec)
        self._save_footprint(lib_dir, fp)

    def write_port_footprint(self, lib_dir: Path, port_name: str) -> None:
        """Generate and save a standalone port marker footprint."""
        p = compat.pcbnew()
        scratch = p.CreateEmptyBoard()
        fp = p.FOOTPRINT(scratch)
        fp.SetFPID(p.LIB_ID("MultiBoard_Ports", f"Port_{port_name}"))
        fp.SetLayer(p.F_Cu)
        fp.SetReference("REF**")
        fp.SetValue(port_name)
        self._describe(fp, f"Inter-board port: {port_name}")

        pad = p.PAD(fp)
        pad.SetNumber("1")
        pad.SetAttribute(p.PAD_ATTRIB_SMD)
        pad.SetShape(p.PAD_SHAPE_ROUNDRECT)
        compat.pad_set_size(pad, 2.5, 2.5)
        try:
            pad.SetRoundRectRadiusRatio(0.2)
        except Exception:
            pass
        self._smd_layers(pad)
        pad.SetPosition(compat.vec_mm(0, 0))
        fp.Add(pad)

        for radius in (2.0, 1.5):
            circle = p.PCB_SHAPE(fp)
            circle.SetShape(p.SHAPE_T_CIRCLE)
            circle.SetStart(compat.vec_mm(0, 0))
            circle.SetEnd(compat.vec_mm(radius, 0))
            circle.SetLayer(p.F_SilkS)
            compat.set_stroke(circle, 0.15, "solid")
            fp.Add(circle)

        self._save_footprint(lib_dir, fp)

    def _build_block(self, scratch, spec: BlockSpec):
        p = compat.pcbnew()
        fp = p.FOOTPRINT(scratch)
        fp.SetFPID(p.LIB_ID("MultiBoard_Blocks", f"Block_{spec.name}"))
        fp.SetLayer(p.F_Cu)

        # A block represents a board, not a part: keep it out of the BOM and the
        # pick-and-place output.
        try:
            fp.SetAttributes(p.FP_BOARD_ONLY | p.FP_EXCLUDE_FROM_POS_FILES | p.FP_EXCLUDE_FROM_BOM)
        except AttributeError:
            for setter in ("SetBoardOnly", "SetExcludedFromPosFiles", "SetExcludedFromBOM"):
                try:
                    getattr(fp, setter)(True)
                except AttributeError:
                    pass

        self._describe(fp, spec.description or f"Board block: {spec.name}")

        hw, hh = spec.width_mm / 2, spec.height_mm / 2
        fp.SetReference("REF**")
        fp.SetValue(spec.name)
        try:
            fp.Reference().SetPosition(compat.vec_mm(0, -hh - 4))
            fp.Reference().SetLayer(p.F_SilkS)
            fp.Value().SetPosition(compat.vec_mm(0, hh + 4))
            fp.Value().SetLayer(p.F_Fab)
        except AttributeError:
            pass

        radius = spec.corner_radius()
        self._rounded_outline(fp, hw, hh, radius, p.F_SilkS, 0.32, "solid")
        self._rounded_outline(fp, hw - 1.8, hh - 1.8, max(0.5, radius - 1.8), p.F_SilkS, 0.14, "dash")
        self._rounded_outline(fp, hw, hh, radius, p.F_Fab, 0.12, "solid")

        # Courtyard, 1 mm proud of the outline.
        court = p.PCB_SHAPE(fp)
        court.SetShape(p.SHAPE_T_RECTANGLE)
        court.SetStart(compat.vec_mm(-hw - 1, -hh - 1))
        court.SetEnd(compat.vec_mm(hw + 1, hh + 1))
        court.SetLayer(p.F_CrtYd)
        compat.set_stroke(court, 0.05, "solid")
        fp.Add(court)

        self._pin1_marker(fp, -hw + 1.2, -hh + 1.2, 2.2)
        self._text(fp, spec.name, 0, 0, size=2.5, thickness=0.4, layer=p.F_SilkS, bold=True)

        for port in sorted(spec.ports, key=lambda pt: (pt.side, pt.position, pt.name)):
            self._port_pad(fp, spec, port)

        return fp

    def _rounded_outline(self, fp, hw, hh, radius, layer, width, style) -> None:
        """Four edges and four corner arcs."""
        p = compat.pcbnew()
        r = max(0.3, min(radius, hw * 0.9, hh * 0.9))
        if hw <= r or hh <= r:
            return

        def segment(x1, y1, x2, y2):
            s = p.PCB_SHAPE(fp)
            s.SetShape(p.SHAPE_T_SEGMENT)
            s.SetStart(compat.vec_mm(x1, y1))
            s.SetEnd(compat.vec_mm(x2, y2))
            s.SetLayer(layer)
            compat.set_stroke(s, width, style)
            fp.Add(s)

        def arc(cx, cy, start_angle):
            import math

            s = p.PCB_SHAPE(fp)
            s.SetShape(p.SHAPE_T_ARC)
            pts = []
            for frac in (0.0, 0.5, 1.0):
                a = math.radians(start_angle + 90.0 * frac)
                pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
            s.SetStart(compat.vec_mm(*pts[0]))
            try:
                s.SetMid(compat.vec_mm(*pts[1]))
            except AttributeError:
                pass
            s.SetEnd(compat.vec_mm(*pts[2]))
            s.SetLayer(layer)
            compat.set_stroke(s, width, style)
            fp.Add(s)

        segment(-hw + r, -hh, hw - r, -hh)
        segment(hw, -hh + r, hw, hh - r)
        segment(hw - r, hh, -hw + r, hh)
        segment(-hw, hh - r, -hw, -hh + r)

        if style == "solid":
            arc(-hw + r, -hh + r, 180)
            arc(hw - r, -hh + r, 270)
            arc(hw - r, hh - r, 0)
            arc(-hw + r, hh - r, 90)

    def _pin1_marker(self, fp, x, y, size) -> None:
        p = compat.pcbnew()
        shape = p.PCB_SHAPE(fp)
        shape.SetShape(p.SHAPE_T_POLY)
        pts = [compat.vec_mm(x, y), compat.vec_mm(x + size, y), compat.vec_mm(x, y + size)]
        try:
            shape.SetPolyPoints(pts)
        except (AttributeError, TypeError):
            # Fall back to an outline triangle if the polygon setter is absent.
            for a, b in ((0, 1), (1, 2), (2, 0)):
                seg = p.PCB_SHAPE(fp)
                seg.SetShape(p.SHAPE_T_SEGMENT)
                seg.SetStart(pts[a])
                seg.SetEnd(pts[b])
                seg.SetLayer(p.F_SilkS)
                compat.set_stroke(seg, 0.2, "solid")
                fp.Add(seg)
            return
        shape.SetLayer(p.F_SilkS)
        compat.set_stroke(shape, 0.05, "solid")
        try:
            shape.SetFilled(True)
        except AttributeError:
            pass
        fp.Add(shape)

    def _text(
        self, fp, value, x, y, *, size=1.0, thickness=0.15, layer=None, bold=False, rotation=0.0
    ) -> None:
        p = compat.pcbnew()
        text = p.PCB_TEXT(fp)
        text.SetText(value)
        text.SetPosition(compat.vec_mm(x, y))
        text.SetLayer(layer if layer is not None else p.F_SilkS)
        try:
            text.SetTextSize(compat.vec_mm(size, size))
            text.SetTextThickness(compat.mm(thickness))
            if bold:
                text.SetBold(True)
            if rotation:
                text.SetTextAngle(compat.angle(rotation))
        except AttributeError:
            pass
        fp.Add(text)

    def _port_pad(self, fp, spec: BlockSpec, port: PortDef) -> None:
        p = compat.pcbnew()
        x, y = spec.port_position(port)
        rotation = spec.port_rotation(port)

        pad = p.PAD(fp)
        pad.SetNumber(port.name or "?")
        pad.SetAttribute(p.PAD_ATTRIB_SMD)
        pad.SetShape(p.PAD_SHAPE_ROUNDRECT)
        compat.pad_set_size(pad, 3.6, 1.7)
        try:
            pad.SetRoundRectRadiusRatio(0.28)
        except Exception:
            pass
        self._smd_layers(pad)
        pad.SetPosition(compat.vec_mm(x, y))
        try:
            pad.SetOrientation(compat.angle(rotation))
        except Exception:
            pass
        for setter, value in (("SetPinFunction", port.name), ("SetPinType", "passive")):
            try:
                getattr(pad, setter)(value)
            except AttributeError:
                pass
        fp.Add(pad)

        if port.side in ("left", "right"):
            lx, ly, rot = x + (4 if port.side == "left" else -4), y, 0.0
        else:
            lx, ly, rot = x, y + (4 if port.side == "top" else -4), 90.0

        self._text(fp, port.name, lx, ly, size=1.0, thickness=0.15, rotation=rot)

        net = port.effective_net()
        if net and net != port.name:
            self._text(fp, net, lx, ly + 1.4, size=0.9, thickness=0.12, layer=p.F_Fab, rotation=rot)

    def _smd_layers(self, pad) -> None:
        p = compat.pcbnew()
        for getter in ("SMDMask", "SMDMaskLayers"):
            fn = getattr(p.PAD, getter, None)
            if fn is not None:
                try:
                    pad.SetLayerSet(fn())
                    return
                except Exception:
                    continue
        try:
            mask = p.LSET()
            for layer in (p.F_Cu, p.F_Paste, p.F_Mask):
                mask.addLayer(layer)
            pad.SetLayerSet(mask)
        except Exception:
            pass

    def _describe(self, fp, text: str) -> None:
        for setter in ("SetLibDescription", "SetDescription"):
            try:
                getattr(fp, setter)(text)
                return
            except AttributeError:
                continue

    def _save_footprint(self, lib_dir: Path, fp) -> None:
        """
        Write a footprint to a ``.pretty`` directory.

        Ownership note: the footprint is parented to a scratch board but is
        deliberately **never** added to it. The library writer takes the object
        by pointer without transferring ownership, and adding it as well would
        set up a double free when both Python objects are collected.
        """
        p = compat.pcbnew()
        lib_dir.mkdir(parents=True, exist_ok=True)

        if compat.has("FootprintSave"):
            try:
                p.FootprintSave(str(lib_dir), fp)
                return
            except Exception:
                pass

        io = p.PCB_IO_MGR.PluginFind(p.PCB_IO_MGR.KICAD_SEXP)
        io.FootprintSave(str(lib_dir), fp)

    # =====================================================================
    # The update pipeline
    # =====================================================================

    def apply_update(
        self,
        pcb_path: Path,
        plan: "plan_mod.UpdatePlan",
        netlist_path: Path,
        *,
        lib_paths: dict[str, Path],
        progress: Optional[Callable[[int, str], None]] = None,
        cancel: Optional[Callable[[], bool]] = None,
    ) -> ApplyResult:
        """
        Execute a plan against a board.

        Only ``enabled`` items are applied, so the checkboxes in the preview
        dialog are load-bearing rather than decorative. Nothing is saved if the
        user cancels.
        """
        p = compat.pcbnew()
        result = ApplyResult()

        def report(pct: int, msg: str) -> bool:
            if progress:
                progress(pct, msg)
            return bool(cancel and cancel())

        if report(5, "Loading board..."):
            result.cancelled = True
            return result

        board = p.LoadBoard(str(pcb_path))
        if board is None:
            result.failed.append(f"Could not load {pcb_path.name}")
            return result

        existing = {}
        for fp in board.GetFootprints():
            ref = fp.GetReference()
            if ref:
                existing[ref] = fp

        managed: list[object] = []
        new_footprints: list[object] = []

        # -- removals ------------------------------------------------------
        for item in plan.enabled_items(plan_mod.REMOVE):
            if report(10, f"Removing {item.ref}..."):
                result.cancelled = True
                return result
            fp = existing.get(item.ref)
            if fp is not None:
                board.Remove(fp)
                existing.pop(item.ref, None)
                result.removed += 1

        # -- replacements --------------------------------------------------
        for item in plan.enabled_items(plan_mod.REPLACE):
            if report(20, f"Replacing {item.ref}..."):
                result.cancelled = True
                return result
            old = existing.get(item.ref)
            if old is None:
                continue
            new = self._load_footprint(item.footprint, lib_paths)
            if new is None:
                # v12 silently kept the old footprint and counted it as updated.
                result.failed.append(f"{item.ref}: could not load {item.footprint}")
                continue
            new.SetReference(item.ref)
            new.SetValue(item.value)
            new.SetPosition(old.GetPosition())
            compat.set_orientation(new, compat.get_orientation(old))
            new.SetLayer(old.GetLayer())
            board.Remove(old)
            board.Add(new)
            existing[item.ref] = new
            managed.append(new)
            result.replaced += 1

        # -- additions -----------------------------------------------------
        additions = plan.enabled_items(plan_mod.ADD)
        for i, item in enumerate(additions):
            if i % 10 == 0 and report(
                30 + int(35 * i / max(len(additions), 1)), f"Adding components ({i + 1}/{len(additions)})..."
            ):
                result.cancelled = True
                return result
            fp = self._load_footprint(item.footprint, lib_paths)
            if fp is None:
                result.failed.append(f"{item.ref}: could not load {item.footprint}")
                continue
            fp.SetReference(item.ref)
            fp.SetValue(item.value)
            board.Add(fp)
            existing[item.ref] = fp
            new_footprints.append(fp)
            managed.append(fp)
            result.added += 1

        # -- value refresh -------------------------------------------------
        for item in plan.enabled_items(plan_mod.UPDATE):
            fp = existing.get(item.ref)
            if fp is not None:
                fp.SetValue(item.value)
                managed.append(fp)
                result.updated += 1

        # -- link footprints back to their schematic symbols ---------------
        if report(70, "Linking to schematic..."):
            result.cancelled = True
            return result
        self._link_paths(existing, netlist_path, result)

        if new_footprints:
            self._pack(new_footprints)

        # -- nets ----------------------------------------------------------
        if report(80, "Assigning nets..."):
            result.cancelled = True
            return result
        self._assign_nets(board, netlist_path, existing, result)

        if report(95, "Saving..."):
            result.cancelled = True
            return result

        p.SaveBoard(str(pcb_path), board)
        return result

    def _link_paths(self, footprints: dict[str, object], netlist_path: Path, result: ApplyResult) -> None:
        """
        Write each footprint's KIID path so KiCad knows which symbol it is.

        v12 read the wrong XML element for this, so the path was always empty and
        no footprint was ever linked. The visible symptom was KiCad's own
        "Update PCB from Schematic" treating every part as new.
        """
        p = compat.pcbnew()
        try:
            components = netlist_mod.parse_netlist(netlist_path)
        except netlist_mod.NetlistError as exc:
            result.warnings.append(f"Could not link footprints to symbols: {exc}")
            return

        linked = 0
        for ref, fp in footprints.items():
            comp = components.get(ref)
            if comp is None or not comp.path:
                continue
            try:
                fp.SetPath(p.KIID_PATH(comp.path))
                linked += 1
            except Exception:
                continue

        if footprints and not linked:
            result.warnings.append(
                "No footprints could be linked to schematic symbols. "
                "KiCad's own schematic-to-PCB update may see them as unlinked."
            )

    def _assign_nets(
        self, board, netlist_path: Path, footprints: dict[str, object], result: ApplyResult
    ) -> None:
        """
        Apply nets from the netlist, clearing stale ones first.

        The clearing pass is what v12 lacked. Because it only ever *set* nets, a
        connection deleted in the schematic stayed on the pad indefinitely, and
        the board's connectivity slowly diverged from the schematic with nothing
        reporting it.
        """
        p = compat.pcbnew()

        for fp in footprints.values():
            try:
                if fp.GetAttributes() & p.FP_BOARD_ONLY:
                    continue
            except (AttributeError, TypeError):
                pass
            try:
                if fp.IsLocked():
                    continue
            except AttributeError:
                pass
            for pad in fp.Pads():
                try:
                    pad.SetNetCode(0)
                except AttributeError:
                    try:
                        pad.SetNet(board.FindNet(0))
                    except Exception:
                        pass

        cache: dict[str, object] = {}
        try:
            for name, item in board.GetNetsByName().items():
                cache[str(name)] = item
        except Exception:
            pass

        def net_for(name: str):
            if name not in cache:
                item = p.NETINFO_ITEM(board, name)
                board.Add(item)
                cache[name] = item
            return cache[name]

        try:
            for net_name, nodes in netlist_mod.iter_nets(netlist_path):
                net = None
                for ref, pin in nodes:
                    fp = footprints.get(ref)
                    if fp is None:
                        continue
                    pad = fp.FindPadByNumber(pin)
                    if pad is None:
                        continue
                    if net is None:
                        net = net_for(net_name)
                    pad.SetNet(net)
        except netlist_mod.NetlistError as exc:
            result.warnings.append(f"Nets were not assigned: {exc}")
            return

        try:
            board.BuildConnectivity()
        except AttributeError:
            pass

    def _pack(self, footprints: list[object]) -> None:
        """Lay newly added footprints out on a grid clear of the existing board."""
        x0, y0 = PACK_ORIGIN
        for i, fp in enumerate(footprints):
            col, row = i % PACK_MAX_PER_ROW, i // PACK_MAX_PER_ROW
            fp.SetPosition(compat.vec_mm(x0 + col * PACK_GRID_SPACING, y0 + row * PACK_GRID_SPACING))

    # =====================================================================
    # Footprint loading
    # =====================================================================

    def _load_footprint(self, fpid: str, lib_paths: dict[str, Path]):
        """Load ``lib:name`` from the project's resolved library paths."""
        if not fpid or fpid in self._fp_failed:
            return None

        p = compat.pcbnew()
        lib, _, name = fpid.partition(":")
        if not name:
            lib, name = "", fpid

        candidates = []
        if lib and lib in lib_paths:
            candidates.append(str(lib_paths[lib]))
        candidates.extend(str(path) for nick, path in lib_paths.items() if nick != lib)

        for lib_path in candidates:
            try:
                fp = p.FootprintLoad(lib_path, name)
                if fp is not None:
                    return fp
            except Exception:
                continue

        self._fp_failed.add(fpid)
        return None

    def clear_footprint_cache(self) -> None:
        self._fp_cache.clear()
        self._fp_failed.clear()
