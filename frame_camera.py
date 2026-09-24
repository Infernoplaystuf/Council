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

PNG specifically, because frame_timing, frame_roi and frame_classes all
discover frames by IMAGE_SUFFIXES and open them with Pillow — a capture
written as .npy is invisible to every one of them. The .raw is the event
camera's real data: a PNG is a 20 ms picture of it, and PNGs may be skipped
when storage falls behind, but the .raw is written as the SDK hands the bytes
over and loses nothing.

Save to a LOCAL folder and copy the run afterwards. A network share could not
keep up with a real EVK4.

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

import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

#: How often a UI timer should call `pump`. ~30 Hz: a display consumes about
#: thirty frames a second no matter what the sensor does.
LIVE_MS = 33


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
        #: Whether the status line has said its last word about the latest
        #: run. The pump writes the live numbers only while there is
        #: something live to report; otherwise it would overwrite every other
        #: message in that line thirty times a second.
        self.reported = True
        #: The run is being saved to a network share. Said in the live
        #: status line, because a message returned by Start is overwritten
        #: by the live numbers within one tick.
        self.network = False

    def clear(self) -> None:
        self.device = None
        self.session = None
        self.info = None
        self.folder = None
        self.run = ""
        self.raw_path = None
        self.reported = True
        self.network = False


_LIVE = _Live()
_LOCK = threading.Lock()


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
        _LIVE.device = device
        _LIVE.info = info
        _LIVE.session = capture.CaptureSession(device)
        _LIVE.reported = True

    limits = device.limits()
    area = device.roi()
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
        _LIVE.clear()
    if session is not None:
        session.close()
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


def start(folder: Any, exposure: Any = "", gain: Any = "") -> Dict[str, Any]:
    """Begin the live view, and save every frame into `folder`.

    `folder` is the SAME folder the slider shows, which is what makes the
    slider a review of what is being captured.

    Each run writes under its own stamped name, so starting a second run into
    the same folder adds to it and can never overwrite the first — this module
    has no way to delete anything and should not have one.

    AN EVENT CAMERA ALSO RECORDS ITS .raw, started BEFORE the stream so the
    file holds the whole run (see EvkDevice.start_raw).
    """
    from council_core import capture

    session = _require_session()
    if session.running:
        raise RuntimeError("already capturing — stop first")
    where = str(folder or "").strip().strip('"')
    if not where:
        raise RuntimeError("choose a folder to save frames into first")
    out = Path(where)
    if out.exists() and not out.is_dir():
        raise RuntimeError(f"{out} is a file, not a folder")

    if str(exposure or "").strip():
        set_exposure(exposure)
    if str(gain or "").strip():
        set_gain(gain)

    run = _unique_run(out)
    recorder = capture.Recorder(out, stem=f"{run}_frame",
                                index_name=f"{run}_frames.csv")
    try:
        session.record_to(recorder)
    except OSError as exc:
        raise RuntimeError(f"cannot save frames into {out}: {exc}") from exc

    device = session.device
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
    try:
        session.start()
    except Exception:
        if raw is not None:
            try:
                device.stop_raw()
            except Exception:                             # noqa: BLE001
                pass
        session.record_to(None)
        raise

    said = f"Capturing into {out}."
    if raw is not None:
        said += f" Raw: {raw.name}."
    if _LIVE.network:
        said += (" This folder is on a network share — save to a local "
                 "folder and copy the run afterwards, or frames will be "
                 "skipped.")
    return {"summary": said, "folder": str(out), "run": run,
            "raw": str(raw) if raw is not None else ""}


def stop() -> Dict[str, Any]:
    """End the run. Returns whether the grab thread actually stopped.

    Stopping the camera also finishes the .raw (EvkDevice.stop). Saving the
    PNGs still queued is given STOP_DRAIN_SECONDS; anything left after that
    carries on in the background and the status line counts it down, so a
    slow disk never freezes the window.
    """
    session = _LIVE.session
    if session is None:
        return {"summary": "Not capturing."}
    ended = session.stop()
    session.record_to(None, drain_timeout=STOP_DRAIN_SECONDS)
    stats = session.stats()
    if not ended:
        # The truth, not a hopeful message. Something is still holding the
        # camera, and the next start would be racing it.
        return {"summary": "The camera did not stop cleanly.",
                "status": stats.line()}
    line = _stopped_line(session)
    return {"summary": line, "status": stats.line()}


def _stopped_line(session: Any) -> str:
    """What the status line says once a run is over — or nearly over."""
    stats = session.stats()
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
    frame = latest()
    if frame is not None:
        show(frame.image)
    _report(say, frame)
    return frame is not None


