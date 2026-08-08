#!/usr/bin/env python3
"""
Build the Plugin and Content Manager archive.

Built from an explicit **allowlist**, never a directory walk. The v12 release ZIP
was assembled by walking the source tree, and it shipped a complete ``.git``
directory -- history, remote URL, a 654 KB packfile -- plus ``__pycache__``
bytecode pinned to CPython 3.11 and two install scripts that had been deleted
from the repository. Roughly 85% of that archive was git objects.

The archive is also reproducible: fixed timestamps, sorted entries, deflate. Two
builds of the same commit produce byte-identical output and therefore the same
sha256, which is what a package repository needs.

Run: python tools/build_package.py [--out dist]
"""

import argparse
import hashlib
import json
import struct
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from multiboard.version import __version__  # noqa: E402

# (source, destination-inside-archive). Directories expand to their .py files.
ALLOWLIST = [
    ("metadata.json", "metadata.json"),
    ("LICENSE", "plugins/LICENSE"),
    ("resources/icon.png", "resources/icon.png"),
]
PACKAGE_DIRS = ["multiboard", "multiboard/core", "multiboard/backend", "multiboard/ui"]
ICON_DIR = "multiboard/icons"

FORBIDDEN = (
    ".git",
    "__pycache__",
    ".pyc",
    ".pyo",
    ".DS_Store",
    ".pytest_cache",
    "tests/",
    "tools/",
    ".zip",
    ".egg-info",
)

# 1980-01-01, the ZIP epoch: constant output regardless of checkout mtimes.
FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def collect() -> list:
    """Every (source_path, archive_name) pair that belongs in the package."""
    entries = []

    for source, dest in ALLOWLIST:
        path = ROOT / source
        if not path.exists():
            raise SystemExit(f"error: required file missing: {source}")
        entries.append((path, dest))

    for directory in PACKAGE_DIRS:
        for path in sorted((ROOT / directory).glob("*.py")):
            entries.append((path, f"plugins/{path.relative_to(ROOT).as_posix()}"))

    for path in sorted((ROOT / ICON_DIR).glob("*.png")):
        entries.append((path, f"plugins/{path.relative_to(ROOT).as_posix()}"))

    return sorted(entries, key=lambda e: e[1])


def verify(names: list) -> None:
    """Refuse to write an archive containing anything that must never ship."""
    bad = [n for n in names if any(token in n for token in FORBIDDEN)]
    if bad:
        raise SystemExit("error: archive would contain excluded files:\n  " + "\n  ".join(bad))

    if "metadata.json" not in names:
        raise SystemExit("error: metadata.json missing from archive")
    if "resources/icon.png" not in names:
        raise SystemExit("error: resources/icon.png missing from archive")
    if not any(n.startswith("plugins/multiboard/") for n in names):
        raise SystemExit("error: no plugin sources in archive")


def check_icon() -> None:
    """PCM requires the package icon to be exactly 64x64."""
    data = (ROOT / "resources" / "icon.png").read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit("error: resources/icon.png is not a PNG")
    width, height = struct.unpack(">II", data[16:24])
    if (width, height) != (64, 64):
        raise SystemExit(
            f"error: resources/icon.png is {width}x{height}; PCM requires exactly 64x64. "
            "Run: python tools/make_icons.py"
        )


def check_metadata() -> dict:
    """The in-package metadata must not carry the repository-only download keys."""
    data = json.loads((ROOT / "metadata.json").read_text(encoding="utf-8"))
    versions = data.get("versions") or []
    if not versions:
        raise SystemExit("error: metadata.json declares no versions")

    entry = versions[0]
    if entry.get("version") != __version__:
        raise SystemExit(
            f"error: metadata.json version {entry.get('version')!r} does not match "
            f"multiboard/version.py ({__version__!r})"
        )

    leaked = [k for k in ("download_url", "download_sha256", "download_size", "install_size") if k in entry]
    if leaked:
        raise SystemExit(
            "error: metadata.json inside the package must not contain "
            f"{leaked}. Those keys belong only in the metadata submitted to a "
            "package repository."
        )
    return data


def build(out_dir: Path) -> Path:
    check_icon()
    check_metadata()

    entries = collect()
    names = [name for _path, name in entries]
    verify(names)

    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"multiboard-{__version__}-pcm.zip"
    archive.unlink(missing_ok=True)

    install_size = 0
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path, name in entries:
            data = path.read_bytes()
            install_size += len(data)
            info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)

    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    size = archive.stat().st_size

    # --out may point anywhere, including outside the repository.
    try:
        shown = archive.relative_to(ROOT)
    except ValueError:
        shown = archive
    print(f"{shown}")
    print(f"  version       {__version__}")
    print(f"  files         {len(entries)}")
    print(f"  download_size {size}")
    print(f"  install_size  {install_size}")
    print(f"  sha256        {digest}")
    print()
    print("For a package repository, add these to the version entry (never to the")
    print("metadata.json inside the archive):")
    print(
        json.dumps(
            {
                "download_url": f"https://github.com/Eliot-Abramo/Kicad-Multi-PCB/releases/download/v{__version__}/{archive.name}",
                "download_sha256": digest,
                "download_size": size,
                "install_size": install_size,
            },
            indent=2,
        )
    )
    return archive


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="dist", help="output directory (default: dist)")
    args = parser.parse_args()
    build(ROOT / args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
