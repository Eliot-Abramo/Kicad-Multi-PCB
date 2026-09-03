# Copyright (c) 2026 Eliot Abramo
# SPDX-License-Identifier: MIT

"""
KiCad installation discovery.

v12 found ``kicad-cli`` with ``shutil.which`` plus a Windows-only directory
scan. Two consequences:

* macOS users got "kicad-cli not found" out of the box, because the binary lives
  inside the app bundle and is not on PATH. So did Flatpak and Snap users.
* The Windows scan used ``sorted(iterdir(), reverse=True)``, which is
  lexicographic -- ``"9.0" > "10.0"``. On a machine with both installed the
  plugin silently drove KiCad 9's toolchain against KiCad 10 files.

This module resolves an installation once, from several sources in confidence
order, and compares versions as parsed tuples.
"""

import os
import platform
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..constants import DISCOVERY_CACHE_TTL

Version = tuple[int, ...]

_VERSION_DIR = re.compile(r"^(?:kicad[-_]?)?(\d+)(?:\.(\d+))?(?:\.(\d+))?$", re.IGNORECASE)
_CLI_VERSION = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


@dataclass(frozen=True)
class KicadInstall:
    """A resolved KiCad installation."""

    version: Version
    cli: Optional[Path] = None
    share: Optional[Path] = None
    config_dir: Optional[Path] = None
    third_party: Optional[Path] = None
    source: str = "unknown"
    spawn_prefix: tuple[str, ...] = ()
    """Argv prefix needed to reach the binary, e.g. ``("flatpak-spawn", "--host")``."""

    @property
    def major(self) -> int:
        return self.version[0] if self.version else 0

    @property
    def major_minor(self) -> str:
        return f"{self.version[0]}.{self.version[1]}" if len(self.version) >= 2 else str(self.major)

    def describe(self) -> str:
        v = ".".join(str(p) for p in self.version) if self.version else "unknown"
        return f"KiCad {v} ({self.source})"


def parse_version_dir(name: str) -> Optional[Version]:
    """
    Parse a version-shaped directory name into a tuple, or None.

    Returning None for non-version names is deliberate: stray directories are
    skipped rather than sorted alongside real ones.

    >>> sorted(filter(None, map(parse_version_dir, ["9.0", "10.0", "bin"])))[-1]
    (10, 0)
    """
    m = _VERSION_DIR.match(name.strip())
    if not m:
        return None
    return tuple(int(g) for g in m.groups() if g is not None)


@dataclass(frozen=True)
class InProcHint:
    """
    What the host application knows about itself.

    ``core`` must not import pcbnew, so the caller supplies this instead of us
    sniffing for it. ``multiboard.compat.inproc_hint()`` builds one when running
    inside KiCad; the CLI passes nothing and discovery falls back to scanning.
    """

    version: Optional[Version] = None
    cli_candidates: tuple[Path, ...] = ()


_hint: InProcHint = InProcHint()


def set_inproc_hint(hint: InProcHint) -> None:
    """Register what the host knows. Called once, at plugin start-up."""
    global _hint
    _hint = hint
    invalidate()


def inproc_hint() -> InProcHint:
    return _hint


