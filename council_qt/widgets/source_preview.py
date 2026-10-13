"""
council_qt.widgets.source_preview — a file behind an answer, shown read-only.

The Council tab's source chips open this: the file's first rows and its
columns, read with no model (council_core.turn_context._data_preview_text —
the Tk console's _preview_data_file), in read-only boxes, and a "Show in
folder" button that shows the file in the file manager (the Tk console's
_reveal_file). A folder behind an answer is listed. Nothing here writes to
the file.

The file is read on a worker thread, never the GUI thread (A1): the dialog
opens at once, says it is reading, and is filled when the read is done (found
in review: a 53 MB JSON held the window for 4 s).
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, List, Optional, Tuple, Union

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPlainTextEdit,
                               QPushButton, QTabWidget, QVBoxLayout)


def reveal_command(path, platform: str = sys.platform
                   ) -> Union[str, List[str]]:
    """The command that shows ``path`` in the file manager: selected in
    Explorer and Finder, its folder elsewhere (the Tk console's
    _reveal_file). Explorer reads its /select argument itself, so on
    Windows it is one string, as Tk runs it."""
    path = Path(path)
    if platform == "win32":
        return f'explorer /select,"{path}"'
    if platform == "darwin":
        return ["open", "-R", str(path)]
    return ["xdg-open", str(path.parent)]


def reveal_in_file_manager(path) -> None:
    """Show ``path`` in the file manager. Opens nothing, writes nothing.
    Not on an offscreen run: there is no screen there for it, and the file
    manager would open on the desktop of whoever runs it."""
    if os.environ.get("QT_QPA_PLATFORM", "").lower() == "offscreen":
        print(f"[reveal] offscreen: not opening the file manager for {path}")
        return
    subprocess.Popen(reveal_command(path))      # noqa: S603 — a file path


#: How many entries a folder's listing shows.
FOLDER_LISTING_MAX = 500


def file_preview(path: Path) -> Tuple[str, str, str]:
    """(columns, first rows, a line about the file) — read with no model.
    Blocking: run it off the GUI thread."""
    from council_core import turn_context
    try:
        schema, rows = turn_context._data_preview_text(path)
    except Exception as exc:                              # noqa: BLE001
        schema, rows = (f"The preview failed: {exc}", "")
    try:
        size = f"{path.stat().st_size:,} bytes"
    except OSError:
        size = "size unknown"
    return schema, rows, f"{path}  ·  {size}"


def folder_preview(path: Path) -> Tuple[str, str, str]:
    """(a count, the folder's entries, a line about it): names and sizes
    only, nothing opened; at most FOLDER_LISTING_MAX entries. Blocking."""
    names: List[str] = []
    files = folders = 0
    more = False
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if len(names) >= FOLDER_LISTING_MAX:
                    more = True
                    break
                try:
                    if entry.is_dir():
                        folders += 1
                        names.append(f"{entry.name}{os.sep}")
                    else:
                        files += 1
                        names.append(f"{entry.name}  "
                                     f"({entry.stat().st_size:,} bytes)")
                except OSError:
                    names.append(entry.name)
    except OSError as exc:
        return (f"The folder could not be listed: {exc}", "", str(path))
    names.sort(key=str.lower)
    count = (f"{files} file(s) and {folders} folder(s)"
             + (f" — the first {FOLDER_LISTING_MAX} shown" if more else ""))
    return count, "\n".join(names), f"{path}  ·  folder"


class SourcePreview(QDialog):
    """A file behind an answer, shown READ-ONLY inside the app: its first
    rows and its columns, read with no model (turn_context._data_preview_text
    — the Tk console's _preview_data_file), and a button that shows it in
    the file manager; a folder is listed. Read on a worker thread. Nothing
    here writes to the file."""

    #: (request, columns, rows, info) — a read finished, on its worker.
    _read = Signal(int, str, str, str)

    def __init__(self, parent=None, reveal: Optional[Callable] = None):
        super().__init__(parent)
        self.path: Optional[Path] = None
        self._reveal = reveal
        #: True from a click until its read is shown.
        self.loading = False
        self._request = 0
        self._read.connect(self._fill)
        self.resize(900, 600)
        layout = QVBoxLayout(self)
        self.info = QLabel("")
        self.info.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.info)
        tabs = QTabWidget()
        self.rows = QPlainTextEdit()
        self.schema = QPlainTextEdit()
        for label, view in (("First rows", self.rows),
                            ("Columns", self.schema)):
            view.setReadOnly(True)
            view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            tabs.addTab(view, label)
        layout.addWidget(tabs, 1)
        buttons = QHBoxLayout()
        self.reveal_button = QPushButton("Show in folder")
        self.reveal_button.setToolTip("Show this file in the file manager.")
        self.reveal_button.clicked.connect(self.on_reveal)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        buttons.addWidget(self.reveal_button)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    def show_file(self, path: Path) -> None:
        """Open on ``path``, read on a worker (file_preview)."""
        self._open(Path(path), file_preview)

    def show_folder(self, path: Path) -> None:
        """Open on the folder ``path``, listed on a worker."""
        self._open(Path(path), folder_preview)

    def _open(self, path: Path, reader: Callable) -> None:
        self.path = path
        self._request += 1
        request = self._request
        self.loading = True
        self.setWindowTitle(f"Preview: {path.name or path} (read-only)")
        self.info.setText(f"{path}  ·  reading…")
        self.rows.setPlainText("Reading…")
        self.schema.setPlainText("")
        self.show()
        self.raise_()

        def work() -> None:
            try:
                schema, rows, info = reader(path)
            except Exception as exc:                      # noqa: BLE001
                schema, rows, info = (f"The preview failed: {exc}", "",
                                      str(path))
            try:
                self._read.emit(request, schema, rows, info)
            except RuntimeError:
                pass                     # the window was closed first

        threading.Thread(target=work, name="source-preview",
                         daemon=True).start()

    def _fill(self, request: int, schema: str, rows: str, info: str) -> None:
        if request != self._request:
            return                       # an earlier click's read
        self.loading = False
        self.info.setText(info)
        self.rows.setPlainText(rows or "(empty)")
        self.schema.setPlainText(schema or "(empty)")

    def on_reveal(self) -> None:
        if self.path is not None and self._reveal is not None:
            self._reveal(self.path)
