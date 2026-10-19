"""
typhon.trigger — Triggering: Software and Timer trigger sources, exact and
snapshot capture, the record gate and the triggers CSV
(docs/typhon_plugins_design.md 2.3, 3.6).

STAGE P3 ROW 2: AN ADAPTER OVER TODAY'S CODE (design 7.2)
The code stays where it is until stage P7: council_core/triggered.py (a
run's books, TriggerRun; the set-up's rows) and frame_camera's trigger
functions (arming, the edge count, closing the books, the words), reached
through typhon_plugin_api.builtin. This plugin calls them with a camera MADE
OF ITS FACETS (`FacetCamera`: the camera's SoftwareTrigger and
HardwareTrigger, design 3.4), never with the Device — so a run of "Typhon
(plugins)" does exactly what today's does, and only the calls change.

THE CORE OWNS THE ORDER; THIS FILLS ITS SLOT (design 3.6)
Start: check_start (what the run needs, per mode and source — before
anything stops) -> the run's name reserved -> `trigger_slot(camera)`: one
RunTriggers for the run -> arm (an exact run: the camera's trigger, through
its facet) -> the camera record's capture block -> the record gate (the
run's books, TriggerRun, its triggers CSV made through the file service) ->
the recording -> ready (the camera's edge count from its first ready) ->
on_run_started -> start (Typhon's own source: Software, Timer). Stop: halt
(the source stops; the last frames are waited for; the camera's input
closed) -> run_stopped -> the stream stops -> close (the books closed, the
edges counted) -> the recording ends -> disarm. The host wraps each step
(council_core.plugin_host.TriggerSlot) and serves out a run in progress
even after a best-effort fault, so Stop always puts the camera back.

WHAT A RUN NEEDS (design 3.6): a snapshot from Software or the Timer needs
frames only (Typhon keeps the frames after each trigger and arms nothing);
a snapshot from a camera line is refused; an exact run from Software or the
Timer needs the camera's software trigger, from a camera line its hardware
trigger with that line among its inputs. A camera with a trigger facet is
asked its own words (the facet's `problem`), as today's Start asked it.

ITS KEYS are today's, "capture.*" (triggered.py:81-88), read and written in
place: the camera's set-up stays the storage today's code reads
(device.capture, design 2.5), read through `ctx.capture_setup()` and
written as the Basic tab's boxes write it (frame_camera.set_camera_setting:
refused while capturing, the views told). The trigger columns of the frames
CSV keep their names (capture.INDEX_COLUMNS).

ITS SCREEN (built now, shown from stage P4): `settings_rows()` (the trigger
rows of the set-up, Downsample being the core's), `panel(parent)` (those
rows, Capture now and the status), `toolbar()` (Capture now, F9) and
`status()` (the status segment, at most 80 characters). Until P4 the window
shows today's trigger boxes and Capture now button.
"""
from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

import typhon_plugin_api as api
from typhon_plugin_api import builtin

#: Typhon's own sources, and which capability an exact run of each needs.
SOFTWARE_TRIGGER = "software_trigger"
HARDWARE_TRIGGER = "hardware_trigger"
FRAMES = "frames"


def setup_of(values: Mapping[str, Any]) -> Any:
    """A CaptureSetup from a RunPlan camera's `capture` (its plain values:
    CaptureSetup.as_record()), or None for a camera with none."""
    if not values:
        return None
    setup = api.CaptureSetup()
    for key in ("mode", "source"):
        if values.get(key) is not None:
            setattr(setup, key, str(values[key]))
    for key, kind in (("per_trigger", int), ("every_s", float),
                      ("downsample", int)):
        if values.get(key) is not None:
            setattr(setup, key, kind(values[key]))
    setup.edge = str(values.get("edge") or "")
    for key in ("delay_us", "debounce_us"):
        value = values.get(key)
        setattr(setup, key, None if value is None else float(value))
    return setup


