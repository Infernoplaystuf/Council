"""
pixel_art.py — pixel art editor model + tools.

Pure model module. The GUI layer (council_gui_engine._build_pixel_art_tab)
owns the Tk widgets; this file owns:

  * ``PixelDocument`` — the canvas state (Pillow RGBA Image per frame,
    palette, current size, undo/redo history)
  * Drawing primitives — pencil / eraser / fill / line / rect, all
    with optional vertical-mirror symmetry
  * Palette presets — Default / NES / Game Boy / PICO-8
  * Save helpers — single-frame PNG and horizontal sprite-sheet PNG

Pillow is required. When the module imports without it, ``PIL_OK``
is False and the GUI shows an "install Pillow" message instead of
the editor.
"""

from __future__ import annotations

import colorsys
import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional, Tuple

try:
    from PIL import Image, ImageDraw
    PIL_OK = True
except Exception:
    Image = None
    ImageDraw = None
    PIL_OK = False


RGBA = Tuple[int, int, int, int]

# ============================================================
# Built-in palettes
# ============================================================
# Names map to lists of (r,g,b) tuples; the editor renders these as
# clickable swatches. Picking a palette also flips the colour picker
# to swatch mode (free RGB still available).

PALETTE_DEFAULT: List[Tuple[int, int, int]] = [
    (0, 0, 0), (255, 255, 255), (128, 128, 128),
    (255, 0, 0), (0, 255, 0), (0, 0, 255),
    (255, 255, 0), (255, 0, 255), (0, 255, 255),
    (139, 69, 19), (255, 165, 0), (75, 0, 130),
    (255, 192, 203), (160, 82, 45), (0, 100, 0),
    (240, 230, 140),
]


PALETTE_GAMEBOY: List[Tuple[int, int, int]] = [
    (15, 56, 15),     # darkest green
    (48, 98, 48),
    (139, 172, 15),
    (155, 188, 15),   # lightest green
]


# Pico-8 16-colour fixed palette
PALETTE_PICO8: List[Tuple[int, int, int]] = [
    (0,   0,   0),    (29,  43,  83),   (126, 37,  83),   (0,   135, 81),
    (171, 82,  54),   (95,  87,  79),   (194, 195, 199),  (255, 241, 232),
    (255, 0,   77),   (255, 163, 0),    (255, 236, 39),   (0,   228, 54),
    (41,  173, 255),  (131, 118, 156),  (255, 119, 168),  (255, 204, 170),
]


# A representative NES palette (56 distinct entries — the full hardware
# palette has 64 indices with duplicates).
PALETTE_NES: List[Tuple[int, int, int]] = [
    (124, 124, 124), (0,   0,   252), (0,   0,   188), (68,  40,  188),
    (148, 0,   132), (168, 0,   32),  (168, 16,  0),   (136, 20,  0),
    (80,  48,  0),   (0,   120, 0),   (0,   104, 0),   (0,   88,  0),
    (0,   64,  88),  (0,   0,   0),
    (188, 188, 188), (0,   120, 248), (0,   88,  248), (104, 68,  252),
    (216, 0,   204), (228, 0,   88),  (248, 56,  0),   (228, 92,  16),
    (172, 124, 0),   (0,   184, 0),   (0,   168, 0),   (0,   168, 68),
    (0,   136, 136),
    (248, 248, 248), (60,  188, 252), (104, 136, 252), (152, 120, 248),
    (248, 120, 248), (248, 88,  152), (248, 120, 88),  (252, 160, 68),
    (248, 184, 0),   (184, 248, 24),  (88,  216, 84),  (88,  248, 152),
    (0,   232, 216), (120, 120, 120),
    (252, 252, 252), (164, 228, 252), (184, 184, 248), (216, 184, 248),
    (248, 184, 248), (248, 164, 192), (240, 208, 176), (252, 224, 168),
    (248, 216, 120), (216, 248, 120), (184, 248, 184), (184, 248, 216),
    (0,   252, 252), (248, 216, 248), (255, 255, 255),
]


# ============================================================
# Colour ramps — the working tool of pixel art
# ============================================================
# A "ramp" is an ordered run of shades for ONE material: shadow →
# midtone → highlight. Naively darkening a colour (just dropping
# value) reads as muddy and lifeless. Real pixel art *hue-shifts*:
# shadows rotate toward the cool end (blue/purple) and highlights
# toward the warm end (yellow), while shadows gain a little
# saturation and highlights lose some. That single trick is most of
# what separates flat-looking sprites from ones with form.
#
# ``make_ramp`` implements exactly that, so the artist can pick one
# base colour for a shirt / rock / metal and get a usable shading run
# instantly.

