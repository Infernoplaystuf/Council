"""
The harness that has the Council rebuild Typhon (typhon_recreate.py).

No model here. The oracle stub answers each prompt with what Typhon has, so
it must score full marks; a model that answers badly must score low and say
why. The widget stage's machinery (spec, assemble, swap, run the tests,
count, repair) runs against a small test file in tmp_path rather than
Typhon's own suites, which take minutes; run_typhon_recreate.py --stub
oracle runs those.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import typhon_recreate as tr  # noqa: E402


@pytest.fixture(scope="module")
def typhon():
    return tr.load_typhon()


@pytest.fixture(scope="module")
def sections():
    return tr.load_sections()["sections"]


# ============================================================
# The plain-English parts
# ============================================================

def test_every_typhon_shape_is_graded_in_exactly_one_part(typhon, sections):
    for s in typhon.shapes:
        homes = [sec["id"] for sec in sections if tr._inside(s, sec["region"])]
        assert len(homes) == 1, (s.id, s.kind, s.label, homes)


def test_the_parts_tile_the_window_without_overlapping(sections):
    spec = tr.load_sections()
    area = sum(w * h for _x, _y, w, h in (s["region"] for s in sections))
    assert area == spec["canvas"]["w"] * spec["canvas"]["h"]


def test_no_prompt_carries_typhon_itself(typhon, sections, tmp_path):
    """The parts are described, never copied: nothing only typhon.gspec
    says (its port names, its shape ids) reaches a model."""
    seen = []
    oracle = tr.oracle()

    def spy(prompt, **kw):
        seen.append(prompt)
        return oracle(prompt, **kw)

    tr.run_layout(tmp_path, model_call=spy, runtime=False)
    assert seen
    # The port names Typhon's author typed (capture_folder, …), not ones
    # that are also ordinary words ("frame").
    ports = {s.port["name"] for s in typhon.shapes if s.port
             and "_" in s.port["name"]}
    assert len(ports) > 20
    for prompt in seen:
        assert "typhon.gspec" not in prompt
        assert not [p for p in ports if f'"{p}"' in prompt]


# ============================================================
# Stage 1 — layout
# ============================================================

def test_the_oracle_scores_full_marks_on_the_layout(tmp_path):
    r = tr.run_layout(tmp_path, model_call=tr.oracle(), runtime=False)
    assert r["score"] == 100.0
    assert r["totals"]["same_kind"] == r["totals"]["original"] == 93
    assert r["totals"]["read_same"] == r["totals"]["labelled"]
    assert r["findings"] == []
    assert r["structural"] == []
    assert r["window"]["bg_ok"]
    assert r["generate"]["ok"], r["generate"]


def test_a_model_that_answers_nothing_is_refused_part_by_part(tmp_path):
    r = tr.run_layout(tmp_path, model_call=lambda p, **k: "no idea",
                      runtime=False, only=["header", "timings"])
    assert r["score"] == 0.0
    assert all(not s["ok"] for s in r["sections"].values())
    assert any("Title bar" in f for f in r["findings"])
    assert "no shapes at all" in r["structural"]


def test_a_missing_widget_and_a_wrong_kind_are_named():
    a = tr.load_typhon()
    secs = tr.load_sections()["sections"]
    timings = [s for s in a.shapes if tr.section_of(s, secs) == "timings"]
    import copy
    rebuilt = copy.deepcopy(timings)
    entry = next(s for s in rebuilt if s.kind == "entry")
    entry.kind = "spinbox"                              # near, not same
    rebuilt = [s for s in rebuilt if s.kind != "button"]   # no Scan button
    res = tr.SectionResult("timings", "Bad timings", ok=True, shapes=rebuilt)
    g = tr.grade_layout(timings, [s for s in secs if s["id"] == "timings"],
                        [res], {}, {})
    sec = g["sections"]["timings"]
    assert sec["missing"] == ["button 'Scan for bad timings'"]
    assert sec["wrong_kind"] == ["entry drawn as a spinbox"]
    assert sec["near_kind"] == 1
    assert g["score"] < 100


# ============================================================
# Stage 2 — wiring
# ============================================================

def test_the_oracle_scores_full_marks_on_the_wiring(tmp_path):
    r = tr.run_wiring(tmp_path, model_call=tr.oracle(), runtime=False)
    assert r["tally"] == {"PASS": 41}
    assert r["retrieval_misses"] == 0
    assert r["findings"] == []
    assert r["generate"]["ok"], r["generate"]


def test_the_shortlist_offers_the_right_function_for_every_control(
        typhon, sections):
    shapes, want = tr.unwired(typhon)
    offers = tr.catalogue()
    names = {s["id"]: s["name"] for s in sections}
    for s in shapes:
        if s.id not in want:
            continue
        part = names[tr.section_of(s, sections)]
        got = tr.shortlist(s, tr.context_label(s, shapes), part, offers)
        assert (want[s.id]["module"], want[s.id]["function"]) in {
            (o.module, o.name) for o in got}, (s.id, s.label)


def test_a_wrong_link_is_repaired_once_then_graded(tmp_path):
    replies = iter(['{"module": "frame_camera", "function": "nope"}',
                    '{"module": "frame_camera", "function": "status"}'])
    r = tr.run_wiring(tmp_path, model_call=lambda p, **k: next(replies),
                      runtime=False, only=["s12"])
    row = r["controls"][0]
    assert row["calls"] == 2 and row["grade"] == "FAIL"
    assert "frame_timing.scan_report" in r["findings"][0]


def test_right_function_wrong_ports_is_partial():
    want = {"module": "frame_roi", "function": "export_roi",
            "inputs": ["capture_folder", "roi", "output_folder"],
            "outputs": {"save_status": "summary"}}
    got = dict(want, inputs=["roi", "capture_folder", "output_folder"])
    assert tr.grade_link(got, want)["grade"] == "PARTIAL"
    assert tr.grade_link(want, want)["grade"] == "PASS"
    assert tr.grade_link({}, want)["grade"] == "FAIL"


# ============================================================
# Stage 3 — widgets: the spec
# ============================================================

def test_a_spec_has_signatures_and_purpose_but_no_bodies():
    header, plist = tr.pieces("capture_review")
    names = [p.name for p in plist]
    assert "CaptureReviewer" in names and "natural_key" in names
    reviewer = next(p for p in plist if p.name == "CaptureReviewer")
    assert "def play_pause(self) -> str" in reviewer.spec
    body_lines = [ln.strip() for ln in reviewer.original.splitlines()
                  if ln.strip().startswith(("self._", "return self."))]
    assert body_lines
    assert not [ln for ln in body_lines if ln in reviewer.spec]
    assert "from PySide6.QtCore import" in header


def test_the_surface_is_what_the_app_and_its_tests_use():
    assert "PresetPicker" in tr.surface("preset_picker")
    # frame_camera reaches CaptureReviewer by `from ... import`, the tests
    # read natural_key off the module: both count.
    used = tr.surface("capture_review")
    assert {"CaptureReviewer", "natural_key"} <= used
    assert tr.widget_tests("pop_out") == [tr.TESTS / "test_capture_review.py"]


def test_the_policy_gate_refuses_a_piece_before_anything_runs_it():
    piece = tr.Piece("pop_out", "function", "", "")
    bad = "import subprocess\n\ndef pop_out(c, t=''):\n    subprocess.run('x')\n"
    assert any(p.startswith("policy:") for p in tr.check_piece(bad, piece))
    assert tr.check_piece("def other():\n    pass\n", piece) == [
        "does not define pop_out"]
    assert tr.check_piece("def pop_out(:\n", piece)[0].startswith(
        "does not parse")


def test_a_failure_is_blamed_on_the_piece_its_traceback_points_into(tmp_path):
    a = tr.Piece("alpha", "function", "", "")
    b = tr.Piece("beta", "function", "", "")
    source, spans = tr.assemble("import os", [(a, "def alpha():\n    pass"),
                                              (b, "def beta():\n    x = 1\n"
                                                  "    return x")])
    line = spans["beta"][0] + 1
    fail = {"t::one": f'File "/w/mod.py", line {line}, in beta\nboom'}
    assert tr.blame(fail, spans, Path("/w/mod.py"), [a, b]) == {
        "beta": ["t::one"]}
    named = {"t::two": "AttributeError: alpha() got nothing"}
    assert tr.blame(named, spans, Path("/w/mod.py"), [a, b]) == {
        "alpha": ["t::two"]}


# ============================================================
# Stage 3 — widgets: the whole loop, on a small test file
# ============================================================

SMALL_TESTS = '''
from council_qt.widgets import pop_out as po


def test_an_empty_canvas_pops_nothing():
    assert po.pop_out(object(), "x") is None


def test_nothing_to_do_with_the_widget():
    assert 1 + 1 == 2
'''


@pytest.fixture
def small_suite(tmp_path, monkeypatch):
    test_file = tmp_path / "suite" / "test_small_pop_out.py"
    test_file.parent.mkdir()
    test_file.write_text(SMALL_TESTS, encoding="utf-8")
    monkeypatch.setattr(tr, "widget_tests", lambda name: [test_file])
    return test_file


def test_the_oracle_passes_every_counted_test(tmp_path, small_suite):
    rep = tr.rebuild_widget("pop_out", tmp_path / "w",
                            model_call=tr.oracle(), repairs=0)
    # The test that passes with the widget gone is not counted.
    assert rep["baseline"]["counted"] == [
        "test_small_pop_out::test_an_empty_canvas_pops_nothing"]
    assert (rep["passed"], rep["counted"], rep["score"]) == (1, 1, 100.0)
    assert Path(rep["module"]).read_text(encoding="utf-8").count(
        "def pop_out(") == 1


def test_a_model_that_never_delivers_is_reported_and_repaired(
        tmp_path, small_suite):
    calls = []

    def bad(prompt, **kw):
        calls.append(prompt)
        return "```python\ndef something_else():\n    pass\n```"

    rep = tr.rebuild_widget("pop_out", tmp_path / "w", model_call=bad,
                            repairs=1)
    assert rep["passed"] == 0 and rep["counted"] == 1
    assert rep["pieces"]["pop_out"]["problems"] == ["does not define pop_out"]
    assert len(calls) == 2                     # the write, then one repair
    assert "these failed" in calls[1]
    found = tr.widget_findings(rep)
    assert any("never delivered" in f for f in found)
    assert any("0 of 1" in f for f in found)


# ============================================================
# The report
# ============================================================

def test_the_report_leads_with_the_problem_areas(tmp_path):
    results = {
        "started": "now", "model": "stub",
        "layout": tr.run_layout(tmp_path, model_call=lambda p, **k: "",
                                runtime=False, only=["header"]),
        "wiring": tr.run_wiring(tmp_path, model_call=tr.oracle(),
                                runtime=False, only=["s12"]),
    }
    path = tr.write_report(results, tmp_path / "out")
    text = path.read_text(encoding="utf-8")
    assert text.index("## Problem areas") < text.index("## Layout")
    assert "could not produce 1 of 1 parts (Title bar)" in text
    assert json.loads((tmp_path / "out" / "results.json").read_text(
        encoding="utf-8"))["wiring"]["tally"] == {"PASS": 1}


def test_the_side_by_side_picture_is_drawn(tmp_path, typhon):
    assert tr.draw_wireframe(typhon.shapes, (1504, 1016),
                             tmp_path / "t.png", bg=typhon.window.bg,
                             title="Typhon")
    assert (tmp_path / "t.png").stat().st_size > 10_000


def test_every_module_constant_is_one_piece_shared_by_all():
    _h, plist = tr.pieces("camera_settings_window")
    consts = plist[0]
    assert consts.kind == "constants"
    # `FLOAT, INT, BOOL, CHOICE, TEXT = ...` binds five names, and the
    # setting rows need them although no test reads them off the module.
    assert {"FLOAT", "INT", "TEXT"} <= set(consts.names)
    stub = tr.placeholder(consts)
    assert "TEXT = None" in stub


def test_the_councils_own_imports_pass_the_gate_and_nothing_else_new():
    piece = tr.Piece("f", "function", "", "")
    lazy = "def f():\n    from council_core import live_display\n    return 1\n"
    assert tr.check_piece(lazy, piece) == []
    net = "def f():\n    import socket\n    return socket\n"
    assert tr.check_piece(net, piece)
