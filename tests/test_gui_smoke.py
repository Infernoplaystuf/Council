"""
gui_smoke — a model-written function or handler, run once on sample data in
a fenced subprocess.

Every "bad" case here PASSED gui_policy (measured with the map's
policy_probe.py): a port that does not exist, chart.set(fig), a write
outside, shutil.move, an endless loop, tkinter in a Qt handler. The smoke
run is what catches them, with the line, before a person is asked to accept
the code. Each case also proves the fence held: the file it tried to write,
or the folder it tried to move, is not there afterwards.

No model anywhere. Each run is one subprocess (~0.3 s; ~1 s when numpy,
Pillow or matplotlib must load first).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gui_smoke  # noqa: E402

PORTS = [
    {"name": "folder", "kind": "file_picker", "type": "path",
     "binder": "var", "sample": "<FOLDER>"},
    {"name": "threshold", "kind": "scale", "type": "float", "binder": "var"},
    {"name": "status", "kind": "label", "type": "str", "binder": "var"},
    {"name": "names", "kind": "listbox", "type": "str", "binder": "list"},
    {"name": "view", "kind": "image_canvas", "type": "image",
     "binder": "proxy", "writer": "set_image"},
    {"name": "chart", "kind": "chart_panel", "type": "figure",
     "binder": "proxy", "writer": "figure_for_drawing"},
    {"name": "go", "kind": "button", "type": "event", "binder": "event"},
]
HEAD = "from __future__ import annotations\n\nclass HandlerMixin:\n"


def handler(body: str, **kw) -> gui_smoke.SmokeResult:
    return gui_smoke.smoke_handler(HEAD + body, "on_btn_go", PORTS,
                                   app_root=ROOT, timeout=kw.pop("timeout", 8),
                                   **kw)


def function(src: str, args, expect, name="f", **kw):
    return gui_smoke.smoke_function({"logic.py": src}, name, args, expect,
                                    app_root=ROOT,
                                    timeout=kw.pop("timeout", 8), **kw)


@pytest.fixture
def victim(tmp_path):
    """A path OUTSIDE the sandbox a candidate will try to reach."""
    return tmp_path / "victim.txt"


# ============================================================
# Good code passes
# ============================================================

def test_a_good_handler_passes_and_says_what_it_set():
    r = handler("""
    def on_btn_go(self, *args) -> None:
        from pathlib import Path
        files = sorted(p.name for p in Path(self.ports.folder.get()).iterdir())
        self.ports.names.set(files)
        self.ports.status.set(f"{len(files)} file(s)")
""")
    assert r.ok, r.summary()
    assert set(r.sets) == {"names", "status"}


def test_the_chart_recipe_passes():
    r = handler("""
    def on_btn_go(self, *args) -> None:
        fig = self.ports.chart.widget.figure_for_drawing()
        fig.add_subplot(111).plot([1, 2, 3])
        self.ports.chart.widget.redraw()
""")
    assert r.ok, r.summary()


def test_a_good_function_passes_with_its_keys():
    r = function("""
def f(folder):
    import os
    names = sorted(n for n in os.listdir(folder) if n.endswith(".png"))
    return {"count": len(names), "names": names}
""", ["<FOLDER>"], {"count": "text", "names": "list"})
    assert r.ok, r.summary()
    assert r.result_keys == ["count", "names"]


def test_sample_frames_are_real_images_pillow_can_open():
    """The sample folder holds valid PNGs written with the stdlib, so image
    code is exercised for real, not handed a placeholder."""
    pytest.importorskip("PIL")
    pytest.importorskip("numpy")
    r = function("""
def f(path):
    import numpy as np
    from PIL import Image
    a = np.asarray(Image.open(path))
    return {"mean": float(a.mean()), "size": f"{a.shape[1]}x{a.shape[0]}"}
""", ["<PNG>"], {"mean": "number", "size": "text"})
    assert r.ok, r.summary()


def test_writing_inside_the_given_output_folder_is_allowed():
    r = function("""
def f(out):
    import os
    p = os.path.join(out, "report.txt")
    with open(p, "w") as fh:
        fh.write("ok")
    return {"saved": p}
