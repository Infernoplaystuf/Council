"""
feature_detect.py — deterministic, offline detection + counting of discrete
features/objects in an image, with an annotated output image.

For metal-AM imagery (pores, spatter, particles, holes, bright/dark spots) the
right tool is classical computer vision, not an ML detector: Otsu threshold ->
connected-components -> area filter. It is fully offline, needs no model or
VRAM, is reproducible, and draws boxes + numbers on each counted feature.

You tell it how many features you EXPECT; it reports how many it found, whether
that matches, and writes an annotated PNG (features highlighted) to the vault
output folder. Read-only on the input.

Optional deps (numpy, scipy, Pillow) — like image_index.py, the functions
return ``{"error": ...}`` instead of raising if a dependency is missing.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

_IMAGE_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff",
}


def _imports():
    try:
        from PIL import Image, ImageDraw  # type: ignore
        import numpy as np  # type: ignore
        from scipy import ndimage as ndi  # type: ignore
        return Image, ImageDraw, np, ndi
    except Exception:
        return None, None, None, None


def _otsu(gray, np) -> int:
    """Otsu's threshold on a 0-255 grayscale array (pure numpy)."""
    hist = np.bincount(gray.ravel(), minlength=256).astype("float64")
    total = int(gray.size)
    sum_total = float((np.arange(256) * hist).sum())
    w_b = 0.0
    sum_b = 0.0
    best_between = -1.0
    threshold = 127
    for i in range(256):
        w_b += hist[i]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += i * hist[i]
        m_b = sum_b / w_b
        m_f = (sum_total - sum_b) / w_f
        between = w_b * w_f * (m_b - m_f) ** 2
        # >= picks the LAST threshold among ties. For a degenerate two-value
        # image (e.g. pure black features on white) the between-class variance
        # is flat across the whole gap, and picking the FIRST max lands the
        # threshold at 0 — which makes `g < 0` match nothing. Last-max lands it
        # just below the bright class, so both bright and dark masks are correct.
        if between >= best_between:
            best_between = between
            threshold = i
    return int(threshold)


def _default_out_dir(out_dir: Optional[Any]) -> Optional[Path]:
    if out_dir is not None:
        d = Path(out_dir)
    else:
        try:
            import data_index
            import os
            vault = os.environ.get("COUNCIL_VAULT_ROOT", "").strip()
            root = Path(vault).expanduser() if vault else (Path.home()
                                                           / ".council" / "vault")
            d = data_index.output_dir(root) / "annotated"
        except Exception:
            return None
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:
        return None


