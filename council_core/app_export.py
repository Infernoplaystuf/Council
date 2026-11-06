"""
Export an app built in the GUI Designer to a folder of its own (Andy, 6
November 2026: "guis like Typhon created within the Council [should] have
their own folders that I can then copy and move to other machines and run
the GUI there … the Council is the development suite for Typhon but I can
export it to other machines").

WHAT AN EXPORTED FOLDER HOLDS

    <dest>/<app>/
        main.py, app.py, handlers.py, ui/, project.gspec, manifest.json ...
                     the project folder as it is (its camera set-up and
                     presets too); main.py's path block rewritten to _council
        _council/    exactly the Council files the app uses, found by
                     reading their imports (a linked app may import the
                     analysis modules: frame_camera, frame_classes ...; each
                     of those imports more), and the files they read
        _council/SOURCE_COMMIT
                     the Council commit it was exported from (a capture's
                     run record names it, as it names the checkout's commit)
        data/        made on first run: the app's own vault, settings,
                     plugin home and classifiers — nothing of this PC's
        run.bat, run.sh
                     start it with that data folder; PYTHON names the Python
        requirements.txt
                     the packages it needs (pip install -r), the optional
                     ones (a camera's SDK) as comments
        README.txt, export.json
                     how to run it; what was exported, from where, when

Nothing of the Council's models, Ledger, Connections or engine goes with it:
only what the app's imports reach. A standalone project (no Council
modules) gets the same folder with an empty _council.

WHAT IS NEVER DONE
The project is only read. An existing destination is refused, never
replaced or merged into. The vault's data stays behind unless asked for
(`with_classifiers`: a copy of the classifier store, under data/).

CHECKED, NOT ASSUMED
`check(folder)` starts the exported app in a separate Python (`-I`, a
temporary data folder, offscreen) that cannot see the Council: it imports
every file under _council, builds the app's window and closes it, and says
which module was missing. The Designer's Export runs it after every export.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

#: The Council's own folder (the repository root, or a bundle's).
COUNCIL_ROOT = Path(__file__).resolve().parent.parent
#: Inside an exported folder.
LIB_DIR = "_council"
DATA_DIR = "data"
EXPORT_FILE = "export.json"
EXPORT_FORMAT = "council-app-export"
COMMIT_FILE = "SOURCE_COMMIT"
#: The exported folder's own setup check (check.bat / check.sh run it).
CHECK_SCRIPT = "check_setup.py"

#: Never copied out of a project folder.
SKIP_NAMES = {"__pycache__", ".pytest_cache", ".git", "logs"}
SKIP_SUFFIXES = {".pyc", ".pyo"}

#: Import name -> the name pip installs it under, where they differ.
PIP_NAMES = {"PIL": "Pillow", "sklearn": "scikit-learn", "cv2":
             "opencv-python", "yaml": "PyYAML", "skimage": "scikit-image",
             "pypylon": "pypylon"}
#: Packages an app needs only for some hardware or feature: listed as
#: comments in requirements.txt, with what they are for.
OPTIONAL = {"pypylon": "Basler cameras (also the pylon runtime; CoaXPress "
                       "needs the pylon Software Suite's CXP support and the "
                       "frame grabber's driver)",
            "metavision_hal": "Prophesee event cameras (the Metavision SDK; "
                              "not on PyPI)",
            "metavision_sdk_core": "Prophesee event cameras",
            "metavision_core": "Prophesee event cameras",
            "sklearn": "the classifier (Classification)",
            "matplotlib": "plots",
            "pandas": "tables",
            "scipy": "some analysis tools",
            "psutil": "memory and process readings",
            "openpyxl": "Excel files",
            "tifffile": "TIFF frames",
            "h5py": "HDF5 files",
            "cv2": "image processing",
            "PIL": "pictures",
            "numpy": "arrays"}
#: Imported by the Council only for its own windows or developer tools,
#: never needed by an exported app.
NEVER = {"PySide6", "shiboken6", "tkinter", "_tkinter", "pytest"}


class ExportError(Exception):
    """An export that was refused before anything was written."""


@dataclass
class ExportResult:
    ok: bool
    folder: Optional[Path]
    message: str
    modules: List[str] = field(default_factory=list)
    files: List[str] = field(default_factory=list)
    required: List[str] = field(default_factory=list)
    optional: List[str] = field(default_factory=list)


@dataclass
class CheckResult:
    ok: bool
    message: str
    output: str = ""


# ======================================================================
# What the app uses
# ======================================================================
def _module_file(name: str, root: Path = COUNCIL_ROOT) -> Optional[Path]:
    """The Council file of module `name`: a package's __init__ first (so
    the council_qt package wins over the council_qt.py entry point), else a
    module. None for anything not in the Council."""
    if not name or name.startswith("."):
        return None
    base = root.joinpath(*name.split("."))
    init = base / "__init__.py"
    if init.is_file():
        return init
    module = base.with_suffix(".py")
    if module.is_file():
        return module
    return None


def _imports(path: Path, package: str) -> Tuple[Set[str], Set[str]]:
    """(module names, string arguments of importlib.import_module) that the
    file names anywhere — function-level imports included, since a lazy
    import is still one the app makes when that function runs."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return set(), set()
    names: Set[str] = set()
    dynamic: Set[str] = set()
    # Module-level text constants: import_module(SOME_MODULE) names one.
    constants: Dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(
                node.value, ast.Constant) and isinstance(node.value.value,
                                                         str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value

    def named(arg: ast.AST) -> Optional[str]:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        if isinstance(arg, ast.Name):
            return constants.get(arg.id)
        if isinstance(arg, ast.JoinedStr):
            # f"{__package__}.plugin_host"
            text = ""
            for part in arg.values:
                if isinstance(part, ast.Constant):
                    text += str(part.value)
                elif isinstance(part, ast.FormattedValue) and isinstance(
                        part.value, ast.Name) and \
                        part.value.id == "__package__":
                    text += package
                else:
                    return None
            return text
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".") if package else []
                keep = len(parts) - node.level + 1
                base = parts[:max(keep, 0)]
                module = ".".join(base + ([node.module] if node.module
                                          else []))
            else:
                module = node.module or ""
            if module:
                names.add(module)
                names.update(f"{module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Call):
            func = node.func
            called = getattr(func, "attr", getattr(func, "id", ""))
            if called in ("import_module", "__import__") and node.args:
                found = named(node.args[0])
                if found:
                    dynamic.add(found)
    return names, dynamic


def _package_of(name: str, path: Path) -> str:
    return name if path.name == "__init__.py" else name.rpartition(".")[0]


def _reexports(path: Path) -> Set[str]:
    """The modules named in a `_REEXPORTS = {...: ("module", "name")}`
    table (typhon_plugin_api.builtin: looked up by name at every use)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return set()
    out: Set[str] = set()
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.Assign) and node.targets:
            target = getattr(node.targets[0], "id", None)
        elif isinstance(node, ast.AnnAssign):
            target = getattr(node.target, "id", None)
        if target != "_REEXPORTS" or not isinstance(node.value, ast.Dict):
            continue
        for value in node.value.values:
            if isinstance(value, ast.Tuple) and value.elts and isinstance(
                    value.elts[0], ast.Constant):
                out.add(str(value.elts[0].value))
    return out


def _builtin_plugin_modules(root: Path) -> Set[str]:
    """Every module of the built-in Typhon plugins: the plugin host loads
    them by name (typhon_plugins.<folder>.<entry>)."""
    out = {"typhon_plugins"}
    base = root / "typhon_plugins"
    if not base.is_dir():
        return set()
    for folder in sorted(base.iterdir()):
        if not (folder / "plugin.json").is_file():
            continue
        for py in folder.rglob("*.py"):
            rel = py.relative_to(root).with_suffix("")
            parts = list(rel.parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            out.add(".".join(parts))
    return out


def app_sources(project_dir: Path) -> List[Path]:
    """The project's own Python files."""
    out = []
    for path in sorted(Path(project_dir).rglob("*.py")):
        if any(p in SKIP_NAMES for p in path.relative_to(project_dir).parts):
            continue
        out.append(path)
    return out


def council_modules(project_dir: Path,
                    root: Path = COUNCIL_ROOT) -> Dict[str, Path]:
    """Every Council module the app at `project_dir` uses, by name: what
    its files import, and what those import, to the end."""
    project_dir = Path(project_dir)
    todo: List[str] = []
    for path in app_sources(project_dir):
        names, dynamic = _imports(path, "")
        todo.extend(names | dynamic)
    found: Dict[str, Path] = {}
    while todo:
        name = todo.pop()
        parts = name.split(".")
        for i in range(1, len(parts) + 1):
            prefix = ".".join(parts[:i])
            if prefix in found:
                continue
            path = _module_file(prefix, root)
            if path is None:
                break
            found[prefix] = path
            names, dynamic = _imports(path, _package_of(prefix, path))
            todo.extend(names | dynamic)
            # A table of names looked up at use (typhon_plugin_api and its
            # builtin: _REEXPORTS) names modules too.
            todo.extend(_reexports(path))
            if prefix == "typhon_plugin_api.builtin":
                todo.extend(("frame_classes",
                             "council_qt.widgets.classifier_picker"))
            if prefix == "council_core.plugin_host":
                todo.extend(_builtin_plugin_modules(root))
    return dict(sorted(found.items()))


def data_files(modules: Dict[str, Path],
               root: Path = COUNCIL_ROOT) -> List[Path]:
    """The files the modules read beside themselves: each built-in plugin's
    manifest, and the app icon and splash (branding)."""
    out: List[Path] = []
    for name, path in modules.items():
        if name.startswith("typhon_plugins.") and path.name == "__init__.py":
            manifest = path.parent / "plugin.json"
            if manifest.is_file():
                out.append(manifest)
    if "branding" in modules:
        for asset in ("icon.ico", "icon.png", "icon.icns", "splash.png"):
            p = root / "assets" / asset
            if p.is_file():
                out.append(p)
    return out


def _module_name(path: Path, base: Path) -> str:
    rel = Path(path).relative_to(base).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def third_party(files: Iterable[Tuple[Path, Path]],
                local: Iterable[str]) -> Set[str]:
    """Top-level packages the files ((file, the folder its imports are
    relative to)) import that are neither the standard library, the
    Council's own nor the app's own."""
    local_top = {n.split(".")[0] for n in local}
    std = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}
    out: Set[str] = set()
    for path, base in files:
        name = _module_name(path, base)
        names, dynamic = _imports(Path(path), _package_of(name, Path(path)))
        for name in names | dynamic:
            top = name.split(".")[0]
            if top and top not in std and top not in local_top and \
                    _module_file(top) is None:
                out.add(top)
    return out


