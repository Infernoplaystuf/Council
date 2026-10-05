"""
council_core.camera_record — <run>_camera.json: which camera a capture run
came from, where on its sensor its pictures are, and how it was set at Start.

WHY A RUN NEEDS ONE
A run's PNGs say what the camera saw and its frames CSV says when; neither
says which camera, where on the sensor, or with what settings. A bird-bath run
(an EVK4 watching a static back yard, its own area kept to the bath) saves
pictures 160 x 120 that are NOT the top-left 160 x 120 of the sensor — and the
.raw beside them holds events in whole-sensor coordinates, so the raw view
showed a 1280 x 720 picture with only the bath active while the PNGs were the
bath alone. The record is what lets a viewer (capture_review's raw view), a
script months later, or a person show and read the run as it was taken.

WHAT IT SAYS (format "typhon-camera-record", version 1)
    camera     backend, model, serial, vendor, kind, label — the camera as it
               identified itself when it was opened
    sensor     its full size, in sensor pixels
    area       the camera's own area (AOI / ROI) at Start, x, y, w, h in
               SENSOR pixels (binned pixels on a binned Basler, as the camera
               counts them) — where on the sensor every picture of the run is
    settings   every setting the camera described at Start, key: value (as
               frame_camera.settings_list keys them), with `units` and the
               keys that are read-only readings (a temperature)
    preset     the preset applied (or saved) last, when nothing has changed
               the camera since — "" otherwise, and then `preset_changed`
               says which preset it was and what no longer matches
    app        the app that ran the capture: its name (window title), the
               Designer project, the example it was built from, when it
               was last generated
    software   the Council version and the source commit (when the Council
               runs from a git checkout), and this record's format
    host, written, at   which PC, and when (local time, and seconds since 1970)

The run's area never changes mid-run: frame_camera refuses an area, a preset
or a setting the stream is in the way of while capturing ("one set-up per
run"), so the area at Start is the area of every picture in it.

WRITTEN ONCE, NEVER OVER ANYTHING
The record is written beside the run's other files — the capture folder is
where the run writes; frame_roi and frame_classes still never write there —
whole, to a temporary name first and then renamed into place, so a crash
mid-write leaves no half record. The rename REFUSES an existing name (on
Windows os.rename does; elsewhere a hard link does), so a record is never
replaced: the run stem is unique (frame_camera._unique_run) and a second
writer for the same run gets RecordExists. JSON only, never pickle.

READ TOLERANTLY
`read` returns None for a run without a record (every run before this
existed), and for a file that is not one of these records or is damaged: a
viewer falls back to what it did before (the full sensor), rather than
failing a review over metadata. `area_of` hands back an area only when it is
four whole numbers inside the sensor the record names.

NO TOOLKIT, NO CAMERA SDK
Plain data in, plain data out; frame_camera gathers it from the open camera.
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

#: "<run>_camera.json" beside "<run>_frame_000001.png" and "<run>_frames.csv".
RECORD_SUFFIX = "_camera.json"

FORMAT = "typhon-camera-record"
FORMAT_VERSION = 1

#: Far bigger than any record (a few KB); something else has this name.
MAX_RECORD_BYTES = 4 * 1024 * 1024


class RecordExists(FileExistsError):
    """A record for this run is already there; it is never replaced."""


def record_path(folder: Any, run: str) -> Path:
    """Where `run`'s camera record is (or would be)."""
    return Path(str(folder)) / f"{run}{RECORD_SUFFIX}"


