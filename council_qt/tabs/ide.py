"""
council_qt.tabs.ide — a code pane, an output pane, and a subprocess.

Advanced mode, as in Tk. It runs the buffer as a real Python process with the
user's own permissions, which is why the trust gate in front of it matters and
why the gate lives in `council_core.ide_trust` where it can be tested.

THE OUTPUT PANE IS CAPPED AND DOES NOT STEAL THE SCROLLBAR
The Tk pane is unbounded and calls `see("end")` on every line, and the queue
drain is a `while True` with no per-tick limit. A script printing a few hundred
thousand lines takes the window with it. `setMaximumBlockCount` bounds the
widget, and the scroll only follows when the reader is already at the bottom —
so scrolling up to read a traceback while output is still arriving works.

ONE RUN AT A TIME
Neither Tk path has a busy guard, and both derive the same file path from the
same script name. Start a long streaming run, edit the buffer, press Run: the
second invocation overwrites the exact .py the first subprocess is executing,
and the traceback you get points at code that is no longer what ran.

THE STREAMING CALLBACKS COME FROM SOMEONE ELSE'S THREADS
`run_code_streaming` starts two daemon readers inside council_engine, and the
per-line callbacks fire on THOSE — not on the worker this tab started. So each
line is marshalled from inside the callback.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
                               QSplitter, QVBoxLayout, QWidget)
from PySide6.QtCore import Qt

from council_core import ide_jobs, ide_trust, paths

from .. import theme
from ..view import ViewHelpers, amp

#: How many lines the output pane keeps. A run that prints a million lines is
#: a run whose first thousand are the interesting ones and whose last thousand
#: are what it ended on; the middle is not worth the window freezing for.
MAX_BLOCKS = 5_000


class IdeActions:
    """What the IDE tab can ask the application to do."""

    def __init__(self, workspace: Optional[Path] = None,
                 vault_dir: Optional[Path] = None):
        self.vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()
        self.workspace = Path(workspace) if workspace \
            else self.vault_dir / "workspace"
        self._runner = None
        self._librarian = None

    @property
    def runner(self):
        if self._runner is None:
            import council_engine
            paths.ensure(self.workspace)
            self._runner = council_engine.LocalRunner(self.workspace)
        return self._runner

    @property
    def librarian(self):
        if self._librarian is None:
            import council_engine
            self._librarian = council_engine.Librarian(
                self.vault_dir, self.vault_dir / "logs" / "council.log")
        return self._librarian

    def run_blocking(self, code: str, name: str) -> ide_jobs.RunResult:
        return ide_jobs.run_blocking(self.runner, code, name=name)

    def run_streaming(self, code: str, name: str, on_stdout, on_stderr):
        return ide_jobs.run_streaming(self.runner, code, name=name,
                                      on_stdout=on_stdout,
                                      on_stderr=on_stderr)

    def snapshot(self, code: str) -> ide_jobs.RunResult:
        return ide_jobs.snapshot(self.librarian, code)


class IdeTab(ViewHelpers, QWidget):
    """Editor, output, and four buttons."""

    def __init__(self, window=None, actions: Optional[IdeActions] = None,
                 confirm=None):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or IdeActions()
        self._tokens = theme.tokens("dark")
        self.trust = ide_trust.TrustStore()
        self._busy = False
        #: Supplied by the host so a test never opens a dialog.
        self.confirm = confirm or (lambda *a, **k: False)

        self._build()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)

        row = QHBoxLayout()
        row.addWidget(QLabel("Script name:"))
        self.name_box = QLineEdit("script")
        self.name_box.setMaximumWidth(220)
        row.addWidget(self.name_box)
        self.run_btn = self._button(row, amp("▶ Run"), self.on_run)
        self.stream_btn = self._button(row, amp("▶ Run (streaming)"),
                                       self.on_run_streaming)
        self._button(row, amp("💾 Snapshot to vault"), self.on_snapshot)
        self._button(row, amp("Clear output"), self.clear_output)
        row.addStretch(1)
        self.status = QLabel("")
        row.addWidget(self.status)
        outer.addLayout(row)

        split = QSplitter(Qt.Orientation.Vertical)
        self.code = QPlainTextEdit()
        self.code.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.code.setFont(QFont("Consolas", 10))
        self.code.setPlaceholderText("# Python. Run executes this as a real "
                                     "subprocess, with your permissions.")
        split.addWidget(self.code)

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.output.setFont(QFont("Consolas", 10))
        # The Tk pane is unbounded. A script printing a few hundred thousand
        # lines takes the window with it.
        self.output.setMaximumBlockCount(MAX_BLOCKS)
        split.addWidget(self.output)
        split.setSizes([420, 260])
        outer.addWidget(split, 1)

    # ------------------------------------------------------------------
    def script_name(self) -> str:
        return self.name_box.text() or "script"

    def clear_output(self) -> None:
        self.output.clear()

    def append(self, text: str) -> None:
        """Add output, following the scroll ONLY if the reader is at the end.

        Tk calls `see("end")` on every line, so scrolling up to read a
        traceback while output is still arriving is impossible.
        """
        if not text:
            return
        bar = self.output.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4
        cursor = self.output.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text if text.endswith("\n") else text + "\n")
        if at_bottom:
            bar.setValue(bar.maximum())

    # ------------------------------------------------------------------
    def _allowed(self, code: str) -> bool:
        """The trust gate. Asks once per exact script text, per session."""
        verdict = ide_trust.check(code, self.trust)
        if verdict.allowed:
            return True
        if self.confirm("Confirm code execution", verdict.prompt):
            self.trust.trust(code)
            return True
        self.append("[Cancelled by user]")
        return False

    def _start(self, what: str) -> Optional[str]:
        """(code) if a run may begin, else None."""
        if self._busy:
            self.status.setText("A run is already going.")
            return None
        code = self.code.toPlainText()
        if not code.strip():
            return None
        if not self._allowed(code):
            return None
        self._busy = True
        self.run_btn.setEnabled(False)
        self.stream_btn.setEnabled(False)
        self.status.setText(what)
        return code

    def _finish(self, result: ide_jobs.RunResult) -> None:
        self._busy = False
        self.run_btn.setEnabled(True)
        self.stream_btn.setEnabled(True)
        self.append(result.summary(f"{ide_jobs.script_basename(self.script_name())}.py"))
        self.status.setText("")

    # ------------------------------------------------------------------
    def on_run(self) -> None:
        code = self._start("Running…")
        if code is None:
            return
        name = self.script_name()

        def work() -> None:
            result = self.actions.run_blocking(code, name)

            def show() -> None:
                # The partial output a timeout CARRIES. Tk formats the
                # exception and drops it.
                if result.stdout:
                    self.append(result.stdout)
                if result.stderr:
                    self.append(result.stderr)
                self._finish(result)

            self._to_ui(show)

        threading.Thread(target=work, name="ide-run", daemon=True).start()

    def on_run_streaming(self) -> None:
        code = self._start("Running…")
        if code is None:
            return
        name = self.script_name()

        def work() -> None:
            # These fire on the RUNNER's drain threads, not on this one, so
            # each line marshals from inside the callback.
            def out(line: str) -> None:
                self._to_ui(lambda line=line: self.append(line.rstrip("\n")))

            result = self.actions.run_streaming(code, name, out, out)
            self._to_ui(lambda: self._finish(result))

        threading.Thread(target=work, name="ide-stream", daemon=True).start()

    def on_snapshot(self) -> None:
        """Save the buffer into the vault, on a worker — it is a disk write,
        which the Tk tab does on the GUI thread."""
        code = self.code.toPlainText()
        if not code.strip():
            self.status.setText("Nothing to snapshot.")
            return

        def work() -> None:
            result = self.actions.snapshot(code)

            def show() -> None:
                if result.error:
                    self.append(f"[snapshot failed: {result.error}]")
                else:
                    self.append(f"[snapshot saved: {result.path}]")
                self._report(result)

            self._to_ui(show)

        threading.Thread(target=work, name="ide-snapshot",
                         daemon=True).start()

    def _report(self, result: ide_jobs.RunResult) -> None:
        append = getattr(self.window, "append_transcript", None)
        if append is None:
            return
        try:
            append("Librarian",
                   f"snapshot: {result.path}" if not result.error
                   else f"snapshot failed: {result.error}", "final")
        except Exception:                                 # noqa: BLE001
            pass


def build_ide(window) -> QWidget:
    """Factory for the tab registry."""
    return IdeTab(window)
