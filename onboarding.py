# ============================================================
# onboarding.py  —  First-run wizard
# ============================================================
# Walks a new user through the four things they need before their
# first deliberation can succeed:
#
#   1. Acknowledgement of disk-space requirements
#   2. Ollama detection (or guidance to install)
#   3. Model availability (or pull a default)
#   4. Personality config sanity check
#
# Marker file: vault/.onboarded — its presence skips the wizard.
# Delete the file to re-run onboarding.
# ============================================================

from __future__ import annotations

import json
import platform
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox
from typing import Callable, List, Optional

import branding


# Default model the wizard offers to pull. The exact tag matters — the
# Council's backend layer expects this specific quantization.
# Chosen for: free, runs on a 16GB-RAM laptop, decent quality, English-first.
DEFAULT_MODEL     = "qwen2.5:14b-instruct-q4_K_M"
DEFAULT_MODEL_ALT = "qwen2.5:7b-instruct-q4_K_M"   # smaller fallback, same family

# Estimated download size we surface to the user up front.
DEFAULT_MODEL_GB = 9
ALT_MODEL_GB     = 5


# ============================================================
# Detection helpers
# ============================================================

def ollama_available(host: str = "http://127.0.0.1:11434") -> bool:
    """Return True if a local Ollama daemon is reachable."""
    try:
        import urllib.request as _u
        with _u.urlopen(host + "/api/tags", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def ollama_models(host: str = "http://127.0.0.1:11434") -> List[str]:
    """Return list of installed model names. Empty list on failure."""
    try:
        import urllib.request as _u
        with _u.urlopen(host + "/api/tags", timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except Exception:
        return []


def ollama_install_url() -> str:
    return "https://ollama.com/download"


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

        # Step-by-step state
        self._steps = ["welcome", "disk", "ollama", "model", "ready"]
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
            "Quick walkthrough — about three minutes. We'll:\n"
            "   • Confirm there's enough disk space for an AI model\n"
            "   • Detect or help install Ollama (the local AI engine)\n"
            "   • Pull a usable model so the panel can answer\n\n"
            "Everything runs locally; nothing leaves this machine.\n\n"
            "Skip if you'd rather configure things by hand."
        )

    # Step 2: disk space warning
    def _render_disk(self):
        self._heading("Disk space")
        # Try to detect free space
        try:
            import shutil as _sh
            free_bytes = _sh.disk_usage(str(self.vault_dir)).free
            free_gb = free_bytes / (1024**3)
            free_str = f"{free_gb:.1f} GB free"
        except Exception:
            free_gb = None
            free_str = "(could not detect)"

        self._label(
            f"AI models are large. The recommended starter model is "
            f"{DEFAULT_MODEL_GB} GB.\n\n"
            f"Detected free space on this drive: {free_str}",
            pady=(0, 12),
        )
        if free_gb is not None and free_gb < DEFAULT_MODEL_GB + 5:
            self._label(
                "⚠ Less than 14 GB free — you may want to clear some space, "
                "or pick the smaller fallback model on the next step.",
                fg=self._theme()["warning"],
            )
        else:
            self._label("✓ You have enough space for the default model.",
                        fg=self._theme()["success"])

    # Step 3: Ollama detection
    def _render_ollama(self):
        self._heading("AI engine — Ollama")

        self._ollama_status_var = tk.StringVar(value="Checking…")
        self._label("", pady=2)  # spacer
        tk.Label(self.body, textvariable=self._ollama_status_var,
                 bg=self._theme()["bg"], fg=self._theme()["fg"],
                 font=("Segoe UI", 11), wraplength=580, justify="left", anchor="w"
                 ).pack(fill="x", pady=4)

        instr = tk.Frame(self.body, bg=self._theme()["bg"])
        instr.pack(fill="x", pady=12)

        ttk.Button(instr, text="🔄 Re-check",
                   command=self._refresh_ollama).pack(side="left")
        ttk.Button(instr, text="🌐 Open Ollama download page",
                   command=lambda: self._open_url(ollama_install_url())
                   ).pack(side="left", padx=8)

        self._refresh_ollama()

    def _refresh_ollama(self):
        ok = ollama_available()
        if ok:
            self._ollama_status_var.set("✓ Ollama is running on this machine.")
            self._ollama_ok = True
        else:
            self._ollama_status_var.set(
                "✗ Ollama not detected at 127.0.0.1:11434.\n\n"
                "1) Download and install Ollama from ollama.com\n"
                "2) Launch it (it runs in the background)\n"
                "3) Click Re-check above"
            )
            self._ollama_ok = False

    # Step 4: Model
    def _render_model(self):
        self._heading("Choose a model")

        if not ollama_available():
            self._label("Ollama isn't running yet — go back one step to install it first.",
                        fg=self._theme()["warning"])
            return

        installed = ollama_models()
        if installed:
            self._label("Models already installed on your machine:")
            for m in installed[:8]:
                self._label(f"   ✓ {m}", font=("Consolas", 10), pady=1,
                            fg=self._theme()["success"])
            self._label(
                "\nYou're good to go. You can pull additional models later "
                "from the Apothecary tab.",
                pady=(12, 0),
            )
            return

        self._label(
            "No models installed yet. Pick one to download now:",
            pady=(0, 10),
        )

        choice_frame = tk.Frame(self.body, bg=self._theme()["bg"])
        choice_frame.pack(fill="x")
        self._model_choice = tk.StringVar(value=DEFAULT_MODEL)

        for name, gb, desc in [
            (DEFAULT_MODEL,     DEFAULT_MODEL_GB, "Recommended  •  Balanced quality and speed"),
            (DEFAULT_MODEL_ALT, ALT_MODEL_GB,     "Lightweight  •  For modest hardware (8 GB RAM)"),
        ]:
            f = tk.Frame(choice_frame, bg=self._theme()["bg"])
            f.pack(fill="x", pady=4)
            tk.Radiobutton(f, text=f"  {name}  ({gb} GB)",
                           variable=self._model_choice, value=name,
                           bg=self._theme()["bg"], fg=self._theme()["fg"],
                           selectcolor=self._theme()["panel_bg"],
                           activebackground=self._theme()["bg"],
                           activeforeground=self._theme()["fg"],
                           font=("Segoe UI", 11)
                           ).pack(side="left")
            tk.Label(f, text=desc, font=("Segoe UI", 9),
                     bg=self._theme()["bg"], fg=self._theme()["muted_fg"]
                     ).pack(side="left", padx=8)

        self._pull_status_var = tk.StringVar(value="")
        tk.Label(self.body, textvariable=self._pull_status_var,
                 bg=self._theme()["bg"], fg=self._theme()["info"],
                 font=("Consolas", 9), wraplength=580, justify="left", anchor="w"
                 ).pack(fill="x", pady=(16, 4))

        self._pull_btn = ttk.Button(self.body, text="⬇  Download selected model",
                                     command=self._pull_model)
        self._pull_btn.pack(pady=8)

    def _pull_model(self):
        model = self._model_choice.get()
        self._pull_btn.configure(state="disabled", text="Downloading…")
        self._pull_status_var.set(f"Pulling {model}. This is a one-time download "
                                  f"(several GB). The window will stay responsive.")

        def worker():
            try:
                proc = subprocess.Popen(
                    ["ollama", "pull", model],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1, shell=False,
                )
                last_line = ""
                for line in proc.stdout:
                    last_line = line.strip()
                    if last_line:
                        self.after(0, lambda s=last_line: self._pull_status_var.set(s))
                proc.wait()
                if proc.returncode == 0:
                    self.after(0, lambda: self._pull_status_var.set(
                        f"✓ {model} downloaded successfully."))
                else:
                    self.after(0, lambda: self._pull_status_var.set(
                        f"✗ Pull exited with code {proc.returncode}: {last_line}"))
            except FileNotFoundError:
                self.after(0, lambda: self._pull_status_var.set(
                    "✗ `ollama` command not found on PATH. Install Ollama first."))
            except Exception as e:
                self.after(0, lambda: self._pull_status_var.set(f"✗ {e}"))
            finally:
                self.after(0, lambda: self._pull_btn.configure(
                    state="normal", text="⬇  Download selected model"))

        threading.Thread(target=worker, daemon=True).start()

    # Step 5: ready
    def _render_ready(self):
        self._heading("Ready.")
        self._label(
            "Three ways to start poking at it:\n\n"
            "   1. Drop a CSV onto the Grapher tab — the Analyst suggests a "
            "chart and tells you what it sees.\n"
            "   2. Click 📦 Sample on the Grapher for some bundled fake data "
            "if you don't have a file handy.\n"
            "   3. Council tab → ask a question in plain English. The panel "
            "deliberates and gives you an answer.\n\n"
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
            "Skipping means you'll need to configure Ollama and a model "
            "manually before deliberations work. Continue?",
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
