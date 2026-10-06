"""
Barbie Capture v5 (v4 + camera setup) and Typhon (v5 in #045f80).

Also covers what made the setup wizard possible at all: generated camera apps
now get `frame_camera.attach(self)` written into app.py, because the live view
and the first-run wizard both start there — and an app that needs a hand edit
before it can show its own setup wizard has not really got one.

Everything offscreen. Apps are constructed and rendered with grab(), never
shown; the first-run wizard is replaced before it can open.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import frame_camera
import gui_emit_qt
import gui_projects as gpj
import python_envs as pe
import run_example_gui as rex
from council_core import camera_setup as cs
from PySide6.QtWidgets import QApplication

EXAMPLES = ROOT / "examples" / "gui"
TYPHON_BG = "#045f80"
GENERATED = ("app", "handlers", "ui", "ui.main_ui", "ui.ports", "ui.widgets")


def gspec(name):
    return json.loads((EXAMPLES / f"{name}.gspec").read_text(encoding="utf-8"))


def spec_of(name):
    import gui_layout as gl
    import gui_shapes as gs
    import gui_spec as gsp

    proj = gs.load_gspec(EXAMPLES / f"{name}.gspec")
    tree = gl.infer(proj.shapes, proj.canvas.w, proj.canvas.h)
    return gsp.build(proj.shapes, tree, {}, project=name, mode=proj.mode,
                     title=proj.window.title, requires=proj.requires,
                     root_bg=proj.window.bg, root_fg=proj.window.fg,
                     root_font=proj.window.font)


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def construct(pdir):
    """The generated App, imported from `pdir` and never shown."""
    sys.path.insert(0, str(pdir))
    for name in GENERATED:
        sys.modules.pop(name, None)
    import app as generated
    return generated.App()


@pytest.fixture
def forget_generated():
    kept = list(sys.path)
    yield
    frame_camera.disconnect()
    frame_camera._LIVE.setup_path = None
    sys.path[:] = kept
    for name in GENERATED:
        sys.modules.pop(name, None)


# ======================================================================
# The wireframes
# ======================================================================
def test_v5_is_v4_plus_exactly_one_button():
    """Versions are kept side by side to show progress — v5 must not quietly
    rearrange v4."""
    v4, v5 = gspec("barbie_capture_v4"), gspec("barbie_capture_v5")
    old = {s["id"]: s for s in v4["shapes"]}
    new = {s["id"]: s for s in v5["shapes"]}
    added = sorted(set(new) - set(old))
    assert added == ["s54"]
    assert new["s54"]["label"] == "Camera setup…"
    changed = sorted(k for k in old if old[k] != new[k])
    assert changed == ["s41"], "only the heading beside the button may move"


def test_the_setup_button_is_linked_to_the_wizard():
    button = next(s for s in gspec("barbie_capture_v5")["shapes"]
                  if s["id"] == "s54")
    assert button["script"]["module"] == "frame_camera"
    assert button["script"]["function"] == "setup"
    assert button["script"]["outputs"]["cameras"] == "rows"


def test_typhon_is_v5_in_teal_plus_the_capture_review_controls():
    """Typhon began as v5 in #045f80. Since the first EVK4 tests it alone has
    the slider that follows a capture (no generated browser on it), a view
    line above the picture, Play / Pause, PNG / Raw and Pop out, an FPS box
    where v3's unwired "Frame count" was (wired, so it applies as it
    changes), real exposure and gain ranges, and Settings in the top right
    corner. And Connect / Apply area / Full sensor no longer write the
    camera's area (sensor pixels) into the crop box (picture pixels): Connect
    leaves it alone and the area buttons clear it. Then the camera's own
    set-up: a line saying the camera's area, the preset picker, Save preset
    and Camera settings (s59-s63), for which the status line moved down a
    row (s51). Then the classifier library (s64-s78): the name box became
    the model dropdown (s26, s27 fills it), the class controls moved down
    under the library (s28-s40), and the camera notes (s52, s53) moved to
    the free space in the left column to make the room. Nothing else
    moved."""
    v5, typhon = gspec("barbie_capture_v5"), gspec("typhon")
    assert typhon["window"]["bg"] == TYPHON_BG
    assert typhon["window"]["fg"] == v5["window"]["fg"]
    assert typhon["window"]["title"] == "Typhon"
    old = {s["id"]: s for s in v5["shapes"]}
    new = {s["id"]: s for s in typhon["shapes"]}
    assert sorted(set(new) - set(old)) == [f"s{i}" for i in range(55, 79)]
    strip = lambda s: {k: v for k, v in s.items() if k != "label"}
    changed = sorted(k for k in old if strip(old[k]) != strip(new[k]))
    assert changed == ["s04", "s06", "s08", "s09", "s11", "s23", "s26",
                       "s27", "s28", "s29", "s30", "s31", "s32", "s33",
                       "s34", "s35", "s36", "s37", "s38", "s39", "s40",
                       "s44", "s46", "s49", "s50", "s51", "s52", "s53"]
    assert new["s25"]["label"].startswith("Model"), "only relabelled"
    assert "roi" not in new["s44"]["script"]["outputs"]
    assert new["s49"]["script"]["outputs"]["roi"] == "crop"
    assert new["s50"]["script"]["outputs"]["roi"] == "crop"
    assert new["s11"]["drives"] == {}, "a generated browser would own the slider"
    assert new["s09"]["port"] == {"name": "view_status"}
    assert new["s55"]["script"]["function"] == "play_pause"
    assert new["s56"]["script"]["function"] == "toggle_view"
    assert new["s57"]["script"]["function"] == "pop_out"
    assert new["s08"]["port"] == {"name": "frame_rate"}
    assert new["s46"]["script"]["inputs"][-1] == "frame_rate"
    assert new["s58"]["script"]["module"] == "gui_settings"


# ======================================================================
# The FPS box and the Settings button
# ======================================================================
def test_the_fps_box_says_fps_not_frame_count():
    """The user's Typhon still read "Frame count": the box must say FPS."""
    shapes = {s["id"]: s for s in gspec("typhon")["shapes"]}
    assert shapes["s07"]["label"].startswith("FPS")
    assert "count" not in shapes["s07"]["label"].lower()


