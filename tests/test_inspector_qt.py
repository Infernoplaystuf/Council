"""
The property panel, in Qt.

designer_form decides everything; this widget builds a control per Field and
reads it back. So the tests are about the two things a widget can get wrong
that a pure-function suite cannot see: which control a field becomes, and
whether the panel reports rows the user never touched.

That second one is the defect this panel exists to fix. The Tk inspector hands
every variable it holds to _apply_props, which writes each to every selected
shape — so selecting two buttons and pressing Apply renames the second to the
first, with no undo entry and nothing on screen to say it happened.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from council_core import designer_form as form  # noqa: E402
from gui_shapes import PALETTE, new_shape  # noqa: E402

pytest.importorskip("PySide6", reason="the inspector needs PySide6")

from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox,  # noqa: E402
                               QLabel, QLineEdit, QPushButton)

from council_qt.widgets.inspector import ColourRow, InspectorView  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def mk(kind="button", **kw):
    shape = new_shape(kind, 0, 0)
    for key, value in kw.items():
        setattr(shape, key, value)
    return shape


@pytest.fixture
def panel(qapp):
    view = InspectorView()
    yield view
    view.deleteLater()


def _labels(view):
    return [w.text() for w in view._body.findChildren(QLabel)]


# ============================================================
# Only what the user touched
# ============================================================

def test_an_untouched_panel_submits_nothing(panel):
    """THE DEFECT THIS PANEL FIXES. Select two shapes, press Apply without
    typing, and the Tk panel writes shape one's every field onto shape two."""
    panel.show_fields(form.fields_for([mk(label="first"), mk(label="second")]))
    assert panel.values() == {}


def test_pressing_apply_on_an_untouched_panel_changes_nothing(panel, qapp):
    seen = []
    panel.applied.connect(seen.append)
    panel.show_fields(form.fields_for([mk(label="first"), mk(label="second")]))
    panel._body.findChild(QPushButton).click()
    qapp.processEvents()
    assert seen == [{}]


def test_only_the_edited_row_is_submitted(panel):
    panel.show_fields(form.fields_for([mk(label="first"), mk(label="second")]))
    panel._controls["label"].setText("both")
    panel._controls["label"].textEdited.emit("both")
    assert panel.values() == {"label": "both"}


def test_typing_in_a_row_is_what_marks_it_edited(panel):
    """setText alone is not a user edit — rebuilding the panel calls it for
    every row, and a panel that counted that would submit everything."""
    panel.show_fields(form.fields_for([mk()]))
    panel._controls["label"].setText("programmatic")
    assert panel.values() == {}


def test_a_dropdown_choice_counts_as_an_edit(panel):
    panel.show_fields(form.fields_for([mk()]))
    box = panel._controls["resize"]
    box.setCurrentIndex((box.currentIndex() + 1) % box.count())
    assert "resize" in panel.values()


def test_a_checkbox_toggle_counts_as_an_edit(panel):
    kind = next((k for k in PALETTE
                 if any(f.kind == form.SWITCH
                        for f in form.fields_for([mk(k)]))), None)
    if kind is None:
        pytest.skip("no kind in the palette shows a switch")
    panel.show_fields(form.fields_for([mk(kind)]))
    box = next(w for w in panel._controls.values()
               if isinstance(w, QCheckBox))
    box.toggle()
    assert panel.values()


def test_rebuilding_forgets_the_previous_edits(panel):
    """The selection changed. Carrying a pending edit across would apply it to
    a shape the user was not looking at when they typed it."""
    panel.show_fields(form.fields_for([mk()]))
    panel._controls["label"].textEdited.emit("typed")
    panel.show_fields(form.fields_for([mk()]))
    assert panel.values() == {}


def test_rebuilding_destroys_the_old_controls(panel, qapp):
    """Left parented, they stack up invisibly and the panel grows a row per
    selection change."""
    panel.show_fields(form.fields_for([mk()]))
    before = len(panel._body.findChildren(QLineEdit))
    for _ in range(5):
        panel.show_fields(form.fields_for([mk()]))
    qapp.processEvents()
    assert len(panel._body.findChildren(QLineEdit)) == before


# ============================================================
# Which control a field becomes
# ============================================================

def test_nothing_selected_says_so(panel):
    panel.show_fields([])
    assert "(nothing selected)" in _labels(panel)
    assert not panel._body.findChildren(QLineEdit)


def test_a_note_is_text_and_not_a_control(panel):
    """It is the REASON a block is absent. As a control it would be an input
    the user can type a sentence into."""
    panel.show_fields([form.Field("", "This kind cannot be coloured.",
                                  form.NOTE)])
    assert "This kind cannot be coloured." in _labels(panel)
    assert not panel._body.findChildren(QLineEdit)
    assert panel.values() == {}


def test_a_heading_is_text_and_not_a_control(panel):
    panel.show_fields([form.Field("", "button properties", form.TEXT,
                                  heading=True)])
    assert "button properties" in _labels(panel)
    assert not panel._body.findChildren(QLineEdit)


