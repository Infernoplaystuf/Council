"""
python_envs.py — which Python runs a generated app, and can it?

A generated app normally runs under the Council's own Python. A camera app
cannot: vendor SDKs (pypylon, Metavision) ship compiled for particular Pythons
with their own C++ runtimes, so they live in a separate env — the same reason
DREAM3D lives in `nxpython`. So each project may name the Python that runs it.

THE SETTING (Manifest.python), one of:

    ""                      the Council's own Python — the default, and what
                            every project did before this setting existed
    "pylon"                 a conda env, by NAME, so it survives reinstalling
                            or moving miniconda
    "C:\\...\\python.exe"     an explicit interpreter (a venv, a python.org install)

It lives in the project's MANIFEST, not its wireframe. A .gspec is the portable
part — committed as an example, pulled onto other machines — and a Python path
names one machine. The portable half is the wireframe's `requires`: the gspec
says what it needs, each machine says which Python provides it.

THE SELF-CHECK (probe)
----------------------
Before a launch the chosen Python checks ITSELF, in its own process:

  * its version, and that it has tkinter (no GUI without it)
  * every required module — actually IMPORTED, not just located: a compiled
    extension built for the wrong ABI is found by find_spec and then dies at
    import, and that is the exact failure this setting exists to avoid
  * that every generated file COMPILES under it. The policy gate judges code
    against the Council's Python, so syntax newer than the target (a `match`,
    `except*`) passes the gate and then crashes on an older vendor Python —
    measured on 3.9. Letting the target compile the files closes that gap.

No `conda run` fallback, ever: it buffers the child's output and cannot be
stopped cleanly. An env's own python.exe is always called directly.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# Same roots nx_bridge searches for the DREAM3D env, so a camera env and the
# nx env are found the same way.
CONDA_ROOTS = [
    Path.home() / "miniforge3",
    Path.home() / "miniconda3",
    Path.home() / "anaconda3",
    Path("C:/ProgramData/miniforge3"),
    Path("C:/ProgramData/Anaconda3"),
    Path("C:/ProgramData/miniconda3"),
    Path("/opt/conda"),
]

DEFAULT_LABEL = "Council's Python"
PROBE_TIMEOUT = 90.0     # importing a vendor SDK can be slow the first time


def is_frozen() -> bool:
    """True inside the packaged DatasInferno.exe, where sys.executable is the
    app itself and there is no Python to fall back to."""
    return bool(getattr(sys, "frozen", False))


def _env_python(env_dir: Path) -> Optional[Path]:
    for rel in ("python.exe", "bin/python", "bin/python3"):
        p = env_dir / rel
        if p.is_file():
            return p
    return None


def list_envs() -> List[Tuple[str, str]]:
    """Every conda env found on this machine: (name, python path), by name.

    The base install is listed as "base". Names are unique — the first root
    that has a given env wins, matching find_env."""
    seen: Dict[str, str] = {}
    for root in CONDA_ROOTS:
        base = _env_python(root)
        if base is not None and "base" not in seen:
            seen["base"] = str(base)
        envs = root / "envs"
        if not envs.is_dir():
            continue
        try:
            children = sorted(envs.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for d in children:
            py = _env_python(d)
            if py is not None and d.name not in seen:
                seen[d.name] = str(py)
    return sorted(seen.items(), key=lambda kv: kv[0].lower())


def find_env(name: str) -> Optional[str]:
    return dict(list_envs()).get(name)


def looks_like_path(spec: str) -> bool:
    """An explicit interpreter path, as opposed to a conda env name."""
    return (os.sep in spec or "/" in spec or "\\" in spec
            or spec.lower().endswith(".exe"))


@dataclass
class Resolved:
    spec: str              # what the manifest said
    python: str            # the interpreter to run, or "" when unresolved
    label: str             # how to name it to the user
    error: str = ""


def resolve(spec: str) -> Resolved:
    """Turn a manifest setting into an interpreter path."""
    spec = str(spec or "").strip().strip('"')
    if not spec:
        if is_frozen():
            return Resolved(spec, "", DEFAULT_LABEL,
                            "this packaged build has no Python of its own — "
                            "choose one under 'Run with'")
        return Resolved(spec, sys.executable, DEFAULT_LABEL)
    if looks_like_path(spec):
        p = Path(spec).expanduser()
        if p.is_dir():
            inner = _env_python(p)
            if inner is None:
                return Resolved(spec, "", str(p),
                                f"no python.exe inside {p}")
            p = inner
        if not p.is_file():
            return Resolved(spec, "", str(p), f"{p} does not exist")
        return Resolved(spec, str(p), str(p))
    py = find_env(spec)
    if py is None:
        names = [n for n, _ in list_envs()]
        known = (", ".join(names[:12]) + (" ..." if len(names) > 12 else "")
                 ) or "none found"
        return Resolved(spec, "", f"conda env '{spec}'",
                        f"no conda env named '{spec}' on this machine "
                        f"(found: {known})")
    return Resolved(spec, py, f"conda env '{spec}'")


# Runs INSIDE the target interpreter. Deliberately plain: no f-strings, no
# walrus, no annotations, so a 3.6-era vendor Python can run it too.
#
# It answers through FILES, not stdout. A package is free to print while it
# imports, and stdout was measured failing three ways: a banner with no
# trailing newline swallowed the marker, so a working Python was reported as
# having CRASHED; a package printing the marker itself could answer "ready";
# and a helper process an SDK started kept the pipe open, so a finished probe
# waited out its timeout and was reported as hanging.
_PROBE = r'''
import json, sys
req = json.loads(sys.argv[1])
sys.path[0:0] = req.get("path", [])


def _note(path, text):
    fh = open(path, "w", encoding="utf-8")
    fh.write(text)
    fh.close()


out = {"version": "%d.%d.%d" % tuple(sys.version_info[:3]),
       "executable": sys.executable, "tkinter": "", "tkinter_error": "",
       "missing": {}, "not_found": [], "compile_errors": {}}
try:
    import tkinter
    out["tkinter"] = str(tkinter.TkVersion)
except Exception as exc:
    out["tkinter_error"] = "%s: %s" % (type(exc).__name__, exc)
for name in req.get("modules", []):
    # Noted BEFORE the import: a native crash kills this process with no
    # chance to report, and the last note is then the only way to say WHICH
    # package took it down.
    _note(req["trying"], name)
    try:
        __import__(name)
    except BaseException as exc:
        out["missing"][name] = "%s: %s" % (type(exc).__name__, exc)
_note(req["trying"], "")
try:
    from importlib.util import find_spec
except Exception:
    find_spec = None
for name in req.get("locate", []):
    # Located, not imported: the imports the app makes at startup that are
    # not declared (numpy is allowed undeclared; tomllib is stdlib on 3.11
    # but not on 3.9). find_spec on a top-level name imports nothing.
    try:
        if find_spec is not None and find_spec(name) is None:
            out["not_found"].append(name)
    except Exception:
        out["not_found"].append(name)
for path in req.get("files", []):
    try:
        with open(path, "rb") as fh:
            compile(fh.read(), path, "exec")
    except SyntaxError as exc:
        out["compile_errors"][path] = "line %s: %s" % (exc.lineno, exc.msg)
    except Exception as exc:
        out["compile_errors"][path] = "%s: %s" % (type(exc).__name__, exc)
_note(req["result"], json.dumps(out))
'''


@dataclass
class Probe:
    ok: bool
    version: str = ""
    tkinter: str = ""
    missing: Dict[str, str] = field(default_factory=dict)
    compile_errors: Dict[str, str] = field(default_factory=dict)
    error: str = ""        # the interpreter itself could not be run


NOT_FOUND = "the app imports it at startup, and this Python does not have it"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def probe(python: str, *, modules: Sequence[str] = (),
          files: Sequence[str] = (), locate: Sequence[str] = (),
          path: Sequence[str] = (), cwd: Optional[str] = None,
          timeout: float = PROBE_TIMEOUT) -> Probe:
    """Ask ``python`` about itself. Never raises.

    ``modules`` are imported, ``locate`` only found; ``path`` goes at the
    front of the target's sys.path and ``cwd`` is where it runs — the same
    two things the generated main.py sets up, so a module the app finds (its
    own helper file, a linked Council module) is found here too."""
    import tempfile
    with tempfile.TemporaryDirectory(prefix="council_probe_",
                                     ignore_cleanup_errors=True) as tmp:
        tmp = Path(tmp)
        payload = json.dumps({"modules": list(modules),
                              "locate": list(locate),
                              "files": [str(f) for f in files],
                              "path": [str(p) for p in path],
                              "trying": str(tmp / "trying.txt"),
                              "result": str(tmp / "result.json")})
        env = dict(os.environ, PYTHONIOENCODING="utf-8",
                   PYTHONFAULTHANDLER="1")
        log = tmp / "output.txt"
        try:
            # Output goes to a FILE: subprocess.run waits for a pipe to reach
            # end-of-file, and an SDK's helper process holding it open made a
            # finished probe wait out its whole timeout.
            with open(log, "wb") as out:
                r = subprocess.run([python, "-c", _PROBE, payload],
                                   stdin=subprocess.DEVNULL, stdout=out,
                                   stderr=subprocess.STDOUT, timeout=timeout,
                                   env=env, cwd=cwd or None)
        except subprocess.TimeoutExpired:
            return Probe(False, error=f"it did not answer within {timeout:g} "
                         f"s (an import may be hanging)")
        except OSError as exc:
            return Probe(False, error=f"it could not be started: {exc}")
        try:
            d = json.loads(_read(tmp / "result.json"))
            if not isinstance(d, dict):
                raise ValueError("not a JSON object")
        except ValueError:
            d = None
        if d is None:
            tried = _read(tmp / "trying.txt").strip()
            code = r.returncode
            shown = (f"0x{code & 0xFFFFFFFF:08X}"
                     if (code < 0 or code > 0xFFFF) else str(code))
            if tried:
                # Measured on this machine: an env whose numpy was
                # pip-installed dies inside the import with 0xC06D007F (a
                # delay-load DLL fault) and no Python traceback at all.
                # find_spec would have said "fine".
                return Probe(False, missing={tried: (
                    f"importing it CRASHED this Python (exit code {shown}) — "
                    f"usually a package built for a different Python, or a "
                    f"missing DLL")})
            tail = _read(log).strip().splitlines()[-3:]
            return Probe(False, error="it crashed while checking itself"
                         + (": " + " | ".join(tail) if tail else
                            f" (exit code {shown})"))
    missing = dict(d.get("missing") or {})
    for name in d.get("not_found") or []:
        missing.setdefault(str(name), NOT_FOUND)
    compile_errors = dict(d.get("compile_errors") or {})
    tk = str(d.get("tkinter") or "")
    ok = bool(tk) and not missing and not compile_errors
    pr = Probe(ok, str(d.get("version") or ""), tk, missing, compile_errors)
    if not tk:
        pr.error = "it has no tkinter: " + str(d.get("tkinter_error") or "")
    return pr


# Import name -> what to install. Only where the two differ, which is exactly
# where a user reading "missing PIL" would otherwise be stuck.
INSTALL_HINTS = {
    "PIL": "Pillow", "cv2": "opencv-python", "yaml": "PyYAML",
    "sklearn": "scikit-learn", "skimage": "scikit-image",
    "pypylon": "pypylon",
}


def install_hint(module: str) -> str:
    if module.startswith("metavision"):
        return "the Metavision SDK from Prophesee"
    return INSTALL_HINTS.get(module, module)


def describe(res: Resolved, pr: Optional[Probe]) -> List[str]:
    """Plain-language lines for the designer's log. The first says whether the
    app will start; the rest say why not."""
    if res.error:
        return [f"Not started: {res.error}."]
    assert pr is not None
    head = f"{res.label} (Python {pr.version})" if pr.version else res.label
    if pr.ok:
        return [f"Run with {head}: ready."]
    out = [f"Not started: {head} cannot run this app."]
    if pr.error:
        out.append(f"  - {pr.error}")
    import gui_policy
    stdlib = gui_policy._stdlib_names()
    for mod, why in pr.missing.items():
        if why == NOT_FOUND and mod in stdlib:
            # tomllib under 3.9, imghdr under 3.13: the gate judged the code
            # against the Council's Python, whose standard library differs.
            out.append(f"  - {mod} is standard library in the Council's "
                       f"Python {sys.version_info[0]}.{sys.version_info[1]} "
                       f"but not in this one — choose another Python, or "
                       f"stop importing it")
            continue
        out.append(f"  - missing {mod} — install {install_hint(mod)} into "
                   f"that Python ({why})")
    for path, why in pr.compile_errors.items():
        out.append(f"  - {Path(path).name} does not compile under Python "
                   f"{pr.version}: {why}")
    return out


@dataclass
class Preflight:
    """Everything that must hold before a generated app launches."""
    ok: bool
    python: str                 # the interpreter to launch with, when ok
    lines: List[str]            # for the log; the first says go or no-go


def preflight(pdir, spec: str, mode: str = "linked",
              requires: Sequence[str] = ()) -> Preflight:
    """The whole pre-launch check, in the order a user needs the answers:

      1. the policy gate — with the project's `requires` on its allowlist
      2. the interpreter named in the manifest exists
      3. that interpreter can run THIS app (probe)

    One function for the designer's Run and for run_example_gui, so the two
    cannot disagree about whether a project may start."""
    import gui_policy
    requires = gui_policy.as_requires(requires)
    ok, errs = gui_policy.validate_dir(pdir, mode, requires)
    if not ok:
        return Preflight(False, "", ["Not started: the policy gate refused "
                                     "this code:"] + ["  - " + e for e in errs])
    res = resolve(spec)
    pr = None
    if not res.error:
        files = project_files(pdir)
        # The target's sys.path as the generated main.py builds it: the app
        # root in front (linked mode), then the project.
        path = ([str(gui_policy.APP_ROOT)] if mode == "linked" else []) \
            + [str(Path(pdir).resolve())]
        pr = probe(res.python, modules=requires, files=files,
                   locate=[m for m in startup_imports(files)
                           if m not in requires],
                   path=path, cwd=str(pdir))
    good = not res.error and pr is not None and pr.ok
    return Preflight(good, res.python if good else "", describe(res, pr))


def startup_imports(files: Sequence[str]) -> List[str]:
    """Top-level modules the app imports AS IT STARTS: absolute imports at
    module level of its own files.

    Not those inside a try (optional by construction) or a function (needed
    only when that feature is used). Each of these must exist for the app to
    open at all, and the gate cannot know whether the target Python has
    them: it allows numpy undeclared, and it judges the stdlib by the
    Council's own version."""
    import ast
    out: List[str] = []
    for f in files:
        try:
            tree = ast.parse(Path(f).read_text(encoding="utf-8",
                                               errors="replace"))
        except (OSError, SyntaxError, ValueError):
            continue            # the compile check reports a bad file
        for node in tree.body:
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                names = [node.module or ""]
            else:
                continue
            for n in names:
                root = n.split(".")[0]
                if root and root != "__future__" and root not in out:
                    out.append(root)
    return out


