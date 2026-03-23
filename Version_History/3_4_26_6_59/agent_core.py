# ============================================================
# Conda env:
#   conda create -n council python=3.11 -y
#   conda activate council
# ============================================================

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
import json
import re


# -----------------------------
# Data structures
# -----------------------------
@dataclass
class AgentEvent:
    who: str
    kind: str   # "thought" | "action" | "observation" | "final"
    text: str


@dataclass
class AgentContext:
    user_text: str
    shared: Dict[str, Any] = field(default_factory=dict)
    events: List[AgentEvent] = field(default_factory=list)


# Tool signature:
# returns (ok, message_for_model, payload_for_ctx)
ToolFn = Callable[[Dict[str, Any]], Tuple[bool, str, Dict[str, Any]]]


# -----------------------------
# Base agent
# -----------------------------
class BaseAgent:
    def __init__(self, name: str):
        self.name = name

    def act(self, ctx: AgentContext) -> List[AgentEvent]:
        raise NotImplementedError


# -----------------------------
# Tool protocol (simple + robust)
# -----------------------------
# Agent can request tools by emitting a JSON object anywhere in text:
#
#   {"tool": "run_python", "args": {"code": "..."}}
#
# Or multiple:
#   {"tool_calls":[{"tool":"run_python","args":{...}}, ...]}
#
_TOOL_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_tool_calls(text: str) -> List[Dict[str, Any]]:
    """
    Best-effort parse: scan for JSON objects and pick ones with tool/tool_calls.
    Keeps it permissive so models don't need perfect formatting.
    """
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


# -----------------------------
# Model-backed agent (optionally tool-using)
# -----------------------------
class ModelAgent(BaseAgent):
    def __init__(
        self,
        name: str,
        model_backend: Any,  # must have .respond(str)->str
        *,
        tools: Optional[Dict[str, ToolFn]] = None,
        enable_tools: bool = False,
        max_tool_steps: int = 3,
    ):
        super().__init__(name)
        self.backend = model_backend
        self.tools = tools or {}
        self.enable_tools = enable_tools
        self.max_tool_steps = max_tool_steps

    def _compose_prompt(self, ctx: AgentContext) -> str:
        panel = ctx.shared.get("panel_outputs", {})
        critique = ctx.shared.get("judge_critique", "")

        tool_instructions = ""
        if self.enable_tools and self.tools:
            tool_list = ", ".join(sorted(self.tools.keys()))
            tool_instructions = (
                "\n\nTOOLS AVAILABLE:\n"
                f"- {tool_list}\n\n"
                "If you want to use a tool, output ONLY a JSON object like:\n"
                '{"tool":"run_python","args":{"code":"print(123)"}}\n'
                "Or multiple calls:\n"
                '{"tool_calls":[{"tool":"...","args":{...}}, ...]}\n'
                "Otherwise, write a normal answer.\n"
            )

        panel_notes = ""
        if panel:
            panel_notes = "COUNCIL PANEL NOTES:\n" + "\n".join([f"- {k}: {v}" for k, v in panel.items()]) + "\n\n"

        judge_notes = ""
        if critique:
            judge_notes = f"JUDGE CRITIQUE FROM PRIOR ROUND:\n{critique}\n\n"

        return panel_notes + judge_notes + "USER REQUEST:\n" + ctx.user_text + tool_instructions

    def act(self, ctx: AgentContext) -> List[AgentEvent]:
        events: List[AgentEvent] = []

        prompt = self._compose_prompt(ctx)
        text = self.backend.respond(prompt)

        # If tools disabled, return final
        if not (self.enable_tools and self.tools):
            return [AgentEvent(self.name, "final", text)]

        # Tool loop
        for step in range(self.max_tool_steps):
            calls = _extract_tool_calls(text)
            if not calls:
                events.append(AgentEvent(self.name, "final", text))
                return events

            events.append(AgentEvent(self.name, "action", f"Tool calls requested ({len(calls)})."))

            tool_obs: List[str] = []
            tool_payloads: Dict[str, Any] = {}

            for i, call in enumerate(calls, start=1):
                tool_name = str(call.get("tool", "")).strip()
                args = call.get("args", {}) if isinstance(call.get("args", {}), dict) else {}

                if tool_name not in self.tools:
                    tool_obs.append(f"[{i}] ERROR: tool '{tool_name}' not allowed.")
                    continue

                ok, msg, payload = self.tools[tool_name](args)
                tool_obs.append(f"[{i}] {tool_name}: {'OK' if ok else 'FAIL'}\n{msg}")
                if payload:
                    tool_payloads[f"{tool_name}_{i}"] = payload

            # Store tool outputs into ctx for synthesis
            ctx.shared.setdefault("tool_payloads", {}).update(tool_payloads)

            obs_text = "\n\n".join(tool_obs)
            events.append(AgentEvent(self.name, "observation", obs_text))

            # Ask model to continue with tool results
            followup = (
                "TOOL RESULTS:\n"
                f"{obs_text}\n\n"
                "Now produce the best possible answer for the user (no tool JSON unless more tools are needed)."
            )
            text = self.backend.respond(followup)

        # Max steps reached
        events.append(AgentEvent(self.name, "final", text))
        return events


# -----------------------------
# Deliberation orchestrator
# -----------------------------
class DeliberationOrchestrator:
    def __init__(self, *, judge_backend: Any, agents: Dict[str, BaseAgent], max_rounds: int = 2):
        self.judge = judge_backend  # must have .critique(user_text, resp)->str
        self.agents = agents
        self.max_rounds = max_rounds

    def run(self, user_text: str, *, panel: List[str], synth: str = "writer") -> List[AgentEvent]:
        ctx = AgentContext(user_text=user_text)
        events: List[AgentEvent] = []

        for r in range(self.max_rounds):
            events.append(AgentEvent("Orchestrator", "thought", f"Deliberation round {r+1}/{self.max_rounds}"))

            panel_outputs: Dict[str, str] = {}
            for name in panel:
                evs = self.agents[name].act(ctx)
                events.extend(evs)
                finals = [e.text for e in evs if e.kind == "final"]
                if finals:
                    panel_outputs[name] = finals[-1]

            ctx.shared["panel_outputs"] = panel_outputs

            # Synthesis
            synth_events = self.agents[synth].act(ctx)
            events.extend(synth_events)
            synth_final = next((e.text for e in reversed(synth_events) if e.kind == "final"), "")

            # Judge critique
            critique = self.judge.critique(user_text, synth_final) if hasattr(self.judge, "critique") else ""
            ctx.shared["judge_critique"] = critique
            events.append(AgentEvent("Judge", "observation", critique))

            # Stop early if PASS
            if "Verdict: PASS" in critique:
                break

        return events