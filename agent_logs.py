"""
agent_logs.py — append-only JSONL audit logs for the constrained agent.

Two separate stores, both local files (one record per line, never
mutated in place — audit-grade trail):

  ConversationLog  ~/.council/vault/.agent_runs.jsonl
    One record per ConstrainedAgent.run() call. Schema:
        {
          "ts":             int (ms epoch),
          "task":           str,
          "final_answer":   str,
          "outcome":        "done" | "max_steps" | "byte_budget" | "error",
          "tools_used":     [str, ...],
          "tools_missing":  [str, ...],
          "step_count":     int,
          "kind":           "agent_run" | "council_deliberation",
          "session":        str (optional — Council passes this),
        }

  ToolGapLog       ~/.council/vault/.tool_gaps.jsonl
    One record per unlisted-tool request. Schema:
        {
          "ts":             int (ms epoch),
          "requested_name": str,           # the model's chosen name
          "args_shape":     {field: type}, # normalised shape, not values
          "args_sample":    {field: ...},  # first observed values, capped
          "task":           str,
          "step":           int,
          "context":        {arbitrary},
        }

Both classes:
  • Honour COUNCIL_VAULT_ROOT so the wizard's vault override is respected.
  • Create parent directories on first write.
  • Reading an empty / nonexistent file returns [] — not an exception.
  • Append-only; we never rewrite past records (audit trail).

Persisted on local disk only. No socket access. Verified by tests under
the loopback egress guard.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

_LOG = logging.getLogger("agent_logs")

# Cap how much of the model's "args" we serialise — gaps are aggregated
# by shape, not by full payload, so we don't need to keep the entire
# 1 MB pasted CSV the model handed us.
_ARG_VALUE_TRUNCATE_CHARS = 200


def _vault_root() -> Path:
    """Where audit logs live. Honour COUNCIL_VAULT_ROOT so the wizard's
    per-project vault is respected (default ~/.council/vault)."""
    env = os.environ.get("COUNCIL_VAULT_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".council" / "vault"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _args_shape(args: Dict[str, Any]) -> Dict[str, str]:
    """Normalised shape: {field_name: type_name}. The aggregator uses
    this to group gap requests that look 'the same' even when the
    specific values differ."""
    out: Dict[str, str] = {}
    if not isinstance(args, dict):
        return out
    for k, v in args.items():
        out[str(k)] = type(v).__name__
    return out


def _args_sample(args: Dict[str, Any]) -> Dict[str, Any]:
    """First-observed values, truncated. Useful as a hint when writing
    the human-reviewed tool implementation."""
    out: Dict[str, Any] = {}
    if not isinstance(args, dict):
        return out
    for k, v in args.items():
        s = str(v)
        if len(s) > _ARG_VALUE_TRUNCATE_CHARS:
            s = s[:_ARG_VALUE_TRUNCATE_CHARS] + "…"
        out[str(k)] = s
    return out


# ============================================================
# Conversation log
# ============================================================

class ConversationLog:
    """Append-only log of agent runs (and optionally Council
    deliberations). One record per line."""

    DEFAULT_FILENAME = ".agent_runs.jsonl"

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else (_vault_root() / self.DEFAULT_FILENAME)
        self._lock = threading.Lock()

    @classmethod
    def default(cls) -> "ConversationLog":
        return cls()

    def append(self,
               *,
               task: str,
               final_answer: str,
               outcome: str,
               tools_used: Iterable[str] = (),
               tools_missing: Iterable[str] = (),
               step_count: int = 0,
               kind: str = "agent_run",
               session: Optional[str] = None,
               extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        rec: Dict[str, Any] = {
            "ts":            _now_ms(),
            "task":          str(task),
            "final_answer":  str(final_answer),
            "outcome":       str(outcome),
            "tools_used":    list(tools_used),
            "tools_missing": list(tools_missing),
            "step_count":    int(step_count),
            "kind":          str(kind),
        }
        if session is not None:
            rec["session"] = str(session)
        if extra:
            rec["extra"] = dict(extra)
        self._write(rec)
        return rec

    def append_run(self, run) -> Dict[str, Any]:
        """Convenience for ConstrainedAgent.run(): pulls the schema out
        of an AgentRun and writes it."""
        return self.append(
            task=run.task,
            final_answer=run.final_answer,
            outcome=run.stopped_reason or "done",
            tools_used=run.tools_used,
            tools_missing=run.tools_missing,
            step_count=len(run.steps),
            kind="agent_run",
        )

    def all(self) -> List[Dict[str, Any]]:
        """Read every record. Returns [] when the file doesn't exist.

        Holds the same lock as ``_write`` so a reader can't observe a
        half-flushed final line mid-append (the JSONL iterator would
        silently skip it and transiently miss the newest record)."""
        with self._lock:
            return list(_iter_jsonl(self.path))

    def _write(self, rec: Dict[str, Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ============================================================
# Tool gap log
# ============================================================

class ToolGapLog:
    """Append-only log of unlisted-tool requests. The analyzer reads
    this to identify recurring gaps and draft tool proposals."""

    DEFAULT_FILENAME = ".tool_gaps.jsonl"

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else (_vault_root() / self.DEFAULT_FILENAME)
        self._lock = threading.Lock()

    @classmethod
    def default(cls) -> "ToolGapLog":
        return cls()

    def append(self,
               *,
               requested_name: str,
               args: Dict[str, Any],
               task: str,
               step: int = 0,
               context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        rec: Dict[str, Any] = {
            "ts":             _now_ms(),
            "requested_name": str(requested_name),
            "args_shape":     _args_shape(args or {}),
            "args_sample":    _args_sample(args or {}),
            "task":           str(task),
            "step":           int(step),
            "context":        dict(context or {}),
        }
        self._write(rec)
        return rec

    def all(self) -> List[Dict[str, Any]]:
        # Locked for the same reason as ConversationLog.all().
        with self._lock:
            return list(_iter_jsonl(self.path))

    def _write(self, rec: Dict[str, Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ============================================================
# Failure log — the app's self-improvement signal
# ============================================================

# Signature normalisation: strip volatile fragments (absolute paths,
# hex addresses, long numbers, quoted payloads) so the analyzer can
# bucket "the same failure" across runs even when the specific file
# or value differs.
import re as _re

_SIG_SCRUBBERS = (
    (_re.compile(r"[A-Za-z]:[\\/][^\s'\"]+"), "<path>"),       # windows path
    (_re.compile(r"/[^\s'\"]+/[^\s'\"]+"), "<path>"),           # posix path
    (_re.compile(r"0x[0-9a-fA-F]+"), "<addr>"),
    (_re.compile(r"'[^']{24,}'"), "'<str>'"),                   # long quoted payloads
    (_re.compile(r'"[^"]{24,}"'), '"<str>"'),
    (_re.compile(r"\b\d{4,}\b"), "<n>"),                        # long numbers / line nos
)


def normalize_failure_signature(kind: str, message: str,
                                 max_len: int = 160) -> str:
    """Collapse a raw failure message into a stable bucket key.

    ``kind`` anchors the bucket (e.g. 'analyst.exec_error'); the
    message is scrubbed of paths / addresses / long literals so two
    occurrences of the same root cause on different files normalise
    to the same signature."""
    msg = str(message or "").strip().splitlines()[0] if message else ""
    for pat, repl in _SIG_SCRUBBERS:
        msg = pat.sub(repl, msg)
    msg = " ".join(msg.split())[:max_len]
    return f"{kind}: {msg}" if msg else str(kind)


class FailureLog:
    """Append-only log of structured failure records from any subsystem
    (analyst code-gen/exec, model loading, DB connections, …).

    This is the input half of the app's self-improvement loop: the
    ToolGapAnalyzer aggregates recurring signatures into improvement
    proposals a human reviews in the Agent panel. Nothing in this class
    (or its consumers) auto-fixes anything — same cardinal rule as the
    tool registry: identification is automatic, change is human-gated.

    Schema (one JSONL record per failure):
        {
          "ts":         int (ms epoch),
          "kind":       str,   # e.g. "analyst.exec_error"
          "subsystem":  str,   # e.g. "vault_analyst"
          "signature":  str,   # normalised bucket key
          "message":    str,   # first line, capped
          "detail":     str,   # fuller context (code, traceback), capped
          "context":    {arbitrary, optional}
        }
    """

    DEFAULT_FILENAME = ".failures.jsonl"
    _MESSAGE_CAP = 500
    _DETAIL_CAP = 4000

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else (_vault_root() / self.DEFAULT_FILENAME)
        self._lock = threading.Lock()

    @classmethod
    def default(cls) -> "FailureLog":
        return cls()

    def append(self,
               *,
               kind: str,
               subsystem: str,
               message: str,
               detail: str = "",
               context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        rec: Dict[str, Any] = {
            "ts":        _now_ms(),
            "kind":      str(kind),
            "subsystem": str(subsystem),
            "signature": normalize_failure_signature(kind, message),
            "message":   str(message or "")[: self._MESSAGE_CAP],
            "detail":    str(detail or "")[: self._DETAIL_CAP],
        }
        if context:
            rec["context"] = dict(context)
        self._write(rec)
        return rec

    def all(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(_iter_jsonl(self.path))

    def _write(self, rec: Dict[str, Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def record_failure(kind: str, subsystem: str, message: str,
                   detail: str = "",
                   context: Optional[Dict[str, Any]] = None) -> None:
    """Fire-and-forget failure capture for hot paths.

    NEVER raises — a failure in failure-recording must not break the
    code path that was already failing. Call sites sprinkle this at
    the moment an error is surfaced to the user."""
    try:
        FailureLog.default().append(
            kind=kind, subsystem=subsystem,
            message=message, detail=detail, context=context,
        )
    except Exception as exc:  # pragma: no cover — best-effort by design
        _LOG.debug("record_failure swallowed: %r", exc)


# ============================================================
# Helpers
# ============================================================

def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    # Corrupt line — skip rather than crash the reader.
                    _LOG.warning("skipping malformed log line in %s", p)
                    continue
    except Exception as exc:
        _LOG.warning("could not read log %s: %r", p, exc)
        return
