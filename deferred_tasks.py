"""
deferred_tasks.py — "the model couldn't do this easily, save it for later".

When the council can't satisfy a request in-chat (e.g. "give me a much bigger
summary of that file", a deep stats pass, or something that needs a tool the
app doesn't have yet), the user can DEFER it: capture the question + context
into a small per-vault store. The Vault tab then lists those deferred tasks
and can RUN the runnable ones with the heavyweight deterministic tooling that
isn't convenient to invoke mid-conversation — and surfaces tool REQUESTS for
the developer.

This is the USER-facing, explicitly-saved counterpart to the developer-facing
auto-captured FailureLog / ToolGapLog in agent_logs.py. Tool-request kinds are
ALSO mirrored into ToolGapLog so the existing tool_gap_analyzer keeps working.

Storage: a single JSON array at <vault>/deferred_tasks.json (read-modify-write
under a lock). Task counts are small (dozens), so a rewrite-on-change file is
simpler and safer than append-only JSONL for mutable status.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

_STORE_NAME = "deferred_tasks.json"
_LOCK = threading.Lock()

# Runnable kinds map to a deterministic heavy operation in the Vault tab;
# tool_request / other are not auto-runnable.
KIND_BIGGER_SUMMARY = "bigger_summary"
KIND_DEEPER_STATS = "deeper_stats"
KIND_TOOL_REQUEST = "tool_request"
KIND_OTHER = "other"
KINDS = (KIND_BIGGER_SUMMARY, KIND_DEEPER_STATS, KIND_TOOL_REQUEST, KIND_OTHER)
RUNNABLE_KINDS = (KIND_BIGGER_SUMMARY, KIND_DEEPER_STATS)

STATUS_PENDING = "pending"
STATUS_DONE = "done"
STATUS_DISMISSED = "dismissed"


def _vault_root(vault_dir: Optional[Any] = None) -> Path:
    if vault_dir is not None:
        return Path(vault_dir).expanduser().resolve()
    env = os.environ.get("COUNCIL_VAULT_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".council" / "vault"


@dataclass
class DeferredTask:
    id: str
    created_ts: float
    kind: str
    question: str
    answer_excerpt: str = ""
    files: List[str] = field(default_factory=list)
    folder: str = ""                 # optional scope folder (relative to data_in)
    note: str = ""
    status: str = STATUS_PENDING
    result_path: str = ""            # set when a runnable task is completed
    result_summary: str = ""
    done_ts: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "DeferredTask":
        known = {f for f in DeferredTask.__dataclass_fields__}  # type: ignore
        return DeferredTask(**{k: v for k, v in d.items() if k in known})

    def label(self) -> str:
        q = (self.question or "").strip().replace("\n", " ")
        if len(q) > 70:
            q = q[:67] + "…"
        return q or "(no question text)"


class DeferredTaskStore:
    """Per-vault store of deferred tasks. All methods are process-safe via a
    module lock; concurrent processes are not coordinated (single-user app)."""

    def __init__(self, vault_dir: Optional[Any] = None) -> None:
        self.path = _vault_root(vault_dir) / _STORE_NAME

    # ---- low-level read / write ----
    def _read(self) -> List[Dict[str, Any]]:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                return data if isinstance(data, list) else []
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

    def _new_id(self, existing: List[Dict[str, Any]]) -> str:
        base = int(time.time() * 1000)
        used = {str(it.get("id")) for it in existing}
        cand = f"t{base}"
        n = 0
        while cand in used:               # ms collision within a fast loop
            n += 1
            cand = f"t{base}-{n}"
        return cand

    # ---- public API ----
    def add(self, *, kind: str, question: str, answer_excerpt: str = "",
            files: Optional[List[str]] = None, folder: str = "",
            note: str = "") -> DeferredTask:
        kind = kind if kind in KINDS else KIND_OTHER
        with _LOCK:
            items = self._read()
            task = DeferredTask(
                id=self._new_id(items), created_ts=time.time(), kind=kind,
                question=(question or "").strip(),
                answer_excerpt=(answer_excerpt or "")[:1000],
                files=list(files or []), folder=folder or "",
                note=(note or "").strip())
            items.append(task.to_dict())
            self._write(items)
        # Mirror tool requests into the developer-facing ToolGapLog so the
        # existing analyzer/proposal pipeline still sees them. Best-effort.
        if kind == KIND_TOOL_REQUEST:
            try:
                import agent_logs
                agent_logs.ToolGapLog().append(
                    requested_name="user_requested_tool",
                    args={}, task=task.question or task.note,
                    context={"source": "deferred_tasks", "note": task.note})
            except Exception:
                pass
        return task

    def all(self) -> List[DeferredTask]:
        return [DeferredTask.from_dict(d) for d in self._read()]

    def pending(self) -> List[DeferredTask]:
        return [t for t in self.all() if t.status == STATUS_PENDING]

    def get(self, task_id: str) -> Optional[DeferredTask]:
        for t in self.all():
            if t.id == task_id:
                return t
        return None

    def _update(self, task_id: str, **changes: Any) -> bool:
        with _LOCK:
            items = self._read()
            hit = False
            for it in items:
                if str(it.get("id")) == str(task_id):
                    it.update(changes)
                    hit = True
                    break
            if hit:
                self._write(items)
            return hit

    def mark_done(self, task_id: str, *, result_path: str = "",
                  result_summary: str = "") -> bool:
        return self._update(task_id, status=STATUS_DONE,
                            result_path=result_path,
                            result_summary=result_summary[:1000],
                            done_ts=time.time())

    def dismiss(self, task_id: str) -> bool:
        return self._update(task_id, status=STATUS_DISMISSED,
                            done_ts=time.time())

    def reopen(self, task_id: str) -> bool:
        return self._update(task_id, status=STATUS_PENDING, done_ts=None)

    def find_answered(self, question: str, *,
                      min_overlap: float = 0.5) -> Optional[DeferredTask]:
        """Return the best COMPLETED task whose question lexically matches
        ``question`` AND whose result file still exists — so re-asking a
        previously-deferred question can be answered from the saved result
        instead of recomputed. Returns None when nothing matches well enough.
        """
        best: Optional[DeferredTask] = None
        best_score = float(min_overlap)
        for t in self.all():
            if t.status != STATUS_DONE or not t.result_path:
                continue
            try:
                if not Path(t.result_path).exists():
                    continue
            except Exception:
                continue
            sc = _question_overlap(question, t.question)
            if sc >= best_score:
                best, best_score = t, sc
        return best


# Jaccard overlap on meaningful tokens — cheap "is this the same question?"
# check, mirroring task_memory's similarity but kept self-contained.
_OVERLAP_STOP = {
    "a", "an", "the", "of", "in", "on", "for", "to", "and", "or", "is", "it",
    "what", "how", "with", "by", "at", "give", "me", "my", "please", "can",
    "you", "show", "tell", "get", "do", "i", "want", "need", "that", "this",
}


def _question_overlap(a: str, b: str) -> float:
    import re
    def toks(s: str):
        return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
                if w not in _OVERLAP_STOP and len(w) > 1}
    sa, sb = toks(a), toks(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)
