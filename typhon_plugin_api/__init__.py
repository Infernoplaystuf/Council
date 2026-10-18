"""
typhon_plugin_api — the one Typhon module a plugin imports: plugin API 0.2,
the working version of the API that is frozen as 1.0 at stage P5.

WHAT THIS IS (docs/typhon_plugins_design.md 3.2, 3.5, 3.11)
A plugin — built in or written by someone else — imports Typhon only through
this package. The built-in plugins obey the same rule (a test checks every
import in typhon_plugins/), which is the real proof that the API is enough
for an outside author. What a plugin is handed (its context, camera handles,
live frames) is described here; the classes Typhon already has and a plugin
may use (Device, Frame, Provider, Setting, ...) are re-exported from where
they live, so their code stays where it is while the branch lives (design
7.2) and a fix to them on qt-migration lands in the same lines.

0.x: ANY CHANGE IS ALLOWED, FOR NOW
On the typhon-plugins branch the API is numbered 0.x while the built-in
plugins shape it; no camera plugin, run hook or UI point has been used for
real yet. A plugin names the exact 0.x it was written for (its manifest's
"api"). At stage P5 it is frozen as 1.0, and from then on an addition
raises the minor number and a removal or change raises the major (design
3.2). What 0.1 has, and what each later stage adds, is in
docs/typhon_plugin_api_notes.md — the working notes until the author's guide
of 1.0.

ASK, DO NOT COMPARE VERSIONS
`has("name")` says whether this Typhon offers a feature of the API; a plugin
asks it rather than comparing version strings (design 3.11).

CAMERA PLUGINS (0.2, stage P2)
A camera plugin finds, opens and describes one family of cameras
(`CameraPlugin`, design 3.3): it declares the capabilities its cameras can
have in its manifest and confirms them on each camera it opens (3.4), gives
the settings tabs its rows (`CategoryHints`), and a review source for a run
file of its own (`ReviewSource`: the Prophesee raw view). The built-in camera
plugins are adapters over today's Backend, Device and Provider classes; what
they reach of them while the code stays in council_core is in
`typhon_plugin_api.builtin`, which is not part of the API a plugin from
someone else may use (design 7.2).

A camera plugin with a SET-UP (`CameraSetup`; its manifest contributes
"setup") is a camera family the set-up wizard offers as a check box: it
brings its install guide (`Guide` of `Step`s), its checks (`Readiness` of
`Check`s) and, optionally, a scan page (`setup_scan`) into the wizard
(design 2.3, 2.4 item 15). A family nobody ticked is left out of the scan.
A camera plugin without a set-up (the simulated cameras) is always scanned.

IMPORTING THIS IS CHEAP
The re-exported classes are looked up on first use (module __getattr__), so
reading a manifest, or importing this package, loads no camera code.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import (Any, Callable, Dict, FrozenSet, List, Mapping, Optional,
                    Protocol, Sequence, Tuple, runtime_checkable)

#: This API's version: 0.x until it is frozen as 1.0 at stage P5.
API_VERSION = "0.2"

#: The features of this API version, by name, for `has` (design 3.11). A
#: hook is here only once Typhon calls it: activate and deactivate (the
#: host), on_frame (the host's live feeds), and a camera plugin's hooks the
#: host's camera registry calls (0.2: scanning, opening, confirming
#: capabilities, the settings tabs' rows, the review source; from stage P2
#: rows 3 and 4 its set-up guide, checks and scan, which the set-up wizard
#: of a "Typhon (plugins)" project calls). check_start joins when Start
#: asks it (stage P3), status when the status strip does (P4), a camera
#: plugin's facet and record_fields at P3.
FEATURES: FrozenSet[str] = frozenset({
    # what a plugin's context offers (PluginContext)
    "context.say", "context.call_in_ui", "context.cameras",
    "context.active_camera", "context.capture_folder",
    "context.current_frame", "context.crop_box", "context.on",
    "context.settings", "context.save_settings", "context.state",
    "context.temp_dir",
    # the hooks Typhon calls (FeaturePlugin)
    "hook.activate", "hook.deactivate", "hook.on_frame",
    # a camera plugin's hooks Typhon calls (CameraPlugin, 0.2)
    "camera.available", "camera.discover", "camera.open",
    "camera.capabilities", "camera.categories", "camera.review_source",
    # a camera plugin's set-up, which the set-up wizard calls (CameraSetup)
    "camera.setup_guide", "camera.setup_checks", "camera.setup_scan",
    # the types
    "type.FrameView", "type.CameraHandle", "type.CropBox",
    "type.CameraPlugin", "type.CategoryHints", "type.ReviewSource",
    "type.CameraSetup", "type.Guide", "type.Step", "type.Check",
    "type.Readiness",
})

#: What `ctx.on(event, fn)` may listen for; `fn` gets one read-only mapping.
EVENTS: Tuple[str, ...] = ("frames_reloaded", "camera_changed",
                           "crop_box_changed", "run_started", "run_stopped")

#: A plugin's status text is at most this long (design 3.5).
STATUS_MAX = 80


def has(name: str) -> bool:
    """Whether this Typhon offers the API feature `name` ("context.crop_box",
    "hook.on_frame", ...)."""
    return str(name) in FEATURES


# ======================================================================
# What a plugin is handed
# ======================================================================
@dataclass(frozen=True)
class CameraHandle:
    """One camera of the rig, as a feature plugin sees it (design 3.4): its
    slot's name (cam1, ...), what it is (`info`, a CameraInfo, or None while
    the slot has no camera), "frame" or "event", and its capabilities. Never
    the Device: what a feature does to a camera goes through a facet.

    0.2: the capabilities are what the camera's plugin confirmed when it
    opened the camera, else what today's Device says of itself (frames or
    events, restartable, area_live, grab_failures, raw_recording); `facet`
    answers None until the facets arrive at stage P3."""
    name: str
    info: Any = None
    kind: str = ""
    capabilities: FrozenSet[str] = frozenset()

    @property
    def connected(self) -> bool:
        return self.info is not None

    def facet(self, name: str) -> Any:
        """The facet `name` ("software_trigger", "binning", ...), or None
        when the camera has none. None for every name in 0.1."""
        return None


@dataclass(frozen=True)
class CropBox:
    """The crop box (x, y, w, h) in the saved pictures' pixels, with the
    Downsample factor those pictures were saved with (design 3.5, 3.12)."""
    xywh: Tuple[int, int, int, int]
    factor: int = 1


@dataclass(frozen=True)
class FrameView:
    """A live frame, as `on_frame` receives it on the plugin's own thread
    (design 3.8).

    `image` is a READ-ONLY view of the frame's pixels — the array the
    writer saves moments later — so writing to it raises, and so does
    making it writeable (`flags.writeable = True`, `setflags`): it is an
    array over a read-only buffer, and nothing is copied. `timestamp` is the
    camera's own clock and NOT always microseconds (a Basler gives its tick
    count, a boA5320 gives 0); `host_us` is when Typhon offered it to the
    plugin (this PC's monotonic clock, µs). `meta` is read-only: `aoi` (the
    camera's area the frame was taken with), `format`, and Downsample's
    `reduce` / `downsample` when on. `saved` is the frame's file name, or
    None: in 0.1 Typhon does not yet tell a plugin which frames were saved
    (stage P3).

    0.1 has no `to_sensor` yet: the coordinate fields of design 3.12
    (binning, decimation, mirroring) arrive at stage P3."""
    camera: str
    index: int
    image: Any
    timestamp: int = 0
    host_us: int = 0
    saved: Optional[str] = None
    meta: Mapping[str, Any] = field(default_factory=lambda:
                                    MappingProxyType({}))


@runtime_checkable
class PluginContext(Protocol):
    """What Typhon hands a plugin in `activate(ctx)` (design 3.5). A
    `camera`-scoped feature's context is tied to its camera slot; a `rig`
    feature's follows the active camera."""

    plugin_id: str

    def say(self, text: str) -> None:
        """Put `text` in the status line, named as this plugin's."""

    def call_in_ui(self, fn: Callable[[], None]) -> None:
        """Run `fn` on the UI thread soon. From any thread; the only way a
        plugin touches a widget from `on_frame` or other threads."""

    def cameras(self) -> Mapping[str, CameraHandle]:
        """Every camera slot of the rig, by name."""

    def active_camera(self) -> Optional[str]:
        """The active camera's name — the one the settings act on."""

    def capture_folder(self, camera: Optional[str] = None) -> Optional[Path]:
        """Where the camera's latest run saves (this plugin's camera, or
        the active one), or None before any run."""

    def current_frame(self) -> Optional[Path]:
        """The saved frame the slider shows, or None."""

    def crop_box(self, camera: Optional[str] = None) -> Optional[CropBox]:
        """The crop box, or None when none is drawn."""

    def on(self, event: str, fn: Callable[[Mapping[str, Any]], None]
           ) -> Callable[[], None]:
        """Listen for one of EVENTS; returns the call that stops listening."""

    def settings(self) -> Dict[str, Any]:
        """This plugin's settings in this project: the app-wide defaults
        with this project's own values laid over them."""

    def save_settings(self, values: Mapping[str, Any]) -> None:
        """Save `values` (plain values only) as THIS project's settings of
        this plugin; other projects keep theirs (question 14)."""

    def state(self, camera: Optional[str] = None) -> Dict[str, Any]:
        """This plugin's own per-camera state for this window's session (a
        dict on the camera slot; never saved)."""

    def temp_dir(self) -> Path:
        """A folder of this plugin's own for temporary files; Typhon removes
        it when the window closes."""


@runtime_checkable
class FeaturePlugin(Protocol):
    """A feature plugin (design 3.5). Every method is optional; Typhon calls
    the ones a plugin has. In 0.1 Typhon calls `activate` and `deactivate`
    and, where it is fed live frames, `on_frame`; the run hooks are wired at
    stage P3 and the screen points at stage P4."""

    def activate(self, ctx: PluginContext) -> None: ...

    def deactivate(self) -> None: ...


# ======================================================================
# Camera plugins (0.2, design 3.3, 3.4)
# ======================================================================
@dataclass(frozen=True)
class CategoryHints:
    """Where a camera plugin's settings groups go in the settings tabs
    (design 2.4 item 9, 3.3), as rows for the core's tables: `shared_tabs`
    (a tab's title, the groups it holds in the order it shows them, its
    tooltip), `tips` (the tooltip of a tab a group has to itself) and
    `most_used` (a group's keys a tab shows first). Typhon merges every
    camera plugin's hints (council_core.camera_categories.merge) and plans
    the tabs from them; a group no plugin names still gets a tab of its own,
    so a camera works without hints — the limit is the tab strip's width."""
    shared_tabs: Tuple[Tuple[str, Tuple[str, ...], str], ...] = ()
    tips: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({}))
    most_used: Mapping[str, Tuple[str, ...]] = field(
        default_factory=lambda: MappingProxyType({}))

    def groups(self) -> Tuple[str, ...]:
        """Every group these hints name, in the order they name them."""
        out: List[str] = []
        for _title, groups, _tip in self.shared_tabs:
            out += [g for g in groups if g not in out]
        out += [g for g in list(self.tips) + list(self.most_used)
                if g not in out]
        return tuple(out)


