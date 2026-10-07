"""Model loading: the per-question meter, the weekly report's swap figures,
grouping calls by model, and preloading while the user types."""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import council_engine as ce  # noqa: E402
from council_core import council_turn as ct  # noqa: E402
from council_core import usage_log  # noqa: E402
from council_core.turn_meter import TurnMeter  # noqa: E402


def test_the_meter_counts_calls_and_loads():
    with TurnMeter() as m:
        ce._record_stats("writer", {"seconds": 12.0, "load_s": 9.0,
                                    "model": "a"})
        ce._record_stats("peasant", {"seconds": 4.0, "load_s": 6.0,
                                     "model": "b"})
        ce._record_stats("writer", {"seconds": 10.0, "load_s": 8.0,
                                    "model": "a"})
        ce._record_stats("writer", {"seconds": 2.0, "load_s": 0.0,
                                    "model": "a"})
    r = m.result
    assert r.calls == 4 and r.loads == 3 and r.loaded == {"a": 2, "b": 1}
    assert r.swapping and "swapping" in r.note()
    ce._record_stats("writer", {"seconds": 1.0})       # after: not counted
    assert m.result.calls == 4


def test_loads_by_host_and_the_placement_report(tmp_path):
    from council_core import placement
    now = time.time()
    for model, load in (("a", 9.0), ("b", 7.0), ("a", 8.0), ("a", 0.1)):
        usage_log.record(tmp_path, {"role": "writer", "model": model,
                                    "backend": "ollama", "host": "local",
                                    "seconds": 10, "load_s": load}, now=now)
    calls = usage_log.read(tmp_path, now - 10)
    assert usage_log.loads_by_host(calls)["local"]["loads"] == 3
    report = placement.build_report(tmp_path, now=now + 1)
    text = report.text()
    assert "MODEL LOADING" in text and "a ×2, b ×1" in text
    assert "model loaded 2 times" in text
    assert "swapping" in placement.INSTRUCTIONS


class Persona(ce.PersonalityModel):
    """A real PersonalityModel type (so the orchestrator asks the engine
    where it runs) whose replies are canned."""

    def respond(self, prompt, **kw):
        self.prompts.append(prompt)
        if "CONFIDENCE: <0-100>%" in prompt:
            return f"{self.name} answer {len(self.prompts)}\nCONFIDENCE: 70% — x"
        if "NEW questions" in prompt or "For EACH" in prompt:
            return "Q1: a?\nQ2: b?"
        return f"{self.name} says {len(self.prompts)}"


def persona(name):
    p = Persona(name=name, system_prompt="", weights={}, registry=None,
                trace=False)
    p.prompts = []
    return p


class Judge:
    def route(self, text):
        return "intern"

    def rank_candidates(self, user_text, candidates, extra_context=""):
        return json.dumps({"winner": "writer", "confidence": 70,
                           "scores": {k: 70 for k in candidates}})

    def critique(self, user_text, response, *, extra_context="",
                 query_mode=""):
        return "Verdict: PASS"


def _models(**slots):
    m = type("Models", (), {r: None for r in ct.AGENT_NAMES})()
    for k, v in slots.items():
        setattr(m, k, v)
    return m


def test_the_peasant_waits_when_its_model_would_swap(monkeypatch):
    keys = {"writer": ("local", "A"), "intern": ("local", "A"),
            "peasant": ("local", "B")}
    monkeypatch.setattr(ce, "model_key", lambda role: keys.get(role, ("", "")))
    order = []
    w, i, p = persona("writer"), persona("intern"), persona("peasant")
    for who in (w, i, p):
        orig = who.respond

        def rec(prompt, _o=orig, _n=who.name, **kw):
            order.append(_n)
            return _o(prompt, **kw)
        who.respond = rec
    events = []
    ct.run_turn("q", _models(writer=w, intern=i, peasant=p), judge=Judge(),
                max_rounds=1, debate_turns=1, depth="standard",
                on_event=events.append)
    # Both drafts before any Peasant question: A, A, then B.
    first_peasant = order.index("peasant")
    assert order[:first_peasant].count("intern") >= 1
    assert order[:first_peasant].count("writer") >= 1
    assert any("Peasant questions held" in e.text for e in events)


def test_no_swap_no_wait(monkeypatch):
    monkeypatch.setattr(ce, "model_key", lambda role: ("local", "A"))
    events = []
    ct.run_turn("q", _models(writer=persona("writer"), intern=persona("intern"),
                             peasant=persona("peasant")),
                judge=Judge(), max_rounds=1, debate_turns=1,
                depth="standard", on_event=events.append)
    assert not any("Peasant questions held" in e.text for e in events)


def test_rebuttals_are_grouped_by_model():
    from council_core.deliberation import DeliberationOrchestrator
    o = DeliberationOrchestrator.__new__(DeliberationOrchestrator)
    keys = {"a": ("local", "X"), "b": ("local", "Y"), "c": ("local", "X")}
    o._model_of = lambda k: keys[k]
    assert o._group_by_model(["a", "b", "c"]) == ["a", "c", "b"]


def test_warm_never_warms_two_models_that_would_swap(monkeypatch):
    keys = {"writer": ("local", "ollama:a"), "judge": ("local", "ollama:b")}
    monkeypatch.setattr(ce, "model_key", lambda role: keys[role])
    monkeypatch.setattr(ce, "_target_for",
                        lambda slot, model=None: ("ollama", "a"))
    sent = []
    from council_core import local_models

    def fake_get(url, timeout, body=None):
        sent.append((url, body))
        return {"models": []} if url.endswith("/api/ps") else {}
    monkeypatch.setattr(local_models, "_get_json", fake_get)
    out = ce.warm(["writer", "judge"])
    assert any(b and b.get("model") == "a" for _u, b in sent)
    assert not any(b and b.get("model") == "b" for _u, b in sent)
    assert any("would swap" in line for line in out)


def test_the_preloader_runs_at_most_once_per_gap():
    from council_core.preload import Preloader
    calls = []
    gate = threading.Event()

    def warm(roles):
        calls.append(tuple(roles))
        gate.wait(2)
        return ["ok"]
    p = Preloader(warm, min_gap_s=60)
    assert p.kick(["writer"]) is True
    assert p.kick(["writer"]) is False          # running / too soon
    gate.set()
    p.join(2)
    assert calls == [("writer",)] and p.log == ["ok"]


def test_preload_is_off_with_the_switch(monkeypatch):
    from council_core.preload import Preloader
    monkeypatch.setenv("COUNCIL_PRELOAD", "0")
    assert Preloader(lambda r: []).kick() is False


def test_the_actions_preload_only_real_personalities(tmp_path):
    from types import SimpleNamespace as NS
    from council_qt.tabs.council import CouncilActions
    a = CouncilActions(vault_dir=tmp_path)
    assert a.preload() is False                      # nothing loaded yet
    a._models = NS(writer=object(), judge=object())
    assert a.preload() is False                      # stand-ins: never
