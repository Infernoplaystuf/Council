"""
council_core.build_agent — the Build chat's Agent mode: the council plans a
change to the open folder, the user approves the plan, and an agent carries
it out on a branch of its own, one checked and committed step at a time
(council_core.code_agent, the Code tab's pipeline). Then the user reviews the
branch and merges it into the folder, or discards it.

    The project   the folder's saved project (council_core.project), found
                  by its folder; a folder with none gets one named after it,
                  with pytest as its test command when it has tests, saved
                  the first time a plan is made.
    The agent     a profile (council_core.code_profiles): the built-in
                  "Qt app builder" and the rest, or the user's own. A
                  profile whose output is a report changes nothing: its
                  report is the answer.
    The plan      code_agent.make_plan's steps, kept on the chat turn as
                  plain data (plan_steps / plan_from_steps) so it survives a
                  restart; the user can leave steps out before Go.
    The job       code_agent.run_job in a worktree under the project's
                  folder in the vault. The user's folder is not touched
                  until Merge, which is refused over uncommitted work.

No Qt here: the panel (council_qt.widgets.build_agent) calls it on a worker.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import code_agent as ca
from . import code_profiles as ap
from . import project as pj
from . import worktree as wt

def _same(a: str, b: str) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except (OSError, ValueError):
        return False


def test_command(python: str) -> str:
    """pytest with the Build tab's "Run with" Python, quoted for shlex."""
    return f'"{python}" -m pytest -q -p no:cacheprovider'


