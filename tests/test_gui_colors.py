"""
gui_colors — colour data, colour maths, and the capability table.

The load-bearing test is test_caps_covers_the_catalogue_exactly. The whole
point of COLOUR_CAPS is that no widget kind can be offered a picker the emitter
cannot honour; a kind added to the catalogue without an entry here would
default to "no caps" silently, which is the failure mode inverted rather than
prevented. Pinning the two sets together forces the decision.

Run:  python -m pytest tests/test_gui_colors.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui_colors as gcol           # noqa: E402
from gui_shapes import PALETTE      # noqa: E402


class FakeShape:
    """Duck-typed stand-in — resolve_scene must not need the real Shape."""

    def __init__(self, sid, kind, bg="", fg=""):
        self.id, self.kind, self.bg, self.fg = sid, kind, bg, fg


# ============================================================
# Purity
# ============================================================

def test_the_module_is_pure():
    import ast
    src = (Path(__file__).resolve().parent.parent / "gui_colors.py").read_text(
        encoding="utf-8")
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    banned = {m for m in mods
              if m in {"tkinter", "council_engine", "gui_shapes", "gui_canvas"}
              or m.startswith("vault_")}
    assert not banned, f"gui_colors must stay pure; imports {banned}"


# ============================================================
# THE CROSS-FILE INVARIANT
# ============================================================

def test_caps_covers_the_catalogue_exactly():
    """Every widget kind must have an explicit answer. A kind with no entry
    silently reports "cannot be coloured", which looks like a decision and is
    not one."""
    assert set(gcol.COLOUR_CAPS) == set(PALETTE), (
        f"missing: {set(PALETTE) - set(gcol.COLOUR_CAPS)}; "
        f"stale: {set(gcol.COLOUR_CAPS) - set(PALETTE)}")


def test_every_uncolourable_kind_explains_itself():
    """A picker that vanishes with no reason reads as a bug."""
    for kind, c in gcol.COLOUR_CAPS.items():
        if not c:
            assert gcol.note(kind), f"{kind} offers nothing and says nothing"


def test_caps_values_are_a_closed_set():
    for kind, c in gcol.COLOUR_CAPS.items():
        assert set(c) <= {"bg", "fg"}, f"{kind} has a bogus channel: {c}"
        assert not ("fg" in c and "bg" not in c), (
            f"{kind} offers text colour but no background — the derived "
            "foreground rule has nothing to work from")


# ============================================================
# Grammar
# ============================================================

def test_the_grammar_is_closed():
    assert gcol.is_colour("") and gcol.is_colour("#abc")
    assert gcol.is_colour("#AABBCC")
    # Tk would accept these; we do not.
    assert not gcol.is_colour("red")
    assert not gcol.is_colour("SystemButtonFace")
    assert not gcol.is_colour("#ab")
    assert not gcol.is_colour("#abcdefff")


def test_normalise_expands_and_lowercases():
    assert gcol.normalise("#ABC") == "#aabbcc"
    assert gcol.normalise("#A1B2C3") == "#a1b2c3"
    assert gcol.normalise("") == ""
    assert gcol.normalise(None) == ""


def test_normalise_refuses_a_colour_name_loudly():
    """Caught here rather than as a TclError on a line the user never wrote,
    when the generated app starts."""
    with pytest.raises(ValueError):
        gcol.normalise("chartreuse")


def test_to_hex_clamps_and_drops_alpha():
    assert gcol.to_hex((255, 0, 0)) == "#ff0000"
    assert gcol.to_hex((300, -5, 0)) == "#ff0000"
    assert gcol.to_hex((1, 2, 3, 255)) == "#010203", "alpha is dropped"
    with pytest.raises(ValueError):
        gcol.to_hex((1, 2))


# ============================================================
# Maths
# ============================================================

def test_mix_hits_both_ends_and_the_middle():
    assert gcol.mix("#000000", "#ffffff", 0.0) == "#000000"
    assert gcol.mix("#000000", "#ffffff", 1.0) == "#ffffff"
    assert gcol.mix("#000000", "#ffffff", 0.5) == "#808080"


def test_mix_clamps_out_of_range_t():
    assert gcol.mix("#000000", "#ffffff", 5.0) == "#ffffff"
    assert gcol.mix("#000000", "#ffffff", -3.0) == "#000000"


def test_contrast_ratio_matches_the_wcag_extremes():
    assert gcol.contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)
    assert gcol.contrast_ratio("#123456", "#123456") == pytest.approx(1.0, abs=0.001)


def test_auto_fg_picks_the_readable_one():
    assert gcol.contrast_ratio(gcol.auto_fg("#1e1e2e"), "#1e1e2e") > 4.5
    assert gcol.contrast_ratio(gcol.auto_fg("#f5f5f7"), "#f5f5f7") > 4.5
    assert gcol.auto_fg("") == ""


@pytest.mark.parametrize("name", list(gcol.PALETTES))
def test_every_shipped_swatch_is_a_legal_colour(name):
    for hexv in gcol.palette(name):
        assert gcol.is_colour(hexv), f"{name} has a bad swatch {hexv!r}"
        assert gcol.normalise(hexv) == hexv, f"{name}/{hexv} is not canonical"


def test_the_palettes_the_user_asked_for_are_present():
    assert {"Data's Inferno", "Light", "NES", "Game Boy"} <= set(gcol.PALETTES)
    assert gcol.DEFAULT_PALETTE in gcol.PALETTES


def test_gameboy_is_the_canonical_dmg_ramp():
    assert gcol.GAMEBOY == ["#0f380f", "#306230", "#8bac0f", "#9bbc0f"]


def test_the_nes_filler_white_was_dropped_in_the_port():
    """The source list padded to 56 with a #ffffff that is not an NES colour;
    the real NES white #fcfcfc is already in the set."""
    assert "#fcfcfc" in gcol.NES
    assert "#ffffff" not in gcol.NES


