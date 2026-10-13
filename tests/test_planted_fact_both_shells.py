"""
A fact planted in a temp-vault file reaches the Writer's prompt and the
answer's sources in the Qt Council, through council_core.turn_context
.prepare_turn.

The survey (docs/qt_migration/remaining_scope_2026-10-06.md section 1): the Qt
Council sent only the typed question; a planted fact reached 0 of 13 prompts
with deliberation on and 0 of 1 in the default build, while Tk put it in the
Writer's prompt, used it, and showed a file chip. Here the Qt Council, built
the way the app builds it (council_qt.tabs.council.build_council, which reads
branding.DEMO_MODE): the default build (one Writer) and the full panel
(DI_DEMO_MODE=0), against a FakeOllama that records every prompt and answers
the Writer from what it was given. The question names no file: the value
search has to find it.

(This file also ran the Tk shell's _send on a stand-in console and compared
the two shells; that half went when the Tk shell was retired — CLAUDE.md.)
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

pytest.importorskip("PySide6", reason="the Qt tab needs PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

FACT = "ORCHID"
QUESTION = "Which pump is in bay 4?"
WRITER = "WRITER of the Council"


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def _pump(app, predicate, timeout=120.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _split(body):
    """(system text, the rest) of one recorded chat."""
    messages = body.get("messages") or []
    system = "\n".join(m.get("content") or "" for m in messages
                       if m.get("role") == "system")
    rest = "\n".join(m.get("content") or "" for m in messages
                     if m.get("role") != "system")
    return system, rest


def _writer_answers(body):
    """The fake's reply: the Writer says what it was given about bay 4."""
    system, rest = _split(body)
    if WRITER in system:
        return (f"From pumps.csv: the pump in bay 4 is {FACT}."
                if FACT in rest else "I was not given the pump data.")
    return None


def _writer_prompts(fake):
    return [rest for system, rest in map(_split, fake.state.chats)
            if WRITER in system]


