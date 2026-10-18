"""
typhon_plugin_api — the one Typhon module a plugin imports: plugin API 0.3,
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

A RUN, AS A PLUGIN SEES IT (stage P3 rows 1 and 4)
A plugin whose manifest contributes "run_hooks" is asked `check_start(plan)`
(a RunPlan: the cameras of the run to be, each with its folder, area,
Downsample and capture set-up) before anything stops — a reason refuses
Start — then `on_run_started(run)` (a RunInfo) once the recording has begun,
and `on_run_finished(run)` once its last frame is on disk, with the end
record's counts; the mapping it returns is its block in that end record
(`<run>_run_end.json`'s "plugins"), cleaned and size-checked. A camera
plugin's `record_fields(device)` goes in its entry of the camera record's
"plugins" block, which says what made the run (design 3.9, 4.2).
The context's two FILE SERVICES are the only way a plugin writes a run
file, and neither ever writes over anything (design 3.9, 10.3):
`create_file` streams a new file, made exclusively, in a folder of the run
in progress; `write_new` writes a whole file under a temporary name and
renames it into a name that must not exist ("_2", "_3" ... on a rerun),
beside any run, past ones included, or in the plugin's own `data_dir()`
(`<project>/plugin_data/<id>/`). A run file's name is the run's and one of
the endings the manifest's "files" declares; Typhon registers every one, so
a run name is never used twice.
A plugin contributing "presets" keeps values of its own in each camera's
presets as "<id>/<key>": asked `preset_values(camera)` when one is saved,
and handed them in `apply_preset_values(camera, values)` when it is
applied. The keys of a plugin that is not running are never handed to the
camera, never lost, and said as kept (design 3.9).

THE CAPTURE SET-UP AND THE FACETS (stage P3 rows 2 and 3)
A feature plugin never gets a camera's Device: it gets a `CameraHandle`,
whose `facet(name)` gives what the camera's plugin offers by capability
name (design 3.4) — `SoftwareTrigger` (Typhon fires it), `HardwareTrigger`
(the camera waits for an input), `Binning` (the camera averages its own
pixels), or None. The types they take and give — `CaptureSetup` (a run's
trigger mode, source, frames per trigger, timer, edge, delay, debounce and
Downsample), `CaptureOptions` (what the camera offers it), the modes and
Typhon's own sources — are here, so a camera plugin implements a facet
against this API alone and never imports the triggering plugin (design
2.5). While the code stays where it is (design 7.2) they are today's
classes, re-exported; the camera's set-up stays the storage today's code
reads (`device.capture`), read by a plugin through `ctx.capture_setup()`.
Downsample is the core's: its key (save.downsample), its row in every frame
camera's "Capture" group and the one frame stage that makes the saved
picture smaller are Typhon's, not a plugin's (design 2.1). A `FrameView`
carries every term that turns a picture's pixel into a sensor pixel, and
`to_sensor()` does it (design 3.12). A `TriggerSource` says "capture now"
(design 3.6): Typhon's own are the triggering plugin's Software and Timer.

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

#: This API's version: 0.x until it is frozen as 1.0 at stage P5 — 0.1 at
#: stage P1, 0.2 at P2 (the camera plugins), 0.3 at P3 (runs, presets, the
#: facets, triggering).
API_VERSION = "0.3"

#: The features of this API version, by name, for `has` (design 3.11). A
#: hook is here only once Typhon calls it: activate and deactivate (the
#: host), on_frame (the host's live feeds), and a camera plugin's hooks the
#: host's camera registry calls (0.2: scanning, opening, confirming
#: capabilities, the settings tabs' rows, the review source; from stage P2
#: rows 3 and 4 its set-up guide, checks and scan, which the set-up wizard
#: of a "Typhon (plugins)" project calls). From stage P3 rows 1 and 4 the
#: run hooks (Start asks check_start, before anything stops), a camera
#: plugin's record_fields (the camera record at Start), the preset hooks,
#: and the context's two file services and data folder. From P3 rows 2 and
#: 3: a camera plugin's facets (a camera handle asks them), the types of
#: the capture set-up and the facets, a FrameView's coordinates and the
#: file it was saved as, the context's view of the camera's set-up, and the
#: triggering plugin's trigger slot and Capture now (F9), which Start, Stop
#: and the F9 key call. status, panel, toolbar and settings_rows join when
#: the window asks them (P4).
FEATURES: FrozenSet[str] = frozenset({
    # what a plugin's context offers (PluginContext)
    "context.say", "context.call_in_ui", "context.cameras",
    "context.active_camera", "context.capture_folder",
    "context.current_frame", "context.crop_box", "context.on",
    "context.settings", "context.save_settings", "context.state",
    "context.temp_dir",
    # the two file services and the plugin's data folder (P3 row 1)
    "context.create_file", "context.write_new", "context.data_dir",
    # the hooks Typhon calls (FeaturePlugin)
    "hook.activate", "hook.deactivate", "hook.on_frame",
    # a run's hooks (P3 row 1) and a camera's presets (P3 row 4)
    "hook.check_start", "hook.on_run_started", "hook.on_run_finished",
    "hook.preset_values", "hook.apply_preset_values",
    # a camera plugin's hooks Typhon calls (CameraPlugin, 0.2)
    "camera.available", "camera.discover", "camera.open",
    "camera.capabilities", "camera.categories", "camera.review_source",
    "camera.record_fields",
    # a camera plugin's set-up, which the set-up wizard calls (CameraSetup)
    "camera.setup_guide", "camera.setup_checks", "camera.setup_scan",
    # the types
    "type.FrameView", "type.CameraHandle", "type.CropBox",
    "type.CameraPlugin", "type.CategoryHints", "type.ReviewSource",
    "type.CameraSetup", "type.Guide", "type.Step", "type.Check",
    "type.Readiness", "type.RunPlan", "type.RunInfo", "type.RunCamera",
    # P3 rows 2 and 3: the facets a camera handle gives, the capture
    # set-up's types, a frame's coordinates, the camera's set-up read in
    # place, the trigger sources, and the triggering plugin's trigger slot
    # and Capture now (built-in: the record gate's)
    "camera.facet", "type.CaptureSetup", "type.CaptureOptions",
    "type.SoftwareTrigger", "type.HardwareTrigger", "type.Binning",
    "type.TriggerSource", "type.TriggerEvent", "type.Action",
    "frame.to_sensor", "frame.saved", "context.capture_setup",
    "hook.trigger_slot", "hook.capture_now",
    # P3 row 6: a built-in plugin's frame stage for a run, which Start asks
    # (after the core's Downsample, guarded on the recording path)
    "hook.frame_stage",
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
    events, restartable, area_live, grab_failures, raw_recording). From P3:
    `facet(name)` is what the camera's plugin gives for a capability name
    (FACETS), asked when it is called; `lookup` is how the handle asks
    (Typhon's, never compared)."""
    name: str
    info: Any = None
    kind: str = ""
    capabilities: FrozenSet[str] = frozenset()
    lookup: Optional[Callable[[str], Any]] = field(default=None,
                                                   compare=False, repr=False)

    @property
    def connected(self) -> bool:
        return self.info is not None

    def facet(self, name: str) -> Any:
        """The facet `name` ("software_trigger", "hardware_trigger",
        "binning"), or None when the camera has none — or no camera is
        open. A facet whose capability the camera did not confirm may still
        be given: its `problem()` then says why it cannot (design 3.6: the
        facet's own check runs at Start)."""
        if self.lookup is None or self.info is None:
            return None
        return self.lookup(str(name))


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
    None for a frame not saved (a snapshot run passes over the frames
    between its triggers; the live view saves nothing).

    THE PICTURE'S COORDINATES (design 3.12, from P3). A plugin sees the
    full-size frame, while the saved PNG, the live view and the boxes may be
    reduced; everything a plugin stores or draws is in full-sensor, unbinned
    pixels (pixel (x, y) centred at index (x, y)). The terms: `area`, the
    camera's area the frame was taken with (x, y, w, h, in the camera's own
    pixels — binned ones on a binned camera); `binning`, sensor pixels per
    camera pixel (h, v) — with the camera's binning as Downsample, the
    Downsample factor; `decimation` and `mirror`, as the camera reports
    them; and Downsample's `downsample` factor and `downsample_method`
    ("typhon_block_mean": Typhon averages the saved picture's blocks;
    "camera_binning_average": the camera bins; "" for off). `to_sensor(x,
    y)` turns a pixel of the SAVED picture into sensor pixels."""
    camera: str
    index: int
    image: Any
    timestamp: int = 0
    host_us: int = 0
    saved: Optional[str] = None
    meta: Mapping[str, Any] = field(default_factory=lambda:
                                    MappingProxyType({}))
    area: Tuple[int, int, int, int] = (0, 0, 0, 0)
    binning: Tuple[int, int] = (1, 1)
    decimation: Tuple[int, int] = (1, 1)
    mirror: Tuple[bool, bool] = (False, False)
    downsample: int = 1
    downsample_method: str = ""

    def to_sensor(self, x: float, y: float) -> Tuple[float, float]:
        """The saved picture's pixel (x, y) in sensor pixels (design 3.12):
        Typhon's Downsample factor first, then the area's origin, then the
        camera's binning — council_core.reduce.sensor_position on each
        axis. A mirrored or decimated picture raises ValueError: those
        conversions need terms no camera reports yet, and a position is
        never guessed."""
        if any(bool(m) for m in self.mirror):
            raise ValueError("this picture is mirrored (the camera's "
                             "ReverseX / ReverseY): to_sensor needs the "
                             "sensor's size to undo it, which a FrameView "
                             "does not carry yet — no position is guessed")
        if tuple(int(d) for d in self.decimation) != (1, 1):
            raise ValueError("this picture is decimated: to_sensor does not "
                             "convert a decimated picture yet — no position "
                             "is guessed")
        place = __getattr__("sensor_position")
        factor = (max(1, int(self.downsample or 1))
                  if self.downsample_method == TYPHON_DOWNSAMPLE else 1)
        bx, by = (max(1, int(b)) for b in self.binning)
        ax, ay = (float(a) for a in tuple(self.area)[:2])
        return (place(float(x), factor, ax, bx),
                place(float(y), factor, ay, by))


#: Downsample's two methods, as the frames CSV and the records say them
#: (council_core.reduce: TYPHON_MEAN and CAMERA_AVERAGE).
TYPHON_DOWNSAMPLE = "typhon_block_mean"
CAMERA_DOWNSAMPLE = "camera_binning_average"


@dataclass(frozen=True)
class RunCamera:
    """One camera of a run to be, as a RunPlan names it (design 3.6, 3.11):
    its slot's name, "frame" or "event", what it is (`info`, a CameraInfo)
    and can do, the folder its run saves into, its area (x, y, w, h, sensor
    pixels), Downsample's factor (1: off) and method ("typhon_block_mean",
    "camera_binning_average", or "" for off), and its capture set-up as
    plain values ({} for a camera with none: an event camera)."""
    name: str
    kind: str = ""
    info: Any = None
    capabilities: FrozenSet[str] = frozenset()
    folder: Optional[Path] = None
    area: Tuple[int, int, int, int] = (0, 0, 0, 0)
    downsample: int = 1
    downsample_method: str = ""
    capture: Mapping[str, Any] = field(default_factory=lambda:
                                       MappingProxyType({}))


@dataclass(frozen=True)
class RunPlan:
    """The run Start is about to make, as `check_start(plan)` sees it
    (design 3.6, 3.11), before anything stops or is reserved: `base`, the
    name it will have — "_2", "_3" ... added when a run of that name is
    already in its folder — and each camera of it (RunCamera, by name)."""
    base: str
    cameras: Mapping[str, RunCamera] = field(default_factory=lambda:
                                             MappingProxyType({}))

    @property
    def folders(self) -> Mapping[str, Optional[Path]]:
        """Each camera's capture folder, by camera name."""
        return MappingProxyType({name: cam.folder
                                 for name, cam in self.cameras.items()})


