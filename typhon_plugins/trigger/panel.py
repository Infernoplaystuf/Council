"""
typhon.trigger's panel: the triggering pop-out's inner widget
(docs/typhon_plugins_design.md 3.7) — the trigger rows of the camera's
set-up, Capture now (F9) and the run's status.

BUILT, NOT SHOWN, UNTIL STAGE P4. The window keeps today's trigger boxes on
the Basic tab and today's Capture now button until P4 gives the plugins
their pop-out windows; this is the widget that window will hold. It is made
on the UI thread, on first open (TriggerPlugin.panel).

Its rows are the plugin's settings_rows() — the set-up read in place, the
camera's own "capture.*" keys — and a change is written as the Basic tab's
boxes write it (TriggerPlugin.write_setting: refused while capturing, the
views told). Downsample is not here: it is the core's (design 2.5).

Qt widgets only (QtCore, QtGui, QtWidgets: design 3.11's rule for panels).
"""
from __future__ import annotations

from typing import Any, Dict

from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QFormLayout,
                               QLabel, QLineEdit, QPushButton, QSpinBox,
                               QVBoxLayout, QWidget)


class TriggerPanel(QWidget):
    """The trigger rows, Capture now and the status line."""

    def __init__(self, plugin: Any, parent: Any = None) -> None:
        super().__init__(parent)
        self.plugin = plugin
        #: Each row's widget, by its key (capture.mode, capture.source ...).
        self.rows: Dict[str, Any] = {}
        self._filling = False
        layout = QVBoxLayout(self)
        self._form = QFormLayout()
        layout.addLayout(self._form)
        self.capture_button = QPushButton("Capture now (F9)", self)
        self.capture_button.clicked.connect(self._capture_now)
        layout.addWidget(self.capture_button)
        self.status_label = QLabel("", self)
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)
        self.said = QLabel("", self)
        self.said.setWordWrap(True)
        layout.addWidget(self.said)
        layout.addStretch(1)
        self._build()
        self.refresh()

    # -- the rows ------------------------------------------------------------
    def _build(self) -> None:
        for setting in self.plugin.settings_rows():
            widget = self._widget_for(setting)
            widget.setToolTip(str(getattr(setting, "help", "") or ""))
            self._form.addRow(str(setting.label), widget)
            self.rows[setting.key] = widget

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
    def refresh(self) -> None:
        """The rows as the camera's set-up has them now, greyed during a
        run (fixed while capturing); Capture now enabled while a press
        fires; the status segment."""
        status = self.plugin.status()
        capturing = bool(status)
        run = self.plugin._run(None)
        fires = bool(capturing and run is not None and run.books is not None
                     and run.books.typhon_fires)
        values = {s.key: s.value for s in self.plugin.settings_rows()}
        self._filling = True
        try:
            for key, widget in self.rows.items():
                widget.setEnabled(not capturing)
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
        self.capture_button.setEnabled(fires)
        self.status_label.setText(status)

    def _capture_now(self) -> None:
        self.said.setText(str(self.plugin.capture_now() or ""))
        self.refresh()