def _exe(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


def plausible_kicad_version(version: Optional[Version]) -> bool:
    """
    Whether a parsed tuple could actually be a KiCad version.

    KiCad majors have run 4..10 and will keep climbing; anything below 4 is a
    library version, a wxWidgets soname, or another stray number that happened to
    look like one. Without this guard a broken binary whose error message mentions
    ``libwx_gtk3u_core-3.2.so`` gets "discovered" as KiCad 3.2 and then drives
    every subsequent operation.
    """
    return bool(version) and 4 <= version[0] <= 99


CLI_PROBE_TIMEOUT = 5.0
"""
Seconds to wait for a ``kicad-cli version`` probe.

Discovery may try several candidates in turn, and every one of them blocks the
caller. ``kicad-cli version`` prints a string and exits; a binary that has not
answered in five seconds is broken or is on a filesystem that will not serve it
promptly, and either way the next candidate is the better bet. The previous
fifteen meant a machine with a stale KiCad on a dead network mount could stall
the plugin for a minute before showing anything.
"""

MAX_CLI_PROBES = 6
"""
Candidates to probe per discovery stage.

Each probe can cost up to :data:`CLI_PROBE_TIMEOUT`, so without a cap the worst
case grows with however many plausible-looking paths a machine happens to have.
Candidates are already ordered best-first -- newest version, nearest to the
running KiCad -- so the answer is in the first few or it is not there.
"""


def _cli_version(cli: Path, prefix: tuple[str, ...] = ()) -> Optional[Version]:
    """
    Ask a kicad-cli binary for its version.

    Only a *successful* run is trusted, and only its **stdout**. Reading stderr
    too means any diagnostic containing a version-shaped number -- a missing
    shared library is the common one -- is mistaken for the answer.
    """
    try:
        res = subprocess.run(
            [*prefix, str(cli), "version"],
            capture_output=True,
            text=True,
            timeout=CLI_PROBE_TIMEOUT,
            encoding="utf-8",
            errors="replace",
            env=child_env(),
            **({"creationflags": 0x08000000} if os.name == "nt" else {}),
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if res.returncode != 0:
        return None

    m = _CLI_VERSION.search(res.stdout or "")
    if not m:
        return None
    version = tuple(int(g) for g in m.groups() if g is not None)
    return version if plausible_kicad_version(version) else None


def child_env() -> dict[str, str]:
    """
    Environment for spawning kicad-cli.

    KiCad's embedded Python exports PYTHONHOME/PYTHONPATH/PYTHONEXECUTABLE. A
    child kicad-cli inherits them, tries to initialise against the wrong
    interpreter, and dies with an opaque error. This is a recurring failure mode
    for KiCad plugins on Windows and macOS, and it is invisible unless you
    capture stderr -- which v12 did not.
    """
    env = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONEXECUTABLE", "PYTHONSTARTUP", "PYTHONNOUSERSITE"):
        env.pop(key, None)
    return env


# =============================================================================
# Candidate enumeration
# =============================================================================


def _candidate_roots() -> list[Path]:
    """Directories that may contain versioned KiCad installs."""
    roots: list[Path] = []
    if os.name == "nt":
        for var in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(var)
            if base:
                roots.append(Path(base) / "Programs" / "KiCad")
                roots.append(Path(base) / "KiCad")
    return [r for r in roots if r.is_dir()]


def _versioned_cli_candidates() -> list[tuple[Version, Path]]:
    """``(version, cli_path)`` from versioned install roots, newest resolvable first."""
    out: list[tuple[Version, Path]] = []
    for root in _candidate_roots():
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            ver = parse_version_dir(entry.name)
            if not ver:
                continue
            cli = entry / "bin" / _exe("kicad-cli")
            if cli.exists():
                out.append((ver, cli))
    out.sort(key=lambda t: t[0], reverse=True)
    return out


def _fixed_cli_candidates() -> list[tuple[str, Path, tuple[str, ...]]]:
    """``(source, path, spawn_prefix)`` for non-versioned platform locations."""
    system = platform.system()
    out: list[tuple[str, Path, tuple[str, ...]]] = []

    if system == "Darwin":
        for base in (Path("/Applications"), Path.home() / "Applications"):
            out.append(
                ("appbundle", base / "KiCad" / "KiCad.app" / "Contents" / "MacOS" / _exe("kicad-cli"), ())
            )
            out.append(("appbundle", base / "KiCad.app" / "Contents" / "MacOS" / _exe("kicad-cli"), ()))
        out.append(("homebrew", Path("/opt/homebrew/bin/kicad-cli"), ()))
        out.append(("homebrew", Path("/usr/local/bin/kicad-cli"), ()))

    elif system == "Linux":
        appdir = os.environ.get("APPDIR")
        if appdir:
            out.append(("appimage", Path(appdir) / "usr" / "bin" / "kicad-cli", ()))
        out.append(("snap", Path("/snap/kicad/current/usr/bin/kicad-cli"), ()))

        # A Flatpak's binary cannot be executed directly from the host -- it
        # needs the runtime's libraries, and running it raw fails with a missing
        # shared object. Go through `flatpak run` instead, using the presence of
        # the binary only as a cheap "is it installed?" probe.
        for base in (Path("/var/lib/flatpak"), Path.home() / ".local/share/flatpak"):
            binary = base / "app/org.kicad.KiCad/current/active/files/bin/kicad-cli"
            if binary.exists():
                out.append(
                    (
                        "flatpak",
                        Path("kicad-cli"),
                        ("flatpak", "run", "--command=kicad-cli", "org.kicad.KiCad"),
                    )
                )
                break

        out.append(("scan", Path("/usr/bin/kicad-cli"), ()))
        out.append(("scan", Path("/usr/local/bin/kicad-cli"), ()))

    return out


def _in_sandbox() -> bool:
    return Path("/.flatpak-info").exists()


# =============================================================================
# Derived paths
# =============================================================================


def share_dirs(cli: Optional[Path], version: Version) -> Optional[Path]:
    """Locate ``share/kicad`` for an installation (stock symbol/footprint libs)."""
    mm = f"{version[0]}.{version[1]}" if len(version) >= 2 else str(version[0])
    candidates: list[Path] = []

    if cli:
        # <prefix>/bin/kicad-cli  ->  <prefix>/share/kicad
        candidates.append(cli.parent.parent / "share" / "kicad")
        # KiCad.app/Contents/MacOS/kicad-cli -> KiCad.app/Contents/SharedSupport
        candidates.append(cli.parent.parent / "SharedSupport")
        candidates.append(cli.parent.parent / "share" / "kicad" / mm)

    candidates += [
        Path("/usr/share/kicad"),
        Path("/usr/local/share/kicad"),
        Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport"),
        Path("/snap/kicad/current/usr/share/kicad"),
    ]

    for c in candidates:
        try:
            if (c / "footprints").is_dir() or (c / "symbols").is_dir():
                return c
        except OSError:
            continue
    return None


def config_dir_for(version: Version) -> Optional[Path]:
    """
    KiCad's user settings directory for a version.

    Windows ``%APPDATA%\\kicad\\10.0``, Linux ``~/.config/kicad/10.0``,
    macOS ``~/Library/Preferences/kicad/10.0``. Honours ``KICAD_CONFIG_HOME``.
    """
    mm = f"{version[0]}.{version[1]}" if len(version) >= 2 else f"{version[0]}.0"

    override = os.environ.get("KICAD_CONFIG_HOME")
    if override:
        return Path(override) / mm

    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA")
        root = Path(base) / "kicad" if base else None
    elif system == "Darwin":
        root = Path.home() / "Library" / "Preferences" / "kicad"
    else:
        base = os.environ.get("XDG_CONFIG_HOME")
        root = (Path(base) if base else Path.home() / ".config") / "kicad"

    return (root / mm) if root else None


def third_party_dir(version: Version) -> Optional[Path]:
    """Where PCM installs packages: ``$KICAD<N>_3RD_PARTY`` or the documents default."""
    major = version[0] if version else 10
    env = os.environ.get(f"KICAD{major}_3RD_PARTY")
    if env:
        return Path(env)

    system = platform.system()
    if system == "Windows" or system == "Darwin":
        base = Path.home() / "Documents" / "KiCad"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        base = (Path(xdg) if xdg else Path.home() / ".local" / "share") / "kicad"

    mm = f"{version[0]}.{version[1]}" if len(version) >= 2 else f"{version[0]}.0"
    return base / mm / "3rdparty"


def env_lib_dirs(version: Version) -> dict[str, Path]:
    """
    KiCad's library path variables, as used inside ``fp-lib-table`` URIs.

    v12 dropped any URI still containing ``${`` after substituting
    ``${KIPRJMOD}``, so every library referenced through
    ``${KICAD10_FOOTPRINT_DIR}`` silently vanished and its footprints "failed to
    load". These variables are frequently absent from the plugin's process
    environment, so we reconstruct them from the resolved installation.
    """
    major = version[0] if version else 10
    out: dict[str, Path] = {}

    for key in (
        f"KICAD{major}_FOOTPRINT_DIR",
        f"KICAD{major}_SYMBOL_DIR",
        f"KICAD{major}_3DMODEL_DIR",
        f"KICAD{major}_TEMPLATE_DIR",
        f"KICAD{major}_3RD_PARTY",
    ):
        val = os.environ.get(key)
        if val:
            out[key] = Path(val)

    share = share_dirs(None, version)
    if share:
        out.setdefault(f"KICAD{major}_FOOTPRINT_DIR", share / "footprints")
        out.setdefault(f"KICAD{major}_SYMBOL_DIR", share / "symbols")
        out.setdefault(f"KICAD{major}_3DMODEL_DIR", share / "3dmodels")
        out.setdefault(f"KICAD{major}_TEMPLATE_DIR", share / "template")

    tp = third_party_dir(version)
    if tp:
        out.setdefault(f"KICAD{major}_3RD_PARTY", tp)

    return out


# =============================================================================
# Discovery
# =============================================================================

_cache: Optional[KicadInstall] = None
_neg_until: float = 0.0


def invalidate() -> None:
    """Forget the cached result. Wired to Doctor's "Re-detect" action."""
    global _cache, _neg_until
    _cache = None
    _neg_until = 0.0


def discover(*, refresh: bool = False) -> Optional[KicadInstall]:
    """
    Resolve the KiCad installation to drive, or None.

    Order, first usable hit wins:

    1. ``KICAD_CLI`` -- the documented user escape hatch.
    2. The in-process hint, if the host registered one (see :class:`InProcHint`).
    3. ``shutil.which("kicad-cli")``.
    4. Versioned install roots (Windows), newest by parsed tuple.
    5. Fixed platform locations: macOS app bundle, Homebrew, Flatpak, Snap,
       AppImage, distro paths.

    A failure is cached briefly so a missing install does not cost a filesystem
    walk on every call.
    """
    global _cache, _neg_until

    if not refresh:
        if _cache is not None:
            return _cache
        if time.monotonic() < _neg_until:
            return None

    install = _discover_uncached()
    if install is not None:
        _cache = install
    else:
        _neg_until = time.monotonic() + DISCOVERY_CACHE_TTL
    return install


def _build(
    version: Version, cli: Optional[Path], source: str, spawn_prefix: tuple[str, ...] = ()
) -> KicadInstall:
    return KicadInstall(
        version=version,
        cli=cli,
        share=share_dirs(cli, version),
        config_dir=config_dir_for(version),
        third_party=third_party_dir(version),
        source=source,
        spawn_prefix=spawn_prefix,
    )


def _discover_uncached() -> Optional[KicadInstall]:
    hint = inproc_hint()
    inproc = hint.version

    # 1. Explicit override.
    override = os.environ.get("KICAD_CLI")
    if override:
        cli = Path(override).expanduser()
        if cli.exists():
            ver = _cli_version(cli) or inproc or (10, 0, 0)
            return _build(ver, cli, "env")

    # 2. The host application's own installation, if it told us about it.
    if inproc:
        for cli in hint.cli_candidates[:MAX_CLI_PROBES]:
            if _cli_version(cli):
                return _build(inproc, cli, "inproc")

    # 3. PATH.
    which = shutil.which("kicad-cli")
    if which:
        cli = Path(which)
        ver = _cli_version(cli)
        if ver:
            # Trust the running pcbnew's version over the CLI's if they disagree
            # on major -- Doctor reports the mismatch separately.
            return _build(inproc or ver, cli, "path")

    # 4. Versioned roots, newest first by tuple.
    for _ver, cli in _versioned_cli_candidates()[:MAX_CLI_PROBES]:
        real = _cli_version(cli)
        if real:
            return _build(real, cli, "scan")

    # 5. Fixed platform locations. A candidate with a spawn prefix is invoked
    # through that prefix, so its path need not exist on the host filesystem.
    for source, cli, prefix in _fixed_cli_candidates():
        if not prefix and not cli.exists():
            continue
        ver = _cli_version(cli, prefix)
        if ver:
            return _build(ver, cli, source, prefix)

    # 5b. Flatpak host escape, when we are inside a sandbox.
    if _in_sandbox():
        prefix = ("flatpak-spawn", "--host")
        ver = _cli_version(Path("kicad-cli"), prefix)
        if ver:
            return _build(ver, Path("kicad-cli"), "flatpak-host", prefix)

    # Last resort: we know the version from pcbnew but found no CLI. Return it
    # anyway so the UI can say "KiCad 10 detected, kicad-cli missing" instead of
    # the useless "kicad-cli not found".
    if inproc:
        return _build(inproc, None, "inproc-nocli")

    return None
