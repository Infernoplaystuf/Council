"""
council_qt.tabs.capture — Barbie Capture, in Qt, driving a real camera.

This replaces the wireframe of the same name. `examples/gui/barbie_capture*.
gspec` are GUI-Designer sketches that generate Tk, and what they actually
build is a FILE BROWSER: a folder picker, a scrubber over images already on
disk, and a crop box. No version of them declares a camera SDK, and none ever
touched a camera. This tab does.

TWO SENSORS THAT ARE NOT THE SAME SHAPE
A Basler boA5320-150cm delivers frames. A Prophesee EVK4 delivers events and
has no frames, no exposure and no gain. Presenting them through one panel is
fine; pretending they are the same device is not. So the exposure and gain
controls are HIDDEN for an event camera rather than shown doing nothing, the
accumulation-window control appears only for one, and the status line names
the event rate when there is one.

THE GRAB THREAD NEVER TOUCHES A WIDGET
The session's loop drops each frame into a one-slot mailbox and returns. A
33 ms timer on the UI thread takes the newest one and draws it. That is what
makes 150 fps survivable: the display consumes about thirty frames a second
and the rest are dropped ON PURPOSE — and COUNTED, and shown, because a live
view quietly showing a fifth of what it claims is the failure this design
exists to prevent.

DISCONNECTING IS NOT OPTIONAL
A grab thread outliving its window crashes on exit. `shutdown` joins it, and
it is wired to the application's aboutToQuit as well as to the button, so
quitting mid-capture is as safe as pressing Stop.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QApplication, QComboBox, QDoubleSpinBox,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QPlainTextEdit, QPushButton, QSplitter,
                               QVBoxLayout, QWidget)

from council_core import cameras, capture, paths

from .. import theme
from ..view import ViewHelpers, amp
from ..widgets.live_view import LiveView

#: How often the display pulls the newest frame. ~30 Hz.
DRAW_MS = 33


class CaptureTab(ViewHelpers, QWidget):
    """Scan, connect, live view, AOI, record."""

    def __init__(self, window=None, backends=None, ask_dir=None,
                 auto_scan: bool = True):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self._tokens = theme.tokens("dark")
        #: Injectable so a test can supply fake SDKs.
        self.backends = backends
        #: Injectable so a test never opens a dialog.
        self.ask_dir = ask_dir or (lambda *a, **k: None)

        self._found = cameras.Discovery()
        self.device: Optional[cameras.Device] = None
        self.session: Optional[capture.CaptureSession] = None
        self._busy = False

        self._build()
        self._timer = QTimer(self)
        self._timer.setInterval(DRAW_MS)
        self._timer.timeout.connect(self._draw)

        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

        self._sync()
        if auto_scan:
            self.scan()

    # ==================================================================
    # Building
    # ==================================================================
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self._left())
        split.addWidget(self._right())
        split.setSizes([360, 820])
        outer.addWidget(split, 1)

    def _left(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        row = QHBoxLayout()
        self.scan_btn = self._button(row, amp("⟳ Scan"), self.scan)
        self.connect_btn = self._button(row, amp("Connect"), self.on_connect)
        self.disconnect_btn = self._button(row, amp("Disconnect"),
                                           self.on_disconnect)
        row.addStretch(1)
        layout.addLayout(row)

        box = QGroupBox("Cameras")
        box_layout = QVBoxLayout(box)
        self.cameras_list = QListWidget()
        self.cameras_list.currentRowChanged.connect(lambda _r: self._sync())
        box_layout.addWidget(self.cameras_list)
        layout.addWidget(box, 1)

        # Why a backend was skipped. An empty list with no explanation sends
        # the user to check cables when the answer is an uninstalled SDK.
        notes_box = QGroupBox("What was not searched")
        notes_layout = QVBoxLayout(notes_box)
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setMaximumHeight(130)
        notes_layout.addWidget(self.notes)
        layout.addWidget(notes_box)
        return panel

    def _right(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        self.view = LiveView()
        self.view.roi_drawn.connect(self.apply_roi)
        layout.addWidget(self.view, 1)

        self.status = QLabel("Not connected.")
        layout.addWidget(self.status)

        row = QHBoxLayout()
        self.start_btn = self._button(row, amp("▶ Start"), self.on_start)
        self.stop_btn = self._button(row, amp("■ Stop"), self.on_stop)
        self.record_btn = self._button(row, amp("● Record…"), self.on_record)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addWidget(self._settings())
        return panel

    def _settings(self) -> QWidget:
        box = QGroupBox("Camera")
        form = QFormLayout(box)

        self.exposure = QDoubleSpinBox()
        self.exposure.setRange(1.0, 1000000.0)
        self.exposure.setValue(5000.0)
        self.exposure.setSuffix(" µs")
        self.exposure.editingFinished.connect(self.apply_exposure)
        self.exposure_label = QLabel("Exposure")
        form.addRow(self.exposure_label, self.exposure)

        self.gain = QDoubleSpinBox()
        self.gain.setRange(0.0, 100.0)
        self.gain.editingFinished.connect(self.apply_gain)
        self.gain_label = QLabel("Gain")
        form.addRow(self.gain_label, self.gain)

        # Event cameras only. Not an exposure: a longer window collects more
        # events, not more light.
        self.window_ms = QDoubleSpinBox()
        self.window_ms.setRange(1.0, 1000.0)
        self.window_ms.setValue(cameras.DEFAULT_ACCUMULATE_MS)
        self.window_ms.setSuffix(" ms")
        self.window_ms.valueChanged.connect(self.apply_window)
        self.window_label = QLabel("Accumulate")
        form.addRow(self.window_label, self.window_ms)

        roi_row = QHBoxLayout()
        self.roi_edit = QLineEdit()
        self.roi_edit.setPlaceholderText("x, y, w, h — or drag a box on the view")
        roi_row.addWidget(self.roi_edit, 1)
        self.roi_btn = QPushButton(amp("Apply area"))
        self.roi_btn.clicked.connect(self.on_roi_typed)
        roi_row.addWidget(self.roi_btn)
        self.full_btn = QPushButton(amp("Full sensor"))
        self.full_btn.clicked.connect(self.on_full_frame)
        roi_row.addWidget(self.full_btn)
        holder = QWidget()
        holder.setLayout(roi_row)
        form.addRow(QLabel("Area of interest"), holder)
        return box

    # ==================================================================
    # Scanning and connecting
    # ==================================================================
    def scan(self) -> None:
        """Look for cameras. Enumeration can block, so it runs off the UI."""
        if not self._begin("Scanning…"):
            return

        def work() -> None:
            try:
                found = cameras.discover(self.backends)
            except Exception as exc:                        # noqa: BLE001
                said = repr(exc)
                self._to_ui(lambda: self._scanned(cameras.Discovery(
                    notes=[f"the scan failed: {said}"])))
                return
            self._to_ui(lambda: self._scanned(found))

        threading.Thread(target=work, name="camera-scan", daemon=True).start()

    def _scanned(self, found: cameras.Discovery) -> None:
        self._found = found
        self.cameras_list.clear()
        for info in found.cameras:
            item = QListWidgetItem(f"{info.label}  ·  {info.kind}")
            item.setData(Qt.ItemDataRole.UserRole, info.key)
            self.cameras_list.addItem(item)
        self.notes.setPlainText("\n".join(found.notes)
                                or "Every backend was searched.")
        self._end(f"{len(found.cameras)} camera(s) found."
                  if found.cameras else "No cameras found.")

    def selected(self) -> Optional[cameras.CameraInfo]:
        """The chosen camera, resolved BY INDEX.

        Not by parsing the label. Recovering an identifier from display text
        is the defect this codebase keeps finding, and a label here carries a
        separator and a kind appended to it.
        """
        row = self.cameras_list.currentRow()
        if row < 0 or row >= len(self._found.cameras):
            return None
        return self._found.cameras[row]

    def on_connect(self) -> None:
        info = self.selected()
        if info is None:
            self.status.setText("Choose a camera first.")
            return
        if not self._begin(f"Opening {info.label}…"):
            return

        def work() -> None:
            try:
                device = cameras.open_camera(info, self.backends)
            except Exception as exc:                        # noqa: BLE001
                # `exc` is unbound once the except block ends; bind it now or
                # the deferred lambda raises NameError on the error path.
                said = str(exc)
                self._to_ui(lambda: self._end(f"Could not open it: {said}"))
                return
            self._to_ui(lambda: self._opened(device))

        threading.Thread(target=work, name="camera-open", daemon=True).start()

    def _opened(self, device: cameras.Device) -> None:
        self.device = device
        self.session = capture.CaptureSession(device)
        limits = device.limits()
        roi = device.roi()
        self.view.origin = (roi.x, roi.y)
        self.roi_edit.setText(f"{roi.x}, {roi.y}, {roi.w}, {roi.h}")
        self._apply_limits(limits)
        self._end(f"Connected to {device.info.label}. "
                  f"Sensor {limits.width}x{limits.height}.")

    def _apply_limits(self, limits: cameras.Limits) -> None:
        """Show only the controls this sensor actually has.

        An event camera has no exposure and no gain. A disabled box that looks
        like a setting is worse than no box: it invites the user to wonder
        what they did wrong.
        """
        has_exposure = limits.exposure_us[1] > 0
        for widget in (self.exposure, self.exposure_label):
            widget.setVisible(has_exposure)
        if has_exposure:
            self.exposure.setRange(max(1.0, limits.exposure_us[0]),
                                   limits.exposure_us[1])
        has_gain = limits.gain[1] > 0
        for widget in (self.gain, self.gain_label):
            widget.setVisible(has_gain)
        if has_gain:
            self.gain.setRange(limits.gain[0], limits.gain[1])

        is_event = self.device is not None and self.device.info.kind == "event"
        for widget in (self.window_ms, self.window_label):
            widget.setVisible(is_event)

    def on_disconnect(self) -> None:
        self.shutdown()
        self.view.clear()
        self._end("Disconnected.")

    # ==================================================================
    # Running
    # ==================================================================
    def on_start(self) -> None:
        if self.session is None:
            return
        try:
            self.session.start()
        except Exception as exc:                            # noqa: BLE001
            self.status.setText(f"Could not start: {exc}")
            self._sync()
            return
        self._timer.start()
        self._sync()

    def on_stop(self) -> None:
        self._timer.stop()
        if self.session is not None and not self.session.stop():
            # The truth, not a hopeful message: something is still holding on.
            self.status.setText("The camera did not stop cleanly.")
        self._sync()

    def on_record(self) -> None:
        """Start or stop writing frames to disk."""
        if self.session is None:
            return
        if self.session.recorder is not None:
            self.session.record_to(None)
            self._sync()
            return
        where = self.ask_dir("Save frames to", str(paths.vault_dir()))
        if not where:
            return
        try:
            self.session.record_to(capture.Recorder(Path(where)))
        except OSError as exc:
            self.status.setText(f"Could not record there: {exc}")
            return
        self._sync()

    def _draw(self) -> None:
        """Take the newest frame and show it. UI thread, every 33 ms."""
        if self.session is None:
            return
        frame = self.session.mailbox.take()
        if frame is not None:
            self.view.show_frame(frame)
        stats = self.session.stats()
        self.status.setText(self._status_line(stats, frame))
        if stats.recording_failed:
            self._sync()

    def _status_line(self, stats: capture.Stats,
                     frame: Optional[cameras.Frame]) -> str:
        line = stats.line()
        meta = getattr(frame, "meta", None) or {}
        if meta.get("kind") == "event":
            rate = meta.get("event_rate_hz") or 0.0
            # Named for what it is. An event camera has no frame rate; what
            # the number above counts is accumulation windows.
            line += f" · {meta.get('events', 0)} events/window"
            if rate:
                line += f" · {rate / 1000.0:.1f} kev/s"
        if stats.last_error:
            line += f" · {stats.last_error}"
        return line

    # ==================================================================
    # Settings
    # ==================================================================
    def apply_roi(self, roi: cameras.Roi) -> None:
        """Set the camera's AOI — on the sensor, not as a crop."""
        if self.device is None:
            return
        was_running = self.session is not None and self.session.running
        if was_running:
            self.on_stop()
        try:
            got = self.device.set_roi(roi)
        except cameras.CameraError as exc:
            self.status.setText(str(exc))
            return
        finally:
            if was_running:
                self.on_start()
        self.view.origin = (got.x, got.y)
        self.roi_edit.setText(f"{got.x}, {got.y}, {got.w}, {got.h}")
        if got.as_tuple() != roi.as_tuple():
            # Say so. The camera snapped it to its increments, and a box that
            # silently moves is a box the user will fight with.
            self.status.setText(
                f"Area set to {got.x}, {got.y}, {got.w}, {got.h} — "
                f"snapped to what the sensor accepts.")

    def on_roi_typed(self) -> None:
        parts = [p.strip() for p in self.roi_edit.text().replace(";", ",")
                 .split(",") if p.strip()]
        try:
            x, y, w, h = (int(float(p)) for p in parts)
        except (TypeError, ValueError):
            self.status.setText("Type the area as x, y, w, h.")
            return
        if min(x, y, w, h) < 0:
            self.status.setText("An area cannot have negative numbers.")
            return
        self.apply_roi(cameras.Roi(x, y, w, h))

    def on_full_frame(self) -> None:
        if self.device is None:
            return
        limits = self.device.limits()
        self.apply_roi(cameras.Roi(0, 0, limits.width, limits.height))

    def apply_exposure(self) -> None:
        if self.device is None:
            return
        try:
            got = self.device.set_exposure_us(self.exposure.value())
        except cameras.CameraError as exc:
            self.status.setText(str(exc))
            return
        if got:
            self.exposure.setValue(got)

    def apply_gain(self) -> None:
        if self.device is None:
            return
        try:
            got = self.device.set_gain(self.gain.value())
        except cameras.CameraError as exc:
            self.status.setText(str(exc))
            return
        self.gain.setValue(got)

    def apply_window(self, value: float) -> None:
        if self.device is not None:
            setattr(self.device, "accumulate_ms", float(value))

    # ==================================================================
    # Lifecycle
    # ==================================================================
    def shutdown(self) -> None:
        """Stop the grab thread and release the camera. Safe to call twice."""
        self._timer.stop()
        session, self.session = self.session, None
        if session is not None:
            session.close()
        self.device = None
        self._sync()

    # ==================================================================
    # Button state
    # ==================================================================
    def _begin(self, message: str) -> bool:
        if self._busy:
            return False
        self._busy = True
        self.status.setText(message)
        self._sync()
        return True

    def _end(self, message: str) -> None:
        self._busy = False
        self.status.setText(message)
        self._sync()

    def _sync(self) -> None:
        """One place that decides what is clickable."""
        connected = self.device is not None
        running = self.session is not None and self.session.running
        recording = (self.session is not None
                     and self.session.recorder is not None)
        idle = not self._busy

        self.scan_btn.setEnabled(idle and not connected)
        self.connect_btn.setEnabled(idle and not connected
                                    and self.selected() is not None)
        self.disconnect_btn.setEnabled(idle and connected)
        self.start_btn.setEnabled(idle and connected and not running)
        self.stop_btn.setEnabled(running)
        self.record_btn.setEnabled(running)
        self.record_btn.setText(amp("■ Stop recording") if recording
                                else amp("● Record…"))
        for widget in (self.roi_btn, self.full_btn, self.exposure, self.gain,
                       self.window_ms, self.roi_edit):
            widget.setEnabled(idle and connected)


def build_capture(window) -> QWidget:
    """Factory for the tab registry."""
    return CaptureTab(window)
