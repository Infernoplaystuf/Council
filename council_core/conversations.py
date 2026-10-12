"""
council_core.conversations — the conversation record both shells write.

ONE LIST OF SESSIONS
A session is `<vault>/conversations/<session id>.jsonl`, one JSON object per
line ({"ts", "who", "text"}, sometimes more keys). The Tk shell has written
these since the start; the Qt shell wrote nothing (it imported a module,
`convo_store`, that does not exist, so its History and Sessions list were
empty and no Qt conversation was ever kept). Both shells now write the SAME
files in the SAME place through `ConversationStore`, so Tk's sessions and
Qt's are one list.

`ConversationStore` is the engine's class, MOVED here so a reader (the
Sessions tab, History) does not have to load the model engine to list a
folder; `council_engine.ConversationStore` is this class. Three things
changed on the way, all invisible to the Tk shell's callers:

  * Its read cache is keyed on the FILE, not on the instance's own appends.
    The engine builds its own store for the personalities and the Tk console
    another for writing, so the personalities' cache never heard of an
    append: with SILO off, turn 2's prompt was served turn 1's history from
    the cache. (Latent in Tk because SILO blanks history there.)
  * A record is appended with ONE write of one whole line (`append_line`), so
    a crash leaves the line whole or absent, never a merged half — and a torn
    line is skipped on reading, as before.
  * `claim()` creates a session file only if it does not exist, which is how a
    Qt launch makes sure no other launch is writing the same session.

The other files a turn leaves, all in the formats the Tk shell writes:
  conversation_logs/session_<id>.jsonl   the debug log (conversation_logger;
                                         the model never reads it)
  question_history.json                  History (question_history.py)
  verdict_history.jsonl                  one line per judged deliberation

`Conversation` is one launch's conversation in the Qt shell: a session id,
the memory scope (council_core.memory_scope), the prior session the user
loaded, and the writing. Toolkit-free.
"""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import memory_scope

#: Windows needs O_BINARY or the C runtime rewrites every "\n" as "\r\n".
_O_BINARY = getattr(os, "O_BINARY", 0)

#: A session id as a launch first gets it: the Tk format, plus "_<n>" when
#: another launch took that second first. Only such an id is renamed when the
#: session gets a name (as Tk's _rename_session does for its own).
RAW_SESSION_ID = re.compile(r"^\d{8}_\d{6}(?:_\d+)?$")

#: How many other names a session tries when its own is taken.
_NAME_TRIES = 9


def now_iso() -> str:
    """council_engine.now_iso — the timestamp the records carry."""
    return datetime.now().isoformat(timespec="seconds")


def safe_name(s: str, maxlen: int = 128) -> str:
    """council_engine.safe_name, the same rule (a test holds them equal):
    the session id as a file name."""
    return (re.sub(r"[^A-Za-z0-9._-]+", "_", s.strip())[:maxlen] or "item")


def append_line(path: Path, data: bytes) -> None:
    """Append ``data`` (one whole line) with a single write.

    Opened per call: nothing is held open between turns, so another program
    reading the file (the Tk shell's preview, a backup) never meets a lock.

    A file that does not end in a newline holds a line torn by a crash; the
    record starts on a line of its own, so the fragment stays one unreadable
    line (skipped on reading) instead of swallowing this record too.
    """
    try:
        with open(path, "rb") as existing:
            existing.seek(0, os.SEEK_END)
            if existing.tell():
                existing.seek(-1, os.SEEK_END)
                if existing.read(1) != b"\n":
                    data = b"\n" + data
    except FileNotFoundError:
        pass
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT | _O_BINARY,
                 0o644)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to a temp file beside ``path`` and swap it in."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}."
                         f"{threading.get_ident()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# ============================================================
# The store (moved from council_engine)
# ============================================================

