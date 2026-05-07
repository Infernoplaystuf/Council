# ============================================================
# updater.py  —  Notify-only update check
# ============================================================
# Design:
#   • Background HTTPS GET to a manifest URL on app startup
#   • Manifest is small JSON describing the latest version
#   • If newer version found → user sees a non-modal toast
#   • Clicking the toast opens the user's browser to the download page
#   • NEVER auto-downloads, NEVER auto-replaces, NEVER blocks startup
#   • Failure (no network, server down, malformed JSON) is silent —
#     the app must work fully offline
#
# Manifest schema (hosted at branding.UPDATE_MANIFEST_URL):
#   {
#     "latest_version": "1.0.2",
#     "minimum_supported": "1.0.0",
#     "released_at": "2026-06-15",
#     "release_notes_url": "https://datas-inferno.app/changelog/1.0.2",
#     "download_url": "https://datas-inferno.app/download",
#     "platforms": {
#       "windows": "https://.../DatasInferno-1.0.2-win.zip",
#       "macos":   "https://.../DatasInferno-1.0.2-mac.dmg",
#       "linux":   "https://.../DatasInferno-1.0.2-linux.AppImage"
#     }
#   }
#
# A "skipped" version is remembered in vault/.update_skipped so we
# don't pester users about updates they've already declined.
# ============================================================

from __future__ import annotations

import json
import platform
import sys
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import branding


# ============================================================
# Module-level config
# ============================================================
# Override at runtime by setting branding.UPDATE_MANIFEST_URL.
# Empty string disables the updater entirely.
DEFAULT_MANIFEST_URL = ""

CHECK_TIMEOUT_SECONDS = 4
SKIP_FILE = ".update_skipped"


# ============================================================
# Version comparison
# ============================================================

def _parse_version(s: str):
    """Parse '1.2.3' into a tuple of ints. Trailing tags ignored."""
    s = (s or "").strip().lstrip("v").split("-")[0].split("+")[0]
    parts = []
    for chunk in s.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts) if parts else (0,)


def _is_newer(remote: str, local: str) -> bool:
    return _parse_version(remote) > _parse_version(local)


# ============================================================
# Skip-list persistence
# ============================================================

def _skip_path(vault_dir: Path) -> Path:
    return vault_dir / SKIP_FILE


def _read_skipped(vault_dir: Path) -> set:
    p = _skip_path(vault_dir)
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text(encoding="utf-8")).get("versions", []))
    except Exception:
        return set()


def skip_version(vault_dir: Path, version: str) -> None:
    skipped = _read_skipped(vault_dir)
    skipped.add(version)
    try:
        _skip_path(vault_dir).write_text(
            json.dumps({"versions": sorted(skipped),
                        "updated_at": datetime.now(timezone.utc).isoformat()}),
            encoding="utf-8")
    except Exception:
        pass


# ============================================================
# Manifest fetch
# ============================================================

def _manifest_url() -> str:
    """Resolve the manifest URL — branding override > module default."""
    return getattr(branding, "UPDATE_MANIFEST_URL", "") or DEFAULT_MANIFEST_URL


def fetch_manifest(timeout: float = CHECK_TIMEOUT_SECONDS) -> Optional[dict]:
    """
    Fetch + parse the update manifest. Returns None on any failure
    (no network, 404, bad JSON, timeout). Never raises.
    """
    url = _manifest_url()
    if not url:
        return None
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": f"{branding.PRODUCT_NAME}/{branding.VERSION}",
                "Accept":     "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(64 * 1024)   # cap response size at 64KB
        return json.loads(data.decode("utf-8"))
    except Exception:
        return None


# ============================================================
# Decision: should we notify?
# ============================================================

def evaluate(vault_dir: Path, manifest: dict) -> Optional[dict]:
    """
    Decide whether to surface this manifest to the user.
    Returns None if no notification needed, or a dict with the fields the
    UI needs:  {version, release_notes_url, download_url, platform_url}.
    """
    if not isinstance(manifest, dict):
        return None
    latest = str(manifest.get("latest_version", "")).strip()
    if not latest:
        return None
    if not _is_newer(latest, branding.VERSION):
        return None
    if latest in _read_skipped(vault_dir):
        return None

    # Pick the platform-specific download if available
    plat_key = {
        "win32":  "windows",
        "darwin": "macos",
        "linux":  "linux",
    }.get(sys.platform, "")
    plats = manifest.get("platforms", {}) or {}
    platform_url = plats.get(plat_key) if plat_key else None

    return {
        "version":           latest,
        "release_notes_url": str(manifest.get("release_notes_url", "")),
        "download_url":      str(manifest.get("download_url", "")),
        "platform_url":      platform_url,
        "released_at":       str(manifest.get("released_at", "")),
    }


# ============================================================
# Public API: spawn a background check
# ============================================================

def check_async(vault_dir: Path,
                on_update: Callable[[dict], None],
                delay_seconds: float = 2.0) -> None:
    """
    Run the update check in a background thread. `on_update` is invoked
    only if a newer non-skipped version is found. Always silent on failure.

    Caller must arrange for the callback to land on the UI thread (the
    GUI uses tk.after(0, ...) inside its handler).
    """
    if not _manifest_url():
        return  # updater disabled

    def worker():
        # Small delay so we don't compete with onboarding/activation
        try:
            import time
            time.sleep(max(0.0, delay_seconds))
        except Exception:
            pass
        manifest = fetch_manifest()
        if not manifest:
            return
        decision = evaluate(vault_dir, manifest)
        if not decision:
            return
        try:
            on_update(decision)
        except Exception:
            pass

    threading.Thread(target=worker, daemon=True).start()
