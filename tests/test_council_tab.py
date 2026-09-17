"""
The Council tab, tested against the requirements rather than against Tk.

`docs/qt_migration/phase6_port_requirements.md` section A lists nine confirmed
defects in the Tk Council tab, six of them logic defects that a faithful
translation carries across silently. Five are designed out of the Qt tab, and
this file is what stops them coming back — each test names the requirement it
holds and what the Tk version does instead.

A test that merely says "the tab builds" is already covered, generically, by
tests/test_council_qt_foundation.py's parametrised sweep over the registry.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

pytest.importorskip("PySide6", reason="the Qt tab needs PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from council_core import council_options  # noqa: E402
from council_qt.tabs.council import (CouncilActions, CouncilTab,  # noqa: E402
                                     PER_TURN_FIELDS, amp)

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture
def tab(qapp):
    widget = CouncilTab()
    yield widget
    widget.deleteLater()


def _pump(app, predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


# ============================================================
# A1 — the worker never reads a widget
# ============================================================

def test_the_options_snapshot_is_taken_from_the_checkboxes(tab):
    tab._checkboxes["tools"].setChecked(True)
    tab._checkboxes["stream"].setChecked(False)
    options = tab.options()
    assert options.tools is True
    assert options.stream is False


def test_the_snapshot_is_a_value_not_a_live_view(tab):
    """The whole point of A1. If the worker held something that reads the
    checkbox, toggling mid-turn would change the turn's behaviour halfway
    through — and reading a Qt widget from a worker is a data race, not just
    a surprise."""
    options = tab.options()
    before = options.tools
    tab._checkboxes["tools"].setChecked(not before)
    assert options.tools == before, "the snapshot tracked the widget"


def test_a_switch_demo_mode_hides_keeps_its_default(qapp):
    """A missing checkbox must not read as False. `deliberate` is hidden in
    DEMO_MODE and forced off at run time; `adversarial` is hidden and stays at
    its default rather than being silently cleared."""
    demo = CouncilTab(demo_mode=True)
    assert "deliberate" not in demo._checkboxes
    options = demo.options()
    assert options.deliberate is False          # forced by effective()
    assert options.adversarial is False         # its default, not an accident
    demo.deleteLater()


def test_demo_mode_forces_deliberation_off_even_if_the_switch_says_yes(qapp):
    """Two separate pieces of code in the Tk shell, and only the run-time one
    matters — a hidden checkbox that was still honoured would be a bug you
    could not see."""
    options = council_options.CouncilOptions(deliberate=True)
    assert options.effective(demo_mode=True).deliberate is False
    assert options.effective(demo_mode=False).deliberate is True


def test_the_turn_worker_is_handed_the_snapshot_and_the_typed_text(tab, qapp):
    seen = {}

    class Recording(CouncilActions):
        def send(self, typed_text, options, *, on_event=None, on_token=None):
            seen["text"] = typed_text
            seen["options"] = options
            seen["thread"] = threading.current_thread().name

    tab.actions = Recording()
    tab.input.setPlainText("  why is Q3 short?  ")
    tab.on_send()
    assert _pump(qapp, lambda: "text" in seen)
    assert seen["text"] == "why is Q3 short?"
    assert isinstance(seen["options"], council_options.CouncilOptions)
    assert seen["thread"] != threading.main_thread().name, (
        "the turn ran on the GUI thread")


def test_no_model_call_happens_in_the_widget():
    """A6: the Tk build calls local_chat(timeout=45) unconditionally on the UI
    thread every send, and refreshes the data index from a button handler.

    The rule is scoped to CouncilTab — the WIDGET — rather than to a function
    named `work`. An earlier version checked the latter and reported
    `CouncilActions._direct` as a violation: it does call the model, but only
    ever from inside send(), which the widget only ever calls from a worker.
    Scoping by name meant the test could not distinguish "on the GUI thread"
    from "lexically outside one particular closure", which is the thing it is
    actually about.
    """
    import ast
    source = (ROOT / "council_qt" / "tabs" / "council.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)

    tab = next(node for node in ast.walk(tree)
               if isinstance(node, ast.ClassDef) and node.name == "CouncilTab")
    worker_nodes = {id(node) for fn in ast.walk(tab)
                    if isinstance(fn, ast.FunctionDef) and fn.name == "work"
                    for node in ast.walk(fn)}

    offenders = []
    for node in ast.walk(tab):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr in ("local_chat", "respond", "refresh", "run_turn"):
            if id(node) not in worker_nodes:
                offenders.append(f"line {node.lineno}: .{node.func.attr}()")
    assert not offenders, (
        f"the widget does blocking work on the GUI thread: {offenders}")


# ============================================================
# A2 — one turn at a time
# ============================================================

def test_a_second_send_is_refused_while_one_is_running(tab, qapp):
    """`_send` in Tk is reachable eight ways with nothing serialising it, and
    two concurrent turns patch and restore model.respond over each other."""
    release = threading.Event()

    class Slow(CouncilActions):
        def send(self, typed_text, options, *, on_event=None, on_token=None):
            release.wait(2.0)

    tab.actions = Slow()
    tab.input.setPlainText("first")
    tab.on_send()
    assert _pump(qapp, lambda: tab._turn_active)

    assert tab.begin_turn() is False, "a second turn was allowed"
    release.set()
    assert _pump(qapp, lambda: not tab._turn_active)


def test_refusing_a_second_send_says_so(tab, qapp):
    """An ignored click reads as a broken button."""
    tab._turn_active = True
    tab.begin_turn()
    assert "already running" in tab.transcript.toPlainText()


def test_the_send_button_is_disabled_for_the_duration(tab, qapp):
    release = threading.Event()

    class Slow(CouncilActions):
        def send(self, typed_text, options, *, on_event=None, on_token=None):
            release.wait(2.0)

    tab.actions = Slow()
    tab.input.setPlainText("first")
    tab.on_send()
    assert _pump(qapp, lambda: not tab.send_btn.isEnabled())
    release.set()
    assert _pump(qapp, lambda: tab.send_btn.isEnabled())


def test_a_failing_turn_still_releases_the_lock(tab, qapp):
    """Otherwise one exception makes the tab permanently unusable, and the
    only cue is a button that never comes back."""
    class Boom(CouncilActions):
        def send(self, typed_text, options, *, on_event=None, on_token=None):
            raise RuntimeError("model gone")

    tab.actions = Boom()
    tab.input.setPlainText("anything")
    tab.on_send()
    assert _pump(qapp, lambda: not tab._turn_active)
    assert tab.send_btn.isEnabled()
    assert "model gone" in tab.transcript.toPlainText()


def test_a_turn_against_a_real_engine_reports_what_stopped_it(tab, qapp):
    """The Qt tab runs a REAL turn — this test is the proof.

    It used to assert "the turn is not extracted yet". It is now, and on a
    machine with no model file configured the turn gets as far as

        ▶ Round 1/2 — Candidate generation
        ▶ Writer — drafting answer
        Council: The turn failed: RuntimeError('COUNCIL_BACKEND=gguf but
                 COUNCIL_GGUF_PATH is not set. ...')

    which is the whole path working: personalities built, agents built, panel
    chosen, orchestrator started, phase events rendered into the transcript,
    and a real failure reported where the user is looking rather than raised
    into a worker nobody watches.

    So the assertion is about the PROPERTY, not the wording: something reached
    the transcript, it named what to do, and the tab came back."""
    tab.input.setPlainText("anything")
    tab.on_send()
    assert _pump(qapp, lambda: not tab._turn_active, timeout=20)
    text = tab.transcript.toPlainText()
    assert text.strip(), "the turn said nothing at all"
    assert "Council:" in text or "ERROR:" in text, (
        "the failure never reached the transcript")
    assert tab.send_btn.isEnabled(), "the tab did not recover"


# ============================================================
# A3 — the verdict bar is tied to a verdict
# ============================================================

def test_the_verdict_bar_stays_hidden_without_a_verdict(tab):
    """THE MOST SERIOUS DEFECT IN THE TK TAB. Its fast path posts done without
    ever posting a verdict_record, the pump shows the bar anyway, and agreeing
    stamps user_agreed=True on the last line of verdict_history.jsonl — which
    belongs to an unrelated earlier deliberation."""
    tab.show_verdict_bar(None)
    assert not tab.vfb_frame.isVisibleTo(tab)
    assert tab._last_verdict_id is None


def test_the_verdict_bar_appears_with_one(tab):
    tab.show_verdict_bar("verdict-42")
    assert tab.vfb_frame.isVisibleTo(tab)
    assert tab._last_verdict_id == "verdict-42"


def test_a_verdict_response_carries_the_id_it_is_answering(tab):
    recorded = {}

    class Recording(CouncilActions):
        def record_verdict_response(self, verdict_id, agreed, objection=""):
            recorded.update(id=verdict_id, agreed=agreed, objection=objection)

    tab.actions = Recording()
    tab.show_verdict_bar("verdict-42")
    tab.on_agree()
    assert recorded == {"id": "verdict-42", "agreed": True, "objection": ""}


def test_an_objection_is_carried_with_the_id_too(tab):
    recorded = {}

    class Recording(CouncilActions):
        def record_verdict_response(self, verdict_id, agreed, objection=""):
            recorded.update(id=verdict_id, agreed=agreed, objection=objection)

    tab.actions = Recording()
    tab.show_verdict_bar("verdict-7")
    tab.on_disagree_open()
    tab.objection.setPlainText("it ignored the Q3 refunds")
    tab.on_disagree_submit()
    assert recorded["id"] == "verdict-7"
    assert recorded["agreed"] is False
    assert "Q3 refunds" in recorded["objection"]


def test_an_empty_objection_is_refused_rather_than_sent(tab):
    recorded = []

    class Recording(CouncilActions):
        def record_verdict_response(self, verdict_id, agreed, objection=""):
            recorded.append(verdict_id)

    tab.actions = Recording()
    tab.show_verdict_bar("verdict-7")
    tab.on_disagree_open()
    tab.on_disagree_submit()
    assert recorded == []
    assert "Say what you disagree with" in tab.transcript.toPlainText()


def test_the_api_cannot_express_stamping_the_last_line():
    """The Tk bug is only possible because nothing identifies WHICH verdict is
    being answered. A signature that requires an id removes the whole class."""
    import inspect
    signature = inspect.signature(CouncilActions.record_verdict_response)
    assert "verdict_id" in signature.parameters
    assert signature.parameters["verdict_id"].default is inspect.Parameter.empty


# ============================================================
# A4 — per-turn state is reset in one place
# ============================================================

def test_the_reset_clears_every_field_a_turn_sets(tab):
    """The Tk reset block clears four fields and misses two, so after one fast
    answer the Expand button stays live holding a stale question."""
    for field in PER_TURN_FIELDS:
        setattr(tab, field, "stale")
    tab.expand_btn.setEnabled(True)
    tab.reset_turn()
    left = [f for f in PER_TURN_FIELDS if getattr(tab, f) is not None]
    assert not left, f"reset_turn() missed {left}"
    assert not tab.expand_btn.isEnabled()


def test_the_declared_field_list_and_the_reset_agree():
    """A hand-maintained reset drifts. This is the test that keeps the list
    honest: it reads reset_turn's source and requires it to iterate the
    declared tuple rather than name fields itself."""
    import ast
    import inspect
    source = inspect.getsource(CouncilTab.reset_turn)
    tree = ast.parse(source.strip())
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "PER_TURN_FIELDS" in names, (
        "reset_turn() does not iterate PER_TURN_FIELDS, so the two can drift")


def test_expanding_with_no_fast_answer_is_refused(tab):
    tab._last_fast_question = None
    tab.on_expand_with_council()
    assert "no fast answer" in tab.transcript.toPlainText()


def test_a_new_turn_resets_the_previous_one(tab, qapp):
    tab._last_fast_question = "an old question"
    tab.expand_btn.setEnabled(True)
    tab.input.setPlainText("something new")
    tab.on_send()
    assert _pump(qapp, lambda: not tab._turn_active)
    assert tab._last_fast_question is None
    assert not tab.expand_btn.isEnabled(), (
        "the stale Expand button survived a new turn")


# ============================================================
# A5 — intent detection reads what the user typed
# ============================================================

def test_the_tab_never_rebinds_the_typed_text_to_the_augmented_text():
    """`user_text = augmented` at :19568 is the whole bug: three later scans
    (lead role, LaTeX, script keywords) then read injected spreadsheet
    contents, and a column called "artist" reassigns the lead role."""
    source = (ROOT / "council_qt" / "tabs" / "council.py").read_text(
        encoding="utf-8")
    for bad in ("typed = augmented", "typed_text = augmented",
                "typed = self._augmented"):
        assert bad not in source, f"the typed text is rebound: {bad}"


def test_the_action_takes_the_typed_text_as_its_own_parameter():
    """So a caller cannot pass the wrong one by accident."""
    import inspect
    signature = inspect.signature(CouncilActions.send)
    assert list(signature.parameters)[1] == "typed_text"


# ============================================================
# The view itself
# ============================================================

def test_every_switch_the_shared_module_describes_has_a_checkbox(tab):
    """A switch described and not built is a setting the user cannot reach;
    one built and not described is a setting the Tk shell does not have."""
    expected = {s.key for s in council_options.visible_switches(False)}
    assert set(tab._checkboxes) == expected


def test_the_switches_are_in_the_order_the_tk_shell_packs_them():
    """Users find a checkbox by position. A port that sorts them
    alphabetically has moved every one of them."""
    keys = [s.key for s in council_options.SWITCHES]
    assert keys[:4] == ["deliberate", "tools", "fill_ide", "stream"]


def test_ampersands_survive_in_captions(tab):
    assert amp("Find & Chart") == "Find && Chart"
    assert "&&" not in tab.send_btn.text(), "no ampersand to escape here"


def test_the_transcript_and_the_stream_are_separate_widgets(tab):
    """They carry different content at different rates — the stream takes
    ~100 tokens/sec and scrolls once per drain; the transcript does not see a
    token at all."""
    assert tab.transcript is not tab.stream_box


def test_a_final_answer_offers_the_save_panel(tab):
    assert not tab.save_frame.isVisibleTo(tab)
    tab.append("Writer", "here is your answer", "final")
    assert tab.save_frame.isVisibleTo(tab)
    assert tab._last_answer == "here is your answer"


def test_a_phase_marker_does_not_offer_the_save_panel(tab):
    tab.append("", "thinking", "phase")
    assert not tab.save_frame.isVisibleTo(tab)


def test_saving_with_no_answer_says_so_rather_than_opening_a_dialog(tab):
    tab._last_answer = None
    tab.on_save_answer()
    assert "no answer to save" in tab.transcript.toPlainText()


def test_the_specialist_pin_offers_automatic_first(tab):
    assert tab.specialist_box.itemText(0) == council_options.AUTO_LABEL
    assert tab.pinned_specialist() is None


def test_the_backend_override_defaults_to_leaving_routing_alone(tab):
    assert tab.backend_box.currentText() == council_options.DEFAULT_BACKEND
    assert tab.backend_override() is None


def test_the_tab_renders_through_the_shared_transcript_policy():
    source = (ROOT / "council_qt" / "tabs" / "council.py").read_text(
        encoding="utf-8")
    assert "transcript_core" in source
    assert "append_entry" in source


def test_the_tab_applies_the_run_time_rule_and_not_just_the_default(qapp):
    """Found by mutation: deleting `.effective(demo_mode)` from the tab's
    options() changed nothing any test could see, because in DEMO_MODE
    `deliberate` has no checkbox and its default is already False. So the tab
    was passing for the wrong reason — the rule was never exercised through it,
    only through the shared module's own unit test.

    Forcing the stored state to True is the only way to tell the two apart, and
    it is worth telling apart: a hidden checkbox that was still honoured is
    exactly the bug the run-time rule exists to prevent."""
    demo = CouncilTab(demo_mode=True)
    demo._opts.deliberate = True          # as if something set it
    assert demo.options().deliberate is False, (
        "the tab returned the stored value without applying the DEMO_MODE rule")
    demo.deleteLater()
