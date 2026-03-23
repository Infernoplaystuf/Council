# ============================================================
# grapher_app.py  —  Standalone AI-assisted data grapher
# ============================================================
# Can be used in three ways:
#
#   1. Standalone executable:
#        python grapher_app.py
#        python grapher_app.py --vault /path/to/data
#        python grapher_app.py --vault /path --no-ai
#
#   2. Embedded in another tkinter window:
#        from grapher_app import GrapherApp
#        app = GrapherApp(parent_frame, vault_dir=Path("data"))
#        app.frame.pack(fill="both", expand=True)
#
#   3. With a pre-built council PersonalityModel:
#        from grapher_app import GrapherApp
#        import council_engine as ce
#        writer = ce.PersonalityModel(...)
#        app = GrapherApp(parent_frame, vault_dir=..., personality_model=writer)
#
# Requirements (all optional except the first two):
#   pip install pandas numpy plotly
#   pip install openpyxl         # Excel support
#   pip install tkinterweb       # embedded HTML rendering
#   pip install scikit-learn scipy  # PCA / spectrogram plots
# ============================================================

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import tempfile
import threading
import webbrowser
from copy import copy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

# ── Core graph modules ────────────────────────────────────────
try:
    import graph_data   as gd
    import graph_engine as ge
    _GRAPHER_OK = True
except ImportError as _e:
    _GRAPHER_OK = False
    print(f"[GrapherApp] graph modules unavailable: {_e}")

# ── Embedded HTML renderer ────────────────────────────────────
try:
    import tkinterweb
    _TKWEB_OK = True
except ImportError:
    _TKWEB_OK = False

# ── AI personality (optional — full council not required) ─────
try:
    import graph_personality as gp
    _GP_OK = True
except ImportError:
    _GP_OK = False

# ── council_engine for PersonalityModel (optional) ───────────
try:
    import council_engine as _ce
    _CE_OK = True
except ImportError:
    _CE_OK = False

# ── Catppuccin-Mocha colour palette ──────────────────────────
PALETTE = {
    "bg":        "#1e1e2e",
    "surface":   "#313244",
    "overlay":   "#585b70",
    "text":      "#cdd6f4",
    "subtext":   "#a6adc8",
    "blue":      "#89b4fa",
    "green":     "#a6e3a1",
    "red":       "#f38ba8",
    "yellow":    "#f9e2af",
    "mauve":     "#cba6f7",
}


# ============================================================
# GrapherApp — embeddable / standalone grapher widget
# ============================================================

