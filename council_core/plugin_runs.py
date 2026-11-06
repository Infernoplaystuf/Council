"""
council_core.plugin_runs — a run as the Typhon plugins see it: the run in
progress, the two file services, and a plugin's own data folder.

WHERE IT SITS (docs/typhon_plugins_design.md 3.5, 3.6, 3.9, 4.1, 10.3)
The plugin host (council_core.plugin_host) drives a run's hooks in the order
of design 3.6 — check_start before anything stops, the run name reserved,
the recording started, on_run_started, ... the recording over,
on_run_finished — and keeps an ActiveRun for each run between them. This
module is that ActiveRun and the file rules; it calls no plugin itself.

THE TWO FILE SERVICES: NEVER OVER ANYTHING (design 3.9 "Files")
  (a) STREAMED, during a run (`create_file`): a new file created
      EXCLUSIVELY — the file system refuses a name that exists, whoever made
      it — in one of the run in progress's folders, for data written as
      frames arrive (as the triggers CSV is).
  (b) WHOLE, at any time (`write_new`): written to a temporary name of its
      own beside it (unique to the writer: process, thread and a count),
      flushed to disk, then renamed into a name that must not exist —
      Windows' rename refuses one by itself; elsewhere a hard link, which
      refuses, does it — "_2", "_3" ... on a rerun. Beside any run, past
      runs included (the folder must hold that run's frames CSV), or in the
      plugin's data folder. The velocity design's rule for its analysis
      files (its section 4), and camera_record.write's.
A run file is named "<run><ending>", the run's name and an ending the
plugin's manifest declares in "files": the host registers every declared
ending (capture.register_run_files), so a run's name is never used again.
There is no delete: the API has none (design 10.3), and a temporary file
this module made itself is the only thing it ever removes.

A PLUGIN'S OWN DATA (design 4.1): `<project>/plugin_data/<id>/`, made on
first ask, holding no code — the Designer's policy gate checks every .py
file under a project (gui_policy), and a plugin's data is not code.

No Qt, no pickle, no network.
"""
from __future__ import annotations

import itertools
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import typhon_plugin_api as api

#: A plugin's data for one project: <project>/plugin_data/<id>/ (design 4.1).
DATA_FOLDER = "plugin_data"
#: How many names write_new tries (name, name_2, ...) before giving up.
MAX_RERUNS = 1000
#: What a plugin's data folder never holds: code (the policy gate checks
#: every .py under a project, design 4.1) or anything Windows runs.
CODE_ENDINGS = (".py", ".pyw", ".pyc", ".pyo", ".pyd", ".pyz", ".dll", ".so",
                ".dylib", ".exe", ".bat", ".cmd", ".com", ".ps1", ".vbs",
                ".js", ".lnk", ".scr", ".msi")
#: Names Windows keeps for its devices, whatever their extension.
RESERVED_NAMES = frozenset({"con", "prn", "aux", "nul"}
                           | {f"com{i}" for i in range(10)}
                           | {f"lpt{i}" for i in range(10)})
#: A plain file name: no folder, no stream (":"), not hidden, a sane length.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ +-]{0,159}\Z")
#: What an end record says that on_run_finished is told (RunInfo.counts).
COUNT_KEYS = ("frames_saved", "frames_not_saved", "grab_failures",
              "grab_attempts", "first_index", "last_index",
              "recording_failed", "stream_ended")

#: A run's states: its name reserved (begin_run), recording (on_run_started
#: was asked), stopped (Stop), over (its end record written), or abandoned
#: (a Start that failed after the name was reserved).
RESERVED = "reserved"
RECORDING = "recording"
STOPPED = "stopped"
ENDED = "ended"
ABANDONED = "abandoned"

_COUNTER = itertools.count(1)


class PluginFileError(OSError):
    """A file service refused, and why, in words: no run in progress, a
    folder or name that is not the run's, a name already there."""


