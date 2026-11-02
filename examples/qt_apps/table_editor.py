"""
table_editor.py — open, filter, sort and edit a CSV as a spreadsheet-like
table, with undo.

WHAT IT DOES
Open a CSV (or start with the sample rows). Type in the filter box to show
only matching rows, pick which column it searches, click a header to sort.
Double-click a cell to edit it: number columns get a spin box, a column
with a handful of repeated values gets a drop-down of those values. Every
edit, added row and deleted row can be undone (Ctrl+Z) and redone. Ctrl+C
copies the selected cells as tab-separated text, which pastes into Excel;
Ctrl+V pastes such text back. Closing with unsaved changes asks first.

SHOWS
  * a custom QAbstractTableModel (data / setData / flags / headerData,
    insertRows / removeRows with begin/end notifications)
  * QSortFilterProxyModel for sorting and filtering without touching the
    data, and mapping proxy indexes back to the source
  * a QStyledItemDelegate that picks the editor per column (QComboBox,
    QDoubleSpinBox, QLineEdit) and moves the value in and out
  * QUndoStack + QUndoCommand, with its undo/redo QActions in a menu and
    toolbar, and the stack's clean state driving the "*" in the title
  * QClipboard copy/paste of a rectangular selection
  * a header context menu (customContextMenuRequested) to hide columns
  * QToolBar with icons from the style (QStyle.StandardPixmap)
  * the csv module for reading and writing; "Save changes?" on close

RUN
    python examples/qt_apps/table_editor.py [file.csv]

REQUIRES  nothing beyond PySide6
"""
from __future__ import annotations

import csv
import io
import sys
from pathlib import Path
from typing import Any, List, Optional

from PySide6.QtCore import (QAbstractTableModel, QModelIndex,
                            QSortFilterProxyModel, Qt)
from PySide6.QtGui import QAction, QKeySequence, QUndoCommand, QUndoStack
from PySide6.QtWidgets import (QApplication, QComboBox, QDoubleSpinBox,
                               QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                               QMainWindow, QMenu, QMessageBox, QStyle,
                               QStyledItemDelegate, QTableView, QVBoxLayout,
                               QWidget)

SAMPLE = [["part", "status", "qty", "price"],
          ["bolt M6", "in stock", "120", "0.15"],
          ["nut M6", "in stock", "300", "0.05"],
          ["washer", "low", "12", "0.02"],
          ["bracket", "ordered", "0", "4.50"],
          ["hinge", "in stock", "40", "2.25"]]
#: A column with at most this many distinct values is edited from a list.
CHOICE_LIMIT = 8


def as_number(text: str) -> Optional[float]:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


