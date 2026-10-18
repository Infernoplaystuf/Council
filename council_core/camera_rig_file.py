"""
council_core.camera_rig_file — camera_rig.json: the name each of a
project's cameras goes by in the rig, and the project's rig presets.

WHERE THIS COMES FROM
The multi-camera design (docs/typhon_multicamera_design.md 2.2 and 2.5, M1
row 4) keeps a project's camera NAMES and RIG PRESETS in a new project
file, camera_rig.json, beside camera_setup.json and camera_presets.json;
the plugin design builds that row in stage P2 (docs/typhon_plugins_design.md
5.4, 12) and keeps the triggering plugin's rig-wide set-up there later, as
a namespaced block (3.6; stage P6).

THE NAMES
cam1-cam4 by default, renameable, short and safe in a folder name (at most
16 characters, a-z, 0-9 and "-": council_core.camera_rig.NAME_RE). Each
name stands for one camera, by its backend and serial — the serial is what
stays the same: an EVK4's model is read only once it is open
(cameras.identify) — or, for a camera with no serial, by its backend and
the key it is found by — and never across kinds: a frame camera is not an
event camera, whatever its serial says (`Named.is_camera`). The same camera
gets the same name next time (`assign`), and a new camera gets the name it
is offered when no other camera has it, else the first free camN.

RIG PRESETS
Which preset goes on which camera: {rig preset: {camera name: preset
name}}. The presets themselves stay per camera in camera_presets.json
(council_core.camera_presets); this file only names them. Putting one on
the open cameras is M1 row 5 (stage P6); `plan` already says what it would
do — which preset goes on which open camera, which camera it names is not
open, which open camera it leaves alone.

ADDITIVE
A new file: camera_setup.json and camera_presets.json are unchanged by it.
Inside it, whatever this module does not know — a block a later Typhon or a
plugin keeps at the top level, a field of a camera's entry — is kept as it
is when the file is rewritten: this module changes only what it was asked
to change.

THE FILE IS NEVER LOST QUIETLY (camera_presets.json's rules)
JSON only, never pickle. Every change re-reads the file under an OS lock on
a sidecar file (.camera_rig.json.lock), changes it, and writes it whole
under a temporary name no other writer uses, flushed to disk, then renamed
into place. A file that is damaged, is not one of these, or was written by
a newer Typhon raises RigFileError and is left exactly as it is: no change
writes over it, and reading never writes anything.

No Qt, no device and no frame_camera here.
"""
from __future__ import annotations

import itertools
import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import (Any, Callable, Dict, Iterable, Iterator, List, Mapping,
                    Optional, Tuple)

from .camera_rig import NAME_RE

#: Written in the app's project folder.
RIG_FILE = "camera_rig.json"
FORMAT_NAME = "typhon-camera-rig"
#: Bump if the shape changes. A file of a NEWER version is refused, not
#: rewritten: an older Typhon must not destroy what a newer one saved.
FORMAT_VERSION = 1

#: Far bigger than any rig file; something else has this name.
MAX_FILE_BYTES = 1024 * 1024
#: Cameras a project may have named over its life (a rig runs at most 4).
MAX_CAMERAS = 64
MAX_RIG_PRESETS = 200
#: A rig preset's name, and a preset's, as camera_presets keeps them.
MAX_NAME = 60
#: How long a change waits for another window's change to the same file.
LOCK_SECONDS = 10.0

#: The camera entry's own fields; any other field is kept as it is.
ENTRY_FIELDS = ("backend", "key", "serial", "model", "kind", "named")

_LOCK = threading.RLock()
_TEMP_NUMBERS = itertools.count(1)


class RigFileError(Exception):
    """camera_rig.json exists but is not one this module can trust, or it
    cannot be written. It is left exactly as it is; `path` names it.
    `newer`: a newer Typhon wrote it — not damage."""

    def __init__(self, message: str, path: Optional[Path] = None,
                 newer: bool = False):
        super().__init__(message)
        self.path = path
        self.newer = newer


