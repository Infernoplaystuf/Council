"""
The conversation record both shells write — council_core.conversations.

The Qt shell kept no conversation at all: it imported `convo_store`, which does
not exist, so History and the Sessions list were empty and nothing a user asked
in Qt was saved. It now writes the files the Tk shell writes, where Tk writes
them, so the two shells' sessions are one list. These tests hold the format,
the place, the "two launches at once" cases and the engine-facing fixes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import conversations  # noqa: E402
from council_core.conversations import Conversation, ConversationStore  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def _lines(path):
    return [json.loads(line) for line in
            Path(path).read_text(encoding="utf-8").splitlines() if line]


# ============================================================
# The store is the engine's
# ============================================================

def test_the_engine_store_is_this_class():
    import council_engine as ce
    assert ce.ConversationStore is ConversationStore


def test_the_file_name_rule_is_the_engines():
    import council_engine as ce
    for raw in ("20261012_101500", "a b/c\\d:e", "  ", "x" * 200, "ünï"):
        assert conversations.safe_name(raw, 64) == ce.safe_name(raw, 64)


def test_a_second_store_on_the_folder_sees_new_turns(tmp_path):
    """The personalities have their own store. Its cache was invalidated only
    by ITS OWN appends, so after another store wrote turn 1's answer, turn 2's
    prompt was served the history it had cached during turn 1."""
    import council_engine as ce
    writer = ce.ConversationStore(tmp_path / "conversations")
    reader = ce.ConversationStore(tmp_path / "conversations")
    writer.append("s", {"who": "User", "text": "first"})
    assert [t["text"] for t in reader.load_last("s")] == ["first"]
    writer.append("s", {"who": "Writer", "text": "answer"})
    writer.append("s", {"who": "User", "text": "second"})
    assert [t["text"] for t in reader.load_last("s")] == [
        "first", "answer", "second"]


def test_a_record_is_one_whole_json_line(tmp_path):
    store = ConversationStore(tmp_path / "c")
    store.append("s", {"ts": "t", "who": "User", "text": "multi\nline ü"})
    raw = (tmp_path / "c" / "s.jsonl").read_bytes()
    assert raw.count(b"\n") == 1 and raw.endswith(b"\n")
    assert b"\r" not in raw, "Windows text mode crept in"
    assert _lines(tmp_path / "c" / "s.jsonl")[0]["text"] == "multi\nline ü"


def test_a_torn_line_is_skipped_not_fatal(tmp_path):
    store = ConversationStore(tmp_path / "c")
    store.append("s", {"who": "User", "text": "kept"})
    with open(tmp_path / "c" / "s.jsonl", "ab") as fh:
        fh.write(b'{"who": "Writer", "te')          # a crash mid-line
    store.append("s", {"who": "User", "text": "also kept"})
    assert [t["text"] for t in store.load_last("s")] == ["kept", "also kept"]


def test_claim_is_exclusive(tmp_path):
    store = ConversationStore(tmp_path / "c")
    assert store.claim("s") is True
    assert store.claim("s") is False


# ============================================================
# One launch's conversation
# ============================================================

def test_a_launch_writes_what_tk_writes_where_tk_writes_it(tmp_path):
    vault = tmp_path / "vault"
    conv = Conversation(vault)
    conv.record("User", "what is in bay 4?", "final")
    conv.record("", "▶ Writer — drafting answer", "phase")
    conv.record("Writer", "thinking", "thought")
    conv.record("Writer", "a pump", "final")
    conv.record("Council", "saved", "observation")
    conv.close()

    session = vault / "conversations" / f"{conv.session_id}.jsonl"
    rows = _lines(session)
    assert [(r["who"], r["text"]) for r in rows] == [
        ("User", "what is in bay 4?"), ("Writer", "a pump"),
        ("Council", "saved")]
    assert all(set(r) >= {"ts", "who", "text"} for r in rows)

    log = vault / "conversation_logs" / f"session_{conv.session_id}.jsonl"
    kinds = [r["kind"] for r in _lines(log)]
    assert kinds[0] == "session_start" and kinds[-1] == "session_end"
    assert "phase" in kinds and "thought" in kinds

    import question_history
    asked = [q["q"] for q in question_history.QuestionHistory(vault).all()]
    assert asked == ["what is in bay 4?"]


def test_the_session_id_is_the_tk_format(tmp_path):
    conv = Conversation(tmp_path / "vault")
    assert conversations.RAW_SESSION_ID.match(conv.session_id)
    assert len(conv.session_id.split("_")[0]) == 8


def test_nothing_asked_leaves_no_empty_session(tmp_path):
    vault = tmp_path / "vault"
    Conversation(vault).close()
    assert not list((vault / "conversations").glob("*.jsonl"))


def test_two_launches_in_the_same_second_get_two_sessions(tmp_path):
    vault = tmp_path / "vault"
    when = datetime(2026, 10, 12, 10, 15, 0)
    first, second = Conversation(vault), Conversation(vault)
    a = first._new_session_id(now=when)
    b = second._new_session_id(now=when)
    assert a == "20261012_101500" and b == "20261012_101500_2"


def test_a_session_tk_already_wrote_is_not_written_into(tmp_path):
    """The Tk shell does not check: if it took this second first, the Qt
    launch moves to a fresh id instead of appending into Tk's file."""
    vault = tmp_path / "vault"
    conv = Conversation(vault)
    taken = conv.session_id
    tk = ConversationStore(vault / "conversations")
    tk.append(taken, {"ts": "t", "who": "User", "text": "Tk's question"})
    before = (vault / "conversations" / f"{taken}.jsonl").read_bytes()

    conv.record("User", "Qt's question")
    assert conv.session_id != taken
    assert (vault / "conversations" / f"{taken}.jsonl").read_bytes() == before
    assert [r["text"] for r in _lines(
        vault / "conversations" / f"{conv.session_id}.jsonl")] == [
        "Qt's question"]


