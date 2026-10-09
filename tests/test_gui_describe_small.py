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
                # And vertically (REVIEW: only x was checked, and a squeezed
                # labelframe's child sat below it).
                assert c.y - p.y >= gt.INSET and p.y + p.h - (c.y + c.h) >= 0


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
        assert_parents_kept(tree, checked.shapes, canvas)
    assert accepted >= 120, accepted


def assert_parents_kept(tree, shapes, canvas):
    """Every widget is inside the container the TREE put it in — not just
    inside something: a child placed outside its container is re-parented
    by gui_layout, and what Generate builds is then not the design the
    model wrote. REVIEW: pins the layouter's promise for every accepted
    random tree (a squeezed container used to be able to break it)."""
    parsed = gt.parse(json.loads(tree_reply(tree)))
    lay = gt.layout(parsed.root, *canvas, window=parsed.window)
    paths = lay.paths
    assert len(paths) == len(shapes)
    kids = gl.build_containment_tree(shapes, warnings=[])
    actual = {i: p for p, ids in kids.items() for i in ids}
    index = {s.id: n for n, s in enumerate(shapes)}
    for n, s in enumerate(shapes):
        # The intended parent: the last row emitted before this one whose
        # path is this one's ancestor (a wrapped page shares its child's).
        want = None
        for j in range(n - 1, -1, -1):
            if paths[n] == paths[j] or paths[n].startswith(paths[j] + " > "):
                want = j
                break
        got = actual.get(s.id)
        assert (index[got] if got else None) == want, (paths[n], canvas)


def test_a_layout_far_too_big_is_a_fault_that_says_what_to_do():
    wide = row(*[leaf("button", f"Button number {i}") for i in range(24)])
    checked = gd.check_reply(tree_reply(col(wide)))
    assert not checked.ok and checked.stage == gd.STAGE_GATE
    assert any("notebook" in f and "px wide" in f for f in checked.faults)


def test_a_container_squeezed_past_its_insets_is_a_layout_fault():
    """REVIEW (fuzzed): the root may be within MAX_SQUEEZE while a notebook
    nested in a notebook's page — every page drawn side by side, so widths
    divide twice — is squeezed below its own insets. The layouter then put
    a widget OUTSIDE its page, and the gate said 'props.tabs has 2 title(s)
    but 3 page(s)' about a notebook the model wrote with two pages: a fault
    a small model cannot act on, spending its repair rounds. Refused by the
    layouter instead, naming the container and what to do."""
    inner = {"kind": "notebook", "label": "Inner", "children": [
        leaf("listbox", "lb", grow=False), leaf("spinbox", "sp", grow=False)]}
    pane = {"kind": "panedwindow", "label": "pw",
            "props": {"orient": "vertical"}, "children": [
                {"kind": "freeform", "label": "x", "children": [
                    leaf("scrubber", "Save as"), leaf("image_canvas", "img")]}]}
    group = {"kind": "labelframe", "label": "LF", "grow": False, "children": [
        leaf("button", "B" * 60), leaf("file_picker", "fp"),
        leaf("log_pane", "x"), leaf("label", "lab")]}
    form = {"kind": "frame", "label": "OK", "grow": False, "children": [
        leaf("combobox", "x", grow=False), leaf("text", "OK"),
        leaf("combobox", "Name")]}
    tree = col({"kind": "notebook", "label": "Outer", "grow": True,
                "children": [
                    {"kind": "page", "label": "P1", "children": [pane]},
                    {"kind": "page", "label": "P2", "children": [
                        col(group, form)]},
                    {"kind": "page", "label": "P3", "children": [inner]}]},
               leaf("button", "Go"))
    checked = gd.check_reply(tree_reply(tree), canvas_w=800, canvas_h=1200)
    if checked.ok:
        assert_clean_geometry(checked.shapes, (800, 1200))
        return
    assert not any("props.tabs" in f for f in checked.faults), checked.faults
    assert any("too small" in f and "Inner" in f for f in checked.faults), \
        checked.faults


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


