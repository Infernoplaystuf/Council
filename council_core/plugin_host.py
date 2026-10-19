"""
council_core.plugin_host — the Typhon plugin host: it finds plugins by their
manifests, loads one only when it is first needed, calls it behind a guard,
and switches it off when it fails.

WHERE IT SITS (docs/typhon_plugins_design.md 3, 4, 5.1, 10)
The multi-camera Rig is the plugin host (design 5.1): `rig.plugins` is one of
these, made on first use (council_core.camera_rig). It holds the registry,
the project's plugin list, each plugin's instances and contexts, the faults,
and the live-frame feeds. No Qt here; a screen asks it and draws.

STAGE P1: THE HOST, AND NO PLUGIN WIRED TO A FEATURE YET
Today's Typhon behaves exactly as before: nothing in frame_camera calls the
host yet. The camera plugins are wired at stage P2, the run hooks and the
triggering plugin at P3, the screen points at P4. Third-party plugins are
FOUND and listed now — one folder each, copied into the plugin home's
plugins folder (question 7) — but never loaded before stage P5 brings
approval and the hash-then-compile loader (design 4.5, 4.6).

STAGE P2: THE CAMERA-PLUGIN REGISTRY (design 2.4 items 3 and 9, 3.3, 3.4)
The camera plugins this project runs are scanned and opened through the
host: `backends()` gives them as today's cameras.Backend, so cameras.discover
and cameras.open_camera — the code that stays in council_core, which the
Council's Capture tab keeps calling through cameras.backends() — run on top
of the registry with the same order and the same notes; `open_camera`
confirms what the opened camera can do onto its slot (the manifest's
capabilities, within what Typhon knows); `category_tables` merges each
plugin's rows for the settings tabs; `review_sources` gives the slider's
views of a plugin's run files (the Prophesee raw view). A camera's own
refusal (CameraError) is the camera's answer, never the plugin's fault;
anything else a camera plugin raises is said and logged, and a built-in
camera plugin is never switched off without asking (`camera_call`) — not
even when it fails to load or activate: it is tried again at the next scan
(`retry_cameras`). A scan says why it has no cameras of a camera plugin
that cannot run, and of a ticked family no plugin it runs opens.

STAGE P2 ROWS 3 AND 4: THE SET-UP'S FAMILIES, AND THE RIG'S NAMES
A "Typhon (plugins)" project (one with typhon_plugins.json) scans and opens
its cameras through this registry (frame_camera); today's Typhon never
does. Its camera_setup.json — the families its set-up wizard ticked, or the
one camera a single-choice file names — is read as part of its plugin list
(`setup_choice`): a camera plugin with a set-up whose family was not ticked
is left out of the scan, and one with no set-up (the simulated cameras) is
always in it (`camera_plugins`). Each camera plugin with a set-up is a
check box in the wizard, with its guide, checks and (Basler) scan page,
guarded (`camera_families`). A camera opened into a slot takes its name in
the rig from camera_rig.json (`name_camera`).

FINDING PLUGINS READS MANIFESTS ONLY (design 4.1, 4.4)
Built-in plugins are the folders of typhon_plugins/ that BUILTIN_PLUGINS
names. A plugin is built in because it is on that list, never because of
the folder it sits in: any other folder there is refused, not loaded.
Third-party plugins are folders under <plugin home>/plugins/, each named
after its id with the dots turned into underscores. Nothing of a plugin is
imported to list it.

LAZY LOADING (design 4.4)
A plugin's code is imported when it is first needed — `instance()`, or a
call into it — as the package typhon_plugins.<name> (no sys.path change).
Its object is made with no arguments, then `activate(ctx)`.

EVERY CALL IS GUARDED, AND A FAULT HAS ONE OF THREE ENDINGS (design 3.10)
Looking a hook up is guarded with the call (getattr can run plugin code: a
property, a __getattr__). Any exception counts, SystemExit and
KeyboardInterrupt included — except a KeyboardInterrupt on the main thread
outside the recording path, which is the user's Ctrl+C wherever it landed:
it goes on up, and no plugin is blamed for it.
  * Best effort — every hook but the two below: the plugin is switched off
    for the rest of the session ("faulted"), named in the status line, its
    traceback appended to <project>/plugin_errors.log, and the caller
    carries on with a default. Such a fault never stops a capture: a gate
    of that plugin serves out the run in progress (GuardedGate).
  * The recording path — a plugin's record gate (per-frame tags): the
    plugin is faulted AND the recording ends cleanly with the reason in
    <run>_run_end.json, as a failed write does since M0 (`record_gate`,
    capture.FrameWriter.end_recording): carrying on would leave half a run
    tagged and half not. The frame at a tag fault is still saved, marked
    "error: plugin fault" (design 3.6, as today's capture marks a gate that
    raises); nothing after it is taken. A gate ends the recording of ITS
    run's writer, never a later run's.
  * check_start — before anything stops: the plugin is faulted and that
    Start is refused with the reason; the next Start runs without it.
A hang cannot be interrupted in Python: the watchdog switches off a call
that has run past WATCHDOG_SECONDS, which stops further calls into it. It
starts by itself on the host's own thread when the first plugin is loaded
or fed frames, and stops at close(). A hung plugin's gate ends its run at
the next frame: the plugin may hold what its gate needs.
M0's data-safety rules (reserved run names, exclusive frame writes, loud
failures) stay in the core; nothing here can switch them off.

LOADING HOLDS UP NO OTHER PLUGIN
A plugin is imported and activated outside the host's lock: one that is
slow (sklearn takes 3-5 s to import) or hangs there holds up only callers
of that same plugin, who wait for it — and the watchdog lets them go.

STAGE P3 ROW 1: A RUN, AS THE PLUGINS SEE IT (design 3.5, 3.6, 3.9, 3.10)
The core owns the order; the host fills it in. Start: `check_start(plan)`
(a RunPlan, before anything stops or is reserved) → the run name reserved
→ `begin_run` → recording → `run_started` (each run-hook plugin's
on_run_started, best effort; "run_started" told) → the trigger source. Stop:
`run_stopped` ("run_stopped" told) → the recording over → when its last
frame is on disk, `end_blocks` (each plugin's on_run_finished with the end
record's counts, bounded in time, its block cleaned and size-checked into
the end record's "plugins"; the run's best-effort faults into its
"plugin_faults"). `record_plugins` is the camera record's "plugins" block:
what made the run. Every plugin's declared run files are registered as run
markers (capture.register_run_files), so a run name is never used twice.
The two file services and a plugin's data folder are council_core.
plugin_runs'. A frame stage (`frame_stage`) is guarded on the recording
path, as a record gate is.

STAGE P3 ROW 4: PRESETS AND RECORDS BY NAMESPACE (design 3.9)
A preset is split by owner before camera_settings.apply (`split_preset`;
council_core.plugin_keys): "<id>/<key>" goes to its plugin when it runs and
takes presets (`apply_preset_values`), and every key of a plugin that is
not running is kept untouched — never applied, never lost on a save over it
(`preset_settings`), said as kept (`kept_words`). Records keep plugin data
under their "plugins" block, by id, only.

LIVE FRAMES ON THE PLUGIN'S OWN THREAD (design 3.8, 10.2)
`feed()` gives a plugin a LiveFeed: the grab thread drops each frame into a
two-slot queue that throws away the oldest and counts it, and the plugin's
own thread calls `on_frame` with a FrameView whose pixels it cannot unlock
by accident (an array over a read-only buffer of the frame's: nothing is
copied) — capped by default to a share of the time (DUTY_CAP). A plugin on
its own thread still shares Python's lock with the grab thread; how much
that costs inside the real Qt Typhon is measured by
tools/typhon_plugins_timing.py.

No pickle, no network, nothing deleted but the temporary folders this host
made itself.
"""
from __future__ import annotations

import collections
import importlib
import importlib.util
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import (Any, Callable, Deque, Dict, Iterable, List, Mapping,
                    Optional, Sequence, Tuple)

import typhon_plugin_api as api

from . import camera_capabilities as cc
from . import camera_record
from . import plugin_keys as pk
from . import plugin_lists as pl
from . import plugin_manifest as pm
from . import plugin_runs as pr
from .plugin_capture import needs_core_capture, plugin_device

#: THE BUILT-IN LIST: (id, folder in typhon_plugins/). Only these load as
#: built-in plugins, trusted like the rest of the Council's code (design
#: 4.1). A test checks it against the folders.
BUILTIN_PLUGINS: Tuple[Tuple[str, str], ...] = (
    ("typhon.basler", "basler"),
    ("typhon.prophesee", "prophesee"),
    ("typhon.simulated", "simulated"),
    ("typhon.trigger", "trigger"),
    ("typhon.classify", "classify"),
    ("typhon.timing", "timing"),
    ("typhon.crop", "crop"),
)
#: The package the built-ins are imported as.
BUILTIN_PACKAGE = "typhon_plugins"
BUILTIN_DIR = Path(__file__).resolve().parent.parent / BUILTIN_PACKAGE

#: A plugin's states (design 4.3), as the Plugins window will show them.
DISCOVERED = "discovered"        # manifest read; no project open
INCOMPATIBLE = "incompatible"    # bad manifest, wrong API, missing package
NEEDS_APPROVAL = "needs approval"  # third-party (approval: stage P5)
MISSING = "missing"              # a list names it; it is not on this PC
ON = "on"                        # in this project's list, not loaded yet
OFF = "off"                      # not in this project's list
ACTIVE = "active"                # its code is loaded and activated
FAULTED = "faulted"              # off until the next start

#: How the three endings of a fault are named (design 3.10).
BEST_EFFORT = "best effort"
RECORDING = "recording path"
START_CHECK = "start check"
HANG = "hang"
LOAD = "load"

#: A call into a plugin that runs longer than this is switched off.
WATCHDOG_SECONDS = 2.0
#: How long Start, Stop and the end of a run wait for one plugin's call —
#: check_start, on_run_started, a run_started or run_stopped listener, a
#: camera plugin's record_fields, on_run_finished (with the cleaning of the
#: block it gives) — before going on without it, the plugin switched off
#: as a hang (design 3.10: never held up by a plugin; design 3.11 gives
#: these 500 ms each and the watchdog switches off a call over 2 s, which
#: this is). Each runs on a thread of its own (_bounded).
RUN_HOOK_SECONDS = 2.0
#: A fault's words, at most, wherever they are kept or said — the status
#: line, Start's and Stop's lines, the records' plugin_faults and
#: `faulted`, a recording's reason. The whole text and its traceback go
#: to plugin_errors.log (P3 review: a 6 MB exception message made a 6 MB
#: end record, over the 4 MB a record may be read at).
FAULT_TEXT = 2000
#: How often the watchdog looks (seconds).
WATCHDOG_EVERY = 0.5
#: A live feed's queue: the newest frames, the oldest thrown away.
FEED_SLOTS = 2
#: The share of the time a live plugin may be busy, when capped.
DUTY_CAP = 0.25
#: Appended to in the project's folder: one entry per fault.
ERRORS_LOG = "plugin_errors.log"
#: Folders that hold compiled code a plugin may not ship (design 4.5).
COMPILED = (".pyc", ".pyo", ".pyd", ".dll", ".so", ".dylib")
#: A third-party folder is listed, not walked forever.
MAX_FILES = 2000

#: The tags a frame gets when its plugin gate failed (as capture.py's own
#: for a gate that raises).
FAULT_TAGS = {"trigger": "", "trigger_source": "error: plugin fault"}
#: What a record gate's tags may hold: the frames CSV's trigger columns
#: (council_core.triggered.frame_columns, a test holds the two equal), each
#: text, a whole or a finite number, or nothing — never one of the core's
#: own meta keys (P3 review: a gate's "reduce" changed the saved pictures'
#: size mid-run, and a str for tags killed the grab thread).
GATE_COLUMNS = ("trigger", "trigger_frame", "trigger_source", "trigger_time",
                "arrived")

#: The camera settings a FrameView's own terms are read from (design 3.12):
#: GenICam's names, which the built-in frame cameras use. A camera with no
#: such setting does not bin, decimate or mirror; one whose setting cannot
#: be read leaves the term unknown, and to_sensor then refuses.
GEOMETRY = (("binning", "BinningHorizontal", "BinningVertical"),
            ("decimation", "DecimationHorizontal", "DecimationVertical"),
            ("mirror", "ReverseX", "ReverseY"))


def fault_text(exc: BaseException) -> str:
    """`exc` as a fault's words: its type and message, at most FAULT_TEXT
    characters (the whole is in plugin_errors.log)."""
    try:
        text = f"{type(exc).__name__}: {exc}"
    except BaseException:                               # noqa: BLE001
        text = f"{type(exc).__name__}: (its message could not be read)"
    return cut(text)


def _trace(exc: BaseException) -> str:
    """`exc`'s whole traceback, for plugin_errors.log only."""
    try:
        return "".join(traceback.format_exception(type(exc), exc,
                                                  exc.__traceback__))
    except BaseException:                               # noqa: BLE001
        return f"{type(exc).__name__} (its traceback could not be read)\n"


def cut(text: str, limit: int = FAULT_TEXT) -> str:
    """`text` at most `limit` characters, saying so when it was cut."""
    text = str(text)
    if len(text) <= limit:
        return text
    tail = " … (cut: the whole text is in plugin_errors.log)"
    return text[:max(0, limit - len(tail))] + tail


def gate_tags(tags: Any) -> Optional[Dict[str, Any]]:
    """A record gate's tags for a frame, as the core puts them on it: None
    (a frame no trigger asked for), or a new dict of the frames CSV's
    trigger columns (GATE_COLUMNS) with plain values. Anything else raises
    TypeError or ValueError — a fault on the recording path (GuardedGate),
    so junk never reaches the frame's meta: a str there stopped the grab
    thread, and "reduce" changed what was saved (P3 review)."""
    if tags is None:
        return None
    if not isinstance(tags, Mapping):
        raise TypeError(f"its tags are a {type(tags).__name__}, not a mapping "
                        f"of the frames CSV's trigger columns")
    out: Dict[str, Any] = {}
    for key, value in (dict.items(tags) if isinstance(tags, dict)
                       else tags.items()):
        if not isinstance(key, str) or key not in GATE_COLUMNS:
            raise ValueError(f"its tags hold {key!r}, which is not one of "
                             f"the frames CSV's trigger columns "
                             f"({', '.join(GATE_COLUMNS)})")
        if value is None or isinstance(value, bool):
            out[key] = value
        elif isinstance(value, str):
            out[key] = str.__str__(value)
        elif isinstance(value, int):
            out[key] = int.__index__(value)
        elif isinstance(value, float) and value == value and \
                value not in (float("inf"), float("-inf")):
            out[key] = float.__float__(value)
        else:
            raise TypeError(f"its tag {key!r} is a {type(value).__name__}, "
                            f"not text or a number")
    return out

#: PYTHON'S SWITCH INTERVAL WHILE A RUN RECORDS (design 10.2; stage P3 row
#: 5), in seconds — None leaves it as it is (today's: Python's 5 ms). A live
#: plugin on its own thread shares Python's lock with the grab thread, and
#: at 5 ms one busy 20 ms a frame starved it: 344-430 of 600 frames due were
#: read in 4 s at 150 fps; at 1 ms all were (113-169 a period late, 16-23
#: with the duty cap; with no plugin 0-1 late either way). Measured inside
#: the real Qt Typhon (tools/typhon_plugins_timing.py, docs/
#: typhon_plugins_branch.md "Frame timing"). Windows' 1 ms timer is NOT
#: asked for: there the window's own Qt timers already give the process
#: one, and asking changed nothing measured.
RECORDING_SWITCH_S: Optional[float] = 0.001


class SwitchInterval:
    """Python's switch interval at RECORDING_SWITCH_S while any run holds
    it (`hold(run)` as its recording begins, `release(run)` once it is
    over), and put back as it was when the last one lets go. Process-wide,
    as the interval is: several runs (cameras) share one hold.

    Never coarser than it was: an interval already finer is left alone. A
    change someone else made meanwhile is theirs, and is left as it is
    when the hold ends."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holders: List[Any] = []
        #: The interval before the first hold, and the one set — None when
        #: nothing was changed.
        self._before: Optional[float] = None
        self._set: Optional[float] = None

    @property
    def held(self) -> bool:
        return bool(self._holders)

    def hold(self, owner: Any) -> None:
        wanted = RECORDING_SWITCH_S
        with self._lock:
            if any(h is owner for h in self._holders):
                return
            if not self._holders and wanted is not None:
                before = sys.getswitchinterval()
                if float(wanted) < before:
                    sys.setswitchinterval(float(wanted))
                    self._before, self._set = before, sys.getswitchinterval()
            self._holders.append(owner)

    def release(self, owner: Any) -> None:
        with self._lock:
            kept = [h for h in self._holders if h is not owner]
            if len(kept) == len(self._holders):
                return
            self._holders = kept
            if kept or self._set is None:
                return
            before, made = self._before, self._set
            self._before = self._set = None
            if abs(sys.getswitchinterval() - made) < 1e-9:
                sys.setswitchinterval(before)


#: The one hold of this process (the switch interval is the process's).
SWITCH = SwitchInterval()


class PluginHung(RuntimeError):
    """What the watchdog records for a call that ran too long."""


class RecordingEnded(RuntimeError):
    """A plugin's frame stage failed, or its plugin was switched off: the
    frame is not saved and the recording ends, with this reason (a failed
    write, to the recorder: design 3.10)."""


def _ours(exc: BaseException) -> bool:
    """Whether a best-effort guard takes `exc` as the plugin's fault: any
    exception (design 3.10), SystemExit and KeyboardInterrupt included —
    except a KeyboardInterrupt on the main thread, which is the user's
    Ctrl+C wherever it landed. That one goes on up, and the plugin that
    happened to be running is not blamed. (The recording path takes every
    exception, on any thread: GuardedGate.)"""
    return not (isinstance(exc, KeyboardInterrupt)
                and threading.current_thread() is threading.main_thread())


#: What `_hook` returns for a hook the plugin does not have.
_NO_HOOK = object()
#: What `_bounded` returns for a call that did not return in time.
_TIMED_OUT = object()

#: The widest whole number a preset holds (camera_presets.WIDEST_INT).
_WIDEST = 2 ** 63


def _plain_value(value: Any) -> bool:
    """A value a preset holds (camera_presets' rule): true or false, text,
    a 64-bit whole number, a finite number."""
    if isinstance(value, (bool, str)):
        return True
    if isinstance(value, int):
        return -_WIDEST <= value < _WIDEST
    return isinstance(value, float) and value == value and \
        value not in (float("inf"), float("-inf"))


def _plain_items(fn: Callable[..., Any]) -> Callable[..., Any]:
    """`fn`, its result — when a mapping — copied to a plain dict as it
    returns (in its caller's guard: a mapping's items() is its own code),
    its text keys and its text and number values made Python's own types,
    so nothing read of it afterwards runs the plugin's code."""
    def plain(value: Any) -> Any:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, str):
            return str.__str__(value)
        if isinstance(value, int):
            return int.__index__(value)
        if isinstance(value, float):
            return float.__float__(value)
        return value

    def call(*args: Any, **kwargs: Any) -> Any:
        got = fn(*args, **kwargs)
        if got is None or got is _NO_HOOK or not isinstance(got, Mapping):
            return got
        items = dict.items(got) if isinstance(got, dict) else got.items()
        return {(str.__str__(k) if isinstance(k, str) else k): plain(v)
                for k, v in items}
    return call


def _hook(obj: Any, name: str) -> Callable[..., Any]:
    """A call of `obj`'s hook `name` that LOOKS IT UP when called, so the
    lookup runs inside the caller's guard: getattr runs plugin code (a
    property, a __getattr__) and lets everything but AttributeError out.
    Returns _NO_HOOK when there is no such hook."""
    def call(*args: Any, **kwargs: Any) -> Any:
        fn = getattr(obj, name, None)
        if not callable(fn):
            return _NO_HOOK
        return fn(*args, **kwargs)
    return call


