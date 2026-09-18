"""
council_core.splash_art — the spinning cog and its flame, as numbers.

A frameless window showing a gear wrapped around a flame. All of it is drawn
rather than loaded: no image files, so no DPI variants and nothing to ship.

WHY THIS IS A SHARED MODULE AND NOT TWO DRAWINGS
The artwork is geometry plus an animation clock, and neither needs a toolkit.
What differs between the two front ends is only HOW a polygon reaches the
screen: Tk keeps canvas items and moves them with coords(), Qt repaints. So the
points live here and each front end paints them its own way, through the same
five-method painter protocol the Designer canvas already uses.

THE FLAME FLICKERS BECAUSE IT IS JITTERED, NOT BECAUSE IT IS ANIMATED
Three layered teardrops — deep red, ember orange, bright core — each offset by
a small random amount per frame, over a gentle sine pulse. Sparks spawn at the
tip, rise, and expire. That is the whole effect; there is no asset behind it.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any, List, Tuple

#: The square the artwork is drawn in.
SIZE = 360

COG_TEETH = 16
COG_OUTER = 150
COG_INNER = 130
#: The filled disc inside the teeth that the flame sits in.
COG_HUB_R = 70

#: ~30 FPS, and ~3 degrees a frame — one turn every four seconds. Slow enough
#: to read as deliberate rather than as a loading spinner in distress.
FRAME_MS = 33
ROT_PER_FRAME = math.pi / 60

#: The flame, in the hub.
FLAME_H, FLAME_W = 80, 46
FLAME_OFFSET_Y = 30

#: Deep red, ember, core.
FLAME_COLOURS = ("#7a1818", "#d04020", "#f6c14a")
SPARK_COLOURS = ("#f6c14a", "#ff8a3d", "#f5d178")
HUB_FILL = "#0a0808"

MAX_SPARKS = 8


def cog_points(cx: float, cy: float, outer_r: float, inner_r: float,
               teeth: int, rotation_rad: float) -> List[float]:
    """Polygon vertices for a gear.

    Each tooth contributes four points — inner base, outer base, outer tip,
    inner tip — so the polygon has 4 x teeth vertices and reads as a proper
    sawtooth gear rather than as a star.
    """
    points: List[float] = []
    tooth_step = 2 * math.pi / teeth
    # The tooth takes ~45% of its angular slot; the gap is the rest.
    tooth_arc = tooth_step * 0.45
    gap_arc = tooth_step - tooth_arc
    for index in range(teeth):
        a0 = rotation_rad + index * tooth_step
        a1 = a0 + gap_arc / 2          # rising edge
        a2 = a1 + tooth_arc            # falling edge
        a3 = a2 + gap_arc / 2          # back to the next inner base
        for angle, radius in ((a0, inner_r), (a1, outer_r),
                              (a2, outer_r), (a3, inner_r)):
            points.append(cx + radius * math.cos(angle))
            points.append(cy + radius * math.sin(angle))
    return points


def flame_points(cx: float, cy_base: float, w: float, h: float,
                 jitter: float = 0.0) -> List[float]:
    """An asymmetric teardrop pointing upward.

    `jitter` offsets a few control points so the flame flickers. Asymmetric on
    purpose: a symmetric flame reads as a logo, not as fire.
    """
    j = jitter
    return [
        cx, cy_base,                                     # bottom centre
        cx - w * 0.45, cy_base - h * 0.10,               # bottom-left
        cx - w * 0.50, cy_base - h * 0.35 + j * 4,
        cx - w * 0.30 + j, cy_base - h * 0.55,
        cx - w * 0.40, cy_base - h * 0.75 - j * 3,
        cx - w * 0.15, cy_base - h * 0.90 - j * 2,
        cx, cy_base - h - j * 3,                         # tip
        cx + w * 0.18, cy_base - h * 0.90 - j * 2,
        cx + w * 0.40, cy_base - h * 0.75 - j * 3,
        cx + w * 0.30 - j, cy_base - h * 0.55,
        cx + w * 0.50, cy_base - h * 0.35 + j * 4,
        cx + w * 0.45, cy_base - h * 0.10,
    ]


@dataclass
class Spark:
    x: float
    y: float
    radius: int
    colour: str
    #: Frames left before it expires.
    ttl: int
    #: Pixels risen per frame.
    speed: float


class Animation:
    """The clock. One `advance()` per frame; `paint()` whenever asked.

    Separate from the painting so a test can run a thousand frames without a
    display and assert the thing never drifts — the sparks in particular are
    the sort of list that grows without bound if culling is written wrong.
    """

    def __init__(self, size: int = SIZE, rng: Any = None):
        self.size = size
        self.cx = self.cy = size / 2
        self.rotation = 0.0
        self.frame = 0
        self.sparks: List[Spark] = []
        #: Injectable so a test gets the same picture twice.
        self._rng = rng or random.Random()

    # ------------------------------------------------------------------
    def advance(self) -> None:
        self.rotation += ROT_PER_FRAME
        self.frame += 1
        self._age_sparks()
        self._maybe_spawn_spark()

    def _age_sparks(self) -> None:
        for spark in self.sparks:
            spark.ttl -= 1
            spark.y -= spark.speed
        # Culled every frame rather than on a timer: an uncapped list on a
        # 30 FPS loop is an unbounded leak in a window that may be up for the
        # whole of a slow model load.
        self.sparks = [s for s in self.sparks if s.ttl > 0]

    def _maybe_spawn_spark(self) -> None:
        if self._rng.random() >= 0.4 or len(self.sparks) >= MAX_SPARKS:
            return
        width, height = self.flame_size()
        self.sparks.append(Spark(
            x=self.cx + self._rng.uniform(-width * 0.3, width * 0.3),
            y=self.flame_base_y() - height + self._rng.uniform(-4, 4),
            radius=self._rng.choice([1, 1, 2]),
            colour=self._rng.choice(SPARK_COLOURS),
            ttl=self._rng.randint(8, 16),
            speed=self._rng.uniform(0.8, 1.6)))

    # ------------------------------------------------------------------
    def pulse(self) -> float:
        """A gentle 0.94 → 1.06 over a sine. Breathing, not blinking."""
        return 1.0 + 0.06 * math.sin(self.frame / 6.0)

    def flame_size(self) -> Tuple[int, int]:
        pulse = self.pulse()
        return int(FLAME_W * pulse), int(FLAME_H * pulse)

    def flame_base_y(self) -> float:
        return self.cy + FLAME_OFFSET_Y

    # ------------------------------------------------------------------
    def paint(self, surface: Any, accent: str, panel_bg: str) -> None:
        """One frame, through the five-method painter protocol.

        The same protocol the Designer canvas uses, which is why the Qt splash
        needed no drawing code of its own.
        """
        surface.create_polygon(
            *cog_points(self.cx, self.cy, COG_OUTER, COG_INNER,
                        COG_TEETH, self.rotation),
            fill=panel_bg, outline=accent, width=2)
        surface.create_oval(self.cx - COG_HUB_R, self.cy - COG_HUB_R,
                            self.cx + COG_HUB_R, self.cy + COG_HUB_R,
                            fill=HUB_FILL, outline=accent, width=1)

        jitter = self._rng.uniform(-2.0, 2.0)
        width, height = self.flame_size()
        base = self.flame_base_y()
        layers = (
            (base, width, height, jitter),
            (base - 6, int(width * 0.65), int(height * 0.78), jitter * 0.6),
            (base - 12, int(width * 0.35), int(height * 0.55), jitter * 0.3),
        )
        for colour, (y, w, h, j) in zip(FLAME_COLOURS, layers):
            surface.create_polygon(*flame_points(self.cx, y, w, h, j),
                                   fill=colour, outline="", smooth=True)

        for spark in self.sparks:
            surface.create_oval(spark.x - spark.radius, spark.y - spark.radius,
                                spark.x + spark.radius, spark.y + spark.radius,
                                fill=spark.colour, outline="")
