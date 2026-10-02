"""
gui_codebehind — the gates and the loop, against scripted stub models.

No model, no project, no subprocess: the smoke run is injected too, so this
file proves the CONTRACT — every gate, the deterministic fixes, the repair
prompt's content, best-of-N stopping at the first pass, and that write()
never raises. tests/test_gui_smoke.py proves the sandbox; tests/
test_designer_codebehind.py proves the whole thing on a real project.
"""
from __future__ import annotations

import ast
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gui_codebehind as gcb  # noqa: E402
from council_core import designer_wiring as dw  # noqa: E402


def fn_target(**kw) -> gcb.Target:
    base = dict(
        mode="function", name="count_images",
        instruction="count the PNG files in the folder and list them",
        label="Count images", kind="button",
        params=[gcb.Param("folder", "str", "folder", "file_picker",
                          'a folder path the user picked in "Folder"',
                          "<FOLDER>")],
        outputs=[gcb.Output("status", "status", "label", "text",
                            'shown as text in "Status"'),
                 gcb.Output("files", "files", "listbox", "list",
                            'a list of strings, shown in "Files"')],
        project_mode="linked", toolkit="qt", local_modules=["logic"])
    base.update(kw)
    return gcb.Target(**base)


PORT_ROWS = [
    gcb.PortRow("folder", "file_picker", "path", "var", "", "Folder"),
    gcb.PortRow("status", "label", "str", "var", "", "Status"),
    gcb.PortRow("files", "listbox", "str", "list", "", "Files"),
    gcb.PortRow("plot", "chart_panel", "figure", "proxy",
                "figure_for_drawing", "Plot"),
    gcb.PortRow("go", "button", "event", "event", "", "Go"),
]


def h_target(**kw) -> gcb.Target:
    base = dict(mode="handler", name="on_btn_go",
                instruction="show how many files the folder holds",
                label="Go", kind="button", ports=list(PORT_ROWS),
                handlers=["on_btn_go", "on_close"], project_mode="linked",
                toolkit="qt")
    base.update(kw)
    return gcb.Target(**base)


GOOD = '''```python
def count_images(folder: str) -> dict:
    """Count PNGs."""
    import os
    names = sorted(n for n in os.listdir(folder) if n.endswith(".png"))
    return {"status": f"{len(names)} PNG file(s)", "files": names}
```'''


def fence(code: str) -> str:
    return f"Here you go:\n```python\n{code.strip()}\n```\nHope it helps."