# ======================================================================
# The registry: every plugin found, by its manifest
# ======================================================================
@dataclass
class PluginRecord:
    """One plugin found on this PC. `problem` says why it can never run
    here (a bad manifest, the wrong API, ...); a third-party plugin's
    `approval` says why it waits."""
    id: str
    folder: Path
    builtin: bool
    manifest: Optional[pm.Manifest] = None
    problem: str = ""
    approval: str = ""
    #: Made in this process (tests, the timing bench): never from disk.
    factory: Optional[Callable[[], Any]] = None

    @property
    def name(self) -> str:
        return self.manifest.name if self.manifest is not None else self.id


@dataclass
class Registry:
    """What discovery found: plugins by id, and folders it will not load
    (with why), in the order found."""
    home: Path
    records: Dict[str, PluginRecord] = field(default_factory=dict)
    refused: List[Tuple[Path, str]] = field(default_factory=list)


class _Loading:
    """A plugin instance being made, outside the host's lock: the thread
    making it, the object once constructed (for a call from its own
    activate), whether it was let go meanwhile, and an Event set when the
    making is over, made or not."""

    def __init__(self) -> None:
        self.thread = threading.get_ident()
        self.made: Any = None
        self.cancelled = False
        self.done = threading.Event()


def _third_party_wait() -> str:
    return ("a plugin from someone else: it is listed, and it loads once "
            "you have approved it — approval comes with stage P5 of the "
            "plugin work; until then it does not run")


def _compiled_files(folder: Path) -> Tuple[List[str], bool]:
    """Compiled code inside `folder` (relative names, up to a few), and
    whether the folder holds more than MAX_FILES files (it is not walked
    past them)."""
    found: List[str] = []
    seen = 0
    for root, dirs, files in os.walk(folder):
        for name in list(dirs):
            if name == "__pycache__":
                found.append(str(Path(root, name).relative_to(folder)))
        for name in files:
            seen += 1
            if seen > MAX_FILES:
                return found, True
            if name.lower().endswith(COMPILED):
                found.append(str(Path(root, name).relative_to(folder)))
        if len(found) >= 5:
            break
    return found, False


def ending_clash(files: Iterable[str], taken: Mapping[str, str]) -> str:
    """Why run-file endings `files` cannot be a plugin's beside `taken`
    (ending -> the plugin it is), or "": the same ending — one run file,
    one plugin — or one HOLDING another anywhere. capture.taken_runs and a
    run's readers split a file name at the first ending it holds, so with
    "_ab_x.csv" taken, "<run>_zz_ab_x.csv" would read as the file of a run
    "<run>_zz" (P3 review; the manifest refuses the same of the core's and
    the built-ins' endings, plugin_manifest.check)."""
    for mine in files:
        for other, owner in taken.items():
            if mine == other:
                return f"its run file {mine!r} is {owner}'s already"
            if other in mine:
                return (f"its run file {mine!r} holds {owner}'s run-file "
                        f"ending {other!r}, so its files would be read as "
                        f"{owner}'s, of another run")
            if mine in other:
                return (f"its run file {mine!r} is held in {owner}'s ending "
                        f"{other!r}, so {owner}'s files would be read as its "
                        f"own, of another run")
    return ""


def discover(home: Any, *, builtin_dir: Any = None,
             builtins: Sequence[Tuple[str, str]] = BUILTIN_PLUGINS,
             api_version: str = pm.API_VERSION) -> Registry:
    """Read every manifest — the built-ins named in `builtins`, inside
    `builtin_dir`, and the third-party folders under <home>/plugins/ —
    importing nothing. Never raises for a plugin: each is a record, with
    its problem if it has one; folders that are no plugin go to `refused`."""
    home = Path(home)
    builtin_dir = Path(builtin_dir) if builtin_dir is not None else BUILTIN_DIR
    reg = Registry(home=home)
    listed = {name for _pid, name in builtins}
    shorts: Dict[str, str] = {}
    suffixes: Dict[str, str] = {}

    for pid, name in builtins:
        folder = builtin_dir / name
        record = PluginRecord(id=pid, folder=folder, builtin=True)
        try:
            m = pm.read(folder, builtin=True)
        except pm.ManifestError as exc:
            record.problem = f"its manifest: {exc}"
        else:
            record.manifest = m
            if m.id != pid:
                record.problem = (f"its manifest names {m.id!r}; the "
                                  f"built-in list has {pid!r} in {name}/")
            else:
                record.problem = pm.api_problem(m.api, api_version)
            if not record.problem:
                # One run file, one plugin — built-ins too (stage P3: the
                # registry marks each one a run's; before, only a third
                # party's claim was checked) — and none holding another's.
                record.problem = ending_clash(m.files, suffixes)
            if not record.problem:
                shorts.setdefault(m.short, pid)
                for suffix in m.files:
                    suffixes.setdefault(suffix, pid)
        reg.records[pid] = record

    if builtin_dir.is_dir():
        for entry in sorted(builtin_dir.iterdir(), key=lambda p: p.name):
            if (entry.is_dir() and entry.name not in listed
                    and entry.name != "__pycache__"):
                reg.refused.append((entry, (
                    "it sits among the built-in plugins but is not on the "
                    "built-in list, so it is not loaded: a plugin is built "
                    "in only by being on that list. A plugin you were given "
                    "goes in the plugins folder of the plugin home "
                    f"({pl.plugins_folder(home)})")))

    third = pl.plugins_folder(home)
    entries = []
    if third.is_dir():
        try:
            entries = sorted(third.iterdir(), key=lambda p: p.name)
        except OSError:
            entries = []
    for entry in entries:
        if not entry.is_dir():
            continue
        if entry.name.startswith((".", "_")):
            reg.refused.append((entry, "a hidden or private folder is never "
                                       "a plugin; it is not loaded"))
            continue
        try:
            m = pm.read(entry, builtin=False)
        except pm.ManifestError as exc:
            reg.refused.append((entry, f"incompatible — its manifest: {exc}"))
            continue
        record = PluginRecord(id=m.id, folder=entry, builtin=False,
                              manifest=m, approval=_third_party_wait())
        want = m.id.replace(".", "_")
        problem = ""
        if entry.name != want:
            problem = (f"its folder is {entry.name!r}; a plugin with the id "
                       f"{m.id!r} lives in a folder named {want!r}")
        elif m.id in reg.records:
            problem = f"another plugin already has the id {m.id!r}"
        elif m.short in shorts:
            problem = (f"its short name {m.short!r} is {shorts[m.short]}'s "
                       f"already")
        else:
            problem = ending_clash(m.files, suffixes)
        if not problem:
            problem = pm.api_problem(m.api, api_version)
        if not problem:
            compiled, too_many = _compiled_files(entry)
            if compiled:
                problem = (f"it holds compiled code ({', '.join(compiled[:3])}"
                           f"); a plugin is shipped as source, so the bytes "
                           f"you approve are the bytes that run")
            elif too_many:
                problem = (f"it holds more than {MAX_FILES} files; a "
                           f"plugin's folder is checked file by file before "
                           f"it may run, so it holds at most {MAX_FILES}")
        record.problem = problem
        if m.id in reg.records:
            reg.refused.append((entry, problem))
            continue
        if not problem:
            shorts[m.short] = m.id
            for suffix in m.files:
                suffixes[suffix] = m.id
        reg.records[m.id] = record
    return reg


# ======================================================================
# Python packages a plugin needs (checked, never installed: question 8)
# ======================================================================
def python_problem(requirements: Iterable[str]) -> str:
    """What is missing from THIS interpreter for `requirements` (import
    names with optional version bounds), or "". The module is FOUND, never
    imported; a version is read from the package that provides it."""
    missing: List[str] = []
    for text in requirements:
        try:
            name, specs = pm.requirement_parts(text)
        except ValueError:
            missing.append(f"{text} (not a requirement)")
            continue
        try:
            found = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            found = None
        if found is None:
            missing.append(f"{text} (install {_install_hint(name)})")
            continue
        if specs:
            version = _version_of(name)
            if version and not pm.version_ok(version, specs):
                missing.append(f"{text} (this Python has {version})")
    if not missing:
        return ""
    return ("needs Python package" + ("s " if len(missing) > 1 else " ")
            + ", ".join(missing) + " in the project's interpreter — install "
            "it there yourself; Typhon never installs anything")


def _install_hint(name: str) -> str:
    try:
        import python_envs

        return python_envs.install_hint(name)
    except Exception:                                     # noqa: BLE001
        return name


def _version_of(module: str) -> str:
    """The version of the distribution that provides `module`, or ""."""
    from importlib import metadata

    try:
        dists = metadata.packages_distributions().get(module) or [module]
    except Exception:                                     # noqa: BLE001
        dists = [module]
    for dist in dists:
        try:
            return metadata.version(dist)
        except Exception:                                 # noqa: BLE001
            continue
    return ""


# ======================================================================
# Faults
# ======================================================================
@dataclass(frozen=True)
class Fault:
    """Why a plugin was switched off: which hook, which of the endings of
    design 3.10, the error, its traceback, and when."""
    plugin: str
    hook: str
    kind: str
    error: str
    trace: str
    at: str

    @property
    def reason(self) -> str:
        return f"{self.error} (in {self.hook}; {self.kind})"


def backend_problem(name: str, builtin: bool = False) -> str:
    """Why `name` cannot be a camera plugin's backend name, or "": it is the
    camera family camera_setup.json saves (camera_setup.FAMILY_RE: lower
    case, at most 64 characters, no "|", which preset keys are split at),
    and a plugin from someone else's carries a dot (design 3.3)."""
    from .camera_setup import FAMILY_RE

    if not FAMILY_RE.match(name):
        return (f"its backend name {name!r} cannot name a camera family: "
                f"use at most 64 characters, a-z, 0-9, '.', '_' and '-', "
                f"starting with a letter or digit")
    if not builtin and "." not in name:
        return (f"its backend name {name!r} has no dot: a plugin from "
                f"someone else names its cameras' family with one "
                f"('acme.thermal'), so it can never take a built-in's")
    return ""


def capabilities_of(device: Any, info: Any) -> frozenset:
    """What an open camera can do, from what today's Device says of itself
    (design 3.4; council_core.camera_capabilities.of_device, the one rule).
    The trigger and binning capabilities are confirmed by the camera's
    plugin when it opens the camera (stage P2), their facets come at P3."""
    return cc.of_device(device, info)


