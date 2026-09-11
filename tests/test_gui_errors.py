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

        # A status bar and a progress bar have an honest empty state. Left as
        # they were, the previous folder's answer sat beside the new folder
        # after its scan failed (adversarial review, 2026-09).
        sb = rt["StatusBar"](top)
        st = rt["_ProxyPort"]("st", sb, writer="set", type="str",
                              direction="out")
        st.set("3 of 5 frames captured on a bad timing")
        st.clear()
        assert sb.message.cget("text") == ""
        from tkinter import ttk
        dv = tk.DoubleVar(top, 0.0)
        pb = rt["_VarPort"]("pb", ttk.Progressbar(top), var=dv,
                            option="variable", type="float", direction="out")
        pb.set(3.0)
        pb.clear()
        assert dv.get() == 0.0

        # ...and a cleared number box reads back as None, not 0 — the same
        # false zero clear() keeps off the screen, handed to its next reader
        p_str.set("")
        num = P("n", tk.Entry(top), var=tk.StringVar(top, "7"),
                option="textvariable", type="int", direction="io")
        num.clear()
        assert num.get() is None
    finally:
        top.destroy()


@pytest.mark.parametrize("raw,t,want", [
    ("", "int", None), ("  ", "float", None), ("abc", "int", None),
    ("7", "int", 7), ("3.0", "int", 3), ("2.5", "float", 2.5),
    (4, "int", 4), (4, "float", 4.0), ("inf", "int", None),
])
def test_coerce_never_invents_a_number(rt, raw, t, want):
    assert rt["_coerce"](raw, t) == want


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


# ============================================================
# Links that validated and then failed on every press
# (adversarial review, 2026-09)
# ============================================================

@pytest.mark.parametrize("patch,fragment", [
    # the function written into the module path — an easy model mistake
    ({"module": "frame_timing.count_bad_frames"}, "is not a module"),
    # a constant and an imported module are names, not functions
    ({"function": "IMAGE_SUFFIXES"}, "not a function"),
    ({"function": "os"}, "not a function"),
    # malformed shapes are reported, not raised
    ({"outputs": ["count"], "output": ""}, "must map port name"),
    ({"outputs": "count", "output": ""}, "must map port name"),
    ({"inputs": "folder"}, "must be a list"),
    ({"inputs": 3}, "must be a list"),
])
def test_a_link_that_can_never_work_is_refused(patch, fragment):
    ok, errs = _linked({**GOOD, **patch})
    assert not ok and any(fragment in e for e in errs), errs


def _rig(src_kind, dst_kind, carrier="button"):
    a = gs.new_shape(src_kind, 0, 0); a.id, a.z, a.port = "a", 1, {"name": "src"}
    b = gs.new_shape(dst_kind, 0, 60); b.id, b.z, b.port = "b", 2, {"name": "dst"}
    c = gs.new_shape(carrier, 0, 120); c.id, c.z, c.label = "c", 3, "Go"
    c.script = {"module": "frame_timing", "function": "count_bad_frames",
                "inputs": ["src"], "output": "dst"}
    shapes = [a, b, c]
    return gsp.build(shapes, gl.infer(shapes, 400, 300), project="r")


@pytest.mark.parametrize("src,dst,carrier,fragment", [
    ("status_bar", "entry", "button", "no value to read"),
    ("image_canvas", "entry", "button", "no value to read"),
    ("entry", "button", "button", "cannot show a result"),
    ("entry", "chart_panel", "button", "cannot show a result"),
    ("entry", "label", "entry", "put it on a button"),
])
def test_ports_that_do_not_fit_the_link_are_refused(src, dst, carrier,
                                                    fragment):
    """MEASURED: output to a button wrote the count, then raised setting the
    button — and reported a SUCCESSFUL scan as a failure."""
    ok, errs = gsp.validate(_rig(src, dst, carrier))
    assert not ok and any(fragment in e for e in errs), errs


def test_an_output_with_no_blank_state_is_warned_about():
    warns = gsp.script_warnings(_rig("entry", "scale"))
    assert warns and "no blank state" in warns[0]
    assert gsp.script_warnings(_rig("entry", "label")) == []


