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

ROUNDS
Round 1 is the full council: drafts, the Peasant's questions, rebuttals,
cross-fire, the Judge's ranking, the Writer's answer, the Judge's critique.
On NEEDS_WORK the next round is a REVISION — the Writer rewrites its own
answer against the critique and the Judge checks it again, two calls instead
of ~30 — unless the critique rejects the approach, names nothing to change,
or comes with very low confidence (needs_full_round); then the whole panel
runs again.

The two defects this file once carried (A7: an extra round announced but
never run, because `for r in range(...)` fixed the count; A8: the required
changes skipped exactly when confidence was lowest) are fixed; see
tests/test_revision_round.py.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .confidence import HIGH as _cf_HIGH, LOW as _cf_LOW, \
    VERY_LOW as _cf_VERY_LOW


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

    from .council_schemas import QUESTIONS_SCHEMA, render_questions
    return render_questions(_ask(
        peasant_model, prompt, extra_context=extra_context,
        json_schema=QUESTIONS_SCHEMA, think=THINK["peasant"]))

#: How hard each step thinks, on models that think (council_engine.
#: think_level): short, frequent steps low; the answer itself and the
#: Judge high.
THINK = {"draft": "medium", "rebuttal": "medium", "cross_fire": "low",
         "peasant": "low", "confidence": "low", "adversarial": "low",
         "synthesis": "high"}


def _ask(model: Any, prompt: str, **kw: Any) -> str:
    """model.respond(prompt, **kw), dropping json_schema / think for a
    model that does not take them (stand-ins, older wrappers)."""
    try:
        return model.respond(prompt, **kw)
    except TypeError as exc:
        if not any(k in str(exc) for k in ("json_schema", "think",
                                           "unexpected keyword")):
            raise
        kw.pop("json_schema", None)
        kw.pop("think", None)
        return model.respond(prompt, **kw)


def _prior_qa_block(prior_qa: Optional[List[Dict[str, str]]]) -> str:
    if not prior_qa:
        return ""
    lines = ["━━━ QUESTIONS YOU HAVE ALREADY ASKED THIS SESSION ━━━",
             "Do NOT ask any of these again, even in paraphrased form.", ""]
    for i, item in enumerate(prior_qa[-30:], 1):
        lines.append(f"[{i}] Q: {item['q']}")
        if item.get("a"):
            lines.append(f"    A: {item['a']}")
    lines.append("━━━ END OF PRIOR Q&A ━━━")
    return "\n".join(lines)


def peasant_turn_questions(peasant_model, *, messages: Dict[str, str],
                           user_text: str, turn: int,
                           prior_qa: Optional[List[Dict[str, str]]] = None,
                           query_mode: str = "") -> str:
    """ONE Peasant call for a whole cross-fire turn: two questions for each
    member's message, under a TO <ROLE>: heading. It used to be one call
    per member per turn, plus a reformat call whenever the reply lacked
    Q1/Q2 labels."""
    parts = [f"ORIGINAL REQUEST:\n{user_text}\n"]
    for role, msg in messages.items():
        parts.append(f"{role.upper()} — CROSS-FIRE TURN {turn}:\n{msg}\n")
    if prior := _prior_qa_block(prior_qa):
        parts.append(prior)
    if query_mode == "conversational":
        parts.append("⚠ CONVERSATIONAL MODE: ask about accuracy, clarity and "
                     "whether it answers the user — not about code.")
    elif query_mode == "technical":
        parts.append("⚠ TECHNICAL MODE: focus on correctness, edge cases "
                     "and robustness.")
    roles = ", ".join(r.upper() for r in messages)
    prompt = (
        f"For EACH of these members ({roles}), ask two NEW questions about "
        "something specific in their message above. Use exactly this form "
        "for every member:\n"
        "TO <ROLE>:\nQ1: <question>?\nQ2: <question>?\n"
        "Do not repeat any earlier question.")
    from .council_schemas import render_turn_questions, turn_questions_schema
    roles_l = [r.lower() for r in messages]
    return render_turn_questions(_ask(
        peasant_model, prompt, extra_context="\n\n".join(parts),
        max_tokens=160 * max(1, len(messages)),
        json_schema=turn_questions_schema(roles_l),
        think=THINK["peasant"]), roles_l)