# Hue anchors (HSV hue, 0..1) the shifts aim at.
_HUE_COOL = 0.72     # blue-violet — where shadows lean
_HUE_WARM = 0.13     # yellow-orange — where highlights lean


def _hue_toward(h: float, target: float, amount: float) -> float:
    """Rotate hue ``h`` toward ``target`` by ``amount``, along the
    shortest way around the colour wheel. Never overshoots."""
    if amount <= 0:
        return h % 1.0
    delta = (target - h) % 1.0
    if delta > 0.5:                 # shorter to go backwards
        delta -= 1.0
    step = max(-abs(delta), min(abs(delta), amount)) * (1 if delta >= 0 else -1)
    return (h + step) % 1.0


def make_ramp(
    base: Iterable[int],
    steps: int = 7,
    *,
    hue_shift: float = 0.055,
    sat_shift: float = 0.55,
) -> List[Tuple[int, int, int]]:
    """Build a hue-shifted shading ramp from one base colour.

    Returns ``steps`` RGB tuples ordered **dark → light**, with the
    base colour sitting at the middle. Shadows rotate toward
    blue-violet and gain saturation; highlights rotate toward
    yellow and lose it.

    ``hue_shift`` is the maximum hue rotation (in 0..1 hue units) at
    the ends of the ramp — 0 disables hue shifting and gives a plain
    light/dark run.
    """
    base = list(base)[:3]
    r, g, b = (max(0, min(255, int(v))) for v in base)
    h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    # Near-neutral bases have a meaningless hue. Give them a faint
    # tint so the cool-shadow / warm-highlight shift still reads —
    # coloured greys look far better than flat ones.
    if s < 0.06:
        h, s = _HUE_COOL, 0.10
    steps = max(2, int(steps))
    out: List[Tuple[int, int, int]] = []
    for i in range(steps):
        # t runs -1 (darkest) .. 0 (base) .. +1 (lightest)
        t = (i / (steps - 1)) * 2.0 - 1.0
        if t < 0:
            nv = v * (1.0 + 0.75 * t)                 # down to ~25% value
            ns = min(1.0, s * (1.0 - 0.25 * t))       # a touch richer
            nh = _hue_toward(h, _HUE_COOL, hue_shift * -t)
        else:
            nv = v + (1.0 - v) * 0.85 * t             # up toward white
            ns = s * (1.0 - sat_shift * t)            # wash out
            nh = _hue_toward(h, _HUE_WARM, hue_shift * t)
        nr, ng, nb = colorsys.hsv_to_rgb(nh, max(0.0, min(1.0, ns)),
                                          max(0.0, min(1.0, nv)))
        out.append((round(nr * 255), round(ng * 255), round(nb * 255)))
    return out


def _hsv(h: float, s: float, v: float) -> Tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return (round(r * 255), round(g * 255), round(b * 255))


def _spread(hues: int = 12, *, s: float, v: float) -> List[Tuple[int, int, int]]:
    """Even sweep around the colour wheel at fixed saturation/value."""
    return [_hsv(i / hues, s, v) for i in range(hues)]


def make_spectrum(hues: int = 12, shades: int = 6) -> List[Tuple[int, int, int]]:
    """A wide grid: every hue across several lightness steps, plus a
    neutral column. The 'show me everything' palette."""
    out: List[Tuple[int, int, int]] = []
    for i in range(hues):
        h = i / hues
        for j in range(shades):
            t = j / (shades - 1)
            # dark+saturated → light+washed
            out.append(_hsv(h, 1.0 - 0.55 * t, 0.32 + 0.68 * t))
    for j in range(shades):            # neutral ramp on the end
        t = j / (shades - 1)
        out.append(_hsv(0, 0, 0.05 + 0.95 * t))
    return out


# ── Themed palettes ───────────────────────────────────────────
PALETTE_PASTEL = (
    _spread(12, s=0.30, v=1.00) + _spread(12, s=0.18, v=0.97)
)

PALETTE_NEON = (
    _spread(12, s=1.00, v=1.00) + [(255, 255, 255), (12, 8, 20)]
)

