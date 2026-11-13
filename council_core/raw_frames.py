"""Raw recording: a run's frames appended to ONE file as the camera gave them,
converted to the usual PNGs afterwards (Andy, 13 November 2026).

WHY. A 65 MP boA9344 frame is 65-131 MB, and saving it as its own PNG means
a new file, a PNG wrapper and its checksums for every frame. Appending the
pixels to one file is the plainest write a disk can do: one long sequential
stream, nothing to encode. The run's frames CSV is written as for a PNG run,
naming the PNG each frame WILL be, so once converted the run is a PNG run
like any other (the slider, the classifier and the Ledger read it as before).
Until it is converted the slider has no pictures to show.

THE FILE, <run>_frames.tyraw: records one after another, each
    b"TYRF"  4 bytes
    n        4 bytes, little-endian: the length of the JSON header
    header   n bytes of JSON: {"name": the PNG's name, "index": frame number,
             "dtype": "uint8" | "uint16", "shape": [h, w] or [h, w, 3]}
    pixels   the image's bytes, C order
A record cut short (the PC lost power mid-write) is reported by `read` and
`convert`, and every whole record before it is still read.

NEVER OVER ANYTHING. The .tyraw file is created exclusively ("xb"); each PNG
too (capture.write_image): a PNG already there is reported and left alone.
`convert` keeps the .tyraw unless asked to remove it AND every frame in it
was written as a new PNG by that call.

TURNED ON with TYPHON_SAVE_FORMAT=raw (before Typhon starts), or
raw_frames.set_format("raw"). Converted afterwards by hand:
    python _council/council_core/raw_frames.py <folder or .tyraw> [--remove-raw]
"""
from __future__ import annotations

import json
import os
import struct
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

MAGIC = b"TYRF"
SUFFIX = ".tyraw"
#: `<run>` + this is the run's raw file.
RAW_NAME = "_frames" + SUFFIX
FORMAT_ENV = "TYPHON_SAVE_FORMAT"
FORMATS = ("png", "raw")
#: Bytes buffered before a write reaches the disk.
BUFFER = 16 << 20

_format: Optional[str] = None


def save_format() -> str:
    """"raw" or "png": set_format's, else TYPHON_SAVE_FORMAT, else png."""
    if _format is not None:
        return _format
    asked = os.environ.get(FORMAT_ENV, "").strip().lower()
    return asked if asked in FORMATS else "png"


def set_format(name: str) -> str:
    """Choose how the next runs save; returns the format now in force."""
    global _format
    name = str(name or "").strip().lower()
    if name not in FORMATS:
        raise ValueError(f"save format {name!r}: expected one of {FORMATS}")
    _format = name
    return _format


class RawStack:
    """A writer for capture.Recorder that appends every frame to one file.

    `write(image, path)` is Recorder's writer: `path` is the PNG the frame
    becomes, and only its name is kept, in the record's header. Safe from
    several threads: appends are taken one at a time."""

    def __init__(self, path: Any):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._handle: Any = None
        self.frames = 0
        self.bytes = 0

    def _open(self) -> Any:
        if self._handle is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = open(self.path, "xb", buffering=BUFFER)
        return self._handle

    def write(self, image: Any, path: Any) -> None:
        import numpy as np

        data = np.ascontiguousarray(image)
        if data.dtype not in (np.uint8, np.uint16):
            raise TypeError(f"raw recording takes 8- and 16-bit pixels, not "
                            f"{data.dtype}")
        name = Path(path).name
        try:
            index = int(name.rsplit("_", 1)[-1].split(".")[0])
        except ValueError:
            index = None
        header = json.dumps({"name": name, "index": index,
                             "dtype": str(data.dtype),
                             "shape": list(data.shape)}).encode("utf-8")
        with self._lock:
            handle = self._open()
            handle.write(MAGIC + struct.pack("<I", len(header)) + header)
            handle.write(memoryview(data).cast("B"))
            self.frames += 1
            self.bytes += data.nbytes

    def close(self) -> None:
        with self._lock:
            handle, self._handle = self._handle, None
        if handle is not None:
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
            handle.close()


