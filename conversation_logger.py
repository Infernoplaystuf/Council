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
    # The knowledge graph's own store and exports (council_core/
    # knowledge_graph.py). Its JSON/CSV exports quote the user's documents,
    # so a vault search must not find them as if they were documents.
    ".knowledge_graph",
    # The app's own logs: logs/council.log is the Librarian's log of EVERY
    # transcript line of every session (the same content as
    # conversation_logs/), and logs/crashes/ holds crash reports. Indexed as
    # a .log document, an older session's words came back as a "vault
    # match" — the way past conversations must not reach a prompt under
    # "This conversation only" (council_core.memory_scope). Only the vault's
    # own top-level logs/ is meant: a "logs" folder inside data_in/ is the
    # user's and stays searchable.
    "logs",
    # Learned memory: role and project memory and the compiled user profile
    # (council_engine.RoleMemoryManager writes memory/*.md), and the Sage's
    # knowledge notes. Indexed as documents, a vault search put them back
    # into a prompt — past "This conversation only", and past SILO in Tk.
    # (The Sage itself still reads its own knowledge base, through
    # council_core.vault_context.sage_block — what the user taught it in the
    # Agents tab — in the Qt scopes "conversation" and "all", never in
    # "none"; a vault search never returns it to any role.)
    "memory",
    "sage_knowledge",
    # The model-call meter (council_core.usage_log) and the weekly placement
    # reviews (council_core.placement): the app's bookkeeping, not documents.
    ".council_usage",
    # How the council decided earlier questions (council_core.past_decisions).
    ".council_memory",
    # The council benchmark's runs (council_core.council_bench): measurements
    # of the app, not the user's documents.
    ".council_bench",
    # Code projects the council works on (council_core.project): briefs,
    # job notes and git worktrees — whole copies of code, not documents.
    ".council_projects",
    # The user's coding-agent profiles (council_core.agent_profiles).
    ".council_agents",
)

# Lowercased once at import for the per-path membership test in
# is_protected_path — that ran on every (path, folder) pair during vault-wide
# walks and rebuilt this set each time.
_PROTECTED_SUBDIRS_LC = frozenset(s.lower() for s in PROTECTED_SUBDIRS)

# App-STATE / bookkeeping files various subsystems write to the vault ROOT
# (past questions, job records, indices, caches, settings, connection lists).
# They hold the app's OWN data, NOT the user's — so a vault CONTENT search must
# never index or return them. Guarded here by BASENAME so the protection holds
# no matter where a search was rooted. Without this, "search the vault for
# bacon" hit question_history.json / agent_jobs.json (which contain the term
# because it was asked/ran before) instead of the user's real files.
PROTECTED_STATE_FILES: tuple = (
    "question_history.json", "agent_jobs.json", "deferred_tasks.json",
    "derived_results.json", "collections.json",
    "image_index.json", "vault_index.json", "vault_embeddings.json",
    "fuzzy_denylist.json", "semantic_cache.json", "backend_settings.json",
    "sql_connections.json", "mongo_connections.json",
    "data_index_cache.pickle", ".vault_col_index.json",
    "data_index_cache.json.gz",
    ".agent_runs.jsonl", ".tool_gaps.jsonl", ".failures.jsonl",
    ".user_quirks.jsonl", ".tool_proposals.jsonl",
    ".onboarded", ".council_python",
    # Agent profiles: settings, profiles and the council's tool requests
    # (council_core/agent_profiles.py) — app state, and it quotes tool code.
    "agent_profiles.json",
    # Older conversations, in the app's own words: every judged question
    # (verdict_history.jsonl), every question the vault could not answer
    # (vault_rag_misses.txt), the Librarian's gap list quoting them. A
    # "search the vault for X" put both of the first two into the prompt
    # (measured) — an older session's question, past "This conversation
    # only" (council_core.memory_scope).
    "verdict_history.jsonl", "vault_rag_misses.txt", "librarian_wishlist.md",
    # The app's own settings at the vault root: the Personal Specialists, the
    # council instructions and content style, the model pins, the node list
    # and the Grapher presets. A vault search returned specialists.json as a
    # "source" for questions about sales (docs/qt_migration/
    # remaining_scope_2026-10-06.md, the source-chips row).
    "specialists.json", "council_instructions.json", "content_style.json",
    "personality_backends.json", "node_registry.json", "graph_presets.json",
    # The model slots and the PC check's benchmark (council_core.model_slots,
    # council_core.pc_check) — the last two of council_core.vault_setup
    # .VAULT_ROOT_STATE this list did not name. "search the vault for X"
    # put both into the prompt when they held the word (measured).
    "model_slots.json", "model_bench.json",
    # Which role answers on which machine (council_core.node_routing) and
    # the Quant Duel's settings (council_core.quant_duel): app settings.
    "node_routing.json", "quant_duel.json",
)

#: App files protected only at the vault ROOT — a name too ordinary to
#: protect everywhere (a user's own trends.md in data_in/ is their data).
#: trends.md is the cross-session trend analysis of all role memory.
PROTECTED_ROOT_FILES: tuple = ("trends.md",)
_PROTECTED_ROOT_FILES_LC = frozenset(s.lower() for s in PROTECTED_ROOT_FILES)
_PROTECTED_STATE_FILES_LC = frozenset(s.lower() for s in PROTECTED_STATE_FILES)

#: The README the app writes in data_in/ and data_out/
#: (data_index.init_data_dirs) — protected while it IS the app's text: a
#: vault search returned both as matches and chips (found in review). Not
#: every README.txt (a user's data_in/jobs/README.txt is theirs), and not one
#: the user has rewritten (then it is theirs too).
APP_README_FOLDERS: tuple = ("data_in", "data_out")
APP_README_NAME = "readme.txt"


def _is_app_readme(path: Path) -> bool:
    """True when ``path`` holds the app's own README text, word for word."""
    try:
        import data_index
        with open(path, "rb") as fh:
            data = fh.read(8192)
        text = data.decode("utf-8").replace("\r\n", "\n").strip()
    except Exception:
        return False
    return text in (data_index._README_INPUT.strip(),
                    data_index._README_OUTPUT.strip())


def is_protected_path(path: Any, vault_dir: Any) -> bool:
    """True if `path` is a protected vault subdir member OR an app-state file.

    Works on absolute paths, relative paths, and Path objects. Returns
    False on any error rather than raising — callers use this as a
    filter and never want it to crash a vault walk.
    """
    try:
        p_raw = Path(path).expanduser()
    except Exception:
        return False
    # App-state files are protected by BASENAME, independent of how the walk
    # was rooted. A vault-WIDE search roots at the vault itself, which makes
    # the vault_dir-relative subdir check below resolve against the vault's
    # PARENT and silently stop matching — basename is the robust guard.
    if p_raw.name.lower() in _PROTECTED_STATE_FILES_LC:
        return True
    try:
        vd = Path(vault_dir).expanduser().resolve()
        p  = p_raw.resolve()
    except Exception:
        return False
    try:
        rel = p.relative_to(vd)
    except ValueError:
        # The path isn't under vault_dir at all — not "protected" by us
        # (it's just outside our scope).
        return False
    parts = rel.parts
    if len(parts) == 1 and parts[0].lower() in _PROTECTED_ROOT_FILES_LC:
        return True
    if (len(parts) == 2 and parts[0].lower() in APP_README_FOLDERS
            and parts[1].lower() == APP_README_NAME and _is_app_readme(p)):
        return True
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