""", ["<OUT>"], {"saved": "text"})
    assert r.ok, r.summary()


def test_a_deliberate_value_error_is_the_documented_failure_not_a_fault():
    """raise ValueError("why") is how a logic function says "cannot" — on
    three sample frames that can be the honest answer. Said, not failed."""
    r = function("""
def f(folder):
    raise ValueError("no frames carry a timestamp in " + folder)
""", ["<FOLDER>"], {"count": "text"})
    assert r.ok
    assert "ValueError" in r.soft


# ---- a crash relabelled as a refusal -------------------------------------

RECORDED = json.loads((ROOT / "tests" / "data" / "llm_bench" /
                       "recorded_2026-10-05.json").read_text(encoding="utf-8"))


def recorded_code(key: str) -> str:
    """The code inside one recorded reply's ```python fence."""
    reply = RECORDED["code"][key]
    return reply.split("```python", 1)[-1].split("```", 1)[0].strip("\n")


def test_broad_handler_at_reads_the_innermost_handler():
    src = """def f(x):
    try:
        return int(x)
    except Exception as e:
        raise ValueError(f"bad: {e}")


def g(x):
    try:
        return int(x)
    except ValueError:
        raise ValueError("Age must be a whole number")


def h(x):
    try:
        try:
            return int(x)
        except ValueError:
            raise ValueError("inner")
    except:
        raise ValueError("outer")
"""
    assert gui_smoke.broad_handler_at(src, 5) == "except Exception"
    assert gui_smoke.broad_handler_at(src, 12) == ""
    assert gui_smoke.broad_handler_at(src, 20) == ""
    assert gui_smoke.broad_handler_at(src, 22) == "except:"
    assert gui_smoke.broad_handler_at(src, 3) == ""
    assert gui_smoke.raises_at(src, 5) and not gui_smoke.raises_at(src, 3)


def test_a_crash_a_catch_all_raises_again_as_value_error_is_a_fault():
    """qwen2.5's K7 (2026-10-05), as recorded: `except Exception as e:
    raise ValueError(f"Failed to save: {e}")` around an f-string that
    itself raised. The run called it a deliberate refusal, the writer
    accepted it, and the hidden test read an empty file."""
    r = function(recorded_code("qwen2.5 K7 4"),
                 ["sample", 3, True, "<SAVE>"], {"status": "text"},
                 name="save")
    assert not r.ok and not r.soft
    assert "Invalid format specifier" in r.error
    assert "raised again as ValueError" in r.error
    assert r.where == "logic.py:8" and "file.write" in r.line_text


def test_a_refusal_the_code_raised_itself_stays_one_under_a_catch_all():
    """qwen2.5-coder's K4 shape: `raise ValueError(result["error"])` inside
    the try, relabelled by `except Exception: raise ValueError(str(e))`."""
    r = function("""
def f(folder):
    try:
        result = {"error": "no frames in " + folder}
        if "error" in result:
            raise ValueError(result["error"])
        return {"count": "1"}
    except Exception as e:
        raise ValueError(str(e))
""", ["<FOLDER>"], {"count": "text"})
    assert r.ok, r.summary()
    assert "no frames" in r.soft


def test_a_refusal_from_a_handler_that_names_its_error_stays_one():
    r = function("""
def f(age):
    try:
        return {"count": str(int(age))}
    except ValueError:
        raise ValueError("Age must be a whole number")
""", ["thirty"], {"count": "text"})
    assert r.ok, r.summary()
    assert "whole number" in r.soft


@pytest.mark.parametrize("parse", ["int(age)", "float(age)",
                                   "json.loads(age)",
                                   "datetime.strptime(age, '%Y')"])
def test_a_catch_all_around_reading_the_users_text_is_still_a_refusal(parse):
    """The commonest shape of an honest refusal small models write: a
    catch-all around int(text). What it caught is the user's text failing
    to parse, not the code failing — so it stays the refusal it means."""
    r = function(f"""
import json
from datetime import datetime


def f(age):
    try:
        n = {parse}
    except Exception as e:
        raise ValueError(f"Age must be a whole number: {{e}}")
    return {{"count": str(n)}}
""", ["thirty"], {"count": "text"})
    assert r.ok, r.summary()
    assert "whole number" in r.soft


