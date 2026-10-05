"""
Make classes, mark frames, train a very simple classifier — Barbie Capture v3.

A random forest over 32x32 greyscale thumbnails plus their row/column profiles.
Measured on the 120-frame sample capture: marking 3 bad-timing and 4 good
frames, then "Classify all frames", picked out exactly the 10 frames
frame_timing finds by a completely different method, with no false alarms.

Everything is kept in <vault>/classifiers/<name>/ — never beside the frames.
Tests point COUNCIL_VAULT_ROOT at a temp folder and build their own frames, so
they run on a fresh clone.

Then the classifier LIBRARY (Typhon, 2026-10-02/05): versions, save as /
rename / delete, export and import of one file, the run record, origin tags
and user tags in a store many apps share, filtering, and what an app needs
to go its own way (a store of its own, a bundle of everything it made, and a
module that imports nothing of the Council's). Each test names the
behaviour it would catch missing.

Run:  python -m pytest tests/test_frame_classes.py -q
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import types
import zipfile
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
    """A temp vault, no store override, and no generated app in this
    process: another test file may have left a real project's `app` or
    `handlers` module in sys.modules, and those decide which app a new
    classifier says made it."""
    v = tmp_path / "vault"
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(v))
    monkeypatch.delenv(fc.STORE_ENV, raising=False)
    for mod in ("app", "handlers"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
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
    """A version is written in a hidden temp folder and renamed into place;
    neither that folder nor any temp file may be left behind."""
    _mark_some(capture[0])
    fc.train("frames")
    store = vault / "classifiers" / "frames"
    assert sorted(p.name for p in store.iterdir()) == \
        ["about.json", "classes.json", "model.npz", "versions"]
    assert [p.name for p in (store / "versions").iterdir()] == ["v1"]
    assert sorted(p.name for p in (store / "versions" / "v1").iterdir()) == \
        ["classes.json", "meta.json", "model.npz"]


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


# ============================================================
# The library — helpers
# ============================================================

def _store(vault):
    return vault / "classifiers"


@pytest.fixture
def trained(vault, capture):
    _mark_some(capture[0])
    fc.train("frames")
    return capture


def _project(root, name, title, created="20261005_101500", example=""):
    """A generated app's project folder, as far as an origin needs one:
    handlers.py and ui/ (what makes it a project), manifest.json, and
    project.gspec's window title."""
    pdir = root / name
    (pdir / "ui").mkdir(parents=True)
    (pdir / "handlers.py").write_text("", encoding="utf-8")
    (pdir / "manifest.json").write_text(json.dumps(
        {"name": name, "created": created, "example": example}),
        encoding="utf-8")
    (pdir / "project.gspec").write_text(json.dumps(
        {"project": name, "window": {"title": title}}), encoding="utf-8")
    return pdir


def _run_as(monkeypatch, pdir):
    """Make this process look like the app in ``pdir`` — a generated
    main.py imports `app` from its own folder, and that is how the running
    app is found."""
    mod = types.ModuleType("app")
    mod.__file__ = str(pdir / "app.py")
    monkeypatch.setitem(sys.modules, "app", mod)


def _about(vault, name):
    return json.loads((_store(vault) / name / "about.json")
                      .read_text(encoding="utf-8"))


def _legacy(vault, folder, with_model=True):
    """A classifier as the build before versions and origins left it:
    classes.json and model.npz, nothing else."""
    d = _store(vault) / "frames"
    d.mkdir(parents=True)
    labels = {str((folder / n).resolve()): c for n, c in
              [("frame_0003.png", "bad timing"), ("frame_0017.png", "bad timing"),
               ("frame_0000.png", "good"), ("frame_0008.png", "good")]}
    (d / "classes.json").write_text(json.dumps(
        {"version": 1, "classes": ["good", "bad timing"], "labels": labels}),
        encoding="utf-8")
    if with_model:
        paths = sorted(labels)
        classes = ["good", "bad timing"]
        model = {"X": np.stack([fc.features(p) for p in paths]),
                 "y": np.array([classes.index(labels[p]) for p in paths]),
                 "classes": classes, "paths": paths}
        (d / "model.npz").write_bytes(fc._npz_bytes(model))
    return d


# ============================================================
# Versions
# ============================================================

def test_every_train_that_changes_the_model_is_a_new_immutable_version(
        vault, capture):
    folder, _ = capture
    _mark_some(folder)
    r1 = fc.train("frames")
    assert re.fullmatch(r"frames v1 \([0-9a-f]{8}\)", r1["version"])
    assert r1["version"].endswith(f"({r1['sha256'][:8]})")
    v1 = _store(vault) / "frames" / "versions" / "v1"
    before = {p.name: p.read_bytes() for p in v1.iterdir()}
    assert json.loads(before["meta.json"])["sha256"] == r1["sha256"]
    fc.mark_frame("frames", str(folder), "frame_0011.png", ["bad timing"])
    r2 = fc.train("frames")
    assert r2["version"].startswith("frames v2 (")
    assert r2["sha256"] != r1["sha256"]
    assert {p.name: p.read_bytes() for p in v1.iterdir()} == before
    with np.load(_store(vault) / "frames" / "model.npz",
                 allow_pickle=False) as top:
        assert fc._model_sha({k: top[k] for k in top.files}) == r2["sha256"]


def test_training_unchanged_marks_is_not_a_new_version(vault, trained):
    """Two numbers for one model would make the run record ambiguous."""
    again = fc.train("frames")
    assert "Unchanged: still frames v1" in again["summary"]
    assert [p.name for p in (_store(vault) / "frames" / "versions")
            .iterdir()] == ["v1"]


def test_a_classifier_from_before_versions_becomes_v1_with_nothing_lost(
        vault, capture):
    d = _legacy(vault, capture[0])
    marks = (d / "classes.json").read_bytes()
    model = (d / "model.npz").read_bytes()
    r = fc.open_classifier("frames")
    assert "is now frames v1" in r["summary"]
    assert r["version"].startswith("frames v1")
    meta = json.loads((d / "versions" / "v1" / "meta.json").read_text())
    assert meta["how"] == "migrated"
    assert (d / "classes.json").read_bytes() == marks
    assert (d / "model.npz").read_bytes() == model
    assert (d / "versions" / "v1" / "model.npz").read_bytes() == model
    assert fc.predict_frame("frames", str(capture[0]),
                            "frame_0026.png")["label"] == "bad timing"


def test_an_unreadable_model_is_reported_and_kept_never_replaced(
        vault, capture):
    d = _legacy(vault, capture[0], with_model=False)
    (d / "model.npz").write_bytes(b"not a model at all")
    r = fc.open_classifier("frames")
    assert "has a problem" in r["summary"] and "left as it is" in r["summary"]
    assert (d / "model.npz").read_bytes() == b"not a model at all"
    with pytest.raises(RuntimeError, match="cannot be read"):
        fc.predict_frame("frames", str(capture[0]), "frame_0001.png")
    t = fc.train("frames")
    kept = list(d.glob("model.unusable-*.npz"))
    assert len(kept) == 1 and kept[0].read_bytes() == b"not a model at all"
    assert kept[0].name in t["summary"]