def test_a_switch_caption_is_the_control(panel):
    """A separate label leaves two things to click for one answer."""
    panel.show_fields([form.Field("freeform", "Freeform (place)",
                                  form.SWITCH, True)])
    boxes = panel._body.findChildren(QCheckBox)
    assert len(boxes) == 1
    assert boxes[0].text() == "Freeform (place)"
    assert boxes[0].isChecked()
    assert "Freeform (place)" not in _labels(panel)


def test_an_ampersand_in_a_caption_is_not_a_shortcut(panel):
    """Qt reads "&" in a caption as a mnemonic; Tk does not. A prop called
    "save&exit" would render as "saveexit" with an underlined e."""
    panel.show_fields([form.Field("k", "save & exit", form.SWITCH, False)])
    box = panel._body.findChildren(QCheckBox)[0]
    assert "&&" in box.text()
    assert box.text().replace("&&", "&") == "save & exit"


def test_a_choice_offers_exactly_its_choices(panel):
    field = next(f for f in form.fields_for([mk()]) if f.kind == form.CHOICE)
    panel.show_fields([field])
    box = panel._body.findChildren(QComboBox)[0]
    assert [box.itemText(i) for i in range(box.count())] == field.choices


def test_a_port_name_shows_its_derived_default_as_a_placeholder(panel):
    import gui_ports
    kind = next(k for k in PALETTE if gui_ports.caps(k).types)
    shape = mk(kind, label="Start Capture")
    panel.show_fields(form.port_fields(shape))
    assert panel._controls["name"].placeholderText() == \
        gui_ports.default_port_name(kind, shape.label)


def test_a_banner_is_shown_when_asked_for(panel):
    panel.show_fields(form.fields_for([mk(), mk()]), banner="2 shapes selected")
    assert "2 shapes selected" in _labels(panel)


# ============================================================
# The colour row
# ============================================================

def test_choosing_a_swatch_fills_the_hex_entry(qapp):
    """A typed hex WINS, so leaving the entry alone would make the choice
    invisible and then lose it at Apply."""
    row = ColourRow(form.Field("bg", "Background", form.COLOUR, "",
                               form.colour_options()))
    option = form.colour_options()[1]
    row.picker.setCurrentText(option)
    assert row.entry.text() == form.option_hex(option)
    assert row.value() == form.option_hex(option)
    row.deleteLater()


def test_a_typed_hex_beats_the_dropdown(qapp):
    row = ColourRow(form.Field("bg", "Background", form.COLOUR, "",
                               form.colour_options()))
    row.picker.setCurrentText(form.colour_options()[1])
    row.entry.setText("#ff0000")
    assert row.value() == "#ff0000"
    row.deleteLater()


def test_clearing_both_controls_means_inherit(qapp):
    row = ColourRow(form.Field("bg", "Background", form.COLOUR, "",
                               form.colour_options()))
    row.picker.setCurrentText("(inherit)")
    row.entry.setText("")
    assert form.is_inherited(row.value())
    row.deleteLater()


def test_a_shapes_existing_colour_selects_its_swatch(qapp):
    hex_value = form.option_hex(form.colour_options()[1])
    row = ColourRow(form.Field("bg", "Background", form.COLOUR, hex_value,
                               form.colour_options()))
    assert row.picker.currentText() == form.colour_options()[1]
    assert row.entry.text() == hex_value
    row.deleteLater()


def test_a_colour_with_no_swatch_still_shows_in_the_entry(qapp):
    """Otherwise a hand-edited .gspec's colour silently disappears the first
    time the panel opens on it."""
    row = ColourRow(form.Field("bg", "Background", form.COLOUR, "#123457",
                               form.colour_options()))
    assert row.entry.text() == "#123457"
    assert row.picker.currentText() == "(inherit)"
    row.deleteLater()


def test_a_junk_colour_does_not_crash_the_chip(qapp):
    row = ColourRow(form.Field("bg", "Background", form.COLOUR, "wat",
                               form.colour_options()))
    row.entry.setText("#zz")
    assert row.chip.styleSheet()
    row.deleteLater()


def test_the_colour_row_submits_through_the_core_rule(panel):
    panel.show_fields([form.Field("bg", "Background", form.COLOUR, "",
                                  form.colour_options())])
    row = panel._controls["bg"]
    row.entry.setText("#abcdef")
    assert panel.values() == {"bg": "#abcdef"}


# ============================================================
# What Apply emits
# ============================================================

def test_apply_emits_the_change_set_core_built(panel, qapp):
    seen = []
    panel.applied.connect(seen.append)
    shape = mk(label="Go")
    panel.show_fields(form.fields_for([shape]))
    panel._controls["min_w"].textEdited.emit("")
    panel._controls["min_w"].setText(" 12 ")
    panel._body.findChild(QPushButton).click()
    qapp.processEvents()
    assert seen == [{"min_w": 12}], "the value never went through cast()"


def test_a_port_edit_lands_under_port(panel, qapp):
    import gui_ports
    kind = next(k for k in PALETTE if gui_ports.caps(k).types)
    seen = []
    panel.applied.connect(seen.append)
    panel.show_fields(form.port_fields(mk(kind, label="Go")))
    panel._controls["name"].setText("shutter")
    panel._controls["name"].textEdited.emit("shutter")
    panel._body.findChild(QPushButton).click()
    qapp.processEvents()
    assert seen == [{"port": {"name": "shutter"}}]
