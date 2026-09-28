"""
council_qt.widgets.vault_agent_panel — a sandboxed ReAct agent over the vault.

Ported from `vault_agent.VaultAgentPanel` (Tk). The agent itself
(`vault_agent.VaultAgent`) is toolkit-free and synchronous; this is the task
box, the model picker, the quick-task menu and the step log around it.

THE MODEL IS READ BEFORE THE THREAD STARTS
Tk's worker calls `self._model_var.get()` — a Tk variable, read from the
worker thread (vault_agent.py:699). Tkinter happens to marshal that; the Qt
spelling, a QComboBox read off the GUI thread, is an access violation. The
task and the role are captured here, on the GUI thread, and the worker is
handed plain strings.

RESOLVING A ROLE TO A MODEL CAN BE SLOW
`resolve(role)` may have to load the personalities from disk the first time,
so it runs on the worker too — never on the GUI thread.

ONE RUN AT A TIME, SAID OUT LOUD
Tk guards with a modal "Agent is already running". Here the Run button is
disabled for the run and a second Return in the task box writes a line.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, List, Optional

from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (QComboBox, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QMenu, QPushButton, QTextEdit,
                               QVBoxLayout, QWidget)

from .. import theme
from ..view import ViewHelpers, amp

#: The roles the Tk picker offers, in its order.
ROLES = ("writer", "coder", "judge", "intern", "peasant")

#: (label, task). Verbatim from the Tk panel.
PRESETS = (
    ("List all vault files",
     "List all files and directories in the vault, organized by subdirectory."),
    ("Summarise RAG misses",
     "Read vault_rag_misses.txt and write a summary of the most common topics "
     "I've searched for that weren't in the vault. Save it as "
     "rag_miss_summary.txt."),
    ("Organise loose text files",
     "List all .txt files in the vault root. Move any that look like notes or "
     "knowledge into a notes/ subdirectory, and any that look like logs into "
     "logs/."),
    ("Create index of vault contents",
     "Search all subdirectories and create a file called vault_index.md that "
     "lists every file with a one-line description of its contents."),
    ("Find duplicate/similar files",
     "Search across all vault text files for any that appear to cover the "
     "same topic. List them in a file called potential_duplicates.txt."),
    ("Clean up empty files",
     "Find all files under 10 bytes in the vault and list them. "
     "Ask me before deleting any."),
)

#: event phase -> (log tag, prefix). Tk's two maps, merged.
PHASES = {
    "thinking":    ("hdr",       "  ⏳ "),
    "thought":     ("thought",   "  💭 "),
    "tool_call":   ("tool_call", "  → "),
    "tool_result": ("tool_ok",   "  ← "),
    "done":        ("done",      "✓ "),
    "error":       ("error",     "✗ "),
    "agent_start": ("hdr",       "\n▶ "),
}


def event_line(phase: str, msg: str):
    """(tag, text) for one agent event."""
    tag, prefix = PHASES.get(phase, ("thought", "  "))
    return tag, prefix + msg


def summary_lines(steps: List[Any]):
    """[(tag, text)] for the end of a run — the answer, or why there is none."""
    lines = []
    done = [s for s in steps if s.kind == "done"]
    if done:
        lines.append(("hdr", "\n── Final Answer ──────────────────────────────"))
        lines.append(("done", done[-1].content))
    else:
        errors = [s for s in steps if s.kind == "error"]
        if errors:
            lines.append(("error", f"\nAgent stopped: {errors[-1].content}"))
    calls = sum(1 for s in steps if s.kind == "tool_call")
    lines.append(("hdr", f"\n[{len(steps)} steps, {calls} tool calls]"))
    return lines


def _default_runner(model, vault_dir: Path, task: str, on_event):
    import vault_agent
    return vault_agent.VaultAgent(model, vault_dir,
                                  event_callback=on_event).run(task)


class VaultAgentPanel(ViewHelpers, QWidget):
    """Task box, model picker, quick tasks, step log."""

    def __init__(self, resolve: Callable[[str], Any], vault_dir: Path,
                 parent: Optional[QWidget] = None,
                 runner: Callable[..., List[Any]] = _default_runner):
        super().__init__(parent)
        self.resolve = resolve
        self.vault_dir = Path(vault_dir)
        self._runner = runner
        self._busy = False
        self._tokens = theme.tokens("dark")
        self._build()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)

        header = QHBoxLayout()
        title = QLabel(amp("🗂  Vault Agent"))
        title.setStyleSheet("font-weight: bold;")
        header.addWidget(title)
        note = QLabel("(sandbox: vault only — cannot touch your system files)")
        note.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        header.addWidget(note)
        header.addStretch(1)
        outer.addLayout(header)

        box = QGroupBox("Task")
        row = QHBoxLayout(box)
        self.task = QLineEdit()
        self.task.setPlaceholderText("What should the agent do in the vault?")
        self.task.returnPressed.connect(self.on_run)
        row.addWidget(self.task, 1)
        row.addWidget(QLabel("Model:"))
        self.model = QComboBox()
        self.model.addItems(ROLES)
        row.addWidget(self.model)
        outer.addWidget(box)

        actions = QHBoxLayout()
        self.run_btn = self._button(actions, "▶  Run Task", self.on_run)
        self.presets_btn = QPushButton(amp("📋  Quick tasks ▾"))
        menu = QMenu(self.presets_btn)
        for label, text in PRESETS:
            act = menu.addAction(amp(label))
            act.setToolTip(text)
            act.triggered.connect(lambda _=False, t=text: self.run_preset(t))
        menu.setToolTipsVisible(True)
        self.presets_btn.setMenu(menu)
        actions.addWidget(self.presets_btn)
        actions.addStretch(1)
        self._button(actions, "Clear log", self.clear)
        outer.addLayout(actions)

        outer.addWidget(QLabel("Steps:"))
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setStyleSheet("font-family: Consolas, monospace;")
        outer.addWidget(self.log, 1)

        t = self._tokens
        self._colours = {
            "thought": t["fg"], "tool_call": t["info"],
            "tool_ok": t["success"], "tool_fail": t["error"],
            "done": t["success"], "error": t["error"], "hdr": t["warning"],
        }

    # -- the log ---------------------------------------------------------
    def append(self, text: str, tag: str = "thought") -> None:
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(self._colours.get(tag, self._tokens["fg"])))
        if tag == "done":
            fmt.setFontWeight(700)
        cursor = self.log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text.rstrip() + "\n", fmt)
        self.log.setTextCursor(cursor)
        self.log.ensureCursorVisible()

    def clear(self) -> None:
        self.log.clear()

    # -- running ---------------------------------------------------------
    def run_preset(self, text: str) -> None:
        self.task.setText(text)
        self.on_run()

    def on_run(self) -> None:
        task = self.task.text().strip()
        if not task:
            return
        if self._busy:
            self.append("The agent is already running — wait for it to "
                        "finish.", "error")
            return
        role = self.model.currentText()          # read HERE, on the GUI thread
        self._busy = True
        self.run_btn.setEnabled(False)
        self.clear()
        self.append(f"Task: {task}", "hdr")
        self.append(f"Model: {role}  |  Vault: {self.vault_dir}\n", "hdr")

        def on_event(phase: str, msg: str) -> None:
            tag, text = event_line(phase, msg)
            self._to_ui(self.append, text, tag)

        def work() -> None:
            try:
                model = self.resolve(role)
                if model is None:
                    self._to_ui(self.append,
                                f"No '{role}' model is loaded. Check the "
                                "model pins in the Models tab.", "error")
                    return
                steps = self._runner(model, self.vault_dir, task, on_event)
                for tag, text in summary_lines(steps):
                    self._to_ui(self.append, text, tag)
            except Exception as exc:                      # noqa: BLE001
                self._to_ui(self.append, f"Agent error: {exc}", "error")
            finally:
                self._to_ui(self._done)

        threading.Thread(target=work, name="vault-agent", daemon=True).start()

    def _done(self) -> None:
        self._busy = False
        self.run_btn.setEnabled(True)
