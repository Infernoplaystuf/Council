"""
typhon.plc's "Fan blade" section: the strobe lock's settings (the fan's
speed, its blades, the view, where the hub or the side-on box is, the
tolerance), its buttons (Set camera for the fan, Find hub, Use this frame
as reference) and, during a run, what the lock is doing. Every edit is kept
in the project at once (plugin.update_fan).

Qt widgets only (QtCore, QtWidgets: design 3.11's rule for panels).
"""
from __future__ import annotations

import threading
from typing import Any, Dict

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QPushButton, QSpinBox, QWidget)

from . import strobe

INTRO = ("No sensor on the fan: the camera runs on its own clock at a rate "
         "that makes each picture whole turns apart, and the pictures "
         "themselves keep the blade in the same spot. Record from the "
         "camera's own clock (not the PLC's trigger); the PLC's timer can "
         "stay on as the backup.")
ANY_BLADE = "Any blade"
THIS_BLADE = "This blade (mark it)"


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


def _ints(parent: QWidget, *values: int) -> list:
    out = []
    for v in values:
        box = QSpinBox(parent)
        box.setRange(0, 100000)
        box.setValue(int(v))
        box.setKeyboardTracking(False)
        out.append(box)
    return out


def _row(parent: QWidget, *items: Any) -> QWidget:
    w = QWidget(parent)
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    for it in items:
        if isinstance(it, str):
            lay.addWidget(QLabel(it, w))
        else:
            lay.addWidget(it)
    lay.addStretch(1)
    return w


