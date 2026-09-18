"""
council_qt.tabs.forge — Tool Creation, ported.

Describe a tool in plain English; the local model writes the Python, the
analyst sandbox validates it read-only and test-runs it, and it lands in
<vault>/App_Built_tools/ UNREVIEWED.

That word is carried through every message this tab shows, because the one
conclusion a user must not draw is that something checked the code for them.
The sandbox proves the tool does not delete, write, reach the network or shell
out. It does not prove the tool is right.

All three actions run on workers here — including Save, which the Tk tab runs
on the GUI thread. Saving writes a file and re-validates it.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import List, Optional

from PySide6.QtWidgets import (QGroupBox, QHBoxLayout, QLabel, QListWidget,
                               QPlainTextEdit, QSplitter, QVBoxLayout, QWidget)
from PySide6.QtCore import Qt

from council_core import forge_jobs
from council_core import paths

from .. import theme
from ..view import ViewHelpers


class ForgeActions:
    """What the Tool Creation tab can ask the application to do."""

    def __init__(self, vault_dir: Optional[Path] = None):
        self.vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()

    def list_tools(self):
        return forge_jobs.list_tools(self.vault_dir)

    def forge(self, task: str):
        return forge_jobs.forge(task, self.vault_dir)

    def save_edited(self, code: str):
        return forge_jobs.save_edited(code, self.vault_dir)

    def run_tool(self, name: str):
        return forge_jobs.run_tool(name, self.vault_dir)


class ForgeTab(ViewHelpers, QWidget):
    """A task box, a code pane, a tool list and an output pane."""

    def __init__(self, window=None, actions: Optional[ForgeActions] = None):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or ForgeActions()
        self._tokens = theme.tokens("dark")
        self._names: List[str] = []
        self._busy = False

        self._build()
        self.refresh_list()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)

        blurb = QLabel(
            "Describe a tool you need — the local model writes it, the "
            "sandbox validates it read-only and test-runs it, and it is saved "
            "UNREVIEWED. Read the code before you trust it.")
        blurb.setWordWrap(True)
        blurb.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        outer.addWidget(blurb)

        outer.addWidget(QLabel("Task:"))
        self.task = QPlainTextEdit()
        self.task.setMaximumHeight(70)
        self.task.setPlaceholderText(
            "e.g. “count rows per supplier in every CSV and rank them”")
        outer.addWidget(self.task)

        row = QHBoxLayout()
        self.generate_btn = self._button(row, "✨ Generate Tool", self.on_generate)
        self.save_btn = self._button(row, "💾 Save Edited Code", self.on_save)
        self.run_btn = self._button(row, "▶ Run Selected", self.on_run)
        self._button(row, "⟳ Refresh", self.refresh_list)
        row.addStretch(1)
        outer.addLayout(row)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self._code_box())
        split.addWidget(self._right_side())
        split.setSizes([560, 420])
        outer.addWidget(split, 1)

        self.status = QLabel("Ready.")
        outer.addWidget(self.status)

    def _code_box(self) -> QGroupBox:
        box = QGroupBox("Tool code (editable — review before trusting)")
        layout = QVBoxLayout(box)
        self.code = QPlainTextEdit()
        self.code.setLineWrapMode(QPlainTextEdit.NoWrap)
        layout.addWidget(self.code)
        return box

    def _right_side(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        list_box = QGroupBox("Existing app-built tools")
        list_layout = QVBoxLayout(list_box)
        self.tools = QListWidget()
        list_layout.addWidget(self.tools)
        layout.addWidget(list_box, 1)

        out_box = QGroupBox("Output")
        out_layout = QVBoxLayout(out_box)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        out_layout.addWidget(self.output)
        layout.addWidget(out_box, 1)
        return panel

    # ------------------------------------------------------------------
    def selected_tool(self) -> Optional[str]:
        index = self.tools.currentRow()
        return self._names[index] if 0 <= index < len(self._names) else None

    def refresh_list(self) -> None:
        result = self.actions.list_tools()
        self.tools.clear()
        self._names = list(result.names)
        for name, blurb in result.rows:
            self.tools.addItem(f"{name}  —  {blurb}")
        if not result.ok:
            # The Tk version swallows any failure into an empty list, so a
            # broken tools directory looks exactly like an empty one.
            self.status.setText(result.message)

    def _start(self, status: str, call, *, then=None) -> None:
        """Run one forge action on a worker and report it.

        Every action goes through here, including Save — which the Tk tab runs
        on the GUI thread even though it writes a file and re-validates it.
        """
        if self._busy:
            self.status.setText("Already working — wait for it to finish.")
            return
        self._busy = True
        self._set_buttons(False)
        self.status.setText(status)

        def work() -> None:
            result = call()

            def show() -> None:
                self._busy = False
                self._set_buttons(True)
                self.status.setText(result.status)
                if result.body:
                    self.output.setPlainText(result.body)
                if result.code:
                    self.code.setPlainText(result.code)
                if then is not None:
                    then(result)

            self._to_ui(show)

        threading.Thread(target=work, name="forge", daemon=True).start()

    def _set_buttons(self, enabled: bool) -> None:
        for button in (self.generate_btn, self.save_btn, self.run_btn):
            button.setEnabled(enabled)

    def on_generate(self) -> None:
        task = self.task.toPlainText().strip()
        if not task:
            self.status.setText("Describe a tool first.")
            return
        self._start("Generating… the local model is writing the tool.",
                    lambda: self.actions.forge(task),
                    then=lambda result: self.refresh_list())

    def on_save(self) -> None:
        code = self.code.toPlainText().strip()
        if not code:
            self.status.setText("Nothing to save — generate or paste code first.")
            return
        self._start("Saving…", lambda: self.actions.save_edited(code),
                    then=lambda result: self.refresh_list())

    def on_run(self) -> None:
        name = self.selected_tool()
        if not name:
            self.status.setText("Select a tool in the list to run.")
            return
        self._start(f"Running '{name}'…", lambda: self.actions.run_tool(name))


def build_forge(window) -> QWidget:
    """Factory for the tab registry."""
    return ForgeTab(window)
