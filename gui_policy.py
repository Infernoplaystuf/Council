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

For the same reason: a name assembled at runtime (getattr(os, "sys" + "tem"))
cannot be read statically either. Every SPELLED form is checked — attribute,
`from os import system`, `from os import *`, getattr with a literal, and the
namespace dicts (__dict__, vars(), globals()) a literal key would reach it
through — but this is a gate against generated code going wrong, not a
sandbox against code written to escape it.
"""
from __future__ import annotations

import ast
import keyword
import sys
from pathlib import Path
from typing import List, Sequence, Set, Tuple

# The Council's own source directory. A `requires` naming a module here is
# Council code, not a package — see check_requires.
APP_ROOT = Path(__file__).resolve().parent

MODES = ("linked", "standalone")

# Modules a "linked" project may import beyond the stdlib (spec 8).
# council_engine is ABSENT ON PURPOSE: importing it from a generated app would
# construct a SECOND GGUF singleton in a second process — two models, two VRAM
# allocations, and an inference lock that no longer serialises anything.
LINKED_MODULES = frozenset({
    "image_stats", "image_index", "plot_registry", "plots_pane", "graph_data",
    "vault_analyst", "data_index", "df_cache", "stats_cache", "provenance",
    "frame_timing", "frame_roi", "frame_classes",
})

# Third-party packages both modes may use.
THIRD_PARTY = frozenset({"pandas", "numpy", "matplotlib", "PIL", "pillow"})

# The generated project's own modules.
# "main" is the entry point since the launch.py -> main.py rename, and the
# launch.py shim left in older projects is literally `from main import main`.
# Without it here that shim — generated code — failed the project's own gate,
# so every older project reported "policy REFUSED" on Generate while running
# fine, which teaches a user to ignore the one message that matters.
PROJECT_MODULES = frozenset({"ui", "app", "handlers", "launch", "main",
                             "widgets", "main_ui"})

# Denied in BOTH modes. Each escapes the process, executes arbitrary text, or
# deserialises into live objects.
DENIED_MODULES = frozenset({
    "subprocess", "socket", "requests", "urllib", "urllib2", "urllib3",
    "http", "ftplib", "telnetlib", "smtplib", "ctypes", "cffi",
    "pickle", "cPickle", "marshal", "shelve", "dill",
    "importlib", "imp", "runpy", "code", "codeop", "pty", "multiprocessing",
    # `builtins.exec(...)` is exec — and `compile` cannot be denied as an
    # attribute, because re.compile is everywhere.
    "builtins",
})

# Builtins that turn data into code, and the namespace dicts that hand any
# denied name back through a string key: vars(os)["system"],
# globals()["__builtins__"]["eval"]. Generated code uses none of them.
DENIED_BUILTINS = frozenset({"eval", "exec", "compile", "__import__",
                             "__builtins__", "globals", "vars", "locals"})

# `from X import *` pulls every public name in — `from os import *` is
# `system` with no attribute left to check. Tkinter is the one library whose
# documentation teaches star imports, and it exports nothing on the deny list.
STAR_IMPORT_OK = frozenset({"tkinter"})

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
    "__reduce_ex__", "__getattribute__", "__dict__",
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


def _roots(names: Sequence[str]) -> Set[str]:
    return {str(n).strip().split(".")[0] for n in names if str(n).strip()}


def allowed_modules(mode: str, extra: Sequence[str] = ()) -> Set[str]:
    """Every root module name importable in ``mode``.

    ``extra`` is the project's declared `requires` — a camera app's SDK — plus,
    from validate_dir, the project's own modules. It widens the allowlist for
    THAT project only, so an import nobody declared is still refused, and it
    can never re-admit a denied module, nor an app module into a standalone
    project: those subtractions happen last."""
    base = _stdlib_names() | set(THIRD_PARTY) | set(PROJECT_MODULES)
    if mode == "linked":
        base |= set(LINKED_MODULES)
    # Council code is never admitted this way (check_requires says why).
    base |= {r for r in _roots(extra)
             if r in LINKED_MODULES or r in PROJECT_MODULES
             or not is_council_module(r)}
    if mode != "linked":
        base -= set(LINKED_MODULES)
    return base - set(DENIED_MODULES)


def as_requires(value) -> List[str]:
    """A `requires` value as a clean list, whatever shape it arrived in.

    A hand-edited or model-written gspec may say "requires": "numpy" where a
    list was meant; iterating that string gave ['n', 'u', 'm', 'p', 'y'] and
    main.py imported each letter."""
    if value is None:
        return []
    if isinstance(value, str):
        return parse_requires(value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value).strip()] if str(value).strip() else []


def parse_requires(text) -> List[str]:
    """'pypylon, numpy PIL' -> ['pypylon', 'numpy', 'PIL'].

    Commas or whitespace separate; order is kept and repeats dropped, so what
    the user typed is what the gspec stores. Validity is check_requires' job."""
    out: List[str] = []
    for part in str(text or "").replace(",", " ").split():
        if part not in out:
            out.append(part)
    return out


