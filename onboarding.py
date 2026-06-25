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
from pathlib import Path
from typing import Callable, List, Optional

# Tk is optional at IMPORT time: the pure helpers in this module
# (gguf_file_status, load/save_gguf_path, load/save_clip_path,
# needs_onboarding) are used by the engine, the setup wizard, and CI
# smoke tests on headless Linux boxes where python3-tk isn't installed.
# Only the OnboardingWizard UI actually needs Tk — it raises a clear
# error if constructed without it.
try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
    _TK_OK = True
except Exception:                       # pragma: no cover — headless box
    tk = None          # type: ignore[assignment]
    ttk = messagebox = filedialog = None  # type: ignore[assignment]
    _TK_OK = False

# Base class for the wizard. With `from __future__ import annotations`
# every tk.* type hint in this module is a string (never evaluated), so
# the class statement below is the only module-level Tk dependency.
_TkBase = tk.Toplevel if _TK_OK else object

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
        """Convert a model_catalog.ModelSpec into the dict shape the
        wizard UI expects. Emits the ``vendor`` + ``vram`` fields that
        the in-repo ship-readiness pass added so any UI consumer of
        those keys keeps working."""
        return {
            "name":   spec.name,
            "vendor": spec.org,
            "size":   f"~{spec.size_gb:.1f} GB",
            "vram":   int(round(spec.vram_gb_q4)),
            "ctx":    f"{spec.context_k}K context",
            "url":    f"https://huggingface.co/{spec.hf_repo}",
            "blurb":  f"{spec.org}. {spec.blurb}",
            "spec":   spec,
        }

    RECOMMENDED_MODELS = [_spec_to_legacy(s) for s in _mc.MODELS]
    DEFAULT_MODEL_ID   = _mc.DEFAULT_MODEL_ID
except Exception as _exc:
    # Hard fallback so the wizard still runs if model_catalog ever errors.
    RECOMMENDED_MODELS = [
        {
            "name":   "IBM Granite 3.1 8B Instruct (Q4_K_M)",
            "vendor": "IBM",
            "size":   "~5 GB",
            "vram":   8,
            "ctx":    "128K context",
            "url":    "https://huggingface.co/ibm-granite/granite-3.1-8b-instruct-GGUF",
            "blurb":  "IBM. Solid baseline; conservative refusal behaviour.",
        },
    ]
    DEFAULT_MODEL_ID = "granite-3.1-8b-q4"


# ============================================================
# Vision-capable models — same US-only policy, requires a second
# .gguf file (the multimodal projector / "mmproj") alongside the
# main weights. The user points COUNCIL_GGUF_PATH at the LLM and
# COUNCIL_GGUF_CLIP_PATH at the mmproj. Curated separately because
# vision specs carry an extra field (mmproj) that model_catalog
# doesn't track yet.
# ============================================================

RECOMMENDED_VISION_MODELS = [
    {
        "name":    "Llama 3.2 11B Vision Instruct (Q4_K_M)",
        "vendor":  "Meta",
        "size":    "~8 GB + ~1 GB mmproj",
        "vram":    12,
        "url":     "https://huggingface.co/bartowski/Llama-3.2-11B-Vision-Instruct-GGUF",
        "mmproj":  "Download the mmproj-llama-3.2-11b-vision-f16.gguf file"
                   " from the same repo and set COUNCIL_GGUF_CLIP_PATH",
        "blurb":   "Meta. Strongest US-made vision model that fits on a "
                   "16 GB GPU. Lets the Council reason about images, "
                   "charts, scanned documents.",
    },
    {
        "name":    "Phi-4 Multimodal (Q4_K_M)",
        "vendor":  "Microsoft",
        "size":    "~9 GB + projector",
        "vram":    14,
        "url":     "https://huggingface.co/microsoft/phi-4-multimodal-instruct",
        "mmproj":  "Microsoft ships the projector alongside the weights",
        "blurb":   "Microsoft. Vision + speech-aware. Use when you want "
                   "audio file analysis as well as images.",
    },
    {
        "name":    "Gemma 3 12B Instruct (Q4_K_M)",
        "vendor":  "Google",
        "size":    "~7 GB + ~0.5 GB mmproj",
        "vram":    10,
        "url":     "https://huggingface.co/bartowski/gemma-3-12b-it-GGUF",
        "mmproj":  "Download the mmproj-gemma-3-12b-it-f16.gguf file",
        "blurb":   "Google. Compact vision model. Solid alternative to "
                   "Llama 3.2 Vision on cards with 10-12 GB VRAM.",
    },
]


