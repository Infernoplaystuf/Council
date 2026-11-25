"""
typhon.event_analysis's window: the script's six tabs (their boxes from
forms.py), the calibration tab's picture that dots are clicked on, the
picture the running analysis is making, and the processing log.

Qt widgets only (QtCore, QtGui, QtWidgets: design 3.11's rule for panels).
The work runs on the plugin's thread; this window polls it on a timer.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPlainTextEdit, QPushButton,
                               QScrollArea, QSizePolicy, QSpinBox, QSplitter,
                               QTabWidget, QVBoxLayout, QWidget)

from . import forms

POLL_MS = 100
INTRO = ("Analyse an event recording (.raw, .dat or .npz of events) or a "
         "video: Andy's six Prophesee tabs. Outputs go to the folder you "
         "pick (left empty: '<recording>_analysis' beside it) and never "
         "overwrite a file.")


def _no_dialogs() -> bool:
    return bool(os.environ.get("COUNCIL_NO_DIALOGS"))


def _pixmap(rgb: Tuple[int, int, bytes]) -> QPixmap:
    w, h, data = rgb
    image = QImage(data, w, h, 3 * w, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(image.copy())


class Boxes(QScrollArea):
    """A tab's boxes, scrolled: asks for little room, so the window fits
    Typhon's smallest screen; the boxes scroll inside it."""

    def sizeHint(self) -> QSize:                           # noqa: N802
        return QSize(520, 240)