class Script:
    """A scripted model: returns its replies in order, records each call."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, prompt, seed=None, temperature=None, should_stop=None):
        self.calls.append({"prompt": prompt, "seed": seed,
                           "temperature": temperature})
        reply = self.replies[min(len(self.calls), len(self.replies)) - 1]
        if isinstance(reply, BaseException):
            raise reply
        return reply


# ============================================================
# Purity
# ============================================================

def test_the_module_is_pure():
    """No toolkit, no engine, no subprocess — the model and the smoke run
    are injected, which is what lets every path here run with a stub."""
    tree = ast.parse((ROOT / "gui_codebehind.py").read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    for banned in ("tkinter", "PySide6", "council_engine", "subprocess",
                   "role_models", "gui_smoke"):
        assert banned not in roots


# ============================================================
# Gate 0 — extract
# ============================================================

def test_the_fenced_block_is_taken_from_prose():
    code, unclosed = gcb.extract_code(GOOD.replace("```python",
                                                   "Sure!\n```python"))
    assert code.startswith("def count_images") and not unclosed


def test_a_helper_in_its_own_block_is_joined_and_nested():
    reply = ('First a helper:\n```python\ndef _png(n):\n'
             '    return n.lower().endswith(".png")\n```\nThen:\n```python\n'
             'def count_images(folder):\n    import os\n'
             '    names = [n for n in os.listdir(folder) if _png(n)]\n'
             '    return {"status": str(len(names)), "files": names}\n```\n'
             'Example:\n```python\nprint(count_images("."))\n```')
    cand = gcb.check(reply, fn_target())
    assert cand.stage == gcb.STAGE_SMOKE, cand.faults
    assert "    def _png(n):" in cand.code and "print(" not in cand.code


def test_an_unclosed_fence_is_reported_as_cut_off():
    reply = "```python\ndef count_images(folder: str) -> dict:\n    names = ["
    cand = gcb.check(reply, fn_target())
    assert not cand.ok and cand.stage == gcb.STAGE_SHAPE
    assert "cut off" in cand.faults[0]


def test_no_code_at_all_is_stage_zero():
    cand = gcb.check("I cannot help with that.", fn_target())
    assert cand.stage == gcb.STAGE_EXTRACT
    assert "no Python code" in cand.faults[0]


def test_prose_after_unfenced_code_is_dropped():
    reply = ('def count_images(folder: str) -> dict:\n'
             '    return {"status": "x", "files": []}\n'
             'This function returns the files.\n')
    cand = gcb.check(reply, fn_target())
    assert cand.stage == gcb.STAGE_SMOKE, cand.faults
    assert any("prose" in n for n in cand.notes)


def test_a_syntax_error_names_its_line():
    cand = gcb.check(fence("def count_images(folder):\n"
                           "    if folder\n        return {}\n"), fn_target())
    assert cand.stage == gcb.STAGE_SHAPE and cand.faults[0].startswith("line 2")


# ============================================================
# Gate 1 — shape, and its deterministic fixes
# ============================================================

def test_a_wrong_named_single_function_is_renamed():
    cand = gcb.check(GOOD.replace("count_images", "count_pngs"), fn_target())
    assert cand.stage == gcb.STAGE_SMOKE, cand.faults
    assert cand.code.startswith("def count_images(folder: str) -> dict:")
    assert any("renamed count_pngs" in n for n in cand.notes)


def test_a_differently_named_parameter_is_aliased_not_rewritten():
    reply = fence('''
def count_images(path):
    import os
    names = os.listdir(path)
    return {"status": str(len(names)), "files": names}''')
    cand = gcb.check(reply, fn_target())
    assert cand.stage == gcb.STAGE_SMOKE, cand.faults
    assert "    path = folder" in cand.code
    assert "def count_images(folder: str) -> dict:" in cand.code


def test_parameters_are_put_in_the_order_the_widgets_pass_them():
    target = fn_target(params=[
        gcb.Param("folder", "str", "folder", "file_picker"),
        gcb.Param("limit", "int", "limit", "spinbox")])
    reply = fence('''
def count_images(limit, folder):
    import os
    names = os.listdir(folder)[:limit]
    return {"status": str(len(names)), "files": names}''')
    cand = gcb.check(reply, target)
    assert cand.code.startswith(
        "def count_images(folder: str, limit: int) -> dict:")
    assert any("order" in n for n in cand.notes)


def test_an_extra_required_parameter_is_a_fault():
    reply = fence('''
def count_images(folder, pattern):
    return {"status": "", "files": []}''')
    cand = gcb.check(reply, fn_target(params=[
        gcb.Param("folder", "str", "folder", "file_picker")]))
    # "pattern" cannot be aliased to anything: every target param is taken.
    assert cand.stage == gcb.STAGE_SHAPE
    assert "pattern" in cand.faults[0]


def test_helpers_imports_and_example_calls_are_tidied_into_one_function():
    reply = fence('''
import os

LIMIT = 100

def _is_png(name):
    return name.lower().endswith(".png")

def count_images(folder):
    names = [n for n in os.listdir(folder) if _is_png(n)][:LIMIT]
    return {"status": f"{len(names)}", "files": names}

print(count_images("."))''')
    cand = gcb.check(reply, fn_target())
    assert cand.stage == gcb.STAGE_SMOKE, cand.faults
    tree = ast.parse(cand.code)
    assert len(tree.body) == 1 and isinstance(tree.body[0], ast.FunctionDef)
    assert "import os" in cand.code and "def _is_png" in cand.code
    assert "LIMIT = 100" in cand.code
    assert "print(" not in cand.code
    assert any("dropped" in n for n in cand.notes)


def test_two_unrelated_functions_and_no_target_name_is_a_fault():
    reply = fence('''
def a(x):
    return {}

def b(y):
    return {}''')
    cand = gcb.check(reply, fn_target())
    assert cand.stage == gcb.STAGE_SHAPE and "exactly one" in cand.faults[0]


def test_a_missing_docstring_is_written_from_the_instruction():
    reply = fence('''
def count_images(folder):
    return {"status": "", "files": []}''')
    cand = gcb.check(reply, fn_target())
    doc = ast.get_docstring(ast.parse(cand.code).body[0])
    assert doc.startswith("count the PNG files")


def test_quotes_and_backslashes_in_the_instruction_cannot_break_the_code():
    reply = fence('''
def count_images(folder):
    return {"status": "", "files": []}''')
    target = fn_target(instruction='say "hi" for C:\\new\\frames"')
    cand = gcb.check(reply, target)
    assert cand.stage == gcb.STAGE_SMOKE, cand.faults
    ast.parse(cand.code)


def test_a_handler_written_as_bare_statements_is_wrapped():
    reply = fence('self.ports.status.set("hello")')
    cand = gcb.check(reply, h_target())
    assert cand.stage == gcb.STAGE_SMOKE, cand.faults
    assert cand.code.startswith("def on_btn_go(self, *args) -> None:")


def test_a_handler_inside_a_class_is_taken_out():
    reply = fence('''
class HandlerMixin:
    def on_btn_go(self, *args):
        self.ports.status.set("hi")''')
    cand = gcb.check(reply, h_target())
    assert cand.stage == gcb.STAGE_SMOKE, cand.faults
    assert "class" not in cand.code


# ============================================================
# Gate 2 — policy, plus what code behind a GUI must never do
# ============================================================

@pytest.mark.parametrize("body,why", [
    ("from tkinter import messagebox\n    messagebox.showinfo('a', 'b')",
     "tkinter"),
    ("import matplotlib.pyplot as plt", "pyplot"),
    ("x = input('folder?')", "input()"),
    ("global CACHE", "global"),
    ("import os\n    os.chdir(folder)", "chdir"),
    ("import sys\n    sys.exit(1)", "exit()"),
    ("while True:\n        pass", "never ends"),
    ("import time\n    time.sleep(5)", "sleep"),
    ("import subprocess", "subprocess"),
    ("import os\n    os.remove(folder)", "remove"),
    ("eval('1')", "eval"),
    ("import threading\n    threading.Thread(target=print).start()",
     "threads"),
])
def test_policy_refuses(body, why):
    reply = fence(f"def count_images(folder):\n    {body}\n"
                  f"    return {{'status': '', 'files': []}}")
    cand = gcb.check(reply, fn_target())
    assert cand.stage == gcb.STAGE_POLICY, (cand.stage, cand.faults)
    assert any(why in f for f in cand.faults), cand.faults


@pytest.mark.parametrize("body", [
    "import sys\n    sys._getframe(0)",
    "import inspect\n    inspect.currentframe()",
    "import inspect\n    inspect.stack()",
    "import sys\n    sys.exc_info()[2].tb_frame",
    "import gc\n    gc.collect()",
    "import sys\n    sys.settrace(None)",
    "import sys\n    sys.addaudithook(print)",
    "import os\n    os.path.realpath = str",
    "import json\n    json.dump = print",
    "import json\n    setattr(json, 'dump', print)",
    "import os.path\n    del os.path.join",
])
def test_reaching_into_the_interpreter_or_rebinding_a_module_is_refused(
        body):
    """Review: each of these passed the static gate. Code behind a widget
    has no use for frames, the garbage collector, interpreter hooks or
    patching a module — and in the smoke run each one reaches the harness's
    own state (its fence and its verdict live in the caller's frame and in
    stdlib functions it calls)."""
    reply = fence(f"def count_images(folder):\n    {body}\n"
                  f"    return {{'status': '', 'files': []}}")
    cand = gcb.check(reply, fn_target())
    assert cand.stage == gcb.STAGE_POLICY, (cand.stage, cand.faults)


@pytest.mark.parametrize("body,why", [
    ("import concurrent.futures as cf\n"
     "    with cf.ProcessPoolExecutor() as ex:\n"
     "        list(ex.map(len, [folder]))", "worker processes"),
    ("from concurrent.futures import ThreadPoolExecutor\n"
     "    with ThreadPoolExecutor() as ex:\n"
     "        list(ex.map(len, [folder]))", "worker processes"),
    ("import _thread\n    _thread.start_new_thread(print, ('x',))",
     "worker processes"),
    ("from os import _exit\n    _exit(0)", "exit()"),
    ("import os\n    os.abort()", "exit()"),
    ("import atexit\n    atexit.register(print, 'bye')", "atexit"),
    ("import _winapi\n    _winapi.GetCurrentProcess()", "_winapi"),
    ("import _ctypes", "_ctypes"),
    ("import signal\n    signal.raise_signal(2)", "signal"),
])
def test_workers_exits_and_low_level_modules_are_refused(body, why):
    """Review: each passed every static gate. A ProcessPoolExecutor's worker
    is a second Python the smoke fence never sees (and, in the app, work
    the window cannot report); _exit/abort end the app (and ended the smoke
    run before the harness answered); atexit, signal, _winapi and _ctypes
    are the OS's and the interpreter's hooks, which code behind a widget
    has no use for — gui_policy denies ctypes, not the module under it."""
    reply = fence(f"def count_images(folder):\n    {body}\n"
                  f"    return {{'status': '', 'files': []}}")
    cand = gcb.check(reply, fn_target())
    assert cand.stage == gcb.STAGE_POLICY, (cand.stage, cand.faults)
    assert any(why in f for f in cand.faults), cand.faults


@pytest.mark.parametrize("ch", ["\u202e", "\u2066", "\u200b", "\ufeff"])
def test_invisible_and_direction_control_characters_are_refused(ch):
    """Review: the person's review is the last gate, and a right-to-left
    override (or a zero-width character) in a comment or string makes the
    diff they read differ from the code that runs ("Trojan Source"). A
    documentation excerpt can carry one into a reply. Code behind a widget
    never needs them; an escape (\\u202e) says the same thing visibly."""
    reply = fence(f'''
def count_images(folder):
    import os
    names = sorted(os.listdir(folder))  # sorted{ch} names
    return {{"status": str(len(names)), "files": names}}''')
    cand = gcb.check(reply, fn_target())
    assert cand.stage == gcb.STAGE_POLICY, (cand.stage, cand.faults)
    assert any("line 4" in f and "invisible" in f for f in cand.faults), \
        cand.faults


def test_an_invisible_character_pasted_into_the_instruction_is_not_a_fault():
    """The shaper writes the instruction into a missing docstring; that line
    is ours, not the model's, so it is cleaned rather than refused."""
    reply = fence('''
def count_images(folder):
    import os
    names = sorted(os.listdir(folder))
    return {"status": str(len(names)), "files": names}''')
    cand = gcb.check(reply, fn_target(
        instruction="count the\u200b PNG files in the folder"))
    assert cand.stage == gcb.STAGE_SMOKE, cand.faults


