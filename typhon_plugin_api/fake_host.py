"""
typhon_plugin_api.fake_host — a stand-in rig to test a plugin on its own
(docs/typhon_plugins_design.md 9.2; stage P3 row 6).

WHAT IT IS
Simulated cameras, a temporary "Typhon (plugins)" project and plugin home,
and no visible window: Typhon's own Start and Stop (frame_camera's, the
core's order of design 3.6) drive the plugin, so a plugin meets exactly
the order, the records and the faults it meets in the app — check_start
before anything stops, the run's name reserved, its record gate and run
hooks, the end record with its "plugins" block. Nothing here is a second
copy of the core.

    with FakeHost(tmp_path) as fake:
        fake.adopt(manifest, MyPlugin())        # a plugin made in-process
        fake.connect("frame")
        run = fake.capture(frames=5)            # Start, 5 frames, Stop
        run.frames, run.end, run.record ...

Everything is under the folder it is given (the test's temporary folder);
TYPHON_PLUGIN_HOME points there while it runs and is put back at close, and
so are the camera module's own state and COUNCIL_SIM_CAMERAS. One FakeHost
at a time in a process: the camera module has one window's state.

AT STAGE P5 it ships with the author's kit (design 3.11: python -m
typhon_plugin_api.check). Until then it is the branch's own, used by the
contract suites (tests/contracts/).
"""
from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

#: The simulated cameras' rows say this (cameras.SyntheticBackend).
SIMULATED = "Simulated"


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
    simulated cameras (COUNCIL_SIM_CAMERAS), default today's two."""

    def __init__(self, folder: Any, off: Sequence[str] = (),
                 sim_cameras: Optional[str] = None) -> None:
        import frame_camera
        from council_core import camera_setup, plugin_host, plugin_lists

        self.folder = Path(str(folder))
        self.home = self.folder / "plugin_home"
        self.project = self.folder / "project"
        self.runs = self.folder / "runs"
        self.home.mkdir(parents=True, exist_ok=True)
        self.project.mkdir(parents=True, exist_ok=True)
        self._env = {name: os.environ.get(name) for name in
                     (plugin_lists.HOME_ENV, "COUNCIL_SIM_CAMERAS")}
        os.environ[plugin_lists.HOME_ENV] = str(self.home)
        if sim_cameras is None:
            os.environ.pop("COUNCIL_SIM_CAMERAS", None)
        else:
            os.environ["COUNCIL_SIM_CAMERAS"] = str(sim_cameras)
        ids = [pid for pid, _name in plugin_host.BUILTIN_PLUGINS]
        plugin_lists.make_project(self.project, plugin_lists.read_base_set(
            self.home, ids), self.home)
        for pid in off:
            plugin_lists.switch_off(self.project, pid)
        self._fc = frame_camera
        frame_camera.disconnect()
        rig = frame_camera._RIG
        self._kept = (rig._plugins, rig.setup_path, rig.found)
        rig._plugins = None
        frame_camera._LIVE.setup_path = camera_setup.setup_path(self.project)
        frame_camera._LIVE.found = None
        self._closed = False
        self._feeds: List[Any] = []

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

    def adopt(self, manifest: Any, plugin: Any) -> Any:
        """A plugin made in this process: `manifest` (a checked Manifest,
        or its JSON as a dict, a built-in's rules), `plugin` the object."""
        from council_core import plugin_manifest as pm

        if isinstance(manifest, Mapping):
            manifest = pm.check(dict(manifest), builtin=True)
        self.host.adopt(manifest, lambda: plugin)
        return manifest

    def instance(self, pid: str) -> Any:
        return self.host.instance(pid)

    # -- a camera, and a run ----------------------------------------------
    def connect(self, kind: str = "frame") -> Dict[str, Any]:
        """Connect the simulated `kind` camera, as the window's Connect."""
        rows = self._fc.list_cameras()["rows"]
        row = next((r for r in rows if SIMULATED in str(r)
                    and str(r).endswith(kind)), None)
        if row is None:
            raise RuntimeError(f"no simulated {kind} camera in {rows}")
        return self._fc.connect(row)

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
            for each in self._feeds:
                each.stop()
            rig = fc._RIG
            if rig._plugins is not None:
                rig._plugins.close()
            rig._plugins, rig.setup_path, rig.found = self._kept
            for slot in rig:
                slot.plugin_state.clear()
            for name, value in self._env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

    def __enter__(self) -> "FakeHost":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
