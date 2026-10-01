"""
"Describe it" on SMALL local models (3.8B-8B): the reply schema, the layout
tree, best-of-N, diversified repairs, near-JSON and truncation salvage, and
worked examples chosen per request.

NO MODEL IS LOADED ANYWHERE IN THIS FILE. Every model is a scripted stub that
replays the kinds of reply a small model writes — fenced, trailing commas,
Python spellings, "dropdown" for combobox, props beside "kind", overlapping
pixels, a reply cut off by the token limit — and records the keywords each
call received, so the profile's seeds, temperatures and schema are visible.

THE LOAD-BEARING PROPERTY, as in test_gui_describe.py: nothing accepted can
be refused by Generate. Every tree the layouter places is re-run here through
gui_layout.infer -> gui_spec.build -> gui_spec.validate, independently.

Run:  python -m pytest tests/test_gui_describe_small.py -q
"""
from __future__ import annotations

import copy
import json
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gui_describe as gd            # noqa: E402
import gui_describe_tree as gt       # noqa: E402
import gui_layout as gl              # noqa: E402
import gui_spec as gsp               # noqa: E402
from gui_shapes import CONTAINER_KINDS, PALETTE, load_gspec  # noqa: E402

QT_TESTS = ROOT / "examples" / "gui" / "qt_tests"
#: The A and B tiers. C is degraded on purpose (README): a float scale and
#: two menubar shapes describe's own type check refuses.
FIXTURES = sorted(p.stem for p in QT_TESTS.glob("[ab]_*.gspec"))
CANVASES = [(1100, 700), (1504, 1016), (1280, 800), (900, 600), (1920, 1080)]


class Model:
    """A scripted model that takes the keywords designer_project's call
    takes. The last reply repeats once the script runs out."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, prompt, *, json_schema=None, seed=None,
                 temperature=None, num_predict=None, should_stop=None):
        self.calls.append({"prompt": prompt, "schema": json_schema,
                           "seed": seed, "temperature": temperature,
                           "num_predict": num_predict})
        if not self.replies:
            return ""
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]

    @property
    def prompts(self):
        return [c["prompt"] for c in self.calls]


def tree_reply(layout, title="App", **window):
    return json.dumps({"window": {"title": title, **window}, "layout": layout})


def leaf(kind, label="", **extra):
    return {"kind": kind, "label": label, **extra}


def col(*children, **extra):
    return {"kind": "column", "children": list(children), **extra}


def row(*children, **extra):
    return {"kind": "row", "children": list(children), **extra}


SMALL = gd.profile_for(3.8, n_ctx=4096, model="phi3.5")
MEDIUM = gd.profile_for(8.0, n_ctx=8192, model="llama3.1:8b")

FORM_TREE = col(
    row(leaf("label", "Name"), leaf("entry", "Name box")),
    row(leaf("label", "Password"), leaf("entry", "Password box",
                                         props={"show": "*"})),
    row(leaf("button", "Sign in"), leaf("button", "Cancel")))


def gate(shapes, window, canvas):
    """Generate's own pipeline, run independently of gui_describe."""
    tree = gl.infer(shapes, *canvas)
    spec = gsp.build(shapes, tree, project="t",
                     title=window.get("title", ""),
                     root_bg=window.get("bg", ""), root_fg=window.get("fg", ""),
                     root_font=window.get("font", ""))
    ok, errs = gsp.validate(spec)
    return ok, errs, tree


def assert_clean_geometry(shapes, canvas):
    """On the grid, inside the canvas, every child inset in its parent, and
    no two siblings overlapping — what the layouter promises."""
    by_id = {s.id: s for s in shapes}
    for s in shapes:
        for v in (s.x, s.y, s.w, s.h):
            assert v % gd.GRID == 0, s
        assert s.x >= gt.MARGIN and s.y >= gt.MARGIN, s
        assert s.x + s.w <= canvas[0] and s.y + s.h <= canvas[1], s
    kids = gl.build_containment_tree(shapes, warnings=[])
    for parent, ids in kids.items():
        sibs = [by_id[i] for i in ids]
        for n, a in enumerate(sibs):
            for b in sibs[n + 1:]:
                ox, oy = gd._overlap(a, b)
                assert ox <= 0 or oy <= 0, (a, b)
        if parent is not None:
            p = by_id[parent]
            for c in sibs:
                assert c.x - p.x >= gt.INSET and p.x + p.w - (c.x + c.w) >= 0


