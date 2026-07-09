#!/usr/bin/env python3
"""
gpu_check.py — a quick, honest report of whether GPU inference will ACTUALLY
work for this app, so a misconfigured box says so loudly instead of silently
falling back to (slow) CPU.

Run:
    python gpu_check.py           # full report
    python gpu_check.py --quiet   # just the one-line verdict

Exit code: 0 if GPU offload is available, 1 otherwise. Never hangs (all
subprocess calls are time-bounded); safe to run on any machine.

Checks:
  1. NVIDIA driver present (nvidia-smi) + the GPU(s) and driver's CUDA version.
  2. llama-cpp-python installed AND built with GPU offload support — this is the
     piece that decides whether the model runs on the GPU. A plain
     `pip install llama-cpp-python` is CPU-only; setup.bat installs the CUDA one.
  3. torch CUDA availability (informational — embeddings can use it).
"""
from __future__ import annotations

import shutil
import subprocess
import sys


def _nvidia_smi():
    """Return a list of 'name, driver, memory' strings, [] if smi ran but found
    nothing, or None if nvidia-smi isn't installed / on PATH."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        r = subprocess.run(
            [exe, "--query-gpu=name,driver_version,memory.total",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
    except Exception:
        pass
    return []


def _llama_gpu():
    """(supports_gpu_offload | None, note). None means unknown/uninstalled."""
    try:
        import llama_cpp
    except Exception as exc:
        return None, f"not importable ({exc!r})"
    ver = getattr(llama_cpp, "__version__", "?")
    fn = getattr(llama_cpp, "llama_supports_gpu_offload", None)
    if fn is None:
        return None, f"v{ver} (too old to report GPU support)"
    try:
        return bool(fn()), f"v{ver}"
    except Exception as exc:
        return None, f"v{ver} (offload check failed: {exc!r})"


def _torch_cuda():
    try:
        import torch
        return bool(torch.cuda.is_available()), getattr(torch.version, "cuda", None)
    except Exception:
        return None, None


def main() -> int:
    quiet = "--quiet" in sys.argv
    gpus = _nvidia_smi()
    llama_gpu, llama_note = _llama_gpu()
    torch_cuda, torch_cudaver = _torch_cuda()

    if not quiet:
        print("-- GPU readiness check --------------------------------")
        if gpus is None:
            print("  GPU driver : nvidia-smi NOT found (no NVIDIA driver, or "
                  "not on PATH)")
        elif not gpus:
            print("  GPU driver : nvidia-smi present but reported no GPU")
        else:
            for g in gpus:
                print(f"  GPU        : {g}")
        _off = ("GPU offload: YES" if llama_gpu is True
                else "GPU offload: NO" if llama_gpu is False
                else "GPU offload: unknown")
        print(f"  llama-cpp  : {llama_note}   [{_off}]")
        if torch_cuda is not None:
            print("  torch CUDA : "
                  + ("available" if torch_cuda else "NOT available")
                  + (f" (cuda {torch_cudaver})" if torch_cudaver else ""))

    ready = bool(gpus) and llama_gpu is True
    if ready:
        print("  VERDICT    : OK - GPU offload available; the model will run "
              "on the GPU.")
        return 0

    # Not ready — surface the single most useful next step.
    if not gpus:
        print("  VERDICT    : No usable NVIDIA GPU detected. The app runs on "
              "CPU (slower). Install/repair the NVIDIA driver, then re-check.")
    elif llama_gpu is False:
        print("  VERDICT    : llama-cpp-python is a CPU-ONLY build. Re-run "
              "setup (setup.bat) to install the CUDA wheel for your GPU.")
    else:
        print("  VERDICT    : GPU offload not confirmed. Re-run setup.bat; if "
              "it persists, check `nvidia-smi` CUDA version vs the wheel tier.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
