# ============================================================
# tab_ideas.py  —  Overnight Video Idea Generator tab
# ============================================================
# Usage modes:
#   1. Mixin inside CouncilConsole: call _build_ideas_tab()
#   2. Standalone: python tab_ideas.py [--vault PATH] [--no-ai]
# ============================================================

from __future__ import annotations

import json
import re
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from council_modules import StandaloneHost, PALETTE
from idea_engine import (
    IdeaItem, IdeationSettings, IdeaStore, IdeationLoop,
    _missing_pitcher_fields,
)

try:
    import council_engine as ce
    from council_engine import get_model_size_label as _get_model_size_label
    _CE_OK = True
except ImportError:
    _CE_OK = False

try:
    from PIL import Image as _PILImage, ImageTk as _ImageTk
    _PIL_OK = True
except ImportError:
    _PIL_OK = False

try:
    from image_engine import ThumbnailGenerator
    _IMG_OK = True
except ImportError:
    _IMG_OK = False


def _idea_slug(item: "IdeaItem") -> str:
    """Generate a safe filename for an idea's Markdown file."""
    title_part = re.sub(r"[^a-z0-9]+", "-", item.display_title.lower())[:50].strip("-")
    date_part  = item.generated_at[:10]
    return f"{date_part}_{item.id}_{title_part}.md"


# ============================================================
# IdeaTabMixin
# ============================================================

