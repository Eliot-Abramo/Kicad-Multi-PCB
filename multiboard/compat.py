"""
The pcbnew surface, for KiCad 10.

This plugin targets KiCad 10 only, so this is not a 9-vs-10 shim. What it *is*
is a set of probes for the handful of calls whose exact signature or spelling we
could not verify without a running KiCad, plus one hard version gate.

Two facts shape everything here:

* ``FOOTPRINT::GetFieldByName`` and ``HasFieldByName`` were **removed** in
  KiCad 10 in favour of ``GetField(name)`` / ``HasField(name)``, and
  ``GetFields()`` now returns a deque rather than a vector. Plugins that call
  the old names fail with ``AttributeError`` on 10 while working on 9.
* KiCad 11 removes the SWIG bindings entirely. There is no forward compatibility
  to design for -- there is only failing loudly and early.

Every probe resolves once at import and caches, so nothing pays a ``hasattr``
cost per call.
"""

import math
from pathlib import Path
from typing import Any, Optional

from .version import MAX_KICAD, MIN_KICAD

_pcbnew = None
_version: Optional[tuple[int, ...]] = None


def pcbnew():
    """The pcbnew module, imported lazily so core code paths never pull it in."""
    global _pcbnew
    if _pcbnew is None:
        import pcbnew as _mod

        _pcbnew = _mod
    return _pcbnew


def available() -> bool:
    try:
        pcbnew()
        return True
    except ImportError:
        return False


UNKNOWN_VERSION = ()
"""Returned when the running KiCad's version cannot be determined at all."""

_VERSION_TEXT = None


def kicad_version() -> tuple[int, ...]:
    """
    ``(major, minor, patch)`` of the running KiCad, or :data:`UNKNOWN_VERSION`.

    Every accessor here is treated as untrusted. ``GetMajorMinorPatchTuple()``
    is documented to return a tuple of ints, but in a real KiCad 10 build SWIG
    hands back an opaque ``SwigPyObject`` wrapping ``std::tuple`` that is not
    iterable -- so ``tuple(...)`` raises ``TypeError``, not ``AttributeError``.
    Anything that is not a plain string or a genuinely iterable sequence falls
    through to parsing the textual accessors, which are stable.
    """
    global _version, _VERSION_TEXT
    if _version is not None:
        return _version

    import re

    mod = pcbnew()
    pattern = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")

    # Structured accessor first, but only if it really yields numbers.
    try:
        raw = mod.GetMajorMinorPatchTuple()
        parts = tuple(int(x) for x in raw)
        if len(parts) >= 2:
            _version = parts
            _VERSION_TEXT = ".".join(str(p) for p in parts)
            return _version
    except Exception:
        pass

    # Textual accessors. str() on a SWIG object is always safe, and these
    # return plain strings like "10.0.5" in every build.
    for fn in (
        "GetMajorMinorPatchVersion",
        "GetMajorMinorVersion",
        "GetSemanticVersion",
        "GetBaseVersion",
        "GetBuildVersion",
        "Version",
        "FullVersion",
    ):
        try:
            text = str(getattr(mod, fn)())
        except Exception:
            continue
        match = pattern.search(text)
        if match:
            _version = tuple(int(g) for g in match.groups() if g is not None)
            _VERSION_TEXT = text
            return _version

    _version = UNKNOWN_VERSION
    return _version


def kicad_version_text() -> str:
    """The version as KiCad reported it, for diagnostics."""
    kicad_version()
    return _VERSION_TEXT or "unknown"


def inproc_hint():
    """
    Tell ``core.kicad_env`` what the KiCad we are running inside looks like.

    This is the most reliable source of both the version and the kicad-cli path,
    and v12 used neither -- it went straight to ``shutil.which``. It lives here
    rather than in ``core`` because ``core`` may not import pcbnew; the plugin
    registers the hint once at start-up and discovery picks it up from there.
    """
    import sys

    from .core.kicad_env import InProcHint

    if not available():
        return InProcHint()

    exe = "kicad-cli.exe" if __import__("os").name == "nt" else "kicad-cli"
    seeds = []
    if sys.executable:
        seeds.append(Path(sys.executable).parent)
    module = getattr(pcbnew(), "__file__", "") or ""
    if module:
        # list() first: Path.parents accepts slices only on Python 3.10+,
        # and KiCad's bundled interpreter is 3.9
        seeds.extend(list(Path(module).parents)[:6])

    candidates = []
    for seed in seeds:
        for rel in (".", "bin", "..", "../bin", "../../bin", "../MacOS"):
            try:
                cand = (seed / rel / exe).resolve()
            except OSError:
                continue
            if cand.exists() and cand not in candidates:
                candidates.append(cand)

    version = kicad_version()
    return InProcHint(
        version=version or None,  # UNKNOWN_VERSION must not read as "version 0"
        cli_candidates=tuple(candidates),
    )


