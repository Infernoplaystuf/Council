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


# ============================================================
# The binding block
# ============================================================

def _kind_with_ports():
    import gui_ports
    return next(k for k in PALETTE if gui_ports.caps(k).types)


def _kind_without_ports():
    import gui_ports
    return next((k for k in PALETTE if not gui_ports.caps(k).types), None)


def test_a_kind_with_no_runtime_value_gets_a_reason_not_an_empty_panel():
    """"No rows" and "rows withheld, here is why" are indistinguishable to a
    caller unless the reason is itself a row. The Tk panel shows the note; a
    port block that just returned [] would show nothing at all."""
    kind = _kind_without_ports()
    if kind is None:
        pytest.skip("every kind in the palette has a port")
    rows = form.port_fields(mk(kind))
    assert [r.kind for r in rows] == [form.NOTE]
    assert rows[0].label.strip(), "the note has no text in it"


def test_the_note_is_the_reason_the_catalogue_gives():
    import gui_ports
    kind = next((k for k in PALETTE if gui_ports.note(k)), None)
    if kind is None:
        pytest.skip("no kind carries a PORT_NOTE")
    assert form.port_fields(mk(kind))[0].label == gui_ports.note(kind)


def test_a_bindable_kind_gets_a_name_and_a_direction():
    keys = [r.key for r in form.port_fields(mk(_kind_with_ports()))]
    assert "name" in keys and "dir" in keys


def test_the_port_name_defaults_to_the_derived_one():
    import gui_ports
    shape = mk(_kind_with_ports(), label="Start Capture")
    name = next(r for r in form.port_fields(shape) if r.key == "name")
    assert name.value == gui_ports.default_port_name(shape.kind, shape.label)


def test_submitting_the_derived_name_unchanged_persists_nothing():
    """THE REASON `placeholder` EXISTS. Freezing the derived name on the first
    Apply means renaming the label afterwards no longer renames the port, and
    the user has done nothing to ask for that."""
    shape = mk(_kind_with_ports(), label="Start Capture")
    rows = form.port_fields(shape)
    derived = next(r for r in rows if r.key == "name").placeholder
    assert derived
    changes = form.collect_all(rows, {"name": derived})
    assert changes["port"]["name"] == ""


def test_a_name_the_user_actually_typed_is_persisted():
    shape = mk(_kind_with_ports(), label="Start Capture")
    rows = form.port_fields(shape)
    changes = form.collect_all(rows, {"name": "shutter"})
    assert changes["port"]["name"] == "shutter"


def test_the_direction_is_offered_in_words_and_stored_as_a_code():
    """The .gspec stores "i". A panel showing "i" asks the user to learn an
    encoding for no reason."""
    rows = form.port_fields(mk(_kind_with_ports()))
    direction = next(r for r in rows if r.key == "dir")
    assert direction.value in form.DIR_LABEL.values()
    for choice in direction.choices:
        assert choice in form.DIR_CODE, f"{choice!r} maps to no stored code"


def test_a_chosen_direction_round_trips_back_to_its_code():
    rows = form.port_fields(mk(_kind_with_ports()))
    direction = next(r for r in rows if r.key == "dir")
    for label in direction.choices:
        changes = form.collect_all(rows, {"dir": label})
        assert changes["port"]["dir"] == form.DIR_CODE[label]
        assert len(changes["port"]["dir"]) <= 2


def test_only_the_directions_the_kind_allows_are_offered():
    import gui_ports
    for kind in PALETTE:
        cap = gui_ports.caps(kind)
        if not cap.types:
            continue
        rows = form.port_fields(mk(kind))
        direction = next((r for r in rows if r.key == "dir"), None)
        if direction is None:
            continue
        for label in direction.choices:
            assert form.DIR_CODE[label] in cap.dirs, (
                f"{kind} offers {label!r}, which it cannot honour")


def test_a_single_legal_type_is_shown_rather_than_offered():
    """A dropdown with one entry is a control that pretends to do something."""
    import gui_ports
    kind = next((k for k in PALETTE if len(gui_ports.caps(k).types) == 1), None)
    if kind is None:
        pytest.skip("every bindable kind has several types")
    rows = form.port_fields(mk(kind))
    assert not any(r.key == "type" for r in rows)
    assert any(r.kind == form.NOTE and "Value type" in r.label for r in rows)


def test_several_legal_types_become_a_real_choice():
    import gui_ports
    kind = next((k for k in PALETTE if len(gui_ports.caps(k).types) > 1), None)
    if kind is None:
        pytest.skip("no kind has more than one legal type")
    row = next(r for r in form.port_fields(mk(kind)) if r.key == "type")
    assert row.kind == form.CHOICE
    assert set(row.choices) == set(gui_ports.caps(kind).types)


def test_an_event_has_no_default_value():
    """There is no value to default. Offering the row invites an edit that
    cannot be honoured."""
    import gui_ports
    kind = next((k for k in PALETTE if gui_ports.caps(k).binder == "event"),
                None)
    if kind is None:
        pytest.skip("no event-binder kind in the palette")
    assert not any(r.key == "default" for r in form.port_fields(mk(kind)))


