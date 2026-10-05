"""
frame_camera.py — a live camera for a generated GUI, in the shape its handlers
already expect.

WHY THIS MODULE EXISTS AT ALL
The camera layer lives in council_core/cameras.py, and a generated app may not
import it: gui_policy.is_council_module("council_core") is True, so every
spelling of that import is refused, and declaring it in a project's `requires`
is itself a validation error. The allowlist that a linked app CAN reach is
LINKED_MODULES, and the three modules Barbie already uses — frame_timing,
frame_roi, frame_classes — are top-level modules beside this one. So this is a
fourth member of that family: a thin, plain-function wrapper that puts the
camera on the allowlist without putting the whole of council_core there.

THE HANDLER CONTRACT, WHICH IS WHY EVERY FUNCTION RETURNS A DICT
The emitter generates a handler per script link, shaped exactly like this:

    result = add_class(self.ports.classifier_name.get(), ...)
    if isinstance(result, dict) and result.get('error'):
        raise RuntimeError(result['error'])
    self.ports.classes.set(result["classes"])

so a function here is called with PORT VALUES (strings), returns a DICT whose
keys are written straight back to ports, and reports failure by raising or by a
non-empty 'error'. Nothing here takes a widget, because a script link cannot
pass one.

THE LIVE VIEW IS A PULL, NOT A PUSH — AND THAT IS THE WHOLE TRICK
A generated Qt app has NO thread-to-UI marshalling for handler code. The
emitter says so itself (gui_emit_qt.py:429: "The reader thread never touches Qt
— a widget touched from a non-GUI thread is undefined behaviour"), and there is
no _to_ui, no queue drain and no Signal that handlers.py can reach. A grab loop
calling port.set() from its own thread is therefore undefined behaviour, and
that is the blocker that has kept cameras out of generated apps.

So the grab thread never calls anything here. It drops each frame into a
one-slot mailbox and returns. `latest()` and `pump()` TAKE from that mailbox,
and both are meant to be called from a QTimer, which runs on the UI thread.
Nothing ever touches a widget off-thread, and the missing marshalling API stops
mattering instead of being worked around.

WHERE THE FRAMES GO, AND WHY THAT IS THE SAME FOLDER YOU BROWSE
The capture folder IS the browse folder. Each run writes, side by side:

    <run>_frame_000001.png ...   what the camera looked like, one per frame
    <run>_frames.csv             one row per saved PNG: when, how many events,
                                 and where it falls in the .raw
    <run>_events.raw             EVENT CAMERAS ONLY: every event the sensor
                                 sent, in Prophesee's own format
    <run>_camera.json            which camera, where on its sensor (the
                                 camera's own area, sensor pixels), every
                                 setting at Start, the preset if it was
                                 still as applied, the app and the software
                                 (council_core.camera_record) — written once

PNG specifically, because frame_timing, frame_roi and frame_classes all
discover frames by IMAGE_SUFFIXES and open them with Pillow — a capture
written as .npy is invisible to every one of them. The .raw is the event
camera's real data: a PNG is a 20 ms picture of it, and PNGs may be skipped
when storage falls behind, but the .raw is written as the SDK hands the bytes
over and loses nothing.

Save to a LOCAL folder and copy the run afterwards. A network share could not
keep up with a real EVK4.

CONNECTED MEANS LIVE
Connected and not capturing, the camera streams and the picture shows what it
sees — to aim, focus and draw the camera's area by — while nothing is written
anywhere: no PNG, no CSV, no .raw. Whatever the folder holds: the slider's
END is the live picture and dragging back reviews the saved frames (the DVR
rule, capture_review). Start turns the live view into a capture; Stop turns it
back. Only the raw view turns it off. Only apps with a capture slider (Typhon)
get it; see `_manage_preview`.

A FRAME CAMERA IS STOPPED AND STARTED; AN EVK4'S STREAM NEVER IS
A Basler's grabs are independent, so its live view stops when there is
nothing to show it on, and Start restarts it for the capture. An EVK4's stream
must not be restarted on the same connection (Device.restartable, and
EvkDevice for the measurements): it starts once and runs until Disconnect. Its
capture is the .raw opening and closing inside the running stream, and when
the live view is not wanted it simply is not drawn.

THE CAMERA'S AREA IS NOT THE CROP BOX
Two boxes, two coordinate systems, kept apart:
  * the CROP BOX (the "roi" port) is in IMAGE pixels of the picture it was
    drawn on; "Save cropped frames" (frame_roi) cuts saved frames with it;
  * the CAMERA'S AREA is in SENSOR pixels — the camera's own AOI/ROI, so an
    EVK4 emits only there and a Basler reads only that out (`current_area`).
The live picture of a camera with an area IS that area, so a box drawn on it
is turned into sensor pixels by adding the origin of the area the shown frame
was taken with (Frame.meta["aoi"]) — `set_area`. The live view is shown at
full resolution, never decimated, so there is no display scale to undo. The
camera's area is never written back into the crop box.

ONE SET-UP PER RUN
The camera's area, a preset and any setting the stream is in the way of are
REFUSED while capturing, on every camera: a Basler would restart into the
same run with frames of two sizes, and an EVK4's .raw would change meaning
halfway. Settings that change live (exposure, gain, a bias) still apply
mid-capture, as the FPS box always has. See `_refuse_while_capturing`.

CHANGES THAT NEED THE STREAM STOPPED DO NOT FREEZE THE WINDOW
A Basler's area or pixel format needs its grab loop stopped (which waits for
the frame in flight, up to a second on a slow camera) and started again. That
runs on a worker (`_Job`); the caller waits up to APPLY_WAIT_SECONDS for it
and gets the full answer when it is quick — the usual case — or "applying…"
and the answer in the status line, and to every `on_camera_change` listener,
when it is not.

THE SLIDER FOLLOWS THE CAPTURE
In an app with a "frame" slider and no generated browser on it (Typhon), attach
puts a CaptureReviewer in charge of the canvas: the slider grows as frames are
saved, its last position is live, dragging back reviews a saved frame while
the capture carries on, Play plays, and PNG / Raw swaps to the .raw of the
same run. See council_qt/widgets/capture_review.py.

NOTHING HERE EVER DELETES
A generated app is forbidden from even SPELLING remove/unlink/rmtree (the gate
checks attribute names whether or not they are called), and a capture app that
could silently drop a run would be wrong regardless. Each run writes under its
own stamped stem, so a second run never overwrites the first and no cleanup is
required to make one safe.
"""
from __future__ import annotations

import itertools
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

#: How often a UI timer should call `pump`. ~30 Hz: a display consumes about
#: thirty frames a second no matter what the sensor does.
LIVE_MS = 33

#: The ports `attach` looks up BY NAME and cannot run without: the picture
#: (`view`, through _port_widget) and the status line (`status`, through
#: _port) — both raise "this app has no ... port". Every other name attach
#: reads (frame, capture_folder, current_frame, roi, view_status) is optional:
#: it is fetched with getattr(..., None) and the feature it serves is skipped.
#: Read by gui_spec with ast, never by importing this module, so Generate
#: refuses a wireframe that links here and has renamed either of them.
COUNCIL_REQUIRED_PORTS = ("live_view", "capture_status")

#: Running this module's functions opens camera SDKs and a grab thread, so
#: the code writer's smoke run (gui_smoke) replaces it with a stand-in that
#: returns each function's documented result keys. Read with ast, like
#: COUNCIL_REQUIRED_PORTS — a smoke run never imports this module to ask.
COUNCIL_SMOKE_FAKE = True


class _Live:
    """The one open camera, its grab loop, and where its frames are going.

    Module-level state, which is a real departure from this family: every other
    frame_* function is stateless and takes a folder path. A camera cannot be —
    it is an open device with a thread attached, and reopening it per call
    would be both slow and wrong. So the state is here, explicitly, with one
    instance and an explicit shutdown.
    """

    def __init__(self) -> None:
        self.device = None
        self.session = None
        self.info = None
        self.found = None          # the last scan, for resolving a choice
        self.folder: Optional[Path] = None
        self.run = ""
        #: camera_setup.json for the attached app. Survives disconnect: it
        #: is about the app, not about the open camera.
        self.setup_path: Optional[Path] = None
        #: The .raw of the latest run, or None (a frame camera has none).
        self.raw_path: Optional[Path] = None
        #: The attached app's slider controller. About the app, like
        #: setup_path, so it survives disconnect.
        self.reviewer: Any = None
        #: The attached app's picture, for Pop out, and the windows popped
        #: out of it (held so they are not garbage-collected shut).
        self.canvas: Any = None
        self.popouts: List[Any] = []
        #: Whether the status line has said its last word about the latest
        #: run. The pump writes the live numbers only while there is
        #: something live to report; otherwise it would overwrite every other
        #: message in that line thirty times a second.
        self.reported = True
        #: The run is being saved to a network share. Said in the live
        #: status line, because a message returned by Start is overwritten
        #: by the live numbers within one tick.
        self.network = False
        #: Between Start and Stop. The session RUNNING is not enough to tell:
        #: the preview runs it too, without recording.
        self.capturing = False
        #: The session is running only to show the camera, recording nothing.
        self.previewing = False
        #: A preview that failed to start is retried from here, not every tick.
        self.preview_retry_at = 0.0
        #: Why the status line went quiet: "stopped" after a capture,
        #: "ended" when a capture's camera stopped by itself (see
        #: idle_reason), "preview-off" when the folder has frames, or a
        #: complete message to show as it is.
        self.idle = ""
        self.idle_reason = ""
        #: A camera that must not be restarted (an EVK4) has had its stream
        #: started on this connection. Once that stream is not running, it
        #: is dead for the rest of the connection: shown, hidden or
        #: recording, nothing may start it again.
        self.stream_started = False
        #: What the FPS box last asked for, said in the LIVE line until
        #: rate_note_until. The box's own reply goes to the same status line,
        #: and while the camera streams that line is rewritten thirty times a
        #: second — so the live line has to carry the change itself, or the
        #: user never sees that the box did anything.
        self.rate_note = ""
        self.rate_note_until = 0.0
        #: The last run's result, said in the live line for RUN_NOTE_SECONDS
        #: after Stop — the live view comes back at once and would otherwise
        #: replace "Stopped. 120 saved" before anyone read it.
        self.run_note_until = 0.0
        #: The camera area (x, y, w, h) of the newest LIVE frame on screen:
        #: what a box drawn on the picture is relative to.
        self.shown_aoi: Optional[tuple] = None
        #: A camera change running on a worker (see _Job), or None.
        self.job: Any = None
        #: on_camera_change listeners. About the app, like the reviewer.
        self.listeners: List[Callable[[Dict[str, Any]], Any]] = []
        #: The open settings window, held so it is not collected shut.
        self.settings_window: Any = None
        #: The main window's preset picker and camera-area line (attach),
        #: held for the same reason. About the app, like the reviewer.
        self.picker: Any = None
        #: Every savable setting as the camera had it when it was connected
        #: — what "Reset" puts back. An EVK4 is opened with its sensor's
        #: default biases, so for it this IS the camera's default; a Basler
        #: keeps what it was last given until it is powered off, so it also
        #: offers its own factory set (load_camera_defaults).
        self.as_connected: Dict[str, Any] = {}
        #: When each box beside Start ("exposure", "gain", "frame_rate")
        #: was last changed, and when a preset or the settings window last
        #: set the same thing on the camera — as a running count, not a
        #: clock (Windows' monotonic clock ticks every 15.6 ms). Start
        #: applies a box only when it is the newer of the two (see
        #: START_BOXES). Box changes are about the app and survive
        #: disconnect; what the camera was set to is about the connection.
        self.box_changed: Dict[str, int] = {}
        self.camera_set: Dict[str, int] = {}
        #: Each box's value at the last Start — how a change is noticed in
        #: an app whose box has no hook (`hooked_boxes`, set by attach).
        self.box_seen: Dict[str, str] = {}
        self.hooked_boxes: set = set()
        #: A box beside Start ("exposure", "gain", "frame_rate") changed
        #: while a camera change had the camera on the worker: its latest
        #: value, written by the UI thread once the change is done
        #: (_write_queued). About the connection: dropped at Disconnect.
        self.queued: Dict[str, Any] = {}
        #: The preset the camera was last put back to (or saved as): its
        #: name, settings and area as they were then — what a run's camera
        #: record compares the camera with at Start, to say the preset only
        #: while nothing has changed since. About the connection.
        self.preset_in_use: Optional[Dict[str, Any]] = None
        #: The latest run's camera record (<run>_camera.json), or None.
        self.record_path: Optional[Path] = None

    def clear(self) -> None:
        self.device = None
        self.session = None
        self.info = None
        self.folder = None
        self.run = ""
        self.raw_path = None
        self.reported = True
        self.network = False
        self.capturing = False
        self.previewing = False
        self.preview_retry_at = 0.0
        self.idle = ""
        self.idle_reason = ""
        self.stream_started = False
        self.rate_note = ""
        self.rate_note_until = 0.0
        self.run_note_until = 0.0
        self.shown_aoi = None
        self.job = None
        self.as_connected = {}
        self.camera_set = {}
        self.queued = {}
        self.preset_in_use = None
        self.record_path = None


_LIVE = _Live()
_LOCK = threading.Lock()
_STAMPS = itertools.count(1)


