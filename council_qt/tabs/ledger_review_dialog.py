"""
The Ledger tab's "Review by clicking…" window (user, 2026-10-22: "For L1 can
we do something similar to the existing link by clicking? That just be more
easily readable and viewable. Highlight the cells in question in that").

The review workbook's items on the left, the ones that need a decision first.
Pick one: the template above shows where its value goes, the source workbook
below shows the cell it was read from, both highlighted and scrolled to. Say
Right or Wrong (or correct the value) and the next item comes up. Save writes
the answers into the review workbook's own Accept and value cells
(council_core.ledger_review), so Apply, and checking it in Excel, are as
before.

Reads run on worker threads; their results come back through Qt signals.
"""
from __future__ import annotations

import threading
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton,
                               QSplitter, QTableView, QTableWidget, QTableWidgetItem,
                               QTabWidget, QVBoxLayout, QWidget)

from council_core import ledger_linking as ll
from council_core import ledger_review as lv
from sample_ledger import LedgerError

from .ledger_link_dialog import FIELD_TINT, PICKED_TINT, SheetModel

#: the source: the cell the value came from; the rest of a record's row; a clash
CELL_TINT, ROW_TINT, CLASH_TINT = "#FFC000", "#FFE699", "#F8CBAD"
#: the list: an answer given here, not saved yet
ANSWER_TINT = "#E2EFDA"

SHOW_NEEDS, SHOW_ALL = "Needs a decision", "Everything"