# ============================================================
# Persistence — shared with the in-app Browse button on the
# Council tab. Keep the filename and JSON key in sync with
# council_gui_engine._backend_settings_path / _save_backend_settings.
# ============================================================

_BACKEND_SETTINGS_FILENAME = "backend_settings.json"


def _backend_settings_path(vault_dir: Path) -> Path:
    return vault_dir / _BACKEND_SETTINGS_FILENAME


def _load_backend_settings(vault_dir: Path) -> dict:
    """Read the entire backend settings dict, or return {} on any failure.
    Internal helper — callers use the typed accessors below."""
    p = _backend_settings_path(vault_dir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _merge_backend_settings(vault_dir: Path, **updates: str) -> None:
    """Merge `updates` into the existing backend settings JSON without
    blowing away other keys. Atomic-style: read, mutate, write.

    Previously save_gguf_path() overwrote the entire file with
    `{"gguf_path": ...}` — that destroyed any sibling key like
    `clip_path`. The merge keeps every key the wizard / engine may have
    written and only touches the ones the caller is updating.
    """
    data = _load_backend_settings(vault_dir)
    for k, v in updates.items():
        if v is None or v == "":
            # Setting to empty/None means CLEAR the key entirely.
            data.pop(k, None)
        else:
            data[k] = v
    try:
        _backend_settings_path(vault_dir).write_text(
            json.dumps(data, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def load_gguf_path(vault_dir: Path) -> str:
    """Return the persisted GGUF model path, or empty string if none.

    Reads COUNCIL_GGUF_PATH from the environment first (a launch-time
    override always wins), then falls back to vault/backend_settings.json.
    """
    env = _os.environ.get("COUNCIL_GGUF_PATH", "").strip()
    if env:
        return env
    return str(_load_backend_settings(vault_dir).get("gguf_path", "")).strip()


def save_gguf_path(vault_dir: Path, path: str) -> None:
    """Persist the GGUF model path so the next launch picks it up.

    Also sets COUNCIL_GGUF_PATH in os.environ for the current process so
    the wizard's choice is live immediately — no app restart needed.
    Preserves any sibling keys (e.g. clip_path) already in the file.
    """
    _merge_backend_settings(vault_dir, gguf_path=path)
    if path:
        _os.environ["COUNCIL_GGUF_PATH"] = path
    else:
        _os.environ.pop("COUNCIL_GGUF_PATH", None)


def load_clip_path(vault_dir: Path) -> str:
    """Return the persisted vision mmproj (CLIP) path, or empty string.

    Same precedence as load_gguf_path: env var COUNCIL_GGUF_CLIP_PATH
    wins, then the persisted backend_settings.json key. The engine's
    _get_gguf_model checks the env var directly, so the wizard
    populates it on save for live effect within the same process.
    """
    env = _os.environ.get("COUNCIL_GGUF_CLIP_PATH", "").strip()
    if env:
        return env
    return str(_load_backend_settings(vault_dir).get("clip_path", "")).strip()


def save_clip_path(vault_dir: Path, path: str) -> None:
    """Persist the vision mmproj path. Empty string clears it (text-only
    mode after a user had picked vision in a previous run).
    """
    _merge_backend_settings(vault_dir, clip_path=path)
    if path:
        _os.environ["COUNCIL_GGUF_CLIP_PATH"] = path
    else:
        _os.environ.pop("COUNCIL_GGUF_CLIP_PATH", None)


def clip_file_status(path: str) -> tuple:
    """Inspect a candidate mmproj .gguf path. Same shape as
    gguf_file_status: (ok: bool, message: str). The validation is the
    SAME — an mmproj IS a GGUF file (just a smaller one with vision
    encoder weights rather than language-model weights) — so the
    magic-byte check applies unchanged. We keep a separate name for
    documentation clarity at call sites in the wizard.
    """
    return gguf_file_status(path)


def gguf_file_status(path: str) -> tuple:
    """Inspect a candidate .gguf path. Returns (ok: bool, message: str).

    Beyond the extension check, we read the first 4 bytes and confirm
    they match the GGUF magic. This catches the most common "I downloaded
    it but it doesn't work" failure mode: a huggingface-cli download
    that was interrupted mid-stream and left a partial file (or an HTML
    error page) on disk with the .gguf extension. Without the magic
    check the user clicks Finish, the app tries to load the file, and
    llama-cpp crashes deep in C with no actionable error.
    """
    if not path:
        return False, "(no model selected)"
    p = Path(path)
    if not p.exists():
        return False, f"✗ File does not exist: {p}"
    if not p.is_file():
        return False, f"✗ Not a file: {p}"
    if p.suffix.lower() != ".gguf":
        return False, f"⚠ Not a .gguf extension: {p.name}"
    # Magic-byte check
    try:
        if p.stat().st_size < 1024:
            return False, (f"⚠ {p.name} is suspiciously small "
                            f"({p.stat().st_size} bytes) — likely a failed "
                            "download. Re-fetch the file.")
        with open(p, "rb") as fh:
            magic = fh.read(4)
        if magic != b"GGUF":
            return False, (f"⚠ {p.name} doesn't start with the GGUF magic "
                            f"bytes (got {magic!r}). Likely a corrupted or "
                            "partial download — re-fetch the file.")
    except Exception as exc:
        return False, f"⚠ Could not read {p.name}: {exc!r}"
    try:
        size_gb = p.stat().st_size / (1024 ** 3)
    except Exception:
        return True, f"✓ {p.name}"
    return True, f"✓ {p.name} ({size_gb:.1f} GB)"


# ============================================================
# Wizard
# ============================================================

class OnboardingWizard(_TkBase):
    """
    Modal wizard shown on first launch. Returns when the user finishes
    or cancels. The host application should check `wizard.completed`
    after the wizard closes.
    """

    def __init__(self, parent: tk.Tk, vault_dir: Path):
        if not _TK_OK:
            raise RuntimeError(
                "OnboardingWizard requires tkinter, which is not "
                "available (headless box / python3-tk not installed). "
                "The file helpers in this module work without it.")
        super().__init__(parent)
        self.parent     = parent
        self.vault_dir  = vault_dir
        self.completed  = False
        self.skipped    = False

        # Cached probe results — populated lazily in the hardware /
        # previous-install steps. Stash on self so the model + plan
        # steps can read them without re-probing.
        self._hw_info: Optional[dict] = None
        self._prev_info: Optional[dict] = None

        self.title(f"Welcome to {branding.PRODUCT_NAME}")
        try:
            branding.apply_window_icon(self)
        except Exception:
            pass
        self.geometry("680x600")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        # Step list. New hardware + previous-install + plan steps land
        # between welcome and the disk-space check so the wizard can:
        #   1. Probe the user's CPU/RAM/GPU once.
        #   2. Look for an existing conda env / vault / GGUF on disk.
        #   3. Show a recommended install plan with one-click reuse.
        # The model + ready steps then preselect the recommendation.
        self._steps = ["welcome", "hardware", "previous_install",
                       "plan", "disk", "model", "ready"]
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
            "   • Detect your hardware and recommend a model size\n"
            "   • Look for a previous install we can reuse\n"
            "   • Confirm there's enough disk space\n"
            "   • Point the app at a GGUF model file\n\n"
            "All recommended models are from US-based providers "
            "(Meta, Microsoft, IBM, Google). Everything runs locally;\n"
            "nothing leaves this machine.\n\n"
            "Skip if you'd rather configure things by hand "
            f"(set COUNCIL_GGUF_PATH=<path-to-.gguf> in your environment)."
        )

    # Step 2: hardware scan — runs the detector, summarises CPU/RAM/GPU.
    def _render_hardware(self):
        self._heading("Hardware scan")
        if self._hw_info is None:
            self._label("Probing your machine…", pady=(0, 8))
            self.update_idletasks()
            try:
                import hardware_detect as _hwd
                self._hw_info = _hwd.detect()
            except Exception as exc:
                self._hw_info = {"error": repr(exc)}
        info = self._hw_info or {}
        if "error" in info:
            self._label("Hardware detection failed: " + str(info["error"]),
                         fg=self._theme().get("warning"))
            self._label(
                "The wizard can still proceed; you'll just have to pick a "
                "model size yourself in the next step."
            )
            return

        # Pretty-print the key fields.
        def _fmt(label, val):
            return f"   • {label:<14} {val}"

        gpu_line = info.get("gpu_name") or "(none — CPU-only inference)"
        vram = info.get("vram_gb")
        if vram:
            gpu_line += f"   ·  {vram:.1f} GB VRAM"
        ram = info.get("ram_gb")
        ram_line = f"{ram:.1f} GB" if ram else "(unknown)"

        avx_warn = ""
        if info.get("has_avx2") is False and info.get("os") in ("linux", "wsl"):
            avx_warn = ("\n⚠ This CPU lacks AVX2 — prebuilt llama-cpp wheels "
                        "will crash. See installs.txt 'Illegal instruction'.")

        self._label(
            _fmt("OS:", f"{info.get('os','?')} — {info.get('os_version','?')}") + "\n"
            + _fmt("CPU:", str(info.get("cpu_brand") or "?")) + "\n"
            + _fmt("Cores:", str(info.get("cpu_cores") or "?")) + "\n"
            + _fmt("RAM:", ram_line) + "\n"
            + _fmt("GPU:", gpu_line) + "\n"
            + _fmt("CUDA max:", str(info.get("cuda_max") or "—"))
            + avx_warn,
            font=("Consolas", 10),
        )

    # Step 3: previous-install detection — looks for reusable artifacts.
    def _render_previous_install(self):
        self._heading("Previous install check")
        if self._prev_info is None:
            self._label("Scanning for a previous install…", pady=(0, 8))
            self.update_idletasks()
            try:
                import previous_install_detect as _pid
                app_dir = Path(__file__).resolve().parent
                self._prev_info = _pid.detect(app_dir, self.vault_dir)
            except Exception as exc:
                self._prev_info = {"error": repr(exc)}
        info = self._prev_info or {}
        if "error" in info:
            self._label("Detection failed: " + str(info["error"]),
                         fg=self._theme().get("warning"))
            return

        # Conda env
        env = info.get("conda_env", {}) or {}
        if env.get("present"):
            self._label(
                f"   ✓ conda env 'council' found at\n     {env.get('path') or '(unknown path)'}\n"
                f"     (managed by {env.get('tool') or 'unknown tool'})",
                fg=self._theme().get("success"),
            )
        else:
            self._label(
                "   ○ No 'council' conda env found.\n"
                "     setup-wsl.sh or installs.txt will create one for you.",
            )

        # Vault
        vlt = info.get("vault", {}) or {}
        if vlt.get("present"):
            files = vlt.get("data_in_files", 0)
            settings = vlt.get("has_settings")
            tag = "+ settings" if settings else ""
            self._label(
                f"   ✓ Vault exists at {vlt.get('path')}\n"
                f"     {files} file(s) in data_in/ {tag}",
                fg=self._theme().get("success") if files else None,
            )
        else:
            self._label("   ○ No existing vault — one will be created.")

        # GGUF models
        models = info.get("gguf_models") or []
        if models:
            valid = [m for m in models if m.get("valid")]
            self._label(
                f"   ✓ Found {len(models)} .gguf file(s) "
                f"({len(valid)} valid):",
                fg=self._theme().get("success") if valid else
                    self._theme().get("warning"),
            )
            for m in models[:5]:
                ok = "✓" if m.get("valid") else "⚠"
                self._label(
                    f"        {ok} {m['name']}  ({m.get('size_gb','?')} GB)",
                    font=("Consolas", 9), pady=2,
                )
            if len(models) > 5:
                self._label(f"        ... ({len(models)-5} more)",
                             font=("Consolas", 9), pady=2)
        else:
            self._label(
                "   ○ No GGUF models found in ~/models, ~/Downloads, or "
                "~/.cache/huggingface.\n     You'll pick one in the next step."
            )

        prev = info.get("previous_model")
        if prev:
            self._label(
                f"\n   ↻ Previous session used: {prev}\n"
                "   We'll preselect this on the model step.",
                fg=self._theme().get("success"),
            )

    # Step 4: install plan — show hardware-aware recommendation.
    def _render_plan(self):
        self._heading("Recommended setup")
        info = self._hw_info or {}
        rec = info.get("recommended", {}) or {}
        cuda_tier = rec.get("cuda_tier", "cpu")
        model_pick = rec.get("model_pick", "(no recommendation)")
        n_ctx_max = rec.get("n_ctx_max", 4096)

        gpu_name = info.get("gpu_name") or "no GPU"
        vram = info.get("vram_gb")
        vram_str = f"{vram:.1f} GB VRAM" if vram else "CPU only"

        if cuda_tier == "cpu":
            install_blurb = (
                "   • Install path: SECTION A in installs.txt (CPU only)\n"
                "   • Or on WSL: ./setup-wsl.sh — it auto-detects no GPU\n"
                "     and installs the CPU torch wheel."
            )
        else:
            install_blurb = (
                f"   • Install path: SECTION {'D' if cuda_tier=='cu128' else 'C' if cuda_tier=='cu124' else 'B'} "
                f"in installs.txt — CUDA wheel tier: {cuda_tier}\n"
                f"   • Or on WSL: ./setup-wsl.sh — it auto-detects your GPU\n"
                f"     and picks the same wheel tier."
            )

        self._label(
            f"For {gpu_name} ({vram_str}) we recommend:\n\n"
            f"   • Model:       {model_pick}\n"
            f"   • Max n_ctx:   {n_ctx_max:,} tokens\n"
            f"   • CUDA tier:   {cuda_tier}\n\n"
            f"{install_blurb}\n\n"
            "You can still pick any model in the next step — this is\n"
            "just the recommendation based on what fits comfortably.",
            font=("Segoe UI", 11),
        )

        # If a previous model on disk is valid, offer one-click reuse.
        prev = (self._prev_info or {}).get("previous_model")
        if prev and Path(prev).exists():
            tk.Label(
                self.body,
                text=f"\n↻ Reuse previous model: {Path(prev).name}",
                bg=self._theme()["bg"], fg=self._theme().get("success"),
                font=("Segoe UI", 10, "italic"), wraplength=580, justify="left",
                anchor="w",
            ).pack(fill="x", pady=(8, 0))

    # Step 5: disk space warning
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
            "All recommended models are from US-based providers (Meta, "
            "Microsoft, IBM, Google). The app loads them via .gguf files "
            "(the standard local-LLM format). You can:\n"
            "   • Pick a .gguf file already on this machine\n"
            "   • Download a recommended one from Hugging Face\n",
            pady=(0, 12),
        )

        # Preselect — order of preference:
        #   1. Whatever the user has already configured (env var or
        #      backend_settings.json).
        #   2. The previous-install detector's "last used" path, if it
        #      still exists.
        #   3. The first GGUF file the detector found in standard
        #      locations, if it's valid.
        # Otherwise the field stays empty and the user picks via Browse.
        current = load_gguf_path(self.vault_dir)
        if not current:
            prev = (self._prev_info or {}).get("previous_model")
            if prev and Path(prev).exists():
                current = prev
        if not current:
            for m in (self._prev_info or {}).get("gguf_models", []):
                if m.get("valid"):
                    current = m["path"]
                    break

        self._gguf_path_var = tk.StringVar(value=current)
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

        # Recommended models list — toggle between text-only and
        # vision-capable. Vision adds image understanding (the user
        # can drop screenshots, scans, charts into the vault and the
        # model can read them) but requires a SECOND .gguf file (the
        # multimodal projector — mmproj) and twice the disk/VRAM.
        sep = tk.Frame(self.body, bg=self._theme()["muted_fg"], height=1)
        sep.pack(fill="x", pady=12)

        # Text-only vs vision toggle. We preselect based on whatever
        # we last persisted — a user who picked vision and downloaded
        # an mmproj last time gets the toggle ON automatically on
        # re-run instead of having to opt in again.
        toggle_row = tk.Frame(self.body, bg=self._theme()["bg"])
        toggle_row.pack(fill="x", pady=(0, 6))
        if not hasattr(self, "_vision_mode_var"):
            init = bool(load_clip_path(self.vault_dir))
            self._vision_mode_var = tk.BooleanVar(value=init)
        tk.Label(toggle_row, text="Show vision-capable models",
                 font=("Segoe UI", 10),
                 bg=self._theme()["bg"], fg=self._theme()["fg"]
                 ).pack(side="left", padx=(0, 8))
        ttk.Checkbutton(
            toggle_row, variable=self._vision_mode_var,
            command=self._render_step,   # re-render to swap the list
        ).pack(side="left")
        # Recommendation badge based on detected VRAM
        vram = ((self._hw_info or {}).get("vram_gb") or 0)
        if vram >= 12:
            badge = "✓ this machine can run vision models"
            badge_fg = self._theme().get("success")
        elif vram >= 8:
            badge = "○ vision works on smaller cards but trims n_ctx"
            badge_fg = self._theme().get("muted_fg")
        else:
            badge = "⚠ vision models need ~10 GB VRAM; you have less"
            badge_fg = self._theme().get("warning")
        tk.Label(toggle_row, text=badge, font=("Segoe UI", 9),
                 bg=self._theme()["bg"], fg=badge_fg
                 ).pack(side="left", padx=(12, 0))

        showing_vision = bool(self._vision_mode_var.get())
        models = RECOMMENDED_VISION_MODELS if showing_vision else RECOMMENDED_MODELS

        if showing_vision:
            self._label(
                "Vision models — drop image / scan / chart files into "
                "the vault and the model can read them. Each row "
                "links to a HF page where you'll download BOTH the "
                "main weights AND the mmproj file. Use the two pickers "
                "below (one for the main .gguf, one for the mmproj).",
                font=("Segoe UI", 10), fg=self._theme()["muted_fg"],
                pady=(0, 8))

            # Second file picker — for the multimodal projector. Lives
            # right below the model pickers so the user sees both
            # paths together and can browse both without leaving the
            # wizard. Hidden in text-only mode.
            if not hasattr(self, "_clip_path_var"):
                self._clip_path_var   = tk.StringVar(value=load_clip_path(self.vault_dir))
                self._clip_status_var = tk.StringVar(value="")
            self._refresh_clip_status()

            clip_frame = tk.Frame(self.body, bg=self._theme()["bg"])
            clip_frame.pack(fill="x", pady=(0, 4))
            tk.Label(clip_frame, text="mmproj file:",
                     font=("Segoe UI", 10),
                     bg=self._theme()["bg"], fg=self._theme()["muted_fg"]
                     ).pack(side="left", padx=(0, 6))
            tk.Label(clip_frame, textvariable=self._clip_status_var,
                     font=("Consolas", 10),
                     bg=self._theme()["bg"], fg=self._theme()["fg"]
                     ).pack(side="left", fill="x", expand=True)

            clip_btn_frame = tk.Frame(self.body, bg=self._theme()["bg"])
            clip_btn_frame.pack(fill="x", pady=(2, 8))
            ttk.Button(clip_btn_frame,
                        text="📁 Browse for mmproj .gguf…",
                        command=self._browse_clip).pack(side="left")
            ttk.Button(clip_btn_frame, text="🗑 Clear (text-only)",
                        command=self._clear_clip).pack(side="left", padx=8)
        else:
            # User turned the vision toggle OFF — clear any previously
            # persisted clip path so the next launch doesn't try to
            # load vision against a non-vision GGUF.
            try:
                if load_clip_path(self.vault_dir):
                    save_clip_path(self.vault_dir, "")
            except Exception:
                pass
            self._label(
                "Recommended models — click to open the Hugging Face "
                "download page in your browser. After downloading, come "
                "back and use Browse… to select the .gguf file.",
                font=("Segoe UI", 10), fg=self._theme()["muted_fg"],
                pady=(0, 8))

        # Scrollable container so the catalog grows beyond the wizard's
        # fixed height without spilling off-screen — matters for the
        # 9-row text catalog (and any growth in the vision list).
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
        def _on_mwheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mwheel)

        for m in models:
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
            tk.Label(row,
                     text=(f"  ·  {m.get('vendor','?')}  ·  {m['size']}"
                           + (f"  ·  {m.get('ctx','')}" if m.get('ctx') else "")),
                     font=("Segoe UI", 9),
                     bg=self._theme()["bg"], fg=self._theme()["muted_fg"]
                     ).pack(side="left")
            # Right-aligned buttons: Open page, Auto-download (catalog-only)
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
                # Bind exc as a default arg: self.after defers _err to the Tk
                # loop AFTER this except block exits, at which point Python
                # has deleted `exc` — referencing it then is a NameError.
                def _err(exc=exc):
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

    def _browse_clip(self):
        """File picker for the multimodal projector (mmproj) .gguf. Same
        pattern as _browse_gguf — pick file, validate magic, persist
        the path through save_clip_path so the engine picks it up on
        next inference call without needing an env-var export."""
        start_dir = ""
        cur = self._clip_path_var.get().strip()
        if cur:
            try:
                start_dir = str(Path(cur).parent)
            except Exception:
                start_dir = ""
        # Fall back to the main GGUF's folder — mmproj files almost
        # always live alongside the weights they project for.
        if not start_dir:
            gguf_cur = self._gguf_path_var.get().strip()
            if gguf_cur:
                try:
                    start_dir = str(Path(gguf_cur).parent)
                except Exception:
                    start_dir = ""
        path = filedialog.askopenfilename(
            parent=self,
            title="Select mmproj (.gguf) file for vision",
            initialdir=start_dir or None,
            filetypes=[("GGUF projector files", "*.gguf"),
                        ("All files", "*.*")],
        )
        if not path:
            return
        self._clip_path_var.set(path)
        save_clip_path(self.vault_dir, path)
        try:
            import council_engine as _ce
            _ce.refresh_backend_config()
        except Exception:
            pass
        self._refresh_clip_status()

    def _clear_clip(self):
        """User decided to go back to text-only — wipe the persisted
        clip path AND the env var so the next model load skips the
        Llava chat-handler path entirely."""
        self._clip_path_var.set("")
        save_clip_path(self.vault_dir, "")
        try:
            import council_engine as _ce
            _ce.refresh_backend_config()
        except Exception:
            pass
        self._refresh_clip_status()

    def _refresh_clip_status(self):
        if not hasattr(self, "_clip_path_var"):
            return
        path = self._clip_path_var.get().strip()
        if not path:
            self._clip_status_var.set("(none — text-only)")
            self._clip_ok = True   # empty is a valid choice
            return
        ok, msg = clip_file_status(path)
        if hasattr(self, "_clip_status_var"):
            self._clip_status_var.set(msg)
        self._clip_ok = ok

    # Step 4: ready
    def _render_ready(self):
        self._heading("Ready.")
        # Show what we settled on so the user knows the chosen model
        # name without having to scroll back.
        path = load_gguf_path(self.vault_dir)
        ok, msg = gguf_file_status(path)
        if ok:
            self._label(f"GGUF model:  {msg}", font=("Consolas", 10),
                        fg=self._theme()["success"], pady=(0, 6))
        else:
            self._label(
                "No GGUF model selected. The app will start, but you'll "
                "need to set COUNCIL_GGUF_PATH or use the Browse… button "
                "on the Council tab before any query can be answered.",
                fg=self._theme()["warning"], pady=(0, 6),
            )
        # Show the mmproj path too when the user opted into vision —
        # full disclosure of both files we'll load on next launch so
        # nothing about the configuration is hidden.
        clip = load_clip_path(self.vault_dir)
        if clip:
            cok, cmsg = clip_file_status(clip)
            if cok:
                self._label(f"mmproj:      {cmsg}  (vision enabled)",
                            font=("Consolas", 10),
                            fg=self._theme()["success"], pady=(0, 12))
            else:
                self._label(
                    f"mmproj:      {cmsg}",
                    font=("Consolas", 10),
                    fg=self._theme()["warning"], pady=(0, 12),
                )
        else:
            self._label("Vision:      off (text-only mode)",
                        font=("Consolas", 10),
                        fg=self._theme()["muted_fg"], pady=(0, 12))

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

    if not _TK_OK:
        # Headless box — can't show a wizard; treat as not completed so
        # the caller can fall back to env-var / CLI configuration.
        if on_complete:
            on_complete(False)
        return

    wiz = OnboardingWizard(parent, vault_dir)
    parent.wait_window(wiz)
    if on_complete:
        on_complete(wiz.completed)
