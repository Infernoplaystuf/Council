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
    plugins    A "TYPHON (PLUGINS)" RUN ONLY (added in version 1): what made
               the run — each plugin's id, name, version and short name, a
               camera plugin's own record fields — by plugin id
               (docs/typhon_plugins_design.md 3.9, 4.2)

PLUGIN DATA, ONE RULE (docs/typhon_plugins_design.md 3.9)
Keys format 1 already has stay where they are (`capture`, `downsample`);
everything new a plugin adds is in `plugins`, under its id, and nowhere
else, so no fact is ever kept in two places across old and new runs. Each
block is CLEANED (clean_block: NaN and infinities become null, tuples lists,
numbers plain numbers, numpy's included) and SIZE-CHECKED (BLOCK_BYTES each,
PLUGINS_BYTES in all) AS THE RECORD WRITES IT — indented, at the block's
own depth in the record (json_size) — so a plugin can never make a record
unwritable or unreadable: in the plugin design's prototype a NaN stopped the
record being written, and a 14.3 MB block made it unreadable, over
MAX_RECORD_BYTES. (Measured compactly before the P3 review, a block of
[x, y] points inside the limits came out 4.6 MB indented, and read() gave
None.) Cleaning runs no code of the block's own where it can help it — a
dict's items, a list's items, a str's or a number's value are read with the
built-in types' own methods — and stops at the first byte past the limit,
so an oversized block is refused at once, not after a full copy.

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
import numbers
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

#: One plugin's block in a record, as the record writes it, at most; and
#: all of them together — far under MAX_RECORD_BYTES, so the record stays
#: readable.
BLOCK_BYTES = 256 * 1024
PLUGINS_BYTES = 1024 * 1024
#: A block's nesting, at most (a cycle is refused as too deep).
MAX_DEPTH = 32
#: How write_once indents (json.dumps(indent=INDENT)), and where a plugin's
#: block sits in a record: the record is level 0, its "plugins" level 1,
#: each plugin's block level 2 (in the camera record and the end record).
INDENT = 2
PLUGINS_LEVEL = 1
BLOCK_LEVEL = 2


class RecordExists(FileExistsError):
    """A record for this run is already there; it is never replaced."""


