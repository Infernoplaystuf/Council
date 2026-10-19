"""
Smoke tests for the grouped-tab layout — the per-profile top-level tab bar,
the "Show tabs" and "Show more" affordances, the Custom-flip, the auto-hide,
and the lazy build of inner tabs.

``council_qt.profiles`` is written by the Council dogfood pipeline in a
separate PR. Until it lands, this test file installs a lightweight stand-in
into ``sys.modules`` for the test session only (see ``_install_fake_profiles``)
so the controller has something to read; swapping Council's module in and the
tests should pass unchanged.
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="the grouped tab layout needs PySide6")

from PySide6.QtWidgets import QApplication, QLabel, QWidget   # noqa: E402

# The six groups from TAB_CONSOLIDATION_SPEC.md §1. Kept here as the
# contract this file tests; the Council PR populates these in the real
# module with the same shape.
_GROUPS = {
    "Council": ["council", "sessions", "speech", "docs", "changelog"],
    "Knowledge": ["vault", "vault_health", "ledger", "connections",
                  "lens", "librarian"],
    "Agents & Models": ["agents", "agent_creator", "specialists", "models",
                        "nodes", "apothecary"],
    "Build": ["code", "ide", "fanout", "forge", "jobs", "var_graph"],
    "Create": ["designer", "grapher", "capture", "dream3d"],
    "Lab": ["diagnostics", "council_map", "quant_duel"],
}
_GROUP_ORDER = ["Council", "Knowledge", "Agents & Models",
                "Build", "Create", "Lab"]
_DEMO = {"council", "vault", "ledger", "connections"}
_DEVELOPER = {
    "council", "sessions", "docs", "vault", "vault_health", "ledger",
    "connections", "lens", "librarian", "models", "agents", "specialists",
    "nodes", "code", "ide", "forge", "jobs", "grapher", "diagnostics",
}
_FULL = set().union(*_GROUPS.values())


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def profiles_module(monkeypatch):
    """Install the stand-in profiles module for the test; remove on teardown.

    The real module may or may not be installed in the venv the test runs
    in; this swap is per-test so a real one that lands later does not get
    masked at import time.
    """
    mod = types.ModuleType("council_qt.profiles")
    mod.GROUPS = _GROUPS
    mod.GROUP_ORDER = _GROUP_ORDER
    mod.DEMO = _DEMO
    mod.DEVELOPER = _DEVELOPER
    mod.FULL = _FULL
    monkeypatch.setitem(sys.modules, "council_qt.profiles", mod)
    yield mod


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    from council_qt import ui_settings
    path = tmp_path / "ui_settings.json"
    monkeypatch.setenv(ui_settings.ENV_VAR, str(path))
    return path


def _build_window(profiles_module):
    """A CouncilWindow wired with the grouped controller and all 30 tabs."""
    from council_qt import group_tabs
    from council_qt.window import CouncilWindow

    window = CouncilWindow()
    controller = group_tabs.GroupedTabsController(window, profiles_module)
    window.install_grouped_layout(controller)

    for group in profiles_module.GROUP_ORDER:
        for tab_id in profiles_module.GROUPS[group]:
            def factory(tid=tab_id):
                w = QLabel(f"content of {tid}")
                w.setProperty("tab_id", tid)
                return w
            factory.__name__ = f"build_{tab_id}"
            window.add_tab(tab_id.title(), factory, tab_id=tab_id)
    return window, controller


def _visible_group_titles(window):
    bar = window.tabs.tabBar()
    return [window.tabs.tabText(i) for i in range(window.tabs.count())
            if bar.isTabVisible(i)]


def _visible_inner_ids(controller, group_name):
    page = controller.groups[group_name]
    bar = page.inner.tabBar()
    return [tid for i, tid in enumerate(page.inner_tab_ids())
            if bar.isTabVisible(i)]


# ------------------------------------------------------------ the layout

def test_the_six_groups_appear_in_the_spec_order(
        qapp, profiles_module, isolated_settings):
    window, controller = _build_window(profiles_module)
    try:
        controller.apply_profile_state({"profile": "full"})
        assert _visible_group_titles(window) == _GROUP_ORDER
    finally:
        window.request_close()


def test_demo_leaves_only_council_and_knowledge_at_the_top(
        qapp, profiles_module, isolated_settings):
    window, controller = _build_window(profiles_module)
    try:
        controller.apply_profile_state({"profile": "demo"})
        assert _visible_group_titles(window) == ["Council", "Knowledge"]
        assert _visible_inner_ids(controller, "Council") == ["council"]
        assert _visible_inner_ids(controller, "Knowledge") == [
            "vault", "ledger", "connections"]
    finally:
        window.request_close()


def test_developer_shows_the_profile_tabs_in_group_order(
        qapp, profiles_module, isolated_settings):
    window, controller = _build_window(profiles_module)
    try:
        controller.apply_profile_state({"profile": "developer"})
        # Build is in the Developer profile; Create and Lab also have
        # one tab each (grapher, diagnostics).
        assert _visible_group_titles(window) == _GROUP_ORDER
        assert _visible_inner_ids(controller, "Create") == ["grapher"]
        assert _visible_inner_ids(controller, "Lab") == ["diagnostics"]
    finally:
        window.request_close()


# ----------------------------------------------------------- the toggles

def test_toggling_a_tab_on_a_preset_flips_the_profile_to_custom(
        qapp, profiles_module, isolated_settings):
    window, controller = _build_window(profiles_module)
    try:
        controller.apply_profile_state({"profile": "developer"})
        assert controller.profile_name == "developer"
        controller._on_toggle("ide", False)                 # noqa: SLF001
        assert controller.profile_name == "custom"
        assert controller.custom_base == "developer"
        assert "ide" not in controller.visible_ids
    finally:
        window.request_close()


def test_show_more_lists_every_hidden_tab_and_restores_one(
        qapp, profiles_module, isolated_settings):
    window, controller = _build_window(profiles_module)
    try:
        controller.apply_profile_state({"profile": "demo"})
        hidden = [tid for tid in _FULL if tid not in controller.visible_ids]
        assert hidden
        # The whole Agents & Models group is hidden under Demo, so the
        # Show-more menu has a header for it.
        controller._apply()                                  # noqa: SLF001
        action_texts = [a.text() for a in
                        controller._show_more_menu.actions()  # noqa: SLF001
                        if a.text()]
        # One of its inner tabs must show up in the menu.
        assert any("Agents" in t for t in action_texts)
        controller._on_toggle("models", True)                # noqa: SLF001
        assert "models" in controller.visible_ids
        assert "Agents & Models" in _visible_group_titles(window)
    finally:
        window.request_close()


def test_hiding_every_tab_of_a_group_auto_hides_the_group_tab(
        qapp, profiles_module, isolated_settings):
    window, controller = _build_window(profiles_module)
    try:
        controller.apply_profile_state({"profile": "full"})
        for tab_id in _GROUPS["Create"]:
            controller._on_toggle(tab_id, False)              # noqa: SLF001
        assert "Create" not in _visible_group_titles(window)
        # Showing one restores it.
        controller._on_toggle("grapher", True)                # noqa: SLF001
        assert "Create" in _visible_group_titles(window)
    finally:
        window.request_close()


# ----------------------------------------------------- the profile menu

def test_switching_the_profile_via_the_menu_changes_the_visible_set(
        qapp, profiles_module, isolated_settings):
    window, controller = _build_window(profiles_module)
    try:
        controller.apply_profile_state({"profile": "full"})
        controller._switch_profile("demo")                    # noqa: SLF001
        assert controller.profile_name == "demo"
        assert _visible_group_titles(window) == ["Council", "Knowledge"]
    finally:
        window.request_close()


def test_resetting_a_custom_profile_snaps_back_to_its_base(
        qapp, profiles_module, isolated_settings):
    window, controller = _build_window(profiles_module)
    try:
        controller.apply_profile_state({"profile": "developer"})
        controller._on_toggle("code", False)                  # noqa: SLF001
        assert controller.profile_name == "custom"
        controller._reset_custom_to_base()                    # noqa: SLF001
        assert controller.profile_name == "developer"
        assert "code" in controller.visible_ids
    finally:
        window.request_close()


def test_the_profile_label_updates_when_the_profile_changes(
        qapp, profiles_module, isolated_settings):
    window, controller = _build_window(profiles_module)
    try:
        controller.apply_profile_state({"profile": "demo"})
        assert "Demo" in controller.profile_button.text()
        controller._switch_profile("developer")               # noqa: SLF001
        assert "Developer" in controller.profile_button.text()
    finally:
        window.request_close()


# ------------------------------------------------------------- lazy build

def test_a_registered_but_hidden_tab_pays_no_build_cost(
        qapp, profiles_module, isolated_settings):
    """The Tk-era contract: a tab registers with a factory, and that factory
    runs on first show. Hiding the tab via Demo must not run it at all."""
    from council_qt import group_tabs
    from council_qt.window import CouncilWindow

    window = CouncilWindow()
    controller = group_tabs.GroupedTabsController(window, profiles_module)
    window.install_grouped_layout(controller)

    built: list = []

    def factory(tid):
        def _f():
            built.append(tid)
            return QLabel(f"{tid}")
        _f.__name__ = f"build_{tid}"
        return _f

    for group in profiles_module.GROUP_ORDER:
        for tab_id in profiles_module.GROUPS[group]:
            window.add_tab(tab_id.title(), factory(tab_id), tab_id=tab_id)

    controller.apply_profile_state({"profile": "demo"})
    # Pumping once lets lazy-build run for the current visible inner tab.
    qapp.processEvents()
    # The Council group's first visible inner tab (council) builds; nothing
    # else in a Demo profile does yet.
    assert "council" in built
    assert "ide" not in built
    assert "apothecary" not in built
    window.request_close()


# ---------------------------------------------- integration with the window

def test_the_show_tab_helper_navigates_through_the_group(
        qapp, profiles_module, isolated_settings):
    window, controller = _build_window(profiles_module)
    try:
        controller.apply_profile_state({"profile": "full"})
        window.show_tab("Grapher")
        current = window.tabs.widget(window.tabs.currentIndex())
        assert current is controller.groups["Create"]
        inner = controller.groups["Create"].inner
        assert inner.tabText(inner.currentIndex()) == "Grapher"
    finally:
        window.request_close()


def test_has_tab_sees_both_grouped_and_flat_tabs(
        qapp, profiles_module, isolated_settings):
    window, controller = _build_window(profiles_module)
    try:
        controller.apply_profile_state({"profile": "full"})
        assert window.has_tab("Vault")
        window.add_tab("Flat", QWidget)
        assert window.has_tab("Flat")
        assert not window.has_tab("No Such Tab")
    finally:
        window.request_close()


# ----------------------------------------------------------- missing profile

def test_a_missing_profiles_module_leaves_the_flat_layout_intact(
        qapp, isolated_settings, monkeypatch):
    """When council_qt.profiles has not been written yet, add_tab must still
    work and the shell must open — the Council dogfood PR has not landed."""
    from council_qt import group_tabs

    # Not raising on a missing module is the whole point of the fallback.
    monkeypatch.setitem(sys.modules, "council_qt.profiles", None)
    # Setting a key to None makes a subsequent import raise ImportError,
    # which is what try_import_profiles handles.
    assert group_tabs.try_import_profiles() is None

    from council_qt.window import CouncilWindow
    window = CouncilWindow()
    try:
        window.add_tab("Only", QWidget, tab_id="only")
        assert window.has_tab("Only")
    finally:
        window.request_close()
