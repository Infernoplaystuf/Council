"""
system_panel.py — the hardware survey and model fit, as data and as text.

``snapshot()`` takes the hardware survey from ``inferno_local.cookbook`` and
gives each catalog model a fit verdict (clean, tight, oom, cpu-only), so a
user can see which models will run comfortably on their machine. Run it
standalone for the text dump:

    python system_panel.py

(The Tk "System & Models" window that drew it was retired with the Tk shell,
2026-10-23.)
"""
from __future__ import annotations

from typing import Any, Dict, List

from inferno_local import cookbook
import model_catalog


# ============================================================
# Pure-data snapshot (used by GUI panel + CLI dump)
# ============================================================

def snapshot() -> Dict[str, Any]:
    """Return everything the panel renders in a single dict.

    Side-effect free apart from probing the local machine. No model
    download, no network call. Safe to call from a worker thread."""
    hw = cookbook.describe()
    vram = cookbook.primary_vram_gb()
    fits = []
    for spec in model_catalog.MODELS:
        fits.append({
            "model_id": spec.id,
            "name":     spec.name,
            "org":      spec.org,
            "size_gb":  spec.size_gb,
            "vram_gb_q4": spec.vram_gb_q4,
            "license":  spec.license,
            "verdict":  cookbook.fit_verdict(spec, vram),
            "is_default": spec.is_default,
        })
    return {"hardware": hw, "models": fits, "primary_vram_gb": vram}


# ============================================================
# CLI mode
# ============================================================

def render_cli(snap: Dict[str, Any]) -> str:
    hw = snap["hardware"]
    lines: List[str] = []
    lines.append("─── Hardware ─────────────────────────────────────")
    lines.append(f"  OS:           {hw['os']} {hw['os_release']}")
    lines.append(f"  Python:       {hw['python']}")
    lines.append(f"  CPU:          {hw['cpu']}")
    lines.append(f"  RAM:          {hw['ram_gb']} GB")
    if hw["gpus"]:
        for i, g in enumerate(hw["gpus"]):
            lines.append(f"  GPU {i}:        {g['name']}  "
                         f"{g['vram_gb_total']} GB total / "
                         f"{g['vram_gb_free']} GB free  "
                         f"(driver {g['driver']})")
    else:
        lines.append("  GPU:          (no nvidia-smi — CPU-only)")
    lines.append(f"  CUDA runtime: {hw['cuda_runtime']}")
    lines.append(f"  Models dir:   {hw['models_dir']}")
    if hw["models_on_disk"]:
        for m in hw["models_on_disk"]:
            tag = "(catalog)" if m["matched"] else "(sideload)"
            lines.append(f"    • {m['filename']:<48s} {m['size_gb']:6.2f} GB  {tag}")
    else:
        lines.append("    (no .gguf files in models dir)")
    lines.append("")
    lines.append("─── Catalog (US-origin) ──────────────────────────")
    for m in snap["models"]:
        verdict = m["verdict"]
        emoji = {"clean": "✓", "tight": "⚠", "oom": "✗", "cpu-only": "·"}[verdict["fit"]]
        star = " ★" if m["is_default"] else "  "
        lines.append(f"  {emoji}{star} {m['name']:<40s} "
                     f"{m['size_gb']:5.1f} GB  "
                     f"~{m['vram_gb_q4']:4.1f} GB VRAM  "
                     f"{m['license']}")
        lines.append(f"        {verdict['reason']}")
    return "\n".join(lines)


# ============================================================
# Standalone CLI entry
# ============================================================

if __name__ == "__main__":
    snap = snapshot()
    print(render_cli(snap))
