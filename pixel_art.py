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


PALETTES = {
    "Default":  PALETTE_DEFAULT,
    "NES":      PALETTE_NES,
    "Game Boy": PALETTE_GAMEBOY,
    "PICO-8":   PALETTE_PICO8,
}


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
