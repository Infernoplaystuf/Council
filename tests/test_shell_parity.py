"""
The two shells keep the same record of the same turn.

Found in review: the Qt Council said it "writes the same files as Tk", but for
one default-build question against the same model the Tk conversation file
held [User, Council "Tracking → goal: …", Writer] and the Qt one held
[User, Writer, Council "This conversation is saved as …"] — Qt sent the turn's
own notices (prepare_turn's lines, the note under an answer from the data) to
the debug log only. Qt also never wrote logs/council.log, the Librarian's line
log Tk writes for every kept entry, and logged a turn's entries only when the
turn ended, under the session's NEW name.

Here one question is asked in each shell, each with its own temp vault, against
the same FakeOllama: the Tk side is the REAL CouncilConsole._send,
_append_transcript and _poll_ui_queue on a stand-in console (no window — tests
must never open one, tests/no_tk_guard.py) with the real ConversationStore,
ConversationLogger and Librarian; the Qt side is the real Council tab.
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

QUESTION = "What is the pump in bay 4 called?"

#: Lines only the Qt shell says, by design: it names a default-build session
#: from the question's words and says so (Tk names nothing there).
QT_ONLY = ("This conversation is saved as",)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def _pump(app, predicate, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def fake(tmp_path, monkeypatch):
    import council_engine as ce
    from council_core import local_models
    from tests.fake_ollama import FakeOllama, refuse_egress
    with FakeOllama() as server:
        monkeypatch.setenv("COUNCIL_OLLAMA_HOST", server.url)
        monkeypatch.setenv("COUNCIL_BACKEND", "ollama")
        monkeypatch.setenv("COUNCIL_OLLAMA_MODEL", "phi3.5")
        monkeypatch.setenv("COUNCIL_OLLAMA_FALLBACK", "0")
        for var in ("COUNCIL_GGUF_PATH", "COUNCIL_OLLAMA_NUM_CTX",
                    "COUNCIL_OLLAMA_KEEP_ALIVE", "COUNCIL_GGUF_N_CTX",
                    "COUNCIL_AGENT_MEMORY_ENABLE", "COUNCIL_REMOTE_NODES",
                    "COUNCIL_MEMORY_SCOPE"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setitem(sys.modules, "llama_cpp", None)
        monkeypatch.setattr(ce, "_hardware_gb", lambda: (8.0, 31.7))
        monkeypatch.setattr(ce, "_OLLAMA_CPT", {})
        local_models.invalidate_cache()
        ce.refresh_backend_config()
        refused = refuse_egress(monkeypatch)
        yield SimpleNamespace(server=server, refused=refused)
    ce.refresh_backend_config()
    local_models.invalidate_cache()


# ============================================================
# The Tk side: the real _send on a stand-in console
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


def _tk_turn(monkeypatch, vault: Path, question: str):
    import conversation_logger
    import council_engine as ce
    import council_gui_engine as cge
    import provenance
    import task_memory
    from council_core import conversations

    monkeypatch.setattr(cge, "VAULT_DIR", vault)
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
    C = cge.CouncilConsole

    class Console:
        _send = C._send
        _append_transcript = C._append_transcript
        _poll_ui_queue = C._poll_ui_queue
        _turn_state = C._turn_state
        _current_pinned_filenames = C._current_pinned_filenames
        _pin_state = C._pin_state
        _update_pins_from_response = C._update_pins_from_response
        PIN_LIFETIME_TURNS = C.PIN_LIFETIME_TURNS
        _LISTING_INTENT_RE = C._LISTING_INTENT_RE

        def __init__(self):
            self.input = _Text(question)
            self.ui_q = _Recording()
            self.task_memory = task_memory.TaskMemory()
            self.provenance = provenance.ProvenanceTracker(max_turns=20)
            self._referenced_files = collections.OrderedDict()
            self._fast_answers_enabled = True
            self._turn_state_obj = None
            self._pinned_files_map = None
            self._pin_turn_counter = None
            self._force_full_council = False
            self._stream_buffers = {}
            self.transcript = None
            self.dream3d_transcript = None
            # The stores the Tk console writes, as its __init__ makes them.
            self.convo_store = ce.ConversationStore(vault / "conversations")
            self.conv_logger = conversation_logger.ConversationLogger(vault)
            self.session_id, _problem = conversations.claim_session_id(
                self.convo_store, self.conv_logger.log_dir)
            self.conv_logger.start_session(self.session_id)
            self.librarian = ce.Librarian(vault, vault / "logs"
                                          / "council.log")
            self.personalities = ce.build_personalities(
                pins={}, vault_dir=vault, session_id=self.session_id,
                trace=False)
            # As _unpack_personalities does: the librarian ROLE is
            # librarian_personality (self.librarian is the log's Librarian).
            for role, model in self.personalities.items():
                setattr(self, "librarian_personality" if role == "librarian"
                        else role, model)

        def __getattr__(self, _name):
            return _Anything()

    console = Console()
    console._send()
    deadline = time.monotonic() + 60
    while not console.ui_q.done and time.monotonic() < deadline:
        time.sleep(0.02)
    assert console.ui_q.done, "the Tk worker never finished"
    while not console.ui_q.empty():
        console._poll_ui_queue()
    console.conv_logger.flush()
    return console.session_id


# ============================================================
# The Qt side: the real Council tab
# ============================================================

def _qt_turn(qapp, monkeypatch, vault: Path, question: str):
    from council_qt.tabs.council import CouncilActions, CouncilTab
    from council_qt.window import CouncilWindow
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
    window = CouncilWindow()
    view = CouncilTab(window, actions=CouncilActions(vault_dir=vault,
                                                     demo_mode=True),
                      demo_mode=True)
    try:
        started = view.conversation.session_id
        view.input.setPlainText(question)
        view.on_send()
        assert _pump(qapp, lambda: not view._turn_active), (
            "the Qt turn never finished")
        for _ in range(20):
            qapp.processEvents()
        return started, view.conversation.session_id
    finally:
        window.request_close()
        view.deleteLater()
        qapp.processEvents()


def _rows(vault: Path, session_id: str):
    path = vault / "conversations" / f"{session_id}.jsonl"
    return [(r["who"], r["text"]) for r in
            (json.loads(line) for line in
             path.read_text(encoding="utf-8").splitlines() if line)]


def _council_log(vault: Path):
    """(who: text lines without their timestamps, the raw bytes)."""
    raw = (vault / "logs" / "council.log").read_bytes()
    lines = [line.split("] ", 1)[1] for line in
             raw.decode("utf-8").splitlines() if line.startswith("[")]
    return lines, raw


def _qt_only(text: str) -> bool:
    return text.startswith(QT_ONLY)


def test_one_question_leaves_the_same_record_in_both_shells(
        qapp, fake, tmp_path, monkeypatch):
    tk_vault, qt_vault = tmp_path / "tk", tmp_path / "qt"
    tk_vault.mkdir()
    qt_vault.mkdir()

    tk_id = _tk_turn(monkeypatch, tk_vault, QUESTION)
    started, qt_id = _qt_turn(qapp, monkeypatch, qt_vault, QUESTION)

    tk_rows = _rows(tk_vault, tk_id)
    qt_rows = _rows(qt_vault, qt_id)
    assert [w for w, _t in tk_rows] == ["User", "Council", "Writer"], tk_rows
    assert tk_rows[1][1].startswith("Tracking → goal:"), tk_rows
    assert [row for row in qt_rows if not _qt_only(row[1])] == tk_rows, (
        f"\nTk: {tk_rows}\nQt: {qt_rows}")

    tk_log, tk_raw = _council_log(tk_vault)
    qt_log, qt_raw = _council_log(qt_vault)
    assert [line for line in qt_log
            if not _qt_only(line.split(": ", 1)[1])] == tk_log, (
        f"\nTk: {tk_log}\nQt: {qt_log}")
    # The same bytes per line: Tk appends in text mode (CRLF on Windows).
    assert tk_raw.endswith(os.linesep.encode())
    assert qt_raw.endswith(os.linesep.encode())
    assert qt_raw.count(b"\n") == qt_raw.count(os.linesep.encode())

    # The debug log took each entry as it was said, under the id it was said
    # under (the session was named after the answer).
    assert qt_id != started
    log = [json.loads(line) for line in
           (qt_vault / "conversation_logs" / f"session_{started}.jsonl")
           .read_text(encoding="utf-8").splitlines()]
    said = {r["text"]: r.get("meta", {}).get("session") for r in log
            if r.get("kind") in ("observation", "final")}
    answer = next(text for who, text in qt_rows if who == "Writer")
    assert said[answer] == started, said
    tracking = next(text for who, text in qt_rows
                    if text.startswith("Tracking"))
    assert said[tracking] == started, said
    assert not fake.refused


def test_an_answer_from_the_data_keeps_its_note_in_both_shells(
        qapp, fake, tmp_path, monkeypatch):
    """The fast path: no model is asked, and the note under the answer is
    the app's own line — kept in each shell's conversation."""
    from council_core import turn_context
    tk_vault, qt_vault = tmp_path / "tk", tmp_path / "qt"
    for vault in (tk_vault, qt_vault):
        (vault / "data_in").mkdir(parents=True)
        (vault / "data_in" / "pumps.csv").write_text(
            "pump,bay,name\nP1,4,ORCHID\nP2,5,LILAC\n", encoding="utf-8")
    question = "How many files are in the vault?"
    tk_id = _tk_turn_fast(monkeypatch, tk_vault, question)
    _started, qt_id = _qt_turn(qapp, monkeypatch, qt_vault, question)
    tk_rows = _rows(tk_vault, tk_id)
    qt_rows = [row for row in _rows(qt_vault, qt_id) if not _qt_only(row[1])]
    # The note's words differ by design (Qt has no Expand button yet); the
    # record's shape does not.
    assert [w for w, _t in qt_rows] == [w for w, _t in tk_rows], (
        f"\nTk: {tk_rows}\nQt: {qt_rows}")
    assert qt_rows[-1] == ("Council", turn_context.FAST_ANSWER_NOTE)
    assert "file(s)" in next(t for w, t in qt_rows if w == "Writer")


def _tk_turn_fast(monkeypatch, vault, question):
    """_tk_turn for a question answered from the data: _send returns
    without starting a worker."""
    import council_gui_engine as cge

    # A fast answer never puts "done"; it is done when _send returns.
    original = cge.CouncilConsole._send

    def send_then_done(self):
        original(self)
        self.ui_q.put(("done", None))

    monkeypatch.setattr(cge.CouncilConsole, "_send", send_then_done)
    try:
        return _tk_turn(monkeypatch, vault, question)
    finally:
        monkeypatch.setattr(cge.CouncilConsole, "_send", original)