def test_the_fps_box_applies_its_rate_as_it_changes():
    """It used to take effect only at Start. Wired to apply_frame_rate, it
    reaches the camera on every committed change; Start still passes it."""
    shapes = {s["id"]: s for s in gspec("typhon")["shapes"]}
    link = shapes["s08"]["script"]
    assert (link["module"], link["function"]) == ("frame_camera",
                                                  "apply_frame_rate")
    assert link["inputs"] == ["frame_rate"]
    assert link["outputs"] == {"capture_status": "summary"}
    assert "frame_rate" in shapes["s46"]["script"]["inputs"]


def test_settings_sits_in_the_top_right_corner_and_overlaps_nothing():
    """In the free band right of the title, its right edge on the right
    column's (1480), clear of every other shape."""
    shapes = {s["id"]: s for s in gspec("typhon")["shapes"]}
    button = shapes["s58"]
    assert button["label"].startswith("Settings")
    assert (button["x"], button["y"], button["w"], button["h"]) == \
        (1376, 16, 104, 32)
    right_column = shapes["s24"]
    assert button["x"] + button["w"] == right_column["x"] + right_column["w"]
    assert button["y"] + button["h"] <= right_column["y"]
    title = shapes["s00"]
    assert button["x"] >= title["x"] + title["w"]

    def overlaps(a, b):
        return not (a["x"] >= b["x"] + b["w"] or b["x"] >= a["x"] + a["w"]
                    or a["y"] >= b["y"] + b["h"] or b["y"] >= a["y"] + a["h"])
    assert [k for k, s in shapes.items()
            if k != "s58" and overlaps(button, s)] == []
    assert button["script"] == {"function": "settings_menu", "inputs": [],
                                "module": "gui_settings", "outputs": {}}


def test_settings_is_drawn_in_the_top_right_of_the_generated_window(
        qapp, typhon_dir, forget_generated):
    """The layout inference keeps it there: top right, the rest unmoved."""
    from PySide6.QtWidgets import QPushButton
    ui = construct(typhon_dir)
    ui.resize(1504, 1016)
    ui.grab()
    button = next(b for b in ui.findChildren(QPushButton)
                  if b.text().startswith("Settings"))
    g = button.geometry()
    assert g.y() < 40 and ui.width() - (g.x() + g.width()) < 40
    start = next(b for b in ui.findChildren(QPushButton)
                 if b.text() == "Start capture")
    assert start.geometry().y() > 900
    assert hasattr(ui, "on_btn_settings")


def overlapping(shapes):
    """Pairs of shapes that overlap, by id."""
    def overlaps(a, b):
        return not (a["x"] >= b["x"] + b["w"] or b["x"] >= a["x"] + a["w"]
                    or a["y"] >= b["y"] + b["h"] or b["y"] >= a["y"] + a["h"])
    items = list(shapes.values())
    return [(a["id"], b["id"]) for i, a in enumerate(items)
            for b in items[i + 1:] if overlaps(a, b)]


def test_the_camera_set_up_controls_sit_under_the_area_buttons():
    """The camera's area line, the preset picker, Save preset and Camera
    settings: in the middle column under Apply area / Full sensor, on the
    Start / Stop row, inside the canvas, overlapping nothing."""
    doc = gspec("typhon")
    shapes = {s["id"]: s for s in doc["shapes"]}
    assert (doc["canvas"]["w"], doc["canvas"]["h"]) == (1504, 1016)
    assert overlapping(shapes) == []
    middle = shapes["s10"]                       # the picture's column
    for sid in ("s59", "s51", "s60", "s61", "s62", "s63"):
        s = shapes[sid]
        assert middle["x"] <= s["x"] and \
            s["x"] + s["w"] <= middle["x"] + middle["w"], sid
        assert s["y"] > shapes["s49"]["y"], sid
        assert s["y"] + s["h"] <= doc["canvas"]["h"] - 16, sid
    assert shapes["s61"]["y"] == shapes["s46"]["y"], "on the Start row"
    row = [shapes[k] for k in ("s61", "s62", "s63")]
    for left, right in zip(row, row[1:]):
        assert left["x"] + left["w"] < right["x"]


def test_the_preset_picker_and_settings_are_linked():
    shapes = {s["id"]: s for s in gspec("typhon")["shapes"]}
    picker = shapes["s61"]
    assert picker["kind"] == "combobox" and picker["port"] == {
        "name": "preset"}
    assert picker["props"]["readonly"] is False, "type a name to save one"
    assert picker["script"] == {"function": "pick_preset",
                                "inputs": ["preset"],
                                "module": "frame_camera",
                                "outputs": {"capture_status": "summary"}}
    assert shapes["s62"]["script"]["function"] == "save_preset"
    assert shapes["s62"]["script"]["inputs"] == ["preset"]
    assert shapes["s63"]["script"]["function"] == "camera_settings"
    assert shapes["s59"]["port"] == {"name": "camera_area"}
    # The two boxes are named apart in the window too.
    assert shapes["s17"]["label"].startswith("Crop box")
    assert "ROI" in shapes["s48"]["label"] and "area" in shapes["s48"][
        "label"]
    # Every port a link reads or writes exists.
    ports = {s["port"].get("name") for s in shapes.values() if s["port"]}
    for s in shapes.values():
        link = s.get("script") or {}
        if link.get("module") != "frame_camera":
            continue
        assert set(link.get("inputs", [])) <= ports, s["id"]
        assert set(link.get("outputs", {})) <= ports, s["id"]


#: The classifier library's controls in typhon.gspec: shape -> (kind,
#: label, port, frame_classes function or None).
LIBRARY = {
    "s26": ("combobox", "Model", "classifier_name", "open_classifier"),
    "s27": ("button", "Open", None, "open_classifier"),
    "s64": ("label", "Show", None, None),
    "s65": ("combobox", "Filter", "classifier_filter", "list_classifiers"),
    "s66": ("entry", "New name", "new_name", None),
    "s67": ("button", "Save as", None, "save_as"),
    "s68": ("button", "Rename", None, "rename_classifier"),
    "s69": ("button", "Delete", None, "delete_classifier"),
    "s70": ("file_picker", "Export folder", "export_to", None),
    "s71": ("button", "Export", None, "export_classifier"),
    "s72": ("button", "Export this app's classifiers", None,
            "export_this_app"),
    "s73": ("file_picker", "Import file", "import_from", None),
    "s74": ("button", "Import", None, "import_classifier"),
    "s75": ("entry", "Tag", "tag", None),
    "s76": ("button", "Add tag", None, "add_tag"),
    "s77": ("button", "Remove tag", None, "remove_tag"),
    "s78": ("label", "", "classified_with_line", None),
}