# ======================================================================
# The host
# ======================================================================
class PluginHost:
    """The rig's plugin host. Cheap to make: it reads nothing until asked."""

    def __init__(self, rig: Any = None, *, builtin_dir: Any = None,
                 builtins: Sequence[Tuple[str, str]] = BUILTIN_PLUGINS,
                 package: str = BUILTIN_PACKAGE,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.rig = rig
        self.builtin_dir = (Path(builtin_dir) if builtin_dir is not None
                            else BUILTIN_DIR)
        self.builtins = tuple(builtins)
        self.package = package
        self._clock = clock
        self._lock = threading.RLock()
        self._fault_lock = threading.RLock()
        self.project_dir: Optional[Path] = None
        self.project: Optional[pl.ProjectList] = None
        #: Why the project's list could not be read, or "".
        self.project_problem = ""
        self.home: pl.Home = pl.resolve_home()
        #: Things the user should hear about the lists and the home.
        self.notes: List[str] = []
        self._registry: Optional[Registry] = None
        self._base: Optional[pl.BaseSet] = None
        self._adopted: Dict[str, PluginRecord] = {}
        self._python: Dict[str, str] = {}
        self._instances: Dict[Tuple[str, Optional[int]], Any] = {}
        self._contexts: Dict[Tuple[str, Optional[int]], "Context"] = {}
        #: Instances being made right now (outside the lock), by key.
        self._loading: Dict[Tuple[str, Optional[int]], "_Loading"] = {}
        #: Plugins switched off for this session, with why (design 3.10).
        self.faults: Dict[str, Fault] = {}
        #: Built-in camera plugins whose last load or activate failed, with
        #: why. Never switched off (design 3.10: not without asking): tried
        #: again at the next scan (retry_cameras).
        self._load_failures: Dict[str, Fault] = {}
        #: Each camera plugin's backend name, once read (the scan's notes).
        self._backend_names: Dict[str, str] = {}
        self._subs: Dict[str, List[Tuple[str, Callable[..., Any]]]] = {
            e: [] for e in api.EVENTS}
        self._ui: Deque[Tuple[Optional[str], Callable[[], Any]]] = \
            collections.deque()
        self._busy: Dict[Tuple[str, int], Tuple[str, float]] = {}
        self._feeds: List["LiveFeed"] = []
        self._temp: Dict[str, Path] = {}
        self._watchdog: Optional[threading.Thread] = None
        self._closing = threading.Event()
        #: The last few things said, newest last (for tests and the window).
        self.said: Deque[str] = collections.deque(maxlen=50)
        #: Set by the window: put a line in the status line (UI thread);
        #: run a function on the UI thread; the slider's current frame; the
        #: crop box. None until wired (stage P4).
        self.say_fn: Optional[Callable[[str], Any]] = None
        self.ui_caller: Optional[Callable[[Callable[[], None]], Any]] = None
        self.current_frame_fn: Optional[Callable[[], Optional[Path]]] = None
        self.crop_box_fn: Optional[Callable[[Optional[str]],
                                            Optional[api.CropBox]]] = None
        self.ui_thread = threading.main_thread().ident
        #: Runs from their name's reservation to their end record (stage
        #: P3; council_core.plugin_runs.ActiveRun), and each camera's own.
        self._runs: List[pr.ActiveRun] = []
        self._current: Dict[str, pr.ActiveRun] = {}
        #: Whose run files this host registered as run markers
        #: (capture.register_run_files), forgotten at close().
        self._token = object()
        self._registered: set = set()
        #: Each camera's triggering slot while its run lasts (stage P3 row
        #: 2): from trigger_slot until its run's camera is put back.
        self._trigger_slots: Dict[str, "TriggerSlot"] = {}

    # -- the project and its lists ------------------------------------
    def builtin_ids(self) -> List[str]:
        return [pid for pid, _name in self.builtins]

    def open_project(self, project_dir: Any, *, create: bool = False
                     ) -> List[str]:
        """Use `project_dir`'s plugin list (typhon_plugins.json) and the
        plugin home it records. `create`: a project with no list gets one
        now, copying the base set as it is (question 1). Returns the notes
        (a recorded home missing, a damaged list); never raises for them.
        Plugins loaded for another project are deactivated first, and one
        still being made is let go when it is: the list decides what runs.
        Faults stand for the session.

        A DAMAGED LIST runs only the built-in camera plugins until it is
        mended: never the base set as it is now, which would run plugins
        this project switched off and plugins added to the base set after
        it was made (question 1)."""
        if self._instances or self._loading:
            self._deactivate(lambda key: True)
        with self._lock:
            self.project_dir = Path(project_dir)
            self.project = None
            self.project_problem = ""
            self._load_failures.clear()
            self.notes = []
            self._registry = None
            self._base = None
            recorded = None
            try:
                self.project = pl.read_project(self.project_dir)
            except pl.PluginListError as exc:
                self.project_problem = str(exc)
                self.notes.append(f"{exc} — until it is mended this window "
                                  f"runs only the built-in camera plugins, "
                                  f"and changes nothing in that file")
            if self.project is not None:
                recorded = self.project.plugin_home or None
            self.home = pl.resolve_home(recorded)
            if self.home.note:
                self.notes.append(self.home.note)
            if self.project is None and create and not self.project_problem:
                try:
                    self.project = pl.make_project(
                        self.project_dir, self.base_set(), self.home.path)
                except pl.PluginListError as exc:
                    self.notes.append(f"{exc} — this project has no plugin "
                                      f"list, so it runs the base set")
            return list(self.notes)

    def base_set(self) -> pl.BaseSet:
        """The app-wide base set of this host's plugin home (read once; a
        change re-reads it). A damaged file is a note and the built-ins."""
        with self._lock:
            if self._base is None:
                try:
                    self._base = pl.read_base_set(self.home.path,
                                                  self.builtin_ids())
                except pl.PluginListError as exc:
                    self.notes.append(f"{exc} — every built-in plugin is "
                                      f"used meanwhile")
                    self._base = pl.BaseSet(self.builtin_ids(), {}, None,
                                            written=False)
            return self._base

    def runs(self) -> List[str]:
        """The plugin ids this project runs (design 4.2, question 1): its
        copy of the base set, plus its additions, minus its switch-offs —
        with no project list, the base set itself — and with a damaged one,
        only the built-in camera plugins (open_project)."""
        if self.project is not None:
            out = self.project.runs()
        elif self.project_problem:
            out = self.builtin_cameras()
        else:
            out = list(self.base_set().plugins)
        return out + [pid for pid in self._adopted if pid not in out]

    def builtin_cameras(self) -> List[str]:
        """The built-in camera plugins, in the built-in list's order: what a
        project whose list is damaged runs, so its cameras still open."""
        records = self.registry.records
        return [pid for pid in self.builtin_ids()
                if pid in records and records[pid].manifest is not None
                and records[pid].manifest.kind == "camera"]

    @property
    def registry(self) -> Registry:
        # Once made, read without the host's lock: the watchdog names a
        # plugin whose hung activate holds that lock (instance()).
        found = self._registry
        if found is not None:
            return found
        with self._lock:
            made = self._registry is None
            if made:
                self._registry = discover(self.home.path,
                                          builtin_dir=self.builtin_dir,
                                          builtins=self.builtins)
            found = self._registry
        if made:
            self._register_files(only_if_loaded=True)
        return found

    # -- the run-file suffix registry (stage P3; design 2.4 item 7, 3.9) --
    def _register_files(self, only_if_loaded: bool = False) -> None:
        """Every plugin's declared run files — each found plugin's, on or
        off, approved or not, and each adopted one's — as run markers
        (capture.register_run_files), so a run whose only file left is a
        plugin's keeps its name. `only_if_loaded`: only when the capture
        code is loaded already — the host itself loads no camera code — and
        register_run_files does it before a run's name is reserved."""
        if only_if_loaded and "council_core.capture" not in sys.modules:
            return
        from . import capture

        registry = self._registry
        records = dict(registry.records) if registry is not None else {}
        records.update(self._adopted)
        for pid, rec in records.items():
            m = rec.manifest
            if m is None or not m.files:
                continue
            owner = (self._token, pid)
            try:
                capture.register_run_files(owner, m.files)
            except ValueError:
                continue
            self._registered.add(owner)

    def register_run_files(self) -> None:
        """Register every plugin's run files now (see _register_files):
        before a run's name is reserved."""
        self.registry               # found first
        self._register_files()

    def _unregister_files(self) -> None:
        if not self._registered:
            return
        from . import capture

        for owner in list(self._registered):
            capture.unregister_run_files(owner)
        self._registered.clear()

    def record(self, pid: str) -> Optional[PluginRecord]:
        return self._adopted.get(pid) or self.registry.records.get(pid)

    def manifest(self, pid: str) -> Optional[pm.Manifest]:
        rec = self.record(pid)
        return rec.manifest if rec is not None else None

    # -- states ----------------------------------------------------------
    def state(self, pid: str) -> Tuple[str, str]:
        """(state, reason) of plugin `pid` in this window (design 4.3)."""
        rec = self.record(pid)
        if rec is None:
            return MISSING, "it is not on this PC"
        if rec.problem:
            return INCOMPATIBLE, rec.problem
        if rec.approval:
            return NEEDS_APPROVAL, rec.approval
        if pid in self.faults:
            return FAULTED, self.faults[pid].reason
        failed = self._load_failures.get(pid)
        if failed is not None:
            return FAULTED, (f"{failed.error} (in {failed.hook}; a built-in "
                             f"camera plugin stays on: it is tried again at "
                             f"the next scan)")
        problem = self._python_problem(rec)
        if problem:
            return INCOMPATIBLE, problem
        if rec.factory is None and self.project is None \
                and not self.project_problem and pid not in self.runs():
            return DISCOVERED, "its manifest was read"
        if pid not in self.runs():
            if self.project_problem:
                return OFF, ("this project's plugin list is damaged: until "
                             "it is mended only the built-in camera plugins "
                             "run")
            return OFF, "this project does not run it"
        if any(key[0] == pid for key in list(self._instances)):
            return ACTIVE, "loaded"
        return ON, "loads when it is first used"

    def states(self) -> List[Tuple[str, str, str]]:
        """(id, state, reason) of every plugin found or named by a list,
        then the folders refused (by folder name)."""
        ids = list(self.registry.records) + [
            p for p in list(self._adopted) + self.runs()
            if p not in self.registry.records]
        out = [(pid, *self.state(pid)) for pid in dict.fromkeys(ids)]
        for folder, why in self.registry.refused:
            out.append((folder.name, INCOMPATIBLE, why))
        return out

    def _python_problem(self, rec: PluginRecord) -> str:
        m = rec.manifest
        if m is None or not m.python:
            return ""
        if rec.id not in self._python:
            self._python[rec.id] = python_problem(m.python)
        return self._python[rec.id]

    # -- the slots ---------------------------------------------------------
    def _slot(self, camera: Optional[str] = None) -> Any:
        rig = self.rig
        if rig is None:
            return None
        if camera is None:
            return getattr(rig, "active", None)
        try:
            return rig.slot(camera)
        except (KeyError, AttributeError):
            return None

    def _key(self, m: pm.Manifest, camera: Optional[str]) -> Tuple[str, Any]:
        if m.kind == "feature" and m.scope == "camera":
            slot = self._slot(camera)
            return (m.id, getattr(slot, "id", None))
        return (m.id, None)

    def camera_handles(self) -> Mapping[str, api.CameraHandle]:
        """Every slot of the rig as a CameraHandle, by name."""
        rig = self.rig
        out: Dict[str, api.CameraHandle] = {}
        for slot in (list(rig) if rig is not None else []):
            info = getattr(slot, "info", None)
            device = getattr(slot, "device", None)
            # The slot's answer: what its camera plugin confirmed, else what
            # the camera says of itself (camera_rig.CameraSlot.capabilities).
            caps = getattr(slot, "capabilities", None)
            out[slot.name] = api.CameraHandle(
                name=slot.name, info=info if device is not None else None,
                kind=str(getattr(info, "kind", "") or "") if device is not None
                else "",
                capabilities=(frozenset(caps) if caps is not None
                              else capabilities_of(device, info)),
                lookup=lambda name, camera=slot.name: self.camera_facet(
                    camera, name))
        return MappingProxyType(out)

    def camera_facet(self, camera: Optional[str], name: str) -> Any:
        """The facet `name` of the camera open in slot `camera` (the active
        one): what its camera plugin's facet(device, name) gives (design
        3.4), asked now — or None for no camera, a camera no plugin of this
        project opened, a name it has no facet for, or a plugin that failed
        to give one (logged and said; a built-in camera plugin stays on,
        design 3.10)."""
        from .cameras import CameraError

        slot = self._slot(camera)
        device = getattr(slot, "device", None)
        info = getattr(slot, "info", None)
        if device is None or info is None or str(name) not in api.FACETS:
            return None
        pid = self.camera_plugin_for(str(getattr(info, "backend", "") or ""))
        if pid is None:
            return None
        try:
            return self.camera_call(pid, "facet", plugin_device(device),
                                    str(name))
        except CameraError:
            return None

    def stream_of(self, camera: Optional[str]) -> Any:
        """The grab thread of `camera`'s stream now (a new one at each
        start), or None: what a live feed reads the camera's own terms
        again at (LiveFeed.geometry)."""
        session = getattr(self._slot(camera), "session", None)
        return getattr(session, "_thread", None)

    def camera_geometry(self, camera: Optional[str]) -> Dict[str, Any]:
        """The camera's own binning, decimation and mirroring now (design
        3.12), from its settings (GEOMETRY: GenICam's names) — each a pair,
        or None when it cannot be told (the FrameView then refuses
        to_sensor rather than guess). A camera that has no such setting
        does not do it: (1, 1), or (False, False) (P3 review: they were
        never filled in, so a mirrored picture's to_sensor gave a wrong
        place instead of refusing)."""
        out: Dict[str, Any] = {name: None for name, _x, _y in GEOMETRY}
        device = getattr(self._slot(camera), "device", None)
        if device is None:
            return out
        try:
            from .camera_settings import SettingError

            provider = device.settings_provider()
        except Exception:                                 # noqa: BLE001
            return out
        for name, across, down in GEOMETRY:
            values: List[Any] = []
            for key in (across, down):
                try:
                    values.append(provider.read(key))
                except SettingError:
                    values.append(False if name == "mirror" else 1)
                except Exception:                         # noqa: BLE001
                    values.append(None)
            out[name] = geometry_pair(name, values)
        return out

    # -- loading -------------------------------------------------------------
    def adopt(self, manifest: pm.Manifest, factory: Callable[[], Any]) -> None:
        """Register a plugin MADE IN THIS PROCESS (tests, the timing bench):
        `factory()` makes it. Nothing is read from disk, so nothing here
        needs approval — whoever calls this is already running code."""
        with self._lock:
            self._adopted[manifest.id] = PluginRecord(
                id=manifest.id, folder=Path("."), builtin=manifest.builtin,
                manifest=manifest, factory=factory)
        self._register_files(only_if_loaded=True)

    def instance(self, pid: str, camera: Optional[str] = None) -> Any:
        """Plugin `pid`'s instance (for a camera-scoped feature, the one of
        `camera`, else of the active camera), loaded and activated now if it
        was not yet — or None when it cannot run (the state says why).

        The import and `activate` run OUTSIDE the host's lock, so a plugin
        that is slow or hangs there holds up no other plugin. A second
        caller of the same plugin meanwhile waits for it to be made — or
        gets None as soon as the plugin is switched off (the watchdog), so
        a hung load never holds it for good. A call from inside the
        plugin's own activate gets the object being made."""
        while True:
            with self._lock:
                state, _why = self.state(pid)
                if state not in (ON, ACTIVE):
                    return None
                rec = self.record(pid)
                m = rec.manifest
                if camera is not None and self._slot(camera) is None:
                    return None                 # no camera of that name
                key = self._key(m, camera)
                if key in self._instances:
                    return self._instances[key]
                loading = self._loading.get(key)
                if loading is None:
                    loading = _Loading()
                    self._loading[key] = loading
                    break                       # this thread makes it
            if loading.thread == threading.get_ident():
                return loading.made             # its own activate asking
            while not loading.done.wait(0.05):
                if pid in self.faults:
                    return None
        try:
            return self._make(pid, rec, key, loading)
        finally:
            with self._lock:
                if self._loading.get(key) is loading:
                    del self._loading[key]
            loading.done.set()

    def _make(self, pid: str, rec: PluginRecord, key: Tuple[str, Any],
              loading: "_Loading") -> Any:
        """Import, make and activate plugin `pid` for `key`, outside the
        host's lock; register it unless it was switched off meanwhile or
        let go (another project opened, switched off): then it is let go
        here, and None is returned."""
        self.start_watchdog()
        made = self._load(rec)
        if made is None or pid in self.faults or pid in self._load_failures:
            return None
        loading.made = made
        ctx = Context(self, pid, key[1])
        self._guard(pid, "activate", _hook(made, "activate"), (ctx,), {},
                    None)
        with self._lock:
            keep = pid not in self.faults and pid not in self._load_failures \
                and not loading.cancelled
            if keep:
                self._contexts[key] = ctx
                self._instances[key] = made
            else:
                ctx._gone = True
        if keep:
            return made
        if pid not in self.faults and pid not in self._load_failures:
            self._let_go(pid, made)
        return None

    def _load(self, rec: PluginRecord) -> Any:
        """Import the plugin's entry and make its object (guarded: a plugin
        that fails to load is faulted)."""
        def make() -> Any:
            if rec.factory is not None:
                return rec.factory()
            if not rec.builtin:
                raise RuntimeError("third-party plugins load from stage P5")
            folder = dict((pid, name) for pid, name in self.builtins)[rec.id]
            module = importlib.import_module(
                f"{self.package}.{folder}.{rec.manifest.module}")
            factory = getattr(module, rec.manifest.attribute)
            return factory()
        return self._guard(rec.id, "loading", make, (), {}, None, kind=LOAD)

    def loaded(self) -> List[str]:
        """The plugins whose code has been loaded, in order."""
        return list(dict.fromkeys(pid for pid, _slot in self._instances))

    def call(self, pid: str, hook: str, *args: Any,
             camera: Optional[str] = None, default: Any = None,
             **kwargs: Any) -> Any:
        """Call `hook` of plugin `pid` (loading it first if need be),
        guarded as best effort: `default` when it has no such hook, cannot
        run, or fails — and a failure switches it off."""
        made = self.instance(pid, camera)
        if made is None:
            return default
        out = self._guard(pid, hook, _hook(made, hook), args, kwargs,
                          default)
        return default if out is _NO_HOOK else out

    def with_hook(self, contribution: str) -> List[str]:
        """The plugins this project runs whose manifest lists
        `contribution` — so a hook is asked of those only, and nothing else
        is imported for it."""
        out = []
        for pid in self.runs():
            m = self.manifest(pid)
            if m is not None and contribution in m.contributes:
                out.append(pid)
        return out

    def check_start(self, plan: Any) -> List[str]:
        """Every run-hook plugin's check_start(plan): the reasons Start is
        refused (design 3.6, 3.10). A plugin whose check FAILS refuses this
        Start with its fault and is switched off; the next Start runs
        without it. BOUNDED (_bounded): one that has not answered in
        RUN_HOOK_SECONDS is switched off as a hang and refuses this Start
        the same way — Start's thread (the window's) is never held longer
        (P3 review: a check that slept 6 s held Start 6 s)."""
        reasons: List[str] = []
        for pid in self.with_hook("run_hooks"):
            made = self.instance(pid)
            if made is None:
                continue
            # Its reason made plain text inside the guard: str() runs the
            # plugin's own code for anything but a str.
            said = self._bounded(pid, "check_start",
                                 _hook(made, "check_start"), (plan,),
                                 after="that Start was refused, and the next "
                                       "runs without it", kind=START_CHECK,
                                 then=lambda out: cut(str(out)) if out
                                 else "")
            if said is _NO_HOOK:
                continue
            fault = self.faults.get(pid)
            if fault is not None and fault.hook == "check_start":
                if fault.kind == HANG:
                    reasons.append(f"{self._name(pid)} did not answer its "
                                   f"start check within "
                                   f"{RUN_HOOK_SECONDS:g} s and was switched "
                                   f"off")
                else:
                    reasons.append(f"{self._name(pid)} failed its start "
                                   f"check and was switched off: "
                                   f"{fault.error}")
            elif said and said is not _TIMED_OUT:
                reasons.append(f"{self._name(pid)}: {said}")
        return reasons

    def _name(self, pid: str) -> str:
        rec = self.record(pid)
        return rec.name if rec is not None else pid

    # -- a run (stage P3 row 1; design 3.5, 3.6, 3.9, 3.10) -----------------
    def plan_run(self, base: str,
                 cameras: Mapping[str, Mapping[str, Any]]) -> api.RunPlan:
        """The RunPlan check_start is asked with: `base`, the run's name to
        be, and each camera of it from plain data — kind, info,
        capabilities, folder, area, downsample, downsample_method, capture
        (the capture set-up as plain values) — read-only."""
        made: Dict[str, api.RunCamera] = {}
        for name, about in dict(cameras or {}).items():
            about = dict(about or {})
            folder = about.get("folder")
            area = tuple(int(v) for v in (about.get("area") or (0, 0, 0, 0)))
            made[str(name)] = api.RunCamera(
                name=str(name), kind=str(about.get("kind") or ""),
                info=about.get("info"),
                capabilities=frozenset(about.get("capabilities") or ()),
                folder=Path(str(folder)) if folder is not None else None,
                area=area if len(area) == 4 else (0, 0, 0, 0),
                downsample=int(about.get("downsample") or 1),
                downsample_method=str(about.get("downsample_method") or ""),
                capture=MappingProxyType(dict(about.get("capture") or {})))
        return api.RunPlan(base=str(base), cameras=MappingProxyType(made))

    def begin_run(self, run: str, folders: Mapping[str, Any]
                  ) -> pr.ActiveRun:
        """A run whose name was just reserved (capture.claim_run), with its
        folders by camera: the run in progress for those cameras from now
        until its end record. Its plugins hear of it at run_started."""
        self.register_run_files()
        active = pr.ActiveRun(run=str(run), folders={
            str(k): Path(str(v)) for k, v in dict(folders).items()})
        with self._lock:
            self._runs = [r for r in self._runs if not r.over]
            self._runs.append(active)
            for camera in active.folders:
                self._current[camera] = active
        return active

    def current_run(self, camera: Optional[str] = None
                    ) -> Optional[pr.ActiveRun]:
        """`camera`'s run (the active camera's) from its name's reservation
        until Stop (run_stopped) — its recording may have ended by itself
        meanwhile — or None."""
        if camera is None:
            camera = getattr(self._slot(None), "name", None)
        return self._current.get(str(camera)) if camera else None

    def _forget(self, active: pr.ActiveRun, current: bool = False) -> None:
        """Drop runs that are over from the runs kept, and with `current`
        `active` as its cameras' run too (Stop, or a Start abandoned)."""
        with self._lock:
            if current:
                for camera, run in list(self._current.items()):
                    if run is active:
                        del self._current[camera]
            self._runs = [r for r in self._runs if not r.over]

    def _run_plugins(self, active: pr.ActiveRun
                     ) -> List[Tuple[str, Optional[str]]]:
        """(plugin, camera) to ask a run's hooks: every run-hook plugin this
        project runs — a camera-scoped one once for each camera of the run,
        a rig one once (camera None)."""
        out: List[Tuple[str, Optional[str]]] = []
        for pid in self.with_hook("run_hooks"):
            m = self.manifest(pid)
            if m is not None and m.kind == "feature" and m.scope == "camera":
                out += [(pid, camera) for camera in active.folders]
            else:
                out.append((pid, None))
        return out

    def run_started(self, active: pr.ActiveRun) -> List[str]:
        """The run's recording has begun: each run-hook plugin's
        on_run_started(RunInfo), best effort (design 3.10: a fault is said
        — the words returned, for Start's summary — the plugin is switched
        off, and the capture carries on); then "run_started" is told. A
        run whose recording already ended by itself (its end record is
        written: a failed write) is not started: its plugins are told
        nothing, as for a Start that failed.

        BOUNDED (_bounded): each hook and each listener on a thread of its
        own, waited for RUN_HOOK_SECONDS at most — one that has not
        returned is switched off as a hang and Start goes on — so Start's
        thread (the window's) is never held by a plugin, nor the trigger
        source, which starts after this, kept waiting (P3 review: a hook
        that slept 6 s held Start 6 s while the run recorded)."""
        with active.lock:
            if active.state != pr.RESERVED:
                return []
            active.state = pr.RECORDING
            active.told_started = True
        # While it records: Python's finer switch interval (stage P3 row 5),
        # until Stop, or its end record when the recording ends by itself.
        SWITCH.hold(active)
        info = active.info()
        notes: List[str] = []
        for pid, camera in self._run_plugins(active):
            made = self.instance(pid, camera)
            if made is None:
                continue
            active.hooked.append((pid, camera))
            self._bounded(pid, "on_run_started",
                          _hook(made, "on_run_started"), (info,),
                          after="Start went on without it")
            fault = self.faults.get(pid)
            if fault is not None and fault.hook == "on_run_started":
                if fault.kind == HANG:
                    notes.append(f"{self._name(pid)} did not return from "
                                 f"on_run_started within "
                                 f"{RUN_HOOK_SECONDS:g} s and was switched "
                                 f"off; the capture carries on")
                else:
                    notes.append(f"{self._name(pid)} failed as the run "
                                 f"started and was switched off — "
                                 f"{fault.error}; the capture carries on")
        notes += self._tell("run_started", run=active.run, folders={
            k: str(v) for k, v in active.folders.items()})
        return notes

    def run_stopped(self, active: Optional[pr.ActiveRun] = None,
                    camera: Optional[str] = None) -> List[str]:
        """Stop was pressed (or the camera disconnected), before the
        recording is ended: no streamed file is made in the run from now
        on; "run_stopped" is told, once, to a run that was started — also
        when its recording had already ended by itself. Its plugins'
        on_run_finished comes with its end record (end_blocks), after.
        Each listener is BOUNDED as in run_started (_tell); frame_camera's
        Stop tells it once a frame camera's stream has stopped, so a
        listener that hangs adds no frame to the run (P3 review: one that
        slept 6 s held Stop 6 s, and the run recorded on meanwhile). The
        words for a listener switched off."""
        if active is None:
            active = self.current_run(camera)
        if active is None:
            return []
        SWITCH.release(active)
        with active.lock:
            started = active.told_started
            if active.state in (pr.RESERVED, pr.RECORDING):
                active.state = pr.STOPPED
            told, active.told_stopped = active.told_stopped, True
            ended = active.state == pr.ENDED
        self._forget(active, current=True)
        notes: List[str] = []
        if started and not told:
            notes = self._tell("run_stopped", run=active.run, folders={
                k: str(v) for k, v in active.folders.items()})
        if ended:
            # Its end record came first (the recording ended by itself):
            # the files its plugins left open are closed at Stop — once
            # the triggering plugin's books, which write a row for every
            # trigger until Stop as today's do, have closed theirs.
            slots = [s for s in self._trigger_slots.values()
                     if s.camera in active.folders and not s.over]
            if slots:
                slots[0].closes.append(active)
            else:
                active.close_files()
        return notes

    def _tell(self, event: str, **payload: Any) -> List[str]:
        """"run_started" or "run_stopped" told to every listener, as emit
        does — but each BOUNDED (_bounded): one that has not returned in
        RUN_HOOK_SECONDS is switched off as a hang and the rest are told.
        The words for each one switched off that way."""
        if event not in self._subs:
            raise ValueError(f"{event!r} is not an event")
        data = MappingProxyType(dict(payload))
        with self._lock:
            listeners = list(self._subs[event])
        notes: List[str] = []
        for pid, fn in listeners:
            if pid in self.faults:
                continue
            hook = f"its {event} listener"
            self._bounded(pid, hook, fn, (data,),
                          after="the run went on without it")
            fault = self.faults.get(pid)
            if fault is not None and fault.hook == hook and \
                    fault.kind == HANG:
                notes.append(f"{self._name(pid)}'s {event} listener did not "
                             f"return within {RUN_HOOK_SECONDS:g} s, and it "
                             f"was switched off")
        return notes

    def abandon_run(self, active: pr.ActiveRun) -> None:
        """A Start that failed after the run's name was reserved: its
        plugins were never told it started, so they are told nothing."""
        with active.lock:
            active.state = pr.ABANDONED
        SWITCH.release(active)
        active.close_files()
        self._forget(active, current=True)

    def end_blocks(self, active: pr.ActiveRun, camera: Optional[str] = None
                   ) -> Callable[[Mapping[str, Any]], Dict[str, Any]]:
        """What `camera`'s Recorder of the run asks once its last frame is
        on disk (capture.Recorder.end_blocks): that end record's "plugins"
        and "plugin_faults" (see _end_of_run). Bound to `active` and to
        `camera` (the run's first, when not given), so a run whose last
        frames land after the next Start still ends as its own, and each
        camera of a run files its own counts."""
        cam = (str(camera) if camera is not None
               else next(iter(active.folders), ""))
        return lambda doc: self._end_of_run(active, cam, doc)

    def _end_of_run(self, active: pr.ActiveRun, camera: str,
                    doc: Mapping[str, Any]) -> Dict[str, Any]:
        """`camera`'s end record of the run is being written: its counts
        are filed under `camera`, and the plugins whose turn it is are
        asked on_run_finished(RunInfo with the end records' counts) — a
        camera-scoped plugin with its camera's end record, a rig plugin
        ONCE, with the end record of the run's last camera to end, when
        every camera's counts are in (P3 review, ready for M1's runs of
        several cameras; a run of one camera asks every plugin at its one
        end record, as before). Best effort, and BOUNDED (_bounded): one
        that does not return in RUN_HOOK_SECONDS is switched off and the
        record written without it. Its block is CLEANED AND SIZE-CHECKED
        INSIDE THAT SAME GUARDED, BOUNDED CALL — cleaning reads the block,
        which can run the plugin's own code (a mapping's items(), a
        number's __float__) — so whatever it raises, SystemExit included,
        or however long it takes, is that plugin's fault and never the
        writer thread's (P3 review: a SystemExit there lost the end
        record, an error there lost every plugin's block, and an
        oversized block held the end record for tens of seconds). The
        files the plugins left open in the run are closed once its last
        camera has ended; the faults of the run are listed. Runs on the
        thread that writes the end record; each plugin's call on a thread
        of its own."""
        with active.lock:
            active.counts[camera] = pr.counts_of(dict(doc))
            last = all(c in active.counts for c in active.folders)
            if last:
                if active.state in (pr.RESERVED, pr.RECORDING):
                    active.state = pr.STOPPED
                active.ended = time.time()
        if last:
            # Its recording is over (a failed write may end it before Stop).
            SWITCH.release(active)
        info = active.info()
        blocks: Dict[str, Any] = {}
        for pid, cam in list(active.hooked):
            if not (cam == camera or (cam is None and last)):
                continue
            if pid in self.faults:
                continue
            made = self.instance(pid, cam)
            if made is None:
                continue
            out = self._bounded(
                pid, "on_run_finished", _hook(made, "on_run_finished"),
                (info,), after="the run's end record was written without "
                               "it",
                then=camera_record.clean_block)
            if out is None or out is _NO_HOOK or out is _TIMED_OUT \
                    or pid in self.faults:
                continue
            blocks[pid] = out
        kept, refused = camera_record.plugins_block(blocks)
        for pid, why in refused.items():
            if pid not in self.faults:
                self.fault(pid, "on_run_finished",
                           camera_record.BlockError(why))
        with active.lock:
            if last:
                active.state = pr.ENDED
            faults = dict(active.faults)
            stopped = active.told_stopped
        if last:
            if stopped:
                active.close_files()
            # else: the recording ended by itself (a failed write) while the
            # run goes on until Stop, and a plugin may still be writing what
            # the run holds into its streamed file (the triggering plugin's
            # triggers CSV has a row for every trigger until Stop, as
            # today's): what is left open is closed at Stop (run_stopped).
            self._forget(active)
        return {"plugins": kept, "plugin_faults": faults}

    def _bounded(self, pid: str, hook: str, fn: Callable[..., Any],
                 args: Sequence[Any], *, after: str,
                 then: Optional[Callable[[Any], Any]] = None,
                 kind: str = BEST_EFFORT) -> Any:
        """`fn(*args)` — plugin `pid`'s `hook` — guarded as `kind`, on a
        thread of its own, and waited for RUN_HOOK_SECONDS at most: what it
        returned (passed through `then` INSIDE THE SAME GUARD, so a result
        that runs the plugin's code when it is read — a block's items(), a
        reason's __str__ — is the plugin's to fail), its guard's default
        (None) when it failed, or _TIMED_OUT when it has not returned in
        time: the plugin is then switched off as a hang, `after` saying
        what went on without it. The call itself cannot be stopped (Python
        has no way to); nothing waits for it after that."""
        wait = RUN_HOOK_SECONDS
        box: Dict[str, Any] = {}
        done = threading.Event()

        def call(*given: Any) -> Any:
            out = fn(*given)
            if then is not None and out is not None and out is not _NO_HOOK:
                out = then(out)
            return out

        def run() -> None:
            try:
                box["out"] = self._guard(pid, hook, call, args, {}, None,
                                         kind=kind)
            finally:
                done.set()
        threading.Thread(target=run, daemon=True,
                         name=f"plugin-{hook}-{pid}").start()
        if not done.wait(wait):
            self.fault(pid, hook, PluginHung(
                f"{hook} did not return within {wait:g} s; {after}"), HANG)
            return _TIMED_OUT
        return box.get("out")

    def record_plugins(self, camera: Optional[str], device: Any = None
                       ) -> Tuple[Dict[str, Any], List[str]]:
        """The camera record's "plugins" block for a run of `camera`'s (the
        active camera's): what made the run (design 3.9, 4.2) — each plugin
        this project runs that is on, loaded or switched off after a fault,
        by id: its name, version, short name (an old run's sidecar is never
        put down to a later plugin of that short name) and kind, why it is
        switched off, and for the camera plugin that opened the camera its
        record_fields(device), cleaned and size-checked. With the words for
        Start's summary of any record field that could not go in, and of
        any plugin's entry the record cannot hold (said, never dropped in
        silence: P3 review)."""
        slot = self._slot(camera)
        info = getattr(slot, "info", None)
        backend = str(getattr(info, "backend", "") or "")
        source = self.camera_plugin_for(backend) if backend else None
        out: Dict[str, Any] = {}
        notes: List[str] = []
        for pid in self.runs():
            state, why = self.state(pid)
            m = self.manifest(pid)
            if state not in (ON, ACTIVE, FAULTED) or m is None:
                continue
            entry: Dict[str, Any] = {"name": m.name, "version": m.version,
                                     "short": m.short, "kind": m.kind}
            if state == FAULTED:
                entry["faulted"] = cut(why)
            if pid == source and device is not None:
                fields, note = self._record_fields(pid, device)
                if fields:
                    entry["fields"] = fields
                if note:
                    notes.append(note)
            out[pid] = entry
        kept, refused = camera_record.plugins_block(out)
        for pid, why in refused.items():
            notes.append(f"{self._name(pid)} is left out of the run's camera "
                         f"record — {why}")
        return kept, notes

    def _record_fields(self, pid: str, device: Any
                       ) -> Tuple[Dict[str, Any], str]:
        """Camera plugin `pid`'s record_fields(device), cleaned, best effort
        (design 3.10): ({}, why) when they could not be had or kept.

        The call AND the cleaning of what it gives run inside the camera
        guard (camera_call's rule: a built-in camera plugin is said and
        stays on, one from someone else is switched off), on a thread of
        its own, BOUNDED by RUN_HOOK_SECONDS — so a block that raises when
        read, SystemExit included, or takes long to read never refuses or
        holds Start (P3 review: it refused Start, a SystemExit escaped
        start() on the window's thread, and a slow one held Start 6 s)."""
        from .cameras import CameraError

        own = plugin_device(device)

        def call(made: Any) -> Any:
            fn = getattr(made, "record_fields", None)
            if not callable(fn):
                return {}
            fields = fn(own)
            if not fields:
                return {}
            return camera_record.clean_block(
                fields, level=camera_record.BLOCK_LEVEL + 1)

        box: Dict[str, Any] = {}
        done = threading.Event()

        def run() -> None:
            try:
                box["out"] = self._camera_run(pid, "record_fields", call)
            except BaseException as exc:                # noqa: BLE001
                # A CameraError: the camera's answer, or the plugin's fault
                # already logged and said by the camera guard.
                box["error"] = exc
            finally:
                done.set()
        threading.Thread(target=run, daemon=True,
                         name=f"plugin-record_fields-{pid}").start()
        said = (f"{self._name(pid)}: its fields in the run's camera record "
                f"were not written — ")
        if not done.wait(RUN_HOOK_SECONDS):
            self._camera_fault(pid, "record_fields", PluginHung(
                f"record_fields did not return within {RUN_HOOK_SECONDS:g} "
                f"s; the run's camera record was written without them"),
                kind=HANG)
            return {}, (f"{said}they did not come within "
                        f"{RUN_HOOK_SECONDS:g} s.")
        error = box.get("error")
        if isinstance(error, CameraError):
            return {}, f"{said}{cut(str(error))}."
        if error is not None:
            return {}, f"{said}{fault_text(error)}."
        fields = box.get("out")
        return (fields if isinstance(fields, dict) else {}), ""

    def frame_stage(self, pid: str, stage: Callable[[Any, Any], Any],
                    session: Any = None) -> "GuardedStage":
        """Plugin `pid`'s frame stage for one run, guarded on the RECORDING
        PATH (design 3.8, 3.10): set it as the run's capture.Recorder.
        frame_stage. A fault in it ends that run's recording, its reason in
        <run>_run_end.json."""
        return GuardedStage(self, pid, stage, session)

    def frame_stages(self, camera: str, session: Any = None
                     ) -> Optional[StagedAfterDownsample]:
        """What the run about to start on `camera` saves through when a
        built-in plugin gives it a frame stage (design 2.1, 3.8, 3.11;
        P3 row 6): every built-in plugin this project runs whose manifest
        contributes "frame_stage" is asked `frame_stage(camera)` — a stage
        for this run, or None for none — and each one given is guarded on
        the recording path (GuardedStage) and runs after the core's
        Downsample (StagedAfterDownsample). None when no plugin gives one:
        the recorder saves through Downsample alone, as today.

        Asked at Start before anything stops: what a plugin raises then
        switches it off and refuses this Start (RuntimeError, its words),
        as the trigger slot's gate does — a run without the stage asked for
        would not be the run asked for; the next Start runs without it."""
        stages: List[GuardedStage] = []
        for pid in self.with_hook("frame_stage"):
            rec = self.record(pid)
            if rec is None or not rec.builtin:
                continue
            made = self.instance(pid)
            if made is None:
                continue
            given = self._guard(pid, "frame_stage", _hook(made, "frame_stage"),
                                (str(camera),), {}, None)
            if pid in self.faults:
                fault = self.faults[pid]
                raise RuntimeError(f"Start refused — {self._name(pid)}: its "
                                   f"frame stage could not be had, and it "
                                   f"was switched off — {fault.error}")
            if given is None or given is _NO_HOOK:
                continue
            if not callable(given):
                exc = TypeError(f"frame_stage gave {type(given).__name__}, "
                                f"not a stage(image, meta)")
                self.fault(pid, "frame_stage", exc)
                raise RuntimeError(f"Start refused — {self._name(pid)}: "
                                   f"{exc}; it was switched off")
            stages.append(GuardedStage(self, pid, given, session))
        return StagedAfterDownsample(stages) if stages else None

    # -- the trigger slot (stage P3 row 2; design 3.6) ----------------------
    def trigger_plugin(self) -> Optional[str]:
        """The built-in plugin this project runs that fills the record
        gate's slot in Start and Stop (its manifest contributes
        "record_gate": the triggering plugin), when it can run now — or
        None."""
        for pid in self.with_hook("record_gate"):
            rec = self.record(pid)
            if rec is None or not rec.builtin:
                continue
            if self.state(pid)[0] in (ON, ACTIVE):
                return pid
        return None

    def trigger_slot(self, camera: str) -> Optional["TriggerSlot"]:
        """The triggering plugin's part of the run about to start on
        `camera` (its trigger_slot(camera)), guarded as a TriggerSlot — or
        None when no such plugin runs, or it gives none (a fault: said and
        logged, and it is switched off)."""
        pid = self.trigger_plugin()
        if pid is None:
            return None
        made = self.instance(pid)
        if made is None:
            return None
        inner = self._guard(pid, "trigger_slot", _hook(made, "trigger_slot"),
                            (str(camera),), {}, None)
        if inner is None or inner is _NO_HOOK or pid in self.faults:
            return None
        slot = TriggerSlot(self, pid, made, inner, str(camera))
        with self._lock:
            self._trigger_slots[str(camera)] = slot
        return slot

    def _trigger_slot_over(self, slot: "TriggerSlot") -> None:
        with self._lock:
            if self._trigger_slots.get(slot.camera) is slot:
                del self._trigger_slots[slot.camera]

    def capture_now(self, camera: Optional[str]) -> Optional[str]:
        """Capture now (F9) on `camera` (the active one): the triggering
        plugin's words — through the run in progress's slot when there is
        one, which serves its run out even after a best-effort fault — or
        None when no triggering plugin runs here."""
        if camera is None:
            camera = getattr(self._slot(None), "name", None)
        slot = self._trigger_slots.get(str(camera))
        if slot is not None:
            return slot.capture_now()
        pid = self.trigger_plugin()
        if pid is None:
            return None
        said = self.call(pid, "capture_now", camera, default=None)
        return said if isinstance(said, str) else None

    # -- the two file services and a plugin's data (design 3.9, 4.1) --------
    def _files_of(self, pid: str) -> Tuple[str, ...]:
        m = self.manifest(pid)
        return tuple(m.files) if m is not None else ()

    def create_file(self, pid: str, folder: Any, name: Any, *,
                    binary: bool = False) -> Any:
        """(a) plugin `pid`'s streamed run file (plugin_runs.create_file),
        in the run in progress that `folder` is a folder of."""
        with self._lock:
            runs = [r for r in reversed(self._runs) if r.open]
        active = next((r for r in runs if r.camera_of(folder)),
                      runs[0] if runs else None)
        return pr.create_file(active, folder, name, self._files_of(pid),
                              binary=binary)

    def write_new(self, pid: str, folder: Any, name: Any, data: Any) -> Path:
        """(b) plugin `pid`'s whole file (plugin_runs.write_new): beside a
        run in `folder`, or in its own data folder."""
        own = (pr.data_dir(self.project_dir, pid, make=False)
               if self.project_dir is not None else None)

        def runs(where: Path) -> List[str]:
            with self._lock:
                return [r.run for r in self._runs
                        if not r.over and r.camera_of(where)]
        return pr.write_new(folder, name, data, suffixes=self._files_of(pid),
                            own=own, runs=runs)

    def data_dir(self, pid: str) -> Path:
        """Plugin `pid`'s data folder in this project (design 4.1)."""
        if self.project_dir is None:
            raise pr.PluginFileError("no project is open, so there is no "
                                     "project folder for a plugin's data")
        return pr.data_dir(self.project_dir, pid)

    # -- presets by namespace (stage P3 row 4; design 2.5, 3.9) -------------
    def _running(self) -> set:
        """The plugins running in this window: on or loaded."""
        return {pid for pid in self.runs()
                if self.state(pid)[0] in (ON, ACTIVE)}

    def _takes_presets(self, pid: str) -> bool:
        m = self.manifest(pid)
        return m is not None and "presets" in m.contributes

    def split_preset(self, settings: Mapping[str, Any],
                     given: Optional[Mapping[str, Any]] = None) -> pk.Split:
        """`settings` (a preset's) split by owner (plugin_keys.split): what
        the camera is given, what each running plugin that takes presets is
        handed, and what is kept untouched for plugins not running."""
        running = self._running()
        return pk.split(settings, running=lambda pid: pid in running,
                        takes=self._takes_presets, given=given)

    def _for_camera(self, pid: str, camera: Optional[str]) -> Any:
        m = self.manifest(pid)
        scoped = m is not None and m.kind == "feature" and m.scope == "camera"
        return self.instance(pid, camera if scoped else None)

    def apply_preset_values(self, camera: Optional[str],
                            values: Mapping[str, Mapping[str, Any]]
                            ) -> List[str]:
        """Each running plugin's own values of the preset applied
        (`apply_preset_values(camera, values)`, best effort: a fault is
        said — the words returned — and the plugin switched off)."""
        notes: List[str] = []
        for pid, mine in dict(values or {}).items():
            made = self._for_camera(pid, camera)
            if made is None:
                notes.append(f"{self._name(pid)} is not running here: its "
                             f"{len(mine)} setting{'s' if len(mine) != 1 else ''}"
                             f" kept, not applied")
                continue
            self._guard(pid, "apply_preset_values",
                        _hook(made, "apply_preset_values"),
                        (camera, MappingProxyType(dict(mine))), {}, None)
            fault = self.faults.get(pid)
            if fault is not None and fault.hook == "apply_preset_values":
                notes.append(f"{self._name(pid)} could not take its values "
                             f"from the preset and was switched off — "
                             f"{fault.error}")
        return notes

    def preset_settings(self, camera: Optional[str],
                        settings: Mapping[str, Any],
                        old: Optional[Mapping[str, Any]] = None
                        ) -> Tuple[Dict[str, Any], List[str]]:
        """The preset to save for `camera`: `settings` (the camera's, as
        camera_presets.capture read them) with each running plugin's own
        values as "<id>/<key>" (preset_values(camera); plain values only),
        and — from `old`, the preset of that name it replaces — the keys of
        every plugin not running, and of a running one that gave nothing,
        exactly as they were (plugin_keys.merged). With the words to say."""
        running = self._running()
        new = dict(settings)
        gave: set = set()
        notes: List[str] = []
        for pid in self.with_hook("presets"):
            if pid not in running:
                continue
            made = self._for_camera(pid, camera)
            if made is None:
                continue
            # A mapping it gives is copied to a plain dict INSIDE the guard:
            # its items() is the plugin's own code (P3 review).
            got = self._guard(pid, "preset_values",
                              _plain_items(_hook(made, "preset_values")),
                              (camera,), {}, None)
            if pid in self.faults:
                fault = self.faults[pid]
                if fault.hook == "preset_values":
                    notes.append(f"{self._name(pid)} could not give its "
                                 f"values for the preset and was switched off "
                                 f"— {fault.error}; its keys are kept as "
                                 f"they were")
                continue
            if got is None or got is _NO_HOOK:
                continue
            if not isinstance(got, Mapping):
                notes.append(f"{self._name(pid)} gave its preset values as "
                             f"{type(got).__name__}, not a mapping — kept "
                             f"as they were")
                continue
            gave.add(pid)
            bad: List[str] = []
            for key, value in got.items():
                if not isinstance(key, str) or not _plain_value(value):
                    bad.append(key if isinstance(key, str)
                               else f"a {type(key).__name__} key")
                    continue
                try:
                    new[pk.namespaced(pid, key)] = value
                except ValueError:
                    bad.append(str(key))
            if bad:
                notes.append(f"{self._name(pid)}: {', '.join(bad)} not saved "
                             f"— a preset holds plain values only (true or "
                             f"false, text, a whole number, a finite "
                             f"number)")
        saved = pk.merged(new, dict(old or {}),
                          running=lambda pid: pid in running,
                          gives=lambda pid: pid in gave)
        kept: Dict[str, Dict[str, Any]] = {}
        for key, value in dict(old or {}).items():
            pid = pk.owner(key)
            if pid is not None and key in saved and saved[key] is value and (
                    pid not in running or (pk.is_namespaced(key)
                                           and pid not in gave)):
                kept.setdefault(pid, {})[key] = value
        words = self.kept_words(kept, applied=False)
        if words:
            notes.append(words)
        return saved, notes

    def kept_words(self, kept: Mapping[str, Mapping[str, Any]],
                   applied: bool = True) -> str:
        """The words for keys kept untouched for plugins not running here:
        "Kept for plugins not running here, not applied: Acme thermal
        (acme.thermal): 2 settings." — "" for none. A plugin that IS
        running but takes no preset values (its manifest does not
        contribute "presets") is said as such, never as not running (P3
        review: the Timing scan, running, was listed as not running)."""
        running = self._running()
        absent: List[str] = []
        idle: List[str] = []
        for pid, keys in dict(kept or {}).items():
            count = len(keys)
            label = self._name(pid)
            label = label if label == pid else f"{label} ({pid})"
            (idle if pid in running else absent).append(
                f"{label}: {count} setting{'s' if count != 1 else ''}")
        how = ("not applied" if applied else "kept in the preset as they "
               "were")
        out = []
        if absent:
            out.append(f"Kept for plugins not running here, {how}: "
                       f"{'; '.join(absent)}.")
        if idle:
            out.append(f"Kept for plugins running here that take no preset "
                       f"values, {how}: {'; '.join(idle)}.")
        return " ".join(out)

    # -- camera plugins (stage P2; design 2.4 item 3, 3.3, 3.4) ---------------
    def _runnable_cameras(self) -> List[str]:
        """Every camera plugin this project runs that can run now, in its
        list's order, whatever the set-up ticked."""
        out = []
        for pid in self.runs():
            m = self.manifest(pid)
            if m is None or m.kind != "camera":
                continue
            if self.state(pid)[0] in (ON, ACTIVE):
                out.append(pid)
        return out

    def has_setup(self, pid: str) -> bool:
        """Whether camera plugin `pid` has a set-up — a family the set-up
        wizard offers as a check box — as its manifest says ("setup")."""
        m = self.manifest(pid)
        return m is not None and m.kind == "camera" \
            and "setup" in m.contributes

    def setup_choice(self) -> Optional[List[str]]:
        """The camera families this project's set-up ticked: camera_setup.
        json beside its plugin list — FORMAT 1, its "cameras" list, or a
        file of today's single choice read as a list of one (design 2.4
        item 15) — or None when it was never set up (no file, or one not
        worth trusting): then every camera plugin is scanned, as today's
        Typhon scans everything until it is set up. Read at every call: the
        wizard changes it."""
        if self.project_dir is None:
            return None
        from . import camera_setup

        return camera_setup.load_families(
            camera_setup.setup_path(self.project_dir))

    def camera_plugins(self) -> List[str]:
        """The camera plugins this project runs and that can run now, in
        its list's order — the base set's, which is the built-in list's:
        Basler, Prophesee, Simulated, the order cameras.backends() has —
        less the families its set-up did not tick (setup_choice; stage P2
        row 4: the set-up's choice read as the project's plugin list). A
        camera plugin with no set-up (the simulated cameras) is always
        here, as today's simulated cameras are in every list."""
        out = self._runnable_cameras()
        ticked = self.setup_choice()
        if ticked is None:
            return out
        return [pid for pid in out if not self.has_setup(pid)
                or self.camera_backend(pid) in ticked]

    def family_plugins(self) -> List[str]:
        """The camera plugins this project runs that have a set-up — the
        families its set-up wizard offers, ticked or not — in its list's
        order, by their manifests alone: nothing is loaded. None of them
        (every one switched off) leaves only the simulated cameras, which
        need no set-up, so a project never set up is not asked then."""
        return [pid for pid in self._runnable_cameras()
                if self.has_setup(pid)]

    def camera_families(self) -> List[Any]:
        """Every camera family this project can be set up for, in its list's
        order — each camera plugin it runs that has a set-up, ticked or not
        — as council_core.camera_setup.Family, the set-up wizard's check
        boxes. Each part is guarded: a plugin's guide or check that fails
        comes back as a guide or a failed check saying so (logged, and a
        built-in camera plugin stays on), never as an exception in the
        wizard."""
        self.retry_cameras()
        out = []
        for pid in self.family_plugins():
            made = self._family(pid)
            if made is not None:
                out.append(made)
        return out

    def _family(self, pid: str) -> Any:
        from . import camera_setup as cs
        from .cameras import CameraError

        key = self.camera_backend(pid)
        if not key:
            return None
        name = self._name(pid)

        def failed_guide(why: str) -> Any:
            return cs.Guide(key=key, title=name, summary="", steps=(cs.Step(
                f"The {name} plugin could not give its set-up steps: "
                f"{why}"),))

        def guide(python: str) -> Any:
            try:
                made = self.camera_call(pid, "setup_guide", python)
            except CameraError as exc:
                return failed_guide(str(exc))
            if not isinstance(made, cs.Guide):
                return failed_guide(f"it gave {type(made).__name__}, not a "
                                    f"Guide")
            return made

        def failed_check(why: str) -> Any:
            return cs.Readiness(key, [cs.Check(
                "The check itself", False, why,
                "Try again; if it persists, reinstall the SDK.")])

        def check() -> Any:
            try:
                made = self.camera_call(pid, "setup_checks")
            except CameraError as exc:
                return failed_check(str(exc))
            if not isinstance(made, cs.Readiness):
                return failed_check(f"the {name} plugin gave "
                                    f"{type(made).__name__}, not a "
                                    f"Readiness")
            return made

        def own(attr: str) -> Any:
            try:
                return self._camera_run(pid, attr,
                                        lambda made: getattr(made, attr, None))
            except CameraError:
                return None

        label = own("setup_label")
        scan = None
        if callable(own("setup_scan")):
            def scan(held: Mapping[str, str]) -> Any:
                return self.camera_call(pid, "setup_scan", dict(held or {}))
        words = guide(sys.executable)
        scan_label = own("setup_scan_label")
        scan_tip = own("setup_scan_tip")
        return cs.Family(
            key=key, plugin=pid,
            label=label if isinstance(label, str) and label else name,
            title=words.title, summary=words.summary, guide=guide,
            check=check, scan=scan,
            scan_label=(scan_label if isinstance(scan_label, str)
                        and scan_label else f"Scan {name}"),
            scan_tip=scan_tip if isinstance(scan_tip, str) else "")

    def camera_call(self, pid: str, hook: str, *args: Any) -> Any:
        """Camera plugin `pid`'s `hook`, for a camera job: a scan, an open,
        a confirmation (loading the plugin first if need be).

        THE CAMERA'S OWN REFUSAL — a CameraError (NeedsStop, GrabFailed,
        SettingError ...) — reaches the caller as it is: the camera said no
        (it is busy, gone, cannot take that area), which is never the
        plugin's fault. ANYTHING ELSE is the plugin's: logged and said, and
        the caller gets a CameraError naming the plugin, so the code that
        handles a camera's refusal handles it too. A plugin from someone
        else is then switched off for the session; a built-in camera plugin
        is not (design 3.10: never switched off without asking).

        NOT WATCHED BY THE WATCHDOG: these wait on an SDK and a camera, for
        seconds (pypylon's import alone took 0.6-2.0 s, design 3.3), and
        switching the plugin off would free neither."""
        def call(made: Any) -> Any:
            fn = getattr(made, hook, None)
            if not callable(fn):
                raise AttributeError(f"the camera plugin has no {hook}()")
            return fn(*args)
        return self._camera_run(pid, hook, call)

    def _camera_run(self, pid: str, hook: str,
                    fn: Callable[[Any], Any]) -> Any:
        """`fn(plugin)` on camera plugin `pid`, guarded as camera_call says
        (the lookup of the hook inside the guard: getattr runs plugin
        code)."""
        from .cameras import CameraError

        made = self.instance(pid)
        if made is None:
            raise CameraError(f"{self._name(pid)} cannot run here: "
                              f"{self.state(pid)[1]}")
        try:
            return fn(made)
        except CameraError:
            raise
        except BaseException as exc:                    # noqa: BLE001
            if not _ours(exc):
                raise
            self._camera_fault(pid, hook, exc)
            raise CameraError(f"{self._name(pid)} failed in {hook}: "
                              f"{fault_text(exc)}") from exc

    def _camera_fault(self, pid: str, hook: str, exc: BaseException,
                      kind: str = BEST_EFFORT) -> None:
        """A camera plugin's own error (camera_call): a plugin from someone
        else is switched off; a built-in camera plugin is only logged and
        said, and stays on."""
        rec = self.record(pid)
        if rec is None or not rec.builtin:
            self.fault(pid, hook, exc, kind)
            return
        made = Fault(plugin=pid, hook=hook, kind=kind,
                     error=fault_text(exc),
                     trace=_trace(exc),
                     at=time.strftime("%Y-%m-%d %H:%M:%S"))
        self._log(made)
        self._note_in_runs(pid, f"{made.reason} — a built-in camera plugin "
                                f"stays on")
        self._announce(f"Plugin {self._name(pid)} failed in {hook}: "
                       f"{made.error} — it stays on (a built-in camera "
                       f"plugin is never switched off without asking)")

    def camera_backend(self, pid: str) -> str:
        """Camera plugin `pid`'s backend name — its cameras' CameraInfo.
        backend ("basler") — or "" when it cannot run or names none.

        The name is the camera family the set-up saves (camera_setup.
        FAMILY_RE: lower case, at most 64 characters, so never the "|" that
        preset keys are split at), and a plugin from someone else must name
        one with a dot ("acme.thermal", design 3.3), so it can never claim a
        built-in's preset keys. A plugin from someone else that names one
        that is not is switched off with the reason (backend_problem):
        before the P2 review "Acme.Thermal" was offered as a check box, and
        finishing the wizard raised ValueError in the window."""
        from .cameras import CameraError

        try:
            name = self._camera_run(pid, "backend",
                                    lambda made: getattr(made, "backend", ""))
        except CameraError:
            return ""
        name = name if isinstance(name, str) else ""
        if not name:
            return ""
        rec = self.record(pid)
        builtin = rec is not None and rec.builtin
        problem = backend_problem(name, builtin=builtin)
        if problem:
            if rec is not None and not builtin:
                self.fault(pid, "backend", ValueError(problem))
            return ""
        self._backend_names[pid] = name
        return name

    def camera_plugin_for(self, backend: str) -> Optional[str]:
        """The camera plugin this project runs whose cameras carry
        `backend`, or None. The first in the list's order wins."""
        for pid in self.camera_plugins():
            if self.camera_backend(pid) == backend:
                return pid
        return None

    def camera_available(self, pid: str) -> Tuple[bool, str]:
        """(ready?, what to install) of camera plugin `pid`; a plugin that
        cannot say is not ready, and says why."""
        from .cameras import CameraError

        try:
            said = self.camera_call(pid, "available")
        except CameraError as exc:
            return False, str(exc)
        if isinstance(said, tuple) and len(said) == 2:
            return bool(said[0]), str(said[1] or "")
        return bool(said), ""

    def backends(self, pids: Optional[Sequence[str]] = None
                 ) -> List["PluginBackend"]:
        """The camera plugins (this project's, or `pids`) as today's
        cameras.Backend: what cameras.discover(known=) and cameras.
        open_camera(known=) take, so a scan through the camera plugins is
        today's scan — the same order, the same notes — and the code that
        stays in council_core works on top of the registry (design 2.4
        item 3). cameras.backends() itself is unchanged: the Council's
        Capture tab keeps it."""
        chosen = list(pids) if pids is not None else self.camera_plugins()
        return [PluginBackend(self, pid) for pid in chosen]

    def discover_cameras(self, pids: Optional[Sequence[str]] = None) -> Any:
        """Every camera the camera plugins find, and why any was skipped
        (cameras.Discovery), through cameras.discover. A scan is where a
        built-in camera plugin that failed to load is tried again
        (retry_cameras). The project's own scan (no `pids`) also says why
        it has no cameras of a camera plugin it runs that cannot run now,
        and of a family the set-up ticked that no camera plugin it runs
        opens now (`_unscanned`): before the P2 review such a plugin's
        cameras dropped out of the second scan with no word, and a ticked
        family switched off left "Every backend was searched."."""
        from . import cameras

        self.retry_cameras()
        chosen = list(pids) if pids is not None else self.camera_plugins()
        backends = self.backends(chosen)
        found = cameras.discover(backends)
        for backend in backends:
            found.notes.extend(backend.dropped)
        if pids is None:
            found.notes.extend(self._unscanned(chosen))
        return found

    def retry_cameras(self) -> None:
        """Let every built-in camera plugin whose load or activate failed be
        tried again at its next use: a built-in camera plugin is never
        switched off without asking (design 3.10). Asked by a scan and by
        the set-up wizard's families; a load that fails again is logged and
        said again."""
        with self._fault_lock:
            self._load_failures.clear()

    def _unscanned(self, scanned: Sequence[str]) -> List[str]:
        """The notes for what the project's scan could not look at, beyond
        what cameras.discover says of the plugins it scanned: one for each
        camera plugin this project runs that cannot run now (faulted,
        failed to load, incompatible, waiting for approval) — unless it is
        a family the set-up did not tick, which is not looked for, as today
        — and one for each ticked family that no camera plugin this project
        runs opens now (its plugin switched off, or not on this PC). None
        when every camera plugin runs: the scan is today's, note for
        note."""
        from . import camera_setup

        ticked = self.setup_choice()
        notes: List[str] = []
        said: set = set()
        for pid in self.runs():
            m = self.manifest(pid)
            if m is None or m.kind != "camera" or pid in scanned:
                continue
            state, why = self.state(pid)
            if state in (ON, ACTIVE):
                continue                # a family not ticked: not looked for
            backend = self._backend_names.get(pid, "")
            if ticked is not None and backend and self.has_setup(pid) \
                    and backend not in ticked:
                continue
            if backend:
                said.add(backend)
            notes.append(f"{self._name(pid)}: not available — {why}")
        opened = {self._backend_names.get(pid, "") for pid in scanned}
        for family in ticked or ():
            if family in said or family in opened:
                continue
            label = (camera_setup.label(family)
                     if family in camera_setup.CHOICES else family)
            notes.append(f"{label}: ticked in Camera setup…, but no "
                         f"camera plugin this project runs can open its "
                         f"cameras now (switched off in its plugin list, or "
                         f"not on this PC), so none were looked for")
        return notes

    def camera_capabilities(self, pid: str, device: Any) -> frozenset:
        """What camera plugin `pid` confirms the opened `device` can do,
        within what its manifest declares and Typhon knows (design 3.4: the
        manifest's list, confirmed by the opened camera; an unknown name is
        ignored). A confirmation that fails is the device's own
        description, within the same bounds."""
        from .cameras import CameraError

        m = self.manifest(pid)
        declared = cc.known(m.capabilities if m is not None else ())
        try:
            confirmed = frozenset(self.camera_call(pid, "capabilities",
                                                   device) or ())
        except CameraError:
            confirmed = cc.of_device(device)
        return frozenset(confirmed & declared)

    def open_camera(self, info: Any, camera: Optional[str] = None) -> Any:
        """Open `info`'s camera through the camera plugin of its backend
        and return the Device. With `camera` (a slot's name) the slot keeps
        the capabilities the plugin confirmed (CameraSlot.confirm); the
        caller puts the device in the slot, as Connect does. A camera's
        refusal is a CameraError, as cameras.open_camera's is. A slot name
        the rig does not have is refused before anything is opened.

        A camera from a plugin of someone else's whose own flags (restartable,
        area_live, raw_recording, grab_failures, frames or events) disagree
        with what its plugin confirmed is closed again and refused: the core
        still acts on those flags (frame_camera restarts a stream when
        `restartable` says so) while every feature asks the slot, so the two
        must agree (P2 review). The built-in plugins are held to the same by
        the contract suite."""
        from .cameras import CameraError

        slot = None
        if camera is not None:
            slot = self._slot(camera)
            if slot is None:
                raise KeyError(f"no camera slot named {camera!r}")
        backend = str(getattr(info, "backend", "") or "")
        pid = self.camera_plugin_for(backend)
        if pid is None:
            raise CameraError(f"no camera plugin this project runs opens "
                              f"{backend!r} cameras")
        device = self.camera_call(pid, "open", info)
        caps = self.camera_capabilities(pid, device)
        rec = self.record(pid)
        if rec is not None and not rec.builtin:
            own = cc.of_device(device, info)
            odd = [name for name in cc.FROM_FLAGS
                   if (name in own) != (name in caps)]
            if odd:
                try:
                    device.close()
                except Exception:                         # noqa: BLE001
                    pass
                said = ", ".join(
                    f"{name} (" + ("the camera has it, its plugin does not "
                                   "say so" if name in own else
                                   "its plugin says so, the camera does "
                                   "not") + ")" for name in odd)
                raise CameraError(f"{self._name(pid)} opened "
                                  f"{getattr(info, 'label', info)}, but the "
                                  f"camera and its plugin disagree on what it "
                                  f"can do: {said} — the camera is not used")
        if needs_core_capture(device, str(getattr(info, "kind", "") or "")):
            # A frame camera whose plugin gives it no capture set-up: the
            # core holds one (council_core.plugin_capture) — the Downsample
            # row, the trigger rows, snapshot runs (design 2.5, 3.6; P3
            # review). Its plugin is still handed its own Device.
            from .plugin_capture import CoreCapture

            device = CoreCapture(device)
        confirm = getattr(slot, "confirm", None)
        if callable(confirm):
            confirm(device, caps)
        return device

    def category_tables(self, pids: Optional[Sequence[str]] = None) -> Any:
        """The settings tabs' tables merged from every camera plugin's
        hints (design 2.4 item 9; council_core.camera_categories.merge), for
        camera_categories.plan(..., tables=). A plugin whose hints cannot be
        read adds none: its groups still get tabs of their own."""
        from . import camera_categories
        from .cameras import CameraError

        hints = []
        for pid in (list(pids) if pids is not None
                    else self.camera_plugins()):
            try:
                made = self.camera_call(pid, "categories")
            except CameraError:
                continue
            if isinstance(made, api.CategoryHints):
                hints.append(made)
        return camera_categories.merge(hints)

    def review_sources(self) -> Dict[str, api.ReviewSource]:
        """The review sources of the camera plugins this project runs, by
        plugin id (design 3.7: the slider's views of a camera plugin's run
        files) — asked only of those whose manifest lists review_source."""
        from .cameras import CameraError

        out: Dict[str, api.ReviewSource] = {}
        for pid in self.camera_plugins():
            m = self.manifest(pid)
            if m is None or "review_source" not in m.contributes:
                continue
            try:
                made = self.camera_call(pid, "review_source")
            except CameraError:
                continue
            if isinstance(made, api.ReviewSource):
                out[pid] = made
        return out

    # -- the rig's camera names (camera_rig.json; stage P2 row 3) ---------
    def rig_file(self) -> Any:
        """This project's camera_rig.json (council_core.camera_rig_file.
        RigFile: its camera names and rig presets), or None with no project
        open."""
        if self.project_dir is None:
            return None
        from . import camera_rig_file

        return camera_rig_file.RigFile(
            camera_rig_file.rig_path(self.project_dir))

    def name_camera(self, camera: str, info: Any) -> Tuple[str, str]:
        """Give the camera just opened in slot `camera` its name in the rig
        (multi-camera design 2.2): the name it had before in this project
        (camera_rig.json) — the same camera gets the same name next time —
        else the slot's own when no other camera has it, else the first
        free camN; the slot is then called that. Returns (name, note): the
        note says why the slot kept its name — a rig file that is damaged
        (left as it is) or cannot be written, or the name taken by another
        slot — and is "" when all went well. Never raises for those."""
        rig = self.rig
        slot = self._slot(camera)
        if rig is None or slot is None:
            raise KeyError(f"no camera slot named {camera!r}")
        store = self.rig_file()
        if store is None:
            return slot.name, ""
        from .camera_rig_file import RigError, RigFileError

        others = [s.name for s in rig if s is not slot]
        try:
            name = store.assign(info, prefer=slot.name, taken=others)
        except (RigError, RigFileError) as exc:
            said = f"the camera keeps the name {slot.name} — {exc}"
            if getattr(exc, "damaged", False):
                # How to mend it (P2 review): the file is never moved or
                # written over unasked, so the user is told what to do.
                said += (" — mend it by hand, or move it aside (rename "
                         "the file), and the next Connect starts a new one")
            return slot.name, said
        if name == slot.name:
            return name, ""
        try:
            rig.rename_slot(slot.name, name)
        except ValueError as exc:
            return slot.name, (f"this camera is {name} in the rig, but it "
                               f"keeps the name {slot.name} here: {exc}")
        return name, ""

    def rename_camera(self, camera: str, new: str) -> str:
        """Call the camera open in slot `camera` `new` from now on (multi-
        camera design 2.2: renameable): in camera_rig.json, every rig
        preset that names it included, so it keeps the new name next time,
        and the slot. Returns the new name.

        Refused before anything changes — camera_rig_file.RigError, a
        ValueError — when `new` is not a safe name, when another slot or
        another camera of the rig has it, or when the slot has no camera
        open (a name belongs to a camera); RigFileError when the file is
        damaged, newer or cannot be written, and it is left as it is. The
        name window that asks for it comes with the rig controls (stage P6);
        nothing on today's screen calls this."""
        from .camera_rig_file import RigError, check_name

        rig = self.rig
        slot = self._slot(camera)
        if rig is None or slot is None:
            raise KeyError(f"no camera slot named {camera!r}")
        check_name(new)
        info = getattr(slot, "info", None)
        if getattr(slot, "device", None) is None or info is None:
            raise RigError(f"{slot.name} has no camera open: connect one "
                           f"first (a name belongs to a camera)")
        others = [s.name for s in rig if s is not slot]
        if new in others:
            raise RigError(f"a camera is already named {new!r}")
        store = self.rig_file()
        if store is not None:
            have = store.name_of(info)
            if have != new:
                if any(c.name == new for c in store.cameras()):
                    raise RigError(f"another camera of this rig is named "
                                   f"{new!r}")
                if have is None:
                    # Not written down yet (its file was damaged at
                    # Connect, and mended since): it is now, as `new`.
                    store.assign(info, prefer=new, taken=others)
                else:
                    store.rename(have, new)
        rig.rename_slot(slot.name, new)
        return new

    # -- the guard -----------------------------------------------------------
    def _guard(self, pid: str, hook: str, fn: Callable[..., Any],
               args: Sequence[Any], kwargs: Mapping[str, Any], default: Any,
               kind: str = BEST_EFFORT) -> Any:
        """Call into plugin `pid`, never letting its error out: any
        exception, SystemExit and KeyboardInterrupt included, faults it —
        but a KeyboardInterrupt on the main thread is the user's (_ours). A
        faulted plugin is not called again."""
        if pid in self.faults:
            return default
        key = (pid, threading.get_ident())
        before = self._busy.get(key)
        self._busy[key] = (hook, self._clock())
        try:
            return fn(*args, **kwargs)
        except BaseException as exc:                    # noqa: BLE001
            if not _ours(exc):
                raise
            if hook in ("loading", "activate") and self._keeps_on(pid):
                self._load_failed(pid, hook, exc, kind)
            else:
                self.fault(pid, hook, exc, kind)
            return default
        finally:
            if before is None:
                self._busy.pop(key, None)
            else:
                self._busy[key] = before

    def _keeps_on(self, pid: str) -> bool:
        """Whether plugin `pid` is a built-in camera plugin: never switched
        off without asking (design 3.10)."""
        rec = self.record(pid)
        return rec is not None and rec.builtin and rec.manifest is not None \
            and rec.manifest.kind == "camera"

    def _load_failed(self, pid: str, hook: str, exc: BaseException,
                     kind: str) -> None:
        """A built-in camera plugin failed to load or activate: logged and
        said, NOT switched off. It cannot run until it is tried again —
        the next scan (retry_cameras) — so a scan still says why it has no
        cameras, every time (camera_call's rule for its other calls; before
        the P2 review a failed load switched it off for the session, and
        its cameras dropped out of the next scan with no word)."""
        made = Fault(plugin=pid, hook=hook, kind=kind,
                     error=fault_text(exc),
                     trace=_trace(exc),
                     at=time.strftime("%Y-%m-%d %H:%M:%S"))
        with self._fault_lock:
            self._load_failures[pid] = made
        self._log(made)
        self._note_in_runs(pid, f"{made.reason} — a built-in camera plugin "
                                f"stays on")
        self._announce(f"Plugin {self._name(pid)} failed in {hook}: "
                       f"{made.error} — it stays on (a built-in camera "
                       f"plugin is never switched off without asking) and is "
                       f"tried again at the next scan")

    def fault(self, pid: str, hook: str, exc: BaseException,
              kind: str = BEST_EFFORT) -> Fault:
        """Switch plugin `pid` off for the rest of the session, because of
        `exc` in `hook`: kept in `faults`, said in the status line, and its
        traceback appended to <project>/plugin_errors.log. The first fault
        is the one its state shows; every one is logged. Its words are cut
        to FAULT_TEXT characters wherever they are said or kept (the
        records' plugin_faults and `faulted`); the log keeps the whole."""
        made = Fault(plugin=pid, hook=hook, kind=kind,
                     error=fault_text(exc),
                     trace=_trace(exc),
                     at=time.strftime("%Y-%m-%d %H:%M:%S"))
        # Its own small lock, not the host's: the watchdog faults a plugin
        # whose hung call may be holding the host's lock (activate).
        with self._fault_lock:
            first = pid not in self.faults
            if first:
                self.faults[pid] = made
        self._log(made)
        self._note_in_runs(pid, made.reason)
        if first:
            for feed in list(self._feeds):
                if feed.plugin == pid:
                    feed.stop(wait=0.0)
            self._announce(f"Plugin {self._name(pid)} was switched off after "
                           f"an error in {hook}: {made.error}")
        return self.faults[pid]

    def _note_in_runs(self, pid: str, reason: str) -> None:
        """A plugin's fault goes in the end record of every run not over
        yet (design 3.10: a best-effort fault is said there)."""
        for active in list(self._runs):
            active.note_fault(pid, reason)

    def _log(self, fault: Fault) -> None:
        if self.project_dir is None:
            return
        rec = self.record(fault.plugin)
        version = (rec.manifest.version if rec is not None
                   and rec.manifest is not None else "?")
        text = (f"==== {fault.at}  {fault.plugin} {version}  {fault.hook} "
                f"({fault.kind}): {fault.error}\n{fault.trace}\n")
        try:
            with self._fault_lock, open(self.project_dir / ERRORS_LOG, "a",
                                        encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            pass                  # the fault is still kept and said

    def watchdog(self, now: Optional[float] = None) -> List[str]:
        """Switch off every plugin with a call running past
        WATCHDOG_SECONDS; returns their ids. The call itself cannot be
        interrupted — Python has no way to — but nothing calls the plugin
        again, and a live feed stops offering it frames."""
        now = self._clock() if now is None else now
        hung: List[str] = []
        for (pid, _tid), (hook, started) in dict(self._busy).items():
            if pid in self.faults or now - started <= WATCHDOG_SECONDS:
                continue
            self.fault(pid, hook, PluginHung(
                f"{hook} has run {now - started:.1f} s, past the "
                f"{WATCHDOG_SECONDS:g} s a call may take"), HANG)
            hung.append(pid)
        return hung

    def start_watchdog(self, every: Optional[float] = None) -> None:
        """Run `watchdog` on a thread of its own, every `every` seconds
        (WATCHDOG_EVERY), until `close`. The host starts it itself when the
        first plugin is loaded or fed frames; a host that loads nothing
        starts no thread."""
        with self._lock:
            if self._closing.is_set() or (
                    self._watchdog is not None and self._watchdog.is_alive()):
                return
            pause = WATCHDOG_EVERY if every is None else float(every)

            def loop() -> None:
                while not self._closing.wait(pause):
                    try:
                        self.watchdog()
                    except Exception:                     # noqa: BLE001
                        pass          # the next look tries again
            self._watchdog = threading.Thread(target=loop, daemon=True,
                                              name="plugin-watchdog")
            self._watchdog.start()

    # -- the recording path --------------------------------------------------
    def record_gate(self, pid: str, gate: Any, session: Any = None
                    ) -> "GuardedGate":
        """Plugin `pid`'s record gate for one run of `session`, guarded on
        the recording path: a fault in it ends THAT run's recording cleanly
        with its reason (design 3.10). Hand it to `session.record_to(...,
        gate=)`, which gives it the run's writer."""
        return GuardedGate(self, pid, gate, session)

    def recording_reason(self, pid: str, hook: str, exc: BaseException,
                         frame: Any = None) -> str:
        """The reason a recording ends with for a fault on the recording
        path, naming the plugin, the hook and the frame."""
        where = (f" at frame {frame.index}"
                 if getattr(frame, "index", None) is not None else "")
        return (f"recording ended by plugin {self._name(pid)} ({pid}): its "
                f"{hook} failed{where} — {fault_text(exc)}")

    def recording_fault(self, pid: str, hook: str, exc: BaseException,
                        frame: Any = None) -> str:
        """Fault `pid` on the recording path; the reason the recording ends
        with (recording_reason)."""
        self.fault(pid, hook, exc, RECORDING)
        return self.recording_reason(pid, hook, exc, frame)

    # -- events and the UI thread --------------------------------------------
    def subscribe(self, pid: str, event: str,
                  fn: Callable[[Mapping[str, Any]], Any]) -> Callable[[], None]:
        if event not in self._subs:
            raise ValueError(f"{event!r} is not an event a plugin can listen "
                             f"for ({', '.join(api.EVENTS)})")
        entry = (pid, fn)
        with self._lock:
            self._subs[event].append(entry)

        def stop() -> None:
            with self._lock:
                try:
                    self._subs[event].remove(entry)
                except ValueError:
                    pass
        return stop

    def emit(self, event: str, **payload: Any) -> int:
        """Tell every listener of `event` (best effort each); how many were
        told."""
        if event not in self._subs:
            raise ValueError(f"{event!r} is not an event")
        data = MappingProxyType(dict(payload))
        told = 0
        with self._lock:
            listeners = list(self._subs[event])
        for pid, fn in listeners:
            if pid in self.faults:
                continue
            self._guard(pid, f"its {event} listener", fn, (data,), {}, None)
            told += 1
        return told

    def queue_ui(self, pid: Optional[str], fn: Callable[[], Any]) -> None:
        """Run `fn` on the UI thread: through `ui_caller` when the window
        set one, else when `pump_ui` is next called (from the UI timer)."""
        caller = self.ui_caller
        if caller is not None:
            caller(lambda: self._run_ui(pid, fn))
        else:
            self._ui.append((pid, fn))

    def pump_ui(self, limit: int = 100) -> int:
        """Run up to `limit` queued UI calls; how many ran."""
        ran = 0
        while self._ui and ran < limit:
            pid, fn = self._ui.popleft()
            self._run_ui(pid, fn)
            ran += 1
        return ran

    def _run_ui(self, pid: Optional[str], fn: Callable[[], Any]) -> None:
        if pid is None:
            try:
                fn()
            except Exception:                             # noqa: BLE001
                pass
        else:
            self._guard(pid, "call_in_ui", fn, (), {}, None)

    def _announce(self, text: str) -> None:
        """A line for the status line: on the UI thread now, from any other
        thread through the UI queue."""
        self.said.append(text)
        fn = self.say_fn
        if fn is None:
            return
        if threading.get_ident() == self.ui_thread:
            try:
                fn(text)
            except Exception:                             # noqa: BLE001
                pass
        else:
            self.queue_ui(None, lambda: fn(text))

    def say(self, pid: str, text: str) -> None:
        """A plugin's line, named as its own, at most a status line long."""
        line = " ".join(str(text).split())
        if len(line) > 200:
            line = line[:199] + "…"
        self._announce(f"{self._name(pid)}: {line}")

    # -- settings (design 4.2, question 14) ------------------------------------
    def settings(self, pid: str) -> Dict[str, Any]:
        return pl.effective_settings(self.base_set(), self.project, pid)

    def save_settings(self, pid: str, values: Mapping[str, Any]) -> None:
        """THIS project's own values of `pid`'s settings (question 14)."""
        if self.project is None or self.project_dir is None:
            raise pl.PluginListError(
                "no project is open, so there is nowhere to keep this "
                "project's settings" if self.project_problem == "" else
                self.project_problem)
        self.project = pl.save_project_settings(self.project_dir, pid, values)

    def make_default(self, pid: str, keys: Optional[Sequence[str]] = None
                     ) -> None:
        """"Make this the default for every project": this project's values
        become the base set's defaults (question 14)."""
        if self.project_dir is None or self.project is None:
            raise pl.PluginListError("no project is open")
        base, self.project = pl.make_default(
            self.project_dir, self.home.path, self.builtin_ids(), pid, keys)
        self._base = base

    def set_base_plugins(self, plugins: Sequence[str]) -> None:
        """Change the base set's list; it reaches projects made after this
        (question 1)."""
        self._base = pl.set_base_plugins(self.home.path, self.builtin_ids(),
                                         plugins)

    def switch_on(self, pid: str) -> None:
        if self.project_dir is None or self.project is None:
            raise pl.PluginListError("no project is open")
        self.project = pl.switch_on(self.project_dir, pid)

    def switch_off(self, pid: str) -> None:
        """Off in this project; its instances are deactivated now."""
        if self.project_dir is None or self.project is None:
            raise pl.PluginListError("no project is open")
        self.project = pl.switch_off(self.project_dir, pid)
        self._deactivate(lambda key: key[0] == pid)

    # -- live frames ---------------------------------------------------------
    def feed(self, pid: str, on_frame: Optional[Callable[[Any], Any]] = None,
             camera: Optional[str] = None, budget_ms: Optional[float] = None,
             duty: Optional[float] = DUTY_CAP) -> Optional["LiveFeed"]:
        """A LiveFeed of frames to plugin `pid`'s `on_frame` (or to the
        `on_frame` given, which counts as that plugin's), on a thread of
        its own; None when the plugin cannot run or has no on_frame.
        `duty` caps its busy share of the time: DUTY_CAP by default (design
        3.11's hook table; the P1 timing bench), None for uncapped."""
        made = self.instance(pid, camera)
        if made is None:
            return None
        fn = on_frame
        if fn is None:
            # Looked up inside the guard: getattr can run plugin code.
            fn = self._guard(pid, "on_frame",
                             lambda: getattr(made, "on_frame", None), (), {},
                             None)
        if not callable(fn) or pid in self.faults:
            return None
        self.start_watchdog()
        m = self.manifest(pid)
        budget = budget_ms if budget_ms is not None else (
            m.budget_ms if m is not None else pm.DEFAULT_BUDGET_MS)
        slot = self._slot(camera)
        made_feed = LiveFeed(self, pid, fn,
                             camera=getattr(slot, "name", camera or ""),
                             budget_ms=budget, duty=duty)
        with self._lock:
            self._feeds.append(made_feed)
        made_feed.start()
        return made_feed

    # -- per-plugin temporary folders ------------------------------------------
    def temp_dir(self, pid: str) -> Path:
        with self._lock:
            made = self._temp.get(pid)
            if made is None or not made.is_dir():
                short = (self.manifest(pid).short
                         if self.manifest(pid) is not None else "plugin")
                made = Path(tempfile.mkdtemp(prefix=f"typhon-{short}-"))
                self._temp[pid] = made
            return made

    # -- closing ----------------------------------------------------------------
    def _deactivate(self, which: Callable[[Tuple[str, Any]], bool]) -> None:
        """Let go of the instances whose key `which` picks — and of any
        still being made for such a key, once its activate returns."""
        with self._lock:
            keys = [k for k in self._instances if which(k)]
            made = [(k, self._instances.pop(k)) for k in keys]
            for k in keys:
                ctx = self._contexts.pop(k, None)
                if ctx is not None:
                    ctx._gone = True        # its services close with it
            for k, loading in self._loading.items():
                if which(k):
                    loading.cancelled = True
            gone = {k[0] for k in keys}
            for event in self._subs:
                self._subs[event] = [(p, f) for p, f in self._subs[event]
                                     if p not in gone]
        for feed in list(self._feeds):
            if feed.plugin in gone:
                feed.stop()
        for (pid, _slot), obj in made:
            self._let_go(pid, obj)

    def _let_go(self, pid: str, obj: Any) -> None:
        """`obj`'s deactivate — called even for a faulted plugin, so it can
        let go of what it holds. Its error, the lookup's included, is only
        logged and never stops the others."""
        try:
            fn = getattr(obj, "deactivate", None)
            if callable(fn):
                fn()
        except BaseException as exc:                    # noqa: BLE001
            if not _ours(exc):
                raise
            self._log(Fault(pid, "deactivate", BEST_EFFORT,
                            fault_text(exc), _trace(exc),
                            time.strftime("%Y-%m-%d %H:%M:%S")))

    def close(self) -> None:
        """The window is closing (after any recording has stopped): stop the
        watchdog and the feeds, deactivate every instance, remove this
        host's temporary folders."""
        self._closing.set()
        for feed in list(self._feeds):
            feed.stop()
        self._deactivate(lambda key: True)
        self._unregister_files()
        with self._lock:
            self._feeds.clear()
            temp, self._temp = dict(self._temp), {}
            runs = list(self._runs) + list(self._current.values())
        for active in runs:
            SWITCH.release(active)          # a run this host never ended
        for folder in temp.values():
            # Only folders this host made with mkdtemp, under the system's
            # temporary folder: never anything of the user's.
            try:
                if Path(folder).resolve().parent == \
                        Path(tempfile.gettempdir()).resolve():
                    shutil.rmtree(folder, ignore_errors=True)
            except OSError:
                pass


# ======================================================================
# A plugin's context
# ======================================================================
class Context:
    """What one plugin instance is handed (typhon_plugin_api.PluginContext).
    `slot_id` ties a camera-scoped feature to its slot; None follows the
    active camera.

    A PLUGIN SWITCHED OFF IS OFF (design 3.10): once its plugin is faulted,
    or its instance let go (switched off in the project, another project
    opened, the window closing), the context's file services refuse
    (PluginFileError), save_settings refuses (PluginListError) and `on`
    listens for nothing — a thread the plugin left running can no longer
    make run files, write files or change the project (P3 review: a
    faulted plugin's thread still made a run file and saved settings).
    Nothing it could do was ever over anything; now it does nothing."""

    def __init__(self, host: PluginHost, pid: str,
                 slot_id: Optional[int] = None) -> None:
        self._host = host
        self.plugin_id = pid
        self._slot_id = slot_id
        #: Its instance was let go (host._deactivate): its services close.
        self._gone = False

    def _off(self) -> str:
        """Why this plugin may no longer act here, or ""."""
        host = self._host
        fault = host.faults.get(self.plugin_id)
        if fault is not None:
            return (f"{host._name(self.plugin_id)} was switched off for this "
                    f"session ({fault.reason})")
        if self._gone:
            return (f"{host._name(self.plugin_id)} no longer runs in this "
                    f"window (switched off, or its project closed)")
        return ""

    def _open(self) -> None:
        why = self._off()
        if why:
            raise pr.PluginFileError(f"{why}: its file services are closed")

    def _own_slot(self) -> Any:
        rig = self._host.rig
        if rig is None:
            return None
        if self._slot_id is not None:
            for slot in rig:
                if slot.id == self._slot_id:
                    return slot
            return None
        return getattr(rig, "active", None)

    def _slot_for(self, camera: Optional[str]) -> Any:
        return self._own_slot() if camera is None else self._host._slot(camera)

    def say(self, text: str) -> None:
        self._host.say(self.plugin_id, text)

    def call_in_ui(self, fn: Callable[[], None]) -> None:
        self._host.queue_ui(self.plugin_id, fn)

    def cameras(self) -> Mapping[str, api.CameraHandle]:
        return self._host.camera_handles()

    def active_camera(self) -> Optional[str]:
        slot = getattr(self._host.rig, "active", None)
        return getattr(slot, "name", None)

    def capture_folder(self, camera: Optional[str] = None) -> Optional[Path]:
        folder = getattr(self._slot_for(camera), "folder", None)
        return Path(folder) if folder else None

    def current_frame(self) -> Optional[Path]:
        fn = self._host.current_frame_fn
        return fn() if fn is not None else None

    def crop_box(self, camera: Optional[str] = None
                 ) -> Optional[api.CropBox]:
        fn = self._host.crop_box_fn
        return fn(camera) if fn is not None else None

    def on(self, event: str, fn: Callable[[Mapping[str, Any]], Any]
           ) -> Callable[[], None]:
        if self._off():
            if event not in api.EVENTS:
                raise ValueError(f"{event!r} is not an event a plugin can "
                                 f"listen for ({', '.join(api.EVENTS)})")
            return lambda: None             # a plugin switched off hears none
        return self._host.subscribe(self.plugin_id, event, fn)

    def settings(self) -> Dict[str, Any]:
        return self._host.settings(self.plugin_id)

    def save_settings(self, values: Mapping[str, Any]) -> None:
        why = self._off()
        if why:
            raise pl.PluginListError(f"{why}: it changes nothing in the "
                                     f"project now")
        self._host.save_settings(self.plugin_id, values)

    def state(self, camera: Optional[str] = None) -> Dict[str, Any]:
        slot = self._slot_for(camera)
        if slot is None:
            raise RuntimeError(f"no camera named {camera!r}")
        return slot.plugin_state.setdefault(self.plugin_id, {})

    def temp_dir(self) -> Path:
        return self._host.temp_dir(self.plugin_id)

    def create_file(self, folder: Any, name: str, *,
                    binary: bool = False) -> Any:
        self._open()
        return self._host.create_file(self.plugin_id, folder, name,
                                      binary=binary)

    def write_new(self, folder: Any, name: str, data: Any) -> Path:
        self._open()
        return self._host.write_new(self.plugin_id, folder, name, data)

    def data_dir(self) -> Path:
        self._open()
        return self._host.data_dir(self.plugin_id)

    def capture_setup(self, camera: Optional[str] = None) -> Any:
        """A copy of the camera's capture set-up (device.capture, the
        storage today's code reads, design 2.5), or None."""
        from dataclasses import replace

        setup = getattr(getattr(self._slot_for(camera), "device", None),
                        "capture", None)
        return replace(setup) if setup is not None else None

    def __repr__(self) -> str:
        return f"<plugin context {self.plugin_id}>"


# ======================================================================
# The recording path
# ======================================================================
def end_recording(session: Any, reason: str, frame: Any = None) -> bool:
    """End `session`'s recording now, for `reason` (capture.FrameWriter.
    end_recording); False when it was not recording or had already ended."""
    writer = getattr(session, "writer", None)
    end = getattr(writer, "end_recording", None)
    if not callable(end):
        return False
    return bool(end(reason, frame))


class GuardedGate:
    """A plugin's record gate for ONE run, guarded on the RECORDING PATH
    (design 3.6, 3.10). It is what the session is handed as `gate=`:
    `tag(frame)` and the gate's other calls (saved, failed_grab,
    confirm_saves) go to the plugin's gate, and anything else of the gate
    (its summary, its thread) is reached through unchanged.

    ITS RUN'S WRITER. CaptureSession.record_to hands the gate the run's
    FrameWriter (bind_writer) before it calls anything of it; failing that,
    the gate takes the session's writer at the first frame it tags, which
    is the same step (the session holds its lock across both). A fault ends
    THAT writer's recording — never a later run's, however late it comes:
    a fault after its run's writer has closed is logged, the plugin is
    switched off, and nothing else changes.

    IT IS HANDED A FRAME IT CANNOT CHANGE (P3 row 6): `tag` gets the
    frame's index and time stamp, its pixels behind a read-only buffer and
    its meta read-only (read_only_frame) — a gate reads a frame and gives
    its tags, which the session puts on the frame itself; a gate that
    writes into a frame fails, a fault like any other below.

    WHEN IT FAILS — any exception, on any thread, KeyboardInterrupt
    included — the gate is marked ended FIRST, so the grab thread asks the
    plugin nothing more from that moment; then its writer's recording ends;
    then the host switches the plugin off, logs and says it. The frame at a
    tag fault is still saved, marked "error: plugin fault", as today's
    capture marks a gate that raises (design 3.6); nothing after it is
    taken (3.10), and <run>_run_end.json gives the reason.

    WHEN ITS PLUGIN IS SWITCHED OFF ELSEWHERE. A fault in a best-effort
    hook (status, a panel, a listener, on_run_started) never stops a
    capture (3.10): the gate serves out the run in progress, so the run
    stays tagged whole; the plugin gets no gate in the next run. A hang
    (the watchdog), a fault on the recording path (another camera's gate of
    the same plugin), a failed load, or a fault the plugin already had when
    this gate was made end the run at the next frame instead, without
    asking the plugin anything: a hung plugin may hold what its gate
    needs."""

    #: The gate's calls on the recording path besides `tag`.
    RECORDING_CALLS = ("saved", "failed_grab", "confirm_saves")
    #: The faults of its plugin that stop a gate during its run.
    STOPPING = (RECORDING, HANG, LOAD)

    def __init__(self, host: Optional[PluginHost], pid: str, gate: Any,
                 session: Any = None) -> None:
        self._host = host
        self._pid = pid
        self._gate = gate
        self._session = session
        self._lock = threading.Lock()
        #: The FrameWriter of this gate's run, once known.
        self._writer: Any = None
        self._writer_ended = False
        #: The plugin was already switched off when this gate was made.
        self._off_from_start = host is not None and pid in host.faults
        #: Why the recording was ended by this gate, or "".
        self.ended = ""

    @property
    def gate(self) -> Any:
        return self._gate

    # -- its run's writer ----------------------------------------------------
    def bind_writer(self, writer: Any) -> None:
        """The FrameWriter of this gate's run (CaptureSession.record_to,
        before confirm_saves). An end already due — the plugin was off
        before the run began — happens now, before any frame."""
        with self._lock:
            if self._writer is None:
                self._writer = writer
        if self.ended:
            self._end_writer(self.ended)
        else:
            self._stop_if_off()

    def _from_session(self) -> None:
        """No writer handed over: the session's, taken on the grab thread
        in the step that tags a frame (the session's lock is held)."""
        if self._writer is None and self._session is not None:
            with self._lock:
                if self._writer is None:
                    self._writer = getattr(self._session, "writer", None)

    def _end_writer(self, reason: str, frame: Any = None) -> None:
        """End this run's recording (once). With no writer known yet, it
        ends when one is (bind_writer, or the next frame)."""
        with self._lock:
            writer = self._writer
            if writer is None or self._writer_ended:
                return
            self._writer_ended = True
        end = getattr(writer, "end_recording", None)
        if callable(end):
            try:
                end(reason, frame)
            except Exception:                             # noqa: BLE001
                pass

    # -- endings ---------------------------------------------------------------
    def _off(self) -> Optional[Fault]:
        """The plugin's fault when it stops this gate (see the class)."""
        host = self._host
        fault = host.faults.get(self._pid) if host is not None else None
        if fault is None:
            return None
        if self._off_from_start or fault.kind in self.STOPPING:
            return fault
        return None

    def _stop_if_off(self, frame: Any = None) -> bool:
        fault = self._off()
        if fault is None:
            return False
        name = self._host._name(self._pid)
        self._finish(f"recording ended: plugin {name} ({self._pid}) was "
                     f"switched off — {fault.reason}", frame)
        return True

    def _finish(self, reason: str, frame: Any = None) -> None:
        with self._lock:
            if self.ended:
                return
            self.ended = reason
        self._end_writer(reason, frame)

    def end(self, reason: str) -> None:
        """End this gate's run's recording now, for `reason` (a fault of its
        plugin's elsewhere on the recording path: its trigger source that
        did not start, TriggerSlot); the grab thread asks the plugin nothing
        more."""
        self._finish(str(reason))

    def _fail(self, hook: str, exc: BaseException, frame: Any = None) -> None:
        """The plugin's gate raised: ended first, then the writer, then the
        host's fault (which writes the log and says it)."""
        host = self._host
        reason = (host.recording_reason(self._pid, hook, exc, frame)
                  if host is not None else
                  f"recording ended: the record gate's {hook} failed — "
                  f"{fault_text(exc)}")
        self._finish(reason, frame)
        if host is not None:
            host.fault(self._pid, hook, exc, RECORDING)

    # -- the calls -------------------------------------------------------------
    def tag(self, frame: Any) -> Any:
        self._from_session()
        if self.ended:
            self._end_writer(self.ended, frame)     # if it was waiting
            return dict(FAULT_TAGS)
        if self._stop_if_off(frame):
            return dict(FAULT_TAGS)
        try:
            # A frame it can read and not change (P3 row 6): its pixels and
            # meta are what is saved and recorded. ITS TAGS CHECKED (P3
            # review): None, or the trigger columns with plain values — junk
            # is a fault on the recording path like any other, so it never
            # reaches the frame's meta (a str there stopped the grab thread,
            # a "reduce" changed the pictures saved).
            return gate_tags(self._gate.tag(read_only_frame(frame)))
        except BaseException as exc:                    # noqa: BLE001
            self._fail("record gate", exc, frame)
            return dict(FAULT_TAGS)

    def _summary(self, target: Callable[[], Any]) -> Dict[str, Any]:
        """The run's capture block for its end record (the books'
        summary(), which Recorder.finish asks on a writer thread as the
        recording ends): GUARDED and CLEANED — whatever it raises,
        SystemExit included, is its plugin's best-effort fault and the
        block says so; what it gives is made plain JSON (NaN as null), so
        the end record is always written (P3 review: a SystemExit there,
        or a NaN, left the run with no end record)."""
        try:
            got = target()
            if not isinstance(got, Mapping):
                raise TypeError(f"its summary is a {type(got).__name__}, "
                                f"not a mapping")
            made = camera_record._plain_tree(dict(
                dict.items(got) if isinstance(got, dict) else got.items()))
            return made if isinstance(made, dict) else {}
        except BaseException as exc:                    # noqa: BLE001
            if self._host is not None:
                self._host.fault(self._pid, "record gate (summary)", exc)
            return {"error": fault_text(exc)}

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            # Its own fields are never forwarded (and asking for one before
            # __init__ set it must not recurse).
            raise AttributeError(name)
        if name == "summary":
            # What the end record's capture block is made of (Recorder.
            # capture_info): guarded and cleaned (_summary), looked up in
            # the guard too.
            try:
                found = getattr(self._gate, name)
            except AttributeError:
                raise
            except BaseException as exc:                # noqa: BLE001
                failed = exc

                def found() -> Any:                     # its fault, below
                    raise failed
            if not callable(found):
                return found

            def summary() -> Dict[str, Any]:
                return self._summary(found)
            self.__dict__[name] = summary
            return summary
        if name not in self.RECORDING_CALLS:
            return getattr(self._gate, name)
        try:
            target = getattr(self._gate, name)
        except AttributeError:
            raise
        except BaseException as exc:                    # noqa: BLE001
            # Looking it up ran plugin code that failed: a fault on the
            # recording path like any other.
            self._fail(f"record gate ({name})", exc)
            return lambda *args, **kwargs: None
        if not callable(target):
            return target

        def guarded(*args: Any, **kwargs: Any) -> Any:
            if name == "failed_grab":
                self._from_session()                # the grab thread's step
            if self.ended or self._stop_if_off():
                return None
            try:
                return target(*args, **kwargs)
            except BaseException as exc:                # noqa: BLE001
                self._fail(f"record gate ({name})", exc)
                return None
        self.__dict__[name] = guarded
        return guarded


def read_only(meta: Any) -> Mapping[str, Any]:
    """A frame's meta as a plugin on the recording path is handed it: a
    read-only view of the frame's own dict (nothing copied), so a write
    raises — the frames CSV row is made from that dict after the picture is
    saved (capture.Recorder.write)."""
    if isinstance(meta, MappingProxyType):
        return meta
    if isinstance(meta, dict):
        return MappingProxyType(meta)
    return MappingProxyType(dict(meta or {}))


#: The dtypes a saved picture has (design 3.11: uint8 or uint16).
PICTURE_DTYPES = ("uint8", "uint16")


def picture_problem(picture: Any) -> str:
    """Why `picture` is not a picture a recording can save — a numpy array,
    uint8 or uint16, (h, w) or (h, w, 3), not empty — or ""."""
    shape = getattr(picture, "shape", None)
    dtype = getattr(picture, "dtype", None)
    if shape is None or dtype is None or not hasattr(picture, "ndim"):
        return f"{type(picture).__name__}, not a picture (a numpy array)"
    if str(dtype) not in PICTURE_DTYPES:
        return (f"a {dtype} array, not a picture: a picture is uint8 or "
                f"uint16")
    if not (len(shape) == 2 or (len(shape) == 3 and shape[2] == 3)):
        return (f"an array of shape {tuple(shape)}, not a picture: (h, w) or "
                f"(h, w, 3)")
    if not all(int(n) > 0 for n in shape):
        return f"an empty picture, of shape {tuple(shape)}"
    return ""


_FRAME_TYPE: Any = None


def read_only_frame(frame: Any) -> Any:
    """`frame` as a record gate is handed it (design 3.6, 3.8, 10.3): its
    index and time stamp, its pixels behind a read-only buffer (locked_
    pixels) and its meta read-only (read_only) — a gate reads a frame and
    gives its tags; what is saved is never its to change. Only the small
    Frame is new: nothing of the picture is copied."""
    global _FRAME_TYPE
    if _FRAME_TYPE is None:
        from .cameras import Frame
        _FRAME_TYPE = Frame
    if not isinstance(frame, _FRAME_TYPE):
        return frame
    return _FRAME_TYPE(image=locked_pixels(frame.image), index=frame.index,
                       timestamp_us=frame.timestamp_us,
                       meta=read_only(frame.meta))


class _Watched:
    """A call on the recording path, in the host's busy table while it runs
    (as _guard puts every other call), so the watchdog switches off one
    that hangs (P3 review). Nothing without a host."""

    __slots__ = ("_host", "_key", "_hook", "_before")

    def __init__(self, host: Optional[PluginHost], pid: str,
                 hook: str) -> None:
        self._host = host
        self._key = (pid, threading.get_ident())
        self._hook = hook
        self._before: Any = None

    def __enter__(self) -> "_Watched":
        host = self._host
        if host is not None:
            self._before = host._busy.get(self._key)
            host._busy[self._key] = (self._hook, host._clock())
        return self

    def __exit__(self, *exc: Any) -> bool:
        host = self._host
        if host is not None:
            if self._before is None:
                host._busy.pop(self._key, None)
            else:
                host._busy[self._key] = self._before
        return False


class GuardedStage:
    """A plugin's FRAME STAGE for one run (`stage(image, meta) -> image`,
    the picture to save), guarded on the RECORDING PATH (design 3.8, 3.10):
    what the run's capture.Recorder is given as `frame_stage`. It runs on
    the writer threads.

    IT IS HANDED READ-ONLY ONES (P3 row 6): the picture behind a read-only
    buffer (locked_pixels: the array may be the frame's own, which the live
    view and the frame's other readers share) and the meta as a read-only
    view (read_only: its CSV row is made from it after the save), so a
    stage that writes into either fails — a fault, below — instead of
    changing what is shown or recorded. What it gives back must be a
    picture (picture_problem), or that is its fault too.

    WHEN IT FAILS — any exception, SystemExit and KeyboardInterrupt
    included — the frame it was making is not saved, the recording ends
    there as at a failed write (the frames queued behind it are not saved
    either: they were not yet made, and saving them as they came would
    leave half a run staged and half not), the end record's
    recording_failed names the plugin, and the plugin is switched off.

    WHEN ITS PLUGIN IS SWITCHED OFF ELSEWHERE, the rule is the record
    gate's (GuardedGate): a best-effort fault lets the stage serve out the
    run; a hang, a fault on the recording path, a failed load, or a fault it
    had before this run began end the run at the next frame, without
    asking the plugin anything.

    ONE PICTURE PER RUN (P3 review): every picture it gives in a run must
    have the size, the dtype and the colour mode of its first one, or it
    fails as above; before, a stage that cropped or changed dtype mid-run
    left one run holding pictures of two sizes and two modes, while its
    records described one.

    WATCHED (P3 review): each call is in the host's busy table, so the
    watchdog switches off a stage that has not returned in
    WATCHDOG_SECONDS — said, logged and in the end record's plugin_faults
    — and the recording ends at the next frame staged, its reason naming
    the plugin; the frame of the hung call is not saved when the call
    comes back (it is counted as not saved). A stage that never returns
    still holds its run's end record (the frames CSV is published in grab
    order), so a built-in frame stage must return well within the
    watchdog's limit: the frame-stage contract suite times each one on a
    full-size frame."""

    STOPPING = GuardedGate.STOPPING

    def __init__(self, host: Optional[PluginHost], pid: str,
                 stage: Callable[[Any, Any], Any], session: Any = None
                 ) -> None:
        self._host = host
        self._pid = pid
        self._stage = stage
        self._session = session
        self._lock = threading.Lock()
        self._off_from_start = host is not None and pid in host.faults
        #: Why the recording was ended by this stage, or "".
        self.ended = ""
        #: (shape, dtype) of the first picture it gave this run.
        self._picture: Optional[Tuple[Tuple[int, ...], str]] = None

    def _finish(self, reason: str) -> str:
        with self._lock:
            if not self.ended:
                self.ended = reason
            return self.ended

    def __call__(self, image: Any, meta: Any) -> Any:
        if self.ended:
            raise RecordingEnded(self.ended)
        host = self._host
        fault = host.faults.get(self._pid) if host is not None else None
        if fault is not None and (self._off_from_start
                                  or fault.kind in self.STOPPING):
            name = host._name(self._pid)
            raise RecordingEnded(self._finish(
                f"recording ended: plugin {name} ({self._pid}) was switched "
                f"off — {fault.reason}"))
        try:
            with _Watched(host, self._pid, "frame stage"):
                made = self._stage(locked_pixels(image), read_only(meta))
            problem = picture_problem(made)
            if problem:
                raise TypeError(f"it gave {problem}")
            kind = (tuple(int(n) for n in made.shape), str(made.dtype))
            with self._lock:
                first = self._picture
                if first is None:
                    self._picture = first = kind
            if kind != first:
                raise TypeError(
                    f"it gave a picture of shape {kind[0]}, {kind[1]}, after "
                    f"ones of shape {first[0]}, {first[1]} in this run: one "
                    f"run holds pictures of one size, dtype and colour "
                    f"mode")
        except BaseException as exc:                    # noqa: BLE001
            reason = (host.recording_reason(self._pid, "frame stage", exc)
                      if host is not None else
                      f"recording ended: the frame stage failed — "
                      f"{fault_text(exc)}")
            reason = self._finish(reason)
            if host is not None:
                host.fault(self._pid, "frame stage", exc, RECORDING)
            raise RecordingEnded(reason) from exc
        fault = host.faults.get(self._pid) if host is not None else None
        if fault is not None and fault.kind in self.STOPPING:
            # Switched off while this call ran (the watchdog: a hang): the
            # picture it made late is not saved; the recording has ended.
            name = host._name(self._pid)
            raise RecordingEnded(self._finish(
                f"recording ended: plugin {name} ({self._pid}) was switched "
                f"off — {fault.reason}"))
        return made


class StagedAfterDownsample:
    """What a run's recorder saves through when built-in plugins give
    frame stages (PluginHost.frame_stages): the core's own, Downsample
    (cameras.frame_stage, looked up at each frame), then each plugin's
    guarded stage on the picture Downsample made, in the project list's
    order. Downsample is never skipped: a plugin's stage comes after it,
    so the records' Downsample is always what was done (design 2.1)."""

    def __init__(self, stages: Sequence[GuardedStage]) -> None:
        self.stages = tuple(stages)

    def __call__(self, image: Any, meta: Any) -> Any:
        from . import cameras

        picture = cameras.frame_stage(image, meta)
        for stage in self.stages:
            picture = stage(picture, meta)
        return picture


class TriggerSlot:
    """The triggering plugin's part of ONE run on one camera, as the core's
    Start and Stop call it (design 3.6: the core owns the order, the
    triggering plugin fills the slot in it). Each step is the plugin's
    `trigger_slot(camera)` object's method of that name (typhon.trigger's
    RunTriggers), called here under the rule for that step:

      BEFORE THE RECORDING BEGINS — arm, gate: what they raise refuses this
        Start, in the camera's words (today's Start refused an arming or a
        triggers CSV it could not make); the core undoes what was armed.
        record (the camera record's capture block) and words (Start's
        summary) are best effort: a fault is said and logged, the plugin
        switched off, and the core writes today's block itself.
      ONCE IT HAS BEGUN — ready, start: the recording path (design 3.10).
        A fault ends that run's recording now with its reason, naming the
        plugin (its gate's writer), and switches it off; the words for
        Start's summary are returned.
      STOP, AND EVERYTHING AFTER IT — halt, close, disarm, abort, undo,
        capture_now: SERVED OUT. Called even when the plugin was switched
        off during the run (a best-effort fault elsewhere: design 3.10's
        "never stop a capture"), so its books are closed and the camera's
        trigger put back — a camera is never left armed because a plugin's
        status line failed. A fault is logged and said, never raised.

    Not watched by the watchdog: these wait on a camera (arming, a burst
    stopping, the last frames landing), as camera calls do."""

    def __init__(self, host: PluginHost, pid: str, plugin: Any, inner: Any,
                 camera: str) -> None:
        self._host = host
        self.plugin_id = pid
        self._plugin = plugin
        self._inner = inner
        self.camera = camera
        #: The run's guarded record gate, once made (make_gate).
        self.gate: Optional[GuardedGate] = None
        #: What went wrong at Stop, for Stop's line.
        self.notes: List[str] = []
        self.over = False
        #: Runs whose plugins' files are closed when this slot's run is
        #: over (run_stopped: a recording that ended before Stop).
        self.closes: List[pr.ActiveRun] = []

    @property
    def inner(self) -> Any:
        return self._inner

    def _name(self) -> str:
        return self._host._name(self.plugin_id)

    def _step(self, step: str) -> Callable[..., Any]:
        fn = getattr(self._inner, step, None)
        if not callable(fn):
            raise AttributeError(f"the triggering plugin's slot has no "
                                 f"{step}()")
        return fn

    # -- before the recording begins -----------------------------------------
    def arm(self, setup: Any) -> Any:
        """The camera armed for an exact run; what Stop puts back. Raises
        the refusal (frame_camera undoes the Start)."""
        return self._step("arm")(setup)

    def make_gate(self, run: str, folder: Any, setup: Any,
                  values: Mapping[str, Any], rate: float,
                  session: Any = None) -> GuardedGate:
        """The run's books, as the record gate for `session` — guarded on
        the recording path (P1's GuardedGate). Raises what stopped them
        being made (an OSError: the triggers CSV)."""
        books = self._step("gate")(run, folder, setup, values, rate)
        self.gate = self._host.record_gate(self.plugin_id, books, session)
        return self.gate

    def record(self, run: str, setup: Any, armed: Any) -> Any:
        """The camera record's capture block, or None (the core writes
        today's itself)."""
        return self._best("record", run, setup, armed, default=None)

    def words(self, setup: Any, armed: Any) -> str:
        """What Start says of the trigger set-up, or ""."""
        said = self._best("words", setup, armed, default="")
        return said if isinstance(said, str) else ""

    def _best(self, step: str, *args: Any, default: Any = None) -> Any:
        try:
            return self._step(step)(*args)
        except BaseException as exc:                    # noqa: BLE001
            if not _ours(exc):
                raise
            self._host.fault(self.plugin_id, f"trigger {step}", exc)
            return default

    # -- once the recording has begun: the recording path --------------------
    def ready(self) -> List[str]:
        """The stream runs: the camera's edge count from its first ready."""
        return self._recording("ready")

    def start(self) -> List[str]:
        """The run's source starts (Software, Timer)."""
        return self._recording("start")

    def _recording(self, step: str) -> List[str]:
        try:
            self._step(step)()
            return []
        except BaseException as exc:                    # noqa: BLE001
            if not _ours(exc):
                raise
            host = self._host
            reason = host.recording_reason(self.plugin_id, f"trigger {step}",
                                           exc)
            if self.gate is not None:
                self.gate.end(reason)
            host.fault(self.plugin_id, f"trigger {step}", exc, RECORDING)
            return [f"{self._name()} failed as the run started, and the "
                    f"recording ended — {fault_text(exc)}"]

    # -- Stop, and after: served out -----------------------------------------
    def _serve(self, step: str, *args: Any, default: Any = None) -> Any:
        try:
            return self._step(step)(*args)
        except BaseException as exc:                    # noqa: BLE001
            if not _ours(exc):
                raise
            self._host.fault(self.plugin_id, f"trigger {step}", exc)
            self.notes.append(f"{self._name()} failed in its {step} at "
                              f"Stop — {fault_text(exc)}")
            return default

    def halt(self, grabbed: Callable[[], int]) -> None:
        """Stop the source; give the last frames their moment; an exact run
        from a camera line closes the input before the stream stops."""
        self._serve("halt", grabbed)

    def close(self) -> None:
        """Close the run's books before its recording ends."""
        self._serve("close")

    def disarm(self) -> str:
        """Every trigger back as before Start, after the recording ended;
        what was NOT put back (and any failure at Stop), or ""."""
        said = self._serve("disarm", default="")
        self._finished()
        notes = [str(said)] if said else []
        return "; ".join(notes + self.notes)

    def abort(self) -> None:
        """Disconnect in the middle of the run: the books stopped and the
        camera's triggers put back."""
        self._serve("abort")
        self._finished()

    def undo(self) -> None:
        """A Start that failed after the run's name was reserved: what was
        armed put back, the triggers CSV closed."""
        self._serve("undo")
        self._finished()

    def capture_now(self) -> str:
        """Capture now during the run (the plugin's capture_now(camera)),
        served out; its words."""
        try:
            fn = getattr(self._plugin, "capture_now", None)
            said = fn(self.camera) if callable(fn) else None
        except BaseException as exc:                    # noqa: BLE001
            if not _ours(exc):
                raise
            self._host.fault(self.plugin_id, "capture_now", exc)
            return (f"Capture now: {self._name()} failed — "
                    f"{fault_text(exc)}.")
        return str(said or "")

    def _finished(self) -> None:
        self.over = True
        self._host._trigger_slot_over(self)
        closes, self.closes = list(self.closes), []
        for active in closes:
            active.close_files()

    def __repr__(self) -> str:
        return f"<trigger slot {self.plugin_id} on {self.camera}>"


# ======================================================================
# Live frames
# ======================================================================
class LiveFeed:
    """Frames to one plugin's `on_frame`, on the plugin's own thread
    (design 3.8). The grab thread's part, `offer`, only puts the frame in a
    two-slot queue — the oldest thrown away and counted — and wakes the
    thread; nothing is copied. The plugin sees a read-only FrameView.

    `duty`, when set, caps the plugin's busy share of the time: after a
    call of t seconds the thread rests t * (1/duty - 1), and frames offered
    meanwhile are dropped (counted). `budget_ms` is the time a call should
    take; calls over it are counted, not stopped (the watchdog stops a
    hang)."""

    def __init__(self, host: PluginHost, plugin: str,
                 fn: Callable[[api.FrameView], Any], *, camera: str = "",
                 budget_ms: float = pm.DEFAULT_BUDGET_MS,
                 duty: Optional[float] = None) -> None:
        self.host = host
        self.plugin = plugin
        self.camera = camera
        self.budget_s = float(budget_ms) / 1000.0
        self.duty = duty if duty is None else max(0.01, min(1.0, float(duty)))
        self._fn = fn
        self._queue: Deque[Tuple[Any, int, Optional[str]]] = collections.deque(
            maxlen=FEED_SLOTS)
        self._cond = threading.Condition()
        self._stopping = False
        self._thread: Optional[threading.Thread] = None
        self.offered = 0
        self.dropped = 0
        self.delivered = 0
        self.over_budget = 0
        self.busy_s = 0.0
        self.longest_s = 0.0
        #: The camera's own binning, decimation and mirroring (design 3.12;
        #: PluginHost.camera_geometry), read once per stream: they are
        #: settings the stream must be stopped to change (GenICam locks
        #: them while grabbing, as the simulated camera's ReverseX).
        self._geometry: Optional[Dict[str, Any]] = None
        self._geometry_of: Any = None

    def geometry(self) -> Dict[str, Any]:
        """The camera's own terms for this feed's frames, read again when
        its stream has started anew since they were last read."""
        stream = self.host.stream_of(self.camera)
        if self._geometry is None or self._geometry_of is not stream:
            self._geometry = self.host.camera_geometry(self.camera)
            self._geometry_of = stream
        return self._geometry

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"plugin-feed-{self.plugin}")
        self._thread.start()

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def offer(self, frame: Any, saved: Optional[str] = None) -> None:
        """The grab thread's part: queue `frame`, drop the oldest if full.
        `saved`: the file the frame is saved as, or None for a frame not
        saved (FrameView.saved)."""
        if self._stopping:
            return
        host_us = time.perf_counter_ns() // 1000
        with self._cond:
            self.offered += 1
            if len(self._queue) == FEED_SLOTS:
                self.dropped += 1
            self._queue.append((frame, host_us, saved))
            self._cond.notify()

    def stop(self, wait: float = 1.0) -> None:
        """No more frames; wait up to `wait` s for the thread to end (a
        plugin stuck in a call may outlive it: it is a daemon thread)."""
        with self._cond:
            self._stopping = True
            dropped = len(self._queue)
            self._queue.clear()
            self.dropped += dropped
            self._cond.notify_all()
        thread = self._thread
        if wait > 0 and thread is not None and thread is not \
                threading.current_thread():
            thread.join(wait)

    def _run(self) -> None:
        host = self.host
        while True:
            with self._cond:
                while not self._queue and not self._stopping:
                    self._cond.wait()
                if self._stopping:
                    return
                frame, host_us, saved = self._queue.popleft()
            if self.plugin in host.faults:
                return
            try:
                terms = self.geometry()
            except Exception:                             # noqa: BLE001
                terms = {}      # unknown: a FrameView then refuses to_sensor
            view = frame_view(frame, self.camera, host_us, saved=saved,
                              binning=terms.get("binning"),
                              decimation=terms.get("decimation"),
                              mirror=terms.get("mirror"))
            del frame
            started = time.perf_counter()
            host._guard(self.plugin, "on_frame", self._fn, (view,), {}, None)
            took = time.perf_counter() - started
            del view
            self.delivered += 1
            self.busy_s += took
            self.longest_s = max(self.longest_s, took)
            if took > self.budget_s:
                self.over_budget += 1
            if self.duty is not None and self.duty < 1.0 and took > 0:
                # Rest: the grab thread has Python's lock to itself. Frames
                # offered meanwhile wait in the two slots, the oldest
                # dropped and counted as ever.
                rest = took * (1.0 / self.duty - 1.0)
                with self._cond:
                    if self._cond.wait_for(lambda: self._stopping, rest):
                        return

    def stats(self) -> Dict[str, Any]:
        return {"offered": self.offered, "delivered": self.delivered,
                "dropped": self.dropped, "over_budget": self.over_budget,
                "busy_s": round(self.busy_s, 4),
                "longest_ms": round(self.longest_s * 1000.0, 3)}


def locked_pixels(image: Any) -> Any:
    """`image` as an array a plugin cannot make writeable by accident, with
    nothing copied (design 3.8, 10.3).

    A plain read-only view is not enough: NumPy lets a view be made
    writeable again whenever the array it was made from is writeable, and a
    frame's own array is (the core writes into it, and the pool reuses it).
    Measured in the P1 review: `view.flags.writeable = True` then a write
    changed the pixels the writer saved moments later. An array over a
    READ-ONLY memoryview of the frame's pixels refuses both
    `flags.writeable = True` and `setflags(write=True)`, because what it
    was made from cannot be written. A plugin that reaches through
    `.base.obj` to the frame's own array on purpose is not stopped; design
    10.3 lists that among what a plugin in Typhon's process can still do.
    The frame's own array is left as it is, so the core is unchanged.

    The memoryview holds the frame's array, so the frame pool does not
    reuse it while a plugin holds the view (cameras.FramePool). A dtype the
    buffer protocol cannot carry gets the plain read-only view."""
    if image is None or not hasattr(image, "view"):
        return image
    try:
        import numpy as np

        locked = np.asarray(memoryview(image).toreadonly())
        if locked.shape == image.shape and locked.dtype == image.dtype:
            return locked
    except (TypeError, ValueError, BufferError, ImportError):
        pass
    view = image.view()
    try:
        view.flags.writeable = False
    except (AttributeError, ValueError):
        pass
    return view


class PluginBackend:
    """A camera plugin as today's cameras.Backend (`name`, `kind`,
    `install_hint`, `available`, `discover`, `open`) — what cameras.discover
    and cameras.open_camera take as `known`, so the scan and the open that
    stay in council_core run on top of the registry (design 2.4 item 3).
    Every call goes through the host's camera guard (PluginHost.
    camera_call): a camera's refusal is its own, any other error the
    plugin's."""

    def __init__(self, host: PluginHost, pid: str) -> None:
        self.host = host
        self.plugin = pid
        #: What to install, as the last `available()` said.
        self.install_hint = ""
        #: What the last `discover()` left out, and why (the scan's notes).
        self.dropped: List[str] = []

    @property
    def name(self) -> str:
        return self.host.camera_backend(self.plugin) or self.plugin

    @property
    def kind(self) -> str:
        """"event" for a plugin whose cameras can only be event cameras,
        else "frame" (Backend.kind)."""
        m = self.host.manifest(self.plugin)
        caps = set(m.capabilities) if m is not None else set()
        return "event" if "events" in caps and "frames" not in caps \
            else "frame"

    def available(self) -> bool:
        ok, hint = self.host.camera_available(self.plugin)
        self.install_hint = hint
        return ok

    def discover(self) -> List[Any]:
        """The cameras the plugin lists — only those of its own family: a
        CameraInfo carrying its backend, a key, and the kind "frame" or
        "event". Any other is left out, and why is kept in `dropped` for the
        scan's notes: before the P2 review a plugin could list a camera
        under another family's backend, which open_camera then sent to that
        family's plugin, its presets keyed as that family's."""
        from .cameras import CameraInfo

        listed = list(self.host.camera_call(self.plugin, "discover") or [])
        own = self.name
        who = self.host._name(self.plugin)
        kept: List[Any] = []
        self.dropped = []
        for info in listed:
            if not isinstance(info, CameraInfo):
                why = (f"it listed {type(info).__name__} {str(info)[:40]!r}, "
                       f"which is not a camera (CameraInfo)")
            elif info.backend != own:
                why = (f"{info.label} carries the {info.backend!r} backend; a "
                       f"camera plugin lists only cameras of its own family "
                       f"({own!r})")
            elif not info.key:
                why = f"{info.label} has no key to be opened by"
            elif info.kind not in ("frame", "event"):
                why = (f"{info.label} is a {info.kind!r} camera; a camera is "
                       f"a frame or an event camera")
            else:
                kept.append(info)
                continue
            self.dropped.append(f"{who}: a camera is left out — {why}")
        return kept

    def open(self, info: Any) -> Any:
        return self.host.camera_call(self.plugin, "open", info)

    def __repr__(self) -> str:
        return f"<PluginBackend {self.plugin}>"


#: frame_view's "not given": the frame's own terms are taken as plain (no
#: binning, decimation or mirroring) — what a caller that knows them says.
#: None, given, is "not known": the FrameView keeps None and to_sensor
#: refuses (design 3.12: a position is never guessed).
NOT_GIVEN: Any = object()


def geometry_pair(term: str, values: Sequence[Any]) -> Optional[Tuple[Any,
                                                                      Any]]:
    """A camera's two settings of `term` (GEOMETRY) as a FrameView term: a
    pair of whole numbers (binning, decimation, at least 1) or of truths
    (mirror) — or None when either is unknown or not a value of that
    kind."""
    if len(values) != 2 or any(v is None for v in values):
        return None
    if term == "mirror":
        out = []
        for value in values:
            if isinstance(value, bool):
                out.append(value)
            elif isinstance(value, (int, float)):
                out.append(bool(value))
            elif str(value).strip().lower() in ("true", "on", "1", "yes"):
                out.append(True)
            elif str(value).strip().lower() in ("false", "off", "0", "no"):
                out.append(False)
            else:
                return None
        return (out[0], out[1])
    try:
        made = tuple(max(1, int(v)) for v in values)
    except (TypeError, ValueError):
        return None
    return (made[0], made[1])


def frame_view(frame: Any, camera: str, host_us: int = 0,
               saved: Optional[str] = None, binning: Any = NOT_GIVEN,
               decimation: Any = NOT_GIVEN, mirror: Any = NOT_GIVEN
               ) -> api.FrameView:
    """A FrameView of `frame` whose pixels the plugin cannot unlock by
    accident (locked_pixels; nothing is copied), with every term of design
    3.12 that turns its pixels into sensor pixels: the camera's area the
    frame was taken with (Frame.meta "aoi") and Downsample's factor and
    method (its "downsample", "downsample_method", "reduce"), as the frame
    carries them; the camera's own `binning`, `decimation` and `mirror` —
    a live feed reads them from the camera's settings (PluginHost.
    camera_geometry) — each a pair, None when it could not be told (the
    view then refuses to_sensor), or plain when not given. With the
    camera's binning as the Downsample, the binning IS the factor.
    `saved`: the file the frame is saved as, or None."""
    view = locked_pixels(getattr(frame, "image", None))
    meta = dict(getattr(frame, "meta", None) or {})

    def term(value: Any, name: str, plain: Tuple[Any, Any]
             ) -> Optional[Tuple[Any, Any]]:
        if value is NOT_GIVEN:
            return plain
        if value is None:
            return None
        if isinstance(value, (tuple, list)):
            return geometry_pair(name, list(value))
        return geometry_pair(name, [value, value])

    try:
        area = tuple(int(v) for v in (meta.get("aoi") or (0, 0, 0, 0)))
    except (TypeError, ValueError):
        area = (0, 0, 0, 0)
    if len(area) != 4:
        area = (0, 0, 0, 0)
    try:
        factor = max(1, int(meta.get("downsample") or meta.get("reduce")
                            or 1))
    except (TypeError, ValueError):
        factor = 1
    method = str(meta.get("downsample_method") or "")
    if not method and meta.get("reduce"):
        method = api.TYPHON_DOWNSAMPLE
    if factor <= 1:
        method = ""
    bins = term(binning, "binning", (1, 1))
    if method == api.CAMERA_DOWNSAMPLE and (binning is NOT_GIVEN
                                            or bins is None):
        # The frame says the camera binned it by the factor.
        bins = (factor, factor)
    return api.FrameView(camera=camera, index=int(getattr(frame, "index", 0)),
                         image=view,
                         timestamp=int(getattr(frame, "timestamp_us", 0) or 0),
                         host_us=int(host_us),
                         saved=str(saved) if saved else None,
                         meta=MappingProxyType(meta), area=area,
                         binning=bins,
                         decimation=term(decimation, "decimation", (1, 1)),
                         mirror=term(mirror, "mirror", (False, False)),
                         downsample=factor, downsample_method=method)
