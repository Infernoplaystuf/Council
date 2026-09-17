"""
The transcript: the shared policy, and the Qt view that renders it.

WHY THIS FILE IS SEPARATE
The transcript is the widget the whole phase-6 estimate is least confident
about, so it gets its own tests rather than a corner of the foundation file.
Roughly half of them need no toolkit at all — the policy half — and those are
the ones that keep the two front ends saying the same thing.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from council_core import transcript as core  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# ============================================================
# The policy half — no toolkit
# ============================================================

def test_the_shared_module_imports_no_toolkit():
    source = (ROOT / "council_core" / "transcript.py").read_text(encoding="utf-8")
    for toolkit in ("tkinter", "PySide6", "PyQt5", "PyQt6"):
        assert toolkit not in source, f"council_core.transcript imports {toolkit}"


def test_a_final_entry_puts_the_colour_on_the_name_not_the_answer():
    """Colouring a whole answer reads badly over hundreds of lines; the name is
    what you scan for. Easy to get backwards when re-implementing."""
    segments = core.render("Writer", "  the answer  ", "final")
    assert len(segments) == 2
    assert segments[0].text == "\nWriter:\n"
    assert segments[0].tag == "who_writer"
    assert segments[1].text == "the answer\n"
    assert segments[1].tag is None


def test_a_phase_marker_is_indented_and_tagged_whole():
    segments = core.render("", "thinking", "phase")
    assert segments == [core.Segment("  thinking\n", "phase")]


def test_a_token_carries_no_header_and_no_newline():
    """A token is part of a line being built; a newline or a header per token
    would produce one word per line."""
    segments = core.render("Writer", "hel", "token")
    assert segments == [core.Segment("hel", "token")]


def test_an_unknown_kind_is_shown_in_full_rather_than_dropped():
    segments = core.render("Sage", "something", "a-kind-nobody-planned")
    assert "".join(s.text for s in segments) == "\nSage:\nsomething\n"


# -- the bug this extraction found --------------------------------------------

def test_a_known_role_gets_its_own_colour():
    assert core.role_tag("Writer") == "who_writer"
    assert core.role_tag("Judge") == "who_judge"
    assert core.TAGS["who_writer"].foreground == core.ROLE_COLORS["Writer"]


def test_a_lowercase_role_gets_its_colour_too():
    """The Tk version's test was `if tag in ROLE_COLORS or who in ROLE_COLORS`.
    The first half is dead — `tag` is "who_writer", the keys are "Writer" — so
    only an exact case-sensitive match survived, and "writer" (exactly how
    council_modules.MODEL_ROLES spells it) fell through to grey."""
    assert core.role_tag("writer") == "who_writer"
    assert core.role_tag("WRITER") == "who_writer"


def test_an_unknown_speaker_still_gets_a_readable_default():
    assert core.role_tag("Someone New") == "who_default"
    assert core.role_tag("") == "who_default"
    assert core.TAGS["who_default"].bold is True


def test_the_dead_condition_is_really_dead():
    """Pins the reasoning above rather than the conclusion, so a future reader
    can check the claim instead of trusting it."""
    for role in core.ROLE_COLORS:
        assert f"who_{role.lower()}" not in core.ROLE_COLORS


# -- errors -------------------------------------------------------------------

def test_an_error_entry_is_red_without_tagging_a_range_afterwards():
    """The Tk shell writes a normal entry then paints over the last two lines
    with tag_add. That needs a widget that can tag a range after the fact —
    the one Tk Text idiom with no cheap QTextEdit equivalent — and it breaks
    if the entry is not exactly two lines."""
    segments = core.render("ERROR", "it broke\non two lines\nor three", "error")
    assert all(s.tag == "error" for s in segments)


def test_an_error_of_any_length_is_fully_red():
    long_error = "\n".join(f"line {i}" for i in range(20))
    segments = core.render("ERROR", long_error, "error")
    joined = "".join(s.text for s in segments)
    assert joined.count("\n") > 3
    assert all(s.tag == "error" for s in segments)


# -- who hears about an entry -------------------------------------------------

@pytest.mark.parametrize("kind,stored", [
    ("final", True), ("observation", True),
    ("token", False), ("phase", False), ("thought", False),
])
def test_only_conversation_reaches_the_durable_record(kind, stored):
    """A stream of 2,000 tokens is the same answer written 2,000 times."""
    assert core.stores_in_history(kind) is stored


def test_the_session_log_keeps_everything_except_tokens():
    assert core.logs_to_session("phase") is True
    assert core.logs_to_session("thought") is True
    assert core.logs_to_session("token") is False


def test_provenance_records_the_model_and_not_the_user():
    """"Where did X come from" must not answer "you said it"."""
    assert core.records_provenance("Writer", "final") is True
    assert core.records_provenance("User", "final") is False
    assert core.records_provenance("Writer", "phase") is False


def test_the_deferred_turn_is_the_user_question_and_the_writers_answer():
    assert core.is_user_question("User", "final") is True
    assert core.is_final_answer("Writer", "final") is True
    assert core.is_final_answer("Judge", "final") is False
    assert core.is_final_answer("Writer", "token") is False


# -- the streaming header -----------------------------------------------------

def test_a_speaker_is_named_once_and_then_not_again():
    seen = set()
    first = core.stream_segments("Writer", "he", seen)
    seen.add("Writer")
    second = core.stream_segments("Writer", "llo", seen)
    assert first[0].text == "\nWriter: "
    assert first[0].tag == "who_writer"
    assert second == [core.Segment("llo", None)]


def test_plain_text_rebuilds_the_conversation_without_a_widget():
    entries = [("User", "why", "final"), ("Writer", "because", "final")]
    text = core.plain_text(entries)
    assert "User:" in text and "why" in text
    assert "Writer:" in text and "because" in text


# ============================================================
# The Qt half
# ============================================================

pytest.importorskip("PySide6", reason="the Qt view needs PySide6")

from PySide6.QtGui import QTextCursor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from council_qt.widgets.transcript import (  # noqa: E402
    MirroredTranscript, StreamView, TranscriptView, build_formats)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def _scrollable(qapp, view, entries=80):
    """A view with a real scroll range.

    `show()` is required and is not optional cosmetics: an unshown widget is
    never laid out, so its scrollbar reports 0..0 and every assertion about
    scrolling passes without testing anything. Under QT_QPA_PLATFORM=offscreen
    this opens no window — measured: range 0..3504 after showing, 0..0 before.
    """
    view.resize(400, 120)
    view.show()
    qapp.processEvents()
    for i in range(entries):
        view.append_entry("Writer", f"entry {i}", "final")
    qapp.processEvents()
    assert view.verticalScrollBar().maximum() > 0, (
        "the view has no scroll range, so this test would assert nothing")
    return view


def test_every_tag_the_policy_describes_becomes_a_format(qapp):
    """A tag with no format renders as body text — a silent loss of colour,
    and exactly what happens when someone adds a role and forgets the view."""
    formats = build_formats()
    assert set(formats) == set(core.TAGS)


def test_the_view_renders_a_role_in_its_own_colour(qapp):
    view = TranscriptView()
    view.append_entry("Writer", "hello", "final")

    cursor = view.textCursor()
    cursor.movePosition(QTextCursor.Start)
    cursor.movePosition(QTextCursor.Down)          # onto the "Writer:" line
    cursor.select(QTextCursor.LineUnderCursor)
    colour = cursor.charFormat().foreground().color().name()
    assert colour.lower() == core.ROLE_COLORS["Writer"].lower()


def test_a_phase_marker_is_italic_and_a_name_is_not(qapp):
    """The Dream3D mirror gets this wrong today: it copies only the foreground
    and forces bold on every tag, so phases render bold instead of italic."""
    formats = build_formats()
    assert formats["phase"].font().italic() is True
    assert formats["phase"].font().bold() is False
    assert formats["who_writer"].font().bold() is True
    assert formats["who_writer"].font().italic() is False


def test_the_view_is_read_only_and_has_no_undo_history(qapp):
    """Undo on a write-only log is pure memory: every append would be kept a
    second time."""
    view = TranscriptView()
    assert view.isReadOnly()
    assert view.isUndoRedoEnabled() is False


def test_appending_stays_at_the_bottom_when_you_are_at_the_bottom(qapp):
    view = _scrollable(qapp, TranscriptView())
    assert view.at_bottom(), "the view stopped following new entries"
    view.append_entry("Writer", "one more", "final")
    qapp.processEvents()
    assert view.at_bottom(), "a new entry was appended below the visible end"


def test_appending_leaves_you_where_you_were_reading(qapp):
    """A user who scrolled up is reading something. Yanking them to the end on
    every token makes a streaming answer impossible to read — obvious in use,
    invisible in a test that does not exist."""
    view = _scrollable(qapp, TranscriptView())
    bar = view.verticalScrollBar()
    bar.setValue(bar.maximum() // 3)
    parked = bar.value()

    view.append_entry("Writer", "something new", "final")
    qapp.processEvents()
    assert bar.value() == parked, "the view scrolled away from the reader"


def test_appending_is_not_quadratic(qapp):
    """The naive translation of insert(END, ...) is
    `setHtml(toHtml() + more)`, which is quadratic and stalls visibly at a few
    hundred entries. This is the measurement that would catch that, rather
    than a comment asserting it does not happen."""
    view = TranscriptView()

    def cost(n):
        view.clear_all()
        start = time.perf_counter()
        for i in range(n):
            view.append_entry("Writer", f"entry {i}", "final")
        return time.perf_counter() - start

    cost(200)                                  # warm up
    small = cost(200)
    large = cost(800)
    # Linear would be 4x. Allow generous slack for a noisy machine but not the
    # 16x that quadratic growth would produce.
    assert large < small * 9, (
        f"appending looks super-linear: 200 entries {small:.3f}s, "
        f"800 entries {large:.3f}s")


# -- the stream box -----------------------------------------------------------

def test_tokens_do_not_scroll_until_flushed(qapp):
    """The Tk shell defers the scroll to once per queue drain after the
    per-token version turned out to be the hottest path in the app. Same
    problem in Qt, same fix — and a test so it is not tidied back."""
    view = StreamView()
    view.resize(400, 100)
    view.show()
    qapp.processEvents()
    for i in range(300):
        view.append_token("Writer", f"tok{i} ")
    qapp.processEvents()
    bar = view.verticalScrollBar()
    assert bar.maximum() > 0, "no scroll range — this test would assert nothing"
    assert bar.value() < bar.maximum(), (
        "the tokens scrolled the view on their own; the deferred-scroll split "
        "has been lost")
    view.flush()
    qapp.processEvents()
    assert view.at_bottom(), "flush did not catch the view up"


def test_flushing_without_tokens_does_nothing(qapp):
    view = StreamView()
    view.flush()                               # must not raise
    assert not view._dirty


def test_a_cleared_stream_names_the_speaker_again(qapp):
    view = StreamView()
    view.append_token("Writer", "a")
    view.clear_all()
    view.append_token("Writer", "b")
    assert "Writer:" in view.toPlainText()


# -- the mirror ---------------------------------------------------------------

def test_both_transcripts_receive_the_same_entry(qapp):
    council, dream3d = TranscriptView(), TranscriptView()
    mirror = MirroredTranscript([council, dream3d])
    mirror.append_entry("Judge", "the verdict", "final")
    for view in (council, dream3d):
        assert "the verdict" in view.toPlainText()


def test_a_missing_mirror_costs_nothing(qapp):
    """The Dream3D tab is built lazily, so most entries are written when only
    one view exists. The Tk shell skips a None widget; this must too."""
    council = TranscriptView()
    mirror = MirroredTranscript([council])
    mirror.append_entry("Writer", "alone", "final")
    assert "alone" in council.toPlainText()

    dream3d = TranscriptView()
    mirror.add(dream3d)
    mirror.append_entry("Writer", "together", "final")
    assert "together" in dream3d.toPlainText()
    assert "alone" not in dream3d.toPlainText(), (
        "a late mirror replayed history it never had")


def test_a_destroyed_view_drops_out_instead_of_raising(qapp):
    """Closing a tab deletes the C++ object under a live Python reference. The
    Tk shell catches TclError for exactly this."""
    council, dream3d = TranscriptView(), TranscriptView()
    mirror = MirroredTranscript([council, dream3d])
    dream3d.deleteLater()
    dream3d.setParent(None)
    import shiboken6
    shiboken6.delete(dream3d)

    mirror.append_entry("Writer", "still works", "final")
    assert "still works" in council.toPlainText()
    assert len(mirror) == 1, "the dead view was not dropped"


def test_the_entry_is_rendered_once_for_all_views(qapp):
    """Rendering per view would double the work for every entry, and is the
    kind of thing that looks identical until the transcript is long."""
    import ast
    import inspect
    source = inspect.getsource(MirroredTranscript.append_entry)
    tree = ast.parse(source.strip())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "render"]
    assert len(calls) == 1
    loops = [n for n in ast.walk(tree) if isinstance(n, ast.For)]
    assert loops, "expected a loop over the views"
    assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr == "render"
                   for loop in loops for n in ast.walk(loop)), (
        "render() is being called inside the per-view loop")


# ============================================================
# Both front ends
# ============================================================

def test_the_tk_shell_renders_through_the_shared_module():
    """The whole point. If the Tk shell keeps its own copy of the formatting,
    the two transcripts drift and nobody notices until a user switches."""
    engine = (ROOT / "council_gui_engine.py").read_text(encoding="utf-8")
    assert "transcript_core.render(" in engine
    assert "transcript_core.stream_segments(" in engine
    assert "transcript_core.role_tag(" in engine


@pytest.mark.parametrize("predicate", [
    "logs_to_session", "stores_in_history", "records_provenance",
    "is_user_question", "is_final_answer",
])
def test_every_policy_decision_is_made_in_one_place(predicate):
    engine = (ROOT / "council_gui_engine.py").read_text(encoding="utf-8")
    assert f"transcript_core.{predicate}(" in engine, (
        f"the Tk shell still decides {predicate} for itself")


def test_the_tk_shell_no_longer_repaints_an_error_after_writing_it():
    engine = (ROOT / "council_gui_engine.py").read_text(encoding="utf-8")
    assert 'tag_add("error"' not in engine


def test_both_transcripts_are_configured_from_the_same_description():
    """The Dream3D mirror used to read the Council transcript's foregrounds
    back out and force bold on all of them."""
    engine = (ROOT / "council_gui_engine.py").read_text(encoding="utf-8")
    assert engine.count("_apply_transcript_tags(") == 3     # def + 2 calls
    assert "tag_cget" not in engine


def test_the_tk_helpers_apply_segments_without_reinterpreting_them():
    """_insert_segments must not decide anything — no kind checks, no role
    lookups. The moment it does, there are two renderers again."""
    import ast
    engine = (ROOT / "council_gui_engine.py").read_text(encoding="utf-8")
    tree = ast.parse(engine)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "_insert_segments")
    source = ast.get_source_segment(engine, fn)
    for forbidden in ("kind", "role_tag", "who"):
        assert forbidden not in source, (
            f"_insert_segments looks at {forbidden!r} — it is re-deciding what "
            f"render() already decided")


# ============================================================
# The real Tk method, against a fake widget
# ============================================================

ENGINE_PROBE = '''
import sys
sys.path.insert(0, %(root)r)
import council_gui_engine as cge

class FakeText:
    """Records what a Tk Text would have been told, in order."""
    def __init__(self): self.calls = []; self.state = None
    def configure(self, **kw): self.state = kw.get("state", self.state)
    def insert(self, where, text, *tags):
        self.calls.append((text, tags[0] if tags else None))
    def see(self, where): pass

class FakeConsole:
    _append_transcript = cge.CouncilConsole._append_transcript
    _role_tag = cge.CouncilConsole._role_tag
    def __init__(self):
        self.transcript = FakeText()
        self.dream3d_transcript = FakeText()
        self.conv_logger = None
        self.provenance = None
        self.stored = []
        self.session_id = "s1"
        self.librarian = type("L", (), {"log_event": lambda s, w, t: None})()
        self.convo_store = type("C", (), {
            "append": lambda s, sid, rec: self.stored.append(rec)})()

c = FakeConsole()
c._append_transcript("Writer", "  an answer  ", "final")
c._append_transcript("", "deliberating", "phase")
c._append_transcript("Writer", "tok", "token")
c._append_transcript("ERROR", "it broke", "error")

import json
print("RESULT" + json.dumps({
    "calls": c.transcript.calls,
    "mirror": c.dream3d_transcript.calls,
    "state": c.transcript.state,
    "last_answer": getattr(c, "_last_answer", None),
    "stored": len(c.stored),
}))
'''


@pytest.fixture(scope="module")
def engine_transcript():
    """Run the REAL `_append_transcript` against a fake widget, in a subprocess.

    Worth the four seconds the engine takes to import: every other test here
    checks the shared module or the Qt view, and this is the only one that
    proves the Tk shell still produces the same characters after the rewire.

    A subprocess because importing the engine prints backend banners and sets a
    Windows AppUserModelID — side effects that do not belong in a pytest
    session shared with other files. Safe to import: a static check (see
    test_the_engine_builds_no_widget_at_import) confirms it constructs no Tk
    object at module level, so nothing is displayed.
    """
    import json
    import subprocess
    import tempfile
    import textwrap

    code = ENGINE_PROBE % {"root": str(ROOT)}
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False,
                                     encoding="utf-8") as handle:
        handle.write(textwrap.dedent(code))
        path = handle.name

    env = dict(os.environ, COUNCIL_NO_DIALOGS="1", QT_QPA_PLATFORM="offscreen")
    proc = subprocess.run([sys.executable, path], capture_output=True,
                          text=True, timeout=300, env=env)
    Path(path).unlink(missing_ok=True)
    if proc.returncode != 0:
        pytest.fail(f"the engine no longer imports or the method raised:\n"
                    f"{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}")
    line = next((ln for ln in proc.stdout.splitlines()
                 if ln.startswith("RESULT")), None)
    assert line, f"probe produced no result:\n{proc.stdout[-2000:]}"
    return json.loads(line[len("RESULT"):])


def test_the_engine_builds_no_widget_at_import():
    """What makes importing it in a test safe, and worth keeping true."""
    import ast
    tree = ast.parse((ROOT / "council_gui_engine.py").read_text(encoding="utf-8"))
    built = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Import,
                             ast.ImportFrom)):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute):
                if inner.func.attr in ("Tk", "Toplevel", "StringVar",
                                       "BooleanVar", "IntVar"):
                    built.append(f"line {inner.lineno}: {inner.func.attr}")
    assert not built, f"importing the engine would build a widget: {built}"


def test_the_tk_shell_writes_exactly_what_render_says(engine_transcript):
    """The characters and tags the real method produces, end to end."""
    calls = [tuple(c) for c in engine_transcript["calls"]]
    assert calls == [
        ("\nWriter:\n", "who_writer"),
        ("an answer\n", None),
        ("  deliberating\n", "phase"),
        ("tok", "token"),
        ("\nERROR:\n", "error"),
        ("it broke\n", "error"),
    ]


def test_the_two_transcripts_receive_identical_writes(engine_transcript):
    assert engine_transcript["calls"] == engine_transcript["mirror"]


def test_the_widget_is_left_read_only(engine_transcript):
    """A transcript left in state="normal" is a transcript the user can type
    into, and the next insert lands wherever their cursor went."""
    assert engine_transcript["state"] == "disabled"


def test_the_deferred_turn_is_still_captured(engine_transcript):
    """"⤓ Defer to Vault" sends the exact turn the model could not satisfy, so
    this tracking is the feature, not bookkeeping."""
    assert engine_transcript["last_answer"] == "  an answer  "


def test_phases_and_tokens_stay_out_of_the_stored_conversation(engine_transcript):
    """Four entries went in; the phase and the token are progress noise."""
    assert engine_transcript["stored"] == 2
