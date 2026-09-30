"""
council_qt.widgets.python_scripts — the Settings menu, and the window that
lists every Python file a generated app uses.

The Qt half of gui_settings, which a generated app's Settings button calls.
Everything the window SAYS comes from there (gui_settings.scripts_in_use,
plain Python, testable with no display); this module only lays it out, the
same split frame_camera keeps with pop_out.

THE MENU IS A POPUP, NEVER exec(). exec() runs a nested event loop, and a
Typhon capture's live view and status line are driven by timers on the main
one — a Settings menu left open would freeze the picture. popup() returns at
once and the app carries on underneath.

UNDER THE BUTTON THAT WAS PRESSED. A script link cannot pass its widget, so
the menu looks for it: the button under the mouse — unless the keyboard
focus is on another button, which was then pressed with Space (see
pressed_button) — else it opens at the mouse. It
drops down from the button's bottom edge, and right-aligned when a left
alignment would run off the window — a Settings button lives in a top-right
corner.

THE WINDOW IS NON-MODAL AND KEPT. It stays open beside the running app, can
be filtered, and "Copy all" puts the whole list — interpreter first — on the
clipboard for a bug report. Every cell is selectable, and Ctrl+C copies the
selected rows.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QCursor, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractButton, QAbstractItemView,
                               QApplication, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMenu, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

#: The table's columns, left to right.
COLUMNS = ("Name", "What it does", "Loaded", "Path")
NAME, WHAT, LOADED, PATH = range(4)


# ======================================================================
# The menu
# ======================================================================
def pressed_button() -> Optional[QAbstractButton]:
    """The button that was just pressed, as far as it can be told.

    The one under the mouse and the one with the keyboard focus are the
    witnesses. For a click they agree: a button takes the focus when it is
    clicked. When they DISAGREE, the button under the mouse was not clicked
    — or it would have the focus — so the focused one was pressed from the
    keyboard (Space, with the mouse resting over some other button). Only a
    button that takes no focus on a click leaves the focus elsewhere when it
    is clicked; then the mouse is believed.
    """
    if QApplication.instance() is None:
        return None
    under = first_button([QApplication.widgetAt(QCursor.pos())])
    focus = first_button([QApplication.focusWidget()])
    if under is None or focus is None or under is focus:
        return under or focus
    takes_click_focus = bool(under.focusPolicy()
                             & Qt.FocusPolicy.ClickFocus)
    return focus if takes_click_focus else under


def first_button(candidates: Sequence[Optional[QWidget]]
                 ) -> Optional[QAbstractButton]:
    """The first candidate that is (or sits inside) a visible button.

    widgetAt can answer with a child of the button (a label inside a styled
    one), so each candidate's parents are tried too, a few levels up — not
    all the way, or any widget inside a panel would find some button."""
    for widget in candidates:
        hops = 0
        while widget is not None and hops < 3:
            if isinstance(widget, QAbstractButton) and widget.isVisible():
                return widget
            widget = widget.parentWidget()
            hops += 1
    return None


def drop_point(button: Optional[QWidget], width: int) -> QPoint:
    """Where a menu `width` wide should open under `button` (global
    coordinates) — or at the mouse when there is no button."""
    if button is None:
        return QCursor.pos()
    below = button.mapToGlobal(QPoint(0, button.height()))
    window = button.window()
    right = window.mapToGlobal(QPoint(window.width(), 0)).x()
    if below.x() + width > right:
        # Right-aligned with the button instead: a corner button's menu
        # would otherwise hang off the window's edge.
        below.setX(button.mapToGlobal(QPoint(button.width(), 0)).x() - width)
    return below


def settings_menu(items: Sequence[Tuple[str, Callable[..., Any]]],
                  button: Optional[QWidget] = None) -> Tuple[QMenu, QPoint]:
    """(the menu, where to pop it) for `items` = [(label, callback)].

    Built but NOT shown: the caller pops it (non-blocking) or, under
    COUNCIL_NO_DIALOGS, holds it for a test to inspect. Parented to the
    app's window so it takes the app's look and closes with it.
    """
    button = button if button is not None else pressed_button()
    parent = button.window() if button is not None else (
        QApplication.activeWindow())
    menu = QMenu(parent)
    menu.setObjectName("council_settings_menu")
    for label, callback in items:
        action = menu.addAction(label)
        # A window opened from the menu belongs to the app's window.
        action.triggered.connect(
            lambda _checked=False, cb=callback: cb(parent))
    menu.ensurePolished()
    return menu, drop_point(button, menu.sizeHint().width())


# ======================================================================
# The window
# ======================================================================
class ScriptsWindow(QWidget):
    """Every Python file the app uses: name, what it does, loaded, path.

    `load` returns the rows (gui_settings.Script — name, path, what, group,
    loaded); `header` the lines above the table (the interpreter first);
    `titles` names each group. Both are called again by Refresh, because
    what is loaded changes as the app is used.
    """

    def __init__(self, load: Callable[[], Sequence[Any]],
                 header: Callable[[], List[str]],
                 titles: Optional[Dict[str, str]] = None,
                 parent: Optional[QWidget] = None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        title = parent.windowTitle() if parent is not None else ""
        self.setWindowTitle(f"Python Scripts — {title}" if title
                            else "Python Scripts")
        self._load, self._header = load, header
        self._titles = dict(titles or {})
        self.rows: List[Any] = []
        #: Seconds the last list took to build — shown, so its cost is seen.
        self.seconds = 0.0

        outer = QVBoxLayout(self)
        self.interpreter = QLabel(self)
        self.interpreter.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.interpreter.setWordWrap(True)
        outer.addWidget(self.interpreter)

        bar = QHBoxLayout()
        self.filter = QLineEdit(self)
        self.filter.setPlaceholderText("Filter by name, path or what it does")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self.apply_filter)
        bar.addWidget(self.filter, 1)
        self.copy_btn = QPushButton("Copy all", self)
        self.copy_btn.setToolTip("The interpreter and every row, as text, "
                                 "for a bug report")
        self.copy_btn.clicked.connect(self.copy_all)
        bar.addWidget(self.copy_btn)
        self.refresh_btn = QPushButton("Refresh", self)
        self.refresh_btn.setToolTip("Look again — what is loaded changes as "
                                    "the app is used")
        self.refresh_btn.clicked.connect(self.refresh)
        bar.addWidget(self.refresh_btn)
        outer.addLayout(bar)

        self.table = QTableWidget(0, len(COLUMNS), self)
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setWordWrap(False)
        # A long path keeps both ends — the drive and the FILE NAME — and
        # loses the middle; the full text is in each cell's tooltip.
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.verticalHeader().setDefaultSectionSize(
            self.fontMetrics().height() + 8)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(NAME, QHeaderView.ResizeMode.Interactive)
        head.setSectionResizeMode(WHAT, QHeaderView.ResizeMode.Interactive)
        head.setSectionResizeMode(LOADED,
                                  QHeaderView.ResizeMode.ResizeToContents)
        head.setStretchLastSection(True)
        outer.addWidget(self.table, 1)

        self.status = QLabel(self)
        outer.addWidget(self.status)
        QShortcut(QKeySequence(QKeySequence.StandardKey.Copy), self.table,
                  self.copy_selected)

        self.setMinimumSize(640, 320)
        self.resize(1180, 600)
        self.refresh()
        self.table.setColumnWidth(NAME, 230)
        self.table.setColumnWidth(WHAT, 520)

    # -- filling ------------------------------------------------------------
    @property
    def count(self) -> int:
        return len(self.rows)

    def refresh(self) -> None:
        """Build the list again and redraw it, keeping the filter."""
        started = time.perf_counter()
        self.rows = list(self._load())
        self.seconds = time.perf_counter() - started
        self.interpreter.setText("\n".join(self._header()))
        self._fill()
        self.apply_filter(self.filter.text())

    def _fill(self) -> None:
        table = self.table
        table.setUpdatesEnabled(False)
        try:
            table.clearSpans()
            table.setRowCount(0)
            group = None
            for script in self.rows:
                if script.group != group:
                    group = script.group
                    self._add_heading(self._titles.get(group, group))
                row = table.rowCount()
                table.insertRow(row)
                values = (script.name, script.what,
                          "yes" if script.loaded else "not yet",
                          str(script.path))
                for column, text in enumerate(values):
                    item = QTableWidgetItem(text)
                    item.setFlags(Qt.ItemFlag.ItemIsSelectable
                                  | Qt.ItemFlag.ItemIsEnabled)
                    item.setToolTip(script.what if column == WHAT else text)
                    item.setData(Qt.ItemDataRole.UserRole, script)
                    table.setItem(row, column, item)
        finally:
            table.setUpdatesEnabled(True)
        loaded = sum(1 for s in self.rows if s.loaded)
        self.status.setText(f"{self.count} Python files · {loaded} loaded · "
                            f"listed in {self.seconds * 1000:.0f} ms")

    def _add_heading(self, text: str) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        item = QTableWidgetItem(text)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        self.table.setItem(row, 0, item)
        self.table.setSpan(row, 0, 1, len(COLUMNS))

    # -- filtering and copying ---------------------------------------------
    def _script_at(self, row: int) -> Any:
        item = self.table.item(row, NAME)
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def apply_filter(self, text: str = "") -> None:
        """Hide rows that do not contain `text` (any column, any case), and
        a group heading with nothing left under it."""
        needle = str(text or "").strip().lower()
        heading = None
        shown_under = False
        for row in range(self.table.rowCount()):
            script = self._script_at(row)
            if script is None:
                if heading is not None:
                    self.table.setRowHidden(heading, not shown_under)
                heading, shown_under = row, False
                continue
            hay = f"{script.name} {script.what} {script.path}".lower()
            hide = bool(needle) and needle not in hay
            self.table.setRowHidden(row, hide)
            shown_under = shown_under or not hide
        if heading is not None:
            self.table.setRowHidden(heading, not shown_under)

    def visible_rows(self) -> List[Any]:
        return [self._script_at(r) for r in range(self.table.rowCount())
                if not self.table.isRowHidden(r)
                and self._script_at(r) is not None]

    def text(self, scripts: Optional[Sequence[Any]] = None) -> str:
        """The interpreter, then one tab-separated line per file, under its
        group's heading — what Copy all puts on the clipboard."""
        lines = list(self._header())
        group = None
        for script in (self.rows if scripts is None else scripts):
            if script.group != group:
                group = script.group
                lines += ["", f"[{self._titles.get(group, group)}]"]
            lines.append(script.line())
        return "\n".join(lines) + "\n"

    def copy_all(self) -> None:
        QGuiApplication.clipboard().setText(self.text())
        self.status.setText(f"Copied {self.count} files to the clipboard.")

    def copy_selected(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        chosen = [self._script_at(r) for r in rows
                  if self._script_at(r) is not None]
        if chosen:
            QGuiApplication.clipboard().setText(
                "\n".join(s.line() for s in chosen) + "\n")


def alive(window: Any) -> bool:
    """Whether a held window still exists (a closed one is only hidden)."""
    try:
        window.isVisible()
    except RuntimeError:                                  # already deleted
        return False
    return True
