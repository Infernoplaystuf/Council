#!/usr/bin/env python3
"""
setup_council.py — one-command installer for Data's Inferno / Council.

Run this AFTER cloning the repo. It does everything between "I have
the source on disk" and "the GUI is ready to launch":

    1. Probes your hardware (CPU, RAM, GPU, CUDA version).
    2. Looks for a previous install we can reuse (existing conda env,
       previous GGUF model, previous vault).
    3. Picks the right CUDA wheel tier for your GPU (cu121 / cu124 /
       cu128 / cpu).
    4. Creates a conda env named `council` (Python 3.11) — or reuses
       the existing one if you have it.
    5. Installs torch + llama-cpp-python with the matching CUDA wheel.
    6. Installs the rest of the Python deps (numpy / pandas / chromadb
       / sentence-transformers / etc.) from requirements.txt.
    7. Runs the smoke test suite to verify the build is sound.
    8. Optionally launches the in-app GUI wizard for model selection.

Cross-platform — works on Windows, Linux, macOS, and WSL. Pure stdlib
during the bootstrap phase, so it runs against any Python ≥ 3.8 you
already have installed.

USAGE:
    python setup_council.py                  # interactive
    python setup_council.py --yes            # accept all defaults
    python setup_council.py --cuda-tier cpu  # override auto-detect
    python setup_council.py --skip-install   # plan only, no install
    python setup_council.py --reinstall      # blow away existing env

Why a separate file from Python's `setup.py` convention (setuptools
package install)? Because this app isn't pip-installable as a wheel
— it's a Tkinter app with bundled config files. The filename
`setup_council.py` makes the intent unambiguous.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


# ────────────────────────────────────────────────────────────────────
# Path bootstrap — find our own hardware_detect + previous_install_
# detect modules. We use only stdlib until the conda env is built.
# ────────────────────────────────────────────────────────────────────
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# These two modules are pure stdlib — safe to import before the env
# exists.
import hardware_detect            # noqa: E402
import previous_install_detect    # noqa: E402


# ────────────────────────────────────────────────────────────────────
# Terminal helpers — colour when supported, plain otherwise.
# ────────────────────────────────────────────────────────────────────
def _color_supported() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    if sys.platform.startswith("win"):
        # Windows 10+ supports ANSI when VT100 is enabled. Try to
        # enable it; fall back to no-colour if the call fails.
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            return False
    return True


_COLOR = _color_supported()
def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text

def step(n: int, total: int, msg: str) -> None:
    print(_c("36;1", f"\n[{n}/{total}] {msg}"))

def info(msg: str) -> None:
    print(_c("90", f"    {msg}"))

def ok(msg: str) -> None:
    print(_c("32", f"    ✓ {msg}"))

def warn(msg: str) -> None:
    print(_c("33", f"    ⚠ {msg}"))

def fail(msg: str) -> None:
    print(_c("31;1", f"    ✗ {msg}"))


# ────────────────────────────────────────────────────────────────────
# Shell helpers
# ────────────────────────────────────────────────────────────────────
def run(cmd: list, *, check: bool = True, capture: bool = False,
        shell: bool = False, env=None) -> subprocess.CompletedProcess:
    """Run a subprocess. By default streams output to the parent
    terminal so the user sees pip/conda progress in real time."""
    info("$ " + (cmd if shell else " ".join(map(str, cmd))))
    return subprocess.run(
        cmd, check=check, shell=shell,
        capture_output=capture, text=capture, env=env,
    )


def have(prog: str) -> bool:
    return shutil.which(prog) is not None


# ────────────────────────────────────────────────────────────────────
# Conda detection / install
# ────────────────────────────────────────────────────────────────────
def find_conda() -> "Path | None":
    """Locate a working conda / mamba / micromamba executable.

    Returns the Path to the binary, or None if nothing is installed."""
    for name in ("conda", "mamba", "micromamba"):
        p = shutil.which(name)
        if p:
            return Path(p)
    # Manual check of the common install dirs — some users have conda
    # installed but not on PATH.
    home = Path.home()
    for candidate in (
        home / "miniforge3"  / "Scripts" / "conda.exe",
        home / "miniforge3"  / "bin" / "conda",
        home / "miniconda3"  / "Scripts" / "conda.exe",
        home / "miniconda3"  / "bin" / "conda",
        home / "anaconda3"   / "Scripts" / "conda.exe",
        home / "anaconda3"   / "bin" / "conda",
        Path("C:/ProgramData/miniconda3/Scripts/conda.exe"),
        Path("C:/ProgramData/Anaconda3/Scripts/conda.exe"),
        Path("/opt/conda/bin/conda"),
    ):
        if candidate.is_file():
            return candidate
    return None


def install_miniforge_unix() -> "Path | None":
    """Bootstrap Miniforge on Linux / macOS / WSL. Returns the conda
    binary path on success, None on failure."""
    home = Path.home()
    target = home / "miniforge3"
    if target.is_dir():
        ok(f"miniforge already present at {target}")
        return target / "bin" / "conda"

    if sys.platform == "darwin":
        url_base = "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-MacOSX"
        arch = "arm64" if (os.uname().machine in ("arm64", "aarch64")) else "x86_64"
        url = f"{url_base}-{arch}.sh"
    else:
        url = ("https://github.com/conda-forge/miniforge/releases/latest/"
               "download/Miniforge3-Linux-x86_64.sh")

    installer = home / "miniforge_installer.sh"
    info(f"Downloading Miniforge installer ({url}) → {installer}")
    try:
        urllib.request.urlretrieve(url, str(installer))
    except Exception as exc:
        fail(f"Miniforge download failed: {exc}")
        return None
    try:
        run(["bash", str(installer), "-b", "-p", str(target)])
    except subprocess.CalledProcessError as exc:
        fail(f"Miniforge install failed: {exc}")
        return None
    finally:
        try:
            installer.unlink()
        except Exception:
            pass
    conda = target / "bin" / "conda"
    return conda if conda.is_file() else None


def install_miniforge_windows() -> "Path | None":
    """On Windows we don't auto-install conda — the silent installer
    is unreliable and users typically already have it. Instead we
    print actionable guidance and bail. The user re-runs after they've
    installed it from https://conda-forge.org/miniforge/."""
    fail("conda was not found on PATH and we don't auto-install on Windows.")
    print()
    print("    Install Miniforge from:")
    print(_c("36;4", "      https://conda-forge.org/miniforge/"))
    print("    Pick: 'Windows installers' → Miniforge3-Windows-x86_64.exe")
    print("    During install, leave 'Add to PATH' UNCHECKED (recommended)")
    print("    but DO check 'Register as default Python'.")
    print()
    print("    After install, open a NEW PowerShell or Anaconda Prompt and")
    print("    re-run:  python setup_council.py")
    return None


