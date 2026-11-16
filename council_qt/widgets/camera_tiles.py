"""
council_qt.widgets.camera_tiles — which camera the main view shows, in a
Typhon window that runs several cameras (Andy, 16 November 2026).

With two or more cameras connected, a dropdown sits just under the main
view's right edge, above the slider: "Camera 1", "Camera 2", ... and "Multicam".

  * Camera N shows that camera in the main view, as with one camera. It is
    the ACTIVE camera: the settings tabs, the boxes beside Start, the
    capture set-up, the preset box, Camera settings… and the slider act on
    it (frame_camera._select_camera).
  * Multicam lays every camera's live stream over the main view: side by
    side for two, a 2 x 2 grid for three or four. The active camera is
    outlined; clicking another camera's picture makes it the active one,
    so the settings follow it, and Multicam stays on.

With one camera the dropdown and the grid are hidden and the window looks
exactly as before.

Fed by frame_camera's UI timer (`show_cameras`), on the UI thread. Each
picture in the grid is redrawn at most GRID_HZ times a second, from every
n-th pixel of the frame (never the whole frame: a 65 MP frame scaled whole
would cost more than the tick).
"""
from __future__ import annotations

import math
import time
from typing import Any, Callable, Dict, List, Optional

from PySide6.QtCore import QEvent, QObject, QPoint, Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QComboBox, QFrame, QGridLayout, QLabel,
                               QVBoxLayout, QWidget)

#: The dropdown's last entry.
MULTICAM = "Multicam"
#: How often a picture in the grid is redrawn.
GRID_HZ = 10.0
MARGIN = 6
#: The dropdown's height: it fits the gap between the picture and the
#: slider in Typhon's wireframe (24 px).
GAP_HEIGHT = 22


def small_image(image: Any, width: int, height: int) -> Optional[QImage]:
    """An 8-bit QImage of a camera frame (mono or RGB) about `width` x
    `height`, taken from every n-th pixel; None when it cannot be made."""
    try:
        import numpy as np

        data = np.asarray(image)
        if data.ndim not in (2, 3) or data.size == 0:
            return None
        h, w = data.shape[:2]
        step = max(1, int(max(w / max(width, 1), h / max(height, 1))))
        data = data[::step, ::step]
        if data.dtype != np.uint8:
            if data.dtype.kind in "ui":
                top = int(data.max()) if data.size else 0
                data = (data >> max(0, top.bit_length() - 8)).astype(np.uint8)
            else:
                data = np.clip(data, 0, 255).astype(np.uint8)
        data = np.ascontiguousarray(data)
        h, w = data.shape[:2]
        if data.ndim == 2:
            out = QImage(data.data, w, h, w, QImage.Format.Format_Grayscale8)
        elif data.shape[2] == 3:
            out = QImage(data.data, w, h, 3 * w, QImage.Format.Format_RGB888)
        else:
            return None
        return out.copy()          # owns its pixels once `data` goes
    except Exception:                                     # noqa: BLE001
        return None


def grid_shape(count: int) -> "tuple[int, int]":
    """(rows, columns): side by side for two, 2 x 2 for three or four."""
    if count <= 2:
        return 1, max(1, count)
    columns = math.ceil(math.sqrt(count))
    return math.ceil(count / columns), columns


def entry_text(number: int, label: str) -> str:
    """"Camera 2 · Simulated event camera (SIM-2)"."""
    label = str(label or "").strip()
    return f"Camera {number} · {label}" if label else f"Camera {number}"


class _Cell(QFrame):
    """One camera's picture in Multicam, with its caption under it."""

    def __init__(self, name: str, pick: Callable[[str], Any],
                 parent: QWidget) -> None:
        super().__init__(parent)
        self.name = name
        self._pick = pick
        self._drawn = 0.0
        self.active = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        column = QVBoxLayout(self)
        column.setContentsMargins(3, 3, 3, 3)
        column.setSpacing(2)
        self.picture = QLabel(self)
        self.picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.picture.setMinimumSize(32, 24)
        self.caption = QLabel(name, self)
        self.caption.setStyleSheet("color: white; font-size: 11px;")
        column.addWidget(self.picture, 1)
        column.addWidget(self.caption)
        self.set_active(False)

    def set_active(self, active: bool) -> None:
        if active == self.active:
            return
        self.active = active
        border = "#4da3ff" if active else "#444"
        self.setStyleSheet(
            f"QFrame {{ background: #111; border: 2px solid {border}; }} "
            f"QLabel {{ border: none; background: transparent; }}")
        self.setToolTip("" if active else
                        f"Click to adjust {self.name}'s settings")

    def show_frame(self, frame: Any) -> None:
        now = time.monotonic()
        if frame is None or now - self._drawn < 1 / GRID_HZ:
            return
        size = self.picture.size()
        image = small_image(getattr(frame, "image", None),
                            size.width(), size.height())
        if image is None:
            return
        self._drawn = now
        self.picture.setPixmap(QPixmap.fromImage(image).scaled(
            size, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.FastTransformation))

    def mousePressEvent(self, event: Any) -> None:  # noqa: N802 (Qt)
        if event.button() == Qt.MouseButton.LeftButton and not self.active:
            self._pick(self.name)
        super().mousePressEvent(event)


