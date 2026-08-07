"""
gui_policy.py — what a generated GUI project is allowed to do.

Pure stdlib, importable from both the designer and the generated app, so the
same rule is enforced wherever code can be run (the nx_policy pattern).

THIS IS A DIFFERENT, DELIBERATELY MORE PERMISSIVE POLICY THAN THE ANALYST'S
--------------------------------------------------------------------------
vault_analyst.validate_generated_code guards a READ-ONLY data sandbox: it must
forbid writing files at all. A generated application is not that. It legitimately
opens files the user picked, saves output, and imports modules. Reusing the
analyst validator here would reject every real app; reusing its STRUCTURE is
correct, and so is its hard-won lesson:

    CHECK EVERY ast.Attribute NODE, CALLED OR NOT.

A blocklist is only as strong as its least-checked syntactic form. `os.system(x)`
is an obvious call, but `fn = os.system` then `fn(x)` is the same capability
through a name binding, and a validator that only inspects ast.Call sees
nothing. That exact hole was demonstrated against this repo's analyst sandbox —
a bound method escaped it and deleted a real file — which is why it is closed
here from the start rather than after an incident.

WHAT THIS CANNOT DO, STATED PLAINLY
-----------------------------------
Spec 8 asks that a project not write "outside the project directory or an
explicitly user-chosen path". Statically, that is undecidable: open(p, "w")
where p is computed at runtime cannot be resolved by reading the source. So this
module denies the calls whose PURPOSE is destruction (rmtree, unlink, remove,
rmdir) and the modules that escape the process entirely, and it does NOT claim
to prove where a write lands. Claiming an enforcement that does not exist would
be worse than the gap: it would make a reviewer stop looking.
"""
from __future__ import annotations

import ast
import sys
from typing import List, Sequence, Set, Tuple

MODES = ("linked", "standalone")

# Modules a "linked" project may import beyond the stdlib (spec 8).
# council_engine is ABSENT ON PURPOSE: importing it from a generated app would
# construct a SECOND GGUF singleton in a second process — two models, two VRAM
# allocations, and an inference lock that no longer serialises anything.
LINKED_MODULES = frozenset({
    "image_stats", "image_index", "plot_registry", "plots_pane", "graph_data",
    "vault_analyst", "data_index", "df_cache", "stats_cache", "provenance",
})

# Third-party packages both modes may use.
THIRD_PARTY = frozenset({"pandas", "numpy", "matplotlib", "PIL", "pillow"})

# The generated project's own modules.
PROJECT_MODULES = frozenset({"ui", "app", "handlers", "launch", "widgets",
                             "main_ui"})

# Denied in BOTH modes. Each escapes the process, executes arbitrary text, or
# deserialises into live objects.
DENIED_MODULES = frozenset({
    "subprocess", "socket", "requests", "urllib", "urllib2", "urllib3",
    "http", "ftplib", "telnetlib", "smtplib", "ctypes", "cffi",
    "pickle", "cPickle", "marshal", "shelve", "dill",
    "importlib", "imp", "runpy", "code", "codeop", "pty", "multiprocessing",
})

# Builtins that turn data into code.
DENIED_BUILTINS = frozenset({"eval", "exec", "compile", "__import__"})

# Attribute names denied wherever they appear. os and sys are PERMITTED — a
# real application needs os.path and sys.argv — so the dangerous surface is
# denied by name instead of by module.
DENIED_ATTRS = frozenset({
    # process / shell escape
    "system", "popen", "spawn", "spawnl", "spawnv", "execv", "execl", "execve",
    "fork", "forkpty", "kill", "killpg", "putenv",
    # destruction
    "rmtree", "unlink", "remove", "removedirs", "rmdir",
    # introspection escapes that reach the interpreter's own state
    "__subclasses__", "__bases__", "__mro__", "__globals__", "__code__",
    "__closure__", "__builtins__", "__import__", "__reduce__",
    "__reduce_ex__", "__getattribute__",
    # loaders
    "load_module", "import_module", "exec_module", "loads", "load",
})

# `loads`/`load` are denied above because pickle.loads is the risk, but json
# and PIL use the same spelling harmlessly. These receivers are exempted so a
# normal app is not rejected for reading its own settings file.
SAFE_LOAD_RECEIVERS = frozenset({"json", "yaml", "tomllib", "toml", "Image",
                                 "np", "numpy", "plt", "pd", "pandas"})


