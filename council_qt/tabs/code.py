"""
council_qt.tabs.code — the council develops a project: pick the project and
an agent, describe the change, approve the plan, watch it work, then merge
or discard its branch.

    Project    a git folder with its test command, GUI checks and reference
               documents (council_core.project). Brief… shows what every
               job starts from and any addition the council proposed — you
               accept it or not.
    Agent      a profile (council_core.agent_profiles): its instructions,
               tools, model and output (a patch, or a report that changes
               nothing).
    Plan       the planner's steps, editable: untick a step, change its
               check, rewrite its details. Nothing runs before you press
               Run.
    Run        the job works on its own branch in a worktree
               (council_core.code_agent): each step checked by the app and
               committed. The log shows every tool call.
    Diff / Merge / Discard   the job's whole change; merge it into the
               branch your folder has checked out (refused while you have
               uncommitted work), or throw the branch away.

Planning and running are on worker threads; Stop ends the job after the
current model call.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QHeaderView, QLabel,
                               QPlainTextEdit, QTableWidget, QTableWidgetItem,
                               QTabWidget, QVBoxLayout, QWidget)

from council_core import agent_profiles as ap
from council_core import code_agent as ca
from council_core import paths
from council_core import project as pj
from council_core import worktree as wt

from .. import theme
from ..view import ViewHelpers
from ..widgets.code_dialogs import BriefDialog, ProfileDialog, ProjectDialog

PLAN_COLUMNS = ("Use", "Step", "Check", "Test first", "Files", "Details")


class CodeActions:
    """What the tab asks of the application. Tests pass their own models."""

    def __init__(self, vault_dir: Optional[Path] = None,
                 chat: Optional[Callable] = None,
                 chat_tools: Optional[Callable] = None):
        self.vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()
        self.chat = chat
        self.chat_tools = chat_tools

    def projects(self) -> List[pj.Project]:
        return pj.list_projects(self.vault_dir)

    def save_project(self, project: pj.Project) -> None:
        pj.save(self.vault_dir, project)

    def profiles(self) -> List[ap.AgentProfile]:
        return ap.list_profiles(self.vault_dir)

    def save_profile(self, profile: ap.AgentProfile) -> ap.AgentProfile:
        return ap.save(self.vault_dir, profile)

    def plan(self, project: pj.Project, profile: ap.AgentProfile,
             task: str) -> ca.Plan:
        wt.require_repo(Path(project.root))
        proj = ap.with_references(project, profile)
        framed = (f"{task}\n\n(The agent doing this: {profile.name}. "
                  f"{profile.instructions})")
        return ca.make_plan(self.vault_dir, proj, framed, chat=self.chat)

    def run(self, project: pj.Project, profile: ap.AgentProfile, task: str,
            plan: ca.Plan, on_event, should_stop) -> ca.JobRecord:
        proj = ap.with_references(project, profile)
        rec = ca.new_job(self.vault_dir, proj, task, plan)
        return ca.run_job(self.vault_dir, proj, rec, chat=self.chat,
                          chat_tools=self.chat_tools, role=profile.role,
                          on_event=on_event, should_stop=should_stop,
                          max_turns=profile.max_turns,
                          tool_names=profile.tools,
                          extra_system=profile.instructions)

    def report(self, project: pj.Project, profile: ap.AgentProfile,
               task: str, on_event, should_stop) -> str:
        return ap.run_report(self.vault_dir, project, profile, task,
                             chat_tools=self.chat_tools, on_event=on_event,
                             should_stop=should_stop)

    def bench(self, project: pj.Project, count: int, say, should_stop):
        from council_core import code_bench
        return code_bench.run(self.vault_dir, project, label="history",
                              count=count, chat=self.chat,
                              chat_tools=self.chat_tools, say=say,
                              should_stop=should_stop)

    def jobs(self, project: pj.Project) -> List[ca.JobRecord]:
        return [r for r in ca.list_records(self.vault_dir, project)
                if r.branch]

    def job(self, project: pj.Project, rec: ca.JobRecord) -> wt.Job:
        return wt.open_job(project.root,
                           pj.project_dir(self.vault_dir, project)
                           / "worktrees", rec.id, rec.base)

    def set_status(self, project: pj.Project, rec: ca.JobRecord,
                   status: str) -> None:
        rec.status = status
        ca.save_record(self.vault_dir, project, rec)


# ============================================================
# The tab
# ============================================================

class CodeTab(ViewHelpers, QWidget):
    def __init__(self, window=None, actions: Optional[CodeActions] = None):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or CodeActions()
        self._tokens = theme.tokens("dark")
        self._busy = False
        self._stop = threading.Event()
        self._plan: Optional[ca.Plan] = None
        self._projects: List[pj.Project] = []
        self._profiles: List[ap.AgentProfile] = []
        self._jobs: List[ca.JobRecord] = []
        self._build()
        self.refresh()

    # -- layout -----------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        intro = QLabel(
            "The council works on a project on its own git branch: it "
            "plans, you approve, it changes the code in small checked steps "
            "and commits each one; then you merge or discard the branch. "
            "Your folder is not touched until you merge.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        outer.addWidget(intro)

        row = QHBoxLayout()
        row.addWidget(QLabel("Project:"))
        self.project_box = QComboBox()
        self.project_box.setMinimumWidth(220)
        self.project_box.currentIndexChanged.connect(self._project_changed)
        row.addWidget(self.project_box)
        self._button(row, "New…", self.on_new_project)
        self._button(row, "Edit…", self.on_edit_project)
        self._button(row, "Brief…", self.on_brief)
        row.addSpacing(16)
        row.addWidget(QLabel("Agent:"))
        self.profile_box = QComboBox()
        self.profile_box.setMinimumWidth(200)
        row.addWidget(self.profile_box)
        self._button(row, "Agents…", self.on_new_profile)
        row.addStretch(1)
        outer.addLayout(row)

        self.task = QPlainTextEdit()
        self.task.setMaximumHeight(80)
        self.task.setPlaceholderText(
            "What should change? e.g. “Add a Reset button to the camera "
            "settings window that puts exposure and gain back to the "
            "preset's values”")
        outer.addWidget(self.task)

        buttons = QHBoxLayout()
        self.plan_btn = self._button(buttons, "Plan", self.on_plan)
        self.run_btn = self._button(buttons, "Run approved plan", self.on_run)
        self.stop_btn = self._button(buttons, "Stop", self.on_stop)
        buttons.addSpacing(16)
        buttons.addWidget(QLabel("Job:"))
        self.job_box = QComboBox()
        self.job_box.setMinimumWidth(260)
        buttons.addWidget(self.job_box)
        self.diff_btn = self._button(buttons, "Diff", self.on_diff)
        self.merge_btn = self._button(buttons, "Merge", self.on_merge)
        self.discard_btn = self._button(buttons, "Discard", self.on_discard)
        buttons.addStretch(1)
        self.bench_btn = self._button(buttons, "Benchmark on history",
                                      self.on_bench)
        self.bench_btn.setToolTip(
            "Takes recent commits that changed code and its tests, undoes "
            "the code, and measures whether the council can redo it (the "
            "commit's tests pass). Your folder is not touched.")
        outer.addLayout(buttons)
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)

        self.plan_table = QTableWidget(0, len(PLAN_COLUMNS))
        self.plan_table.setHorizontalHeaderLabels(PLAN_COLUMNS)
        self.plan_table.horizontalHeader().setSectionResizeMode(
            len(PLAN_COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        self.plan_table.setMaximumHeight(200)
        outer.addWidget(self.plan_table)

        self.views = QTabWidget()
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.diff = QPlainTextEdit()
        self.diff.setReadOnly(True)
        self.diff.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.views.addTab(self.log, "Log")
        self.views.addTab(self.diff, "Diff")
        self.views.addTab(self.notes, "Notes / report")
        outer.addWidget(self.views, 1)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)

    # -- state ------------------------------------------------------------
    def refresh(self) -> None:
        current = self.project_box.currentText()
        self._projects = self.actions.projects()
        self.project_box.blockSignals(True)
        self.project_box.clear()
        for p in self._projects:
            self.project_box.addItem(p.name)
        if current:
            self.project_box.setCurrentText(current)
        self.project_box.blockSignals(False)
        self._profiles = self.actions.profiles()
        keep = self.profile_box.currentText()
        self.profile_box.clear()
        for p in self._profiles:
            self.profile_box.addItem(
                p.name + (" — report" if p.output == "report" else ""))
        if keep:
            self.profile_box.setCurrentText(keep)
        self._project_changed()

    def project(self) -> Optional[pj.Project]:
        i = self.project_box.currentIndex()
        return self._projects[i] if 0 <= i < len(self._projects) else None

    def profile(self) -> Optional[ap.AgentProfile]:
        i = self.profile_box.currentIndex()
        return self._profiles[i] if 0 <= i < len(self._profiles) else None

    def _project_changed(self, *_a) -> None:
        p = self.project()
        self._jobs = self.actions.jobs(p) if p else []
        self.job_box.clear()
        for r in reversed(self._jobs):
            self.job_box.addItem(f"{r.status} — {r.task.splitlines()[0][:50]} "
                                 f"({r.id[:15]})", r.id)
        if p is not None:
            note = ca.latest_note(self.actions.vault_dir, p)
            self.notes.setPlainText(note or "(no notes yet)")

    def current_job(self) -> Optional[ca.JobRecord]:
        jid = self.job_box.currentData()
        return next((r for r in self._jobs if r.id == jid), None)

    def say(self, kind: str, text: str) -> None:
        prefix = {"phase": "▶ ", "tool": "  · ", "result": "      ",
                  "check": "  ✔ ", "note": ""}.get(kind, "")
        if kind == "result":
            text = text[:600]
        if kind == "note":
            self.notes.setPlainText(text)
            return
        self.log.appendPlainText(prefix + text.replace(
            "\n", "\n" + " " * len(prefix)))

    def _busy_on(self, on: bool, status: str = "") -> None:
        self._busy = on
        for b in (self.plan_btn, self.diff_btn, self.merge_btn,
                  self.discard_btn, self.bench_btn):
            b.setEnabled(not on)
        self.run_btn.setEnabled(not on and self._plan is not None)
        self.stop_btn.setEnabled(on)
        if status:
            self.status.setText(status)

    def _work(self, name: str, fn: Callable[[], Any],
              done: Callable[[Any], None]) -> None:
        def work():
            try:
                result = fn()
                self._to_ui(lambda: done(result))
            except Exception as exc:                      # noqa: BLE001
                msg = f"{type(exc).__name__}: {exc}"
                self._to_ui(lambda: self.status.setText(msg))
            finally:
                self._to_ui(lambda: self._busy_on(False))
        threading.Thread(target=work, name=name, daemon=True).start()

    # -- plan -------------------------------------------------------------
    def show_plan(self, plan: ca.Plan) -> None:
        self._plan = plan
        t = self.plan_table
        t.setRowCount(len(plan.steps))
        for i, s in enumerate(plan.steps):
            use = QTableWidgetItem()
            use.setFlags(use.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            use.setCheckState(Qt.CheckState.Checked)
            t.setItem(i, 0, use)
            t.setItem(i, 1, QTableWidgetItem(s.title))
            check = QComboBox()
            check.addItems(list(ca.CHECKS))
            check.setCurrentText(s.check)
            t.setCellWidget(i, 2, check)
            tf = QTableWidgetItem()
            tf.setFlags(tf.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            tf.setCheckState(Qt.CheckState.Checked if s.test_first
                             else Qt.CheckState.Unchecked)
            t.setItem(i, 3, tf)
            t.setItem(i, 4, QTableWidgetItem(", ".join(s.files)))
            t.setItem(i, 5, QTableWidgetItem(s.details))
        self.run_btn.setEnabled(True)
        self.notes.setPlainText(plan.text())

    def plan_from_table(self) -> Optional[ca.Plan]:
        if self._plan is None:
            return None
        t = self.plan_table
        steps = []
        for i in range(t.rowCount()):
            if t.item(i, 0).checkState() != Qt.CheckState.Checked:
                continue
            steps.append(ca.Step(
                t.item(i, 1).text().strip(),
                [f.strip() for f in t.item(i, 4).text().split(",")
                 if f.strip()],
                t.cellWidget(i, 2).currentText(),
                t.item(i, 3).checkState() == Qt.CheckState.Checked,
                t.item(i, 5).text().strip()))
        return ca.Plan(self._plan.goal, steps, self._plan.risks) \
            if steps else None

    # -- actions ----------------------------------------------------------
    def on_new_project(self) -> None:
        self._project_dialog(None)

    def on_edit_project(self) -> None:
        if self.project() is not None:
            self._project_dialog(self.project())

    def _project_dialog(self, project: Optional[pj.Project]) -> None:
        dlg = ProjectDialog(self, project)

        def done(code):
            if code:
                p = dlg.project()
                self.actions.save_project(p)
                self.refresh()
                self.project_box.setCurrentText(p.name)
        dlg.finished.connect(done)
        dlg.open()
        self._dialog = dlg

    def on_brief(self) -> None:
        p = self.project()
        if p is None:
            self.status.setText("Make a project first (New…).")
            return
        dlg = BriefDialog(self, self.actions.vault_dir, p)
        dlg.open()
        self._dialog = dlg

    def on_new_profile(self) -> None:
        dlg = ProfileDialog(self, self.profile())

        def done(code):
            if code:
                saved = self.actions.save_profile(dlg.profile())
                self.refresh()
                self.profile_box.setCurrentText(saved.name + (
                    " — report" if saved.output == "report" else ""))
        dlg.finished.connect(done)
        dlg.open()
        self._dialog = dlg

    def on_plan(self) -> None:
        p, prof = self.project(), self.profile()
        task = self.task.toPlainText().strip()
        if p is None or prof is None or not task:
            self.status.setText("Pick a project and an agent, and describe "
                                "the change.")
            return
        if prof.output == "report":
            self._start_report(p, prof, task)
            return
        self._busy_on(True, "The planner is reading the project…")
        self._work("code-plan", lambda: self.actions.plan(p, prof, task),
                   lambda plan: (self.show_plan(plan), self.status.setText(
                       "Check the plan: untick steps, edit them, then Run.")))

    def _start_report(self, p, prof, task) -> None:
        self._stop.clear()
        self.log.clear()
        self._busy_on(True, f"{prof.name} is reviewing…")
        self._work("code-report", lambda: self.actions.report(
            p, prof, task, on_event=lambda k, t: self._to_ui(
                lambda: self.say(k, t)), should_stop=self._stop.is_set),
            lambda report: (self.notes.setPlainText(report),
                            self.views.setCurrentWidget(self.notes),
                            self.status.setText("Report ready.")))

    def on_run(self) -> None:
        p, prof = self.project(), self.profile()
        plan = self.plan_from_table()
        task = self.task.toPlainText().strip()
        if p is None or prof is None or plan is None:
            self.status.setText("Plan first, and keep at least one step.")
            return
        self._stop.clear()
        self.log.clear()
        self.views.setCurrentWidget(self.log)
        self._busy_on(True, "Working — each step is checked and committed.")

        def done(rec: ca.JobRecord) -> None:
            self._plan = None
            self._project_changed()
            self.job_box.setCurrentIndex(0)
            steps = ca.plan_of(rec).steps
            self.status.setText(
                f"Job {rec.status}: {sum(s.status == 'done' for s in steps)}"
                f"/{len(steps)} step(s) committed on {rec.branch}. Review the "
                "Diff, then Merge or Discard.")
            if pj.proposed_brief(self.actions.vault_dir, p):
                self.status.setText(self.status.text() + " The council "
                                    "proposed an addition to the brief "
                                    "(Brief…).")
        self._work("code-run", lambda: self.actions.run(
            p, prof, task, plan,
            on_event=lambda k, t: self._to_ui(lambda: self.say(k, t)),
            should_stop=self._stop.is_set), done)

    def on_bench(self) -> None:
        p = self.project()
        if p is None:
            self.status.setText("Pick a project first.")
            return
        self._stop.clear()
        self.log.clear()
        self.views.setCurrentWidget(self.log)
        self._busy_on(True, "Benchmarking on 5 past commits — this runs the "
                            "council 5 times.")
        self._work("code-bench", lambda: self.actions.bench(
            p, 5, lambda line: self._to_ui(lambda: self.say("phase", line)),
            self._stop.is_set),
            lambda s: self.status.setText(
                f"Solved {s['solved']} of {s['valid']} usable task(s) "
                f"({s['tasks']} tried); {s['mean_calls']} calls and "
                f"{s['mean_seconds']} s per task. Saved as {s['file']}."))

    def on_stop(self) -> None:
        self._stop.set()
        self.status.setText("Stopping after the current model call…")

    def _with_job(self, fn: Callable[[pj.Project, ca.JobRecord], None]) -> None:
        p, rec = self.project(), self.current_job()
        if p is None or rec is None:
            self.status.setText("Pick a job first.")
            return
        try:
            fn(p, rec)
        except wt.WorktreeError as exc:
            self.status.setText(str(exc))

    def on_diff(self) -> None:
        def show(p, rec):
            job = self.actions.job(p, rec)
            self.diff.setPlainText(job.diff() or "(no committed changes)")
            self.views.setCurrentWidget(self.diff)
            self.status.setText(" · ".join(job.log()) or "No commits.")
        self._with_job(show)

    def on_merge(self) -> None:
        def merge(p, rec):
            job = self.actions.job(p, rec)
            line = wt.merge(job, message=f"Council: {rec.task.splitlines()[0]}")
            wt.discard(job)
            self.actions.set_status(p, rec, "merged")
            self._project_changed()
            self.status.setText(f"Merged into your folder ({line}).")
        self._with_job(merge)

    def on_discard(self) -> None:
        def discard(p, rec):
            wt.discard(self.actions.job(p, rec))
            self.actions.set_status(p, rec, "discarded")
            self._project_changed()
            self.status.setText("The job's branch and worktree are gone.")
        self._with_job(discard)


def build_code(window) -> QWidget:
    """Factory for the tab registry."""
    return CodeTab(window)
