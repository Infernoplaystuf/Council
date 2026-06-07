# ============================================================
# onboarding.py  —  First-run wizard
# ============================================================
# Walks a new user through the three things they need before their
# first deliberation can succeed:
#
#   1. Acknowledgement of disk-space requirements
#   2. Point the app at a GGUF model file (or show download links)
#   3. Confirm ready-to-go
#
# The app is GGUF-only — Ollama is not used. The wizard writes the
# chosen path to vault/backend_settings.json (same file the in-app
# Browse button on the Council tab uses), so the picker stays in
# sync across launches.
#
# Marker file: vault/.onboarded — its presence skips the wizard.
# Delete the file to re-run onboarding.
# ============================================================

from __future__ import annotations

import json
import platform
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox, filedialog
from typing import Callable, List, Optional

import branding

import os as _os


# ============================================================
# Recommended GGUF models — surfaced as download buttons in the
# model step. Sourced from model_catalog.MODELS (US-origin only).
# Add or reorder models there; this list rebuilds automatically.
# ============================================================

try:
    import model_catalog as _mc

    def _spec_to_legacy(spec) -> dict:
        return {
            "name":  spec.name,
            "size":  f"~{spec.size_gb:.1f} GB",
            "ctx":   f"{spec.context_k}K context",
            "url":   f"https://huggingface.co/{spec.hf_repo}",
            "blurb": f"{spec.org}. {spec.blurb}",
            "spec":  spec,
        }

    RECOMMENDED_MODELS = [_spec_to_legacy(s) for s in _mc.MODELS]
    DEFAULT_MODEL_ID   = _mc.DEFAULT_MODEL_ID
except Exception as _exc:
    # Hard fallback so the wizard still runs if model_catalog ever errors.
    RECOMMENDED_MODELS = [
        {
            "name":  "IBM Granite 3.1 8B Instruct (Q4_K_M)",
            "size":  "~5 GB",
            "ctx":   "128K context",
            "url":   "https://huggingface.co/ibm-granite/granite-3.1-8b-instruct-GGUF",
            "blurb": "IBM. Solid baseline; conservative refusal behaviour.",
        },
    ]
    DEFAULT_MODEL_ID = "granite-3.1-8b-q4"


# ============================================================
# Persistence — shared with the in-app Browse button on the
# Council tab. Keep the filename and JSON key in sync with
# council_gui_engine._backend_settings_path / _save_backend_settings.
# ============================================================

_BACKEND_SETTINGS_FILENAME = "backend_settings.json"


def _backend_settings_path(vault_dir: Path) -> Path:
    return vault_dir / _BACKEND_SETTINGS_FILENAME


def load_gguf_path(vault_dir: Path) -> str:
    """Return the persisted GGUF model path, or empty string if none.

    Reads COUNCIL_GGUF_PATH from the environment first (a launch-time
    override always wins), then falls back to vault/backend_settings.json.
    """
    env = _os.environ.get("COUNCIL_GGUF_PATH", "").strip()
    if env:
        return env
    p = _backend_settings_path(vault_dir)
    if not p.exists():
        return ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return ""
    return str(data.get("gguf_path", "")).strip()


def save_gguf_path(vault_dir: Path, path: str) -> None:
    """Persist the GGUF model path so the next launch picks it up.

    Also sets COUNCIL_GGUF_PATH in os.environ for the current process so
    the wizard's choice is live immediately — no app restart needed.
    """
    try:
        _backend_settings_path(vault_dir).write_text(
            json.dumps({"gguf_path": path}, indent=2),
            encoding="utf-8",
        )
        _os.environ["COUNCIL_GGUF_PATH"] = path
    except Exception:
        pass


def gguf_file_status(path: str) -> tuple:
    """Inspect a candidate .gguf path. Returns (ok: bool, message: str)."""
    if not path:
        return False, "(no model selected)"
    p = Path(path)
    if not p.exists():
        return False, f"✗ File does not exist: {p}"
    if not p.is_file():
        return False, f"✗ Not a file: {p}"
    if p.suffix.lower() != ".gguf":
        return False, f"⚠ Not a .gguf extension: {p.name}"
    try:
        size_gb = p.stat().st_size / (1024 ** 3)
    except Exception:
        return True, f"✓ {p.name}"
    return True, f"✓ {p.name} ({size_gb:.1f} GB)"


