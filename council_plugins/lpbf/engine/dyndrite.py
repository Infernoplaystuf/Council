"""
The connection to Dyndrite's MCP server, and the rules for its tools.

COUNCIL IS THE CLIENT; THE MODEL ONLY PROPOSES
The server's token is read by Council (the plugin's ``mcp`` service, council_
core.secret_source) from an environment variable or the Windows Credential
Manager, and sent by Council as a header (HTTP) or an environment variable
of the server process (stdio). The settings saved here hold only WHERE the
token is ("env:DYNDRITE_TOKEN"), and the model is never shown it.

EVERY TOOL HAS A RULE (`rule_for`)
    read     answers questions, changes nothing: the assistant may call it
    change   creates, edits, exports, runs: never called by the assistant;
             a person files a request, an engineer approves it, then it runs
             once (engine.store tool_requests)
    blocked  never called
A tool's rule comes from the user's override, else from the server's own
MCP annotations (readOnlyHint / destructiveHint), else from its name. A tool
that matches neither is a CHANGE tool: unknown means "ask a person".

ASKING (`ask`) — orchestrated, for 8-20B local models
Council lists the read tools; the model answers with one JSON step at a
time — call a read tool with arguments, or give the answer — constrained by
a schema whose tool names are only the read tools. Council checks the
arguments against the tool's input schema, calls it, and hands back the
text (trimmed). At most `max_steps` calls. The answer cites the tool
results it used.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

SETTINGS_FILE = "dyndrite.json"
READ, CHANGE, BLOCKED = "read", "change", "blocked"
RULES = (READ, CHANGE, BLOCKED)

DEFAULT_SETTINGS: Dict[str, Any] = {
    "name": "Dyndrite",
    "transport": "http",
    "url": "http://127.0.0.1:8000/mcp",
    "command": "",
    "args": [],
    "env": {},
    "cwd": "",
    "allow_remote": False,
    "timeout": 60.0,
    "headers": {},
    "token_source": "env:DYNDRITE_TOKEN",
    "token_header": "Authorization",
    "token_scheme": "Bearer",
    "token_env": "DYNDRITE_TOKEN",
}

_READ_WORDS = ("get", "list", "search", "query", "read", "describe", "find",
               "help", "doc", "docs", "documentation", "show", "explain",
               "lookup", "info", "syntax", "example", "examples", "inspect",
               "status", "version", "schema", "fetch", "view")
_CHANGE_WORDS = ("create", "set", "update", "delete", "remove", "run",
                 "execute", "exec", "export", "write", "save", "generate",
                 "slice", "apply", "import", "assign", "build", "add", "place",
                 "move", "load", "open", "close", "start", "stop", "send",
                 "upload", "modify", "edit", "rename", "copy", "toolpath",
                 "script", "eval", "python")

MAX_RESULT_CHARS = 6000


def load_settings(ctx) -> Dict[str, Any]:
    data = ctx.load_json(SETTINGS_FILE, None)
    out = dict(DEFAULT_SETTINGS)
    if isinstance(data, dict):
        out.update({k: v for k, v in data.get("server", {}).items()
                    if k in DEFAULT_SETTINGS})
    return out


def load_rules(ctx) -> Dict[str, str]:
    data = ctx.load_json(SETTINGS_FILE, None)
    rules = data.get("rules", {}) if isinstance(data, dict) else {}
    return {str(k): v for k, v in rules.items() if v in RULES}


def save(ctx, server: Dict[str, Any], rules: Dict[str, str]) -> None:
    clean = {k: server.get(k, v) for k, v in DEFAULT_SETTINGS.items()}
    ctx.save_json(SETTINGS_FILE, {"format": 1, "server": clean,
                                  "rules": {k: v for k, v in rules.items()
                                            if v in RULES}})


def _words(name: str) -> List[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name or ""))
    return [w for w in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if w]


def guess_rule(tool: Dict[str, Any]) -> str:
    """The rule from the server's annotations, else the tool's name."""
    ann = tool.get("annotations") or {}
    if isinstance(ann, dict):
        if ann.get("destructiveHint") is True:
            return CHANGE
        if ann.get("readOnlyHint") is True:
            return READ
        if ann.get("readOnlyHint") is False:
            return CHANGE
    words = _words(tool.get("name", ""))
    if any(w in _CHANGE_WORDS for w in words):
        return CHANGE
    if (words and words[0] in _READ_WORDS) or any(
            w in ("doc", "docs", "documentation", "help", "syntax")
            for w in words):
        return READ
    return CHANGE


def rule_for(tool: Dict[str, Any], overrides: Dict[str, str]) -> str:
    return overrides.get(str(tool.get("name", ""))) or guess_rule(tool)


def read_tools(tools: Sequence[Dict[str, Any]], overrides: Dict[str, str]
               ) -> List[Dict[str, Any]]:
    return [t for t in tools if rule_for(t, overrides) == READ]


# ----------------------------------------------------------------------
# Arguments
# ----------------------------------------------------------------------
_TYPES = {"string": str, "integer": int, "number": (int, float),
          "boolean": bool, "object": dict, "array": list}


def argument_problem(tool: Dict[str, Any], arguments: Any) -> str:
    """Why `arguments` do not fit the tool's input schema, or ""."""
    if not isinstance(arguments, dict):
        return "arguments must be a JSON object"
    schema = tool.get("inputSchema") or {}
    props = schema.get("properties") or {}
    for name in schema.get("required") or []:
        if name not in arguments:
            return f"{tool.get('name')} needs {name!r}"
    if props and schema.get("additionalProperties") is False:
        extra = sorted(set(arguments) - set(props))
        if extra:
            return f"{tool.get('name')} has no argument {extra[0]!r}"
    for name, value in arguments.items():
        want = (props.get(name) or {}).get("type")
        if isinstance(want, str) and want in _TYPES:
            ok = isinstance(value, _TYPES[want])
            if want in ("integer", "number") and isinstance(value, bool):
                ok = False
            if not ok:
                return f"{name!r} should be {want}"
        enum = (props.get(name) or {}).get("enum")
        if isinstance(enum, list) and value not in enum:
            return f"{name!r} must be one of {enum}"
    return ""


