"""
Several cameras in one Typhon window (Andy, 16 November 2026): a second
camera connects into a slot of its own, each camera keeps its own capture
set-up, one Start records every one into <folder>/<run>/<camera>/ with a
rig record, one Stop ends them all, Capture now fires every camera Typhon
triggers, and the dropdown (Camera 1, Camera 2, ..., Multicam) chooses what
the main view shows. All on the simulated cameras.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import frame_camera  # noqa: E402
from council_core import capture  # noqa: E402
from tests.sim_cameras import simulated_row  # noqa: E402


def _reset():
    frame_camera.shutdown()
    frame_camera._LIVE.found = None
    frame_camera._LIVE.reviewer = None
    frame_camera._LIVE.rig_run = None
    frame_camera._LIVE.tiles = None
    rig = frame_camera._RIG
    for slot in list(rig.slots):
        if slot is not rig.active and slot.device is None:
            rig.remove_slot(slot.name)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    capture.warm_imports()
    monkeypatch.setenv("TYPHON_NO_SPEED_CHECK", "1")
    _reset()
    yield
    _reset()


def connect(kind):
    listed = frame_camera.list_cameras()
    return frame_camera.connect(simulated_row(listed["rows"], kind))


def two_cameras():
    """cam1 the simulated frame camera, cam2 the simulated event camera
    (the active one, connected last)."""
    connect("frame")
    out = connect("event")
    return out


def settle(seconds=0.4):
    time.sleep(seconds)


def tend(seconds=0.5):
    """What the window's timer does for the cameras not shown."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        frame_camera._tend_others()
        time.sleep(0.03)


# ======================================================================
# Connecting
# ======================================================================
def test_a_second_camera_connects_into_a_slot_of_its_own():
    first = connect("frame")
    assert "beside" not in first["summary"]
    second = connect("event")
    rig = frame_camera._RIG
    assert [s.name for s in rig.connected()] == ["cam1", "cam2"]
    assert rig.active.name == "cam2"
    assert "Connected as cam2, beside cam1" in second["summary"]
    assert rig.slot("cam1").info.kind == "frame"
    assert rig.slot("cam2").info.kind == "event"


def test_the_same_camera_twice_is_refused():
    connect("frame")
    with pytest.raises(RuntimeError, match="already open — in use as cam1"):
        connect("frame")
    assert len(frame_camera._RIG.connected()) == 1


def test_a_camera_is_not_added_while_recording(tmp_path):
    connect("frame")
    frame_camera.start(str(tmp_path))
    try:
        with pytest.raises(RuntimeError, match="capturing"):
            connect("event")
    finally:
        frame_camera.stop()
    assert len(frame_camera._RIG) == 1, "no empty slot left behind"


def test_a_camera_that_does_not_open_leaves_the_rig_as_it_was(monkeypatch):
    connect("frame")
    rig = frame_camera._RIG

    def refuse(host, info):
        raise RuntimeError("no such camera")

    monkeypatch.setattr(frame_camera, "_open_in_slot", refuse)
    with pytest.raises(RuntimeError, match="no such camera"):
        connect("event")
    assert rig.active.name == "cam1"
    assert rig.names() == ["cam1"]


def test_disconnecting_the_shown_camera_shows_the_other():
    two_cameras()
    out = frame_camera.disconnect()
    rig = frame_camera._RIG
    assert [s.name for s in rig.connected()] == ["cam1"]
    assert rig.active.name == "cam1"
    assert rig.slot("cam2").device is None, "kept, empty, for the next"
    connect("event")
    assert rig.active.name == "cam2" and len(rig) == 2
    assert "Showing cam1" in out["summary"]


def test_shutdown_closes_every_camera():
    two_cameras()
    frame_camera.shutdown()
    assert frame_camera._RIG.connected() == []


# ======================================================================
# Each camera its own settings
# ======================================================================
def test_settings_follow_the_shown_camera():
    two_cameras()
    frame_camera._select_camera("cam1")
    frame_camera.set_capture_mode("Triggered: snapshot")
    rig = frame_camera._RIG
    assert rig.slot("cam1").device.capture.triggered
    assert rig.active.name == "cam1"
    # The event camera has no capture set-up of its own to change.
    assert getattr(rig.slot("cam2").device, "capture", None) is None


