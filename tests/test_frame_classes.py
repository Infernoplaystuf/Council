"""
Make classes, mark frames, train a very simple classifier — Barbie Capture v3.

A random forest over 32x32 greyscale thumbnails plus their row/column profiles.
Measured on the 120-frame sample capture: marking 3 bad-timing and 4 good
frames, then "Classify all frames", picked out exactly the 10 frames
frame_timing finds by a completely different method, with no false alarms.

Everything is kept in <vault>/classifiers/<name>/ — never beside the frames.
Tests point COUNCIL_VAULT_ROOT at a temp folder and build their own frames, so
they run on a fresh clone.

Run:  python -m pytest tests/test_frame_classes.py -q
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

import frame_classes as fc        # noqa: E402

np = pytest.importorskip("numpy")
Image = pytest.importorskip("PIL.Image")


def _good(path, shade):
    """Letterboxed: black bars top and bottom, a lit picture band between."""
    a = np.zeros((90, 160), np.uint8)
    a[20:70, :] = shade
    Image.fromarray(a).save(path)


def _bad(path, bar_row):
    """Bad timing: almost all black, one bright bar low in the frame."""
    a = np.zeros((90, 160), np.uint8)
    a[bar_row:bar_row + 3, :] = 255
    Image.fromarray(a).save(path)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(v))
    return v


@pytest.fixture
def capture(tmp_path):
    d = tmp_path / "capture"
    d.mkdir()
    bad = {3, 11, 17, 26}
    for i in range(30):
        p = d / f"frame_{i:04d}.png"
        if i in bad:
            _bad(p, 74 + (i % 3) * 4)
        else:
            _good(p, 120 + (i * 4) % 100)
    return d, sorted(f"frame_{i:04d}.png" for i in bad)


# ============================================================
# Classes
# ============================================================

def test_classes_persist_in_the_vault_not_beside_the_frames(vault, capture):
    folder, _ = capture
    before = sorted(p.name for p in folder.iterdir())
    fc.add_class("frames", "good")
    fc.add_class("frames", "bad timing")
    assert fc.open_classifier("frames")["classes"] == ["good", "bad timing"]
    assert (vault / "classifiers" / "frames" / "classes.json").is_file()
    assert sorted(p.name for p in folder.iterdir()) == before


def test_adding_nothing_or_a_duplicate_keeps_the_list(vault):
    """A stray click must not empty the class list: these are soft cases,
    not failures (a failure would clear the list's port)."""
    fc.add_class("frames", "good")
    r = fc.add_class("frames", "")
    assert r["classes"] == ["good"] and "Type a class name" in r["summary"]
    r = fc.add_class("frames", "good")
    assert r["classes"] == ["good"] and "already" in r["summary"]
    assert r["cleared"] == ""


@pytest.mark.parametrize("bad", ["", "..", "a/b", r"..\x", "has space",
                                 "-lead", "x" * 70])
def test_a_classifier_name_cannot_reach_outside_its_folder(vault, bad):
    with pytest.raises(RuntimeError, match="not a usable classifier name"):
        fc.open_classifier(bad)


def test_a_class_that_still_labels_frames_cannot_be_removed(vault, capture):
    """Removing it would throw away labels the user made."""
    folder, _ = capture
    fc.add_class("frames", "good")
    fc.add_class("frames", "typo")
    fc.mark_frame("frames", str(folder), "frame_0000.png", ["good"])
    r = fc.remove_class("frames", ["good"])
    assert "Not removed" in r["summary"] and "good" in r["classes"]
    r = fc.remove_class("frames", ["typo"])
    assert r["classes"] == ["good"]


# ============================================================
# Marking
# ============================================================

def test_marking_needs_a_class_and_a_frame(vault, capture):
    folder, _ = capture
    fc.add_class("frames", "good")
    with pytest.raises(RuntimeError, match="pick a class"):
        fc.mark_frame("frames", str(folder), "frame_0000.png", [])
    with pytest.raises(RuntimeError, match="no frame on screen"):
        fc.mark_frame("frames", str(folder), "", ["good"])
    with pytest.raises(RuntimeError, match="not a class"):
        fc.mark_frame("frames", str(folder), "frame_0000.png", ["nope"])


def test_the_frame_is_the_file_the_browser_showed(vault, capture):
    """The browser publishes a path RELATIVE to the folder; marking joins it
    with the folder rather than recomputing it from an index."""
    folder, _ = capture
    fc.add_class("frames", "good")
    fc.mark_frame("frames", str(folder), "frame_0005.png", ["good"])
    labels = json.loads((vault / "classifiers" / "frames" /
                         "classes.json").read_text())["labels"]
    assert list(labels) == [str((folder / "frame_0005.png").resolve())]


def test_re_marking_a_frame_changes_its_label_and_says_so(vault, capture):
    folder, _ = capture
    for c in ("good", "bad"):
        fc.add_class("frames", c)
    fc.mark_frame("frames", str(folder), "frame_0001.png", ["good"])
    r = fc.mark_frame("frames", str(folder), "frame_0001.png", ["bad"])
    assert "(was 'good')" in r["summary"] and "good 0, bad 1" in r["summary"]


# ============================================================
# Training and predicting
# ============================================================

def _mark_some(folder):
    for c in ("good", "bad timing"):
        fc.add_class("frames", c)
    for n in ("frame_0003.png", "frame_0017.png"):
        fc.mark_frame("frames", str(folder), n, ["bad timing"])
    for n in ("frame_0000.png", "frame_0008.png", "frame_0020.png"):
        fc.mark_frame("frames", str(folder), n, ["good"])


def test_training_needs_two_classes(vault, capture):
    folder, _ = capture
    fc.add_class("frames", "good")
    fc.mark_frame("frames", str(folder), "frame_0000.png", ["good"])
    with pytest.raises(RuntimeError, match="at least two classes"):
        fc.train("frames")


def test_predicting_before_training_says_so(vault, capture):
    folder, _ = capture
    with pytest.raises(RuntimeError, match="no trained model yet"):
        fc.predict_frame("frames", str(folder), "frame_0000.png")


def test_train_reports_an_honest_accuracy(vault, capture):
    """2 bad + 3 good marked. A bootstrapped forest scored 4/5 here — with
    one bad example held out, a third of its trees never saw the other — so
    small marked sets train without bootstrap."""
    folder, _ = capture
    _mark_some(folder)
    r = fc.train("frames")
    assert "Trained a random forest on 5 frames in 2 classes" in r["summary"]
    assert "Leave-one-out accuracy 100%" in r["summary"]
    assert (vault / "classifiers" / "frames" / "model.npz").is_file()


def test_small_marked_sets_train_without_bootstrap():
    X = np.random.RandomState(0).rand(6, 4).astype(np.float32)
    y = np.array([0, 0, 0, 1, 1, 1])
    assert fc._forest(X, y).bootstrap is False
    Xb = np.random.RandomState(0).rand(40, 4).astype(np.float32)
    assert fc._forest(Xb, np.arange(40) % 2).bootstrap is True


def test_many_marks_switch_to_k_fold(vault, tmp_path):
    """Leave-one-out refits the forest once per frame; past 20 frames that
    would stall the window, so a stratified k-fold is used instead."""
    d = tmp_path / "many"
    d.mkdir()
    for c in ("good", "bad"):
        fc.add_class("frames", c)
    for i in range(24):
        p = d / f"f{i:02d}.png"
        (_bad(p, 74) if i % 3 == 0 else _good(p, 100 + i))
        fc.mark_frame("frames", str(d), p.name, ["bad" if i % 3 == 0 else "good"])
    r = fc.train("frames")
    assert "5-fold accuracy" in r["summary"], r["summary"]


def test_the_model_file_is_plain_arrays_never_a_pickle(vault, capture):
    """Loading a pickle runs code — the reason the gate refuses pickle. The
    store keeps features and refits; it must load with allow_pickle=False."""
    _mark_some(capture[0])
    fc.train("frames")
    m = np.load(vault / "classifiers" / "frames" / "model.npz",
                allow_pickle=False)
    assert set(m.files) == {"X", "y", "classes", "paths"}
    assert m["X"].dtype == np.float32


def test_training_without_scikit_learn_says_so(vault, capture, monkeypatch):
    _mark_some(capture[0])
    monkeypatch.setitem(sys.modules, "sklearn", None)
    monkeypatch.setitem(sys.modules, "sklearn.ensemble", None)
    with pytest.raises(RuntimeError, match="scikit-learn is not installed"):
        fc.train("frames")
    assert not (vault / "classifiers" / "frames" / "model.npz").exists()


def test_prediction_explains_itself(vault, capture):
    folder, _ = capture
    _mark_some(folder)
    fc.train("frames")
    r = fc.predict_frame("frames", str(folder), "frame_0026.png")
    assert r["label"] == "bad timing"
    assert "of the forest agrees" in r["summary"]
    assert "most similar marked frame" in r["summary"]
    assert fc.predict_frame("frames", str(folder), "frame_0014.png")["label"] \
        == "good"


def test_a_new_train_is_picked_up_by_the_next_prediction(vault, capture):
    """The fitted forest is cached per model file by modification time —
    retraining must not keep answering with the old forest."""
    folder, _ = capture
    _mark_some(folder)
    fc.train("frames")
    fc.predict_frame("frames", str(folder), "frame_0026.png")
    fc.add_class("frames", "third")
    fc.mark_frame("frames", str(folder), "frame_0026.png", ["third"])
    import time as _t
    _t.sleep(0.05)
    fc.train("frames")
    assert fc.predict_frame("frames", str(folder), "frame_0026.png")["label"] \
        == "third"


def test_classifying_a_folder_finds_every_bad_frame(vault, capture):
    folder, truth = capture
    _mark_some(folder)
    fc.train("frames")
    r = fc.classify_folder("frames", str(folder))
    assert r["counts"] == {"good": 26, "bad timing": 4}
    got = sorted(row.split()[0] for row in r["rows"] if "bad timing" in row)
    assert got == truth
    assert r["rows"][0].startswith("frame_0000.png")      # capture order


@pytest.mark.parametrize("folder,msg", [("", "no folder chosen"),
                                        ("NOPE", "is not a folder")])
def test_classifying_without_a_folder_raises(vault, capture, folder, msg):
    _mark_some(capture[0])
    fc.train("frames")
    target = folder if folder != "NOPE" else str(capture[0] / "nope")
    with pytest.raises(RuntimeError, match=msg):
        fc.classify_folder("frames", target)


def test_a_moved_training_frame_is_skipped_and_reported(vault, capture):
    folder, _ = capture
    _mark_some(folder)
    extra = folder / "gone.png"
    _good(extra, 50)
    fc.mark_frame("frames", str(folder), "gone.png", ["good"])
    extra.unlink()
    r = fc.train("frames")
    assert "Skipped 1 missing frame" in r["summary"]


def test_sixteen_bit_frames_are_scaled_not_clamped(tmp_path):
    p = tmp_path / "a.tif"
    Image.new("I;16", (8, 8), 4096).save(p)
    v = fc.thumbnail(p)
    assert abs(float(v.mean()) - 16 / 255) < 0.01


def test_saves_leave_no_temp_files_behind(vault, capture):
    _mark_some(capture[0])
    fc.train("frames")
    left = [p.name for p in (vault / "classifiers" / "frames").iterdir()]
    assert sorted(left) == ["classes.json", "model.npz"]


# ============================================================
# The framework pieces it needed
# ============================================================

def test_generated_listboxes_keep_their_own_selection(tk_root):
    """Tk's default exportselection makes selecting in one listbox CLEAR the
    others — a picked class vanished when the user clicked elsewhere."""
    import tkinter as tk
    import gui_emit as ge
    import gui_layout as gl
    import gui_shapes as gs
    import gui_spec as gsp
    a = gs.new_shape("listbox", 0, 0); a.id = "a"
    b = gs.new_shape("listbox", 0, 200); b.id = "b"
    spec = gsp.build([a, b], gl.infer([a, b], 400, 400), project="lb")
    src = ge.emit_main_ui(spec)
    assert src.count("exportselection=False") == 2
    top = tk.Toplevel(tk_root)
    try:
        l1 = tk.Listbox(top, exportselection=False)
        l2 = tk.Listbox(top, exportselection=False)
        for lb in (l1, l2):
            lb.insert("end", "x", "y")
            lb.pack()
        l1.selection_set(1)
        l2.selection_set(0)
        top.update()
        assert l1.curselection() == (1,)
    finally:
        top.destroy()


@pytest.mark.parametrize("label", ["Remove", "Load", "Kill"])
def test_a_button_named_like_a_blocked_call_gets_a_safe_port(label):
    """MEASURED: a button labelled "Remove" produced `self.remove = ...` in
    ports.py and the now-enforced gate refused the whole app."""
    import gui_policy as pol
    import gui_ports as gp
    name = gp.default_port_name("button", label)
    assert name not in pol.DENIED_ATTRS and name.endswith("_button")
    assert not gp.validate_port_name(label.lower())[0]


def test_barbie_v3_builds_passes_the_gate_and_is_ready(tmp_path):
    import gui_policy as pol
    import gui_shapes as gs
    import python_envs as pe
    import run_example_gui as rex
    pdir = rex.build("barbie_capture_v3", project="v3", vault_dir=tmp_path)
    reqs = gs.load_gspec(pdir / "project.gspec").requires
    ok, errs = pol.validate_dir(pdir, "linked", reqs)
    assert ok, errs
    fonts = (pdir / "ui" / "main_ui.py").read_text(encoding="utf-8")
    assert "Arial" in fonts and "Magneto" not in fonts
    assert "Segoe UI" not in fonts
    assert pe.preflight(pdir, "", "linked", reqs).ok


DRIVER = textwrap.dedent('''
    import json, sys, time
    from pathlib import Path
    FRAMES = sys.argv[1]
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
    p = app.ports
    p.capture_folder.set(FRAMES); pump(800)
    cl = p.classes.widget
    for c in ("good", "bad timing"):
        p.new_class.set(c); app.btn_add_class.invoke(); pump(200)
    def mark(i, cls):
        p.frame.set(i); pump(200)
        cl.selection_clear(0, "end")
        cl.selection_set(list(cl.get(0, "end")).index(cls))
        app.btn_mark_this_frame.invoke(); pump(150)
    for i in (3, 17): mark(i, "bad timing")
    for i in (0, 8, 20): mark(i, "good")
    app.btn_train.invoke(); pump(300)
    trained = p.classifier_status.get()
    app.btn_classify_all_frames.invoke(); pump(800)
    rows = list(p.predictions.widget.get(0, "end"))
    print("__OUT__" + json.dumps({"trained": trained, "rows": rows,
                                  "current": p.current_frame.get()}))
    root.destroy()
''')


def test_the_generated_app_marks_trains_and_classifies(tmp_path, capture,
                                                      monkeypatch):
    import run_example_gui as rex
    folder, truth = capture
    vault = tmp_path / "v"
    pdir = rex.build("barbie_capture_v3", project="e2e", vault_dir=vault)
    drv = tmp_path / "drive.py"
    drv.write_text(DRIVER, encoding="utf-8")
    env = dict(os.environ, COUNCIL_NO_DIALOGS="1", COUNCIL_VAULT_ROOT=str(vault))
    r = subprocess.run([sys.executable, str(drv), str(folder)], cwd=str(pdir),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180, env=env)
    assert r.returncode == 0, r.stderr[-1500:]
    out = json.loads(next(l for l in r.stdout.splitlines()
                          if l.startswith("__OUT__"))[len("__OUT__"):])
    assert "Leave-one-out accuracy 100%" in out["trained"], out["trained"]
    got = sorted(row.split()[0] for row in out["rows"] if "bad timing" in row)
    assert got == truth
    assert out["current"] == "frame_0020.png"   # the file last on screen
    assert (vault / "classifiers" / "frames" / "classes.json").is_file()