def install_hint() -> None:
    """Register :func:`inproc_hint` with the discovery layer. Idempotent."""
    from .core.kicad_env import set_inproc_hint

    set_inproc_hint(inproc_hint())


def require_supported() -> None:
    """
    Raise a clear error outside the supported KiCad range.

    Called before any dialog opens. Failing here with a sentence the user can act
    on is far better than a SWIG ``TypeError`` surfacing mid-update with a
    half-written board on disk.
    """
    if not available():
        raise RuntimeError(
            "Multi-Board Manager needs KiCad's Python (pcbnew) bindings.\n\n"
            "KiCad 11 removed them, so this version of the plugin cannot run there. "
            "Check the project page for an updated release."
        )

    version = kicad_version()

    if not version:
        # We could not read a version at all. pcbnew imported, so we are inside
        # some KiCad; refusing to run on a version we merely failed to parse
        # would be worse than proceeding. Doctor reports it separately.
        return

    if version[:2] < MIN_KICAD:
        raise RuntimeError(
            f"Multi-Board Manager {'.'.join(map(str, MIN_KICAD))}+ requires KiCad "
            f"{MIN_KICAD[0]}.{MIN_KICAD[1]} or newer.\n\n"
            f"Detected KiCad {'.'.join(map(str, version))}."
        )
    if version[:2] > MAX_KICAD:
        raise RuntimeError(
            f"Multi-Board Manager supports KiCad up to {MAX_KICAD[0]}.x.\n\n"
            f"Detected KiCad {'.'.join(map(str, version))}. KiCad 11 removed the "
            "SWIG Python bindings this plugin is built on."
        )


# =============================================================================
# Probed capabilities
# =============================================================================

_probes: dict[str, Any] = {}


def _probe(name: str, fn):
    if name not in _probes:
        try:
            _probes[name] = fn()
        except Exception:
            _probes[name] = None
    return _probes[name]


def has(symbol: str) -> bool:
    """Whether pcbnew exposes ``symbol``. Cached."""
    return _probe(f"has:{symbol}", lambda: hasattr(pcbnew(), symbol)) or False


def capabilities() -> dict[str, bool]:
    """What this KiCad build actually offers. Reported verbatim by Doctor."""
    return {
        "NewBoard": has("NewBoard"),
        "CreateEmptyBoard": has("CreateEmptyBoard"),
        "FootprintSave": has("FootprintSave"),
        "FootprintLibCreate": has("FootprintLibCreate"),
        "PCB_IO_MGR": has("PCB_IO_MGR"),
        "FocusOnItem": has("FocusOnItem"),
        "Refresh": has("Refresh"),
        "PADSTACK": has("PADSTACK"),
        "STROKE_PARAMS": has("STROKE_PARAMS"),
        "EDA_ANGLE": has("EDA_ANGLE"),
    }


# =============================================================================
# Geometry helpers
# =============================================================================


def mm(value: float) -> int:
    """Millimetres to KiCad internal units (nanometres)."""
    return pcbnew().FromMM(value)


def vec_mm(x: float, y: float):
    """A ``VECTOR2I`` from millimetre coordinates."""
    p = pcbnew()
    return p.VECTOR2I(p.FromMM(x), p.FromMM(y))


def angle(degrees: float):
    """
    An angle object, or a raw value if this build wants tenths of a degree.

    ``EDA_ANGLE`` has been the type since KiCad 7; the fallback covers builds
    where the SWIG wrapper did not export it.
    """
    p = pcbnew()
    if has("EDA_ANGLE"):
        try:
            return p.EDA_ANGLE(degrees, p.DEGREES_T)
        except (AttributeError, TypeError):
            pass
    return degrees * 10.0


