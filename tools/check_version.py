#!/usr/bin/env python3
"""
Assert every version string in the repository agrees with ``version.py``.

v12 shipped four different answers to "what version is this?": the README badge
said KiCad 9.0+, metadata.json said kicad_version 8.0, ``__version__`` said
"12.0", and the files it generated claimed generator 9.0 in one place and 10.0
in another. This runs in CI so that cannot recur.

Run: python tools/check_version.py
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from multiboard.version import MAX_KICAD, MIN_KICAD, __version__  # noqa: E402

PCM_VERSION = re.compile(r"^\d{1,4}(\.\d{1,4}(\.\d{1,6})?)?$")
PCM_KICAD = re.compile(r"^\d{1,2}(\.\d{1,2}(\.\d{1,2})?)?$")


def fail(message: str) -> None:
    print(f"FAIL {message}")
    fail.count += 1


fail.count = 0


def check_pyproject() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        fail("pyproject.toml has no version")
    elif match.group(1) != __version__:
        fail(f"pyproject.toml says {match.group(1)}, version.py says {__version__}")
    else:
        print(f"ok   pyproject.toml {__version__}")


def check_metadata() -> None:
    data = json.loads((ROOT / "metadata.json").read_text(encoding="utf-8"))
    versions = data.get("versions") or []
    if not versions:
        fail("metadata.json declares no versions")
        return

    entry = versions[0]
    version = entry.get("version", "")
    if version != __version__:
        fail(f"metadata.json version {version!r} != version.py {__version__!r}")
    elif not PCM_VERSION.match(version):
        fail(
            f"metadata.json version {version!r} is not a valid PCM version "
            "(digits and dots only, no suffixes)"
        )
    else:
        print(f"ok   metadata.json version {version}")

    expected_min = f"{MIN_KICAD[0]}.{MIN_KICAD[1]}"
    if entry.get("kicad_version") != expected_min:
        fail(f"metadata.json kicad_version {entry.get('kicad_version')!r} != {expected_min!r}")
    else:
        print(f"ok   metadata.json kicad_version {expected_min}")

    expected_max = f"{MAX_KICAD[0]}.{MAX_KICAD[1]}"
    if entry.get("kicad_version_max") != expected_max:
        fail(
            f"metadata.json kicad_version_max {entry.get('kicad_version_max')!r} != "
            f"{expected_max!r}. This field is what stops PCM offering a SWIG plugin "
            "to KiCad 11, where pcbnew no longer exists."
        )
    else:
        print(f"ok   metadata.json kicad_version_max {expected_max}")

    for field in ("kicad_version", "kicad_version_max"):
        value = entry.get(field, "")
        if value and not PCM_KICAD.match(value):
            fail(f"metadata.json {field} {value!r} does not match the PCM schema")

    if entry.get("runtime") != "swig":
        fail(f"metadata.json runtime {entry.get('runtime')!r} should be 'swig'")
    else:
        print("ok   metadata.json runtime swig")

    if entry.get("status") not in ("stable", "testing", "development", "deprecated"):
        fail(f"metadata.json status {entry.get('status')!r} is not a PCM status")

    for key in ("download_url", "download_sha256", "download_size", "install_size"):
        if key in entry:
            fail(f"metadata.json must not contain {key} inside the package")

    for field, limit in (("description", 500), ("description_full", 5000), ("name", 200)):
        if len(data.get(field, "")) > limit:
            fail(f"metadata.json {field} exceeds {limit} characters")

    tags = data.get("tags") or []
    bad_tags = [t for t in tags if not re.match(r"^[a-z][-a-z0-9]{0,48}[a-z0-9]$", t)]
    if bad_tags:
        fail(f"metadata.json tags must be lowercase kebab-case: {bad_tags}")
    elif tags:
        print(f"ok   metadata.json {len(tags)} tags")


def check_readme() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    expected_badge = f"KiCad-{MIN_KICAD[0]}.{MIN_KICAD[1]}"
    if expected_badge not in text:
        fail(f"README.md badge does not mention {expected_badge}")
    else:
        print(f"ok   README.md badge {expected_badge}")

    stale = re.findall(r"kicad[/\\](9\.0|8\.0)[/\\]", text)
    if stale:
        fail(f"README.md still references old KiCad config paths: {set(stale)}")


def check_no_hardcoded_versions() -> None:
    """No source file may state a plugin version of its own."""
    pattern = re.compile(r'__version__\s*=\s*"')
    offenders = []
    for path in (ROOT / "multiboard").rglob("*.py"):
        if path.name == "version.py":
            continue
        text = path.read_text(encoding="utf-8")
        if pattern.search(text) and "from .version" not in text:
            offenders.append(path.relative_to(ROOT).as_posix())
    if offenders:
        fail(f"these files declare their own __version__: {offenders}")
    else:
        print("ok   no duplicated version strings in multiboard/")


def main() -> int:
    print(f"version.py: {__version__}, KiCad {MIN_KICAD[0]}.{MIN_KICAD[1]}-{MAX_KICAD[0]}.{MAX_KICAD[1]}\n")
    check_pyproject()
    check_metadata()
    check_readme()
    check_no_hardcoded_versions()

    print()
    if fail.count:
        print(f"{fail.count} inconsistenc{'y' if fail.count == 1 else 'ies'} found.")
        return 1
    print("All version strings agree.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
