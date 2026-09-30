"""
council_qt.widgets.example_dialog — "New from example…" in the Designer.

Four answers, one screen: which shipped example, what to call the project,
which toolkit, and which Python will run the app. Every rule is in
council_core.designer_examples — this file lays the answers out and asks that
module whether they are acceptable, on every change, so a taken name or a
missing conda env is said WHILE the dialog is open rather than after it has
closed with the answers gone.

IT BUILDS NOTHING
It hands back an ExampleAnswers and the Designer builds, on a worker. Cancel
leaves nothing behind.

THE TOOLKIT IS ASKED, NOT DEFAULTED, UNLESS THE EXAMPLE SAYS
Typhon and the v4/v5 capture forms are meant for Qt (their live view needs
the Qt canvas) and come up with Qt chosen. The others record no intent, so
the box starts on "Choose…" and Build stays refused until the user picks:
the toolkit is fixed once app.py is written, and a default nobody looked at
would be a choice nobody made.
"""
from __future__ import annotations

from typing import Any, Callable, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QFormLayout,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QPushButton, QVBoxLayout, QWidget)

import python_envs as envs
from council_core import designer_examples as dx

from .. import dialogs
from ..view import amp

#: The toolkit box: what the user reads, and the value it stands for.
TOOLKIT_CHOICES = (("Choose…", dx.TOOLKIT_UNCHOSEN),
                   ("qt — PySide6", "qt"),
                   ("tk — tkinter", "tk"))