class IdeaTabMixin:
    """
    Mixin that adds the Idea Generator tab.

    Expected on self (CouncilConsole or StandaloneHost):
      self.vault_dir
      self.after(ms, fn)
      self._make_text(parent)
      self._append_transcript(who, text, kind)
      # Optional:
      self.nb, self.tab_council, self.input
      self.ideator, self.pitcher      — PersonalityModel instances
      self._content_style             — optional ContentStyleManager
    """

    # =========================================================
    # Build
    # =========================================================

    def _build_ideas_tab(self, parent=None):
        """
        Build the Idea Generator tab.

        parent=None → council mode (creates Frame and adds to self.nb)
        parent=Frame → standalone mode (builds into given frame)
        """
        if parent is None:
            self.tab_ideas = ttk.Frame(self.nb)
            self.nb.add(self.tab_ideas, text="💡 Ideas")
            _root = self.tab_ideas
        else:
            _root = parent

        # ── State ─────────────────────────────────────────────
        self._idea_store    = IdeaStore(self.vault_dir)
        self._idea_loop:    Optional[IdeationLoop] = None
        self._idea_settings = self._load_idea_settings()
        self._idea_cache:   list = []   # list of index dicts for fast display

        # ── Header ────────────────────────────────────────────
        hdr = ttk.Frame(_root)
        hdr.pack(fill="x", padx=10, pady=(8, 4))
        ttk.Label(hdr, text="Overnight Idea Generator",
                  foreground=PALETTE["blue"],
                  font=("", 11, "bold")).pack(side="left")
        ttk.Label(hdr, text="  Continuous ideation — run it, sleep, wake up to a list",
                  foreground=PALETTE["overlay"]).pack(side="left")

        # ── Two-column layout ─────────────────────────────────
        body = ttk.Frame(_root)
        body.pack(fill="both", expand=True, padx=10, pady=(0, 6))
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)

        left  = ttk.Frame(body)
        right = ttk.Frame(body)
        left.grid( row=0, column=0, sticky="nsew", padx=(0, 4))
        right.grid(row=0, column=1, sticky="nsew")

        # ── Left: settings + controls + log ───────────────────
        self._build_ideas_settings(left)
        self._build_ideas_controls(left)
        self._build_ideas_log(left)

        # ── Right: idea list + detail ─────────────────────────
        self._build_ideas_list(right)
        self._build_ideas_detail(right)

        # ── GitHub panel (bottom of left column) ──────────────
        self._build_ideas_github(left)

        # ── Load existing ideas ───────────────────────────────
        self._ideas_list_refresh()

    # ── Settings panel ────────────────────────────────────────

    # Quality descriptions shown next to the model dropdowns
    _MODEL_QUALITY = {
        "ideator":    "standard creative model — built for this role",
        "pitcher":    "standard pitch model — built for this role",
        "sage":       "deep reasoning — slower, most fleshed-out ideas",
        "strategist": "analytical framing — strong on structure & positioning",
        "director":   "production-aware — detailed outlines & shooting notes",
        "writer":     "narrative focus — strong hook writing & flow",
        "content":    "packaging focus — best title/thumbnail concepts",
        "algorithm":  "retention-optimised — hook mechanics & CTR framing",
        "judge":      "critical — will reject weak ideas, fewer but sharper",
        "chat":       "fast / light — quick ideas, least detail",
    }

    def _build_ideas_settings(self, parent):
        sf = ttk.LabelFrame(parent, text="Settings")
        sf.pack(fill="x", pady=(0, 4))

        # Seeds — multi-line with calibration button
        seed_hdr = ttk.Frame(sf)
        seed_hdr.pack(fill="x", padx=8, pady=(6, 0))
        ttk.Label(seed_hdr, text="Niche / seed topics:",
                  foreground=PALETTE["subtext"]).pack(side="left")
        ttk.Button(seed_hdr, text="🎯 Calibrate with AI",
                   command=self._ideas_calibrate_seed).pack(side="right")

        seed_frame = ttk.Frame(sf)
        seed_frame.pack(fill="x", padx=8, pady=(2, 2))
        self._idea_seeds_text = tk.Text(
            seed_frame, height=4, wrap="word",
            bg=PALETTE.get("surface", "#1e1e2e"),
            fg=PALETTE.get("text", "#cdd6f4"),
            insertbackground=PALETTE.get("text", "#cdd6f4"),
            font=("Consolas", 9), relief="flat",
        )
        seeds_sb = ttk.Scrollbar(seed_frame, command=self._idea_seeds_text.yview)
        self._idea_seeds_text.configure(yscrollcommand=seeds_sb.set)
        seeds_sb.pack(side="right", fill="y")
        self._idea_seeds_text.pack(fill="x", expand=True)
        self._idea_seeds_text.insert("1.0", self._idea_settings.seeds)
        ttk.Label(sf,
                  text="  Describe the vibe, format, and what to avoid — or click 🎯 to chat with the model",
                  foreground="#45475a", font=("", 9)).pack(anchor="w", padx=8)

        # Keep a StringVar alias so existing code that reads seeds still works
        self._idea_seeds_var = tk.StringVar()
        def _sync_seeds_var(*_):
            self._idea_seeds_var.set(
                self._idea_seeds_text.get("1.0", "end").strip())
        self._idea_seeds_text.bind("<<Modified>>", _sync_seeds_var)
        self._idea_seeds_text.bind("<KeyRelease>", _sync_seeds_var)

        row1 = ttk.Frame(sf)
        row1.pack(fill="x", padx=8, pady=(4, 2))

        # Style preference
        ttk.Label(row1, text="Style:").pack(side="left")
        self._idea_style_var = tk.StringVar(value=self._idea_settings.style)
        ttk.Combobox(row1, textvariable=self._idea_style_var,
                     values=["any", "educational", "entertainment",
                             "commentary", "tutorial", "vlog", "essay", "experiment",
                             "presentation"],
                     state="readonly", width=14).pack(side="left", padx=6)

        # Cooldown between ideas — models always run to full completion first
        ttk.Label(row1, text="  Cooldown:").pack(side="left", padx=(8, 2))
        self._idea_interval_var = tk.StringVar(value=str(self._idea_settings.interval_s))
        ttk.Spinbox(row1, textvariable=self._idea_interval_var,
                    from_=0, to=600, increment=30, width=6).pack(side="left")
        ttk.Label(row1, text="s between ideas").pack(side="left", padx=(2, 0))

        row2 = ttk.Frame(sf)
        row2.pack(fill="x", padx=8, pady=(2, 4))

        # Max per session
        ttk.Label(row2, text="Max ideas:").pack(side="left")
        self._idea_max_var = tk.StringVar(value=str(self._idea_settings.max_per_session))
        ttk.Spinbox(row2, textvariable=self._idea_max_var,
                    from_=1, to=500, increment=10, width=6).pack(side="left", padx=6)

        # Checkboxes
        self._idea_use_style_var = tk.BooleanVar(value=self._idea_settings.use_content_style)
        ttk.Checkbutton(row2,
                        text="Use content style context",
                        variable=self._idea_use_style_var).pack(side="left", padx=(10, 0))

        # ── Model selection ────────────────────────────────────
        mf = ttk.LabelFrame(sf, text="Models  (affects idea detail)")
        mf.pack(fill="x", padx=8, pady=(4, 6))

        # Determine available role names from self.personalities (or fallback)
        _available = sorted(
            getattr(self, "personalities", {}).keys()
        ) or ["ideator", "pitcher", "sage", "writer", "strategist",
              "director", "content", "algorithm", "chat"]

        saved_cfg = self._load_idea_model_config()

        mrow1 = ttk.Frame(mf)
        mrow1.pack(fill="x", padx=6, pady=(4, 2))
        ttk.Label(mrow1, text="Ideator:", width=8).pack(side="left")
        self._idea_ideator_role_var = tk.StringVar(
            value=saved_cfg.get("ideator_role", "ideator"))
        ideator_cb = ttk.Combobox(
            mrow1, textvariable=self._idea_ideator_role_var,
            values=_available, state="readonly", width=14)
        ideator_cb.pack(side="left", padx=4)
        self._idea_ideator_quality_var = tk.StringVar()
        ttk.Label(mrow1, textvariable=self._idea_ideator_quality_var,
                  foreground=PALETTE["subtext"], font=("", 9)).pack(side="left", padx=4)

        mrow2 = ttk.Frame(mf)
        mrow2.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Label(mrow2, text="Pitcher:", width=8).pack(side="left")
        self._idea_pitcher_role_var = tk.StringVar(
            value=saved_cfg.get("pitcher_role", "pitcher"))
        pitcher_cb = ttk.Combobox(
            mrow2, textvariable=self._idea_pitcher_role_var,
            values=_available, state="readonly", width=14)
        pitcher_cb.pack(side="left", padx=4)
        self._idea_pitcher_quality_var = tk.StringVar()
        ttk.Label(mrow2, textvariable=self._idea_pitcher_quality_var,
                  foreground=PALETTE["subtext"], font=("", 9)).pack(side="left", padx=4)

        # Wire quality label updates
        def _update_quality_labels(*_):
            personalities = getattr(self, "personalities", {})

            def _label(role):
                desc = self._MODEL_QUALITY.get(role, "custom model")
                model = personalities.get(role)
                if model and _CE_OK:
                    size = _get_model_size_label(model)
                    return f"[{size}]  {desc}"
                return desc

            self._idea_ideator_quality_var.set(_label(self._idea_ideator_role_var.get()))
            self._idea_pitcher_quality_var.set(_label(self._idea_pitcher_role_var.get()))

        self._idea_ideator_role_var.trace_add("write", _update_quality_labels)
        self._idea_pitcher_role_var.trace_add("write", _update_quality_labels)
        _update_quality_labels()  # set initial labels

        # ── Brainstorm contributors ────────────────────────────
        bf = ttk.LabelFrame(sf, text="Brainstorm contributors  (propose ideas before ideator evaluates)")
        bf.pack(fill="x", padx=8, pady=(0, 6))

        ttk.Label(bf,
                  text="Each ticked model throws in a raw concept. "
                       "Ideator picks the best foundation.",
                  foreground=PALETTE["subtext"], font=("", 9)).pack(
                      anchor="w", padx=8, pady=(4, 2))

        saved_brainstorm = saved_cfg.get("brainstorm_roles",
                                         ["writer", "strategist", "director",
                                          "content", "algorithm", "sage"])
        self._brainstorm_vars: Dict[str, tk.BooleanVar] = {}

        # Only show roles that are useful for brainstorming
        _brainstorm_candidates = [
            r for r in _available
            if r not in {"eye", "cutter", "coach", "chat", "ideator", "pitcher", "judge"}
        ]
        # Fall back to defaults if no personalities loaded yet
        if not _brainstorm_candidates:
            _brainstorm_candidates = ["writer", "strategist", "director",
                                       "content", "algorithm", "sage"]

        # Two-column grid of checkboxes
        cb_frame = ttk.Frame(bf)
        cb_frame.pack(fill="x", padx=8, pady=(0, 6))
        for idx, role in enumerate(_brainstorm_candidates):
            var = tk.BooleanVar(value=(role in saved_brainstorm))
            self._brainstorm_vars[role] = var
            ttk.Checkbutton(
                cb_frame,
                text=f"{role}  — {self._MODEL_QUALITY.get(role, 'custom')}",
                variable=var,
            ).grid(row=idx // 2, column=idx % 2, sticky="w", padx=4, pady=1)

    # ── Controls ──────────────────────────────────────────────

    def _build_ideas_controls(self, parent):
        cf = ttk.Frame(parent)
        cf.pack(fill="x", pady=(0, 4))

        self._idea_start_btn = ttk.Button(cf, text="▶  Start Loop",
                                           command=self._ideas_start)
        self._idea_start_btn.pack(side="left")

        self._idea_pause_btn = ttk.Button(cf, text="⏸  Pause",
                                           command=self._ideas_pause,
                                           state="disabled")
        self._idea_pause_btn.pack(side="left", padx=4)

        ttk.Button(cf, text="■  Stop",
                   command=self._ideas_stop).pack(side="left")

        ttk.Separator(cf, orient="vertical").pack(side="left", fill="y", padx=8)

        ttk.Button(cf, text="💡  Generate One Now",
                   command=self._ideas_generate_one).pack(side="left")

        self._idea_status_var = tk.StringVar(value="Idle")
        self._idea_status_lbl = ttk.Label(cf, textvariable=self._idea_status_var,
                                           foreground=PALETTE["blue"])
        self._idea_status_lbl.pack(side="left", padx=10)

    # ── Progress log ──────────────────────────────────────────

    def _build_ideas_log(self, parent):
        ttk.Label(parent, text="Progress:").pack(anchor="w")
        lf = ttk.Frame(parent)
        lf.pack(fill="both", expand=True, pady=(0, 4))
        self._idea_log = tk.Text(
            lf,
            bg="#11111b", fg=PALETTE["text"],
            font=("Consolas", 9), state="disabled",
            relief="flat", wrap="word", height=8,
        )
        log_sb = ttk.Scrollbar(lf, command=self._idea_log.yview)
        self._idea_log.configure(yscrollcommand=log_sb.set)
        log_sb.pack(side="right", fill="y")
        self._idea_log.pack(fill="both", expand=True)
        self._idea_log.tag_config("ok",   foreground=PALETTE["green"])
        self._idea_log.tag_config("err",  foreground=PALETTE["red"])
        self._idea_log.tag_config("warn", foreground=PALETTE["yellow"])
        self._idea_log.tag_config("hdr",  foreground=PALETTE["blue"],
                                   font=("Consolas", 9, "bold"))

    # ── Idea list (right panel top) ───────────────────────────

    # ── GitHub panel ──────────────────────────────────────────

    def _build_ideas_github(self, parent):
        gf = ttk.LabelFrame(parent, text="🐙 GitHub Sync")
        gf.pack(fill="x", pady=(4, 0))

        row1 = ttk.Frame(gf)
        row1.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Label(row1, text="Remote URL:").pack(side="left")
        git_cfg = self._load_git_config()
        self._git_remote_var = tk.StringVar(
            value=git_cfg.get("remote", "https://github.com/Infernoplaystuf/Council.git"))
        ttk.Entry(row1, textvariable=self._git_remote_var,
                  width=50).pack(side="left", padx=6, fill="x", expand=True)

        row2 = ttk.Frame(gf)
        row2.pack(fill="x", padx=8, pady=(0, 2))
        ttk.Label(row2, text="Branch:").pack(side="left")
        self._git_branch_var = tk.StringVar(
            value=git_cfg.get("branch", "main"))
        ttk.Entry(row2, textvariable=self._git_branch_var,
                  width=12).pack(side="left", padx=6)

        ttk.Label(row2, text="  Push:").pack(side="left", padx=(8, 2))
        self._git_filter_var = tk.StringVar(
            value=git_cfg.get("filter", "all"))
        ttk.Combobox(row2, textvariable=self._git_filter_var,
                     values=["all", "saved+", "in-production"],
                     state="readonly", width=14).pack(side="left")
        ttk.Label(row2,
                  text="  all=everything  saved+=curated only",
                  foreground="#45475a", font=("", 9)).pack(side="left", padx=4)

        # Auto-push row
        row3 = ttk.Frame(gf)
        row3.pack(fill="x", padx=8, pady=(2, 2))
        self._git_autopush_var = tk.BooleanVar(
            value=git_cfg.get("auto_push", False))
        ttk.Checkbutton(row3, text="Auto-push every",
                        variable=self._git_autopush_var).pack(side="left")
        self._git_autopush_every_var = tk.StringVar(
            value=str(git_cfg.get("auto_push_every", 5)))
        ttk.Spinbox(row3, textvariable=self._git_autopush_every_var,
                    from_=1, to=100, increment=1, width=4).pack(side="left", padx=4)
        ttk.Label(row3,
                  text="ideas  — pushes silently while the loop runs",
                  foreground=PALETTE["subtext"]).pack(side="left")

        # Manual push row
        row4 = ttk.Frame(gf)
        row4.pack(fill="x", padx=8, pady=(2, 6))
        ttk.Button(row4, text="🐙 Push now",
                   command=self._ideas_git_push).pack(side="left")
        ttk.Button(row4, text="Save config",
                   command=self._ideas_git_save_config).pack(side="left", padx=6)
        self._git_status_var = tk.StringVar(value="")
        ttk.Label(row4, textvariable=self._git_status_var,
                  foreground=PALETTE["blue"]).pack(side="left", padx=6)

    def _build_ideas_list(self, parent):
        lf = ttk.LabelFrame(parent, text="Generated Ideas")
        lf.pack(fill="x", pady=(0, 4))

        # Filter bar
        frow = ttk.Frame(lf)
        frow.pack(fill="x", padx=6, pady=(4, 2))
        ttk.Label(frow, text="Filter:").pack(side="left")
        self._idea_filter_var = tk.StringVar()
        self._idea_filter_var.trace_add("write", lambda *_: self._ideas_list_refresh())
        ttk.Entry(frow, textvariable=self._idea_filter_var,
                  width=20).pack(side="left", padx=4)
        ttk.Label(frow, text="Status:").pack(side="left", padx=(6, 2))
        self._idea_status_filter_var = tk.StringVar(value="all")
        ttk.Combobox(frow, textvariable=self._idea_status_filter_var,
                     values=["all", "new", "saved", "archived", "in-production"],
                     state="readonly", width=14).pack(side="left")
        self._idea_status_filter_var.trace_add(
            "write", lambda *_: self._ideas_list_refresh())

        # Treeview
        tv_frame = ttk.Frame(lf)
        tv_frame.pack(fill="x", padx=6, pady=(0, 4))

        cols = ("Title", "Difficulty", "Status", "Rating", "When")
        self._ideas_tv = ttk.Treeview(
            tv_frame, columns=cols, show="headings",
            height=8, selectmode="browse",
        )
        self._ideas_tv.heading("Title",      text="Title",      anchor="w")
        self._ideas_tv.heading("Difficulty", text="Diff",       anchor="center")
        self._ideas_tv.heading("Status",     text="Status",     anchor="center")
        self._ideas_tv.heading("Rating",     text="★",          anchor="center")
        self._ideas_tv.heading("When",       text="When",       anchor="center")
        self._ideas_tv.column("Title",      width=240, stretch=True,  anchor="w")
        self._ideas_tv.column("Difficulty", width=50,  stretch=False, anchor="center")
        self._ideas_tv.column("Status",     width=80,  stretch=False, anchor="center")
        self._ideas_tv.column("Rating",     width=40,  stretch=False, anchor="center")
        self._ideas_tv.column("When",       width=90,  stretch=False, anchor="center")

        # Status → foreground colour
        self._ideas_tv.tag_configure("new",          foreground=PALETTE["text"])
        self._ideas_tv.tag_configure("saved",        foreground=PALETTE["green"])
        self._ideas_tv.tag_configure("archived",     foreground=PALETTE["overlay"])
        self._ideas_tv.tag_configure("in-production",foreground=PALETTE["blue"],
                                      font=("", 9, "bold"))
        # Difficulty → subtle background tint (does NOT conflict with status foreground)
        self._ideas_tv.tag_configure("diff_easy",   background="#1a3028")
        self._ideas_tv.tag_configure("diff_medium", background="#2e2410")
        self._ideas_tv.tag_configure("diff_hard",   background="#2e1212")

        tv_sb = ttk.Scrollbar(tv_frame, orient="vertical",
                               command=self._ideas_tv.yview)
        self._ideas_tv.configure(yscrollcommand=tv_sb.set)
        tv_sb.pack(side="right", fill="y")
        self._ideas_tv.pack(fill="x", expand=True)
        self._ideas_tv.bind("<<TreeviewSelect>>", self._ideas_on_select)

        # Action row
        act_row = ttk.Frame(lf)
        act_row.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Button(act_row, text="⭐ Save",
                   command=lambda: self._ideas_set_status("saved")).pack(side="left")
        ttk.Button(act_row, text="🎬 In Production",
                   command=lambda: self._ideas_set_status("in-production")).pack(
                       side="left", padx=4)
        ttk.Button(act_row, text="📦 Archive",
                   command=lambda: self._ideas_set_status("archived")).pack(
                       side="left")
        ttk.Button(act_row, text="🗑 Delete",
                   command=self._ideas_delete).pack(side="left", padx=(10, 0))
        ttk.Button(act_row, text="🧹 Purge incomplete",
                   command=self._ideas_purge_incomplete).pack(side="left", padx=(4, 0))
        ttk.Separator(act_row, orient="vertical").pack(
            side="left", fill="y", padx=8)

        # Star rating
        ttk.Label(act_row, text="Rate:").pack(side="left")
        for star in range(1, 6):
            ttk.Button(act_row, text=str(star), width=2,
                       command=lambda s=star: self._ideas_set_rating(s)).pack(
                           side="left")

        ttk.Separator(act_row, orient="vertical").pack(
            side="left", fill="y", padx=8)
        ttk.Button(act_row, text="✨ Refine",
                   command=self._ideas_refine_selected).pack(side="left")
        ttk.Button(act_row, text="🖼 Thumbnail",
                   command=self._ideas_thumbnail_selected).pack(side="left", padx=(4, 0))
        ttk.Button(act_row, text="🖼 All 4★+",
                   command=self._ideas_thumbnail_batch).pack(side="left", padx=(4, 0))

        # Export / GitHub
        ttk.Separator(act_row, orient="vertical").pack(
            side="left", fill="y", padx=8)
        ttk.Button(act_row, text="📋 Export MD",
                   command=self._ideas_export).pack(side="left")
        ttk.Button(act_row, text="🐙 Push to GitHub",
                   command=self._ideas_git_push).pack(side="left", padx=4)
        ttk.Button(act_row, text="Send to Council",
                   command=self._ideas_send_to_council).pack(side="left", padx=4)

    # ── Idea detail panel ─────────────────────────────────────

    def _build_ideas_detail(self, parent):
        df = ttk.LabelFrame(parent, text="Idea Detail")
        df.pack(fill="both", expand=True, pady=(0, 4))

        txt_frame = ttk.Frame(df)
        txt_frame.pack(fill="both", expand=True, padx=6, pady=4)
        self._idea_detail = self._make_text(
            txt_frame, wrap="word", state="disabled", height=18)
        d_sb = ttk.Scrollbar(txt_frame, command=self._idea_detail.yview)
        self._idea_detail.configure(yscrollcommand=d_sb.set)
        d_sb.pack(side="right", fill="y")
        self._idea_detail.pack(fill="both", expand=True)

        # Colour tags for the detail view
        self._idea_detail.tag_config(
            "section", foreground=PALETTE["blue"],
            font=("Consolas", 9, "bold"))
        self._idea_detail.tag_config("pos",  foreground=PALETTE["green"])
        self._idea_detail.tag_config("warn", foreground=PALETTE["yellow"])
        self._idea_detail.tag_config("meta", foreground=PALETTE["subtext"],
                                      font=("Consolas", 8))

        # Notes field below detail
        note_row = ttk.Frame(df)
        note_row.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Label(note_row, text="Your notes:",
                  foreground=PALETTE["subtext"]).pack(side="left")
        self._idea_notes_var = tk.StringVar()
        ttk.Entry(note_row, textvariable=self._idea_notes_var,
                  width=40).pack(side="left", padx=6, fill="x", expand=True)
        ttk.Button(note_row, text="Save note",
                   command=self._ideas_save_note).pack(side="left")

        # Thumbnail preview (hidden until an image exists)
        self._thumb_frame = ttk.LabelFrame(df, text="Thumbnail Preview")
        # Initially not packed — shown only when an image is available
        self._thumb_canvas = tk.Canvas(
            self._thumb_frame,
            width=384, height=216,       # 1280×720 scaled to 30%
            bg=PALETTE.get("surface", "#1e1e2e"),
            highlightthickness=0,
        )
        self._thumb_canvas.pack(padx=6, pady=6)
        self._thumb_img_ref = None   # keep reference to prevent GC

    # =========================================================
    # Ideation controls
    # =========================================================

    def _ideas_get_settings(self) -> IdeationSettings:
        """Read current settings from the UI."""
        brainstorm_roles = [
            role for role, var in getattr(self, "_brainstorm_vars", {}).items()
            if var.get()
        ]
        seeds_raw = (
            self._idea_seeds_text.get("1.0", "end").strip()
            if hasattr(self, "_idea_seeds_text")
            else self._idea_seeds_var.get().strip()
        )
        return IdeationSettings(
            seeds             = seeds_raw,
            style             = self._idea_style_var.get(),
            interval_s        = max(30, int(self._idea_interval_var.get() or 90)),
            max_per_session   = max(1, int(self._idea_max_var.get() or 50)),
            use_content_style = bool(self._idea_use_style_var.get()),
            brainstorm_roles  = brainstorm_roles,
        )

    def _ideas_resolve_models(self):
        """Return (ideator_model, pitcher_model) from the selected roles."""
        personalities = getattr(self, "personalities", {})
        ir = getattr(self, "_idea_ideator_role_var", None)
        pr = getattr(self, "_idea_pitcher_role_var", None)
        ideator_role = ir.get() if ir else "ideator"
        pitcher_role = pr.get() if pr else "pitcher"
        ideator_m = personalities.get(ideator_role) or getattr(self, ideator_role, None)
        pitcher_m = personalities.get(pitcher_role) or getattr(self, pitcher_role, None)
        return ideator_m, pitcher_m

    def _ideas_make_loop(self) -> Optional[IdeationLoop]:
        """Build an IdeationLoop from current models and settings."""
        ideator_m, pitcher_m = self._ideas_resolve_models()
        if not ideator_m or not pitcher_m:
            messagebox.showwarning(
                "Ideas",
                "Ideator and/or Pitcher models are not available.\n"
                "Make sure council_engine is running and both roles are built.",
                parent=self.winfo_toplevel())
            return None

        settings = self._ideas_get_settings()
        self._save_idea_settings(settings)
        style_mgr    = getattr(self, "_content_style", None)
        personalities = getattr(self, "personalities", {})

        # Resolve brainstorm models — only roles that exist and aren't excluded
        brainstorm_models = {
            role: personalities[role]
            for role in settings.brainstorm_roles
            if role in personalities
        }

        return IdeationLoop(
            ideator_model        = ideator_m,
            pitcher_model        = pitcher_m,
            store                = self._idea_store,
            settings             = settings,
            progress_cb          = self._ideas_log_append,
            idea_cb              = self._on_new_idea,
            content_style_manager= style_mgr,
            brainstorm_models    = brainstorm_models,
        )

    def _ideas_start(self):
        if self._idea_loop and self._idea_loop.running:
            return
        loop = self._ideas_make_loop()
        if loop is None:
            return
        self._save_idea_model_config()
        self._idea_loop = loop
        self._idea_loop.start()
        self._idea_start_btn.configure(state="disabled")
        self._idea_pause_btn.configure(state="normal")
        self._idea_status_var.set("Running…")

    def _ideas_pause(self):
        if not self._idea_loop:
            return
        self._idea_loop.pause()
        lbl = "▶  Resume" if self._idea_loop.paused else "⏸  Pause"
        self._idea_pause_btn.configure(text=lbl)
        self._idea_status_var.set(
            "Paused" if self._idea_loop.paused else "Running…")

    def _ideas_stop(self):
        if self._idea_loop:
            self._idea_loop.stop()
        # Wait for thread to finish then re-enable start
        def _check():
            if self._idea_loop and self._idea_loop.running:
                self.after(300, _check)
            else:
                self._idea_start_btn.configure(state="normal")
                self._idea_pause_btn.configure(state="disabled",
                                                text="⏸  Pause")
                self._idea_status_var.set("Stopped")
        self.after(300, _check)

    def _ideas_generate_one(self):
        """Generate one idea right now, outside the loop (synchronous in a thread)."""
        ideator_m, pitcher_m = self._ideas_resolve_models()
        if not ideator_m or not pitcher_m:
            messagebox.showwarning(
                "Ideas",
                "Ideator and Pitcher models required.",
                parent=self.winfo_toplevel())
            return

        settings  = self._ideas_get_settings()
        style_mgr = getattr(self, "_content_style", None)
        loop = IdeationLoop(
            ideator_model        = ideator_m,
            pitcher_model        = pitcher_m,
            store                = self._idea_store,
            settings             = settings,
            progress_cb          = self._ideas_log_append,
            idea_cb              = self._on_new_idea,
            content_style_manager= style_mgr,
        )
        self._ideas_log_append("▶ Generating one idea…")
        threading.Thread(target=loop.run_one_now, daemon=True).start()

    # ── Callback from IdeationLoop (called from background thread) ─

    def _on_new_idea(self, item: IdeaItem):
        """Called by IdeationLoop when a new idea is ready."""
        self.after(0, lambda i=item: self._on_new_idea_ui(i))

    def _on_new_idea_ui(self, item: IdeaItem):
        """UI-thread callback — refresh list, update status, auto-push if due."""
        self._ideas_list_refresh()
        session_count = self._idea_loop.count if self._idea_loop else 0
        total_count   = self._idea_store.count()
        self._idea_status_var.set(
            f"Running…  {total_count} ideas total"
            + (f"  (this session: {session_count})" if self._idea_loop else ""))

        # Auto-push check
        if (self._git_autopush_var.get()
                and session_count > 0
                and self._git_remote_var.get().strip()):
            every = max(1, int(self._git_autopush_every_var.get() or 5))
            if session_count % every == 0:
                self._ideas_log_append(
                    f"  🐙 Auto-push triggered (every {every} ideas, "
                    f"session count: {session_count})")
                # Run in background thread so it doesn't block the UI
                sid = self._idea_loop.session_id if self._idea_loop else ""
                threading.Thread(
                    target=self._ideas_git_push_worker,
                    args=(
                        self._git_remote_var.get().strip(),
                        self._git_branch_var.get().strip() or "main",
                        self._git_filter_var.get(),
                        sid,
                    ),
                    daemon=True,
                ).start()

    # =========================================================
    # Log
    # =========================================================

    def _ideas_log_append(self, msg: str):
        def _do():
            self._idea_log.configure(state="normal")
            tag = ("ok"   if "✓" in msg else
                   "err"  if "✗" in msg else
                   "warn" if "⚠" in msg else
                   "hdr"  if msg.startswith("▶") or msg.startswith("■") else "")
            self._idea_log.insert("end", msg + "\n", tag)
            self._idea_log.see("end")
            self._idea_log.configure(state="disabled")
        self.after(0, _do)

    # =========================================================
    # Idea list
    # =========================================================

    def _ideas_list_refresh(self):
        """Redraw the treeview from the store index."""
        self._ideas_tv.delete(*self._ideas_tv.get_children())
        index   = self._idea_store.list_index()
        ftext   = self._idea_filter_var.get().lower()
        fstatus = self._idea_status_filter_var.get()

        shown = []
        for entry in index:
            if fstatus != "all" and entry.get("status") != fstatus:
                continue
            if ftext and ftext not in entry.get("title", "").lower():
                continue
            shown.append(entry)

        self._idea_cache = shown
        for entry in shown:
            ts    = entry.get("generated_at", "")[:16].replace("T", " ")
            stars = "★" * entry.get("rating", 0) if entry.get("rating", 0) else "·"
            diff  = entry.get("difficulty", "")
            diff_key = diff.split()[0].lower() if diff else ""
            diff_label = {"easy": "easy", "medium": "med", "hard": "hard"}.get(
                diff_key, "—")
            diff_tag = {"easy": "diff_easy", "medium": "diff_medium",
                        "hard": "diff_hard"}.get(diff_key, "")
            status = entry.get("status", "new")
            row_tags = (status, diff_tag) if diff_tag else (status,)
            self._ideas_tv.insert(
                "", "end",
                iid=entry["id"],
                values=(entry.get("title", "?"), diff_label,
                        f"{entry.get('status','new')}", stars, ts),
                tags=row_tags,
            )

        count = self._idea_store.count()
        ttk.Label  # just ensure module is available
        # Update status label
        self._idea_status_var.set(
            f"{count} ideas total  ({len(shown)} shown)"
            + (f"  |  Loop: {'running' if self._idea_loop and self._idea_loop.running else 'idle'}")
        )

    def _ideas_selected_id(self) -> Optional[str]:
        sel = self._ideas_tv.selection()
        return sel[0] if sel else None

    def _ideas_on_select(self, event=None):
        idea_id = self._ideas_selected_id()
        if not idea_id:
            return

        # Auto-save notes for the previously shown idea before switching
        prev_id = getattr(self, "_idea_currently_shown_id", None)
        if prev_id and prev_id != idea_id:
            typed = self._idea_notes_var.get().strip()
            prev_loaded = getattr(self, "_idea_loaded_notes", "")
            if typed != prev_loaded:
                prev_item = self._idea_store.load(prev_id)
                if prev_item:
                    prev_item.notes = typed
                    self._idea_store.save(prev_item)
                    self._ideas_log_append(
                        f"  ✓ Notes auto-saved for: {prev_item.display_title}")

        item = self._idea_store.load(idea_id)
        if item:
            self._idea_currently_shown_id = idea_id
            self._idea_loaded_notes = item.notes or ""
            self._ideas_show_detail(item)
            self._idea_notes_var.set(self._idea_loaded_notes)

    def _ideas_show_detail(self, item: IdeaItem):
        """Populate the detail text panel with a colour-coded idea card."""
        txt = self._idea_detail
        txt.configure(state="normal")
        txt.delete("1.0", "end")

        def _sec(header: str, content: str, tag: str = ""):
            if not content:
                return
            txt.insert("end", f"\n{header}\n", "section")
            txt.insert("end", content.strip() + "\n", tag)

        def _list(header: str, items: list):
            if not items:
                return
            txt.insert("end", f"\n{header}\n", "section")
            for it in items:
                txt.insert("end", f"  • {it}\n", "warn")

        # Meta line
        refined_badge = f"  |  ✨ refined from {item.refined_from}" if item.refined_from else ""
        txt.insert("end",
                   f"ID: {item.id}  |  {item.generated_at[:16]}  |  "
                   f"{item.status_icon} {item.status}  |  "
                   f"★ {item.rating or '–'}  |  "
                   f"{item.difficulty or '–'}"
                   f"{refined_badge}\n",
                   "meta")

        _sec("TITLE:", item.title)
        _sec("HOOK ANGLE (raw idea):", item.hook_angle, "pos")
        _sec("HOOK (production):", item.hook)
        _sec("EMOTIONAL TRIGGER:", item.emotional_trigger, "warn")
        _sec("FORMAT:", item.format_suggestion)
        _sec("PREMISE:", item.premise)

        if item.outline:
            txt.insert("end", "\nOUTLINE:\n", "section")
            for line in item.outline:
                txt.insert("end", f"  {line}\n")

        _sec("THUMBNAIL CONCEPT:", item.thumbnail_concept, "pos")
        _sec("TARGET AUDIENCE:", item.target_audience)
        _sec("WHY IT WORKS:", item.why_it_works)

        _list("TITLE VARIANTS:", item.title_variants)

        if item.tags:
            txt.insert("end", "\nTAGS:\n", "section")
            txt.insert("end", "  " + ", ".join(item.tags) + "\n", "meta")

        _sec("ESTIMATED LENGTH:", item.estimated_length)
        _sec("PRODUCTION NOTES:", item.production_notes)

        if item.niche_seed:
            txt.insert("end", f"\nSEED USED: {item.niche_seed}\n", "meta")
        if item.notes:
            txt.insert("end", f"\nYOUR NOTES:\n{item.notes}\n", "warn")

        txt.configure(state="disabled")

        # Show thumbnail preview if an image has been generated
        self._ideas_load_thumbnail(item)

    def _ideas_load_thumbnail(self, item: IdeaItem):
        """Show the generated thumbnail image in the preview canvas, or hide it."""
        if not item.thumbnail_image_path:
            self._thumb_frame.pack_forget()
            return
        img_path = self._idea_store.ideas_dir.parent / "idea_images" / item.thumbnail_image_path
        if not img_path.exists():
            self._thumb_frame.pack_forget()
            return
        try:
            if _PIL_OK:
                pil_img = _PILImage.open(img_path)
                pil_img.thumbnail((384, 216), _PILImage.LANCZOS)
                photo = _ImageTk.PhotoImage(pil_img)
            else:
                # tk.PhotoImage only supports GIF/PNG natively
                photo = tk.PhotoImage(file=str(img_path))
                # Scale down if needed (PhotoImage subsample)
                w, h = photo.width(), photo.height()
                if w > 384:
                    factor = max(1, w // 384)
                    photo = photo.subsample(factor, factor)
            self._thumb_img_ref = photo
            self._thumb_canvas.configure(
                width=photo.width(), height=photo.height())
            self._thumb_canvas.delete("all")
            self._thumb_canvas.create_image(0, 0, anchor="nw", image=photo)
            self._thumb_frame.pack(fill="x", padx=6, pady=(0, 4))
        except Exception:
            self._thumb_frame.pack_forget()

    # =========================================================
    # Per-idea actions
    # =========================================================

    def _ideas_set_status(self, new_status: str):
        idea_id = self._ideas_selected_id()
        if not idea_id:
            return
        item = self._idea_store.load(idea_id)
        if not item:
            return
        item.status = new_status
        self._idea_store.save(item)
        self._ideas_list_refresh()
        self._ideas_show_detail(item)

    def _ideas_set_rating(self, stars: int):
        idea_id = self._ideas_selected_id()
        if not idea_id:
            return
        item = self._idea_store.load(idea_id)
        if not item:
            return
        item.rating = stars
        self._idea_store.save(item)
        self._ideas_list_refresh()

    def _ideas_save_note(self):
        idea_id = self._ideas_selected_id()
        if not idea_id:
            return
        item = self._idea_store.load(idea_id)
        if not item:
            return
        item.notes = self._idea_notes_var.get().strip()
        self._idea_store.save(item)
        self._idea_loaded_notes = item.notes   # keep tracker in sync
        self._ideas_log_append(f"✓ Note saved for: {item.display_title}")

    def _ideas_delete(self):
        idea_id = self._ideas_selected_id()
        if not idea_id:
            return
        item = self._idea_store.load(idea_id)
        if not item:
            return
        if not messagebox.askyesno(
                "Delete Idea",
                f"Permanently delete:\n{item.display_title}?",
                parent=self.winfo_toplevel()):
            return
        self._idea_store.delete(idea_id)
        self._idea_detail.configure(state="normal")
        self._idea_detail.delete("1.0", "end")
        self._idea_detail.configure(state="disabled")
        self._ideas_list_refresh()

    # =========================================================
    # Thumbnail generation
    # =========================================================

    def _thumb_gen(self) -> Optional["ThumbnailGenerator"]:
        """Return a cached ThumbnailGenerator, or None if image_engine unavailable."""
        if not _IMG_OK:
            return None
        if not hasattr(self, "_thumbnail_generator"):
            self._thumbnail_generator = ThumbnailGenerator(
                Path(self.vault_dir))
        return self._thumbnail_generator

    def _ideas_thumbnail_selected(self):
        """Generate a thumbnail for the currently selected idea (must be 4+ stars)."""
        idea_id = self._ideas_selected_id()
        if not idea_id:
            messagebox.showinfo("Thumbnail",
                "Select an idea first.", parent=self.winfo_toplevel())
            return
        item = self._idea_store.load(idea_id)
        if not item:
            return
        if item.rating < 4:
            messagebox.showinfo("Thumbnail",
                f"Thumbnails are only generated for ideas rated 4 stars or above.\n"
                f"This idea is rated {item.rating or 0} star(s).\n"
                f"Rate it 4 or 5 stars first.",
                parent=self.winfo_toplevel())
            return
        gen = self._thumb_gen()
        if not gen:
            messagebox.showwarning("Thumbnail",
                "image_engine.py not found — cannot generate thumbnails.",
                parent=self.winfo_toplevel())
            return
        if not gen.available:
            messagebox.showwarning("Thumbnail",
                "No image backend detected.\n\n"
                "Start ComfyUI (localhost:8188) or Automatic1111 (localhost:7860) "
                "then try again.",
                parent=self.winfo_toplevel())
            return
        if not item.thumbnail_concept:
            messagebox.showinfo("Thumbnail",
                "This idea has no thumbnail concept text to generate from.",
                parent=self.winfo_toplevel())
            return
        self._ideas_log_append(
            f"  🖼 Generating thumbnail for: {item.display_title} "
            f"[{gen.backend_label}]…")
        threading.Thread(
            target=self._thumb_worker,
            args=([item],),
            daemon=True,
        ).start()

    def _ideas_thumbnail_batch(self):
        """Generate thumbnails for all 4+ star ideas that don't have one yet."""
        gen = self._thumb_gen()
        if not gen:
            messagebox.showwarning("Thumbnail",
                "image_engine.py not found.", parent=self.winfo_toplevel())
            return
        if not gen.available:
            messagebox.showwarning("Thumbnail",
                "No image backend detected.\n\n"
                "Start ComfyUI (localhost:8188) or Automatic1111 (localhost:7860) "
                "then try again.",
                parent=self.winfo_toplevel())
            return
        all_items = self._idea_store.list_all()
        candidates = [
            i for i in all_items
            if i.rating >= 4
            and i.thumbnail_concept
            and not i.thumbnail_image_path
        ]
        if not candidates:
            messagebox.showinfo("Thumbnail",
                "No 4+ star ideas without thumbnails found.\n"
                "(Either none are rated 4+ yet, or all already have images.)",
                parent=self.winfo_toplevel())
            return
        if not messagebox.askyesno(
                "Generate Thumbnails",
                f"Generate thumbnails for {len(candidates)} idea(s) rated 4+ stars?\n"
                f"Backend: {gen.backend_label}\n\n"
                f"This may take several minutes. The council will log progress.",
                parent=self.winfo_toplevel()):
            return
        self._ideas_log_append(
            f"  🖼 Batch thumbnail generation: {len(candidates)} idea(s) "
            f"[{gen.backend_label}]…")
        threading.Thread(
            target=self._thumb_worker,
            args=(candidates,),
            daemon=True,
        ).start()

    def _thumb_worker(self, items: list):
        """Background thread: generate thumbnails for a list of IdeaItems."""
        gen = self._thumb_gen()
        if not gen:
            return
        done = 0
        for item in items:
            try:
                path = gen.generate(item.thumbnail_concept, item.id)
                if path:
                    item.thumbnail_image_path = path.name
                    self._idea_store.save(item)
                    done += 1
                    self.after(0, lambda i=item: self._on_thumbnail_ready(i))
                    self.after(0, lambda t=item.display_title: self._ideas_log_append(
                        f"  ✓ Thumbnail saved: {t}"))
                else:
                    self.after(0, lambda t=item.display_title: self._ideas_log_append(
                        f"  ✗ Thumbnail failed: {t}"))
            except Exception as e:
                self.after(0, lambda t=item.display_title, err=e:
                    self._ideas_log_append(f"  ✗ Thumbnail error ({t}): {err}"))
        self.after(0, lambda: self._ideas_log_append(
            f"  🖼 Thumbnail batch complete: {done}/{len(items)} generated."))

    def _on_thumbnail_ready(self, item: IdeaItem):
        """Called on UI thread when a thumbnail finishes — refresh if it's selected."""
        if self._ideas_selected_id() == item.id:
            self._ideas_load_thumbnail(item)

    # =========================================================
    # Seed calibration chat
    # =========================================================

    _SEED_CALIBRATOR_PROMPT = """\
You are a seed calibrator for a video idea generator. Your job is to understand
exactly what kind of video concepts a creator wants, then produce a precise seed
description the generator will use to stay on-topic.

CONVERSATION GOAL:
Ask focused questions to nail down:
1. The specific tone and format (parody, mock-academic, ranked list, essay, etc.)
2. Examples of content the creator loves — titles, shows, channels, jokes they like
3. What the generator keeps getting WRONG — common misinterpretations to correct

Ask one or two targeted questions at a time. Do not dump a list of 10 questions.
Once you understand the vibe clearly (usually 2-4 exchanges), produce the result.

WHEN READY, output EXACTLY this format — nothing after it:
───────────────────────────────
REFINED SEED:
[3-6 sentences describing the seed in enough detail that even wrong-footed models
will stay in the right lane. Include: tone, format, what the humour comes from,
what to avoid, and 1-2 concrete example titles that fit the seed perfectly.]

WHAT THIS WILL GENERATE:
[One sentence summary of the kind of ideas this will produce.]
───────────────────────────────

Do NOT produce the REFINED SEED block until you genuinely understand what the
creator wants. It is better to ask one more question than to produce a wrong seed.
"""

    def _ideas_calibrate_seed(self):
        """Open a chat window to calibrate the seed with the AI."""
        personalities = getattr(self, "personalities", {})
        model = (
            personalities.get("ideator")
            or personalities.get("writer")
            or personalities.get("sage")
        )
        if not model and not _CE_OK:
            messagebox.showinfo(
                "Calibrate Seed",
                "AI not available — type your seed description directly in the box.",
                parent=self.winfo_toplevel())
            return

        win = tk.Toplevel(self.winfo_toplevel())
        win.title("🎯 Calibrate Seed — Chat with the Model")
        win.geometry("700x560")
        win.configure(bg=PALETTE.get("base", "#1e1e2e"))
        win.grab_set()

        # Header
        hdr = ttk.Label(win,
            text="Describe what you're going for — the model will ask questions "
                 "until it understands, then produce a refined seed.",
            wraplength=660, font=("", 9), foreground=PALETTE["subtext"])
        hdr.pack(fill="x", padx=10, pady=(8, 4))

        # Chat log
        log_frame = ttk.Frame(win)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(0, 4))
        log = tk.Text(log_frame, wrap="word", state="disabled",
                      bg=PALETTE.get("mantle", "#181825"),
                      fg=PALETTE.get("text", "#cdd6f4"),
                      font=("Consolas", 9), relief="flat")
        log_sb = ttk.Scrollbar(log_frame, command=log.yview)
        log.configure(yscrollcommand=log_sb.set)
        log_sb.pack(side="right", fill="y")
        log.pack(fill="both", expand=True)
        log.tag_config("you",   foreground=PALETTE["blue"],  font=("Consolas", 9, "bold"))
        log.tag_config("model", foreground=PALETTE["green"], font=("Consolas", 9))
        log.tag_config("seed",  foreground=PALETTE["yellow"],font=("Consolas", 9, "bold"))
        log.tag_config("meta",  foreground=PALETTE["subtext"],font=("Consolas", 8, "italic"))

        # Current seed as context
        current_seed = self._idea_seeds_text.get("1.0", "end").strip()

        # Input row
        inp_frame = ttk.Frame(win)
        inp_frame.pack(fill="x", padx=10, pady=(0, 4))
        inp_var = tk.StringVar()
        inp = ttk.Entry(inp_frame, textvariable=inp_var, font=("Consolas", 10))
        inp.pack(side="left", fill="x", expand=True, padx=(0, 6))
        send_btn = ttk.Button(inp_frame, text="Send ↵", width=8)
        inp.pack(side="left", fill="x", expand=True, padx=(0, 6))

        # Accept-seed row (hidden until model produces REFINED SEED)
        accept_frame = ttk.Frame(win)
        self._calib_refined_seed = ""
        accept_lbl = ttk.Label(accept_frame,
            text="Model produced a refined seed ↑",
            foreground=PALETTE["yellow"])
        accept_lbl.pack(side="left")
        accept_btn = ttk.Button(accept_frame, text="✓ Use this seed",
            command=lambda: self._calib_accept_seed(win))
        accept_btn.pack(side="right")

        # Conversation history fed to the model
        history: list = []

        def _log_append(who: str, text: str, tag: str):
            log.configure(state="normal")
            log.insert("end", f"\n{who}:\n", tag)
            # If the text contains REFINED SEED, colour it specially
            if "REFINED SEED:" in text:
                parts = text.split("REFINED SEED:")
                log.insert("end", parts[0])
                log.insert("end", "REFINED SEED:" + parts[1], "seed")
            else:
                log.insert("end", text + "\n", tag)
            log.configure(state="disabled")
            log.see("end")

        def _model_reply(user_msg: str):
            history.append({"role": "user", "content": user_msg})
            # Build context string
            ctx = ""
            if current_seed:
                ctx = f"CURRENT SEED (what the creator has so far):\n{current_seed}\n\n"
            ctx += "\n".join(
                f"{'Creator' if m['role']=='user' else 'You'}: {m['content']}"
                for m in history
            )
            send_btn.configure(state="disabled")
            inp.configure(state="disabled")

            def _run():
                try:
                    if model:
                        # Temporarily override system prompt for calibration
                        import copy
                        cal_model = copy.copy(model)
                        cal_model.system_prompt = self._SEED_CALIBRATOR_PROMPT
                        reply = cal_model.respond(ctx)
                    else:
                        reply = ("I don't have a model available. "
                                 "Please type your seed directly in the settings box.")
                except Exception as e:
                    reply = f"[Error: {e}]"
                history.append({"role": "assistant", "content": reply})
                win.after(0, lambda: _on_reply(reply))

            threading.Thread(target=_run, daemon=True).start()

        def _on_reply(reply: str):
            _log_append("Model", reply, "model")
            send_btn.configure(state="normal")
            inp.configure(state="normal")
            inp.focus_set()
            # Check if model produced REFINED SEED
            if "REFINED SEED:" in reply:
                m = re.search(
                    r"REFINED SEED:\s*\n(.*?)(?=\nWHAT THIS WILL GENERATE:|$)",
                    reply, re.DOTALL)
                if m:
                    self._calib_refined_seed = m.group(1).strip()
                    accept_frame.pack(fill="x", padx=10, pady=(0, 8))

        def _send(*_):
            msg = inp_var.get().strip()
            if not msg:
                return
            inp_var.set("")
            _log_append("You", msg, "you")
            threading.Thread(
                target=_model_reply, args=(msg,), daemon=True).start()

        send_btn.configure(command=_send)
        inp.bind("<Return>", _send)

        # Kick off with a greeting from the model based on current seed
        _log_append("", "← Type what you're going for. The model will ask questions "
                    "until it understands, then produce a refined seed.", "meta")
        if current_seed:
            # Prime the model by showing it the current seed
            threading.Thread(
                target=_model_reply,
                args=(f"Here is my current seed: {current_seed}\n\n"
                      "What questions do you have to better understand what I want?",),
                daemon=True,
            ).start()
        else:
            threading.Thread(
                target=_model_reply,
                args=("I haven't set a seed yet. "
                      "Ask me what I'm going for.",),
                daemon=True,
            ).start()

        inp.focus_set()

    def _calib_accept_seed(self, win: tk.Toplevel):
        """Paste the refined seed back into the seeds text box and close the dialog."""
        if self._calib_refined_seed:
            self._idea_seeds_text.delete("1.0", "end")
            self._idea_seeds_text.insert("1.0", self._calib_refined_seed)
        win.destroy()

    def _ideas_purge_incomplete(self):
        """Delete every stored idea that is missing required pitcher sections."""
        all_entries = self._idea_store.list_index()
        to_delete = []
        for entry in all_entries:
            item = self._idea_store.load(entry["id"])
            if item is None:
                to_delete.append(entry["id"])
                continue
            fields = {
                "title":             item.title,
                "hook":              item.hook,
                "premise":           item.premise,
                "outline":           item.outline,
                "thumbnail_concept": item.thumbnail_concept,
                "target_audience":   item.target_audience,
                "why_it_works":      item.why_it_works,
                "title_variants":    item.title_variants,
                "difficulty":        item.difficulty,
                "estimated_length":  item.estimated_length,
            }
            if _missing_pitcher_fields(fields):
                to_delete.append(entry["id"])

        if not to_delete:
            messagebox.showinfo("Purge Incomplete",
                                "No incomplete ideas found — nothing to delete.",
                                parent=self.winfo_toplevel())
            return

        if not messagebox.askyesno(
                "Purge Incomplete",
                f"Permanently delete {len(to_delete)} incomplete idea(s)?\n"
                f"(Missing required sections like title, outline, thumbnail, etc.)",
                parent=self.winfo_toplevel()):
            return

        for idea_id in to_delete:
            self._idea_store.delete(idea_id)

        self._idea_detail.configure(state="normal")
        self._idea_detail.delete("1.0", "end")
        self._idea_detail.configure(state="disabled")
        self._ideas_list_refresh()
        self._ideas_log_append(
            f"🧹 Purged {len(to_delete)} incomplete idea(s).")

    def _ideas_refine_selected(self):
        """Run a refinement pass on the selected idea using the pitcher model."""
        idea_id = self._ideas_selected_id()
        if not idea_id:
            messagebox.showinfo("Refine", "Select an idea first.",
                                parent=self.winfo_toplevel())
            return
        item = self._idea_store.load(idea_id)
        if not item:
            return
        pitcher = getattr(self, "pitcher", None)
        ideator = getattr(self, "ideator", None)
        if not pitcher:
            messagebox.showwarning("Refine",
                "Pitcher model not available — start the council first.",
                parent=self.winfo_toplevel())
            return
        self._ideas_log_append(
            f"  ✨ Refining: {item.display_title}…")
        threading.Thread(
            target=self._ideas_refine_worker,
            args=(item, pitcher, ideator),
            daemon=True,
        ).start()

    def _ideas_refine_worker(self, item: IdeaItem, pitcher, ideator):
        """Background thread: re-pitch an idea, incorporating user notes."""
        try:
            from idea_engine import _build_refinement_prompt, _parse_pitcher_response
            prompt   = _build_refinement_prompt(item)
            response = pitcher.respond(prompt)
            fields   = _parse_pitcher_response(response)

            # Build a new IdeaItem that is the refined version
            from idea_engine import IdeaItem as _IdeaItem
            refined = _IdeaItem(
                # Keep raw ideator fields from the original
                raw_idea          = item.raw_idea,
                hook_angle        = item.hook_angle,
                emotional_trigger = item.emotional_trigger,
                format_suggestion = item.format_suggestion,
                seed_used         = item.seed_used,
                niche_seed        = item.niche_seed,
                # Overwrite pitcher fields with refined version
                title             = fields.get("title") or item.title,
                hook              = fields.get("hook") or item.hook,
                premise           = fields.get("premise") or item.premise,
                outline           = fields.get("outline") or item.outline,
                thumbnail_concept = fields.get("thumbnail_concept") or item.thumbnail_concept,
                target_audience   = fields.get("target_audience") or item.target_audience,
                why_it_works      = fields.get("why_it_works") or item.why_it_works,
                title_variants    = fields.get("title_variants") or item.title_variants,
                tags              = fields.get("tags") or item.tags,
                difficulty        = fields.get("difficulty") or item.difficulty,
                estimated_length  = fields.get("estimated_length") or item.estimated_length,
                production_notes  = fields.get("production_notes") or item.production_notes,
                ideator_model     = item.ideator_model,
                pitcher_model     = getattr(pitcher, "name", "pitcher"),
                # Carry over user state
                rating            = item.rating,
                status            = item.status,
                notes             = item.notes,
                refined_from      = item.id,
            )
            self._idea_store.save(refined)
            self.after(0, lambda: self._ideas_list_refresh())
            self.after(0, lambda: self._ideas_log_append(
                f"  ✓ Refined idea saved: {refined.display_title}"))
        except Exception as e:
            import traceback
            self.after(0, lambda: self._ideas_log_append(
                f"  ✗ Refinement error: {e}\n{traceback.format_exc()[:200]}"))

    def _ideas_export(self):
        """Export all ideas (or filtered view) to a Markdown file."""
        index = self._idea_store.list_index()
        if not index:
            messagebox.showinfo("Export", "No ideas to export.",
                                parent=self.winfo_toplevel())
            return
        path = filedialog.asksaveasfilename(
            title="Export ideas",
            defaultextension=".md",
            filetypes=[("Markdown", "*.md"), ("Text", "*.txt"), ("All", "*.*")],
            initialfile="ideas_export.md",
        )
        if not path:
            return
        lines = ["# Video Ideas Export\n",
                 f"Exported: {__import__('datetime').datetime.now():%Y-%m-%d %H:%M}\n",
                 f"Total: {len(index)} ideas\n\n---\n"]
        for entry in index:
            item = self._idea_store.load(entry["id"])
            if not item:
                continue
            lines.append(f"\n## {item.display_title}")
            lines.append(f"*{item.generated_at[:16]}  |  "
                         f"{item.status}  |  ★ {item.rating}  |  "
                         f"{item.difficulty}*\n")
            if item.hook_angle:
                lines.append(f"**Hook angle:** {item.hook_angle}\n")
            if item.premise:
                lines.append(f"**Premise:**\n{item.premise}\n")
            if item.outline:
                lines.append("**Outline:**")
                for pt in item.outline:
                    lines.append(f"- {pt}")
                lines.append("")
            if item.thumbnail_concept:
                lines.append(f"**Thumbnail:** {item.thumbnail_concept}\n")
            if item.title_variants:
                lines.append("**Title variants:** " +
                              " / ".join(item.title_variants) + "\n")
            if item.notes:
                lines.append(f"**Notes:** {item.notes}\n")
            lines.append("\n---")

        Path(path).write_text("\n".join(lines), encoding="utf-8")
        self._ideas_log_append(f"✓ Exported {len(index)} ideas to {Path(path).name}")
        messagebox.showinfo("Export",
                             f"Exported {len(index)} ideas to:\n{path}",
                             parent=self.winfo_toplevel())

    def _ideas_send_to_council(self):
        """Send the selected idea's full pitch to the Council input (or clipboard)."""
        idea_id = self._ideas_selected_id()
        if not idea_id:
            messagebox.showinfo("Ideas", "Select an idea first.",
                                parent=self.winfo_toplevel())
            return
        item = self._idea_store.load(idea_id)
        if not item:
            return

        lines = [f"VIDEO IDEA: {item.display_title}"]
        if item.hook_angle:
            lines.append(f"\nHOOK ANGLE: {item.hook_angle}")
        if item.premise:
            lines.append(f"\nPREMISE:\n{item.premise}")
        if item.outline:
            lines.append("\nOUTLINE:")
            for pt in item.outline:
                lines.append(f"  {pt}")
        if item.why_it_works:
            lines.append(f"\nWHY IT WORKS: {item.why_it_works}")
        if item.title_variants:
            lines.append("\nTITLE VARIANTS: " + " / ".join(item.title_variants))
        text = "\n".join(lines)

        if hasattr(self, "nb") and self.nb is not None:
            self._set_text(self.input, text)
            self.nb.select(self.tab_council)
        else:
            # Standalone: copy to clipboard
            self.winfo_toplevel().clipboard_clear()
            self.winfo_toplevel().clipboard_append(text)
            messagebox.showinfo("Copied",
                                 "Idea pitch copied to clipboard.",
                                 parent=self.winfo_toplevel())

    # =========================================================
    # GitHub / git integration
    # =========================================================

    def _git_config_file(self) -> Path:
        return self.vault_dir / "idea_git_config.json"

    def _load_git_config(self) -> dict:
        try:
            f = self._git_config_file()
            if f.exists():
                return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {
            "remote":          "https://github.com/Infernoplaystuf/Council.git",
            "branch":          "main",
            "filter":          "all",      # auto-push sends everything; manual can filter
            "auto_push":       False,
            "auto_push_every": 5,
        }

    def _ideas_git_save_config(self):
        cfg = {
            "remote":          self._git_remote_var.get().strip(),
            "branch":          self._git_branch_var.get().strip() or "main",
            "filter":          self._git_filter_var.get(),
            "auto_push":       bool(self._git_autopush_var.get()),
            "auto_push_every": max(1, int(self._git_autopush_every_var.get() or 5)),
        }
        try:
            self._git_config_file().write_text(
                json.dumps(cfg, indent=2), encoding="utf-8")
            self._git_status_var.set("Config saved.")
            self._ideas_log_append("✓ GitHub config saved.")
        except Exception as e:
            self._ideas_log_append(f"✗ Config save failed: {e}")

    def _idea_to_markdown(self, item: IdeaItem) -> str:
        """Render a single IdeaItem as a Markdown document."""
        stars = "★" * item.rating if item.rating else "not rated"
        lines = [
            f"# {item.display_title}",
            "",
            f"**Generated:** {item.generated_at[:16]}  ",
            f"**Status:** {item.status}  ",
            f"**Rating:** {stars}  ",
            f"**Difficulty:** {item.difficulty or '—'}  ",
            f"**Estimated length:** {item.estimated_length or '—'}  ",
            f"**Seed / niche:** {item.niche_seed or '—'}  ",
            "",
        ]
        if item.hook_angle:
            lines += ["## Hook Angle", "", item.hook_angle, ""]
        if item.emotional_trigger:
            lines += ["## Emotional Trigger", "", item.emotional_trigger, ""]
        if item.format_suggestion:
            lines += ["## Format", "", item.format_suggestion, ""]
        if item.hook:
            lines += ["## Hook (first 30 seconds)", "", item.hook, ""]
        if item.premise:
            lines += ["## Premise", "", item.premise, ""]
        if item.outline:
            lines += ["## Outline", ""]
            for pt in item.outline:
                lines.append(f"- {pt}")
            lines.append("")
        if item.thumbnail_concept:
            lines += ["## Thumbnail Concept", "", item.thumbnail_concept, ""]
        if item.target_audience:
            lines += ["## Target Audience", "", item.target_audience, ""]
        if item.why_it_works:
            lines += ["## Why It Works", "", item.why_it_works, ""]
        if item.title_variants:
            lines += ["## Title Variants", ""]
            for t in item.title_variants:
                lines.append(f"- {t}")
            lines.append("")
        if item.tags:
            lines += ["## Tags", "", ", ".join(item.tags), ""]
        if item.production_notes:
            lines += ["## Production Notes", "", item.production_notes, ""]
        if item.notes:
            lines += ["## My Notes", "", item.notes, ""]
        lines += [
            "---",
            f"*ID: {item.id} · Generated by Council Ideator/Pitcher*",
        ]
        return "\n".join(lines)

    def _ideas_to_readme(self, items: List[IdeaItem], session_id: str = "") -> str:
        """Generate an index README.md for a session subfolder."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        header = f"Session: `{session_id}`" if session_id else "Ideas"
        lines = [
            f"# Council — {header}",
            "",
            f"*Auto-generated by the Council Ideation Engine · Last sync: {now}*",
            "",
            f"**Ideas in this session:** {len(items)}",
            "",
            "| # | Title | Difficulty | Status | Rating | Date |",
            "|---|-------|-----------|--------|--------|------|",
        ]
        for i, item in enumerate(items, 1):
            slug = _idea_slug(item)
            date = item.generated_at[:10]
            diff = item.difficulty.split()[0] if item.difficulty else "—"
            stars = "★" * item.rating if item.rating else "·"
            title_link = f"[{item.display_title}](./{slug})"
            lines.append(
                f"| {i} | {title_link} | {diff} | {item.status} | {stars} | {date} |")
        return "\n".join(lines) + "\n"

    def _ideas_top_readme(self, ideas_root: Path) -> str:
        """Generate the root ideas/README.md that indexes all session subfolders."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [
            "# Council — Ideas Index",
            "",
            f"*Last updated: {now}*",
            "",
            "Each subfolder is one ideation session. "
            "Folder names are `month_day_year_hour_minute_second`.",
            "",
            "| Session | Ideas | Link |",
            "|---------|-------|------|",
        ]
        # Enumerate session dirs (exclude hidden dirs and non-directories)
        sessions = sorted(
            [d for d in ideas_root.iterdir()
             if d.is_dir() and not d.name.startswith(".")],
            reverse=True  # newest first
        )
        for sd in sessions:
            md_files = [f for f in sd.iterdir()
                        if f.suffix == ".md" and f.name != "README.md"]
            count = len(md_files)
            lines.append(f"| `{sd.name}` | {count} | [Browse](./{sd.name}/) |")

        if not sessions:
            lines.append("| — | — | *No sessions yet* |")

        return "\n".join(lines) + "\n"

    def _ideas_git_push(self):
        """Export filtered ideas to ideas/ folder and push to GitHub."""
        remote = self._git_remote_var.get().strip()
        branch = self._git_branch_var.get().strip() or "main"
        filt   = self._git_filter_var.get()

        if not remote:
            messagebox.showwarning("GitHub",
                                    "Enter a GitHub remote URL first.",
                                    parent=self.winfo_toplevel())
            return

        self._ideas_git_save_config()
        self._git_status_var.set("Pushing…")
        self._ideas_log_append(
            f"▶ Preparing GitHub push  filter={filt!r}  branch={branch!r}")

        def _worker():
            try:
                self._ideas_git_push_worker(remote, branch, filt)
            except Exception as e:
                import traceback
                self._ideas_log_append(f"✗ Push failed: {e}")
                self._ideas_log_append(traceback.format_exc()[:400])
                self.after(0, lambda: self._git_status_var.set("Push failed"))

        threading.Thread(target=_worker, daemon=True).start()

    def _ideas_git_push_worker(self, remote: str, branch: str, filt: str,
                                session_id: str = ""):
        """Background thread: write Markdown files into a session subfolder, then push."""
        repo_root = Path(__file__).parent
        ideas_root = repo_root / "ideas"
        ideas_root.mkdir(exist_ok=True)

        # ── Determine session folder ───────────────────────────
        # Auto-push during a loop uses the loop's session_id.
        # Manual push groups ideas by the date of their generated_at timestamp.
        if not session_id:
            now = datetime.now()
            session_id = f"manual_{now.month}_{now.day}_{now.strftime('%y_%H_%M_%S')}"

        session_dir = ideas_root / session_id
        session_dir.mkdir(exist_ok=True)

        # ── Filter ideas ──────────────────────────────────────
        index = self._idea_store.list_index()
        if filt == "in-production":
            keep = {"in-production"}
        elif filt == "saved+":
            keep = {"saved", "in-production"}
        else:
            keep = {"new", "saved", "archived", "in-production"}

        filtered_entries = [e for e in index if e.get("status") in keep]
        if not filtered_entries:
            self._ideas_log_append("⚠ No ideas match the selected filter — nothing to push.")
            self.after(0, lambda: self._git_status_var.set("Nothing to push"))
            return

        self._ideas_log_append(
            f"  Writing {len(filtered_entries)} idea files to ideas/{session_id}/…")

        # ── Write Markdown files into session subfolder ────────
        items_written: List[IdeaItem] = []
        for entry in filtered_entries:
            item = self._idea_store.load(entry["id"])
            if not item:
                continue
            slug    = _idea_slug(item)
            md_path = session_dir / slug
            md_path.write_text(self._idea_to_markdown(item), encoding="utf-8")
            items_written.append(item)

        # ── Session README ────────────────────────────────────
        session_readme = session_dir / "README.md"
        session_readme.write_text(
            self._ideas_to_readme(items_written, session_id=session_id),
            encoding="utf-8")

        # ── Top-level index README ─────────────────────────────
        # Enumerate all session folders so the root README links to each one.
        top_readme = ideas_root / "README.md"
        top_readme.write_text(
            self._ideas_top_readme(ideas_root), encoding="utf-8")

        self._ideas_log_append(
            f"  ✓ {len(items_written)} Markdown files in {session_id}/ + index updated")

        # ── Git operations ────────────────────────────────────
        def _git(*args) -> str:
            result = subprocess.run(
                ["git"] + list(args),
                cwd=str(repo_root),
                capture_output=True, text=True,
            )
            out = (result.stdout + result.stderr).strip()
            if out:
                self._ideas_log_append(f"  git {args[0]}: {out[:200]}")
            return result.returncode, out

        # Init repo if needed
        if not (repo_root / ".git").exists():
            self._ideas_log_append("  Initialising git repo…")
            _git("init", "-b", branch)
            _git("remote", "add", "origin", remote)
        else:
            rc, _ = _git("remote", "get-url", "origin")
            if rc != 0:
                _git("remote", "add", "origin", remote)
            else:
                _git("remote", "set-url", "origin", remote)

        # Stage ideas/
        _git("add", "ideas/")

        # Check if there's anything to commit
        rc, diff_out = _git("diff", "--cached", "--name-only")
        if not diff_out.strip():
            self._ideas_log_append("  Nothing new to commit — ideas already up to date.")
            self.after(0, lambda: self._git_status_var.set("Already up to date"))
            return

        # Commit
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        msg = f"ideas: sync {len(items_written)} ideas [{now_str}] session={session_id}"
        rc, _ = _git("commit", "-m", msg)
        if rc != 0:
            self._ideas_log_append("✗ Commit failed — see above.")
            self.after(0, lambda: self._git_status_var.set("Commit failed"))
            return

        # Push
        self._ideas_log_append(f"  Pushing to {remote} ({branch})…")
        rc, push_out = _git("push", "-u", "origin", branch)
        if rc == 0:
            self._ideas_log_append(
                f"✓ Pushed {len(items_written)} ideas to GitHub ({branch})")
            self.after(0, lambda n=len(items_written):
                       self._git_status_var.set(f"Pushed {n} ideas"))
        else:
            self._ideas_log_append(
                "✗ Push rejected. If this is your first push you may need to:\n"
                "  1. Create the repo on GitHub if it doesn't exist\n"
                "  2. Authenticate: run  gh auth login  or configure a PAT\n"
                "  3. If branch doesn't exist: git push -u origin main\n"
                f"  Error: {push_out[:300]}")
            self.after(0, lambda: self._git_status_var.set("Auth needed — see log"))

    # =========================================================
    # Settings persistence
    # =========================================================

    def _settings_file(self) -> Path:
        return self.vault_dir / "idea_settings.json"

    def _load_idea_settings(self) -> IdeationSettings:
        try:
            f = self._settings_file()
            if f.exists():
                data = json.loads(f.read_text(encoding="utf-8"))
                return IdeationSettings.from_dict(data)
        except Exception:
            pass
        return IdeationSettings()

    def _save_idea_settings(self, settings: IdeationSettings):
        try:
            self._settings_file().write_text(
                json.dumps(settings.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8")
        except Exception:
            pass

    def _model_config_file(self) -> Path:
        return self.vault_dir / "idea_model_config.json"

    def _load_idea_model_config(self) -> dict:
        try:
            f = self._model_config_file()
            if f.exists():
                return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {"ideator_role": "ideator", "pitcher_role": "pitcher"}

    def _save_idea_model_config(self):
        try:
            cfg = {
                "ideator_role":    self._idea_ideator_role_var.get(),
                "pitcher_role":    self._idea_pitcher_role_var.get(),
                "brainstorm_roles": [
                    role for role, var in getattr(self, "_brainstorm_vars", {}).items()
                    if var.get()
                ],
            }
            self._model_config_file().write_text(
                json.dumps(cfg, indent=2), encoding="utf-8")
        except Exception:
            pass


# ============================================================
# IdeaApp — standalone runner
# ============================================================

class IdeaApp(StandaloneHost, IdeaTabMixin):
    """
    Full Idea Generator running as its own window.

        python tab_ideas.py [--vault PATH] [--no-ai]
    """

    def __init__(self, vault_dir: Optional[Path] = None, no_ai: bool = False):
        StandaloneHost.__init__(
            self,
            vault_dir = vault_dir,
            title     = "Council — Overnight Idea Generator",
            geometry  = "1300x840",
        )

        if not no_ai:
            self._init_models()

        self._build_ideas_tab(parent=self.root)

    def _poll_queue(self):
        self.root.after(80, self._poll_queue)


def run_standalone():
    import argparse
    ap = argparse.ArgumentParser(
        description="Council Overnight Idea Generator — standalone")
    ap.add_argument("--vault", metavar="PATH",
                    help="Path to vault directory (default: ~/council_vault)")
    ap.add_argument("--no-ai", action="store_true",
                    help="Disable AI (opens UI without models for testing)")
    args = ap.parse_args()

    app = IdeaApp(
        vault_dir = Path(args.vault) if args.vault else None,
        no_ai     = args.no_ai,
    )
    app.run()


if __name__ == "__main__":
    run_standalone()