class RigError(ValueError):
    """Something the user can put right: a name that is taken, unsafe or
    unknown."""


def rig_path(project_dir: Any) -> Path:
    return Path(project_dir) / RIG_FILE


# ======================================================================
# What the file holds
# ======================================================================
@dataclass(frozen=True)
class Named:
    """A camera the rig knows by `name`."""
    name: str
    backend: str
    key: str
    serial: str = ""
    model: str = ""
    kind: str = ""

    def is_camera(self, info: Any) -> bool:
        """Whether `info` (a cameras.CameraInfo, or anything shaped like
        one) is this camera: the same backend and serial, or — with no
        serial on either side — the same backend and key. A camera of
        another kind (a frame camera, where this one is an event camera)
        is never this one, whatever its serial: a kind not read yet on
        either side does not count against it."""
        if _text(info, "backend") != self.backend:
            return False
        kind = _text(info, "kind")
        if self.kind and kind and kind != self.kind:
            return False
        serial = _text(info, "serial")
        if self.serial and serial:
            return serial == self.serial
        return _text(info, "key") == self.key


@dataclass(frozen=True)
class RigPlan:
    """What putting rig preset `preset` on the open cameras would do:
    `apply` — open camera name: its preset; `missing` — cameras it names
    that are not open; `untouched` — open cameras it does not name."""
    preset: str
    apply: Dict[str, str] = field(default_factory=dict)
    missing: List[str] = field(default_factory=list)
    untouched: List[str] = field(default_factory=list)


def _text(info: Any, name: str) -> str:
    return str(getattr(info, name, "") or "")


def _tidy(name: Any) -> str:
    return " ".join(str(name or "").split())


def _fold(name: Any) -> str:
    return _tidy(name).casefold()


def clean_label(name: Any, what: str = "a rig preset") -> str:
    """A rig preset's (or a preset's) name as a list shows it: spacing
    tidied, 1-60 characters."""
    tidy = _tidy(name)
    if not tidy:
        raise RigError(f"give {what} a name")
    if len(tidy) > MAX_NAME:
        raise RigError(f"{what}'s name is {len(tidy)} characters; at most "
                       f"{MAX_NAME}")
    return tidy


def check_name(name: Any) -> str:
    """A camera's name, or RigError saying what a name may be."""
    text = str(name or "")
    if not NAME_RE.match(text):
        raise RigError(f"{text!r} cannot name a camera: use at most 16 "
                       f"characters, a-z, 0-9 and '-', starting with a "
                       f"letter or digit")
    return text


def _skeleton() -> Dict[str, Any]:
    return {"format": FORMAT_NAME, "format_version": FORMAT_VERSION,
            "cameras": {}, "rig_presets": {}}


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a number JSON allows")


def _checked(doc: Any, path: Path) -> Dict[str, Any]:
    """`doc` as a rig document, or RigFileError naming what is wrong."""
    if not isinstance(doc, dict) or doc.get("format") != FORMAT_NAME:
        raise RigFileError(f"{path} is not a camera rig file; it is left as "
                           f"it is", path)
    version = doc.get("format_version")
    if not isinstance(version, int) or isinstance(version, bool) \
            or version < 1:
        raise RigFileError(f"{path} has no format_version; it is left as "
                           f"it is", path)
    if version > FORMAT_VERSION:
        raise RigFileError(f"{path} was written by a newer Typhon "
                           f"(format_version {version}); it is left as it "
                           f"is", path, newer=True)
    cams = doc.setdefault("cameras", {})
    if not isinstance(cams, dict):
        raise RigFileError(f"{path}: cameras is not name: camera", path)
    for name, entry in cams.items():
        if not isinstance(name, str) or not NAME_RE.match(name):
            raise RigFileError(f"{path}: {name!r} is not a camera name; the "
                               f"file is left as it is", path)
        if not isinstance(entry, dict) or not all(
                isinstance(entry.get(k, ""), str) for k in ENTRY_FIELDS) \
                or not entry.get("backend") or not entry.get("key"):
            raise RigFileError(f"{path}: camera {name}'s entry is damaged "
                               f"(it needs a backend and a key); the file "
                               f"is left as it is", path)
    presets = doc.setdefault("rig_presets", {})
    if not isinstance(presets, dict):
        raise RigFileError(f"{path}: rig_presets is not name: preset", path)
    for name, entry in presets.items():
        on = entry.get("cameras") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not _tidy(name) \
                or not isinstance(on, dict) or not all(
                    isinstance(c, str) and NAME_RE.match(c)
                    and isinstance(p, str) and _tidy(p)
                    for c, p in on.items()):
            raise RigFileError(f"{path}: rig preset {name!r} is damaged; the "
                               f"file is left as it is", path)
    return doc


