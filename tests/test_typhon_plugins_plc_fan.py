"""
typhon.plc's fan-blade strobe lock (Andy, 24 November 2026: the PLC's
timer triggers the camera every second today; the goal is a picture of one
fan blade in the same spot every time, with no sensor on the fan; the
timer stays as a backup, switched by hand).

  * the blade's angle in a picture, face-on (1, 3, 5 blades; any blade or
    a marked one) and side-on, and the hub found from a few pictures;
  * the lock's sums, and the lock holding a drawn fan still when the rpm
    entered is off, the camera's rate changes arriving pictures late;
  * the lock following a run's frames CSV and PNGs on disk (FanRun);
  * in a "Typhon (plugins)" project: the PLC's timer switched off and on
    (MHR5), the camera set for the fan, the reference taken, a run with the
    lock writing its phase log and its block in the end record, and the
    window's fan section offscreen.
"""
from __future__ import annotations

import csv
import os
import random
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

import numpy as np  # noqa: E402
import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import frame_camera  # noqa: E402
from council_core import blade_phase as bp  # noqa: E402
from tests.test_typhon_plugins_plc import PLC, plc_on  # noqa: E402
from tests.test_typhon_plugins_trigger import (  # noqa: E402,F401
    connect_sim, home, host, plugin_project, restore, run_files, wait_for)
from typhon_plugins.plc import strobe  # noqa: E402
from typhon_plugins.plc.fan import FanRun, FanSettings  # noqa: E402

SIZE = (240, 180)
HUB = dict(hub_x=120.0, hub_y=90.0, ring_in=30.0, ring_out=75.0)


# ======================================================================
# The blade in a picture
# ======================================================================
@pytest.mark.parametrize("blades", [1, 3, 5])
@pytest.mark.parametrize("any_blade", [True, False])
def test_the_face_on_angle_is_found_to_a_fraction_of_a_degree(blades,
                                                              any_blade):
    view = bp.FanView(blades=blades, any_blade=any_blade, **HUB)
    mark = not any_blade
    ref = bp.profile_of(bp.fan_picture(SIZE, 0.0, blades, mark=mark,
                                       noise=4), view)
    for turn in (0.4, 3.0, -8.5, 13.0, 29.0, -50.0):
        img = bp.fan_picture(SIZE, turn, blades, mark=mark, noise=4, seed=7)
        got = bp.measure_image(img, view, ref)
        assert got.value == pytest.approx(bp.wrap(turn, view.pitch_deg),
                                          abs=0.3), turn
        assert got.score > 0.8


def test_a_marked_blade_is_told_from_the_others():
    view = bp.FanView(blades=3, any_blade=False, **HUB)
    ref = bp.profile_of(bp.fan_picture(SIZE, 0.0, 3, mark=True), view)
    got = bp.measure_image(bp.fan_picture(SIZE, 120.0 + 2.0, 3, mark=True),
                           view, ref)
    assert got.value == pytest.approx(122.0, abs=0.5), \
        "a whole blade on is 120 degrees, not 0"


def test_the_side_on_shift_follows_the_turn():
    view = bp.FanView(view=bp.VIEW_SIDE, box=(20, 70, 200, 40))
    ref = bp.profile_of(bp.fan_picture(SIZE, 10.0, 3, view=bp.VIEW_SIDE),
                        view)
    shifts = [bp.measure_image(bp.fan_picture(SIZE, a, 3, view=bp.VIEW_SIDE,
                                              noise=3), view, ref).value
              for a in (8.0, 9.0, 10.0, 11.0, 12.0)]
    assert shifts[2] == pytest.approx(0.0, abs=0.5)
    assert all(b > a for a, b in zip(shifts, shifts[1:])), shifts


def test_the_hub_is_found_from_pictures_of_the_spinning_fan(tmp_path):
    from PIL import Image

    paths = []
    for i, a in enumerate((0, 17, 41, 77, 100, 133)):
        p = tmp_path / f"f{i}.png"
        Image.fromarray(bp.fan_picture(SIZE, a, 3, noise=2, seed=i)).save(p)
        paths.append(p)
    x, y, r_in, r_out = bp.find_hub(paths)
    assert (x, y) == (pytest.approx(120, abs=2), pytest.approx(90, abs=2))
    assert 5 < r_in < r_out < 90
    with pytest.raises(ValueError, match="at least 3"):
        bp.find_hub(paths[:2])


