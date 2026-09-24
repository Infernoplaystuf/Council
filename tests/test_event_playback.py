"""
council_core.event_playback — a .raw as windows a slider can jump around in.

Most of this runs against a fake HAL shaped like the real one. The last
section runs against the REAL Metavision SDK when it can be imported (on this
machine: the pylon env with the OpenEB build on PATH), and skips otherwise.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from council_core import cameras, event_playback as ep

EVENT_DTYPE = np.dtype([("x", "<u2"), ("y", "<u2"), ("p", "<i2"), ("t", "<i8")])
W, H = 64, 48


def batch(*quads):
    """(x, y, p, t) tuples as one CD buffer."""
    out = np.zeros(len(quads), dtype=EVENT_DTYPE)
    for i, quad in enumerate(quads):
        out[i] = quad
    return out


# ======================================================================
# A fake HAL, shaped like metavision_hal
# ======================================================================
class FakeConfig:
    def __init__(self):
        # The real defaults: build_index=True writes <name>.raw.tmp_index.
        self.build_index = True
        self.do_time_shifting = True


class FakeGeometry:
    def get_width(self):
        return W

    def get_height(self):
        return H


class FakeCd:
    def __init__(self):
        self.callbacks = []

    def add_event_buffer_callback(self, fn):
        self.callbacks.append(fn)


class FakeStream:
    def __init__(self, batches):
        self.batches = list(batches)
        self.stopped = False

    def start(self):
        pass

    def stop(self):
        self.stopped = True

    def poll_buffer(self):
        return 1 if self.batches else -1

    def get_latest_raw_data(self):
        return self.batches.pop(0)


class FakeDecoder:
    def __init__(self, cd):
        self.cd = cd

    def decode(self, data):
        for fn in self.cd.callbacks:
            fn(data)


class FakeDevice:
    def __init__(self, batches):
        self.cd = FakeCd()
        self.stream = FakeStream(batches)
        self.decoder = FakeDecoder(self.cd)

    def get_i_geometry(self):
        return FakeGeometry()

    def get_i_events_stream(self):
        return self.stream

    def get_i_events_stream_decoder(self):
        return self.decoder

    def get_i_event_cd_decoder(self):
        return self.cd


class FakeHal:
    RawFileConfig = FakeConfig

    def __init__(self, batches):
        hal = self
        self.opened = []

        class DeviceDiscovery:
            @staticmethod
            def open_raw_file(path, config):
                device = FakeDevice(batches)
                hal.opened.append((path, config, device))
                return device

        self.DeviceDiscovery = DeviceDiscovery


def play(tmp_path, *batches, window_us=20_000):
    raw = tmp_path / "run_events.raw"
    raw.write_bytes(b"% end\n")
    hal = FakeHal(batches)
    playback = ep.RawPlayback(raw, window_us=window_us, hal=hal)
    assert playback.wait_done(10)
    return playback, hal


# ======================================================================
# Windows
# ======================================================================
def test_windows_are_counted_from_the_first_event(tmp_path):
    """Not from zero: a time-shifted EVT3 file starts several ms in, and the
    PNGs' raw times are measured from the first event."""
    pb, _ = play(tmp_path, batch((1, 1, 1, 45_000), (2, 2, 0, 64_999),
                                 (3, 3, 1, 65_000), (4, 4, 1, 125_000)))
    assert pb.error == ""
    assert pb.count == 5                      # 45 ms..125 ms is windows 0..4
    assert pb.events == 4
    first = pb.frame(0)
    assert first[1, 1] == 255 and first[2, 2] == 0 and first[3, 3] == 128
    pb.close()


def test_a_quiet_stretch_is_empty_windows_not_missing_ones(tmp_path):
    """Window k must BE time k*W, or the slider's position is a lie about
    when; so a gap in the events is a run of empty windows."""
    pb, _ = play(tmp_path, batch((1, 1, 1, 0), (2, 2, 1, 100_000)))
    assert pb.count == 6
    for k in (1, 2, 3, 4):
        assert (pb.frame(k) == cameras.EVENT_MID).all()
    pb.close()


def test_a_window_split_across_buffers_is_joined(tmp_path):
    """USB buffers do not respect window edges."""
    pb, _ = play(tmp_path, batch((1, 1, 1, 0), (2, 2, 1, 5000)),
                 batch((3, 3, 0, 9000), (4, 4, 1, 30000)))
    first = pb.frame(0)
    assert first[1, 1] == 255 and first[2, 2] == 255 and first[3, 3] == 0
    assert pb.frame(1)[4, 4] == 255
    pb.close()


