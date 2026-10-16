"""
typhon_plugin_api — the one Typhon module a plugin imports: plugin API 0.1,
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

IMPORTING THIS IS CHEAP
The re-exported classes are looked up on first use (module __getattr__), so
reading a manifest, or importing this package, loads no camera code.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import (Any, Callable, Dict, FrozenSet, Mapping, Optional,
                    Protocol, Tuple, runtime_checkable)

#: This API's version: 0.x until it is frozen as 1.0 at stage P5.
API_VERSION = "0.1"

#: The features of this API version, by name, for `has` (design 3.11). A
#: hook is here only once Typhon calls it: activate and deactivate (the
#: host), on_frame (the host's live feeds). check_start joins when Start
#: asks it (stage P3), status when the status strip does (P4).
FEATURES: FrozenSet[str] = frozenset({
    # what a plugin's context offers (PluginContext)
    "context.say", "context.call_in_ui", "context.cameras",
    "context.active_camera", "context.capture_folder",
    "context.current_frame", "context.crop_box", "context.on",
    "context.settings", "context.save_settings", "context.state",
    "context.temp_dir",
    # the hooks Typhon calls (FeaturePlugin)
    "hook.activate", "hook.deactivate", "hook.on_frame",
    # the types
    "type.FrameView", "type.CameraHandle", "type.CropBox",
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

    0.1: the capabilities are what today's Device says of itself (frames or
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
}

__all__ = sorted(["API_VERSION", "FEATURES", "EVENTS", "STATUS_MAX", "has",
                  "CameraHandle", "CropBox", "FrameView", "PluginContext",
                  "FeaturePlugin", *_REEXPORTS])


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