def test_switching_tells_the_views_as_a_connect_does():
    two_cameras()
    heard = []
    remove = frame_camera.on_camera_change(heard.append)
    try:
        out = frame_camera._select_camera("cam1")
    finally:
        remove()
    assert out["summary"].startswith("Showing cam1")
    assert [h["what"] for h in heard] == ["connected"]
    assert frame_camera.camera_state()["kind"] == "frame"


def test_work_on_a_camera_not_shown_is_not_announced():
    two_cameras()
    heard = []
    remove = frame_camera.on_camera_change(heard.append)
    try:
        with frame_camera._on_slot(frame_camera._RIG.slot("cam1")):
            frame_camera._announce({"what": "stopped"})
        frame_camera._announce({"what": "box"})
    finally:
        remove()
    assert [h["what"] for h in heard] == ["box"]
    assert frame_camera._RIG.active.name == "cam2"


# ======================================================================
# One Start, one Stop
# ======================================================================
def test_one_start_records_every_camera_in_a_folder_each(tmp_path):
    two_cameras()
    out = frame_camera.start(str(tmp_path))
    run = out["run"]
    top = tmp_path / run
    assert out["folder"] == str(top)
    assert "Capturing 2 cameras" in out["summary"]
    rig = frame_camera._RIG
    assert all(s.capturing for s in rig.connected())
    settle()
    tend(0.3)
    stopped = frame_camera.stop()
    assert stopped["summary"].startswith("Stopped 2 cameras")
    assert not any(s.capturing for s in rig.connected())
    for name in ("cam1", "cam2"):
        folder = top / name
        assert (folder / f"{run}_frames.csv").is_file()
        assert (folder / f"{run}_camera.json").is_file()
        assert list(folder.glob(f"{run}_frame_*.png")), name
    record = json.loads((top / f"{run}_rig.json").read_text("utf-8"))
    once = [s.name for s in rig.connected() if frame_camera._streams_once(s)]
    rest = [s.name for s in rig.connected()
            if not frame_camera._streams_once(s)]
    assert record["start_order"] == once + rest, \
        "a one-stream camera (an EVK4) starts first"
    by_name = {c["name"]: c for c in record["cameras"]}
    assert by_name["cam2"]["records_every_event"] is True
    assert by_name["cam1"]["records_every_event"] is False
    end = json.loads((top / f"{run}_rig_end.json").read_text("utf-8"))
    assert set(end["cameras"]) == {"cam1", "cam2"}


def test_each_camera_records_in_its_own_mode(tmp_path):
    """The Basler-like camera saves only what it is triggered for while the
    event camera beside it records all the time — Andy's example."""
    two_cameras()
    frame_camera._select_camera("cam1")
    frame_camera.set_capture_mode("Triggered: snapshot")
    out = frame_camera.start(str(tmp_path))
    top = tmp_path / out["run"]
    settle(0.6)
    frame_camera.stop()
    run = out["run"]
    assert not list((top / "cam1").glob(f"{run}_frame_*.png")), \
        "no trigger, so nothing saved"
    assert list((top / "cam2").glob(f"{run}_frame_*.png"))
    record = json.loads((top / f"{run}_rig.json").read_text("utf-8"))
    by_name = {c["name"]: c for c in record["cameras"]}
    assert by_name["cam1"]["capture"]["mode"] == "Triggered: snapshot"


def test_capture_now_fires_the_triggered_camera_of_the_run(tmp_path):
    two_cameras()
    frame_camera._select_camera("cam1")
    frame_camera.set_capture_mode("Triggered: snapshot")
    frame_camera._select_camera("cam2")      # the event camera is shown
    out = frame_camera.start(str(tmp_path))
    settle(0.4)
    said = frame_camera.capture_now()["summary"]
    assert said.startswith("cam1:")
    settle(0.5)
    frame_camera.stop()
    run = out["run"]
    assert list((tmp_path / run / "cam1").glob(f"{run}_frame_*.png"))


