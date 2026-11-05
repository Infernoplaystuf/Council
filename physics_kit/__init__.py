"""
physics_kit — the tested physics building blocks a Council physics model is
made from (user, 2026-11-05: "making a [Build tab feature] and giving it a
goal on a physics based model to make", e.g. a PVC pipe organ, radio
antennas, eddy currents, water erosion on materials).

A physics model is a folder (made by the Build tab's "New physics model…"):

    model.py          MODEL = Model(...): its inputs, compute(), what to fit
    tests/            reference cases it must pass (textbook values, limits)
    physics.json      the data it is connected to (a folder, a Ledger, a
                      Connections graph) - Council reads that data and hands
                      it to compute() as `data`
    physics_kit/      a copy of this package, so the model runs on any Python
                      with numpy (and the Build tab's agent can read it)

Local models are weak at deriving physics from scratch, so models are built
FROM these blocks, not from memory:

    acoustics     notes and frequencies, pipe resonance with end correction,
                  struck bars and tubes, Helmholtz resonators, PVC pipe sizes
    antennas      wavelength, a thin-wire method-of-moments solver (dipoles,
                  monopoles, Yagis: impedance, resonance, gain, pattern)
    eddy          skin depth, plate losses at any thickness, induction
                  heating, magnetic braking (Reitz), crack-depth reach
    erosion       particle-laden water (Finnie), flowing water on soil and
                  sediment (Shields, excess shear), sliding wear (Archard)
    materials     a starter table of material properties, each with how sure
                  the number is
    units         small conversions (inch, °C, ...)

Everything is SI inside. Only numpy is needed (no scipy, no Council).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

__version__ = "1.0"

#: Units of the numbers in a Result.values entry, for display.
Number = float


@dataclass
class Input:
    """One thing the user sets. ``choices`` makes it a pick list; a number
    without ``choices`` gets a spin box from ``low`` to ``high``."""
    name: str
    default: Any
    unit: str = ""
    label: str = ""
    low: Optional[float] = None
    high: Optional[float] = None
    choices: Sequence[str] = ()
    help: str = ""

    def to_json(self) -> Dict[str, Any]:
        d = asdict(self)
        d["choices"] = list(self.choices)
        d["label"] = self.label or self.name.replace("_", " ")
        return d


@dataclass
class Fit:
    """A way to calibrate the model from measured data: an equation (text,
    with ``input`` the measured x) whose fitted parameter(s) become model
    inputs (``sets``: equation parameter -> model input name). Other names in
    the equation are the run's Result.fit_constants."""
    name: str
    equation: str
    input: str
    sets: Dict[str, str] = field(default_factory=dict)
    x_unit: str = "m"                 # the unit the equation wants its input in
    x_hint: str = ""                  # words to find the x column by
    y_hint: str = ""
    help: str = ""
    #: show this fit only when this input has this value ("pipe_type": "Stopped")
    when: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Chart:
    """A line or scatter chart: x values, and one or more named y series."""
    title: str
    x: Sequence[float]
    series: Dict[str, Sequence[float]]
    x_label: str = ""
    y_label: str = ""
    kind: str = "line"                # line | scatter | polar (x in degrees)
    log_x: bool = False
    log_y: bool = False

    def to_json(self) -> Dict[str, Any]:
        return {"title": self.title, "x": [_num(v) for v in self.x],
                "series": {k: [_num(v) for v in vs] for k, vs in self.series.items()},
                "x_label": self.x_label, "y_label": self.y_label, "kind": self.kind,
                "log_x": self.log_x, "log_y": self.log_y}


@dataclass
class Result:
    """What compute() returns. ``values``: name -> (number, unit) or a plain
    value; ``tables``: name -> list of row dicts (the design sheet first);
    ``notes``: assumptions and warnings, said plainly."""
    values: Dict[str, Any] = field(default_factory=dict)
    tables: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    charts: List[Chart] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    #: numbers a Fit's equation uses that come from this run (the speed of
    #: sound at the chosen temperature, the pipe's radius, ...)
    fit_constants: Dict[str, float] = field(default_factory=dict)

    def to_json(self) -> Dict[str, Any]:
        vals = {}
        for k, v in self.values.items():
            if isinstance(v, tuple) and len(v) == 2:
                vals[k] = {"value": _plain(v[0]), "unit": v[1]}
            else:
                vals[k] = {"value": _plain(v), "unit": ""}
        return {"values": vals,
                "tables": {k: [{c: _plain(x) for c, x in row.items()} for row in rows]
                           for k, rows in self.tables.items()},
                "charts": [c.to_json() for c in self.charts], "notes": list(self.notes),
                "fit_constants": {k: _num(v) for k, v in self.fit_constants.items()}}


