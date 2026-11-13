"""How fast a save folder takes frames, and what frame rate that sustains.

A 65 MP boA9344 frame is about 65 MB in Mono8 and 131 MB in Mono12 (saved
as 16-bit): 30 fps of it is 2-4 GB/s, more than most disks keep up for long.
When the disk is slower than the camera, the save queue (capture.FrameWriter)
fills and frames are counted NOT saved. This says so BEFORE a run, from a
short timed write into the folder itself (Andy, 13 November 2026).

The test writes ONE file of its own, named typhon_speed_test_<pid>_<n>.tmp,
created exclusively (never over anything), flushed to the disk (fsync, so it
is the disk's speed and not Windows' file cache), and removes that file, and
only that file, afterwards.

A short test can read high: many SSDs write fast into a cache for the first
few GB and slower after. The words say so.

`python -m council_core.save_speed <folder> [MB]` prints the result.
"""
from __future__ import annotations

import itertools
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

#: The default test size. Big enough to get past small caches, small enough
#: to take about a second on a fast NVMe drive.
TEST_MB = 512
CHUNK_MB = 32
#: Set to 1 to skip the check when a run starts.
OFF_ENV = "TYPHON_NO_SPEED_CHECK"
PREFIX = "typhon_speed_test_"

_counter = itertools.count(1)
#: folder -> the last result this process measured there.
_measured: Dict[str, Dict[str, Any]] = {}


def measure(folder: Any, size_mb: int = TEST_MB) -> Dict[str, Any]:
    """Write `size_mb` MB into `folder` and time it. Returns mb_s (0.0 when
    it could not), seconds, size_mb, folder and error ("" when it worked).
    Never raises; leaves nothing behind."""
    folder = Path(folder)
    out: Dict[str, Any] = {"folder": str(folder), "size_mb": int(size_mb),
                           "mb_s": 0.0, "seconds": 0.0, "error": ""}
    path = folder / f"{PREFIX}{os.getpid()}_{next(_counter)}.tmp"
    chunk = os.urandom(CHUNK_MB << 20)      # incompressible, like a frame
    left = int(size_mb) << 20
    made = False
    try:
        folder.mkdir(parents=True, exist_ok=True)
        began = time.perf_counter()
        with open(path, "xb", buffering=0) as handle:
            made = True
            while left > 0:
                piece = chunk if left >= len(chunk) else chunk[:left]
                handle.write(piece)
                left -= len(piece)
            os.fsync(handle.fileno())
        seconds = max(time.perf_counter() - began, 1e-6)
        out["seconds"] = round(seconds, 3)
        out["mb_s"] = round(size_mb * (1 << 20) / 1e6 / seconds, 1)
    except Exception as exc:                            # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if made:
            try:
                path.unlink()
            except OSError:
                pass
    if not out["error"]:
        _measured[str(folder)] = out
    return out


def measured(folder: Any) -> Optional[Dict[str, Any]]:
    """This process's last result for `folder`, or None."""
    return _measured.get(str(Path(folder)))


def sustained_fps(mb_s: float, frame_bytes: int) -> float:
    """Frames a second the folder keeps up with, at `frame_bytes` each."""
    if mb_s <= 0 or frame_bytes <= 0:
        return 0.0
    return mb_s * 1e6 / frame_bytes


def words(result: Dict[str, Any], frame_bytes: int = 0,
          fps: float = 0.0, queue_bytes: int = 0) -> List[str]:
    """What the result means, in a sentence or three."""
    if result.get("error"):
        return [f"Could not time the save folder: {result['error']}"]
    mb_s = float(result.get("mb_s") or 0.0)
    lines = [f"The save folder takes about {mb_s:,.0f} MB/s "
             f"({result.get('size_mb')} MB written in "
             f"{result.get('seconds')} s; a longer run can be slower once "
             f"the drive's cache fills)."]
    if frame_bytes > 0:
        keeps = sustained_fps(mb_s, frame_bytes)
        lines.append(f"At {frame_bytes / 1e6:,.1f} MB a frame that keeps up "
                     f"with about {keeps:,.1f} fps.")
        if fps > keeps > 0 and queue_bytes > 0:
            fill = fps * frame_bytes - mb_s * 1e6
            seconds = queue_bytes / fill
            lines.append(
                f"At {fps:,.1f} fps the save queue "
                f"({queue_bytes / (1 << 30):,.1f} GB) fills in about "
                f"{seconds:,.1f} s; after that frames are NOT saved. A "
                f"smaller ROI, Mono8, a lower frame rate or a faster drive "
                f"helps.")
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("usage: python -m council_core.save_speed <folder> [MB]")
        return 2
    size = int(args[1]) if len(args) > 1 else TEST_MB
    result = measure(args[0], size)
    said = words(result)
    if not result["error"]:
        for name, size in (("Mono8", 65_408_000), ("Mono12", 130_816_000)):
            keeps = sustained_fps(result["mb_s"], size)
            said.append(f"A full boA9344 frame in {name} "
                        f"({size / 1e6:.0f} MB): about {keeps:.1f} fps.")
    for line in said:
        print(line)
    return 1 if result["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
