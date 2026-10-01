"""
Classification tests — the only model-facing module, driven entirely by stubs.

NO GGUF IS LOADED ANYWHERE IN THIS FILE. model_call is an injected callable, so
every path (valid reply, invalid kind, malformed JSON, low confidence, the
repair loop, total failure) is scripted and deterministic. That injection is the
whole reason this module is testable at all.

Run:  python -m pytest tests/test_gui_classify.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui_classify as gcl   # noqa: E402
import gui_layout as gl      # noqa: E402
from gui_shapes import GENERIC_KIND, Shape  # noqa: E402


def generic(sid, x=0, y=0, w=200, h=100, label="", note=""):
    return Shape(id=sid, kind=GENERIC_KIND, x=x, y=y, w=w, h=h,
                 label=label, note=note)


class Stub:
    """A scripted model. Records prompts so the repair loop is observable."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if not self.replies:
            return ""
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def reply(*rows):
    return json.dumps({"shapes": list(rows)})


# ============================================================
# The zero-model path
# ============================================================

def test_no_generic_shapes_means_no_model_call():
    """The common path for a carefully drawn wireframe. If this ever regresses,
    every generation starts loading a model it does not need."""
    stub = Stub(reply())
    typed = [Shape(id="a", kind="button", x=0, y=0, w=10, h=10),
             Shape(id="b", kind="treeview", x=0, y=0, w=10, h=10)]
    cls, qs = gcl.classify(typed, None, stub)
    assert (cls, qs) == ([], [])
    assert stub.prompts == [], "a fully typed wireframe must not call the model"
    assert gcl.needs_model(typed) is False


def test_needs_model_detects_a_single_generic_shape():
    assert gcl.needs_model([Shape(id="a", kind="button", x=0, y=0, w=1, h=1),
                            generic("g")]) is True


# ============================================================
# The happy path
# ============================================================

def test_valid_reply_is_accepted():
    stub = Stub(reply({"id": "g1", "kind": "treeview", "confidence": 0.94,
                       "props": {"mode": "table", "columns": ["Layer", "N"]}}))
    cls, qs = gcl.classify([generic("g1", label="Layer Stats")], None, stub)
    assert len(stub.prompts) == 1, "one call, no repair needed"
    assert len(cls) == 1
    c = cls[0]
    assert (c.kind, c.confidence, c.flagged) == ("treeview", 0.94, False)
    assert c.props == {"mode": "table", "columns": ["Layer", "N"]}
    assert qs == [], "a confident answer asks nothing"


def test_prompt_carries_relative_geometry_and_siblings():
    """Relative position identifies a toolbar; raw pixels do not survive the
    user resizing the canvas."""
    shapes = [
        Shape(id="box", kind="frame", x=0, y=0, w=1000, h=800),
        generic("g1", 10, 10, 950, 40, label="Tools"),
        Shape(id="s2", kind="button", x=10, y=100, w=80, h=30, label="Run"),
    ]
    tree = gl.infer(shapes, 1000, 800)
    stub = Stub(reply({"id": "g1", "kind": "toolbar", "confidence": 0.9}))
    gcl.classify(shapes, tree, stub)
    p = stub.prompts[0]
    assert "% of its width" in p and "across and" in p
    assert "Tools" in p
    assert "toolbar" in p, "the catalogue must be in the prompt"
    assert GENERIC_KIND not in p.split("You may ONLY use these")[1], (
        "'generic' is the question, it cannot be an allowed answer")


# ============================================================
# Repair loop
# ============================================================

def test_an_invalid_kind_drives_a_repair_pass():
    stub = Stub(
        reply({"id": "g1", "kind": "holographic_dial", "confidence": 0.9}),
        reply({"id": "g1", "kind": "scale", "confidence": 0.88}),
    )
    cls, qs = gcl.classify([generic("g1")], None, stub)
    assert len(stub.prompts) == 2, "an invalid kind must trigger exactly one repair"
    assert "WHAT IS WRONG" in stub.prompts[1], "the repair shows the faults"
    assert "holographic_dial" in stub.prompts[1], "...and the model's own output"
    assert cls[0].kind == "scale" and not cls[0].flagged