def test_a_model_written_by_an_older_build_after_versions_is_adopted(
        vault, trained):
    """An older build knows only model.npz; what it trained must become a
    version, not be overwritten by the copy of the current one."""
    d = _store(vault) / "frames"
    with np.load(d / "model.npz", allow_pickle=False) as m:
        old = {k: m[k] for k in m.files}
    old["X"] = old["X"] * 0.5
    (d / "model.npz").write_bytes(fc._npz_bytes(old))
    r = fc.open_classifier("frames")
    assert "older build is now frames v2" in r["summary"]
    assert json.loads((d / "versions" / "v2" / "meta.json")
                      .read_text())["how"] == "adopted"


def test_a_version_whose_model_was_altered_is_refused_by_name(vault, trained):
    d = _store(vault) / "frames" / "versions" / "v1"
    with np.load(d / "model.npz", allow_pickle=False) as m:
        other = {k: m[k] for k in m.files}
    other["X"] = other["X"] + 0.01
    (d / "model.npz").write_bytes(fc._npz_bytes(other))
    with pytest.raises(RuntimeError, match=r"frames v1 \(.*no longer matches"):
        fc.predict_frame("frames", str(trained[0]), "frame_0001.png")


def test_a_marked_frame_whose_file_is_gone_trains_from_its_stored_features(
        vault, trained):
    folder, _ = trained
    (folder / "frame_0003.png").unlink()
    fc.mark_frame("frames", str(folder), "frame_0011.png", ["bad timing"])
    r = fc.train("frames")
    assert "on 6 frames" in r["summary"]
    assert "Reused the stored features of 1" in r["summary"]


# ============================================================
# The library: list, save as, rename, delete
# ============================================================

def test_a_list_row_names_its_classifier_in_a_listbox_table_or_dropdown(
        vault, trained):
    fc.add_tag("frames", "lab 2")
    lib = fc.list_classifiers()
    row, cells = lib["rows"][0], lib["table"][0]
    assert row.startswith("frames   v1 (") and "tags: lab 2" in row
    assert len(cells) == 7 and cells[0] == "frames"
    assert fc._name_of([row]) == "frames"          # a listbox's selection
    assert fc._name_of([cells]) == "frames"        # a table's selection
    assert fc._name_of(row) == "frames"            # a dropdown's text
    assert fc.open_classifier(row)["classes"] == ["good", "bad timing"]
    with pytest.raises(RuntimeError, match="not a usable classifier name"):
        fc.open_classifier("has space")


def test_save_as_copies_every_version_keeps_the_origin_and_records_the_copy(
        vault, trained):
    folder, _ = trained
    fc.mark_frame("frames", str(folder), "frame_0011.png", ["bad timing"])
    v2 = fc.train("frames")["version"]
    fc.classify_folder("frames", str(folder))
    r = fc.save_as("frames", "frames2")
    assert r["name"] == "frames2" and r["names"] == ["frames", "frames2"]
    copy = _store(vault) / "frames2"
    assert sorted(p.name for p in (copy / "versions").iterdir()) == ["v1", "v2"]
    assert not (copy / fc.RUNS).exists()            # the original's history
    a, b = _about(vault, "frames"), _about(vault, "frames2")
    assert b["origin"] == a["origin"]
    assert b["lineage"][-1]["event"] == "copied"
    assert b["lineage"][-1]["from_version"] == v2
    assert f"copied from {v2}" in r["summary"]


def test_save_as_and_rename_never_overwrite_whatever_the_case(vault, trained):
    fc.add_class("other", "x")
    other = (_store(vault) / "other" / "classes.json").read_bytes()
    for call in (lambda: fc.save_as("frames", "other"),
                 lambda: fc.save_as("frames", "OTHER"),
                 lambda: fc.rename_classifier("frames", "Other")):
        with pytest.raises(RuntimeError, match="already exists .*nothing was "
                                               "overwritten"):
            call()
    assert (_store(vault) / "other" / "classes.json").read_bytes() == other
    assert sorted(p.name for p in _store(vault).iterdir()) == ["frames", "other"]


def test_rename_keeps_versions_origin_and_run_record(vault, trained):
    folder, _ = trained
    fc.classify_folder("frames", str(folder))
    origin = _about(vault, "frames")["origin"]
    r = fc.rename_classifier("frames", "timing", "frames")
    assert r["name"] == "timing"                    # the name box follows it
    assert not (_store(vault) / "frames").exists()
    about = _about(vault, "timing")
    assert about["origin"] == origin
    last = about["lineage"][-1]
    assert (last["event"], last["from"]) == ("renamed", "frames")
    assert len(fc.run_history("timing")["records"]) == 1
    assert fc.predict_frame("timing", str(folder), "frame_0026.png")[
        "version"].startswith("timing v1 (")


def test_delete_moves_the_classifier_aside_and_moving_it_back_restores_it(
        vault, trained):
    r = fc.delete_classifier("frames", "frames")
    moved = Path(r["moved_to"])
    assert moved.parent == _store(vault) / fc.DELETED and moved.is_dir()
    assert r["name"] == "" and r["rows"] == []
    os.rename(moved, _store(vault) / "frames")
    assert fc.open_classifier("frames")["version"].startswith("frames v1")


def test_rename_and_delete_wait_while_the_classifier_is_in_use(vault, trained):
    with fc._using(_store(vault) / "frames"):
        with pytest.raises(RuntimeError, match="is in use"):
            fc.rename_classifier("frames", "x")
        with pytest.raises(RuntimeError, match="is in use"):
            fc.delete_classifier("frames")
    assert (_store(vault) / "frames").is_dir()


# ============================================================
# Origin and tags
# ============================================================

def test_a_new_classifier_records_the_app_that_made_it(vault, tmp_path,
                                                        monkeypatch):
    pdir = _project(tmp_path / "apps", "example_typhon", "Typhon",
                    example="typhon")
    _run_as(monkeypatch, pdir)
    fc.add_class("frames", "good")
    o = _about(vault, "frames")["origin"]
    assert o["app"] == "Typhon" and o["project"] == "example_typhon"
    assert o["example"] == "typhon"
    assert o["project_id"] == fc._project_id("example_typhon",
                                             "20261005_101500")
    assert len(o["project_id"]) == 16 and o["host"] == fc._host()
    assert o["created"][:10] == fc._now()[:10]
    assert "From Typhon (project example_typhon) on" in \
        fc.open_classifier("frames")["summary"]


def test_the_origin_is_recorded_once_and_never_rewritten(vault, tmp_path,
                                                         monkeypatch, capture):
    folder, _ = capture
    apps = tmp_path / "apps"
    _run_as(monkeypatch, _project(apps, "example_typhon", "Typhon"))
    fc.add_class("frames", "good")
    origin = _about(vault, "frames")["origin"]
    # Another app opens the shared classifier and works on it.
    _run_as(monkeypatch, _project(apps, "barbie", "Barbie Capture"))
    fc.add_class("frames", "bad timing")
    _mark_some(folder)
    fc.train("frames")
    fc.add_tag("frames", "shared")
    fc.rename_classifier("frames", "frames-b")
    assert _about(vault, "frames-b")["origin"] == origin
    d = _store(vault) / "frames-b"
    about = fc._read_about(d)
    with pytest.raises(RuntimeError, match="recorded once and never rewritten"):
        fc._write_about(d, dict(about, origin=fc._new_origin()))
    assert _about(vault, "frames-b")["origin"] == origin


