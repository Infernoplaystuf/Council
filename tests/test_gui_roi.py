"""
Region of interest on the live image, crop-on-save, and multi-word fonts.

Barbie Capture v2 lets the user box the part of the frame that matters: Draw
ROI arms a drag, Apply ROI crops and zooms the view to the box (and keeps it
cropped on every new frame), and Save writes each frame cropped to the box.

The last test builds the v2 example and drives the GENERATED app in its own
process, pressing its real buttons. That is deliberate: the retrieval layer in
this repo passed every component test for six months while the path the app
actually used returned nothing, because no test ever called that path.

Run:  python -m pytest tests/test_gui_roi.py -q
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace as Ev

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import frame_roi as fr            # noqa: E402
import gui_colors as gcol         # noqa: E402
import gui_emit as ge             # noqa: E402
import gui_layout as gl           # noqa: E402
import gui_shapes as gs           # noqa: E402
import gui_spec as gsp            # noqa: E402

Image = pytest.importorskip("PIL.Image")


# ============================================================
# Fonts with spaces in their names
# ============================================================

@pytest.mark.parametrize("given,want", [
    ("Segoe UI 12", "{Segoe UI} 12"),
    ("Segoe UI 20 bold", "{Segoe UI} 20 bold"),
    ("Times New Roman 11 italic", "{Times New Roman} 11 italic"),
    ("Segoe UI", "{Segoe UI}"),
    ("Magneto 12", "Magneto 12"),           # one word: untouched
    ("Arial", "Arial"),
    ("{Segoe UI} 12", "{Segoe UI} 12"),     # already quoted: untouched
    ("", ""),
])
def test_tk_font_quotes_multi_word_families(given, want):
    assert gcol.tk_font(given) == want


def test_a_multi_word_font_no_longer_kills_the_app(tk_root):
    """MEASURED FAILURE: TclError: expected integer but got "UI" — raised at
    widget construction, so the generated app died before its window."""
    import tkinter as tk
    from tkinter import ttk
    with pytest.raises(tk.TclError):
        ttk.Label(tk_root, text="x", font="Segoe UI 12")
    lbl = ttk.Label(tk_root, text="x", font=gcol.tk_font("Segoe UI 12"))
    lbl.destroy()


def test_emit_writes_the_quoted_font():
    s = gs.new_shape("label", 0, 0)
    s.id, s.label, s.font = "l1", "Hello", "Segoe UI 11 bold"
    spec = gsp.build([s], gl.infer([s], 400, 200), project="f")
    src = ge.emit_main_ui(spec)
    assert '"{Segoe UI} 11 bold"' in src
    assert '"Segoe UI 11 bold"' not in src


# ============================================================
# The canvas
# ============================================================

@pytest.fixture(scope="module")
def rt():
    ns: dict = {}
    exec(compile(ge.WIDGETS_PY, "<widgets>", "exec"), ns)
    exec(compile(ge.PORTS_RUNTIME, "<ports>", "exec"), ns)
    return ns


@pytest.fixture
def canvas(rt, tk_root):
    import tkinter as tk
    top = tk.Toplevel(tk_root)
    top.geometry("500x400")
    ic = rt["ImageCanvas"](top, roi=True)
    ic.pack(fill="both", expand=True)
    top.update()
    ic.set_image(Image.new("RGB", (200, 100), "grey"))
    top.update()
    try:
        yield ic
    finally:
        top.destroy()


def _draw(ic, box):
    """Draw ``box`` (image pixels) through the canvas's own mouse handlers."""
    x, y, w, h = box
    x0, y0 = ic._to_canvas(x, y)
    x1, y1 = ic._to_canvas(x + w, y + h)
    ic.arm_roi()
    ic._press(Ev(x=x0, y=y0))
    ic._drag(Ev(x=(x0 + x1) / 2, y=(y0 + y1) / 2))
    ic._drag(Ev(x=x1, y=y1))
    ic._release(Ev(x=x1, y=y1))


def test_a_drag_draws_the_box_in_image_pixels(canvas):
    _draw(canvas, (20, 10, 60, 40))
    assert canvas.get_roi() == (20, 10, 60, 40)
    assert canvas.canvas.find_withtag("roi_box"), "the box is not on screen"


def test_apply_crops_the_view_and_zooms_to_it(canvas):
    _draw(canvas, (20, 10, 60, 40))
    canvas.apply_roi()
    assert canvas._view.size == (60, 40)
    # zoomed to FIT the crop, not left at the whole-frame scale
    cw = canvas.canvas.winfo_width()
    assert canvas._view.size[0] * canvas._scale > 0.9 * cw or \
        canvas._view.size[1] * canvas._scale > 0.9 * canvas.canvas.winfo_height()