# ======================================================================
# The lock's sums
# ======================================================================
def test_turns_rate_and_blur():
    assert strobe.turns_per_picture(1200, 1.0) == 20
    assert strobe.turns_per_picture(1250, 1.0) == 21
    assert strobe.turns_per_picture(30, 1.0) == 1
    assert strobe.frame_rate(1200, 20) == pytest.approx(1.0)
    assert strobe.blur_deg(1200, 100) == pytest.approx(0.72)
    assert strobe.wrap(190.0, 360.0) == pytest.approx(-170.0)
    assert strobe.wrap(-36.0, 72.0) == pytest.approx(36.0)


def simulate(true_rpm, entered, blades=5, any_blade=True, side=False,
             frames=150, lag=2, noise=0.2, seed=1):
    """A fan at true_rpm, the camera at the lock's rate; a new rate reaches
    the camera `lag` pictures after the lock asks (pictures in flight)."""
    rnd = random.Random(seed)
    turns = strobe.turns_per_picture(entered, 1.0)
    pitch = 360.0 / blades if any_blade else 360.0
    lock = strobe.Lock(rate=strobe.frame_rate(entered, turns), turns=turns,
                       pitch_deg=pitch, side=side,
                       px_per_deg=None if side else 1.0,
                       max_jump=60.0 if side else 0.0)
    t, rate, pending, errors = 0.0, lock.rate, [], []
    start = None
    for n in range(frames):
        for due, new in list(pending):
            if due <= n:
                rate = new
                pending.remove((due, new))
        t += 1.0 / rate
        angle = 360.0 * true_rpm / 60.0 * t + 7.0
        if side:
            start = angle if start is None else start
            # the blades' place along the box: 3 px a degree near the
            # reference, the same again a turn on
            near = strobe.wrap(angle - start, 360.0)
            value = 3.0 * near + rnd.gauss(0, noise * 3)
            errors.append(near)
        else:
            value = strobe.wrap(angle, pitch) + rnd.gauss(0, noise)
            errors.append(strobe.wrap(angle, pitch))
        new = lock.add(n, value, 0.9)
        if new:
            lock.took(new)
            pending.append((n + lag, new))
    return lock, errors


@pytest.mark.parametrize("true_rpm,entered,blades,any_blade", [
    (1203.0, 1200.0, 5, True),       # 0.25 % off: 18 degrees a picture
    (1188.0, 1200.0, 5, True),
    (1200.5, 1200.0, 1, False),
    (3000.0, 3000.0, 3, True),
])
def test_the_lock_holds_a_drawn_fan_still(true_rpm, entered, blades,
                                          any_blade):
    lock, errors = simulate(true_rpm, entered, blades, any_blade)
    held = errors[-30:]
    assert max(held) - min(held) < 1.0, held
    assert all(abs(e) < 1.0 for e in held), "at the reference spot"
    # With alike blades, a rate a whole blade off looks the same: the speed
    # is known to a blade's worth of turn per picture (1188 holds still at
    # 1200's rate with 5 blades), the one nearest what was entered.
    step = lock.rate * 60.0 * lock.pitch_deg / 360.0
    off = (lock.rpm() - true_rpm) / step
    assert off == pytest.approx(round(off), abs=0.05)
    assert lock.summary()["in_tolerance"] >= 60


def test_the_side_on_lock_measures_its_own_scale():
    lock, errors = simulate(1200.3, 1200.0, side=True)
    assert lock.px_per_deg == pytest.approx(3.0, rel=0.15)
    held = errors[-30:]
    assert max(held) - min(held) < 1.0, held


def test_a_side_on_view_too_far_off_says_so():
    lock, _ = simulate(1230.0, 1200.0, side=True, frames=20)
    lock_words = lock.said or lock.summary()["words"]
    assert "jump too far" in lock_words


