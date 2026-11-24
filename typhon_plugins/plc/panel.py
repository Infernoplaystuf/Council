"""
typhon.plc's window: the PLC's address, the mode, the registers, the belt,
the live readout (count, speed, the delay they make, the delay the camera
has) and the buttons — Test connection, Apply delay to camera, Calibrate
from the newest pictures. Every edit is kept in the project at once.

The readout is refreshed twice a second from the plugin's monitor (which
reads the PLC on its own thread); nothing here waits on the network
except Test connection, which is bounded by the client's timeout.

Qt widgets only (QtCore, QtWidgets: design 3.11's rule for panels).
"""
from __future__ import annotations

import threading
from typing import Any, Dict

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QScrollArea, QSpinBox,
                               QVBoxLayout, QWidget)

from . import timing
from .fan_panel import FanPanel

REFRESH_MS = 500
INTRO = ("A Do-More PLC says when a bottle passes its photo-eye; Typhon "
         "times the picture so the bottle is in the middle of the view. "
         "The Do-More Designer set-up is in docs/typhon_plc_trigger.md.")
WIRE_HINT = ("Wire: the PLC's output goes to the camera's trigger input. "
             "In Triggering pick Triggered: exact and that line (Line1, "
             "CxpTrigger0 …); this window sets the camera's trigger delay.")
NET_HINT = ("Network: in Triggering pick Trigger from \"PLC (network)\". "
            "Typhon reads the bottle count and fires each bottle itself "
            "(a few ms of spread).")


def _spin(parent: QWidget, lo: float, hi: float, value: float,
          decimals: int = 1, suffix: str = "") -> QDoubleSpinBox:
    box = QDoubleSpinBox(parent)
    box.setRange(lo, hi)
    box.setDecimals(decimals)
    box.setValue(value)
    if suffix:
        box.setSuffix(suffix)
    box.setKeyboardTracking(False)
    return box


def _show_row(form: QFormLayout, field: QWidget, shown: bool) -> None:
    """Show or hide a form row (setRowVisible from Qt 6.4; before it, the
    field and its label)."""
    if hasattr(form, "setRowVisible"):
        form.setRowVisible(field, shown)
        return
    field.setVisible(shown)
    label = form.labelForField(field)
    if label is not None:
        label.setVisible(shown)


