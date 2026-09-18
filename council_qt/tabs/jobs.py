"""
council_qt.tabs.jobs — Agent Jobs, ported.

Give the agent a goal and a step budget; a single-worker runner executes a
bounded, read-only loop and writes a Markdown report. Three message kinds
arrive while it runs, and this is the only phase-7 tab fed by a live background
thread.

THE DEFECT THAT MAKES THE TK TAB UNUSABLE AFTER ONE JOB
`_aj_start` clears the goal box with `self._set_text(self._aj_goal, "")`, and
`_set_text` re-DISABLES any widget not in a hardcoded tuple of three names.
`_aj_goal` is not one of them, so the goal box is permanently read-only after
the first job and the only way back is restarting the app.

Clearing an input here is `self.goal.clear()`, which cannot disable anything.
The bug is not so much fixed as made unavailable.
"""
from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from typing import List, Optional

from PySide6.QtWidgets import (QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QPlainTextEdit, QSpinBox, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from council_core import jobs as jobs_core

from .. import theme
from ..view import ViewHelpers


class JobsActions:
    """What the Agent Jobs tab can ask the application to do."""

    def __init__(self, runner=None, vault_dir: Optional[Path] = None):
        self.vault_dir = Path(vault_dir or Path.home() / "council_vault")
        self._runner = runner

    def runner(self):
        """The job runner, built on first use.

        Lazily because constructing it opens the job store, and a tab the user
        never starts a job from should not pay for that.
        """
        if self._runner is None:
            import agent_jobs_runner
            self._runner = agent_jobs_runner.JobRunner(vault_dir=self.vault_dir)
        return self._runner

    def start(self, goal: str, steps):
        return jobs_core.start(self.runner(), goal, steps)

    def cancel(self, job_id):
        return jobs_core.cancel(self.runner(), job_id)

    def listing(self):
        return jobs_core.listing(self.runner())

    def report_path(self, job_id):
        return jobs_core.report_path(self.runner(), job_id)


class JobsTab(ViewHelpers, QWidget):
    """A goal box, a queue, and a live step log."""

    def __init__(self, window=None, actions: Optional[JobsActions] = None):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or JobsActions()
        self._tokens = theme.tokens("dark")
        self._ids: List[str] = []
        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)

        title = QLabel("Give the agent a goal — it plans and runs the steps "
                       "itself.")
        title.setStyleSheet("font-weight: bold;")
        outer.addWidget(title)

        safety = QLabel(jobs_core.SAFETY)
        safety.setWordWrap(True)
        safety.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        outer.addWidget(safety)

        self.goal = QPlainTextEdit()
        self.goal.setMaximumHeight(70)
        self.goal.setPlaceholderText(
            "e.g. summarise every CSV in the vault and say which look stale")
        outer.addWidget(self.goal)

        row = QHBoxLayout()
        row.addWidget(QLabel("Max steps:"))
        self.steps = QSpinBox()
        self.steps.setRange(jobs_core.MIN_STEPS, jobs_core.MAX_STEPS)
        self.steps.setValue(jobs_core.DEFAULT_STEPS)
        row.addWidget(self.steps)
        self._button(row, "▶ Start job", self.on_start)
        self._button(row, "⟳ Refresh", self.refresh)
        self._button(row, "■ Cancel", self.on_cancel)
        self._button(row, "🔎 Open report", self.on_open_report)
        self._button(row, "🗑 Remove finished", self.on_remove_finished)
        row.addStretch(1)
        self.status = QLabel("")
        self.status.setStyleSheet(f"color: {self._tokens['success']};")
        row.addWidget(self.status)
        outer.addLayout(row)

        self.queue = QTreeWidget()
        self.queue.setColumnCount(3)
        self.queue.setHeaderLabels(["Status", "Goal", "Steps"])
        self.queue.setRootIsDecorated(False)
        self.queue.header().setSectionResizeMode(1, QHeaderView.Stretch)
        outer.addWidget(self.queue, 1)

        log_box = QGroupBox("Step log")
        log_layout = QVBoxLayout(log_box)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        log_layout.addWidget(self.log)
        outer.addWidget(log_box, 1)

    # ------------------------------------------------------------------
    def selected_job(self) -> Optional[str]:
        """The id of the selected row.

        Read from a parallel list, not recovered from the row's text. Three
        places in this app recover an identifier by splitting a display label
        and all three are defects.
        """
        index = self.queue.indexOfTopLevelItem(self.queue.currentItem())
        return self._ids[index] if 0 <= index < len(self._ids) else None

    def refresh(self) -> None:
        def work() -> None:
            result = self.actions.listing()

            def show() -> None:
                if not result.ok:
                    self.status.setText(result.message)
                    return
                self.queue.clear()
                self._ids = list(result.ids)
                for row in result.rows:
                    QTreeWidgetItem(self.queue, list(row))

            self._to_ui(show)

        threading.Thread(target=work, name="jobs-refresh", daemon=True).start()

    def on_start(self) -> None:
        goal = self.goal.toPlainText()
        problem = jobs_core.check_goal(goal)
        if problem:
            self.status.setText(problem)
            return
        steps = self.steps.value()        # on the GUI thread, before the work

        def work() -> None:
            result = self.actions.start(goal, steps)

            def show() -> None:
                self.status.setText(result.message)
                if result.ok:
                    # `clear()` cannot disable the widget. The Tk version's
                    # clear leaves the goal box permanently read-only.
                    self.goal.clear()
                    self.append_log(f"▶ {result.job_id}: {goal.strip()}")
                    self.refresh()

            self._to_ui(show)

        threading.Thread(target=work, name="jobs-start", daemon=True).start()

    def on_cancel(self) -> None:
        job_id = self.selected_job()
        result = self.actions.cancel(job_id)
        self.status.setText(result.message)
        if result.ok:
            self.refresh()

    def on_open_report(self) -> None:
        path = self.actions.report_path(self.selected_job())
        if path is None or not Path(path).exists():
            self.status.setText("That job has no report yet.")
            return
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", str(path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:                          # noqa: BLE001
            self.status.setText(f"Could not open it: {exc}")

    def on_remove_finished(self) -> None:
        self.status.setText("Removing finished jobs is not ported yet.")

    # -- the live feed --------------------------------------------------
    def append_log(self, line: str) -> None:
        self.log.appendPlainText(line)

    def on_job_step(self, job_id: str, number, text: str) -> None:
        """One step from the running job. Always on the GUI thread."""
        self.append_log(jobs_core.step_line(job_id, number, text))

    def on_job_status(self, job_id: str, text: str) -> None:
        self.status.setText(text)
        self.refresh()

    def on_job_done(self, job_id: str, text: str) -> None:
        self.append_log(f"■ {job_id}: {text}")
        self.refresh()


def build_jobs(window) -> QWidget:
    """Factory for the tab registry."""
    return JobsTab(window)
