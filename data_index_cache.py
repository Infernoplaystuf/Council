"""
data_index_cache.py — warm-start sidecar for ``DataIndex._profiles``.

Without this, every app launch re-profiles every CSV / TSV / JSON in
the vault from cold — on a 500-file vault that's 2-5 seconds of
pandas-less but I/O-heavy work, all of it blocking the GUI thread
before the user sees anything.

This module persists the in-memory profiles (as plain dicts —
DataIndex converts them) to a sidecar file. On next launch,
``DataIndex.__init__`` calls ``try_load`` to restore them, then
``refresh()`` only re-profiles files whose mtime moved (the existing
delta-skip in refresh already handles that).

Gzip-compressed JSON, never pickle
----------------------------------
It was a pickle, written to and loaded from data_in/ — the folder the
user copies other people's files into — and unpickling runs code. The
profiles are plain data (names, counts, sample rows of strings), so JSON
holds them; gzip keeps the file small (the sample rows are most of it).
DataIndex now keeps it in <data_out>/.index/, not in data_in/. An older
build's data_index_cache.pickle is never read (and never deleted).

Cache invalidation
------------------
- mtime stamps inside each profile are checked at refresh time.
- A schema-version mismatch (CACHE_VERSION) drops the whole cache.
- A cache older than 30 days is also dropped so dormant vaults don't
  silently hold stale data forever.
- A file that cannot be read or parsed is a cold start, never an error.
"""

from __future__ import annotations

import gzip
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional


CACHE_FILENAME = "data_index_cache.json.gz"
#: What older builds wrote (a pickle in data_in/). Never read: listed so the
#: rest of the app can tell it is not the user's file.
OLD_CACHE_FILENAME = "data_index_cache.pickle"
CACHE_VERSION  = 3
MAX_CACHE_AGE_SECONDS = 30 * 86400


def cache_path(cache_dir: Any) -> Path:
    return Path(cache_dir).expanduser().resolve() / CACHE_FILENAME


def try_load(cache_dir: Any) -> Optional[Dict[str, Dict[str, Any]]]:
    """The cached ``{path: profile dict}``, or ``None`` when there is no
    usable cache: the file is missing, older than MAX_CACHE_AGE_SECONDS,
    of another CACHE_VERSION, or cannot be read or parsed.

    The caller treats all of these as a cache miss and continues
    with the existing cold-refresh path. We never let a malformed
    cache file derail the app — silent fallback is better than a
    startup crash on first run after an upgrade.
    """
    p = cache_path(cache_dir)
    if not p.exists():
        return None
    try:
        age = time.time() - p.stat().st_mtime
        if age > MAX_CACHE_AGE_SECONDS:
            return None
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception as exc:
        print(f"[data_index_cache] load failed: {exc!r}")
        return None
    if (not isinstance(payload, dict)
            or payload.get("version") != CACHE_VERSION):
        return None
    profiles = payload.get("profiles")
    if not isinstance(profiles, dict):
        return None
    # Drop entries whose source file vanished between sessions
    out: Dict[str, Dict[str, Any]] = {}
    for path, profile in profiles.items():
        try:
            if isinstance(profile, dict) and Path(path).exists():
                out[str(path)] = profile
        except Exception:
            continue
    return out


def save(cache_dir: Any, profiles: Dict[str, Dict[str, Any]]) -> bool:
    """Atomically write the cache (temp file + os.replace). Returns True
    on success. ``profiles``: path -> a JSON-able dict."""
    p = cache_path(cache_dir)
    tmp = p.with_suffix(p.suffix + ".tmp")
    payload = {
        "version":  CACHE_VERSION,
        "saved_at": time.time(),
        "profiles": {str(k): v for k, v in profiles.items()},
    }
    try:
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=1) as fh:
            json.dump(payload, fh, ensure_ascii=False, default=str)
        with open(tmp, "rb+") as fh:
            try:
                os.fsync(fh.fileno())
            except Exception:
                pass
        os.replace(tmp, p)
        return True
    except Exception as exc:
        print(f"[data_index_cache] save failed: {exc!r}")
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return False