def test_an_unknown_prop_key_is_rejected_then_repaired():
    stub = Stub(
        reply({"id": "g1", "kind": "treeview", "confidence": 0.9,
               "props": {"colour_scheme": "inferno"}}),
        reply({"id": "g1", "kind": "treeview", "confidence": 0.9,
               "props": {"mode": "tree"}}),
    )
    cls, _ = gcl.classify([generic("g1")], None, stub)
    assert len(stub.prompts) == 2
    assert cls[0].props == {"mode": "tree"}


def test_malformed_json_drives_repair_then_succeeds():
    stub = Stub("I think this is a table, probably.",
                reply({"id": "g1", "kind": "treeview", "confidence": 0.8}))
    cls, _ = gcl.classify([generic("g1")], None, stub)
    assert len(stub.prompts) == 2
    assert cls[0].kind == "treeview"


def test_json_wrapped_in_prose_and_fences_is_still_read():
    """Reuses nx_generate's balanced-brace scanner — a regex from the first '{'
    to the last '}' gets this wrong, as the Grapher's analyst proved."""
    stub = Stub("Sure! Here you go:\n```json\n"
                + reply({"id": "g1", "kind": "listbox", "confidence": 0.85})
                + "\n```\nHope that helps.")
    cls, _ = gcl.classify([generic("g1")], None, stub)
    assert len(stub.prompts) == 1
    assert cls[0].kind == "listbox"


def test_repair_gives_up_after_max_attempts_and_falls_back_to_label():
    stub = Stub(reply({"id": "g1", "kind": "nonsense", "confidence": 0.9}))
    cls, qs = gcl.classify([generic("g1", label="???")], None, stub,
                           max_attempts=3)
    assert len(stub.prompts) == 3, "exactly max_attempts calls, then stop"
    c = cls[0]
    assert c.kind == gcl.FALLBACK_KIND == "label"
    assert c.flagged, "the user must be told this was a fallback, not a choice"
    assert c.reason
    assert len(qs) == 1, "a failed shape always becomes a question"


def test_partial_credit_keeps_the_shapes_that_worked():
    """A reply that types three shapes right and one wrong must keep the three
    — re-asking for all of them wastes the good answers."""
    stub = Stub(
        reply({"id": "a", "kind": "treeview", "confidence": 0.9},
              {"id": "b", "kind": "button", "confidence": 0.9},
              {"id": "c", "kind": "not_a_widget", "confidence": 0.9}),
        reply({"id": "c", "kind": "entry", "confidence": 0.9}),
    )
    cls, _ = gcl.classify([generic("a"), generic("b"), generic("c")], None, stub)
    kinds = {c.shape_id: c.kind for c in cls}
    assert kinds == {"a": "treeview", "b": "button", "c": "entry"}
    # Boxes are numbered in the prompt (an id is 32 tokens); the numbers stay
    # those of the first prompt, so box 3 is still box 3 in the repair.
    boxes = stub.prompts[1].split("BOXES", 1)[1]
    assert "- box 1:" not in boxes, "the repair only re-asks the failures"
    assert "- box 3:" in boxes


# ============================================================
# Confidence -> questions (spec 10.2)
# ============================================================

def test_low_confidence_becomes_a_question_not_a_guess():
    stub = Stub(reply({"id": "g1", "kind": "entry", "confidence": 0.42}))
    cls, qs = gcl.classify([generic("g1", label="Layer 47")], None, stub)
    assert cls[0].kind == "entry", "the guess is kept as the default..."
    assert len(qs) == 1, "...but the user is asked"
    q = qs[0]
    assert q.shape_id == "g1" and "Layer 47" in q.question
    assert "entry" in q.options and q.default == "entry"


