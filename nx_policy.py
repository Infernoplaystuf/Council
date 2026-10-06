"""
nx_policy.py — which DREAM3D-NX filters this app will never run.

Pure stdlib, imported by BOTH environments (the app side and the nx worker),
so the same rule is enforced wherever a pipeline can be executed.

Why this exists
---------------
The rest of Part B sanctions filters by PROVENANCE: "is this UUID in the
installed binary?". That is not sanction. It answers where a filter came from,
not what it can do — and two of the 289 filters in the installed package do
something no amount of path-guarding can contain:

  * Execute Process runs an arbitrary shell command (`arguments: str`).
  * Create Python Plugin and/or Filters writes Python code to disk.

Both were reachable end to end. Execute Process is not a reader or a writer, so
the runner rewrote none of its paths and the writer-containment check skipped
it entirely; a two-step Read + Execute Process pipeline ran to completion and
h_run_folder reported ok=1, failed=0 while the spawned command did its work.
`blocking` defaults to False, so the filter returns no errors and the app calls
it a clean run. Retrieval ranked Execute Process the #1 hit for "run a process",
so the model was handed the UUID to copy — no hallucination required. And the
shortlist is not a boundary: validate() indexes the whole catalog.

A spawned process is bound by none of this app's guarantees. It is not a
writer, so data_out containment does not apply to it; it does not need the
network, so air-gapping does not stop `del /s /q`; and it runs as the user, so
it can delete a database. That is the one thing this app must never be able to
do.

So capability is denied by UUID — the only stable key, since the JSON filter
name format drifts between versions.

This is a denylist, not an allowlist, deliberately: the other 287 filters are
domain operations on the data structure, and an allowlist over them would have
to be regenerated on every DREAM3D-NX update and would silently break real
pipelines. The two entries here are the capability outliers, and they are
outliers precisely because they escape the data structure.
"""
from __future__ import annotations

import ast
from typing import Dict, List, Optional, Tuple

# The model writes real Python — filters as execute() lines, plus whatever glue
# the task needs. That is the point of the feature, and a filter-selection
# cannot express the spec's own CSV path
# (npview[:] = np.loadtxt(...) is not a filter).
#
# So the gate is on the code, not on the model's freedom to write it — the same
# shape as vault_analyst.validate_generated_code, which already gates every
# model-authored tool in app_built_tools. It is not reused verbatim because it
# forbids the attribute `remove`, and DataStructure.remove is legitimate here.
#
# Imports are an ALLOWLIST: everything a pipeline legitimately needs is short
# and known, while the ways to reach a shell are not enumerable.
ALLOWED_IMPORT_ROOTS = frozenset({
    "simplnx", "orientationanalysis", "itkimageprocessing",
    "numpy", "math", "json", "pathlib", "datetime", "re", "typing",
})

# Names that end the conversation regardless of import.
DENIED_CALLS = frozenset({
    "eval", "exec", "compile", "__import__", "open", "input", "breakpoint",
    "getattr", "setattr", "delattr", "globals", "locals", "vars", "memoryview",
})

# The class names of the capability outliers. The pipeline path denies them by
# uuid; a script names the CLASS, so both spellings must be covered.
DENIED_CLASS_NAMES = frozenset({
    "ExecuteProcessFilter", "CreatePythonSkeletonFilter",
})

# Ways to reach a filter WITHOUT naming its class, which the class-name list
# above cannot see. Measured on the installed build: nx.get_filters() returns
# all 154 simplnx filter classes, ExecuteProcessFilter and
# CreatePythonSkeletonFilter among them, so `nx.get_filters()[i].execute(...)`
# reached the shell past every check here. A saved pipeline (Pipeline.from_file,
# or one assembled with Pipeline.append) can hold either filter too, and the
# plugin loaders load Python. A pipeline script needs none of them — filters
# are called by name — so a script may not touch them at all. (nx_worker runs
# a SAVED pipeline itself and checks every step's uuid before anything runs.)
FILTER_ROUTE_NAMES = frozenset({
    "get_filters", "get_python_filter_ids", "get_python_plugins",
    "load_python_plugin", "reload_python_plugins", "unload_python_plugins",
    "PythonPlugin", "AbstractPlugin", "ManualImportFinder",
    "Pipeline", "PipelineFilter", "AbstractPipelineNode", "test_filter",
})

