"""
council_core.structured_output — JSON a model must return, checked the same
way on every backend.

WHY
Every JSON consumer in the app (Describe, Classify, TaskChain's plan and
verdict, the agent's tool calls) used to scrape JSON out of free text and pay
a whole extra generation for each repair. local_chat(json_schema=...) now
CONSTRAINS the output instead — a llama.cpp grammar on the GGUF path, Ollama's
"format" on the Ollama path — and this module holds the parts that do not
depend on either: finding the JSON in a reply, validating it, estimating how
long the longest valid answer can be, and the schema behind emulated tool
calls.

VALIDATION
jsonschema when it is installed (the council env has 4.26). Without it — the
system Python that has llama-cpp-python but nothing else — a small validator
covering the keywords these schemas use (type, enum, const, required,
properties, additionalProperties, items, min/maxItems, min/maxLength,
minimum/maximum, anyOf/oneOf/allOf) answers instead, so a model's reply is
never accepted unchecked just because a package is missing.

TRUNCATION IS THE ONE THING A GRAMMAR CANNOT FIX
A grammar keeps every token inside the schema, but a reply cut off at
num_predict is still half an object. worst_case_tokens() says how long the
longest valid reply can be; a schema with an unbounded array or string has no
worst case (None), which is the signal to add maxItems / maxLength — bounded()
adds them where they are missing.

No toolkit, no model, no network: pure functions, testable anywhere.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

#: Characters of minified JSON per token — measured on phi3.5's tokenizer for
#: grammar-constrained wireframe output (38.8 tokens per ~105-char shape);
#: conservative for bigger vocabularies.
CHARS_PER_TOKEN = 2.5


def schema_key(schema: Any) -> str:
    """A stable short hash of a schema, for caching its compiled grammar."""
    text = json.dumps(schema, sort_keys=True, separators=(",", ":"),
                      default=str)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


# ============================================================
# Finding the JSON in a reply
# ============================================================

_FENCE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)```", re.DOTALL)

#: How many opening brackets the balanced-span scan tries per bracket kind.
#: Each try walks to the end of the reply, so an unbounded count made a
#: degenerate reply ("{{{{…" — a small model stuck repeating one token)
#: quadratic: measured 4.2 s for 8,000 braces, and the Describe worker sat in
#: it. A real reply's JSON starts within the first few brackets.
MAX_SPAN_STARTS = 64

#: What a model's reply can make json.loads raise. Nesting deeper than the
#: interpreter's recursion limit ("[[[[…]]]]", a few thousand brackets)
#: escapes the C decoder as RecursionError, not ValueError — and escaped
#: through parse_json into chat_tools' caller.
_BAD_JSON = (ValueError, RecursionError)


def _loads_ok(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except _BAD_JSON:
        return False


def extract_json_text(text: str) -> Optional[str]:
    """The JSON value in ``text``: the whole reply when it parses (what a
    constrained reply is), else a fenced block, else the first balanced
    {...} / [...] span. None when there is none. Never raises."""
    if text is None:
        return None
    s = text.strip()
    if not s:
        return None
    if _loads_ok(s):
        return s
    for m in _FENCE.finditer(s):
        inner = m.group(1).strip()
        if _loads_ok(inner):
            return inner
    for opener, closer in (("{", "}"), ("[", "]")):
        start = s.find(opener)
        tries = 0
        while start != -1 and tries < MAX_SPAN_STARTS:
            tries += 1
            span = _balanced(s, start, opener, closer)
            if span is not None and _loads_ok(span):
                return span
            start = s.find(opener, start + 1)
    return None


def _balanced(s: str, start: int, opener: str, closer: str) -> Optional[str]:
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return None


def parse_json(text: str) -> Tuple[Any, str]:
    """(value, "") or (None, why)."""
    found = extract_json_text(text)
    if found is None:
        return None, "no JSON value in the reply"
    try:
        return json.loads(found), ""
    except _BAD_JSON as exc:                              # pragma: no cover
        return None, f"invalid JSON: {exc}"


# ============================================================
# Validation
# ============================================================

def validate(value: Any, schema: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """(ok, problems) — jsonschema when importable, else the mini validator."""
    try:
        import jsonschema  # type: ignore[import]
    except Exception:                                     # noqa: BLE001
        errs: List[str] = []
        _mini(value, schema, "$", errs)
        return not errs, errs[:20]
    try:
        cls = jsonschema.validators.validator_for(schema)
        errors = sorted(cls(schema).iter_errors(value),
                        key=lambda e: list(e.absolute_path))
    except Exception as exc:                              # noqa: BLE001
        return False, [f"schema could not be used: {exc}"]
    out = []
    for e in errors[:20]:
        where = "$" + "".join(f"[{p!r}]" for p in e.absolute_path)
        out.append(f"{where}: {e.message}")
    return not errors, out


def check_text(text: str, schema: Dict[str, Any]) -> Tuple[bool, List[str]]:
    value, why = parse_json(text)
    if why:
        return False, [why]
    return validate(value, schema)


_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool,
    "null": type(None),
}


def _is_type(value: Any, t: str) -> bool:
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    py = _TYPES.get(t)
    return True if py is None else isinstance(value, py)


def _mini(v: Any, s: Any, path: str, errs: List[str]) -> None:
    """The keywords this app's schemas use. Not a full JSON Schema."""
    if not isinstance(s, dict) or len(errs) > 50:
        return
    if "const" in s and v != s["const"]:
        errs.append(f"{path}: expected {s['const']!r}")
    if "enum" in s and v not in s["enum"]:
        errs.append(f"{path}: {v!r} is not one of {s['enum']!r}")
    t = s.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_is_type(v, x) for x in types):
            errs.append(f"{path}: expected {t}, got {type(v).__name__}")
            return
    for key in ("anyOf", "oneOf"):
        if key in s:
            ok = 0
            for sub in s[key]:
                sub_errs: List[str] = []
                _mini(v, sub, path, sub_errs)
                ok += not sub_errs
            if ok == 0 or (key == "oneOf" and ok > 1):
                errs.append(f"{path}: does not match {key}")
    for sub in s.get("allOf", []) or []:
        _mini(v, sub, path, errs)
    if isinstance(v, dict):
        props = s.get("properties") or {}
        for name in s.get("required", []) or []:
            if name not in v:
                errs.append(f"{path}: missing {name!r}")
        for name, sub in props.items():
            if name in v:
                _mini(v[name], sub, f"{path}.{name}", errs)
        extra = s.get("additionalProperties", True)
        for name in v:
            if name in props:
                continue
            if extra is False:
                errs.append(f"{path}: unexpected {name!r}")
            elif isinstance(extra, dict):
                _mini(v[name], extra, f"{path}.{name}", errs)
    if isinstance(v, list):
        if "minItems" in s and len(v) < s["minItems"]:
            errs.append(f"{path}: fewer than {s['minItems']} items")
        if "maxItems" in s and len(v) > s["maxItems"]:
            errs.append(f"{path}: more than {s['maxItems']} items")
        items = s.get("items")
        if isinstance(items, dict):
            for i, item in enumerate(v):
                _mini(item, items, f"{path}[{i}]", errs)
    if isinstance(v, str):
        if "minLength" in s and len(v) < s["minLength"]:
            errs.append(f"{path}: shorter than {s['minLength']}")
        if "maxLength" in s and len(v) > s["maxLength"]:
            errs.append(f"{path}: longer than {s['maxLength']}")
        if "pattern" in s and not re.search(s["pattern"], v):
            errs.append(f"{path}: does not match {s['pattern']!r}")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if "minimum" in s and v < s["minimum"]:
            errs.append(f"{path}: below {s['minimum']}")
        if "maximum" in s and v > s["maximum"]:
            errs.append(f"{path}: above {s['maximum']}")


