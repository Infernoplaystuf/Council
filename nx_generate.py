"""
nx_generate.py — a DREAM3D-NX pipeline from a natural-language request.

Runs in the APP env (pure: catalog + text), so it is testable without simplnx.

The model is kept on rails. It never writes code and never recalls a binding
from memory: it PICKS from a shortlist of filters retrieved out of the catalog
of the INSTALLED binary, and every UUID and argument key it emits is checked
back against that catalog before anything runs.

That is necessary but NOT sufficient, and an earlier version of this docstring
claimed otherwise ("the worst case is a suboptimal filter choice, never a
hallucinated call"). It was wrong. Being in the catalog says where a filter
came from, not what it can do, and two of the 289 execute arbitrary code —
Execute Process is a shell. Retrieval ranked it the #1 hit for "run a process",
so the model was handed the UUID to copy, and validate() approved it. Capability
is now denied by UUID via nx_policy, at BOTH ends: denied filters are never
retrieved (so the model never sees one) and never validate (so an emitted one
is rejected and drives the repair pass).

Note the shortlist is not a boundary either: validate() indexes the whole
catalog, so a UUID the retriever never surfaced still validates. That is
deliberate — a saved pipeline is legitimate — which is exactly why the
load-bearing capability check lives in nx_worker, on the executing side.

    request ──▶ retrieve(k)          lexical, deterministic, no model
            ──▶ build_prompt         only the shortlist + their REAL params
            ──▶ model                emits .d3dpipeline-shaped JSON
            ──▶ validate             every uuid + arg key vs the catalog
            ──▶ repair (one pass)    the errors go back to the model
            ──▶ preflight / trial    simplnx itself is the final gate

Why the catalog and not the docs: see nx_introspect. Filters are keyed by UUID
because the JSON name format drifts between versions.

On preflight, from the real install: there is NO pipeline-level preflight.
nx.Pipeline exposes none, and IFilter.preflight2 does NOT propagate the data
structure (it stays empty), so per-filter preflight validates ARGUMENTS but
cannot dry-run a chain. The honest final gate is a limit=1 trial run, whose
errors come from simplnx itself.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional

import nx_policy

# Args that exist on every filter but are not model-supplied.
IMPLICIT_ARGS = {"data_structure"}
# Present in saved pipelines, not an execute() parameter.
NON_PARAM_KEYS = {"parameters_version"}

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOP = {"the", "a", "an", "of", "to", "and", "or", "for", "on", "in", "with",
         "from", "into", "then", "my", "all", "each", "every", "please",
         "data", "file", "files", "make", "create", "run", "do", "i", "want"}


def _stem(t: str) -> str:
    """Crude suffix stripping, so 'smooth' finds 'Laplacian Smoothing'.

    Without it, exact token matching treats smooth/smoothing, crop/cropping
    and align/alignment as unrelated words, and a filter is missed for the
    only phrasing a user would actually type."""
    for suf in ("ization", "isation", "ing", "ment", "ers", "er", "ed",
                "es", "s"):
        if len(t) > len(suf) + 3 and t.endswith(suf):
            return t[:-len(suf)]
    return t


def _tokens(s: str) -> List[str]:
    return [_stem(t) for t in _TOKEN_RE.findall(str(s or "").lower())
            if t not in _STOP and len(t) > 1]


def _split_camel(s: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ",
                  str(s or ""))


def filter_text(entry: dict) -> str:
    """The searchable text for one filter."""
    parts = [entry.get("human_name") or "",
             _split_camel(entry.get("py_attr") or ""),
             " ".join(entry.get("default_tags") or []),
             entry.get("module") or ""]
    return " ".join(p for p in parts if p)


def retrieve(catalog: dict, query: str, k: int = 12) -> List[dict]:
    """The k filters most likely to serve ``query``.

    Deterministic and model-free. The catalog is only ~289 filters, so a
    lexical score over human_name/tags/class-name is enough and — unlike an
    embedding index — needs no model call, no warm-up and no cache, which
    matters when a single in-process GGUF serializes all inference.
    """
    q = set(_tokens(query))
    if not q:
        return []
    scored = []
    for e in catalog.get("filters", []):
        if nx_policy.is_denied(e.get("uuid")):
            continue          # never put a shell in the model's vocabulary
        name_toks = set(_tokens(entry_name := (e.get("human_name") or "")))
        all_toks = set(_tokens(filter_text(e)))
        if not all_toks:
            continue
        hits = q & all_toks
        if not hits:
            continue
        # A hit in the human name is worth more than one in tags/module.
        score = len(hits) + 2.0 * len(q & name_toks)
        # Prefer the shorter name when two filters match equally: it is the
        # more general one ("Crop Image Geometry" over "Crop Image Geometry
        # (Advanced)").
        score -= 0.01 * len(entry_name)
        scored.append((score, e))
    scored.sort(key=lambda t: (-t[0], t[1].get("py_attr") or ""))
    return [e for _s, e in scored[:k]]


def _params_of(entry: dict) -> List[dict]:
    return [p for p in (entry.get("execute", {}) or {}).get("params", [])
            if p.get("name") not in IMPLICIT_ARGS]


def describe_filter(entry: dict) -> str:
    """One filter, as the model should see it: real name, real UUID, real
    parameter keys and types."""
    lines = [f"- {entry.get('human_name') or entry.get('py_attr')}",
             f"  uuid: {entry.get('uuid')}",
             f"  class: {entry.get('alias', 'nx')}.{entry.get('py_attr')}"]
    for p in _params_of(entry):
        req = "required" if p.get("required") else f"default={p.get('default')}"
        lines.append(f"    {p['name']}: {p.get('type')}  ({req})")
    return "\n".join(lines)


def build_prompt(query: str, candidates: List[dict]) -> str:
    """The constrained request. The shortlist IS the vocabulary."""
    cat = "\n".join(describe_filter(e) for e in candidates)
    return f"""You build DREAM3D-NX pipelines.

