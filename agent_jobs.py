"""
agent_jobs.py — persisted state model for background agentic jobs.

An AgentJob is a GOAL the user hands to a local-model agent to pursue
autonomously: a bounded ReAct loop (safe_agent.ConstrainedAgent) over a
read-only tool allow-list. The job plus its per-step trace persist to
<vault>/agent_jobs.json so a job that was running is discoverable after a
restart and the user can inspect exactly what the agent did.

Pure + dependency-light (json / dataclasses / threading / time). The runtime
cancel signal is a threading.Event held on the in-RAM runner, never here — this
module only stores serialisable state.

Storage mirrors deferred_tasks.py / derived_results.py: a single JSON list,
written atomically via a temp file + replace. Every read AND write holds the
module lock: on Windows a file another thread is reading cannot be replaced,
and the write used to drop that update in silence (measured: 14 of 150 steps
kept while the Jobs tab polled). A failed read is never taken for an empty
list on the write path, which used to save only the job being changed and
lose every other one. A busy file is waited for (another Council may hold it);
a write that still cannot land raises JobStoreError instead of vanishing; a
file that no longer parses is moved aside, never overwritten.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

_STORE_NAME = "agent_jobs.json"
_LOCK = threading.RLock()
#: How long a busy file (open in another program) is waited for, and how
#: often it is tried again.
_RETRY_SECONDS = 2.0
_RETRY_PAUSE_S = 0.02


class JobStoreError(RuntimeError):
    """The job list could not be read or saved - said, never dropped."""


def _retrying(action: Any) -> Any:
    """`action()`, tried again while Windows says the file is in use."""
    deadline = time.monotonic() + _RETRY_SECONDS
    while True:
        try:
            return action()
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_RETRY_PAUSE_S)


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _vault_root(vault_dir: Optional[Any] = None) -> Path:
    if vault_dir is not None:
        return Path(vault_dir).expanduser().resolve()
    env = os.environ.get("COUNCIL_VAULT_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".council" / "vault"


@dataclass
class JobStep:
    """One turn of the agent loop, already reduced to display/persist form."""
    index: int
    kind: str = "model_reason"       # model_reason | tool | final | error | gap
    label: str = ""
    tool: Optional[str] = None
    ok: Optional[bool] = None
    observation: str = ""            # bounded
    error: Optional[str] = None
    elapsed_s: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "JobStep":
        return JobStep(**{k: v for k, v in d.items() if k in _JOBSTEP_FIELDS})


_JOBSTEP_FIELDS = frozenset(JobStep.__dataclass_fields__)   # hoisted once


@dataclass
class AgentJob:
    job_id: str
    goal: str
    status: str = JobStatus.QUEUED.value
    steps: List[JobStep] = field(default_factory=list)
    max_steps: int = 6
    result_summary: str = ""
    report_path: str = ""
    stopped_reason: str = ""
    # "single": one agent pursues the goal. "chain": council_core.task_chain —
    # a plan, a small worker per step, checks, one answer. Older records have
    # no mode and load as "single".
    mode: str = "single"
    created_ts: float = field(default_factory=time.time)
    updated_ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["steps"] = [s.to_dict() if isinstance(s, JobStep) else s
                      for s in self.steps]
        return d

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "AgentJob":
        dd = {k: v for k, v in d.items() if k in _AGENTJOB_FIELDS}
        dd["steps"] = [JobStep.from_dict(s) for s in (dd.get("steps") or [])]
        return AgentJob(**dd)


_AGENTJOB_FIELDS = frozenset(AgentJob.__dataclass_fields__)   # hoisted once


class JobStore:
    """Per-vault persistence for AgentJobs (agent_jobs.json)."""

    def __init__(self, vault_dir: Optional[Any] = None) -> None:
        self.path = _vault_root(vault_dir) / _STORE_NAME

    def _read(self, strict: bool = False) -> List[Dict[str, Any]]:
        """The stored list. `strict` (the write path): a file that cannot be
        read raises JobStoreError rather than reading as empty, and a file
        that does not parse is moved aside first. A display read falls back
        to an empty list."""
        with _LOCK:
            if not self.path.exists():
                return []
            try:
                text = _retrying(
                    lambda: self.path.read_text(encoding="utf-8"))
            except OSError as exc:
                if strict:
                    raise JobStoreError(
                        f"could not read the job list {self.path}: {exc}")                         from exc
                return []
            try:
                d = json.loads(text)
            except ValueError:
                d = None
            if isinstance(d, list):
                return d
            if strict:
                self._set_aside()
            return []

    def _set_aside(self) -> None:
        """A job list that no longer parses is kept as
        agent_jobs.json.damaged-<when>, never replaced."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for n in range(1, 1000):
            name = f"{_STORE_NAME}.damaged-{stamp}" + (f"-{n}" if n > 1 else "")
            target = self.path.with_name(name)
            if target.exists():
                continue
            try:
                _retrying(lambda: self.path.rename(target))
            except FileExistsError:
                continue
            except OSError as exc:
                raise JobStoreError(
                    f"the job list {self.path} is damaged and could not be "
                    f"moved aside: {exc}") from exc
            return
        raise JobStoreError(f"the job list {self.path} is damaged and could "
                            "not be moved aside")

    def _write(self, items: List[Dict[str, Any]]) -> None:
        with _LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(
                f".{_STORE_NAME}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            try:
                _retrying(lambda: tmp.replace(self.path))
            except OSError as exc:
                try:
                    tmp.unlink()        # our own temp file, never the list
                except OSError:
                    pass
                raise JobStoreError(
                    f"could not save the job list {self.path}: {exc}") from exc

    def all(self) -> List[AgentJob]:
        return [AgentJob.from_dict(d) for d in self._read()]

    def get(self, job_id: str) -> Optional[AgentJob]:
        # Scan the raw dicts first; only materialise the ONE match (was
        # from_dict on every job + all its steps just to find one).
        for d in self._read():
            if d.get("job_id") == job_id:
                return AgentJob.from_dict(d)
        return None

    def get_summary(self, job_id: str):
        """(status, goal, step_count) for one job from a single raw read,
        WITHOUT building AgentJob/JobStep dataclasses — for the hot UI row
        update on every job event. None if the job isn't found."""
        for d in self._read():
            if d.get("job_id") == job_id:
                return (d.get("status", ""), str(d.get("goal", "")),
                        len(d.get("steps") or []))
        return None

    def upsert(self, job: AgentJob) -> None:
        with _LOCK:
            items = self._read(strict=True)
            job.updated_ts = time.time()
            out = [it for it in items if it.get("job_id") != job.job_id]
            out.append(job.to_dict())
            self._write(out)

    def set_status(self, job_id: str, status: str,
                   stopped_reason: Optional[str] = None) -> None:
        with _LOCK:
            items = self._read(strict=True)
            for it in items:
                if it.get("job_id") == job_id:
                    it["status"] = status
                    it["updated_ts"] = time.time()
                    if stopped_reason is not None:
                        it["stopped_reason"] = stopped_reason
            self._write(items)

    def append_step(self, job_id: str, step: JobStep) -> None:
        with _LOCK:
            items = self._read(strict=True)
            for it in items:
                if it.get("job_id") == job_id:
                    it.setdefault("steps", []).append(step.to_dict())
                    it["updated_ts"] = time.time()
            self._write(items)

    def running(self) -> List[AgentJob]:
        return [j for j in self.all() if j.status == JobStatus.RUNNING.value]

    def delete(self, job_id: str) -> bool:
        with _LOCK:
            items = self._read(strict=True)
            new = [it for it in items if it.get("job_id") != job_id]
            if len(new) != len(items):
                self._write(new)
                return True
        return False