# ======================================================================
# The run in progress
# ======================================================================
@dataclass(eq=False)
class ActiveRun:
    """One run, from its name's reservation to its end record, as the
    plugin host keeps it: its name, its folders by camera, when it started,
    which plugins were asked on_run_started (`hooked`, (id, camera)), the
    plugins that faulted during it, and the files they streamed into it."""
    run: str
    folders: Dict[str, Path]
    started: float = field(default_factory=time.time)
    state: str = RESERVED
    ended: Optional[float] = None
    hooked: List[Tuple[str, Optional[str]]] = field(default_factory=list)
    faults: Dict[str, str] = field(default_factory=dict)
    files: List[Any] = field(default_factory=list)
    counts: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: Its plugins were told it started ("run_started"), and that it
    #: stopped ("run_stopped"): each is told once.
    told_started: bool = False
    told_stopped: bool = False
    lock: Any = field(default_factory=threading.Lock, repr=False)

    @property
    def open(self) -> bool:
        """A streamed file may be made in it: reserved or recording."""
        return self.state in (RESERVED, RECORDING)

    @property
    def over(self) -> bool:
        return self.state in (ENDED, ABANDONED)

    def info(self) -> api.RunInfo:
        """The run as a plugin sees it (RunInfo), read-only."""
        return api.RunInfo(
            run=self.run,
            folders=MappingProxyType({k: Path(v) for k, v in
                                      self.folders.items()}),
            started=self.started, ended=self.ended,
            counts=MappingProxyType({k: MappingProxyType(dict(v))
                                     for k, v in self.counts.items()}))

    def camera_of(self, folder: Any) -> Optional[str]:
        """The camera whose run folder `folder` is, or None."""
        for camera, where in self.folders.items():
            if same_folder(where, folder):
                return camera
        return None

    def note_fault(self, pid: str, reason: str) -> None:
        """A plugin faulted while this run was not over: its end record
        says so (the first fault of each plugin)."""
        with self.lock:
            if not self.over:
                self.faults.setdefault(pid, reason)

    def keep(self, handle: Any) -> None:
        with self.lock:
            self.files.append(handle)

    def close_files(self) -> int:
        """Close every file streamed into the run that its plugin left
        open; how many were."""
        with self.lock:
            handles, self.files = list(self.files), []
        left = 0
        for handle in handles:
            try:
                if not handle.closed:
                    left += 1
                    handle.close()
            except Exception:                             # noqa: BLE001
                pass
        return left


def counts_of(doc: Any) -> Dict[str, Any]:
    """The counts of an end record a plugin is told (COUNT_KEYS)."""
    return {key: doc.get(key) for key in COUNT_KEYS
            if isinstance(doc, dict) and key in doc}


# ======================================================================
# Names and folders
# ======================================================================
def same_folder(a: Any, b: Any) -> bool:
    """Whether `a` and `b` name one folder (resolved, case as Windows has
    it)."""
    try:
        left = os.path.normcase(str(Path(str(a)).resolve()))
        right = os.path.normcase(str(Path(str(b)).resolve()))
    except (OSError, RuntimeError, ValueError):
        return False
    return left == right


def check_name(name: Any) -> str:
    """`name` when it is a plain file name — no folder in it, no stream, not
    hidden, not one of the names Windows keeps for devices, not ending in a
    dot or a space — else PluginFileError."""
    text = str(name if name is not None else "")
    stem = text.split(".", 1)[0].strip().lower()
    if (not NAME_RE.match(text) or text.endswith((".", " "))
            or stem in RESERVED_NAMES):
        raise PluginFileError(f"{text!r} is not a plain file name: letters, "
                              f"digits, '.', '_', '-', '+' and spaces, "
                              f"starting with a letter or digit, with no "
                              f"folder in it")
    return text


def numbered(name: str, n: int) -> str:
    """`name` for its n-th write: itself, then "<stem>_2<ext>", "_3" ...
    ("<run>_tracks.csv" -> "<run>_tracks_2.csv", the velocity design's
    rerun names)."""
    if n <= 1:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        return f"{name}_{n}"
    return f"{stem}_{n}.{ext}"


def data_dir(project_dir: Any, pid: str, make: bool = True) -> Path:
    """Plugin `pid`'s data folder in the project at `project_dir`:
    <project>/plugin_data/<id>/ (design 4.1), made when `make`."""
    from .plugin_manifest import ID_RE

    if not ID_RE.match(str(pid)):
        raise PluginFileError(f"{pid!r} is not a plugin id")
    where = Path(str(project_dir)) / DATA_FOLDER / str(pid)
    if make:
        where.mkdir(parents=True, exist_ok=True)
    return where


