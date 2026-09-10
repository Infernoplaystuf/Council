"""
Failures are SHOWN — not rounded to zero, not printed to a console nobody reads.

The measured bug: Barbie's "Scan for bad timings" said "0 bad frames" when no
folder had been chosen (Path("") is the working directory, which holds no
frames), when the folder was empty, and when numpy was missing. The reason
sat in FolderReport.errors, which nothing displayed, and the generated handler
only printed exceptions. A false zero is worse than a crash: the operator
writes it in the log.

Now: frame_timing raises when nothing could be scanned; the generated handler
clears every port it fills and shows the message in the window; the live view
says in the panel why it is empty; and script links are validated at Generate.

Run:  python -m pytest tests/test_gui_errors.py -q
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import frame_timing as ft          # noqa: E402
import gui_emit as ge              # noqa: E402
import gui_layout as gl            # noqa: E402
import gui_shapes as gs            # noqa: E402
import gui_spec as gsp             # noqa: E402

Image = pytest.importorskip("PIL.Image")


# ============================================================
# frame_timing: an answer only when something was scanned
# ============================================================

@pytest.fixture
def frames(tmp_path):
    d = tmp_path / "frames"
    d.mkdir()
    for i in range(3):
        Image.new("L", (40, 30), 200).save(d / f"f_{i}.png")
    return d


@pytest.mark.parametrize("fn", [ft.scan_report, ft.count_bad_frames,
                                ft.list_bad_frames])
def test_no_folder_chosen_raises_instead_of_scanning_the_cwd(fn, tmp_path,
                                                             monkeypatch):
    """MEASURED: scan_report('') -> {'count': 0, ... '0 of 0 frames ...'}."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="no folder chosen"):
        fn("")


@pytest.mark.parametrize("fn", [ft.scan_report, ft.count_bad_frames,
                                ft.list_bad_frames])
def test_an_empty_folder_raises(fn, tmp_path):
    with pytest.raises(RuntimeError, match="no images in"):
        fn(str(tmp_path))


def test_not_a_folder_raises(tmp_path):
    with pytest.raises(RuntimeError, match="is not a folder"):
        ft.scan_report(str(tmp_path / "nope"))


def test_missing_numpy_raises_and_says_so(frames, monkeypatch):
    monkeypatch.setattr(ft, "_NUMPY", False)
    with pytest.raises(RuntimeError, match="numpy is not installed"):
        ft.scan_report(str(frames))


def test_a_real_scan_still_answers(frames):
    r = ft.scan_report(str(frames))
    assert r["count"] == 0 and r["names"] == []     # a TRUE zero this time


def test_one_unreadable_frame_does_not_abort_the_scan(frames):
    (frames / "broken.png").write_bytes(b"not an image")
    assert ft.count_bad_frames(str(frames)) == 0


def test_every_frame_unreadable_raises(tmp_path):
    for i in range(2):
        (tmp_path / f"x{i}.png").write_bytes(b"junk")
    with pytest.raises(RuntimeError, match="none of the 2 images"):
        ft.scan_report(str(tmp_path))


def test_classify_folder_is_still_a_report_that_never_raises(tmp_path):
    rep = ft.classify_folder("")
    assert rep.total == 0 and "no folder chosen" in rep.errors[0]


# ============================================================
# Ports: clear() shows nothing — never a zero
# ============================================================

@pytest.fixture(scope="module")
def rt():
    ns: dict = {}
    exec(compile(ge.WIDGETS_PY, "<widgets>", "exec"), ns)
    exec(compile(ge.PORTS_RUNTIME, "<ports>", "exec"), ns)
    return ns


def test_clear_blanks_text_and_never_writes_a_zero(rt, tk_root):
    import tkinter as tk
    top = tk.Toplevel(tk_root)
    try:
        sv, iv = tk.StringVar(top, "10"), tk.IntVar(top, 7)
        ent, spin = tk.Entry(top), tk.Spinbox(top)
        P = rt["_VarPort"]
        p_str = P("s", ent, var=sv, option="textvariable", type="str",
                  direction="io")
        p_int = P("i", spin, var=iv, option="textvariable", type="int",
                  direction="io")
        p_str.clear()
        p_int.clear()
        assert sv.get() == ""
        assert iv.get() == 7, "an IntVar has no blank state; clearing must " \
                              "not write 0 into it"

        lb = tk.Listbox(top)
        lp = rt["_ListPort"]("l", lb, direction="io", type="list")
        lp.set(["a", "b"])
        lp.clear()
        assert lb.size() == 0

        ic = rt["ImageCanvas"](top)
        ic.pack()
        top.update()
        img = rt["_ProxyPort"]("v", ic, writer="set_image", type="image",
                               direction="out")
        img.set(Image.new("RGB", (10, 10)))
        img.clear()
        assert ic._base is None

        sb = rt["StatusBar"](top)
        st = rt["_ProxyPort"]("st", sb, writer="set", type="str",
                              direction="out")
        st.set("saved 3 frames")
        st.clear()          # a status line is a record; failure keeps it
    finally:
        top.destroy()


def test_the_panel_says_why_it_is_empty(rt, tk_root):
    import tkinter as tk
    top = tk.Toplevel(tk_root)
    top.geometry("300x200")
    try:
        ic = rt["ImageCanvas"](top)
        ic.pack(fill="both", expand=True)
        top.update()
        ic.show_message("Pillow is not installed")
        msg = ic.canvas.find_withtag("message")
        assert msg and ic.canvas.itemcget(msg[0], "text") == \
            "Pillow is not installed"
        ic.set_image(Image.new("RGB", (10, 10)))
        assert not ic.canvas.find_withtag("message"), \
            "an image arriving must replace the message"
    finally:
        top.destroy()


