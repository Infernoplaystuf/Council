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
        tmp_path / "app.py", tmp_path / "handlers.py", tmp_path / "launch.py"]
    good, errs = pol.validate_project(files, "linked")
    assert good, errs


# ============================================================
# Preview runner
# ============================================================

def _project(tmp_path, body: str) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    (p / "launch.py").write_text(body, encoding="utf-8")
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
    assert "launch.py, line 1" in text, (
        f"the failing line must be named, not left in a stack: {text}")


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
