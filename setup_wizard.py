"""
setup_wizard.py — first-run Council setup wizard for terminal users.

Designed for accessibility:
  • zero clicks past Enter — every prompt has a sensible default
  • non-intimidating language; explains what each step does and why
  • idempotent — re-running skips work already done
  • safe to abort at any step (Ctrl+C) — nothing destructive runs
    before the user types 'y'
  • runs on plain Windows cmd / PowerShell / bash / WSL identically

What it does, in order:

  1. Welcome + scope (what gets created, where, how much disk)
  2. Detects Python, OS, GPU (optional CUDA tier guess)
  3. Creates ./.venv if missing (uses the Python you ran the wizard with)
  4. Installs torch + llama-cpp-python with the right CUDA wheel
     (or asks if it's already installed)
  5. Installs the rest of the Council deps
  6. Shows the curated US-origin model catalog; user picks one
     (or skips — they can come back later with `python setup_wizard.py`)
  7. Downloads the chosen GGUF via huggingface_hub
  8. Optional: locates Tesseract for image OCR (entirely optional —
     image filenames are searchable even without it)
  9. Writes vault/backend_settings.json so the GUI knows which model
     to load on launch
 10. Offers to launch the app

Invocation:
    python setup_wizard.py                  # interactive
    python setup_wizard.py --auto           # accept all defaults, no prompts
    python setup_wizard.py --model phi-4-q4 # preselect a model id
    python setup_wizard.py --check-only     # diagnostic mode, no install

Everything the wizard touches is local. No telemetry. No phone-home.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple


# Lazy-imported so we can still run --check-only on a bare Python install:
#   - huggingface_hub (model download)
#   - model_catalog   (curated list)


# ============================================================
# Console encoding shim
#
# Windows still defaults the console to cp1252, which blows up on the
# box-drawing characters and checkmarks we use for the section dividers.
# Switching to UTF-8 here keeps the cosmetic step out of every caller's
# environment. Falls back gracefully on terminals that don't support
# reconfigure (Python < 3.7 or stdout redirected to a non-TTY).
# ============================================================
try:
    if sys.stdout.encoding and "utf" not in sys.stdout.encoding.lower():
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ============================================================
# Cosmetics
# ============================================================

# ANSI works in Windows 10 1607+ default terminals, modern PowerShell,
# Windows Terminal, all Linux/macOS shells. We don't bail if it doesn't
# render — the labels still read fine plain.
_ANSI = sys.stdout.isatty() and os.environ.get("NO_COLOR") not in ("1", "true")

def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _ANSI else s

def good(s: str) -> str:    return _c("32", s)   # green
def warn(s: str) -> str:    return _c("33", s)   # yellow
def bad(s: str) -> str:     return _c("31", s)   # red
def dim(s: str) -> str:     return _c("90", s)   # grey
def bold(s: str) -> str:    return _c("1",  s)
def header(s: str) -> str:  return _c("36;1", s) # cyan bold


def hr() -> None:
    print(dim("─" * 78))


def banner(step: int, total: int, title: str) -> None:
    print()
    hr()
    print(header(f"  Step {step}/{total}  {title}"))
    hr()


def ask(prompt: str, default: str = "") -> str:
    """Prompt the user with a default in brackets. Returns stripped input,
    or the default when the user just presses Enter."""
    suffix = f" [{default}]" if default else ""
    try:
        ans = input(f"  {prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  cancelled.")
        sys.exit(1)
    return ans or default


def confirm(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        a = ask(f"{prompt} ({d})", "")
        if not a:
            return default
        if a.lower() in ("y", "yes"):
            return True
        if a.lower() in ("n", "no"):
            return False
        print(warn("  please answer y or n"))


# ============================================================
# Detection helpers
# ============================================================

def detect_os() -> str:
    """'windows' | 'linux' | 'wsl' | 'darwin'."""
    sys_plat = sys.platform
    if sys_plat == "win32":
        return "windows"
    if sys_plat == "darwin":
        return "darwin"
    try:
        if "microsoft" in Path("/proc/version").read_text(errors="ignore").lower():
            return "wsl"
    except Exception:
        pass
    return "linux"


def detect_gpu() -> Tuple[Optional[str], Optional[float], Optional[str]]:
    """Returns (gpu_name, vram_gb, cuda_supported) from nvidia-smi.

    All three are None when nvidia-smi isn't on PATH (CPU-only install
    path). When nvidia-smi exists but reports no device, returns
    (None, None, "") so the caller can distinguish 'no GPU' from
    'no driver'.
    """
    if shutil.which("nvidia-smi") is None:
        return None, None, None
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
             "--format=csv,noheader"],
            text=True, timeout=10,
        ).strip()
    except Exception:
        return None, None, None
    if not out:
        return None, None, ""
    # First device only.
    line = out.splitlines()[0]
    parts = [p.strip() for p in line.split(",")]
    name = parts[0] if parts else None
    vram_gb = None
    if len(parts) >= 2:
        m = re.search(r"(\d+(?:\.\d+)?)", parts[1])
        if m:
            mem_mb = float(m.group(1))
            vram_gb = round(mem_mb / 1024, 1)
    # Now ask nvidia-smi for CUDA version
    cuda_ver = ""
    try:
        ver_out = subprocess.check_output(
            ["nvidia-smi"], text=True, timeout=10,
        )
        m = re.search(r"CUDA Version: (\d+\.\d+)", ver_out)
        if m:
            cuda_ver = m.group(1)
    except Exception:
        pass
    return name, vram_gb, cuda_ver


def cuda_tier_for(cuda_ver: str) -> str:
    """Map an nvidia-smi-reported CUDA version to a wheel tier we ship.

    Returns one of: 'cpu' | 'cu121' | 'cu124' | 'cu128'.
    """
    if not cuda_ver:
        return "cpu"
    major, _, minor = cuda_ver.partition(".")
    try:
        m, n = int(major), int(minor or "0")
    except ValueError:
        return "cu121"
    if (m, n) >= (12, 8) or m >= 13:
        return "cu128"
    if (m, n) >= (12, 4):
        return "cu124"
    if (m, n) >= (12, 0):
        return "cu121"
    return "cpu"


def check_tesseract() -> Tuple[bool, str]:
    """Looks for the Tesseract OCR binary. Tesseract is OPTIONAL — the
    image parser falls back to filename + EXIF indexing when missing.
    Returns (found, path-or-message)."""
    # Honour an existing env override first
    for env in ("COUNCIL_TESSERACT_CMD", "TESSERACT_CMD"):
        p = os.environ.get(env, "").strip()
        if p and Path(p).exists():
            return True, p
    common = []
    if sys.platform == "win32":
        common = [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ]
    else:
        common = ["/usr/bin/tesseract", "/usr/local/bin/tesseract"]
    for c in common:
        if Path(c).exists():
            return True, c
    on_path = shutil.which("tesseract")
    if on_path:
        return True, on_path
    return False, ("(not installed — OCR for image content will be skipped; "
                   "image filenames are still searchable)")


# ============================================================
# Subprocess runner with friendly output
# ============================================================

def run_pip(py: Path, args: List[str], *, quiet: bool = False) -> int:
    """Run pip against the given Python interpreter. Streams output.
    Returns the exit code."""
    cmd = [str(py), "-m", "pip"] + args
    if not quiet:
        print(dim(f"  $ {' '.join(args)}"))
    try:
        proc = subprocess.run(cmd, check=False)
        return proc.returncode
    except FileNotFoundError:
        print(bad(f"  pip / python not invokable: {py}"))
        return 127


def venv_python(root: Path) -> Path:
    """Path to the venv's Python interpreter (whether or not it exists yet)."""
    if sys.platform == "win32":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


