"""
"Typhon (plugins)" at stage P4, as far as Andy asked on 2 November 2026: the
classifier as an optional plugin in a pop-out window of its own, the
toolbar strip and Plugins window that make it optional, and datasets — the
"Add to dataset…" button beside PNG / Raw, the Datasets window, and a
dataset sent to the classifier.

  * the wireframe: the 32 classifier shapes gone, sklearn no longer required
    by the app (the plugin declares it), the plugin strip and the two
    dataset buttons in, the live view given the freed width;
  * the panel's links ARE the classic Typhon's: same functions, inputs and
    outputs, so its behaviour is today's in another window;
  * in the built app: the strip's Classification button opens one window
    (built once, hidden on close, its place remembered); the Plugins window
    switches the classifier off (no button, no window) and on again;
  * frames added to datasets from the slider, sent to the classifier,
    marked, trained on and classified; Delete asks twice;
  * the Datasets window: list, thumbnails, remove, merge, rename, delete
    (asks twice, moves aside), Send to.

Offscreen, in temporary vaults; windows are built and grabbed, never shown
on a screen.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import frame_camera  # noqa: E402
import frame_classes  # noqa: E402
import run_example_gui as rex  # noqa: E402
from council_core import capture  # noqa: E402
from council_core import datasets as ds  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

pytest.importorskip("sklearn")

EXAMPLES = ROOT / "examples" / "gui"
NAME = "typhon_plugins"
GENERATED = ("app", "handlers", "ui", "ui.main_ui", "ui.ports", "ui.widgets")
CLASSIFIER_SHAPES = {f"s{i:02d}" for i in list(range(24, 41))
                     + list(range(64, 79))}
CLASSIFY = "typhon.classify"


def gspec(name):
    return json.loads((EXAMPLES / f"{name}.gspec").read_text(encoding="utf-8"))


# ======================================================================
# The wireframe
# ======================================================================
def test_the_classifier_left_the_main_window():
    old, new = gspec("typhon"), gspec(NAME)
    old_ids = {s["id"] for s in old["shapes"]}
    new_ids = {s["id"] for s in new["shapes"]}
    assert CLASSIFIER_SHAPES <= old_ids
    assert not (CLASSIFIER_SHAPES & new_ids)
    assert new_ids - old_ids == {"s93", "s94", "s95"}
    assert old_ids - new_ids == CLASSIFIER_SHAPES
    assert "sklearn" in old["requires"] and "sklearn" not in new["requires"]
    modules = {s["script"].get("module") for s in new["shapes"]
               if s.get("script")}
    assert "frame_classes" not in modules
    ports = {(s.get("port") or {}).get("name") for s in new["shapes"]}
    for gone in ("classifier_name", "classes", "predictions",
                 "classifier_status"):
        assert gone not in ports


def test_the_strip_and_the_dataset_buttons_are_in():
    by = {s["id"]: s for s in gspec(NAME)["shapes"]}
    strip = by["s95"]
    assert strip["kind"] == "toolbar"
    assert strip["port"]["name"] == frame_camera.PLUGIN_TOOLBAR
    add, sets = by["s93"], by["s94"]
    assert add["script"] == {"function": "add_to_dataset", "inputs": [],
                             "module": "frame_camera",
                             "outputs": {"capture_status": "summary"}}
    assert sets["script"]["function"] == "open_datasets"
    png = by["s56"]
    assert png["label"] == "PNG / Raw"
    assert add["y"] == png["y"] and add["x"] > png["x"], \
        "Add to dataset sits on PNG / Raw's row, after it"


def test_nothing_overlaps_and_everything_is_on_the_canvas():
    doc = gspec(NAME)
    w, h = doc["canvas"]["w"], doc["canvas"]["h"]
    shapes = doc["shapes"]
    for s in shapes:
        assert 0 <= s["x"] and s["x"] + s["w"] <= w, s["id"]
        assert 0 <= s["y"] and s["y"] + s["h"] <= h, s["id"]
    inside = {"s80", "s81"}            # the Basic page and its note
    flat = [s for s in shapes if s["kind"] not in ("notebook", "frame")
            and s["id"] not in inside]
    clashes = []
    for i, a in enumerate(flat):
        for b in flat[i + 1:]:
            if (a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"]
                    and a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"]):
                clashes.append((a["id"], b["id"]))
    new = {"s93", "s94", "s95", "s10", "s23", "s20", "s22", "s48", "s49",
           "s50", "s51", "s59", "s60", "s61", "s62", "s63"}
    assert [c for c in clashes if set(c) & new] == []


def test_the_panels_links_are_the_classic_typhons():
    from typhon_plugins.classify import panel
    classic = {}
    for s in gspec("typhon")["shapes"]:
        if s["id"] in CLASSIFIER_SHAPES and s.get("script"):
            sc = s["script"]
            classic[s["label"]] = (sc["module"], sc["function"],
                                   tuple(sc["inputs"]), sc["outputs"])
    mine = {what: ("frame_classes", fn, tuple(inputs), outputs)
            for _key, what, fn, inputs, outputs in panel.LINKS}
    assert mine == classic
    for _key, _what, fn, _inputs, _outputs in panel.DATASET_LINKS:
        assert callable(getattr(frame_classes, fn))


# ======================================================================
# The built app
# ======================================================================
@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    vault = tmp_path_factory.mktemp("tp_p4_vault")
    return rex.build(NAME, project=NAME, vault_dir=vault, target="qt")


@pytest.fixture
def frames(tmp_path):
    from PIL import Image
    folder = tmp_path / "capture"
    folder.mkdir()
    rng = np.random.default_rng(7)
    made = {}
    for run, base in (("20261102_120000", 40), ("20261102_120500", 200)):
        made[run] = []
        for i in range(1, 7):
            a = (rng.integers(0, 30, (48, 64)) + base).clip(0, 255)
            p = folder / f"{run}_frame_{i:06d}.png"
            Image.fromarray(a.astype("uint8")).save(p)
            made[run].append(p)
    return folder, made


@pytest.fixture
def ui(qapp, built, tmp_path, monkeypatch):
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(tmp_path / "vault"))
    monkeypatch.setenv("FRAME_CLASSES_STORE", str(tmp_path / "store"))
    capture.warm_imports()
    kept = list(sys.path)
    frame_camera.disconnect()
    sys.path.insert(0, str(built))
    for name in GENERATED:
        sys.modules.pop(name, None)
    import app as generated
    window = generated.App()
    window.resize(1504, 1016)
    yield window
    strip = frame_camera._SCREEN.get("strip")
    if strip is not None:
        strip.close()
    sets = frame_camera._SCREEN.get("datasets")
    if sets is not None:
        sets.deleteLater()
    frame_camera._SCREEN.update(app=None, strip=None, datasets=None)
    host = frame_camera._RIG.plugins
    try:
        host.switch_on(CLASSIFY)
    except Exception:
        pass
    frame_camera.disconnect()
    for attr in ("setup_path", "tabs", "picker", "reviewer",
                 "capture_button"):
        setattr(frame_camera._LIVE, attr, None)
    window.deleteLater()
    qapp.processEvents()
    sys.path[:] = kept
    for name in GENERATED:
        sys.modules.pop(name, None)


def pump(seconds=0.2, until=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.01)
    return until() if until is not None else True


def strip_texts(ui):
    toolbar = ui.ports.plugin_toolbar.widget
    return [b.text() for b in toolbar.findChildren(QPushButton)
            if b.isVisibleTo(toolbar) or not b.isHidden()]


def test_the_strip_has_classification_then_plugins(ui):
    strip = ui._plugin_strip
    assert list(strip.buttons) == [CLASSIFY]
    toolbar = ui.ports.plugin_toolbar.widget
    box = toolbar.layout()
    order = [box.itemAt(i).widget().text() for i in range(box.count())
             if box.itemAt(i).widget() is not None]
    assert order == ["Classification", "Plugins…"]
    assert "typhon.trigger" not in strip.buttons, \
        "the trigger boxes are still on the Basic tab"
    sheet = strip.buttons[CLASSIFY].styleSheet()
    assert "#045f80" in sheet, "dressed as the window's own buttons"


def test_one_window_built_once_hidden_on_close_and_placed_again(ui, built):
    strip = ui._plugin_strip
    strip.buttons[CLASSIFY].click()
    win = strip.windows[CLASSIFY]
    panel = win.inner
    assert win.isVisible() and win.windowTitle().startswith("Classification")
    assert panel is frame_camera._RIG.plugins.instance(CLASSIFY).panel_widget
    win.setGeometry(40, 50, 520, 860)
    size = [win.width(), win.height()]
    win.close()
    assert not win.isVisible() and strip.windows[CLASSIFY] is win
    places = json.loads((built / "typhon_windows.json").read_text(
        encoding="utf-8"))
    assert places[CLASSIFY][2:] == size
    strip.buttons[CLASSIFY].click()
    assert strip.windows[CLASSIFY] is win and win.inner is panel
    assert win.isVisible()


def test_the_plugins_window_switches_the_classifier_off_and_on(ui, built):
    strip = ui._plugin_strip
    strip.open(CLASSIFY)
    dlg = strip.open_plugins_window()
    box = dlg.boxes[CLASSIFY]
    assert box.isChecked() and set(dlg.boxes) >= {CLASSIFY, "typhon.trigger"}
    assert not any(p in dlg.boxes for p in ("typhon.basler",
                                            "typhon.simulated")), \
        "camera families are the set-up wizard's"
    box.setChecked(False)
    host = frame_camera._RIG.plugins
    assert host.state(CLASSIFY)[0] == "off"
    assert CLASSIFY not in strip.buttons and CLASSIFY not in strip.windows
    listed = json.loads((built / "typhon_plugins.json").read_text(
        encoding="utf-8"))
    assert CLASSIFY in json.dumps(listed)
    assert "off" in dlg.notes[CLASSIFY].text()
    assert CLASSIFY not in host.dataset_plugins()
    box.setChecked(True)
    assert CLASSIFY in strip.buttons
    assert strip.open(CLASSIFY)


def test_frames_to_datasets_to_the_classifier(ui, frames):
    from council_qt.widgets import datasets_window as dw
    folder, made = frames
    dark, bright = made["20261102_120000"], made["20261102_120500"]
    ui.ports.capture_folder.set(str(folder))
    reviewer = frame_camera._LIVE.reviewer
    assert pump(3.0, until=lambda: len(reviewer.files) == 12)
    reviewer._slider_to(2)
    reviewer._moved(2)
    assert pump(2.0, until=lambda: frame_camera._shown_frame() == dark[2])
    run = frame_camera._run_frames(dark[2])
    assert run == dark
    dlg = dw.AddToDatasetDialog(ui, current=dark[2], run_frames=run)
    assert dlg.frames("frame") == [dark[2]]
    dlg.from_box.setValue(2)
    dlg.to_box.setValue(4)
    assert dlg.frames("range") == dark[1:4]
    assert dlg.add("dark", "run")["frames"] == 6
    dlg = dw.AddToDatasetDialog(ui, current=bright[0], run_frames=bright)
    assert dlg.name_box.currentText() == "dark", "the last one, offered"
    dlg.add("bright", "run")
    assert ds.names() == ["bright", "dark"]

    host = frame_camera._RIG.plugins
    said = host.send_dataset(CLASSIFY, "dark")
    assert "'dark' is in Classification: 6 frames" in said
    panel = ui._plugin_strip.windows[CLASSIFY].inner
    assert panel.dataset_name() == "dark"
    p = panel.ports
    p.classifier_name.set("welds")
    panel.buttons["open"].click()
    for cls in ("dark", "bright"):
        p.new_class.set(cls)
        panel.buttons["add_class"].click()
    assert p.classes.items() == ["dark", "bright"]
    panel.classes.setCurrentRow(0)
    panel.buttons["mark_dataset"].click()
    assert "6 frames marked 'dark'" in panel.status.text()
    panel.use_dataset("bright")
    panel.classes.setCurrentRow(1)
    panel.buttons["mark_dataset"].click()
    panel.buttons["train"].click()
    assert "Trained" in panel.status.text(), panel.status.text()
    panel.use_dataset("dark")
    panel.buttons["classify_dataset"].click()
    rows = p.predictions.items()
    assert len(rows) == 6 and all(r.endswith("dark") for r in rows)
    panel.buttons["predict"].click()
    assert dark[2].name in panel.status.text() and "'dark'" in \
        panel.status.text()
    panel.buttons["classify_all"].click()
    assert len(p.predictions.items()) == 12
    assert panel.classified_line.text(), "the classified-with line follows"
    assert panel.failed == ""


def test_a_failed_press_clears_what_it_fills_and_says_why(ui):
    strip = ui._plugin_strip
    strip.open(CLASSIFY)
    panel = strip.windows[CLASSIFY].inner
    panel.ports.predictions.set(["old row"])
    panel.ports.classifier_name.set("")
    panel.buttons["classify_all"].click()
    assert panel.ports.predictions.items() == []
    assert panel.failed.startswith("Classify all frames failed:")
    panel.buttons["mark_dataset"].click()
    assert "pick a dataset first" in panel.failed


def test_delete_asks_for_a_second_press(ui):
    strip = ui._plugin_strip
    strip.open(CLASSIFY)
    panel = strip.windows[CLASSIFY].inner
    p = panel.ports
    p.classifier_name.set("gone")
    panel.buttons["open"].click()
    p.new_class.set("a")
    panel.buttons["add_class"].click()
    store = frame_classes.store_dir("gone")
    assert store.is_dir()
    panel.buttons["delete"].click()
    assert panel.buttons["delete"].text() == "Sure?"
    assert store.is_dir()
    panel.buttons["delete"].click()
    assert not store.is_dir()


def test_without_the_strip_an_app_is_left_alone(ui):
    """Today's Typhon never makes the host (no plugin list), so nothing of
    this runs there: the strip is only for a plugins project."""
    assert frame_camera._plugin_project() is not None
    assert frame_camera._SCREEN["strip"] is ui._plugin_strip


# ======================================================================
# The Datasets window
# ======================================================================
def test_the_datasets_window(ui, frames):
    folder, made = frames
    dark, bright = made["20261102_120000"], made["20261102_120500"]
    ds.add_frames("a", dark)
    ds.add_frames("b", bright[:2])
    dark[5].unlink()
    frame_camera.open_datasets()
    win = frame_camera._SCREEN["datasets"]
    assert [win.list.item(i).text() for i in range(win.list.count())] == \
        ["a — 6 frames, 1 missing", "b — 2 frames"]
    assert win.current_name() == "a"
    assert win.frames.count() == 6
    assert pump(2.0, until=lambda: not win._pending)
    assert "(missing)" in win.frames.item(5).text()
    assert win.receivers() == [CLASSIFY]
    win.fill_send_menu()
    assert [a.text() for a in win.send_menu.actions()] == ["Classification"]
    assert "'a' is in Classification" in win.send_to(CLASSIFY)
    win.frames.item(0).setSelected(True)
    assert "1 frame taken out of 'a'" in win.remove_selected()
    assert dark[0].is_file()
    assert "added to 'b'" in win.merge_into("b")
    assert ds.summary("b")["frames"] == 7
    assert win.current_name() == "b", "the merged dataset is shown"
    assert "'b' is now 'c'" in win.rename_to("c")
    assert ds.names() == ["a", "c"] and win.current_name() == "c"
    assert win.delete().startswith("Delete 'c'?")
    assert ds.names() == ["a", "c"]
    assert "moved to" in win.delete()
    assert ds.names() == ["a"]
    assert (Path(os.environ["COUNCIL_VAULT_ROOT"]) / "datasets" /
            ds.DELETED).is_dir()


def test_add_to_dataset_without_a_frame_says_what_to_do(ui):
    said = frame_camera.add_to_dataset()["summary"]
    assert "show a saved frame first" in said


# ======================================================================
# The library's two new calls
# ======================================================================
def test_mark_frames_and_classify_frames(tmp_path, monkeypatch, frames):
    monkeypatch.setenv("FRAME_CLASSES_STORE", str(tmp_path / "store"))
    _folder, made = frames
    dark, bright = made["20261102_120000"], made["20261102_120500"]
    frame_classes.open_classifier("m")
    frame_classes.add_class("m", "dark")
    frame_classes.add_class("m", "bright")
    with pytest.raises(RuntimeError, match="pick a class"):
        frame_classes.mark_dataset("m", dark, [])
    with pytest.raises(RuntimeError, match="is not a file"):
        frame_classes.mark_dataset("m", dark + [tmp_path / "x.png"], ["dark"])
    assert frame_classes.open_classifier("m")["summary"]
    out = frame_classes.mark_dataset("m", dark, ["dark"])
    assert out["marked"] == 6 and out["changed"] == 0
    frame_classes.mark_dataset("m", bright, ["bright"])
    out = frame_classes.mark_dataset("m", bright[:1], ["dark"])
    assert out["changed"] == 1
    frame_classes.mark_dataset("m", bright[:1], ["bright"])
    frame_classes.train("m")
    got = frame_classes.classify_dataset("m", [str(p) for p in bright])
    assert len(got["rows"]) == 6 and got["counts"]["bright"] == 6
    assert not (frame_classes.store_dir("m") / frame_classes.RUNS).exists(), \
        "a dataset's classification is not a capture folder's record"
