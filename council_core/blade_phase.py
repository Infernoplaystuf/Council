"""
council_core.blade_phase — where a fan's blade is in a picture, for the PLC
trigger plugin's strobe lock (typhon_plugins/plc/strobe.py;
docs/typhon_plc_trigger.md 9). Here, not in the plugin's folder, because it
needs numpy and Pillow and a built-in plugin imports only the plugin API:
the plugin reaches it through typhon_plugin_api.builtin.

TWO VIEWS
- Face-on (looking down the fan's axle): the picture is sampled on a ring
  around the hub (between two radii), giving the brightness all the way
  round, one value per angle step. Its circular cross-correlation with the
  reference picture's ring says how many degrees the fan has turned since
  the reference. With N alike blades and "any blade", the answer is folded
  into one blade's pitch (360/N degrees), so the blade nearest the
  reference spot counts; with "this blade", the whole turn counts (mark
  that blade, a strip of tape, or alike blades look the same).
- Side-on (the blades pass across the view): the picture's brightness in a
  box is averaged across the motion, giving a profile along it; its
  cross-correlation with the reference's says how many pixels the blades
  sit from where they were. The plugin turns pixels into degrees with a
  scale it measures itself (strobe.py).

The hub and the ring can be found from a few pictures of the spinning fan
(find_hub): the pixels that change from picture to picture are the blades,
a ring around the hub.

fan_picture draws a fan for the tests and the simulated camera.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence, Tuple

import numpy as np

VIEW_FACE = "Face-on"
VIEW_SIDE = "Side-on"
AXIS_X = "Left-right"
AXIS_Y = "Up-down"

#: Angle steps round the ring (a multiple of the blade count is used).
RING_BINS = 720
#: Radii sampled between the inner and outer radius.
RING_RADII = 24


@dataclass
class FanView:
    """Where to look in the picture."""
    view: str = VIEW_FACE
    hub_x: float = 0.0
    hub_y: float = 0.0
    ring_in: float = 0.0
    ring_out: float = 0.0
    blades: int = 1
    any_blade: bool = True
    box: Tuple[int, int, int, int] = (0, 0, 0, 0)     # x, y, w, h (side)
    axis: str = AXIS_X

    @property
    def pitch_deg(self) -> float:
        n = max(1, int(self.blades))
        return 360.0 / n if self.any_blade else 360.0

    def bins(self) -> int:
        n = max(1, int(self.blades))
        return n * int(math.ceil(RING_BINS / n))

    def problem(self) -> str:
        if self.view == VIEW_FACE:
            if self.ring_out <= self.ring_in or self.ring_in < 0:
                return ("set the ring: an outer radius bigger than the "
                        "inner one (Find hub sets both)")
        else:
            if self.box[2] < 8 or self.box[3] < 1:
                return "set the side-on box (at least 8 pixels along)"
        return ""


@dataclass
class Measure:
    value: float          # degrees (face-on) or pixels (side-on)
    score: float          # how alike the picture is to the reference, 0..1


def load_gray(path: Path) -> np.ndarray:
    from .bottle_position import load_gray as _load
    return _load(Path(path))


# -- face-on ----------------------------------------------------------------
def ring_profile(img: np.ndarray, view: FanView) -> np.ndarray:
    """Brightness round the ring, one value per angle step (angle 0 to the
    right of the hub, growing clockwise on the screen)."""
    bins = view.bins()
    theta = np.arange(bins) * (2.0 * np.pi / bins)
    radii = np.linspace(view.ring_in, view.ring_out, RING_RADII)
    xs = view.hub_x + radii[:, None] * np.cos(theta)[None, :]
    ys = view.hub_y + radii[:, None] * np.sin(theta)[None, :]
    h, w = img.shape[:2]
    xi = np.clip(np.rint(xs).astype(int), 0, w - 1)
    yi = np.clip(np.rint(ys).astype(int), 0, h - 1)
    inside = (xs >= 0) & (xs <= w - 1) & (ys >= 0) & (ys <= h - 1)
    vals = img[yi, xi].astype(np.float64)
    if not inside.any():
        raise ValueError("the ring is outside the picture")
    vals[~inside] = np.nan
    with np.errstate(all="ignore"):
        prof = np.nanmean(vals, axis=0)
    return np.nan_to_num(prof, nan=float(np.nanmean(vals)))


def _norm(a: np.ndarray) -> np.ndarray:
    a = np.asarray(a, dtype=np.float64)
    a = a - a.mean()
    s = float(np.sqrt((a * a).sum()))
    return a / s if s > 0 else a


def _peak(corr: np.ndarray, circular: bool) -> Tuple[float, float]:
    """Sub-step position of the highest value (parabola through three)."""
    k = int(np.argmax(corr))
    n = len(corr)
    if circular:
        left, right = corr[(k - 1) % n], corr[(k + 1) % n]
    else:
        if k == 0 or k == n - 1:
            return float(k), float(corr[k])
        left, right = corr[k - 1], corr[k + 1]
    den = left - 2 * corr[k] + right
    frac = 0.5 * (left - right) / den if den < 0 else 0.0
    return k + float(np.clip(frac, -0.5, 0.5)), float(corr[k])


def ring_turn(profile: Sequence[float], reference: Sequence[float],
              view: FanView) -> Measure:
    """Degrees the fan has turned from the reference, in (-pitch/2,
    pitch/2]: positive is clockwise on the screen."""
    a, b = _norm(profile), _norm(reference)
    if len(a) != len(b):
        raise ValueError("the reference was taken with another blade count: "
                         "take it again")
    bins = len(a)
    corr = np.real(np.fft.ifft(np.fft.fft(a) * np.conj(np.fft.fft(b))))
    n = max(1, int(view.blades)) if view.any_blade else 1
    step = bins // n
    folded = corr.reshape(n, step).sum(axis=0) / n if n > 1 else corr
    k, score = _peak(folded, circular=True)
    deg = k * 360.0 / bins
    return Measure(wrap(deg, view.pitch_deg), max(0.0, min(1.0, score)))


# -- side-on ----------------------------------------------------------------
def line_profile(img: np.ndarray, view: FanView) -> np.ndarray:
    x, y, w, h = (int(v) for v in view.box)
    part = img[max(0, y):y + h, max(0, x):x + w].astype(np.float64)
    if part.size == 0:
        raise ValueError("the side-on box is outside the picture")
    return part.mean(axis=0 if view.axis == AXIS_X else 1)


def line_shift(profile: Sequence[float], reference: Sequence[float]
               ) -> Measure:
    """Pixels the blades moved along the box from the reference (positive
    is right or down)."""
    a, b = _norm(profile), _norm(reference)
    if len(a) != len(b):
        raise ValueError("the reference was taken with another box: take "
                         "it again")
    n = len(a)
    size = 1 << int(math.ceil(math.log2(2 * n)))
    corr = np.real(np.fft.ifft(np.fft.fft(a, size) *
                               np.conj(np.fft.fft(b, size))))
    lags = np.concatenate([corr[-(n - 1):], corr[:n]]) if n > 1 else corr[:1]
    k, score = _peak(lags, circular=False)
    return Measure(k - (n - 1), max(0.0, min(1.0, score)))


# -- one picture ---------------------------------------------------------------
def profile_of(img: np.ndarray, view: FanView) -> np.ndarray:
    return ring_profile(img, view) if view.view == VIEW_FACE \
        else line_profile(img, view)


def reference(path: Path, view: FanView) -> List[float]:
    """The reference picture's profile, as plain numbers (kept in the
    project's settings)."""
    why = view.problem()
    if why:
        raise ValueError(why)
    return [round(float(v), 3) for v in profile_of(load_gray(path), view)]


