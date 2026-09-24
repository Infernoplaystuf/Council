"""
council_qt.widgets.capture_review — Typhon's slider. Watch a capture, scrub
back through it while it is still running, play it like a video, and swap
between the saved PNGs and an event camera's raw recording.

WHY NOT THE GENERATED _FrameBrowser
That browser lists a folder when the folder CHANGES. A capture writes into the
folder the browser is already showing, so nothing changes and the slider keeps
the range it had before Start. That is the "slider did nothing after the
capture" a real EVK4 test found. Rescanning a folder of thousands of files
thirty times a second is not the fix. The writer already knows every file it
wrote, in order (FrameWriter.paths), so new frames are taken from there.

THE DVR RULE
While a capture runs, the slider's LAST position means "live": the camera's
newest frame, not a file. Drag back and you review a saved frame while the
capture carries on underneath. The range keeps growing and your position stays
where you left it. Drag to the end, or Play until you reach it, and you are
live again.

BEFORE A CAPTURE: THE LIVE PREVIEW
Connected, not capturing, and nothing in the folder to review: the picture
shows what the camera sees, and nothing is saved. `wants_preview` is the whole
rule; frame_camera starts and stops the camera from it, and this draws what
arrives. With frames in the folder the slider shows those instead.

PNG AND RAW ARE TWO VIEWS OF ONE RUN
The PNGs are what the camera looked like, one accumulation window each. They
are written through a queue that may skip frames when storage falls behind.
The raw file is every event the sensor sent. Swapping views keeps your place
in the run: the raw view opens at the moment the PNG on screen was taken, and
swapping back lands on the PNG nearest the moment the raw view was showing.

ONE CONTROLLER PER CANVAS
This owns the canvas, the slider and the ROI box. frame_camera.attach creates
it only for an app with no generated _FrameBrowser on the same slider. Two
controllers drawing into one canvas is how a scrubbed frame gets replaced by a
live one 33 ms later.

EVERYTHING HERE RUNS ON THE UI THREAD
frame_camera's QTimer calls `tick`, and the slider, the buttons and the play
timer are all Qt events. Nothing is pushed from the capture thread. This takes
from the writer's append-only list, which is the same pull the live view uses.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence

from PySide6.QtCore import QObject, Qt, QTimer

#: What a saved frame can be. The same list the generated browser uses.
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif",
                  ".webp")

#: Coalesce a typed folder path, and a slider drag. The generated browser's
#: measured intervals.
SCAN_MS = 150
SHOW_MS = 30

#: PNG playback, about 30 frames a second. A PNG has no timestamp of its own
#: to play "in real time" against, and a display cannot show more anyway.
PNG_PLAY_MS = 33

#: How often raw playback redraws. Raw playback IS real time: each redraw
#: shows the window the wall clock has reached, skipping any it could not draw
#: in time, the way a video player drops frames rather than slowing down.
RAW_PLAY_MS = 33

#: How long a note ("No raw file for this run") stays in the view line before
#: the ordinary position text comes back.
NOTE_SECONDS = 4.0

PNG, RAW = "png", "raw"

#: A run's files are "<run>_frame_<index>.png", "<run>_frames.csv" and
#: "<run>_events.raw", where <run> is the time the capture started, with "_2",
#: "_3" added when two runs start in the same second (frame_camera.start).
RUN_OF = re.compile(r"^(\d{8}_\d{6}(?:_\d+)?)_frame_\d+", re.IGNORECASE)
RAW_SUFFIX = "_events.raw"


def natural_key(path: Any) -> list:
    """frame_9 before frame_10 — the generated browser's _natkey, exactly."""
    parts = []
    for seg in str(path).replace(chr(92), "/").split("/"):
        row = []
        for tok in re.split("([0-9]+)", seg):
            if tok.isdigit():
                row.append((0, int(tok), ""))
            else:
                row.append((1, 0, tok.lower()))
        parts.append(row)
    return parts


def list_images(folder: str) -> List[str]:
    """Every saved frame directly in `folder`, in capture order."""
    files: List[str] = []
    try:
        with os.scandir(folder) as found:
            for entry in found:
                if (entry.is_file() and
                        os.path.splitext(entry.name)[1].lower() in IMAGE_SUFFIXES):
                    files.append(entry.path)
    except OSError:
        return []
    files.sort(key=natural_key)
    return files


