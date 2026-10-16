"""
image_stats.py — deterministic, model-free statistics OVER image pixels.

Fills the one real gap the RAG tool catalog flags: the app indexes images by
filename / EXIF / OCR but never looks at pixel content. These helpers compute
"the statistics of an image" (per-channel mean/std, brightness, contrast,
dominant colours, histogram) and roll them up across a folder — all offline
with Pillow + numpy, no vision model, no per-file model calls.

Optional-dep pattern like image_index.py: if Pillow/numpy aren't importable the
functions return a clear ``{"error": ...}`` instead of raising. Large images are
downscaled before analysis so a 100-megapixel scan stays bounded.
"""
from __future__ import annotations

import io
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

_IMAGE_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
    ".tif", ".tiff", ".ico",
}

# The analyst sandbox hands these functions a path STRING it has already
# confirmed is inside the data folders. They work on that string with os
# functions bound here, at import, before any model code runs — never by
# building a pathlib object from it — so nothing model code does to the
# pathlib classes (or to a module attribute) can change which file is opened
# or which folder is walked (2026-10-16).
_ISFILE = os.path.isfile
_ISDIR = os.path.isdir
_BASENAME = os.path.basename
_SPLITEXT = os.path.splitext
_NORMCASE = os.path.normcase
_REALPATH = os.path.realpath
_COMMONPATH = os.path.commonpath
_STAT = os.stat
_SCANDIR = os.scandir
_FSPATH = os.fspath
_OPEN = io.open


def plain_error(exc: BaseException, path: str, fh: Any = None) -> str:
    """Why an image could not be read, in the library's own words, with the
    open file object's repr (Pillow's "cannot identify image file
    <_io.BufferedReader name=...>") and the full path replaced by the file's
    name, and any other object repr ``<...>`` likewise. The reason
    stays — "image file is truncated (12 bytes not processed)", the
    decompression-bomb size limit — only the internals go (2026-10-16
    review: naming just the exception class dropped the reason)."""
    name = _BASENAME(path)
    msg = str(exc) or type(exc).__name__
    spellings = [path, repr(path)[1:-1]]
    if fh is not None:
        try:
            spellings.insert(0, repr(fh))
        except Exception:                                 # noqa: BLE001
            pass
    for s in spellings:
        if s:
            msg = msg.replace(s, name)
    msg = _OBJECT_REPR.sub(name, msg)
    msg = " ".join(msg.split())
    return msg if len(msg) <= 240 else msg[:237] + "..."


_OBJECT_REPR = re.compile(r"<[^<>]{1,400}>")


def _path_str(path: Any) -> str:
    """``path`` as a plain string, taken once (a str is used as-is)."""
    if isinstance(path, str):
        return path
    s = _FSPATH(path)
    return s.decode("utf-8", "replace") if isinstance(s, bytes) else s


def _walk_files(root: str, recursive: bool) -> List[str]:
    """Every file under ``root`` as a path string — os.scandir only. A
    sub-folder is entered only when it is not a link and its real path stays
    inside ``root`` (a junction or link out of the folder is not followed)."""
    root_real = _NORMCASE(_REALPATH(root))
    out: List[str] = []
    todo = [root]
    while todo:
        d = todo.pop()
        try:
            with _SCANDIR(d) as it:
                entries = list(it)
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_file():
                    out.append(e.path)
                elif (recursive and e.is_dir(follow_symlinks=False)
                        and not e.is_symlink()):
                    real = _NORMCASE(_REALPATH(e.path))
                    try:
                        if _COMMONPATH([real, root_real]) == root_real:
                            todo.append(e.path)
                    except ValueError:
                        continue
            except OSError:
                continue
    return out


def _imports():
    try:
        from PIL import Image  # type: ignore
        import numpy as np  # type: ignore
        return Image, np
    except Exception:
        return None, None


