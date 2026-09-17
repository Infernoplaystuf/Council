"""
The Lens — parallel critique, and the three defects it was carrying.

The Lens is the smallest complete tab in the app, which makes it the honest
test of whether the port's pattern generalises past the two big tabs. The
interesting content is that a 26-toolkit-line tab still had three real defects
in it, all silent.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

from council_core import lens as lens_core  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class FakeModel:
    def __init__(self, reply="a critique", raises=None, delay=0.0):
        self.reply, self.raises, self.delay = reply, raises, delay
        self.calls = []

    def respond(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        if self.delay:
            time.sleep(self.delay)
        if self.raises:
            raise self.raises
        return self.reply


class Models:
    def __init__(self, **slots):
        for role, _default in lens_core.LENS_ROLES:
            setattr(self, role, None)
        for name, value in slots.items():
            setattr(self, name, value)


# ============================================================
# The shared half
# ============================================================

def test_the_lens_module_imports_no_toolkit():
    source = (ROOT / "council_core" / "lens.py").read_text(encoding="utf-8")
    for toolkit in ("tkinter", "PySide6", "PyQt5", "messagebox"):
        assert toolkit not in source


def test_the_role_order_is_the_layout_order():
    """Users find a checkbox by position, so the order is part of the product
    and not an implementation detail to tidy."""
    roles = [role for role, _ in lens_core.LENS_ROLES]
    assert roles[:4] == ["writer", "coder", "sage", "peasant"]
    assert roles[-1] == "musician"


def test_the_defaults_match_the_tk_tab():
    """No test anywhere looks at a checkbox's initial state, which is exactly
    why these have to be described in one place."""
    defaults = dict(lens_core.LENS_ROLES)
    assert defaults["writer"] and defaults["coder"] and defaults["content"]
    assert not defaults["artist"] and not defaults["intern"]
    assert not defaults["skeptic"] and not defaults["musician"]


# -- defect 1: a checkbox that cannot ever work -------------------------------

def test_a_role_with_no_model_is_reported_as_unavailable():
    """The Tk tab offers `musician` and there is no musician personality
    anywhere — not in REQUIRED_ROLES, not in OPTIONAL_ROLES, not in
    _unpack_personalities. Ticking it answers "(Role not loaded)" every single
    time."""
    available = lens_core.available_roles(Models(writer=FakeModel()))
    assert available["writer"] is True
    assert available["musician"] is False


def test_the_qt_tab_disables_a_role_it_cannot_run(qapp_lens):
    tab, _ = qapp_lens
    assert not tab._boxes["musician"].isEnabled()
    assert not tab._boxes["musician"].isChecked()
    assert "nothing to say" in tab._boxes["musician"].toolTip()


def test_a_role_that_is_available_is_offered(qapp_lens):
    tab, _ = qapp_lens
    assert tab._boxes["writer"].isEnabled()
    assert tab._boxes["writer"].isChecked()


# -- defect 2: the done count -------------------------------------------------

def test_the_count_is_of_roles_that_answered_not_roles_asked():
    """The Tk status line reports len(selected_roles), so three roles that
    errored and one that answered still reads "Done — 4 roles responded"."""
    models = Models(writer=FakeModel("good"),
                    coder=FakeModel(raises=RuntimeError("no weights")))
    result = lens_core.run_lens(models, "review this", ["writer", "coder"])
    assert result.ok
    assert result.answered == 1
    assert result.failed == ["coder"]
    assert "1 of 2" in result.message
    assert "coder" in result.message


def test_a_run_where_nobody_answers_says_so():
    models = Models(writer=FakeModel(raises=RuntimeError("boom")))
    result = lens_core.run_lens(models, "review this", ["writer"])
    assert result.ok
    assert result.answered == 0
    assert "No role could answer" in result.message


def test_a_clean_run_does_not_mention_failures():
    models = Models(writer=FakeModel(), coder=FakeModel())
    result = lens_core.run_lens(models, "review this", ["writer", "coder"])
    assert result.answered == 2
    assert "could not" not in result.message


def test_a_missing_model_counts_as_a_failure_not_an_answer():
    """"(Role not loaded)" is not a critique."""
    result = lens_core.run_lens(Models(), "review this", ["writer"])
    assert result.results["writer"] == "(Role not loaded)"
    assert result.answered == 0


# -- defect 3: silence on an empty box ----------------------------------------

def test_an_empty_box_is_refused_with_words():
    """_lens_run returns with no message at all, so the button appears dead."""
    assert "Paste something" in lens_core.check_request("", ["writer"])
    assert "Paste something" in lens_core.check_request("   ", ["writer"])


def test_no_roles_ticked_is_refused():
    assert "at least one role" in lens_core.check_request("content", [])


def test_a_valid_request_has_no_complaint():
    assert lens_core.check_request("content", ["writer"]) is None


def test_the_qt_tab_says_why_it_did_nothing(qapp_lens):
    tab, app = qapp_lens
    tab.input.setPlainText("")
    tab.on_run()
    assert "Paste something" in tab.status.text()


# -- the truncation nobody mentions -------------------------------------------

def test_only_the_first_three_thousand_characters_are_reviewed():
    model = FakeModel()
    lens_core.run_lens(Models(writer=model), "x" * 5000, ["writer"])
    prompt = model.calls[0][0]
    assert prompt.count("x") == lens_core.CONTENT_LIMIT


def test_long_content_says_that_it_is_being_truncated():
    """The Tk tab slices silently, so a user who pastes a long document
    wonders why the back half was ignored."""
    note = lens_core.truncation_note("x" * 5000)
    assert "3,000" in note and "5,000" in note
    assert lens_core.truncation_note("short") == ""


def test_the_qt_tab_shows_the_truncation_note(qapp_lens):
    tab, app = qapp_lens
    tab.input.setPlainText("x" * 4000)
    app.processEvents()
    assert tab.truncation.isVisibleTo(tab)
    assert "3,000" in tab.truncation.text()


# -- the prompt and the shape of the output -----------------------------------

def test_every_role_gets_the_identical_prompt():
    """The role's own system prompt is what makes the critique different.
    Role-specific wording here would quietly turn eleven independent readings
    into eleven nudged ones."""
    writer, coder = FakeModel(), FakeModel()
    lens_core.run_lens(Models(writer=writer, coder=coder), "content",
                       ["writer", "coder"])
    assert writer.calls[0][0] == coder.calls[0][0]


def test_the_prompt_tells_roles_not_to_defer_to_each_other():
    """The whole point of a lens is that the roles do not see each other."""
    prompt = lens_core.build_prompt("anything")
    assert "Do NOT synthesise" in prompt
    assert "defer to other roles" in prompt


def test_a_critique_is_separated_before_its_text():
    """So a long critique never runs into the next role's heading."""
    block = lens_core.format_critique("writer", "some words")
    assert block.index("WRITER") < block.index("some words")
    assert block.count("─" * 40) == 2


