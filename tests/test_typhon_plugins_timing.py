"""
tools/typhon_plugins_timing.py, the bench behind P1 row 3 (plugin design
10.2): it runs inside the real Qt "Typhon (plugins)" app, offscreen, and
gives numbers. Checked here for working, never for the numbers — those
depend on the PC and are recorded in docs/typhon_plugins_branch.md.

And what P3 row 5 built from those numbers: Python's switch interval at
1 ms while a run of a "Typhon (plugins)" project records (council_core.
plugin_host.SwitchInterval), put back when the run is over; today's Typhon
leaves it as it is. Windows' 1 ms timer is not asked for: inside the real
Qt Typhon the window's own timers already give the process one (measured,
docs/typhon_plugins_branch.md "Frame timing").
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy  # noqa: F401  (loaded here, not first by a capture thread
#                             while pytest.approx asks it: a half-made
#                             module raised "no attribute isscalar")
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import typhon_plugins_timing as timing  # noqa: E402


def test_one_run_measures_a_capture_in_the_built_app(tmp_path):
    """One short capture through the app's own Start and Stop, with a plugin
    on the rig's host fed every live frame on its own thread."""
    import run_example_gui as rex

    pdir = rex.build("typhon_plugins", project="timing",
                     vault_dir=tmp_path / "vault", target="qt")
    spec = {"timer": "default", "plugin": "noop", "seconds": 1.0,
            "fps": 60.0, "project": str(pdir),
            "folder": str(tmp_path / "runs")}
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
               COUNCIL_NO_DIALOGS="1")
    out = subprocess.run([sys.executable, str(ROOT / "tools" /
                                              "typhon_plugins_timing.py"),
                          "--child", json.dumps(spec)],
                         capture_output=True, text=True, timeout=300,
                         env=env, cwd=str(ROOT), encoding="utf-8")
    assert out.returncode == 0, out.stderr[-2000:]
    said = json.loads([line for line in out.stdout.splitlines()
                       if line.startswith("{")][-1])
    assert said["timer"] == "default" and said["plugin"] == "noop"
    assert 30 <= said["frames"] <= 90, said        # about 60 in one second
    assert said["saved"] == said["grabbed"] >= said["frames"]
    assert said["feed"]["delivered"] > 0 and said["faults"] == []
    for key in ("late", "late_2", "median_ms", "p99_ms", "max_ms",
                "read_late", "event_wait_1ms", "switch_interval_ms",
                "cpu_busy_pct", "logical_cpus", "event_wait_1ms_bare",
                "switch_interval_ms_recording"):
        assert key in said, key
    # "default" is today's settings: the plugin Typhon's own finer switch
    # interval while recording (P3 row 5) is held off for it.
    assert said["switch_interval_ms_recording"] == pytest.approx(5.0)
    if os.name == "nt":
        # The PC's load is part of every result (P1 review: the same 5 ms
        # plugin made 0 or 512-544 frames late on the same laptop).
        assert 0.0 <= said["cpu_busy_pct"] <= 100.0
    assert "PC busy" in timing.row(said)
    assert len(list((tmp_path / "runs").glob("*_frame_*.png"))) == \
        said["saved"]
    assert timing.row(said).split()[:2] == ["default", "noop"]


def test_the_typhon_row_is_what_the_plugin_typhon_sets_by_itself(tmp_path):
    """"typhon": the bench sets nothing; the plugin Typhon's own hold (P3
    row 5) puts the switch interval at 1 ms while it records, and leaves
    the live view before it at Python's 5 ms."""
    import run_example_gui as rex

    pdir = rex.build("typhon_plugins", project="timing",
                     vault_dir=tmp_path / "vault", target="qt")
    spec = {"timer": "typhon", "plugin": "none", "seconds": 0.5,
            "fps": 60.0, "project": str(pdir),
            "folder": str(tmp_path / "runs")}
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
               COUNCIL_NO_DIALOGS="1")
    out = subprocess.run([sys.executable, str(ROOT / "tools" /
                                              "typhon_plugins_timing.py"),
                          "--child", json.dumps(spec)],
                         capture_output=True, text=True, timeout=300,
                         env=env, cwd=str(ROOT), encoding="utf-8")
    assert out.returncode == 0, out.stderr[-2000:]
    said = json.loads([line for line in out.stdout.splitlines()
                       if line.startswith("{")][-1])
    assert said["timer"] == "typhon"
    assert said["switch_interval_ms"] == pytest.approx(5.0), "live view"
    assert said["switch_interval_ms_recording"] == pytest.approx(1.0)
    assert said["saved"] == said["grabbed"] > 0