def run_of(path: Any) -> str:
    """The run a saved frame belongs to, or "" for a file not named by us."""
    found = RUN_OF.match(os.path.basename(str(path)))
    return found.group(1) if found else ""


def run_of_raw(path: Any) -> str:
    """The run a raw recording belongs to: "<run>_events.raw" -> "<run>"."""
    name = os.path.basename(str(path or ""))
    if not name.lower().endswith(RAW_SUFFIX):
        return ""
    return name[:-len(RAW_SUFFIX)]


def raw_path_for(folder: Any, run: str) -> Optional[Path]:
    """The raw recording of `run`, if there is one."""
    if not run or not folder:
        return None
    path = Path(str(folder)) / f"{run}{RAW_SUFFIX}"
    return path if path.is_file() else None


def latest_raw(folder: Any) -> Optional[Path]:
    """The newest raw recording in `folder`, by run name."""
    try:
        names = [e.name for e in os.scandir(str(folder))
                 if e.is_file() and e.name.lower().endswith(RAW_SUFFIX)]
    except OSError:
        return None
    if not names:
        return None
    return Path(str(folder)) / sorted(names, key=natural_key)[-1]


class DisplayDecoder:
    """A saved frame as something the canvas can show.

    16-BIT FRAMES ARE SHIFTED, NOT DIVIDED BY 256. A 12-bit Basler frame is
    saved as a 16-bit PNG holding 0..4095 (the file is the data, see
    capture.write_image). Dividing by 256, as the generated browser does,
    shows it almost black. Shifting by the bits it actually uses shows it as
    the camera saw it.

    THE SHIFT ONLY EVER GROWS within a folder. Worked out per frame, a dark
    frame would get a smaller shift than its neighbours and flash brighter
    during playback.
    """

    def __init__(self) -> None:
        self.shift = 0

    def reset(self) -> None:
        self.shift = 0

    def __call__(self, path: str) -> Any:
        from PIL import Image

        with Image.open(path) as im:
            # draft() makes libjpeg decode a large JPEG at reduced size; it
            # is a no-op for PNG.
            im.draft("RGB", (2048, 2048))
            if im.mode in ("I", "I;16", "I;16B", "I;16L"):
                import numpy as np

                data = np.asarray(im)
                top = int(data.max()) if data.size else 0
                self.shift = max(self.shift, max(0, top.bit_length() - 8))
                return (data >> self.shift).astype(np.uint8)
            if im.mode == "F":
                return im.convert("L")
            # copy() forces the decode, so the pixels survive the `with`.
            return im.copy()


class _NoFeed:
    """What the reviewer sees when no camera is involved: a folder only."""

    def capturing(self) -> bool:
        return False

    def saving(self) -> bool:
        return False

    def run(self) -> str:
        return ""

    def written(self, start: int = 0) -> Sequence[Any]:
        return []

    def raw_growing(self, path: Any) -> bool:
        return False

    def previewing(self) -> bool:
        return False


