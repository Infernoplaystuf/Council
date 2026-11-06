"""
Send trigger (Andy, 5 November 2026: "a button inside of Typhon to send a
trigger to a camera selected from a drop down … a plugin so it can be
optionally added"): typhon.send_trigger.

  * optional: built in, but a new project does not run it; ticked in
    Plugins… its button appears on the toolbar strip;
  * its window: the connected cameras in a drop-down; a press is Capture
    now on the camera picked — one trigger of the run in progress there,
    counted in the run's trigger file; with no run, what to set up.

Offscreen, in temporary vaults and plugin homes.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import frame_camera  # noqa: E402
import typhon_plugin_api as api  # noqa: E402
from council_core import plugin_host as ph  # noqa: E402
from council_core import triggered as tg  # noqa: E402
from tests.sim_cameras import simulated_row, without_camera_sdks  # noqa: E402
from tests.test_typhon_plugins_classify_panel import (  # noqa: E402,F401
    built, pump, qapp, ui)

SEND = "typhon.send_trigger"


def test_it_is_built_in_but_not_run_by_a_new_project():
    assert SEND in dict(ph.BUILTIN_PLUGINS)
    assert SEND in ph.OPTIONAL_BUILTINS
    assert SEND not in ph.default_plugins()
    assert "typhon.trigger" in ph.default_plugins()
    assert api.has("context.capture_now")


def test_ticked_in_plugins_it_gets_a_button(ui):
    strip = ui._plugin_strip
    assert SEND not in strip.buttons, "off in a new project"
    dlg = strip.open_plugins_window()
    box = dlg.boxes[SEND]
    assert not box.isChecked() and box.text() == "Send trigger"
    box.setChecked(True)
    try:
        assert SEND in strip.buttons
        strip.buttons[SEND].click()
        win = strip.windows[SEND]
        assert win.isVisible() and win.windowTitle().startswith(
            "Send trigger")
    finally:
        box.setChecked(False)
    assert SEND not in strip.buttons
    dlg.deleteLater()


def open_send(ui, monkeypatch):
    without_camera_sdks(monkeypatch)
    monkeypatch.delenv("COUNCIL_SIM_CAMERAS", raising=False)
    strip = ui._plugin_strip
    strip.switch(SEND, True)
    strip.buttons[SEND].click()
    return strip.windows[SEND].inner


def test_the_drop_down_lists_the_connected_cameras(ui, monkeypatch):
    panel = open_send(ui, monkeypatch)
    try:
        panel.refresh()
        assert panel.camera_box.count() == 0
        assert not panel.send_button.isEnabled()
        rows = frame_camera.list_cameras()["rows"]
        frame_camera.connect(simulated_row(rows, "frame"))
        panel.refresh()
        assert panel.camera_box.count() == 1
        name = frame_camera._RIG.active.name
        assert panel.chosen() == name
        assert panel.camera_box.currentText().startswith(f"{name} — ")
        assert panel.send_button.isEnabled()
        said = panel.send()
        assert said.startswith(f"{name}: ") and "Triggered" in said, said
    finally:
        frame_camera.disconnect()
        ui._plugin_strip.switch(SEND, False)


def test_a_press_is_one_trigger_of_the_run_on_that_camera(ui, monkeypatch,
                                                          tmp_path):
    panel = open_send(ui, monkeypatch)
    try:
        rows = frame_camera.list_cameras()["rows"]
        frame_camera.connect(simulated_row(rows, "frame"))
        for key, value in ((tg.MODE_KEY, tg.MODE_SNAPSHOT),
                           (tg.SOURCE_KEY, tg.SOFTWARE),
                           (tg.PER_KEY, 2)):
            assert frame_camera.set_camera_setting(key, value)["ok"]
        frame_camera.start(str(tmp_path / "frames"))
        session = frame_camera._LIVE.session
        panel.refresh()
        for n in (1, 2):
            said = panel.send()
            assert "Capture now" in said, said
            assert pump(5.0, until=lambda: session.stats().recorded
                        >= 2 * n), n
        frame_camera.stop()
        csv = next((tmp_path / "frames").glob(f"*{tg.TRIGGERS_SUFFIX}"))
        assert len(csv.read_text(encoding="utf-8").splitlines()) == 3, \
            "a header and the two triggers sent"
    finally:
        frame_camera.disconnect()
        ui._plugin_strip.switch(SEND, False)