def fixture_tree(name):
    project = load_gspec(QT_TESTS / f"{name}.gspec")
    return project, gt.tree_from_shapes(project.shapes)


# ============================================================
# The reply schemas
# ============================================================

try:
    import jsonschema
except ImportError:                      # the council env has it
    jsonschema = None
needs_jsonschema = pytest.mark.skipif(jsonschema is None,
                                      reason="jsonschema not installed")


def _compact_payload(name, canvas=(1100, 700)):
    raw = json.loads((QT_TESTS / f"{name}.gspec").read_text(encoding="utf-8"))
    return gd._rescaled(raw, *canvas)


@needs_jsonschema
@pytest.mark.parametrize("name", FIXTURES)
def test_the_pixel_schema_admits_every_wireframe_the_gate_accepts(name):
    """A schema stricter than check_reply would forbid correct designs at
    the sampler, where no repair round can reach them. (The schema requires
    "label", which the compact form leaves out when empty; "" is the same
    design, so it is filled in — requiring it costs a model nothing and
    gets every widget a caption to name its port from.)"""
    payload = _compact_payload(name)
    payload["window"] = {"title": "T", **payload["window"]}
    for s in payload["shapes"]:
        s.setdefault("label", "")
    assert gd.check_reply(json.dumps(payload)).ok
    jsonschema.validate(payload, gd.wireframe_schema(1100, 700))


@needs_jsonschema
@pytest.mark.parametrize("name", FIXTURES)
def test_the_tree_schema_admits_every_qt_tests_tree(name):
    _project, tree = fixture_tree(name)
    jsonschema.validate({"window": {"title": "T"}, "layout": tree},
                        gt.tree_schema())


@needs_jsonschema
@pytest.mark.parametrize("bad, why", [
    ({"kind": "dropdown"}, "unknown kind"),
    ({"bg": "pink"}, "colour name"),
    ({"props": {"values": "Low,High"}, "kind": "combobox"}, "string for a list"),
    ({"id": "x"}, "refused key"),
    ({"bg": "#ff0000", "kind": "combobox"}, "colour a combobox cannot take"),
    ({"props": {"colour_scheme": "inferno"}}, "unknown prop"),
    ({"font": "Arial"}, "font without a size"),
])
def test_the_pixel_schema_refuses_what_a_small_model_gets_wrong(bad, why):
    shape = {"kind": "button", "label": "Go", "x": 16, "y": 16, "w": 112,
             "h": 32}
    shape.update(bad)
    payload = {"window": {"title": "T"}, "shapes": [shape]}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, gd.wireframe_schema())


@needs_jsonschema
@pytest.mark.parametrize("bad", [
    {"kind": "button", "label": "Go", "children": [leaf("label", "x")]},
    {"kind": "button", "label": "Go", "x": 10},
    {"kind": "notebook", "label": "N", "children": [leaf("entry", "e")]},
    {"kind": "row", "children": []},
    {"kind": "fancy_slider", "label": "s"},
])
def test_the_tree_schema_refuses_bad_nodes(bad):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"window": {"title": "T"}, "layout": col(bad)},
                            gt.tree_schema())


def _walk(node, path=""):
    if isinstance(node, dict):
        yield path, node
        for k, v in node.items():
            yield from _walk(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}/{i}")


@pytest.mark.parametrize("schema", [
    gd.wireframe_schema(), gt.tree_schema()], ids=["pixel", "tree"])
def test_every_list_and_string_in_a_schema_is_bounded(schema):
    """A grammar cannot close an object the token limit cut off, so a
    constrained reply must not be able to run on: every array has maxItems,
    every free string maxLength (or a pattern / enum)."""
    for path, node in _walk(schema):
        if node.get("type") == "array":
            assert "maxItems" in node, path
        if node.get("type") == "string":
            assert any(k in node for k in ("maxLength", "pattern", "enum")), \
                path


@pytest.mark.parametrize("schema", [
    gd.wireframe_schema(), gt.tree_schema()], ids=["pixel", "tree"])