@dataclass(frozen=True)
class RunInfo:
    """A run, as on_run_started and on_run_finished see it (design 3.5,
    3.11): its reserved name, its folders by camera name, when it started
    (seconds since 1970) and — at on_run_finished only — when its
    recording ended and each camera's counts from its end record
    (`<run>_run_end.json`: frames_saved, frames_not_saved, grab_failures,
    grab_attempts, first_index, last_index, recording_failed, ...)."""
    run: str
    folders: Mapping[str, Path] = field(default_factory=lambda:
                                        MappingProxyType({}))
    started: float = 0.0
    ended: Optional[float] = None
    counts: Mapping[str, Mapping[str, Any]] = field(
        default_factory=lambda: MappingProxyType({}))

    @property
    def cameras(self) -> Tuple[str, ...]:
        return tuple(self.folders)


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

    def create_file(self, folder: Path, name: str, *,
                    binary: bool = False) -> Any:
        """(a) STREAMED, during a run: a new file `name` in `folder`, one
        of the run in progress's folders, opened for writing — text
        (UTF-8, newline="") or `binary` — and made EXCLUSIVELY: a file of
        that name already there is refused (PluginFileError), never
        opened. `name` is "<run><ending>", the run in progress's name and
        an ending this plugin's manifest declares in "files". Close it by
        on_run_finished; Typhon closes what is left open then."""

    def write_new(self, folder: Path, name: str, data: bytes) -> Path:
        """(b) WHOLE, at any time: `data` written under a temporary name of
        its own beside it, then renamed to `name` with a rename that
        REFUSES an existing name — "<stem>_2<ext>", "_3" ... on a rerun —
        so nothing is ever written over. Returns the path written. `folder`
        holds the run `name` belongs to (`name` is "<run><ending>", an
        ending the manifest declares), past runs included, or is this
        plugin's data_dir(), where any plain file name but code goes."""

    def data_dir(self) -> Path:
        """This plugin's own data folder in this project,
        `<project>/plugin_data/<id>/`, made on first ask; files go in it
        through write_new (design 4.1)."""

    def capture_setup(self, camera: Optional[str] = None) -> Any:
        """A copy of the camera's capture set-up (a CaptureSetup: trigger
        mode, source, frames per trigger, timer, edge, delay, debounce,
        Downsample) as the camera has it now — this plugin's camera, or the
        active one — or None for a camera with none (an event camera) or no
        camera. Read in place: the camera's set-up is the storage while the
        code stays where it is (design 2.5); a change goes through the
        camera's settings (its "capture.*" keys), never through this copy."""


