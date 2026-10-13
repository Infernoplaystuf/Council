"""
Parallel members (DeliberationOrchestrator(parallel_members=True)).

Pinned: drafts and rebuttals really overlap; the drafts become INDEPENDENT
(one at a time, a later member reads the earlier answers; side by side, none
does) while the rebuttals are unchanged; events come out in panel order;
token streaming pauses while they overlap and comes back after; a member
that fails still fails the turn; off by default.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import council_turn as ct  # noqa: E402
from council_core.deliberation import (DeliberationOrchestrator,  # noqa: E402
                                       ModelAgent)
from tests.test_council_turn import FakeJudge  # noqa: E402

PANEL = ["coder", "sage", "strategist"]


class Meter:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def enter(self):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)

    def leave(self):
        with self.lock:
            self.active -= 1


class SlowModel:
    """Deterministic replies, a fixed delay, and a concurrency meter."""

    def __init__(self, name, meter, delay=0.15, fail=False):
        self.name, self.meter, self.delay, self.fail = name, meter, delay, fail
        self.saw_others = False

    def respond(self, prompt, token_callback=None, **kw):
        self.meter.enter()
        try:
            time.sleep(self.delay)
            if self.fail:
                raise RuntimeError(f"{self.name} broke")
            if "How confident are you" in prompt:
                text = "70"
            elif "Produce your rebuttal now" in prompt:
                text = f"{self.name} rebuts: keep it"
            else:
                if "CANDIDATE ANSWERS" in prompt and "Round 2" not in prompt:
                    self.saw_others = True
                text = f"{self.name} answers the question"
            if token_callback is not None:
                token_callback(text)
            return text
        finally:
            self.meter.leave()


class QuickModel:
    """The Peasant and the Writer: instant, two questions, a synthesis."""

    def respond(self, prompt, token_callback=None, **kw):
        if "Q1" in prompt or "question" in prompt.lower():
            text = "Q1: Why that? Q2: What if not?"
        else:
            text = "the synthesis"
        if token_callback is not None:
            token_callback(text)
        return text


def _run(parallel, fail=None, tokens=None):
    meter = Meter()
    cb = (lambda who, tok: tokens.append(who)) if tokens is not None else None
    agents = {k: ModelAgent(k.capitalize(),
                            SlowModel(k, meter, fail=(k == fail)),
                            token_callback=cb) for k in PANEL}
    agents["writer"] = ModelAgent("Writer", QuickModel())
    agents["peasant"] = ModelAgent("Peasant", QuickModel())
    live = []
    orch = DeliberationOrchestrator(judge_model=FakeJudge(), agents=agents,
                                    max_rounds=1, debate_turns=1,
                                    parallel_members=parallel,
                                    event_callback=live.append)
    _run.live = live
    t0 = time.monotonic()
    events = orch.run("How should we do it?", panel=list(PANEL),
                      synth="writer")
    return events, meter.peak, time.monotonic() - t0, agents, cb


def _content(events):
    return [(e.who, e.kind, e.text) for e in events if e.kind != "phase"]


def test_off_by_default():
    orch = DeliberationOrchestrator(judge_model=FakeJudge(), agents={})
    assert orch.parallel_members is False


def test_members_overlap_and_drafts_become_independent():
    seq, seq_peak, seq_s, seq_agents, _c = _run(False)
    par, par_peak, par_s, par_agents, _c = _run(True)
    assert seq_peak == 1
    assert par_peak == len(PANEL)
    # One at a time, later members read the earlier answers; side by side,
    # no draft sees another.
    assert [seq_agents[k].model.saw_others for k in PANEL] == \
        [False, True, True]
    assert not any(par_agents[k].model.saw_others for k in PANEL)
    # Same speakers, same order, same text (the fakes answer alike either
    # way) — so the panel order of the transcript is kept.
    assert _content(par) == _content(seq)
    # Drafts (answer + confidence) and rebuttals overlap: three members'
    # slow calls collapse into one member's worth.
    assert par_s < seq_s * 0.6, (par_s, seq_s)
    phases = [e.text for e in _run.live if e.kind == "phase"]
    assert any("3 members at once" in p for p in phases)


def test_streaming_pauses_while_members_overlap_and_comes_back():
    seq_tokens, par_tokens = [], []
    _run(False, tokens=seq_tokens)
    _e, _p, _s, agents, cb = _run(True, tokens=par_tokens)
    members = {k.capitalize() for k in PANEL}
    assert members <= set(seq_tokens)            # one at a time: streamed
    # Side by side: drafts and rebuttals are not streamed (cross-fire still
    # is, one member at a time).
    assert par_tokens.count("Coder") < seq_tokens.count("Coder")
    for k in PANEL:
        assert agents[k].token_callback is cb    # restored


def test_a_member_that_fails_still_fails_the_turn():
    with pytest.raises(RuntimeError, match="sage broke"):
        _run(True, fail="sage")


def test_run_turn_passes_the_switch_through(monkeypatch):
    seen = {}

    class Spy(DeliberationOrchestrator):
        def __init__(self, **kw):
            seen.update(kw)
            super().__init__(**kw)

    monkeypatch.setattr(ct, "DeliberationOrchestrator", Spy)

    class Models:
        pass

    models = Models()
    for name in ct.AGENT_NAMES:
        setattr(models, name, None)
    models.writer = QuickModel()
    models.judge = FakeJudge()
    ct.run_turn("why?", models, max_rounds=1, parallel_members=True)
    assert seen.get("parallel_members") is True


def test_the_switch_is_on_the_toolbar_and_off_by_default():
    from council_core import council_options as co
    assert co.SWITCHES_BY_KEY["parallel"].default is False
    assert co.CouncilOptions.defaults().parallel is False
