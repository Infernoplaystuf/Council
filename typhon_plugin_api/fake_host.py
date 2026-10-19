"""
typhon_plugin_api.fake_host — a stand-in rig to test a plugin on its own
(docs/typhon_plugins_design.md 9.2; stage P3 row 6).

WHAT IT IS
Simulated cameras, a temporary "Typhon (plugins)" project and plugin home,
and no visible window: Typhon's own Start and Stop (frame_camera's, the
core's order of design 3.6) drive the plugin, so a plugin meets exactly
the order, the records and the faults it meets in the app — check_start
before anything stops, the run's name reserved, its record gate, frame
stage and run hooks, the end record with its "plugins" block. Nothing here
is a second copy of the core.

FOR A PLUGIN'S AUTHOR (design 3.11, 9.2). With a Council checkout on
PYTHONPATH, a plugin's own tests load its folder and drive it:

    from typhon_plugin_api.fake_host import FakeHost

    def test_my_plugin(tmp_path):
        with FakeHost(tmp_path) as fake:
            fake.load("path/to/acme_marks")       # its plugin.json + code
            fake.connect("frame")                 # the simulated camera
            run = fake.capture(frames=5)          # Start, 5 frames, Stop
            assert run.problems() == []           # saved = CSV = end record
            assert run.end["plugins"]["acme.marks"] == {...}

`load(folder)` reads the folder's manifest as Typhon reads one from
someone else (the third-party rules: no record gate or frame stage, a run
file named `_<short>_<what>`, a short name and run files of its own) and
imports its code from the folder's own files — without writing
`__pycache__` into it, which would get the folder refused (design 4.5).
Typhon itself loads plugins from someone else only from stage P5, after
your approval; the fake host loads the one you name because you are
already running your own code. `adopt(manifest, plugin)` takes a plugin
made in the test's own process instead.

Only the simulated cameras are scanned unless `real_cameras=True`: the
temporary project switches the other built-in camera plugins off, so no
camera SDK is imported and no real camera is touched.

Everything is under the folder it is given (the test's temporary folder):
TYPHON_PLUGIN_HOME and COUNCIL_VAULT_ROOT point there while it runs and are
put back at close, and so are COUNCIL_SIM_CAMERAS, the camera module's own
state and the modules `load` imported. One FakeHost at a time in a process:
the camera module has one window's state.

AT STAGE P5 it ships with the author's kit (design 3.11: python -m
typhon_plugin_api.check, the contract suites of tests/contracts/ against
the plugin's folder). Until then it is the branch's, used by those suites.
"""
from __future__ import annotations

import csv
import importlib
import json
import os
import sys
import threading
import time
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

#: The simulated cameras' rows say this (cameras.SyntheticBackend).
SIMULATED = "Simulated"
#: The built-in camera plugin that stays on when only the simulated cameras
#: are scanned.
SIMULATED_PLUGIN = "typhon.simulated"
#: The package a plugin `load` imports from its folder is put under.
PACKAGE = "typhon_fakehost"


class FakeHostError(RuntimeError):
    """What the fake host refuses: a second one at a time, a plugin folder
    Typhon would refuse, and why."""


_OPEN_LOCK = threading.Lock()
_OPEN: List["FakeHost"] = []