def _natural(name: str) -> List[Any]:
    """frame_9 before frame_10 (the slider's own order)."""
    return [(0, int(t), "") if t.isdigit() else (1, 0, t.lower())
            for t in re.split("([0-9]+)", name)]


@dataclass(frozen=True)
class ReviewSource:
    """How the slider shows one of a camera plugin's run files (design 3.3,
    3.7, 3.11): the Prophesee plugin's raw view of `<run>_events.raw`.

    `name` is the view's name ("raw"), `label` the word on the slider's
    view button ("Raw"), `suffix` the run file it shows (`<run><suffix>`).
    `open(path, window_us, **extra)` opens one for review and returns a
    playback the slider polls on its own timer — `count` (windows ready),
    `done`, `error`, `window_us`, `frame(k)` and `close()`, as
    council_core.event_playback.RawPlayback has them; `extra` may carry the
    run's `origin_us` and `area`. A file still being recorded is not opened
    unless `opens_while_recording`. The slider's live end stays Typhon's."""
    name: str
    label: str
    suffix: str
    open: Callable[..., Any]
    opens_while_recording: bool = False

    def run_of(self, path: Any) -> str:
        """The run a file of this source belongs to ("<run><suffix>" ->
        "<run>"), or ""."""
        name = os.path.basename(str(path or ""))
        if not self.suffix or not name.lower().endswith(self.suffix.lower()):
            return ""
        return name[:-len(self.suffix)]

    def file_for(self, folder: Any, run: str) -> Optional[Path]:
        """`run`'s file of this source in `folder`, if there is one."""
        if not run or not folder:
            return None
        path = Path(str(folder)) / f"{run}{self.suffix}"
        return path if path.is_file() else None

    def latest(self, folder: Any) -> Optional[Path]:
        """The newest file of this source in `folder`, by run name."""
        try:
            names = [e.name for e in os.scandir(str(folder))
                     if e.is_file() and self.run_of(e.name)]
        except OSError:
            return None
        if not names:
            return None
        return Path(str(folder)) / sorted(names, key=_natural)[-1]