# ======================================================================
# The export
# ======================================================================
_PATH_BLOCK = re.compile(
    r"_APP_ROOT = Path\(.*?\n(?:.*\n)*?if str\(_APP_ROOT\) not in sys\.path:"
    r"\n    sys\.path\.insert\(0, str\(_APP_ROOT\)\)\n")

EXPORTED_PATH_BLOCK = (
    "# EXPORTED (council_core.app_export): the Council files this app uses\n"
    "# are in _council beside it; no Council is needed on this machine.\n"
    f"_APP_ROOT = Path(__file__).resolve().parent / {LIB_DIR!r}\n"
    "if str(_APP_ROOT) not in sys.path:\n"
    "    sys.path.insert(1, str(_APP_ROOT))\n")


def exported_main(text: str) -> str:
    """main.py with its path block pointing at _council (after the app's
    own folder, so the app's files come first). A standalone app's main.py
    has no block and is kept as it is."""
    if "_APP_ROOT" not in text:
        return text
    new, count = _PATH_BLOCK.subn(EXPORTED_PATH_BLOCK, text, count=1)
    if count != 1:
        raise ExportError("main.py's path block is not one the Designer "
                          "writes — Generate the project again, then export")
    return new


def _copy_project(src: Path, dest: Path) -> List[str]:
    copied = []
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if any(p in SKIP_NAMES for p in rel.parts) or \
                path.suffix in SKIP_SUFFIXES or not path.is_file():
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append(rel.as_posix())
    return copied


