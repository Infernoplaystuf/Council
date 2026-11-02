"""
job_runner.py — run many background jobs at once with per-job progress,
cancel, a live log, and a summary at the end.

WHAT IT DOES
Choose a folder and a job type: "SHA-256 checksum" or "Count lines" runs one
job per file; "Simulated work" queues N jobs that just take time, so the
window can be tried anywhere. Start runs them on a thread pool, at most
"Parallel" at a time. Each row shows its own progress bar and status;
select rows and press Cancel to stop them (queued ones never start), or
Cancel all. The log shows what happened with times; the totals and
elapsed time update as jobs finish. Export results writes a CSV.

SHOWS
  * QThreadPool + QRunnable for many short jobs, setMaxThreadCount live
  * a QRunnable cannot have signals, so each job owns a small QObject
    (JobSignals) whose signals cross back to the UI thread
  * cooperative cancel with threading.Event, checked inside the work loop,
    and cancelling jobs that are still queued
  * QTreeWidget as a multi-column job list, with a QProgressBar placed in
    a cell by setItemWidget, and row colours from a status
  * QPlainTextEdit.appendHtml as a coloured, timestamped log with a line
    limit (setMaximumBlockCount)
  * QElapsedTimer + QTimer for an elapsed clock in the status bar
  * QSpinBox, QComboBox, QPushButton enable/disable by state
  * reading big files in chunks so progress can be reported

RUN
    python examples/qt_apps/job_runner.py [folder]

REQUIRES  nothing beyond PySide6
"""
from __future__ import annotations

import csv
import hashlib
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import (QElapsedTimer, QObject, QRunnable, Qt,
                            QThreadPool, QTime, QTimer, Signal)
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QComboBox,
                               QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                               QMainWindow, QPlainTextEdit, QProgressBar,
                               QPushButton, QSpinBox, QSplitter, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

KINDS = ["SHA-256 checksum", "Count lines", "Simulated work"]
CHUNK = 1 << 20
STATUS_COLOURS = {"queued": "#888888", "running": "#1f6fd0",
                  "done": "#1a8a3a", "cancelled": "#b07000",
                  "failed": "#c03030"}


class Cancelled(Exception):
    pass


class JobSignals(QObject):
    progress = Signal(int, int)               # job id, percent
    finished = Signal(int, str, str)          # job id, status, result


@dataclass
class JobSpec:
    id: int
    kind: str
    target: str                               # a file path, or a label