class ConversationStore:
    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        # (session_id, n) -> (the file's (size, mtime) when read, the turns).
        # Keyed on the FILE: a store whose cache only its own append() could
        # invalidate served stale history to every other store on the folder
        # (see the module note).
        self._cache: Dict[tuple, Tuple[tuple, List[Dict[str, Any]]]] = {}
        self._lock = threading.Lock()

    def session_path(self, session_id: str) -> Path:
        return self.base_dir / f"{safe_name(session_id, 64)}.jsonl"

    def claim(self, session_id: str) -> bool:
        """Create the session's file if no one has. True when this call made
        it — then no other launch is writing that session."""
        try:
            fd = os.open(str(self.session_path(session_id)),
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY,
                         0o644)
        except FileExistsError:
            return False
        os.close(fd)
        return True

    def append(self, session_id: str, record: Dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock:
            append_line(self.session_path(session_id), line.encode("utf-8"))
            for key in [k for k in self._cache if k[0] == session_id]:
                del self._cache[key]

    @staticmethod
    def _signature(path: Path) -> Optional[tuple]:
        try:
            st = path.stat()
        except OSError:
            return None
        return (st.st_size, st.st_mtime_ns)

    def load_last(self, session_id: str, n: int = 12) -> List[Dict[str, Any]]:
        p = self.session_path(session_id)
        signature = self._signature(p)
        if signature is None:
            return []
        cache_key = (session_id, n)
        with self._lock:
            cached = self._cache.get(cache_key)
        if cached is not None and cached[0] == signature:
            return cached[1]
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        out: List[Dict[str, Any]] = []
        for ln in lines[-n:]:
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
        with self._lock:
            self._cache[cache_key] = (signature, out)
        return out

    def list_sessions(self) -> List[str]:
        """Return session IDs sorted newest-first."""
        files = sorted(self.base_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [p.stem for p in files]

    def rename_session(self, old_id: str, new_id: str) -> bool:
        """
        Rename a session file from old_id to new_id.
        Returns True on success. No-ops if old doesn't exist or new already exists.
        """
        old_p = self.session_path(old_id)
        new_p = self.session_path(new_id)
        if not old_p.exists() or new_p.exists():
            return False
        old_p.rename(new_p)
        with self._lock:
            for key in [k for k in self._cache if k[0] == old_id]:
                del self._cache[key]
        return True

    def summary_path(self, session_id: str) -> Path:
        """Path for the generated prose summary of a completed session."""
        return self.base_dir / f"{safe_name(session_id, 64)}.summary.md"

    def save_session_summary(self, session_id: str, summary_text: str) -> None:
        """Persist a generated prose summary for a completed session."""
        _atomic_write_text(self.summary_path(session_id),
                           summary_text.strip() + "\n")

    def load_generated_summary(self, session_id: str) -> str:
        """Load a previously generated prose summary, or '' if none exists."""
        p = self.summary_path(session_id)
        if not p.exists():
            return ""
        return p.read_text(encoding="utf-8", errors="replace").strip()

    # ── Crash-recovery sentinel ────────────────────────────────
    # A session writes an `.active` sentinel when it starts deliberating,
    # and removes it on clean shutdown. On startup, any remaining sentinel
    # indicates a crash mid-deliberation — the orphaned session can be
    # offered for resume.

    def _active_sentinel(self, session_id: str) -> Path:
        return self.base_dir / f"{safe_name(session_id, 64)}.active"

    def mark_session_active(self, session_id: str, query: str = "") -> None:
        """Mark the session as in-flight. Safe to call repeatedly."""
        p = self._active_sentinel(session_id)
        try:
            p.write_text(json.dumps({
                "session_id": session_id,
                "started_at": now_iso(),
                "query":      query[:200],
            }), encoding="utf-8")
        except Exception:
            pass  # never let a sentinel write block deliberation

    def mark_session_done(self, session_id: str) -> None:
        """Remove the in-flight sentinel after a clean deliberation."""
        p = self._active_sentinel(session_id)
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass

    def find_orphaned_sessions(self) -> List[Dict[str, Any]]:
        """
        Return metadata for any sessions that have an `.active` sentinel
        without a matching clean shutdown. Used at startup to offer resume.
        """
        orphans: List[Dict[str, Any]] = []
        for p in self.base_dir.glob("*.active"):
            try:
                meta = json.loads(p.read_text(encoding="utf-8"))
                # Only surface orphans whose .jsonl actually exists
                if self.session_path(meta.get("session_id", "")).exists():
                    orphans.append(meta)
            except Exception:
                # Malformed sentinel — best-effort cleanup
                try: p.unlink()
                except Exception: pass
        # Newest first
        orphans.sort(key=lambda m: m.get("started_at", ""), reverse=True)
        return orphans

    def clear_orphan(self, session_id: str) -> None:
        """Discard an orphan's sentinel (user chose not to resume)."""
        self.mark_session_done(session_id)

    def load_session_summary(self, session_id: str, max_turns: int = 6) -> str:
        """
        Return a condensed text summary of a past session suitable for
        injecting into role context, keeping token count bounded.

        Prefers a generated prose summary (saved by save_session_summary)
        over the raw turn fallback, since generated summaries are more
        compressed and more useful as prior context.
        """
        generated = self.load_generated_summary(session_id)
        if generated:
            return f"[Prior session summary: {session_id}]\n{generated}"

        # Fallback: build a crude summary from the last N raw turns
        turns = self.load_last(session_id, n=max_turns)
        if not turns:
            return ""
        lines = [f"[Past session: {session_id}]"]
        for t in turns:
            who = t.get("who", "?")
            text = t.get("text", "")
            # Truncate very long turns
            if len(text) > 400:
                text = text[:400] + "…"
            lines.append(f"{who}: {text}")
        return "\n".join(lines)


# ============================================================
# Session names
# ============================================================

_STOPWORDS = frozenset("""
a an and are be can could did do does for from give how i in is it me my of
on or please show tell that the this to was what when where which who why
with would you your
""".split())


def name_from_question(question: str) -> str:
    """A session name from the first question: up to five of its words.

    The default build has one Writer and no judge to ask, and loading a second
    model to name a file would cost more than the answer did — so the name is
    made from the words, the way the judge is asked to ("describe WHAT was
    asked"). snake_case, as the judge's names are."""
    words = re.findall(r"[a-z0-9]+", (question or "").lower())
    kept = [w for w in words if w not in _STOPWORDS] or words
    name = "_".join(kept[:5])[:60].strip("_")
    return name or "council_session"


def clean_name(raw: str) -> str:
    """council_engine.JudgeModel.name_session's cleaning, for a name from any
    source: word characters only, snake_case, at most 60."""
    clean = re.sub(r"[^\w\s]", "", (raw or "").strip().lower())
    clean = re.sub(r"\s+", "_", clean.strip())
    return clean[:60].strip("_")


# ============================================================
# Verdicts
# ============================================================

def verdict_record(result: Any, session_id: str, question: str) -> Dict[str, Any]:
    """The line verdict_history.jsonl gets for a judged turn — the Tk
    shell's record (ts, session_id, route, query, confidence, passed, rounds)
    plus the turn's verdict id, so a response can find ITS line later."""
    events = list(getattr(result, "events", None) or [])
    critique = getattr(result, "critique", "") or ""
    return {
        "ts": now_iso(),
        "session_id": session_id,
        "route": getattr(result, "route", "") or "",
        "query": (question or "")[:120],
        "confidence": int(getattr(result, "confidence", 0) or 0),
        "passed": "Verdict: PASS" in critique,
        "rounds": sum(1 for e in events
                      if getattr(e, "who", "") == "Judge"
                      and getattr(e, "kind", "") == "observation"
                      and "Verdict:" in (getattr(e, "text", "") or "")),
        "verdict_id": getattr(result, "verdict_id", "") or "",
    }


# ============================================================
# One launch's conversation
# ============================================================

@dataclass(frozen=True)
class Snapshot:
    """What a turn needs to know about the conversation, taken once."""
    session_id: str
    prior_session_id: Optional[str]
    scope: str


@dataclass
class Outcome:
    ok: bool
    message: str
    session_id: Optional[str] = None


class Conversation:
    """This launch's conversation, written as the Tk shell writes one.

    Thread-safe: the GUI thread records; a worker takes a `snapshot()`.
    Nothing here raises into a caller for an ordinary reason — a conversation
    that cannot be saved must not stop the answer from being shown. Problems
    are kept in `problems` (and printed) instead.

    ``scope`` defaults to the Qt shell's (memory_scope.for_qt); ``session_id``
    to a fresh one for this launch.
    """

    def __init__(self, vault_dir: Any, *, scope: str = "",
                 session_id: str = "") -> None:
        self.vault_dir = Path(vault_dir)
        self.scope = scope or memory_scope.for_qt()
        self.session_id = session_id
        self.prior_session_id: Optional[str] = None
        self.problems: List[str] = []
        self.store = ConversationStore(self.vault_dir / "conversations")
        self._lock = threading.RLock()
        self._listeners: List[Callable[..., None]] = []
        self._claimed = False
        self._closed = False
        self.named = False
        self.logger = None
        try:
            import conversation_logger
            self.logger = conversation_logger.ConversationLogger(
                self.vault_dir)
        except Exception as exc:                          # noqa: BLE001
            self._problem(f"the conversation log is unavailable: {exc!r}")
        if not self.session_id:
            self.session_id = self._new_session_id()
        elif self.logger is not None:
            self.logger.start_session(self.session_id)

    # -- the id ---------------------------------------------------------
    def _new_session_id(self, now: Optional[datetime] = None) -> str:
        """The Tk format, unique among sessions AND logs on disk.

        The debug log's file is created here, exclusively: it is the launch's
        claim on the id, so two launches in the same second do not share a
        log, and the second gets "_2". (The session's own file is claimed on
        its first record, so a launch nobody asks anything leaves no empty
        session in the list.)"""
        stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
        for n in range(1, 1000):
            candidate = stamp if n == 1 else f"{stamp}_{n}"
            if self.store.session_path(candidate).exists():
                continue
            if self.logger is None:
                return candidate
            log_path = self.logger.log_dir / f"session_{candidate}.jsonl"
            try:
                fd = os.open(str(log_path),
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY,
                             0o644)
            except FileExistsError:
                continue
            except OSError as exc:
                self._problem(f"the conversation log could not be "
                              f"started: {exc!r}")
                return candidate
            os.close(fd)
            self.logger.start_session(candidate)
            return candidate
        return f"{stamp}_{os.getpid()}"

    def _claim(self) -> None:
        """Make the session's file ours before the first record.

        If another launch wrote that id first (the Tk shell does not check),
        this launch moves to a fresh id rather than writing into its file."""
        if self._claimed:
            return
        for _ in range(20):
            if self.store.claim(self.session_id):
                self._claimed = True
                return
            old = self.session_id
            self.session_id = self._new_session_id()
            self._problem(f"session {old} was already taken by another "
                          f"launch; this one is {self.session_id}")
        self._claimed = True          # give up claiming; still write

    # -- listeners ------------------------------------------------------
    def subscribe(self, listener: Callable[..., None]) -> None:
        """``listener(event, **details)`` after a change: "prior_loaded",
        "prior_cleared", "renamed". Called on the caller's thread."""
        self._listeners.append(listener)

    def _emit(self, event: str, **details: Any) -> None:
        for listener in list(self._listeners):
            try:
                listener(event, **details)
            except Exception as exc:                      # noqa: BLE001
                self._problem(f"a listener failed on {event}: {exc!r}")

    def _problem(self, text: str) -> None:
        self.problems.append(text)
        print(f"[conversation] {text}", flush=True)

    # -- reading --------------------------------------------------------
    def snapshot(self) -> Snapshot:
        with self._lock:
            return Snapshot(self.session_id, self.prior_session_id, self.scope)

    def turns(self, n: int = 200) -> List[Dict[str, Any]]:
        """This session's last ``n`` records."""
        try:
            return self.store.load_last(self.session_id, n=n)
        except Exception as exc:                          # noqa: BLE001
            self._problem(f"could not read this session: {exc!r}")
            return []

    def apply_to(self, models: Any, snapshot: Optional[Snapshot] = None) -> None:
        """Point every personality at this session, its loaded prior and its
        scope. Called by the turn, on its worker, before any model call."""
        snap = snapshot or self.snapshot()
        for model in personalities_of(models):
            if not hasattr(model, "session_id"):
                continue                     # a stand-in, not a personality
            model.session_id = snap.session_id
            model.prior_session_id = snap.prior_session_id
            if hasattr(model, "memory_scope"):
                model.memory_scope = snap.scope

    # -- writing --------------------------------------------------------
    def record(self, who: str, text: str, kind: str = "final",
               **extra: Any) -> bool:
        """One transcript entry, kept the way the Tk shell keeps it
        (_append_transcript): the debug log takes everything but tokens; the
        conversation takes everything but tokens, phases and thoughts; a
        question also goes into History. ``extra`` keys are added to the
        conversation record (provenance, for instance). True if stored."""
        from . import transcript as transcript_core
        try:
            if self.logger is not None and transcript_core.logs_to_session(kind):
                self.logger.log_event(kind=kind, who=who, text=text,
                                      meta={"session": self.session_id})
        except Exception as exc:                          # noqa: BLE001
            self._problem(f"the debug log did not take an entry: {exc!r}")
        if not transcript_core.stores_in_history(kind):
            return False
        if who == "User":
            self.note_question(text)
        record = {"ts": now_iso(), "who": who, "text": text}
        record.update({k: v for k, v in extra.items() if v is not None})
        try:
            with self._lock:
                self._claim()
                self.store.append(self.session_id, record)
        except Exception as exc:                          # noqa: BLE001
            self._problem(f"a turn was not saved: {exc!r}")
            return False
        return True

    def note_question(self, text: str) -> None:
        """question_history.json, which History reads — Tk's too."""
        try:
            import question_history
            question_history.QuestionHistory(self.vault_dir).add(text)
        except Exception as exc:                          # noqa: BLE001
            self._problem(f"the question was not added to History: {exc!r}")

    def record_verdict(self, record: Dict[str, Any]) -> bool:
        """One line in verdict_history.jsonl, as the Tk shell appends it."""
        try:
            path = self.vault_dir / "verdict_history.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            append_line(path, (json.dumps(record, ensure_ascii=False)
                               + "\n").encode("utf-8"))
        except Exception as exc:                          # noqa: BLE001
            self._problem(f"the verdict was not recorded: {exc!r}")
            return False
        return True

    def flush(self) -> None:
        """Push the debug log's buffer to disk (after each turn)."""
        if self.logger is not None:
            try:
                self.logger.flush()
            except Exception as exc:                      # noqa: BLE001
                self._problem(f"the debug log did not flush: {exc!r}")

    def close(self, reason: str = "user_close") -> None:
        """End the debug log with its session_end marker. Once."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        if self.logger is not None:
            from . import shutdown
            failure = shutdown.end_conversation_log(self.logger, reason)
            if failure:
                self._problem(failure)

    # -- the prior ------------------------------------------------------
    def load_prior(self, session_id: str) -> Outcome:
        """Bring ONE older session into this conversation, on the user's say.

        The only way an older session reaches a prompt in "This conversation
        only": the personalities get it as PRIOR SESSION CONTEXT from the next
        question on (council_engine.PersonalityModel.respond), and listeners
        say so in the transcript."""
        session_id = (session_id or "").strip()
        if not session_id:
            return Outcome(False, "Select a session to load.")
        with self._lock:
            if session_id == self.session_id:
                return Outcome(False, "That is this conversation — it is "
                                      "already part of every answer.")
            if not self.store.session_path(session_id).exists():
                return Outcome(False, f"There is no saved session called "
                                      f"'{session_id}'.")
            self.prior_session_id = session_id
        count = len(self.store.load_last(session_id, n=100000))
        if self.scope == memory_scope.NONE:
            message = (f"Loaded the earlier conversation '{session_id}', but "
                       f"memory is off (COUNCIL_MEMORY_SCOPE / "
                       f"COUNCIL_DEMO_SILO), so the council will not use it.")
        else:
            message = (f"Loaded the earlier conversation '{session_id}' "
                       f"({count} saved entries) into this one. From the next "
                       f"question on, the council uses it as background.")
        self._emit("prior_loaded", session_id=session_id, message=message)
        return Outcome(True, message, session_id)

    def clear_prior(self) -> Outcome:
        with self._lock:
            old, self.prior_session_id = self.prior_session_id, None
        if not old:
            return Outcome(True, "No earlier conversation is loaded.")
        message = (f"The earlier conversation '{old}' is no longer used "
                   f"from the next question on.")
        self._emit("prior_cleared", session_id=old, message=message)
        return Outcome(True, message, old)

    # -- the name -------------------------------------------------------
    def rename(self, name: str) -> Outcome:
        """Give this session a name, once — as the Tk shell does, only while
        its id is still the launch's timestamp. Never over another session:
        a taken name gets "_2", "_3"…, and with all of those taken the
        session keeps its timestamp."""
        base = clean_name(name)
        if not base:
            return Outcome(False, "No name was given.")
        with self._lock:
            old = self.session_id
            if self.named or not RAW_SESSION_ID.match(old):
                return Outcome(False, "This session already has a name.")
            if not self._claimed:
                return Outcome(False, "Nothing has been saved to name yet.")
            for n in range(1, _NAME_TRIES + 2):
                candidate = base if n == 1 else f"{base}_{n}"
                if RAW_SESSION_ID.match(candidate):
                    continue          # a name that looks like an id is not one
                try:
                    if self.store.rename_session(old, candidate):
                        self.session_id = candidate
                        self.named = True
                        break
                except OSError as exc:
                    self._problem(f"the session could not be named: {exc!r}")
                    return Outcome(False, f"Could not name the session: {exc}")
            else:
                self.named = True     # every name taken: keep the timestamp
                return Outcome(False, f"'{base}' and its variants are taken; "
                                      f"the session stays {old}.")
        self._emit("renamed", old=old, new=self.session_id)
        return Outcome(True, f"This conversation is saved as "
                             f"'{self.session_id}'.", self.session_id)


def personalities_of(models: Any) -> List[Any]:
    """Every model object a turn may call: the personalities dict when there
    is one (council_turn.Personalities), else the role attributes."""
    found: List[Any] = []
    table = getattr(models, "personalities", None)
    if isinstance(table, dict):
        found.extend(m for m in table.values() if m is not None)
    for role in ("judge", "writer", "peasant", "intern", "coder", "artist",
                 "skeptic", "sage", "strategist", "librarian", "content",
                 "director", "algorithm", "coach"):
        model = getattr(models, role, None)
        if model is not None and all(model is not m for m in found):
            found.append(model)
    return found