def test_the_classifier_library_sits_in_the_right_column_overlapping_nothing():
    """The user's dropdown of saved models, its filter, Save as / Rename /
    Delete, Export / Export this app's classifiers / Import, tags and the
    "classified with" line — in the right column with the class controls
    under them, inside the 1504 x 1016 canvas, overlapping nothing."""
    doc = gspec("typhon")
    shapes = {s["id"]: s for s in doc["shapes"]}
    assert (doc["canvas"]["w"], doc["canvas"]["h"]) == (1504, 1016)
    assert overlapping(shapes) == []
    column = shapes["s24"]                       # the "Classifier" heading
    for sid in [*LIBRARY, *(f"s{i}" for i in range(28, 41))]:
        s = shapes[sid]
        assert column["x"] <= s["x"] and \
            s["x"] + s["w"] <= column["x"] + column["w"], sid
        assert s["y"] > column["y"], sid
        assert s["y"] + s["h"] <= doc["canvas"]["h"] - 16, sid
    # The library first, the class controls under it, the predictions last.
    assert max(shapes[k]["y"] for k in LIBRARY) < shapes["s28"]["y"]
    assert shapes["s40"]["y"] == max(shapes[f"s{i}"]["y"]
                                     for i in range(28, 41))
    # The camera notes, moved for the room: left column, above the cameras.
    for sid in ("s52", "s53"):
        assert shapes[sid]["x"] + shapes[sid]["w"] <= shapes["s10"]["x"]
        assert shapes[sid]["y"] + shapes[sid]["h"] < shapes["s41"]["y"]
    assert shapes["s53"]["port"] == {"name": "camera_notes"}


def test_the_classifier_library_is_linked_to_frame_classes():
    shapes = {s["id"]: s for s in gspec("typhon")["shapes"]}
    ports = {s["port"].get("name") for s in shapes.values() if s["port"]}
    for sid, (kind, label, port, function) in LIBRARY.items():
        s = shapes[sid]
        assert (s["kind"], s["label"]) == (kind, label), sid
        if port:
            assert s["port"]["name"] == port, sid
        link = s.get("script") or {}
        if function is None:
            assert link == {}, sid
            continue
        assert (link["module"], link["function"]) == ("frame_classes",
                                                      function), sid
        assert set(link["inputs"]) <= ports, sid
        assert set(link["outputs"]) <= ports, sid
    model = shapes["s26"]
    assert model["props"]["readonly"] is False, "type a new model's name"
    assert model["port"]["default"] == "frames"
    assert model["script"]["outputs"]["classifier_name"] == "name"
    assert shapes["s65"]["port"]["default"] == "All classifiers"
    assert shapes["s70"]["props"]["mode"] == "folder"
    assert shapes["s73"]["props"]["mode"] == "file"
    # The existing controls still act on the box's model; Classify fills
    # the "classified with" line too.
    for sid in ("s30", "s33", "s34", "s35", "s36", "s38"):
        assert "classifier_name" in shapes[sid]["script"]["inputs"], sid
    assert shapes["s38"]["script"]["outputs"]["classified_with_line"] == \
        "classified_with"
    # Every frame_classes link in Typhon names a function that exists and a
    # result key it documents (what the Wiring editor offers).
    from council_core import designer_wiring as dw
    info = {f.name: f for f in dw.module_info("frame_classes").functions}
    for s in shapes.values():
        link = s.get("script") or {}
        if link.get("module") != "frame_classes":
            continue
        keys = set(info[link["function"]].result_keys)
        assert set(link["outputs"].values()) <= keys, (s["id"], keys)


def test_the_review_controls_fit_beside_the_slider():
    shapes = {s["id"]: s for s in gspec("typhon")["shapes"]}
    row = [shapes[k] for k in ("s11", "s55", "s56", "s57")]
    for left, right in zip(row, row[1:]):
        assert left["x"] + left["w"] <= right["x"], (left["id"], right["id"])
    assert row[-1]["x"] + row[-1]["w"] <= shapes["s10"]["x"] + shapes["s10"]["w"]


def test_typhon_says_typhon_inside_the_window_too():
    """An offscreen render of the first build still read "Barbie Capture"
    across the top: the title bar had been renamed, the heading had not."""
    labels = [str(s.get("label", "")) for s in gspec("typhon")["shapes"]]
    assert "Typhon" in labels
    assert not any("barbie" in l.lower() for l in labels)


def test_no_pink_is_left_in_typhon():
    text = (EXAMPLES / "typhon.gspec").read_text(encoding="utf-8").lower()
    assert "#ff4fa3" not in text


def test_white_on_typhon_teal_is_readable():
    """WCAG AA needs 4.5:1 for body text. Measured 7.11:1 — AAA."""
    def lum(h):
        rgb = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
               for c in rgb]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    ratio = (lum("#ffffff") + 0.05) / (lum(TYPHON_BG) + 0.05)
    assert ratio >= 4.5, ratio


# ======================================================================
# app.py is written with the camera already attached
# ======================================================================
@pytest.mark.parametrize("name", ["barbie_capture_v4", "barbie_capture_v5",
                                  "typhon"])
def test_a_camera_wireframe_gets_attach_written_into_app_py(name):
    assert gui_emit_qt.links_frame_camera(spec_of(name))
    assert "frame_camera.attach(self)" in gui_emit_qt.emit_app_py(spec_of(name))


@pytest.mark.parametrize("name", ["barbie_capture_v3", "image_viewer"])
def test_a_wireframe_without_a_camera_is_left_alone(name):
    """Every other generated app must come out exactly as before."""
    assert not gui_emit_qt.links_frame_camera(spec_of(name))
    assert "frame_camera" not in gui_emit_qt.emit_app_py(spec_of(name))


