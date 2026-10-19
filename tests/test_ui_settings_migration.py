"""
The upgrade and first-run paths spec'd in TAB_CONSOLIDATION_SPEC.md §6 and §4.

Each test points ``COUNCIL_UI_SETTINGS`` at its own tmp_path so the real
``~/.council/ui_settings.json`` is never touched; this is the one piece of
state ``modes.advanced()`` reads under its new profile layering, and letting
a test suite migrate the user's actual file would be a surprise worth
avoiding.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import modes                              # noqa: E402
from council_qt import ui_settings                          # noqa: E402


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    path = tmp_path / "ui_settings.json"
    monkeypatch.setenv(ui_settings.ENV_VAR, str(path))
    monkeypatch.delenv("COUNCIL_ADVANCED", raising=False)
    return path


def test_a_truly_fresh_install_lands_on_full_with_no_notice(isolated_settings):
    section, notice = ui_settings.load_or_migrate()
    assert section["profile"] == "full"
    assert notice is None
    # Written back to disk so the next launch does not migrate again.
    on_disk = json.loads(isolated_settings.read_text(encoding="utf-8"))
    assert on_disk["ui"]["profile"] == "full"


def test_a_legacy_advanced_true_flag_maps_to_developer_with_a_notice(
        isolated_settings):
    isolated_settings.write_text(json.dumps({"advanced_mode": True}),
                                 encoding="utf-8")
    section, notice = ui_settings.load_or_migrate()
    assert section["profile"] == "developer"
    assert notice and "Profile menu" in notice
    on_disk = json.loads(isolated_settings.read_text(encoding="utf-8"))
    assert on_disk["ui"]["profile"] == "developer"
    # The retired flag stays on disk for one release so a downgraded build
    # still finds its advanced-mode preference.
    assert on_disk["advanced_mode"] is True


def test_a_legacy_advanced_false_flag_maps_to_demo_with_a_notice(
        isolated_settings):
    isolated_settings.write_text(json.dumps({"advanced_mode": False}),
                                 encoding="utf-8")
    section, notice = ui_settings.load_or_migrate()
    assert section["profile"] == "demo"
    assert notice is not None


def test_the_notice_fires_once_and_is_gone_on_the_next_launch(
        isolated_settings):
    isolated_settings.write_text(json.dumps({"advanced_mode": True}),
                                 encoding="utf-8")
    first, first_notice = ui_settings.load_or_migrate()
    second, second_notice = ui_settings.load_or_migrate()
    assert first_notice is not None
    assert second_notice is None
    assert second["profile"] == first["profile"]


# ------------------------------------------------------- advanced-mode layering

def test_demo_profile_reads_as_not_advanced(isolated_settings):
    ui_settings.save_ui({"profile": "demo"})
    assert modes.advanced([]) is False


def test_developer_and_full_profiles_turn_advanced_on(isolated_settings):
    ui_settings.save_ui({"profile": "developer"})
    assert modes.advanced([]) is True
    ui_settings.save_ui({"profile": "full"})
    assert modes.advanced([]) is True


def test_a_custom_profile_carries_its_own_advanced_flag(isolated_settings):
    ui_settings.save_ui({
        "profile": "custom",
        "custom": {"base_profile": "demo", "advanced_mode": True,
                   "visible_tabs": []},
    })
    assert modes.advanced([]) is True
    ui_settings.save_ui({
        "profile": "custom",
        "custom": {"base_profile": "developer", "advanced_mode": False,
                   "visible_tabs": []},
    })
    assert modes.advanced([]) is False


def test_the_environment_override_still_wins_over_the_profile(
        isolated_settings, monkeypatch):
    ui_settings.save_ui({"profile": "demo"})
    monkeypatch.setenv("COUNCIL_ADVANCED", "1")
    assert modes.advanced([]) is True


def test_the_cli_flag_still_wins_over_the_profile(isolated_settings):
    ui_settings.save_ui({"profile": "demo"})
    assert modes.advanced(["app.py", "--advanced"]) is True


def test_a_bad_settings_file_falls_back_to_not_advanced(isolated_settings):
    isolated_settings.write_text("{not json", encoding="utf-8")
    assert modes.advanced([]) is False


def test_a_missing_settings_file_falls_back_to_not_advanced(isolated_settings):
    # The file is missing until a launch creates it; modes.advanced() reads
    # the live state and must not migrate on its own.
    assert not isolated_settings.exists()
    assert modes.advanced([]) is False
    assert not isolated_settings.exists()
