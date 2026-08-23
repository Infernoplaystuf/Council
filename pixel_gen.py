"""
pixel_gen.py — procedural pixel-art idea generator.

Makes *inspiration* sprites and material tiles algorithmically: no
model, no training data, no network. Every pixel here comes out of
seeded maths and the palettes in ``pixel_art``, so it carries none of
the provenance questions that generative image models do — and it runs
instantly, offline, forever.

What it is good at is volume and variation: "give me 24 weapon
silhouettes / door variants / brick tilings, let me pick one and draw
over it." What it is not is a finished illustration. The intended
workflow is generate → pick → load into the canvas → paint by hand.

Two families:

  * ``gen_sprite`` — mirrored cellular silhouettes (the classic
    roguelike sprite trick): seed a half-grid, smooth it into coherent
    mass, mirror it, then outline and top-light it with a colour ramp.
  * ``gen_tile`` — seamless material tiles (brick courses, planks,
    stone blocks, thatch, woven cloth, glass panes) drawn straight
    from the material kits, so a generated brick tile already matches
    the Brick & Clay palette you will shade it with.

Both are deterministic: the same ``seed`` always yields the same
image, so a variation you like can be regenerated at a larger size.
"""

from __future__ import annotations

import random
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from PIL import Image
    PIL_OK = True
except Exception:
    Image = None
    PIL_OK = False

import pixel_art as _px

RGB = Tuple[int, int, int]
RGBA = Tuple[int, int, int, int]

TRANSPARENT: RGBA = (0, 0, 0, 0)


# ============================================================
# Palette helpers
# ============================================================

def ramp_for(palette_name: str, row: Optional[str] = None) -> List[RGB]:
    """Pick a usable dark→light ramp out of a palette.

    For a material kit, ``row`` names the material ("Oak"); without one
    a non-shadow row is chosen. For a flat palette the whole list is
    used, which still reads as a ramp for the generated ``Ramp:`` ones.
    """
    rows = getattr(_px, "PALETTE_ROWS", {}).get(palette_name)
    if rows:
        if row:
            for label, colours in rows:
                if label == row:
                    return list(colours)
        # Prefer an actual material over the Shadow/Accents rows.
        for label, colours in rows:
            if label not in ("Shadow", "Accents", "Highlight"):
                return list(colours)
        return list(rows[0][1])
    flat = _px.PALETTES.get(palette_name) or _px.PALETTE_DEFAULT
    return list(flat)


def _shade(ramp: Sequence[RGB], t: float) -> RGB:
    """Sample a ramp at 0..1 (dark→light), clamped."""
    if not ramp:
        return (128, 128, 128)
    i = int(round(max(0.0, min(1.0, t)) * (len(ramp) - 1)))
    return tuple(ramp[i])  # type: ignore[return-value]


def _kit_row(palette_name: str, label: str,
             fallback: Optional[str] = None) -> List[RGB]:
    """Fetch a named row from a kit, falling back sensibly."""
    rows = getattr(_px, "PALETTE_ROWS", {}).get(palette_name)
    if rows:
        for lbl, colours in rows:
            if lbl == label:
                return list(colours)
        if fallback:
            return _kit_row(palette_name, fallback)
    return ramp_for(palette_name)


# ============================================================
# Sprite generation — mirrored cellular silhouettes
# ============================================================

