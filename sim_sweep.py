"""
sim_sweep.py — parameter sweeps over a ``SimRunner``.

A sweep describes how to vary each parameter across multiple runs.
Examples::

    ParameterSweep({
        "jump_velocity": {"type": "range", "start": 300, "stop": 600,
                          "step": 50},                # 7 points
        "gravity":       {"type": "list",
                          "values": [800, 980, 1100]},   # 3 points
    })   # 7 × 3 = 21 runs

    ParameterSweep({
        "seed": {"type": "list", "values": list(range(100))},
    })   # 100 runs

Sweep semantics
---------------
- Cartesian product across all parameter axes (full grid)
- Sequential execution by default. The runner is invoked per
  combination; results are appended to the recorder and emitted via
  the progress callback.
- Parameters can be:
    range —  {"type": "range", "start": <n>, "stop": <n>,
              "step": <n>=1}   (stop exclusive, like Python's range)
    list  —  {"type": "list",  "values": [...]}
    const —  {"type": "const", "value": <n>}    (single value, useful
              for clarity when one axis is fixed)
    persona — {"type": "persona", "names": ["Greedy", "Cautious"]}
              or {"type": "persona", "names": "all"} — each named
              persona is materialised into a flat block of
              ``persona_name`` + ``persona.<weight>`` keys via
              sim_personas.PersonaRegistry. Unknown names are
              dropped with a console warning so a typo doesn't
              silently kill the sweep.

A future-friendly ``concurrency`` field is read but currently
ignored — sequential runs avoid GIL surprises in the Python runner
and serialise nicely against a single-instance Godot binary.

Why a separate module
---------------------
sweep / runner / recorder are independent concerns:
  - runner = "how do I produce one SimRun?"
  - recorder = "how do I persist + index N SimRuns?"
  - sweep = "what parameter combinations should I run?"
Keeping them apart lets each be tested in isolation and lets new
sweep strategies (latin-hypercube, random, optuna-style) drop in
without touching the runner or recorder.
"""

from __future__ import annotations

import itertools
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple


# ============================================================
# Public types
# ============================================================

# Per-axis spec accepted by ParameterSweep. Kept as plain dicts so the
# user's saved-sweep JSON files stay human-readable.
AxisSpec = Dict[str, Any]


@dataclass
class SweepProgress:
    """One progress tick emitted by ``run_sweep``."""
    completed: int                  # runs finished so far
    total:     int                  # total runs in the sweep
    current:   Dict[str, Any]       # the params used for the most recent run
    run_id:    str = ""             # SimRun.id of the most recent run
    error:     str = ""             # populated when the runner reported error


# ============================================================
# Sweep
# ============================================================

class ParameterSweep:
    """Iterable over a cartesian product of parameter axes.

    Construct from a dict mapping param-name → axis-spec; iterate to
    get each ``Dict[str, Any]`` parameter combination. ``len(sweep)``
    reports the total run count up front so the UI can show a real
    progress bar.
    """

    def __init__(
        self,
        axes: Dict[str, AxisSpec],
        *,
        persona_registry: Any = None,
    ):
        self.axes = dict(axes or {})
        # Each axis materialises into a list of **blocks** — a block is
        # a dict that contributes one or more keys to the per-run params
        # when merged. Scalar axes produce single-key blocks
        # ({axis_name: value}). Persona axes produce multi-key blocks
        # (persona_name + persona.<weight>... + persona). The cartesian
        # product over blocks is then a chain of dict.update calls.
        #
        # ``persona_registry`` is consulted by the persona axis kind
        # only. Other axes don't need it; when a persona axis is
        # present and no registry is supplied, we fall back to the
        # built-ins (a fresh PersonaRegistry against an empty vault
        # dir gives those plus no user customisations).
        self._blocks_per_axis: List[Tuple[str, List[Dict[str, Any]]]] = [
            (name, _materialise_axis_blocks(name, spec, persona_registry))
            for name, spec in self.axes.items()
        ]

    # ----------------------------------------------------------------

    def __len__(self) -> int:
        if not self._blocks_per_axis:
            return 0
        total = 1
        for _name, blocks in self._blocks_per_axis:
            total *= max(1, len(blocks))
        return total

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        if not self._blocks_per_axis:
            return iter(())
        block_lists = [blocks for _name, blocks in self._blocks_per_axis]
        for combo in itertools.product(*block_lists):
            merged: Dict[str, Any] = {}
            for block in combo:
                merged.update(block)
            yield merged

    def expand(self) -> List[Dict[str, Any]]:
        """Realise the full list in memory — handy for "warn me if
        this would be 10k runs before I hit Start"."""
        return list(self)

    def to_dict(self) -> Dict[str, Any]:
        return {"axes": self.axes}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ParameterSweep":
        axes = (d or {}).get("axes") or {}
        if not isinstance(axes, dict):
            raise ValueError("ParameterSweep: 'axes' must be a dict")
        return cls(axes)

    @classmethod
    def from_json(cls, path: Any) -> "ParameterSweep":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def from_dict_with_registry(
        cls,
        d: Dict[str, Any],
        persona_registry: Any,
    ) -> "ParameterSweep":
        """Convenience: like ``from_dict`` but threads a PersonaRegistry
        in so persona axes can resolve named profiles against the
        user's vault customisations."""
        axes = (d or {}).get("axes") or {}
        if not isinstance(axes, dict):
            raise ValueError("ParameterSweep: 'axes' must be a dict")
        return cls(axes, persona_registry=persona_registry)