def test_a_function_offered_inside_try_or_if_counts(tmp_path):
    """`try: from fast import scan / except ImportError: def scan()` is the
    standard way to offer a function; reading only the top level rejected
    the working link."""
    (tmp_path / "compat.py").write_text(
        "try:\n    from fast_xyz import scan\nexcept ImportError:\n"
        "    def scan(folder):\n        return 0\n"
        "if True:\n    def other():\n        pass\n", encoding="utf-8")
    defs = gsp._top_level_defs("compat", root=tmp_path)
    assert {"scan", "other"} <= defs


# ============================================================
# Stubs an older Council wrote are upgraded — unedited ones only
# ============================================================

def _built(tmp_path, name):
    import run_example_gui as rex
    return rex.build("barbie_capture_v2", project=name,
                     vault_dir=tmp_path / "v")


def _regenerate(pdir):
    """What the designer's Generate does, minus the model call."""
    import gui_projects as gpj
    proj = gs.load_gspec(pdir / gpj.GSPEC_NAME)
    man = gpj.load_manifest(pdir)
    spec = gsp.build(proj.shapes, gl.infer(proj.shapes, proj.canvas.w,
                                           proj.canvas.h),
                     registry=man.widget_names,
                     port_registry=man.port_names, project=pdir.name,
                     mode=man.mode, title=proj.window.title,
                     root_bg=proj.window.bg, root_fg=proj.window.fg,
                     root_font=proj.window.font, requires=proj.requires)
    return spec, ge.emit(spec, pdir)


@pytest.mark.parametrize("era", [1, 2])
def test_an_unedited_old_stub_is_upgraded_on_generate(tmp_path, era):
    """MEASURED on all four of the user's projects: the old stub only
    print()ed a failure, so once scans raised instead of returning zeros, an
    empty folder showed the PREVIOUS folder's count and file names."""
    pdir = _built(tmp_path, f"old{era}")
    spec, _ = _regenerate(pdir)
    h = "on_btn_scan_for_bad_timings"
    script, title = ge._script_for(spec, h), ge._title_for(spec, h)
    old = ge._legacy_stubs(h, script, title)[era].rstrip("\n")
    hp = pdir / "handlers.py"
    src = hp.read_text(encoding="utf-8")
    new = ge.handler_stub(h, script, title).strip("\n")
    assert new in src
    hp.write_text(src.replace(new, old), encoding="utf-8")

    _, res = _regenerate(pdir)
    after = hp.read_text(encoding="utf-8")
    assert res.handlers_upgraded == [h]
    assert new in after and old not in after
    assert "def on_close" in after, "the rest of handlers.py must survive"
    compile(after, "handlers.py", "exec")


def test_an_edited_old_stub_is_left_alone_and_named(tmp_path):
    pdir = _built(tmp_path, "edited")
    spec, _ = _regenerate(pdir)
    h = "on_btn_scan_for_bad_timings"
    script, title = ge._script_for(spec, h), ge._title_for(spec, h)
    old = ge._legacy_stubs(h, script, title)[1].rstrip("\n")
    edited = old.replace("failed: {exc!r}", "FAILED (mine): {exc!r}")
    hp = pdir / "handlers.py"
    src = hp.read_text(encoding="utf-8")
    hp.write_text(src.replace(ge.handler_stub(h, script, title).strip("\n"),
                              edited), encoding="utf-8")

    _, res = _regenerate(pdir)
    assert res.handlers_upgraded == []
    assert edited in hp.read_text(encoding="utf-8")
    assert any(h in w and "report_error" in w for w in res.warnings)


def test_a_line_added_to_an_old_stub_counts_as_an_edit(tmp_path):
    """A substring match would have upgraded this and orphaned the line."""
    pdir = _built(tmp_path, "appended")
    spec, _ = _regenerate(pdir)
    h = "on_btn_scan_for_bad_timings"
    script, title = ge._script_for(spec, h), ge._title_for(spec, h)
    old = ge._legacy_stubs(h, script, title)[1].rstrip("\n")
    hp = pdir / "handlers.py"
    src = hp.read_text(encoding="utf-8")
    hp.write_text(src.replace(ge.handler_stub(h, script, title).strip("\n"),
                              old + "\n            self.bell()"),
                  encoding="utf-8")
    _, res = _regenerate(pdir)
    assert res.handlers_upgraded == []
    assert "self.bell()" in hp.read_text(encoding="utf-8")


