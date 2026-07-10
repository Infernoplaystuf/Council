"""
agent_jobs_runner.py — the background runner for autonomous agentic jobs.

Given a GOAL, a local-model agent (safe_agent.ConstrainedAgent) drives a
bounded plan->act->observe loop over a READ-ONLY tool allow-list until the goal
is met or the step budget is hit. This module runs those jobs on ONE background
daemon worker (a FIFO queue), because the single in-process GGUF serialises all
inference (council_engine._INFERENCE_LOCK) — a second worker would only add
contention. Each step is persisted (JobStore) and streamed to the UI via the
app's ui_q, and each job has a cooperative cancel Event checked at step
boundaries.

Security is structural, not policy: the agent's data tools are READ-ONLY and
sandboxed to file_root — list_files (discover), search_files (grep contents ->
file list), read_local_file, run_pandas_analysis (the validated pandas
sandbox), and query_memory. It may also AUTHOR tools (write_tool -> run_app_tool
-> list_app_tools): the code is validated by the SAME sandbox rules and can
only ever be read-only itself, so a self-built tool still cannot delete,
write outside the output dir, use the network, or shell out. The only write the
agent can cause is the curated save of a validated tool file under the vault's
App_Built_tools/ (flagged UNREVIEWED). The "never delete from a database" and
offline rules can't be violated. The final REPORT is written by THIS runner
(not a model-invoked tool) into a dedicated agent_jobs_out/ folder.
"""
from __future__ import annotations

import queue
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from agent_jobs import AgentJob, JobStatus, JobStep, JobStore, _vault_root


class JobCancelled(Exception):
    """Raised out of the wrapped on_step to break the agent loop at the next
    step boundary when the job's cancel Event is set."""


class LocalRunner:
    """Adapter that gives ConstrainedAgent the ``.chat(messages, max_tokens)``
    it expects, backed by the single in-process GGUF via
    council_engine.local_chat (which serialises on _INFERENCE_LOCK and clamps
    the prompt to the context window). Released between calls, so a foreground
    Council question can interleave between agent steps."""

    def __init__(self, temperature: float = 0.2) -> None:
        self.temperature = float(temperature)

    def chat(self, messages, max_tokens: Optional[int] = None) -> str:
        import council_engine as ce
        return ce.local_chat(list(messages), temperature=self.temperature,
                             num_predict=int(max_tokens or 600))


_AGENT_TOOLS = ("list_files", "search_files", "read_local_file",
                "run_pandas_analysis", "query_memory",
                "list_app_tools", "write_tool", "run_app_tool")


def _default_file_root(vault_dir: Optional[Any]) -> Path:
    """data_in/ under the vault — the read sandbox root for agent jobs."""
    try:
        import data_index
        return data_index.input_dir(_vault_root(vault_dir))
    except Exception:
        return _vault_root(vault_dir) / "data_in"


def _step_from_event(ev: Any) -> JobStep:
    """Reduce a safe_agent.StepEvent to a persistable JobStep."""
    if getattr(ev, "error", None):
        kind = "error"
    elif getattr(ev, "action", "") == "final":
        kind = "final"
    elif getattr(ev, "available", True) is False:
        kind = "gap"
    elif getattr(ev, "tool", None):
        kind = "tool"
    else:
        kind = "model_reason"
    obs = (getattr(ev, "observation", None)
           or getattr(ev, "final_answer", None) or "")
    tool = getattr(ev, "tool", None)
    if kind == "final":
        label = "final answer"
    elif kind == "gap":
        label = f"requested unavailable tool: {tool}"
    elif tool:
        label = f"tool: {tool}"
    else:
        label = "reasoning"
    return JobStep(
        index=int(getattr(ev, "step", 0)),
        kind=kind,
        label=label,
        tool=tool,
        ok=(getattr(ev, "error", None) is None),
        observation=str(obs)[:1400],
        error=getattr(ev, "error", None),
        elapsed_s=float(getattr(ev, "elapsed_s", 0.0) or 0.0),
    )


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")[:48] or "job"


