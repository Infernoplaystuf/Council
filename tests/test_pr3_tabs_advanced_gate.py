"""
Regressions for the HIGH / MEDIUM audit findings on PR #3 (grouped tabs +
profiles), 2026-10-19:

HIGH 1 — a flat-fallback launch (no ``council_qt.profiles`` yet) must not
         migrate the settings file to ``{"ui":{"profile":"full"}}``.
         That migration was turning the six advanced tabs on for every user
         who had never opted in, by way of ``modes.advanced()`` reading the
         just-written "full" profile and the Tk-era env/CLI gate being
         bypassed.
HIGH 2 — ``_preset_ids`` must survive the Council dogfood profiles contract
         (frozen ``Profile`` dataclass with ``visible_tabs: frozenset``), and
         ``council_qt._register_tabs`` must fall back to the flat layout when
         the grouped-controller setup raises.
MEDIUM a — ``_maybe_build_inner`` must not cascade into every inner tab, and
         must not un-hide a profile-hidden tab it swaps in.
MEDIUM b — ``_switch_profile`` to Custom from a preset must carry the
         previous preset over to ``custom_base`` / ``custom_advanced``.
MEDIUM c — With the grouped controller installed, advanced tabs register
         regardless of ``modes.advanced()`` so a runtime Demo→Developer flip
         can show them (the profile filter hides them under Demo).

Qt tests are offscreen and share one QApplication, matching the style of
``tests/test_group_tabs.py``.
"""
from __future__ import annotations

import json
import os
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6", reason="PR #3 lives on PySide6")

from PySide6.QtWidgets import QApplication, QLabel, QWidget  # noqa: E402