def test_a_label_with_a_newline_still_generates_valid_python(tmp_path):
    """A newline written raw into the report_error title ended the string
    literal, and handlers.py — never rewritten — stayed broken."""
    ent = gs.new_shape("entry", 0, 0); ent.id, ent.z = "e", 1
    ent.port = {"name": "folder"}
    out = gs.new_shape("entry", 0, 40); out.id, out.z = "o", 2
    out.port = {"name": "count"}
    btn = gs.new_shape("button", 0, 80); btn.id, btn.z = "b", 3
    btn.label = "Scan\nfolder"
    btn.script = dict(GOOD)
    shapes = [ent, out, btn]
    spec = gsp.build(shapes, gl.infer(shapes, 400, 300), project="nl")
    ge.emit(spec, tmp_path)
    for f in ("handlers.py", "ui/main_ui.py", "ui/ports.py", "main.py"):
        compile((tmp_path / f).read_text(encoding="utf-8"), f, "exec")


# ============================================================
# The error still reaches the user when something else is wrong too
# ============================================================

PRESS = textwrap.dedent('''
    import json, os, sys, time
    from pathlib import Path
    main_py = Path.cwd() / "main.py"
    boot = main_py.read_text(encoding="utf-8").split("from app import main")[0]
    exec(compile(boot, str(main_py), "exec"), {"__file__": str(main_py)})
    import tkinter as tk
    from tkinter import messagebox
    shown = []
    messagebox.showerror = lambda title, msg, **k: shown.append([title, msg])
    from app import App
    root = tk.Tk(); app = App(root); app.pack()
    for _ in range(30):
        root.update(); time.sleep(0.01)
    real_err = sys.stderr
    if os.environ.get("NO_STDERR"):
        sys.stderr = None              # what pythonw gives a GUI app
    app.btn_scan_for_bad_timings.invoke()
    for _ in range(60):
        root.update(); time.sleep(0.01)
    sys.stderr = real_err
    print("__OUT__" + json.dumps(shown))
    root.destroy()
''')


def _press(pdir, tmp_path, **env):
    drv = tmp_path / "press.py"
    drv.write_text(PRESS, encoding="utf-8")
    e = {k: v for k, v in os.environ.items() if k != "COUNCIL_NO_DIALOGS"}
    e.update(env)
    r = subprocess.run([sys.executable, str(drv)], cwd=str(pdir),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120, env=e)
    assert r.returncode == 0, r.stderr[-1500:]
    return json.loads(next(l for l in r.stdout.splitlines()
                           if l.startswith("__OUT__"))[len("__OUT__"):])


def test_the_dialog_still_appears_under_pythonw(tmp_path):
    """MEASURED under pythonw.exe: sys.stderr is None, report_error raised
    on its first line, and no dialog appeared at all."""
    pdir = _built(tmp_path, "pyw")
    shown = _press(pdir, tmp_path, NO_STDERR="1")
    assert shown and "no folder chosen" in shown[0][1]


def test_a_port_renamed_under_the_handler_cannot_hide_the_error(tmp_path):
    """After a rename, handlers.py still names the old port. The old stub's
    clear() raised AttributeError inside the except, so report_error never
    ran and the failure vanished."""
    pdir = _built(tmp_path, "renamed")
    hp = pdir / "handlers.py"
    src = hp.read_text(encoding="utf-8")
    assert 'self.clear_ports("bad_count", "bad_list")' in src
    hp.write_text(src.replace('clear_ports("bad_count"',
                              'clear_ports("port_renamed_away"'),
                  encoding="utf-8")
    shown = _press(pdir, tmp_path)
    assert shown and "no folder chosen" in shown[0][1]