# ============================================================
# Bounds — so a constrained reply fits num_predict
# ============================================================

def bounded(schema: Dict[str, Any], *, max_items: int = 64,
            max_length: int = 400) -> Dict[str, Any]:
    """A copy of ``schema`` with maxItems on every array and maxLength on
    every string that lacks one. Enums and consts are left alone."""
    out = copy.deepcopy(schema)

    def walk(s: Any) -> None:
        if isinstance(s, list):
            for x in s:
                walk(x)
            return
        if not isinstance(s, dict):
            return
        t = s.get("type")
        types = t if isinstance(t, list) else [t]
        if "array" in types and "maxItems" not in s:
            s["maxItems"] = max_items
        if ("string" in types and "maxLength" not in s and "enum" not in s
                and "const" not in s and "pattern" not in s):
            s["maxLength"] = max_length
        for key in ("properties", "$defs", "definitions"):
            if isinstance(s.get(key), dict):
                for sub in s[key].values():
                    walk(sub)
        for key in ("items", "additionalProperties", "anyOf", "oneOf",
                    "allOf", "prefixItems"):
            if key in s:
                walk(s[key])

    walk(out)
    return out


def worst_case_chars(schema: Any, _depth: int = 0) -> Optional[int]:
    """Characters of the longest minified value ``schema`` allows, or None
    when something in it is unbounded (an array without maxItems, a string
    without maxLength, a free-form object)."""
    if _depth > 32 or not isinstance(schema, dict):
        return None
    if "const" in schema:
        return len(json.dumps(schema["const"]))
    if "enum" in schema:
        return max((len(json.dumps(x)) for x in schema["enum"]), default=2)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            subs = [worst_case_chars(x, _depth + 1) for x in schema[key]]
            return None if any(x is None for x in subs) else max(subs or [0])
    t = schema.get("type")
    if isinstance(t, list):
        subs = [worst_case_chars(dict(schema, type=x), _depth + 1) for x in t]
        return None if any(x is None for x in subs) else max(subs or [0])
    if t == "boolean":
        return 5
    if t == "null":
        return 4
    if t in ("integer", "number"):
        hi = max(abs(int(schema.get("maximum", 0) or 0)),
                 abs(int(schema.get("minimum", 0) or 0)))
        return (len(str(hi)) + 1) if ("maximum" in schema
                                      and "minimum" in schema) else 20
    if t == "string":
        n = schema.get("maxLength")
        return None if n is None else int(n) * 2 + 2   # escapes
    if t == "array":
        n = schema.get("maxItems")
        item = worst_case_chars(schema.get("items") or {}, _depth + 1) \
            if schema.get("items") else None
        if n is None or item is None:
            return None
        return 2 + int(n) * (item + 1)
    if t == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        if schema.get("additionalProperties", True) is not False and \
                not props:
            return None
        total = 2
        for name, sub in props.items():
            w = worst_case_chars(sub, _depth + 1)
            if w is None:
                return None
            total += len(json.dumps(name)) + 2 + w
        return total
    return None