class FanPanel(QGroupBox):
    def __init__(self, plugin: Any, parent: Any = None) -> None:
        super().__init__("Fan blade: same spot every picture", parent)
        self.setObjectName("plc_fan")
        self.plugin = plugin
        f = plugin.fan
        form = QFormLayout(self)
        self.form = form
        intro = QLabel(INTRO, self)
        intro.setWordWrap(True)
        form.addRow(intro)
        self.enabled = QCheckBox("Hold the blade still during runs "
                                 "(strobe lock)", self)
        self.enabled.setObjectName("fan_enabled")
        self.enabled.setChecked(f.enabled)
        form.addRow("", self.enabled)
        self.rpm = _spin(self, 0.0, 100000.0, f.rpm, 2, " rpm")
        self.rpm.setToolTip("The fan's speed for this run (steady once at "
                            "speed). The lock measures the real one.")
        self.max_fps = _spin(self, 0.01, 1000.0, f.max_fps, 2, " /s")
        self.max_fps.setToolTip("The most pictures per second you want: "
                                "each picture is then whole turns apart.")
        form.addRow("Fan speed", _row(self, self.rpm, "pictures at most",
                                      self.max_fps))
        self.blades = QSpinBox(self)
        self.blades.setRange(1, 64)
        self.blades.setValue(f.blades)
        self.which = QComboBox(self)
        self.which.addItems([ANY_BLADE, THIS_BLADE])
        self.which.setCurrentText(ANY_BLADE if f.any_blade else THIS_BLADE)
        self.which.setToolTip("Any blade: whichever blade is nearest the "
                              "spot. This blade: the same one every time; "
                              "mark it (tape), alike blades look the same.")
        form.addRow("Blades", _row(self, self.blades, self.which))
        self.view = QComboBox(self)
        self.view.addItems(list(strobe.VIEWS))
        self.view.setCurrentText(f.view)
        form.addRow("Looking", self.view)
        self.hub_x, self.hub_y = (_spin(self, -100000, 100000, v, 1)
                                  for v in (f.hub_x, f.hub_y))
        self.ring_in, self.ring_out = (_spin(self, 0, 100000, v, 1)
                                       for v in (f.ring_in, f.ring_out))
        self.face_row = _row(self, "hub x", self.hub_x, "y", self.hub_y,
                             "ring", self.ring_in, "to", self.ring_out, "px")
        form.addRow("Face-on", self.face_row)
        self.box_x, self.box_y, self.box_w, self.box_h = _ints(
            self, f.box_x, f.box_y, f.box_w, f.box_h)
        self.axis = QComboBox(self)
        self.axis.addItems(list(strobe.AXES))
        self.axis.setCurrentText(f.axis)
        self.side_row = _row(self, "box x", self.box_x, "y", self.box_y,
                             "w", self.box_w, "h", self.box_h, self.axis)
        form.addRow("Side-on", self.side_row)
        self.tolerance = _spin(self, 0.01, 180.0, f.tolerance_deg, 2, "°")
        self.target = _spin(self, -180.0, 180.0, f.target_deg, 2, "°")
        self.target.setToolTip("Where to hold the blade, from the "
                               "reference picture's spot (+ clockwise).")
        form.addRow("Tolerance", _row(self, self.tolerance, "hold at",
                                      self.target, "from the reference"))
        self.info = QLabel("", self)
        self.info.setObjectName("fan_info")
        self.info.setWordWrap(True)
        form.addRow(self.info)
        self.camera_button = QPushButton("Set camera for the fan", self)
        self.camera_button.setObjectName("btn_fan_camera")
        self.camera_button.setToolTip("Before Start: trigger mode Off and "
                                      "the frame rate for whole turns.")
        self.hub_button = QPushButton("Find hub from newest pictures", self)
        self.hub_button.setObjectName("btn_fan_hub")
        self.ref_button = QPushButton("Use this frame as reference", self)
        self.ref_button.setObjectName("btn_fan_reference")
        self.ref_button.setToolTip("The frame the slider shows: the lock "
                                   "holds the blade where it is in it.")
        form.addRow(_row(self, self.camera_button, self.hub_button,
                         self.ref_button))
        self.said = QLabel("", self)
        self.said.setObjectName("fan_said")
        self.said.setWordWrap(True)
        form.addRow(self.said)

        for w in (self.rpm, self.max_fps, self.hub_x, self.hub_y,
                  self.ring_in, self.ring_out, self.tolerance, self.target,
                  self.blades, self.box_x, self.box_y, self.box_w,
                  self.box_h):
            w.valueChanged.connect(self.save)
        for w in (self.which, self.view, self.axis):
            w.currentTextChanged.connect(self.save)
        self.enabled.toggled.connect(self.save)
        self.camera_button.clicked.connect(self.set_camera)
        self.hub_button.clicked.connect(self.find_hub)
        self.ref_button.clicked.connect(self.take_reference)
        self._busy = False
        self._done = ""
        self._shown()
        self.refresh()

    def values(self) -> Dict[str, Any]:
        return {"enabled": self.enabled.isChecked(),
                "rpm": self.rpm.value(), "max_fps": self.max_fps.value(),
                "blades": self.blades.value(),
                "any_blade": self.which.currentText() == ANY_BLADE,
                "view": self.view.currentText(),
                "hub_x": self.hub_x.value(), "hub_y": self.hub_y.value(),
                "ring_in": self.ring_in.value(),
                "ring_out": self.ring_out.value(),
                "box_x": self.box_x.value(), "box_y": self.box_y.value(),
                "box_w": self.box_w.value(), "box_h": self.box_h.value(),
                "axis": self.axis.currentText(),
                "tolerance_deg": self.tolerance.value(),
                "target_deg": self.target.value()}

    def save(self, *_: Any) -> None:
        why = self.plugin.update_fan(self.values())
        if why:
            self.said.setText(f"Not kept in the project: {why}")
        self._shown()
        self.refresh()

    def _shown(self) -> None:
        face = self.view.currentText() == strobe.VIEW_FACE
        for row, shown in ((self.face_row, face), (self.side_row, not face)):
            if hasattr(self.form, "setRowVisible"):
                self.form.setRowVisible(row, shown)
            else:
                row.setVisible(shown)

    def _set_fields(self) -> None:
        f = self.plugin.fan
        for box, value in ((self.hub_x, f.hub_x), (self.hub_y, f.hub_y),
                           (self.ring_in, f.ring_in),
                           (self.ring_out, f.ring_out)):
            box.blockSignals(True)
            box.setValue(value)
            box.blockSignals(False)

    def refresh(self) -> None:
        f = self.plugin.fan
        parts = [f"{f.turns} turn(s) per picture, {f.rate:.4f} pictures/s"]
        parts.append(f"blur {strobe.blur_deg(f.rpm, 100.0):.2f}° per 100 µs "
                     f"of exposure")
        why = f.problem()
        parts.append(f"to do: {why}" if why else
                     f"reference {f.reference_name}")
        run = self.plugin.fan_run
        if run is not None:
            s = run.lock.summary()
            rpm = s.get("rpm_measured")
            parts.append(f"locking: {s['in_tolerance']} of {s['measured']} "
                         f"in tolerance"
                         + (f", fan {rpm:.2f} rpm" if rpm else ""))
        self.info.setText(" · ".join(parts))
        if self._done:
            self.said.setText(self._done)
            self._done = ""
            self._set_fields()
            for b in (self.hub_button, self.ref_button):
                b.setEnabled(True)

    # -- the buttons --------------------------------------------------------
    def set_camera(self) -> None:
        self.said.setText(self.plugin.fan_camera())

    def _off_ui(self, fn: Any, text: str) -> None:
        if self._busy:
            return
        self._busy = True
        for b in (self.hub_button, self.ref_button):
            b.setEnabled(False)
        self.said.setText(text)

        def work() -> None:
            try:
                out = fn()
            except Exception as exc:                      # noqa: BLE001
                out = f"Failed: {exc}"
            self._done = out
            self._busy = False

        threading.Thread(target=work, daemon=True, name="plc-fan").start()

    def find_hub(self) -> None:
        self._off_ui(self.plugin.fan_hub, "Finding the hub…")

    def take_reference(self) -> None:
        ctx = self.plugin.ctx
        path = ctx.current_frame() if ctx is not None else None
        self._off_ui(lambda: self.plugin.fan_reference(path),
                     "Taking the reference…")