# ======================================================================
# The lock on a run's files
# ======================================================================
class FakeRun:
    """A run saving drawn fan pictures and its frames CSV, the camera's
    rate taking effect `lag` pictures after it is set."""

    def __init__(self, folder, run, rpm, blades=3, lag=2):
        self.folder, self.run, self.rpm, self.lag = folder, run, rpm, lag
        self.blades = blades
        self.t, self.n, self.rate, self.pending = 0.0, 0, 1.0, []
        self.index = open(folder / f"{run}_frames.csv", "w", newline="",
                          encoding="utf-8")
        self.rows = csv.writer(self.index)
        self.rows.writerow(("file", "index", "timestamp_us"))
        self.index.flush()
        self.angles = []

    def set_rate(self, rate):
        self.pending.append((self.n + self.lag, rate))
        return rate

    def picture(self):
        from PIL import Image

        for due, new in list(self.pending):
            if due <= self.n:
                self.rate = new
                self.pending.remove((due, new))
        self.t += 1.0 / self.rate
        angle = 360.0 * self.rpm / 60.0 * self.t + 11.0
        self.angles.append(bp.wrap(angle, 360.0 / self.blades))
        name = f"{self.run}_frame_{self.n:06d}.png"
        Image.fromarray(bp.fan_picture(SIZE, angle, self.blades, noise=3,
                                       seed=self.n)).save(self.folder / name)
        self.rows.writerow((name, self.n, 0))
        self.index.flush()
        self.n += 1


def fan_settings(**more):
    view = bp.FanView(blades=3, **HUB)
    ref = [float(v) for v in bp.profile_of(bp.fan_picture(SIZE, 0.0, 3),
                                           view)]
    values = dict(enabled=True, rpm=600.0, max_fps=1.0, blades=3,
                  reference=ref, reference_name="ref.png", **HUB)
    values.update(more)
    f = FanSettings.from_values(values)
    f.reference_geometry = f.geometry()
    return f


def test_the_lock_follows_a_run_on_disk(tmp_path):
    f = fan_settings()
    assert f.problem() == "" and f.turns == 10
    run = FakeRun(tmp_path, "run1", rpm=601.5)
    lock = FanRun(tmp_path, "run1", f, run.set_rate, measure=bp.measure)
    for _ in range(80):
        run.picture()
        lock.step()
    lock.stop()
    held = run.angles[-20:]
    assert all(abs(a) < 1.0 for a in held), held
    s = lock.summary()
    assert s["measured"] == 80 and s["nudges"] >= 3
    assert s["rpm_measured"] == pytest.approx(601.5, abs=0.3)
    rows = lock.rows()
    assert rows[-1]["in_tolerance"] == 1 and rows[-1]["file"].endswith(".png")
    text = lock.csv_bytes().decode("utf-8")
    assert text.splitlines()[0].startswith("file,index,measured,turn_deg")


def test_a_lock_behind_measures_the_newest_and_logs_the_rest(tmp_path):
    f = fan_settings()
    run = FakeRun(tmp_path, "run2", rpm=600.0)
    lock = FanRun(tmp_path, "run2", f, run.set_rate, measure=bp.measure)
    for _ in range(4):
        run.picture()
    assert lock.step() == 1
    assert lock.behind == 3 and [r["note"] for r in lock.rows()] == \
        ["behind"] * 3 + [""]


def test_the_fan_settings_round_trip_and_say_what_is_missing():
    f = FanSettings.from_values({"rpm": "900", "blades": 0, "view": "?",
                                 "reference": [1, 2, "x"]})
    assert f.rpm == 900.0 and f.blades == 1 and f.view == strobe.VIEW_FACE
    assert f.reference == [], "a bad reference is dropped"
    assert "ring" in f.problem()
    f = fan_settings()
    assert FanSettings.from_values(f.values()) == f
    f.hub_x += 5
    assert "take the reference again" in f.problem()


