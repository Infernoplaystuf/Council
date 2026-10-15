"""
Policy and preview-runner tests.

The policy test that matters most is the bound-method one: `fn = os.system`
followed by `fn(x)` is the same capability as calling it directly, and a
validator that only walks ast.Call nodes sees nothing. That exact hole was
demonstrated against this repo's analyst sandbox and used to delete a real file,
which is why it is asserted here rather than assumed.

Run:  python -m pytest tests/test_gui_policy.py -q
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui_policy as pol     # noqa: E402
import gui_runner as run     # noqa: E402


def ok(code, mode="linked"):
    good, errs = pol.validate(code, mode)
    return good, errs


# ============================================================
# What a real app must be allowed to do
# ============================================================

def test_a_realistic_generated_app_passes():
    good, errs = ok('''
import os
import sys
import tkinter as tk
from tkinter import ttk, filedialog
from pathlib import Path

from ui.main_ui import MainUi


class App(MainUi):
    def on_btn_open(self):
        path = filedialog.askopenfilename()
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                self.log_output.append(fh.read())

    def on_btn_save(self):
        out = Path(sys.argv[0]).parent / "result.csv"
        out.write_text("a,b\\n1,2\\n", encoding="utf-8")
''')
    assert good, errs


def test_os_and_sys_are_permitted():
    """A real application needs os.path and sys.argv. Denying the modules
    outright would reject every generated app."""
    assert ok("import os, sys\np = os.path.join('a', 'b')\nn = sys.argv")[0]


def test_linked_mode_allows_the_app_module_allowlist():
    for m in sorted(pol.LINKED_MODULES):
        good, errs = ok(f"import {m}", "linked")
        assert good, (m, errs)


def test_third_party_and_project_modules_are_allowed():
    assert ok("import pandas, numpy, matplotlib\nfrom PIL import Image")[0]
    assert ok("from ui.widgets import ImageCanvas")[0]
    assert ok("from .widgets import Scrubber")[0], "relative imports are internal"


# ============================================================
# What it must refuse
# ============================================================

@pytest.mark.parametrize("mod", ["subprocess", "socket", "requests", "urllib",
                                 "ctypes", "pickle", "marshal", "importlib"])
def test_escape_modules_are_denied_in_both_modes(mod):
    for mode in ("linked", "standalone"):
        good, errs = ok(f"import {mod}", mode)
        assert not good and any(mod in e for e in errs), (mod, mode)


@pytest.mark.parametrize("expr", ["eval('1')", "exec('x=1')",
                                  "compile('1', '<s>', 'eval')"])
def test_data_to_code_builtins_are_denied(expr):
    assert not ok(expr)[0]


def test_council_engine_is_refused_with_the_reason():
    good, errs = ok("import council_engine", "linked")
    assert not good
    assert any("second GGUF singleton" in e for e in errs), (
        "the error must say WHY, or someone will just add it to the allowlist")


def test_shell_escapes_are_denied_even_through_permitted_os():
    assert not ok("import os\nos.system('dir')")[0]
    assert not ok("import os\nos.popen('dir')")[0]


def test_destructive_calls_are_denied():
    assert not ok("import shutil\nshutil.rmtree('/x')")[0]
    assert not ok("import os\nos.remove('x')")[0]
    assert not ok("from pathlib import Path\nPath('x').unlink()")[0]


def test_a_bound_method_is_caught_even_though_it_is_never_called_here():
    """THE test. `fn = os.system` is the capability; the call may be anywhere.
    A validator that only inspects ast.Call nodes misses it entirely — that is
    how a bound method escaped this repo's analyst sandbox and deleted a file."""
    good, errs = ok("import os\nfn = os.system\nfn('dir')")
    assert not good and any("system" in e for e in errs)

    good, errs = ok("import shutil\nnuke = shutil.rmtree")
    assert not good, "never called, but the reference is the capability"


def test_getattr_string_indirection_is_caught():
    good, errs = ok("import os\ngetattr(os, 'system')('dir')")
    assert not good and any("getattr" in e for e in errs)


def test_dunder_introspection_escapes_are_denied():
    assert not ok("x = ().__class__.__bases__")[0]
    assert not ok("f = (lambda: 0).__globals__")[0]


