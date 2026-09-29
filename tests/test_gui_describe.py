"""
gui_describe — "Describe it": plain English -> a validated wireframe.

NO GGUF IS LOADED ANYWHERE IN THIS FILE. model_call is an injected callable, so
every path (valid reply, prose around a fence, truncation, a hallucinated kind,
a mistyped prop, stacked full-canvas containers, a model that raises, running
out of attempts) is scripted and deterministic.

THE LOAD-BEARING PROPERTY is that nothing describe() accepts can then be
refused by Generate: every accepted wireframe is re-run here, independently,
through gui_layout.infer -> gui_spec.build -> gui_spec.validate.

Run:  python -m pytest tests/test_gui_describe.py -q
"""
from __future__ import annotations

import ast
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gui_describe as gd            # noqa: E402
import gui_layout as gl              # noqa: E402
import gui_spec as gsp               # noqa: E402
from gui_shapes import CONTAINER_KINDS, GENERIC_KIND, PALETTE  # noqa: E402


class Stub:
    """A scripted model. Records prompts so the repair loop is observable.
    The last reply repeats once the script runs out."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if not self.replies:
            return ""
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def reply(payload) -> str:
    return json.dumps(payload)


def wire(*shapes, title="Scan", **window):
    return {"window": {"title": title, **window}, "shapes": list(shapes)}


# A plain form: every shape at root, nothing overlapping, all on the grid.
FORM = wire(
    {"kind": "label", "label": "Input folder", "x": 24, "y": 24, "w": 240, "h": 24},
    {"kind": "file_picker", "label": "", "x": 24, "y": 56, "w": 400, "h": 32,
     "props": {"mode": "folder"}},
    {"kind": "button", "label": "Run", "x": 24, "y": 104, "w": 120, "h": 32},
    {"kind": "image_canvas", "label": "", "x": 456, "y": 24, "w": 616, "h": 520},
    {"kind": "log_pane", "label": "", "x": 24, "y": 560, "w": 1048, "h": 112},
    bg="#1e1e2e",
)

# Nesting: a notebook whose two pages are frames drawn side by side.
NOTEBOOK = wire(
    {"kind": "notebook", "label": "", "x": 16, "y": 16, "w": 1064, "h": 600,
     "props": {"tabs": ["General", "Advanced"]}},
    {"kind": "frame", "label": "General", "x": 32, "y": 48, "w": 512, "h": 552},
    {"kind": "frame", "label": "Advanced", "x": 560, "y": 48, "w": 504, "h": 552},
    {"kind": "checkbutton", "label": "Start on login", "x": 48, "y": 64,
     "w": 200, "h": 24},
    {"kind": "entry", "label": "", "x": 48, "y": 104, "w": 240, "h": 32},
    {"kind": "spinbox", "label": "", "x": 576, "y": 64, "w": 120, "h": 32},
    {"kind": "combobox", "label": "", "x": 576, "y": 112, "w": 200, "h": 32,
     "props": {"values": ["Low", "High"]}},
    {"kind": "button", "label": "Save", "x": 960, "y": 640, "w": 112, "h": 40},
    title="Settings",
)

# The exact shape gui_examples was written against: a full-canvas Frame AND a
# full-canvas Notebook, which came up as a completely blank app.
STACKED = wire(
    {"kind": "frame", "label": "", "x": 0, "y": 0, "w": 1100, "h": 700},
    {"kind": "notebook", "label": "", "x": 0, "y": 0, "w": 1100, "h": 700,
     "props": {"tabs": ["Main"]}},
    {"kind": "button", "label": "Go", "x": 40, "y": 40, "w": 120, "h": 32},
    title="Blank",
)


def with_shape(base, i, **changes):
    """A deep copy of ``base`` with shape ``i`` changed."""
    out = copy.deepcopy(base)
    out["shapes"][i].update(changes)
    return out


def gate(result):
    """Re-run Generate's own pipeline on an accepted result, independently."""
    shapes, win = result.shapes, result.window
    tree = gl.infer(shapes, gd.CANVAS_W, gd.CANVAS_H)
    spec = gsp.build(shapes, tree, project="t", title=win.get("title", ""),
                     root_bg=win.get("bg", ""), root_fg=win.get("fg", ""),
                     root_font=win.get("font", ""))
    ok, errs = gsp.validate(spec)
    return ok, errs, tree


# ============================================================
# Purity
# ============================================================

ALLOWED_LOCAL = {"gui_shapes", "gui_snap", "gui_layout", "gui_spec",
                 "gui_colors", "gui_ports", "gui_examples", "nx_generate"}


def test_module_imports_only_the_stdlib_and_the_pure_designer_modules():
    """The moment this module reaches for a toolkit or a model loader it
    cannot be driven by a scripted stub, which is the whole test strategy."""
    src = (ROOT / "gui_describe.py").read_text(encoding="utf-8")
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    stray = {m for m in mods
             if m not in sys.stdlib_module_names and m not in ALLOWED_LOCAL
             and m != "__future__"}
    assert not stray, f"gui_describe imports non-pure modules: {stray}"
    for banned in ("council_engine", "role_models", "PySide6", "tkinter"):
        assert banned not in mods