PALETTE_MUTED = (
    _spread(12, s=0.42, v=0.62) + _spread(12, s=0.30, v=0.78)
)

PALETTE_GRAYSCALE = [_hsv(0, 0, i / 15) for i in range(16)]

# Warm/cool neutral ramps — greys with a temperature read better in
# game art than pure greys.
PALETTE_NEUTRALS = (
    make_ramp((120, 116, 128), 8)            # cool grey
    + make_ramp((128, 118, 104), 8)          # warm grey
)

PALETTE_SEPIA = make_ramp((150, 108, 66), 12, hue_shift=0.02)

PALETTE_EARTH = (
    make_ramp((124, 84, 48), 6)              # soil / bark
    + make_ramp((96, 112, 52), 6)            # moss / foliage
    + make_ramp((150, 138, 96), 6)           # dry grass / sand
    + make_ramp((104, 104, 112), 6)          # stone
)

# A deliberately broad range of skin tones — character art needs the
# full span, not a token few. Each is a full ramp so faces and hands
# can be shaded, not just filled.
PALETTE_SKIN = (
    make_ramp((255, 219, 186), 6)            # very light
    + make_ramp((241, 194, 155), 6)          # light
    + make_ramp((214, 160, 116), 6)          # medium
    + make_ramp((172, 116, 78), 6)           # tan
    + make_ramp((123, 78, 51), 6)            # deep
    + make_ramp((78, 48, 32), 6)             # very deep
)

PALETTE_FOLIAGE = (
    make_ramp((72, 132, 56), 7)              # leaf green
    + make_ramp((44, 92, 68), 7)             # pine
    + make_ramp((156, 168, 64), 7)           # sun-bleached
)

PALETTE_METAL = (
    make_ramp((132, 140, 152), 7)            # steel
    + make_ramp((186, 152, 66), 7)           # gold / brass
    + make_ramp((148, 96, 64), 7)            # copper
)

PALETTE_SKY = (
    make_ramp((92, 148, 220), 7)             # daylight blue
    + make_ramp((236, 140, 96), 7)           # sunset
    + make_ramp((44, 52, 96), 7)             # night
)

PALETTE_SPECTRUM = make_spectrum()


# ── Single-hue ramps ──────────────────────────────────────────
# One entry per common material hue. These are what you reach for
# when you know the colour you want and need its shades.
_RAMP_BASES = [
    ("Red",     (200, 58,  58)),
    ("Orange",  (216, 122, 48)),
    ("Yellow",  (224, 192, 62)),
    ("Green",   (78,  158, 66)),
    ("Teal",    (54,  156, 148)),
    ("Blue",    (58,  110, 200)),
    ("Indigo",  (78,  72,  168)),
    ("Purple",  (136, 72,  172)),
    ("Pink",    (216, 96,  150)),
    ("Brown",   (124, 84,  48)),
    ("Black",   (44,  44,  56)),
    ("White",   (232, 232, 240)),
]

_RAMP_PALETTES = {
    f"Ramp: {name}": make_ramp(rgb, 14) for name, rgb in _RAMP_BASES
}


# ============================================================
# Material kits — everything one structure needs, in one palette
# ============================================================
# Building a wooden wall means reaching for a dozen browns (different
# boards, weathering, grain) AND a set of neutrals for ambient
# occlusion, outlines and cast shadow. Hunting those across separate
# palettes is slow, so a "kit" gathers the whole material in one
# place, organised as one ramp per ROW: each row is a single wood /
# stone / metal, running dark → light.
#
# ``PALETTE_ROWS`` keeps that row structure (and the row names) so the
# editor can lay a kit out as clean gradient rows instead of a
# reflowed soup, and name the row you're hovering.

#: name -> [(row_label, [rgb, ...]), ...]
PALETTE_ROWS: dict = {}


def register_kit(name: str, rows: List[Tuple[str, List[Tuple[int, int, int]]]]) -> None:
    """Register a row-structured palette. Also flattens it into
    ``PALETTES`` so every existing code path keeps working."""
    PALETTE_ROWS[name] = rows
    PALETTES[name] = [c for _label, row in rows for c in row]


def shadow_row(tint: Iterable[int], steps: int = 12) -> List[Tuple[int, int, int]]:
    """A near-black → light-grey neutral run, faintly tinted toward the
    material's temperature. Tinted neutrals sit on a material far more
    convincingly than pure grey."""
    return make_ramp(tint, steps, hue_shift=0.03, sat_shift=0.75)


