"""
council_core.deliberation — the council itself.

THE LUCKIEST THING IN THIS PORT
Everything in this file was already OUTSIDE `CouncilConsole`. `ModelAgent` and
`DeliberationOrchestrator` are module-level classes in council_gui_engine.py
and touch no widget, no Tk variable and no scheduling — so the deliberation,
which the estimate treated as the deepest part of the Council tab, needed
moving rather than rewriting. 825 lines, moved verbatim by script.

WHAT IT IS
`DeliberationOrchestrator.run()` takes a question and a panel of roles, runs
them for a bounded number of rounds, has a judge critique the synthesis, and
returns a list of `AgentEvent`s. An event is (who, kind, text) where kind is
one of thought / action / observation / final / token / phase — which is
exactly what a front end needs to render a turn, and nothing more. Neither
front end is mentioned anywhere in here.

Progress reaches the caller through callbacks it supplies (`event_callback`,
`token_callback`, `clarification_cb`), and the caller decides which thread
they land on. That is the same contract the rest of council_core uses.

THERE IS A SECOND, SMALLER IMPLEMENTATION IN THE REPO AND IT IS NOT THIS ONE
`agent_core.py` also defines `ModelAgent` and `DeliberationOrchestrator`, and
`council_agents.py` imports from it. They have diverged hard: agent_core's
orchestrator is 38 lines against this one's 449, and 6% of its lines match.
Nothing imports `council_agents.py`, so the agent_core pair is not what runs —
this is. Recorded rather than resolved: per the standing correction about the
"dead" modules, a module nothing imports on THIS branch may well be another
branch's feature, and merging or deleting either one is not a decision to make
while porting a GUI.

KNOWN DEFECTS, PRESERVED AND PINNED
Two confirmed defects live in here and are deliberately NOT fixed in this
commit, because moving code and changing it in the same step makes both
unreviewable:

  * `for r in range(self.max_rounds)` materialises the range once, so the
    escalation `self.max_rounds = 3` cannot lengthen the loop — while the
    phase message tells the user an extra round is being added. It also
    corrupts the `Round {r+1}/{self.max_rounds}` counter for any round that
    does run, so a two-round run prints "Round 2/3".
  * `parse_required_changes` is skipped exactly when confidence is lowest,
    because the `else` belongs to the low-confidence branch.

Both are A7 and A8 in docs/qt_migration/phase6_port_requirements.md, and there
are tests here that assert the CURRENT behaviour so the fix is a visible,
separate change rather than a silent one.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass
class AgentEvent:
    who: str
    kind: str   # "thought" | "action" | "observation" | "final" | "token" | "phase"
    text: str

@dataclass
class AgentContext:
    user_text: str
    shared: Dict[str, Any] = field(default_factory=dict)

ToolFn = Callable[[Dict[str, Any]], Tuple[bool, str, Dict[str, Any]]]

_TOOL_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

def _extract_tool_calls(text: str) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []
    for m in _TOOL_JSON_RE.finditer(text):
        blob = m.group(0).strip()
        try:
            obj = json.loads(blob)
        except Exception:
            continue
        if isinstance(obj, dict) and "tool" in obj:
            calls.append({"tool": obj.get("tool"), "args": obj.get("args", {})})
        elif isinstance(obj, dict) and "tool_calls" in obj and isinstance(obj["tool_calls"], list):
            for tc in obj["tool_calls"]:
                if isinstance(tc, dict) and "tool" in tc:
                    calls.append({"tool": tc.get("tool"), "args": tc.get("args", {})})
    return calls

def _strip_code_blocks(text: str) -> str:
    """Remove all fenced code blocks from a response."""
    # Remove triple-backtick blocks (with or without language tag)
    cleaned = re.sub(r"```[\w]*\n?[\s\S]*?```", "", text, flags=re.MULTILINE)
    # Remove lines that are just indented code (4-space indent used as code)
    # Only remove if 3+ consecutive indented lines (a real code block, not a quote)
    lines = cleaned.split("\n")
    out, run = [], 0
    for line in lines:
        if line.startswith("    ") and line.strip():
            run += 1
        else:
            if run >= 3:
                # drop the accumulated indented block
                for _ in range(run):
                    if out:
                        out.pop()
            run = 0
            out.append(line)
    if run < 3:
        pass  # trailing indented block was short, already in out via append
    cleaned = "\n".join(out)
    # Collapse 3+ blank lines to 2
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned

def _detect_user_question(text: str) -> str:
    """
    Check if a personality response contains a direct question aimed at the user
    (not rhetorical, not Peasant-style cross-examination).
    Returns the question sentence if found, empty string otherwise.
    """
    import re as _re
    # Look for lines that end with ? and contain user-directed phrasing
    _user_markers = [
        "could you", "can you", "would you", "do you", "what is your",
        "what are your", "please clarify", "please provide", "i need to know",
        "could you clarify", "could you provide", "could you tell",
        "what do you mean", "what exactly", "which do you prefer",
        "which would you", "how do you want", "what would you like",
        "do you have", "do you want", "are you looking for",
    ]
    sentences = re.split(r"(?<=[.!?])\s+", text)
    for s in sentences:
        s_low = s.lower().strip()
        if s.strip().endswith("?") and any(m in s_low for m in _user_markers):
            return s.strip()
    return ""

def peasant_cross_exam(
    peasant_model,
    *,
    candidate_role: str,
    candidate_text: str,
    user_text: str,
    prior_qa: Optional[List[Dict[str, str]]] = None,
    query_mode: str = "",
) -> str:
    """
    Cross-examine a candidate response as the Peasant.

    Passes the code/answer as extra_context so the full Peasant system
    prompt applies — meaning questions are specific to THIS code, not generic.

    prior_qa is a list of {"q": "...", "a": "..."} dicts accumulated across
    all earlier Peasant turns in this deliberation.  When present they are
    injected as a hard DO-NOT-REPEAT block so the Peasant cannot recycle
    questions that have already been asked and answered.
    """
    has_code = any(marker in candidate_text for marker in
                   ["def ", "class ", "import ", "```", "for ", "while ", "if "])
    content_label = "CODE" if has_code else "RESPONSE"

    parts = [
        f"ORIGINAL REQUEST:\n{user_text}\n",
        f"{candidate_role.upper()} {content_label} TO REVIEW:\n{candidate_text}\n",
    ]

    if prior_qa:
        lines = [
            "━━━ QUESTIONS YOU HAVE ALREADY ASKED THIS SESSION ━━━",
            "Do NOT ask any of these again, even in paraphrased form.",
            "Do NOT ask questions whose answers are already contained below.",
            "",
        ]
        for i, item in enumerate(prior_qa, 1):
            lines.append(f"[{i}] Q: {item['q']}")
            if item.get("a"):
                lines.append(f"    A: {item['a']}")
            lines.append("")
        lines.append("━━━ END OF PRIOR Q&A ━━━")
        parts.append("\n".join(lines))

    _mode_instruction = ""
    if query_mode == "conversational":
        _mode_instruction = (
            "⚠ CONVERSATIONAL MODE: The user asked a conversational question, not for code.\n"
            "Do NOT ask about error handling, imports, types, or code structure.\n"
            "Ask whether the explanation is accurate, clear, complete, and actually answers "
            "what the user asked.\n\n"
        )
    elif query_mode == "technical":
        _mode_instruction = (
            "⚠ TECHNICAL MODE: Focus on code correctness, edge cases, and robustness.\n\n"
        )
    if _mode_instruction:
        parts.append(_mode_instruction)

    parts.append(
        "Your task: identify NEW specific problems, edge cases, or dangerous assumptions "
        "in the above that have NOT already been raised. Ask questions tied to specific "
        "lines, variable names, or behaviours you can see in this exact code — "
        "not generic questions, and not anything already covered above."
    )

    extra_context = "\n\n".join(parts)

    prompt = (
        f"Review the {candidate_role} {content_label.lower()} above and ask your NEW questions now. "
        "Every question must reference something specific you can see in that code, "
        "and must not duplicate any question from the prior Q&A list above."
    )

    return peasant_model.respond(prompt, extra_context=extra_context)

def _looks_like_two_questions(text: str) -> bool:
    """
    Returns True if the Peasant response looks like it followed the format.
    Accepts either the old Q1/Q2 format or the new format with DANGEROUS ASSUMPTION.
    """
    t = text.lower()
    has_questions = ("q1:" in t) and ("q2:" in t)
    # Also accept if model gave specific feedback even without strict Q1/Q2 labels
    has_question_marks = t.count("?") >= 2
    return has_questions or (has_question_marks and len(text) > 80)

def _peasant_quality_score(
    text: str,
    candidate_text: str,
    prior_qa: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """
    Score Peasant output on four axes; return dict with 0-4 total.

    Axes:
      questions  -- at least 2 question marks present
      length     -- at least 80 chars
      specific   -- references at least 3 words from the candidate text
      non_repeat -- no prior question shares >60% 4-gram overlap
    """
    import re as _re
    scores: Dict[str, bool] = {}

    scores["questions"] = text.count("?") >= 2
    scores["length"]    = len(text.strip()) >= 80

    cand_words = set(w.lower() for w in re.findall(r"\w{4,}", candidate_text))
    resp_words = set(w.lower() for w in re.findall(r"\w{4,}", text))
    scores["specific"] = len(cand_words & resp_words) >= 3

    if prior_qa:
        def _ngrams(s: str, n: int = 4) -> set:
            ws = s.lower().split()
            return set(tuple(ws[i:i+n]) for i in range(len(ws) - n + 1))
        resp_ng = _ngrams(text)
        overlap_ok = True
        for qa in prior_qa:
            prev_ng = _ngrams(qa.get("q", ""))
            if prev_ng and resp_ng:
                overlap = len(resp_ng & prev_ng) / max(len(prev_ng), 1)
                if overlap > 0.6:
                    overlap_ok = False
                    break
        scores["non_repeat"] = overlap_ok
    else:
        scores["non_repeat"] = True

    total = sum(scores.values())
    return {"total": total, "max": 4, "axes": scores}

class ModelAgent:
    def __init__(
        self,
        display_name: str,
        personality_model: Any,
        *,
        tools: Dict[str, ToolFn] | None = None,
        enable_tools: bool = False,
        max_tool_steps: int = 3,
        token_callback: Optional[Callable[[str, str], None]] = None,
    ):
        self.display_name = display_name
        self.model = personality_model
        self.tools = tools or {}
        self.enable_tools = enable_tools
        self.max_tool_steps = max_tool_steps
        # token_callback(who, token) — called for each streamed token
        self.token_callback = token_callback

    def _compose_prompt(self, ctx: AgentContext) -> str:
        parts: List[str] = []

        # Inject query mode so every personality knows what type of response to give.
        # This overrides the code-centric defaults baked into each system prompt.
        _mode = ctx.shared.get("query_mode", "")
        if _mode == "conversational":
            parts.append(
                "QUERY MODE: CONVERSATIONAL\n"
                "The user is having a conversation — NOT asking for code.\n"
                "Respond entirely in natural prose. Do NOT write code, scripts, or "
                "technical implementations unless the user explicitly asked for them.\n"
                "Focus on explaining, discussing, or answering the question directly."
            )
        elif _mode == "technical":
            parts.append(
                "QUERY MODE: TECHNICAL\n"
                "The user wants working code or a technical solution.\n"
                "Prioritise correctness, completeness, and runnability."
            )

        cands = ctx.shared.get("candidates", {})
        rank = ctx.shared.get("judge_ranking", "")
        critique = ctx.shared.get("judge_critique", "")
        tool_payloads = ctx.shared.get("tool_payloads", {})
        discussion = ctx.shared.get("discussion_transcript", "")

        if cands:
            parts.append("CANDIDATE ANSWERS + PEASANT QUESTIONS + REBUTTALS:")
            for role, data in cands.items():
                parts.append(f"--- {role} ---")
                parts.append("ANSWER:")
                parts.append(data.get("answer", ""))
                if pq := data.get("peasant_q", ""):
                    parts.append(f"PEASANT QUESTIONS:\n{pq}")
                if rb := data.get("rebuttal", ""):
                    parts.append(f"REBUTTAL:\n{rb}")
                if disc := data.get("discussion", ""):
                    parts.append(f"DISCUSSION LOG:\n{disc}")
                parts.append("")

        if discussion:
            parts.append(f"FULL DISCUSSION (condensed):\n{discussion}\n")
        if rank:
            # Inject explicit winner label — models respond far better to a clear
            # directive than to having to parse the winner out of embedded JSON.
            try:
                import json as _cjson
                _robj = _cjson.loads(rank) if isinstance(rank, str) else rank
                _winner = _robj.get("winner", "")
                if _winner and _winner != "unknown":
                    parts.append(f"WINNING CANDIDATE: {_winner} — synthesise primarily from this answer.")
            except Exception:
                pass
            parts.append(f"JUDGE RANKING (JSON):\n{rank}\n")
        if critique:
            parts.append(f"JUDGE CRITIQUE:\n{critique}\n")
        required_changes = ctx.shared.get("required_changes", [])
        if required_changes:
            parts.append("REQUIRED CHANGES -- YOU MUST ADDRESS EVERY ITEM BELOW:")
            for _rc in required_changes:
                parts.append("  - " + _rc)
            parts.append("")
        adv_challenge = ctx.shared.get("adversarial_challenge", "")
        if adv_challenge:
            parts.append(
                "ADVERSARIAL CHALLENGE (you MUST explicitly rebut this in your answer):\n"
                + adv_challenge + "\n"
            )
        if tool_payloads:
            parts.append("PRIOR TOOL OUTPUTS:")
            for k, v in tool_payloads.items():
                parts.append(f"- {k}: {str(v)[:900]}")
            parts.append("")

        # Repeat mode reminder as the LAST thing before user request.
        # Models anchor to recency — the instruction at the bottom wins over code seen above.
        _mode_bottom = ctx.shared.get("query_mode", "")
        if _mode_bottom == "conversational":
            parts.append(
                "⚠ REMINDER: This is a CONVERSATIONAL query. "
                "Do NOT write code. Respond in prose only. "
                "Ignore any code in the candidate answers above — it should not have been there."
            )
        elif _mode_bottom == "technical":
            parts.append(
                "⚠ REMINDER: This is a TECHNICAL query. "
                "Prioritise working, complete code."
            )

        parts.append(f"USER REQUEST:\n{ctx.user_text}")

        if self.enable_tools and self.tools:
            tool_list = ", ".join(sorted(self.tools.keys()))
            parts += [
                "",
                f"TOOLS AVAILABLE: {tool_list}",
                "To use a tool, output ONLY JSON: {\"tool\":\"name\",\"args\":{...}}",
                "Otherwise write a normal answer.",
            ]
        return "\n".join(parts)

    def _make_token_cb(self) -> Optional[Callable[[str], None]]:
        if self.token_callback is None:
            return None
        who = self.display_name
        cb = self.token_callback
        def _cb(token: str):
            cb(who, token)
        return _cb

    def act(self, ctx: AgentContext) -> List[AgentEvent]:
        events: List[AgentEvent] = []
        prompt = self._compose_prompt(ctx)

        if not (self.enable_tools and self.tools):
            events.append(AgentEvent(self.display_name, "thought", "Generating response…"))
            text = self.model.respond(prompt, token_callback=self._make_token_cb())
            return [AgentEvent(self.display_name, "final", text)]

        events.append(AgentEvent(self.display_name, "thought", "Calling model backend…"))
        text = self.model.respond(prompt, token_callback=self._make_token_cb())

        for _ in range(self.max_tool_steps):
            calls = _extract_tool_calls(text)
            if not calls:
                events.append(AgentEvent(self.display_name, "final", text))
                return events

            events.append(AgentEvent(self.display_name, "action", f"Tool calls requested ({len(calls)})."))
            obs_lines: List[str] = []
            payloads: Dict[str, Any] = {}

            for i, call in enumerate(calls, start=1):
                tool_name = str(call.get("tool", "")).strip()
                args = call.get("args", {})
                if tool_name not in self.tools:
                    obs_lines.append(f"[{i}] ERROR: unknown tool '{tool_name}'")
                    continue
                if not isinstance(args, dict):
                    obs_lines.append(f"[{i}] ERROR: args must be a dict")
                    continue
                ok, msg, payload = self.tools[tool_name](args)
                obs_lines.append(f"[{i}] {tool_name}: {'OK' if ok else 'FAIL'}\n{msg}")
                if payload:
                    payloads[f"{tool_name}_{i}"] = payload

            ctx.shared.setdefault("tool_payloads", {}).update(payloads)
            obs_text = "\n\n".join(obs_lines).strip() or "(no tool output)"
            events.append(AgentEvent(self.display_name, "observation", obs_text))

            followup = (
                f"TOOL RESULTS:\n{obs_text}\n\n"
                "Now produce the best possible answer (no tool JSON unless more tools needed)."
            )
            events.append(AgentEvent(self.display_name, "thought", "Calling model (post-tool)…"))
            text = self.model.respond(followup, token_callback=self._make_token_cb())

        events.append(AgentEvent(self.display_name, "final", text))
        return events

class DeliberationOrchestrator:
    """
    Runs the full deliberation loop and emits events live through
    an `event_callback(AgentEvent)` as they occur.
    """

    def __init__(
        self,
        *,
        judge_model: Any,
        agents: Dict[str, ModelAgent],
        max_rounds: int = 2,
        debate_turns: int = 2,
        event_callback: Optional[Callable[[AgentEvent], None]] = None,
        clarification_cb: Optional[Callable[[str, str], None]] = None,
        pause_event: Optional[threading.Event] = None,
        answer_getter: Optional[Callable[[], str]] = None,
    ):
        self.judge = judge_model
        self.agents = agents
        self.max_rounds = max_rounds
        self.debate_turns = max(1, int(debate_turns))
        self.event_callback = event_callback or (lambda e: None)
        # Clarification pause support
        self._clarification_cb = clarification_cb   # fn(who, question) → shows UI
        self._pause_event      = pause_event         # threading.Event to wait on
        self._answer_getter    = answer_getter        # fn() → str answer

    def _emit(self, event: AgentEvent) -> None:
        self.event_callback(event)

    def _phase(self, label: str) -> None:
        self._emit(AgentEvent("Orchestrator", "phase", f"▶ {label}"))

    def run(self, user_text: str, *, panel: List[str], synth: str = "writer",
            extra_ctx: Optional[Dict[str, Any]] = None) -> List[AgentEvent]:
        ctx = AgentContext(user_text=user_text)
        if extra_ctx:
            ctx.shared.update(extra_ctx)
        all_events: List[AgentEvent] = []

        # Guard: synth must exist in agents — fall back to writer or first panel member
        if synth not in self.agents:
            synth = "writer" if "writer" in self.agents else (panel[0] if panel else synth)

        # Accumulates every Peasant question across all rounds and cross-fire
        # turns so the model is never shown a blank slate and cannot re-ask
        # something already covered.  Each entry: {"q": <text>, "a": ""}
        _peasant_qa_log: List[Dict[str, str]] = []

        def _log_peasant_questions(qtxt: str) -> None:
            import re as _re
            parts = re.split(r"(?:^|\n)(?:Q\d+[:.)]|\d+[.)]\ +|\[\d+\]\ *)", qtxt)
            questions = [p.strip() for p in parts if p.strip() and "?" in p]
            if not questions:
                questions = [p.strip() for p in qtxt.split("\n\n") if "?" in p]
            if not questions:
                questions = [qtxt.strip()]
            for q in questions:
                _peasant_qa_log.append({"q": q, "a": ""})

        def emit(ev: AgentEvent):
            all_events.append(ev)
            self._emit(ev)

        for r in range(self.max_rounds):
            # ── Check pause at start of each round ──────────────────
            # If a clarification is pending, wait here before any
            # new model calls fire. This ensures the whole round
            # waits, not just the individual candidate step.
            if self._pause_event and not self._pause_event.is_set():
                self._pause_event.wait(timeout=300)

            self._phase(f"Round {r+1}/{self.max_rounds} — Candidate generation")

            candidates: Dict[str, Dict[str, str]] = {}
            discussion_lines: List[str] = []

            # 1) Candidates + Peasant cross-exam
            for key in panel:
                self._phase(f"{key.capitalize()} — drafting answer")
                evs = self.agents[key].act(ctx)
                for ev in evs:
                    emit(ev)
                answer = next((e.text for e in reversed(evs) if e.kind == "final"), "")
                # Strip code from candidate answers on conversational routes
                # so they don't contaminate what other panel members read.
                _qmode = ctx.shared.get("query_mode", "")
                _stored_answer = answer
                if _qmode == "conversational":
                    _stored_answer = _strip_code_blocks(answer)
                # ── #8 Self-reported confidence ─────────────────────────────
                # Ask each candidate to rate their own confidence 1-10.
                # A single cheap token call — models are usually well-calibrated
                # at distinguishing "I'm guessing" from "I'm certain".
                _self_conf = 5  # default if call fails
                try:
                    _conf_raw = self.agents[key].model.respond(
                        "Rate your confidence in the answer you just gave, 1–10. "
                        "Reply with ONLY the single digit — no words, no punctuation.\n\n"
                        f"YOUR ANSWER (first 400 chars):\n{answer[:400]}",
                        max_tokens=5,
                    ).strip()
                    _self_conf = int(_conf_raw[0]) if _conf_raw and _conf_raw[0].isdigit() else 5
                    _self_conf = max(1, min(10, _self_conf))
                except Exception:
                    pass
                candidates[key] = {
                    "answer": _stored_answer,
                    "peasant_q": "", "rebuttal": "", "discussion": "",
                    "self_confidence": _self_conf,
                }
                if _self_conf <= 4:
                    emit(AgentEvent(key.capitalize(), "observation",
                                   f"⚠ Self-confidence: {_self_conf}/10 — answer may be weak"))
                else:
                    emit(AgentEvent(key.capitalize(), "observation",
                                   f"Confidence: {_self_conf}/10"))
                discussion_lines.append(f"{key.upper()} CANDIDATE [conf:{_self_conf}/10]:\n{_stored_answer}\n")

                # ── Clarification pause ──────────────────────────────
                # If a non-Peasant personality asked the user a direct question,
                # pause deliberation and wait for the user to answer.
                if key != "peasant" and self._clarification_cb and self._pause_event:
                    _q = _detect_user_question(_stored_answer)
                    if _q:
                        self._pause_event.clear()  # pause
                        self._clarification_cb(key.capitalize(), _q)
                        # Block the worker thread until user answers (5 min max)
                        self._pause_event.wait(timeout=300)
                        _user_answer = self._answer_getter() if self._answer_getter else ""
                        if _user_answer and not _user_answer.startswith("[User skipped"):
                            _clarif_note = (f"\n\nUSER CLARIFICATION for {key}:\n"
                                           f"  Q: {_q}\n  A: {_user_answer}\n")
                            user_text = user_text + _clarif_note
                            ctx.user_text = user_text
                            discussion_lines.append(_clarif_note)

                if key != "peasant" and "peasant" in self.agents:
                    self._phase(f"Peasant — cross-examining {key}")
                    _pexam_mode = ctx.shared.get("query_mode", "")
                    qtxt = peasant_cross_exam(
                        self.agents["peasant"].model,
                        candidate_role=key, candidate_text=answer, user_text=user_text,
                        prior_qa=_peasant_qa_log if _peasant_qa_log else None,
                        query_mode=_pexam_mode,
                    )
                    _pq_score = _peasant_quality_score(qtxt, answer, _peasant_qa_log)
                    if not _looks_like_two_questions(qtxt):
                        # Reformat existing answer rather than full regeneration — cheaper
                        _reformat_prompt = (
                            "Your response below is good but needs exactly two questions "
                            "labelled Q1: and Q2:. Reformat it now — keep the same ideas, "
                            "just add Q1: and Q2: labels and make sure each ends with '?'.\n\n"
                            f"YOUR RESPONSE:\n{qtxt}"
                        )
                        qtxt = self.agents["peasant"].model.respond(
                            _reformat_prompt, max_tokens=300)
                        _pq_score = _peasant_quality_score(qtxt, answer, _peasant_qa_log)
                        if not _looks_like_two_questions(qtxt):
                            _axes = ", ".join(
                                k + ("=✓" if v else "=✗")
                                for k, v in _pq_score["axes"].items()
                            )
                            emit(AgentEvent("Peasant", "observation",
                                "⚠ Quality low after reformat ("
                                + str(_pq_score["total"]) + "/4: " + _axes + ")"))
                    _log_peasant_questions(qtxt)
                    candidates[key]["peasant_q"] = qtxt
                    _stag = " [q:" + str(_pq_score["total"]) + "/4]"
                    ev = AgentEvent("Peasant", "observation",
                                   f"Questions about {key}" + _stag + ":\n" + qtxt)
                    emit(ev)
                    discussion_lines.append(f"PEASANT → {key}:\n{qtxt}\n")

                ctx.shared["candidates"] = candidates
                ctx.shared["discussion_transcript"] = "\n".join(discussion_lines[-40:])

            # 2) Rebuttals
            if self._pause_event and not self._pause_event.is_set():
                self._pause_event.wait(timeout=300)
            self._phase("Rebuttal round")
            for key in panel:
                if key == "peasant" or key not in candidates:
                    continue
                other_roles = [r for r in candidates if r != key]
                debate_lines = [
                    "DEBATE CONTEXT:",
                    f"User request:\n{user_text}\n",
                    f"Your original answer ({key}):\n{candidates[key].get('answer','')}\n",
                ]
                if my_pq := candidates[key].get("peasant_q", ""):
                    debate_lines.append(f"Peasant questions about YOUR answer:\n{my_pq}\n")
                for rr in other_roles:
                    debate_lines.append(f"Other candidate ({rr}):\n{candidates[rr].get('answer','')}\n")
                    if pq := candidates[rr].get("peasant_q", ""):
                        debate_lines.append(f"Peasant questions about {rr}:\n{pq}\n")
                _rb_mode = ctx.shared.get("query_mode", "")
                _rb_mode_line = (
                    "⚠ MODE: CONVERSATIONAL — rebuttal must be in prose only, no code.\n"
                    if _rb_mode == "conversational" else
                    "⚠ MODE: TECHNICAL — focus on code correctness and completeness.\n"
                    if _rb_mode == "technical" else ""
                )
                debate_lines += [
                    _rb_mode_line,
                    "INSTRUCTIONS:",
                    "- Write a rebuttal/improvement note.",
                    "- Explicitly state disagreements.",
                    "- Address Peasant questions.",
                    "- Propose concrete fixes.",
                    "- Keep under 12 bullet points.",
                    "- Do NOT introduce code unless this is a TECHNICAL query.",
                ]
                extra_context = "\n".join(debate_lines)
                self._phase(f"{key.capitalize()} — rebuttal")
                rebuttal_text = self.agents[key].model.respond(
                    "Produce your rebuttal now.", extra_context=extra_context,
                    token_callback=self.agents[key]._make_token_cb(),
                    max_tokens=600,  # rebuttals must be concise bullets, not essays
                )
                candidates[key]["rebuttal"] = rebuttal_text
                ev = AgentEvent(key.capitalize(), "observation", f"Rebuttal:\n{rebuttal_text}")
                emit(ev)
                discussion_lines.append(f"{key.upper()} REBUTTAL:\n{rebuttal_text}\n")

                # ── Back-fill Peasant QA answers (Change 8) ────────────
                # The candidate's rebuttal IS their answer to Peasant's questions.
                # Fill the "a" slot in _peasant_qa_log so that in cross-fire,
                # Peasant sees what was already answered and can go deeper.
                if _peasant_qa_log:
                    peasant_qs_for_key = candidates[key].get("peasant_q", "")
                    for qa_entry in _peasant_qa_log:
                        if not qa_entry.get("a") and qa_entry["q"][:60] in peasant_qs_for_key:
                            qa_entry["a"] = rebuttal_text[:400].strip()
                ctx.shared["candidates"] = candidates
                ctx.shared["discussion_transcript"] = "\n".join(discussion_lines[-60:])

            # 3) Cross-fire
            if self._pause_event and not self._pause_event.is_set():
                self._pause_event.wait(timeout=300)
            self._phase(f"Cross-fire — {self.debate_turns} turns")
            for turn in range(1, self.debate_turns + 1):
                for key in panel:
                    if key == "peasant" or key not in candidates:
                        continue
                    _cf_mode = ctx.shared.get("query_mode", "")
                    _cf_mode_note = (
                        "⚠ CONVERSATIONAL mode: respond in prose only, no code.\n"
                        if _cf_mode == "conversational" else
                        "⚠ TECHNICAL mode: focus on code quality and correctness.\n"
                        if _cf_mode == "technical" else ""
                    )
                    extra_context = (
                        f"CROSS-FIRE CONTEXT — Turn {turn}/{self.debate_turns}\n\n"
                        + _cf_mode_note +
                        "Rules:\n"
                        "- Write ONE short message.\n"
                        "- Include: AGREE: ... | DISAGREE: ... | ADD: ...\n"
                        "- Address Peasant questions about your answer.\n"
                        "- Keep under 10 lines.\n\n"
                        f"Discussion so far:\n{ctx.shared.get('discussion_transcript','')}\n"
                    )
                    self._phase(f"{key.capitalize()} — cross-fire T{turn}")
                    msg = self.agents[key].model.respond(
                        "Post your cross-fire message now.", extra_context=extra_context,
                        token_callback=self.agents[key]._make_token_cb(),
                        max_tokens=400,  # cross-fire must be tight — 10 lines max
                    )
                    candidates[key]["discussion"] = (
                        candidates[key].get("discussion", "") + f"\nTURN {turn}:\n{msg}\n"
                    ).strip()
                    ev = AgentEvent(key.capitalize(), "observation", f"Cross-fire T{turn}:\n{msg}")
                    emit(ev)
                    discussion_lines.append(f"{key.upper()} CROSS-FIRE T{turn}:\n{msg}\n")

                    if "peasant" in self.agents:
                        self._phase(f"Peasant — questions after {key} T{turn}")
                        _cf_pmode = ctx.shared.get("query_mode", "")
                        pq = peasant_cross_exam(
                            self.agents["peasant"].model,
                            candidate_role=f"{key} (T{turn})", candidate_text=msg, user_text=user_text,
                            prior_qa=_peasant_qa_log if _peasant_qa_log else None,
                            query_mode=_cf_pmode,
                        )
                        _cf_score = _peasant_quality_score(pq, msg, _peasant_qa_log)
                        if not _looks_like_two_questions(pq):
                            # Reformat rather than regenerate — same ideas, proper labels
                            _cf_reformat = (
                                "Your response below is good but needs exactly two questions "
                                "labelled Q1: and Q2:. Reformat it now — keep the same ideas, "
                                "just add Q1: and Q2: labels and make sure each ends with '?'.\n\n"
                                f"YOUR RESPONSE:\n{pq}"
                            )
                            pq = self.agents["peasant"].model.respond(
                                _cf_reformat, max_tokens=300)
                            _cf_score = _peasant_quality_score(pq, msg, _peasant_qa_log)
                            if not _looks_like_two_questions(pq):
                                _axes = ", ".join(
                                    k + ("=✓" if v else "=✗")
                                    for k, v in _cf_score["axes"].items()
                                )
                                emit(AgentEvent("Peasant", "observation",
                                    "⚠ CF quality low after reformat ("
                                    + str(_cf_score["total"]) + "/4: " + _axes + ")"))
                        _log_peasant_questions(pq)
                        _cftag = " [q:" + str(_cf_score["total"]) + "/4]"
                        pev = AgentEvent("Peasant", "observation",
                                        f"Cross-fire questions after {key} T{turn}" + _cftag + ":\n" + pq)
                        emit(pev)
                        discussion_lines.append(f"PEASANT → {key} T{turn}:\n{pq}\n")

                    ctx.shared["candidates"] = candidates
                    ctx.shared["discussion_transcript"] = "\n".join(discussion_lines[-80:])

            # 4) Judge ranks
            self._phase("Judge — ranking candidates")
            rank_json = self.judge.rank_candidates(user_text, candidates)
            ctx.shared["judge_ranking"] = rank_json
            try:
                import json as _rj
                ctx.shared["judge_confidence"] = int(_rj.loads(rank_json).get("confidence", 0))
            except Exception:
                ctx.shared["judge_confidence"] = 0
            ev = AgentEvent("Judge", "observation", f"Ranking:\n{rank_json}")
            emit(ev)

            # 4a) Low-confidence gap logging ─────────────────────────────────
            # Roles that reported self-confidence ≤4 are flagged so the
            # Librarian wishlist captures what vault data would have helped.
            for _lc_role, _lc_data in candidates.items():
                if _lc_data.get("self_confidence", 10) <= 4:
                    try:
                        _lc_topic = f"{_lc_role} answer to: {user_text[:80]}"
                        _lc_reason = (
                            f"{_lc_role} self-reported confidence "
                            f"{_lc_data['self_confidence']}/10 — vault data on this topic "
                            "would have strengthened the answer"
                        )
                        ctx.shared.setdefault("_low_conf_gaps", []).append(
                            {"who": _lc_role, "topic": _lc_topic, "reason": _lc_reason}
                        )
                    except Exception:
                        pass

            # 4b) Peasant adversarial challenge (optional)
            _adversarial_challenge = ""
            if (ctx.shared.get("peasant_adversarial", False)
                    and "peasant" in self.agents):
                try:
                    import json as _aj
                    _robj = _aj.loads(rank_json)
                    _winner_role = _robj.get("winner", "")
                    _winner_ans = candidates.get(_winner_role, {}).get("answer", "")
                except Exception:
                    _winner_role, _winner_ans = "", ""
                if _winner_role and _winner_ans:
                    self._phase("Peasant — adversarial challenge")
                    _adv_ctx = (
                        "USER REQUEST:\n" + user_text + "\n\n"
                        "WINNING CANDIDATE: " + _winner_role + "\n"
                        "WINNING ANSWER:\n" + _winner_ans + "\n\n"
                        "Your task: argue AGAINST this answer. Identify the single most\n"
                        "dangerous flaw, edge case, or false assumption.\n"
                        "Be specific and adversarial. Do NOT offer improvements.\n"
                        "Format: CHALLENGE: <your strongest objection in 3-6 sentences>"
                    )
                    _adversarial_challenge = self.agents["peasant"].model.respond(
                        "State your adversarial challenge now.",
                        extra_context=_adv_ctx,
                        max_tokens=300,
                    )
                    ctx.shared["adversarial_challenge"] = _adversarial_challenge
                    ctx.shared["adversarial_target"] = _winner_role
                    emit(AgentEvent("Peasant", "observation",
                                   "Adversarial: " + _adversarial_challenge))

            # 5) Writer synthesizes
            self._phase("Writer — synthesizing final answer")
            synth_evs = self.agents[synth].act(ctx)
            for ev in synth_evs:
                emit(ev)
            synth_final = next((e.text for e in reversed(synth_evs) if e.kind == "final"), "")
            # T1-D: Track per-round Writer output, emit unified diff on round 2+
            _round_outputs = ctx.shared.setdefault("_round_outputs", [])
            _round_outputs.append(synth_final)
            if len(_round_outputs) >= 2:
                import difflib as _dl
                _prev_r = len(_round_outputs) - 1
                _curr_r = len(_round_outputs)
                _prev_lines = _round_outputs[-2].splitlines(keepends=True)
                _curr_lines = _round_outputs[-1].splitlines(keepends=True)
                _diff_lines = list(_dl.unified_diff(
                    _prev_lines, _curr_lines,
                    fromfile="round_" + str(_prev_r),
                    tofile="round_" + str(_curr_r),
                    lineterm="",
                ))
                if _diff_lines:
                    _diff_text = "".join(_diff_lines[:80])
                    _diff_label = "r" + str(_prev_r) + " -> r" + str(_curr_r)
                    _diff_msg = "Round diff (" + _diff_label + "):\n" + _diff_text
                    emit(AgentEvent("Orchestrator", "observation", _diff_msg))


            # 6) Judge critiques
            self._phase("Judge — critiquing synthesis")
            critique = self.judge.critique(user_text, synth_final, extra_context=f"Ranking:\n{rank_json}", query_mode=ctx.shared.get("query_mode", ""))
            ctx.shared["judge_critique"] = critique
            ev = AgentEvent("Judge", "observation", critique)
            emit(ev)

            if "Verdict: PASS" in critique:
                self._phase("✓ Verdict: PASS — deliberation complete")
                break

            # ── Confidence-gated early exit ──────────────────────────────
            # Even on NEEDS_WORK, if Judge confidence is very high (≥8/10)
            # and this is the final round, skip re-deliberation — the answer
            # is probably good enough and more rounds won't help much.
            _conf = ctx.shared.get("judge_confidence", 0)
            _is_last_round = (r == self.max_rounds - 1)
            if _conf >= 8 and _is_last_round:
                self._phase(
                    f"✓ High confidence ({_conf}/10) — accepting answer despite NEEDS_WORK"
                )
                break

            # ── Confidence-gated extra round ─────────────────────────────
            # If confidence is very low (≤2/10) on round 1, allow an extra
            # round beyond max_rounds — the answer needs more work.
            if _conf <= 2 and r == 0 and self.max_rounds < 3:
                self._phase(
                    f"⚠ Low confidence ({_conf}/10) — adding extra deliberation round"
                )
                self.max_rounds = 3

            else:
                # T2-C: Parse REQUIRED_CHANGES for targeted round-2 Writer brief
                _changes = self.judge.__class__.parse_required_changes(critique)
                if _changes:
                    ctx.shared["required_changes"] = _changes
                    _chg_txt = "\n".join("- " + c for c in _changes)
                    emit(AgentEvent("Judge", "observation",
                                   "Required changes for next round:\n" + _chg_txt))

        # Expose shared context so caller can retrieve low-confidence gaps etc.
        self._last_ctx = ctx
        return all_events