def is_council_module(root: str) -> bool:
    """Whether ``root`` is a module of the Council itself (a .py file or a
    package beside this one)."""
    return ((APP_ROOT / f"{root}.py").is_file()
            or (APP_ROOT / root / "__init__.py").is_file())


def check_requires(names: Sequence[str], mode: str = "linked") -> List[str]:
    """Problems with a project's declared `requires`, one message each.

    Declaring a package is how a project widens its own allowlist, so the
    declaration itself is gated: a denied module (subprocess, socket, pickle,
    ...) cannot be declared into it, and neither can Council code. `requires`
    is for PACKAGES. The Council modules an app may reach are LINKED_MODULES,
    and only in linked mode: council_agents, vault_rag and the rest import
    council_engine at load, so declaring one would build the second GGUF
    singleton the council_engine ban exists to prevent."""
    errs: List[str] = []
    seen: Set[str] = set()
    for raw in as_requires(names):
        name = str(raw).strip()
        if not name:
            continue
        parts = name.split(".")
        if not all(p.isidentifier() and not keyword.iskeyword(p)
                   for p in parts):
            errs.append(f"requires {name!r} is not an importable module name "
                        f"(one package per entry, by its IMPORT name — PIL, "
                        f"not Pillow; no 'as')")
            continue
        root = parts[0]
        if root in DENIED_MODULES:
            errs.append(f"requires {name!r}: {root!r} is never permitted in a "
                        f"generated app, declared or not")
        elif root == "council_engine":
            errs.append("requires 'council_engine': it would build a second "
                        "GGUF singleton inside the app")
        elif root in LINKED_MODULES:
            if mode != "linked":
                errs.append(f"requires {name!r}: {root!r} is Council code, so "
                            f"a standalone project cannot use it — switch the "
                            f"project to linked mode")
        elif root not in PROJECT_MODULES and is_council_module(root):
            errs.append(f"requires {name!r}: {root!r} is part of the Council, "
                        f"not a package. An app may use only the linked "
                        f"modules: {', '.join(sorted(LINKED_MODULES))}")
        if name in seen:
            errs.append(f"requires {name!r} is listed twice")
        seen.add(name)
    return errs


