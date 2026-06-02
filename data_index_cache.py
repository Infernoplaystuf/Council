"""
data_index_cache.py — pickle-backed sidecar for ``DataIndex._profiles``.

Without this, every app launch re-profiles every CSV / TSV / JSON in
the vault from cold — on a 500-file vault that's 2-5 seconds of
pandas-less but I/O-heavy work, all of it blocking the GUI thread
before the user sees anything.

This module persists the in-memory ``_profiles`` dict to a sidecar
pickle. On next launch, ``DataIndex.__init__`` calls ``try_load`` to
restore it, then ``refresh()`` only re-profiles files whose mtime
moved (the existing delta-skip in refresh already handles that).

Why pickle and not JSON
-----------------------
- Profiles include ``rows: List[Dict[str, str]]`` up to 5000 entries
  per file. JSON re-serialises every string and inflates 3-4× on disk;
  pickle binary form is much smaller and ~5× faster to parse.
- The format is process-local and the file is the user's own. There's
  no untrusted-input attack surface for pickle to worry about here.
- Format-version byte at the start guards against schema drift —
  on mismatch we just refuse to load and the index rebuilds from
  scratch like before.

Cache invalidation
------------------
- mtime stamps inside each FileProfile are checked at refresh time.
- A schema-version mismatch (CACHE_VERSION) drops the whole cache.
- A cache older than 30 days is also dropped so dormant vaults don't
  silently hold stale data forever.
"""

from __future__ import annotations

import os
import pickle
import time
from pathlib import Path
from typing import Any, Dict, Optional


CACHE_FILENAME = "data_index_cache.pickle"
CACHE_VERSION  = 2
MAX_CACHE_AGE_SECONDS = 30 * 86400


def cache_path(vault_dir: Any) -> Path:
    return Path(vault_dir).expanduser().resolve() / CACHE_FILENAME


def try_load(vault_dir: Any) -> Optional[Dict[Path, Any]]:
    """Restore the cached ``{path: FileProfile}`` dict.

    Returns ``None`` on any of:
      - file missing
      - schema-version mismatch
      - cache too old (> MAX_CACHE_AGE_SECONDS)
      - pickle decode error

    The caller treats all of these as a cache miss and continues
    with the existing cold-refresh path. We never let a malformed
    cache file derail the app — silent fallback is better than a
    startup crash on first run after an Anvil upgrade.
    """
    p = cache_path(vault_dir)
    if not p.exists():
        return None
    try:
        age = time.time() - p.stat().st_mtime
        if age > MAX_CACHE_AGE_SECONDS:
            return None
        with open(p, "rb") as fh:
            payload = pickle.load(fh)
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
    out: Dict[Path, Any] = {}
    for path, profile in profiles.items():
        try:
            if Path(path).exists():
                out[Path(path)] = profile
        except Exception:
            continue
    return out


def save(vault_dir: Any, profiles: Dict[Path, Any]) -> bool:
    """Atomically write the cache. Returns True on success."""
    p = cache_path(vault_dir)
    tmp = p.with_suffix(p.suffix + ".tmp")
    payload = {
        "version":  CACHE_VERSION,
        "saved_at": time.time(),
        # Pickle accepts Path keys but normalising to str makes the
        # cache cross-platform-portable and more diff-friendly when
        # the user inspects the file.
        "profiles": {str(k): v for k, v in profiles.items()},
    }
    try:
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "wb") as fh:
            pickle.dump(payload, fh, protocol=pickle.HIGHEST_PROTOCOL)
            fh.flush()
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