Below is the COMPLETE list of filters you may use. It was read from the
installed package on this machine. You may not use any filter that is not
listed, and you may not invent a uuid — copy each uuid exactly as written.

AVAILABLE FILTERS
{cat}

REQUEST
{query}

Reply with ONLY a JSON object in this exact shape, no prose, no code fence:

{{"pipeline": [
  {{"filter": {{"name": "<the class name above>", "uuid": "<copied exactly>"}},
   "args": {{"<a real parameter key from that filter>": <value>}}}}
]}}

Rules:
- Use only the uuids listed above, copied character for character.
- Use only the parameter keys listed under the filter you chose.
- Omit any parameter you do not need; defaults will apply.
- Order the steps so each one's inputs exist by the time it runs.
"""


def extract_json(text: str) -> Optional[dict]:
    """The JSON object out of a model reply, tolerating fences and prose.

    Scans for a BALANCED object rather than regexing between the first '{' and
    the last '}' — that greedy form breaks on any trailing brace, which is
    exactly how the Grapher's analyst silently dropped valid specs."""
    if not text:
        return None
    s = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    start = s.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start:i + 1])
                    except Exception:
                        break
        start = s.find("{", start + 1)
    return None


def validate(pipeline: Any, catalog: dict) -> List[str]:
    """Every reason ``pipeline`` could not run against the INSTALLED package.

    Empty list means it is structurally sound: real UUIDs, real argument keys.
    It does NOT mean the pipeline is sensible — that is what the trial run is
    for."""
    errors: List[str] = []
    idx = {f["uuid"]: f for f in nx_policy.permitted_filters(catalog)}
    if isinstance(pipeline, dict):
        steps = pipeline.get("pipeline")
        if steps is None:
            return ["top level must be an object with a 'pipeline' list"]
    elif isinstance(pipeline, list):
        steps = pipeline
    else:
        return [f"top level must be an object or list, got "
                f"{type(pipeline).__name__}"]
    if not isinstance(steps, list):
        return ["'pipeline' must be a list of steps"]
    if not steps:
        return ["the pipeline is empty"]

    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"step {i}: must be an object")
            continue
        filt = step.get("filter")
        if not isinstance(filt, dict):
            errors.append(f"step {i}: missing a 'filter' object")
            continue
        uuid = filt.get("uuid")
        if not uuid:
            errors.append(f"step {i}: 'filter.uuid' is required — copy it "
                          f"from the filter list")
            continue
        entry = idx.get(uuid)
        if entry is None:
            if nx_policy.is_denied(uuid):
                errors.append(f"step {i}: this filter is not permitted. "
                              f"{nx_policy.reason(uuid)}")
            else:
                errors.append(
                    f"step {i}: uuid {uuid!r} is not in the installed package. "
                    f"Use a uuid exactly as listed.")
            continue
        args = step.get("args", {})
        if args is None:
            args = {}
        if not isinstance(args, dict):
            errors.append(f"step {i} ({entry['py_attr']}): 'args' must be an "
                          f"object")
            continue
        valid = {p["name"] for p in _params_of(entry)}
        for k in args:
            if k in NON_PARAM_KEYS or k in IMPLICIT_ARGS:
                continue
            if k not in valid:
                near = ", ".join(sorted(valid)[:8]) or "(none)"
                errors.append(
                    f"step {i} ({entry['py_attr']}): {k!r} is not a parameter "
                    f"of this filter. Valid keys: {near}")
        for p in _params_of(entry):
            if p.get("required") and p["name"] not in args:
                errors.append(
                    f"step {i} ({entry['py_attr']}): required parameter "
                    f"{p['name']!r} ({p.get('type')}) is missing")
    return errors


