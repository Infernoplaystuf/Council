"""
A fact planted in a temp-vault file reaches the Writer's prompt and the
answer's sources — in both shells, through the one seam
(council_core.turn_context.prepare_turn).

The survey (docs/qt_migration/remaining_scope_2026-10-06.md section 1): the Qt
Council sent only the typed question; a planted fact reached 0 of 13 prompts
with deliberation on and 0 of 1 in the default build, while Tk put it in the
Writer's prompt, used it, and showed a file chip. Here:

  * Qt, built the way the app builds it (council_qt.tabs.council
    .build_council, which reads branding.DEMO_MODE): the default build (one
    Writer) and the full panel (DI_DEMO_MODE=0);
  * Tk, the REAL _send and _poll_ui_queue on a stand-in console (no window:
    tests/no_tk_guard.py), with the real personalities;

all against one FakeOllama that records every prompt and answers the Writer
from what it was given. The question names no file: the value search has to
find it.
"""
from __future__ import annotations

import collections
import json
import os
import queue
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
    from council_core import local_models, turn_context
    from tests.fake_ollama import FakeOllama, refuse_egress

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
        # The Tk console's process-wide indexes start empty for this vault.
        for name in ("_VAULT_INDEX_INSTANCE", "_DATA_INDEX_INSTANCE",
                     "_REFERENCED_FILES_MAP"):
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
        chips = [chip.text() for chip in view.source_chips]
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


# ============================================================
# Tk: the real _send on a stand-in console
# ============================================================

class _Anything:
    """Every widget and Tk variable the stand-in does not care about:
    callable, and .get() is False, so switches read as off."""

    def __call__(self, *_a, **_k):
        return None

    def get(self, *_a, **_k):
        return False

    def __getattr__(self, _name):
        return _Anything()


class _Text:
    def __init__(self, text=""):
        self.text = text

    def get(self, *_a):
        return self.text


class _Recording(queue.Queue):
    """The console's ui queue, remembering whether the worker said done."""

    def __init__(self):
        super().__init__()
        self.done = False

    def put(self, item, *args, **kwargs):
        if item and item[0] in ("done", "error"):
            self.done = True
        super().put(item, *args, **kwargs)


def _tk_ask(monkeypatch, vault: Path):
    import conversation_logger
    import council_engine as ce
    import council_gui_engine as cge
    import provenance
    import task_memory
    from council_core import conversations

    monkeypatch.setattr(cge, "VAULT_DIR", vault)
    C = cge.CouncilConsole

    class Console:
        _send = C._send
        _poll_ui_queue = C._poll_ui_queue
        _turn_state = C._turn_state
        _current_pinned_filenames = C._current_pinned_filenames
        _pin_state = C._pin_state
        _update_pins_from_response = C._update_pins_from_response
        _resolve_source_paths = C._resolve_source_paths
        PIN_LIFETIME_TURNS = C.PIN_LIFETIME_TURNS
        _LISTING_INTENT_RE = C._LISTING_INTENT_RE

        def __init__(self):
            self.input = _Text(QUESTION)
            self.ui_q = _Recording()
            self.said = []
            self.chips = []
            self.task_memory = task_memory.TaskMemory()
            self.provenance = provenance.ProvenanceTracker(max_turns=20)
            self._referenced_files = collections.OrderedDict()
            self._fast_answers_enabled = True
            self._turn_state_obj = None
            self._pinned_files_map = None
            self._pin_turn_counter = None
            self._force_full_council = False
            self._stream_buffers = {}
            self.convo_store = ce.ConversationStore(vault / "conversations")
            self.conv_logger = conversation_logger.ConversationLogger(vault)
            self.session_id, _problem = conversations.claim_session_id(
                self.convo_store, self.conv_logger.log_dir)
            self.conv_logger.start_session(self.session_id)
            self.personalities = ce.build_personalities(
                pins={}, vault_dir=vault, session_id=self.session_id,
                trace=False)
            for role, model in self.personalities.items():
                setattr(self, "librarian_personality" if role == "librarian"
                        else role, model)

        def _append_transcript(self, who, text, kind="final"):
            self.said.append((who, text, kind))

        def _render_source_chips(self, raw_sources):
            # The real method draws chips in a Tk Text; what it draws is
            # the files _resolve_source_paths finds — the real one.
            self.chips.append([p.name for p in
                               self._resolve_source_paths(raw_sources)])

        def __getattr__(self, _name):
            return _Anything()

    console = Console()
    console._send()
    deadline = time.monotonic() + 120
    while not console.ui_q.done and time.monotonic() < deadline:
        time.sleep(0.02)
    assert console.ui_q.done, "the Tk worker never finished"
    while not console.ui_q.empty():
        console._poll_ui_queue()
    console.conv_logger.flush()
    return console


def test_tk_gives_the_writer_the_fact_and_cites_it(engine, monkeypatch):
    console = _tk_ask(monkeypatch, engine.vault)
    prompts = _writer_prompts(engine.fake)
    assert len(prompts) == 1, prompts
    assert FACT in prompts[0], prompts[0]
    assert prompts[0].rstrip().endswith(QUESTION)
    finals = [text for who, text, kind in console.said
              if who == "Writer" and kind == "final"]
    assert finals and FACT in finals[-1], console.said
    # The file the block was read from (a full path since the review:
    # a bare name was resolved to the first file of that name).
    assert [Path(s).name for s in console._last_turn_sources] == [
        "pumps.csv"]
    assert console.chips == [["pumps.csv"]]
    assert not engine.refused


def test_both_shells_give_the_writer_the_same_context(qapp, engine,
                                                      monkeypatch):
    """One seam: the same vault and question give both shells' Writers the
    same vault text in front of the question. (What comes BEFORE the task
    differs by design: the Qt shell's "This conversation only" adds RECENT
    CONVERSATION; the Tk shell's SILO adds nothing.)"""
    _tk_ask(monkeypatch, engine.vault)
    tk_prompt = _writer_prompts(engine.fake)[-1]
    first = len(engine.fake.state.chats)
    _qt_ask(qapp, monkeypatch, demo_mode=True)
    qt_prompt = [rest for system, rest in
                 map(_split, engine.fake.state.chats[first:])
                 if WRITER in system][-1]

    def vault_part(prompt):
        return prompt[prompt.index("USER TASK:"):prompt.rindex(QUESTION)]

    assert "RECENT CONVERSATION" not in tk_prompt, "Tk's SILO changed"
    assert "RECENT CONVERSATION" in qt_prompt, "Qt's scope changed"

    import re
    tokens = re.compile(r"~[\d,]+ tokens")
    assert FACT in vault_part(tk_prompt) and FACT in vault_part(qt_prompt)
    assert (tokens.sub("", vault_part(tk_prompt))
            == tokens.sub("", vault_part(qt_prompt)))
