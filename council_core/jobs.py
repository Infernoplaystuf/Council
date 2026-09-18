"""
council_core.jobs — the agent job queue, as a front end needs it.

A job is a goal plus a step budget. A single-worker runner executes a bounded,
READ-ONLY ReAct loop against the vault and writes a Markdown report; three
kinds of message — status, step, done — arrive while it runs.

SAFE BY DESIGN IS A PROMISE, SO IT IS STATED ONCE
The agent can list, read and summarise. It cannot write, delete, install or
reach the network. That sentence is the reason a user is willing to hand it a
goal and walk away, so it lives here rather than being retyped per front end.

THE DEFECT THAT MAKES THE TAB UNUSABLE AFTER ONE JOB
`_aj_start` clears the goal box with `self._set_text(self._aj_goal, "")`, and
`_set_text` re-DISABLES any widget not in a hardcoded tuple of three names.
`_aj_goal` is not one of them. So the goal box is permanently read-only after
the first job, and the only way back is to restart the app.

Nothing here can reproduce that, because nothing here touches a widget — which
is the point. Clearing an input is the view's business; this module returns
what to say and what was started.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple

#: The Tk spinbox's range and default.
MIN_STEPS, MAX_STEPS, DEFAULT_STEPS = 2, 20, 6

SAFETY = ("Safe by design: it can only list, read and summarise. It cannot "
          "write, delete, install, or reach the network.")


@dataclass
class JobResult:
    ok: bool
    message: str
    job_id: Optional[str] = None
    error: Optional[BaseException] = None


@dataclass
class JobListResult:
    ok: bool
    message: str
    rows: List[Tuple[str, str, str]] = field(default_factory=list)  # status/goal/steps
    ids: List[str] = field(default_factory=list)
    error: Optional[BaseException] = None


def clamp_steps(value: Any) -> int:
    """A usable step budget from whatever the control holds."""
    try:
        steps = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_STEPS
    return max(MIN_STEPS, min(MAX_STEPS, steps))


def check_goal(goal: str) -> Optional[str]:
    """Why this job cannot start, or None."""
    if not (goal or "").strip():
        return "Enter a goal first."
    return None


def start(runner: Any, goal: str, steps: Any) -> JobResult:
    """Submit a job. ``runner`` is an agent_jobs_runner.JobRunner."""
    problem = check_goal(goal)
    if problem:
        return JobResult(False, problem)
    try:
        job_id = runner.submit(goal.strip(), max_steps=clamp_steps(steps))
    except Exception as exc:                              # noqa: BLE001
        return JobResult(False, f"Could not start the job: {exc!r}", error=exc)
    return JobResult(True, f"Started {job_id}. Watch the step log below.",
                     job_id=job_id)


def cancel(runner: Any, job_id: Optional[str]) -> JobResult:
    if not job_id:
        return JobResult(False, "Select a job to cancel.")
    try:
        runner.cancel(job_id)
    except Exception as exc:                              # noqa: BLE001
        return JobResult(False, f"Could not cancel {job_id}: {exc!r}",
                         error=exc)
    return JobResult(True, f"Cancelling {job_id} after the current step…",
                     job_id=job_id)


def listing(runner: Any) -> JobListResult:
    """Every job, as rows plus the ids behind them.

    The ids travel BESIDE the rows rather than being recovered from the display
    text. Three separate places in this app recover an identifier by splitting
    a label — the chart overlay, the specialist pin and the session list — and
    all three are defects. A row is for reading.
    """
    try:
        jobs = list(runner.store.all())
    except Exception as exc:                              # noqa: BLE001
        return JobListResult(False, f"Could not read the job queue: {exc!r}",
                             error=exc)

    rows, ids = [], []
    for job in jobs:
        job_id = getattr(job, "id", "") or ""
        status = getattr(job, "status", "") or "?"
        goal = (getattr(job, "goal", "") or "")[:80]
        done = getattr(job, "steps_done", None)
        budget = getattr(job, "max_steps", None)
        steps = ("" if done is None else
                 f"{done}/{budget}" if budget else str(done))
        rows.append((status, goal, steps))
        ids.append(job_id)

    return JobListResult(
        True,
        (f"{len(rows)} job(s)." if rows else
         "No jobs yet. Give the agent a goal above."),
        rows=rows, ids=ids)


def finished_ids(runner: Any) -> List[str]:
    """Jobs that will not change again, for "Remove finished"."""
    try:
        return [getattr(j, "id", "") for j in runner.store.all()
                if (getattr(j, "status", "") or "").lower()
                in ("done", "failed", "cancelled", "error")]
    except Exception:                                     # noqa: BLE001
        return []


def report_path(runner: Any, job_id: str) -> Optional[Path]:
    """Where a finished job wrote its report, if it wrote one."""
    if not job_id:
        return None
    try:
        job = runner.store.get(job_id)
    except Exception:                                     # noqa: BLE001
        return None
    path = getattr(job, "report_path", None) or getattr(job, "output", None)
    return Path(path) if path else None


def step_line(job_id: str, number: Any, text: str) -> str:
    """One line in the live step log."""
    return f"  {job_id} · step {number}: {text}"