@pytest.mark.parametrize("body", [
    "import numpy as np\n    np.ctypeslib.load_library('x', folder)",
    "import numpy as np\n    k = np.ctypeslib.ctypes.windll.kernel32",
    "from numpy import ctypeslib\n    ctypeslib.as_array",
])
def test_ctypes_reached_through_another_module_is_refused(body):
    """Review: gui_policy denies `import ctypes`, but numpy hands it over as
    an attribute (np.ctypeslib.ctypes.windll...) — native calls the smoke
    fence does not watch (it cannot refuse ctypes without breaking the
    libraries that load native code lazily) and that passed every gate."""
    reply = fence(f"def count_images(folder):\n    {body}\n"
                  f"    return {{'status': '', 'files': []}}")
    cand = gcb.check(reply, fn_target())
    assert cand.stage == gcb.STAGE_POLICY, (cand.stage, cand.faults)
    assert any("ctypes" in f for f in cand.faults), cand.faults


def test_abort_on_something_else_is_not_an_exit():
    reply = fence('''
def count_images(folder):
    import os

    class Job:
        def abort(self):
            return None
    Job().abort()
    names = sorted(os.listdir(folder))
    return {"status": str(len(names)), "files": names}''')
    cand = gcb.check(reply, fn_target())
    assert cand.stage == gcb.STAGE_SMOKE, cand.faults


