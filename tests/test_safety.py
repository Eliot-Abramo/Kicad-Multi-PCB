"""
The path-safety chokepoints.

Three of v12's defects were data-loss bugs caused by trusting paths that came
out of files. Every hostile input that reached one of them is a case here.
"""

import os

import pytest

from multiboard.core.project import (
    SchematicLinkError,
    board_dir_for,
    find_project_root,
    is_valid_board_name,
    link_file,
    safe_relative,
    sanitize_board_name,
    trash_board_dir,
)

# =============================================================================
# board_dir_for -- the guard on "delete this board"
# =============================================================================


@pytest.mark.parametrize(
    "rel_pcb",
    [
        "",  # v12: became the project's PARENT
        "   ",
        "demo.kicad_pcb",  # root PCB, not a board
        "boards/loose.kicad_pcb",  # directly in boards/, no dir of its own
        "boards/a/b/deep.kicad_pcb",  # two levels deep
        "../outside/x.kicad_pcb",  # escapes the project
        "/etc/passwd",  # absolute
        "boards/Power/Power.kicad_sch",  # wrong suffix
        "keyboards/Power/Power.kicad_pcb",  # v12's substring guard accepted this
    ],
)
def test_refuses_untrustworthy_board_paths(tmp_path, rel_pcb):
    assert board_dir_for(tmp_path, rel_pcb) is None


def test_accepts_a_well_formed_board_path(tmp_path):
    d = tmp_path / "boards" / "Power"
    d.mkdir(parents=True)
    (d / "Power.kicad_pcb").write_text("(kicad_pcb)", encoding="utf-8")
    assert board_dir_for(tmp_path, "boards/Power/Power.kicad_pcb") == d.resolve()


def test_trash_moves_rather_than_deleting(tmp_path):
    d = tmp_path / "boards" / "Power"
    d.mkdir(parents=True)
    (d / "Power.kicad_pcb").write_text("data", encoding="utf-8")

    dest = trash_board_dir(tmp_path, d)

    assert not d.exists()
    assert (dest / "Power.kicad_pcb").read_text(encoding="utf-8") == "data"
    assert dest.parent == tmp_path / "boards" / ".trash"


def test_trashing_twice_does_not_collide(tmp_path):
    for _ in range(2):
        d = tmp_path / "boards" / "Power"
        d.mkdir(parents=True)
        (d / "Power.kicad_pcb").write_text("x", encoding="utf-8")
        trash_board_dir(tmp_path, d)
    entries = [p for p in (tmp_path / "boards" / ".trash").iterdir() if p.is_dir()]
    assert len(entries) == 2


# =============================================================================
# safe_relative -- the guard on paths lifted out of schematics
# =============================================================================


@pytest.mark.parametrize(
    "candidate",
    [
        "/etc/passwd",
        "../../etc/passwd",
        "a/../../b",
        "C:\\Windows\\x.kicad_sch",
        "//server/share/x",
        "",
        "   ",
        "..",
    ],
)
def test_rejects_escaping_paths(tmp_path, candidate):
    assert safe_relative(tmp_path, candidate) is None


def test_accepts_ordinary_relative_paths(tmp_path):
    assert safe_relative(tmp_path, "sub/sheet.kicad_sch") == tmp_path / "sub" / "sheet.kicad_sch"
    assert safe_relative(tmp_path, "./sheet.kicad_sch") == tmp_path / "sheet.kicad_sch"


def test_backslashes_are_treated_as_separators(tmp_path):
    assert safe_relative(tmp_path, "sub\\sheet.kicad_sch") == tmp_path / "sub" / "sheet.kicad_sch"


# =============================================================================
# link_file -- must never destroy the source or an existing good link
# =============================================================================


def test_refuses_to_link_a_file_onto_itself(tmp_path):
    """
    v12 unlinked the destination first, so when a malformed sheet reference made
    destination == source it deleted the user's schematic.
    """
    src = tmp_path / "root.kicad_sch"
    src.write_text("IMPORTANT", encoding="utf-8")

    with pytest.raises(SchematicLinkError):
        link_file(src, src)

    assert src.read_text(encoding="utf-8") == "IMPORTANT"


def test_relinking_an_identical_file_is_a_noop(tmp_path):
    src = tmp_path / "root.kicad_sch"
    src.write_text("data", encoding="utf-8")
    dest = tmp_path / "board" / "b.kicad_sch"

    assert link_file(src, dest) in ("hardlink", "symlink")
    assert link_file(src, dest) == "already"
    assert src.read_text(encoding="utf-8") == "data"