class JobRunner:
    """Single-worker FIFO runner for autonomous agent jobs."""

    def __init__(self, *, vault_dir: Optional[Any] = None, ui_q: Any = None,
                 file_root: Optional[Any] = None, runner: Any = None,
                 max_steps: int = 6) -> None:
        self._store = JobStore(vault_dir)
        self._vault_dir = vault_dir
        self._ui_q = ui_q
        self._file_root = (Path(file_root) if file_root
                           else _default_file_root(vault_dir))
        self._runner = runner or LocalRunner()
        self._max_steps = int(max_steps)
        self._q: "queue.Queue[str]" = queue.Queue()
        self._cancels: dict = {}
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._started = False

    # ── public API ────────────────────────────────────────────
    @property
    def store(self) -> JobStore:
        return self._store

    def start(self) -> None:
        with self._lock:
            if not self._started:
                self._started = True
                self._worker = threading.Thread(target=self._loop, daemon=True)
                self._worker.start()

    def submit(self, goal: str, *, max_steps: Optional[int] = None) -> str:
        goal = (goal or "").strip()
        job_id = "job_%d" % int(time.time() * 1000)
        job = AgentJob(job_id=job_id, goal=goal,
                       status=JobStatus.QUEUED.value,
                       max_steps=int(max_steps or self._max_steps))
        self._store.upsert(job)
        self._cancels[job_id] = threading.Event()
        self.start()
        self._q.put(job_id)
        self._post(("job_status", job_id, JobStatus.QUEUED.value))
        return job_id

    def cancel(self, job_id: str) -> None:
        ev = self._cancels.get(job_id)
        if ev is not None:
            ev.set()

    # ── internals ─────────────────────────────────────────────
    def _post(self, msg: tuple) -> None:
        if self._ui_q is not None:
            try:
                self._ui_q.put(msg)
            except Exception:
                pass

    def _loop(self) -> None:
        while True:
            job_id = self._q.get()
            try:
                self._run_job(job_id)
            except Exception as exc:
                self._store.set_status(job_id, JobStatus.FAILED.value,
                                       stopped_reason=repr(exc))
                self._post(("job_done", job_id, JobStatus.FAILED.value,
                            repr(exc)))
            finally:
                self._q.task_done()

    def _build_agent(self, job: AgentJob):
        from safe_agent import AgentPolicy, ConstrainedAgent
        from tool_registry import build_default_registry
        out_dir = _vault_root(self._vault_dir) / "agent_jobs_out"
        policy = AgentPolicy(
            allowed_tools=_AGENT_TOOLS,
            file_root=self._file_root,
            output_dir=out_dir,
            max_steps=int(job.max_steps),
        )
        registry = build_default_registry(policy)
        gap_log = None
        try:
            import agent_logs
            gap_log = agent_logs.ToolGapLog()
        except Exception:
            gap_log = None
        return ConstrainedAgent(self._runner, registry, policy, gap_log=gap_log)

    def _run_job(self, job_id: str) -> None:
        """Run one job synchronously. Also callable directly (tests)."""
        job = self._store.get(job_id)
        if job is None:
            return
        cancel = self._cancels.setdefault(job_id, threading.Event())
        self._store.set_status(job_id, JobStatus.RUNNING.value)
        self._post(("job_status", job_id, JobStatus.RUNNING.value))
        agent = self._build_agent(job)

        reduced: list = []   # steps already reduced in on_step — reuse for the report

        def on_step(ev: Any, run: Any) -> None:
            step = _step_from_event(ev)
            reduced.append(step)
            self._store.append_step(job_id, step)
            self._post(("job_step", job_id, step.to_dict()))
            if cancel.is_set():
                raise JobCancelled()

        try:
            run = agent.run(job.goal, on_step=on_step)
        except JobCancelled:
            self._store.set_status(job_id, JobStatus.CANCELLED.value,
                                   stopped_reason="cancelled")
            self._post(("job_done", job_id, JobStatus.CANCELLED.value,
                        "cancelled"))
            return

        summary = (getattr(run, "final_answer", "") or "").strip()
        stopped = getattr(run, "stopped_reason", "") or ""
        status = (JobStatus.FAILED.value if stopped == "error"
                  else JobStatus.DONE.value)
        report_path = ""
        try:
            report_path = self._write_report(job_id, job.goal, run,
                                             steps=reduced)
        except Exception:
            report_path = ""
        j = self._store.get(job_id)
        if j is not None:
            j.result_summary = summary[:4000]
            j.status = status
            j.stopped_reason = stopped
            j.report_path = report_path
            self._store.upsert(j)
        self._post(("job_done", job_id, status, summary[:400]))

    def _write_report(self, job_id: str, goal: str, run: Any,
                      *, steps: Optional[list] = None) -> str:
        """Write a Markdown report of the run — the job's deliverable. Done by
        the runner (NOT a model-invoked tool), into a dedicated jobs-out dir.
        ``steps`` are the already-reduced JobSteps from on_step (reused so we
        don't re-reduce every StepEvent); falls back to reducing run.steps."""
        out_dir = _vault_root(self._vault_dir) / "agent_jobs_out"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"report__{_slug(goal)}__{job_id}.md"
        lines = [f"# Agent job report", "",
                 f"**Goal:** {goal}", "",
                 f"**Outcome:** {getattr(run, 'stopped_reason', '') or 'done'}",
                 "", "## Answer", "",
                 (getattr(run, "final_answer", "") or "").strip(), "",
                 "## Steps", ""]
        _steps = (steps if steps is not None
                  else [_step_from_event(ev)
                        for ev in (getattr(run, "steps", []) or [])])
        for st in _steps:
            lines.append(f"- **{st.index}. {st.label}**"
                         + (f" — {st.observation[:200]}" if st.observation
                            else "")
                         + (f" _(error: {st.error})_" if st.error else ""))
        try:
            path.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            return ""
        return str(path)