def test_ordinary_attributes_and_numpy_stack_are_not_introspection():
    reply = fence('''
def count_images(folder):
    import numpy as np
    from types import SimpleNamespace
    box = SimpleNamespace()
    box.total = int(np.stack([np.zeros(2), np.ones(2)]).sum())
    return {"status": str(box.total), "files": []}''')
    cand = gcb.check(reply, fn_target())
    assert cand.stage == gcb.STAGE_SMOKE, cand.faults


def test_a_broad_except_that_hides_the_failure_is_refused():
    reply = fence('''
def count_images(folder):
    try:
        import os
        names = os.listdir(folder)
    except Exception:
        names = []
    return {"status": str(len(names)), "files": names}''')
    cand = gcb.check(reply, fn_target())
    assert cand.stage == gcb.STAGE_POLICY
    assert "broad except" in cand.faults[0]


@pytest.mark.parametrize("handler", [
    "except Exception as exc:\n        raise ValueError(str(exc))",
    "except Exception as exc:\n        return {'error': str(exc)}",
])
def test_a_broad_except_that_still_reports_is_fine(handler):
    reply = fence(f'''
def count_images(folder):
    try:
        import os
        names = os.listdir(folder)
    {handler}
    return {{"status": str(len(names)), "files": names}}''')
    cand = gcb.check(reply, fn_target())
    assert cand.stage == gcb.STAGE_SMOKE, cand.faults


def test_skipping_one_bad_file_in_a_loop_is_fine():
    reply = fence('''
def count_images(folder):
    import os
    names, skipped = [], 0
    for n in os.listdir(folder):
        try:
            names.append(n.encode("ascii").decode())
        except Exception:
            skipped += 1
            continue
    return {"status": f"{len(names)} ({skipped} skipped)", "files": names}''')
    assert gcb.check(reply, fn_target()).stage == gcb.STAGE_SMOKE


def test_a_standalone_project_may_not_call_council_modules():
    reply = fence('''
def count_images(folder):
    from frame_timing import count_bad_frames
    return {"status": str(count_bad_frames(folder)), "files": []}''')
    cand = gcb.check(reply, fn_target(project_mode="standalone"))
    assert cand.stage == gcb.STAGE_POLICY
    assert "frame_timing" in cand.faults[0]


# ============================================================
# Gate 3 — references
# ============================================================

def catalogue(module):
    return dw.module_info(module)


def test_a_close_misspelling_of_a_linked_function_is_fixed():
    reply = fence('''
def count_images(folder):
    from frame_timing import scan_reports
    r = scan_reports(folder)
    return {"status": r["summary"], "files": r["names"]}''')
    cand = gcb.check(reply, fn_target(), catalogue)
    assert cand.stage == gcb.STAGE_SMOKE, cand.faults
    assert "from frame_timing import scan_report as scan_reports" in cand.code
    assert any("used scan_report()" in n for n in cand.notes)


def test_a_function_that_does_not_exist_is_a_fault_with_what_does():
    reply = fence('''
def count_images(folder):
    import frame_timing
    return {"status": frame_timing.do_magic(folder), "files": []}''')
    cand = gcb.check(reply, fn_target(), catalogue)
    assert cand.stage == gcb.STAGE_REFS
    assert "frame_timing has no function 'do_magic'" in cand.faults[0]


def test_the_wrong_number_of_arguments_is_a_fault():
    reply = fence('''
def count_images(folder):
    from frame_timing import count_bad_frames
    n = count_bad_frames(folder, 3, 4)
    return {"status": str(n), "files": []}''')
    cand = gcb.check(reply, fn_target(), catalogue)
    assert cand.stage == gcb.STAGE_REFS
    assert "takes 1 argument(s); this passes 3" in cand.faults[0]


def test_every_return_must_carry_every_key():
    reply = fence('''
def count_images(folder):
    import os
    if not os.path.isdir(folder):
        return {"status": "no folder"}
    names = os.listdir(folder)
    return {"status": str(len(names)), "files": names}''')
    cand = gcb.check(reply, fn_target())
    assert cand.stage == gcb.STAGE_REFS
    # Lines are the TIDIED code's (a docstring was added at line 2) — the
    # numbered code the repair prompt shows, so the two agree.
    assert "line 5" in cand.faults[0] and "'files'" in cand.faults[0]
    assert cand.code.split("\n")[4].strip() == 'return {"status": "no folder"}'


def test_returning_a_number_instead_of_a_dict_is_a_fault():
    reply = fence('''
def count_images(folder):
    import os
    return len(os.listdir(folder))''')
    cand = gcb.check(reply, fn_target())
    assert cand.stage == gcb.STAGE_REFS and "not a dict" in cand.faults[0]


def test_never_returning_is_a_fault():
    reply = fence('''
def count_images(folder):
    import os
    os.listdir(folder)''')
    cand = gcb.check(reply, fn_target())
    assert cand.stage == gcb.STAGE_REFS and "never returns" in cand.faults[0]


def test_a_function_that_only_ever_reports_an_error_is_a_fault():
    """Review: a refusal written as `return {"error": ...}` passed every
    gate (the smoke run calls an error key a soft pass) and was offered with
    Accept enabled — code that never computes anything."""
    reply = fence('''
def count_images(folder):
    return {"error": "I cannot do that"}''')
    cand = gcb.check(reply, fn_target())
    assert cand.stage == gcb.STAGE_REFS, cand.faults
    assert any("every return is an error" in f for f in cand.faults)


