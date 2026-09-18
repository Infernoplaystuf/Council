"""
council_qt.dialogs.wizard — the Designer's guided on-ramp, in Qt.

Five screens of controls over `council_core.wizard`, which decides the step
order, what makes a step invalid, and what the answers mean. This file binds a
widget to a field and moves between pages.

IT WRITES NOTHING
The wizard hands back a `WizardResult` and the host creates the project. So
cancelling leaves nothing behind — no half-made directory, no name taken. That
is the Tk behaviour and it is worth keeping: a wizard that created as it went
would strand a project on every abandoned attempt.

EACH STEP IS VALIDATED AS YOU LEAVE IT
Not on Finish. An error about a name typed four screens ago is an error you
have to go looking for.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QStackedWidget,
                               QVBoxLayout, QWidget)

from council_core import wizard as core

from .view import amp

#: The five screens, in the order the user meets them.
STEP_TITLES = {
    "basics": "1 of 5 — Name and window",
    "layout": "2 of 5 — Starting layout",
    "contents": "3 of 5 — What goes in it",
    "reserve": "4 of 5 — Space to fill in later",
    "review": "5 of 5 — Review",
}

TEMPLATES = ("form", "toolbar_main_status", "split_view", "blank")


class GuiWizard(QDialog):
    """Ask five questions, hand back a WizardResult."""

    #: Emitted with the finished result. Not emitted on cancel.
    finished_with = Signal(object)
    #: A line for the Designer's log.
    logged = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None,
                 existing: Optional[List[str]] = None):
        super().__init__(parent)
        self.setWindowTitle("New GUI project")
        self.setModal(True)
        self.answers = core.Answers()
        self._existing = list(existing or [])
        self._step = 0
        self._fields: Dict[str, QWidget] = {}

        outer = QVBoxLayout(self)
        self.heading = QLabel()
        self.heading.setStyleSheet("font-weight: bold;")
        outer.addWidget(self.heading)

        self.pages = QStackedWidget()
        for step in core.STEPS:
            self.pages.addWidget(self._page(step))
        outer.addWidget(self.pages, 1)

        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #f38ba8;")
        outer.addWidget(self.error)

        bar = QHBoxLayout()
        self.back_btn = QPushButton(amp("← Back"))
        self.back_btn.clicked.connect(self.back)
        self.next_btn = QPushButton(amp("Next →"))
        self.next_btn.clicked.connect(self.next)
        cancel = QPushButton(amp("Cancel"))
        cancel.clicked.connect(self.reject)
        bar.addWidget(cancel)
        bar.addStretch(1)
        bar.addWidget(self.back_btn)
        bar.addWidget(self.next_btn)
        outer.addLayout(bar)

        self._render()

    # ==================================================================
    # Pages
    # ==================================================================
    def _page(self, step: str) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        builder = getattr(self, f"_page_{step}")
        builder(form)
        return page

    def _text(self, form, key: str, label: str) -> QLineEdit:
        entry = QLineEdit(getattr(self.answers, key))
        entry.textChanged.connect(
            lambda value, k=key: setattr(self.answers, k, value))
        form.addRow(label, entry)
        self._fields[key] = entry
        return entry

    def _choice(self, form, key: str, label: str, options) -> QComboBox:
        box = QComboBox()
        box.addItems([str(o) for o in options])
        box.setCurrentText(str(getattr(self.answers, key)))
        box.currentTextChanged.connect(
            lambda value, k=key: setattr(self.answers, k, value))
        form.addRow(label, box)
        self._fields[key] = box
        return box

    def _page_basics(self, form) -> None:
        self._text(form, "name", "Project name")
        self._choice(form, "mode", "Import mode", ("linked", "standalone"))
        self._text(form, "title", "Window title (defaults to the name)")
        self._text(form, "min_w", "Minimum width")
        self._text(form, "min_h", "Minimum height")

    def _page_layout(self, form) -> None:
        self._choice(form, "template", "Starting layout", TEMPLATES)

    def _page_contents(self, form) -> None:
        self._text(form, "fields", "Number of fields (form)")
        self._text(form, "labels", "Field labels, comma-separated (optional)")
        self._text(form, "buttons", "Buttons, comma-separated")
        self._choice(form, "main_kind", "Main area (toolbar layout)",
                     core.MAIN_KINDS)
        self._choice(form, "left_kind", "Left pane (split view)",
                     core.SIDE_KINDS)
        self._choice(form, "right_kind", "Right pane (split view)",
                     core.SIDE_KINDS)

    def _page_reserve(self, form) -> None:
        note = QLabel(
            "A reserved space is an empty Frame held open at the size you "
            "draw. It is generated with NO model call, so you can fill it in "
            "later without paying for a guess now.")
        note.setWordWrap(True)
        form.addRow(note)
        self._text(form, "reserve", "How many")
        self._text(form, "reserve_w", "Each one's width")
        self._text(form, "reserve_h", "Each one's height")

    def _page_review(self, form) -> None:
        self.review = QLabel()
        self.review.setWordWrap(True)
        form.addRow(self.review)

    # ==================================================================
    # Moving between them
    # ==================================================================
    def _render(self) -> None:
        step = core.STEPS[self._step]
        self.heading.setText(STEP_TITLES[step])
        self.pages.setCurrentIndex(self._step)
        self.back_btn.setEnabled(self._step > 0)
        last = self._step == len(core.STEPS) - 1
        self.next_btn.setText(amp("Finish" if last else "Next →"))
        self.error.setText("")
        if last:
            self._refresh_review()

    def _refresh_review(self) -> None:
        try:
            self.review.setText("\n".join(core.summary(self.answers)))
        except Exception as exc:                         # noqa: BLE001
            # The review is a PREVIEW. A template that cannot be built must
            # say so here rather than on Finish, where the user has already
            # been told the project is being made.
            self.review.setText(f"Could not build the layout: {exc}")

    def next(self) -> None:
        problem = core.validate(core.STEPS[self._step], self.answers,
                                self._existing)
        if problem:
            self.error.setText(problem)
            return
        if self._step == len(core.STEPS) - 1:
            self.finish()
            return
        self._step += 1
        self._render()

    def back(self) -> None:
        """Never validates. Going back to fix the thing that failed must not
        be blocked by the thing that failed."""
        if self._step > 0:
            self._step -= 1
            self._render()

    def finish(self) -> None:
        try:
            result = core.to_result(self.answers)
        except Exception as exc:                         # noqa: BLE001
            self.error.setText(f"Could not build the layout: {exc}")
            return
        self.logged.emit(f"wizard: {result.template} -> "
                         f"{len(result.shapes)} shape(s)")
        self.finished_with.emit(result)
        self.accept()


def open_wizard(parent=None, on_done: Optional[Callable] = None,
                log: Optional[Callable[[str], None]] = None,
                existing: Optional[List[str]] = None) -> GuiWizard:
    """The Tk module's entry point, with the same shape."""
    dialog = GuiWizard(parent, existing=existing)
    if on_done is not None:
        dialog.finished_with.connect(on_done)
    if log is not None:
        dialog.logged.connect(log)
    dialog.show()
    return dialog
