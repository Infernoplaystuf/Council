# ============================================================
# splash.py  —  startup animation
# ============================================================
# A frameless window that shows a spinning cog wrapped around a
# flame for ~1.8 seconds before the main window takes over.
#
# Drawing approach:
#   • Tk Canvas. No external images.
#   • The cog is a polygon with alternating outer/inner radii that
#     visually reads as a gear with N teeth. Rotated by recomputing
#     its points each frame.
#   • The flame is three layered teardrop polygons (deep red,
#     ember orange, bright core) with a small jitter so it flickers.
#   • Frame rate: ~30 FPS via tk.after(33, ...).
#
# Usage:
#   from splash import show_splash
#   show_splash(parent, duration_ms=1800, on_done=lambda: ...)
# ============================================================

from __future__ import annotations

import math
import random
import tkinter as tk
from typing import Callable, Optional

import branding


# ============================================================
# Geometry helpers
# ============================================================

def _cog_points(cx: float, cy: float, outer_r: float, inner_r: float,
                teeth: int, rotation_rad: float) -> list:
    """
    Generate polygon vertices for a gear-like shape.

    Each tooth contributes 4 points (inner-base, outer-base, outer-tip,
    inner-tip) so the resulting polygon has 4 * teeth vertices and
    looks like a proper sawtooth gear instead of a star.
    """
    pts: list = []
    tooth_step = 2 * math.pi / teeth
    # Tooth occupies ~45% of the angular slot; the gap is the rest
    tooth_arc = tooth_step * 0.45
    gap_arc = tooth_step - tooth_arc
    for i in range(teeth):
        a0 = rotation_rad + i * tooth_step
        a1 = a0 + gap_arc / 2          # rising edge of tooth
        a2 = a1 + tooth_arc            # falling edge of tooth
        a3 = a2 + gap_arc / 2          # back to next inner base
        for a, r in ((a0, inner_r), (a1, outer_r), (a2, outer_r), (a3, inner_r)):
            pts.append(cx + r * math.cos(a))
            pts.append(cy + r * math.sin(a))
    return pts


def _flame_points(cx: float, cy_base: float, w: float, h: float,
                  jitter: float = 0.0) -> list:
    """
    Asymmetric flame teardrop pointing upward. `jitter` adds a small
    random offset to a few control points so the flame flickers.
    """
    j = jitter
    return [
        cx,                cy_base,                       # bottom centre
        cx - w * 0.45,     cy_base - h * 0.10,            # bottom-left
        cx - w * 0.50,     cy_base - h * 0.35 + j*4,
        cx - w * 0.30 + j, cy_base - h * 0.55,
        cx - w * 0.40,     cy_base - h * 0.75 - j*3,
        cx - w * 0.15,     cy_base - h * 0.90 - j*2,
        cx,                cy_base - h - j*3,             # tip
        cx + w * 0.18,     cy_base - h * 0.90 - j*2,
        cx + w * 0.40,     cy_base - h * 0.75 - j*3,
        cx + w * 0.30 - j, cy_base - h * 0.55,
        cx + w * 0.50,     cy_base - h * 0.35 + j*4,
        cx + w * 0.45,     cy_base - h * 0.10,
    ]


# ============================================================
# Splash window
# ============================================================