# ======================================================================
# Finding and opening
# ======================================================================
def list_cameras() -> Dict[str, Any]:
    """Every camera on the machine, and why any backend was skipped.

    The notes matter as much as the list. An empty list with no explanation
    sends a user to check cables when the real answer is that an SDK was never
    installed — so `notes` says which backend was skipped and what to install,
    and a pypylon that enumerates nothing names the CoaXPress frame grabber.
    """
    from council_core import cameras

    found = cameras.discover(_backends_for(current_choice()))
    _LIVE.found = found
    rows = [_row(i, c) for i, c in enumerate(found.cameras)]
    # A LIST, not a joined string. These go to a listbox port, whose
    # set() iterates what it is given -- a string becomes one row PER
    # CHARACTER, which is exactly what it did the first time this ran
    # against a real camera: the panel showed "p", "r", "o", ...
    notes = list(found.notes) or ["Every backend was searched."]
    if found.cameras:
        summary = f"{len(found.cameras)} camera(s) found."
    else:
        summary = "No cameras found — see the notes."
    return {"rows": rows, "summary": summary, "notes": notes}


def _backends_for(choice: Optional[str]):
    """The backends to search for a set-up app, or None for all of them.

    An app set up for a Basler has no business reporting that the Metavision
    SDK is missing — that note is noise to someone who will never plug in an
    event camera. The simulated cameras stay in every list: they are how the
    app is explored before the hardware arrives.
    """
    from council_core import cameras

    if choice == "basler":
        return [cameras.BaslerBackend(), cameras.SyntheticBackend()]
    if choice == "prophesee":
        return [cameras.EvkBackend(), cameras.SyntheticBackend()]
    return None


def _picked(selection: Any) -> str:
    """A listbox port's value is its SELECTION (a list); accept a str too."""
    if isinstance(selection, (list, tuple)):
        return str(selection[0]).strip() if selection else ""
    return str(selection if selection is not None else "").strip()


def _row(index: int, info: Any) -> str:
    """One line in the camera list.

    The INDEX is at the front and is what `connect` resolves on. Two identical
    cameras with no serial produce identical labels, and picking between them
    by matching label text would open whichever came first — the "recover an
    identifier from display text" defect this codebase keeps finding.
    """
    return f"{index + 1}. {info.label} · {info.kind}"


def _chosen(which: Any) -> Any:
    """Resolve a list selection to a camera, WITHOUT parsing the label.

    Accepts the row's position (an int, or the "3." that a listbox hands back
    as text), or an exact camera key. It never tries to reconstruct a camera
    from the descriptive part of the row, because that part is not unique.
    """
    found = _LIVE.found
    if found is None or not found.cameras:
        raise RuntimeError("scan for cameras first")

    # A LISTBOX PORT'S VALUE IS ITS SELECTION, AND THAT IS A LIST. The
    # generated handler passes self.ports.cameras.get() straight in, and
    # str() of a list is "['1. ...']" — which parses as no camera at all.
    # frame_classes._picked is the same rule for the same reason.
    raw = _picked(which)
    if not raw:
        raise RuntimeError("choose a camera in the list first")

    head = raw.split(".", 1)[0].strip()
    if head.isdigit():
        position = int(head) - 1
        if 0 <= position < len(found.cameras):
            return found.cameras[position]
        raise RuntimeError(f"there is no camera {head} in the list")

    for info in found.cameras:
        if info.key == raw:
            return info
    raise RuntimeError(f"{raw!r} is not one of the cameras found")


def connect(which: Any) -> Dict[str, Any]:
    """Open the chosen camera and report what it is."""
    from council_core import cameras, capture

    with _LOCK:
        if _LIVE.device is not None:
            raise RuntimeError("a camera is already open — disconnect first")
        info = _chosen(which)
        device = cameras.open_camera(info)
        # What the camera says it is once open (an event camera's sensor is
        # read then, never at discovery — cameras.identify): the identity
        # presets and the run's camera record use.
        opened = getattr(device, "info", None)
        if isinstance(opened, cameras.CameraInfo) and opened.key == info.key:
            info = opened
        _LIVE.device = device
        _LIVE.info = info
        _LIVE.session = capture.CaptureSession(device)
        _LIVE.reported = True

    limits = device.limits()
    area = device.roi()
    _LIVE.as_connected = _snapshot_or_nothing(device)
    _announce({"what": "connected", "summary": f"Connected to {info.label}."})
    return {
        "summary": (f"Connected to {info.label}. "
                    f"Sensor {limits.width}x{limits.height}."),
        "area": _area_text(area),
        "sensor": f"{limits.width}x{limits.height}",
        # An event camera has no exposure and no gain. Reporting the real
        # range lets a panel hide what this sensor does not have rather than
        # show a control that cannot work.
        "has_exposure": limits.exposure_us[1] > 0,
        "has_gain": limits.gain[1] > 0,
        "kind": info.kind,
    }


def disconnect() -> Dict[str, Any]:
    """Stop grabbing, release the camera, forget it."""
    with _LOCK:
        session = _LIVE.session
        job = _LIVE.job
        _LIVE.clear()
    if job is not None:
        # A change still stopping or restarting the stream owns it until it
        # is done; closing under it would race its restart. Cancelled first,
        # so it does not start a stream that is about to be closed.
        job.cancelled = True
        job.done.wait(JOB_JOIN_SECONDS)
    if session is not None:
        session.close()
        _announce({"what": "disconnected", "summary": "Disconnected."})
    return {"summary": "Disconnected.", "status": ""}


def shutdown() -> Dict[str, Any]:
    """What `on_close` should call.

    A grab thread that outlives its window crashes the application on exit, so
    this joins it rather than merely dropping the reference.
    """
    return disconnect()


# ======================================================================
# Capturing
# ======================================================================
#: How long Stop waits for queued frames before handing the UI back. Frames
#: still queued after that keep saving in the background, and the status line
#: counts them down; the app waits for them in full when it quits.
STOP_DRAIN_SECONDS = 1.0


def start(folder: Any, exposure: Any = "", gain: Any = "",
          frame_rate: Any = None) -> Dict[str, Any]:
    """Begin the live view, and save every frame into `folder`.

    `folder` is the SAME folder the slider shows, which is what makes the
    slider a review of what is being captured.

    Each run writes under its own stamped name, so starting a second run into
    the same folder adds to it and can never overwrite the first — this module
    has no way to delete anything and should not have one.

    AN EVENT CAMERA ALSO RECORDS ITS .raw, started BEFORE the stream so the
    file holds the whole run (see EvkDevice.start_raw).

    EXPOSURE, GAIN AND FRAME RATE of 0 (or blank) leave the camera as it is —
    a spin box always has a number in it. A frame rate of 0 is the camera's
    own default: free-running for a frame camera, 20 ms windows for an EVK4.
    The FPS box also applies its rate the moment it changes
    (apply_frame_rate); Start passes it again, so a rate chosen before a
    camera was connected is not lost.

    A PRESET IS NEWER THAN A BOX NOBODY TOUCHED SINCE. A box is skipped —
    and the summary says so — when a preset or the settings window set the
    same thing on the camera after the box was last changed (START_BOXES).
    Before, picking "Bird bath" and pressing Start with the FPS box at its
    0 lifted the preset's frame-rate limit, and put an EVK4's picture
    window back to 20 ms (measured).

    THE RUN SAYS WHERE IT CAME FROM. <run>_camera.json is written beside
    the run's other files once the boxes are applied (council_core.
    camera_record): the camera, its area in sensor pixels, every setting,
    the preset when the camera is still as it left it, the app and the
    software. A record that cannot be written is said in the summary and
    the capture goes ahead.

    FROM THE PREVIEW. A frame camera's preview is stopped and the camera
    started again for the capture. An EVK4's stream is left running and the
    .raw opens inside it — restarting would carry stale decoder state into
    the capture (EvkDevice.restartable). Everything that can refuse (the
    folder, exposure, gain) is checked BEFORE anything stops, so a refused
    Start leaves the picture running.
    """
    from council_core import capture

    session = _require_session()
    if _LIVE.capturing:
        raise RuntimeError("already capturing — stop first")
    _refuse_while_busy("start a capture")
    where = str(folder or "").strip().strip('"')
    if not where:
        raise RuntimeError("choose a folder to save frames into first")
    out = Path(where)
    if out.exists() and not out.is_dir():
        raise RuntimeError(f"{out} is a file, not a folder")

    for box, value in (("exposure", exposure), ("gain", gain),
                       ("frame_rate", frame_rate)):
        # A box whose value differs from the last Start's was changed in
        # between — the only way to know it in an app with no port hooks
        # (a Tk build). A hooked box (attach, Qt) was stamped WHEN it was
        # changed, which may be before a preset: not re-stamped as now.
        text = str(value if value is not None else "").strip()
        if (box not in _LIVE.hooked_boxes and box in _LIVE.box_seen
                and _LIVE.box_seen[box] != text):
            _box_changed(box)
        _LIVE.box_seen[box] = text
    kept = []
    for box, value, write in (("exposure", exposure, set_exposure),
                              ("gain", gain, set_gain)):
        if _box_given(value):
            if _box_is_newer(box):
                write(value)
            else:
                kept.append(box)
    if frame_rate is not None and str(frame_rate).strip():
        if _box_is_newer("frame_rate"):
            set_frame_rate(frame_rate)
        else:
            kept.append("frame rate")

    device = session.device
    restartable = getattr(device, "restartable", True)
    if _stream_dead(session):
        raise RuntimeError(_DEAD_STREAM)
    if session.running and restartable:
        _stop_preview(session)
    elif restartable:
        _release_dead_stream(session)

    run = _unique_run(out)
    recorder = capture.Recorder(out, stem=f"{run}_frame",
                                index_name=f"{run}_frames.csv")
    try:
        # reset: this run's numbers only — frames the preview showed were
        # never meant to be saved, and counting them would read as frames the
        # capture lost. In the same step as the switch (see record_to).
        session.record_to(recorder, reset=True)
    except OSError as exc:
        raise RuntimeError(f"cannot save frames into {out}: {exc}") from exc
    # After the boxes beside Start were applied and before the stream runs
    # for the run: the camera as every picture of this run will have it.
    record, record_note = _write_camera_record(out, run, device)
    _LIVE.record_path = record

    raw = None
    if getattr(device, "records_raw", False):
        try:
            raw = device.start_raw(out / f"{run}_events.raw")
        except Exception as exc:                          # noqa: BLE001
            session.record_to(None)
            raise RuntimeError(f"cannot record the raw file: {exc}") from exc
    _LIVE.folder = out
    _LIVE.run = run
    _LIVE.raw_path = raw
    _LIVE.reported = False
    _LIVE.network = _on_network_share(out)
    if not session.running:
        _prepare(device, recording=True)
        try:
            session.start()
        except Exception:
            if raw is not None:
                try:
                    device.stop_raw()
                except Exception:                         # noqa: BLE001
                    pass
            session.record_to(None)
            raise
        _started_stream(session)
    _LIVE.capturing = True
    _LIVE.previewing = False

    said = f"Capturing into {out}."
    if raw is not None:
        said += f" Raw: {raw.name}."
    if record_note:
        said += f" {record_note}"
    if kept:
        said += (f" Kept the camera's own {' and '.join(kept)}: a preset or "
                 f"the settings window set {'it' if len(kept) == 1 else 'them'}"
                 f" after the box beside Start was last changed.")
    if _LIVE.network:
        said += (" This folder is on a network share — save to a local "
                 "folder and copy the run afterwards, or frames will be "
                 "skipped.")
    # A settings window greys out what a capture refuses (the area, a
    # preset, a setting the stream is in the way of) from this.
    _announce({"what": "capturing", "summary": said})
    return {"summary": said, "folder": str(out), "run": run,
            "raw": str(raw) if raw is not None else "",
            "record": str(record) if record is not None else ""}


def stop() -> Dict[str, Any]:
    """End the run. Returns whether the grab thread actually stopped.

    A frame camera is stopped. An EVK4 keeps streaming: its PNGs stop and
    its .raw is closed inside the running stream (EvkDevice.finish_raw), so
    the next capture or the preview carries on without a restart. Saving the
    PNGs still queued is given STOP_DRAIN_SECONDS; anything left after that
    carries on in the background and the status line counts it down, so a
    slow disk never freezes the window.
    """
    session = _LIVE.session
    if session is None:
        return {"summary": "Not capturing."}
    if not _LIVE.capturing:
        if _LIVE.previewing:
            return {"summary": "Not capturing — that is the live view, and "
                               "nothing is being saved. Start capture saves."}
        return {"summary": "Not capturing."}
    _LIVE.capturing = False
    _LIVE.idle = "stopped"
    device = session.device
    if getattr(device, "restartable", True):
        ended = session.stop()
        session.record_to(None, drain_timeout=STOP_DRAIN_SECONDS)
    else:
        ended = True
        session.record_to(None, drain_timeout=STOP_DRAIN_SECONDS)
        finish = getattr(device, "finish_raw", None)
        if callable(finish):
            try:
                finish()
            except Exception as exc:                      # noqa: BLE001
                _LIVE.idle = (f"Stopped, but the raw file did not close "
                              f"cleanly: {exc}")
    stats = session.run_stats()
    if not ended:
        # The truth, not a hopeful message. Something is still holding the
        # camera, and the next start would be racing it.
        _announce({"what": "stopped",
                   "summary": "The camera did not stop cleanly."})
        return {"summary": "The camera did not stop cleanly.",
                "status": stats.line()}
    line = _stopped_line(session)
    _LIVE.run_note_until = time.monotonic() + RUN_NOTE_SECONDS
    _announce({"what": "stopped", "summary": line})
    return {"summary": line, "status": stats.line()}


