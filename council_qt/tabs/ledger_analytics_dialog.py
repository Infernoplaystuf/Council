"""
The Ledger's Analyze window (council_core.ledger_analytics) - user,
2026-10-30: "add a data analytics function for example the ledger output
file we can do some analysis on it"; a window inside the Ledger.

Four views over the parent CSV, for all parts or one group, per flaw or
per part: Coverage (what is filled, and the parts with most left blank),
Summary (each field's numbers or most common values), Trends (a field over a
date or part order, with control limits and the points breaking a run rule),
Correlation (every pair of number fields as a matrix, strongest pairs first;
click a pair for its scatter - user, 2026-10-30: "we need a correlation
matrix so that we can better determine which variables correlate to each
other") and Outliers (values far from the rest of their field). Double-click a part
in Trends or Outliers to open it in the Filled template window and check its
source. "Export…" writes the analysis workbook beside the Ledger. Read only;
not modal.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import pandas as pd
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton,
                               QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
                               QVBoxLayout, QWidget)

from council_core import ledger_analytics as la
from sample_ledger import LedgerError

ALL = "All parts"
PART_ORDER = "Part order"
ANY = "Any status"
#: rows a grid shows at most (the export and the workbook hold them all)
SHOW_ROWS = 2000


def fill_table(table: QTableWidget, df: pd.DataFrame, grey_blanks: bool = False,
               shade: Optional[Callable[[pd.Series], Optional[str]]] = None) -> None:
    """``df`` into ``table``, read only; numbers sort as numbers.
    ``grey_blanks``: an empty cell greyed. ``shade(row)``: a colour for the
    whole row, or None."""
    table.setSortingEnabled(False)
    table.clear()
    table.setColumnCount(len(df.columns))
    table.setRowCount(len(df))
    table.setHorizontalHeaderLabels([str(c) for c in df.columns])
    grey = QColor("#e4e4e4")
    for r, row in enumerate(df.itertuples(index=False)):
        colour = shade(df.iloc[r]) if shade else None
        for c, v in enumerate(row):
            it = QTableWidgetItem()
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                it.setData(Qt.DisplayRole, round(float(v), 4) if isinstance(v, float) else v)
            else:
                it.setText("" if v is None else (v.isoformat() if hasattr(v, "isoformat")
                                                 else str(v)))
            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            if colour:
                it.setBackground(QColor(colour))
            elif grey_blanks and (v is None or str(v).strip() == ""):
                it.setBackground(grey)
            table.setItem(r, c, it)
    table.resizeColumnsToContents()
    table.setSortingEnabled(True)


def _table() -> QTableWidget:
    t = QTableWidget()
    t.setEditTriggers(QTableWidget.NoEditTriggers)
    t.setSelectionBehavior(QTableWidget.SelectRows)
    t.verticalHeader().setVisible(False)
    return t


class AnalyticsDialog(QDialog):
    def __init__(self, data: la.Data, parent=None,
                 log: Optional[Callable[[str], None]] = None,
                 on_part: Optional[Callable[[str], None]] = None,
                 opener: Optional[Callable[[str], None]] = None,
                 runner: Optional[Callable] = None):
        super().__init__(parent)
        self.data = data
        self.log = log or (lambda _s: None)
        #: the Ledger tab's Filled template window on one part
        self.on_part = on_part
        #: opens a file the way the Ledger tab does (the exported workbook)
        self.opener = opener
        #: runs a slow step off the GUI thread (LedgerTab._run: title, step,
        #: done); None runs it here
        self.runner = runner
        self.canvas = None
        self.corr_canvas = None
        self.scatter_canvas = None
        self.current_corr: Optional[la.Correlation] = None
        self.setWindowTitle(f"Analyze — {Path(data.csv).stem}")
        self.setModal(False)
        self.resize(1150, 720)
        self._build()
        self.refresh()

    # ── layout ──────────────────────────────────────────────────────────
    def _build(self) -> None:
        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Group:"))
        self.group_choice = QComboBox()
        self.group_choice.addItems([ALL] + self.data.group_names())
        bar.addWidget(self.group_choice)
        bar.addWidget(QLabel("Count:"))
        self.per_choice = QComboBox()
        self.per_choice.addItems(["Per flaw", "Per part"] if self.data.item_field
                                 else ["Per part"])
        self.per_choice.setToolTip(
            "Per part takes a per-flaw field's average over the part's flaws, as the "
            "template's AVERAGE columns do. A part-level field counts once per part either way.")
        bar.addWidget(self.per_choice)
        self.stack_box = QCheckBox("Repeated tables as one field")
        self.stack_box.setChecked(bool(self.data.stacked))
        self.stack_box.setEnabled(bool(self.data.stacked) or self._has_runs())
        self.stack_box.setToolTip(
            "A table filled in once per run (Concealment 1, 2, 3 …) analysed as one field per "
            "column: a part's runs averaged (a number) or its one value (a text), plus how "
            "many runs it filled. Untick to see every run as its own field.")
        self.stack_box.toggled.connect(self.on_stack)
        bar.addWidget(self.stack_box)
        bar.addStretch(1)
        self.export_btn = QPushButton("Export…")
        self.export_btn.setToolTip(
            "Write the analysis workbook beside the Ledger: Coverage, Missing, Summary, "
            "Trends, Outliers and a chart per number field, for the group shown.")
        self.export_btn.clicked.connect(self.on_export)
        bar.addWidget(self.export_btn)
        lay.addLayout(bar)
        self.facts = QLabel()
        self.facts.setWordWrap(True)
        lay.addWidget(self.facts)

        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        # Coverage
        page = QSplitter(Qt.Vertical)
        self.coverage_table, self.missing_table = _table(), _table()
        page.addWidget(self._titled("What is filled, per field", self.coverage_table))
        page.addWidget(self._titled("Parts with the most fields left blank",
                                    self.missing_table))
        self.left_out_table = _table()
        page.addWidget(self._titled("Fields in the correlation, and why the others are not",
                                    self.left_out_table))
        self.tabs.addTab(page, "Coverage")
        # Data: every row, searchable, blanks grey (the script's Verify Data)
        data_page = QWidget()
        dl = QVBoxLayout(data_page)
        row = QHBoxLayout()
        row.addWidget(QLabel("Find:"))
        self.data_find = QLineEdit()
        self.data_find.setPlaceholderText("rows with this text in any column (a part, a value…)")
        row.addWidget(self.data_find, 1)
        dl.addLayout(row)
        self.data_note = QLabel()
        dl.addWidget(self.data_note)
        self.data_table = _table()
        self.data_table.cellDoubleClicked.connect(
            lambda r, _c: self._open_part(self.data_table, r))
        dl.addWidget(self.data_table, 1)
        self.tabs.addTab(data_page, "Data")
        # Check: what a merge read from each copy, cell by cell
        check_page = QWidget()
        kl = QVBoxLayout(check_page)
        self.check_note = QLabel()
        self.check_note.setWordWrap(True)
        kl.addWidget(self.check_note)
        split = QSplitter(Qt.Vertical)
        self.check_files = _table()
        self.check_files.currentCellChanged.connect(
            lambda r, _c, _r0, _c0: self.refresh_check_cells())
        split.addWidget(self._titled(
            "Each copy: its values read in place, read where its layout moved, or blank. "
            "Click one to see its values.", self.check_files))
        cells = QWidget()
        cv = QVBoxLayout(cells)
        cv.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel("Show:"))
        self.check_status = QComboBox()
        from council_core import ledger_merge as lm
        self.check_status.addItems([ANY, lm.MOVED, lm.BLANK, lm.READ, lm.NO_SHEET])
        row.addWidget(self.check_status)
        row.addWidget(QLabel("Find:"))
        self.check_find = QLineEdit()
        self.check_find.setPlaceholderText("a field, a cell, a value…")
        row.addWidget(self.check_find, 1)
        cv.addLayout(row)
        self.check_cells = _table()
        self.check_cells.cellDoubleClicked.connect(
            lambda r, _c: self._open_part(self.check_cells, r))
        cv.addWidget(self.check_cells, 1)
        split.addWidget(cells)
        kl.addWidget(split, 1)
        self.tabs.addTab(check_page, "Check")
        # Summary
        self.summary_table = _table()
        self.tabs.addTab(self._titled("Each field: numbers, dates or most common values",
                                      self.summary_table), "Summary")
        # Trends
        trend = QWidget()
        tl = QVBoxLayout(trend)
        row = QHBoxLayout()
        row.addWidget(QLabel("Field:"))
        self.field_choice = QComboBox()
        self.field_choice.addItems(self.data.number_fields)
        row.addWidget(self.field_choice, 1)
        row.addWidget(QLabel("Over:"))
        self.by_choice = QComboBox()
        self.by_choice.addItems(self.data.date_fields + [PART_ORDER])
        row.addWidget(self.by_choice)
        tl.addLayout(row)
        self.chart_box = QVBoxLayout()
        tl.addLayout(self.chart_box, 3)
        self.trend_note = QLabel()
        self.trend_note.setWordWrap(True)
        tl.addWidget(self.trend_note)
        self.trend_table = _table()
        self.trend_table.cellDoubleClicked.connect(
            lambda r, _c: self._open_part(self.trend_table, r))
        tl.addWidget(self.trend_table, 2)
        self.tabs.addTab(trend, "Trends")
        # Correlation
        corr = QWidget()
        cl = QVBoxLayout(corr)
        row = QHBoxLayout()
        row.addWidget(QLabel("Method:"))
        self.method_choice = QComboBox()
        self.method_choice.addItems(["Pearson", "Spearman"])
        self.method_choice.setToolTip(
            "Pearson: a straight-line relation. Spearman: any relation that only goes one "
            "way (by rank), and less moved by one wild value.")
        row.addWidget(self.method_choice)
        self.twins_box = QCheckBox("Show a field against its inspectors' readings")
        self.twins_box.setToolTip(
            "A field and one inspector's own reading of it ('… [JB]') nearly always "
            "correlate: they say the readings agree, not that two things are related. "
            "Left out of the pairs unless ticked; the matrix always shows them.")
        row.addWidget(self.twins_box)
        row.addStretch(1)
        cl.addLayout(row)
        self.corr_note = QLabel()
        self.corr_note.setWordWrap(True)
        cl.addWidget(self.corr_note)
        split = QSplitter(Qt.Horizontal)
        self.corr_box = QWidget()
        self.corr_box_lay = QVBoxLayout(self.corr_box)
        self.corr_box_lay.setContentsMargins(0, 0, 0, 0)
        split.addWidget(self.corr_box)
        right = QSplitter(Qt.Vertical)
        self.pairs_table = _table()
        self.pairs_table.currentCellChanged.connect(
            lambda r, _c, _r0, _c0: self.show_pair(r))
        right.addWidget(self._titled("Pairs, strongest first. Click one for its scatter.",
                                     self.pairs_table))
        self.scatter_box = QWidget()
        self.scatter_lay = QVBoxLayout(self.scatter_box)
        self.scatter_lay.setContentsMargins(0, 0, 0, 0)
        right.addWidget(self.scatter_box)
        split.addWidget(right)
        split.setSizes([600, 500])
        cl.addWidget(split, 1)
        self.tabs.addTab(corr, "Correlation")
        # Plots: a field's spread (the script's Statistics / Plots)
        plots = QWidget()
        pl = QVBoxLayout(plots)
        row = QHBoxLayout()
        row.addWidget(QLabel("Plot:"))
        self.plot_choice = QComboBox()
        self.plot_choice.addItems(la.PLOTS)
        row.addWidget(self.plot_choice)
        row.addWidget(QLabel("Field:"))
        self.plot_field = QComboBox()
        self.plot_field.addItems(self.data.number_fields)
        row.addWidget(self.plot_field, 1)
        row.addWidget(QLabel("Split by:"))
        self.plot_by = QComboBox()
        self.plot_by.addItems(["(nothing)"] + la.box_groups(self.data))
        row.addWidget(self.plot_by)
        pl.addLayout(row)
        self.plot_box = QVBoxLayout()
        pl.addLayout(self.plot_box, 1)
        self.plot_canvas = None
        self.tabs.addTab(plots, "Plots")
        # Outliers
        self.outlier_table = _table()
        self.outlier_table.cellDoubleClicked.connect(
            lambda r, _c: self._open_part(self.outlier_table, r))
        self.tabs.addTab(self._titled(
            f"Values far from the rest of their field (robust z of {la.OUTLIER_Z} or more). "
            "Double-click one to check its source in the filled template.",
            self.outlier_table), "Outliers")
        self.status = QLabel()
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        self.group_choice.currentTextChanged.connect(lambda _t: self.refresh())
        self.per_choice.currentTextChanged.connect(lambda _t: self.refresh())
        self.field_choice.currentTextChanged.connect(lambda _t: self.refresh_trend())
        self.by_choice.currentTextChanged.connect(lambda _t: self.refresh_trend())
        self.method_choice.currentTextChanged.connect(lambda _t: self.refresh_correlation())
        self.twins_box.toggled.connect(lambda _b: self.refresh_correlation())
        self.data_find.textChanged.connect(lambda _t: self.refresh_data())
        for w in (self.plot_choice, self.plot_field, self.plot_by):
            w.currentTextChanged.connect(lambda _t: self.refresh_plot())
        self.check_status.currentTextChanged.connect(lambda _t: self.refresh_check_cells())
        self.check_find.textChanged.connect(lambda _t: self.refresh_check_cells())

    def _has_runs(self) -> bool:
        return any(la._RUN.match(f) for f in self.data.fields)

    def on_stack(self, on: bool) -> None:
        """The Ledger read again with its repeated tables stacked or not."""
        self.data = la.load(self.data.csv, stack=on)
        for box, items in ((self.field_choice, self.data.number_fields),
                           (self.by_choice, self.data.date_fields + [PART_ORDER]),
                           (self.plot_field, self.data.number_fields),
                           (self.plot_by, ["(nothing)"] + la.box_groups(self.data))):
            keep = box.currentText()
            box.blockSignals(True)
            box.clear()
            box.addItems(items)
            if keep in items:
                box.setCurrentText(keep)
            box.blockSignals(False)
        self.refresh()

    @staticmethod
    def _titled(text: str, w: QWidget) -> QWidget:
        box = QWidget()
        bl = QVBoxLayout(box)
        bl.setContentsMargins(0, 0, 0, 0)
        lab = QLabel(text)
        lab.setWordWrap(True)
        bl.addWidget(lab)
        bl.addWidget(w, 1)
        return box

    # ── what is shown ───────────────────────────────────────────────────
    @property
    def group(self) -> str:
        t = self.group_choice.currentText()
        return "" if t == ALL else t

    @property
    def per(self) -> str:
        return la.PER_ROW if self.per_choice.currentText() == "Per flaw" else la.PER_PART

    @property
    def by(self) -> str:
        t = self.by_choice.currentText()
        return "" if t == PART_ORDER else t

    def refresh(self) -> None:
        d, g = self.data, self.group
        rows = la.rows(d, g)
        parts = rows[d.id_field].str.lower().nunique()
        self.facts.setText(
            f"{parts} part(s), {len(rows)} row(s), {len(la.fields_of(d, g))} field(s) "
            f"({len(d.number_fields)} numbers, {len(d.date_fields)} dates)"
            + (f"; group '{g}'" if g else ""))
        fill_table(self.coverage_table, la.coverage(d, g))
        fill_table(self.missing_table, la.missing_by_part(d, g))
        fill_table(self.summary_table, la.summary(d, g, self.per))
        fill_table(self.outlier_table, la.outliers(d, g, self.per))
        fill_table(self.left_out_table, la.left_out_of_correlation(d, g))
        self.refresh_data()
        self.refresh_check()
        self.refresh_plot()
        self.refresh_trend()
        self.refresh_correlation()

    def refresh_plot(self) -> None:
        kind = self.plot_choice.currentText()
        self.plot_field.setEnabled(kind != la.PCA)
        self.plot_by.setEnabled(kind == la.BOX)
        by = self.plot_by.currentText()
        fig = la.plot_figure(self.data, kind, self.plot_field.currentText(),
                             "" if by == "(nothing)" else by, self.group, self.per)
        self.plot_canvas = self._swap(self.plot_box, self.plot_canvas, fig)

    def refresh_data(self) -> None:
        d = self.data
        df = la.rows(d, self.group)
        cols = [c for c in [d.id_field, d.item_field, *d.fields] if c and c in df.columns]
        hits = la.find_rows(df[cols], self.data_find.text())
        self.data_note.setText(
            f"{len(hits)} of {len(df)} row(s)" + (f"; the first {SHOW_ROWS} shown"
                                                  if len(hits) > SHOW_ROWS else "")
            + ". Grey: left blank. Double-click a row to check its part's sources.")
        fill_table(self.data_table, hits.head(SHOW_ROWS).rename(
            columns={d.id_field: "Part"}), grey_blanks=True)

    def refresh_check(self) -> None:
        self.check = la.merge_check(self.data)
        if self.check is None:
            self.check_note.setText(
                "This Ledger was not made by 'Merge filled copies…' (or was merged before "
                "the check was kept: merge it again). Extract's values are checked in "
                "Review by clicking.")
            fill_table(self.check_files, pd.DataFrame())
            fill_table(self.check_cells, pd.DataFrame())
            return
        files = la.check_by_file(self.check)
        moved = int((files["Read at moved cell"] > 0).sum()) if len(files) else 0
        self.check_note.setText(
            f"{len(files)} {'copy' if len(files) == 1 else 'copies'}, {len(self.check)} "
            "value(s) read. "
            + (f"{moved} {'copy' if moved == 1 else 'copies'} read where the layout moved "
               "(yellow): check those first. "
               if moved else "Every copy was read at the template's own cells. ")
            + "Files left out are on the merged workbook's 'Not merged' sheet.")
        fill_table(self.check_files, files,
                   shade=lambda r: "#fff2cc" if r["Read at moved cell"] else None)
        if self.check_files.rowCount():
            self.check_files.setCurrentCell(0, 0)
        self.refresh_check_cells()

    def refresh_check_cells(self) -> None:
        from council_core import ledger_merge as lm
        if getattr(self, "check", None) is None:
            return
        t, r = self.check_files, self.check_files.currentRow()
        df = self.check
        if 0 <= r < t.rowCount():
            cols = [t.horizontalHeaderItem(c).text() for c in range(t.columnCount())]
            f = t.item(r, cols.index("File")).text()
            df = df[df["File"] == f]
        st = self.check_status.currentText()
        if st != ANY:
            df = df[df["Status"] == st]
        df = la.find_rows(df, self.check_find.text())
        fill_table(self.check_cells, df.head(SHOW_ROWS),
                   shade=lambda row: ("#fff2cc" if row["Status"] == lm.MOVED else
                                      "#e4e4e4" if row["Status"] in (lm.BLANK, lm.NO_SHEET)
                                      else None))

    def refresh_trend(self) -> None:
        f = self.field_choice.currentText()
        if not f:
            self.trend_note.setText("This Ledger has no number fields to chart yet.")
            fill_table(self.trend_table, pd.DataFrame())
            return
        try:
            t = la.trend(self.data, f, self.group, by=self.by, per=self.per)
        except LedgerError as e:
            self.trend_note.setText(str(e))
            return
        self.current_trend = t
        self._show_figure(la.trend_figure(t))
        self.trend_note.setText(t.note + (" Double-click a point's row to check its source."
                                          if len(t.flagged) else ""))
        fill_table(self.trend_table, t.flagged if len(t.flagged) else t.points)

    def refresh_correlation(self) -> None:
        c = la.correlation(self.data, self.group, self.per,
                           self.method_choice.currentText().lower())
        self.current_corr = c
        self.corr_note.setText(c.note)
        self.corr_canvas = self._swap(self.corr_box_lay, self.corr_canvas,
                                      la.correlation_figure(c))
        fill_table(self.pairs_table, c.pairs(twins=self.twins_box.isChecked()))
        if self.pairs_table.rowCount():
            self.pairs_table.setCurrentCell(0, 0)
            self.show_pair(0)
        else:
            self.scatter_canvas = self._swap(self.scatter_lay, self.scatter_canvas, None)

    def show_pair(self, row: int) -> None:
        if row < 0 or row >= self.pairs_table.rowCount():
            return
        t = self.pairs_table
        cols = [t.horizontalHeaderItem(c).text() for c in range(t.columnCount())]
        a = t.item(row, cols.index("Field A")).text()
        b = t.item(row, cols.index("Field B")).text()
        self.scatter_canvas = self._swap(self.scatter_lay, self.scatter_canvas,
                                         la.scatter_figure(self.data, a, b, self.group,
                                                           self.per))

    @staticmethod
    def _swap(layout, old, fig):
        """``fig`` drawn in ``layout`` in place of the canvas ``old``."""
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        if old is not None:
            layout.removeWidget(old)
            old.setParent(None)
            old.deleteLater()
        if fig is None:
            return None
        canvas = FigureCanvasQTAgg(fig)
        layout.addWidget(canvas)
        return canvas

    def _show_figure(self, fig) -> None:
        self.canvas = self._swap(self.chart_box, self.canvas, fig)

    def _open_part(self, table: QTableWidget, row: int) -> None:
        cols = [table.horizontalHeaderItem(c).text() for c in range(table.columnCount())]
        if "Part" not in cols or not self.on_part:
            return
        it = table.item(row, cols.index("Part"))
        if it and it.text():
            self.on_part(it.text())

    def on_export(self) -> Optional[Path]:
        """The analysis workbook for what is shown; on the runner when there
        is one (a chart per number field takes a while), else now."""
        group, per, by = self.group, self.per, self.by

        def step(_progress=None) -> Path:
            return la.export(self.data, group=group, per=per, by=by)

        def done(out: Path) -> None:
            self.status.setText(f"Analysis written: {out}")
            self.log(f"── Analysis written: {out}")
            if self.opener:
                self.opener(str(out))

        if self.runner is not None:
            self.status.setText("Writing the analysis workbook…")
            self.runner("Writing the analysis", step, done)
            return None
        try:
            out = step()
        except LedgerError as e:
            self.status.setText(str(e))
            return None
        done(out)
        return out