def _report(say: Optional[Callable[[str], Any]], frame: Any) -> None:
    """The status line: live numbers while capturing or saving, then one
    final line, then silence — so "Connected to ..." and every other message
    written there stays readable."""
    session = _LIVE.session
    if say is None or session is None:
        return
    if session.running or session.saving:
        say(_status_line(session.stats(), frame))
        _LIVE.reported = False
    elif not _LIVE.reported:
        say(_stopped_line(session))
        _LIVE.reported = True


def _tick(show: Callable[[Any], Any], say: Optional[Callable[[str], Any]],
          reviewer: Any) -> None:
    """One timer tick: the newest frame to the reviewer (or straight to the
    canvas when the app has none), then the status line."""
    if reviewer is None:
        pump(show, say)
        return
    frame = latest()
    reviewer.tick(frame)
    _report(say, frame)


def attach(app: Any, view: str = "live_view",
           status: str = "capture_status",
           interval_ms: int = LIVE_MS, first_run: bool = True,
           wizard: Optional[Callable[[Any], Any]] = None,
           scrubber: str = "frame", folder: str = "capture_folder",
           current: str = "current_frame", roi: str = "roi",
           view_status: str = "view_status") -> Any:
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

    _LIVE.setup_path = _setup_path_for(app)
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


class _Feed:
    """What the reviewer is told about the capture. Read on the UI thread."""

    def capturing(self) -> bool:
        session = _LIVE.session
        return bool(session is not None and session.running)

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
        for marker in ("_frame_", "_events.raw", "_frames.csv"):
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
    """
    from council_core import camera_setup

    path = _LIVE.setup_path or _fallback_setup_path()
    _LIVE.setup_path = path
    if _dialogs_disabled():
        said = "Camera setup skipped — dialogs are disabled."
    else:
        from PySide6.QtWidgets import QApplication
        from council_qt.widgets import camera_wizard

        window = parent or QApplication.activeWindow()
        title = window.windowTitle() if window is not None else ""
        chosen = camera_wizard.run_wizard(window, path, app_name=title)
        said = (f"Set up for {camera_setup.label(chosen)}." if chosen else
                "Camera setup cancelled — nothing changed.")
    listed = list_cameras()
    listed["summary"] = f"{said} {listed['summary']}"
    return listed


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


def _status_line(stats: Any, frame: Any) -> str:
    line = stats.line()
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
    """Set the camera's own AOI, so future frames ARE that size.

    Not a crop. Cropping moves every pixel over the link and throws most of
    them away; a sensor AOI reads out less, which on a boA5320-150cm is the
    frame rate, and on an event camera stops the masked pixels emitting at all.

    The request is SNAPPED to what the sensor accepts and the result says what
    was actually taken — a box that silently moves is a box the user fights.
    """
    from council_core import cameras

    device = _require_device()
    box = _parse_area(area)
    if box is None:
        raise RuntimeError("type the area as x, y, w, h")

    was_running = _LIVE.session is not None and _LIVE.session.running
    if was_running and getattr(device, "raw_path", None) is not None:
        # Changing the area restarts the stream, and restarting the stream
        # ends the .raw — the rest of the run would be missing from it.
        raise RuntimeError("stop the capture before changing the camera's "
                           "area — it would cut the raw recording short")
    if was_running:
        _LIVE.session.stop()
    try:
        got = device.set_roi(cameras.Roi(*box))
    finally:
        if was_running:
            _LIVE.session.start()

    text = _area_text(got)
    snapped = tuple(got.as_tuple()) != tuple(box)
    return {"area": text,
            "summary": (f"Area set to {text}"
                        + (" — snapped to what the sensor accepts."
                           if snapped else ".")),
            "snapped": snapped}


def full_frame() -> Dict[str, Any]:
    """Give the whole sensor back."""
    device = _require_device()
    limits = device.limits()
    return set_area(f"0, 0, {limits.width}, {limits.height}")


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


# ======================================================================
# Exposure and gain
# ======================================================================
def set_exposure(value: Any) -> Dict[str, Any]:
    """Exposure in MICROSECONDS, which is what the camera's node map takes."""
    device = _require_device()
    micros = _number(value, "exposure")
    got = device.set_exposure_us(micros)
    return {"exposure": f"{got:.0f}", "summary": f"Exposure {got:.0f} µs."}


def set_gain(value: Any) -> Dict[str, Any]:
    device = _require_device()
    got = device.set_gain(_number(value, "gain"))
    return {"gain": f"{got:.2f}", "summary": f"Gain {got:.2f}."}


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
