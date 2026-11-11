"""
The Ledger's "Merge filled copies…" window (council_core.ledger_merge) - user,
2026-11-11: a template and a folder of filled-out copies merged into one
workbook, a sheet per filled file, then analysed.

Pick the template, the folder of copies and where to save; Merge runs on the
Ledger tab's worker. When it is done the tab shows the merged Ledger (its
template and parent CSV), so Analyze…, Groups… and the Connections graph use
it; "Analyze…" here opens the tab's Analyze window. Not modal.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtWidgets import (QCheckBox, QDialog, QFileDialog, QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSpinBox,
                               QVBoxLayout)

from council_core import ledger_merge as lm


class MergeDialog(QDialog):
    def __init__(self, parent=None, *, template: str = "", folder: str = "",
                 run: Optional[Callable] = None, on_done: Optional[Callable] = None,
                 on_analyze: Optional[Callable[[], None]] = None,
                 opener: Optional[Callable[[str], None]] = None):
        super().__init__(parent)
        self._run = run
        self._on_done = on_done or (lambda _r: None)
        self._on_analyze = on_analyze
        self._opener = opener
        self.result: Optional[lm.MergeResult] = None
        #: tests replace these: no file dialogs
        self._pick_file = lambda: QFileDialog.getOpenFileName(
            self, "Template workbook", "", "Excel workbooks (*.xlsx *.xlsm)")[0]
        self._pick_dir = lambda title: QFileDialog.getExistingDirectory(self, title)
        self._out_touched = False
        self.setWindowTitle("Merge filled copies")
        self.setModal(False)
        self.resize(760, 460)
        self._build()
        self.template.setText(template)
        self.folder.setText(folder)
        self._refresh()

    # ── layout ──────────────────────────────────────────────────────────
    def _build(self) -> None:
        lay = QVBoxLayout(self)
        help_ = QLabel(
            "Merge copies of a template that were filled in by hand into one workbook: an "
            "\"All data\" sheet with every value, then each copy's filled template on a "
            "sheet of its own. Each copy is read from the template's own cells, so it must "
            "keep the template's layout; a file laid out otherwise is listed on a \"Not "
            "merged\" sheet (Extract reads those). The merge is kept as a Ledger, so "
            "Analyze… works on it.")
        help_.setWordWrap(True)
        lay.addWidget(help_)
        grid = QGridLayout()
        self.template = self._row(grid, 0, "Template workbook:", self._browse_template)
        self.folder = self._row(grid, 1, "Folder of filled copies:", self._browse_folder)
        self.out_dir = self._row(grid, 2, "Save the merge to:", self._browse_out)
        self.out_dir.textEdited.connect(self._touched)
        self.folder.textChanged.connect(self._default_out)
        self.recursive = QCheckBox("Include subfolders")
        self.recursive.setChecked(True)
        grid.addWidget(self.recursive, 3, 1)
        match_row = QHBoxLayout()
        match_row.addWidget(QLabel("Skip files that match less than"))
        self.min_match = QSpinBox()
        self.min_match.setRange(50, 100)
        self.min_match.setSuffix("%")
        self.min_match.setValue(round(lm.MIN_MATCH * 100))
        self.min_match.setToolTip(
            "How much of the template's layout (its labels in the same cells) a file must "
            "have to be merged. A file under it is skipped and listed on the Not merged "
            "sheet with how much it matched.")
        match_row.addWidget(self.min_match)
        match_row.addWidget(QLabel("of the template's layout"))
        match_row.addStretch(1)
        grid.addLayout(match_row, 4, 1)
        lay.addLayout(grid)
        row = QHBoxLayout()
        self.merge_btn = QPushButton("Merge")
        self.merge_btn.clicked.connect(self.on_merge)
        row.addWidget(self.merge_btn)
        self.open_btn = QPushButton("Open workbook")
        self.open_btn.clicked.connect(self.on_open)
        row.addWidget(self.open_btn)
        self.analyze_btn = QPushButton("Analyze…")
        self.analyze_btn.setToolTip("The Ledger's Analyze window on the merged data: what "
                                    "is filled, each field's numbers, trends with control "
                                    "limits, correlation and outliers.")
        self.analyze_btn.clicked.connect(self.on_analyze)
        row.addWidget(self.analyze_btn)
        row.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        row.addWidget(close)
        lay.addLayout(row)
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setPlaceholderText("What was merged, and what was not, shows here.")
        lay.addWidget(self.report, 1)

    def _row(self, grid: QGridLayout, r: int, label: str, browse) -> QLineEdit:
        grid.addWidget(QLabel(label), r, 0)
        edit = QLineEdit()
        edit.textChanged.connect(self._refresh)
        grid.addWidget(edit, r, 1)
        b = QPushButton("Browse…")
        b.clicked.connect(browse)
        grid.addWidget(b, r, 2)
        return edit

    # ── picking ─────────────────────────────────────────────────────────
    def _browse_template(self) -> None:
        p = self._pick_file()
        if p:
            self.template.setText(p)

    def _browse_folder(self) -> None:
        p = self._pick_dir("Folder of filled copies")
        if p:
            self.folder.setText(p)

    def _browse_out(self) -> None:
        p = self._pick_dir("Save the merge to")
        if p:
            self._out_touched = True
            self.out_dir.setText(p)

    def _touched(self, _text: str) -> None:
        self._out_touched = True

    def _default_out(self, text: str) -> None:
        """'<folder> merged' beside the folder, until a folder is picked."""
        if not self._out_touched:
            self.out_dir.setText(str(lm.default_out_dir(text.strip())) if text.strip() else "")

    def _refresh(self) -> None:
        ready = all(e.text().strip() for e in (self.template, self.folder, self.out_dir))
        self.merge_btn.setEnabled(ready)
        done = self.result is not None
        self.open_btn.setEnabled(done and self._opener is not None)
        self.analyze_btn.setEnabled(done and self._on_analyze is not None)

    # ── doing ───────────────────────────────────────────────────────────
    def on_merge(self) -> None:
        template = self.template.text().strip()
        folder = self.folder.text().strip()
        out = self.out_dir.text().strip()
        recursive = self.recursive.isChecked()
        min_match = self.min_match.value() / 100.0
        self.report.setPlainText("Merging…")

        def step(progress):
            return lm.merge(template, folder, out, recursive=recursive,
                            min_match=min_match, progress=progress)

        def done(res) -> None:
            if res is None:
                return
            self.result = res
            self.report.setPlainText(res.describe())
            self._refresh()
            self._on_done(res)

        def after(err) -> None:
            if err:
                self.report.setPlainText(err.split("\n")[0])
        if self._run is None:                       # no tab: run here
            try:
                done(step(lambda *_a, **_k: None))
            except Exception as e:                  # noqa: BLE001
                after(f"error: {e}")
            return
        self._run("Merging filled copies", step, done, after=after)

    def on_open(self) -> None:
        if self.result is not None and self._opener is not None:
            self._opener(self.result.workbook)

    def on_analyze(self) -> None:
        if self.result is not None and self._on_analyze is not None:
            self._on_analyze()