def set_stroke(shape, width_mm: float, style: str = "solid") -> None:
    """
    Set a graphic's stroke width and dash style.

    The line-style enum was renamed after KiCad 7 (``PLOT_DASH_TYPE_*`` became
    ``LINE_STYLE_*``), so both spellings are probed.
    """
    p = pcbnew()
    style_value = None
    for prefix in ("LINE_STYLE_", "PLOT_DASH_TYPE_"):
        candidate = getattr(p, f"{prefix}{style.upper()}", None)
        if candidate is not None:
            style_value = candidate
            break

    if has("STROKE_PARAMS") and style_value is not None:
        try:
            shape.SetStroke(p.STROKE_PARAMS(mm(width_mm), style_value))
            return
        except (AttributeError, TypeError):
            pass

    try:
        shape.SetWidth(mm(width_mm))
    except Exception:
        pass


def pad_set_size(pad, width_mm: float, height_mm: float) -> None:
    """
    Set a pad's size, tolerating the per-layer padstack signature.

    KiCad 9 introduced per-layer padstacks and ``PAD::SetSize`` gained a layer
    parameter. Whether the one-argument overload survives in a given 10.x build
    is the single API detail here most likely to differ, so both arities are
    attempted rather than assumed.
    """
    p = pcbnew()
    size = vec_mm(width_mm, height_mm)
    try:
        pad.SetSize(size)
        return
    except (TypeError, NotImplementedError):
        pass

    for enum_path in (("PADSTACK", "ALL_LAYERS"), ("PADSTACK", "ALL_LAYERS_MASK")):
        holder = getattr(p, enum_path[0], None)
        layer = getattr(holder, enum_path[1], None) if holder is not None else None
        if layer is not None:
            try:
                pad.SetSize(layer, size)
                return
            except (TypeError, NotImplementedError, AttributeError):
                continue

    try:
        pad.SetSize(p.F_Cu, size)
    except Exception:
        pass


# =============================================================================
# Footprint fields
# =============================================================================


def fp_fields(fp) -> dict[str, str]:
    """
    Every field on a footprint as ``{name: text}``.

    ``GetFieldsText()`` is a SWIG-layer helper rather than a C++ method, which
    makes it the stable surface across the field-API refactor.
    """
    try:
        # A SWIG map is usually dict()-able, but do not bet the call on it.
        return {str(k): str(v) for k, v in fp.GetFieldsText().items()}
    except Exception:
        pass

    out: dict[str, str] = {}
    try:
        for field in fp.GetFields():
            try:
                out[field.GetName()] = field.GetText()
            except Exception:
                continue
    except Exception:
        pass
    return out


def fp_get_field(fp, name: str, default: str = "") -> str:
    """One field's text. ``GetFieldByName`` was removed in KiCad 10."""
    try:
        return fp.GetFieldText(name)
    except (AttributeError, KeyError):
        pass
    try:
        field = fp.GetField(name)
        if field is not None:
            return field.GetText()
    except (AttributeError, TypeError, KeyError):
        pass
    return fp_fields(fp).get(name, default)


def fp_set_field(fp, name: str, value: str) -> bool:
    """Set a field, creating it if absent. Returns whether it worked."""
    try:
        fp.SetField(name, value)
        return True
    except (AttributeError, TypeError):
        return False


def fpid_string(fp) -> str:
    """``"lib:footprint"`` for a footprint."""
    try:
        fpid = fp.GetFPID()
        return f"{fpid.GetLibNickname()}:{fpid.GetLibItemName()}"
    except Exception:
        return ""


def set_orientation(fp, degrees: float) -> None:
    try:
        fp.SetOrientationDegrees(degrees)
    except Exception:
        fp.SetOrientation(angle(degrees))


def get_orientation(fp) -> float:
    try:
        return float(fp.GetOrientationDegrees())
    except Exception:
        try:
            return float(fp.GetOrientation().AsDegrees())
        except Exception:
            return 0.0


def normalize_degrees(value: float) -> float:
    return math.fmod(math.fmod(value, 360.0) + 360.0, 360.0)
