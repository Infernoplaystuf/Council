"""
The process parameters this plugin knows, their units, and a machine's
limits for them.

BASE UNITS
Every value is held in one unit per parameter so that the checks and the
derived energy densities never mix units: laser power in W, scan speed in
mm/s, hatch spacing and layer thickness in mm (so VED comes out in J/mm^3),
angles in degrees. `to_base(value, unit, parameter)` converts what a person
or a model wrote ("280 W", "1.2 m/s", "30 um") and refuses a unit that does
not fit the parameter.

A PROFILE is one machine + material: the allowed range of each parameter,
an optional VED window (a warning, not a block: it is a rule of thumb), and
the most specimens one build plate takes. Profiles live in the plugin's
data folder as profiles.json. The example shipped here has MADE-UP numbers
and says so; the engineer replaces them with the machine's real limits.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

PROFILES_FILE = "profiles.json"


@dataclass(frozen=True)
class Parameter:
    key: str
    label: str
    unit: str                     # base unit
    kind: str                     # "power" | "speed" | "length" | "angle"


PARAMETERS: Dict[str, Parameter] = {p.key: p for p in (
    Parameter("laser_power", "Laser power", "W", "power"),
    Parameter("scan_speed", "Scan speed", "mm/s", "speed"),
    Parameter("hatch_spacing", "Hatch spacing", "mm", "length"),
    Parameter("layer_thickness", "Layer thickness", "mm", "length"),
    Parameter("scan_rotation", "Scan rotation per layer", "deg", "angle"),
    Parameter("focus_offset", "Focus offset", "mm", "length"),
    Parameter("contour_power", "Contour laser power", "W", "power"),
    Parameter("contour_speed", "Contour scan speed", "mm/s", "speed"),
)}

#: Other names a person or model may use for a parameter.
ALIASES = {
    "power": "laser_power", "laser power": "laser_power", "p": "laser_power",
    "speed": "scan_speed", "scan speed": "scan_speed", "velocity": "scan_speed",
    "v": "scan_speed",
    "hatch": "hatch_spacing", "hatch spacing": "hatch_spacing",
    "hatch distance": "hatch_spacing", "h": "hatch_spacing",
    "layer": "layer_thickness", "layer thickness": "layer_thickness",
    "layer height": "layer_thickness", "t": "layer_thickness",
    "rotation": "scan_rotation", "scan rotation": "scan_rotation",
    "focus": "focus_offset", "focus offset": "focus_offset",
    "defocus": "focus_offset",
    "contour power": "contour_power", "contour speed": "contour_speed",
}

#: unit text -> (kind, factor to the base unit)
UNITS: Dict[str, Tuple[str, float]] = {
    "w": ("power", 1.0), "kw": ("power", 1000.0), "mw": ("power", 0.001),
    "mm/s": ("speed", 1.0), "m/s": ("speed", 1000.0), "mm/min": ("speed", 1 / 60),
    "um/s": ("speed", 0.001), "µm/s": ("speed", 0.001),
    "mm": ("length", 1.0), "um": ("length", 0.001), "µm": ("length", 0.001),
    "micron": ("length", 0.001), "microns": ("length", 0.001),
    "m": ("length", 1000.0), "cm": ("length", 10.0),
    "deg": ("angle", 1.0), "degree": ("angle", 1.0), "degrees": ("angle", 1.0),
    "°": ("angle", 1.0),
}


class UnitError(ValueError):
    pass


def parameter_key(name: str) -> str:
    """The parameter `name` means, or "" when it is not one this plugin
    knows."""
    text = str(name or "").strip().lower().replace("_", " ")
    key = text.replace(" ", "_")
    if key in PARAMETERS:
        return key
    return ALIASES.get(text, "")


def to_base(value: Any, unit: str, parameter: str) -> float:
    """`value` in `unit` converted to `parameter`'s base unit."""
    p = PARAMETERS[parameter]
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise UnitError(f"{p.label}: {value!r} is not a number") from None
    u = str(unit or p.unit).strip().lower().replace(" ", "")
    if u in ("", p.unit.lower()):
        return number
    if u not in UNITS:
        raise UnitError(f"{p.label}: unit {unit!r} is not one this plugin "
                        f"reads (use {p.unit})")
    kind, factor = UNITS[u]
    if kind != p.kind:
        raise UnitError(f"{p.label}: {unit!r} is a unit of {kind}, not of "
                        f"{p.kind}")
    return number * factor