_TO_ROLE = re.compile(r"^\s*\**\s*TO\s+([A-Za-z_]+)\s*\**\s*:?\s*\**\s*$",
                      re.IGNORECASE | re.MULTILINE)


def split_turn_questions(text: str, roles: List[str]) -> Dict[str, str]:
    """{role: its questions} from peasant_turn_questions' reply. A reply
    without the headings goes to every member whole."""
    marks = [(m.start(), m.end(), m.group(1).lower())
             for m in _TO_ROLE.finditer(text or "")]
    out: Dict[str, str] = {}
    for i, (_s, end, role) in enumerate(marks):
        stop = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        if role in roles:
            out[role] = text[end:stop].strip()
    if not out:
        return {r: (text or "").strip() for r in roles}
    return out


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

#: Words in a critique that reject the APPROACH rather than name fixes.
_START_OVER = re.compile(
    r"wrong (approach|method|question|problem|file|data)|start over|"
    r"from scratch|fundamentally|misunderst|does not answer|doesn't answer|"
    r"did not answer|didn't answer|off[- ]topic|irrelevant|wrong language|"
    r"not what (the user|was) asked", re.IGNORECASE)


def _depth_of(ctx: "AgentContext"):
    """The question's depth (council_core.depth) from ctx.shared["depth"];
    Deep when nobody said, which is how the council always ran."""
    from .depth import DEEP, Depth
    d = ctx.shared.get("depth")
    if isinstance(d, Depth):
        return d
    if isinstance(d, str) and d:
        return Depth(d, "", forced=True)
    return Depth(DEEP, "")


def required_changes_of(judge: Any, critique: str) -> List[str]:
    """The critique's REQUIRED_CHANGES bullets — the judge's own parser when
    it has one, else the same rule here."""
    parse = getattr(type(judge), "parse_required_changes", None)
    if callable(parse):
        try:
            return list(parse(critique) or [])
        except Exception:                                 # noqa: BLE001
            pass
    if "Verdict: PASS" in (critique or ""):
        return []
    m = re.search(r"REQUIRED_CHANGES:\s*\n((?:\s*-\s*.+\n?)+)", critique or "")
    if not m:
        return []
    return [ln.strip().lstrip("- ").strip() for ln in m.group(1).splitlines()
            if ln.strip().startswith("-")]