# ────────────────────────────────────────────────────────────────────
# Plan
# ────────────────────────────────────────────────────────────────────
def build_plan(hw: dict, prev: dict, args) -> dict:
    """Combine hardware + previous-install + CLI overrides into the
    concrete plan we'll execute. Returns a dict the runner consumes."""
    rec = hw.get("recommended") or {}
    cuda_tier = args.cuda_tier or rec.get("cuda_tier", "cpu")
    env_name = args.env_name or "council"
    python_version = args.python_version or "3.11"
    reuse_env = (prev.get("conda_env", {}).get("present")
                  and not args.reinstall)

    return {
        "env_name":        env_name,
        "python_version":  python_version,
        "cuda_tier":       cuda_tier,
        "reuse_env":       reuse_env,
        "previous_model":  prev.get("previous_model"),
        "found_models":    [m for m in (prev.get("gguf_models") or [])
                            if m.get("valid")],
        "recommended":     rec,
    }


# ────────────────────────────────────────────────────────────────────
# Install commands
# ────────────────────────────────────────────────────────────────────
_TORCH_INDEX = {
    "cpu":   "https://download.pytorch.org/whl/cpu",
    "cu121": "https://download.pytorch.org/whl/cu121",
    "cu124": "https://download.pytorch.org/whl/cu124",
    "cu128": "https://download.pytorch.org/whl/nightly/cu128",
}