def _write(path: Path, text: str) -> None:
    """Atomic: written beside, then put in place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


_FIND_PYTHON = [
    "if defined PYTHON goto have_python",
    "where python >nul 2>nul && set \"PYTHON=python\" && goto have_python",
    "where py >nul 2>nul && set \"PYTHON=py\" && goto have_python",
    "echo No Python found. Install Python 3.11 or newer (tick \"Add python.exe",
    "echo to PATH\"), or set PYTHON to python.exe first, e.g.",
    "echo     set PYTHON=C:\\Python311\\python.exe",
    "echo See README.txt.",
    "pause",
    "exit /b 9009",
    ":have_python",
]


def _bat(name: str) -> str:
    return "\r\n".join([
        "@echo off",
        f"REM {name}: exported from the Council. Runs with its own data",
        "REM folder (data\\ beside this file): vault, settings, plugin home",
        "REM and classifiers. Set PYTHON to the Python to use (one with the",
        "REM packages in requirements.txt), else python on PATH, else the py",
        "REM launcher, is used.",
        "setlocal",
        "set \"HERE=%~dp0\"",
        "set \"COUNCIL_VAULT_ROOT=%HERE%data\\vault\"",
        "set \"COUNCIL_APP_DIR=%HERE%data\\app\"",
        "set \"TYPHON_PLUGIN_HOME=%HERE%data\\plugin_home\"",
        "set \"FRAME_CLASSES_STORE=%HERE%data\\classifiers\"",
        "set \"COUNCIL_SKIP_PATH_MIGRATION=1\"",
        *_FIND_PYTHON,
        "cd /d \"%HERE%\"",
        "\"%PYTHON%\" \"%HERE%main.py\" %*",
        "set \"CODE=%ERRORLEVEL%\"",
        "if not \"%CODE%\"==\"0\" (",
        "  echo.",
        f"  echo {name} stopped with an error ^(code %CODE%^). Run",
        "  echo check.bat to see what is missing; README.txt has the fixes.",
        "  pause",
        ")",
        "exit /b %CODE%", ""])


def _check_bat() -> str:
    return "\r\n".join([
        "@echo off",
        "REM Checks this machine can run the app: Python, packages, the",
        "REM Council files in _council, and which cameras are seen. Changes",
        "REM nothing (a throw-away data folder is used).",
        "setlocal",
        "set \"HERE=%~dp0\"",
        *_FIND_PYTHON,
        "cd /d \"%HERE%\"",
        f"\"%PYTHON%\" \"%HERE%{CHECK_SCRIPT}\" %*",
        "set \"CODE=%ERRORLEVEL%\"",
        "pause",
        "exit /b %CODE%", ""])


def _sh(name: str) -> str:
    return "\n".join([
        "#!/bin/sh",
        f"# {name}: exported from the Council. Runs with its own data folder",
        "# (data/ beside this file). Set PYTHON to the Python to use.",
        "HERE=\"$(cd \"$(dirname \"$0\")\" && pwd)\"",
        "export COUNCIL_VAULT_ROOT=\"$HERE/data/vault\"",
        "export COUNCIL_APP_DIR=\"$HERE/data/app\"",
        "export TYPHON_PLUGIN_HOME=\"$HERE/data/plugin_home\"",
        "export FRAME_CLASSES_STORE=\"$HERE/data/classifiers\"",
        "export COUNCIL_SKIP_PATH_MIGRATION=1",
        "cd \"$HERE\"",
        "exec \"${PYTHON:-python3}\" \"$HERE/main.py\" \"$@\"", ""])


def _check_sh() -> str:
    return "\n".join([
        "#!/bin/sh",
        "# Checks this machine can run the app (changes nothing).",
        "HERE=\"$(cd \"$(dirname \"$0\")\" && pwd)\"",
        "cd \"$HERE\"",
        f"exec \"${{PYTHON:-python3}}\" \"$HERE/{CHECK_SCRIPT}\" \"$@\"", ""])


def _requirements(required: Sequence[str], optional: Sequence[str]) -> str:
    lines = ["# pip install -r requirements.txt",
             "# (Python 3.11 or newer.)", "PySide6"]
    lines += [PIP_NAMES.get(n, n) for n in required]
    if optional:
        lines += ["", "# Optional — only for what each line says:"]
        for name in optional:
            why = OPTIONAL.get(name, "used by some features")
            lines.append(f"# {PIP_NAMES.get(name, name)}    — {why}")
    return "\n".join(lines) + "\n"


#: check_setup.py, written into every export: run on the other machine
#: (check.bat), it says what is missing there. Standard library and the
#: app's own files only; a throw-away data folder; nothing is changed.
SETUP_CHECK = r"""
'''Checks this machine can run the exported app (check.bat / check.sh).
Changes nothing: a throw-away data folder is used.'''
import atexit, importlib, json, os, shutil, sys, tempfile, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(HERE, "_council")
problems = []