# ============================================================
# Wizard
# ============================================================

class OnboardingWizard(tk.Toplevel):
    """
    Modal wizard shown on first launch. Returns when the user finishes
    or cancels. The host application should check `wizard.completed`
    after the wizard closes.
    """

    def __init__(self, parent: tk.Tk, vault_dir: Path):
        super().__init__(parent)
        self.parent     = parent
        self.vault_dir  = vault_dir
        self.completed  = False
        self.skipped    = False

        self.title(f"Welcome to {branding.PRODUCT_NAME}")
        try:
            branding.apply_window_icon(self)
        except Exception:
            pass
        self.geometry("640x520")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        # Step list — dropped "ollama" step (the app is GGUF-only;
        # no daemon to detect or service to install). The model step
        # now does GGUF file selection directly.
        self._steps = ["welcome", "disk", "model", "ready"]
        self._step_idx = 0

        self._build_ui()
        self._render_step()

        # Center on parent
        self.update_idletasks()
        try:
            px = parent.winfo_rootx() + (parent.winfo_width() - 640) // 2
            py = parent.winfo_rooty() + (parent.winfo_height() - 520) // 2
            self.geometry(f"+{max(0, px)}+{max(0, py)}")
        except Exception:
            pass

    # ---- Layout -----------------------------------------------------

    def _build_ui(self):
        theme = branding.get_theme("dark")
        self.configure(bg=theme["bg"])

        # Header
        header = tk.Frame(self, bg=theme["panel_bg"], height=72)
        header.pack(fill="x", side="top")
        header.pack_propagate(False)
        tk.Label(header, text=branding.PRODUCT_NAME,
                 font=("Segoe UI", 20, "bold"),
                 bg=theme["panel_bg"], fg=theme["fg"]).pack(side="left", padx=20, pady=8)
        tk.Label(header, text=f"Setup • v{branding.VERSION}",
                 font=("Segoe UI", 10),
                 bg=theme["panel_bg"], fg=theme["muted_fg"]).pack(side="left", padx=4, pady=8)

        # Body — replaced per step
        self.body = tk.Frame(self, bg=theme["bg"])
        self.body.pack(fill="both", expand=True, padx=24, pady=16)

        # Footer
        footer = tk.Frame(self, bg=theme["bg"])
        footer.pack(fill="x", side="bottom", padx=20, pady=14)
        self.btn_skip = ttk.Button(footer, text="Skip setup", command=self._on_skip)
        self.btn_skip.pack(side="left")
        self.btn_next = ttk.Button(footer, text="Next →", command=self._on_next)
        self.btn_next.pack(side="right")
        self.btn_back = ttk.Button(footer, text="← Back", command=self._on_back)
        self.btn_back.pack(side="right", padx=6)

    def _clear_body(self):
        for w in self.body.winfo_children():
            w.destroy()

    def _theme(self):
        return branding.get_theme("dark")

    # ---- Step rendering --------------------------------------------

    def _render_step(self):
        self._clear_body()
        step = self._steps[self._step_idx]
        getattr(self, f"_render_{step}")()
        self.btn_back.configure(state="disabled" if self._step_idx == 0 else "normal")
        # Last step: change Next to Finish
        if self._step_idx == len(self._steps) - 1:
            self.btn_next.configure(text="Finish")
        else:
            self.btn_next.configure(text="Next →")

    def _label(self, text, *, font=("Segoe UI", 11), pady=6, fg=None):
        t = self._theme()
        tk.Label(self.body, text=text, font=font,
                 bg=t["bg"], fg=fg or t["fg"],
                 wraplength=580, justify="left", anchor="w"
                 ).pack(fill="x", pady=pady)

    def _heading(self, text):
        self._label(text, font=("Segoe UI", 16, "bold"), pady=(0, 12))

    # Step 1: welcome
    def _render_welcome(self):
        self._heading(f"{branding.PRODUCT_NAME} — first launch")
        self._label(branding.PRODUCT_TAGLINE, font=("Segoe UI", 12), pady=(0, 14))
        self._label(
            "Quick walkthrough — about two minutes. We'll:\n"
            "   • Confirm there's enough disk space for an AI model\n"
            "   • Point the app at a GGUF model file (we'll suggest a few)\n\n"
            "Everything runs locally; nothing leaves this machine.\n"
            "The model never sends your data to the cloud.\n\n"
            "Skip if you'd rather configure things by hand "
            f"(set COUNCIL_GGUF_PATH=<path-to-.gguf> in your environment)."
        )

    # Step 2: disk space warning
    def _render_disk(self):
        self._heading("Disk space")
        # Try to detect free space
        try:
            import shutil as _sh
            free_bytes = _sh.disk_usage(str(self.vault_dir)).free
            free_gb = free_bytes / (1024 ** 3)
            free_str = f"{free_gb:.1f} GB free"
        except Exception:
            free_gb = None
            free_str = "(could not detect)"

        self._label(
            "GGUF models vary in size depending on the chosen quantization:\n"
            "   • 7-8B models at Q4-Q5 are typically 5-7 GB\n"
            "   • 14B models at Q4 are around 9 GB\n"
            "   • 70B models at Q4 are 40+ GB (only if you have a beefy GPU)\n\n"
            f"Detected free space on this drive: {free_str}",
            pady=(0, 12),
        )
        if free_gb is not None and free_gb < 10:
            self._label(
                "⚠ Less than 10 GB free — you may want to clear some space, "
                "or pick a smaller 7-8B Q4 model on the next step.",
                fg=self._theme()["warning"],
            )
        elif free_gb is not None:
            self._label(
                "✓ You have enough space for any recommended model.",
                fg=self._theme()["success"],
            )

    # Step 3: Model selection (GGUF file picker)
    def _render_model(self):
        self._heading("Choose a GGUF model")

        self._label(
            "The app loads models via .gguf files (the standard local-LLM "
            "format from the llama.cpp project). You can:\n"
            "   • Pick a .gguf file already on this machine\n"
            "   • Download a recommended one from Hugging Face\n",
            pady=(0, 12),
        )

        # Current path status
        self._gguf_path_var = tk.StringVar(value=load_gguf_path(self.vault_dir))
        self._gguf_status_var = tk.StringVar(value="")
        self._refresh_gguf_status()

        path_frame = tk.Frame(self.body, bg=self._theme()["bg"])
        path_frame.pack(fill="x", pady=(0, 4))
        tk.Label(path_frame, text="Current model:", font=("Segoe UI", 10),
                 bg=self._theme()["bg"], fg=self._theme()["muted_fg"]
                 ).pack(side="left", padx=(0, 6))
        tk.Label(path_frame, textvariable=self._gguf_status_var,
                 font=("Consolas", 10),
                 bg=self._theme()["bg"], fg=self._theme()["fg"]
                 ).pack(side="left", fill="x", expand=True)

        # Browse button
        btn_frame = tk.Frame(self.body, bg=self._theme()["bg"])
        btn_frame.pack(fill="x", pady=8)
        ttk.Button(btn_frame, text="📁 Browse for .gguf file…",
                   command=self._browse_gguf).pack(side="left")
        ttk.Button(btn_frame, text="🔄 Re-check",
                   command=self._refresh_gguf_status).pack(side="left", padx=8)

        # Recommended models list (US-origin catalog).
        sep = tk.Frame(self.body, bg=self._theme()["muted_fg"], height=1)
        sep.pack(fill="x", pady=12)
        self._label(
            f"Recommended US-origin models ({len(RECOMMENDED_MODELS)}) — click "
            "Open to visit the Hugging Face page, or Auto-download to pull "
            "the .gguf straight into ./models/ and select it automatically.",
            font=("Segoe UI", 10), fg=self._theme()["muted_fg"],
            pady=(0, 8))

        # Scrollable container so the list grows beyond the wizard's
        # fixed height without spilling off-screen.
        list_wrap = tk.Frame(self.body, bg=self._theme()["bg"])
        list_wrap.pack(fill="both", expand=True)
        canvas = tk.Canvas(list_wrap, bg=self._theme()["bg"],
                           highlightthickness=0, height=200)
        canvas.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(list_wrap, orient="vertical",
                               command=canvas.yview)
        scroll.pack(side="right", fill="y")
        canvas.configure(yscrollcommand=scroll.set)
        inner = tk.Frame(canvas, bg=self._theme()["bg"])
        canvas.create_window((0, 0), window=inner, anchor="nw")
        def _on_resize(_e=None):
            canvas.configure(scrollregion=canvas.bbox("all"))
        inner.bind("<Configure>", _on_resize)
        # Mouse-wheel scroll inside the list
        def _on_mwheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mwheel)

        for m in RECOMMENDED_MODELS:
            row = tk.Frame(inner, bg=self._theme()["bg"])
            row.pack(fill="x", pady=3, padx=(0, 8))
            name_label = f"  {m['name']}"
            spec_obj = m.get("spec")
            if spec_obj is not None and getattr(spec_obj, "is_default", False):
                name_label = "  ★ " + m["name"]
            tk.Label(row, text=name_label,
                     font=("Segoe UI", 10, "bold"),
                     bg=self._theme()["bg"], fg=self._theme()["fg"],
                     ).pack(side="left")
            tk.Label(row, text=f"  ·  {m['size']}  ·  {m['ctx']}",
                     font=("Segoe UI", 9),
                     bg=self._theme()["bg"], fg=self._theme()["muted_fg"]
                     ).pack(side="left")
            # Right-aligned buttons: Open page, Auto-download
            ttk.Button(row, text="🌐 Open",
                       command=lambda u=m["url"]: self._open_url(u)
                       ).pack(side="right", padx=(4, 0))
            if spec_obj is not None:
                ttk.Button(row, text="⬇ Auto-download",
                           command=lambda s=spec_obj: self._auto_download(s)
                           ).pack(side="right", padx=(4, 0))

    def _browse_gguf(self):
        """Open a file dialog to pick a .gguf file; persist on selection."""
        start_dir = ""
        cur = self._gguf_path_var.get().strip()
        if cur:
            try:
                start_dir = str(Path(cur).parent)
            except Exception:
                start_dir = ""
        path = filedialog.askopenfilename(
            parent=self,
            title="Select GGUF model file",
            initialdir=start_dir or None,
            filetypes=[("GGUF model files", "*.gguf"), ("All files", "*.*")],
        )
        if not path:
            return
        self._gguf_path_var.set(path)
        save_gguf_path(self.vault_dir, path)
        # Refresh the model loader so subsequent in-app queries pick up
        # the new path without an app restart.
        try:
            import council_engine as _ce
            _ce.refresh_backend_config()
        except Exception:
            # If council_engine isn't fully loaded yet (or doesn't expose
            # refresh_backend_config in this build), the env-var change
            # alone is sufficient — the singleton model loader picks it
            # up lazily on next inference call.
            pass
        self._refresh_gguf_status()

    def _auto_download(self, spec) -> None:
        """Download a catalog model's .gguf via huggingface_hub and select
        it. Runs in a background thread so the Tk UI stays responsive."""
        import threading
        dest_dir = (Path(__file__).resolve().parent / "models")
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Confirm before downloading multi-GB content.
        if not messagebox.askyesno(
            "Auto-download model",
            f"Download {spec.name}?\n\n"
            f"From: {spec.hf_repo}\n"
            f"File: {spec.hf_file}\n"
            f"Size: ~{spec.size_gb:.1f} GB\n"
            f"Into: {dest_dir}\n\n"
            "This can take several minutes on a slow connection. The wizard "
            "stays open and the file's path is auto-selected when finished.",
            parent=self,
        ):
            return

        # Status label updates from worker thread via Tk's after-callback
        # mechanism (Tk isn't thread-safe but `after` queues to the UI loop).
        try:
            self._gguf_status_var.set(f"⏳ downloading {spec.hf_file}…")
        except Exception:
            pass

        def _worker():
            try:
                from huggingface_hub import hf_hub_download
                path = hf_hub_download(
                    repo_id=spec.hf_repo,
                    filename=spec.hf_file,
                    local_dir=str(dest_dir),
                )
                def _ok():
                    self._gguf_path_var.set(str(path))
                    save_gguf_path(self.vault_dir, str(path))
                    try:
                        import council_engine as _ce
                        _ce.refresh_backend_config()
                    except Exception:
                        pass
                    self._refresh_gguf_status()
                    messagebox.showinfo(
                        "Download complete",
                        f"Saved to:\n{path}\n\nThe wizard's model selection "
                        "now points at this file.",
                        parent=self,
                    )
                self.after(0, _ok)
            except Exception as exc:
                def _err():
                    self._gguf_status_var.set(f"✗ download failed: {exc!r}")
                    messagebox.showerror(
                        "Download failed",
                        f"{exc!r}\n\n"
                        "Common causes:\n"
                        " • No internet on the air-gapped host\n"
                        " • Corporate SSL inspection (try `pip install pip-system-certs`)\n"
                        " • Repo is gated and needs an HF login (`huggingface-cli login`)\n"
                        "\nYou can also click 🌐 Open to download manually.",
                        parent=self,
                    )
                self.after(0, _err)

        threading.Thread(target=_worker, daemon=True).start()

    def _refresh_gguf_status(self):
        path = self._gguf_path_var.get().strip()
        ok, msg = gguf_file_status(path)
        # Use the existing variable if present; otherwise this is called
        # before the label is built (e.g. step re-render). Render-safe.
        if hasattr(self, "_gguf_status_var"):
            self._gguf_status_var.set(msg)
        self._gguf_ok = ok

    # Step 4: ready
    def _render_ready(self):
        self._heading("Ready.")
        # Show what we settled on so the user knows the chosen model
        # name without having to scroll back.
        path = load_gguf_path(self.vault_dir)
        ok, msg = gguf_file_status(path)
        if ok:
            self._label(f"GGUF model:  {msg}", font=("Consolas", 10),
                        fg=self._theme()["success"], pady=(0, 12))
        else:
            self._label(
                "No GGUF model selected. The app will start, but you'll "
                "need to set COUNCIL_GGUF_PATH or use the Browse… button "
                "on the Council tab before any query can be answered.",
                fg=self._theme()["warning"], pady=(0, 12),
            )

        self._label(
            "Three ways to start poking at it:\n\n"
            "   1. Drop a CSV onto the Grapher tab — the Analyst suggests a "
            "chart and tells you what it sees.\n"
            "   2. Click 📦 Sample on the Grapher for some bundled fake data "
            "if you don't have a file handy.\n"
            "   3. Council tab → ask a question in plain English. The panel "
            "deliberates and gives you an answer.\n\n"
            "Tip: type 'context info' in the Council tab to see the model's "
            "current context-window budget and tune COUNCIL_GGUF_N_CTX if "
            "needed.\n\n"
            "Click Finish.",
            pady=(0, 14),
        )

    # ---- Navigation -------------------------------------------------

    def _on_next(self):
        if self._step_idx == len(self._steps) - 1:
            self._finish()
            return
        self._step_idx += 1
        self._render_step()

    def _on_back(self):
        if self._step_idx == 0:
            return
        self._step_idx -= 1
        self._render_step()

    def _on_skip(self):
        if not messagebox.askyesno(
            "Skip setup?",
            "Skipping means you'll need to set COUNCIL_GGUF_PATH manually "
            "(or use the Browse… button on the Council tab) before any "
            "query can be answered. Continue?",
            parent=self,
        ):
            return
        self.skipped = True
        self.completed = False
        self._mark_onboarded()
        self.destroy()

    def _finish(self):
        self.completed = True
        self._mark_onboarded()
        self.destroy()

    def _mark_onboarded(self):
        try:
            (self.vault_dir / ".onboarded").write_text(
                json.dumps({"version": branding.VERSION,
                            "skipped": self.skipped}),
                encoding="utf-8",
            )
        except Exception:
            pass

    # ---- helpers ----------------------------------------------------

    def _open_url(self, url: str):
        try:
            import webbrowser as _wb
            _wb.open(url)
        except Exception:
            pass


# ============================================================
# Entry point
# ============================================================

def needs_onboarding(vault_dir: Path) -> bool:
    """Return True if the wizard has not yet been completed/skipped."""
    return not (vault_dir / ".onboarded").exists()


def run_if_needed(parent: tk.Tk, vault_dir: Path,
                  on_complete: Optional[Callable[[bool], None]] = None) -> None:
    """
    Show the onboarding wizard if needed. Calls on_complete(completed_bool)
    after the wizard closes (or immediately if it wasn't shown).
    """
    if not needs_onboarding(vault_dir):
        if on_complete:
            on_complete(True)
        return

    wiz = OnboardingWizard(parent, vault_dir)
    parent.wait_window(wiz)
    if on_complete:
        on_complete(wiz.completed)
