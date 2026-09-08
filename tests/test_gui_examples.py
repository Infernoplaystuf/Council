"""
gui_examples — the worked wireframes a designing model is shown.

These exist because RULES DID NOT WORK. A local model asked to design a GUI
produced, despite explicit prohibitions: a full-canvas Frame and a full-canvas
Notebook stacked on each other (blank app), one of three identical rows
captured by a container and two left outside, an image panel sized to exactly
fill its parent, and no background colour at all. A complete correct example
is a stronger signal than a list of boundaries.

So the load-bearing property is that the examples are CORRECT — if one drifts
into the shape the model already gets wrong, it teaches the mistake. Every
test below checks the examples against the same gates a real project passes.

Run:  python -m pytest tests/test_gui_examples.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui_examples as gx      # noqa: E402
import gui_layout as gl        # noqa: E402
import gui_spec as gsp         # noqa: E402
import gui_shapes as gs        # noqa: E402


def test_the_examples_exist():
    assert gx.names(), "no examples on disk"
    assert "barbie_capture" in gx.names()


@pytest.mark.parametrize("name", gx.names() or ["barbie_capture"])
def test_every_example_loads_as_a_real_gspec(name, tmp_path):
    """They must be loadable by the app itself, not just valid JSON — an
    example the designer cannot open is not an example."""
    raw = gx.load(name)
    p = tmp_path / f"{name}.gspec"
    p.write_text(json.dumps(raw), encoding="utf-8")
    proj = gs.load_gspec(p)
    assert proj.shapes, "an example with no shapes teaches nothing"


@pytest.mark.parametrize("name", gx.names() or ["barbie_capture"])
def test_every_example_passes_validate(name, tmp_path):
    """THE LOAD-BEARING TEST. An example that would be rejected by the app
    is teaching the model to produce rejected wireframes."""
    raw = gx.load(name)
    p = tmp_path / f"{name}.gspec"
    p.write_text(json.dumps(raw), encoding="utf-8")
    proj = gs.load_gspec(p)
    tree = gl.infer(proj.shapes, proj.canvas.w, proj.canvas.h)
    spec = gsp.build(proj.shapes, tree, project=name,
                     title=proj.window.title,
                     root_bg=proj.window.bg, root_fg=proj.window.fg,
                     root_font=proj.window.font)
    ok, errs = gsp.validate(spec)
    assert ok, f"{name} would be REJECTED by the app: {errs}"


@pytest.mark.parametrize("name", gx.names() or ["barbie_capture"])
def test_no_example_stacks_two_full_canvas_containers(name):
    """The exact failure the examples exist to prevent."""
    raw = gx.load(name)
    cw, ch = raw.get("canvas", {}).get("w", 1280), raw.get("canvas", {}).get("h", 800)
    full = [s for s in raw["shapes"]
            if s.get("w", 0) >= cw * 0.95 and s.get("h", 0) >= ch * 0.95]
    assert len(full) < 2, f"{name} stacks {len(full)} full-canvas shapes"


@pytest.mark.parametrize("name", gx.names() or ["barbie_capture"])
def test_every_example_uses_only_real_palette_kinds(name):
    for s in gx.load(name)["shapes"]:
        assert s["kind"] in gs.PALETTE, f"{name}: unknown kind {s['kind']!r}"


def test_the_examples_demonstrate_every_declaration_type():
    """If a declaration has no worked example, a model has nothing to copy —
    which is how `drives` and `script` would get guessed at."""
    seen = set()
    for n in gx.names():
        for s in gx.load(n)["shapes"]:
            seen.update(k for k in ("port", "script", "drives") if s.get(k))
        w = gx.load(n).get("window") or {}
        seen.update(k for k in ("bg", "fg", "font") if w.get(k))
    for needed in ("port", "script", "drives", "bg", "fg", "font"):
        assert needed in seen, f"no example demonstrates {needed!r}"


# ============================================================
# The rendered context
# ============================================================

def test_for_prompt_is_compact_enough_to_actually_send():
    """A 4096-token model cannot afford a 3k-token example block. This is a
    budget, not a preference — exceeding it means the example crowds out the
    user's own request."""
    text = gx.for_prompt()
    approx_tokens = len(text) // 4
    assert approx_tokens < 2200, f"{approx_tokens} tokens is too much context"


def test_for_prompt_drops_props_that_are_only_defaults():
    """A model that sees "text": "" in every example learns to emit it."""
    text = gx.for_prompt()
    assert '"text": ""' not in text
    assert '"wraplength": 0' not in text


def test_for_prompt_keeps_props_the_author_chose():
    """The folder mode is the whole point of that widget in the example."""
    text = gx.for_prompt("image_viewer")
    assert '"mode": "folder"' in text


def test_for_prompt_never_leaks_shape_ids():
    """ids are assigned by the app. An example carrying them invites a model
    to invent its own, which then collide with the registry."""
    text = gx.for_prompt()
    assert '"id"' not in text


def test_for_prompt_explains_the_declarations():
    text = gx.for_prompt()
    for token in ("drives", "script", "port", "inherits"):
        assert token in text, f"the help text never mentions {token!r}"


def test_for_prompt_of_one_example_is_smaller_than_both():
    assert len(gx.for_prompt("barbie_capture")) < len(gx.for_prompt())


def test_an_unknown_example_raises_with_the_available_names():
    with pytest.raises(KeyError) as exc:
        gx.load("no_such_example")
    assert "barbie_capture" in str(exc.value)


def test_module_is_pure():
    import ast
    src = (Path(__file__).resolve().parent.parent / "gui_examples.py").read_text(
        encoding="utf-8")
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    banned = {m for m in mods
              if m in {"tkinter", "council_engine"} or m.startswith("vault_")}
    assert not banned, f"gui_examples must stay pure; imports {banned}"
