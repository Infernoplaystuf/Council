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


# ======================================================================
# The camera's lines (5 November 2026): the levels and "Find my trigger
# line", on the fake Basler of test_typhon_plugins_basler_lines
# ======================================================================
@pytest.fixture
def basler(ui, monkeypatch):
    from tests.test_basler_trigger import FakePylon
    from tests.test_typhon_plugins_basler_lines import LevelBasler
    without_camera_sdks(monkeypatch)
    monkeypatch.delenv("COUNCIL_SIM_CAMERAS", raising=False)
    cam = LevelBasler(sources=("Software", "Line1", "Line2", "Line3"),
                      line_modes={"Line1": "Input", "Line2": "Output",
                                  "Line3": "Input"})
    frame_camera._plugin_host().instance("typhon.basler").sdk = FakePylon(cam)
    rows = frame_camera.list_cameras()["rows"]
    first = frame_camera._RIG.slots[0]
    kept = first.name
    frame_camera.connect(next(r for r in rows if "Basler" in r or "boA" in r))
    yield ui, cam
    frame_camera.disconnect()
    if first.name != kept:     # the Basler named its slot: the name put back
        frame_camera._RIG.rename_slot(first.name, kept)


def test_a_simulated_camera_shows_no_lines(camera):
    panel = open_triggering(camera)
    panel.refresh()
    assert panel.lines_label.isHidden() and panel.find_button.isHidden()


def test_the_lines_levels_are_shown(basler):
    ui, cam = basler
    panel = open_triggering(ui)
    cam.levels["Line3"] = True
    panel.refresh()
    assert not panel.find_button.isHidden()
    text = panel.lines_label.text()
    assert "Line1 low" in text and "Line3 HIGH" in text, text
    assert "Line2" not in text, "an output is not an input"


def test_find_my_trigger_line_then_use_it(basler, monkeypatch):
    import threading
    import time
    from typhon_plugins.trigger import panel as tp
    ui, cam = basler
    panel = open_triggering(ui)
    panel.refresh()
    panel.find_button.click()
    assert panel.finding is not None and "Watching Line1, Line3" in \
        panel.said.text()
    assert panel.find_button.text() == "Stop looking"

    def send():
        for _ in range(3):
            cam.pulse("Line3", 0.01)
            time.sleep(0.03)
    sender = threading.Thread(target=send)
    sender.start()
    assert pump(5.0, until=lambda: panel.finding.counts.get("Line3") == 3)
    sender.join()
    panel._find_until = 0.0                     # the time is up
    assert pump(2.0, until=lambda: panel.finding is None)
    assert panel.said.text().startswith("Your trigger cable is on Line3"), \
        panel.said.text()
    assert not panel.use_button.isHidden()
    assert panel.use_button.text() == "Use Line3"
    assert cam.value("CounterEventSource", "Counter2") == "Off", "put back"
    panel.rows[tg.MODE_KEY].setCurrentText(tg.MODE_SNAPSHOT)
    panel.use_button.click()
    assert frame_camera._LIVE.device.capture.source == "Line3"
    assert panel.use_button.isHidden()


def test_the_finder_waits_while_capturing(basler, tmp_path):
    ui, cam = basler
    panel = open_triggering(ui)
    frame_camera.start(str(tmp_path / "frames"))
    try:
        panel.refresh()
        assert not panel.find_button.isEnabled()
        with pytest.raises(RuntimeError, match="Stop the capture first"):
            panel.plugin.find_line()
    finally:
        frame_camera.stop()
