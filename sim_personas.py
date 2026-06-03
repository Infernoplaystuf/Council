"""
sim_personas.py — player-archetype profiles for simulations.

Real players don't all play the same way. A balance test that only
runs the "average" player misses the cases that actually break:
the greedy player who hoards consumables, the aggressive player
who tanks every hit, the relaxed player who never engages the
optimal loop. This module lets a sim sweep over a roster of
**personas** so each parameter combo can be exercised against
multiple playstyles.

Shape
-----
A ``PersonaProfile`` is a name + a small dict of 0..1 weights:

  risk_tolerance      0 cautious        ←→ 1 reckless
  aggression          0 defensive       ←→ 1 attack-first
  greed               0 minimalist      ←→ 1 hoard everything
  exploration         0 grindy / focus  ←→ 1 wanderer
  pace                0 leisurely       ←→ 1 speedrunner
  completionism       0 skip side stuff ←→ 1 100%
  patience            0 rage-quit       ←→ 1 endless
  caution             0 reckless        ←→ 1 paranoid

Eight built-in personas ship in this module covering the common
archetypes (Greedy / Aggressive / Relaxed / Cautious / Speedrunner
/ Completionist / Hardcore / Casual). Users can add or override
profiles by editing ``vault/simulations/personas.json``.

How games consume them
----------------------
When a sim runs with a persona, the runner injects two kinds of
keys into the ``params`` dict:

  persona_name       — the canonical name (e.g. "Greedy")
  persona.<weight>   — each weight as a flat float, e.g.
                       persona.greed = 0.9

This lets the game **either** switch on the name for big behavior
changes (``if persona_name == "Greedy"``), **or** read individual
weights for fine tuning (``var g = Anvil.get_persona_weight("greed",
0.5)``). The Godot autoload in ``assets/anvil_telemetry.gd`` exposes
the same lookup helper.

Sweep integration
-----------------
``ParameterSweep`` learns a new axis kind in ``sim_sweep.py``:

  {"type": "persona", "names": ["Greedy", "Cautious"]}
  {"type": "persona", "names": "all"}     # iterate the registry

Behind the scenes each named persona is materialised into a flat
``{"persona_name": "...", "persona.greed": 0.9, ...}`` block and
merged into the per-run params dict, so the cartesian product
still produces a single flat params view per run.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional


# ============================================================
# Weight schema
# ============================================================

# The canonical weight names. We don't enforce membership — a user
# can add custom weights to a persona — but every built-in covers
# these eight axes so a game that reads them gets a consistent
# baseline regardless of which persona is active.
WEIGHT_AXES = (
    "risk_tolerance",
    "aggression",
    "greed",
    "exploration",
    "pace",
    "completionism",
    "patience",
    "caution",
)


def _clip01(v: float) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.5


# ============================================================
# PersonaProfile
# ============================================================

@dataclass
class PersonaProfile:
    """One named playstyle archetype.

    Weights are stored as a plain dict (not a fixed set of fields) so
    users can add their own custom axes without code changes — the
    ``WEIGHT_AXES`` tuple is just the documented common set.
    """
    name:    str
    summary: str = ""
    weights: Dict[str, float] = field(default_factory=dict)
    # ``tags`` is free-form metadata for the UI — e.g. ["meta", "tryhard"]
    tags:    List[str] = field(default_factory=list)
    # ``custom`` distinguishes user-added profiles from the built-ins
    # so the registry can refuse to overwrite a built-in via the
    # JSON file and surface the conflict instead.
    custom:  bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "PersonaProfile":
        name = str(d.get("name") or "").strip() or "Unnamed"
        weights_raw = d.get("weights") or {}
        weights: Dict[str, float] = {}
        if isinstance(weights_raw, Mapping):
            for k, v in weights_raw.items():
                weights[str(k)] = _clip01(v)
        return cls(
            name=name,
            summary=str(d.get("summary") or ""),
            weights=weights,
            tags=list(d.get("tags") or []),
            custom=bool(d.get("custom", False)),
        )

    def get(self, axis: str, default: float = 0.5) -> float:
        """Read one weight; missing axes fall back to ``default``."""
        v = self.weights.get(axis)
        return float(v) if isinstance(v, (int, float)) else default

    def to_params(self) -> Dict[str, Any]:
        """Flatten into the per-run params shape the runners inject.

        Two kinds of keys come out of this:
          persona_name              — string
          persona.<axis>            — float per weight
        Plus a verbatim ``persona`` nested dict for backends that
        prefer the structured view (e.g. a Python sim that pattern-
        matches on the dict).
        """
        flat: Dict[str, Any] = {"persona_name": self.name}
        for axis, value in self.weights.items():
            flat[f"persona.{axis}"] = float(value)
        flat["persona"] = {
            "name":    self.name,
            "summary": self.summary,
            "weights": dict(self.weights),
            "tags":    list(self.tags),
        }
        return flat


# ============================================================
# Built-in roster (8 personas)
# ============================================================

def _builtin(
    name: str, summary: str, tags: List[str], **weights: float,
) -> PersonaProfile:
    """Build a built-in profile with every WEIGHT_AXES key populated.

    Missing axes default to 0.5 so games that read an axis we
    didn't explicitly set still get a sensible mid-value.
    """
    w = {axis: 0.5 for axis in WEIGHT_AXES}
    w.update({k: _clip01(v) for k, v in weights.items()})
    return PersonaProfile(name=name, summary=summary, tags=tags,
                           weights=w, custom=False)


BUILTIN_PERSONAS: List[PersonaProfile] = [
    _builtin(
        "Greedy",
        "Hoards every resource; takes risks for loot; impatient with "
        "non-rewarding content.",
        tags=["loot", "risk"],
        greed=0.95, risk_tolerance=0.7, aggression=0.55,
        exploration=0.6, pace=0.5, completionism=0.4,
        patience=0.35, caution=0.3,
    ),
    _builtin(
        "Aggressive",
        "Attack-first, low caution, ignores defensive tools and "
        "consumables; thrives on direct engagement.",
        tags=["combat", "tryhard"],
        aggression=0.95, risk_tolerance=0.85, pace=0.7,
        caution=0.15, patience=0.4, greed=0.45,
        exploration=0.4, completionism=0.35,
    ),
    _builtin(
        "Relaxed",
        "Leisurely pace, low-stakes engagement; happy to wander; "
        "rarely optimises.",
        tags=["casual", "exploration"],
        pace=0.2, patience=0.85, exploration=0.7,
        aggression=0.2, risk_tolerance=0.3, greed=0.35,
        completionism=0.3, caution=0.7,
    ),
    _builtin(
        "Cautious",
        "Risk-averse; defensive play; uses every consumable; rarely "
        "engages without an advantage.",
        tags=["safety", "consumables"],
        caution=0.95, risk_tolerance=0.15, patience=0.8,
        aggression=0.2, greed=0.5, pace=0.3,
        exploration=0.45, completionism=0.55,
    ),
    _builtin(
        "Speedrunner",
        "Maximum pace; skips side content; tolerates mistakes that "
        "save time; high-execution play.",
        tags=["speed", "optimisation"],
        pace=0.98, completionism=0.1, exploration=0.15,
        patience=0.25, risk_tolerance=0.7, aggression=0.6,
        greed=0.2, caution=0.3,
    ),
    _builtin(
        "Completionist",
        "100% everything; collects every item; explores every room "
        "before progressing.",
        tags=["100%", "collector"],
        completionism=0.98, exploration=0.9, patience=0.9,
        pace=0.25, greed=0.55, risk_tolerance=0.4,
        aggression=0.4, caution=0.6,
    ),
    _builtin(
        "Hardcore",
        "Punishing-difficulty player; pushes through frustration; "
        "high challenge tolerance; rarely quits.",
        tags=["challenge", "endurance"],
        patience=0.95, risk_tolerance=0.8, aggression=0.7,
        completionism=0.55, pace=0.55, caution=0.45,
        greed=0.4, exploration=0.5,
    ),
    _builtin(
        "Casual",
        "Short sessions; low friction tolerance; ignores grind; "
        "quits at first major frustration.",
        tags=["short-session", "low-friction"],
        patience=0.15, completionism=0.2, aggression=0.3,
        pace=0.4, exploration=0.4, risk_tolerance=0.35,
        greed=0.3, caution=0.55,
    ),
]


# ============================================================
# PersonaRegistry
# ============================================================

PERSONAS_FILENAME = "personas.json"


class PersonaRegistry:
    """Combined view over built-ins + user-defined personas.

    User profiles live in ``vault/simulations/personas.json``. The
    file is read on first access and any time ``reload()`` is
    called. Built-ins are never written back — the JSON file only
    holds user customisations and overrides.

    Conflict policy: if a user profile has the same name as a
    built-in, the user profile wins. A diagnostic line is printed
    so the user knows the override was applied; the built-in is
    still available via ``builtin(name)``.
    """

    def __init__(self, vault_dir: Any):
        self.vault_dir = Path(vault_dir).expanduser().resolve()
        self.dir = self.vault_dir / "simulations"
        self.path = self.dir / PERSONAS_FILENAME
        self._builtins: Dict[str, PersonaProfile] = {
            p.name: p for p in BUILTIN_PERSONAS
        }
        self._user: Dict[str, PersonaProfile] = {}
        self._lock = threading.RLock()
        self.reload()

    # ----------------------------------------------------------------
    # Persistence
    # ----------------------------------------------------------------

    def reload(self) -> None:
        """Re-read the user JSON file. No-op if it doesn't exist."""
        with self._lock:
            self._user = {}
            if not self.path.exists():
                return
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"[PersonaRegistry] could not load {self.path}: {exc!r}")
                return
            entries = data.get("personas") if isinstance(data, dict) else None
            if not isinstance(entries, list):
                return
            for raw in entries:
                if not isinstance(raw, Mapping):
                    continue
                try:
                    p = PersonaProfile.from_dict(raw)
                    p.custom = True
                    self._user[p.name] = p
                except Exception as exc:
                    print(f"[PersonaRegistry] dropped malformed entry: {exc!r}")

    def save(self) -> None:
        """Persist the user-defined personas (built-ins are not written)."""
        with self._lock:
            payload = {
                "personas": [p.to_dict() for p in self._user.values()],
            }
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except Exception as exc:
            print(f"[PersonaRegistry] save failed: {exc!r}")

    # ----------------------------------------------------------------
    # CRUD
    # ----------------------------------------------------------------

    def add(self, profile: PersonaProfile) -> None:
        """Add or overwrite a user-defined profile and persist."""
        profile.custom = True
        with self._lock:
            self._user[profile.name] = profile
        self.save()

    def remove(self, name: str) -> bool:
        """Drop a user-defined profile. Built-ins can't be removed."""
        with self._lock:
            if name in self._user:
                del self._user[name]
                self.save()
                return True
        return False

    # ----------------------------------------------------------------
    # Read
    # ----------------------------------------------------------------

    def get(self, name: str) -> Optional[PersonaProfile]:
        """Lookup: user override first, then built-in."""
        with self._lock:
            return self._user.get(name) or self._builtins.get(name)

    def builtin(self, name: str) -> Optional[PersonaProfile]:
        """Always returns the built-in version, ignoring user overrides."""
        return self._builtins.get(name)

    def names(self) -> List[str]:
        """All known names, builtins first then custom, alphabetised within."""
        with self._lock:
            bi = sorted(self._builtins.keys())
            user_extras = sorted(n for n in self._user if n not in self._builtins)
            return bi + user_extras

    def all(self) -> List[PersonaProfile]:
        with self._lock:
            out: List[PersonaProfile] = []
            for name in self.names():
                profile = self._user.get(name) or self._builtins.get(name)
                if profile is not None:
                    out.append(profile)
            return out

    # ----------------------------------------------------------------
    # Bulk helpers used by the sweep axis
    # ----------------------------------------------------------------

    def expand_names(self, names: Iterable[str] | str) -> List[str]:
        """Resolve ``"all"`` to every known name; otherwise validate
        each name against the registry. Unknown names are dropped
        with a warning so a misspelling doesn't silently nuke a sweep.
        """
        if isinstance(names, str):
            if names.lower() == "all":
                return self.names()
            names = [names]
        out: List[str] = []
        for n in names:
            if not isinstance(n, str):
                continue
            if self.get(n) is None:
                print(f"[PersonaRegistry] unknown persona dropped: {n!r}")
                continue
            out.append(n)
        return out


# ============================================================
# Helpers for runners + sweep
# ============================================================

def persona_to_params(persona: PersonaProfile) -> Dict[str, Any]:
    """Shortcut for the runner: flatten a persona to params keys."""
    return persona.to_params()


def merge_persona_params(
    base_params: Mapping[str, Any],
    persona: Optional[PersonaProfile],
) -> Dict[str, Any]:
    """Overlay persona params on top of the user-supplied base params.

    Conflict policy: explicit base params win. This means a sweep
    can override a persona weight on a specific run (e.g. force
    ``persona.greed = 0.0`` on a control run) by setting it in
    the base axes.
    """
    out: Dict[str, Any] = {}
    if persona is not None:
        out.update(persona.to_params())
    for k, v in (base_params or {}).items():
        out[k] = v
    return out