class PlcPanel(QWidget):
    def __init__(self, plugin: Any, parent: Any = None) -> None:
        super().__init__(parent)
        self.setObjectName("plc_panel")
        self.plugin = plugin
        s = plugin.settings
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        outer.addWidget(scroll)
        inner = QWidget(scroll)
        scroll.setWidget(inner)
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(12, 12, 12, 12)
        intro = QLabel(INTRO, self)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # -- the PLC --------------------------------------------------------
        plc = QGroupBox("PLC", self)
        form = QFormLayout(plc)
        self.plc_form = form
        self.link = QComboBox(plc)
        self.link.addItems(list(timing.LINKS))
        self.link.setCurrentText(s.link)
        form.addRow("Connection", self.link)
        self.host = QLineEdit(s.host, plc)
        self.host.setPlaceholderText("192.168.1.10")
        self.port = QSpinBox(plc)
        self.port.setRange(1, 65535)
        self.port.setValue(s.port)
        row = QHBoxLayout()
        row.addWidget(self.host, 1)
        row.addWidget(QLabel("port", plc))
        row.addWidget(self.port)
        self.address_row = QWidget(plc)
        self.address_row.setLayout(row)
        row.setContentsMargins(0, 0, 0, 0)
        form.addRow("Address", self.address_row)
        # the serial port (Modbus RTU)
        self.serial_port = QComboBox(plc)
        self.serial_port.setEditable(True)
        self.serial_port.addItems(plugin.serial_ports())
        self.serial_port.setCurrentText(s.serial_port)
        self.serial_port.setToolTip("The PC's serial port wired to the "
                                    "PLC's (COM1, COM3 …; a USB adapter "
                                    "shows up as one).")
        self.baud = QComboBox(plc)
        self.baud.addItems([str(b) for b in timing.BAUDS])
        self.baud.setEditable(True)
        self.baud.setCurrentText(str(s.baud))
        self.parity = QComboBox(plc)
        self.parity.addItems(list(timing.PARITIES))
        self.parity.setCurrentText(s.parity)
        self.stop_bits = QComboBox(plc)
        self.stop_bits.addItems(["1", "2"])
        self.stop_bits.setCurrentText(str(s.stop_bits))
        srow = QHBoxLayout()
        srow.setContentsMargins(0, 0, 0, 0)
        srow.addWidget(self.serial_port, 1)
        srow.addWidget(QLabel("baud", plc))
        srow.addWidget(self.baud)
        self.serial_row = QWidget(plc)
        self.serial_row.setLayout(srow)
        form.addRow("Serial port", self.serial_row)
        prow = QHBoxLayout()
        prow.setContentsMargins(0, 0, 0, 0)
        prow.addWidget(QLabel("parity", plc))
        prow.addWidget(self.parity)
        prow.addWidget(QLabel("stop bits", plc))
        prow.addWidget(self.stop_bits)
        prow.addWidget(QLabel("station", plc))
        self.unit = QSpinBox(plc)
        self.unit.setRange(0, 247)
        self.unit.setValue(s.unit)
        prow.addWidget(self.unit)
        prow.addStretch(1)
        self.framing_row = QWidget(plc)
        self.framing_row.setLayout(prow)
        form.addRow("", self.framing_row)
        self.simulated = QCheckBox("Use a simulated PLC (no hardware)", plc)
        self.simulated.setChecked(s.simulated)
        form.addRow("", self.simulated)
        self.mode = QComboBox(plc)
        self.mode.addItems(list(timing.MODES))
        self.mode.setCurrentText(s.mode)
        form.addRow("Mode", self.mode)
        self.hint = QLabel("", plc)
        self.hint.setWordWrap(True)
        form.addRow(self.hint)
        self.count_register = QLineEdit(s.count_register, plc)
        self.count_register.setToolTip("The register the ladder adds 1 to "
                                       "per bottle (MHR1 = 40001).")
        form.addRow("Bottle count", self.count_register)
        self.fire_register = QLineEdit(s.fire_register, plc)
        self.fire_register.setToolTip("Fire camera via PLC writes 1 here; "
                                      "the ladder pulses the camera's "
                                      "output and writes 0 back.")
        form.addRow("Fire request", self.fire_register)
        self.timer_register = QLineEdit(s.timer_register, plc)
        self.timer_register.setToolTip("0: the PLC's timer triggers the "
                                       "camera; 1: the timer is off and "
                                       "the computer triggers it.")
        self.timer_on = QPushButton("PLC timer on", plc)
        self.timer_on.setObjectName("btn_plc_timer_on")
        self.timer_off = QPushButton("PLC timer off (computer)", plc)
        self.timer_off.setObjectName("btn_plc_timer_off")
        trow = QHBoxLayout()
        trow.setContentsMargins(0, 0, 0, 0)
        trow.addWidget(self.timer_register, 1)
        trow.addWidget(self.timer_on)
        trow.addWidget(self.timer_off)
        form.addRow("Timer switch", trow)
        layout.addWidget(plc)

        # -- the belt -------------------------------------------------------
        belt = QGroupBox("Belt and timing", self)
        form = QFormLayout(belt)
        self.speed_source = QComboBox(belt)
        self.speed_source.addItems(list(timing.SPEEDS))
        self.speed_source.setCurrentText(s.speed_source)
        form.addRow("Belt speed", self.speed_source)
        self.speed = _spin(belt, 0.0, 100000.0, s.speed_mm_s, 1, " mm/s")
        form.addRow("Fixed speed", self.speed)
        self.speed_register = QLineEdit(s.speed_register, belt)
        self.speed_scale = _spin(belt, 0.001, 10000.0, s.speed_scale, 3)
        self.speed_scale.setToolTip("Register units per mm/s: 10 when the "
                                    "PLC keeps tenths of a mm/s.")
        row = QHBoxLayout()
        row.addWidget(self.speed_register, 1)
        row.addWidget(QLabel("units per mm/s", belt))
        row.addWidget(self.speed_scale)
        form.addRow("Speed register", row)
        self.distance = _spin(belt, 0.0, 100000.0, s.distance_mm, 1, " mm")
        self.distance.setToolTip("From the photo-eye to the middle of the "
                                 "camera's view, along the belt.")
        form.addRow("Photo-eye to view", self.distance)
        self.latency = _spin(belt, 0.0, 10000.0, s.fixed_latency_ms, 3,
                             " ms")
        self.latency.setToolTip("PLC scan + output switching + the camera's "
                                "own trigger latency (and, in network mode, "
                                "anything else fixed).")
        form.addRow("Fixed latency", self.latency)
        self.offset = _spin(belt, -10000.0, 10000.0, s.offset_ms, 3, " ms")
        self.offset.setToolTip("Calibration's correction (+ fires later).")
        form.addRow("Calibration offset", self.offset)
        self.poll = _spin(belt, 0.5, 100.0, s.poll_ms, 1, " ms")
        form.addRow("Poll every (network)", self.poll)
        self.follow = QCheckBox("Keep the camera's delay matched to the "
                                "speed (between runs)", belt)
        self.follow.setChecked(s.follow_speed)
        form.addRow("", self.follow)
        layout.addWidget(belt)

        # -- calibration ----------------------------------------------------
        cal = QGroupBox("Auto-calibrate", self)
        form = QFormLayout(cal)
        self.view_mm = _spin(cal, 0.0, 100000.0, s.view_mm, 1, " mm")
        self.view_mm.setToolTip("How many mm of belt the picture spans, "
                                "along the belt.")
        form.addRow("View along belt", self.view_mm)
        self.direction = QComboBox(cal)
        self.direction.addItems(list(timing.DIRECTIONS))
        self.direction.setCurrentText(s.direction)
        form.addRow("Bottles move", self.direction)
        self.frames = QSpinBox(cal)
        self.frames.setRange(1, 200)
        self.frames.setValue(s.calibrate_frames)
        form.addRow("Pictures to use", self.frames)
        layout.addWidget(cal)

        # -- readout and buttons --------------------------------------------
        self.readout = QLabel("", self)
        self.readout.setObjectName("plc_readout")
        self.readout.setWordWrap(True)
        layout.addWidget(self.readout)
        row = QHBoxLayout()
        self.test_button = QPushButton("Test connection", self)
        self.apply_button = QPushButton("Apply delay to camera", self)
        self.calibrate_button = QPushButton("Calibrate from newest pictures",
                                            self)
        for b in (self.test_button, self.apply_button,
                  self.calibrate_button):
            row.addWidget(b)
        layout.addLayout(row)
        self.fire_button = QPushButton("Fire camera via PLC", self)
        self.fire_button.setObjectName("btn_plc_fire")
        self.fire_button.setToolTip("The PLC pulses the camera's trigger "
                                    "output once — works during a run "
                                    "triggered from the PLC's wire.")
        self.fire_button.clicked.connect(self.fire)
        layout.addWidget(self.fire_button)
        self.fan_panel = FanPanel(plugin, inner)
        layout.addWidget(self.fan_panel)
        self.said = QLabel("", self)
        self.said.setObjectName("plc_said")
        self.said.setWordWrap(True)
        layout.addWidget(self.said)
        layout.addStretch(1)

        for w in (self.host, self.count_register, self.speed_register,
                  self.fire_register, self.timer_register):
            w.editingFinished.connect(self.save)
        self.serial_port.lineEdit().editingFinished.connect(self.save)
        self.serial_port.activated.connect(self.save)
        self.baud.lineEdit().editingFinished.connect(self.save)
        self.baud.activated.connect(self.save)
        for w in (self.link, self.parity, self.stop_bits):
            w.currentTextChanged.connect(self.save)
        for w in (self.port, self.frames, self.unit):
            w.valueChanged.connect(self.save)
        for w in (self.speed, self.speed_scale, self.distance, self.latency,
                  self.offset, self.poll, self.view_mm):
            w.valueChanged.connect(self.save)
        for w in (self.mode, self.speed_source, self.direction):
            w.currentTextChanged.connect(self.save)
        for w in (self.simulated, self.follow):
            w.toggled.connect(self.save)
        self.test_button.clicked.connect(self.test)
        self.apply_button.clicked.connect(self.apply)
        self.calibrate_button.clicked.connect(self.calibrate)
        self.timer_on.clicked.connect(lambda: self.set_timer(True))
        self.timer_off.clicked.connect(lambda: self.set_timer(False))

        self._calibrating = False
        self._calibrated = ""
        self.timer = QTimer(self)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(self.refresh)
        self._shown()
        self.refresh()

    # -- keeping the settings ---------------------------------------------
    def values(self) -> Dict[str, Any]:
        try:
            baud = int(self.baud.currentText())
        except ValueError:
            baud = self.plugin.settings.baud
        return {"link": self.link.currentText(),
                "serial_port": self.serial_port.currentText().strip(),
                "baud": baud, "parity": self.parity.currentText(),
                "stop_bits": int(self.stop_bits.currentText()),
                "unit": self.unit.value(),
                "host": self.host.text().strip(), "port": self.port.value(),
                "simulated": self.simulated.isChecked(),
                "mode": self.mode.currentText(),
                "count_register": self.count_register.text().strip(),
                "fire_register": self.fire_register.text().strip(),
                "timer_register": self.timer_register.text().strip(),
                "speed_source": self.speed_source.currentText(),
                "speed_mm_s": self.speed.value(),
                "speed_register": self.speed_register.text().strip(),
                "speed_scale": self.speed_scale.value(),
                "distance_mm": self.distance.value(),
                "fixed_latency_ms": self.latency.value(),
                "offset_ms": self.offset.value(),
                "poll_ms": self.poll.value(),
                "follow_speed": self.follow.isChecked(),
                "view_mm": self.view_mm.value(),
                "direction": self.direction.currentText(),
                "calibrate_frames": self.frames.value()}

    def save(self, *_: Any) -> None:
        why = self.plugin.update_settings(self.values())
        if why:
            self.said.setText(f"Not kept in the project: {why}")
        self._shown()
        self.refresh()

    def _shown(self) -> None:
        wire = self.mode.currentText() == timing.MODE_WIRE
        self.hint.setText(WIRE_HINT if wire else NET_HINT)
        self.apply_button.setEnabled(wire)
        self.follow.setEnabled(wire)
        self.poll.setEnabled(not wire)
        from_plc = self.speed_source.currentText() == timing.SPEED_PLC
        self.speed.setEnabled(not from_plc or self.simulated.isChecked())
        self.speed_register.setEnabled(from_plc)
        self.speed_scale.setEnabled(from_plc)
        sim = self.simulated.isChecked()
        serial = self.link.currentText() == timing.LINK_SERIAL
        self.host.setEnabled(not sim)
        self.port.setEnabled(not sim)
        self.serial_port.setEnabled(not sim)
        for row, shown in ((self.address_row, not serial),
                           (self.serial_row, serial),
                           (self.framing_row, serial)):
            _show_row(self.plc_form, row, shown)

    # -- the readout --------------------------------------------------------
    def refresh(self) -> None:
        self.fan_panel.refresh()
        r = self.plugin.reading
        if not self.plugin._set_up():
            self.readout.setText("Set the PLC's address or serial port (or "
                                 "tick the simulated PLC).")
            return
        parts = []
        if r.get("connected"):
            parts.append(f"Connected · bottle count {r.get('count')}")
            if r.get("rtt_ms") is not None:
                parts.append(f"reply {r['rtt_ms']} ms")
        else:
            parts.append(f"Not connected: {r.get('error') or 'waiting'}")
        speed = r.get("speed")
        parts.append("speed ?" if speed is None else
                     f"speed {speed:.1f} mm/s")
        delay = r.get("delay_us")
        parts.append("delay ?" if delay is None else
                     f"delay needed {delay / 1000.0:.3f} ms")
        timer = r.get("timer")
        if timer is not None:
            parts.append("PLC timer on" if timer else
                         "PLC timer off (computer)")
        applied = self.plugin.applied_us()
        if self.mode.currentText() == timing.MODE_WIRE:
            parts.append("camera has " + ("—" if applied is None else
                                          f"{applied / 1000.0:.3f} ms"))
        self.readout.setText(" · ".join(parts))
        if self._calibrated:
            self.said.setText(self._calibrated)
            self._calibrated = ""
            self.offset.blockSignals(True)
            self.offset.setValue(self.plugin.settings.offset_ms)
            self.offset.blockSignals(False)
            self.calibrate_button.setEnabled(True)

    def showEvent(self, event: Any) -> None:              # noqa: N802
        super().showEvent(event)
        self.refresh()
        self.timer.start()

    def hideEvent(self, event: Any) -> None:              # noqa: N802
        super().hideEvent(event)
        self.timer.stop()

    # -- the buttons --------------------------------------------------------
    def test(self) -> None:
        r = self.plugin.read_now()
        if r.get("connected"):
            self.said.setText(f"The PLC answered: bottle count "
                              f"{r.get('count')}, speed {r.get('speed')} "
                              f"mm/s, reply {r.get('rtt_ms')} ms.")
        else:
            self.said.setText(f"No answer: {r.get('error')}")
        self.refresh()

    def set_timer(self, on: bool) -> None:
        self.said.setText(self.plugin.set_timer(on))
        self.refresh()

    def fire(self) -> None:
        self.said.setText(self.plugin.fire_via_plc())

    def apply(self) -> None:
        self.said.setText(self.plugin.apply_delay())
        self.refresh()

    def calibrate(self) -> None:
        if self._calibrating:
            return
        self._calibrating = True
        self.calibrate_button.setEnabled(False)
        self.said.setText("Calibrating from the newest pictures…")

        def work() -> None:
            try:
                text = self.plugin.calibrate()
            except Exception as exc:                      # noqa: BLE001
                text = f"Calibration failed: {exc}"
            self._calibrated = text
            self._calibrating = False

        threading.Thread(target=work, daemon=True,
                         name="plc-calibrate").start()