def detect_and_count_features(path: Any, *,
                              polarity: str = "auto",
                              min_area: int = 6,
                              max_area_frac: float = 0.25,
                              expected: Optional[int] = None,
                              threshold: Optional[int] = None,
                              annotate: bool = True,
                              out_dir: Optional[Any] = None,
                              max_side: int = 1600,
                              max_features: int = 5000,
                              denoise: bool = False) -> Dict[str, Any]:
    """Detect + count discrete features and (optionally) write an annotated PNG.

    polarity: 'bright' (features lighter than background, e.g. spatter/spots),
    'dark' (features darker, e.g. pores/holes), or 'auto' (whichever gives the
    minority foreground). min_area drops speckle noise; max_area_frac drops the
    whole-frame background. ``expected`` (if given) is compared to the count.
    Returns a dict with count / features / annotated_image path.
    """
    Image, ImageDraw, np, ndi = _imports()
    if Image is None:
        return {"error": "numpy + scipy + Pillow are required for feature "
                         "detection (pip install numpy scipy pillow)."}
    p = Path(path)
    if not p.is_file():
        return {"error": f"not a file: {p}"}
    try:
        im = Image.open(p)
        im.load()
    except Exception as exc:
        return {"error": f"could not open image: {exc!r}"}

    w0, h0 = im.size
    gray_im = im.convert("L")
    scale = 1.0
    if max(w0, h0) > max_side and max(w0, h0) > 0:
        scale = max_side / float(max(w0, h0))
        gray_im = gray_im.resize((max(1, int(w0 * scale)),
                                  max(1, int(h0 * scale))))
    g = np.asarray(gray_im).astype("uint8")

    t = int(threshold) if threshold is not None else _otsu(g, np)
    pol = (polarity or "auto").lower()
    bright_mask = g > t
    dark_mask = g < t
    if pol == "bright":
        mask = bright_mask
    elif pol == "dark":
        mask = dark_mask
    else:  # auto — features are usually the minority of the pixels; never pick
        # an all-empty mask.
        bm, dm = float(bright_mask.mean()), float(dark_mask.mean())
        if bm == 0.0:
            mask, pol = dark_mask, "dark"
        elif dm == 0.0:
            mask, pol = bright_mask, "bright"
        elif bm <= dm:
            mask, pol = bright_mask, "bright"
        else:
            mask, pol = dark_mask, "dark"

    # Morphological opening is OFF by default, and that is a correctness call,
    # not a preference.
    #
    # It was unconditional. An opening is an erosion followed by a dilation, so
    # it deletes anything THINNER than its structuring element outright —
    # regardless of area, and regardless of min_area, which the docstring
    # promises is the size filter. Measured on a synthetic scan of 4 round
    # pores + 4 cracks (1 px x 60 px, area 60 — TEN TIMES min_area=6):
    #
    #     ground truth : 8 features
    #     reported     : 4          <- every crack silently erased
    #
    # In this app's actual domain that is the worst possible bias: cracks and
    # lack-of-fusion ARE thin elongated features, so "count the defects" hid
    # exactly the defects that matter and reported a confident number.
    #
    # min_area already removes speckle (a 1-3 px speck is far below it), which
    # is all the opening was there for — so the default now measures what the
    # user asked for, and denoise=True remains for a genuinely noisy scan.
    if denoise:
        try:
            mask = ndi.binary_opening(mask, structure=np.ones((3, 3)))
        except Exception:
            pass
    lbl, n = ndi.label(mask)
    feats: List[Dict[str, Any]] = []
    if n > 0:
        areas = ndi.sum(np.ones_like(lbl, dtype="float64"), lbl,
                        index=list(range(1, n + 1)))
        slices = ndi.find_objects(lbl)
        max_area = float(max_area_frac) * float(g.size)
        for i in range(1, n + 1):
            a = float(areas[i - 1])
            if a < min_area or a > max_area:
                continue
            sl = slices[i - 1]
            if sl is None:
                continue
            y0, y1 = sl[0].start, sl[0].stop
            x0, x1 = sl[1].start, sl[1].stop
            feats.append({
                "id": len(feats) + 1,
                "area": int(a),
                "bbox": [int(x0), int(y0), int(x1), int(y1)],
                "centroid": [int((x0 + x1) / 2), int((y0 + y1) / 2)],
            })
            if len(feats) >= max_features:
                break
    count = len(feats)

    annotated_path = None
    if annotate and count >= 0:
        try:
            base = im.convert("RGB")
            if scale != 1.0:
                base = base.resize(gray_im.size)
            draw = ImageDraw.Draw(base)
            for f in feats:
                x0, y0, x1, y1 = f["bbox"]
                pad = 1
                draw.rectangle([x0 - pad, y0 - pad, x1 + pad, y1 + pad],
                               outline=(255, 40, 40), width=2)
                draw.text((x0, max(0, y0 - 11)), str(f["id"]),
                          fill=(255, 235, 0))
            d = _default_out_dir(out_dir)
            if d is not None:
                stem = re_safe(p.stem)
                outp = d / f"{stem}_detected_{count}.png"
                base.save(outp)
                annotated_path = str(outp)
        except Exception:
            annotated_path = None

    result: Dict[str, Any] = {
        "file": p.name,
        "count": count,
        "polarity": pol,
        "threshold": int(t),
        "min_area": int(min_area),
        "image_size": [w0, h0],
        "annotated_image": annotated_path,
        "features": feats[:300],
    }
    if expected is not None:
        try:
            exp = int(expected)
            result["expected"] = exp
            result["matches_expected"] = (count == exp)
            result["difference"] = count - exp
        except Exception:
            pass
    return result


def re_safe(name: str) -> str:
    import re
    return re.sub(r"[^\w\-]+", "_", str(name)).strip("_") or "image"
