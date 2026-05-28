# ============================================================
# tab_video.py  —  Video Analyser tab as a standalone module
# ============================================================
# Usage modes:
#   1. Mixin inside CouncilConsole (call _build_video_tab())
#   2. Standalone: python tab_video.py [--vault PATH]
#
# Depends only on:
#   council_modules.StandaloneHost  (shared interface / palette)
#   video_processor (optional — graceful fallback)
# ============================================================

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Optional

import tkinter as tk
from tkinter import ttk, messagebox

from council_modules import StandaloneHost, PALETTE

# ── Optional video processor import ───────────────────────────
try:
    import video_processor as vp
    _VIDEO_OK = True
except (ImportError, OSError):
    vp = None
    _VIDEO_OK = False


# ============================================================
# VideoTabMixin — all video UI + logic as a mixin
# ============================================================

class VideoTabMixin:
    """
    Mixin that adds a Video Analyser tab.

    Expected attributes on self (provided by CouncilConsole or StandaloneHost):
      self.vault_dir           — Path  (where to store queue / analyses)
      self.after(ms, fn, …)   — Tk after delegation
      self._make_text(parent)  — dark tk.Text factory
      self._append_transcript(who, text, kind)
      self._set_text(widget, text)
      # Optional (council only):
      self.nb                  — ttk.Notebook
      self.tab_council         — council tab Frame
      self.input               — council input Text widget
      # Model attributes (may be None):
      self.writer, self.director, self.content, self.sage,
      self.algorithm, self.coach, self.cutter, self.eye
      self._content_style      — optional ContentStyleManager
    """

    # =========================================================
    # Build
    # =========================================================

    def _build_video_tab(self, parent=None):
        """
        Build the video tab UI.

        Council mode  (parent=None): creates a new Frame and adds it to self.nb.
        Standalone    (parent=Frame): builds into the supplied parent widget.
        """
        if parent is None:
            # Council embedding
            self.tab_video = ttk.Frame(self.nb)
            self.nb.add(self.tab_video, text="🎬 Video")
            _root = self.tab_video
        else:
            _root = parent

        # ── Video processor instance ───────────────────────────
        if _VIDEO_OK:
            self._video_proc = vp.VideoProcessor(vault_dir=self.vault_dir)
        else:
            self._video_proc = None

        # ── Header ────────────────────────────────────────────
        hdr = ttk.Frame(_root)
        hdr.pack(fill="x", padx=10, pady=(8, 4))
        ttk.Label(hdr, text="Video Analyser",
                  foreground=PALETTE["blue"],
                  font=("", 11, "bold")).pack(side="left")
        ttk.Label(hdr,
                  text="  Transcribe, describe, and learn your creator vibe",
                  foreground=PALETTE["overlay"]).pack(side="left")

        if not _VIDEO_OK:
            ttk.Label(_root,
                      text="video_processor.py not found — place it in your council folder.",
                      foreground=PALETTE["red"]).pack(padx=12, pady=20)
            return

        # ── File picker ───────────────────────────────────────
        file_frame = ttk.LabelFrame(_root, text="Video File")
        file_frame.pack(fill="x", padx=10, pady=(0, 6))

        self._video_path_var = tk.StringVar()
        file_row = ttk.Frame(file_frame)
        file_row.pack(fill="x", padx=8, pady=6)
        ttk.Entry(file_row, textvariable=self._video_path_var,
                  width=60).pack(side="left", fill="x", expand=True)
        ttk.Button(file_row, text="Browse…",
                   command=self._video_browse).pack(side="left", padx=6)

        # ── Options ───────────────────────────────────────────
        opt_frame = ttk.LabelFrame(_root, text="Options")
        opt_frame.pack(fill="x", padx=10, pady=(0, 6))

        opt_row1 = ttk.Frame(opt_frame)
        opt_row1.pack(fill="x", padx=8, pady=(6, 2))

        ttk.Label(opt_row1, text="Whisper model:").pack(side="left")
        self._whisper_model_var = tk.StringVar(value="base")
        ttk.Combobox(opt_row1, textvariable=self._whisper_model_var,
                     values=["tiny", "base", "small", "medium", "large-v2"],
                     state="readonly", width=10).pack(side="left", padx=6)
        ttk.Label(opt_row1, text="  Device:").pack(side="left")
        self._whisper_device_var = tk.StringVar(value="cuda")
        ttk.Combobox(opt_row1, textvariable=self._whisper_device_var,
                     values=["cuda", "cpu"],
                     state="readonly", width=6).pack(side="left", padx=6)

        opt_row2 = ttk.Frame(opt_frame)
        opt_row2.pack(fill="x", padx=8, pady=(2, 6))

        self._do_frames_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt_row2, text="Extract frames",
                        variable=self._do_frames_var).pack(side="left")
        ttk.Label(opt_row2, text="  every").pack(side="left", padx=(6, 2))
        self._frame_interval_var = tk.StringVar(value="10")
        ttk.Spinbox(opt_row2, textvariable=self._frame_interval_var,
                    from_=5, to=60, increment=5, width=5).pack(side="left")
        ttk.Label(opt_row2, text="s  max").pack(side="left", padx=(4, 2))
        self._max_frames_var = tk.StringVar(value="20")
        ttk.Spinbox(opt_row2, textvariable=self._max_frames_var,
                    from_=5, to=60, increment=5, width=5).pack(side="left")
        ttk.Label(opt_row2, text="frames").pack(side="left", padx=(4, 0))

        opt_row3 = ttk.Frame(opt_frame)
        opt_row3.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(opt_row3, text="Vision model:").pack(side="left")
        self._vision_model_var = tk.StringVar(value="llava:7b")
        self._vision_cb = ttk.Combobox(opt_row3, textvariable=self._vision_model_var,
                                        values=["llava:7b", "moondream", "llava-phi3",
                                                "llava-llama3", "minicpm-v"],
                                        width=18)
        self._vision_cb.pack(side="left", padx=6)
        ttk.Button(opt_row3, text="↺ Detect installed",
                   command=self._video_detect_vision_models).pack(side="left", padx=4)

        self._do_vibe_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt_row3,
                        text="Run vibe analysis (saves to Content Style)",
                        variable=self._do_vibe_var).pack(side="left", padx=12)

        opt_row4 = ttk.Frame(opt_frame)
        opt_row4.pack(fill="x", padx=8, pady=(2, 4))
        self._do_audio_quality_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt_row4,
                        text="🎚 Audio quality (noise/loudness/silence)",
                        variable=self._do_audio_quality_var).pack(side="left")
        self._do_energy_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt_row4, text="  ⚡ Energy profile",
                        variable=self._do_energy_var).pack(side="left", padx=4)
        self._do_visual_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt_row4, text="  🖼 Visual issues",
                        variable=self._do_visual_var).pack(side="left", padx=4)

        opt_row5 = ttk.Frame(opt_frame)
        opt_row5.pack(fill="x", padx=8, pady=(0, 6))
        self._do_roast_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt_row5,
                        text="🔥 Peasant Roast (brutal content critique)",
                        variable=self._do_roast_var).pack(side="left")
        ttk.Label(opt_row5, text="   Roast model:",
                  foreground=PALETTE["overlay"]).pack(side="left", padx=(10, 2))
        self._roast_model_var = tk.StringVar(value="writer")
        ttk.Combobox(opt_row5, textvariable=self._roast_model_var,
                     values=["writer", "director", "content", "cutter",
                             "algorithm", "coach", "sage"],
                     state="readonly", width=10).pack(side="left")
        ttk.Label(opt_row5, text="  Sage logic critique:",
                  foreground=PALETTE["overlay"]).pack(side="left", padx=(10, 2))
        self._do_sage_logic_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt_row5, variable=self._do_sage_logic_var).pack(side="left")
        ttk.Label(opt_row5,
                  text="  (Sage adds a separate logic/clarity pass on top of the roast)",
                  foreground="#45475a", font=("", 9)).pack(side="left", padx=4)

        opt_row6 = ttk.Frame(opt_frame)
        opt_row6.pack(fill="x", padx=8, pady=(2, 6))
        self._do_algorithm_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt_row6,
                        text="📦 Algorithm (retention, hook, packaging)",
                        variable=self._do_algorithm_var).pack(side="left")
        self._do_coach_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt_row6,
                        text="  🎙 Coach (delivery, pacing, vocal habits)",
                        variable=self._do_coach_var).pack(side="left", padx=12)

        # ── Action bar ────────────────────────────────────────
        act_frame = ttk.Frame(_root)
        act_frame.pack(fill="x", padx=10, pady=(0, 6))
        self._video_run_btn = ttk.Button(act_frame, text="▶  Analyse Video",
                                          command=self._video_run)
        self._video_run_btn.pack(side="left")
        ttk.Button(act_frame, text="■  Stop",
                   command=self._video_stop).pack(side="left", padx=6)
        ttk.Button(act_frame, text="➕  Add to Queue",
                   command=self._queue_add_current).pack(side="left", padx=6)
        ttk.Button(act_frame, text="📂  Past Analyses",
                   command=self._video_show_history).pack(side="left", padx=6)
        ttk.Button(act_frame, text="Send transcript to Council",
                   command=self._video_send_transcript).pack(side="left", padx=6)
        ttk.Button(act_frame, text="📋  Edit Suggestions",
                   command=self._video_show_edit_suggestions).pack(side="left", padx=6)
        ttk.Button(act_frame, text="🔥  Show Roast",
                   command=self._video_show_roast).pack(side="left", padx=6)
        ttk.Button(act_frame, text="📦  Algorithm Notes",
                   command=self._video_show_algorithm_notes).pack(side="left", padx=6)
        ttk.Button(act_frame, text="🎙  Coach Notes",
                   command=self._video_show_coach_notes).pack(side="left", padx=6)

        # ── Progress log ──────────────────────────────────────
        ttk.Label(_root, text="Progress:").pack(anchor="w", padx=10)
        log_frame = ttk.Frame(_root)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(0, 4))
        self._video_log = tk.Text(
            log_frame,
            bg="#11111b", fg=PALETTE["text"],
            font=("Consolas", 9), state="disabled",
            relief="flat", wrap="word",
        )
        log_sb = ttk.Scrollbar(log_frame, command=self._video_log.yview)
        self._video_log.configure(yscrollcommand=log_sb.set)
        log_sb.pack(side="right", fill="y")
        self._video_log.pack(fill="both", expand=True)
        self._video_log.tag_config("ok",   foreground=PALETTE["green"])
        self._video_log.tag_config("err",  foreground=PALETTE["red"])
        self._video_log.tag_config("warn", foreground=PALETTE["yellow"])
        self._video_log.tag_config("hdr",  foreground=PALETTE["blue"],
                                    font=("Consolas", 9, "bold"))

        # State
        self._last_video_analysis = None
        self._video_cancelled     = False

        # ── Video Queue ───────────────────────────────────────
        self._video_queue_items  = []
        self._queue_running      = False
        self._queue_paused       = False
        self._queue_stop_flag    = False
        self._queue_current_idx  = -1

        q_frame = ttk.LabelFrame(_root, text="📋 Analysis Queue")
        q_frame.pack(fill="x", padx=10, pady=(4, 6))

        # Queue treeview
        q_tv_frame = ttk.Frame(q_frame)
        q_tv_frame.pack(fill="x", padx=6, pady=(4, 0))

        cols = ("#", "File", "Type", "Status", "Duration")
        self._queue_tv = ttk.Treeview(
            q_tv_frame, columns=cols, show="headings",
            height=6, selectmode="browse",
        )
        self._queue_tv.heading("#",        text="#",        anchor="center")
        self._queue_tv.heading("File",     text="File",     anchor="w")
        self._queue_tv.heading("Type",     text="Type",     anchor="center")
        self._queue_tv.heading("Status",   text="Status",   anchor="center")
        self._queue_tv.heading("Duration", text="Duration", anchor="center")
        self._queue_tv.column("#",        width=28,  stretch=False, anchor="center")
        self._queue_tv.column("File",     width=320, stretch=True,  anchor="w")
        self._queue_tv.column("Type",     width=80,  stretch=False, anchor="center")
        self._queue_tv.column("Status",   width=110, stretch=False, anchor="center")
        self._queue_tv.column("Duration", width=80,  stretch=False, anchor="center")

        self._queue_tv.tag_configure("queued",
                                      foreground=PALETTE["subtext"])
        self._queue_tv.tag_configure("processing",
                                      foreground=PALETTE["blue"],
                                      font=("", 9, "bold"))
        self._queue_tv.tag_configure("done",    foreground=PALETTE["green"])
        self._queue_tv.tag_configure("error",   foreground=PALETTE["red"])
        self._queue_tv.tag_configure("skipped", foreground=PALETTE["overlay"])

        q_sb = ttk.Scrollbar(q_tv_frame, orient="vertical",
                              command=self._queue_tv.yview)
        self._queue_tv.configure(yscrollcommand=q_sb.set)
        q_sb.pack(side="right", fill="y")
        self._queue_tv.pack(fill="x", expand=True)
        self._queue_tv.bind("<Double-Button-1>", self._queue_show_result)

        # Queue add controls
        q_add_row = ttk.Frame(q_frame)
        q_add_row.pack(fill="x", padx=6, pady=(4, 2))

        ttk.Button(q_add_row, text="➕ Add Files…",
                   command=self._queue_browse_add).pack(side="left")
        ttk.Label(q_add_row, text="  Type:",
                  foreground=PALETTE["overlay"]).pack(side="left", padx=(10, 2))
        self._queue_type_var = tk.StringVar(value="raw")
        ttk.Combobox(q_add_row, textvariable=self._queue_type_var,
                     values=["raw", "edited", "custom"],
                     state="readonly", width=8).pack(side="left")
        ttk.Label(
            q_add_row,
            text="  raw=full analysis+roast  ·  edited=QC+loudness only  ·  custom=use options panel",
            foreground="#45475a", font=("", 9),
        ).pack(side="left", padx=6)

        # Queue management buttons
        q_mgmt_row = ttk.Frame(q_frame)
        q_mgmt_row.pack(fill="x", padx=6, pady=(0, 2))
        ttk.Button(q_mgmt_row, text="▲", width=3,
                   command=self._queue_move_up).pack(side="left")
        ttk.Button(q_mgmt_row, text="▼", width=3,
                   command=self._queue_move_down).pack(side="left", padx=2)
        ttk.Button(q_mgmt_row, text="🗑 Remove",
                   command=self._queue_remove).pack(side="left", padx=4)
        ttk.Button(q_mgmt_row, text="Change Type",
                   command=self._queue_change_type).pack(side="left", padx=4)
        ttk.Button(q_mgmt_row, text="Clear Done",
                   command=self._queue_clear_done).pack(side="left", padx=4)
        ttk.Button(q_mgmt_row, text="Clear All",
                   command=self._queue_clear_all).pack(side="left", padx=4)
        ttk.Separator(q_mgmt_row, orient="vertical").pack(
            side="left", fill="y", padx=8)
        ttk.Button(q_mgmt_row, text="💾 Save Queue",
                   command=self._queue_save).pack(side="left", padx=2)
        ttk.Button(q_mgmt_row, text="📂 Load Queue",
                   command=self._queue_load).pack(side="left", padx=2)

        # Queue run controls
        q_run_row = ttk.Frame(q_frame)
        q_run_row.pack(fill="x", padx=6, pady=(4, 6))
        self._queue_run_btn = ttk.Button(q_run_row, text="▶  Run Queue",
                                          command=self._queue_run)
        self._queue_run_btn.pack(side="left")
        self._queue_pause_btn = ttk.Button(q_run_row, text="⏸  Pause",
                                            command=self._queue_pause,
                                            state="disabled")
        self._queue_pause_btn.pack(side="left", padx=6)
        ttk.Button(q_run_row, text="■  Stop Queue",
                   command=self._queue_stop).pack(side="left")

        self._queue_status_var = tk.StringVar(value="Queue empty")
        ttk.Label(q_run_row, textvariable=self._queue_status_var,
                  foreground=PALETTE["blue"]).pack(side="left", padx=12)

        # Auto-load persisted queue
        self._queue_autoload()

    # =========================================================
    # Video methods
    # =========================================================

    def _video_log_append(self, msg: str):
        """Append a line to the video progress log (thread-safe via after)."""
        def _do():
            self._video_log.configure(state="normal")
            tag = ("ok"   if "✓" in msg else
                   "err"  if "✗" in msg else
                   "warn" if "⚠" in msg else
                   "hdr"  if msg.startswith("▶") else "")
            self._video_log.insert("end", msg + "\n", tag)
            self._video_log.see("end")
            self._video_log.configure(state="disabled")
        self.after(0, _do)

    def _video_browse(self):
        from tkinter import filedialog as _fd
        path = _fd.askopenfilename(
            title="Select video file",
            filetypes=[
                ("Video files", "*.mp4 *.mov *.avi *.mkv *.webm *.m4v *.flv *.wmv"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self._video_path_var.set(path)

    def _video_detect_vision_models(self):
        if not self._video_proc:
            return
        models = self._video_proc.available_vision_models()
        if models:
            self._vision_cb.configure(values=models)
            self._vision_model_var.set(models[0])
            self._video_log_append(f"✓ Found vision models: {', '.join(models)}")
        else:
            self._video_log_append(
                "⚠ No vision models found. Install one with:\n"
                "  ollama pull llava:7b\n"
                "  ollama pull moondream"
            )

    def _video_run(self):
        if not self._video_proc:
            messagebox.showwarning("Video",
                                    "video_processor.py not available.",
                                    parent=self.winfo_toplevel())
            return
        path = self._video_path_var.get().strip()
        if not path:
            messagebox.showwarning("Video", "Select a video file first.",
                                    parent=self.winfo_toplevel())
            return

        # Clear log
        self._video_log.configure(state="normal")
        self._video_log.delete("1.0", "end")
        self._video_log.configure(state="disabled")

        self._video_cancelled = False
        self._video_run_btn.configure(state="disabled")

        # Resolve models
        _roast_role     = self._roast_model_var.get()
        personality     = (getattr(self, _roast_role, None)
                           or getattr(self, "director", None)
                           or getattr(self, "content", None)
                           or getattr(self, "writer", None))
        sage_model      = (getattr(self, "sage", None)
                           if self._do_sage_logic_var.get() else None)
        algorithm_model = (getattr(self, "algorithm", None)
                           if self._do_algorithm_var.get() else None)
        coach_model     = (getattr(self, "coach", None)
                           if self._do_coach_var.get() else None)
        style_mgr       = getattr(self, "_content_style", None)

        def worker():
            try:
                result = self._video_proc.process(
                    path,
                    whisper_model     = self._whisper_model_var.get(),
                    whisper_device    = self._whisper_device_var.get(),
                    do_frames         = bool(self._do_frames_var.get()),
                    frame_interval_s  = int(self._frame_interval_var.get() or 10),
                    max_frames        = int(self._max_frames_var.get() or 20),
                    vision_model      = self._vision_model_var.get(),
                    personality_model = personality if self._do_vibe_var.get() else None,
                    content_style_manager = style_mgr if self._do_vibe_var.get() else None,
                    sage_model        = sage_model,
                    algorithm_model   = algorithm_model,
                    coach_model       = coach_model,
                    do_audio_analysis = bool(self._do_audio_quality_var.get()),
                    do_energy_profile = bool(self._do_energy_var.get()),
                    do_visual_analysis= bool(self._do_visual_var.get()),
                    do_edit_suggestions=True,
                    do_roast          = bool(self._do_roast_var.get()),
                    progress_cb       = self._video_log_append,
                    cancelled         = lambda: self._video_cancelled,
                )
                self._last_video_analysis = result

                # Build rich summary
                summary_parts = [f"✓ Processed: {Path(path).name}"]

                if result.transcript:
                    summary_parts.append(
                        f"📝 Transcript: {len(result.transcript)} segments")

                if result.audio_quality:
                    aq = result.audio_quality
                    aq_flags = []
                    if aq.has_clipping:            aq_flags.append("⚠ CLIPPING")
                    if aq.is_too_quiet:            aq_flags.append("⚠ Too quiet")
                    if aq.is_normalisation_needed: aq_flags.append("⚠ Needs loudness normalisation")
                    if aq.longest_silence_s > 3:   aq_flags.append(
                        f"⚠ Longest silence: {aq.longest_silence_s:.1f}s")
                    flags_txt = "  " + "  ".join(aq_flags) if aq_flags else "  ✓ Clean"
                    summary_parts.append(
                        f"🎚 Audio: {aq.integrated_lufs:.1f} LUFS  "
                        f"RMS {aq.rms_db:.1f} dB  "
                        f"{aq.silence_count} silence gaps\n{flags_txt}")

                if result.energy_profile:
                    dead  = sum(1 for e in result.energy_profile if e.label == "dead")
                    low   = sum(1 for e in result.energy_profile if e.label == "low")
                    high  = sum(1 for e in result.energy_profile if e.label == "high")
                    summary_parts.append(
                        f"⚡ Energy: {high} high / {low} low / {dead} dead windows")
                    dead_segs = [e for e in result.energy_profile if e.label == "dead"]
                    if dead_segs:
                        summary_parts.append(
                            "  Dead air at: " +
                            ", ".join(e.timecode() for e in dead_segs[:4]))

                if result.visual_issues:
                    by_type: dict = {}
                    for vi in result.visual_issues:
                        by_type[vi.issue_type] = by_type.get(vi.issue_type, 0) + 1
                    vis_txt = "  ".join(f"{k}: {v}" for k, v in by_type.items())
                    summary_parts.append(f"🖼 Visual issues: {vis_txt}")

                if result.edit_suggestions:
                    high_p = [s for s in result.edit_suggestions if s.priority == "high"]
                    summary_parts.append(
                        f"📋 Edit suggestions: {len(result.edit_suggestions)} total "
                        f"({len(high_p)} high priority) — click 'Edit Suggestions' to view")

                if result.roast:
                    summary_parts.append(
                        f"\n🔥 PEASANT GRADE: {result.roast.grade}\n"
                        + result.roast.roast_text[:600]
                        + ("\n..." if len(result.roast.roast_text) > 600 else "")
                        + "\n\n(Full roast available via 'Show Roast' button)")

                if result.vibe_summary:
                    summary_parts.append(
                        f"\n📊 VIBE:\n{result.vibe_summary}\n\n"
                        f"PACING:\n{result.pacing_notes}")

                _summary = "\n\n".join(summary_parts)
                self.after(0, lambda s=_summary: self._append_transcript(
                    "Video Analyser", s, "final"))

            except Exception as e:
                import traceback
                self._video_log_append(f"✗ Fatal error: {e}")
                self._video_log_append(traceback.format_exc()[:500])
            finally:
                self.after(0, lambda: self._video_run_btn.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def _video_stop(self):
        self._video_cancelled = True
        self._video_log_append(
            "⚠ Stop requested — will halt after current step completes.")

    # ── Result popups ─────────────────────────────────────────

    def _video_show_edit_suggestions(self):
        """Popup showing all edit suggestions with FFmpeg snippets."""
        a = self._last_video_analysis
        if not a or not a.edit_suggestions:
            messagebox.showinfo("Edit Suggestions",
                                "No edit suggestions yet. Run an analysis first.",
                                parent=self.winfo_toplevel())
            return

        win = tk.Toplevel(self.winfo_toplevel())
        win.title("📋 Edit Suggestions")
        win.configure(bg=PALETTE["bg"])
        win.geometry("800x560")

        ttk.Label(win,
                  text=f"Edit suggestions for: {Path(a.video_path).name}",
                  foreground=PALETTE["blue"],
                  font=("", 10, "bold")).pack(anchor="w", padx=12, pady=(8, 4))

        fr = ttk.Frame(win)
        fr.pack(fill="both", expand=True, padx=10, pady=4)
        txt = tk.Text(fr, bg="#11111b", fg=PALETTE["text"],
                      font=("Consolas", 9), relief="flat", wrap="word",
                      state="normal")
        sb = ttk.Scrollbar(fr, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)
        txt.tag_config("high",   foreground=PALETTE["red"],    font=("Consolas", 9, "bold"))
        txt.tag_config("medium", foreground=PALETTE["yellow"], font=("Consolas", 9, "bold"))
        txt.tag_config("low",    foreground=PALETTE["green"],  font=("Consolas", 9, "bold"))
        txt.tag_config("cmd",    foreground="#89dceb", background="#181825",
                                  font=("Consolas", 9))
        txt.tag_config("header", foreground=PALETTE["mauve"],
                                  font=("Consolas", 10, "bold"))

        # Audio quality summary
        if a.audio_quality:
            aq = a.audio_quality
            txt.insert("end", "── AUDIO QUALITY REPORT ─────────────────────────\n",
                       "header")
            txt.insert("end",
                       f"  RMS: {aq.rms_db:.1f} dB  |  Peak: {aq.peak_db:.1f} dB  |  "
                       f"DR: {aq.dynamic_range_db:.1f} dB\n"
                       f"  Integrated: {aq.integrated_lufs:.1f} LUFS  |  "
                       f"LRA: {aq.loudness_range_lu:.1f} LU  |  "
                       f"TruePeak: {aq.true_peak_dbtp:.1f} dBTP\n"
                       f"  Clipping: {'⚠ YES' if aq.has_clipping else 'No'}  |  "
                       f"Too quiet: {'⚠ YES' if aq.is_too_quiet else 'No'}  |  "
                       f"Needs normalisation: {'⚠ YES' if aq.is_normalisation_needed else 'No'}\n"
                       f"  Silence gaps: {aq.silence_count}  "
                       f"({aq.total_silence_s:.1f}s total, "
                       f"longest {aq.longest_silence_s:.1f}s)\n\n")

        # Energy profile summary
        if a.energy_profile:
            txt.insert("end", "── ENERGY PROFILE ───────────────────────────────\n",
                       "header")
            for ep in a.energy_profile:
                tag = ("high" if ep.label == "high" else
                       "medium" if ep.label == "normal" else "low")
                bar = "█" * int(ep.score * 20)
                txt.insert("end",
                           f"  {ep.timecode():22s}  {ep.label:6s}  "
                           f"{ep.wps:.1f} wps  [{bar:<20}]\n", tag)
                if ep.note:
                    txt.insert("end", f"            {ep.note}\n")
            txt.insert("end", "\n")

        # Edit suggestions
        txt.insert("end", "── EDIT SUGGESTIONS ─────────────────────────────\n",
                   "header")
        for i, s in enumerate(a.edit_suggestions, 1):
            pri_tag = s.priority if s.priority in ("high", "medium", "low") else "low"
            txt.insert("end",
                       f"[{i}] [{s.priority.upper()}] {s.suggestion_type}\n",
                       pri_tag)
            if s.timecode:
                txt.insert("end", f"    Timecode: {s.timecode}\n")
            txt.insert("end", f"    {s.description}\n")
            if s.ffmpeg_snippet:
                txt.insert("end", f"    $ {s.ffmpeg_snippet}\n", "cmd")
            txt.insert("end", "\n")

        txt.configure(state="disabled")
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=6)

    def _video_show_roast(self):
        """Popup showing the full Peasant Roast critique."""
        a = self._last_video_analysis
        if not a or not a.roast:
            messagebox.showinfo("Peasant Roast",
                                "No roast available. Run analysis with Roast enabled.",
                                parent=self.winfo_toplevel())
            return

        roast = a.roast
        win   = tk.Toplevel(self.winfo_toplevel())
        win.title("🔥 Peasant Roast")
        win.configure(bg=PALETTE["bg"])
        win.geometry("820x620")

        grade_colour = {
            "A": PALETTE["green"], "B": "#94e2d5",
            "C": PALETTE["yellow"], "D": "#fab387", "F": PALETTE["red"],
        }.get(roast.grade[:1], PALETTE["text"])

        hdr = ttk.Frame(win)
        hdr.pack(fill="x", padx=12, pady=(8, 4))
        ttk.Label(hdr, text="🔥 Peasant Roast",
                  foreground=PALETTE["red"],
                  font=("", 12, "bold")).pack(side="left")
        ttk.Label(hdr, text=f"  Grade: {roast.grade}",
                  foreground=grade_colour,
                  font=("", 14, "bold")).pack(side="left", padx=8)
        ttk.Label(hdr, text=f"  {Path(a.video_path).name}",
                  foreground=PALETTE["overlay"]).pack(side="left")

        # Show detected content context
        if getattr(a, "video_context", None) and a.video_context.content_type != "unknown":
            vc = a.video_context
            ctx_lbl = (f"  [{vc.content_type.upper()}]"
                       + (f"  {vc.topic[:70]}" if vc.topic else "")
                       + (f"  (detected by {vc.detected_by})" if vc.detected_by else ""))
            ttk.Label(hdr, text=ctx_lbl,
                      foreground=PALETTE["blue"], font=("", 9)).pack(side="left", padx=6)

        # Filler word summary
        if roast.filler_word_hits:
            top = list(roast.filler_word_hits.items())[:6]
            filler_txt = "  Worst filler words: " + "  ".join(
                f"'{w}' x{n}" for w, n in top)
            ttk.Label(win, text=filler_txt,
                      foreground=PALETTE["yellow"],
                      font=("", 9)).pack(anchor="w", padx=12)

        fr = ttk.Frame(win)
        fr.pack(fill="both", expand=True, padx=10, pady=4)
        txt = tk.Text(fr, bg="#11111b", fg=PALETTE["text"],
                      font=("Consolas", 9), relief="flat", wrap="word",
                      state="normal")
        sb = ttk.Scrollbar(fr, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)
        txt.tag_config("section", foreground=PALETTE["mauve"],
                        font=("Consolas", 10, "bold"))
        txt.tag_config("pos",  foreground=PALETTE["green"])
        txt.tag_config("neg",  foreground=PALETTE["red"])
        txt.tag_config("warn", foreground=PALETTE["yellow"])

        def _section(header, content, tag=""):
            if not content:
                return
            txt.insert("end", f"\n── {header} ─────────────────\n", "section")
            txt.insert("end", content + "\n", tag)

        _section("THE ROAST", roast.roast_text, "neg")

        if roast.boring_sections:
            txt.insert("end", "\n── BORING PATCHES ──────────────────\n", "section")
            for tc, reason in roast.boring_sections:
                txt.insert("end", f"  {tc}  {reason}\n", "warn")

        if roast.logic_issues:
            txt.insert("end", "\n── LOGIC / CLARITY ISSUES ──────────\n", "section")
            for issue in roast.logic_issues:
                txt.insert("end", f"  • {issue}\n", "warn")

        if roast.clarity_issues:
            txt.insert("end", "\n── MUST FIX ─────────────────────────\n", "section")
            for c in roast.clarity_issues:
                txt.insert("end", f"  ✗ {c}\n", "neg")

        if roast.positive_notes:
            txt.insert("end", "\n── WHAT ACTUALLY WORKED ─────────────\n", "section")
            for p in roast.positive_notes:
                txt.insert("end", f"  ✓ {p}\n", "pos")

        txt.configure(state="disabled")

        bf = ttk.Frame(win)
        bf.pack(fill="x", padx=10, pady=6)

        # "Send to Council" only works when embedded in the full council
        if hasattr(self, "nb") and self.nb is not None:
            ttk.Button(bf, text="Send roast to Council",
                       command=lambda: (
                           self._set_text(self.input,
                                          f"Roast Grade: {roast.grade}\n\n"
                                          + roast.roast_text),
                           self.nb.select(self.tab_council),
                           win.destroy()
                       )).pack(side="left")
        else:
            # Standalone: copy to clipboard
            ttk.Button(bf, text="Copy roast to clipboard",
                       command=lambda: (
                           win.clipboard_clear(),
                           win.clipboard_append(roast.roast_text),
                       )).pack(side="left")

        ttk.Button(bf, text="Close", command=win.destroy).pack(side="right")

    def _video_show_algorithm_notes(self):
        a     = self._last_video_analysis
        notes = getattr(a, "algorithm_notes", "") if a else ""
        if not notes:
            messagebox.showinfo("Algorithm Notes",
                                "No Algorithm notes available.\n"
                                "Run analysis with '📦 Algorithm' enabled.",
                                parent=self.winfo_toplevel())
            return
        self._video_text_popup("📦 Algorithm — Retention & Packaging",
                               notes, Path(a.video_path).name)

    def _video_show_coach_notes(self):
        a     = self._last_video_analysis
        notes = getattr(a, "coach_notes", "") if a else ""
        if not notes:
            messagebox.showinfo("Coach Notes",
                                "No Coach notes available.\n"
                                "Run analysis with '🎙 Coach' enabled.",
                                parent=self.winfo_toplevel())
            return
        self._video_text_popup("🎙 Coach — Delivery & Pacing",
                               notes, Path(a.video_path).name)

    def _video_text_popup(self, title: str, text: str, subtitle: str = ""):
        """Generic colour-coded read-only popup for algorithm / coach notes."""
        win = tk.Toplevel(self.winfo_toplevel())
        win.title(title)
        win.configure(bg=PALETTE["bg"])
        win.geometry("860x600")

        hdr = ttk.Frame(win)
        hdr.pack(fill="x", padx=12, pady=(8, 4))
        ttk.Label(hdr, text=title, foreground=PALETTE["blue"],
                  font=("", 12, "bold")).pack(side="left")
        if subtitle:
            ttk.Label(hdr, text=f"  {subtitle}",
                      foreground=PALETTE["overlay"]).pack(side="left")

        txt_frame = ttk.Frame(win)
        txt_frame.pack(fill="both", expand=True, padx=10, pady=4)
        txt = self._make_text(txt_frame, wrap="word", state="normal")
        sb  = ttk.Scrollbar(txt_frame, command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True)

        txt.tag_config("hdr",  foreground=PALETTE["blue"],
                                font=("Consolas", 9, "bold"))
        txt.tag_config("pos",  foreground=PALETTE["green"])
        txt.tag_config("warn", foreground="#fab387")
        txt.tag_config("neg",  foreground=PALETTE["red"])

        for line in text.splitlines():
            upper = line.upper().strip()
            if any(upper.startswith(h) for h in (
                "HOOK", "RETENTION", "PACING", "ENERGY", "CLARITY",
                "CONFIDENCE", "WORST", "DRILL", "TITLE", "DESCRIPTION",
                "OPEN LOOP", "PATTERN", "ALGORITHM", "DELIVERY GRADE",
                "WHAT'S WORKING",
            )):
                txt.insert("end", line + "\n", "hdr")
            elif "✓" in line or "WORKING" in line.upper():
                txt.insert("end", line + "\n", "pos")
            elif any(w in line.upper()
                     for w in ("WEAK", "MISSING", "FAILS", "BAD", "WORST")):
                txt.insert("end", line + "\n", "neg")
            elif line.startswith("-") or line.startswith("•"):
                txt.insert("end", line + "\n", "warn")
            else:
                txt.insert("end", line + "\n")

        txt.configure(state="disabled")

        bf = ttk.Frame(win)
        bf.pack(fill="x", padx=10, pady=6)

        if hasattr(self, "nb") and self.nb is not None:
            ttk.Button(bf, text="Send to Council",
                       command=lambda: (
                           self._set_text(self.input, text),
                           self.nb.select(self.tab_council),
                           win.destroy(),
                       )).pack(side="left")
        else:
            ttk.Button(bf, text="Copy to clipboard",
                       command=lambda: (
                           win.clipboard_clear(),
                           win.clipboard_append(text),
                       )).pack(side="left")

        ttk.Button(bf, text="Close", command=win.destroy).pack(side="right")

    # =========================================================
    # Queue — management helpers
    # =========================================================

    def _queue_file(self) -> Path:
        return self.vault_dir / "video_queue.json"

    def _queue_tv_refresh(self):
        self._queue_tv.delete(*self._queue_tv.get_children())
        for i, item in enumerate(self._video_queue_items):
            dur = (f"{int(item.duration_s//60)}:{int(item.duration_s%60):02d}"
                   if item.duration_s > 0 else "—")
            self._queue_tv.insert(
                "", "end",
                iid=str(i),
                values=(i + 1, item.label, item.type_icon,
                        f"{item.status_icon} {item.status}", dur),
                tags=(item.status,),
            )
        n      = len(self._video_queue_items)
        done   = sum(1 for x in self._video_queue_items if x.status == "done")
        errors = sum(1 for x in self._video_queue_items if x.status == "error")
        self._queue_status_var.set(
            f"{n} items  ·  {done} done  ·  {errors} errors"
            if n else "Queue empty")

    def _queue_selected_idx(self) -> Optional[int]:
        sel = self._queue_tv.selection()
        return int(sel[0]) if sel else None

    def _queue_add_current(self):
        path = self._video_path_var.get().strip()
        if not path:
            messagebox.showwarning("Queue", "Select a video file first.",
                                    parent=self.winfo_toplevel())
            return
        self._queue_enqueue(Path(path), self._queue_type_var.get())

    def _queue_browse_add(self):
        from tkinter import filedialog as _fd
        paths = _fd.askopenfilenames(
            title="Add videos to queue",
            filetypes=[
                ("Video files", "*.mp4 *.mov *.avi *.mkv *.webm *.m4v *.flv *.wmv"),
                ("All files", "*.*"),
            ],
        )
        for p in paths:
            self._queue_enqueue(Path(p), self._queue_type_var.get())

    def _queue_enqueue(self, path: Path, video_type: str = "raw"):
        if not _VIDEO_OK:
            return
        item = vp.VideoQueueItem(path=str(path), video_type=video_type)
        self._video_queue_items.append(item)
        self._queue_tv_refresh()
        self._queue_save()
        self._video_log_append(f"  + Queued [{item.type_icon}]: {item.label}")

    def _queue_remove(self):
        idx = self._queue_selected_idx()
        if idx is None:
            return
        item = self._video_queue_items[idx]
        if item.status == "processing":
            messagebox.showwarning("Queue",
                                    "Cannot remove an item currently being processed.",
                                    parent=self.winfo_toplevel())
            return
        self._video_queue_items.pop(idx)
        self._queue_tv_refresh()
        self._queue_save()

    def _queue_move_up(self):
        idx = self._queue_selected_idx()
        if idx is None or idx == 0:
            return
        q = self._video_queue_items
        q[idx - 1], q[idx] = q[idx], q[idx - 1]
        self._queue_tv_refresh()
        self._queue_tv.selection_set(str(idx - 1))
        self._queue_save()

    def _queue_move_down(self):
        idx = self._queue_selected_idx()
        if idx is None or idx >= len(self._video_queue_items) - 1:
            return
        q = self._video_queue_items
        q[idx + 1], q[idx] = q[idx], q[idx + 1]
        self._queue_tv_refresh()
        self._queue_tv.selection_set(str(idx + 1))
        self._queue_save()

    def _queue_change_type(self):
        idx = self._queue_selected_idx()
        if idx is None:
            return
        item = self._video_queue_items[idx]
        if item.status == "processing":
            return
        item.video_type = self._queue_type_var.get()
        self._queue_tv_refresh()
        self._queue_save()

    def _queue_clear_done(self):
        self._video_queue_items = [
            x for x in self._video_queue_items
            if x.status not in ("done", "error", "skipped")
        ]
        self._queue_tv_refresh()
        self._queue_save()

    def _queue_clear_all(self):
        if self._queue_running:
            messagebox.showwarning("Queue", "Stop the queue first before clearing.",
                                    parent=self.winfo_toplevel())
            return
        self._video_queue_items.clear()
        self._queue_tv_refresh()
        self._queue_save()

    # =========================================================
    # Queue — persistence
    # =========================================================

    def _queue_save(self):
        if not _VIDEO_OK:
            return
        try:
            data = [item.to_dict() for item in self._video_queue_items]
            self._queue_file().write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8")
        except Exception:
            pass  # non-fatal

    def _queue_load(self):
        if not _VIDEO_OK:
            return
        qf = self._queue_file()
        if not qf.exists():
            messagebox.showinfo("Queue", "No saved queue found.",
                                parent=self.winfo_toplevel())
            return
        if self._video_queue_items:
            if not messagebox.askyesno("Queue",
                                        "Replace current queue with saved queue?",
                                        parent=self.winfo_toplevel()):
                return
        try:
            data = json.loads(qf.read_text(encoding="utf-8"))
            self._video_queue_items = [vp.VideoQueueItem.from_dict(d) for d in data]
            for item in self._video_queue_items:
                if item.status == "processing":
                    item.status = "queued"
            self._queue_tv_refresh()
            self._video_log_append(
                f"✓ Loaded queue: {len(self._video_queue_items)} items")
        except Exception as e:
            messagebox.showerror("Queue", f"Load error: {e}",
                                  parent=self.winfo_toplevel())

    def _queue_autoload(self):
        if not _VIDEO_OK:
            return
        try:
            qf = self._queue_file()
            if qf.exists():
                data  = json.loads(qf.read_text(encoding="utf-8"))
                items = [vp.VideoQueueItem.from_dict(d) for d in data]
                for item in items:
                    if item.status == "processing":
                        item.status = "queued"
                self._video_queue_items = items
                self._queue_tv_refresh()
        except Exception:
            pass

    # =========================================================
    # Queue — runner
    # =========================================================

    def _queue_run(self):
        if not _VIDEO_OK:
            return
        pending = [x for x in self._video_queue_items if x.status == "queued"]
        if not pending:
            messagebox.showinfo("Queue", "No queued items to process.",
                                parent=self.winfo_toplevel())
            return
        if self._queue_running:
            return

        self._queue_running   = True
        self._queue_paused    = False
        self._queue_stop_flag = False
        self._queue_run_btn.configure(state="disabled")
        self._queue_pause_btn.configure(state="normal")
        self._video_log_append(
            f"▶ Queue started — {len(pending)} item(s) to process")

        def _runner():
            for idx, item in enumerate(self._video_queue_items):
                if item.status != "queued":
                    continue
                if self._queue_stop_flag:
                    break

                while self._queue_paused and not self._queue_stop_flag:
                    import time as _t; _t.sleep(0.5)
                if self._queue_stop_flag:
                    break

                self._queue_current_idx = idx
                item.status = "processing"
                self.after(0, self._queue_tv_refresh)
                self._video_log_append(
                    f"\n▶▶ Queue [{idx+1}/{len(self._video_queue_items)}]: "
                    f"{item.label}  [{item.type_icon}]")

                try:
                    flags  = self._queue_flags_for(item)
                    result = self._video_proc.process(
                        item.path,
                        **flags,
                        progress_cb = self._video_log_append,
                        cancelled   = lambda: self._queue_stop_flag,
                    )
                    self._last_video_analysis = result
                    item.status     = "done"
                    item.duration_s = result.duration_s
                    saved = self._video_proc.list_analyses()
                    if saved:
                        item.result_path = str(saved[0])
                    self._video_log_append(
                        f"  ✓ Queue item done: {item.label}"
                        + (f"  Grade: {result.roast.grade}" if result.roast else ""))
                    if result.vibe_summary or result.roast:
                        self._queue_post_summary(item, result)

                except Exception as e:
                    import traceback as _tb
                    item.status    = "error"
                    item.error_msg = str(e)
                    self._video_log_append(f"  ✗ Queue error [{item.label}]: {e}")
                    self._video_log_append(_tb.format_exc()[:300])

                self.after(0, self._queue_tv_refresh)
                self._queue_save()

            # Runner finished
            self._queue_running   = False
            self._queue_stop_flag = False
            done   = sum(1 for x in self._video_queue_items if x.status == "done")
            errors = sum(1 for x in self._video_queue_items if x.status == "error")
            self._video_log_append(
                f"\n✓ Queue finished — {done} done, {errors} errors")
            self.after(0, lambda: (
                self._queue_run_btn.configure(state="normal"),
                self._queue_pause_btn.configure(state="disabled"),
                self._queue_tv_refresh(),
            ))

        threading.Thread(target=_runner, daemon=True).start()

    def _queue_pause(self):
        if not self._queue_running:
            return
        self._queue_paused = not self._queue_paused
        lbl = "▶  Resume" if self._queue_paused else "⏸  Pause"
        self._queue_pause_btn.configure(text=lbl)
        self._video_log_append(
            "⏸ Queue paused — will stop after current item finishes."
            if self._queue_paused else
            "▶  Queue resumed.")

    def _queue_stop(self):
        if not self._queue_running:
            return
        self._queue_stop_flag = True
        self._queue_paused    = False
        self._video_log_append("■ Queue stop requested — finishing current item…")

    def _queue_flags_for(self, item) -> dict:
        """
        Return VideoProcessor.process() kwargs for this queue item.
        raw    → full analysis preset
        edited → QC-only preset  (no roast, no coach)
        custom → mirrors the current options panel
        """
        if not _VIDEO_OK:
            return {}
        preset = vp.VIDEO_TYPE_PRESETS.get(item.video_type,
                                            vp.VIDEO_TYPE_PRESETS["raw"]).copy()

        _roast_role = self._roast_model_var.get()
        personality = (getattr(self, _roast_role, None)
                       or getattr(self, "director", None)
                       or getattr(self, "writer", None))
        sage_m      = (getattr(self, "sage", None)
                       if self._do_sage_logic_var.get() else None)
        style_mgr   = getattr(self, "_content_style", None)

        base = {
            "whisper_model":          self._whisper_model_var.get(),
            "whisper_device":         self._whisper_device_var.get(),
            "vision_model":           self._vision_model_var.get(),
            "personality_model":      personality,
            "content_style_manager":  style_mgr,
            "sage_model":             sage_m,
            "algorithm_model":        getattr(self, "algorithm", None),
            "coach_model":            getattr(self, "coach", None),
        }

        if item.video_type == "custom":
            base.update({
                "do_frames":            bool(self._do_frames_var.get()),
                "frame_interval_s":     int(self._frame_interval_var.get() or 10),
                "max_frames":           int(self._max_frames_var.get() or 20),
                "do_audio_analysis":    bool(self._do_audio_quality_var.get()),
                "do_energy_profile":    bool(self._do_energy_var.get()),
                "do_visual_analysis":   bool(self._do_visual_var.get()),
                "do_edit_suggestions":  True,
                "do_roast":             bool(self._do_roast_var.get()),
            })
        else:
            base.update(preset)
            if item.video_type == "edited":
                base["do_roast"]            = False
                base["do_edit_suggestions"] = False
                base["coach_model"]         = None  # delivery coaching is pre-edit only

        return base

    def _queue_post_summary(self, item, result):
        """Post a brief summary of a completed queue item to the transcript."""
        parts = [f"✓ Queue: {item.label}  [{item.type_icon}]"]
        if result.roast:
            parts.append(f"🔥 Grade: {result.roast.grade}")
            fillers = sum(result.roast.filler_word_hits.values())
            if fillers:
                parts.append(f"   Filler words: {fillers}")
            if result.roast.boring_sections:
                parts.append(f"   Boring patches: {len(result.roast.boring_sections)}")
        if result.edit_suggestions:
            high = [s for s in result.edit_suggestions if s.priority == "high"]
            parts.append(
                f"📋 {len(result.edit_suggestions)} edit suggestions "
                f"({len(high)} high priority)")
        if result.audio_quality and result.audio_quality.has_clipping:
            parts.append("⚠ CLIPPING detected in audio")
        if getattr(result, "algorithm_notes", "") and result.algorithm_notes:
            import re as _re
            hm = _re.search(r"HOOK VERDICT:\s*(.+)",
                             result.algorithm_notes, _re.IGNORECASE)
            if hm:
                parts.append(f"📦 Algorithm hook: {hm.group(1).strip()[:80]}")
        if getattr(result, "coach_notes", "") and result.coach_notes:
            import re as _re2
            gm = _re2.search(r"DELIVERY GRADE:\s*([A-F][+-]?)",
                              result.coach_notes, _re2.IGNORECASE)
            if gm:
                parts.append(f"🎙 Coach delivery grade: {gm.group(1)}")
        if result.vibe_summary:
            parts.append(f"\n{result.vibe_summary[:200]}")

        _summary = "\n".join(parts)
        self.after(0, lambda s=_summary: self._append_transcript(
            "Video Queue", s, "final"))

    def _queue_show_result(self, event=None):
        """Double-click a queue row: show edit suggestions / error details."""
        idx = self._queue_selected_idx()
        if idx is None:
            return
        item = self._video_queue_items[idx]
        if item.status == "error":
            messagebox.showerror("Queue Error",
                                  f"{item.label}\n\n{item.error_msg}",
                                  parent=self.winfo_toplevel())
            return
        if item.status != "done":
            messagebox.showinfo("Queue",
                                 f"{item.label} — status: {item.status}",
                                 parent=self.winfo_toplevel())
            return
        if item.result_path and Path(item.result_path).exists() and self._video_proc:
            a = self._video_proc.load_analysis(Path(item.result_path))
            if a:
                self._last_video_analysis = a
                self._video_show_edit_suggestions()
                return
        messagebox.showinfo("Queue",
                             f"{item.label} — done but result file not found.\n"
                             f"Expected: {item.result_path}",
                             parent=self.winfo_toplevel())

    # =========================================================
    # Transcript / history helpers
    # =========================================================

    def _video_send_transcript(self):
        """Send the last analysis transcript to the Council input (or clipboard)."""
        if (not self._last_video_analysis
                or not self._last_video_analysis.transcript):
            messagebox.showinfo("Video",
                                "No transcript available. Run analysis first.",
                                parent=self.winfo_toplevel())
            return
        txt = self._last_video_analysis.full_transcript_text
        if len(txt) > 3000:
            txt = txt[:3000] + "\n\n[... transcript truncated — full version in vault ...]"

        if hasattr(self, "nb") and self.nb is not None:
            self._set_text(self.input, txt)
            self.nb.select(self.tab_council)
        else:
            self._append_transcript("Transcript", txt)

    def _video_show_history(self):
        if not self._video_proc:
            return
        analyses = self._video_proc.list_analyses()
        if not analyses:
            messagebox.showinfo("Video", "No past analyses found.",
                                parent=self.winfo_toplevel())
            return

        win = tk.Toplevel(self.winfo_toplevel())
        win.title("Past Video Analyses")
        win.configure(bg=PALETTE["bg"])
        win.geometry("680x480")

        ttk.Label(win, text="Past analyses — double-click to view",
                  foreground=PALETTE["overlay"]).pack(anchor="w", padx=12, pady=(8, 4))

        lb_frame = ttk.Frame(win)
        lb_frame.pack(fill="both", expand=True, padx=12, pady=(0, 4))
        lb = tk.Listbox(lb_frame, bg="#11111b", fg=PALETTE["text"],
                        font=("Consolas", 9), selectmode="single")
        lb_sb = ttk.Scrollbar(lb_frame, command=lb.yview)
        lb.configure(yscrollcommand=lb_sb.set)
        lb_sb.pack(side="right", fill="y")
        lb.pack(fill="both", expand=True)

        for p in analyses:
            lb.insert("end", p.name)

        detail = tk.Text(win, height=10, bg="#11111b", fg=PALETTE["text"],
                         font=("Consolas", 9), state="disabled",
                         relief="flat", wrap="word")
        detail.pack(fill="x", padx=12, pady=(0, 4))

        def _on_select(event=None):
            sel = lb.curselection()
            if not sel:
                return
            p = analyses[sel[0]]
            a = self._video_proc.load_analysis(p)
            if not a:
                return
            detail.configure(state="normal")
            detail.delete("1.0", "end")
            detail.insert("end", "File: " + Path(a.video_path).name + "\n")
            detail.insert("end", "Processed: " + a.processed_at + "\n")
            detail.insert("end",
                          "Segments: " + str(len(a.transcript)) +
                          "  Frames: " + str(len(a.frame_descriptions)) + "\n")
            if getattr(a, "audio_quality", None):
                aq = a.audio_quality
                detail.insert("end",
                    f"Audio: {aq.integrated_lufs:.1f} LUFS  RMS {aq.rms_db:.1f} dB"
                    + ("  ⚠ CLIPPING" if aq.has_clipping else "")
                    + ("  ⚠ Too quiet" if aq.is_too_quiet else "")
                    + "\n")
            if getattr(a, "roast", None):
                detail.insert("end",
                    f"Roast grade: {a.roast.grade}  "
                    f"Filler words: {sum(a.roast.filler_word_hits.values())}\n")
            if getattr(a, "edit_suggestions", None):
                detail.insert("end",
                    f"Edit suggestions: {len(a.edit_suggestions)}\n")
            detail.insert("end", "\n")
            if a.vibe_summary:
                detail.insert("end", "VIBE:\n" + a.vibe_summary + "\n\n")
            if a.pacing_notes:
                detail.insert("end", "PACING:\n" + a.pacing_notes + "\n")
            if getattr(a, "roast", None) and a.roast.roast_text:
                detail.insert("end",
                    f"\nROAST EXCERPT:\n{a.roast.roast_text[:400]}...\n")
            detail.configure(state="disabled")

        def _load_selected():
            sel = lb.curselection()
            if not sel:
                return
            p = analyses[sel[0]]
            a = self._video_proc.load_analysis(p)
            if a:
                self._last_video_analysis = a
                self._video_log_append(f"✓ Loaded analysis: {p.name}")
                win.destroy()

        lb.bind("<<ListboxSelect>>", _on_select)
        lb.bind("<Double-1>", lambda e: _load_selected())

        bf = ttk.Frame(win)
        bf.pack(fill="x", padx=12, pady=(0, 8))
        ttk.Button(bf, text="Load Selected",
                   command=_load_selected).pack(side="left")
        ttk.Button(bf, text="Close", command=win.destroy).pack(side="right")


# ============================================================
# VideoApp — standalone runner (python tab_video.py)
# ============================================================

class VideoApp(StandaloneHost, VideoTabMixin):
    """
    Full Video Analyser running as its own window.

    Usage:
        python tab_video.py [--vault PATH] [--no-ai]
    """

    def __init__(self, vault_dir: Optional[Path] = None, no_ai: bool = False):
        StandaloneHost.__init__(
            self,
            vault_dir = vault_dir,
            title     = "Video Analyser",
            geometry  = "1200x820",
        )

        if not no_ai:
            self._init_models()

        # Build the video tab directly into root
        self._build_video_tab(parent=self.root)

    def _poll_queue(self):
        """Drain the UI queue (no extra items needed for standalone video)."""
        self.root.after(80, self._poll_queue)


def run_standalone():
    import argparse
    ap = argparse.ArgumentParser(description="Council Video Analyser — standalone")
    ap.add_argument("--vault", metavar="PATH",
                    help="Path to vault directory (default: ~/council_vault)")
    ap.add_argument("--no-ai", action="store_true",
                    help="Disable AI models (transcription + signal analysis only)")
    args = ap.parse_args()

    app = VideoApp(
        vault_dir = Path(args.vault) if args.vault else None,
        no_ai     = args.no_ai,
    )
    app.run()


if __name__ == "__main__":
    run_standalone()