class FacetCamera:
    """One camera's trigger facets under the names today's trigger code
    calls on a camera (frame_camera's arming and edge count, TriggerRun's
    firing): trigger_problem, arm_trigger, disarm_trigger, trigger_ready,
    fire_trigger, count_edges, count_from_now, count_to_now — so that code
    runs unchanged with the facets in place of the Device. Arming, the
    camera's check and putting it back go to the facet the run's source
    needs (`hardware`: a camera line); a camera with no such facet answers
    as today's base Device does."""

    def __init__(self, software: Any = None, hardware: Any = None,
                 line: bool = False) -> None:
        self.software = software
        self.hardware = hardware
        self.line = bool(line)

    def _arming(self) -> Any:
        return self.hardware if self.line else self.software

    def trigger_problem(self, setup: Any) -> str:
        facet = self._arming() or self.software or self.hardware
        if facet is None:
            return "this camera cannot be triggered from Typhon"
        return str(facet.problem(setup) or "")

    def arm_trigger(self, setup: Any) -> Any:
        facet = self._arming()
        if facet is None:
            raise api.CameraError("this camera cannot be triggered from "
                                  "Typhon")
        return facet.arm(setup)

    def disarm_trigger(self, armed: Any) -> List[str]:
        facet = self._arming()
        return list(facet.disarm(armed) or []) if facet is not None else []

    def trigger_ready(self, timeout_s: float = 0.0) -> Optional[bool]:
        facet = self._arming() or self.software or self.hardware
        return facet.ready(timeout_s) if facet is not None else None

    def fire_trigger(self) -> None:
        if self.software is None:
            raise api.CameraError("this camera has no software trigger")
        self.software.fire()

    def count_edges(self, armed: Any, seen: Optional[int] = None) -> Any:
        if self.hardware is None:
            return None, "the camera has no edge counter Typhon can use"
        return self.hardware.count_edges(armed, seen)

    def count_from_now(self, armed: Any) -> None:
        if self.hardware is not None:
            self.hardware.count_from_now(armed)

    def count_to_now(self, armed: Any) -> None:
        if self.hardware is not None:
            self.hardware.count_to_now(armed)


class _Grabbed:
    """What frame_camera's wait for the last frames reads of the session
    (`stats().grabbed`), from the count the core hands the plugin."""

    def __init__(self, grabbed: Callable[[], int]) -> None:
        self._grabbed = grabbed

    def stats(self) -> Any:
        class _Now:
            grabbed = int(self._grabbed())
        return _Now()


# ======================================================================
# Typhon's own sources (design 3.6)
# ======================================================================
class TyphonSource:
    """Software or the Timer: one of Typhon's own trigger sources
    (typhon_plugin_api.TriggerSource), for one run's books.

    THE BOOKS KEEP THE SOURCE: TriggerRun's worker keeps the Timer's
    schedule and fires the Software presses, as today — so a tick that comes
    while the camera is still busy is counted missed exactly as today, and
    a press is fired once the camera says it is ready. `start(fire)` starts
    that worker and tells `fire` each trigger it makes (a TriggerEvent:
    missed ones too); `stop()` halts it, within its own bound, and nothing
    is fired after it. A source belongs to one run: once stopped it stays
    stopped (a new run makes new sources)."""

    camera_line = False

    def __init__(self, name: str, books: Any = None) -> None:
        self.name = str(name)
        self._books = books
        self._fire: Optional[Callable[[Any], None]] = None
        self._started = False
        self._stopped = False
        self._lock = threading.Lock()

    def needs(self, mode: str = api.MODE_SNAPSHOT) -> frozenset:
        """A snapshot needs frames only; an exact run the camera's software
        trigger (design 3.6)."""
        if str(mode) == api.MODE_EXACT:
            return frozenset({FRAMES, SOFTWARE_TRIGGER})
        return frozenset({FRAMES})

    @property
    def running(self) -> bool:
        books = self._books
        return bool(self._started and not self._stopped and books is not None
                    and books.running)

    def _told(self, source: str, number: int, at: float) -> None:
        fire = self._fire
        if fire is not None and not self._stopped:
            fire(api.TriggerEvent(source=source, number=int(number),
                                  at=float(at)))

    def start(self, fire: Callable[[Any], None]) -> None:
        with self._lock:
            if self._started or self._stopped or self._books is None:
                return
            self._started = True
            self._fire = fire
        self._books.on_trigger = self._told
        self._books.start()

    def press(self) -> str:
        """A Capture now press (Software): the words the books give."""
        if self._books is None or self._stopped:
            return "the run is stopping"
        return str(self._books.capture_now())

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._fire = None
        books = self._books
        if books is not None and self._started:
            books.halt()