@pytest.fixture
def engine(tmp_path, monkeypatch):
    import council_engine as ce
    from council_core import local_models
    from tests.fake_ollama import FakeOllama, refuse_egress
    # Batch 2's module; absent on older code, where these tests are to fail
    # on their own assertions, not in the fixture (found in review).
    try:
        from council_core import turn_context
    except ImportError:
        turn_context = None

    vault = tmp_path / "vault"
    (vault / "data_in").mkdir(parents=True)
    (vault / "data_in" / "pumps.csv").write_text(
        f"pump,bay,name,pressure_bar\nP1,4,{FACT},12.5\nP2,5,LILAC,9.1\n",
        encoding="utf-8")
    with FakeOllama() as fake:
        fake.state.reply_fn = _writer_answers
        monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
        monkeypatch.setenv("COUNCIL_OLLAMA_HOST", fake.url)
        monkeypatch.setenv("COUNCIL_BACKEND", "ollama")
        monkeypatch.setenv("COUNCIL_OLLAMA_MODEL", "phi3.5")
        monkeypatch.setenv("COUNCIL_OLLAMA_FALLBACK", "0")
        for var in ("COUNCIL_GGUF_PATH", "COUNCIL_MEMORY_SCOPE",
                    "COUNCIL_DEMO_SILO", "COUNCIL_REMOTE_NODES",
                    "COUNCIL_OLLAMA_NUM_CTX", "COUNCIL_GGUF_N_CTX",
                    "COUNCIL_AGENT_MEMORY_ENABLE"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setitem(sys.modules, "llama_cpp", None)
        monkeypatch.setattr(ce, "_hardware_gb", lambda: (8.0, 31.7))
        monkeypatch.setattr(ce, "_OLLAMA_CPT", {})
        # The module-wide indexes (used outside a prepared turn) start empty
        # for this vault.
        for name in ("_VAULT_INDEX_INSTANCE", "_DATA_INDEX_INSTANCE",
                     "_REFERENCED_FILES_MAP"):
            if turn_context is not None:
                monkeypatch.setattr(turn_context, name, None)
        local_models.invalidate_cache()
        ce.refresh_backend_config()
        refused = refuse_egress(monkeypatch)
        yield SimpleNamespace(ce=ce, vault=vault, fake=fake, refused=refused)
    ce.refresh_backend_config()
    local_models.invalidate_cache()


# ============================================================
# Qt: the tab the app builds
# ============================================================

def _qt_ask(qapp, monkeypatch, demo_mode: bool):
    """Build the Council tab as the app does, for DI_DEMO_MODE on or off,
    ask QUESTION, and return what the tab and its conversation hold."""
    import branding
    from council_qt.tabs.council import build_council
    from council_qt.window import CouncilWindow

    monkeypatch.setenv("DI_DEMO_MODE", "1" if demo_mode else "0")
    monkeypatch.setattr(branding, "DEMO_MODE", demo_mode)
    window = CouncilWindow()
    view = build_council(window)
    shown = []
    view.open_source = shown.append
    view.reveal_source = shown.append
    try:
        assert view.demo_mode is demo_mode
        view.input.setPlainText(QUESTION)
        view.on_send()
        assert _pump(qapp, lambda: not view._turn_active, timeout=300), (
            "the turn never finished")
        for _ in range(20):
            qapp.processEvents()
        chips = [chip.text() for chip in getattr(view, "source_chips", [])]
        route, answer = view._last_route, view._last_answer
        session = view.conversation.session_id
        vault = view.actions.vault_dir
    finally:
        window.request_close()
        view.deleteLater()
        qapp.processEvents()
    rows = [json.loads(line) for line in
            (vault / "conversations" / f"{session}.jsonl")
            .read_text(encoding="utf-8").splitlines() if line]
    return SimpleNamespace(chips=chips, route=route, answer=answer, rows=rows)


def _answer_sources(rows):
    """The sources saved with the answer in the conversation file."""
    answers = [r for r in rows if r.get("provenance")]
    assert answers, f"no answer was saved with its provenance: {rows}"
    return [Path(p).name for p in answers[-1]["provenance"]["sources"]]


def test_qt_default_build_gives_the_writer_the_fact_and_cites_it(
        qapp, engine, monkeypatch):
    got = _qt_ask(qapp, monkeypatch, demo_mode=True)
    assert got.route == "direct", "the default build is one Writer"
    prompts = _writer_prompts(engine.fake)
    assert len(prompts) == 1, prompts
    assert FACT in prompts[0], prompts[0]
    assert prompts[0].rstrip().endswith(QUESTION)
    assert FACT in got.answer, "the Writer did not answer from the file"
    assert got.chips == ["📄 pumps.csv"]
    assert _answer_sources(got.rows) == ["pumps.csv"]
    assert not engine.refused


def test_qt_full_panel_gives_the_writer_the_fact_and_cites_it(
        qapp, engine, monkeypatch):
    """DI_DEMO_MODE=0: deliberation is on by default and the panel runs."""
    got = _qt_ask(qapp, monkeypatch, demo_mode=False)
    assert got.route not in (None, "direct", "data"), got.route
    prompts = _writer_prompts(engine.fake)
    assert prompts, "the Writer was never asked"
    assert all(FACT in p for p in prompts), (
        f"{sum(FACT in p for p in prompts)} of {len(prompts)} Writer "
        f"prompts carried the file")
    assert FACT in got.answer
    assert got.chips == ["📄 pumps.csv"]
    assert _answer_sources(got.rows) == ["pumps.csv"]
    assert not engine.refused


def test_qt_follow_up_gets_this_conversation_and_the_vault_together(
        qapp, engine, monkeypatch):
    """The two blocks of the seam in one prompt: turn 2's Writer is given
    turn 1 (RECENT CONVERSATION, "This conversation only") AND the vault
    file — and an older session in the same vault stays out."""
    import branding
    from council_qt.tabs.council import build_council
    from council_qt.window import CouncilWindow

    older = engine.vault / "conversations" / "older_pump_talk.jsonl"
    older.parent.mkdir(parents=True, exist_ok=True)
    older.write_text(json.dumps({"who": "User", "text":
                                 "the old pump is called WALNUT"}) + "\n",
                     encoding="utf-8")
    monkeypatch.setattr(branding, "DEMO_MODE", True)
    window = CouncilWindow()
    view = build_council(window)
    view.open_source = view.reveal_source = (lambda _p: None)
    try:
        for question in ("Note: the spare pump is called KIWI.", QUESTION):
            view.input.setPlainText(question)
            view.on_send()
            assert _pump(qapp, lambda: not view._turn_active), question
            for _ in range(20):
                qapp.processEvents()
    finally:
        window.request_close()
        view.deleteLater()
        qapp.processEvents()
    last = _writer_prompts(engine.fake)[-1]
    conversation, _sep, task = last.partition("USER TASK:")
    assert "KIWI" in conversation, last
    assert FACT in task, last
    assert "WALNUT" not in last
