# ============================================================
# tab_grapher.py  —  Grapher tab mixin for CouncilConsole
# ============================================================
# Thin wrapper that embeds grapher_app.GrapherApp as a council tab.
#
# Usage as a mixin:
#   class CouncilConsole(GrapherTabMixin, ...):
#       def build_ui(self):
#           self._build_grapher_tab()
#
# Standalone (just launches grapher_app directly):
#   python tab_grapher.py [--vault PATH] [--no-ai] [--title TEXT]
# ============================================================

from __future__ import annotations

from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import ttk

from council_modules import PALETTE

try:
    from grapher_app import GrapherApp
    _GRAPHER_APP_OK = True
except ImportError:
    GrapherApp      = None
    _GRAPHER_APP_OK = False


# ============================================================
# GrapherTabMixin
# ============================================================

class GrapherTabMixin:
    """
    Mixin that adds a Grapher tab to CouncilConsole.

    Expected on self:
      self.nb           — ttk.Notebook
      self.vault_dir    — Path
      self.writer       — PersonalityModel (used as grapher AI; may be None)
    """

    def _build_grapher_tab(self):
        """Add the Grapher tab to self.nb."""
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="📊 Grapher")
        self.tab_grapher = tab

        if not _GRAPHER_APP_OK:
            ttk.Label(
                tab,
                text="grapher_app.py not found — place it in your council folder.",
                foreground=PALETTE["red"],
            ).pack(padx=12, pady=20)
            return

        # Resolve a suitable AI model — prefer writer, fall back to any available
        personality = (
            getattr(self, "writer", None)
            or getattr(self, "director", None)
            or getattr(self, "content", None)
        )

        self._grapher_app = GrapherApp(
            parent            = tab,
            vault_dir         = self.vault_dir,
            personality_model = personality,
        )
        self._grapher_app.frame.pack(fill="both", expand=True)


# ============================================================
# Standalone entry-point (just delegates to grapher_app)
# ============================================================

def run_standalone():
    """Launch grapher_app directly as a standalone window."""
    if not _GRAPHER_APP_OK:
        print("ERROR: grapher_app.py not found. "
              "Place it in the same folder as tab_grapher.py.")
        raise SystemExit(1)

    # Delegate entirely to grapher_app's own CLI runner
    from grapher_app import run_standalone as _ga_run
    _ga_run()


if __name__ == "__main__":
    run_standalone()