def test_confidence_at_the_floor_is_accepted_silently():
    stub = Stub(reply({"id": "g1", "kind": "entry",
                       "confidence": gcl.CONFIDENCE_FLOOR}))
    _cls, qs = gcl.classify([generic("g1")], None, stub)
    assert qs == [], "the floor is inclusive; only BELOW it asks"


def test_confidence_is_clamped_and_junk_becomes_zero():
    stub = Stub(reply({"id": "g1", "kind": "entry", "confidence": 5},
                      {"id": "g2", "kind": "entry", "confidence": "very"}))
    cls, _ = gcl.classify([generic("g1"), generic("g2")], None, stub)
    conf = {c.shape_id: c.confidence for c in cls}
    assert conf["g1"] == 1.0 and conf["g2"] == 0.0


# ============================================================
# Failure modes that must not crash
# ============================================================

def test_a_model_that_raises_falls_back_cleanly():
    def boom(_p):
        raise RuntimeError("model exploded")
    cls, qs = gcl.classify([generic("g1")], None, boom)
    assert cls[0].kind == "label" and cls[0].flagged
    assert "exploded" in cls[0].reason
    assert len(qs) == 1


def test_no_model_available_still_returns_a_usable_answer():
    cls, qs = gcl.classify([generic("g1")], None, None)
    assert cls[0].flagged and cls[0].kind == "label"
    assert len(qs) == 1


def test_a_reply_about_the_wrong_shape_is_rejected():
    stub = Stub(reply({"id": "somebody_else", "kind": "entry",
                       "confidence": 0.9}))
    cls, _ = gcl.classify([generic("g1")], None, stub, max_attempts=1)
    assert cls[0].flagged, "an id we never asked about must not be accepted"


def test_apply_classifications_shapes_the_map_gui_spec_wants():
    cls = [gcl.Classification("s1", "treeview", 0.9, {"mode": "tree"})]
    assert gcl.apply_classifications(cls) == {
        "s1": {"kind": "treeview", "props": {"mode": "tree"}}}


# ============================================================
# Small local models
# ============================================================

class Model(Stub):
    """A stub taking the keywords designer_project's call takes."""

    def __init__(self, *replies):
        super().__init__(*replies)
        self.options = []

    def __call__(self, prompt, *, json_schema=None, num_predict=None,
                 temperature=None, seed=None):
        self.options.append(dict(schema=json_schema, num_predict=num_predict,
                                 temperature=temperature, seed=seed))
        return super().__call__(prompt)


def test_boxes_are_numbered_and_answers_map_back_by_number():
    """A shape id is 32 hex characters and 32 tokens on phi3.5; ten boxes
    cut a 700-token reply off. "box": 2 is one token and cannot be echoed
    wrong into another box."""
    stub = Stub(json.dumps({"shapes": [
        {"box": 2, "kind": "entry", "confidence": 0.9},
        {"box": 1, "kind": "button", "confidence": 0.95}]}))
    a, b = generic("a" * 32), generic("b" * 32, x=300)
    cls, _ = gcl.classify([a, b], None, stub)
    assert {c.shape_id: c.kind for c in cls} == {a.id: "button",
                                                 b.id: "entry"}
    boxes = stub.prompts[0].split("BOXES", 1)[1]
    assert "- box 1:" in boxes and "- box 2:" in boxes
    assert a.id not in stub.prompts[0]


def test_the_prompt_lists_every_kinds_props_and_names_no_toolkit():
    stub = Stub(reply({"box": 1, "kind": "label", "confidence": 0.9}))
    gcl.classify([generic("g1")], None, stub)
    p = stub.prompts[0]
    assert "Tkinter" not in p and "tkinter" not in p
    assert "- treeview = a table (props.columns) or a tree — props: mode " \
           "(table|tree)" in p
    assert "command (" not in p, "callbacks are wired by generation"


