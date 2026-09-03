# Copyright (c) 2026 Eliot Abramo
# SPDX-License-Identifier: MIT

"""Config migration, netlist parsing, board scanning, colour maths, discovery."""

import json
from pathlib import Path

import pytest
from conftest import make_netlist, make_pcb

from multiboard.core import color, kicad_env
from multiboard.core.cache import ConfigCorrupt, atomic_write_json, read_json
from multiboard.core.config import ProjectConfig, load, migrate, save
from multiboard.core.netlist import parse_netlist, top_level_sheets
from multiboard.core.pcb_scan import scan_pcb_file, validate_footprint_library
from multiboard.version import CONFIG_SCHEMA

# =============================================================================
# Atomic writes
# =============================================================================


def test_write_keeps_the_previous_version_as_backup(tmp_path):
    p = tmp_path / "c.json"
    atomic_write_json(p, {"v": 1})
    atomic_write_json(p, {"v": 2})
    assert json.loads(p.read_text())["v"] == 2
    assert json.loads(p.with_suffix(".json.bak").read_text())["v"] == 1


def test_corrupt_file_recovers_from_backup_and_says_so(tmp_path):
    p = tmp_path / "c.json"
    atomic_write_json(p, {"v": 1})
    atomic_write_json(p, {"v": 2})
    p.write_text("{ this is not json", encoding="utf-8")

    data, warning = read_json(p)
    assert data["v"] == 1
    assert warning and "recovered" in warning


def test_corrupt_with_no_backup_raises_rather_than_silently_emptying(tmp_path):
    """v12 logged and continued with an empty config: every board vanished."""
    p = tmp_path / "c.json"
    p.write_text("{ broken", encoding="utf-8")
    with pytest.raises(ConfigCorrupt):
        read_json(p)


def test_no_temp_files_survive_a_write(tmp_path):
    p = tmp_path / "c.json"
    atomic_write_json(p, {"v": 1})
    assert [f.name for f in tmp_path.iterdir() if ".tmp" in f.name] == []


# =============================================================================
# Config migration
# =============================================================================


def test_v12_config_migrates_without_losing_boards():
    v12 = {
        "version": "12.0",
        "root_schematic": "demo.kicad_sch",
        "boards": {
            "Power": {
                "name": "Power",
                "pcb_path": "boards/Power/Power.kicad_pcb",
                "description": "Regulators",
                "ports": {"VIN": {"name": "VIN", "net": "VIN", "side": "left", "position": 0.25}},
            }
        },
    }
    cfg = ProjectConfig.from_dict(v12)

    assert cfg.schema == CONFIG_SCHEMA
    assert cfg.plugin_version == "12.0"  # what wrote it, preserved
    assert cfg.boards["Power"].description == "Regulators"
    assert cfg.boards["Power"].ports["VIN"].position == 0.25
    assert cfg.assignments == {} and cfg.rules == []  # v12 semantics preserved


def test_migration_is_idempotent():
    once = migrate({"version": "12.0", "boards": {}})
    assert migrate(once) == once


def test_legacy_string_board_entry_does_not_crash():
    """v12 produced pcb_path='' here, which is what made rmtree dangerous."""
    cfg = ProjectConfig.from_dict({"boards": {"Power": "Power"}})
    assert cfg.boards["Power"].pcb_path == ""


def test_board_entry_missing_its_name_key_is_tolerated():
    """v12 used data["name"] and took the whole config down with a KeyError."""
    cfg = ProjectConfig.from_dict({"boards": {"Power": {"pcb_path": "x.kicad_pcb"}}})
    assert cfg.boards["Power"].name == "Power"


def test_dict_key_is_authoritative_over_a_drifted_name():
    cfg = ProjectConfig.from_dict({"boards": {"Power": {"name": "Stale", "pcb_path": "p"}}})
    assert cfg.boards["Power"].name == "Power"