def say(text=""):
    print(text, flush=True)


say(f"Python {sys.version.split()[0]} at {sys.executable}")
if sys.version_info < (3, 11):
    problems.append("Python is older than 3.11: install Python 3.11 or newer "
                    "and point run.bat at it (set PYTHON=...).")
try:
    with open(os.path.join(HERE, "export.json"), encoding="utf-8") as f:
        record = json.load(f)
except (OSError, ValueError):
    record = {}
pip = {"PIL": "Pillow", "sklearn": "scikit-learn", "cv2": "opencv-python",
       "yaml": "PyYAML", "skimage": "scikit-image"}
say("")
say("Packages:")
for name in ["PySide6"] + list(record.get("requires", [])):
    try:
        importlib.import_module(name)
        say(f"  {name}: ok")
    except Exception as exc:
        say(f"  {name}: MISSING ({type(exc).__name__}: {exc})")
        problems.append(f"{pip.get(name, name)} is not installed: "
                        f"python -m pip install -r requirements.txt")
for name in record.get("optional", []):
    try:
        importlib.import_module(name)
        say(f"  {name} (optional): ok")
    except Exception:
        say(f"  {name} (optional): not installed")
if problems:
    say("")
    for line in problems:
        say("PROBLEM: " + line)
    sys.exit(1)