def test_opening_says_what_happened_to_it_since(vault, trained):
    fc.save_as("frames", "frames2")
    fc.rename_classifier("frames2", "frames3")
    r = fc.open_classifier("frames3")
    assert [h.split(" on ")[0] for h in r["history"]] == [
        f"copied from frames v1 ({r['version'].split('(')[1]}",
        "renamed from frames2"]
    assert "; renamed from frames2 on " in r["summary"]


def test_retraining_another_apps_classifier_says_whose_model_changed(
        vault, tmp_path, monkeypatch, capture):
    """The store is shared: a new version made from Typhon is Barbie's
    current model too, and the person pressing Train should know."""
    folder, _ = capture
    apps = tmp_path / "apps"
    _run_as(monkeypatch, _project(apps, "barbie", "Barbie Capture"))
    _mark_some(folder)
    first = fc.train("frames")
    assert "was made by" not in first["summary"]
    _run_as(monkeypatch, _project(apps, "example_typhon", "Typhon"))
    fc.mark_frame("frames", str(folder), "frame_0011.png", ["bad timing"])
    second = fc.train("frames")
    assert ("'frames' was made by Barbie Capture (project barbie); "
            f"{second['version']} is its current model there too.")         in second["summary"]


def test_a_lineage_at_its_limit_stays_readable(vault, trained):
    d = _store(vault) / "frames"
    about = fc._read_about(d)
    many = [{"event": "renamed", "from": f"n{i}"} for i in range(fc._LINEAGE_MAX)]
    fc._write_about(d, dict(about, lineage=many))
    fc.rename_classifier("frames", "frames-x")      # one more than the limit
    lineage = fc._read_about(_store(vault) / "frames-x")["lineage"]
    assert len(lineage) == fc._LINEAGE_MAX
    assert lineage[-1]["from"] == "frames" and lineage[0]["from"] == "n1"


def test_a_classifier_from_before_origin_tags_is_unknown_never_guessed(
        vault, tmp_path, monkeypatch, capture):
    d = _legacy(vault, capture[0])
    _run_as(monkeypatch, _project(tmp_path / "apps", "example_typhon",
                                  "Typhon"))
    r = fc.open_classifier("frames")
    assert fc.UNKNOWN_ORIGIN in r["summary"]
    assert r["origin"] == fc.UNKNOWN_ORIGIN
    fc.add_class("frames", "third")
    assert not (d / "about.json").exists()          # not stamped "Typhon"
    assert "origin unknown" in fc.list_classifiers()["rows"][0]
    fc.add_tag("frames", "old rig")                 # now it is written ...
    o = _about(vault, "frames")["origin"]           # ... still unknown
    assert o["unknown"] is True and o["app"] == fc.UNKNOWN_ORIGIN
    assert o["project"] == "" and o["project_id"] == ""


def test_user_tags_are_added_once_taken_off_and_apart_from_the_origin(
        vault, trained):
    origin = _about(vault, "frames")["origin"]
    assert fc.add_tag("frames", "  Lab   2 ")["tags"] == ["Lab 2"]
    assert "already tagged" in fc.add_tag("frames", "lab 2")["summary"]
    assert "Type a tag" in fc.add_tag("frames", "")["summary"]
    with pytest.raises(RuntimeError, match="at most 40 characters"):
        fc.add_tag("frames", "x" * 41)
    assert fc.add_tag("frames", "night")["tags"] == ["Lab 2", "night"]
    assert fc.remove_tag("frames", ["LAB 2"])["tags"] == ["night"]
    assert "is not tagged" in fc.remove_tag("frames", "nope")["summary"]
    assert _about(vault, "frames")["origin"] == origin


def test_the_list_filters_by_this_app_app_project_tag_and_unknown(
        vault, tmp_path, monkeypatch, capture):
    apps = tmp_path / "apps"
    _legacy(vault, capture[0])                      # "frames": origin unknown
    _run_as(monkeypatch, _project(apps, "barbie", "Barbie Capture"))
    fc.add_class("b1", "x")
    typhon = _project(apps, "example_typhon", "Typhon")
    _run_as(monkeypatch, typhon)
    fc.add_class("t1", "x")
    fc.add_tag("t1", "night")

    def names(show):
        return fc.list_classifiers(show)["names"]

    assert names("") == ["b1", "frames", "t1"]
    assert names(fc.FILTER_THIS_APP) == ["t1"]
    assert names("App: Barbie Capture") == ["b1"]
    assert names("project: EXAMPLE_TYPHON") == ["t1"]
    assert names("Tag: NIGHT") == names("#night") == ["t1"]
    assert names(fc.FILTER_UNKNOWN) == ["frames"]
    assert names("typhon") == ["t1"]                # any: app, project, tag
    assert names(["App: Typhon"]) == ["t1"]         # a listbox's selection
    lib = fc.list_classifiers("App: Typhon")
    assert lib["summary"].startswith("1 of 3 saved classifiers (App: Typhon)")
    assert lib["filters"] == [fc.FILTER_ALL, fc.FILTER_THIS_APP,
                              "App: Barbie Capture", "App: Typhon",
                              "Project: barbie", "Project: example_typhon",
                              "Tag: night", fc.FILTER_UNKNOWN]
    # Built again from scratch (new created -> new id): still "this app".
    shutil.rmtree(typhon)
    _run_as(monkeypatch, _project(apps, "example_typhon", "Typhon",
                                  created="20261006_090000"))
    assert names(fc.FILTER_THIS_APP) == ["t1"]


def test_names_are_unique_whatever_their_case(vault, monkeypatch):
    """On Windows "Frames" IS "frames"; a store copied there must not hold
    both, so no PC may make both — here, a case-sensitive one is acted out
    by treating "Frames" as a folder that does not exist yet."""
    fc.add_class("frames", "x")
    monkeypatch.setattr(fc, "_is_new", lambda d: True)
    with pytest.raises(RuntimeError, match="'frames' already exists"):
        fc.add_class("Frames", "y")
    assert fc._taken("FRAMES") == "frames"


# ============================================================
# The Council rules this module restates (it imports neither)
# ============================================================

def test_the_project_id_is_stable_across_generate_and_new_for_a_new_project(
        tmp_path, monkeypatch, vault):
    import gui_projects as gpj
    pdir = gpj.create("p1", vault_dir=tmp_path)
    (pdir / "handlers.py").write_text("", encoding="utf-8")
    m = gpj.load_manifest(pdir)
    _run_as(monkeypatch, pdir)
    first = fc._this_app()["project_id"]
    assert first and first == fc._project_id("p1", m.created)
    gpj.save_manifest(pdir, m)                      # what every Generate does
    fc._APP_CACHE.clear()
    assert fc._this_app()["project_id"] == first
    other = gpj.create("p2", vault_dir=tmp_path)
    assert fc._project_id("p2", gpj.load_manifest(other).created) != first
    assert fc._project_id("p1", "") == ""           # no creation time: no id


def test_the_vault_and_app_folder_rules_agree_with_the_council(
        tmp_path, monkeypatch, vault):
    import gui_projects as gpj
    import gui_settings as gst
    for value in (str(tmp_path / "v1"), ""):
        if value:
            monkeypatch.setenv("COUNCIL_VAULT_ROOT", value)
        else:
            monkeypatch.delenv("COUNCIL_VAULT_ROOT")
        assert fc._vault_root() == gpj.resolve_vault_root()
    pdir = _project(tmp_path / "apps", "p", "P")
    for folder in (pdir, pdir / "ui", tmp_path):
        assert fc._looks_like_project(folder) == gst._looks_like_project(folder)
    _run_as(monkeypatch, pdir)
    assert fc._app_folder() == gst.app_folder() == pdir.resolve()