#: Kit definitions. Registered into ``PALETTES`` once that dict exists
#: (see the ``register_kit`` calls below the PALETTES declaration).
_KIT_DEFS: List[Tuple[str, List[Tuple[str, List[Tuple[int, int, int]]]]]] = [
    # Six wood species × 9 shades = 54 browns, plus warm-tinted
    # neutrals for shadow and accents (rope, iron nails, moss).
    ("Wooden Structure", [
        ("Pine",        make_ramp((196, 154, 104), 9)),
        ("Oak",         make_ramp((154, 110, 64), 9)),
        ("Walnut",      make_ramp((96, 64, 42), 9)),
        ("Mahogany",    make_ramp((128, 62, 40), 9)),
        ("Weathered",   make_ramp((140, 124, 108), 9)),
        ("Driftwood",   make_ramp((176, 166, 148), 9)),
        ("Shadow",      shadow_row((92, 80, 68))),
        ("Accents",     make_ramp((122, 96, 54), 5)        # rope / twine
                        + make_ramp((104, 100, 96), 5)     # iron nail
                        + make_ramp((96, 116, 68), 4)),    # moss
    ]),
    ("Stone Structure", [
        ("Granite",     make_ramp((140, 140, 146), 9)),
        ("Sandstone",   make_ramp((196, 168, 124), 9)),
        ("Slate",       make_ramp((96, 106, 124), 9)),
        ("Basalt",      make_ramp((72, 70, 76), 9)),
        ("Limestone",   make_ramp((206, 200, 184), 9)),
        ("Mossy",       make_ramp((118, 132, 104), 9)),
        ("Shadow",      shadow_row((76, 78, 86))),
        ("Accents",     make_ramp((92, 116, 84), 5)        # lichen
                        + make_ramp((62, 66, 74), 5)       # wet / crack
                        + make_ramp((168, 148, 120), 4)),  # mortar
    ]),
    ("Metal Structure", [
        ("Iron",        make_ramp((132, 140, 152), 9)),
        ("Gunmetal",    make_ramp((74, 78, 88), 9)),
        ("Steel",       make_ramp((176, 184, 196), 9)),
        ("Gold",        make_ramp((212, 172, 64), 9)),
        ("Brass",       make_ramp((180, 148, 78), 9)),
        ("Copper",      make_ramp((168, 102, 62), 9)),
        ("Rust",        make_ramp((140, 74, 42), 9)),
        ("Shadow",      shadow_row((70, 74, 84))),
        ("Accents",     make_ramp((92, 148, 132), 5)       # verdigris
                        + make_ramp((255, 196, 96), 5)     # hot spark
                        + make_ramp((44, 46, 54), 4)),     # deep cavity
    ]),
    # Fired clay runs warm and chalky; mortar is the quiet row that
    # makes brickwork read as courses instead of a flat wall.
    ("Brick & Clay", [
        ("Red brick",   make_ramp((156, 74, 56), 9)),
        ("Fired brick", make_ramp((108, 52, 44), 9)),
        ("Pale brick",  make_ramp((196, 148, 112), 9)),
        ("Terracotta",  make_ramp((188, 106, 68), 9)),
        ("Adobe",       make_ramp((176, 140, 100), 9)),
        ("Mortar",      make_ramp((172, 164, 148), 9)),
        ("Shadow",      shadow_row((88, 72, 66))),
        ("Accents",     make_ramp((96, 112, 68), 5)        # moss
                        + make_ramp((72, 64, 60), 5)       # soot
                        + make_ramp((208, 190, 168), 4)),  # chipped edge
    ]),
    # Thatch lives on deep shadow between the bundles — the shadow row
    # matters as much as the straw here.
    ("Thatch & Straw", [
        ("Fresh straw", make_ramp((206, 172, 96), 9)),
        ("Dry straw",   make_ramp((176, 142, 78), 9)),
        ("Aged thatch", make_ramp((124, 96, 54), 9)),
        ("Weathered",   make_ramp((144, 132, 108), 9)),
        ("Reed",        make_ramp((156, 156, 96), 9)),
        ("Shadow",      shadow_row((78, 64, 48))),
        ("Accents",     make_ramp((132, 106, 64), 5)       # rope binding
                        + make_ramp((92, 88, 62), 5)       # damp patch
                        + make_ramp((104, 124, 72), 4)),   # moss
    ]),
    # Glass is sold by its speculars, not its body colour — hence a
    # dedicated near-white highlight row alongside the tints.
    ("Glass & Crystal", [
        ("Clear",       make_ramp((196, 220, 226), 9)),
        ("Bottle green", make_ramp((108, 158, 124), 9)),
        ("Blue glass",  make_ramp((108, 152, 196), 9)),
        ("Amber",       make_ramp((198, 148, 76), 9)),
        ("Amethyst",    make_ramp((150, 118, 190), 9)),
        ("Rose quartz", make_ramp((204, 146, 156), 9)),
        ("Highlight",   make_ramp((236, 248, 252), 9)),
        ("Shadow",      shadow_row((68, 78, 92))),
    ]),
    ("Fabric & Cloth", [
        ("Linen",       make_ramp((206, 190, 160), 9)),
        ("Wool",        make_ramp((146, 140, 134), 9)),
        ("Dyed red",    make_ramp((164, 62, 62), 9)),
        ("Dyed blue",   make_ramp((66, 92, 158), 9)),
        ("Dyed green",  make_ramp((86, 124, 76), 9)),
        ("Leather",     make_ramp((128, 88, 56), 9)),
        ("Shadow",      shadow_row((80, 76, 78))),
        ("Accents",     make_ramp((196, 164, 72), 5)       # gold trim
                        + make_ramp((58, 52, 50), 5)       # dark thread
                        + make_ramp((222, 214, 200), 4)),  # stitching
    ]),
]


