"""
council_qt.widgets.pop_out — the picture on screen, copied into its own window.

Typhon's "Pop out" button: the frame the viewer is showing (a saved PNG, a raw
window, or the camera live) is COPIED into a separate window that can be
moved to another monitor and made full screen, while the main window carries
on capturing, playing or previewing. Each press is a new window with its own
copy; closing it frees it.

A COPY, NOT A VIEW. The main canvas's picture is replaced thirty times a
second while live; a window that shared it would show whatever arrived last.
QImage.copy() gives the new window pixels of its own.

THE APP'S OWN CANVAS. The window is built with the same ImageCanvas class the
generated app uses, so panning and zooming work exactly as in the main window.

FIT FOLLOWS THE WINDOW. A canvas fits the picture once, when it gets it. A
window that goes full screen or is resized is re-fitted, so full screen means
the picture fills the screen rather than sitting at its old size in a corner.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QImage, QKeySequence, QShortcut
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QVBoxLayout,
                               QWidget)


def current_image(canvas: Any) -> Optional[QImage]:
    """A copy of the whole picture `canvas` is showing, or None if empty."""
    base = getattr(canvas, "_base", None)
    if not isinstance(base, QImage) or base.isNull():
        return None
    return base.copy()


class PopOutWindow(QWidget):
    """One copied picture in a window of its own."""

    def __init__(self, image: QImage, title: str,
                 make_canvas: Callable[[QWidget], Any]):
        super().__init__(None, Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle(title or "Picture")
        self.image = image

        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 4, 4, 4)
        bar = QHBoxLayout()
        self.full_btn = QPushButton("Full screen", self)
        self.full_btn.clicked.connect(self.toggle_full_screen)
        self.fit_btn = QPushButton("Fit", self)
        self.fit_btn.clicked.connect(self.fit)
        bar.addWidget(self.full_btn)
        bar.addWidget(self.fit_btn)
        self.hint = QLabel("F11 full screen · Esc leaves it · scroll to zoom · "
                           "drag to pan", self)
        bar.addWidget(self.hint)
        bar.addStretch(1)
        outer.addLayout(bar)

        self.canvas = make_canvas(self)
        outer.addWidget(self.canvas, 1)
        self.canvas.set_image(image)

        QShortcut(QKeySequence(Qt.Key.Key_F11), self, self.toggle_full_screen)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self.leave_full_screen)
        self.installEventFilter(self)
        self.resize(min(1280, max(480, image.width() + 16)),
                    min(900, max(360, image.height() + 56)))

    # -- full screen -------------------------------------------------------
    def toggle_full_screen(self) -> None:
        if self.isFullScreen():
            self.leave_full_screen()
        else:
            self.showFullScreen()
            self.full_btn.setText("Leave full screen")

    def leave_full_screen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        self.full_btn.setText("Full screen")

    def fit(self) -> None:
        fit = getattr(self.canvas, "zoom_to_fit", None)
        if callable(fit):
            fit()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if obj is self and event.type() in (QEvent.Type.Resize,
                                            QEvent.Type.WindowStateChange,
                                            QEvent.Type.Show):
            # After the layout has settled, not during the event.
            QTimer.singleShot(0, self.fit)
        return False

    def mouseDoubleClickEvent(self, event: Any) -> None:  # noqa: N802
        self.toggle_full_screen()


def canvas_maker(canvas: Any) -> Callable[[QWidget], Any]:
    """Build new canvases of the same class as `canvas`, without the ROI bar
    (a box drawn on a copy would change nothing)."""
    cls = type(canvas)

    def make(parent: QWidget) -> Any:
        try:
            return cls(parent, roi=False)
        except TypeError:
            return cls(parent)
    return make


def pop_out(canvas: Any, title: str = "") -> Optional[PopOutWindow]:
    """Copy what `canvas` shows into a new window and show it. None if the
    canvas is empty."""
    image = current_image(canvas)
    if image is None:
        return None
    window = PopOutWindow(image, title, canvas_maker(canvas))
    window.show()
    window.raise_()
    return window
