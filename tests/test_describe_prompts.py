"""
The graded Describe-it prompts, and the harness that grades them.

No model here. The prompt file is checked for the mistakes that would make a
real run grade the wrong thing — a kind the palette does not have, a prop the
kind does not take — and the grader is checked against wireframes whose right
answer is known. One prompt then goes the whole way, describe → Generate →
construct the app in a subprocess, through a scripted model.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_describe_prompts as rdp  # noqa: E402
from gui_shapes import PALETTE, new_shape  # noqa: E402

TIERS = {"easy", "medium", "hard", "adversarial"}


def mk(kind, label="", x=40, y=40, w=120, h=32, **props):
    shape = new_shape(kind, x, y, w=w, h=h, label=label)
    shape.props.update(props)
    return shape


def result(ok=True, shapes=(), errors=()):
    return SimpleNamespace(ok=ok, shapes=list(shapes), errors=list(errors),
                           notes=[], attempts=1, window={})


# ============================================================
# The prompt file
# ============================================================

PROMPTS = rdp.load_prompts()


def test_every_prompt_has_a_unique_id_and_a_known_tier():
    ids = [p["id"] for p in PROMPTS]
    assert len(ids) == len(set(ids))
    assert {p["tier"] for p in PROMPTS} == TIERS


def test_every_tier_has_prompts():
    for tier in TIERS:
        assert sum(p["tier"] == tier for p in PROMPTS) >= 4, tier


@pytest.mark.parametrize("prompt", PROMPTS, ids=lambda p: p["id"])
def test_every_kind_a_prompt_expects_is_a_real_palette_kind(prompt):
    """A typo here grades every answer PARTIAL for a reason the model never
    saw, and the run reads as the model's failure."""
    expect = prompt["expect"]
    kinds = (list(expect.get("kinds_all", [])) + list(expect.get("kinds_any", []))
             + list((expect.get("count_at_least") or {}).keys())
             + [r["kind"] for r in expect.get("props_any", [])])
    for kind in kinds:
        assert kind in PALETTE and kind != "generic", kind
    for rule in expect.get("props_any", []):
        assert rule["prop"] in PALETTE[rule["kind"]].get("prop_schema", {}), rule


@pytest.mark.parametrize("prompt", PROMPTS, ids=lambda p: p["id"])
def test_only_adversarial_prompts_may_be_refused(prompt):
    expected = prompt["expect"]["ok"]
    if prompt["tier"] == "adversarial":
        assert expected is None
    else:
        assert expected is True
    assert prompt["text"].strip() and prompt["why"].strip()


# ============================================================
# The grader
# ============================================================

LOGIN = {"ok": True, "kinds_all": ["label", "entry", "button"],
         "count_at_least": {"entry": 2}, "labels_any": ["sign in"],
         "min_shapes": 3, "max_shapes": 12}


def login_shapes():
    return [mk("label", "User", 40, 40), mk("entry", "", 176, 40, w=320),
            mk("label", "Password", 40, 88), mk("entry", "", 176, 88, w=320),
            mk("button", "Sign in", 296, 144, w=96)]


def test_everything_asked_for_is_a_pass():
    assert rdp.grade(LOGIN, result(shapes=login_shapes()))["grade"] == rdp.PASS


def test_a_missing_kind_is_partial_not_fail():
    """Valid and runnable, just not what was asked — a different failure from
    an app that cannot be generated, and graded as one."""
    shapes = [s for s in login_shapes() if s.kind != "label"]
    graded = rdp.grade(LOGIN, result(shapes=shapes))
    assert graded["grade"] == rdp.PARTIAL
    assert "no label" in graded["problems"]


def test_a_refusal_fails_where_an_answer_was_expected():
    assert rdp.grade(LOGIN, result(ok=False, errors=["x"]))["grade"] == rdp.FAIL


def test_a_refusal_is_safe_where_one_is_allowed():
    graded = rdp.grade({"ok": None}, result(ok=False, errors=["no"]))
    assert graded["grade"] == rdp.SAFE


def test_a_shape_off_the_canvas_fails_whatever_gui_describe_claimed():
    """The grader re-derives validity. Asking the thing under test whether it
    passed grades nothing."""
    shapes = login_shapes() + [mk("button", "Far", 1080, 40, w=96)]
    graded = rdp.grade(LOGIN, result(shapes=shapes))
    assert graded["grade"] == rdp.FAIL
    assert any("outside" in p for p in graded["problems"])


def test_a_script_the_model_wrote_fails_the_prompt():
    shapes = login_shapes()
    shapes[-1].script = {"module": "os", "function": "system"}
    graded = rdp.grade({"ok": None, "no_code_fields": True},
                       result(shapes=shapes))
    assert graded["grade"] == rdp.FAIL


