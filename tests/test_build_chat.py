"""The Build tab, step 2: the Ask-mode chat beside the editor.

council_core.build_chat (what the model is shown, the conversation kept per
folder) and the Qt panel in the tab, offscreen, with a fake model.
"""
from __future__ import annotations

import inspect
import os
import sys
import threading
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import build_chat as bc  # noqa: E402


class Cancelled(Exception):
    """Stands in for council_engine.GenerationCancelled (matched by name)."""
    def __init__(self, partial=""):
        super().__init__("cancelled")
        self.partial = partial


Cancelled.__name__ = "GenerationCancelled"


class FakeModel:
    """chat(messages, on_token=, should_stop=): streams ``reply`` word by
    word, records what it was shown, and can wait to be stopped."""

    def __init__(self, reply="It prints the value.", hold=False, fail=None):
        self.reply, self.hold, self.fail = reply, hold, fail
        self.seen = []
        self.started = threading.Event()

    def __call__(self, messages, on_token=None, should_stop=None):
        self.seen.append(messages)
        self.started.set()
        if self.fail:
            raise self.fail
        sent = []
        for word in self.reply.split(" "):
            piece = word if not sent else " " + word
            sent.append(piece)
            if on_token:
                on_token(piece)
        while self.hold:
            if should_stop and should_stop():
                raise Cancelled("".join(sent))
            time.sleep(0.01)
        return self.reply


# ── what the model is shown ──────────────────────────────────────────────
def test_the_open_file_is_shown_numbered_with_the_selection():
    ctx = bc.Context(path="/p/main.py", text="a = 1\nb = 2\nc = 3", language="Python",
                     sel_start=2, sel_end=3, folder="/p")
    block = bc.context_block(ctx)
    assert "Open file: /p/main.py (Python, 3 lines)" in block
    assert "1| a = 1\n2| b = 2\n3| c = 3" in block
    assert "selected lines 2-3:\n```\n2| b = 2\n3| c = 3\n```" in block
    assert "Workspace folder: /p" in block and "shown lines" not in block


def test_without_a_selection_the_cursor_line_is_said():
    block = bc.context_block(bc.Context(path="x.py", text="x", cursor_line=1))
    assert "The cursor is on line 1." in block
    assert bc.context_block(bc.Context()) == "No file is open."


def test_a_long_file_is_cut_to_a_window_around_the_selection():
    text = "\n".join(f"line {i}" for i in range(1, 5001))
    first, last, shown = bc.file_window(text, 4000, limit=2000)
    assert first <= 4000 <= last and len(shown) <= 2000
    assert shown.split("\n")[0] == f"line {first}"
    block = bc.context_block(bc.Context(path="big.py", text=text, sel_start=4000,
                                        sel_end=4000))
    assert "you are shown lines" in block and "4000| line 4000" in block
    assert "   1| line 1\n" not in block


def test_only_the_tail_of_the_run_output_is_shown():
    out = "\n".join(f"out {i}" for i in range(500)) + "\nValueError: bad\n"
    block = bc.context_block(bc.Context(output=out))
    assert "ValueError: bad" in block and "out 0\n" not in block
    assert len(bc.tail(out).split("\n")) == bc.OUTPUT_LINES
    assert "last run" not in bc.context_block(bc.Context(output="  \n"))


def test_the_newest_history_is_kept_and_the_cut_is_said(monkeypatch):
    monkeypatch.setattr(bc, "HISTORY_CHARS", 25)
    history = [bc.Turn("user", "q" * 10), bc.Turn("assistant", "a" * 10),
               bc.Turn("user", "Q" * 10), bc.Turn("assistant", "A" * 10)]
    msgs = bc.build_messages(history, "  why?  ", bc.Context())
    assert msgs[0]["content"] == bc.SYSTEM
    assert "2 earliest messages" in msgs[1]["content"]
    assert [m["content"] for m in msgs[2:4]] == ["Q" * 10, "A" * 10]
    assert msgs[-1]["role"] == "user" and msgs[-1]["content"].endswith("Question: why?")
    full = bc.build_messages(history[:2], "x", bc.Context())
    assert len(full) == 4 and "earliest" not in full[1]["content"]


def test_code_blocks_are_found_in_order():
    reply = "Try:\n```python\nx = 1\n```\nor\n```\ny = 2\nz = 3\n```\n"
    assert bc.code_blocks(reply) == ["x = 1", "y = 2\nz = 3"]
    assert bc.code_blocks("none here") == []