@runtime_checkable
class FeaturePlugin(Protocol):
    """A feature plugin (design 3.5). Every method is optional; Typhon calls
    the ones a plugin has: `activate` and `deactivate`; where it is fed live
    frames, `on_frame`; with "run_hooks" in its manifest's contributes
    (stage P3), `check_start(plan) -> str` (a reason refuses Start; ""
    or None lets it go), `on_run_started(run)` and `on_run_finished(run) ->
    dict | None` (its block in the run's end record); with "presets",
    `preset_values(camera) -> dict` and `apply_preset_values(camera,
    values)`. The screen points come at stage P4.

    THE FAULT RULE (design 3.10). Every call is guarded: whatever it
    raises switches the plugin off for the session, said and logged. The
    run hooks, record_fields and the preset hooks are BEST EFFORT — the
    fault is said in Start's summary or the run's end record and the
    capture carries on; only check_start refuses a Start. On the RECORDING
    PATH (a record gate, a frame stage: built-in plugins only) a fault ends
    the recording, with the reason in the end record."""

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
        """The newest file of this source in `folder`, by run name — in
        the slider's own order (council_qt.widgets.capture_review.
        latest_raw: file names, naturally sorted), which it keeps until P4.
        That order puts "<run>_events.raw" after "<run>_2_events.raw", two
        runs started in one second: today's slider's, to be fixed there."""
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
    (design 3.4). `facet(device, name)` gives the camera's facet for a
    capability name (FACETS: SoftwareTrigger, HardwareTrigger, Binning) —
    the interface, even when this camera cannot do it (its `problem()`
    says why) — or None when the camera has none (from stage P3).
    `categories()` gives the settings tabs its rows;
    `record_fields(device)` plain JSON for its entry of the run's camera
    record (stage P3: asked at Start, cleaned and size-checked, best
    effort); `review_source()` a ReviewSource, or None."""

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
# The facets (stage P3 rows 2 and 3; design 3.4, 3.11)
# ======================================================================
@runtime_checkable
class SoftwareTrigger(Protocol):
    """A camera Typhon can fire (capability `software_trigger`), for an
    exact run from Software or the Timer (design 3.6). Today's Device
    methods (cameras.Device: trigger_problem, arm_trigger, trigger_ready,
    fire_trigger, disarm_trigger) regrouped:

      problem(setup)     why the camera cannot be armed for `setup` (a
                         CaptureSetup), in words, or "" — asked at Start
                         before anything stops, as today
      arm(setup)         arm the camera's own trigger for an exact run (the
                         stream stopped); what disarm puts back
      ready(timeout_s)   is the camera waiting for its next trigger? Waits
                         up to `timeout_s`; None when it cannot say
      fire()             one software trigger
      disarm(armed)      put back what arm changed; what changed, in words

    A camera's own refusal is a CameraError (raised by arm, fire)."""

    def problem(self, setup: Any) -> str: ...

    def arm(self, setup: Any) -> Dict[str, Any]: ...

    def ready(self, timeout_s: float) -> Optional[bool]: ...

    def fire(self) -> None: ...

    def disarm(self, armed: Any) -> List[str]: ...


