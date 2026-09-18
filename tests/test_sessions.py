"""
Sessions — the widest-reaching button in the app, and five defects behind it.

Loading a session as prior rebuilds EVERY personality. The Tk version does
that on the GUI thread while a model call runs on a worker that writes into
the transcript — and an AST pass over all 61 `Thread(target=...)` sites in the
engine found that summary worker is the only one touching a UI method
directly.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

from council_core import sessions as sessions_core  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class FakeStore:
    def __init__(self, sessions=(), turns=None, summaries=None, fail=None):
        self._sessions = list(sessions)
        self._turns = turns or {}
        self._summaries = dict(summaries or {})
        self._fail = fail
        self.saved = {}

    def list_sessions(self):
        if self._fail:
            raise self._fail
        return self._sessions

    def load_last(self, session_id, n=40):
        return self._turns.get(session_id, [])[-n:]

    def load_generated_summary(self, session_id):
        return self._summaries.get(session_id)

    def save_session_summary(self, session_id, text):
        self.saved[session_id] = text


class FakeWriter:
    def __init__(self, reply="a summary", raises=None):
        self.reply, self.raises = reply, raises
        self.prompts = []

    def respond(self, prompt, **kwargs):
        self.prompts.append(prompt)
        if self.raises:
            raise self.raises
        return self.reply


def _verdicts(tmp_path, records):
    path = tmp_path / "verdict_history.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                    encoding="utf-8")
    return path


def test_the_module_imports_no_toolkit():
    source = (ROOT / "council_core" / "sessions.py").read_text(encoding="utf-8")
    for toolkit in ("tkinter", "PySide6", "PyQt5", "messagebox"):
        assert toolkit not in source


# ============================================================
# Defect 1 — the id is not the label
# ============================================================

def test_a_row_carries_its_id_beside_its_label(tmp_path):
    """Both Tk handlers recover the id with label.split("  [")[0].strip().
    Presentation IS the data model: change the badge format and both break
    silently. Fourth instance of this habit found in the port today."""
    path = _verdicts(tmp_path, [{"session_id": "s1", "confidence": 7,
                                 "passed": True}])
    result = sessions_core.list_sessions(FakeStore(["s1"]), path)
    row = result.rows[0]
    assert row.id == "s1"
    assert row.label != row.id            # it is badged
    assert "7/10" in row.label


def test_an_id_containing_the_separator_survives(tmp_path):
    """The Tk split would truncate this one. Contrived, and exactly the kind
    of thing that happens once and is never diagnosed."""
    weird = "run  [2026]  backfill"
    path = _verdicts(tmp_path, [])
    result = sessions_core.list_sessions(FakeStore([weird]), path)
    assert result.rows[0].id == weird


def test_the_qt_tab_reads_the_id_from_the_row_object():
    """Read through conftest.code_of, which strips the docstring.

    The first version of this failed on the prose EXPLAINING the Tk defect —
    the sentence quoting `label.split("  [")[0]`. That is the third time in
    this port a source search has tripped on a comment about the thing it was
    looking for, which is why the helper now exists.
    """
    from tests.source_checks import code_of
    source = (ROOT / "council_qt" / "tabs" / "sessions.py").read_text(
        encoding="utf-8")
    body = code_of(source, "selected_session")
    assert ".id" in body
    assert "split(" not in body, "the tab recovers the id from the label"


# ============================================================
# Defect 2 — the badge answers the wrong question
# ============================================================

def test_the_badge_is_the_last_verdict_not_the_proudest(tmp_path):
    """THE DEFECT. The Tk lookup keeps, per session, the record with the
    greatest confidence — so a session that failed at confidence 9 and later
    passed at 6 is badged FAILED, permanently. The badge answers "how did this
    end?"."""
    path = _verdicts(tmp_path, [
        {"session_id": "s1", "confidence": 9, "passed": False},
        {"session_id": "s1", "confidence": 6, "passed": True},
    ])
    result = sessions_core.list_sessions(FakeStore(["s1"]), path)
    row = result.rows[0]
    assert row.passed is True, "the badge reported the highest-confidence run"
    assert "6/10" in row.label
    assert "✓" in row.label


def test_a_session_with_no_verdict_is_shown_plainly(tmp_path):
    result = sessions_core.list_sessions(FakeStore(["s1"]),
                                         _verdicts(tmp_path, []))
    assert result.rows[0].label == "s1"
    assert result.rows[0].passed is None


def test_a_torn_verdict_line_does_not_lose_the_others(tmp_path):
    path = tmp_path / "verdict_history.jsonl"
    path.write_text('{"session_id": "s1", "confidence": 5, "passed": true}\n'
                    '{not json at all\n'
                    '{"session_id": "s2", "confidence": 8, "passed": false}\n',
                    encoding="utf-8")
    records, total = sessions_core.read_verdicts(path)
    assert len(records) == 2
    assert total == 3


# ============================================================
# Defect 3 — the count that was not a count
# ============================================================

def test_the_summary_says_what_it_actually_read(tmp_path):
    """The Tk summary loads the last 200 records and reports "{total}
    deliberations", where total is the length of what it loaded. A vault with
    5,000 deliberations reports 200."""
    line = sessions_core.verdict_summary(
        5000, [{"passed": True}] * 150 + [{"passed": False}] * 50)
    assert "5000 deliberations recorded" in line
    assert "last 200 read" in line
    assert "150 of those passed" in line


def test_a_full_read_says_so_simply():
    line = sessions_core.verdict_summary(3, [{"passed": True},
                                             {"passed": False},
                                             {"passed": True}])
    assert line == "3 deliberations | 2 passed."