def test_two_processes_at_once_keep_every_line(tmp_path):
    """Two real launches recording at the same time: two sessions, every
    line of each intact, and History has every question from both."""
    vault = tmp_path / "vault"
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(ROOT)!r})
        from council_core.conversations import Conversation
        conv = Conversation({str(vault)!r})
        for i in range(40):
            conv.record("User", f"{{sys.argv[1]}} question {{i}}")
            conv.record("Writer", f"{{sys.argv[1]}} answer {{i}}")
        conv.close()
        print(conv.session_id)
    """)
    env = dict(os.environ)
    procs = [subprocess.Popen([PY, "-c", script, name], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True)
             for name in ("alpha", "beta")]
    ids = []
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err
        ids.append(out.strip().splitlines()[-1])
    assert len(set(ids)) == 2, ids
    for session_id, name in zip(ids, ("alpha", "beta")):
        rows = _lines(vault / "conversations" / f"{session_id}.jsonl")
        assert len(rows) == 80
        assert all(r["text"].startswith(name) for r in rows)
    import question_history
    asked = {q["q"] for q in question_history.QuestionHistory(vault).all()}
    assert len(asked) == 80, f"History lost {80 - len(asked)} questions"


def test_question_history_survives_four_writers_at_once(tmp_path):
    """add() is read-modify-write; with only a thread lock, two processes
    each read the old list and the second write dropped the first's
    question."""
    vault = tmp_path / "vault"
    vault.mkdir()
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(ROOT)!r})
        import question_history
        store = question_history.QuestionHistory({str(vault)!r})
        for i in range(30):
            store.add(f"{{sys.argv[1]}}-{{i}}")
    """)
    procs = [subprocess.Popen([PY, "-c", script, f"p{n}"],
                              stderr=subprocess.PIPE, text=True)
             for n in range(4)]
    for proc in procs:
        _out, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err
    import question_history
    asked = [q["q"] for q in question_history.QuestionHistory(vault).all()]
    assert len(asked) == 120, f"{120 - len(asked)} questions were lost"
    assert not list(vault.glob("*.lock")) and not list(vault.glob("*.tmp"))


# ============================================================
# Naming
# ============================================================

def test_a_session_is_named_once_and_the_file_moves(tmp_path):
    vault = tmp_path / "vault"
    conv = Conversation(vault)
    old = conv.session_id
    conv.record("User", "why is Q3 revenue short?")
    seen = []
    conv.subscribe(lambda event, **d: seen.append((event, d)))
    outcome = conv.rename(conversations.name_from_question(
        "why is Q3 revenue short?"))
    assert outcome.ok, outcome.message
    assert conv.session_id == "q3_revenue_short"
    assert not (vault / "conversations" / f"{old}.jsonl").exists()
    assert (vault / "conversations" / "q3_revenue_short.jsonl").exists()
    assert seen == [("renamed", {"old": old, "new": "q3_revenue_short"})]
    assert not conv.rename("again").ok, "named twice"