def test_kind_is_the_first_key_of_every_variant(schema):
    """Required properties are emitted in order by the grammar converter:
    with "kind" first the sampler commits to a kind before writing anything
    that is typed by it."""
    for path, node in _walk(schema):
        props = node.get("properties") if isinstance(node, dict) else None
        if isinstance(props, dict) and "kind" in props:
            assert list(props)[0] == "kind", path
            assert node["required"][0] == "kind", path


def test_reply_schema_is_the_same_json_each_time_and_safe_to_edit():
    a = gd.reply_schema(SMALL, 1100, 700)
    b = gd.reply_schema(SMALL, 1100, 700)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    a["$defs"].clear()
    assert gd.reply_schema(SMALL, 1100, 700)["$defs"], "cache poisoned"
    assert "layout" in b["properties"]
    assert "shapes" in gd.reply_schema(gd.LEGACY, 1100, 700)["properties"]


def test_the_schemas_compile_to_a_llama_cpp_grammar():
    """Compile only — no model. llama-cpp-python's own converter, the path
    LlamaGrammar.from_json_schema takes. (Run under the interpreter that has
    llama_cpp; the council env does not, so there it is skipped and the
    measurement lives in the report.)"""
    grammar = pytest.importorskip("llama_cpp.llama_grammar")
    for schema in (gd.wireframe_schema(), gd.wireframe_schema(1504, 1016, 40),
                   gt.tree_schema()):
        gbnf = grammar.json_schema_to_gbnf(json.dumps(schema))
        assert gbnf.startswith("") and "root ::=" in gbnf
        grammar.LlamaGrammar.from_json_schema(json.dumps(schema),
                                              verbose=False)


# ============================================================
# Profiles
# ============================================================

@pytest.mark.parametrize("params, mode, best", [
    (3.8, "tree", 3), (8.0, "tree", 2), (14.7, "pixel", 1),
    (None, "tree", 2), (20.9, "pixel", 1)])
def test_the_profile_follows_the_model_size(params, mode, best):
    p = gd.profile_for(params)
    assert (p.mode, p.n_best, p.constrained) == (mode, best, True)
    assert len(p.temperatures) == best and len(set(p.temperatures)) == best


def test_a_request_with_exact_positions_keeps_pixel_mode():
    p = gd.profile_for(3.8, text="a Run button at x=40, y=200 and a 300 px "
                                 "wide log")
    assert p.mode == "pixel"
    assert gd.profile_for(3.8, text="a login form").mode == "tree"


@pytest.mark.parametrize("text, exact", [
    ("A stage panel: X and Y coordinates entries and a Move button", False),
    ("A GPS logger showing coordinates in a table", False),
    ("Enter the ROI coordinates, then press Crop", False),
    ("Put the logo at pixel coordinates 16, 16", True),
    ("use exact coordinates for every widget", True),
    ("the OK button at screen coordinates (40, 600)", True)])
def test_coordinates_as_data_do_not_force_pixel_mode(text, exact):
    """REVIEW: any "coordinates" sent a 3.8B model to pixel mode — and a
    stage's or an ROI's coordinates are what this user's apps edit."""
    assert gd.wants_exact_positions(text) is exact
    assert gd.profile_for(3.8, text=text).mode == ("pixel" if exact
                                                   else "tree")


@pytest.mark.parametrize("name, size", [
    ("ollama:llama3.1:8b", 8.0), ("ollama:phi3.5", 3.8),
    ("Phi-3.5-mini-instruct-Q4_K_M", 3.8), ("phi-4-Q4_K_M", 14.7),
    ("ollama:gpt-oss:20b", 20.0), ("granite-3.1-8b-instruct.Q4_K_M", 8.0),
    ("Llama-3.2-3B-Instruct-Q5_K_M", 3.0), ("mystery", None)])
def test_params_are_read_from_a_model_name(name, size):
    assert gd.parse_params_b(name) == size


def test_the_reply_budget_and_window_come_from_the_profile():
    p = gd.profile_for(3.8, n_ctx=8192)
    assert p.budget_chars == int((8192 - p.reply_tokens - gd.SLACK_TOKENS)
                                 * gd.CHARS_PER_TOKEN)
    assert gd.LEGACY.budget_chars >= gd.DEFAULT_BUDGET_CHARS - 1