def test_roundtrip_through_disk(tmp_path):
    cfg = ProjectConfig(root_schematic="demo.kicad_sch")
    cfg.assignments["R1"] = "Power"
    save(tmp_path / "c.json", cfg)
    loaded, warning = load(tmp_path / "c.json")
    assert warning is None
    assert loaded.assignments == {"R1": "Power"}


def test_renaming_a_board_carries_assignments_and_rules():
    from multiboard.core.config import AssignRule, BoardConfig

    cfg = ProjectConfig()
    cfg.boards["Power"] = BoardConfig("Power", "p.kicad_pcb")
    cfg.assignments["R1"] = "Power"
    cfg.rules.append(AssignRule("sheet", "/Power/", "Power"))

    cfg.rename_board("Power", "Supply")

    assert cfg.assignments["R1"] == "Supply"
    assert cfg.rules[0].board == "Supply"
    assert "Power" not in cfg.boards


def test_forgetting_a_board_drops_its_assignments_and_rules():
    from multiboard.core.config import AssignRule, BoardConfig

    cfg = ProjectConfig()
    cfg.boards["Power"] = BoardConfig("Power", "p.kicad_pcb")
    cfg.assignments["R1"] = "Power"
    cfg.rules.append(AssignRule("sheet", "/Power/", "Power"))

    cfg.forget_board("Power")

    assert cfg.assignments == {} and cfg.rules == []


def test_port_defaults_to_its_own_name_as_net():
    from multiboard.core.config import PortDef

    assert PortDef(name="VIN").effective_net() == "VIN"
    assert PortDef(name="VIN", net="V_IN").effective_net() == "V_IN"


# =============================================================================
# Netlist
# =============================================================================


def test_tstamps_plural_is_read(tmp_path):
    """
    v12 looked for <tstamp>; KiCad 6+ writes <tstamps>. The consequence was that
    no footprint was ever linked back to its schematic symbol.
    """
    p = tmp_path / "n.xml"
    p.write_text(make_netlist([{"ref": "R1", "tstamps": "/abc-123"}]), encoding="utf-8")
    assert parse_netlist(p)["R1"].path == "/abc-123"


def test_path_is_normalised_to_one_leading_slash(tmp_path):
    p = tmp_path / "n.xml"
    p.write_text(make_netlist([{"ref": "R1", "tstamps": "abc/def/"}]), encoding="utf-8")
    assert parse_netlist(p)["R1"].path == "/abc/def"


def test_sheetpath_is_captured_and_normalised(tmp_path):
    p = tmp_path / "n.xml"
    p.write_text(make_netlist([{"ref": "R1", "sheet": "Power"}]), encoding="utf-8")
    assert parse_netlist(p)["R1"].sheetpath == "/Power/"


def test_empty_boolean_property_means_true(tmp_path):
    p = tmp_path / "n.xml"
    p.write_text(make_netlist([{"ref": "R1", "properties": {"dnp": ""}}]), encoding="utf-8")
    assert parse_netlist(p)["R1"].dnp


def test_empty_user_field_is_not_treated_as_a_boolean(tmp_path):
    """v12 matched any property whose name contained 'exclude' and 'board'."""
    p = tmp_path / "n.xml"
    p.write_text(make_netlist([{"ref": "R1", "properties": {"Excludes board rev": ""}}]), encoding="utf-8")
    comp = parse_netlist(p)["R1"]
    assert not comp.exclude_from_board
    assert comp.fields["Excludes board rev"] == ""


def test_custom_fields_are_all_available(tmp_path):
    p = tmp_path / "n.xml"
    p.write_text(
        make_netlist([{"ref": "R1", "properties": {"MB_Board": "Power", "MPN": "X1"}}]),
        encoding="utf-8",
    )
    comp = parse_netlist(p)["R1"]
    assert comp.fields["MB_Board"] == "Power"
    assert comp.fields["MPN"] == "X1"


def test_value_of_dnp_marks_the_component(tmp_path):
    p = tmp_path / "n.xml"
    p.write_text(make_netlist([{"ref": "R1", "value": "DNP"}]), encoding="utf-8")
    assert parse_netlist(p)["R1"].dnp


