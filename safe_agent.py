"""
safe_agent.py — constrained tool-using agent for Data's Inferno.

Implements §5 of the Odysseus brief: an LLM can ask to call exactly the
tools on a reviewed allow-list, nothing else. No shell, no subprocess of
model output, no eval/exec outside the existing pandas sandbox, no
network tools, no writes outside the configured outputs directory.

Public surface:

    Tool(name, fn, schema, timeout_s)
    AgentPolicy(allowed_tools, file_root, output_dir, max_steps,
                default_timeout_s)
    ToolDenied                      raised on any unlisted name
    ToolTimeout                     raised when per-tool deadline blows
    AgentTrace                      list of ToolCall records (every step
                                    is visible / auditable)
    dispatch_tool(name, args, policy) -> ToolCall
    run_agent(messages, runner, policy, *, tool_call_parser=...) ->
        AgentResult(trace, final_answer)

The default tool set (file, pandas, memory) is built by
``default_tools(policy)`` so a single AgentPolicy + LocalMemory is enough
to spin up a working agent. The pandas tool routes through the EXISTING
``vault_analyst.execute_pandas_code`` — NO NEW EXECUTION SURFACE.

Tool-call protocol (model-side):

    The model's reply is parsed for JSON blocks shaped like
        {"tool": "<name>", "args": {<json>}}
    one per step. The agent dispatches, attaches the observation as a
    user/tool message, and asks the model to continue. When the model
    emits text without a tool block (or hits max_steps) we stop and
    return the accumulated trace + the final assistant text.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from dataclasses import dataclass, field
from pathlib import Path
from typing import (Any, Callable, Dict, Iterable, List, Optional, Sequence,
                    Tuple)


_LOG = logging.getLogger("safe_agent")


class ToolDenied(Exception):
    """Raised when the model requested a tool not on the allow-list."""


class ToolTimeout(Exception):
    """Raised when a tool exceeded its configured timeout."""


# ============================================================
# Schema / records
# ============================================================

@dataclass
class Tool:
    """A reviewed callable the agent may invoke.

    ``fn`` receives the parsed args dict and the AgentPolicy (in that
    order). Tools are expected to be deterministic and side-effect-bounded
    (no shell, no network) — the dispatcher enforces no eval/exec/shell
    against ``fn`` itself, but each Tool's implementation must also be
    safe by construction.
    """
    name: str
    fn: Callable[[Dict[str, Any], "AgentPolicy"], Any]
    schema: Dict[str, Any] = field(default_factory=dict)
    timeout_s: Optional[float] = None
    description: str = ""


@dataclass
class ToolCall:
    """One step in the agent trace. Goes straight into the audit log."""
    step: int
    name: str
    args: Dict[str, Any]
    result: Any = None
    error: Optional[str] = None
    elapsed_s: float = 0.0


@dataclass
class AgentPolicy:
    """All bounds the agent must obey. Build one per session; never
    mutate at runtime — agents read the policy each step so a config
    swap won't take effect mid-loop anyway, but immutability keeps
    audits clean."""
    allowed_tools: Tuple[str, ...]
    file_root: Path                       # read_local_file is rooted here
    output_dir: Path                      # only writeable area
    max_steps: int = 6
    default_timeout_s: float = 30.0
    # Optional: clamp the model's max_tokens per call so a runaway
    # generation can't burn an entire context window.
    max_tokens_per_step: int = 600
    # Hard limit on total bytes pulled by file reads — defence against a
    # loop that keeps reading the same 100 MB file.
    max_total_read_bytes: int = 32 * 1024 * 1024

    def __post_init__(self) -> None:
        self.file_root = Path(self.file_root).expanduser().resolve()
        self.output_dir = Path(self.output_dir).expanduser().resolve()
        self.allowed_tools = tuple(self.allowed_tools)


@dataclass
class AgentTrace:
    calls: List[ToolCall] = field(default_factory=list)
    bytes_read: int = 0
    denied: List[Tuple[str, str]] = field(default_factory=list)   # (name, reason)


@dataclass
class AgentResult:
    final_answer: str
    trace: AgentTrace
    stopped_reason: str = ""    # "done" | "max_steps" | "error"


# ============================================================
# Dispatcher
# ============================================================

def dispatch_tool(name: str,
                  args: Dict[str, Any],
                  policy: AgentPolicy,
                  tools: Dict[str, Tool],
                  trace: AgentTrace,
                  *,
                  step: int) -> ToolCall:
    """Run one tool. Refuses unlisted names. Enforces per-tool timeout.

    Always returns a ToolCall (with ``error`` set when something blew up)
    so the agent loop can include the observation in the next prompt
    rather than crashing the whole turn.
    """
    if name not in policy.allowed_tools:
        trace.denied.append((name, "not on allow-list"))
        raise ToolDenied(f"tool {name!r} is not on the allow-list "
                          f"(allowed: {policy.allowed_tools})")
    tool = tools.get(name)
    if tool is None:
        trace.denied.append((name, "not registered"))
        raise ToolDenied(f"tool {name!r} is allow-listed but has no "
                          "registered implementation — this is a "
                          "configuration error.")
    timeout = tool.timeout_s or policy.default_timeout_s
    call = ToolCall(step=step, name=name, args=dict(args or {}))
    t0 = time.time()
    try:
        # Run on its own thread so a tool that wedges doesn't block the
        # whole agent. Threads can't be hard-killed in Python — we just
        # give up waiting and surface TimeoutError. The orphaned thread
        # will exit on its own eventually.
        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(tool.fn, args or {}, policy)
            try:
                call.result = fut.result(timeout=timeout)
            except FutTimeout:
                call.error = f"timeout after {timeout}s"
                raise ToolTimeout(call.error)
    except (ToolDenied, ToolTimeout):
        raise
    except Exception as exc:
        call.error = repr(exc)
    finally:
        call.elapsed_s = time.time() - t0
        trace.calls.append(call)
    return call


# ============================================================
# Default tools — read_local_file, run_pandas_analysis, query_memory
# ============================================================

def _safe_resolve(root: Path, target: str) -> Path:
    """Resolve ``target`` against ``root`` and reject anything that lands
    outside the resolved root — covers ``../`` AND symlink escapes."""
    if not isinstance(target, str) or not target.strip():
        raise PermissionError("read_local_file: 'path' must be a non-empty string")
    t = Path(target)
    if t.is_absolute():
        full = t.resolve()
    else:
        full = (root / t).resolve()
    root_resolved = root.resolve()
    try:
        full.relative_to(root_resolved)
    except ValueError:
        raise PermissionError(
            f"read_local_file: {target!r} resolves outside the configured "
            f"file_root ({root_resolved}). Path traversal / symlink "
            "escapes are refused.")
    return full


def _tool_read_local_file(args: Dict[str, Any],
                          policy: AgentPolicy) -> Dict[str, Any]:
    path = str(args.get("path", "")).strip()
    full = _safe_resolve(policy.file_root, path)
    if not full.exists():
        return {"path": str(full), "error": "file does not exist"}
    if not full.is_file():
        return {"path": str(full), "error": "not a regular file"}
    # Bounded read so a 1 GB file doesn't OOM the agent.
    max_bytes = min(2 * 1024 * 1024, policy.max_total_read_bytes)
    try:
        with full.open("rb") as f:
            data = f.read(max_bytes + 1)
    except Exception as exc:
        return {"path": str(full), "error": f"read failed: {exc!r}"}
    truncated = len(data) > max_bytes
    text = data[:max_bytes].decode("utf-8", errors="replace")
    return {
        "path":      str(full.relative_to(policy.file_root)),
        "bytes":     len(data),
        "truncated": truncated,
        "text":      text,
    }


def _tool_run_pandas_analysis(args: Dict[str, Any],
                              policy: AgentPolicy) -> Dict[str, Any]:
    """Routes to the EXISTING vault_analyst sandbox. We add NO new
    execution surface — just pass the code through to the validated
    pandas runner that the Council already ships."""
    code = str(args.get("code", ""))
    if not code.strip():
        return {"error": "missing 'code' argument"}
    try:
        # Lazy-import vault_analyst — it pulls pandas/openpyxl which
        # are heavy at import time.
        from vault_analyst import execute_pandas_code
    except Exception as exc:
        return {"error": f"vault_analyst unavailable: {exc!r}"}
    df, msg = execute_pandas_code(code, [policy.file_root])
    out: Dict[str, Any] = {"message": msg}
    if df is not None:
        try:
            out["preview"] = df.head(20).to_dict(orient="records")
            out["shape"] = list(df.shape)
        except Exception:
            out["preview"] = str(df)[:2000]
    return out


def _tool_query_memory(args: Dict[str, Any],
                       policy: AgentPolicy) -> Dict[str, Any]:
    text = str(args.get("text", "")).strip()
    if not text:
        return {"error": "missing 'text' argument"}
    k = int(args.get("k", 5))
    try:
        import council_memory
        hits = council_memory.get_memory().query(text, k=k)
    except Exception as exc:
        return {"error": f"memory query failed: {exc!r}"}
    return {
        "hits": [
            {"id": h.id, "distance": h.distance,
             "text": (h.text or "")[:800],
             "metadata": dict(h.metadata or {})}
            for h in hits
        ],
    }


def default_tools(policy: AgentPolicy) -> Dict[str, Tool]:
    """The reviewed allow-list. Extending requires a code review per §0."""
    return {
        "read_local_file": Tool(
            name="read_local_file",
            fn=_tool_read_local_file,
            description="Read a text file relative to file_root. "
                        "Refuses absolute paths or any path that resolves "
                        "outside the root after symlink resolution.",
            schema={"path": "str"},
            timeout_s=15.0,
        ),
        "run_pandas_analysis": Tool(
            name="run_pandas_analysis",
            fn=_tool_run_pandas_analysis,
            description="Execute a sandboxed pandas snippet through the "
                        "vault_analyst sandbox. The code is statically "
                        "validated before execution; restricted builtins; "
                        "DATA_FOLDER preloaded to file_root.",
            schema={"code": "str"},
            timeout_s=60.0,
        ),
        "query_memory": Tool(
            name="query_memory",
            fn=_tool_query_memory,
            description="Retrieve the top-k records from LocalMemory.",
            schema={"text": "str", "k": "int (optional, default 5)"},
            timeout_s=15.0,
        ),
    }


# ============================================================
# LLM driver — parses tool calls out of the model's reply
# ============================================================

def _iter_brace_balanced(text: str):
    """Yield (start, end_exclusive) for every top-level brace-balanced
    JSON-shaped object in ``text``. Handles nesting (e.g. `{"args": {...}}`)
    by walking the string and tracking depth. Skips strings so a `}`
    inside a quoted value doesn't close the object prematurely."""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c != "{":
            i += 1
            continue
        depth = 0
        j = i
        in_str = False
        esc = False
        while j < n:
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        yield i, j + 1
                        i = j + 1
                        break
            j += 1
        else:
            # No matching brace — give up at the next character
            i += 1