@runtime_checkable
class CameraPlugin(Protocol):
    """A camera plugin (design 3.3): one family of cameras. Typhon makes one
    per window and opens a Device per camera slot through it.

    `backend` is its cameras' CameraInfo.backend ("basler"; a third party's
    carries a dot, "acme.thermal", so it can never claim another's preset
    keys). `available()` is (ready?, what to install when not) and must not
    raise for a missing SDK; `discover()` lists its cameras without opening
    any; `open(info)` returns today's Device. A camera's own refusal is a
    CameraError (or NeedsStop) and reaches the user as the camera's answer,
    never as the plugin's fault. `capabilities(device)` confirms, on the
    opened camera, the capabilities the manifest declares — never more
    (design 3.4). `facet(device, name)` is None until the facets arrive at
    stage P3. `categories()` gives the settings tabs its rows;
    `record_fields(device)` plain JSON for the run's record (wired at P3);
    `review_source()` a ReviewSource, or None."""

    backend: str

    def available(self) -> Tuple[bool, str]: ...

    def discover(self) -> List[Any]: ...

    def open(self, info: Any) -> Any: ...

    def capabilities(self, device: Any) -> FrozenSet[str]: ...

    def facet(self, device: Any, name: str) -> Any: ...

    def categories(self) -> CategoryHints: ...

    def record_fields(self, device: Any) -> Dict[str, Any]: ...

    def review_source(self) -> Optional[ReviewSource]: ...