# numpy is allowed for the array copy, and two corners of it run native code
# or unpickle (= run) arbitrary objects: ctypeslib.load_library loads a DLL,
# and np.load(..., allow_pickle=True) executes whatever the file says.
NATIVE_CODE_NAMES = frozenset({"ctypeslib", "load_library", "f2py",
                               "distutils"})
DENIED_KWARGS = frozenset({"allow_pickle"})

# Attribute-based escapes: __globals__ -> builtins -> anything.
_DUNDER_OK = frozenset({"__init__", "__name__", "__file__", "__doc__"})

# uuid -> why it is refused (shown to the user; keep it plain).
DENIED_UUIDS: Dict[str, str] = {
    "fb511a70-2175-4595-8c11-d1b5b6794221":
        "'Execute Process' runs an arbitrary shell command. Nothing this app "
        "does can contain a spawned process: it is not a writer, so the "
        "output-area check does not bind it, and it runs with your account's "
        "full access to your files.",
    "1a35f50d-a9f5-9ea2-af70-5b9cf894e45f":
        "'Create Python Plugin and/or Filters' writes Python code to disk, "
        "which DREAM3D-NX can then load and run.",
}


def is_denied(uuid) -> bool:
    return str(uuid) in DENIED_UUIDS


def reason(uuid) -> Optional[str]:
    return DENIED_UUIDS.get(str(uuid))


def permitted_filters(catalog: dict) -> list:
    """The catalog's filters minus the capability outliers."""
    return [f for f in (catalog or {}).get("filters", [])
            if f.get("uuid") and not is_denied(f["uuid"])]


def validate_script(code: str) -> Tuple[bool, List[str]]:
    """(ok, reasons) for a model-written simplnx pipeline script.

    Gates the code the APP would execute. It does not gate what the app WRITES:
    a script the user reads and runs themselves is their call on their machine,
    and this app's job there is to be legible, not to be a nanny.

    The model is free to write real Python here — execute() lines, numpy glue,
    the npview[:] = np.loadtxt(...) copy the spec requires, loops over files.
    What it may not do is reach outside that: no shell, no filesystem module,
    no dynamic attribute lookup, none of the two filters whose capability is
    arbitrary code execution, and none of the routes that reach a filter
    without naming it (FILTER_ROUTE_NAMES).

    This is about permission, not correctness: whether the filters and
    parameters the script names exist is nx_ground.check_script's job.
    """
    if not (code or "").strip():
        return False, ["the script is empty"]
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return False, [f"syntax error: {exc}"]

    # The capability rule first: the same check workflow_runner applies to
    # every script it runs, whoever wrote it.
    reasons: List[str] = _capability_reasons(tree)
    for node in ast.walk(tree):
        # ---- imports: allowlist ---------------------------------------
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root not in ALLOWED_IMPORT_ROOTS:
                    reasons.append(
                        f"line {node.lineno}: import {a.name!r} is not "
                        f"allowed. Allowed: "
                        f"{', '.join(sorted(ALLOWED_IMPORT_ROOTS))}")
                elif set(a.name.split(".")) & NATIVE_CODE_NAMES:
                    reasons.append(f"line {node.lineno}: import {a.name!r} "
                                   f"is not allowed (it loads native code)")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if node.level or root not in ALLOWED_IMPORT_ROOTS:
                reasons.append(
                    f"line {node.lineno}: from {node.module!r} import ... is "
                    f"not allowed")
            for a in node.names:
                if a.name in NATIVE_CODE_NAMES or set(
                        (node.module or "").split(".")) & NATIVE_CODE_NAMES:
                    reasons.append(f"line {node.lineno}: importing {a.name} "
                                   f"is not allowed (it loads native code)")
        # ---- calls ------------------------------------------------------
        elif isinstance(node, ast.Call):
            fn = node.func
            name = None
            if isinstance(fn, ast.Name):
                name = fn.id
            elif isinstance(fn, ast.Attribute):
                name = fn.attr
            if name in DENIED_CALLS:
                reasons.append(f"line {node.lineno}: {name}() is not allowed")
            for kw in node.keywords:
                if kw.arg in DENIED_KWARGS and not (
                        isinstance(kw.value, ast.Constant)
                        and kw.value.value is False):
                    reasons.append(
                        f"line {node.lineno}: {kw.arg}= is not allowed "
                        f"(unpickling a file runs whatever code it holds)")
        # ---- attributes -------------------------------------------------
        elif isinstance(node, ast.Attribute):
            if node.attr in NATIVE_CODE_NAMES:
                reasons.append(f"line {node.lineno}: {node.attr} is not "
                               f"allowed (it loads native code)")
            elif (node.attr.startswith("__") and node.attr.endswith("__")
                    and node.attr not in _DUNDER_OK):
                reasons.append(
                    f"line {node.lineno}: {node.attr} is not allowed "
                    f"(dunder access reaches the interpreter)")
    return (not reasons), reasons