class SplashWindow(tk.Toplevel):
    """Frameless splash window with a spinning cog and flickering flame."""

    SIZE = 360                 # canvas square edge
    COG_TEETH = 16
    COG_OUTER = 150
    COG_INNER = 130
    COG_HUB_R = 70             # filled hub disc inside the teeth
    FRAME_MS = 33              # ~30 FPS
    ROT_PER_FRAME = math.pi / 60   # ~3 deg per frame → 1 turn / 4s

    def __init__(self, parent: tk.Tk,
                 duration_ms: int = 1800,
                 on_done: Optional[Callable[[], None]] = None,
                 manual: bool = False):
        super().__init__(parent)
        self.parent = parent
        self.on_done = on_done
        # Manual mode: the caller controls dismissal (via dismiss()) and
        # keeps the cog turning during a blocking section with pump().
        # Used to COVER the host window's construction — the splash goes
        # up first, spins one frame per build step while the Tk loop is
        # blocked, then the caller dismisses once the build is done and a
        # minimum display time has elapsed. Non-manual mode keeps the
        # original fire-and-forget behaviour (auto-dismiss after
        # duration_ms).
        self.manual = bool(manual)

        # Frameless, on-top
        self.overrideredirect(True)
        self.attributes("-topmost", True)

        # Theme
        try:
            self._t = branding.get_theme("dark")
        except Exception:
            self._t = {"bg": "#1a1414", "panel_bg": "#231a1a",
                       "fg": "#d4d4d4", "muted_fg": "#7a7575",
                       "accent": "#d32f2f"}
        bg = self._t["bg"]
        self.configure(bg=bg)

        # Centre on screen
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        x = (sw - self.SIZE) // 2
        y = (sh - self.SIZE) // 2 - 40
        self.geometry(f"{self.SIZE}x{self.SIZE + 60}+{x}+{y}")

        # Canvas for the animation
        self.canvas = tk.Canvas(self, width=self.SIZE, height=self.SIZE,
                                bg=bg, highlightthickness=0, bd=0)
        self.canvas.pack(side="top", fill="both", expand=False)

        # Title strip below the canvas
        try:
            title_fg     = self._t["fg"]
            subtitle_fg  = self._t["muted_fg"]
            product_name = branding.PRODUCT_NAME
            tagline      = branding.PRODUCT_TAGLINE
        except Exception:
            title_fg, subtitle_fg = "#d4d4d4", "#7a7575"
            product_name = "Data's Inferno"
            tagline = ""
        title_strip = tk.Frame(self, bg=bg, height=60)
        title_strip.pack(side="top", fill="x")
        tk.Label(title_strip, text=product_name,
                 font=("Segoe UI", 18, "bold"),
                 bg=bg, fg=title_fg
                 ).pack(pady=(2, 0))
        tk.Label(title_strip, text=tagline,
                 font=("Segoe UI", 9),
                 bg=bg, fg=subtitle_fg
                 ).pack()

        # Animation state
        self._rotation = 0.0
        self._frame = 0
        self._duration_ms = max(800, int(duration_ms))
        self._cancelled = False

        # Centre of the canvas
        self._cx = self.SIZE // 2
        self._cy = self.SIZE // 2

        # Pre-create the IDs so the animator can update vs delete+recreate
        self._cog_id   = None
        self._hub_id   = None
        self._flame_outer_id  = None
        self._flame_mid_id    = None
        self._flame_core_id   = None
        self._spark_ids = []

        # Begin animation. The after-driven loop spins the cog whenever
        # the Tk event loop is live. During a blocking host construction
        # the loop can't run — pump() advances frames in that window.
        self.after(10, self._animate)
        # Auto-close only in fire-and-forget mode. Manual callers dismiss
        # explicitly so the splash can cover an arbitrarily long load.
        if not self.manual:
            self.after(self._duration_ms, self._dismiss)

    # ---- Animation -------------------------------------------------

    def _animate(self):
        """Driven by the Tk after-loop: render one frame, reschedule."""
        if self._cancelled or not self.winfo_exists():
            return
        self._animate_once()
        self.after(self.FRAME_MS, self._animate)

    def pump(self):
        """Render ONE frame synchronously and flush the redraw, WITHOUT
        dispatching the event queue. Call this between steps of a long
        blocking operation (e.g. building the host window's tabs) so the
        cog keeps turning even though the Tk loop isn't running yet.
        update_idletasks() repaints but does not process input events,
        so it can't re-enter the caller's construction code."""
        if self._cancelled or not self.winfo_exists():
            return
        # Swallow ANY error — pump is a cosmetic frame advance during the
        # host's construction; it must never abort the build or destabilise
        # a fragile host event loop (e.g. Spyder's mixed Qt/Tk process).
        try:
            self._animate_once()
            self.update_idletasks()
        except Exception:
            pass

    def _animate_once(self):
        self._rotation += self.ROT_PER_FRAME
        self._frame += 1
        c = self.canvas
        t = self._t

        # ── Cog (gear ring) ─────────────────────────────────────
        cog_pts = _cog_points(
            self._cx, self._cy,
            outer_r=self.COG_OUTER,
            inner_r=self.COG_INNER,
            teeth=self.COG_TEETH,
            rotation_rad=self._rotation,
        )
        if self._cog_id is None:
            self._cog_id = c.create_polygon(
                cog_pts, fill=t["panel_bg"], outline=t["accent"], width=2,
                smooth=False,
            )
        else:
            c.coords(self._cog_id, *cog_pts)

        # ── Cog hub (the inner disc that holds the flame) ───────
        if self._hub_id is None:
            self._hub_id = c.create_oval(
                self._cx - self.COG_HUB_R, self._cy - self.COG_HUB_R,
                self._cx + self.COG_HUB_R, self._cy + self.COG_HUB_R,
                fill="#0a0808", outline=t["accent"], width=1,
            )

        # ── Flame layers ────────────────────────────────────────
        # A small jitter on each frame makes the flame flicker.
        jitter = random.uniform(-2.0, 2.0)
        # Pulsing scale — gentle 0.92 → 1.05 over a sine
        pulse = 1.0 + 0.06 * math.sin(self._frame / 6.0)

        flame_h = int(80 * pulse)
        flame_w = int(46 * pulse)
        flame_base_y = self._cy + 30  # so the flame sits inside the hub

        outer = _flame_points(self._cx, flame_base_y, flame_w, flame_h, jitter)
        mid   = _flame_points(self._cx, flame_base_y - 6,
                              int(flame_w * 0.65), int(flame_h * 0.78), jitter * 0.6)
        core  = _flame_points(self._cx, flame_base_y - 12,
                              int(flame_w * 0.35), int(flame_h * 0.55), jitter * 0.3)

        if self._flame_outer_id is None:
            self._flame_outer_id = c.create_polygon(outer, fill="#7a1818", outline="", smooth=True)
            self._flame_mid_id   = c.create_polygon(mid,   fill="#d04020", outline="", smooth=True)
            self._flame_core_id  = c.create_polygon(core,  fill="#f6c14a", outline="", smooth=True)
        else:
            c.coords(self._flame_outer_id, *outer)
            c.coords(self._flame_mid_id,   *mid)
            c.coords(self._flame_core_id,  *core)

        # ── Sparks rising off the flame ─────────────────────────
        # Cull old sparks
        for sid, ttl, dy in self._spark_ids:
            ttl -= 1
        self._spark_ids = [(sid, ttl - 1, dy) for sid, ttl, dy in self._spark_ids]
        # Move existing
        for sid, ttl, dy in list(self._spark_ids):
            if ttl <= 0:
                c.delete(sid)
            else:
                c.move(sid, 0, -dy)
        self._spark_ids = [(sid, ttl, dy) for sid, ttl, dy in self._spark_ids if ttl > 0]
        # Maybe spawn a new spark
        if random.random() < 0.4 and len(self._spark_ids) < 8:
            sx = self._cx + random.uniform(-flame_w * 0.3, flame_w * 0.3)
            sy = flame_base_y - flame_h + random.uniform(-4, 4)
            radius = random.choice([1, 1, 2])
            color = random.choice(["#f6c14a", "#ff8a3d", "#f5d178"])
            sid = c.create_oval(sx - radius, sy - radius, sx + radius, sy + radius,
                                 fill=color, outline="")
            self._spark_ids.append((sid, random.randint(8, 16),
                                     random.uniform(0.8, 1.6)))

    # ---- Dismiss ---------------------------------------------------

    def dismiss(self, on_done: Optional[Callable[[], None]] = None):
        """Public dismissal for manual-mode callers. If ``on_done`` is
        given it overrides the one set at construction."""
        if on_done is not None:
            self.on_done = on_done
        self._dismiss()

    def _dismiss(self):
        if self._cancelled:
            return
        self._cancelled = True
        try:
            cb = self.on_done
            self.destroy()
        except Exception:
            cb = self.on_done
        if cb:
            try: cb()
            except Exception: pass


# ============================================================
# Public entry point
# ============================================================

def show_splash(parent: tk.Tk,
                duration_ms: int = 1800,
                on_done: Optional[Callable[[], None]] = None,
                manual: bool = False) -> SplashWindow:
    """
    Show the splash window. `parent` should be a withdrawn root window;
    deiconify it inside `on_done` to reveal the main UI when the splash
    finishes.

    ``manual=True`` suppresses the auto-dismiss timer: the caller spins
    the cog through a blocking load with ``pump()`` and ends it with
    ``dismiss(on_done=...)`` once the load is done. Used to cover the
    host window's entire construction.
    """
    return SplashWindow(parent, duration_ms=duration_ms,
                        on_done=on_done, manual=manual)