def test_power_symbols_are_excluded(tmp_path):
    p = tmp_path / "n.xml"
    p.write_text(make_netlist([{"ref": "#PWR01"}, {"ref": "R1"}]), encoding="utf-8")
    assert set(parse_netlist(p)) == {"R1"}


def test_missing_footprint_makes_a_component_unplaceable(tmp_path):
    p = tmp_path / "n.xml"
    p.write_text(make_netlist([{"ref": "R1", "footprint": None}]), encoding="utf-8")
    comp = parse_netlist(p)["R1"]
    assert not comp.placeable
    assert comp.skip_reason() == "No footprint assigned"


def test_top_level_sheets_are_deduplicated(tmp_path):
    p = tmp_path / "n.xml"
    p.write_text(
        make_netlist(
            [
                {"ref": "R1", "sheet": "/Power/"},
                {"ref": "R2", "sheet": "/Power/Regulators/"},
                {"ref": "R3", "sheet": "/IO/"},
            ]
        ),
        encoding="utf-8",
    )
    assert top_level_sheets(parse_netlist(p)) == ["IO", "Power"]


# =============================================================================
# Board scanning
# =============================================================================


@pytest.mark.parametrize("version", [20211014, 20221018, 20240108, 20241229, 20260206])
def test_scans_every_board_format_we_claim_to_support(tmp_path, version):
    p = tmp_path / "b.kicad_pcb"
    p.write_text(
        make_pcb([{"ref": "R1", "value": "10k", "fpid": "R:0402"}], version=version), encoding="utf-8"
    )
    scan = scan_pcb_file(p)
    assert scan.format_version == version
    assert scan.footprints[0].ref == "R1"


def test_legacy_fp_text_reference_is_read(tmp_path):
    p = tmp_path / "b.kicad_pcb"
    p.write_text(
        make_pcb([{"ref": "R1", "value": "10k", "legacy_text": True}], version=20211014),
        encoding="utf-8",
    )
    fp = scan_pcb_file(p).footprints[0]
    assert (fp.ref, fp.value) == ("R1", "10k")


def test_both_net_serialisations_are_read(tmp_path):
    """Format 20251028 stopped writing netcodes."""
    p = tmp_path / "b.kicad_pcb"
    p.write_text(make_pcb([{"ref": "R1", "pads": [("1", 7, "GND"), ("2", "VCC")]}]), encoding="utf-8")
    assert dict(scan_pcb_file(p).footprints[0].pad_nets) == {"1": "GND", "2": "VCC"}


def test_attributes_are_decoded(tmp_path):
    p = tmp_path / "b.kicad_pcb"
    p.write_text(
        make_pcb(
            [
                {"ref": "R1", "attrs": ["smd", "dnp", "exclude_from_bom"]},
                {"ref": "MB1", "attrs": ["board_only"]},
            ]
        ),
        encoding="utf-8",
    )
    fps = {f.ref: f for f in scan_pcb_file(p).footprints}
    assert fps["R1"].dnp and fps["R1"].exclude_from_bom
    assert fps["MB1"].board_only and not fps["MB1"].counts_for_ownership


def test_back_side_is_detected(tmp_path):
    p = tmp_path / "b.kicad_pcb"
    p.write_text(make_pcb([{"ref": "R1", "layer": "B.Cu"}]), encoding="utf-8")
    assert scan_pcb_file(p).footprints[0].side == "back"


def test_a_newer_format_warns_instead_of_answering_wrongly(tmp_path):
    p = tmp_path / "b.kicad_pcb"
    p.write_text(make_pcb([{"ref": "R1"}], version=20990101), encoding="utf-8")
    scan = scan_pcb_file(p)
    assert scan.footprints and any("newer" in w for w in scan.warnings)


def test_unreadable_file_reports_rather_than_raising(tmp_path):
    scan = scan_pcb_file(tmp_path / "missing.kicad_pcb")
    assert scan.footprints == [] and scan.warnings


