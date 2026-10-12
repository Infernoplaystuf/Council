"""What the Judge reads of each candidate (council_core/judge_view.py)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from council_core import judge_view as jv  # noqa: E402


def test_the_budget_grows_with_the_window_and_shrinks_with_candidates():
    assert jv.answer_budget(4096, 3) == jv.MIN_CHARS
    big = jv.answer_budget(32768, 3)
    assert big > 2000 * 5 and big <= jv.MAX_CHARS
    assert jv.answer_budget(32768, 6) < big


def test_a_long_answer_keeps_its_end():
    body = "\n".join(f"line {i} of setup" for i in range(1500))
    text = "START\n" + body + "\nthe bug is in parse_header\nEND"
    out = jv.fit(text, 3000, "where is the bug in parse_header")
    assert len(out) <= 3300
    assert "START" in out[:300] and out.rstrip().endswith("END")
    assert "elided" in out           # the Judge is told it is partial
    assert "parse_header" in out


def test_code_check():
    ans = ("Here:\n```python\nimport os\ndef f():\n    return x\n```\n"
           "and\n```python\nprint(1)\n```\nand ```bash\nls\n```")
    chk = jv.code_check(ans)
    assert chk.startswith("CODE CHECK")
    assert "block 1" in chk and "undefined name 'x'" in chk
    assert "block 2: no syntax errors" in chk
    assert jv.has_problems(chk)
    assert jv.code_check("no code here") == ""
    assert not jv.has_problems(jv.code_check("```python\nprint(1)\n```"))


def test_the_engine_judge_reads_the_whole_long_answer(monkeypatch):
    import council_engine
    seen = {}

    def respond(self, prompt, **kw):
        seen["prompt"] = prompt
        return json.dumps({"winner": "coder", "scores": {"coder": 70},
                           "confidence": 70})
    monkeypatch.setattr(council_engine.PersonalityModel, "respond", respond)
    monkeypatch.setattr(council_engine, "effective_n_ctx", lambda s="": 32768)
    judge = council_engine.JudgeModel.__new__(council_engine.JudgeModel)
    judge.name = "judge"
    tail = "\n".join(f"    step_{i}()" for i in range(300))
    answer = "```python\ndef main():\n" + tail + "\n    final_bug()\n```"
    assert len(answer) > 4000
    out = json.loads(judge.rank_candidates(
        "fix it", {"coder": {"answer": answer,
                             "code_check": "CODE CHECK (parsed, not run): x"}}))
    assert "final_bug()" in seen["prompt"]
    assert "CODE CHECK (parsed, not run): x" in seen["prompt"]
    assert out["confidence"] == 70
