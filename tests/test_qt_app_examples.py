"""The example Qt apps in examples/qt_apps/ and gui_reference, which hands
them to the models.

Every example is held to what makes it worth copying: it passes the policy
gate a generated app must pass, it builds offscreen, and the thing it is the
example OF works (a plot of each kind draws, an edit undoes, a reply
streams, a wizard branches). The Code tab's "Qt app builder" gets them."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gui_policy  # noqa: E402
import gui_reference as gr  # noqa: E402

pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QWizard  # noqa: E402

NAMES = gr.names()
SAMPLE_DATA = ROOT / "assets" / "sample_data"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _delete_windows():
    """Each test's windows are deleted by Qt before the next test. Left for
    the interpreter's shutdown, an undo stack or a worker pool torn down
    after QApplication crashed the process once every test had passed
    (segfault on Linux, a failed file on the Windows runner)."""
    yield
    app = QApplication.instance()
    if app is None:
        return
    for w in app.topLevelWidgets():
        w.close()
        w.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


def load(name: str):
    """An example as a module, by path: examples/ is not a package."""
    path = gr.EXAMPLES_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"qt_example_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod              # dataclasses look it up
    spec.loader.exec_module(mod)
    return mod


def spin(ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def wait_for(cond, ms: int = 5000) -> bool:
    for _ in range(ms // 20):
        if cond():
            return True
        spin(20)
    return cond()


# ============================================================
# The set
# ============================================================

def test_there_are_ten_examples_at_most():
    assert 1 <= len(NAMES) <= gr.MAX_EXAMPLES == 10


def test_the_requested_two_are_there():
    assert {"csv_plotter", "local_chatbot"} <= set(NAMES)


@pytest.mark.parametrize("name", NAMES)
def test_each_docstring_has_the_sections_gui_reference_reads(name):
    ex = gr.load(name)
    assert ex.title and ex.does
    assert len(ex.shows) >= 5, "SHOWS lists what the example teaches"
    doc = (gr.EXAMPLES_DIR / f"{name}.py").read_text(encoding="utf-8")
    for head in ("WHAT IT DOES", "SHOWS", "RUN", "REQUIRES"):
        assert f"\n{head}" in doc


@pytest.mark.parametrize("name", NAMES)
def test_each_passes_the_generated_app_gate(name):
    """What a model copies from an example must be allowed in a generated
    app, with only the packages the example declares."""
    ex = gr.load(name)
    source = ex.path.read_text(encoding="utf-8")
    assert gui_policy.check_requires(ex.requires, "standalone") == []
    ok, errs = gui_policy.validate(source, mode="standalone",
                                   extra_modules=ex.requires, toolkit="qt")
    assert ok, errs


@pytest.mark.parametrize("name", NAMES)
def test_each_builds_offscreen(qapp, name):
    mod = load(name)
    win = mod.make_window([])
    win.show()
    spin(50)
    assert win.windowTitle()
    win.close()
    win.deleteLater()


def test_each_is_in_the_readme():
    readme = (gr.EXAMPLES_DIR / "README.md").read_text(encoding="utf-8")
    for name in NAMES:
        assert f"`{name}.py`" in readme


# ============================================================
# What each is the example of
# ============================================================

def test_csv_plotter_draws_every_chart_for_a_folder(qapp):
    pytest.importorskip("matplotlib")
    pytest.importorskip("pandas")
    m = load("csv_plotter")
    win = m.make_window([str(SAMPLE_DATA)])
    assert win.files.count() == len(list(SAMPLE_DATA.glob("*.csv")))
    assert win.ticked_columns(), "the first numeric column is ticked"
    for chart in m.CHARTS:
        win.chart.setCurrentText(chart)
        win.statusBar().clearMessage()
        win.replot()
        assert "Could not draw" not in win.statusBar().currentMessage(), chart
        assert win.figure.axes, chart
    # Two files ticked: their series are labelled by file.
    win.files.item(1).setCheckState(Qt.Checked)
    for i in range(win.y_cols.count()):
        win.y_cols.item(i).setCheckState(Qt.Checked)
    labels = [s[0] for s in win.series()]
    assert labels and all(":" in label for label in labels)
    win.close()


def test_local_chatbot_streams_a_reply_with_no_model(qapp):
    m = load("local_chatbot")
    win = m.make_window([])
    assert wait_for(lambda: win.model.count() == 1)        # Echo is ready
    win.input.setPlainText("hello there")
    win.send()
    assert wait_for(lambda: len(win.history) == 2 and win.thread is None)
    assert win.history[-1] == {"role": "assistant",
                               "content": "You said: hello there "}
    assert "You said: hello there" in win.view.toPlainText()
    win.close()


def test_local_chatbot_says_what_is_missing(qapp, monkeypatch):
    """A backend whose package is not installed is reported, not a crash."""
    m = load("local_chatbot")
    win = m.make_window([])
    wait_for(lambda: win.thread is None)
    failures = []
    monkeypatch.setattr(m.QMessageBox, "warning",
                        lambda *a: failures.append(a[2]))
    monkeypatch.setitem(sys.modules, "ollama", None)      # import fails
    win.kind.setCurrentText(m.OllamaBackend.name)
    assert wait_for(lambda: bool(failures))
    assert "pip install ollama" in failures[0]
    win.close()


def test_table_editor_undoes_edits_and_deletes(qapp):
    m = load("table_editor")
    win = m.make_window([])
    model, proxy = win.model, win.proxy
    first = list(model.rows[0])
    proxy.setData(proxy.index(0, 2), "99")
    assert model.rows[0][2] == "99" and "*" in win.windowTitle()
    win.undo.undo()
    assert model.rows[0] == first and "*" not in win.windowTitle()
    n = model.rowCount()
    win.table.selectRow(0)
    win.delete_rows()
    assert model.rowCount() == n - 1
    win.undo.undo()
    assert model.rowCount() == n and model.rows[0] == first
    win.filter.setText("M6")
    assert proxy.rowCount() == 2
    win.undo.setClean()
    win.close()


def test_table_editor_pastes_into_the_rows_shown_when_sorted(qapp):
    m = load("table_editor")
    win = m.make_window([])
    win.table.sortByColumn(2, Qt.DescendingOrder)        # qty, as numbers
    top = [win.proxy.index(r, 0).data() for r in range(2)]
    win.table.setCurrentIndex(win.proxy.index(0, 3))
    QApplication.clipboard().setText("1.11\n2.22")
    win.paste()
    price = {r[0]: r[3] for r in win.model.rows}
    assert (price[top[0]], price[top[1]]) == ("1.11", "2.22")
    win.undo.setClean()
    win.close()


def test_settings_dialog_round_trips_through_json(qapp, tmp_path):
    m = load("settings_dialog")
    path = tmp_path / "settings.json"
    win = m.MainWindow(path)
    dlg = m.SettingsDialog(win.settings, win.apply, win)
    assert dlg.from_widgets() == win.settings, "widgets show the settings"
    dlg.user_name.setText("Tester")
    dlg.theme_group.button(m.THEMES.index("Dark")).setChecked(True)
    dlg.zoom.setValue(150)
    dlg.accept()
    saved = m.read_settings(path)
    assert (saved.user_name, saved.theme, saved.zoom) == ("Tester", "Dark",
                                                          150)
    assert json.loads(path.read_text())["zoom"] == 150
    dlg2 = m.SettingsDialog(saved, win.apply, win)
    dlg2.user_name.setText("Someone else")
    dlg2.reject()                                     # Cancel restores
    assert win.settings.user_name == "Tester"
    win.close()


def test_image_viewer_steps_and_maps_a_region(qapp):
    pytest.importorskip("numpy")
    from PySide6.QtCore import QRectF
    from PySide6.QtWidgets import QGraphicsRectItem
    m = load("image_viewer")
    win = m.make_window([str(ROOT / "assets")])
    assert len(win.files) >= 2 and win.hist.counts.sum() > 0
    win.step(1)
    assert win.slider.value() == 1 == win.thumbs.currentRow()
    v = win.view
    v.region = QGraphicsRectItem()
    v.scene().addItem(v.region)
    v.region.setRect(v.pix.mapRectToScene(QRectF(10, 10, 50, 40)))
    assert v.region_in_image().toRect().getRect() == (10, 10, 50, 40)
    win.close()


def test_job_runner_checksums_a_folder_and_cancels(qapp):
    import hashlib
    m = load("job_runner")
    win = m.make_window([str(SAMPLE_DATA)])
    win.start()
    assert wait_for(lambda: len(win.results) == len(win.jobs))
    target, status, digest = win.results[0]
    assert status == "done"
    assert digest == hashlib.sha256(Path(target).read_bytes()).hexdigest()
    win.kind.setCurrentText("Simulated work")
    win.count.setValue(12)
    win.parallel.setValue(2)
    win.start()
    win.cancel_all()
    assert wait_for(lambda: len(win.results) == 12)
    assert win._counts()["cancelled"] >= 10
    win.close()


def test_live_dashboard_raises_and_clears_alarms(qapp):
    m = load("live_dashboard")
    win = m.DashboardWindow(seed=1)
    win.feed.stop()
    card = win.cards[0]
    s = card.sensor
    card.update_value(s.alarm + 1)
    assert card.state == m.ALARM and win.alarms.count() == 1
    card.update_value(s.warn - 0.1)                   # inside the hysteresis
    assert card.state == m.WARN
    card.update_value(s.low)
    assert card.state == m.OK and win.alarms.count() == 3
    win.grab()                                        # every paintEvent runs
    win.close()


def test_text_editor_highlights_finds_and_replaces_in_one_undo(qapp,
                                                              tmp_path):
    m = load("text_editor")
    src = tmp_path / "demo.py"
    src.write_text('"""doc\nstring"""\n# a comment\ndef f():\n'
                   '    return "x" + "x"\n', encoding="utf-8")
    win = m.EditorWindow([src], recent_file=tmp_path / "recent.json")
    win.show()
    qapp.processEvents()
    ed = win.editor()
    doc = ed.document()
    assert doc.findBlockByNumber(0).userState() == 1      # inside """
    assert doc.findBlockByNumber(1).userState() == 0      # closed
    comment = doc.findBlockByNumber(2).layout().formats()
    assert comment and comment[0].format.fontItalic()
    win.find_bar.find.setText('"x"')
    win.find_bar.replace.setText('"y"')
    assert win.find_bar.replace_all() == 2
    assert '"y" + "y"' in ed.toPlainText()
    ed.undo()
    assert '"x" + "x"' in ed.toPlainText()
    assert win.recent() == [str(src.resolve())]
    doc.setModified(False)
    win.close()


