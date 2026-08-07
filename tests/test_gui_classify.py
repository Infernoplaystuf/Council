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
    assert "id: a" not in stub.prompts[1], "the repair only re-asks the failures"
    assert "id: c" in stub.prompts[1]


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