@dataclass
class Run:
    """One run the fake host captured: its folder and name, what Start and
    Stop said, and its files as today's readers read them."""
    folder: Path
    run: str
    started: str = ""
    stopped: str = ""

    def _path(self, ending: str) -> Path:
        return self.folder / f"{self.run}{ending}"

    def _rows(self, ending: str) -> Optional[List[Dict[str, str]]]:
        path = self._path(ending)
        if not path.is_file():
            return None
        with open(path, newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))

    @property
    def frames(self) -> List[Dict[str, str]]:
        """The frames CSV's rows."""
        return self._rows("_frames.csv") or []

    @property
    def triggers(self) -> Optional[List[Dict[str, str]]]:
        """The triggers CSV's rows, or None for a run with none."""
        return self._rows("_capture_triggers.csv")

    @property
    def end(self) -> Dict[str, Any]:
        """The end record (<run>_run_end.json)."""
        return json.loads(self._path("_run_end.json").read_text(
            encoding="utf-8"))

    @property
    def record(self) -> Dict[str, Any]:
        """The camera record (<run>_camera.json)."""
        return json.loads(self._path("_camera.json").read_text(
            encoding="utf-8"))

    def pngs(self) -> List[Path]:
        return sorted(self.folder.glob(f"{self.run}_frame_*.png"))

    def file(self, ending: str) -> Path:
        """The run's file `<run><ending>` (a plugin's sidecar), there or
        not."""
        return self._path(ending)

    def problems(self) -> List[str]:
        """What breaks the data-safety sums (M0), in words — [] for a whole
        run: frames saved = frames CSV rows = the end record's count, each
        row's file there and each saved picture in the CSV, in order."""
        out: List[str] = []
        if not self._path("_run_end.json").is_file():
            return [f"{self.run}_run_end.json is not there: the recording "
                    f"has not ended"]
        try:
            end = self.end
        except (OSError, ValueError) as exc:
            return [f"{self.run}_run_end.json cannot be read: {exc}"]
        rows = [r.get("file", "") for r in self.frames]
        pngs = [p.name for p in self.pngs()]
        saved = end.get("frames_saved")
        if len(rows) != saved:
            out.append(f"the frames CSV has {len(rows)} rows, the end record "
                       f"says {saved} saved")
        if len(pngs) != saved:
            out.append(f"{len(pngs)} pictures are saved, the end record says "
                       f"{saved}")
        missing = [r for r in rows if r not in set(pngs)]
        if missing:
            out.append(f"the frames CSV names pictures that are not there: "
                       f"{', '.join(missing[:3])}")
        unlisted = [p for p in pngs if p not in set(rows)]
        if unlisted:
            out.append(f"pictures not in the frames CSV: "
                       f"{', '.join(unlisted[:3])}")
        if rows != sorted(rows):
            out.append("the frames CSV is not in the order the frames came")
        kinds = {}
        for png in self.pngs():
            kinds.setdefault(png_kind(png), png.name)
        if len(kinds) > 1:
            out.append("the run's pictures are of more than one size or "
                       "mode: " + ", ".join(
                           f"{name} {kind}" for kind, name in
                           list(kinds.items())[:3]))
        return out