def test_the_tk_target_is_untouched(tmp_path):
    """attach drives a Qt canvas; the Tk app.py must not gain a call it
    cannot honour."""
    pdir = rex.build("barbie_capture_v5", project="tk5", vault_dir=tmp_path)
    assert "frame_camera" not in (pdir / "app.py").read_text(encoding="utf-8")


# ======================================================================
# The generated apps
# ======================================================================
@pytest.fixture(scope="module")
def typhon_dir(tmp_path_factory):
    return rex.build("typhon", project="typhon",
                     vault_dir=tmp_path_factory.mktemp("vault"), target="qt")


def test_typhon_passes_its_own_gate(typhon_dir):
    pf = pe.preflight(typhon_dir, "", "linked", rex._requires_of(typhon_dir),
                      toolkit=gpj.toolkit_for(typhon_dir))
    assert pf.ok, pf.lines


def test_typhon_renders_teal(qapp, typhon_dir, forget_generated):
    """The colour on screen, not the colour in the file."""
    ui = construct(typhon_dir)
    ui.resize(1500, 1016)
    image = ui.grab().toImage()
    corner = image.pixelColor(6, image.height() - 6).name()
    assert corner == TYPHON_BG
    ui.close()


def test_the_live_view_is_attached_without_editing_app_py(
        qapp, typhon_dir, forget_generated):
    ui = construct(typhon_dir)
    assert getattr(ui, "_frame_camera_live", None) is not None
    assert ui._frame_camera_live.isActive()


def test_attaching_again_is_harmless(qapp, typhon_dir, forget_generated):
    """Old instructions said to add the line by hand; on a new project that
    would call it twice."""
    ui = construct(typhon_dir)
    first = ui._frame_camera_live
    assert frame_camera.attach(ui) is first


def test_a_wrong_argument_is_still_wrong_the_second_time(
        qapp, typhon_dir, forget_generated):
    ui = construct(typhon_dir)
    with pytest.raises(RuntimeError, match="not an image canvas"):
        frame_camera.attach(ui, view="capture_status")


def test_the_setup_answer_is_kept_beside_app_py(
        qapp, typhon_dir, forget_generated):
    construct(typhon_dir)
    assert frame_camera._LIVE.setup_path == typhon_dir.resolve() / cs.SETUP_FILE


def test_the_play_and_png_raw_buttons_work(qapp, typhon_dir, forget_generated,
                                           tmp_path):
    """Pressed in the generated app, with nothing captured yet: each says
    what it can do in the view line rather than failing."""
    ui = construct(typhon_dir)
    ui.ports.capture_folder.set(str(tmp_path))
    _pump_until(lambda: False, seconds=0.3)
    ui.on_btn_play_pause()
    assert "No frames" in ui.ports.view_status.get()
    ui.on_btn_png_raw()
    assert "No raw file" in ui.ports.view_status.get()


def test_the_setup_button_works(qapp, typhon_dir, forget_generated):
    ui = construct(typhon_dir)
    ui.on_btn_camera_setup()
    assert "skipped" in ui.ports.capture_status.get()     # dialogs disabled
    assert ui.ports.cameras.items(), "the camera list was not refreshed"


#: The Windows fonts, so a label is measured in the font the user sees (the
#: offscreen platform has none of its own, and draws every glyph as a box).
WINDOWS_FONTS = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"


@pytest.mark.skipif(not (WINDOWS_FONTS / "arial.ttf").is_file(),
                    reason="needs the Windows fonts (Arial) to measure text")
def test_the_classified_with_line_shows_all_of_a_typical_answer(
        typhon_dir, tmp_path):
    """It wraps at 336 px, and a typical answer is two lines of Arial 10: at
    one row high (24 px) the second — the counts per class — was cut off.
    Seen in an offscreen grab of a built Typhon with the Windows fonts, and
    measured the same way here, in a fresh interpreter given those fonts."""
    import subprocess

    code = (
        "import sys\n"
        f"sys.path[:0] = [{str(typhon_dir)!r}, {str(ROOT)!r}]\n"
        "from PySide6.QtWidgets import QApplication\n"
        "app = QApplication([])\n"
        "import app as generated, frame_camera\n"
        "ui = generated.App(); ui.resize(1504, 1016)\n"
        "port = ui.ports.classified_with_line\n"
        "port.set('Classified with birds v1 (98f54722) on 2026-10-05 15:54 "
        "\\u2014 good 5, bad timing 3')\n"
        "ui.grab()\n"
        "label = port.widget\n"
        "print(label.font().family(), label.width(), label.height(),"
        " label.fontMetrics().height(), label.heightForWidth(label.width()))\n"
        "frame_camera.disconnect()\n")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", COUNCIL_NO_DIALOGS="1",
               QT_QPA_FONTDIR=str(WINDOWS_FONTS),
               COUNCIL_VAULT_ROOT=str(tmp_path / "vault"),
               PYTHONDONTWRITEBYTECODE="1")
    done = subprocess.run([sys.executable, "-c", code], cwd=str(typhon_dir),
                          env=env, capture_output=True, text=True,
                          timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    family, width, height, line, needs = done.stdout.split()[-5:]
    width, height, line, needs = map(int, (width, height, line, needs))
    assert family == "Arial" and width >= 300, done.stdout
    assert needs > line, "one line: this test no longer shows anything"
    assert needs <= height, f"needs {needs} px, has {height}"


def test_the_export_and_import_pickers_say_which_is_which(
        qapp, typhon_dir, forget_generated):
    """Two pickers one row apart, each an entry and Browse with no label:
    the grab showed two identical empty boxes. Each now says, while empty,
    what goes in it."""
    ui = construct(typhon_dir)
    hints = {name: getattr(ui.ports, name).widget.entry.placeholderText()
             for name in ("export_to", "import_from")}
    assert hints == {"export_to": "Folder to export into",
                     "import_from": "Classifier .zip to import"}
    assert ".typhon-classifier.zip" in ui.ports.import_from.widget.toolTip()
    ui.close()


# ======================================================================
# First run
# ======================================================================
def _first_runs(monkeypatch):
    seen = []
    monkeypatch.setattr(frame_camera, "_first_run", lambda app: seen.append(app))
    return seen


def _pump_until(condition, seconds=3.0):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)


