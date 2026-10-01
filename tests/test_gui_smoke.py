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


def test_the_child_half_imports_nothing_from_the_council():
    """It runs under whichever interpreter the project uses, which may have
    none of the Council's packages."""
    import ast
    tree = ast.parse((ROOT / "gui_smoke.py").read_text(encoding="utf-8"))
    top = {(a.name if isinstance(n, ast.Import) else n.module).split(".")[0]
           for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))
           for a in n.names}
    assert top <= set(sys.stdlib_module_names) | {"__future__"}