def test_truncated_board_yields_what_it_has_and_warns(tmp_path):
    p = tmp_path / "b.kicad_pcb"
    p.write_text(make_pcb([{"ref": "R1"}, {"ref": "R2"}])[:-40], encoding="utf-8")
    scan = scan_pcb_file(p)
    assert scan.warnings
    assert any(f.ref == "R1" for f in scan.footprints)


def test_scan_survives_a_cache_roundtrip(tmp_path):
    from multiboard.core.pcb_scan import PcbScan

    p = tmp_path / "b.kicad_pcb"
    p.write_text(
        make_pcb([{"ref": "R1", "value": "10k", "x": 1.5, "pads": [("1", "GND")], "attrs": ["dnp"]}]),
        encoding="utf-8",
    )
    original = scan_pcb_file(p)
    restored = PcbScan.from_dict(p, original.to_dict())
    assert restored.footprints == original.footprints


def test_detects_the_v12_block_footprint_damage(tmp_path):
    """
    Every Block_*.kicad_mod v12 ever wrote carries two stray closing parens.
    Doctor uses this to offer a one-click regeneration.
    """
    lib = tmp_path / "MultiBoard_Blocks.pretty"
    lib.mkdir()
    (lib / "Block_Power.kicad_mod").write_text(
        '(footprint "Block_Power"\n'
        "  (fp_rect (start 0 0) (end 1 1)\n"
        '    (stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd"))\n'
        "  )\n)",
        encoding="utf-8",
    )
    (lib / "Block_IO.kicad_mod").write_text('(footprint "Block_IO" (layer "F.Cu"))', encoding="utf-8")

    problems = validate_footprint_library(lib)
    assert len(problems) == 1
    assert "Block_Power" in problems[0] and "stray" in problems[0]


# =============================================================================
# Colour
# =============================================================================


def test_contrast_ratio_endpoints():
    assert color.contrast_ratio((0, 0, 0), (255, 255, 255)) == pytest.approx(21.0, abs=0.01)
    assert color.contrast_ratio((128, 128, 128), (128, 128, 128)) == pytest.approx(1.0)


def test_tint_moves_toward_the_accent_from_the_actual_background():
    """The reason fixed pastels fail: they only work on one background."""
    light = color.tint((255, 255, 255), (255, 140, 0), 0.14)
    dark = color.tint((48, 48, 48), (255, 140, 0), 0.14)
    assert color.luminance(light) < color.luminance((255, 255, 255))
    assert color.luminance(dark) > color.luminance((48, 48, 48))


def test_ensure_contrast_lifts_an_unreadable_colour():
    fixed = color.ensure_contrast((60, 60, 60), (32, 32, 32))
    assert color.contrast_ratio(fixed, (32, 32, 32)) >= color.MIN_CONTRAST_AA


def test_board_colours_are_stable_across_processes():
    """Python's hash() is randomised per run; a board must keep its colour."""
    assert color.board_color("Power", dark_mode=False) == color.board_color("Power", dark_mode=False)


def test_board_colours_differ_between_boards():
    seen = {
        color.board_color(n, dark_mode=False, index=i) for i, n in enumerate(["Power", "IO", "Main", "RF"])
    }
    assert len(seen) == 4


def test_board_colours_are_readable_in_both_modes():
    for i, name in enumerate(["Power", "IO", "Main", "RF", "Sensor", "Motor"]):
        for dark, bg in ((False, (255, 255, 255)), (True, (48, 48, 48))):
            c = color.board_color(name, dark_mode=dark, index=i)
            assert color.contrast_ratio(c, bg) >= color.MIN_CONTRAST_AA_LARGE, (name, dark)


@pytest.mark.parametrize("text", ["#FF8800", "FF8800", "#F80"])
def test_hex_parsing(text):
    assert color.from_hex(text) == (255, 136, 0)


def test_malformed_hex_degrades_to_grey():
    assert color.from_hex("nonsense") == (128, 128, 128)


# =============================================================================
# Installation discovery
# =============================================================================


