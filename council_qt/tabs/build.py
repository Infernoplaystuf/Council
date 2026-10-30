"""
council_qt.tabs.build — the Build tab: a folder's files, an editor, and Run.

The coding workspace (docs/handoff/build_tab_2026-10-29.md): files on the
left, the editor in the middle with the output under it, the chat on the
right (council_qt.widgets.build_chat). In Edit mode the chat's accepted edits
come back here (apply_edits): into the editor, and in a session (a git
branch of its own, council_core.build_session) committed one turn at a
time, with Undo turn, the grouped review and Merge.

    Run / Test     the open file, or the folder's pytest tests, in council_
                   core.build_sandbox's sandbox: offline, the process tree
                   killed by Stop or the time limit, a memory cap.
    Fix this       after a failure: the chat in Edit mode with the output.
                   After a failed TEST run, applying the fix runs the tests
                   again by themselves, at most three rounds, and stops when
                   the same failure comes back; the model is then asked what
                   is in the way instead of for another fix.
    Preview        the open file's Qt widget built offscreen (gui_check): a
                   picture and its widget tree, or the error for Fix this.

    Open folder…   any folder becomes the workspace; it is remembered.
    Scratch        the vault's workspace folder (the IDE tab's), for code
                   that belongs to no project.
    The tree       folders open as they are expanded (council_core.
                   build_files.list_dir). Double-click a file to open it.
    The editor     a tab per file (council_qt.widgets.code_editor): colours,
                   line numbers, find and replace (Ctrl+F), Ctrl+S saves.
                   A tab with unsaved changes shows a dot.
    Run            saves the current file and runs it with "Run with"'s
                   Python in the file's own folder, in the sandbox,
                   streaming its output. Stop ends it. council_core.ide_trust
                   asks first when the file does something that can affect
                   the machine.

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

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QPlainTextEdit,
                               QSplitter, QTabWidget, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from council_core import build_edits as be
from council_core import build_sandbox as sb
from council_core import build_session as bs
from council_core import worktree as wt

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
                 chat: Optional[Callable[..., str]] = None,
                 review: Optional[Callable[[str], tuple]] = None,
                 show_preview: Optional[Callable[[str, str], None]] = None):
        self.vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()
        app = paths.app_dir()
        self.state_file = Path(state_file) if state_file else app / "build_tab.json"
        self.unsaved_dir = Path(unsaved_dir) if unsaved_dir else app / "build_unsaved"
        self.ask_folder = ask_folder or self._ask_folder
        self.confirm = confirm or self._confirm
        self.ask_name = ask_name or self._ask_name
        self.chat = chat or self._chat
        self.review = review or self._review
        self.show_preview = show_preview or self._show_preview

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

    @staticmethod
    def _review(markdown: str) -> tuple:
        """The grouped review before Merge: ("merge", None), ("ask", n) for
        a group to ask the chat about, or ("cancel", None)."""
        if _no_dialogs():
            return ("cancel", None)
        from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QTextBrowser,
                                       QVBoxLayout as _V)
        dlg = QDialog()
        dlg.setWindowTitle("Review the session before merging")
        dlg.resize(820, 640)
        col = _V(dlg)
        view = QTextBrowser()
        view.setOpenLinks(False)
        view.setMarkdown(markdown)
        col.addWidget(view)
        chosen = {"answer": ("cancel", None)}

        def link(url) -> None:
            kind, _, rest = url.toString().partition(":")
            if kind == "ask" and rest.isdigit():
                chosen["answer"] = ("ask", int(rest))
                dlg.accept()
        view.anchorClicked.connect(link)
        buttons = QDialogButtonBox()
        merge = buttons.addButton("Merge into the project", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)

        def merged() -> None:
            chosen["answer"] = ("merge", None)
            dlg.accept()
        merge.clicked.connect(merged)
        buttons.rejected.connect(dlg.reject)
        col.addWidget(buttons)
        dlg.exec()
        return chosen["answer"]

    @staticmethod
    def _show_preview(png: str, summary: str) -> None:
        """The widget as it was built offscreen, and what was found."""
        if _no_dialogs():
            return
        from PySide6.QtGui import QPixmap
        from PySide6.QtWidgets import (QDialog, QLabel as _L, QPlainTextEdit as _T,
                                       QScrollArea, QVBoxLayout as _V)
        dlg = QDialog()
        dlg.setWindowTitle("Preview (built offscreen)")
        dlg.resize(900, 700)
        col = _V(dlg)
        pic = _L()
        pix = QPixmap(png) if png else QPixmap()
        if not pix.isNull():
            pic.setPixmap(pix)
        else:
            pic.setText("No picture was made.")
        area = QScrollArea()
        area.setWidget(pic)
        col.addWidget(area, 3)
        text = _T(summary)
        text.setReadOnly(True)
        col.addWidget(text, 1)
        dlg.exec()

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
        self.project: Optional[Path] = None   # the folder the user opened
        self.session: Optional[bs.Session] = None
        self._turns: List[dict] = []         # Undo turn outside a session
        self._extra = ""                     # a review group, for the next question
        self.docs: List[_Doc] = []
        self._stop: Optional[threading.Event] = None
        self._running = False
        self._run_lines: List[str] = []       # the last run's output, for the chat
        self._last: Optional[dict] = None     # the last run or test: kind, failed, output
        self._fix: Optional[dict] = None      # the fix-and-test rounds under way
        self._build()
        state = self.actions.load_state()
        self.python_box.setCurrentText(envs.display(state.get("python", "")))
        folder = Path(state.get("folder") or "")
        session = bs.Session.load(self.actions.vault_dir, state["session"]) \
            if state.get("session") else None
        if session is not None:
            self.session = session
            self.open_folder(session.tree_folder(), project=Path(session.folder))
        else:
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
        self.test_btn = self._button(row, "Test", self.on_test)
        self.fix_btn = self._button(row, "Fix this", self.on_fix)
        self.fix_btn.setEnabled(False)
        self.preview_btn = self._button(row, "Preview", self.on_preview)
        row.addStretch(1)
        outer.addLayout(row)

        self.folder_label = QLabel("")
        self.folder_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(self.folder_label)

        srow = QHBoxLayout()
        self.session_label = QLabel("")
        self.session_label.setWordWrap(True)
        srow.addWidget(self.session_label, 1)
        self.session_btn = self._button(srow, "Start session", self.start_session)
        self.undo_btn = self._button(srow, "Undo turn", self.undo_turn)
        self.review_btn = self._button(srow, "Review and merge…", self.review_and_merge)
        self.end_btn = self._button(srow, "End session", self.end_session)
        outer.addLayout(srow)

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
                                    self.chat_context, bridge=self.bridge, host=self)
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
        self.actions.save_state({"folder": str(self.project or self.root or ""),
                                 "python": self.python_spec(),
                                 "session": self.session.id if self.session else ""})

    # -- the folder and its tree ---------------------------------------------
    def open_folder(self, folder: Path, project: Optional[Path] = None) -> None:
        """Show ``folder``. In a session it is the session's worktree and
        ``project`` the user's folder (the chat is the project's)."""
        folder = Path(folder)
        self.root = folder
        self.project = Path(project) if project else folder
        self.tree.clear()
        self._fill_into(self.tree.invisibleRootItem(), folder)
        self.chat_panel.set_folder(self.project)
        self._turns = []
        self._show_session()
        self._remember()

    def _show_session(self) -> None:
        s = self.session
        if s is not None:
            live = [t for t in s.turns if not t.undone]
            self.folder_label.setText(f"{self.project}  (session copy: {self.root})")
            self.session_label.setText(
                f"Session on branch {s.branch}: {len(live)} turn(s). Accepted edits "
                "are committed here; your folder changes only when you merge.")
        else:
            self.folder_label.setText(str(self.root or ""))
            repo = self.root is not None and bs.is_repo(self.root)
            self.session_label.setText(
                "Edit mode puts accepted edits into the editor, not saved: Save "
                "writes them. " + ("Start session to work on a branch of its own, "
                                   "one commit per turn." if repo else
                                   "A session (a branch of its own, one commit per "
                                   "turn) needs the folder to be a git repository."))
            self.session_btn.setEnabled(bool(repo))
        on = s is not None
        self.session_btn.setVisible(not on)
        self.review_btn.setVisible(on)
        self.end_btn.setVisible(on)
        self.undo_btn.setEnabled(bool(s.last_turn()) if on else bool(self._turns))

    def _leave_session_check(self) -> bool:
        if self.session is None:
            return True
        self.say("End the session or merge it first.")
        return False

    def on_open_folder(self) -> None:
        if not self._leave_session_check():
            return
        chosen = self.actions.ask_folder(str(self.project or ""))
        if chosen:
            self.open_folder(Path(chosen))

    def on_scratch(self) -> None:
        if not self._leave_session_check():
            return
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

    def _python(self) -> Optional[str]:
        res = envs.resolve(self.python_spec())
        if res.error or not res.python:
            self.append(f"[Cannot run: {res.error or 'no Python chosen'}]")
            return None
        return res.python

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
        python = self._python()
        if python:
            self._start_run(python, doc.path)

    def _start_run(self, python: str, path: Path) -> None:
        self._start(lambda line_out, stop: bf.run_file(python, path, line_out, stop),
                    f"[Running {path.name} with {python}, offline in the sandbox]",
                    path.name, "run")

    def on_test(self, auto: bool = False) -> bool:
        """Run the folder's tests (pytest) in the sandbox. ``auto``: a round
        the model started after a fix."""
        if self._running:
            self.say("A run is already going.")
            return False
        if self.root is None or not sb.has_tests(self.root):
            self.say("No tests found here: put test_*.py files in the folder or in "
                     "tests/ (pytest).")
            return False
        if not self.save_all():
            return False
        python = self._python()
        if not python:
            return False
        root, argv = Path(self.root), sb.test_argv(python)
        env = sb.sandbox_env({"QT_QPA_PLATFORM": "offscreen", "COUNCIL_NO_DIALOGS": "1",
                              "PYTHONDONTWRITEBYTECODE": "1"})
        label = (f"[Running the tests, round {self._fix['runs']} of {sb.MAX_ROUNDS} "
                 "(the model started it after its fix)]" if auto and self._fix
                 else f"[Running the tests in {root.name} with {python}, offline in the sandbox]")
        self._start(lambda line_out, stop: sb.run(argv, root, line_out, stop,
                                                  timeout=sb.TEST_TIMEOUT_S,
                                                  memory_mb=sb.TEST_MEMORY_MB, env=env),
                    label, "The tests", "test")
        return True

    def _start(self, job, label: str, name: str, kind: str) -> None:
        self._running = True
        self._stop = threading.Event()
        for b in (self.run_btn, self.test_btn, self.fix_btn, self.preview_btn):
            b.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.append(label)
        stop = self._stop
        self._run_lines = []

        def line_out(line: str) -> None:
            self._to_ui(self._run_line, line)

        def work() -> None:
            outcome = job(line_out, stop)
            self._to_ui(self._finished, outcome, name, kind)

        threading.Thread(target=work, name="build-run", daemon=True).start()

    def _finished(self, outcome: sb.Outcome, name: str, kind: str = "run") -> None:
        self._running = False
        for b in (self.run_btn, self.test_btn, self.preview_btn):
            b.setEnabled(True)
        self.stop_btn.setEnabled(False)
        output = "\n".join(self._run_lines)
        tests = sb.TestOutcome(outcome, output) if kind == "test" else None
        if tests is not None and tests.missing_pytest():
            self.append("[pytest is not installed in this Python: pick another under "
                        "\"Run with\", or install it there (pip install pytest)]")
        self.append(outcome.summary(name) if tests is None else
                    f"[{'Tests passed' if tests.passed else 'Tests failed'}: {tests.headline()}]")
        self.say("")
        self._last = {"kind": kind, "name": name, "failed": outcome.failed,
                      "output": output,
                      "headline": tests.headline() if tests else outcome.summary(name)}
        self.fix_btn.setEnabled(outcome.failed)
        # a failed run is usually what the next question is about
        self.chat_panel.use_output.setChecked(outcome.failed)
        if tests is not None:
            self._round_done(tests)

    # -- Fix this, and the rounds the model runs the tests in -------------------
    def on_fix(self) -> None:
        """Send the last failure to the chat in Edit mode. A failed TEST run
        starts the rounds: after each fix is applied the tests run again by
        themselves, at most MAX_ROUNDS times, and stop when the same failure
        comes back (council_core.build_sandbox.stuck)."""
        last = self._last
        if not last or not last["failed"] or self.chat_panel.is_busy():
            return
        if self.chat_panel.proposal is not None:
            self.say("Apply or reject the proposed edits first.")
            return
        what = "The tests" if last["kind"] == "test" else last["name"]
        text = (f"Fix this: {what} failed ({last['headline']}). The output is shown "
                "above; find the cause and fix it.")
        self._fix = ({"runs": 0, "signatures": [sb.signature(last["output"])],
                      "request": text} if last["kind"] == "test" else None)
        self._ask_fix(text)

    def _ask_fix(self, text: str) -> None:
        p = self.chat_panel
        p.set_mode("edit")
        p.use_file.setChecked(True)
        p.use_output.setChecked(True)
        if self._fix is not None:
            self._fix["request"] = text
        p.send(text, kind="fix")

    def _fix_applied(self, request: str) -> None:
        """After Apply: in the rounds, the tests run again by themselves."""
        fx = self._fix
        if fx is None or request != fx.get("request"):
            self._fix = None
            return
        fx["runs"] += 1
        fx["auto"] = True
        if not self.on_test(auto=True):
            self._fix = None

    def _round_done(self, tests: sb.TestOutcome) -> None:
        fx = self._fix
        if fx is None or not fx.pop("auto", False):
            return
        n = fx["runs"]
        if tests.outcome.stopped:
            self._fix = None
            self.chat_panel.note("The test rounds are stopped.",
                                 [{"kind": "test", "state": "no",
                                   "text": f"Round {n}: the tests were stopped by you"}])
            return
        step = {"kind": "test", "state": "ok" if tests.passed else "no",
                "text": f"Ran the tests (round {n} of {sb.MAX_ROUNDS}): {tests.headline()}"}
        if tests.passed:
            self._fix = None
            self.chat_panel.note(f"The tests pass after round {n}.", [step])
            return
        fx["signatures"].append(tests.signature())
        if sb.stuck(fx["signatures"]):
            self._fix = None
            self.chat_panel.note("Stopped: the same failure came back after the fix.",
                                 [step, {"kind": "stuck", "state": "no",
                                         "text": "Stopped: the same failure came back"}])
            self.chat_panel.ask_blocker("the same failure came back after your fix")
            return
        if n >= sb.MAX_ROUNDS:
            self._fix = None
            self.chat_panel.note(f"Stopped after {n} rounds: the tests still fail.",
                                 [step, {"kind": "stuck", "state": "no",
                                         "text": f"Stopped after {n} rounds"}])
            self.chat_panel.ask_blocker(f"the tests still fail after {n} rounds of fixes")
            return
        self.chat_panel.note(f"Round {n}: the tests still fail; asking for another fix.", [step])
        self._ask_fix(f"The tests still fail after your fix (round {n} of {sb.MAX_ROUNDS}): "
                      f"{tests.headline()}. The new output is shown above; fix it.")

    # -- Preview -------------------------------------------------------------
    def on_preview(self) -> None:
        """Build the open file's widget offscreen (council_core.gui_check)
        and show what it looks like, with any error."""
        doc = self.current()
        if self._running or doc is None or not doc.editor.is_python():
            self.say("Open a Python file with a Qt widget to preview it.")
            return
        classes = sb.widget_classes(doc.editor.toPlainText())
        module = sb.module_name(self.edit_root(), doc.path)
        if not classes or module is None:
            self.say(f"No Qt widget class to preview in {doc.path.name}." if not classes
                     else f"{doc.path.name} is outside the folder; open its folder first.")
            return
        if doc.modified() and not self.save(doc):
            return
        cls, root = classes[0], self.edit_root()
        out = self.actions.vault_dir / ".council_build" / "preview" / time.strftime(
            "%Y%m%d-%H%M%S")
        self.append(f"[Preview: building {module}.{cls} offscreen]")
        self._running = True
        for b in (self.run_btn, self.test_btn, self.fix_btn, self.preview_btn):
            b.setEnabled(False)

        def work() -> None:
            from council_core import gui_check
            res = gui_check.check(root, module, cls, out_dir=out)
            self._to_ui(self._preview_done, res)

        threading.Thread(target=work, name="build-preview", daemon=True).start()

    def _preview_done(self, res) -> None:
        self._running = False
        for b in (self.run_btn, self.test_btn, self.preview_btn):
            b.setEnabled(True)
        summary = res.summary(2000)
        for line in summary.split("\n")[:40]:
            self.append(line)
        self._last = {"kind": "preview", "name": f"The preview of {res.cls}",
                      "failed": not res.ok, "output": summary,
                      "headline": (res.errors[0].strip().splitlines()[-1][:160]
                                   if res.errors else "it did not build")}
        self._run_lines = summary.split("\n")
        self.fix_btn.setEnabled(not res.ok)
        self.chat_panel.use_output.setChecked(not res.ok)
        if res.ok or res.screenshot:
            self.actions.show_preview(res.screenshot, summary)

    def _run_line(self, line: str) -> None:
        self._run_lines.append(line)
        if len(self._run_lines) > MAX_OUTPUT_LINES:
            del self._run_lines[:len(self._run_lines) - MAX_OUTPUT_LINES]
        self.append(line)

    # -- the chat's view of the editor -----------------------------------------
    def chat_context(self, use_file: bool, use_output: bool) -> bc.Context:
        """What the chat's model is shown: the open file as edited, the
        selected lines (or the cursor's), and the last run's output."""
        ctx = bc.Context(folder=str(self.project or self.root or ""),
                         root=str(self.root or ""))
        ctx.extra, self._extra = self._extra, ""
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


    # -- Edit mode: what the chat's edits need (the chat's host) ----------------
    def edit_root(self) -> Path:
        return Path(self.root or ".")

    def _path(self, rel: str) -> Path:
        return Path(self.root) / rel

    def read_for_edit(self, rel: str) -> Optional[str]:
        """The file's text now: the editor's buffer when it is open, else the
        disk; None when it does not exist (an edit may create it)."""
        path = self._path(rel)
        doc = self.doc_for(path)
        if doc is not None:
            return doc.editor.toPlainText()
        if not path.exists():
            return None
        return bf.open_text(path).text

    def other_files(self, current: str, extra: List[str]) -> List[tuple]:
        """The other open files (as edited) and ``extra`` (a plan's files),
        for an Edit-mode prompt."""
        out, seen = [], {str(Path(current).resolve())} if current else set()
        for d in self.docs:
            key = str(d.path.resolve())
            if key not in seen:
                seen.add(key)
                out.append((str(d.path), d.editor.toPlainText()))
        for rel in extra:
            safe = be.safe_rel(self.edit_root(), rel)
            if safe is None:
                continue
            path = self._path(safe)
            key = str(path.resolve())
            if key in seen or not path.is_file():
                continue
            seen.add(key)
            try:
                out.append((str(path), bf.open_text(path).text))
            except bf.FileProblem:
                continue
        return out

    def code_map(self, focus: str):
        """The folder's outline for the prompt, and the map that checks a
        plan's names (called from the chat's worker thread: disk only)."""
        cache = self.actions.vault_dir / ".council_build" / "maps"
        return bc.code_map_for(self.edit_root(), focus, cache)

    def open_location(self, rel: str, line: int) -> None:
        safe = be.safe_rel(self.edit_root(), rel)
        if safe is None:
            return
        doc = self.open_file(self._path(safe))
        if doc is None:
            return
        block = doc.editor.document().findBlockByNumber(max(0, line - 1))
        cur = doc.editor.textCursor()
        cur.setPosition(block.position())
        doc.editor.setTextCursor(cur)
        doc.editor.centerCursor()
        doc.editor.setFocus()

    @staticmethod
    def _set_text(doc: _Doc, text: str) -> None:
        """Replace the buffer as ONE undo step (Ctrl+Z takes the edit back)."""
        cur = QTextCursor(doc.editor.document())
        cur.beginEditBlock()
        cur.select(QTextCursor.SelectionType.Document)
        cur.insertText(text)
        cur.endEditBlock()

    def _doc_for_edit(self, rel: str, exists: bool) -> Optional[_Doc]:
        path = self._path(rel)
        doc = self.doc_for(path)
        if doc is not None:
            return doc
        if not exists:
            try:
                bf.new_file(self.edit_root(), rel)
            except bf.FileProblem as exc:
                self.say(str(exc))
                return None
            self.refresh_tree()
        return self.open_file(path)

    def apply_edits(self, prop: be.Proposal, request: str):
        """Put the accepted edits into the files: into each file's editor as
        one undo step; in a session also saved into the session's copy and
        committed as one turn. (steps for the chat, a message)."""
        changes = prop.accepted_changes()
        if not changes:
            self._fix = None
            return ([{"kind": "apply", "state": "no", "text": "No edit was accepted"}],
                    "Nothing applied: no edit was accepted.")
        if self.session is not None:
            if not self.save_all():
                raise RuntimeError("an open file could not be saved")
            self.session.commit_own_edits()
        before: Dict[str, Optional[str]] = {}
        records: List[bs.EditRecord] = []
        done: List[str] = []
        for fc in changes:
            doc = self._doc_for_edit(fc.path, fc.exists)
            if doc is None:
                continue
            before[str(doc.path)] = doc.editor.toPlainText() if fc.exists else ""
            after = fc.after()
            self._set_text(doc, after)
            if self.session is not None:
                self.save(doc)
            shift = 0
            for e in sorted((e for e in fc.edits if e.ok and e.accepted), key=lambda e: e.start):
                old = fc.before[e.start:e.end]
                line = fc.first_line(e) + shift
                records.append(bs.edit_record(fc.path, after, line, e.new, old, e.block.reason))
                shift += e.new.count("\n") - old.count("\n")
            done.append(fc.path)
        n = sum(1 for fc in changes for e in fc.edits if e.ok and e.accepted)
        steps = [{"kind": "apply", "state": "ok",
                  "text": f"Applied {n} edit(s) to {', '.join(done)}"}]
        if self.session is not None:
            turn = self.session.commit_turn(request, records)
            if turn is not None:
                steps.append({"kind": "commit", "state": "ok",
                              "text": f"Committed as turn {turn.number} ({turn.commit[:7]}) "
                                      f"on {self.session.branch}"})
            message = (f"Applied and committed as turn {turn.number}. Undo turn takes it back."
                       if turn else "Applied; nothing changed.")
        else:
            self._turns.append({"request": request, "before": before})
            steps.append({"kind": "apply", "state": "ok",
                          "text": "In the editor, not saved yet: Save writes them"})
            message = "Applied in the editor, not saved yet. Save writes them; Undo turn takes them back."
        self._show_session()
        QTimer.singleShot(0, lambda r=request: self._fix_applied(r))
        return steps, message

    # -- sessions ----------------------------------------------------------------
    def _carry_docs(self, old_root: Path, new_root: Path) -> None:
        """Close every open file under ``old_root`` and open its counterpart
        under ``new_root``, keeping unsaved edits as unsaved edits."""
        carried = []
        for d in list(self.docs):
            try:
                rel = d.path.resolve().relative_to(old_root.resolve())
            except ValueError:
                continue
            carried.append((rel, d.editor.toPlainText() if d.modified() else None))
            self.docs.remove(d)
            i = self.editors.indexOf(d.editor)
            if i >= 0:
                self.editors.removeTab(i)
            d.editor.deleteLater()
        for rel, text in carried:
            path = new_root / rel
            if not path.is_file():
                continue
            doc = self.open_file(path)
            if doc is not None and text is not None:
                self._set_text(doc, text)

    def start_session(self) -> None:
        if self.session is not None or self.root is None:
            return
        if self.chat_panel.is_busy():
            self.say("Wait for the chat to finish first.")
            return
        try:
            session = bs.Session.start(self.actions.vault_dir, self.root)
        except wt.WorktreeError as exc:
            self.say(str(exc))
            return
        old = self.root
        self.session = session
        self.open_folder(session.tree_folder(), project=old)
        self._carry_docs(old, self.root)
        self.say(f"Session started on branch {session.branch}. Your folder is not "
                 "changed until you merge.")

    def _close_session(self, message: str) -> None:
        project, tree = self.project, self.root
        self.session = None
        self.open_folder(project)
        self._carry_docs(tree, project)
        self.say(message)

    def end_session(self) -> None:
        s = self.session
        if s is None:
            return
        live = [t for t in s.turns if not t.undone]
        if not self.actions.confirm(
                "End the session",
                f"End the session without merging? Its {len(live)} turn(s) on "
                f"{s.branch} are thrown away. Your project folder is not changed."):
            return
        for d in list(self.docs):
            d.editor.document().setModified(False)     # the session's copies
        s.end()
        self.session = None
        project = Path(s.folder)
        tree = self.root
        self.open_folder(project)
        self._carry_docs(tree, project)
        self.say("Session ended; nothing was merged.")

    def review_and_merge(self) -> None:
        s = self.session
        if s is None:
            return
        if not self.save_all():
            return
        try:
            groups = s.review()
        except wt.WorktreeError as exc:
            self.say(str(exc))
            return
        if not groups:
            self.say("The session has no changes to merge.")
            return
        answer, n = self.actions.review(bs.review_markdown(groups))
        if answer == "ask" and n is not None and 0 <= n < len(groups):
            g = groups[n]
            self._extra = (f"The user is asking about this change in the session "
                           f"({g.title()}):\n```diff\n{g.diff}\n```")
            self.chat_panel.set_mode("ask")
            self.chat_panel.input.setPlainText(f"About {g.title()}: ")
            self.chat_panel.input.setFocus()
            self.say("Ask the chat about it, then Review and merge again.")
            return
        if answer != "merge":
            return
        try:
            line = s.merge()
        except wt.WorktreeError as exc:
            self.say(str(exc))
            return
        self._close_session(f"Merged into {s.folder}: {line}")

    def undo_turn(self) -> None:
        """Take the newest applied turn back."""
        if self.chat_panel.is_busy():
            return
        s = self.session
        if s is not None:
            if not self.save_all():
                return
            try:
                turn = s.undo_turn()
            except wt.WorktreeError as exc:
                self.say(str(exc))
                return
            self._reload_docs()
            self.refresh_tree()
            self.chat_panel.note(f"Turn {turn.number} undone.",
                                 [{"kind": "undo", "state": "ok",
                                   "text": f"Undid turn {turn.number} (a revert commit)"}])
            self._show_session()
            return
        if not self._turns:
            return
        last = self._turns.pop()
        for path, text in last["before"].items():
            doc = self.doc_for(Path(path)) or self.open_file(Path(path))
            if doc is not None and text is not None:
                self._set_text(doc, text)
        self.chat_panel.note("The last turn's edits are taken back (in the editor).",
                             [{"kind": "undo", "state": "ok",
                               "text": "Undid the last turn's edits in the editor"}])
        self._show_session()

    def _reload_docs(self) -> None:
        """Every open file read again from disk (after a revert), and a tab
        whose file the revert removed closed."""
        for d in list(self.docs):
            if not d.path.is_file():
                self.docs.remove(d)
                i = self.editors.indexOf(d.editor)
                if i >= 0:
                    self.editors.removeTab(i)
                d.editor.deleteLater()
                continue
            try:
                d.opened = bf.open_text(d.path)
            except bf.FileProblem:
                continue
            self._set_text(d, d.opened.text)
            d.editor.document().setModified(False)


def build_build(window) -> QWidget:
    """Factory for the tab registry."""
    return BuildTab(window)