# ============================================================
# Export and import of one classifier
# ============================================================

def test_an_export_carries_the_classifier_to_another_pc_without_its_frames(
        vault, trained, tmp_path, monkeypatch):
    folder, _ = trained
    fc.add_tag("frames", "lab 1")
    fc.classify_folder("frames", str(folder))
    out = tmp_path / "out"
    out.mkdir()
    e = fc.export_classifier("frames", str(out))
    assert Path(e["path"]).name == "frames-v1.typhon-classifier.zip"
    origin = _about(vault, "frames")["origin"]
    # The other PC: another vault, and the frames are not there.
    pc2 = tmp_path / "pc2"
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(pc2))
    shutil.rmtree(folder)
    fresh = tmp_path / "new_capture"
    fresh.mkdir()
    _bad(fresh / "f1.png", 78)
    _good(fresh / "f2.png", 140)
    r = fc.import_classifier(e["path"])
    assert r["name"] == "frames" and r["imported"] == ["frames"]
    assert r["version"] == e["version"]             # same id on both PCs
    assert fc.predict_frame("frames", str(fresh), "f1.png")["label"] == \
        "bad timing"
    about = _about(pc2, "frames")
    assert about["origin"] == origin and about["tags"] == ["lab 1"]
    assert about["lineage"][-1]["event"] == "imported"
    assert about["lineage"][-1]["file"] == "frames-v1.typhon-classifier.zip"
    assert len(fc.run_history("frames")["records"]) == 1   # its history came
    # ... and it trains further there, the gone frames from stored features.
    fc.mark_frame("frames", str(fresh), "f2.png", ["good"])
    t = fc.train("frames")
    assert t["version"].startswith("frames v2 (")
    assert "Reused the stored features of 5" in t["summary"]


def test_importing_a_taken_name_asks_for_another_and_never_overwrites(
        vault, trained, tmp_path, monkeypatch):
    e = fc.export_classifier("frames", str(tmp_path))
    pc2 = tmp_path / "pc2"
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(pc2))
    fc.add_class("frames", "something else")        # pc2's own "frames"
    mine = (_store(pc2) / "frames" / "classes.json").read_bytes()
    with pytest.raises(RuntimeError, match="'frames' already exists .*type "
                                           "another name in New name"):
        fc.import_classifier(e["path"])
    assert (_store(pc2) / "frames" / "classes.json").read_bytes() == mine
    r = fc.import_classifier(e["path"], "frames-lab1")
    assert r["name"] == "frames-lab1" and r["imported"] == ["frames-lab1"]
    again = fc.import_classifier(e["path"], "frames-lab1")
    assert "already is this classifier" in again["summary"]
    assert again["imported"] == []
    assert sorted(p.name for p in _store(pc2).iterdir()) == \
        ["frames", "frames-lab1"]                   # no staging left behind


def _rezip(src, dst, change=None, drop=(), add=None, manifest=None,
           rehash=True):
    """A copy of export ``src`` with members changed, dropped or added —
    the manifest's checksums recomputed unless ``rehash`` is False, so a
    test reaches the check after them."""
    with zipfile.ZipFile(src) as zf:
        members = {n: zf.read(n) for n in zf.namelist()}
    members.update(change or {})
    for k in drop:
        members.pop(k, None)
    members.update(add or {})
    man = json.loads(members["manifest.json"])
    man.update(manifest or {})
    if rehash:
        man["members"] = {k: hashlib.sha256(v).hexdigest()
                          for k, v in members.items()
                          if k != "manifest.json"}
    members["manifest.json"] = json.dumps(man).encode()
    with zipfile.ZipFile(dst, "w") as zf:
        for k, v in members.items():
            zf.writestr(k, v)
    return dst


def _npz_with(blobs):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in blobs.items():
            zf.writestr(f"{name}.npy", data)
    return buf.getvalue()


def _npy(a, allow_pickle=False):
    buf = io.BytesIO()
    np.save(buf, a, allow_pickle=allow_pickle)
    return buf.getvalue()


def _huge_header_npy():
    """An .npy whose header claims a billion rows and holds 16 bytes."""
    from numpy.lib import format as npf
    buf = io.BytesIO()
    npf.write_array_header_1_0(buf, {"descr": "<f4", "fortran_order": False,
                                     "shape": (10 ** 9, 1092)})
    return buf.getvalue() + b"\0" * 16


@pytest.mark.parametrize("how,msg", [
    ("not a zip", "not a zip file"),
    ("a stray member", "not part of it"),
    ("a path outside", "not part of it"),
    ("an altered member", "does not match its checksum"),
    ("a newer format", "written by a newer build"),
    ("other features", "features this build does not make"),
    ("python objects", "holds Python objects"),
    ("a lying header", "claims shape"),
    ("no model", "not a complete classifier export"),
])
def test_import_refuses_anything_but_an_intact_export(vault, trained, tmp_path,
                                                      monkeypatch, how, msg):
    e = Path(fc.export_classifier("frames", str(tmp_path))["path"])
    with zipfile.ZipFile(e) as zf:
        npz = zf.read("model.npz")
    with np.load(io.BytesIO(npz), allow_pickle=False) as m:
        arrays = {k: m[k] for k in m.files}
    bad = tmp_path / "bad.typhon-classifier.zip"
    if how == "not a zip":
        bad.write_bytes(b"PK? no")
    elif how == "a stray member":
        _rezip(e, bad, add={"run_me.py": b"print(1)"})
    elif how == "a path outside":
        _rezip(e, bad, add={"../../escape.txt": b"x"})
    elif how == "an altered member":
        _rezip(e, bad, change={"classes.json": b'{"classes": [], "labels": {}}'},
               rehash=False)
    elif how == "a newer format":
        _rezip(e, bad, manifest={"format_version": 2})
    elif how == "other features":
        _rezip(e, bad, manifest={"feature_length": 1024})
    elif how == "python objects":
        objs = dict(arrays, classes=np.array(["good", "bad timing"],
                                              dtype=object))
        _rezip(e, bad, change={"model.npz": _npz_with(
            {k: _npy(v, allow_pickle=True) for k, v in objs.items()})})
    elif how == "a lying header":
        _rezip(e, bad, change={"model.npz": _npz_with(
            {**{k: _npy(v) for k, v in arrays.items()},
             "X": _huge_header_npy()})})
    elif how == "no model":
        _rezip(e, bad, drop=("model.npz",))
    pc2 = tmp_path / "pc2"
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(pc2))
    with pytest.raises(RuntimeError, match=msg):
        fc.import_classifier(str(bad), "x")
    assert not _store(pc2).exists() or not any(_store(pc2).iterdir())


