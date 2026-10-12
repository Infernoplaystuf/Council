"""
The Tk shell answers from the vault through the SAME function as Qt.

The Tk console's _send used to build the turn's context inline (280 lines);
it now calls council_core.turn_context.prepare_turn, as the Qt Council does.
This runs the REAL Tk `_send` — no window: tests must never open one
(tests/no_tk_guard.py) — on a stand-in console, with a recording Writer, and
checks that a fact planted in a temp vault reaches the Writer's prompt and
the transcript says what it always said.
"""
from __future__ import annotations

import collections
import queue
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
FACT = "ORCHID"


class _Anything:
    """Every widget and Tk variable the stand-in does not care about: callable,
    and .get() is False, so switches read as off."""

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


class _Writer:
    """The Writer, recording what it is asked."""

    def __init__(self):
        self.asked = []
        self.conversation_store = None

    def respond(self, user_text, **_kwargs):
        self.asked.append(user_text)
        return "From the file: the pump in bay 4 is ORCHID."


def _console(cge, question):
    import provenance
    import task_memory

    C = cge.CouncilConsole

    class Console:
        _send = C._send
        _turn_state = C._turn_state
        _current_pinned_filenames = C._current_pinned_filenames
        _pin_state = C._pin_state
        PIN_LIFETIME_TURNS = C.PIN_LIFETIME_TURNS
        _LISTING_INTENT_RE = C._LISTING_INTENT_RE

        def __init__(self):
            self.input = _Text(question)
            self.said = []
            self.ui_q = queue.Queue()
            self.task_memory = task_memory.TaskMemory()
            self.provenance = provenance.ProvenanceTracker(max_turns=20)
            self._referenced_files = collections.OrderedDict()
            self._fast_answers_enabled = True
            self.writer = _Writer()
            self.personalities = {"writer": self.writer}
            # State the real methods create on first use (they test for
            # None, which __getattr__ below would otherwise answer).
            self._turn_state_obj = None
            self._pinned_files_map = None
            self._pin_turn_counter = None
            self._force_full_council = False

        def _append_transcript(self, who, text, kind="final"):
            self.said.append((who, text, kind))

        def __getattr__(self, _name):
            return _Anything()

    return Console()


def _fresh_module_indexes(monkeypatch):
    """The Tk console keeps ONE vault index and data index for the process
    (module_indexes=True), built for the first vault it reads. A test's own
    vault needs its own: without this, the index a test before built for
    ITS vault was searched (found when test_shell_parity ran after this
    file: its empty Tk vault "matched" the previous test's pumps.csv)."""
    from council_core import turn_context
    for name in ("_VAULT_INDEX_INSTANCE", "_DATA_INDEX_INSTANCE",
                 "_REFERENCED_FILES_MAP"):
        monkeypatch.setattr(turn_context, name, None)


@pytest.fixture
def tk_engine(tmp_path, monkeypatch):
    import council_engine as ce
    import council_gui_engine as cge
    from council_core import local_models
    from tests.fake_ollama import FakeOllama, refuse_egress
    vault = tmp_path / "vault"
    (vault / "data_in").mkdir(parents=True)
    (vault / "data_in" / "pumps.csv").write_text(
        f"pump,bay,name,pressure_bar\nP1,4,{FACT},12.5\nP2,5,LILAC,9.1\n",
        encoding="utf-8")
    with FakeOllama() as fake:
        monkeypatch.setattr(cge, "VAULT_DIR", vault)
        _fresh_module_indexes(monkeypatch)
        monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
        monkeypatch.setenv("COUNCIL_OLLAMA_HOST", fake.url)
        monkeypatch.setenv("COUNCIL_BACKEND", "ollama")
        monkeypatch.setenv("COUNCIL_OLLAMA_MODEL", "phi3.5")
        monkeypatch.setenv("COUNCIL_OLLAMA_FALLBACK", "0")
        monkeypatch.setitem(sys.modules, "llama_cpp", None)
        local_models.invalidate_cache()
        ce.refresh_backend_config()
        refuse_egress(monkeypatch)
        yield cge, vault
    ce.refresh_backend_config()
    local_models.invalidate_cache()


def _send(cge, question, *, worker=True):
    """Run _send; with worker, wait for its worker's "done" (a fast
    answer from the data returns without starting one)."""
    console = _console(cge, question)
    console._send()
    deadline = time.monotonic() + (60 if worker else 0)
    events = []
    while time.monotonic() < deadline:
        try:
            item = console.ui_q.get(timeout=0.1)
        except queue.Empty:
            continue
        events.append(item)
        if item[0] in ("done", "error"):
            break
    return console, events


def test_the_tk_writer_is_given_the_planted_file(tk_engine):
    cge, _vault = tk_engine
    console, events = _send(cge, "Summarize pumps.csv")
    assert ("done", None) in events, events
    assert console.writer.asked, "the Writer was never asked"
    assert FACT in console.writer.asked[-1]
    assert console.writer.asked[-1].rstrip().endswith("Summarize pumps.csv")
    # The file the block was read from (a full path since the review:
    # a bare name was resolved to the first file of that name).
    assert [Path(s).name for s in console._last_turn_sources] == [
        "pumps.csv"]
    said = [text for _who, text, _kind in console.said]
    assert any(text.startswith("Tracking → goal:") for text in said), said


def test_a_tk_count_is_answered_from_the_data(tk_engine):
    cge, _vault = tk_engine
    console, _events = _send(cge, "How many files are in the vault?",
                             worker=False)
    assert not console.writer.asked, "a model was asked for a count"
    finals = [text for who, text, kind in console.said
              if who == "Writer" and kind == "final"]
    assert finals and "file(s)" in finals[0]


def test_tk_sends_through_prepare_turn():
    """Not a copy of it: the context block is the one shared call."""
    source = (ROOT / "council_gui_engine.py").read_text(encoding="utf-8")
    send = source[source.index("    def _send(self):"):]
    send = send[:send.index("\n    def ", 10)]
    assert "_turn_context.prepare_turn(" in send
    assert "_inject_file_contents(" not in send
    assert "_run_analyst_step(" not in send
