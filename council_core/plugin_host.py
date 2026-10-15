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
  * Best effort — every hook but the two below: any exception, SystemExit
    included, switches the plugin off for the rest of the session
    ("faulted"), names it in the status line, appends the traceback to
    <project>/plugin_errors.log, and the caller carries on with a default.
  * The recording path — a plugin's record gate (per-frame tags): the
    plugin is faulted AND the recording ends cleanly at that frame with the
    reason in <run>_run_end.json, as a failed write does since M0 (`record_
    gate`, capture.FrameWriter.end_recording): carrying on would leave half
    a run tagged and half not.
  * check_start — before anything stops: the plugin is faulted and that
    Start is refused with the reason; the next Start runs without it.
A hang cannot be interrupted in Python: the watchdog switches off a call
that has run past WATCHDOG_SECONDS, which stops further calls into it.
M0's data-safety rules (reserved run names, exclusive frame writes, loud
failures) stay in the core; nothing here can switch them off.

LIVE FRAMES ON THE PLUGIN'S OWN THREAD (design 3.8, 10.2)
`feed()` gives a plugin a LiveFeed: the grab thread drops each frame into a
two-slot queue that throws away the oldest and counts it, and the plugin's
own thread calls `on_frame` with a read-only FrameView — optionally capped
to a share of the time (DUTY_CAP). A plugin on its own thread still shares
Python's lock with the grab thread; how much that costs inside the real Qt
Typhon is measured by tools/typhon_plugins_timing.py.