def worst_case_tokens(schema: Any) -> Optional[int]:
    chars = worst_case_chars(schema)
    return None if chars is None else int(chars / CHARS_PER_TOKEN) + 8


# ============================================================
# Tool calls, for models without native tool calling
# ============================================================

def normalize_tools(tools: Sequence[Any]) -> List[Dict[str, Any]]:
    """Ollama / OpenAI shaped ({"type": "function", "function": {...}}),
    plain ({"name", "description", "parameters"}) or an MCP server's
    tools/list entry ({"name", "description", "inputSchema"}) -> plain."""
    out = []
    for t in tools or ():
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = str(fn.get("name") or "").strip()
        if not name:
            continue
        params = (fn.get("parameters") or fn.get("inputSchema")
                  or fn.get("input_schema")
                  or {"type": "object", "properties": {}})
        out.append({"name": name,
                    "description": str(fn.get("description") or ""),
                    "parameters": params})
    return out


def tool_choice_schema(tools: Sequence[Dict[str, Any]], *,
                       answer_max: int = 4000) -> Dict[str, Any]:
    """{"tool": <one of the names>, "arguments": {...that tool's schema}} or
    {"answer": "..."} — each tool its own variant, so the arguments are
    constrained to THAT tool's parameters, not to any tool's."""
    variants = []
    defs: Dict[str, Any] = {}
    for i, t in enumerate(normalize_tools(tools)):
        variants.append({
            "type": "object",
            "properties": {"tool": {"const": t["name"]},
                           "arguments": _hoist_defs(t["parameters"], i,
                                                    defs)},
            "required": ["tool", "arguments"],
            "additionalProperties": False,
        })
    variants.append({
        "type": "object",
        "properties": {"answer": {"type": "string", "maxLength": answer_max}},
        "required": ["answer"],
        "additionalProperties": False,
    })
    out: Dict[str, Any] = {"anyOf": variants}
    if defs:
        out["$defs"] = defs
    return out