@pytest.mark.parametrize(
    "name,expected",
    [
        ("10.0", (10, 0)),
        ("9.0", (9, 0)),
        ("10.0.5", (10, 0, 5)),
        ("kicad-10.0", (10, 0)),
        ("bin", None),
        ("", None),
        ("nightly", None),
    ],
)
def test_version_directory_parsing(name, expected):
    assert kicad_env.parse_version_dir(name) == expected


def test_kicad_10_sorts_above_kicad_9():
    """
    v12 used sorted(reverse=True) on directory names, and "9.0" > "10.0"
    lexicographically -- so it drove KiCad 9's toolchain against KiCad 10 files.
    """
    versions = [kicad_env.parse_version_dir(n) for n in ("9.0", "10.0", "8.0")]
    assert max(v for v in versions if v) == (10, 0)


def test_child_env_strips_the_embedded_python_variables(monkeypatch):
    monkeypatch.setenv("PYTHONHOME", "/kicad/python")
    monkeypatch.setenv("PYTHONPATH", "/kicad/lib")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = kicad_env.child_env()
    assert "PYTHONHOME" not in env and "PYTHONPATH" not in env
    assert env["PATH"] == "/usr/bin"


def test_third_party_dir_honours_the_versioned_env_var(monkeypatch):
    monkeypatch.setenv("KICAD10_3RD_PARTY", "/custom/3rd")
    assert kicad_env.third_party_dir((10, 0)) == Path("/custom/3rd")


def test_config_dir_honours_kicad_config_home(monkeypatch):
    monkeypatch.setenv("KICAD_CONFIG_HOME", "/cfg")
    assert kicad_env.config_dir_for((10, 0)) == Path("/cfg/10.0")


def test_env_lib_dirs_reconstructs_missing_variables(monkeypatch):
    """
    v12 dropped any fp-lib-table URI still containing '${' after substituting
    KIPRJMOD, so libraries behind ${KICAD10_FOOTPRINT_DIR} silently vanished.
    """
    monkeypatch.delenv("KICAD10_FOOTPRINT_DIR", raising=False)
    monkeypatch.setenv("KICAD10_3RD_PARTY", "/tp")
    dirs = kicad_env.env_lib_dirs((10, 0))
    assert dirs["KICAD10_3RD_PARTY"] == Path("/tp")


def test_negative_discovery_is_cached(monkeypatch):
    calls = []

    def never_found():
        calls.append(1)
        return None

    monkeypatch.setattr(kicad_env, "_discover_uncached", never_found)
    kicad_env.invalidate()
    assert kicad_env.discover() is None
    assert kicad_env.discover() is None
    assert len(calls) == 1
    kicad_env.invalidate()


# =============================================================================
# Discovery robustness -- regressions found by running against a real machine
# =============================================================================


@pytest.mark.parametrize(
    "version,ok",
    [
        ((10, 0, 5), True),
        ((9, 0), True),
        ((4, 0), True),
        ((99, 1), True),
        ((3, 2), False),
        ((3, 2, 0), False),
        ((0, 1), False),
        (None, False),
        ((), False),
    ],
)
def test_only_plausible_kicad_versions_are_accepted(version, ok):
    """
    A Flatpak kicad-cli that cannot load prints
    'libwx_gtk3u_core-3.2.so.0: cannot open shared object file' to stderr.
    Without this guard that became "KiCad 3.2" and drove every later operation.
    """
    assert kicad_env.plausible_kicad_version(version) is ok


def _fake_run(stdout="", stderr="", returncode=0):
    import subprocess

    def run(*_a, **_k):
        return subprocess.CompletedProcess([], returncode, stdout, stderr)

    return run


def test_cli_version_ignores_stderr(monkeypatch):
    monkeypatch.setattr(
        kicad_env.subprocess,
        "run",
        _fake_run(
            stdout="",
            stderr="error while loading shared libraries: libwx_gtk3u_core-3.2.so.0",
            returncode=127,
        ),
    )
    assert kicad_env._cli_version(Path("/fake/kicad-cli")) is None


