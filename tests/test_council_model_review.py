"""tools/council_model_review.py — reading results and writing the review (no
model: the runs themselves need real ones, on the user's PC)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import council_model_review as mr  # noqa: E402


def make_results(folder: Path, right: int = 8, wall: float = 10.0,
                 solved: int = 3, grade: str = "PASS",
                 spill: str = "") -> Path:
    (folder / "ab").mkdir(parents=True)
    (folder / "build").mkdir()
    chat = [{"id": f"q{i}", "kind": "math", "correct": i < right,
             "wall_s": wall, "answer": "x", "error": ""} for i in range(10)]
    (folder / "ab" / "chat-after-app.jsonl").write_text(
        "\n".join(json.dumps(q) for q in chat), encoding="utf-8")
    (folder / "ab" / "kg-after.json").write_text(
        json.dumps({"f1": 0.9}), encoding="utf-8")
    (folder / "build" / "build_bench.json").write_text(json.dumps(
        [{"id": f"p{i}", "solved": i < solved} for i in range(4)]),
        encoding="utf-8")
    (folder / "docs_bench.json").write_text(json.dumps(
        {"summary": {"passed": 19, "total": 19}}), encoding="utf-8")
    (folder / "llm_bench.json").write_text(json.dumps(
        {"code": {"summary": [{"passed": 7, "graded": 8}]},
         "gui": {"summary": [{"gen_tok_s": 90.0}]}}), encoding="utf-8")
    (folder / "describe.jsonl").write_text("\n".join(json.dumps(
        {"id": i, "name": "Prompt injection" if i == "A2" else i,
         "tier": "adversarial" if i == "A2" else "easy",
         "grade": grade if i == "A2" else "PASS"}) for i in ("E1", "A2")),
        encoding="utf-8")
    (folder / "codebehind.jsonl").write_text(json.dumps(
        {"id": "E1", "tier": "easy", "grade": "PASS"}), encoding="utf-8")
    (folder / "usage.csv").write_text(
        "step,seconds,gpu_pct,vram_mib,gpu_w,cpu_pct,ram_gb\n"
        "chat,3600,80,5120,300,10,20\nbenches,2,0,1024,25,2,18\n",
        encoding="utf-8")
    if spill:
        (folder / "splits.json").write_text(json.dumps([spill]),
                                            encoding="utf-8")
    (folder / "done.json").write_text("{}", encoding="utf-8")
    return folder


def test_the_list_covers_the_families_at_several_sizes():
    sizes = {}
    for family, _maker, _based, tag in mr.MATRIX:
        sizes.setdefault(family, []).append(tag)
    for family in ("Phi", "Granite", "Gemma", "Llama"):
        assert len(sizes[family]) >= 2, family
    tags = [m[3] for m in mr.MATRIX]
    assert len(tags) == len(set(tags))


def test_ollama_list_is_read_in_gb():
    text = ("NAME  ID  SIZE  MODIFIED\n"
            "phi4:14b  abc  9.1 GB  8 months ago\n"
            "nomic-embed-text:latest  def  274 MB  2 days ago\n")
    assert mr.parse_ollama_list(text) == {
        "phi4:14b": 9.1, "nomic-embed-text:latest": 0.274}


def test_collect_reads_every_score(tmp_path):
    r = mr.collect(make_results(tmp_path / "m"), "phi4:14b")
    assert r["family"] == "Phi" and r["based"] == "US"
    assert r["chat"]["right"] == 8 and r["chat"]["total"] == 10
    assert r["chat"]["per_right"] == 100 / 8
    assert r["chat"]["wrong"] == ["q8", "q9"]
    s = r["scores"]
    assert (s["chat"], s["build"], s["docs"]) == (0.8, 0.75, 1.0)
    assert s["coding"] == 7 / 8 and s["connections"] == 0.9
    assert r["usage"]["vram_gb"] == 5.0 and r["usage"]["chat_wh"] == 300
    assert r["tok_s"] == 90.0 and r["done"]


def test_a_test_that_did_not_run_is_not_a_zero(tmp_path):
    folder = make_results(tmp_path / "m")
    (folder / "build" / "build_bench.json").unlink()
    r = mr.collect(folder, "phi4:14b")
    assert "build" not in r["scores"]
    assert r["missing"] == ["Fixing code (Build tab)"]
    assert r["overall"] > 0.8
    text = mr.review([r], {})
    assert "not run" in text and "Not measured: Fixing code" in text


def test_strengths_and_weaknesses_come_from_the_results(tmp_path):
    strong = mr.collect(make_results(tmp_path / "a", right=9, solved=4),
                        "gemma3:12b")
    weak = mr.collect(make_results(
        tmp_path / "b", right=3, wall=90, solved=0, grade="FAIL",
        spill="16%/84% CPU/GPU"), "gemma3:1b")
    middle = mr.collect(make_results(tmp_path / "c", right=7, wall=20),
                        "llama3.1:8b")
    rows = [strong, weak, middle]
    got = mr.judge(strong, rows)
    assert any(x.startswith("Fixing code") and "best tested" in x
               for x in got["good"])
    assert any(x.startswith("Fast") for x in got["good"])
    got = mr.judge(weak, rows)
    assert any(x.startswith("Fixing code (Build tab): 0%") for x in got["bad"])
    assert any("Does not fit on the GPU: Ollama ran it 16%/84%" in x
               for x in got["bad"])
    assert any("Prompt injection" in x for x in got["bad"])
    assert any(x.startswith("Slow") for x in got["bad"])
    assert any("3 of 10 maths" in x for x in got["bad"])


def test_failed_turns_and_blank_answers_are_called_out(tmp_path):
    folder = make_results(tmp_path / "m")
    rows = mr.read_jsonl(folder / "ab" / "chat-after-app.jsonl")
    rows[0].update(error="boom", answer="")
    rows[1].update(answer="  ")
    (folder / "ab" / "chat-after-app.jsonl").write_text(
        "\n".join(json.dumps(q) for q in rows), encoding="utf-8")
    r = mr.collect(folder, "phi4:14b")
    assert r["chat"]["errors"] == 1 and r["chat"]["blank"] == 1
    bad = mr.judge(r, [r])["bad"]
    assert any("1 chat turns failed" in x for x in bad)
    assert any("1 chat answers came back blank" in x for x in bad)


def test_the_review_is_written_with_a_section_per_family(tmp_path):
    make_results(tmp_path / mr.folder_name("gemma3:12b"))
    make_results(tmp_path / mr.folder_name("phi4:14b"), right=9)
    meta = {"when": "now", "build": "abc", "repeat": 2, "planned": 3,
            "sizes": {"phi4:14b": 9.1},
            "skipped": {"llama3.1:8b": "not installed"}}
    text = mr.write_review(
        tmp_path, ["gemma3:12b", "phi4:14b", "llama3.1:8b"], meta)
    assert "Models tested: 2 of 3" in text
    assert "### Gemma (Google, US)" in text and "### Phi (Microsoft, US)" in text
    assert "**phi4:14b**" in text and "9.1 GB" in text
    assert "- llama3.1:8b: not installed" in text
    page = (tmp_path / "review.html").read_text(encoding="utf-8")
    assert (tmp_path / "review.md").read_text(encoding="utf-8") == text
    assert "<table>" in page and "<h3>Phi (Microsoft, US)</h3>" in page
    assert "<b>phi4:14b</b>" in page and "|" not in page.split("</style>")[1]


def test_no_results_yet_is_said_plainly():
    assert "No results yet." in mr.review([], {"planned": 15})


def test_a_family_is_listed_smallest_first(tmp_path):
    rows = [mr.collect(make_results(tmp_path / t), t)
            for t in ("phi4:14b", "phi4-mini:latest")]
    text = mr.review(rows, {"sizes": {"phi4:14b": 9.1,
                                      "phi4-mini:latest": 2.5}})
    assert "By size: phi4-mini:latest" in text
    assert text.index("**phi4-mini:latest**") < text.index("**phi4:14b**")
