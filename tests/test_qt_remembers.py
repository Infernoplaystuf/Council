"""
"Qt remembers" (Batch 1) — the real Qt Council tab, the real engine, a fake
Ollama that records every prompt.

The user's decision (6 October 2026) is "This conversation only": a follow-up
sees this session's earlier turns; an older session, learned role/project
memory and the profile stay out of every prompt — unless the user loads ONE
older session on purpose (Load as Prior), which the transcript says.

Before Batch 1 the Qt shell saved nothing (so turn 2 never saw turn 1) and,
with SILO unset, put the role memory, project memory and profile the Tk shell
wrote into every prompt. Each fact below is planted where a prompt could
remember it from; the prompts the fake Ollama received are the evidence.

Nothing here reaches a real model: the engine talks to tests/fake_ollama.py on
loopback, and refuse_egress fails the test on any other connection.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

pytest.importorskip("PySide6", reason="the Qt tab needs PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

FACTS = {
    "turn": "ORCHID",          # said in turn 1 of this conversation
    "older": "TANGERINE",      # in an older saved session (and in History)
    "role": "WALNUT",          # the Writer's role memory
    "project": "KESTREL",      # the shared project memory
    "profile": "HAIKU",        # the compiled user profile
}
OLDER = "older_pump_talk"


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def _pump(app, predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """The engine against a FakeOllama, a temp vault with a fact planted in
    every place a prompt can remember from."""
    import council_engine as ce
    from council_core import local_models
    from tests.fake_ollama import FakeOllama, refuse_egress

    vault = tmp_path / "vault"
    vault.mkdir()
    with FakeOllama() as fake:
        monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
        monkeypatch.setenv("COUNCIL_OLLAMA_HOST", fake.url)
        monkeypatch.setenv("COUNCIL_BACKEND", "ollama")
        monkeypatch.setenv("COUNCIL_OLLAMA_MODEL", "phi3.5")
        monkeypatch.setenv("COUNCIL_OLLAMA_FALLBACK", "0")
        for var in ("COUNCIL_GGUF_PATH", "COUNCIL_OLLAMA_NUM_CTX",
                    "COUNCIL_OLLAMA_KEEP_ALIVE", "COUNCIL_GGUF_N_CTX",
                    "COUNCIL_AGENT_MEMORY_ENABLE", "COUNCIL_REMOTE_NODES",
                    "COUNCIL_MEMORY_SCOPE", "COUNCIL_DEMO_SILO",
                    "COUNCIL_QUIRKS_APPLY"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setitem(sys.modules, "llama_cpp", None)
        monkeypatch.setattr(ce, "_hardware_gb", lambda: (8.0, 31.7))
        monkeypatch.setattr(ce, "_OLLAMA_CPT", {})
        local_models.invalidate_cache()
        ce.refresh_backend_config()
        refused = refuse_egress(monkeypatch)

        # An older session, written the way the Tk shell writes one.
        older = ce.ConversationStore(vault / "conversations")
        older.append(OLDER, {"ts": "2026-10-01T09:00:00", "who": "User",
                             "text": f"the spare impeller is "
                                     f"{FACTS['older']}"})
        older.append(OLDER, {"ts": "2026-10-01T09:00:09", "who": "Writer",
                             "text": "Noted."})
        memory = ce.RoleMemoryManager(vault / "memory")
        memory.update("writer", f"- the user likes {FACTS['role']}")
        memory.update(ce._PROJECT_MEMORY_KEY,
                      f"- the plant is in {FACTS['project']}")
        memory.update(ce._USER_PROFILE_KEY,
                      f"- always answer as a {FACTS['profile']}")
        # ...and the older session's question in History, as Tk adds it.
        import question_history
        question_history.QuestionHistory(vault).add(
            f"the spare impeller is {FACTS['older']}")

        yield SimpleNamespace(ce=ce, vault=vault, fake=fake, refused=refused)
    ce.refresh_backend_config()
    local_models.invalidate_cache()


def _council(qapp, vault, *, demo_mode=True):
    from council_qt.tabs.council import CouncilActions, CouncilTab
    from council_qt.window import CouncilWindow
    window = CouncilWindow()
    view = CouncilTab(window, actions=CouncilActions(vault_dir=vault,
                                                     demo_mode=demo_mode),
                      demo_mode=demo_mode)
    return window, view


def _ask(qapp, view, question, timeout=60.0):
    view.input.setPlainText(question)
    view.on_send()
    assert _pump(qapp, lambda: not view._turn_active, timeout=timeout), (
        "the turn never finished")
    for _ in range(20):
        qapp.processEvents()


def _prompts(fake, since=0):
    """The user-side text of every chat the fake received after ``since``."""
    out = []
    for body in fake.state.chats[since:]:
        out.append("\n".join(m.get("content") or ""
                             for m in body.get("messages") or []
                             if m.get("role") != "system"))
    return out


def _found(prompt):
    return {name for name, fact in FACTS.items() if fact in prompt}


def _close(qapp, window, view):
    window.request_close()
    view.deleteLater()
    qapp.processEvents()


def _session_rows(vault, session_id):
    path = vault / "conversations" / f"{session_id}.jsonl"
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line]


# ============================================================
# The default build: one Writer
# ============================================================

def test_a_follow_up_sees_this_conversation_and_nothing_older(qapp, engine):
    window, view = _council(qapp, engine.vault)
    try:
        _ask(qapp, view, f"Remember: the pump in bay 4 is called "
                         f"{FACTS['turn']}.")
        first = len(engine.fake.state.chats)
        assert first >= 1, "turn 1 reached no model"
        # History lists the older session's question on screen — and that
        # listing must not become part of this conversation.
        view.on_history()
        assert FACTS["older"] in view.transcript.toPlainText()
        view.on_examples()
        _ask(qapp, view, "What is the pump in bay 4 called?")
        prompts = _prompts(engine.fake, since=first)
        assert prompts, "turn 2 reached no model"
        assert "turn" in _found(prompts[-1]), (
            "turn 1's fact did not reach turn 2's prompt:\n" + prompts[-1])
        for prompt in _prompts(engine.fake):
            leaked = _found(prompt) - {"turn"}
            assert not leaked, f"{sorted(leaked)} reached a prompt:\n{prompt}"
        assert not engine.refused
    finally:
        _close(qapp, window, view)


def test_load_as_prior_is_the_one_way_an_older_session_gets_in(qapp, engine):
    from council_qt.tabs.sessions import SessionsTab
    window, view = _council(qapp, engine.vault)
    sessions = SessionsTab(window)
    try:
        _ask(qapp, view, "What is the spare impeller called?")
        before = _prompts(engine.fake)
        assert before and all("older" not in _found(p) for p in before)

        assert _pump(qapp, lambda: any(r.id == OLDER for r in sessions._shown))
        sessions.sessions.setCurrentRow(
            [r.id for r in sessions._shown].index(OLDER))
        sessions.on_load_prior()
        assert _pump(qapp, lambda: "Loaded the earlier conversation"
                     in view.transcript.toPlainText()), (
            "the transcript does not say an older session was loaded")
        assert OLDER in sessions.prior.text()

        since = len(engine.fake.state.chats)
        _ask(qapp, view, "And now — what is the spare impeller called?")
        after = _prompts(engine.fake, since=since)
        assert "older" in _found(after[-1]), after[-1]
        assert not (_found(after[-1]) & {"role", "project", "profile"})

        sessions.on_clear_prior()
        since = len(engine.fake.state.chats)
        _ask(qapp, view, "One more question.")
        assert "older" not in _found(_prompts(engine.fake, since=since)[-1])
    finally:
        sessions.deleteLater()
        _close(qapp, window, view)


def test_the_conversation_is_saved_as_tk_saves_it(qapp, engine):
    """The same files, the same place, the same format — and a Tk reader
    (vault_tools.recent_queries) finds the Qt question."""
    window, view = _council(qapp, engine.vault)
    try:
        started = view.conversation.session_id
        _ask(qapp, view, "Why is Q3 revenue short?")
        session = view.conversation.session_id
        assert session != started, "the session was not named"
        assert session == "q3_revenue_short"
        rows = _session_rows(engine.vault, session)
        assert rows[0]["who"] == "User"
        assert rows[0]["text"] == "Why is Q3 revenue short?"
        answer = next(r for r in rows if r["who"] == "Writer")
        assert answer["text"] == "Hello from the fake."
        assert all(set(r) >= {"ts", "who", "text"} for r in rows)

        provenance = answer["provenance"]
        assert provenance["shell"] == "qt" and provenance["route"] == "direct"
        assert provenance["memory"] == "conversation"
        assert provenance["prior_session"] is None
        assert "This conversation is saved as 'q3_revenue_short'" in (
            view.transcript.toPlainText())

        import vault_tools
        asked = [q["text"] for q in vault_tools.recent_queries(engine.vault)]
        assert "Why is Q3 revenue short?" in asked
        import question_history
        assert question_history.QuestionHistory(engine.vault).recent(1)[0][
            "q"] == "Why is Q3 revenue short?"
    finally:
        _close(qapp, window, view)
    log = (engine.vault / "conversation_logs"
           / f"session_{started}.jsonl").read_text(encoding="utf-8")
    kinds = [json.loads(line)["kind"] for line in log.splitlines()]
    assert kinds[0] == "session_start" and kinds[-1] == "session_end"


def test_the_sessions_list_holds_tk_and_qt_sessions(qapp, engine):
    from council_qt.tabs.sessions import SessionsTab
    window, view = _council(qapp, engine.vault)
    try:
        _ask(qapp, view, "Pump pressure check")
        sessions = SessionsTab(window)
        assert _pump(qapp, lambda: len(sessions._shown) >= 2)
        labels = {r.id: r.label for r in sessions._shown}
        assert OLDER in labels, "the Tk session is missing"
        current = view.conversation.session_id
        assert labels[current].endswith("(this conversation)")
        sessions.sessions.setCurrentRow(
            [r.id for r in sessions._shown].index(OLDER))
        sessions.on_preview()
        assert _pump(qapp, lambda: FACTS["older"]
                     in sessions.preview.toPlainText())
        sessions.deleteLater()
    finally:
        _close(qapp, window, view)


def test_the_question_is_saved_before_the_answer_and_close_ends_the_log(
        qapp, monkeypatch, tmp_path):
    """A crash mid-turn keeps the question; closing the window mid-turn
    ends the log cleanly — the window's close hooks run the save."""
    from council_core import council_turn
    from tests.test_council_turn import FakeModel, Models

    waiting, release = threading.Event(), threading.Event()

    class Slow(FakeModel):
        def respond(self, prompt, **kwargs):
            waiting.set()
            release.wait(10)
            return "late"

    monkeypatch.setattr(council_turn, "load_personalities",
                        lambda *_a, **_k: (Models(writer=Slow()), ""))
    vault = tmp_path / "vault"
    window, view = _council(qapp, vault)
    try:
        view.input.setPlainText("Is the question kept?")
        view.on_send()
        assert _pump(qapp, waiting.is_set)
        session = view.conversation.session_id
        rows = _session_rows(vault, session)
        assert [(r["who"], r["text"]) for r in rows] == [
            ("User", "Is the question kept?")]
        window.request_close()
        log = (vault / "conversation_logs" / f"session_{session}.jsonl")
        kinds = [json.loads(line)["kind"] for line in
                 log.read_text(encoding="utf-8").splitlines()]
        assert kinds[-1] == "session_end"
    finally:
        release.set()
        _pump(qapp, lambda: not any(t.name == "council-turn" and t.is_alive()
                                    for t in threading.enumerate()))
        view.deleteLater()
        qapp.processEvents()