def test_the_export_is_usable_with_nothing_but_numpy_and_scikit_learn(
        vault, trained, tmp_path):
    """An app written later, without this module, reads the manifest,
    computes features as it says, rebuilds the forest as it says — and gets
    this module's answer."""
    folder, _ = trained
    path = fc.export_classifier("frames", str(tmp_path))["path"]
    with zipfile.ZipFile(path) as zf:
        assert set(zf.namelist()) == {"manifest.json", "README.txt",
                                      "meta.json", "classes.json", "model.npz"}
        man = json.loads(zf.read("manifest.json"))
        readme = zf.read("README.txt").decode()
        npz = zf.read("model.npz")
    assert (man["format"], man["format_version"]) == ("typhon-classifier", 1)
    assert (man["feature_layout"], man["feature_length"]) == (1, 1092)
    assert {"features", "model", "origin", "lineage", "tags", "members",
            "version_id", "model_sha256"} <= set(man)
    assert "allow_pickle=False" in readme and "format_version" in readme

    def recipe_features(p):                         # written from the words
        from PIL import Image as Im
        with Im.open(p) as im:
            t = np.asarray(im.convert("L").resize((32, 32), Im.BILINEAR),
                           dtype=np.float32).reshape(-1) / 255.0
        g = t.reshape(32, 32)
        return np.concatenate([t, g.mean(axis=1), g.mean(axis=0),
                               [t.mean(), t.std(), (t < 0.2).mean(),
                                (t > 0.8).mean()]]).astype(np.float32)

    from sklearn.ensemble import RandomForestClassifier
    with np.load(io.BytesIO(npz), allow_pickle=False) as m:
        X, y, classes = m["X"], m["y"], [str(c) for c in m["classes"]]
    forest = RandomForestClassifier(n_estimators=100,
                                    bootstrap=len(y) >= 30,
                                    class_weight="balanced", random_state=0,
                                    n_jobs=1).fit(X, y)
    for name in ("frame_0026.png", "frame_0014.png"):
        x = recipe_features(folder / name)
        assert np.allclose(x, fc.features(folder / name))
        assert classes[int(forest.predict(x[None, :])[0])] == \
            fc.predict_frame("frames", str(folder), name)["label"]


@pytest.mark.skipif(os.name != "nt", reason="Windows' 260-character limit")
def test_a_path_too_long_for_windows_is_named_as_such(vault, trained,
                                                      tmp_path):
    """MEASURED: a store 188 characters deep failed its first Train with a
    bare "No such file or directory" — Windows' word for a path too long."""
    deep = tmp_path
    while len(str(deep)) < 225:
        deep = deep / ("d" * 12)
    deep.mkdir(parents=True)
    try:
        (deep / ("x" * 45)).write_bytes(b"")
    except OSError:
        pass
    else:
        pytest.skip("long paths are switched on for this PC")
    with pytest.raises(RuntimeError, match="Windows refuses 260 or more: "
                                           "save it in a shorter folder"):
        fc.export_classifier("frames", str(deep))


# ============================================================
# The run record
# ============================================================

def test_classifying_is_recorded_in_the_store_never_in_the_capture_folder(
        vault, trained):
    folder, _ = trained
    before = sorted((p.name, p.stat().st_mtime_ns) for p in folder.iterdir())
    r = fc.classify_folder("frames", str(folder))
    assert "classified with frames v1 (" in r["summary"]
    assert "recorded" in r["summary"]
    assert sorted((p.name, p.stat().st_mtime_ns)
                  for p in folder.iterdir()) == before
    lines = (_store(vault) / "frames" / fc.RUNS).read_text().splitlines()
    rec = json.loads(lines[0])
    assert len(lines) == 1 and rec["version_id"] == r["version"]
    assert rec["sha256"] == r["sha256"] and rec["counts"] == r["counts"]
    assert rec["runs"] == {"frame": 30} and rec["frames"] == 30
    assert rec["folder"] == str(folder.resolve()) and rec["host"] == fc._host()
    assert set(rec["by"]) == {"app", "project", "project_id", "script"}
    assert fc._run_of("20261002_101500_frame_000001.png") == "20261002_101500"


def test_the_record_answers_by_classifier_and_by_folder(vault, trained,
                                                        tmp_path):
    folder, _ = trained
    fc.classify_folder("frames", str(folder))
    fc.mark_frame("frames", str(folder), "frame_0011.png", ["bad timing"])
    v2 = fc.train("frames")["version"]
    fc.classify_folder("frames", str(folder))
    h = fc.run_history("frames")
    assert len(h["rows"]) == 2 and v2 in h["rows"][0]   # newest first
    line = fc.classified_with(str(folder))["classified_with"]
    assert line.startswith(f"Classified with {v2} on ")
    assert fc.classified_with("")["classified_with"] == ""
    other = tmp_path / "other"
    other.mkdir()
    assert "Not classified yet" in \
        fc.classified_with(str(other))["classified_with"]


def test_a_partial_last_record_line_is_skipped_and_the_next_starts_afresh(
        vault, trained):
    folder, _ = trained
    fc.classify_folder("frames", str(folder))
    runs = _store(vault) / "frames" / fc.RUNS
    with open(runs, "a", encoding="utf-8") as fh:
        fh.write('{"when": "2026-10-05 1')            # a crash mid-line
    fc.classify_folder("frames", str(folder))
    h = fc.run_history("frames")
    assert len(h["records"]) == 2
    assert "1 line(s) of the record could not be read" in h["summary"]


def test_another_pcs_record_never_answers_for_a_folder_here(vault, trained):
    folder, _ = trained
    rec = {"when": "2026-10-04 10:00:00", "folder": str(folder.resolve()),
           "version_id": "frames v9 (00000000)", "counts": {"good": 1},
           "host": "SOME-OTHER-PC", "frames": 1}
    (_store(vault) / "frames" / fc.RUNS).write_text(json.dumps(rec) + "\n")
    assert "Not classified yet" in \
        fc.classified_with(str(folder))["classified_with"]
    assert "on SOME-OTHER-PC" in fc.run_history("frames")["rows"][0]


# ============================================================
# An app of its own: one store resolver, a bundle, no Council imports
# ============================================================

def test_the_store_is_decided_in_one_place(vault, tmp_path, monkeypatch):
    assert fc.classifier_store() == vault / "classifiers"
    own = tmp_path / "own"
    monkeypatch.setenv(fc.STORE_ENV, str(own))
    fc.add_class("frames", "x")
    assert (own / "frames" / "classes.json").is_file()
    assert not (vault / "classifiers").exists()
    assert fc.store_info()["how"] == f"set by {fc.STORE_ENV}"
    monkeypatch.delenv(fc.STORE_ENV)
    pdir = _project(tmp_path / "apps", "solo", "Solo")
    _run_as(monkeypatch, pdir)
    (pdir / fc.STORE_CONFIG).write_text('{"store": "classifiers"}')
    assert fc.classifier_store() == pdir / "classifiers"
    monkeypatch.setenv(fc.STORE_ENV, "rel")         # relative: to the app
    assert fc.classifier_store() == pdir / "rel"
    monkeypatch.delenv(fc.STORE_ENV)
    (pdir / fc.STORE_CONFIG).write_text("{not json")
    with pytest.raises(RuntimeError, match="says where this app keeps its "
                                           "classifiers"):
        fc.add_class("frames", "y")
    assert not (vault / "classifiers").exists()     # not quietly the vault


