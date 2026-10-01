"""
council_core.docs_bench — the benchmark must be answerable, solvable and fair
before any model's score on it means anything.

  * every expected fact is really on an expected page (as served)
  * every reference solution passes its hidden test, and a wrong one fails
  * the answer key, played as a model, scores 100% — so a model's misses
    are the model's, not the harness's
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from council_core import docs_bench as db, docs_qa as qa  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "pydocs_mcp_server", ROOT / "tools" / "pydocs_mcp_server.py")
pd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pd)

BENCH = db.load_bench()


def test_the_shape_is_what_the_measurement_phase_expects():
    assert BENCH["package"] == "glimmerquay"
    assert len(BENCH["questions"]) == 10
    assert len(BENCH["code_tasks"]) == 5
    assert len(BENCH["negatives"]) >= 2
    ids = [x["id"] for k in ("questions", "negatives", "code_tasks")
           for x in BENCH[k]]
    assert len(ids) == len(set(ids))
    assert set(db.QUICK) <= set(ids)
    for t in BENCH["code_tasks"]:
        assert {"function", "task", "reference", "hidden_test",
                "sources"} <= set(t)


def test_the_package_is_unknown_to_this_environment():
    assert importlib.util.find_spec("glimmerquay") is None


@pytest.fixture(scope="module")
def index():
    return pd.build_index(pd.Finder([str(db.BENCH_DIR)]), "glimmerquay")


@pytest.mark.parametrize("item", BENCH["questions"],
                         ids=[q["id"] for q in BENCH["questions"]])
def test_every_fact_is_on_an_expected_page(index, item):
    pages = []
    for name in item["sources"]:
        e = index.lookup(name)
        assert e is not None, f"{name} is not a page the server serves"
        pages.append(pd.render_page(index, e))
    assert any(all(db.fact_found(f, p) for f in item["facts"])
               for p in pages), (item["id"], item["facts"])


@pytest.mark.parametrize("task", BENCH["code_tasks"],
                         ids=[t["id"] for t in BENCH["code_tasks"]])
def test_every_reference_solution_passes_its_hidden_test(task):
    result = db.run_code_test(task["reference"], task)
    assert result["passed"], result["output"]


def test_a_wrong_solution_fails():
    task = next(t for t in BENCH["code_tasks"] if t["id"] == "c02")
    wrong = task["reference"].replace('checksum="xor8", ', "")
    assert not db.run_code_test(wrong, task)["passed"]


def test_the_sandbox_blocks_a_delete_outside_it(tmp_path):
    victim = tmp_path / "keep.txt"
    victim.write_text("x", encoding="utf-8")
    task = next(t for t in BENCH["code_tasks"] if t["id"] == "c02")
    evil = (task["reference"] + f"\nimport os\nos.remove({str(victim)!r})\n")
    result = db.run_code_test(evil, task)
    assert not result["passed"]
    assert "blocked" in result["output"]
    assert victim.exists()


@pytest.mark.parametrize("fact,text,found", [
    ("64", "capacity is 64.", True), ("64", "capacity is 640", False),
    ("2750", "it is 2,750 ms", True), ("8.25", "gain 8.25x", True),
    ("8.25", "gain 18.25", False), ("GQ", "starts with b'GQ'", True),
    (["uV", "microvolt"], "microvolts too", False),
    (["uV", "microvolt"], "and `uV`", True), ("rotate", "rotates", False)])
def test_fact_matching(fact, text, found):
    assert db.fact_found(fact, text) is found


def test_the_answer_key_scores_full_marks():
    report = db.run(db.oracle_model_call(BENCH), items="all", bench=BENCH,
                    model_label="oracle")
    s = report.summary()
    assert report.passed == report.summary()["total"] == 17, report.lines()
    assert s["citations_right"] == "10/10"
    assert s["not_covered_right"] == f"{len(BENCH['negatives'])}/" \
                                     f"{len(BENCH['negatives'])}"
    assert report.good


def test_a_model_that_invents_fails_the_check():
    def liar(messages, *, json_schema=None, **kw):
        if "search queries" in messages[0]["content"]:
            return '{"queries": ["ledger"], "package": "glimmerquay"}'
        return ('{"answer": "It is 128 [1].", "sources": [1], '
                '"covered": true, "code": "def x():\\n    pass\\n"}')

    report = db.capability_check(liar, model_label="liar")
    assert report.items and not report.good
    assert report.passed == 0
    assert any("invented" in i.detail for i in report.items
               if i.kind == "negative")


def test_checks_are_remembered_newest_first(tmp_path):
    report = db.run(db.oracle_model_call(BENCH), items="quick", bench=BENCH,
                    model_label="oracle")
    db.record_check(report, tmp_path)
    report.model = "second"
    db.record_check(report, tmp_path)
    rows = db.load_checks(tmp_path)
    assert [r["model"] for r in rows] == ["second", "oracle"]
    assert rows[0]["passed"] == 5 and rows[0]["good"]


def test_the_cli_runs_the_oracle(tmp_path, capsys):
    out = tmp_path / "r.json"
    assert db.main(["--oracle", "--quick", "--out", str(out)]) == 0
    assert "oracle: 5/5 passed" in capsys.readouterr().out
    assert out.exists()