PALETTES = {
    # Curated / hardware
    "Default":       PALETTE_DEFAULT,
    "NES":           PALETTE_NES,
    "Game Boy":      PALETTE_GAMEBOY,
    "PICO-8":        PALETTE_PICO8,
    # Wide ranges
    "Spectrum":      PALETTE_SPECTRUM,
    "Pastels":       PALETTE_PASTEL,
    "Neons":         PALETTE_NEON,
    "Muted":         PALETTE_MUTED,
    "Grayscale":     PALETTE_GRAYSCALE,
    "Neutrals":      PALETTE_NEUTRALS,
    "Sepia":         PALETTE_SEPIA,
    # Subject kits
    "Skin tones":    PALETTE_SKIN,
    "Earth":         PALETTE_EARTH,
    "Foliage":       PALETTE_FOLIAGE,
    "Metal":         PALETTE_METAL,
    "Sky":           PALETTE_SKY,
}

# Material kits first — they're the workhorses for building assets.
for _kit_name, _kit_rows in _KIT_DEFS:
    register_kit(_kit_name, _kit_rows)

# Single-hue shading ramps.
PALETTES.update(_RAMP_PALETTES)

#: Name used for the ramp generated live from the artist's current
#: colour. The GUI rewrites this entry on demand.
CUSTOM_RAMP_NAME = "★ Ramp from current colour"
PALETTES[CUSTOM_RAMP_NAME] = make_ramp((128, 128, 160), 10)


def to_hex(rgb: Iterable[int]) -> str:
    """RGB tuple → ``#rrggbb`` for tk swatches."""
    rgb = list(rgb)
    r = max(0, min(255, int(rgb[0])))
    g = max(0, min(255, int(rgb[1])))
    b = max(0, min(255, int(rgb[2])))
    return f"#{r:02x}{g:02x}{b:02x}"


def hex_to_rgba(s: str, alpha: int = 255) -> RGBA:
    """``#rrggbb`` or ``#rrggbbaa`` → (r, g, b, a)."""
    s = s.lstrip("#")
    if len(s) == 6:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), alpha)
    if len(s) == 8:
        return (int(s[0:2], 16), int(s[2:4], 16),
                int(s[4:6], 16), int(s[6:8], 16))
    raise ValueError(f"not a hex color: {s!r}")


# ============================================================
# Frame + Document
# ============================================================

@dataclass
class PixelFrame:
    """One animation frame. Backs a Pillow RGBA Image of the document's
    canvas size."""
    image: Any                  # PIL.Image.Image (RGBA)
    name:  str = "frame"

    def copy(self) -> "PixelFrame":
        if not PIL_OK:
            raise RuntimeError("Pillow not installed")
        return PixelFrame(image=self.image.copy(), name=self.name)


