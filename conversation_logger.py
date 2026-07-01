"""
Conversation logger — writes a per-session JSONL log of every visible
transcript event to vault/conversation_logs/ for the *human user* to
inspect later.

CRITICAL CONSTRAINT
-------------------
The model must NEVER read these logs. They exist purely for the user's
own debugging. Multiple layers of defense enforce this:

  1. `PROTECTED_SUBDIRS` lists vault subfolders that the model is
     forbidden to see. `is_protected_path()` is the single check
     consulted from every other module that walks the vault.
  2. vault_index.rebuild() skips any path under a protected subdir.
  3. vault_analyst.list_csv_files / list_excel_files / list_data_files
     all filter the protected subdirs out.
  4. council_gui_engine._extract_file_paths refuses to inject files
     whose path lies under a protected subdir, even if the user pastes
     one literally.
  5. council_gui_engine._read_file_for_injection re-checks before reading.

If a future feature ever walks the vault, add the same guard there.
The pattern is one line:

    if conversation_logger.is_protected_path(p, vault_dir):
        continue
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


# ============================================================
# Protection registry
# ============================================================

PROTECTED_SUBDIRS: tuple = (
    "conversation_logs",
    # Past Q&A from earlier sessions. Was readable by query_history_search
    # (a chat intent) but ALSO indexed by vault_index — that meant the
    # model could see its own old answers as "context" in a new session,
    # leading to cross-machine value hallucinations when an older session
    # mentioned data that doesn't exist on the current machine.
    # query_history_search bypasses vault_index (reads conversations/
    # directly), so excluding the folder here doesn't break that feature.
    "conversations",
)

# Lowercased once at import for the per-path membership test in
# is_protected_path — that ran on every (path, folder) pair during vault-wide
# walks and rebuilt this set each time.
_PROTECTED_SUBDIRS_LC = frozenset(s.lower() for s in PROTECTED_SUBDIRS)


def is_protected_path(path: Any, vault_dir: Any) -> bool:
    """True if `path` is inside one of the protected vault subdirs.

    Works on absolute paths, relative paths, and Path objects. Returns
    False on any error rather than raising — callers use this as a
    filter and never want it to crash a vault walk.
    """
    try:
        vd = Path(vault_dir).expanduser().resolve()
        p  = Path(path).expanduser().resolve()
    except Exception:
        return False
    try:
        rel = p.relative_to(vd)
    except ValueError:
        # The path isn't under vault_dir at all — not "protected" by us
        # (it's just outside our scope).
        return False
    parts = rel.parts
    return bool(parts) and parts[0].lower() in _PROTECTED_SUBDIRS_LC


# ============================================================
# Logger
# ============================================================

LOG_SUBDIR = "conversation_logs"


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


class ConversationLogger:
    """Per-session, append-only JSONL log under vault/conversation_logs/.

    The buffer flushes every 50 events, every periodic tick from the
    GUI, and on session end / app close. Thread-safe — the GUI can
    log from worker threads.
    """

    def __init__(self, vault_dir: Any):
        self.vault_dir = Path(vault_dir).expanduser().resolve()
        self.log_dir = self.vault_dir / LOG_SUBDIR
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.session_id: Optional[str] = None
        self.log_path: Optional[Path] = None
        self._buffer: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------

    def start_session(self, session_id: Optional[str] = None) -> Path:
        """Open a new log file for the given session_id (defaults to now)."""
        if not session_id:
            session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_id = session_id
        self.log_path = self.log_dir / f"session_{session_id}.jsonl"
        self.log_event(
            kind="session_start", who="system",
            text=f"Session {session_id} started",
        )
        return self.log_path

    def end_session(self, reason: str = "normal") -> None:
        """Write a session-end marker and flush remaining events."""
        if not self.session_id:
            return
        self.log_event(
            kind="session_end", who="system",
            text=f"Session ended: {reason}",
        )
        self.flush()

    # -- writing ------------------------------------------------------

    def log_event(
        self,
        kind: str,
        who: str,
        text: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        event = {
            "ts":   _now_iso(),
            "kind": str(kind),
            "who":  str(who),
            "text": str(text),
        }
        if meta:
            try:
                event["meta"] = dict(meta)
            except Exception:
                event["meta"] = {"raw": repr(meta)}
        with self._lock:
            self._buffer.append(event)
            if len(self._buffer) >= 50:
                self._flush_locked()

    def flush(self) -> None:
        """Public flush — drains the buffer to disk."""
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if not self.log_path or not self._buffer:
            return
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                for event in self._buffer:
                    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._buffer.clear()
        except Exception as exc:
            # Never raise from logging — print so the user sees the failure
            # but the app keeps running.
            print(f"[ConversationLogger] flush failed: {exc!r}")