# ======================================================================
# One run of one camera: the plugin's part of the core's order
# ======================================================================
class RunTriggers:
    """The triggering plugin's part of ONE run on one camera: what each step
    of the core's Start and Stop does (design 3.6), and the run's books
    (`books`, a TriggerRun) once a triggered run has them. Kept in the
    plugin's per-camera state (`ctx.state(camera)["run"]`) while the run
    lasts; the books of the last run stay there for what is said after
    Stop."""

    def __init__(self, plugin: "TriggerPlugin", camera: str) -> None:
        self.plugin = plugin
        self.camera = str(camera)
        ctx = plugin.ctx
        handle = ctx.cameras().get(self.camera) if ctx is not None else None
        self.software = handle.facet(SOFTWARE_TRIGGER) if handle else None
        self.hardware = handle.facet(HARDWARE_TRIGGER) if handle else None
        self.setup: Any = None
        self.armed: Any = None
        self.books: Any = None
        self.sources: List[TyphonSource] = []
        #: From the moment its source started until Stop began: what
        #: Capture now fires and the status segment says (frame_camera's
        #: "capturing, with a triggered run").
        self.live = False

    def _camera(self, setup: Any = None) -> FacetCamera:
        setup = setup if setup is not None else self.setup
        line = bool(setup is not None and getattr(setup, "triggered", False)
                    and not getattr(setup, "typhon_fires", True))
        return FacetCamera(self.software, self.hardware, line=line)

    # -- Start ------------------------------------------------------------
    def arm(self, setup: Any) -> Any:
        """An exact run: the camera's own trigger armed through its facet
        (frame_camera._arm, in today's words when it refuses)."""
        self.setup = replace(setup)
        self.armed = builtin.arm(self._camera(setup), setup)
        return self.armed

    def record(self, run: str, setup: Any, armed: Any) -> Any:
        """The camera record's capture block (format 1's "capture", which
        this plugin fills, design 2.4 item 8)."""
        return builtin.capture_record(run, setup, armed)

    def gate(self, run: str, folder: Any, setup: Any,
             values: Mapping[str, Any], rate: float) -> Any:
        """The run's books (TriggerRun) — the record gate — with its
        triggers CSV made through the file service: exclusively, in the
        run's folder, under today's name."""
        self.setup = replace(setup)
        exposure_s, period_s = builtin.trigger_timing(values, rate)
        name = f"{run}{builtin.TRIGGERS_SUFFIX}"
        handle = self.plugin.ctx.create_file(Path(str(folder)), name)
        books = builtin.TriggerRun(setup, self._camera(setup),
                                   Path(str(folder)) / name,
                                   exposure_s=exposure_s,
                                   frame_period_s=period_s)
        try:
            books.open(handle)
        except BaseException:
            handle.close()
            raise
        self.books = books
        self.sources = [TyphonSource(api.SOFTWARE, books),
                        TyphonSource(api.TIMER, books)]
        return books

    def words(self, setup: Any, armed: Any) -> str:
        """What Start says of the trigger set-up (frame_camera's words)."""
        return builtin.trigger_start_words(setup, armed)

    def ready(self) -> None:
        """The stream runs: an exact run from a camera line starts the
        camera's edge count once it is ready (frame_camera.
        _count_from_ready)."""
        books = self.books
        if books is not None and self.armed is not None and \
                not books.typhon_fires:
            builtin.count_from_ready(self._camera(), self.armed)

    def start(self) -> None:
        """Typhon's own source starts — the run's books' worker (Software:
        it fires each Capture now; Timer: its schedule)."""
        books = self.books
        if books is None:
            return
        source = self.source()
        if source is not None:
            source.start(lambda event: None)
        else:
            # The camera's own input: nothing of Typhon's to fire; the
            # books' worker still settles each trigger's deadline.
            books.start()
        self.live = True

    def source(self) -> Optional[TyphonSource]:
        """The run's own source (Software or Timer), or None for a camera
        line."""
        books = self.books
        if books is None or not books.typhon_fires:
            return None
        name = api.TIMER if books.timer else api.SOFTWARE
        return next((s for s in self.sources if s.name == name), None)

    def undo(self) -> None:
        """A Start that failed after arming: the triggers CSV closed (it
        stays, header only), the camera put back."""
        self.live = False
        books = self.books
        if books is not None:
            try:
                books.close()
            except Exception:                             # noqa: BLE001
                pass
        if self.armed is not None:
            builtin.disarm(self._camera(), self.armed)
            self.armed = None

    # -- Stop ---------------------------------------------------------------
    def halt(self, grabbed: Callable[[], int]) -> None:
        """Fire nothing more, give a trigger fired a moment ago its frame,
        and — an exact run from a camera line — end the edge count before
        the stream stops and let the frames already exposed land."""
        self.live = False
        books = self.books
        if books is None:
            return
        for source in self.sources:
            source.stop()
        books.halt()
        books.wait_pending(min(builtin.STOP_TRIGGER_WAIT_SECONDS,
                               books.exposure_s + 0.5))
        armed = self.armed
        if armed is not None and not books.typhon_fires:
            camera = self._camera()
            try:
                camera.count_to_now(armed)
            except Exception:                             # noqa: BLE001
                pass
            if isinstance(armed, dict) and armed.get("counter"):
                builtin.let_frames_land(_Grabbed(grabbed), camera, books)

    def close(self) -> None:
        """The books closed before the recording ends, so its end record
        has every trigger: the camera's edge count first (frame_camera.
        _end_triggers)."""
        if self.books is not None:
            builtin.end_triggers(self._camera(), self.books, self.armed)

    def disarm(self) -> str:
        """Every trigger back as it was before Start; what was NOT put
        back, or ""."""
        armed, self.armed = self.armed, None
        if armed is None:
            return ""
        return builtin.disarm(self._camera(), armed)

    def abort(self) -> None:
        """Disconnect in the middle of the run: the books stopped, the
        camera's triggers put back (as today's Disconnect does)."""
        self.live = False
        for source in self.sources:
            source.stop()
        if self.books is not None:
            self.books.stop()
        armed, self.armed = self.armed, None
        if armed is not None:
            try:
                self._camera().disarm_trigger(armed)
            except Exception:                             # noqa: BLE001
                pass
        # The camera is going: the plugin keeps nothing of it.
        state = self.plugin._state(self.camera)
        if state.get("run") is self:
            del state["run"]

    def capture_now(self) -> str:
        """A Capture now press during the run: today's words."""
        books = self.books
        if books is None:
            return ""
        source = self.source()
        said = source.press() if source is not None else books.capture_now()
        return f"Capture now: {said}."


