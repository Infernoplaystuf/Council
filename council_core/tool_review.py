"""
council_core.tool_review — a model-built tool goes from "unreviewed" to
"a tool the council uses", only through the user.

Tool Forge already writes a tool from a description, runs it through the
analyst sandbox's validator, test-runs it with no arguments and saves it in
<vault>/App_Built_tools/ — UNREVIEWED, and that is where it stopped: no
council member could call it, and nothing said whether it worked.

    TESTS      `write_tests` asks the model for 2–3 example calls with the
               kind of result each must give (a number, text, a table…, or
               an exact value for a pure function), saved beside the tool
               as <name>.tests.json. `run_tests` runs them in the sandbox.
    APPROVAL   `approve(name, roles)` — the USER's action, offered only
               when every test passes — records the code's fingerprint and
               the roles that may call it (approved.json). An approved tool
               joins those roles' tool lists (council_tools.make_tools) as
               app_<name>, with its arguments read from its signature.
               Editing the code changes the fingerprint: the tool is
               unapproved again until the user approves the new version.
    VERSIONS   app_built_tools keeps every replaced version under
               versions/<name>-<time>.py.
    PROPOSALS  `task_from_proposal` turns a tool-gap proposal ("the Intern
               asked for X six times") into a Forge description, so a
               recurring gap becomes a tool in one step.

Nothing here relaxes the sandbox: an approved tool runs exactly as an
unreviewed one does (app_built_tools.run_tool → the analyst sandbox).
"""
from __future__ import annotations

import ast
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

APPROVED_FILE = "approved.json"
KINDS = ("number", "text", "bool", "list", "dict", "table", "any")

TESTS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {"tests": {"type": "array", "items": {
        "type": "object",
        "properties": {"args": {"type": "object"},
                       "expect": {"type": "string", "enum": list(KINDS)},
                       "equals": {},
                       "why": {"type": "string"}},
        "required": ["args", "expect"]}}},
    "required": ["tests"],
}

TESTS_PROMPT = """Write 2 or 3 test calls for this tool. Each: "args" \
(keyword arguments — use the defaults when unsure; {{}} calls it with \
none), "expect" (the kind of result: number, text, bool, list, dict, \
table or any) and, ONLY when the result is certain without seeing the \
user's data (a pure calculation), "equals" with the exact value.

TOOL TASK: {task}

CODE:
{code}

Reply with JSON only: {{"tests": [{{"args": {{}}, "expect": "number"}}]}}"""


def _abt():
    import app_built_tools
    return app_built_tools


def fingerprint(code: str) -> str:
    return hashlib.sha256((code or "").encode("utf-8")).hexdigest()[:16]


def _tests_path(name: str, vault_dir: Any) -> Path:
    return _abt().tools_dir(vault_dir) / f"{name}.tests.json"


def load_tests(name: str, vault_dir: Any) -> List[Dict[str, Any]]:
    try:
        data = json.loads(_tests_path(name, vault_dir).read_text(
            encoding="utf-8"))
        return [t for t in data if isinstance(t, dict)] \
            if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def save_tests(name: str, tests: List[Dict[str, Any]], vault_dir: Any) -> None:
    p = _tests_path(name, vault_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(tests, indent=2), encoding="utf-8")
    tmp.replace(p)


def write_tests(name: str, task: str, vault_dir: Any,
                model_call: Callable[..., str]) -> List[Dict[str, Any]]:
    """Ask the model for the tool's tests and save them."""
    from .council_schemas import _parse
    code = _abt().get_tool_code(name, vault_dir) or ""
    raw = model_call(TESTS_PROMPT.format(task=task, code=code[-6000:]))
    obj = _parse(raw)
    tests = []
    for t in (obj or {}).get("tests") or [] if isinstance(obj, dict) else []:
        if isinstance(t, dict) and isinstance(t.get("args", {}), dict):
            entry = {"args": t.get("args") or {},
                     "expect": t.get("expect") if t.get("expect") in KINDS
                     else "any"}
            if "equals" in t:
                entry["equals"] = t["equals"]
            tests.append(entry)
    if not tests:
        tests = [{"args": {}, "expect": "any"}]
    save_tests(name, tests[:3], vault_dir)
    return tests[:3]


def _value(df: Any) -> Any:
    """What the tool returned: run_tool wraps a scalar in a one-cell frame
    (numpy scalars are turned back into Python ones)."""
    try:
        if list(df.columns) == ["result"] and len(df) == 1:
            v = df["result"].iloc[0]
            return v.item() if hasattr(v, "item") else v
    except Exception:                                     # noqa: BLE001
        pass
    return df


def _kind_ok(value: Any, kind: str) -> bool:
    import numbers
    if kind == "any":
        return True
    if kind == "number":
        return isinstance(value, numbers.Number) and not isinstance(value, bool)
    if kind == "text":
        return isinstance(value, str)
    if kind == "bool":
        return isinstance(value, bool) or type(value).__name__ == "bool_"
    if kind == "list":
        return isinstance(value, (list, tuple))
    if kind == "dict":
        return isinstance(value, dict) or hasattr(value, "to_dict")
    if kind == "table":
        return hasattr(value, "columns")
    return True


