"""Structured replies (council_core/council_schemas.py), per-step thinking
(council_engine.think_level) and native tool calls (ModelAgent.act)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import council_engine as ce  # noqa: E402
from council_core import council_schemas as cs  # noqa: E402
from council_core.deliberation import AgentContext, ModelAgent  # noqa: E402


# ============================================================
# Rendering the structured replies back into the classic text
# ============================================================

def test_a_json_critique_renders_into_the_text_the_council_reads():
    raw = json.dumps({"verdict": "NEEDS_WORK", "findings": ["no units"],
                      "suggestions": ["cite"],
                      "required_changes": ["State units (kg)", "Cite p.4"]})
    text = cs.render_critique(raw)
    assert "Verdict: NEEDS_WORK" in text and "- no units" in text
    assert ce.JudgeModel.parse_required_changes(text) == [
        "State units (kg)", "Cite p.4"]
    ok = cs.render_critique(json.dumps({"verdict": "PASS", "findings": [],
                                        "required_changes": []}))
    assert "Verdict: PASS" in ok and "REQUIRED_CHANGES" not in ok
    assert cs.render_critique("Verdict: PASS\nfree text") == \
        "Verdict: PASS\nfree text"


def test_questions_render_with_labels():
    assert cs.render_questions('{"q1": "Which seal", "q2": "Why 3 bar?"}') \
        == "Q1: Which seal?\nQ2: Why 3 bar?"
    turn = cs.render_turn_questions(
        json.dumps({"coder": {"q1": "a?", "q2": "b?"},
                    "intern": {"q1": "c?", "q2": "d?"}}), ["coder", "intern"])
    assert turn == "TO CODER:\nQ1: a?\nQ2: b?\nTO INTERN:\nQ1: c?\nQ2: d?"
    assert cs.render_questions("Q1: x?\nQ2: y?") == "Q1: x?\nQ2: y?"


def test_the_ranking_schema_names_the_candidates():
    sch = cs.ranking_schema(["coder", "writer"])
    assert sch["properties"]["winner"]["enum"] == ["coder", "writer"]
    assert sch["properties"]["scores"]["required"] == ["coder", "writer"]
    assert sch["properties"]["confidence"]["maximum"] == 100


# ============================================================
# The Judge asks for structure and thinks hard
# ============================================================

def _judge(monkeypatch, reply):
    seen = {}

    def respond(self, prompt, **kw):
        seen.update(kw, prompt=prompt)
        return reply
    monkeypatch.setattr(ce.PersonalityModel, "respond", respond)
    j = ce.JudgeModel.__new__(ce.JudgeModel)
    j.name = "judge"
    return j, seen


def test_the_ranking_is_constrained_and_high_effort(monkeypatch):
    j, seen = _judge(monkeypatch, json.dumps(
        {"winner": "coder", "scores": {"coder": 80}, "rationale": "r",
         "confidence": 77}))
    out = json.loads(j.rank_candidates("q", {"coder": {"answer": "a"}}))
    assert seen["json_schema"]["properties"]["winner"]["enum"] == ["coder"]
    assert seen["think"] == "high" and out["confidence"] == 77


def test_the_critique_is_constrained_and_rendered(monkeypatch):
    j, seen = _judge(monkeypatch, json.dumps(
        {"verdict": "NEEDS_WORK", "findings": ["f"],
         "required_changes": ["Add a test"]}))
    text = j.critique("q", "answer")
    assert seen["json_schema"] is cs.CRITIQUE_SCHEMA
    assert seen["think"] == "high"
    assert "Verdict: NEEDS_WORK" in text and "- Add a test" in text


# ============================================================
# Thinking per step
# ============================================================

GPT_OSS = {"name": "gpt-oss:20b", "family": "gptoss",
           "capabilities": ["completion", "thinking", "tools"]}
LLAMA = {"name": "llama3.1:8b", "family": "llama",
         "capabilities": ["completion", "tools"]}


def test_the_step_sets_the_thinking_level(monkeypatch):
    monkeypatch.delenv("COUNCIL_OLLAMA_THINK", raising=False)
    assert ce.ollama_think(GPT_OSS) == "low"
    with ce.think_level("high"):
        assert ce.ollama_think(GPT_OSS) == "high"
    with ce.think_level("medium"):
        assert ce.ollama_think(GPT_OSS) == "medium"
    assert ce.ollama_think(GPT_OSS) == "low"
    assert ce.ollama_think(LLAMA) is None        # does not think: nothing sent
    monkeypatch.setenv("COUNCIL_OLLAMA_THINK", "medium")
    with ce.think_level("high"):
        assert ce.ollama_think(GPT_OSS) == "medium"   # the user's word wins


def test_more_thinking_gets_more_room():
    assert ce._think_headroom("high") > ce._think_headroom("medium") > \
        ce._think_headroom("low")


def test_respond_passes_the_schema_and_the_level(monkeypatch):
    seen = {}

    class Spec:
        def generate(self, **kw):
            seen.update(kw, level=ce._THINK_LEVEL.get())
            return "ok"

    class Registry:
        def get(self, key):
            return Spec()

        def best_for(self, **kw):
            return Spec()

    m = ce.PersonalityModel(name="peasant", system_prompt="s", weights={},
                            registry=Registry(), trace=False)
    assert m.respond("hi", json_schema={"type": "object"}, think="low") == "ok"
    assert seen["json_schema"] == {"type": "object"} and seen["level"] == "low"
    assert ce._THINK_LEVEL.get() is None             # reset after the call
    m.respond("hi")
    assert "json_schema" not in seen or seen["json_schema"] == {"type": "object"}


def test_a_stand_in_without_the_new_arguments_still_works():
    class Old:
        def respond(self, prompt, token_callback=None):
            return "plain"
    events = ModelAgent("Writer", Old()).act(AgentContext("q"), think="high")
    assert events[-1].text == "plain"


# ============================================================
# Tool calls
# ============================================================

def test_the_follow_up_after_a_tool_still_has_the_question():
    class M:
        def __init__(self):
            self.asked = []

        def respond(self, prompt, **kw):
            self.asked.append(prompt)
            if len(self.asked) == 1:
                return '{"tool": "calc", "args": {"expr": "2+2"}}'
            return "4"
    m = M()
    agent = ModelAgent("Intern", m, enable_tools=True,
                       tools={"calc": lambda a: (True, "2+2 = 4", {})})
    agent.act(AgentContext("what is two plus two, exactly?"))
    assert "what is two plus two, exactly?" in m.asked[1]
    assert "2+2 = 4" in m.asked[1]


def test_native_tool_calls_are_used_when_the_model_has_them(monkeypatch):
    monkeypatch.setattr(ce, "native_tools", lambda role: True)

    class M:
        name = "intern"

        def __init__(self):
            self.calls = []

        def respond(self, prompt, **kw):
            raise AssertionError("the text path should not be used")

        def respond_with_tools(self, prompt, tools, **kw):
            self.calls.append((prompt, [t["name"] for t in tools]))
            if len(self.calls) == 1:
                return {"content": "", "tool_calls": [
                    {"name": "calc", "arguments": {"expr": "17*23"}}]}
            return {"content": "It is 391.", "tool_calls": []}

    from council_core import tool_kit
    m = M()
    agent = ModelAgent("Intern", m, enable_tools=True,
                       tools={"calc": tool_kit.calc})
    events = agent.act(AgentContext("17 times 23?"))
    assert events[-1].text == "It is 391."
    assert m.calls[0][1] == ["calc"]
    assert "17*23 = 391" in m.calls[1][0]


def test_native_failure_falls_back_to_text(monkeypatch):
    monkeypatch.setattr(ce, "native_tools", lambda role: True)

    class M:
        name = "intern"

        def respond(self, prompt, **kw):
            return "text answer"

        def respond_with_tools(self, prompt, tools, **kw):
            raise NotImplementedError("no model")

    from council_core import tool_kit
    agent = ModelAgent("Intern", M(), enable_tools=True,
                       tools={"calc": tool_kit.calc})
    assert agent.act(AgentContext("q"))[-1].text == "text answer"


def test_every_tool_has_a_schema():
    from council_core import council_tools, tool_kit
    tools = council_tools.make_tools(None, None, Path("."))
    specs = tool_kit.tool_specs(tools)
    assert len(specs) == len(tools)
    assert all(s["parameters"]["type"] == "object" for s in specs)
