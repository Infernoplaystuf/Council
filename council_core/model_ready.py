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

The notice exists to say "the Council cannot answer yet". So that is the
question asked here — the way the engine would find a model, without
importing the engine (seconds) and without loading or running anything — for
each role an answer needs (answering_roles: the Writer, and in the full build
the Judge), through the slot model_slots.json gives that role:

  1. The slot's model ("main" follows the main model: the COUNCIL_GGUF_PATH a
     user exported, else the one saved in the app, else the launcher's pick —
     onboarding.load_gguf_path): a GGUF file that is on disk AND a
     llama-cpp-python to load it, or an "ollama:<name>" model the local
     Ollama has (no fallback from one of those, as in the engine).
  2. Otherwise the engine's own fallback (council_engine._route_chat): with
     COUNCIL_BACKEND=ollama, or COUNCIL_OLLAMA_FALLBACK not switched off, an
     installed Ollama model the engine would actually pick — COUNCIL_OLLAMA_MODEL
     when set, else a US-origin chat model that fits this PC's memory, ranked
     with the engine's figures (the engine never picks another).

Found in review, and why it is per role and sized: "any slot resolves" said
ready with main on a model that was not pulled (the Writer and the Judge use
main), and a 400 GB model counted on a 32 GB PC. And an Ollama that is not
running yet is named as such, not as "no model is configured".

The Tk wizard still decides on the marker (onboarding.run_if_needed): that is
"has this person been walked through setup", a different question.

ONLY METADATA, ONLY LOOPBACK
Ollama is asked /api/version and /api/tags through local_models, which refuses
a non-loopback host — never a generation — and only when a role's GGUF does
not already answer. A "no answer" is not left in local_models' cache (the
engine would read it for the next 10 s), and council_qt.launch runs the check
on a worker beside the window build, after the splash is up, so a slow probe
never holds back the first pixel.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

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


def answering_roles() -> Tuple[str, ...]:
    """The roles a Council answer cannot do without.

    The Writer always: the default build (DEMO_MODE) answers with one Writer
    call. The Judge too in the full build, which routes and critiques every
    turn with it. ANY slot that resolved used to count — found in review: main
    on an Ollama model that was not pulled and a "coding" slot that was, and
    the check said ready while every answer failed, since the Writer and the
    Judge are on main."""
    try:
        import branding
        demo = bool(getattr(branding, "DEMO_MODE", True))
    except Exception:                                     # noqa: BLE001
        demo = True
    return ("writer",) if demo else ("writer", "judge")


def check(vault_dir, *, roles: Optional[Sequence[str]] = None,
          loader_available: Optional[bool] = None) -> Readiness:
    """Whether every role in ``roles`` (default answering_roles()) has a model
    that answers, by the engine's own routing. Never raises for an ordinary
    reason — a missing file, no server, a damaged slots file
    (model_slots.load falls back to one model, as the engine does)."""
    from . import model_slots

    vault = Path(vault_dir)
    config = model_slots.load(vault)
    route = _Route(
        main=_main_model(vault),
        to_ollama=os.environ.get("COUNCIL_BACKEND", "").strip().lower()
        == "ollama",
        can_load=(gguf_loader_available() if loader_available is None
                  else bool(loader_available)))
    route.fallback = route.to_ollama or not _off("COUNCIL_OLLAMA_FALLBACK")

    served = {}                 # slot name -> what answers it ("" = nothing)
    problems: List[str] = []
    first = ""
    for role in (roles or answering_roles()):
        name = config.slot_for(role)
        if name not in served:
            served[name] = route.serve(config.slots[name], problems)
        if not served[name]:
            reason = "; ".join(dict.fromkeys(problems))
            return Readiness(False, reason=reason or "no model is configured yet")
        first = first or served[name]
    return Readiness(True, model=first)


