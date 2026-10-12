"""Which members help, by route, and panels on one model
(council_core/verdict_log.py, the placement report, the role cards)."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from council_core import verdict_log as vl  # noqa: E402
from council_core.deliberation import AgentEvent  # noqa: E402

FINAL = "Use the EPDM seal rated for 3 bar; check the gasket torque."


def _result(winner="writer"):
    rank = json.dumps({"winner": winner, "scores": {"writer": 80,
                                                    "skeptic": 40}})
    return NS(route="ide", depth="deep", panel=["coder", "skeptic"],
              synth="writer", winner=winner, verdict="PASS", confidence=80,
              answer=FINAL, meter={"calls": 9},
              events=[AgentEvent("Judge", "observation", "Ranking:\n" + rank)])


CANDS = {"writer": {"answer": "EPDM seal rated 3 bar is right", "rebuttal": "",
                    "self_confidence": 80},
         "skeptic": {"answer": "Pumps explode when overheated badly",
                     "rebuttal": "", "self_confidence": 60}}


def test_influence():
    assert vl.influence("EPDM seal rated bar", FINAL) == 1.0
    assert vl.influence("explode overheated", FINAL) == 0.0


def test_entry_keeps_numbers_not_text(tmp_path):
    line = vl.entry(_result(), CANDS)
    assert line["winner"] == "writer" and line["calls"] == 9
    assert line["members"]["skeptic"]["score"] == 40
    assert line["members"]["writer"]["influence"] > \
        line["members"]["skeptic"]["influence"]
    assert FINAL not in json.dumps(line)


def test_summary_flags_a_member_that_never_helps(tmp_path):
    now = time.time()
    for i in range(6):
        vl.record(tmp_path, vl.entry(_result(), CANDS, now=now - 10 + i))
    rows = vl.summarise(vl.read(tmp_path, now - 60))
    sk = next(r for r in rows if r["role"] == "skeptic")
    assert sk["questions"] == 6 and sk["wins"] == 0
    assert any("never wins" in line for line in vl.summary_lines(rows))


def test_shared_models():
    panels = {"ide": (["coder", "intern", "skeptic"], "writer"),
              "chat": (["writer", "peasant"], "writer")}
    out = vl.shared_models({"coder": "llama3.1:8b", "intern": "llama3.1:8b",
                            "skeptic": "phi4"}, panels)
    assert out == [("ide", "llama3.1:8b", ["coder", "intern"])]


def test_the_weekly_report_shows_both(tmp_path):
    from council_core import placement
    now = time.time()
    for i in range(6):
        vl.record(tmp_path, vl.entry(_result(), CANDS, now=now - 100 + i))
    report = placement.build_report(tmp_path, now=now)
    report.mix = vl.shared_models({"coder": "a", "intern": "a"},
                                  {"ide": (["coder", "intern"], "writer")})
    text = report.text()
    assert "WHO HELPS" in text and "skeptic on 6 question(s)" in text
    assert "MODEL MIX" in text and "coder, intern all answer with a" in text


def test_the_role_card_warns_about_a_shared_model():
    from council_core import role_specs
    rm = {r: "llama3.1:8b" for r in ("coder", "intern", "skeptic", "writer",
                                     "peasant", "artist", "sage",
                                     "strategist")}
    a = next(x for x in role_specs.assess(rm) if x.spec.role == "coder")
    note = next(n for n in a.notes if n.startswith("Shares llama3.1:8b"))
    assert "Llama" not in note.split("family (")[1]
    assert "Gemma" in note


def test_a_council_question_is_recorded(tmp_path):
    from council_core import council_options
    from council_core import council_turn as ct
    from council_core import vault_context as vc
    from council_qt.tabs.council import CouncilActions
    from tests.test_council_turn import FakeJudge, FakeModel

    actions = CouncilActions(vault_dir=tmp_path)
    models = NS(**{n: None for n in ct.AGENT_NAMES})
    models.writer, models.peasant = FakeModel("a"), FakeModel("b")
    models.judge = FakeJudge()
    actions._models = models
    actions.memo_llm = None
    actions.vault_brief = lambda q: vc.Brief()
    opts = council_options.CouncilOptions.defaults()
    opts.depth = "deep"
    assert actions.send("which pump for the line?", opts).ok
    lines = vl.read(tmp_path, 0)
    assert len(lines) == 1 and set(lines[0]["members"]) == {"writer",
                                                             "peasant"}