def _load_entry():
    """``council_qt.py`` as a module (the file sits next to the package of
    the same name; ``import council_qt`` gives the package)."""
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "council_qt.py"
    spec = importlib.util.spec_from_file_location("council_qt_entry", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------------ shared fixtures

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
def set_only_profiles(monkeypatch):
    """The pre-dogfood shape: DEMO/DEVELOPER/FULL as plain sets."""
    mod = types.ModuleType("council_qt.profiles")
    mod.GROUPS = _GROUPS
    mod.GROUP_ORDER = _GROUP_ORDER
    mod.DEMO = _DEMO
    mod.DEVELOPER = _DEVELOPER
    mod.FULL = _FULL
    monkeypatch.setitem(sys.modules, "council_qt.profiles", mod)
    yield mod


@pytest.fixture
def dataclass_profiles(monkeypatch):
    """The dogfood-PR shape: a frozen Profile dataclass with visible_tabs."""

    @dataclass(frozen=True)
    class Profile:
        name: str
        visible_tabs: frozenset = field(default_factory=frozenset)

    mod = types.ModuleType("council_qt.profiles")
    mod.GROUPS = _GROUPS
    mod.GROUP_ORDER = _GROUP_ORDER
    mod.Profile = Profile
    mod.DEMO = Profile("demo", frozenset(_DEMO))
    mod.DEVELOPER = Profile("developer", frozenset(_DEVELOPER))
    mod.FULL = Profile("full", frozenset(_FULL))
    monkeypatch.setitem(sys.modules, "council_qt.profiles", mod)
    yield mod


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    from council_qt import ui_settings
    path = tmp_path / "ui_settings.json"
    monkeypatch.setenv(ui_settings.ENV_VAR, str(path))
    monkeypatch.delenv("COUNCIL_ADVANCED", raising=False)
    return path


# ============================================================= HIGH 1

def test_flat_fallback_launch_does_not_migrate_settings_file(
        qapp, isolated_settings, monkeypatch):
    """A launch with no ``council_qt.profiles`` module must leave the
    settings file alone — otherwise the just-written "full" profile turns
    the six advanced tabs on for every user who never opted in."""
    # No profiles module → flat fallback.
    monkeypatch.setitem(sys.modules, "council_qt.profiles", None)

    # Fresh install: no settings file on disk.
    assert not isolated_settings.exists()

    entry = _load_entry()
    from council_qt.window import CouncilWindow

    window = CouncilWindow()
    try:
        import council_qt.tabs as _tabs
        monkeypatch.setattr(_tabs, "registry",
                            lambda advanced=False: [], raising=True)

        entry._register_tabs(window)                             # noqa: SLF001

        # The HIGH 1 claim: the fallback launch must not create the file
        # (or must create one that does NOT promote the user to "full").
        if isolated_settings.exists():
            on_disk = json.loads(isolated_settings.read_text(encoding="utf-8"))
            assert on_disk.get("ui", {}).get("profile") != "full", (
                "flat fallback promoted the user to the Full profile, "
                "turning six advanced tabs on without consent")
    finally:
        window.request_close()


def test_flat_fallback_without_env_or_cli_hides_advanced_tabs(
        qapp, isolated_settings, monkeypatch):
    """In the flat fallback the Tk-era gate must be the ONLY source: no env,
    no --advanced, no advanced tabs. The audit found the six advanced tabs
    were being registered for everyone."""
    monkeypatch.setitem(sys.modules, "council_qt.profiles", None)
    monkeypatch.delenv("COUNCIL_ADVANCED", raising=False)
    monkeypatch.setattr(sys, "argv", ["council_qt.py"])

    entry = _load_entry()
    import council_qt.tabs as _tabs
    from council_qt.window import CouncilWindow

    recorded_advanced: list = []

    def fake_registry(advanced=False):
        recorded_advanced.append(advanced)
        return []

    monkeypatch.setattr(_tabs, "registry", fake_registry, raising=True)

    window = CouncilWindow()
    try:
        entry._register_tabs(window)                             # noqa: SLF001
        assert recorded_advanced, "registry() was not called"
        assert recorded_advanced[-1] is False, (
            "flat fallback must hide the six advanced tabs when no env or "
            "CLI override is set")
    finally:
        window.request_close()


def test_flat_fallback_with_env_override_shows_advanced_tabs(
        qapp, isolated_settings, monkeypatch):
    """The env override is the opt-in path the Tk shell has always had."""
    monkeypatch.setitem(sys.modules, "council_qt.profiles", None)
    monkeypatch.setenv("COUNCIL_ADVANCED", "1")
    monkeypatch.setattr(sys, "argv", ["council_qt.py"])

    entry = _load_entry()
    import council_qt.tabs as _tabs
    from council_qt.window import CouncilWindow

    recorded_advanced: list = []

    def fake_registry(advanced=False):
        recorded_advanced.append(advanced)
        return []

    monkeypatch.setattr(_tabs, "registry", fake_registry, raising=True)
    window = CouncilWindow()
    try:
        entry._register_tabs(window)                             # noqa: SLF001
        assert recorded_advanced[-1] is True
    finally:
        window.request_close()


# ============================================================= HIGH 2

def test_preset_ids_handles_dataclass_profile_contract(
        qapp, dataclass_profiles, isolated_settings):
    """``_preset_ids`` must read ``visible_tabs`` off a frozen Profile — the
    shape the Council dogfood PR delivers. Iterating the dataclass itself
    raises TypeError, which is the launch crash the audit flags."""
    from council_qt import group_tabs
    from council_qt.window import CouncilWindow

    window = CouncilWindow()
    try:
        controller = group_tabs.GroupedTabsController(window,
                                                     dataclass_profiles)
        # The pre-fix implementation does ``{t for t in preset ...}`` which
        # raises on a Profile dataclass.
        got = controller._preset_ids("demo")                     # noqa: SLF001
        assert got == {"council", "vault", "ledger", "connections"}
        got_dev = controller._preset_ids("developer")            # noqa: SLF001
        assert "ide" in got_dev
        assert "quant_duel" not in got_dev
    finally:
        window.request_close()


def test_register_tabs_falls_back_to_flat_when_controller_setup_raises(
        qapp, isolated_settings, monkeypatch):
    """A broken profiles module (anything the controller cannot initialise)
    must leave the shell on the flat layout rather than taking the launch
    down. The audit asks for the grouped-controller setup to be wrapped."""
    entry = _load_entry()
    import council_qt.tabs as _tabs
    from council_qt.window import CouncilWindow

    # A profiles module whose shape makes GroupedTabsController raise:
    # GROUPS is a non-dict, so iterating it mid-init throws.
    mod = types.ModuleType("council_qt.profiles")
    mod.GROUPS = 123                                      # not a dict
    mod.GROUP_ORDER = ["Council"]
    mod.DEMO = set()
    mod.DEVELOPER = set()
    mod.FULL = set()
    monkeypatch.setitem(sys.modules, "council_qt.profiles", mod)

    monkeypatch.setattr(_tabs, "registry",
                        lambda advanced=False: [], raising=True)

    window = CouncilWindow()
    try:
        # Must not raise.
        entry._register_tabs(window)                             # noqa: SLF001
        # And must leave the window in the flat state (no controller
        # installed).
        assert window.grouped is None
    finally:
        window.request_close()


# ============================================================= MEDIUM a

def test_maybe_build_inner_does_not_cascade_into_every_tab(
        qapp, set_only_profiles, isolated_settings):
    """Opening one group must build only its currently-visible inner tab;
    the audit found removeTab/insertTab fires currentChanged for every
    index Qt lands on during the swap, which builds every tab in the group.
    """
    from council_qt import group_tabs
    from council_qt.window import CouncilWindow

    window = CouncilWindow()
    controller = group_tabs.GroupedTabsController(window, set_only_profiles)
    window.install_grouped_layout(controller)

    built: list = []

    def factory(tid):
        def _f():
            built.append(tid)
            return QLabel(tid)
        _f.__name__ = f"build_{tid}"
        return _f

    for group in set_only_profiles.GROUP_ORDER:
        for tab_id in set_only_profiles.GROUPS[group]:
            window.add_tab(tab_id.title(), factory(tab_id), tab_id=tab_id)
    try:
        controller.apply_profile_state({"profile": "full"})
        qapp.processEvents()
        built.clear()

        # Switch to the Knowledge group's first visible inner tab. Pre-fix,
        # the swap cascaded and built the whole group.
        for i in range(window.tabs.count()):
            if window.tabs.tabText(i) == "Knowledge":
                window.tabs.setCurrentIndex(i)
                break
        qapp.processEvents()

        # Exactly one inner tab (whichever Knowledge shows first) should
        # have built — not all six.
        assert len(built) <= 1, (
            f"opening the Knowledge group built {built!r} — cascade "
            "regression")
    finally:
        window.request_close()


def test_maybe_build_inner_preserves_profile_hidden_visibility(
        qapp, set_only_profiles, isolated_settings):
    """A tab the active profile hides must stay hidden across a lazy-build
    swap. The audit noticed insertTab defaults the new tab to visible,
    un-hiding tabs the profile had hidden."""
    from council_qt import group_tabs
    from council_qt.window import CouncilWindow

    window = CouncilWindow()
    controller = group_tabs.GroupedTabsController(window, set_only_profiles)
    window.install_grouped_layout(controller)

    for group in set_only_profiles.GROUP_ORDER:
        for tab_id in set_only_profiles.GROUPS[group]:
            def factory(tid=tab_id):
                def _f():
                    return QLabel(tid)
                _f.__name__ = f"build_{tid}"
                return _f
            window.add_tab(tab_id.title(), factory(), tab_id=tab_id)
    try:
        # Demo hides every advanced-adjacent inner tab in Knowledge except
        # vault/ledger/connections.
        controller.apply_profile_state({"profile": "demo"})
        qapp.processEvents()

        knowledge = controller.groups["Knowledge"]
        inner_bar = knowledge.inner.tabBar()

        # Switch to Knowledge: the Vault tab builds (it's current under Demo).
        for i in range(window.tabs.count()):
            if window.tabs.tabText(i) == "Knowledge":
                window.tabs.setCurrentIndex(i)
                break
        qapp.processEvents()

        # After the swap, every hidden-by-Demo tab must still be hidden.
        hidden_ids = [tid for tid in knowledge.inner_tab_ids()
                      if tid not in controller.visible_ids]
        for tid in hidden_ids:
            pos = knowledge.index_of(tid)
            assert pos >= 0
            assert not inner_bar.isTabVisible(pos), (
                f"profile-hidden inner tab {tid!r} became visible after "
                "lazy-build swap")
    finally:
        window.request_close()


# ============================================================= MEDIUM b

def test_switch_profile_to_custom_from_developer_keeps_developer_as_base(
        qapp, set_only_profiles, isolated_settings):
    """Spec §4: Custom starts with what the user is seeing. Switching from
    Developer must carry Developer over as custom_base, and advanced-mode
    must come with it — not reset to Demo/False."""
    from council_qt import group_tabs
    from council_qt.window import CouncilWindow

    window = CouncilWindow()
    controller = group_tabs.GroupedTabsController(window, set_only_profiles)
    try:
        controller.apply_profile_state({"profile": "developer"})
        assert controller.custom_base == "demo"             # default
        controller._switch_profile("custom")                 # noqa: SLF001
        assert controller.profile_name == "custom"
        assert controller.custom_base == "developer"
        assert controller.custom_advanced is True
    finally:
        window.request_close()


def test_switch_profile_to_custom_from_full_keeps_full_as_base(
        qapp, set_only_profiles, isolated_settings):
    from council_qt import group_tabs
    from council_qt.window import CouncilWindow

    window = CouncilWindow()
    controller = group_tabs.GroupedTabsController(window, set_only_profiles)
    try:
        controller.apply_profile_state({"profile": "full"})
        controller._switch_profile("custom")                 # noqa: SLF001
        assert controller.custom_base == "full"
        assert controller.custom_advanced is True
    finally:
        window.request_close()


# ============================================================= MEDIUM c

def test_grouped_controller_registers_advanced_tabs_so_profile_can_show_them(
        qapp, set_only_profiles, isolated_settings, monkeypatch):
    """With the grouped controller installed, advanced tabs register no
    matter what ``modes.advanced()`` says — the profile filter is what
    hides them. Otherwise a runtime Demo→Developer flip cannot bring them
    back without a relaunch."""
    entry = _load_entry()
    import council_qt.tabs as _tabs
    from council_qt.window import CouncilWindow

    recorded_advanced: list = []

    def fake_registry(advanced=False):
        recorded_advanced.append(advanced)
        return []

    monkeypatch.setattr(_tabs, "registry", fake_registry, raising=True)

    # No env, no CLI override. Pre-fix, modes.advanced() would see the
    # Demo profile (or a missing file) and return False; the six advanced
    # tabs would never register, and a runtime switch to Developer would
    # have nothing to show.
    monkeypatch.delenv("COUNCIL_ADVANCED", raising=False)
    monkeypatch.setattr(sys, "argv", ["council_qt.py"])

    # Pre-seed Demo so modes.advanced() definitely returns False.
    from council_qt import ui_settings
    ui_settings.save_ui({"profile": "demo"})

    window = CouncilWindow()
    try:
        entry._register_tabs(window)                             # noqa: SLF001
        assert recorded_advanced, "registry() was never called"
        assert recorded_advanced[-1] is True, (
            "grouped layout must register advanced tabs so the profile "
            "filter can show them at runtime")
    finally:
        window.request_close()