def test_without_a_profile_the_model_gets_the_prompt_alone():
    """The plain path is unchanged: a caller that passes no profile sends
    no schema, seed or temperature — every existing model call still works."""
    model = Model(json.dumps({"window": {"title": "x"}, "shapes": [
        {"kind": "button", "label": "Go", "x": 16, "y": 16, "w": 112,
         "h": 32}]}))
    res = gd.describe("a button", model_call=model)
    assert res.ok and len(model.calls) == 1
    c = model.calls[0]
    assert (c["schema"], c["seed"], c["temperature"], c["num_predict"]) == (
        None, None, None, None)


def test_call_model_passes_only_what_the_callable_takes():
    seen = []
    assert gd.call_model(lambda p: seen.append(p) or "a", "x",
                         json_schema={}, seed=3) == "a"

    def some(prompt, *, seed=None):
        seen.append(seed)
        return "b"
    assert gd.call_model(some, "x", json_schema={}, seed=4) == "b"
    assert seen == ["x", 4]

    def inner_type_error(prompt, **kw):
        raise TypeError("a real bug inside the model")
    with pytest.raises(TypeError, match="real bug"):
        gd.call_model(inner_type_error, "x", seed=1)


# ============================================================
# The layouter: every qt_tests-shaped layout passes the gate
# ============================================================

@pytest.mark.parametrize("canvas", CANVASES, ids=lambda c: f"{c[0]}x{c[1]}")
@pytest.mark.parametrize("name", FIXTURES)
def test_every_qt_tests_layout_written_as_a_tree_passes_the_gate(name,
                                                                 canvas):
    project, tree = fixture_tree(name)
    model = Model(tree_reply(tree, title=project.window.title))
    res = gd.describe("x", model_call=model, canvas_w=canvas[0],
                      canvas_h=canvas[1], profile=SMALL)
    assert res.ok, res.errors
    assert res.mode == "tree" and len(model.calls) == 1
    assert sorted(s.kind for s in res.shapes) == sorted(
        s.kind for s in project.shapes)
    ok, errs, tree_ = gate(res.shapes, res.window, canvas)
    assert ok, f"describe accepted what Generate refuses: {errs}"
    assert not [w for w in tree_.warnings if " overlap, so " in w]
    assert_clean_geometry(res.shapes, canvas)


@pytest.mark.parametrize("name", FIXTURES)
def test_nesting_survives_the_round_trip(name):
    """Drawn wireframe -> tree -> layout: the same parent for every widget
    (by label and kind), so a tree example teaches the drawing it came
    from."""
    project, tree = fixture_tree(name)
    res = gd.describe("x", model_call=Model(tree_reply(tree)), profile=SMALL)
    assert res.ok, res.errors

    def parents(shapes):
        # A notebook page is titled by its TAB in a tree ("Advanced", not
        # the frame's own "Advanced page"), so pages compare by kind only.
        kids = gl.build_containment_tree(shapes, warnings=[])
        by_id = {s.id: s for s in shapes}
        pages = {i for p, ids in kids.items() if p
                 and by_id[p].kind == "notebook" for i in ids}

        def name(i):
            if i in pages:
                return (by_id[i].kind,)
            return (by_id[i].kind, by_id[i].label)
        out = []
        for p, ids in kids.items():
            out += [(name(i), name(p) if p else None) for i in ids]
        return sorted(out, key=repr)
    assert parents(res.shapes) == parents(project.shapes)


def test_layout_is_deterministic():
    _p, tree = fixture_tree("b_notebook_tabs")
    a = gd.check_reply(tree_reply(tree))
    b = gd.check_reply(tree_reply(tree))
    assert [(s.kind, s.x, s.y, s.w, s.h) for s in a.shapes] == \
        [(s.kind, s.x, s.y, s.w, s.h) for s in b.shapes]


LEAVES = [k for k in gt.LEAF_KINDS if k not in ("menubar",)]