def test_everything_one_app_made_goes_into_one_bundle_and_a_store_of_its_own(
        vault, tmp_path, monkeypatch, capture):
    folder, _ = capture
    apps = tmp_path / "apps"
    _run_as(monkeypatch, _project(apps, "barbie", "Barbie Capture"))
    for c in ("good", "bad timing"):
        fc.add_class("b1", c)
    typhon = _project(apps, "example_typhon", "Typhon")
    _run_as(monkeypatch, typhon)
    _mark_some(folder)
    fc.train("frames")
    fc.add_class("draft", "x")                       # never trained
    out = tmp_path / "out"
    out.mkdir()
    b = fc.export_this_app(str(out))
    assert b["names"] == ["frames"] and b["skipped"] == ["draft"]
    with zipfile.ZipFile(b["path"]) as zf:
        assert sorted(zf.namelist()) == [
            "README.txt", "bundle.json",
            "classifiers/frames.typhon-classifier.zip"]
        index = json.loads(zf.read("bundle.json"))
    assert (index["format"], index["format_version"]) == \
        ("typhon-classifier-bundle", 1)
    assert index["made_by"]["project"] == "example_typhon"
    origin = _about(vault, "frames")["origin"]
    # Typhon goes its own way: a store beside it.
    monkeypatch.setenv(fc.STORE_ENV, str(typhon / "classifiers"))
    r = fc.import_classifier(b["path"])
    assert r["imported"] == ["frames"] and r["skipped"] == []
    about = json.loads((typhon / "classifiers" / "frames" / "about.json")
                       .read_text())
    assert about["origin"] == origin
    assert about["lineage"][-1]["file"].startswith(Path(b["path"]).name)
    assert "already here 1 (frames)" in \
        fc.import_classifier(b["path"])["summary"]
    # Somewhere "frames" is already another app's: skipped, then a prefix.
    monkeypatch.setenv(fc.STORE_ENV, str(tmp_path / "elsewhere"))
    _run_as(monkeypatch, _project(apps, "third", "Third"))
    fc.add_class("frames", "theirs")
    r = fc.import_classifier(b["path"])
    assert r["imported"] == [] and r["skipped"] == ["frames"]
    assert "lab2-" in r["summary"] and "Nothing was overwritten" in r["summary"]
    r = fc.import_classifier(b["path"], "lab2-")
    assert r["imported"] == ["lab2-frames"]
    assert fc.import_classifier(b["path"], "lab2-")["imported"] == []


@pytest.mark.parametrize("how,msg", [
    ("an unlisted member", "does not list exactly"),
    ("an altered classifier", "does not match its checksum"),
    ("a path outside", "not part of it"),
    ("a name that cannot be one", "cannot be a classifier name"),
])
def test_a_bundle_is_checked_whole_before_anything_is_imported(
        vault, trained, tmp_path, monkeypatch, how, msg):
    fc.save_as("frames", "frames2")
    b = Path(fc.export_classifiers(str(tmp_path))["path"])
    with zipfile.ZipFile(b) as zf:
        members = {n: zf.read(n) for n in zf.namelist()}
    one = "classifiers/frames2.typhon-classifier.zip"
    if how == "an unlisted member":
        members["classifiers/extra.typhon-classifier.zip"] = members[one]
    elif how == "an altered classifier":
        members[one] = members[one][:-10] + b"0123456789"
    elif how == "a path outside":
        members["classifiers/../../x.typhon-classifier.zip"] = members[one]
    elif how == "a name that cannot be one":
        index = json.loads(members["bundle.json"])
        index["classifiers"][0]["name"] = "CON"
        members["bundle.json"] = json.dumps(index).encode()
    bad = tmp_path / "bad.typhon-classifiers.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        for k, v in members.items():
            zf.writestr(k, v)
    pc2 = tmp_path / "pc2"
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(pc2))
    with pytest.raises(RuntimeError, match=msg):
        fc.import_classifier(str(bad))
    assert not _store(pc2).exists() or not any(_store(pc2).iterdir())


ALONE = textwrap.dedent('''
    import json, os, sys
    from pathlib import Path
    alone, frames_dir, repo = map(Path, sys.argv[1:4])
    sys.path.insert(0, str(alone))
    if sys.argv[4] == "reachable":
        sys.path.append(str(repo))          # there, and still not used
    import numpy as np
    from PIL import Image
    import frame_classes as fc
    frames_dir.mkdir()
    for i in range(8):
        a = np.zeros((90, 160), np.uint8)
        if i % 3 == 0:
            a[74:77, :] = 255
        else:
            a[20:70, :] = 120 + i * 9
        Image.fromarray(a).save(frames_dir / f"f{i}.png")
    for c in ("good", "bad"):
        fc.add_class("frames", c)
    for i in (0, 3):
        fc.mark_frame("frames", str(frames_dir), f"f{i}.png", ["bad"])
    for i in (1, 2, 4):
        fc.mark_frame("frames", str(frames_dir), f"f{i}.png", ["good"])
    out = {"train": fc.train("frames")["summary"],
           "classify": fc.classify_folder("frames", str(frames_dir))["counts"],
           "store": str(fc.classifier_store())}
    e = fc.export_classifier("frames", str(alone))
    os.environ["FRAME_CLASSES_STORE"] = str(alone / "second_store")
    out["import"] = fc.import_classifier(e["path"])["imported"]
    out["list"] = fc.list_classifiers("This app")["names"]
    out["council"] = sorted(
        name for name, m in list(sys.modules.items())
        if str(getattr(m, "__file__", "") or "").lower().startswith(
            str(repo).lower()))
    print("__OUT__" + json.dumps(out))
''')


@pytest.mark.parametrize("council", ["absent", "reachable"])
def test_frame_classes_runs_with_no_council_module_at_all(tmp_path, council):
    """An app that leaves the Council carries this one file. Run from a
    folder holding nothing else, in an isolated interpreter (-I: no
    PYTHONPATH, no user site, not even the current folder on sys.path), it
    makes, trains, classifies, exports and imports — and no module of the
    Council's is ever loaded: not when the Council is absent, and not when
    it is on sys.path either (a `try: import gui_projects` would pass the
    first and fail the second)."""
    alone = tmp_path / "alone"
    alone.mkdir()
    shutil.copy(REPO / "frame_classes.py", alone / "frame_classes.py")
    driver = tmp_path / "drive.py"
    driver.write_text(ALONE, encoding="utf-8")
    env = dict(os.environ, COUNCIL_VAULT_ROOT=str(tmp_path / "vault"))
    env.pop(fc.STORE_ENV, None)     # the vault's store first, then its own
    r = subprocess.run([sys.executable, "-I", str(driver), str(alone),
                        str(tmp_path / "frames"), str(REPO), council],
                       cwd=str(tmp_path), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180,
                       env=env)
    assert r.returncode == 0, r.stderr[-1500:]
    out = json.loads(next(l for l in r.stdout.splitlines()
                          if l.startswith("__OUT__"))[len("__OUT__"):])
    assert "Saved as frames v1" in out["train"]
    assert out["classify"] == {"good": 5, "bad": 3}
    assert out["import"] == ["frames"] and out["list"] == ["frames"]
    assert out["council"] == []
    assert Path(out["store"]) == tmp_path / "vault" / "classifiers"
    assert (alone / "second_store" / "frames" / "about.json").is_file()
    assert "imports nothing of the Council's" in fc.__doc__


# ============================================================
# What a generated app sees: result keys, names, the gate
# ============================================================