#: Degenerate repetition — a small model stuck emitting "[" until the token
#: limit — and a list nested deeper than the interpreter recurses.
NESTING_BOMBS = [
    '{"shapes": ' + "[" * 3000 + "]" * 2999,
    tree_reply(col(leaf("combobox", "a", props={"values": "BOMB"}),
                   leaf("button", "b"))).replace('"BOMB"',
                                                 "[" * 900 + "]" * 900),
    json.dumps({"window": {"title": "x"}, "shapes": [
        {"kind": "combobox", "label": "a", "x": 16, "y": 16, "w": 160,
         "h": 32, "props": {"values": "BOMB"}}]}).replace(
        '"BOMB"', "[" * 900 + "]" * 900),
]


@pytest.mark.parametrize("bomb", NESTING_BOMBS,
                         ids=["cut-off", "tree-prop", "pixel-prop"])
def test_a_reply_nested_past_the_recursion_limit_costs_one_call(bomb):
    """REVIEW: the salvage's json.loads caught only ValueError, and the prop
    checks recurse — a RecursionError ended the WHOLE describe ("failed
    unexpectedly") on the first such reply, with every candidate and repair
    round still unspent. It is one bad reply, judged like any other."""
    checked = gd.check_reply(bomb)
    assert not checked.ok and checked.faults
    model = Model(bomb, tree_reply(FORM_TREE))
    res = gd.describe("a login form", model_call=model, profile=SMALL)
    assert res.ok, res.errors
    assert len(model.calls) == 2


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


# ============================================================
# The widgets the description names
# ============================================================

TIMER_TEXT = ("A countdown timer: a spin box for minutes, a progress bar, "
              "and Start and Reset buttons.")
TIMER_NO_BAR = col(row(leaf("label", "Minutes"), leaf("spinbox", "Minutes")),
                   row(leaf("button", "Start"), leaf("button", "Reset")))
TIMER_FULL = col(row(leaf("label", "Minutes"), leaf("spinbox", "Minutes")),
                 leaf("progressbar", "Time left"),
                 row(leaf("button", "Start"), leaf("button", "Reset")))


def wanted_kinds(text):
    return [w.kinds for w in gd.requested_widgets(text)]


@pytest.mark.parametrize("text, kinds", [
    ("a spin box and a progress bar", [("spinbox",), ("progressbar",)]),
    # the specific phrase is used up, so it does not also demand a button
    ("three radio buttons", [("radiobutton",)]),
    ("a 'Debug' Check Button", [("checkbutton",)]),
    # a multi-line box is not also a single-line entry
    ("a multi-line Message box", [("text", "log_pane")]),
    ("a status bar but no menu bar", [("status_bar",)]),
    ("a form without a status bar", []),
    ("a login form", []),
    # a log view is a log_pane, never a text box (see _REQUEST_WORDS)
    ("a large log view below", [("log_pane",)]),
    ("a box to type a number", [("entry", "text", "combobox", "spinbox")]),
])
def test_requested_widgets_reads_only_what_is_named(text, kinds):
    assert wanted_kinds(text) == kinds


BENCH_CASES = {c["id"]: c for c in json.loads(
    (ROOT / "tests" / "data" / "llm_bench" / "gui_cases.json").read_text(
        encoding="utf-8"))["cases"]}
#: Model replies the benchmark recorded on 2026-10-05, verbatim.
RECORDED = json.loads((ROOT / "tests" / "data" / "llm_bench" /
                       "recorded_2026-10-05.json").read_text(encoding="utf-8"))


def recorded_gaps(key):
    """Describe's verdict on one recorded reply to its benchmark case."""
    case = BENCH_CASES[key.split()[1]]
    checked = gd.check_reply(RECORDED["gui"][key], canvas_w=gd.CANVAS_W,
                             canvas_h=gd.CANVAS_H)
    assert checked.ok, (key, checked.faults)
    return gd.missing_widgets(checked.shapes,
                              gd.requested_widgets(case["text"]))


#: Counts a description gives that its grader does not check, each read by
#: hand: C1 names "a Gain slider" AND "a frame slider", and every C1 design
#: the grader passed on 2026-10-05 drew both.
UNGRADED_COUNTS = {("C1", "a slider"): 2}