def test_an_applied_roi_survives_every_new_frame(canvas):
    """A scrubbed or live sequence must stay zoomed on the region, not snap
    back to the whole frame each time a new image arrives."""
    _draw(canvas, (20, 10, 60, 40))
    canvas.apply_roi()
    for shade in ("red", "blue", "white"):
        canvas.set_image(Image.new("RGB", (200, 100), shade))
        assert canvas._view.size == (60, 40)


def test_clear_returns_to_the_whole_frame(canvas):
    _draw(canvas, (20, 10, 60, 40))
    canvas.apply_roi()
    canvas.clear_roi()
    assert canvas.get_roi() is None
    assert canvas._view.size == (200, 100)


def test_a_click_without_a_drag_keeps_the_existing_box(canvas):
    _draw(canvas, (20, 10, 60, 40))
    x, y = canvas._to_canvas(100, 50)
    canvas.arm_roi()
    canvas._press(Ev(x=x, y=y))
    canvas._release(Ev(x=x, y=y))
    assert canvas.get_roi() == (20, 10, 60, 40)


def test_drawing_is_disabled_while_a_crop_is_applied(canvas):
    """A box drawn on a zoomed crop is ambiguous — Clear first."""
    _draw(canvas, (20, 10, 60, 40))
    canvas.apply_roi()
    assert "disabled" in canvas._btn_draw.state()
    canvas.arm_roi()
    assert canvas._roi_armed is False


def test_an_oversized_box_is_reported_clamped_not_as_typed(canvas):
    """A 400x300 box on a 200x100 frame shows 200 x 100 — never a size whose
    pixels do not exist."""
    canvas.set_roi((0, 0, 400, 300))
    canvas.apply_roi()
    assert canvas._view.size == (200, 100)
    note = canvas._roi_note.cget("text")
    assert "200 x 100" in note and "clamped" in note


def test_roi_is_off_by_default_so_drag_still_pans(rt, tk_root):
    import tkinter as tk
    top = tk.Toplevel(tk_root)
    try:
        ic = rt["ImageCanvas"](top)
        ic.pack(fill="both", expand=True)
        top.update()
        ic.set_image(Image.new("RGB", (50, 50)))
        ox = ic._ox
        ic._press(Ev(x=10, y=10)); ic._drag(Ev(x=30, y=10)); ic._release(Ev(x=30, y=10))
        assert ic.roi_enabled is False and ic.get_roi() is None
        assert ic._ox == pytest.approx(ox + 20)
    finally:
        top.destroy()


# ============================================================
# The ROI port, both ways
# ============================================================

def test_the_box_and_the_entry_stay_in_sync_both_ways(rt, tk_root, tmp_path):
    import tkinter as tk
    top = tk.Toplevel(tk_root)
    top.geometry("500x420")
    try:
        ic = rt["ImageCanvas"](top, roi=True)
        scrub = rt["Scrubber"](top, from_=0, to=0)
        pick = rt["FilePicker"](top, mode="folder")
        ent = tk.Entry(top)
        for w in (pick, scrub, ic, ent):
            w.pack(fill="both", expand=True)
        top.update()
        var = tk.StringVar(top)
        ent.configure(textvariable=var)
        P = rt["_VarPort"]
        fb = rt["_FrameBrowser"](
            P("f", pick, var=pick.var, option="", type="str", direction="io",
              default=None, deep=False),
            P("i", scrub, var=scrub.var, option="", type="int", direction="io",
              default=None, deep=False),
            rt["_ProxyPort"]("v", ic, writer="set_image", type="image",
                             direction="out"),
            roi_port=P("roi", ent, var=var, option="", type="str",
                       direction="io", default=None, deep=False))
        # The browser defers its first folder load to idle; with no folder
        # that load CLEARS the canvas. Let it run before setting the image.
        top.update()
        ic.set_image(Image.new("RGB", (200, 100)))
        top.update()

        _draw(ic, (20, 10, 60, 40))
        top.update()
        assert var.get() == "20, 10, 60, 40", "drawing did not reach the entry"

        var.set("5, 6, 70, 30")
        top.update()
        assert ic.get_roi() == (5, 6, 70, 30), "typing did not move the box"

        var.set("5, 6, 7")                      # half-typed: leave it alone
        top.update()
        assert ic.get_roi() == (5, 6, 70, 30)

        var.set("")
        top.update()
        assert ic.get_roi() is None
        assert fb is not None
    finally:
        top.destroy()


# ============================================================
# frame_roi — crop-on-save
# ============================================================