def _stdlib_names() -> Set[str]:
    """The stdlib module names for the running interpreter.

    sys.stdlib_module_names (3.10+) is authoritative and version-accurate,
    which a hand-maintained list never stays."""
    names = getattr(sys, "stdlib_module_names", None)
    if names:
        return set(names)
    return {  # pragma: no cover - only on <3.10, below this app's floor
        "abc", "argparse", "ast", "base64", "collections", "csv", "dataclasses",
        "datetime", "enum", "functools", "io", "itertools", "json", "logging",
        "math", "os", "pathlib", "random", "re", "shutil", "statistics",
        "string", "sys", "tempfile", "textwrap", "threading", "time", "tkinter",
        "traceback", "typing", "uuid", "warnings",
    }


def allowed_modules(mode: str) -> Set[str]:
    """Every root module name importable in ``mode``."""
    base = _stdlib_names() | set(THIRD_PARTY) | set(PROJECT_MODULES)
    if mode == "linked":
        base |= set(LINKED_MODULES)
    return base - set(DENIED_MODULES)


def validate(code: str, mode: str = "linked") -> Tuple[bool, List[str]]:
    """(ok, errors) for one source file.

    Returns EVERY fault, like gui_spec.validate — a user fixing generated or
    hand-written code should see the whole list, not one per run."""
    errs: List[str] = []
    if mode not in MODES:
        return False, [f"unknown import mode {mode!r}; expected one of {MODES}"]
    try:
        tree = ast.parse(code or "")
    except SyntaxError as exc:
        return False, [f"does not parse: line {exc.lineno}: {exc.msg}"]

    allowed = allowed_modules(mode)

    for node in ast.walk(tree):
        # ---- imports ----
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                _check_import(root, node, mode, allowed, errs)
        elif isinstance(node, ast.ImportFrom):
            if node.level:          # relative: inside the project, fine
                continue
            root = (node.module or "").split(".")[0]
            if root:
                _check_import(root, node, mode, allowed, errs)

        # ---- attribute access: CALLED OR NOT ----
        elif isinstance(node, ast.Attribute):
            if node.attr in DENIED_ATTRS:
                recv = _receiver_name(node.value)
                if node.attr in ("load", "loads") and recv in SAFE_LOAD_RECEIVERS:
                    continue
                where = f"{recv}." if recv else ""
                errs.append(
                    f"line {getattr(node, 'lineno', 0)}: {where}{node.attr} is "
                    f"not permitted in a generated app")

        # ---- name-level ----
        elif isinstance(node, ast.Name):
            if node.id in DENIED_BUILTINS:
                errs.append(f"line {getattr(node, 'lineno', 0)}: "
                            f"{node.id}() is not permitted")

        # ---- getattr(x, "system") — the string-indirection bypass ----
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == "getattr" \
                    and len(node.args) >= 2:
                arg = node.args[1]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                        and arg.value in (DENIED_ATTRS | DENIED_BUILTINS):
                    errs.append(
                        f"line {getattr(node, 'lineno', 0)}: "
                        f"getattr(..., {arg.value!r}) reaches a denied "
                        f"attribute by name")

    return (not errs), errs


def _check_import(root: str, node: ast.AST, mode: str, allowed: Set[str],
                  errs: List[str]) -> None:
    line = getattr(node, "lineno", 0)
    if root in DENIED_MODULES:
        errs.append(f"line {line}: importing {root!r} is not permitted "
                    f"in a generated app")
        return
    if root == "council_engine":
        errs.append(
            f"line {line}: council_engine must not be imported by a generated "
            f"app — it would build a second GGUF singleton in this process. "
            f"Route model access through the designer instead.")
        return
    if root in allowed:
        return
    if mode == "standalone" and root in LINKED_MODULES:
        errs.append(
            f"line {line}: {root!r} is an app module, so this project is not "
            f"standalone. Switch the project to linked mode, or remove it.")
        return
    errs.append(f"line {line}: {root!r} is not on the {mode} allowlist")


def _receiver_name(node: ast.AST) -> str:
    """A readable name for whatever an attribute was reached through."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_receiver_name(node.value)}.{node.attr}".lstrip(".")
    if isinstance(node, ast.Call):
        return _receiver_name(node.func)
    return ""


def validate_project(paths: Sequence, mode: str = "linked"
                     ) -> Tuple[bool, List[str]]:
    """Validate several files, prefixing each fault with its filename."""
    from pathlib import Path
    all_errs: List[str] = []
    for p in paths:
        p = Path(p)
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            all_errs.append(f"{p.name}: cannot read ({exc})")
            continue
        ok, errs = validate(src, mode)
        all_errs.extend(f"{p.name}: {e}" for e in errs)
    return (not all_errs), all_errs