def test_parse_failed_tells_bad_input_from_bad_code():
    assert gui_smoke.parse_failed(ValueError(
        "invalid literal for int() with base 10: 'x'"))
    assert gui_smoke.parse_failed(ValueError(
        "could not convert string to float: 'x'"))
    import json as _json
    try:
        _json.loads("x")
    except ValueError as exc:
        assert gui_smoke.parse_failed(exc)
    assert not gui_smoke.parse_failed(ValueError(
        "Invalid format specifier ' \"age\": {age}, \"subscribed\": "
        "{subscribed}' for object of type 'str'"))
    assert not gui_smoke.parse_failed(KeyError("width"))


def test_in_a_handler_a_crash_raised_again_as_value_error_is_a_fault():
    """The same, through the failure envelope handler mode wraps a body in
    (report_error decides it there)."""
    r = handler("""
    def on_btn_go(self, *args) -> None:
        try:
            try:
                self.ports.status.set(f'{"n": 1}')
            except Exception as e:
                raise ValueError(f"Failed: {e}")
        except Exception as exc:
            self.report_error("Go", exc)
""")
    assert not r.ok and not r.soft
    assert "raised again as ValueError" in r.error
    assert r.where == "handlers.py:8" and "status.set" in r.line_text


# ---- repeated presses: every press from the ORIGINAL data ----------------

K2_PORTS = [
    {"name": "search", "kind": "entry", "type": "str", "binder": "var",
     "sample": "sample", "sample2": ""},
    {"name": "fruits", "kind": "listbox", "type": "str", "binder": "list",
     "sample": ["alpha", "beta"], "sample2": []},
    {"name": "count", "kind": "label", "type": "str", "binder": "var"},
    {"name": "filter", "kind": "button", "type": "event", "binder": "event"},
]


def k2_press(key: str, ports=None) -> gui_smoke.SmokeResult:
    body = "\n".join("    " + ln if ln.strip() else ""
                     for ln in recorded_code(key).split("\n"))
    return gui_smoke.smoke_handler(HEAD + body + "\n", "on_btn_filter",
                                   ports or K2_PORTS, app_root=ROOT,
                                   timeout=8)


@pytest.mark.parametrize("key", ["llama3.1:8b K2 1", "qwen2.5-coder K2 1"])
def test_a_handler_that_filters_what_the_last_press_left_is_caught(key):
    """K2 (2026-10-05): one press passed the smoke run for both; each
    filtered the list the previous press had left, and failed the hidden
    test ("'AN' must search the ORIGINAL list")."""
    r = k2_press(key)
    assert not r.ok, r.summary()
    assert any("each press must start from the ORIGINAL data" in p
               for p in r.problems), r.problems
    # what was SELECTED in the list is said as that, never as its items
    said = next(p for p in r.problems if "ORIGINAL" in p)
    assert "fruits selected=[]" in said and "fruits=" not in said, said


def test_one_press_still_cannot_tell():
    """Without a second sample (a task that never says ORIGINAL) the run is
    the single press it always was."""
    plain = [{k: v for k, v in p.items() if k != "sample2"} for p in K2_PORTS]
    assert k2_press("llama3.1:8b K2 1", plain).ok


def test_a_handler_that_keeps_the_original_passes_the_repeated_presses():
    """phi4:14b's K2 — the one model that passed the hidden test."""
    r = k2_press("phi4:14b K2 1")
    assert r.ok, r.summary()


def test_ports_keep_what_a_press_set():
    """The fake window has state now: a listbox's items are what the last
    press set, as in the real one."""
    r = handler("""
    def on_btn_go(self, *args) -> None:
        self.ports.names.set(["one"])
        self.ports.status.set(",".join(self.ports.names.items()))
        if self.ports.status.get() != "one":
            raise RuntimeError("the label did not keep its text")
""")
    assert r.ok, r.summary()


# ============================================================
# Bad code fails, with the line, and the fence holds
# ============================================================

def test_chart_set_raises_the_real_type_error_with_the_line():
    r = handler("""
    def on_btn_go(self, *args) -> None:
        from matplotlib.figure import Figure
        self.ports.chart.set(Figure())
""")
    assert not r.ok
    assert "figure_for_drawing() takes 1 positional argument" in r.error
    assert r.where == "handlers.py:7"


def test_a_port_that_does_not_exist_names_the_real_ports():
    r = handler("""
    def on_btn_go(self, *args) -> None:
        self.ports.result.set("x")
""")
    assert not r.ok
    assert "no port 'result'" in r.error and "status" in r.error


