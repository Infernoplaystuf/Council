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

Two kinds of script, two rule sets
----------------------------------
A pipeline script the app runs (workflow_runner) is one of two kinds, and
script_trust() says which:

  MODEL — a model wrote it or edited it. Every place the app saves model code
    (nx_ops.write_script, the Tk "Write pipeline", the chat's "create
    pipeline", a model's "modify" edits) puts MODEL_STAMP on its first line,
    and a script in the vault's data_out (the app's own output area) counts
    as one whatever its first line says. Rules:
      * validate_script, in full: imports from ALLOWED_IMPORT_ROOTS only, no
        shell, no dynamic attribute lookup, no file-changing call (pathlib is
        allowed for joining paths, exists() and glob(); its unlink/rename/
        write_text are not), no route to a module the allowlist keeps out
        (pathlib.os, typing.sys...), no dunder, no saved Pipeline and no
        get_filters (FILTER_ROUTE_NAMES);
      * and at run time, inside the script's own interpreter (nx_guard):
        every OUTPUT path a filter is given must land under the vault's
        data_out or the run's staging folder and may not replace a file the
        run did not make; inputs are read-only; any file change the script
        makes itself is held to the same rule, and processes, native code and
        sockets are refused. Static checks can be evaded; that one is not
        a check on the text.
  USER — a script the user saved or imported themselves (no stamp, not in
    data_out). Their machine, their call: any import, any file the script
    chooses — as before. What still holds: no Execute Process / Python-codegen
    filter by any name, uuid or route (capability_reasons), checked before the
    run and again by nx_guard when a filter or a pipeline executes. A user
    script MAY run a saved pipeline the way the simplnx tutorials do —
    nx.Pipeline.from_file(...), set_args, execute — when the .d3dpipeline's
    filters pass this policy: a literal path given to simplnx's
    Pipeline.from_file is read and checked before anything runs
    (pipeline_file_reasons; any other class's from_file is the script's own
    business), and nx_guard checks every step's uuid when Pipeline.execute
    runs, whatever the path was.

