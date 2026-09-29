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

    def __init__(self, temperature: float = 0.2,
                 role: Optional[str] = None) -> None:
        self.temperature = float(temperature)
        # Which role this agent speaks for — and so which model slot answers
        # (council_core.model_slots). None is the main model, as before.
        self.role = role

    def chat(self, messages, max_tokens: Optional[int] = None) -> str:
        import council_engine as ce
        return ce.local_chat(list(messages), temperature=self.temperature,
                             num_predict=int(max_tokens or 600),
                             role=self.role)


_TABULAR = {".csv", ".tsv", ".txt"}


def data_overview(root: Path, *, max_files: int = 25,
                  max_chars: int = 1500) -> str:
    """What the agent can read, for a planner that has not looked yet.

    File names (relative to the sandbox root) with sizes, and for delimited
    text files the header row and the number of rows. Deterministic, read-only
    and bounded: the header comes from the first line, the row count stops at
    2 MB. A planner that saw this plans against real column names instead of
    inventing plausible ones.
    """
    root = Path(root)
    if not root.is_dir():
        return ""
    lines = []
    try:
        files = sorted(p for p in root.rglob("*") if p.is_file()
                       and not any(part.startswith(".")
                                   for part in p.relative_to(root).parts))
    except OSError:
        return ""
    for path in files[:max_files]:
        rel = path.relative_to(root).as_posix()
        try:
            size = path.stat().st_size
        except OSError:
            continue
        entry = f"- {rel} ({size:,} bytes)"
        if path.suffix.lower() in _TABULAR:
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    header = fh.readline().strip()
                    rows, read = 0, len(header)
                    for line in fh:
                        rows += 1
                        read += len(line)
                        if read > 2_000_000:
                            rows = f"{rows}+"
                            break
                entry += f" — columns: {header[:300]} — {rows} data rows"
            except OSError:
                pass
        lines.append(entry)
    if len(files) > max_files:
        lines.append(f"- … and {len(files) - max_files} more file(s)")
    return "\n".join(lines)[:max_chars]


def _engine_chat(role: str, messages, max_tokens: int) -> str:
    """One model call as ``role`` — the chain's planner, checker and writer.
    Low temperature: these are judgement calls, not creative ones."""
    import council_engine as ce
    return ce.local_chat(list(messages), temperature=0.2,
                         num_predict=int(max_tokens), role=role)


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
                 max_steps: int = 6,
                 chat: Optional[Callable[..., str]] = None) -> None:
        self._store = JobStore(vault_dir)
        self._vault_dir = vault_dir
        self._ui_q = ui_q
        self._file_root = (Path(file_root) if file_root
                           else _default_file_root(vault_dir))
        self._runner = runner or LocalRunner()
        # (role, messages, max_tokens) -> reply: the chain's planner, checker
        # and writer. Defaults to the engine, each role on its own model slot.
        self._chat = chat or _engine_chat
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

    def submit(self, goal: str, *, max_steps: Optional[int] = None,
               mode: str = "single") -> str:
        goal = (goal or "").strip()
        job_id = "job_%d" % int(time.time() * 1000)
        job = AgentJob(job_id=job_id, goal=goal,
                       status=JobStatus.QUEUED.value,
                       max_steps=int(max_steps or self._max_steps),
                       mode="chain" if mode == "chain" else "single")
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

    def _runner_for(self, role: Optional[str]):
        """A model runner speaking for ``role``. An injected runner (a test's
        stand-in) is used as-is."""
        if isinstance(self._runner, LocalRunner):
            return LocalRunner(self._runner.temperature, role=role)
        return self._runner

    def _build_agent(self, job: AgentJob, runner: Any = None):
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
        return ConstrainedAgent(runner or self._runner, registry, policy,
                                gap_log=gap_log)

    def _run_job(self, job_id: str) -> None:
        """Run one job synchronously. Also callable directly (tests)."""
        job = self._store.get(job_id)
        if job is None:
            return
        cancel = self._cancels.setdefault(job_id, threading.Event())
        self._store.set_status(job_id, JobStatus.RUNNING.value)
        self._post(("job_status", job_id, JobStatus.RUNNING.value))
        if job.mode == "chain":
            self._run_chain(job, cancel)
            return
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

    def _run_chain(self, job: AgentJob, cancel: threading.Event) -> None:
        """A connected-tasks job: council_core.task_chain over this runner's
        sandboxed agents. Same store, queue, cancel and report as a single
        job — only what happens between "running" and "done" differs.

        Each worker is a fresh ConstrainedAgent with this job's step budget,
        speaking for the chain's worker role (the Intern), so under the
        Balanced preset the steps run on the small fast model while planning,
        checking and writing stay on the main one.
        """
        from council_core import task_chain as tc
        job_id = job.job_id
        counter = {"n": 0}

        def on_event(ev: Any) -> None:
            counter["n"] += 1
            first = (ev.text or "").splitlines()[0] if ev.text else ev.stage
            step = JobStep(index=counter["n"], kind=f"chain_{ev.stage}",
                           label=f"{ev.stage}: {first}"[:200],
                           ok=ev.ok, observation=(ev.text or "")[:1400])
            self._store.append_step(job_id, step)
            self._post(("job_step", job_id, step.to_dict()))

        def work(role: str, task: str, on_step: Callable) -> Any:
            agent = self._build_agent(job, runner=self._runner_for(role))
            run = agent.run(task, on_step=lambda ev, _run: on_step(ev))
            return tc.WorkerResult(
                answer=getattr(run, "final_answer", "") or "",
                stopped_reason=getattr(run, "stopped_reason", "") or "",
                tools_used=list(getattr(run, "tools_used", []) or []))

        chain = tc.TaskChain(self._chat, work, on_event=on_event,
                             should_stop=cancel.is_set,
                             data_overview=lambda: data_overview(
                                 self._file_root))
        try:
            result = chain.run(job.goal)
        except (tc.ChainCancelled, JobCancelled):
            self._store.set_status(job_id, JobStatus.CANCELLED.value,
                                   stopped_reason="cancelled")
            self._post(("job_done", job_id, JobStatus.CANCELLED.value,
                        "cancelled"))
            return
        except Exception as exc:
            self._store.set_status(job_id, JobStatus.FAILED.value,
                                   stopped_reason=repr(exc))
            self._post(("job_done", job_id, JobStatus.FAILED.value,
                        repr(exc)))
            return

        report_path = ""
        out_dir = _vault_root(self._vault_dir) / "agent_jobs_out"
        text = tc.report_markdown(result)
        # The goal in the name is a convenience; the report is the point. A
        # long goal in a deep vault passed Windows' 260-character path limit
        # and the report was silently lost — so fall back to the id alone.
        for name in (f"chain__{_slug(job.goal)}__{job_id}.md",
                     f"chain__{job_id}.md"):
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / name).write_text(text, encoding="utf-8")
                report_path = str(out_dir / name)
                break
            except Exception:
                continue
        stopped = (f"{result.verified_count}/{len(result.steps)} steps "
                   "verified")
        j = self._store.get(job_id)
        if j is not None:
            j.result_summary = (result.answer or "")[:4000]
            j.status = JobStatus.DONE.value
            j.stopped_reason = stopped
            j.report_path = report_path
            self._store.upsert(j)
        self._post(("job_done", job_id, JobStatus.DONE.value,
                    (result.answer or "")[:400]))

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