# ============================================================
# Axis materialisation
# ============================================================

def _materialise_axis_blocks(
    name: str,
    spec: AxisSpec,
    persona_registry: Any = None,
) -> List[Dict[str, Any]]:
    """Turn an axis spec into a list of param **blocks**.

    A block is a dict that, when merged into the per-run params,
    contributes one or more keys. For scalar axes (range / list /
    const) each block is ``{axis_name: scalar_value}``. For persona
    axes each block is the flat persona param block produced by
    ``PersonaProfile.to_params()`` — multiple keys per "value".
    """
    if not isinstance(spec, dict):
        raise ValueError(
            f"sweep axis {name!r}: spec must be a dict, got "
            f"{type(spec).__name__}"
        )
    kind = (spec.get("type") or "").lower()
    if kind == "persona":
        return _materialise_persona_blocks(name, spec, persona_registry)
    # Scalar — re-use the existing scalar materialiser and wrap each
    # value in a single-key block.
    values = _materialise_axis(name, spec)
    return [{name: v} for v in values]


def _materialise_persona_blocks(
    axis_name: str,
    spec: AxisSpec,
    persona_registry: Any,
) -> List[Dict[str, Any]]:
    """Resolve a persona axis spec into per-persona param blocks.

    Lazy import of sim_personas so a missing personas module
    surfaces only when the user actually uses a persona axis,
    not on every import of sim_sweep.
    """
    try:
        import sim_personas as _sp
    except Exception as exc:
        raise ValueError(
            f"sweep axis {axis_name!r}: persona type requires "
            f"sim_personas module ({exc!r})"
        ) from None
    names_raw = spec.get("names")
    if names_raw is None:
        raise ValueError(
            f"sweep axis {axis_name!r}: persona type needs a 'names' "
            f"key (a list of persona names or the string \"all\")"
        )
    # Without an injected registry, fall back to a built-ins-only
    # view by giving PersonaRegistry an empty vault dir under the
    # OS temp dir. The user gets the eight built-in personas but
    # not their custom personas.json.
    if persona_registry is None:
        import tempfile
        persona_registry = _sp.PersonaRegistry(tempfile.gettempdir())
    names = persona_registry.expand_names(names_raw)
    if not names:
        raise ValueError(
            f"sweep axis {axis_name!r}: no valid personas resolved from "
            f"{names_raw!r}"
        )
    blocks: List[Dict[str, Any]] = []
    for n in names:
        profile = persona_registry.get(n)
        if profile is None:
            continue
        blocks.append(profile.to_params())
    return blocks


