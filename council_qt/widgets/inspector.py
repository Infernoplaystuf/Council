"""
council_qt.widgets.inspector — the Designer's property panel, in Qt.

One widget per `council_core.designer_form.Field`, and nothing else. Which
rows exist, what each one is, what a raw string becomes and what the two
colour controls mean are all decided in `designer_form`; this file knows how
to build a QLineEdit and how to read one back.

ONLY WHAT THE USER TOUCHED IS SUBMITTED
The Tk panel hands every variable it holds to `_apply_props`, which writes each
one to every selected shape. Select two buttons, change nothing, press Apply,
and the second button's label is now the first one's — there is no undo entry
for a change the user did not make, and nothing on screen said it happened.

So this panel tracks which rows were edited and submits those. `collect`
already returns only the keys it is given, which is the half of the fix that
lives in core; this is the other half. A multi-selection edit is finally what
it looks like: change the one field you meant to change, and only that field
lands on all of them.

NUMBERS ARE A LINE EDIT, NOT A SPIN BOX
A spin box would refuse the typo rather than coercing it, which sounds better
until a .gspec holds min_w="" and the panel cannot show it. `cast` stays the
single answer to "what does this raw string mean", and a bad number is 0 —
carried over deliberately, because an inspector that throws on a typo loses
every other edit in the same Apply.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QScrollArea,
                               QVBoxLayout, QWidget)

from council_core import designer_form as form
from council_core.designer_scene import THEME

from ..view import ViewHelpers, amp


class ColourRow(QWidget):
    """A swatch dropdown, a hex entry, and a chip showing the result.

    The rule that combines the two controls is `form.resolve_colour`, not
    anything here — the Tk docstring states it and the Tk code does not
    implement it, so it is written once in core and both front ends ask.
    """

    changed = Signal()

    def __init__(self, field: form.Field, parent: Optional[QWidget] = None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)

        self.picker = QComboBox()
        self.picker.addItems(field.choices)
        self.picker.setCurrentText(form.hex_option(field.value) or "(inherit)")
        self.entry = QLineEdit(field.value or "")
        self.entry.setMaximumWidth(96)
        self.chip = QFrame()
        self.chip.setFixedSize(16, 16)
        self.chip.setFrameShape(QFrame.Box)

        row.addWidget(self.picker, 1)
        row.addWidget(self.entry)
        row.addWidget(self.chip)

        self.picker.currentTextChanged.connect(self._picked)
        self.entry.textChanged.connect(self._typed)
        self._paint_chip()

    def _picked(self, option: str) -> None:
        # Choosing from the dropdown writes the hex into the entry, because a
        # typed hex WINS — leaving the entry alone would make the choice
        # invisible and then lose it.
        self.entry.setText(form.option_hex(option))
        self._paint_chip()
        self.changed.emit()

    def _typed(self, _text: str) -> None:
        self._paint_chip()
        self.changed.emit()

    def _paint_chip(self) -> None:
        shown = QColor(self.value())
        if not shown.isValid():
            shown = QColor(THEME["surface"])
        self.chip.setStyleSheet(
            f"background: {shown.name()}; border: 1px solid {THEME['overlay']};")

    def value(self) -> str:
        """What the two controls mean together.

        The dropdown's HEX goes to the rule, not its caption. Handing over the
        caption makes "(inherit)" a colour named "(inherit)" — and a swatch
        whose entry was then cleared writes "Data's Inferno: #1e1e2e" onto the
        shape, which no toolkit can render and which survives into the .gspec.
        """
        return form.resolve_colour(self.entry.text(),
                                   form.option_hex(self.picker.currentText()))


class InspectorView(QScrollArea, ViewHelpers):
    """The property panel. Builds rows, reports what changed."""

    #: Emitted with the change-set when Apply is pressed. Carries ONLY the
    #: rows the user edited.
    applied = Signal(dict)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self._fields: List[form.Field] = []
        self._controls: Dict[str, QWidget] = {}
        self._dirty: set = set()
        self._body = QWidget()
        self._layout = QVBoxLayout(self._body)
        self._layout.setAlignment(Qt.AlignTop)
        self.setWidget(self._body)
        self.show_fields([], empty_text="(nothing selected)")

    # ==================================================================
    # Building
    # ==================================================================
    def show_fields(self, fields: Sequence[form.Field],
                    empty_text: str = "(nothing selected)",
                    banner: str = "") -> None:
        """Replace the panel with a row per field."""
        self._clear()
        self._fields = list(fields)
        self._dirty.clear()
        if banner:
            self._add_label(banner, THEME["yellow"])
        if not self._fields:
            self._add_label(empty_text, THEME["subtext"])
            return
        for field in self._fields:
            self._add_row(field)
        apply_button = QPushButton(amp("Apply"))
        apply_button.clicked.connect(self._apply)
        self._layout.addWidget(apply_button, 0, Qt.AlignLeft)

    def _clear(self) -> None:
        self._controls.clear()
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

    def _add_label(self, text: str, colour: str = "") -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        if colour:
            label.setStyleSheet(f"color: {colour};")
        self._layout.addWidget(label)
        return label

    def _add_row(self, field: form.Field) -> None:
        if field.heading:
            heading = self._add_label(field.label)
            heading.setStyleSheet("font-weight: bold;")
            return
        if field.kind == form.NOTE:
            # Text, not a control. The reason a block is absent.
            self._add_label(field.label, THEME["subtext"])
            return

        if field.kind == form.SWITCH:
            # Its own case because the caption IS the control — a separate
            # label would leave two things to click for one answer. `amp`
            # because Qt reads "&" in a caption as a mnemonic and Tk does not.
            control: QWidget = QCheckBox(amp(field.label))
            control.setChecked(bool(field.value))
            control.toggled.connect(lambda _v, k=field.key: self._touch(k))
        else:
            self._add_label(field.label)
            control = self._control_for(field)
        self._layout.addWidget(control)
        self._controls[field.key] = control

    def _control_for(self, field: form.Field) -> QWidget:
        if field.kind == form.COLOUR:
            row = ColourRow(field)
            row.changed.connect(lambda k=field.key: self._touch(k))
            return row
        if field.kind == form.CHOICE:
            box = QComboBox()
            box.addItems([str(c) for c in field.choices])
            box.setCurrentText(str(field.value))
            box.currentTextChanged.connect(
                lambda _t, k=field.key: self._touch(k))
            return box
        entry = QLineEdit("" if field.value is None else str(field.value))
        if field.placeholder:
            entry.setPlaceholderText(field.placeholder)
        entry.textEdited.connect(lambda _t, k=field.key: self._touch(k))
        return entry

    def _touch(self, key: str) -> None:
        self._dirty.add(key)

    # ==================================================================
    # Reading back
    # ==================================================================
    def values(self) -> Dict[str, Any]:
        """The raw value of every row the user EDITED.

        Not every row. See the module docstring: handing the whole panel to a
        multi-selection overwrites the shapes the user never looked at.
        """
        out: Dict[str, Any] = {}
        for field in self._fields:
            if field.key not in self._dirty:
                continue
            out[field.key] = self._read(self._controls.get(field.key))
        return out

    @staticmethod
    def _read(control: Optional[QWidget]) -> Any:
        if isinstance(control, ColourRow):
            return control.value()
        if isinstance(control, QCheckBox):
            return control.isChecked()
        if isinstance(control, QComboBox):
            return control.currentText()
        if isinstance(control, QLineEdit):
            return control.text()
        return ""

    def _apply(self) -> None:
        self.applied.emit(form.collect_all(self._fields, self.values()))
