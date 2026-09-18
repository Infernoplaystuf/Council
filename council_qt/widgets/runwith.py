"""
council_qt.widgets.runwith — the Designer's "Run with:" picker.

It shows and edits ONE setting: the open project's `Manifest.python`. It never
runs anything — Run does that, through `python_envs.preflight`.

Its own widget rather than rows in the Designer tab, for the same reason the Tk
one is: the tab is held to thin marshalling, and anything computed there cannot
be tested without the whole Council.

WHY A CAMERA APP NEEDS THIS AT ALL
A generated app that imports pypylon must run in the environment that has
pypylon, which is not the Council's own Python. The setting is per project and
lives in its manifest, which is why choosing one with no project open is
refused rather than remembered: there would be nowhere to put it, and a choice
that looked accepted and was gone on the next open is worse than a refusal.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QComboBox, QFileDialog, QHBoxLayout, QLabel,
                               QWidget)

import python_envs as envs
from council_core import designer_project as dp


class RunWithBox(QWidget):
    """A label and a read-only dropdown, bound to the open project."""

    #: A line for the Designer's log.
    logged = Signal(str)

    def __init__(self, get_dir: Callable[[], Optional[object]],
                 parent: Optional[QWidget] = None,
                 ask_path: Optional[Callable] = None):
        super().__init__(parent)
        self._get_dir = get_dir
        #: Supplied so a test never opens a file dialog.
        self._ask_path = ask_path or self._browse
        self._loading = False

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("Run with:"))
        self.box = QComboBox()
        self.box.setMinimumWidth(180)
        self.box.currentTextChanged.connect(self._picked)
        row.addWidget(self.box)
        self.sync()

    # ------------------------------------------------------------------
    def fill(self) -> None:
        """Re-read the available environments.

        The Tk box does this on `postcommand` — every time the list drops
        down — because a conda env created while the app was open would
        otherwise never appear.
        """
        current = self.box.currentText() or envs.DEFAULT_LABEL
        self._loading = True
        try:
            self.box.clear()
            self.box.addItems(envs.choices(current))
            self.box.setCurrentText(current)
        finally:
            self._loading = False

    def showPopup(self) -> None:                         # pragma: no cover
        self.fill()
        self.box.showPopup()

    def sync(self) -> None:
        """Show the open project's setting. Call after open / new / wizard."""
        spec = dp.interpreter_of(self._get_dir())
        self._loading = True
        try:
            self.box.clear()
            self.box.addItems(envs.choices(envs.display(spec)))
            self.box.setCurrentText(envs.display(spec))
        finally:
            self._loading = False

    # ------------------------------------------------------------------
    def _picked(self, choice: str) -> None:
        if self._loading or not choice:
            # Rebuilding the list fires this for every item. Saving on those
            # would write whichever entry happened to be added last.
            return
        directory = self._get_dir()
        if choice == envs.BROWSE_LABEL:
            path = self._ask_path()
            if not path:
                self.sync()
                return
            spec = str(path)
        else:
            spec = envs.spec_from_choice(choice)
        # `set_interpreter` is the one that knows there is nowhere to put a
        # choice with no project open, so it does the refusing. An early
        # return here as well would be a second copy of that rule, free to
        # drift from it.
        result = dp.set_interpreter(directory, spec)
        self.sync()
        if not result.ok:
            self.logged.emit(result.message)
            return
        self.logged.emit(f"Run with: {self.box.currentText()} — checked when "
                         f"you press Run")

    def _browse(self) -> str:
        path, _filter = QFileDialog.getOpenFileName(
            self, "Choose the Python that runs this project", "",
            "Python (python*.exe python python3);;All files (*.*)")
        return path