def test_a_note_row_is_never_collected():
    """It has no key, so a caller that submitted one would write to "" ."""
    rows = form.port_fields(mk(_kind_with_ports()))
    assert form.collect_all(rows, {"": "anything"}) == {}


# ============================================================
# The colour block
# ============================================================

def test_a_kind_that_cannot_be_coloured_says_so():
    """ttk will not honour bg on a Notebook. Offering the picker produces an
    edit that silently does nothing, which reads as a broken app."""
    import gui_colors
    kind = next((k for k in PALETTE if not gui_colors.caps(k)), None)
    if kind is None:
        pytest.skip("every kind can be coloured")
    rows = form.colour_fields(mk(kind))
    assert [r.kind for r in rows] == [form.NOTE]
    assert rows[0].label.strip()


def test_only_the_channels_the_kind_honours_are_offered():
    import gui_colors
    for kind in PALETTE:
        able = gui_colors.caps(kind)
        if not able:
            continue
        keys = {r.key for r in form.colour_fields(mk(kind))}
        assert keys == set(able), f"{kind}: offered {keys}, honours {set(able)}"


def test_inherit_is_the_first_option():
    """It is the REVERT. A picker with no way back to unset makes the first
    click permanent."""
    assert form.colour_options()[0] == "(inherit)"


def test_every_palette_colour_is_pickable():
    import gui_colors
    options = form.colour_options()
    for name, colours in gui_colors.PALETTES.items():
        for hex_value in colours:
            assert any(form.option_hex(o).lower() == hex_value.lower()
                       for o in options), f"{name} {hex_value} is unpickable"


def test_a_swatch_label_resolves_back_to_its_hex():
    option = form.colour_options()[1]
    assert form.option_hex(option).startswith("#")
    assert form.hex_option(form.option_hex(option)) == option


def test_inherit_resolves_to_no_colour():
    assert form.option_hex("(inherit)") == ""
    assert form.option_hex("") == ""
    assert form.hex_option("") == ""


def test_a_colour_no_palette_has_matches_no_swatch():
    """Rather than the first one, which would silently retheme the widget."""
    assert form.hex_option("#123457") == ""


def test_a_junk_colour_on_a_shape_does_not_break_the_panel():
    """A .gspec can be edited by hand. An inspector that throws while OPENING
    leaves no way to fix the file from the app that wrote it."""
    import gui_colors
    kind = next(k for k in PALETTE if gui_colors.caps(k))
    rows = form.colour_fields(mk(kind, bg="not a colour", fg=None))
    assert all(r.value == "" for r in rows)


def test_a_shorthand_hex_is_normalised():
    import gui_colors
    kind = next(k for k in PALETTE if "bg" in gui_colors.caps(k))
    row = next(r for r in form.colour_fields(mk(kind, bg="#abc"))
               if r.key == "bg")
    assert row.value == gui_colors.normalise("#abc")


# ============================================================
# The window panel
# ============================================================

class _Window:
    title, min_w, min_h, bg, fg = "App", 640, 480, "#1e1e2e", ""


def test_the_window_panel_carries_the_windows_own_values():
    rows = {r.key: r.value for r in form.window_fields(_Window())}
    assert rows["title"] == "App"
    assert rows["min_w"] == 640
    assert rows["bg"] == "#1e1e2e"


def test_the_window_can_be_coloured():
    """Without this the colour story lands for every widget and not for the
    window they sit on — and the window's colour is uneditable from the UI."""
    kinds = {r.key: r.kind for r in form.window_fields(_Window())}
    assert kinds["bg"] == form.COLOUR and kinds["fg"] == form.COLOUR


def test_the_declared_packages_are_shown_as_typed():
    row = next(r for r in form.window_fields(_Window(), ["pypylon", "numpy"])
               if r.key == "requires")
    assert row.value == "pypylon, numpy"


def test_no_declared_packages_is_an_empty_field_not_the_word_none():
    row = next(r for r in form.window_fields(_Window()) if r.key == "requires")
    assert row.value == ""


def test_the_window_size_fields_are_numbers():
    rows = {r.key: r.kind for r in form.window_fields(_Window())}
    assert rows["min_w"] == form.NUMBER and rows["min_h"] == form.NUMBER


# ============================================================
# collect_all
# ============================================================

def test_collect_all_still_returns_only_what_was_submitted():
    rows = form.fields_for([mk()]) + form.port_fields(mk(_kind_with_ports()))
    assert form.collect_all(rows, {}) == {}


def test_plain_rows_and_port_rows_land_in_different_places():
    shape = mk(_kind_with_ports(), label="Go")
    rows = form.fields_for([shape]) + form.port_fields(shape)
    changes = form.collect_all(rows, {"label": "Stop", "dir": "event"})
    assert changes["label"] == "Stop"
    assert "dir" not in changes
    assert changes["port"]["dir"] == "e"