scratch = tempfile.mkdtemp(prefix="app-check-")
atexit.register(shutil.rmtree, scratch, ignore_errors=True)
os.environ.update(QT_QPA_PLATFORM="offscreen", COUNCIL_NO_DIALOGS="1",
                  COUNCIL_SKIP_PATH_MIGRATION="1",
                  COUNCIL_VAULT_ROOT=os.path.join(scratch, "vault"),
                  COUNCIL_APP_DIR=os.path.join(scratch, "app"),
                  TYPHON_PLUGIN_HOME=os.path.join(scratch, "plugin_home"),
                  FRAME_CLASSES_STORE=os.path.join(scratch, "classifiers"))
sys.path[:0] = [HERE, LIB]
from PySide6.QtWidgets import QApplication
qapp = QApplication.instance() or QApplication([])
say("")
names = []
for base, dirs, files in os.walk(LIB):
    dirs[:] = [d for d in dirs if d != "__pycache__"]
    for f in files:
        if f.endswith(".py"):
            rel = os.path.relpath(os.path.join(base, f), LIB)[:-3]
            parts = [p for p in rel.split(os.sep) if p != "__init__"]
            if parts:
                names.append(".".join(parts))
bad = 0
for name in sorted(names):
    try:
        importlib.import_module(name)
    except Exception as exc:
        bad += 1
        say(f"  cannot load {name}: {type(exc).__name__}: {exc}")
        problems.append(f"{name} did not load (a package above, or a "
                        f"damaged copy: copy the folder again)")
say(f"Council files in _council: {len(names) - bad} of {len(names)} load")
try:
    import app
    window = app.App()
    qapp.processEvents()
    window.close()
    say("The app's window: builds")
except Exception as exc:
    problems.append(f"the app's window did not build: "
                    f"{type(exc).__name__}: {exc}")
    traceback.print_exc()
if "frame_camera" in sys.modules or os.path.isfile(
        os.path.join(LIB, "frame_camera.py")):
    say("")
    try:
        from council_core import cameras
        hint = cameras.BaslerBackend.install_hint
        if cameras.BaslerBackend().available():
            found = cameras.BaslerBackend().discover()
            say(f"Basler (pylon): ready, {len(found)} camera(s) seen")
            for info in found:
                say(f"  {info.label}")
            if not found:
                say("  None seen: is the camera plugged in and powered, and "
                    "does the pylon Viewer see it?")
        else:
            say(f"Basler (pylon): not available ({hint})")
        import frame_camera
        rows = frame_camera.list_cameras().get("rows", [])
        say(f"The app's camera list: {len(rows)}")
        for row in rows:
            say(f"  {row}")
    except Exception as exc:
        say(f"Camera check stopped: {type(exc).__name__}: {exc}")
