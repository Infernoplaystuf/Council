"""
Pipeline editor — interactive variable-change loop for Dream3D / simplnx
Python pipelines.

The user describes a change in natural language ("change the threshold to
0.8", "swap the file path to my_data.dream3d", "remove the smoothing filter"
etc.). A local model emits a structured JSON edit list against the parsed
pipeline; this module applies those edits to the source text and writes a
new versioned copy to `vault/pipelines/out/<name>_<suffix>.py`.

Why edits instead of asking the model to rewrite the whole file:
  - 100+ line pipelines are easy for the model to corrupt under "rewrite"
    prompts. Structured edits scoped to one filter at a time stay accurate.
  - We can AST-validate after each step and abort cleanly if a single edit
    produces a syntax error.
  - Edits double as a change log the user can read in the transcript.

Supported edit ops:
  set_param      change one keyword arg of a filter (by 1-based index or name)
  replace_text   find-and-replace exact substring (count-limited)
  insert_after   add line(s) after a marker line
  delete_lines   remove the line containing a marker

Each op is a dict like:
  {"op": "set_param", "filter_index": 2, "param": "threshold",
   "new_value_text": "0.8"}
  {"op": "replace_text", "find": "ImageGeometry", "replace": "Geom2",
   "max_count": 1}
  {"op": "insert_after", "marker": "import simplnx as nx",
   "lines": ["import numpy as np"]}
  {"op": "delete_lines", "marker": "write_xdmf_file"}
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pipeline_scanner import (
    Pipeline, FilterStep,
    parse_py_pipeline, parse_pipeline_file,
    vault_pipelines_out_dir,
    _is_filter_execute_call, _is_pipeline_insert_call,
)


# ============================================================
# Edit application
# ============================================================

@dataclass
class EditResult:
    new_source: str
    log: List[str]
    succeeded: bool
    error: Optional[str] = None


def _find_filter_call_nodes(source: str) -> List[Tuple[int, str, ast.Call]]:
    """Return [(index, filter_name, call_node), ...] for every filter call
    in source order — matching the indexing pipeline_scanner uses (1-based)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out: List[Tuple[int, str, ast.Call]] = []
    counter = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fname = _is_filter_execute_call(node)
        if not fname:
            insert_match = _is_pipeline_insert_call(node)
            if insert_match:
                fname = insert_match[0]
        if fname:
            counter += 1
            out.append((counter, fname, node))
    return out


def _value_text_end(call_text: str, start: int) -> int:
    """Given `call_text` and `start` pointing just after `param=`, return the
    index where the value expression ends (the next top-level comma or close
    paren). Honors brackets/braces/parens/string literals."""
    depth = 0
    in_str: Optional[str] = None
    escape = False
    i = start
    n = len(call_text)
    while i < n:
        c = call_text[i]
        if escape:
            escape = False
        elif in_str:
            if c == "\\":
                escape = True
            elif c == in_str:
                in_str = None
        elif c in ('"', "'"):
            in_str = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return i
            depth -= 1
        elif c == "," and depth == 0:
            return i
        i += 1
    return n


def _apply_set_param(source: str, edit: Dict[str, Any]) -> Tuple[str, str]:
    """Change one keyword argument of a filter. Returns (new_source, message)."""
    target_idx = edit.get("filter_index")
    filter_name = edit.get("filter_name", "")
    param = edit.get("param") or ""
    new_value_text = edit.get("new_value_text")
    if new_value_text is None:
        return source, "missing new_value_text"

    calls = _find_filter_call_nodes(source)
    target_node: Optional[ast.Call] = None
    target_label = ""

    if isinstance(target_idx, int):
        for idx, fname, node in calls:
            if idx == target_idx:
                target_node = node
                target_label = f"#{idx} {fname}"
                break
    elif filter_name:
        for idx, fname, node in calls:
            if fname == filter_name:
                target_node = node
                target_label = f"#{idx} {fname}"
                break

    if target_node is None:
        return source, f"target filter not found (index={target_idx}, name={filter_name!r})"

    start_line = target_node.lineno - 1
    end_line = (target_node.end_lineno or target_node.lineno) - 1
    lines = source.split("\n")
    call_text = "\n".join(lines[start_line:end_line + 1])

    pat = re.compile(rf"\b{re.escape(param)}\s*=\s*")
    m = pat.search(call_text)
    if not m:
        return source, f"param {param!r} not in {target_label}"

    val_start = m.end()
    val_end = _value_text_end(call_text, val_start)
    new_call_text = call_text[:val_start] + str(new_value_text) + call_text[val_end:]
    new_lines = lines[:start_line] + new_call_text.split("\n") + lines[end_line + 1:]
    return "\n".join(new_lines), f"{target_label}: {param} -> {new_value_text}"