def test_the_wizard_opens_on_first_run(qapp, tmp_path, monkeypatch,
                                       forget_generated):
    monkeypatch.delenv("COUNCIL_NO_DIALOGS", raising=False)
    seen = _first_runs(monkeypatch)
    pdir = rex.build("typhon", project="fresh", vault_dir=tmp_path, target="qt")
    ui = construct(pdir)
    assert seen == [], "it must wait for the window, not open inside __init__"
    _pump_until(lambda: seen)
    assert seen == [ui]


def test_the_wizard_does_not_open_once_the_app_is_set_up(
        qapp, tmp_path, monkeypatch, forget_generated):
    monkeypatch.delenv("COUNCIL_NO_DIALOGS", raising=False)
    seen = _first_runs(monkeypatch)
    pdir = rex.build("typhon", project="done", vault_dir=tmp_path, target="qt")
    cs.save_choice(cs.setup_path(pdir), "basler")
    construct(pdir)
    _pump_until(lambda: seen, seconds=0.3)
    assert seen == []


def test_the_wizard_never_opens_when_dialogs_are_disabled(
        qapp, tmp_path, monkeypatch, forget_generated):
    monkeypatch.setenv("COUNCIL_NO_DIALOGS", "1")
    seen = _first_runs(monkeypatch)
    pdir = rex.build("typhon", project="quiet", vault_dir=tmp_path, target="qt")
    construct(pdir)
    _pump_until(lambda: seen, seconds=0.3)
    assert seen == []


def test_first_run_fills_the_camera_panel(qapp, typhon_dir, monkeypatch,
                                          forget_generated):
    """Finish the wizard and the list is already there — no extra Scan."""
    monkeypatch.setenv("COUNCIL_NO_DIALOGS", "1")
    ui = construct(typhon_dir)
    ui.ports.cameras.set([])
    frame_camera._first_run(ui)
    assert ui.ports.cameras.items()
    assert ui.ports.capture_status.get()



# ======================================================================
# The camera's own set-up: area, settings, presets — in the generated app
# ======================================================================
def _connect(ui, kind):
    ui.on_btn_scan_for_cameras()
    rows = ui.ports.cameras.items()
    ui.ports.cameras.widget.setCurrentRow(
        next(i for i, r in enumerate(rows) if r.endswith(kind)))
    ui.on_btn_connect()
    assert ui.ports.capture_status.get().startswith("Connected"), \
        ui.ports.capture_status.get()


def test_a_preset_saved_in_typhon_is_picked_again_after_a_restart(
        qapp, tmp_path, forget_generated):
    """The user's bird bath, end to end through the generated window: the
    live view runs, a box drawn on it becomes the camera's own area, a
    setting is changed in the settings window, the set-up is saved under a
    name typed into the preset box — then the app is closed, opened again,
    connected again, and picking the name puts the camera back."""
    pdir = rex.build("typhon", project="birds", vault_dir=tmp_path,
                     target="qt")
    ui = construct(pdir)
    picker = ui._camera_presets
    assert ui.ports.camera_area.get().startswith("Camera's area: —")
    _connect(ui, "event")
    _pump_until(lambda: frame_camera._LIVE.previewing, 2.0)
    assert "the whole sensor" in ui.ports.camera_area.get()
    _pump_until(lambda: frame_camera._LIVE.shown_aoi is not None, 2.0)

    ui.ports.roi.set("320, 200, 160, 120")            # drawn on the picture
    ui.on_btn_apply_area_to_camera()
    assert ui.ports.roi.get() == "", "the crop box was cleared"
    assert ui.ports.camera_area.get().startswith(
        "Camera's area: 320, 200, 160, 120 of 640x480")

    ui.on_btn_camera_settings()
    window = frame_camera._LIVE.settings_window
    assert window is not None and not window.isVisible()   # dialogs off
    window.rows["bias.bias_diff_on"].editor.setValue(40)
    window.flush_now()
    assert frame_camera._LIVE.device.state["bias.bias_diff_on"] == 40

    ui.ports.preset.set("Bird bath")                 # typed into the box
    ui.on_btn_save_preset()
    assert "Saved preset 'Bird bath'" in ui.ports.capture_status.get()
    combo = ui.ports.preset.widget
    assert [combo.itemText(i) for i in range(combo.count())] == ["Bird bath"]
    assert window.preset_list.count() == 1, "the window lists it too"
    assert (pdir / "camera_presets.json").is_file(), "kept in the project"

    # Closed and opened again: a new camera object, everything as it came.
    frame_camera.disconnect()
    ui.close()
    ui = construct(pdir)
    assert ui._camera_presets is not picker
    _connect(ui, "event")
    assert frame_camera._LIVE.device.state["bias.bias_diff_on"] == 0
    combo = ui.ports.preset.widget
    assert combo.findText("Bird bath") == 0 and combo.currentIndex() == -1
    combo.setCurrentIndex(0)
    combo.textActivated.emit("Bird bath")            # the user's pick
    _pump_until(lambda: frame_camera._LIVE.job is None, 2.0)
    assert frame_camera.current_area()["area"] == "320, 200, 160, 120"
    assert frame_camera._LIVE.device.state["bias.bias_diff_on"] == 40
    assert "Preset 'Bird bath'" in ui.ports.capture_status.get()
    assert ui.ports.camera_area.get().startswith(
        "Camera's area: 320, 200, 160, 120")


def test_a_new_name_in_the_preset_box_is_a_hint_not_an_error(
        qapp, typhon_dir, forget_generated, capsys):
    ui = construct(typhon_dir)
    _connect(ui, "frame")
    ui.ports.preset.set("Not saved yet")
    ui.ports.preset.widget.textActivated.emit("Not saved yet")   # Return
    assert "Save preset" in ui.ports.capture_status.get()
    assert "failed" not in capsys.readouterr().err