class _Route:
    """council_engine._target_for / _route_chat, read without running them:
    what would serve one slot, or why nothing would."""

    def __init__(self, *, main: str, to_ollama: bool, can_load: bool):
        self.main = main
        self.to_ollama = to_ollama
        self.can_load = can_load
        self.fallback = False
        self._ollama: Optional[_Ollama] = None

    @property
    def ollama(self) -> "_Ollama":
        if self._ollama is None:
            self._ollama = _Ollama()
        return self._ollama

    def serve(self, slot, problems: List[str]) -> str:
        from . import local_models, model_slots

        path = slot.resolved_path(self.main).strip()
        if path and model_slots.is_ollama(path):
            # An "ollama:<name>" slot is that model or nothing: the engine
            # does not fall back from it.
            return self.ollama.has(local_models.ollama_name(path), problems)
        if path and not self.to_ollama:
            if not os.path.isfile(path):
                problems.append(f"the model file {path} is not on disk")
            elif not self.can_load:
                problems.append(f"{Path(path).name} is set, but "
                                "llama-cpp-python is not installed in this "
                                "Python, so it cannot be loaded")
            else:
                return path
        if self.fallback:
            # COUNCIL_BACKEND=ollama sends every GGUF slot here; otherwise a
            # GGUF that cannot load falls back to a localhost Ollama.
            return self.ollama.default_pick(problems)
        return ""


class _Ollama:
    """The local Ollama, asked at most once per check: /api/version, then
    /api/tags — metadata only, loopback only (local_models refuses any other
    host)."""

    _UNASKED = object()

    def __init__(self):
        from . import local_models
        self.host = local_models.ollama_host()
        self._tags = self._UNASKED

    def tags(self):
        """The installed models, or None when no server answers. A "no
        answer" is not left in local_models' cache: the engine reads it for
        10 s before each call, and this runs at startup, before an Ollama
        launched alongside the app may be up (found in review)."""
        from . import local_models
        if self._tags is self._UNASKED:
            try:
                self._tags = (local_models.ollama_tags(self.host)
                              if local_models.ollama_reachable(self.host)
                              else None)
            except RuntimeError:
                self._tags = None        # not loopback: never asked
            if self._tags is None:
                local_models.forget_host(self.host)
        return self._tags

    def _down(self, what: str) -> str:
        return (f"no Ollama server answers at {self.host}{what} — is Ollama "
                "running?")

    def has(self, name: str, problems: List[str], *, why: str = "") -> str:
        from . import local_models
        if self.tags() is None:
            problems.append(self._down(f" for {name}{why}"))
            return ""
        if local_models.ollama_model(name, self.host,
                                     fill_from_show=False) is not None:
            return local_models.ollama_id(name)
        problems.append(f"Ollama at {self.host} does not have {name}{why}")
        return ""

    def default_pick(self, problems: List[str]) -> str:
        """council_engine._pick_default_ollama_model: COUNCIL_OLLAMA_MODEL,
        else the best installed US-origin chat model that FITS this PC —
        ranked with the same memory figures (found in review: ranked without
        them, a 400 GB model counted on a 32 GB PC that the engine then
        refused)."""
        from . import local_models
        named = os.environ.get("COUNCIL_OLLAMA_MODEL", "").strip()
        if named:
            return self.has(local_models.ollama_name(named), problems,
                            why=" (COUNCIL_OLLAMA_MODEL)")
        tags = self.tags()
        if tags is None:
            problems.append(self._down(""))
            return ""
        entries = [local_models.ollama_entry(t, host=self.host,
                                             fill_from_show=False)
                   for t in tags]
        vram, ram = hardware_gb()
        best = local_models.recommend_for_role(entries, "writer",
                                               vram_gb=vram, ram_gb=ram)
        if best is not None:
            return local_models.ollama_id(best["name"])
        unsized = local_models.recommend_for_role(entries, "writer")
        if unsized is not None:
            problems.append(
                f"Ollama at {self.host} has {unsized['name']}, but no "
                f"US-origin chat model that fits this PC "
                f"({vram or 0:g} GB GPU, {ram or 0:g} GB RAM)")
        elif entries:
            problems.append(f"Ollama at {self.host} has no US-origin chat "
                            "model installed")
        else:
            problems.append(f"Ollama at {self.host} has no models pulled")
        return ""


def hardware_gb() -> Tuple[Optional[float], Optional[float]]:
    """(VRAM GB, RAM GB) as council_engine._hardware_gb reads them: the
    Models tab's probe when it has run (looked up, never imported), else
    hardware_detect.quick_memory — one nvidia-smi call, asked only when the
    fallback has to pick a model."""
    jobs = sys.modules.get("council_core.model_jobs")
    hw = getattr(jobs, "_DETECTED", None) if jobs is not None else None
    if hw is not None and (hw.vram_gb or hw.ram_gb):
        return hw.vram_gb, hw.ram_gb
    try:
        import hardware_detect
        return hardware_detect.quick_memory()
    except Exception:                                     # noqa: BLE001
        return None, None