def image_pixel_stats(path: Any, *, max_side: int = 1024,
                      top_colors: int = 5) -> Dict[str, Any]:
    """Per-image pixel statistics. Returns a dict with dimensions, format,
    per-channel R/G/B mean/std/min/max, overall brightness (luminance mean) and
    contrast (luminance std), and the dominant colours. ``{"error": ...}`` if
    Pillow/numpy are missing or the file can't be read."""
    Image, np = _imports()
    if Image is None:
        return {"error": "Pillow + numpy are required for image stats "
                         "(pip install pillow numpy)."}
    p = _path_str(path)
    if not _ISFILE(p):
        return {"error": f"not a file: {p}"}
    fh = None
    try:
        # Pillow is given an open file, not the name: it never resolves or
        # re-opens the path itself.
        with _OPEN(p, "rb") as fh:
            im = Image.open(fh)
            im.load()
    except Exception as exc:
        # The reason in words, with the file's name for Pillow's open-file
        # repr (<_io.BufferedReader name=...>) and the full path.
        return {"error": f"could not open image {_BASENAME(p)}: "
                         f"{plain_error(exc, p, fh)}"}
    fmt, mode = im.format, im.mode
    w, h = im.size
    try:
        work = im.convert("RGB")
        if max(w, h) > max_side and max(w, h) > 0:
            scale = max_side / float(max(w, h))
            work = work.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        arr = np.asarray(work).astype("float64")   # H x W x 3
        chans: Dict[str, Any] = {}
        for i, c in enumerate(("R", "G", "B")):
            ch = arr[:, :, i]
            chans[c] = {"mean": round(float(ch.mean()), 2),
                        "std": round(float(ch.std()), 2),
                        "min": int(ch.min()), "max": int(ch.max())}
        lum = (0.2126 * arr[:, :, 0] + 0.7152 * arr[:, :, 1]
               + 0.0722 * arr[:, :, 2])
        brightness = round(float(lum.mean()), 2)
        contrast = round(float(lum.std()), 2)
        dominant: List[Dict[str, Any]] = []
        try:
            q = work.convert("P", palette=Image.ADAPTIVE,
                             colors=max(1, top_colors))
            pal = q.getpalette() or []
            colors = q.getcolors() or []
            total = float(work.size[0] * work.size[1]) or 1.0
            for cnt, idx in sorted(colors, reverse=True)[:top_colors]:
                rgb = tuple(int(v) for v in pal[idx * 3:idx * 3 + 3])
                dominant.append({"rgb": rgb,
                                 "fraction": round(cnt / total, 3)})
        except Exception:
            pass
    except Exception as exc:
        return {"error": f"pixel analysis of {_BASENAME(p)} failed: "
                         f"{plain_error(exc, p)}"}
    try:
        size_kb = round(_STAT(p).st_size / 1024.0, 1)
    except Exception:
        size_kb = None
    return {
        "file": _BASENAME(p), "format": fmt, "mode": mode,
        "width": w, "height": h,
        "megapixels": round(w * h / 1e6, 3),
        "aspect_ratio": round(w / h, 3) if h else None,
        "size_kb": size_kb,
        "brightness": brightness, "contrast": contrast,
        "channels": chans, "dominant_colors": dominant,
    }


def aggregate_image_folder(folder: Any, *, recursive: bool = True,
                           max_images: int = 500) -> Dict[str, Any]:
    """Roll up pixel stats across every image in a folder: count, format
    distribution, width/height/brightness/contrast summaries, and the brightest
    / darkest files. Bounded to ``max_images``."""
    Image, np = _imports()
    root = _path_str(folder)
    if not _ISDIR(root):
        return {"error": f"not a folder: {root}"}
    imgs = [p for p in _walk_files(root, recursive)
            if _SPLITEXT(p)[1].lower() in _IMAGE_SUFFIXES
            and not _BASENAME(p).startswith(".")]
    truncated = len(imgs) > max_images
    imgs = sorted(imgs, key=_NORMCASE)[:max_images]
    if not imgs:
        return {"count": 0, "note": "no images found"}
    if Image is None:
        return {"count": len(imgs),
                "error": "Pillow + numpy required for pixel stats; found "
                         f"{len(imgs)} image file(s) by name only."}

    per: List[Dict[str, Any]] = []
    for p in imgs:
        s = image_pixel_stats(p)
        if "error" not in s:
            per.append(s)
    if not per:
        return {"count": len(imgs), "error": "no images could be analysed"}

    def _summ(key):
        vals = [x[key] for x in per if isinstance(x.get(key), (int, float))]
        if not vals:
            return None
        return {"min": round(min(vals), 2), "max": round(max(vals), 2),
                "mean": round(sum(vals) / len(vals), 2)}

    by_format: Dict[str, int] = {}
    for x in per:
        by_format[str(x.get("format"))] = by_format.get(
            str(x.get("format")), 0) + 1
    bright_sorted = sorted(per, key=lambda x: x.get("brightness", 0))
    return {
        "count": len(per),
        "analysed_of": len(imgs),
        "truncated": truncated,
        "by_format": by_format,
        "width": _summ("width"),
        "height": _summ("height"),
        "megapixels": _summ("megapixels"),
        "brightness": _summ("brightness"),
        "contrast": _summ("contrast"),
        "darkest": bright_sorted[0]["file"] if bright_sorted else None,
        "brightest": bright_sorted[-1]["file"] if bright_sorted else None,
    }


def ocr_image(path: Any, *, max_chars: int = 8000) -> Dict[str, Any]:
    """Extract text rendered inside an image (charts, scanned tables, labels)
    with Tesseract, so numbers in images can be routed into the CSV pipeline.
    Optional — needs pytesseract + the Tesseract binary; returns a clear note
    otherwise. Honours COUNCIL_TESSERACT_CMD like the vault indexer."""
    Image, _np = _imports()
    if Image is None:
        return {"error": "Pillow is required for OCR (pip install pillow)."}
    p = Path(path)
    if not p.is_file():
        return {"error": f"not a file: {p}"}
    try:
        import os
        import pytesseract as _pt  # type: ignore
    except Exception:
        return {"error": "pytesseract is not installed; OCR unavailable "
                         "(pip install pytesseract, and install the Tesseract "
                         "binary)."}
    try:
        tess = os.environ.get("COUNCIL_TESSERACT_CMD", "").strip()
        if tess:
            _pt.pytesseract.tesseract_cmd = tess
        text = _pt.image_to_string(Image.open(p)) or ""
    except Exception as exc:
        return {"error": f"OCR failed (is the Tesseract binary installed?): "
                         f"{exc!r}"}
    text = text.strip()
    return {"file": p.name, "chars": len(text), "text": text[:max_chars],
            "truncated": len(text) > max_chars}