def test_a_list_prop_written_as_a_string_is_a_fault_not_characters():
    """'values': 'Low,High' passed every check and filled the combobox with
    L, o, w, ',', H, ..."""
    stub = Stub(
        reply({"box": 1, "kind": "combobox", "confidence": 0.9,
               "props": {"values": "Low,High"}}),
        reply({"box": 1, "kind": "combobox", "confidence": 0.9,
               "props": {"values": ["Low", "High"]}}))
    cls, _ = gcl.classify([generic("g1")], None, stub)
    assert len(stub.prompts) == 2
    assert "props.values must be a JSON list" in stub.prompts[1]
    assert cls[0].props == {"values": ["Low", "High"]}


def test_a_callback_prop_is_ignored_not_a_fault():
    stub = Stub(reply({"box": 1, "kind": "button", "confidence": 0.9,
                       "props": {"command": "on_go", "text": "Go"}}))
    cls, _ = gcl.classify([generic("g1")], None, stub)
    assert len(stub.prompts) == 1 and cls[0].props == {"text": "Go"}


def test_each_call_gets_a_schema_over_the_boxes_asked_and_a_sized_reply():
    model = Model(
        reply({"box": 1, "kind": "entry", "confidence": 0.9},
              {"box": 2, "kind": "holo_dial", "confidence": 0.9},
              {"box": 3, "kind": "button", "confidence": 0.9}),
        reply({"box": 2, "kind": "scale", "confidence": 0.9}))
    gcl.classify([generic("a"), generic("b"), generic("c")], None, model)
    first, second = model.options
    assert first["schema"]["$defs"]["box"]["enum"] == [1, 2, 3]
    assert second["schema"]["$defs"]["box"]["enum"] == [2], \
        "the repair asks only about the box that failed"
    assert first["num_predict"] == gcl.num_predict_for(3)
    assert second["num_predict"] == gcl.num_predict_for(1)
    assert first["seed"] is None and second["seed"] is not None


def test_the_reply_budget_grows_with_the_boxes():
    assert gcl.num_predict_for(1) == gcl.MIN_TOKENS
    assert gcl.num_predict_for(10) > 700, "ten boxes no longer cut off"
    assert gcl.num_predict_for(10_000) == gcl.MAX_TOKENS


def test_answer_schema_admits_good_rows_and_refuses_bad_ones():
    jsonschema = __import__("pytest").importorskip("jsonschema")
    schema = gcl.answer_schema([1, 2])
    jsonschema.validate({"shapes": [
        {"box": 1, "kind": "treeview", "confidence": 0.9,
         "props": {"columns": ["A", "B"]}}]}, schema)
    for bad in ({"box": 3, "kind": "label", "confidence": 0.9},
                {"box": 1, "kind": "generic", "confidence": 0.9},
                {"box": 1, "kind": "label", "confidence": 0.95},
                {"box": 1, "kind": "label", "confidence": 0.9,
                 "props": {"colour_scheme": "x"}}):
        with __import__("pytest").raises(jsonschema.ValidationError):
            jsonschema.validate({"shapes": [bad]}, schema)


def test_a_cut_off_reply_keeps_its_complete_rows():
    """Truncated mid-row: the rows before the cut are answers, and only the
    rest is asked again."""
    full = reply({"box": 1, "kind": "entry", "confidence": 0.9},
                 {"box": 2, "kind": "button", "confidence": 0.9},
                 {"box": 3, "kind": "listbox", "confidence": 0.9})
    cut = full[:full.index('"listbox"')]
    stub = Stub(cut, reply({"box": 3, "kind": "listbox", "confidence": 0.9}))
    cls, _ = gcl.classify([generic("a"), generic("b"), generic("c")], None,
                          stub)
    assert [c.kind for c in cls] == ["entry", "button", "listbox"]
    assert len(stub.prompts) == 2
    boxes = stub.prompts[1].split("BOXES", 1)[1]
    assert "- box 3:" in boxes and "- box 1:" not in boxes