@runtime_checkable
class HardwareTrigger(Protocol):
    """A camera that waits for an input line (capability
    `hardware_trigger`), for an exact run from a camera line (design 3.6):

      inputs()                 what the camera offers: a CaptureOptions
                               (its hardware sources, edges, delay and
                               debounce ranges, and Downsample's own words)
      problem(setup), arm(setup), ready(timeout_s), disarm(armed)
                               as SoftwareTrigger's
      count_edges(armed, seen) the camera's own count of edges on the armed
                               input between count_from_now and
                               count_to_now: (count, "") or (None, why not)
                               — also when it cannot cover the `seen`
                               triggers Typhon saw
      count_from_now(armed)    start the count (the stream runs, the camera
                               ready)
      count_to_now(armed)      end it, before the stream stops; the camera
                               takes no more hardware triggers"""

    def inputs(self) -> Any: ...

    def problem(self, setup: Any) -> str: ...

    def arm(self, setup: Any) -> Dict[str, Any]: ...

    def ready(self, timeout_s: float) -> Optional[bool]: ...

    def disarm(self, armed: Any) -> List[str]: ...

    def count_edges(self, armed: Any, seen: Optional[int] = None
                    ) -> Tuple[Optional[int], str]: ...

    def count_from_now(self, armed: Any) -> None: ...

    def count_to_now(self, armed: Any) -> None: ...