def _apply_replace_text(source: str, edit: Dict[str, Any]) -> Tuple[str, str]:
    find_s = edit.get("find") or ""
    replace_s = edit.get("replace") or ""
    max_count = int(edit.get("max_count", 1))
    if not find_s:
        return source, "empty find string"
    if find_s not in source:
        return source, f"find string not present: {find_s!r}"
    new_source = source.replace(find_s, replace_s, max_count)
    return new_source, f"replaced {find_s!r} -> {replace_s!r} (up to {max_count})"


def _apply_insert_after(source: str, edit: Dict[str, Any]) -> Tuple[str, str]:
    marker = edit.get("marker") or ""
    new_lines = edit.get("lines") or []
    if isinstance(new_lines, str):
        new_lines = [new_lines]
    if not marker:
        return source, "empty marker"
    lines = source.split("\n")
    for i, line in enumerate(lines):
        if marker in line:
            insertion = list(new_lines)
            updated = lines[:i + 1] + insertion + lines[i + 1:]
            return "\n".join(updated), f"inserted {len(insertion)} line(s) after marker"
    return source, f"marker not found: {marker!r}"


def _apply_delete_lines(source: str, edit: Dict[str, Any]) -> Tuple[str, str]:
    marker = edit.get("marker") or ""
    if not marker:
        return source, "empty marker"
    lines = source.split("\n")
    kept = [ln for ln in lines if marker not in ln]
    removed = len(lines) - len(kept)
    if removed == 0:
        return source, f"marker not found: {marker!r}"
    return "\n".join(kept), f"removed {removed} line(s) containing {marker!r}"


_OP_HANDLERS = {
    "set_param":    _apply_set_param,
    "replace_text": _apply_replace_text,
    "insert_after": _apply_insert_after,
    "delete_lines": _apply_delete_lines,
}


def apply_edits(source: str, edits: List[Dict[str, Any]]) -> EditResult:
    """Apply edits in order. After each edit, validate the result with
    ast.parse; abort cleanly on the first syntax error."""
    current = source
    log: List[str] = []
    for i, edit in enumerate(edits, start=1):
        op = edit.get("op")
        handler = _OP_HANDLERS.get(op)
        if not handler:
            log.append(f"#{i} unknown op: {op!r}")
            continue
        try:
            new_source, msg = handler(current, edit)
        except Exception as exc:
            return EditResult(current, log, False, f"#{i} {op} raised: {exc}")

        try:
            ast.parse(new_source)
        except SyntaxError as exc:
            return EditResult(
                current, log, False,
                f"#{i} {op} produced invalid Python: {exc.msg} at line {exc.lineno}",
            )
        current = new_source
        log.append(f"#{i} {op}: {msg}")

    return EditResult(current, log, True, None)


# ============================================================
# Output naming + saving
# ============================================================

_SUFFIX_SAFE = re.compile(r"[^A-Za-z0-9_-]+")


def safe_suffix(s: str, max_len: int = 40) -> str:
    """Sanitize a model-proposed suffix to a safe filename fragment."""
    s = (s or "").strip().lower().replace(" ", "_")
    s = _SUFFIX_SAFE.sub("", s)
    return (s or "modified")[:max_len]


def save_modified_pipeline(
    original_path: Path,
    new_source: str,
    suffix: str,
    vault_dir: Path,
) -> Path:
    """Write the modified source to vault/pipelines/out/<stem>_<suffix>.py.

    Picks a non-clobbering filename — if the name already exists, appends
    _v2, _v3, ... until a free slot is found.
    """
    out_dir = vault_pipelines_out_dir(vault_dir)
    stem = original_path.stem
    safe = safe_suffix(suffix)
    base = f"{stem}_{safe}"
    candidate = out_dir / f"{base}.py"
    n = 2
    while candidate.exists():
        candidate = out_dir / f"{base}_v{n}.py"
        n += 1
    candidate.write_text(new_source, encoding="utf-8")
    return candidate


# ============================================================
# Prompt building
# ============================================================

EDIT_PROMPT_SCHEMA = """\
Output ONLY a JSON object with the shape below. No markdown fences, no
commentary outside the JSON.

{
  "suffix": "<short_descriptive_suffix_in_snake_case>",
  "edits": [
    { "op": "set_param", "filter_index": <int>, "param": "<param_name>",
      "new_value_text": "<python source code for the new value>" },
    { "op": "replace_text", "find": "<exact string>", "replace": "<new string>",
      "max_count": 1 },
    { "op": "insert_after", "marker": "<substring of an existing line>",
      "lines": ["<new line>", "..."] },
    { "op": "delete_lines", "marker": "<substring of lines to remove>" }
  ]
}

Rules:
- Use the SMALLEST set of edits that achieves the user's request.
- Prefer "set_param" for parameter changes — it's the most precise.
- "new_value_text" must be valid Python source for the value (e.g.
  '0.8', 'nx.DataPath("MyGroup/Out")', '"C:/data/new.dream3d"',
  '[200, 200, 200]'). Quote strings yourself.
- "suffix" should be short, descriptive snake_case (e.g. "threshold_0_8",
  "new_input_path", "larger_dims").
- If the user's request is impossible from the pipeline shown, return
  {"suffix":"no_change","edits":[]} and nothing else.
"""