def measure(path: Path, view: FanView, ref: Sequence[float]) -> Measure:
    return measure_image(load_gray(path), view, ref)


def measure_image(img: np.ndarray, view: FanView, ref: Sequence[float]
                  ) -> Measure:
    prof = profile_of(img, view)
    if view.view == VIEW_FACE:
        return ring_turn(prof, ref, view)
    return line_shift(prof, ref)


def wrap(value: float, period: float) -> float:
    """value folded into (-period/2, period/2]."""
    if period <= 0:
        return value
    out = math.fmod(value + period / 2.0, period)
    if out <= 0:
        out += period
    return out - period / 2.0


# -- the hub, from a few pictures of the spinning fan ---------------------------
def find_hub(paths: Sequence[Path]) -> Tuple[float, float, float, float]:
    """(hub x, hub y, inner radius, outer radius) in pixels, from the
    pixels that change between pictures (the blades). Needs pictures where
    the blades sit in different places: a few seconds of a run before the
    lock, or a free-running camera."""
    imgs = [load_gray(Path(p)) for p in paths]
    if len(imgs) < 3:
        raise ValueError("need at least 3 pictures of the spinning fan")
    return hub_of(imgs)


def hub_of(imgs: Sequence[np.ndarray]) -> Tuple[float, float, float, float]:
    stack = np.stack([np.asarray(i, dtype=np.float32) for i in imgs])
    step = max(1, int(max(stack.shape[1:]) // 512))
    var = stack[:, ::step, ::step].std(axis=0)
    cut = float(np.percentile(var, 90))
    if cut <= 0:
        raise ValueError("nothing changes between these pictures: is the "
                         "fan turning, and the pictures from one run?")
    weights = np.where(var >= cut * 0.5, var, 0.0)
    ys, xs = np.nonzero(weights)
    wv = weights[ys, xs]
    cx = float((xs * wv).sum() / wv.sum())
    cy = float((ys * wv).sum() / wv.sum())
    dist = np.hypot(xs - cx, ys - cy)
    r_in = float(np.percentile(dist, 20))
    r_out = float(np.percentile(dist, 95))
    return cx * step, cy * step, r_in * step, r_out * step


# -- a drawn fan (tests, the simulated camera) ----------------------------------
def fan_picture(size: Tuple[int, int] = (320, 240), angle_deg: float = 0.0,
                blades: int = 3, view: str = VIEW_FACE, mark: bool = False,
                noise: float = 0.0, seed: int = 0) -> np.ndarray:
    """An 8-bit gray fan turned `angle_deg` clockwise on the screen. Face-on:
    bright blades on a dark ground round a hub in the middle. Side-on: the
    blades' edges seen across the view (their x is r·sin of their angle).
    `mark` brightens blade 0's tip (the "this blade" tape)."""
    w, h = size
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    out = np.full((h, w), 30.0)
    cx, cy = w / 2.0, h / 2.0
    r_max = 0.45 * min(w, h)
    pitch = 360.0 / max(1, blades)
    if view == VIEW_FACE:
        ang = np.degrees(np.arctan2(yy - cy, xx - cx))
        rad = np.hypot(xx - cx, yy - cy)
        for k in range(max(1, blades)):
            centre = angle_deg + k * pitch
            # a blade widening toward its tip, with a swept leading edge
            half = 6.0 + 14.0 * (rad / r_max)
            d = (ang - centre - 8.0 * (rad / r_max) + 180.0) % 360.0 - 180.0
            on = (np.abs(d) <= half) & (rad <= r_max) & (rad >= 0.12 * r_max)
            out[on] = 200.0 - 40.0 * (d[on] / half[on])
            if mark and k == 0:
                tip = on & (rad > 0.8 * r_max)
                out[tip] = 255.0
        out[np.hypot(xx - cx, yy - cy) < 0.12 * r_max] = 120.0
    else:
        for k in range(max(1, blades)):
            a = math.radians(angle_deg + k * pitch)
            if math.cos(a) <= 0:          # behind the hub: hidden
                continue
            x = cx + r_max * math.sin(a)
            width = 4.0 + 10.0 * math.cos(a)
            on = np.abs(xx - x) <= width
            out[on] = 190.0 - 50.0 * ((xx[on] - x) / width)
    if noise > 0:
        rng = np.random.default_rng(seed)
        out = out + rng.normal(0.0, noise, out.shape)
    return np.clip(out, 0, 255).astype(np.uint8)
