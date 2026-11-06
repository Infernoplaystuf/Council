"""
typhon.trigger's panel: the Triggering pop-out's inner widget
(docs/typhon_plugins_design.md 3.7) — the trigger rows of the camera's
set-up, Capture now (F9) and the run's status.

SHOWN FROM P4 (2 November 2026): the "Triggering" button on the plugin
toolbar opens it in its own window, and the Basic tab no longer has the
trigger boxes (examples/gui/typhon_plugins.gspec); the main window keeps
Capture now (F9) beside Start and Stop. It is made on the UI thread, on
first open (TriggerPlugin.panel).

Its rows are the plugin's settings_rows() — the set-up read in place, the
camera's own "capture.*" keys — and a change is written as the Basic tab's
boxes wrote it (TriggerPlugin.write_setting: refused while capturing, the
views told). Downsample is not here: it is the core's (design 2.5).

Only the rows the chosen mode and source use are shown: Frames per trigger
for exact and snapshot, Timer every for the Timer, Record for for a record
run, and the camera's edge, delay and debounce for one of its inputs. The
rows are built again when what they offer changes (a camera connected, a
trigger-source plugin switched on). While it is shown it reads the status
twice a second.

THE CAMERA'S LINES (5 November 2026): for a camera whose I/O lines can be
read (a Basler's), a line under the rows gives each input line's level, and
"Find my trigger line" watches every input for FIND_LINE_SECONDS, says on
which line the pulses came and offers "Use LineN" (capture.source). Not
while capturing.

Qt widgets only (QtCore, QtGui, QtWidgets: design 3.11's rule for panels).
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QSpinBox, QVBoxLayout, QWidget)

import typhon_plugin_api as api
from typhon_plugin_api import builtin

#: How often the shown panel reads the run's status, ms.
REFRESH_MS = 500

MODE_KEY = "capture.mode"
SOURCE_KEY = "capture.source"
PER_KEY = "capture.per_trigger"
EVERY_KEY = "capture.every_s"
RECORD_KEY = "capture.record_s"
LINE_KEYS = ("capture.edge", "capture.delay_us", "capture.debounce_us")

INTRO = ("What tells the camera to capture. Exact: the camera exposes only "
         "on a trigger. Snapshot: it runs live and each trigger saves the "
         "next frames. Record: each trigger saves every frame for a while.")
NO_CAMERA = "Connect a frame camera to set up its triggers."
FIND = "Find my trigger line"
FIND_TIP = ("Watches every input line of the camera for a few seconds: send "
            "a few pulses down the trigger cable and Typhon says which line "
            "they came on.")


def shown_keys(values: Dict[str, Any]) -> set:
    """The rows the set-up `values` (key: value) uses now."""
    mode = str(values.get(MODE_KEY, ""))
    source = str(values.get(SOURCE_KEY, ""))
    keys = {MODE_KEY}
    if mode in ("", api.MODE_CONTINUOUS):
        return keys
    keys.add(SOURCE_KEY)
    if mode == api.MODE_RECORD:
        keys.add(RECORD_KEY)
    else:
        keys.add(PER_KEY)
    if source == api.TIMER:
        keys.add(EVERY_KEY)
    if source.startswith(("Line", "CxpTrigger", "CC")):
        keys.update(LINE_KEYS)
    return keys


class TriggerPanel(QWidget):
    """The trigger rows, Capture now and the status line."""

    def __init__(self, plugin: Any, parent: Any = None) -> None:
        super().__init__(parent)
        self.setObjectName("trigger_panel")
        self.plugin = plugin
        #: Each row's widget, by its key (capture.mode, capture.source ...).
        self.rows: Dict[str, Any] = {}
        #: What the rows were built from: (key, choices) of each.
        self._made: Tuple[Any, ...] = ()
        self._filling = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        intro = QLabel(INTRO, self)
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.empty = QLabel(NO_CAMERA, self)
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)
        self._form = QFormLayout()
        layout.addLayout(self._form)
        # the camera's lines: their levels, and the line finder
        self.lines_label = QLabel("", self)
        self.lines_label.setObjectName("trigger_lines")
        self.lines_label.setWordWrap(True)
        layout.addWidget(self.lines_label)
        finder = QHBoxLayout()
        self.find_button = QPushButton(FIND, self)
        self.find_button.setObjectName("btn_trigger_find_line")
        self.find_button.setToolTip(FIND_TIP)
        self.find_button.clicked.connect(self._find_clicked)
        finder.addWidget(self.find_button)
        self.use_button = QPushButton("", self)
        self.use_button.setObjectName("btn_trigger_use_line")
        self.use_button.clicked.connect(self._use_found)
        self.use_button.hide()
        finder.addWidget(self.use_button)
        finder.addStretch(1)
        layout.addLayout(finder)
        #: The line finder's watcher while it looks, and when it ends.
        self.finding: Optional[Any] = None
        self._find_until = 0.0
        self.found_line = ""
        self._find_timer = QTimer(self)
        self._find_timer.setInterval(200)
        self._find_timer.timeout.connect(self._find_tick)
        self.capture_button = QPushButton("Capture now (F9)", self)
        self.capture_button.setObjectName("btn_trigger_capture_now")
        self.capture_button.clicked.connect(self._capture_now)
        layout.addWidget(self.capture_button)
        self.status_label = QLabel("", self)
        self.status_label.setObjectName("trigger_status")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.said = QLabel("", self)
        self.said.setWordWrap(True)
        layout.addWidget(self.said)
        layout.addStretch(1)
        self.timer = QTimer(self)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(self.refresh)
        self.refresh()

    # -- the rows ------------------------------------------------------------
    @staticmethod
    def _shape(settings: Any) -> Tuple[Any, ...]:
        return tuple((s.key, tuple(getattr(s, "choices", None) or ()))
                     for s in settings)

    def _build(self, settings: Any) -> None:
        while self._form.rowCount():
            self._form.removeRow(0)
        self.rows = {}
        for setting in settings:
            widget = self._widget_for(setting)
            widget.setToolTip(str(getattr(setting, "help", "") or ""))
            self._form.addRow(str(setting.label), widget)
            self.rows[setting.key] = widget
        self._made = self._shape(settings)

    def _widget_for(self, setting: Any) -> Any:
        kind = str(getattr(setting, "kind", ""))
        key = setting.key
        if getattr(setting, "choices", None):
            box = QComboBox(self)
            box.addItems([str(c) for c in setting.choices])
            box.currentTextChanged.connect(
                lambda text, k=key: self._changed(k, text))
            return box
        if kind == "int":
            spin = QSpinBox(self)
            spin.setRange(int(setting.minimum or 0),
                          int(setting.maximum or 1_000_000))
            spin.valueChanged.connect(lambda v, k=key: self._changed(k, v))
            return spin
        if kind == "float":
            dspin = QDoubleSpinBox(self)
            dspin.setDecimals(3)
            dspin.setRange(float(setting.minimum or 0.0),
                           float(setting.maximum or 1e9))
            if getattr(setting, "unit", ""):
                dspin.setSuffix(f" {setting.unit}")
            if key == RECORD_KEY:
                dspin.setSpecialValueText("until the next trigger")
            dspin.valueChanged.connect(lambda v, k=key: self._changed(k, v))
            return dspin
        line = QLineEdit(self)
        line.editingFinished.connect(
            lambda k=key, w=line: self._changed(k, w.text()))
        return line

    def _changed(self, key: str, value: Any) -> None:
        if self._filling:
            return
        try:
            out = self.plugin.write_setting(key, value)
            self.said.setText(str(out.get("summary", "")))
        except Exception as exc:                          # noqa: BLE001
            self.said.setText(f"{type(exc).__name__}: {exc}")
        self.refresh()

    # -- showing what is ------------------------------------------------------
    def showEvent(self, event: Any) -> None:              # noqa: N802
        super().showEvent(event)
        self.refresh()
        self.timer.start()

    def hideEvent(self, event: Any) -> None:              # noqa: N802
        super().hideEvent(event)
        self.timer.stop()
        if self.finding is not None:       # a hidden finder: stopped
            self._find_done()

    def refresh(self) -> None:
        """The rows as the camera's set-up has them now (built again when
        what they offer changed; only those the mode and source use
        shown), greyed during a run (fixed while capturing); Capture now
        enabled while a press fires; the status segment."""
        try:
            settings = list(self.plugin.settings_rows())
        except Exception:                                 # noqa: BLE001
            settings = []
        if self._shape(settings) != self._made:
            self._build(settings)
        status = self.plugin.status()
        capturing = bool(status)
        try:
            capturing = bool(self.plugin.capturing()) or capturing
        except Exception:                                 # noqa: BLE001
            pass
        run = self.plugin._run(None)
        fires = bool(capturing and run is not None and run.books is not None
                     and run.books.typhon_fires)
        values = {s.key: s.value for s in settings}
        wanted = shown_keys(values)
        self._filling = True
        try:
            for key, widget in self.rows.items():
                widget.setEnabled(not capturing)
                self._form.setRowVisible(widget, key in wanted)
                if key not in values:
                    continue
                value = values[key]
                if isinstance(widget, QComboBox):
                    widget.setCurrentText(str(value))
                elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                    widget.setValue(value)
                else:
                    widget.setText(str(value))
        finally:
            self._filling = False
        self.empty.setVisible(not self.rows)
        self.capture_button.setEnabled(fires)
        self.status_label.setText(status)
        self._show_lines(capturing)

    # -- the camera's lines ---------------------------------------------------
    def _show_lines(self, capturing: bool) -> None:
        try:
            levels = dict(self.plugin.line_levels())
        except Exception:                                 # noqa: BLE001
            levels = {}
        if not levels and self.finding is None:
            self.lines_label.hide()
            self.find_button.hide()
            self.use_button.hide()
            return
        words = {True: "HIGH", False: "low", None: "?"}
        self.lines_label.setText("Input lines now: " + "  ·  ".join(
            f"{line} {words.get(level, '?')}"
            for line, level in levels.items()))
        self.lines_label.show()
        self.find_button.show()
        self.find_button.setEnabled(self.finding is not None or
                                    not capturing)
        self.use_button.setEnabled(not capturing)

    def _find_clicked(self) -> None:
        if self.finding is not None:
            self._find_done()
            return
        try:
            self.finding = self.plugin.find_line()
        except Exception as exc:                          # noqa: BLE001
            self.said.setText(str(exc))
            return
        self.found_line = ""
        self.use_button.hide()
        self._find_until = time.monotonic() + float(
            builtin.FIND_LINE_SECONDS)
        self.find_button.setText("Stop looking")
        self._find_tick()
        self._find_timer.start()

    def _find_tick(self) -> None:
        watcher = self.finding
        if watcher is None:
            return
        left = self._find_until - time.monotonic()
        if left <= 0:
            self._find_done()
            return
        seen = sum(watcher.counts.values())
        self.said.setText(
            f"Watching {', '.join(watcher.lines)} — send a few pulses down "
            f"the trigger cable now ({left:.0f} s left; {seen} pulse"
            f"{'' if seen == 1 else 's'} so far).")

    def _find_done(self) -> None:
        """The finder stopped: what it found, and "Use LineN" for it."""
        self._find_timer.stop()
        watcher, self.finding = self.finding, None
        self.find_button.setText(FIND)
        if watcher is None:
            return
        watcher.stop()
        self.said.setText(builtin.find_line_words(watcher))
        seen = {l: n for l, n in watcher.counts.items() if n}
        self.found_line = max(seen, key=seen.get) if seen else ""
        if self.found_line:
            self.use_button.setText(f"Use {self.found_line}")
            self.use_button.show()
        self.refresh()

    def _use_found(self) -> None:
        if not self.found_line:
            return
        self._changed(SOURCE_KEY, self.found_line)
        self.use_button.hide()

    def closeEvent(self, event: Any) -> None:             # noqa: N802
        if self.finding is not None:
            self._find_done()
        super().closeEvent(event)

    def _capture_now(self) -> None:
        self.said.setText(str(self.plugin.capture_now() or ""))
        self.refresh()
