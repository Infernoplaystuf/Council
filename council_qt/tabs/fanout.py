"""
council_qt.tabs.fanout — several coding agents on one task, side by side.

Pick a code folder, describe the change, press Plan: the planner splits it
into units that never share a file (council_core.fanout). Untick any unit you
do not want, press Run: each unit's worker codes in its own copy — on this PC
or on the machines set up in Machines & roles — then everything is combined,
tested, reviewed by the Judge, and written as a patch. Your folder is never
written: apply the patch yourself (`git apply`).

A test command is optional. When given, it runs the workers' code on this PC
(in the copies, without a shell, with a time limit); left empty, nothing runs.

Planning and running are on worker threads; progress comes back through
_to_ui. Stop is cooperative: workers stop at their next step.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QPlainTextEdit, QSplitter,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from council_core import fanout as fo
from council_core import paths

from .. import theme
from ..view import ViewHelpers

COLUMNS = ("Use", "Unit", "Title", "Files", "Machine", "Status", "Tries")
STATUS_TOKEN = {"done": "success", "failed": "error", "error": "error",
                "working": "warning", "checking": "warning",
                "waiting": "muted_fg"}


class FanoutActions:
    """What the tab asks of the application. Tests pass their own."""

    def __init__(self, vault_dir: Optional[Path] = None,
                 chat: Optional[Callable] = None):
        self.vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()
        self.chat = chat

    def plan(self, task: str, repo: Path) -> fo.Plan:
        return fo.make_plan(task, repo, chat=self.chat)

    def run(self, task: str, repo: Path, plan: fo.Plan, test_command: str,
            should_stop: Callable[[], bool],
            on_update: Callable[[fo.UnitResult], None]) -> fo.JobResult:
        return fo.run_job(task, repo, plan, self.vault_dir,
                          test_command=test_command, chat=self.chat,
                          should_stop=should_stop, on_update=on_update)

    def machines(self) -> List[str]:
        return [t.name for t in fo.worker_targets()]


class FanoutTab(ViewHelpers, QWidget):
    def __init__(self, window=None, actions: Optional[FanoutActions] = None):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or FanoutActions()
        self._tokens = theme.tokens("dark")
        self.plan: Optional[fo.Plan] = None
        self.result: Optional[fo.JobResult] = None
        self._busy = False
        self._stop = threading.Event()
        self._rows: Dict[str, int] = {}
        self._build()
        self._set_busy(False)

    # ------------------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        intro = QLabel(
            "Split a coding task across several agents working at the same "
            "time, each on its own files and its own copy of the code — on "
            "this PC and on the machines set up in Council Map ▸ Machines & "
            "roles. Your folder is never changed: you get a patch to apply.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        outer.addWidget(intro)

        row = QHBoxLayout()
        row.addWidget(QLabel("Code folder:"))
        self.folder = QLineEdit()
        self.folder.setPlaceholderText("the project to change")
        row.addWidget(self.folder, 1)
        self._button(row, "Browse…", self.browse)
        outer.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel("Test command:"))
        self.test_cmd = QLineEdit()
        self.test_cmd.setPlaceholderText("python -m pytest -q   (optional)")
        row.addWidget(self.test_cmd, 1)
        outer.addLayout(row)
        warn = QLabel("A test command runs the workers' code on this PC, in "
                      "copies of your folder. Leave it empty to run nothing.")
        warn.setWordWrap(True)
        warn.setStyleSheet(f"color: {self._tokens['warning']};")
        outer.addWidget(warn)

        outer.addWidget(QLabel("The task:"))
        self.task = QPlainTextEdit()
        self.task.setPlaceholderText(
            "e.g. Add a --json option to every report command, and tests "
            "for it.")
        self.task.setMaximumHeight(90)
        outer.addWidget(self.task)

        row = QHBoxLayout()
        self.plan_btn = self._button(row, "1. Plan the split", self.on_plan)
        self.run_btn = self._button(row, "2. Run the ticked units",
                                    self.on_run)
        self.stop_btn = self._button(row, "Stop", self.on_stop)
        self.copy_btn = self._button(row, "Copy patch path", self.copy_patch)
        row.addStretch(1)
        self.machines = QLabel("")
        self.machines.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        row.addWidget(self.machines)
        outer.addLayout(row)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch)
        split.addWidget(self.table)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        split.addWidget(self.details)
        split.setSizes([700, 500])
        outer.addWidget(split, 1)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        self._show_machines()

    def _show_machines(self) -> None:
        try:
            names = self.actions.machines()
        except Exception:                                 # noqa: BLE001
            names = ["This PC"]
        self.machines.setText("Workers run on: " + ", ".join(names))

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.plan_btn.setEnabled(not busy)
        self.run_btn.setEnabled(not busy and bool(self.plan
                                                  and self.plan.units))
        self.stop_btn.setEnabled(busy)
        self.copy_btn.setEnabled(bool(self.result and self.result.patch))

    def browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Code folder",
                                                  self.folder.text())
        if chosen:
            self.folder.setText(chosen)

    # ------------------------------------------------------------------
    def _inputs(self):
        task = self.task.toPlainText().strip()
        folder = Path(self.folder.text().strip()).expanduser()
        if not task:
            return None, None, "Describe the task first."
        if not self.folder.text().strip() or not folder.is_dir():
            return None, None, "Pick a code folder that exists."
        return task, folder, ""

    def on_plan(self) -> None:
        task, folder, problem = self._inputs()
        if problem:
            self.status.setText(problem)
            return
        self._set_busy(True)
        self.status.setText("The planner is splitting the task…")

        def work() -> None:
            try:
                plan = self.actions.plan(task, folder)
                self._to_ui(lambda: self._show_plan(plan))
            except Exception as exc:                      # noqa: BLE001
                message = f"Planning failed: {exc}"
                self._to_ui(lambda: self.status.setText(message))
            finally:
                self._to_ui(lambda: self._set_busy(False))

        threading.Thread(target=work, name="fanout-plan", daemon=True).start()

    def _show_plan(self, plan: fo.Plan) -> None:
        self.plan, self.result = plan, None
        self._rows = {}
        self.table.setRowCount(len(plan.units))
        for r, unit in enumerate(plan.units):
            self._rows[unit.id] = r
            use = QTableWidgetItem("")
            use.setFlags(use.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            use.setCheckState(Qt.CheckState.Checked)
            self.table.setItem(r, 0, use)
            for c, text in enumerate((unit.id, unit.title,
                                      ", ".join(unit.owned), "", "planned",
                                      ""), start=1):
                self.table.setItem(r, c, QTableWidgetItem(text))
        self.table.resizeColumnsToContents()
        L = [f"PLAN — {plan.summary}", "", "CONTRACT (what the units agree "
             "on):", plan.contract or "(none)", ""]
        for unit in plan.units:
            L += [f"{unit.id} — {unit.title}", f"  files: "
                  f"{', '.join(unit.owned)}", f"  {unit.instructions}", ""]
        if plan.rejected:
            L.append("REJECTED (not run):")
            L += [f"  {u.id} {u.title}: {u.why_not}" for u in plan.rejected]
        self.details.setPlainText("\n".join(L))
        self.status.setText(
            f"{len(plan.units)} unit(s) can run at once. Untick any you do "
            "not want, then Run." if plan.units else
            "The planner produced no unit that can run; see the details.")
        self._set_busy(False)

    def approved_plan(self) -> Optional[fo.Plan]:
        if not self.plan:
            return None
        keep = [u for u in self.plan.units
                if self.table.item(self._rows[u.id], 0).checkState()
                == Qt.CheckState.Checked]
        return fo.Plan(self.plan.summary, self.plan.contract, keep)

    def on_run(self) -> None:
        task, folder, problem = self._inputs()
        plan = self.approved_plan()
        if problem or not plan or not plan.units:
            self.status.setText(problem or "Plan first, and tick at least "
                                "one unit.")
            return
        self._stop.clear()
        self._set_busy(True)
        self.status.setText(f"{len(plan.units)} worker(s) running…")
        test_cmd = self.test_cmd.text().strip()

        def update(r: fo.UnitResult) -> None:
            snapshot = fo.UnitResult(**{**r.__dict__})
            self._to_ui(lambda: self._show_unit(snapshot))

        def work() -> None:
            try:
                result = self.actions.run(task, folder, plan, test_cmd,
                                          self._stop.is_set, update)
                self._to_ui(lambda: self._show_result(result))
            except Exception as exc:                      # noqa: BLE001
                message = f"The job failed: {exc}"
                self._to_ui(lambda: self.status.setText(message))
            finally:
                self._to_ui(lambda: self._set_busy(False))

        threading.Thread(target=work, name="fanout-run", daemon=True).start()

    def on_stop(self) -> None:
        self._stop.set()
        self.status.setText("Stopping — workers stop at their next step…")

    def _show_unit(self, r: fo.UnitResult) -> None:
        row = self._rows.get(r.unit)
        if row is None:
            return
        for col, text in ((4, r.machine), (5, r.status),
                          (6, str(r.attempts or ""))):
            item = QTableWidgetItem(text)
            if col == 5:
                token = STATUS_TOKEN.get(r.status)
                if token:
                    item.setForeground(QColor(self._tokens[token]))
            self.table.setItem(row, col, item)

    def _show_result(self, result: fo.JobResult) -> None:
        self.result = result
        for r in result.units:
            self._show_unit(r)
        tail = ""
        if result.test_output and result.tests_passed is False:
            tail = "\n\nTEST OUTPUT (last part):\n" + result.test_output
        self.details.setPlainText(result.text() + tail)
        verdict = (result.review or {}).get("verdict", "")
        self.status.setText(
            "Done — " + ("tests passed" if result.tests_passed else
                         "tests FAILED" if result.tests_passed is False else
                         "no tests run")
            + (f", Judge: {verdict}" if verdict else "")
            + (". The patch is ready." if result.patch else "."))
        self._set_busy(False)

    def copy_patch(self) -> None:
        if self.result and self.result.patch:
            QGuiApplication.clipboard().setText(self.result.patch)
            self.status.setText("Patch path copied. In your folder: "
                                f"git apply \"{self.result.patch}\"")


def build_fanout(window) -> QWidget:
    """Factory for the tab registry."""
    return FanoutTab(window)
