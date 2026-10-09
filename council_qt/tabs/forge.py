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

from PySide6.QtWidgets import (QComboBox, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QPlainTextEdit,
                               QSplitter, QVBoxLayout, QWidget)
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
        """Write the tool, then its tests (council_core.tool_review), and
        run them — so the user sees at once whether it works."""
        result = forge_jobs.forge(task, self.vault_dir)
        if result.ok and result.name:
            from council_core import tool_review
            try:
                tool_review.write_tests(result.name, task, self.vault_dir,
                                        forge_jobs.default_model_call)
                result.body = (result.body + "\n\n" + self.tests_text(
                    result.name)).strip()
            except Exception as exc:                      # noqa: BLE001
                result.body += f"\n\nTests could not be written: {exc}"
        return result

    def tests_text(self, name: str) -> str:
        from council_core import tool_review
        results = tool_review.run_tests(name, self.vault_dir)
        passed = sum(ok for ok, _ in results)
        lines = [f"TESTS for {name}: {passed}/{len(results)} pass"]
        lines += [("  ✓ " if ok else "  ✗ ") + what for ok, what in results]
        return "\n".join(lines)

    def run_tests(self, name: str):
        text = self.tests_text(name)
        return forge_jobs.ForgeResult(True, text.splitlines()[0], body=text)

    def approve(self, name: str, roles: List[str]):
        from council_core import tool_review
        ok, msg = tool_review.approve(name, roles, self.vault_dir)
        return forge_jobs.ForgeResult(ok, msg, body=msg)

    def revoke(self, name: str):
        from council_core import tool_review
        ok = tool_review.revoke(name, self.vault_dir)
        msg = (f"{name} is no longer approved; the council stops using it."
               if ok else f"{name} was not approved.")
        return forge_jobs.ForgeResult(ok, msg, body=msg)

    def status(self, name: str) -> str:
        from council_core import tool_review
        try:
            return tool_review.status(name, self.vault_dir)
        except Exception:                                 # noqa: BLE001
            return "unreviewed"

    def proposals(self) -> List[dict]:
        """Pending tool-gap proposals (tool_gap_analyzer), newest first."""
        try:
            import tool_gap_analyzer
            q = tool_gap_analyzer.ProposalQueue(
                self.vault_dir / tool_gap_analyzer.ProposalQueue
                .DEFAULT_FILENAME)
            items = [p for p in q.current_status()
                     if p.get("status") == "pending"]
        except Exception:                                 # noqa: BLE001
            return []
        return sorted(items, key=lambda p: -int(p.get("ts") or 0))

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

        # Review: tests, then the user's approval for named roles
        # (council_core.tool_review). Only an approved tool reaches the
        # council, and only for those roles.
        review = QHBoxLayout()
        self.tests_btn = self._button(review, "🧪 Run Tests", self.on_tests)
        review.addWidget(QLabel("Roles:"))
        self.roles = QLineEdit("intern")
        self.roles.setToolTip("Council roles that may call the tool, "
                              "comma-separated: intern, coder, skeptic, …")
        self.roles.setMaximumWidth(220)
        review.addWidget(self.roles)
        self.approve_btn = self._button(review, "✓ Approve for roles",
                                        self.on_approve)
        self.revoke_btn = self._button(review, "Revoke", self.on_revoke)
        review.addSpacing(16)
        review.addWidget(QLabel("From a request:"))
        self.proposal_box = QComboBox()
        self.proposal_box.setMinimumWidth(220)
        review.addWidget(self.proposal_box, 1)
        self._button(review, "Use", self.on_use_proposal)
        outer.addLayout(review)

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
        status = getattr(self.actions, "status", lambda n: "")
        for name, blurb in result.rows:
            mark = {"approved": "  [approved]",
                    "changed since approval": "  [changed — re-approve]"}.get(
                status(name), "")
            self.tools.addItem(f"{name}  —  {blurb}{mark}")
        self.proposal_box.clear()
        for p in getattr(self.actions, "proposals", lambda: [])()[:30]:
            self.proposal_box.addItem(
                f"{p.get('proposed_name')} (asked {p.get('observed_count', 1)}×)",
                p)
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
        for button in (self.generate_btn, self.save_btn, self.run_btn,
                       self.tests_btn, self.approve_btn, self.revoke_btn):
            button.setEnabled(enabled)

    def on_tests(self) -> None:
        name = self.selected_tool()
        if not name:
            self.status.setText("Select a tool in the list first.")
            return
        self._start(f"Testing '{name}'…", lambda: self.actions.run_tests(name))

    def on_approve(self) -> None:
        name = self.selected_tool()
        if not name:
            self.status.setText("Select a tool in the list first.")
            return
        roles = [r.strip().lower() for r in self.roles.text().split(",")
                 if r.strip()]
        self._start(f"Testing '{name}' before approving…",
                    lambda: self.actions.approve(name, roles),
                    then=lambda result: self.refresh_list())

    def on_revoke(self) -> None:
        name = self.selected_tool()
        if name:
            self._start("Revoking…", lambda: self.actions.revoke(name),
                        then=lambda result: self.refresh_list())

    def on_use_proposal(self) -> None:
        p = self.proposal_box.currentData()
        if not p:
            self.status.setText("No tool requests are waiting.")
            return
        from council_core import tool_review
        self.task.setPlainText(tool_review.task_from_proposal(p))
        self.status.setText("The request is in the task box — Generate to "
                            "build it.")

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
