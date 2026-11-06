"""
council_qt.widgets.code_editor — the Build tab's editor: line numbers,
syntax colours, indentation that follows the code, and a find/replace bar.

    CodeEditor       a QPlainTextEdit with a line-number gutter, the current
                     line marked, Tab / Shift+Tab indenting the selected
                     lines by four spaces, Enter keeping the indentation (one
                     level more after a line ending in ':'), and Ctrl+/
                     commenting the selected lines in Python.
    CodeHighlighter  colours the text from council_core.code_highlight's
                     spans, recomputed a moment after typing stops (the
                     spans are for the WHOLE text; see that module).
    FindBar          find next / previous, match case, replace, replace all.

Colours come from the theme's tokens plus fixed hues for the kinds the
theme has no word for, chosen to read on the dark theme's background.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from PySide6.QtCore import QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QKeySequence,
                           QPainter, QSyntaxHighlighter, QTextCharFormat,
                           QTextCursor, QTextDocument, QTextFormat)
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QPushButton, QTextEdit,
                               QWidget)

from council_core import code_highlight as ch

from .. import theme

INDENT = "    "
#: How long typing must pause before the colours are recomputed.
REHIGHLIGHT_MS = 150

#: The hue for each kind of span, on the dark theme.
_HUES = {
    "keyword": "#c586c0", "constant": "#569cd6", "string": "#ce9178",
    "comment": "#6a9955", "number": "#b5cea8", "definition": "#dcdcaa",
    "builtin": "#4ec9b0", "decorator": "#d7ba7d", "operator": "#d4d4d4",
    "heading": "#569cd6", "tag": "#569cd6", "attribute": "#9cdcfe",
}


def formats() -> Dict[str, QTextCharFormat]:
    """A text format for every span kind."""
    out: Dict[str, QTextCharFormat] = {}
    for kind in ch.KINDS:
        f = QTextCharFormat()
        f.setForeground(QColor(_HUES.get(kind, theme.tokens("dark")["fg"])))
        if kind in ("comment",):
            f.setFontItalic(True)
        if kind in ("heading", "definition"):
            f.setFontWeight(QFont.Weight.DemiBold)
        out[kind] = f
    return out


def mono_font(size: int = 10) -> QFont:
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font.setPointSize(size)
    return font


class CodeHighlighter(QSyntaxHighlighter):
    """Paints each line with its share of the whole text's spans."""

    def __init__(self, document: QTextDocument, filename: str = ""):
        super().__init__(document)
        self._formats = formats()
        self._spans: List[ch.Span] = []
        self._ends: List[int] = []
        self._lexer = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(REHIGHLIGHT_MS)
        self._timer.timeout.connect(self.refresh)
        document.contentsChange.connect(self._on_change)
        self.set_filename(filename)

    def set_filename(self, filename: str) -> None:
        self.filename = filename or ""
        self._lexer = ch.lexer_for(self.filename)
        self.refresh()

    def _on_change(self, _pos: int, removed: int, added: int) -> None:
        if removed or added:              # a format change reports 0, 0
            self._timer.start()

    def refresh(self) -> None:
        """Recompute the spans now and repaint."""
        self._timer.stop()
        text = self.document().toPlainText()
        self._spans = ch.spans(text, self.filename, self._lexer) \
            if self._lexer is not None else []
        self._ends = [s + n for s, n, _k in self._spans]
        self.rehighlight()

    def spans(self) -> List[ch.Span]:
        return list(self._spans)

    def highlightBlock(self, text: str) -> None:            # noqa: N802
        if not self._spans:
            return
        start = self.currentBlock().position()
        for offset, length, kind in ch.in_range(self._spans, self._ends,
                                                start, len(text)):
            fmt = self._formats.get(kind)
            if fmt is not None:
                self.setFormat(offset, length, fmt)


class _Gutter(QWidget):
    def __init__(self, editor: "CodeEditor"):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self) -> QSize:                           # noqa: N802
        return QSize(self.editor.gutter_width(), 0)

    def paintEvent(self, event) -> None:                   # noqa: N802
        self.editor.paint_gutter(event)


