"""
End-to-end: barbie_capture_v4 generated as a Qt app, with a live camera in it.

This builds the real project with the real emitter, imports the real generated
code, and drives it with the simulated camera. No window is ever shown — the
app is constructed offscreen and never `show()`n.

It is the test that would have caught every blocker this work had to clear:
the policy gate refusing PySide6, the gate refusing council_core, the Qt target
being unreachable from the build path, and the live view having nowhere to put
a frame.
"""
from __future__ import annotations

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
import gui_projects as gpj
import python_envs as pe
import run_example_gui as rex
from PySide6.QtWidgets import QApplication

#: What app.py gains — the one line a user adds to wire the live view.
ATTACH = """
import frame_camera


def _attach(app):
    frame_camera.attach(app)
"""


@pytest.fixture(scope="module")
def qt_app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    """barbie_capture_v4, generated for Qt, exactly as the CLI would."""
    vault = tmp_path_factory.mktemp("vault")
    pdir = rex.build("barbie_capture_v4", project="v4", vault_dir=vault,
                     target="qt")
    yield pdir


@pytest.fixture
def app(qt_app, project):
    """The generated App, constructed offscreen and never shown."""
    kept_path, kept_modules = list(sys.path), dict(sys.modules)
    sys.path.insert(0, str(project))
    for name in ("app", "handlers", "ui", "ui.main_ui", "ui.ports",
                 "ui.widgets"):
        sys.modules.pop(name, None)
    try:
        import app as generated
        made = generated.App()
        yield made
    finally:
        frame_camera.disconnect()
        sys.path[:] = kept_path
        for name in ("app", "handlers", "ui", "ui.main_ui", "ui.ports",
                     "ui.widgets"):
            sys.modules.pop(name, None)
        sys.modules.update({k: v for k, v in kept_modules.items()
                            if k not in sys.modules})


# ======================================================================
# The project itself
# ======================================================================
def test_the_project_is_a_qt_app(project):
    assert "PySide6" in (project / "app.py").read_text(encoding="utf-8")
    assert gpj.toolkit_for(project) == "qt"


def test_the_project_passes_its_own_gate(project):
    pf = pe.preflight(project, "", "linked", rex._requires_of(project),
                      toolkit=gpj.toolkit_for(project))
    assert pf.ok, pf.lines


def test_the_camera_handlers_were_generated(project):
    source = (project / "handlers.py").read_text(encoding="utf-8")
    for expected in ("on_btn_scan_for_cameras", "on_btn_connect",
                     "on_btn_start_capture", "on_btn_stop_capture",
                     "on_btn_apply_area_to_camera", "on_btn_full_sensor"):
        assert f"def {expected}" in source, f"{expected} was not generated"
    assert "from frame_camera import" in source


def test_the_wireframe_does_not_declare_a_camera_sdk(project):
    """The app must START on a machine with no SDK, and say so when asked.

    Declaring pypylon in `requires` would make main.py refuse to launch at all
    on every machine that has not got it — which is every machine so far.
    """
    requires = rex._requires_of(project)
    assert not any("pylon" in r or "metavision" in r for r in requires)


# ======================================================================
# The live view
# ======================================================================
def test_the_app_has_the_ports_the_camera_drives(app):
    for name in ("live_view", "capture_folder", "roi", "capture_status",
                 "cameras", "camera_notes", "exposure", "gain"):
        assert getattr(app.ports, name, None) is not None, f"no {name} port"


def test_attach_starts_a_timer_that_is_kept_alive(app):
    """A QTimer whose last reference goes out of scope is collected.

    The live view would then work for exactly as long as attach()'s frame
    existed, and afterwards die silently.
    """
    timer = frame_camera.attach(app)
    assert timer.isActive()
    assert app._frame_camera_live is timer


def test_attach_refuses_a_port_that_is_not_an_image_canvas(app):
    with pytest.raises(RuntimeError, match="not an image canvas"):
        frame_camera.attach(app, view="capture_status")


def test_attach_names_a_port_that_does_not_exist(app):
    with pytest.raises(RuntimeError, match="no 'nonesuch' port"):
        frame_camera.attach(app, view="nonesuch")


def test_a_simulated_camera_reaches_the_generated_canvas(app, tmp_path):
    """The whole thing, end to end: camera -> mailbox -> timer -> canvas."""
    canvas = app.ports.live_view.widget
    shown = []
    canvas.set_array = lambda arr, copy=True: shown.append(arr)

    app.ports.capture_folder.set(str(tmp_path))
    app.on_btn_scan_for_cameras()
    # A listbox port's value is its SELECTION, so the scan populates items()
    # and get() stays empty until the user picks one.
    assert app.ports.cameras.items(), "the scan found nothing"
    assert app.ports.cameras.get() == [], "nothing should be selected yet"

    app.ports.live_view.widget  # noqa: B018  (the canvas exists)
    app.ports.cameras.widget.setCurrentRow(0)
    app.on_btn_connect()
    assert "Connected" in app.ports.capture_status.get()
    frame_camera.start(str(tmp_path))
    frame_camera.start(str(tmp_path))
    try:
        deadline = time.monotonic() + 5.0
        while not shown and time.monotonic() < deadline:
            frame_camera.pump(canvas.set_array, app.ports.capture_status.set)
            time.sleep(0.01)
    finally:
        frame_camera.stop()

    assert shown, "no frame ever reached the generated canvas"
    assert hasattr(shown[0], "shape"), "the canvas was not handed an array"
    assert "fps" in app.ports.capture_status.get()


def test_captured_frames_land_in_the_folder_the_scrubber_browses(app, tmp_path):
    """The capture folder IS the browse folder — the design in one test."""
    app.ports.capture_folder.set(str(tmp_path))
    frame_camera.list_cameras()
    frame_camera.connect("1.")
    frame_camera.start(app.ports.capture_folder.get())
    time.sleep(0.2)
    frame_camera.stop()
    assert sorted(tmp_path.glob("*.png")), "nothing landed in the folder"


def test_the_area_button_sets_the_sensor_area(app):
    frame_camera.list_cameras()
    frame_camera.connect("1.")
    app.ports.roi.set("0, 0, 320, 240")
    app.on_btn_apply_area_to_camera()
    assert frame_camera._LIVE.device.roi().as_tuple() == (0, 0, 320, 240)
    assert app.ports.roi.get() == "0, 0, 320, 240"


def test_a_failed_camera_call_reports_instead_of_crashing(app):
    """The generated handler's report_error path, with a real failure."""
    app.ports.cameras.set([])
    app.on_btn_connect()            # nothing scanned, nothing chosen
    # It must not raise. The generated handler catches and reports.


def test_connect_accepts_a_listbox_selection(app):
    """The generated handler passes ports.cameras.get(), which is a LIST.

    str() of a list is "['1. ...']", which resolves to no camera at all.
    """
    app.on_btn_scan_for_cameras()
    app.ports.cameras.widget.setCurrentRow(1)
    selection = app.ports.cameras.get()
    assert isinstance(selection, list) and selection
    assert frame_camera._chosen(selection).kind == "event"


def test_connecting_with_nothing_selected_says_so(app):
    app.on_btn_scan_for_cameras()
    with pytest.raises(RuntimeError, match="choose a camera"):
        frame_camera._chosen(app.ports.cameras.get())
