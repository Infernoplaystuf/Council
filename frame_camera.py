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
The capture folder IS the browse folder. Frames are written into it as PNGs,
and the generated app's _FrameBrowser already lists that folder, sorts it
naturally and drives the scrubber from it. So "review what I just captured" is
not a feature anyone has to build: it is what the existing panel does once the
frames land there. PNG specifically, because frame_timing, frame_roi and
frame_classes all discover frames by IMAGE_SUFFIXES and open them with Pillow —
a capture written as .npy is invisible to every one of them.

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

    def clear(self) -> None:
        self.device = None
        self.session = None
        self.info = None
        self.folder = None
        self.run = ""


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

    found = cameras.discover()
    _LIVE.found = found
    rows = [_row(i, c) for i, c in enumerate(found.cameras)]
    notes = "\n".join(found.notes) or "Every backend was searched."
    if found.cameras:
        summary = f"{len(found.cameras)} camera(s) found."
    else:
        summary = "No cameras found — see the notes."
    return {"rows": rows, "summary": summary, "notes": notes}


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

    raw = str(which if which is not None else "").strip()
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
def start(folder: Any, exposure: Any = "", gain: Any = "") -> Dict[str, Any]:
    """Begin the live view, and save every frame into `folder`.

    `folder` is the SAME folder the frame browser is pointed at, which is what
    makes the scrubber a review of what was just captured.

    Each run writes under its own stamped stem, so starting a second run into
    the same folder adds to it and can never overwrite the first — this module
    has no way to delete anything and should not have one.
    """
    from council_core import capture

    session = _require_session()
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

    run = time.strftime("%Y%m%d_%H%M%S")
    recorder = capture.Recorder(out, stem=f"{run}_frame")
    try:
        session.record_to(recorder)
    except OSError as exc:
        raise RuntimeError(f"cannot save frames into {out}: {exc}") from exc
    _LIVE.folder = out
    _LIVE.run = run
    session.start()
    return {"summary": f"Capturing into {out}.", "folder": str(out),
            "run": run}


def stop() -> Dict[str, Any]:
    """End the run. Returns whether the grab thread actually stopped."""
    session = _LIVE.session
    if session is None:
        return {"summary": "Not capturing."}
    ended = session.stop()
    session.record_to(None)
    stats = session.stats()
    if not ended:
        # The truth, not a hopeful message. Something is still holding the
        # camera, and the next start would be racing it.
        return {"summary": "The camera did not stop cleanly.",
                "status": stats.line()}
    return {"summary": f"Stopped. {stats.line()}", "status": stats.line()}


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
    if say is not None:
        session = _LIVE.session
        if session is not None:
            say(_status_line(session.stats(), frame))
    return frame is not None


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