#: How long the live line carries the last run's result after Stop.
RUN_NOTE_SECONDS = 10.0


def _run_note(session: Any) -> str:
    """The last run, short: what was saved, what was not, the .raw."""
    stats = session.run_stats()
    bits = [f"{stats.recorded} saved"]
    if stats.skipped:
        bits.append(f"{stats.skipped} NOT saved")
    raw = _LIVE.raw_path
    if raw is not None:
        try:
            bits.append(f"raw {raw.stat().st_size / (1024 * 1024):.1f} MB")
        except OSError:
            bits.append("raw MISSING")
    return "last run: " + ", ".join(bits)


def _stopped_line(session: Any) -> str:
    """What the status line says once a run is over — or nearly over."""
    stats = session.run_stats()
    line = f"Stopped. {stats.line()}"
    if stats.waiting:
        line = f"Stopped — still saving. {stats.line()}"
    raw = _LIVE.raw_path
    if raw is not None:
        try:
            size = raw.stat().st_size / (1024 * 1024)
            line += f" · raw {raw.name} ({size:.1f} MB)"
        except OSError:
            line += f" · raw {raw.name} MISSING"
    return line


def status() -> Dict[str, Any]:
    """The measured rate, and the frames the display never showed.

    The drop count is always present, including at zero: a number that only
    appears once it is bad is a number nobody is watching when it goes bad.
    """
    session = _LIVE.session
    if session is None:
        return {"summary": "No camera open.", "status": ""}
    line = session.stats().line()
    return {"summary": line, "status": line}


# ======================================================================
# The live view — called from a UI-thread timer, never from the grab loop
# ======================================================================
def latest() -> Any:
    """The newest frame, or None if none has arrived since the last call.

    THE UI THREAD CALLS THIS. See the module docstring: the grab loop only ever
    puts frames into a mailbox, and this takes from it, so no widget is ever
    touched off-thread.
    """
    session = _LIVE.session
    if session is None:
        return None
    return session.mailbox.take()


def pump(show: Callable[[Any], Any],
         say: Optional[Callable[[str], Any]] = None) -> bool:
    """Move the newest frame to the display. Returns whether one was shown.

    Wire this to a QTimer in app.py, which is created once and never
    regenerated::

        from PySide6.QtCore import QTimer
        import frame_camera

        self._live = QTimer(self)
        self._live.timeout.connect(self._pump)
        self._live.start(frame_camera.LIVE_MS)

        def _pump(self):
            frame_camera.pump(self.ports.live_view.widget.set_array,
                              self.ports.capture_status.set)

    `show` takes a numpy array — ImageCanvas.set_array is exactly that shape,
    and it is the cheap path: the generated to_qimage sends an array straight
    to QImage with no PIL round trip.
    """
    _watch_capture()
    frame = latest()
    if frame is not None:
        show(frame.image)
        _LIVE.shown_aoi = _aoi_of(frame)
    _report(say, frame)
    return frame is not None


def _report(say: Optional[Callable[[str], Any]], frame: Any) -> None:
    """The status line: live numbers while capturing or saving, then one
    final line, then silence — so "Connected to ..." and every other message
    written there stays readable."""
    session = _LIVE.session
    if say is None or session is None:
        return
    if _LIVE.capturing or session.saving:
        say(_status_line(session.stats(), frame))
        _LIVE.reported = False
    elif _LIVE.job is not None:
        # The stream is stopped ON PURPOSE for a moment; not "quiet".
        say(f"{_LIVE.job.label}…")
        _LIVE.reported = False
    elif _LIVE.previewing and session.running:
        say(_preview_line(session, session.stats(), frame))
        _LIVE.reported = False
    elif not _LIVE.reported:
        say(_idle_line(session))
        _LIVE.reported = True


def _preview_line(session: Any, stats: Any, frame: Any) -> str:
    """Said while the live view runs: that NOTHING is being saved comes
    first. Short: the line is one row, and Connect already said which camera
    it is. For a while after Stop it also carries the run's result, which
    the live view coming back would otherwise have replaced unread."""
    line = f"Live view — not saving · {stats.rate:.1f} fps{_rate_note()}"
    meta = getattr(frame, "meta", None) or {}
    if meta.get("kind") == "event":
        line += f" · {meta.get('events', 0)} events/window"
    if time.monotonic() < _LIVE.run_note_until:
        line += f" · {_run_note(session)}"
    return line


def _idle_line(session: Any) -> str:
    """Said once when the camera goes quiet, saying why."""
    if _LIVE.idle == "stopped":
        return _stopped_line(session)
    if _LIVE.idle == "ended":
        return (f"Capture ended — {_LIVE.idle_reason}. "
                f"{session.run_stats().line()}")
    label = getattr(_LIVE.info, "label", "") or "camera"
    if _LIVE.idle == "preview-off":
        return (f"Connected to {label}. The live view is off while the raw "
                f"file is shown; PNG view shows the camera again.")
    if _LIVE.idle:
        return _LIVE.idle
    return f"Connected to {label}."


_DEAD_STREAM = ("the camera stopped sending, and its stream cannot be "
                "restarted on the same connection — Disconnect and Connect "
                "again")


def _started_stream(session: Any) -> None:
    """Remember that a one-stream camera's stream has been started."""
    if not getattr(session.device, "restartable", True):
        _LIVE.stream_started = True


def _stream_dead(session: Any) -> bool:
    """A one-stream camera whose stream was started and is not running."""
    return (not getattr(session.device, "restartable", True)
            and _LIVE.stream_started and not session.running)


def _watch_capture() -> None:
    """End a capture whose camera stopped by itself, and say why.

    Otherwise the app still believed it was capturing: the status line froze
    on the last frame rate and Start was refused until Stop was pressed.
    """
    session = _LIVE.session
    if session is None or not _LIVE.capturing or session.running:
        return
    _LIVE.capturing = False
    reason = session.stats().last_error or "the camera stopped sending"
    # Release the device side (a frame camera stops grabbing; an EVK4's .raw
    # is closed) and stop recording WITHOUT waiting: what is still queued
    # keeps saving and the status line counts it down.
    try:
        session.stop()
    except Exception:                                     # noqa: BLE001
        pass
    session.record_to(None, drain_timeout=0)
    if not getattr(session.device, "restartable", True):
        reason += " — Disconnect and Connect again"
    _LIVE.idle = "ended"
    _LIVE.idle_reason = reason
    _LIVE.reported = False
    _announce({"what": "stopped", "summary": f"Capture ended — {reason}."})


#: How long a preview that failed to start waits before trying again.
PREVIEW_RETRY_SECONDS = 5.0


def _manage_preview(want: bool) -> None:
    """Run the camera without recording while the viewer wants to show it.

    Called every tick from the UI thread, as are Start and Stop, so nothing
    here races them. `want` comes from the reviewer: connected and not
    capturing, in the PNG view — whatever the folder holds.

    Left alone while a camera change (_Job) has the stream stopped on
    purpose: that is not the camera ending by itself, and restarting it
    here would race the job's own restart.
    """
    session = _LIVE.session
    if session is None or _LIVE.capturing or _LIVE.job is not None:
        return
    now = time.monotonic()
    restartable = getattr(session.device, "restartable", True)
    if _stream_dead(session):
        # Shown, hidden or after a capture: a one-stream camera whose stream
        # has stopped is never started again on this connection.
        if _LIVE.previewing or _LIVE.idle in ("", "preview-off", "stopped"):
            _LIVE.previewing = False
            reason = session.stats().last_error or "it stopped sending"
            _LIVE.idle = f"Camera stopped: {reason}. Disconnect and Connect again."
            _LIVE.reported = False
        return
    if _LIVE.previewing and not session.running:
        # It ended by itself: the camera was unplugged, or failed.
        _LIVE.previewing = False
        reason = session.stats().last_error or "the camera stopped sending"
        _LIVE.idle = f"Live view stopped: {reason}"
        _LIVE.reported = False
        _LIVE.preview_retry_at = now + PREVIEW_RETRY_SECONDS
        return
    if want and session.running:
        if not _LIVE.previewing:
            # An EVK4 kept streaming while there was nothing to show it on.
            _LIVE.previewing = True
            _LIVE.idle = ""
        return
    if want and not session.running:
        if now < _LIVE.preview_retry_at:
            return
        if session.saving:
            # The stopped run is still landing: starting the preview now
            # would reset the counts and hide "N waiting to save".
            return
        try:
            session.record_to(None)
            session.reset_stats()
            _prepare(session.device, recording=False)
            session.start()
        except Exception as exc:                          # noqa: BLE001
            _LIVE.idle = f"Live view stopped: {type(exc).__name__}: {exc}"
            _LIVE.reported = False
            _LIVE.preview_retry_at = now + PREVIEW_RETRY_SECONDS
            return
        _started_stream(session)
        _LIVE.previewing = True
        _LIVE.idle = ""
    elif not want and _LIVE.previewing:
        if restartable:
            _stop_preview(session)
        else:
            # Not stopped — never restart this stream — just not drawn.
            _LIVE.previewing = False
        _LIVE.idle = "preview-off"
        _LIVE.reported = False


def _prepare(device: Any, recording: bool) -> None:
    """Tell the camera, before it starts, whether frames will be recorded
    (keep every one) or only shown (the newest will do)."""
    prepare = getattr(device, "prepare", None)
    if callable(prepare):
        try:
            prepare(recording)
        except Exception:                                 # noqa: BLE001
            pass


def _stop_preview(session: Any) -> None:
    if not session.stop():
        raise RuntimeError("the camera did not stop its live preview cleanly")
    _LIVE.previewing = False


def _release_dead_stream(session: Any) -> None:
    """A grab loop that ended by itself (an unplugged or failing camera)
    leaves its device marked started: CaptureSession only stops the device
    when asked. Then a Basler refused its area and pixel format "while
    streaming" with nothing streaming, and its start() returned at once —
    so a capture begun there kept the live view's grab strategy. Stopping
    the session puts the device side right; nothing else is running."""
    device = session.device
    if not session.running and getattr(device, "streaming", False):
        session.stop()
        # Whatever stopped the live view, a change is about to be made or a
        # capture started: try the live view again at once afterwards.
        _LIVE.preview_retry_at = 0.0


# ======================================================================
# The boxes beside Start, and the camera's own settings
# ======================================================================
#: What each box beside Start sets, by the setting keys a preset or the
#: settings window writes for the same thing (camera_settings keys, which
#: are the same on a Basler, an EVK4 and the simulated cameras).
START_BOXES: Dict[str, tuple] = {
    "exposure": ("ExposureTime", "ExposureAuto"),
    "gain": ("Gain", "GainAuto"),
    "frame_rate": ("AcquisitionFrameRate", "AcquisitionFrameRateEnable",
                   "window_ms"),
}


def _box_changed(box: str) -> None:
    """A box beside Start was changed (attach's port hook; the FPS box's
    own link). Its value is the user's latest word on that setting."""
    _LIVE.box_changed[box] = next(_STAMPS)


def _camera_set(keys: Any) -> None:
    """A preset, a reset or the settings window set these keys on the
    camera: the latest word on whichever boxes they belong to."""
    keys = set(keys)
    for box, owned in START_BOXES.items():
        if keys.intersection(owned):
            _LIVE.camera_set[box] = next(_STAMPS)


def _box_is_newer(box: str) -> bool:
    """Should Start apply this box? Yes unless the camera was set to the
    same thing by a preset or the settings window after the box changed."""
    return _LIVE.box_changed.get(box, 0) >= _LIVE.camera_set.get(box, -1)


def _box_given(value: Any) -> bool:
    """A box's value means "set this": not blank and not 0 ("0 = keep")."""
    text = str(value if value is not None else "").strip()
    if not text:
        return False
    try:
        return float(text) != 0.0
    except ValueError:
        return True                      # let the setter say what is wrong


def _changed_keys(applied: Any) -> List[str]:
    """The keys a camera_settings.Applied actually wrote."""
    return [c.key for c in getattr(applied, "changes", [])
            if c.ok and not c.skipped]


def _tick(show: Callable[[Any], Any], say: Optional[Callable[[str], Any]],
          reviewer: Any) -> None:
    """One timer tick: a finished camera change reported, the live view
    started or stopped, the newest frame to the reviewer (or straight to the
    canvas when the app has none), then the status line."""
    if reviewer is None:
        _poll_job()
        pump(show, say)
        return
    if reviewer is not _LIVE.reviewer:
        # A window that is no longer the attached one (closed, or replaced by
        # a later attach) must not start and stop the shared camera — two
        # viewers disagreeing would toggle the preview every tick.
        return
    _poll_job()
    _watch_capture()
    try:
        _manage_preview(bool(reviewer.wants_preview()))
    except Exception as exc:                              # noqa: BLE001
        _LIVE.idle = f"Live view: {exc}"
        _LIVE.reported = False
    frame = latest()
    if reviewer.tick(frame):
        _LIVE.shown_aoi = _aoi_of(frame)
    _report(say, frame)