def test_exposure_is_not_capped_at_100_microseconds():
    """v3's spin boxes ran 0..100, so a Basler could never be exposed longer
    than 100 us from the app."""
    shapes = {s["id"]: s for s in gspec("typhon")["shapes"]}
    assert shapes["s04"]["props"]["to"] >= 1_000_000
    assert shapes["s06"]["props"]["to"] >= 24
    assert shapes["s08"]["props"]["to"] >= 1000


def test_the_pop_out_button_opens_a_copy(qapp, typhon_dir, forget_generated,
                                         tmp_path):
    import numpy as np
    from PIL import Image

    Image.fromarray(np.full((48, 64), 77, np.uint8)).save(
        tmp_path / "20260924_120000_frame_000001.png")
    ui = construct(typhon_dir)
    ui.ports.capture_folder.set(str(tmp_path))
    _pump_until(lambda: False, seconds=0.4)
    ui.on_btn_pop_out()
    windows = [w for w in frame_camera._LIVE.popouts if w.isVisible()]
    assert windows, ui.ports.view_status.get()
    assert windows[-1].windowTitle() == "20260924_120000_frame_000001.png"
    for w in windows:
        w.close()


def test_start_uses_a_box_only_when_it_was_changed_after_the_preset(
        qapp, tmp_path, forget_generated):
    """attach hooks the boxes beside Start: a value typed BEFORE a preset
    was applied is older than the preset and Start keeps the preset's; one
    typed after it is the user's latest word and Start applies it."""
    pdir = rex.build("typhon", project="boxes", vault_dir=tmp_path,
                     target="qt")
    ui = construct(pdir)
    _connect(ui, "frame")
    state = frame_camera._LIVE.device.state
    ui.ports.capture_folder.set(str(tmp_path / "runs"))
    ui.ports.exposure.widget.setValue(5000)              # typed first
    frame_camera.apply_camera_settings({"ExposureTime": 12000.0})
    ui.on_btn_start_capture()
    assert state["ExposureTime"] == 12000.0
    assert "Kept the camera's own exposure" in ui.ports.capture_status.get()
    ui.on_btn_stop_capture()
    ui.ports.exposure.widget.setValue(8000)              # typed after
    ui.on_btn_start_capture()
    assert state["ExposureTime"] == 8000.0
    ui.on_btn_stop_capture()


# ======================================================================
# The classifier library, end to end in the generated window
# ======================================================================
def _frames(folder, count=8):
    """Frames whose timing is good (a bright band in the middle) or bad
    (the band low and dim) — every third one bad."""
    import numpy as np
    from PIL import Image

    folder.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        image = np.full((48, 64), 20, np.uint8)
        if i % 3 == 0:
            image[36:44, :] = 70
        else:
            image[18:30, :] = 160 + 8 * i
        Image.fromarray(image).save(folder / f"20261005_120000_frame_{i:06d}.png")
    return sorted(p.name for p in folder.glob("*.png"))


def _pick_model(ui, name):
    """What the user does: pick `name` in the model dropdown's open list."""
    combo = ui.ports.classifier_name.widget
    ui._classifier_picker.refresh()          # as the press that opens it does
    index = combo.findText(name)
    assert index >= 0, (name, ui._classifier_picker.items())
    combo.setCurrentIndex(index)
    combo.textActivated.emit(name)


def _status(ui):
    return ui.ports.classifier_status.get()


@pytest.fixture
def library_vault(tmp_path, monkeypatch):
    """A vault of its own: the shared classifier store is <vault>/classifiers
    (frame_classes.classifier_store), never anywhere real."""
    vault = tmp_path / "vault"
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
    monkeypatch.delenv("FRAME_CLASSES_STORE", raising=False)
    return vault