# ======================================================================
# The file
# ======================================================================
class RigFile:
    """One project's camera_rig.json. Cheap to make: it reads nothing until
    asked, and re-reads the file at every question (another window may have
    changed it)."""

    def __init__(self, path: Any):
        self.path = Path(path)

    # -- reading -----------------------------------------------------------
    def read(self) -> Dict[str, Any]:
        """The document, checked — a new one when there is no file (nothing
        is written by reading). RigFileError for a file it cannot trust."""
        path = self.path
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return _skeleton()
        except OSError as exc:
            raise RigFileError(f"cannot read {path}: {exc}", path) from exc
        if size > MAX_FILE_BYTES:
            raise RigFileError(f"{path} is {size:,} bytes — not a camera rig "
                               f"file; it is left as it is", path)
        try:
            text = path.read_text(encoding="utf-8-sig")
            doc = json.loads(text, parse_constant=_no_constant)
        except (OSError, UnicodeDecodeError, ValueError,
                RecursionError) as exc:
            raise RigFileError(f"{path} is damaged ({str(exc)[:120]}); it is "
                               f"left as it is", path) from exc
        return _checked(doc, path)

    def cameras(self) -> List[Named]:
        """Every camera the rig has named, in the file's order."""
        return [_named(name, entry)
                for name, entry in self.read()["cameras"].items()]

    def name_of(self, info: Any) -> Optional[str]:
        """The name camera `info` goes by in this rig, or None."""
        return _name_in(self.read(), info)

    # -- naming --------------------------------------------------------------
    def assign(self, info: Any, *, prefer: Optional[str] = None,
               taken: Iterable[str] = ()) -> str:
        """The name of camera `info`: the one it had before — the same
        camera gets the same name next time — else `prefer` when no other
        camera has it and it is not `taken` (names in use in the rig right
        now), else the first free camN. A new camera, or a known one whose
        model is now read more fully (an EVK4's sensor) or whose kind was
        not known, is written down; nothing else in the file changes. A
        known kind is never rewritten: a camera of another kind is another
        camera (Named.is_camera)."""
        if prefer is not None:
            check_name(prefer)
        busy = set(taken)
        backend, key = _text(info, "backend"), _text(info, "key")
        if not backend or not key:
            raise RigError("a camera needs a backend and a key to be named")

        chosen: List[str] = []

        def change(doc: Dict[str, Any]) -> bool:
            cams = doc["cameras"]
            have = _name_in(doc, info)
            if have is not None:
                chosen.append(have)
                entry = cams[have]
                fresh = {}
                if _text(info, "model") and                         _text(info, "model") != entry.get("model"):
                    fresh["model"] = _text(info, "model")
                for k in ("kind", "serial"):
                    if _text(info, k) and not entry.get(k):
                        fresh[k] = _text(info, k)
                entry.update(fresh)
                return bool(fresh)
            if len(cams) >= MAX_CAMERAS:
                raise RigError(f"this project has named {len(cams)} cameras; "
                               f"forget one first (at most {MAX_CAMERAS})")
            used = set(cams) | busy
            if prefer is not None and prefer not in used:
                name = prefer
            else:
                name = next(f"cam{i}" for i in itertools.count(1)
                            if f"cam{i}" not in used)
            cams[name] = {"backend": backend, "key": key,
                          "serial": _text(info, "serial"),
                          "model": _text(info, "model"),
                          "kind": _text(info, "kind"),
                          "named": time.strftime("%Y-%m-%d %H:%M:%S")}
            chosen.append(name)
            return True

        self._change(change)
        return chosen[0]

    def rename(self, old: str, new: str) -> None:
        """Camera `old` is called `new` from now on, in every rig preset
        too. Refused when `new` is not a safe name or another camera has
        it."""
        check_name(new)

        def change(doc: Dict[str, Any]) -> bool:
            cams = doc["cameras"]
            if old not in cams:
                raise RigError(f"no camera is named {old!r}")
            if new == old:
                return False
            if new in cams:
                raise RigError(f"a camera is already named {new!r}")
            doc["cameras"] = {(new if k == old else k): v
                              for k, v in cams.items()}
            for entry in doc["rig_presets"].values():
                on = entry["cameras"]
                entry["cameras"] = {(new if k == old else k): v
                                    for k, v in on.items()}
            return True

        self._change(change)

    def forget(self, name: str) -> None:
        """Drop camera `name`; the next camera may take its name. Rig
        presets that name it keep it: `plan` lists it as missing."""
        def change(doc: Dict[str, Any]) -> bool:
            if name not in doc["cameras"]:
                raise RigError(f"no camera is named {name!r}")
            del doc["cameras"][name]
            return True

        self._change(change)

    # -- rig presets -----------------------------------------------------------
    def rig_presets(self) -> Dict[str, Dict[str, str]]:
        """{rig preset: {camera name: preset name}}, in the file's order."""
        return {name: dict(entry["cameras"])
                for name, entry in self.read()["rig_presets"].items()}

    def save_rig_preset(self, name: Any, cameras: Mapping[str, Any], *,
                        replace: bool = False) -> str:
        """Save rig preset `name`: which preset goes on which camera.
        Returns its name as saved. A name already used (case and spacing
        ignored) is refused unless `replace`, which keeps that rig preset's
        spelling and replaces what it holds."""
        clean = clean_label(name)
        on: Dict[str, str] = {}
        for camera, preset in dict(cameras or {}).items():
            on[check_name(camera)] = clean_label(preset, "a preset")
        if not on:
            raise RigError("a rig preset puts a preset on at least one "
                           "camera")
        saved: List[str] = []

        def change(doc: Dict[str, Any]) -> bool:
            presets = doc["rig_presets"]
            same = _find(presets, clean)
            if same is not None and not replace:
                raise RigError(f"a rig preset is already called {same!r}")
            if same is None and len(presets) >= MAX_RIG_PRESETS:
                raise RigError(f"this project has {len(presets)} rig "
                               f"presets; at most {MAX_RIG_PRESETS}")
            key = same if same is not None else clean
            entry = dict(presets.get(key, {}))
            entry["cameras"] = on
            entry["saved"] = time.strftime("%Y-%m-%d %H:%M:%S")
            presets[key] = entry
            saved.append(key)
            return True

        self._change(change)
        return saved[0]

    def rename_rig_preset(self, old: Any, new: Any) -> str:
        clean = clean_label(new)
        done: List[str] = []

        def change(doc: Dict[str, Any]) -> bool:
            presets = doc["rig_presets"]
            have = _find(presets, old)
            if have is None:
                raise RigError(f"no rig preset is called {_tidy(old)!r}")
            other = _find(presets, clean)
            if other is not None and other != have:
                raise RigError(f"a rig preset is already called {other!r}")
            doc["rig_presets"] = {(clean if k == have else k): v
                                  for k, v in presets.items()}
            done.append(clean)
            return clean != have

        self._change(change)
        return done[0]

    def delete_rig_preset(self, name: Any) -> str:
        gone: List[str] = []

        def change(doc: Dict[str, Any]) -> bool:
            presets = doc["rig_presets"]
            have = _find(presets, name)
            if have is None:
                raise RigError(f"no rig preset is called {_tidy(name)!r}")
            del presets[have]
            gone.append(have)
            return True

        self._change(change)
        return gone[0]

    def plan(self, name: Any, open_cameras: Iterable[str]) -> RigPlan:
        """What putting rig preset `name` on the cameras open now (their
        names, in the rig's order) would do. Nothing is changed."""
        presets = self.read()["rig_presets"]
        have = _find(presets, name)
        if have is None:
            raise RigError(f"no rig preset is called {_tidy(name)!r}")
        on = presets[have]["cameras"]
        opened = list(dict.fromkeys(open_cameras))
        return RigPlan(preset=have,
                       apply={c: on[c] for c in opened if c in on},
                       missing=[c for c in on if c not in opened],
                       untouched=[c for c in opened if c not in on])

    # -- writing ---------------------------------------------------------------
    def _change(self, change: Callable[[Dict[str, Any]], bool]) -> None:
        """Re-read the file under the lock, let `change` alter it, and write
        it whole when it says it changed something. A file this module
        cannot trust is refused before anything is written."""
        with _locked(self.path):
            doc = self.read()
            if change(doc):
                _write_whole(self.path, doc)


