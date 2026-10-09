"""
specialists.SpecialistRegistry keeps a specialists.json it cannot read.

THE DEFECT (found in review, older than batch 0)
A vault whose specialists.json did not parse — '{"specs": []}', a hand edit
with a slip in it — was REPLACED with the three seeded defaults the moment the
registry loaded: it printed "Failed to parse", fell through to the seeding
path, and save() wrote over the user's file. The Qt Council tab loads the
registry while it is built (refresh_specialists), and Tk's console does at
startup, so every launch did it.

Now the defaults are used in memory and the file is left alone; the first
real save (the user adds or removes a specialist) moves the unreadable file
aside — specialists.json.unreadable — before writing, so its text survives.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import specialists  # noqa: E402

BROKEN = '{"specs": []}'


@pytest.fixture
def vault(tmp_path):
    folder = tmp_path / "vault"
    folder.mkdir()
    (folder / "specialists.json").write_text(BROKEN, encoding="utf-8")
    return folder


def test_loading_an_unreadable_file_does_not_overwrite_it(vault):
    registry = specialists.SpecialistRegistry(vault)
    assert (vault / "specialists.json").read_text(encoding="utf-8") == BROKEN
    defaults = [s.id for s in specialists.default_specialists()]
    assert [s.id for s in registry.all()] == defaults, (
        "the app should still have the defaults to work with")
    assert registry.unreadable, "the registry does not say the file was bad"


def test_a_save_keeps_the_unreadable_file_aside_first(vault):
    registry = specialists.SpecialistRegistry(vault)
    registry.add(specialists.Specialist(id="ops", name="Ops"))
    kept = vault / "specialists.json.unreadable"
    assert kept.read_text(encoding="utf-8") == BROKEN
    saved = json.loads((vault / "specialists.json").read_text(encoding="utf-8"))
    assert "ops" in [d["id"] for d in saved]
    assert registry.unreadable is None


def test_an_earlier_set_aside_copy_is_not_overwritten(vault):
    (vault / "specialists.json.unreadable").write_text("OLDER",
                                                       encoding="utf-8")
    registry = specialists.SpecialistRegistry(vault)
    registry.add(specialists.Specialist(id="ops", name="Ops"))
    assert (vault / "specialists.json.unreadable").read_text(
        encoding="utf-8") == "OLDER"
    assert (vault / "specialists.json.unreadable1").read_text(
        encoding="utf-8") == BROKEN


def test_a_missing_file_is_still_seeded(tmp_path):
    registry = specialists.SpecialistRegistry(tmp_path)
    assert (tmp_path / "specialists.json").is_file()
    assert registry.unreadable is None
    assert registry.all()


def test_the_council_tab_build_leaves_the_file_alone(vault):
    """The trigger the review measured: one Qt launch, and the file held the
    three defaults. Building the Council tab is the step that loads it."""
    pytest.importorskip("PySide6")
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")
    from PySide6.QtWidgets import QApplication

    from council_qt.tabs.council import CouncilActions, CouncilTab

    app = QApplication.instance() or QApplication([])
    tab = CouncilTab(actions=CouncilActions(vault_dir=vault))
    try:
        assert (vault / "specialists.json").read_text(
            encoding="utf-8") == BROKEN
    finally:
        tab.deleteLater()
        app.processEvents()