def test_setup_wizard_validates_branches_and_writes(qapp, tmp_path):
    m = load("setup_wizard")
    wiz = m.SetupWizard()
    wiz.show()
    wiz.next()
    nxt = wiz.button(QWizard.NextButton)
    assert not nxt.isEnabled(), "the name is mandatory"
    wiz.project.name.setText("Line 3")
    wiz.next()
    wiz.place.folder.setText(str(tmp_path / "missing"))
    assert not nxt.isEnabled()
    wiz.place.folder.setText(str(tmp_path))
    wiz.next()
    wiz.next()
    assert wiz.currentId() == m.PAGE_PARTS
    wiz.next()
    wiz.next()
    assert wiz.currentId() == m.PAGE_SUMMARY
    wiz.next()                                        # Create
    assert wait_for(lambda: wiz.create_page.done)
    cfg = json.loads((tmp_path / "Line_3" / "project.json").read_text())
    assert cfg["name"] == "Line 3" and cfg["components"]
    wiz.close()

    blank = m.SetupWizard()
    blank.show()
    blank.next()
    blank.project.name.setText("Other")
    blank.project.template.setCurrentIndex(
        blank.project.template.findData("blank"))
    blank.next()
    blank.place.folder.setText(str(tmp_path))
    blank.next()
    blank.next()
    assert blank.currentId() == m.PAGE_WHEN, "Blank skips the components"
    blank.close()


