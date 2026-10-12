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
#: A lock file older than this belongs to a writer that died holding it.
_LOCK_STALE_S = 10.0
#: How long `add` waits for another process's lock before writing anyway —
#: a question kept with a small risk beats a question dropped for certain.
_LOCK_WAIT_S = 3.0


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
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > _LOCK_STALE_S:
                    lock.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                break
            time.sleep(0.01)
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
            if self.path.exists():
                d = json.loads(self.path.read_text(encoding="utf-8"))
                return d if isinstance(d, list) else []
        except Exception:
            pass
        return []

    def _write(self, items: List[Dict[str, Any]]) -> None:
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}."
                                  f"{threading.get_ident()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            for attempt in range(10):
                try:
                    tmp.replace(self.path)
                    break
                except PermissionError:
                    # Windows refuses the swap while a reader has the file
                    # open; readers are quick.
                    if attempt == 9:
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
            items = self._read()
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
