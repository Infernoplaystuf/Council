"""
council_qt.tabs.ledger — the 📑 Ledger tab: template + source workbooks -> review
workbook -> parent CSV, without the command line.

The four steps, top to bottom, are the pipeline's own order:

  1. Template -> create (or extend) the parent CSV, or point at an existing CSV.
  2. Source workbooks: files and folders.
  3. Extract -> a review workbook, opened in Excel for checking.
  4. Apply the saved review -> the parent CSV, and optionally one filled
     template per part.

Every step runs on a worker thread (a workbook of many pictures takes a while) and the buttons are
disabled until it finishes; the summary line and the log say what it found.
Everything stays on this machine: the work is council_core/ledger_ingest.py,
and the pipeline itself sample_ledger.py and source_extract.py.
"""
from __future__ import annotations

import threading
import traceback
from pathlib import Path
from typing import Callable, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QGridLayout,
                               QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout,
                               QWidget)

from council_core import ledger_ingest
from sample_ledger import LedgerError

from .. import theme
from ..view import ViewHelpers, amp

XLSX = "Excel workbooks (*.xlsx)"


class LedgerActions:
    """The tab's seam to the pipeline and the desktop: tests replace pieces
    (a stand-in image reader, no Excel, no file dialogs)."""

    def __init__(self, image_reader=None, opener: Optional[Callable] = None):
        self.image_reader = image_reader
        self.opener = opener or ledger_ingest.open_path

    def create_parent(self, template, csv):
        return ledger_ingest.create_parent(template, csv or None)

    def extract(self, csv, sources, part, use_images, progress):
        return ledger_ingest.run_extract(csv, sources, part=part, use_images=use_images,
                                         image_reader=self.image_reader, progress=progress)

    def apply(self, review, fill_dir, use_images, progress):
        return ledger_ingest.run_apply(review, fill_dir or None, use_images=use_images,
                                       image_reader=self.image_reader, progress=progress)

    def open(self, path) -> None:
        self.opener(path)