def test_too_many_shapes_fails_even_when_refusal_was_allowed():
    shapes = [mk("button", f"B{i}", 16 + (i % 10) * 104, 16 + (i // 10) * 48,
                 w=96) for i in range(12)]
    graded = rdp.grade({"ok": None, "max_shapes": 10}, result(shapes=shapes))
    assert graded["grade"] == rdp.FAIL


def test_a_list_prop_given_as_a_string_does_not_count():
    """"Light,Dark,System" as one string splits into characters in the Qt
    combobox. The expectation must not be met by it."""
    expect = {"ok": True, "props_any": [
        {"kind": "combobox", "prop": "values", "contains": "Dark"}]}
    as_list = [mk("combobox", "Theme", values=["Light", "Dark"])]
    as_text = [mk("combobox", "Theme", values="Light,Dark")]
    assert rdp.grade(expect, result(shapes=as_list))["grade"] == rdp.PASS
    assert rdp.grade(expect, result(shapes=as_text))["grade"] == rdp.PARTIAL


def test_the_notebook_tab_count_is_checked():
    expect = {"ok": True, "notebook_tabs": 3}
    book = mk("notebook", "", 16, 16, w=600, h=400, tabs=["A", "B"])
    assert rdp.grade(expect, result(shapes=[book]))["grade"] == rdp.PARTIAL


# ============================================================
# One prompt, the whole way
# ============================================================

LOGIN_REPLY = json.dumps({
    "window": {"title": "Sign in"},
    "shapes": [
        {"kind": "label", "label": "Username", "x": 40, "y": 40, "w": 120, "h": 32},
        {"kind": "entry", "label": "", "x": 176, "y": 40, "w": 320, "h": 32},
        {"kind": "label", "label": "Password", "x": 40, "y": 88, "w": 120, "h": 32},
        {"kind": "entry", "label": "", "x": 176, "y": 88, "w": 320, "h": 32},
        {"kind": "button", "label": "Sign in", "x": 296, "y": 144, "w": 96, "h": 32},
        {"kind": "button", "label": "Cancel", "x": 400, "y": 144, "w": 96, "h": 32},
    ]})


def test_a_prompt_goes_from_text_to_a_generated_qt_project(tmp_path):
    e1 = next(p for p in PROMPTS if p["id"] == "E1")
    record = rdp.run_one(e1, tmp_path / "vault",
                         model_call=lambda _prompt: LOGIN_REPLY,
                         runtime=False)
    assert record["grade"] == rdp.PASS, record
    assert record["generate"]["ok"], record
    assert (Path(record["project"]) / "app.py").read_text(
        encoding="utf-8").count("PySide6")


def test_the_generated_app_is_constructed_and_poked_in_a_subprocess(tmp_path):
    pytest.importorskip("PySide6")
    e1 = next(p for p in PROMPTS if p["id"] == "E1")
    record = rdp.run_one(e1, tmp_path / "vault",
                         model_call=lambda _prompt: LOGIN_REPLY)
    assert record["grade"] == rdp.PASS, record
    assert record["runtime"]["ok"], record["runtime"]
    assert "poked=" in record["runtime"]["detail"]


def test_a_run_never_overwrites_an_existing_project(tmp_path):
    e1 = next(p for p in PROMPTS if p["id"] == "E1")
    first = rdp.run_one(e1, tmp_path / "vault",
                        model_call=lambda _prompt: LOGIN_REPLY, runtime=False)
    second = rdp.run_one(e1, tmp_path / "vault",
                         model_call=lambda _prompt: LOGIN_REPLY, runtime=False)
    assert first["project"] != second["project"]


def test_overlapping_siblings_fail_the_grade():
    """gui_spec.validate does not see an overlap the layout's grid absorbs,
    so the grader checks pixels itself."""
    shapes = login_shapes() + [mk("button", "Late", 296 + 48, 144, w=96)]
    graded = rdp.grade(LOGIN, result(shapes=shapes))
    assert graded["grade"] == rdp.FAIL
    assert any("overlap" in p for p in graded["problems"])


def test_a_handler_that_raises_fails_the_runtime_probe(tmp_path):
    """The ports runtime catches a handler's exception and PRINTS "handler
    raised" to stdout; a stderr-only scan graded that app as a pass."""
    pytest.importorskip("PySide6")
    e1 = next(p for p in PROMPTS if p["id"] == "E1")
    record = rdp.run_one(e1, tmp_path / "vault",
                         model_call=lambda _prompt: LOGIN_REPLY, runtime=False)
    pdir = Path(record["project"])
    handlers = pdir / "handlers.py"
    import re
    text = handlers.read_text(encoding="utf-8")
    boom = ("def on_btn_sign_in(self, *args):\n"
            "        raise RuntimeError('boom')\n\n"
            "    def _unused(self, *args):")
    edited, n = re.subn(r"def on_btn_sign_in\(self, \*args\)[^:\n]*:",
                        lambda _m: boom, text)
    assert n == 1, "the stub's signature changed; the test must follow it"
    handlers.write_text(edited, encoding="utf-8")
    ran = rdp.run_generated(pdir)
    assert not ran["ok"], ran
    assert "handler raised" in ran["detail"], ran
