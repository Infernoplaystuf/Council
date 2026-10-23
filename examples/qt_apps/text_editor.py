"""
text_editor.py — a tabbed text and code editor: line numbers, Python
syntax colours, find and replace, recent files, and unsaved-change checks.

WHAT IT DOES
Each open file is a tab (drag tabs to reorder, middle-click or × to close).
Python files get coloured keywords, strings, numbers and comments; every
file gets line numbers and the current line highlighted. Ctrl+F opens the
find bar (match case, whole words, regular expressions; Enter finds next,
Shift+Enter previous); Replace and Replace all work on the current tab.
File > Recent lists the last files opened. A tab with unsaved changes shows
"*", and closing it — or the window — asks first. Ctrl+= / Ctrl+- zoom,
View toggles word wrap, Format > Font picks the font.

SHOWS
  * QMainWindow with a full menu bar, a QToolBar, QActions with standard
    QKeySequence shortcuts, and a QStatusBar with permanent widgets
  * QTabWidget of documents (tabsClosable, movable, documentMode)
  * the line-number-area pattern: a side QWidget painted by the editor,
    kept in step with blockCountChanged / updateRequest / resizeEvent
  * extra selections to highlight the current line
  * QSyntaxHighlighter with QRegularExpression rules and a multi-line
    state (triple-quoted strings) via setCurrentBlockState
  * QTextDocument.find with FindFlags, QTextCursor for replace, and
    beginEditBlock / endEditBlock so Replace all is one undo step
  * an inline find bar under the tabs, shown and hidden, Esc to close
  * document modificationChanged driving tab titles
  * a "recent files" menu rebuilt on aboutToShow, kept in a JSON file
  * QFontDialog, QMessageBox.question with Save / Discard / Cancel
  * drag and drop of files to open them

RUN
    python examples/qt_apps/text_editor.py [file ...]

REQUIRES  nothing beyond PySide6
"""
from __future__ import annotations

import json
import keyword
import sys
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import QRect, QRegularExpression, QSize, QStandardPaths, Qt
from PySide6.QtGui import (QAction, QColor, QFont, QFontDatabase,
                           QKeySequence, QPainter, QSyntaxHighlighter,
                           QTextCharFormat, QTextCursor, QTextDocument,
                           QTextFormat)
from PySide6.QtWidgets import (QApplication, QCheckBox, QFileDialog,
                               QFontDialog, QHBoxLayout, QLabel, QLineEdit,
                               QMainWindow, QMessageBox, QPlainTextEdit,
                               QPushButton, QTabWidget, QTextEdit,
                               QVBoxLayout, QWidget)

MAX_RECENT = 10


def recent_path() -> Path:
    base = QStandardPaths.writableLocation(QStandardPaths.AppConfigLocation)
    return Path(base or Path.home()) / "qt_example_recent.json"


def fmt(colour: str, bold: bool = False, italic: bool = False
        ) -> QTextCharFormat:
    f = QTextCharFormat()
    f.setForeground(QColor(colour))
    if bold:
        f.setFontWeight(QFont.Bold)
    f.setFontItalic(italic)
    return f


class PythonHighlighter(QSyntaxHighlighter):
    STATE_TRIPLE = 1

    def __init__(self, doc: QTextDocument):
        super().__init__(doc)
        words = "|".join(keyword.kwlist)
        self.rules = [
            (QRegularExpression(rf"\b({words})\b"), fmt("#0033b3", bold=True)),
            (QRegularExpression(r"\b(self|cls|True|False|None)\b"),
             fmt("#94558d")),
            (QRegularExpression(r"\b[0-9]+(\.[0-9]+)?\b"), fmt("#1750eb")),
            (QRegularExpression(r"\bdef\s+(\w+)|\bclass\s+(\w+)"),
             fmt("#00627a", bold=True)),
            (QRegularExpression(r"@\w+"), fmt("#9e880d")),
            (QRegularExpression(r"\"[^\"\n]*\"|'[^'\n]*'"), fmt("#067d17")),
            (QRegularExpression(r"#[^\n]*"), fmt("#8c8c8c", italic=True)),
        ]
        self.triple = QRegularExpression(r'"""|\'\'\'')
        self.triple_fmt = fmt("#067d17")

    def highlightBlock(self, text: str) -> None:
        for rx, f in self.rules:
            it = rx.globalMatch(text)
            while it.hasNext():
                m = it.next()
                self.setFormat(m.capturedStart(), m.capturedLength(), f)
        # Triple-quoted strings may span blocks: carry the state forward.
        self.setCurrentBlockState(0)
        if self.previousBlockState() == self.STATE_TRIPLE:
            start, search = 0, 0            # inside a string from the start
        else:
            m = self.triple.match(text)
            start = m.capturedStart() if m.hasMatch() else -1
            search = start + 3
        while start >= 0:
            m = self.triple.match(text, search)
            if not m.hasMatch():
                self.setCurrentBlockState(self.STATE_TRIPLE)
                self.setFormat(start, len(text) - start, self.triple_fmt)
                break
            end = m.capturedEnd()
            self.setFormat(start, end - start, self.triple_fmt)
            nxt = self.triple.match(text, end)
            start = nxt.capturedStart() if nxt.hasMatch() else -1
            search = start + 3