def validate(code: str, mode: str = "linked",
             extra_modules: Sequence[str] = ()) -> Tuple[bool, List[str]]:
    """(ok, errors) for one source file.

    Returns EVERY fault, like gui_spec.validate — a user fixing generated or
    hand-written code should see the whole list, not one per run.

    ``extra_modules`` is the project's declared `requires`."""
    errs: List[str] = []
    if mode not in MODES:
        return False, [f"unknown import mode {mode!r}; expected one of {MODES}"]
    try:
        tree = ast.parse(code or "")
    except SyntaxError as exc:
        return False, [f"does not parse: line {exc.lineno}: {exc.msg}"]

    allowed = allowed_modules(mode, extra_modules)

    for node in ast.walk(tree):
        # ---- imports ----
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                _check_import(root, node, mode, allowed, errs)
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root and not node.level:     # relative: inside the project
                _check_import(root, node, mode, allowed, errs)
            # `from os import system` binds the denied attribute to a plain
            # name, and a plain name is never checked again — so the IMPORTED
            # NAMES are checked here, relative imports included.
            line = getattr(node, "lineno", 0)
            for a in node.names:
                if a.name == "*":
                    if node.level or root in STAR_IMPORT_OK:
                        continue
                    errs.append(f"line {line}: `from {node.module} import *` "
                                f"is not permitted in a generated app — "
                                f"import the names it uses")
                elif a.name in DENIED_BUILTINS or (
                        a.name in DENIED_ATTRS
                        and not (a.name in ("load", "loads")
                                 and root in SAFE_LOAD_RECEIVERS)):
                    errs.append(f"line {line}: importing {a.name!r} from "
                                f"{node.module or '.'} is not permitted in a "
                                f"generated app")

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
                call = "" if node.id.startswith("__b") else "()"
                errs.append(f"line {getattr(node, 'lineno', 0)}: "
                            f"{node.id}{call} is not permitted")

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
    if root not in PROJECT_MODULES and is_council_module(root):
        errs.append(f"line {line}: {root!r} is part of the Council — an app "
                    f"may use only the linked modules")
        return
    errs.append(f"line {line}: {root!r} is not on the {mode} allowlist — if "
                f"the app needs it, add it to the project's requires")


def _receiver_name(node: ast.AST) -> str:
    """A readable name for whatever an attribute was reached through."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_receiver_name(node.value)}.{node.attr}".lstrip(".")
    if isinstance(node, ast.Call):
        return _receiver_name(node.func)
    return ""


# Folders inside a project that hold no code the app runs.
_SKIP_DIRS = frozenset({"__pycache__", ".git", ".venv", "venv", "env"})


def project_sources(pdir) -> list:
    """Every .py in a generated project, subfolders included.

    Not a fixed list of the files generation writes: the app can import any
    module it can reach, and a helper module the gate never read was a way
    past it — measured, a widgets.py beside main.py ran subprocess with the
    gate saying OK. Built from what EXISTS, so the launch.py shim left in
    older projects is gated too."""
    pdir = Path(pdir)
    out = []
    for p in sorted(pdir.rglob("*.py")):
        rel = p.relative_to(pdir).parts[:-1]
        if any(part in _SKIP_DIRS or part.startswith(".") for part in rel):
            continue
        out.append(p)
    return out


def project_modules(pdir) -> List[str]:
    """The top-level module names a project provides itself — its own .py
    files and packages — which its code may import.

    Safe to admit because project_sources gates every one of them. A local
    file cannot re-admit a denied module or council_engine: allowed_modules
    and _check_import still subtract and refuse those by name."""
    pdir = Path(pdir)
    names = [p.stem for p in pdir.glob("*.py")]
    names += [d.name for d in pdir.iterdir()
              if d.is_dir() and (d / "__init__.py").is_file()]
    return sorted({n for n in names if n.isidentifier()})


def validate_dir(pdir, mode: str = "linked",
                 extra_modules: Sequence[str] = ()) -> Tuple[bool, List[str]]:
    """The gate over a whole project directory. The one call Generate, Run
    and run_example_gui all make, so they cannot disagree about a project."""
    return validate_project(project_sources(pdir), mode, extra_modules,
                            local_modules=project_modules(pdir), root=pdir)


def validate_project(paths: Sequence, mode: str = "linked",
                     extra_modules: Sequence[str] = (),
                     local_modules: Sequence[str] = (), root=None
                     ) -> Tuple[bool, List[str]]:
    """Validate several files, prefixing each fault with its filename (its
    path under ``root`` when given, so ui/app.py and app.py stay distinct).

    ``extra_modules`` is the project's declared `requires`; a bad declaration
    is itself a fault. ``local_modules`` are the project's own modules."""
    all_errs: List[str] = list(check_requires(extra_modules, mode))
    importable = as_requires(extra_modules) + list(local_modules)
    for p in paths:
        p = Path(p)
        try:
            name = p.relative_to(root).as_posix() if root else p.name
        except ValueError:
            name = p.name
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            all_errs.append(f"{name}: cannot read ({exc})")
            continue
        ok, errs = validate(src, mode, importable)
        all_errs.extend(f"{name}: {e}" for e in errs)
    return (not all_errs), all_errs