def test_the_model_dropdown_end_to_end_in_a_built_typhon(
        qapp, tmp_path, library_vault, forget_generated):
    """The user's classifier library, through Typhon's own generated
    handlers and the dropdown attach fills: make a model, mark, train,
    classify a folder and see "classified with"; export it, delete it,
    import it back under the same name; and a second app's model listed
    with where it came from — the store is shared, every model says whose."""
    import subprocess

    import frame_classes as fc
    from council_qt.widgets import classifier_picker as cpk

    pdir = rex.build("typhon", project="birdlab", vault_dir=library_vault,
                     target="qt")
    ui = construct(pdir)
    p = ui.ports
    picker = ui._classifier_picker
    assert isinstance(picker, cpk.ClassifierPicker)
    combo = p.classifier_name.widget
    assert combo.isEditable() and combo.currentText() == "frames"
    assert picker.items() == [] and fc.classifier_store() == \
        library_vault / "classifiers"
    frames = tmp_path / "run"
    names = _frames(frames)

    # Create: a new name typed into the box, Return, then its classes.
    combo.lineEdit().clear()
    _type_and_return(combo, "birds")
    assert "'birds' is new" in _status(ui), _status(ui)
    for cls in ("good", "bad timing"):
        p.new_class.set(cls)
        ui.on_btn_add_class()
    assert p.classes.items() == ["good", "bad timing"]

    # Mark the frames on screen, through the slider and the Mark button.
    p.capture_folder.set(str(frames))
    _pump_until(lambda: len(ui._capture_review.files) == len(names), 2.0)
    rv = ui._capture_review
    for index, name in enumerate(names):
        rv.scrubber.set(index, notify=True)
        _pump_until(lambda: p.current_frame.get() == name, 1.0)
        p.classes.widget.setCurrentRow(1 if index % 3 == 0 else 0)
        ui.on_btn_mark_this_frame()
        assert "marked" in _status(ui), _status(ui)

    # Train, classify the folder: the line says which model version did it.
    ui.on_btn_train()
    assert "Saved as birds v1 (" in _status(ui), _status(ui)
    ui.on_btn_classify_all_frames()
    line = p.classified_with_line.get()
    assert line.startswith("Classified with birds v1 ("), line
    assert "bad timing 3" in line and "good 5" in line
    assert len(p.predictions.items()) == len(names)

    # The dropdown lists it, its row saying where it came from.
    picker.refresh()
    assert picker.items() == ["birds"]
    row = picker.row_of("birds")
    assert row.startswith("birds   v1 (") and "from Typhon (birdlab)" in row

    # Export one file, Delete (moved aside), Import it back by that name.
    out = tmp_path / "out"
    out.mkdir()
    p.export_to.set(str(out))
    ui.on_btn_export()
    assert _status(ui).startswith("Exported birds v1 ("), _status(ui)
    exported = next(out.glob("birds-v1.typhon-classifier.zip"))
    ui.on_btn_delete()
    assert combo.currentText() == "" and p.classes.items() == []
    assert "moved aside" in _status(ui)
    picker.refresh()
    assert picker.items() == []
    assert list((fc.classifier_store() / ".deleted").iterdir())
    p.import_from.set(str(exported))
    p.new_name.set("")
    ui.on_btn_import_()
    assert combo.currentText() == "birds", _status(ui)
    assert p.classes.items() == ["good", "bad timing"]
    assert _status(ui).startswith("Imported birds v1 ("), _status(ui)
    picker.refresh()
    assert picker.items() == ["birds"]
    # The record of what classified the folder survived the round trip.
    p.capture_folder.set(str(tmp_path))                 # another folder ...
    _pump_until(lambda: "Not classified" in p.classified_with_line.get(),
                2.0)
    p.capture_folder.set(str(frames))                   # ... and back
    _pump_until(lambda: p.classified_with_line.get().startswith(
        "Classified with birds v1"), 2.0)
    assert p.classified_with_line.get().startswith("Classified with birds v1")

    # A second app on this PC (a Barbie project) makes a model of its own.
    other = rex.build("barbie_capture_v5", project="barbie_lab",
                      vault_dir=library_vault, target="qt")
    driver = (f"import sys; sys.argv[0] = {str(other / 'main.py')!r}; "
              f"sys.path.insert(0, {str(Path(fc.__file__).parent)!r}); "
              f"import frame_classes as fc; "
              f"print(fc.add_class('barbie-frames', 'good')['summary'])")
    env = dict(os.environ, COUNCIL_VAULT_ROOT=str(library_vault))
    env.pop("FRAME_CLASSES_STORE", None)
    made = subprocess.run([sys.executable, "-c", driver], cwd=str(other),
                          capture_output=True, text=True, timeout=120,
                          env=env)
    assert made.returncode == 0, made.stderr[-2000:]
    _pick_model(ui, "birds")                 # the list is read as it opens
    assert picker.items() == ["barbie-frames", "birds"]
    theirs = picker.row_of("barbie-frames")
    assert "from Barbie" in theirs and "(barbie_lab)" in theirs, theirs
    assert "from Typhon (birdlab)" in picker.row_of("birds")
    # Show: This app, or that app's name, narrows the list.
    p.classifier_filter.set("This app")
    p.classifier_filter.widget.textActivated.emit("This app")
    assert picker.items() == ["birds"]
    filters = [p.classifier_filter.widget.itemText(i)
               for i in range(p.classifier_filter.widget.count())]
    barbie = next(f for f in filters if f.startswith("App: Barbie"))
    p.classifier_filter.set(barbie)
    p.classifier_filter.widget.textActivated.emit(barbie)
    assert picker.items() == ["barbie-frames"]
    # Whose it is: Typhon may open, copy and tag Barbie's model, not train it.
    # The refusal is an answer: the classes and the class typed stay.
    _pick_model(ui, "barbie-frames")
    assert "From Barbie" in _status(ui), _status(ui)
    refused = []
    ui.report_error = lambda what, exc: refused.append(f"{what}: {exc}")
    p.new_class.set("bad timing")
    ui.on_btn_add_class()
    assert "belongs to Barbie" in _status(ui), _status(ui)
    assert p.classes.items() == ["good"] and p.new_class.get() == "bad timing"
    p.new_name.set("barbie-copy")
    ui.on_btn_save_as()
    assert combo.currentText() == "barbie-copy", _status(ui)
    assert p.new_name.get() == "", "the name typed was used"
    assert "from Barbie" in (picker.refresh() or picker.row_of("barbie-copy"))
    # Everything that BELONGS to this app, in one bundle: its own model and
    # its copy of Barbie's (which keeps Barbie as where it came from) — the
    # models a Typhon going its own way can change. The copy is untrained,
    # so it is named as left out, not missed.
    ui.on_btn_export_this_app_s_classifiers()
    assert _status(ui).startswith("Exported 1 classifier (This app: Typhon"), \
        _status(ui)
    assert "Not in it: barbie-copy ('barbie-copy' has no trained model" in \
        _status(ui)
    assert next(out.glob("birdlab-*.typhon-classifiers.zip"))
    assert refused == [], "nothing was refused with a dialog"
    ui.close()


# ----------------------------------------------------------------------
# The model dropdown's helper on its own (council_qt.widgets.
# classifier_picker), against a stand-in for frame_classes
# ----------------------------------------------------------------------
class _Library:
    """frame_classes' list_classifiers / classified_with, counted."""

    def __init__(self):
        self.models = {"frames": "frames   v2 (1a2b3c4d) · 2 classes · from "
                                 "Typhon (lab)",
                       "night": "night   not trained · 1 class · from "
                                "Barbie (b5)"}
        self.listed, self.asked = [], []
        self.broken = ""

    def list_classifiers(self, show=""):
        self.listed.append(show)
        if self.broken:
            raise RuntimeError(self.broken)
        names = sorted(n for n in self.models
                       if show in ("", "All classifiers") or show in n)
        return {"names": names, "rows": [self.models[n] for n in names],
                "filters": ["All classifiers", "This app", "App: Barbie"]}

    def classified_with(self, folder):
        self.asked.append(folder)
        return {"classified_with": f"Classified with frames v2 — {folder}"
                if folder else ""}


class _ComboPort:
    def __init__(self, combo):
        self.widget = combo

    def get(self):
        return self.widget.currentText()


class _LinePort:
    def __init__(self, value=""):
        self.value, self.hooks = value, []

    def get(self):
        return self.value

    def set(self, value):
        self.value = value
        for hook in self.hooks:
            hook(value)

    def on_change(self, hook):
        self.hooks.append(hook)