# ============================================================
# Steps
# ============================================================

TOTAL_STEPS = 10


def step1_welcome(root: Path) -> None:
    banner(1, TOTAL_STEPS, "Welcome")
    print(f"""
  This wizard sets up the Council ("Data's Inferno") workspace
  in:  {bold(str(root))}

  It will, only when you say yes at each step:
    • Create a Python virtual environment under {dim('./.venv')}
    • Install PyTorch + llama-cpp-python (CUDA wheel if you have an NVIDIA GPU)
    • Install the Council Python dependencies (~1-2 GB)
    • Help you download a US-origin GGUF model (~3-9 GB depending on choice)
    • Persist your settings to {dim('vault/backend_settings.json')}

  Nothing leaves your machine. You can re-run this wizard any time.
  Press {bold('Ctrl+C')} to cancel — nothing destructive runs before you confirm.
""")


def step2_environment(args) -> Tuple[str, Optional[str], Optional[float], str]:
    banner(2, TOTAL_STEPS, "Detecting your environment")
    os_name = detect_os()
    print(f"  OS:               {bold(os_name)}")
    print(f"  Python:           {bold(sys.version.split()[0])}  ({sys.executable})")
    if sys.version_info < (3, 11):
        print(warn("  ⚠ Python ≥ 3.11 is recommended (3.12 is fine). Continuing anyway."))
    gpu_name, vram_gb, cuda_ver = detect_gpu()
    if gpu_name is None:
        print(dim("  GPU:              (no nvidia-smi found — CPU-only install will be used)"))
        cuda_tier = "cpu"
    else:
        print(f"  GPU:              {bold(gpu_name)}  ({vram_gb} GB VRAM)")
        print(f"  Driver supports:  CUDA {cuda_ver or '?'}")
        cuda_tier = cuda_tier_for(cuda_ver or "")
        print(f"  Selected wheel:   {bold(cuda_tier)} "
              + dim(f"(override with COUNCIL_CUDA_TIER=...)"))
    if os.environ.get("COUNCIL_CUDA_TIER"):
        cuda_tier = os.environ["COUNCIL_CUDA_TIER"]
        print(warn(f"  override: COUNCIL_CUDA_TIER={cuda_tier}"))
    return os_name, gpu_name, vram_gb, cuda_tier


