"""
council_core.rag_jobs — the vault's semantic index, owned once.

WHAT THIS REPLACES
The Tk engine's `_init_rag_index` (council_gui_engine.py:4700): construct a
VaultRAG if there is none, index it, post a line. It lives on the console class
and is started from two places — startup, and the Agents tab's "Re-index Vault
Now" button — as a bare `threading.Thread`.

TWO DEFECTS DESIGNED OUT
  * THE BUTTON DID NOTHING FOR FIVE MINUTES. `VaultRAG.index()` defaults to
    force=False and returns an EMPTY IndexStats if it indexed in the last 300 s.
    Startup indexes, so pressing Re-index soon after launch did no work and
    reported "Vault indexed: 0 files, 0 chunks" as a success. A user-requested
    reindex is `force=True`; only the periodic path may skip.
  * TWO CLICKS BUILT TWO INDEXES. `if self.rag is None: self.rag = VaultRAG()`
    is check-then-set with no lock, and the button had no re-entrancy guard, so
    two quick clicks could open two chromadb clients on one directory. Here
    construction is under a lock and a second reindex while one runs is refused
    and SAYS so.

INTERACTIVE HOSTS
Under Spyder/IPython, building VaultRAG off the main thread initialises torch
on a worker and the kernel segfaults (council_core.startup.is_interactive_host).
`ensure()` is therefore separate from `reindex()`: a front end calls `ensure()`
on the GUI thread first when `needs_main_thread()` says so, then reindexes on a
worker as usual.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from . import startup


@dataclass
class Outcome:
    ok: bool
    message: str
    files: int = 0
    chunks: int = 0
    backend: str = ""
    skipped: bool = False     # refused because one was already running


class RagIndex:
    """One VaultRAG for one vault, built lazily and indexed on request."""

    def __init__(self, vault_dir: Path, chroma_dir: Optional[Path] = None,
                 factory: Optional[Callable[..., Any]] = None):
        self.vault_dir = Path(vault_dir)
        self.chroma_dir = Path(chroma_dir) if chroma_dir else \
            self.vault_dir / ".chromadb"
        self._factory = factory
        self._rag = None
        self._build_lock = threading.Lock()
        self._index_lock = threading.Lock()

    # -- construction --------------------------------------------------
    @staticmethod
    def needs_main_thread() -> bool:
        return startup.is_interactive_host()

    def ensure(self):
        """The VaultRAG, built on first call. Raises what construction raises."""
        with self._build_lock:
            if self._rag is None:
                factory = self._factory
                if factory is None:
                    import vault_rag
                    factory = vault_rag.VaultRAG
                self._rag = factory(vault_dir=self.vault_dir,
                                    chroma_dir=self.chroma_dir)
            return self._rag

    @property
    def rag(self):
        """The VaultRAG if built, else None. Never builds."""
        return self._rag

    @property
    def busy(self) -> bool:
        return self._index_lock.locked()

    # -- work ------------------------------------------------------------
    def reindex(self, *, force: bool = True) -> Outcome:
        """Index the vault. ``force=True`` is a user asking; it never skips.

        Never raises: the outcome carries the message a log line needs.
        """
        if not self._index_lock.acquire(blocking=False):
            return Outcome(False, "A re-index is already running.",
                           skipped=True)
        try:
            rag = self.ensure()
            stats = rag.index(force=force)
            return Outcome(
                True,
                f"Vault indexed: {stats.total_files} files, "
                f"{stats.total_chunks} chunks ({stats.backend})",
                files=stats.total_files, chunks=stats.total_chunks,
                backend=stats.backend)
        except Exception as exc:                          # noqa: BLE001
            return Outcome(False, f"RAG index error: {exc}")
        finally:
            self._index_lock.release()

    def count(self) -> Optional[int]:
        """Chunks indexed, or None if nothing is built. Can touch the disk
        (the keyword backend walks the vault while dirty) — call off the GUI
        thread."""
        rag = self._rag
        if rag is None:
            return None
        try:
            return int(rag.collection_count())
        except Exception:                                 # noqa: BLE001
            return None


_shared: Dict[Path, RagIndex] = {}
_shared_lock = threading.Lock()


def for_vault(vault_dir: Path) -> RagIndex:
    """The process-wide index for ``vault_dir``.

    Shared so the Agents tab's button and whatever later reads RAG context for
    a turn hold the SAME VaultRAG — two would mean two chromadb clients on one
    directory, which is the race above by another route.
    """
    key = Path(vault_dir).resolve()
    with _shared_lock:
        index = _shared.get(key)
        if index is None:
            index = _shared[key] = RagIndex(key)
        return index
