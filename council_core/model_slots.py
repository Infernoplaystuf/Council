"""
council_core.model_slots — which model answers for which role.

WHAT WAS THERE
Every role is pinned to a backend key (personality_backends.json), each key
names a model tier — but under the GGUF backend all of them resolved to ONE
Llama instance loaded from COUNCIL_GGUF_PATH, behind one global lock. The
pins were decoration: the whole council was one model taking turns.

WHAT THIS ADDS
A SLOT is a loaded model: a name and a GGUF file. Roles are assigned to slots
in vault/model_slots.json:

    {"version": 1,
     "slots": {"main": {"path": ""},
               "fast": {"path": "C:/.../Llama-3.2-3B-Instruct-Q5_K_M.gguf"}},
     "roles": {"peasant": "fast", "intern": "fast", "artist": "fast"}}

  * "main" always exists. Its path "" means "whatever COUNCIL_GGUF_PATH is",
    so the Models tab's Download & switch keeps changing the main model.
  * A role not listed answers from "main".
  * No file at all == one slot, "main" == exactly the behaviour before this
    module existed. Nobody is migrated by accident.
  * A slot's "path" may instead be "ollama:<name>" — a model a LOCALHOST
    Ollama server already has (council_core.local_models). Measured on the
    RTX 4070 Laptop this was built on: the council env has no
    llama-cpp-python, so those are the models the Qt app can actually run.
    An Ollama slot may carry "n_ctx", the window sent as num_ctx (8192 when
    absent); a GGUF slot's window comes from the loader's ladder instead.

THE ROLES
The council's own (writer … artist) plus "docs": the model that answers
questions from a documentation server (an MCP server holding a Python
package's docs) and writes code from what it read.

PLACEMENT (the CPU-fallback decision)
Slots are placed on the GPU in priority order — main first, then by how many
roles each serves — while the weights plus a minimum context fit in free VRAM.
A slot that does not fit runs on the CPU (n_gpu_layers=0) rather than evicting
another: slower, but always available, and nothing reloads mid-conversation.
The context memory left over is shared among the GPU slots, main taking two
shares, so no model auto-sizes its context to fill the card and starve the
next one — which is what the single-model loader did, correctly, when it was
the only model.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

MAIN = "main"
FILE_NAME = "model_slots.json"
VERSION = 1

#: The roles the Models tab offers, in council order. Every other role answers
#: from "main" unless the file says otherwise.
COUNCIL_ROLES: Tuple[str, ...] = (
    "writer", "judge", "coder", "skeptic", "sage", "strategist",
    "peasant", "intern", "artist", "docs")

ROLE_LABELS = {
    "writer": "Writer (synthesis)", "judge": "Judge (verdicts)",
    "coder": "Coder (GUIs and code)", "skeptic": "Skeptic", "sage": "Sage",
    "strategist": "Strategist", "peasant": "Peasant (plain questions)",
    "intern": "Intern (first drafts)", "artist": "Artist",
    "docs": "Docs (answers from documentation)",
}

GB = 1024 ** 3
#: Kept free on the card for the driver, the desktop and llama.cpp scratch.
DEFAULT_MARGIN_BYTES = 1 * GB
#: A GPU slot needs at least this much left for its context (≈4k tokens on
#: an 8-14B model); below it, the slot goes to the CPU instead.
MIN_KV_BYTES = GB // 2
#: llama.cpp's CUDA compute buffer with flash attention at n_ubatch 512:
#: ≈260 MiB reported for an 8B model; rounded up. Partial offload is where the
#: card is tightest, so it is counted there instead of left to the margin.
COMPUTE_BYTES = 512 * 1024 ** 2

OLLAMA_PREFIX = "ollama:"


def is_ollama(path: str) -> bool:
    return isinstance(path, str) and path.strip().lower().startswith(
        OLLAMA_PREFIX)


@dataclass
class Slot:
    name: str
    path: str = ""          # "" on main: follow COUNCIL_GGUF_PATH
    n_ctx: Optional[int] = None   # Ollama slots: num_ctx (None: the default)

    def resolved_path(self, main_path: str = "") -> str:
        if self.path:
            return self.path
        return main_path if self.name == MAIN else ""

    @property
    def is_ollama(self) -> bool:
        return is_ollama(self.path)


@dataclass
class SlotConfig:
    slots: Dict[str, Slot] = field(default_factory=lambda: {MAIN: Slot(MAIN)})
    roles: Dict[str, str] = field(default_factory=dict)

    def slot_for(self, role: Optional[str]) -> str:
        name = self.roles.get(role or "", MAIN)
        return name if name in self.slots else MAIN

    def roles_of(self, slot: str) -> List[str]:
        if slot == MAIN:
            return [r for r in COUNCIL_ROLES if self.slot_for(r) == MAIN]
        return [r for r, s in self.roles.items() if s == slot]

    @property
    def multi(self) -> bool:
        return len(self.slots) > 1

    def to_json(self) -> dict:
        def one(s: Slot) -> dict:
            d = {"path": s.path}
            if s.n_ctx:
                d["n_ctx"] = int(s.n_ctx)
            return d
        return {"version": VERSION,
                "slots": {n: one(s) for n, s in self.slots.items()},
                "roles": {r: s for r, s in self.roles.items()
                          if s != MAIN and s in self.slots}}


class ConfigError(ValueError):
    pass


def config_path(vault_dir: Path) -> Path:
    return Path(vault_dir) / FILE_NAME


def parse(data: dict) -> SlotConfig:
    """A SlotConfig from the file's JSON. Raises ConfigError on nonsense."""
    if not isinstance(data, dict):
        raise ConfigError("model_slots.json must hold an object")
    raw_slots = data.get("slots") or {}
    if not isinstance(raw_slots, dict):
        raise ConfigError("'slots' must be an object")
    slots = {MAIN: Slot(MAIN)}
    for name, spec in raw_slots.items():
        name = str(name).strip()
        if not name:
            raise ConfigError("a slot has an empty name")
        path = str((spec or {}).get("path", "") if isinstance(spec, dict)
                   else spec or "").strip()
        if name != MAIN and not path:
            raise ConfigError(f"slot '{name}' has no model file")
        if is_ollama(path) and not path[len(OLLAMA_PREFIX):].strip():
            raise ConfigError(f"slot '{name}' names no Ollama model")
        n_ctx = spec.get("n_ctx") if isinstance(spec, dict) else None
        if n_ctx is not None:
            try:
                n_ctx = int(n_ctx)
            except (TypeError, ValueError):
                raise ConfigError(f"slot '{name}' has a bad n_ctx {n_ctx!r}")
            if n_ctx < 512:
                raise ConfigError(f"slot '{name}' n_ctx {n_ctx} is too small")
        slots[name] = Slot(name, path, n_ctx)
    roles = {}
    for role, slot in (data.get("roles") or {}).items():
        if slot not in slots:
            raise ConfigError(f"role '{role}' uses unknown slot '{slot}'")
        roles[str(role)] = str(slot)
    return SlotConfig(slots, roles)