def test_the_benchmark_descriptions_never_demand_a_widget_they_do_not_want():
    """Precision over recall: a requirement read wrongly ranks a right
    design below a wrong one and spends a repair round. Every widget read
    from a benchmark description must be one its expectations allow, and
    no more of a kind than the grader counts."""
    for case in BENCH_CASES.values():
        allowed = set(case["expect"].get("kinds_all", []))
        for group in case["expect"].get("kinds_any", []):
            allowed |= set(group)
        counts = case["expect"].get("count_at_least", {})
        for w in gd.requested_widgets(case["text"]):
            assert allowed.intersection(w.kinds), (case["id"], w.what)
            if w.count > 1 and not w.name:
                assert any(counts.get(k, 0) >= w.count for k in w.kinds) \
                    or UNGRADED_COUNTS.get((case["id"], w.what)) == w.count, \
                    (case["id"], w.what, w.count)


@pytest.mark.parametrize("cid, captions", [
    ("S1", ["Send", "Clear"]),
    ("S2", ["Convert"]),
    ("S3", ["Start", "Pause", "Reset"]),
    ("S4", ["Add", "Remove", "Clear all"]),
    ("M1", ["Open"]),
    ("M2", ["OK", "Cancel"]),
    ("M3", ["Add", "Delete"]),
    ("M4", ["Plot"]),
    ("C1", ["Scan for cameras", "Connect", "Start capture", "Stop capture"]),
    ("C2", ["Export", "Clear log"]),
    # the toolbar's "Open, Save, Previous and Next" are not called buttons
    ("C3", ["Add box", "Delete annotation"]),
    ("C4", ["Apply", "Clear"]),
])
def test_the_buttons_a_description_names_are_read_with_their_captions(
        cid, captions):
    named = [w.name for w in gd.requested_widgets(BENCH_CASES[cid]["text"])
             if w.name]
    assert named == captions


@pytest.mark.parametrize("text, count", [
    ("two dropdowns to choose the X column and the Y column", 2),
    ("a dropdown for the unit to convert from and a dropdown for the unit "
     "to convert to", 2),
    ("a theme dropdown (Light, Dark)", 1),
    ("a 'Start at login' checkbox ... a 'Debug logging' checkbox", 2),
    # what the description is OF names the app, not one more widget
    ("An image viewer: a large image area with a frame slider under it", 1),
    ("A table editor with a table of parts and an Add button", 1),
    ("A window with two dropdowns", 2),
    ("A dropdown with three choices", 1),
])
def test_how_many_the_text_asks_for_is_read(text, count):
    first = gd.requested_widgets(text)[0]
    assert first.count == count, first


@pytest.mark.parametrize("text", ["Two buttons side by side",
                                  "no OK button", "three radio buttons"])
def test_articles_numbers_and_negations_are_never_captions(text):
    assert not [w for w in gd.requested_widgets(text) if w.name]


def test_a_button_with_another_caption_does_not_answer_a_named_one():
    """Kinds alone were not enough: any two buttons answered "Start and
    Reset buttons"."""
    other = col(row(leaf("label", "Minutes"), leaf("spinbox", "Minutes")),
                leaf("progressbar", "Time left"),
                row(leaf("button", "Go"), leaf("button", "Stop")))
    checked = gd.check_reply(tree_reply(other))
    gaps = gd.missing_widgets(checked.shapes, gd.requested_widgets(TIMER_TEXT))
    assert gaps == ["a button labelled 'Start' (button or toolbar)",
                    "a button labelled 'Reset' (button or toolbar)"]


def test_each_named_button_needs_its_own_button():
    """"Clear" answers "Clear all" — but one button is not two."""
    text = "Apply and Clear buttons, and a Clear log button"
    one = gd.check_reply(tree_reply(col(row(leaf("button", "Apply"),
                                            leaf("button", "Clear")))))
    assert gd.missing_widgets(one.shapes, gd.requested_widgets(text)) == [
        "a button labelled 'Clear log' (button or toolbar)"]
    two = gd.check_reply(tree_reply(col(row(
        leaf("button", "Clear log"), leaf("button", "Apply"),
        leaf("button", "Clear")))))
    assert gd.missing_widgets(two.shapes, gd.requested_widgets(text)) == []