def read(path: Any) -> Iterator[Tuple[Dict[str, Any], Any]]:
    """(header, image) for every whole record in a .tyraw file. A record cut
    short ends the reading with ValueError, after every whole one."""
    import numpy as np

    with open(path, "rb") as handle:
        at = 0
        while True:
            head = handle.read(8)
            if not head:
                return
            if len(head) < 8 or head[:4] != MAGIC:
                raise ValueError(f"{Path(path).name}: not a frame record at "
                                 f"byte {at} (cut short or damaged)")
            size = struct.unpack("<I", head[4:])[0]
            raw = handle.read(size)
            if len(raw) < size:
                raise ValueError(f"{Path(path).name}: the record at byte "
                                 f"{at} is cut short")
            header = json.loads(raw.decode("utf-8"))
            dtype = np.dtype(header["dtype"])
            shape = tuple(int(n) for n in header["shape"])
            count = int(np.prod(shape)) * dtype.itemsize
            pixels = handle.read(count)
            if len(pixels) < count:
                raise ValueError(f"{Path(path).name}: frame "
                                 f"{header.get('name')} is cut short (the "
                                 f"recording did not finish)")
            yield header, np.frombuffer(pixels, dtype).reshape(shape)
            at += 8 + size + count


def raw_files(target: Any) -> List[Path]:
    """The .tyraw files at `target`: the file itself, or those in a folder."""
    target = Path(target)
    if target.is_file():
        return [target] if target.suffix == SUFFIX else []
    if target.is_dir():
        return sorted(target.glob(f"*{SUFFIX}"))
    return []


def convert(path: Any, remove_raw: bool = False,
            threads: int = 0) -> Dict[str, Any]:
    """Write each frame of one .tyraw file as its PNG beside it. Returns
    converted, already (a PNG already there, left alone), failed (name ->
    why), damaged ("" or why the reading stopped early) and removed (the
    .tyraw, only when asked and every frame converted)."""
    from .capture import write_image

    path = Path(path)
    out = {"file": str(path), "converted": 0, "already": 0, "failed": {},
           "damaged": "", "removed": False}
    lock = threading.Lock()

    def one(header: Dict[str, Any], image: Any) -> None:
        name = str(header.get("name") or "")
        target = path.parent / Path(name).name
        try:
            write_image(image, target)
            key = "converted"
        except FileExistsError:
            key = "already"
        except Exception as exc:                        # noqa: BLE001
            with lock:
                out["failed"][name] = f"{type(exc).__name__}: {exc}"
            return
        with lock:
            out[key] += 1

    workers = threads or max(1, min(8, (os.cpu_count() or 2) - 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = []
        try:
            for header, image in read(path):
                pending.append(pool.submit(one, header, image))
                if len(pending) >= workers * 2:
                    pending.pop(0).result()
        except ValueError as exc:
            out["damaged"] = str(exc)
        for job in pending:
            job.result()
    # Removed only when EVERY frame in it is now a PNG this call wrote: a
    # PNG that was already there may be someone else's, and then the
    # .tyraw is the only copy of that frame.
    if remove_raw and not out["failed"] and not out["damaged"] \
            and not out["already"]:
        try:
            path.unlink()
            out["removed"] = True
        except OSError:
            pass
    return out


def words(result: Dict[str, Any]) -> List[str]:
    name = Path(result["file"]).name
    lines = [f"{name}: {result['converted']} frame(s) written as PNG"
             + (f", {result['already']} already there (left alone)"
                if result["already"] else "")
             + (", raw file removed" if result["removed"] else "") + "."]
    for frame, why in sorted(result["failed"].items()):
        lines.append(f"  {frame} NOT converted: {why}")
    if result["damaged"]:
        lines.append(f"  {result['damaged']}")
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    remove = "--remove-raw" in args
    args = [a for a in args if a != "--remove-raw"]
    if not args:
        print("usage: python raw_frames.py <folder or .tyraw> [--remove-raw]")
        return 2
    files = raw_files(args[0])
    if not files:
        print(f"No {SUFFIX} file at {args[0]}")
        return 1
    bad = 0
    for each in files:
        result = convert(each, remove_raw=remove)
        bad += bool(result["failed"] or result["damaged"])
        for line in words(result):
            print(line)
    return 1 if bad else 0


if __name__ == "__main__":
    if __package__ in (None, ""):
        # Run as a file (the exported app's _council folder): the package
        # import above needs its parent on the path.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from council_core import raw_frames as _self
        raise SystemExit(_self.main())
    raise SystemExit(main())
