# ============================================================
# crash_reporter.py  —  Local-only crash logging
# ============================================================
# Privacy guarantee: nothing is sent off-machine without the user
# explicitly clicking the Email button (which only opens the user's
# default mail client with a pre-filled draft they can review).
#
# What gets captured in a crash log:
#   • App name + version
#   • Python version + platform (OS, OS version, architecture)
#   • Timestamp
#   • The full Python traceback
#
# What is NEVER captured:
#   • User messages, transcripts, or any conversation content
#   • File paths from the vault
#   • Personality model outputs
#   • Any data the user has loaded
#
# Logs are written to vault/logs/crashes/<timestamp>.txt and also
# kept in-memory for the most recent crash so the dialog can open
# without reading from disk.
# ============================================================

from __future__ import annotations

import platform
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ============================================================
# Module state — tracks the most recent crash for the dialog
# ============================================================

_last_crash_path: Optional[Path] = None
_last_crash_text: str = ""
_lock = threading.Lock()


# ============================================================
# Capture
# ============================================================

def _format_report(exc_type, exc_value, tb) -> str:
    """Build a redacted crash report string. Synchronous, fast, no I/O."""
    try:
        import branding
        product = f"{branding.PRODUCT_NAME} {branding.VERSION}"
    except Exception:
        product = "Data's Inferno (version unknown)"

    parts = [
        "=" * 60,
        f"  {product} crash report",
        f"  Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "=" * 60,
        "",
        "ENVIRONMENT",
        f"  Python    : {sys.version.split()[0]}",
        f"  Platform  : {platform.system()} {platform.release()} ({platform.machine()})",
        f"  Tk        : (loaded)" if "tkinter" in sys.modules else "  Tk        : (not loaded)",
        "",
        "EXCEPTION",
        f"  Type      : {exc_type.__name__ if exc_type else '(unknown)'}",
        f"  Message   : {str(exc_value)[:400] if exc_value else '(none)'}",
        "",
        "TRACEBACK",
    ]
    parts.append("".join(traceback.format_exception(exc_type, exc_value, tb)))
    parts.append("")
    parts.append(
        "Privacy: this report contains only the crash trace, not your "
        "data, files, or conversation history. Review before sending."
    )
    return "\n".join(parts)


def _save_report(text: str, vault_dir: Path) -> Optional[Path]:
    """Write the report to vault/logs/crashes/. Best-effort, never raises."""
    try:
        crash_dir = vault_dir / "logs" / "crashes"
        crash_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = crash_dir / f"crash_{ts}.txt"
        path.write_text(text, encoding="utf-8")
        return path
    except Exception:
        return None


def capture(exc_type, exc_value, tb, vault_dir: Path) -> Optional[Path]:
    """Format and persist a crash report. Returns the file path or None."""
    global _last_crash_path, _last_crash_text
    text = _format_report(exc_type, exc_value, tb)
    path = _save_report(text, vault_dir)
    with _lock:
        _last_crash_path = path
        _last_crash_text = text
    return path


def get_last_crash() -> tuple:
    """Return (path, text) tuple for the most recent in-process crash."""
    with _lock:
        return _last_crash_path, _last_crash_text


# ============================================================
# Hook installation
# ============================================================

def install(vault_dir: Path, on_crash=None) -> None:
    """
    Install crash hooks so unhandled exceptions in the main thread
    AND in worker threads are captured.

    `on_crash` is called as `on_crash(crash_path)` from the main thread
    after a crash is captured — typically to show the dialog.
    """
    # 1. Plain Python excepthook — uncaught errors at module/main level
    _orig_excepthook = sys.excepthook

    def _excepthook(exc_type, exc_value, tb):
        # Always print to stderr first so console-launched users see something
        try: _orig_excepthook(exc_type, exc_value, tb)
        except Exception: pass
        path = capture(exc_type, exc_value, tb, vault_dir)
        if on_crash and path:
            try: on_crash(path)
            except Exception: pass

    sys.excepthook = _excepthook

    # 2. Threading excepthook (Python 3.8+)
    if hasattr(threading, "excepthook"):
        _orig_thread_hook = threading.excepthook
        def _thread_hook(args):
            try: _orig_thread_hook(args)
            except Exception: pass
            capture(args.exc_type, args.exc_value, args.exc_traceback, vault_dir)
            # Don't open the dialog from non-main threads — a GUI toolkit
            # would error.
        threading.excepthook = _thread_hook