# ======================================================================
# The plugin
# ======================================================================
class TriggerPlugin:
    """Triggering (feature plugin, rig scope; design 2.3, 3.6)."""

    def __init__(self) -> None:
        self.ctx: Optional[api.PluginContext] = None

    def activate(self, ctx: api.PluginContext) -> None:
        self.ctx = ctx

    def deactivate(self) -> None:
        self.ctx = None

    # -- per camera -----------------------------------------------------------
    def _camera(self, camera: Optional[str]) -> Optional[str]:
        if camera:
            return str(camera)
        return self.ctx.active_camera() if self.ctx is not None else None

    def _state(self, camera: Optional[str]) -> Dict[str, Any]:
        name = self._camera(camera)
        if self.ctx is None or not name:
            return {}
        try:
            return self.ctx.state(name)
        except RuntimeError:
            return {}

    def _run(self, camera: Optional[str]) -> Optional[RunTriggers]:
        return self._state(camera).get("run")

    def _facets(self, camera: str) -> Dict[str, Any]:
        handle = self.ctx.cameras().get(camera) if self.ctx else None
        if handle is None:
            return {}
        out = {}
        for name in (SOFTWARE_TRIGGER, HARDWARE_TRIGGER):
            facet = handle.facet(name)
            if facet is not None:
                out[name] = facet
        return out

    # -- what a run needs (design 3.6) -----------------------------------
    def problem(self, camera: str, setup: Any,
                capabilities: Optional[Any] = None,
                facets: Optional[Mapping[str, Any]] = None) -> str:
        """Why a run of `setup` cannot be made on `camera`, or "" (design
        3.6). A camera with a trigger facet is asked its own words — the
        facet the run needs, else the other: the camera's check is today's
        trigger_problem — so the refusal reads as today's; a camera with
        none is held to the rule: snapshot from Software or the Timer
        needs frames only, a snapshot from a camera line is refused, an
        exact run needs the trigger facet its source needs."""
        if setup is None or not setup.triggered:
            return ""
        if facets is None:
            facets = self._facets(camera)
        caps = frozenset(capabilities or ())
        line = not setup.typhon_fires
        needed = HARDWARE_TRIGGER if line else SOFTWARE_TRIGGER
        facet = facets.get(needed) or facets.get(
            SOFTWARE_TRIGGER if line else HARDWARE_TRIGGER)
        if facet is not None:
            why = str(facet.problem(setup) or "")
        elif setup.snapshot and line:
            why = (f"Snapshot takes its trigger from Software (Capture now) "
                   f"or the Timer; {setup.source} is the camera's own input, "
                   f"which only an exact run waits for — choose Triggered: "
                   f"exact, or Software / Timer")
        elif setup.snapshot:
            why = ""                      # frames only: nothing is armed
        elif needed not in caps:
            why = (f"this camera has no {needed.replace('_', ' ')} Typhon can "
                   + ("arm (it offers no hardware trigger input)" if line
                      else "fire (it offers no software trigger)")
                   + " — choose Triggered: snapshot from Software or the "
                     "Timer, which needs no trigger on the camera")
        else:
            why = ""
        if not why and setup.source == api.TIMER and setup.every_s <= 0:
            why = "the timer's interval must be more than 0 s"
        return why

    def check_start(self, plan: Any) -> str:
        """Every camera of the run, asked what its mode and source need
        (design 3.6), before anything stops; a reason refuses Start."""
        reasons = []
        for name, camera in plan.cameras.items():
            setup = setup_of(camera.capture)
            if setup is None or not setup.triggered:
                continue
            why = self.problem(name, setup, camera.capabilities)
            if why:
                head = f"cannot start a {setup.short()} run"
                if len(plan.cameras) > 1:
                    head += f" on {name}"
                reasons.append(f"{head}: {why}")
        return "; ".join(reasons)

    def on_run_started(self, run: Any) -> None:
        """Nothing of its own: the source starts at its own step, after
        every plugin was told (design 3.6)."""

    def on_run_finished(self, run: Any) -> None:
        """Nothing for the end record's "plugins": the run's capture block
        is format 1's own (design 3.9)."""
        return None

    # -- the trigger slot (the core's order, design 3.6) ---------------------
    def trigger_slot(self, camera: str) -> RunTriggers:
        """The plugin's part of the run about to start on `camera`."""
        made = RunTriggers(self, camera)
        self._state(camera)["run"] = made
        return made

    def sources(self, camera: Optional[str] = None) -> List[TyphonSource]:
        """Typhon's own sources: those of the run in progress on `camera`,
        else ones of no run (not running)."""
        run = self._run(camera)
        if run is not None and run.sources:
            return list(run.sources)
        return [TyphonSource(api.SOFTWARE), TyphonSource(api.TIMER)]

    # -- Capture now (F9), the status segment --------------------------------
    def capture_now(self, camera: Optional[str] = None) -> str:
        """Capture now, the toolbar's button and F9: one trigger of the run
        in progress, or why there is nothing to fire — today's words."""
        run = self._run(camera)
        if run is not None and run.live and run.books is not None:
            return run.capture_now()
        name = self._camera(camera)
        setup = (self.ctx.capture_setup(name)
                 if self.ctx is not None else None)
        if setup is None and self._kind(name) != "event":
            # Today's words name an event camera for a camera with no
            # set-up; a frame camera has the core's from P3's review on
            # (council_core.plugin_capture), so this is only a camera that
            # is not there (P3 review: a frame camera was called an event
            # camera).
            return ("Capture now is for a triggered capture of a frame "
                    "camera — connect one, choose Capture: Triggered: exact "
                    "or Triggered: snapshot, then Start.")
        return builtin.capture_now_idle(setup)

    def _kind(self, camera: Optional[str]) -> str:
        """"frame", "event", or "" for no camera open there."""
        handle = (self.ctx.cameras().get(camera)
                  if self.ctx is not None and camera else None)
        return str(getattr(handle, "kind", "") or "")

    def status(self, camera: Optional[str] = None) -> str:
        """The status segment (design 3.5, 3.7): the run's triggers while
        one is armed — at most STATUS_MAX characters — else ""."""
        run = self._run(camera)
        if run is None or not run.live or run.books is None:
            return ""
        return short(run.books.line())

    # -- the screen (built now; the window asks it from stage P4) -----------
    def settings_rows(self, camera: Optional[str] = None) -> List[Any]:
        """The trigger rows of the camera's set-up (no Downsample: the
        core's), as today's Capture group shows them, read in place."""
        name = self._camera(camera)
        setup = self.ctx.capture_setup(name) if self.ctx else None
        if setup is None:
            return []
        hardware = self._facets(name).get(HARDWARE_TRIGGER) if name else None
        options = None
        if hardware is not None:
            try:
                options = hardware.inputs()
            except Exception:                             # noqa: BLE001
                options = None
        return builtin.trigger_settings(setup, options or
                                        api.CaptureOptions())

    def write_setting(self, key: str, value: Any) -> Dict[str, Any]:
        """One of the set-up's "capture.*" keys written in place, as the
        Basic tab's boxes write it: refused while capturing, the views
        told. The active camera's (frame_camera's one camera)."""
        if not str(key).startswith("capture."):
            raise KeyError(f"{key!r} is not the triggering plugin's: its keys "
                           f"are capture.*")
        return builtin.set_camera_setting(key, value)

    def toolbar(self) -> List[api.Action]:
        """Capture now, on F9 (design 3.7)."""
        return [api.Action(text="Capture now (F9)",
                           callback=lambda: self.capture_now(),
                           tooltip="Fire one trigger of the triggered run "
                                   "(F9 does the same)",
                           shortcut="F9")]

    def panel(self, parent: Any = None) -> Any:
        """The triggering pop-out's inner widget (design 3.7): the trigger
        rows, Capture now and the status. Built on first open; not shown in
        today's window (stage P4)."""
        from .panel import TriggerPanel
        return TriggerPanel(self, parent)


def short(line: str, limit: int = api.STATUS_MAX) -> str:
    """`line` cut to `limit` characters at a " · " where it can be, else
    with an ellipsis."""
    text = " ".join(str(line or "").split())
    if len(text) <= limit:
        return text
    parts = text.split(" · ")
    while len(parts) > 1 and len(" · ".join(parts)) > limit:
        parts.pop()
    text = " · ".join(parts)
    return text if len(text) <= limit else text[:limit - 1] + "…"
