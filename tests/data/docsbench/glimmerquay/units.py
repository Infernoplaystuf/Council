"""
glimmerquay.units — voltage units and gain presets.
"""
from __future__ import annotations

from typing import Iterable, List

#: Gain multipliers by preset name: "low" is 0.5, "mid" is 2.0 and "high"
#: is 8.25.
GAIN_PRESETS = {"low": 0.5, "mid": 2.0, "high": 8.25}

_TO_MV = {"V": 1000.0, "mV": 1.0, "uV": 0.001, "kV": 1_000_000.0}


def to_millivolts(value: float, unit: str = "V") -> float:
    """Convert `value` in `unit` to millivolts.

    The accepted units are "V" (the default), "mV", "uV" (microvolts) and
    "kV". Any other unit raises ValueError. Unit names are case-sensitive.
    """
    try:
        return float(value) * _TO_MV[unit]
    except KeyError:
        raise ValueError(f"unknown unit {unit!r}; use V, mV, uV or kV") \
            from None


def apply_gain(samples: Iterable[float], preset: str = "mid") -> List[float]:
    """Multiply every sample by the gain of a preset from GAIN_PRESETS.

    The default preset is "mid" (2.0). The "high" preset applies a gain of
    8.25 and "low" a gain of 0.5. An unknown preset raises KeyError.
    """
    gain = GAIN_PRESETS[preset]
    return [float(s) * gain for s in samples]