class ExampleDialog(QDialog):
    """Pick an example and say how to build it. Hands back ExampleAnswers."""

    def __init__(self, parent: Optional[QWidget] = None, vault_dir: Any = None,
                 examples: Optional[List[dx.ExampleInfo]] = None,
                 check: Optional[Callable[[dx.ExampleAnswers], str]] = None,
                 ask_python_path: Optional[Callable[[], str]] = None):
        super().__init__(parent)
        self.setWindowTitle("New from example")
        self.setModal(True)
        self._examples = list(examples if examples is not None
                              else dx.offered())
        self._check = check or (lambda answers: dx.problem(answers, vault_dir))
        #: Supplied so a test never opens a file dialog.
        self._ask_python_path = ask_python_path or self._browse_python
        #: True until the user types a name of their own. While it is, picking
        #: another example renames the project to match; after, their name
        #: stays.
        self._auto_name = True

        outer = QVBoxLayout(self)
        row = QHBoxLayout()
        self.list = QListWidget()
        for info in self._examples:
            self.list.addItem(info.name)
        self.list.setMinimumWidth(180)
        row.addWidget(self.list)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.note.setMinimumWidth(360)
        row.addWidget(self.note, 1)
        outer.addLayout(row, 1)

        form = QFormLayout()
        self.name = QLineEdit()
        form.addRow("Project name", self.name)
        self.toolkit = QComboBox()
        for text, value in TOOLKIT_CHOICES:
            self.toolkit.addItem(text, value)
        form.addRow("Toolkit (fixed once built)", self.toolkit)
        python_row = QHBoxLayout()
        self.python = QComboBox()
        self.python.setEditable(True)
        self.python.addItems([c for c in envs.choices(envs.DEFAULT_LABEL)
                              if c != envs.BROWSE_LABEL])
        self.python.setCurrentText(envs.DEFAULT_LABEL)
        self.python.setMinimumWidth(220)
        python_row.addWidget(self.python, 1)
        browse = QPushButton(amp("Browse…"))
        browse.clicked.connect(self.on_browse)
        python_row.addWidget(browse)
        form.addRow("Run with (a conda env name or python.exe)", python_row)
        outer.addLayout(form)

        self.warning = QLabel()
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("color: #c69026;")
        outer.addWidget(self.warning)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #f38ba8;")
        outer.addWidget(self.error)

        bar = QHBoxLayout()
        cancel = QPushButton(amp("Cancel"))
        cancel.clicked.connect(self.reject)
        self.build_btn = QPushButton(amp("Build"))
        self.build_btn.setDefault(True)
        self.build_btn.clicked.connect(self.accept)
        bar.addStretch(1)
        bar.addWidget(cancel)
        bar.addWidget(self.build_btn)
        outer.addLayout(bar)

        self.list.currentRowChanged.connect(self._picked)
        # textEdited, not textChanged: only the USER typing ends auto-naming,
        # not the dialog filling the box in.
        self.name.textEdited.connect(self._typed_name)
        self.toolkit.currentIndexChanged.connect(lambda _i: self._revalidate())
        self.python.currentTextChanged.connect(lambda _t: self._revalidate())
        if self._examples:
            self.list.setCurrentRow(0)
        self._revalidate()

    # ------------------------------------------------------------------
    def select(self, name: str) -> None:
        """Pick an example by name — what a click on the list does."""
        for row, info in enumerate(self._examples):
            if info.name == name:
                self.list.setCurrentRow(row)
                return

    def set_toolkit(self, value: str) -> None:
        index = self.toolkit.findData(value)
        if index >= 0:
            self.toolkit.setCurrentIndex(index)

    def answers(self) -> dx.ExampleAnswers:
        info = self._current()
        return dx.ExampleAnswers(
            example=info.name if info else "",
            project=self.name.text().strip(),
            toolkit=str(self.toolkit.currentData() or ""),
            python=envs.spec_from_choice(self.python.currentText().strip()))

    def accept(self) -> None:
        """Refused while the answers cannot be built — and the reason stays
        on screen, next to the box that needs changing."""
        problem = self._check(self.answers())
        if problem:
            self.error.setText(problem)
            return
        super().accept()

    def on_browse(self) -> None:
        path = self._ask_python_path()
        if path:
            self.python.setCurrentText(str(path))

    # ------------------------------------------------------------------
    def _current(self) -> Optional[dx.ExampleInfo]:
        row = self.list.currentRow()
        if 0 <= row < len(self._examples):
            return self._examples[row]
        return None

    def _picked(self, _row: int) -> None:
        info = self._current()
        if info is None:
            return
        self.note.setText(info.note or "(no notes for this example)")
        if self._auto_name:
            self.name.setText(info.default_project)
        # The example's own toolkit when it records one; otherwise back to
        # "Choose…", so a choice made for a different example is not carried
        # over as if it had been made for this one.
        self.set_toolkit(info.toolkit or dx.TOOLKIT_UNCHOSEN)
        self._revalidate()

    def _typed_name(self, _text: str) -> None:
        self._auto_name = False
        self._revalidate()

    def _revalidate(self) -> None:
        answers = self.answers()
        problem = self._check(answers) if answers.example else ""
        self.error.setText(problem)
        self.build_btn.setEnabled(not problem)
        self.warning.setText(dx.toolkit_warning(answers.example,
                                                answers.toolkit))

    def _browse_python(self) -> str:                     # pragma: no cover
        path, _filter = QFileDialog.getOpenFileName(
            self, "Choose the Python that runs this app", "",
            "Python (python*.exe python python3);;All files (*.*)")
        return path


def ask_example(parent: Optional[QWidget] = None,
                vault_dir: Any = None) -> Optional[dx.ExampleAnswers]:
    """Show the dialog and return the answers, or None on cancel.

    None without constructing anything under COUNCIL_NO_DIALOGS: a modal in
    an unattended run waits for a click nobody can make.
    """
    if dialogs.disabled():
        return None
    dialog = ExampleDialog(parent, vault_dir=vault_dir)
    try:
        if dialog.exec() == QDialog.DialogCode.Accepted:
            return dialog.answers()
        return None
    finally:
        dialog.deleteLater()


def ask_export_path(parent: Optional[QWidget], title: str, folder: str,
                    filename: str) -> str:
    """The save dialog for Export .gspec, or "" on cancel.

    The platform's own overwrite prompt is turned off: the Designer asks
    about an existing file itself, in words that say what is replaced, and
    asking twice teaches the user to click through both.
    """
    if dialogs.disabled():
        return ""
    return dialogs.asksaveasfilename(
        title=title, initialdir=folder, initialfile=filename,
        defaultextension=".gspec",
        filetypes=[("GUI wireframe", "*.gspec")], parent=parent,
        confirmoverwrite=False)