def test_an_image_port_given_a_path_says_open_it_first():
    r = handler("""
    def on_btn_go(self, *args) -> None:
        self.ports.view.set(self.ports.folder.get() + "/frame_000.png")
""")
    assert not r.ok
    assert "PIL image" in r.error and "Image.open" in r.error


def test_an_undefined_name_is_a_name_error_at_its_line():
    r = handler("""
    def on_btn_go(self, *args) -> None:
        self.ports.status.set(str(np.mean([1, 2])))
""")
    assert not r.ok
    assert "NameError" in r.error and r.where == "handlers.py:6"


def test_a_write_outside_the_sandbox_is_blocked(victim):
    r = handler(f"""
    def on_btn_go(self, *args) -> None:
        with open({str(victim)!r}, "w") as fh:
            fh.write("clobbered")
""")
    assert not r.ok
    assert any("writes outside" in b for b in r.blocked)
    assert not victim.exists()


def test_a_swallowed_refusal_still_fails(victim):
    """Catching the PermissionError and carrying on must not turn a blocked
    write into a pass."""
    r = handler(f"""
    def on_btn_go(self, *args) -> None:
        try:
            with open({str(victim)!r}, "w") as fh:
                fh.write("x")
        except Exception:
            pass
        self.ports.status.set("done")
""")
    assert not r.ok and r.blocked
    assert not victim.exists()


def test_the_fence_keeps_its_own_path_check_when_the_candidate_rebinds_os(
        victim):
    """Review: the fence's inside-the-sandbox test called os.path.realpath
    at refusal time, a module attribute the candidate's own code could
    rebind. The fence now holds the functions it relies on from before it
    is armed (and gui_codebehind refuses the rebinding statically)."""
    r = handler(f"""
    def on_btn_go(self, *args) -> None:
        import os
        here = os.getcwd()
        os.path.realpath = lambda p, *a, **k: os.path.join(here, "x")
        with open({str(victim)!r}, "w") as fh:
            fh.write("clobbered")
""")
    assert not r.ok
    assert any("writes outside" in b for b in r.blocked), r.summary()
    assert not victim.exists()


def test_moving_a_folder_is_blocked(tmp_path):
    dest = tmp_path / "moved_here"
    r = handler(f"""
    def on_btn_go(self, *args) -> None:
        import shutil
        shutil.move(self.ports.folder.get(), {str(dest)!r})
""")
    assert not r.ok
    assert any("shutil.move" in b for b in r.blocked)
    assert not dest.exists()


def test_deleting_even_a_sample_file_is_blocked():
    r = handler("""
    def on_btn_go(self, *args) -> None:
        import os
        os.remove(os.path.join(self.ports.folder.get(), "notes.txt"))
""")
    assert not r.ok and any("os.remove" in b for b in r.blocked)


def test_starting_a_process_is_blocked():
    r = handler("""
    def on_btn_go(self, *args) -> None:
        import subprocess
        subprocess.run(["cmd", "/c", "echo", "hi"])
""")
    assert not r.ok and any("subprocess.Popen" in b for b in r.blocked)


def test_a_worker_process_is_blocked(victim):
    """Review: a ProcessPoolExecutor started a second Python with NO fence —
    multiprocessing creates its worker through _winapi.CreateProcess, not
    subprocess.Popen — and the worker ran model code that wrote outside the
    sandbox. concurrent.futures passes gui_policy (only multiprocessing is
    denied by name)."""
    r = function(f"""
import concurrent.futures as cf

def work(path):
    with open(path, "w") as fh:
        fh.write("written by an unfenced worker")
    return 1

def f():
    with cf.ProcessPoolExecutor(max_workers=1) as ex:
        return {{"n": ex.submit(work, {str(victim)!r}).result()}}
""", [], {"n": "number"}, timeout=20)
    assert not r.ok, r.summary()
    # The pool's pipe (_winapi.CreateNamedPipe) is refused before its
    # CreateProcess is reached; a fork on POSIX.
    assert any("_winapi." in b or "fork" in b for b in r.blocked), \
        r.summary()
    assert not victim.exists()