def test_a_function_that_raises_before_any_return_is_a_fault():
    """Review: `raise ValueError(...)` with a dead return under it passed
    the key check and was a soft pass in the smoke run — a placeholder."""
    reply = fence('''
def count_images(folder):
    raise ValueError("not implemented yet")
    return {"status": "", "files": []}''')
    cand = gcb.check(reply, fn_target())
    assert cand.stage == gcb.STAGE_REFS, cand.faults
    assert any("always raises" in f for f in cand.faults)


def test_raising_when_nothing_was_found_after_a_loop_is_fine():
    reply = fence('''
def count_images(folder):
    import os
    for name in sorted(os.listdir(folder)):
        if name.endswith(".png"):
            return {"status": name, "files": [name]}
    raise ValueError("no PNG files in " + folder)''')
    cand = gcb.check(reply, fn_target())
    assert cand.stage == gcb.STAGE_SMOKE, cand.faults


def test_an_error_branch_beside_a_real_result_is_fine():
    reply = fence('''
def count_images(folder):
    import os
    if not os.path.isdir(folder):
        return {"error": "not a folder"}
    names = sorted(os.listdir(folder))
    return {"status": str(len(names)), "files": names}''')
    cand = gcb.check(reply, fn_target())
    assert cand.stage == gcb.STAGE_SMOKE, cand.faults


def test_a_misspelt_port_is_fixed_and_a_made_up_one_is_not():
    cand = gcb.check(fence('self.ports.statuss.set("x")'), h_target())
    assert cand.stage == gcb.STAGE_SMOKE, cand.faults
    assert "self.ports.status.set" in cand.code
    cand = gcb.check(fence('self.ports.result_box.set("x")'), h_target())
    assert cand.stage == gcb.STAGE_REFS
    assert "no port 'result_box'" in cand.faults[0]


@pytest.mark.parametrize("line,why", [
    ("self.ports.plot.set(None)", "chart"),
    ("self.ports.go.get()", "no value to get"),
    ("self.ports.go.set(1)", "no value to set"),
    ("self.ports.status.items()", "only a listbox"),
    ("self.ports.status.widget.setText('x')", "Qt widget"),
    ("self.root.title('x')", "self.root is not part of the app"),
])
def test_ports_are_used_the_way_their_widget_allows(line, why):
    cand = gcb.check(fence(line), h_target())
    assert cand.stage == gcb.STAGE_REFS, (cand.stage, cand.faults)
    assert any(why in f for f in cand.faults), cand.faults


def test_the_chart_recipe_and_other_handlers_are_allowed():
    reply = fence('''
fig = self.ports.plot.widget.figure_for_drawing()
fig.add_subplot(111).plot([1, 2])
self.ports.plot.widget.redraw()
self.ports.go.enable(False)
self.on_close()''')
    cand = gcb.check(reply, h_target())
    assert cand.stage == gcb.STAGE_SMOKE, cand.faults


# ============================================================
# Gate 4 — names
# ============================================================

def test_a_known_module_name_gets_its_import():
    reply = fence('''
def count_images(folder):
    names = [p.name for p in Path(folder).glob("*.png")]
    return {"status": str(np.int64(len(names))), "files": names}''')
    cand = gcb.check(reply, fn_target())
    assert cand.stage == gcb.STAGE_SMOKE, cand.faults
    assert "from pathlib import Path" in cand.code
    assert "import numpy as np" in cand.code
    ast.parse(cand.code)


def test_an_unknown_name_is_a_fault_with_a_suggestion():
    reply = fence('''
def count_images(folder):
    names = list(foldr)
    return {"status": "", "files": names}''')
    cand = gcb.check(reply, fn_target())
    assert cand.stage == gcb.STAGE_NAMES
    assert "undefined name 'foldr'" in cand.faults[0]
    assert "did you mean 'folder'" in cand.faults[0]


def test_a_shortlisted_function_called_bare_is_imported():
    target = fn_target(shortlist=[gcb.Ref("frame_timing", "scan_report",
                                          "scan_report(folder)",
                                          params=("folder",), required=1)])
    reply = fence('''
def count_images(folder):
    r = scan_report(folder)
    return {"status": r["summary"], "files": r["names"]}''')
    cand = gcb.check(reply, target, catalogue)
    assert cand.stage == gcb.STAGE_SMOKE, cand.faults
    assert "from frame_timing import scan_report" in cand.code


def test_annotations_are_not_names_to_resolve():
    assert gcb.undefined_names("def f(x: SomeType) -> Other:\n    return x\n"
                               ) == []


# ============================================================
# The loop
# ============================================================

class Smoke:
    """An injected smoke runner: passes, or fails with the given faults."""

    def __init__(self, *verdicts):
        self.verdicts = list(verdicts)
        self.seen = []

    def __call__(self, cand):
        self.seen.append(cand.code)
        v = self.verdicts[min(len(self.seen), len(self.verdicts)) - 1]
        return SimpleNamespace(
            ok=not v, skipped="", soft="", notes=[],
            faults=lambda v=v: [v] if v else [],
            summary=lambda v=v: "smoke run passed" if not v else v)