# -- the designer's "Run with" choices ----------------------------------
BROWSE_LABEL = "Browse for python.exe..."
_CONDA_PREFIX = "conda: "


def display(spec: str) -> str:
    """How a manifest setting reads in the dropdown."""
    spec = str(spec or "")
    if not spec:
        return DEFAULT_LABEL
    return spec if looks_like_path(spec) else _CONDA_PREFIX + spec


def choices(current: str = "") -> List[str]:
    """Dropdown values: the default, every conda env, the current explicit
    path if there is one, and Browse. Rebuilt each time it opens, so an env
    created since the Council started appears without a restart."""
    values = [DEFAULT_LABEL] + [_CONDA_PREFIX + n for n, _ in list_envs()]
    if current and current not in values and current != BROWSE_LABEL:
        values.append(current)
    return values + [BROWSE_LABEL]


def spec_from_choice(choice: str) -> str:
    """The manifest setting for a dropdown choice (not BROWSE_LABEL)."""
    if choice == DEFAULT_LABEL:
        return ""
    if choice.startswith(_CONDA_PREFIX):
        return choice[len(_CONDA_PREFIX):]
    return choice


def project_files(pdir) -> List[str]:
    """Every .py a generated project runs, for the compile check — the same
    set the policy gate reads, so neither can miss a file the other sees."""
    import gui_policy
    return [str(f) for f in gui_policy.project_sources(pdir)]
