"""
Designs of experiments, numpy only.

Every design is made in CODED units first — each varied factor runs from
-1 (its low value) to +1 (its high value) — and then scaled to the real
values. Every point therefore lies inside [low, high], which the limit
checks rely on: a face-centred central composite is used (axial points on
the faces, alpha = 1) rather than the rotatable one whose star points fall
outside the range the engineer approved.

    full_factorial      every combination of each factor's levels
    fractional          2-level 2^(k-p) with standard generators
                        (k = 2..7; resolution III or better)
    latin_hypercube     n runs, each factor's range cut into n slices and
                        each slice used once; best of 50 by maximin
                        distance (seeded, so the same seed gives the same
                        plan)
    central_composite   face-centred: 2^k corners, 2k face points, centres
    box_behnken         mid-edge points (k >= 3), centres

`design_points(...)` returns coded rows; `scale` maps them to values.
"""
from __future__ import annotations

import itertools
from typing import Dict, List, Sequence

import numpy as np

DESIGNS = {
    "full_factorial": "Full factorial",
    "fractional_factorial": "Fractional factorial (2-level)",
    "latin_hypercube": "Latin hypercube",
    "central_composite": "Central composite (face-centred)",
    "box_behnken": "Box-Behnken",
}

#: Generators for 2^(k-p) designs: the extra factors as products of the
#: base factors (0-based indices of the base columns).
_GENERATORS: Dict[int, List[Sequence[int]]] = {
    2: [], 3: [],
    4: [(0, 1, 2)],                                   # 2^(4-1) IV, D=ABC
    5: [(0, 1, 2, 3)],                                # 2^(5-1) V, E=ABCD
    6: [(0, 1, 2), (1, 2, 3)],                        # 2^(6-2) IV
    7: [(0, 1, 2), (1, 2, 3), (0, 2, 3)],             # 2^(7-3) IV
}


class DesignError(ValueError):
    pass


def full_factorial(levels: Sequence[int]) -> np.ndarray:
    """Coded points for each factor at `levels[i]` evenly spaced levels."""
    if not levels:
        raise DesignError("a full factorial needs at least one varied factor")
    axes = []
    for n in levels:
        n = int(n)
        if n < 2:
            raise DesignError("each varied factor needs at least 2 levels")
        axes.append(np.linspace(-1.0, 1.0, n))
    return np.array(list(itertools.product(*axes)), dtype=float)


def fractional_factorial(k: int) -> np.ndarray:
    if k not in _GENERATORS:
        raise DesignError("a fractional factorial here takes 2 to 7 varied "
                          "factors")
    gens = _GENERATORS[k]
    base = k - len(gens)
    rows = np.array(list(itertools.product((-1.0, 1.0), repeat=base)))
    extra = [np.prod(rows[:, list(g)], axis=1) for g in gens]
    return np.column_stack([rows] + extra) if extra else rows


def latin_hypercube(k: int, n: int, seed: int = 0, tries: int = 50) -> np.ndarray:
    if k < 1:
        raise DesignError("a Latin hypercube needs at least one varied factor")
    if n < 2:
        raise DesignError("a Latin hypercube needs at least 2 runs")
    rng = np.random.default_rng(int(seed))
    best, best_d = None, -1.0
    for _ in range(max(1, tries)):
        cut = (np.arange(n)[:, None] + rng.random((n, k))) / n
        for j in range(k):
            cut[:, j] = cut[rng.permutation(n), j]
        pts = cut * 2.0 - 1.0
        diff = pts[:, None, :] - pts[None, :, :]
        dist = np.sqrt((diff ** 2).sum(-1))
        np.fill_diagonal(dist, np.inf)
        d = float(dist.min())
        if d > best_d:
            best, best_d = pts, d
    return best


def central_composite(k: int) -> np.ndarray:
    if k < 2:
        raise DesignError("a central composite design needs at least 2 "
                          "varied factors")
    corners = np.array(list(itertools.product((-1.0, 1.0), repeat=k)))
    faces = []
    for j in range(k):
        for s in (-1.0, 1.0):
            p = np.zeros(k)
            p[j] = s
            faces.append(p)
    return np.vstack([corners, np.array(faces)])


def box_behnken(k: int) -> np.ndarray:
    if k < 3:
        raise DesignError("a Box-Behnken design needs at least 3 varied "
                          "factors")
    rows = []
    for i, j in itertools.combinations(range(k), 2):
        for a, b in itertools.product((-1.0, 1.0), repeat=2):
            p = np.zeros(k)
            p[i], p[j] = a, b
            rows.append(p)
    return np.array(rows)


def design_points(design: str, k: int, *, levels: Sequence[int] = (),
                  runs: int = 0, seed: int = 0, center_points: int = 0
                  ) -> np.ndarray:
    """Coded points (rows) for `design` over k varied factors, centre
    points appended (not for a Latin hypercube or a full factorial with an
    odd level count, which already has its centre)."""
    if design == "full_factorial":
        pts = full_factorial(list(levels) or [2] * k)
    elif design == "fractional_factorial":
        pts = fractional_factorial(k)
    elif design == "latin_hypercube":
        pts = latin_hypercube(k, int(runs), seed)
    elif design == "central_composite":
        pts = central_composite(k)
    elif design == "box_behnken":
        pts = box_behnken(k)
    else:
        raise DesignError(f"{design!r} is not a design this plugin makes "
                          f"({', '.join(DESIGNS)})")
    if center_points and design != "latin_hypercube":
        pts = np.vstack([pts, np.zeros((int(center_points), k))])
    return pts


def scale(points: np.ndarray, lows: Sequence[float], highs: Sequence[float]
          ) -> np.ndarray:
    """Coded -1..+1 to real values, column by column."""
    lo = np.asarray(lows, dtype=float)
    hi = np.asarray(highs, dtype=float)
    return lo + (np.asarray(points, dtype=float) + 1.0) / 2.0 * (hi - lo)