def test_toolbar_buttons_and_a_pickers_browse_button_answer_by_caption():
    text = "a toolbar with Open and Save buttons, and a Browse button"
    shapes = gd.check_reply(tree_reply(col(
        leaf("toolbar", "Main", props={"buttons": ["Open", "Save"]}),
        leaf("file_picker", "Folder"), leaf("text", "Notes")))).shapes
    assert gd.missing_widgets(shapes, gd.requested_widgets(text)) == []


def test_best_of_n_prefers_the_candidate_with_the_named_buttons():
    wrong = col(row(leaf("label", "Minutes"), leaf("spinbox", "Minutes")),
                leaf("progressbar", "Time left"),
                row(leaf("button", "Go"), leaf("button", "Halt")))
    model = Model(tree_reply(wrong), tree_reply(TIMER_FULL))
    res = gd.describe(TIMER_TEXT, model_call=model, profile=SMALL)
    assert res.ok, res.errors
    assert {s.label for s in res.shapes if s.kind == "button"} == {
        "Start", "Reset"}
    assert any("candidate 2 of 3 passed" in n for n in res.notes)


def test_the_benchmark_repair_that_dropped_named_buttons_is_seen():
    """qwen2.5's C1 (2026-10-05): candidate 2 had "Scan for cameras" and
    "Connect" but no list or status bar; the repair added those and DROPPED
    both buttons — and was accepted as complete, then failed the grade
    ("2 button, wanted at least 4; no label mentioning scan / connect")."""
    assert recorded_gaps("qwen2.5 C1 3") == [
        "a button labelled 'Scan for cameras' (button or toolbar)",
        "a button labelled 'Connect' (button or toolbar)"]
    assert recorded_gaps("qwen2.5 C1 2") == [
        "a status bar (status_bar)",
        "a list (listbox or treeview or combobox)"]


def test_the_benchmark_count_gap_is_seen():
    """phi3.5's S2 reply 3 (label + ONE dropdown + Convert) was accepted
    as complete: "a dropdown for ... and a dropdown for ..." was not read
    as two. (Its missing entry stays unseen: each requested widget is
    answered on its own, and a dropdown is a box one can type in — reading
    the kinds jointly would turn "a dropdown list of cameras" into two.)"""
    assert recorded_gaps("phi3.5 S2 3") == ["a second dropdown (combobox)"]


@pytest.mark.parametrize("key", ["llama3.1:8b C1 1", "phi4:14b C1 1",
                                 "qwen2.5-coder C1 1", "qwen2.5-coder C4 1",
                                 "phi4:14b C4 1", "llama3.1:8b S2 1"])
def test_recorded_designs_the_grader_passed_have_no_gaps(key):
    """The precision side, on real replies: every one of these passed the
    benchmark's grade, and none may now be sent back for a repair."""
    assert recorded_gaps(key) == []


def test_a_caption_never_stands_in_for_the_widget():
    """llama3.1:8b's C3, four rounds running: a labelframe titled "Frame
    slider" holding a spin box. A check that read captions as widgets
    would have passed it; the gap now also says what the caption is, and
    what it holds."""
    assert recorded_gaps("llama3.1:8b C3 2") == [
        "a slider (scale or scrubber) — 'Frame slider' is a labelframe "
        "holding a spinbox, not a slider"]


def test_the_repair_round_is_told_what_the_captioned_box_holds():
    """The C3 shape, small: the repair prompt names the spin box the
    'Frame slider' group holds, not only the missing slider."""
    text = "An image viewer: a large image area with a frame slider under it."
    boxed = col(leaf("image_canvas", "Image"),
                leaf("labelframe", "Frame slider",
                     children=[leaf("spinbox", "")]))
    fixed = col(leaf("image_canvas", "Image"), leaf("scale", "Frame"))
    model = Model(*[tree_reply(boxed)] * SMALL.n_best, tree_reply(fixed))
    res = gd.describe(text, model_call=model, profile=SMALL)
    assert res.ok, res.errors
    assert ("missing a slider (scale or scrubber) — 'Frame slider' is a "
            "labelframe holding a spinbox, not a slider") in \
        model.calls[-1]["prompt"]
    assert "scale" in {s.kind for s in res.shapes}