def _aoi_of(frame: Any) -> Optional[tuple]:
    """The camera area a frame was taken with, or None if it does not say."""
    aoi = (getattr(frame, "meta", None) or {}).get("aoi")
    try:
        return tuple(int(v) for v in aoi) if aoi is not None else None
    except (TypeError, ValueError):
        return None


def attach(app: Any, view: str = "live_view",
           status: str = "capture_status",
           interval_ms: int = LIVE_MS, first_run: bool = True,
           wizard: Optional[Callable[[Any], Any]] = None,
           scrubber: str = "frame", folder: str = "capture_folder",
           current: str = "current_frame", roi: str = "roi",
           view_status: str = "view_status", presets: str = "preset",
           area: str = "camera_area") -> Any:
    """Start the live view. ONE line in app.py, which is never regenerated::

        class App(HandlerMixin, MainUi):
            def __init__(self, parent=None, **kw):
                super().__init__(parent)
                frame_camera.attach(self)

    Everything Qt lives here rather than in the generated project, so the app
    stays what the wireframe produced. This module may import PySide6 because
    the policy gate scans the PROJECT directory, not the Council modules a
    linked app reaches — and the import is lazy, so a Tk app that calls
    nothing here never pays for it.

    CALL IT FROM THE UI THREAD. It creates a QTimer, and a QTimer created on a
    worker belongs to that worker's event loop — which a grab thread does not
    have, so it would simply never fire. `App.__init__` is the UI thread.

    SAFE TO CALL TWICE. Projects generated now get this line written into
    app.py for them; projects generated earlier had it added by hand, and a
    user following the old instructions on a new project would add it a
    second time. The second call returns the first timer rather than
    starting two live views and two setup wizards.

    FIRST RUN. If this app has never been set up, the camera setup wizard
    opens once the window is up — which camera, what to install, and a check
    that it is installed. Cancelling it means it is offered again next time.
    `first_run=False` or COUNCIL_NO_DIALOGS skips it; `wizard` replaces it,
    so a test can see it scheduled without a window appearing.

    THE SLIDER. When the app has a `scrubber` port and a `folder` port, and no
    generated browser already drives that slider, a CaptureReviewer takes the
    canvas: it follows the capture, plays, and swaps PNG / raw. `current`,
    `roi` and `view_status` are used when the app has them.

    THE PRESET BOX AND THE AREA LINE. When the app has a `presets` combobox
    and/or an `area` label, a PresetPicker (council_qt.widgets.
    preset_picker) keeps the box listing this camera's presets and the
    label saying the camera's area in sensor pixels — after every change,
    from this window or the settings window (on_camera_change). A link can
    only write the box's text, never its list. Optional, like the slider.
    """
    from PySide6.QtCore import Qt, QTimer

    canvas = _port_widget(app, view)
    show = getattr(canvas, "set_array", None)
    if not callable(show):
        raise RuntimeError(f"the {view!r} port is not an image canvas")
    say = getattr(_port(app, status), "set", None) if status else None

    # Checked AFTER the arguments: a second call is harmless, a wrong one is
    # still wrong.
    held = getattr(app, "_frame_camera_live", None)
    if held is not None:
        return held

    reviewer = _reviewer_for(app, canvas, scrubber, folder, current, roi,
                             view_status)
    _LIVE.reviewer = reviewer
    _LIVE.canvas = canvas
    app._capture_review = reviewer

    timer = QTimer(app)
    timer.setInterval(int(interval_ms))
    # PRECISE: Windows rounds coarse timers to its ~15.6 ms tick, which
    # turned a 33 ms timer into ~20 updates a second.
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    timer.timeout.connect(lambda: _tick(show, say, reviewer))
    timer.start()
    # HELD ON THE APP ON PURPOSE. A QTimer whose last reference goes out of
    # scope is collected and silently stops firing — the live view would work
    # for as long as this function's frame existed and then quietly die.
    app._frame_camera_live = timer

    # A grab thread that outlives its window crashes the application on exit.
    # Joining it is not the app author's job to remember.
    try:
        from PySide6.QtWidgets import QApplication
        running = QApplication.instance()
        if running is not None:
            running.aboutToQuit.connect(shutdown)
    except Exception:                                     # noqa: BLE001
        pass

    _hook_start_boxes(app)
    _LIVE.setup_path = _setup_path_for(app)
    # After setup_path: the presets it lists are this app's project's.
    picker = _picker_for(app, presets, area)
    if _LIVE.picker is not None and _LIVE.picker is not picker:
        _LIVE.picker.close()           # an earlier window's, now replaced
    _LIVE.picker = picker
    app._camera_presets = picker
    if first_run and current_choice() is None and not _dialogs_disabled():
        # After the window is up, not inside __init__: a modal dialog opened
        # while the main window is still being built has no window on screen
        # to sit over.
        QTimer.singleShot(0, lambda: (wizard or _first_run)(app))
    return timer


def _reviewer_for(app: Any, canvas: Any, scrubber: str, folder: str,
                  current: str, roi: str, view_status: str) -> Any:
    """A CaptureReviewer for this app, or None when it should not have one.

    None when a generated _FrameBrowser already drives the slider (it is
    stored as ports.browse_<slider port>): two controllers drawing into one
    canvas is how a frame you scrubbed to gets replaced by a live one.
    """
    ports = getattr(app, "ports", None)
    if ports is None or getattr(ports, f"browse_{scrubber}", None) is not None:
        return None
    slider = getattr(getattr(ports, scrubber, None), "widget", None)
    where = getattr(ports, folder, None)
    if where is None or not all(callable(getattr(slider, name, None))
                                for name in ("set_range", "on_step", "set")):
        return None
    from council_core import cameras
    from council_qt.widgets.capture_review import CaptureReviewer

    return CaptureReviewer(
        canvas=canvas, scrubber=slider, folder=where,
        current=getattr(ports, current, None) if current else None,
        roi=getattr(ports, roi, None) if roi else None,
        view=getattr(ports, view_status, None) if view_status else None,
        feed=_Feed(), window_us=int(cameras.DEFAULT_ACCUMULATE_MS * 1000),
        parent=app)


def _hook_start_boxes(app: Any) -> None:
    """Note each change of a box beside Start (the ports named in
    START_BOXES), so Start can tell a box changed after a preset from one
    left as it was before it (_box_is_newer)."""
    ports = getattr(app, "ports", None)
    for box in START_BOXES:
        port = getattr(ports, box, None) if ports is not None else None
        hook = getattr(port, "on_change", None)
        if not callable(hook):
            continue
        try:
            hook(lambda *_a, b=box: _box_changed(b))
        except Exception:                                 # noqa: BLE001
            continue                    # a port with no change hook
        _LIVE.hooked_boxes.add(box)


def _picker_for(app: Any, presets: str, area: str) -> Any:
    """A PresetPicker for this app's preset box and area line, or None when
    it has neither."""
    import sys

    ports = getattr(app, "ports", None)
    combo = getattr(ports, presets, None) if (ports is not None
                                              and presets) else None
    line = getattr(ports, area, None) if (ports is not None and area) \
        else None
    if combo is None and line is None:
        return None
    from council_qt.widgets.preset_picker import PresetPicker

    return PresetPicker(sys.modules[__name__], combo=combo, area=line,
                        parent=app)


class _Feed:
    """What the reviewer is told about the capture. Read on the UI thread."""

    def capturing(self) -> bool:
        session = _LIVE.session
        return bool(_LIVE.capturing and session is not None and session.running)

    def previewing(self) -> bool:
        """The live view runs. Still True while a camera change has the
        stream stopped for a moment: the picture is coming straight back,
        and the slider should not leave the live end and return."""
        session = _LIVE.session
        return bool(_LIVE.previewing and session is not None
                    and (session.running or _LIVE.job is not None))

    def saving(self) -> bool:
        session = _LIVE.session
        return bool(session is not None and session.saving)

    def run(self) -> str:
        return _LIVE.run

    def written(self, start: int = 0) -> List[Any]:
        session = _LIVE.session
        return session.written(start) if session is not None else []

    def raw_growing(self, path: Any) -> bool:
        """Is `path` the .raw the camera is writing right now?"""
        device = _LIVE.device
        live = getattr(device, "raw_path", None) if device is not None else None
        if live is None:
            return False
        try:
            return Path(str(path)).resolve() == Path(str(live)).resolve()
        except OSError:
            return str(path) == str(live)


def play_pause() -> Dict[str, Any]:
    """Play the slider like a video, or pause it. Script-linkable."""
    reviewer = _require_reviewer()
    said = reviewer.play_pause()
    return {"summary": said, "view": reviewer.view_text()}


def toggle_view() -> Dict[str, Any]:
    """Swap the slider between the saved PNGs and the run's .raw."""
    reviewer = _require_reviewer()
    said = reviewer.toggle_view()
    return {"summary": said, "view": reviewer.view_text()}


def _require_reviewer() -> Any:
    reviewer = _LIVE.reviewer
    if reviewer is None:
        raise RuntimeError("this app has no capture slider to play")
    return reviewer


def _unique_run(out: Path) -> str:
    """The run's name: the time it started, made unique if a run started in
    the same second is already in the folder. The raw log would otherwise
    truncate the first run's .raw, silently."""
    import os

    base = time.strftime("%Y%m%d_%H%M%S")
    try:
        names = [e.name for e in os.scandir(out)] if out.is_dir() else []
    except OSError:
        names = []
    taken = set()
    for name in names:
        for marker in ("_frame_", "_events.raw", "_frames.csv",
                       "_camera.json"):
            head, found, _ = name.partition(marker)
            if found:
                taken.add(head)
    run, n = base, 1
    while run in taken:
        n += 1
        run = f"{base}_{n}"
    return run


def _on_network_share(path: Path) -> bool:
    """Is `path` on a network share (a UNC path or a mapped network drive)?"""
    import os

    text = str(path.absolute())
    if text.startswith(("\\\\", "//")):
        return True
    if os.name != "nt":
        return False
    drive = os.path.splitdrive(text)[0]
    if not drive or not drive.endswith(":"):
        return False
    try:
        import ctypes

        DRIVE_REMOTE = 4
        return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == DRIVE_REMOTE
    except Exception:                                     # noqa: BLE001
        return False


# ======================================================================
# The run's camera record (<run>_camera.json)
# ======================================================================
def _write_camera_record(folder: Path, run: str, device: Any
                         ) -> "tuple[Optional[Path], str]":
    """Write the run's camera record; (its path, "") or (None, why not).

    NEVER A REASON NOT TO CAPTURE. The record is what lets the run be read
    later — which camera, where on its sensor, how it was set — but a full
    disk or a refused name is said in Start's summary, not raised: the
    frames are the user's data and the capture goes ahead."""
    from council_core import camera_record

    try:
        listed = settings_list()["settings"]
        error = ""
    except Exception as exc:                              # noqa: BLE001
        listed, error = [], _said(exc)
    try:
        limits, area = device.limits(), device.roi()
        preset, changed = _preset_still_in_use(listed, area)
        info = _LIVE.info
        camera = {k: getattr(info, k, "") for k in
                  ("backend", "model", "serial", "vendor", "kind")}
        camera["label"] = getattr(info, "label", "") or ""
        record = camera_record.build(
            run=run, camera=camera, sensor=(limits.width, limits.height),
            area=area.as_tuple(), settings=listed, settings_error=error,
            preset=preset, preset_changed=changed, app=_app_identity(),
            software=_software(), host=_host_name())
        return camera_record.write(folder, run, record), ""
    except Exception as exc:                              # noqa: BLE001
        return None, (f"The run's camera record ({run}"
                      f"{camera_record.RECORD_SUFFIX}) was NOT written: "
                      f"{_said(exc)}.")


def _preset_still_in_use(listed: List[Dict[str, Any]], area: Any
                         ) -> "tuple[str, Optional[Dict[str, Any]]]":
    """(the preset's name, None) when the camera is still as the preset in
    use left it, else ("", {"name", "differs"}) — or ("", None) with none.

    COMPARED, NOT ASSUMED. Every way of changing the camera (a box beside
    Start, the settings window, Apply area, a hand-written link) would
    have to remember to forget the preset; comparing the preset with the
    camera at Start catches all of them. A setting another one owns at the
    moment (an exposure time under auto exposure) is not compared: the
    camera moves it, and the preset that turned the auto loop on is still
    what the camera is set to."""
    from council_core import camera_settings

    held = _LIVE.preset_in_use
    if not held:
        return "", None
    now = {str(row.get("key")): row for row in listed}
    differs = []
    for key, value in dict(held.get("settings") or {}).items():
        row = now.get(key)
        if row is None or row.get("held") or row.get("read_only"):
            continue
        if not camera_settings.same(value, row.get("value")):
            differs.append(key)
    roi = held.get("roi")
    if roi is not None and tuple(roi) != tuple(area.as_tuple()):
        differs.append("area")
    if differs:
        return "", {"name": held["name"], "differs": differs}
    return str(held["name"]), None