@runtime_checkable
class Binning(Protocol):
    """A camera that averages its own pixels (capability `binning`): the
    camera half of Downsample (design 2.1). Downsample itself is the
    core's; the core hands its factor to this facet or to its own frame
    stage.

      can_bin(factor)  does the camera bin `factor` x `factor` in AVERAGE
                       mode (never Sum, which clips)?
      apply(factor)    Downsample `factor` — the camera's binning where it
                       can, else Typhon's averaging — and what was done
      check(rebin)     the binning checked against Downsample: "" when it
                       is as Typhon left it, else what was done about it
      info()           how Downsample is made now: factor, method, note"""

    def can_bin(self, factor: int) -> bool: ...

    def apply(self, factor: int) -> str: ...

    def check(self, rebin: bool = False) -> str: ...

    def info(self) -> Dict[str, Any]: ...


#: Each facet by the capability it serves (design 3.4's table).
FACETS: Dict[str, Any] = {"software_trigger": SoftwareTrigger,
                          "hardware_trigger": HardwareTrigger,
                          "binning": Binning}


# ======================================================================
# Trigger sources and toolbar actions (design 3.6, 3.7, 3.11)
# ======================================================================
@dataclass(frozen=True)
class TriggerEvent:
    """One trigger a source made: its source's name, a running number (1,
    2, ... in the order made) and the host time (seconds since 1970)."""
    source: str
    number: int
    at: float


