"""
Generate the application icon (assets/icon.png + assets/icon.ico).

Draws a static frame of the splash's cog-and-flame design using PIL so the
taskbar icon visually matches what users see at startup. Re-run this any
time the splash design or theme colors change:

  python assets/make_icon.py

Outputs:
  assets/icon.png  256x256 PNG with transparent background
  assets/icon.ico  multi-resolution ICO (16, 32, 48, 64, 128, 256)
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw


# ── Colors mirror branding.THEMES["dark"] + splash.py flame palette ───
BG_TRANSPARENT  = (0, 0, 0, 0)         # icon background — transparent
COG_FILL        = (35, 26, 26, 255)    # panel_bg #231a1a
COG_OUTLINE     = (211, 47, 47, 255)   # accent   #d32f2f
HUB_FILL        = (10, 8, 8, 255)      # very dark #0a0808
FLAME_OUTER     = (122, 24, 24, 255)   # #7a1818
FLAME_MID       = (208, 64, 32, 255)   # #d04020
FLAME_CORE      = (246, 193, 74, 255)  # #f6c14a

# Canvas / geometry — proportional, scaled per output size
ICON_BASE = 1024                    # render at high res for crisp downscale
COG_TEETH = 16
COG_OUTER_FRAC = 0.46               # outer radius as fraction of icon size
COG_INNER_FRAC = 0.40
COG_HUB_FRAC   = 0.22
COG_LINE_FRAC  = 0.012              # outline width
COG_ROTATION_RAD = math.pi / COG_TEETH / 2  # half-tooth offset for symmetry


def _cog_polygon(cx: float, cy: float, outer_r: float, inner_r: float,
                 teeth: int, rotation_rad: float) -> list[tuple[float, float]]:
    """Vertex list for a gear-shaped polygon (matches splash.py geometry)."""
    pts: list[tuple[float, float]] = []
    tooth_step = 2 * math.pi / teeth
    tooth_arc  = tooth_step * 0.45
    gap_arc    = tooth_step - tooth_arc
    for i in range(teeth):
        a0 = rotation_rad + i * tooth_step
        a1 = a0 + gap_arc / 2
        a2 = a1 + tooth_arc
        a3 = a2 + gap_arc / 2
        for a, r in ((a0, inner_r), (a1, outer_r), (a2, outer_r), (a3, inner_r)):
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def _flame_polygon(cx: float, cy_base: float, w: float, h: float) -> list[tuple[float, float]]:
    """Asymmetric teardrop pointing up (no jitter for the static icon)."""
    return [
        (cx,                cy_base),
        (cx - w * 0.45,     cy_base - h * 0.10),
        (cx - w * 0.50,     cy_base - h * 0.35),
        (cx - w * 0.30,     cy_base - h * 0.55),
        (cx - w * 0.40,     cy_base - h * 0.75),
        (cx - w * 0.15,     cy_base - h * 0.90),
        (cx,                cy_base - h),
        (cx + w * 0.18,     cy_base - h * 0.90),
        (cx + w * 0.40,     cy_base - h * 0.75),
        (cx + w * 0.30,     cy_base - h * 0.55),
        (cx + w * 0.50,     cy_base - h * 0.35),
        (cx + w * 0.45,     cy_base - h * 0.10),
    ]


def render_icon(size: int) -> Image.Image:
    """Render the icon at the given pixel size."""
    img = Image.new("RGBA", (size, size), BG_TRANSPARENT)
    d = ImageDraw.Draw(img, "RGBA")
    cx = cy = size / 2.0
    outer_r = size * COG_OUTER_FRAC
    inner_r = size * COG_INNER_FRAC
    hub_r   = size * COG_HUB_FRAC
    line_w  = max(1, int(size * COG_LINE_FRAC))

    # 1. Cog (gear) — filled dark, red outline
    cog_pts = _cog_polygon(cx, cy, outer_r, inner_r, COG_TEETH, COG_ROTATION_RAD)
    d.polygon(cog_pts, fill=COG_FILL, outline=COG_OUTLINE)
    # Re-stroke to get a thicker outline (PIL outlines are 1px)
    cog_pts_closed = cog_pts + [cog_pts[0]]
    d.line(cog_pts_closed, fill=COG_OUTLINE, width=line_w, joint="curve")

    # 2. Hub disc — dark fill inside the cog
    d.ellipse(
        [cx - hub_r, cy - hub_r, cx + hub_r, cy + hub_r],
        fill=HUB_FILL, outline=COG_OUTLINE, width=max(1, line_w // 2),
    )

    # 3. Flame — three layered teardrops on top of the hub
    flame_h     = size * 0.28
    flame_w     = size * 0.18
    flame_base  = cy + flame_h * 0.45         # bottom of flame inside the hub
    d.polygon(_flame_polygon(cx, flame_base, flame_w,        flame_h),        fill=FLAME_OUTER)
    d.polygon(_flame_polygon(cx, flame_base, flame_w * 0.75, flame_h * 0.85), fill=FLAME_MID)
    d.polygon(_flame_polygon(cx, flame_base, flame_w * 0.45, flame_h * 0.55), fill=FLAME_CORE)

    return img


def main() -> None:
    here = Path(__file__).parent.resolve()
    png_path    = here / "icon.png"
    ico_path    = here / "icon.ico"
    splash_path = here / "splash.png"

    # Master render at high resolution for crisp downsampling
    master = render_icon(ICON_BASE)

    # PNG — write a 256x256 (taskbar) version
    icon_png = master.resize((256, 256), Image.LANCZOS)
    icon_png.save(png_path, format="PNG", optimize=True)
    print(f"wrote {png_path}  ({icon_png.size[0]}x{icon_png.size[1]})")

    # ICO — multi-resolution from the same master, downsampled per size
    ico_sizes = [(s, s) for s in (16, 32, 48, 64, 128, 256)]
    master.save(ico_path, format="ICO", sizes=ico_sizes)
    print(f"wrote {ico_path}  ({len(ico_sizes)} sizes)")

    # Static splash asset — composite onto the theme bg so the file looks
    # like what users see when splash.py is replaced by an image fallback.
    splash = Image.new("RGBA", (512, 512), (26, 20, 20, 255))  # bg #1a1414
    icon_for_splash = master.resize((400, 400), Image.LANCZOS)
    splash.paste(icon_for_splash, ((512 - 400) // 2, (512 - 400) // 2), icon_for_splash)
    splash.save(splash_path, format="PNG", optimize=True)
    print(f"wrote {splash_path}  ({splash.size[0]}x{splash.size[1]})")


if __name__ == "__main__":
    main()
