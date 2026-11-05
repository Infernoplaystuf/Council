"""The Build tab's physics models, offscreen: "New physics model…" (an
example, or a goal the Physics modeller plans), the Model page (inputs, Run
model, results, charts), connecting data and Calibrate."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("scipy")
pytest.importorskip("matplotlib")
pytest.importorskip("pygments")

from PySide6.QtWidgets import QApplication, QComboBox, QDoubleSpinBox  # noqa: E402

from council_core import physics_project as pp  # noqa: E402
from council_qt.tabs.build import BuildActions, BuildTab  # noqa: E402
from council_qt.widgets.physics_panel import (ModelPanel, NewModelDialog,  # noqa: E402
                                              PhysicsActions, chart_figure)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def pump(qapp, until, seconds=60.0):
    deadline = time.time() + seconds
    while not until() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()
    assert until(), "timed out"


def panel_for(tmp_path, example, **actions) -> ModelPanel:
    dest = pp.create(tmp_path / example, example=example, git=False)
    panel = ModelPanel(None, PhysicsActions(**actions), python=lambda: sys.executable,
                       vault_dir=tmp_path / "vault")
    assert panel.set_folder(dest)
    return panel


def test_a_folder_that_isnt_a_model_shows_nothing(qapp, tmp_path):
    panel = ModelPanel(None, PhysicsActions(), python=lambda: sys.executable,
                       vault_dir=tmp_path)
    assert not panel.set_folder(tmp_path) and panel.folder is None
    panel.deleteLater()


def test_the_organ_runs_and_shows_its_cut_list_and_charts(qapp, tmp_path):
    panel = panel_for(tmp_path, "pvc_organ")
    pump(qapp, lambda: panel.boxes and not panel._busy)
    assert "PVC pipe organ" in panel.title.text()
    assert isinstance(panel.boxes["instrument"], QComboBox)
    assert isinstance(panel.boxes["temperature_c"], QDoubleSpinBox)
    panel.boxes["notes"].setValue(5)
    panel.on_run()
    pump(qapp, lambda: panel.result and not panel._busy)
    titles = [panel.pages.tabText(i) for i in range(panel.pages.count())]
    assert titles[:3] == ["Results", "Cut list", "Measured vs model (measured_pipes)"]
    assert titles[-4:] == ["Charts", "Data", "Calibrate", "About"]
    assert panel.pages.widget(1).rowCount() == 5
    assert panel.charts_lay.count() == 1
    assert pp.load_manifest(panel.folder)["inputs"]["notes"] == 5     # kept for next time
    assert panel.export_btn.isEnabled()
    out = panel.on_export()
    assert out.is_file() and out.parent.name == "results"
    # a second run replaces the table pages instead of adding more
    panel.on_run()
    pump(qapp, lambda: not panel._busy)
    assert panel.pages.count() == len(titles)
    panel.deleteLater()


def test_calibrate_fits_the_end_correction_and_uses_it(qapp, tmp_path):
    panel = panel_for(tmp_path, "pvc_organ")
    pump(qapp, lambda: panel.boxes and not panel._busy)
    panel.on_run()
    pump(qapp, lambda: panel.result and not panel._busy)
    assert panel.fit_box.currentText() == "End correction from measured open pipes"
    assert panel.table_box.currentText() == "measured_pipes"
    assert panel.x_box.currentText() == "Length (mm)" and panel.x_unit.currentText() == "mm"
    assert panel.y_box.currentText() == "Frequency (Hz)"
    panel.on_fit()
    assert panel.calibration is not None, panel.fit_text.text()
    k = panel.calibration.sets["end_correction"]
    assert 0.7 < k < 0.9 and panel.use_btn.isEnabled()
    panel.use_fit()
    assert abs(panel.inputs()["end_correction"] - k) < 1e-6
    pump(qapp, lambda: not panel._busy)
    panel.deleteLater()


def test_data_is_connected_and_removed(qapp, tmp_path):
    extra = tmp_path / "more"
    extra.mkdir()
    (extra / "t.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    panel = panel_for(tmp_path, "antenna", ask_folder=lambda start: str(extra),
                      pick_ledger=lambda: str(tmp_path / "led.csv"),
                      pick_graph=lambda vault: ("main", "Main graph", "yagi notes"))
    pump(qapp, lambda: not panel._busy)
    panel.add_folder()
    panel.add_ledger()
    panel.add_knowledge()
    kinds = [x.kind for x in pp.links(panel.folder)]
    assert kinds == ["folder", "folder", "ledger", "knowledge"]
    assert panel.links_list.count() == 4
    panel.read_data()
    pump(qapp, lambda: not panel._busy)
    assert "analyzer_sweep" in panel.data_view.toHtml() and "t" in panel.gathered.tables
    assert any("led.csv" in n for n in panel.gathered.notes)     # missing: said, not raised
    panel.links_list.setCurrentRow(2)
    panel.remove_link()
    assert [x.kind for x in pp.links(panel.folder)] == ["folder", "folder", "knowledge"]
    panel.deleteLater()


def test_a_failing_model_says_so(qapp, tmp_path):
    panel = panel_for(tmp_path, "water_erosion")
    pump(qapp, lambda: panel.boxes and not panel._busy)
    (panel.folder / "model.py").write_text(
        (panel.folder / "model.py").read_text(encoding="utf-8").replace(
            "def compute(p, data) -> Result:\n", "def compute(p, data) -> Result:\n    1 / 0\n"),
        encoding="utf-8")
    panel.on_run()
    pump(qapp, lambda: not panel._busy)
    assert "ZeroDivisionError" in panel.status.text()
    panel.deleteLater()


def test_charts_draw_polar_log_and_scatter():
    for kind, extra in (("polar", {}), ("line", {"log_x": True, "log_y": True}),
                        ("scatter", {})):
        fig = chart_figure({"title": "t", "x": [0, 90, 180], "series": {"a": [1, 2, None],
                            "b": [3, 4, 5]}, "kind": kind, **extra})
        assert fig.axes


def test_the_new_model_window_names_the_folder(qapp, tmp_path):
    dlg = NewModelDialog(tmp_path)
    assert dlg.result_dict()["example"] == "pvc_organ"
    assert dlg.result_dict()["dest"] == str(tmp_path / "physics_models" / "pvc_pipe_organ")
    dlg.goal_rb.setChecked(True)
    dlg.goal.setPlainText("Steel bar chimes")
    got = dlg.result_dict()
    assert got["example"] == "" and got["goal"] == "Steel bar chimes"
    dlg.deleteLater()


# ── in the Build tab ─────────────────────────────────────────────────────
def make_tab(tmp_path, ask_new):
    actions = BuildActions(
        vault_dir=tmp_path / "vault", state_file=tmp_path / "state.json",
        unsaved_dir=tmp_path / "unsaved", confirm=lambda t, x: True,
        ask_folder=lambda start: "", ask_name=lambda t, x: None,
        physics=PhysicsActions(ask_new=ask_new))
    return BuildTab(None, actions)


def test_new_physics_model_from_an_example_opens_its_model_page(qapp, tmp_path):
    dest = tmp_path / "vault" / "physics_models" / "eddy"
    tab = make_tab(tmp_path, lambda vault, parent=None: {
        "dest": str(dest), "example": "eddy_current", "goal": "", "title": "Eddy"})
    assert not tab.workspace.isTabVisible(1)                   # scratch is no model
    tab.on_new_physics()
    assert tab.root == dest and pp.is_model(dest)
    assert tab.workspace.isTabVisible(1) and tab.workspace.currentIndex() == 1
    assert "Eddy currents example" in tab.status.text()
    tab.model_panel._python = lambda: sys.executable
    pump(qapp, lambda: not tab.model_panel._busy)
    tab.open_folder(tmp_path)                                 # not a model: the page goes
    assert not tab.workspace.isTabVisible(1)
    tab.deleteLater()


def test_new_physics_model_from_a_goal_asks_the_physics_modeller_for_a_plan(qapp, tmp_path):
    dest = tmp_path / "vault" / "physics_models" / "bars"
    tab = make_tab(tmp_path, lambda vault, parent=None: {
        "dest": str(dest), "example": "", "goal": "Notes of struck steel bars", "title": "Bars"})
    sent = []
    tab.chat_panel.send = lambda *a, **k: sent.append(tab.chat_panel.input.toPlainText())
    tab.on_new_physics()
    assert tab.chat_panel.mode() == "agent"
    assert tab.chat_panel.profile_box.currentText() == "Physics modeller"
    assert sent and "Notes of struck steel bars" in sent[0] and "README.md" in sent[0]
    tab.deleteLater()


def test_cancelling_the_new_model_window_does_nothing(qapp, tmp_path):
    tab = make_tab(tmp_path, lambda vault, parent=None: None)
    before = tab.root
    tab.on_new_physics()
    assert tab.root == before
    tab.deleteLater()