def test_the_matrix_names_only_what_it_knows():
    assert set(timing.TIMERS) == {"default", "1ms", "switch", "period",
                                  "typhon"}
    assert timing.PLUGINS["slow20cap"] == (20.0, 0.25)
    assert timing._percentile([1, 2, 3, 4, 5], 0.99) == 5
    assert timing._percentile([], 0.5) == 0.0


def test_the_pcs_load_is_read_from_its_cpu_times():
    # (idle, kernel incl. idle, user): 30 idle of 100 = 70 % busy.
    assert timing.cpu_busy_pct((0, 0, 0), (30, 60, 40)) == 70.0
    assert timing.cpu_busy_pct(None, (1, 2, 3)) is None
    assert timing.cpu_busy_pct((5, 5, 5), (5, 5, 5)) is None
    load = timing.Load()
    load.begin()
    # Busy for 0.2 s, not for a fixed amount of work: Windows moves its
    # CPU times on at its clock tick (about 15.6 ms), and a few ms of work
    # inside one tick read the same times twice, which is "cannot tell"
    # (None) — it failed that way once in the gate.
    deadline = time.perf_counter() + 0.2
    while time.perf_counter() < deadline:
        sum(range(1000))
    said = load.end()
    assert said["logical_cpus"] == os.cpu_count()
    if os.name == "nt":
        assert 0.0 <= said["cpu_busy_pct"] <= 100.0


# ======================================================================
# P3 row 5: Python's switch interval at 1 ms while a "Typhon (plugins)"
# run records (design 10.2), put back after it
# ======================================================================
@pytest.fixture
def interval():
    """The process's switch interval, put back after the test whatever it
    did."""
    before = sys.getswitchinterval()
    yield before
    sys.setswitchinterval(before)


def test_the_finer_switch_interval_is_held_while_any_run_holds_it(
        interval, monkeypatch):
    from council_core import plugin_host as ph

    monkeypatch.setattr(ph, "RECORDING_SWITCH_S", 0.001)
    held = ph.SwitchInterval()
    one, two = object(), object()
    held.hold(one)
    assert sys.getswitchinterval() == pytest.approx(0.001)
    held.hold(two)
    held.hold(one)                       # the same run again: one hold
    held.release(one)
    assert sys.getswitchinterval() == pytest.approx(0.001), \
        "the other run still records"
    held.release(two)
    assert sys.getswitchinterval() == pytest.approx(interval), "put back"
    held.release(two)                    # again: nothing happens
    assert sys.getswitchinterval() == pytest.approx(interval)
    assert not held.held


def test_a_change_made_meanwhile_by_someone_else_is_left_alone(
        interval, monkeypatch):
    from council_core import plugin_host as ph

    monkeypatch.setattr(ph, "RECORDING_SWITCH_S", 0.001)
    held = ph.SwitchInterval()
    run = object()
    held.hold(run)
    sys.setswitchinterval(0.002)          # someone else's choice
    held.release(run)
    assert sys.getswitchinterval() == pytest.approx(0.002)


def test_a_finer_interval_already_set_or_none_asked_is_left_as_it_is(
        interval, monkeypatch):
    from council_core import plugin_host as ph

    sys.setswitchinterval(0.0005)
    held = ph.SwitchInterval()
    monkeypatch.setattr(ph, "RECORDING_SWITCH_S", 0.001)
    held.hold("run")
    assert sys.getswitchinterval() == pytest.approx(0.0005), "never coarser"
    held.release("run")
    assert sys.getswitchinterval() == pytest.approx(0.0005)
    sys.setswitchinterval(0.005)
    monkeypatch.setattr(ph, "RECORDING_SWITCH_S", None)
    held.hold("run")
    assert sys.getswitchinterval() == pytest.approx(0.005), "None: as is"
    held.release("run")
    assert sys.getswitchinterval() == pytest.approx(0.005)


def _sim(monkeypatch):
    from tests.sim_cameras import without_camera_sdks
    without_camera_sdks(monkeypatch)