_LLAMA_CPP_INDEX = {
    "cpu":   None,  # plain PyPI wheel
    "cu121": "https://abetlen.github.io/llama-cpp-python/whl/cu121",
    "cu124": "https://abetlen.github.io/llama-cpp-python/whl/cu124",
    "cu128": "https://abetlen.github.io/llama-cpp-python/whl/cu124",  # nightly torch, cu124 llama-cpp
}


def env_python(conda: Path, env_name: str) -> "Path | None":
    """Return the Python binary inside the conda env, or None if env
    doesn't exist."""
    # Run `conda run -n <env> python -c 'import sys; print(sys.executable)'`
    try:
        r = subprocess.run(
            [str(conda), "run", "-n", env_name,
             "python", "-c", "import sys; print(sys.executable)"],
            capture_output=True, text=True, check=False,
        )
        if r.returncode == 0:
            p = Path(r.stdout.strip())
            if p.is_file():
                return p
    except Exception:
        pass
    return None


def run_in_env(conda: Path, env_name: str, args: list,
               check: bool = True) -> subprocess.CompletedProcess:
    """Run a command inside the conda env. Uses `conda run`."""
    full = [str(conda), "run", "--no-capture-output", "-n", env_name] + args
    return run(full, check=check)


def execute_plan(plan: dict, conda: Path, *, only_plan: bool = False) -> bool:
    """Run the conda + pip install dance. Returns True on success."""
    env_name      = plan["env_name"]
    python_v      = plan["python_version"]
    cuda_tier     = plan["cuda_tier"]
    reuse         = plan["reuse_env"]

    if only_plan:
        info("--skip-install set; printing plan only.")
        print()
        print(_c("36;1", "Would run:"))
        print(f"  conda create -n {env_name} python={python_v} -y")
        print(f"  conda activate {env_name}")
        print(f"  pip install torch --index-url {_TORCH_INDEX[cuda_tier]}")
        url = _LLAMA_CPP_INDEX[cuda_tier]
        if url:
            print(f"  pip install llama-cpp-python --extra-index-url {url} "
                  "--force-reinstall --no-cache-dir")
        else:
            print("  pip install llama-cpp-python")
        print("  pip install -r requirements.txt")
        return True

    # ── Create or reuse env ──
    if reuse:
        ok(f"reusing existing conda env '{env_name}'")
    else:
        try:
            run([str(conda), "create", "-n", env_name,
                 f"python={python_v}", "-y"])
        except subprocess.CalledProcessError:
            fail(f"failed to create env '{env_name}'")
            return False

    # ── torch ──
    info(f"installing torch ({cuda_tier} wheels)")
    torch_idx = _TORCH_INDEX[cuda_tier]
    torch_args = ["pip", "install", "torch", "torchvision", "torchaudio",
                   "--index-url", torch_idx]
    if cuda_tier == "cu128":
        torch_args.insert(2, "--pre")
    try:
        run_in_env(conda, env_name, torch_args)
    except subprocess.CalledProcessError:
        fail("torch install failed")
        return False

    # ── llama-cpp-python ──
    info(f"installing llama-cpp-python ({cuda_tier})")
    lcp_args = ["pip", "install", "llama-cpp-python"]
    lcp_idx = _LLAMA_CPP_INDEX[cuda_tier]
    if lcp_idx:
        lcp_args += ["--extra-index-url", lcp_idx,
                      "--force-reinstall", "--no-cache-dir"]
    try:
        run_in_env(conda, env_name, lcp_args)
    except subprocess.CalledProcessError:
        fail("llama-cpp-python install failed — see error above")
        return False

    # ── requirements.txt ──
    req_file = HERE / "requirements.txt"
    if req_file.is_file():
        info("installing remaining deps from requirements.txt")
        try:
            run_in_env(conda, env_name,
                       ["pip", "install", "-r", str(req_file),
                        "--upgrade-strategy", "only-if-needed"])
        except subprocess.CalledProcessError:
            fail("requirements.txt install failed")
            return False
    else:
        warn("requirements.txt not found — skipping bulk dep install")

    return True


