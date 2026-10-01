"""
run_codebehind_prompts.py — the graded harness for the code writer, kept
working with --stub (each task's canned reply; no model).

The tasks' expectations are checked against real numbers from gui_smoke's
sample data (3 PNGs at levels 20/128/235, a 5-row CSV), so these also pin
the sample data the grades depend on.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import run_codebehind_prompts as rcp  # noqa: E402


def test_every_task_is_well_formed():
    tasks = rcp.load_prompts()
    ids = [t["id"] for t in tasks]
    assert len(ids) == len(set(ids)) >= 10
    for t in tasks:
        assert t["mode"] in ("function", "handler") and t["text"].strip()
        assert "stub" in t and "expect" in t
        if t["expect"].get("ok") is None:
            assert t["expect"].get("forbid"), t["id"]


def test_the_stub_run_passes_end_to_end(tmp_path):
    out = tmp_path / "res.jsonl"
    code = rcp.main(["--vault", str(tmp_path / "v"), "--stub",
                     "--only", "E1,M2,H1,G1,A1,A2", "--out", str(out)])
    rows = [json.loads(ln) for ln in out.read_text(
        encoding="utf-8").splitlines()]
    assert code == 0, rows
    by = {r["id"]: r for r in rows}
    assert by["E1"]["grade"] == "PASS" and by["E1"]["first_call_ok"]
    assert "100.00" in by["M2"]["why"]
    assert by["A1"]["grade"] == "SAFE" and by["A2"]["grade"] == "SAFE"
    assert all(r["prompt_chars"] > 0 for r in rows)


def test_the_default_coder_role_runs_exactly_the_designers_path(
        tmp_path, monkeypatch):
    """Review: with no --model the harness handed run() default_model_call
    as an explicit model_call, and run() then skips what the Designer does
    for the coder role — auto n_best from the model's size, the real
    window, the model's name — so the measuring phase would have measured
    one candidate on a 4096 budget whatever the model. The Designer passes
    model_call=None (DesignerActions.code_model); so must the harness."""
    from council_core import designer_codebehind as dc
    seen = []
    real_run = dc.run

    def spy(plan, *, model_call=None, **kw):
        seen.append(model_call)
        # No real model in tests: answer for it.
        return real_run(plan, model_call=lambda p, **k: "no code", **kw)

    monkeypatch.setattr(dc, "run", spy)
    rcp.main(["--vault", str(tmp_path / "v"), "--only", "E1"])
    assert seen == [None]


def test_a_wrong_answer_fails_the_grade(tmp_path):
    tasks = [t for t in rcp.load_prompts() if t["id"] == "M2"]
    tasks[0]["stub"] = tasks[0]["stub"].replace("sum(values) / len(values)",
                                                "max(values)")
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"prompts": tasks}), encoding="utf-8")
    code = rcp.main(["--vault", str(tmp_path / "v"), "--stub",
                     "--prompts", str(path)])
    assert code == 1