def test_every_result_key_the_wiring_editor_offers_is_returned(
        vault, trained, tmp_path):
    """The Wiring editor offers a function's "Keys:" as result keys; one
    that is not really returned is a KeyError in a generated handler."""
    from council_core import designer_wiring as dw
    folder, _ = trained
    e = fc.export_classifier("frames", str(tmp_path))
    calls = {
        "open_classifier": lambda: fc.open_classifier("frames"),
        "add_class": lambda: fc.add_class("frames", "third"),
        "remove_class": lambda: fc.remove_class("frames", ["third"]),
        "mark_frame": lambda: fc.mark_frame("frames", str(folder),
                                            "frame_0001.png", ["good"]),
        "train": lambda: fc.train("frames"),
        "predict_frame": lambda: fc.predict_frame("frames", str(folder),
                                                  "frame_0001.png"),
        "classify_folder": lambda: fc.classify_folder("frames", str(folder)),
        "list_classifiers": lambda: fc.list_classifiers(),
        "save_as": lambda: fc.save_as("frames", "copy"),
        "rename_classifier": lambda: fc.rename_classifier("copy", "copy2"),
        "add_tag": lambda: fc.add_tag("frames", "t"),
        "remove_tag": lambda: fc.remove_tag("frames", "t"),
        "list_versions": lambda: fc.list_versions("frames"),
        "export_classifier": lambda: fc.export_classifier("frames",
                                                          str(tmp_path)),
        "export_classifiers": lambda: fc.export_classifiers(str(tmp_path)),
        "export_this_app": lambda: fc.export_this_app(str(tmp_path)),
        "import_classifier": lambda: fc.import_classifier(e["path"], "imp"),
        "import_bundle": lambda: fc.import_bundle(
            fc.export_classifiers(str(tmp_path), "frames")["path"], "b-"),
        "run_history": lambda: fc.run_history("frames"),
        "classified_with": lambda: fc.classified_with(str(folder)),
        "store_info": lambda: fc.store_info(),
        "delete_classifier": lambda: fc.delete_classifier("copy2"),
    }
    info = {f.name: f for f in dw.module_info("frame_classes").functions}
    for name, call in calls.items():
        keys = set(info[name].result_keys)
        assert keys, f"{name} offers no result keys"
        missing = keys - set(call())
        assert not missing, f"{name} offers {missing} but does not return them"


def test_no_function_is_named_like_a_call_the_gate_denies():
    """A generated handler imports a linked function BY NAME, and the gate
    checks imported names: a function called `load` would fail the app."""
    import gui_policy as pol
    public = sorted(n for n, v in vars(fc).items()
                    if callable(v) and not n.startswith("_")
                    and getattr(v, "__module__", "") == "frame_classes")
    assert "export_this_app" in public and "add_tag" in public
    denied = pol.DENIED_ATTRS | pol.DENIED_BUILTINS | pol.DENIED_QT_NAMES
    assert not set(public) & denied
    handler = "\n".join(f"from frame_classes import {n}" for n in public)
    ok, errs = pol.validate(handler, "linked", ["numpy", "PIL", "sklearn"],
                            toolkit="qt")
    assert ok, errs


# ============================================================
# The built Typhon app
# ============================================================

IN_TYPHON = textwrap.dedent('''
    import json, sys
    from pathlib import Path
    FRAMES, OUT = Path(sys.argv[1]), sys.argv[2]
    main_py = Path.cwd() / "main.py"
    boot = main_py.read_text(encoding="utf-8").split("from app import main")[0]
    exec(compile(boot, str(main_py), "exec"), {"__file__": str(main_py)})
    from PySide6.QtWidgets import QApplication
    qt = QApplication.instance() or QApplication([])
    from app import App
    ui = App()                                   # never shown
    p = ui.ports
    # Typhon's own buttons, through its generated handlers.
    p.classifier_name.set("typhon-frames")
    for c in ("good", "bad timing"):
        p.new_class.set(c)
        ui.on_btn_add_class()
    added = p.classifier_status.get()
    # The new functions, called the way a script link calls them: inputs
    # from ports, result keys into ports.
    from frame_classes import (add_tag, classifier_store, classified_with,
                               export_this_app, list_classifiers, mark_frame,
                               open_classifier, store_info)
    for name, cls in (("f0.png", "bad timing"), ("f3.png", "bad timing"),
                      ("f1.png", "good"), ("f2.png", "good"),
                      ("f4.png", "good")):
        mark_frame(p.classifier_name.get(), str(FRAMES), name, [cls])
    ui.on_btn_train()
    trained = p.classifier_status.get()
    p.capture_folder.set(str(FRAMES))
    ui.on_btn_classify_all_frames()
    result = classified_with(p.capture_folder.get())
    p.classifier_status.set(result["classified_with"])
    line = p.classifier_status.get()
    result = add_tag(p.classifier_name.get(), "rig A")
    result = list_classifiers("This app")
    p.predictions.set(result["rows"])
    opened = open_classifier(p.predictions.items()[0])
    bundle = export_this_app(OUT)
    store = store_info()
    about = json.loads((classifier_store() / "typhon-frames" /
                        "about.json").read_text(encoding="utf-8"))
    print("__OUT__" + json.dumps({
        "added": added, "trained": trained, "line": line,
        "listed": result["names"], "opened": opened["summary"],
        "bundle": bundle["names"], "store": store["store"],
        "app": store["app"], "origin": about["origin"],
        "tags": about["tags"]}))
    ui.close()
''')


def test_built_typhon_tags_the_classifiers_it_makes_with_its_own_project(
        tmp_path):
    """Typhon built for real (Qt, linked) into a temp vault passes its own
    gate; a classifier made through its real Add class button says it came
    from Typhon — that project, that id — and the new library functions
    work from inside it exactly as a script link calls them."""
    import gui_policy as pol
    import gui_projects as gpj
    import gui_shapes as gs
    import run_example_gui as rex
    vault = tmp_path / "v"
    pdir = rex.build("typhon", project="typhon_cls", vault_dir=vault,
                     target="qt")
    reqs = gs.load_gspec(pdir / "project.gspec").requires
    ok, errs = pol.validate_dir(pdir, "linked", reqs, toolkit="qt")
    assert ok, errs
    frames = tmp_path / "frames"
    frames.mkdir()
    for i in range(6):
        (_bad if i % 3 == 0 else _good)(frames / f"f{i}.png",
                                        74 if i % 3 == 0 else 120 + 9 * i)
    drv = tmp_path / "in_typhon.py"
    drv.write_text(IN_TYPHON, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", COUNCIL_NO_DIALOGS="1",
               COUNCIL_VAULT_ROOT=str(vault))
    env.pop(fc.STORE_ENV, None)
    r = subprocess.run([sys.executable, str(drv), str(frames), str(out)],
                       cwd=str(pdir), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=240,
                       env=env)
    assert r.returncode == 0, r.stderr[-2000:]
    assert " failed: " not in r.stderr, r.stderr[-2000:]
    got = json.loads(next(l for l in r.stdout.splitlines()
                          if l.startswith("__OUT__"))[len("__OUT__"):])
    o = got["origin"]
    assert (o["app"], o["project"], o["example"]) == \
        ("Typhon", "typhon_cls", "typhon")
    assert o["project_id"] == fc._project_id(
        "typhon_cls", gpj.load_manifest(pdir).created)
    assert o["host"] == fc._host() and got["tags"] == ["rig A"]
    assert "Added 'bad timing'" in got["added"]
    assert "Saved as typhon-frames v1 (" in got["trained"]
    assert got["line"].startswith("Classified with typhon-frames v1 (")
    assert got["listed"] == ["typhon-frames"]
    assert "From Typhon (project typhon_cls)" in got["opened"]
    assert got["bundle"] == ["typhon-frames"]
    assert Path(got["store"]) == vault / "classifiers"
    assert got["app"] == "Typhon (project typhon_cls)"


