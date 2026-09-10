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
_PROBE = r'''
import json, sys
req = json.loads(sys.argv[1])
out = {"version": "%d.%d.%d" % tuple(sys.version_info[:3]),
       "executable": sys.executable, "tkinter": "", "tkinter_error": "",
       "missing": {}, "compile_errors": {}}
try:
    import tkinter
    out["tkinter"] = str(tkinter.TkVersion)
except Exception as exc:
    out["tkinter_error"] = "%s: %s" % (type(exc).__name__, exc)
for name in req.get("modules", []):
    # Announced BEFORE the import: a native crash kills this process with no
    # chance to report, and the last announcement is then the only way to say
    # WHICH package took it down.
    sys.stdout.write("__TRY__" + name + "\n")
    sys.stdout.flush()
    try:
        __import__(name)
    except BaseException as exc:
        out["missing"][name] = "%s: %s" % (type(exc).__name__, exc)
for path in req.get("files", []):
    try:
        with open(path, "rb") as fh:
            compile(fh.read(), path, "exec")
    except SyntaxError as exc:
        out["compile_errors"][path] = "line %s: %s" % (exc.lineno, exc.msg)
    except Exception as exc:
        out["compile_errors"][path] = "%s: %s" % (type(exc).__name__, exc)
sys.stdout.write("__PROBE__" + json.dumps(out) + "\n")
'''


@dataclass
class Probe:
    ok: bool
    version: str = ""
    tkinter: str = ""
    missing: Dict[str, str] = field(default_factory=dict)
    compile_errors: Dict[str, str] = field(default_factory=dict)
    error: str = ""        # the interpreter itself could not be run


def probe(python: str, *, modules: Sequence[str] = (),
          files: Sequence[str] = (), timeout: float = PROBE_TIMEOUT) -> Probe:
    """Ask ``python`` about itself. Never raises."""
    payload = json.dumps({"modules": list(modules),
                          "files": [str(f) for f in files]})
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONFAULTHANDLER="1")
    try:
        r = subprocess.run([python, "-c", _PROBE, payload],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return Probe(False, error=f"it did not answer within {timeout:g} s "
                     f"(an import may be hanging)")
    except OSError as exc:
        return Probe(False, error=f"it could not be started: {exc}")
    lines = r.stdout.splitlines()
    line = next((l for l in lines if l.startswith("__PROBE__")), "")
    if not line:
        tried = [l[len("__TRY__"):] for l in lines if l.startswith("__TRY__")]
        code = r.returncode
        shown = f"0x{code & 0xFFFFFFFF:08X}" if (code < 0 or code > 0xFFFF) \
            else str(code)
        if tried:
            # Measured on this machine: an env whose numpy was pip-installed
            # dies inside the import with 0xC06D007F (a delay-load DLL fault)
            # and no Python traceback at all. find_spec would have said "fine".
            return Probe(False, missing={tried[-1]: (
                f"importing it CRASHED this Python (exit code {shown}) — "
                f"usually a package built for a different Python, or a "
                f"missing DLL")})
        tail = (r.stderr or r.stdout).strip().splitlines()[-3:]
        return Probe(False, error="it crashed while checking itself"
                     + (": " + " | ".join(tail) if tail else
                        f" (exit code {shown})"))
    d = json.loads(line[len("__PROBE__"):])
    missing = dict(d.get("missing") or {})
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
    for mod, why in pr.missing.items():
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
    ok, errs = gui_policy.validate_dir(pdir, mode, requires)
    if not ok:
        return Preflight(False, "", ["Not started: the policy gate refused "
                                     "this code:"] + ["  - " + e for e in errs])
    res = resolve(spec)
    pr = None if res.error else probe(res.python, modules=list(requires),
                                      files=project_files(pdir))
    good = not res.error and pr is not None and pr.ok
    return Preflight(good, res.python if good else "", describe(res, pr))


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
    """Every .py a generated project runs, for the compile check."""
    pdir = Path(pdir)
    files = sorted((pdir / "ui").glob("*.py"))
    files += [pdir / n for n in ("app.py", "handlers.py", "main.py")
              if (pdir / n).is_file()]
    return [str(f) for f in files]