# ============================================================
# gui_reference: picking one for a request
# ============================================================

@pytest.mark.parametrize("request_text, expected", [
    ("plot a folder of csv files as violin plots", "csv_plotter"),
    ("a chat window for a gguf model", "local_chatbot"),
    ("talk to ollama", "local_chatbot"),
    ("spreadsheet I can edit with undo", "table_editor"),
    ("browse folders and preview files", "file_browser"),
    ("a preferences dialog", "settings_dialog"),
    ("zoom into an image and crop a region", "image_viewer"),
    ("run jobs in the background with progress bars", "job_runner"),
    ("a dashboard with gauges and alarms", "live_dashboard"),
    ("code editor with syntax highlighting", "text_editor"),
    ("a step-by-step setup wizard", "setup_wizard"),
])
def test_select_picks_the_matching_example(request_text, expected):
    assert gr.select(request_text)[0].name == expected


def test_nothing_matches_an_unrelated_request():
    assert gr.select("hello") == []
    assert gr.for_prompt("hello") == ""


def test_for_prompt_has_the_example_whole_and_lists_the_rest():
    block = gr.for_prompt("plot csv files")
    src = (gr.EXAMPLES_DIR / "csv_plotter.py").read_text(encoding="utf-8")
    assert src in block
    assert "setup_wizard:" in block and "settings_dialog.py ---" not in block


def test_for_prompt_falls_back_to_an_outline_when_short_of_room():
    block = gr.for_prompt("plot csv files", max_chars=2000)
    assert "too long to show whole" in block
    assert "class PlotWindow:" in block and "def replot(...)" in block
    assert "def replot(self)" not in block


def test_requires_lines_parse():
    assert gr.requires_of("pandas, numpy, matplotlib\n") == [
        "pandas", "numpy", "matplotlib"]
    assert gr.requires_of("nothing beyond PySide6") == []
    assert gr.load("local_chatbot").requires == ["ollama", "llama_cpp"]


# ============================================================
# The Code tab's Qt app builder
# ============================================================

def test_the_qt_app_builder_reads_the_examples(tmp_path):
    from council_core import code_agent as ca
    from council_core import code_profiles as ap
    from council_core import project as pj
    prof = ap.get(tmp_path, "qt-app-builder")
    assert prof is not None and prof.built_in
    assert gr.attached(prof.references)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('hi')\n")
    project = pj.Project("plots", str(repo))
    pj.save(tmp_path / "vault", project)

    plain = ca.context(tmp_path / "vault", project, "plot csv files")
    assert "QT EXAMPLE APPS" not in plain, "only an agent that asks for them"

    seen = ap.with_references(project, prof)
    ctx = ca.context(tmp_path / "vault", seen, "plot a folder of csv files")
    assert "QT EXAMPLE APPS" in ctx and "class PlotWindow" in ctx
    hits = pj.references(seen).search("QThreadPool QRunnable cancel")
    assert any(Path(h["source"]).name == "job_runner.py" for h in hits)