class Agent:
    """What Agent mode asks of the council. Tests pass scripted models."""

    def __init__(self, vault_dir: Path, chat: Optional[Callable] = None,
                 chat_tools: Optional[Callable] = None):
        self.vault_dir = Path(vault_dir)
        self.chat = chat
        self.chat_tools = chat_tools

    # -- the project and the agents -------------------------------------
    def project_for(self, root: Path, python: str = "",
                    has_tests: bool = False) -> pj.Project:
        """The saved project for ``root``, else a new one (not saved yet)."""
        for p in pj.list_projects(self.vault_dir):
            if p.root and _same(p.root, str(root)):
                return p
        taken = {p.slug for p in pj.list_projects(self.vault_dir)}
        name, n = Path(root).name or "project", 2
        base = name
        while pj.slug(name) in taken:
            name, n = f"{base} ({n})", n + 1
        return pj.Project(name, str(Path(root)),
                          test_command=test_command(python)
                          if python and has_tests else "")

    def save_project(self, project: pj.Project) -> None:
        pj.save(self.vault_dir, project)

    def is_saved(self, project: pj.Project) -> bool:
        return pj.load(self.vault_dir, project.name) is not None

    def profiles(self) -> List[ap.AgentProfile]:
        return ap.list_profiles(self.vault_dir)

    def save_profile(self, profile: ap.AgentProfile) -> ap.AgentProfile:
        return ap.save(self.vault_dir, profile)

    # -- plan, run, report ------------------------------------------------
    def plan(self, project: pj.Project, profile: ap.AgentProfile,
             task: str) -> ca.Plan:
        """BLOCKING. Refused (WorktreeError) outside a git folder."""
        wt.require_repo(Path(project.root))
        if not self.is_saved(project):
            self.save_project(project)
        proj = ap.with_references(project, profile)
        framed = (f"{task}\n\n(The agent doing this: {profile.name}. "
                  f"{profile.instructions})")
        return ca.make_plan(self.vault_dir, proj, framed, chat=self.chat)

    def run(self, project: pj.Project, profile: ap.AgentProfile, task: str,
            plan: ca.Plan, on_event=None, should_stop=None) -> ca.JobRecord:
        """BLOCKING: every step of ``plan`` on the job's own branch."""
        proj = ap.with_references(project, profile)
        rec = ca.new_job(self.vault_dir, proj, task, plan)
        return ca.run_job(self.vault_dir, proj, rec, chat=self.chat,
                          chat_tools=self.chat_tools, role=profile.role,
                          on_event=on_event, should_stop=should_stop,
                          max_turns=profile.max_turns,
                          tool_names=profile.tools,
                          extra_system=profile.instructions)

    def report(self, project: pj.Project, profile: ap.AgentProfile,
               task: str, on_event=None, should_stop=None) -> str:
        """BLOCKING: a report-only agent's answer; nothing is changed."""
        wt.require_repo(Path(project.root))
        return ap.run_report(self.vault_dir, project, profile, task,
                             chat_tools=self.chat_tools, on_event=on_event,
                             should_stop=should_stop)

    def bench(self, project: pj.Project, count: int, say, should_stop):
        """BLOCKING: the council redoes recent commits (code_bench)."""
        from . import code_bench
        if not self.is_saved(project):
            self.save_project(project)
        return code_bench.run(self.vault_dir, project, label="history",
                              count=count, chat=self.chat,
                              chat_tools=self.chat_tools, say=say,
                              should_stop=should_stop)

    # -- a finished job -----------------------------------------------------
    def record(self, project: pj.Project, job_id: str) -> Optional[ca.JobRecord]:
        return next((r for r in ca.list_records(self.vault_dir, project)
                     if r.id == job_id), None)

    def jobs(self, project: pj.Project) -> List[ca.JobRecord]:
        return [r for r in ca.list_records(self.vault_dir, project) if r.branch]

    def _job(self, project: pj.Project, rec: ca.JobRecord) -> wt.Job:
        return wt.open_job(project.root,
                           pj.project_dir(self.vault_dir, project) / "worktrees",
                           rec.id, rec.base)

    def diff(self, project: pj.Project, rec: ca.JobRecord) -> str:
        return self._job(project, rec).diff()

    def commits(self, project: pj.Project, rec: ca.JobRecord) -> List[str]:
        return self._job(project, rec).log()

    def merge(self, project: pj.Project, rec: ca.JobRecord) -> str:
        """Into the folder's checked-out branch; WorktreeError when the
        folder has uncommitted work (nothing is changed then)."""
        job = self._job(project, rec)
        line = wt.merge(job, message=f"Council: {rec.task.splitlines()[0]}")
        wt.discard(job)
        self._set_status(project, rec, "merged")
        return line

    def discard(self, project: pj.Project, rec: ca.JobRecord) -> None:
        wt.discard(self._job(project, rec))
        self._set_status(project, rec, "discarded")

    def _set_status(self, project: pj.Project, rec: ca.JobRecord,
                    status: str) -> None:
        rec.status = status
        ca.save_record(self.vault_dir, project, rec)

    def proposed_brief(self, project: pj.Project) -> bool:
        return bool(pj.proposed_brief(self.vault_dir, project))


# ── the plan as chat-turn data ───────────────────────────────────────────
def plan_steps(plan: ca.Plan) -> Dict[str, Any]:
    """``plan`` as JSON-able data for a chat turn; every step in use."""
    return {"kind": "agent-plan", "goal": plan.goal, "risks": plan.risks,
            "steps": [dict(asdict(s), use=True) for s in plan.steps]}


def plan_from_steps(card: Dict[str, Any]) -> Optional[ca.Plan]:
    """The plan with the steps the user kept, or None when none are."""
    fields = set(ca.Step.__dataclass_fields__)
    steps = [ca.Step(**{k: v for k, v in s.items() if k in fields})
             for s in card.get("steps", []) if s.get("use", True)]
    if not steps:
        return None
    return ca.Plan(card.get("goal", ""), steps, card.get("risks", ""))


def outcome_line(rec: ca.JobRecord) -> str:
    steps = ca.plan_of(rec).steps
    done = sum(s.status == "done" for s in steps)
    return (f"Job {rec.status}: {done}/{len(steps)} step(s) committed on "
            f"{rec.branch or 'no branch'}.")


__all__ = ["Agent", "plan_steps", "plan_from_steps", "outcome_line",
           "test_command"]
