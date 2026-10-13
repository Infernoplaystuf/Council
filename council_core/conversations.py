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
    line is skipped on reading, as before. The append is atomic between
    PROCESSES too (every launch shares verdict_history.jsonl): on Windows
    open("a") and O_APPEND are seek-then-write and lost records under load,
    so there the file is opened for append-only access (see append_line).
    A writer that still uses open("a") — the Tk console's council.log, and
    any older copy of the app — can still lose a line to that race.
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

    ATOMIC BETWEEN PROCESSES. Every launch appends to verdict_history.jsonl,
    and two launches can share a session file, so two processes append to
    one file at once. On POSIX, O_APPEND makes the seek and the write one
    step. On Windows it does NOT: the C runtime emulates O_APPEND as
    "seek to the end, then write", and between the two another process's
    record lands and is written over — measured, four processes x 3000
    records lost 1459 and left 28 torn lines (so did the Tk shell's
    open("a")). On Windows the file is opened through CreateFileW with
    FILE_APPEND_DATA as its only data access: every write then goes to the
    end of the file as the file system writes it, never over another's.

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
    if _WIN_APPEND is not None:
        _WIN_APPEND(str(path), data)
        return
    fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT | _O_BINARY,
                 0o644)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)


def _windows_appender() -> Optional[Callable[[str, bytes], None]]:
    """append_line's writer on Windows (see there), or None elsewhere."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel32.CreateFileW
        create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                           wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        write = kernel32.WriteFile
        write.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        write.restype = wintypes.BOOL
        close = kernel32.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
    except Exception:                                     # noqa: BLE001
        return None
    file_append_data, synchronize = 0x0004, 0x00100000
    share_all = 0x1 | 0x2 | 0x4            # read, write, delete
    open_always, attribute_normal = 4, 0x80
    invalid = wintypes.HANDLE(-1).value

    def append(path: str, data: bytes) -> None:
        handle = create(path, file_append_data | synchronize, share_all,
                        None, open_always, attribute_normal, None)
        if handle is None or handle == invalid:
            err = ctypes.get_last_error()
            raise OSError(None, ctypes.FormatError(err).strip(), path, err)
        try:
            view = memoryview(data)
            while view:
                chunk = view[:1 << 30]
                buf = (ctypes.c_char * len(chunk)).from_buffer_copy(chunk)
                done = wintypes.DWORD(0)
                if not write(handle, buf, len(chunk), ctypes.byref(done),
                             None):
                    err = ctypes.get_last_error()
                    raise OSError(None, ctypes.FormatError(err).strip(),
                                  path, err)
                view = view[done.value:]
        finally:
            close(handle)

    return append


_WIN_APPEND = _windows_appender()


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
        #: Why the folder could not be created, or "". Not raised: a vault
        #: that cannot keep conversations (read-only, permission denied)
        #: must still open — the Qt Council tab is built eagerly, and this
        #: raising in its constructor stopped the whole app from starting
        #: (found in review). A write raises then, to a caller that says so.
        self.problem = ""
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.problem = (f"the folder {self.base_dir} could not be "
                            f"created ({exc.__class__.__name__}: "
                            f"{exc.strerror or exc})")
        # (session_id, n) -> (the file's (size, mtime) when read, the turns).
        # Keyed on the FILE: a store whose cache only its own append() could
        # invalidate served stale history to every other store on the folder
        # (see the module note).
        self._cache: Dict[tuple, Tuple[tuple, List[Dict[str, Any]]]] = {}
        self._lock = threading.Lock()

    def _ensure_dir(self) -> None:
        """Before a write: the folder, made now if it could not be at start
        (raises when it still cannot)."""
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.problem = ""

    def _stem(self, session_id: str) -> str:
        """The file stem a session id names.

        A session the list shows is reached by its OWN stem when that file
        exists: list_sessions() lists every *.jsonl by its stem, and the
        store used to reach one through safe_name() only — so 'pump notes'
        (renamed in Explorer) loaded the different session 'pump_notes',
        and 'older - Copy' was listed and then "not found" (found in
        review). Anything else — every id the app makes, and a new name —
        is safe_name()'d as always, so writes are unchanged. A stem with a
        path in it is never followed out of the folder."""
        sid = session_id or ""
        if (sid and sid not in (".", "..") and not sid.startswith(".")
                and not any(ch in sid for ch in "/\\:")
                and (self.base_dir / f"{sid}.jsonl").is_file()):
            return sid
        return safe_name(sid, 64)

    def session_path(self, session_id: str) -> Path:
        return self.base_dir / f"{self._stem(session_id)}.jsonl"

    def stored_id(self, session_id: str) -> str:
        """The id the session is listed under — its file's stem (a judge's
        name with letters a file name cannot hold is stored safe_name()'d)."""
        return self._stem(session_id)

    def claim(self, session_id: str) -> bool:
        """Create the session's file if no one has. True when this call made
        it — then no other launch is writing that session."""
        self._ensure_dir()
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
        self._ensure_dir()
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
                record = json.loads(ln)
            except Exception:
                continue
            # Only records: a line holding null, a list, a string or a
            # number (a hand-edited or foreign file) made every reader's
            # t.get() raise, and with it every turn that had the file
            # loaded as prior (found in review).
            if isinstance(record, dict):
                out.append(record)
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
        return self.base_dir / f"{self._stem(session_id)}.summary.md"

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
        return self.base_dir / f"{self._stem(session_id)}.active"

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
            who = t.get("who") or "?"
            text = str(t.get("text") or "")       # null or a number: as text
            # Truncate very long turns
            if len(text) > 400:
                text = text[:400] + "…"
            lines.append(f"{who}: {text}")
        return "\n".join(lines)


