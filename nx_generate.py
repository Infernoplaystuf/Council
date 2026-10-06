"""
nx_generate.py — a DREAM3D-NX pipeline from a natural-language request.

Runs in the APP env (pure: catalog + text), so it is testable without simplnx.

TWO PATHS, AND ONLY ONE IS SHIPPED. The Dream3D tab's "Write pipeline" button
(council_core.nx_ops.write_script) and the pipeline chat's "create pipeline"
(pipeline_editor.generate_pipeline_from_description) both call write_script():
the model writes Python, nx_policy gates it and nx_ground checks every filter,
keyword and stated value against the installed catalog. generate() /
render_from_selection() below — the model picks filters as JSON — have no
caller in the app today; what follows describes them.

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
            stem = t[:-len(suf)]
            # 'surfaces' -> 'surfac' would never meet 'surface' -> 'surface',
            # so an e-final noun's plural stopped matching its singular. Keep
            # the 'e' when stripping 'es'/'s' would strand a stem that reads
            # like one.
            if suf in ("es", "s") and stem.endswith(("c", "g", "v", "z")):
                return stem + "e"
            return stem
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


# Stemmed, as _tokens yields them: 'writing'/'writes' stem to 'writ' while
# 'write' stays whole, and 'saving'/'saved' to 'sav'.
_READ_VERBS = {"read", "load", "import", "open", "ingest"}
_WRITE_VERBS = {"write", "writ", "save", "sav", "export", "dump"}
# Words that say nothing about WHICH reader or writer: "write it to the
# current folder" must not pick a filter for having "it" or "folder" in it.
_PIN_IGNORE = {_stem(w) for w in (
    "as", "it", "is", "at", "by", "be", "this", "that", "these", "them",
    "new", "out", "use", "using", "result", "results", "everything",
    "current", "folder", "filter", "number", "numbers", "every")}


def io_pins(catalog: dict, query: str) -> List[dict]:
    """The reader / writer the request names, so the shortlist always has
    them.

    The lexical rank alone crowded them out: for "Read the DREAM3D file ...
    compute the feature centroids and sizes ... write out.dream3d" every
    "Compute ..." filter outscored them, ReadDREAM3DFilter came 13th and
    WriteDREAM3DFilter 16th, k=12 cut both, and the model (llama3.1:8b)
    invented ImportData(...).execute to read the file.

    For each read/write verb in the request, the words after it (up to the
    next verb) name the format; the reader/writer whose own name shares the
    rarest of those words is pinned. Never a denied filter."""
    seq = _tokens(query)
    pool = nx_policy.permitted_filters(catalog)
    names = [(e, set(_tokens(e.get("human_name") or ""))
              | set(_tokens(_split_camel(e.get("py_attr") or ""))))
             for e in pool]
    df: Dict[str, int] = {}
    for _e, nt in names:
        for t in nt:
            df[t] = df.get(t, 0) + 1
    n = len(names) + 1

    def idf(t: str) -> float:
        import math
        return math.log(n / (df.get(t, 0) + 1)) + 1.0

    pins: List[dict] = []
    for i, t in enumerate(seq):
        if t in _READ_VERBS:
            canon = {"read", "import"}
        elif t in _WRITE_VERBS:
            canon = {"write", "writ", "export"}
        else:
            continue
        window = []
        for u in seq[i + 1:i + 9]:
            if u in _READ_VERBS or u in _WRITE_VERBS:
                break
            window.append(u)
        obj = set(window) - _PIN_IGNORE
        best = None
        for e, nt in names:
            shared = nt & obj
            if not (nt & canon) or not shared:
                continue
            score = sum(idf(x) for x in shared) \
                - 0.01 * len(e.get("human_name") or "")
            if best is None or score > best[0]:
                best = (score, e)
        if best and best[1] not in pins:
            pins.append(best[1])
    return pins[:4]


def retrieve(catalog: dict, query: str, k: int = 12) -> List[dict]:
    """The k filters most likely to serve ``query``.

    Deterministic and model-free. The catalog is only ~289 filters, so a
    lexical score over human_name/tags/class-name is enough and — unlike an
    embedding index — needs no model call, no warm-up and no cache, which
    matters when a single in-process GGUF serializes all inference.

    The reader and writer the request names come first (io_pins), whatever
    their lexical rank: without them the model improvises the I/O.
    """
    q = set(_tokens(query))
    if not q:
        return []
    pins = io_pins(catalog, query)[:k]
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
    rest = [e for _s, e in scored if e not in pins]
    return (pins + rest)[:k]


def _params_of(entry: dict) -> List[dict]:
    return [p for p in (entry.get("execute", {}) or {}).get("params", [])
            if p.get("name") not in IMPLICIT_ARGS]


def describe_filter(entry: dict, enums: Optional[dict] = None) -> str:
    """One filter, as the model should see it: real name, real UUID, real
    parameter keys and types — and, for an enum-typed parameter, the members
    to choose from (the binding refuses the int a saved pipeline stores)."""
    lines = [f"- {entry.get('human_name') or entry.get('py_attr')}",
             f"  uuid: {entry.get('uuid')}",
             f"  class: {entry.get('alias', 'nx')}.{entry.get('py_attr')}"]
    for p in _params_of(entry):
        req = "required" if p.get("required") else f"default={p.get('default')}"
        line = f"    {p['name']}: {p.get('type')}  ({req})"
        if str(p.get("default")) in ("DataPath('')", 'DataPath("")',
                                     "DataPath()"):
            # Copied as-is, an empty path fails at run time ("Geometry Path
            # cannot be empty"): llama3.1:8b did exactly that, 3 runs of 4.
            line += "  empty: give the path from your data"
        members = (enums or {}).get(p.get("type") or "")
        if members:
            import nx_ground
            line += (f"  one of: " + ", ".join(
                f"{nx_ground.py_name(p['type'])}.{m}"
                for m in nx_ground._members({"enums": enums}, p["type"])))
        lines.append(line)
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
    # The LAST fenced block, not the first: a reply that shows a draft and then
    # the real answer would otherwise have its draft win.
    fences = re.findall(r"```(?:json)?\s*(.+?)```", s, re.DOTALL)
    if fences:
        s = fences[-1].strip()
    found = None
    pos = 0
    while True:
        start = s.find("{", pos)
        if start == -1:
            return found
        end = _balanced_end(s, start)
        if end is None:
            return found        # nothing balanced from here; stop scanning
        try:
            found = json.loads(s[start:end + 1])
            # Resume AFTER the object just consumed. Resuming at start+1 walks
            # back INTO it and lets a nested {"filter": ...} overwrite the real
            # answer with one of its own children.
            pos = end + 1
        except Exception:
            pos = start + 1     # not JSON; try the next brace along


def _balanced_end(s: str, start: int) -> Optional[int]:
    """Index of the '}' closing the object at ``start``, or None.

    Brace-counting that respects strings and escapes — a '}' inside a quoted
    value must not close the object."""
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
                return i
    return None


def validate(pipeline: Any, catalog: dict) -> List[str]:
    """Every reason ``pipeline`` could not run against the INSTALLED package.

    Empty list means it is structurally sound: real UUIDs, real argument
    keys, and values of the type each parameter takes (nx_ground's
    json_value_problem — 'banana' for a NumericType, 12345 for a DataPath).
    It does NOT mean the pipeline is sensible — that is what the trial run is
    for.

    On the required check below, plainly: a parameter is required when its
    signature has no default, and on the installed build that is
    data_structure and nothing else (0 of the other 1972 parameters). This
    JSON never carries data_structure — the renderer supplies it — so the
    check cannot fire against the real catalog. It stays because it is the
    rule, and a build that adds a parameter without a default would need it;
    what actually catches a bad argument here is the value check."""
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
        if not isinstance(uuid, str):
            # A list/dict uuid is unhashable: idx.get(uuid) would raise
            # TypeError straight out of generate() instead of driving a repair.
            errors.append(f"step {i}: 'filter.uuid' must be a string, got "
                          f"{type(uuid).__name__}")
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
        import nx_ground
        params = {p["name"]: p for p in _params_of(entry)}
        for k in args:
            if k in NON_PARAM_KEYS or k in IMPLICIT_ARGS:
                continue
            if k not in params:
                near = ", ".join(sorted(params)[:8]) or "(none)"
                errors.append(
                    f"step {i} ({entry['py_attr']}): {k!r} is not a parameter "
                    f"of this filter. Valid keys: {near}")
                continue
            v = args[k]
            if isinstance(v, dict) and set(v) == {"value", "version"}:
                v = v["value"]          # a saved pipeline's envelope
            if v is None and params[k].get("required"):
                continue                # the required check says so below
            t = params[k].get("type") or ""
            why = nx_ground.json_value_problem(v, t, catalog)
            if why:
                errors.append(f"step {i} ({entry['py_attr']}): {k!r} "
                              f"({t}) {why}")
        for p in _params_of(entry):
            if not p.get("required"):
                continue
            if p["name"] not in args:
                errors.append(
                    f"step {i} ({entry['py_attr']}): required parameter "
                    f"{p['name']!r} ({p.get('type')}) is missing")
            elif args[p["name"]] is None:
                # Key-presence alone is not supply: null reaches execute() and
                # raises there, long after this was supposed to catch it.
                errors.append(
                    f"step {i} ({entry['py_attr']}): required parameter "
                    f"{p['name']!r} is null — give it a value")
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


def _compound_lines(candidates: List[dict], catalog: Optional[dict]) -> List[str]:
    """How to build each compound parameter value the shortlist takes
    (ReadCSVDataParameter, ArrayThresholdSet, ...), from the catalog's
    record of the class. A model left to guess passes a dict, which the
    binding refuses ('Unable to cast Python instance of type dict')."""
    if not catalog or not catalog.get("classes"):
        return []
    import nx_ground
    classes = catalog["classes"]
    seen: List[str] = []
    for e in candidates:
        for p in _params_of(e):
            kind, arg = nx_ground.classify(p.get("type"), catalog)
            if kind == "list":
                kind, arg = nx_ground.classify(arg, catalog)
            if kind != "object" or arg not in classes or arg in seen \
                    or arg.endswith("Dream3dImportParameter.ImportData"):
                continue
            seen.append(arg)
            # What goes INTO it: a list of an abstract base (IArrayThreshold)
            # is filled with its concrete subclasses (ArrayThreshold).
            for t in (classes[arg].get("props") or {}).values():
                k2, a2 = nx_ground.classify(t, catalog)
                if k2 == "list":
                    k2, a2 = nx_ground.classify(a2, catalog)
                if k2 != "object":
                    continue
                for c, rec in classes.items():
                    if c not in seen and (c == a2 and rec.get("init")
                                          or a2 in (rec.get("bases") or [])):
                        seen.append(c)
    return [f"    a {c} -> {nx_ground.construct_hint(c, catalog)}"
            for c in seen]


def build_script_prompt(query: str, candidates: List[dict],
                        catalog: Optional[dict] = None) -> str:
    """Ask for a real simplnx script, grounded on real signatures.

    States the import lines and the call form outright. The prompt used to
    show nx.X.execute(...) and never say `import simplnx as nx`; with
    llama3.1:8b 7 of 12 scripts died on NameError/ImportError for nx, and
    others instantiated filters and set attributes on them."""
    enums = (catalog or {}).get("enums")
    cat = "\n".join(describe_filter(e, enums) for e in candidates)
    allowed = ", ".join(sorted(nx_policy.ALLOWED_IMPORT_ROOTS))
    mods = {e.get("module") or "simplnx" for e in candidates}
    imports = ["    import simplnx as nx"]
    if "orientationanalysis" in mods:
        imports.append("    import orientationanalysis as nxor")
    if "itkimageprocessing" in mods:
        imports.append("    import itkimageprocessing as nxitk")
    imports.append("    import numpy as np          # only if you use numpy")
    compound = _compound_lines(candidates, catalog)
    compound_block = ("\n".join(compound) + "\n") if compound else ""
    return f"""You write DREAM3D-NX pipelines as Python, using the simplnx API.

