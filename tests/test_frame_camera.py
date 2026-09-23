"""
Tests for frame_camera — the camera as a generated app's handlers see it.

These run entirely on the SIMULATED backend, which is always available, so the
whole module is exercised on a machine with no camera SDK installed (which is
every machine this has run on so far).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import frame_camera
from council_core import cameras


@pytest.fixture(autouse=True)
def clean():
    """Module-level state means every test must start from nothing."""
    frame_camera.disconnect()
    frame_camera._LIVE.found = None
    yield
    frame_camera.disconnect()
    frame_camera._LIVE.found = None


def connected(kind="frame"):
    listed = frame_camera.list_cameras()
    rows = [r for r in listed["rows"] if r.endswith(kind)]
    assert rows, f"no simulated {kind} camera in {listed['rows']}"
    return frame_camera.connect(rows[0])


def settle(seconds=0.2):
    time.sleep(seconds)


# ======================================================================
# Finding
# ======================================================================
def test_listing_returns_rows_and_a_summary():
    listed = frame_camera.list_cameras()
    assert listed["rows"], "the simulated backend should always appear"
    assert "found" in listed["summary"]


def test_a_missing_sdk_is_explained_in_the_notes():
    """An empty list with no reason sends the user to check cables."""
    listed = frame_camera.list_cameras()
    notes = listed["notes"]
    joined = " ".join(notes).lower()
    assert "pypylon" in joined or "basler" in joined
    assert "prophesee" in joined or "metavision" in joined


def test_the_notes_are_a_list_of_lines_not_one_string():
    """They go to a LISTBOX port, whose set() iterates what it is given.

    A string becomes one row per character — measured, the first time this
    ran against a real camera the panel showed 'p', 'r', 'o', ...
    """
    notes = frame_camera.list_cameras()["notes"]
    assert isinstance(notes, list)
    assert all(isinstance(n, str) and len(n) > 1 for n in notes), notes


def test_every_row_carries_its_position():
    listed = frame_camera.list_cameras()
    assert listed["rows"][0].startswith("1.")


# ======================================================================
# Choosing — the recurring defect in this codebase
# ======================================================================
def test_the_camera_is_chosen_by_position_not_by_its_description():
    """Two identical cameras produce identical descriptions.

    Matching on the descriptive part would open whichever came first. The
    position is what disambiguates, so that is what is resolved on.
    """
    frame_camera.list_cameras()
    found = frame_camera._LIVE.found
    same = cameras.CameraInfo("simulated", "sim-frame", "Twin", "", "sim",
                              "frame")
    other = cameras.CameraInfo("simulated", "sim-event", "Twin", "", "sim",
                               "event")
    found.cameras = [same, other]
    assert frame_camera._chosen("1. Twin · frame").key == "sim-frame"
    assert frame_camera._chosen("2. Twin · event").key == "sim-event"


def test_a_camera_can_also_be_chosen_by_its_key():
    frame_camera.list_cameras()
    assert frame_camera._chosen("sim-event").key == "sim-event"


def test_choosing_before_scanning_says_so():
    with pytest.raises(RuntimeError, match="scan"):
        frame_camera._chosen("1.")


def test_choosing_nothing_says_so():
    frame_camera.list_cameras()
    with pytest.raises(RuntimeError, match="choose a camera"):
        frame_camera._chosen("")


def test_choosing_a_position_that_is_not_there_says_so():
    frame_camera.list_cameras()
    with pytest.raises(RuntimeError, match="no camera 99"):
        frame_camera._chosen("99.")


# ======================================================================
# Connecting
# ======================================================================
def test_connecting_reports_the_sensor():
    out = connected()
    assert "640x480" in out["summary"]
    assert out["sensor"] == "640x480"


def test_an_event_camera_reports_no_exposure_and_no_gain():
    """So a panel can hide controls this sensor does not have."""
    out = connected("event")
    assert out["has_exposure"] is False
    assert out["has_gain"] is False
    assert out["kind"] == "event"


def test_a_frame_camera_reports_an_exposure_range():
    out = connected("frame")
    assert out["has_exposure"] is True


def test_connecting_twice_is_refused_rather_than_leaking_the_first():
    connected()
    with pytest.raises(RuntimeError, match="already open"):
        connected()


def test_working_without_a_camera_says_to_connect_first():
    for call in (lambda: frame_camera.set_area("0,0,10,10"),
                 lambda: frame_camera.set_exposure("100"),
                 lambda: frame_camera.set_gain("1"),
                 lambda: frame_camera.start("x")):
        with pytest.raises(RuntimeError, match="connect a camera first"):
            call()


# ======================================================================
# Capturing
# ======================================================================
def test_frames_are_written_into_the_folder_the_browser_shows(tmp_path):
    """The capture folder IS the browse folder — that is the whole design."""
    connected()
    frame_camera.start(str(tmp_path))
    settle()
    frame_camera.stop()
    written = sorted(tmp_path.glob("*.png"))
    assert written, "nothing was captured"


def test_captured_frames_are_readable_by_the_other_handlers(tmp_path):
    """A capture the rest of Barbie cannot open is not a capture."""
    import frame_classes
    import frame_timing

    connected()
    frame_camera.start(str(tmp_path))
    settle()
    frame_camera.stop()
    one = sorted(tmp_path.glob("*.png"))[0]
    assert frame_classes.thumbnail(one).shape == (32 * 32,)
    assert isinstance(frame_timing.count_bad_frames(tmp_path), int)


def test_a_second_run_never_overwrites_the_first(tmp_path):
    """This module cannot delete anything, and must not need to.

    RECONNECTING between the runs is the point. A device numbers frames from
    its own counter, so two runs on ONE open camera get 1..n then n+1..m and
    never collide whatever the stem is. Close the camera and reopen it — an
    app restarted, which is the normal case — and the counter starts at 1
    again. Only a per-run stem saves the first capture then.
    """
    connected()
    frame_camera.start(str(tmp_path))
    settle()
    frame_camera.stop()
    first = {p.name for p in tmp_path.glob("*.png")}
    assert first
    stamps = {p.name: p.stat().st_mtime_ns for p in tmp_path.glob("*.png")}

    frame_camera.disconnect()    # the frame counter restarts from here
    time.sleep(1.1)              # the run stamp has one-second resolution
    connected()
    frame_camera.start(str(tmp_path))
    settle()
    frame_camera.stop()
    after = {p.name for p in tmp_path.glob("*.png")}
    assert len(after) > len(first), "the second run wrote nothing"
    # NOT a content check. The simulated camera restarts its counter AND
    # regenerates identical pixels, so an overwritten frame_000001.png is
    # byte-identical to the one it replaced — a hash comparison passes while
    # the first run is being destroyed. The modification time is what
    # actually distinguishes "still there" from "written over".
    for name, when in stamps.items():
        assert (tmp_path / name).stat().st_mtime_ns == when, (
            f"{name} from the first run was written over")


def test_starting_without_a_folder_says_so():
    connected()
    with pytest.raises(RuntimeError, match="folder"):
        frame_camera.start("   ")


def test_starting_into_a_file_is_refused(tmp_path):
    connected()
    a_file = tmp_path / "not_a_folder.txt"
    a_file.write_text("x", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not a folder"):
        frame_camera.start(str(a_file))


def test_stopping_reports_the_measured_rate_and_the_drops(tmp_path):
    connected()
    frame_camera.start(str(tmp_path))
    settle()
    out = frame_camera.stop()
    assert "fps" in out["status"]
    assert "dropped" in out["status"]


def test_stopping_when_not_started_is_not_an_error():
    assert "Not capturing" in frame_camera.stop()["summary"]


def test_status_without_a_camera_says_so():
    assert "No camera" in frame_camera.status()["summary"]


# ======================================================================
# The live view
# ======================================================================
def test_latest_returns_nothing_before_a_camera_is_open():
    assert frame_camera.latest() is None


def test_pump_hands_the_newest_frame_to_the_display(tmp_path):
    connected()
    frame_camera.start(str(tmp_path))
    settle()
    shown, said = [], []
    for _ in range(20):
        frame_camera.pump(shown.append, said.append)
        time.sleep(0.01)
    frame_camera.stop()
    assert shown, "no frame ever reached the display"
    assert hasattr(shown[0], "shape"), "the display was not given an array"
    assert any("fps" in s for s in said)


def test_pump_is_quiet_when_no_frame_has_arrived():
    shown = []
    assert frame_camera.pump(shown.append) is False
    assert shown == []


def test_an_event_camera_reports_events_not_just_frames(tmp_path):
    """Calling an event stream "30 fps" is a lie about what it is."""
    connected("event")
    frame_camera.start(str(tmp_path))
    settle()
    said = []
    for _ in range(20):
        frame_camera.pump(lambda a: None, said.append)
        time.sleep(0.01)
    frame_camera.stop()
    assert any("events/window" in s for s in said), said[:3]


# ======================================================================
# The area of interest
# ======================================================================
def test_the_area_is_set_on_the_sensor():
    connected()
    frame_camera.set_area("0, 0, 320, 240")
    assert frame_camera._LIVE.device.roi().as_tuple() == (0, 0, 320, 240)


def test_an_unaligned_area_is_snapped_and_the_user_is_told():
    connected()
    out = frame_camera.set_area("101, 101, 333, 333")
    assert out["snapped"] is True
    assert "snapped" in out["summary"]
    assert frame_camera._LIVE.device.roi().w % 4 == 0


def test_an_aligned_area_is_not_reported_as_snapped():
    connected()
    out = frame_camera.set_area("100, 100, 320, 240")
    assert out["snapped"] is False
    assert "snapped" not in out["summary"]


def test_future_frames_are_the_size_of_the_area(tmp_path):
    """"Draw a box and future images are saved at that size" — the point."""
    from PIL import Image

    connected()
    frame_camera.set_area("0, 0, 320, 240")
    frame_camera.start(str(tmp_path))
    settle()
    frame_camera.stop()
    one = sorted(tmp_path.glob("*.png"))[0]
    assert Image.open(one).size == (320, 240)


def test_setting_an_area_while_running_leaves_it_running(tmp_path):
    connected()
    frame_camera.start(str(tmp_path))
    settle(0.1)
    frame_camera.set_area("0, 0, 128, 128")
    assert frame_camera._LIVE.session.running, "the capture was left stopped"
    frame_camera.stop()


def test_full_frame_gives_the_sensor_back():
    connected()
    frame_camera.set_area("0, 0, 64, 64")
    frame_camera.full_frame()
    assert frame_camera._LIVE.device.roi().as_tuple() == (0, 0, 640, 480)


def test_a_nonsense_area_is_refused_with_a_readable_message():
    connected()
    with pytest.raises(RuntimeError, match="x, y, w, h"):
        frame_camera.set_area("wide-ish")


def test_an_area_may_also_be_given_as_four_numbers():
    connected()
    frame_camera.set_area((0, 0, 128, 128))
    assert frame_camera._LIVE.device.roi().as_tuple() == (0, 0, 128, 128)


# ======================================================================
# Exposure and gain
# ======================================================================
def test_exposure_is_clamped_to_what_the_camera_accepts():
    connected()
    out = frame_camera.set_exposure("999999999")
    assert float(out["exposure"]) <= 100000.0


def test_a_non_numeric_exposure_is_refused_readably():
    connected()
    with pytest.raises(RuntimeError, match="must be a number"):
        frame_camera.set_exposure("bright")


def test_gain_is_reported_back():
    connected()
    assert float(frame_camera.set_gain("3")["gain"]) == 3.0


# ======================================================================
# Shutdown
# ======================================================================
def test_shutdown_joins_the_grab_thread(tmp_path):
    """A grab thread outliving its window crashes the app on exit."""
    connected()
    frame_camera.start(str(tmp_path))
    settle(0.05)
    session = frame_camera._LIVE.session
    frame_camera.shutdown()
    assert session.running is False
    assert frame_camera._LIVE.device is None


def test_shutdown_twice_is_harmless():
    connected()
    frame_camera.shutdown()
    frame_camera.shutdown()


# ======================================================================
# The handler contract
# ======================================================================
@pytest.mark.parametrize("call", [
    lambda: frame_camera.list_cameras(),
    lambda: frame_camera.status(),
    lambda: frame_camera.stop(),
    lambda: frame_camera.disconnect(),
])
def test_every_handler_function_returns_a_dict_with_a_summary(call):
    """The emitted handler writes result["summary"] straight to a port."""
    out = call()
    assert isinstance(out, dict)
    assert isinstance(out.get("summary"), str) and out["summary"]


# ======================================================================
# The point of the module: a generated app may actually import it
# ======================================================================
def test_a_generated_qt_app_with_camera_handlers_passes_the_gate(tmp_path):
    """The whole reason this module exists rather than importing council_core.

    council_core is refused by the gate in every spelling, so a camera reached
    that way never starts. This proves the shim is reachable from generated
    code, with the real emitter and the real gate.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    import gui_policy
    from tests.test_gui_emit_qt import emit

    spec, out = emit("barbie_capture_v3", tmp_path, "qt")
    handlers = out / "handlers.py"
    handlers.write_text(handlers.read_text(encoding="utf-8") + '''
    def on_btn_connect(self, *args) -> None:
        from frame_camera import connect, set_area, start, stop, pump
        result = connect(self.ports.cameras.get())
        self.ports.capture_status.set(result["summary"])
''', encoding="utf-8")

    ok, problems = gui_policy.validate_dir(out, "linked", spec.requires,
                                           toolkit="qt")
    assert ok, f"the gate refused a camera app: {problems}"


def test_council_core_would_have_been_refused():
    """The counterfactual, so the shim is never 'simplified away'."""
    import gui_policy

    assert gui_policy.is_council_module("council_core") is True
    assert "council_core" not in gui_policy.allowed_modules("linked", [])
    assert "frame_camera" in gui_policy.allowed_modules("linked", [])