def test_the_memory_scope_is_shown(qapp, tmp_path, monkeypatch):
    from council_qt.tabs.council import CouncilActions, CouncilTab
    monkeypatch.delenv("COUNCIL_MEMORY_SCOPE", raising=False)
    monkeypatch.delenv("COUNCIL_DEMO_SILO", raising=False)
    view = CouncilTab(actions=CouncilActions(vault_dir=tmp_path / "v"))
    try:
        assert view.memory_label.text() == "Memory: this conversation only"
        assert "Load as Prior" in view.memory_label.toolTip()
        assert view.conversation.scope == "conversation"
    finally:
        view.deleteLater()


# ============================================================
# The full council (DI_DEMO_MODE=0): every role's prompt
# ============================================================

def test_the_full_council_keeps_every_role_to_this_conversation(qapp,
                                                                 engine):
    """The judge records a verdict, as the Tk shell does after a
    deliberation; no role's prompt carries an older session, memory or the
    profile."""
    window, view = _council(qapp, engine.vault, demo_mode=False)
    try:
        view._checkboxes["deliberate"].setChecked(True)
        _ask(qapp, view, f"Note: the pump in bay 4 is called "
                         f"{FACTS['turn']}.", timeout=180)
        first = len(engine.fake.state.chats)
        _ask(qapp, view, "What is the pump in bay 4 called?", timeout=180)
        prompts = _prompts(engine.fake)
        assert len(prompts) > first, "turn 2 reached no model"
        for prompt in prompts:
            leaked = _found(prompt) - {"turn"}
            assert not leaked, f"{sorted(leaked)} reached a prompt:\n{prompt}"
        assert any("turn" in _found(p) for p in prompts[first:]), (
            "no prompt in turn 2 carried turn 1")
        verdicts = engine.vault / "verdict_history.jsonl"
        if view._last_route and view._last_route != "direct":
            rows = [json.loads(line) for line in
                    verdicts.read_text(encoding="utf-8").splitlines()]
            assert rows and rows[-1]["session_id"] == \
                view.conversation.session_id
            assert rows[-1]["verdict_id"]
    finally:
        _close(qapp, window, view)