def fmt(value: float, parameter: str) -> str:
    """'280 W', '0.03 mm (30 µm)'."""
    p = PARAMETERS[parameter]
    text = f"{value:g} {p.unit}"
    if p.unit == "mm" and 0 < abs(value) < 1:
        text += f" ({value * 1000:g} µm)"
    return text


# ----------------------------------------------------------------------
# Profiles
# ----------------------------------------------------------------------
EXAMPLE_PROFILE: Dict[str, Any] = {
    "name": "Example machine, Ti-6Al-4V (MADE-UP limits: replace with yours)",
    "machine": "Example LPBF machine",
    "material": "Ti-6Al-4V",
    "limits": {
        "laser_power": {"min": 100.0, "max": 400.0},
        "scan_speed": {"min": 400.0, "max": 2000.0},
        "hatch_spacing": {"min": 0.06, "max": 0.16},
        "layer_thickness": {"min": 0.02, "max": 0.06},
        "scan_rotation": {"min": 0.0, "max": 90.0},
        "focus_offset": {"min": -2.0, "max": 2.0},
    },
    "ved_window": [40.0, 120.0],
    "max_specimens": 60,
    "example": True,
}


def default_profiles() -> List[Dict[str, Any]]:
    return [copy.deepcopy(EXAMPLE_PROFILE)]


def profile_problem(profile: Dict[str, Any]) -> str:
    """Why a profile cannot be used, or ""."""
    if not isinstance(profile, dict):
        return "a profile is a JSON object"
    if not str(profile.get("name") or "").strip():
        return "the profile has no name"
    limits = profile.get("limits")
    if not isinstance(limits, dict) or not limits:
        return f"{profile.get('name')}: it lists no limits"
    for key, rng in limits.items():
        if key not in PARAMETERS:
            return f"{profile['name']}: {key!r} is not a parameter this plugin knows"
        try:
            lo, hi = float(rng["min"]), float(rng["max"])
        except (KeyError, TypeError, ValueError):
            return f"{profile['name']}: {key} needs a numeric min and max"
        if lo > hi:
            return f"{profile['name']}: {key} min {lo:g} is above max {hi:g}"
    window = profile.get("ved_window")
    if window is not None:
        try:
            lo, hi = (float(x) for x in window)
        except (TypeError, ValueError):
            return f"{profile['name']}: ved_window is [low, high] in J/mm^3"
        if lo > hi:
            return f"{profile['name']}: ved_window low is above high"
    try:
        if int(profile.get("max_specimens", 1)) < 1:
            return f"{profile['name']}: max_specimens must be at least 1"
    except (TypeError, ValueError):
        return f"{profile['name']}: max_specimens is a whole number"
    return ""


def load_profiles(ctx) -> List[Dict[str, Any]]:
    """The saved profiles, or the example when none are saved. A profile
    that does not check out is dropped from use (and listed by
    `problems`)."""
    data = ctx.load_json(PROFILES_FILE, None)
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), list):
        return default_profiles()
    good = [p for p in data["profiles"] if not profile_problem(p)]
    return good or default_profiles()


def problems(ctx) -> List[str]:
    data = ctx.load_json(PROFILES_FILE, None)
    if not isinstance(data, dict):
        return []
    return [profile_problem(p) for p in data.get("profiles") or []
            if profile_problem(p)]


def save_profiles(ctx, profiles: List[Dict[str, Any]]) -> None:
    for p in profiles:
        problem = profile_problem(p)
        if problem:
            raise ValueError(problem)
    ctx.save_json(PROFILES_FILE, {"format": 1, "profiles": profiles})


def by_name(profiles: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    for p in profiles:
        if p.get("name") == name:
            return p
    return None


def describe_profile(profile: Dict[str, Any]) -> str:
    """The limits as lines a model or a person reads."""
    lines = [f"Machine: {profile.get('machine', '')}; material: "
             f"{profile.get('material', '')}"]
    for key, rng in profile.get("limits", {}).items():
        p = PARAMETERS[key]
        lines.append(f"- {key} ({p.label}): {rng['min']:g} to {rng['max']:g} {p.unit}")
    if profile.get("ved_window"):
        lo, hi = profile["ved_window"]
        lines.append(f"- VED window (advice): {lo:g} to {hi:g} J/mm^3")
    lines.append(f"- at most {profile.get('max_specimens', 1)} specimens per build")
    return "\n".join(lines)
