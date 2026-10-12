"""
council_qt.widgets.source_preview — a file behind an answer, shown read-only.

The Council tab's source chips open this: the file's first rows and its
columns, read with no model (council_core.turn_context._data_preview_text —
the Tk console's _preview_data_file), in read-only boxes, and a "Show in
folder" button that shows the file in the file manager (the Tk console's
_reveal_file). Nothing here writes to the file.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional, Union

from PySide6.QtCore import Qt
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


class SourcePreview(QDialog):
    """A file behind an answer, shown READ-ONLY inside the app: its first
    rows and its columns, read with no model (turn_context._data_preview_text
    — the Tk console's _preview_data_file), and a button that shows it in
    the file manager. Nothing here writes to the file."""

    def __init__(self, parent=None, reveal: Optional[Callable] = None):
        super().__init__(parent)
        self.path: Optional[Path] = None
        self._reveal = reveal
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
        from council_core import turn_context
        self.path = Path(path)
        try:
            schema, rows = turn_context._data_preview_text(self.path)
        except Exception as exc:                          # noqa: BLE001
            schema, rows = (f"The preview failed: {exc}", "")
        try:
            size = f"{self.path.stat().st_size:,} bytes"
        except OSError:
            size = "size unknown"
        self.setWindowTitle(f"Preview: {self.path.name} (read-only)")
        self.info.setText(f"{self.path}  ·  {size}")
        self.rows.setPlainText(rows or "(empty)")
        self.schema.setPlainText(schema or "(empty)")
        self.show()
        self.raise_()

    def on_reveal(self) -> None:
        if self.path is not None and self._reveal is not None:
            self._reveal(self.path)