@pytest.fixture
def frames(tmp_path):
    src = tmp_path / "capture"
    src.mkdir()
    for i in (1, 2, 10):
        Image.new("RGB", (120, 80), (i * 20, 0, 0)).save(src / f"f_{i}.png")
    out = tmp_path / "saved"
    out.mkdir()
    return src, out


@pytest.mark.parametrize("given,want", [
    ("12, 40, 300, 200", (12, 40, 300, 200)),
    ((1, 2, 3, 4), (1, 2, 3, 4)),
    ("12 40 300 200", (12, 40, 300, 200)),
    ("1, 2, 3", None),
    ("", None),
    (None, None),
    ("-5, 0, 10, 10", None),
    ("0, 0, 1, 10", None),
])
def test_parse_roi(given, want):
    assert fr.parse_roi(given) == want


def test_save_writes_every_frame_cropped_into_a_new_folder(frames):
    src, out = frames
    r = fr.export_roi(str(src), "10, 5, 50, 30", str(out), stamp="T")
    assert r["error"] == "" and r["count"] == 3
    got = Path(r["out"])
    assert got.parent == out and got.name == "roi_10_5_50x30_T"
    sizes = {Image.open(p).size for p in got.iterdir()}
    assert sizes == {(50, 30)}
    assert "Saved 3 cropped frames" in r["summary"]


def test_save_never_touches_the_capture_folder(frames):
    src, _ = frames
    before = sorted(p.name for p in src.iterdir())
    for target in (src, src / "inside"):
        r = fr.export_roi(str(src), "0, 0, 10, 10", str(target))
        assert r["count"] == 0 and "outside the capture folder" in r["error"]
        assert r["summary"].startswith("Not saved")
    assert sorted(p.name for p in src.iterdir()) == before
    assert not (src / "inside").exists()


def test_two_saves_never_merge_or_overwrite(frames):
    src, out = frames
    a = fr.export_roi(str(src), "0, 0, 20, 20", str(out), stamp="SAME")
    b = fr.export_roi(str(src), "0, 0, 20, 20", str(out), stamp="SAME")
    assert a["out"] != b["out"]
    assert Path(b["out"]).name.endswith("_2")


def test_an_oversized_box_is_reported_as_what_was_written(frames):
    """MEASURED BUG, fixed: the summary and folder said 400 x 300 while every
    file on disk was clamped smaller."""
    src, out = frames
    r = fr.export_roi(str(src), "100, 50, 400, 300", str(out), stamp="C")
    assert Path(r["out"]).name == "roi_100_50_20x30_C"
    assert "20 x 30 at 100, 50" in r["summary"] and "clamped" in r["summary"]
    assert {Image.open(p).size for p in Path(r["out"]).iterdir()} == {(20, 30)}


def test_a_box_outside_every_frame_is_an_error_not_a_zero(frames):
    src, out = frames
    r = fr.export_roi(str(src), "500, 500, 10, 10", str(out))
    assert r["count"] == 0 and r["error"]
    assert r["summary"].startswith("Not saved")
    assert not list(out.iterdir()), "an empty output folder was left behind"


@pytest.mark.parametrize("folder,roi,out_folder,fragment", [
    ("", "0, 0, 5, 5", "x", "no capture folder"),
    ("CAPTURE", "", "x", "no ROI"),
    ("CAPTURE", "0, 0, 5, 5", "", "choose a folder"),
])
def test_every_missing_input_says_what_is_missing(frames, folder, roi,
                                                   out_folder, fragment):
    src, out = frames
    folder = str(src) if folder == "CAPTURE" else folder
    out_folder = str(out) if out_folder == "x" else out_folder
    r = fr.export_roi(folder, roi, out_folder)
    assert fragment in r["error"] and r["summary"].startswith("Not saved")


def test_missing_pillow_is_reported_not_silent(frames, monkeypatch):
    src, out = frames
    monkeypatch.setitem(sys.modules, "PIL", None)
    r = fr.export_roi(str(src), "0, 0, 5, 5", str(out))
    assert "Pillow" in r["error"] and r["summary"].startswith("Not saved")


def test_sixteen_bit_frames_stay_sixteen_bit(tmp_path):
    src, out = tmp_path / "c", tmp_path / "o"
    src.mkdir(); out.mkdir()
    Image.new("I;16", (40, 40), 4096).save(src / "a.tif")
    r = fr.export_roi(str(src), "5, 5, 10, 10", str(out))
    got = Image.open(next(Path(r["out"]).iterdir()))
    assert got.mode == "I;16" and got.getpixel((0, 0)) == 4096


# ============================================================
# The declaration
# ============================================================