# ======================================================================
# Building one
# ======================================================================
def _plain(value: Any) -> Any:
    """A value JSON can carry: plain scalars as they are, a non-finite
    float as None (JSON has no NaN), anything else as its text."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return number if math.isfinite(number) else None


def build(*, run: str, camera: Mapping[str, Any], sensor: Tuple[int, int],
          area: Tuple[int, int, int, int],
          settings: Optional[List[Mapping[str, Any]]] = None,
          settings_error: str = "", preset: str = "",
          preset_changed: Optional[Mapping[str, Any]] = None,
          app: Optional[Mapping[str, Any]] = None,
          software: Optional[Mapping[str, Any]] = None,
          host: str = "") -> Dict[str, Any]:
    """The record, as a dict ready for `write`.

    `settings` is what frame_camera.settings_list returns under "settings"
    (one dict per setting: key, value, unit, read_only, ...); it is kept as
    key: value with the units and read-only keys beside it — readable by a
    person, and by code that wants to put a camera back as it was for this
    run."""
    values: Dict[str, Any] = {}
    units: Dict[str, str] = {}
    readings: List[str] = []
    for row in settings or []:
        key = str(row.get("key") or "")
        if not key:
            continue
        values[key] = _plain(row.get("value"))
        if row.get("unit"):
            units[key] = str(row["unit"])
        if row.get("read_only"):
            readings.append(key)
    width, height = (int(v) for v in sensor)
    x, y, w, h = (int(v) for v in area)
    out: Dict[str, Any] = {
        "format": FORMAT, "format_version": FORMAT_VERSION, "run": str(run),
        "written": time.strftime("%Y-%m-%d %H:%M:%S"), "at": time.time(),
        "host": str(host or ""),
        "camera": {k: str(camera.get(k) or "") for k in
                   ("backend", "model", "serial", "vendor", "kind", "label")},
        "sensor": {"width": width, "height": height},
        "area": {"x": x, "y": y, "w": w, "h": h},
        "full_sensor": (x, y, w, h) == (0, 0, width, height),
        "settings": values, "units": units, "read_only": readings,
        "preset": str(preset or ""),
        "app": {str(k): _plain(v) for k, v in dict(app or {}).items()},
        "software": {str(k): _plain(v)
                     for k, v in dict(software or {}).items()},
    }
    if settings_error:
        out["settings_error"] = str(settings_error)
    if preset_changed:
        out["preset_changed"] = {
            "name": str(preset_changed.get("name") or ""),
            "differs": [str(k) for k in preset_changed.get("differs") or []]}
    return out


# ======================================================================
# Writing it: whole, once
# ======================================================================
def write(folder: Any, run: str, record: Mapping[str, Any]) -> Path:
    """Write `record` as `run`'s camera record and return its path.

    Raises RecordExists when the run already has one (it is left as it
    is), and OSError when the folder cannot be written."""
    final = record_path(folder, run)
    text = json.dumps(dict(record), indent=2, ensure_ascii=False,
                      allow_nan=False) + "\n"
    if final.exists():
        raise RecordExists(f"{final.name} is already there — a run's record "
                           f"is written once and never replaced")
    temp = final.with_name(f".{final.name}.{os.getpid()}.tmp")
    with open(temp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        _publish(temp, final)
    except BaseException:
        # Ours, never the user's: the half-step of a write that did not
        # happen. The record itself is either all there or not there.
        try:
            os.remove(temp)
        except OSError:
            pass
        raise
    return final


def _publish(temp: Path, final: Path) -> None:
    """Put `temp` at `final` in one step, refusing an existing `final`.

    Windows' rename refuses an existing name by itself (and os.replace,
    which would not, is not used). On other systems rename REPLACES, so a
    hard link — which refuses — takes its place, and the temporary name is
    dropped after."""
    try:
        if os.name == "nt":
            os.rename(temp, final)
        else:
            os.link(temp, final)
            os.remove(temp)
    except FileExistsError as exc:
        raise RecordExists(f"{final.name} is already there — a run's record "
                           f"is written once and never replaced") from exc


# ======================================================================
# Reading it back
# ======================================================================
def read(folder: Any, run: str) -> Optional[Dict[str, Any]]:
    """`run`'s camera record, or None when it has none — or one that is not
    a record this module can read (damaged, too big, another format, a
    NEWER version of this one). Never raises for any of those."""
    if not run:
        return None
    path = record_path(folder, run)
    try:
        if path.stat().st_size > MAX_RECORD_BYTES:
            return None
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        return None
    version = doc.get("format_version")
    if not isinstance(version, int) or isinstance(version, bool) \
            or not 1 <= version <= FORMAT_VERSION:
        return None
    return doc


def _whole(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def area_of(record: Optional[Mapping[str, Any]]
            ) -> Optional[Tuple[int, int, int, int]]:
    """The run's area on the sensor, (x, y, w, h) in sensor pixels — or None
    when the record has none that can be trusted: not four whole numbers,
    an empty area, or one that does not lie inside the sensor it names."""
    if not record:
        return None
    area = record.get("area")
    sensor = record.get("sensor")
    if not isinstance(area, Mapping) or not isinstance(sensor, Mapping):
        return None
    x, y, w, h = (_whole(area.get(k)) for k in ("x", "y", "w", "h"))
    width, height = _whole(sensor.get("width")), _whole(sensor.get("height"))
    if None in (x, y, w, h, width, height):
        return None
    if x < 0 or y < 0 or w <= 0 or h <= 0:
        return None
    if x + w > width or y + h > height:
        return None
    return x, y, w, h


# ======================================================================
# Which software made the run
# ======================================================================
def source_commit(root: Any) -> str:
    """The commit the Council at `root` is checked out at — read from the
    files of its git folder, never by running git (a capture must not wait
    on a program, and a bundled build has no git). "" when `root` is not a
    git checkout or anything about it is unexpected."""
    try:
        base = Path(str(root))
        dot = base / ".git"
        if dot.is_file():
            # A worktree: ".git" is a file saying where its git folder is.
            head = dot.read_text(encoding="utf-8").strip()
            if not head.startswith("gitdir:"):
                return ""
            gitdir = Path(head[len("gitdir:"):].strip())
            if not gitdir.is_absolute():
                gitdir = base / gitdir
        elif dot.is_dir():
            gitdir = dot
        else:
            return ""
        common = gitdir
        pointer = gitdir / "commondir"
        if pointer.is_file():
            found = Path(pointer.read_text(encoding="utf-8").strip())
            common = found if found.is_absolute() else gitdir / found
        head = (gitdir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head if _is_sha(head) else ""
        ref = head[len("ref:"):].strip()
        for where in (gitdir, common):
            loose = where / ref
            if loose.is_file():
                sha = loose.read_text(encoding="utf-8").strip()
                return sha if _is_sha(sha) else ""
        packed = common / "packed-refs"
        if packed.is_file():
            for line in packed.read_text(encoding="utf-8").splitlines():
                parts = line.strip().split(" ", 1)
                if len(parts) == 2 and parts[1] == ref and _is_sha(parts[0]):
                    return parts[0]
    except (OSError, ValueError):
        return ""
    return ""


def _is_sha(text: str) -> bool:
    return len(text) in (40, 64) and all(c in "0123456789abcdef"
                                         for c in text.lower())