# ── the conversation on disk ─────────────────────────────────────────────
def test_each_folder_keeps_its_own_conversation(tmp_path):
    vault = tmp_path / "vault"
    model = FakeModel("Use a loop.")
    one = bc.Conversation(vault, str(tmp_path / "one"))
    assert one.ask("How?", bc.Context(path="a.py", text="x"), model) == "Use a loop."
    assert one.path.parent == vault / ".council_build" / "chats"
    assert one.path.name.startswith("one-")
    again = bc.Conversation(vault, str(tmp_path / "one"))
    assert [(t.role, t.content, t.file) for t in again.turns] == [
        ("user", "How?", "a.py"), ("assistant", "Use a loop.", "a.py")]
    assert bc.Conversation(vault, str(tmp_path / "two")).turns == []
    again.clear()
    assert bc.Conversation(vault, str(tmp_path / "one")).turns == []


def test_the_second_question_carries_the_first_answer(tmp_path):
    model = FakeModel("Because.")
    conv = bc.Conversation(tmp_path, "f")
    conv.ask("Why?", bc.Context(), model)
    conv.ask("And then?", bc.Context(), model)
    second = model.seen[1]
    assert [m["content"] for m in second[1:3]] == ["Why?", "Because."]


def test_a_stopped_answer_is_kept_as_far_as_it_got(tmp_path):
    def stopped(messages, on_token=None, should_stop=None):
        on_token("Half an")
        on_token(" answer")
        raise Cancelled("Half an answer")
    conv = bc.Conversation(tmp_path, "f")
    with pytest.raises(Cancelled):
        conv.ask("Go", bc.Context(), stopped)
    kept = bc.Conversation(tmp_path, "f").turns
    assert [t.role for t in kept] == ["user", "assistant"]
    assert kept[1].content == "Half an answer\n\n(stopped)"


def test_a_failed_call_keeps_the_question_only(tmp_path):
    conv = bc.Conversation(tmp_path, "f")
    with pytest.raises(RuntimeError):
        conv.ask("Go", bc.Context(), FakeModel(fail=RuntimeError("no model")))
    assert [t.role for t in bc.Conversation(tmp_path, "f").turns] == ["user"]


def test_the_chats_are_kept_where_the_vault_search_does_not_look(tmp_path):
    from council_core.vault_context import is_app_source
    conv = bc.Conversation(tmp_path, "f")
    conv.ask("q", bc.Context(), FakeModel("a"))
    assert is_app_source(str(conv.path), tmp_path)
    # a chat the first version kept in build_chats/ is still read
    old = bc._old_store_path(tmp_path, "g")
    old.parent.mkdir(parents=True)
    old.write_text('{"turns": [{"role": "user", "content": "old"}]}', encoding="utf-8")
    assert [t.content for t in bc.Conversation(tmp_path, "g").turns] == ["old"]


def test_a_damaged_file_starts_an_empty_conversation(tmp_path):
    path = bc.store_path(tmp_path, "f")
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert bc.Conversation(tmp_path, "f").turns == []


def test_local_chat_passes_the_token_callback_through():
    import council_engine
    assert "token_callback" in inspect.signature(council_engine.local_chat).parameters
    src = inspect.getsource(council_engine.local_chat)
    assert "token_callback=token_callback" in src


# ── the panel in the tab ─────────────────────────────────────────────────
from PySide6.QtGui import QGuiApplication, QTextCursor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from council_qt.tabs.build import BuildActions, BuildTab  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def pump(qapp, until, seconds=10.0):
    deadline = time.time() + seconds
    while not until() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    assert until(), "timed out"


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "main.py").write_text("x = 1\ny = 2\nprint(x + y)\n", encoding="utf-8")
    return root


def make_tab(tmp_path, folder, model):
    actions = BuildActions(
        vault_dir=tmp_path / "vault", state_file=tmp_path / "state.json",
        unsaved_dir=tmp_path / "unsaved", confirm=lambda t, x: True,
        ask_folder=lambda start: "", ask_name=lambda t, l: None, chat=model)
    actions.save_state({"folder": str(folder)})
    return BuildTab(None, actions)