say("")
if problems:
    for line in problems:
        say("PROBLEM: " + line)
    sys.exit(1)
say("All set: run run.bat (run.sh on Linux) to start the app.")
"""


def _readme(name: str, required: Sequence[str],
            optional: Sequence[str], commit: str) -> str:
    return f"""{name} — exported from the Council

RUN IT
  Windows:  double-click run.bat
  Linux:    sh run.sh
  First time on a new machine: do SET UP below, then run check.bat
  (sh check.sh on Linux) — it says what is missing, if anything.

SET UP A NEW MACHINE
  1. Python 3.11 or newer (python.org). On Windows tick "Add python.exe to
     PATH" in the installer. run.bat uses, in order: PYTHON if it is set,
     python on PATH, then the py launcher. To use one Python in
     particular, open a Command Prompt here and
         set PYTHON=C:\\Python311\\python.exe
         run.bat
  2. The packages, with the same Python:
         python -m pip install -r requirements.txt
     Required: PySide6{''.join(', ' + PIP_NAMES.get(n, n) for n in required)}.
     Optional (lines starting with # in requirements.txt; install one
     with pip install <name>): {', '.join(PIP_NAMES.get(n, n) for n in optional) or 'none'}.
  3. Cameras need their maker's software as well: Basler needs the pylon
     runtime (pylon Software Suite) and pip install pypylon; a CoaXPress
     camera also needs the suite's CXP support and the frame grabber's
     driver, and the grabber set (in its own software) to pass triggers.
  4. Run check.bat. It changes nothing.

NO INTERNET ON THAT MACHINE
  On a PC with internet and the same Windows and Python version (3.11 ->
  3.11), in this folder:
      python -m pip download -r requirements.txt -d wheels
      python -m pip download pypylon -d wheels      (Basler cameras)
  Copy the whole folder (wheels\\ included) across, then on the machine:
      python -m pip install --no-index --find-links wheels -r requirements.txt
      python -m pip install --no-index --find-links wheels pypylon
  The Python and pylon installers are copied across the same way.

IF SOMETHING GOES WRONG
  "No Python found"          Install Python (step 1) or set PYTHON.
  "No module named ..."      Step 2 was missed or used another Python: run
                             it with the Python run.bat uses (check.bat
                             prints which one).
  No camera in the list      check.bat says whether pylon is installed and
                             which cameras it sees. If pylon sees none, open
                             the pylon Viewer: if it does not see the camera
                             either, it is the cable, power or driver.
  The window closes at once  Start it from a Command Prompt (run.bat) to
                             read the error, or run check.bat.
  Moving to a new version    Export again from the Council into a new
                             folder. Copy data\\ from the old folder to keep
                             its settings, plugins and classifiers.

WHAT IS IN THIS FOLDER
  main.py, app.py, ui/ ...  the app as it was built in the Council
  _council/                 the Council files the app uses (nothing else
                            of the Council: no models, no Ledger)
  run.bat, run.sh           start the app
  check.bat, check.sh       check this machine can run it
  requirements.txt          the Python packages it uses
  data/                     made on first run: this copy's own vault,
                            settings, plugin home and classifiers

Copy the whole folder to another machine; nothing here points back at the
PC it was exported from. To change the app, change it in the Council and
export it again (into a new folder: an export never replaces one).