@pytest.mark.parametrize("key", ["qwen2.5 C4 1", "phi3.5 C4 4"])
def test_a_text_box_is_not_the_log_view_the_benchmark_asks_for(key):
    """C4 (2026-10-05): both drew a text box for "a large log view", which
    Describe accepted and the grader failed ("no log_pane")."""
    assert recorded_gaps(key) == ["a log view (log_pane)"]


def test_a_repair_round_is_told_the_log_view_is_a_log_pane():
    text = "A log monitor: a log file picker at the top and a large log view."
    as_text = col(leaf("file_picker", "Log file"), leaf("text", "Log"))
    as_log = col(leaf("file_picker", "Log file"), leaf("log_pane", "Log"))
    model = Model(*[tree_reply(as_text)] * SMALL.n_best, tree_reply(as_log))
    res = gd.describe(text, model_call=model, profile=SMALL)
    assert res.ok, res.errors
    assert "missing a log view (log_pane)" in model.calls[-1]["prompt"]
    assert "log_pane" in {s.kind for s in res.shapes}


def test_best_of_n_prefers_the_candidate_with_the_named_widgets():
    """phi3.5's failures were valid layouts missing a widget the request
    named; best-of-N took the first VALID candidate. The complete one wins."""
    model = Model(tree_reply(TIMER_NO_BAR), tree_reply(TIMER_FULL))
    res = gd.describe(TIMER_TEXT, model_call=model, profile=SMALL)
    assert res.ok, res.errors
    assert len(model.calls) == 2
    assert "progressbar" in {s.kind for s in res.shapes}
    assert any("candidate 2 of 3 passed" in n for n in res.notes)


def test_a_repair_round_is_told_which_named_widget_to_add():
    model = Model(tree_reply(TIMER_NO_BAR), tree_reply(TIMER_NO_BAR),
                  tree_reply(TIMER_NO_BAR), tree_reply(TIMER_FULL))
    res = gd.describe(TIMER_TEXT, model_call=model, profile=SMALL)
    assert res.ok, res.errors
    assert len(model.calls) == SMALL.n_best + 1
    repair = model.calls[-1]["prompt"]
    assert "missing a progress bar (progressbar)" in repair
    assert "progressbar" in {s.kind for s in res.shapes}
    assert not any("leaves out" in n for n in res.notes)


def test_a_valid_design_is_never_refused_for_a_missing_widget():
    """No complete design arrives: the valid one is returned, and the note
    says what it leaves out — never a failure the old code would have
    accepted."""
    model = Model(tree_reply(TIMER_NO_BAR))
    res = gd.describe(TIMER_TEXT, model_call=model, profile=SMALL,
                      max_attempts=2)
    assert res.ok and res.shapes
    assert len(model.calls) == SMALL.n_best + 1
    assert any("leaves out a progress bar" in n for n in res.notes)


def test_the_design_with_fewer_gaps_is_the_one_returned():
    neither = col(row(leaf("label", "Minutes"), leaf("entry", "Minutes")),
                  row(leaf("button", "Start"), leaf("button", "Reset")))
    model = Model(tree_reply(neither), tree_reply(TIMER_NO_BAR))
    res = gd.describe(TIMER_TEXT, model_call=model,
                      profile=gd.profile_for(3.8, n_ctx=4096, n_best=2),
                      max_attempts=1)
    assert res.ok
    assert "spinbox" in {s.kind for s in res.shapes}


def test_the_plain_path_still_takes_the_first_valid_design():
    """No profile (stubs, the Tk shell): one call, as before."""
    model = Model(tree_reply(TIMER_NO_BAR), tree_reply(TIMER_FULL))
    res = gd.describe(TIMER_TEXT, model_call=model)
    assert res.ok and len(model.calls) == 1


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