_LOCAL_REF = re.compile(r"^#/(\$defs|definitions)/(.+)$")


def _hoist_defs(params: Any, index: int, defs: Dict[str, Any]) -> Any:
    """``params`` with its own $defs / definitions moved into ``defs`` (the
    combined schema's ROOT) under a per-tool prefix, and its "#/$defs/X"
    references rewritten to match.

    WHY: an MCP server's inputSchema is usually generated by pydantic
    (FastMCP), which puts nested models in "$defs" and points at them with
    "#/$defs/Name" — a pointer from the DOCUMENT root. Nested under
    {"anyOf": [{"properties": {"arguments": <here>}}]} that root is the
    combined schema, where the pointer leads nowhere: jsonschema refused the
    whole schema (every emulated call reported invalid) and a grammar
    converter cannot build it. A "#" (whole-schema) reference becomes a
    reference to the tool's own hoisted copy."""
    if not isinstance(params, dict):
        return params
    own = {}
    for key in ("$defs", "definitions"):
        if isinstance(params.get(key), dict):
            own.update({(key, n): s for n, s in params[key].items()})
    prefix = f"t{index}_"
    root_name = f"{prefix}_root"

    def fix(node: Any) -> Any:
        if isinstance(node, list):
            return [fix(x) for x in node]
        if not isinstance(node, dict):
            return node
        out = {}
        for k, v in node.items():
            if k in ("$defs", "definitions") and node is params:
                continue
            if k == "$ref" and isinstance(v, str):
                if v == "#":
                    out[k] = f"#/$defs/{root_name}"
                    defs.setdefault(root_name, None)
                    continue
                m = _LOCAL_REF.match(v)
                if m and (m.group(1), m.group(2)) in own:
                    out[k] = f"#/$defs/{prefix}{m.group(2)}"
                    continue
            out[k] = fix(v)
        return out

    body = fix(params)
    for (_kind, name), sub in own.items():
        defs[prefix + name] = fix(sub)
    if root_name in defs:
        defs[root_name] = body
    return body


def tool_prompt(tools: Sequence[Dict[str, Any]]) -> str:
    """The system text that describes the tools to a model that has no
    native tool calling."""
    lines = ["You can call these tools. To call one, reply with ONLY a JSON "
             'object {"tool": "<name>", "arguments": {...}}. When you can '
             'answer without a tool, reply with ONLY {"answer": "<text>"}.',
             "", "Tools:"]
    for t in normalize_tools(tools):
        lines.append(f"- {t['name']}: {t['description']}".rstrip(": "))
        lines.append("  arguments schema: " + json.dumps(
            t["parameters"], separators=(",", ":")))
    return "\n".join(lines)