class DotCanvas(QLabel):
    """The calibration picture: left click labels a dot, right click
    removes the nearest. Keeps the image size to map clicks back."""

    def __init__(self, panel: "EventAnalysisPanel") -> None:
        super().__init__(panel)
        self.panel = panel
        self.setObjectName("event_analysis_canvas")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumHeight(160)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self.setStyleSheet("background: black; color: #bbb;")
        self.setText("Scan a calibration recording, then click known dots.")
        self.image_size: Optional[Tuple[int, int]] = None
        self._rgb: Optional[Tuple[int, int, bytes]] = None

    def show_rgb(self, rgb: Tuple[int, int, bytes],
                 image_size: Tuple[int, int]) -> None:
        self._rgb = rgb
        self.image_size = image_size
        self._fit()

    def _fit(self) -> None:
        if self._rgb is None:
            return
        pix = _pixmap(self._rgb)
        self.setPixmap(pix.scaled(self.width(), self.height(),
                                  Qt.AspectRatioMode.KeepAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, event: Any) -> None:            # noqa: N802
        super().resizeEvent(event)
        self._fit()

    def to_image(self, x: float, y: float) -> Optional[Tuple[float, float, float]]:
        """Widget (x, y) -> image (x, y, image px per widget px)."""
        if self.image_size is None:
            return None
        iw, ih = self.image_size
        scale = min(self.width() / iw, self.height() / ih)
        if scale <= 0:
            return None
        x0 = (self.width() - iw * scale) / 2
        y0 = (self.height() - ih * scale) / 2
        return (x - x0) / scale, (y - y0) / scale, 1 / scale

    def mousePressEvent(self, event: Any) -> None:        # noqa: N802
        pos = event.position()
        got = self.to_image(pos.x(), pos.y())
        if got is None:
            return
        x, y, per = got
        if event.button() == Qt.MouseButton.RightButton:
            self.panel.remove_dot(x, y, 25 * per)
        else:
            self.panel.add_dot(x, y)


class EventAnalysisPanel(QWidget):
    def __init__(self, plugin: Any, parent: Any = None) -> None:
        super().__init__(parent)
        self.setObjectName("event_analysis_panel")
        self.plugin = plugin
        self.boxes: Dict[str, Any] = {}
        self.starts: Dict[str, QPushButton] = {}
        self._seen = 0
        self._preview_n = -1
        self._job: Any = None
        values = plugin.values()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        intro = QLabel(INTRO, self)
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.problem = QLabel("", self)
        self.problem.setWordWrap(True)
        self.problem.setStyleSheet("color: #c0392b;")
        layout.addWidget(self.problem)

        split = QSplitter(Qt.Orientation.Vertical, self)
        self.tabs = QTabWidget(split)
        self.tabs.setObjectName("event_analysis_tabs")
        for tab in forms.TABS:
            self.tabs.addTab(self._build_tab(tab, values), tab.title)
        below = QWidget(split)
        low = QHBoxLayout(below)
        low.setContentsMargins(0, 0, 0, 0)
        self.preview = QLabel("The picture being made shows here.", below)
        self.preview.setObjectName("event_analysis_preview")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(240, 160)
        self.preview.setStyleSheet("background: black; color: #bbb;")
        low.addWidget(self.preview, 1)
        logbox = QGroupBox("Processing log", below)
        lg = QVBoxLayout(logbox)
        self.log = QPlainTextEdit(logbox)
        self.log.setObjectName("event_analysis_log")
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        lg.addWidget(self.log)
        low.addWidget(logbox, 1)
        split.addWidget(self.tabs)
        split.addWidget(below)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        layout.addWidget(split, 1)

        self.timer = QTimer(self)
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        why = plugin.missing()
        self.problem.setText(why)
        self.problem.setVisible(bool(why))

    # -- building -----------------------------------------------------
    def _build_tab(self, tab: forms.Tab, values: Dict[str, Any]) -> QWidget:
        page = QWidget()
        page.setObjectName(f"event_analysis_tab_{tab.key}")
        outer = QVBoxLayout(page)
        scroll = Boxes(page)
        scroll.setWidgetResizable(True)
        inner = QWidget()
        col = QVBoxLayout(inner)
        heading = QLabel(tab.heading, inner)
        heading.setWordWrap(True)
        col.addWidget(heading)
        for group in tab.groups:
            holder: QWidget
            if group.title:
                holder = QGroupBox(group.title, inner)
            else:
                holder = QWidget(inner)
            form = QFormLayout(holder)
            for f in group.fields:
                widget = self._box(f, values.get(f.key, f.default), holder)
                if f.kind == 'check':
                    form.addRow(widget)
                else:
                    form.addRow(f.label, widget)
            if group.note:
                note = QLabel(group.note, holder)
                note.setWordWrap(True)
                form.addRow(note)
            col.addWidget(holder)
        if tab.key == 'cal':
            self._calibration_rows(col, inner)
        else:
            row = QHBoxLayout()
            start = QPushButton(tab.start, inner)
            start.setObjectName(f"btn_event_analysis_{tab.key}")
            start.clicked.connect(lambda _=False, k=tab.key: self.start(k))
            self.starts[tab.key] = start
            row.addWidget(start)
            stop = QPushButton("Stop processing", inner)
            stop.clicked.connect(self.plugin.stop)
            row.addWidget(stop)
            row.addStretch(1)
            col.addLayout(row)
            col.addStretch(1)
        scroll.setWidget(inner)
        if tab.key == 'cal':
            # the boxes above the picture the dots are clicked on, the
            # line between them dragged to taste
            parts = QSplitter(Qt.Orientation.Vertical, page)
            parts.addWidget(scroll)
            parts.addWidget(self.canvas)
            parts.setStretchFactor(0, 1)
            parts.setStretchFactor(1, 2)
            outer.addWidget(parts, 1)
            outer.addWidget(self.cal_status)
        else:
            outer.addWidget(scroll, 1)
        return page

    def _box(self, f: forms.Field, value: Any, parent: QWidget) -> QWidget:
        if f.kind == 'check':
            box = QCheckBox(f.label, parent)
            box.setChecked(bool(value))
            self.boxes[f.key] = box
            return box
        if f.kind == 'choice':
            combo = QComboBox(parent)
            combo.addItems(list(f.choices))
            if str(value) in f.choices:
                combo.setCurrentText(str(value))
            self.boxes[f.key] = combo
            if f.key == 'cal_view':
                combo.currentTextChanged.connect(lambda _t: self.redraw())
            return combo
        edit = QLineEdit(str(value), parent)
        edit.setObjectName(f"event_analysis_{f.key}")
        self.boxes[f.key] = edit
        if f.kind == 'text':
            return edit
        row = QWidget(parent)
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit, 1)
        browse = QPushButton("Browse", row)
        browse.clicked.connect(lambda _=False, k=f.key, kind=f.kind:
                               self.browse(k, kind))
        h.addWidget(browse)
        if f.kind == 'recording':
            latest = QPushButton("Latest recording", row)
            latest.setToolTip("The newest recording in Typhon's capture "
                              "folder (an EVK4 run's .raw, a video ...)")
            latest.clicked.connect(lambda _=False, k=f.key:
                                   self.use_latest(k))
            h.addWidget(latest)
        return row

    def _calibration_rows(self, col: QVBoxLayout, inner: QWidget) -> None:
        actions = QHBoxLayout()
        start = QPushButton(forms.CALIBRATION.start, inner)
        start.setObjectName("btn_event_analysis_cal")
        start.clicked.connect(lambda: self.start('cal'))
        self.starts['cal'] = start
        actions.addWidget(start)
        detect = QPushButton("Detect candidate dots", inner)
        detect.clicked.connect(self.detect_dots)
        actions.addWidget(detect)
        stop = QPushButton("Stop", inner)
        stop.clicked.connect(self.plugin.stop)
        actions.addWidget(stop)
        actions.addStretch(1)
        col.addLayout(actions)

        labels = QHBoxLayout()
        labels.addWidget(QLabel("Next dot row", inner))
        self.next_row = QSpinBox(inner)
        self.next_row.setRange(0, 999)
        labels.addWidget(self.next_row)
        labels.addWidget(QLabel("column", inner))
        self.next_col = QSpinBox(inner)
        self.next_col.setRange(0, 999)
        labels.addWidget(self.next_col)
        self.advance = QCheckBox("Auto-advance", inner)
        self.advance.setChecked(True)
        labels.addWidget(self.advance)
        self.snap_on = QCheckBox("Snap detected dots", inner)
        labels.addWidget(self.snap_on)
        labels.addStretch(1)
        col.addLayout(labels)

        bar = QHBoxLayout()
        for text, slot in (("Undo dot", self.undo),
                           ("Fit homography", self.fit_points),
                           ("Auto-match visible dots", self.match_points),
                           ("Save JSON", self.save_calibration)):
            b = QPushButton(text, inner)
            b.clicked.connect(lambda _=False, s=slot: s())
            bar.addWidget(b)
        bar.addStretch(1)
        col.addLayout(bar)
        self.canvas = DotCanvas(self)
        self.cal_status = QLabel("Scan calibration recording, then click "
                                 "known dots.", self)
        self.cal_status.setWordWrap(True)

    # -- the boxes ----------------------------------------------------
    def values(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key, box in self.boxes.items():
            if isinstance(box, QCheckBox):
                out[key] = box.isChecked()
            elif isinstance(box, QComboBox):
                out[key] = box.currentText()
            else:
                out[key] = box.text()
        return out

    def set_value(self, key: str, value: Any) -> None:
        box = self.boxes[key]
        if isinstance(box, QCheckBox):
            box.setChecked(bool(value))
        elif isinstance(box, QComboBox):
            box.setCurrentText(str(value))
        else:
            box.setText(str(value))

    def browse(self, key: str, kind: str) -> None:
        if _no_dialogs():
            return
        start = self.boxes[key].text() or str(
            self.plugin.capture_folder() or "")
        if kind == 'folder':
            got = QFileDialog.getExistingDirectory(self, "Choose a folder",
                                                   start)
        elif kind == 'save':
            got, _ = QFileDialog.getSaveFileName(self, "Output MP4", start,
                                                 "MP4 (*.mp4)")
        elif kind == 'json':
            got, _ = QFileDialog.getOpenFileName(self, "Homography JSON",
                                                 start, "JSON (*.json)")
        elif kind == 'csv':
            got, _ = QFileDialog.getOpenFileName(self, "Layer trigger CSV",
                                                 start, "CSV (*.csv)")
        else:
            got, _ = QFileDialog.getOpenFileName(self, "Recording", start,
                                                 forms.RECORDING_FILTER)
        if got:
            self.boxes[key].setText(got)

    def use_latest(self, key: str) -> str:
        events_only = key in ('vel_input', 'melt_input')
        found = self.plugin.latest_recording(events_only=events_only)
        if found is None:
            said = "No recording found in Typhon's capture folder."
        else:
            self.boxes[key].setText(str(found))
            said = f"Latest recording: {found}"
        self.say(said)
        return said

    # -- running ------------------------------------------------------
    def say(self, text: str) -> None:
        self.log.appendPlainText(text)

    def refuse(self, title: str, text: str) -> None:
        self.say(f"{title}: {text}")
        if not _no_dialogs():
            QMessageBox.warning(self, title, text)

    def start(self, tab: str) -> str:
        job, why = self.plugin.start(tab, self.values())
        if job is None:
            self.refuse(forms.tab(tab).title, why)
            return why
        self._job = job
        self._seen = 0
        self._preview_n = -1
        self.say(f"Started: {forms.tab(tab).title}")
        for b in self.starts.values():
            b.setEnabled(False)
        return ''

    def poll(self) -> None:
        job = self._job
        if job is None:
            return
        for line in job.new_lines(self._seen):
            self.say(line)
        self._seen = job.logged
        if job.preview is not None and job.preview_n != self._preview_n:
            self._preview_n = job.preview_n
            pix = _pixmap(job.preview)
            self.preview.setPixmap(pix.scaled(
                self.preview.width(), self.preview.height(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        if job.done.is_set():
            self._job = None
            for b in self.starts.values():
                b.setEnabled(True)
            if job.error:
                self.say(f"Stopped with an error: {job.error}")
            elif job.stop.is_set():
                self.say("Stopped.")
            else:
                self.say("Finished.")
            if job.tab == 'cal' and not job.error and job.result:
                self.calibration_scanned(job.result)

    # -- the calibration tab --------------------------------------------
    def _grid(self) -> Optional[Tuple[int, int, float]]:
        try:
            return forms.grid(self.values())
        except ValueError as exc:
            self.refuse("Calibration settings", str(exc))
            return None

    def calibration_scanned(self, result: Dict[str, Any]) -> None:
        dots = self.plugin.calibration()
        dots.scanned(result)
        self._sync_next()
        self.cal_status.setText(
            f'Scan complete: {result["samples"]:,} slices/frames. Click dots '
            f'with known row/column.')
        self.detect_dots()

    def detect_dots(self) -> None:
        dots = self.plugin.calibration()
        v = self.values()
        try:
            n = dots.detect(v['cal_view'], float(v['blob_min']),
                            float(v['blob_max']))
        except ValueError as exc:
            self.refuse("Dot detection", str(exc))
            return
        if dots.recording is not None:
            self.say(f"Candidate dot centers: {n}")
        self.redraw()

    def redraw(self) -> None:
        dots = self.plugin.calibration()
        if dots.recording is None:
            return
        g = self._grid()
        if g is None:
            return
        picture = dots.picture(self.values()['cal_view'], *g)
        if picture is None:
            return
        rgb = self.plugin.picture_bytes(picture)
        self.canvas.show_rgb(rgb, (dots.recording['width'],
                                   dots.recording['height']))

    def _sync_next(self) -> None:
        dots = self.plugin.calibration()
        self.next_row.setValue(int(dots.next_row))
        self.next_col.setValue(int(dots.next_col))

    def add_dot(self, x: float, y: float) -> None:
        dots = self.plugin.calibration()
        g = self._grid()
        if g is None or dots.recording is None:
            return
        dots.next_row, dots.next_col = self.next_row.value(), \
            self.next_col.value()
        v = self.values()
        try:
            added = dots.add(x, y, g[0], g[1], snap=self.snap_on.isChecked(),
                             snap_radius=float(v['snap']),
                             advance=self.advance.isChecked())
        except ValueError as exc:
            self.refuse("Dot selection", str(exc))
            return
        if added:
            self._sync_next()
            self.cal_status.setText(
                f'{len(dots.points)} dots labeled; next row '
                f'{dots.next_row}, column {dots.next_col}')
            self.redraw()

    def remove_dot(self, x: float, y: float, radius: float) -> None:
        dots = self.plugin.calibration()
        if dots.remove_near(x, y, radius):
            self._sync_next()
            self.redraw()

    def undo(self) -> None:
        dots = self.plugin.calibration()
        dots.undo()
        self._sync_next()
        self.redraw()

    def fit_points(self) -> bool:
        dots = self.plugin.calibration()
        g = self._grid()
        if g is None:
            return False
        try:
            fit = dots.fit_points(g[2])
        except ValueError as exc:
            self.refuse("Homography fitting", str(exc))
            self.redraw()
            return False
        self.cal_status.setText(
            f'Fit: {len(dots.points)} dots, {fit["inliers"]} inliers, '
            f'RMS {fit["rms"]:.4f} mm')
        self.redraw()
        return True

    def match_points(self) -> None:
        dots = self.plugin.calibration()
        g = self._grid()
        if g is None:
            return
        if dots.fit is None and not self.fit_points():
            return
        try:
            count = dots.match(g[0], g[1], g[2], float(self.values()['snap']))
        except ValueError as exc:
            self.refuse("Auto-match", str(exc))
            return
        self.say(f'Added {count} automatically indexed dots; check the '
                 f'magenta grid overlay.')
        self.redraw()

    def save_calibration(self, path: str = "") -> str:
        """Ask where (unless given), then write the JSON and its preview;
        every tab's JSON box then names it. The path, or ''."""
        dots = self.plugin.calibration()
        if dots.recording is None:
            self.refuse("Calibration", "Scan a calibration recording first.")
            return ""
        if dots.fit is None and not self.fit_points():
            return ""
        if not path:
            if _no_dialogs():
                return ""
            if QMessageBox.question(
                    self, "Verify", "Are the grid labels and magenta "
                    "predicted dots correct?") != \
                    QMessageBox.StandardButton.Yes:
                return ""
            start = os.path.join(os.path.dirname(
                self.values().get('cal_input', '')), 'EVK4_Homography.json')
            path, _ = QFileDialog.getSaveFileName(self, "Save homography",
                                                  start, "JSON (*.json)")
            if not path:
                return ""
        try:
            saved, preview = self.plugin.save_calibration(path, self.values())
        except (ValueError, OSError) as exc:
            self.refuse("Calibration validation", str(exc))
            return ""
        for key in forms.JSON_KEYS:
            self.set_value(key, saved)
        self.say(f"Calibration saved: {saved} (preview {preview}); every "
                 f"tab now uses this JSON.")
        return saved

    # -- shown and hidden -----------------------------------------------
    def showEvent(self, event: Any) -> None:              # noqa: N802
        super().showEvent(event)
        self.timer.start()