# ======================================================================
# (a) Streamed, during a run
# ======================================================================
def create_file(active: Optional[ActiveRun], folder: Any, name: Any,
                suffixes: Sequence[str], *, binary: bool = False) -> Any:
    """A new run file `name` in `folder`, made EXCLUSIVELY and opened for
    writing (text: UTF-8, newline="", line-buffered; or `binary`, which
    its plugin flushes), for the run in progress `active` (see the
    module's notes). PluginFileError when there
    is no run in progress, `folder` is not one of its folders, `name` is
    not "<run><ending>" with an ending in `suffixes`, or the file is
    already there."""
    if active is None or not active.open:
        raise PluginFileError("no run is in progress: a streamed run file "
                              "is made during a run (write_new writes a "
                              "whole one at any time)")
    text = check_name(name)
    camera = active.camera_of(folder)
    if camera is None:
        raise PluginFileError(f"{folder} is not one of the run's folders "
                              f"({', '.join(str(f) for f in active.folders.values())}"
                              f"): a run's streamed file goes beside its "
                              f"frames")
    if not any(text == f"{active.run}{suffix}" for suffix in suffixes):
        raise PluginFileError(
            f"{text!r} is not a file of the run {active.run} that this "
            f"plugin declares: name it {active.run}<ending>, with an ending "
            f"its manifest's files declare "
            f"({', '.join(suffixes) or 'it declares none'})")
    path = Path(active.folders[camera]) / text
    try:
        # A text file is LINE-BUFFERED: each row reaches the file as its
        # line ends, so a crash mid-run leaves every row written so far (P3
        # review: a sidecar held none of its five rows after a crash, all
        # of them still in the 8 KB buffer). The OS keeps them; nothing is
        # forced onto the disk itself. A binary file is buffered as usual —
        # only its plugin knows where a record ends — so a plugin flushes
        # it where a crash must not cut it (the API notes say so): an
        # unbuffered one may write part of what it is given.
        handle = (open(path, "xb") if binary else
                  open(path, "x", newline="", encoding="utf-8",
                       buffering=1))
    except FileExistsError as exc:
        raise PluginFileError(f"{text} is already there: a run's file is "
                              f"made once, never written over") from exc
    active.keep(handle)
    return handle


# ======================================================================
# (b) Whole, at any time
# ======================================================================
def publish_new(temp: Path, final: Path) -> None:
    """Put `temp` at `final` in one step, REFUSING an existing `final`
    (FileExistsError): Windows' rename refuses by itself (os.replace, which
    would not, is never used); elsewhere a hard link, which refuses, then
    the temporary name dropped."""
    if os.name == "nt":
        os.rename(temp, final)
    else:
        os.link(temp, final)
        os.remove(temp)


def write_new(folder: Any, name: Any, data: Any, *,
              suffixes: Sequence[str], own: Optional[Path] = None,
              runs: Callable[[Path], Iterable[str]] = lambda where: ()
              ) -> Path:
    """Write `data` (bytes) whole as `name` in `folder`, never over anything
    (see the module's notes); the path written — "<stem>_2<ext>" ... when
    the name is taken. `folder` is the plugin's own data folder `own` (any
    plain file name but code), or a folder holding the run `name` belongs
    to ("<run><ending>", an ending in `suffixes`): one whose frames CSV is
    there, or one of `runs(folder)` (the run in progress)."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError(f"write_new writes bytes, not "
                        f"{type(data).__name__}: encode text first")
    text = check_name(name)
    where = Path(str(folder))
    if own is not None and same_folder(where, own):
        if text.lower().endswith(CODE_ENDINGS):
            raise PluginFileError(f"{text!r} is code, and a plugin's data "
                                  f"folder holds none (the project's policy "
                                  f"check reads every .py file in it)")
    else:
        from .capture import INDEX_SUFFIX

        suffix = next((s for s in suffixes
                       if text.endswith(s) and len(text) > len(s)), None)
        if suffix is None:
            raise PluginFileError(
                f"{text!r} is not a run file this plugin declares: name it "
                f"<run><ending>, with an ending its manifest's files declare "
                f"({', '.join(suffixes) or 'it declares none'}), or write it "
                f"in its data folder (data_dir)")
        run = text[:-len(suffix)]
        if not ((where / f"{run}{INDEX_SUFFIX}").is_file()
                or run in set(runs(where))):
            raise PluginFileError(f"{where} holds no run {run!r} (no "
                                  f"{run}{INDEX_SUFFIX}): a run file goes "
                                  f"beside its run's frames")
    if not where.is_dir():
        raise PluginFileError(f"{where} is not a folder")
    temp = where / (f".{text}.{os.getpid()}.{threading.get_ident()}."
                    f"{next(_COUNTER)}.tmp")
    try:
        with open(temp, "xb") as handle:
            handle.write(bytes(data))
            handle.flush()
            os.fsync(handle.fileno())
        for n in range(1, MAX_RERUNS + 1):
            final = where / numbered(text, n)
            try:
                publish_new(temp, final)
            except FileExistsError:
                continue
            return final
        raise PluginFileError(f"{text} and {MAX_RERUNS - 1} numbered names "
                              f"after it are all taken in {where}")
    finally:
        # Ours, never the user's: the temporary name of a write that did
        # not happen (after a rename it is gone already).
        try:
            if temp.exists():
                os.remove(temp)
        except OSError:
            pass