def test_cli_version_ignores_a_failed_run(monkeypatch):
    monkeypatch.setattr(kicad_env.subprocess, "run", _fake_run(stdout="10.0.5", returncode=1))
    assert kicad_env._cli_version(Path("/fake/kicad-cli")) is None


def test_cli_version_reads_a_successful_run(monkeypatch):
    monkeypatch.setattr(kicad_env.subprocess, "run", _fake_run(stdout="10.0.5\n", returncode=0))
    assert kicad_env._cli_version(Path("/fake/kicad-cli")) == (10, 0, 5)


def test_cli_version_rejects_an_implausible_stdout_version(monkeypatch):
    monkeypatch.setattr(kicad_env.subprocess, "run", _fake_run(stdout="wxWidgets 3.2.4", returncode=0))
    assert kicad_env._cli_version(Path("/fake/kicad-cli")) is None


def test_export_refuses_before_deleting_when_cli_is_missing(tmp_path):
    """
    A missing toolchain must not cost the user their existing netlist -- that
    would turn "kicad-cli not found" into "the component index is empty".
    """
    from multiboard.core.netlist import NetlistError, export_netlist, netlist_path

    sch = tmp_path / "demo.kicad_sch"
    sch.write_text("(kicad_sch)", encoding="utf-8")
    existing = netlist_path(tmp_path)
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text("<export/>", encoding="utf-8")

    with pytest.raises(NetlistError, match="kicad-cli"):
        export_netlist(None, tmp_path, sch)

    assert existing.exists(), "the previous netlist was destroyed"


# =============================================================================
# Bounded state
#
# "It grows a little on every keystroke" is how a plugin that feels fine in a
# demo becomes one that has to be restarted after an afternoon's work.
# =============================================================================


def test_compiled_rule_cache_is_bounded():
    """
    The rules editor previews matches as you type.

    So this is fed one pattern per keystroke -- every prefix of every regex
    anyone ever types. An unbounded dict kept all of them for the life of the
    process.
    """
    from multiboard.core.rules import REGEX_CACHE_SIZE, _compile

    _compile.cache_clear()
    for i in range(REGEX_CACHE_SIZE * 4):
        _compile(f"^R{i}$")

    assert _compile.cache_info().currsize <= REGEX_CACHE_SIZE


def test_invalid_regex_is_quarantined_not_raised():
    from multiboard.core.rules import _compile

    assert _compile("(unclosed") is None
    assert _compile(r"^R\d+$") is not None


def test_lock_scan_ignores_deleted_boards(tmp_path):
    """
    A lock inside a trashed board is not a lock on anything.

    It was also an unbounded walk: Doctor runs on every index refresh, and
    rglob descended through every board the project had ever deleted.
    """
    from multiboard.core.project import stale_locks

    live = tmp_path / "boards" / "Power"
    live.mkdir(parents=True)
    (live / "~Power.kicad_pcb.lck").write_text("", encoding="utf-8")

    dead = tmp_path / "boards" / ".trash" / "Old-20260101-0000"
    dead.mkdir(parents=True)
    (dead / "~Old.kicad_pcb.lck").write_text("", encoding="utf-8")

    found = [p.name for p in stale_locks(tmp_path)]
    assert found == ["~Power.kicad_pcb.lck"]


def test_trash_check_survives_an_unreadable_file(tmp_path, monkeypatch):
    """One bad stat must not take the whole Doctor report down."""
    from multiboard.core import doctor

    trashed = tmp_path / "boards" / ".trash" / "Old-20260101-0000"
    trashed.mkdir(parents=True)
    (trashed / "a.bin").write_bytes(b"x" * 10)

    real_stat = Path.stat
    monkeypatch.setattr(
        Path,
        "stat",
        lambda self, *a, **k: (
            (_ for _ in ()).throw(OSError("gone")) if self.name == "a.bin" else real_stat(self, *a, **k)
        ),
    )

    check = doctor._check_trash(tmp_path)
    assert check.level == doctor.INFO
    assert "1 deleted board" in check.title