def test_a_good_first_reply_is_accepted_in_one_call():
    model, smoke = Script(GOOD), Smoke(None)
    res = gcb.write(fn_target(), model, smoke=smoke)
    assert res.ok and res.attempts == 1
    assert res.code.startswith("def count_images")
    assert any(g.startswith("smoke run: ok") for g in res.gates)
    assert model.calls[0]["seed"] == 1


def test_no_model_is_a_result_with_zero_calls():
    res = gcb.write(fn_target(), None)
    assert not res.ok and res.attempts == 0 and "no model" in res.errors[0]


def test_a_model_that_raises_is_a_result_not_an_exception():
    res = gcb.write(fn_target(), Script(RuntimeError("CUDA out of memory")))
    assert not res.ok and "CUDA out of memory" in res.errors[0]


def test_a_plain_prompt_only_model_is_still_callable():
    res = gcb.write(fn_target(), lambda prompt: GOOD)
    assert res.ok


def test_a_repair_quotes_the_exact_fault_and_a_targeted_hint():
    bad = fence('''
def count_images(folder):
    return {"status": str(len(os.listdir(folder)))}''')
    model = Script(bad, GOOD)
    res = gcb.write(fn_target(), model, smoke=Smoke(None))
    assert res.ok and res.attempts == 2
    repair = model.calls[1]["prompt"]
    assert "WHAT YOU RETURNED" in repair and "WHAT IS WRONG" in repair
    assert "lacks the key(s) 'files'" in repair
    assert "every return must be a dict" in repair
    assert "1 | def count_images" in repair
    assert model.calls[1]["temperature"] == gcb.REPAIR_TEMPERATURE


def test_a_smoke_failure_is_repaired_with_the_chart_recipe():
    target = h_target(instruction="plot the numbers 1 2 3")
    bad = fence("self.ports.plot.widget.figure_for_drawing().add_subplot"
                "(111).plot([1, 2, 3])")
    model = Script(bad, bad)
    smoke = Smoke("smoke run raised TypeError: figure_for_drawing() takes 1 "
                  "positional argument but 2 were given", None)
    res = gcb.write(target, model, smoke=smoke, max_repairs=1)
    assert res.ok
    assert "figure_for_drawing()" in model.calls[1]["prompt"]
    assert "self.ports.plot.widget.redraw()" in model.calls[1]["prompt"]


def test_best_of_n_stops_at_the_first_candidate_that_passes():
    bad = "no code here"
    model = Script(bad, GOOD, GOOD)
    res = gcb.write(fn_target(), model, smoke=Smoke(None), n_best=3)
    assert res.ok and res.attempts == 2
    assert [c["seed"] for c in model.calls] == [1, 2]
    assert [c["temperature"] for c in model.calls] == list(
        gcb.TEMPERATURES[:2])


def test_the_repair_starts_from_the_best_candidate_not_the_last():
    nearly = fence('''
def count_images(folder):
    import os
    return {"status": "x"}''')                 # reaches references
    worse = "Sorry, I cannot."                  # stage 0
    model = Script(nearly, worse, GOOD)
    res = gcb.write(fn_target(), model, smoke=Smoke(None), n_best=2)
    assert res.ok and res.attempts == 3
    assert "return {\"status\": \"x\"}" in model.calls[2]["prompt"]


def test_exhausting_the_rounds_returns_no_code_and_the_best_faults():
    bad = fence('''
def count_images(folder):
    return 3''')
    res = gcb.write(fn_target(), Script(bad), smoke=Smoke(None),
                    max_repairs=2)
    assert not res.ok and res.code == "" and res.attempts == 3
    assert "not a dict" in res.errors[0]
    assert res.best is not None and res.best.stage == gcb.STAGE_REFS


def test_the_same_answer_twice_raises_the_temperature_and_moves_the_seed():
    bad = fence("def count_images(folder):\n    return 3\n")
    model = Script(bad)
    gcb.write(fn_target(), model, smoke=Smoke(None), max_repairs=3)
    temps = [c["temperature"] for c in model.calls]
    seeds = [c["seed"] for c in model.calls]
    assert temps[1] == gcb.REPAIR_TEMPERATURE
    assert temps[2] > temps[1] and seeds[2] != 101


def test_stop_ends_the_loop_before_the_next_call():
    model = Script("nothing")
    calls = []

    def stopper():
        calls.append(1)
        return len(model.calls) >= 1

    res = gcb.write(fn_target(), model, should_stop=stopper, max_repairs=3)
    assert res.stopped and len(model.calls) == 1 and not res.ok


def test_stop_during_a_generation_is_stopped_not_a_model_failure():
    """Review: the engine answers should_stop() by RAISING (council_engine.
    GenerationCancelled), and the loop reported that as 'the model call
    failed: GenerationCancelled(...)' with stopped=False."""
    pressed = []

    def model(prompt, seed=None, temperature=None, should_stop=None):
        pressed.append(1)                    # Stop, mid-generation
        raise RuntimeError("generation cancelled")

    res = gcb.write(fn_target(), model, should_stop=lambda: bool(pressed),
                    max_repairs=2)
    assert res.stopped and not res.ok
    assert res.errors[0] == "stopped" and len(pressed) == 1


class SoftSmoke:
    """Passes every candidate; the ones whose code contains ``soft_on``
    pass softly (a deliberate ValueError on the sample data)."""

    def __init__(self, soft_on: str):
        self.soft_on, self.seen = soft_on, []

    def __call__(self, cand):
        self.seen.append(cand.code)
        soft = ("it raised ValueError on the sample data: no frames"
                if self.soft_on in cand.code else "")
        return SimpleNamespace(ok=True, skipped="", soft=soft, notes=[],
                               faults=lambda: [],
                               summary=lambda: "smoke run passed")


