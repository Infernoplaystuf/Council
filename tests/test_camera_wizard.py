"""
Tests for the camera setup wizard and how a generated capture app runs it.

Offscreen throughout: the wizard is constructed and driven page by page but
never shown, and run_wizard is given an `execute` so no modal loop starts.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import frame_camera
from council_core import camera_setup as cs
from council_qt.widgets import camera_wizard as cw
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

PYTHON = r"C:\envs\pylon\python.exe"


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def readiness(choice, *checks):
    return cs.Readiness(choice, list(checks))


def ok_checker(choice):
    return readiness(choice, cs.Check("pypylon installed", True, "12.3"))


def settle(wizard, seconds=5.0):
    """Pump until the Check page has collected its result."""
    deadline = time.monotonic() + seconds
    while wizard.check.readiness is None and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)
    assert wizard.check.readiness is not None, "the check never came back"


def wizard_at(qapp, choice, checker=ok_checker, current=None):
    w = cw.CameraSetupWizard(checker=checker, python=PYTHON, current=current)
    w.restart()
    if choice:
        w.choose.buttons[choice].setChecked(True)
    return w


# ======================================================================
# Choose
# ======================================================================
def test_nothing_is_chosen_until_the_user_chooses(qapp):
    w = wizard_at(qapp, None)
    assert w.choice() is None
    assert w.choose.isComplete() is False


def test_choosing_a_camera_lets_the_user_continue(qapp):
    w = wizard_at(qapp, "basler")
    assert w.choice() == "basler"
    assert w.choose.isComplete() is True


def test_a_saved_choice_is_preselected_when_the_wizard_is_rerun(qapp):
    w = wizard_at(qapp, None, current="prophesee")
    assert w.choice() == "prophesee"


def test_every_camera_is_offered(qapp):
    w = wizard_at(qapp, None)
    assert set(w.choose.buttons) == set(cs.CHOICES)


# ======================================================================
# Install
# ======================================================================
def test_the_install_page_shows_the_chosen_cameras_steps(qapp):
    w = wizard_at(qapp, "basler")
    w.next()
    text = w.install.steps.text()
    assert "CoaXPress" in text
    assert "baslerweb.com" in text
    assert "Metavision" not in text


def test_going_back_and_choosing_again_replaces_the_steps(qapp):
    """Back and a different choice must not leave the old steps up."""
    w = wizard_at(qapp, "basler")
    w.next()
    w.back()
    w.choose.buttons["prophesee"].setChecked(True)
    w.next()
    text = w.install.steps.text()
    assert "Metavision" in text and "CoaXPress" not in text


def test_the_install_command_names_this_apps_python(qapp):
    w = wizard_at(qapp, "basler")
    w.next()
    assert PYTHON in w.install.steps.text()


def test_copy_puts_the_install_command_on_the_clipboard(qapp):
    w = wizard_at(qapp, "basler")
    w.next()
    w.install.copy_commands()
    assert QGuiApplication.clipboard().text() == \
        f'"{PYTHON}" -m pip install pypylon'


def test_there_is_no_copy_button_when_there_is_nothing_to_copy(
        qapp, monkeypatch):
    bare = cs.Guide("basler", "Bare", "", (cs.Step("just read this"),))
    monkeypatch.setattr(cs, "guide", lambda choice, **kw: bare)
    w = wizard_at(qapp, "basler")
    w.next()
    assert w.install.copy_btn.isHidden() is True


def test_the_evk4_driver_commands_copy_one_per_line(qapp):
    """Pasted into an administrator prompt, each must be its own command."""
    w = wizard_at(qapp, "prophesee")
    w.next()
    w.install.copy_commands()
    lines = QGuiApplication.clipboard().text().splitlines()
    assert lines == list(cs.WDI_COMMANDS)


def test_a_multi_line_command_is_shown_on_separate_lines():
    """Rich text folds newlines into spaces — three commands would read as
    one that fails."""
    g = cs.Guide("x", "X", "", (cs.Step("run", command="one\ntwo"),))
    assert "one<br>two" in cw.render_steps(g)


def test_step_text_is_escaped_not_interpreted_as_markup():
    g = cs.Guide("x", "X", "", (cs.Step("use <b>this</b> & that"),))
    rendered = cw.render_steps(g)
    assert "&lt;b&gt;this&lt;/b&gt; &amp; that" in rendered
    assert "<b>this</b>" not in rendered


# ======================================================================
# Check
# ======================================================================
def test_the_check_runs_off_the_ui_thread(qapp):
    """pylon enumeration takes seconds; the wizard must not freeze on it."""
    seen = {}

    def checker(choice):
        seen["thread"] = threading.current_thread().name
        return ok_checker(choice)

    w = wizard_at(qapp, "basler", checker=checker)
    w.next()
    w.next()
    settle(w)
    assert seen["thread"] != threading.main_thread().name


def test_results_are_shown_with_a_symbol_and_a_word(qapp):
    """Meaning must survive a dark theme or a colour-blind reader."""
    def checker(choice):
        return readiness(choice,
                         cs.Check("pypylon installed", True, "12.3"),
                         cs.Check("CoaXPress support", None, "USB, GigE",
                                  "install CXP (step 2)"),
                         cs.Check("Camera connected", False, "boom",
                                  "reinstall"))
    w = wizard_at(qapp, "basler", checker=checker)
    w.next()
    w.next()
    settle(w)
    text = w.check.results.text()
    for symbol, word in (("✓", "Done"), ("⚠", "Check"), ("✗", "Missing")):
        assert symbol in text and word in text
    assert "install CXP (step 2)" in text


def test_a_passing_check_shows_no_fix(qapp):
    rendered = cw.render_checks(readiness(
        "basler", cs.Check("pypylon installed", True, "12.3", "SHOULD NOT SHOW")))
    assert "SHOULD NOT SHOW" not in rendered


def test_a_checker_that_crashes_is_reported_not_raised(qapp):
    """A checklist that crashes is no checklist at all."""
    def checker(choice):
        raise RuntimeError("the SDK exploded")

    w = wizard_at(qapp, "basler", checker=checker)
    w.next()
    w.next()
    settle(w)
    assert w.check.readiness.checks[0].ok is False
    assert "the SDK exploded" in w.check.results.text()


def test_check_again_is_disabled_while_a_check_runs(qapp):
    release = threading.Event()

    def slow(choice):
        release.wait(5)
        return ok_checker(choice)

    w = wizard_at(qapp, "basler", checker=slow)
    w.next()
    w.next()
    try:
        assert w.check.again_btn.isEnabled() is False
        assert w.check.checking
    finally:
        release.set()
    settle(w)
    assert w.check.again_btn.isEnabled() is True


def test_finishing_is_never_blocked_by_a_failed_check(qapp):
    """Someone setting up a machine often finishes before unpacking the camera."""
    def checker(choice):
        return readiness(choice, cs.Check("pypylon installed", False, "no"))

    w = wizard_at(qapp, "basler", checker=checker)
    w.next()
    w.next()
    settle(w)
    assert w.check.isComplete() is True


# ======================================================================
# run_wizard — the save
# ======================================================================
def test_finishing_saves_the_choice(qapp, tmp_path):
    path = cs.setup_path(tmp_path)

    def finish(wizard):
        wizard.choose.buttons["prophesee"].setChecked(True)
        return 1

    assert cw.run_wizard(None, path, checker=ok_checker,
                         execute=finish) == "prophesee"
    assert cs.load_choice(path) == "prophesee"


def test_cancelling_saves_nothing_so_it_is_asked_again(qapp, tmp_path):
    path = cs.setup_path(tmp_path)

    def cancel(wizard):
        wizard.choose.buttons["basler"].setChecked(True)
        return 0

    assert cw.run_wizard(None, path, checker=ok_checker,
                         execute=cancel) is None
    assert not path.exists()


def test_rerunning_starts_from_the_saved_answer(qapp, tmp_path):
    path = cs.setup_path(tmp_path)
    cs.save_choice(path, "basler")
    seen = {}

    def peek(wizard):
        seen["preselected"] = wizard.choice()
        return 0

    cw.run_wizard(None, path, checker=ok_checker, execute=peek)
    assert seen["preselected"] == "basler"


# ======================================================================
# frame_camera — how the app uses the answer
# ======================================================================
@pytest.fixture
def clean_camera():
    frame_camera.disconnect()
    kept = frame_camera._LIVE.setup_path
    frame_camera._LIVE.found = None
    yield
    frame_camera.disconnect()
    frame_camera._LIVE.setup_path = kept
    frame_camera._LIVE.found = None


def test_an_app_set_up_for_basler_does_not_report_on_metavision(
        clean_camera, tmp_path):
    """A note about an SDK the user will never need is noise."""
    path = cs.setup_path(tmp_path)
    cs.save_choice(path, "basler")
    frame_camera._LIVE.setup_path = path
    notes = " ".join(frame_camera.list_cameras()["notes"]).lower()
    assert "prophesee" not in notes and "metavision" not in notes


def test_an_app_set_up_for_prophesee_does_not_report_on_pylon(
        clean_camera, tmp_path):
    path = cs.setup_path(tmp_path)
    cs.save_choice(path, "prophesee")
    frame_camera._LIVE.setup_path = path
    notes = " ".join(frame_camera.list_cameras()["notes"]).lower()
    assert "basler" not in notes and "pypylon" not in notes


def test_the_simulated_cameras_stay_available_whatever_was_chosen(
        clean_camera, tmp_path):
    """They are how the app is explored before the hardware arrives."""
    path = cs.setup_path(tmp_path)
    for choice in cs.CHOICES:
        cs.save_choice(path, choice)
        frame_camera._LIVE.setup_path = path
        rows = frame_camera.list_cameras()["rows"]
        assert any("simulated" in r for r in rows), choice


def test_an_app_that_was_never_set_up_searches_everything(
        clean_camera, tmp_path):
    frame_camera._LIVE.setup_path = cs.setup_path(tmp_path)
    notes = " ".join(frame_camera.list_cameras()["notes"]).lower()
    assert "pypylon" in notes or "basler" in notes
    assert "prophesee" in notes or "metavision" in notes


def test_setup_with_dialogs_disabled_says_so_and_still_lists(
        clean_camera, tmp_path, monkeypatch):
    monkeypatch.setenv("COUNCIL_NO_DIALOGS", "1")
    frame_camera._LIVE.setup_path = cs.setup_path(tmp_path)
    out = frame_camera.setup()
    assert "skipped" in out["summary"]
    assert out["rows"], "the camera list was not refreshed"


def test_setup_saves_and_reports_the_choice(clean_camera, tmp_path,
                                            monkeypatch):
    monkeypatch.delenv("COUNCIL_NO_DIALOGS", raising=False)
    path = cs.setup_path(tmp_path)
    frame_camera._LIVE.setup_path = path
    monkeypatch.setattr(cw, "run_wizard",
                        lambda parent, p, **kw: (cs.save_choice(p, "basler")
                                                 or "basler"))
    out = frame_camera.setup()
    assert "Set up for Basler" in out["summary"]
    assert frame_camera.current_choice() == "basler"


def test_a_cancelled_setup_says_nothing_changed(clean_camera, tmp_path,
                                                monkeypatch):
    monkeypatch.delenv("COUNCIL_NO_DIALOGS", raising=False)
    frame_camera._LIVE.setup_path = cs.setup_path(tmp_path)
    monkeypatch.setattr(cw, "run_wizard", lambda parent, p, **kw: None)
    assert "nothing changed" in frame_camera.setup()["summary"]
