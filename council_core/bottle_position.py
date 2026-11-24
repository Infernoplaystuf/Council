"""
council_core.bottle_position — the PLC trigger plugin's auto-calibration
(typhon_plugins/plc; docs/typhon_plc_trigger.md 5): where the bottle sits
in each picture, along the belt. Here, not in the plugin's folder, because
it needs numpy and Pillow and a built-in plugin imports only the plugin
API: the plugin reaches it through typhon_plugin_api.builtin.

The bottle is told from the belt by its edges: each picture's gradient
energy is summed across the belt, giving a profile along it; the busiest
stretch of that profile (above its median by a few spreads) is the
bottle, and the middle of that stretch is the bottle's place. The offset
from the picture's middle, in pixels along the belt's direction, says how
early (negative) or late (positive) the picture was taken; the plugin's
timing.correction_ms turns the median offset into milliseconds.

Works best on a plain belt; a busy belt or a bottle filling the view gives
no clear stretch, and that picture is skipped (said in the result).
Pictures are read with Pillow (Typhon's PNGs, 8 or 16 bit).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

import numpy as np

#: The belt's direction in the picture (typhon_plugins/plc/timing.py has
#: the same words).
DIR_RIGHT = "Left to right"
DIR_LEFT = "Right to left"
DIR_DOWN = "Top to bottom"
DIR_UP = "Bottom to top"

PICTURE_ENDINGS = (".png", ".tif", ".tiff", ".bmp", ".jpg", ".jpeg")


@dataclass
class Calibration:
    offsets_px: List[float] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    width_px: int = 0          # the picture's length along the belt

    @property
    def median_px(self) -> Optional[float]:
        return float(np.median(self.offsets_px)) if self.offsets_px else None

    @property
    def spread_px(self) -> Optional[float]:
        if len(self.offsets_px) < 2:
            return None
        return float(np.std(self.offsets_px))


def load_gray(path: Path) -> np.ndarray:
    from PIL import Image                     # Pillow: Council's own needs

    with Image.open(path) as img:
        arr = np.asarray(img)
    if arr.ndim == 3:
        arr = arr[..., :3].mean(axis=2)
    return arr.astype(np.float32)


def bottle_offset(image: np.ndarray, direction: str = DIR_RIGHT
                  ) -> Optional[float]:
    """Pixels from the middle of the view to the bottle's middle, along
    the belt's direction (positive: further on). None when no bottle
    stands out."""
    img = np.asarray(image, dtype=np.float32)
    if img.ndim == 3:
        img = img.mean(axis=2)
    along_x = direction in (DIR_RIGHT, DIR_LEFT)
    if not along_x:
        img = img.T
    if img.shape[1] < 8 or img.shape[0] < 2:
        return None
    gx = np.abs(np.diff(img, axis=1))[:-1, :]
    gy = np.abs(np.diff(img, axis=0))[:, :-1]
    profile = (gx + gy).sum(axis=0)
    width = max(3, profile.size // 100)
    profile = np.convolve(profile, np.ones(width) / width, mode="same")
    base = float(np.median(profile))
    spread = float(np.median(np.abs(profile - base))) or 1e-6
    strong = profile > base + 6.0 * spread
    if not strong.any() or strong.mean() > 0.8:
        return None
    # the strongest cluster of strong columns (a bottle's two sides and
    # its label are one cluster); its middle is halfway between its ends
    idx = np.flatnonzero(strong)
    gap = max(2, profile.size // 8)
    runs = np.split(idx, np.flatnonzero(np.diff(idx) > gap) + 1)
    best = max(runs, key=lambda r: profile[r].sum())
    middle = (float(best[0]) + float(best[-1])) / 2.0
    centre = (profile.size - 1) / 2.0
    offset = middle - centre
    if direction in (DIR_LEFT, DIR_UP):
        offset = -offset
    return offset


def latest_pictures(folder: Optional[Path], n: int) -> List[Path]:
    """The `n` newest pictures under `folder` (its run folders included)."""
    if folder is None or not Path(folder).is_dir():
        return []
    found = [p for p in Path(folder).rglob("*")
             if p.suffix.lower() in PICTURE_ENDINGS and p.is_file()]
    found.sort(key=lambda p: p.stat().st_mtime)
    return found[-int(n):]


def calibrate(paths: Sequence[Path], direction: str = DIR_RIGHT
              ) -> Calibration:
    out = Calibration()
    for path in paths:
        try:
            img = load_gray(Path(path))
        except Exception as exc:                          # noqa: BLE001
            out.skipped.append(f"{Path(path).name}: {exc}")
            continue
        offset = bottle_offset(img, direction)
        if offset is None:
            out.skipped.append(f"{Path(path).name}: no bottle stands out")
            continue
        out.offsets_px.append(offset)
        out.width_px = img.shape[1] if direction in (DIR_RIGHT, DIR_LEFT) \
            else img.shape[0]
    return out