def test_a_database_outside_the_sandbox_is_blocked(tmp_path):
    """Review: sqlite3 opens its file in C (no 'open' audit event), so a
    database anywhere on the disk was created or changed unfenced."""
    db = tmp_path / "outside.db"
    r = function(f"""
def f():
    import sqlite3
    con = sqlite3.connect({str(db)!r})
    con.execute("create table t (x)")
    con.commit()
    con.close()
    return {{"n": 1}}
""", [], {"n": "number"})
    assert not r.ok and any("sqlite3.connect" in b for b in r.blocked), \
        r.summary()
    assert not db.exists()


def test_a_database_inside_the_sandbox_or_in_memory_is_fine():
    r = function("""
def f(out):
    import os
    import sqlite3
    mem = sqlite3.connect(":memory:")
    mem.execute("create table t (x)")
    con = sqlite3.connect(os.path.join(out, "results.db"))
    con.execute("create table t (x)")
    con.commit()
    con.close()
    return {"n": 2}
""", ["<OUT>"], {"n": "number"})
    assert r.ok, r.summary()


def test_nothing_the_candidate_leaves_behind_runs_after_the_fence(victim):
    """Review: the fence was DISARMED after the call, and the interpreter
    then ran the candidate's exit handlers with nothing watching — a write
    outside the sandbox from atexit landed on disk."""
    r = function(f"""
def f():
    import atexit

    def later():
        with open({str(victim)!r}, "w") as fh:
            fh.write("after the fence")
    atexit.register(later)
    return {{"n": 1}}
""", [], {"n": "number"})
    assert not victim.exists(), r.summary()


def test_the_returned_value_is_inspected_inside_the_fence(victim):
    """Review: repr()/str() of the returned value — the preview and the
    port checks — ran the candidate's own __repr__ after the fence was
    disarmed."""
    r = function(f"""
class Count:
    def __repr__(self):
        with open({str(victim)!r}, "w") as fh:
            fh.write("from __repr__")
        return "Count()"

def f():
    return {{"n": Count()}}
""", [], {"n": "any"})
    assert not r.ok and any("writes outside" in b for b in r.blocked), \
        r.summary()
    assert not victim.exists()


def test_a_verdict_the_candidate_wrote_itself_is_not_believed():
    """Review: the verdict file lives in the sandbox, which the candidate
    may write. Writing {"ok": true} there and leaving before the harness
    did turned a function that does nothing into 'smoke run passed'."""
    r = function("""
def f():
    import json
    from os import _exit
    with open("_smoke_out.json", "w") as fh:
        json.dump({"ok": True, "result_keys": ["n"]}, fh)
    _exit(0)
""", [], {"n": "number"})
    assert not r.ok, r.summary()
    assert "verdict" in r.error


def test_the_network_is_blocked():
    r = handler("""
    def on_btn_go(self, *args) -> None:
        import socket
        socket.create_connection(("127.0.0.1", 9), timeout=1)
""")
    assert not r.ok and any("socket" in b for b in r.blocked)


def test_a_toolkit_import_is_blocked_even_though_policy_admits_tkinter():
    r = handler("""
    def on_btn_go(self, *args) -> None:
        from tkinter import messagebox
        messagebox.showinfo("hi", "there")
""")
    assert not r.ok and any("tkinter" in b for b in r.blocked)


def test_an_endless_loop_times_out_instead_of_hanging():
    t0 = time.perf_counter()
    r = handler("""
    def on_btn_go(self, *args) -> None:
        while True:
            pass
""", timeout=3)
    assert not r.ok and r.timed_out
    assert time.perf_counter() - t0 < 10
    assert "did not finish" in r.faults()[0]


def test_sys_exit_is_reported_as_closing_the_app():
    r = function("""
def f(folder):
    import sys
    sys.exit(1)
""", ["<FOLDER>"], {"count": "text"})
    assert not r.ok and "closes the app" in r.error


# ============================================================
# What a function returns is checked against where it is shown
# ============================================================

def test_a_list_for_a_label_is_a_problem():
    r = function("""
def f(folder):
    import os
    return {"status": os.listdir(folder)}
""", ["<FOLDER>"], {"status": "text"})
    assert not r.ok and "shows text, but got a list" in r.problems[0]


def test_a_string_for_a_listbox_would_be_split_into_letters():
    r = function("""
def f(folder):
    return {"names": "a,b,c"}
""", ["<FOLDER>"], {"names": "list"})
    assert not r.ok and "split into letters" in r.problems[0]