class ReviewDialog(QDialog):
    _template_read = Signal(object, str)        # (TemplateView or None, error)
    _source_read = Signal(str, object, str)     # (path, SampleView or None, error)

    def __init__(self, review: lv.Review, parent=None,
                 log: Optional[Callable[[str], None]] = None,
                 opener: Optional[Callable[[str], None]] = None,
                 on_saved: Optional[Callable[[int], None]] = None):
        super().__init__(parent)
        self.setWindowTitle(f"Review by clicking — {Path(review.path).name}")
        self.resize(1400, 860)
        self.review = review
        self.log = log or (lambda _s: None)
        self.opener = opener
        self.on_saved = on_saved
        self.template: Optional[ll.TemplateView] = None
        #: (field, item) -> (sheet, cell): the one template cell a value goes in
        self._exact: Dict[Tuple[str, str], Tuple[str, Tuple[int, int]]] = {}
        self.sources: Dict[str, Optional[ll.SampleView]] = {}
        self._reading: set = set()
        self.answers: Dict[Tuple[str, int], Dict[str, Any]] = {}
        self.shown: List[lv.Item] = []
        self.current: Optional[lv.Item] = None
        self._template_models: List[SheetModel] = []
        self._template_views: List[QTableView] = []
        self._source_models: List[SheetModel] = []
        self._source_views: List[QTableView] = []
        self._source_shown = ""
        self._build()
        self._template_read.connect(self._got_template)
        self._source_read.connect(self._got_source)
        self._fill_list()
        if review.csv:
            self._read_template()
        else:
            self.template_note.setText("The review does not say which parent CSV it is "
                                       "for: the template cannot be shown.")
        if self.shown:
            self.items.selectRow(0)

    # ── layout ──────────────────────────────────────────────────────────
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        help_ = QLabel(
            "Pick an item: the template shows where its value goes, the source shows the "
            "cell it was read from, both highlighted. Press Right or Wrong (or correct the "
            "value) and the next one comes up. Save writes your answers into the review "
            "workbook; then press Apply in the Ledger tab.")
        help_.setWordWrap(True)
        outer.addWidget(help_)
        row = QHBoxLayout()
        row.addWidget(QLabel("Show:"))
        self.show_choice = QComboBox()
        self.show_choice.addItems([SHOW_NEEDS, SHOW_ALL])
        self.show_choice.currentIndexChanged.connect(lambda _i: self._fill_list())
        row.addWidget(self.show_choice)
        self.counts = QLabel("")
        row.addWidget(self.counts, 1)
        outer.addLayout(row)

        split = QSplitter(Qt.Horizontal)
        self.items = QTableWidget(0, 3)
        self.items.setHorizontalHeaderLabels(["Accept", "Item", "From"])
        self.items.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.items.horizontalHeader().setSectionResizeMode(2, QHeaderView.Interactive)
        self.items.verticalHeader().setVisible(False)
        self.items.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.items.setSelectionMode(QAbstractItemView.SingleSelection)
        self.items.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.items.itemSelectionChanged.connect(self._picked)
        split.addWidget(self.items)

        right = QSplitter(Qt.Vertical)
        top = QWidget()
        tv = QVBoxLayout(top)
        tv.setContentsMargins(0, 0, 0, 0)
        self.template_note = QLabel("<b>Template</b> — where the value goes. Reading it…")
        tv.addWidget(self.template_note)
        self.template_tabs = QTabWidget()
        tv.addWidget(self.template_tabs, 1)
        right.addWidget(top)
        bottom = QWidget()
        bv = QVBoxLayout(bottom)
        bv.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.source_note = QLabel("<b>Source</b> — where it was read from")
        self.source_note.setWordWrap(True)
        row.addWidget(self.source_note, 1)
        self.open_btn = QPushButton("Open file")
        self.open_btn.setToolTip("Open the source workbook in its own program")
        self.open_btn.clicked.connect(self._open_source)
        row.addWidget(self.open_btn)
        bv.addLayout(row)
        self.source_tabs = QTabWidget()
        bv.addWidget(self.source_tabs, 1)
        right.addWidget(bottom)
        right.setSizes([330, 430])
        split.addWidget(right)
        split.setSizes([460, 940])
        outer.addWidget(split, 1)

        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.detail.setMinimumHeight(70)
        self.detail.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        outer.addWidget(self.detail)
        row = QHBoxLayout()
        self.value_label = QLabel("Value:")
        row.addWidget(self.value_label)
        self.value_edit = QLineEdit()
        self.value_edit.setPlaceholderText("Correct the value here if it was read wrong")
        self.value_edit.returnPressed.connect(self.on_right)
        row.addWidget(self.value_edit, 1)
        self.right_btn = QPushButton("✓ Right — accept")
        self.right_btn.setToolTip("Accept = Y: this value goes into the parent CSV "
                                  "(with your correction, if you typed one)")
        self.right_btn.clicked.connect(self.on_right)
        row.addWidget(self.right_btn)
        self.wrong_btn = QPushButton("✗ Wrong — leave out")
        self.wrong_btn.setToolTip("Accept = N: this value does not go into the parent CSV")
        self.wrong_btn.clicked.connect(self.on_wrong)
        row.addWidget(self.wrong_btn)
        outer.addLayout(row)
        row = QHBoxLayout()
        self.status = QLabel("")
        self.status.setWordWrap(True)
        row.addWidget(self.status, 1)
        self.save_btn = QPushButton("Save answers")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self.on_save)
        row.addWidget(self.save_btn)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        row.addWidget(close)
        outer.addLayout(row)
        self._refresh_buttons()

    def _sheet_view(self, model: SheetModel) -> QTableView:
        view = QTableView()
        view.setModel(model)
        view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        view.setSelectionMode(QAbstractItemView.NoSelection)
        view.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        view.horizontalHeader().setDefaultSectionSize(90)
        view.verticalHeader().setDefaultSectionSize(22)
        return view

    # ── the list ────────────────────────────────────────────────────────
    def _accept_of(self, item: lv.Item) -> str:
        a = self.answers.get((item.sheet, item.row))
        return a["accept"] if a and "accept" in a else item.accept

    def _fill_list(self) -> None:
        keep = self.current
        everything = self.show_choice.currentText() == SHOW_ALL
        self.shown = [i for i in self.review.items if everything or i.needs]
        self.items.blockSignals(True)
        self.items.setRowCount(len(self.shown))
        for n, it in enumerate(self.shown):
            self._fill_row(n, it)
        self.items.blockSignals(False)
        needs = sum(i.needs for i in self.review.items)
        self.counts.setText(f"{needs} of {len(self.review.items)} need a decision; "
                            f"{len(self.answers)} answered here, not saved yet"
                            if self.answers else
                            f"{needs} of {len(self.review.items)} need a decision")
        if keep in self.shown:
            self.items.selectRow(self.shown.index(keep))

    def _fill_row(self, n: int, it: lv.Item) -> None:
        answered = (it.sheet, it.row) in self.answers
        cells = [self._accept_of(it) or "?", it.title,
                 Path(it.where.split(" › ", 1)[0]).name if it.where else ""]
        tint = (ANSWER_TINT if answered else CLASH_TINT if it.clash
                else "#FFF2CC" if it.needs else None)
        for c, text in enumerate(cells):
            cell = QTableWidgetItem(text)
            if c == 1:
                cell.setToolTip(it.detail)
            if tint:
                cell.setBackground(QBrush(QColor(tint)))
                cell.setForeground(QBrush(QColor("#000000")))
            self.items.setItem(n, c, cell)

    def _picked(self) -> None:
        rows = self.items.selectionModel().selectedRows()
        if not rows:
            return
        n = rows[0].row()
        if not 0 <= n < len(self.shown):
            return
        it = self.current = self.shown[n]
        a = self.answers.get((it.sheet, it.row), {})
        self.detail.setText(it.detail)
        editable = bool(it.value_col)
        self.value_label.setVisible(editable)
        self.value_edit.setVisible(editable)
        self.value_edit.setText(str(a.get("value", it.value)) if editable else "")
        self._tint_template()
        self._show_source()
        self._refresh_buttons()

    def select(self, item: lv.Item) -> None:
        """Pick ``item`` in the list (tests, and the next one after an answer)."""
        if item not in self.shown:
            self.show_choice.setCurrentText(SHOW_ALL)
        self.items.selectRow(self.shown.index(item))

    # ── answers ─────────────────────────────────────────────────────────
    def _answer(self, accept: str) -> None:
        it = self.current
        if it is None:
            return
        a: Dict[str, Any] = {"accept": accept}
        if it.value_col:
            typed = self.value_edit.text().strip()
            if typed != it.value:
                a.update(value=typed, value_col=it.value_col)
        self.answers[(it.sheet, it.row)] = a
        n = self.shown.index(it)
        self._fill_row(n, it)
        self._fill_list()
        self.status.setText(f"{'Accepted' if accept == 'Y' else 'Left out'}: {it.title}. "
                            "Not saved yet.")
        nxt = next((i for i in self.shown[n + 1:] if (i.sheet, i.row) not in self.answers),
                   None)
        if nxt is not None:
            self.items.selectRow(self.shown.index(nxt))
        self._refresh_buttons()

    def on_right(self) -> None:
        self._answer("Y")

    def on_wrong(self) -> None:
        self._answer("N")

    def on_save(self) -> bool:
        if not self.answers:
            self.status.setText("Nothing to save: no answers given here yet.")
            return True
        try:
            n = lv.save(self.review.path, self.answers)
        except LedgerError as e:
            self.status.setText(f"Not saved: {e}")
            return False
        for it in self.review.items:
            a = self.answers.get((it.sheet, it.row))
            if a:
                it.accept = a["accept"]
                if "value" in a:
                    it.value = a["value"]
        self.answers.clear()
        self._fill_list()
        self.status.setText(f"Saved {n} answer(s) into {Path(self.review.path).name}. "
                            "Press Apply in the Ledger tab to put them in the parent CSV.")
        self.log(f"Review by clicking: saved {n} answer(s) into {self.review.path}")
        if self.on_saved is not None:
            self.on_saved(n)
        return True

    def reject(self) -> None:                                # noqa: D401
        if self.answers and not _no_dialogs():
            got = QMessageBox.question(
                self, "Unsaved answers",
                f"Save your {len(self.answers)} answer(s) into the review before closing?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save)
            if got == QMessageBox.Cancel:
                return
            if got == QMessageBox.Save and not self.on_save():
                return
        super().reject()

    def _refresh_buttons(self) -> None:
        on = self.current is not None
        self.right_btn.setEnabled(on)
        self.wrong_btn.setEnabled(on)
        self.open_btn.setEnabled(bool(on and self.current.source_file
                                      and self.opener is not None))
        self.save_btn.setEnabled(bool(self.answers))

    # ── the template ────────────────────────────────────────────────────
    def _read_template(self) -> None:
        csv = self.review.csv

        def work() -> None:
            try:
                view = ll.template_view(csv)
                if view.kind == "samples":
                    self._exact = lv.template_cells(csv)
                self._template_read.emit(view, "")
            except LedgerError as e:
                self._template_read.emit(None, str(e))
            except Exception:                              # noqa: BLE001
                self._template_read.emit(None, traceback.format_exc(limit=3))
        threading.Thread(target=work, name="ledger-review-template", daemon=True).start()

    def _got_template(self, view, err: str) -> None:
        if view is None:
            self.template_note.setText(f"<b>Template</b> — could not be read: {err}")
            return
        self.template = view
        self.template_tabs.clear()
        self._template_models, self._template_views = [], []
        for sheet in view.sheets:
            m = SheetModel(sheet, self)
            v = self._sheet_view(m)
            self._template_models.append(m)
            self._template_views.append(v)
            self.template_tabs.addTab(v, sheet.name)
        self._tint_template()

    def _tint_template(self) -> None:
        if self.template is None:
            return
        it = self.current
        want = set(it.fields) if it is not None else set()
        # the one cell this value goes in (a flaw's row of a grid), when known
        exact = (self._exact.get((it.fields[0], it.item)) if it is not None and it.fields
                 else None)
        first: Optional[Tuple[int, Tuple[int, int]]] = None
        for i, m in enumerate(self._template_models):
            tints = {}
            for rc, f in m.sheet.fields.items():
                if f in want:
                    tints[rc] = PICKED_TINT
                    if first is None or (i, rc) < first:
                        first = (i, rc)
                else:
                    tints[rc] = FIELD_TINT
            if exact is not None and exact[0] == m.sheet.name:
                tints[exact[1]] = CELL_TINT
                first = (i, exact[1])
            m.set_tints(tints)
        if first is not None:
            i, (r, c) = first
            self.template_tabs.setCurrentIndex(i)
            self._scroll(self._template_views[i], self._template_models[i], r, c)
            self.template_note.setText("<b>Template</b> — where the value goes: "
                                       + ", ".join(sorted(want))[:300])
        elif want:
            self.template_note.setText("<b>Template</b> — "
                                       + ", ".join(sorted(want))[:200]
                                       + " has no cell of its own on the template")
        else:
            self.template_note.setText("<b>Template</b> — this item has no field")

    @staticmethod
    def _scroll(view: QTableView, model: SheetModel, r: int, c: int) -> None:
        idx = model.index(r - 1, c - 1)
        if idx.isValid():
            view.scrollTo(idx, QAbstractItemView.PositionAtCenter)

    # ── the source ──────────────────────────────────────────────────────
    def _show_source(self) -> None:
        it = self.current
        if it is None:
            return
        path = it.source_file
        if not path:
            self.source_note.setText(f"<b>Source</b> — {it.where or 'unknown'}: the review "
                                     "does not say where that file is")
            self._clear_source()
            return
        if path in self.sources:
            self._draw_source(path)
            return
        self.source_note.setText(f"<b>Source</b> — reading {Path(path).name}…")
        if path in self._reading:
            return
        self._reading.add(path)
        csv = self.review.csv or None

        def work() -> None:
            try:
                self._source_read.emit(path, ll.sample_view(path, csv), "")
            except LedgerError as e:
                self._source_read.emit(path, None, str(e))
            except Exception:                              # noqa: BLE001
                self._source_read.emit(path, None, traceback.format_exc(limit=3))
        threading.Thread(target=work, name="ledger-review-source", daemon=True).start()

    def _got_source(self, path: str, view, err: str) -> None:
        self._reading.discard(path)
        self.sources[path] = view
        if view is None:
            self.log(f"Review by clicking: could not read {Path(path).name}: {err}")
        if self.current is not None and self.current.source_file == path:
            self._draw_source(path, err)

    def _clear_source(self) -> None:
        self.source_tabs.clear()
        self._source_models, self._source_views = [], []
        self._source_shown = ""

    def _draw_source(self, path: str, err: str = "") -> None:
        view = self.sources.get(path)
        it = self.current
        if view is None:
            self._clear_source()
            self.source_note.setText(f"<b>Source</b> — {Path(path).name} could not be read"
                                     + (f": {err.splitlines()[0]}" if err else ""))
            return
        if self._source_shown != path:
            self._clear_source()
            for sheet in view.sheets:
                m = SheetModel(sheet, self)
                v = self._sheet_view(m)
                self._source_models.append(m)
                self._source_views.append(v)
                self.source_tabs.addTab(v, sheet.name)
            self._source_shown = path
        cells = (lv.record_row_cells(view, it) if self.review.kind == "records"
                 else list(it.source_cells))
        exact = set(it.source_cells) if self.review.kind != "records" else set()
        on = None
        for i, m in enumerate(self._source_models):
            if m.sheet.name == it.source_sheet and cells:
                tints = {rc: ROW_TINT for rc in cells}
                for rc in exact:
                    tints[rc] = CLASH_TINT if it.clash else CELL_TINT
                m.set_tints(tints)
                on = i
            else:
                m.set_tints({})
        name = Path(path).name
        if on is None:
            why = ("it was read from a picture" if "image" in it.where else
                   f"the sheet {it.source_sheet!r} is not shown" if it.source_sheet else
                   "the review gives no cell")
            self.source_note.setText(f"<b>Source</b> — {name}: {why}")
            return
        r, c = cells[0]
        self.source_tabs.setCurrentIndex(on)
        self._scroll(self._source_views[on], self._source_models[on], r, c)
        self.source_note.setText(f"<b>Source</b> — {it.where}"
                                 if self.review.kind != "records" else
                                 f"<b>Source</b> — {name} › {it.source_sheet}, row {r}")

    def _open_source(self) -> None:
        if self.current is not None and self.current.source_file and self.opener:
            self.opener(self.current.source_file)



class FilledTemplateDialog(ReviewDialog):
    """The template filled in for one part, each value in its cell tinted
    by how sure it is (ledger_review.confidence_tint) - user, 2026-10-23:
    "a pop-out of the output filled in template excel so I can click on
    cells and see their source easily to confirm if its right or wrong".

    Click a filled cell: the values offered for it come up on the left, the
    first picked, and its source cell is shown highlighted below. Right /
    Wrong / a correction are answers to the review, as in Review by
    clicking (the same Save)."""

    def __init__(self, review: lv.Review, parent=None,
                 log: Optional[Callable[[str], None]] = None,
                 opener: Optional[Callable[[str], None]] = None,
                 on_saved: Optional[Callable[[int], None]] = None, part: str = "",
                 on_rerun: Optional[Callable[[str, str], None]] = None,
                 on_groups: Optional[Callable[[], None]] = None):
        self._parts = lv.parts(review)
        #: the Ledger tab's Groups window (LedgerTab.on_groups_window)
        self.on_groups = on_groups
        #: the Ledger tab's re-run (LedgerTab.on_rerun_with_answers): given
        #: the review and the part shown, once the answers are saved
        self.on_rerun = on_rerun
        self.part = part if part in self._parts else (self._parts[0] if self._parts else "")
        #: (template sheet, cell) clicked: the list shows the values offered for it
        self._cell: Optional[Tuple[str, Tuple[int, int]]] = None
        #: what the Ledger holds already (ledger_review.ledger_reader), read
        #: when first shown
        self._held = None
        super().__init__(review, parent, log, opener, on_saved)
        self.setWindowTitle(f"Filled template — {Path(review.path).name}")

    def _build(self) -> None:
        super()._build()
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Part:"))
        self.part_choice = QComboBox()
        self.part_choice.addItems(self._parts)
        self.part_choice.setCurrentText(self.part)
        self.part_choice.currentTextChanged.connect(self.pick_part)
        bar.addWidget(self.part_choice)
        for tint, words in lv.LEGEND:
            key = QLabel(f" {words} ")
            key.setStyleSheet(f"background:{tint}; color:#000000; border-radius:3px")
            bar.addWidget(key)
        bar.addStretch(1)
        self.all_btn = QPushButton("All values of this part")
        self.all_btn.setToolTip("List every value of this part, not only the clicked cell's")
        self.all_btn.clicked.connect(self.show_whole_part)
        bar.addWidget(self.all_btn)
        self.export_btn = QPushButton("Open as Excel")
        self.export_btn.setToolTip("Write this part's filled template (tinted, with a note of "
                                   "each value's source) beside the review, and open it")
        self.export_btn.clicked.connect(self.on_export)
        bar.addWidget(self.export_btn)
        # user, 2026-10-23: "look at a few values and then give an answer and
        # have it rerun and fill things in more confidently"
        self.rerun_btn = QPushButton("Re-run with my answers")
        self.rerun_btn.setToolTip(
            "Save your answers, Apply them (the values said Right go in the parent CSV, "
            "where the next run learns from them), and run Extract again on the same "
            "sources. This window opens again on the new review; the old review is kept.")
        self.rerun_btn.clicked.connect(self.on_rerun_clicked)
        self.rerun_btn.setVisible(self.on_rerun is not None)
        bar.addWidget(self.rerun_btn)
        self.groups_btn = QPushButton("Groups…")
        self.groups_btn.setToolTip("Slice the parts into groups, each with its own fields and "
                                   "workbook (user, 2026-10-26)")
        self.groups_btn.clicked.connect(lambda: self.on_groups and self.on_groups())
        self.groups_btn.setVisible(self.on_groups is not None)
        bar.addWidget(self.groups_btn)
        self.layout().insertLayout(0, bar)
        help_ = QLabel("Click a filled cell of the template: its value's source is shown "
                       "below, highlighted. Say Right or Wrong (or correct it); a cell "
                       "several sources filled lists each on the left. Save writes your "
                       "answers into the review workbook; then Apply - or Re-run with "
                       "my answers, to run Extract again learning from them.")
        help_.setWordWrap(True)
        self.layout().insertWidget(1, help_)
        self.show_choice.setVisible(False)

    # ── which values are listed ─────────────────────────────────────────
    def _candidates(self) -> Dict[Tuple[str, Tuple[int, int]], List[lv.Item]]:
        return lv.cell_candidates(self.review.items, self._exact, self.part,
                                  self._accept_of)

    def _cell_of(self, it: Optional[lv.Item]) -> Optional[Tuple[str, Tuple[int, int]]]:
        if it is None or not it.fields:
            return None
        return self._exact.get((it.fields[0], it.item)) or self._exact.get((it.fields[0], ""))

    def _fill_list(self) -> None:
        keep = self.current
        mine = [i for i in self.review.items if i.part == self.part]
        if self._cell is not None:
            mine = list(self._candidates().get(self._cell, []))
        self.shown = mine
        self.items.blockSignals(True)
        self.items.setRowCount(len(self.shown))
        for n, it in enumerate(self.shown):
            self._fill_row(n, it)
        self.items.blockSignals(False)
        part_items = [i for i in self.review.items if i.part == self.part]
        needs = sum(i.needs for i in part_items)
        what = (f"{len(self.shown)} value(s) offered for the clicked cell"
                if self._cell is not None else f"{len(part_items)} value(s)")
        self.counts.setText(f"{self.part}: {what}; {needs} of the part's need a decision"
                            + (f"; {len(self.answers)} answered here, not saved yet"
                               if self.answers else ""))
        if keep in self.shown:
            self.items.selectRow(self.shown.index(keep))

    def select(self, item: lv.Item) -> None:
        if item not in self.shown:
            self._cell = None
            if item.part != self.part:
                self.part_choice.setCurrentText(item.part)
            self._fill_list()
        self.items.selectRow(self.shown.index(item))

    def pick_part(self, part: str) -> None:
        if not part or part == self.part and self.shown:
            return
        self.part, self._cell, self.current = part, None, None
        self._fill_list()
        self.items.clearSelection()
        if self.shown:
            self.items.selectRow(0)
        else:
            self._tint_template()

    def show_whole_part(self) -> None:
        self._cell = None
        self._fill_list()

    def click_cell(self, sheet: str, rc: Tuple[int, int]) -> bool:
        """A template cell clicked: list the values offered for it, the first
        picked. False when no value of this part goes there."""
        cands = self._candidates().get((sheet, rc))
        if not cands:
            had = self._ledger_cells().get((sheet, rc))
            self.status.setText(f"Already in the Ledger: {had[0]} = {had[1]} (not read this "
                                "run)." if had else "No value of this part goes in that cell.")
            return False
        self._cell = (sheet, rc)
        self._fill_list()
        self.items.clearSelection()                 # row 0 may be selected already
        self.items.selectRow(0)
        return True

    def _got_template(self, view, err: str) -> None:
        super()._got_template(view, err)
        for i, v in enumerate(self._template_views):
            v.clicked.connect(lambda idx, i=i: self.click_cell(
                self._template_models[i].sheet.name, (idx.row() + 1, idx.column() + 1)))

    def _answer(self, accept: str) -> None:
        super()._answer(accept)
        self._tint_template()

    # ── the filled template ─────────────────────────────────────────────
    def _ledger_cells(self) -> Dict[Tuple[str, Tuple[int, int]], Tuple[str, str]]:
        """This part's values the Ledger holds already, by template cell."""
        if self._held is None:
            self._held = lv.ledger_reader(self.review.csv) or (lambda *_a: "")
        return lv.ledger_cells(self._held, self.part, self._exact or {})

    def _tint_template(self) -> None:
        if self.template is None:
            return
        cands = self._candidates()
        known = self._ledger_cells()
        at = self._cell_of(self.current)
        first = None
        for i, m in enumerate(self._template_models):
            tints, texts, tips = {}, {}, {}
            for (sheet, rc), (fname, value) in known.items():
                if sheet == m.sheet.name:
                    texts[rc], tints[rc] = value, lv.TINT_LEDGER
                    tips[rc] = f"{fname} = {value}\nalready in the Ledger"
            for (sheet, rc), cs in cands.items():
                if sheet != m.sheet.name:
                    continue
                it = cs[0]
                was = (known.get((sheet, rc)) or ("", ""))[1]
                texts[rc] = it.value
                tints[rc] = lv.TINT_CLASH if lv.differs(it.value, was) else \
                    lv.confidence_tint(it)
                others = [o.value for o in cs[1:] if o.value != it.value]
                tips[rc] = (f"{it.fields[0]} = {it.value}\nfrom {it.where}\nscore "
                            f"{it.score:g}" + (" · accepted" if self._accept_of(it) == "Y"
                                               else "")
                            + (f"\nthe Ledger has {was}" if lv.differs(it.value, was) else "")
                            + (f"\nalso offered: {', '.join(others[:5])}" if others else ""))
            if at is not None and at[0] == m.sheet.name:
                tints[at[1]] = CELL_TINT
                first = (i, at[1])
            m.set_tints(tints, texts, tips)
        n = len(cands)
        old = len(set(known) - set(cands))
        if first is not None:
            i, (r, c) = first
            self.template_tabs.setCurrentIndex(i)
            self._scroll(self._template_views[i], self._template_models[i], r, c)
        self.template_note.setText(
            f"<b>Template filled in for {self.part}</b> — {n} cell(s) filled"
            + (f" (and {old} already in the Ledger)" if old else "")
            + "; click one to see where its value came from" if self.part else
            "<b>Template</b> — this review has no parts")

    def on_rerun_clicked(self) -> bool:
        """Save the answers, hand the review to the Ledger tab to re-run,
        and close (the tab opens the window again on the new review)."""
        if self.on_rerun is None or not self.on_save():
            return False
        if self.on_rerun(self.review.path, self.part) is False:
            return False                   # not started (the tab says why): stay open
        self.log(f"Filled template: re-running with the answers in {self.review.path}")
        self.reject()
        return True

    def on_export(self) -> Optional[str]:
        if not self.part:
            return None
        out = lv.filled_dir(self.review.path) / f"{lv._safe_name(self.part)}.xlsx"
        try:
            path = lv.write_filled(self.review, self.part, out, self._exact or None)
        except (LedgerError, OSError) as e:
            self.status.setText(f"Not written: {e}")
            return None
        self.status.setText(f"Written: {path}"
                            + (" (answers not saved yet are not in it)" if self.answers else ""))
        self.log(f"Filled template for {self.part}: {path}")
        if self.opener is not None:
            self.opener(path)
        return path

def _no_dialogs() -> bool:
    import os
    return os.environ.get("COUNCIL_NO_DIALOGS", "") not in ("", "0")