A model's script never gets the USER rules by being moved or renamed: the
stamp travels with the text. Deleting the stamp line is the user taking the
script over, which is theirs to do — and the app says so where it matters:
every refusal of a MODEL script opens with why it got those rules and how
to hand it back (model_rules_note), and a model "modify" of the user's own
script says, when it saves the copy, what the rule change means for it
(pipeline_editor._model_rules_warnings) — the copy's `import os` or its
writes to the user's own folder are refused under the model rules. (Scripts a model wrote BEFORE the stamp
existed carry none; the ones still in data_out are caught by the location
rule, a generated one the user kept in pipelines/in is not.)
"""
from __future__ import annotations

import ast
import json
import os
import re
from typing import Dict, Iterable, List, Optional, Tuple

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
# A dunder NAME (not attribute): `__builtins__["open"]`, or `x = __import__`
# then `x("os")` — neither is an attribute, nor a call of a denied name.
_DUNDER_NAMES_OK = frozenset({"__name__", "__file__", "__doc__"})

# File-changing calls. pathlib is allowed — a pipeline joins paths, asks
# exists(), globs a folder, reads a CSV — and every one of these is reachable
# from a Path or a numpy array without importing anything else. Measured
# before this list: Path(doc).unlink(), Path(doc).write_text(""),
# Path(doc).rename(...) and np.save(doc, a) all passed validate_script, and a
# model-written script could delete or overwrite any file the user can.
# Refused as ANY reference, not only as a call: `w = p.write_text; w("")`.
# Not here because the name is shared with something a pipeline needs:
# `replace` (str.replace / Path.replace), `remove` (DataStructure.remove),
# `copy` (ndarray.copy), `mkdir` (an output folder; nx_guard keeps it under
# the output area). nx_guard refuses what these miss, at run time.
FILE_CHANGE_NAMES = frozenset({
    "unlink", "rmdir", "rename", "renames", "removedirs", "rmtree", "touch",
    "write_text", "write_bytes", "write", "writelines", "symlink_to",
    "hardlink_to", "link_to", "chmod", "lchmod", "chown", "truncate",
    "tofile", "save", "savez", "savez_compressed", "savetxt", "memmap",
    "dump", "copyfile", "copytree", "move", "makedirs", "mkfifo", "mknod",
    "unpack_archive", "make_archive",
    # simplnx's own: writes into an existing .dream3d from C++.
    "append_to_dream3d_file",
})

# Modules the import allowlist keeps out, reached as an ATTRIBUTE of one it
# lets in: pathlib.os.remove(...), typing.sys.modules["shutil"].rmtree(...),
# json.codecs.open(..., "w"), typing.types.FunctionType(...). Each is a
# second way in to what the allowlist exists to keep out. (`code` is not
# here: it is a simplnx property.)
MODULE_REACH_NAMES = frozenset({
    "os", "sys", "shutil", "subprocess", "io", "codecs", "builtins",
    "importlib", "operator", "types", "ctypes", "nt", "posix", "_os",
    "tempfile", "modules", "socket", "pickle", "marshal", "runpy", "inspect",
    "gc", "multiprocessing", "threading", "_thread", "signal", "winreg",
    "_winapi", "msvcrt", "functools", "system", "popen", "startfile",
})

# ---- provenance: who wrote a script --------------------------------------
# The first line the app writes above every script a model wrote or edited.
# script_trust() looks for it on any line, so a comment or shebang the user
# adds above it does not hide it. See "Two kinds of script" above.
MODEL_STAMP = "# council: model-"
MODEL = "model"
USER = "user"
_STAMP_RE = re.compile(r"^\s*#\s*council:\s*model-(written|edited)\b",
                       re.MULTILINE)


def stamp_model_script(code: str, origin: str, *, edited: bool = False) -> str:
    """``code`` with the model-script stamp on its first line (once).

    ``origin`` says which part of the app saved it ("nx_generate", "the
    pipeline chat", ...). Every save of model-written or model-edited code
    goes through this, so the workflow runner gives it the MODEL rules."""
    code = code or ""
    if _STAMP_RE.search(code):
        return code
    kind = "edited" if edited else "written"
    return (f"{MODEL_STAMP}{kind} ({origin}). The Council runs this under "
            f"the model-script rules (nx_policy).\n{code}")


def script_trust(code: str, path=None, app_roots: Iterable = ()) -> str:
    """MODEL or USER for a script about to be run — see "Two kinds of
    script". MODEL when the text carries the stamp, or ``path`` lies in one
    of ``app_roots`` (the vault's data_out: what is there, the app wrote)."""
    if _STAMP_RE.search(code or ""):
        return MODEL
    if path is not None:
        try:
            import path_contain
            if any(path_contain.is_under(path, r) for r in app_roots if r):
                return MODEL
        except Exception:                                 # noqa: BLE001
            return MODEL                # cannot tell: the stricter rules
    return USER


def model_rules_note(code: str, path=None, app_roots: Iterable = ()) -> str:
    """Why ``code`` gets the MODEL rules, and how the user hands it back to
    their own — the first thing a refusal says, since a model-edited copy of
    the user's own script was refused for what the user's script always did
    ("import 'os' is not allowed") with nothing saying why. '' for a USER
    script."""
    code = code or ""
    m = _STAMP_RE.search(code)
    if m:
        at = code.index("#", m.start())
        line = code.count("\n", 0, at) + 1
        return (f"line {line} carries the model stamp ('{MODEL_STAMP}"
                f"{m.group(1)}'), so the model-script rules apply; if you "
                f"have read the script and want it run as your own, delete "
                f"that line")
    if path is not None and script_trust(code, path, app_roots) == MODEL:
        return ("the script is in the vault's data_out, where the app saves "
                "what a model writes, so the model-script rules apply; to run "
                "it as your own, read it and save a copy outside data_out")
    return ""

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
    no dynamic attribute lookup, no file-changing call (FILE_CHANGE_NAMES:
    a pipeline's outputs are written by its writer filters, which nx_guard
    holds to the output area), no route to a module the import allowlist
    keeps out (MODULE_REACH_NAMES), none of the two filters whose capability
    is arbitrary code execution, and none of the routes that reach a filter
    without naming it (FILTER_ROUTE_NAMES).

    These are the MODEL rules (see "Two kinds of script"). The text check is
    the first gate, not the only one: nx_guard enforces the same containment
    at run time, where a trick this check misses still meets it.

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
    seen = set(reasons)

    def add(node, text: str) -> None:
        line = f"line {getattr(node, 'lineno', 0)}: {text}"
        if line not in seen:
            seen.add(line)
            reasons.append(line)

    # A denied name at a call site is reported once, by the call.
    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    for node in ast.walk(tree):
        # ---- imports: allowlist ---------------------------------------
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root not in ALLOWED_IMPORT_ROOTS:
                    add(node, f"import {a.name!r} is not allowed. Allowed: "
                              f"{', '.join(sorted(ALLOWED_IMPORT_ROOTS))}")
                elif set(a.name.split(".")) & NATIVE_CODE_NAMES:
                    add(node, f"import {a.name!r} is not allowed (it loads "
                              f"native code)")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if node.level or root not in ALLOWED_IMPORT_ROOTS:
                add(node, f"from {node.module!r} import ... is not allowed")
            for a in node.names:
                if a.name in NATIVE_CODE_NAMES or set(
                        (node.module or "").split(".")) & NATIVE_CODE_NAMES:
                    add(node, f"importing {a.name} is not allowed (it loads "
                              f"native code)")
                elif a.name in FILE_CHANGE_NAMES:
                    add(node, f"importing {a.name} is not allowed: "
                              f"{_FILE_CHANGE_WHY}")
                elif a.name in MODULE_REACH_NAMES or a.name in DENIED_CALLS:
                    add(node, f"importing {a.name} is not allowed")
        # ---- calls ------------------------------------------------------
        elif isinstance(node, ast.Call):
            fn = node.func
            name = None
            if isinstance(fn, ast.Name):
                name = fn.id
            elif isinstance(fn, ast.Attribute):
                name = fn.attr
            if name in DENIED_CALLS:
                add(node, f"{name}() is not allowed")
            for kw in node.keywords:
                if kw.arg in DENIED_KWARGS and not (
                        isinstance(kw.value, ast.Constant)
                        and kw.value.value is False):
                    add(node, f"{kw.arg}= is not allowed (unpickling a file "
                              f"runs whatever code it holds)")
        # ---- names: a denied builtin held, not called -------------------
        elif isinstance(node, ast.Name):
            # `f = open; f(doc, "w")` and `i = __import__; i("os")` never
            # call a denied name at the call site.
            if node.id in DENIED_CALLS:
                if id(node) not in called:
                    add(node, f"{node.id} is not allowed")
            elif (node.id.startswith("__") and node.id.endswith("__")
                    and node.id not in _DUNDER_NAMES_OK):
                add(node, f"{node.id} is not allowed (dunder access reaches "
                          f"the interpreter)")
        # ---- definitions: a dunder method runs when Python decides ------
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            if (node.name.startswith("__") and node.name.endswith("__")
                    and node.name != "__init__"):
                add(node, f"defining {node.name} is not allowed (the "
                          f"interpreter calls it, not the script)")
        # ---- attributes -------------------------------------------------
        elif isinstance(node, ast.Attribute):
            if node.attr in NATIVE_CODE_NAMES:
                add(node, f"{node.attr} is not allowed (it loads native code)")
            elif (node.attr.startswith("__") and node.attr.endswith("__")
                    and node.attr not in _DUNDER_OK):
                add(node, f"{node.attr} is not allowed (dunder access "
                          f"reaches the interpreter)")
            elif node.attr in FILE_CHANGE_NAMES:
                add(node, f".{node.attr} is not allowed: {_FILE_CHANGE_WHY}")
            elif node.attr in MODULE_REACH_NAMES:
                add(node, f".{node.attr} is not allowed: it reaches a module "
                          f"the import allowlist keeps out")
            elif node.attr in DENIED_CALLS and id(node) not in called:
                add(node, f".{node.attr} is not allowed")
    return (not reasons), reasons


_FILE_CHANGE_WHY = ("a pipeline script may not change files itself — its "
                    "outputs are written by its writer filters (e.g. "
                    "WriteDREAM3DFilter), into the output area")


def _capability_reasons(tree: ast.AST,
                        allow: frozenset = frozenset()) -> List[str]:
    """Every place ``tree`` names a denied filter, or a route that reaches a
    filter without naming it (FILTER_ROUTE_NAMES, minus ``allow``), or
    carries a denied filter's uuid as text."""
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
        elif name in FILTER_ROUTE_NAMES and name not in allow:
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


# ---- what a USER script may do that a model's may not ----------------------

# The simplnx tutorials run a saved pipeline: Pipeline.from_file(...), then
# set_args on a step, then execute. A user's own tutorial-style script was
# refused outright once Pipeline joined FILTER_ROUTE_NAMES (it hides which
# filters run). For a USER script it is allowed again, because what it runs
# can be checked: the file before the run (pipeline_file_reasons), every
# step's uuid when nx_guard sees Pipeline.execute.
USER_PIPELINE_NAMES = frozenset({"Pipeline", "PipelineFilter",
                                 "AbstractPipelineNode"})


def pipeline_file_uuids(text: str) -> List[str]:
    """Every filter uuid a .d3dpipeline's JSON names (nested ones too).
    Raises ValueError when the text is not a pipeline."""
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("pipeline"),
                                                    list):
        raise ValueError("it has no \"pipeline\" list")
    found: List[str] = []

    def walk(v) -> None:
        if isinstance(v, dict):
            f = v.get("filter")
            if isinstance(f, dict) and isinstance(f.get("uuid"), str):
                found.append(f["uuid"].lower())
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(data["pipeline"])
    return found


def _pipeline_class_refs(tree: ast.AST):
    """A test for "this expression is simplnx's Pipeline class", from the
    script's own imports: nx.Pipeline / simplnx.Pipeline after
    `import simplnx [as nx]`, the name `from simplnx import Pipeline [as P]`
    (or `import *`) binds, and a plain name assigned one of those
    (`P = nx.Pipeline`). A script that never imports simplnx has none."""
    modules, names = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "simplnx":
                    modules.add(a.asname or a.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "simplnx" \
                and not node.level:
            for a in node.names:
                if a.name == "Pipeline":
                    names.add(a.asname or a.name)
                elif a.name == "*":
                    names.add("Pipeline")

    def is_ref(e) -> bool:
        if isinstance(e, ast.Name):
            return e.id in names
        return (isinstance(e, ast.Attribute) and e.attr == "Pipeline"
                and isinstance(e.value, ast.Name) and e.value.id in modules)
    # A plain name ever assigned one of those is one too (P = nx.Pipeline).
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and is_ref(node.value):
            names.update(t.id for t in node.targets
                         if isinstance(t, ast.Name))
    return is_ref


def pipeline_file_reasons(tree: ast.AST, search_dirs: Iterable = ()
                          ) -> List[str]:
    """Why a script's Pipeline.from_file(<literal path>) must not run: the
    saved pipeline it names holds a denied filter.

    Only simplnx's Pipeline.from_file is read (the only from_file in the
    installed simplnx): a user's Settings.from_file('settings.json'), a
    tokenizer's or libmagic's is not a pipeline, and refusing it as "not a
    pipeline" broke scripts that ran before (the receiver used not to be
    looked at).

    A relative path is looked for in ``search_dirs`` (the script's own
    folder, then the run's working folder). A path this cannot find, one
    computed at run time, one reached through a spelling the imports above do
    not show, and a file this cannot read as a pipeline are not refused here:
    nx_guard checks every step's uuid when that pipeline executes, which no
    path trick gets past — this check only says so earlier."""
    reasons: List[str] = []
    is_pipeline = _pipeline_class_refs(tree)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "from_file"
                and is_pipeline(node.func.value)):
            continue
        arg = node.args[0] if node.args else next(
            (k.value for k in node.keywords if k.arg in ("path", "arg0")),
            None)
        if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
            continue
        raw = arg.value
        cands = [raw] if os.path.isabs(raw) else [
            os.path.join(str(d), raw) for d in search_dirs if d]
        path = next((c for c in cands if os.path.isfile(c)), None)
        if path is None:
            continue
        try:
            with open(path, encoding="utf-8-sig") as fh:
                uuids = pipeline_file_uuids(fh.read())
        except (OSError, ValueError):
            continue                    # nx_guard checks it if it runs
        for u in uuids:
            if is_denied(u):
                reasons.append(f"line {node.lineno}: the pipeline {raw} "
                               f"holds a refused filter — {reason(u)}")
    return reasons


def run_reasons(code: str, *, trust: str = MODEL,
                search_dirs: Iterable = ()) -> List[str]:
    """Why workflow_runner must not run ``code`` — [] when it may.

    MODEL: validate_script in full. USER: the capability rule, with a saved
    pipeline allowed when its filters pass (USER_PIPELINE_NAMES,
    pipeline_file_reasons). See "Two kinds of script" at the top."""
    if trust == MODEL:
        return validate_script(code)[1]
    try:
        tree = ast.parse(code or "")
    except SyntaxError as exc:
        return [f"the script could not be checked (syntax error here: "
                f"{exc.msg}, line {exc.lineno})"]
    return (_capability_reasons(tree, allow=USER_PIPELINE_NAMES)
            + pipeline_file_reasons(tree, search_dirs))