def _capability_reasons(tree: ast.AST) -> List[str]:
    """Every place ``tree`` names a denied filter, or a route that reaches a
    filter without naming it (FILTER_ROUTE_NAMES), or carries a denied
    filter's uuid as text."""
    reasons: List[str] = []
    seen = set()

    def add(node, text: str) -> None:
        key = (getattr(node, "lineno", 0), text)
        if key not in seen:
            seen.add(key)
            reasons.append(f"line {key[0]}: {text}")

    def name_hit(node, name: str, verb: str = "") -> None:
        if name in DENIED_CLASS_NAMES:
            add(node, f"{verb}{name} is refused — {reason_for_class(name)}")
        elif name in FILTER_ROUTE_NAMES:
            add(node, f"{verb}{name} is not allowed: it reaches filters "
                      f"without naming them (a filter list, a saved "
                      f"pipeline or a plugin loader), so this check could "
                      f"not see what it runs. Call each filter by name: "
                      f"nx.<Filter>.execute(data_structure=ds, ...)")

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # The imported NAME matters, not just the module. simplnx is
            # allowed, so `from simplnx import ExecuteProcessFilter as EP`
            # passed the module check, and `EP.execute(...)` never mentions
            # the denied class at the call site — the alias walked the shell
            # straight through a denylist that only ever saw call sites.
            for a in node.names:
                name_hit(node, a.name, "importing ")
        elif isinstance(node, ast.Attribute):
            name_hit(node, node.attr)
        elif isinstance(node, ast.Name):
            name_hit(node, node.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            for uuid in DENIED_UUIDS:
                if uuid in node.value.lower():
                    add(node, f"the text names a refused filter's uuid "
                              f"({uuid}) — {DENIED_UUIDS[uuid]}")
    return reasons


def capability_reasons(code: str) -> List[str]:
    """Why the app must not run ``code`` at all, whoever wrote it — [] if it
    may.

    The run-end check for scripts (workflow_runner), the counterpart of
    nx_worker._check_capability for saved pipelines: a denied filter by class
    name, a route to filters that hides which one runs, a denied uuid in the
    text. It is NOT validate_script: a script the user put in their pipelines
    folder may import what it likes — that is their call — but no script the
    app runs reaches Execute Process.

    A script that does not parse here is refused too: this interpreter may be
    older than the one that runs it (3.11 vs the nx env's 3.12), and a script
    this check cannot read is one it cannot vouch for."""
    try:
        tree = ast.parse(code or "")
    except SyntaxError as exc:
        return [f"the script could not be checked (syntax error here: "
                f"{exc.msg}, line {exc.lineno})"]
    return _capability_reasons(tree)


def reason_for_class(class_name: str) -> str:
    for uuid, why in DENIED_UUIDS.items():
        if class_name.replace("Filter", "") in why.replace(" ", "") \
                or class_name in why:
            return why
    if class_name == "ExecuteProcessFilter":
        return DENIED_UUIDS["fb511a70-2175-4595-8c11-d1b5b6794221"]
    if class_name == "CreatePythonSkeletonFilter":
        return DENIED_UUIDS["1a35f50d-a9f5-9ea2-af70-5b9cf894e45f"]
    return "this filter executes arbitrary code."
