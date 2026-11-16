"""
An experiment specification: what is varied and over which range, what is
held fixed, which design, how many replicates. A model may PROPOSE one from
plain English (`from_request`); the plugin then normalises it to base units
and the deterministic checks (engine.plan.check) decide whether it can go
further. The model never produces the runs: the design is computed here.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Tuple

from . import doe
from . import limits as lm


@dataclass
class Variable:
    key: str
    low: float
    high: float
    levels: int = 2


@dataclass
class Fixed:
    key: str
    value: float


@dataclass
class ExperimentSpec:
    title: str = "New experiment"
    profile: str = ""                       # the limits profile's name
    design: str = "full_factorial"
    variables: List[Variable] = field(default_factory=list)
    fixed: List[Fixed] = field(default_factory=list)
    runs: int = 0                           # Latin hypercube only
    center_points: int = 0
    replicates: int = 1
    randomize: bool = True
    seed: int = 1
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ExperimentSpec":
        if not isinstance(d, dict):
            raise ValueError("a specification is a JSON object")
        spec = cls(**{k: v for k, v in d.items()
                      if k in cls.__dataclass_fields__
                      and k not in ("variables", "fixed")})
        spec.variables = [Variable(**{k: v for k, v in x.items()
                                      if k in Variable.__dataclass_fields__})
                          for x in d.get("variables") or []]
        spec.fixed = [Fixed(**{k: v for k, v in x.items()
                               if k in Fixed.__dataclass_fields__})
                      for x in d.get("fixed") or []]
        for v in spec.variables:
            v.low, v.high, v.levels = float(v.low), float(v.high), int(v.levels)
        for f in spec.fixed:
            f.value = float(f.value)
        spec.runs = int(spec.runs or 0)
        spec.center_points = int(spec.center_points or 0)
        spec.replicates = max(1, int(spec.replicates or 1))
        spec.seed = int(spec.seed or 0)
        spec.randomize = bool(spec.randomize)
        return spec

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    def run_count(self) -> int:
        """How many specimens this spec makes (0 when it cannot be made)."""
        try:
            return len(design_matrix(self)) * self.replicates
        except doe.DesignError:
            return 0


def design_matrix(spec: ExperimentSpec):
    """Coded points of the spec's design (one row per distinct run)."""
    k = len(spec.variables)
    return doe.design_points(
        spec.design, k, levels=[v.levels for v in spec.variables],
        runs=spec.runs, seed=spec.seed, center_points=spec.center_points)


# ----------------------------------------------------------------------
# From plain English, through a model
# ----------------------------------------------------------------------
REQUEST_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "design": {"type": "string", "enum": list(doe.DESIGNS)},
        "variables": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "parameter": {"type": "string"},
                "low": {"type": "number"}, "high": {"type": "number"},
                "unit": {"type": "string"},
                "levels": {"type": "integer"}},
            "required": ["parameter", "low", "high", "unit"]}},
        "fixed": {"type": "array", "items": {
            "type": "object",
            "properties": {"parameter": {"type": "string"},
                           "value": {"type": "number"},
                           "unit": {"type": "string"}},
            "required": ["parameter", "value", "unit"]}},
        "runs": {"type": "integer"},
        "center_points": {"type": "integer"},
        "replicates": {"type": "integer"},
        "notes": {"type": "string"},
    },
    "required": ["title", "design", "variables", "fixed"],
}


def _prompt(request: str, profile: Dict[str, Any]) -> List[Dict[str, str]]:
    params = "\n".join(f"- {p.key}: {p.label} (base unit {p.unit})"
                       for p in lm.PARAMETERS.values())
    designs = "\n".join(f"- {k}: {v}" for k, v in doe.DESIGNS.items())
    system = (
        "You turn an engineer's request for a laser powder bed fusion "
        "experiment into a JSON specification. You do not invent machine "
        "limits and you do not list the runs: the program computes the "
        "design and checks every value. Use only these parameter names:\n"
        f"{params}\nDesigns:\n{designs}\n"
        "Give each value with its unit as the engineer wrote it. If the "
        "engineer gives a number of specimens for a two-factor grid, choose "
        "levels whose product is that number (25 = 5 x 5). Use "
        "latin_hypercube with 'runs' when they ask for a space-filling or "
        "random plan. If a range is not given, take it from the machine's "
        "limits below and say so in notes.\n\nMachine limits:\n"
        f"{lm.describe_profile(profile)}")
    return [{"role": "system", "content": system},
            {"role": "user", "content": request}]


def _parse_json(text: str) -> Dict[str, Any]:
    text = str(text or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ValueError("the model's reply held no JSON") from None
        return json.loads(m.group(0))


def from_model_json(data: Dict[str, Any], profile: Dict[str, Any]
                    ) -> Tuple[ExperimentSpec, List[str]]:
    """A spec from the model's JSON, values converted to base units, and
    the notes about what was changed or dropped on the way."""
    notes: List[str] = []
    spec = ExperimentSpec(title=str(data.get("title") or "New experiment")[:120],
                          profile=str(profile.get("name", "")),
                          notes=str(data.get("notes") or "")[:2000])
    design = str(data.get("design") or "full_factorial")
    if design not in doe.DESIGNS:
        notes.append(f"Design {design!r} is not known; used full factorial.")
        design = "full_factorial"
    spec.design = design
    seen = set()
    for item in data.get("variables") or []:
        key = lm.parameter_key(item.get("parameter", ""))
        if not key:
            notes.append(f"Dropped varied parameter {item.get('parameter')!r}: "
                         f"not one this plugin knows.")
            continue
        if key in seen:
            notes.append(f"{key} was given twice; the first was kept.")
            continue
        try:
            lo = lm.to_base(item.get("low"), item.get("unit", ""), key)
            hi = lm.to_base(item.get("high"), item.get("unit", ""), key)
        except lm.UnitError as exc:
            notes.append(f"Dropped {key}: {exc}")
            continue
        seen.add(key)
        spec.variables.append(Variable(key, min(lo, hi), max(lo, hi),
                                       max(2, int(item.get("levels") or 2))))
    for item in data.get("fixed") or []:
        key = lm.parameter_key(item.get("parameter", ""))
        if not key or key in seen:
            if not key:
                notes.append(f"Dropped fixed parameter "
                             f"{item.get('parameter')!r}: not one this plugin "
                             f"knows.")
            continue
        try:
            value = lm.to_base(item.get("value"), item.get("unit", ""), key)
        except lm.UnitError as exc:
            notes.append(f"Dropped fixed {key}: {exc}")
            continue
        seen.add(key)
        spec.fixed.append(Fixed(key, value))
    for name in ("runs", "center_points", "replicates"):
        try:
            setattr(spec, name, max(0, int(data.get(name) or 0)))
        except (TypeError, ValueError):
            pass
    spec.replicates = max(1, spec.replicates)
    if spec.design == "latin_hypercube" and spec.runs < 2:
        spec.runs = 10
        notes.append("No run count was given for the Latin hypercube; used 10.")
    return spec, notes


def from_request(request: str, profile: Dict[str, Any],
                 chat: Callable[..., str]) -> Tuple[ExperimentSpec, List[str]]:
    """Ask the model for a spec. Raises ValueError when the reply cannot be
    read; the checks are NOT run here (engine.plan.check does that)."""
    reply = chat(_prompt(request, profile), max_tokens=900,
                 schema=REQUEST_SCHEMA)
    return from_model_json(_parse_json(reply), profile)