# ======================================================================
# In a project
# ======================================================================
def test_the_window_switches_the_plcs_timer(plugin_project):
    plugin = plc_on(plugin_project, {"simulated": True})
    try:
        sim = plugin.sim
        sim.timer_s = 0.05
        assert wait_for(lambda: len(sim.timer_pulses) >= 2, 3.0)
        said = plugin.set_timer(False)
        assert "computer triggers" in said, said
        assert sim.holding[4] == 1
        n = len(sim.timer_pulses)
        time.sleep(0.3)
        assert len(sim.timer_pulses) == n, "the timer stopped"
        assert "triggers the camera again" in plugin.set_timer(True)
        assert wait_for(lambda: len(sim.timer_pulses) > n, 3.0)
        assert plugin.read_now()["timer"] is True
        plugin.update_settings({"timer_register": "MHR3"})
        assert "of its own" in plugin.set_timer(False)
    finally:
        plugin.deactivate()


def newest_picture(folder):
    return sorted(Path(folder).rglob("*.png"))[-1]


def test_a_run_with_the_lock_writes_its_log_and_block(plugin_project,
                                                      tmp_path):
    from PIL import Image

    plugin = plc_on(plugin_project, {"simulated": False})
    try:
        said = plugin.fan_camera()
        assert "AcquisitionFrameRate" in said, said
        folder = tmp_path / "frames"
        out = frame_camera.start(str(folder))
        assert out.get("ok", True), out
        session = frame_camera._LIVE.session
        assert wait_for(lambda: session.stats().recorded >= 2)
        frame_camera.stop()
        first = newest_picture(folder)
        h, w = np.asarray(Image.open(first)).shape[:2]
        plugin.update_fan({"enabled": True, "rpm": 600.0, "blades": 2,
                           "hub_x": w / 2, "hub_y": h / 2,
                           "ring_in": min(w, h) * 0.1,
                           "ring_out": min(w, h) * 0.4})
        assert "Reference" in plugin.fan_reference(first)
        assert plugin.fan.problem() == ""
        kept = host().settings(PLC)
        assert kept["fan_blades"] == 2, "kept"
        ref_file = plugin.ctx.data_dir() / kept["fan_reference_file"]
        assert ref_file.is_file()
        assert plugin._load_reference(ref_file.name) == plugin.fan.reference
        folder2 = tmp_path / "frames2"
        out = frame_camera.start(str(folder2))
        assert out.get("ok", True), out
        session = frame_camera._LIVE.session
        assert wait_for(lambda: session.stats().recorded >= 4)
        assert wait_for(lambda: plugin.fan_run is not None and
                        plugin.fan_run.lock.measured + plugin.fan_run.behind
                        >= 1, 10.0)
        frame_camera.stop()
        end = next(folder2.glob("*_run_end.json"))
        run = end.name[:-len("_run_end.json")]
        block = run_files(folder2, run)["end"]["plugins"][PLC]["fan"]
        assert block["measured"] + block["behind"] >= 1
        assert block["log"] == f"{run}_blade_phase.csv"
        log = folder2 / block["log"]
        assert log.is_file()
        assert log.read_text(encoding="utf-8").startswith("file,index")
        assert plugin.fan_run is None
    finally:
        plugin.deactivate()


def test_its_fan_section_is_built_offscreen(plugin_project):
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    plugin = plc_on(plugin_project, {"simulated": True})
    try:
        panel = host().build_panel(PLC)
        fan = panel.fan_panel
        assert fan.objectName() == "plc_fan"
        fan.rpm.setValue(1500.0)
        fan.blades.setValue(4)
        assert plugin.fan.rpm == 1500.0 and plugin.fan.blades == 4
        assert "25 turn(s) per picture" in fan.info.text(), fan.info.text()
        assert "to do:" in fan.info.text()
        fan.view.setCurrentText(strobe.VIEW_SIDE)
        assert plugin.fan.view == strobe.VIEW_SIDE
        assert fan.side_row.isVisibleTo(fan)
        assert not fan.face_row.isVisibleTo(fan)
        panel.timer_off.click()
        assert "computer triggers" in panel.said.text(), panel.said.text()
        panel.refresh()
        assert "PLC timer off" in panel.readout.text()
        panel.deleteLater()
        app.processEvents()
    finally:
        plugin.deactivate()
