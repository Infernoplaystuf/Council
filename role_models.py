"""
role_models.py — per-role specialist GGUF assignment + GPU-gated swap.

The app runs ONE GGUF at a time, so "specialist models per task" is
implemented as SWAPPING the loaded model when a task phase wants a
different specialist (e.g. a code model for the IDE path, a reasoning
model for analysis). Two deliberate guardrails:

  • GPU-gated. Swapping reloads the model (~seconds). On a CPU-only run
    that latency is pure loss on top of already-slow inference, so
    swap_to_role() is a NO-OP unless GPU offload is actually enabled.
    (Override with COUNCIL_ROLE_SWAP=1/0.)

  • No redundant swaps. A swap only happens when the role's assigned
    model differs from the one currently loaded; re-asking for the same
    role is free.

Assignments persist in <vault>/backend_settings.json under "role_models"
({role: gguf_path}), alongside the existing gguf_path / clip_path keys.
An unassigned role falls back to the base COUNCIL_GGUF_PATH model.

For TRUE parallel specialists (no swap latency), the remote-node
framework on the main / odysseus branches is the better path — run the
analyst model on another machine. That's a separate integration; this
module only covers local single-GPU swapping.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

_BACKEND_SETTINGS = "backend_settings.json"
_ROLE_KEY = "role_models"

# Tracks which model path is currently loaded so we can skip a redundant
# reload. Initialised lazily to the base COUNCIL_GGUF_PATH.
_LOADED_PATH: Optional[str] = None
_LOCK = threading.Lock()


def _vault_root() -> Path:
    env = os.environ.get("COUNCIL_VAULT_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".council" / "vault"


def gpu_swap_enabled() -> bool:
    """Whether per-role model swapping is allowed right now.

    Explicit override wins (COUNCIL_ROLE_SWAP=1/0). Otherwise auto: only
    when GPU offload is on (COUNCIL_GGUF_GPU_LAYERS > 0) AND a CUDA
    device is actually present — i.e. the user is "running with GPU
    usage enabled", which is the only place a multi-second model reload
    per phase is worth paying."""
    ov = os.environ.get("COUNCIL_ROLE_SWAP", "").strip().lower()
    if ov in ("1", "true", "yes", "on"):
        return True
    if ov in ("0", "false", "no", "off"):
        return False
    try:
        layers = int(os.environ.get("COUNCIL_GGUF_GPU_LAYERS", "99"))
    except ValueError:
        layers = 99
    if layers <= 0:
        return False
    try:
        import torch  # type: ignore
        return bool(torch.cuda.is_available())
    except Exception:
        return False


# ============================================================
# Assignment registry (persisted in backend_settings.json)
# ============================================================

class RoleModelRegistry:
    """Read/write the {role: gguf_path} map in backend_settings.json,
    merging so the existing gguf_path / clip_path keys are preserved."""

    def __init__(self, vault_dir: Optional[Any] = None) -> None:
        root = Path(vault_dir) if vault_dir is not None else _vault_root()
        self.path = root / _BACKEND_SETTINGS

    def _read(self) -> Dict[str, Any]:
        try:
            if self.path.exists():
                d = json.loads(self.path.read_text(encoding="utf-8"))
                return d if isinstance(d, dict) else {}
        except Exception:
            pass
        return {}

    def all(self) -> Dict[str, str]:
        m = self._read().get(_ROLE_KEY)
        return dict(m) if isinstance(m, dict) else {}

    def get(self, role: str) -> Optional[str]:
        return self.all().get(str(role))

    def set(self, role: str, gguf_path: str) -> None:
        data = self._read()
        roles = data.get(_ROLE_KEY)
        if not isinstance(roles, dict):
            roles = {}
        roles[str(role)] = str(gguf_path)
        data[_ROLE_KEY] = roles
        self._write(data)

    def remove(self, role: str) -> bool:
        data = self._read()
        roles = data.get(_ROLE_KEY)
        if isinstance(roles, dict) and str(role) in roles:
            del roles[str(role)]
            data[_ROLE_KEY] = roles
            self._write(data)
            return True
        return False

    def _write(self, data: Dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            pass


# ============================================================
# Resolution + swap
# ============================================================

def _base_model_path() -> str:
    return os.environ.get("COUNCIL_GGUF_PATH", "").strip()


def resolve_model_for_role(role: str,
                           vault_dir: Optional[Any] = None) -> str:
    """The model assigned to ``role``, or the base COUNCIL_GGUF_PATH when
    the role has no specialist assigned."""
    assigned = RoleModelRegistry(vault_dir).get(role)
    if assigned and Path(assigned).is_file():
        return assigned
    return _base_model_path()


def current_loaded_path() -> str:
    global _LOADED_PATH
    if _LOADED_PATH is None:
        _LOADED_PATH = _base_model_path()
    return _LOADED_PATH or ""


def swap_to_role(role: str, vault_dir: Optional[Any] = None) -> Dict[str, Any]:
    """Swap the loaded GGUF to the model assigned to ``role``.

    No-op (without touching the engine) when GPU swapping is disabled or
    the role's model is already loaded. Returns a dict describing what
    happened: {"swapped": bool, "reason": str, "model": path}.
    """
    global _LOADED_PATH
    target = resolve_model_for_role(role, vault_dir)
    if not target:
        return {"swapped": False, "reason": "no-model", "model": ""}
    if not gpu_swap_enabled():
        return {"swapped": False, "reason": "gpu-disabled", "model": current_loaded_path()}
    with _LOCK:
        if current_loaded_path() == target:
            return {"swapped": False, "reason": "already-loaded", "model": target}
        # Point the engine at the role's model and reset its singleton so
        # the next inference loads it.
        os.environ["COUNCIL_GGUF_PATH"] = target
        try:
            import council_engine
            council_engine.refresh_backend_config()
        except Exception as exc:
            return {"swapped": False, "reason": f"reload-failed: {exc!r}",
                    "model": current_loaded_path()}
        _LOADED_PATH = target
        return {"swapped": True, "reason": "ok", "model": target}


def restore_base(base_path: Optional[str] = None,
                 vault_dir: Optional[Any] = None) -> Dict[str, Any]:
    """Swap back to the base model (the one COUNCIL_GGUF_PATH pointed at
    before any role swap). Pass the base path explicitly if the env var
    has since been changed."""
    global _LOADED_PATH
    target = (base_path or _base_model_path()).strip()
    if not target or not gpu_swap_enabled():
        return {"swapped": False, "reason": "gpu-disabled-or-no-base",
                "model": current_loaded_path()}
    with _LOCK:
        if current_loaded_path() == target:
            return {"swapped": False, "reason": "already-loaded", "model": target}
        os.environ["COUNCIL_GGUF_PATH"] = target
        try:
            import council_engine
            council_engine.refresh_backend_config()
        except Exception as exc:
            return {"swapped": False, "reason": f"reload-failed: {exc!r}",
                    "model": current_loaded_path()}
        _LOADED_PATH = target
        return {"swapped": True, "reason": "ok", "model": target}