def test_an_empty_history_says_so():
    assert "No deliberations" in sessions_core.verdict_summary(0, [])


def test_reading_reports_the_file_size_not_the_sample(tmp_path):
    path = _verdicts(tmp_path, [{"session_id": f"s{i}"} for i in range(50)])
    records, total = sessions_core.read_verdicts(path, last_n=10)
    assert len(records) == 10
    assert total == 50


# ============================================================
# Defect 4 — filtering touches no disk
# ============================================================

def test_filtering_is_a_string_test_over_loaded_rows():
    """The Tk filter traces onto the full refresh, so every keystroke re-globs
    the conversations directory and re-parses up to 500 verdict records."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(sessions_core.filter_rows).strip())
    names = {getattr(n.func, "attr", getattr(n.func, "id", ""))
             for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert not (names & {"read_verdicts", "list_sessions", "glob", "open"}), (
        f"filtering reaches for the disk: {names}")


def test_filtering_matches_the_id_and_not_the_badge(tmp_path):
    rows = [sessions_core.SessionRow("alpha", "alpha  [9/10 ✓]"),
            sessions_core.SessionRow("beta", "beta  [3/10 ✗]")]
    assert len(sessions_core.filter_rows(rows, "alpha")) == 1
    assert len(sessions_core.filter_rows(rows, "")) == 2
    # the badge is presentation; filtering on it would be filtering on chrome
    assert len(sessions_core.filter_rows(rows, "9/10")) == 0


# ============================================================
# Defect 5 — the summary worker
# ============================================================

def test_the_summary_reports_through_a_result_not_a_widget():
    """The Tk worker calls a transcript method from inside the thread — the
    only one of the engine's 61 threads that touches a UI method directly."""
    from tests.source_checks import code_of
    body = code_of(sessions_core.ensure_summary)
    for widgetish in ("_append_transcript", "insert(", "configure("):
        assert widgetish not in body


def test_a_session_already_summarised_is_not_summarised_again():
    """It costs a model call every time the user loads that session."""
    writer = FakeWriter()
    store = FakeStore(turns={"s1": [{"who": "User", "text": "hi"}]},
                      summaries={"s1": "already done"})
    result = sessions_core.ensure_summary(store, writer, "s1")
    assert result.ok
    assert writer.prompts == []
    assert result.message == ""       # nothing to say about a no-op


def test_a_new_session_is_summarised_and_cached():
    writer = FakeWriter("the summary")
    store = FakeStore(turns={"s1": [{"who": "User", "text": "hi"}]})
    result = sessions_core.ensure_summary(store, writer, "s1")
    assert result.ok
    assert store.saved["s1"] == "the summary"
    assert "s1" in result.message


def test_a_session_with_no_turns_is_left_alone():
    writer = FakeWriter()
    result = sessions_core.ensure_summary(FakeStore(turns={}), writer, "s1")
    assert result.ok and writer.prompts == []


def test_a_failing_writer_is_reported_not_raised():
    store = FakeStore(turns={"s1": [{"who": "User", "text": "hi"}]})
    result = sessions_core.ensure_summary(store, FakeWriter(
        raises=RuntimeError("model gone")), "s1")
    assert not result.ok
    assert "model gone" in result.message


def test_the_prompt_asks_for_prose_and_says_how_many_turns():
    turns = [{"who": "User", "text": "why"}, {"who": "Writer", "text": "because"}]
    prompt = sessions_core.build_summary_prompt(turns)
    assert "no bullet points" in prompt
    assert "most recent 2 turns" in prompt
    assert "User: why" in prompt


def test_a_long_turn_is_trimmed_in_the_prompt():
    """Forty full turns of a long session would blow the context the summary
    is meant to fit inside."""
    prompt = sessions_core.build_summary_prompt(
        [{"who": "User", "text": "x" * 2000}])
    assert prompt.count("x") == 600


# ============================================================
# The rest
# ============================================================

def test_an_unreadable_store_is_reported(tmp_path):
    result = sessions_core.list_sessions(
        FakeStore(fail=OSError("conversations folder is gone")),
        _verdicts(tmp_path, []))
    assert not result.ok
    assert "gone" in result.message


def test_an_empty_store_says_when_sessions_appear(tmp_path):
    result = sessions_core.list_sessions(FakeStore([]), _verdicts(tmp_path, []))
    assert result.ok
    assert "after you close one" in result.message


def test_the_prior_label_reads_naturally():
    assert sessions_core.prior_label(None) == "Prior: none"
    assert sessions_core.prior_label("s1") == "Prior: s1"


def test_previewing_nothing_is_harmless():
    assert sessions_core.preview(FakeStore(), "") == ""


def test_a_preview_shows_who_said_what():
    store = FakeStore(turns={"s1": [{"who": "User", "text": "why is Q3 short"}]})
    text = sessions_core.preview(store, "s1")
    assert "User:" in text and "why is Q3 short" in text


def test_the_rebuild_happens_on_a_worker_in_the_qt_tab():
    """The Tk version rebuilds every personality on the GUI thread, which is a
    freeze of however long the weights take to load."""
    import ast
    source = (ROOT / "council_qt" / "tabs" / "sessions.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    method = next(node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) and node.name == "_rebuild")
    body = ast.get_source_segment(source, method) or ""
    assert "threading.Thread" in body
    assert "_to_ui" in body


def test_the_tab_hands_the_models_over_rather_than_reaching_across():
    """The prior session is global state and this tab does not own it."""
    source = (ROOT / "council_qt" / "tabs" / "sessions.py").read_text(
        encoding="utf-8")
    assert "on_models_changed" in source
