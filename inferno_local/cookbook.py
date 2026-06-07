"""
inferno_local.cookbook — hardware-aware local model fit helper.

A thin layer over the **vendored** ``model_catalog.MODELS`` list. There
is no live download of a 270-row recipe table — everything ships in
``model_catalog.py`` and we cross-reference what's on disk in the local
models folder against it.

Public surface:

    describe() -> dict
        Hardware snapshot: cpu, ram_gb, gpu(s), cuda_runtime, models_dir
        contents (filename → catalog spec when matched).

    best_quant(model_id, *, vram_gb=None) -> dict
        For a catalog model id, returns the recommended quant for the
        given (or auto-detected) VRAM budget. The vendored catalog only
        carries one quant per model so this is mostly informational —
        but keeps the call site stable if we expand quants later.

    fit_verdict(spec, hw) -> dict
        "fits cleanly" | "tight, expect swap" | "won't fit".

    scan_models_dir(path) -> list[(filename, ModelSpec|None)]
        Cross-reference a folder of .gguf files against MODELS.

No network calls. Honours ``COUNCIL_MODELS_DIR`` to override the default
``./models`` location.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# model_catalog is the single source of truth for the curated US-origin
# GGUF list. Importing it here keeps cookbook from drifting.
try:
    import model_catalog as _mc
    _HAVE_CATALOG = True
except Exception:
    _mc = None  # type: ignore[assignment]
    _HAVE_CATALOG = False


# ============================================================
# Hardware probe
# ============================================================

def _ram_gb() -> Optional[float]:
    """Best-effort total RAM in GB. Falls back to None if psutil missing."""
    try:
        import psutil  # type: ignore
    except Exception:
        # On Linux/WSL we can read /proc/meminfo without a dep
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return round(kb / (1024 ** 2), 1)
        except Exception:
            return None
        return None
    return round(psutil.virtual_memory().total / (1024 ** 3), 1)


def _gpu_info() -> List[Dict[str, Any]]:
    """Returns one dict per NVIDIA GPU. Empty list when no nvidia-smi."""
    if shutil.which("nvidia-smi") is None:
        return []
    try:
        out = subprocess.check_output(
            ["nvidia-smi",
             "--query-gpu=name,memory.total,memory.free,driver_version",
             "--format=csv,noheader"],
            text=True, timeout=10,
        ).strip()
    except Exception:
        return []
    gpus: List[Dict[str, Any]] = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        m_total = re.search(r"(\d+(?:\.\d+)?)", parts[1])
        m_free  = re.search(r"(\d+(?:\.\d+)?)", parts[2])
        gpus.append({
            "name":     parts[0],
            "vram_gb_total": round(float(m_total.group(1)) / 1024, 1) if m_total else None,
            "vram_gb_free":  round(float(m_free.group(1))  / 1024, 1) if m_free  else None,
            "driver":   parts[3],
        })
    return gpus


def _cuda_runtime() -> Optional[str]:
    """CUDA Version as reported by nvidia-smi (driver max-supported)."""
    if shutil.which("nvidia-smi") is None:
        return None
    try:
        out = subprocess.check_output(["nvidia-smi"], text=True, timeout=10)
        m = re.search(r"CUDA Version: (\d+\.\d+)", out)
        return m.group(1) if m else None
    except Exception:
        return None


def _models_dir() -> Path:
    p = os.environ.get("COUNCIL_MODELS_DIR", "").strip()
    if p:
        return Path(p).expanduser().resolve()
    return (Path(__file__).resolve().parent.parent / "models")


# ============================================================
# Public API
# ============================================================

def describe() -> Dict[str, Any]:
    """Hardware + on-disk model snapshot. Pure read; no network."""
    gpus = _gpu_info()
    return {
        "os":           platform.system(),
        "os_release":   platform.release(),
        "python":       platform.python_version(),
        "cpu":          platform.processor() or platform.machine(),
        "ram_gb":       _ram_gb(),
        "gpus":         gpus,
        "cuda_runtime": _cuda_runtime(),
        "models_dir":   str(_models_dir()),
        "models_on_disk": scan_models_dir(_models_dir()),
        "catalog_size": (len(_mc.MODELS) if _HAVE_CATALOG else 0),
    }


def primary_vram_gb() -> Optional[float]:
    """Total VRAM of the first NVIDIA GPU, or None if no GPU."""
    gpus = _gpu_info()
    return gpus[0]["vram_gb_total"] if gpus else None


def fit_verdict(spec, hw_vram_gb: Optional[float]) -> Dict[str, Any]:
    """Verdict for one ModelSpec against an available-VRAM number.

    Returns ``{"fit": "clean"|"tight"|"oom", "reason": "..."}``.
    """
    if hw_vram_gb is None:
        return {"fit": "cpu-only",
                "reason": "no GPU detected — will run on CPU, expect slow inference."}
    need = float(spec.vram_gb_q4)
    head = 1.5  # headroom for CUDA driver + Tk UI
    if need + head <= hw_vram_gb:
        return {"fit": "clean",
                "reason": f"needs ~{need:.1f} GB + {head} GB headroom; you have {hw_vram_gb:.1f} GB."}
    if need <= hw_vram_gb:
        return {"fit": "tight",
                "reason": f"needs ~{need:.1f} GB; you have {hw_vram_gb:.1f} GB — fits without margin, expect occasional OOM."}
    return {"fit": "oom",
            "reason": f"needs ~{need:.1f} GB but only {hw_vram_gb:.1f} GB available."}


def best_quant(model_id: str, *, vram_gb: Optional[float] = None) -> Dict[str, Any]:
    """Best (only) quant for the given catalog model id at this VRAM
    budget. Returns the verdict alongside the spec so the Settings panel
    can render both."""
    if not _HAVE_CATALOG:
        return {"error": "model_catalog not importable"}
    spec = _mc.by_id(model_id)
    if spec is None:
        return {"error": f"unknown model id: {model_id!r}"}
    hw_vram = vram_gb if vram_gb is not None else primary_vram_gb()
    return {
        "model_id": spec.id,
        "name":     spec.name,
        "quant":    spec.quant,
        "size_gb":  spec.size_gb,
        "verdict":  fit_verdict(spec, hw_vram),
    }


def scan_models_dir(path: Path) -> List[Dict[str, Any]]:
    """Walk a folder of .gguf files and match each filename against the
    catalog. Unmatched files still appear so the user sees their own
    sideloaded models. NO download lookup — pure local cross-reference."""
    p = Path(path)
    if not p.exists():
        return []
    out: List[Dict[str, Any]] = []
    catalog_by_file = (
        {m.hf_file: m for m in _mc.MODELS} if _HAVE_CATALOG else {}
    )
    for f in sorted(p.glob("*.gguf")):
        spec = catalog_by_file.get(f.name)
        item = {
            "filename": f.name,
            "size_gb":  round(f.stat().st_size / (1024 ** 3), 2),
            "matched":  bool(spec),
        }
        if spec is not None:
            item["model_id"] = spec.id
            item["org"]      = spec.org
            item["license"]  = spec.license
        out.append(item)
    return out


if __name__ == "__main__":
    # Diagnostic — `python -m inferno_local.cookbook`
    import json
    print(json.dumps(describe(), indent=2, default=str))
