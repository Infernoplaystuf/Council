"""
image_viewer.py — view a folder of images: zoom, pan, step through with a
slider or thumbnails, read pixel values, draw a region and save it.

WHAT IT DOES
Open an image or a folder (or drop one on the window). The wheel zooms
around the cursor, dragging pans, F fits the image to the window and 1
shows it at 100 %. The slider, the thumbnail strip and the Left/Right keys
step through the folder. The status bar reads out the pixel under the
mouse. Turn on "Draw region" and drag a rectangle: its size is shown and
"Save region…" writes that part of the image. Rotate and flip apply to the
view. The histogram at the side shows the red, green and blue levels.

SHOWS
  * QGraphicsView / QGraphicsScene / QGraphicsPixmapItem, zooming with
    wheelEvent and AnchorUnderMouse, panning with ScrollHandDrag
  * mapping view -> scene -> image coordinates (mapToScene, mapFromScene)
  * drawing a selection with mouse press/move/release into a movable
    QGraphicsRectItem, then QImage.copy(rect).save(...)
  * QTransform for rotate and flip
  * a custom QWidget whose paintEvent draws a histogram with QPainter and
    QPainterPath, the counts computed with numpy from QImage bits
  * a thumbnail strip: QListWidget in IconMode, filled a few at a time
    from a QTimer so a big folder does not freeze the window
  * QSlider and QListWidget kept in step without signal loops
  * QAction shortcuts (F, 1, Left, Right, F11 full screen), QToolBar,
    checkable actions, drag and drop of files

RUN
    python examples/qt_apps/image_viewer.py [image | folder]

REQUIRES  numpy
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

import numpy as np
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QAction, QColor, QIcon, QImage, QPainter,
                           QPainterPath, QPen, QPixmap, QTransform)
from PySide6.QtWidgets import (QApplication, QDockWidget, QFileDialog,
                               QGraphicsItem, QGraphicsPixmapItem,
                               QGraphicsRectItem, QGraphicsScene,
                               QGraphicsView, QLabel, QListWidget,
                               QListWidgetItem, QMainWindow, QSlider,
                               QVBoxLayout, QWidget)

SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp"}
THUMB = 96
THUMBS_PER_TICK = 8


def image_files(folder: Path) -> List[Path]:
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in SUFFIXES)


def rgb_histogram(img: QImage) -> np.ndarray:
    """3 x 256 counts (R, G, B)."""
    img = img.convertToFormat(QImage.Format_RGB32)
    w, h = img.width(), img.height()
    if not w or not h:
        return np.zeros((3, 256))
    arr = np.frombuffer(img.constBits(), np.uint8,
                        count=img.sizeInBytes()).reshape(h, img.bytesPerLine())
    px = arr[:, :w * 4].reshape(h, w, 4)            # B, G, R, A in memory
    return np.stack([np.bincount(px[..., c].ravel(), minlength=256)
                     for c in (2, 1, 0)])


class Histogram(QWidget):
    def __init__(self):
        super().__init__()
        self.counts = np.zeros((3, 256))
        self.setMinimumSize(200, 120)

    def set_image(self, img: QImage) -> None:
        self.counts = rgb_histogram(img)
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), self.palette().base())
        top = float(self.counts[:, 1:255].max() or 1)   # ignore clipped ends
        w, h = self.width(), self.height()
        for row, colour in zip(self.counts, ("#e04040", "#40b040", "#4060e0")):
            path = QPainterPath(QPointF(0, h))
            for i, n in enumerate(row):
                path.lineTo(i * w / 255, h - min(1.0, n / top) * (h - 4))
            path.lineTo(w, h)
            c = QColor(colour)
            p.setPen(QPen(c, 1))
            c.setAlpha(60)
            p.setBrush(c)
            p.drawPath(path)


class ImageView(QGraphicsView):
    """Zoom with the wheel; pan by dragging; draw a region when asked."""
    hovered = Signal(int, int)               # image pixel under the mouse
    region_changed = Signal(QRectF)

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.pix = QGraphicsPixmapItem()
        self.pix.setTransformationMode(Qt.SmoothTransformation)
        self.scene().addItem(self.pix)
        self.region: Optional[QGraphicsRectItem] = None
        self.drawing = False
        self._origin = QPointF()
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setMouseTracking(True)
        self.setBackgroundBrush(QColor(40, 40, 40))

    def set_image(self, img: QImage) -> None:
        self.pix.setPixmap(QPixmap.fromImage(img))
        self.pix.setTransformOriginPoint(self.pix.boundingRect().center())
        self.scene().setSceneRect(self.pix.sceneBoundingRect())

    def fit(self) -> None:
        self.fitInView(self.pix.sceneBoundingRect(), Qt.KeepAspectRatio)

    def actual_size(self) -> None:
        self.setTransform(QTransform())

    def wheelEvent(self, event) -> None:
        factor = 1.25 if event.angleDelta().y() > 0 else 0.8
        self.scale(factor, factor)

    def set_draw_mode(self, on: bool) -> None:
        self.drawing = on
        self.setDragMode(QGraphicsView.NoDrag if on
                         else QGraphicsView.ScrollHandDrag)

    def mousePressEvent(self, event) -> None:
        if self.drawing and event.button() == Qt.LeftButton:
            self._origin = self.mapToScene(event.position().toPoint())
            if self.region is None:
                self.region = QGraphicsRectItem()
                self.region.setPen(QPen(QColor("#ffcc00"), 0, Qt.DashLine))
                self.region.setBrush(QColor(255, 204, 0, 40))
                self.region.setFlag(QGraphicsItem.ItemIsMovable)
                self.scene().addItem(self.region)
            self.region.setPos(0, 0)
            self.region.setRect(QRectF(self._origin, self._origin))
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        scene = self.mapToScene(event.position().toPoint())
        local = self.pix.mapFromScene(scene)
        self.hovered.emit(int(local.x()), int(local.y()))
        if self.drawing and event.buttons() & Qt.LeftButton and self.region:
            self.region.setRect(QRectF(self._origin, scene).normalized())
            self.region_changed.emit(self.region_in_image())
            return
        super().mouseMoveEvent(event)

    def region_in_image(self) -> QRectF:
        """The region in image pixels, clipped to the image."""
        if self.region is None:
            return QRectF()
        scene_rect = self.region.mapRectToScene(self.region.rect())
        r = self.pix.mapRectFromScene(scene_rect)
        return r.intersected(self.pix.boundingRect())


class ViewerWindow(QMainWindow):
    def __init__(self, start: Optional[Path] = None):
        super().__init__()
        self.setWindowTitle("Image viewer")
        self.resize(1100, 720)
        self.setAcceptDrops(True)
        self.files: List[Path] = []
        self.image = QImage()
        self._pending: List[int] = []

        self.view = ImageView()
        self.view.hovered.connect(self._hover)
        self.view.region_changed.connect(
            lambda r: self.region_label.setText(
                f"Region {int(r.width())} x {int(r.height())} at "
                f"({int(r.x())}, {int(r.y())})"))
        central = QWidget()
        v = QVBoxLayout(central)
        v.addWidget(self.view, 1)
        self.slider = QSlider(Qt.Horizontal, enabled=False)
        self.slider.valueChanged.connect(self.show_index)
        v.addWidget(self.slider)
        self.setCentralWidget(central)

        self.thumbs = QListWidget(viewMode=QListWidget.IconMode,
                                  flow=QListWidget.LeftToRight,
                                  wrapping=False, movement=QListWidget.Static)
        self.thumbs.setIconSize(QSize(THUMB, THUMB))
        self.thumbs.setFixedHeight(THUMB + 36)
        self.thumbs.currentRowChanged.connect(self.slider.setValue)
        dock = QDockWidget("Thumbnails", self)
        dock.setObjectName("thumbs")
        dock.setWidget(self.thumbs)
        self.addDockWidget(Qt.BottomDockWidgetArea, dock)

        side = QWidget()
        sv = QVBoxLayout(side)
        self.hist = Histogram()
        sv.addWidget(self.hist)
        self.info = QLabel(wordWrap=True)
        sv.addWidget(self.info)
        self.region_label = QLabel("No region")
        sv.addWidget(self.region_label)
        sv.addStretch(1)
        sdock = QDockWidget("Details", self)
        sdock.setObjectName("details")
        sdock.setWidget(side)
        self.addDockWidget(Qt.RightDockWidgetArea, sdock)

        self.thumb_timer = QTimer(self, interval=0)
        self.thumb_timer.timeout.connect(self._next_thumbs)
        self._actions()
        if start:
            self.open_path(start)

    def _actions(self) -> None:
        tb = self.addToolBar("View")
        tb.setObjectName("view_toolbar")
        menu = self.menuBar().addMenu("&Image")

        def add(text, slot, key=None, checkable=False) -> QAction:
            a = QAction(text, self, checkable=checkable)
            if key:
                a.setShortcut(key)
            (a.toggled if checkable else a.triggered).connect(slot)
            tb.addAction(a)
            menu.addAction(a)
            return a

        add("Open…", self.open_dialog, "Ctrl+O")
        add("Open folder…", self.open_folder, "Ctrl+Shift+O")
        tb.addSeparator()
        add("Fit", self.view.fit, "F")
        add("100%", self.view.actual_size, "1")
        add("Rotate", lambda: self._transform(rotate=90), "R")
        add("Flip", lambda: self._transform(flip=True), "H")
        tb.addSeparator()
        add("Draw region", self.view.set_draw_mode, "D", checkable=True)
        add("Save region…", self.save_region, "Ctrl+S")
        add("Previous", lambda: self.step(-1), Qt.Key_Left)
        add("Next", lambda: self.step(1), Qt.Key_Right)
        full = add("Full screen", self._full_screen, "F11", checkable=True)
        full.setVisible(False)                 # shortcut only

    # ---------------------------------------------------------- opening
    def open_dialog(self) -> None:
        name, _ = QFileDialog.getOpenFileName(
            self, "Open image", "",
            "Images (" + " ".join("*" + s for s in sorted(SUFFIXES)) + ")")
        if name:
            self.open_path(Path(name))

    def open_folder(self) -> None:
        name = QFileDialog.getExistingDirectory(self, "Folder of images")
        if name:
            self.open_path(Path(name))

    def open_path(self, path: Path) -> None:
        folder = path if path.is_dir() else path.parent
        self.files = image_files(folder)
        if not self.files:
            self.statusBar().showMessage(f"No images in {folder}")
            return
        self.thumbs.clear()
        for p in self.files:
            item = QListWidgetItem(p.name)
            item.setToolTip(str(p))
            self.thumbs.addItem(item)
        self._pending = list(range(len(self.files)))
        self.thumb_timer.start()
        self.slider.blockSignals(True)
        self.slider.setRange(0, len(self.files) - 1)
        self.slider.setEnabled(len(self.files) > 1)
        self.slider.blockSignals(False)
        first = self.files.index(path) if path in self.files else 0
        self.show_index(first)

    def _next_thumbs(self) -> None:
        """A few thumbnails per tick, so the window keeps responding."""
        for _ in range(THUMBS_PER_TICK):
            if not self._pending:
                self.thumb_timer.stop()
                return
            i = self._pending.pop(0)
            img = QImage(str(self.files[i]))
            if not img.isNull():
                self.thumbs.item(i).setIcon(QIcon(QPixmap.fromImage(
                    img.scaled(THUMB, THUMB, Qt.KeepAspectRatio,
                               Qt.SmoothTransformation))))

    def show_index(self, i: int) -> None:
        if not 0 <= i < len(self.files):
            return
        path = self.files[i]
        img = QImage(str(path))
        if img.isNull():
            self.statusBar().showMessage(f"Could not read {path.name}")
            return
        self.image = img
        self.view.set_image(img)
        self.view.fit()
        self.hist.set_image(img)
        self.info.setText(f"<b>{path.name}</b><br>{img.width()} x "
                          f"{img.height()}, {img.depth()}-bit<br>"
                          f"{i + 1} of {len(self.files)}")
        for w, v in ((self.slider, i), (self.thumbs, i)):
            w.blockSignals(True)
            (w.setValue if w is self.slider else w.setCurrentRow)(v)
            w.blockSignals(False)
        self.setWindowTitle(f"{path.name} — Image viewer")

    def step(self, delta: int) -> None:
        if self.files:
            self.show_index((self.slider.value() + delta) % len(self.files))

    # ---------------------------------------------------------- tools
    def _hover(self, x: int, y: int) -> None:
        if self.image.valid(x, y):
            c = self.image.pixelColor(x, y)
            self.statusBar().showMessage(
                f"x {x}  y {y}   R {c.red()}  G {c.green()}  B {c.blue()}")

    def _transform(self, rotate: int = 0, flip: bool = False) -> None:
        item = self.view.pix
        t = item.transform()
        if flip:
            t = QTransform(-1, 0, 0, 1, item.boundingRect().width(), 0) * t
        item.setTransform(t)
        item.setRotation((item.rotation() + rotate) % 360)
        self.view.scene().setSceneRect(item.sceneBoundingRect())
        self.view.fit()

    def save_region(self) -> None:
        r = self.view.region_in_image().toRect()
        if r.isEmpty():
            self.statusBar().showMessage("Draw a region first (D).")
            return
        name, _ = QFileDialog.getSaveFileName(self, "Save region",
                                              "region.png", "PNG (*.png)")
        if name and self.image.copy(r).save(name):
            self.statusBar().showMessage(f"Saved {r.width()} x {r.height()} "
                                         f"to {name}")

    def _full_screen(self, on: bool) -> None:
        self.showFullScreen() if on else self.showNormal()

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        urls = event.mimeData().urls()
        if urls:
            self.open_path(Path(urls[0].toLocalFile()))


def make_window(argv: Optional[List[str]] = None) -> ViewerWindow:
    args = argv if argv is not None else sys.argv[1:]
    return ViewerWindow(Path(args[0]) if args else None)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    win = make_window()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
