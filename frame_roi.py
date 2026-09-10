"""
frame_roi.py — crop a folder of frames to a region of interest.

A camera frame is mostly background. Once the interesting part has been boxed
on screen, keeping only that box is the cheapest storage saving available:
a 300x200 region of a 1920x1080 frame is about 3% of the pixels.

WHAT IT WILL NOT DO
-------------------
It never writes into the capture folder and never overwrites anything. Every
save goes into a NEW subfolder of the folder the user picked, named for the box
and the moment it was saved:

    <save folder>/roi_12_40_300x200_20260910_101500/frame_0000.png ...

The source frames are the raw data. A crop that wrote over them, or into the
folder being browsed, would destroy exactly what the ROI was drawn to study —
so a save folder that is the capture folder, or inside it, is refused.

FAILURE IS REPORTED, NOT ROUNDED TO ZERO
----------------------------------------
Every problem — no folder, no box, no Pillow, a box outside every frame —
comes back in ``error`` AND in the one-line ``summary`` a panel displays. The
bad-timing scan in the same GUI once said "0 bad frames" when numpy was
missing, because its error went into a field nothing displayed. A save that
silently wrote nothing would be the same bug with worse consequences: the user
would delete the originals believing the crops existed.

Pillow is imported lazily, so importing this module never fails.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif",
                  ".webp")

Roi = Tuple[int, int, int, int]      # x, y, w, h in full-frame pixels


def parse_roi(value: Any) -> Optional[Roi]:
    """(x, y, w, h) from a tuple/list, or from text like "12, 40, 300, 200".

    None for anything that is not a usable box: the wrong number of values,
    a negative origin, or a width/height under 2 pixels."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        parts: List[Any] = list(value)
    else:
        parts = re.findall(r"-?\d+", str(value))
    if len(parts) != 4:
        return None
    try:
        x, y, w, h = (int(round(float(p))) for p in parts)
    except (TypeError, ValueError):
        return None
    if x < 0 or y < 0 or w < 2 or h < 2:
        return None
    return (x, y, w, h)


def format_roi(roi: Optional[Roi]) -> str:
    return "" if roi is None else ", ".join(str(v) for v in roi)


def crop_box(roi: Roi, size: Tuple[int, int]) -> Optional[Tuple[int, int, int, int]]:
    """The ROI clamped to a frame of ``size``, as a PIL box, or None when the
    ROI lies entirely outside that frame.

    Clamped rather than padded: PIL.crop pads an out-of-bounds box with black,
    which would save pixels the camera never recorded."""
    iw, ih = size
    x, y, w, h = roi
    left, top = max(0, x), max(0, y)
    right, bottom = min(iw, x + w), min(ih, y + h)
    if right - left < 1 or bottom - top < 1:
        return None
    return (left, top, right, bottom)


def _natkey(p: Path) -> List[Any]:
    """frame_9 before frame_10 — the order the frames were captured in."""
    return [(0, int(t), "") if t.isdigit() else (1, 0, t.lower())
            for t in re.split(r"(\d+)", p.name)]


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _fail(message: str) -> Dict[str, Any]:
    return {"count": 0, "skipped": [], "out": "", "error": message,
            "summary": f"Not saved: {message}"}


def export_roi(folder: Any, roi: Any, out_folder: Any, *,
               stamp: Optional[str] = None) -> Dict[str, Any]:
    """Crop every frame in ``folder`` to ``roi`` into a new subfolder of
    ``out_folder``.

    Keys (stable, because a script link binds ports to them by name):
        count    int        frames written
        skipped  list[str]  frames that could not be read or lie outside the box
        out      str        the folder the crops were written to
        error    str        why nothing was saved, or ""
        summary  str        one line for a status readout — always set
    """
    src = Path(str(folder or "").strip().strip('"'))
    box = parse_roi(roi)
    dst_root_text = str(out_folder or "").strip().strip('"')

    if not str(folder or "").strip() or not src.is_dir():
        return _fail(f"no capture folder ({str(folder or '').strip() or 'none chosen'})")
    if box is None:
        return _fail("no ROI — draw a box on the image and click Apply ROI")
    if not dst_root_text:
        return _fail("choose a folder to save the cropped frames into")
    dst_root = Path(dst_root_text)
    src_r, dst_r = src.resolve(), dst_root.resolve()
    if dst_r == src_r or _inside(dst_r, src_r):
        return _fail("the save folder must be outside the capture folder, "
                     "so the original frames are never touched")
    try:
        from PIL import Image
    except ImportError:
        return _fail("Pillow is not installed, so frames cannot be cropped "
                     "(pip install Pillow)")

    frames = sorted((p for p in src.iterdir()
                     if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES),
                    key=_natkey)
    if not frames:
        return _fail(f"no images in {src}")

    stamp = stamp or time.strftime("%Y%m%d_%H%M%S")
    out: Optional[Path] = None
    written, skipped = 0, []
    boxes = set()                 # the crops ACTUALLY written, not the request
    for p in frames:
        try:
            with Image.open(p) as im:
                cb = crop_box(box, im.size)
                if cb is None:
                    skipped.append(p.name)
                    continue
                piece = im.crop(cb)
                if out is None:
                    # Named for the crop really taken from the first frame.
                    # A box typed larger than the frame is clamped, and a
                    # folder called 400x300 holding 380x220 images would be
                    # a label that lies about its contents.
                    l, t, r, b = cb
                    base = f"roi_{l}_{t}_{r - l}x{b - t}_{stamp}"
                    out, n = dst_root / base, 1
                    while out.exists():   # two saves in one second: never merge
                        n += 1
                        out = dst_root / f"{base}_{n}"
                    out.mkdir(parents=True, exist_ok=False)
                kw = {"quality": 95} if p.suffix.lower() in (".jpg", ".jpeg") else {}
                piece.save(out / p.name, **kw)
                boxes.add(cb)
                written += 1
        except Exception:
            skipped.append(p.name)

    if written == 0 or out is None:
        return {"count": 0, "skipped": skipped, "out": "",
                "error": "the ROI lies outside every frame",
                "summary": "Not saved: the ROI lies outside every frame"}

    plural = "s" if written != 1 else ""
    if len(boxes) == 1:
        l, t, r, b = next(iter(boxes))
        what = f"{r - l} x {b - t} at {l}, {t}"
        if (r - l, b - t) != (box[2], box[3]) or (l, t) != (box[0], box[1]):
            what += f", clamped from {box[2]} x {box[3]} to fit the frame"
    else:
        what = f"{len(boxes)} different sizes, because the frames differ in size"
    summary = f"Saved {written} cropped frame{plural} ({what}) to {out}"
    if skipped:
        shown = ", ".join(skipped[:3]) + (" ..." if len(skipped) > 3 else "")
        summary += f" — {len(skipped)} skipped: {shown}"
    return {"count": written, "skipped": skipped, "out": str(out),
            "error": "", "summary": summary}