# ============================================================
# Modes differ
# ============================================================

def test_standalone_refuses_app_modules_and_says_why():
    good, errs = pol.validate("import vault_analyst", "standalone")
    assert not good
    assert any("not standalone" in e or "linked mode" in e for e in errs), errs


def test_an_unknown_third_party_module_is_refused():
    assert not ok("import tensorflow")[0]


def test_an_unknown_mode_is_refused():
    good, errs = pol.validate("x = 1", "webassembly")
    assert not good and "unknown import mode" in errs[0]


def test_unparseable_source_reports_the_line():
    good, errs = ok("def broken(:\n    pass")
    assert not good and "line" in errs[0]


def test_every_fault_is_reported_not_just_the_first():
    good, errs = ok("import socket\nimport pickle\neval('1')")
    assert not good and len(errs) >= 3, errs


def test_validate_project_prefixes_filenames(tmp_path):
    (tmp_path / "a.py").write_text("import socket\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("x = 1\n", encoding="utf-8")
    good, errs = pol.validate_project([tmp_path / "a.py", tmp_path / "b.py"])
    assert not good and errs[0].startswith("a.py:")


def test_the_emitted_project_passes_its_own_policy(tmp_path):
    """The generator must not produce code its own policy rejects."""
    import gui_emit as ge
    import gui_layout as gl
    import gui_spec as gsp
    from gui_shapes import Shape
    shapes = [
        Shape(id="tb", kind="toolbar", x=0, y=0, w=1000, h=40, label="Tools"),
        Shape(id="img", kind="image_canvas", x=0, y=40, w=700, h=560,
              label="View"),
        Shape(id="log", kind="log_pane", x=0, y=640, w=1000, h=160, label="Log"),
        Shape(id="fp", kind="file_picker", x=700, y=40, w=300, h=30,
              label="Input"),
    ]
    spec = gsp.build(shapes, gl.infer(shapes, 1000, 800), project="p")
    ge.emit(spec, tmp_path)
    files = sorted((tmp_path / "ui").glob("*.py")) + [
        tmp_path / "app.py", tmp_path / "handlers.py", tmp_path / "main.py"]
    good, errs = pol.validate_project(files, "linked")
    assert good, errs


# ============================================================
# Preview runner
# ============================================================

def _project(tmp_path, body: str, entry: str = "main.py") -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    (p / entry).write_text(body, encoding="utf-8")
    return p


def test_preview_streams_output_and_exits_cleanly(tmp_path):
    p = _project(tmp_path, "print('hello from the preview')\n")
    lines = []
    done = []
    pv = run.start(p, on_line=lambda t, lv: lines.append((lv, t)),
                   on_exit=done.append)
    for _ in range(100):
        if done:
            break
        time.sleep(0.05)
    assert done == [0], "clean exit"
    assert any("hello from the preview" in t for _lv, t in lines)
    assert not run.is_running(p), "a finished preview deregisters itself"


def test_a_crash_points_at_the_offending_line(tmp_path):
    p = _project(tmp_path, "raise ValueError('boom')\n")
    lines = []
    done = []
    run.start(p, on_line=lambda t, lv: lines.append((lv, t)),
              on_exit=done.append)
    for _ in range(100):
        if done:
            break
        time.sleep(0.05)
    assert done and done[0] != 0
    text = "\n".join(t for _lv, t in lines)
    assert "exited with code" in text
    assert "main.py, line 1" in text, (
        f"the failing line must be named, not left in a stack: {text}")


def test_a_project_generated_before_the_rename_still_runs(tmp_path):
    """Projects made before main.py existed have only launch.py, and are not
    regenerated until the user opens them. Run them anyway."""
    p = _project(tmp_path, "print('from the old launcher')\n", entry="launch.py")
    lines, done = [], []
    run.start(p, on_line=lambda t, lv: lines.append((lv, t)), on_exit=done.append)
    for _ in range(100):
        if done:
            break
        time.sleep(0.05)
    assert done == [0]
    assert any("from the old launcher" in t for _lv, t in lines)


def test_a_project_with_neither_entry_point_says_so(tmp_path):
    p = tmp_path / "empty"
    p.mkdir()
    with pytest.raises(FileNotFoundError) as exc:
        run.start(p)
    assert "main.py" in str(exc.value)


def test_explain_failure_names_the_last_generated_frame(tmp_path):
    tb = (
        'Traceback (most recent call last):\n'
        f'  File "{tmp_path}/launch.py", line 3, in <module>\n'
        '  File "/usr/lib/python3/tkinter/__init__.py", line 999, in call\n'
        f'  File "{tmp_path}/ui/main_ui.py", line 42, in _build\n'
        "AttributeError: 'NoneType' object has no attribute 'grid'\n")
    msg = run.explain_failure(tb, tmp_path)
    assert "main_ui.py, line 42" in msg, msg
    assert "AttributeError" in msg
    assert "tkinter" not in msg, "Tk's own frames are noise, not the answer"


def test_a_second_run_stops_the_first(tmp_path):
    p = _project(tmp_path, "import time\ntime.sleep(30)\n")
    first = run.start(p)
    assert first.running
    second = run.start(p)
    for _ in range(60):
        if not first.running:
            break
        time.sleep(0.05)
    assert not first.running, "one preview per project"
    assert second.running and second.pid != first.pid
    run.stop(p)


def test_stop_and_stop_all(tmp_path):
    p = _project(tmp_path, "import time\ntime.sleep(30)\n")
    run.start(p)
    assert run.is_running(p)
    assert run.stop(p) is True
    assert not run.is_running(p)
    assert run.stop(p) is False, "stopping nothing is not an error"

    run.start(p)
    assert run.stop_all() >= 1
    assert not run.is_running(p)


def test_missing_launcher_is_a_clear_error(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError) as exc:
        run.start(empty)
    assert "generate the project first" in str(exc.value)


def test_runner_has_no_timeout():
    """A GUI runs until the user closes it. A timeout would kill a working
    preview mid-use — the exact LocalRunner failure this module exists to
    avoid."""
    import inspect
    src = inspect.getsource(run.start)
    assert "timeout" not in src.split("def start")[1].split("Popen")[1][:400]


def test_atexit_backstop_is_registered():
    import inspect
    src = inspect.getsource(run)
    assert "atexit.register(stop_all)" in src, (
        "without it, a crash in the designer leaves an orphaned preview window")


# ============================================================
# Every SPELLED way to reach a denied name (adversarial review, 2026-09)
# ============================================================
#
# The gate checked `os.system(...)` as an attribute and nothing else, so each
# of these passed it — and the gate is enforced at Run. `from os import
# system` binds the denied attribute to a plain name, which is never checked
# again; `from os import *` does the same for every name at once.

@pytest.mark.parametrize("code", [
    "from os import system\nsystem('x')",
    "from os import system as s\ns('x')",
    "from shutil import rmtree\nrmtree('x')",
    "from os import remove, unlink, execv",
    "from nt import system",
    "from os import *\nsystem('x')",
    "from shutil import *",
    "import builtins\nbuiltins.exec('1')",
    "__builtins__.exec('1')",
    "import os\nos.__dict__['system']('x')",
    "import os\nvars(os)['system']('x')",
    "globals()['__builtins__']['eval']('1')",
    "from . import system",
])
def test_a_denied_name_is_refused_however_it_is_spelled(code):
    good, errs = ok(code)
    assert not good, f"passed the gate: {code!r}"


@pytest.mark.parametrize("code", [
    "import re\nre.compile('a')",
    "from json import loads\nloads('1')",
    "from os import path\npath.join('a', 'b')",
    "from tkinter import *\nTk",
    "from PIL import Image\nImage.open",
])
def test_the_honest_spellings_still_pass(code):
    good, errs = ok(code)
    assert good, errs


# ============================================================
# The gate reads every file the app can import
# ============================================================

def _proj(tmp_path):
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "main.py").write_text("from app import main\n",
                                      encoding="utf-8")
    (tmp_path / "app.py").write_text("def main():\n    pass\n",
                                     encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("where", ["widgets.py", "helpers.py",
                                   "ui/sub/evil.py"])
def test_a_helper_module_is_gated_too(tmp_path, where):
    """MEASURED: a root-level widgets.py (its name is on PROJECT_MODULES)
    running subprocess got 'policy: OK', and importing the app ran it."""
    pdir = _proj(tmp_path)
    (pdir / where).parent.mkdir(parents=True, exist_ok=True)
    (pdir / where).write_text("import subprocess\nsubprocess.run(['x'])\n",
                              encoding="utf-8")
    good, errs = pol.validate_dir(pdir)
    assert not good and any("subprocess" in e for e in errs), errs


def test_the_projects_own_helper_module_may_be_imported(tmp_path):
    """The gate used to say 'add it to requires', and the probe then said it
    was missing — a project with its own helper could never Run."""
    pdir = _proj(tmp_path)
    (pdir / "helpers.py").write_text("def scale(x):\n    return x * 2\n",
                                     encoding="utf-8")
    (pdir / "app.py").write_text("import helpers\ndef main():\n    pass\n",
                                 encoding="utf-8")
    good, errs = pol.validate_dir(pdir)
    assert good, errs
    assert "helpers" in pol.project_modules(pdir)


def test_a_local_file_cannot_readmit_a_denied_module(tmp_path):
    pdir = _proj(tmp_path)
    (pdir / "app.py").write_text("import subprocess\nimport council_engine\n",
                                 encoding="utf-8")
    (pdir / "subprocess.py").write_text("", encoding="utf-8")
    (pdir / "council_engine.py").write_text("", encoding="utf-8")
    good, errs = pol.validate_dir(pdir)
    assert not good
    assert any("'subprocess'" in e for e in errs)
    assert any("council_engine" in e for e in errs)


def test_generated_output_folders_are_not_read_as_code(tmp_path):
    pdir = _proj(tmp_path)
    (pdir / "__pycache__").mkdir()
    (pdir / "__pycache__" / "x.py").write_text("import subprocess\n",
                                               encoding="utf-8")
    (pdir / ".backups").mkdir()
    (pdir / ".backups" / "app.py").write_text("import subprocess\n",
                                              encoding="utf-8")
    good, errs = pol.validate_dir(pdir)
    assert good, errs


# ============================================================
# What the Typhon plugin research measured passing (2026-10-15)
# ============================================================
#
# Each of these passed the gate, and the ones that can run safely were run
# from a gated temp project before the fix: asyncio started a child Python
# that wrote a canary file, asyncio and socketserver+xmlrpc served and
# answered on 127.0.0.1, a ProcessPoolExecutor worker wrote a canary, and
# os._exit(7) ended the process without running its `finally`. os.startfile
# and webbrowser were judged statically only — running them opens windows.
# The allowlist is sys.stdlib_module_names, so every stdlib module nobody
# had named was admitted.

def _refused(code, reason, mode="linked"):
    """The gate refuses ``code`` in both toolkits, and says why."""
    for toolkit in ("qt", "tk"):
        good, errs = pol.validate(code, mode, (), toolkit)
        assert not good, f"passed the gate ({toolkit}): {code!r}"
        assert any(reason in e for e in errs), (code, reason, errs)


@pytest.mark.parametrize("code,reason", [
    ("import os\nos._exit(0)", pol.WHY_EXIT),
    ("from os import _exit\n_exit(0)", pol.WHY_EXIT),
    ("import os\nbye = os._exit", pol.WHY_EXIT),
    ("import asyncio", pol.WHY_ASYNCIO),
    ("import asyncio.subprocess", pol.WHY_ASYNCIO),
    ("from asyncio import create_subprocess_exec", pol.WHY_ASYNCIO),
    ("from asyncio.subprocess import PIPE", pol.WHY_ASYNCIO),
    # a loop handed over by anything, no asyncio import in sight
    ("def go(loop):\n    return loop.subprocess_exec(None, 'x')",
     pol.WHY_PROGRAM),
    ("def go(loop):\n    return loop.create_connection(None, 'h', 1)",
     pol.WHY_NETWORK),
    ("import socketserver\nsocketserver.TCPServer(('', 0), None)",
     pol.WHY_NETWORK),
    ("import xmlrpc.client", pol.WHY_NETWORK),
    ("from xmlrpc.server import SimpleXMLRPCServer", pol.WHY_NETWORK),
    ("import concurrent.futures as cf\ncf.ProcessPoolExecutor()",
     pol.WHY_PROGRAM),
    ("from concurrent.futures import ProcessPoolExecutor", pol.WHY_PROGRAM),
    ("import concurrent.futures.process", pol.WHY_PROGRAM),
    ("from concurrent.futures import process", pol.WHY_PROGRAM),
    ("from concurrent.futures.process import _process_worker",
     pol.WHY_PROGRAM),
    ("import os\nos.startfile('x')", pol.WHY_DESKTOP),
    ("from os import startfile", pol.WHY_DESKTOP),
    ("import webbrowser\nwebbrowser.open('http://x')", pol.WHY_DESKTOP),
    ("from webbrowser import open as browse", pol.WHY_DESKTOP),
])
def test_what_the_plugin_research_measured_is_refused_with_a_reason(
        code, reason):
    _refused(code, reason)


# ---- their relatives: what else the same gaps let through ----

@pytest.mark.parametrize("name", [
    "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve",
    "spawnvp", "spawnvpe", "execl", "execle", "execlp", "execlpe", "execv",
    "execve", "execvp", "execvpe", "posix_spawn", "posix_spawnp"])
def test_every_spelling_of_os_spawn_and_exec_is_refused(name):
    """spawnl/spawnv/execl/execv/execve were denied; their siblings
    spawnvp, execvpe, posix_spawn ... passed — the same call, one letter
    over."""
    _refused(f"import os\nos.{name}('x')", pol.WHY_PROGRAM)
    _refused(f"from os import {name}", pol.WHY_PROGRAM)


@pytest.mark.parametrize("code,reason", [
    # processes
    ("import _multiprocessing", pol.WHY_PROGRAM),
    ("from multiprocessing.dummy import Pool", pol.WHY_PROGRAM),
    ("import _winapi\n_winapi.CreateProcess", pol.WHY_PROGRAM),
    ("import _posixsubprocess", pol.WHY_PROGRAM),
    ("import pipes", pol.WHY_PROGRAM),
    ("import distutils.spawn", pol.WHY_PROGRAM),
    ("import ensurepip", pol.WHY_PROGRAM),
    ("import venv", pol.WHY_PROGRAM),
    ("import idlelib.rpc", pol.WHY_PROGRAM),
    ("import logging.handlers as h\nh.subprocess", pol.WHY_PROGRAM),
    # ending the app at once
    ("import os\nos.abort()", pol.WHY_EXIT),
    ("import os as o\no.abort()", pol.WHY_EXIT),
    ("import shutil\nshutil.os.abort()", pol.WHY_EXIT),
    ("from os import abort", pol.WHY_EXIT),
    ("import signal\nsignal.raise_signal(signal.SIGTERM)", pol.WHY_EXIT),
    ("import signal\nsignal.alarm(1)", pol.WHY_EXIT),
    ("import faulthandler\nfaulthandler._sigsegv()", pol.WHY_EXIT),
    # the network, by module, by private module, by attribute route
    ("import poplib", pol.WHY_NETWORK),
    ("import imaplib", pol.WHY_NETWORK),
    ("import nntplib", pol.WHY_NETWORK),
    ("import smtpd", pol.WHY_NETWORK),
    ("import asyncore", pol.WHY_NETWORK),
    ("import asynchat", pol.WHY_NETWORK),
    ("from wsgiref.simple_server import make_server", pol.WHY_NETWORK),
    ("import ssl", pol.WHY_NETWORK),
    ("import _ssl", pol.WHY_NETWORK),
    ("import _socket", pol.WHY_NETWORK),
    ("import _overlapped", pol.WHY_NETWORK),
    ("import _asyncio", pol.WHY_ASYNCIO),
    ("import logging.handlers\nlogging.handlers.socket.create_connection",
     pol.WHY_NETWORK),
    ("import logging.handlers\nlogging.handlers.SocketHandler('h', 1)",
     pol.WHY_NETWORK),
    ("import logging.config\nlogging.config.listen(9030)", pol.WHY_NETWORK),
    ("from logging import config\nconfig.listen(9030)", pol.WHY_NETWORK),
    ("import frame_camera\nframe_camera.socket.socket()", pol.WHY_NETWORK),
    # the desktop and the registry
    ("import antigravity", pol.WHY_DESKTOP),
    ("import winreg", pol.WHY_REGISTRY),
    # ctypes by another door
    ("import _ctypes", pol.WHY_NATIVE),
    ("import numpy as np\nnp.ctypeslib.load_library('x', '.')",
     pol.WHY_NATIVE),
    ("import numpy as np\nnp.ctypeslib.ctypes.windll.kernel32",
     pol.WHY_NATIVE),
    ("from numpy import ctypeslib", pol.WHY_NATIVE),
    ("import frame_camera\nframe_camera.ctypes.CDLL('x')", pol.WHY_NATIVE),
    ("import os\nos.add_dll_directory('x')", pol.WHY_NATIVE),
    # an import by a name built at runtime, or text run as code
    ("import sys\nsys.modules['subprocess'].Popen(['x'])",
     pol.WHY_MODULE_TABLE),
    ("import sys\nsys.modules.get('builtins')", pol.WHY_MODULE_TABLE),
    ("from sys import modules", pol.WHY_MODULE_TABLE),
    ("import os\nos.sys.modules", pol.WHY_MODULE_TABLE),
    ("import sys\nsys.meta_path.insert(0, None)", pol.WHY_MODULE_TABLE),
    ("import sys\nsys.path_hooks.append(None)", pol.WHY_MODULE_TABLE),
    ("import gc\ngc.get_objects()", pol.WHY_ALL_OBJECTS),
    ("import sys\nsys._getframe().f_builtins['eval']", pol.WHY_INTERPRETER),
    ("import pkgutil\npkgutil.resolve_name('subprocess:Popen')",
     pol.WHY_CODE),
    ("import pydoc\npydoc.locate('subprocess.Popen')", pol.WHY_CODE),
    ("import zipimport", pol.WHY_CODE),
    ("import site\nsite.addsitedir('x')", pol.WHY_CODE),
    ("import _imp", pol.WHY_CODE),
    ("import _frozen_importlib", pol.WHY_CODE),
    ("import frame_camera\nframe_camera.importlib", pol.WHY_CODE),
    ("import timeit\ntimeit.timeit('1')", pol.WHY_CODE),
    ("from timeit import Timer", pol.WHY_CODE),
    ("import cProfile\ncProfile.run('1')", pol.WHY_CODE),
    ("import trace", pol.WHY_CODE),
    ("import pdb", pol.WHY_CODE),
    ("import doctest", pol.WHY_CODE),
    ("import os, operator\noperator.attrgetter('system')(os)",
     pol.WHY_PROGRAM),
    ("import os\nfrom operator import attrgetter\n"
     "attrgetter('path.system')(os)", pol.WHY_PROGRAM),
    ("import os, operator\noperator.methodcaller('startfile', 'x')(os)",
     pol.WHY_DESKTOP),
    ("import os, inspect\ninspect.getattr_static(os, '_exit')",
     pol.WHY_EXIT),
    ("import os\ngetattr(os, 'abort')()", pol.WHY_EXIT),
])
def test_the_relatives_of_those_escapes_are_refused_too(code, reason):
    _refused(code, reason)


@pytest.mark.parametrize("mod", sorted(pol.DENIED_MODULE_REASONS))
def test_every_denied_module_is_refused_in_both_modes_with_its_reason(mod):
    """Every name on the list, not a sample: a module added to it is tested
    the moment it is added. Static, so whether it is installed is moot."""
    _refused(f"import {mod}", pol.DENIED_MODULE_REASONS[mod])
    _refused(f"import {mod}", pol.DENIED_MODULE_REASONS[mod], "standalone")
    _refused(f"from {mod} import x", pol.DENIED_MODULE_REASONS[mod])


@pytest.mark.parametrize("name,reason", [
    ("asyncio", pol.WHY_ASYNCIO), ("socketserver", pol.WHY_NETWORK),
    ("xmlrpc.client", pol.WHY_NETWORK), ("webbrowser", pol.WHY_DESKTOP),
    ("concurrent.futures.process", pol.WHY_PROGRAM),
    ("_winapi", pol.WHY_PROGRAM), ("ssl", pol.WHY_NETWORK)])
def test_a_denied_module_cannot_be_declared_into_the_allowlist(name, reason):
    errs = pol.check_requires([name])
    assert errs and "never permitted" in errs[0] and reason in errs[0], errs


def test_a_helper_module_using_asyncio_fails_the_project(tmp_path):
    pdir = _proj(tmp_path)
    (pdir / "helpers.py").write_text(
        "import asyncio\n"
        "async def go():\n"
        "    await asyncio.create_subprocess_shell('x')\n",
        encoding="utf-8")
    good, errs = pol.validate_dir(pdir)
    assert not good
    assert any(e.startswith("helpers.py:") and "asyncio" in e for e in errs)


@pytest.mark.parametrize("code", [
    # the same words, on the things they are ordinary on
    "import gc\ngc.collect()",
    "import faulthandler\nfaulthandler.enable()",
    "import threading\nthreading.Timer(1.0, print).start()",
    "from timeit import default_timer\nt0 = default_timer()",
    "import timeit\nt0 = timeit.default_timer()",
    "class Job:\n    def abort(self):\n        pass\nJob().abort()",
    "import signal\nsignal.signal(signal.SIGINT, signal.SIG_DFL)",
    "import logging.config\nlogging.config.dictConfig({'version': 1})",
    "import logging.handlers\nlogging.handlers.RotatingFileHandler('a.log')",
    "from concurrent.futures import ThreadPoolExecutor",
    "from concurrent import futures\nfutures.ThreadPoolExecutor()",
    "import operator\nkey = operator.attrgetter('name')",
    "x = getattr(object(), 'value', None)",
    "import sys\nsys.path.insert(0, 'x')\nsys.exit(0)",
    "import os\nos.path.join('a', 'b')\nos.environ.get('X')",
    "from PySide6.QtWidgets import QApplication\n"
    "app = QApplication([])\napp.exec()",
])
def test_the_ordinary_uses_of_those_modules_still_pass(code):
    """A gate that refuses honest code teaches people to ignore it: these
    are the near-misses of each new rule."""
    for toolkit in ("qt", "tk"):
        if "PySide6" in code and toolkit == "tk":
            continue
        good, errs = pol.validate(code, "linked", (), toolkit)
        assert good, (code, errs)


def test_a_label_that_is_now_a_denied_name_gets_a_port_the_gate_passes():
    """gui_ports derives port names from labels and steers clear of every
    name the gate denies (a "Remove" button's port is remove_button). The
    new names join that list, so a "Socket" or "Modules" button drawn today
    gets a port the gate accepts instead of an app that cannot Run."""
    import gui_ports
    for label in ("Socket", "Modules", "Startfile", "Asyncio"):
        name = gui_ports.default_port_name("button", label)
        assert name == f"{label.lower()}_button", (label, name)
        good, errs = pol.validate(f"class Ports:\n    def __init__(self):\n"
                                  f"        self.{name} = None\n")
        assert good, (label, name, errs)


# ============================================================
# Every shipped example still passes
# ============================================================

def _example_params():
    import gui_examples
    return [(name, toolkit) for name in gui_examples.names()
            for toolkit in ("qt", "tk")]


@pytest.mark.parametrize("example,toolkit", _example_params())
def test_every_shipped_example_still_passes_the_gate(example, toolkit,
                                                     tmp_path):
    """Typhon, the Barbie capture forms and the image viewer, each built the
    way "New from example" builds it (into a temp vault) for both targets,
    and gated the way Run gates it. The Tk target only writes text here — no
    window is made. (The qt_tests wireframes are gated by
    test_qt_wireframes.)"""
    import gui_projects
    from council_core import designer_examples as dx
    vault = tmp_path / "vault"
    vault.mkdir()
    built = dx.build_project(example, f"ex_{example}_{toolkit}", vault,
                             toolkit=toolkit)
    project = gui_projects.open_project(f"ex_{example}_{toolkit}",
                                        vault_dir=vault)
    mode = gui_projects.load_manifest(built.project_dir).mode
    good, errs = pol.validate_dir(built.project_dir, mode,
                                  list(project.requires or []),
                                  toolkit=toolkit)
    assert good, errs