def png_kind(path: Path) -> str:
    """A PNG's size and mode from its header, "640x480 8-bit grey" — read
    without decoding it (P3 review: a run's pictures are one size and one
    mode, which the records describe)."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(29)
    except OSError as exc:
        return f"unreadable ({type(exc).__name__})"
    if len(head) < 26 or head[:8] != b"\x89PNG\r\n\x1a\n" or \
            head[12:16] != b"IHDR":
        return "not a PNG"
    width = int.from_bytes(head[16:20], "big")
    height = int.from_bytes(head[20:24], "big")
    depth, colour = head[24], head[25]
    mode = {0: "grey", 2: "RGB", 3: "palette", 4: "grey+alpha",
            6: "RGBA"}.get(colour, f"colour type {colour}")
    return f"{width}x{height} {depth}-bit {mode}"


def wait_for(condition: Callable[[], Any], seconds: float = 10.0) -> bool:
    """`condition()` came true within `seconds`."""
    deadline = time.monotonic() + seconds
    while True:
        if condition():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.005)


class FakeHost:
    """A stand-in rig (see the module): a temporary project under
    `folder`, its plugin host, simulated cameras and Typhon's own Start and
    Stop. `off`: plugins this project switches off; `sim_cameras`: the
    simulated cameras (COUNCIL_SIM_CAMERAS), default today's two;
    `real_cameras`: also scan the built-in Basler and Prophesee plugins'
    cameras (their SDKs and any real camera)."""

    #: The environment it points at its folder while it runs.
    ENV = ("TYPHON_PLUGIN_HOME", "COUNCIL_SIM_CAMERAS", "COUNCIL_VAULT_ROOT")

    def __init__(self, folder: Any, off: Sequence[str] = (),
                 sim_cameras: Optional[str] = None,
                 real_cameras: bool = False) -> None:
        with _OPEN_LOCK:
            if _OPEN:
                raise FakeHostError("a FakeHost is open already in this "
                                    "process: one at a time (the camera "
                                    "module has one window's state)")
            _OPEN.append(self)
        try:
            self._open(folder, off, sim_cameras, real_cameras)
        except BaseException:
            with _OPEN_LOCK:
                _OPEN.remove(self)
            raise

    def _open(self, folder: Any, off: Sequence[str],
              sim_cameras: Optional[str], real_cameras: bool) -> None:
        import frame_camera
        from council_core import camera_setup, plugin_host, plugin_lists

        self.folder = Path(str(folder))
        self.home = self.folder / "plugin_home"
        self.project = self.folder / "project"
        self.runs = self.folder / "runs"
        self.vault = self.folder / "vault"
        for where in (self.home, self.project, self.vault):
            where.mkdir(parents=True, exist_ok=True)
        self._env = {name: os.environ.get(name) for name in self.ENV}
        os.environ[plugin_lists.HOME_ENV] = str(self.home)
        os.environ["COUNCIL_VAULT_ROOT"] = str(self.vault)
        if sim_cameras is None:
            os.environ.pop("COUNCIL_SIM_CAMERAS", None)
        else:
            os.environ["COUNCIL_SIM_CAMERAS"] = str(sim_cameras)
        self._modules: List[str] = []
        self._feeds: List[Any] = []
        self._closed = False
        ids = [pid for pid, _name in plugin_host.BUILTIN_PLUGINS]
        plugin_lists.make_project(self.project, plugin_lists.read_base_set(
            self.home, ids), self.home)
        switched = list(off)
        if not real_cameras:
            switched += [pid for pid in self._builtin_cameras()
                         if pid != SIMULATED_PLUGIN and pid not in switched]
        for pid in switched:
            plugin_lists.switch_off(self.project, pid)
        self._fc = frame_camera
        frame_camera.disconnect()
        rig = frame_camera._RIG
        self._kept = (rig._plugins, rig.setup_path, rig.found)
        rig._plugins = None
        frame_camera._LIVE.setup_path = camera_setup.setup_path(self.project)
        frame_camera._LIVE.found = None

    @staticmethod
    def _builtin_cameras() -> List[str]:
        from council_core import plugin_host
        from council_core import plugin_manifest as pm

        out = []
        for pid, name in plugin_host.BUILTIN_PLUGINS:
            try:
                m = pm.read(plugin_host.BUILTIN_DIR / name, builtin=True)
            except pm.ManifestError:
                continue
            if m.kind == "camera":
                out.append(pid)
        return out

    # -- the window's pieces ---------------------------------------------
    @property
    def host(self) -> Any:
        """The project's plugin host (the rig's), made on first ask."""
        return self._fc._plugin_host()

    @property
    def camera(self) -> str:
        """The active camera slot's name."""
        return self._fc._RIG.active.name

    @property
    def device(self) -> Any:
        return self._fc._LIVE.device

    @property
    def session(self) -> Any:
        return self._fc._LIVE.session

    @property
    def faults(self) -> Mapping[str, Any]:
        """The plugins switched off this session, with why (design 3.10)."""
        return self.host.faults

    @property
    def said(self) -> List[str]:
        """What the plugins and the host said, oldest first."""
        return list(self.host.said)

    def adopt(self, manifest: Any, plugin: Any) -> Any:
        """A plugin made in this process: `manifest` (a checked Manifest,
        or its JSON as a dict, a built-in's rules), `plugin` the object."""
        from council_core import plugin_manifest as pm

        if isinstance(manifest, Mapping):
            manifest = pm.check(dict(manifest), builtin=True)
        self.host.adopt(manifest, lambda: plugin)
        return manifest

    def load(self, folder: Any) -> Any:
        """The plugin in `folder` (its plugin.json and code), as Typhon
        reads a plugin from someone else (see the module): its manifest
        checked by the third-party rules, its code imported from the
        folder's own files with nothing written into it. Its object is
        made, and activated, on first use, as in the app. Returns its
        Manifest; FakeHostError (or the manifest's ManifestError) says
        what Typhon would refuse."""
        from council_core import plugin_host as ph
        from council_core import plugin_manifest as pm

        where = Path(str(folder)).resolve()
        builtin = {(ph.BUILTIN_DIR / name).resolve(): pid
                   for pid, name in ph.BUILTIN_PLUGINS}
        if where in builtin:
            # A built-in is on this project's list already.
            return pm.read(where, builtin=True)
        m = pm.read(where, builtin=False)
        problem = pm.api_problem(m.api, pm.API_VERSION)
        compiled, too_many = ph._compiled_files(where)
        if not problem and compiled:
            problem = (f"it holds compiled code ({', '.join(compiled[:3])}); "
                       f"a plugin is shipped as source")
        if not problem and too_many:
            problem = f"it holds more than {ph.MAX_FILES} files"
        host = self.host
        if not problem:
            # Against EVERY plugin Typhon found — built-ins switched off in
            # this project among them, whose short names and run files are
            # still theirs — as Typhon's discovery checks it (P3 review: a
            # switched-off built-in's short name was accepted here).
            records = dict(host.registry.records)
            records.update(host._adopted)
            shorts: Dict[str, str] = {}
            endings: Dict[str, str] = {}
            for pid, rec in records.items():
                other = rec.manifest
                if other is None or pid == m.id:
                    continue
                shorts.setdefault(other.short, pid)
                for suffix in other.files:
                    endings.setdefault(suffix, pid)
            if m.short in shorts:
                problem = (f"its short name {m.short!r} is "
                           f"{shorts[m.short]}'s already")
            else:
                problem = ph.ending_clash(m.files, endings)
        if problem:
            raise FakeHostError(f"{m.id} would be refused: {problem}")
        package = f"{PACKAGE}.{m.id.replace('.', '_')}"
        module = self._import(where, package, m.module)
        factory = getattr(module, m.attribute, None)
        if not callable(factory):
            raise FakeHostError(f"{m.id}: its entry {m.module}:{m.attribute} "
                                f"names nothing callable")
        host.adopt(m, factory)
        return m

    def _import(self, folder: Path, package: str, module: str) -> Any:
        """`<package>.<module>` from `folder`, a package of its own, with no
        bytecode written into the folder."""
        root = sys.modules.get(PACKAGE)
        if root is None:
            root = types.ModuleType(PACKAGE)
            root.__path__ = []                      # a namespace of its own
            sys.modules[PACKAGE] = root
            self._modules.append(PACKAGE)
        if package not in sys.modules:
            made = types.ModuleType(package)
            made.__path__ = [str(folder)]
            made.__file__ = str(folder / "__init__.py")
            sys.modules[package] = made
            self._modules.append(package)
        before = set(sys.modules)
        kept = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            imported = importlib.import_module(f"{package}.{module}")
        finally:
            sys.dont_write_bytecode = kept
            self._modules += [n for n in set(sys.modules) - before
                              if n.startswith(package)]
        return imported

    def instance(self, pid: str) -> Any:
        return self.host.instance(pid)

    # -- a camera, and a run ----------------------------------------------
    def connect(self, kind: str = "frame", label: str = SIMULATED
                ) -> Dict[str, Any]:
        """Connect the `kind` camera whose row in the scan holds `label`
        (the simulated one by default; a camera plugin's own by its
        label), as the window's Connect."""
        rows = self._fc.list_cameras()["rows"]
        row = next((r for r in rows if label in str(r)
                    and str(r).endswith(kind)), None)
        if row is None:
            raise RuntimeError(f"no {label} {kind} camera in {rows}")
        return self._fc.connect(row)

    def live_view(self, seconds: float = 5.0) -> bool:
        """The live view, as the window's tick starts it after Connect:
        True once the camera streams."""
        from council_core import capture

        capture.warm_imports()
        self._fc._manage_preview(True)
        return wait_for(lambda: self.session is not None and
                        self.session.running, seconds)

    def set(self, key: str, value: Any) -> Dict[str, Any]:
        """One camera setting, as the settings window writes it."""
        out = self._fc.set_camera_setting(key, value)
        if not out.get("ok", True):
            raise RuntimeError(out.get("summary", ""))
        return out

    def feed(self, pid: str, **kw: Any) -> Any:
        """Feed plugin `pid`'s on_frame every frame the camera sends (the
        grab thread offers each; the plugin's own thread calls it), until
        close. The window wires live frames from stage P4."""
        made = self.host.feed(pid, camera=self.camera, **kw)
        if made is None:
            return None
        self._feeds.append(made)
        session = self.session
        before = session.on_frame

        def offer(frame: Any) -> None:
            if before is not None:
                before(frame)
            for each in list(self._feeds):
                each.offer(frame)
        session.on_frame = offer
        return made

    def start(self, folder: Any = None) -> Dict[str, Any]:
        return self._fc.start(str(folder or self.runs))

    def stop(self) -> Dict[str, Any]:
        return self._fc.stop()

    def capture_now(self) -> str:
        return self._fc.capture_now()["summary"]

    def capture(self, frames: int = 3, during: Optional[Callable[[], Any]]
                = None, folder: Any = None, seconds: float = 10.0) -> Run:
        """Start, until `frames` are saved (and `during()`, if given, has
        run), then Stop; the run once its end record is on disk."""
        out = Path(str(folder or self.runs))
        started = self.start(out)
        try:
            if during is not None:
                during()
            wait_for(lambda: self.session.stats().recorded >= frames,
                     seconds)
        finally:
            stopped = self.stop()
        run = Run(out, started["run"], started.get("summary", ""),
                  stopped.get("summary", ""))
        wait_for(lambda: (out / f"{run.run}_run_end.json").is_file(),
                 seconds)
        return run

    # -- closing ------------------------------------------------------------
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        fc = self._fc
        try:
            fc.disconnect()
        finally:
            try:
                for each in self._feeds:
                    each.stop()
                rig = fc._RIG
                if rig._plugins is not None:
                    rig._plugins.close()
                rig._plugins, rig.setup_path, rig.found = self._kept
                for slot in rig:
                    slot.plugin_state.clear()
                for name in reversed(self._modules):
                    sys.modules.pop(name, None)
                for name, value in self._env.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
            finally:
                with _OPEN_LOCK:
                    if self in _OPEN:
                        _OPEN.remove(self)

    def __enter__(self) -> "FakeHost":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
