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
written atomically via a temp file + replace, guarded by a module lock.
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
_LOCK = threading.Lock()


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
        known = set(JobStep.__dataclass_fields__)  # type: ignore
        return JobStep(**{k: v for k, v in d.items() if k in known})


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
    created_ts: float = field(default_factory=time.time)
    updated_ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["steps"] = [s.to_dict() if isinstance(s, JobStep) else s
                      for s in self.steps]
        return d

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "AgentJob":
        known = set(AgentJob.__dataclass_fields__)  # type: ignore
        dd = {k: v for k, v in d.items() if k in known}
        dd["steps"] = [JobStep.from_dict(s) for s in (dd.get("steps") or [])]
        return AgentJob(**dd)


class JobStore:
    """Per-vault persistence for AgentJobs (agent_jobs.json)."""

    def __init__(self, vault_dir: Optional[Any] = None) -> None:
        self.path = _vault_root(vault_dir) / _STORE_NAME

    def _read(self) -> List[Dict[str, Any]]:
        try:
            if self.path.exists():
                d = json.loads(self.path.read_text(encoding="utf-8"))
                return d if isinstance(d, list) else []
        except Exception:
            pass
        return []

    def _write(self, items: List[Dict[str, Any]]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            pass

    def all(self) -> List[AgentJob]:
        return [AgentJob.from_dict(d) for d in self._read()]

    def get(self, job_id: str) -> Optional[AgentJob]:
        for j in self.all():
            if j.job_id == job_id:
                return j
        return None

    def upsert(self, job: AgentJob) -> None:
        with _LOCK:
            items = self._read()
            job.updated_ts = time.time()
            out = [it for it in items if it.get("job_id") != job.job_id]
            out.append(job.to_dict())
            self._write(out)

    def set_status(self, job_id: str, status: str,
                   stopped_reason: Optional[str] = None) -> None:
        with _LOCK:
            items = self._read()
            for it in items:
                if it.get("job_id") == job_id:
                    it["status"] = status
                    it["updated_ts"] = time.time()
                    if stopped_reason is not None:
                        it["stopped_reason"] = stopped_reason
            self._write(items)

    def append_step(self, job_id: str, step: JobStep) -> None:
        with _LOCK:
            items = self._read()
            for it in items:
                if it.get("job_id") == job_id:
                    it.setdefault("steps", []).append(step.to_dict())
                    it["updated_ts"] = time.time()
            self._write(items)

    def running(self) -> List[AgentJob]:
        return [j for j in self.all() if j.status == JobStatus.RUNNING.value]

    def delete(self, job_id: str) -> bool:
        with _LOCK:
            items = self._read()
            new = [it for it in items if it.get("job_id") != job_id]
            if len(new) != len(items):
                self._write(new)
                return True
        return False
