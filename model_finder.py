"""
model_finder.py — hardware-aware discovery of US-origin GGUF models.

Two layers:

  • OFFLINE (always available): rank the curated `model_catalog` by how
    well each model fits this machine's VRAM/RAM for a requested role.
    This is the authoritative, trustworthy US-only list.

  • ONLINE (optional, best-effort): when the machine has internet AND
    `huggingface_hub` is installed, query the HF Hub for ADDITIONAL
    GGUF models, filter to likely US-origin creators, and rank by fit.
    Any failure (offline, SSL/proxy block, missing dep) degrades
    silently to the catalog — never raises.

Honest caveat on "US-made": Hugging Face has no country-of-origin field,
and GGUF re-packagers (e.g. bartowski) host models from many countries.
So online origin is a HEURISTIC — we match the underlying model name
against an allowlist of US creator orgs and an explicit non-US exclusion
list, and we FLAG online results as origin-unverified. The curated
catalog is the only list guaranteed US-only; online results are
suggestions to verify, not guarantees.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import model_catalog as _cat

# Creator-org fragments matched (case-insensitively) in a model id/name.
US_ORG_FRAGMENTS = (
    "meta-llama", "llama", "microsoft", "phi-", "phi3", "phi4",
    "ibm", "granite", "google", "gemma", "allenai", "olmo",
    "openai", "gpt-oss", "nvidia", "nemotron", "databricks", "dolly",
    "stabilityai",   # US-based (Stability AI Inc.)
)

# Explicit non-US exclusions — dropped even when a US repacker hosts them.
NON_US_FRAGMENTS = (
    "qwen", "alibaba", "deepseek", "mistral", "mixtral", "yi-", "01-ai",
    "01ai", "cohere", "command-r", "command_r", "falcon", "tii",
    "baichuan", "internlm", "glm-", "chatglm", "zhipu", "minimax",
    "stablelm-zephyr-cn", "openbmb", "moonshot", "kimi",
)


def classify_origin(model_id_or_name: str) -> str:
    """Best-effort: 'us' | 'non_us' | 'unknown' for a model id/name.
    Non-US exclusions win over US fragments (so e.g. a US repo hosting a
    Qwen model is correctly flagged non_us)."""
    s = (model_id_or_name or "").lower()
    for frag in NON_US_FRAGMENTS:
        if frag in s:
            return "non_us"
    for frag in US_ORG_FRAGMENTS:
        if frag in s:
            return "us"
    return "unknown"


# ---- VRAM / size estimation ----

def _params_b_from_name(name: str) -> Optional[float]:
    """Pull a parameter count (billions) out of a model name, e.g.
    'Llama-3.1-8B' -> 8.0, '3.2-3B' -> 3.0, 'gpt-oss-20b' -> 20.0,
    '8x7B' -> 56.0 (MoE total). Returns None when unfound."""
    s = (name or "").lower()
    m = re.search(r"(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*b\b", s)   # MoE: AxB
    if m:
        return float(m.group(1)) * float(m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?)\s*b\b", s)
    if m:
        return float(m.group(1))
    return None


def estimate_vram_gb(params_b: float, *, quant: str = "Q4_K_M") -> float:
    """Rough VRAM (GB) to run a model at a quant, with ~1.5 GB headroom
    for the CUDA driver + UI. Q4 ≈ 0.56 GB/B-param of weights; lighter/
    heavier quants scale from there. Deliberately conservative."""
    per_b = {
        "Q2": 0.42, "Q3": 0.48, "Q4": 0.62, "Q5": 0.72,
        "Q6": 0.85, "Q8": 1.10, "F16": 2.1,
    }
    key = "Q4"
    qs = (quant or "Q4").upper()
    for k in per_b:
        if qs.startswith(k):
            key = k
            break
    return round(params_b * per_b[key] + 1.5, 1)


def fits_hardware(vram_gb: Optional[float], ram_gb: Optional[float],
                  needed_vram_gb: float) -> bool:
    """A model fits if it's within VRAM, OR (CPU/low-VRAM fallback) within
    a generous slice of system RAM (llama-cpp runs on CPU when offload
    doesn't fit). RAM path uses half of RAM as a safe ceiling."""
    if vram_gb and needed_vram_gb <= vram_gb:
        return True
    if ram_gb and needed_vram_gb <= ram_gb * 0.5:
        return True
    return False


# ---- catalog ranking (offline, authoritative) ----

def recommend_from_catalog(vram_gb: Optional[float] = None,
                           ram_gb: Optional[float] = None,
                           role: str = "general",
                           limit: int = 5) -> List[Dict[str, Any]]:
    """Rank the curated US-only catalog by fit for this machine + role.
    Always returns SOMETHING runnable: if nothing fits VRAM, the smallest
    models are returned (they'll run on CPU)."""
    role_models = [m for m in _cat.MODELS if m.role == role] or list(_cat.MODELS)
    budget = vram_gb if vram_gb else (ram_gb * 0.5 if ram_gb else 0)

    def _fits(m) -> bool:
        return budget <= 0 or (m.vram_gb_q4 + 1.5) <= budget

    fitting = [m for m in role_models if _fits(m)]
    if fitting:
        # biggest model that fits first (better quality within budget),
        # defaults nudged up.
        ranked = sorted(fitting, key=lambda m: (not m.is_default, -m.params_b))
    else:
        # nothing fits VRAM — smallest first (CPU fallback).
        ranked = sorted(role_models, key=lambda m: m.params_b)
    out = []
    for m in ranked[:limit]:
        out.append({
            "id": m.id, "name": m.name, "org": m.org, "role": m.role,
            "params_b": m.params_b, "quant": m.quant, "size_gb": m.size_gb,
            "context_k": m.context_k, "vram_gb_q4": m.vram_gb_q4,
            "hf_repo": m.hf_repo, "hf_file": m.hf_file,
            "license": m.license, "blurb": m.blurb,
            "origin": "us", "origin_verified": True,
            "fits_vram": bool(vram_gb and (m.vram_gb_q4 + 1.5) <= vram_gb),
            "source": "catalog",
        })
    return out


# ---- online HF discovery (optional, best-effort) ----

def search_huggingface(vram_gb: Optional[float] = None,
                       ram_gb: Optional[float] = None,
                       query: str = "",
                       limit: int = 25,
                       timeout_s: float = 8.0) -> List[Dict[str, Any]]:
    """Query the HF Hub for GGUF models, keep likely-US-origin ones that
    fit, ranked by fit. Returns [] on ANY problem (offline, no dep,
    SSL/proxy block) so the caller falls back to the catalog.

    Results are flagged origin_verified=False — the US-origin call is a
    name-based heuristic, not a guarantee."""
    try:
        from huggingface_hub import HfApi  # type: ignore
    except Exception:
        return []
    try:
        api = HfApi()
        # GGUF library tag + text search; sorted by downloads (proxy for
        # quality/availability of real GGUF repos).
        models = api.list_models(
            filter="gguf", search=(query or None), sort="downloads",
            direction=-1, limit=max(limit * 4, 40),
        )
    except Exception:
        return []

    out: List[Dict[str, Any]] = []
    seen: set = set()
    try:
        for mi in models:
            mid = getattr(mi, "id", None) or getattr(mi, "modelId", "")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            origin = classify_origin(mid)
            if origin == "non_us":
                continue                      # drop known non-US
            params = _params_b_from_name(mid)
            if params is None:
                continue                      # can't size it → skip for fit ranking
            need = estimate_vram_gb(params)
            out.append({
                "id": mid, "name": mid.split("/")[-1],
                "org": mid.split("/")[0],
                "params_b": params,
                "approx_vram_gb": need,
                "fits_vram": bool(vram_gb and need <= vram_gb),
                "fits_hardware": fits_hardware(vram_gb, ram_gb, need),
                "url": f"https://huggingface.co/{mid}",
                "downloads": getattr(mi, "downloads", None),
                "origin": origin,            # 'us' or 'unknown'
                "origin_verified": False,    # heuristic — verify before trusting
                "source": "huggingface",
            })
    except Exception:
        return out  # whatever we gathered before the error

    # Prefer fits + US over unknown + bigger-within-budget.
    out = [m for m in out if m["fits_hardware"]]
    out.sort(key=lambda m: (m["origin"] != "us", not m["fits_vram"],
                            -m["params_b"]))
    return out[:limit]


# ---- top-level entry ----

def find_models(hardware: Optional[Dict[str, Any]] = None,
                role: str = "general",
                prefer_online: bool = True,
                query: str = "") -> Dict[str, Any]:
    """Recommend US-origin models for this machine + role.

    Returns {"hardware": {...}, "catalog": [...], "online": [...],
    "online_available": bool}. The catalog list is always present and
    authoritative; the online list augments it when reachable.
    """
    if hardware is None:
        try:
            import hardware_detect
            hardware = hardware_detect.detect()
        except Exception:
            hardware = {}
    vram = hardware.get("vram_gb")
    ram = hardware.get("ram_gb")

    catalog = recommend_from_catalog(vram, ram, role=role)
    online: List[Dict[str, Any]] = []
    if prefer_online:
        online = search_huggingface(vram, ram, query=query)
    return {
        "hardware": {"vram_gb": vram, "ram_gb": ram,
                     "gpu_name": hardware.get("gpu_name")},
        "role": role,
        "catalog": catalog,
        "online": online,
        "online_available": bool(online),
    }


# ---- upgrade detection ----

# A candidate must be at least this much bigger (params) than the current
# model to count as a real upgrade — avoids nagging over a trivial bump.
_UPGRADE_MIN_GAIN = 1.20


def assess_upgrade(hardware: Optional[Dict[str, Any]] = None,
                   current_model: str = "",
                   current_params_b: Optional[float] = None,
                   role: str = "general",
                   limit: int = 6,
                   min_gain: float = _UPGRADE_MIN_GAIN) -> Dict[str, Any]:
    """Decide whether this machine can run a STRONGER model than the one
    currently loaded, and list catalog upgrades that fit.

    Inputs:
      • hardware           — detect() dict (auto-detected if None).
      • current_model      — loaded model id / filename (used to infer size).
      • current_params_b   — explicit current size in B (overrides the name
                             parse when known).

    Returns:
      {current_params_b, current_vram_gb, budget_gb, headroom_gb,
       can_upgrade, reason, upgrades:[catalog dicts]}.
    A model is an "upgrade" when it FITS the hardware AND is >= min_gain×
    bigger than the current model. When the current size is unknown, the
    best-fitting catalog models are returned but can_upgrade stays False
    (we won't claim an upgrade we can't justify).
    """
    if hardware is None:
        try:
            import hardware_detect
            hardware = hardware_detect.detect()
        except Exception:
            hardware = {}
    vram = hardware.get("vram_gb")
    ram = hardware.get("ram_gb")
    budget = vram if vram else (ram * 0.5 if ram else 0)

    cur_params = current_params_b or _params_b_from_name(current_model or "")
    cur_vram = estimate_vram_gb(cur_params) if cur_params else None
    headroom = round(budget - (cur_vram or 0.0), 1) if budget else None

    # Fitting catalog models for the role (pull a deep list, then keep the
    # ones that genuinely fit the VRAM/RAM budget).
    pool = recommend_from_catalog(vram, ram, role=role, limit=50)

    def _fits(m: Dict[str, Any]) -> bool:
        return budget <= 0 or (m["vram_gb_q4"] + 1.5) <= budget

    fitting = [m for m in pool if _fits(m)]

    if cur_params:
        upgrades = [m for m in fitting
                    if m["params_b"] >= cur_params * min_gain]
    else:
        upgrades = fitting        # unknown current size — just show fits

    upgrades = sorted(upgrades, key=lambda m: -m["params_b"])[:limit]
    can_upgrade = bool(upgrades) and bool(cur_params)

    if not budget:
        reason = ("Couldn't read your VRAM/RAM, so I can't judge headroom. "
                  "Showing models that commonly run on modest hardware.")
    elif can_upgrade:
        big = upgrades[0]
        reason = (f"Your hardware has room (~{headroom} GB free over the "
                  f"current ~{cur_params:g}B model) — it can run a stronger "
                  f"model, up to {big['name']} ({big['params_b']:g}B).")
    elif cur_params and not upgrades:
        reason = (f"You're already running a model (~{cur_params:g}B) at or "
                  "above the best fit for your hardware — no stronger model "
                  "fits comfortably.")
    elif not cur_params:
        reason = ("Couldn't tell the current model's size from its name, so "
                  "I can't confirm an upgrade — here are the best fits for "
                  "your hardware to compare.")
    else:
        reason = "No clear upgrade found for your hardware."

    return {
        "current_model": current_model,
        "current_params_b": cur_params,
        "current_vram_gb": cur_vram,
        "budget_gb": round(budget, 1) if budget else None,
        "headroom_gb": headroom,
        "can_upgrade": can_upgrade,
        "reason": reason,
        "upgrades": upgrades,
    }