class GrapherApp:
    """
    Self-contained AI-assisted data grapher.

    Parameters
    ----------
    parent : tk.Widget
        Parent widget to embed the grapher frame into.
        When running standalone pass ``None`` — a Tk root is
        created automatically.
    vault_dir : Path | None
        Directory scanned for data files.  Defaults to
        ``~/council_vault`` (or wherever the council keeps its
        vault when run inside council_gui_engine).
    personality_model : Any | None
        Optional ``council_engine.PersonalityModel`` (or any
        object with a ``.respond(prompt) -> str`` method) used
        for AI-assist features.  When omitted the app tries to
        build a minimal council Writer if ``council_engine`` is
        importable; otherwise AI buttons are disabled.
    title : str
        Window title shown when running standalone.
    """

    def __init__(
        self,
        parent:            Optional[tk.Widget] = None,
        vault_dir:         Optional[Path]      = None,
        personality_model: Any                 = None,
        title:             str                 = "📊 Grapher",
    ):
        # ── Vault directory ───────────────────────────────────
        if vault_dir is None:
            vault_dir = Path.home() / "council_vault"
        self.vault_dir = Path(vault_dir)
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir = self.vault_dir / "graph_output"
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # ── Window / frame ────────────────────────────────────
        self._owns_root = parent is None
        if self._owns_root:
            self.root = tk.Tk()
            self.root.title(title)
            self.root.geometry("1340x820")
            self._apply_theme(self.root)
            self.frame = ttk.Frame(self.root)
            self.frame.pack(fill="both", expand=True)
        else:
            self.root  = parent.winfo_toplevel()
            self.frame = ttk.Frame(parent)

        # ── Async queue ───────────────────────────────────────
        self.ui_q: queue.Queue = queue.Queue()

        # ── AI model ─────────────────────────────────────────
        self._writer:           Any = personality_model
        self._analyst:          Any = None
        self._ai_available:     bool = False
        self._init_ai()

        # ── State ─────────────────────────────────────────────
        self._dataset:            Optional[Any] = None  # gd.DataSet
        self._spec:               Optional[Any] = None  # ge.PlotSpec
        self._file_paths:         List[Path]    = []
        self._last_html_path:     Optional[Path]= None
        self._plot_history:       List[Tuple[str, Path]] = []
        self._transforms:         List[Dict]    = []
        self._live_job:           Optional[str] = None
        self._overlay_ds:         Optional[Any] = None
        self._overlay_paths:      List[Path]    = []
        self._live_reload_var:    tk.BooleanVar  = tk.BooleanVar(value=False)
        self._live_interval_var:  tk.IntVar      = tk.IntVar(value=5)

        # ── Build UI ──────────────────────────────────────────
        self._build_ui()
        self._refresh_files()

        # ── Start queue poll ──────────────────────────────────
        self._poll_queue()

    # =========================================================
    # Theme
    # =========================================================

    def _apply_theme(self, root: tk.Tk):
        root.configure(bg=PALETTE["bg"])
        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".",
            background=PALETTE["bg"], foreground=PALETTE["text"],
            fieldbackground=PALETTE["surface"], selectbackground=PALETTE["overlay"],
            selectforeground=PALETTE["text"], bordercolor=PALETTE["overlay"],
            troughcolor=PALETTE["surface"], insertcolor=PALETTE["text"],
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
                        ("pressed", PALETTE["overlay"])],
            foreground=[("active", PALETTE["text"])])
        style.configure("TCombobox",
            fieldbackground=PALETTE["surface"], foreground=PALETTE["text"],
            background=PALETTE["surface"], arrowcolor=PALETTE["blue"])
        style.configure("TScrollbar",
            background=PALETTE["surface"], troughcolor=PALETTE["bg"],
            arrowcolor=PALETTE["subtext"])
        style.configure("TCheckbutton",
            background=PALETTE["bg"], foreground=PALETTE["text"])
        style.configure("TRadiobutton",
            background=PALETTE["bg"], foreground=PALETTE["text"])
        style.configure("TSeparator", background=PALETTE["overlay"])
        style.configure("TSpinbox",
            fieldbackground=PALETTE["surface"], foreground=PALETTE["text"],
            background=PALETTE["surface"], arrowcolor=PALETTE["blue"])

    # =========================================================
    # AI initialisation
    # =========================================================

    def _init_ai(self):
        """Try to set up the AI analyst. Degrades gracefully if unavailable."""
        if not _GRAPHER_OK:
            return

        # 1. If a personality_model was passed in, use it directly
        if self._writer is not None:
            self._build_analyst(self._writer)
            return

        # 2. Try to build a minimal Writer from council_engine
        if _CE_OK:
            try:
                reg = _ce.BackendRegistry()
                reg.discover()
                if not reg.backends:
                    return
                writer = _ce.PersonalityModel(
                    name        = "writer",
                    system_prompt = _ce.ROLE_PROMPTS.get("writer", "You are a helpful assistant."),
                    weights     = {"default": 1.0},
                    registry    = reg,
                    temperature = 0.4,
                )
                self._writer = writer
                self._build_analyst(writer)
            except Exception as e:
                print(f"[GrapherApp] council_engine init failed: {e}")

    def _build_analyst(self, model):
        if not _GP_OK or not _GRAPHER_OK:
            return
        try:
            self._analyst = gp.AnalystPersonality(
                personality_model=model,
                event_callback=lambda ph, msg: self.ui_q.put(("event", ph, msg)),
            )
            self._ai_available = True
        except Exception as e:
            print(f"[GrapherApp] Analyst init failed: {e}")

    # =========================================================
    # UI construction
    # =========================================================

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

    def _build_ui(self):
        ci = self.frame

        # ── Outer horizontal paned window ─────────────────────
        main_pane = tk.PanedWindow(ci, orient="horizontal",
                                   bg=PALETTE["bg"], sashwidth=6)
        main_pane.pack(fill="both", expand=True, padx=6, pady=6)

        # ── LEFT: file browser + scrollable settings ──────────
        left_outer = ttk.Frame(main_pane)
        main_pane.add(left_outer, width=340)
        self._build_left(left_outer)

        # ── RIGHT: plot + panels ──────────────────────────────
        right_pane = tk.PanedWindow(main_pane, orient="vertical",
                                    bg=PALETTE["bg"], sashwidth=5)
        main_pane.add(right_pane)
        self._build_right(right_pane)

    # ── Left panel ────────────────────────────────────────────

    def _build_left(self, parent):
        # ── File browser ──────────────────────────────────────
        fb = ttk.LabelFrame(parent, text="Data File")
        fb.pack(fill="x", padx=4, pady=(4, 4))

        fl1 = ttk.Frame(fb)
        fl1.pack(fill="x", padx=6, pady=(4, 2))
        ttk.Label(fl1, text="Vault:", width=6).pack(side="left")
        self._file_var = tk.StringVar()
        self._file_cb  = ttk.Combobox(fl1, textvariable=self._file_var,
                                       width=22, state="readonly")
        self._file_cb.pack(side="left", padx=4, fill="x", expand=True)
        self._file_cb.bind("<<ComboboxSelected>>", self._load_file)
        ttk.Button(fl1, text="⟳", width=2,
                   command=self._refresh_files).pack(side="left")

        fl2 = ttk.Frame(fb)
        fl2.pack(fill="x", padx=6, pady=(0, 2))
        ttk.Button(fl2, text="📂 Browse…",
                   command=self._browse_file).pack(side="left")
        ttk.Label(fl2, text="Sheet:", foreground=PALETTE["subtext"]).pack(
            side="left", padx=(10, 2))
        self._sheet_var = tk.StringVar()
        self._sheet_cb  = ttk.Combobox(fl2, textvariable=self._sheet_var,
                                        width=10, state="readonly")
        self._sheet_cb.pack(side="left")
        self._sheet_cb.bind("<<ComboboxSelected>>", self._reload_sheet)

        self._status_var = tk.StringVar(value="No file loaded")
        ttk.Label(fb, textvariable=self._status_var,
                  foreground=PALETTE["blue"], wraplength=300,
                  justify="left").pack(anchor="w", padx=6, pady=(0, 4))

        # ── Live reload + overlay ──────────────────────────────
        fl3 = ttk.Frame(fb)
        fl3.pack(fill="x", padx=6, pady=(0, 2))
        ttk.Checkbutton(fl3, text="🔄 Live reload",
                        variable=self._live_reload_var,
                        command=self._live_toggle).pack(side="left")
        ttk.Spinbox(fl3, from_=1, to=120,
                    textvariable=self._live_interval_var, width=4).pack(
                        side="left", padx=2)
        ttk.Label(fl3, text="s", foreground=PALETTE["subtext"]).pack(side="left")

        fl4 = ttk.Frame(fb)
        fl4.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(fl4, text="Overlay:", foreground=PALETTE["subtext"],
                  width=7).pack(side="left")
        self._overlay_var = tk.StringVar(value="(none)")
        self._overlay_cb  = ttk.Combobox(fl4, textvariable=self._overlay_var,
                                          width=18, state="readonly")
        self._overlay_cb.pack(side="left", padx=4, fill="x", expand=True)
        self._overlay_cb.bind("<<ComboboxSelected>>", self._overlay_load)
        ttk.Button(fl4, text="📂", width=2,
                   command=self._overlay_browse).pack(side="left")

        # ── Scrollable settings ───────────────────────────────
        ctrl_frame  = ttk.Frame(parent)
        ctrl_frame.pack(fill="both", expand=True, padx=4)

        ctrl_canvas = tk.Canvas(ctrl_frame, bg=PALETTE["bg"], highlightthickness=0)
        ctrl_scroll = ttk.Scrollbar(ctrl_frame, orient="vertical",
                                    command=ctrl_canvas.yview)
        self._ctrl_inner = ttk.Frame(ctrl_canvas)
        self._ctrl_inner.bind(
            "<Configure>",
            lambda e: ctrl_canvas.configure(
                scrollregion=ctrl_canvas.bbox("all")))
        ctrl_canvas.create_window((0, 0), window=self._ctrl_inner, anchor="nw")
        ctrl_canvas.configure(yscrollcommand=ctrl_scroll.set)
        ctrl_scroll.pack(side="right", fill="y")
        ctrl_canvas.pack(side="left", fill="both", expand=True)

        def _mw_enter(e):
            ctrl_canvas.bind_all(
                "<MouseWheel>",
                lambda ev: ctrl_canvas.yview_scroll(
                    -1 * (ev.delta // 120), "units"))
        def _mw_leave(e):
            ctrl_canvas.unbind_all("<MouseWheel>")
        ctrl_canvas.bind("<Enter>", _mw_enter)
        ctrl_canvas.bind("<Leave>", _mw_leave)

        ci = self._ctrl_inner
        self._build_plot_type_section(ci)
        self._build_column_section(ci)
        self._build_options_section(ci)
        self._build_transform_section(ci)
        self._build_buttons_section(ci)
        self._build_ai_section(ci)
        self._build_preset_section(ci)

    def _build_plot_type_section(self, ci):
        ttk.Label(ci, text="Plot Type",
                  font=("", 10, "bold")).pack(anchor="w", pady=(8, 2), padx=6)

        PLOT_GROUPS = {
            "Basic":       ["line", "bar", "scatter", "histogram", "pie", "area"],
            "Statistical": ["box", "violin", "heatmap", "correlation",
                            "distribution", "density_2d", "parallel_coords"],
            "Scientific":  ["fft", "spectrogram", "polar", "contour",
                            "surface_3d", "scatter_3d"],
            "Time Series": ["timeseries", "rolling_mean", "trend", "anomaly"],
            "Dimensional": ["pca"],
        }
        self._plot_type_var = tk.StringVar(value="line")
        for group, types in PLOT_GROUPS.items():
            ttk.Label(ci, text=group, foreground=PALETTE["blue"],
                      font=("", 9, "bold")).pack(anchor="w", padx=8, pady=(4, 0))
            fr = ttk.Frame(ci)
            fr.pack(fill="x", padx=8)
            for col_n, pt in enumerate(types):
                ttk.Radiobutton(
                    fr, text=pt, value=pt,
                    variable=self._plot_type_var,
                ).grid(row=col_n // 2, column=col_n % 2, sticky="w", padx=2)

        ttk.Separator(ci, orient="horizontal").pack(fill="x", padx=6, pady=6)

    def _build_column_section(self, ci):
        ttk.Label(ci, text="Columns",
                  font=("", 10, "bold")).pack(anchor="w", padx=6, pady=(0, 2))

        def _col_row(label, attr):
            fr = ttk.Frame(ci)
            fr.pack(fill="x", padx=6, pady=1)
            ttk.Label(fr, text=label, width=11).pack(side="left")
            var = tk.StringVar(value="—")
            cb  = ttk.Combobox(fr, textvariable=var, width=16, state="readonly")
            cb.pack(side="left", padx=4)
            setattr(self, attr + "_var", var)
            setattr(self, attr + "_cb",  cb)

        _col_row("X axis:",    "_cx")
        _col_row("Y axis:",    "_cy")
        _col_row("Z axis:",    "_cz")
        _col_row("Color by:",  "_cc")
        _col_row("Size by:",   "_cs")
        _col_row("Facet col:", "_cfacet_col")
        _col_row("Facet row:", "_cfacet_row")

        ttk.Label(ci, text="Multi-column (Ctrl+click):",
                  foreground=PALETTE["subtext"]).pack(
                      anchor="w", padx=6, pady=(6, 0))
        self._cols_lb = tk.Listbox(
            ci, selectmode="multiple", height=5,
            bg=PALETTE["surface"], fg=PALETTE["text"],
            selectbackground=PALETTE["overlay"], exportselection=False)
        self._cols_lb.pack(fill="x", padx=6)

        ttk.Separator(ci, orient="horizontal").pack(fill="x", padx=6, pady=6)

    def _build_options_section(self, ci):
        ttk.Label(ci, text="Options",
                  font=("", 10, "bold")).pack(anchor="w", padx=6, pady=(0, 2))

        def _spin(label, attr, lo, hi, default):
            fr = ttk.Frame(ci); fr.pack(fill="x", padx=6, pady=1)
            ttk.Label(fr, text=label, width=16).pack(side="left")
            var = tk.IntVar(value=default)
            ttk.Spinbox(fr, from_=lo, to=hi, textvariable=var, width=8).pack(side="left")
            setattr(self, attr, var)

        def _flt(label, attr, default):
            fr = ttk.Frame(ci); fr.pack(fill="x", padx=6, pady=1)
            ttk.Label(fr, text=label, width=16).pack(side="left")
            var = tk.StringVar(value=str(default))
            ttk.Entry(fr, textvariable=var, width=10).pack(side="left")
            setattr(self, attr, var)

        def _combo(label, attr, values, default):
            fr = ttk.Frame(ci); fr.pack(fill="x", padx=6, pady=1)
            ttk.Label(fr, text=label, width=16).pack(side="left")
            var = tk.StringVar(value=default)
            ttk.Combobox(fr, textvariable=var, values=values,
                         width=14, state="readonly").pack(side="left")
            setattr(self, attr, var)

        _spin("Histogram bins:", "_opt_bins",      5,   500,  30)
        _spin("Rolling window:", "_opt_window",    2,  1000,  10)
        _spin("Trend degree:",   "_opt_trend",     1,     6,   1)
        _flt( "Anomaly σ:",      "_opt_sigma",                3.0)
        _flt( "Sample rate Hz:", "_opt_samplerate",           1.0)
        _spin("Marker size:",    "_opt_marker",    1,    30,   6)
        _spin("Line width:",     "_opt_lw",        1,    10,   2)
        _flt( "Opacity:",        "_opt_opacity",              0.85)
        _combo("Colour scheme:", "_opt_cscheme",
               ["viridis", "plasma", "inferno", "magma", "Blues", "Reds",
                "RdBu_r", "RdYlGn", "spectral", "YlOrRd", "Greens"],
               "viridis")
        _combo("Plotly theme:",  "_opt_theme",
               ["plotly_dark", "plotly", "ggplot2", "seaborn",
                "simple_white", "presentation"],
               "plotly_dark")
        _flt("Title:", "_opt_title", "")

        ttk.Separator(ci, orient="horizontal").pack(fill="x", padx=6, pady=6)

    def _build_transform_section(self, ci):
        ttk.Label(ci, text="Transforms",
                  font=("", 10, "bold")).pack(anchor="w", padx=6, pady=(0, 2))
        self._transform_lb = tk.Listbox(
            ci, height=3, bg=PALETTE["surface"], fg=PALETTE["green"],
            selectbackground=PALETTE["overlay"], exportselection=False)
        self._transform_lb.pack(fill="x", padx=6)
        tf_fr = ttk.Frame(ci)
        tf_fr.pack(fill="x", padx=6, pady=(2, 0))
        for op in ("normalize", "log", "standardize", "clip"):
            ttk.Button(
                tf_fr, text=op,
                command=lambda o=op: self._transform_add(o),
            ).pack(side="left", padx=1)
        ttk.Button(tf_fr, text="✕ clear",
                   command=self._transform_clear).pack(side="right")
        ttk.Separator(ci, orient="horizontal").pack(fill="x", padx=6, pady=6)

    def _build_buttons_section(self, ci):
        ttk.Button(ci, text="▶  Plot (Interactive)",
                   command=self._plot_interactive).pack(fill="x", padx=6, pady=2)

        row2 = ttk.Frame(ci)
        row2.pack(fill="x", padx=6, pady=2)
        ttk.Button(row2, text="📌 Pin plot",
                   command=self._pin_plot).pack(side="left", expand=True, fill="x")
        if self._ai_available:
            ttk.Button(row2, text="🎙 Narrate",
                       command=self._narrate).pack(side="left", padx=2)

        exp_row = ttk.Frame(ci)
        exp_row.pack(fill="x", padx=6, pady=2)
        ttk.Button(exp_row, text="🖼 Export PNG",
                   command=lambda: self._export("png")).pack(
                       side="left", expand=True, fill="x")
        ttk.Button(exp_row, text="PDF",
                   command=lambda: self._export("pdf")).pack(side="left", padx=2)
        ttk.Button(exp_row, text="SVG",
                   command=lambda: self._export("svg")).pack(side="left")
        ttk.Separator(ci, orient="horizontal").pack(fill="x", padx=6, pady=6)

    def _build_ai_section(self, ci):
        ttk.Label(ci, text="AI Assist",
                  font=("", 10, "bold")).pack(anchor="w", padx=6)
        ai_note = ("Describe what to visualise:" if self._ai_available
                   else "AI unavailable — install council_engine or pass a model.")
        ttk.Label(ci, text=ai_note,
                  foreground=(PALETTE["subtext"] if self._ai_available
                               else PALETTE["red"]),
                  font=("", 9)).pack(anchor="w", padx=6)

        self._ai_prompt = tk.Text(
            ci, height=3, wrap="word",
            font=("Consolas", 10),
            bg=PALETTE["surface"], fg=PALETTE["text"],
            insertbackground=PALETTE["text"],
            state="normal" if self._ai_available else "disabled")
        self._ai_prompt.pack(fill="x", padx=6, pady=(2, 4))
        if self._ai_available:
            self._ai_prompt.insert("1.0",
                "Show me the distribution of all numeric columns")

        if self._ai_available:
            ttk.Button(ci, text="🤖 Ask AI to plot",
                       command=self._ai_plot).pack(fill="x", padx=6, pady=2)
        ttk.Button(ci, text="📊 Quick stats summary",
                   command=self._quick_stats).pack(fill="x", padx=6, pady=2)
        ttk.Button(ci, text="📊 Plot council table",
                   command=self._plot_council_table).pack(fill="x", padx=6, pady=2)
        ttk.Separator(ci, orient="horizontal").pack(fill="x", padx=6, pady=6)

    def _build_preset_section(self, ci):
        ttk.Label(ci, text="Presets",
                  font=("", 10, "bold")).pack(anchor="w", padx=6, pady=(0, 2))
        pr_fr = ttk.Frame(ci)
        pr_fr.pack(fill="x", padx=6, pady=(0, 6))
        self._preset_var = tk.StringVar(value="")
        self._preset_cb  = ttk.Combobox(pr_fr, textvariable=self._preset_var,
                                         width=16, state="readonly")
        self._preset_cb.pack(side="left", fill="x", expand=True)
        ttk.Button(pr_fr, text="💾 Save",
                   command=self._save_preset).pack(side="left", padx=2)
        ttk.Button(pr_fr, text="📂 Load",
                   command=self._load_preset).pack(side="left")
        self._refresh_presets()

    # ── Right panel ───────────────────────────────────────────

    def _build_right(self, right_pane):
        # Plot area
        plot_frame = ttk.Frame(right_pane)
        right_pane.add(plot_frame, height=480)

        self._plot_label_var = tk.StringVar(value="")
        ttk.Label(plot_frame, textvariable=self._plot_label_var,
                  foreground=PALETTE["green"]).pack(anchor="w", padx=4)

        self._web_frame = None
        if _TKWEB_OK:
            try:
                self._web_frame = tkinterweb.HtmlFrame(
                    plot_frame, messages_enabled=False)
                self._web_frame.pack(fill="both", expand=True)
            except Exception:
                self._web_frame = None

        if self._web_frame is None:
            no_web = ttk.Frame(plot_frame)
            no_web.pack(fill="both", expand=True)
            ttk.Label(
                no_web,
                text="Interactive plots open in your browser.\n"
                     "Install tkinterweb for embedded view:\n"
                     "  pip install tkinterweb",
                foreground=PALETTE["blue"], font=("", 11),
            ).pack(expand=True)
            ttk.Button(
                no_web, text="🌐 Open last plot in browser",
                command=self._open_in_browser,
            ).pack(pady=8)

        # Stats panel
        stats_frame = ttk.LabelFrame(right_pane, text="Stats & AI Analysis")
        right_pane.add(stats_frame, height=180)
        self._stats = self._make_text(stats_frame, height=8,
                                       wrap="word", state="disabled")
        self._stats.pack(fill="both", expand=True, padx=4, pady=4)

        # History panel
        hist_frame = ttk.LabelFrame(right_pane, text="📌 Plot History")
        right_pane.add(hist_frame, height=120)
        hist_top = ttk.Frame(hist_frame)
        hist_top.pack(fill="x", padx=4, pady=2)
        ttk.Button(hist_top, text="🗂 Open",
                   command=self._history_open).pack(side="left")
        ttk.Button(hist_top, text="✕ Clear",
                   command=self._history_clear).pack(side="left", padx=4)
        self._history_lb = tk.Listbox(
            hist_frame, height=4,
            bg=PALETTE["surface"], fg=PALETTE["text"],
            selectbackground=PALETTE["overlay"], exportselection=False)
        self._history_lb.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self._history_lb.bind(
            "<Double-Button-1>", lambda e: self._history_open())

        # Correlation drill panel
        corr_frame = ttk.LabelFrame(right_pane, text="🔍 Correlation Drill")
        right_pane.add(corr_frame, height=80)
        corr_row = ttk.Frame(corr_frame)
        corr_row.pack(fill="x", padx=4, pady=4)
        ttk.Label(corr_row, text="Col A:", width=7).pack(side="left")
        self._corr_a_var = tk.StringVar(value="—")
        self._corr_a_cb  = ttk.Combobox(corr_row, textvariable=self._corr_a_var,
                                          width=12, state="readonly")
        self._corr_a_cb.pack(side="left", padx=2)
        ttk.Label(corr_row, text="Col B:", width=6).pack(side="left", padx=(8, 0))
        self._corr_b_var = tk.StringVar(value="—")
        self._corr_b_cb  = ttk.Combobox(corr_row, textvariable=self._corr_b_var,
                                          width=12, state="readonly")
        self._corr_b_cb.pack(side="left", padx=2)
        ttk.Button(corr_row, text="🔍 Drill",
                   command=self._corr_drill).pack(side="left", padx=6)

    # =========================================================
    # Queue poll
    # =========================================================

    def _poll_queue(self):
        try:
            while True:
                item = self.ui_q.get_nowait()
                kind = item[0]

                if kind == "event":
                    _, phase, msg = item
                    self._show_stats(f"[{phase}] {msg}")

                elif kind == "stats":
                    _, text = item
                    self._show_stats(text)

                elif kind == "ai_result":
                    _, result = item
                    if result.parse_error:
                        self._show_stats(
                            f"✗ AI parse error: {result.parse_error}\n\n"
                            + getattr(result, "analysis", ""))
                    else:
                        if result.analysis:
                            self._show_stats(result.analysis)
                        if result.spec and self._dataset:
                            self._apply_spec_to_controls(result.spec)
                            self._render_plotly(result.spec, self._dataset)

                elif kind == "autosuggest":
                    _, hint, suggestion = item
                    if hint:
                        self._show_stats(hint)
                    if (suggestion and getattr(suggestion, "spec", None)
                            and self._dataset):
                        self._apply_spec_to_controls(suggestion.spec)

        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)

    # =========================================================
    # File operations
    # =========================================================

    def _refresh_files(self):
        if not _GRAPHER_OK:
            return
        files  = gd.scan_vault_for_data(self.vault_dir)
        labels = [str(p.relative_to(self.vault_dir)) for p in files]
        self._file_cb["values"] = labels
        self._file_paths        = files
        n = len(files)
        self._status_var.set(
            f"{n} data file{'s' if n != 1 else ''} in vault")

    def _browse_file(self):
        import tkinter.filedialog as fd
        path_str = fd.askopenfilename(
            title="Open data file",
            filetypes=[
                ("All supported",
                 "*.csv *.tsv *.xlsx *.xls *.json *.npy *.npz *.txt *.log"),
                ("CSV/TSV",  "*.csv *.tsv"),
                ("Excel",    "*.xlsx *.xls"),
                ("JSON",     "*.json"),
                ("NumPy",    "*.npy *.npz"),
                ("Text/Log", "*.txt *.log"),
                ("All files", "*.*"),
            ],
        )
        if not path_str:
            return
        p     = Path(path_str)
        label = str(p)
        existing = list(self._file_cb["values"])
        if label not in existing:
            existing.append(label)
            self._file_cb["values"] = existing
            self._file_paths.append(p)
        self._file_var.set(label)
        self._do_load(p)

    def _load_file(self, event=None):
        if not _GRAPHER_OK:
            return
        sel = self._file_var.get()
        if not sel:
            return
        for p in self._file_paths:
            try:
                rel = str(p.relative_to(self.vault_dir))
            except ValueError:
                rel = str(p)
            if rel == sel or str(p) == sel:
                self._do_load(p)
                return

    def _do_load(self, path: Path):
        if not _GRAPHER_OK:
            return
        self._status_var.set(f"Loading {path.name}…")
        ds = gd.DataLoader.load(path)
        self._dataset = ds

        if ds.load_error:
            self._status_var.set(f"✗ {ds.load_error}")
            return

        rows, cols = ds.shape
        self._status_var.set(
            f"✓ {path.name} — {rows:,} rows × {cols} cols  [{ds.format}]")

        all_cols = ["—"] + ds.all_columns
        num_cols = ["—"] + ds.numeric_columns
        cat_cols = ["—"] + ds.categorical_columns

        for attr, choices in [
            ("_cx", all_cols), ("_cy", num_cols), ("_cz", num_cols),
            ("_cc", all_cols), ("_cs", num_cols),
            ("_cfacet_col", cat_cols), ("_cfacet_row", cat_cols),
        ]:
            if hasattr(self, attr + "_cb"):
                getattr(self, attr + "_cb")["values"] = choices
                getattr(self, attr + "_var").set(choices[0])

        if len(all_cols) > 1: self._cx_var.set(all_cols[1])
        if len(num_cols) > 1: self._cy_var.set(num_cols[1])

        self._cols_lb.delete(0, "end")
        for col in ds.all_columns:
            self._cols_lb.insert("end", col)
        for i, col in enumerate(ds.all_columns):
            if col in ds.numeric_columns:
                self._cols_lb.selection_set(i)

        if hasattr(self, "_corr_a_cb"):
            self._corr_a_cb["values"] = num_cols
            self._corr_b_cb["values"] = num_cols
            self._corr_a_var.set(num_cols[1] if len(num_cols) > 1 else "—")
            self._corr_b_var.set(num_cols[2] if len(num_cols) > 2 else "—")

        if hasattr(self, "_overlay_cb"):
            vault_labels = ["(none)"] + list(self._file_cb["values"])
            self._overlay_cb["values"] = vault_labels
            self._overlay_var.set("(none)")

        if ds.metadata.get("sheets"):
            sheets = ds.metadata["sheets"]
            self._sheet_cb["values"] = sheets
            self._sheet_var.set(
                ds.metadata.get("active_sheet", sheets[0]))
        else:
            self._sheet_cb["values"] = []
            self._sheet_var.set("")

        self._show_stats(ds.summary())

        # Auto-suggest (#3)
        if self._analyst and _GRAPHER_OK:
            def _suggest(ds=ds):
                try:
                    result = self._analyst.analyse(
                        "What is the best single plot to explore this dataset? "
                        "Pick the most revealing visualisation.", ds)
                    hint = (f"💡 Auto-suggestion: "
                            f"{result.spec.plot_type if result.spec else '?'}\n"
                            f"{result.analysis[:300]}" if result.analysis else "")
                    self.ui_q.put(("autosuggest", hint, result))
                except Exception:
                    pass
            threading.Thread(target=_suggest, daemon=True).start()

    def _reload_sheet(self, event=None):
        if not _GRAPHER_OK or self._dataset is None:
            return
        if self._sheet_var.get():
            self._do_load(self._dataset.source_path)

    # =========================================================
    # Spec building
    # =========================================================

    def _col(self, attr) -> Optional[str]:
        v = getattr(self, attr + "_var").get()
        return v if v and v not in ("—", "\u2014") else None

    def _build_spec(self) -> Optional[Any]:
        if not _GRAPHER_OK:
            return None
        ds  = self._dataset
        sel = self._cols_lb.curselection()
        multi = ([ds.all_columns[i] for i in sel
                  if i < len(ds.all_columns)] if ds else [])
        try:
            sigma   = float(self._opt_sigma.get())
            opacity = float(self._opt_opacity.get())
            sr      = float(self._opt_samplerate.get())
            title   = self._opt_title.get()
        except Exception:
            sigma, opacity, sr, title = 3.0, 0.85, 1.0, ""

        return ge.PlotSpec(
            plot_type         = self._plot_type_var.get(),
            x_col             = self._col("_cx"),
            y_col             = self._col("_cy"),
            z_col             = self._col("_cz"),
            color_col         = self._col("_cc"),
            size_col          = self._col("_cs"),
            columns           = multi,
            title             = title,
            color_scheme      = self._opt_cscheme.get(),
            theme             = self._opt_theme.get(),
            bins              = self._opt_bins.get(),
            window            = self._opt_window.get(),
            trend_degree      = self._opt_trend.get(),
            anomaly_threshold = sigma,
            fft_sample_rate   = sr,
            marker_size       = self._opt_marker.get(),
            line_width        = self._opt_lw.get(),
            opacity           = opacity,
            renderer          = "plotly",
            facet_col         = self._col("_cfacet_col"),
            facet_row         = self._col("_cfacet_row"),
        )

    def _apply_spec_to_controls(self, spec):
        if spec is None:
            return
        try:
            if getattr(spec, "plot_type", None):
                self._plot_type_var.set(spec.plot_type)

            def _try_set(attr, val):
                if not val:
                    return
                cb  = getattr(self, attr + "_cb",  None)
                var = getattr(self, attr + "_var", None)
                if cb and var and val in list(cb["values"]):
                    var.set(val)

            _try_set("_cx", getattr(spec, "x_col",     None))
            _try_set("_cy", getattr(spec, "y_col",     None))
            _try_set("_cz", getattr(spec, "z_col",     None))
            _try_set("_cc", getattr(spec, "color_col", None))
            _try_set("_cs", getattr(spec, "size_col",  None))

            ds = self._dataset
            if getattr(spec, "columns", None) and ds:
                self._cols_lb.selection_clear(0, "end")
                for i, col in enumerate(ds.all_columns):
                    if col in spec.columns:
                        self._cols_lb.selection_set(i)

            for attr_name, spec_attr, cast in [
                ("_opt_bins",        "bins",              int),
                ("_opt_window",      "window",            int),
                ("_opt_trend",       "trend_degree",      int),
                ("_opt_marker",      "marker_size",       int),
                ("_opt_lw",          "line_width",        int),
                ("_opt_sigma",       "anomaly_threshold", str),
                ("_opt_samplerate",  "fft_sample_rate",   str),
                ("_opt_opacity",     "opacity",           str),
                ("_opt_cscheme",     "color_scheme",      str),
                ("_opt_theme",       "theme",             str),
                ("_opt_title",       "title",             str),
            ]:
                val = getattr(spec, spec_attr, None)
                if val is not None and hasattr(self, attr_name):
                    getattr(self, attr_name).set(cast(val))
        except Exception as e:
            print(f"[GrapherApp] apply_spec_to_controls: {e}")

    # =========================================================
    # Rendering
    # =========================================================

    def _plot_interactive(self):
        if not _GRAPHER_OK:
            return
        if self._dataset is None:
            self._show_stats("No file loaded. Select a file first.")
            return
        spec = self._build_spec()
        if spec:
            self._render_plotly(spec, self._dataset)

    def _render_plotly(self, spec, ds):
        if not _GRAPHER_OK:
            return

        # Apply transforms
        working_ds = ds
        if self._transforms and ds.df is not None:
            working_ds      = copy(ds)
            working_ds.df, tf_log = ge.apply_transforms(ds.df, self._transforms)
            if hasattr(self, "_transform_lb"):
                self._transform_lb.delete(0, "end")
                for msg in tf_log:
                    self._transform_lb.insert("end", msg)

        # Multi-file overlay
        if (self._overlay_ds is not None
                and self._overlay_ds.df is not None
                and spec.y_col
                and spec.plot_type in ("line", "timeseries", "scatter", "area")):
            try:
                renderer = ge.PlotlyRenderer()
                html = renderer._overlay_render(spec, working_ds, self._overlay_ds)
            except Exception:
                renderer = ge.PlotlyRenderer()
                html = renderer.render(spec, working_ds)
        else:
            renderer = ge.PlotlyRenderer()
            html = renderer.render(spec, working_ds)

        label = f"{spec.plot_type.replace('_', ' ').title()} — {ds.name}"
        self._plot_label_var.set(label)
        self._spec = spec

        # Save to history
        self.output_dir.mkdir(parents=True, exist_ok=True)
        ts    = datetime.now().strftime("%H%M%S")
        hpath = self.output_dir / f"plot_{ts}_{spec.plot_type}.html"
        hpath.write_text(html, encoding="utf-8")
        self._last_html_path = hpath
        entry = f"{ts} — {spec.plot_type} — {ds.name}"
        self._plot_history.append((entry, hpath))
        if hasattr(self, "_history_lb"):
            self._history_lb.insert("end", entry)
            self._history_lb.see("end")

        if self._web_frame is not None:
            self._web_frame.load_html(html)
        else:
            webbrowser.open(hpath.as_uri())
            self._show_stats(
                f"Plot opened in browser: {hpath}\n\n"
                + ge.DataAnalyser.describe(ds)[:800])

    def _open_in_browser(self):
        if self._last_html_path and self._last_html_path.exists():
            webbrowser.open(self._last_html_path.as_uri())

    def _export(self, fmt: str):
        if not _GRAPHER_OK or self._dataset is None:
            self._show_stats("Load a file first.")
            return
        import tkinter.filedialog as fd
        spec = self._build_spec()
        spec.renderer = "matplotlib"
        ds   = self._dataset
        path_str = fd.asksaveasfilename(
            title=f"Export {fmt.upper()}",
            defaultextension=f".{fmt}",
            initialfile=f"{ds.name}_{spec.plot_type}.{fmt}",
            filetypes=[(fmt.upper(), f"*.{fmt}"), ("All files", "*.*")],
        )
        if not path_str:
            return
        mpl_r = ge.MatplotlibRenderer()
        fig   = mpl_r.render(spec, ds)
        if fig:
            saved = mpl_r.save(fig, Path(path_str))
            self._show_stats(f"✓ Exported: {saved}")
        else:
            self._show_stats("✗ Export failed — matplotlib could not render this plot type.")

    # =========================================================
    # AI assist
    # =========================================================

    def _ai_plot(self):
        if not _GRAPHER_OK or not self._ai_available:
            return
        if self._dataset is None:
            self._show_stats("Load a data file first.")
            return
        prompt = self._ai_prompt.get("1.0", "end").strip()
        if not prompt:
            return
        ds = self._dataset
        self._show_stats("Asking AI…")

        def _worker():
            try:
                result = self._analyst.analyse(prompt, ds)
                self.ui_q.put(("ai_result", result))
            except Exception as e:
                self.ui_q.put(("stats", f"✗ AI error: {e}"))

        threading.Thread(target=_worker, daemon=True).start()

    def _quick_stats(self):
        if not _GRAPHER_OK or self._dataset is None:
            self._show_stats("Load a file first.")
            return
        ds = self._dataset

        def _worker():
            try:
                if self._analyst:
                    text = self._analyst.quick_analysis(ds)
                else:
                    text = ge.DataAnalyser.describe(ds)
                self.ui_q.put(("stats", text))
            except Exception as e:
                self.ui_q.put(("stats", f"Error: {e}"))

        threading.Thread(target=_worker, daemon=True).start()

    def _narrate(self):
        if not _GRAPHER_OK or not self._ai_available or self._dataset is None:
            return
        spec = self._spec or self._build_spec()
        ds   = self._dataset
        self._show_stats("Generating narration…")

        def _worker():
            try:
                prompt = (
                    f"Narrate the following data plot in plain English for a non-technical audience.\n"
                    f"Dataset: {ds.name}  ({ds.shape[0]:,} rows × {ds.shape[1]} cols)\n"
                    f"Plot type: {spec.plot_type}\n"
                    f"X: {spec.x_col or 'index'}  |  Y: {spec.y_col or '(multi)'}\n"
                    f"Numeric columns: {', '.join(ds.numeric_columns[:6])}\n\n"
                    f"Dataset summary:\n{ds.summary()[:600]}\n\n"
                    f"Give 3–5 sentences: what the chart shows, key insights, "
                    f"and one follow-up question worth investigating."
                )
                text = self._writer.respond(prompt)
                self.ui_q.put(("stats", f"🎙 Narration:\n\n{text}"))
            except Exception as e:
                self.ui_q.put(("stats", f"✗ Narration error: {e}"))

        threading.Thread(target=_worker, daemon=True).start()

    # =========================================================
    # Plot council table
    # =========================================================

    def _plot_council_table(self):
        """
        Parse a markdown table from the clipboard or a paste dialog,
        load it, and auto-suggest a plot.
        When used standalone the table text is read from the clipboard.
        When embedded in council_gui_engine it can be overridden.
        """
        if not _GRAPHER_OK:
            return
        # Try clipboard first; fall back to a paste dialog
        try:
            text = self.root.clipboard_get()
        except Exception:
            text = ""

        if not self._has_md_table(text):
            text = simpledialog.askstring(
                "Paste Table",
                "Paste a Markdown table here:",
                parent=self.root,
            ) or ""

        if not self._has_md_table(text):
            self._show_stats(
                "✗ No Markdown table found.\n\n"
                "Copy a Markdown table to the clipboard and try again,\n"
                "or paste it directly into the dialog.")
            return

        self._load_md_table(text)

    def _has_md_table(self, text: str) -> bool:
        return bool(re.search(
            r"\|.+\|\s*\n\|[-| :]+\|\s*\n(?:\|.+\|\s*\n?)+", text))

    def _load_md_table(self, text: str):
        table_match = re.search(
            r"(\|.+\|\s*\n\|[-| :]+\|\s*\n(?:\|.+\|\s*\n?)+)", text)
        if not table_match:
            return
        raw = table_match.group(1)
        try:
            import pandas as pd
            lines = [l.strip() for l in raw.strip().splitlines() if l.strip()]
            lines = [l for l in lines if not re.match(r"^\|[-| :]+\|$", l)]
            rows  = [[c.strip() for c in l.strip("|").split("|")] for l in lines]
            if len(rows) < 2:
                raise ValueError("Not enough rows")
            df = pd.DataFrame(rows[1:], columns=rows[0])
            for col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(df[col])
        except Exception as e:
            self._show_stats(f"✗ Table parse error: {e}")
            return

        with tempfile.NamedTemporaryFile(
                suffix=".csv", delete=False, mode="w", encoding="utf-8") as f:
            df.to_csv(f, index=False)
            tmp = Path(f.name)

        ds = gd.DataLoader.load(tmp)
        ds.name = "pasted_table"
        if ds.load_error:
            self._show_stats(f"✗ Could not load table: {ds.load_error}")
            return
        self._dataset = ds
        self._show_stats(
            f"✓ Table loaded: {ds.shape[0]} rows × {ds.shape[1]} cols\n"
            + ds.summary())
        if self._analyst:
            def _w(ds=ds):
                try:
                    result = self._analyst.analyse(
                        "Best plot for this pasted table data?", ds)
                    self.ui_q.put(("ai_result", result))
                except Exception:
                    pass
            threading.Thread(target=_w, daemon=True).start()

    # =========================================================
    # Transform pipeline
    # =========================================================

    def _transform_add(self, op: str):
        step = {"op": op, "cols": [], "params": {}}
        if op == "clip":
            step["op"]     = "clip_outliers"
            step["params"] = {"sigma": 3.0}
        self._transforms.append(step)
        self._transform_lb.insert("end", op)

    def _transform_clear(self):
        self._transforms.clear()
        self._transform_lb.delete(0, "end")

    # =========================================================
    # Plot history
    # =========================================================

    def _pin_plot(self):
        if self._last_html_path and self._last_html_path.exists():
            self._show_stats(f"📌 Pinned: {self._last_html_path.name}")
        else:
            self._show_stats("No plot to pin — plot something first.")

    def _history_open(self):
        sel = self._history_lb.curselection()
        if sel:
            idx = sel[0]
        elif self._plot_history:
            idx = len(self._plot_history) - 1
        else:
            return
        if idx < len(self._plot_history):
            _, path = self._plot_history[idx]
            if path.exists():
                webbrowser.open(path.as_uri())
            else:
                self._show_stats(f"✗ File no longer exists: {path}")

    def _history_clear(self):
        self._plot_history.clear()
        self._history_lb.delete(0, "end")

    # =========================================================
    # Live reload
    # =========================================================

    def _live_toggle(self):
        if self._live_reload_var.get():
            self._live_schedule()
        else:
            if self._live_job:
                self.root.after_cancel(self._live_job)
                self._live_job = None

    def _live_schedule(self):
        if not self._live_reload_var.get():
            return
        try:
            ms = int(self._live_interval_var.get()) * 1000
        except Exception:
            ms = 5000
        self._live_job = self.root.after(ms, self._live_tick)

    def _live_tick(self):
        self._live_job = None
        if not self._live_reload_var.get():
            return
        ds = self._dataset
        if ds and ds.source_path.exists():
            new_ds = gd.DataLoader.load(ds.source_path)
            if not new_ds.load_error and new_ds.shape != ds.shape:
                self._dataset = new_ds
                spec = self._spec or self._build_spec()
                if spec:
                    self._render_plotly(spec, new_ds)
        self._live_schedule()

    # =========================================================
    # Overlay
    # =========================================================

    def _overlay_load(self, event=None):
        if not _GRAPHER_OK:
            return
        label = self._overlay_var.get()
        if not label or label == "(none)":
            self._overlay_ds = None
            return
        for p in self._file_paths:
            try:
                rel = str(p.relative_to(self.vault_dir))
            except ValueError:
                rel = str(p)
            if rel == label or str(p) == label:
                self._overlay_ds = gd.DataLoader.load(p)
                self._show_stats(
                    f"📎 Overlay loaded: {p.name}  "
                    f"({self._overlay_ds.shape[0]:,} rows)")
                return

    def _overlay_browse(self):
        import tkinter.filedialog as fd
        path_str = fd.askopenfilename(
            title="Open overlay file",
            filetypes=[("All supported",
                        "*.csv *.tsv *.xlsx *.xls *.json *.npy *.npz *.txt"),
                       ("All files", "*.*")],
        )
        if not path_str:
            return
        p = Path(path_str)
        self._overlay_ds = gd.DataLoader.load(p)
        label = str(p)
        existing = list(self._overlay_cb["values"])
        if label not in existing:
            existing.append(label)
            self._overlay_cb["values"] = existing
            self._overlay_paths.append(p)
        self._overlay_var.set(label)
        self._show_stats(
            f"📎 Overlay loaded: {p.name}  "
            f"({self._overlay_ds.shape[0]:,} rows)")

    # =========================================================
    # Correlation drill
    # =========================================================

    def _corr_drill(self):
        if not _GRAPHER_OK or self._dataset is None:
            self._show_stats("Load a file first.")
            return
        col_a = self._corr_a_var.get()
        col_b = self._corr_b_var.get()
        ds    = self._dataset
        if col_a in ("—", "\u2014") or col_b in ("—", "\u2014") or col_a == col_b:
            self._show_stats("Select two different numeric columns.")
            return
        if col_a not in ds.df.columns or col_b not in ds.df.columns:
            self._show_stats(f"Columns not found: {col_a}, {col_b}")
            return
        spec = ge.PlotSpec(
            plot_type    = "scatter",
            x_col        = col_a,
            y_col        = col_b,
            title        = f"Correlation: {col_a} vs {col_b}",
            color_scheme = self._opt_cscheme.get(),
            theme        = self._opt_theme.get(),
            trend_degree = 1,
            renderer     = "plotly",
        )
        self._render_plotly(spec, ds)
        try:
            import pandas as pd
            clean = ds.df[[col_a, col_b]].dropna()
            r = clean[col_a].corr(clean[col_b])
            n = len(clean)
            strength = ("Strong" if abs(r) > 0.7
                        else "Moderate" if abs(r) > 0.4 else "Weak")
            direction = "positive" if r > 0 else "negative"
            self._show_stats(
                f"🔍 Correlation Drill: {col_a} vs {col_b}\n"
                f"Pearson r = {r:.4f}  |  n = {n:,}\n"
                f"{strength} {direction} correlation\n\n"
                + ge.DataAnalyser.describe(ds)[:400]
            )
        except Exception as e:
            self._show_stats(f"Correlation error: {e}")

    # =========================================================
    # Presets
    # =========================================================

    def _preset_file(self) -> Path:
        return self.vault_dir / "graph_presets.json"

    def _refresh_presets(self):
        if not hasattr(self, "_preset_cb"):
            return
        try:
            pf = self._preset_file()
            if pf.exists():
                data = json.loads(pf.read_text(encoding="utf-8"))
                self._preset_cb["values"] = list(data.keys())
        except Exception:
            pass

    def _save_preset(self):
        name = simpledialog.askstring("Save Preset", "Preset name:",
                                       parent=self.root)
        if not name:
            return
        spec = self._build_spec()
        if spec is None:
            return
        pf   = self._preset_file()
        data = {}
        if pf.exists():
            try:
                data = json.loads(pf.read_text(encoding="utf-8"))
            except Exception:
                pass
        data[name] = spec.to_dict()
        pf.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self._refresh_presets()
        self._preset_var.set(name)
        self._show_stats(f"✓ Preset '{name}' saved.")

    def _load_preset(self):
        name = self._preset_var.get()
        if not name:
            return
        try:
            data = json.loads(self._preset_file().read_text(encoding="utf-8"))
            spec = ge.PlotSpec.from_dict(data[name])
            self._apply_spec_to_controls(spec)
            self._show_stats(f"✓ Preset '{name}' loaded.")
        except Exception as e:
            self._show_stats(f"✗ Load preset error: {e}")

    # =========================================================
    # Stats display
    # =========================================================

    def _show_stats(self, text: str):
        self._stats.configure(state="normal")
        self._stats.delete("1.0", "end")
        self._stats.insert("1.0", text)
        self._stats.configure(state="disabled")

    # =========================================================
    # Standalone entry point
    # =========================================================

    def run(self):
        """Start the Tk mainloop. Only valid when owns_root=True."""
        if self._owns_root:
            self.root.mainloop()


# ============================================================
# CLI entry point
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Standalone AI-assisted data grapher")
    parser.add_argument(
        "--vault", metavar="PATH", default=None,
        help="Vault directory to scan for data files "
             "(default: ~/council_vault)")
    parser.add_argument(
        "--no-ai", action="store_true", default=False,
        help="Disable AI features entirely (faster startup)")
    parser.add_argument(
        "--title", default="📊 Grapher",
        help="Window title")
    args = parser.parse_args()

    vault = Path(args.vault) if args.vault else None

    # When --no-ai is set, patch _init_ai to do nothing
    if args.no_ai:
        GrapherApp._init_ai = lambda self: None

    app = GrapherApp(vault_dir=vault, title=args.title)
    app.run()


if __name__ == "__main__":
    main()