# ============================================================
# The generated app, pressed like a user would
# ============================================================

DRIVER = textwrap.dedent('''
    import json, os, sys, time
    from pathlib import Path
    FRAMES, EMPTY = sys.argv[1], sys.argv[2]
    main_py = Path.cwd() / "main.py"
    boot = main_py.read_text(encoding="utf-8").split("from app import main")[0]
    exec(compile(boot, str(main_py), "exec"), {"__file__": str(main_py)})
    import tkinter as tk
    from app import App
    root = tk.Tk(); app = App(root); app.pack()
    def pump(ms):
        end = time.time() + ms / 1000
        while time.time() < end:
            root.update(); time.sleep(0.005)
    pump(300)
    p, out = app.ports, {}
    def scan(key):
        app.btn_scan_for_bad_timings.invoke(); pump(1200)
        out[key] = [p.bad_count.get(), app.lst_listbox.size()]
    scan("no_folder")
    p.capture_folder.set(FRAMES); pump(800); scan("frames")
    p.capture_folder.set(EMPTY); pump(800); scan("empty_after")
    app.btn_save_cropped_frames.invoke(); pump(500)
    out["save_status"] = p.save_status.get()
    print("__OUT__" + json.dumps(out))
    root.destroy()
''')


def test_the_scan_never_shows_a_false_zero(tmp_path, frames):
    import run_example_gui as rex
    empty = tmp_path / "empty"
    empty.mkdir()
    pdir = rex.build("barbie_capture_v2", project="err", vault_dir=tmp_path / "v")
    drv = tmp_path / "drive.py"
    drv.write_text(DRIVER, encoding="utf-8")
    r = subprocess.run([sys.executable, str(drv), str(frames), str(empty)],
                       cwd=str(pdir), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120,
                       env=dict(os.environ, COUNCIL_NO_DIALOGS="1"))
    assert r.returncode == 0, r.stderr[-1500:]
    out = json.loads(next(l for l in r.stdout.splitlines()
                          if l.startswith("__OUT__"))[len("__OUT__"):])
    assert out["no_folder"] == ["", 0], "no folder must not read as 0"
    assert out["frames"] == ["0", 0]              # a real, TRUE zero
    assert out["empty_after"] == ["", 0], "an empty folder must not read as 0"
    # every failure was reported, by the button's own name
    assert "Scan for bad timings failed: no folder chosen" in r.stderr
    assert "Scan for bad timings failed: no images in" in r.stderr
    # a function returning {'error': ...} is a failure too: Save with no ROI
    assert out["save_status"] == ""
    assert "Save cropped frames failed:" in r.stderr and "no ROI" in r.stderr


# ============================================================
# Script links are checked at Generate
# ============================================================

def _linked(script, requires=()):
    ent = gs.new_shape("entry", 0, 0); ent.id, ent.z = "e", 1
    ent.port = {"name": "folder"}
    out = gs.new_shape("entry", 0, 40); out.id, out.z = "o", 2
    out.port = {"name": "count"}
    btn = gs.new_shape("button", 0, 80); btn.id, btn.z = "b", 3
    btn.label = "Go"
    btn.script = script
    shapes = [ent, out, btn]
    return gsp.validate(gsp.build(shapes, gl.infer(shapes, 400, 300),
                                  project="s", requires=list(requires)))


GOOD = {"module": "frame_timing", "function": "count_bad_frames",
        "inputs": ["folder"], "output": "count"}


def test_a_correct_script_link_validates():
    ok, errs = _linked(GOOD)
    assert ok, errs


@pytest.mark.parametrize("patch,fragment", [
    ({"function": "count_bad_frame"}, "has no function 'count_bad_frame'"),
    ({"inputs": ["nope"]}, "script input 'nope' names no port"),
    ({"output": "nope"}, "script output 'nope' names no port"),
    ({"module": "some_vendor_sdk"}, "add it to the project's requires"),
    ({"module": "subprocess"}, "not allowed"),
    ({"function": "not valid"}, "no valid function"),
])
def test_a_broken_script_link_is_an_error_at_generate(patch, fragment):
    """Each of these used to surface only when the button was pressed, as a
    print to a console."""
    ok, errs = _linked({**GOOD, **patch})
    assert not ok and any(fragment in e for e in errs), errs


def test_a_re_exported_function_counts_as_defined(tmp_path):
    """`from .core import scan` in a module really does offer `scan`; only
    counting defs would reject a working link as 'has no function'."""
    (tmp_path / "wrapper.py").write_text(
        "from frame_timing import count_bad_frames as tally\n"
        "import os.path\n\n"
        "def local():\n    pass\n", encoding="utf-8")
    defs = gsp._top_level_defs("wrapper", root=tmp_path)
    assert {"tally", "local", "os"} <= defs


def test_a_star_import_makes_the_function_check_step_aside(tmp_path):
    (tmp_path / "starry.py").write_text("from frame_timing import *\n",
                                        encoding="utf-8")
    assert gsp._top_level_defs("starry", root=tmp_path) is None


def test_a_declared_vendor_module_is_allowed_and_not_parsed():
    """A vendor SDK is not a file in the app root, so its functions cannot be
    checked without importing it — and validation never imports."""
    ok, errs = _linked({**GOOD, "module": "pypylon"}, requires=["pypylon"])
    assert ok, errs
