"""
council_core.council_schemas — the shapes the council's structured replies
must take, and how they are turned back into the text the rest reads.

Three replies used to be free text that the code then repaired:

  * the Judge's RANKING asked for "ONLY valid JSON" and a parser dug a
    {...} out of whatever came back — "winner: unknown" when it could not;
  * the Judge's CRITIQUE had to follow a "Verdict: … REQUIRED_CHANGES: …"
    layout that a regex then read;
  * the Peasant's QUESTIONS needed "Q1: … Q2: …", and a whole second call
    was spent to reformat a reply that lacked the labels.

Each now goes out with a JSON schema (council_engine: a llama.cpp grammar
for an in-app model, Ollama's "format" for a served one), so the reply has
the shape by construction. The render functions turn it back into the text
everything downstream already reads — the verdict line, the bullet list of
required changes, Q1/Q2 — so nothing else had to change. When a backend
cannot constrain a reply (the engine falls back to plain text) the parse
fails and the reply is used as it came, as before.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional


def _parse(text: str) -> Optional[Any]:
    try:
        from .structured_output import parse_json
        value, _why = parse_json(text or "")
        return value
    except Exception:                                     # noqa: BLE001
        try:
            return json.loads(text)
        except Exception:                                 # noqa: BLE001
            return None


def ranking_schema(roles: Iterable[str]) -> Dict[str, Any]:
    roles = [str(r) for r in roles]
    pct = {"type": "integer", "minimum": 0, "maximum": 100}
    return {
        "type": "object",
        "properties": {
            "winner": {"type": "string", "enum": roles} if roles
            else {"type": "string"},
            "scores": {"type": "object",
                       "properties": {r: pct for r in roles},
                       "required": roles},
            "rationale": {"type": "string"},
            "confidence": pct,
        },
        "required": ["winner", "scores", "rationale", "confidence"],
    }


CRITIQUE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "NEEDS_WORK"]},
        "findings": {"type": "array", "items": {"type": "string"}},
        "suggestions": {"type": "array", "items": {"type": "string"}},
        "required_changes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "findings", "required_changes"],
}

CRITIQUE_JSON_NOTE = (
    "Reply as JSON: {\"verdict\": \"PASS\" | \"NEEDS_WORK\", \"findings\": "
    "[...], \"suggestions\": [...], \"required_changes\": [...]} — "
    "required_changes empty when the verdict is PASS.")


def render_critique(text: str) -> str:
    """A JSON critique in the classic text layout; anything else as is."""
    obj = _parse(text)
    if not isinstance(obj, dict) or obj.get("verdict") not in (
            "PASS", "NEEDS_WORK"):
        return text
    lines = ["=== Judge Critique ===", f"Verdict: {obj['verdict']}",
             "Findings:"]
    lines += [f"- {f}" for f in obj.get("findings") or []] or ["- (none)"]
    if obj.get("suggestions"):
        lines.append("Suggestions:")
        lines += [f"- {x}" for x in obj["suggestions"]]
    changes = [c for c in obj.get("required_changes") or [] if str(c).strip()]
    if obj["verdict"] == "NEEDS_WORK" and changes:
        lines.append("REQUIRED_CHANGES:")
        lines += [f"- {c}" for c in changes]
    lines.append("========================")
    return "\n".join(lines)


_PAIR = {"type": "object",
         "properties": {"q1": {"type": "string"}, "q2": {"type": "string"}},
         "required": ["q1", "q2"]}

QUESTIONS_SCHEMA: Dict[str, Any] = _PAIR


def turn_questions_schema(roles: Iterable[str]) -> Dict[str, Any]:
    roles = [str(r).lower() for r in roles]
    return {"type": "object", "properties": {r: _PAIR for r in roles},
            "required": roles}


def _q(text: Any) -> str:
    t = str(text or "").strip()
    return t if t.endswith("?") or not t else t + "?"


def render_questions(text: str) -> str:
    """{"q1", "q2"} as "Q1: …? / Q2: …?"; anything else as is."""
    obj = _parse(text)
    if isinstance(obj, dict) and obj.get("q1") and obj.get("q2"):
        return f"Q1: {_q(obj['q1'])}\nQ2: {_q(obj['q2'])}"
    return text


def render_turn_questions(text: str, roles: Iterable[str]) -> str:
    """{role: {q1, q2}} as "TO ROLE:" blocks; anything else as is."""
    obj = _parse(text)
    if not isinstance(obj, dict):
        return text
    out: List[str] = []
    for r in roles:
        pair = obj.get(r) or obj.get(str(r).lower()) or obj.get(str(r).upper())
        if isinstance(pair, dict) and pair.get("q1"):
            out.append(f"TO {str(r).upper()}:\nQ1: {_q(pair.get('q1'))}\n"
                       f"Q2: {_q(pair.get('q2'))}")
    return "\n".join(out) if out else text


__all__ = ["ranking_schema", "CRITIQUE_SCHEMA", "CRITIQUE_JSON_NOTE",
           "render_critique", "QUESTIONS_SCHEMA", "turn_questions_schema",
           "render_questions", "render_turn_questions"]