def _grow_mask(w: int, h: int, rng: random.Random,
               density: float, smooth: int) -> List[List[bool]]:
    """Seed a half-width grid and smooth it into coherent mass.

    Cells nearer the vertical centre and the middle rows are more
    likely to be solid, which keeps the silhouette from fraying into
    disconnected noise at the edges.
    """
    half = (w + 1) // 2
    grid = [[False] * half for _ in range(h)]
    # Leave a margin so the sprite floats inside the box.
    pad_y = max(1, h // 10)
    for y in range(h):
        if y < pad_y or y >= h - pad_y:
            continue
        # Taper toward top and bottom so shapes read as objects rather
        # than filling the frame; x=0 is the mirror line, so keep mass
        # dense there and let it thin out toward the silhouette edge.
        vy = 1.0 - abs((y + 0.5) / h - 0.5) * 2.0
        for x in range(half):
            vx = 1.0 - (x / max(1, half - 1))
            p = density * (0.45 + 0.55 * vy) * (0.40 + 0.60 * vx)
            grid[y][x] = rng.random() < p
    # Guarantee a connected spine down the mirror line so smoothing
    # always has a body to grow from instead of eroding to specks.
    for y in range(pad_y, h - pad_y):
        grid[y][0] = True
        if half > 1:
            grid[y][1] = True

    def _neighbours(g, y, x):
        n = 0
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dx == 0 and dy == 0:
                    continue
                yy, xx = y + dy, x + dx
                if not (0 <= yy < h):
                    continue
                if xx < 0:
                    # Across the mirror line the neighbour is the
                    # reflection of this row's innermost column.
                    if g[yy][0]:
                        n += 1
                elif xx < half and g[yy][xx]:
                    n += 1
        return n

    # Cellular smoothing (survive 4+, born 5+) — the standard cave rule,
    # which consolidates noise into coherent mass without dissolving it.
    for _ in range(max(0, smooth)):
        nxt = [row[:] for row in grid]
        for y in range(h):
            for x in range(half):
                n = _neighbours(grid, y, x)
                nxt[y][x] = n >= 4 if grid[y][x] else n >= 5
        # Keep the margin clear and the spine intact.
        for y in range(h):
            if y < pad_y or y >= h - pad_y:
                nxt[y] = [False] * half
            else:
                nxt[y][0] = True
        grid = nxt
    return grid


def _drop_islands(solid: List[List[bool]], w: int, h: int,
                  min_frac: float = 0.12) -> None:
    """Delete disconnected specks, keeping only masses that are a real
    part of the silhouette. Mutates ``solid`` in place.

    Cellular growth reliably throws off a few orphan pixels beside the
    body; left in, they read as dirt rather than design.
    """
    seen = [[False] * w for _ in range(h)]
    groups: List[List[Tuple[int, int]]] = []
    for sy in range(h):
        for sx in range(w):
            if not solid[sy][sx] or seen[sy][sx]:
                continue
            stack = [(sy, sx)]
            seen[sy][sx] = True
            comp: List[Tuple[int, int]] = []
            while stack:
                y, x = stack.pop()
                comp.append((y, x))
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    yy, xx = y + dy, x + dx
                    if (0 <= yy < h and 0 <= xx < w
                            and solid[yy][xx] and not seen[yy][xx]):
                        seen[yy][xx] = True
                        stack.append((yy, xx))
            groups.append(comp)
    if not groups:
        return
    biggest = max(len(g) for g in groups)
    for g in groups:
        if len(g) < max(3, int(biggest * min_frac)):
            for (y, x) in g:
                solid[y][x] = False


def gen_sprite(
    palette: str = "Ramp: Blue",
    *,
    size: int = 32,
    seed: int = 0,
    density: float = 0.58,
    smooth: int = 2,
    row: Optional[str] = None,
    outline: bool = True,
) -> Any:
    """Generate one mirrored, top-lit sprite silhouette.

    Returns a Pillow RGBA Image of ``size``×``size`` with a transparent
    background. Deterministic for a given ``seed``.
    """
    if not PIL_OK:
        raise RuntimeError("Pillow not installed")
    rng = random.Random(seed)
    w = h = max(8, int(size))
    ramp = ramp_for(palette, row)
    mask_half = _grow_mask(w, h, rng, density, smooth)
    half = (w + 1) // 2

    # Mirror the half-grid outward from the CENTRE. Column 0 of the
    # half-grid is the spine, so it lands either side of the midline and
    # the last column lands at the silhouette's outer edge.
    solid = [[False] * w for _ in range(h)]
    for y in range(h):
        for x in range(half):
            v = mask_half[y][x]
            left = half - 1 - x
            right = w - half + x
            if 0 <= left < w:
                solid[y][left] = v
            if 0 <= right < w:
                solid[y][right] = v

    _drop_islands(solid, w, h)

    img = Image.new("RGBA", (w, h), TRANSPARENT)
    px = img.load()
    for y in range(h):
        for x in range(w):
            if not solid[y][x]:
                continue
            # Top-lit shading: brighter toward the top of the local mass.
            above = 0
            yy = y - 1
            while yy >= 0 and solid[yy][x]:
                above += 1
                yy -= 1
            depth = min(above, 4) / 4.0
            t = 0.78 - depth * 0.5
            # Edge pixels darken so forms separate.
            edge = (
                x == 0 or x == w - 1 or y == 0 or y == h - 1
                or not solid[y][max(0, x - 1)]
                or not solid[y][min(w - 1, x + 1)]
                or not solid[max(0, y - 1)][x]
                or not solid[min(h - 1, y + 1)][x]
            )
            if edge and outline:
                t = 0.12
            px[x, y] = tuple(_shade(ramp, t)) + (255,)
    return img


def gen_sprite_sheet(
    palette: str = "Ramp: Blue",
    *,
    count: int = 12,
    size: int = 32,
    seed: int = 0,
    **kw: Any,
) -> List[Any]:
    """A batch of sprite variations — the 'pick one' contact sheet."""
    return [
        gen_sprite(palette, size=size, seed=seed + i, **kw)
        for i in range(max(1, int(count)))
    ]


# ============================================================
# Tile generation — seamless material patterns
# ============================================================

def _put(px: Any, w: int, h: int, x: int, y: int, colour: RGB) -> None:
    """Write a pixel with wrap-around, keeping tiles seamless."""
    px[x % w, y % h] = tuple(colour) + (255,)


def _tile_brick(px, w, h, rng, kit) -> None:
    body = _kit_row(kit, "Red brick", "Granite")
    alt = _kit_row(kit, "Fired brick", "Sandstone")
    mortar = _kit_row(kit, "Mortar", "Shadow")
    course = max(4, h // 4)          # brick height
    brick_w = max(6, w // 2)
    gap = max(1, course // 5)
    for y in range(h):
        row_i = y // course
        offset = (row_i % 2) * (brick_w // 2)
        for x in range(w):
            in_mortar_y = (y % course) < gap
            in_mortar_x = ((x + offset) % brick_w) < gap
            if in_mortar_y or in_mortar_x:
                _put(px, w, h, x, y, _shade(mortar, 0.55))
                continue
            # Per-brick tonal jitter so courses don't look printed.
            bid = (row_i * 31 + ((x + offset) // brick_w) * 17)
            jitter = random.Random(bid ^ rng.randint(0, 9999)).uniform(-0.14, 0.14)
            ramp = body if (bid % 3) else alt
            # Light the top of each brick, shade its underside.
            local = (y % course) / course
            t = 0.72 - local * 0.30 + jitter
            _put(px, w, h, x, y, _shade(ramp, t))


def _tile_plank(px, w, h, rng, kit) -> None:
    woods = ["Pine", "Oak", "Walnut", "Mahogany", "Weathered"]
    shadow = _kit_row(kit, "Shadow", "Shadow")
    plank_h = max(5, h // 4)
    for y in range(h):
        pid = y // plank_h
        ramp = _kit_row(kit, woods[pid % len(woods)], None)
        local = (y % plank_h) / plank_h
        for x in range(w):
            if local < 0.09:                    # seam between boards
                _put(px, w, h, x, y, _shade(shadow, 0.22))
                continue
            base = 0.70 - local * 0.22
            # Grain: low-frequency streaks along the board.
            g = random.Random(pid * 977 + x * 13).random()
            if g > 0.88:
                base -= 0.16
            elif g > 0.74:
                base -= 0.07
            _put(px, w, h, x, y, _shade(ramp, base))


def _tile_stone(px, w, h, rng, kit) -> None:
    stones = ["Granite", "Slate", "Basalt", "Limestone", "Mossy"]
    mortar = _kit_row(kit, "Shadow", "Shadow")
    cell = max(6, w // 3)
    # Jittered lattice → irregular blocks (a cheap Voronoi stand-in).
    seeds = []
    for gy in range(0, h + cell, cell):
        for gx in range(0, w + cell, cell):
            r = random.Random(gx * 7919 + gy * 104729 + rng.randint(0, 999))
            seeds.append((
                (gx + r.randint(0, cell)) % w,
                (gy + r.randint(0, cell)) % h,
                r.randrange(len(stones)),
                r.uniform(-0.10, 0.10),
            ))
    for y in range(h):
        for x in range(w):
            best = None
            best_d = 1e9
            second = 1e9
            for (sx, sy, si, jit) in seeds:
                dx = min(abs(x - sx), w - abs(x - sx))
                dy = min(abs(y - sy), h - abs(y - sy))
                d = dx * dx + dy * dy
                if d < best_d:
                    second = best_d
                    best_d, best = d, (si, jit)
                elif d < second:
                    second = d
            if best is None:
                continue
            si, jit = best
            # Near-equidistant pixels are block borders → mortar line.
            if second - best_d < max(2.0, cell * 0.9):
                _put(px, w, h, x, y, _shade(mortar, 0.30))
                continue
            ramp = _kit_row(kit, stones[si % len(stones)], None)
            _put(px, w, h, x, y, _shade(ramp, 0.66 + jit))


def _tile_thatch(px, w, h, rng, kit) -> None:
    straws = ["Fresh straw", "Dry straw", "Aged thatch", "Weathered", "Reed"]
    shadow = _kit_row(kit, "Shadow", "Shadow")
    # Lay a deep-shadow bed first so the gaps between bundles read as
    # depth rather than holes punched in the tile.
    for y in range(h):
        for x in range(w):
            _put(px, w, h, x, y, _shade(shadow, 0.12))
    # Several passes of strands so the thatch covers densely without
    # looking combed — each pass lays a new layer over the last.
    for _pass in range(3):
        for x in range(w):
            r = random.Random(x * 6151 + rng.randint(0, 99999))
            ramp = _kit_row(kit, straws[r.randrange(len(straws))], None)
            lean = r.choice((-1, 0, 0, 1))
            length = r.randint(max(4, h // 2), h)
            top = r.randint(-h // 4, max(1, h - length // 2))
            for i in range(length):
                y = top + i
                xx = x + (i // max(2, h // 4)) * lean
                t = 0.82 - (i / max(1, length)) * 0.55
                _put(px, w, h, xx, y, _shade(ramp, t))
            # Deep shadow under each bundle's tip sells the layering.
            _put(px, w, h, x, top + length, _shade(shadow, 0.18))


def _tile_weave(px, w, h, rng, kit) -> None:
    warp = _kit_row(kit, "Dyed blue", "Linen")
    weft = _kit_row(kit, "Linen", "Wool")
    shadow = _kit_row(kit, "Shadow", "Shadow")
    thread = max(2, w // 8)
    for y in range(h):
        for x in range(w):
            over = ((x // thread) + (y // thread)) % 2 == 0
            ramp = warp if over else weft
            # Round each thread with a light-to-dark run across it.
            local = ((y if over else x) % thread) / thread
            t = 0.78 - abs(local - 0.5) * 0.7
            if local < 0.08:
                _put(px, w, h, x, y, _shade(shadow, 0.3))
            else:
                _put(px, w, h, x, y, _shade(ramp, t))


def _tile_pane(px, w, h, rng, kit) -> None:
    body = _kit_row(kit, "Clear", "Granite")
    high = _kit_row(kit, "Highlight", "Clear")
    frame = _kit_row(kit, "Shadow", "Shadow")
    b = max(1, w // 16)
    for y in range(h):
        for x in range(w):
            if x < b or y < b or x >= w - b or y >= h - b:
                _put(px, w, h, x, y, _shade(frame, 0.35))
                continue
            # Diagonal specular streak — the thing that reads as glass.
            d = (x + y) / (w + h)
            t = 0.55 + 0.12 * ((x - y) % 7 == 0)
            ramp = body
            if 0.42 < d < 0.52 or 0.68 < d < 0.72:
                ramp, t = high, 0.85
            _put(px, w, h, x, y, _shade(ramp, t))


_TILE_KINDS: Dict[str, Any] = {
    "Brick":  (_tile_brick,  "Brick & Clay"),
    "Planks": (_tile_plank,  "Wooden Structure"),
    "Stone":  (_tile_stone,  "Stone Structure"),
    "Thatch": (_tile_thatch, "Thatch & Straw"),
    "Weave":  (_tile_weave,  "Fabric & Cloth"),
    "Pane":   (_tile_pane,   "Glass & Crystal"),
}

TILE_KINDS: List[str] = list(_TILE_KINDS.keys())


def default_kit_for(kind: str) -> str:
    """The material kit a tile kind is designed around."""
    entry = _TILE_KINDS.get(kind)
    return entry[1] if entry else "Wooden Structure"


def gen_tile(
    kind: str = "Brick",
    *,
    kit: Optional[str] = None,
    size: int = 32,
    seed: int = 0,
) -> Any:
    """Generate one seamless material tile.

    ``kind`` is one of ``TILE_KINDS``; ``kit`` defaults to the material
    kit that kind was designed around. Deterministic for a given seed.
    """
    if not PIL_OK:
        raise RuntimeError("Pillow not installed")
    entry = _TILE_KINDS.get(kind)
    if entry is None:
        raise ValueError(f"unknown tile kind: {kind!r}")
    fn, default_kit = entry
    kit = kit or default_kit
    w = h = max(8, int(size))
    rng = random.Random(seed)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    fn(img.load(), w, h, rng, kit)
    return img


def gen_tile_sheet(
    kind: str = "Brick",
    *,
    count: int = 12,
    kit: Optional[str] = None,
    size: int = 32,
    seed: int = 0,
) -> List[Any]:
    """A batch of tile variations to choose between."""
    return [
        gen_tile(kind, kit=kit, size=size, seed=seed + i)
        for i in range(max(1, int(count)))
    ]
