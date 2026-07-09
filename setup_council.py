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
    terminal so the user sees pip/conda progress in real time.

    Most callers should use ``step_run`` instead — it gives
    structured failure reporting + tracks the step counter."""
    info("$ " + (cmd if shell else " ".join(map(str, cmd))))
    return subprocess.run(
        cmd, check=check, shell=shell,
        capture_output=capture, text=capture, env=env,
    )


def have(prog: str) -> bool:
    return shutil.which(prog) is not None


# ────────────────────────────────────────────────────────────────────
# Step runner — single source of truth for "run a command, report
# success/failure with full context, never silently swallow errors".
#
# This replaces the previous pattern where each install step had its
# own try/except that printed a one-liner like "torch install failed".
# Users reported "the setup crashes the second there is one failure"
# — the symptom was that the one-liner gave them no idea what to do
# next. step_run carries:
#   • Step number and human-readable label
#   • The exact command that was attempted (so the user can re-run
#     it manually to debug)
#   • The exit code on failure
#   • A "suggested fix" string the caller passes in — the wrong-
#     CUDA-tier hint, the "check your internet" hint, etc.
# Plus it appends to a module-level _STEP_LOG so the failure summary
# at the end of main() can print exactly which step failed.
# ────────────────────────────────────────────────────────────────────

_STEP_LOG: list = []   # list of (step_no, label, status, detail)
_STEP_COUNTER = [0]    # mutable singleton for step numbering


def step_run(
    label: str,
    cmd: list,
    *,
    suggested_fix: str = "",
    soft_fail: bool = False,
    env=None,
) -> bool:
    """Run a command as a numbered install step.

    Parameters
    ----------
    label : str
        Human-readable step name shown in the progress line and the
        final summary. Examples: "create conda env", "install torch
        (cu124)", "install requirements.txt".
    cmd : list
        argv-style command list.
    suggested_fix : str
        Free-text hint shown on failure. The model knows the most
        common cause for each step (e.g. torch failure → check
        internet + check CUDA tier; conda create failure → check
        env name not already in use elsewhere).
    soft_fail : bool
        When True, a non-zero exit is logged as a warning rather
        than a failure. Used for optional steps (crawl4ai-setup,
        embedding model warm-cache).
    env : dict, optional
        Environment for the subprocess.

    Returns
    -------
    bool
        True on success (or soft-fail). False on a hard failure —
        caller should abort the install.
    """
    _STEP_COUNTER[0] += 1
    n = _STEP_COUNTER[0]
    print()
    print(_c("36;1", f"  [step {n}] {label}"))
    info("$ " + " ".join(str(c) for c in cmd))
    try:
        result = subprocess.run(cmd, check=False, env=env)
    except FileNotFoundError as exc:
        _STEP_LOG.append((n, label, "fail",
                          f"executable not found: {exc}"))
        fail(f"step {n} ({label}): the executable is not on PATH.")
        if suggested_fix:
            print(_c("33", f"    suggested fix: {suggested_fix}"))
        return False
    except Exception as exc:
        _STEP_LOG.append((n, label, "fail",
                          f"subprocess raised: {exc!r}"))
        fail(f"step {n} ({label}): subprocess raised {exc!r}")
        if suggested_fix:
            print(_c("33", f"    suggested fix: {suggested_fix}"))
        return False

    if result.returncode == 0:
        _STEP_LOG.append((n, label, "ok", ""))
        ok(f"step {n} ({label}) — done")
        return True

    # Non-zero exit — structured failure reporting.
    detail = f"exit code {result.returncode}"
    if soft_fail:
        _STEP_LOG.append((n, label, "skip", detail))
        warn(f"step {n} ({label}) failed ({detail}) — continuing "
             "(this step is optional).")
        if suggested_fix:
            print(_c("33", f"    note: {suggested_fix}"))
        return True

    _STEP_LOG.append((n, label, "fail", detail))
    fail(f"step {n} ({label}) failed ({detail}).")
    print(_c("33", "    The command that failed:"))
    print(_c("90", "      " + " ".join(str(c) for c in cmd)))
    if suggested_fix:
        print(_c("33", f"    suggested fix: {suggested_fix}"))
    print(_c("33", "    Re-run setup.sh / setup.bat to retry — the "
                    "script is idempotent and will skip steps that "
                    "already completed."))
    return False


def step_skip(label: str, reason: str) -> None:
    """Record a step as skipped — already done, optional, etc."""
    _STEP_COUNTER[0] += 1
    n = _STEP_COUNTER[0]
    _STEP_LOG.append((n, label, "skip", reason))
    print()
    print(_c("36;1", f"  [step {n}] {label}"))
    ok(f"step {n} ({label}) — skipping: {reason}")


def print_step_summary() -> None:
    """End-of-run table summarising every step. Always called from
    main() — successful runs see a green ✓ column; failed runs see
    the failing step in red so the user knows exactly where to look."""
    print()
    print(_c("36;1;4", "Install summary"))
    if not _STEP_LOG:
        info("(no steps ran)")
        return
    for n, label, status, detail in _STEP_LOG:
        if status == "ok":
            sym = _c("32", "✓")
        elif status == "skip":
            sym = _c("33", "○")
        else:
            sym = _c("31;1", "✗")
        line = f"  {sym}  step {n:>2}  {label}"
        if detail:
            line += "   " + _c("90", f"({detail})")
        print(line)


# ────────────────────────────────────────────────────────────────────
# Conda detection / install
# ────────────────────────────────────────────────────────────────────
def find_conda(prev: dict = None) -> "Path | None":
    """Locate a working conda / mamba / micromamba executable.

    Resolution order:
      1. previous_install_detect's conda_env.tool field — if the
         detector found a `council` env via `conda env list`, the
         tool that listed it (conda / mamba / micromamba) is on
         PATH and we trust that.
      2. shutil.which on conda / mamba / micromamba.
      3. Manual scan of common install dirs (~/miniforge3,
         ~/miniconda3, ~/anaconda3, /opt/conda, C:/ProgramData/...).

    Returns the Path to the binary, or None if nothing is found.
    Order matters: a user with conda installed under Anaconda
    Navigator may have NO conda on PATH but their env IS findable
    via `conda env list` from the Anaconda prompt the detector
    used. Trusting the detector's tool catches that case.
    """
    # Step 1 — trust the detector
    if prev:
        env_block = prev.get("conda_env") or {}
        tool = env_block.get("tool")
        if tool:
            p = shutil.which(tool)
            if p:
                return Path(p)

    # Step 2 — PATH scan
    for name in ("conda", "mamba", "micromamba"):
        p = shutil.which(name)
        if p:
            return Path(p)

    # Step 3 — known install locations
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


# ────────────────────────────────────────────────────────────────────
# Env verification + package probes
# ────────────────────────────────────────────────────────────────────

def verify_env(conda: Path, env_name: str) -> dict:
    """Probe a conda env. Returns a dict:
        {
          "exists": bool,
          "python_works": bool,
          "python_version": str | None,
          "pip_works": bool,
          "notes": list[str],   # human-readable findings
        }

    Used in the reuse decision. If `exists` is True but
    `python_works` is False, the env is half-built (a previous
    install crashed mid-conda-create) and the caller should offer
    to recreate.
    """
    out = {
        "exists": False,
        "python_works": False,
        "python_version": None,
        "pip_works": False,
        "notes": [],
    }
    # 1. Does the env exist at all? `conda env list` is the canonical
    #    answer — but only if conda itself is invokable.
    try:
        r = subprocess.run(
            [str(conda), "env", "list"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if r.returncode != 0:
            out["notes"].append(f"'conda env list' returned {r.returncode}")
            return out
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if parts and parts[0] == env_name:
                out["exists"] = True
                break
    except subprocess.TimeoutExpired:
        out["notes"].append("'conda env list' timed out after 15s — "
                             "conda may be stuck. Try `conda env list` "
                             "from a fresh terminal.")
        return out
    except Exception as exc:
        out["notes"].append(f"conda probe failed: {exc!r}")
        return out

    if not out["exists"]:
        return out

    # 2. Does Python work inside the env?
    try:
        r = subprocess.run(
            [str(conda), "run", "-n", env_name, "python",
             "-c", "import sys; print(sys.version.split()[0])"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if r.returncode == 0 and r.stdout.strip():
            out["python_works"] = True
            out["python_version"] = r.stdout.strip()
        else:
            out["notes"].append(
                f"env exists but 'python -c \"...\"' returned "
                f"{r.returncode}; stderr (last 200 chars): "
                f"{r.stderr[-200:].strip() if r.stderr else '(empty)'}"
            )
    except subprocess.TimeoutExpired:
        out["notes"].append("python invocation timed out inside the env.")
        return out
    except Exception as exc:
        out["notes"].append(f"python probe failed: {exc!r}")
        return out

    if not out["python_works"]:
        return out

    # 3. Does pip work?
    try:
        r = subprocess.run(
            [str(conda), "run", "-n", env_name, "pip", "--version"],
            capture_output=True, text=True, timeout=20, check=False,
        )
        out["pip_works"] = (r.returncode == 0)
        if not out["pip_works"]:
            out["notes"].append(
                f"pip is broken in the env (exit {r.returncode}). "
                "Recreate the env or run "
                f"'{conda} run -n {env_name} python -m ensurepip'.")
    except Exception as exc:
        out["notes"].append(f"pip probe failed: {exc!r}")
    return out


def pkg_already_installed(
    conda: Path,
    env_name: str,
    import_name: str,
    *,
    timeout: int = 15,
) -> bool:
    """Cheap probe: returns True if ``import <name>`` succeeds
    inside the env. Used by every install step to skip work that's
    already done — makes a re-run after a partial install instant
    on the steps that succeeded the first time around.

    `import_name` is the IMPORT name (e.g. 'llama_cpp', not
    'llama-cpp-python').
    """
    try:
        r = subprocess.run(
            [str(conda), "run", "-n", env_name, "python",
             "-c", f"import {import_name}"],
            capture_output=True, text=True, timeout=timeout,
            check=False,
        )
        return r.returncode == 0
    except Exception:
        return False


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
    """Run the conda + pip install dance. Returns True on success.

    Resumable + idempotent — each install step probes whether the
    package is already importable inside the env and skips if so.
    A re-run after a partial failure jumps straight to the broken
    step instead of redoing 2 GB of torch downloads.
    """
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

    # ── Step 1 — create or reuse env ──────────────────────────────────
    # The previous behaviour was "if previous_install_detect said the
    # env exists, reuse it." That happily reused a HALF-BUILT env
    # from a previous crashed install, and the next pip step then
    # exploded with no useful context. Now we VERIFY the env works
    # before reusing.
    if reuse:
        info("verifying the existing conda env actually works…")
        info_blk = verify_env(conda, env_name)
        if info_blk["exists"] and info_blk["python_works"] and info_blk["pip_works"]:
            step_skip(f"create conda env {env_name!r}",
                      f"reusing existing env (Python {info_blk['python_version']})")
        else:
            warn(f"the existing env {env_name!r} is broken:")
            for note in info_blk["notes"]:
                print(_c("90", f"      {note}"))
            warn("recreating from scratch.")
            if not step_run(
                f"recreate conda env {env_name!r}",
                [str(conda), "create", "-n", env_name,
                  f"python={python_v}", "-y"],
                suggested_fix=(
                    "If 'env already exists' — `conda env remove -n "
                    f"{env_name}` manually and re-run setup. Otherwise "
                    "check disk space and conda's own error message above."
                ),
            ):
                return False
    else:
        if not step_run(
            f"create conda env {env_name!r}",
            [str(conda), "create", "-n", env_name,
              f"python={python_v}", "-y"],
            suggested_fix=(
                "Common causes: (a) the env name already exists — "
                "pass --reinstall to recreate, or `conda env remove "
                f"-n {env_name}`. (b) disk full. (c) conda-forge "
                "channel down — try again later."
            ),
        ):
            return False

    # ── Step 2 — torch ────────────────────────────────────────────────
    # Skip if torch is already importable. Note: a CPU-only torch
    # already installed wouldn't be replaced by --cuda-tier=cu124,
    # but the skip prevents re-downloading 2 GB after a torch step
    # already succeeded. Users who DO want to swap tiers should
    # re-run with --reinstall (which blows away the whole env).
    if pkg_already_installed(conda, env_name, "torch"):
        step_skip(f"install torch ({cuda_tier})",
                  "already importable inside the env")
    else:
        torch_idx = _TORCH_INDEX[cuda_tier]
        torch_args = [str(conda), "run", "--no-capture-output", "-n", env_name,
                       "pip", "install", "torch", "torchvision", "torchaudio",
                       "--index-url", torch_idx]
        if cuda_tier == "cu128":
            torch_args.insert(torch_args.index("torch"), "--pre")
        if not step_run(
            f"install torch ({cuda_tier})",
            torch_args,
            suggested_fix=(
                "Common causes:\n"
                "       (a) the CUDA tier doesn't match your driver — "
                "run `nvidia-smi` and check 'CUDA Version' is ≥ what "
                f"{cuda_tier} needs (12.1 / 12.4 / 12.8).\n"
                "       (b) no internet / blocked HTTPS — pytorch.org "
                "must be reachable.\n"
                "       (c) wrong wheel for your Python version — only "
                "Python 3.8-3.12 is supported by torch wheels."
            ),
        ):
            return False

    # ── Step 3 — llama-cpp-python ────────────────────────────────────
    if pkg_already_installed(conda, env_name, "llama_cpp"):
        step_skip(f"install llama-cpp-python ({cuda_tier})",
                  "already importable inside the env")
    else:
        lcp_args = [str(conda), "run", "--no-capture-output", "-n", env_name,
                     "pip", "install", "llama-cpp-python"]
        lcp_idx = _LLAMA_CPP_INDEX[cuda_tier]
        if lcp_idx:
            lcp_args += ["--extra-index-url", lcp_idx,
                          "--force-reinstall", "--no-cache-dir"]
        if not step_run(
            f"install llama-cpp-python ({cuda_tier})",
            lcp_args,
            suggested_fix=(
                "Common causes:\n"
                "       (a) abetlen's CUDA wheel index is down — "
                "the URL is https://abetlen.github.io/llama-cpp-python\n"
                "       (b) the CUDA tier wheel doesn't exist for "
                "your Python — try a different tier or build from "
                "source: `CMAKE_ARGS='-DGGML_CUDA=ON' pip install "
                "llama-cpp-python --no-binary llama-cpp-python`\n"
                "       (c) compiler not installed (Windows: install "
                "Visual Studio Build Tools; Linux: apt install "
                "build-essential)."
            ),
        ):
            return False

    # ── Step 4 — requirements.txt (bulk pip install) ─────────────────
    # pandas / numpy / scipy / chromadb / sentence-transformers / etc.
    # No skip-probe here — `pip install -r requirements.txt --upgrade-
    # strategy only-if-needed` is itself idempotent and fast on
    # already-satisfied requirements (~5 seconds vs the 2-3 minute
    # first install). The skip-probe would need to import every dep
    # one by one which costs more than just letting pip resolve.
    req_file = HERE / "requirements.txt"
    if req_file.is_file():
        if not step_run(
            "install requirements.txt",
            [str(conda), "run", "--no-capture-output", "-n", env_name,
              "pip", "install", "-r", str(req_file),
              "--upgrade-strategy", "only-if-needed"],
            suggested_fix=(
                "Common causes:\n"
                "       (a) numpy / pandas ABI mismatch when re-using "
                "an old env — pass --reinstall.\n"
                "       (b) a single pip wheel failed to download — "
                "re-run setup; pip caches downloads and skips what "
                "succeeded.\n"
                "       (c) the chromadb / sentence-transformers "
                "transitive deps need a C compiler on first install "
                "for tokenisers — see the llama-cpp suggestion above."
            ),
        ):
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


def print_previous(prev: dict, *, verbose: bool = False) -> None:
    # ── Conda env ─────────────────────────────────────────────────
    env = prev.get("conda_env", {}) or {}
    if env.get("present"):
        ok(f"existing conda env at {env.get('path') or '(unknown path)'}")
        tool = env.get("tool")
        if tool:
            info(f"   ↳ listed by `{tool} env list`")
    else:
        info("no existing 'council' conda env")
        # Surface the diagnostic notes so the user can see WHAT we
        # tried — was conda invokable? did `conda env list` time out?
        # was the env in a non-standard location?
        for note in (env.get("notes") or [])[:6]:
            print(_c("90", f"      {note}"))
        # Show probed filesystem paths in verbose mode (otherwise it's
        # ~15 lines of paths nobody reads). Even without --verbose,
        # show the FIRST FEW so the user can confirm we covered their
        # install location.
        candidates = env.get("candidates_checked") or []
        if candidates:
            show = candidates if verbose else candidates[:4]
            print(_c("90",
                f"      probed {len(candidates)} candidate path(s); "
                f"{'showing all' if verbose else 'first 4'}:"))
            for c in show:
                print(_c("90", f"        {c}"))
            if not verbose and len(candidates) > 4:
                print(_c("90",
                    f"        ... ({len(candidates) - 4} more — "
                    "pass --verbose to see them all)"))

    # ── Vault ──────────────────────────────────────────────────────
    vlt = prev.get("vault", {}) or {}
    if vlt.get("present"):
        info(f"vault at {vlt['path']}  ({vlt['data_in_files']} file(s) in data_in/)")
        if vlt.get("has_settings"):
            info("   ↳ backend_settings.json present (model + clip pairing persisted)")
    else:
        info("no existing vault")
        for note in (vlt.get("notes") or [])[:4]:
            print(_c("90", f"      {note}"))
        for c in (vlt.get("alternates_checked") or []):
            print(_c("90", f"      also tried: {c}"))

    # ── Models ─────────────────────────────────────────────────────
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
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print every candidate path the detectors "
                             "probed (useful for diagnosing 'doesn't see "
                             "my install' reports).")
    args = parser.parse_args()

    total = 5
    print(_c("36;1;4", "\nData's Inferno — one-command installer\n"))

    step(1, total, "Probing hardware")
    hw = hardware_detect.detect()
    print_hardware(hw)

    step(2, total, "Looking for a previous install")
    # Vault lives at ~/.council/vault by GUI convention
    # (council_gui_engine.py: APP_DIR = Path.home() / ".council";
    # VAULT_DIR = APP_DIR / "vault"). Passing HERE/'vault' here was
    # a setup-script bug — it told the detector to look in the wrong
    # place, which is why users reported "can't find pre-existing
    # vault that stays consistent normally on where it saves at."
    vault_canonical = Path.home() / ".council" / "vault"
    env_name_for_probe = args.env_name or "council"
    prev = previous_install_detect.detect(
        HERE, vault_canonical, env_name=env_name_for_probe,
    )
    print_previous(prev, verbose=bool(getattr(args, "verbose", False)))

    step(3, total, "Build install plan")
    plan = build_plan(hw, prev, args)
    print_plan(plan)
    if not confirm("Proceed with this plan?", default=True, auto_yes=args.yes):
        info("Aborted.")
        return 0

    step(4, total, "Locate or install conda")
    # Pass `prev` so find_conda can trust the detector's discovery
    # of the env's managing tool (conda / mamba / micromamba). This
    # is what catches Anaconda-Navigator installs that don't put
    # conda on PATH but DO let `conda env list` find the env.
    conda = find_conda(prev)
    if conda is None:
        if sys.platform.startswith("win"):
            install_miniforge_windows()
            print_step_summary()
            return 2
        # Linux / macOS / WSL — offer to install Miniforge
        if confirm("conda not found — install Miniforge into ~/miniforge3?",
                    default=True, auto_yes=args.yes):
            conda = install_miniforge_unix()
            if conda is None:
                print_step_summary()
                return 2
        else:
            fail("conda required. Install miniforge or miniconda manually.")
            print_step_summary()
            return 2
    ok(f"using conda at {conda}")

    step(5, total, "Install (this can take 10-25 min on a cold start)")
    if not execute_plan(plan, conda, only_plan=args.skip_install):
        # execute_plan already logged structured diagnostics for the
        # specific step that failed. The summary table here gives
        # the user a single place to see where things stopped.
        print_step_summary()
        print()
        print(_c("31;1", "Setup did not complete."))
        print(_c("33", "  • Each install step is idempotent — re-running "
                        "setup will skip everything that succeeded above\n"
                        "    and pick up from the first failed step."))
        print(_c("33", "  • If the same step keeps failing, the suggested-"
                        "fix block printed above the summary lists the\n"
                        "    most common causes for that specific step."))
        print(_c("33", "  • To recreate the env from scratch (blows away "
                        "everything so far):  setup.sh --reinstall"))
        return 3

    if not args.skip_install and not args.skip_smoke:
        if not run_smoke_test(conda, plan["env_name"]):
            warn("smoke tests failed — the app may still run, but check "
                 "the failures above before relying on it.")

    print_step_summary()
    print()
    print(_c("32;1", "✓ Setup complete."))

    # ── Plug-and-play launch marker ──────────────────────────────────
    # Write the exact env interpreter to .council_python so the launcher
    # scripts (run-windows.bat / run-wsl.sh) find it WITHOUT needing conda on
    # PATH — this is what wires "setup" straight into "run" as one flow.
    _envpy = None
    if not args.skip_install:
        try:
            _envpy = env_python(conda, plan["env_name"])
            if _envpy is not None:
                (HERE / ".council_python").write_text(
                    str(_envpy) + "\n", encoding="utf-8")
                ok("launch marker written (.council_python)")
        except Exception as _e:
            warn(f"could not write launch marker: {_e}")

    # ── GPU readiness (honest heads-up before first launch) ──────────
    if _envpy is not None:
        try:
            print()
            subprocess.run([str(_envpy), str(HERE / "gpu_check.py")],
                           check=False, timeout=60)
        except Exception:
            pass

    print()
    print("Next steps:")
    _is_win = sys.platform.startswith("win")
    _launcher = "run-windows.bat" if _is_win else "./run-wsl.sh  (or ./run-linux.sh)"
    _need_model = not (plan.get("previous_model")
                       or any(m.get("valid")
                              for m in plan.get("found_models", [])))
    print(f"  1. Launch:  {_launcher}")
    if _need_model:
        print("     On first launch the in-app wizard detects your GPU and")
        print("     lets you download a model sized to your VRAM.")
        print("     (Or pre-download:  huggingface-cli download bartowski/"
              f"{_model_for_tier(plan['recommended'].get('model_tier','small'))}"
              " --local-dir ./models)")
    else:
        print("     The wizard will preselect the model it found.")
    print(f"  Manual alternative:  conda activate {plan['env_name']}"
          " && python council_gui_engine.py")
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