Exported from Council commit {commit or '(unknown)'}.
"""


def export_app(project_dir: Path, dest_parent: Path, *,
               name: Optional[str] = None,
               with_classifiers: bool = False) -> ExportResult:
    """Export the project at `project_dir` into a new folder
    `dest_parent/<name>` (default: the project's name). Refused, with
    nothing written, when that folder exists or the project is not a
    generated app."""
    src = Path(project_dir).resolve()
    if not (src / "main.py").is_file() or not (src / "app.py").is_file():
        raise ExportError(f"{src} is not a generated app (no main.py and "
                          f"app.py) — Generate it in the GUI Designer first")
    name = str(name or src.name).strip()
    if not name or any(ch in name for ch in '\\/:*?"<>|') or \
            name in (".", ".."):
        raise ExportError(f"{name!r} cannot be a folder name")
    dest_parent = Path(dest_parent).expanduser().resolve()
    dest = dest_parent / name
    if dest.exists():
        raise ExportError(f"{dest} already exists — an export never "
                          f"replaces or adds to a folder; choose another "
                          f"name or folder")
    if dest == src or src in dest.parents:
        raise ExportError("the export cannot go inside the project itself")
    if dest_parent == COUNCIL_ROOT or COUNCIL_ROOT in dest_parent.parents:
        raise ExportError("the export cannot go inside the Council's own "
                          "folder")
    main_text = exported_main((src / "main.py").read_text(encoding="utf-8"))
    modules = council_modules(src)
    extra = data_files(modules)
    manifest = {}
    try:
        manifest = json.loads((src / "manifest.json").read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {}

    dest_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{name}.export-",
                                    dir=str(dest_parent)))
    try:
        copied = _copy_project(src, staging)
        _write(staging / "main.py", main_text)
        lib = staging / LIB_DIR
        lib.mkdir()
        lib_files = []
        for path in list(modules.values()) + extra:
            rel = path.relative_to(COUNCIL_ROOT)
            target = lib / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            lib_files.append(rel.as_posix())
        # A package whose __init__ was not reached still needs one to import
        # its modules: every folder of copied .py files gets its __init__.
        for folder in {p.parent for p in lib.rglob("*.py")}:
            if folder != lib and not (folder / "__init__.py").exists():
                src_init = COUNCIL_ROOT / folder.relative_to(lib) / \
                    "__init__.py"
                if src_init.is_file():
                    shutil.copy2(src_init, folder / "__init__.py")
                    lib_files.append(
                        (folder.relative_to(lib) / "__init__.py").as_posix())
        from council_core import camera_record
        commit = camera_record.source_commit(COUNCIL_ROOT)
        _write(lib / COMMIT_FILE, commit + "\n")

        own = app_sources(src)
        local = list(modules) + [_module_name(p, src) for p in own]
        used = third_party([(p, COUNCIL_ROOT) for p in modules.values()]
                           + [(p, src) for p in own], local)
        used -= NEVER
        declared = [str(r) for r in manifest.get("requires", []) or []]
        declared += re.findall(r"^    import (\w+)  # noqa: F401$",
                               main_text, flags=re.M)
        required = sorted(set(declared) - NEVER - {""})
        optional = sorted(n for n in used if n not in required)
        _write(staging / "requirements.txt",
               _requirements(required, optional))
        _write(staging / "run.bat", _bat(name))
        _write(staging / "run.sh", _sh(name))
        _write(staging / "check.bat", _check_bat())
        _write(staging / "check.sh", _check_sh())
        _write(staging / CHECK_SCRIPT, SETUP_CHECK.lstrip("\n"))
        _write(staging / "README.txt",
               _readme(name, required, optional, commit))
        data = staging / DATA_DIR
        data.mkdir()
        if with_classifiers:
            import frame_classes
            store = Path(frame_classes.classifier_store())
            if store.is_dir():
                shutil.copytree(store, data / "classifiers",
                                ignore=shutil.ignore_patterns(
                                    "__pycache__", "*.tmp"))
        record = {
            "format": EXPORT_FORMAT, "version": 1, "app": name,
            "project": manifest.get("name") or src.name,
            "example": manifest.get("example", ""),
            "exported": time.strftime("%Y-%m-%d %H:%M:%S"),
            "from_project": str(src), "council_commit": commit,
            "council_modules": list(modules),
            "requires": required, "optional": optional,
            "with_classifiers": bool(with_classifiers)}
        _write(staging / EXPORT_FILE, json.dumps(record, indent=2) + "\n")
        os.replace(staging, dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return ExportResult(
        True, dest,
        f"Exported {name} to {dest}: the app and {len(modules)} Council "
        f"file(s) it uses (no Council needed there). Copy the folder to the "
        f"other machine and run run.bat.",
        modules=list(modules), files=sorted(copied + lib_files),
        required=required, optional=optional)


# ======================================================================
# The check: the exported folder runs with no Council in sight
# ======================================================================
_CHECK = r'''
import importlib, os, sys, runpy
folder = os.path.abspath(sys.argv[1])
council = os.path.abspath(sys.argv[2])
os.chdir(folder)
from PySide6.QtWidgets import QApplication
qapp = QApplication.instance() or QApplication([])
runpy.run_path(os.path.join(folder, "main.py"), run_name="council_export_check")
lib = os.path.join(folder, "_council")
names = []
for base, dirs, files in os.walk(lib):
    dirs[:] = [d for d in dirs if d != "__pycache__"]
    for f in files:
        if not f.endswith(".py"):
            continue
        rel = os.path.relpath(os.path.join(base, f), lib)[:-3].split(os.sep)
        if rel[-1] == "__init__":
            rel = rel[:-1]
        if rel:
            names.append(".".join(rel))
bad = []
for name in sorted(names):
    try:
        importlib.import_module(name)
    except Exception as exc:
        bad.append(f"{name}: {type(exc).__name__}: {exc}")
app_mod = sys.modules.get("app")
window = None
if app_mod is not None and hasattr(app_mod, "App"):
    window = app_mod.App()
    qapp.processEvents()
fc = sys.modules.get("frame_camera")
if fc is not None:
    rows = fc.list_cameras().get("rows", [])
    print("CAMERAS", len(rows))
if window is not None:
    window.close()
    qapp.processEvents()
outside = sorted({os.path.abspath(getattr(m, "__file__", "") or "")
                  for m in list(sys.modules.values())
                  if (getattr(m, "__file__", "") or "").startswith(council)})
for line in bad:
    print("MISSING", line)
for path in outside:
    print("OUTSIDE", path)
print("WINDOW", "built" if window is not None else "none")
sys.exit(1 if bad or outside else 0)
'''


def check(folder: Path, python: Optional[str] = None,
          timeout_s: float = 180.0) -> CheckResult:
    """Run the exported app at `folder` in a separate Python that cannot
    see the Council (`-I`, started in the folder, a throw-away data folder,
    offscreen): every file under _council imported, the window built and
    closed. ok only when nothing was missing and nothing came from the
    Council's own folder."""
    folder = Path(folder).resolve()
    with tempfile.TemporaryDirectory(prefix="export-check-") as scratch:
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("COUNCIL_", "TYPHON_", "FRAME_CLASSES",
                                    "PYTHON"))}
        env.update(QT_QPA_PLATFORM="offscreen", COUNCIL_NO_DIALOGS="1",
                   COUNCIL_VAULT_ROOT=str(Path(scratch) / "vault"),
                   COUNCIL_APP_DIR=str(Path(scratch) / "app"),
                   TYPHON_PLUGIN_HOME=str(Path(scratch) / "plugin_home"),
                   FRAME_CLASSES_STORE=str(Path(scratch) / "classifiers"),
                   COUNCIL_SKIP_PATH_MIGRATION="1")
        script = Path(scratch) / "export_check.py"
        script.write_text(_CHECK, encoding="utf-8")
        try:
            done = subprocess.run(
                [python or sys.executable, "-I", "-B", str(script),
                 str(folder),
                 str(COUNCIL_ROOT)], cwd=str(folder), env=env,
                capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return CheckResult(False, f"the exported app did not finish "
                                      f"starting within {timeout_s:g} s")
    out = (done.stdout or "") + (done.stderr or "")
    missing = [l[len("MISSING "):] for l in out.splitlines()
               if l.startswith("MISSING ")]
    outside = [l[len("OUTSIDE "):] for l in out.splitlines()
               if l.startswith("OUTSIDE ")]
    if done.returncode == 0:
        return CheckResult(True, "Checked: the exported app starts with no "
                                 "Council present.", out)
    if missing or outside:
        words = []
        if missing:
            words.append("missing: " + "; ".join(missing[:5]))
        if outside:
            words.append("read from the Council: " + "; ".join(outside[:5]))
        return CheckResult(False, "The exported app does not stand alone — "
                                  + " — ".join(words), out)
    tail = "\n".join(out.strip().splitlines()[-8:])
    return CheckResult(False, f"The exported app did not start "
                              f"(exit {done.returncode}):\n{tail}", out)
