"""
The Designer's guided on-ramp.

An ON-RAMP, not a second designer: everything it produces is a plain Shape
list that lands on the same canvas, with the same snap engine and the same undo
stack. So what is worth testing is what it DECIDES — the step order, what makes
a step invalid, and how five screens of strings become a wireframe.

And the thing it must never do: write anything. Cancelling leaves no half-made
project behind, and a wizard that created as it went would strand one on every
abandoned attempt.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from council_core import wizard as core  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def answers(**kw):
    base = core.Answers(name="Camera")
    for key, value in kw.items():
        setattr(base, key, value)
    return base


def test_the_module_imports_no_toolkit():
    source = (ROOT / "council_core" / "wizard.py").read_text(encoding="utf-8")
    for toolkit in ("tkinter", "PySide6", "PyQt5"):
        assert toolkit not in source


# ============================================================
# Validation — per step, not on Finish
# ============================================================

def test_a_project_needs_a_name():
    assert core.validate("basics", answers(name="")) == "Give the project a name."
    assert core.validate("basics", answers(name="   "))


def test_a_name_already_taken_is_refused():
    """The host would fail on create anyway — after five screens of answers
    the user would have to retype."""
    assert "already a project" in core.validate(
        "basics", answers(name="Camera"), existing=["Camera"])


def test_the_name_check_ignores_case():
    """Two projects differing only in case are two directories on Linux and
    one on Windows, and the user meant one either way."""
    assert core.validate("basics", answers(name="Camera"),
                         existing=["camera"])


def test_an_unusable_window_is_refused():
    """The difference between a wizard that stops you and one that hands you a
    broken app and lets you find out later."""
    assert "will not be usable" in core.validate(
        "basics", answers(min_w="100"))
    assert "will not be usable" in core.validate(
        "basics", answers(min_h="10"))


def test_a_non_numeric_size_is_refused_not_defaulted():
    """Unlike the property panel, this runs ONCE and creates a project. A
    silent 0 here is a window nobody can use and nobody was warned about."""
    assert core.validate("basics", answers(min_w="wide"))


def test_a_negative_field_count_is_refused():
    assert core.validate("contents", answers(template="form", fields="-1"))


def test_zero_fields_is_allowed():
    """A form with no fields is a legitimate starting point — buttons only."""
    assert core.validate("contents", answers(template="form", fields="0")) == ""


def test_the_field_count_only_matters_for_a_form():
    assert core.validate("contents",
                         answers(template="blank", fields="nonsense")) == ""


def test_a_negative_reserve_count_is_refused():
    assert core.validate("reserve", answers(reserve="-2"))


def test_a_valid_answer_set_passes_every_step():
    good = answers()
    for step in core.STEPS:
        assert core.validate(step, good) == "", step


def test_there_are_five_steps_in_the_documented_order():
    assert core.STEPS == ("basics", "layout", "contents", "reserve", "review")


# ============================================================
# What the answers mean
# ============================================================

def test_the_window_title_falls_back_to_the_project_name():
    """A window called "Untitled" when the user named the project is a detail
    nobody remembers to go back and fix."""
    assert core.to_result(answers(title="")).title == "Camera"


def test_an_explicit_title_wins():
    assert core.to_result(answers(title="Grab Station")).title == "Grab Station"


def test_the_minimum_size_is_clamped_rather_than_rejected_at_finish():
    """validate already refused anything smaller on the way past. A Finish
    that threw on a value the user can no longer see would be unfixable."""
    assert core.to_result(answers(min_w="10")).min_w == core.MIN_USABLE


def test_a_form_gets_the_fields_that_were_asked_for():
    options = core.template_options(answers(template="form", fields="4"))
    assert options["n_fields"] == 4


def test_empty_labels_let_the_template_name_them():
    """None, not []. An empty list produces a form of nameless fields."""
    assert core.template_options(answers(template="form",
                                         labels=""))["labels"] is None


def test_given_labels_are_split_and_trimmed():
    options = core.template_options(
        answers(template="form", labels=" Exposure , Gain ,, ROI "))
    assert options["labels"] == ["Exposure", "Gain", "ROI"]


def test_a_form_with_no_buttons_still_gets_one():
    """A form you cannot submit is not a starting point."""
    assert core.template_options(answers(template="form",
                                         buttons=""))["buttons"]


def test_each_template_gets_only_its_own_options():
    """A keyword the builder does not take is a TypeError at Finish."""
    assert "main_kind" not in core.template_options(answers(template="form"))
    assert "n_fields" not in core.template_options(
        answers(template="toolbar_main_status"))
    assert core.template_options(answers(template="blank")) == {}


# ============================================================
# The shapes it produces
# ============================================================

def test_it_produces_shapes_for_every_template():
    for template in ("form", "toolbar_main_status", "split_view", "blank"):
        shapes = core.shapes_for(answers(template=template))
        assert isinstance(shapes, list)


def test_reserved_spaces_are_added_on_top():
    """Appended AFTER the template so they take the higher z, which keeps the
    whole list in one increasing z order."""
    plain = core.shapes_for(answers(template="form", reserve="0"))
    held = core.shapes_for(answers(template="form", reserve="3"))
    assert len(held) == len(plain) + 3
    assert [s.z for s in held] == sorted(s.z for s in held)


def test_reserved_spaces_are_not_shrunk_to_nothing():
    """A frame smaller than a handle is not a space you can fill in later."""
    shapes = core.shapes_for(answers(reserve="1", reserve_w="1",
                                     reserve_h="1"))
    reserved = shapes[-1]
    assert reserved.w >= core.MIN_RESERVED
    assert reserved.h >= core.MIN_RESERVED


def test_the_summary_says_what_finish_will_make():
    lines = "\n".join(core.summary(answers(template="form", reserve="2")))
    assert "Camera" in lines
    assert "shape(s)" in lines
    assert "reserved" in lines


def test_the_summary_mentions_no_model_call_for_reserved_space():
    """That is the whole reason to reserve rather than describe: it is
    generated with no model call."""
    assert "no model call" in "\n".join(core.summary(answers(reserve="1")))


def test_the_summary_leaves_reserved_out_when_there_is_none():
    assert "reserved" not in "\n".join(core.summary(answers(reserve="0")))


# ============================================================
# The Qt dialog
# ============================================================

pytest.importorskip("PySide6", reason="the wizard dialog needs PySide6")

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from council_qt.wizard import GuiWizard, open_wizard  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture
def wizard(qapp):
    dialog = GuiWizard(existing=["taken"])
    yield dialog
    dialog.deleteLater()


def test_it_has_a_page_per_step(wizard):
    assert wizard.pages.count() == len(core.STEPS)


def test_it_starts_on_the_first_step(wizard):
    assert wizard._step == 0
    assert "1 of 5" in wizard.heading.text()


def test_back_is_unavailable_on_the_first_step(wizard):
    assert not wizard.back_btn.isEnabled()


def test_the_last_step_says_finish(wizard):
    wizard._fields["name"].setText("Camera")
    for _ in range(len(core.STEPS) - 1):
        wizard.next()
    assert "Finish" in wizard.next_btn.text()


def test_a_failed_step_does_not_advance(wizard):
    wizard._fields["name"].setText("")
    wizard.next()
    assert wizard._step == 0
    assert wizard.error.text()


def test_the_reason_is_shown_on_the_screen_that_caused_it(wizard):
    """Not on Finish. An error about a name typed four screens ago is an error
    you have to go looking for."""
    wizard._fields["name"].setText("taken")
    wizard.next()
    assert "already a project" in wizard.error.text()
    assert wizard._step == 0


def test_back_never_validates(wizard):
    """Going back must not be blocked by the thing that is wrong on THIS page.

    Driven from "contents" with a field count that fails, because that is the
    only place the difference shows: the first version went back from "layout",
    which has no rules at all, so a Back that validated behaved identically and
    the mutation survived.
    """
    wizard._fields["name"].setText("Camera")
    wizard.next()                                 # basics -> layout
    wizard.next()                                 # layout -> contents
    assert core.STEPS[wizard._step] == "contents"
    wizard._fields["fields"].setText("-3")
    assert core.validate("contents", wizard.answers), "premise gone"
    wizard.back()
    assert core.STEPS[wizard._step] == "layout", (
        "Back refused to leave a page because that page was invalid")


def test_typing_reaches_the_answers(wizard):
    wizard._fields["name"].setText("Grab Station")
    assert wizard.answers.name == "Grab Station"


def test_choosing_reaches_the_answers(wizard):
    wizard._fields["mode"].setCurrentText("standalone")
    assert wizard.answers.mode == "standalone"


def test_the_review_page_shows_the_summary(wizard):
    wizard._fields["name"].setText("Camera")
    for _ in range(len(core.STEPS) - 1):
        wizard.next()
    assert "Camera" in wizard.review.text()
    assert "shape(s)" in wizard.review.text()


def test_finishing_reports_the_result(wizard, qapp):
    got = []
    wizard.finished_with.connect(got.append)
    wizard._fields["name"].setText("Camera")
    for _ in range(len(core.STEPS)):
        wizard.next()
    qapp.processEvents()
    assert got and got[0].name == "Camera"
    assert got[0].shapes


def test_cancelling_reports_nothing(wizard, qapp):
    got = []
    wizard.finished_with.connect(got.append)
    wizard.reject()
    qapp.processEvents()
    assert got == []


def test_the_wizard_writes_nothing(wizard, tmp_path, monkeypatch):
    """It hands back a layout and the HOST creates the project, so cancelling
    leaves nothing behind. A wizard that created as it went would strand a
    project on every abandoned attempt."""
    import gui_projects
    made = []
    monkeypatch.setattr(gui_projects, "create",
                        lambda *a, **k: made.append(a))
    wizard._fields["name"].setText("Camera")
    for _ in range(len(core.STEPS)):
        wizard.next()
    assert made == []


def test_the_dialog_never_touches_the_filesystem():
    from tests.source_checks import code_of
    source = (ROOT / "council_qt" / "wizard.py").read_text(encoding="utf-8")
    for name in ("finish", "next", "back"):
        body = code_of(source, name)
        for forbidden in ("open(", "write_text", "mkdir", "save_project",
                          "gui_projects"):
            assert forbidden not in body, f"{name} writes something"


def test_open_wizard_connects_the_callbacks(qapp):
    got, logged = [], []
    dialog = open_wizard(on_done=got.append, log=logged.append,
                         existing=[])
    dialog._fields["name"].setText("Camera")
    for _ in range(len(core.STEPS)):
        dialog.next()
    qapp.processEvents()
    assert got and got[0].name == "Camera"
    assert any("wizard:" in line for line in logged)
    dialog.deleteLater()


def test_it_is_modal(qapp):
    """It is a five-step question. Letting the user edit the canvas behind it
    means answering about a design that is changing underneath."""
    dialog = GuiWizard()
    assert dialog.isModal()
    dialog.deleteLater()
