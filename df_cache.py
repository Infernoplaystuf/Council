"""
df_cache.py — process-wide LRU cache for parsed pandas DataFrames.

The analyst stack calls ``pd.read_csv(path)`` from many helpers in
sequence. A single user turn that asks "how many rows + average
revenue + outliers" can re-read the same 50 MB CSV three times.

This module wraps reads in a memory-bounded LRU cache keyed on
``(path, mtime)``. The next caller in the same turn gets the cached
DataFrame; a file modified between reads invalidates its entry
naturally because the mtime moves.

Memory budget
-------------
Default 256 MB total estimated bytes. Configurable via the
``COUNCIL_DF_CACHE_MB`` env var. When the cache exceeds budget,
oldest entries are evicted until it fits.

DataFrame size estimation uses ``memory_usage(deep=True).sum()``.
That's accurate for object/string-heavy frames; numeric frames are
estimated from dtype × shape.

Concurrency
-----------
``threading.RLock``. The cache is read-mostly — multiple helper
calls in one turn don't usually overlap (the GUI calls them
sequentially from one worker thread), but the lock keeps a
late-arriving call from another thread (e.g. a background flush)
from corrupting the OrderedDict during eviction.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple


# ============================================================
# Configuration
# ============================================================

DEFAULT_BUDGET_MB = 256


def _budget_bytes() -> int:
    """Read the budget from env at call time so test harnesses can
    override per-test without touching module state."""
    raw = os.environ.get("COUNCIL_DF_CACHE_MB", "")
    try:
        mb = int(raw) if raw else DEFAULT_BUDGET_MB
    except ValueError:
        mb = DEFAULT_BUDGET_MB
    return max(1, mb) * 1024 * 1024


# ============================================================
# Cache class
# ============================================================

class DataFrameCache:
    """Memory-bounded LRU of ``(path, mtime) → (DataFrame, est_bytes)``.

    Designed for stdlib + pandas only; we don't pull in cachetools
    or another caching library to keep the dependency surface flat.
    """

    def __init__(self, max_bytes: Optional[int] = None):
        self._max_bytes = max_bytes if max_bytes is not None else _budget_bytes()
        self._store: "OrderedDict[Tuple[str, float, str], Tuple[Any, int]]" = OrderedDict()
        self._total_bytes = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._lock = threading.RLock()

    # ----------------------------------------------------------------
    # Key computation
    # ----------------------------------------------------------------

    @staticmethod
    def _key(path: Any, extra: str = "") -> Optional[Tuple[str, float, str]]:
        """Build a cache key from path + mtime + an optional discriminator
        (e.g. read-options digest so head-only reads don't collide with
        full reads).

        Returns None if the file doesn't exist — the caller should
        treat that as a cache miss and let the loader raise the real
        error.
        """
        try:
            p = Path(path).expanduser().resolve()
            mtime = p.stat().st_mtime
        except OSError:
            return None
        return (str(p), mtime, extra)

    # ----------------------------------------------------------------
    # Bytes accounting
    # ----------------------------------------------------------------

    @staticmethod
    def _estimate_bytes(df: Any) -> int:
        """Best-effort size estimate. Pandas ``memory_usage(deep=True)``
        is the most accurate measure for object-typed columns; for the
        rare non-DataFrame value we fall back to a generous guess."""
        try:
            import pandas as pd
            if isinstance(df, pd.DataFrame):
                return int(df.memory_usage(deep=True, index=True).sum())
        except Exception:
            pass
        # Generic fallback — try sys.getsizeof
        import sys
        try:
            return max(sys.getsizeof(df), 1024)
        except Exception:
            return 1024

    # ----------------------------------------------------------------
    # Public API
    # ----------------------------------------------------------------

    def get(self, path: Any, extra: str = "") -> Optional[Any]:
        """Return the cached DataFrame for ``path`` (and discriminator),
        or None on miss. LRU-touches the entry on hit."""
        key = self._key(path, extra)
        if key is None:
            return None
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            self._store.move_to_end(key)
            self._hits += 1
            return entry[0]

    def put(self, path: Any, value: Any, extra: str = "") -> None:
        """Cache ``value`` (a DataFrame) for ``path``. Evicts oldest
        entries until the total fits the budget."""
        key = self._key(path, extra)
        if key is None:
            return
        est = self._estimate_bytes(value)
        # Refuse to cache a single entry larger than the whole budget
        # — it would immediately blow everything else out.
        if est > self._max_bytes:
            return
        with self._lock:
            existing = self._store.pop(key, None)
            if existing is not None:
                self._total_bytes -= existing[1]
            self._store[key] = (value, est)
            self._total_bytes += est
            self._evict_locked()

    def get_or_load(
        self,
        path: Any,
        loader: Callable[[Any], Any],
        *,
        extra: str = "",
    ) -> Any:
        """Cache-aware load. ``loader(path)`` is called only on miss."""
        hit = self.get(path, extra)
        if hit is not None:
            return hit
        value = loader(path)
        self.put(path, value, extra)
        return value

    def invalidate(self, path: Any) -> int:
        """Drop every cache entry for ``path`` (across all extras).
        Returns the number of entries removed."""
        try:
            target = str(Path(path).expanduser().resolve())
        except Exception:
            return 0
        removed = 0
        with self._lock:
            for key in list(self._store.keys()):
                if key[0] == target:
                    _val, est = self._store.pop(key)
                    self._total_bytes -= est
                    removed += 1
        return removed

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._total_bytes = 0

    def _evict_locked(self) -> None:
        while self._total_bytes > self._max_bytes and self._store:
            _key, (_val, est) = self._store.popitem(last=False)
            self._total_bytes -= est
            self._evictions += 1

    # ----------------------------------------------------------------
    # Diagnostics
    # ----------------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            total_q = self._hits + self._misses
            hit_rate = (self._hits / total_q) if total_q else 0.0
            return {
                "n_entries":  len(self._store),
                "bytes":      self._total_bytes,
                "budget":     self._max_bytes,
                "hits":       self._hits,
                "misses":     self._misses,
                "evictions":  self._evictions,
                "hit_rate":   round(hit_rate, 3),
            }


# ============================================================
# Module-level singleton + convenience wrappers
# ============================================================

# The analyst stack imports this and calls ``cached_read_csv`` from
# its inventory helpers. One shared cache means cross-helper hits
# inside a single user turn.
GLOBAL = DataFrameCache()


def cached_read_csv(path: Any, **read_kw) -> Any:
    """Drop-in replacement for ``pd.read_csv(path, **kw)`` that
    consults the shared cache.

    The read-options are hashed into the cache key so a ``nrows=5``
    head-read doesn't collide with a full read of the same file.
    """
    import pandas as pd
    extra = _hash_kwargs(read_kw)
    def _load(p):
        return pd.read_csv(p, **read_kw)
    return GLOBAL.get_or_load(path, _load, extra=extra)


def cached_read_excel(path: Any, **read_kw) -> Any:
    """Same idea for ``pd.read_excel``."""
    import pandas as pd
    extra = _hash_kwargs(read_kw)
    def _load(p):
        return pd.read_excel(p, **read_kw)
    return GLOBAL.get_or_load(path, _load, extra=extra)


def _hash_kwargs(kw: Dict[str, Any]) -> str:
    """Build a stable digest of read kwargs for cache-key discrimination.

    Cheap and deterministic — sorted ``repr`` is good enough; the
    space of distinct read kwargs we care about is small (nrows,
    usecols, dtype, sep, encoding...).
    """
    if not kw:
        return ""
    try:
        items = sorted((str(k), repr(v)) for k, v in kw.items())
    except Exception:
        return repr(kw)
    return "|".join(f"{k}={v}" for k, v in items)
