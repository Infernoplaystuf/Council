"""
council_core.build_models — which model the Build tab's chat and agent use.

They answer as the "coder" role (council_core.model_slots), so the choice is
the coder role's model, and the Models tab shows the same choice.

    choices      the INSTALLED models ranked for code on this PC
                 (local_models.rank_for_role: US-origin only, a model that
                 fits the GPU memory before a bigger one that spills to RAM,
                 a short context marked down); the first is the
                 recommendation.
    to_get       when nothing installed fits the card well: the catalog's
                 US models good for code that fit the detected GPU memory,
                 each with the command that fetches it. The Council never
                 downloads a model itself; the user runs the command.
    with_coder   a copy of the slot map with the coder role on the chosen
                 model (its own slot, or the one already holding it); the
                 other roles keep theirs.
    save_coder   writes it (atomically, model_slots.save) and tells the
                 engine to reload its map; it takes effect on the next
                 question.

Hardware (``memory``) and the model list are BLOCKING: call them on a worker.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import local_models, model_slots

ROLE = "coder"
GB = model_slots.GB


@dataclass
class Choice:
    id: str              # a GGUF path or "ollama:<name>"; "" = the main model
    label: str
    why: str = ""
    recommended: bool = False
    fit: str = ""


def memory() -> Tuple[Optional[float], Optional[float]]:
    """(total GPU memory GB, RAM GB). BLOCKING (nvidia-smi once)."""
    try:
        from . import model_jobs
        hw = getattr(model_jobs, "_DETECTED", None)
        if hw is not None and (hw.vram_gb or hw.ram_gb):
            return hw.vram_gb, hw.ram_gb
    except Exception:                                     # noqa: BLE001
        pass
    try:
        import hardware_detect
        return hardware_detect.quick_memory()
    except Exception:                                     # noqa: BLE001
        return None, None


def main_path(vault_dir: Path) -> str:
    env = os.environ.get("COUNCIL_GGUF_PATH", "").strip()
    if env:
        return env
    try:
        from . import backend_settings
        return backend_settings.load_gguf_path(vault_dir) or ""
    except Exception:                                     # noqa: BLE001
        return ""


def installed(vault_dir: Path) -> List[Dict[str, Any]]:
    """Every model this PC can run (GGUF files and localhost Ollama's).
    BLOCKING."""
    cfg = model_slots.load(vault_dir)
    also = [main_path(vault_dir)] + [s.path for s in cfg.slots.values()]
    return local_models.list_local_models(also=[a for a in also if a])


def choices(models: Sequence[Dict[str, Any]], vram_gb: Optional[float],
            ram_gb: Optional[float]) -> List[Choice]:
    ranked = local_models.rank_for_role(models, ROLE, vram_gb=vram_gb,
                                        ram_gb=ram_gb)
    out = []
    for i, e in enumerate(ranked):
        size = (e.get("size_bytes") or 0) / GB
        label = str(e.get("name") or e.get("id"))
        if size:
            label += f" · {size:.1f} GB"
        out.append(Choice(str(e["id"]), label, e.get("why", ""),
                          recommended=(i == 0), fit=e.get("fit", "")))
    return out


def to_get(vram_gb: Optional[float], have: Sequence[str] = ()) -> List[str]:
    """Lines naming the catalog's code models that fit ``vram_gb`` and are
    not installed, best first, each with the command that fetches it."""
    if not vram_gb:
        return []
    try:
        import model_catalog
    except Exception:                                     # noqa: BLE001
        return []
    have_keys = {_key(h) for h in have}
    out = []
    for spec in model_catalog.for_vram(vram_gb, role="code"):
        if spec.ollama and _key(local_models.ollama_id(spec.ollama)) in have_keys:
            continue
        if any(Path(h).name.lower() == spec.hf_file.lower() for h in have
               if not model_slots.is_ollama(h)):
            continue
        how = (f"ollama pull {spec.ollama}" if spec.ollama else
               f"download {spec.hf_file} from huggingface.co/{spec.hf_repo}")
        out.append(f"{spec.name} (about {spec.vram_gb_q4:.0f} GB): {how}")
    return out


def _key(path: str) -> str:
    return model_slots._key(path)                         # noqa: SLF001


def current(config: model_slots.SlotConfig, main: str = "") -> str:
    """The coder role's model: "" when it answers from the main model."""
    name = config.slot_for(ROLE)
    if name == model_slots.MAIN:
        return ""
    return config.slots[name].resolved_path(main)


def with_coder(config: model_slots.SlotConfig, model_id: str,
               main: str = "") -> model_slots.SlotConfig:
    """A copy of ``config`` with the coder role on ``model_id`` ("" = the
    main model). A slot only the coder used before is dropped; every other
    role keeps its model."""
    slots = {n: replace(s) for n, s in config.slots.items()}
    roles = dict(config.roles)
    old = roles.pop(ROLE, None)
    main_key = _key(slots[model_slots.MAIN].resolved_path(main) or "")
    if model_id and _key(model_id) != main_key:
        hit = next((n for n, s in slots.items()
                    if n != model_slots.MAIN and _key(s.path) == _key(model_id)),
                   None)
        if hit is None:
            stem = (model_id[len(model_slots.OLLAMA_PREFIX):]
                    if model_slots.is_ollama(model_id) else Path(model_id).stem)
            hit = model_slots._slot_name(stem, slots)    # noqa: SLF001
            slots[hit] = model_slots.Slot(hit, model_id)
        roles[ROLE] = hit
    if old and old != roles.get(ROLE) and old != model_slots.MAIN \
            and old not in roles.values():
        slots.pop(old, None)
    return model_slots.SlotConfig(slots, roles)


def save_coder(vault_dir: Path, model_id: str) -> str:
    """Put the coder role on ``model_id`` and reload the engine's map.
    Returns the status line."""
    trouble = model_slots.problem(vault_dir)
    if trouble:                     # never write over a file it cannot read
        return f"Not changed: {trouble} Fix it in the Models tab."
    main = main_path(vault_dir)
    cfg = with_coder(model_slots.load(vault_dir), model_id, main)
    model_slots.save(vault_dir, cfg)
    try:
        import council_engine
        council_engine.refresh_backend_config()
    except Exception as exc:                              # noqa: BLE001
        return f"Saved, but the engine could not reload ({exc}); restart the app to use it."
    return "The chat and the agent use it from the next question."


__all__ = ["Choice", "memory", "installed", "choices", "to_get", "current",
           "with_coder", "save_coder", "main_path", "ROLE"]