# ────────────────────────────────────────────────────────────────────
# Smoke test runner
# ────────────────────────────────────────────────────────────────────
def run_smoke_test(conda: Path, env_name: str) -> bool:
    """Invoke tests/smoke_test.py inside the freshly built env."""
    test_path = HERE / "tests" / "smoke_test.py"
    if not test_path.is_file():
        warn("tests/smoke_test.py not found — skipping verification")
        return True
    info("running smoke tests inside the env")
    try:
        run_in_env(conda, env_name, ["python", str(test_path)])
        ok("smoke tests passed")
        return True
    except subprocess.CalledProcessError:
        fail("smoke tests reported failures — see output above")
        return False


# ────────────────────────────────────────────────────────────────────
# Interactive prompts
# ────────────────────────────────────────────────────────────────────
def confirm(question: str, default: bool = True,
            auto_yes: bool = False) -> bool:
    if auto_yes:
        return True
    suffix = " [Y/n] " if default else " [y/N] "
    while True:
        try:
            ans = input(_c("36", question + suffix)).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print(_c("31", "    please answer y or n"))


# ────────────────────────────────────────────────────────────────────
# Pretty-printers
# ────────────────────────────────────────────────────────────────────
def print_hardware(hw: dict) -> None:
    print(f"    OS:       {hw.get('os','?')} — {hw.get('os_version','?')}")
    print(f"    CPU:      {hw.get('cpu_brand') or '?'}   "
          f"({hw.get('cpu_cores') or '?'} cores)")
    ram = hw.get("ram_gb")
    print(f"    RAM:      {ram:.1f} GB" if ram else "    RAM:      (unknown)")
    gpu_line = hw.get("gpu_name") or "(none — CPU-only)"
    if hw.get("vram_gb"):
        gpu_line += f"   ·  {hw['vram_gb']:.1f} GB VRAM"
    print(f"    GPU:      {gpu_line}")
    if hw.get("cuda_max"):
        print(f"    CUDA max: {hw['cuda_max']}")
    if hw.get("notes"):
        for n in hw["notes"]:
            warn(n)


def print_previous(prev: dict) -> None:
    env = prev.get("conda_env", {}) or {}
    if env.get("present"):
        ok(f"existing conda env at {env.get('path') or '(unknown path)'}")
    else:
        info("no existing 'council' conda env")
    vlt = prev.get("vault", {}) or {}
    if vlt.get("present"):
        info(f"vault at {vlt['path']}  ({vlt['data_in_files']} file(s) in data_in/)")
    else:
        info("no existing vault")
    models = prev.get("gguf_models") or []
    valid = [m for m in models if m.get("valid")]
    if valid:
        ok(f"{len(valid)} valid GGUF model(s) found")
        for m in valid[:5]:
            info(f"   {m['name']}  ({m.get('size_gb','?')} GB)")
    elif models:
        warn(f"{len(models)} GGUF file(s) but NONE pass magic-byte check — "
              "likely failed downloads")
    if prev.get("previous_model"):
        info(f"last-used model: {prev['previous_model']}")


def print_plan(plan: dict) -> None:
    rec = plan.get("recommended", {})
    print(f"    Env name:      {plan['env_name']}")
    print(f"    Python:        {plan['python_version']}")
    print(f"    CUDA tier:     {plan['cuda_tier']}")
    print(f"    Reusing env:   {'yes' if plan['reuse_env'] else 'no — will create'}")
    print(f"    Model pick:    {rec.get('model_pick','?')}")
    print(f"    Max n_ctx:     {rec.get('n_ctx_max','?'):,}"
          if isinstance(rec.get('n_ctx_max'), int) else
          f"    Max n_ctx:     {rec.get('n_ctx_max','?')}")


