"""
question_history.py — a small per-vault log of the questions asked in the
Council tab, so the user can browse past questions and re-ask one with a
click (cheap + correct now that derived results are reused).

Storage: <vault>/question_history.json — a flat list of {"q", "ts"} in the
order asked. Reads/writes are best-effort and never raise into the UI. The
log de-duplicates an immediately repeated question and is capped so it can't
grow without bound.

Pure + dependency-light (json / pathlib / time), so it's unit-testable with
an explicit timestamp.

TWO LAUNCHES AT ONCE
Both shells write this file (the Qt shell since its conversations are kept).
`add` is read-modify-write, and the thread lock below only covers threads of
ONE process: two launches adding at the same moment each read the old list and
the second write dropped the first's question. So `add` also holds a lock FILE
(question_history.json.lock, created exclusively; one left by a crashed writer
is broken after a few seconds), and each write goes through its own temp file
(a shared name let one writer swap in another's half-written list). A swap that
Windows refuses because a reader has the file open is retried.

AN UNREADABLE FILE IS NOT AN EMPTY ONE
`add` used to treat any read failure as "no history yet" and write a one-item
list over the file — so a read that Windows refused for a moment (another
process swapping the file in, a scanner holding it) wiped every earlier
question. `add` now retries the read, and if the file still cannot be read, or
is not a JSON list, it leaves the file alone and drops only the new question.
"""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

_STORE_NAME = "question_history.json"
_LOCK = threading.Lock()
_CAP = 500
#: A lock file older than this belongs to a writer that died holding it (a
#: live writer holds it for milliseconds).
_LOCK_STALE_S = 5.0
#: How long `add` waits for the lock — longer than the stale age, so a dead
#: writer's lock is broken rather than ignored. Past it, `add` writes anyway:
#: a question kept with a small risk beats a question dropped for certain.
_LOCK_WAIT_S = 8.0


class _Unreadable(Exception):
    """The history file exists but cannot be read as a list right now."""


@contextmanager
def _process_lock(path: Path) -> Iterator[bool]:
    """Hold ``<path>.lock`` (exclusive create) across processes. Yields
    whether it is held; never raises for an ordinary reason."""
    lock = path.with_name(path.name + ".lock")
    deadline = time.monotonic() + _LOCK_WAIT_S
    fd = None
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except (FileExistsError, PermissionError):
            # PermissionError too: on Windows a lock file another process has
            # just deleted is "delete pending" for a moment, and creating it
            # then is ACCESS DENIED, not "exists". Treated as anything but
            # "held by someone", every writer went ahead unlocked.
            try:
                if time.time() - lock.stat().st_mtime > _LOCK_STALE_S:
                    lock.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                break
            time.sleep(0.005)
        except OSError:
            break
    try:
        yield fd is not None
    finally:
        if fd is not None:
            os.close(fd)
            try:
                lock.unlink()
            except OSError:
                pass


def _vault_root(vault_dir: Optional[Any] = None) -> Path:
    if vault_dir is not None:
        return Path(vault_dir).expanduser().resolve()
    env = os.environ.get("COUNCIL_VAULT_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".council" / "vault"


class QuestionHistory:
    """Per-vault store of Council questions (question_history.json)."""

    def __init__(self, vault_dir: Optional[Any] = None) -> None:
        self.path = _vault_root(vault_dir) / _STORE_NAME

    def _read(self) -> List[Dict[str, Any]]:
        try:
            return self._read_strict()
        except Exception:
            return []

    def _read_strict(self, attempts: int = 1) -> List[Dict[str, Any]]:
        """The list, [] when there is no file yet; raises _Unreadable when
        the file is there but cannot be read as a list (after ``attempts``
        tries — Windows refuses a read for a moment while the file is being
        swapped in)."""
        for attempt in range(attempts):
            try:
                text = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return []
            except OSError as exc:
                if attempt == attempts - 1:
                    raise _Unreadable(repr(exc)) from exc
                time.sleep(0.02)
                continue
            try:
                data = json.loads(text)
            except ValueError as exc:
                raise _Unreadable(f"not JSON: {exc}") from exc
            if not isinstance(data, list):
                raise _Unreadable("not a list")
            return data
        return []

    def _write(self, items: List[Dict[str, Any]]) -> None:
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}."
                                  f"{threading.get_ident()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            for attempt in range(50):
                try:
                    tmp.replace(self.path)
                    break
                except PermissionError:
                    # Windows refuses the swap while a reader has the file
                    # open; readers are quick.
                    if attempt == 49:
                        raise
                    time.sleep(0.02)
        except Exception:
            pass
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    def add(self, question: str, ts: Optional[float] = None) -> None:
        """Append a question. Skips blanks and an immediate duplicate of the
        previous entry; caps the log length."""
        q = (question or "").strip()
        if not q:
            return
        with _LOCK, _process_lock(self.path):
            try:
                items = self._read_strict(attempts=25)
            except _Unreadable as exc:
                # Never write over a history we could not read.
                print(f"[question_history] not added — the history file "
                      f"could not be read ({exc}); it was left as it is.")
                return
            if items and str(items[-1].get("q", "")).strip() == q:
                return
            items.append({"q": q, "ts": ts if ts is not None else time.time()})
            if len(items) > _CAP:
                items = items[-_CAP:]
            self._write(items)

    def recent(self, limit: int = 200) -> List[Dict[str, Any]]:
        """Most-recent-first list of {"q", "ts"} (up to ``limit``)."""
        items = self._read()
        items.reverse()
        return items[:max(0, int(limit))]

    def all(self) -> List[Dict[str, Any]]:
        return self._read()

    def clear(self) -> None:
        with _LOCK, _process_lock(self.path):
            self._write([])