class LineNumbers(QWidget):
    def __init__(self, editor: "CodeEditor"):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self.editor.number_width(), 0)

    def paintEvent(self, event) -> None:
        self.editor.paint_numbers(event)


class CodeEditor(QPlainTextEdit):
    def __init__(self, path: Optional[Path] = None):
        super().__init__()
        self.path = path
        self.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.numbers = LineNumbers(self)
        self.blockCountChanged.connect(self._update_width)
        self.updateRequest.connect(self._update_numbers)
        self.cursorPositionChanged.connect(self._highlight_line)
        self._update_width()
        self._highlight_line()
        self.highlighter: Optional[PythonHighlighter] = None
        if path is not None:
            self.setPlainText(path.read_text(encoding="utf-8",
                                             errors="replace"))
            self.document().setModified(False)
        self.set_language()

    def set_language(self) -> None:
        if self.path is not None and self.path.suffix in (".py", ".pyw"):
            self.highlighter = PythonHighlighter(self.document())

    def title(self) -> str:
        return self.path.name if self.path else "untitled"

    # ---- line numbers
    def number_width(self) -> int:
        digits = len(str(max(1, self.blockCount())))
        return 10 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_width(self, *_: object) -> None:
        self.setViewportMargins(self.number_width(), 0, 0, 0)

    def _update_numbers(self, rect: QRect, dy: int) -> None:
        if dy:
            self.numbers.scroll(0, dy)
        else:
            self.numbers.update(0, rect.y(), self.numbers.width(),
                                rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_width()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        r = self.contentsRect()
        self.numbers.setGeometry(QRect(r.left(), r.top(),
                                       self.number_width(), r.height()))

    def paint_numbers(self, event) -> None:
        p = QPainter(self.numbers)
        p.fillRect(event.rect(), self.palette().alternateBase())
        block = self.firstVisibleBlock()
        top = round(self.blockBoundingGeometry(block)
                    .translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        current = self.textCursor().blockNumber()
        h = self.fontMetrics().height()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                n = block.blockNumber()
                p.setPen(self.palette().text().color() if n == current
                         else QColor("#9a9a9a"))
                p.drawText(0, top, self.numbers.width() - 5, h,
                           Qt.AlignRight, str(n + 1))
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())

    def _highlight_line(self) -> None:
        sel = QTextEdit.ExtraSelection()
        colour = self.palette().highlight().color()
        colour.setAlpha(30)
        sel.format.setBackground(colour)
        sel.format.setProperty(QTextFormat.FullWidthSelection, True)
        sel.cursor = self.textCursor()
        sel.cursor.clearSelection()
        self.setExtraSelections([sel])


class FindBar(QWidget):
    def __init__(self, window: "EditorWindow"):
        super().__init__()
        self.win = window
        self.find = QLineEdit(placeholderText="Find")
        self.replace = QLineEdit(placeholderText="Replace with")
        self.case = QCheckBox("Match case")
        self.words = QCheckBox("Whole words")
        self.regex = QCheckBox("Regex")
        self.find.returnPressed.connect(lambda: self.next(forward=True))
        row1, row2 = QHBoxLayout(), QHBoxLayout()
        row1.addWidget(self.find, 1)
        for text, slot in (("Previous", lambda: self.next(False)),
                           ("Next", lambda: self.next(True))):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row1.addWidget(b)
        for w in (self.case, self.words, self.regex):
            row1.addWidget(w)
        close = QPushButton("×", flat=True, maximumWidth=28)
        close.clicked.connect(self.hide)
        row1.addWidget(close)
        row2.addWidget(self.replace, 1)
        for text, slot in (("Replace", self.replace_one),
                           ("Replace all", self.replace_all)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row2.addWidget(b)
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 2, 4, 2)
        v.addLayout(row1)
        v.addLayout(row2)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key_Escape:
            self.hide()
        elif event.key() in (Qt.Key_Return, Qt.Key_Enter) \
                and event.modifiers() & Qt.ShiftModifier:
            self.next(forward=False)
        else:
            super().keyPressEvent(event)

    def flags(self, forward: bool) -> QTextDocument.FindFlag:
        f = QTextDocument.FindFlag(0)
        if not forward:
            f |= QTextDocument.FindBackward
        if self.case.isChecked():
            f |= QTextDocument.FindCaseSensitively
        if self.words.isChecked():
            f |= QTextDocument.FindWholeWords
        return f

    def pattern(self):
        text = self.find.text()
        if self.regex.isChecked():
            rx = QRegularExpression(text)
            if not self.case.isChecked():
                rx.setPatternOptions(
                    QRegularExpression.CaseInsensitiveOption)
            return rx
        return text

    def next(self, forward: bool = True) -> bool:
        ed = self.win.editor()
        if ed is None or not self.find.text():
            return False
        found = ed.find(self.pattern(), self.flags(forward))
        if not found:                              # wrap around
            cur = ed.textCursor()
            cur.movePosition(QTextCursor.Start if forward
                             else QTextCursor.End)
            ed.setTextCursor(cur)
            found = ed.find(self.pattern(), self.flags(forward))
        self.win.statusBar().showMessage("" if found else "Not found", 2000)
        return found

    def replace_one(self) -> None:
        ed = self.win.editor()
        if ed is None:
            return
        if ed.textCursor().hasSelection():
            ed.textCursor().insertText(self.replace.text())
        self.next(True)

    def replace_all(self) -> int:
        ed = self.win.editor()
        if ed is None or not self.find.text():
            return 0
        doc = ed.document()
        cur = QTextCursor(doc)
        n = 0
        cur.beginEditBlock()                       # one undo step
        while True:
            hit = doc.find(self.pattern(), cur, self.flags(True))
            if hit.isNull():
                break
            hit.insertText(self.replace.text())
            cur.setPosition(hit.position())
            n += 1
        cur.endEditBlock()
        self.win.statusBar().showMessage(f"Replaced {n}", 3000)
        return n


class EditorWindow(QMainWindow):
    def __init__(self, files: Optional[List[Path]] = None,
                 recent_file: Optional[Path] = None):
        super().__init__()
        self.setWindowTitle("Text editor")
        self.resize(1000, 700)
        self.setAcceptDrops(True)
        self.recent_file = recent_file or recent_path()

        self.tabs = QTabWidget(tabsClosable=True, movable=True,
                               documentMode=True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.find_bar = FindBar(self)
        self.find_bar.hide()
        central = QWidget()
        v = QVBoxLayout(central)
        v.setContentsMargins(0, 0, 0, 0)
        v.addWidget(self.tabs, 1)
        v.addWidget(self.find_bar)
        self.setCentralWidget(central)

        self.position = QLabel()
        self.statusBar().addPermanentWidget(self.position)
        self._menus()
        for f in files or []:
            self.open_path(f)
        if not self.tabs.count():
            self.new_tab()

    def _menus(self) -> None:
        mb = self.menuBar()
        tb = self.addToolBar("File")
        tb.setObjectName("file_toolbar")

        def add(menu, text, slot, keys=None, toolbar=False) -> QAction:
            a = QAction(text, self)
            if keys is not None:
                a.setShortcut(keys)
            a.triggered.connect(slot)
            menu.addAction(a)
            if toolbar:
                tb.addAction(a)
            return a

        fm = mb.addMenu("&File")
        add(fm, "New", self.new_tab, QKeySequence.New, True)
        add(fm, "Open…", self.open_dialog, QKeySequence.Open, True)
        self.recent_menu = fm.addMenu("Recent")
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        add(fm, "Save", self.save, QKeySequence.Save, True)
        add(fm, "Save as…", self.save_as, QKeySequence.SaveAs)
        add(fm, "Close tab", lambda: self.close_tab(self.tabs.currentIndex()),
            QKeySequence.Close)
        fm.addSeparator()
        add(fm, "Quit", self.close, QKeySequence.Quit)

        em = mb.addMenu("&Edit")
        for text, name, keys in (("Undo", "undo", QKeySequence.Undo),
                                 ("Redo", "redo", QKeySequence.Redo),
                                 ("Cut", "cut", QKeySequence.Cut),
                                 ("Copy", "copy", QKeySequence.Copy),
                                 ("Paste", "paste", QKeySequence.Paste),
                                 ("Select all", "selectAll",
                                  QKeySequence.SelectAll)):
            add(em, text, lambda _=False, n=name: self._on_editor(n), keys)
        em.addSeparator()
        add(em, "Find / replace…", self.show_find, QKeySequence.Find, True)
        add(em, "Find next", lambda: self.find_bar.next(True),
            QKeySequence.FindNext)

        vm = mb.addMenu("&View")
        self.wrap = QAction("Word wrap", self, checkable=True)
        self.wrap.toggled.connect(self._apply_wrap)
        vm.addAction(self.wrap)
        add(vm, "Zoom in", lambda: self._on_editor("zoomIn"),
            QKeySequence.ZoomIn)
        add(vm, "Zoom out", lambda: self._on_editor("zoomOut"),
            QKeySequence.ZoomOut)
        add(mb.addMenu("F&ormat"), "Font…", self.pick_font)

    # ---------------------------------------------------------- tabs
    def editor(self) -> Optional[CodeEditor]:
        w = self.tabs.currentWidget()
        return w if isinstance(w, CodeEditor) else None

    def _on_editor(self, method: str) -> None:
        ed = self.editor()
        if ed is not None:
            getattr(ed, method)()

    def add_editor(self, ed: CodeEditor) -> None:
        i = self.tabs.addTab(ed, ed.title())
        self.tabs.setTabToolTip(i, str(ed.path or ""))
        ed.document().modificationChanged.connect(
            lambda _m, e=ed: self._retitle(e))
        ed.cursorPositionChanged.connect(self._show_position)
        self._apply_wrap(self.wrap.isChecked(), ed)
        self.tabs.setCurrentIndex(i)
        ed.setFocus()

    def new_tab(self) -> None:
        self.add_editor(CodeEditor())

    def _retitle(self, ed: CodeEditor) -> None:
        i = self.tabs.indexOf(ed)
        star = "*" if ed.document().isModified() else ""
        self.tabs.setTabText(i, ed.title() + star)
        if ed is self.editor():
            self._tab_changed()

    def _tab_changed(self, *_: object) -> None:
        ed = self.editor()
        if ed is not None:
            star = "*" if ed.document().isModified() else ""
            self.setWindowTitle(f"{ed.title()}{star} — Text editor")
            self._show_position()

    def _show_position(self) -> None:
        ed = self.editor()
        if ed is not None:
            c = ed.textCursor()
            self.position.setText(f"Ln {c.blockNumber() + 1}, "
                                  f"Col {c.positionInBlock() + 1}")

    def close_tab(self, i: int) -> bool:
        ed = self.tabs.widget(i)
        if not isinstance(ed, CodeEditor) or not self.maybe_save(ed):
            return False
        self.tabs.removeTab(i)
        ed.deleteLater()
        if not self.tabs.count():
            self.new_tab()
        return True

    # ---------------------------------------------------------- files
    def open_dialog(self) -> None:
        names, _ = QFileDialog.getOpenFileNames(self, "Open", "",
                                                "Text (*.txt *.md *.py *.json "
                                                "*.csv *.ini);;All (*)")
        for n in names:
            self.open_path(Path(n))

    def open_path(self, path: Path) -> None:
        path = path.resolve()
        for i in range(self.tabs.count()):                # already open
            ed = self.tabs.widget(i)
            if isinstance(ed, CodeEditor) and ed.path == path:
                self.tabs.setCurrentIndex(i)
                return
        try:
            ed = CodeEditor(path)
        except OSError as exc:
            QMessageBox.warning(self, "Could not open", str(exc))
            return
        # Replace an untouched empty first tab.
        cur = self.editor()
        if cur is not None and cur.path is None and not cur.toPlainText() \
                and self.tabs.count() == 1:
            self.tabs.removeTab(0)
            cur.deleteLater()
        self.add_editor(ed)
        self._remember(path)

    def save(self, ed: Optional[CodeEditor] = None) -> bool:
        ed = ed if isinstance(ed, CodeEditor) else self.editor()
        if ed is None:
            return False
        if ed.path is None:
            return self.save_as(ed)
        ed.path.write_text(ed.toPlainText(), encoding="utf-8")
        ed.document().setModified(False)
        self.statusBar().showMessage(f"Saved {ed.path}", 3000)
        return True

    def save_as(self, ed: Optional[CodeEditor] = None) -> bool:
        ed = ed if isinstance(ed, CodeEditor) else self.editor()
        if ed is None:
            return False
        name, _ = QFileDialog.getSaveFileName(self, "Save as", ed.title())
        if not name:
            return False
        ed.path = Path(name).resolve()
        ed.set_language()
        if ed.highlighter is not None:
            ed.highlighter.rehighlight()
        self._remember(ed.path)
        ok = self.save(ed)
        self._retitle(ed)
        return ok

    def maybe_save(self, ed: CodeEditor) -> bool:
        if not ed.document().isModified():
            return True
        self.tabs.setCurrentWidget(ed)
        answer = QMessageBox.question(
            self, "Unsaved changes", f"Save changes to {ed.title()}?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if answer == QMessageBox.Save:
            return self.save(ed)
        return answer == QMessageBox.Discard

    def closeEvent(self, event) -> None:
        for i in range(self.tabs.count()):
            ed = self.tabs.widget(i)
            if isinstance(ed, CodeEditor) and not self.maybe_save(ed):
                event.ignore()
                return
        event.accept()

    # ---------------------------------------------------------- recent
    def recent(self) -> List[str]:
        try:
            data = json.loads(self.recent_file.read_text(encoding="utf-8"))
            return [str(p) for p in data][:MAX_RECENT]
        except (OSError, ValueError, TypeError):
            return []

    def _remember(self, path: Path) -> None:
        items = [str(path)] + [p for p in self.recent() if p != str(path)]
        try:
            self.recent_file.parent.mkdir(parents=True, exist_ok=True)
            self.recent_file.write_text(json.dumps(items[:MAX_RECENT]),
                                        encoding="utf-8")
        except OSError:
            pass                            # a recent list is a nicety

    def _fill_recent(self) -> None:
        self.recent_menu.clear()
        items = [p for p in self.recent() if Path(p).is_file()]
        for p in items:
            self.recent_menu.addAction(Path(p).name,
                                       lambda p=p: self.open_path(Path(p))
                                       ).setToolTip(p)
        if not items:
            self.recent_menu.addAction("(none)").setEnabled(False)

    # ---------------------------------------------------------- view
    def show_find(self) -> None:
        ed = self.editor()
        if ed is not None and ed.textCursor().hasSelection():
            self.find_bar.find.setText(ed.textCursor().selectedText())
        self.find_bar.show()
        self.find_bar.find.setFocus()
        self.find_bar.find.selectAll()

    def _apply_wrap(self, on: bool, ed: Optional[CodeEditor] = None) -> None:
        targets = [ed] if ed is not None else [
            self.tabs.widget(i) for i in range(self.tabs.count())]
        for e in targets:
            e.setLineWrapMode(QPlainTextEdit.WidgetWidth if on
                              else QPlainTextEdit.NoWrap)

    def pick_font(self) -> None:
        ed = self.editor()
        ok, font = QFontDialog.getFont(ed.font() if ed else self.font(), self)
        if ok:
            for i in range(self.tabs.count()):
                self.tabs.widget(i).setFont(font)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        for u in event.mimeData().urls():
            p = Path(u.toLocalFile())
            if p.is_file():
                self.open_path(p)


def make_window(argv: Optional[List[str]] = None) -> EditorWindow:
    args = argv if argv is not None else sys.argv[1:]
    return EditorWindow([Path(a) for a in args])


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    win = make_window()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