These filters were read from the package installed on this machine. Their
parameter names, types and defaults are exact. Use only these, and call them
exactly as shown. Your script is checked against the installed package before
it is accepted: a filter, parameter or attribute that does not exist, or a
value of the wrong type, is sent back to you.

AVAILABLE FILTERS
{cat}

THE SCRIPT STARTS WITH
{chr(10).join(imports)}

    ds = nx.DataStructure()

HOW A FILTER IS CALLED
A filter is a class; call its execute() directly. Never create a filter
object and never set attributes on one: every parameter is a keyword argument
of execute(), and data_structure=ds is always the first.
    result = nx.<FilterName>.execute(data_structure=ds, <param>=<value>, ...)
    assert not result.errors, result.errors

TYPED VALUES (get these right — the binding converts nothing)
    a data path      -> nx.DataPath("Some/Path")       never a bare string
    a numeric type   -> nx.NumericType.float32          never an int like 8
    a dream3d import -> nx.Dream3dImportParameter.ImportData(file_path="C:/x.dream3d")
    a file path      -> a plain string
    an int           -> 3, never 3.0
    a list[int] / list[float] / list[list[float]] -> exactly that nesting
{compound_block}
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


def script_repair_prompt(query: str, candidates: List[dict], code: str,
                         errors: List[str], extra: List[dict],
                         catalog: Optional[dict] = None) -> str:
    """The script, the exact faults, and the real signatures of the filters
    the faults point at — the ones the model reached for and the nearest
    real ones to what it invented — ahead of the original request."""
    enums = (catalog or {}).get("enums")
    shown = list(errors[:25])
    if len(errors) > 25:
        shown.append(f"... and {len(errors) - 25} more")
    have = {e.get("uuid") for e in candidates}
    more = [e for e in extra if e.get("uuid") not in have][:4]
    ref = ""
    if more:
        ref = ("\nREAL FILTERS THE ERRORS POINT AT (exact signatures)\n"
               + "\n".join(describe_filter(e, enums) for e in more) + "\n")
    return (f"Your script was checked against the DREAM3D-NX package "
            f"installed on this machine and refused.\n\n"
            f"YOUR SCRIPT\n{(code or '')[:3000]}\n\n"
            f"WHAT IS WRONG\n"
            f"{chr(10).join('- ' + e for e in shown)}\n{ref}\n"
            f"{build_script_prompt(query, candidates, catalog)}"
            f"Fix every point above. Reply with ONLY the corrected Python.")