SOFT = fence('''
def count_images(folder):
    import os
    names = [n for n in os.listdir(folder) if n.endswith(".tif")]
    if not names:
        raise ValueError("no frames")
    return {"status": str(len(names)), "files": names}''')


def test_best_of_n_prefers_a_clean_pass_to_a_soft_one():
    """Review: a soft pass (ValueError on the sample data the planner chose
    for this task) ended the first round, so a candidate that looked for
    the wrong files was offered while two more samples were never drawn."""
    model = Script(SOFT, GOOD)
    res = gcb.write(fn_target(), model, smoke=SoftSmoke(".tif"), n_best=3)
    assert res.ok and res.attempts == 2
    assert ".tif" not in res.code and ".png" in res.code


def test_a_soft_pass_is_still_offered_when_no_sample_does_better():
    model = Script(SOFT)
    res = gcb.write(fn_target(), model, smoke=SoftSmoke(".tif"), n_best=2,
                    max_repairs=3)
    assert res.ok and ".tif" in res.code
    assert res.attempts == 2                 # no repair spent on it
    assert any("no frames" in n for n in res.notes)


def test_a_held_soft_pass_survives_a_later_sample_failing():
    """Review: the soft pass held for a cleaner sample was dropped when the
    NEXT sample's model call raised (a timeout) — a candidate that passed
    every gate, and was offered before the hold existed, became 'the model
    call failed' with nothing to accept."""
    model = Script(SOFT, RuntimeError("timed out"))
    res = gcb.write(fn_target(), model, smoke=SoftSmoke(".tif"), n_best=3)
    assert res.ok and ".tif" in res.code, res.errors
    assert res.attempts == 2
    assert any("timed out" in n for n in res.notes), res.notes


def test_a_smoke_runner_that_raises_does_not_break_the_promise():
    def boom(_cand):
        raise OSError("sandbox gone")
    res = gcb.write(fn_target(), Script(GOOD), smoke=boom)
    assert res.ok
    assert any("could not be made" in n for n in res.notes)


def test_an_instruction_too_long_for_the_window_is_refused_before_any_call():
    """Review: a 200 KB instruction (a pasted spec) built a 198k-char
    prompt against a 9.1k budget — everything sheddable shed and still 20x
    over, so the engine's clamp would cut the middle (the signature) out of
    every one of up to six calls. Say so instead, and call nothing."""
    model = Script(GOOD)
    long = "count the PNG files in the folder and list them. " * 400
    res = gcb.write(fn_target(instruction=long), model)
    assert not res.ok and model.calls == [] and res.attempts == 0
    assert "too long" in res.errors[0]
    assert gcb.write(fn_target(), Script(GOOD)).ok      # a normal one fits


def test_too_many_ports_is_not_blamed_on_a_short_instruction():
    """Review: handler mode on a 120-port window with the instruction
    'enable the save button' was refused as 'the instruction is too long —
    say it in a few sentences'. The ports were what did not fit, and the
    way out is to name the widgets it uses (only those are then listed)."""
    ports = [gcb.PortRow(f"field_{i:03d}", "entry", "str", "var", "",
                         f"Field {i}") for i in range(120)]
    model = Script(GOOD)
    res = gcb.write(h_target(ports=ports,
                             instruction="enable the save button"), model)
    assert not res.ok and model.calls == []
    assert "120 ports" in res.errors[0] and "too long" not in res.errors[0]
    # Naming the ones it uses lists only those, and it fits.
    named = h_target(ports=ports, instruction="copy field_001 into field_002")
    model = Script(GOOD)
    gcb.write(named, model)
    assert len(model.calls) >= 1


def test_an_empty_instruction_or_no_outputs_is_refused_before_any_call():
    model = Script(GOOD)
    assert not gcb.write(fn_target(instruction="  "), model).ok
    assert not gcb.write(fn_target(outputs=[]), model).ok
    assert model.calls == []


def test_progress_is_reported():
    lines = []
    gcb.write(fn_target(), Script(GOOD), smoke=Smoke(None),
              on_progress=lines.append)
    assert lines and "asking the model" in lines[0]


# ============================================================
# Prompt
# ============================================================

def test_the_prompt_carries_the_signature_keys_and_the_example():
    prompt, shed = gcb.build_prompt(fn_target())
    assert "def count_images(folder: str) -> dict:" in prompt
    assert '"status"' in prompt and '"files"' in prompt
    assert "raise ValueError" in prompt
    assert "EXAMPLE of the shape" in prompt and shed == []


def test_documentation_is_included_and_shed_first_when_tight():
    docs = [{"server": "pkgdocs", "source": "skimage/filters.md",
             "title": "skimage.filters.threshold_otsu",
             "text": "threshold_otsu(image, nbins=256) -> float. " * 40}]
    target = fn_target(docs=docs)
    prompt, shed = gcb.build_prompt(target)
    assert "DOCUMENTATION" in prompt and "threshold_otsu" in prompt
    small, shed = gcb.build_prompt(target, 1800)
    assert "DOCUMENTATION" not in small and "documentation" in shed


def test_a_handler_prompt_lists_ports_with_their_real_methods():
    prompt, _ = gcb.build_prompt(h_target())
    assert "self.ports.files.items() -> list of str" in prompt
    assert "figure_for_drawing()" in prompt and "never .set(fig)" in prompt
    assert "self.ports.go  button" in prompt


