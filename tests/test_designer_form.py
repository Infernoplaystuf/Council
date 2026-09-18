"""
The property panel's decisions — where the Designer's toolkit cost really is.

The measurement that made phase 8 look expensive said gui_canvas.py has 119
toolkit lines. 76 of them are the inspector; the 647-line drawing engine
scores one. The canvas hand-draws, and the form is the part that has to be
rebuilt per toolkit — so what it DECIDES is what had to move.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import designer_form as form  # noqa: E402
from gui_shapes import PALETTE, Shape, is_container, new_shape  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def mk(kind="button", **kw):
    shape = new_shape(kind, 0, 0)
    for key, value in kw.items():
        setattr(shape, key, value)
    return shape


def test_the_module_imports_no_toolkit():
    source = (ROOT / "council_core" / "designer_form.py").read_text(
        encoding="utf-8")
    for toolkit in ("tkinter", "PySide6", "PyQt5", "import tk"):
        assert toolkit not in source


# ============================================================
# Which rows appear
# ============================================================

def test_nothing_selected_means_no_rows():
    """The caller shows its own empty state, because what it should say
    differs between a docked panel and a dialog."""
    assert form.fields_for([]) == []


def test_the_common_rows_are_always_there():
    keys = [f.key for f in form.fields_for([mk()])]
    for expected in ("label", "note", "resize", "min_w", "min_h"):
        assert expected in keys


def test_the_rows_keep_the_tk_order():
    """A user finds a field by position."""
    keys = [f.key for f in form.fields_for([mk()])][:5]
    assert keys == ["label", "note", "resize", "min_w", "min_h"]


def test_only_a_container_offers_freeform():
    container = next((k for k in PALETTE if is_container(k)), None)
    if container is None:
        pytest.skip("no container kind in the palette")
    assert "freeform" in [f.key for f in form.fields_for([mk(container)])]
    plain = next(k for k in PALETTE if not is_container(k))
    assert "freeform" not in [f.key for f in form.fields_for([mk(plain)])]


def test_a_number_field_is_typed_as_one():
    field = next(f for f in form.fields_for([mk()]) if f.key == "min_w")
    assert field.kind == form.NUMBER


def test_a_choice_field_carries_its_choices():
    field = next(f for f in form.fields_for([mk()]) if f.key == "resize")
    assert field.kind == form.CHOICE
    assert field.choices


# -- what a multi-selection changes -------------------------------------------

def test_a_mixed_selection_hides_the_per_kind_schema():
    """A schema belongs to ONE kind. Showing the first shape's would offer
    fields the others do not have."""
    with_schema = next(
        (k for k in PALETTE if (PALETTE[k] or {}).get("prop_schema")), None)
    if with_schema is None:
        pytest.skip("no kind in the palette declares a prop_schema")
    single = form.fields_for([mk(with_schema)])
    multi = form.fields_for([mk(with_schema), mk(with_schema)])
    assert any(f.prop for f in single)
    assert not any(f.prop for f in multi)


def test_the_binding_and_colour_blocks_are_single_shape_only():
    """"Which script does this run" and "what colour is it" are answers about
    ONE widget."""
    assert form.shows_single_shape_blocks([mk()])
    assert not form.shows_single_shape_blocks([mk(), mk()])
    assert not form.shows_single_shape_blocks([])


def test_every_row_carries_the_first_shapes_value():
    """That is the Tk behaviour, and it is exactly why apply_props must write
    only what CHANGED — applying the whole panel would overwrite every other
    shape's label with the first one's."""
    rows = form.fields_for([mk(label="first"), mk(label="second")])
    assert next(f for f in rows if f.key == "label").value == "first"


# ============================================================
# Casting
# ============================================================

@pytest.mark.parametrize("raw,expected", [
    ("42", 42), ("  7 ", 7), ("", 0), (None, 0), ("abc", 0), ("3.9", 0),
])
def test_a_bad_number_becomes_zero_rather_than_raising(raw, expected):
    """An inspector that throws on a typo loses every other edit in the same
    Apply, and the user has no idea which field did it."""
    assert form.cast(form.NUMBER, raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("a, b ,, c", ["a", "b", "c"]), ("", []), (None, []), ("solo", ["solo"]),
])
def test_a_list_field_splits_and_drops_empties(raw, expected):
    assert form.cast(form.LIST, raw) == expected


def test_a_switch_is_coerced_to_a_real_bool():
    assert form.cast(form.SWITCH, 1) is True
    assert form.cast(form.SWITCH, "") is False


def test_text_never_comes_back_as_none():
    """A None reaching a shape's label becomes the string "None" on screen."""
    assert form.cast(form.TEXT, None) == ""


# ============================================================
# Collecting
# ============================================================

def test_only_the_keys_the_caller_passes_are_returned():
    """THE WHOLE POINT. A caller that passed every field would overwrite every
    selected shape with the first one's values."""
    rows = form.fields_for([mk()])
    changes = form.collect(rows, {"label": "new"})
    assert changes == {"label": "new"}


def test_prop_fields_are_gathered_under_props():
    with_schema = next(
        (k for k in PALETTE if (PALETTE[k] or {}).get("prop_schema")), None)
    if with_schema is None:
        pytest.skip("no kind in the palette declares a prop_schema")
    rows = form.fields_for([mk(with_schema)])
    prop_row = next(f for f in rows if f.prop)
    changes = form.collect(rows, {prop_row.key: "value"})
    assert "props" in changes
    assert prop_row.key in changes["props"]
    assert prop_row.key not in changes


def test_a_heading_is_never_collected():
    rows = [form.Field("", "button properties", form.TEXT, heading=True)]
    assert form.collect(rows, {"": "anything"}) == {}


def test_values_are_cast_on_the_way_out():
    rows = form.fields_for([mk()])
    changes = form.collect(rows, {"min_w": " 12 "})
    assert changes["min_w"] == 12


def test_collecting_nothing_returns_nothing():
    assert form.collect(form.fields_for([mk()]), {}) == {}


# ============================================================
# The colour rule the code did not implement
# ============================================================

def test_a_typed_hex_wins_over_the_dropdown():
    """The Tk docstring states this rule. The CODE registers only the hex
    variable into the target dict, so any path that leaves the entry untouched
    loses the dropdown choice silently. The rule is written once here, and
    both front ends call it."""
    assert form.resolve_colour("#ff0000", "blue") == "#ff0000"


def test_an_empty_entry_lets_the_dropdown_win():
    assert form.resolve_colour("", "blue") == "blue"
    assert form.resolve_colour("   ", "blue") == "blue"


def test_both_empty_means_inherit():
    """"" on a shape's bg/fg means inherit from the ancestor or the OS — it is
    not a colour, and writing black instead would make every widget opaque."""
    assert form.resolve_colour("", "") == form.INHERIT
    assert form.resolve_colour(None, None) == form.INHERIT
    assert form.is_inherited(form.resolve_colour("", ""))


def test_a_theme_name_resolves_through_the_palette():
    assert form.resolve_colour("", "accent", {"accent": "#89b4fa"}) == "#89b4fa"


def test_a_name_with_no_palette_entry_is_passed_through():
    """So a literal colour name like "red" still works."""
    assert form.resolve_colour("", "red", {"accent": "#89b4fa"}) == "red"