# ────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(
        prog="setup_council.py",
        description="One-command installer for Data's Inferno / Council.",
    )
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Accept all defaults; non-interactive.")
    parser.add_argument("--cuda-tier", choices=("cpu", "cu121", "cu124", "cu128"),
                        help="Override auto-detected CUDA wheel tier.")
    parser.add_argument("--env-name", default=None,
                        help="Conda env name (default: 'council').")
    parser.add_argument("--python-version", default=None,
                        help="Python version (default: 3.11).")
    parser.add_argument("--skip-install", action="store_true",
                        help="Print the plan but skip the install step.")
    parser.add_argument("--reinstall", action="store_true",
                        help="Recreate the env even if one already exists.")
    parser.add_argument("--skip-smoke", action="store_true",
                        help="Skip the post-install smoke test.")
    args = parser.parse_args()

    total = 5
    print(_c("36;1;4", "\nData's Inferno — one-command installer\n"))

    step(1, total, "Probing hardware")
    hw = hardware_detect.detect()
    print_hardware(hw)

    step(2, total, "Looking for a previous install")
    prev = previous_install_detect.detect(HERE, HERE / "vault")
    print_previous(prev)

    step(3, total, "Build install plan")
    plan = build_plan(hw, prev, args)
    print_plan(plan)
    if not confirm("Proceed with this plan?", default=True, auto_yes=args.yes):
        info("Aborted.")
        return 0

    step(4, total, "Locate or install conda")
    conda = find_conda()
    if conda is None:
        if sys.platform.startswith("win"):
            install_miniforge_windows()
            return 2
        # Linux / macOS / WSL — offer to install Miniforge
        if confirm("conda not found — install Miniforge into ~/miniforge3?",
                    default=True, auto_yes=args.yes):
            conda = install_miniforge_unix()
            if conda is None:
                return 2
        else:
            fail("conda required. Install miniforge or miniconda manually.")
            return 2
    ok(f"using conda at {conda}")

    step(5, total, "Install (this can take 10-25 min on a cold start)")
    if not execute_plan(plan, conda, only_plan=args.skip_install):
        fail("install did not complete cleanly")
        return 3

    if not args.skip_install and not args.skip_smoke:
        if not run_smoke_test(conda, plan["env_name"]):
            warn("smoke tests failed — the app may still run, but check "
                 "the failures above before relying on it.")

    print()
    print(_c("32;1", "✓ Setup complete."))
    print()
    print("Next steps:")
    print(f"  1. Activate the env:    conda activate {plan['env_name']}")
    if not (plan.get("previous_model")
            or any(m.get("valid") for m in plan.get("found_models", []))):
        print(f"  2. Download a model:    huggingface-cli download "
              f"bartowski/{_model_for_tier(plan['recommended'].get('model_tier','small'))}"
              f" --local-dir ~/models")
        print(f"  3. Point at the model: export COUNCIL_GGUF_PATH=~/models/<file>.gguf")
        print(f"  4. Launch:             python council_gui_engine.py")
    else:
        print(f"  2. Launch:             python council_gui_engine.py")
        print(f"     The wizard will preselect the model it found.")
    return 0


def _model_for_tier(tier: str) -> str:
    """Map a recommendation tier to a Hugging Face repo slug for the
    'next steps' hint at the end of setup."""
    return {
        "large":    "Llama-3.3-70B-Instruct-GGUF",
        "medium":   "phi-4-GGUF",
        "small":    "Meta-Llama-3.1-8B-Instruct-GGUF",
        "tiny":     "Llama-3.2-3B-Instruct-GGUF",
        "cpu_only": "Llama-3.2-3B-Instruct-GGUF",
    }.get(tier, "phi-4-GGUF")


if __name__ == "__main__":
    sys.exit(main())