def repair_prompt(query: str, candidates: List[dict], bad: Any,
                  errors: List[str]) -> str:
    """One repair pass: hand the model its own output and the exact faults."""
    return f"""Your previous pipeline did not validate against the installed package.

WHAT YOU RETURNED
{json.dumps(bad, indent=2)[:4000]}

WHAT IS WRONG
{chr(10).join('- ' + e for e in errors)}

{build_prompt(query, candidates)}
Fix every point above. Reply with ONLY the corrected JSON object."""


def generate(query: str, catalog: dict, model_fn: Callable[[str], str], *,
             k: int = 12, max_attempts: int = 2) -> Dict[str, Any]:
    """Natural language -> a validated .d3dpipeline-shaped dict.

    ``model_fn`` takes a prompt and returns text. Returns
    {"ok", "pipeline", "errors", "attempts", "candidates", "raw"}. A result is
    only ok when it validates; nothing here executes anything.
    """
    candidates = retrieve(catalog, query, k=k)
    if not candidates:
        return {"ok": False, "pipeline": None, "attempts": 0,
                "candidates": [],
                "errors": ["No filter in the installed package matches that "
                           "request. Try naming the operation, e.g. 'read a "
                           "DREAM3D file and write an STL'."]}
    prompt = build_prompt(query, candidates)
    last_errors: List[str] = []
    parsed: Any = None
    raw = ""
    for attempt in range(1, max_attempts + 1):
        raw = model_fn(prompt) or ""
        parsed = extract_json(raw)
        if parsed is None:
            last_errors = ["the reply was not JSON"]
        else:
            last_errors = validate(parsed, catalog)
            if not last_errors:
                return {"ok": True, "pipeline": parsed, "errors": [],
                        "attempts": attempt,
                        "candidates": [c["uuid"] for c in candidates],
                        "raw": raw}
        if attempt < max_attempts:
            prompt = repair_prompt(query, candidates, parsed or raw,
                                   last_errors)
    return {"ok": False, "pipeline": parsed, "errors": last_errors,
            "attempts": max_attempts,
            "candidates": [c["uuid"] for c in candidates], "raw": raw}


def render_from_selection(query: str, catalog: dict,
                          model_fn: Callable[[str], str], *, k: int = 12,
                          max_attempts: int = 2) -> Dict[str, Any]:
    """Task -> a validated filter SELECTION -> Python rendered from it.

    The conservative path: the model only picks filters and arguments, and the
    Python is rendered by the same type-aware renderer that converts a saved
    .d3dpipeline. Nothing the model writes is ever executed as source.

    It cannot express glue, which is a real limit and not a small one — the
    spec's own CSV route needs `npview[:] = np.loadtxt(...)`, and that is not a
    filter. Use write_script() for a task that needs code between the filters;
    use this when the task really is just a chain of filters.
    """
    import nx_transpile
    res = generate(query, catalog, model_fn, k=k, max_attempts=max_attempts)
    if not res.get("ok"):
        return {**res, "code": None, "render_warnings": []}
    rendered = nx_transpile.transpile(res["pipeline"], catalog)
    return {**res, "code": rendered["code"],
            "render_warnings": rendered.get("warnings", [])}