def _materialise_axis(name: str, spec: AxisSpec) -> List[Any]:
    """Turn an axis spec into the concrete list of values to try.

    Tolerant: if the spec is malformed we raise with a clear message
    referencing the axis name so the user can fix their config.
    """
    if not isinstance(spec, dict):
        raise ValueError(
            f"sweep axis {name!r}: spec must be a dict, got "
            f"{type(spec).__name__}"
        )
    kind = (spec.get("type") or "").lower()
    if kind == "list":
        values = spec.get("values")
        if not isinstance(values, (list, tuple)):
            raise ValueError(
                f"sweep axis {name!r}: 'list' requires a 'values' array"
            )
        return list(values)
    if kind == "const":
        return [spec.get("value")]
    if kind == "range":
        try:
            start = spec["start"]
            stop  = spec["stop"]
        except KeyError as missing:
            raise ValueError(
                f"sweep axis {name!r}: 'range' needs start, stop"
                f" (missing {missing})"
            ) from None
        step = spec.get("step", 1)
        if step == 0:
            raise ValueError(
                f"sweep axis {name!r}: 'range' step cannot be zero"
            )
        return _frange(start, stop, step)
    raise ValueError(
        f"sweep axis {name!r}: unknown type {kind!r} "
        f"(use range / list / const)"
    )


def _frange(start: float, stop: float, step: float) -> List[float]:
    """Float-tolerant range, stop exclusive (matching Python range)."""
    out: List[float] = []
    # Cast to float so int+int doesn't silently miss a 0.5 step
    s, e, st = float(start), float(stop), float(step)
    if st > 0:
        v = s
        # Use a small epsilon so 0.1+0.2+0.3 doesn't accidentally
        # exclude the stop value when the user really meant inclusive.
        # Python's range is exclusive; we match that, but we don't
        # over-trim on float drift.
        eps = abs(st) * 1e-9
        while v < e - eps:
            out.append(_clean_float(v))
            v += st
    else:
        v = s
        eps = abs(st) * 1e-9
        while v > e + eps:
            out.append(_clean_float(v))
            v += st
    return out


def _clean_float(v: float) -> Any:
    """Snap whole-number floats to int and round messy floats to 9
    decimal places so the on-disk JSON stays clean."""
    if v == int(v):
        return int(v)
    return round(v, 9)


# ============================================================
# Sweep runner
# ============================================================

def run_sweep(
    sweep: ParameterSweep,
    runner: Any,                          # SimRunner
    recorder: Any,                        # SimRecorder
    *,
    sim_name: str = "",
    on_progress: Optional[Callable[[SweepProgress], None]] = None,
    cancel: Optional[threading.Event] = None,
) -> List[Any]:
    """Run every combination in ``sweep`` through ``runner``, persist
    each result to ``recorder``, and return the list of SimRun objects.

    ``on_progress`` fires once per completed run with a SweepProgress
    snapshot — the GUI uses it to update the progress bar and a
    running results table.

    ``cancel`` is an optional threading.Event the caller can set to
    request an early stop. The loop checks before each run so a
    cancel during one run lets that one finish and bails before the
    next.

    Errors from the runner are NOT raised — they're recorded into the
    run.error field and surfaced via on_progress with a non-empty
    error string. Sweep proceeds to the next combination regardless,
    so a single bad config doesn't abort an overnight batch.
    """
    on_progress = on_progress or (lambda p: None)
    cancel = cancel or threading.Event()
    total = len(sweep)
    results: List[Any] = []
    completed = 0
    for params in sweep:
        if cancel.is_set():
            break
        try:
            run = runner.run(params=params, sim_name=sim_name)
        except Exception as exc:
            # Defensive — a runner shouldn't normally raise, but if it
            # does we synthesise a failed SimRun so the caller still
            # gets a record per attempted combination.
            from sim_recorder import SimRun
            run = SimRun(sim_name=sim_name,
                          backend=getattr(runner, "backend", "?"),
                          params=dict(params),
                          error=f"runner raised: {exc!r}")
        try:
            recorder.record(run)
        except Exception as exc:
            # Persistence failure shouldn't abort the sweep either.
            print(f"[sim_sweep] record failed for {params}: {exc!r}")
        results.append(run)
        completed += 1
        try:
            on_progress(SweepProgress(
                completed=completed, total=total,
                current=dict(params),
                run_id=run.id,
                error=run.error,
            ))
        except Exception:
            pass
    return results