def extract_code(text: str) -> str:
    """The Python out of a model reply, tolerating a code fence."""
    if not text:
        return ""
    s = text.strip()
    m = re.search(r"```(?:python)?\s*(.+?)```", s, re.DOTALL)
    if m:
        return m.group(1).strip()
    return s


def _fit_candidates_to_ctx(query: str, candidates: List[dict],
                           n_ctx: Optional[int], *,
                           num_predict: int = 900,
                           catalog: Optional[dict] = None,
                           build: Optional[Callable[[List[dict]], str]] = None
                           ) -> List[dict]:
    """Drop the lowest-ranked filters until the prompt fits the model's window.

    The generation prompt lists every retrieved filter with its full signature
    and had NO context awareness — it always dumped k filters. That is why the
    Dream3D generator overflowed a small n_ctx while the Council tab (which
    budgets its injected context) did not: same model, same window, but only
    one side sized its prompt to it. On overflow the clamp head/tail-trims the
    biggest message, cutting filters out of the MIDDLE of the list — so the
    model is told to "use only these filters" from a list that was silently
    mangled.

    A no-op when n_ctx is ample (the common case). Never returns empty — the
    single most relevant filter is always kept, because a shorter shortlist is
    recoverable but an empty one is not. Conservative on chars-per-token (3.2,
    not the clamp's optimistic 4.0) because filter text is dense with
    identifiers and punctuation, which tokenizes finer than prose. (Measured
    2026-10-06 over 23 real llama3.1:8b prompts: 3.36-3.55 chars per token,
    so 3.2 over-budgets by 5-10% — safe.)

    ``build`` renders the prompt for a given shortlist (default: the first
    request); the repair round passes its own, which also carries the
    previous script and the errors."""
    if not n_ctx or n_ctx <= 0 or len(candidates) <= 1:
        return candidates
    if build is None:
        def build(cur):
            return build_script_prompt(query, cur, catalog)
    budget_chars = int(max(256, n_ctx - num_predict - 256) * 3.2)
    cur = list(candidates)
    while len(cur) > 1 and len(build(cur)) > budget_chars:
        cur = cur[:-1]          # retrieve() ranked best-first, so drop the tail
    return cur


