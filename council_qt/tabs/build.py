"""
council_qt.tabs.build — the Build tab: a folder's files, an editor, and Run.

This is the first step of the coding workspace (the plan in the project
thread, 2026-10-29): files on the left, the editor in the middle, the output
under it. The chat beside the editor, edits shown as diffs to accept, and
sandboxed Run and Test come in the next steps, on top of this.

    Open folder…   any folder becomes the workspace; it is remembered.
    Scratch        the vault's workspace folder (the IDE tab's), for code
                   that belongs to no project.
    The tree       folders open as they are expanded (council_core.
                   build_files.list_dir). Double-click a file to open it.
    The editor     a tab per file (council_qt.widgets.code_editor): colours,
                   line numbers, find and replace (Ctrl+F), Ctrl+S saves.
                   A tab with unsaved changes shows a dot.
    Run            saves the current file and runs it with "Run with"'s
                   Python in the file's own folder, streaming its output.
                   Stop ends it. council_core.ide_trust asks first when the
                   file does something that can affect the machine.

NOTHING IS LOST
  * Save goes through build_files.save_text: atomic, the file's own encoding
    and line endings, and it asks before overwriting a file that changed on
    disk since it was opened.
  * Closing a tab with unsaved changes asks.
  * Closing the Council with unsaved changes cannot be stopped from a tab,
    so each unsaved buffer is written to the app folder's build_unsaved
    (never over the file itself) and the tab says where the next time it
    opens.

Everything that touches the user (a folder picker, a yes/no, a file name) is
an injectable callable on BuildActions, so the tests never open a dialog.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QPlainTextEdit,
                               QSplitter, QTabWidget, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

import python_envs as envs
from council_core import build_chat as bc
from council_core import build_files as bf
from council_core import code_highlight as ch
from council_core import ide_trust, paths

from ..view import ViewHelpers, amp
from ..widgets.build_chat import ChatPanel
from ..widgets.code_editor import CodeEditor, FindBar, mono_font

#: How many output lines the Run pane keeps (as the IDE tab's).
MAX_OUTPUT_LINES = 5_000
_PATH = Qt.ItemDataRole.UserRole
_PLACEHOLDER = "…"


def _no_dialogs() -> bool:
    return os.environ.get("COUNCIL_NO_DIALOGS") == "1"


class BuildActions:
    """What the tab asks of the application and of the user. Tests pass
    their own callables; the defaults open Qt dialogs."""

    def __init__(self, vault_dir: Optional[Path] = None,
                 state_file: Optional[Path] = None,
                 unsaved_dir: Optional[Path] = None,
                 ask_folder: Optional[Callable[[str], str]] = None,
                 confirm: Optional[Callable[[str, str], bool]] = None,
                 ask_name: Optional[Callable[[str, str], Optional[str]]] = None,
                 chat: Optional[Callable[..., str]] = None):
        self.vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()
        app = paths.app_dir()
        self.state_file = Path(state_file) if state_file else app / "build_tab.json"
        self.unsaved_dir = Path(unsaved_dir) if unsaved_dir else app / "build_unsaved"
        self.ask_folder = ask_folder or self._ask_folder
        self.confirm = confirm or self._confirm
        self.ask_name = ask_name or self._ask_name
        self.chat = chat or self._chat

    # -- the defaults ------------------------------------------------------
    @staticmethod
    def _ask_folder(start: str) -> str:
        if _no_dialogs():
            return ""
        from PySide6.QtWidgets import QFileDialog
        return QFileDialog.getExistingDirectory(None, "Open a folder", start)

    @staticmethod
    def _confirm(title: str, text: str) -> bool:
        if _no_dialogs():
            return False
        from PySide6.QtWidgets import QMessageBox
        return QMessageBox.question(None, title, text) == \
            QMessageBox.StandardButton.Yes

    @staticmethod
    def _ask_name(title: str, label: str) -> Optional[str]:
        if _no_dialogs():
            return None
        from PySide6.QtWidgets import QInputDialog
        text, ok = QInputDialog.getText(None, title, label)
        return text if ok else None

    @staticmethod
    def _chat(messages, on_token=None, should_stop=None) -> str:
        """One answer from the Council's local "coder" model (the main
        model when no model is set for that role), streamed."""
        import council_engine
        from council_core import token_budget
        return council_engine.local_chat(
            messages, role="coder", temperature=0.2,
            num_predict=token_budget.reply("long", 1500), timeout=180,
            token_callback=on_token, should_stop=should_stop)

    # -- the workspace -----------------------------------------------------
    def scratch_dir(self) -> Path:
        return paths.ensure(self.vault_dir / "workspace")

    def load_state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_state(self, state: dict) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_name(self.state_file.name + ".tmp")
            tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
            os.replace(tmp, self.state_file)
        except OSError:
            pass

    def keep_unsaved(self, name: str, text: str) -> Optional[Path]:
        """An unsaved buffer kept in build_unsaved (never over its file)."""
        try:
            self.unsaved_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            path = self.unsaved_dir / f"{stamp}-{Path(name).name}"
            path.write_text(text, encoding="utf-8")
            return path
        except OSError:
            return None

    def unsaved_kept(self) -> List[Path]:
        try:
            return sorted(p for p in self.unsaved_dir.iterdir() if p.is_file())
        except OSError:
            return []


class _Doc:
    """One open file: its editor and what it was opened from."""

    def __init__(self, opened: bf.OpenedFile, editor: CodeEditor):
        self.opened = opened
        self.editor = editor

    @property
    def path(self) -> Path:
        return self.opened.path

    def modified(self) -> bool:
        return self.editor.document().isModified()


class BuildTab(ViewHelpers, QWidget):
    """Files, editor, output."""

    def __init__(self, window=None, actions: Optional[BuildActions] = None):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or BuildActions()
        self.trust = ide_trust.TrustStore()
        self.root: Optional[Path] = None
        self.docs: List[_Doc] = []
        self._stop: Optional[threading.Event] = None
        self._running = False
        self._run_lines: List[str] = []       # the last run's output, for the chat
        self._build()
        state = self.actions.load_state()
        self.python_box.setCurrentText(envs.display(state.get("python", "")))
        folder = Path(state.get("folder") or "")
        self.open_folder(folder if state.get("folder") and folder.is_dir()
                         else self.actions.scratch_dir())
        kept = self.actions.unsaved_kept()
        if kept:
            self.say(f"Unsaved edits from an earlier session are kept in "
                     f"{kept[0].parent} ({len(kept)} file(s)).")
        hooks = getattr(window, "close_hooks", None)
        if isinstance(hooks, list):
            hooks.append(self.keep_unsaved)

    # ------------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)

        row = QHBoxLayout()
        self.open_btn = self._button(row, "Open folder…", self.on_open_folder)
        self.scratch_btn = self._button(row, "Scratch", self.on_scratch)
        self.new_btn = self._button(row, "New file…", self.on_new_file)
        self.save_btn = self._button(row, "Save", self.save_current)
        self.save_all_btn = self._button(row, "Save all", self.save_all)
        row.addSpacing(12)
        row.addWidget(QLabel("Run with:"))
        self.python_box = QComboBox()
        self.python_box.addItems([c for c in envs.choices() if c != envs.BROWSE_LABEL])
        self.python_box.currentTextChanged.connect(self._remember)
        row.addWidget(self.python_box)
        self.run_btn = self._button(row, amp("▶ Run"), self.on_run)
        self.stop_btn = self._button(row, amp("■ Stop"), self.on_stop)
        self.stop_btn.setEnabled(False)
        row.addStretch(1)
        outer.addLayout(row)

        self.folder_label = QLabel("")
        self.folder_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(self.folder_label)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemExpanded.connect(self._fill)
        self.tree.itemActivated.connect(self._activated)
        split.addWidget(self.tree)

        right = QSplitter(Qt.Orientation.Vertical)
        editors = QWidget()
        col = QVBoxLayout(editors)
        col.setContentsMargins(0, 0, 0, 0)
        self.editors = QTabWidget()
        self.editors.setTabsClosable(True)
        self.editors.setMovable(True)
        self.editors.setDocumentMode(True)
        self.editors.tabCloseRequested.connect(self.close_tab)
        self.editors.currentChanged.connect(self._current_changed)
        col.addWidget(self.editors, 1)
        self.find_bar = FindBar()
        self.find_bar.hide()
        col.addWidget(self.find_bar)
        right.addWidget(editors)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.output.setFont(mono_font())
        self.output.setMaximumBlockCount(MAX_OUTPUT_LINES)
        right.addWidget(self.output)
        right.setSizes([520, 180])
        split.addWidget(right)
        self.chat_panel = ChatPanel(self.actions.vault_dir, self.actions.chat,
                                    self.chat_context, bridge=self.bridge)
        split.addWidget(self.chat_panel)
        split.setSizes([220, 700, 420])
        outer.addWidget(split, 1)

        self.status = QLabel("")
        outer.addWidget(self.status)

    # -- messages ----------------------------------------------------------
    def say(self, text: str) -> None:
        self.status.setText(text)

    def append(self, text: str) -> None:
        """One line of output; the view follows only a reader at the end."""
        bar = self.output.verticalScrollBar()
        at_end = bar.value() >= bar.maximum() - 4
        cur = self.output.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        cur.insertText(text + "\n")
        if at_end:
            bar.setValue(bar.maximum())

    def _remember(self, *_a) -> None:
        self.actions.save_state({"folder": str(self.root or ""),
                                 "python": self.python_spec()})

    # -- the folder and its tree ---------------------------------------------
    def open_folder(self, folder: Path) -> None:
        folder = Path(folder)
        self.root = folder
        self.folder_label.setText(str(folder))
        self.tree.clear()
        self._fill_into(self.tree.invisibleRootItem(), folder)
        self.chat_panel.set_folder(folder)
        self._remember()

    def on_open_folder(self) -> None:
        chosen = self.actions.ask_folder(str(self.root or ""))
        if chosen:
            self.open_folder(Path(chosen))

    def on_scratch(self) -> None:
        self.open_folder(self.actions.scratch_dir())

    def _fill_into(self, parent: QTreeWidgetItem, folder: Path) -> None:
        listing = bf.list_dir(folder)
        if listing.error:
            self.say(listing.error)
        for e in listing.entries:
            item = QTreeWidgetItem([e.name + ("/" if e.is_dir else "")])
            item.setData(0, _PATH, str(e.path))
            item.setToolTip(0, str(e.path))
            if e.is_dir:
                item.addChild(QTreeWidgetItem([_PLACEHOLDER]))
            parent.addChild(item)
        if listing.truncated:
            parent.addChild(QTreeWidgetItem([f"(first {bf.MAX_ENTRIES} shown)"]))

    def _fill(self, item: QTreeWidgetItem) -> None:
        """A folder's children, listed the first time it is opened."""
        if item.childCount() == 1 and item.child(0).data(0, _PATH) is None \
                and item.child(0).text(0) == _PLACEHOLDER:
            item.takeChild(0)
            self._fill_into(item, Path(item.data(0, _PATH)))

    def _activated(self, item: QTreeWidgetItem, _col: int = 0) -> None:
        path = item.data(0, _PATH)
        if path and Path(path).is_file():
            self.open_file(Path(path))

    def refresh_tree(self) -> None:
        if self.root is not None:
            self.tree.clear()
            self._fill_into(self.tree.invisibleRootItem(), self.root)

    # -- open files ----------------------------------------------------------
    def doc_for(self, path: Path) -> Optional[_Doc]:
        path = Path(path).resolve()
        return next((d for d in self.docs if d.path.resolve() == path), None)

    def current(self) -> Optional[_Doc]:
        ed = self.editors.currentWidget()
        return next((d for d in self.docs if d.editor is ed), None)

    def open_file(self, path: Path) -> Optional[_Doc]:
        existing = self.doc_for(path)
        if existing is not None:
            self.editors.setCurrentWidget(existing.editor)
            return existing
        try:
            opened = bf.open_text(path)
        except bf.FileProblem as exc:
            self.say(str(exc))
            return None
        editor = CodeEditor(filename=opened.path.name)
        editor.setPlainText(opened.text)
        editor.highlighter.refresh()          # coloured now, not after a pause
        editor.document().setModified(False)
        doc = _Doc(opened, editor)
        self.docs.append(doc)
        editor.document().modificationChanged.connect(lambda _m, d=doc: self._retitle(d))
        editor.save_requested.connect(self.save_current)
        editor.find_requested.connect(self.open_find)
        i = self.editors.addTab(editor, opened.path.name)
        self.editors.setTabToolTip(i, str(opened.path))
        self.editors.setCurrentIndex(i)
        self.say(f"{opened.path.name} · {ch.language(opened.path.name)}")
        return doc

    def _retitle(self, doc: _Doc) -> None:
        i = self.editors.indexOf(doc.editor)
        if i >= 0:
            self.editors.setTabText(i, doc.path.name + (" •" if doc.modified() else ""))

    def _current_changed(self, _i: int) -> None:
        doc = self.current()
        self.find_bar.attach(doc.editor if doc else None)

    def open_find(self) -> None:
        doc = self.current()
        if doc is not None:
            self.find_bar.attach(doc.editor)
            self.find_bar.open()

    def close_tab(self, index: int) -> bool:
        ed = self.editors.widget(index)
        doc = next((d for d in self.docs if d.editor is ed), None)
        if doc is None:
            return False
        if doc.modified() and not self.actions.confirm(
                "Unsaved changes",
                f"{doc.path.name} has changes that are not saved. Close it and "
                f"lose them?"):
            return False
        self.docs.remove(doc)
        self.editors.removeTab(index)
        ed.deleteLater()
        return True

    # -- saving --------------------------------------------------------------
    def save(self, doc: _Doc) -> bool:
        text = doc.editor.toPlainText()
        try:
            doc.opened = bf.save_text(doc.opened, text)
        except bf.ChangedOnDisk as exc:
            if not self.actions.confirm(
                    "Changed on disk",
                    f"{exc}. Save over it anyway? Its changes will be lost."):
                self.say(f"{doc.path.name} not saved: it changed on disk.")
                return False
            try:
                doc.opened = bf.save_text(doc.opened, text, force=True)
            except bf.FileProblem as exc2:
                self.say(str(exc2))
                return False
        except bf.FileProblem as exc:
            self.say(str(exc))
            return False
        doc.editor.document().setModified(False)
        self.say(f"Saved {doc.path.name}.")
        return True

    def save_current(self) -> bool:
        doc = self.current()
        return self.save(doc) if doc is not None else False

    def save_all(self) -> bool:
        return all([self.save(d) for d in self.docs if d.modified()])

    def on_new_file(self) -> None:
        if self.root is None:
            return
        name = self.actions.ask_name("New file", f"Name, inside {self.root.name}:")
        if not name:
            return
        try:
            path = bf.new_file(self.root, name)
        except bf.FileProblem as exc:
            self.say(str(exc))
            return
        self.refresh_tree()
        self.open_file(path)

    def keep_unsaved(self) -> None:
        """On the Council's close: every unsaved buffer kept, never over its
        own file (the user did not choose to save it)."""
        for doc in self.docs:
            if doc.modified():
                self.actions.keep_unsaved(doc.path.name, doc.editor.toPlainText())

    # -- running ---------------------------------------------------------------
    def python_spec(self) -> str:
        return envs.spec_from_choice(self.python_box.currentText())

    def on_run(self) -> None:
        if self._running:
            self.say("A run is already going.")
            return
        doc = self.current()
        if doc is None:
            self.say("Open a Python file to run it.")
            return
        if not doc.editor.is_python():
            self.say(f"{doc.path.name} is not a Python file.")
            return
        if doc.modified() and not self.save(doc):
            return
        code = doc.editor.toPlainText()
        verdict = ide_trust.check(code, self.trust)
        if not verdict.allowed:
            if not self.actions.confirm("Confirm code execution", verdict.prompt):
                self.append("[Cancelled]")
                return
            self.trust.trust(code)
        res = envs.resolve(self.python_spec())
        if res.error or not res.python:
            self.append(f"[Cannot run: {res.error or 'no Python chosen'}]")
            return
        self._start_run(res.python, doc.path)

    def _start_run(self, python: str, path: Path) -> None:
        self._running = True
        self._stop = threading.Event()
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.append(f"[Running {path.name} with {python}]")
        stop = self._stop
        self._run_lines = []

        def line_out(line: str) -> None:
            self._to_ui(self._run_line, line)

        def work() -> None:
            outcome = bf.run_file(python, path, line_out, stop)
            self._to_ui(self._finished, outcome, path.name)

        threading.Thread(target=work, name="build-run", daemon=True).start()

    def _finished(self, outcome: bf.RunOutcome, name: str) -> None:
        self._running = False
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.append(outcome.summary(name))
        self.say("")
        # a failed run is usually what the next question is about
        self.chat_panel.use_output.setChecked(
            outcome.returncode not in (0, None) and not outcome.stopped)

    def _run_line(self, line: str) -> None:
        self._run_lines.append(line)
        if len(self._run_lines) > MAX_OUTPUT_LINES:
            del self._run_lines[:len(self._run_lines) - MAX_OUTPUT_LINES]
        self.append(line)

    # -- the chat's view of the editor -----------------------------------------
    def chat_context(self, use_file: bool, use_output: bool) -> bc.Context:
        """What the chat's model is shown: the open file as edited, the
        selected lines (or the cursor's), and the last run's output."""
        ctx = bc.Context(folder=str(self.root or ""))
        doc = self.current() if use_file else None
        if doc is not None:
            ed = doc.editor
            cur = ed.textCursor()
            ctx.path = str(doc.path)
            ctx.text = ed.toPlainText()
            ctx.language = ch.language(doc.path.name)
            ctx.cursor_line = cur.blockNumber() + 1
            if cur.hasSelection():
                document = ed.document()
                first = document.findBlock(cur.selectionStart()).blockNumber()
                end = cur.selectionEnd()
                last_block = document.findBlock(end)
                # a selection ending at the start of a line leaves that line out
                last = last_block.blockNumber()
                if end == last_block.position() and last > first:
                    last -= 1
                ctx.sel_start, ctx.sel_end = first + 1, last + 1
        if use_output:
            ctx.output = "\n".join(self._run_lines)
        return ctx

    def on_stop(self) -> None:
        if self._stop is not None:
            self._stop.set()

    def is_running(self) -> bool:
        return self._running


def build_build(window) -> QWidget:
    """Factory for the tab registry."""
    return BuildTab(window)
