"""
Packaging: the archive must contain exactly what PCM expects and nothing else.

v12's release ZIP shipped a complete ``.git`` directory -- history, remote URL, a
654 KB packfile -- plus ``__pycache__`` bytecode and two install scripts that had
already been deleted from the repository. About 85% of that archive was git
objects. These tests make the same mistake impossible rather than merely unlikely.
"""

import json
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN = (".git", "__pycache__", ".pyc", ".pyo", "tests/", "tools/", ".DS_Store")


@pytest.fixture(scope="module")
def archive(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("dist")
    result = subprocess.run(
        [sys.executable, "tools/build_package.py", "--out", str(out)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    built = list(out.glob("*.zip"))
    assert len(built) == 1, built
    return built[0]


@pytest.fixture(scope="module")
def names(archive: Path) -> list:
    with zipfile.ZipFile(archive) as zf:
        return zf.namelist()


def test_archive_excludes_everything_it_should(names):
    bad = [n for n in names if any(token in n for token in FORBIDDEN)]
    assert not bad, f"archive contains {bad}"


def test_pcm_layout(names):
    """No wrapper directory; metadata and resources at the archive root."""
    assert "metadata.json" in names
    assert "resources/icon.png" in names
    assert "plugins/multiboard/__init__.py" in names
    assert not any(n.startswith("multiboard-") for n in names)


def test_every_source_module_ships(names):
    shipped = {n for n in names if n.endswith(".py")}
    for package in ("multiboard", "multiboard/core", "multiboard/backend", "multiboard/ui"):
        for source in (ROOT / package).glob("*.py"):
            expected = f"plugins/{source.relative_to(ROOT).as_posix()}"
            assert expected in shipped, f"{expected} missing from the archive"


def test_toolbar_icons_ship(names):
    assert "plugins/multiboard/icons/icon.png" in names
    assert "plugins/multiboard/icons/icon@2x.png" in names


def test_package_icon_is_exactly_64px():
    """PCM rejects anything else. The repo shipped a 24x24 for years."""
    data = (ROOT / "resources" / "icon.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", data[16:24]) == (64, 64)


def test_archive_is_reproducible(tmp_path):
    """Two builds of one commit must produce identical bytes, and so one sha256."""
    import hashlib

    digests = []
    for i in range(2):
        out = tmp_path / f"build{i}"
        subprocess.run(
            [sys.executable, "tools/build_package.py", "--out", str(out)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=True,
        )
        built = next(out.glob("*.zip"))
        digests.append(hashlib.sha256(built.read_bytes()).hexdigest())
    assert digests[0] == digests[1]


def test_in_package_metadata_has_no_download_keys():
    """Those keys belong only in metadata submitted to a package repository."""
    data = json.loads((ROOT / "metadata.json").read_text(encoding="utf-8"))
    entry = data["versions"][0]
    for key in ("download_url", "download_sha256", "download_size", "install_size"):
        assert key not in entry


def test_metadata_targets_kicad_10_only():
    from multiboard.version import MAX_KICAD, MIN_KICAD, __version__

    entry = json.loads((ROOT / "metadata.json").read_text(encoding="utf-8"))["versions"][0]
    assert entry["version"] == __version__
    assert entry["kicad_version"] == f"{MIN_KICAD[0]}.{MIN_KICAD[1]}"
    # This is what stops PCM offering a SWIG plugin to KiCad 11.
    assert entry["kicad_version_max"] == f"{MAX_KICAD[0]}.{MAX_KICAD[1]}"
    assert entry["runtime"] == "swig"


def test_version_check_script_passes():
    result = subprocess.run(
        [sys.executable, "tools/check_version.py"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# =============================================================================
# PCM schema constraints
#
# Several fields that read like display labels are constrained to lowercase
# kebab-case. Getting one wrong is only discovered at install time, where PCM
# reports a bare regex and does not name the field. These assert the whole set.
# =============================================================================

PCM_SLUG = r"^[a-z][-a-z0-9]{0,48}[a-z0-9]$"
PCM_IDENTIFIER = r"^[a-zA-Z][-a-zA-Z0-9.]{0,98}[a-zA-Z0-9]$"


@pytest.fixture(scope="module")
def metadata() -> dict:
    return json.loads((ROOT / "metadata.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("field", ["type", "category"])
def test_slug_fields_are_lowercase_kebab_case(metadata, field):
    """`category: "Design Tools"` is rejected by PCM; `"design-tools"` is not."""
    import re

    value = metadata.get(field)
    if value is None:
        pytest.skip(f"{field} is optional and absent")
    assert re.match(PCM_SLUG, value), f"{field}={value!r} must match {PCM_SLUG}"


def test_tags_are_lowercase_kebab_case(metadata):
    import re

    tags = metadata.get("tags") or []
    assert tags, "PCM requires at least one tag"
    assert len(set(tags)) == len(tags), "tags must be unique"
    bad = [t for t in tags if not re.match(PCM_SLUG, t)]
    assert not bad, f"tags must match {PCM_SLUG}: {bad}"


def test_identifier_is_reverse_dns(metadata):
    import re

    assert re.match(PCM_IDENTIFIER, metadata["identifier"])


def test_required_root_fields_present(metadata):
    for field in (
        "name",
        "description",
        "description_full",
        "identifier",
        "type",
        "author",
        "license",
        "resources",
        "versions",
    ):
        assert metadata.get(field), f"PCM requires {field}"


@pytest.mark.parametrize("field,limit", [("name", 200), ("description", 500), ("description_full", 5000)])
def test_text_length_limits(metadata, field, limit):
    assert len(metadata[field]) <= limit, f"{field} is {len(metadata[field])} chars, max {limit}"


def test_status_and_platforms_are_valid(metadata):
    entry = metadata["versions"][0]
    assert entry["status"] in ("stable", "testing", "development", "deprecated")
    assert set(entry.get("platforms", [])) <= {"linux", "macos", "windows"}


def test_contact_keys_are_lowercase(metadata):
    import re

    for role in ("author", "maintainer"):
        for key in metadata.get(role, {}).get("contact") or {}:
            assert re.match(r"^[a-z][-a-z0-9 ]{0,48}[a-z0-9]$", key), f"{role}.contact.{key}"
