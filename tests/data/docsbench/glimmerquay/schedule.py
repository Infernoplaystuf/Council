"""
glimmerquay.schedule — periodic sampling windows.
"""
from __future__ import annotations

import math
from typing import List


class Cadence:
    """A repeating sampling window.

    Parameters
    ----------
    period_s : float
        Seconds between the starts of consecutive windows.
    jitter_pct : float, keyword-only, default 0.0
        Allowed timing jitter, as a percentage of the period (documentation
        only; windows are computed without jitter).
    anchor : {"epoch", "start"}, keyword-only, default "epoch"
        "epoch" aligns windows to multiples of the period counted from time
        0; "start" aligns them to the `start` given to windows().
    """

    def __init__(self, period_s: float, *, jitter_pct: float = 0.0,
                 anchor: str = "epoch"):
        if period_s <= 0:
            raise ValueError("period_s must be positive")
        if anchor not in ("epoch", "start"):
            raise ValueError("anchor must be 'epoch' or 'start'")
        self.period_s = float(period_s)
        self.jitter_pct = float(jitter_pct)
        self.anchor = anchor

    def windows(self, start: float, count: int) -> List[float]:
        """The start times of the first `count` windows at or after `start`.

        With anchor="epoch" the first window is the first multiple of
        period_s that is >= start; with anchor="start" it is `start` itself.
        """
        if self.anchor == "start":
            first = float(start)
        else:
            first = math.ceil(start / self.period_s) * self.period_s
        return [first + i * self.period_s for i in range(int(count))]


def next_window(cadence: Cadence, now: float) -> float:
    """The start time of the first window of `cadence` strictly after `now`."""
    nxt = cadence.windows(now, 2)
    return nxt[0] if nxt[0] > now else nxt[1]
