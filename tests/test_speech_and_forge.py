"""
Speech and Tool Creation — two small tabs, two different kinds of care.

Speech is small in widgets and not small in care: its worker reads a spinbox
from a background thread, which is the Council tab's thirteen-variable defect
in miniature, and nothing serialises playback.

Tool Creation is the opposite — the logic was already toolkit-free and already
returned (ok, message) pairs. What was not shared was the WORDING, and the
wording is most of the safety, because the one conclusion a user must not draw
is that something reviewed the generated code for them.
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

from council_core import forge_jobs, speech as speech_core  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


# ============================================================
# Speech
# ============================================================

class FakeEngine:
    """A pyttsx3 stand-in that records what it was told."""

    def __init__(self, block=None):
        self.properties = {}
        self.said = []
        self.stopped = 0
        self._block = block

    def setProperty(self, name, value):
        self.properties[name] = value

    def say(self, text):
        self.said.append(text)

    def runAndWait(self):
        if self._block is not None:
            self._block.wait(2.0)

    def stop(self):
        self.stopped += 1


def _player(engine):
    player = speech_core.TtsPlayer()
    player._engine = engine
    return player


@pytest.mark.parametrize("module", ["speech.py", "forge_jobs.py"])
def test_neither_module_imports_a_toolkit(module):
    source = (ROOT / "council_core" / module).read_text(encoding="utf-8")
    for toolkit in ("tkinter", "PySide6", "PyQt5", "messagebox"):
        assert toolkit not in source


# -- the rate is a value, never a control -------------------------------------

def test_speak_takes_a_number_and_cannot_reach_a_control():
    """THE DEFECT THIS MODULE EXISTS FOR. `_tts_speak_last`'s worker calls
    `self._tts_rate_var.get()` — a widget read from a background thread, which
    survives only because Tkinter marshals Variable.get() internally. Qt does
    not."""
    import inspect
    signature = inspect.signature(speech_core.TtsPlayer.speak)
    assert "rate" in signature.parameters
    source = inspect.getsource(speech_core.TtsPlayer.speak)
    for widgetish in (".get()", "_var", "Variable"):
        assert widgetish not in source, (
            f"speak() reaches for something control-shaped: {widgetish}")


def test_the_qt_tab_reads_the_spinbox_before_the_worker_starts():
    import ast
    source = (ROOT / "council_qt" / "tabs" / "speech.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    handler = next(node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef)
                   and node.name == "on_speak")
    worker = next(node for node in ast.walk(handler)
                  if isinstance(node, ast.FunctionDef) and node.name == "work")
    worker_source = ast.get_source_segment(source, worker) or ""
    assert "self.rate" not in worker_source, (
        "the worker reads the spinbox, exactly as the Tk version does")
    assert "self.rate.value()" in source


@pytest.mark.parametrize("given,expected", [
    ("200", 200), ("abc", 175), ("", 175), (None, 175),
    ("9999", 300), ("1", 80), (150, 150),
])
def test_a_rate_is_clamped_to_something_usable(given, expected):
    """A missing control, an empty box and non-numeric text all happen with an
    editable spinbox, and the Tk version already guards two of the three."""
    assert speech_core.clamp_rate(given) == expected


# -- one speaker at a time ----------------------------------------------------

def test_a_second_speak_while_one_is_running_is_refused():
    """Every Speak press in the Tk build starts a fresh daemon thread against
    ONE shared engine, with runAndWait() blocking inside it. What two threads
    driving one pyttsx3 engine does is engine-dependent and none of the
    outcomes are good."""
    gate = threading.Event()
    player = _player(FakeEngine(block=gate))

    results = []
    thread = threading.Thread(
        target=lambda: results.append(player.speak("first", rate=175)))
    thread.start()
    for _ in range(200):                      # wait until it is really going
        if player.speaking:
            break
        time.sleep(0.005)

    second = player.speak("second", rate=175)
    assert not second.ok
    assert "Already speaking" in second.message

    gate.set()
    thread.join(2.0)
    assert results and results[0].ok


def test_the_lock_is_released_even_when_the_engine_raises():
    """Otherwise one failure makes the button permanently dead, and the only
    cue is that nothing happens ever again."""
    class Exploding(FakeEngine):
        def runAndWait(self):
            raise RuntimeError("audio device gone")

    player = _player(Exploding())
    first = player.speak("hello", rate=175)
    assert not first.ok and "audio device gone" in first.message
    assert not player.speaking
    assert player.speak("again", rate=175).message != "Already speaking — press Stop first."


def test_stopping_when_nothing_is_speaking_is_harmless():
    assert speech_core.TtsPlayer().stop().ok


def test_stop_reaches_the_engine():
    engine = FakeEngine()
    player = _player(engine)
    player.stop()
    assert engine.stopped == 1


# -- what actually gets spoken ------------------------------------------------

def test_a_long_answer_is_capped_and_says_so():
    """Uncapped, a long report is a twenty-minute reading with no way out but
    Stop."""
    engine = FakeEngine()
    result = _player(engine).speak("x" * 9000, rate=175)
    assert result.ok
    assert len(engine.said[0]) == speech_core.MAX_SPOKEN_CHARS
    assert "first 4,000" in result.message


def test_a_short_answer_is_spoken_whole_with_no_caveat():
    engine = FakeEngine()
    result = _player(engine).speak("short answer", rate=175)
    assert "first" not in result.message


def test_the_rate_reaches_the_engine():
    engine = FakeEngine()
    _player(engine).speak("hello", rate=210)
    assert engine.properties["rate"] == 210


def test_nothing_to_speak_says_so():
    assert "nothing to speak" in speech_core.TtsPlayer().speak("  ").message


def test_a_build_without_pyttsx3_loses_one_button_and_nothing_else():
    """The import is optional on purpose; a missing package should cost the
    Speak function and not the tab."""
    player = speech_core.TtsPlayer()
    player._engine = None
    result = player.speak("hello")
    if not player.available:
        assert not result.ok
        assert "pyttsx3" in result.message


# ============================================================
# Tool Creation
# ============================================================

def test_every_success_message_says_the_code_is_unreviewed():
    """A model wrote it and nobody has read it. The sandbox proves the tool
    does not delete, write, reach the network or shell out; it does not prove
    the tool is correct, and those are very different assurances."""
    import types
    saved = sys.modules.get("tool_forge")
    sys.modules["tool_forge"] = types.SimpleNamespace(
        generate_tool=lambda task, call, **kw: (True, "wrote it", "counter", "code"))
    try:
        result = forge_jobs.forge("count rows", Path.home())
        assert result.ok
        assert "UNREVIEWED" in result.status
        assert "UNREVIEWED" in result.body
    finally:
        if saved is not None:
            sys.modules["tool_forge"] = saved
        else:
            sys.modules.pop("tool_forge", None)


def test_the_unreviewed_wording_is_defined_once():
    """It is the safety message, so it must not be re-typed per call site and
    drift."""
    source = (ROOT / "council_core" / "forge_jobs.py").read_text(
        encoding="utf-8")
    assert source.count("UNREVIEWED — review the code") <= 1


def test_a_failed_generation_tells_you_what_you_can_still_do():
    import types
    saved = sys.modules.get("tool_forge")
    sys.modules["tool_forge"] = types.SimpleNamespace(
        generate_tool=lambda task, call, **kw: (False, "model said no", None, "partial"))
    try:
        result = forge_jobs.forge("something", Path.home())
        assert not result.ok
        assert "Save Edited Code" in result.body
        assert result.code == "partial", "the partial code was thrown away"
    finally:
        if saved is not None:
            sys.modules["tool_forge"] = saved
        else:
            sys.modules.pop("tool_forge", None)


def test_generation_never_raises_into_the_worker():
    import types
    saved = sys.modules.get("tool_forge")
    sys.modules["tool_forge"] = types.SimpleNamespace(
        generate_tool=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    try:
        result = forge_jobs.forge("something", Path.home())
        assert not result.ok
        assert result.error is not None
    finally:
        if saved is not None:
            sys.modules["tool_forge"] = saved
        else:
            sys.modules.pop("tool_forge", None)


def test_an_empty_task_is_refused():
    assert "Describe a tool first" in forge_jobs.forge("  ", Path.home()).status


def test_an_empty_save_is_refused():
    assert "Nothing to save" in forge_jobs.save_edited("", Path.home()).status


def test_running_nothing_is_refused():
    assert "Select a tool" in forge_jobs.run_tool("", Path.home()).status


def test_a_broken_tools_directory_does_not_look_like_an_empty_one():
    """The Tk version swallows any failure into an empty list."""
    import types
    saved = sys.modules.get("app_built_tools")
    sys.modules["app_built_tools"] = types.SimpleNamespace(
        list_tools=lambda **kw: (_ for _ in ()).throw(OSError("unreadable")))
    try:
        result = forge_jobs.list_tools(Path.home())
        assert not result.ok
        assert "Could not list" in result.message
    finally:
        if saved is not None:
            sys.modules["app_built_tools"] = saved
        else:
            sys.modules.pop("app_built_tools", None)


def test_saving_happens_on_a_worker_in_the_qt_tab():
    """_forge_save is the only forge action the Tk tab runs on the GUI thread,
    and it writes a file and re-validates it."""
    import ast
    source = (ROOT / "council_qt" / "tabs" / "forge.py").read_text(
        encoding="utf-8")
    tree = ast.parse(source)
    handler = next(node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef) and node.name == "on_save")
    body = ast.get_source_segment(source, handler) or ""
    assert "_start(" in body, "on_save does not go through the worker helper"
    assert "save_edited" in body