class Job(QRunnable):
    def __init__(self, spec: JobSpec):
        super().__init__()
        self.spec = spec
        self.signals = JobSignals()
        self.cancel = threading.Event()
        self.setAutoDelete(False)             # the window keeps a reference

    def run(self) -> None:
        s = self.spec
        try:
            if self.cancel.is_set():
                raise Cancelled()
            if s.kind == "SHA-256 checksum":
                result = self._sha256(Path(s.target))
            elif s.kind == "Count lines":
                result = self._lines(Path(s.target))
            else:
                result = self._simulated()
            self.signals.finished.emit(s.id, "done", result)
        except Cancelled:
            self.signals.finished.emit(s.id, "cancelled", "")
        except Exception as exc:                         # noqa: BLE001
            self.signals.finished.emit(s.id, "failed",
                                       f"{type(exc).__name__}: {exc}")

    def _chunks(self, path: Path):
        size = max(1, path.stat().st_size)
        done = 0
        with open(path, "rb") as f:
            while True:
                if self.cancel.is_set():
                    raise Cancelled()
                block = f.read(CHUNK)
                if not block:
                    break
                done += len(block)
                self.signals.progress.emit(self.spec.id,
                                           min(100, done * 100 // size))
                yield block

    def _sha256(self, path: Path) -> str:
        h = hashlib.sha256()
        for block in self._chunks(path):
            h.update(block)
        return h.hexdigest()

    def _lines(self, path: Path) -> str:
        return str(sum(block.count(b"\n") for block in self._chunks(path)))

    def _simulated(self) -> str:
        steps = 20 + self.spec.id % 15
        for i in range(steps):
            if self.cancel.is_set():
                raise Cancelled()
            time.sleep(0.05)
            self.signals.progress.emit(self.spec.id, (i + 1) * 100 // steps)
        return f"{steps} steps"


class RunnerWindow(QMainWindow):
    def __init__(self, folder: Optional[str] = None):
        super().__init__()
        self.setWindowTitle("Job runner")
        self.resize(1000, 640)
        self.pool = QThreadPool(self)
        self.jobs: Dict[int, Job] = {}
        self.items: Dict[int, QTreeWidgetItem] = {}
        self.bars: Dict[int, QProgressBar] = {}
        self.results: Dict[int, tuple] = {}
        self.clock = QElapsedTimer()
        self.tick = QTimer(self, interval=200)
        self.tick.timeout.connect(self._update_status)

        top = QHBoxLayout()
        self.folder = QLineEdit(folder or str(Path.cwd()))
        browse = QPushButton("Folder…")
        browse.clicked.connect(self._browse)
        self.kind = QComboBox()
        self.kind.addItems(KINDS)
        self.count = QSpinBox(minimum=1, maximum=500, value=24,
                              prefix="jobs: ")
        self.kind.currentTextChanged.connect(
            lambda k: self.count.setEnabled(k == "Simulated work"))
        self.count.setEnabled(False)
        self.parallel = QSpinBox(minimum=1, maximum=32,
                                 value=max(2, QThreadPool.globalInstance()
                                           .maxThreadCount() // 2),
                                 prefix="parallel: ")
        self.parallel.valueChanged.connect(self.pool.setMaxThreadCount)
        self.pool.setMaxThreadCount(self.parallel.value())
        for w in (QLabel("Folder"), self.folder, browse, self.kind,
                  self.count, self.parallel):
            top.addWidget(w, 1 if w is self.folder else 0)

        buttons = QHBoxLayout()
        self.start_btn = QPushButton("Start")
        self.start_btn.clicked.connect(self.start)
        self.cancel_btn = QPushButton("Cancel selected")
        self.cancel_btn.clicked.connect(self.cancel_selected)
        self.cancel_all_btn = QPushButton("Cancel all")
        self.cancel_all_btn.clicked.connect(self.cancel_all)
        self.export_btn = QPushButton("Export results…")
        self.export_btn.clicked.connect(self.export)
        for b in (self.start_btn, self.cancel_btn, self.cancel_all_btn):
            buttons.addWidget(b)
        buttons.addStretch(1)
        self.totals = QLabel()
        buttons.addWidget(self.totals)
        buttons.addWidget(self.export_btn)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["#", "Target", "Progress", "Status",
                                   "Result"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setColumnWidth(0, 40)
        self.tree.setColumnWidth(1, 300)
        self.tree.setColumnWidth(2, 140)

        self.log = QPlainTextEdit(readOnly=True)
        self.log.setMaximumBlockCount(2000)

        split = QSplitter(Qt.Vertical)
        split.addWidget(self.tree)
        split.addWidget(self.log)
        split.setSizes([420, 160])

        central = QWidget()
        v = QVBoxLayout(central)
        v.addLayout(top)
        v.addLayout(buttons)
        v.addWidget(split, 1)
        self.setCentralWidget(central)
        self._set_running(False)

    # ---------------------------------------------------------- jobs
    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Folder", self.folder.text())
        if d:
            self.folder.setText(d)

    def specs(self) -> List[JobSpec]:
        kind = self.kind.currentText()
        if kind == "Simulated work":
            return [JobSpec(i, kind, f"task {i + 1}")
                    for i in range(self.count.value())]
        folder = Path(self.folder.text())
        if not folder.is_dir():
            self.say("failed", f"Not a folder: {folder}")
            return []
        files = sorted(p for p in folder.iterdir() if p.is_file())
        return [JobSpec(i, kind, str(p)) for i, p in enumerate(files)]

    def start(self) -> None:
        specs = self.specs()
        if not specs:
            self.say("failed", "Nothing to run.")
            return
        self.tree.clear()
        for table in (self.jobs, self.items, self.bars, self.results):
            table.clear()
        for s in specs:
            item = QTreeWidgetItem([str(s.id + 1), Path(s.target).name
                                    if Path(s.target).exists() else s.target,
                                    "", "queued", ""])
            item.setToolTip(1, s.target)
            self.tree.addTopLevelItem(item)
            bar = QProgressBar(value=0, maximumHeight=16)
            self.tree.setItemWidget(item, 2, bar)
            self._colour(item, "queued")
            job = Job(s)
            job.signals.progress.connect(self._progress)
            job.signals.finished.connect(self._finished)
            self.jobs[s.id], self.items[s.id], self.bars[s.id] = job, item, bar
        self.say("running", f"Starting {len(specs)} job(s): "
                            f"{self.kind.currentText()}, "
                            f"{self.pool.maxThreadCount()} at a time")
        self.clock.start()
        self.tick.start()
        self._set_running(True)
        for job in self.jobs.values():
            self.pool.start(job)

    def _progress(self, job_id: int, pct: int) -> None:
        self.bars[job_id].setValue(pct)
        item = self.items[job_id]
        if item.text(3) == "queued":
            item.setText(3, "running")
            self._colour(item, "running")

    def _finished(self, job_id: int, status: str, result: str) -> None:
        item = self.items[job_id]
        item.setText(3, status)
        item.setText(4, result)
        self._colour(item, status)
        if status == "done":
            self.bars[job_id].setValue(100)
        self.results[job_id] = (self.jobs[job_id].spec.target, status, result)
        if status != "done":
            self.say(status, f"#{job_id + 1} {item.text(1)}: {status} "
                             f"{result}".rstrip())
        self._update_status()
        if len(self.results) == len(self.jobs):
            self.tick.stop()
            self._set_running(False)
            counts = self._counts()
            self.say("done", "Finished: " + ", ".join(
                f"{n} {k}" for k, n in counts.items() if n)
                + f" in {self.clock.elapsed() / 1000:.1f} s")

    def cancel_selected(self) -> None:
        for item in self.tree.selectedItems():
            self._cancel(int(item.text(0)) - 1)

    def cancel_all(self) -> None:
        for job_id in self.jobs:
            self._cancel(job_id)

    def _cancel(self, job_id: int) -> None:
        job = self.jobs.get(job_id)
        if job is None or job_id in self.results:
            return
        job.cancel.set()
        # A job still waiting in the queue is taken out and never runs.
        if self.pool.tryTake(job):
            self._finished(job_id, "cancelled", "")

    # ---------------------------------------------------------- display
    def _colour(self, item: QTreeWidgetItem, status: str) -> None:
        item.setForeground(3, QBrush(QColor(STATUS_COLOURS.get(status,
                                                               "#000"))))

    def _counts(self) -> Dict[str, int]:
        out = {k: 0 for k in STATUS_COLOURS}
        for item in self.items.values():
            out[item.text(3)] = out.get(item.text(3), 0) + 1
        return out

    def _update_status(self) -> None:
        c = self._counts()
        self.totals.setText(f"{c['done']} done · {c['running']} running · "
                            f"{c['queued']} queued · {c['failed']} failed · "
                            f"{c['cancelled']} cancelled")
        if self.clock.isValid():
            self.statusBar().showMessage(
                f"Elapsed {self.clock.elapsed() / 1000:.1f} s")

    def say(self, status: str, text: str) -> None:
        stamp = QTime.currentTime().toString("HH:mm:ss")
        colour = STATUS_COLOURS.get(status, "#000")
        self.log.appendHtml(f'<span style="color:#888">{stamp}</span> '
                            f'<span style="color:{colour}">{text}</span>')

    def _set_running(self, on: bool) -> None:
        self.start_btn.setEnabled(not on)
        self.cancel_btn.setEnabled(on)
        self.cancel_all_btn.setEnabled(on)
        self.export_btn.setEnabled(not on and bool(self.results))
        for w in (self.kind, self.folder):
            w.setEnabled(not on)

    def export(self) -> None:
        name, _ = QFileDialog.getSaveFileName(self, "Export results",
                                              "results.csv", "CSV (*.csv)")
        if not name:
            return
        with open(name, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["target", "status", "result"])
            w.writerows(self.results[k] for k in sorted(self.results))
        self.say("done", f"Exported {len(self.results)} rows to {name}")

    def closeEvent(self, event) -> None:
        self.cancel_all()
        self.pool.waitForDone(3000)
        super().closeEvent(event)


def make_window(argv: Optional[List[str]] = None) -> RunnerWindow:
    args = argv if argv is not None else sys.argv[1:]
    return RunnerWindow(args[0] if args else None)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    win = make_window()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