class BlockError(ValueError):
    """A plugin's block that cannot go in a record, and why."""


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
          host: str = "",
          capture: Optional[Mapping[str, Any]] = None,
          downsample: Optional[Mapping[str, Any]] = None,
          plugins: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The record, as a dict ready for `write`.

    `settings` is what frame_camera.settings_list returns under "settings"
    (one dict per setting: key, value, unit, read_only, ...); it is kept as
    key: value with the units and read-only keys beside it — readable by a
    person, and by code that wants to put a camera back as it was for this
    run.

    ADDED IN VERSION 1, so a reader of version 1 may meet them (format_
    version stays 1: a reader that does not know them reads the rest as
    before):
      capture     a frame camera's capture set-up for the run (council_core.
                  triggered): mode, source, frames per trigger, timer, edge,
                  delay, debounce, Downsample; the trigger Start armed and
                  what arming changed; the run's triggers CSV
      downsample  Downsample ON only: the factor [x, y], the method
                  (council_core.reduce), "half_up" rounding, the picture's
                  size, and the right and bottom pixels a block could not
                  hold (`dropped`). With a camera's binning, `sensor` and
                  `area` above are in BINNED pixels, as the camera counts
                  them; with Typhon's averaging they are the camera's own,
                  and a saved pixel x covers camera pixels x*f .. x*f+f-1
                  from the area's origin (reduce.sensor_position)
      plugins     a "Typhon (plugins)" run's: plugin id -> its block, each
                  cleaned and size-checked here (plugins_block); a block
                  that cannot go in is left out (the caller said why)."""
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
    if capture:
        out["capture"] = _plain_tree(capture)
    if downsample:
        out["downsample"] = _plain_tree(downsample)
    if plugins is not None:
        out["plugins"] = plugins_block(plugins)[0]
    return out


def _plain_tree(value: Any) -> Any:
    """`value` with every leaf JSON can carry (_plain), keys as text."""
    if isinstance(value, Mapping):
        return {str(k): _plain_tree(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_tree(v) for v in value]
    return _plain(value)


# ======================================================================
# Plugins' blocks: cleaned and size-checked (plugin design 3.9)
# ======================================================================
class _Cleaner:
    """One block's walk: the plain copy, and its size AS write_once WRITES
    IT — json.dumps(indent=INDENT, ensure_ascii=False) in UTF-8, with the
    block at nesting `level` of its record — counted as it goes, so the walk
    stops at the first byte past `limit` (a block of a million points is
    refused after the first 256 KB of them, not after a copy of all).

    The size of a value at nesting level L: a scalar's JSON; "[]" or "{}"
    when empty; else the opening bracket, a newline and (L+1)*INDENT spaces
    before each item (a key is its JSON and ": " first), a comma between
    items, then a newline, L*INDENT spaces and the closing bracket. That is
    json.dumps's own layout (separators "," and ": " with an indent), so
    the count is the bytes written, exactly (a test compares the two)."""

    def __init__(self, limit: Optional[int], what: str) -> None:
        self.limit = limit
        self.what = what
        self.size = 0

    def add(self, n: int) -> None:
        self.size += n
        if self.limit is not None and self.size > self.limit:
            raise BlockError(f"{self.what} is over the {self.limit:,} bytes "
                             f"a plugin's block may be, as the record "
                             f"writes it (indented)")

    def text(self, value: str, where: str) -> str:
        if self.limit is not None and len(value) > self.limit:
            # More characters than bytes allowed: refused before encoding.
            self.add(len(value) + 2)
        try:
            encoded = json.dumps(value, ensure_ascii=False).encode("utf-8")
        except UnicodeEncodeError as exc:
            raise BlockError(f"{where} holds text that is not UTF-8 (a lone "
                             f"surrogate), which no record can hold") from exc
        self.add(len(encoded))
        return value

    def walk(self, value: Any, where: str, level: int, depth: int) -> Any:
        if depth > MAX_DEPTH:
            raise BlockError(f"{where} is nested more than {MAX_DEPTH} deep")
        if value is None:
            self.add(4)
            return None
        if isinstance(value, bool):
            self.add(4 if value else 5)
            return bool(value)
        kind = type(value)
        if (kind.__module__ or "").split(".")[0] == "numpy" \
                and getattr(value, "ndim", None) == 0 \
                and callable(getattr(value, "item", None)):
            # numpy's scalars — np.bool_ (named "bool" in numpy 2, so a
            # name test missed it), np.float32, np.int64 ... — as Python's.
            return self.walk(value.item(), where, level, depth)
        if isinstance(value, str):
            return self.text(str.__str__(value), where)
        if isinstance(value, int):
            try:
                number = int.__index__(value)
                self.add(len(int.__repr__(number)))
            except ValueError as exc:   # more digits than Python writes
                raise BlockError(f"{where} is a whole number too long to "
                                 f"write") from exc
            return number
        if isinstance(value, float):
            number = float.__float__(value)
            if not math.isfinite(number):
                self.add(4)
                return None
            self.add(len(float.__repr__(number)))
            return number
        if isinstance(value, Mapping):
            items = (dict.items(value) if isinstance(value, dict)
                     else value.items())
            self.add(1)
            out: Dict[str, Any] = {}
            for key, item in items:
                if not isinstance(key, str):
                    raise BlockError(f"{where} has the key {key!r}, which is "
                                     f"not text — a record's keys are text")
                key = str.__str__(key)
                self.add((1 if out else 0) + 1 + INDENT * (level + 1))
                self.text(key, where)
                self.add(2)
                out[key] = self.walk(item, f"{where}.{key}", level + 1,
                                     depth + 1)
            self.add(1 if not out else 1 + INDENT * level + 1)
            return out
        if isinstance(value, (list, tuple)):
            items = (list.__iter__(value) if isinstance(value, list)
                     else tuple.__iter__(value))
            self.add(1)
            made: List[Any] = []
            for i, item in enumerate(items):
                self.add((1 if made else 0) + 1 + INDENT * (level + 1))
                made.append(self.walk(item, f"{where}[{i}]", level + 1,
                                      depth + 1))
            self.add(1 if not made else 1 + INDENT * level + 1)
            return made
        if isinstance(value, numbers.Integral):
            return self.walk(int(value), where, level, depth)
        if isinstance(value, numbers.Real):
            return self.walk(float(value), where, level, depth)
        raise BlockError(f"{where} is a {type(value).__name__}, which is not "
                         f"JSON (text, a number, true or false, null, a list "
                         f"or a mapping)")


def json_size(value: Any, level: int = 0) -> int:
    """The bytes `value` (plain JSON data) takes in a record as write_once
    writes it, at nesting `level` of that record (0: the record itself)."""
    walker = _Cleaner(None, "the value")
    walker.walk(value, "the value", int(level), 0)
    return walker.size


def _clean_sized(block: Any, limit: int, level: int) -> Tuple[Dict[str, Any],
                                                              int]:
    if not isinstance(block, Mapping):
        raise BlockError(f"a block is a mapping of names to values, not a "
                         f"{type(block).__name__}")
    walker = _Cleaner(limit, "the block")
    clean = walker.walk(block, "the block", int(level), 0)
    return clean, walker.size


def clean_block(block: Any, limit: int = BLOCK_BYTES,
                level: int = BLOCK_LEVEL) -> Dict[str, Any]:
    """A plugin's block as plain JSON, or BlockError saying why it cannot
    be one: a mapping with text keys, of text, numbers, true or false,
    null, lists and mappings, at most MAX_DEPTH deep and `limit` bytes as
    the record writes it — indented, at nesting `level` of the record
    (BLOCK_LEVEL: a plugin's block under a record's "plugins"; json_size).
    CLEANED, not refused: NaN and the infinities become null (JSON has
    none: a NaN stopped a record being written), tuples lists, any number
    type a plain number (numpy's included, np.bool_ a plain true or
    false). The walk stops at the first byte past the limit."""
    return _clean_sized(block, limit, level)[0]


def plugins_block(blocks: Mapping[str, Any],
                  level: int = PLUGINS_LEVEL) -> Tuple[Dict[str, Any],
                                                       Dict[str, str]]:
    """(the blocks that go in a record, by plugin id; why each other one
    does not): each cleaned and size-checked (clean_block, at its depth in
    the record), and the record's whole `plugins` part — at nesting
    `level` — at most PLUGINS_BYTES as the record writes it, the blocks
    past it, in order, left out — so a record holding them stays under
    MAX_RECORD_BYTES."""
    kept: Dict[str, Any] = {}
    refused: Dict[str, str] = {}
    # "{", then a newline, the indent and "}" once anything is in it.
    total = 1 + 1 + INDENT * level + 1
    for pid, block in dict(blocks or {}).items():
        try:
            clean, size = _clean_sized(block, BLOCK_BYTES, level + 1)
        except BlockError as exc:
            refused[str(pid)] = str(exc)
            continue
        entry = ((1 if kept else 0) + 1 + INDENT * (level + 1)
                 + json_size(str(pid)) + 2 + size)
        if total + entry > PLUGINS_BYTES:
            refused[str(pid)] = (f"the block is {size:,} bytes as the record "
                                 f"writes it, and the plugins' blocks may be "
                                 f"{PLUGINS_BYTES:,} bytes in all")
            continue
        kept[str(pid)] = clean
        total += entry
    return kept, refused


# ======================================================================
# Writing it: whole, once
# ======================================================================
def write(folder: Any, run: str, record: Mapping[str, Any]) -> Path:
    """Write `record` as `run`'s camera record and return its path.

    Raises RecordExists when the run already has one (it is left as it
    is), and OSError when the folder cannot be written."""
    return write_once(record_path(folder, run), record)


def write_once(final: Any, record: Mapping[str, Any]) -> Path:
    """Write `record` as JSON at `final`, WHOLE and ONCE: to a temporary
    name first, then renamed into a name that must not exist — so a crash
    mid-write leaves no half record and nothing is ever replaced. The camera
    record and a run's end record (capture.Recorder.finish) both use it.

    Raises RecordExists when `final` is already there (it is left as it
    is), and OSError when the folder cannot be written."""
    final = Path(str(final))
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