def build_script_prompt(query: str, candidates: List[dict]) -> str:
    """Ask for a real simplnx script, grounded on real signatures."""
    cat = "\n".join(describe_filter(e) for e in candidates)
    allowed = ", ".join(sorted(nx_policy.ALLOWED_IMPORT_ROOTS))
    return f"""You write DREAM3D-NX pipelines as Python, using the simplnx API.

These filters were read from the package installed on this machine. Their
parameter names, types and defaults are exact. Use only these, and call them
exactly as shown.

AVAILABLE FILTERS
{cat}

HOW A FILTER IS CALLED
    result = nx.<FilterName>.execute(data_structure=ds, <param>=<value>, ...)
    assert not result.errors, result.errors

TYPED VALUES (get these right — they are not plain strings)
    a data path      -> nx.DataPath("Some/Path")
    a numeric type   -> nx.NumericType.float32
    a dream3d import -> nx.Dream3dImportParameter.ImportData(file_path="C:/x.dream3d")
    a file path      -> a plain string

WRITING DATA INTO AN ARRAY (there is no zero-copy wrap; you must copy)
    view = ds[nx.DataPath("Values")].npview()
    view[:] = np.loadtxt("C:/data/in.csv", delimiter=",")

REQUEST
{query}

Write a COMPLETE Python script. Start from `ds = nx.DataStructure()`. You may
use ordinary Python — loops over files, numpy, pathlib — to do whatever the
task needs between filters.

You may import only: {allowed}
Do not use the shell, the filesystem modules, eval/exec, or getattr.

Reply with ONLY the Python, no prose, no code fence.
"""


def extract_code(text: str) -> str:
    """The Python out of a model reply, tolerating a code fence."""
    if not text:
        return ""
    s = text.strip()
    m = re.search(r"```(?:python)?\s*(.+?)```", s, re.DOTALL)
    if m:
        return m.group(1).strip()
    return s


def write_script(query: str, catalog: dict, model_fn: Callable[[str], str], *,
                 k: int = 12, max_attempts: int = 3) -> Dict[str, Any]:
    """A task in English -> a runnable simplnx Python pipeline script.

    THE deliverable. The model writes real Python: filters as execute() lines
    plus whatever code the task needs between them — the numpy copy, a loop
    over a folder, a computed path. That freedom is the point; a pure filter
    selection cannot express the spec's own CSV route.

    It is grounded, not trusted. The prompt carries the REAL signatures of the
    retrieved filters (from the installed binary, so the model is not recalling
    an API), and the result is gated by nx_policy.validate_script before this
    app would run it — the same shape as vault_analyst.validate_generated_code,
    which already gates every model-authored tool here.

    The gate is on EXECUTION, not on authorship: the script is returned either
    way, and a user reading and running it themselves is their call. `ok` says
    whether the app should run it.

    Returns {"ok", "code", "errors", "attempts", "candidates", "raw"}.
    """
    candidates = retrieve(catalog, query, k=k)
    if not candidates:
        return {"ok": False, "code": None, "attempts": 0, "candidates": [],
                "errors": ["No filter in the installed package matches that "
                           "request."]}
    prompt = build_script_prompt(query, candidates)
    errors: List[str] = []
    code = ""
    for attempt in range(1, max_attempts + 1):
        raw = model_fn(prompt) or ""
        code = extract_code(raw)
        ok, errors = nx_policy.validate_script(code)
        if ok:
            return {"ok": True, "code": code, "errors": [],
                    "attempts": attempt,
                    "candidates": [c["uuid"] for c in candidates], "raw": raw}
        if attempt < max_attempts:
            prompt = (f"Your script was refused:\n"
                      f"{chr(10).join('- ' + e for e in errors)}\n\n"
                      f"{build_script_prompt(query, candidates)}"
                      f"Fix every point above. Reply with ONLY the Python.")
    return {"ok": False, "code": code, "errors": errors,
            "attempts": max_attempts,
            "candidates": [c["uuid"] for c in candidates]}