@runtime_checkable
class CameraSetup(Protocol):
    """A camera plugin's set-up (design 2.3, 2.4 item 15, 3.3): what makes
    its family a check box in the set-up wizard. Its manifest lists
    "setup" among what it contributes.

    `setup_label` is the family's short name ("Basler"), said after the
    wizard. `setup_guide(python)` is the install steps (`Guide`, its
    `Step`s) written for the interpreter the app runs under, and must
    not raise. `setup_checks()` asks the installed software what it can
    actually do and returns a `Readiness` of `Check`s, each True (done),
    False (will stop the camera working) or None (could not tell, or only
    matters for some cameras); it takes seconds, so the wizard runs it
    off the UI thread, and it must not raise for a missing SDK.

    Optional: `setup_scan(held)` — a scan page (the Basler plugin's): every
    camera of the family and whether each will run, as a report shaped
    like council_core.basler_scan.ScanReport (`summary()`, `notes`,
    `cameras` with `verdict`, `label`, `capabilities` and `checks`), with
    `setup_scan_label` the words on its button. `held` maps the key of
    every camera this app holds open to its name in the rig ("cam2"): the
    scan must not open those again (a second open fails on real hardware)
    and says they are in use, and as which camera. The report type is
    settled with the author's kit at stage P5."""

    setup_label: str

    def setup_guide(self, python: str) -> Any: ...

    def setup_checks(self) -> Any: ...