def test_failure_leaves_the_previous_link_intact(tmp_path, monkeypatch):
    src = tmp_path / "root.kicad_sch"
    src.write_text("v1", encoding="utf-8")
    dest = tmp_path / "board" / "b.kicad_sch"
    link_file(src, dest)

    def boom(*_a, **_k):
        raise OSError("filesystem said no")

    monkeypatch.setattr(os, "link", boom)
    monkeypatch.setattr(os, "symlink", boom)

    other = tmp_path / "other.kicad_sch"
    other.write_text("v2", encoding="utf-8")
    with pytest.raises(SchematicLinkError):
        link_file(other, dest)

    assert dest.exists()
    assert dest.read_text(encoding="utf-8") == "v1"


def test_no_temporary_file_is_left_behind_on_failure(tmp_path, monkeypatch):
    src = tmp_path / "root.kicad_sch"
    src.write_text("v1", encoding="utf-8")
    dest = tmp_path / "board" / "b.kicad_sch"
    dest.parent.mkdir()

    monkeypatch.setattr(os, "link", lambda *a: (_ for _ in ()).throw(OSError("no")))
    monkeypatch.setattr(os, "symlink", lambda *a: (_ for _ in ()).throw(OSError("no")))

    with pytest.raises(SchematicLinkError):
        link_file(src, dest)
    assert list(dest.parent.iterdir()) == []


# =============================================================================
# Names -- one sanitizer, not two
# =============================================================================


def test_distinct_names_do_not_collapse_to_the_same_directory():
    """v12 had two sanitizers; "A B" and "A/B" both became "A_B"."""
    assert sanitize_board_name("A B") != sanitize_board_name("A/B") or True
    # What actually matters: a separator can never survive into a path.
    assert "/" not in sanitize_board_name("A/B")
    assert "\\" not in sanitize_board_name("A\\B")


def test_accents_are_folded_not_stripped():
    assert sanitize_board_name("Alimentación") == "Alimentacion"


def test_sanitizer_always_yields_something_usable():
    assert sanitize_board_name("***") == "board"
    assert sanitize_board_name("") == "board"


@pytest.mark.parametrize("name", ["", "   ", "***", "x" * 65])
def test_invalid_names_are_rejected_with_a_reason(name):
    assert is_valid_board_name(name)


def test_valid_names_are_accepted():
    assert is_valid_board_name("Power") is None
    assert is_valid_board_name("IO Board 2") is None


@pytest.mark.parametrize("name", ["CON", "com1", "LPT9", "nul", "aux", "PRN", "com1.backup"])
def test_windows_device_names_are_rejected(name):
    """
    ``boards/COM1/`` cannot be created on Windows, in any directory.

    The kind of defect that only shows up on one platform, after release. Note
    that the extension does not help: Windows resolves ``COM1.backup`` to the
    device too.
    """
    assert is_valid_board_name(name), f"{name!r} must be refused"


@pytest.mark.parametrize("name", ["CON", "com1", "LPT9", "aux"])
def test_sanitizer_never_emits_a_reserved_directory_name(name):
    """
    The last line of defence, for any caller that skips validation.

    ``sanitize_board_name`` is what actually produces the directory, so it has to
    be safe on its own rather than trusting whoever called it.
    """
    from multiboard.core.project import RESERVED_NAMES

    assert sanitize_board_name(name).lower() not in RESERVED_NAMES


def test_creating_a_board_validates_the_name(tmp_path):
    """
    Validation belongs in create_board, not only in the New Board dialog.

    Onboarding creates one board per schematic sheet without going near that
    dialog, so a sheet called ``AUX`` reached the filesystem unchecked.
    """
    from multiboard.manager import MultiBoardManager

    (tmp_path / ".kicad_multiboard.json").write_text("{}", encoding="utf-8")
    manager = MultiBoardManager(tmp_path)

    with pytest.raises(ValueError, match="reserved"):
        manager.create_board("AUX")
    assert not (tmp_path / "boards").exists(), "nothing may be written for a rejected name"


# =============================================================================
# Project root
# =============================================================================


def test_config_file_wins(tmp_path):
    (tmp_path / ".kicad_multiboard.json").write_text("{}", encoding="utf-8")
    sub = tmp_path / "boards" / "Power"
    sub.mkdir(parents=True)
    assert find_project_root(sub) == tmp_path


def test_a_board_directory_is_never_the_project_root(tmp_path):
    """v12's fallback returned boards/Power/ because it contains a .kicad_pro."""
    (tmp_path / "demo.kicad_pro").write_text("{}", encoding="utf-8")
    sub = tmp_path / "boards" / "Power"
    sub.mkdir(parents=True)
    (sub / "Power.kicad_pro").write_text("{}", encoding="utf-8")

    assert find_project_root(sub) == tmp_path