def test_a_window_looks_exactly_as_the_live_view_drew_it(tmp_path):
    rng = np.random.default_rng(3)
    n = 500
    quads = [(int(rng.integers(0, W)), int(rng.integers(0, H)),
              int(rng.integers(0, 2)), int(t))
             for t in np.sort(rng.integers(0, 60_000, n))]
    pb, _ = play(tmp_path, batch(*quads))
    events = batch(*quads)
    for k in range(pb.count):
        inside = events[(events["t"] - events["t"][0]) // 20_000 == k]
        want = cameras.accumulate_events(inside["x"], inside["y"], inside["p"],
                                         W, H, np)
        assert np.array_equal(pb.frame(k), want), k
    pb.close()


def test_windows_can_be_read_in_any_order(tmp_path):
    pb, _ = play(tmp_path, *[batch((k % W, 0, 1, k * 20_000)) for k in range(30)])
    for k in (29, 0, 17, 3, 29, 1):
        assert pb.frame(k)[0, k % W] == 255
    pb.close()


# ======================================================================
# What it must NOT do
# ======================================================================
def test_the_file_is_opened_without_the_index_and_time_shifted(tmp_path):
    """A default open writes "<name>.raw.tmp_index" into the capture folder,
    and Python cannot use that index anyway."""
    pb, hal = play(tmp_path, batch((1, 1, 1, 0)))
    config = hal.opened[0][1]
    assert config.build_index is False
    assert config.do_time_shifting is True
    pb.close()


def test_nothing_is_written_beside_the_recording(tmp_path):
    pb, _ = play(tmp_path, batch((1, 1, 1, 0), (2, 2, 1, 50_000)))
    pb.frame(1)
    assert os.listdir(tmp_path) == ["run_events.raw"]
    pb.close()


def test_close_removes_the_temporary_data(tmp_path):
    pb, _ = play(tmp_path, batch((1, 1, 1, 0)))
    pb.frame(0)
    tmp = pb._tmp
    assert os.path.isdir(tmp)
    pb.close()
    assert not os.path.exists(tmp)


def test_the_stream_is_stopped_when_the_pass_ends(tmp_path):
    pb, hal = play(tmp_path, batch((1, 1, 1, 0)))
    assert hal.opened[0][2].stream.stopped
    pb.close()


def test_the_decoder_callback_does_not_hold_the_device(tmp_path):
    """The cycle that kept a .raw locked on Windows until the process ended."""
    pb, hal = play(tmp_path, batch((1, 1, 1, 0)))
    device = hal.opened[0][2]
    for fn in device.cd.callbacks:
        assert getattr(fn, "__self__", None) is not device
        assert getattr(fn, "__self__", None) is not pb
    pb.close()


def test_a_missing_file_is_an_error_not_a_hang(tmp_path):
    pb = ep.RawPlayback(tmp_path / "gone.raw", hal=FakeHal([]))
    assert pb.wait_done(5)
    assert "does not exist" in pb.error and pb.count == 0
    pb.close()


def test_no_sdk_is_said_plainly(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "metavision_hal", None)
    with pytest.raises(ep.RawUnavailable, match="Metavision SDK"):
        ep.open_raw(tmp_path / "x.raw")


# ======================================================================
# Anchored at the capture's own origin
# ======================================================================
def play_from(tmp_path, origin, *batches):
    raw = tmp_path / "run_events.raw"
    raw.write_bytes(b"% end\n")
    hal = FakeHal(batches)
    playback = ep.RawPlayback(raw, window_us=20_000, hal=hal, origin_us=origin)
    assert playback.wait_done(10)
    return playback, hal


def test_given_the_origin_the_file_is_read_on_the_camera_clock(tmp_path):
    pb, hal = play_from(tmp_path, 1_000, batch((1, 1, 1, 50_000)))
    assert hal.opened[0][1].do_time_shifting is False
    assert hal.opened[0][1].build_index is False
    pb.close()


def test_windows_start_at_the_origin_not_at_the_files_first_event(tmp_path):
    """A .raw opened inside a running stream loses up to 4.1 ms at its head;
    anchored at its own first event, every window would be that far off."""
    pb, _ = play_from(tmp_path, 10_000,
                      batch((1, 1, 1, 13_000), (2, 2, 1, 29_999), (3, 3, 1, 30_000)))
    assert pb.count == 2
    assert pb.frame(0)[1, 1] == 255 and pb.frame(0)[2, 2] == 255
    assert pb.frame(1)[3, 3] == 255 and pb.frame(1)[2, 2] == 128
    pb.close()


def test_the_evt3_wrap_between_file_and_camera_clocks_is_put_back(tmp_path):
    """Measured: an EVT3 file decodes k x 16,777,216 us behind the live
    view. The origin (camera clock) tells how many wraps to add back."""
    wrap = ep.TIME_WRAP_US
    origin = 3 * wrap + 5_000
    pb, _ = play_from(tmp_path, origin,
                      batch((1, 1, 1, 5_000 + 100), (2, 2, 1, 5_000 + 25_000)))
    assert pb.count == 2
    assert pb.frame(0)[1, 1] == 255 and pb.frame(1)[2, 2] == 255
    pb.close()


def test_the_origin_is_read_from_the_frames_csv(tmp_path):
    index = tmp_path / "run_frames.csv"
    index.write_text("file,index,timestamp_us,raw_t_us,events\n"
                     "a.png,1,1500000,20000,5\n", encoding="utf-8")
    assert ep.raw_origin(index) == 1_480_000


def test_a_csv_without_raw_times_gives_no_origin(tmp_path):
    index = tmp_path / "run_frames.csv"
    index.write_text("file,index,timestamp_us,raw_t_us,events\n"
                     "a.png,1,0,,\n", encoding="utf-8")
    assert ep.raw_origin(index) is None
    assert ep.raw_origin(tmp_path / "missing.csv") is None


# ======================================================================
# The frames CSV: where each PNG falls in the .raw
# ======================================================================
def test_frame_times_reads_the_csv_sorted_and_skips_rows_without_a_time(tmp_path):
    csv_path = tmp_path / "run_frames.csv"
    csv_path.write_text(
        "file,index,timestamp_us,raw_t_us,events\n"
        "b.png,2,0,40000,5\n"
        "a.png,1,0,20000,5\n"
        "basler.png,3,0,,\n", encoding="utf-8")
    assert ep.frame_times(csv_path) == [(20000, "a.png"), (40000, "b.png")]


def test_frame_times_of_a_missing_csv_is_empty(tmp_path):
    assert ep.frame_times(tmp_path / "nope.csv") == []


def test_a_png_maps_to_the_raw_window_at_its_middle():
    """A PNG's time is its LAST event; its window ended there."""
    assert ep.window_for(39_999, 20_000) == 1
    assert ep.window_for(40_000, 20_000) == 1
    assert ep.window_for(50_001, 20_000) == 2
    assert ep.window_for(5_000, 20_000) == 0


def test_the_nearest_png_to_a_time():
    times = [(20000, "a.png"), (40000, "b.png"), (80000, "c.png")]
    assert ep.nearest_frame(times, 41000) == "b.png"
    assert ep.nearest_frame(times, 65000) == "c.png"
    assert ep.nearest_frame(times, 0) == "a.png"
    assert ep.nearest_frame([], 5) == ""


# ======================================================================
# Against the real SDK, when it is here
# ======================================================================
def _real_sdk():
    try:
        import metavision_hal
        import metavision_sdk_stream
    except Exception:                                       # noqa: BLE001
        pytest.skip("the Metavision SDK is not importable here")
    return metavision_hal, metavision_sdk_stream


def test_a_real_recording_plays_back_exactly(tmp_path):
    hal, stream_mod = _real_sdk()
    rng = np.random.default_rng(5)
    n = 20_000
    t = np.sort(rng.integers(3_000_000, 4_000_000, n)).astype(np.int64)
    events = np.zeros(n, EVENT_DTYPE)
    events["t"] = t
    events["x"] = rng.integers(0, 1280, n)
    events["y"] = rng.integers(0, 720, n)
    events["p"] = rng.integers(0, 2, n)
    path = tmp_path / "rec_events.raw"
    writer = stream_mod.RAWEvt2EventFileWriter(1280, 720, str(path))
    writer.add_cd_events(events)
    writer.flush()
    writer.close()
    del writer

    with ep.open_raw(path, window_us=20_000) as pb:
        assert pb.wait_done(30) and pb.error == ""
        assert pb.events == n
        assert pb.count == int(t[-1] - t[0]) // 20_000 + 1
        rel = t - t[0]
        for k in (0, 7, pb.count - 1):
            inside = events[rel // 20_000 == k]
            want = cameras.accumulate_events(inside["x"], inside["y"],
                                             inside["p"], 1280, 720, np)
            assert np.array_equal(pb.frame(k), want), k
    assert sorted(os.listdir(tmp_path)) == ["rec_events.raw"], \
        "something was written beside the recording"
    os.rename(path, tmp_path / "moved.raw")               # not locked



def test_the_runs_window_is_read_from_its_csv(tmp_path):
    index = tmp_path / "run_frames.csv"
    index.write_text("file,index,timestamp_us,raw_t_us,events,window_us\n"
                     "a.png,1,1500000,20000,5,5000\n", encoding="utf-8")
    assert ep.run_window_us(index) == 5000
    index.write_text("file,index,timestamp_us,raw_t_us,events\n"
                     "a.png,1,1500000,20000,5\n", encoding="utf-8")
    assert ep.run_window_us(index) is None