def test_a_typhon_plugins_run_records_at_the_finer_interval(
        tmp_path, interval, monkeypatch):
    """Through Typhon's own Start and Stop (the fake host): the live view
    before and after at the interval as it was, the recording at 1 ms."""
    from council_core import plugin_host as ph
    from typhon_plugin_api.fake_host import FakeHost

    _sim(monkeypatch)
    seen = []
    with FakeHost(tmp_path) as fake:
        fake.connect()
        assert sys.getswitchinterval() == pytest.approx(interval)
        run = fake.capture(frames=3, during=lambda: seen.append(
            sys.getswitchinterval()))
        assert sys.getswitchinterval() == pytest.approx(interval), \
            "put back at Stop"
        assert run.end["frames_saved"] >= 3
    assert seen == [pytest.approx(ph.RECORDING_SWITCH_S)]
    assert ph.RECORDING_SWITCH_S == pytest.approx(0.001)


def test_a_disconnect_mid_run_puts_the_interval_back(tmp_path, interval,
                                                     monkeypatch):
    from typhon_plugin_api.fake_host import FakeHost, wait_for

    _sim(monkeypatch)
    with FakeHost(tmp_path) as fake:
        fake.connect()
        fake.start()
        assert wait_for(lambda: fake.session.stats().recorded >= 1)
        assert sys.getswitchinterval() < interval
        fake._fc.disconnect()
        assert sys.getswitchinterval() == pytest.approx(interval)


def test_a_start_that_fails_never_holds_it(tmp_path, interval, monkeypatch):
    import frame_camera
    from typhon_plugin_api.fake_host import FakeHost

    _sim(monkeypatch)

    def no(*a, **k):
        raise OSError("no room")
    with FakeHost(tmp_path) as fake:
        fake.connect()
        monkeypatch.setattr(frame_camera, "_downsample_record", no)
        with pytest.raises(RuntimeError, match="no room"):
            fake.start()
        assert sys.getswitchinterval() == pytest.approx(interval)


def test_a_recording_that_ends_by_itself_puts_it_back_with_its_end_record(
        tmp_path, interval, monkeypatch):
    """A failed write ends the recording before Stop: the run is not
    recording any more, so the interval goes back as its end record is
    written, not at the Stop that may come much later."""
    from council_core import capture
    from typhon_plugin_api.fake_host import FakeHost, wait_for

    _sim(monkeypatch)
    real = capture.Recorder.save

    def save(self, frame, path):
        if frame.index >= 3:
            raise OSError(28, "No space left on device")
        real(self, frame, path)
    monkeypatch.setattr(capture.Recorder, "save", save)
    with FakeHost(tmp_path) as fake:
        fake.connect()
        started = fake.start()
        end = fake.runs / f"{started['run']}_run_end.json"
        assert wait_for(end.is_file), "the recording ended by itself"
        assert wait_for(lambda: sys.getswitchinterval() ==
                        pytest.approx(interval), 5)
        fake.stop()
        assert sys.getswitchinterval() == pytest.approx(interval)


def test_todays_typhon_leaves_the_switch_interval_as_it_is(
        tmp_path, interval, monkeypatch):
    import frame_camera
    from council_core import camera_setup as cs
    from tests.sim_cameras import simulated_row
    from typhon_plugin_api.fake_host import wait_for

    _sim(monkeypatch)
    folder = tmp_path / "classic"
    folder.mkdir()
    frame_camera.disconnect()
    rig = frame_camera._RIG
    kept = (rig._plugins, rig.setup_path, rig.found)
    rig._plugins = None
    frame_camera._LIVE.setup_path = cs.setup_path(folder)
    frame_camera._LIVE.found = None
    seen = []
    try:
        rows = frame_camera.list_cameras()["rows"]
        frame_camera.connect(simulated_row(rows, "frame"))
        frame_camera.start(str(tmp_path / "runs"))
        try:
            assert wait_for(lambda: frame_camera._LIVE.session.stats()
                            .recorded >= 2)
            seen.append(sys.getswitchinterval())
        finally:
            frame_camera.stop()
        assert rig._plugins is None, "today's Typhon makes no host"
    finally:
        frame_camera.disconnect()
        rig._plugins, rig.setup_path, rig.found = kept
    assert seen == [pytest.approx(interval)]