@dataclass
class PixelDocument:
    """Editor state. One per editor instance.

    Owns the frames (one Pillow Image each), the current canvas
    size, palette selection, undo stack, and "dirty since last save"
    flag. The GUI mirrors state from here and writes back via the
    tool functions below.
    """
    width:    int = 32
    height:   int = 32
    frames:   List[PixelFrame] = field(default_factory=list)
    current:  int = 0           # index into frames
    palette:  str = "Default"
    symmetry: bool = False      # vertical mirror about x = width/2
    file_path: Optional[Path] = None
    dirty:    bool = False
    _history: List[bytes] = field(default_factory=list)
    _redo:    List[bytes] = field(default_factory=list)
    history_limit: int = 50

    def __post_init__(self) -> None:
        if not PIL_OK:
            return
        if not self.frames:
            self.frames.append(self._blank_frame("frame_1"))

    # ----------------------------------------------------------------
    # Frame helpers
    # ----------------------------------------------------------------

    def _blank_frame(self, name: str) -> PixelFrame:
        img = Image.new("RGBA", (self.width, self.height), (0, 0, 0, 0))
        return PixelFrame(image=img, name=name)

    @property
    def frame(self) -> PixelFrame:
        return self.frames[self.current]

    def resize_canvas(self, new_w: int, new_h: int) -> None:
        """Resize every frame to (new_w, new_h). Content is left-top
        aligned; truncated or padded with transparent as needed."""
        new_w = max(1, int(new_w))
        new_h = max(1, int(new_h))
        new_frames: List[PixelFrame] = []
        for fr in self.frames:
            big = Image.new("RGBA", (new_w, new_h), (0, 0, 0, 0))
            big.paste(fr.image, (0, 0))
            new_frames.append(PixelFrame(image=big, name=fr.name))
        self.frames = new_frames
        self.width  = new_w
        self.height = new_h
        self._history.clear()
        self._redo.clear()
        self.dirty = True

    def add_frame(self, *, duplicate: bool = True) -> None:
        if duplicate and self.frames:
            new = self.frame.copy()
            new.name = f"frame_{len(self.frames) + 1}"
        else:
            new = self._blank_frame(f"frame_{len(self.frames) + 1}")
        self.frames.append(new)
        self.current = len(self.frames) - 1
        self.dirty = True

    def remove_frame(self, idx: int) -> bool:
        if len(self.frames) <= 1 or idx < 0 or idx >= len(self.frames):
            return False
        del self.frames[idx]
        self.current = max(0, min(self.current, len(self.frames) - 1))
        self.dirty = True
        return True

    def go_frame(self, idx: int) -> None:
        if 0 <= idx < len(self.frames):
            self.current = idx

    # ----------------------------------------------------------------
    # Undo / redo — whole-frame snapshots
    # ----------------------------------------------------------------

    def snapshot(self) -> None:
        """Push the current frame's pixels to the undo stack. Call
        this BEFORE applying a destructive operation."""
        if not PIL_OK:
            return
        self._history.append(self.frame.image.tobytes())
        if len(self._history) > self.history_limit:
            self._history.pop(0)
        self._redo.clear()

    def undo(self) -> bool:
        if not self._history:
            return False
        cur_bytes = self.frame.image.tobytes()
        self._redo.append(cur_bytes)
        if len(self._redo) > self.history_limit:
            self._redo.pop(0)
        snap = self._history.pop()
        self.frame.image = Image.frombytes(
            "RGBA", (self.width, self.height), snap,
        )
        self.dirty = True
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        cur_bytes = self.frame.image.tobytes()
        self._history.append(cur_bytes)
        snap = self._redo.pop()
        self.frame.image = Image.frombytes(
            "RGBA", (self.width, self.height), snap,
        )
        self.dirty = True
        return True


# ============================================================
# Drawing primitives
# ============================================================

def _in_bounds(doc: PixelDocument, x: int, y: int) -> bool:
    return 0 <= x < doc.width and 0 <= y < doc.height


def _mirror_x(doc: PixelDocument, x: int) -> int:
    """Vertical-axis mirror — x' = width - 1 - x."""
    return doc.width - 1 - x


def _put_pixel(doc: PixelDocument, x: int, y: int, color: RGBA) -> None:
    """Single-pixel put with optional symmetry. Skips out-of-bounds."""
    if _in_bounds(doc, x, y):
        doc.frame.image.putpixel((x, y), color)
    if doc.symmetry:
        mx = _mirror_x(doc, x)
        if mx != x and _in_bounds(doc, mx, y):
            doc.frame.image.putpixel((mx, y), color)