# ----------------------------------------------------------------------
# Asking
# ----------------------------------------------------------------------
@dataclass
class Step:
    tool: str
    arguments: Dict[str, Any]
    text: str = ""
    error: str = ""


@dataclass
class Answer:
    answer: str
    steps: List[Step] = field(default_factory=list)
    note: str = ""


def _tool_lines(tools: Sequence[Dict[str, Any]]) -> str:
    out = []
    for t in tools:
        schema = json.dumps(t.get("inputSchema") or {}, separators=(",", ":"))
        if len(schema) > 600:
            schema = schema[:600] + "…"
        desc = str(t.get("description") or "").strip().replace("\n", " ")
        out.append(f"- {t.get('name')}: {desc[:300]}\n  arguments: {schema}")
    return "\n".join(out)


def step_schema(names: Sequence[str]) -> Dict[str, Any]:
    return {"type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["call", "answer"]},
                "tool": {"type": "string", "enum": list(names) or [""]},
                "arguments": {"type": "object"},
                "answer": {"type": "string"}},
            "required": ["action"]}


def _parse(text: str) -> Dict[str, Any]:
    text = str(text or "").strip()
    try:
        data = json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0)) if m else {}
    return data if isinstance(data, dict) else {}


def ask(question: str, tools: Sequence[Dict[str, Any]],
        overrides: Dict[str, str], chat: Callable[..., str],
        call: Callable[[str, Dict[str, Any]], Dict[str, Any]], *,
        max_steps: int = 3,
        should_stop: Optional[Callable[[], bool]] = None) -> Answer:
    """Answer `question` with the server's READ tools only.

    `call(tool, arguments) -> {text, is_error}` is the plugin's mcp service;
    it is never handed a tool that is not a read tool."""
    allowed = read_tools(tools, overrides)
    by_name = {str(t.get("name")): t for t in allowed}
    if not allowed:
        return Answer("", note="The server has no tool marked 'read', so the "
                      "assistant cannot ask it anything. Mark the "
                      "documentation or query tools as 'read' on the "
                      "Dyndrite page.")
    system = (
        "You answer an engineer's question about Dyndrite LPBF Pro using the "
        "Dyndrite server's read-only tools. Reply with ONE JSON object: "
        '{"action":"call","tool":<name>,"arguments":{...}} to look '
        'something up, or {"action":"answer","answer":<text>} when the '
        "results so far answer the question. Use only what the tools "
        "returned; if they do not cover it, say so. Never write recipe code "
        "that you have not seen in a tool result.\nTools:\n"
        + _tool_lines(allowed))
    messages: List[Dict[str, str]] = [{"role": "system", "content": system},
                                      {"role": "user", "content": question}]
    steps: List[Step] = []
    schema = step_schema(list(by_name))
    for _ in range(max(1, max_steps) + 1):
        if should_stop and should_stop():
            return Answer("", steps, note="Stopped.")
        data = _parse(chat(messages, max_tokens=700, schema=schema))
        if data.get("action") == "answer" or len(steps) >= max_steps:
            text = str(data.get("answer") or "").strip()
            if data.get("action") != "answer" or not text:
                messages.append({"role": "user", "content":
                                 "Give your answer now as "
                                 '{"action":"answer","answer":...}.'})
                data = _parse(chat(messages, max_tokens=700, schema=schema))
                text = str(data.get("answer") or "").strip()
            return Answer(text or "The tools did not give an answer.", steps)
        name = str(data.get("tool") or "")
        args = data.get("arguments") if isinstance(data.get("arguments"), dict) \
            else {}
        step = Step(name, args)
        if name not in by_name:
            step.error = (f"{name!r} is not one of the read tools; only "
                          f"{', '.join(by_name)} may be used.")
        else:
            step.error = argument_problem(by_name[name], args)
        if not step.error:
            try:
                got = call(name, args)
                step.text = str(got.get("text", ""))[:MAX_RESULT_CHARS]
                if got.get("is_error"):
                    step.error = "the tool reported an error: " + step.text[:300]
            except Exception as exc:                          # noqa: BLE001
                step.error = f"the call failed: {exc}"
        steps.append(step)
        messages.append({"role": "assistant",
                         "content": json.dumps({"action": "call", "tool": name,
                                                "arguments": args})})
        messages.append({"role": "user", "content":
                         (f"Result of {name}:\n{step.text}" if not step.error
                          else f"That call was not made: {step.error}")})
    return Answer("The tools did not give an answer.", steps)