# ============================================================
# The Typhon wiring planned for this (not in typhon.gspec yet)
# ============================================================

#: handler -> (function, input ports, {port: result key}, button label).
#: The UI plan, as data: each becomes a script link in typhon.gspec. New
#: ports: classifier_filter (editable combobox: All classifiers / This app /
#: Origin unknown, or typed "App: X", "Tag: Y"), models (listbox — a Qt
#: combobox port takes one string, not a list, so the list of models is a
#: listbox), new_name, tag (entries), export_to (folder picker), import_from
#: (file picker), model_history (listbox), classified_with_line (label).
TYPHON_LINKS = {
    "on_classifier_filter": ("list_classifiers", ["classifier_filter"],
                             {"models": "rows", "classifier_status": "summary"},
                             "Show"),
    "on_btn_refresh": ("list_classifiers", ["classifier_filter"],
                       {"models": "rows", "classifier_status": "summary"},
                       "Refresh"),
    "on_btn_use_selected": ("open_classifier", ["models"],
                            {"classifier_name": "name", "classes": "classes",
                             "classifier_status": "summary"}, "Use selected"),
    "on_btn_save_as": ("save_as",
                       ["classifier_name", "new_name", "classifier_filter"],
                       {"classifier_name": "name", "classes": "classes",
                        "models": "rows", "classifier_status": "summary"},
                       "Save as"),
    "on_btn_rename": ("rename_classifier",
                      ["models", "new_name", "classifier_name",
                       "classifier_filter"],
                      {"classifier_name": "name", "models": "rows",
                       "classifier_status": "summary"}, "Rename"),
    "on_btn_delete": ("delete_classifier",
                      ["models", "classifier_name", "classifier_filter"],
                      {"classifier_name": "name", "classes": "classes",
                       "models": "rows", "classifier_status": "summary"},
                      "Delete"),
    "on_btn_add_tag": ("add_tag", ["models", "tag", "classifier_filter"],
                       {"tag": "cleared", "models": "rows",
                        "classifier_status": "summary"}, "Add tag"),
    "on_btn_remove_tag": ("remove_tag", ["models", "tag", "classifier_filter"],
                          {"models": "rows", "classifier_status": "summary"},
                          "Remove tag"),
    "on_btn_export": ("export_classifier", ["models", "export_to"],
                      {"classifier_status": "summary"}, "Export"),
    "on_btn_export_all_from_this_app": ("export_this_app", ["export_to"],
                                        {"classifier_status": "summary"},
                                        "Export all from this app"),
    "on_btn_import": ("import_classifier",
                      ["import_from", "new_name", "classifier_filter"],
                      {"classifier_name": "name", "classes": "classes",
                       "models": "rows", "classifier_status": "summary"},
                      "Import"),
    "on_btn_versions": ("list_versions", ["models"],
                        {"model_history": "rows",
                         "classifier_status": "summary"}, "Versions"),
    "on_btn_history": ("run_history", ["models"],
                       {"model_history": "rows",
                        "classifier_status": "summary"}, "History"),
    "on_btn_classified_with": ("classified_with", ["capture_folder"],
                               {"classified_with_line": "classified_with"},
                               "Which model?"),
}


class _Port:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value

    def clear(self):
        self.value = None


def test_the_planned_typhon_wiring_runs_as_generated_handlers(
        vault, trained, tmp_path):
    """Every planned link, written by the real emitter (gui_emit's
    handler_stub, what Generate puts in handlers.py), passes the gate and
    runs: inputs read from ports, result keys written to ports — a flow
    through the whole library with no error reported."""
    import gui_emit as ge
    import gui_policy as pol
    folder, _ = trained
    stubs = "".join(ge.handler_stub(h, {"module": "frame_classes",
                                        "function": fn, "inputs": ins,
                                        "outputs": outs}, title)
                    for h, (fn, ins, outs, title) in TYPHON_LINKS.items())
    code = "class Handlers:\n" + stubs
    ok, errs = pol.validate(code, "linked", ["numpy", "PIL", "sklearn"],
                            toolkit="qt")
    assert ok, errs
    space = {}
    exec(compile(code, "handlers.py", "exec"), space)
    out = tmp_path / "out"
    out.mkdir()
    errors = []
    h = space["Handlers"]()
    h.ports = types.SimpleNamespace(**{n: _Port(v) for n, v in {
        "classifier_filter": fc.FILTER_ALL, "models": [],
        "classifier_name": "frames", "classes": [], "classifier_status": "",
        "new_name": "", "tag": "", "export_to": str(out), "import_from": "",
        "capture_folder": str(folder), "classified_with_line": "",
        "model_history": []}.items()})
    h.clear_ports = lambda *names: [getattr(h.ports, n).clear() for n in names]
    h.report_error = lambda what, exc: errors.append(f"{what}: {exc}")
    p = h.ports

    def pick(name):                        # the user clicks a row
        p.models.value = [r for r in p.models.value
                          if fc._name_of(r) == name]

    h.on_classifier_filter()
    assert len(p.models.value) == 1
    pick("frames")
    h.on_btn_use_selected()
    assert p.classifier_name.value == "frames"
    assert p.classes.value == ["good", "bad timing"]
    p.new_name.value = "frames-copy"
    h.on_btn_save_as()
    assert p.classifier_name.value == "frames-copy"
    pick("frames-copy")
    p.tag.value = "lab"
    h.on_btn_add_tag()
    assert p.tag.value == "" and any("tags: lab" in r for r in p.models.value)
    p.classifier_filter.value = "Tag: lab"
    h.on_classifier_filter()
    assert [fc._name_of(r) for r in p.models.value] == ["frames-copy"]
    pick("frames-copy")
    p.new_name.value = "frames-lab"
    h.on_btn_rename()
    assert p.classifier_name.value == "frames-lab"   # it was the open one
    pick("frames-lab")
    h.on_btn_export()
    assert p.classifier_status.value.startswith("Exported frames-lab v1 (")
    h.on_btn_export_all_from_this_app()
    assert "Exported 2 classifier(s)" in p.classifier_status.value
    h.on_btn_versions()
    assert p.model_history.value[0].startswith("v1 (")
    pick("frames-lab")
    h.on_btn_delete()
    assert p.classifier_name.value == "" and p.models.value == []
    p.import_from.value = str(out / "frames-lab-v1.typhon-classifier.zip")
    p.classifier_filter.value = fc.FILTER_ALL
    p.new_name.value = ""
    h.on_btn_import()
    assert p.classifier_name.value == "frames-lab"
    fc.classify_folder("frames", str(folder))
    h.on_btn_classified_with()
    assert p.classified_with_line.value.startswith("Classified with frames v1")
    pick("frames")
    h.on_btn_history()
    assert len(p.model_history.value) == 1
    assert errors == []