class CaptureReviewer(QObject):
    """The canvas, the slider and the ROI box, for one capture folder.

    `canvas` is the generated ImageCanvas. `scrubber` is the generated Scrubber
    widget. `folder`, `current`, `roi` and `view` are PORTS (get/set/on_change).
    `feed` reports the capture: see frame_camera._Feed, or _NoFeed.
    `open_raw(path, window_us)` opens a raw recording for playback; see
    council_core.event_playback.open_raw.
    """

    def __init__(self, *, canvas: Any, scrubber: Any, folder: Any,
                 current: Any = None, roi: Any = None, view: Any = None,
                 feed: Any = None,
                 open_raw: Optional[Callable[[Any, int], Any]] = None,
                 window_us: int = 20_000, parent: Any = None):
        super().__init__(parent)
        self.canvas = canvas
        self.scrubber = scrubber
        self.folder = folder
        self.current = current
        self.roi = roi
        self.view = view
        self.feed = feed or _NoFeed()
        self._open_raw = open_raw or _default_open_raw
        self.window_us = int(window_us)
        self.decode = DisplayDecoder()

        self.mode = PNG
        self.files: List[str] = []
        self._known: set = set()
        self.root = ""
        self._root_key = ""
        #: At the end of the slider while capturing: show the camera, not a file.
        self.live = False
        self.playing = False
        self.raw: Any = None
        self.raw_file: Optional[Path] = None
        self._capturing = False
        self._growing = False
        #: The camera is running for the preview (see wants_preview).
        self._previewing = False
        self._run = ""
        self._seen = 0
        self._shown: Any = None
        self._note = ""
        self._note_until = 0.0
        self._play_anchor = (0.0, 0)
        self._png_due = 0.0
        self._roi_syncing = False
        #: Set while THIS code moves the slider. The generated Scrubber
        #: reports a clamped QSlider as a user move: shrinking its range
        #: emits valueChanged unblocked, and that reaches _moved.
        self._moving = False
        #: Stopped while live, with frames still on their way to disk: keep
        #: going to the END as they land, unless the user moves the slider.
        self._end_after_save = False
        #: The raw window to show once the reader has got that far.
        self._raw_target: Optional[int] = None
        #: The window the open raw view uses (the run's own, from its CSV).
        self._raw_window = int(window_us)
        self._raw_seen = -1
        self._raw_finished = False

        self._scan = QTimer(self)
        self._scan.setSingleShot(True)
        self._scan.timeout.connect(self.reload)
        self._show = QTimer(self)
        self._show.setSingleShot(True)
        self._show.timeout.connect(self._show_now)
        self._play = QTimer(self)
        self._play.setSingleShot(True)
        # PRECISE. Windows rounds Qt's default coarse timers to its ~15.6 ms
        # tick, so a 33 ms frame fired at ~47 ms: measured 20 fps, not 30.
        self._play.setTimerType(Qt.TimerType.PreciseTimer)
        self._play.timeout.connect(self._advance)

        scrubber.on_step(self._moved)
        folder.on_change(lambda *_: self._scan.start(SCAN_MS))
        hook = getattr(canvas, "on_roi_change", None)
        if roi is not None and callable(hook):
            hook(self._roi_drawn)
            roi.on_change(self._roi_typed)
        # DEFERRED, like the generated browser: this is built inside the
        # window's __init__, before the canvas has a size to draw into.
        QTimer.singleShot(0, self.reload)

    # ==================================================================
    # The folder
    # ==================================================================
    def reload(self, keep: Optional[str] = None, at_end: bool = False) -> None:
        """List the folder again. Keeps the frame on screen if it is still
        there (`keep` names it), or goes to the end, or to the first frame."""
        self._scan.stop()
        folder = str(self.folder.get() or "").strip().strip('"')
        if folder and not os.path.isdir(folder):
            return                  # a half-typed path: keep what is loaded
        if _key(folder) != self._root_key:
            self.decode.reset()
            if self.mode == RAW:
                self._close_raw()
            keep = None
        self.root = folder
        self._root_key = _key(folder)
        self.files = list_images(folder) if folder else []
        self._known = set(self.files)
        self._set_range()
        if self.mode == RAW:
            return
        if keep and keep in self._known:
            index = self.files.index(keep)
        elif at_end or self.live:
            index = len(self.files) - 1
        else:
            index = 0
        self._shown = None
        self._slider_to(max(0, index))
        if not self.live:
            self._show_png(max(0, index))
        self._say()

    def _take_new(self) -> None:
        """Add the frames the writer has saved since the last look."""
        new = self.feed.written(self._seen)
        if not new:
            return
        self._seen += len(new)
        added = 0
        for path in new:
            text = str(path)
            if text in self._known or _key(os.path.dirname(text)) != self._root_key:
                continue
            self.files.append(text)
            self._known.add(text)
            added += 1
        if not added:
            return
        if self.mode == PNG:
            self._set_range()
            if self.live:
                self._slider_to(len(self.files) - 1)
            elif self._end_after_save:
                self._slider_to(len(self.files) - 1)
                self._show_png(len(self.files) - 1)
        self._say()

    def _set_range(self) -> None:
        count = self.raw.count if (self.mode == RAW and self.raw) else len(self.files)
        self._slider_range(max(0, count - 1))

    # ==================================================================
    # The capture
    # ==================================================================
    def wants_preview(self) -> bool:
        """Show the camera live without saving? Only with nothing to review:
        no frames in the folder, no capture running, not in the raw view."""
        return self.mode == PNG and not self._capturing and not self.files

    def tick(self, frame: Any = None) -> bool:
        """Called ~30 times a second with the camera's newest frame, or None.

        Returns whether the live frame was shown.
        """
        capturing = bool(self.feed.capturing())
        growing = capturing or bool(self.feed.saving())
        run = str(self.feed.run() or "")
        if run != self._run:
            # A new run means a new writer, whose list starts empty.
            self._run = run
            self._seen = 0
        if capturing and not self._capturing:
            self._began()
        self._capturing = capturing
        if growing:
            self._take_new()
        if self._growing and not growing:
            self._finished()
        elif not capturing and self.live:
            self._stopped()
        self._growing = growing
        if self.mode == RAW and self.raw is not None:
            self._raw_progress()
        if self._note and time.monotonic() >= self._note_until:
            # A note is shown for NOTE_SECONDS; the label only changes when
            # told to, so without this an old note stays up indefinitely.
            self._say()

        previewing = bool(getattr(self.feed, "previewing", lambda: False)())
        if previewing != self._previewing:
            self._previewing = previewing
            if not previewing and self.wants_preview():
                # The preview ended (camera disconnected, or it failed) with
                # nothing to review: do not leave its last frame looking live.
                self._clear_canvas()
            self._say()

        if self.live and self.mode == PNG and frame is not None:
            self.canvas.set_array(frame.image)
            self._shown = None
            return True
        if previewing and frame is not None and self.wants_preview():
            self.canvas.set_array(frame.image)
            self._shown = None
            return True
        return False

    def _clear_canvas(self) -> None:
        self._shown = None
        try:
            self.canvas.set_image(None)
        except Exception:                                   # noqa: BLE001
            pass

    def _began(self) -> None:
        self._stop_playing()
        self._end_after_save = False
        if self.mode == RAW:
            self._close_raw()
            self.mode = PNG
        # A folder typed a moment ago may not have been scanned yet, and the
        # capture is writing into it now.
        if self._scan.isActive() or _key(self.folder.get() or "") != self._root_key:
            self.reload()
        self.live = True
        self._set_range()
        self._slider_to(max(0, len(self.files) - 1))
        self._set_current("")
        self._say()

    def _stopped(self) -> None:
        """The camera stopped; frames may still be on their way to disk."""
        self.live = False
        self._end_after_save = True
        if self.mode == PNG and self.files:
            self._slider_to(len(self.files) - 1)
            self._show_png(len(self.files) - 1)
        self._say()

    def _finished(self) -> None:
        """Everything is on disk. List the folder properly, keeping your place."""
        to_end = self.live or self._end_after_save
        self.live = False
        self._end_after_save = False
        keep = self._shown[2] if self._shown and self._shown[0] == PNG else None
        if self.mode == PNG:
            self.reload(keep=None if to_end else keep,
                        at_end=to_end or keep is None)
        self._say()

    # ==================================================================
    # Showing one position
    # ==================================================================
    def _slider_to(self, index: int) -> None:
        self._moving = True
        try:
            self.scrubber.set(index)
        finally:
            self._moving = False

    def _slider_range(self, hi: int) -> None:
        self._moving = True
        try:
            self.scrubber.set_range(0, hi)
        finally:
            self._moving = False

    def _moved(self, index: int) -> None:
        """The USER moved the slider (drag, arrows, or a typed number)."""
        if self._moving:
            return
        self._end_after_save = False            # the user chose a place
        if self.playing:
            # Playback carries on from where the user put it, rather than
            # snapping back to where the play clock thinks it should be.
            self._play_anchor = (time.monotonic(), int(index))
        if self.mode == PNG and self._capturing:
            live = int(index) >= len(self.files) - 1
            if live != self.live:
                self.live = live
                if live:
                    self._stop_playing()
                    self._set_current("")
            if live:
                self._say()
                return
        self._show.start(SHOW_MS)

    def _show_now(self) -> None:
        index = self.scrubber.get()
        if self.mode == RAW:
            self._show_raw(index)
        else:
            self._show_png(index)

    def _show_png(self, index: int) -> None:
        if not self.files:
            self._set_current("")
            if not self._previewing:
                # Nothing to show — unless the preview is about to draw here.
                self._clear_canvas()
            self._say()
            return
        index = max(0, min(int(index), len(self.files) - 1))
        path = self.files[index]
        if self._shown == (PNG, index, path):
            return
        try:
            image = self.decode(path)
        except Exception as exc:                            # noqa: BLE001
            self._shown = None
            self._set_current("")
            self.canvas.show_message(
                f"Cannot read {os.path.basename(path)}: {exc}")
            self._say()
            return
        self.canvas.set_image(image)
        self._shown = (PNG, index, path)
        self._set_current(path)
        self._say()

    def _show_raw(self, index: int) -> None:
        if self.raw is None or self.raw.count < 1:
            return
        index = max(0, min(int(index), self.raw.count - 1))
        if self._shown == (RAW, index, self.raw_file):
            return
        try:
            image = self.raw.frame(index)
        except Exception as exc:                            # noqa: BLE001
            self._shown = None
            self.canvas.show_message(f"Cannot read the raw file: {exc}")
            self._note_for(f"Raw read failed: {exc}")
            return
        self.canvas.set_array(image, copy=False)
        self._shown = (RAW, index, self.raw_file)
        self._say()

    # ==================================================================
    # Play / pause
    # ==================================================================
    def play_pause(self) -> str:
        """Start or stop playback. Returns what happened, in a few words."""
        if self.playing:
            self._stop_playing()
            self._say()
            return "Paused."
        if self.mode == RAW:
            if self.raw is None or self.raw.count < 1:
                return self._note_for("Nothing to play")
            if self.scrubber.get() >= self.raw.count - 1:
                self._slider_to(0)
        else:
            if not self.files:
                return self._note_for("No frames to play yet")
            if self.live:
                return self._note_for("Live — drag back, then Play")
            if self.scrubber.get() >= len(self.files) - 1 and not self._capturing:
                self._slider_to(0)
        self.playing = True
        self._play_anchor = (time.monotonic(), self.scrubber.get())
        self._png_due = time.monotonic() + PNG_PLAY_MS / 1000.0
        self._show_now()
        self._play.start(PNG_PLAY_MS if self.mode == PNG else RAW_PLAY_MS)
        self._say()
        return "Playing."

    def _stop_playing(self) -> None:
        self.playing = False
        self._play.stop()

    def _advance(self) -> None:
        if not self.playing:
            return
        if self.mode == PNG:
            index = self.scrubber.get() + 1
            if index > len(self.files) - 1:
                self._stop_playing()
                if self._capturing:
                    # Caught up with the capture: that is live.
                    self.live = True
                    self._set_current("")
                self._say()
                return
            now = time.monotonic()
            if now < self._png_due - 0.002:
                self._play.start(max(1, int((self._png_due - now) * 1000)))
                return
            self._slider_to(index)
            self._show_png(index)
            # ON A SCHEDULE: each frame is due 33 ms after the previous one
            # was DUE, not after it was shown. Measured, a fixed 33 ms after
            # each decode gave 17 fps, and timing from the moment shown still
            # gave 21 under load — Windows timers fire up to ~15 ms late, and
            # that lateness was added to every frame. A player behind by more
            # than a few frames restarts its schedule rather than bursting.
            self._png_due += PNG_PLAY_MS / 1000.0
            now = time.monotonic()
            if self._png_due < now - 0.1:
                self._png_due = now + PNG_PLAY_MS / 1000.0
            self._play.start(max(1, int((self._png_due - now) * 1000)))
            return
        if self.raw is None:
            self._stop_playing()
            return
        began, first = self._play_anchor
        elapsed_us = (time.monotonic() - began) * 1e6
        index = first + int(elapsed_us // max(1, self.raw.window_us))
        last = self.raw.count - 1
        if index >= last:
            index = last
            if self.raw.done:
                self._stop_playing()
            # Still reading: hold at the edge and carry on as it grows.
        if index != self.scrubber.get() or not self.playing:
            self._slider_to(index)
            self._show_raw(index)
        if self.playing:
            self._play.start(RAW_PLAY_MS)
        else:
            self._say()

    # ==================================================================
    # PNG <-> raw
    # ==================================================================
    def toggle_view(self) -> str:
        """Swap between the saved PNGs and the raw recording of the same run."""
        if self.mode == RAW:
            return self._to_png()
        return self._to_raw()

    def _to_raw(self) -> str:
        shown = self._shown[2] if self._shown and self._shown[0] == PNG else ""
        run = run_of(shown) if shown else ""
        path = raw_path_for(self.root, run) if run else None
        if path is None and not shown:
            path = latest_raw(self.root) if self.root else None
        if path is None:
            if self.files and not run and shown:
                return self._note_for("No raw file for this frame")
            return self._note_for("No raw file for this run")
        if self.feed.raw_growing(path):
            return self._note_for("Raw opens after Stop")
        from council_core import event_playback

        index = Path(self.root) / f"{run_of_raw(path)}_frames.csv"
        origin = event_playback.raw_origin(index)
        extra = {} if origin is None else {"origin_us": origin}
        # The run's own window: a run captured at 200 fps has 5 ms pictures,
        # and its raw view should show the same.
        self._raw_window = event_playback.run_window_us(index) or self.window_us
        try:
            playback = self._open_raw(path, self._raw_window, **extra)
        except Exception as exc:                            # noqa: BLE001
            return self._note_for(f"Cannot open raw: {exc}")

        self._stop_playing()
        self.live = False
        self.raw = playback
        self.raw_file = path
        self.mode = RAW
        self._shown = None
        self._raw_target = self._raw_index_for(path, shown)
        self._raw_seen = -1
        self._raw_finished = False
        self._set_current("")
        self.canvas.show_message(f"Reading {path.name}…")
        self._raw_progress()
        return f"Raw view: {path.name}"

    def _raw_progress(self) -> None:
        """Take in what the reader has finished since the last look."""
        raw = self.raw
        count = raw.count
        if count != self._raw_seen:
            self._raw_seen = count
            self._slider_range(max(0, count - 1))
            target = self._raw_target
            if target is not None and (count > target or raw.done):
                self._raw_target = None
                index = min(target, max(0, count - 1))
                self._slider_to(index)
                if count:
                    self._show_raw(index)
            elif target is None and self._shown is None and count:
                self._show_raw(self.scrubber.get())
            self._say()
        if raw.done and not self._raw_finished:
            self._raw_finished = True
            if count == 0:
                said = raw.error or "the file holds no events"
                self._to_png()
                self._note_for(f"Raw: {said}"[:80])
                return
            if self._raw_target is not None:
                index, self._raw_target = min(self._raw_target, count - 1), None
                self._slider_to(index)
                self._show_raw(index)
            if raw.error:
                self._note_for(f"Raw stopped early: {raw.error}"[:80])
            self._say()

    def _to_png(self) -> str:
        position = self.scrubber.get()
        run = run_of_raw(self.raw_file)
        count = self.raw.count if self.raw is not None else 0
        self._stop_playing()
        self._close_raw()
        self._set_range()
        index = self._png_index_for(run, position, count)
        self._shown = None
        self._slider_to(index)
        self._show_png(index)
        return "PNG view"

    def _times(self, run: str) -> list:
        from council_core import event_playback

        if not run or not self.root:
            return []
        return event_playback.frame_times(Path(self.root) / f"{run}_frames.csv")

    def _raw_index_for(self, path: Path, shown: str) -> int:
        """The raw window showing the same moment as the PNG on screen.

        From the run's CSV, which records where each PNG fell in the .raw.
        A run without one (an older capture) opens at its start rather than
        at a guess that looks right and is not.
        """
        from council_core import event_playback

        if not shown:
            return 0
        name = os.path.basename(shown)
        for raw_t, file in self._times(run_of_raw(path)):
            if file == name:
                return event_playback.window_for(raw_t, self._raw_window)
        return 0

    def _png_index_for(self, run: str, window: int, count: int) -> int:
        """The PNG nearest raw window `window` — from the CSV, or failing
        that, the same fraction of the way through the run."""
        from council_core import event_playback

        members = [i for i, f in enumerate(self.files) if run_of(f) == run]
        name = event_playback.nearest_frame(
            self._times(run), (int(window) + 1) * self._raw_window)
        if name:
            for i in members:
                if os.path.basename(self.files[i]) == name:
                    return i
        if members:
            fraction = window / max(1, count - 1)
            return members[min(len(members) - 1,
                               int(round(fraction * (len(members) - 1))))]
        return min(int(window), max(0, len(self.files) - 1))

    def _close_raw(self) -> None:
        if self.raw is not None:
            _close(self.raw)
        self.raw = None
        self.raw_file = None
        self._raw_target = None
        if self.mode == RAW:
            self.mode = PNG

    # ==================================================================
    # Ports
    # ==================================================================
    def _set_current(self, path: str) -> None:
        """The displayed file relative to the folder, "" when it is not a file
        (live, or raw). "Mark this frame" and "Predict this frame" read it."""
        if self.current is None:
            return
        rel = ""
        if path:
            try:
                rel = os.path.relpath(path, self.root or ".")
            except ValueError:            # another drive: keep it absolute
                rel = str(path)
        try:
            self.current.set(rel)
        except Exception:                                   # noqa: BLE001
            pass

    def _note_for(self, text: str) -> str:
        self._note = str(text)
        self._note_until = time.monotonic() + NOTE_SECONDS
        self._say()
        return self._note

    def describe_shown(self) -> str:
        """What is on screen, in words — the title of a popped-out copy."""
        if self.mode == RAW and self.raw is not None:
            at = self.scrubber.get() * self.raw.window_us / 1e6
            name = self.raw_file.name if self.raw_file else "raw"
            return f"{name} at {at:.3f} s"
        if self._shown and self._shown[0] == PNG:
            return os.path.basename(self._shown[2])
        if self.live:
            return f"Live · {time.strftime('%H:%M:%S')}"
        if self._previewing:
            return f"Preview · {time.strftime('%H:%M:%S')}"
        return "Picture"

    def view_text(self) -> str:
        """The one line under "Live view": where the slider is, and what it shows."""
        if self._note and time.monotonic() < self._note_until:
            return self._note
        self._note = ""
        playing = " · playing" if self.playing else ""
        if self.mode == RAW and self.raw is not None:
            at = self.scrubber.get() * self.raw.window_us / 1e6
            total = self.raw.count * self.raw.window_us / 1e6
            if not self.raw.done:
                return f"Raw {at:.2f} s · reading, {total:.1f} s so far{playing}"
            return f"Raw {at:.2f} / {total:.2f} s{playing}"
        if self.live:
            return f"Live · {len(self.files)} saved"
        if self._previewing and self.wants_preview():
            return "Preview · not saving"
        if not self.files:
            return "No frames yet" if self.root else "Choose a folder"
        where = f"PNG {self.scrubber.get() + 1} / {len(self.files)}"
        if self._capturing:
            where += " · capturing"
        return where + playing

    def _say(self) -> None:
        if self.view is None:
            return
        try:
            self.view.set(self.view_text())
        except Exception:                                   # noqa: BLE001
            pass

    # -- the ROI box, both ways: drawn on the canvas <-> typed in the entry --
    def _roi_drawn(self, roi: Any) -> None:
        if self._roi_syncing or self.roi is None:
            return
        self._roi_syncing = True
        try:
            self.roi.set("" if roi is None else ", ".join(str(v) for v in roi))
        finally:
            self._roi_syncing = False

    def _roi_typed(self, text: Any = None) -> None:
        if self._roi_syncing:
            return
        text = str(text if text is not None else self.roi.get() or "")
        nums = re.findall("[0-9]+", text)
        setter = getattr(self.canvas, "set_roi", None)
        if not callable(setter):
            return
        self._roi_syncing = True
        try:
            if not text.strip():
                setter(None)
            elif len(nums) == 4:
                setter(tuple(int(n) for n in nums))
            # Anything else is a half-typed box: leave the current one alone.
        finally:
            self._roi_syncing = False

    def close(self) -> None:
        self._stop_playing()
        self._close_raw()


def _key(folder: Any) -> str:
    """A folder as a comparable key: same folder, same key, however spelled."""
    text = str(folder or "").strip().strip('"')
    if not text:
        return ""
    return os.path.normcase(os.path.abspath(text))


def _close(playback: Any) -> None:
    closer = getattr(playback, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:                                   # noqa: BLE001
            pass


def _default_open_raw(path: Any, window_us: int, **kw: Any) -> Any:
    from council_core import event_playback

    return event_playback.open_raw(path, window_us=window_us, **kw)