def run_tests(name: str, vault_dir: Any,
              allowed_folders: Optional[List[Any]] = None
              ) -> List[Tuple[bool, str]]:
    """[(passed, what happened)] for each saved test."""
    out = []
    for t in load_tests(name, vault_dir) or [{"args": {}, "expect": "any"}]:
        df, msg = _abt().run_tool(name, t.get("args") or {},
                                  vault_dir=vault_dir,
                                  allowed_folders=allowed_folders)
        call = f"{name}(**{t.get('args') or {}})"
        if df is None:
            out.append((False, f"{call} failed: {msg[-300:]}"))
            continue
        value = _value(df)
        if not _kind_ok(value, t.get("expect", "any")):
            out.append((False, f"{call} returned {type(value).__name__}, "
                               f"expected {t.get('expect')}"))
            continue
        if "equals" in t and value != t["equals"]:
            out.append((False, f"{call} returned {value!r}, expected "
                               f"{t['equals']!r}"))
            continue
        shown = str(value)[:120] if not hasattr(value, "columns") else \
            f"a table {getattr(value, 'shape', '')}"
        out.append((True, f"{call} → {shown}"))
    return out


# ============================================================
# Approval
# ============================================================

def _approved_path(vault_dir: Any) -> Path:
    return _abt().tools_dir(vault_dir) / APPROVED_FILE


def approvals(vault_dir: Any) -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(_approved_path(vault_dir).read_text(
            encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_approvals(data: Dict[str, Any], vault_dir: Any) -> None:
    p = _approved_path(vault_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def status(name: str, vault_dir: Any) -> str:
    """"approved", "changed since approval" or "unreviewed"."""
    rec = approvals(vault_dir).get(name)
    if not rec:
        return "unreviewed"
    code = _abt().get_tool_code(name, vault_dir) or ""
    return "approved" if rec.get("fingerprint") == fingerprint(code) \
        else "changed since approval"


def approve(name: str, roles: List[str], vault_dir: Any, *,
            results: Optional[List[Tuple[bool, str]]] = None) -> Tuple[bool, str]:
    """The user approves `name` for `roles`. Refused unless its tests pass
    (they are run now when `results` is not given)."""
    code = _abt().get_tool_code(name, vault_dir)
    if code is None:
        return False, f"No tool named {name}."
    results = results if results is not None else run_tests(name, vault_dir)
    failed = [r for ok, r in results if not ok]
    if failed:
        return False, "Not approved — a test fails: " + failed[0]
    if not roles:
        return False, "Pick at least one role that may use it."
    data = approvals(vault_dir)
    data[name] = {"fingerprint": fingerprint(code), "roles": sorted(set(roles)),
                  "approved": time.strftime("%Y-%m-%d %H:%M"),
                  "tests_passed": len(results)}
    _save_approvals(data, vault_dir)
    return True, (f"{name} approved for {', '.join(sorted(set(roles)))}; it "
                  "joins their tools from the next question.")


def revoke(name: str, vault_dir: Any) -> bool:
    data = approvals(vault_dir)
    if data.pop(name, None) is None:
        return False
    _save_approvals(data, vault_dir)
    return True


def _params(code: str) -> Dict[str, Any]:
    """The entry function's keyword arguments as a JSON schema, typed from
    their defaults."""
    props: Dict[str, Any] = {}
    try:
        tree = ast.parse(code)
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef))
    except (SyntaxError, StopIteration):
        return {"type": "object", "properties": {}}
    args = fn.args.args
    defaults = [None] * (len(args) - len(fn.args.defaults)) + \
        list(fn.args.defaults)
    for a, d in zip(args, defaults):
        t = "string"
        if isinstance(d, ast.Constant):
            t = {bool: "boolean", int: "integer", float: "number"}.get(
                type(d.value), "string")
        props[a.arg] = {"type": t}
    return {"type": "object", "properties": props}


def approved_tools(vault_dir: Any) -> List[Tuple[str, Callable, List[str]]]:
    """[(tool name for the council, ToolFn, roles)] for every tool whose
    current code is the code the user approved."""
    out = []
    abt = _abt()
    for name, rec in approvals(vault_dir).items():
        code = abt.get_tool_code(name, vault_dir)
        if code is None or rec.get("fingerprint") != fingerprint(code):
            continue
        meta = abt.get_tool(name, vault_dir) or {}

        def fn(args, _n=name):
            df, msg = abt.run_tool(_n, dict(args or {}), vault_dir=vault_dir)
            if df is None:
                return False, msg, {}
            value = _value(df)
            text = value.to_string(max_rows=40) if hasattr(value, "to_string") \
                else str(value)
            return True, f"{text[:4000]}\n(app-built tool, approved by you)", \
                {"tool": _n}
        fn.help = (meta.get("description") or name)[:200]
        fn.params = _params(code)
        out.append((f"app_{name}", fn, list(rec.get("roles") or [])))
    return out


def task_from_proposal(proposal: Dict[str, Any]) -> str:
    """A Forge description from a tool-gap proposal."""
    params = proposal.get("input_params") or {}
    lines = [str(proposal.get("description") or proposal.get(
        "proposed_name") or "a tool").strip()]
    if params:
        lines.append("Arguments: " + ", ".join(f"{k} ({v})"
                                               for k, v in params.items()))
    if proposal.get("output"):
        lines.append(f"Returns: {proposal['output']}")
    if proposal.get("rationale"):
        lines.append(f"Why it is needed: {proposal['rationale']}")
    return "\n".join(lines)


__all__ = ["write_tests", "run_tests", "load_tests", "save_tests", "approve",
           "revoke", "status", "approvals", "approved_tools",
           "task_from_proposal", "fingerprint"]