@runtime_checkable
class TriggerSource(Protocol):
    """What says "capture now" (design 3.6). `camera_line`: True for a
    source wired to the camera, which Typhon has nothing to fire for.
    `needs(mode)` is what a run of that mode needs of the camera: a
    snapshot from Typhon's own sources needs frames only, an exact run a
    trigger facet. `start(fire)`: from now on each trigger the source makes
    is told to `fire` (a TriggerEvent; it must return at once); `stop()`
    returns within a moment, and nothing is fired after it."""

    name: str
    camera_line: bool

    def needs(self, mode: str = "Triggered: snapshot") -> FrozenSet[str]: ...

    def start(self, fire: Callable[[TriggerEvent], None]) -> None: ...

    def stop(self) -> None: ...


@dataclass(frozen=True)
class Action:
    """A toolbar button (design 3.7, 3.11): its text, its callback (called
    on the UI thread), a tooltip, and an optional application-wide
    shortcut ("F9")."""
    text: str
    callback: Callable[[], Any]
    tooltip: str = ""
    shortcut: str = ""


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
    # what the two file services refuse with (stage P3 row 1)
    "PluginFileError": ("council_core.plugin_runs", "PluginFileError"),
    # the capture set-up's types, the trigger modes and Typhon's own
    # sources (stage P3 rows 2 and 3; design 2.5): today's, in
    # council_core/triggered.py until the files move at P7
    "CaptureSetup": ("council_core.triggered", "CaptureSetup"),
    "CaptureOptions": ("council_core.triggered", "CaptureOptions"),
    "MODE_CONTINUOUS": ("council_core.triggered", "MODE_CONTINUOUS"),
    "MODE_EXACT": ("council_core.triggered", "MODE_EXACT"),
    "MODE_SNAPSHOT": ("council_core.triggered", "MODE_SNAPSHOT"),
    "MODES": ("council_core.triggered", "MODES"),
    "SOFTWARE": ("council_core.triggered", "SOFTWARE"),
    "TIMER": ("council_core.triggered", "TIMER"),
    "TYPHON_SOURCES": ("council_core.triggered", "TYPHON_SOURCES"),
}

__all__ = sorted(["API_VERSION", "FEATURES", "EVENTS", "STATUS_MAX", "has",
                  "CameraHandle", "CropBox", "FrameView", "PluginContext",
                  "FeaturePlugin", "CameraPlugin", "CameraSetup",
                  "CategoryHints", "ReviewSource", "RunCamera", "RunPlan",
                  "RunInfo", "SoftwareTrigger", "HardwareTrigger", "Binning",
                  "FACETS", "TriggerEvent", "TriggerSource", "Action",
                  "TYPHON_DOWNSAMPLE", "CAMERA_DOWNSAMPLE", *_REEXPORTS])


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