def test_results_arrive_as_they_land():
    """On local weights, the first critique at five seconds versus all of them
    at sixty is the difference between a live feature and a hung one."""
    seen = []
    models = Models(writer=FakeModel(), coder=FakeModel())
    lens_core.run_lens(models, "content", ["writer", "coder"],
                       on_result=lambda role, text: seen.append(role))
    assert sorted(seen) == ["coder", "writer"]


def test_one_role_raising_does_not_lose_the_others():
    """Running eleven models and losing all of them because one raised would
    be a worse trade than any error handling buys."""
    models = Models(writer=FakeModel("kept"),
                    coder=FakeModel(raises=ValueError("gone")))
    result = lens_core.run_lens(models, "content", ["writer", "coder"])
    assert result.results["writer"] == "kept"
    assert "gone" in result.results["coder"]


# ============================================================
# The Qt view
# ============================================================

pytest.importorskip("PySide6", reason="the Qt tab needs PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture
def qapp_lens(qapp):
    """A Lens tab whose actions report a known set of available roles."""
    from council_qt.tabs.lens import LensActions, LensTab

    class Actions(LensActions):
        def __init__(self):
            super().__init__(models=Models(writer=FakeModel(),
                                           coder=FakeModel()))

        def models(self):
            return self._models, ""

    tab = LensTab(actions=Actions())
    yield tab, qapp
    tab.deleteLater()


def test_the_selected_roles_come_back_in_layout_order(qapp_lens):
    tab, _ = qapp_lens
    tab._boxes["coder"].setChecked(True)
    tab._boxes["writer"].setChecked(True)
    assert tab.selected_roles() == ["writer", "coder"]


def test_clearing_empties_the_boxes_and_the_status(qapp_lens):
    tab, _ = qapp_lens
    tab.input.setPlainText("something")
    tab.output.setPlainText("old critique")
    tab.status.setText("Done — 2 roles responded.")
    tab.on_clear()
    assert tab.input.toPlainText() == ""
    assert tab.output.toPlainText() == ""
    assert tab.status.text() == ""


def test_a_second_run_while_one_is_going_is_refused(qapp_lens):
    tab, _ = qapp_lens
    tab._running = True
    tab.input.setPlainText("content")
    tab.on_run()
    assert "already going" in tab.status.text()


def test_the_tab_uses_the_shared_module_for_every_decision():
    """Nothing about roles, prompts, truncation or counting may be decided in
    the view — that is how the Tk tab and this one would drift."""
    source = (ROOT / "council_qt" / "tabs" / "lens.py").read_text(
        encoding="utf-8")
    for decision in ("LENS_ROLES", "check_request", "truncation_note",
                     "format_critique"):
        assert decision in source, f"the view does not use {decision}"

    # build_prompt is deliberately ABSENT. The view never builds a prompt —
    # run_lens does, inside the module — and listing it above was my mistake:
    # a view that called it would be a view that could pass a different one.
    assert "build_prompt" not in source
    assert "Do NOT synthesise" not in source, (
        "the prompt is written in the view as well as in the module")
    assert "CONTENT_LIMIT" not in source or "3000" not in source, (
        "the view knows the truncation limit for itself")