def _plain(v: Any) -> Any:
    try:
        import numpy as np
        if isinstance(v, np.generic):
            v = v.item()
    except ImportError:                                   # pragma: no cover
        pass
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return None if v != v else ("∞" if v > 0 else "-∞")
    return v


def _num(v: Any) -> Any:
    v = float(v)
    return v if v == v and abs(v) != float("inf") else None


@dataclass
class Model:
    """A physics model: what it is for, what the user sets, how it computes,
    what it assumes, and how it can be calibrated from measurements."""
    name: str
    goal: str
    inputs: List[Input]
    compute: Callable[[Dict[str, Any], "Data"], Result]
    assumptions: List[str] = field(default_factory=list)
    limits: List[str] = field(default_factory=list)     # where it stops being trustworthy
    fits: List[Fit] = field(default_factory=list)
    #: what data helps it (shown when connecting data)
    data_help: str = ""

    def defaults(self) -> Dict[str, Any]:
        return {i.name: i.default for i in self.inputs}

    def run(self, inputs: Optional[Dict[str, Any]] = None,
            data: Optional["Data"] = None) -> Result:
        values = self.defaults()
        for k, v in (inputs or {}).items():
            if k in values:
                values[k] = _coerce(v, values[k])
        return self.compute(values, data or Data())

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "goal": self.goal,
                "inputs": [i.to_json() for i in self.inputs],
                "assumptions": list(self.assumptions), "limits": list(self.limits),
                "fits": [f.to_json() for f in self.fits], "data_help": self.data_help}


def _coerce(v: Any, like: Any) -> Any:
    """``v`` as the default's type (a spin box sends floats for an int)."""
    try:
        if isinstance(like, bool):
            return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes")
        if isinstance(like, int):
            return int(round(float(v)))
        if isinstance(like, float):
            return float(v)
    except (TypeError, ValueError):
        return like
    return v


class Data:
    """The data Council connected to the model (physics.json): tables (name ->
    list of row dicts, with units) and texts (what the Connections graph
    knows about the model's topic). Empty when nothing is connected."""

    def __init__(self, tables: Optional[Dict[str, Dict[str, Any]]] = None,
                 texts: Optional[Dict[str, str]] = None):
        self.tables = tables or {}
        self.texts = texts or {}

    @classmethod
    def from_json(cls, d: Optional[Dict[str, Any]]) -> "Data":
        d = d or {}
        return cls(d.get("tables") or {}, d.get("texts") or {})

    def __bool__(self) -> bool:
        return bool(self.tables or self.texts)

    def names(self) -> List[str]:
        return list(self.tables)

    def rows(self, table: str) -> List[Dict[str, Any]]:
        return list((self.tables.get(table) or {}).get("rows") or [])

    def columns(self, table: str) -> List[str]:
        return list((self.tables.get(table) or {}).get("columns") or [])

    def unit(self, table: str, column: str) -> str:
        return str(((self.tables.get(table) or {}).get("units") or {}).get(column, ""))

    def find_column(self, *words: str) -> Optional[tuple]:
        """(table, column) of the first column whose name holds every one of
        ``words`` (case ignored), or None."""
        want = [w.lower() for w in words if w]
        for t in self.tables:
            for c in self.columns(t):
                low = c.lower()
                if all(w in low for w in want):
                    return t, c
        return None

    def numbers(self, table: str, column: str) -> List[float]:
        out = []
        for r in self.rows(table):
            n = to_number(r.get(column))
            if n is not None:
                out.append(n)
        return out

    def pairs(self, table: str, x: str, y: str) -> List[tuple]:
        out = []
        for r in self.rows(table):
            a, b = to_number(r.get(x)), to_number(r.get(y))
            if a is not None and b is not None:
                out.append((a, b))
        return out


def to_number(v: Any) -> Optional[float]:
    """A cell as a float, or None ('12.5 mm' -> 12.5; '' -> None)."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v == v else None
    s = str(v).strip().replace(",", "")
    if not s:
        return None
    num = ""
    for ch in s:
        if ch.isdigit() or ch in "+-.eE":
            num += ch
        else:
            break
    try:
        return float(num)
    except ValueError:
        return None


__all__ = ["Input", "Fit", "Chart", "Result", "Model", "Data", "to_number"]