def write_script(query: str, catalog: dict, model_fn: Callable[[str], str], *,
                 k: int = 12, max_attempts: int = 3,
                 n_ctx: Optional[int] = None) -> Dict[str, Any]:
    """A task in English -> a runnable simplnx Python pipeline script.

    THE deliverable. The model writes real Python: filters as execute() lines
    plus whatever code the task needs between them — the numpy copy, a loop
    over a folder, a computed path. That freedom is the point; a pure filter
    selection cannot express the spec's own CSV route.

    It is grounded, not trusted. The prompt carries the REAL signatures of the
    retrieved filters (from the installed binary, so the model is not recalling
    an API), and the result passes two gates before `ok`:

      * nx_policy.validate_script — may the app run it at all (the same shape
        as vault_analyst.validate_generated_code, which gates every
        model-authored tool here);
      * nx_ground.check_script — does every nx.<Filter> exist in the
        installed catalog, is every execute() keyword a real parameter of that
        filter, and is every value the source states of the type the catalog
        gives. This was missing: a call to nx.TotallyMadeUpFilter with a bogus
        keyword was ok=True, and with llama3.1:8b 12 of 12 accepted scripts
        failed to run.

    A refused script goes back to the model with the exact faults, the
    nearest real names, and the signatures of the real filters it reached for.

    The gate is on EXECUTION, not on authorship: the script is returned either
    way, and a user reading and running it themselves is their call. `ok` says
    whether the app should run it.

    Returns {"ok", "code", "errors", "attempts", "candidates", "raw"}.
    """
    import nx_ground
    candidates = retrieve(catalog, query, k=k)
    if not candidates:
        return {"ok": False, "code": None, "attempts": 0, "candidates": [],
                "errors": ["No filter in the installed package matches that "
                           "request."]}
    # Size the shortlist to the model's window so the prompt is never silently
    # trimmed mid-list. No-op when n_ctx is ample or unknown.
    candidates = _fit_candidates_to_ctx(query, candidates, n_ctx,
                                        catalog=catalog)
    prompt = build_script_prompt(query, candidates, catalog)
    errors: List[str] = []
    code = ""
    raw = ""
    for attempt in range(1, max_attempts + 1):
        raw = model_fn(prompt) or ""
        code = extract_code(raw)
        ok, errors = nx_policy.validate_script(code)
        grounded = nx_ground.check_script(code, catalog)
        errors = list(errors) + [e for e in grounded["errors"]
                                 if e not in errors]
        if not errors:
            return {"ok": True, "code": code, "errors": [],
                    "attempts": attempt,
                    "candidates": [c["uuid"] for c in candidates], "raw": raw}
        if attempt < max_attempts:
            extra = grounded["suggest"]

            def _repair(cur, _code=code, _errors=errors, _extra=extra):
                return script_repair_prompt(query, cur, _code, _errors,
                                            _extra, catalog)
            fitted = _fit_candidates_to_ctx(query, candidates, n_ctx,
                                            catalog=catalog, build=_repair)
            prompt = _repair(fitted)
    return {"ok": False, "code": code, "errors": errors,
            "attempts": max_attempts,
            "candidates": [c["uuid"] for c in candidates], "raw": raw}
