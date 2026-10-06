"""
council_core.engine_settings — the engine knobs saved in the vault, put in
force when the app starts. One rule, both front ends.

WHAT WAS MISSING
The Tk Engine settings dialog saves three knobs into
vault/backend_settings.json — "n_ctx", "gpu_layers", "embed_device" — and the
Tk console copies them into the environment when it starts
(`_load_backend_settings`), which is where the engine reads them, at model
load: COUNCIL_GGUF_N_CTX, COUNCIL_GGUF_GPU_LAYERS, COUNCIL_EMBED_DEVICE. The
Qt app never did, so a context size saved in Tk was silently dropped the
moment the same vault was opened in Qt: the model loaded with the ladder's
own window instead, and nothing said so.

PRECEDENCE: THE ENVIRONMENT WINS, AS IN TK
A value already in the environment — a shell export, a launcher — beats the
saved one. Kept exactly, because a launch-time override is how a user gets out
of a saved setting that crashes the GPU. One consequence worth knowing: the
run-* launchers export COUNCIL_GGUF_GPU_LAYERS=99 and COUNCIL_EMBED_DEVICE=cpu
when unset, so under a launcher a SAVED gpu_layers / embed_device never
applies, in either front end. A saved n_ctx does (no launcher sets it).

TIMING
The engine reads these when it LOADS a model, not at import, so any time
before the first question is early enough. council_qt.launch applies them
before the window — and so before any tab can build personalities.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Dict, MutableMapping, Optional, Tuple

#: (key in backend_settings.json, the environment variable the engine reads).
KNOBS: Tuple[Tuple[str, str], ...] = (
    ("n_ctx", "COUNCIL_GGUF_N_CTX"),
    ("gpu_layers", "COUNCIL_GGUF_GPU_LAYERS"),
    ("embed_device", "COUNCIL_EMBED_DEVICE"),
)


def apply(settings: Optional[dict],
          environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    """Put the saved knobs into ``environ`` (default os.environ) where it has
    none of its own. Returns {variable: value} for what was applied.

    An empty saved value means "not set" — the Engine dialog saves "" for a
    field left blank — and is never applied over anything.
    """
    env = os.environ if environ is None else environ
    applied: Dict[str, str] = {}
    for key, var in KNOBS:
        value = (settings or {}).get(key)
        if value in (None, "") or str(env.get(var, "") or "").strip():
            continue
        env[var] = str(value)
        applied[var] = str(value)
    return applied


def apply_saved(vault_dir=None,
                environ: Optional[MutableMapping[str, str]] = None,
                log: Optional[Callable[[str], None]] = None) -> Dict[str, str]:
    """Read vault/backend_settings.json and `apply` it. Never raises: a launch
    must not die over a context size.

    Read through onboarding's reader, the one the main-model setting already
    uses, rather than a second json.loads: it accepts the BOM PowerShell 5.1
    writes and an empty file, and says "unreadable" rather than "empty" for a
    damaged one (MEASURED 2026-09-29 in onboarding._read_backend_settings).
    """
    try:
        if vault_dir is None:
            from . import paths
            vault_dir = paths.vault_dir()
        import onboarding
        settings = onboarding._read_backend_settings(Path(vault_dir))
        if settings is None:
            if log:
                log(f"[startup] engine settings: could not read "
                    f"{Path(vault_dir) / 'backend_settings.json'}; "
                    "using the defaults.")
            return {}
        applied = apply(settings, environ)
    except Exception as exc:                              # noqa: BLE001
        if log:
            log(f"[startup] engine settings not applied: {exc!r}")
        return {}
    if applied and log:
        log("[startup] engine settings from the vault: "
            + ", ".join(f"{k}={v}" for k, v in applied.items()))
    return applied