No pickle, no network, nothing deleted but the temporary folders this host
made itself.
"""
from __future__ import annotations

import collections
import importlib
import importlib.util
import os
import shutil
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

from . import plugin_lists as pl
from . import plugin_manifest as pm

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


class PluginHung(RuntimeError):
    """What the watchdog records for a call that ran too long."""


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


def _third_party_wait() -> str:
    return ("a plugin from someone else: it is listed, and it loads once "
            "you have approved it — approval comes with stage P5 of the "
            "plugin work; until then it does not run")


def _compiled_files(folder: Path) -> List[str]:
    """Compiled code inside `folder` (relative names), up to a few."""
    found: List[str] = []
    seen = 0
    for root, dirs, files in os.walk(folder):
        for name in list(dirs):
            if name == "__pycache__":
                found.append(str(Path(root, name).relative_to(folder)))
        for name in files:
            seen += 1
            if name.lower().endswith(COMPILED):
                found.append(str(Path(root, name).relative_to(folder)))
            if seen > MAX_FILES:
                found.append(f"(more than {MAX_FILES} files)")
                return found
        if len(found) >= 5:
            break
    return found


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
            clash = [s for s in m.files if s in suffixes]
            if clash:
                problem = (f"its run file {clash[0]!r} is "
                           f"{suffixes[clash[0]]}'s already")
        if not problem:
            problem = pm.api_problem(m.api, api_version)
        if not problem:
            compiled = _compiled_files(entry)
            if compiled:
                problem = (f"it holds compiled code ({', '.join(compiled[:3])}"
                           f"); a plugin is shipped as source, so the bytes "
                           f"you approve are the bytes that run")
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


def capabilities_of(device: Any, info: Any) -> frozenset:
    """What an open camera can do, from what today's Device says of itself
    (design 3.4). The trigger and binning capabilities arrive with their
    facets at stage P3; a camera plugin declares and confirms them at P2."""
    if device is None:
        return frozenset()
    caps = {"events" if getattr(info, "kind", "") == "event" else "frames"}
    if getattr(device, "restartable", True):
        caps.add("restartable")
    if getattr(device, "area_live", False):
        caps.add("area_live")
    if getattr(device, "counts_grab_failures", False):
        caps.add("grab_failures")
    if getattr(device, "records_raw", False):
        caps.add("raw_recording")
    return frozenset(caps)


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
        #: Plugins switched off for this session, with why (design 3.10).
        self.faults: Dict[str, Fault] = {}
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

    # -- the project and its lists ------------------------------------
    def builtin_ids(self) -> List[str]:
        return [pid for pid, _name in self.builtins]

    def open_project(self, project_dir: Any, *, create: bool = False
                     ) -> List[str]:
        """Use `project_dir`'s plugin list (typhon_plugins.json) and the
        plugin home it records. `create`: a project with no list gets one
        now, copying the base set as it is (question 1). Returns the notes
        (a recorded home missing, a damaged list); never raises for them."""
        with self._lock:
            self.project_dir = Path(project_dir)
            self.project = None
            self.project_problem = ""
            self.notes = []
            self._registry = None
            self._base = None
            recorded = None
            try:
                self.project = pl.read_project(self.project_dir)
            except pl.PluginListError as exc:
                self.project_problem = str(exc)
                self.notes.append(f"{exc} — this window runs the base set "
                                  f"and changes nothing in that file")
            if self.project is not None:
                recorded = self.project.plugin_home or None
            self.home = pl.resolve_home(recorded)
            if self.home.note:
                self.notes.append(self.home.note)
            if self.project is None and create and not self.project_problem:
                self.project = pl.make_project(self.project_dir,
                                               self.base_set(), self.home.path)
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
        or, with no project list, the base set itself."""
        if self.project is not None:
            out = self.project.runs()
        else:
            out = list(self.base_set().plugins)
        return out + [pid for pid in self._adopted if pid not in out]

    @property
    def registry(self) -> Registry:
        with self._lock:
            if self._registry is None:
                self._registry = discover(self.home.path,
                                          builtin_dir=self.builtin_dir,
                                          builtins=self.builtins)
            return self._registry

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
        problem = self._python_problem(rec)
        if problem:
            return INCOMPATIBLE, problem
        if rec.factory is None and self.project is None \
                and pid not in self.runs():
            return DISCOVERED, "its manifest was read"
        if pid not in self.runs():
            return OFF, "this project does not run it"
        if any(key[0] == pid for key in self._instances):
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
            out[slot.name] = api.CameraHandle(
                name=slot.name, info=info if device is not None else None,
                kind=str(getattr(info, "kind", "") or "") if device is not None
                else "",
                capabilities=capabilities_of(device, info))
        return MappingProxyType(out)

    # -- loading -------------------------------------------------------------
    def adopt(self, manifest: pm.Manifest, factory: Callable[[], Any]) -> None:
        """Register a plugin MADE IN THIS PROCESS (tests, the timing bench):
        `factory()` makes it. Nothing is read from disk, so nothing here
        needs approval — whoever calls this is already running code."""
        with self._lock:
            self._adopted[manifest.id] = PluginRecord(
                id=manifest.id, folder=Path("."), builtin=manifest.builtin,
                manifest=manifest, factory=factory)

    def instance(self, pid: str, camera: Optional[str] = None) -> Any:
        """Plugin `pid`'s instance (for a camera-scoped feature, the one of
        `camera`, else of the active camera), loaded and activated now if it
        was not yet — or None when it cannot run (the state says why)."""
        with self._lock:
            state, _why = self.state(pid)
            if state not in (ON, ACTIVE):
                return None
            rec = self.record(pid)
            m = rec.manifest
            key = self._key(m, camera)
            if key in self._instances:
                return self._instances[key]
            made = self._load(rec)
            if made is None or pid in self.faults:
                return None
            ctx = Context(self, pid, key[1])
            self._contexts[key] = ctx
            self._instances[key] = made
            activate = getattr(made, "activate", None)
            if callable(activate):
                self._guard(pid, "activate", activate, (ctx,), {}, None)
            if pid in self.faults:
                self._instances.pop(key, None)
                return None
            return made

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
        fn = getattr(made, hook, None)
        if not callable(fn):
            return default
        return self._guard(pid, hook, fn, args, kwargs, default)

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
        without it."""
        reasons: List[str] = []
        for pid in self.with_hook("run_hooks"):
            made = self.instance(pid)
            fn = getattr(made, "check_start", None) if made is not None \
                else None
            if not callable(fn):
                continue
            said = self._guard(pid, "check_start", fn, (plan,), {}, None,
                               kind=START_CHECK)
            if pid in self.faults and self.faults[pid].hook == "check_start":
                reasons.append(f"{self._name(pid)} failed its start check "
                               f"and was switched off: "
                               f"{self.faults[pid].error}")
            elif said:
                reasons.append(f"{self._name(pid)}: {said}")
        return reasons

    def _name(self, pid: str) -> str:
        rec = self.record(pid)
        return rec.name if rec is not None else pid

    # -- the guard -----------------------------------------------------------
    def _guard(self, pid: str, hook: str, fn: Callable[..., Any],
               args: Sequence[Any], kwargs: Mapping[str, Any], default: Any,
               kind: str = BEST_EFFORT) -> Any:
        """Call into plugin `pid`, never letting its error out: any
        exception, SystemExit included, faults it. A faulted plugin is not
        called again."""
        if pid in self.faults:
            return default
        key = (pid, threading.get_ident())
        before = self._busy.get(key)
        self._busy[key] = (hook, self._clock())
        try:
            return fn(*args, **kwargs)
        except (Exception, SystemExit) as exc:          # noqa: BLE001
            self.fault(pid, hook, exc, kind)
            return default
        finally:
            if before is None:
                self._busy.pop(key, None)
            else:
                self._busy[key] = before

    def fault(self, pid: str, hook: str, exc: BaseException,
              kind: str = BEST_EFFORT) -> Fault:
        """Switch plugin `pid` off for the rest of the session, because of
        `exc` in `hook`: kept in `faults`, said in the status line, and its
        traceback appended to <project>/plugin_errors.log. The first fault
        is the one its state shows; every one is logged."""
        made = Fault(plugin=pid, hook=hook, kind=kind,
                     error=f"{type(exc).__name__}: {exc}",
                     trace="".join(traceback.format_exception(
                         type(exc), exc, exc.__traceback__)),
                     at=time.strftime("%Y-%m-%d %H:%M:%S"))
        # Its own small lock, not the host's: the watchdog faults a plugin
        # whose hung call may be holding the host's lock (activate).
        with self._fault_lock:
            first = pid not in self.faults
            if first:
                self.faults[pid] = made
        self._log(made)
        if first:
            for feed in list(self._feeds):
                if feed.plugin == pid:
                    feed.stop(wait=0.0)
            self._announce(f"Plugin {self._name(pid)} was switched off after "
                           f"an error in {hook}: {made.error}")
        return self.faults[pid]

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

    def start_watchdog(self, every: float = 0.5) -> None:
        """Run `watchdog` on a thread of its own until `close`."""
        if self._watchdog is not None and self._watchdog.is_alive():
            return

        def loop() -> None:
            while not self._closing.wait(every):
                self.watchdog()
        self._watchdog = threading.Thread(target=loop, daemon=True,
                                          name="plugin-watchdog")
        self._watchdog.start()

    # -- the recording path --------------------------------------------------
    def record_gate(self, pid: str, gate: Any, session: Any) -> "GuardedGate":
        """Plugin `pid`'s record gate for `session`'s recording, guarded on
        the recording path: a fault in it ends the recording cleanly with
        its reason (design 3.10)."""
        return GuardedGate(self, pid, gate,
                           lambda reason, frame=None: end_recording(
                               session, reason, frame))

    def recording_fault(self, pid: str, hook: str, exc: BaseException,
                        frame: Any = None) -> str:
        """Fault `pid` on the recording path; the reason the recording ends
        with, naming the plugin, the hook and the frame."""
        self.fault(pid, hook, exc, RECORDING)
        where = (f" at frame {frame.index}"
                 if getattr(frame, "index", None) is not None else "")
        return (f"recording ended by plugin {self._name(pid)} ({pid}): its "
                f"{hook} failed{where} — {type(exc).__name__}: {exc}")

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
             duty: Optional[float] = None) -> Optional["LiveFeed"]:
        """A LiveFeed of frames to plugin `pid`'s `on_frame` (or to the
        `on_frame` given, which counts as that plugin's), on a thread of
        its own; None when the plugin cannot run or has no on_frame.
        `duty` caps its busy share of the time (None: uncapped)."""
        made = self.instance(pid, camera)
        if made is None:
            return None
        fn = on_frame if on_frame is not None else getattr(made, "on_frame",
                                                           None)
        if not callable(fn):
            return None
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
        with self._lock:
            keys = [k for k in self._instances if which(k)]
            made = [(k, self._instances.pop(k)) for k in keys]
            for k in keys:
                self._contexts.pop(k, None)
            gone = {k[0] for k in keys}
            for event in self._subs:
                self._subs[event] = [(p, f) for p, f in self._subs[event]
                                     if p not in gone]
        for feed in list(self._feeds):
            if feed.plugin in gone:
                feed.stop()
        for (pid, _slot), obj in made:
            fn = getattr(obj, "deactivate", None)
            if callable(fn):
                # Called even for a faulted plugin, so it can let go of what
                # it holds; its error is only logged.
                try:
                    fn()
                except (Exception, SystemExit) as exc:     # noqa: BLE001
                    self._log(Fault(pid, "deactivate", BEST_EFFORT,
                                    f"{type(exc).__name__}: {exc}",
                                    "".join(traceback.format_exception(
                                        type(exc), exc, exc.__traceback__)),
                                    time.strftime("%Y-%m-%d %H:%M:%S")))

    def close(self) -> None:
        """The window is closing (after any recording has stopped): stop the
        feeds, deactivate every instance, remove this host's temporary
        folders."""
        self._closing.set()
        for feed in list(self._feeds):
            feed.stop()
        self._deactivate(lambda key: True)
        with self._lock:
            self._feeds.clear()
            temp, self._temp = dict(self._temp), {}
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
    active camera."""

    def __init__(self, host: PluginHost, pid: str,
                 slot_id: Optional[int] = None) -> None:
        self._host = host
        self.plugin_id = pid
        self._slot_id = slot_id

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
        return self._host.subscribe(self.plugin_id, event, fn)

    def settings(self) -> Dict[str, Any]:
        return self._host.settings(self.plugin_id)

    def save_settings(self, values: Mapping[str, Any]) -> None:
        self._host.save_settings(self.plugin_id, values)

    def state(self, camera: Optional[str] = None) -> Dict[str, Any]:
        slot = self._slot_for(camera)
        if slot is None:
            raise RuntimeError(f"no camera named {camera!r}")
        return slot.plugin_state.setdefault(self.plugin_id, {})

    def temp_dir(self) -> Path:
        return self._host.temp_dir(self.plugin_id)

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
    """A plugin's record gate, guarded on the RECORDING PATH (design 3.6,
    3.10). It is what the session is handed as `gate=`: `tag(frame)` and
    the gate's other calls go to the plugin's gate; if one fails, the
    plugin is faulted and the recording ends at that frame, with the
    reason as the end record's recording_failed. A gate that is already
    faulted ends the recording at the next frame it is asked about.
    Anything else of the gate (its summary, its thread) is reached
    through unchanged."""

    #: The gate's calls on the recording path besides `tag`.
    RECORDING_CALLS = ("saved", "failed_grab", "confirm_saves")

    def __init__(self, host: PluginHost, pid: str, gate: Any,
                 end: Callable[..., Any]) -> None:
        self._host = host
        self._pid = pid
        self._gate = gate
        self._end = end
        #: Why the recording was ended by this gate, or "".
        self.ended = ""

    @property
    def gate(self) -> Any:
        return self._gate

    def tag(self, frame: Any) -> Any:
        if self.ended:
            return dict(FAULT_TAGS)
        if self._pid in self._host.faults:
            fault = self._host.faults[self._pid]
            self._finish(f"recording ended: plugin {self._host._name(self._pid)}"
                         f" ({self._pid}) was switched off — {fault.reason}",
                         frame)
            return dict(FAULT_TAGS)
        try:
            return self._gate.tag(frame)
        except (Exception, SystemExit) as exc:            # noqa: BLE001
            self._finish(self._host.recording_fault(
                self._pid, "record gate", exc, frame), frame)
            return dict(FAULT_TAGS)

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._gate, name)
        if name not in self.RECORDING_CALLS or not callable(target):
            return target

        def guarded(*args: Any, **kwargs: Any) -> Any:
            if self.ended or self._pid in self._host.faults:
                return None
            try:
                return target(*args, **kwargs)
            except (Exception, SystemExit) as exc:        # noqa: BLE001
                self._finish(self._host.recording_fault(
                    self._pid, f"record gate ({name})", exc))
                return None
        self.__dict__[name] = guarded
        return guarded

    def _finish(self, reason: str, frame: Any = None) -> None:
        if self.ended:
            return
        self.ended = reason
        try:
            self._end(reason, frame)
        except Exception:                                 # noqa: BLE001
            pass


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
        self._queue: Deque[Tuple[Any, int]] = collections.deque(
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

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"plugin-feed-{self.plugin}")
        self._thread.start()

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def offer(self, frame: Any) -> None:
        """The grab thread's part: queue `frame`, drop the oldest if full."""
        if self._stopping:
            return
        host_us = time.perf_counter_ns() // 1000
        with self._cond:
            self.offered += 1
            if len(self._queue) == FEED_SLOTS:
                self.dropped += 1
            self._queue.append((frame, host_us))
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
                frame, host_us = self._queue.popleft()
            if self.plugin in host.faults:
                return
            view = frame_view(frame, self.camera, host_us)
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


def frame_view(frame: Any, camera: str, host_us: int = 0) -> api.FrameView:
    """A read-only FrameView of `frame` (the pixels are not copied)."""
    image = getattr(frame, "image", None)
    view = image
    if image is not None and hasattr(image, "view"):
        view = image.view()
        try:
            view.flags.writeable = False
        except (AttributeError, ValueError):
            pass
    meta = getattr(frame, "meta", None) or {}
    return api.FrameView(camera=camera, index=int(getattr(frame, "index", 0)),
                         image=view,
                         timestamp=int(getattr(frame, "timestamp_us", 0) or 0),
                         host_us=int(host_us), saved=None,
                         meta=MappingProxyType(dict(meta)))
