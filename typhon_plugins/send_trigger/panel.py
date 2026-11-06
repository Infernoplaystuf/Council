"""
typhon.send_trigger's window: a drop-down of the connected cameras and a
"Send trigger" button (plugin.py says what a press does). The drop-down is
read again twice a second while the window is shown, keeping the camera
picked while it is still there (else the active one).

Qt widgets only (QtCore, QtWidgets: design 3.11's rule for panels).
"""
from __future__ import annotations

from typing import Any, List, Tuple

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout, QWidget)

REFRESH_MS = 500
INTRO = ("Sends one trigger to the camera you pick — the same as Capture "
         "now (F9) on that camera. Start a triggered capture on it first.")
NO_CAMERA = "No camera connected."


class SendTriggerPanel(QWidget):
    def __init__(self, plugin: Any, parent: Any = None) -> None:
        super().__init__(parent)
        self.setObjectName("send_trigger_panel")
        self.plugin = plugin
        self._listed: List[Tuple[str, str]] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        intro = QLabel(INTRO, self)
        intro.setWordWrap(True)
        layout.addWidget(intro)
        row = QHBoxLayout()
        row.addWidget(QLabel("Camera", self))
        self.camera_box = QComboBox(self)
        self.camera_box.setObjectName("send_trigger_camera")
        self.camera_box.setMinimumContentsLength(24)
        row.addWidget(self.camera_box, 1)
        layout.addLayout(row)
        self.send_button = QPushButton("Send trigger", self)
        self.send_button.setObjectName("btn_send_trigger")
        self.send_button.clicked.connect(self.send)
        layout.addWidget(self.send_button)
        self.said = QLabel("", self)
        self.said.setWordWrap(True)
        layout.addWidget(self.said)
        layout.addStretch(1)
        self.timer = QTimer(self)
        self.timer.setInterval(REFRESH_MS)
        self.timer.timeout.connect(self.refresh)
        self.refresh()

    def showEvent(self, event: Any) -> None:              # noqa: N802
        super().showEvent(event)
        self.refresh()
        self.timer.start()

    def hideEvent(self, event: Any) -> None:              # noqa: N802
        super().hideEvent(event)
        self.timer.stop()

    def chosen(self) -> str:
        data = self.camera_box.currentData()
        return str(data) if data else ""

    def refresh(self) -> None:
        try:
            listed = list(self.plugin.cameras())
        except Exception:                                 # noqa: BLE001
            listed = []
        if listed != self._listed:
            keep = self.chosen() or (self.plugin.active() or "")
            self.camera_box.blockSignals(True)
            self.camera_box.clear()
            for name, label in listed:
                self.camera_box.addItem(label, name)
            names = [n for n, _ in listed]
            if keep in names:
                self.camera_box.setCurrentIndex(names.index(keep))
            self.camera_box.blockSignals(False)
            self._listed = listed
        self.send_button.setEnabled(bool(listed))
        if not listed:
            self.camera_box.setPlaceholderText(NO_CAMERA)

    def send(self) -> str:
        said = str(self.plugin.send(self.chosen()) or "")
        self.said.setText(said)
        return said