def _picker(qapp):
    from PySide6.QtWidgets import QComboBox

    from council_qt.widgets import classifier_picker as cpk

    model, show = QComboBox(), QComboBox()
    for box in (model, show):
        box.setEditable(True)
        box.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    activated = []
    model.textActivated.connect(activated.append)
    library, line, folder = _Library(), _LinePort(), _LinePort()
    made = cpk.ClassifierPicker(library, combo=_ComboPort(model),
                                show=_ComboPort(show), line=line,
                                folder=folder)
    return made, model, show, library, line, folder, activated


from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent, QMouseEvent  # noqa: E402


def test_the_dropdown_lists_names_and_draws_whole_rows(qapp):
    from PySide6.QtWidgets import QStyleOptionViewItem

    made, model, show, library, *_ , activated = _picker(qapp)
    assert made.items() == ["frames", "night"]
    assert [show.itemText(i) for i in range(show.count())] == [
        "All classifiers", "This app", "App: Barbie"]
    # The open list draws each item as its whole row; the box keeps names.
    option = QStyleOptionViewItem()
    model.itemDelegate().initStyleOption(option, model.model().index(1, 0))
    assert option.text == library.models["night"]
    assert model.itemData(1, Qt.ItemDataRole.ToolTipRole) == \
        library.models["night"]
    model.setCurrentIndex(1)
    assert model.currentText() == "night"
    assert activated == [], "filling the list opened a model"


def test_the_list_is_read_as_it_opens_never_on_a_timer(qapp):
    made, model, show, library, *_ = _picker(qapp)
    before = len(library.listed)
    _pump_until(lambda: False, seconds=0.3)
    assert len(library.listed) == before, "read while nobody looked"
    library.models["fresh"] = "fresh   not trained · from Typhon (lab)"
    press = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(5, 5),
                        QPointF(5, 5), Qt.MouseButton.LeftButton,
                        Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)
    assert made.eventFilter(model, press) is False       # never consumed
    assert made.items() == ["frames", "fresh", "night"]
    del library.models["fresh"]
    for key, mods in ((Qt.Key.Key_F4, Qt.KeyboardModifier.NoModifier),
                      (Qt.Key.Key_Down, Qt.KeyboardModifier.AltModifier)):
        library.models[f"k{int(key)}"] = "k"
        made.eventFilter(model, QKeyEvent(QEvent.Type.KeyPress, key, mods))
        assert f"k{int(key)}" in made.items()
    count = len(library.listed)
    made.eventFilter(model, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A,
                                      Qt.KeyboardModifier.NoModifier))
    assert len(library.listed) == count, "a keystroke read the store"


def test_the_filter_narrows_the_list(qapp):
    made, model, show, library, *_ = _picker(qapp)
    show.setEditText("night")
    show.textActivated.emit("night")
    assert library.listed[-1] == "night" and made.items() == ["night"]


def _type_and_return(box, text):
    """What the user does, key by key, into an editable box. To the BOX: it
    is its line edit's focus proxy, so a real key press reaches the box and
    the box hands it on — sent to the line edit itself, a Return reached it
    twice (once there, once handed on by the box it went up to)."""
    from PySide6.QtTest import QTest

    box.lineEdit().clear()
    QTest.keyClicks(box, text)
    QTest.keyClick(box, Qt.Key.Key_Return)


def test_return_on_a_typed_name_or_filter_acts_on_it_once(qapp):
    """MEASURED with real key presses: with NoInsert (a typed name must not
    join the list as if it were a saved model) Qt activates only text that
    matches an item — a new model's name typed and Return, or part of a
    name typed into Show and Return, did nothing at all. Now each is acted
    on once; a listed name is still activated by Qt alone, never twice."""
    made, model, show, library, *_ , activated = _picker(qapp)
    _type_and_return(model, "birds")
    assert activated == ["birds"], "a new name typed and Return was ignored"
    _type_and_return(model, "night")
    assert activated == ["birds", "night"], "a listed name, said once"
    assert made.items() == ["frames", "night"], "a typed name joined the list"
    _type_and_return(show, "nig")
    assert library.listed[-1] == "nig" and made.items() == ["night"]


def test_a_name_being_typed_survives_a_refill(qapp):
    made, model, show, library, *_ , activated = _picker(qapp)
    model.lineEdit().setText("bir")
    model.lineEdit().setCursorPosition(2)
    made.refresh()
    assert model.currentText() == "bir" and model.currentIndex() == -1
    assert model.lineEdit().cursorPosition() == 2
    model.setCurrentIndex(made.items().index("night"))
    made.refresh()
    assert model.currentText() == "night"
    assert activated == []


def test_a_store_that_cannot_be_read_says_why_and_keeps_the_box(qapp):
    made, model, show, library, *_ = _picker(qapp)
    model.setEditText("frames")
    library.broken = "classifier_store.json names no folder"
    made.refresh()
    assert made.items() == [] and model.currentText() == "frames"
    assert "names no folder" in model.toolTip()
    library.broken = ""
    made.refresh()
    assert "cannot be read" not in model.toolTip()


def test_the_classified_with_line_follows_the_folder_once_typing_stops(qapp):
    from council_qt.widgets import classifier_picker as cpk

    made, model, show, library, line, folder, _ = _picker(qapp)
    asked = len(library.asked)
    for partial in ("C", "C:", "C:/r", "C:/run"):        # typed, key by key
        folder.set(partial)
    assert len(library.asked) == asked, "searched the store per keystroke"
    _pump_until(lambda: line.get().endswith("C:/run"),
                cpk.LINE_DELAY_MS / 1000 + 1.0)
    assert line.get() == "Classified with frames v2 — C:/run"
    assert library.asked[asked:] == ["C:/run"]


def test_an_app_whose_name_box_is_an_entry_gets_no_dropdown(qapp):
    from types import SimpleNamespace

    from PySide6.QtWidgets import QLineEdit

    from council_qt.widgets import classifier_picker as cpk

    ports = SimpleNamespace(classifier_name=SimpleNamespace(
        widget=QLineEdit()))
    assert cpk.attach_to(_Library(), ports, "classifier_name",
                         "classifier_filter", "classified_with_line",
                         "capture_folder") is None
