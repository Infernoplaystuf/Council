"""
goal_cache.py — per-session JSONL store of distilled user goals so a
follow-up turn ("now do that for last quarter", "same thing but ranked")
can resolve its back-reference against earlier intents.

Storage layout
--------------
Goals live under ``vault/conversation_logs/goals_<session_id>.jsonl``.
That subdirectory is already listed in
``conversation_logger.PROTECTED_SUBDIRS``, so the vault index, analyst
listers, and file-injection layer all refuse to read it. The model
NEVER sees this file directly — recent goals are only surfaced when
the dispatch site explicitly asks for them and folds them into the
goal anchor.

Thread-safety
-------------
Mirrors ``ConversationLogger``: a small in-memory buffer flushes every
20 entries or on explicit ``flush()`` / ``end_session()``. All writes
guarded by ``self._lock`` since the GUI logs from worker threads.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


LOG_SUBDIR = "conversation_logs"   # same dir as ConversationLogger
FLUSH_EVERY = 20


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


class GoalCache:
    """Append-only per-session record of (raw user text → distilled goal)."""

    def __init__(self, vault_dir: Any):
        self.vault_dir = Path(vault_dir).expanduser().resolve()
        self.log_dir = self.vault_dir / LOG_SUBDIR
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.session_id: Optional[str] = None
        self.log_path: Optional[Path] = None
        self._buffer: List[Dict[str, Any]] = []
        self._all: List[Dict[str, Any]] = []   # in-memory mirror for recent()
        self._lock = threading.Lock()

    # ----------------------------------------------------------------
    # Lifecycle
    # ----------------------------------------------------------------

    def start_session(self, session_id: Optional[str] = None) -> Path:
        """Open (or reopen) the log file for ``session_id``.

        If the file already exists (e.g. session resumed from disk),
        prior entries are loaded into the in-memory mirror so
        ``recent()`` works immediately.
        """
        if not session_id:
            session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_id = session_id
        self.log_path = self.log_dir / f"goals_{session_id}.jsonl"
        self._all = []
        if self.log_path.exists():
            try:
                with open(self.log_path, "r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            self._all.append(json.loads(line))
                        except Exception:
                            pass
            except Exception as exc:
                print(f"[GoalCache] could not load prior goals: {exc!r}")
        return self.log_path

    def end_session(self) -> None:
        self.flush()

    # ----------------------------------------------------------------
    # Writing
    # ----------------------------------------------------------------

    def record(self, user_text: str, goal: str, meta: Optional[Dict[str, Any]] = None) -> None:
        """Append a new goal entry. Safe from any thread."""
        if not goal:
            return
        entry = {
            "ts":   _now_iso(),
            "raw":  (user_text or "")[:2000],
            "goal": goal,
        }
        if meta:
            try:
                entry["meta"] = dict(meta)
            except Exception:
                entry["meta"] = {"raw": repr(meta)}
        with self._lock:
            self._buffer.append(entry)
            self._all.append(entry)
            if len(self._buffer) >= FLUSH_EVERY:
                self._flush_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if not self.log_path or not self._buffer:
            return
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                for entry in self._buffer:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._buffer.clear()
        except Exception as exc:
            # Logging must never crash the app.
            print(f"[GoalCache] flush failed: {exc!r}")

    # ----------------------------------------------------------------
    # Reading
    # ----------------------------------------------------------------

    def recent(self, n: int = 5) -> List[Dict[str, Any]]:
        """Return the last ``n`` goals (oldest → newest) from this session."""
        with self._lock:
            return list(self._all[-max(0, int(n)):])

    def recent_goal_strings(self, n: int = 5) -> List[str]:
        """Convenience: just the goal strings, oldest → newest."""
        return [e.get("goal", "") for e in self.recent(n) if e.get("goal")]
