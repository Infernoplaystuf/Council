"""
council_core.camera_presets — named camera set-ups (settings + the camera's
own area), saved with the app that uses them.

WHAT A PRESET IS
One preset is everything needed to put a camera back the way it was for one
job: every setting worth saving (camera_settings.snapshot) and the camera's
area — the sensor ROI, so an EVK4 emits events only inside it and a Basler
reads out only that window. "Bird bath" on a static back-yard camera is one
preset: the biases that suit the scene, and the area around the bath.

THE AREA IS IN SENSOR PIXELS, NOT PICTURE PIXELS
The live picture of a camera with an area set IS that area, so a box drawn on
it is relative to the area's origin. A preset stores what the camera itself
was told — x, y, w, h on the sensor (in the camera's own AOI units, which on a
binned Basler are binned pixels; binning is saved with it and written first).
None means "leave the area as it is".

PER PROJECT, PER CAMERA
The file lives in the app's project folder (camera_presets.json beside
camera_setup.json), so two apps can keep different set-ups and a project
copied to another machine takes its presets with it. Inside, presets are kept
per camera — backend, model and serial — because biases tuned for one EVK4
are not automatically right for another. A camera with no preset of a name
is offered the presets of the SAME MODEL saved on another unit (marked as
such): a replacement camera starts from its predecessor's set-up instead of
from nothing. Renaming and deleting only ever touch this camera's own.

THE FILE IS NEVER LOST QUIETLY
JSON, never pickle: a presets file is data a user may open, copy or mail,
and loading it must not be able to run anything. Written whole to a
temporary file and renamed over the old one, so a crash mid-write leaves the
old file or the new one, never half of each. Validated on every read: a file
that is not what this module writes raises PresetFileError and is NEVER
overwritten by an ordinary save — `save(..., repair=True)` first moves it
aside to camera_presets.damaged-<time>.json and says where. When only the
saving camera's own entry is damaged, the file is COPIED there instead and
only that entry starts again: the other cameras' presets stay listed. A file
a NEWER version wrote is not damaged and is never moved, repair or not. One bad
preset inside a good file is skipped (and named in `problems`), not fatal,
and is kept byte-for-byte when the file is rewritten: this module only
rewrites what it was asked to change.

A PERSON MAY EDIT IT
Read as UTF-8 with or without a byte-order mark (Notepad's "UTF-8 with BOM"
is the same JSON). Names are compared as a list shows them — spacing tidied,
case ignored — so a hand-typed "Bird  bath " is the preset called Bird bath.

TWO APPS, ONE PROJECT
Every change re-reads the file, changes one entry and writes it back. Two
Typhon windows on one project (one per camera) doing that at the same moment
lost each other's presets (measured: one kept 3 of its 40), so a change holds
an OS lock on a sidecar file, .camera_presets.json.lock, for its read and
write. The OS drops the lock when the process ends, so a crash never leaves
the project locked; the empty sidecar itself stays.

NO TOOLKIT, NO DEVICE STOPS HERE
Applying a preset is camera_settings.apply (settings in a safe order, then
the area); whether the stream must stop first, and stopping it, is
frame_camera's business.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from .cameras import Roi

#: Written in the app's project folder.
PRESETS_FILE = "camera_presets.json"

#: Bump if the saved shape changes. A file of a NEWER format is refused, not
#: rewritten: an older app must not destroy what a newer one saved.
FORMAT = 1

#: A preset name is a label for a list, not a document.
MAX_NAME = 60

#: Bigger than any presets file this module writes by orders of magnitude;
#: something else entirely has been saved under this name.
MAX_FILE_BYTES = 4 * 1024 * 1024

#: How long a change waits for another app's change to the same file.
LOCK_SECONDS = 10.0

_LOCK = threading.Lock()


class PresetError(Exception):
    """Something about a preset the user can put right: a name that is
    taken, empty or unknown."""


class PresetFileError(PresetError):
    """The presets file exists but is not one this module can trust. It is
    left exactly as it is; `path` names it. `newer`: a newer version of the
    app wrote it — not damage, so it is never moved aside."""

    def __init__(self, message: str, path: Optional[Path] = None,
                 newer: bool = False):
        super().__init__(message)
        self.path = path
        self.newer = newer


# ======================================================================
# Which camera a preset belongs to
# ======================================================================
@dataclass(frozen=True)
class Identity:
    """A camera as presets know it: backend, model and serial."""
    backend: str
    model: str
    serial: str = ""
    kind: str = "frame"

    @classmethod
    def of(cls, info: Any) -> "Identity":
        """From a cameras.CameraInfo (or anything shaped like one)."""
        return cls(backend=str(getattr(info, "backend", "") or ""),
                   model=str(getattr(info, "model", "") or ""),
                   serial=str(getattr(info, "serial", "") or ""),
                   kind=str(getattr(info, "kind", "frame") or "frame"))

    @property
    def key(self) -> str:
        return f"{self.backend}|{self.model}|{self.serial}"

    @property
    def model_key(self) -> str:
        return f"{self.backend}|{self.model}"

    @property
    def label(self) -> str:
        head = self.model or self.backend or "camera"
        return f"{head} ({self.serial})" if self.serial else head

    def as_dict(self) -> Dict[str, str]:
        return {"backend": self.backend, "model": self.model,
                "serial": self.serial, "kind": self.kind}


# ======================================================================
# A preset
# ======================================================================
@dataclass(frozen=True)
class Preset:
    name: str
    settings: Dict[str, Any] = field(default_factory=dict)
    #: The camera's area in sensor pixels, or None to leave it alone.
    roi: Optional[Roi] = None
    created: str = ""
    updated: str = ""
    note: str = ""
    #: The camera it was saved on.
    owner: Optional[Identity] = None
    #: Saved on THIS camera, rather than another unit of the same model.
    own: bool = True

    def line(self) -> str:
        """One row for a list: name, area, how much it sets, whose."""
        bits = [self.name]
        if self.roi is not None:
            bits.append("area " + ", ".join(str(v) for v in
                                            self.roi.as_tuple()))
        count = len(self.settings)
        bits.append(f"{count} setting{'s' if count != 1 else ''}")
        text = " · ".join(bits)
        if not self.own and self.owner is not None:
            text += f" (saved on {self.owner.label})"
        return text

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "settings": dict(self.settings),
                "roi": list(self.roi.as_tuple()) if self.roi else None,
                "created": self.created, "updated": self.updated,
                "note": self.note, "own": self.own,
                "camera": self.owner.as_dict() if self.owner else None,
                "line": self.line()}


def presets_path(project_dir: Any) -> Path:
    return Path(project_dir) / PRESETS_FILE


def _tidy(name: Any) -> str:
    """A name as a list shows it: runs of spacing made one space."""
    return " ".join(str(name if name is not None else "").split())


def _fold(name: Any) -> str:
    """A name as names are compared: tidied, case ignored."""
    return _tidy(name).casefold()


def clean_name(name: Any) -> str:
    """A usable preset name, or PresetError saying why not."""
    text = _tidy(name)
    if not text:
        raise PresetError("give the preset a name")
    if len(text) > MAX_NAME:
        raise PresetError(f"a preset name is at most {MAX_NAME} characters")
    if any(ord(c) < 32 for c in text):
        raise PresetError("a preset name cannot hold control characters")
    return text


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ======================================================================
# Validation — what a read accepts
# ======================================================================
def _scalar(value: Any) -> bool:
    if isinstance(value, bool) or isinstance(value, str):
        return True
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _roi_from(value: Any) -> Optional[Roi]:
    """[x, y, w, h] -> Roi; None -> None; anything else -> ValueError."""
    if value is None:
        return None
    if (not isinstance(value, list) or len(value) != 4
            or not all(isinstance(v, int) and not isinstance(v, bool)
                       for v in value)):
        raise ValueError("the area must be [x, y, w, h] in whole pixels")
    x, y, w, h = value
    if min(x, y) < 0 or w <= 0 or h <= 0:
        raise ValueError("the area must have a size and lie on the sensor")
    return Roi(x, y, w, h)


def _preset_from(name: str, raw: Any, owner: Identity, own: bool) -> Preset:
    if not isinstance(raw, dict):
        raise ValueError("not a preset")
    settings = raw.get("settings", {})
    if not isinstance(settings, dict) or not all(
            isinstance(k, str) and _scalar(v) for k, v in settings.items()):
        raise ValueError("its settings are not plain name: value pairs")
    texts = {k: raw.get(k, "") for k in ("created", "updated", "note")}
    if not all(isinstance(v, str) for v in texts.values()):
        raise ValueError("its dates or note are not text")
    return Preset(name=name, settings=dict(settings),
                  roi=_roi_from(raw.get("roi")), owner=owner, own=own,
                  **texts)


def _empty() -> Dict[str, Any]:
    return {"format": FORMAT, "cameras": {}}


# ======================================================================
# The store
# ======================================================================
class PresetStore:
    """One project's presets file."""

    def __init__(self, path: Any):
        self.path = Path(path)
        #: Presets skipped by the last listing, as "name: why" lines.
        self.problems: List[str] = []
        #: What the last save(repair=True) set aside: "file" (the whole file
        #: was damaged, moved aside, a new one started), "entry" (only this
        #: camera's entry was; the file was copied aside and only that entry
        #: started again), or "".
        self.repaired = ""

    # -- reading -------------------------------------------------------
    def read(self) -> Dict[str, Any]:
        """The whole document, checked. A missing file is an empty one;
        anything this module would not have written is PresetFileError."""
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return _empty()
        except OSError as exc:
            raise PresetFileError(f"cannot read {self.path.name}: {exc}",
                                  self.path) from exc
        if size > MAX_FILE_BYTES:
            raise PresetFileError(f"{self.path.name} is {size:,} bytes — not "
                                  f"a presets file", self.path)
        try:
            # utf-8-sig: the same JSON with Notepad's byte-order mark in
            # front was called damaged, and the next save moved it aside.
            text = self.path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            raise PresetFileError(f"cannot read {self.path.name}: {exc}",
                                  self.path) from exc
        if not text.strip():
            raise PresetFileError(f"{self.path.name} is empty", self.path)
        try:
            doc = json.loads(text)
        except (ValueError, RecursionError) as exc:
            # RecursionError: arrays nested past Python's stack, which the
            # size limit alone does not rule out.
            raise PresetFileError(f"{self.path.name} is damaged (not valid "
                                  f"JSON: {str(exc)[:120]})",
                                  self.path) from exc
        if not isinstance(doc, dict) or not isinstance(doc.get("format"), int):
            raise PresetFileError(f"{self.path.name} is not a presets file",
                                  self.path)
        if doc["format"] > FORMAT:
            raise PresetFileError(f"{self.path.name} was saved by a newer "
                                  f"version of this app (format "
                                  f"{doc['format']})", self.path, newer=True)
        if doc["format"] < 1 or not isinstance(doc.get("cameras"), dict):
            raise PresetFileError(f"{self.path.name} is not a presets file",
                                  self.path)
        return doc

    def presets(self, camera: Identity) -> List[Preset]:
        """This camera's presets, then the same model's from other units
        whose names this camera has not used. Sorted by name in each."""
        doc = self.read()
        self.problems = []
        own: List[Preset] = []
        others: List[Preset] = []
        for key, entry in doc["cameras"].items():
            if not isinstance(entry, dict):
                self.problems.append(f"{key}: not a camera entry")
                continue
            owner = _owner(key, entry)
            if key == camera.key:
                mine = True
            elif owner.model_key == camera.model_key and owner.model:
                mine = False
            else:
                continue
            raw = entry.get("presets", {})
            if not isinstance(raw, dict):
                self.problems.append(f"{key}: its presets are not a list "
                                     f"of names")
                continue
            for name, body in raw.items():
                try:
                    # Listed TIDIED, as save would have stored it: a name a
                    # person typed into the file as "Bird  bath " is shown,
                    # picked and found as "Bird bath". One no list could
                    # show (blank, too long) is a problem, not a blank row.
                    shown = clean_name(name)
                except PresetError as exc:
                    self.problems.append(f"{name!r}: {exc}")
                    continue
                try:
                    preset = _preset_from(shown, body, owner, mine)
                except ValueError as exc:
                    self.problems.append(f"{name}: {exc}")
                    continue
                (own if mine else others).append(preset)
        own.sort(key=lambda p: p.name.casefold())
        seen: set = set()
        unique = []
        for preset in own:
            if _fold(preset.name) in seen:
                self.problems.append(f"{preset.name}: another preset of "
                                     f"this camera has the same name")
                continue
            seen.add(_fold(preset.name))
            unique.append(preset)
        own = unique
        borrowed = []
        # Newest first within a name, then by name: of two units' presets
        # with one name, the one saved most recently is offered.
        others.sort(key=lambda p: p.updated, reverse=True)
        for preset in sorted(others, key=lambda p: p.name.casefold()):
            folded = _fold(preset.name)
            if folded in seen:
                continue
            seen.add(folded)
            borrowed.append(preset)
        return own + borrowed

    def get(self, camera: Identity, name: Any) -> Preset:
        wanted = _fold(clean_name(name))
        for preset in self.presets(camera):
            if _fold(preset.name) == wanted:
                return preset
        raise PresetError(f"there is no preset called {clean_name(name)!r} "
                          f"for {camera.label}")

    # -- writing -------------------------------------------------------
    def save(self, camera: Identity, name: Any, settings: Mapping[str, Any],
             roi: Optional[Roi] = None, note: str = "",
             repair: bool = False) -> Tuple[Preset, bool, Optional[Path]]:
        """Save (or replace) one of this camera's presets.

        Returns the preset, whether it replaced one of the same name, and
        where a damaged file was moved to (`repair` only) or None.
        """
        name = clean_name(name)
        clean = {}
        for key, value in dict(settings).items():
            if not isinstance(key, str) or not _scalar(value):
                raise PresetError(f"{key!r} cannot be saved: only plain "
                                  f"numbers, text and on/off are")
            clean[key] = value
        area = None if roi is None else [int(v) for v in roi.as_tuple()]
        if area is not None:
            _roi_from(area)                      # the same check a read uses
        with self._changing():
            moved = None
            self.repaired = ""
            try:
                doc = self.read()
            except PresetFileError as exc:
                # A newer app's file is not damage: moving it aside would
                # hide every preset the newer app saved from it.
                if exc.newer or not repair or not self.path.exists():
                    raise
                moved = self._move_aside()
                self.repaired = "file"
                doc = _empty()
            entry = doc["cameras"].get(camera.key)
            if entry is not None and not (
                    isinstance(entry, dict)
                    and isinstance(entry.get("presets", {}), dict)):
                if not repair:
                    raise PresetFileError(
                        f"the presets saved for {camera.label} in "
                        f"{self.path.name} are damaged", self.path)
                # ONLY THIS CAMERA'S ENTRY IS DAMAGED. The rest of the file
                # is good — other cameras' presets, another Typhon's on the
                # same project — and moving the whole file aside hid every
                # one of them (measured: the other camera then listed
                # none). The file as it was is COPIED aside, so the damaged
                # entry is kept too, and only that entry starts again.
                moved = self._copy_aside()
                self.repaired = "entry"
                del doc["cameras"][camera.key]
            entry = _entry_for(doc, camera)
            presets = entry["presets"]
            old_name = _find(presets, name)
            replaced = old_name is not None
            created = _now()
            if replaced:
                body = presets.pop(old_name)
                if isinstance(body, dict) and isinstance(body.get("created"),
                                                         str):
                    created = body["created"]
            presets[name] = {"settings": clean, "roi": area,
                             "created": created, "updated": _now(),
                             "note": str(note or "")}
            self._write(doc)
        return (_preset_from(name, presets[name], camera, True), replaced,
                moved)

    def rename(self, camera: Identity, old: Any, new: Any) -> Preset:
        old, new = clean_name(old), clean_name(new)
        with self._changing():
            doc = self.read()
            entry = doc["cameras"].get(camera.key)
            presets = entry.get("presets") if isinstance(entry, dict) else None
            found = _find(presets, old) if isinstance(presets, dict) else None
            if found is None:
                raise PresetError(self._not_own(camera, old))
            clash = _find(presets, new)
            if clash is not None and clash != found:
                raise PresetError(f"there is already a preset called "
                                  f"{clash!r}")
            body = presets.pop(found)
            if isinstance(body, dict):
                body["updated"] = _now()
            presets[new] = body
            self._write(doc)
        try:
            return _preset_from(new, body, camera, True)
        except ValueError:
            return Preset(name=new, owner=camera)    # renamed, still unread

    def delete(self, camera: Identity, name: Any) -> Preset:
        name = clean_name(name)
        with self._changing():
            doc = self.read()
            entry = doc["cameras"].get(camera.key)
            presets = entry.get("presets") if isinstance(entry, dict) else None
            found = _find(presets, name) if isinstance(presets, dict) else None
            if found is None:
                raise PresetError(self._not_own(camera, name))
            body = presets.pop(found)
            self._write(doc)
        try:
            return _preset_from(_tidy(found), body, camera, True)
        except ValueError:
            return Preset(name=_tidy(found), owner=camera)

    def _not_own(self, camera: Identity, name: str) -> str:
        """Why a rename or delete found nothing to act on."""
        try:
            listed = self.presets(camera)
        except PresetFileError:
            listed = []
        for preset in listed:
            if _fold(preset.name) == _fold(name) and not preset.own:
                return (f"{preset.name!r} was saved on {preset.owner.label}; "
                        f"only that camera's own list can change it — save "
                        f"it here under a name of its own instead")
        return f"there is no preset called {name!r} for {camera.label}"

    # -- the file --------------------------------------------------------
    @contextmanager
    def _changing(self) -> Iterator[None]:
        """One read-change-write at a time: this process's threads (_LOCK)
        and every other app on the same project (the OS lock)."""
        with _LOCK, _project_lock(self.path, LOCK_SECONDS):
            yield

    def _write(self, doc: Dict[str, Any]) -> None:
        """Whole, then renamed into place. fsync'd: a laptop that sleeps
        or loses power straight after a save keeps the save."""
        text = json.dumps(doc, indent=2, ensure_ascii=False,
                          allow_nan=False) + "\n"
        temp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(temp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            # Said as what it is. A project folder nested deep enough puts
            # the temp file past Windows' 260-character limit, and all
            # Python says then is "No such file or directory" for a folder
            # that plainly exists (measured: a 240-character project path).
            raise PresetError(_cannot_write(temp, exc)) from exc
        for attempt in range(5):
            try:
                os.replace(temp, self.path)
                return
            except PermissionError:
                # Windows: a virus scanner or an editor holding the old file
                # for a moment. Brief retries, then the honest error.
                if attempt == 4:
                    raise
                time.sleep(0.05)

    def _aside_name(self) -> Path:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        stem = self.path.stem
        target = self.path.with_name(f"{stem}.damaged-{stamp}.json")
        n = 1
        while target.exists():
            n += 1
            target = self.path.with_name(f"{stem}.damaged-{stamp}_{n}.json")
        return target

    def _move_aside(self) -> Path:
        target = self._aside_name()
        os.replace(self.path, target)
        return target

    def _copy_aside(self) -> Path:
        """The file as it is, copied — byte for byte — beside it."""
        target = self._aside_name()
        try:
            target.write_bytes(self.path.read_bytes())
        except OSError as exc:
            raise PresetError(_cannot_write(target, exc)) from exc
        return target


#: Windows' classic path limit (MAX_PATH), counting the terminating NUL.
WINDOWS_MAX_PATH = 260


def _cannot_write(path: Path, exc: OSError) -> str:
    said = f"cannot save the presets file in {path.parent}: {exc.strerror or exc}"
    if os.name == "nt" and len(str(path.absolute())) >= WINDOWS_MAX_PATH - 1:
        said += (f" — its path is {len(str(path.absolute()))} characters, "
                 f"past the {WINDOWS_MAX_PATH} Windows allows; move the "
                 f"project to a shorter folder")
    return said


def _owner(key: str, entry: Mapping[str, Any]) -> Identity:
    """The camera an entry belongs to: its own fields, else its key."""
    parts = (str(key).split("|") + ["", "", ""])[:3]
    return Identity(backend=str(entry.get("backend") or parts[0]),
                    model=str(entry.get("model") or parts[1]),
                    serial=str(entry.get("serial") or parts[2]),
                    kind=str(entry.get("kind") or "frame"))


def _entry_for(doc: Dict[str, Any], camera: Identity) -> Dict[str, Any]:
    """This camera's entry, made if it has none. (A DAMAGED one never gets
    here: save refuses it, or moves the whole file aside, first.)"""
    entry = doc["cameras"].get(camera.key)
    if entry is None:
        entry = dict(camera.as_dict(), presets={})
        doc["cameras"][camera.key] = entry
    entry.setdefault("presets", {})
    return entry


def _find(presets: Mapping[str, Any], name: str) -> Optional[str]:
    """The stored spelling of `name`, matched as a list shows names: case
    and runs of spacing ignored (a person may have typed the file)."""
    wanted = _fold(name)
    for stored in presets:
        if _fold(stored) == wanted:
            return stored
    return None


# ======================================================================
# One change at a time, across every app using the project
# ======================================================================
def _lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


def _try_lock(handle: Any) -> bool:
    """Take the OS lock on byte 0 of `handle` without waiting; whether it
    was taken. msvcrt on Windows (locking past the end of an empty file is
    allowed there), flock elsewhere."""
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(handle: Any) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass                      # closing the handle releases it anyway


@contextmanager
def _project_lock(path: Path, timeout: float) -> Iterator[None]:
    """Hold the project's presets lock (see TWO APPS, ONE PROJECT)."""
    lock = _lock_path(path)
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock, "a+b")
    except OSError as exc:
        raise PresetError(_cannot_write(lock, exc)) from exc
    try:
        deadline = time.monotonic() + timeout
        while not _try_lock(handle):
            if time.monotonic() >= deadline:
                raise PresetError(
                    f"another window is changing the presets in "
                    f"{path.parent} and has not finished — try again")
            time.sleep(0.01)
        try:
            yield
        finally:
            _unlock(handle)
    finally:
        handle.close()


# ======================================================================
# A preset and a camera
# ======================================================================
def capture(device: Any, include_roi: bool = True
            ) -> Tuple[Dict[str, Any], Optional[Roi]]:
    """What a preset of this camera, as it is now, holds."""
    from . import camera_settings

    settings = camera_settings.snapshot(device)
    roi = device.roi() if include_roi else None
    return settings, roi


def apply(device: Any, preset: Preset) -> Any:
    """Settings in a safe order, then the area: camera_settings.apply. Raises
    NeedsStop, before writing anything, if the stream is in the way."""
    from . import camera_settings

    return camera_settings.apply(device, preset.settings, preset.roi)
