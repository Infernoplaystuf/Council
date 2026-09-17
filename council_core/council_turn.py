"""
council_core.council_turn — one question in, one answered turn out.

WHAT THIS IS FOR
`council_core.deliberation` holds the council itself, but a front end cannot
call it directly: it first has to build a dict of ModelAgents from the model
slots, ask the judge which route the question takes, turn that route into a
panel, run the orchestrator, and then dig the answer, the critique and the
confidence back out of a flat list of events. In the Tk shell that assembly is
written out three times — at three different call sites, with three slightly
different agent dicts — and one of them is 1,166 lines from top to bottom.

So it lives here once, and both front ends call `run_turn`.

THE EVENT LIST IS THE INTERFACE
`DeliberationOrchestrator.run` returns `List[AgentEvent]`, where an event is
(who, kind, text) and kind is thought / action / observation / final / token /
phase. That is exactly what a transcript needs and nothing more — no widget, no
thread, no front end. `run_turn` forwards each event to a callback as it
happens AND returns the digested result at the end, because a caller needs both:
the events to narrate the turn live, the result to decide what to do with it.

WHAT THIS DELIBERATELY DOES NOT DO
It does not touch the model slots' lifecycle, does not decide which thread it
runs on, and does not prompt. The caller supplies the models, runs it wherever
it likes, and renders what comes back.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .deliberation import AgentEvent, DeliberationOrchestrator, ModelAgent

#: role -> the display name the transcript shows. The display name is what a
#: user reads, so it is part of the product and not a label to tidy.
AGENT_NAMES = {
    "writer": "Writer",
    "peasant": "Peasant",
    "intern": "Intern",
    "coder": "Coder",
    "artist": "Artist",
    "skeptic": "Skeptic",
    "sage": "Sage",
    "strategist": "Strategist",
    "content": "Content",
    "director": "Director",
}

#: The five roles every build has. The rest are added only when the model slot
#: is actually populated — an agent backed by None raises on its first call, so
#: a panel that references one has to be filtered, not hoped about.
CORE_ROLES = ("writer", "peasant", "intern", "coder", "artist")

#: route -> (panel, synthesising role). Moved verbatim from the Tk engine.
PANEL_FOR_ROUTE: Dict[str, Tuple[List[str], str]] = {
    "chat":       (["writer",     "peasant"],                 "writer"),
    "writer":     (["writer",     "intern",  "peasant"],      "writer"),
    "ide":        (["coder",      "intern",  "skeptic"],      "writer"),
    "artist":     (["artist",     "writer",  "intern"],       "writer"),
    "intern":     (["intern",     "writer",  "peasant"],      "writer"),
    "coder":      (["coder",      "intern",  "skeptic"],      "writer"),
    "peasant":    (["writer",     "peasant"],                 "writer"),
    "sage":       (["sage",       "writer",  "peasant"],      "writer"),
    "strategist": (["strategist", "coder",   "skeptic"],      "writer"),
    "content":    (["content",    "writer",  "strategist"],   "writer"),
    "director":   (["director",   "writer",  "content"],      "writer"),
    "_default":   (["writer",     "intern",  "peasant"],      "writer"),
}

#: What a panel falls back to when the route names roles this build does not
#: have. Never empty: an empty panel makes the orchestrator produce nothing and
#: the user sees a turn that ends with no answer and no error.
FALLBACK_PANEL = ["intern", "coder", "artist"]


def panel_for_route(route: str) -> Tuple[List[str], str]:
    """(panel, synthesising role) for a judge route."""
    panel, synth = PANEL_FOR_ROUTE.get(route, PANEL_FOR_ROUTE["_default"])
    return list(panel), synth


def build_agents(models: Any, *,
                 token_callback: Optional[Callable[[str, str], None]] = None,
                 enable_tools: bool = False,
                 tools: Optional[Dict[str, Any]] = None) -> Dict[str, ModelAgent]:
    """A ModelAgent per populated model slot on ``models``.

    ``models`` is anything carrying the slots as attributes — the Tk console,
    the Qt actions object, or a stand-in in a test. A slot that is absent or
    None is skipped rather than wrapped, because an agent backed by None raises
    on its first call, deep inside a worker, with a message about the model
    rather than about the missing role.
    """
    agents: Dict[str, ModelAgent] = {}
    for role, display in AGENT_NAMES.items():
        model = getattr(models, role, None)
        if model is None and role == "sage":
            # The Sage is reached through a wrapper in some builds.
            wrapper = getattr(models, "sage_agent_obj", None)
            model = getattr(wrapper, "model", None)
        if model is None:
            continue
        agents[role] = ModelAgent(
            display, model,
            enable_tools=enable_tools and role in ("coder", "intern"),
            tools=tools if enable_tools else None,
            token_callback=token_callback)
    return agents


def usable_panel(panel: List[str], agents: Dict[str, ModelAgent]) -> List[str]:
    """The panel minus roles this build has no model for.

    The filter is why FALLBACK_PANEL exists: the `ide` route asks for a
    skeptic, and a build without one would otherwise deliberate with an empty
    panel and answer nothing.
    """
    kept = [role for role in panel if role in agents]
    if kept:
        return kept
    return [role for role in FALLBACK_PANEL if role in agents] or list(agents)[:1]


@dataclass
class TurnResult:
    """What one turn produced, dug out of the event list."""
    ok: bool
    answer: str = ""
    critique: str = ""
    verdict: str = ""
    confidence: int = 0
    route: str = ""
    panel: List[str] = field(default_factory=list)
    events: List[AgentEvent] = field(default_factory=list)
    message: str = ""
    error: Optional[BaseException] = None

    #: Identifies THIS turn's verdict, or "" when it produced none.
    #:
    #: A3 in docs/qt_migration/phase6_port_requirements.md: the Tk build shows
    #: the verdict bar after every turn — including a direct-mode answer that
    #: produced no verdict — and then records the user's agreement against the
    #: last line of verdict_history.jsonl, which belongs to an unrelated
    #: earlier deliberation. A turn that produced no verdict has to be able to
    #: say so, and an empty string is how it says it.
    verdict_id: str = ""


def final_answer(events: List[AgentEvent], synth: str = "writer") -> str:
    """The synthesised answer, last one wins."""
    want = AGENT_NAMES.get(synth, synth.title())
    return next((e.text for e in reversed(events)
                 if e.who == want and e.kind == "final"), "")


def judge_critique(events: List[AgentEvent]) -> str:
    return next((e.text for e in reversed(events)
                 if e.who == "Judge" and e.kind == "observation"), "")


def judge_confidence(events: List[AgentEvent]) -> int:
    """The judge's 0-10 confidence, or 0 when it did not say.

    Parsed defensively on purpose: it arrives as JSON inside a text event, and
    a malformed one must cost the confidence reading rather than the answer the
    user is waiting for.
    """
    import json

    raw = next((e.text.split("Ranking:\n", 1)[-1].strip()
                for e in reversed(events)
                if e.who == "Judge" and "confidence" in e.text), "")
    if not raw:
        return 0
    try:
        return int(json.loads(raw).get("confidence", 0))
    except Exception:                                     # noqa: BLE001
        return 0


def run_turn(question: str, models: Any, *,
             judge: Any = None,
             max_rounds: int = 2,
             debate_turns: int = 2,
             enable_tools: bool = False,
             tools: Optional[Dict[str, Any]] = None,
             on_event: Optional[Callable[[AgentEvent], None]] = None,
             on_token: Optional[Callable[[str, str], None]] = None,
             clarification_cb: Optional[Callable[[str, str], None]] = None,
             pause_event: Optional[threading.Event] = None,
             answer_getter: Optional[Callable[[], str]] = None,
             extra_ctx: Optional[Dict[str, Any]] = None) -> TurnResult:
    """Run one full deliberation and dig the result out of it.

    Never raises. A turn that fails comes back as ``ok=False`` with the reason
    in ``message`` — a front end mid-turn needs something to show the user more
    than it needs a traceback, and the exception is kept in ``error`` for
    whoever does want one.
    """
    question = (question or "").strip()
    if not question:
        return TurnResult(False, message="Ask something first.")

    judge = judge if judge is not None else getattr(models, "judge", None)
    if judge is None:
        return TurnResult(False, message="No judge model is loaded.")

    try:
        agents = build_agents(models, token_callback=on_token,
                              enable_tools=enable_tools, tools=tools)
        if not agents:
            return TurnResult(
                False,
                message="No personality models are loaded, so there is nobody "
                        "to deliberate. Check the Models tab.")

        route = ""
        try:
            route = judge.route(question) or ""
        except Exception:                                 # noqa: BLE001
            route = ""                 # an unroutable question still gets asked
        panel, synth = panel_for_route(route)
        panel = usable_panel(panel, agents)

        collected: List[AgentEvent] = []

        def collect(event: AgentEvent) -> None:
            collected.append(event)
            if on_event is not None:
                on_event(event)

        orchestrator = DeliberationOrchestrator(
            judge_model=judge, agents=agents,
            max_rounds=max_rounds, debate_turns=debate_turns,
            event_callback=collect, clarification_cb=clarification_cb,
            pause_event=pause_event, answer_getter=answer_getter)
        events = orchestrator.run(question, panel=panel, synth=synth,
                                  extra_ctx=extra_ctx) or collected

        critique = judge_critique(events)
        verdict = "PASS" if "Verdict: PASS" in critique else "NEEDS_WORK"
        answer = final_answer(events, synth)
        result = TurnResult(
            True, answer=answer, critique=critique, verdict=verdict,
            confidence=judge_confidence(events), route=route, panel=panel,
            events=list(events))
        # A verdict id only when there IS a verdict. See TurnResult.verdict_id.
        if critique:
            result.verdict_id = f"{route or 'turn'}:{len(events)}:{verdict}"
        return result
    except Exception as exc:                              # noqa: BLE001
        return TurnResult(False, message=f"The turn failed: {exc!r}", error=exc)