def test_shedding_keeps_the_ports_the_task_names():
    target = h_target(instruction="put the folder name in status")
    prompt, shed = gcb.build_prompt(target, 900)
    assert "ports the task does not name" in shed
    assert "self.ports.status" in prompt and "self.ports.plot" not in prompt


def test_the_typed_shortlist_prefers_rare_words():
    infos = [(m, dw.module_info(m)) for m in
             ("frame_timing", "frame_camera", "vault_analyst", "image_stats")]
    refs = gcb.shortlist("count the frames with a bad timing in the folder",
                         infos)
    assert refs[0].module == "frame_timing"
    assert refs[0].signature.endswith(")") or "->" in refs[0].signature
    assert gcb.shortlist("zzz qqq", infos) == []


def test_a_function_returning_a_class_shows_what_to_read_off_it():
    infos = [("frame_timing", dw.module_info("frame_timing"))]
    refs = gcb.shortlist("classify every frame in the folder", infos)
    folder = next(r for r in refs if r.name == "classify_folder")
    assert folder.returns_class == "FolderReport"
    assert "bad" in folder.members and "summary()" in folder.members
    text = gcb._shortlist_text([folder])
    assert "returns a FolderReport with:" in text


def test_building_a_prompt_is_fast():
    target = h_target()
    t0 = time.perf_counter()
    for _ in range(100):
        gcb.build_prompt(target)
    assert (time.perf_counter() - t0) / 100 < 0.005


def test_the_static_gates_are_fast():
    target = fn_target()
    t0 = time.perf_counter()
    for _ in range(50):
        gcb.check(GOOD, target, catalogue)
    assert (time.perf_counter() - t0) / 50 < 0.02


# ============================================================
# logic.py splicing
# ============================================================

def test_a_function_is_appended_then_replaced_in_place():
    first = gcb.splice_function("", "f", "def f():\n    return {}\n")
    assert first.startswith(gcb.LOGIC_HEADER.splitlines()[0])
    both = gcb.splice_function(first, "g", "def g():\n    return {'a': 1}\n")
    again = gcb.splice_function(both, "f", "def f():\n    return {'b': 2}\n")
    tree = ast.parse(again)
    names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
    assert names == ["f", "g"]
    assert "'b': 2" in gcb.function_text(again, "f")


def test_function_text_is_what_the_fingerprint_is_taken_of():
    src = gcb.splice_function("", "f", "def f():\n    return {}\n")
    assert gcb.function_text(src, "f") == "def f():\n    return {}\n"
    assert gcb.sha(gcb.function_text(src, "f")) == gcb.sha(
        "def f():\n    return {}\n")


# ============================================================
# Fixes from the measurement runs (2026-10-02)
# ============================================================

STOPWATCH = fence('''
def on_btn_go(self, *args) -> None:
    import time
    start = getattr(self, "_ai_start", None)
    if start is None:
        self._ai_start = time.monotonic()
        self.ports.status.set("started")
    else:
        self.ports.status.set(f"{time.monotonic() - start:.1f} s")
        self._ai_start = None''')


def _faults(raw, target):
    return " | ".join(gcb.check(raw, target).faults)


def test_a_handler_may_keep_private_state_between_clicks():
    """The user allowed it: a stopwatch, or the list as it was before a
    filter, needs a value that outlives one click."""
    assert "_ai_start" not in _faults(STOPWATCH, h_target())


def test_any_other_self_attribute_is_still_refused():
    for name in ("counter", "_ai_", "ports_backup"):
        raw = fence(f'''
def on_btn_go(self, *args) -> None:
    self.{name} = 1
    self.ports.status.set("x")''')
        faults = _faults(raw, h_target())
        assert f"self.{name}" in faults, (name, faults)


def test_a_typed_number_box_is_described_as_maybe_none():
    entry = gcb.PortRow("age", "entry", "int", "var", "", "Age")
    spin = gcb.PortRow("count", "spinbox", "int", "var", "", "Count")
    assert "None" in gcb.port_line(entry)
    assert "None" not in gcb.port_line(spin)        # a spinbox always has one


def test_an_input_problem_is_a_message_not_an_exception():
    block = gcb._return_block(fn_target())
    assert "do not raise" in block and "Enter a number" in block
    ports = gcb._ports_block(h_target())
    assert "instead of raising" in ports and "self._ai_" in ports


def test_one_failed_model_call_does_not_end_the_attempt():
    """Measured: Ollama's 'token repeat limit reached' on the first of three
    samples ended the case with two samples and every repair unspent."""
    model = Script(RuntimeError("prediction aborted, token repeat limit "
                                "reached"), GOOD)
    res = gcb.write(fn_target(), model, n_best=3)
    assert res.ok and len(model.calls) == 2
    assert model.calls[1]["temperature"] != model.calls[0]["temperature"]
    assert any("trying again" in n for n in res.notes)


def test_a_failure_another_call_cannot_fix_is_not_retried():
    class BackendUnavailable(Exception):
        pass

    model = Script(BackendUnavailable("No Ollama server answers"), GOOD)
    res = gcb.write(fn_target(), model, n_best=3)
    assert not res.ok and len(model.calls) == 1


def test_the_same_failure_twice_stops_the_retries():
    model = Script(RuntimeError("boom"), RuntimeError("boom"), GOOD)
    res = gcb.write(fn_target(), model, n_best=3)
    assert not res.ok and len(model.calls) == 2 and "boom" in res.errors[0]