def test_the_model_sees_the_file_as_edited_and_the_selected_lines(qapp, tmp_path, project):
    model = FakeModel()
    tab = make_tab(tmp_path, project, model)
    doc = tab.open_file(project / "main.py")
    doc.editor.moveCursor(QTextCursor.MoveOperation.End)
    doc.editor.insertPlainText("z = 3\n")                 # not saved
    cur = doc.editor.textCursor()
    cur.setPosition(doc.editor.document().findBlockByNumber(1).position())
    cur.setPosition(doc.editor.document().findBlockByNumber(3).position(),
                    QTextCursor.MoveMode.KeepAnchor)       # lines 2-3
    doc.editor.setTextCursor(cur)
    ctx = tab.chat_context(True, False)
    assert ctx.path == str(project / "main.py") and "z = 3" in ctx.text
    assert (ctx.sel_start, ctx.sel_end) == (2, 3) and ctx.language == "Python"
    assert ctx.folder == str(project) and ctx.output == ""
    assert tab.chat_context(False, False).path == ""
    tab.deleteLater()


def test_a_question_streams_its_answer_and_is_kept(qapp, tmp_path, project):
    model = FakeModel("It adds ```python\nprint(3)\n``` done")
    tab = make_tab(tmp_path, project, model)
    tab.open_file(project / "main.py")
    panel = tab.chat_panel
    panel.input.setPlainText("What does it print?")
    panel.send()
    assert panel.input.toPlainText() == ""
    pump(qapp, lambda: not panel.is_busy())
    assert "What does it print?" in panel.view.toPlainText()
    assert "print(3)" in panel.view.toPlainText() and panel.status.text() == ""
    assert "1| x = 1" in model.seen[0][-1]["content"]
    assert panel.copy_code() == "print(3)"
    assert QGuiApplication.clipboard().text() == "print(3)"
    # the same folder, opened again, shows the conversation
    tab.deleteLater()
    tab2 = make_tab(tmp_path, project, model)
    assert "What does it print?" in tab2.chat_panel.view.toPlainText()
    tab2.chat_panel.new_chat()
    assert tab2.chat_panel.view.toPlainText() == ""
    tab2.deleteLater()


def test_stop_ends_the_answer_where_it_is(qapp, tmp_path, project):
    model = FakeModel("Partial words", hold=True)
    tab = make_tab(tmp_path, project, model)
    panel = tab.chat_panel
    panel.input.setPlainText("Explain")
    panel.send()
    assert panel.stop_btn.isEnabled() and not panel.send_btn.isEnabled()
    pump(qapp, lambda: "Partial words" in panel.view.toPlainText())
    panel.stop()
    pump(qapp, lambda: not panel.is_busy())
    assert panel.status.text() == "Stopped." and panel.send_btn.isEnabled()
    assert "(stopped)" in panel.view.toPlainText()
    tab.deleteLater()


def test_a_model_error_is_said(qapp, tmp_path, project):
    tab = make_tab(tmp_path, project, FakeModel(fail=RuntimeError("no model loaded")))
    panel = tab.chat_panel
    panel.input.setPlainText("Hi")
    panel.send()
    pump(qapp, lambda: not panel.is_busy())
    assert "no model loaded" in panel.status.text()
    tab.deleteLater()


def test_a_failed_run_puts_its_output_in_front_of_the_model(qapp, tmp_path, project):
    project.joinpath("bad.py").write_text("print('before')\nraise ValueError('boom')\n",
                                          encoding="utf-8")
    model = FakeModel()
    tab = make_tab(tmp_path, project, model)
    tab.open_file(project / "bad.py")
    assert not tab.chat_panel.use_output.isChecked()
    tab.on_run()
    pump(qapp, lambda: not tab.is_running() and "exited" in tab.output.toPlainText())
    assert tab.chat_panel.use_output.isChecked()
    tab.chat_panel.input.setPlainText("Why does it fail?")
    tab.chat_panel.send()
    pump(qapp, lambda: not tab.chat_panel.is_busy())
    shown = model.seen[0][-1]["content"]
    assert "last run's output" in shown and "ValueError: boom" in shown
    tab.deleteLater()


def test_opening_another_folder_switches_the_conversation(qapp, tmp_path, project):
    model = FakeModel("Answer one.")
    tab = make_tab(tmp_path, project, model)
    tab.chat_panel.input.setPlainText("first folder")
    tab.chat_panel.send()
    pump(qapp, lambda: not tab.chat_panel.is_busy())
    other = tmp_path / "other"
    other.mkdir()
    tab.open_folder(other)
    assert "first folder" not in tab.chat_panel.view.toPlainText()
    tab.open_folder(project)
    assert "first folder" in tab.chat_panel.view.toPlainText()
    tab.deleteLater()
