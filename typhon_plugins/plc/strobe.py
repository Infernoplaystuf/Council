"""
The fan-blade strobe lock's arithmetic (docs/typhon_plc_trigger.md 9): no
sensor on the fan, so the camera runs on its OWN frame clock (TriggerMode
Off, a set frame rate: microsecond-steady, the lowest latency there is, as
no wire or network sits between the decision and the exposure), at a rate
that makes each picture K whole turns of the fan (or a whole number of
blade pitches, "any blade") after the last. The pictures themselves say how
far off that is: the blade's angle in each one (council_core.blade_phase)
drifts when the rate is a little off, and the lock nudges the camera's
frame rate to stop the drift and bring the blade back to the reference
spot.

The fit uses the camera's frame numbers (the frames CSV's index), not its
timestamps: a boost CXP camera stamps none. Pictures come from disk as the
run saves them, a picture or two late, so after each nudge the lock waits
for pictures taken after it before it fits again.

Stdlib only (a built-in plugin's rule); the pictures are read through
typhon_plugin_api.builtin.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

VIEW_FACE = "Face-on"
VIEW_SIDE = "Side-on"
VIEWS = (VIEW_FACE, VIEW_SIDE)
AXIS_X = "Left-right"
AXIS_Y = "Up-down"
AXES = (AXIS_X, AXIS_Y)

#: Pictures after a nudge before the next fit's pictures count (the ones
#: already on their way to disk were taken at the old rate).
SETTLE_FRAMES = 2
#: Side-on, the first nudge is a probe: this many degrees per picture of
#: extra turn, to measure how many pixels a degree is.
PROBE_DEG = 2.0
#: Nudges are kept within this fraction of the rate.
MAX_STEP = 0.02
#: Below this many pixels per degree the side-on box hardly sees the turn.
MIN_PX_PER_DEG = 0.05


def turns_per_picture(rpm: float, max_fps: float) -> int:
    """K: the fewest whole turns between pictures that keep the camera at
    or under max_fps."""
    if rpm <= 0 or max_fps <= 0:
        return 1
    return max(1, int(math.ceil((rpm / 60.0) / max_fps - 1e-9)))


def frame_rate(rpm: float, turns: int) -> float:
    """Pictures per second for K turns each."""
    return (rpm / 60.0) / max(1, int(turns)) if rpm > 0 else 0.0


def blur_deg(rpm: float, exposure_us: float) -> float:
    """How far the blade turns while the shutter is open."""
    return rpm * 6.0 * exposure_us / 1e6


def wrap(value: float, period: float) -> float:
    """value folded into (-period/2, period/2]."""
    if period <= 0:
        return value
    out = math.fmod(value + period / 2.0, period)
    if out <= 0:
        out += period
    return out - period / 2.0


def _fit(points: List[Tuple[int, float]]) -> Tuple[float, float]:
    """Least-squares (slope per frame, value at the last frame)."""
    n = len(points)
    xs = [float(i) for i, _ in points]
    ys = [v for _, v in points]
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = (sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
             if sxx > 0 else 0.0)
    return slope, my + slope * (xs[-1] - mx)


@dataclass
class Lock:
    """One run's lock. `add` each picture's measure (in degrees face-on, in
    pixels side-on) by its frame number; it answers a new frame rate when
    one is due, else None."""
    rate: float                      # pictures per second asked for now
    turns: int                       # K
    pitch_deg: float = 360.0         # the angle that looks the same
    target: float = 0.0              # where the blade should be (deg)
    tolerance_deg: float = 1.0
    gain: float = 0.6
    drift_gain: float = 0.7
    fit_frames: int = 5
    side: bool = False
    px_per_deg: Optional[float] = None    # side-on: None measures it
    min_score: float = 0.3
    max_jump: float = 0.0            # side-on: a bigger jump (px) is lost

    points: List[Tuple[int, float]] = field(default_factory=list)
    settle_after: int = -1
    last_index: int = -1
    nudges: int = 0
    measured: int = 0
    in_tolerance: int = 0
    low_score: int = 0
    errors_deg: List[float] = field(default_factory=list)
    slope_deg: Optional[float] = None    # drift per picture at last fit
    _probe: Optional[Tuple[float, float]] = None   # (slope px, expected deg)
    said: str = ""

    def _scale(self) -> float:
        return 1.0 if not self.side else (self.px_per_deg or 0.0)

    def error_deg(self, value: float) -> Optional[float]:
        """The picture's distance from the target, in degrees (None
        side-on until the scale is known)."""
        scale = self._scale()
        if scale == 0.0:
            return None
        deg = value / scale
        return wrap(deg - self.target, self.pitch_deg) if not self.side \
            else deg - self.target

    def add(self, index: int, value: float, score: float = 1.0
            ) -> Optional[float]:
        self.last_index = max(self.last_index, int(index))
        if score < self.min_score:
            self.low_score += 1
            return None
        self.measured += 1
        err = self.error_deg(value)
        if err is not None:
            self.errors_deg.append(err)
            if abs(err) <= self.tolerance_deg:
                self.in_tolerance += 1
        if index <= self.settle_after:
            return None
        if self.side and self.points and self.max_jump > 0 and \
                abs(value - self.points[-1][1]) > self.max_jump:
            self.said = ("the blades jump too far between side-on pictures:"
                         " set the rpm closer to the fan's (or lock "
                         "face-on)")
            self.points = []
            self._probe = None
        # unwrap face-on: each value nearest the last one
        if self.points and not self.side:
            prev = self.points[-1][1]
            value = prev + wrap(value - prev, self.pitch_deg)
        self.points.append((int(index), float(value)))
        if len(self.points) < max(2, self.fit_frames):
            return None
        return self._fit_and_nudge()

    def _fit_and_nudge(self) -> Optional[float]:
        slope, now = _fit(self.points)
        self.points = []
        k360 = 360.0 * max(1, self.turns)
        if self.side and self.px_per_deg is None:
            if self._probe is None:
                # Probe: turn PROBE_DEG more per picture to see how many
                # pixels that is.
                self._probe = (slope, PROBE_DEG)
                return self._set(self.rate * k360 / (k360 + PROBE_DEG))
            first, deg = self._probe
            self._probe = None
            scale = (slope - first) / deg
            if abs(scale) < MIN_PX_PER_DEG:
                self.said = ("the side-on box hardly moves with the fan: "
                             "make it longer, across the blades' path")
                self.px_per_deg = None
                return self._set(self.rate * (k360 + deg) / k360)
            self.px_per_deg = scale
        scale = self._scale()
        drift = slope / scale
        self.slope_deg = drift
        # Only part of the drift is taken out each time: one fit's slope
        # carries the pictures' noise, and the lock averages it away over
        # a few nudges.
        # A large drift is no noise: all of it goes at once.
        drift *= 1.0 if abs(drift) > 1.0 else self.drift_gain
        err = (wrap(now / scale - self.target, self.pitch_deg)
               if not self.side else now / scale - self.target)
        # The next fit comes about fit_frames + SETTLE_FRAMES pictures from
        # now: close `gain` of the error over that many.
        span = max(1, self.fit_frames + SETTLE_FRAMES)
        want = -self.gain * err / span
        new = self.rate * (k360 + drift) / (k360 + want)
        if abs(drift) < 1e-6 and abs(err) <= self.tolerance_deg * 0.25:
            return None
        return self._set(new)

    def _set(self, new: float) -> float:
        lo, hi = self.rate * (1 - MAX_STEP), self.rate * (1 + MAX_STEP)
        new = min(hi, max(lo, new))
        self.nudges += 1
        self.settle_after = self.last_index + SETTLE_FRAMES
        self.points = []
        return new

    def took(self, rate: float) -> None:
        """The rate the camera took (snapped to its step)."""
        if rate and rate > 0:
            self.rate = float(rate)

    def rpm(self) -> Optional[float]:
        """The fan's speed the lock has measured."""
        if self.slope_deg is None or self.rate <= 0:
            return None
        return self.rate * (self.turns + self.slope_deg / 360.0) * 60.0

    def summary(self) -> Dict[str, Any]:
        errs = [abs(e) for e in self.errors_deg[-20:]]
        rpm = self.rpm()
        return {"measured": self.measured, "in_tolerance": self.in_tolerance,
                "unclear": self.low_score, "nudges": self.nudges,
                "rate_fps": round(self.rate, 6), "turns": self.turns,
                "rpm_measured": None if rpm is None else round(rpm, 3),
                "recent_error_deg": (round(sorted(errs)[len(errs) // 2], 3)
                                     if errs else None),
                "px_per_deg": (None if self.px_per_deg is None
                               else round(self.px_per_deg, 4)),
                "words": self.said}
