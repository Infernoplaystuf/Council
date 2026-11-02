"""
live_dashboard.py — a live machine dashboard built from custom-painted
widgets: gauges, sparklines, status lights and number cards.

WHAT IT DOES
Four simulated sensors (temperature, pressure, speed, vibration) update a
few times a second. Each has a card with its value, a gauge whose needle
glides to the new reading, and a sparkline of the last minute. A light goes
amber and then red as a reading passes its warning and alarm limits, and
every crossing is added to the alarm list. Pause stops the feed; the rate
slider changes how often it updates; the theme button switches light and
dark; double-click a card to set its limits.

SHOWS
  * custom widgets drawn in paintEvent with QPainter: arcs, a rotated
    needle (translate / rotate / save / restore), text in rects, a
    QPainterPath sparkline with a gradient fill (QLinearGradient)
  * a Qt Property (PySide6 Property) animated by QPropertyAnimation with an
    easing curve, so values glide instead of jump
  * sizeHint / minimumSizeHint and update() for widgets that repaint
  * QTimer driving a data feed; a blinking light from a second QTimer
  * QFrame cards styled with a Qt style sheet (objectName selectors), and
    swapping the whole sheet for a light / dark theme
  * a reusable composite widget (SensorCard) emitting its own Signal
  * hysteresis on alarm limits, so a value at the limit does not flicker
  * QGridLayout that re-flows the cards on resize
  * QInputDialog.getDouble for a quick edit, QListWidget as an event log
  * collections.deque as a fixed-size rolling buffer

RUN
    python examples/qt_apps/live_dashboard.py

REQUIRES  nothing beyond PySide6
"""
from __future__ import annotations

import math
import random
import sys
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional

from PySide6.QtCore import (Property, QEasingCurve, QPointF, QPropertyAnimation,
                            QRectF, QSize, Qt, QTime, QTimer, Signal)
from PySide6.QtGui import (QColor, QFont, QLinearGradient, QPainter,
                           QPainterPath, QPen)
from PySide6.QtWidgets import (QApplication, QFrame, QGridLayout,
                               QHBoxLayout, QInputDialog, QLabel,
                               QListWidget, QMainWindow, QPushButton,
                               QSlider, QSplitter, QVBoxLayout, QWidget)

HISTORY = 240
OK, WARN, ALARM = "ok", "warn", "alarm"
LIGHT_COLOURS = {OK: "#22aa55", WARN: "#e6a100", ALARM: "#dd3333"}

#: Only the window and the cards get a background; everything inside a card
#: is transparent, or every label paints its own box.
THEME = """
QMainWindow {{ background: {window}; }}
QWidget {{ color: {text}; }}
QFrame#card {{ background: {card}; border: 1px solid {border};
              border-radius: 10px; }}
QFrame#card QWidget {{ background: transparent; border: none; }}
QLabel#value {{ font-size: 22pt; font-weight: 600; }}
QLabel#title {{ color: {muted}; }}
QListWidget, QPushButton {{ background: {card}; border: 1px solid {border};
                           border-radius: 4px; padding: 3px 8px; }}
"""
LIGHT_THEME = THEME.format(window="#f4f6f9", text="#1e2430", card="white",
                           border="#d8dde6", muted="#5a6578")
DARK_THEME = THEME.format(window="#171a21", text="#e3e7ef", card="#222731",
                          border="#333a47", muted="#9aa4b5")
#: A reading must fall this fraction of the scale below a limit before the
#: state drops back, or a value hovering at the limit floods the alarm list.
HYSTERESIS = 0.05


@dataclass
class Sensor:
    name: str
    unit: str
    low: float
    high: float
    warn: float
    alarm: float
    base: float
    swing: float


SENSORS = [Sensor("Temperature", "°C", 0, 120, 80, 95, 65, 18),
           Sensor("Pressure", "bar", 0, 10, 7.5, 8.8, 5, 2.2),
           Sensor("Spindle speed", "rpm", 0, 6000, 5200, 5700, 4200, 900),
           Sensor("Vibration", "mm/s", 0, 20, 9, 14, 5, 4)]


