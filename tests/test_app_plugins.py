"""
Council's own plugins (council_core.app_plugins, council_qt.plugin_tabs and
the Settings tab):

  * the manifest reader is the Typhon one, unchanged; Council checks what a
    Council manifest may say (kind "tab", "council." ids, network "none");
  * listing reads manifests only — no plugin code runs to list it;
  * every plugin starts OFF; switching one on is kept in plugins.json;
  * plugins from outside Council are listed but can't be switched on;
  * a switched-on plugin's tab is built from its own folder, and a plugin
    that raises, or returns no widget, leaves Council running with a page
    that says why (and the traceback in plugin_errors.log);
  * the Settings tab lists them and ticking a row saves it.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

from council_core import app_plugins as ap  # noqa: E402
from council_core import plugin_manifest as pm  # noqa: E402


@pytest.fixture(autouse=True)
def app_dir(tmp_path, monkeypatch):
    home = tmp_path / "app"
    monkeypatch.setenv("COUNCIL_APP_DIR", str(home))
    return home


def manifest(**over):
    doc = {"schema": 1, "id": "council.sample", "short": "sample",
           "name": "Sample", "version": "0.1.0", "api": ap.API_VERSION,
           "kind": "tab", "entry": "tab:build_tab", "network": "none",
           "description": "A sample tab."}
    doc.update(over)
    return {k: v for k, v in doc.items() if v is not None}


TAB_SOURCE = '''
from PySide6.QtWidgets import QLabel

BUILT = []

def build_tab(ctx):
    ctx.save_json("plan.json", {"hello": ctx.id})
    BUILT.append(ctx)
    return QLabel("sample tab")
'''


def make_plugin(root: Path, folder: str, doc=None, source=TAB_SOURCE,
                init=True) -> Path:
    path = root / folder
    path.mkdir(parents=True)
    (path / "plugin.json").write_text(json.dumps(doc or manifest()),
                                      encoding="utf-8")
    if init:
        (path / "__init__.py").write_text("", encoding="utf-8")
    (path / "tab.py").write_text(source, encoding="utf-8")
    return path


def listed(tmp_path, names=("sample",)):
    return ap.discover(builtin_dir=tmp_path / "builtin", builtin_names=names,
                       extra_dir=tmp_path / "added")


# ---------------------------------------------------------------- the format
def test_the_manifest_reader_is_the_typhon_one_unchanged():
    """One format, one reader: plugin_manifest.py is a copy of the
    typhon-plugins branch's, so Council's check is a separate module."""
    assert pm.SCHEMA == 1
    assert pm.KINDS == ("camera", "source", "feature")
    assert pm.BUILTIN_PREFIX == "typhon."


def test_a_good_council_manifest_is_read():
    m = ap.check(manifest(), builtin=True)
    assert (m.id, m.kind, m.network, m.entry) == (
        "council.sample", "tab", "none", "tab:build_tab")
    assert (m.module, m.attribute) == ("tab", "build_tab")


@pytest.mark.parametrize("over,field_name,words", [
    ({"kind": "camera"}, "kind", "not one Council has"),
    ({"network": "internet"}, "network", "offline"),
    ({"id": "typhon.sample"}, "id", "starts 'council.'"),
    ({"surprise": 1}, "surprise", "does not have"),
    ({"entry": "../x:y"}, "entry", "module:object"),
    ({"version": "1"}, "version", "major.minor.patch"),
    ({"schema": 2}, "schema", "schema 1"),
    ({"name": "  "}, "name", "empty"),
])
def test_a_bad_council_manifest_is_refused_with_its_field(over, field_name,
                                                          words):
    with pytest.raises(pm.ManifestError) as err:
        ap.check(manifest(**over), builtin=True)
    assert err.value.field == field_name
    assert words in str(err.value)


def test_council_ids_are_kept_for_council():
    with pytest.raises(pm.ManifestError, match="kept for"):
        ap.check(manifest(author="me", licence="MIT"), builtin=False)


def test_the_api_rule_is_the_exact_0x():
    assert ap.api_problem(ap.API_VERSION) == ""
    assert "plugin API" in ap.api_problem("0.9")
    assert "plugin API" in ap.api_problem("1.0")


# ------------------------------------------------------------------ finding
def test_listing_runs_no_plugin_code(tmp_path):
    marker = tmp_path / "ran.txt"
    make_plugin(tmp_path / "builtin", "sample",
                source=f"open({str(marker)!r}, 'w').write('x')\n"
                       "def build_tab(ctx):\n    return None\n")
    (found,) = listed(tmp_path)
    assert found.can_turn_on, found.problem
    assert not marker.exists()


def test_a_folder_not_on_the_built_in_list_is_refused(tmp_path):
    make_plugin(tmp_path / "builtin", "sample")
    (found,) = listed(tmp_path, names=())
    assert not found.can_turn_on
    assert "not on their list" in found.problem


def test_a_folder_must_match_its_id(tmp_path):
    make_plugin(tmp_path / "builtin", "other")
    (found,) = listed(tmp_path, names=("other",))
    assert "lives in 'sample'" in found.problem


def test_a_plugin_from_outside_council_is_listed_but_cannot_be_turned_on(
        tmp_path):
    doc = manifest(id="acme.budget", author="Acme", licence="MIT")
    make_plugin(tmp_path / "added", "acme_budget", doc=doc)
    (found,) = listed(tmp_path, names=())
    assert found.manifest is not None and not found.builtin
    assert found.problem == ap.NOT_YET_APPROVED
    assert not found.can_turn_on


def test_a_plugin_needing_a_service_council_lacks_says_so(tmp_path):
    make_plugin(tmp_path / "builtin", "sample",
                doc=manifest(needs=["local_model"]))
    (found,) = listed(tmp_path)
    assert "does not offer yet" in found.problem


def test_a_plugin_needing_a_missing_package_says_so(tmp_path):
    make_plugin(tmp_path / "builtin", "sample",
                doc=manifest(python=["surely_not_installed_pkg"]))
    (found,) = listed(tmp_path)
    assert "isn't installed" in found.problem


def test_a_broken_manifest_is_listed_with_why(tmp_path):
    path = tmp_path / "builtin" / "sample"
    path.mkdir(parents=True)
    (path / "plugin.json").write_text('{"id": 1, "id": 2}', encoding="utf-8")
    (found,) = listed(tmp_path)
    assert found.manifest is None and "twice" in found.problem


def test_listing_missing_folders_makes_none(tmp_path, app_dir):
    assert ap.discover(builtin_dir=tmp_path / "nope", builtin_names=()) == []
    assert not app_dir.exists()


def test_the_real_built_in_list_reads_cleanly():
    for found in ap.discover():
        if found.builtin:
            assert found.manifest is not None, found.problem


# --------------------------------------------------------------- on and off
def test_every_plugin_starts_off(tmp_path):
    make_plugin(tmp_path / "builtin", "sample")
    found = listed(tmp_path)
    assert ap.enabled_ids() == []
    assert ap.switched_on(found) == []


def test_switching_on_is_kept_for_the_next_start(tmp_path, app_dir):
    make_plugin(tmp_path / "builtin", "sample")
    assert ap.set_enabled("council.sample", True) == ["council.sample"]
    saved = json.loads((app_dir / "plugins.json").read_text(encoding="utf-8"))
    assert saved == {"enabled": ["council.sample"]}
    assert [f.id for f in ap.switched_on(listed(tmp_path))] == [
        "council.sample"]
    ap.set_enabled("council.sample", False)
    assert ap.enabled_ids() == []


def test_a_broken_settings_file_means_all_off(app_dir):
    app_dir.mkdir(parents=True)
    (app_dir / "plugins.json").write_text("{not json", encoding="utf-8")
    assert ap.enabled_ids() == []


def test_a_plugin_that_cannot_run_is_not_loaded_even_if_switched_on(tmp_path):
    doc = manifest(id="acme.budget", author="Acme", licence="MIT")
    make_plugin(tmp_path / "added", "acme_budget", doc=doc)
    ap.set_enabled("acme.budget", True)
    assert ap.switched_on(listed(tmp_path, names=())) == []


# ------------------------------------------------------------- the context
def test_the_context_keeps_files_in_the_plugins_own_folder(app_dir):
    ctx = ap.PluginContext(manifest=ap.check(manifest(), builtin=True))
    ctx.save_json("plan.json", {"a": 1})
    assert (app_dir / "plugin_data" / "council.sample" / "plan.json").exists()
    assert ctx.load_json("plan.json") == {"a": 1}
    assert ctx.load_json("missing.json", default={}) == {}
    for bad in ("../x.json", "a/b.json", ""):
        with pytest.raises(ValueError):
            ctx.save_json(bad, {})


# ------------------------------------------------------------- the Qt side
@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


class FakeWindow:
    def __init__(self):
        self.added = []

    def add_tab(self, title, factory, *, eager=False, tab_id=None):
        self.added.append((title, factory, tab_id))


def test_a_switched_on_plugin_adds_its_tab_and_builds_from_its_folder(
        tmp_path, qapp, app_dir):
    from PySide6.QtWidgets import QLabel
    from council_qt import plugin_tabs

    make_plugin(tmp_path / "builtin", "sample", init=False)
    found = listed(tmp_path)
    window = FakeWindow()
    assert plugin_tabs.register(window, found) == []        # off: nothing

    ap.set_enabled("council.sample", True)
    assert plugin_tabs.register(window, found) == ["🧩 Sample"]
    title, factory, tab_id = window.added[0]
    assert tab_id == "plugin_council_sample"
    widget = factory()
    assert isinstance(widget, QLabel) and widget.text() == "sample tab"
    plan = app_dir / "plugin_data" / "council.sample" / "plan.json"
    assert json.loads(plan.read_text(encoding="utf-8")) == {
        "hello": "council.sample"}


@pytest.mark.parametrize("source,words", [
    ("def build_tab(ctx):\n    raise RuntimeError('boom')\n", "boom"),
    ("raise SystemExit(3)\n", "SystemExit"),
    ("def build_tab(ctx):\n    return 42\n", "not a widget"),
    ("build_tab = 5\n", "not something Council can call"),
])
def test_a_failing_plugin_leaves_council_running(tmp_path, qapp, app_dir,
                                                 source, words):
    from PySide6.QtWidgets import QLabel
    from council_qt import plugin_tabs

    # A fresh id per case: a plugin's package is kept once imported.
    short = f"bad{abs(hash(source)) % 10000}"
    doc = manifest(id=f"council.{short}", short=short, name="Broken")
    make_plugin(tmp_path / "builtin", short, doc=doc, source=source)
    (found,) = listed(tmp_path, names=(short,))
    page = plugin_tabs.build_page(found)
    assert page.objectName() == "PluginFaultPage"
    text = " ".join(l.text() for l in page.findChildren(QLabel))
    assert "Broken could not start" in text and words in text
    log = (app_dir / "plugin_errors.log").read_text(encoding="utf-8")
    assert f"council.{short}" in log


def test_listing_failure_never_stops_the_launch(monkeypatch, qapp):
    from council_qt import plugin_tabs

    def broken(**_kw):
        raise OSError("disk gone")
    monkeypatch.setattr(ap, "discover", broken)
    assert plugin_tabs.register(FakeWindow()) == []


class FakeActions:
    def __init__(self, found):
        self.found = found
        self.on = []

    def plugins(self):
        return self.found

    def enabled(self):
        return list(self.on)

    def set_enabled(self, ident, on):
        self.on = [i for i in self.on if i != ident] + ([ident] if on else [])

    def plugins_folder(self):
        return Path("/nonexistent")

    def open_folder(self, folder):
        pass


def test_the_settings_tab_lists_plugins_and_ticking_saves(tmp_path, qapp):
    from PySide6.QtCore import Qt
    from council_qt.tabs.settings import SettingsTab

    make_plugin(tmp_path / "builtin", "sample")
    doc = manifest(id="acme.budget", author="Acme", licence="MIT",
                   name="Budget")
    make_plugin(tmp_path / "added", "acme_budget", doc=doc)
    actions = FakeActions(listed(tmp_path))
    tab = SettingsTab(None, actions=actions)

    rows = [tab.table.topLevelItem(i)
            for i in range(tab.table.topLevelItemCount())]
    assert [r.text(1) for r in rows] == ["Sample", "Budget"]
    assert all(r.checkState(0) == Qt.CheckState.Unchecked for r in rows)
    assert rows[0].text(4) == "Ready"
    assert not rows[1].flags() & Qt.ItemFlag.ItemIsEnabled
    assert "approval" in rows[1].text(4)

    rows[0].setCheckState(0, Qt.CheckState.Checked)
    assert actions.on == ["council.sample"]
    assert "next start" in tab.status.text()

    tab.table.setCurrentItem(rows[0])
    assert "council.sample" in tab.detail.text()


def test_the_settings_tab_says_when_there_are_no_plugins(qapp):
    from council_qt.tabs.settings import SettingsTab

    tab = SettingsTab(None, actions=FakeActions([]))
    assert "No plugins yet" in tab.detail.text()
