"""
The Triggering window (2 November 2026): the triggering plugin's panel in a
pop-out of its own, opened from the plugin toolbar, in place of the Basic
tab's trigger boxes.

  * the wireframe: the Basic tab's eight trigger shapes (Capture, From, Per
    trigger, Timer every and their labels) gone, a line saying where they
    went in their place; Capture now (F9) stays beside Start and Stop;
  * the built app: Triggering on the strip, before Classification; its
    window says to connect a camera until one is, then shows the rows the
    mode and source use — Frames per trigger for exact and snapshot,
    Timer every for the Timer, Record for for a record run;
  * a record run started from the window: Capture now records, the status
    says so, Stop ends it.

Offscreen, in temporary vaults; windows are built and grabbed, never shown
on a screen.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import frame_camera  # noqa: E402
from council_core import triggered as tg  # noqa: E402
from tests.sim_cameras import simulated_row, without_camera_sdks  # noqa: E402
from tests.test_typhon_plugins_classify_panel import (  # noqa: E402,F401
    NAME, built, gspec, pump, qapp, ui)

TRIGGER = "typhon.trigger"


def test_the_trigger_boxes_left_the_basic_tab():
    by = {s["id"]: s for s in gspec(NAME)["shapes"]}
    old = {s["id"]: s for s in gspec("typhon")["shapes"]}
    ports = {(s.get("port") or {}).get("name") for s in by.values()}
    for gone in ("capture_mode", "trigger_source", "frames_per_trigger",
                 "timer_s"):
        assert gone not in ports, gone
    assert "capture_mode" in {(s.get("port") or {}).get("name")
                              for s in old.values()}, "today's Typhon keeps them"
    line = by["s96"]
    assert line["kind"] == "label" and "Triggering" in line["label"]
    basic = by["s80"]
    assert basic["x"] <= line["x"] and line["x"] + line["w"] <= \
        basic["x"] + basic["w"] and basic["y"] <= line["y"] and \
        line["y"] + line["h"] <= basic["y"] + basic["h"], "on the Basic page"
    assert by["s92"]["label"] == "Capture now (F9)", "F9 stays"


@pytest.fixture
def camera(ui, monkeypatch):
    without_camera_sdks(monkeypatch)
    monkeypatch.delenv("COUNCIL_SIM_CAMERAS", raising=False)
    rows = frame_camera.list_cameras()["rows"]
    frame_camera.connect(simulated_row(rows, "frame"))
    yield ui
    frame_camera.disconnect()


def open_triggering(ui):
    strip = ui._plugin_strip
    assert TRIGGER in strip.buttons
    strip.buttons[TRIGGER].click()
    win = strip.windows[TRIGGER]
    assert win.isVisible() and win.windowTitle().startswith("Triggering")
    return win.inner


def shown(panel):
    return {k for k, w in panel.rows.items()
            if panel._form.isRowVisible(w)}


def test_the_window_asks_for_a_camera_first(ui):
    panel = open_triggering(ui)
    assert panel.rows == {} and not panel.empty.isHidden()
    assert ui._capture_now_key is not None, "F9 is still the window's"


def test_it_shows_the_rows_the_mode_and_source_use(camera):
    panel = open_triggering(camera)
    panel.refresh()
    assert panel.empty.isHidden()
    assert shown(panel) == {tg.MODE_KEY}, "continuous: the mode only"
    panel.rows[tg.MODE_KEY].setCurrentText(tg.MODE_SNAPSHOT)
    assert frame_camera._LIVE.device.capture.mode == tg.MODE_SNAPSHOT
    assert shown(panel) == {tg.MODE_KEY, tg.SOURCE_KEY, tg.PER_KEY}
    panel.rows[tg.SOURCE_KEY].setCurrentText(tg.TIMER)
    assert tg.EVERY_KEY in shown(panel)
    panel.rows[tg.MODE_KEY].setCurrentText(tg.MODE_RECORD)
    assert shown(panel) == {tg.MODE_KEY, tg.SOURCE_KEY, tg.EVERY_KEY,
                            tg.RECORD_KEY}
    record = panel.rows[tg.RECORD_KEY]
    assert record.specialValueText() == "until the next trigger"
    record.setValue(1.5)
    assert frame_camera._LIVE.device.capture.record_s == 1.5


def test_a_record_run_from_the_window(camera, tmp_path):
    panel = open_triggering(camera)
    panel.refresh()
    panel.rows[tg.MODE_KEY].setCurrentText(tg.MODE_RECORD)
    panel.rows[tg.RECORD_KEY].setValue(0.0)
    frame_camera.start(str(tmp_path / "frames"))
    try:
        panel.refresh()
        assert not panel.rows[tg.MODE_KEY].isEnabled(), "fixed while capturing"
        assert panel.capture_button.isEnabled()
        panel.capture_button.click()
        assert "recording" in panel.said.text()
        session = frame_camera._LIVE.session
        assert pump(5.0, until=lambda: session.stats().recorded >= 3)
        panel.refresh()
        assert "RECORDING" in panel.status_label.text(), \
            panel.status_label.text()
        panel.capture_button.click()
        assert pump(2.0, until=lambda: "waiting" in (panel.refresh() or
                                                      panel.status_label
                                                      .text()))
    finally:
        frame_camera.stop()
    panel.refresh()
    assert panel.rows[tg.MODE_KEY].isEnabled()