def load(vault_dir: Path) -> SlotConfig:
    """The saved configuration, or the single-model default. Never raises:
    a damaged file must not stop the council answering — it falls back to one
    model and the Models tab says why (see `problem`)."""
    try:
        return parse(json.loads(config_path(vault_dir).read_text(
            encoding="utf-8")))
    except Exception:                                     # noqa: BLE001
        return SlotConfig()


def problem(vault_dir: Path) -> str:
    """Why the saved file is not in use, or "" (absent counts as fine)."""
    path = config_path(vault_dir)
    if not path.exists():
        return ""
    try:
        parse(json.loads(path.read_text(encoding="utf-8")))
        return ""
    except Exception as exc:                              # noqa: BLE001
        return f"{path.name} could not be used ({exc}); running one model."


def save(vault_dir: Path, config: SlotConfig) -> Path:
    # Checked on the config itself: to_json() drops a role whose slot is
    # missing, so validating only its output would lose that role silently.
    for role, slot in config.roles.items():
        if slot not in config.slots:
            raise ConfigError(f"role '{role}' uses unknown slot '{slot}'")
    parse(config.to_json())                               # and the rest
    path = config_path(vault_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(config.to_json(), indent=2), encoding="utf-8")
    os.replace(tmp, path)
    invalidate()
    return path


# ============================================================
# The process-wide current config (what the engine reads)
# ============================================================

