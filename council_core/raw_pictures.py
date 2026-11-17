"""Pictures made from an event camera's .raw after a run (Andy, 17 November
2026: "can we have the EVK still record the video and not convert into
images while recording?").

AN EVK4 RUN RECORDS ITS .raw ALONE by default (cameras.Device.save_pictures,
"Save pictures while recording" in its settings): nothing is drawn or
encoded while it records, and the run's frames CSV still gets one row per
picture window (when it ended on the camera's clock, where in the .raw, its
events) with an empty file column. This makes those pictures afterwards:
each row's window is drawn from the .raw, exactly as the slider's raw view
draws it (event_playback: mid-grey ground, white ON, black OFF, the run's
own area and picture window), saved as the PNG the run would have saved
(<run>_frame_NNNNNN.png, by the row's frame number), and the row is given
its file. The run is then a PNG run like any other: the slider, the
classifier, frame timing and the ROI tools read it as before.

NEVER OVER ANYTHING. A PNG is created exclusively (capture.write_image); one
already there (a conversion stopped half way and run again) is left as it
is and its row named. The .raw is only read. The CSV is rewritten whole,
atomically (a temporary file beside it, then os.replace), with every row
and column as it was but the file names filled in.

From the slider: right-click the picture, "Make pictures from .raw…". By
hand, for a folder or one .raw:
    python _council/council_core/raw_pictures.py <folder or _events.raw>
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

if __package__ in (None, ""):                         # run as a script
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from council_core import camera_record, capture, event_playback  # noqa: E402

RAW_SUFFIX = "_events.raw"


@dataclass
class Made:
    """What one .raw's conversion did."""
    raw: Path
    #: PNGs written by this call.
    written: int = 0
    #: Rows whose PNG was already there (left as it was, row named).
    existed: int = 0
    #: Rows past the end of what the .raw holds (it was cut short).
    missing: int = 0
    #: Rows that had no picture to make (none, or all named already: 0).
    wanted: int = 0
    error: str = ""
    notes: List[str] = field(default_factory=list)

    def line(self) -> str:
        if self.error:
            return f"{self.raw.name}: {self.error}"
        if not self.wanted:
            return f"{self.raw.name}: every picture is made already"
        bits = [f"{self.written} pictures made"]
        if self.existed:
            bits.append(f"{self.existed} already there")
        if self.missing:
            bits.append(f"{self.missing} past the end of the .raw")
        return f"{self.raw.name}: " + ", ".join(bits)


def run_of(raw: Any) -> str:
    name = os.path.basename(str(raw))
    return name[:-len(RAW_SUFFIX)] if name.endswith(RAW_SUFFIX) else ""


def index_for(raw: Any) -> Path:
    """The run's frames CSV beside its .raw."""
    path = Path(str(raw))
    return path.parent / f"{run_of(path)}{capture.INDEX_SUFFIX}"


def _rows(index: Path):
    with open(index, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def _wanted(row: Dict[str, str]) -> bool:
    raw_t = str(row.get("raw_t_us") or "").strip()
    return (not str(row.get("file") or "").strip()
            and raw_t.lstrip("-").isdigit())


def wants_pictures(raw: Any) -> int:
    """How many of the run's picture windows have no PNG yet: rows of its
    frames CSV with a .raw time and no file. 0 when there is no CSV."""
    try:
        _, rows = _rows(index_for(raw))
    except (OSError, csv.Error, UnicodeDecodeError):
        return 0
    return sum(1 for row in rows if _wanted(row))


def pending(folder: Any) -> List[Path]:
    """Every .raw in `folder` whose run has pictures still to make."""
    try:
        names = sorted(e.name for e in os.scandir(str(folder))
                       if e.is_file() and e.name.endswith(RAW_SUFFIX))
    except OSError:
        return []
    return [Path(str(folder)) / n for n in names
            if wants_pictures(Path(str(folder)) / n)]


def _write_csv(index: Path, columns: List[str],
               rows: List[Dict[str, str]]) -> None:
    fd, tmp = tempfile.mkstemp(prefix=index.name + ".", suffix=".tmp",
                               dir=str(index.parent))
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns,
                                    extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, index)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def open_for(raw: Any, open_raw: Optional[Callable[..., Any]] = None) -> Any:
    """The .raw as the slider's raw view opens it: the run's own window,
    origin and area."""
    raw = Path(str(raw))
    index = index_for(raw)
    extra: Dict[str, Any] = {}
    origin = event_playback.raw_origin(index)
    if origin is not None:
        extra["origin_us"] = origin
    area = camera_record.area_of(camera_record.read(raw.parent, run_of(raw)))
    if area is not None:
        extra["area"] = area
    window = (event_playback.run_window_us(index)
              or event_playback.DEFAULT_WINDOW_US)
    return (open_raw or event_playback.open_raw)(raw, window, **extra)


def make_pictures(raw: Any, *, playback: Any = None,
                  open_raw: Optional[Callable[..., Any]] = None,
                  stop: Optional[threading.Event] = None,
                  progress: Optional[Callable[[int, int], Any]] = None
                  ) -> Made:
    """Draw and save the pictures of the run recorded in `raw`.

    `playback`: the .raw already open and read (the slider's raw view of
    this run); otherwise it is opened here, read to the end and closed.
    `stop` set: stop between pictures, and still name the rows done.
    `progress(done, wanted)` after each picture. Never raises."""
    raw = Path(str(raw))
    made = Made(raw=raw)
    index = index_for(raw)
    try:
        columns, rows = _rows(index)
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        made.error = f"cannot read {index.name}: {exc}"
        return made
    todo = [row for row in rows if _wanted(row)]
    made.wanted = len(todo)
    if not todo:
        return made
    own = playback is None
    try:
        if own:
            playback = open_for(raw, open_raw)
        wait = getattr(playback, "wait_done", None)
        if callable(wait):
            wait()
        if not playback.count:
            made.error = playback.error or "the .raw holds no events"
            return made
        if playback.error:
            made.notes.append(f"the .raw stopped early: {playback.error}")
        run = run_of(raw)
        window = int(playback.window_us)
        for done, row in enumerate(todo, 1):
            if stop is not None and stop.is_set():
                made.notes.append("stopped before the end")
                break
            k = event_playback.window_for(int(row["raw_t_us"]), window)
            if k >= playback.count:
                made.missing += 1
                continue
            try:
                number = int(str(row.get("index") or "").strip())
            except ValueError:
                number = done
            name = f"{run}_frame_{number:06d}.png"
            try:
                capture.write_image(playback.frame(k), raw.parent / name)
                made.written += 1
            except FileExistsError:
                made.existed += 1
            row["file"] = name
            if progress is not None:
                progress(done, made.wanted)
        _write_csv(index, columns, rows)
    except Exception as exc:                              # noqa: BLE001
        made.error = f"{type(exc).__name__}: {exc}"
        if made.written or made.existed:
            try:
                _write_csv(index, columns, rows)
            except Exception:                             # noqa: BLE001
                pass
    finally:
        if own and playback is not None:
            try:
                playback.close()
            except Exception:                             # noqa: BLE001
                pass
    return made


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print(__doc__.strip().splitlines()[-1].strip())
        return 2
    target = Path(args[0].strip().strip('"'))
    raws = [target] if target.is_file() else pending(target)
    if not raws:
        print(f"No .raw with pictures to make in {target}")
        return 0
    failed = 0
    for raw in raws:
        print(f"Reading {raw.name}…", flush=True)
        made = make_pictures(raw)
        print(made.line())
        for note in made.notes:
            print(f"  {note}")
        failed += bool(made.error)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