def needs_full_round(critique: str, changes: List[str], confidence: int) -> str:
    """Why the next round must be the whole panel again, or "" when the
    Writer can revise its answer against the critique instead."""
    from .confidence import VERY_LOW
    if _START_OVER.search(critique or ""):
        return "the Judge rejected the approach, not just details"
    if not changes:
        return "the Judge named no specific changes to make"
    if confidence <= VERY_LOW:
        return f"the Judge's confidence is very low ({confidence}%)"
    return ""


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

    def _compose_prompt(self, ctx: AgentContext, *,
                        ask_confidence: bool = False) -> str:
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
        previous = ctx.shared.get("previous_answer", "")
        if previous:
            parts.append(
                "YOUR PREVIOUS ANSWER — the Judge's critique of it follows. "
                "REVISE it: keep everything that was right, fix every "
                "required change, and write the whole improved answer (not a "
                "list of edits):\n" + previous + "\n")
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
        if ask_confidence:
            from .confidence import DRAFT_INSTRUCTION
            parts += ["", DRAFT_INSTRUCTION]

        if self.enable_tools and self.tools:
            # A tool's one-line `help` (args and purpose) when it has one:
            # a bare name list left a small model guessing the arguments.
            helps = [f"- {n}: {getattr(fn, 'help', '')}".rstrip(": ")
                     for n, fn in self.tools.items()]
            if any(getattr(fn, "help", "") for fn in self.tools.values()):
                parts += ["", "TOOLS AVAILABLE (args — what it does):",
                          *helps]
            else:
                parts += ["", "TOOLS AVAILABLE: "
                          + ", ".join(sorted(self.tools.keys()))]
            parts += [
                "To use a tool, output ONLY JSON: {\"tool\":\"name\",\"args\":{...}}",
                "Use a tool when it replaces a guess (a number, a quote, a "
                "file's contents, whether code runs); otherwise write a "
                "normal answer.",
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

    def act(self, ctx: AgentContext, *,
            ask_confidence: bool = False,
            think: Optional[str] = None) -> List[AgentEvent]:
        """The member's answer as events. `ask_confidence`: a draft, which
        ends with a CONFIDENCE line (council_core.confidence). `think`: the
        step's thinking effort (THINK)."""
        events: List[AgentEvent] = []
        prompt = self._compose_prompt(ctx, ask_confidence=ask_confidence)

        if not (self.enable_tools and self.tools):
            events.append(AgentEvent(self.display_name, "thought", "Generating response…"))
            text = _ask(self.model, prompt,
                        token_callback=self._make_token_cb(), think=think)
            return [AgentEvent(self.display_name, "final", text)]

        # NATIVE tool calling when the member's model has it (an Ollama
        # model with the "tools" capability): the server returns the calls
        # as data, with arguments checked against each tool's schema
        # (tool_kit.PARAMS). Elsewhere the model writes {"tool": …} JSON in
        # its text and _extract_tool_calls finds it, as before.
        specs = self._native_specs()

        def ask(text_prompt: str):
            """(text, calls) for one model call."""
            nonlocal specs
            if specs:
                try:
                    reply = self.model.respond_with_tools(
                        text_prompt, specs, think=think)
                    calls = [{"tool": c.get("name"),
                              "args": c.get("arguments") or {}}
                             for c in reply.get("tool_calls") or []]
                    return str(reply.get("content") or ""), calls
                except Exception as exc:                  # noqa: BLE001
                    events.append(AgentEvent(
                        self.display_name, "thought",
                        f"Native tool calling failed ({exc}); using text."))
                    specs = []
            out = _ask(self.model, text_prompt,
                       token_callback=self._make_token_cb(), think=think)
            return out, _extract_tool_calls(out)

        events.append(AgentEvent(self.display_name, "thought", "Calling model backend…"))
        text, calls = ask(prompt)
        results: List[str] = []

        for _ in range(self.max_tool_steps):
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
                # A tool that raises (run_python past its timeout raises
                # TimeoutExpired; a vault read of a missing file raises) is a
                # failed call the model can see and work around — not the end
                # of the whole turn, which is what letting it escape meant.
                try:
                    ok, msg, payload = self.tools[tool_name](args)
                except Exception as exc:                  # noqa: BLE001
                    ok, msg, payload = (False, f"the tool raised "
                                        f"{type(exc).__name__}: {exc}", {})
                obs_lines.append(f"[{i}] {tool_name}: {'OK' if ok else 'FAIL'}\n{msg}")
                if payload:
                    payloads[f"{tool_name}_{i}"] = payload

            ctx.shared.setdefault("tool_payloads", {}).update(payloads)
            obs_text = "\n\n".join(obs_lines).strip() or "(no tool output)"
            events.append(AgentEvent(self.display_name, "observation", obs_text))
            results.append(obs_text)

            # The follow-up carries the ORIGINAL prompt and every result so
            # far. It used to send the results alone — each call is
            # stateless, so the model answered without seeing the question.
            followup = (
                f"{prompt}\n\nTOOL RESULTS SO FAR:\n"
                + "\n\n".join(results) + "\n\n"
                "Now produce the best possible answer (no tool call unless "
                "more tools are needed)."
            )
            if ask_confidence:
                from .confidence import DRAFT_INSTRUCTION
                followup += "\n\n" + DRAFT_INSTRUCTION
            events.append(AgentEvent(self.display_name, "thought", "Calling model (post-tool)…"))
            text, calls = ask(followup)

        events.append(AgentEvent(self.display_name, "final", text))
        return events

    def _native_specs(self) -> List[Dict[str, Any]]:
        """The tools as native specs when this member's model calls tools
        natively; [] for the text path."""
        if not hasattr(self.model, "respond_with_tools"):
            return []
        try:
            import council_engine
            if not council_engine.native_tools(getattr(self.model, "name",
                                                       None)):
                return []
            from .tool_kit import tool_specs
            return tool_specs(self.tools)
        except Exception:                                 # noqa: BLE001
            return []

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
        parallel_members: bool = False,
    ):
        self.judge = judge_model
        #: Draft and rebut side by side (see _draft_all). Off by default: it
        #: only saves time when the members' models are on different
        #: machines or are different in-app models — calls to the same
        #: model or machine still queue (node_routing.host_slot).
        self.parallel_members = bool(parallel_members)
        self.agents = agents
        self.max_rounds = max_rounds
        self._confidence_reasons: Dict[str, str] = {}
        self.debate_turns = max(1, int(debate_turns))
        self.event_callback = event_callback or (lambda e: None)
        # Clarification pause support
        self._clarification_cb = clarification_cb   # fn(who, question) → shows UI
        self._pause_event      = pause_event         # threading.Event to wait on
        self._answer_getter    = answer_getter        # fn() → str answer

    def _emit(self, event: AgentEvent) -> None:
        self.event_callback(event)

    # -- members side by side ---------------------------------------------
    def _draft_one(self, key: str, ctx: AgentContext,
                   emit: Optional[Callable[[AgentEvent], None]] = None):
        """One member's answer and its self-rated confidence:
        (events, answer, confidence). With `emit`, the answer's events go
        out as soon as they exist (the one-at-a-time path)."""
        from . import confidence as _cf
        evs = self.agents[key].act(ctx, ask_confidence=True,
                                   think=THINK["draft"])
        # Self-reported confidence, 0–100%, from the draft's own last line
        # (CONFIDENCE: 85% — reason). The line is cut from the answer the
        # others read. Only when it is missing is the member asked again,
        # with the whole answer in view this time, not its first 400
        # characters.
        reason = ""
        conf = None
        for i in range(len(evs) - 1, -1, -1):
            if evs[i].kind == "final":
                cleaned, conf, reason = _cf.split_draft(evs[i].text)
                evs[i] = AgentEvent(evs[i].who, "final", cleaned)
                break
        answer = next((e.text for e in reversed(evs) if e.kind == "final"), "")
        if emit is not None:
            for ev in evs:
                emit(ev)
        if conf is None:
            conf = _cf.DEFAULT
            try:
                raw = _ask(
                    self.agents[key].model,
                    "How confident are you in this answer, from 0 to 100 "
                    "percent? Reply with ONLY the number.\n\n"
                    f"THE ANSWER:\n{answer[:4000]}",
                    max_tokens=8, think=THINK["confidence"],
                )
                got = _cf.parse_reply(raw)
                conf = got if got is not None else _cf.DEFAULT
            except Exception:
                pass
        self._confidence_reasons[key] = reason
        return evs, answer, conf

    def _side_by_side(self, keys: List[str], fn: Callable[[str], Any]
                      ) -> Dict[str, Any]:
        """fn(key) for every key at once; results by key. Token streaming
        is off meanwhile — two members streaming together would interleave
        in one view — and the first error is raised once all have finished,
        as the one-at-a-time path would have raised it."""
        from . import node_routing
        saved = {k: self.agents[k].token_callback for k in keys}
        for k in keys:
            self.agents[k].token_callback = None
        try:
            outcomes = node_routing.run_parallel(
                [(lambda k=k: fn(k)) for k in keys])
        finally:
            for k, cb in saved.items():
                self.agents[k].token_callback = cb
        for o in outcomes:
            if not o.ok:
                raise o.error
        return {k: o.value for k, o in zip(keys, outcomes)}

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

        # A QUICK question (council_core.depth): one member — the
        # synthesiser — answers and the Judge checks it; no Peasant, no
        # rebuttal, no ranking, no separate synthesis.
        _quick = _depth_of(ctx).level == "quick"
        if _quick and synth in self.agents:
            panel = [synth]

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

        # A while loop, so an extra round granted below (max_rounds = 3)
        # really runs; `for r in range(...)` had fixed the count at the start.
        r = -1
        _revising = False
        while r + 1 < self.max_rounds:
            r += 1
            # ── Check pause at start of each round ──────────────────
            # If a clarification is pending, wait here before any
            # new model calls fire. This ensures the whole round
            # waits, not just the individual candidate step.
            if self._pause_event and not self._pause_event.is_set():
                self._pause_event.wait(timeout=300)

            if _revising:
                # A REVISION round (council_core.deliberation, round 2+):
                # the Judge named what to fix, so the Writer revises its
                # own answer against the critique and the Judge checks
                # again — two calls. The panel's drafts, the Peasant's
                # questions and the ranking are the previous round's and
                # stay in ctx.shared for the Writer to read.
                self._phase(f"Round {r+1}/{self.max_rounds} — Writer revises "
                            "against the critique")
                ctx.shared["previous_answer"] = synth_final
            else:
                ctx.shared.pop("previous_answer", None)
                self._phase(f"Round {r+1}/{self.max_rounds} — Candidate generation")

                candidates: Dict[str, Dict[str, str]] = {}
                discussion_lines: List[str] = []

                # 1) Candidates + Peasant cross-exam
                # Parallel: every member drafts at once, then the drafts are
                # taken in panel order below — clarifications and the Peasant's
                # cross-examination stay one at a time, since each builds on the
                # last.
                # THIS CHANGES THE DEBATE, deliberately: one at a time, each
                # member drafts after reading the earlier members' answers and
                # the Peasant's questions about them (_compose_prompt reads
                # ctx.shared["candidates"]), so later members can anchor on the
                # first. Side by side, the drafts are independent and the members
                # first meet each other's answers in the rebuttal. A
                # clarification then reaches the next round, not the members
                # drafting alongside.
                drafts: Dict[str, Any] = {}
                if self.parallel_members and len(panel) > 1:
                    self._phase(f"Drafting — {len(panel)} members at once")
                    drafts = self._side_by_side(
                        list(panel), lambda k: self._draft_one(k, ctx))
                for key in panel:
                    if key in drafts:
                        evs, answer, _self_conf = drafts[key]
                        for ev in evs:
                            emit(ev)
                    else:
                        self._phase(f"{key.capitalize()} — drafting answer")
                        evs, answer, _self_conf = self._draft_one(key, ctx, emit)
                    # Strip code from candidate answers on conversational routes
                    # so they don't contaminate what other panel members read.
                    _qmode = ctx.shared.get("query_mode", "")
                    _stored_answer = answer
                    if _qmode == "conversational":
                        _stored_answer = _strip_code_blocks(answer)
                    candidates[key] = {
                        "answer": _stored_answer,
                        "peasant_q": "", "rebuttal": "", "discussion": "",
                        "self_confidence": _self_conf,
                    }
                    _why = self._confidence_reasons.get(key, "")
                    candidates[key]["confidence_reason"] = _why
                    _why_txt = f" — least sure of: {_why}" if _why else ""
                    if _self_conf <= _cf_LOW:
                        emit(AgentEvent(key.capitalize(), "observation",
                                       f"⚠ Self-confidence: {_self_conf}% — answer may be weak{_why_txt}"))
                    else:
                        emit(AgentEvent(key.capitalize(), "observation",
                                       f"Confidence: {_self_conf}%{_why_txt}"))
                    discussion_lines.append(f"{key.upper()} CANDIDATE [conf:{_self_conf}%]:\n{_stored_answer}\n")

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

                    if key != "peasant" and "peasant" in self.agents \
                            and not _quick:
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

                def _rebuttal_context(key: str) -> str:
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
                    return "\n".join(debate_lines)

                def _rebut(key: str) -> str:
                    return _ask(
                        self.agents[key].model,
                        "Produce your rebuttal now.",
                        extra_context=_rebuttal_context(key),
                        token_callback=self.agents[key]._make_token_cb(),
                        max_tokens=600,  # rebuttals must be concise bullets, not essays
                        think=THINK["rebuttal"],
                    )

                rebutters = [k for k in panel
                             if k != "peasant" and k in candidates
                             and not _quick]
                # Each rebuttal reads only the finished drafts and the Peasant's
                # questions — never another rebuttal — so writing them side by
                # side changes nothing but the time it takes.
                rebuttals: Dict[str, str] = {}
                if self.parallel_members and len(rebutters) > 1:
                    self._phase(f"Rebuttals — {len(rebutters)} members at once")
                    rebuttals = self._side_by_side(rebutters, _rebut)
                for key in rebutters:
                    if key in rebuttals:
                        rebuttal_text = rebuttals[key]
                    else:
                        self._phase(f"{key.capitalize()} — rebuttal")
                        rebuttal_text = _rebut(key)
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

                # 3) Cross-fire — council_core.depth decides whether it runs:
                # Deep questions only (a Standard one is lifted when a
                # member is unsure), not when the drafts already agree, and
                # it ends after a turn in which nobody disagrees. The
                # Peasant asks about a whole turn in ONE call.
                from . import depth as _dp
                _depth = _depth_of(ctx)
                _members = [k for k in panel
                            if k != "peasant" and k in candidates]
                _unsure = [k for k in _members
                           if candidates[k].get("self_confidence", 100)
                           <= _cf_LOW]
                _skip_cf = ""
                if not _depth.cross_fire and not (
                        _depth.debate and _unsure and not _depth.forced):
                    _skip_cf = f"{_depth.level} depth"
                elif self.debate_turns < 1 or len(_members) < 2:
                    _skip_cf = "fewer than two members to argue"
                else:
                    _skip_cf = _dp.agree(candidates)
                if _skip_cf:
                    self._phase(f"Cross-fire skipped — {_skip_cf}")
                else:
                    if not _depth.cross_fire:
                        self._phase(f"Cross-fire added — {', '.join(_unsure)} "
                                    "unsure of its answer")
                    self._phase(f"Cross-fire — up to {self.debate_turns} turns")
                for turn in (range(1, self.debate_turns + 1)
                             if not _skip_cf else ()):
                    if self._pause_event and not self._pause_event.is_set():
                        self._pause_event.wait(timeout=300)

                    def _cf_context(turn=turn) -> str:
                        _cf_mode = ctx.shared.get("query_mode", "")
                        _cf_mode_note = (
                            "⚠ CONVERSATIONAL mode: respond in prose only, no code.\n"
                            if _cf_mode == "conversational" else
                            "⚠ TECHNICAL mode: focus on code quality and correctness.\n"
                            if _cf_mode == "technical" else ""
                        )
                        return (
                            f"CROSS-FIRE CONTEXT — Turn {turn}/{self.debate_turns}\n\n"
                            + _cf_mode_note +
                            "Rules:\n"
                            "- Write ONE short message.\n"
                            "- Include: AGREE: ... | DISAGREE: ... | ADD: ...\n"
                            "- Write 'DISAGREE: none' if nothing is left to dispute.\n"
                            "- Address Peasant questions about your answer.\n"
                            "- Keep under 10 lines.\n\n"
                            f"Discussion so far:\n{ctx.shared.get('discussion_transcript','')}\n"
                        )

                    def _post(key: str, turn=turn) -> str:
                        return _ask(
                            self.agents[key].model,
                            "Post your cross-fire message now.",
                            extra_context=_cf_context(turn),
                            token_callback=self.agents[key]._make_token_cb(),
                            max_tokens=400,  # cross-fire must be tight
                            think=THINK["cross_fire"],
                        )

                    def _record(key: str, msg: str, turn=turn) -> None:
                        candidates[key]["discussion"] = (
                            candidates[key].get("discussion", "")
                            + f"\nTURN {turn}:\n{msg}\n").strip()
                        emit(AgentEvent(key.capitalize(), "observation",
                                        f"Cross-fire T{turn}:\n{msg}"))
                        discussion_lines.append(
                            f"{key.upper()} CROSS-FIRE T{turn}:\n{msg}\n")
                        ctx.shared["candidates"] = candidates
                        ctx.shared["discussion_transcript"] = \
                            "\n".join(discussion_lines[-80:])

                    # Side by side, every member of a turn reads the
                    # discussion up to the previous turn (as rebuttals read
                    # the drafts); one at a time, each also reads the
                    # messages before it in this turn.
                    msgs: Dict[str, str] = {}
                    if self.parallel_members and len(_members) > 1:
                        self._phase(f"Cross-fire T{turn} — "
                                    f"{len(_members)} members at once")
                        msgs = self._side_by_side(_members, _post)
                        for key in _members:
                            _record(key, msgs[key])
                    else:
                        for key in _members:
                            self._phase(f"{key.capitalize()} — cross-fire T{turn}")
                            msgs[key] = _post(key)
                            _record(key, msgs[key])

                    if "peasant" in self.agents:
                        self._phase(f"Peasant — questions on cross-fire T{turn}")
                        pq = peasant_turn_questions(
                            self.agents["peasant"].model, messages=msgs,
                            user_text=user_text, turn=turn,
                            prior_qa=_peasant_qa_log or None,
                            query_mode=ctx.shared.get("query_mode", ""))
                        _log_peasant_questions(pq)
                        for key, qs in split_turn_questions(
                                pq, _members).items():
                            emit(AgentEvent(
                                "Peasant", "observation",
                                f"Cross-fire questions for {key} T{turn}:\n{qs}"))
                            discussion_lines.append(
                                f"PEASANT → {key} T{turn}:\n{qs}\n")
                        ctx.shared["discussion_transcript"] = \
                            "\n".join(discussion_lines[-80:])

                    if turn < self.debate_turns and \
                            not _dp.has_disagreement(msgs.values()):
                        self._phase("Cross-fire ended — no disagreements left")
                        break

                # 4) Judge ranks
                self._phase("Judge — ranking candidates")
                # Each candidate's Python, parsed (never run) — judge_view.
                from . import judge_view as _jv
                for _ck, _cd in candidates.items():
                    _chk = _jv.code_check(_cd.get("answer", ""))
                    _cd["code_check"] = _chk
                    if _chk and _jv.has_problems(_chk):
                        emit(AgentEvent("Judge", "observation",
                                        f"{_ck.capitalize()} — {_chk}"))
                # The vault's evidence, for ranking and critique only (never for
                # routing): ctx.shared["judge_evidence"], set by the front end
                # (council_core.vault_context.evidence). Passed only when there
                # is some, so a judge without the keyword still works.
                _evidence = str(ctx.shared.get("judge_evidence") or "")
                # The Judge's own checks (tool_kit.judge_checks): each passage a
                # candidate quotes, looked up in the vault. Run by the app, not
                # by the Judge — the Judge ranks, it does not call tools.
                _checker = ctx.shared.get("judge_checks")
                if callable(_checker):
                    try:
                        _checks = str(_checker(candidates) or "")
                    except Exception as _cexc:                # noqa: BLE001
                        _checks = ""
                        emit(AgentEvent("Judge", "observation",
                                        f"Quote checks failed: {_cexc}"))
                    if _checks:
                        emit(AgentEvent("Judge", "observation", _checks))
                        _evidence = "\n\n".join(x for x in (_evidence, _checks)
                                                 if x)
                if _quick:
                    # One candidate: nothing to rank. Its own confidence
                    # stands in for the Judge's until the critique.
                    _only = next(iter(candidates), synth)
                    _oc = int(candidates.get(_only, {}).get("self_confidence", 50))
                    rank_json = json.dumps({
                        "winner": _only, "scores": {_only: _oc},
                        "rationale": "quick depth: one member answered",
                        "confidence": _oc})
                elif _evidence:
                    rank_json = self.judge.rank_candidates(
                        user_text, candidates, extra_context=_evidence)
                else:
                    rank_json = self.judge.rank_candidates(user_text, candidates)
                ctx.shared["judge_ranking"] = rank_json
                try:
                    import json as _rj
                    from .confidence import normalise_ranking as _nr
                    _robj = _nr(_rj.loads(rank_json))
                    rank_json = _rj.dumps(_robj, ensure_ascii=False)
                    ctx.shared["judge_ranking"] = rank_json
                    ctx.shared["judge_confidence"] = int(_robj.get("confidence", 0))
                except Exception:
                    ctx.shared["judge_confidence"] = 0
                ev = AgentEvent("Judge", "observation", f"Ranking:\n{rank_json}")
                emit(ev)

                # 4a) Low-confidence gap logging ─────────────────────────────────
                # Roles that reported self-confidence ≤40% are flagged so the
                # Librarian wishlist captures what vault data would have helped.
                for _lc_role, _lc_data in candidates.items():
                    if _lc_data.get("self_confidence", 100) <= _cf_LOW:
                        try:
                            _lc_topic = f"{_lc_role} answer to: {user_text[:80]}"
                            _lc_why = _lc_data.get("confidence_reason", "")
                            _lc_reason = (
                                f"{_lc_role} self-reported confidence "
                                f"{_lc_data['self_confidence']}%"
                                + (f" — least sure of: {_lc_why}" if _lc_why
                                   else " — vault data on this topic would "
                                        "have strengthened the answer")
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
                        _adversarial_challenge = _ask(
                            self.agents["peasant"].model,
                            "State your adversarial challenge now.",
                            extra_context=_adv_ctx,
                            max_tokens=300, think=THINK["adversarial"],
                        )
                        ctx.shared["adversarial_challenge"] = _adversarial_challenge
                        ctx.shared["adversarial_target"] = _winner_role
                        emit(AgentEvent("Peasant", "observation",
                                       "Adversarial: " + _adversarial_challenge))

            # 5) Writer synthesizes
            _quick_answer = _quick and not _revising and synth in candidates
            if _quick_answer:
                # The quick answer IS the answer; its draft events are
                # already out under the synthesiser's name.
                synth_evs = [AgentEvent(self.agents[synth].display_name,
                                        "final", candidates[synth]["answer"])]
            else:
                self._phase("Writer — revising the answer" if _revising
                            else "Writer — synthesizing final answer")
                synth_evs = self.agents[synth].act(ctx,
                                                   think=THINK["synthesis"])
            for ev in synth_evs:
                if not _quick_answer:
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
            _crit_ctx = f"Ranking:\n{rank_json}"
            if _evidence:
                _crit_ctx += f"\n\n{_evidence}"
            _synth_chk = _jv.code_check(synth_final)
            if _synth_chk:
                _crit_ctx += f"\n\nTHE ANSWER'S {_synth_chk}"
                if _jv.has_problems(_synth_chk):
                    emit(AgentEvent("Judge", "observation",
                                    f"Final answer — {_synth_chk}"))
            critique = self.judge.critique(user_text, synth_final, extra_context=_crit_ctx, query_mode=ctx.shared.get("query_mode", ""))
            ctx.shared["judge_critique"] = critique
            ev = AgentEvent("Judge", "observation", critique)
            emit(ev)

            if "Verdict: PASS" in critique:
                self._phase("✓ Verdict: PASS — deliberation complete")
                break

            # ── Confidence-gated early exit ──────────────────────────────
            # Even on NEEDS_WORK, if Judge confidence is very high (≥80%)
            # and this is the final round, skip re-deliberation — the answer
            # is probably good enough and more rounds won't help much.
            _conf = ctx.shared.get("judge_confidence", 0)
            _is_last_round = (r == self.max_rounds - 1)
            if _conf >= _cf_HIGH and _is_last_round:
                self._phase(
                    f"✓ High confidence ({_conf}%) — accepting answer despite NEEDS_WORK"
                )
                break

            # ── Confidence-gated extra round ─────────────────────────────
            # If confidence is very low (≤20%) on round 1, allow an extra
            # round beyond max_rounds — the answer needs more work.
            if _conf <= _cf_VERY_LOW and r == 0 and self.max_rounds < 3:
                self._phase(
                    f"⚠ Low confidence ({_conf}%) — adding extra deliberation round"
                )
                self.max_rounds = 3

            # T2-C: the REQUIRED_CHANGES for the next round's Writer. Parsed
            # on every NEEDS_WORK (it used to be skipped exactly when
            # confidence was lowest — the `else` belonged to the branch
            # above).
            _changes = required_changes_of(self.judge, critique)
            ctx.shared["required_changes"] = _changes
            if _changes:
                _chg_txt = "\n".join("- " + c for c in _changes)
                emit(AgentEvent("Judge", "observation",
                               "Required changes for next round:\n" + _chg_txt))

            # Revise or start over? A critique that names what to change
            # gets a revision (Writer + Judge); one that rejects the
            # approach — or gives the Writer nothing to act on, or comes
            # with very low confidence — gets the whole panel again.
            _redo = needs_full_round(critique, _changes, _conf)
            _revising = not _redo
            if r + 1 < self.max_rounds:
                emit(AgentEvent(
                    "Orchestrator", "observation",
                    "Next round: the whole panel again — "
                    + _redo if _redo else
                    "Next round: the Writer revises against the critique "
                    "(no new drafts)."))

        # Expose shared context so caller can retrieve low-confidence gaps etc.
        self._last_ctx = ctx
        return all_events