def _named(name: str, entry: Mapping[str, Any]) -> Named:
    return Named(name=name, backend=str(entry.get("backend", "")),
                 key=str(entry.get("key", "")),
                 serial=str(entry.get("serial", "") or ""),
                 model=str(entry.get("model", "") or ""),
                 kind=str(entry.get("kind", "") or ""))


def _name_in(doc: Mapping[str, Any], info: Any) -> Optional[str]:
    for name, entry in doc["cameras"].items():
        if _named(name, entry).is_camera(info):
            return name
    return None


def _find(presets: Mapping[str, Any], name: Any) -> Optional[str]:
    wanted = _fold(name)
    for have in presets:
        if _fold(have) == wanted:
            return have
    return None


# ======================================================================
# The lock and the whole-file write (camera_presets' and plugin_lists' way)
# ======================================================================
def _lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


def _try_lock(handle: Any) -> bool:
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
        pass


@contextmanager
def _locked(path: Path, timeout: float = LOCK_SECONDS) -> Iterator[None]:
    """One read-change-write of `path` at a time: this process's threads and
    every other window (an OS lock on a sidecar; the OS drops it if the
    process dies, and the empty sidecar stays)."""
    with _LOCK:
        lock = _lock_path(path)
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            handle = open(lock, "a+b")
        except OSError as exc:
            raise RigFileError(f"cannot save {path}: {exc}", path) from exc
        try:
            deadline = time.monotonic() + timeout
            while not _try_lock(handle):
                if time.monotonic() >= deadline:
                    raise RigFileError(f"another window is changing {path} "
                                       f"and has not finished — try again",
                                       path)
                time.sleep(0.01)
            try:
                yield
            finally:
                _unlock(handle)
        finally:
            handle.close()


def _temp_name(path: Path) -> Path:
    """A temporary name no other writer uses: process, thread, counter."""
    return path.with_name(f".{path.name}.{os.getpid()}."
                          f"{threading.get_ident()}.{next(_TEMP_NUMBERS)}.tmp")


def _write_whole(path: Path, doc: Mapping[str, Any]) -> None:
    """Whole, flushed, then renamed over `path` — only ever over a file this
    module read and checked a moment before, under the lock."""
    text = json.dumps(dict(doc), indent=2, ensure_ascii=False,
                      allow_nan=False) + "\n"
    temp = _temp_name(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(temp, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(temp, path)
                return
            except PermissionError:
                # Windows: a scanner or an editor holding the old file for a
                # moment. Brief retries, then the honest error.
                if attempt == 4:
                    raise
                time.sleep(0.05)
    except OSError as exc:
        try:
            os.remove(temp)          # ours: the half-step that did not happen
        except OSError:
            pass
        raise RigFileError(f"cannot save {path}: {exc}", path) from exc