class TableModel(QAbstractTableModel):
    """Rows of strings. Numbers sort as numbers through Qt.UserRole."""

    def __init__(self, header: List[str], rows: List[List[str]]):
        super().__init__()
        self.header = header
        self.rows = [r + [""] * (len(header) - len(r)) for r in rows]
        self.undo: Optional[QUndoStack] = None

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.header)

    def data(self, index: QModelIndex, role=Qt.DisplayRole) -> Any:
        if not index.isValid():
            return None
        value = self.rows[index.row()][index.column()]
        if role in (Qt.DisplayRole, Qt.EditRole):
            return value
        if role == Qt.UserRole:                 # the sort key
            n = as_number(value)
            return n if n is not None and self.is_numeric(index.column()) \
                else value.lower()
        if role == Qt.TextAlignmentRole and self.is_numeric(index.column()):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None

    def headerData(self, section: int, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        return self.header[section] if orientation == Qt.Horizontal \
            else str(section + 1)

    def flags(self, index: QModelIndex):
        return super().flags(index) | Qt.ItemIsEditable

    def setData(self, index: QModelIndex, value: Any, role=Qt.EditRole):
        """An edit from the view goes through the undo stack."""
        if role != Qt.EditRole or not index.isValid():
            return False
        old = self.rows[index.row()][index.column()]
        new = str(value)
        if new == old:
            return False
        if self.undo is not None:
            self.undo.push(EditCell(self, index.row(), index.column(), old,
                                    new))
        else:
            self.set_cell(index.row(), index.column(), new)
        return True

    def set_cell(self, row: int, col: int, value: str) -> None:
        self.rows[row][col] = value
        ix = self.index(row, col)
        self.dataChanged.emit(ix, ix)

    def insertRows(self, row: int, count: int, parent=QModelIndex()) -> bool:
        self.beginInsertRows(parent, row, row + count - 1)
        for _ in range(count):
            self.rows.insert(row, [""] * len(self.header))
        self.endInsertRows()
        return True

    def removeRows(self, row: int, count: int, parent=QModelIndex()) -> bool:
        self.beginRemoveRows(parent, row, row + count - 1)
        del self.rows[row:row + count]
        self.endRemoveRows()
        return True

    def is_numeric(self, col: int) -> bool:
        vals = [r[col] for r in self.rows if r[col].strip()]
        return bool(vals) and all(as_number(v) is not None for v in vals)

    def choices(self, col: int) -> List[str]:
        vals = sorted({r[col] for r in self.rows if r[col].strip()})
        return vals if 1 < len(vals) <= CHOICE_LIMIT \
            and len(self.rows) > len(vals) else []


# ---------------------------------------------------------- undo commands

class EditCell(QUndoCommand):
    def __init__(self, model: TableModel, row: int, col: int, old: str,
                 new: str):
        super().__init__(f"Edit {model.header[col]}")
        self.m, self.row, self.col, self.old, self.new = \
            model, row, col, old, new

    def redo(self) -> None:
        self.m.set_cell(self.row, self.col, self.new)

    def undo(self) -> None:
        self.m.set_cell(self.row, self.col, self.old)


class AddRow(QUndoCommand):
    def __init__(self, model: TableModel, row: int):
        super().__init__("Add row")
        self.m, self.row = model, row

    def redo(self) -> None:
        self.m.insertRows(self.row, 1)

    def undo(self) -> None:
        self.m.removeRows(self.row, 1)


class DeleteRows(QUndoCommand):
    def __init__(self, model: TableModel, rows: List[int]):
        super().__init__(f"Delete {len(rows)} row(s)")
        self.m = model
        self.rows = sorted(rows)
        self.saved = [list(model.rows[r]) for r in self.rows]

    def redo(self) -> None:
        for r in reversed(self.rows):
            self.m.removeRows(r, 1)

    def undo(self) -> None:
        for r, values in zip(self.rows, self.saved):
            self.m.insertRows(r, 1)
            for c, v in enumerate(values):
                self.m.set_cell(r, c, v)


# ---------------------------------------------------------- the delegate

class CellDelegate(QStyledItemDelegate):
    """The editor that fits the column."""

    def __init__(self, model: TableModel, parent=None):
        super().__init__(parent)
        self.m = model

    def createEditor(self, parent, option, index):
        src = index.model().mapToSource(index)
        if self.m.is_numeric(src.column()):
            box = QDoubleSpinBox(parent, minimum=-1e12, maximum=1e12,
                                 decimals=4)
            return box
        options = self.m.choices(src.column())
        if options:
            combo = QComboBox(parent, editable=True)
            combo.addItems(options)
            return combo
        return QLineEdit(parent)

    def setEditorData(self, editor, index):
        value = index.data(Qt.EditRole)
        if isinstance(editor, QDoubleSpinBox):
            editor.setValue(as_number(value) or 0.0)
        elif isinstance(editor, QComboBox):
            editor.setCurrentText(value)
        else:
            editor.setText(value)

    def setModelData(self, editor, model, index):
        if isinstance(editor, QDoubleSpinBox):
            v = editor.value()
            text = str(int(v)) if v.is_integer() else f"{v:g}"
        elif isinstance(editor, QComboBox):
            text = editor.currentText()
        else:
            text = editor.text()
        model.setData(index, text)


# ---------------------------------------------------------- the window

class TableWindow(QMainWindow):
    def __init__(self, path: Optional[Path] = None):
        super().__init__()
        self.resize(900, 560)
        self.path: Optional[Path] = None
        self.undo = QUndoStack(self)
        self.undo.cleanChanged.connect(self._title)

        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSortRole(Qt.UserRole)
        self.proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)

        central = QWidget()
        v = QVBoxLayout(central)
        row = QHBoxLayout()
        row.addWidget(QLabel("Filter"))
        self.filter = QLineEdit(placeholderText="text to find",
                                clearButtonEnabled=True)
        self.filter.textChanged.connect(self.proxy.setFilterFixedString)
        row.addWidget(self.filter, 1)
        self.filter_col = QComboBox()
        self.filter_col.currentIndexChanged.connect(
            lambda i: self.proxy.setFilterKeyColumn(i - 1))  # -1 = all
        row.addWidget(self.filter_col)
        v.addLayout(row)

        self.table = QTableView(sortingEnabled=True, alternatingRowColors=True)
        self.table.setModel(self.proxy)
        self.table.setSelectionMode(QTableView.ExtendedSelection)
        hdr = self.table.horizontalHeader()
        hdr.setStretchLastSection(True)
        hdr.setContextMenuPolicy(Qt.CustomContextMenu)
        hdr.customContextMenuRequested.connect(self._header_menu)
        v.addWidget(self.table, 1)
        self.setCentralWidget(central)
        self._actions()

        if path:
            self.open_path(path)
        else:
            self.set_data(SAMPLE[0], SAMPLE[1:])

    def _actions(self) -> None:
        st = self.style()
        tb = self.addToolBar("Main")
        tb.setObjectName("main_toolbar")
        fm = self.menuBar().addMenu("&File")
        em = self.menuBar().addMenu("&Edit")

        def act(menu, text, slot, keys=None, icon=None) -> QAction:
            a = QAction(text, self)
            if icon is not None:
                a.setIcon(st.standardIcon(icon))
                tb.addAction(a)
            if keys:
                a.setShortcut(keys)
            a.triggered.connect(slot)
            menu.addAction(a)
            return a

        act(fm, "Open…", self.open_file, QKeySequence.Open,
            QStyle.SP_DialogOpenButton)
        act(fm, "Save", self.save, QKeySequence.Save,
            QStyle.SP_DialogSaveButton)
        act(fm, "Save as…", self.save_as, QKeySequence.SaveAs)
        fm.addSeparator()
        act(fm, "Quit", self.close, QKeySequence.Quit)

        undo = self.undo.createUndoAction(self, "Undo")
        undo.setShortcut(QKeySequence.Undo)
        redo = self.undo.createRedoAction(self, "Redo")
        redo.setShortcut(QKeySequence.Redo)
        em.addActions([undo, redo])
        tb.addSeparator()
        tb.addActions([undo, redo])
        em.addSeparator()
        act(em, "Add row", self.add_row, "Ctrl+N",
            QStyle.SP_FileDialogNewFolder)
        act(em, "Delete rows", self.delete_rows, QKeySequence.Delete,
            QStyle.SP_TrashIcon)
        act(em, "Copy", self.copy, QKeySequence.Copy)
        act(em, "Paste", self.paste, QKeySequence.Paste)

    # ---------------------------------------------------------- data
    def set_data(self, header: List[str], rows: List[List[str]]) -> None:
        self.model = TableModel(header, rows)
        self.model.undo = self.undo
        self.proxy.setSourceModel(self.model)
        self.table.setItemDelegate(CellDelegate(self.model, self.table))
        self.filter_col.blockSignals(True)
        self.filter_col.clear()
        self.filter_col.addItems(["all columns"] + header)
        self.filter_col.blockSignals(False)
        self.proxy.setFilterKeyColumn(-1)
        self.undo.clear()
        self.table.resizeColumnsToContents()
        self._title()
        self.statusBar().showMessage(f"{len(rows)} rows, {len(header)} "
                                     f"columns")

    def open_file(self) -> None:
        if not self.maybe_save():
            return
        name, _ = QFileDialog.getOpenFileName(self, "Open CSV", "",
                                              "CSV (*.csv);;All (*)")
        if name:
            self.open_path(Path(name))

    def open_path(self, path: Path) -> None:
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        if not rows:
            QMessageBox.information(self, "Empty file", f"{path} is empty.")
            return
        self.path = path
        self.set_data(rows[0], rows[1:])

    def save(self) -> bool:
        if self.path is None:
            return self.save_as()
        with open(self.path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(self.model.header)
            w.writerows(self.model.rows)
        self.undo.setClean()
        self.statusBar().showMessage(f"Saved {self.path}")
        return True

    def save_as(self) -> bool:
        name, _ = QFileDialog.getSaveFileName(self, "Save CSV", "table.csv",
                                              "CSV (*.csv)")
        if not name:
            return False
        self.path = Path(name)
        return self.save()

    def maybe_save(self) -> bool:
        """True when it is fine to throw the current table away."""
        if self.undo.isClean():
            return True
        answer = QMessageBox.question(
            self, "Unsaved changes", "Save your changes first?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if answer == QMessageBox.Save:
            return self.save()
        return answer == QMessageBox.Discard

    def closeEvent(self, event) -> None:
        event.accept() if self.maybe_save() else event.ignore()

    def _title(self, *_: object) -> None:
        name = self.path.name if self.path else "untitled"
        star = "" if self.undo.isClean() else "*"
        self.setWindowTitle(f"{name}{star} — Table editor")

    # ---------------------------------------------------------- editing
    def selected_source_rows(self) -> List[int]:
        rows = {self.proxy.mapToSource(ix).row()
                for ix in self.table.selectionModel().selectedIndexes()}
        return sorted(rows)

    def add_row(self) -> None:
        rows = self.selected_source_rows()
        at = rows[-1] + 1 if rows else self.model.rowCount()
        self.undo.push(AddRow(self.model, at))

    def delete_rows(self) -> None:
        rows = self.selected_source_rows()
        if rows:
            self.undo.push(DeleteRows(self.model, rows))

    def copy(self) -> None:
        idx = sorted(self.table.selectionModel().selectedIndexes(),
                     key=lambda i: (i.row(), i.column()))
        if not idx:
            return
        out = io.StringIO()
        w = csv.writer(out, delimiter="\t", lineterminator="\n")
        row_of = {}
        for ix in idx:
            row_of.setdefault(ix.row(), []).append(ix.data())
        w.writerows(row_of.values())
        QApplication.clipboard().setText(out.getvalue())

    def paste(self) -> None:
        text = QApplication.clipboard().text()
        start = self.table.currentIndex()
        if not text or not start.isValid():
            return
        rows = list(csv.reader(io.StringIO(text), delimiter="\t"))
        # Every target cell is found BEFORE the first write: a sorted view
        # re-sorts after each edit, so proxy row 1 is a different row by then.
        targets = []
        for dr, values in enumerate(rows):
            for dc, value in enumerate(values):
                ix = self.proxy.index(start.row() + dr, start.column() + dc)
                if ix.isValid():
                    targets.append((self.proxy.mapToSource(ix), value))
        self.undo.beginMacro("Paste")
        for src, value in targets:
            self.model.setData(src, value)
        self.undo.endMacro()

    def _header_menu(self, pos) -> None:
        hdr = self.table.horizontalHeader()
        menu = QMenu(self)
        for col, name in enumerate(self.model.header):
            a = menu.addAction(name)
            a.setCheckable(True)
            a.setChecked(not hdr.isSectionHidden(col))
            a.toggled.connect(lambda on, c=col: hdr.setSectionHidden(c, not on))
        menu.exec(hdr.mapToGlobal(pos))


def make_window(argv: Optional[List[str]] = None) -> TableWindow:
    args = argv if argv is not None else sys.argv[1:]
    return TableWindow(Path(args[0]) if args else None)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    win = make_window()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