def _random_tree(rng, depth=0):
    if depth >= 3 or rng.random() < 0.45:
        k = rng.choice(LEAVES)
        return leaf(k, f"{k} {rng.randrange(1000)}")
    kind = rng.choice(["row", "column", "frame", "labelframe", "notebook",
                       "panedwindow"])
    n = rng.randint(1, 4 if kind != "panedwindow" else 3)
    kids = [_random_tree(rng, depth + 1) for _ in range(max(n, 2)
                                                         if kind ==
                                                         "panedwindow" else n)]
    if kind == "notebook":
        return {"kind": "notebook", "label": f"book {rng.randrange(1000)}",
                "children": [{"kind": "page", "label": f"Tab {i}",
                              "children": [c]} for i, c in enumerate(kids)]}
    if kind in ("row", "column"):
        return {"kind": kind, "children": kids}
    return {"kind": kind, "label": f"{kind} {rng.randrange(1000)}",
            "children": kids}


def test_seeded_random_trees_never_raise_and_every_accepted_one_generates():
    """The reviewer's method, for trees: anything the layouter places and
    check_reply accepts must pass Generate's validator independently."""
    rng = random.Random(20261001)
    accepted = 0
    # The only reasons a well-formed random tree may be refused: it is
    # far bigger than the window, too many widgets, or too deep. Never a
    # GATE fault — those would be the layouter's own geometry failing.
    allowed = ("at its natural size", "is too many", "nested more than")
    for _ in range(250):
        tree = col(*[_random_tree(rng) for _k in range(rng.randint(1, 4))])
        canvas = rng.choice(CANVASES)
        checked = gd.check_reply(tree_reply(tree), canvas_w=canvas[0],
                                 canvas_h=canvas[1])
        if not checked.ok:
            assert checked.faults, "a refusal always says why"
            assert all(any(a in f for a in allowed)
                       for f in checked.faults), (checked.faults, canvas)
            continue
        accepted += 1
        ok, errs, _t = gate(checked.shapes, checked.window, canvas)
        assert ok, (errs, tree)
        assert_clean_geometry(checked.shapes, canvas)
    assert accepted >= 120, accepted


def test_a_layout_far_too_big_is_a_fault_that_says_what_to_do():
    wide = row(*[leaf("button", f"Button number {i}") for i in range(24)])
    checked = gd.check_reply(tree_reply(col(wide)))
    assert not checked.ok and checked.stage == gd.STAGE_GATE
    assert any("notebook" in f and "px wide" in f for f in checked.faults)


def test_a_slightly_too_big_layout_is_squeezed_with_a_note():
    wide = row(*[leaf("button", f"Btn {i}") for i in range(9)])
    checked = gd.check_reply(tree_reply(col(wide)))
    assert checked.ok, checked.faults
    assert any("squeezed" in n for n in checked.notes)


def test_notebook_tabs_come_from_its_pages():
    book = {"kind": "notebook", "label": "Prefs", "props": {"tabs": ["X"]},
            "children": [
                {"kind": "page", "label": "General",
                 "children": [leaf("checkbutton", "Start on login")]},
                leaf("text", "Notes"),        # not a page: wrapped in one
            ]}
    checked = gd.check_reply(tree_reply(col(book)))
    assert checked.ok, checked.faults
    nb = next(s for s in checked.shapes if s.kind == "notebook")
    assert nb.props["tabs"] == ["General", "Notes"]
    assert any("made into the notebook page" in n for n in checked.notes)
    assert any("tab titles taken from its pages" in n for n in checked.notes)


def test_what_small_models_write_beside_kind_is_moved_into_props():
    tree = col(leaf("combobox", "Theme", values=["Light", "Dark"]),
               {"kind": "toolbar", "label": "Tools",
                "items": ["Open", "Save"]},
               leaf("textbox", "Name", x=10, y=20, w=200))
    checked = gd.check_reply(tree_reply(tree))
    assert checked.ok, checked.faults
    combo = next(s for s in checked.shapes if s.kind == "combobox")
    bar = next(s for s in checked.shapes if s.kind == "toolbar")
    assert combo.props["values"] == ["Light", "Dark"]
    assert bar.props["buttons"] == ["Open", "Save"]
    assert any(s.kind == "entry" for s in checked.shapes)
    text = " ".join(checked.notes)
    assert "moved values into" in text and "read 'textbox' as entry" in text
    assert "the layout places it" in text