def test_importing_it_loads_no_toolkit_and_no_model():
    """The AST check sees only this file; the pure modules it imports could
    still pull a toolkit in. A fresh interpreter says what actually loads."""
    code = ("import sys; import gui_describe; "
            "print(sorted(m for m in ('PySide6', 'tkinter', 'council_engine', "
            "'role_models', 'llama_cpp') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]", out.stdout


# ============================================================
# The happy path
# ============================================================

def test_a_valid_reply_is_accepted_in_one_call_and_passes_validate():
    stub = Stub(reply(FORM))
    res = gd.describe("a folder scanner with a preview", model_call=stub)
    assert res.ok, res.errors
    assert len(stub.prompts) == 1 and res.attempts == 1
    assert [s.kind for s in res.shapes] == [s["kind"] for s in FORM["shapes"]]
    assert res.window["title"] == "Scan" and res.window["bg"] == "#1e1e2e"
    assert res.errors == [] and res.raw == reply(FORM)
    ok, errs, _ = gate(res)
    assert ok, errs


def test_prose_and_a_code_fence_around_the_json_are_tolerated():
    text = ("Sure! Here is the design you asked for:\n```json\n"
            + json.dumps(FORM, indent=2) + "\n```\nHope that helps.")
    stub = Stub(text)
    res = gd.describe("a scanner", model_call=stub)
    assert res.ok, res.errors
    assert len(stub.prompts) == 1


def test_accepted_shapes_get_fresh_ids_and_catalogue_default_props():
    res = gd.describe("x", model_call=Stub(reply(FORM)))
    ids = [s.id for s in res.shapes]
    assert len(set(ids)) == len(ids) and all(len(i) == 32 for i in ids)
    picker = next(s for s in res.shapes if s.kind == "file_picker")
    # The reply's own prop, plus every schema default it did not mention.
    assert picker.props["mode"] == "folder"
    assert set(picker.props) == set(PALETTE["file_picker"]["prop_schema"])


def test_nested_containers_come_back_with_containers_under_their_children():
    """The designer paints in (z, id) order: a container with a higher z than
    its children is drawn OVER them. And a notebook's tabs follow its pages'
    (z, id), so pages drawn left to right must get increasing z."""
    res = gd.describe("settings with two tabs", model_call=Stub(reply(NOTEBOOK)))
    assert res.ok, res.errors
    ok, errs, tree = gate(res)
    assert ok, errs
    by_id = {s.id: s for s in res.shapes}
    for s in res.shapes:
        parent = tree.nodes[s.id].parent_id
        if parent:
            assert by_id[parent].z < s.z, (by_id[parent].kind, s.kind)
    nb = next(s for s in res.shapes if s.kind == "notebook")
    pages = [by_id[c] for c in tree.nodes[nb.id].children]
    assert [p.label for p in pages] == ["General", "Advanced"]


def test_the_input_text_reaches_the_model_verbatim():
    text = "  A {weird} request — with braces, “quotes” and a trailing space "
    stub = Stub(reply(FORM))
    gd.describe(text, model_call=stub)
    assert text in stub.prompts[0]


# ============================================================
# Schema faults -> one repair round, with every fault named
# ============================================================

def test_an_unknown_kind_is_sent_back_by_name():
    bad = with_shape(FORM, 2, kind="fancy_slider")
    stub = Stub(reply(bad), reply(FORM))
    res = gd.describe("x", model_call=stub)
    assert res.ok, res.errors
    assert len(stub.prompts) == 2
    repair = stub.prompts[1]
    assert "Your previous answer was rejected." in repair
    assert "WHAT IS WRONG" in repair
    assert "fancy_slider" in repair.split("WHAT IS WRONG", 1)[1]
    assert 'shape 3 (fancy_slider "Run")' in repair


def test_generic_is_neither_offered_nor_accepted():
    """"generic" is the question gui_classify asks, never an answer."""
    prompt = gd.build_prompt("x")
    assert f"- {GENERIC_KIND}" not in prompt
    assert GENERIC_KIND not in gd.KINDS
    stub = Stub(reply(with_shape(FORM, 2, kind="generic")), reply(FORM))
    res = gd.describe("x", model_call=stub)
    assert res.ok and len(stub.prompts) == 2
    assert '"generic" is a placeholder' in stub.prompts[1]


def test_an_unknown_prop_key_is_rejected_with_the_allowed_keys():
    bad = with_shape(FORM, 2, props={"colour_scheme": "dark"})
    stub = Stub(reply(bad), reply(FORM))
    res = gd.describe("x", model_call=stub)
    assert res.ok and len(stub.prompts) == 2
    wrong = stub.prompts[1].split("WHAT IS WRONG", 1)[1]
    assert 'button has no property "colour_scheme"' in wrong
    assert "allowed: state, text" in wrong


def test_a_list_prop_given_as_a_string_is_a_type_fault_not_a_list_of_chars():
    """Neither existing validator checks types. gui_emit_qt runs
    list(_prop(w, "values", [])), so "Low,Mid,High" became a dropdown of the
    characters L, o, w, ','... — and every other check passed it."""
    bad = with_shape(NOTEBOOK, 6, props={"values": "Low,Mid,High"})
    stub = Stub(reply(bad), reply(NOTEBOOK))
    res = gd.describe("x", model_call=stub)
    assert res.ok and len(stub.prompts) == 2
    wrong = stub.prompts[1].split("WHAT IS WRONG", 1)[1]
    assert "props.values must be a JSON list of strings" in wrong
    assert '["Low", "Mid", "High"]' in wrong


@pytest.mark.parametrize("kind,props,needle", [
    ("spinbox", {"to": "100"}, "props.to must be a whole number"),
    ("spinbox", {"to": True}, "props.to must be a whole number"),
    ("text", {"readonly": "yes"}, "props.readonly must be true or false"),
    ("image_canvas", {"overlay_alpha": "half"}, "props.overlay_alpha must be a number"),
    ("label", {"text": 5}, "props.text must be text"),
    ("listbox", {"selectmode": "all"}, "must be one of browse, single"),
    ("menubar", {"menus": ["File", "Edit"]}, "props.menus must be a list of menus"),
])
def test_every_prop_type_is_checked(kind, props, needle):
    row = {"kind": kind, "label": "", "x": 24, "y": 24, "w": 200, "h": 40,
           "props": props}
    checked = gd.check_reply(reply(wire(row)))
    assert not checked.ok
    assert any(needle in f for f in checked.faults), checked.faults


def test_lossless_prop_coercions_are_accepted_silently():
    row = {"kind": "image_canvas", "label": "", "x": 24, "y": 24, "w": 400,
           "h": 300, "props": {"overlay_alpha": 1}}
    tree_row = {"kind": "treeview", "label": "", "x": 448, "y": 24, "w": 400,
                "h": 300, "props": {"columns": ["Layer", 2]}}
    checked = gd.check_reply(reply(wire(row, tree_row)))
    assert checked.ok, checked.faults
    assert checked.shapes[0].props["overlay_alpha"] == 1.0
    assert checked.shapes[1].props["columns"] == ["Layer", "2"]


def test_every_prop_type_in_the_catalogue_is_one_the_type_check_knows():
    """A new prop_schema type would otherwise pass unchecked."""
    used = {d.get("type") for k in PALETTE
            for d in (PALETTE[k].get("prop_schema") or {}).values()}
    assert used <= gd.KNOWN_PROP_TYPES, used - gd.KNOWN_PROP_TYPES


def test_a_colour_name_is_rejected_with_the_hex_to_use():
    """gui_colors' closed grammar: its consumers swallow the ValueError, so
    "pink" became an uncoloured app with no message anywhere."""
    stub = Stub(reply(with_shape(FORM, 0, bg="pink")), reply(FORM))
    res = gd.describe("x", model_call=stub)
    assert res.ok and len(stub.prompts) == 2
    assert '"#ffc0cb" for pink' in stub.prompts[1]


def test_a_window_colour_name_is_rejected_too():
    checked = gd.check_reply(reply(wire(FORM["shapes"][0], bg="hot pink")))
    assert any(f.startswith("window: bg") for f in checked.faults)


def test_colour_on_a_kind_that_cannot_take_it_is_dropped_with_a_note():
    """A notebook has no classic Tk equivalent and gui_colors.COLOUR_CAPS
    gates both emitters, so the colour could never paint. Dropping it costs
    nothing; spending a repair round on it would."""
    bad = with_shape(NOTEBOOK, 0, bg="#ff0000")
    res = gd.describe("x", model_call=Stub(reply(bad)))
    assert res.ok, res.errors
    nb = next(s for s in res.shapes if s.kind == "notebook")
    assert nb.bg == ""
    assert any("bg #ff0000 dropped" in n for n in res.notes)


def test_refused_fields_are_ignored_with_a_note_and_never_applied():
    """ids are the app's; script/drives/port/requires are kept out of props
    in gui_shapes so that a model-authored import target or binding is
    structurally impossible. They must not come back in through here."""
    bad = with_shape(FORM, 2, id="abc123",
                     script={"module": "os", "function": "system"},
                     port={"name": "evil"}, drives={"folder": "x"})
    bad["requires"] = ["numpy"]
    res = gd.describe("x", model_call=Stub(reply(bad)))
    assert res.ok, res.errors
    btn = next(s for s in res.shapes if s.kind == "button")
    assert btn.id != "abc123"
    assert btn.script == {} and btn.port == {} and btn.drives == {}
    joined = "\n".join(res.notes)
    for key in ("id", "script", "port", "drives", "requires"):
        assert f'ignored "{key}"' in joined, key


def test_a_command_prop_is_ignored_because_generation_wires_callbacks():
    bad = with_shape(FORM, 2, props={"command": "os.system('x')"})
    res = gd.describe("x", model_call=Stub(reply(bad)))
    assert res.ok, res.errors
    btn = next(s for s in res.shapes if s.kind == "button")
    assert btn.props["command"] == ""
    assert any("ignored props.command" in n for n in res.notes)


def test_every_schema_fault_is_reported_in_one_round():
    """A repair round is a full generation. Reporting one fault per round is
    how three attempts run out on a wireframe with four problems."""
    bad = copy.deepcopy(FORM)
    bad["shapes"][0]["kind"] = "slider"
    bad["shapes"][1]["props"] = {"mode": "directory"}
    bad["shapes"][2]["w"] = "wide"
    bad["shapes"][3]["colour"] = "#000000"
    checked = gd.check_reply(reply(bad))
    assert checked.stage == gd.STAGE_SCHEMA
    text = "\n".join(checked.faults)
    for needle in ("shape 1", "shape 2", "shape 3", "shape 4"):
        assert needle in text, (needle, checked.faults)


def test_a_truncated_reply_is_diagnosed_and_its_text_is_shown_back():
    cut = reply(FORM)[: len(reply(FORM)) // 2]
    stub = Stub(cut, reply(FORM))
    res = gd.describe("x", model_call=stub)
    assert res.ok and len(stub.prompts) == 2
    repair = stub.prompts[1]
    assert "the reply contained no JSON object" in repair
    assert "cut off" in repair
    assert cut[:200] in repair.split("WHAT IS WRONG", 1)[0]


def test_a_single_shape_instead_of_a_wireframe_gets_a_pointed_hint():
    checked = gd.check_reply(reply(FORM["shapes"][2]))
    assert any("looks like ONE shape" in f for f in checked.faults)


# ============================================================
# Deterministic repair — no model round spent
# ============================================================

def test_off_canvas_and_overlapping_shapes_are_fixed_by_snap_alone():
    messy = wire(
        {"kind": "button", "label": "Open", "x": -40, "y": 13, "w": 120, "h": 30},
        {"kind": "label", "label": "Status", "x": 1180, "y": 20, "w": 160, "h": 24},
        {"kind": "image_canvas", "label": "", "x": 24, "y": 64, "w": 800, "h": 600},
        {"kind": "scrubber", "label": "Frame", "x": 24, "y": 632, "w": 800, "h": 40},
        title="Viewer")
    stub = Stub(reply(messy))
    res = gd.describe("x", model_call=stub)
    assert res.ok, res.errors
    assert len(stub.prompts) == 1, "snap should have repaired it for free"
    for s in res.shapes:
        assert gd.EDGE_MARGIN <= s.x and s.x2 <= gd.CANVAS_W - gd.EDGE_MARGIN
        assert gd.EDGE_MARGIN <= s.y and s.y2 <= gd.CANVAS_H - gd.EDGE_MARGIN
        for v in (s.x, s.y, s.w, s.h):
            assert v % gd.GRID == 0
    for i, a in enumerate(res.shapes):
        for b in res.shapes[i + 1:]:
            assert not a.overlaps(b), (a.kind, b.kind)
    label = next(s for s in res.shapes if s.kind == "label")
    assert label.w == 160, "moved back inside at its own size, not trimmed"
    assert any("moved back inside" in n for n in res.notes)
    assert any("not covered" in n for n in res.notes)
    ok, errs, _ = gate(res)
    assert ok, errs


def test_snap_never_mutates_what_the_model_returned():
    payload = copy.deepcopy(FORM)
    payload["shapes"][0]["x"] = -40
    before = json.dumps(payload, sort_keys=True)
    checked = gd.check_reply(json.dumps(payload))
    assert checked.ok
    assert json.dumps(checked.payload, sort_keys=True) == before


# ============================================================
# The gate — faults Generate would let through, promoted
# ============================================================

def test_two_full_canvas_containers_go_back_for_repair():
    stub = Stub(reply(STACKED), reply(FORM))
    res = gd.describe("x", model_call=stub)
    assert res.ok and len(stub.prompts) == 2
    wrong = stub.prompts[1].split("WHAT IS WRONG", 1)[1]
    assert "each cover the whole window" in wrong


def test_a_notebook_whose_tabs_do_not_match_its_pages_goes_back():
    bad = with_shape(NOTEBOOK, 0, props={"tabs": ["Only one"]})
    stub = Stub(reply(bad), reply(NOTEBOOK))
    res = gd.describe("x", model_call=stub)
    assert res.ok and len(stub.prompts) == 2
    assert "props.tabs has 1 title(s) but 2 page(s)" in stub.prompts[1]


def test_shapes_snap_cannot_separate_are_an_overlap_fault():
    """Two widgets at the SAME origin: separate_overlaps only trims when one
    starts below or right of the other, so this reaches the layout, which
    falls back to free placement — promoted to a fault."""
    same = wire(
        {"kind": "button", "label": "A", "x": 24, "y": 24, "w": 120, "h": 32},
        {"kind": "button", "label": "B", "x": 24, "y": 24, "w": 120, "h": 32})
    checked = gd.check_reply(reply(same))
    assert checked.stage == gd.STAGE_GATE
    assert any(f.startswith("layout:") and "overlap" in f
               for f in checked.faults), checked.faults


def test_gate_faults_name_shapes_by_position_never_by_widget_name():
    """The model wrote no ids and never saw a widget name like btn_a; a fault
    that says "'btn_a'" names something it cannot find."""
    same = wire(
        {"kind": "button", "label": "A", "x": 24, "y": 24, "w": 120, "h": 32},
        {"kind": "button", "label": "B", "x": 24, "y": 24, "w": 120, "h": 32})
    faults = gd.check_reply(reply(same)).faults
    text = "\n".join(faults)
    assert "'btn_" not in text, faults
    assert 'shape 1 (button "A")' in text and 'shape 2 (button "B")' in text


# ============================================================
# The loop — bounded, best-so-far, never raises
# ============================================================

@pytest.mark.parametrize("n", [1, 2, 3])
def test_exactly_max_attempts_calls_then_ok_false_with_no_shapes(n):
    stub = Stub(reply(STACKED))
    res = gd.describe("x", model_call=stub, max_attempts=n)
    assert len(stub.prompts) == n and res.attempts == n
    assert res.ok is False
    assert res.shapes == [], "an invalid wireframe must never be handed back"
    assert res.window == {}, "nor half of one"
    assert res.errors and res.raw == reply(STACKED)


def test_a_worse_later_round_never_replaces_the_best_answer():
    """Round 2 regresses to prose. Round 3 must be asked to fix round 1's
    nearly-right JSON — not "Sorry" — and a final failure reports round 1."""
    bad = with_shape(FORM, 2, kind="fancy_slider")
    stub = Stub(reply(bad), "Sorry, I cannot help with that.", reply(bad))
    res = gd.describe("x", model_call=stub, max_attempts=3)
    assert len(stub.prompts) == 3
    shown = stub.prompts[2].split("WHAT IS WRONG", 1)[0]
    assert "fancy_slider" in shown and "Sorry" not in shown
    assert res.ok is False and res.raw == reply(bad)
    assert any("fancy_slider" in e for e in res.errors)


def test_a_model_that_raises_is_a_failure_not_an_exception():
    def boom(prompt):
        raise RuntimeError("the GGUF went away")
    res = gd.describe("x", model_call=boom)
    assert res.ok is False and res.shapes == [] and res.attempts == 1
    assert "the GGUF went away" in res.errors[0]


def test_a_model_that_raises_mid_repair_still_reports_what_was_wrong():
    calls = []

    def flaky(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            return reply(STACKED)
        raise TimeoutError("slow")
    res = gd.describe("x", model_call=flaky)
    assert res.ok is False and res.attempts == 2
    assert "slow" in res.errors[0]
    assert any("each cover the whole window" in e for e in res.errors[1:])


def test_no_model_means_zero_calls_and_a_reason():
    """Mirrors gui_classify.classify: the designer must keep working with no
    model loaded, and say why Describe did nothing."""
    res = gd.describe("a form", model_call=None)
    assert res.ok is False and res.attempts == 0 and res.shapes == []
    assert "no model available" in res.errors[0]


@pytest.mark.parametrize("junk", [None, 42, "", "[]", "{}", '{"shapes": 5}',
                                  '{"shapes": [1, 2]}', "{" * 50, '"just text"'])
def test_junk_replies_never_raise(junk):
    res = gd.describe("x", model_call=lambda p: junk, max_attempts=2)
    assert res.ok is False and res.shapes == [] and res.errors


def test_blank_text_and_an_unknown_toolkit_make_no_call():
    stub = Stub(reply(FORM))
    assert gd.describe("   ", model_call=stub).ok is False
    res = gd.describe("x", model_call=stub, toolkit="gtk")
    assert res.ok is False and "unknown toolkit" in res.errors[0]
    assert stub.prompts == []


def test_too_many_shapes_is_one_fault_not_sixty_one():
    many = wire(*[{"kind": "button", "label": f"b{i}", "x": 8, "y": 8,
                   "w": 16, "h": 16} for i in range(gd.MAX_SHAPES + 1)])
    checked = gd.check_reply(reply(many))
    assert checked.faults == [f"{gd.MAX_SHAPES + 1} shapes is too many "
                              f"(at most {gd.MAX_SHAPES}) — merge or drop some"]


# ============================================================
# THE LOAD-BEARING TEST: nothing accepted can be refused by Generate
# ============================================================

@pytest.mark.parametrize("name", ["form", "notebook", "example", "messy"])
def test_every_accepted_result_passes_gui_spec_validate(name):
    payloads = {
        "form": FORM,
        "notebook": NOTEBOOK,
        "example": gd.example_wireframe(),
        "messy": with_shape(with_shape(FORM, 0, x=-17, y=3), 4, w=5000),
    }
    res = gd.describe("x", model_call=Stub(reply(payloads[name])))
    assert res.ok, res.errors
    ok, errs, tree = gate(res)
    assert ok, f"describe accepted what Generate refuses: {errs}"
    assert not [w for w in tree.warnings if " overlap, so " in w]


# ============================================================
# The prompt
# ============================================================

def test_the_prompt_lists_every_catalogue_kind_and_never_names_a_toolkit():
    prompt = gd.build_prompt("a form")
    assert prompt.startswith("You design GUI wireframes for a desktop app "
                             "designer")
    for k in gd.KINDS:
        assert f"\n- {k}" in prompt, k
    for k in CONTAINER_KINDS:
        assert f"- {k} [container]" in prompt, k
    assert "tkinter" not in prompt.lower()
    assert "1100 x 700 px" in prompt and "multiple of 8" in prompt
    assert "REQUEST\na form" in prompt
    assert "Reply with ONLY a JSON object in this exact shape" in prompt


def test_the_prompt_is_the_same_for_both_toolkits():
    """A .gspec is toolkit-neutral; a prompt that named one would steer the
    model toward widgets the other target lacks."""
    assert gd.build_prompt("x", toolkit="qt") == gd.build_prompt("x", toolkit="tk")


def test_the_catalogue_carries_types_and_choices_but_no_handler_props():
    prompt = gd.build_prompt("x")
    assert "relief (flat|raised|sunken|groove|ridge)" in prompt
    assert 'values (["a", "b"])' in prompt
    assert "command (" not in prompt


def test_the_worked_example_is_rescaled_stripped_and_correct():
    """An example that the gate would refuse teaches refusal. It is also the
    one place a model sees whole-wireframe JSON, so it must carry nothing the
    loop then throws away."""
    ex = gd.example_wireframe()
    assert ex is not None
    text = json.dumps(ex)
    for key in ('"port"', '"script"', '"drives"', '"requires"', '"id"',
                '"min_w"', '"min_h"'):
        assert key not in text, key
    # No styling either: a small model copies what it is shown, and the
    # example is pink with a Magneto face.
    for key in ('"bg"', '"fg"', '"font"'):
        assert key not in text, key
    for s in ex["shapes"]:
        for k in ("x", "y", "w", "h"):
            assert s[k] % gd.GRID == 0
        assert s["x"] + s["w"] <= gd.CANVAS_W - gd.EDGE_MARGIN
        assert s["y"] + s["h"] <= gd.CANVAS_H - gd.EDGE_MARGIN
    rows = ex["shapes"]
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            apart = (b["x"] >= a["x"] + a["w"] or b["x"] + b["w"] <= a["x"]
                     or b["y"] >= a["y"] + a["h"] or b["y"] + b["h"] <= a["y"])
            assert apart, ("rescaling created an overlap", a, b)
    checked = gd.check_reply(text)
    assert checked.ok, checked.faults
    assert json.dumps(ex["shapes"][0]) in gd.build_prompt("x")


def test_the_default_prompt_fits_its_budget_with_everything_in_it():
    prompt = gd.build_prompt("A folder picker, three numeric rows, an image "
                             "panel and a slider.")
    assert len(prompt) <= gd.DEFAULT_BUDGET_CHARS
    assert "OPTIONAL STYLE" in prompt and "EXAMPLE" in prompt
    assert "relief (" in prompt


def test_budget_sheds_help_first_then_the_example_then_prop_detail():
    text = "a form"
    full = gd.build_prompt(text, budget_chars=10 ** 6)
    one_less = gd.build_prompt(text, budget_chars=len(full) - 1)
    assert "OPTIONAL STYLE" not in one_less and "EXAMPLE" in one_less
    no_example = gd.build_prompt(text, budget_chars=len(one_less) - 1)
    assert "EXAMPLE" not in no_example and "relief (" in no_example
    lean = gd.build_prompt(text, budget_chars=len(no_example) - 1)
    assert "relief (" not in lean


def test_budget_shedding_never_cuts_the_catalogue():
    """A hard cap would slice the catalogue mid-list, and a model shown half
    the kinds treats the rest as forbidden. Shedding drops whole sections."""
    lean = gd.build_prompt("a long request " * 5, budget_chars=10)
    for k in gd.KINDS:
        assert f"\n- {k}" in lean, k
    assert lean.rstrip().endswith('"bg" and "props" are optional.')
    assert "a long request " * 5 in lean


def test_an_over_budget_prompt_is_sent_whole_and_noted():
    stub = Stub(reply(FORM))
    res = gd.describe("x", model_call=stub, budget_chars=100)
    assert res.ok
    assert any("over the" in n and "budget" in n for n in res.notes)
    assert any("left out" in n for n in res.notes)
    for k in gd.KINDS:
        assert f"\n- {k}" in stub.prompts[0]


def test_repair_prompts_stay_inside_the_budget():
    bad = copy.deepcopy(NOTEBOOK)
    for s in bad["shapes"]:
        s["kind"] = "not_a_kind"
    stub = Stub(reply(bad))
    gd.describe("settings with two tabs", model_call=stub)
    assert len(stub.prompts) == 3
    for p in stub.prompts:
        assert len(p) <= gd.DEFAULT_BUDGET_CHARS, len(p)
    assert stub.prompts[1].rstrip().endswith(
        "Fix every point above. Reply with ONLY the corrected JSON object.")


def test_a_repair_prompt_caps_the_fault_list():
    faults = [f"fault {i}" for i in range(40)]
    p = gd.repair_prompt("x", {"shapes": []}, faults)
    assert "- fault 14" in p and "- fault 15" not in p
    assert "25 more problem(s)" in p


# ============================================================
# The UI helper
# ============================================================

def test_fault_summary_reads_well_both_ways():
    good = gd.describe("x", model_call=Stub(reply(FORM)))
    assert gd.fault_summary(good).startswith("designed 5 widgets in 1 attempt")
    bad = gd.describe("x", model_call=Stub(reply(STACKED)), max_attempts=2)
    text = gd.fault_summary(bad)
    assert text.startswith("could not design it after 2 attempts:")
    assert "\n- " in text
    assert gd.fault_summary(gd.describe("x")).startswith("could not design it")


# ============================================================
# Nested children, flattened
# ============================================================
# Measured on Phi-4 Q4: the file-browser prompt came back with the widgets
# inside the frame's own "shapes", three rounds running. Nesting is how most
# JSON describes a tree, so it is flattened rather than argued with.

SPLIT = [
    {"kind": "frame", "label": "Folders", "x": 16, "y": 16, "w": 336, "h": 600},
    {"kind": "frame", "label": "Files", "x": 368, "y": 16, "w": 712, "h": 600},
]


def test_children_nested_with_canvas_coordinates_are_flattened_in_one_call():
    left, right = copy.deepcopy(SPLIT)
    left["shapes"] = [{"kind": "listbox", "label": "", "x": 32, "y": 48,
                       "w": 304, "h": 552}]
    right["children"] = [
        {"kind": "button", "label": "Refresh", "x": 384, "y": 32,
         "w": 120, "h": 32},
        {"kind": "treeview", "label": "", "x": 384, "y": 80, "w": 680,
         "h": 520, "props": {"columns": ["Name", "Size", "Modified"]}}]
    stub = Stub(reply(wire(left, right)))
    res = gd.describe("a file browser", model_call=stub)
    assert res.ok, res.errors
    assert len(stub.prompts) == 1
    assert [s.kind for s in res.shapes] == ["frame", "listbox", "frame",
                                            "button", "treeview"]
    ok, errs, _tree = gate(res)
    assert ok, errs
    assert any("moved 1 shape(s)" in n for n in res.notes)


def test_children_nested_with_offsets_from_their_parent_are_placed_inside_it():
    """The other common reading: x/y from the container's own corner."""
    left, right = copy.deepcopy(SPLIT)
    right["shapes"] = [{"kind": "treeview", "label": "", "x": 16, "y": 32,
                        "w": 680, "h": 552,
                        "props": {"columns": ["Name", "Size"]}}]
    res = gd.describe("x", model_call=Stub(reply(wire(left, right))))
    assert res.ok, res.errors
    tree = next(s for s in res.shapes if s.kind == "treeview")
    frame = next(s for s in res.shapes if s.label == "Files")
    assert frame.x <= tree.x and tree.x + tree.w <= frame.x + frame.w
    assert frame.y <= tree.y and tree.y + tree.h <= frame.y + frame.h
    assert any("relative" in n for n in res.notes)


def test_nesting_two_deep_keeps_every_container_before_its_children():
    outer = {"kind": "frame", "label": "Outer", "x": 16, "y": 16, "w": 1064,
             "h": 660, "shapes": [
                 {"kind": "labelframe", "label": "Inner", "x": 32, "y": 32,
                  "w": 500, "h": 300, "shapes": [
                      {"kind": "checkbutton", "label": "A", "x": 48, "y": 64,
                       "w": 160, "h": 24},
                      {"kind": "checkbutton", "label": "B", "x": 48, "y": 104,
                       "w": 160, "h": 24}]},
                 {"kind": "button", "label": "Go", "x": 560, "y": 32,
                  "w": 120, "h": 32}]}
    res = gd.describe("x", model_call=Stub(reply(wire(outer))))
    assert res.ok, res.errors
    order = [s.label for s in sorted(res.shapes, key=lambda s: s.z)]
    assert order.index("Outer") < order.index("Inner") < order.index("A")
    ok, errs, _tree = gate(res)
    assert ok, errs


def test_flattening_does_not_modify_the_parsed_reply():
    payload = wire(dict(SPLIT[0], shapes=[{"kind": "listbox", "label": "",
                                          "x": 32, "y": 48, "w": 304,
                                          "h": 552}]))
    before = copy.deepcopy(payload)
    gd._flatten_payload(payload, [])
    assert payload == before


def test_a_repair_prompt_shows_the_flat_list_its_faults_are_numbered_by():
    """Otherwise "shape 3" names a row the model cannot find in what it is
    shown."""
    left = dict(SPLIT[0], shapes=[{"kind": "listbx", "label": "", "x": 32,
                                   "y": 48, "w": 304, "h": 552}])
    stub = Stub(reply(wire(left)), reply(FORM))
    gd.describe("x", model_call=stub)
    assert "shape 2" in stub.prompts[1]
    shown = stub.prompts[1].split("WHAT IS WRONG")[0]
    assert shown.count('"kind"') >= 2
    assert '"listbx"' in shown and '"shapes": [{"kind": "listbx"' not in shown


def test_a_nested_key_that_is_not_a_list_is_still_an_unknown_field():
    bad = dict(SPLIT[0], shapes="listbox")
    res = gd.describe("x", model_call=Stub(reply(wire(bad))), max_attempts=1)
    assert not res.ok
    assert any('unknown field "shapes"' in e for e in res.errors)


# ============================================================
# Deterministic repairs measured on a real model
# ============================================================

PHI4_H1 = json.loads((Path(__file__).resolve().parent / "data"
                      / "describe_phi4_h1.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("round_", ["round2", "round3"])
def test_real_phi4_tab_replies_are_accepted(round_):
    """Verbatim Phi-4 replies to the tabbed-preferences prompt. Both used to
    cost a round each and the prompt failed after three."""
    checked = gd.check_reply(PHI4_H1["replies"][round_])
    assert checked.ok, checked.faults
    books = [s for s in checked.shapes if s.kind == "notebook"]
    assert len(books) == 1
    kids = gl.build_containment_tree(checked.shapes, warnings=[])
    pages = kids.get(books[0].id, [])
    assert len(pages) == 3, "one page per tab title"


def test_a_frame_wrapped_around_the_pages_is_removed():
    shapes = wire(
        {"kind": "notebook", "label": "", "x": 16, "y": 16, "w": 1064,
         "h": 600, "props": {"tabs": ["A", "B"]}},
        {"kind": "frame", "label": "wrap", "x": 24, "y": 40, "w": 1048,
         "h": 568},
        {"kind": "frame", "label": "A", "x": 40, "y": 56, "w": 496, "h": 536},
        {"kind": "frame", "label": "B", "x": 552, "y": 56, "w": 504, "h": 536},
    )
    res = gd.describe("x", model_call=Stub(reply(shapes)))
    assert res.ok, res.errors
    assert "wrap" not in [s.label for s in res.shapes]
    assert any("removed the frame wrapped" in n for n in res.notes)


def test_a_wrapper_is_kept_when_the_pages_do_not_match_the_titles():
    """Three titles, two frames: not unambiguous, so the gate says so."""
    shapes = wire(
        {"kind": "notebook", "label": "", "x": 16, "y": 16, "w": 1064,
         "h": 600, "props": {"tabs": ["A", "B", "C"]}},
        {"kind": "frame", "label": "wrap", "x": 24, "y": 40, "w": 1048,
         "h": 568},
        {"kind": "frame", "label": "A", "x": 40, "y": 56, "w": 496, "h": 536},
        {"kind": "frame", "label": "B", "x": 552, "y": 56, "w": 504, "h": 536},
    )
    checked = gd.check_reply(reply(shapes))
    assert not checked.ok
    assert not any("removed the frame" in n for n in checked.notes)


def test_a_widget_that_runs_out_of_its_frame_is_trimmed_into_it():
    shapes = wire(
        {"kind": "frame", "label": "Log", "x": 560, "y": 312, "w": 512,
         "h": 256},
        {"kind": "label", "label": "Log", "x": 576, "y": 336, "w": 200,
         "h": 16},
        {"kind": "log_pane", "label": "", "x": 576, "y": 360, "w": 1040,
         "h": 256},
    )
    res = gd.describe("x", model_call=Stub(reply(shapes)))
    assert res.ok, res.errors
    frame = next(s for s in res.shapes if s.kind == "frame")
    pane = next(s for s in res.shapes if s.kind == "log_pane")
    assert pane.x + pane.w <= frame.x + frame.w
    assert pane.y + pane.h <= frame.y + frame.h
    assert any("trimmed" in n for n in res.notes)


def test_a_button_below_a_notebook_is_never_trimmed_into_it():
    """Its children are tabs. "Apply and Close below the tabs", drawn just
    inside the bottom edge, would have become two more tabs."""
    book = gd.new_shape("notebook", 24, 24, w=1056, h=576)
    button = gd.new_shape("button", 48, 584, w=120, h=32, label="Apply")
    notes = gd._trim_overflow([book, button], ["shape 1", "shape 2"])
    assert notes == []
    assert (button.y, button.h) == (584, 32)


def test_a_widget_hanging_off_a_frames_edge_is_not_squashed_into_it():
    """gui_snap's reason for its tight parenthood slop, kept: the top-left
    must be well INSIDE the frame, not on its edge."""
    frame = gd.new_shape("frame", 16, 16, w=400, h=200)
    hanging = gd.new_shape("button", 16, 208, w=120, h=32, label="Next")
    assert gd._trim_overflow([frame, hanging], ["shape 1", "shape 2"]) == []


# ============================================================
# Review findings: ranking, budget, bounds, overlaps
# ============================================================

def test_an_empty_object_never_replaces_an_answer_with_rows():
    """By fault count alone "{}" (one fault) outranked a five-shape answer
    with two bad rows, and became the next repair's starting point."""
    nearly = with_shape(with_shape(FORM, 0, kind="lable"), 2, kind="buton")
    stub = Stub(reply(nearly), "{}", reply(FORM))
    gd.describe("x", model_call=stub)
    third = stub.prompts[2]
    assert '"lable"' in third and '"buton"' in third, \
        "the repair prompt should still start from the answer with rows"


def test_a_repair_prompt_fits_the_budget_even_when_the_faults_are_huge():
    """The header was never bounded, and one fault echoing a 5000-character
    label was bigger than the whole budget."""
    huge = with_shape(FORM, 0, kind="lable", label="L" * 5000)
    stub = Stub(reply(huge), reply(FORM))
    res = gd.describe("x", model_call=stub)
    assert res.ok
    assert len(stub.prompts[1]) <= gd.DEFAULT_BUDGET_CHARS


def test_the_reply_budget_matches_what_the_designer_asks_for():
    """Reserving less than the caller requests let a prompt through whose
    reply llama_cpp then quietly cut short."""
    from council_core import designer_project as dp
    assert gd.REPLY_TOKENS == dp.DESCRIBE_NUM_PREDICT


def test_a_309_digit_integer_is_a_fault_not_the_end_of_the_loop():
    """It raised OverflowError out of float() and ended describe() before
    the next round could fix it."""
    bad = reply(with_shape(FORM, 0, x=10 ** 320))
    stub = Stub(bad, reply(FORM))
    res = gd.describe("x", model_call=stub)
    assert res.ok, res.errors
    assert len(stub.prompts) == 2


@pytest.mark.parametrize("props, kind", [
    ({"to": 10 ** 10}, "spinbox"),            # a byte count; dies in setRange
    ({"from_": -(2 ** 40)}, "scale"),
])
def test_an_int_prop_past_32_bits_is_a_fault(props, kind):
    shaped = with_shape(FORM, 2, kind=kind, label="", props=props)
    checked = gd.check_reply(reply(shaped))
    assert not checked.ok
    assert any("out of range" in f for f in checked.faults), checked.faults


def test_text_utf8_cannot_hold_is_a_fault():
    """json.loads turns the escape "\\udc00" into a lone surrogate;
    Generate's file write then raised."""
    raw = reply(FORM).replace('"Run"', '"Run \\udc00"')    # a JSON escape
    assert "\\udc00" in raw
    checked = gd.check_reply(raw)
    assert not checked.ok
    assert any("not text" in f for f in checked.faults)


@pytest.mark.parametrize("font", ["Arial 0", "Arial 99999999999 bold"])
def test_a_font_size_out_of_range_is_a_fault(font):
    checked = gd.check_reply(reply(with_shape(FORM, 2, font=font)))
    assert not checked.ok
    assert any("font size" in f for f in checked.faults)


def test_a_small_sibling_overlap_is_separated_without_a_round():
    """A toolbar running 16 px into the text under it vanished into a clean
    grid, so nothing reported it and it was accepted as drawn."""
    shapes = wire(
        {"kind": "toolbar", "label": "", "x": 16, "y": 16, "w": 1064, "h": 48},
        {"kind": "text", "label": "", "x": 16, "y": 48, "w": 1064, "h": 560},
    )
    stub = Stub(reply(shapes))
    res = gd.describe("x", model_call=stub)
    assert res.ok, res.errors
    assert len(stub.prompts) == 1
    bar, body = sorted(res.shapes, key=lambda s: s.y)
    assert bar.y + bar.h <= body.y
    assert any("no longer overlaps" in n for n in res.notes)


def test_a_deep_sibling_overlap_goes_back_to_the_model():
    """Two panels side by side, crossing by 200 px: not a slip of a few
    pixels, not something gui_snap separates (it leaves containers alone),
    and B does not start INSIDE A, so it is not trimmed into it either."""
    shapes = wire(
        {"kind": "frame", "label": "A", "x": 16, "y": 16, "w": 400, "h": 400},
        {"kind": "labelframe", "label": "B", "x": 200, "y": 16, "w": 400,
         "h": 400},
    )
    checked = gd.check_reply(reply(shapes))
    assert not checked.ok
    assert any("overlap by" in f for f in checked.faults)


def _siblings_overlap(shapes):
    kids = gl.build_containment_tree(shapes, warnings=[])
    by_id = {s.id: s for s in shapes}
    for ids in kids.values():
        sibs = [by_id[i] for i in ids]
        for n, a in enumerate(sibs):
            for b in sibs[n + 1:]:
                ox, oy = gd._overlap(a, b)
                if ox > 0 and oy > 0:
                    return (a.kind, b.kind, ox, oy)
    return None


def test_no_accepted_wireframe_has_overlapping_siblings():
    """Seeded fuzz, the reviewer's method: random root-level replies. Every
    one that is accepted must have no overlapping siblings AND pass
    Generate's own validator."""
    import random
    rng = random.Random(20260928)
    kinds = ["label", "button", "entry", "listbox", "text", "frame",
             "checkbutton", "progressbar"]
    accepted = 0
    for _ in range(400):
        rows = []
        for _k in range(rng.randint(1, 6)):
            w, h = rng.randrange(16, 500), rng.randrange(16, 300)
            rows.append({"kind": rng.choice(kinds), "label": "",
                         "x": rng.randrange(0, 1100 - w),
                         "y": rng.randrange(0, 700 - h), "w": w, "h": h})
        checked = gd.check_reply(reply(wire(*rows)))
        if not checked.ok:
            continue
        accepted += 1
        assert _siblings_overlap(checked.shapes) is None, rows
        res = gd.DescribeResult(ok=True, shapes=checked.shapes,
                                window=checked.window)
        ok, errs, _tree = gate(res)
        assert ok, (errs, rows)
    assert accepted >= 20, "the fuzz should exercise the accept path"


def test_every_kind_hint_names_a_real_kind_and_reaches_the_prompt():
    """Phi-4 never picked labelframe while the catalogue gave only its name
    and props. A hint for a kind that no longer exists would be dead text
    paid for in the same context the reply needs."""
    prompt = gd.build_prompt("a labelled group of options")
    for kind, hint in gd.KIND_HINTS.items():
        assert kind in gd.KINDS, kind
        assert f"- {kind}" in prompt and hint in prompt, kind
    assert len(prompt) <= gd.DEFAULT_BUDGET_CHARS


PHI4_M2 = json.loads((Path(__file__).resolve().parent / "data"
                      / "describe_phi4_m2.json").read_text(encoding="utf-8"))


def test_real_phi4_file_browser_reply_is_accepted():
    """Sent three rounds running and refused each time: the treeview starts
    8 px inside its frame and runs 24 px past its bottom."""
    checked = gd.check_reply(PHI4_M2["replies"]["round1"])
    assert checked.ok, checked.faults
    tree = next(s for s in checked.shapes if s.kind == "treeview")
    kids = gl.build_containment_tree(checked.shapes, warnings=[])
    parent = next(p for p, ids in kids.items() if tree.id in ids)
    assert parent is not None, "the treeview belongs to its frame"


def test_a_shallow_start_that_would_keep_half_is_trimmed_in():
    frame = gd.new_shape("frame", 392, 24, w=664, h=648)
    tree = gd.new_shape("treeview", 400, 120, w=624, h=576)
    notes = gd._trim_overflow([frame, tree], ["shape 1", "shape 2"])
    assert notes and tree.y + tree.h <= frame.y + frame.h


def test_a_widget_hanging_below_a_frame_is_still_not_squashed_into_it():
    """Its corner is 8 px inside the frame's bottom edge, so the shallow tier
    sees it — and trimming would keep nothing of its height, so it does not."""
    frame = gd.new_shape("frame", 16, 16, w=400, h=200)
    hanging = gd.new_shape("button", 40, 208, w=120, h=32, label="Next")
    assert gd._trim_overflow([frame, hanging], ["shape 1", "shape 2"]) == []
    assert (hanging.y, hanging.h) == (208, 32)
