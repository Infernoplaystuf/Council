"""
tests.bench_fakes — a scripted "model" for the benchmark's offline tests,
answering through tests/fake_ollama (so the REAL engine path runs: the
Ollama backend, json_schema, last_call_stats).

reply_for(body) picks the answer from what the request is:

  * Describe it (tree or pixel mode, read off the json_schema it sends):
    a valid wireframe for the S2 length converter or the S4 to-do list;
  * the code-behind writer, function mode: a correct K1 function with the
    exact signature line the prompt fixes (``wrong=True``: a subtly wrong
    one); handler mode: a stopwatch method that keeps its state on self —
    the shape the writer's reference gate refuses;
  * docs_qa: docs_bench's answer key (oracle_model_call);
  * the speed probe and warm-up: a short plain reply.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List

S2_TREE = {"window": {"title": "Length converter"}, "layout": {
    "kind": "column", "children": [
        {"kind": "row", "children": [{"kind": "label", "label": "Value"},
                                     {"kind": "entry", "label": "Value"}]},
        {"kind": "row", "children": [
            {"kind": "label", "label": "From"},
            {"kind": "combobox", "label": "From",
             "props": {"values": ["mm", "cm", "m", "in"]}}]},
        {"kind": "row", "children": [
            {"kind": "label", "label": "To"},
            {"kind": "combobox", "label": "To",
             "props": {"values": ["mm", "cm", "m", "in"]}}]},
        {"kind": "button", "label": "Convert"},
        {"kind": "label", "label": "Result"}]}}

S4_TREE = {"window": {"title": "To-do"}, "layout": {
    "kind": "column", "children": [
        {"kind": "row", "children": [{"kind": "entry", "label": "New task"},
                                     {"kind": "button", "label": "Add"}]},
        {"kind": "listbox", "label": "Tasks"},
        {"kind": "row", "children": [{"kind": "button", "label": "Remove"},
                                     {"kind": "button",
                                      "label": "Clear all"}]}]}}


def _px(kind, label, x, y, w, h, **props):
    r = {"kind": kind, "label": label, "x": x, "y": y, "w": w, "h": h}
    if props:
        r["props"] = props
    return r


S4_PIXEL = {"window": {"title": "To-do"}, "shapes": [
    _px("entry", "New task", 16, 16, 560, 32),
    _px("button", "Add", 592, 16, 120, 32),
    _px("listbox", "Tasks", 16, 64, 696, 400),
    _px("button", "Remove", 16, 480, 120, 32),
    _px("button", "Clear all", 152, 480, 120, 32)]}

S2_PIXEL = {"window": {"title": "Length converter"}, "shapes": [
    _px("label", "Value", 16, 16, 120, 32),
    _px("entry", "Value", 152, 16, 240, 32),
    _px("label", "From", 16, 64, 120, 32),
    _px("combobox", "From", 152, 64, 240, 32, values=["mm", "cm", "m", "in"]),
    _px("label", "To", 16, 112, 120, 32),
    _px("combobox", "To", 152, 112, 240, 32, values=["mm", "cm", "m", "in"]),
    _px("button", "Convert", 16, 160, 120, 32),
    _px("label", "Result", 16, 208, 376, 32)]}


def _user_text(body: Dict[str, Any]) -> str:
    msgs = body.get("messages") or []
    return "\n".join(str(m.get("content") or "") for m in msgs)


def k1_function(signature: str, wrong: bool = False) -> str:
    m = re.match(r"def\s+(\w+)\((.*)\)\s*->\s*dict:", signature.strip())
    name = m.group(1)
    params = [p.split(":")[0].strip() for p in m.group(2).split(",")]
    a, b = params[0], params[1]
    op = "-" if wrong else "+"
    return (f"```python\n{signature.strip()}\n"
            f"    \"\"\"Add the two numbers.\"\"\"\n"
            f"    if {a} is None or {b} is None:\n"
            f"        return {{\"result\": \"Invalid input\"}}\n"
            f"    return {{\"result\": f\"{{{a} {op} {b}:g}}\"}}\n```")


STOPWATCH_HANDLER = '''```python
def on_btn_start_stop(self, *args) -> None:
    import time
    if getattr(self, "_started", None) is None:
        self._started = time.monotonic()
        self.ports.state.set("Running")
    else:
        took = time.monotonic() - self._started
        self._started = None
        self.ports.state.set("Stopped")
        self.ports.elapsed.set(f"{took:.1f} s")
```'''


def make_reply_fn(*, wrong_code: bool = False,
                  log: List[str] = None) -> Callable[[Dict[str, Any]], str]:
    """The fake server's reply_fn (see the module docstring)."""
    from council_core import docs_bench
    oracle = docs_bench.oracle_model_call(docs_bench.load_bench())

    def reply(body: Dict[str, Any]) -> str:
        text = _user_text(body)
        fmt = body.get("format")
        kind = "plain"
        out = "ready"
        if isinstance(fmt, dict) and (
                "layout" in (fmt.get("properties") or {})
                or "shapes" in (fmt.get("properties") or {})) or \
                "You design GUI" in text:
            tree = isinstance(fmt, dict) and "layout" in (
                fmt.get("properties") or {}) or "as a TREE" in text
            conv = "length converter" in text.lower()
            pick = (S2_TREE if conv else S4_TREE) if tree else \
                (S2_PIXEL if conv else S4_PIXEL)
            kind, out = ("describe-tree" if tree else "describe-pixel",
                         json.dumps(pick))
        elif "You write ONE Python function" in text:
            sig = text.split("SIGNATURE (use exactly this line):\n", 1)[1]
            sig = sig.split("\n", 1)[0]
            kind, out = "function", k1_function(sig, wrong=wrong_code)
        elif "You write ONE method" in text:
            kind, out = "handler", STOPWATCH_HANDLER
        elif "Write the whole numbers" in text:
            kind, out = "speed", " ".join(str(i) for i in range(1, 120))
        elif "Reply with the one word" in text:
            kind, out = "warmup", "ready"
        else:
            msgs = [{"role": m.get("role"), "content": m.get("content")}
                    for m in body.get("messages") or []]
            try:
                kind, out = "docs", oracle(msgs)
            except Exception as exc:                      # noqa: BLE001
                kind, out = "unknown", f"no scripted reply ({exc!r})"
        if log is not None:
            log.append(kind)
        return out
    return reply
