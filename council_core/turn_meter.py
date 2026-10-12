"""
council_core.turn_meter — what one question cost: model calls, model time,
and model loads.

A council question is many model calls, and on a machine that cannot hold
every model it serves, each switch between models unloads one and loads
another — seconds to tens of seconds each, invisible in the transcript. The
meter listens to the engine's per-call report (council_engine
add_stats_listener, the same report usage_log keeps) for the length of one
question and says, at the end:

    This question: 14 model calls, 96 s of model time in 41 s;
    models loaded 5 times (38 s) — they are swapping on this PC.

The listener is process-wide, so a call made by something else during the
question (a background job) is counted too; the meter says what it saw.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

LOAD_S = 0.5


@dataclass
class Meter:
    calls: int = 0
    seconds: float = 0.0
    wall: float = 0.0
    loads: int = 0
    load_s: float = 0.0
    loaded: Dict[str, int] = field(default_factory=dict)
    by_role: Dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"calls": self.calls, "seconds": round(self.seconds, 1),
                "wall": round(self.wall, 1), "loads": self.loads,
                "load_s": round(self.load_s, 1), "loaded": dict(self.loaded),
                "by_role": dict(self.by_role)}

    @property
    def swapping(self) -> bool:
        """Two or more models each loaded, more loads than models: they are
        pushing each other out."""
        return len(self.loaded) >= 2 and self.loads > len(self.loaded)

    def note(self) -> str:
        if not self.calls:
            return ""
        line = (f"This question: {self.calls} model call(s), "
                f"{self.seconds:.0f} s of model time in {self.wall:.0f} s")
        if self.loads:
            line += (f"; models loaded {self.loads} time(s) "
                     f"({self.load_s:.0f} s)")
            if self.swapping:
                line += (" — they are swapping in and out of memory. Give "
                         "these roles one model, or move one to another "
                         "machine (Council Map ▸ Placement review)")
        return line + "."


class TurnMeter:
    """with TurnMeter() as m: … ; m.result is the Meter."""

    def __init__(self) -> None:
        self.result = Meter()
        self._lock = threading.Lock()
        self._t0 = 0.0
        self._on = False

    def _listen(self, stats: Dict[str, Any]) -> None:
        with self._lock:
            r = self.result
            r.calls += 1
            r.seconds += float(stats.get("seconds") or 0.0)
            role = str(stats.get("role") or "?")
            r.by_role[role] = r.by_role.get(role, 0) + 1
            load = float(stats.get("load_s") or 0.0)
            if load >= LOAD_S:
                r.loads += 1
                r.load_s += load
                m = str(stats.get("model") or "?")
                r.loaded[m] = r.loaded.get(m, 0) + 1

    def __enter__(self) -> "TurnMeter":
        self._t0 = time.monotonic()
        try:
            import council_engine
            council_engine.add_stats_listener(self._listen)
            self._on = True
        except Exception:                                 # noqa: BLE001
            self._on = False
        return self

    def __exit__(self, *exc: Any) -> None:
        self.result.wall = time.monotonic() - self._t0
        if self._on:
            try:
                import council_engine
                council_engine.remove_stats_listener(self._listen)
            except Exception:                             # noqa: BLE001
                pass


__all__ = ["Meter", "TurnMeter"]