def test_a_missing_key_is_named():
    r = function("""
def f(folder):
    return {"count": 3}
""", ["<FOLDER>"], {"count": "text", "names": "list"})
    assert not r.ok and "'names'" in r.problems[0]


def test_an_error_key_is_the_documented_failure():
    r = function("""
def f(folder):
    return {"error": "nothing to scan"}
""", ["<FOLDER>"], {"count": "text"})
    assert r.ok and "nothing to scan" in r.soft


# ============================================================
# Hardware modules are stand-ins
# ============================================================

def test_frame_camera_is_marked_for_a_stand_in_and_frame_timing_is_not():
    assert gui_smoke.is_faked("frame_camera", ROOT)
    assert not gui_smoke.is_faked("frame_timing", ROOT)


def test_a_hardware_module_runs_as_a_stand_in_returning_its_keys():
    src = """
def f(folder):
    from frame_camera import start
    r = start(folder)
    return {"status": r["summary"]}
"""
    fakes = gui_smoke.fakes_for(gui_smoke.imported_roots(src), ROOT)
    assert "start" in fakes["frame_camera"]
    assert "summary" in fakes["frame_camera"]["start"]
    r = function(src, ["<FOLDER>"], {"status": "text"}, fakes=fakes)
    assert r.ok, r.summary()
    assert any("stand-in" in n for n in r.notes)


def test_a_standalone_project_cannot_reach_the_councils_modules():
    r = gui_smoke.smoke_function({"logic.py": """
def f(folder):
    import frame_timing
    return {"count": 1}
"""}, "f", ["<FOLDER>"], {"count": "text"}, app_root=None, timeout=8)
    assert not r.ok and "No module named 'frame_timing'" in r.error


# ============================================================
# Never raises
# ============================================================

def test_a_missing_interpreter_is_a_skip_not_a_crash(tmp_path):
    r = gui_smoke.smoke_function({"logic.py": "def f():\n    return {}\n"},
                                 "f", [], {}, python=str(tmp_path / "nope"))
    assert not r.ran and r.skipped
    assert "skipped" in r.summary()


def test_preload_is_only_what_the_candidate_names():
    assert gui_smoke.preload_for("import os\n") == []
    assert "numpy" in gui_smoke.preload_for("import numpy as np\n")
    assert "PIL.Image" in gui_smoke.preload_for("from PIL import Image\n")
    assert gui_smoke.preload_for("def broken(:\n") == []


def test_the_child_ends_itself_when_nobody_is_left_to_time_it_out(tmp_path):
    """Review: only the PARENT enforced the timeout, and the Designer runs
    the smoke job on a daemon thread — close the Council while a candidate
    loops and the child ran on forever, a core at 100 % until someone found
    it in Task Manager. The child now has its own watchdog. (Run here with
    no parent timeout at all, as after the Council has gone.)"""
    import json
    import subprocess
    (tmp_path / "logic.py").write_text(
        "def f():\n    n = 0\n    while n >= 0:\n        n += 1\n",
        encoding="utf-8")
    job = {"mode": "function", "module": "logic", "function": "f",
           "args": [], "expect": {}, "app_root": "", "preload": [],
           "fakes": {}, "sandbox": str(tmp_path), "timeout": 1.5}
    path = tmp_path / "_smoke_job.json"
    path.write_text(json.dumps(job), encoding="utf-8")
    t0 = time.perf_counter()
    try:
        proc = subprocess.run([sys.executable, str(ROOT / "gui_smoke.py"),
                               str(path)], cwd=tmp_path, input="",
                              capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        pytest.fail("the child ran on with nobody to stop it")
    assert proc.returncode != 0
    assert time.perf_counter() - t0 < 10
    assert not (tmp_path / "_smoke_out.json").exists()


def test_the_child_half_imports_nothing_from_the_council():
    """It runs under whichever interpreter the project uses, which may have
    none of the Council's packages."""
    import ast
    tree = ast.parse((ROOT / "gui_smoke.py").read_text(encoding="utf-8"))
    top = {(a.name if isinstance(n, ast.Import) else n.module).split(".")[0]
           for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))
           for a in n.names}
    assert top <= set(sys.stdlib_module_names) | {"__future__"}