class Gauge(QWidget):
    """A 270° dial. ``value`` is a Qt Property so it can be animated."""

    def __init__(self, low: float, high: float, warn: float, alarm: float):
        super().__init__()
        self.low, self.high, self.warn, self.alarm = low, high, warn, alarm
        self._value = low
        self.anim = QPropertyAnimation(self, b"value", self)
        self.anim.setDuration(350)
        self.anim.setEasingCurve(QEasingCurve.OutCubic)

    def get_value(self) -> float:
        return self._value

    def set_value(self, v: float) -> None:
        self._value = v
        self.update()

    value = Property(float, get_value, set_value)

    def glide_to(self, v: float) -> None:
        self.anim.stop()
        self.anim.setStartValue(self._value)
        self.anim.setEndValue(float(v))
        self.anim.start()

    def sizeHint(self) -> QSize:
        return QSize(150, 150)

    def minimumSizeHint(self) -> QSize:
        return QSize(90, 90)

    def _angle(self, v: float) -> float:
        """Qt angles: 0 is 3 o'clock, counter-clockwise. Dial spans 225..-45."""
        t = (min(max(v, self.low), self.high) - self.low) / \
            ((self.high - self.low) or 1)
        return 225 - 270 * t

    def paintEvent(self, event) -> None:
        side = min(self.width(), self.height()) - 10
        rect = QRectF((self.width() - side) / 2, (self.height() - side) / 2,
                      side, side)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        text = self.palette().windowText().color()

        # Bands: normal, warning, alarm.
        pen_w = max(4.0, side / 14)
        for start, end, colour in ((self.low, self.warn, LIGHT_COLOURS[OK]),
                                   (self.warn, self.alarm,
                                    LIGHT_COLOURS[WARN]),
                                   (self.alarm, self.high,
                                    LIGHT_COLOURS[ALARM])):
            a0, a1 = self._angle(start), self._angle(end)
            p.setPen(QPen(QColor(colour), pen_w, Qt.SolidLine, Qt.FlatCap))
            inner = rect.adjusted(pen_w, pen_w, -pen_w, -pen_w)
            p.drawArc(inner, int(a1 * 16), int((a0 - a1) * 16))

        # Needle, drawn pointing right and rotated into place.
        p.save()
        p.translate(rect.center())
        p.rotate(-self._angle(self._value))
        p.setPen(QPen(text, max(2.0, side / 60), Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(0, 0), QPointF(side * 0.36, 0))
        p.restore()
        p.setBrush(text)
        p.drawEllipse(rect.center(), side / 30, side / 30)

        p.setPen(text)
        p.setFont(QFont(self.font().family(), max(7, int(side / 13))))
        p.drawText(rect.adjusted(0, side * 0.55, 0, 0), Qt.AlignHCenter,
                   f"{self._value:.1f}")


class Sparkline(QWidget):
    def __init__(self, low: float, high: float):
        super().__init__()
        self.low, self.high = low, high
        self.points: Deque[float] = deque(maxlen=HISTORY)
        self.setMinimumHeight(40)

    def add(self, v: float) -> None:
        self.points.append(v)
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(200, 50)

    def paintEvent(self, event) -> None:
        if len(self.points) < 2:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height() - 2
        span = (self.high - self.low) or 1
        step = w / (HISTORY - 1)
        x0 = w - step * (len(self.points) - 1)
        path = QPainterPath()
        for i, v in enumerate(self.points):
            pt = QPointF(x0 + i * step, h - (v - self.low) / span * h)
            path.moveTo(pt) if i == 0 else path.lineTo(pt)
        accent = self.palette().highlight().color()
        fill = QPainterPath(path)
        fill.lineTo(w, h)
        fill.lineTo(x0, h)
        grad = QLinearGradient(0, 0, 0, h)
        top = QColor(accent)
        top.setAlpha(110)
        bottom = QColor(accent)
        bottom.setAlpha(0)
        grad.setColorAt(0, top)
        grad.setColorAt(1, bottom)
        p.fillPath(fill, grad)
        p.setPen(QPen(accent, 1.6))
        p.drawPath(path)


class StatusLight(QWidget):
    """A round light; blinks while in alarm."""

    def __init__(self):
        super().__init__()
        self.state = OK
        self._lit = True
        self.blink = QTimer(self, interval=400)
        self.blink.timeout.connect(self._toggle)
        self.setFixedSize(18, 18)

    def set_state(self, state: str) -> None:
        if state == self.state:
            return
        self.state = state
        self._lit = True
        self.blink.start() if state == ALARM else self.blink.stop()
        self.update()

    def _toggle(self) -> None:
        self._lit = not self._lit
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QColor(LIGHT_COLOURS[self.state])
        if not self._lit:
            c = c.darker(250)
        p.setBrush(c)
        p.setPen(Qt.NoPen)
        p.drawEllipse(self.rect().adjusted(1, 1, -1, -1))


class SensorCard(QFrame):
    state_changed = Signal(str, str, float)          # sensor, state, value
    edit_requested = Signal(object)

    def __init__(self, sensor: Sensor):
        super().__init__(objectName="card")
        self.sensor = sensor
        self.state = OK
        title = QLabel(f"{sensor.name} ({sensor.unit})", objectName="title")
        self.light = StatusLight()
        self.value = QLabel("–", objectName="value")
        self.gauge = Gauge(sensor.low, sensor.high, sensor.warn, sensor.alarm)
        self.spark = Sparkline(sensor.low, sensor.high)
        head = QHBoxLayout()
        head.addWidget(title, 1)
        head.addWidget(self.light)
        body = QHBoxLayout()
        body.addWidget(self.value, 1)
        body.addWidget(self.gauge)
        v = QVBoxLayout(self)
        v.addLayout(head)
        v.addLayout(body, 1)
        v.addWidget(self.spark)
        self.setToolTip("Double-click to change the limits")

    def update_value(self, v: float) -> None:
        s = self.sensor
        self.value.setText(f"{v:,.1f}")
        self.gauge.glide_to(v)
        self.spark.add(v)
        band = (s.high - s.low) * HYSTERESIS
        if v >= s.alarm:
            state = ALARM
        elif v >= s.warn:
            state = WARN if self.state != ALARM or v < s.alarm - band \
                else ALARM
        elif self.state != OK and v >= s.warn - band:
            state = WARN                     # not clearly back yet
        else:
            state = OK
        self.light.set_state(state)
        if state != self.state:
            self.state = state
            self.state_changed.emit(s.name, state, v)

    def set_limits(self, warn: float, alarm: float) -> None:
        self.sensor.warn, self.sensor.alarm = warn, alarm
        self.gauge.warn, self.gauge.alarm = warn, alarm
        self.gauge.update()

    def mouseDoubleClickEvent(self, event) -> None:
        self.edit_requested.emit(self)


class DashboardWindow(QMainWindow):
    def __init__(self, seed: Optional[int] = None):
        super().__init__()
        self.setWindowTitle("Live dashboard")
        self.resize(1100, 680)
        self.rng = random.Random(seed)
        self.t = 0.0
        self.dark = False

        self.cards = [SensorCard(s) for s in SENSORS]
        for c in self.cards:
            c.state_changed.connect(self._state_changed)
            c.edit_requested.connect(self._edit_limits)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(12)

        self.alarms = QListWidget()
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.addWidget(QLabel("Alarms and warnings", objectName="title"))
        rv.addWidget(self.alarms, 1)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.alarms.clear)
        rv.addWidget(clear)

        split = QSplitter()
        split.addWidget(self.grid_host)
        split.addWidget(right)
        split.setSizes([800, 260])

        bar = QHBoxLayout()
        self.pause = QPushButton("Pause", checkable=True)
        self.pause.toggled.connect(self._pause)
        self.rate = QSlider(Qt.Horizontal, minimum=1, maximum=20, value=5,
                            maximumWidth=200)
        self.rate.valueChanged.connect(
            lambda hz: self.feed.setInterval(1000 // hz))
        theme = QPushButton("Light / dark")
        theme.clicked.connect(self.toggle_theme)
        bar.addWidget(self.pause)
        bar.addWidget(QLabel("Updates per second"))
        bar.addWidget(self.rate)
        bar.addStretch(1)
        bar.addWidget(theme)

        central = QWidget()
        v = QVBoxLayout(central)
        v.addLayout(bar)
        v.addWidget(split, 1)
        self.setCentralWidget(central)
        self._reflow()

        self.feed = QTimer(self, interval=1000 // self.rate.value())
        self.feed.timeout.connect(self.step)
        self.feed.start()
        self.setStyleSheet(LIGHT_THEME)

    def step(self) -> None:
        """One reading per sensor: a slow wave, noise, and rare spikes."""
        self.t += 0.05
        for i, c in enumerate(self.cards):
            s = c.sensor
            v = s.base + s.swing * math.sin(self.t / (1.5 + i)) \
                + self.rng.gauss(0, s.swing * 0.12)
            if self.rng.random() < 0.01:
                v += s.swing * 1.5
            c.update_value(min(max(v, s.low), s.high))

    def _state_changed(self, name: str, state: str, value: float) -> None:
        if state == OK:
            text = f"{name} back to normal ({value:.1f})"
        else:
            text = f"{name} {state.upper()}: {value:.1f}"
        stamp = QTime.currentTime().toString("HH:mm:ss")
        self.alarms.insertItem(0, f"{stamp}  {text}")
        self.alarms.item(0).setForeground(QColor(LIGHT_COLOURS[state]))

    def _edit_limits(self, card: SensorCard) -> None:
        s = card.sensor
        warn, ok = QInputDialog.getDouble(self, s.name, "Warning at", s.warn,
                                          s.low, s.high, 1)
        if not ok:
            return
        alarm, ok = QInputDialog.getDouble(self, s.name, "Alarm at",
                                           max(s.alarm, warn), warn, s.high, 1)
        if ok:
            card.set_limits(warn, alarm)

    def _pause(self, paused: bool) -> None:
        self.feed.stop() if paused else self.feed.start()
        self.pause.setText("Resume" if paused else "Pause")

    def toggle_theme(self) -> None:
        self.dark = not self.dark
        self.setStyleSheet(DARK_THEME if self.dark else LIGHT_THEME)

    def _reflow(self) -> None:
        """Two columns when wide, one when narrow."""
        cols = 2 if self.grid_host.width() > 620 or not self.isVisible() \
            else 1
        for i, c in enumerate(self.cards):
            self.grid.addWidget(c, i // cols, i % cols)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._reflow()


def make_window(argv: Optional[List[str]] = None) -> DashboardWindow:
    return DashboardWindow()


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    win = make_window()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