def parse_tool_calls(reply: str) -> List[Tuple[str, Dict[str, Any]]]:
    """Extract JSON tool blocks from a model reply. Walks the string with
    brace-balancing so nested ``args`` objects parse correctly.
    Unparseable blocks and blocks without a ``tool`` key are skipped."""
    out: List[Tuple[str, Dict[str, Any]]] = []
    if not reply:
        return out
    for start, end in _iter_brace_balanced(reply):
        raw = reply[start:end]
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        name = str(obj.get("tool", "")).strip()
        if not name:
            continue
        args = obj.get("args") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {"_raw": args}
        if not isinstance(args, dict):
            args = {"_raw": args}
        out.append((name, args))
    return out


def run_agent(messages: List[Dict[str, str]],
              runner,
              policy: AgentPolicy,
              tools: Optional[Dict[str, Tool]] = None,
              *,
              system_preamble: Optional[str] = None) -> AgentResult:
    """Drive a ModelRunner through up to ``policy.max_steps`` tool turns.

    Each iteration:
      1. Ask the runner for a reply.
      2. Parse JSON tool blocks out of the reply.
      3. For each block, dispatch through ``dispatch_tool``; collect
         observations.
      4. If no tool blocks, treat the reply as the final answer.
      5. Otherwise append observations and continue.

    Bounded by ``max_steps``. Every dispatch is recorded in the trace.
    """
    tools = tools or default_tools(policy)
    trace = AgentTrace()
    convo = list(messages)
    if system_preamble:
        convo.insert(0, {"role": "system", "content": system_preamble})

    for step in range(1, policy.max_steps + 1):
        reply = runner.chat(convo, max_tokens=policy.max_tokens_per_step)
        calls = parse_tool_calls(reply)
        if not calls:
            return AgentResult(final_answer=reply.strip(), trace=trace,
                               stopped_reason="done")
        observations: List[str] = []
        for name, args in calls:
            try:
                call = dispatch_tool(name, args, policy, tools, trace,
                                     step=step)
            except ToolDenied as exc:
                observations.append(f"[tool-denied] {name}: {exc}")
                continue
            except ToolTimeout as exc:
                observations.append(f"[tool-timeout] {name}: {exc}")
                continue
            obs = (
                f"[tool-result] {name} "
                f"({call.elapsed_s:.2f}s): "
                + (json.dumps(call.result, default=str)[:1800]
                   if call.error is None
                   else f"ERROR {call.error}")
            )
            observations.append(obs)
        # Bookkeeping for the byte cap
        for c in trace.calls:
            if c.name == "read_local_file" and isinstance(c.result, dict):
                trace.bytes_read += int(c.result.get("bytes", 0))
        if trace.bytes_read > policy.max_total_read_bytes:
            return AgentResult(
                final_answer="(stopped: total read budget exceeded)",
                trace=trace, stopped_reason="byte_budget")
        convo.append({"role": "assistant", "content": reply})
        convo.append({"role": "user",
                       "content": "\n".join(observations)})

    return AgentResult(final_answer="(stopped: max_steps reached)",
                       trace=trace, stopped_reason="max_steps")
