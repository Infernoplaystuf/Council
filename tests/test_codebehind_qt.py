"""
"Write it with the model…" in the Designer tab — offscreen, scripted model.

The seam, not the decisions (test_designer_codebehind.py has those): the
Wiring group shows the writer prefilled from the widget's note; a request
runs on a worker and reports back; the review is the host's to accept; an
Accept writes logic.py and wires the widget through the Scene (undoable,
dirty); a decline writes nothing; Stop stops; and the worker never touches
a widget off the GUI thread.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

pytest.importorskip("PySide6", reason="the Designer tab needs PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

import gui_projects  # noqa: E402
from council_core import designer_project as dp  # noqa: E402
from council_qt.tabs.designer import DesignerActions, DesignerTab  # noqa: E402
from council_qt.widgets import wiring as wiring_widgets  # noqa: E402
from gui_shapes import new_shape  # noqa: E402

GOOD = '''```python
def count_images(image_folder: str) -> dict:
    """Count the PNG files."""
    import os
    names = sorted(n for n in os.listdir(image_folder)
                   if n.lower().endswith(".png"))
    return {"status": f"{len(names)} PNG file(s)", "files": names}
```'''


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def mk(kind, label, x, y, **props):
    s = new_shape(kind, x, y)
    s.label = label
    s.props.update(props)
    return s


@pytest.fixture
def tab(qapp, tmp_path):
    vault = tmp_path / "vault"
    folder = mk("file_picker", "Image folder", 40, 40, mode="folder")
    status = mk("label", "", 40, 100)
    status.port = {"name": "status"}
    files = mk("listbox", "Files", 40, 160)
    button = mk("button", "Count images", 400, 40)
    button.note = "count the PNG files in the image folder and list them"
    shapes = [folder, status, files, button]
    dp.create("demo", "linked", vault, toolkit="qt")
    dp.save("demo", shapes, vault)
    pdir = gui_projects.project_path("demo", vault)
    assert dp.generate("demo", shapes, pdir, vault).ok
    accepted = []

    def review(r):
        accepted.append(r)
        return tab_accepts[0]

    tab_accepts = [True]
    view = DesignerTab(actions=DesignerActions(vault),
                       ask_choice=lambda *_a: "demo", review_code=review)
    view.on_open()
    view.reviews, view.accepts = accepted, tab_accepts
    view.button_id = button.id
    yield view
    qapp.processEvents()
    view.deleteLater()
    qapp.processEvents()


def select(tab, sid):
    tab.canvas.scene.selection = [sid]
    tab._show_selection()


def wait_idle(tab, qapp, limit=60.0):
    t0 = time.perf_counter()
    while tab._busy and time.perf_counter() - t0 < limit:
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()
    assert not tab._busy, "the code writer did not finish"


def shape(tab, sid):
    return next(s for s in tab.canvas.scene.shapes if s.id == sid)


def logged(tab) -> str:
    return tab.log_view.toPlainText()


def test_the_writer_is_shown_prefilled_from_the_note(tab):
    select(tab, tab.button_id)
    assert not tab.wiring.isHidden()
    assert tab.wiring.instruction.toPlainText().startswith(
        "count the PNG files")
    assert tab.wiring.write_mode.currentData() == "function"
    assert tab.wiring.write_button.isEnabled()
    assert not tab.wiring.stop_button.isEnabled()


def test_a_request_carries_the_rows_as_the_signature(tab):
    select(tab, tab.button_id)
    tab.wiring.input_pick.setCurrentText("image_folder")
    tab.wiring._add_input()
    tab.wiring._add_output("status", "status")
    tab.wiring._add_output("files", "")
    req = tab.wiring.write_request()
    assert req["inputs"] == ["image_folder"]
    assert req["outputs"] == {"status": "status", "files": ""}
    assert req["mode"] == "function" and req["n_best"] is None


def test_accept_writes_logic_and_wires_the_widget_undoably(tab, qapp):
    tab.actions.code_model = lambda prompt, **kw: GOOD
    select(tab, tab.button_id)
    tab.wiring.input_pick.setCurrentText("image_folder")
    tab.wiring._add_input()
    tab.wiring._add_output("status", "status")
    tab.wiring._add_output("files", "files")
    tab.wiring._write()
    assert tab._busy and tab.wiring.stop_button.isEnabled()
    wait_idle(tab, qapp)
    assert len(tab.reviews) == 1 and tab.reviews[0].ok
    pdir = tab.actions.project_dir("demo")
    assert "def count_images(" in (pdir / "logic.py").read_text(
        encoding="utf-8")
    script = shape(tab, tab.button_id).script
    assert script["module"] == "logic" and script["function"] == \
        "count_images"
    assert tab.canvas.scene.dirty
    assert "wrote count_images() into logic.py" in logged(tab)
    # The Wiring group now shows the project's own module as the link.
    assert tab.wiring.module.currentText() == "logic"
    assert tab.wiring.current_problems() == []
    # One Undo takes the wiring back (logic.py stays — it is a file).
    tab.canvas._obey(tab.canvas.scene.undo_once())
    assert not shape(tab, tab.button_id).script
    # Generate after re-wiring writes the linked handler.
    tab.canvas._obey(tab.canvas.scene.redo_once())
    tab.on_generate()
    wait_idle(tab, qapp)
    assert "from logic import count_images" in (pdir / "handlers.py"
                                                ).read_text(encoding="utf-8")


def test_a_declined_review_writes_nothing(tab, qapp):
    tab.actions.code_model = lambda prompt, **kw: GOOD
    tab.accepts[0] = False
    select(tab, tab.button_id)
    tab.wiring._add_output("status", "status")
    tab.wiring._write()
    wait_idle(tab, qapp)
    assert tab.reviews and tab.reviews[0].ok
    assert not (tab.actions.project_dir("demo") / "logic.py").exists()
    assert not shape(tab, tab.button_id).script
    assert "not written" in logged(tab)


def test_the_real_host_dialog_says_no_when_dialogs_are_off(tab, qapp):
    """build_designer's review is ask_accept: under COUNCIL_NO_DIALOGS a
    model's code is never accepted by nobody."""
    os.environ["COUNCIL_NO_DIALOGS"] = "1"
    assert wiring_widgets.ask_accept(object()) is False


