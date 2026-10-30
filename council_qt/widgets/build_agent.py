"""
council_qt.widgets.build_agent — the Build chat's Agent mode and its model.

AGENT MODE (council_core.build_agent; the Code tab's pipeline, in the chat)
  1. The request goes to the planner with the folder's project and the
     chosen agent. Its plan comes back as a card under the reply: each step
     with its check and files, and a link to leave it out or put it back.
     Writing again while the plan is open asks for a new plan with that
     change. A report-only agent answers with its report and changes nothing.
  2. Go runs the kept steps on the job's own branch, in a worktree in the
     vault: each step checked and committed. What it does arrives as the
     reply's step list while it works; Stop ends it after the current call.
  3. The reply then says how many steps were committed, with "Review and
     merge" (the branch's diff in the review window; Merge is refused over
     uncommitted work, and asks for unsaved editor tabs to be saved first)
     and "Discard".
  The folder must be a git folder, and Agent mode waits while an Edit-mode
  session is open (both work on a branch of their own).

THE MODEL (council_core.build_models)
  The "Model:" box beside the mode is the coder role's model, which Ask,
  Edit and Agent all use: the installed US models ranked for code on this
  PC's GPU memory, the best marked ★. When nothing installed fits the card,
  the status line names catalog models that would, with the command that
  fetches one; the Council does not download it. The list is read on a
  worker the first time the chat is shown.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import Qt

from council_core import build_agent as ba
from council_core import build_chat as bc
from council_core import build_models as bm
from council_core import build_sandbox as sb
from council_core import worktree as wt

if TYPE_CHECKING:                                         # pragma: no cover
    from .build_chat import ChatPanel

AGENT = "agent"
GO_TEXT = "Go ahead with the plan."


class AgentMode:
    """Agent mode for one ChatPanel (its conversation, its busy state)."""

    def __init__(self, panel: "ChatPanel", agent: Optional[ba.Agent]):
        self.panel = panel
        self.agent = agent
        self._profiles = []
        self._stop: Optional[threading.Event] = None
        self._job_turn: Optional[bc.Turn] = None

    # -- the agents ---------------------------------------------------------
    def fill_profiles(self) -> None:
        box = self.panel.profile_box
        keep = box.currentText()
        try:
            self._profiles = self.agent.profiles() if self.agent else []
        except OSError:
            self._profiles = []
        box.blockSignals(True)
        box.clear()
        for p in self._profiles:
            box.addItem(p.name + (" (report)" if p.output == "report" else ""))
        if keep:
            box.setCurrentText(keep)
        box.blockSignals(False)

    def profile(self):
        i = self.panel.profile_box.currentIndex()
        return self._profiles[i] if 0 <= i < len(self._profiles) else None

    def project(self):
        host = self.panel.host
        if host is None or self.agent is None:
            return None
        folder = Path(host.agent_folder())
        return self.agent.project_for(folder, host.agent_python(),
                                      sb.has_tests(folder))

    # -- what a turn shows ----------------------------------------------------
    @staticmethod
    def card(t: bc.Turn) -> Optional[dict]:
        return next((s for s in t.steps if s.get("kind") == "agent-plan"), None)

    @staticmethod
    def job(t: bc.Turn) -> Optional[dict]:
        return next((s for s in t.steps if s.get("kind") == "agent-job"), None)

    def plan_is_open(self) -> bool:
        turns = self.panel.turns()
        return bool(turns) and turns[-1].kind == "agent-plan" and not self.panel.is_busy()

    def markdown(self, i: int, t: bc.Turn) -> str:
        card = self.card(t)
        # a plan's content is its text, for the model's history; the card shows it
        out = [t.content] if t.content and card is None else []
        if card is not None:
            open_ = i == len(self.panel.turns()) - 1 and self.plan_is_open()
            if card.get("goal"):
                out.append(f"**Plan:** {card['goal']}")
            for n, s in enumerate(card.get("steps", [])):
                use = s.get("use", True)
                files = ", ".join(f"`{f}`" for f in s.get("files", [])) or "files to find"
                tf = ", test first" if s.get("test_first") else ""
                head = f"{n + 1}. **{s.get('title', '')}**" if use else \
                    f"{n + 1}. ~~{s.get('title', '')}~~ (left out)"
                toggle = ""
                if open_:
                    toggle = f" · [{'leave out' if use else 'put back'}](use:{n})"
                out.append(f"{head}{toggle}  \ncheck: {s.get('check', '')}{tf} · {files}"
                           + (f"  \n{s['details']}" if s.get("details") else ""))
            if card.get("risks"):
                out.append(f"*Risks:* {card['risks']}")
            if open_:
                out.append("[Go](go:) runs the kept steps on a branch of their own; "
                           "your folder is not touched until you merge. Or say "
                           "what to change and the plan is made again.")
        job = self.job(t)
        if job is not None and job.get("job") and job.get("status") not in (
                "merged", "discarded", "running"):
            out.append(f"[Review and merge](review:{job['job']}) · "
                       f"[Discard](discard:{job['job']})")
        elif job is not None and job.get("status") in ("merged", "discarded"):
            out.append(f"*The job was {job['status']}.*")
        return "\n\n".join(out)

    # -- asking ---------------------------------------------------------------
    def _refused(self) -> str:
        host = self.panel.host
        if self.agent is None or host is None:
            return "Agent mode needs a folder."
        if getattr(host, "session", None) is not None:
            return ("End the Edit-mode session first: the agent works on a "
                    "branch of its own.")
        if self.profile() is None:
            return "Pick an agent."
        return ""

    def send(self, question: str) -> None:
        panel = self.panel
        why = self._refused()
        if why:
            panel.status.setText(why)
            return
        project, profile = self.project(), self.profile()
        task = question
        if self.plan_is_open():                       # a change to the plan
            task = f"{self._request()}\n\nChange to the plan: {question}"
        panel.input.clear()
        panel.conversation.turns.append(bc.Turn("user", question, mode=AGENT))
        report = profile.output == "report"
        self._start(f"{profile.name} is {'reviewing' if report else 'planning'}…")

        def work() -> None:
            error, result = "", None
            try:
                if report:
                    result = self.agent.report(project, profile, task,
                                               should_stop=self._stop.is_set)
                else:
                    result = self.agent.plan(project, profile, task)
            except wt.WorktreeError as exc:
                error = f"Agent mode works on a git folder: {exc}"
            except Exception as exc:                      # noqa: BLE001
                error = f"The planner did not answer: {type(exc).__name__}: {exc}"
            panel._to_ui(self._planned, error, result, report)

        threading.Thread(target=work, name="build-agent-plan", daemon=True).start()
        panel.render()

    def _request(self) -> str:
        for t in reversed(self.panel.turns()):
            if t.role == "user" and t.mode == AGENT and t.kind != "go":
                prev = t
                break
        else:
            return ""
        # the first request of a chain of plan changes
        turns = self.panel.turns()
        i = turns.index(prev)
        while i >= 2 and turns[i - 1].kind == "agent-plan" and \
                turns[i - 2].role == "user" and turns[i - 2].mode == AGENT:
            i -= 2
        return turns[i].content

    def _start(self, status: str) -> None:
        p = self.panel
        p._busy = True
        p._streamed = []
        p._pending = ""
        self._stop = threading.Event()
        p._stop = self._stop
        for b in (p.send_btn, p.new_btn, p.mode_box, p.profile_box):
            b.setEnabled(False)
        p.stop_btn.setEnabled(True)
        p.status.setText(status)

    def _planned(self, error: str, result, report: bool) -> None:
        conv = self.panel.conversation
        if not error:
            if report:
                conv.turns.append(bc.Turn("assistant", str(result or "(no report)"),
                                          mode=AGENT, kind="agent-report"))
            else:
                conv.turns.append(bc.Turn("assistant", result.text(), mode=AGENT,
                                          kind="agent-plan",
                                          steps=[ba.plan_steps(result)]))
        self._save()
        self.panel._finish(error)

    def toggle(self, n: int) -> None:
        turns = self.panel.turns()
        if not self.plan_is_open():
            return
        steps = self.card(turns[-1]).get("steps", [])
        if 0 <= n < len(steps):
            steps[n]["use"] = not steps[n].get("use", True)
            self._save()
        self.panel.render()

    def go(self) -> None:
        if not self.plan_is_open():
            return
        why = self._refused()
        plan = ba.plan_from_steps(self.card(self.panel.turns()[-1]))
        if why or plan is None:
            self.panel.status.setText(why or "Keep at least one step.")
            return
        project, profile, task = self.project(), self.profile(), self._request()
        conv = self.panel.conversation
        conv.turns.append(bc.Turn("user", GO_TEXT, mode=AGENT, kind="go"))
        turn = bc.Turn("assistant", "Working: each step is checked and committed "
                       "on the job's branch.", mode=AGENT, kind="agent-job",
                       steps=[{"kind": "agent-job", "status": "running",
                               "project": project.name}])
        conv.turns.append(turn)
        self._job_turn = turn
        self.panel._open_steps.add(len(conv.turns) - 1)
        self._start("The agent is working; Stop ends it after the current step's call.")

        def event(kind: str, text: str) -> None:
            self.panel._to_ui(self._event, turn, kind, text)

        def work() -> None:
            error, rec = "", None
            try:
                rec = self.agent.run(project, profile, task, plan, on_event=event,
                                     should_stop=self._stop.is_set)
            except Exception as exc:                      # noqa: BLE001
                error = f"The job stopped: {type(exc).__name__}: {exc}"
            self.panel._to_ui(self._ran, turn, project, error, rec)

        threading.Thread(target=work, name="build-agent-run", daemon=True).start()
        self.panel.render()

    def _event(self, turn: bc.Turn, kind: str, text: str) -> None:
        if kind in ("result", "note"):
            return
        first = (text or "").strip().splitlines()[0][:200] if text.strip() else ""
        if not first:
            return
        if kind == "phase":
            for s in turn.steps:
                if s.get("kind") == "phase" and s.get("state") == "wait":
                    s["state"] = "ok"
            turn.steps.append({"kind": "phase", "state": "wait", "text": first})
        elif kind == "check":
            turn.steps.append({"kind": "check", "state": "ok", "text": first})
        else:
            turn.steps.append({"kind": "tool", "state": "ok", "text": first})
        if not self.panel._redraw.isActive():
            self.panel._redraw.start()

    def _ran(self, turn: bc.Turn, project, error: str, rec) -> None:
        job = self.job(turn)
        for s in turn.steps:
            if s.get("kind") == "phase" and s.get("state") == "wait":
                s["state"] = "ok" if rec is not None and rec.status == "done" else "no"
        if rec is not None:
            job.update(job=rec.id, status=rec.status)
            line = ba.outcome_line(rec)
            if rec.status == "done":
                line += " Review the change, then merge it or discard it."
            try:
                if self.agent.proposed_brief(project):
                    line += " The council proposed an addition to the project's brief."
            except OSError:
                pass
            turn.content = line
        else:
            job["status"] = "failed"
            turn.content = error
        self._job_turn = None
        self._save()
        self.panel._finish(error if rec is None else "")

    # -- a finished job -------------------------------------------------------
    def _job_for(self, job_id: str):
        for t in self.panel.turns():
            j = self.job(t)
            if j is not None and j.get("job") == job_id:
                return t, j
        return None, None

    def review(self, job_id: str) -> None:
        host, panel = self.panel.host, self.panel
        turn, j = self._job_for(job_id)
        if turn is None or panel.is_busy() or self.agent is None:
            return
        project = self.agent.project_for(Path(host.agent_folder()))
        rec = self.agent.record(project, job_id)
        if rec is None:
            panel.status.setText("That job's record is gone.")
            return
        try:
            diff = self.agent.diff(project, rec)
            commits = self.agent.commits(project, rec)
        except wt.WorktreeError as exc:
            panel.status.setText(str(exc))
            return
        md = [f"## {rec.task.splitlines()[0]}", ba.outcome_line(rec)]
        md += [f"- {c}" for c in commits]
        md.append(f"```diff\n{diff or '(no committed changes)'}\n```")
        answer = host.actions.review("\n\n".join(md))
        if not answer or answer[0] != "merge":
            return
        if host.unsaved():
            panel.status.setText("Save or close your unsaved tabs first; merging "
                                 "changes the files on disk.")
            return
        try:
            line = self.agent.merge(project, rec)
        except wt.WorktreeError as exc:
            panel.status.setText(str(exc))
            return
        j["status"] = "merged"
        self._save()
        host.after_merge()
        panel.status.setText(f"Merged into your folder ({line}).")
        panel.render()

    def discard(self, job_id: str) -> None:
        host, panel = self.panel.host, self.panel
        turn, j = self._job_for(job_id)
        if turn is None or panel.is_busy() or self.agent is None:
            return
        if not host.actions.confirm("Discard the job",
                                    "Throw away the job's branch and its changes?"):
            return
        project = self.agent.project_for(Path(host.agent_folder()))
        rec = self.agent.record(project, job_id)
        try:
            if rec is not None:
                self.agent.discard(project, rec)
        except wt.WorktreeError as exc:
            panel.status.setText(str(exc))
            return
        j["status"] = "discarded"
        self._save()
        panel.status.setText("The job's branch and worktree are gone.")
        panel.render()

    def _save(self) -> None:
        conv = self.panel.conversation
        if conv is not None:
            try:
                conv.save()
            except OSError:
                pass


class ModelPicker:
    """The "Model:" box: the coder role's model (see the module notes)."""

    def __init__(self, panel: "ChatPanel", vault_dir: Path, source=None):
        self.panel = panel
        self.vault_dir = Path(vault_dir)
        # (models, vram, ram) on a worker; tests pass their own
        self.source = source or self._read
        self._filled = False
        self._busy = False

    def _read(self):
        vram, ram = bm.memory()
        return bm.installed(self.vault_dir), vram, ram

    def fill(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._filled = True
        box = self.panel.model_box
        box.setEnabled(False)

        def work() -> None:
            try:
                got = self.source()
            except Exception:                             # noqa: BLE001
                got = ([], None, None)
            self.panel._to_ui(self._show, *got)

        threading.Thread(target=work, name="build-models", daemon=True).start()

    def ensure(self) -> None:
        if not self._filled:
            self.fill()

    def _show(self, models, vram, ram) -> None:
        from council_core import model_slots
        self._busy = False
        box = self.panel.model_box
        cfg = model_slots.load(self.vault_dir)
        now = bm.current(cfg, bm.main_path(self.vault_dir))
        options = bm.choices(models, vram, ram)
        box.blockSignals(True)
        box.clear()
        box.addItem("Main model (as the Council)", "")
        for c in options:
            star = "★ " if c.recommended else ""
            box.addItem(f"{star}{c.label}", c.id)
            box.setItemData(box.count() - 1, c.why, Qt.ItemDataRole.ToolTipRole)
        if now and box.findData(now) < 0:
            box.addItem(Path(now).name if not model_slots.is_ollama(now) else now, now)
        box.setCurrentIndex(max(0, box.findData(now)))
        box.blockSignals(False)
        box.setEnabled(True)
        card = f"{vram:.0f} GB GPU" if vram else "no GPU found"
        best = options[0] if options else None
        tip = ""
        if best is None or best.fit != "gpu":
            have = [m.get("id", "") for m in models]
            more = bm.to_get(vram, have)[:2]
            if more:
                tip = (" Nothing installed fits the card fully; for code try "
                       + "; or ".join(more) + ".")
        box.setToolTip(f"The model Ask, Edit and Agent use ({card}). ★ is the best "
                       f"installed US model for code on this PC.{tip}")
        if tip and not self.panel.status.text():
            self.panel.status.setText(tip.strip())

    def chosen(self, _i: int = 0) -> None:
        model_id = self.panel.model_box.currentData()
        if model_id is None:
            return
        self.panel.status.setText("Switching the coder model…")
        vault = self.vault_dir

        def work() -> None:
            try:
                line = bm.save_coder(vault, str(model_id))
            except Exception as exc:                      # noqa: BLE001
                line = f"Not changed: {exc}"
            self.panel._to_ui(self.panel.status.setText, line)

        threading.Thread(target=work, name="build-model-save", daemon=True).start()