def claim_session_id(store: "ConversationStore", log_dir: Optional[Path],
                     now: Optional[datetime] = None) -> Tuple[str, str]:
    """(session id, problem) — a launch's id in the Tk format, CLAIMED.

    Unique among sessions AND debug logs on disk: the debug log's file
    (conversation_logs/session_<id>.jsonl) is created here, exclusively, so
    two launches in the same second do not share an id — the second gets
    "_2". (The session's own file is claimed on its first record, so a
    launch nobody asks anything leaves no empty session in the list.)

    BOTH shells call this. Only the Qt shell did: the Tk console took
    datetime.now() and its logger buffers, so nothing of a Tk launch was on
    disk yet, and a Qt launch in the same second took the same id — the two
    wrote into one session file, and a Tk question reached the Qt Writer's
    RECENT CONVERSATION under "This conversation only" (found in review).

    ``problem`` is "" or why the log file could not be made (the id is
    still returned, unclaimed)."""
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    for n in range(1, 1000):
        candidate = stamp if n == 1 else f"{stamp}_{n}"
        if store.session_path(candidate).exists():
            continue
        if log_dir is None:
            return candidate, ""
        log_path = Path(log_dir) / f"session_{candidate}.jsonl"
        try:
            fd = os.open(str(log_path),
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY,
                         0o644)
        except FileExistsError:
            continue
        except OSError as exc:
            return candidate, (f"the conversation log could not be "
                               f"started: {exc!r}")
        os.close(fd)
        return candidate, ""
    return f"{stamp}_{os.getpid()}", ""


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
    plus the turn's verdict id, so a response can find ITS line later.

    The Judge's confidence is a percentage since council-map (council_core
    .confidence): it is kept as ``confidence_pct``, which the Sessions list
    reads (sessions.record_confidence_pct), and as the 0–10 ``confidence``
    the file has always held — the Tk shell reads that one from the same
    file."""
    events = list(getattr(result, "events", None) or [])
    critique = getattr(result, "critique", "") or ""
    try:
        pct = max(0, min(100, int(round(float(
            getattr(result, "confidence", 0) or 0)))))
    except (TypeError, ValueError):
        pct = 0
    return {
        "ts": now_iso(),
        "session_id": session_id,
        "route": getattr(result, "route", "") or "",
        "query": (question or "")[:120],
        "confidence": int(round(pct / 10)),
        "confidence_pct": pct,
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
        #: Why this conversation is not being saved, or "" while it is. A
        #: conversation that is not saved is one a follow-up cannot see (the
        #: personalities read the file), so the shell must say so — not only
        #: print it (see `not_saved_message`).
        self.unsaved = ""
        self.store = ConversationStore(self.vault_dir / "conversations")
        self._lock = threading.RLock()
        self._listeners: List[Callable[..., None]] = []
        self._claimed = False
        self._closed = False
        self.named = False
        self.logger = None
        if self.store.problem:
            self._problem(f"this conversation is not being saved: "
                          f"{self.store.problem}")
            self._not_saved(self.store.problem)
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
        """The Tk format, unique among sessions AND logs on disk — see
        claim_session_id. The debug log is started under the claimed id."""
        log_dir = self.logger.log_dir if self.logger is not None else None
        candidate, problem = claim_session_id(self.store, log_dir, now=now)
        if problem:
            self._problem(problem)
        elif self.logger is not None:
            self.logger.start_session(candidate)
        return candidate

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
        "prior_cleared", "renamed", "not_saved" (a save failed — once),
        "saving" (a save worked again). Called on the caller's thread."""
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

    def not_saved_message(self) -> str:
        """What the user is told while this conversation is not saved."""
        return (f"This conversation is not being saved ({self.unsaved}), so "
                f"follow-up questions will not see your earlier questions "
                f"and it will not be in the Sessions list.")

    def _not_saved(self, reason: str) -> None:
        """A save failed. Listeners hear it once ("not_saved"), not on every
        entry; "saving" when a later save works again."""
        first = not self.unsaved
        self.unsaved = reason or "the save failed"
        if first:
            self._emit("not_saved", message=self.not_saved_message())

    def _saved(self) -> None:
        if self.unsaved:
            self.unsaved = ""
            self._emit("saving", message="This conversation is being saved "
                                         "again from here on.")

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
    def record(self, who: str, text: str, kind: str = "final", *,
               logged: bool = False, **extra: Any) -> bool:
        """One transcript entry, kept the way the Tk shell keeps it
        (_append_transcript): the logs take it (see `log`) unless ``logged``
        says they already have; the conversation takes everything but
        tokens, phases and thoughts; a question also goes into History.
        ``extra`` keys are added to the conversation record (provenance, for
        instance). True if stored."""
        from . import transcript as transcript_core
        if not logged:
            self.log(who, text, kind)
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
            self._not_saved(f"{exc.__class__.__name__}: "
                            f"{getattr(exc, 'strerror', None) or exc}")
            return False
        self._saved()
        return True

    def log(self, who: str, text: str, kind: str = "final") -> None:
        """The logs only, as an entry is said — never read by a model:

          conversation_logs/session_<id>.jsonl  the debug log (everything but
                                                tokens), under the id current
                                                when it was said
          logs/council.log                      the Librarian's line log of
                                                every entry the conversation
                                                would keep, as the Tk shell
                                                writes it (Qt wrote none —
                                                found in review)

        Alone, for what is worth keeping for the user's own debugging but is
        not part of the conversation: a deliberation's drafts,
        cross-examination and rankings, and lines that quote other
        sessions."""
        from . import transcript as transcript_core
        try:
            if self.logger is not None and transcript_core.logs_to_session(kind):
                self.logger.log_event(kind=kind, who=who, text=text,
                                      meta={"session": self.session_id})
        except Exception as exc:                          # noqa: BLE001
            self._problem(f"the debug log did not take an entry: {exc!r}")
        if transcript_core.stores_in_history(kind):
            self._line_log(who, text)

    def _line_log(self, who: str, text: str) -> None:
        """One line in logs/council.log — council_engine.Librarian.log_event's
        format ("[ts] who: text") and bytes: it appends in text mode, so on
        Windows every newline is written CRLF. One atomic append."""
        try:
            path = self.vault_dir / "logs" / "council.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            line = f"[{now_iso()}] {who}: {text}".rstrip() + "\n"
            if os.linesep != "\n":
                line = line.replace("\n", os.linesep)
            append_line(path, line.encode("utf-8"))
        except Exception as exc:                          # noqa: BLE001
            self._problem(f"council.log did not take an entry: {exc!r}")

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
            # By FILE, not by spelling: two ids can name one file (see
            # ConversationStore._stem), and this conversation must never be
            # loaded as its own prior (its turns would go in twice).
            if self._same_file(session_id, self.session_id):
                return Outcome(False, "That is this conversation — it is "
                                      "already part of every answer.")
            if not self.store.session_path(session_id).is_file():
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

    def _same_file(self, a: str, b: str) -> bool:
        return (os.path.normcase(str(self.store.session_path(a)))
                == os.path.normcase(str(self.store.session_path(b))))

    def check_prior(self) -> bool:
        """Before a turn: is the loaded prior still saved? False when it
        was not — then it is dropped and listeners are told why.

        Another launch can rename the session after it was loaded here (a
        session is named after its first answer), and the prior then came
        back empty with nothing said (found in review). True when nothing
        is loaded or the file is there."""
        with self._lock:
            old = self.prior_session_id
            if not old or self.store.session_path(old).is_file():
                return True
            self.prior_session_id = None
        message = (f"The earlier conversation '{old}' is no longer saved "
                   f"under that name (it was renamed or removed), so it is "
                   f"not used. Load it again from the Sessions tab if you "
                   f"want it.")
        self._problem(f"the loaded prior {old} is gone")
        self._emit("prior_cleared", session_id=old, message=message)
        return False

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
                        # The id the file is stored under: a judge's name
                        # keeps letters a file name does not ('größe' is
                        # stored 'gr_e'), and an id that is not the file's
                        # stem lost the Sessions list's "(this
                        # conversation)" mark and could be loaded as its
                        # own prior (found in review).
                        self.session_id = self.store.stored_id(candidate)
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