class CodeEditor(QPlainTextEdit):
    """A plain-text code editor. ``filename`` picks the colours."""

    #: Ctrl+S and Ctrl+F, for the tab that holds the editor.
    save_requested = Signal()
    find_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None, filename: str = ""):
        super().__init__(parent)
        t = theme.tokens("dark")
        self._t = t
        self.setFont(mono_font())
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.gutter = _Gutter(self)
        self.blockCountChanged.connect(self._fit_gutter)
        self.updateRequest.connect(self._scroll_gutter)
        self.cursorPositionChanged.connect(self._mark_line)
        self.highlighter = CodeHighlighter(self.document(), filename)
        self._fit_gutter()
        self._mark_line()

    # -- the file -------------------------------------------------------
    @property
    def filename(self) -> str:
        return self.highlighter.filename

    def set_filename(self, filename: str) -> None:
        self.highlighter.set_filename(filename)

    def is_python(self) -> bool:
        return self.filename.lower().endswith((".py", ".pyw"))

    # -- the gutter -----------------------------------------------------
    def gutter_width(self) -> int:
        digits = max(3, len(str(max(1, self.blockCount()))))
        return 12 + self.fontMetrics().horizontalAdvance("9") * digits

    def _fit_gutter(self, *_a) -> None:
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)

    def _scroll_gutter(self, rect: QRect, dy: int) -> None:
        if dy:
            self.gutter.scroll(0, dy)
        else:
            self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._fit_gutter()

    def resizeEvent(self, event) -> None:                  # noqa: N802
        super().resizeEvent(event)
        r = self.contentsRect()
        self.gutter.setGeometry(QRect(r.left(), r.top(), self.gutter_width(), r.height()))

    def paint_gutter(self, event) -> None:
        painter = QPainter(self.gutter)
        painter.fillRect(event.rect(), QColor(self._t["panel_bg"]))
        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        current = self.textCursor().blockNumber()
        height = self.fontMetrics().height()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.setPen(QColor(self._t["fg"] if number == current
                                      else self._t["muted_fg"]))
                painter.drawText(0, top, self.gutter.width() - 6, height,
                                 Qt.AlignmentFlag.AlignRight, str(number + 1))
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            number += 1

    def _mark_line(self) -> None:
        line = QTextEdit.ExtraSelection()
        line.format.setBackground(QColor(self._t["panel_bg"]))
        line.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        line.cursor = self.textCursor()
        line.cursor.clearSelection()
        self.setExtraSelections([line])

    # -- keys -------------------------------------------------------------
    def keyPressEvent(self, event) -> None:                # noqa: N802
        key, mods = event.key(), event.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        if event.matches(QKeySequence.StandardKey.Save):
            self.save_requested.emit()
            return
        if event.matches(QKeySequence.StandardKey.Find):
            self.find_requested.emit()
            return
        if key == Qt.Key.Key_Tab and not ctrl:
            if self.textCursor().hasSelection():
                self.indent_lines()
            else:
                self.insertPlainText(INDENT)
            return
        if key == Qt.Key.Key_Backtab:
            self.dedent_lines()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not ctrl:
            self.new_line()
            return
        if ctrl and key == Qt.Key.Key_Slash:
            self.toggle_comment()
            return
        super().keyPressEvent(event)

    def _selected_blocks(self):
        cur = self.textCursor()
        doc = self.document()
        first = doc.findBlock(cur.selectionStart())
        last_pos = cur.selectionEnd()
        last = doc.findBlock(last_pos)
        # a selection ending at the very start of a line does not take it in
        if cur.hasSelection() and last_pos == last.position() and last != first:
            last = last.previous()
        blocks = []
        b = first
        while b.isValid():
            blocks.append(b)
            if b == last:
                break
            b = b.next()
        return blocks

    def _edit_lines(self, change) -> None:
        """Apply ``change(line_text) -> new_text`` to each selected line, as
        ONE undo step, and keep those lines selected."""
        blocks = self._selected_blocks()
        cur = QTextCursor(self.document())
        cur.beginEditBlock()
        for b in blocks:
            new = change(b.text())
            if new != b.text():
                c = QTextCursor(b)
                c.movePosition(QTextCursor.MoveOperation.EndOfBlock,
                               QTextCursor.MoveMode.KeepAnchor)
                c.insertText(new)
        cur.endEditBlock()
        sel = QTextCursor(self.document())
        sel.setPosition(blocks[0].position())
        end = blocks[-1]
        sel.setPosition(end.position() + len(end.text()), QTextCursor.MoveMode.KeepAnchor)
        self.setTextCursor(sel)

    def indent_lines(self) -> None:
        self._edit_lines(lambda s: INDENT + s if s.strip() else s)

    def dedent_lines(self) -> None:
        def dedent(s: str) -> str:
            n = len(s) - len(s.lstrip(" "))
            if s.startswith("\t"):
                return s[1:]
            return s[min(n, len(INDENT)):]
        self._edit_lines(dedent)

    def toggle_comment(self) -> None:
        """Ctrl+/: '# ' in front of every selected line, or off again when
        they all have it. Python only (the mark differs by language)."""
        if not self.is_python():
            return
        lines = [b.text() for b in self._selected_blocks()]
        code = [s for s in lines if s.strip()]
        if not code:
            return
        off = all(s.lstrip().startswith("#") for s in code)
        col = min(len(s) - len(s.lstrip()) for s in code)

        def change(s: str) -> str:
            if not s.strip():
                return s
            if off:
                body = s[col:]
                body = body[2:] if body.startswith("# ") else body[1:]
                return s[:col] + body
            return s[:col] + "# " + s[col:]
        self._edit_lines(change)

    def new_line(self) -> None:
        """Enter: the new line starts at this line's indentation, one level
        deeper after a ':' (Python's block opener)."""
        cur = self.textCursor()
        line = cur.block().text()[:cur.positionInBlock()]
        indent = line[:len(line) - len(line.lstrip(" \t"))]
        if self.is_python() and line.rstrip().endswith(":"):
            indent += INDENT
        cur.insertText("\n" + indent)
        self.setTextCursor(cur)
        self.ensureCursorVisible()