def _roi_scene(*, roi_prop=True, roi_kind="entry", roi_port="roi"):
    p = gs.new_shape("file_picker", 0, 0); p.id, p.z = "p", 1
    p.props = {"mode": "folder"}; p.port = {"name": "capture_folder"}
    c = gs.new_shape("image_canvas", 0, 60); c.id, c.z = "c", 2
    c.w, c.h = 400, 300; c.port = {"name": "live_view"}
    c.props = {"roi": roi_prop}
    e = gs.new_shape(roi_kind, 420, 60); e.id, e.z = "e", 3
    e.port = {"name": "roi"} if roi_kind != "label" else {"name": "roi"}
    s = gs.new_shape("scrubber", 0, 380); s.id, s.z = "s", 4
    s.w, s.h = 400, 40; s.port = {"name": "frame"}
    s.drives = {"folder": "capture_folder", "target": "live_view",
                "roi": roi_port}
    return [p, c, e, s]


def _validate(shapes):
    return gsp.validate(gsp.build(shapes, gl.infer(shapes, 1280, 800),
                                  project="r"))


def test_a_correct_roi_link_validates_and_emits():
    shapes = _roi_scene()
    ok, errs = _validate(shapes)
    assert ok, errs
    spec = gsp.build(shapes, gl.infer(shapes, 1280, 800), project="r")
    assert "roi_port=self.roi" in ge.emit_ports(spec)
    assert "roi=True" in ge.emit_main_ui(spec)


@pytest.mark.parametrize("kw,fragment", [
    (dict(roi_port="nope"), "drives.roi names no port"),
    (dict(roi_kind="spinbox"), "drives.roi must be an entry"),
    (dict(roi_prop=False), "has roi off"),
])
def test_a_broken_roi_link_blocks_generation(kw, fragment):
    ok, errs = _validate(_roi_scene(**kw))
    assert not ok and any(fragment in e for e in errs), errs


# ============================================================
# The whole thing, as a user would run it
# ============================================================

DRIVER = textwrap.dedent('''
    import json, sys, time
    from pathlib import Path
    from types import SimpleNamespace as Ev
    FOLDER, OUT = sys.argv[1], sys.argv[2]
    main_py = Path.cwd() / "main.py"
    boot = main_py.read_text(encoding="utf-8").split("from app import main")[0]
    exec(compile(boot, str(main_py), "exec"), {"__file__": str(main_py)})
    import tkinter as tk
    from app import App
    root = tk.Tk(); root.geometry("1280x820")
    app = App(root); app.pack(fill="both", expand=True)
    def pump(ms):
        end = time.time() + ms / 1000
        while time.time() < end:
            root.update(); time.sleep(0.005)
    pump(300)
    p, cv = app.ports, app.img_image_canvas
    p.capture_folder.set(FOLDER); pump(700)
    cv.arm_roi()
    x0, y0 = cv._to_canvas(10, 5); x1, y1 = cv._to_canvas(60, 35)
    cv._press(Ev(x=x0, y=y0)); cv._drag(Ev(x=x1, y=y1)); cv._release(Ev(x=x1, y=y1))
    cv.apply_roi(); pump(200)
    p.output_folder.set(OUT)
    app.btn_save_cropped_frames.invoke(); pump(800)
    print(json.dumps({"roi": cv.get_roi(), "entry": p.roi.get(),
                      "view": list(cv._view.size), "status": p.save_status.get()}))
    root.destroy()
''')


def test_the_generated_v2_app_crops_and_saves_end_to_end(tmp_path, frames):
    import run_example_gui as rex
    src, out = frames
    pdir = rex.build("barbie_capture_v2", project="t_v2", vault_dir=tmp_path / "v")
    drv = tmp_path / "drive.py"
    drv.write_text(DRIVER, encoding="utf-8")
    proc = subprocess.run([sys.executable, str(drv), str(src), str(out)],
                          cwd=str(pdir), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)
    if proc.returncode != 0 and "display" in proc.stderr.lower():
        pytest.skip("no display")
    assert proc.returncode == 0, proc.stderr[-2000:]
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert got["roi"] == [10, 5, 50, 30]
    assert got["entry"] == "10, 5, 50, 30"
    assert got["view"] == [50, 30]
    assert got["status"].startswith("Saved 3 cropped frames"), got["status"]
    saved = next(out.iterdir())
    assert {Image.open(f).size for f in saved.iterdir()} == {(50, 30)}


def test_the_generated_v2_project_passes_its_own_policy(tmp_path):
    import gui_policy as pol
    import run_example_gui as rex
    pdir = rex.build("barbie_capture_v2", project="t_pol", vault_dir=tmp_path)
    files = sorted((pdir / "ui").glob("*.py")) + [
        pdir / n for n in ("app.py", "handlers.py", "main.py")]
    ok, errs = pol.validate_project(files, "linked")
    assert ok, errs
