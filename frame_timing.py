"""
frame_timing.py — find frames captured on a bad timing.

A correctly-timed capture is LETTERBOXED: a bright picture band across the
middle with black bars above and below, like a widescreen frame.

A badly-timed capture caught the sensor mid-readout: the frame is almost
entirely black, with one or two narrow BRIGHT BARS low in the image — the only
rows that got exposed before the frame ended.

    normal                              bad timing
    +------------------------+          +------------------------+
    |########################|  black   |                        |
    |                        |          |                        |
    |     picture content    |  bright  |                        |  all black
    |                        |          |                        |
    |########################|  black   |####################    |  <- white bars
    +------------------------+          +------------------------+     near the bottom

WHY A ROW PROFILE AND NOT A MODEL
---------------------------------
The two cases differ in exactly one cheap statistic: WHERE the bright rows
are. Normal frames put them in the middle; bad ones put a thin cluster near
the bottom and nowhere else. A per-row mean over a downscaled greyscale image
separates them in a few milliseconds per frame with no training data, no GPU,
and no per-file model call — which matters because this runs over a whole
capture folder, and the standing rule in this project is that hot loops do
not call a model.

numpy does the arithmetic; Pillow only decodes. Both are optional: without
them, classify_folder reports why rather than pretending it scanned.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:                                   # pragma: no cover - env dependent
    import numpy as _np
    _NUMPY = True
except Exception:                      # pragma: no cover
    _np = None
    _NUMPY = False

try:                                   # pragma: no cover - env dependent
    from PIL import Image as _Image
    _PIL = True
except Exception:                      # pragma: no cover
    _Image = None
    _PIL = False

IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp")

# Tuned against synthesised frames matching the description above. Every one
# is a named constant rather than a literal in the middle of the maths, so a
# user whose camera differs can move them without reading the algorithm.
DARK_LEVEL = 0.22        # a row dimmer than this is "black"
BRIGHT_LEVEL = 0.55      # a row brighter than this is "lit"
MOSTLY_DARK = 0.80       # a bad frame is at least this fraction dark rows
BOTTOM_BAND = 0.30       # "near the bottom" = the last 30% of rows
MAX_BARS = 4             # more lit bands than this is a picture, not bars
ANALYSIS_WIDTH = 160     # downscale before profiling; the signal is vertical


@dataclass
class FrameVerdict:
    path: str
    bad_timing: bool
    reason: str
    dark_fraction: float = 0.0
    lit_bands: int = 0
    lit_centre: float = 0.0          # 0.0 = top of frame, 1.0 = bottom

    def describe(self) -> str:
        tag = "BAD TIMING" if self.bad_timing else "ok"
        return (f"{tag:<11} {Path(self.path).name}  "
                f"dark={self.dark_fraction:.2f} bands={self.lit_bands} "
                f"centre={self.lit_centre:.2f}  ({self.reason})")


@dataclass
class FolderReport:
    total: int = 0
    bad: int = 0
    unreadable: int = 0
    verdicts: List[FrameVerdict] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    @property
    def bad_fraction(self) -> float:
        return (self.bad / self.total) if self.total else 0.0

    def summary(self) -> str:
        if self.errors and not self.total:
            return "; ".join(self.errors)
        return (f"{self.bad} of {self.total} frames captured on a bad timing "
                f"({self.bad_fraction:.1%})"
                + (f"; {self.unreadable} unreadable" if self.unreadable else ""))


def _row_profile(path: Any) -> Optional[Any]:
    """Mean brightness per row, 0.0-1.0, from a downscaled greyscale copy.

    Downscaled on the WIDTH only in spirit — the signal we want is how
    brightness varies down the frame, so horizontal detail is noise and
    throwing it away makes the scan fast enough for a whole capture folder."""
    if not (_PIL and _NUMPY):
        return None
    with _Image.open(path) as im:
        im = im.convert("L")
        w, h = im.size
        if w > ANALYSIS_WIDTH:
            im = im.resize((ANALYSIS_WIDTH,
                            max(1, int(h * ANALYSIS_WIDTH / w))))
        arr = _np.asarray(im, dtype=_np.float32) / 255.0
    return arr.mean(axis=1)


def _bands(mask: Any) -> List[Tuple[int, int]]:
    """Contiguous True runs in a boolean row-mask, as (start, end) pairs."""
    out: List[Tuple[int, int]] = []
    start = None
    for i, on in enumerate(list(mask)):
        if on and start is None:
            start = i
        elif not on and start is not None:
            out.append((start, i))
            start = None
    if start is not None:
        out.append((start, len(mask)))
    return out


def classify_profile(profile: Any) -> Tuple[bool, str, Dict[str, float]]:
    """The decision, separated from any file I/O so it is directly testable.

    Returns (bad_timing, reason, metrics)."""
    rows = len(profile)
    if rows == 0:
        return False, "empty image", {}
    dark = profile < DARK_LEVEL
    lit = profile > BRIGHT_LEVEL
    dark_fraction = float(dark.mean())
    lit_bands = _bands(lit)
    metrics = {"dark_fraction": dark_fraction, "lit_bands": len(lit_bands)}

    if not lit_bands:
        # No lit rows at all: a wholly black frame. Real, but it is a dropped
        # frame rather than the mid-readout signature asked about, and calling
        # it "bad timing" would blur two different faults together.
        metrics["lit_centre"] = 0.0
        return False, "no lit rows at all (a dropped frame, not a bad timing)", metrics

    centres = [((a + b) / 2.0) / rows for a, b in lit_bands]
    lit_centre = sum(centres) / len(centres)
    metrics["lit_centre"] = lit_centre

    if dark_fraction < MOSTLY_DARK:
        return False, "a normal exposure — most of the frame is lit", metrics
    if len(lit_bands) > MAX_BARS:
        return False, f"{len(lit_bands)} lit bands — picture detail, not bars", metrics
    if lit_centre < 1.0 - BOTTOM_BAND:
        return False, "the lit rows sit mid-frame, which is normal letterboxing", metrics
    return True, (f"mostly black ({dark_fraction:.0%}) with {len(lit_bands)} "
                  f"bar(s) low in the frame"), metrics


def classify_frame(path: Any) -> FrameVerdict:
    p = Path(path)
    profile = _row_profile(p)
    if profile is None:
        return FrameVerdict(str(p), False, "numpy and Pillow are required")
    bad, reason, m = classify_profile(profile)
    return FrameVerdict(str(p), bad, reason,
                        dark_fraction=m.get("dark_fraction", 0.0),
                        lit_bands=int(m.get("lit_bands", 0)),
                        lit_centre=m.get("lit_centre", 0.0))


def classify_folder(folder: Any, *, recursive: bool = False) -> FolderReport:
    """Scan a folder of frames. THE ENTRY POINT a generated GUI calls.

    Never raises on a bad file — one unreadable frame in a thousand must not
    abort a scan, so it is counted and named instead."""
    rep = FolderReport()
    text = str(folder or "").strip().strip('"')
    if not text:
        # Path("") IS Path("."): an unset folder picker used to scan the
        # process's working directory — the generated project, which holds
        # no frames — and report "0 bad frames" with no error anywhere.
        rep.errors.append("no folder chosen — pick the folder of frames first")
        return rep
    d = Path(text)
    if not d.is_dir():
        rep.errors.append(f"{d} is not a folder")
        return rep
    if not (_PIL and _NUMPY):
        missing = [n for n, ok in (("Pillow", _PIL), ("numpy", _NUMPY)) if not ok]
        rep.errors.append(" and ".join(missing)
                          + (" is" if len(missing) == 1 else " are")
                          + " not installed, so frames cannot be scanned")
        return rep

    it = d.rglob("*") if recursive else d.iterdir()
    for f in sorted(it):
        if not f.is_file() or f.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        rep.total += 1
        try:
            v = classify_frame(f)
        except Exception as exc:
            rep.unreadable += 1
            rep.errors.append(f"{f.name}: {exc!r}")
            continue
        rep.verdicts.append(v)
        if v.bad_timing:
            rep.bad += 1
    return rep


def _scanned(folder: Any) -> FolderReport:
    """classify_folder, but RAISE when not one frame could be looked at.

    classify_folder never raises, by design — it is a report. The functions
    a GUI binds to are answers, and "0 bad frames" is only an answer if some
    frames were scanned. Returning 0 for "no folder", "numpy missing", or
    "nothing here is an image" put a confident, false zero on screen; the
    reason sat in rep.errors, which nothing displayed. Now those raise, and
    a generated handler shows the message instead of the number.

    Per-FILE failures are still counted, not fatal: one unreadable frame in a
    thousand must not abort the scan."""
    rep = classify_folder(folder)
    if rep.total == 0:
        raise RuntimeError(rep.errors[0] if rep.errors else
                           f"no images in {str(folder).strip()} (looked for "
                           f"{', '.join(IMAGE_SUFFIXES)})")
    if rep.unreadable == rep.total:
        raise RuntimeError(f"none of the {rep.total} images could be read — "
                           f"first error: {rep.errors[0]}")
    return rep


def count_bad_frames(folder: Any) -> int:
    """How many frames in ``folder`` were captured on a bad timing.

    The simplest possible signature — one path in, one number out — because
    this is what a GUI binds a button to. Use classify_folder when you want
    the per-frame detail. Raises RuntimeError when nothing could be scanned
    (see _scanned) rather than returning a 0 that means nothing."""
    return _scanned(folder).bad


def list_bad_frames(folder: Any) -> List[str]:
    """Just the FILENAMES of the bad frames, in name order. Raises like
    count_bad_frames."""
    return [Path(v.path).name
            for v in _scanned(folder).verdicts if v.bad_timing]


def scan_report(folder: Any) -> Dict[str, Any]:
    """Everything a GUI wants from one scan, keyed for a multi-output link.

    A count on its own says a folder has ten bad frames and leaves the user
    to find them. Returning the names alongside means one button press
    answers "how many" and "which" together, from a single pass over the
    folder — scanning twice to fill two widgets would double the work for
    no gain.

    Keys are stable because a script link binds ports to them by name:
        count   int        how many frames were bad
        names   list[str]  their filenames, in name order
        indices list[int]  their positions in the folder listing, so a
                           caller can jump a scrubber straight to one
        summary str        a one-line human description

    Raises RuntimeError when nothing could be scanned — no folder, not a
    folder, numpy/Pillow missing, no images — instead of returning count 0.
    """
    rep = _scanned(folder)
    names, indices = [], []
    for i, v in enumerate(rep.verdicts):
        if v.bad_timing:
            names.append(Path(v.path).name)
            indices.append(i)
    return {"count": rep.bad, "names": names, "indices": indices,
            "summary": rep.summary()}