def test_the_review_dialog_shows_the_diff_and_report(tab, qapp):
    from council_core import designer_codebehind as dc
    plan = tab.actions.plan_code("demo", tab.canvas.scene.export(),
                                 tab.button_id,
                                 {"instruction": "count PNGs",
                                  "inputs": ["image_folder"],
                                  "outputs": {"status": "status"}})
    review = dc.run(plan, model_call=lambda p, **k: GOOD.replace(
        ', "files": names', ""))
    dialog = wiring_widgets.CodeReviewDialog(review)
    assert "+def count_images(" in dialog.diff.toPlainText()
    assert "smoke run: ok" in dialog.report.toPlainText()
    assert dialog.accept_button.isEnabled()
    dialog.deleteLater()


def test_a_failed_job_is_reported_and_offers_nothing(tab, qapp):
    tab.actions.code_model = lambda prompt, **kw: "I cannot."
    select(tab, tab.button_id)
    tab.wiring._add_output("status", "status")
    tab.wiring._write()
    wait_idle(tab, qapp)
    assert tab.reviews == []
    assert "NOT OFFERED" in logged(tab)
    assert "Not offered" in tab.wiring.write_status.text()


def test_stop_ends_the_job(tab, qapp):
    started = []

    def slow(prompt, should_stop=None, **kw):
        started.append(1)
        t0 = time.perf_counter()
        while not (should_stop and should_stop()) and \
                time.perf_counter() - t0 < 10:
            time.sleep(0.01)
        return "nothing"

    tab.actions.code_model = slow
    select(tab, tab.button_id)
    tab.wiring._add_output("status", "status")
    tab.wiring._write()
    t0 = time.perf_counter()
    while not started and time.perf_counter() - t0 < 10:
        qapp.processEvents()
        time.sleep(0.01)
    tab.wiring.stop_button.click()
    wait_idle(tab, qapp, limit=20)
    assert time.perf_counter() - t0 < 10
    assert "stopping the code writer" in logged(tab)
    assert len(started) == 1
    assert tab.wiring.write_button.isEnabled()


def test_an_empty_instruction_never_reaches_the_worker(tab):
    select(tab, tab.button_id)
    tab.wiring.instruction.setPlainText("")
    tab.wiring._write()
    assert not tab._busy
    assert "Say what it should do" in tab.wiring.write_status.text()


def test_model_text_in_the_status_line_is_never_rich_text(tab):
    """Review: progress lines quote the model's reply (a SyntaxError line),
    and the status QLabel was AutoText — `<img src="file:///...">` in a
    reply was rendered as HTML, Qt reading a local file to show it."""
    from PySide6.QtCore import Qt
    select(tab, tab.button_id)
    tab.wiring.set_writing(True, '  shape — line 1: invalid syntax: '
                                 '<img src="file:///C:/x.png">')
    assert tab.wiring.write_status.textFormat() == Qt.TextFormat.PlainText


def test_the_worker_touches_no_widget():
    from tests.source_checks import widget_touches_in_worker
    source = (ROOT / "council_qt" / "tabs" / "designer.py").read_text(
        encoding="utf-8")
    assert widget_touches_in_worker(
        source, "on_write_code",
        ["wiring", "canvas", "log_view", "status", "inspector"]) == []


def test_selection_stays_fast_with_the_writer_in_the_group(tab, qapp):
    """The group gained a section; selecting a button must stay cheap — the
    canvas refreshes it on every selection."""
    select(tab, tab.button_id)
    t0 = time.perf_counter()
    for _ in range(20):
        select(tab, tab.button_id)
    per = (time.perf_counter() - t0) / 20
    assert per < 0.05, f"{per * 1000:.1f} ms per selection"