# ======================================================================
# Today's classes, re-exported (design 3.11): their code stays where it is
# ======================================================================
_REEXPORTS: Dict[str, Tuple[str, str]] = {
    # cameras
    "Device": ("council_core.cameras", "Device"),
    "Backend": ("council_core.cameras", "Backend"),
    "Frame": ("council_core.cameras", "Frame"),
    "FramePool": ("council_core.cameras", "FramePool"),
    "CameraInfo": ("council_core.cameras", "CameraInfo"),
    "Roi": ("council_core.cameras", "Roi"),
    "Limits": ("council_core.cameras", "Limits"),
    "CameraError": ("council_core.cameras", "CameraError"),
    "NeedsStop": ("council_core.cameras", "NeedsStop"),
    "GrabFailed": ("council_core.cameras", "GrabFailed"),
    # settings
    "Provider": ("council_core.camera_settings", "Provider"),
    "Setting": ("council_core.camera_settings", "Setting"),
    "Change": ("council_core.camera_settings", "Change"),
    "SettingError": ("council_core.camera_settings", "SettingError"),
    # the GenICam helpers, shared by every GenICam camera plugin (2.1)
    "Trigger": ("council_core.camera_settings", "Trigger"),
    "trigger_key": ("council_core.camera_settings", "trigger_key"),
    "split_trigger_key": ("council_core.camera_settings",
                          "split_trigger_key"),
    # coordinates (3.12)
    "sensor_position": ("council_core.reduce", "sensor_position"),
    # capabilities (3.4): every name Typhon knows, and the one rule that
    # reads them off an open camera
    "CAPABILITIES": ("council_core.camera_capabilities", "CAPABILITIES"),
    "device_capabilities": ("council_core.camera_capabilities", "of_device"),
    "known_capabilities": ("council_core.camera_capabilities", "known"),
    "missing_capabilities": ("council_core.camera_capabilities", "missing"),
    # a camera plugin's set-up (CameraSetup): the wizard shell's own types,
    # which stay core (design 2.1)
    "Guide": ("council_core.camera_setup", "Guide"),
    "Step": ("council_core.camera_setup", "Step"),
    "Check": ("council_core.camera_setup", "Check"),
    "Readiness": ("council_core.camera_setup", "Readiness"),
}

__all__ = sorted(["API_VERSION", "FEATURES", "EVENTS", "STATUS_MAX", "has",
                  "CameraHandle", "CropBox", "FrameView", "PluginContext",
                  "FeaturePlugin", "CameraPlugin", "CameraSetup",
                  "CategoryHints", "ReviewSource", *_REEXPORTS])


def __getattr__(name: str) -> Any:
    target = _REEXPORTS.get(name)
    if target is None:
        raise AttributeError(f"typhon_plugin_api has no {name!r} (plugin API "
                             f"{API_VERSION})")
    import importlib

    value = getattr(importlib.import_module(target[0]), target[1])
    globals()[name] = value
    return value


def __dir__() -> Any:
    return __all__