def pencil(doc: PixelDocument, x: int, y: int, color: RGBA,
            *, size: int = 1) -> None:
    """Stamp a brush-sized rectangle of pixels centred at (x, y).
    Caller is responsible for ``doc.snapshot()`` before a stroke
    (multiple pencil calls per stroke share one undo entry)."""
    half = max(1, size) // 2
    for dy in range(-half, max(1, size) - half):
        for dx in range(-half, max(1, size) - half):
            _put_pixel(doc, x + dx, y + dy, color)
    doc.dirty = True


def eraser(doc: PixelDocument, x: int, y: int, *, size: int = 1) -> None:
    """Pencil-stamp transparent pixels."""
    pencil(doc, x, y, (0, 0, 0, 0), size=size)


def flood_fill(doc: PixelDocument, x: int, y: int, color: RGBA) -> None:
    """Standard 4-connected fill. No-op if start matches target."""
    if not _in_bounds(doc, x, y):
        return
    img = doc.frame.image
    target = img.getpixel((x, y))
    if target == color:
        return
    # Iterative stack to avoid recursion limit on large canvases
    stack = [(x, y)]
    seen: set = set()
    while stack:
        cx, cy = stack.pop()
        if (cx, cy) in seen:
            continue
        seen.add((cx, cy))
        if not _in_bounds(doc, cx, cy):
            continue
        if img.getpixel((cx, cy)) != target:
            continue
        _put_pixel(doc, cx, cy, color)
        stack.append((cx + 1, cy))
        stack.append((cx - 1, cy))
        stack.append((cx, cy + 1))
        stack.append((cx, cy - 1))
    doc.dirty = True


def line(doc: PixelDocument, x0: int, y0: int, x1: int, y1: int,
          color: RGBA, *, size: int = 1) -> None:
    """Bresenham line of pencil stamps."""
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    while True:
        pencil(doc, x0, y0, color, size=size)
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def rect(doc: PixelDocument, x0: int, y0: int, x1: int, y1: int,
          color: RGBA, *, filled: bool = False, size: int = 1) -> None:
    """Outlined or filled rectangle of pencil stamps."""
    xa, xb = sorted((x0, x1))
    ya, yb = sorted((y0, y1))
    if filled:
        for yy in range(ya, yb + 1):
            for xx in range(xa, xb + 1):
                _put_pixel(doc, xx, yy, color)
        doc.dirty = True
        return
    # Outline — four edges
    for xx in range(xa, xb + 1):
        _put_pixel(doc, xx, ya, color)
        _put_pixel(doc, xx, yb, color)
    for yy in range(ya, yb + 1):
        _put_pixel(doc, xa, yy, color)
        _put_pixel(doc, xb, yy, color)
    doc.dirty = True


def eyedropper(doc: PixelDocument, x: int, y: int) -> Optional[RGBA]:
    """Return the pixel under (x, y), or None if out of bounds."""
    if not _in_bounds(doc, x, y):
        return None
    return tuple(doc.frame.image.getpixel((x, y)))


# ============================================================
# Save / load
# ============================================================

def save_png(doc: PixelDocument, path: Any) -> Path:
    """Save the current frame as a PNG, native pixel size."""
    if not PIL_OK:
        raise RuntimeError("Pillow not installed")
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    doc.frame.image.save(p, "PNG")
    doc.file_path = p
    doc.dirty = False
    return p


def load_png(path: Any) -> PixelDocument:
    """Load a PNG into a single-frame document."""
    if not PIL_OK:
        raise RuntimeError("Pillow not installed")
    p = Path(path).expanduser()
    img = Image.open(p).convert("RGBA")
    doc = PixelDocument(width=img.width, height=img.height, frames=[])
    doc.frames.append(PixelFrame(image=img, name=p.stem))
    doc.file_path = p
    doc.dirty = False
    return doc


def save_sprite_sheet(doc: PixelDocument, path: Any) -> Path:
    """Save all frames horizontally concatenated as a single PNG."""
    if not PIL_OK:
        raise RuntimeError("Pillow not installed")
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    n = len(doc.frames)
    sheet = Image.new("RGBA", (doc.width * n, doc.height), (0, 0, 0, 0))
    for i, fr in enumerate(doc.frames):
        sheet.paste(fr.image, (doc.width * i, 0))
    sheet.save(p, "PNG")
    return p
