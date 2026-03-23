# ============================================================
# council_modules.py  —  Shared protocol for tab modules
# ============================================================
# Defines the interface that every tab mixin expects from its host.
#
# Each tab module (tab_video.py, tab_grapher.py, …) is a mixin
# class. When running inside the full council the host is
# CouncilConsole. When running standalone the host is a minimal
# StandaloneHost that fulfils the same interface.
#
# New tabs should only depend on this contract, not on the
# internals of council_gui_engine.py.
# ============================================================

from __future__ import annotations

import json
import queue
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox
from typing import Any, Callable, Dict, Optional


# ============================================================
# Colour palette (shared with grapher_app.py / standalone apps)
# ============================================================

PALETTE = {
    "bg":      "#1e1e2e",
    "surface": "#313244",
    "overlay": "#585b70",
    "text":    "#cdd6f4",
    "subtext": "#a6adc8",
    "blue":    "#89b4fa",
    "green":   "#a6e3a1",
    "red":     "#f38ba8",
    "yellow":  "#f9e2af",
    "mauve":   "#cba6f7",
}


# ============================================================
# StandaloneHost — minimal host used when running a tab on its own
# ============================================================

class StandaloneHost:
    """
    Minimal stand-in for CouncilConsole when a tab module is
    run as its own script.

    Provides:
      • self.vault_dir          — Path to data / analysis store
      • self.ui_q               — asynch queue polled every 80 ms
      • self.root               — Tk root window
      • self.after()            — delegates to root.after()
      • self._make_text()       — dark-themed tk.Text factory
      • self._append_transcript() — stub (copies to clipboard)
      • self._set_text()        — stub
      • personality models      — populated from council_engine
                                   if available; otherwise None

    Subclass this and call _init_models() before building the UI,
    or pass models explicitly via self.writer = ... etc.
    """

    def __init__(
        self,
        vault_dir: Optional[Path] = None,
        title: str = "Council Module",
        geometry: str = "1100x760",
    ):
        self.vault_dir = Path(vault_dir or Path.home() / "council_vault")
        self.vault_dir.mkdir(parents=True, exist_ok=True)

        self.ui_q: queue.Queue = queue.Queue()

        self.root = tk.Tk()
        self.root.title(title)
        self.root.geometry(geometry)
        self._apply_theme()

        # Personality models — set to None; override in _init_models()
        for role in ("writer", "director", "content", "sage", "strategist",
                     "techpriest", "artist", "musician", "peasant", "intern",
                     "eye", "cutter", "algorithm", "coach"):
            setattr(self, role, None)

        self._content_style = None  # optional ContentStyleManager

        # Council-specific stubs (not present standalone)
        self.nb          = None
        self.tab_council = None
        self.input       = None

    # ── Tk delegation ─────────────────────────────────────────

    def after(self, ms: int, fn=None, *args):
        if fn is None:
            return self.root.after(ms)
        return self.root.after(ms, fn, *args)

    def winfo_toplevel(self):
        return self.root

    # ── Theme ─────────────────────────────────────────────────

    def _apply_theme(self):
        self.root.configure(bg=PALETTE["bg"])
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".",
            background=PALETTE["bg"], foreground=PALETTE["text"],
            fieldbackground=PALETTE["surface"],
            selectbackground=PALETTE["overlay"],
            selectforeground=PALETTE["text"],
            bordercolor=PALETTE["overlay"],
            font=("Segoe UI", 10))
        style.configure("TLabelframe",
            background=PALETTE["bg"], foreground=PALETTE["blue"],
            relief="flat", bordercolor=PALETTE["overlay"])
        style.configure("TLabelframe.Label",
            background=PALETTE["bg"], foreground=PALETTE["blue"],
            font=("Segoe UI", 10, "bold"))
        style.configure("TButton",
            background=PALETTE["surface"], foreground=PALETTE["text"],
            borderwidth=1, relief="flat", padding=4)
        style.map("TButton",
            background=[("active", PALETTE["overlay"]),
                        ("pressed", PALETTE["overlay"])])
        style.configure("TCombobox",
            fieldbackground=PALETTE["surface"],
            foreground=PALETTE["text"],
            background=PALETTE["surface"],
            arrowcolor=PALETTE["blue"])
        style.configure("TScrollbar",
            background=PALETTE["surface"],
            troughcolor=PALETTE["bg"],
            arrowcolor=PALETTE["subtext"])
        style.configure("TCheckbutton",
            background=PALETTE["bg"], foreground=PALETTE["text"])
        style.configure("TSeparator", background=PALETTE["overlay"])

    # ── Shared widget factory ─────────────────────────────────

    def _make_text(self, parent, **kw) -> tk.Text:
        defaults = dict(
            bg=PALETTE["bg"], fg=PALETTE["text"],
            insertbackground=PALETTE["text"],
            selectbackground=PALETTE["overlay"],
            relief="flat", bd=0,
            font=("Consolas", 10),
        )
        defaults.update(kw)
        return tk.Text(parent, **defaults)

    # ── Council stubs ─────────────────────────────────────────

    def _append_transcript(self, who: str, text: str, kind: str = "final"):
        """
        Standalone: copy to clipboard and show a brief notice.
        Council: overridden by CouncilConsole to append to transcript.
        """
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            messagebox.showinfo("Copied",
                                 f"Output from {who!r} copied to clipboard.",
                                 parent=self.root)
        except Exception:
            pass

    def _set_text(self, widget, text: str):
        """No-op standalone — widget reference is None."""
        pass

    # ── AI model init ─────────────────────────────────────────

    def _init_models(self, api_key: Optional[str] = None):
        """
        Try to bootstrap a minimal council Writer from council_engine.
        Silently succeeds or silently fails — never raises.
        Override to supply your own models.
        """
        try:
            import council_engine as ce
            reg = ce.BackendRegistry()
            reg.discover()
            if not reg.backends:
                return
            for role in ("writer", "director", "content", "sage",
                         "algorithm", "coach", "cutter", "eye"):
                try:
                    model = ce.PersonalityModel(
                        name          = role,
                        system_prompt = ce.ROLE_PROMPTS.get(role, "You are a helpful assistant."),
                        weights       = {"default": 1.0},
                        registry      = reg,
                        temperature   = 0.35,
                    )
                    setattr(self, role, model)
                except Exception:
                    pass
        except ImportError:
            pass

    # ── Queue poll ────────────────────────────────────────────

    def _poll_queue(self):
        """Base queue poller — subclasses extend this."""
        self.root.after(80, self._poll_queue)

    # ── Run ───────────────────────────────────────────────────

    def run(self):
        self._poll_queue()
        self.root.mainloop()