def _preset_now_in_use(name: str, settings: Dict[str, Any], roi: Any) -> None:
    """The camera was just put back to (or saved as) preset `name`."""
    _LIVE.preset_in_use = {
        "name": name, "settings": dict(settings or {}),
        "roi": tuple(roi.as_tuple()) if roi is not None else None}


def _app_identity() -> Dict[str, Any]:
    """The app running the capture, from its own project folder: the
    window's title, the Designer project, the example it was built from and
    when it was last generated. Fields that cannot be read are left out,
    never guessed."""
    import json

    folder = _project_dir()
    out: Dict[str, Any] = {"folder": str(folder)}
    try:
        manifest = json.loads((folder / "manifest.json").read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {}
    try:
        gspec = json.loads((folder / "project.gspec").read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        gspec = {}
    if isinstance(manifest, dict):
        for key, field in (("project", "name"), ("example", "example"),
                           ("generated", "updated"), ("created", "created"),
                           ("toolkit", "toolkit")):
            if manifest.get(field):
                out[key] = str(manifest[field])
    window = gspec.get("window") if isinstance(gspec, dict) else None
    title = str(window.get("title") or "") if isinstance(window, dict) else ""
    if title:
        out["name"] = title
    return out


#: The camera record's own version of what frame_camera writes into it.
RECORD_WRITER = 1


def _software() -> Dict[str, Any]:
    """Which software made the run: the Council's version (branding) and the
    commit it is checked out at, read from its git folder's files — "" in a
    build that has none."""
    import platform

    from council_core import camera_record

    root = Path(__file__).resolve().parent
    try:
        import branding
        version = str(getattr(branding, "VERSION", "") or "")
    except Exception:                                     # noqa: BLE001
        version = ""
    return {"council_version": version,
            "source_commit": camera_record.source_commit(root),
            "frame_camera": RECORD_WRITER,
            "python": platform.python_version()}


def _host_name() -> str:
    """This PC's name, the host name alone (as frame_classes records it)."""
    import os
    import socket

    name = os.environ.get("COMPUTERNAME", "")
    if not name:
        try:
            name = socket.gethostname()
        except OSError:
            name = ""
    return name.split(".")[0].strip()


# ======================================================================
# Camera setup — which camera this app is for, and what it needs
# ======================================================================
def current_choice() -> Optional[str]:
    """The camera this app is set up for ("basler", "prophesee") or None."""
    from council_core import camera_setup

    path = _LIVE.setup_path
    return camera_setup.load_choice(path) if path is not None else None


def setup(parent: Any = None) -> Dict[str, Any]:
    """Run the camera setup wizard, then list the chosen camera's devices.

    Script-linkable: a "Camera setup…" button calls this with no inputs and
    gets the same rows/notes/summary a scan returns, so finishing the wizard
    refreshes the camera list for the camera just chosen.

    Keys: rows, summary, notes
    (stated here because the dict is built, not written as a literal, so
    the Wiring group and the code writer could not read them otherwise)
    """
    from council_core import camera_setup

    path = _LIVE.setup_path or _fallback_setup_path()
    _LIVE.setup_path = path
    _tell_the_scan_what_is_open()
    if _dialogs_disabled():
        said = "Camera setup skipped — dialogs are disabled."
    else:
        from PySide6.QtWidgets import QApplication
        from council_qt.widgets import camera_wizard

        window = parent or QApplication.activeWindow()
        title = window.windowTitle() if window is not None else ""
        try:
            chosen = camera_wizard.run_wizard(window, path, app_name=title)
        except getattr(camera_wizard, "NoQtApplication", ()) as exc:
            chosen = None
            said = f"Camera setup skipped — {exc}."
        else:
            said = (f"Set up for {camera_setup.label(chosen)}." if chosen
                    else "Camera setup cancelled — nothing changed.")
    listed = list_cameras()
    listed["summary"] = f"{said} {listed['summary']}"
    return listed


def _tell_the_scan_what_is_open() -> None:
    """The wizard's Basler scan must not open the camera this app already has
    open: a second open fails on real hardware (the emulator allows it)."""
    try:
        from council_qt.widgets import camera_wizard
    except Exception:                                     # noqa: BLE001
        return
    camera_wizard.held_keys = (
        lambda: [_LIVE.info.key] if _LIVE.info is not None else [])


def _first_run(app: Any) -> None:
    """The wizard, then the camera list it implies, straight into the panel."""
    out = setup(parent=app)
    for port, key in (("cameras", "rows"), ("camera_notes", "notes"),
                      ("capture_status", "summary")):
        target = getattr(getattr(app, "ports", None), port, None)
        if target is not None:
            try:
                target.set(out[key])
            except Exception:                             # noqa: BLE001
                pass


def _setup_path_for(app: Any) -> Path:
    """camera_setup.json beside the app's own app.py."""
    import inspect

    from council_core import camera_setup

    try:
        return camera_setup.setup_path(
            Path(inspect.getfile(type(app))).resolve().parent)
    except (TypeError, OSError):
        return _fallback_setup_path()


def _fallback_setup_path() -> Path:
    """Beside the script that was run: a generated app's main.py."""
    import sys

    from council_core import camera_setup

    return camera_setup.setup_path(Path(sys.argv[0] or ".").resolve().parent)


def _dialogs_disabled() -> bool:
    import os

    return bool(os.environ.get("COUNCIL_NO_DIALOGS"))


def _port(app: Any, name: str) -> Any:
    ports = getattr(app, "ports", None)
    found = getattr(ports, name, None) if ports is not None else None
    if found is None:
        raise RuntimeError(f"this app has no {name!r} port")
    return found


def _port_widget(app: Any, name: str) -> Any:
    widget = getattr(_port(app, name), "widget", None)
    if widget is None:
        raise RuntimeError(f"the {name!r} port has no widget")
    return widget


#: What the display-drop count is called in the status line. "dropped" was
#: read as frames LOST; they are frames the screen did not redraw — every one
#: of them is still saved.
SCREEN_DROPS = "not drawn (screen only)"


def _status_line(stats: Any, frame: Any) -> str:
    line = stats.line(SCREEN_DROPS)
    note = _rate_note()
    if note:
        # Beside the MEASURED rate, so the two can be compared while the
        # sliding window catches up with the new one.
        head, sep, rest = line.partition(" · ")
        line = f"{head}{note}{sep}{rest}"
    meta = getattr(frame, "meta", None) or {}
    if meta.get("kind") == "event":
        # An event camera has no frame rate. What the number above counts is
        # accumulation windows; the event rate is the sensor's own measure.
        line += f" · {meta.get('events', 0)} events/window"
        rate = meta.get("event_rate_hz") or 0.0
        if rate:
            line += f" · {rate / 1000.0:.1f} kev/s"
    if _LIVE.network:
        line += " · NETWORK FOLDER: save locally, copy after"
    return line


# ======================================================================
# The area of interest
# ======================================================================
def set_area(area: Any) -> Dict[str, Any]:
    """The camera's own area, from a box drawn on (or typed for) the LIVE
    picture — so future frames ARE that part of the sensor.

    Not a crop. Cropping moves every pixel over the link and throws most of
    them away; a sensor AOI reads out less, which on a boA5320-150cm is the
    frame rate, and on an event camera stops the masked pixels emitting at all.

    THE BOX IS IN PICTURE PIXELS; THE CAMERA WANTS SENSOR PIXELS. The live
    picture of a camera with an area set IS that area, so the box is moved
    by the origin of the area the frame on screen was taken with
    (Frame.meta["aoi"]). Before, it was sent as it was: after one change of
    area, the next box drawn landed somewhere else on the sensor. A box on a
    SAVED frame is refused — that frame's area may not be the camera's now —
    and so is a box that runs past the live picture, which was not drawn on
    it (the camera's area in sensor pixels, left in the box by an older
    hand-edited handler, walked across the sensor with each press).
    `set_camera_area` takes sensor pixels as they are.

    The request is SNAPPED to what the sensor accepts and the result says
    what was actually taken. `crop` is "" — the crop box was drawn on the old
    picture and means nothing on the new one — and the camera's area is never
    written into it.

    Refused while capturing (see _refuse_while_capturing).

    Keys: area, crop, snapped, summary, pending
    """
    from council_core import cameras

    device = _require_device()
    box = _parse_area(area)
    if box is None:
        raise RuntimeError("type the area as x, y, w, h")
    reviewer = _LIVE.reviewer
    on_saved = getattr(reviewer, "showing_saved", None)
    if callable(on_saved) and on_saved():
        raise RuntimeError(
            "that box is on a saved frame, whose area may not be the "
            "camera's now — drag the slider to its end (live) and draw the "
            "area on the live picture")
    x0, y0, width, height = _picture_area(device)
    if box[0] + box[2] > width or box[1] + box[3] > height:
        # NOT A BOX ON THIS PICTURE: one drawn on it always fits inside it.
        # Typically the camera's own area (sensor pixels) left in the crop
        # box — by an older project's Connect or Apply-area handler that
        # was edited by hand, so Update from example kept it. Moved by the
        # origin anyway, each press walked the area across the sensor
        # (measured: 100, 60, 320, 240 became 200, 120, 320, 240).
        raise RuntimeError(
            f"the box {box[0]}, {box[1]}, {box[2]}, {box[3]} runs past the "
            f"live picture ({width} x {height}) — draw it on the picture; "
            f"the camera's own area in sensor pixels is set in Camera "
            f"settings")
    return _change_area(cameras.Roi(box[0] + x0, box[1] + y0, box[2], box[3]))


def set_camera_area(area: Any) -> Dict[str, Any]:
    """The camera's own area typed in SENSOR pixels (x, y, w, h), as
    `current_area` reports it — for a "camera area" box, not a drawn one.

    Keys: area, crop, snapped, summary, pending
    """
    from council_core import cameras

    _require_device()
    box = _parse_area(area)
    if box is None:
        raise RuntimeError("type the camera's area as x, y, w, h")
    return _change_area(cameras.Roi(*box))


def full_frame() -> Dict[str, Any]:
    """Give the whole sensor back.

    Keys: area, crop, snapped, summary, pending
    """
    from council_core import cameras

    device = _require_device()
    limits = device.limits()
    return _change_area(cameras.Roi(0, 0, limits.width, limits.height))


def current_area() -> Dict[str, Any]:
    """The camera's area now, in sensor pixels, and the sensor's size."""
    device = _require_device()
    area, limits = device.roi(), device.limits()
    full = area.as_tuple() == (0, 0, limits.width, limits.height)
    text = _area_text(area)
    return {"area": text, "sensor": f"{limits.width}x{limits.height}",
            "full": full,
            "summary": (f"Camera area: the whole sensor ({limits.width}x"
                        f"{limits.height})." if full else
                        f"Camera area: {text} of {limits.width}x"
                        f"{limits.height}.")}


def _picture_area(device: Any) -> tuple:
    """The live picture on the sensor, (x, y, w, h): its (0, 0) is at x, y
    and it is w x h pixels — the area of the frame on screen, or the
    camera's area if none has been shown yet."""
    aoi = _LIVE.shown_aoi
    if aoi is not None and len(aoi) >= 4:
        return tuple(int(v) for v in aoi[:4])
    area = device.roi()
    return int(area.x), int(area.y), int(area.w), int(area.h)


def _change_area(roi: Any) -> Dict[str, Any]:
    """Keys: area, crop, snapped, summary, pending"""
    from council_core import camera_settings, cameras

    device = _require_device()
    _refuse_while_capturing("changing the camera's area")
    _refuse_while_busy("change the camera's area")
    limits = device.limits()
    if not cameras.on_sensor(roi, limits):
        # Typed in sensor pixels (the settings window's area box): an area
        # wholly off the sensor was pulled to its corner and called
        # "snapped" — another part of the scene.
        raise RuntimeError(cameras.off_sensor(roi, limits))
    stop = bool(camera_settings.stops_needed(device, {}, roi))

    def work() -> Any:
        if device.streaming and camera_settings.area_unchanged(device, roi):
            # Already so: a streaming Basler would refuse even this write
            # (measured: "Full sensor" on a full-sensor live view raised).
            return device.roi()
        return device.set_roi(roi)

    def finish(got: Any) -> Dict[str, Any]:
        text = _area_text(got)
        snapped = tuple(got.as_tuple()) != tuple(roi.as_tuple())
        return {"area": text, "crop": "", "snapped": snapped, "ok": True,
                "what": "area", "pending": False,
                "summary": (f"Camera area set to {text}"
                            + (" — snapped to what the sensor accepts."
                               if snapped else "."))}

    return _run_change("Changing the camera's area", work, finish, stop)


def _parse_area(value: Any):
    """x, y, w, h from text — or None.

    `frame_roi.parse_roi` is the same idea for the crop-on-save path and is
    reused rather than re-implemented, so the two never disagree about what a
    box means.
    """
    if value is None:
        return None
    if isinstance(value, (tuple, list)) and len(value) == 4:
        try:
            box = tuple(int(v) for v in value)
        except (TypeError, ValueError):
            return None
        return box if min(box) >= 0 else None
    import frame_roi
    parsed = frame_roi.parse_roi(value)
    return tuple(parsed) if parsed else None


def _area_text(roi: Any) -> str:
    return f"{roi.x}, {roi.y}, {roi.w}, {roi.h}"


def _roi_or_none(device: Any) -> Any:
    """The camera's area now, or None when it cannot say."""
    try:
        return device.roi()
    except Exception:                                     # noqa: BLE001
        return None


# ======================================================================
# Changing the camera: the rules, and the worker for what needs a stop
# ======================================================================
#: How long a change that needs the stream stopped is waited for before the
#: caller is handed "applying…" and the window carries on. The usual case
#: finishes well inside it — measured on pylon's emulator with the live view
#: running: a preset changing the pixel format and the area held the UI
#: thread 35 ms (median, max 51), the area alone 43 ms; a preset of live
#: settings only, 3.6 ms. A camera at 2 fps, whose grab loop waits ~0.5 s for
#: its frame, is handed "pending" instead of freezing the window that long.
APPLY_WAIT_SECONDS = 0.15

#: How long Disconnect waits for a change in progress to finish.
JOB_JOIN_SECONDS = 5.0


def _refuse_while_capturing(what: str) -> None:
    """ONE SET-UP PER RUN. A Basler's area change used to restart the stream
    INTO the running capture — one run, frames of two sizes — and an EVK4's
    .raw would change meaning halfway. So a change of area, a preset, or a
    setting the stream is in the way of waits for Stop, on every camera."""
    if _LIVE.capturing:
        raise RuntimeError(
            f"stop the capture before {what} — one run keeps one camera "
            f"set-up, so all of its frames (and its .raw) stay comparable")


def _refuse_while_busy(what: str) -> None:
    job = _LIVE.job
    if job is not None:
        raise RuntimeError(f"{job.label} — wait for it to finish, then "
                           f"{what}")


class _Job:
    """A camera change that needs the stream stopped, run off the UI thread.

    Stopping a grab loop waits for the frame in flight — up to the read
    timeout (a second) on a slow or triggered camera — and starting it again
    is a round of SDK calls. On the UI thread that froze the window. Here
    the worker stops the stream, makes the change, starts the live view
    again, and the UI thread (`_poll_job`, every tick) reports the outcome.
    While it runs, the live view is left alone (_manage_preview), Start
    and every other change are refused, and Disconnect waits for it.
    """

    def __init__(self, label: str, work: Callable[[], Any],
                 finish: Callable[[Any], Dict[str, Any]], session: Any):
        self.label = label
        self.work = work
        self.finish = finish
        self.session = session
        self.result: Any = None
        self.error: Optional[BaseException] = None
        self.restart_error = ""
        self.cancelled = False
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run,
                                       name="camera-change", daemon=True)

    def _run(self) -> None:
        session = self.session
        stopped = False
        try:
            if not session.stop():
                raise RuntimeError("the camera did not stop its stream "
                                   "cleanly to make the change")
            stopped = True
            self.result = self.work()
        except BaseException as exc:                      # noqa: BLE001
            self.error = exc
        finally:
            if stopped and not self.cancelled:
                try:
                    _prepare(session.device, recording=False)
                    session.start()
                except Exception as exc:                  # noqa: BLE001
                    self.restart_error = (f"The live view did not restart: "
                                          f"{exc}")
            self.done.set()


def _run_change(label: str, work: Callable[[], Any],
                finish: Callable[[Any], Dict[str, Any]],
                stop: bool) -> Dict[str, Any]:
    """Make a change to the camera; `stop` says the stream is in the way.

    Not in the way (or nothing streams): done here and now. In the way, on a
    camera whose stream may stop: a _Job, waited for APPLY_WAIT_SECONDS. In
    the way on a camera whose stream must never restart (an EVK4): refused —
    nothing an EVK4 offers needs it, so this is a guard, not a path.
    """
    session = _require_session()
    if stop and not session.running and getattr(session.device,
                                                "restartable", True):
        # The live view died by itself: nothing streams, but the device
        # still says it does, and would refuse the change for it.
        _release_dead_stream(session)
    if stop and session.running:
        if not getattr(session.device, "restartable", True):
            raise RuntimeError(
                "this camera's stream cannot be restarted on the same "
                "connection, and this change needs it stopped — Disconnect, "
                "Connect, then make the change before the live view starts")
        job = _Job(label, work, finish, session)
        _LIVE.job = job
        job.thread.start()
        if job.done.wait(APPLY_WAIT_SECONDS):
            return _finish_job(job, raise_errors=True)
        out = {"summary": f"{label}… the camera is restarting its stream; "
                          f"the result will show in the status line.",
               "pending": True, "area": "", "crop": _current_crop(),
               "snapped": False, "ok": True, "what": "pending"}
        # Told, so a settings window open beside the window that made the
        # change greys out and holds its writes until the answer comes,
        # rather than having each one refused "wait for it to finish".
        _announce(out)
        return out
    try:
        result = work()
    except Exception as exc:                              # noqa: BLE001
        from council_core.cameras import NeedsStop

        if (isinstance(exc, NeedsStop) and not stop and session.running
                and getattr(session.device, "restartable", True)
                and not _LIVE.capturing):
            # The camera refused live what it described as live (a node's
            # writability is the camera's to change): the same change, with
            # the stream stopped. NeedsStop is raised before any write.
            return _run_change(label, work, finish, True)
        raise RuntimeError(_said(exc)) from exc
    out = finish(result)
    _settle_crop(out)
    _announce(out)
    return out


def _settle_crop(out: Dict[str, Any]) -> None:
    """A change that moved the camera's area clears the crop box ("crop":
    "") — whoever made it. A link writes "crop" to the box itself, but the
    settings window, the preset picker and a late ("pending") answer write
    no port: the box drawn on the old picture stayed over the new one, and
    the next Apply area moved the camera again from there."""
    if out.get("crop") != "" or out.get("pending"):
        return
    port = getattr(_LIVE.reviewer, "roi", None)
    if port is None:
        return
    try:
        if str(port.get() or ""):
            port.set("")
    except Exception:                                     # noqa: BLE001
        pass


def _finish_job(job: _Job, raise_errors: bool) -> Dict[str, Any]:
    """The outcome of a finished job, said once, on the UI thread."""
    if _LIVE.job is job:
        _LIVE.job = None
    error = job.error
    out = None
    if error is None:
        try:
            out = job.finish(job.result)
        except Exception as exc:                          # noqa: BLE001
            # The change was made, but reading the camera back for the
            # answer failed (unplugged in between). Said like any failure:
            # swallowed by _poll_job, nobody heard anything, and a settings
            # window stayed greyed at "Applying…" until Disconnect.
            error = exc
    # The worker is done with the camera: the boxes beside Start that were
    # changed meanwhile are written now, on this (the UI) thread.
    boxes = _write_queued()
    if error is not None:
        said = f"{job.label} failed: {_said(error)}"
        if job.restart_error:
            said += f" {job.restart_error}"
        out = {"ok": False, "what": "failed", "summary": said,
               "error": _said(error), "pending": False}
        _announce(out)
        if raise_errors:
            raise RuntimeError(said) from error
        _tell_status(f"{said} {boxes}".strip())
        return out
    if job.restart_error:
        out["summary"] = f"{out['summary']} {job.restart_error}"
    _settle_crop(out)
    _announce(out)
    if not raise_errors:
        _tell_status(f"{out['summary']} {boxes}".strip())
    return out


def _poll_job() -> None:
    """Every tick: report a job that finished after its caller stopped
    waiting."""
    job = _LIVE.job
    if job is not None and job.done.is_set():
        try:
            _finish_job(job, raise_errors=False)
        except Exception:                                 # noqa: BLE001
            pass


def _tell_status(text: str) -> None:
    """Put a late answer where it will be read: the idle status line, and
    for a while the live line (which is rewritten every tick)."""
    _LIVE.idle = text
    _LIVE.reported = False
    _LIVE.rate_note = text if len(text) <= 60 else text[:57] + "…"
    _LIVE.rate_note_until = time.monotonic() + RATE_NOTE_SECONDS


def _said(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text.split(" : ")[0][:300]


def _announce(out: Dict[str, Any]) -> None:
    for listener in list(_LIVE.listeners):
        try:
            listener(dict(out))
        except Exception as exc:                          # noqa: BLE001
            print(f"[frame_camera] a camera-change listener failed: {exc!r}")


def on_camera_change(listener: Callable[[Dict[str, Any]], Any]
                     ) -> Callable[[], None]:
    """Call `listener(result)` ON THE UI THREAD after every change to the
    camera made through this module — a setting, a preset, the area — with
    the same dict the function returned, or the late answer of one that was
    still "pending" when it returned; and with {"what": "connected"} /
    {"what": "disconnected"}, and {"what": "pending"} when a change goes
    to the worker (its answer follows). For the settings window, which must show what
    the camera actually took. Returns a function that removes the listener.
    Not script-linkable (it takes a function)."""
    if not callable(listener):
        raise RuntimeError("on_camera_change is for the app's own code (it "
                           "takes a function), not for a button")
    _LIVE.listeners.append(listener)

    def remove() -> None:
        try:
            _LIVE.listeners.remove(listener)
        except ValueError:
            pass

    return remove


def camera_state() -> Dict[str, Any]:
    """What a settings window needs to enable its controls: whether a camera
    is connected, capturing, busy with a change, streaming; its area and
    sensor size.

    Keys: connected, label, kind, capturing, busy, streaming, area, sensor,
    summary
    """
    device = _LIVE.device
    session = _LIVE.session
    info = _LIVE.info
    out = {"connected": device is not None,
           "label": getattr(info, "label", "") if info else "",
           "kind": getattr(info, "kind", "") if info else "",
           "capturing": bool(_LIVE.capturing),
           "busy": _LIVE.job is not None,
           "streaming": bool(session is not None and session.running),
           "area": "", "sensor": "",
           "summary": "No camera open."}
    if device is not None:
        limits = device.limits()
        out.update(area=_area_text(device.roi()),
                   sensor=f"{limits.width}x{limits.height}",
                   summary=f"Connected to {out['label']}.")
    return out


def _current_crop() -> str:
    """The crop box as it is — what a result leaves in the "roi" port when
    the picture did not change."""
    port = getattr(_LIVE.reviewer, "roi", None)
    try:
        return str(port.get() or "") if port is not None else ""
    except Exception:                                     # noqa: BLE001
        return ""


# ======================================================================
# Every setting the camera has (council_core.camera_settings)
# ======================================================================
#: The module whose open_settings(parent) shows the settings window. Looked
#: up when the button is pressed, so this module never imports a window.
SETTINGS_WINDOW_MODULE = "council_qt.widgets.camera_settings_window"


def camera_settings(parent: Any = None) -> Dict[str, Any]:
    """Open the camera's settings window — every setting the camera itself
    describes, and its presets. Script-linkable: a "Camera settings…" button
    calls this with no inputs.

    The window is SETTINGS_WINDOW_MODULE.open_settings(parent, show=...),
    which works through this module's settings_list / set_camera_setting /
    apply_camera_settings / list_presets / apply_preset / save_preset /
    delete_preset / rename_preset / current_area / camera_state and
    on_camera_change. A second press brings the open one forward.

    BUILT BUT NOT SHOWN under COUNCIL_NO_DIALOGS, as gui_settings builds its
    Python Scripts window: a test (or an offscreen check) can then press
    the button and work the window it would have seen. It is non-modal, so
    building it never waits on anyone.

    Keys: summary
    """
    if _LIVE.device is None:
        return {"summary": "Connect a camera first — its settings come from "
                           "the camera itself."}
    import importlib

    try:
        module = importlib.import_module(SETTINGS_WINDOW_MODULE)
    except ImportError:
        return {"summary": "The camera settings window is not part of this "
                           "build."}
    if parent is None:
        try:
            from PySide6.QtWidgets import QApplication

            parent = QApplication.activeWindow()
        except Exception:                                 # noqa: BLE001
            parent = None
    if parent is None and _LIVE.canvas is not None:
        # The attached app's window, so the settings window closes with it
        # rather than outliving it (nothing is "active" offscreen, or when
        # the press came while another program had the focus).
        try:
            parent = _LIVE.canvas.window()
        except Exception:                                 # noqa: BLE001
            parent = None
    hidden = _dialogs_disabled()
    try:
        _LIVE.settings_window = module.open_settings(parent, show=not hidden)
    except getattr(module, "NoQtApplication", ()) as exc:
        # A Tk build: said, not a process abort (see open_settings).
        return {"summary": f"Camera settings: {exc}."}
    label = getattr(_LIVE.info, "label", "") or "the camera"
    if hidden:
        return {"summary": f"Camera settings: {label} (not shown — dialogs "
                           f"are disabled)."}
    return {"summary": f"Camera settings: {label}."}


def settings_list() -> Dict[str, Any]:
    """Every setting the connected camera describes — key, label, group,
    type, value, min/max/step or choices, unit, read-only, live (False:
    the stream must stop to change it), held (why another setting owns it
    now) — as plain dicts, in the order a set is written. `groups` is the
    display order of the groups.

    Keys: settings, groups, camera, kind, capturing, summary
    """
    device = _require_device()
    try:
        described = device.settings()
    except Exception as exc:                              # noqa: BLE001
        raise RuntimeError(f"the camera did not describe its settings: "
                           f"{_said(exc)}") from exc
    label = getattr(_LIVE.info, "label", "") or "camera"
    return {"settings": [s.as_dict() for s in described],
            "groups": list(dict.fromkeys(s.group for s in described)),
            "camera": label, "kind": getattr(_LIVE.info, "kind", ""),
            "capturing": bool(_LIVE.capturing),
            "summary": f"{len(described)} settings on {label}."}


def set_camera_setting(key: Any, value: Any) -> Dict[str, Any]:
    """Write ONE setting and say what the camera took (clamped, snapped, or
    refused — `change`). A setting the stream is in the way of stops and
    restarts the live view (not while capturing); one that changes live
    (exposure, gain, a bias) applies at once, mid-capture too.

    Keys: change, key, value, ok, summary, pending
    """
    from council_core import camera_settings

    device = _require_device()
    _refuse_while_busy("change a setting")
    key = str(key or "").strip()
    try:
        provider = device.settings_provider()
        setting = provider.find(key)
    except camera_settings.SettingError as exc:
        raise RuntimeError(str(exc)) from exc
    # The setting just looked up, not the whole camera described again: a
    # settings window writes one of these per step of a slider.
    stop = bool(camera_settings.stops_needed(device, {key: value},
                                             described=[setting]))
    if stop:
        _refuse_while_capturing(f"changing {setting.label}")
    # Binning changes the area's own numbers (AREA_GROUPS). Read before and
    # after for those only: the area is four node reads, and a slider
    # writes one setting per step.
    moves_area = setting.group in camera_settings.AREA_GROUPS
    before = _roi_or_none(device) if moves_area else None

    def work() -> Any:
        try:
            return provider.set(key, value, setting=setting)
        except camera_settings.SettingError as exc:
            raise RuntimeError(str(exc)) from exc

    def finish(change: Any) -> Dict[str, Any]:
        if change.ok and not change.skipped:
            _camera_set([key])
        out = {"change": change.as_dict(), "key": key, "value": change.value,
               "ok": change.ok, "what": "setting", "pending": False,
               "summary": change.line()}
        now = _roi_or_none(device) if moves_area else None
        if now is not None and now != before:
            # The picture's pixels changed under the crop box: cleared, as
            # for any change of area (_settle_crop), and the area line and
            # the settings window's area box read again (area_moved).
            out.update(area=_area_text(now), crop="", area_moved=True)
            out["summary"] += (f" — the camera's area is now "
                               f"{_area_text(now)} (in its new pixels)")
        return out

    return _run_change(f"Changing {setting.label}", work, finish, stop)


def apply_camera_settings(values: Any, area: Any = None) -> Dict[str, Any]:
    """Write a whole set (a dict of key: value, as settings_list keys them)
    and, optionally, the camera's area in SENSOR pixels — in a safe order,
    the stream stopped once if anything in it needs that. Refused while
    capturing. The settings window's "Apply".

    Keys: applied, area, crop, ok, summary, pending
    """
    from council_core import cameras

    if not isinstance(values, dict):
        raise RuntimeError("settings come as a dict of name: value")
    roi = None
    if area not in (None, ""):
        box = _parse_area(area)
        if box is None:
            raise RuntimeError("type the camera's area as x, y, w, h")
        roi = cameras.Roi(*box)
    return _apply_set(values, roi, "Applying the camera settings",
                      "settings", "")


def _apply_set(values: Dict[str, Any], roi: Any, label: str, what: str,
               name: str) -> Dict[str, Any]:
    """Keys: applied, area, crop, ok, summary, pending"""
    from council_core import camera_settings

    device = _require_device()
    _refuse_while_capturing("applying a camera set-up" if what != "preset"
                            else "applying a preset")
    _refuse_while_busy("apply it")
    stop = bool(camera_settings.stops_needed(device, values, roi))
    before = _roi_or_none(device)

    def work() -> Any:
        return camera_settings.apply(device, values, roi)

    def finish(applied: Any) -> Dict[str, Any]:
        _camera_set(_changed_keys(applied))
        if what == "preset":
            # What the camera TOOK, so a value it snapped is not "changed
            # since" at Start; a preset only partly applied is not in use.
            _LIVE.preset_in_use = None
            if applied.ok:
                _preset_now_in_use(
                    name, {c.key: c.value for c in applied.changes if c.ok},
                    applied.roi)
        # Moved by its own area, or by binning in a set without one.
        moved = (roi is not None and applied.roi is not None) or (
            before is not None and _roi_or_none(device) != before)
        head = {"preset": f"Preset {name!r}",
                "reset": "Settings as connected"}.get(what, "Camera settings")
        return {"applied": applied.as_dict(), "ok": applied.ok,
                "area": _area_text(device.roi()),
                "crop": "" if moved else _current_crop(),
                "what": what, "name": name, "pending": False,
                "summary": f"{head}: {applied.summary()}"}

    return _run_change(label, work, finish, stop)


# ======================================================================
# Back to a known state: as connected, or the camera's own factory set
# ======================================================================
def _snapshot_or_nothing(device: Any) -> Dict[str, Any]:
    """Every savable setting now, or {} when the camera cannot say — a
    camera that cannot describe itself still connects."""
    from council_core import camera_settings

    try:
        return camera_settings.snapshot(device)
    except Exception:                                     # noqa: BLE001
        return {}


def camera_defaults() -> Dict[str, Any]:
    """What "Reset" can put back: `values`, every setting as the camera had
    it when it was connected, and `factory`, what the camera calls its own
    factory set ("" when it has none to load — an EVK4).

    Keys: values, factory, summary
    """
    device = _require_device()
    try:
        factory = device.settings_provider().defaults_source()
    except Exception:                                     # noqa: BLE001
        factory = ""
    values = dict(_LIVE.as_connected)
    said = f"{len(values)} settings as connected"
    if factory:
        said += f"; the camera's own defaults: {factory}"
    return {"values": values, "factory": factory, "summary": said + "."}


def reset_setting(key: Any) -> Dict[str, Any]:
    """Put ONE setting back as the camera had it when it was connected —
    for an EVK4 that is the sensor's default (it is opened with them).
    The same rules as set_camera_setting.

    Keys: change, key, value, ok, summary, pending
    """
    _require_device()
    key = str(key or "").strip()
    if key not in _LIVE.as_connected:
        raise RuntimeError(f"{key or 'that setting'} was not read when the "
                           f"camera was connected — there is nothing to put "
                           f"back")
    return set_camera_setting(key, _LIVE.as_connected[key])


def reset_camera_settings() -> Dict[str, Any]:
    """Put EVERY setting back as the camera had it when it was connected, in
    one safe-ordered set (the area is left alone: Full sensor is for that).
    Refused while capturing.

    Keys: applied, area, crop, ok, summary, pending
    """
    _require_device()
    if not _LIVE.as_connected:
        raise RuntimeError("the camera's settings were not read when it was "
                           "connected — there is nothing to put back")
    return _apply_set(dict(_LIVE.as_connected), None,
                      "Putting the settings back as connected", "reset", "")


def load_camera_defaults() -> Dict[str, Any]:
    """Load the camera's OWN factory settings — a Basler's UserSet
    "Default", which resets every setting and the area. The stream is
    stopped for it and started again (on a worker, like any change that
    needs a stop); refused while capturing, and on a camera with no such
    set (an EVK4 — use reset_camera_settings).

    Keys: area, crop, ok, summary, pending
    """
    device = _require_device()
    provider = device.settings_provider()
    source = provider.defaults_source()
    if not source:
        raise RuntimeError("this camera has no defaults of its own to load — "
                           "Reset puts its settings back as they were when "
                           "it was connected")
    _refuse_while_capturing("loading the camera's defaults")
    _refuse_while_busy("load the camera's defaults")

    def work() -> Any:
        provider.load_defaults()
        return device.roi()

    def finish(area: Any) -> Dict[str, Any]:
        text = _area_text(area)
        # Every setting went back: the latest word on all of the boxes.
        _camera_set(key for keys in START_BOXES.values() for key in keys)
        return {"area": text, "crop": "", "ok": True, "pending": False,
                "what": "defaults",
                "summary": f"Loaded the camera's own defaults ({source}); "
                           f"its area is {text}."}

    return _run_change("Loading the camera's defaults", work, finish,
                       bool(getattr(device, "streaming", False)))


# ======================================================================
# Presets (council_core.camera_presets), kept in the app's project folder
# ======================================================================
def _project_dir() -> Path:
    """The running app's project folder: where camera_setup.json lives."""
    path = _LIVE.setup_path or _fallback_setup_path()
    return Path(path).parent


def _preset_store() -> Any:
    from council_core import camera_presets

    return camera_presets.PresetStore(
        camera_presets.presets_path(_project_dir()))


def _identity() -> Any:
    from council_core import camera_presets

    if _LIVE.info is None:
        raise RuntimeError("connect the camera first — presets are kept per "
                           "camera")
    return camera_presets.Identity.of(_LIVE.info)


def list_presets() -> Dict[str, Any]:
    """This camera's presets, for a list: `presets` is the names (what a
    listbox shows and hands back), `rows` one descriptive line each,
    `details` everything (settings, area in sensor pixels, dates, whether
    it was saved on another unit of the same model). A damaged presets file
    is said in the summary and left untouched.

    Keys: presets, rows, details, problems, file, summary
    """
    from council_core import camera_presets

    if _LIVE.info is None:
        return {"presets": [], "rows": [], "details": [], "problems": [],
                "file": "", "summary": "Connect a camera to see its presets."}
    store = _preset_store()
    camera = _identity()
    try:
        listed = store.presets(camera)
    except camera_presets.PresetFileError as exc:
        then = ("this app will not save presets over it — use the newer "
                "app, or move the file away" if exc.newer else
                "saving a preset moves it aside and starts a new one")
        return {"presets": [], "rows": [], "details": [], "problems": [],
                "file": str(store.path),
                "summary": (f"Presets: {exc}. It is left exactly as it is; "
                            f"{then}.")}
    borrowed = sum(1 for p in listed if not p.own)
    summary = (f"{len(listed)} preset{'s' if len(listed) != 1 else ''} for "
               f"{camera.label}")
    if borrowed:
        summary += f" ({borrowed} saved on another {camera.model})"
    if store.problems:
        summary += f"; {len(store.problems)} unreadable, skipped"
    return {"presets": [p.name for p in listed],
            "rows": [p.line() for p in listed],
            "details": [p.as_dict() for p in listed],
            "problems": list(store.problems), "file": str(store.path),
            "summary": summary + "."}


def save_preset(name: Any, include_roi: Any = True,
                note: Any = "") -> Dict[str, Any]:
    """Save the camera as it is now — every setting worth saving and, with
    `include_roi`, its area in sensor pixels — as preset `name` of this
    camera, in this project. A preset of the same name is replaced.

    Keys: name, presets, rows, summary
    """
    from council_core import camera_presets

    device = _require_device()
    _refuse_while_busy("save a preset")
    camera = _identity()
    with_area = _truthy(include_roi)
    store = _preset_store()
    try:
        settings, roi = camera_presets.capture(device, with_area)
        preset, replaced, moved = store.save(
            camera, _picked(name), settings, roi, note=str(note or ""),
            repair=True)
    except camera_presets.PresetError as exc:
        raise RuntimeError(str(exc)) from exc
    # Saved FROM the camera as it is: the camera is that preset now.
    _preset_now_in_use(preset.name, settings, roi)
    said = (f"{'Replaced' if replaced else 'Saved'} preset {preset.name!r}: "
            f"{len(settings)} settings")
    said += (f" and the camera's area {_area_text(roi)}." if roi is not None
             else ", area left as it is when applied.")
    if moved is not None and store.repaired == "entry":
        said += (f" This camera's presets in the file were damaged: the "
                 f"file as it was is kept as {moved.name}; the other "
                 f"cameras' presets are untouched.")
    elif moved is not None:
        said += (f" The presets file was damaged: it was kept as "
                 f"{moved.name} and a new one started.")
    return _presets_changed(preset.name, said)


def _presets_changed(name: str, said: str) -> Dict[str, Any]:
    """The list after a save, rename or delete — returned, and told to every
    on_camera_change listener ({"what": "presets"}), so the main window's
    picker and an open settings window list the same presets whichever of
    them made the change.

    Keys: name, presets, rows, summary
    """
    listed = list_presets()
    out = {"name": name, "presets": listed["presets"],
           "rows": listed["rows"], "summary": said}
    _announce(dict(out, what="presets"))
    return out


def apply_preset(name: Any) -> Dict[str, Any]:
    """Put the camera back as preset `name` has it: its settings in a safe
    order, then its area, through the camera's own ROI. Accepts a listbox
    selection. Refused while capturing; a change the stream is in the way of
    stops and restarts the live view (on a worker — see _Job).

    Keys: applied, area, crop, ok, summary, pending
    """
    from council_core import camera_presets

    _require_device()
    chosen = _picked(name)
    if not chosen:
        raise RuntimeError("choose a preset in the list first")
    _refuse_while_capturing("applying a preset")
    _refuse_while_busy("apply a preset")
    try:
        preset = _preset_store().get(_identity(), chosen)
    except camera_presets.PresetError as exc:
        raise RuntimeError(str(exc)) from exc
    return _apply_set(preset.settings, preset.roi,
                      f"Applying preset {preset.name!r}", "preset",
                      preset.name)


def pick_preset(name: Any) -> Dict[str, Any]:
    """The main window's preset picker: apply the preset picked — or, for a
    name typed that is not a preset yet, say how to save one under it.

    Script-linkable, for an EDITABLE combobox whose list attach() keeps
    filled with this camera's presets: picking from the list applies, and
    typing a new name then pressing "Save preset" saves. Return in the box
    fires the same link as a pick, so a name that is not a preset is a hint
    here, not an error dialog for typing. Everything apply_preset refuses
    (no camera, capturing, a change still running) is still refused.

    Keys: applied, area, crop, ok, summary, pending
    """
    from council_core import camera_presets

    device = _require_device()
    chosen = _picked(name)
    hint = {"applied": {}, "area": _area_text(device.roi()),
            "crop": _current_crop(), "ok": False, "pending": False,
            "what": "hint", "name": chosen}
    if not chosen:
        return dict(hint, summary="Pick a preset in the list, or type a name "
                                  "and press Save preset.")
    try:
        names = [p.name.casefold()
                 for p in _preset_store().presets(_identity())]
    except camera_presets.PresetError as exc:
        raise RuntimeError(f"presets: {exc}") from exc
    if chosen.casefold() not in names:
        return dict(hint, summary=f"No preset called {chosen!r} yet — press "
                                  f"Save preset to save the camera as it is "
                                  f"now (its settings and its area) under "
                                  f"that name.")
    return apply_preset(chosen)


def delete_preset(name: Any) -> Dict[str, Any]:
    """Delete one of this camera's presets. Accepts a listbox selection.

    Keys: presets, rows, summary
    """
    from council_core import camera_presets

    chosen = _picked(name)
    if not chosen:
        raise RuntimeError("choose a preset in the list first")
    try:
        gone = _preset_store().delete(_identity(), chosen)
    except camera_presets.PresetError as exc:
        raise RuntimeError(str(exc)) from exc
    return _presets_changed("", f"Deleted preset {gone.name!r}.")


def rename_preset(old: Any, new: Any) -> Dict[str, Any]:
    """Rename one of this camera's presets. `old` accepts a listbox
    selection.

    Keys: name, presets, rows, summary
    """
    from council_core import camera_presets

    chosen = _picked(old)
    if not chosen:
        raise RuntimeError("choose a preset in the list first")
    try:
        renamed = _preset_store().rename(_identity(), chosen, _picked(new))
    except camera_presets.PresetError as exc:
        raise RuntimeError(str(exc)) from exc
    held = _LIVE.preset_in_use
    if held and str(held.get("name", "")).casefold() == chosen.casefold():
        held["name"] = renamed.name         # the run record says it as it is
    return _presets_changed(renamed.name,
                            f"Renamed {chosen!r} to {renamed.name!r}.")


def _truthy(value: Any) -> bool:
    """A checkbox port's value, or text from a box."""
    if isinstance(value, bool):
        return value
    return str(value if value is not None else "").strip().lower() not in (
        "", "0", "false", "no", "off")


# ======================================================================
# Exposure and gain
# ======================================================================
#: THE BOXES BESIDE START WAIT FOR A CHANGE ON THE WORKER — QUEUED, NOT
#: REFUSED. A change that needs the stream stopped (an area, a pixel format,
#: a preset) runs on a worker (_Job), which owns the camera until it has
#: stopped the stream, written and started it again. A box beside Start
#: wrote the camera straight from the UI thread meanwhile — two threads in
#: one SDK handle, mid-restart. Refusing would be noise: these boxes move in
#: arrow-click steps, and an error dialog per click for the ~0.15-1 s of a
#: restart says nothing the user can act on (the reason apply_frame_rate
#: already treats "no camera yet" softly), and a spin box cannot retry what
#: was refused, so the value would simply be lost. So the latest value of
#: each box is kept, said ("waits for ..."), and written on the UI thread
#: the moment the change is done (_write_queued, from _finish_job).
QUEUED_BOXES = ("exposure", "gain", "frame_rate")


def _queue_behind_job(box: str, value: Any) -> str:
    """Keep `value` for `box` when a camera change is on the worker; the
    line to say for it, or "" when nothing is running (write it now)."""
    job = _LIVE.job
    if job is None:
        return ""
    _LIVE.queued[box] = value
    doing = job.label[:1].lower() + job.label[1:]
    return f"is set once the camera has finished {doing}"


def _write_queued() -> str:
    """Write what the boxes beside Start asked for while a change was on
    the worker, in a fixed order, latest value of each; what came of it, as
    one line. Called on the UI thread once the worker is done."""
    queued, _LIVE.queued = _LIVE.queued, {}
    if _LIVE.device is None or not queued:
        return ""
    said = []
    for box in QUEUED_BOXES:
        if box not in queued:
            continue
        write = {"exposure": set_exposure, "gain": set_gain,
                 "frame_rate": apply_frame_rate}[box]
        try:
            said.append(write(queued[box])["summary"])
        except Exception as exc:                          # noqa: BLE001
            said.append(f"{box.replace('_', ' ')}: {_said(exc)}")
    return " ".join(said)


def set_exposure(value: Any) -> Dict[str, Any]:
    """Exposure in MICROSECONDS, which is what the camera's node map takes.
    Waits for a change on the worker (QUEUED_BOXES)."""
    device = _require_device()
    micros = _number(value, "exposure")
    waits = _queue_behind_job("exposure", micros)
    if waits:
        return {"exposure": f"{micros:.0f}",
                "summary": f"Exposure {micros:.0f} µs {waits}."}
    got = device.set_exposure_us(micros)
    return {"exposure": f"{got:.0f}", "summary": f"Exposure {got:.0f} µs."}


def set_gain(value: Any) -> Dict[str, Any]:
    """Waits for a change on the worker (QUEUED_BOXES)."""
    device = _require_device()
    gain = _number(value, "gain")
    waits = _queue_behind_job("gain", gain)
    if waits:
        return {"gain": f"{gain:.2f}", "summary": f"Gain {gain:.2f} {waits}."}
    got = device.set_gain(gain)
    return {"gain": f"{got:.2f}", "summary": f"Gain {got:.2f}."}


def set_frame_rate(value: Any) -> Dict[str, Any]:
    """Pictures a second. 0 is the camera's default.

    A frame camera is capped at this rate — the way to capture at a rate the
    disk can keep up with. An event camera has no frames: this sets how long
    each picture collects events (50 fps = 20 ms), and its .raw still has
    every event. Waits for a change on the worker (QUEUED_BOXES).
    """
    device = _require_device()
    fps = _number(value, "frame rate")
    waits = _queue_behind_job("frame_rate", fps)
    if waits:
        return {"frame_rate": f"{fps:.1f}",
                "summary": f"Frame rate {_fps_text(fps)} {waits}."}
    try:
        got = float(device.set_frame_rate(fps))
    except Exception as exc:                              # noqa: BLE001
        raise RuntimeError(f"frame rate: {exc}") from exc
    said = f"{got:.1f} fps" if got else "the camera's own rate"
    if getattr(getattr(device, "info", None), "kind", "") == "event" and got:
        said += f" ({1000.0 / got:.1f} ms windows)"
    return {"frame_rate": f"{got:.1f}", "summary": f"Frame rate: {said}."}


#: How long the live status line says what the FPS box just set. Long enough
#: for the measured rate beside it (a sliding window, capture.RATE_WINDOW) to
#: have caught up, so the user sees the new number arrive.
RATE_NOTE_SECONDS = 4.0

#: A camera rate this far from what was asked for is said to differ — the
#: camera's limit at this exposure and area, not float noise.
RATE_TOLERANCE = 0.02


def apply_frame_rate(frame_rate: Any = 0) -> Dict[str, Any]:
    """The FPS box: frames per second, applied to the camera NOW.

    Script-linkable, and meant for the FPS spin box itself, so a change takes
    effect as it is made — while capturing too — rather than only at the next
    Start. Start still passes the same box, so a rate set before any camera
    is connected is not lost.

    0 is the camera's own rate: free-running for a frame camera, 20 ms
    windows for an EVK4.

    NOT CONNECTED IS NOT AN ERROR HERE. The box can be changed at any time,
    and an error dialog for every arrow click before Connect would be noise:
    it says the rate will apply at Start instead. Everything else the camera
    refuses (no frame-rate control at all) is raised, as set_frame_rate does.

    WHERE THE ANSWER SHOWS. The summary goes to the status line, which the
    live view rewrites thirty times a second while the camera streams; so
    the live line itself carries the change for RATE_NOTE_SECONDS, beside the
    measured rate, and the two never fight over the line. Idle, the summary
    simply stays.

    MEASURED, not assumed, through this function mid-capture: on the pylon
    emulator AcquisitionFrameRate stays writable while grabbing
    (TLParamsLocked = 1), each write took under 0.4 ms, and the delivered
    rate went 9.9 -> 25.0 -> 10.0 -> 62.5 fps (0 = free-running) with every
    frame saved; the simulated camera did the same, 10.0 -> 25.0 -> 10.0 ->
    30.0.
    """
    fps = _number(frame_rate if str(frame_rate or "").strip() else 0,
                  "frame rate")
    if fps < 0:
        raise RuntimeError(f"frame rate must be 0 or more, not {fps:g}")
    # The box was just changed: at Start it is the latest word on the rate,
    # newer than a preset applied before it (_box_is_newer).
    _box_changed("frame_rate")
    wanted = _fps_text(fps)
    device = _LIVE.device
    if device is None:
        return {"frame_rate": f"{fps:.1f}",
                "summary": f"FPS {wanted} — no camera connected yet; it is "
                           f"applied when you press Start capture."}
    waits = _queue_behind_job("frame_rate", fps)
    if waits:
        # Said in the live line too, which the status line's own message
        # would not survive (see WHERE THE ANSWER SHOWS).
        _LIVE.rate_note = f"{wanted} next"
        _LIVE.rate_note_until = time.monotonic() + RATE_NOTE_SECONDS
        return {"frame_rate": f"{fps:.1f}",
                "summary": f"FPS {wanted} {waits}."}
    try:
        got = float(device.set_frame_rate(fps))
    except Exception as exc:                              # noqa: BLE001
        raise RuntimeError(f"frame rate: {exc}") from exc

    event = getattr(getattr(device, "info", None), "kind", "") == "event"
    if fps <= 0:
        # No number: a camera whose cap was just lifted may still report the
        # old setpoint as its rate (the emulator does), and "own rate (25)"
        # would be read as a cap that is still there.
        said = "0 (the camera's own rate)"
        note = "camera's own rate"
    else:
        said = f"{got:.1f}" if got else wanted
        note = f"set to {said}"
        if event and got:
            said += f" ({1000.0 / got:.1f} ms windows)"
        elif got and abs(got - fps) > RATE_TOLERANCE * fps:
            said = (f"{wanted} asked — the camera says it will run at "
                    f"{got:.1f} fps (its limit at this exposure and area)")

    session = _LIVE.session
    live = session is not None and session.running and (
        _LIVE.capturing or _LIVE.previewing)
    _LIVE.rate_note = note
    _LIVE.rate_note_until = time.monotonic() + RATE_NOTE_SECONDS
    if _LIVE.capturing:
        summary = f"FPS now {said}; the capture carries on at the new rate."
    elif live:
        summary = f"FPS now {said}."
    else:
        summary = f"FPS {said}, set on the camera."
    return {"frame_rate": f"{got:.1f}", "summary": summary}


def _fps_text(fps: float) -> str:
    """25 -> "25", 12.5 -> "12.5": what the user typed, not "25.0"."""
    return f"{fps:g}"


def _rate_note() -> str:
    """" (set to 25.0)" for a few seconds after the FPS box changed, else ""."""
    if not _LIVE.rate_note or time.monotonic() >= _LIVE.rate_note_until:
        return ""
    return f" ({_LIVE.rate_note})"


# ======================================================================
# Pop out
# ======================================================================
def pop_out() -> Dict[str, Any]:
    """Copy the picture on screen into a window of its own. Script-linkable.

    The window can go full screen (F11) on any monitor while the app carries
    on; each press opens another with its own copy.
    """
    canvas = _LIVE.canvas
    if canvas is None:
        raise RuntimeError("this app has no picture to pop out")
    from council_qt.widgets import pop_out as popping

    reviewer = _LIVE.reviewer
    title = reviewer.describe_shown() if reviewer is not None else "Camera"
    window = popping.pop_out(canvas, title)
    if window is None:
        said = "Nothing to pop out yet — the picture is empty."
    else:
        _LIVE.popouts = [w for w in _LIVE.popouts if _alive(w)] + [window]
        said = f"Popped out: {title}"
    view = said
    if reviewer is not None:
        view = reviewer._note_for(said[:60])
    return {"summary": said, "view": view}


def _alive(window: Any) -> bool:
    try:
        return bool(window.isVisible())
    except RuntimeError:                                  # already deleted
        return False


def _number(value: Any, what: str) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        raise RuntimeError(f"{what} must be a number, not {value!r}") from None


# ======================================================================
# Small helpers
# ======================================================================
def _require_device() -> Any:
    device = _LIVE.device
    if device is None:
        raise RuntimeError("connect a camera first")
    return device


def _require_session() -> Any:
    session = _LIVE.session
    if session is None:
        raise RuntimeError("connect a camera first")
    return session
