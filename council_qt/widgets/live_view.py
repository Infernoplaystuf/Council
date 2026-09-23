"""
council_qt.widgets.live_view — a numpy frame on screen, and a box drawn on it.

THE IMAGE IS COPIED, AND THAT IS NOT A PERFORMANCE MISTAKE
`QImage(buffer, w, h, stride, format)` does NOT take ownership of the buffer.
It keeps a bare pointer. When the numpy array backing it is collected the
QImage still points at freed memory, and reading it usually still WORKS —
measured on this machine: a pixel read after freeing returned the right value,
because nothing had reused the page yet. That is precisely why the bug ships.
It surfaces later as torn frames, and under a grab loop reusing one buffer it
surfaces immediately. So `to_qimage` copies, once, and owns what it returns.

THE STRIDE IS PASSED EXPLICITLY
An AOI crop of a larger frame is a NON-CONTIGUOUS numpy slice: measured here,
a 6-wide view of a 12-wide array has stride 12. Hand that to QImage without
its real stride and every row is offset a little further than the last, which
renders as a diagonal smear. Arrays are made contiguous before conversion and
the stride is passed anyway, so neither assumption stands alone.

DRAGGING A BOX GIVES SENSOR COORDINATES, NOT SCREEN ONES
The view is scaled to fit and may already be showing a partial AOI. A box the
user drags therefore has to be mapped back through BOTH the display scale and
the current AOI origin, or the second AOI a user sets lands somewhere they did
not click. `_to_sensor` does that, and it is tested.
"""
from __future__ import annotations

from typing import Any, Optional, Tuple

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget

from council_core.cameras import Roi

#: Ignore a "drag" this small — it is a click, and a 2-pixel AOI is never
#: what anyone meant.
MIN_DRAG = 8


def to_qimage(array: Any) -> QImage:
    """A numpy array as a QImage that owns its pixels.

    Accepts 8-bit grayscale, 16-bit grayscale (scaled down — a Basler Mono12
    frame arrives as uint16 and would otherwise render as noise), RGB and
    RGBA.
    """
    import numpy as np

    if array is None:
        return QImage()
    data = np.asarray(array)
    if data.ndim == 2 and data.dtype == np.uint16:
        # Mono10/12/16 -> 8 bits for display. Shifting by the real bit depth
        # would need the camera to tell us; using the observed maximum keeps a
        # dark frame visible instead of black.
        top = int(data.max()) or 1
        shift = max(0, top.bit_length() - 8)
        data = (data >> shift).astype(np.uint8) if shift else data.astype(np.uint8)
    if data.dtype != np.uint8:
        data = np.clip(data, 0, 255).astype(np.uint8)
    data = np.ascontiguousarray(data)

    if data.ndim == 2:
        h, w = data.shape
        fmt = QImage.Format.Format_Grayscale8
        image = QImage(data.data, w, h, data.strides[0], fmt)
    elif data.ndim == 3 and data.shape[2] == 3:
        h, w, _ = data.shape
        image = QImage(data.data, w, h, data.strides[0],
                       QImage.Format.Format_RGB888)
    elif data.ndim == 3 and data.shape[2] == 4:
        h, w, _ = data.shape
        image = QImage(data.data, w, h, data.strides[0],
                       QImage.Format.Format_RGBA8888)
    else:
        return QImage()
    # The copy is the whole point: `data` is collected the moment this returns.
    return image.copy()


class LiveView(QWidget):
    """The picture, scaled to fit, with a draggable AOI box."""

    #: A box the user finished drawing, in SENSOR pixels.
    roi_drawn = Signal(object)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self._pixmap: Optional[QPixmap] = None
        #: Where the frame sits inside the sensor, so a drag can be mapped
        #: back. Set by whoever knows the AOI.
        self.origin: Tuple[int, int] = (0, 0)
        self._drag_from: Optional[QPoint] = None
        self._drag_to: Optional[QPoint] = None
        self.placeholder = "No camera connected."

    # ------------------------------------------------------------------
    def show_frame(self, frame: Any) -> None:
        """Display one frame. Call on the UI thread."""
        image = to_qimage(getattr(frame, "image", frame))
        self._pixmap = None if image.isNull() else QPixmap.fromImage(image)
        self.update()

    def clear(self) -> None:
        self._pixmap = None
        self.update()

    # ------------------------------------------------------------------
    def paintEvent(self, event) -> None:                    # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#101014"))
        if self._pixmap is None:
            painter.setPen(QPen(QColor("#8a8a94")))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             self.placeholder)
            return

        placed = self.placement()
        scaled = self._pixmap.scaled(
            placed.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)
        painter.drawPixmap(placed.left(), placed.top(), scaled)

        if self._drag_from is not None and self._drag_to is not None:
            painter.setPen(QPen(QColor("#3fb950"), 2, Qt.PenStyle.DashLine))
            painter.drawRect(QRect(self._drag_from, self._drag_to).normalized())

    # ------------------------------------------------------------------
    def mousePressEvent(self, event) -> None:               # noqa: N802
        if self._pixmap is not None:
            self._drag_from = event.position().toPoint()
            self._drag_to = self._drag_from

    def mouseMoveEvent(self, event) -> None:                # noqa: N802
        if self._drag_from is not None:
            self._drag_to = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event) -> None:             # noqa: N802
        if self._drag_from is None:
            return
        box = QRect(self._drag_from, event.position().toPoint()).normalized()
        self._drag_from = self._drag_to = None
        self.update()
        if box.width() < MIN_DRAG or box.height() < MIN_DRAG:
            return          # a click, not a selection
        roi = self._to_sensor(box)
        if roi is not None:
            self.roi_drawn.emit(roi)

    # ------------------------------------------------------------------
    def placement(self) -> QRect:
        """Where the frame is drawn inside the widget.

        COMPUTED, not remembered from the last paint. Storing it in
        paintEvent means a box dragged before the first paint -- or after a
        resize that has not repainted yet -- is mapped through a stale
        rectangle, and lands somewhere the user did not click.
        """
        if self._pixmap is None or self._pixmap.isNull():
            return QRect()
        size = self._pixmap.size().scaled(
            self.size(), Qt.AspectRatioMode.KeepAspectRatio)
        return QRect((self.width() - size.width()) // 2,
                     (self.height() - size.height()) // 2,
                     size.width(), size.height())

    # ------------------------------------------------------------------
    def _to_sensor(self, box: QRect) -> Optional[Roi]:
        """A widget rectangle as sensor pixels.

        Through the display scale AND the current AOI origin — a box drawn on
        a frame that is itself a crop is relative to that crop, not to the
        sensor.
        """
        placed = self.placement()
        if self._pixmap is None or placed.isEmpty():
            return None
        scale_x = self._pixmap.width() / max(1, placed.width())
        scale_y = self._pixmap.height() / max(1, placed.height())
        left = (box.left() - placed.left()) * scale_x
        top = (box.top() - placed.top()) * scale_y
        width = box.width() * scale_x
        height = box.height() * scale_y

        # Clamp to the frame before adding the origin, so a drag that ran off
        # the edge of the widget cannot produce an AOI outside the sensor.
        left = max(0.0, min(left, self._pixmap.width()))
        top = max(0.0, min(top, self._pixmap.height()))
        width = max(0.0, min(width, self._pixmap.width() - left))
        height = max(0.0, min(height, self._pixmap.height() - top))
        if width < 1 or height < 1:
            return None
        return Roi(int(left) + int(self.origin[0]),
                   int(top) + int(self.origin[1]),
                   int(width), int(height))