def test_a_widget_with_children_and_an_unknown_kind_go_back_by_path():
    tree = col(leaf("button", "Go", children=[leaf("label", "x")]),
               leaf("holo_dial", "Speed"))
    checked = gd.check_reply(tree_reply(tree))
    assert checked.stage == gd.STAGE_SCHEMA and checked.tree
    text = "\n".join(checked.faults)
    assert 'button "Go": a button cannot hold other widgets' in text
    assert "'holo_dial' is not a widget kind" in text


def test_row_faults_are_told_in_the_terms_of_the_tree():
    """A prop of the wrong type is found by check_reply's row checks, which
    name shapes by position; the model wrote a tree and never saw one."""
    tree = col(row(leaf("label", "Theme"),
                   leaf("combobox", "Theme box",
                        props={"values": "Light,Dark"})))
    checked = gd.check_reply(tree_reply(tree))
    assert not checked.ok
    text = "\n".join(checked.faults)
    assert "shape " not in text
    assert 'combobox "Theme box": props.values must be a JSON list' in text


# ============================================================
# Replies a small model writes
# ============================================================

PIXEL_FORM = {"window": {"title": "Login"}, "shapes": [
    {"kind": "label", "label": "User", "x": 24, "y": 24, "w": 120, "h": 24},
    {"kind": "entry", "label": "User box", "x": 160, "y": 24, "w": 240,
     "h": 32},
    {"kind": "button", "label": "Sign in", "x": 24, "y": 80, "w": 112,
     "h": 32}]}

SLOPPY = """Here is the wireframe:
```json
{
  // the window
  "window": {"title": 'Login',},
  "shapes": [
    {"kind": "label", "label": "User", "x": 24, "y": 24, "w": 120, "h": 24},
    {"kind": "entry", "label": "User box", "x": 160, "y": 24, "w": 240, "h": 32, "props": {"show": "", "justify": None}},
    {"kind": "button", "label": "Sign in", "x": 24, "y": 80, "w": 112, "h": 32},  /* done */
  ],
}
```"""


def test_near_json_is_read_without_spending_a_round():
    model = Model(SLOPPY)
    res = gd.describe("a login form", model_call=model)
    assert res.ok, res.errors
    assert len(model.calls) == 1 and res.window["title"] == "Login"
    assert any("trailing commas" in n for n in res.notes)


def test_a_strict_reply_is_never_rewritten():
    payload, notes, salvaged = gd.parse_reply(json.dumps(PIXEL_FORM))
    assert payload == PIXEL_FORM and notes == [] and not salvaged


@pytest.mark.parametrize("prose", [
    "Here's the layout you asked for:\n",
    "Sure! Here's it:\n```json\n",
    "The users' window:\n"])
def test_an_apostrophe_in_the_prose_before_the_json_does_not_hide_it(prose):
    """REVIEW: the clean-up read the apostrophe in "Here's" as the start of a
    single-quoted string, so the whole JSON after it became one string and
    a reply with a trailing comma — or one cut off — lost its round."""
    sloppy = tree_reply(FORM_TREE).replace("}]", "},]")
    assert gd.check_reply(prose + sloppy).ok
    full = tree_reply(FORM_TREE)
    cut = full[:full.index('"Password"') - 30]
    checked = gd.check_reply(prose + cut)
    assert checked.ok and checked.salvaged, checked.faults


def test_a_cut_off_reply_is_held_while_calls_remain_then_used():
    """Truncated after its second widget: the whole answer is asked for
    while calls remain; if it never comes, the complete part is used, with
    a note that says what happened."""
    full = tree_reply(FORM_TREE)
    cut = full[:full.index('"Password"') - 30]
    model = Model(cut)
    res = gd.describe("a login form", model_call=model, profile=SMALL,
                      max_attempts=2)
    assert res.ok, res.errors
    assert len(model.calls) == SMALL.n_best + 1, "the whole answer was asked"
    assert "cut off" in model.calls[1]["prompt"] or \
        "cut off" in model.calls[-1]["prompt"]
    assert any("cut off before it finished" in n for n in res.notes)
    assert {s.label for s in res.shapes} >= {"Name", "Name box"}