class CameraTiles(QObject):
    """The dropdown (Camera 1, Camera 2, ..., Multicam) and the Multicam
    grid, over the main view."""

    def __init__(self, canvas: QWidget, pick: Callable[[str], Any]) -> None:
        super().__init__(canvas)
        self.canvas = canvas
        self._pick = pick
        self.multicam = False
        self.names: List[str] = []
        # BELOW the picture, above the slider (Andy, 16 November): over the
        # picture it hid part of the stream. A child of the canvas's parent,
        # in the gap the wireframe leaves under the canvas.
        self.combo = QComboBox(canvas.parentWidget() or canvas)
        self.combo.setToolTip("Which camera the main view shows — its "
                              "settings are the ones the tabs adjust — or "
                              "Multicam, every camera at once")
        self.combo.activated.connect(self._chosen)
        self.combo.hide()
        self.grid = QWidget(canvas)
        self.grid.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.grid.setStyleSheet("background: #000;")
        self._layout = QGridLayout(self.grid)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(4)
        self.cells: Dict[str, _Cell] = {}
        self.grid.hide()
        canvas.installEventFilter(self)

    # -- placing -------------------------------------------------------
    def eventFilter(self, watched: Any, event: Any) -> bool:  # noqa: N802
        if watched is self.canvas and event.type() == QEvent.Type.Resize:
            self._place()
        return False

    def _place(self) -> None:
        self.grid.setGeometry(0, 0, self.canvas.width(), self.canvas.height())
        self.grid.raise_()
        width = min(max(self.combo.sizeHint().width(), 160), 300)
        height = min(self.combo.sizeHint().height(), GAP_HEIGHT)
        holder = self.combo.parentWidget()
        if holder is self.canvas:
            # No parent to sit in: inside the picture's lower right corner.
            x = self.canvas.width() - width - MARGIN
            y = self.canvas.height() - height - MARGIN
        else:
            corner = self.canvas.mapTo(holder, QPoint(self.canvas.width(),
                                                      self.canvas.height()))
            x, y = corner.x() - width, corner.y() + 1
        self.combo.setGeometry(x, y, width, height)
        self.combo.raise_()

    # -- the dropdown --------------------------------------------------
    def _chosen(self, index: int) -> None:
        if index == len(self.names):
            self.multicam = True
        elif 0 <= index < len(self.names):
            self.multicam = False
            self._pick(self.names[index])
        self._show_mode()

    def _show_mode(self) -> None:
        if self.multicam and len(self.names) > 1:
            self.grid.show()
            self._place()
        else:
            self.grid.hide()

    def _fill(self, rows: List[Dict[str, Any]]) -> None:
        self.combo.blockSignals(True)
        self.combo.clear()
        for n, row in enumerate(rows, 1):
            self.combo.addItem(entry_text(n, row.get("label", "")))
        self.combo.addItem(MULTICAM)
        self.combo.blockSignals(False)

    def _rebuild_grid(self) -> None:
        for cell in self.cells.values():
            self._layout.removeWidget(cell)
            cell.deleteLater()
        self.cells = {}
        _rows, columns = grid_shape(len(self.names))
        for n, name in enumerate(self.names):
            cell = _Cell(name, self._pick, self.grid)
            self._layout.addWidget(cell, n // columns, n % columns)
            self.cells[name] = cell

    # -- fed by the timer ----------------------------------------------
    def show_cameras(self, rows: List[Dict[str, Any]]) -> None:
        """One row per connected camera: name, label, active, text and
        frame (its newest, or None). Fewer than two: all hidden."""
        if len(rows) < 2:
            self.names = []
            self.multicam = False
            self.combo.hide()
            self.grid.hide()
            return
        names = [str(r["name"]) for r in rows]
        if names != self.names:
            self.names = names
            self._fill(rows)
            self._rebuild_grid()
            self._place()
        active = next((n for n, r in enumerate(rows) if r.get("active")), 0)
        want = len(names) if self.multicam else active
        if self.combo.currentIndex() != want:
            self.combo.blockSignals(True)
            self.combo.setCurrentIndex(want)
            self.combo.blockSignals(False)
        if not self.combo.isVisible():
            self.combo.show()
            self._place()
        self._show_mode()
        if not self.multicam:
            return
        for row in rows:
            cell = self.cells[str(row["name"])]
            cell.set_active(bool(row.get("active")))
            text = str(row.get("text") or row["name"])
            if cell.caption.text() != text:
                cell.caption.setText(text)
            cell.show_frame(row.get("frame"))

    def close(self) -> None:
        try:
            self.canvas.removeEventFilter(self)
            self.combo.deleteLater()
            self.grid.deleteLater()
        except RuntimeError:
            pass