class LedgerTab(ViewHelpers, QWidget):
    def __init__(self, window=None, actions: Optional[LedgerActions] = None):
        super().__init__()
        self.window = window
        self.bridge = getattr(window, "bridge", None)
        self.actions = actions or LedgerActions()
        self._tokens = theme.tokens("dark")
        self._worker: Optional[threading.Thread] = None
        self._busy_buttons: List[QPushButton] = []
        self._build()
        self._refresh_enabled()

    # ── layout ──────────────────────────────────────────────────────────
    def _step_button(self, layout, text: str, slot) -> QPushButton:
        """ViewHelpers' button, locked while a step runs."""
        b = self._button(layout, text, slot)
        self._busy_buttons.append(b)
        return b

    def _path_row(self, grid: QGridLayout, row: int, label: str, browse) -> QLineEdit:
        grid.addWidget(QLabel(amp(label)), row, 0)
        edit = QLineEdit()
        edit.textChanged.connect(self._refresh_enabled)
        grid.addWidget(edit, row, 1)
        b = QPushButton("Browse…")
        b.clicked.connect(browse)
        grid.addWidget(b, row, 2)
        return edit

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 8, 10, 8)

        g1 = QGroupBox("1 · Template and parent CSV")
        grid = QGridLayout(g1)
        self.template = self._path_row(grid, 0, "Template workbook:", self.browse_template)
        self.csv = self._path_row(grid, 1, "Parent CSV:", self.browse_csv)
        self.csv.setPlaceholderText("made beside the template if left empty; "
                                    "or pick an existing parent CSV")
        row = QHBoxLayout()
        self.create_btn = self._step_button(row, "Create / extend parent CSV", self.on_create)
        row.addStretch(1)
        grid.addLayout(row, 2, 1)
        outer.addWidget(g1)

        g2 = QGroupBox("2 · Source workbooks")
        v2 = QVBoxLayout(g2)
        self.sources = QListWidget()
        self.sources.setMaximumHeight(120)
        v2.addWidget(self.sources)
        row = QHBoxLayout()
        self._step_button(row, "Add files…", self.browse_sources)
        self._step_button(row, "Add folder…", self.browse_folder)
        self._step_button(row, "Remove selected", self.remove_selected)
        self._step_button(row, "Clear", self.clear_sources)
        row.addWidget(QLabel("Part number (if not in them):"))
        self.part = QLineEdit()
        self.part.setFixedWidth(120)
        row.addWidget(self.part)
        self.use_images = QCheckBox("Read pasted images (Tesseract, local)")
        self.use_images.setChecked(True)
        row.addWidget(self.use_images)
        row.addStretch(1)
        v2.addLayout(row)
        outer.addWidget(g2)

        g3 = QGroupBox("3 · Extract and review")
        grid3 = QGridLayout(g3)
        self.review = self._path_row(grid3, 0, "Review workbook:", self.browse_review)
        row = QHBoxLayout()
        self.extract_btn = self._step_button(row, "Extract → review workbook", self.on_extract)
        self.open_btn = self._step_button(row, "Open review in Excel", self.on_open_review)
        row.addStretch(1)
        grid3.addLayout(row, 1, 1)
        outer.addWidget(g3)

        g4 = QGroupBox("4 · Apply the saved review")
        grid4 = QGridLayout(g4)
        self.fill_dir = self._path_row(grid4, 0, "Filled templates folder (optional):",
                                       self.browse_fill_dir)
        row = QHBoxLayout()
        self.apply_btn = self._step_button(row, "Apply review → parent CSV", self.on_apply)
        row.addStretch(1)
        grid4.addLayout(row, 1, 1)
        outer.addWidget(g4)

        g5 = QGroupBox("Summary")
        v5 = QVBoxLayout(g5)
        nums = QHBoxLayout()
        self.stat = {}
        for key, title in [("values", "Values found"), ("pre_accepted", "Pre-accepted"),
                           ("to_review", "To review"), ("images", "Images read"),
                           ("problems", "Problems")]:
            box = QVBoxLayout()
            n = QLabel("–")
            n.setStyleSheet("font-size: 18px; font-weight: bold;")
            n.setAlignment(Qt.AlignmentFlag.AlignCenter)
            t = QLabel(title)
            t.setAlignment(Qt.AlignmentFlag.AlignCenter)
            box.addWidget(n)
            box.addWidget(t)
            nums.addLayout(box)
            self.stat[key] = n
        v5.addLayout(nums)
        self.status = QLabel("Pick a template, or an existing parent CSV.")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {self._tokens['muted_fg']};")
        v5.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)               # busy, no percentage
        self.progress.setVisible(False)
        v5.addWidget(self.progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        v5.addWidget(self.log, 1)
        outer.addWidget(g5, 1)

    # ── state ───────────────────────────────────────────────────────────
    @property
    def busy(self) -> bool:
        return self._worker is not None

    def source_paths(self) -> List[str]:
        return [self.sources.item(i).text() for i in range(self.sources.count())]

    def _refresh_enabled(self) -> None:
        if not hasattr(self, "progress"):
            return                                  # still building
        idle = not self.busy
        for b in self._busy_buttons:
            b.setEnabled(idle)
        if not idle:
            return
        csv = self.csv.text().strip()
        self.create_btn.setEnabled(bool(self.template.text().strip()))
        self.extract_btn.setEnabled(bool(csv) and bool(self.source_paths()))
        has_review = bool(self.review.text().strip())
        self.open_btn.setEnabled(has_review)
        self.apply_btn.setEnabled(has_review)

    def _say(self, text: str) -> None:
        self.log.appendPlainText(text)

    def _run(self, title: str, step: Callable, done: Callable) -> None:
        """Run `step(progress)` on a thread; `done(result)` on the GUI thread.
        A LedgerError is the pipeline's own plain-language message; anything
        else is shown with its traceback in the log."""
        if self.busy:
            return
        self.status.setText(f"{title}…")
        self._say(f"── {title}")
        self.progress.setVisible(True)

        def progress(msg: str) -> None:
            self._to_ui(lambda: self.status.setText(f"{title}: {msg}"))

        def work() -> None:
            try:
                result, err = step(progress), None
            except LedgerError as e:
                result, err = None, f"error: {e}"
            except Exception as e:                        # noqa: BLE001
                result, err = None, f"failed: {e!r}\n{traceback.format_exc()}"
            self._to_ui(lambda: self._finish(title, result, err, done))

        self._worker = threading.Thread(target=work, name="ledger-step", daemon=True)
        self._refresh_enabled()
        self._worker.start()

    def _finish(self, title, result, err, done) -> None:
        self._worker = None
        self.progress.setVisible(False)
        if err:
            self.status.setText(err.splitlines()[0])
            self._say(err)
        else:
            done(result)
        self._refresh_enabled()

    def wait(self, timeout: float = 60.0) -> bool:
        """Block until the current step's result is shown (tests, scripts)."""
        import time
        end = time.monotonic() + timeout
        while self.busy and time.monotonic() < end:
            QApplication.processEvents()
            time.sleep(0.01)
        QApplication.processEvents()
        return not self.busy

    # ── step 1 ──────────────────────────────────────────────────────────
    def on_create(self) -> None:
        template, csv = self.template.text().strip(), self.csv.text().strip()

        def done(s) -> None:
            self.csv.setText(s.csv)
            self.status.setText(s.describe().splitlines()[0])
            self._say(s.describe())
        self._run("Reading the template",
                  lambda _p: self.actions.create_parent(template, csv), done)

    # ── step 2 ──────────────────────────────────────────────────────────
    def add_sources(self, paths) -> None:
        have = set(self.source_paths())
        for p in paths:
            p = str(p)
            if p and p not in have:
                self.sources.addItem(p)
                have.add(p)
        self._refresh_enabled()

    def remove_selected(self) -> None:
        for item in self.sources.selectedItems():
            self.sources.takeItem(self.sources.row(item))
        self._refresh_enabled()

    def clear_sources(self) -> None:
        self.sources.clear()
        self._refresh_enabled()

    # ── step 3 ──────────────────────────────────────────────────────────
    def on_extract(self) -> None:
        csv, sources = self.csv.text().strip(), self.source_paths()
        part, images = self.part.text().strip(), self.use_images.isChecked()

        def done(s) -> None:
            self.review.setText(s.review)
            self.stat["values"].setText(str(s.values))
            self.stat["pre_accepted"].setText(str(s.pre_accepted))
            self.stat["to_review"].setText(str(s.to_review))
            self.stat["images"].setText(f"{s.images_read} / {s.images}")
            self.stat["problems"].setText(str(len(s.problems)))
            self.status.setText("Review written. Open it, check the amber and red rows, "
                                "map the leftover labels, save it, then Apply.")
            self._say(s.describe())
        self._run("Extracting",
                  lambda p: self.actions.extract(csv, sources, part, images, p), done)

    def on_open_review(self) -> None:
        path = self.review.text().strip()
        try:
            self.actions.open(path)
        except Exception as e:                            # noqa: BLE001
            self.status.setText(f"could not open {path}: {e}")

    # ── step 4 ──────────────────────────────────────────────────────────
    def on_apply(self) -> None:
        review, fill = self.review.text().strip(), self.fill_dir.text().strip()
        images = self.use_images.isChecked()

        def done(s) -> None:
            self.stat["problems"].setText(str(len(s.problems)))
            self.status.setText(f"{s.accepted} values accepted → {s.rows} rows: "
                                f"{s.csv_result}")
            self._say(s.describe())
        self._run("Applying the review",
                  lambda p: self.actions.apply(review, fill, images, p), done)

    # ── file dialogs ────────────────────────────────────────────────────
    def _start_dir(self, edit: Optional[QLineEdit] = None) -> str:
        for t in ([edit.text()] if edit else []) + [self.csv.text(), self.template.text()]:
            if t.strip():
                return str(Path(t.strip()).parent)
        return str(Path.home())

    def browse_template(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "Template workbook",
                                           self._start_dir(self.template), XLSX)
        if p:
            self.template.setText(p)

    def browse_csv(self) -> None:
        p, _ = QFileDialog.getSaveFileName(
            self, "Parent CSV (an existing one, or a new name)", self._start_dir(self.csv),
            "CSV (*.csv)", options=QFileDialog.Option.DontConfirmOverwrite)
        if p:
            self.csv.setText(p)

    def browse_sources(self) -> None:
        ps, _ = QFileDialog.getOpenFileNames(self, "Source workbooks", self._start_dir(), XLSX)
        self.add_sources(ps)

    def browse_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Folder of source workbooks",
                                             self._start_dir())
        if d:
            self.add_sources([d])

    def browse_review(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "Review workbook",
                                           self._start_dir(self.review), XLSX)
        if p:
            self.review.setText(p)

    def browse_fill_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Folder for filled templates",
                                             self._start_dir(self.fill_dir))
        if d:
            self.fill_dir.setText(d)


def build_ledger(window) -> QWidget:
    """Factory for the council_qt tab registry."""
    return LedgerTab(window)