def test_a_whole_answer_after_a_cut_off_one_wins():
    full = tree_reply(FORM_TREE)
    cut = full[: len(full) // 2]
    model = Model(cut, full)
    res = gd.describe("x", model_call=model, profile=gd.profile_for(
        14.7, mode="tree"))
    assert res.ok and len(model.calls) == 2
    assert not any("cut off before it finished" in n for n in res.notes)
    assert len(res.shapes) == 6


def test_overlapping_pixels_from_a_small_model_in_pixel_mode_go_back():
    bad = copy.deepcopy(PIXEL_FORM)
    bad["shapes"][1].update(x=24, y=24, w=400, h=200)       # over the label
    bad["shapes"].append({"kind": "frame", "label": "Panel", "x": 100,
                          "y": 60, "w": 500, "h": 400})
    checked = gd.check_reply(json.dumps(bad))
    assert not checked.ok and checked.stage == gd.STAGE_GATE


def test_a_tree_in_pixel_mode_and_rows_in_tree_mode_are_both_read():
    tree_model = Model(tree_reply(FORM_TREE))
    res = gd.describe("x", model_call=tree_model,
                      profile=gd.profile_for(14.7))
    assert res.ok and res.mode == "tree"
    pixel_model = Model(json.dumps(PIXEL_FORM))
    res = gd.describe("x", model_call=pixel_model, profile=SMALL)
    assert res.ok and res.mode == "pixel"


# ============================================================
# Best-of-N and repairs
# ============================================================

def test_round_one_tries_distinct_seeds_and_stops_at_the_first_valid():
    bad = tree_reply(col(leaf("holo_dial", "x")))
    good = tree_reply(FORM_TREE)
    model = Model(bad, good, good)
    res = gd.describe("a login form", model_call=model, profile=SMALL)
    assert res.ok and len(model.calls) == 2
    seeds = [c["seed"] for c in model.calls]
    temps = [c["temperature"] for c in model.calls]
    assert seeds == [1, 2] and temps == list(SMALL.temperatures[:2])
    assert all(c["schema"] and "layout" in c["schema"]["properties"]
               for c in model.calls)
    assert all(c["num_predict"] == SMALL.reply_tokens for c in model.calls)
    assert any("candidate 2 of 3 passed" in n for n in res.notes)


def test_the_best_candidate_is_the_one_repaired():
    """Three candidates, none valid: the repair starts from the one with
    rows that nearly work, not from the prose."""
    nearly = tree_reply(col(leaf("button", "Go"), leaf("holo_dial", "x")))
    model = Model("Sorry, I can't.", nearly, "{}", tree_reply(FORM_TREE))
    res = gd.describe("x", model_call=model, profile=SMALL)
    assert res.ok and len(model.calls) == 4
    repair = model.calls[3]["prompt"]
    assert "holo_dial" in repair.split("WHAT IS WRONG")[0]
    assert "Sorry" not in repair


def test_a_model_repeating_itself_is_never_asked_identically_again():
    bad = tree_reply(col(leaf("holo_dial", "x")))
    model = Model(bad)
    res = gd.describe("x", model_call=model, profile=gd.profile_for(
        8.0, n_best=1), max_attempts=4)
    assert not res.ok and len(model.calls) == 4
    asks = [(c["prompt"], c["temperature"], c["seed"]) for c in model.calls]
    assert len(set(asks)) == len(asks), "an identical request was repeated"
    temps = [c["temperature"] for c in model.calls[1:]]
    assert temps == sorted(temps) and temps[-1] > temps[0]
    assert gd.REPEAT_NOTE in model.calls[2]["prompt"]


def test_should_stop_ends_it_between_calls():
    asked = []

    def model(prompt, **kw):
        asked.append(prompt)
        return tree_reply(col(leaf("holo_dial", "x")))
    res = gd.describe("x", model_call=model, profile=SMALL,
                      should_stop=lambda: len(asked) >= 1)
    assert not res.ok and len(asked) == 1
    assert "stopped" in res.errors[0]


def test_the_validated_window_style_comes_back():
    model = Model(tree_reply(FORM_TREE, title="Sign in", bg="#1e1e2e",
                             fg="#ffffff", font="Arial 11 bold"))
    res = gd.describe("x", model_call=model, profile=SMALL)
    assert res.ok
    assert res.window == {"title": "Sign in", "bg": "#1e1e2e",
                          "fg": "#ffffff", "font": "Arial 11 bold"}


# ============================================================
# Worked examples, chosen per request
# ============================================================

@pytest.mark.parametrize("text, first", [
    ("a tabbed preferences window", "b_notebook_tabs"),
    ("a login form with username and password", "a_login_form"),
    ("a dashboard with a live chart and a log", "b_dashboard"),
    ("a text editor with a toolbar and a status bar", "b_toolbar_editor"),
    ("an image viewer that steps through a folder of frames",
     "a_image_viewer"),
])
def test_examples_are_chosen_for_the_request(text, first):
    for mode in gd.MODES:
        assert gd.select_examples(text, mode=mode)[0] == first


def test_a_request_matching_nothing_gets_the_defaults():
    assert gd.select_examples("qwertyuiop", mode="pixel") == [gd.EXAMPLE_NAME]
    assert gd.select_examples("qwertyuiop", mode="tree") == list(
        gd.DEFAULT_EXAMPLES["tree"])


@pytest.mark.parametrize("canvas", CANVASES, ids=lambda c: f"{c[0]}x{c[1]}")
@pytest.mark.parametrize("mode", gd.MODES)
def test_every_example_a_model_is_shown_passes_the_gate_there(mode, canvas):
    """An example the validator would refuse teaches refusal. Every name
    in the pool that is offered at this canvas passes check_reply there."""
    offered = 0
    for name, _words in gd.EXAMPLE_POOL:
        text = gd._checked_example(name, mode, *canvas)
        if text is None:
            continue
        offered += 1
        if mode == "pixel":
            assert gd.check_reply(text, canvas_w=canvas[0],
                                  canvas_h=canvas[1]).ok, name
        else:
            assert gd.check_reply(text.replace("\n", " "),
                                  canvas_w=canvas[0],
                                  canvas_h=canvas[1]).ok, name
    assert offered >= 10, (mode, canvas, offered)


def test_the_default_example_passes_at_typhons_canvas():
    """MEASURED before the fix: stretched to 1504 x 1016 it failed the gate
    with 'label "Frames on a bad timing" and entry overlap'."""
    ex = gd.example_wireframe(1504, 1016)
    checked = gd.check_reply(json.dumps(ex), canvas_w=1504, canvas_h=1016)
    assert checked.ok, checked.faults
    assert gd.select_examples("x", canvas_w=1504, canvas_h=1016) == [
        gd.EXAMPLE_NAME]


def test_a_bigger_window_buys_more_examples():
    text = "a tabbed settings window with a toolbar and a status bar"
    small = gd.build_prompt(text, mode="tree",
                            budget_chars=gd.profile_for(3.8).budget_chars)
    big = gd.build_prompt(text, mode="tree",
                          budget_chars=gd.profile_for(3.8, n_ctx=8192)
                          .budget_chars)
    assert small.count('"kind": "page"') <= big.count('"kind": "page"')
    assert big.count("\n\n{\"window\"") > small.count("\n\n{\"window\"") \
        or "EXAMPLES" in big
    assert len(small) <= gd.profile_for(3.8).budget_chars


def test_the_tree_prompt_never_asks_for_pixels():
    p = gd.build_prompt("a login form", mode="tree")
    assert "never write x, y, w or h" in p
    assert "multiple of 8" not in p and '"x":' not in p
    for k in gt.LEAF_KINDS:
        assert f"\n- {k}" in p, k
    assert p.rstrip().endswith('"bg", "props" and "grow" are optional.')


@pytest.mark.parametrize("params, n_ctx", [(3.8, 4096), (8.0, 4096),
                                            (8.0, 8192), (14.7, 4096)])
def test_prompt_and_reply_fit_the_window(params, n_ctx):
    """Prompt (estimated) + num_predict + template slack <= the window, for
    a request long enough to be realistic."""
    prof = gd.profile_for(params, n_ctx=n_ctx)
    text = ("A capture tool: a folder picker, an image panel with a frame "
            "slider under it, three numeric settings with labels, Start and "
            "Stop buttons, a log at the bottom and a status bar.")
    prompt = gd.build_prompt(text, mode=prof.mode,
                             budget_chars=prof.budget_chars)
    assert gd.estimate_tokens(prompt) + prof.reply_tokens \
        + gd.SLACK_TOKENS <= n_ctx
