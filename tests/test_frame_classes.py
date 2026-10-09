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
import time
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


@pytest.mark.parametrize("bad", ["..", "a/b", r"..\x", "has space",
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
        ["about.json", "classes.json", "mirror.json", "model.npz", "versions"]
    assert [p.name for p in (store / "versions").iterdir()] == ["v1"]
    assert sorted(p.name for p in (store / "versions" / "v1").iterdir()) == \
        ["classes.json", "meta.json", "model.npz"]


# ============================================================
# The framework pieces it needed
# ============================================================

def test_generated_listboxes_keep_their_own_selection():
    """Tk's default exportselection makes selecting in one listbox CLEAR the
    others — a picked class vanished when the user clicked elsewhere. The
    Tk emitter turns it off on every listbox. (Checked as generated text: a
    test may not open a Tk window. A QListWidget has no such coupling.)"""
    import gui_emit as ge
    import gui_layout as gl
    import gui_shapes as gs
    import gui_spec as gsp
    a = gs.new_shape("listbox", 0, 0); a.id = "a"
    b = gs.new_shape("listbox", 0, 200); b.id = "b"
    spec = gsp.build([a, b], gl.infer([a, b], 400, 400), project="lb")
    src = ge.emit_main_ui(spec)
    assert src.count("exportselection=False") == 2


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
    from PySide6.QtWidgets import QApplication
    qt = QApplication.instance() or QApplication([])
    from app import App
    app = App()                                   # never shown
    def pump(ms):
        end = time.time() + ms / 1000
        while time.time() < end:
            qt.processEvents(); time.sleep(0.005)
    pump(300)
    p = app.ports
    p.capture_folder.set(FRAMES); pump(800)
    cl = p.classes.widget
    for c in ("good", "bad timing"):
        p.new_class.set(c); app.btn_add_class.click(); pump(200)
    def mark(i, cls):
        p.frame.set(i); pump(200)
        cl.clearSelection()
        cl.item(p.classes.items().index(cls)).setSelected(True)
        app.btn_mark_this_frame.click(); pump(150)
    for i in (3, 17): mark(i, "bad timing")
    for i in (0, 8, 20): mark(i, "good")
    app.btn_train.click(); pump(300)
    trained = p.classifier_status.get()
    app.btn_classify_all_frames.click(); pump(800)
    rows = p.predictions.items()
    print("__OUT__" + json.dumps({"trained": trained, "rows": rows,
                                  "current": p.current_frame.get()}))
    app.close()
''')


def test_the_generated_app_marks_trains_and_classifies(tmp_path, capture,
                                                      monkeypatch):
    """Barbie Capture v3, built for Qt and pressed like a user would — Add
    class, Mark this frame, Train, Classify all frames — in its own process,
    offscreen. (The Tk GUIs are deprecated: no test may open a Tk window.)"""
    import run_example_gui as rex
    folder, truth = capture
    vault = tmp_path / "v"
    pdir = rex.build("barbie_capture_v3", project="e2e", vault_dir=vault,
                     target="qt")
    drv = tmp_path / "drive.py"
    drv.write_text(DRIVER, encoding="utf-8")
    env = dict(os.environ, COUNCIL_NO_DIALOGS="1", COUNCIL_VAULT_ROOT=str(vault),
               QT_QPA_PLATFORM="offscreen")
    r = subprocess.run([sys.executable, str(drv), str(folder)], cwd=str(pdir),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180, env=env)
    assert r.returncode == 0, r.stderr[-1500:]
    assert " failed: " not in r.stderr, r.stderr[-1500:]
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


def _names_in(store):
    """What a store holds besides its own hidden folders (.deleted for
    Delete, .locks for two apps changing one classifier)."""
    return sorted(p.name for p in store.iterdir() if not p.name.startswith("."))


def _versions(vault, name="frames"):
    return sorted(p.name for p in (_store(vault) / name / "versions").iterdir())


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
    """A taken name is ASKED about — a soft result that keeps the window on
    the open classifier (see the refused-click test) — never overwritten."""
    fc.add_class("other", "x")
    other = (_store(vault) / "other" / "classes.json").read_bytes()
    for call in (lambda: fc.save_as("frames", "other"),
                 lambda: fc.save_as("frames", "OTHER"),
                 lambda: fc.rename_classifier("frames", "Other", "frames")):
        r = call()
        assert re.search("already exists .*nothing was overwritten",
                         r["summary"]), r["summary"]
        assert r["name"] == "frames"
    assert (_store(vault) / "other" / "classes.json").read_bytes() == other
    assert _names_in(_store(vault)) == ["frames", "other"]


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
    # Another app opens the classifier, tagged shared, and works on it.
    _run_as(monkeypatch, _project(apps, "barbie", "Barbie Capture"))
    fc.add_tag("frames", fc.SHARED_TAG)
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
    """The store is shared: a new version made from Typhon of a classifier
    Barbie shared is Barbie's current model too, and the person pressing
    Train should know."""
    folder, _ = capture
    apps = tmp_path / "apps"
    _run_as(monkeypatch, _project(apps, "barbie", "Barbie Capture"))
    _mark_some(folder)
    first = fc.train("frames")
    assert "belongs to" not in first["summary"]
    fc.add_tag("frames", fc.SHARED_TAG)
    _run_as(monkeypatch, _project(apps, "example_typhon", "Typhon"))
    fc.mark_frame("frames", str(folder), "frame_0011.png", ["bad timing"])
    second = fc.train("frames")
    assert ("'frames' belongs to Barbie Capture (project barbie); "
            f"{second['version']} is its current model there too.") \
        in second["summary"]


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
    both, so no PC may make both. A name typed in another case therefore
    IS the classifier that exists — on every PC, not only where the file
    system says so: a case-sensitive one is acted out here by hiding the
    folder from a lookup by the typed spelling."""
    fc.add_class("frames", "x")
    assert fc._taken("FRAMES") == "frames"
    monkeypatch.setattr(fc, "_is_new", lambda d: d.name != "frames")
    fc.add_class("Frames", "y")
    assert _names_in(_store(vault)) == ["frames"]
    assert fc.open_classifier("FRAMES")["classes"] == ["x", "y"]


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
    asked = fc.import_classifier(e["path"], "", "", "frames")
    assert re.search("'frames' already exists .*type another name in New "
                     "name", asked["summary"]), asked["summary"]
    assert asked["imported"] == [] and asked["name"] == "frames"
    assert asked["classes"] == ["something else"]   # the window keeps its own
    assert (_store(pc2) / "frames" / "classes.json").read_bytes() == mine
    r = fc.import_classifier(e["path"], "frames-lab1")
    assert r["name"] == "frames-lab1" and r["imported"] == ["frames-lab1"]
    again = fc.import_classifier(e["path"], "frames-lab1")
    assert "already is this classifier" in again["summary"]
    assert again["imported"] == []
    assert _names_in(_store(pc2)) == \
        ["frames", "frames-lab1"]                   # no staging left behind


def _not_imported(r, msg):
    """An Import the checks refused: ANSWERED, never raised — an Import
    never blanks the window (import_classifier) — with nothing imported
    and the reason in the summary."""
    assert r["imported"] == [], r["summary"]
    assert re.search(msg, r["summary"]), (msg, r["summary"])
    assert "Nothing was imported" in r["summary"], r["summary"]


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
    _not_imported(fc.import_classifier(str(bad), "x"), msg)
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
    assert set(rec["by"]) == {"app", "project", "project_id", "script",
                              "script_id"}
    assert isinstance(rec["at"], float)             # orders one second's runs
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


def test_classifying_says_the_classified_with_line_it_has_just_made(
        vault, trained, monkeypatch):
    """Typhon's Classify link fills the "classified with" line from this —
    the same words classified_with(folder) gives afterwards, made from the
    record just written, and said plainly when nothing could be recorded."""
    folder, _ = trained
    r = fc.classify_folder("frames", str(folder))
    assert r["classified_with"] == \
        fc.classified_with(str(folder))["classified_with"]
    assert r["classified_with"].startswith(f"Classified with {r['version']} on")
    inside = folder / "store"                    # a store inside the capture
    inside.mkdir()
    shutil.copytree(_store(vault) / "frames", inside / "frames")
    monkeypatch.setenv(fc.STORE_ENV, str(inside))
    r = fc.classify_folder("frames", str(folder))
    assert "NOT recorded" in r["classified_with"]


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
    _not_imported(fc.import_classifier(str(bad)), msg)
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
# The listbox wiring the library was planned against. typhon.gspec wires
# a DROPDOWN instead (the user's choice): its real links are run below
# (test_typhons_own_links_...); this plan stays as the listbox case.
# ============================================================

#: handler -> (function, input ports, {port: result key}, button label).
#: The UI plan, as data: each becomes a script link in typhon.gspec. New
#: ports: classifier_filter (editable combobox: All classifiers / This app /
#: Origin unknown, or typed "App: X", "Tag: Y"), models (listbox — a Qt
#: combobox port takes one string, not a list, so the list of models is a
#: listbox), new_name, tag (entries), export_to (folder picker), import_from
#: (file picker), model_history (listbox), classified_with_line (label).
#:
#: Every action on a row takes classifier_name too: refilling the list drops
#: its selection, so the next press with nothing picked acts on the OPEN
#: classifier — and a refused press echoes the open one back, so the window
#: is never blanked by a slip (Delete alone needs a row picked).
TYPHON_LINKS = {
    "on_classifier_filter": ("list_classifiers", ["classifier_filter"],
                             {"models": "rows", "classifier_status": "summary"},
                             "Show"),
    "on_btn_refresh": ("list_classifiers", ["classifier_filter"],
                       {"models": "rows", "classifier_status": "summary"},
                       "Refresh"),
    "on_btn_use_selected": ("open_classifier", ["models", "classifier_name"],
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
    "on_btn_add_tag": ("add_tag",
                       ["models", "tag", "classifier_filter", "classifier_name"],
                       {"tag": "cleared", "models": "rows",
                        "classifier_status": "summary"}, "Add tag"),
    "on_btn_remove_tag": ("remove_tag",
                          ["models", "tag", "classifier_filter",
                           "classifier_name"],
                          {"models": "rows", "classifier_status": "summary"},
                          "Remove tag"),
    "on_btn_export": ("export_classifier",
                      ["models", "export_to", "classifier_name"],
                      {"classifier_status": "summary"}, "Export"),
    "on_btn_export_all_from_this_app": ("export_this_app", ["export_to"],
                                        {"classifier_status": "summary"},
                                        "Export all from this app"),
    "on_btn_import": ("import_classifier",
                      ["import_from", "new_name", "classifier_filter",
                       "classifier_name"],
                      {"classifier_name": "name", "classes": "classes",
                       "models": "rows", "classifier_status": "summary"},
                      "Import"),
    "on_btn_versions": ("list_versions", ["models", "classifier_name"],
                        {"model_history": "rows",
                         "classifier_status": "summary"}, "Versions"),
    "on_btn_history": ("run_history", ["classifier_name"],
                       {"model_history": "rows",
                        "classifier_status": "summary"}, "History"),
    "on_btn_classified_with": ("classified_with", ["capture_folder"],
                               {"classified_with_line": "classified_with"},
                               "Which model?"),
}


class _Port:
    """An entry, label, combobox or picker port: get() is its text."""

    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value

    def clear(self):
        self.value = ""


class _ListPort:
    """A listbox port as the generated ui/ports.py's _ListPort behaves:
    set() refills the rows and DROPS the selection; get() is the SELECTION,
    not the rows. (Handing back all rows, as the first version of this test
    did, hid that a refreshed list has nothing picked.)"""

    def __init__(self, rows=()):
        self.rows, self.selected = list(rows), []

    def get(self):
        return list(self.selected)

    def items(self):
        return list(self.rows)

    def set(self, values):
        self.rows, self.selected = [str(v) for v in (values or [])], []

    def clear(self):
        self.set([])

    def pick(self, name):                   # the user clicks a row
        self.selected = [r for r in self.rows if fc._name_of(r) == name]
        assert self.selected, (name, self.rows)


def _wired_code():
    import gui_emit as ge
    stubs = "".join(ge.handler_stub(h, {"module": "frame_classes",
                                        "function": fn, "inputs": ins,
                                        "outputs": outs}, title)
                    for h, (fn, ins, outs, title) in TYPHON_LINKS.items())
    return "class Handlers:\n" + stubs


def _planned_app(space, folder, out, name="frames"):
    """The generated Handlers class bound to ports that behave like the
    real ones, the classifier ``name`` open and its classes listed."""
    errors = []
    h = space["Handlers"]()
    ports = {n: _Port(v) for n, v in {
        "classifier_filter": fc.FILTER_ALL, "classifier_name": name,
        "classifier_status": "", "new_name": "", "tag": "",
        "export_to": str(out), "import_from": "",
        "capture_folder": str(folder), "classified_with_line": ""}.items()}
    ports.update(models=_ListPort(), model_history=_ListPort(),
                 classes=_ListPort(fc.open_classifier(name)["classes"]))
    h.ports = types.SimpleNamespace(**ports)
    h.clear_ports = lambda *names: [getattr(h.ports, n).clear() for n in names]
    h.report_error = lambda what, exc: errors.append(f"{what}: {exc}")
    return h, h.ports, errors


def test_the_planned_typhon_wiring_runs_as_generated_handlers(
        vault, trained, tmp_path):
    """Every planned link, written by the real emitter (gui_emit's
    handler_stub, what Generate puts in handlers.py), passes the gate and
    runs: inputs read from ports, result keys written to ports — a flow
    through the whole library with no error reported."""
    import gui_policy as pol
    folder, _ = trained
    code = _wired_code()
    ok, errs = pol.validate(code, "linked", ["numpy", "PIL", "sklearn"],
                            toolkit="qt")
    assert ok, errs
    space = {}
    exec(compile(code, "handlers.py", "exec"), space)
    out = tmp_path / "out"
    out.mkdir()
    h, p, errors = _planned_app(space, folder, out)

    h.on_classifier_filter()
    assert len(p.models.items()) == 1
    p.models.pick("frames")
    h.on_btn_use_selected()
    assert p.classifier_name.value == "frames"
    assert p.classes.items() == ["good", "bad timing"]
    p.new_name.value = "frames-copy"
    h.on_btn_save_as()
    assert p.classifier_name.value == "frames-copy"
    # Nothing is picked now (the list was refilled): the open one is meant.
    p.tag.value = "lab"
    h.on_btn_add_tag()
    assert p.tag.value == "" and any(r.startswith("frames-copy ") and
                                     "tags: lab" in r for r in p.models.items())
    p.tag.value = "lab"
    h.on_btn_remove_tag()
    assert not any("tags: lab" in r for r in p.models.items())
    h.on_btn_add_tag()
    p.classifier_filter.value = "Tag: lab"
    h.on_classifier_filter()
    assert [fc._name_of(r) for r in p.models.items()] == ["frames-copy"]
    p.models.pick("frames-copy")
    p.new_name.value = "frames-lab"
    h.on_btn_rename()
    assert p.classifier_name.value == "frames-lab"   # it was the open one
    h.on_btn_export()
    assert p.classifier_status.value.startswith("Exported frames-lab v1 (")
    h.on_btn_export_all_from_this_app()
    assert "Exported 2 classifiers" in p.classifier_status.value
    h.on_btn_versions()
    assert p.model_history.items()[0].startswith("v1 (")
    p.models.pick("frames-lab")
    h.on_btn_delete()
    assert p.classifier_name.value == "" and p.models.items() == []
    p.import_from.value = str(out / "frames-lab-v1.typhon-classifier.zip")
    p.classifier_filter.value = fc.FILTER_ALL
    p.new_name.value = ""
    h.on_btn_import()
    assert p.classifier_name.value == "frames-lab"
    assert p.classes.items() == ["good", "bad timing"]
    fc.classify_folder("frames", str(folder))
    h.on_btn_classified_with()
    assert p.classified_with_line.value.startswith("Classified with frames v1")
    p.classifier_name.value = "frames"
    h.on_btn_history()
    assert len(p.model_history.items()) == 1
    assert errors == []


# ============================================================
# What three reviews found (2026-10-05) — each test failed before its fix
# ============================================================

def test_a_refused_library_click_keeps_the_window_as_it_was(vault, trained,
                                                            tmp_path):
    """MEASURED before the fix, with real Qt ports: Save as with nothing
    typed or a taken name, Delete or Use selected with nothing picked, and
    Import with no file or a taken name each RAISED — and the generated
    handler clears every port the link fills, so the open classifier's name
    and classes went blank and the next Train failed on ''. Asking for a
    name or a pick is not a failure: it comes back soft, the window as it
    was."""
    folder, _ = trained
    fc.save_as("frames", "other")
    out = tmp_path / "out"
    out.mkdir()
    export = fc.export_classifier("other", str(out))["path"]
    space = {}
    exec(compile(_wired_code(), "handlers.py", "exec"), space)
    h, p, errors = _planned_app(space, folder, out)
    h.on_btn_refresh()
    rows = p.models.items()

    def still_open(what):
        assert errors == [], (what, errors)
        assert p.classifier_name.value == "frames", \
            (what, p.classifier_status.value)
        assert p.classes.items() == ["good", "bad timing"], what
        assert p.models.items() == rows, what

    for what, setup, press in (
            ("Save as, nothing typed", {"new_name": ""}, h.on_btn_save_as),
            ("Save as, a taken name", {"new_name": "OTHER"}, h.on_btn_save_as),
            ("Delete, nothing picked", {}, h.on_btn_delete),
            ("Use selected, nothing picked", {}, h.on_btn_use_selected),
            ("Rename, nothing typed", {"new_name": ""}, h.on_btn_rename),
            ("Import, no file", {"import_from": "", "new_name": ""},
             h.on_btn_import),
            ("Import, a taken name", {"import_from": export,
                                      "new_name": "frames"}, h.on_btn_import)):
        for port, value in setup.items():
            getattr(p, port).value = value
        press()
        still_open(what)
        assert p.classifier_status.value, what       # it says what to do
    assert "already exists" in p.classifier_status.value


def _typhon_links():
    """typhon.gspec's own frame_classes links, as {handler: (function,
    inputs, outputs, label)} — handlers named by shape id. The model box is
    a dropdown there: its text is both the model picked and the one open."""
    doc = json.loads((REPO / "examples" / "gui" / "typhon.gspec").read_text(
        encoding="utf-8"))
    return {f"on_{s['id']}": (s["script"]["function"], s["script"]["inputs"],
                              s["script"]["outputs"], s["label"])
            for s in doc["shapes"]
            if (s.get("script") or {}).get("module") == "frame_classes"}


def _typhon_app(folder, out, name="frames"):
    """Typhon's own frame_classes handlers, as Generate writes them, bound
    to ports shaped like the real ones: the model DROPDOWN and the filter
    hand back their text; the lists their selection."""
    import gui_emit as ge
    import gui_policy as pol
    code = "class Handlers:\n" + "".join(
        ge.handler_stub(h, {"module": "frame_classes", "function": fn,
                            "inputs": ins, "outputs": outs}, title)
        for h, (fn, ins, outs, title) in _typhon_links().items())
    ok, errs = pol.validate(code, "linked", ["numpy", "PIL", "sklearn"],
                            toolkit="qt")
    assert ok, errs
    space = {}
    exec(compile(code, "handlers.py", "exec"), space)
    errors = []
    h = space["Handlers"]()
    ports = {n: _Port(v) for n, v in {
        "classifier_name": name, "classifier_filter": fc.FILTER_ALL,
        "classifier_status": "", "new_name": "", "tag": "", "new_class": "",
        "export_to": str(out), "import_from": "", "current_frame": "",
        "capture_folder": str(folder), "classified_with_line": ""}.items()}
    ports.update(classes=_ListPort(fc.open_classifier(name)["classes"]),
                 predictions=_ListPort())
    h.ports = types.SimpleNamespace(**ports)
    h.clear_ports = lambda *names: [getattr(h.ports, n).clear()
                                    for n in names]
    h.report_error = lambda what, exc: errors.append(f"{what}: {exc}")
    by = {title: getattr(h, handler) for handler, (_f, _i, _o, title)
          in _typhon_links().items()}
    return h, h.ports, errors, by


def test_typhons_own_links_run_the_library_with_the_dropdown(
        vault, trained, tmp_path):
    """typhon.gspec's links, written by the real emitter and gated: a
    refused press keeps the window as it was; then Save as, Rename, tags,
    Export, Export this app's classifiers, Delete, Import back and Classify
    with its "classified with" line — no error reported."""
    folder, _ = trained
    fc.save_as("frames", "other")
    out = tmp_path / "out"
    out.mkdir()
    taken = fc.export_classifier("other", str(out))["path"]
    h, p, errors, press = _typhon_app(folder, out)
    assert {"Model", "Open", "Filter", "Save as", "Rename", "Delete",
            "Export", "Export this app's classifiers", "Import", "Add tag",
            "Remove tag", "Classify all frames"} <= set(press)

    for what, setup, button in (
            ("Save as, nothing typed", {"new_name": ""}, "Save as"),
            ("Save as, a taken name", {"new_name": "OTHER"}, "Save as"),
            ("Rename, nothing typed", {"new_name": ""}, "Rename"),
            ("Import, no file", {"import_from": ""}, "Import"),
            ("Import, a taken name", {"import_from": taken,
                                      "new_name": "frames"}, "Import")):
        for port, value in setup.items():
            getattr(p, port).value = value
        press[button]()
        assert errors == [], (what, errors)
        assert p.classifier_name.value == "frames", (what,
                                                     p.classifier_status.value)
        assert p.classes.items() == ["good", "bad timing"], what
        assert p.classifier_status.value, what
    p.classifier_name.value = ""                    # an empty box
    press["Delete"]()
    assert "Pick the classifier to delete" in p.classifier_status.value
    assert errors == []

    p.classifier_name.value, p.new_name.value = "frames", "frames-copy"
    p.import_from.value = ""
    press["Save as"]()
    assert p.classifier_name.value == "frames-copy"
    p.new_name.value = "frames-lab"
    press["Rename"]()
    assert p.classifier_name.value == "frames-lab"
    p.tag.value = "lab"
    press["Add tag"]()
    assert p.tag.value == "" and "Tagged 'frames-lab': lab" in \
        p.classifier_status.value
    p.tag.value = "lab"
    press["Remove tag"]()
    assert "Took 'lab' off" in p.classifier_status.value
    press["Export"]()
    assert p.classifier_status.value.startswith("Exported frames-lab v1 (")
    press["Export this app's classifiers"]()
    assert "Exported 3 classifiers" in p.classifier_status.value
    press["Delete"]()                               # the one in the box
    assert p.classifier_name.value == "" and p.classes.items() == []
    p.import_from.value = str(out / "frames-lab-v1.typhon-classifier.zip")
    p.new_name.value = ""
    press["Import"]()
    assert p.classifier_name.value == "frames-lab"
    assert p.classes.items() == ["good", "bad timing"]
    press["Classify all frames"]()
    assert p.classified_with_line.value.startswith(
        "Classified with frames-lab v1 (")
    assert len(p.predictions.items()) == 30
    assert errors == []


def test_importing_a_bundle_again_keeps_the_window_on_a_classifier(
        vault, trained, tmp_path, monkeypatch):
    """Importing a bundle again is documented as always safe. MEASURED
    before the fix: the second import returned name '' and classes [], so
    the planned Import link blanked the window."""
    b = fc.export_classifiers(str(tmp_path))["path"]
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(tmp_path / "pc2"))
    first = fc.import_classifier(b)
    again = fc.import_classifier(b)
    assert again["imported"] == [] and "already here 1 (frames)" in \
        again["summary"]
    assert (again["name"], again["classes"]) == \
        (first["name"], first["classes"]) == ("frames", ["good", "bad timing"])


def test_train_makes_a_new_version_past_a_damaged_newest_one(vault, trained):
    """Every damaged-version message says "press Train", so Train has to
    work. MEASURED before the fix: with versions/v2/meta.json cut short,
    Train (pressed twice), list_versions, save_as, predict and export all
    raised that same message, nothing was ever written, and Open called
    the classifier "not trained yet"."""
    folder, _ = trained
    fc.mark_frame("frames", str(folder), "frame_0011.png", ["bad timing"])
    fc.train("frames")
    meta = _store(vault) / "frames" / "versions" / "v2" / "meta.json"
    meta.write_text("{not json", encoding="utf-8")       # a power cut mid-write
    opened = fc.open_classifier("frames")
    assert "not trained yet" not in opened["summary"]
    assert "damaged" in opened["summary"] and "Train" in opened["summary"]
    rows = fc.list_versions("frames")["rows"]
    assert rows[0].startswith("v2   DAMAGED") and rows[1].startswith("v1 (")
    t = fc.train("frames")
    assert t["version"].startswith("frames v3 (")
    assert fc.classify_folder("frames", str(folder))["version"] == t["version"]
    assert meta.read_text(encoding="utf-8") == "{not json"   # left as it is
    assert _versions(vault) == ["v1", "v2", "v3"]


def test_an_export_after_remove_class_imports_alone_and_in_a_bundle(
        vault, capture, tmp_path, monkeypatch):
    """Ordinary steps — mark a class, Train, re-mark that frame, Remove
    class — leave a model that knows a class the marks no longer list.
    MEASURED before the fix: the export said it succeeded; the other PC
    refused it ("the model has classes its classes.json does not"), and
    refused every classifier of a bundle that held it."""
    folder, _ = capture
    _mark_some(folder)
    fc.add_class("frames", "dim")
    fc.mark_frame("frames", str(folder), "frame_0005.png", ["dim"])
    fc.train("frames")
    fc.mark_frame("frames", str(folder), "frame_0005.png", ["good"])
    assert fc.remove_class("frames", ["dim"])["summary"] == "Removed 'dim'."
    for c in ("good", "bad timing"):
        fc.add_class("other", c)
    for n, c in (("frame_0003.png", "bad timing"),
                 ("frame_0017.png", "bad timing"),
                 ("frame_0000.png", "good"), ("frame_0008.png", "good")):
        fc.mark_frame("other", str(folder), n, [c])
    fc.train("other")
    out = tmp_path / "out"
    out.mkdir()
    single = fc.export_classifier("frames", str(out))["path"]
    bundle = fc.export_classifiers(str(out))["path"]
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(tmp_path / "pc2"))
    r = fc.import_classifier(single)
    assert r["imported"] == ["frames"] and "dim" in r["classes"]
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(tmp_path / "pc3"))
    assert sorted(fc.import_classifier(bundle)["imported"]) == \
        ["frames", "other"]
    # A file written before the fix — marks without the model's class —
    # imports too.
    with zipfile.ZipFile(single) as zf:
        doc = json.loads(zf.read("classes.json"))
    doc["classes"] = [c for c in doc["classes"] if c != "dim"]
    old = _rezip(single, tmp_path / "old.typhon-classifier.zip",
                 change={"classes.json": json.dumps(doc).encode()})
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(tmp_path / "pc4"))
    assert "dim" in fc.import_classifier(str(old))["classes"]


def test_an_imported_version_number_leaves_room_to_train(vault, trained,
                                                         tmp_path, monkeypatch):
    """MEASURED before the fix: an import claiming v999999 (the checksums
    are the file's own, so anyone can write one) was accepted; the next
    Train wrote v1000000, a folder name the version reader does not match,
    so the old model stayed current and every Open wrote one more."""
    folder, _ = trained
    e = fc.export_classifier("frames", str(tmp_path))["path"]
    with zipfile.ZipFile(e) as zf:
        meta = json.loads(zf.read("meta.json"))

    def numbered(n):
        return str(_rezip(e, tmp_path / f"v{n}.typhon-classifier.zip",
                          change={"meta.json": json.dumps(
                              dict(meta, version=n)).encode()},
                          manifest={"version": n}))
    pc2 = tmp_path / "pc2"
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(pc2))
    _not_imported(fc.import_classifier(numbered(999999), "big"),
                  "version 999999")
    top = fc._IMPORT_VERSION_MAX
    fc.import_classifier(numbered(top), "lab")
    fc.mark_frame("lab", str(folder), "frame_0011.png", ["bad timing"])
    t = fc.train("lab")
    assert t["version"].startswith(f"lab v{top + 1} (")
    for _ in range(3):
        assert fc.open_classifier("lab")["version"] == t["version"]
    assert len(list((_store(pc2) / "lab" / "versions").iterdir())) == 2
    with pytest.raises(RuntimeError, match="last version number"):
        fc._write_version(_store(pc2) / "lab", b"", {}, {},
                          number=fc._VERSION_MAX + 1)


def test_about_json_from_a_newer_build_is_never_downgraded(vault, trained):
    """A GUI made at a later day may share the vault and write a newer
    about.json. MEASURED before the fix: one Add tag rewrote it as format 1
    and dropped the keys this build does not know; Save as dropped them
    from the copy."""
    p = _store(vault) / "frames" / "about.json"
    doc = json.loads(p.read_text(encoding="utf-8"))
    newer = dict(doc, format_version=2, owner="lab B")
    p.write_text(json.dumps(newer), encoding="utf-8")
    assert "From " in fc.open_classifier("frames")["summary"]   # still read
    for call in (lambda: fc.add_tag("frames", "rig A"),
                 lambda: fc.save_as("frames", "copy")):
        with pytest.raises(RuntimeError, match="newer build"):
            call()
    assert json.loads(p.read_text(encoding="utf-8")) == newer
    assert _names_in(_store(vault)) == ["frames"]
    # The same format with a key this build does not know: kept.
    p.write_text(json.dumps(dict(doc, owner="lab B")), encoding="utf-8")
    fc.add_tag("frames", "rig A")
    kept = json.loads(p.read_text(encoding="utf-8"))
    assert kept["owner"] == "lab B" and kept["tags"] == ["rig A"]
    fc.save_as("frames", "copy")
    assert _about(vault, "copy")["owner"] == "lab B"


def test_paths_carried_in_by_an_import_are_never_opened_here(
        vault, trained, tmp_path, monkeypatch):
    """A mark or a run record from another PC names a path on THAT PC.
    MEASURED before the fix: Train stat-ed every imported mark, and "Which
    model?" resolved every host-less record's folder — a UNC path among
    them is an SMB connection to whatever host the file names. And a mark
    whose path happened to exist here was read from THIS PC's file instead
    of the features it was trained on."""
    folder, _ = trained
    fc.classify_folder("frames", str(folder))
    e = fc.export_classifier("frames", str(tmp_path))["path"]
    unc = "\\\\attacker-host.invalid\\share"
    with zipfile.ZipFile(e) as zf:
        doc = json.loads(zf.read("classes.json"))
        rec = json.loads(zf.read("runs.jsonl").decode().splitlines()[0])
    doc["labels"][unc + "\\frame_0001.png"] = "good"
    rec.update(folder=unc + "\\cap", host="")
    forged = _rezip(e, tmp_path / "forged.typhon-classifier.zip", change={
        "classes.json": json.dumps(doc).encode(),
        "runs.jsonl": (json.dumps(rec) + "\n").encode()})
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(tmp_path / "pc2"))
    fc.import_classifier(str(forged))
    _good(folder / "frame_0003.png", 200)     # here, that file is another picture
    seen = []

    def guard(real, what):
        def call(p, *a, **k):
            if "attacker-host" in str(p):
                seen.append((what, str(p)))
                raise OSError(f"blocked: {p}")
            return real(p, *a, **k)
        return call
    monkeypatch.setattr(os, "stat", guard(os.stat, "stat"))
    monkeypatch.setattr(os.path, "realpath",
                        guard(os.path.realpath, "realpath"))
    fc.mark_frame("frames", str(folder), "frame_0011.png", ["bad timing"])
    t = fc.train("frames")
    fc.classified_with(str(folder))
    fc.run_history("", str(folder))
    assert seen == []
    assert "Reused the stored features of 5" in t["summary"], t["summary"]
    assert "on 6 frames" in t["summary"]


def _bulky(vault, name, rows):
    """A trained classifier of ``rows`` all-zero frames: a model that
    unpacks large and compresses to almost nothing. Returns its size
    unpacked."""
    d = _store(vault) / name
    d.mkdir(parents=True)
    marks = {"classes": ["a", "b"], "labels": {}}
    (d / "classes.json").write_text(json.dumps(dict(marks, version=1)),
                                    encoding="utf-8")
    fc._write_about(d, {"origin": fc._new_origin(), "lineage": [], "tags": []})
    model = {"X": np.zeros((rows, 1092), np.float32), "y": np.arange(rows) % 2,
             "classes": ["a", "b"], "paths": [f"p{i}" for i in range(rows)]}
    meta = {"sha256": fc._model_sha(model), "created": fc._now(),
            "how": "trained", "frames": rows,
            "frames_per_class": fc._per_class(model), "classes": ["a", "b"],
            "accuracy": "", "feature_layout": fc.FEATURE_LAYOUT,
            "feature_length": 1092}
    fc._write_version(d, fc._npz_bytes(model), marks, meta)
    return rows * 1092 * 4


def test_a_bundle_unpacks_one_model_at_a_time_within_its_cap(vault, tmp_path,
                                                             monkeypatch):
    """MEASURED before the fix: a 0.02 MB bundle of five such models held
    526 MB once checked — every model unpacked and kept until the last —
    so the 1 GB bundle cap was really 1 GB per classifier in it."""
    import tracemalloc
    one = 0
    for k in range(3):
        one = _bulky(vault, f"big{k}", 6000)
    out = tmp_path / "out"
    out.mkdir()
    path = Path(fc.export_classifiers(str(out))["path"])
    assert path.stat().st_size < one / 10
    tracemalloc.start()
    try:
        _index, items = fc._read_bundle(path)
        held, _peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(items) == 3 and held < one, f"held {held >> 20} MB"
    del items
    monkeypatch.setattr(fc, "MAX_BUNDLE_BYTES", int(one * 2.5))
    with pytest.raises(RuntimeError, match="unpacks to more than"):
        fc._read_bundle(path)


def test_malformed_records_are_skipped_or_refused_never_a_crash(
        vault, trained, tmp_path, monkeypatch):
    """MEASURED before the fix: one run record with a NUL in its folder made
    "Which model?" raise ValueError for EVERY folder on the PC (Delete did
    not help: deleted classifiers' records are searched too); counts that
    were a list, or frames_per_class that was text, raised AttributeError;
    an import whose origin was {} was neither known nor "Origin unknown";
    a refused write of about.json surfaced as a raw PermissionError."""
    folder, _ = trained
    fc.classify_folder("frames", str(folder))
    runs = _store(vault) / "frames" / fc.RUNS
    with open(runs, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"when": "2026-10-05 10:00:00",
                             "folder": "C:\\cap\x00x", "host": ""}) + "\n")
        fh.write(json.dumps({"when": "2026-10-05 10:00:01",
                             "folder": str(folder.resolve()),
                             "counts": ["x"]}) + "\n")
    h = fc.run_history("frames")
    assert len(h["records"]) == 1 and "2 line(s)" in h["summary"]
    assert fc.classified_with(str(folder))["classified_with"].startswith(
        "Classified with frames v1")
    meta_p = _store(vault) / "frames" / "versions" / "v1" / "meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    meta_p.write_text(json.dumps(dict(meta, frames_per_class="x")),
                      encoding="utf-8")
    assert fc.list_versions("frames")["rows"][0].startswith("v1 (")
    meta_p.write_text(json.dumps(meta), encoding="utf-8")
    e = fc.export_classifier("frames", str(tmp_path))["path"]
    with zipfile.ZipFile(e) as zf:
        m = json.loads(zf.read("meta.json"))
    bad_meta = _rezip(e, tmp_path / "m.typhon-classifier.zip", change={
        "meta.json": json.dumps(dict(m, frames_per_class="x")).encode()})
    bad_runs = _rezip(e, tmp_path / "r.typhon-classifier.zip", change={
        "runs.jsonl": (json.dumps({"folder": "C:\\cap\x00x", "host": ""})
                       + "\n").encode()})
    no_origin = _rezip(e, tmp_path / "o.typhon-classifier.zip",
                       manifest={"origin": {}})
    nan_origin = _rezip(e, tmp_path / "n.typhon-classifier.zip",
                        manifest={"origin": {"app": "x", "n": float("nan")}})
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(tmp_path / "pc2"))
    for bad, msg in ((bad_meta, "meta.json does not describe"),
                     (bad_runs, "run record is not readable"),
                     (nan_origin, "where it came from is not readable")):
        _not_imported(fc.import_classifier(str(bad)), msg)
    fc.import_classifier(str(no_origin), "anon")
    assert fc.list_classifiers(fc.FILTER_UNKNOWN)["names"] == ["anon"]
    assert fc.open_classifier("anon")["origin"] == fc.UNKNOWN_ORIGIN

    def refused(path, obj):
        raise PermissionError(13, "Access is denied", str(path))
    monkeypatch.setattr(fc, "_write_json", refused)
    with pytest.raises(RuntimeError, match="cannot save"):
        fc.add_tag("anon", "rig A")


RACE = textwrap.dedent('''
    import json, sys, time
    from pathlib import Path
    repo, mode, who, start, frames = sys.argv[1:6]
    sys.path.insert(0, repo)
    import frame_classes as fc
    if mode == "train":
        fc._require_sklearn()
    out, errors = None, []
    while time.time() < float(start):
        time.sleep(0.001)
    if mode == "mark":
        for i in range(40):
            try:
                fc.mark_frame("frames", frames, f"{who}_{i:02d}.png",
                              ["good" if i % 2 else "bad timing"])
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
    elif mode == "open":
        out = fc.open_classifier("frames")["version"]
    elif mode == "train":
        out = fc.train("frames")["version"]
    elif mode == "record":
        d = fc.store_dir("frames")
        for i in range(300):
            why = fc._append_run(d, {"who": who, "i": i})
            if why:
                errors.append(why)
    print("__OUT__" + json.dumps({"out": out, "errors": errors}))
''')


def _race(tmp_path, mode, who, frames=""):
    """One process per letter of ``who``, all starting ``mode`` at the same
    instant — apps sharing the vault."""
    drv = tmp_path / "race.py"
    drv.write_text(RACE, encoding="utf-8")
    env = dict(os.environ)
    env.pop(fc.STORE_ENV, None)
    start = time.time() + 5.0
    procs = [subprocess.Popen(
        [sys.executable, str(drv), str(REPO), mode, w, str(start), str(frames)],
        cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", env=env) for w in who]
    outs = []
    for proc in procs:
        so, se = proc.communicate(timeout=240)
        assert proc.returncode == 0, se[-1500:]
        outs.append(json.loads(next(l for l in so.splitlines()
                                    if l.startswith("__OUT__"))[7:]))
    return outs


def test_two_apps_marking_one_classifier_at_once_lose_no_mark(vault, tmp_path):
    """MEASURED before the fix: two processes marking 40 frames each of one
    classifier at the same time kept 39 of 80 marks — 35 reported as saved
    and silently lost — because each read, changed and wrote classes.json
    with nothing between them."""
    frames = tmp_path / "frames"
    frames.mkdir()
    for who in "AB":
        for i in range(40):
            Image.new("L", (8, 8), i).save(frames / f"{who}_{i:02d}.png")
    for c in ("good", "bad timing"):
        fc.add_class("frames", c)
    fc.add_tag("frames", "shared")                  # both apps may change it
    outs = _race(tmp_path, "mark", "AB", frames)
    assert [o["errors"] for o in outs] == [[], []]
    labels = json.loads((_store(vault) / "frames" / "classes.json")
                        .read_text(encoding="utf-8"))["labels"]
    assert len(labels) == 80


def test_two_apps_opening_or_training_at_once_make_one_version(vault, capture,
                                                               tmp_path):
    """"Two numbers for one model would make the run record ambiguous."
    MEASURED before the fix: three apps opening a classifier from before
    versions at once made v1, v2 and v3 of one model; two pressing Train
    at once made two versions of identical content."""
    folder, _ = capture
    _legacy(vault, folder)                  # only model.npz: Open adopts it
    outs = _race(tmp_path, "open", "ABC")
    assert len({o["out"] for o in outs}) == 1 and _versions(vault) == ["v1"]
    fc.mark_frame("frames", str(folder), "frame_0011.png", ["bad timing"])
    outs = _race(tmp_path, "train", "AB")
    assert len({o["out"] for o in outs}) == 1
    assert _versions(vault) == ["v1", "v2"]


def test_two_apps_recording_runs_at_once_lose_no_record(vault, trained,
                                                        tmp_path):
    """Appending is seek-to-end-then-write on Windows, not one step across
    processes. MEASURED before the fix (in 1 run of 3): two processes
    appending 400 records each left 799 — one written over."""
    outs = _race(tmp_path, "record", "AB")
    assert [o["errors"] for o in outs] == [[], []]
    lines = (_store(vault) / "frames" / fc.RUNS).read_text(
        encoding="utf-8").splitlines()
    got = {(r["who"], r["i"]) for r in map(json.loads, lines)}
    assert len(lines) == 600 and len(got) == 600


def test_a_store_inside_the_capture_folder_never_writes_there(
        vault, trained, tmp_path, monkeypatch):
    """The capture folder is only ever read. MEASURED before the fix: with
    FRAME_CLASSES_STORE pointing at a capture folder (or a relative value
    and a shortcut starting in one), classify_folder said "recorded" and
    the capture folder gained the record."""
    folder, _ = trained
    e = fc.export_classifier("frames", str(tmp_path))["path"]
    monkeypatch.setenv(fc.STORE_ENV, str(folder))   # a slip: the capture folder
    fc.import_classifier(e)
    assert "capture folder" in fc.store_info()["summary"]

    def snap():
        return sorted((str(p), p.stat().st_mtime_ns) for p in folder.rglob("*"))
    before = snap()
    r = fc.classify_folder("frames", str(folder))
    assert "NOT recorded" in r["summary"] and "inside" in r["summary"]
    for call in (lambda: fc.mark_frame("frames", str(folder), "frame_0001.png",
                                       ["good"]),
                 lambda: fc.train("frames")):
        with pytest.raises(RuntimeError, match="inside the capture folder"):
            call()
    assert snap() == before


@pytest.mark.parametrize("stamp", ["kept", "from before stamps"])
def test_a_crash_before_the_copy_was_refreshed_never_reverts_the_model(
        vault, trained, stamp):
    """The crash: v2 renamed into place, the top-level copy never refreshed.
    MEASURED before the fix: with v1's meta.json unreadable as well, a
    read-only Open took the stale copy of v1 for an older build's Train,
    made it v3 and current — the user's v2 replaced with no Train."""
    folder, _ = trained
    d = _store(vault) / "frames"
    root, mirror = d / "model.npz", d / "mirror.json"
    old_bytes, st = root.read_bytes(), root.stat()
    old_stamp = mirror.read_bytes() if mirror.exists() else None
    fc.mark_frame("frames", str(folder), "frame_0011.png", ["bad timing"])
    v2 = fc.train("frames")
    root.write_bytes(old_bytes)
    os.utime(root, ns=(st.st_atime_ns, st.st_mtime_ns))
    if stamp == "kept" and old_stamp is not None:
        mirror.write_bytes(old_stamp)
    elif mirror.exists():
        mirror.unlink()
    (d / "versions" / "v1" / "meta.json").write_text("", encoding="utf-8")
    assert fc.open_classifier("frames")["version"] == v2["version"]
    assert _versions(vault) == ["v1", "v2"]
    with np.load(root, allow_pickle=False) as m:
        top = {k: m[k] for k in m.files}
    assert fc._model_sha(top) == v2["sha256"]


def test_an_older_builds_train_is_adopted_even_when_it_matches_an_old_version(
        vault, trained):
    """An older build knows only model.npz. MEASURED before the fix: when
    its Train equalled an earlier version (its user put a mark back), the
    next Open took it for a stale copy and wrote the newer version over it
    — that build's Train silently undone."""
    folder, _ = trained
    d = _store(vault) / "frames"
    v1_sha = json.loads((d / "versions" / "v1" / "meta.json")
                        .read_text(encoding="utf-8"))["sha256"]
    v1_bytes = (d / "versions" / "v1" / "model.npz").read_bytes()
    fc.mark_frame("frames", str(folder), "frame_0011.png", ["bad timing"])
    fc.train("frames")
    tmp = d / "model.npz.older-build"
    tmp.write_bytes(v1_bytes)
    os.replace(tmp, d / "model.npz")                # how an older build saves
    r = fc.open_classifier("frames")
    assert "older build is now frames v3" in r["summary"], r["summary"]
    assert r["version"] == f"frames v3 ({v1_sha[:8]})"


def test_a_classifier_moved_away_mid_classify_or_train_is_not_made_again(
        vault, trained, tmp_path, monkeypatch):
    """MEASURED before the fix: Delete or Rename from another app while a
    classify ran left a new "frames" folder holding only that run's record
    — listed as a classifier of unknown origin — and the next classifier
    made under that name never recorded its origin."""
    folder, _ = trained
    store = _store(vault)
    real_load, real_accuracy = fc._load_model, fc._accuracy

    def gone(to):
        os.rename(store / "frames", tmp_path / to)    # another app deletes it

    def load_then_gone(name):
        got = real_load(name)
        gone("deleted1")
        return got
    monkeypatch.setattr(fc, "_load_model", load_then_gone)
    r = fc.classify_folder("frames", str(folder))
    assert "NOT recorded" in r["summary"] and "moved or deleted" in r["summary"]
    assert not (store / "frames").exists()
    monkeypatch.setattr(fc, "_load_model", real_load)
    os.rename(tmp_path / "deleted1", store / "frames")
    fc.mark_frame("frames", str(folder), "frame_0011.png", ["bad timing"])

    def accuracy_then_gone(X, y):
        gone("deleted2")
        return real_accuracy(X, y)
    monkeypatch.setattr(fc, "_accuracy", accuracy_then_gone)
    with pytest.raises(RuntimeError, match="moved or deleted"):
        fc.train("frames")
    assert not (store / "frames").exists()
    # A stray run record alone is not a classifier: one made there now
    # records the app that made it.
    stray = store / "frames"
    stray.mkdir()
    (stray / fc.RUNS).write_text("{}\n", encoding="utf-8")
    _run_as(monkeypatch, _project(tmp_path / "apps", "example_typhon",
                                  "Typhon"))
    fc.add_class("frames", "good")
    assert _about(vault, "frames")["origin"]["app"] == "Typhon"


def test_the_latest_record_is_the_last_one_made_within_one_second(
        vault, trained, monkeypatch):
    """Records said when to the second. MEASURED before the fix: two
    classifiers classifying one folder in the same second, or classify /
    Train / classify within one, gave "Classified with" the FIRST — a
    stable sort kept file order among equal times."""
    folder, _ = trained
    fc.save_as("frames", "frames-b")
    monkeypatch.setattr(fc, "_now", lambda: "2026-10-05 10:18:09")
    fc.classify_folder("frames", str(folder))
    fc.classify_folder("frames-b", str(folder))
    assert fc.classified_with(str(folder))["classified_with"].startswith(
        "Classified with frames-b v1")
    fc.mark_frame("frames", str(folder), "frame_0011.png", ["bad timing"])
    v2 = fc.train("frames")["version"]
    fc.classify_folder("frames", str(folder))
    assert fc.run_history("frames", str(folder))["latest"].startswith(
        f"Classified with {v2}")


def test_a_name_typed_in_another_case_is_the_existing_classifier(
        vault, trained, tmp_path):
    """MEASURED before the fix: classify_folder('FRAMES') recorded
    'FRAMES v1 (...)' and classifier 'FRAMES' while the list said 'frames';
    the export was FRAMES-v1 and arrived on the other PC as 'FRAMES'."""
    folder, _ = trained
    r = fc.classify_folder("FRAMES", str(folder))
    assert r["version"].startswith("frames v1 (")
    rec = json.loads((_store(vault) / "frames" / fc.RUNS)
                     .read_text(encoding="utf-8").splitlines()[-1])
    assert rec["classifier"] == "frames" and rec["version_id"] == r["version"]
    e = fc.export_classifier("Frames", str(tmp_path))
    assert Path(e["path"]).name == "frames-v1.typhon-classifier.zip"
    assert "'frames'" in fc.add_class("FRAMES", "dim")["summary"]
    assert _names_in(_store(vault)) == ["frames"]


def test_an_app_does_not_change_another_apps_classifier(vault, tmp_path,
                                                        monkeypatch, capture):
    """Every shipped capture app names its classifier "frames" by default.
    MEASURED before the fix (Barbie v5 and Typhon built into one vault):
    Typhon, its name box left alone, added classes and marks to Barbie's
    classifier and its Train became Barbie's current model — nothing asked.
    Another app's classifier is now read-only to this one until it is
    copied (Save as) or tagged shared."""
    folder, _ = capture
    apps = tmp_path / "apps"
    _run_as(monkeypatch, _project(apps, "example_barbie_capture_v5",
                                  "Barbie Capture v5 — live"))
    _mark_some(folder)
    v1 = fc.train("frames")["version"]
    _run_as(monkeypatch, _project(apps, "example_typhon", "Typhon"))
    marks = (_store(vault) / "frames" / "classes.json").read_bytes()
    # Add class and Remove class ANSWER (the window keeps its classes, see
    # test_another_apps_model_refuses_a_class_change_without_blanking_it);
    # Mark and Train fill only the status, and raise.
    for r in (fc.add_class("frames", "dim"),
              fc.remove_class("frames", ["good"])):
        assert re.search("belongs to Barbie Capture v5 — live .*Save as",
                         r["summary"]), r["summary"]
        assert r["classes"] == ["good", "bad timing"]
    for call in (lambda: fc.mark_frame("frames", str(folder), "frame_0011.png",
                                       ["bad timing"]),
                 lambda: fc.train("frames")):
        with pytest.raises(RuntimeError, match="belongs to Barbie Capture v5 "
                                               "— live .*Save as"):
            call()
    assert (_store(vault) / "frames" / "classes.json").read_bytes() == marks
    assert fc.open_classifier("frames")["version"] == v1      # reading is fine
    assert fc.classify_folder("frames", str(folder))["version"] == v1
    fc.save_as("frames", "typhon-frames")                     # Typhon's own copy
    assert "Added 'dim'" in fc.add_class("typhon-frames", "dim")["summary"]
    assert _about(vault, "typhon-frames")["origin"]["app"] == \
        "Barbie Capture v5 — live"                            # still its origin
    fc.add_tag("frames", fc.SHARED_TAG)                       # every app's now
    fc.mark_frame("frames", str(folder), "frame_0011.png", ["bad timing"])
    t = fc.train("frames")
    assert "belongs to Barbie Capture v5 — live" in t["summary"]
    assert "current model there too" in t["summary"]


def test_which_model_resolves_only_the_folder_asked_about(vault, trained,
                                                          tmp_path, monkeypatch):
    """MEASURED before the fix: classified_with resolved every record's
    folder on the UI thread — 4 s at 5,000 records, 8-15 s at 20,000."""
    folder, _ = trained
    fc.classify_folder("frames", str(folder))
    runs = _store(vault) / "frames" / fc.RUNS
    rec = json.loads(runs.read_text(encoding="utf-8").splitlines()[0])
    with open(runs, "a", encoding="utf-8") as fh:
        for i in range(2000):
            fh.write(json.dumps(dict(rec, folder=str(tmp_path / f"c{i % 40}")))
                     + "\n")
    calls = []
    real = os.path.realpath
    monkeypatch.setattr(os.path, "realpath",
                        lambda p, *a, **k: (calls.append(p), real(p, *a, **k))[1])
    line = fc.classified_with(str(folder))["classified_with"]
    assert line.startswith("Classified with frames v1") and len(calls) <= 2


def test_two_standalone_apps_both_called_main_py_are_not_one_app(
        vault, tmp_path, monkeypatch):
    """An app of its own need not be a Designer project. MEASURED before the
    fix: two different folders' main.py sharing the vault each listed — and
    would have exported — the other's classifiers as "This app"."""
    for name in ("appA", "appB", "moved/appA"):
        (tmp_path / name).mkdir(parents=True)
        (tmp_path / name / "main.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "appA" / "main.py")])
    fc.add_class("a-model", "x")
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "appB" / "main.py")])
    assert fc.list_classifiers(fc.FILTER_THIS_APP)["names"] == []
    fc.add_class("b-model", "x")
    assert fc.list_classifiers(fc.FILTER_THIS_APP)["names"] == ["b-model"]
    # The same app copied to another place keeps its classifiers.
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "moved/appA" / "main.py")])
    assert fc.list_classifiers(fc.FILTER_THIS_APP)["names"] == ["a-model"]


def test_the_app_filter_matches_part_of_a_title_whatever_its_dashes(
        vault, tmp_path, monkeypatch):
    """MEASURED before the fix: for a classifier from "Barbie Capture v5 —
    live", the filters "Barbie", "barbie capture", "App: Barbie Capture v5"
    and "App: Barbie Capture v5 - live" all found nothing — only the exact
    title, em dash included, did."""
    _run_as(monkeypatch, _project(tmp_path / "apps",
                                  "example_barbie_capture_v5",
                                  "Barbie Capture v5 — live"))
    fc.add_class("frames", "ok")
    for show in ("Barbie", "barbie capture", "App: Barbie Capture v5",
                 "App: Barbie Capture v5 - live",
                 "app: barbie  capture v5 – live", "Project: barbie_capture"):
        assert fc.list_classifiers(show)["names"] == ["frames"], show
    assert fc.list_classifiers("App: Typhon")["names"] == []


def test_library_summaries_lead_short_and_name_no_folders(vault, trained,
                                                          tmp_path, monkeypatch):
    """Typhon's status label is 336x72. MEASURED before the fix: the Save
    as, Export, Export all, Delete and Import summaries were 2 to 5 times
    its height, with full paths as single unbreakable words. The paths are
    in the "path" and "moved_to" keys."""
    out = tmp_path / "out"
    out.mkdir()
    store = str(_store(vault))
    e = fc.export_classifier("frames", str(out))
    texts = {"export": e["summary"],
             "save_as": fc.save_as("frames", "frames2")["summary"],
             "export_all": fc.export_classifiers(str(out))["summary"],
             "delete": fc.delete_classifier(["frames2"], "frames")["summary"],
             "rename": fc.rename_classifier("frames", "frames3",
                                            "frames")["summary"]}
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(tmp_path / "pc2"))
    texts["import"] = fc.import_classifier(e["path"])["summary"]
    texts["open"] = fc.open_classifier("frames")["summary"]
    for what, text in texts.items():
        assert len(text) <= 160, (what, len(text), text)
        assert store not in text and str(out) not in text, (what, text)


def test_an_empty_name_says_to_pick_or_type_one(vault):
    """MEASURED before the fix: "'' is not a usable classifier name — use
    letters, digits ..." — what Use selected said with nothing picked.

    Add class with no model ANSWERS, and keeps the class typed: a fresh
    Typhon's model box starts empty, and a raise made the handler blank the
    New class the user had just typed (2026-10-06)."""
    for call in (lambda: fc.train(""),
                 lambda: fc.predict_frame("", "x", "y")):
        with pytest.raises(RuntimeError, match="pick a classifier in the "
                                               "list, or type its name"):
            call()
    r = fc.add_class("", "a")
    assert (r["classes"], r["cleared"]) == ([], "a")
    assert r["summary"].startswith("Pick a saved model, or type a new")
    assert not _store(vault).exists() or _names_in(_store(vault)) == []
    r = fc.open_classifier("")
    assert (r["name"], r["classes"]) == ("", [])
    assert r["summary"].startswith("Pick a classifier")


def test_the_list_summary_reads_as_sentences(vault, tmp_path, monkeypatch):
    """MEASURED before the fix: "1 saved classifier: frames Kept in ..."
    (no full stop) and "(This app (Typhon (project example_typhon)))"."""
    own = tmp_path / "own"
    monkeypatch.setenv(fc.STORE_ENV, str(own))
    fc.add_class("frames", "x")
    assert fc.list_classifiers()["summary"] == \
        f"1 saved classifier: frames. Kept in {own} (set by {fc.STORE_ENV})."
    _run_as(monkeypatch, _project(tmp_path / "apps", "example_typhon",
                                  "Typhon"))
    fc.add_class("t1", "x")
    s = fc.list_classifiers(fc.FILTER_THIS_APP)["summary"]
    assert s.startswith("1 of 2 saved classifiers (This app: Typhon, project "
                        "example_typhon): t1. Kept in "), s
    assert "((" not in s and "))" not in s


# ============================================================
# What the fourth review found (2026-10-06) — each test failed before its fix
# ============================================================

def _barbie_then_typhon(tmp_path, monkeypatch, name="theirs"):
    """Barbie (another app on this PC) makes ``name`` with two classes;
    this process then runs as Typhon. Returns (barbie, typhon) folders."""
    apps = tmp_path / "apps"
    barbie = _project(apps, "example_barbie_capture_v5",
                      "Barbie Capture v5 — live")
    _run_as(monkeypatch, barbie)
    for c in ("good", "bad timing"):
        fc.add_class(name, c)
    typhon = _project(apps, "birdlab", "Typhon")
    _run_as(monkeypatch, typhon)
    return barbie, typhon


def test_a_mistake_in_a_library_press_never_blanks_the_open_model(
        vault, trained, tmp_path):
    """MEASURED before the fix, through Typhon's own links: an unusable new
    name for Save as ('my birds') or Rename ('a/b'), and an Import of a zip
    holding '../../escape.txt', of a file that is not a zip, or of a path
    that is not there, each RAISED — and a generated handler clears every
    port its link fills, so the model box and the class list went blank
    (import_bundle's docstring: "an Import never blanks the window"). Each
    is a mistake the user corrects, so each now answers softly: the open
    model as it was, the status saying why."""
    folder, _ = trained
    out = tmp_path / "out"
    out.mkdir()
    export = fc.export_classifier("frames", str(out))["path"]
    escape = tmp_path / "escape.typhon-classifier.zip"
    with zipfile.ZipFile(escape, "w") as zf:
        zf.writestr("../../escape.txt", "x")
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"not a zip at all")
    h, p, errors, press = _typhon_app(folder, out)
    before = _names_in(_store(vault))
    for what, setup, button, says in (
            ("Save as, an unusable name", {"new_name": "my birds"}, "Save as",
             "'my birds' is not a usable classifier name"),
            ("Rename, an unusable name", {"new_name": "a/b"}, "Rename",
             "'a/b' is not a usable classifier name"),
            ("Import, a member outside", {"import_from": str(escape),
                                          "new_name": ""}, "Import",
             "Nothing was imported"),
            ("Import, not a zip", {"import_from": str(junk)}, "Import",
             "Nothing was imported"),
            ("Import, no such file", {"import_from": str(tmp_path / "no.zip")},
             "Import", "is not a file"),
            ("Import, an unusable name", {"import_from": export,
                                          "new_name": "has space"}, "Import",
             "'has space' is not a usable classifier name")):
        for port, value in setup.items():
            getattr(p, port).value = value
        press[button]()
        assert errors == [], (what, errors)
        assert p.classifier_name.value == "frames", \
            (what, p.classifier_status.value)
        assert p.classes.items() == ["good", "bad timing"], what
        assert says in p.classifier_status.value, \
            (what, p.classifier_status.value)
    assert _names_in(_store(vault)) == before
    assert not list(tmp_path.parent.glob("escape.txt"))
    # Delete of a name typed that is not saved: said, the name kept.
    p.classifier_name.value = "ghost"
    press["Delete"]()
    assert errors == [] and p.classifier_name.value == "ghost"
    assert "no classifier 'ghost'" in p.classifier_status.value


def test_another_apps_model_refuses_a_class_change_without_blanking_it(
        vault, tmp_path, monkeypatch):
    """MEASURED before the fix: with Barbie's model open in Typhon, Add
    class and Remove class were refused ("belongs to Barbie") by RAISING,
    and the handler blanked the class list and the class just typed. The
    refusal is an answer, like "still labels 3 frames": the classes stay,
    and so does the typed class, for the Save as the message suggests."""
    _barbie_then_typhon(tmp_path, monkeypatch)
    h, p, errors, press = _typhon_app(tmp_path, tmp_path, name="theirs")
    p.new_class.value = "dim"
    press["Add class"]()
    assert p.classes.items() == ["good", "bad timing"]
    assert p.new_class.value == "dim"
    assert "belongs to Barbie Capture v5" in p.classifier_status.value
    assert "Save as" in p.classifier_status.value
    p.classes.pick("good")
    press["Remove class"]()
    assert p.classes.items() == ["good", "bad timing"]
    assert "belongs to Barbie Capture v5" in p.classifier_status.value
    assert errors == []
    assert fc.open_classifier("theirs")["classes"] == ["good", "bad timing"]


def test_rename_and_delete_leave_another_apps_model_alone(
        vault, tmp_path, monkeypatch):
    """MEASURED before the fix: Typhon renamed Barbie's model, and deleted
    it, with one press each, and Barbie's next Open said "'frames' is new".
    WHOSE IT IS guarded Add class, Remove class, Mark and Train only, and the
    dropdown put Rename and Delete one click away for every saved model."""
    barbie, typhon = _barbie_then_typhon(tmp_path, monkeypatch)
    r = fc.rename_classifier("theirs", "mine", "theirs")
    assert "belongs to Barbie Capture v5" in r["summary"], r["summary"]
    assert "Nothing was renamed" in r["summary"] and r["name"] == "theirs"
    r = fc.delete_classifier("theirs", "theirs")
    assert "belongs to Barbie Capture v5" in r["summary"], r["summary"]
    assert (r["name"], r["classes"], r["moved_to"]) == \
        ("theirs", ["good", "bad timing"], "")
    assert _names_in(_store(vault)) == ["theirs"]
    # A copy of its own is Typhon's to rename and delete ...
    fc.save_as("theirs", "mine")
    assert "Renamed 'mine' to 'mine2'" in \
        fc.rename_classifier("mine", "mine2")["summary"]
    assert "moved aside" in fc.delete_classifier("mine2")["summary"]
    # ... and one tagged "shared" is every app's.
    fc.add_tag("theirs", fc.SHARED_TAG)
    assert "Renamed 'theirs' to 'ours'" in \
        fc.rename_classifier("theirs", "ours")["summary"]
    _run_as(monkeypatch, barbie)
    assert fc.open_classifier("ours")["classes"] == ["good", "bad timing"]


def test_this_app_is_the_models_that_belong_to_it(vault, tmp_path, monkeypatch,
                                                 capture):
    """MEASURED before the fix: Typhon's Save as of Barbie's model — Typhon's
    to train, and trained — was missing from "This app" and from "Export
    this app's classifiers" ("no classifier matches This app ... nothing was
    exported"): This app went by ORIGIN, while WHOSE IT IS goes by the latest
    Save as or Import. A spun-off Typhon's bundle would have left behind the
    very models only it can change. And by origin, Barbie's "This app" would
    have listed Typhon's copy, which Barbie may not change."""
    folder, _ = capture
    apps = tmp_path / "apps"
    barbie = _project(apps, "example_barbie_capture_v5", "Barbie Capture v5")
    _run_as(monkeypatch, barbie)
    _mark_some(folder)
    fc.train("frames")
    _run_as(monkeypatch, _project(apps, "birdlab", "Typhon"))
    fc.save_as("frames", "mycopy")
    fc.add_class("mycopy", "blurred")
    assert fc.list_classifiers(fc.FILTER_THIS_APP)["names"] == ["mycopy"]
    out = tmp_path / "out"
    out.mkdir()
    assert fc.export_this_app(str(out))["names"] == ["mycopy"]
    _run_as(monkeypatch, barbie)
    assert fc.list_classifiers(fc.FILTER_THIS_APP)["names"] == ["frames"]


def test_a_new_name_is_cleared_once_used_and_kept_when_refused(
        vault, trained, tmp_path):
    """MEASURED before the fix: New name still read 'frames-night' after
    Save as; with both models deleted, Import of frames' export brought it
    back as 'frames-night'. Save as, Rename and Import now clear the name
    they used, and keep one they asked to be changed."""
    folder, _ = trained
    out = tmp_path / "out"
    out.mkdir()
    export = fc.export_classifier("frames", str(out))["path"]
    h, p, errors, press = _typhon_app(folder, out)
    p.new_name.value = "frames-night"
    press["Save as"]()
    assert (p.classifier_name.value, p.new_name.value) == ("frames-night", "")
    p.new_name.value = "FRAMES"                     # taken: fix it and retry
    press["Rename"]()
    assert p.new_name.value == "FRAMES" and "already exists" in \
        p.classifier_status.value
    p.new_name.value = "dusk"
    press["Rename"]()
    assert (p.classifier_name.value, p.new_name.value) == ("dusk", "")
    press["Delete"]()
    p.classifier_name.value = "frames"
    press["Delete"]()
    p.import_from.value = export
    press["Import"]()
    assert (p.classifier_name.value, p.new_name.value) == ("frames", "")
    assert _names_in(_store(vault)) == ["frames"] and errors == []


def test_export_into_a_folder_that_is_not_there_says_so(vault, trained,
                                                        tmp_path):
    """MEASURED before the fix: Typhon's export picker chooses a FOLDER, and
    a typed folder that did not exist was read as a file name — "Exported
    frames v1 (...) as no_such_dir.typhon-classifier.zip", beside it."""
    for call in (lambda d: fc.export_classifier("frames", d),
                 lambda d: fc.export_this_app(d)):
        with pytest.raises(RuntimeError, match="no_such_dir is not a folder"):
            call(str(tmp_path / "no_such_dir"))
    assert not list(tmp_path.glob("*.zip"))
    named = fc.export_classifier("frames", str(tmp_path / "mine.zip"))
    assert Path(named["path"]).name == "mine.zip"   # a file name says so


def _runs_into(folder, src, stamp, frames):
    """Copy ``frames`` of the capture into ``folder`` as one run of
    frame_camera's naming: <stamp>_frame_000000.png, ..."""
    folder.mkdir(exist_ok=True)
    for i, p in enumerate(frames):
        shutil.copy(src / p, folder / f"{stamp}_frame_{i:06d}.png")


def test_the_classified_with_line_says_which_runs_are_not_classified_yet(
        vault, trained, tmp_path):
    """Typhon writes every run into the capture folder. MEASURED before the
    fix: run 120000 classified with frames v1, then run 130000 written into
    the same folder — the line still read "Classified with frames v1 ... —
    good 5, bad timing 3", as if the new run had been classified too; the
    record listed the first run only."""
    src, _ = trained
    names = sorted(p.name for p in src.glob("*.png"))
    folder = tmp_path / "runs"
    _runs_into(folder, src, "20261005_120000", names[:8])
    first = fc.classify_folder("frames", str(folder))["classified_with"]
    assert fc.classified_with(str(folder))["classified_with"] == first
    _runs_into(folder, src, "20261005_130000", names[8:14])
    line = fc.classified_with(str(folder))["classified_with"]
    assert line.startswith(first), line
    assert "not classified yet: run 20261005_130000 (6 frames)" in line, line
    assert fc.classify_folder("frames", str(folder))["classified_with"] == \
        fc.classified_with(str(folder))["classified_with"]
    assert "not classified" not in \
        fc.classified_with(str(folder))["classified_with"]


def test_a_renamed_model_is_named_as_it_is_called_now(vault, trained):
    """MEASURED before the fix: a folder classified with birds v2, birds
    then renamed hawks — the line and classified_with still said "birds v2",
    a name the dropdown no longer listed; once a new 'birds' was made,
    picking it opened a different, untrained model. The record keeps what
    it was called then; the line names it as it is called now."""
    folder, _ = trained
    sha8 = fc.classify_folder("frames", str(folder))["sha256"][:8]
    fc.rename_classifier("frames", "hawks")
    fc.add_class("frames", "z")                 # a new model takes the name
    now = f"hawks v1 ({sha8}; then called frames)"
    line = fc.classified_with(str(folder))["classified_with"]
    assert line.startswith(f"Classified with {now} on "), line
    assert now in fc.run_history("hawks")["rows"][0]
    fc.delete_classifier("hawks")
    line = fc.classified_with(str(folder))["classified_with"]
    assert line.startswith(f"Classified with {now} (since deleted) on "), line


def test_the_list_and_the_line_read_again_only_what_changed(
        vault, trained, monkeypatch):
    """The dropdown re-reads the store each time it is about to be seen, and
    after every classifier press. MEASURED before the fix: each re-read
    parsed every classifier's classes.json, about.json and newest meta.json
    — 1.0-1.2 ms a classifier on the UI thread, 52-62 ms at 50 saved models,
    520-630 ms at 500 — and the "classified with" line parsed every run
    record (0.3 ms a classifier). Now a file is parsed again only when it
    changed (MEASURED after: 0.3 and 0.1 ms a classifier)."""
    folder, _ = trained
    fc.classify_folder("frames", str(folder))
    fc.save_as("frames", "other")
    fc.list_classifiers()
    fc.classified_with(str(folder))
    opened = []
    real = Path.read_text

    def read_text(self, *args, **kwargs):
        opened.append(f"{self.parent.name}/{self.name}")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    lib = fc.list_classifiers()
    line = fc.classified_with(str(folder))["classified_with"]
    assert opened == [], opened
    assert lib["names"] == ["frames", "other"]
    assert line.startswith("Classified with frames v1 (")
    # Another app tags one and classifies with the other: what changed is
    # read again, and the list and the line say so.
    fc.add_tag("other", "night")
    fc.classify_folder("other", str(folder))
    opened.clear()
    lib = fc.list_classifiers()
    line = fc.classified_with(str(folder))["classified_with"]
    assert any(r.startswith("other ") and "tags: night" in r
               for r in lib["rows"]), lib["rows"]
    assert line.startswith("Classified with other v1 ("), line
    assert opened and all(o.startswith("other/") for o in opened), opened
    # A damaged newest version is seen at once, whatever was read before.
    meta = _store(vault) / "frames" / "versions" / "v1" / "meta.json"
    meta.write_text("{cut short", encoding="utf-8")
    row = next(r for r in fc.list_classifiers()["rows"]
               if r.startswith("frames "))
    assert "PROBLEM" in row, row