def test_no_palette_has_duplicates():
    for name, cols in gcol.PALETTES.items():
        assert len(cols) == len(set(cols)), f"{name} repeats a swatch"


def test_unknown_palette_raises():
    with pytest.raises(KeyError):
        gcol.palette("Commodore 64")


# ============================================================
# Inheritance
# ============================================================

def test_a_child_inherits_its_containers_background():
    frame = FakeShape("f", "frame", bg="#313244")
    label = FakeShape("l", "label")
    got = gcol.resolve_scene([frame, label], {"l": "f"})
    assert got["l"][0] == "#313244"


def test_an_inherited_background_gets_readable_text():
    """Otherwise the label keeps the platform's default black on a dark panel,
    which is the common case rather than the corner case."""
    frame = FakeShape("f", "frame", bg="#1e1e2e")
    label = FakeShape("l", "label")
    bg, fg = gcol.resolve_scene([frame, label], {"l": "f"})["l"]
    assert fg and gcol.contrast_ratio(fg, bg) > 4.5


def test_an_own_colour_beats_an_inherited_one():
    frame = FakeShape("f", "frame", bg="#313244")
    btn = FakeShape("b", "button", bg="#a6e3a1")
    assert gcol.resolve_scene([frame, btn], {"b": "f"})["b"][0] == "#a6e3a1"


def test_inheritance_walks_more_than_one_level():
    outer = FakeShape("o", "frame", bg="#313244")
    mid = FakeShape("m", "frame")
    label = FakeShape("l", "label")
    got = gcol.resolve_scene([outer, mid, label], {"l": "m", "m": "o"})
    assert got["l"][0] == "#313244"


def test_an_uncolourable_kind_inherits_nothing():
    """The wireframe must not paint a colour the generated app will not."""
    frame = FakeShape("f", "frame", bg="#313244")
    tree = FakeShape("t", "treeview")
    assert gcol.resolve_scene([frame, tree], {"t": "f"})["t"] == ("", "")


def test_a_hand_edited_colour_on_an_uncolourable_kind_is_ignored():
    tree = FakeShape("t", "treeview", bg="#ff0000")
    assert gcol.resolve_scene([tree], {})["t"] == ("", "")


def test_a_frame_never_gets_a_foreground():
    """It has no text; offering one would be a control that does nothing."""
    frame = FakeShape("f", "frame", bg="#313244")
    assert gcol.resolve_scene([frame], {})["f"][1] == ""


def test_a_malformed_colour_degrades_instead_of_raising():
    """Reached only from a hand-edited .gspec, and the canvas redraws with this
    on every mouse move — an exception there would wedge the designer."""
    bad = FakeShape("b", "button", bg="not a colour")
    assert gcol.resolve_scene([bad], {})["b"] == ("", "")


def test_a_parent_cycle_terminates():
    """A hand-edited file can describe one, and this runs in the redraw loop."""
    a = FakeShape("a", "frame")
    b = FakeShape("b", "frame")
    got = gcol.resolve_scene([a, b], {"a": "b", "b": "a"})
    assert got["a"] == ("", "") and got["b"] == ("", "")


def test_no_parents_at_all_is_fine():
    btn = FakeShape("b", "button", bg="#a6e3a1")
    assert gcol.resolve_scene([btn])["b"][0] == "#a6e3a1"


def test_an_empty_scene_resolves_to_nothing():
    assert gcol.resolve_scene([], {}) == {}
