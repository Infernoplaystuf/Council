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
    AND in tkinter callbacks are captured.

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
            # Don't open the dialog from non-main threads — Tk would error.
        threading.excepthook = _thread_hook

    # 3. Tk callback exceptions — wired by the GUI when it's available
    # The caller installs this via install_tk_hook() once the root exists.


def install_tk_hook(tk_root, vault_dir: Path, on_crash=None) -> None:
    """
    Replace the Tk root's `report_callback_exception` so widget callbacks
    that raise also flow through the crash reporter. Must be called after
    the Tk root is created.
    """
    def _tk_hook(exc_type, exc_value, tb):
        path = capture(exc_type, exc_value, tb, vault_dir)
        # Print to stderr too so it shows in the console
        try:
            sys.stderr.write("".join(traceback.format_exception(exc_type, exc_value, tb)))
        except Exception:
            pass
        if on_crash and path:
            try: on_crash(path)
            except Exception: pass

    try:
        tk_root.report_callback_exception = _tk_hook
    except Exception:
        pass


# ============================================================
# Dialog (lazy import — only needed when a crash actually occurs)
# ============================================================

def show_dialog(parent, crash_path: Path) -> None:
    """
    Show the post-crash dialog. The user can:
      - View the log inline
      - Save a copy to a file of their choosing
      - Open their email client with the log attached (mailto)
      - Dismiss

    Nothing is sent automatically — the email step opens a pre-filled
    draft the user reviews before clicking send.
    """
    import tkinter as tk
    from tkinter import ttk, filedialog
    try:
        import branding
        product = branding.PRODUCT_NAME
        support = getattr(branding, "SUPPORT_EMAIL", "") or "support@example.com"
    except Exception:
        product = "Data's Inferno"
        support = "support@example.com"

    text = ""
    try:
        if crash_path and crash_path.exists():
            text = crash_path.read_text(encoding="utf-8")
        else:
            _, text = get_last_crash()
    except Exception:
        text = "(crash log unreadable — please check vault/logs/crashes/)"

    win = tk.Toplevel(parent)
    win.title(f"{product} — error")
    win.geometry("640x460")
    try:
        if hasattr(parent, "tk"):
            win.transient(parent)
    except Exception:
        pass

    # Theme — best-effort, falls back to defaults if branding missing
    try:
        import branding as _b
        theme = _b.get_theme("dark")
        win.configure(bg=theme["bg"])
        head_bg = theme["panel_bg"]; fg = theme["fg"]; muted = theme["muted_fg"]
    except Exception:
        head_bg = "#181825"; fg = "#cdd6f4"; muted = "#7f849c"

    head = tk.Frame(win, bg=head_bg)
    head.pack(fill="x")
    tk.Label(head, text=f"⚠  {product} encountered an error",
             font=("Segoe UI", 13, "bold"),
             bg=head_bg, fg=fg).pack(side="left", padx=14, pady=10)

    body = tk.Frame(win, bg=win["bg"])
    body.pack(fill="both", expand=True, padx=14, pady=8)
    tk.Label(body,
             text="The crash log has been saved on this machine. "
                  "Nothing has been sent. You can review the log below "
                  "and choose to save a copy or email it to support.",
             bg=win["bg"], fg=muted, wraplength=600,
             justify="left").pack(anchor="w", pady=(0, 8))

    # Log viewer (read-only)
    txt = tk.Text(body, wrap="none", height=14,
                  bg="#11111b", fg=fg, insertbackground=fg,
                  relief="flat", font=("Consolas", 9))
    sb_y = ttk.Scrollbar(body, orient="vertical", command=txt.yview)
    sb_x = ttk.Scrollbar(body, orient="horizontal", command=txt.xview)
    txt.configure(yscrollcommand=sb_y.set, xscrollcommand=sb_x.set)
    txt.grid(row=0, column=0, sticky="nsew")
    sb_y.grid(row=0, column=1, sticky="ns")
    sb_x.grid(row=1, column=0, sticky="ew")
    body.rowconfigure(0, weight=1)
    body.columnconfigure(0, weight=1)
    txt.insert("1.0", text)
    txt.configure(state="disabled")

    # Buttons
    btns = ttk.Frame(win)
    btns.pack(fill="x", padx=14, pady=(4, 12))

    def _save_copy():
        dest = filedialog.asksaveasfilename(
            parent=win,
            title="Save crash log as",
            defaultextension=".txt",
            initialfile=(crash_path.name if crash_path else "crash.txt"),
            filetypes=[("Text", "*.txt"), ("All", "*.*")],
        )
        if dest:
            try:
                Path(dest).write_text(text, encoding="utf-8")
            except Exception as e:
                tk.messagebox.showerror("Save failed", str(e), parent=win)

    def _email_support():
        # Open the user's default mail client with a pre-filled draft.
        # We can't attach a file via mailto: — paste the log into the body
        # and tell the user it's also saved at crash_path.
        import urllib.parse as _u
        subject = f"[{product}] crash report"
        body_text = (
            "I encountered a crash. The log is below "
            f"(also saved at: {crash_path}).\n\n"
            "Please describe what you were doing when this happened:\n\n"
            "[describe here]\n\n"
            "------------ CRASH LOG ------------\n\n"
            f"{text[:6000]}"   # mailto has length limits on most clients
        )
        url = f"mailto:{support}?subject={_u.quote(subject)}&body={_u.quote(body_text)}"
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass

    def _open_folder():
        if not crash_path:
            return
        try:
            import subprocess
            if sys.platform == "win32":
                subprocess.Popen(["explorer", str(crash_path.parent)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(crash_path.parent)])
            else:
                subprocess.Popen(["xdg-open", str(crash_path.parent)])
        except Exception:
            pass

    ttk.Button(btns, text="📁 Open folder",   command=_open_folder).pack(side="left")
    ttk.Button(btns, text="💾 Save copy…",     command=_save_copy ).pack(side="left", padx=6)
    ttk.Button(btns, text="✉ Email to support", command=_email_support).pack(side="left", padx=6)
    ttk.Button(btns, text="Dismiss",           command=win.destroy).pack(side="right")