_current: Optional[SlotConfig] = None
_current_lock = threading.Lock()


def _vault() -> Path:
    from . import paths
    return paths.vault_dir()


def current() -> SlotConfig:
    """The config in force, read once and kept until `invalidate()`."""
    global _current
    with _current_lock:
        if _current is None:
            _current = load(_vault())
        return _current


def invalidate() -> None:
    global _current
    with _current_lock:
        _current = None


def slot_for_role(role: Optional[str]) -> str:
    try:
        return current().slot_for(role)
    except Exception:                                     # noqa: BLE001
        return MAIN


# ============================================================
# Placement — GPU while it fits, CPU after
# ============================================================

@dataclass(frozen=True)
class Placement:
    slot: str
    on_gpu: bool
    weights_bytes: int
    kv_budget_bytes: Optional[int]     # None on the CPU: RAM, not VRAM
    reason: str = ""


def priority(config: SlotConfig) -> List[str]:
    """Main first, then the slot serving the most roles, then by name."""
    others = sorted((n for n in config.slots if n != MAIN),
                    key=lambda n: (-len(config.roles_of(n)), n))
    return [MAIN] + others


def plan(config: SlotConfig, sizes: Dict[str, int],
         free_vram_bytes: Optional[int], *,
         margin_bytes: int = DEFAULT_MARGIN_BYTES,
         min_kv_bytes: int = MIN_KV_BYTES) -> Dict[str, Placement]:
    """Where each slot runs. Pure arithmetic — the engine supplies the numbers.

    ``sizes`` is each slot's weight bytes (its file size); slots sharing a file
    should be passed once. ``free_vram_bytes`` None means no usable GPU: all on
    CPU.
    """
    order = [n for n in priority(config) if n in sizes]
    if not free_vram_bytes:
        return {n: Placement(n, False, sizes[n], None, "no GPU memory "
                             "information — running on the CPU")
                for n in order}
    room = free_vram_bytes - margin_bytes
    gpu: List[str] = []
    cpu: Dict[str, str] = {}
    used = 0
    for name in order:
        # Every GPU slot, this one included, must keep its minimum context.
        if used + sizes[name] + min_kv_bytes * (len(gpu) + 1) <= room:
            gpu.append(name)
            used += sizes[name]
        else:
            left = max(0, room - used - min_kv_bytes * len(gpu))
            need = sizes[name] + min_kv_bytes
            cpu[name] = (f"needs {need / GB:.1f} GB, {left / GB:.1f} GB left "
                         "on the GPU — running on the CPU")
    kv_room = room - used
    shares = sum(2 if n == MAIN else 1 for n in gpu) or 1
    out = {}
    for name in order:
        if name in cpu:
            out[name] = Placement(name, False, sizes[name], None, cpu[name])
        else:
            share = 2 if name == MAIN else 1
            out[name] = Placement(name, True, sizes[name],
                                  int(kv_room * share / shares), "fits")
    return out


@dataclass(frozen=True)
class OffloadPlan:
    """How many of one model's layers go on the card, and its window.
    n_gpu_layers -1 = every layer (the ladder then sizes the window, n_ctx 0),
    0 = the CPU, k = the last k blocks on the GPU and the rest in RAM."""
    n_gpu_layers: int
    n_ctx: int
    reason: str
    gpu_bytes: int = 0