def step3_venv(root: Path, args) -> Path:
    banner(3, TOTAL_STEPS, "Python virtual environment")
    vpy = venv_python(root)
    if vpy.exists():
        print(good(f"  ✓ found existing venv at {root / '.venv'}"))
        return vpy
    print(f"  No venv yet at {root / '.venv'}")
    if not (args.auto or confirm("Create one now?", default=True)):
        print(warn("  Skipping venv creation — using current Python instead."))
        return Path(sys.executable)
    print(dim("  creating venv (this takes ~10s)..."))
    try:
        subprocess.check_call([sys.executable, "-m", "venv", str(root / ".venv")])
    except subprocess.CalledProcessError as e:
        print(bad(f"  venv creation failed: {e}"))
        sys.exit(1)
    # Upgrade pip in the new venv
    run_pip(vpy, ["install", "--upgrade", "pip", "setuptools", "wheel"], quiet=True)
    print(good(f"  ✓ created venv at {root / '.venv'}"))
    return vpy


def step4_cuda_wheels(py: Path, cuda_tier: str, args) -> None:
    banner(4, TOTAL_STEPS, "PyTorch + llama-cpp-python")
    # Skip-if-already-installed: probe via python -c
    try:
        out = subprocess.check_output(
            [str(py), "-c",
             "import torch, llama_cpp; "
             "print(torch.__version__, torch.cuda.is_available(), llama_cpp.__version__)"],
            text=True, stderr=subprocess.STDOUT, timeout=30,
        ).strip()
        print(good("  ✓ torch + llama-cpp-python already installed:"))
        print(f"    {out}")
        if not args.auto and not confirm("Reinstall anyway?", default=False):
            return
    except Exception:
        pass

    print(f"  Installing torch ({cuda_tier}) — this is the big download (~2-3 GB).")
    if cuda_tier == "cpu":
        rc = run_pip(py, ["install", "torch", "torchvision", "torchaudio",
                          "--index-url", "https://download.pytorch.org/whl/cpu"])
    elif cuda_tier in ("cu121", "cu124"):
        rc = run_pip(py, ["install", "torch", "torchvision", "torchaudio",
                          "--index-url", f"https://download.pytorch.org/whl/{cuda_tier}"])
    else:  # cu128
        rc = run_pip(py, ["install", "--pre", "torch", "torchvision", "torchaudio",
                          "--index-url",
                          "https://download.pytorch.org/whl/nightly/cu128"])
    if rc != 0:
        print(bad("  torch install failed."))
        if not confirm("Continue anyway?", default=False):
            sys.exit(1)

    print(f"  Installing llama-cpp-python ({cuda_tier}) — the GGUF runtime.")
    if cuda_tier == "cpu":
        rc = run_pip(py, ["install", "llama-cpp-python"])
    else:
        # For cu128 the abetlen wheel index doesn't ship 128 yet — cu124
        # is the recommended fallback (PTX on newer arch).
        wheel_tier = "cu124" if cuda_tier == "cu128" else cuda_tier
        rc = run_pip(py, [
            "install", "llama-cpp-python",
            "--extra-index-url",
            f"https://abetlen.github.io/llama-cpp-python/whl/{wheel_tier}",
            "--force-reinstall", "--no-cache-dir",
        ])
    if rc != 0:
        print(bad("  llama-cpp-python install failed."))
        sys.exit(1)
    print(good("  ✓ torch + llama-cpp-python installed."))