def test_a_taken_name_is_never_written_over(tmp_path):
    vault = tmp_path / "vault"
    other = ConversationStore(vault / "conversations")
    other.append("pump_check", {"who": "User", "text": "someone else's"})
    before = (vault / "conversations" / "pump_check.jsonl").read_bytes()
    conv = Conversation(vault)
    conv.record("User", "pump check")
    assert conv.rename("pump_check").ok
    assert conv.session_id == "pump_check_2"
    assert (vault / "conversations" / "pump_check.jsonl").read_bytes() == before


def test_nothing_saved_is_not_named(tmp_path):
    conv = Conversation(tmp_path / "vault")
    assert not conv.rename("anything").ok
    assert conversations.RAW_SESSION_ID.match(conv.session_id)


@pytest.mark.parametrize("question, name", [
    ("why is Q3 revenue short?", "q3_revenue_short"),
    ("What is the pump in bay 4 called?", "pump_bay_4_called"),
    ("?!", "council_session"),
    ("the", "the"),
])
def test_a_name_from_the_question(question, name):
    assert conversations.name_from_question(question) == name


# ============================================================
# Load as Prior
# ============================================================

def test_load_prior_refuses_this_conversation_and_a_missing_one(tmp_path):
    conv = Conversation(tmp_path / "vault")
    conv.record("User", "hello")
    assert not conv.load_prior(conv.session_id).ok
    assert not conv.load_prior("no_such_session").ok
    assert conv.prior_session_id is None


def test_load_prior_sets_it_and_says_so(tmp_path):
    vault = tmp_path / "vault"
    ConversationStore(vault / "conversations").append(
        "older", {"who": "User", "text": "old"})
    conv = Conversation(vault)
    said = []
    conv.subscribe(lambda event, **d: said.append((event, d["message"])))
    assert conv.load_prior("older").ok
    assert conv.snapshot().prior_session_id == "older"
    assert said and said[0][0] == "prior_loaded" and "older" in said[0][1]
    assert conv.clear_prior().ok
    assert conv.snapshot().prior_session_id is None
    assert said[-1][0] == "prior_cleared"


def test_apply_to_points_every_personality_at_this_session(tmp_path):
    import council_engine as ce

    class Models:
        pass

    models = Models()
    models.personalities = {
        role: ce.PersonalityModel(name=role, system_prompt="", weights={},
                                  registry=None, session_id="qt")
        for role in ("writer", "judge")}
    models.writer = models.personalities["writer"]
    models.stand_in = object()
    conv = Conversation(tmp_path / "vault", scope="conversation")
    conv.prior_session_id = "older"
    conv.apply_to(models)
    for model in models.personalities.values():
        assert model.session_id == conv.session_id
        assert model.prior_session_id == "older"
        assert model.memory_scope == "conversation"


def test_a_verdict_line_is_tks_record_plus_its_id(tmp_path):
    from council_core.council_turn import TurnResult
    from council_core.deliberation import AgentEvent
    events = [AgentEvent("Judge", "observation", "Verdict: PASS (round 1)")]
    result = TurnResult(True, answer="a", critique="Verdict: PASS",
                        confidence=8, route="chat", events=events,
                        verdict_id="chat:1:PASS:abc")
    conv = Conversation(tmp_path / "vault")
    assert conv.record_verdict(conversations.verdict_record(
        result, conv.session_id, "why?"))
    (row,) = _lines(tmp_path / "vault" / "verdict_history.jsonl")
    assert row["session_id"] == conv.session_id and row["passed"] is True
    assert row["confidence"] == 8 and row["rounds"] == 1
    assert row["verdict_id"] == "chat:1:PASS:abc"
    assert set(row) >= {"ts", "route", "query"}


def test_the_module_imports_no_toolkit():
    source = (ROOT / "council_core" / "conversations.py").read_text(
        encoding="utf-8")
    for toolkit in ("tkinter", "PySide6", "import council_engine"):
        assert toolkit not in source


def test_a_history_that_cannot_be_read_is_never_written_over(tmp_path):
    """add() took any read failure for "no history yet" and wrote a one-item
    list over the file: a moment's refusal (another process swapping it in)
    or a damaged file cost every earlier question."""
    import question_history
    vault = tmp_path / "vault"
    vault.mkdir()
    path = vault / "question_history.json"
    path.write_text('[{"q": "kept", "ts": 1}, {"q": "half', encoding="utf-8")
    before = path.read_bytes()
    question_history.QuestionHistory(vault).add("new question")
    assert path.read_bytes() == before
