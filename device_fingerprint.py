# ============================================================
# device_fingerprint.py  —  Stable per-machine identifier
# ============================================================
# We hash a small set of stable system attributes so the server can
# tell "this is the same machine that activated before" from "this is
# a new machine".
#
# Goals:
#   • Stable across reboots and minor OS updates
#   • Stable across reinstalls of Data's Inferno itself
#   • Changes when the machine genuinely changes (different hardware,
#     different user account, fresh OS install on different drive)
#   • Privacy-respecting — we hash; we never send raw values
#
# We deliberately do NOT use:
#   • MAC addresses (change with NIC swaps, easy to spoof)
#   • Disk serials (require admin/root on some platforms)
#   • Anything from /proc that requires root
#
# What we do use:
#   • platform.node()   — machine hostname
#   • platform.system() — "Windows"/"Linux"/"Darwin"
#   • platform.machine() — architecture (x86_64/arm64)
#   • Username (getlogin or USER env)
#   • Home directory path  — stable across reboots, varies per user
#
# Cached at vault/.fingerprint so even cosmetic changes (renaming the
# machine) don't invalidate an existing activation. Cache wins over
# re-computation when present.
# ============================================================

from __future__ import annotations

import hashlib
import os
import platform
from pathlib import Path
from typing import Optional


_CACHE_FILENAME = ".fingerprint"


def _gather_attributes() -> str:
    """Concatenate a stable set of attributes into one canonical string."""
    parts = [
        platform.node()    or "",          # machine hostname
        platform.system()  or "",          # OS family
        platform.machine() or "",          # architecture
        platform.release() or "",          # OS major version (Win 10/11)
        _username(),
        str(Path.home()),
    ]
    # Lowercase + strip to absorb minor casing/whitespace drift
    return "|".join(p.strip().lower() for p in parts)


def _username() -> str:
    # Prefer environment variables since getlogin can fail in some
    # environments (e.g. running as a service with no controlling tty).
    return (os.environ.get("USERNAME")     # Windows
            or os.environ.get("USER")      # Unix
            or os.environ.get("LOGNAME")
            or "unknown")


def compute(vault_dir: Optional[Path] = None) -> str:
    """
    Return the device fingerprint as a hex SHA-256 string.

    If `vault_dir` is given, the computed fingerprint is cached at
    vault/.fingerprint so subsequent calls return the same value
    even if a cosmetic system attribute has changed (e.g. user
    renamed the PC).
    """
    if vault_dir:
        cached = _read_cache(vault_dir)
        if cached:
            return cached

    raw = _gather_attributes().encode("utf-8")
    fp = hashlib.sha256(raw).hexdigest()

    if vault_dir:
        _write_cache(vault_dir, fp)
    return fp


def reset(vault_dir: Path) -> None:
    """Drop the cached fingerprint — used when the user explicitly migrates."""
    p = vault_dir / _CACHE_FILENAME
    try:
        if p.exists():
            p.unlink()
    except Exception:
        pass


# ---- Cache I/O (best-effort, never raises) -----------------------------------

def _read_cache(vault_dir: Path) -> Optional[str]:
    p = vault_dir / _CACHE_FILENAME
    if not p.exists():
        return None
    try:
        s = p.read_text(encoding="utf-8").strip()
        # Be defensive — only accept what looks like a sha256
        return s if (len(s) == 64 and all(c in "0123456789abcdef" for c in s)) else None
    except Exception:
        return None


def _write_cache(vault_dir: Path, fingerprint: str) -> None:
    try:
        vault_dir.mkdir(parents=True, exist_ok=True)
        (vault_dir / _CACHE_FILENAME).write_text(fingerprint, encoding="utf-8")
    except Exception:
        pass