def test_a_full_council_turn_keeps_the_question_and_the_answer(qapp, engine):
    """Its drafts, cross-examination and rankings go to the debug log: kept in
    the conversation (as Tk keeps them) they filled the twelve entries a
    follow-up sees, and turn 1's question dropped out."""
    memory = {p.name: p.read_bytes()
              for p in (engine.vault / "memory").iterdir()}
    window, view = _council(qapp, engine.vault, demo_mode=False)
    try:
        view._checkboxes["deliberate"].setChecked(True)
        _ask(qapp, view, "Note: the pump is quiet.", timeout=180)
        # Learned memory is neither read nor WRITTEN in "This conversation
        # only": the Tk shell writes it after a deliberation (two model calls
        # a role); nothing here would ever use it.
        assert {p.name: p.read_bytes() for p in
                (engine.vault / "memory").iterdir()} == memory
        session = view.conversation.session_id
        rows = _session_rows(engine.vault, session)
        assert rows[0]["who"] == "User"
        answers = [r for r in rows if r["who"] == "Writer"]
        assert len(answers) == 1 and "provenance" in answers[0]
        assert answers[0]["provenance"]["route"] != "direct"
        assert {r["who"] for r in rows} <= {"User", "Writer", "Council"}, rows
    finally:
        _close(qapp, window, view)
    log = [json.loads(line) for line in
           next((engine.vault / "conversation_logs").glob("session_*.jsonl"))
           .read_text(encoding="utf-8").splitlines()]
    assert any(r["who"] == "Peasant" for r in log), (
        "the deliberation is not in the debug log either")
