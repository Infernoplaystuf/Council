"""
council_core.model_ready — can this launch answer a question at all?

WHAT THIS REPLACES
The Qt startup's "Setup needed" notice asked `onboarding.needs_onboarding`,
which looks only for `vault/.onboarded` — a marker the Tk wizard writes and
nothing else does. setup.bat / setup_council.py never write it, so a user who
installed through them, or who started with the Qt app, was told "Setup
needed" with a working model configured. The other way round, a vault that
HAS the marker but no model any more (a deleted GGUF, an Ollama model
removed) got no notice at all, and every answer was "no judge model is
loaded".

The notice exists to say "nothing can answer yet". So that is the question
asked here — the way the engine would find a model, without importing the
engine (seconds) and without loading or running anything:

  1. Each model slot in model_slots.json ("main" follows the main model: the
     COUNCIL_GGUF_PATH a user exported, else the one saved in the app, else
     the launcher's pick — onboarding.load_gguf_path): a GGUF file that is on
     disk AND a llama-cpp-python to load it, or an "ollama:<name>" model the
     local Ollama has.
  2. Otherwise the engine's own fallback (council_engine._route_chat): with
     COUNCIL_BACKEND=ollama, or COUNCIL_OLLAMA_FALLBACK not switched off, an
     installed Ollama model the engine would actually pick — COUNCIL_OLLAMA_MODEL
     when set, else a US-origin chat model (the engine never picks another).

The Tk wizard still decides on the marker (onboarding.run_if_needed): that is
"has this person been walked through setup", a different question.

ONLY METADATA, ONLY LOOPBACK
Ollama is asked /api/version and /api/tags through local_models, which refuses
a non-loopback host — never a generation — and only when no GGUF slot already
answers the question.
"""
from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

#: council_engine._OFF_VALUES — what switches the Ollama fallback off.
_OFF_VALUES = ("0", "false", "no", "off")


@dataclass
class Readiness:
    """Whether a model can answer, and if not, why — in words for the notice."""
    usable: bool
    #: Why nothing can answer ("" when something can). Shown to the user.
    reason: str = ""
    #: What would answer: a GGUF path or "ollama:<name>". For the log.
    model: str = ""


def gguf_loader_available() -> bool:
    """Whether llama-cpp-python is importable here — found, not imported.

    A GGUF file nothing can load is not a usable model: measured 2026-10-01,
    the council conda env has no llama_cpp at all, and on that PC every GGUF
    answer comes from the Ollama fallback or not at all.
    """
    try:
        return importlib.util.find_spec("llama_cpp") is not None
    except Exception:                                     # noqa: BLE001
        return False


def _off(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _OFF_VALUES


def _main_model(vault: Path) -> str:
    try:
        import onboarding
        return onboarding.load_gguf_path(vault) or ""
    except Exception:                                     # noqa: BLE001
        return os.environ.get("COUNCIL_GGUF_PATH", "").strip()


def check(vault_dir, *, loader_available: Optional[bool] = None) -> Readiness:
    """Whether any model can answer, by the engine's own rules. Never raises
    for an ordinary reason — a missing file, no server, a damaged slots file
    (model_slots.load falls back to one model, as the engine does)."""
    from . import local_models, model_slots

    vault = Path(vault_dir)
    config = model_slots.load(vault)
    main = _main_model(vault)
    to_ollama = os.environ.get("COUNCIL_BACKEND", "").strip().lower() == \
        "ollama"
    can_load = (gguf_loader_available() if loader_available is None
                else bool(loader_available))

    problems: List[str] = []
    wanted: List[str] = []
    for slot in config.slots.values():
        path = slot.resolved_path(main).strip()
        if not path:
            continue
        if model_slots.is_ollama(path):
            wanted.append(local_models.ollama_name(path))
            continue
        if to_ollama:
            continue                 # the engine sends this slot to Ollama
        if not os.path.isfile(path):
            problems.append(f"the model file {path} is not on disk")
            continue
        if not can_load:
            problems.append(f"{Path(path).name} is set, but llama-cpp-python "
                            "is not installed in this Python, so it cannot "
                            "be loaded")
            continue
        return Readiness(True, model=path)

    fallback = to_ollama or not _off("COUNCIL_OLLAMA_FALLBACK")
    if wanted or fallback:
        found = _ollama(wanted, fallback, problems)
        if found:
            return Readiness(True, model=found)

    reason = "; ".join(dict.fromkeys(problems)) or "no model is configured yet"
    return Readiness(False, reason=reason)


def _ollama(wanted: List[str], fallback: bool, problems: List[str]) -> str:
    """The Ollama model that would answer, as "ollama:<name>", or "".
    Adds to ``problems`` what stood in the way."""
    from . import local_models

    host = local_models.ollama_host()
    try:
        tags = (local_models.ollama_tags(host)
                if local_models.ollama_reachable(host) else None)
    except RuntimeError:
        tags = None                  # not loopback: never asked
    if tags is None:
        if wanted:
            problems.append(f"no Ollama server answers at {host}")
        return ""

    def installed(name: str) -> bool:
        return local_models.ollama_model(name, host,
                                         fill_from_show=False) is not None

    for name in wanted:
        if installed(name):
            return local_models.ollama_id(name)
        problems.append(f"Ollama at {host} does not have {name}")
    if not fallback:
        return ""
    named = os.environ.get("COUNCIL_OLLAMA_MODEL", "").strip()
    if named:
        if installed(named):
            return local_models.ollama_id(named)
        problems.append(f"Ollama at {host} does not have {named} "
                        "(COUNCIL_OLLAMA_MODEL)")
        return ""
    entries = [local_models.ollama_entry(t, host=host, fill_from_show=False)
               for t in tags]
    best = local_models.recommend_for_role(entries, "writer")
    if best is not None:
        return local_models.ollama_id(best["name"])
    if entries:
        problems.append(f"Ollama at {host} has no US-origin chat model "
                        "installed")
    return ""