def plan_offload(*, file_bytes: int, n_layers: int, per_layer_bytes: int,
                 kv_bytes_per_token: int, free_vram_bytes: int,
                 margin_bytes: int = DEFAULT_MARGIN_BYTES,
                 compute_bytes: int = COMPUTE_BYTES,
                 partial_ctx: int = 8192, min_full_ctx: int = 4096,
                 min_layers: int = 1) -> OffloadPlan:
    """Full offload when the whole file plus a ``min_full_ctx`` window fits
    the free VRAM (the same arithmetic as the loader's rung 2, so a model
    that loaded fully before still does); otherwise as many blocks as fit
    beside a ``partial_ctx`` window, each block bringing its share of the KV
    cache with it (llama.cpp keeps a layer's KV on the layer's device).

    Pure arithmetic — the engine supplies the numbers. The output and
    embedding tensors stay on the CPU in a partial load, so only blocks are
    counted against the card, plus the compute buffer.
    """
    full_need = file_bytes + margin_bytes + kv_bytes_per_token * min_full_ctx
    if full_need <= free_vram_bytes:
        return OffloadPlan(-1, 0, "fits on the GPU", file_bytes)
    n_layers = max(1, int(n_layers))
    kv_layer = kv_bytes_per_token * partial_ctx / n_layers
    room = free_vram_bytes - margin_bytes - compute_bytes
    per = per_layer_bytes + kv_layer
    k = int(room // per) if room > 0 and per > 0 else 0
    k = min(k, n_layers - 1)
    if k < min_layers:
        return OffloadPlan(
            0, partial_ctx,
            f"partial offload: {file_bytes / GB:.1f} GB of weights and no "
            f"room for even one layer in {free_vram_bytes / GB:.1f} GB free "
            "— running on the CPU")
    return OffloadPlan(
        k, partial_ctx,
        f"partial offload: {k} of {n_layers} layers on the GPU "
        f"({k * per / GB:.1f} GB with their context), the rest on the CPU "
        f"— {file_bytes / GB:.1f} GB of weights, {free_vram_bytes / GB:.1f} "
        f"GB free, window {partial_ctx:,}",
        int(k * per))


def summary(placements: Dict[str, Placement], labels: Dict[str, str],
            free_vram_bytes: Optional[int]) -> str:
    """One line for the Models tab: what goes where, and the total."""
    if not placements:
        return ""
    parts = []
    gpu_total = 0
    for p in placements.values():
        where = "GPU" if p.on_gpu else "CPU"
        parts.append(f"{labels.get(p.slot, p.slot)} → {where} "
                     f"({p.weights_bytes / GB:.1f} GB)")
        if p.on_gpu:
            gpu_total += p.weights_bytes
    tail = ""
    if free_vram_bytes:
        tail = (f"   ·   {gpu_total / GB:.1f} of {free_vram_bytes / GB:.1f} GB "
                "free VRAM used by weights")
    return "   ".join(parts) + tail


# ============================================================
# Presets and the files a user has
# ============================================================

#: Balanced for a 16 GB card: Phi-4 14B does the thinking, Llama 3.2 3B the
#: quick, divergent roles. ≈11.3 GB of weights, both on the GPU.
BALANCED = {
    "name": "Balanced — Phi-4 14B + Llama 3.2 3B",
    "main": "phi-4-q4",
    "slots": {"fast": "llama-3.2-3b-q5"},
    "roles": {"peasant": "fast", "intern": "fast", "artist": "fast"},
}


def suggest_role_models(models: Sequence[dict], *,
                        vram_gb: Optional[float] = None,
                        ram_gb: Optional[float] = None,
                        roles: Sequence[str] = COUNCIL_ROLES
                        ) -> Dict[str, Tuple[str, str]]:
    """role -> (model id, why) from what is INSTALLED — the "this PC" preset.

    US-origin only (local_models.rank_for_role leaves the rest out), nothing
    downloaded. One model for every role unless a role's own ranking prefers
    another: switching Ollama models between turns reloads weights (seconds
    each time on an 8 GB card that cannot hold two), so a single model that
    fits the card is the fast configuration. Empty when nothing qualifies.
    """
    from . import local_models
    general = local_models.rank_for_role(models, "writer", vram_gb=vram_gb,
                                         ram_gb=ram_gb)
    if not general:
        return {}
    base = general[0]
    out: Dict[str, Tuple[str, str]] = {}
    for role in roles:
        ranked = local_models.rank_for_role(models, role, vram_gb=vram_gb,
                                            ram_gb=ram_gb)
        pick = ranked[0] if ranked else base
        # Same score as the shared model: keep the shared one (no reload).
        for cand in ranked:
            if cand["id"] == base["id"] and cand["score"] >= pick["score"]:
                pick = cand
                break
        out[role] = (pick["id"], pick.get("why", ""))
    return out


def catalog_spec(model_id: str):
    import model_catalog
    return model_catalog.by_id(model_id)


def gguf_dirs(extra: Iterable[Path] = ()) -> List[Path]:
    """Where downloaded models live: the downloader's folder, ~/models, the
    app's own models/ folder — the places the launchers look too."""
    dirs: List[Path] = list(extra)
    try:
        import model_downloader
        dirs.append(model_downloader.default_models_dir())
    except Exception:                                     # noqa: BLE001
        pass
    dirs += [Path.home() / "models",
             Path(__file__).resolve().parent.parent / "models"]
    seen, out = set(), []
    for d in dirs:
        key = str(d).lower()
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def known_files(dirs: Optional[Sequence[Path]] = None,
                also: Iterable[str] = ()) -> List[Path]:
    """Every .gguf on disk in the model folders, plus ``also`` (e.g. the
    current main path), de-duplicated, mmproj vision adapters excluded."""
    found: Dict[str, Path] = {}
    for d in (dirs if dirs is not None else gguf_dirs()):
        try:
            for f in sorted(Path(d).glob("*.gguf")):
                if "mmproj" not in f.name.lower():
                    found.setdefault(str(f.resolve()).lower(), f)
        except OSError:
            continue
    for p in also:
        if p and not is_ollama(p) and Path(p).is_file():
            found.setdefault(str(Path(p).resolve()).lower(), Path(p))
    return list(found.values())


def find_catalog_file(model_id: str,
                      dirs: Optional[Sequence[Path]] = None) -> Optional[Path]:
    spec = catalog_spec(model_id)
    if spec is None:
        return None
    for d in (dirs if dirs is not None else gguf_dirs()):
        f = Path(d) / spec.hf_file
        if f.is_file():
            return f
    return None


def preset_config(preset: dict, files: Dict[str, Path]) -> SlotConfig:
    """The config for ``preset`` given each catalog id's file on disk."""
    slots = {MAIN: Slot(MAIN)}
    for name, model_id in preset["slots"].items():
        slots[name] = Slot(name, str(files[model_id]))
    return SlotConfig(slots, dict(preset["roles"]))


def preset_missing(preset: dict,
                   dirs: Optional[Sequence[Path]] = None) -> List[str]:
    """Catalog ids the preset needs that are not downloaded yet."""
    ids = [preset["main"]] + list(preset["slots"].values())
    return [i for i in ids if find_catalog_file(i, dirs) is None]


def from_role_files(role_files: Dict[str, str]) -> Tuple[SlotConfig, str]:
    """(config, main file) from a per-role choice of FILE (or Ollama model).

    The Writer's file becomes main — it is the model the council synthesises
    with — and each other distinct file becomes a slot named after it. The
    caller makes main's file the active GGUF, so main keeps following
    COUNCIL_GGUF_PATH. When the Writer's choice is an "ollama:<name>" model
    there is no file to make active: main names it directly, and the caller
    must leave COUNCIL_GGUF_PATH alone (it is returned all the same, so the
    caller can tell — is_ollama(main_file)).
    """
    main_file = role_files.get("writer") or next(iter(role_files.values()), "")
    slots = {MAIN: Slot(MAIN, main_file if is_ollama(main_file) else "")}
    by_file: Dict[str, str] = {_key(main_file): MAIN}
    roles: Dict[str, str] = {}
    for role, file in role_files.items():
        key = _key(file)
        if key not in by_file:
            stem = (file[len(OLLAMA_PREFIX):] if is_ollama(file)
                    else Path(file).stem)
            name = _slot_name(stem, slots)
            slots[name] = Slot(name, file)
            by_file[key] = name
        if by_file[key] != MAIN:
            roles[role] = by_file[key]
    return SlotConfig(slots, roles), main_file


def _key(path: str) -> str:
    if is_ollama(path):
        # Not a path: "ollama:llama3.1:8b" resolved against the cwd (or read
        # as a drive on Windows) would compare wrongly. "phi3.5" and
        # "phi3.5:latest" are one model.
        name = path.strip()[len(OLLAMA_PREFIX):].strip().lower()
        return OLLAMA_PREFIX + (name if ":" in name else name + ":latest")
    try:
        return str(Path(path).resolve()).lower()
    except Exception:                                     # noqa: BLE001
        return str(path).lower()


def _slot_name(stem: str, taken: Dict[str, Slot]) -> str:
    base = "".join(c if c.isalnum() else "-" for c in stem.lower()).strip("-")
    base = (base or "model")[:40]
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    return name
