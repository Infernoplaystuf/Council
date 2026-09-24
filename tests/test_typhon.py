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
    line above the picture, Play / Pause, PNG / Raw and Pop out, a frame rate
    where v3's unwired "Frame count" was, and real exposure and gain ranges.
    Nothing else moved."""
    v5, typhon = gspec("barbie_capture_v5"), gspec("typhon")
    assert typhon["window"]["bg"] == TYPHON_BG
    assert typhon["window"]["fg"] == v5["window"]["fg"]
    assert typhon["window"]["title"] == "Typhon"
    old = {s["id"]: s for s in v5["shapes"]}
    new = {s["id"]: s for s in typhon["shapes"]}
    assert sorted(set(new) - set(old)) == ["s55", "s56", "s57"]
    strip = lambda s: {k: v for k, v in s.items() if k != "label"}
    changed = sorted(k for k in old if strip(old[k]) != strip(new[k]))
    assert changed == ["s04", "s06", "s08", "s09", "s11", "s23", "s46"]
    assert new["s11"]["drives"] == {}, "a generated browser would own the slider"
    assert new["s09"]["port"] == {"name": "view_status"}
    assert new["s55"]["script"]["function"] == "play_pause"
    assert new["s56"]["script"]["function"] == "toggle_view"
    assert new["s57"]["script"]["function"] == "pop_out"
    assert new["s08"]["port"] == {"name": "frame_rate"}
    assert new["s46"]["script"]["inputs"][-1] == "frame_rate"


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
