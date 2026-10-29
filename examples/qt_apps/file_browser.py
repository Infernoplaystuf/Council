"""
file_browser.py — browse folders, filter by name, and preview text and
images. Read-only: it never changes a file.

WHAT IT DOES
Folders on the left, the files of the chosen folder on the right, a
preview below. Type a path in the bar (it completes as you type), or use
Back, Forward and Up. The filter box takes patterns like "*.csv *.txt".
Selecting a file previews it: images are shown scaled to fit, text files
show their first part, anything else shows its size and dates. Bookmarks
on the far left jump to places; right-click a file to copy its path.

SHOWS
  * QFileSystemModel shared by a QTreeView (folders only) and a QTableView
    (one folder's contents) — setRootIndex, setFilter, setNameFilters
  * QCompleter on a QLineEdit, completing paths from its own model
  * a navigation history (back / forward stacks) driving enabled actions
  * nested QSplitters, and a QStackedWidget switching the preview kind
  * QPixmap scaled with Qt.KeepAspectRatio and re-scaled on resizeEvent
  * reading only the first N bytes of a file for a preview
  * QFileInfo for size and dates; QLocale.formattedDataSize
  * QListWidget of bookmarks with icons from QFileIconProvider
  * a context menu (QMenu) on a view, and QClipboard
  * QToolBar with style icons, QCheckBox toggling hidden files

RUN
    python examples/qt_apps/file_browser.py [folder]

REQUIRES  nothing beyond PySide6
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QDir, QFileInfo, QLocale, Qt
from PySide6.QtGui import QAction, QKeySequence, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox,
                               QCompleter, QFileIconProvider,
                               QFileSystemModel, QFormLayout, QHeaderView,
                               QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMainWindow, QMenu,
                               QPlainTextEdit, QSplitter, QStackedWidget,
                               QStyle, QTableView, QTreeView, QWidget)

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff"}
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".tsv", ".json", ".py", ".yaml",
                 ".yml", ".ini", ".log", ".xml", ".html", ".toml", ".cfg"}
PREVIEW_BYTES = 64_000


class Preview(QStackedWidget):
    """Text, image or plain facts about the selected file."""

    def __init__(self):
        super().__init__()
        self.text = QPlainTextEdit(readOnly=True)
        self.text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.image = QLabel(alignment=Qt.AlignCenter)
        self.image.setMinimumSize(1, 1)
        self.facts = QWidget()
        self.form = QFormLayout(self.facts)
        for w in (self.text, self.image, self.facts):
            self.addWidget(w)
        self.pixmap: Optional[QPixmap] = None

    def show_file(self, path: str) -> None:
        info = QFileInfo(path)
        suffix = Path(path).suffix.lower()
        if info.isFile() and suffix in IMAGE_SUFFIXES:
            self.pixmap = QPixmap(path)
            if not self.pixmap.isNull():
                self._fit()
                self.setCurrentWidget(self.image)
                return
        if info.isFile() and (suffix in TEXT_SUFFIXES or info.size() < 4096):
            with open(path, "rb") as f:
                raw = f.read(PREVIEW_BYTES)
            if b"\0" not in raw:
                more = "\n\n… (preview cut short)" \
                    if info.size() > PREVIEW_BYTES else ""
                self.text.setPlainText(raw.decode("utf-8", "replace") + more)
                self.setCurrentWidget(self.text)
                return
        self._facts(info)

    def _facts(self, info: QFileInfo) -> None:
        while self.form.rowCount():
            self.form.removeRow(0)
        loc = QLocale()
        self.form.addRow("Name", QLabel(info.fileName()))
        self.form.addRow("Kind", QLabel("folder" if info.isDir()
                                        else info.suffix() or "file"))
        if info.isFile():
            self.form.addRow("Size", QLabel(loc.formattedDataSize(
                info.size())))
        self.form.addRow("Modified", QLabel(loc.toString(
            info.lastModified(), QLocale.ShortFormat)))
        self.form.addRow("Folder", QLabel(info.absolutePath(),
                                          wordWrap=True))
        self.setCurrentWidget(self.facts)

    def _fit(self) -> None:
        if self.pixmap is not None and not self.pixmap.isNull():
            self.image.setPixmap(self.pixmap.scaled(
                self.image.size(), Qt.KeepAspectRatio,
                Qt.SmoothTransformation))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.currentWidget() is self.image:
            self._fit()


class BrowserWindow(QMainWindow):
    def __init__(self, start: Optional[str] = None):
        super().__init__()
        self.setWindowTitle("File browser")
        self.resize(1100, 700)
        self.back: List[str] = []
        self.forward: List[str] = []
        self.current = ""

        # One model of folders for the tree, one of everything for the list.
        self.dirs = QFileSystemModel(self)
        self.dirs.setRootPath("")
        self.dirs.setFilter(QDir.AllDirs | QDir.NoDotAndDotDot)
        self.files = QFileSystemModel(self)
        self.files.setRootPath("")
        self.files.setNameFilterDisables(False)    # hide, not grey out

        self.tree = QTreeView(headerHidden=True)
        self.tree.setModel(self.dirs)
        for col in (1, 2, 3):
            self.tree.hideColumn(col)
        self.tree.clicked.connect(
            lambda ix: self.go(self.dirs.filePath(ix)))

        self.table = QTableView(showGrid=False, sortingEnabled=True)
        self.table.setModel(self.files)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch)
        self.table.doubleClicked.connect(self._open_item)
        self.table.selectionModel().currentRowChanged.connect(
            lambda ix, _old: self.preview.show_file(self.files.filePath(ix)))
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)

        self.preview = Preview()
        self.bookmarks = self._bookmarks()

        right = QSplitter(Qt.Vertical)
        right.addWidget(self.table)
        right.addWidget(self.preview)
        right.setSizes([420, 260])
        split = QSplitter(Qt.Horizontal)
        for w in (self.bookmarks, self.tree, right):
            split.addWidget(w)
        split.setSizes([150, 260, 690])
        self.setCentralWidget(split)
        self._toolbar()
        self.go(start or QDir.homePath())

    def _toolbar(self) -> None:
        st = self.style()
        tb = self.addToolBar("Navigate")
        tb.setObjectName("navigate")
        self.back_act = QAction(st.standardIcon(QStyle.SP_ArrowBack), "Back",
                                self, shortcut=QKeySequence.Back,
                                triggered=self.go_back)
        self.fwd_act = QAction(st.standardIcon(QStyle.SP_ArrowForward),
                               "Forward", self, shortcut=QKeySequence.Forward,
                               triggered=self.go_forward)
        up = QAction(st.standardIcon(QStyle.SP_FileDialogToParent), "Up",
                     self, shortcut="Alt+Up", triggered=self.go_up)
        tb.addActions([self.back_act, self.fwd_act, up])

        self.path_bar = QLineEdit()
        comp = QCompleter(self)
        comp.setModel(self.dirs)
        comp.setCompletionMode(QCompleter.PopupCompletion)
        self.path_bar.setCompleter(comp)
        self.path_bar.returnPressed.connect(
            lambda: self.go(self.path_bar.text()))
        tb.addWidget(self.path_bar)

        self.pattern = QLineEdit(placeholderText="*.csv *.png",
                                 clearButtonEnabled=True, maximumWidth=180)
        self.pattern.textChanged.connect(self._filter)
        tb.addWidget(self.pattern)
        self.hidden = QCheckBox("Hidden")
        self.hidden.toggled.connect(self._filter)
        tb.addWidget(self.hidden)
        self._filter()

    def _bookmarks(self) -> QListWidget:
        icons = QFileIconProvider()
        lw = QListWidget()
        places = [("Home", QDir.homePath()), ("Root", QDir.rootPath()),
                  ("Here", str(Path.cwd()))]
        places += [(d.absolutePath(), d.absolutePath())
                   for d in QDir.drives()][:6]
        for name, path in places:
            item = QListWidgetItem(icons.icon(QFileIconProvider.Folder), name)
            item.setData(Qt.UserRole, path)
            item.setToolTip(path)
            lw.addItem(item)
        lw.itemClicked.connect(lambda it: self.go(it.data(Qt.UserRole)))
        return lw

    # ---------------------------------------------------------- navigation
    def go(self, path: str, record: bool = True) -> None:
        info = QFileInfo(path)
        if not info.isDir():
            self.statusBar().showMessage(f"Not a folder: {path}")
            return
        path = info.absoluteFilePath()
        if record and self.current and path != self.current:
            self.back.append(self.current)
            self.forward.clear()
        self.current = path
        self.table.setRootIndex(self.files.index(path))
        ix = self.dirs.index(path)
        self.tree.setCurrentIndex(ix)
        self.tree.scrollTo(ix)
        self.path_bar.setText(path)
        self.back_act.setEnabled(bool(self.back))
        self.fwd_act.setEnabled(bool(self.forward))
        self.statusBar().showMessage(path)

    def go_back(self) -> None:
        if self.back:
            self.forward.append(self.current)
            self.go(self.back.pop(), record=False)

    def go_forward(self) -> None:
        if self.forward:
            self.back.append(self.current)
            self.go(self.forward.pop(), record=False)

    def go_up(self) -> None:
        self.go(str(Path(self.current).parent))

    def _open_item(self, ix) -> None:
        path = self.files.filePath(ix)
        if QFileInfo(path).isDir():
            self.go(path)

    def _filter(self, *_: object) -> None:
        flags = QDir.AllEntries | QDir.NoDotAndDotDot
        if self.hidden.isChecked():
            flags |= QDir.Hidden
        self.files.setFilter(flags)
        self.files.setNameFilters(self.pattern.text().split())

    def _menu(self, pos) -> None:
        ix = self.table.indexAt(pos)
        if not ix.isValid():
            return
        path = self.files.filePath(ix)
        menu = QMenu(self)
        menu.addAction("Copy path",
                       lambda: QApplication.clipboard().setText(path))
        menu.addAction("Copy name", lambda: QApplication.clipboard().setText(
            Path(path).name))
        if QFileInfo(path).isDir():
            menu.addAction("Open", lambda: self.go(path))
        menu.exec(self.table.viewport().mapToGlobal(pos))


def make_window(argv: Optional[List[str]] = None) -> BrowserWindow:
    args = argv if argv is not None else sys.argv[1:]
    return BrowserWindow(args[0] if args else None)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    win = make_window()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