def step5_council_deps(py: Path, args) -> None:
    banner(5, TOTAL_STEPS, "Council Python dependencies")
    pkgs = [
        "PyYAML", "requests", "pandas", "openpyxl", "xlrd", "pyarrow", "h5py",
        "matplotlib", "plotly", "tkinterweb",
        "chromadb", "sentence-transformers>=2.7,<5", "transformers>=4.44,<5",
        "huggingface_hub", "pypdf", "python-docx", "duckdb", "pymongo",
        "SQLAlchemy>=2.0,<3.0", "beautifulsoup4", "lxml", "html2text",
        "pyttsx3", "faster-whisper", "sounddevice", "soundfile", "paramiko",
        "pytesseract",          # used only if Tesseract binary is present (optional)
        "pip-system-certs",     # Windows cert chain for huggingface.co etc.
        "reportlab",            # used by the corpus generators
    ]
    # Skip-if-installed shortcut: probe a few key imports
    try:
        subprocess.check_call(
            [str(py), "-c",
             "import chromadb, sentence_transformers, openpyxl, pypdf, docx"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print(good("  ✓ core Council deps already installed."))
        if not args.auto and not confirm("Refresh anyway (slower)?", default=False):
            return
    except Exception:
        pass
    rc = run_pip(py, ["install", "--upgrade-strategy", "only-if-needed"] + pkgs)
    if rc != 0:
        print(warn("  some pip installs failed; the GUI will still launch but "
                   "some features may be degraded."))
    else:
        print(good("  ✓ Council deps installed."))


def step6_pick_model(py: Path, args) -> "Optional[object]":
    banner(6, TOTAL_STEPS, "Pick a model")
    try:
        import model_catalog as mc
    except Exception as e:
        print(bad(f"  model_catalog import failed: {e!r}"))
        return None
    # Filter by VRAM if we know it. ALWAYS include the "tiny" role
    # alongside "general" — the only model in the catalog that fits a
    # 4 GB GPU (or runs comfortably on CPU) is llama-3.2-1b-q8, which
    # has role="tiny". The previous strict role="general" filter showed
    # zero models to anyone with a low-VRAM GPU, dead-ending the wizard
    # at "Choose 1-0 or s".
    _, vram_gb, _ = detect_gpu()
    if vram_gb:
        suitable = sorted(
            (m for m in mc.MODELS
             if m.role in ("general", "tiny") and mc.fits(m, vram_gb)),
            key=lambda m: (not m.is_default, -m.params_b),
        )
        print(f"  Showing models that fit comfortably in {vram_gb} GB VRAM. "
              "Add --all to see everything.")
    else:
        suitable = [m for m in mc.MODELS if m.role in ("general", "tiny")]
    if args.all:
        suitable = list(mc.MODELS)

    if args.model:
        chosen = mc.by_id(args.model)
        if chosen is None:
            print(bad(f"  --model {args.model} not in catalog"))
            return None
        print(good(f"  preselected via --model: {chosen.id}"))
        return chosen

    # If no model fits the detected VRAM budget, offer the full
    # catalog with a warning rather than leaving the user at an
    # impossible "Choose 1-0 or s" prompt.
    if not suitable:
        print(warn(
            f"  No catalogued model fits a {vram_gb} GB budget "
            "comfortably. Showing the full catalog — the smallest "
            "option may still run if you accept slow CPU offload."))
        suitable = [m for m in mc.MODELS if m.role in ("general", "tiny")]

    print()
    for i, m in enumerate(suitable, 1):
        tag = bold("★ DEFAULT") if m.is_default else ""
        print(f"  {bold(str(i))}.  {m.name}  {tag}")
        print(f"      {dim(m.org + '  •  ' + str(m.size_gb) + ' GB  •  ctx ' + str(m.context_k) + 'K  •  ' + m.license)}")
        print(f"      {m.blurb}")
    print(f"  {bold('s')}.  Skip — I'll pick a model later (or already have one)")
    print()
    default_idx = next((i for i, m in enumerate(suitable, 1) if m.is_default), 1)
    sel = ask(f"Choose 1-{len(suitable)} or s", str(default_idx))
    if sel.lower().startswith("s"):
        print(warn("  Skipping model download. You can re-run this wizard later."))
        return None
    try:
        i = int(sel)
    except ValueError:
        print(bad(f"  not a number: {sel}"))
        return None
    if not (1 <= i <= len(suitable)):
        print(bad(f"  out of range: {sel}"))
        return None
    chosen = suitable[i - 1]
    print(good(f"  selected: {chosen.id}"))
    return chosen


def step7_download(py: Path, root: Path, spec, args) -> Optional[Path]:
    banner(7, TOTAL_STEPS, "Downloading model")
    if spec is None:
        print(dim("  no model selected — skipping."))
        return None
    dest = root / "models"
    dest.mkdir(exist_ok=True)
    target = dest / spec.hf_file
    if target.exists() and target.stat().st_size > 1_000_000_000:
        print(good(f"  ✓ already on disk: {target}"))
        return target
    print(f"  Will download:")
    print(f"    repo: {spec.hf_repo}")
    print(f"    file: {spec.hf_file}")
    print(f"    size: ~{spec.size_gb} GB")
    print(f"    into: {dest}")
    if not args.auto and not confirm("Proceed?", default=True):
        print(warn("  download skipped."))
        return None
    # Use the venv's huggingface_hub
    code = (
        "from huggingface_hub import hf_hub_download as h; "
        f"print(h(repo_id={spec.hf_repo!r}, filename={spec.hf_file!r}, "
        f"local_dir={str(dest)!r}))"
    )
    print(dim("  downloading (multi-GB; this can take several minutes)..."))
    try:
        out = subprocess.check_output([str(py), "-c", code],
                                       text=True, stderr=subprocess.STDOUT)
        print(good(f"  ✓ downloaded: {out.strip()}"))
        return Path(out.strip())
    except subprocess.CalledProcessError as e:
        print(bad("  download failed:"))
        print(e.output)
        return None


def step8_tesseract(args) -> None:
    banner(8, TOTAL_STEPS, "Image OCR (optional)")
    ok, info = check_tesseract()
    if ok:
        print(good(f"  ✓ Tesseract found: {info}"))
        print(dim("    Set COUNCIL_TESSERACT_CMD to override (the Windows launcher already does this)."))
        return
    print(warn("  Tesseract is NOT installed — that's fine, this step is OPTIONAL."))
    print(f"  {info}")
    print()
    print("  Tesseract enables text extraction from image content")
    print("  (defect photos, scanned documents, screenshots).")
    print("  Without it: image FILENAMES are still searchable, just not pixel content.")
    print()
    if sys.platform == "win32":
        print("  Want OCR? Install Tesseract via either:")
        print("    " + bold("winget install UB-Mannheim.TesseractOCR --source winget"))
        print("    or download from https://github.com/UB-Mannheim/tesseract/wiki")
    else:
        print("  Want OCR? Install via your package manager:")
        print("    " + bold("sudo apt install tesseract-ocr      # Debian/Ubuntu"))
        print("    " + bold("sudo dnf install tesseract          # Fedora"))
        print("    " + bold("sudo pacman -S tesseract            # Arch"))
    print()
    print(dim("  (You can install it any time later; just rerun this wizard or"))
    print(dim("   set COUNCIL_TESSERACT_CMD before launching.)"))


def step9_persist(root: Path, spec, model_path: Optional[Path]) -> None:
    banner(9, TOTAL_STEPS, "Saving your settings")
    # Default to ~/.council/vault (matches council_gui_engine.VAULT_DIR).
    # COUNCIL_VAULT_ROOT override is respected at runtime.
    vault = Path.home() / ".council" / "vault"
    vault.mkdir(parents=True, exist_ok=True)
    cfg_path = vault / "backend_settings.json"
    if model_path:
        cfg = {"gguf_path": str(model_path)}
        if spec is not None:
            cfg["model_id"] = spec.id
            cfg["model_org"] = spec.org
        try:
            cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            print(good(f"  ✓ saved: {cfg_path}"))
        except Exception as e:
            print(warn(f"  could not write {cfg_path}: {e!r}"))
    else:
        print(dim(f"  no model selected — skipping settings write."))
    # Mark first-run done so the in-GUI Tkinter onboarding skips next launch.
    try:
        (vault / ".onboarded").write_text("ok\n", encoding="utf-8")
    except Exception:
        pass


def step10_launch(root: Path, py: Path, args) -> None:
    banner(10, TOTAL_STEPS, "Launch")
    print(f"  Everything's set up. The app lives in:")
    print(f"    {bold(str(root))}")
    print()
    if sys.platform == "win32":
        print(f"  To launch now:  {bold('run-windows.bat')}")
        print(f"  Or directly:    {bold(str(py) + ' council_gui_engine.py')}")
    else:
        print(f"  To launch now:  {bold('./run-linux.sh')}  (or ./run-wsl.sh inside WSL)")
        print(f"  Or directly:    {bold(str(py) + ' council_gui_engine.py')}")
    print()
    if args.auto:
        return
    if not confirm("Launch the app now?", default=True):
        print(good("  Done. Re-run this wizard any time with `python setup_wizard.py`."))
        return
    print(dim("  starting council_gui_engine.py..."))
    try:
        env = os.environ.copy()
        env.setdefault("COUNCIL_BACKEND", "gguf")
        subprocess.Popen([str(py), "council_gui_engine.py"], cwd=str(root), env=env)
        print(good("  launched."))
    except Exception as e:
        print(bad(f"  launch failed: {e!r}"))


# ============================================================
# Diagnostic mode
# ============================================================

def diag(root: Path) -> int:
    print(header("Council setup — diagnostic check"))
    hr()
    os_name = detect_os()
    print(f"  OS:        {os_name}")
    print(f"  Python:    {sys.version.split()[0]}  ({sys.executable})")
    gpu_name, vram_gb, cuda_ver = detect_gpu()
    if gpu_name:
        print(f"  GPU:       {gpu_name}  ({vram_gb} GB)  CUDA {cuda_ver}  → tier {cuda_tier_for(cuda_ver)}")
    else:
        print(f"  GPU:       (none detected via nvidia-smi)")
    vpy = venv_python(root)
    print(f"  venv:      {'found' if vpy.exists() else 'MISSING'}   {vpy}")

    try:
        import model_catalog as mc
        print(f"  catalog:   {len(mc.MODELS)} models, default = {mc.DEFAULT_MODEL_ID}")
    except Exception as e:
        print(f"  catalog:   load failed: {e!r}")

    tess_ok, tess_info = check_tesseract()
    print(f"  tesseract: {'found' if tess_ok else 'optional, not installed'}   {tess_info}")

    vault = Path.home() / ".council" / "vault"
    cfg = vault / "backend_settings.json"
    if cfg.exists():
        try:
            blob = json.loads(cfg.read_text())
            print(f"  model:     {blob.get('gguf_path', '(unset)')}")
        except Exception:
            print(f"  model:     (cannot parse {cfg})")
    else:
        print(f"  model:     no backend_settings.json — wizard not yet completed")
    return 0


# ============================================================
# Main
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="setup_wizard",
        description="First-run setup for the Council app.",
    )
    parser.add_argument("--auto", action="store_true",
                        help="accept all defaults; non-interactive")
    parser.add_argument("--model", default=None,
                        help="preselect a model id from model_catalog "
                             "(e.g. phi-4-q4, llama-3.1-8b-q5)")
    parser.add_argument("--all", action="store_true",
                        help="show every catalog model, not just those fitting your VRAM")
    parser.add_argument("--check-only", action="store_true",
                        help="diagnostic only — no installs, no downloads")
    args = parser.parse_args()

    root = Path(__file__).resolve().parent

    if args.check_only:
        return diag(root)

    step1_welcome(root)
    os_name, gpu_name, vram_gb, cuda_tier = step2_environment(args)
    py = step3_venv(root, args)
    step4_cuda_wheels(py, cuda_tier, args)
    step5_council_deps(py, args)
    spec = step6_pick_model(py, args)
    path = step7_download(py, root, spec, args)
    step8_tesseract(args)
    step9_persist(root, spec, path)
    step10_launch(root, py, args)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  cancelled.")
        sys.exit(130)