def test_one_camera_saves_exactly_as_before(tmp_path):
    connect("frame")
    out = frame_camera.start(str(tmp_path))
    settle(0.3)
    frame_camera.stop()
    assert list(tmp_path.glob(f"{out['run']}_frame_*.png"))
    assert not list(tmp_path.glob("*_rig.json"))
    assert frame_camera._LIVE.rig_run is None


def test_a_start_given_a_camera_folder_saves_beside_the_run(tmp_path):
    two_cameras()
    first = frame_camera.start(str(tmp_path))
    settle(0.2)
    frame_camera.stop()
    inside = tmp_path / first["run"] / "cam2"
    second = frame_camera.start(str(inside))
    settle(0.2)
    frame_camera.stop()
    assert Path(second["folder"]).parent == tmp_path
    assert second["run"] != first["run"]


def test_the_save_budget_is_shared_by_frame_size():
    two_cameras()
    shares = frame_camera._shared_budgets(frame_camera._RIG.connected())
    assert set(shares) == {"cam1", "cam2"}
    assert all(v >= min(frame_camera.RIG_MIN_BUDGET,
                        capture.WRITE_BUDGET_BYTES // 2)
               for v in shares.values())


def test_a_camera_that_refuses_stops_the_ones_started(tmp_path, monkeypatch):
    two_cameras()
    real = frame_camera._start_one

    def picky(folder, *a, **kw):
        if frame_camera._RIG.active.name == "cam2":
            raise RuntimeError("the folder is refused")
        return real(folder, *a, **kw)

    monkeypatch.setattr(frame_camera, "_start_one", picky)
    with pytest.raises(RuntimeError, match="cam2: the folder is refused.*"
                                           "cam1 started and was stopped"):
        frame_camera.start(str(tmp_path))
    assert not any(s.capturing for s in frame_camera._RIG.connected())
    assert frame_camera._RIG.active.name == "cam2"


def test_the_run_name_is_reserved_as_a_folder(tmp_path):
    (tmp_path / "20260101_000000").mkdir()
    run = frame_camera._claim_rig_run(tmp_path, "20260101_000000")
    assert run == "20260101_000000_2"
    assert (tmp_path / run).is_dir()


# ======================================================================
# The dropdown and Multicam
# ======================================================================
def test_the_grid_is_side_by_side_for_two_and_two_by_two_for_more():
    from council_qt.widgets.camera_tiles import grid_shape

    assert grid_shape(2) == (1, 2)
    assert grid_shape(3) == (2, 2)
    assert grid_shape(4) == (2, 2)


def test_the_dropdown_lists_each_camera_then_multicam(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication, QWidget
    from council_qt.widgets.camera_tiles import MULTICAM, CameraTiles

    app = QApplication.instance() or QApplication([])
    holder = QWidget()
    canvas = QWidget(holder)
    canvas.setGeometry(10, 10, 400, 300)
    picked = []
    view = CameraTiles(canvas, picked.append)
    rows = [{"name": "cam1", "label": "Basler", "active": True,
             "text": "cam1", "frame": None},
            {"name": "cam2", "label": "EVK4", "active": False,
             "text": "cam2", "frame": None}]
    view.show_cameras(rows)
    items = [view.combo.itemText(i) for i in range(view.combo.count())]
    assert items == ["Camera 1 · Basler", "Camera 2 · EVK4", MULTICAM]
    assert view.combo.currentIndex() == 0
    # Below the picture, not over it (Andy, 16 November).
    assert view.combo.parentWidget() is holder
    assert view.combo.geometry().top() >= canvas.geometry().bottom()
    view._chosen(1)
    assert picked == ["cam2"] and not view.multicam
    view._chosen(2)
    view.show_cameras(rows)
    assert view.multicam and view.combo.currentIndex() == 2
    assert not view.grid.isHidden()
    assert set(view.cells) == {"cam1", "cam2"}
    view.show_cameras(rows[:1])
    assert view.combo.isHidden() and view.grid.isHidden()
    view.close()
    holder.deleteLater()
    app.processEvents()