class FindBar(QWidget):
    """Find and replace in one editor. Hidden until Ctrl+F."""

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.editor: Optional[CodeEditor] = None
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 2, 0, 2)
        row.addWidget(QLabel("Find:"))
        self.find_box = QLineEdit()
        self.find_box.returnPressed.connect(self.find_next)
        row.addWidget(self.find_box, 2)
        self.prev_btn = QPushButton("Previous")
        self.prev_btn.clicked.connect(self.find_previous)
        row.addWidget(self.prev_btn)
        self.next_btn = QPushButton("Next")
        self.next_btn.clicked.connect(self.find_next)
        row.addWidget(self.next_btn)
        self.case_box = QCheckBox("Match case")
        row.addWidget(self.case_box)
        row.addWidget(QLabel("Replace:"))
        self.replace_box = QLineEdit()
        row.addWidget(self.replace_box, 2)
        self.replace_btn = QPushButton("Replace")
        self.replace_btn.clicked.connect(self.replace_one)
        row.addWidget(self.replace_btn)
        self.all_btn = QPushButton("Replace all")
        self.all_btn.clicked.connect(self.replace_all)
        row.addWidget(self.all_btn)
        self.result = QLabel("")
        row.addWidget(self.result)
        close = QPushButton("✕")
        close.setFlat(True)
        close.clicked.connect(self.hide)
        row.addWidget(close)

    def attach(self, editor: Optional[CodeEditor]) -> None:
        self.editor = editor
        self.result.setText("")

    def open(self) -> None:
        self.show()
        if self.editor is not None:
            selected = self.editor.textCursor().selectedText()
            if selected and " " not in selected:
                self.find_box.setText(selected)
        self.find_box.setFocus()
        self.find_box.selectAll()

    def keyPressEvent(self, event) -> None:                # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            if self.editor is not None:
                self.editor.setFocus()
            return
        super().keyPressEvent(event)

    def _flags(self, back: bool = False):
        f = QTextDocument.FindFlag(0)
        if self.case_box.isChecked():
            f |= QTextDocument.FindFlag.FindCaseSensitively
        if back:
            f |= QTextDocument.FindFlag.FindBackward
        return f

    def _find(self, back: bool) -> bool:
        ed, text = self.editor, self.find_box.text()
        if ed is None or not text:
            return False
        if ed.find(text, self._flags(back)):
            self.result.setText("")
            return True
        # wrap around once
        cur = ed.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End if back
                         else QTextCursor.MoveOperation.Start)
        ed.setTextCursor(cur)
        found = ed.find(text, self._flags(back))
        self.result.setText("" if found else "Not found")
        return found

    def find_next(self) -> bool:
        return self._find(False)

    def find_previous(self) -> bool:
        return self._find(True)

    def _matches(self, selected: str) -> bool:
        want = self.find_box.text()
        return (selected == want if self.case_box.isChecked()
                else selected.lower() == want.lower())

    def replace_one(self) -> None:
        ed = self.editor
        if ed is None or not self.find_box.text():
            return
        cur = ed.textCursor()
        if cur.hasSelection() and self._matches(cur.selectedText()):
            cur.insertText(self.replace_box.text())
        self.find_next()

    def replace_all(self) -> int:
        """Every match replaced, as one undo step. Returns how many."""
        ed, want = self.editor, self.find_box.text()
        if ed is None or not want:
            return 0
        doc = ed.document()
        cur = QTextCursor(doc)
        cur.beginEditBlock()
        n = 0
        found = doc.find(want, 0, self._flags())
        while not found.isNull():
            found.insertText(self.replace_box.text())
            n += 1
            found = doc.find(want, found.position(), self._flags())
        cur.endEditBlock()
        self.result.setText(f"{n} replaced")
        return n