def build_edit_prompt(pipeline: Pipeline, source: str, user_request: str) -> str:
    """Build the prompt the model sees when asked to edit a pipeline."""
    rendered_steps: List[str] = []
    for s in pipeline.steps:
        rendered_steps.append(f"Step {s.index}: {s.filter_name}")
        for pn, vv in s.inputs:
            rendered_steps.append(f"  [{pn}] = {vv}")
        for pn, vv in s.outputs:
            rendered_steps.append(f"  ({pn}) = {vv}")
        for pn, vv in s.configs:
            rendered_steps.append(f"   {pn} = {vv}")
    steps_text = "\n".join(rendered_steps) if rendered_steps else "(no steps detected)"

    return f"""You are editing a Dream3D / simplnx Python pipeline.

User request:
{user_request}

Pipeline name: {pipeline.name}

Parsed steps (filter_index is 1-based; brackets = inputs, parens = outputs):
{steps_text}

Full source of the original pipeline:
{source}

{EDIT_PROMPT_SCHEMA}
"""


# ============================================================
# Model-driven flow (call into council_engine.local_chat)
# ============================================================

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Pull the first JSON object out of a model response."""
    if not text:
        return None
    # Try fenced first
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except Exception:
            pass
    m = _JSON_BLOCK_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def request_edits_from_model(
    pipeline: Pipeline,
    source: str,
    user_request: str,
    *,
    num_predict: int = 800,
    temperature: float = 0.05,
) -> Dict[str, Any]:
    """Ask the local model for edit JSON. Returns {"suffix":..., "edits":[...]}
    or {"suffix":..., "edits":[], "error":"..."} on failure to parse."""
    import council_engine as ce

    prompt = build_edit_prompt(pipeline, source, user_request)
    raw = ce.local_chat(
        messages=[{"role": "user", "content": prompt}],
        temperature=temperature,
        num_predict=num_predict,
        timeout=180,
    )
    obj = _extract_json(raw or "")
    if not isinstance(obj, dict):
        return {"suffix": "modified", "edits": [],
                "error": "model did not return valid JSON",
                "raw": raw[:600] if raw else ""}
    obj.setdefault("suffix", "modified")
    obj.setdefault("edits", [])
    return obj


# ============================================================
# End-to-end orchestrator
# ============================================================

@dataclass
class ModifyResult:
    success: bool
    pipeline: Optional[Pipeline]
    source_path: Optional[Path]
    new_path: Optional[Path]
    edits: List[Dict[str, Any]]
    log: List[str]
    error: Optional[str] = None


def modify_pipeline_by_request(
    pipeline_path: Path,
    user_request: str,
    vault_dir: Path,
) -> ModifyResult:
    """Full flow: parse pipeline, ask model for edits, apply + validate, save.

    Only handles .py pipelines today. For .dream3d files we return an error
    suggesting the user convert to .py first (the simplnx primer guides that).
    """
    if pipeline_path.suffix.lower() != ".py":
        return ModifyResult(
            success=False, pipeline=None, source_path=pipeline_path,
            new_path=None, edits=[], log=[],
            error=("modify currently only supports .py simplnx pipelines. "
                   "Convert your .dream3d file to a .py script first — the "
                   "simplnx primer in the chat can guide you."),
        )

    pipeline = parse_pipeline_file(pipeline_path)
    if not pipeline.steps:
        return ModifyResult(
            success=False, pipeline=pipeline, source_path=pipeline_path,
            new_path=None, edits=[], log=[],
            error=("no filter calls detected in this file. Either it isn't a "
                   "simplnx pipeline or the API call style is unrecognized."),
        )

    try:
        source = pipeline_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return ModifyResult(
            success=False, pipeline=pipeline, source_path=pipeline_path,
            new_path=None, edits=[], log=[], error=f"read failed: {exc}",
        )

    request = request_edits_from_model(pipeline, source, user_request)
    edits = request.get("edits", []) or []
    suffix = request.get("suffix") or "modified"

    if request.get("error"):
        return ModifyResult(
            success=False, pipeline=pipeline, source_path=pipeline_path,
            new_path=None, edits=edits, log=[],
            error=request["error"],
        )
    if not edits:
        return ModifyResult(
            success=False, pipeline=pipeline, source_path=pipeline_path,
            new_path=None, edits=[], log=[],
            error=("model produced no edits — the request may not match "
                   "what's in this pipeline."),
        )

    result = apply_edits(source, edits)
    if not result.succeeded:
        return ModifyResult(
            success=False, pipeline=pipeline, source_path=pipeline_path,
            new_path=None, edits=edits, log=result.log,
            error=result.error,
        )

    new_path = save_modified_pipeline(
        pipeline_path, result.new_source, suffix, vault_dir,
    )
    return ModifyResult(
        success=True, pipeline=pipeline, source_path=pipeline_path,
        new_path=new_path, edits=edits, log=result.log,
    )