def test_a_repeated_answer_is_never_asked_for_identically():
    model = Model(reply({"box": 1, "kind": "holo_dial", "confidence": 0.9}))
    gcl.classify([generic("g")], None, model, max_attempts=3)
    asks = [(p, o["temperature"], o["seed"])
            for p, o in zip(model.prompts, model.options)]
    assert len(set(asks)) == 3
    assert gcl.REPEAT_NOTE in model.prompts[2]


def test_a_long_box_list_sheds_the_prop_detail_to_fit_the_window():
    """Measured: 20 boxes with every kind's props are ~2,300 prompt tokens
    plus a 1,376-token reply. Past the window the props go, never the
    kinds, and a bigger window keeps them."""
    boxes = [generic(f"g{i}", y=70 * i, label=f"Field {i}") for i in range(30)]
    small = Stub(reply({"box": 1, "kind": "entry", "confidence": 0.9}))
    gcl.classify(boxes, None, small, max_attempts=1)
    big = Stub(reply({"box": 1, "kind": "entry", "confidence": 0.9}))
    gcl.classify(boxes, None, big, max_attempts=1, n_ctx=16384)
    assert "— props:" not in small.prompts[0]
    assert "— props:" in big.prompts[0]
    for k in gcl.CLASSIFIABLE:
        assert f"\n- {k}" in small.prompts[0], k
    assert gcl.fits(big.prompts[0], 30, 16384)


def test_only_real_answers_are_persistable():
    cls = [gcl.Classification("a", "entry", 0.9),
           gcl.Classification("b", "label", 0.0, flagged=True)]
    assert gcl.persistable(cls) == {"a": {"kind": "entry", "props": {}}}


def test_a_repair_names_only_the_boxes_it_asks_about():
    """REVIEW: a repair asks about the boxes still wrong — its BOXES list and
    its schema's box enum hold only those — but the faults were checked
    against EVERY box, so round 3 told the model "box 1: no classification
    returned" about a box it was not shown and the schema forbade."""
    model = Model(
        reply({"box": 1, "kind": "entry", "confidence": 0.9},
              {"box": 2, "kind": "button", "confidence": 0.9,
               "props": {"colour": "red"}}),
        reply({"box": 2, "kind": "button", "confidence": 0.9,
               "props": {"size": 3}}),
        reply({"box": 2, "kind": "button", "confidence": 0.9}))
    cls, _ = gcl.classify([generic("a"), generic("b", y=200)], None, model)
    assert [(c.kind, c.flagged) for c in cls] == [("entry", False),
                                                  ("button", False)]
    assert len(model.prompts) == 3
    third = model.prompts[2].split("WHAT IS WRONG", 1)[1].split(
        "You are labelling")[0]
    assert "box 2" in third and "box 1" not in third, third


def test_a_reply_nested_past_the_recursion_limit_is_one_bad_reply():
    """REVIEW: the classifier now reads replies with gui_describe.parse_reply,
    whose cut-off salvage let a RecursionError out — a small model stuck
    writing "[" until the token limit made classify RAISE, and Generate fail,
    where 1fadf49's scanner just saw no JSON and asked again."""
    bomb = '{"shapes": ' + "[" * 3000 + "]" * 2999
    stub = Stub(bomb, reply({"box": 1, "kind": "entry", "confidence": 0.9}))
    cls, _ = gcl.classify([generic("a")], None, stub)
    assert [(c.kind, c.flagged) for c in cls] == [("entry", False)]
    assert len(stub.prompts) == 2


def test_a_prop_nested_past_the_recursion_limit_is_never_accepted():
    """REVIEW: _check_prop reads an exception from the type check as "fine",
    and the check recursed — a 900-deep list came back as a combobox's
    values, to be written into the .gspec and emitted as code no Python
    parser accepts (it allows 200 nested brackets)."""
    deep = "[" * 900 + "]" * 900
    bad = reply({"box": 1, "kind": "combobox", "confidence": 0.9,
                 "props": {"values": "DEEP"}}).replace('"DEEP"', deep)
    cls, _ = gcl.classify([generic("a")], None, Stub(bad))
    assert all(c.flagged or "values" not in c.props for c in cls), cls
