"""
The review-source contract (docs/typhon_plugins_design.md 2.4 item 13, 3.3,
3.7, 3.11, 9.1; stage P3 row 6): what every review source must do. A
review source is how the slider shows one of a camera plugin's run files —
the Prophesee plugin's raw view of `<run>_events.raw` — given by its
`review_source()` (camera plugins only). Run against every built-in camera
plugin that gives one, over a stand-in for the Metavision SDK shaped like
it, and — where the SDK is importable (the `pylon` env with OpenEB on the
path) — over a real .raw written with OpenEB's own writer.

  * it is a ReviewSource: a plain name and a short label for the slider's
    view button, and a suffix that is a run file its manifest declares, a
    run marker, and never a file the core writes;
  * it finds a run's file and the newest one, by run name, and tells which
    run a file is of;
  * `open(path, window_us)` gives a playback the slider polls — count,
    done, error, window_us, frame(k), close() — whose windows are pictures
    (uint8, (h, w)), in any order;
  * a file that is not there, or cannot be read here (no SDK), is said in
    `error`, never raised; opening writes nothing beside the file, and
    close() leaves nothing of the playback behind.

Offscreen, temporary folders only.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import typhon_plugin_api as api  # noqa: E402
from council_core import capture  # noqa: E402
from council_core import camera_rig  # noqa: E402
from council_core import plugin_host as ph  # noqa: E402
from council_core import plugin_lists as pl  # noqa: E402
from council_core import plugin_manifest as pm  # noqa: E402
from tests import fake_rig  # noqa: E402
from tests.test_event_playback import FakeHal, batch  # noqa: E402

BUILTIN = ROOT / "typhon_plugins"
#: The run files the core itself writes: never a review source's.
CORE_FILES = ("_frame_", "_frames.csv", "_camera.json", "_run_end.json",
              "_rig.json", "_rig_end.json")


def sources():
    """(plugin id, folder) of every built-in plugin whose manifest says it
    has a review source."""
    return [(pid, folder) for pid, folder in ph.BUILTIN_PLUGINS
            if "review_source" in pm.read(BUILTIN / folder,
                                          builtin=True).contributes]


SOURCES = sources()
IDS = [pid for pid, _ in SOURCES]


class Template:
    def __init__(self):
        self.device = None
        self.info = None

    def clear(self):
        self.device = None
        self.info = None


@pytest.fixture
def host(tmp_path, monkeypatch):
    where = tmp_path / "plugin_home"
    where.mkdir()
    monkeypatch.setenv(pl.HOME_ENV, str(where))
    made = ph.PluginHost(camera_rig.Rig(Template))
    yield made
    made.close()


def source_of(host, pid):
    plugin = host.instance(pid)
    assert plugin is not None, host.state(pid)
    return plugin.review_source()


def window_batches(n=6, window_us=20_000):
    """Events in `n` windows: window k has one event at (k, k)."""
    return [batch((k, k, 1, k * window_us + 5)) for k in range(n)]


def test_the_sources_held_to_the_contract_are_every_built_in_one():
    assert IDS == ["typhon.prophesee"]


@pytest.mark.parametrize("pid, folder", SOURCES, ids=IDS)
def test_it_is_a_review_source_its_manifest_declares(host, pid, folder):
    source = source_of(host, pid)
    m = pm.read(BUILTIN / folder, builtin=True)
    assert isinstance(source, api.ReviewSource)
    assert m.kind == "camera", "camera plugins only"
    assert source.name.isidentifier() and source.name.islower()
    assert source.label and len(source.label) <= 12
    assert source.suffix.startswith("_") and source.suffix in m.files
    assert not any(core in source.suffix for core in CORE_FILES)
    host.register_run_files()
    assert source.suffix in capture.run_markers(), "the run counts as taken"
    assert callable(source.open)


@pytest.mark.parametrize("pid, folder", SOURCES, ids=IDS)
def test_it_finds_a_runs_file_and_the_newest(host, tmp_path, pid, folder):
    """The newest by run name, in the slider's order (capture_review.
    run_order_key, qt-migration c768e59): a later second; in one second
    "<run>" before "<run>_2" before "<run>_10" — the reruns frame_camera
    gives two runs started in one second."""
    from council_qt.widgets import capture_review as slider

    source = source_of(host, pid)
    runs = tmp_path / "runs"
    runs.mkdir()
    for run in ("20261018_085959", "20261018_090000", "20261018_090000_2",
                "20261018_090000_10"):
        (runs / f"{run}{source.suffix}").write_bytes(b"")
    (runs / "20261018_090000_frames.csv").write_text("file\n")
    assert source.run_of(runs / f"20261018_090000_2{source.suffix}") == \
        "20261018_090000_2"
    assert source.run_of(runs / "20261018_090000_frames.csv") == ""
    assert source.run_of(f"X{source.suffix.upper()}") == "X", \
        "a suffix in another case is the same file on Windows"
    assert source.file_for(runs, "20261018_090000_2") == \
        runs / f"20261018_090000_2{source.suffix}"
    assert source.file_for(runs, "nothing") is None
    assert source.latest(runs) == runs / f"20261018_090000_10{source.suffix}"
    if source.suffix == slider.RAW_SUFFIX:
        assert source.latest(runs) == slider.latest_raw(runs)
    assert source.latest(tmp_path / "no such folder") is None


@pytest.mark.parametrize("pid, folder", SOURCES, ids=IDS)
def test_the_second_run_of_a_second_is_the_newest(host, tmp_path, pid,
                                                  folder):
    """Two runs started in one second are "<stamp>" and "<stamp>_2"
    (frame_camera.start): the second is the newer. Whole names sorted put
    "<stamp>_2<suffix>" first (an underscore sorts before a letter), so the
    raw view opened the older recording; the slider was fixed on
    qt-migration (c768e59) and a review source keeps its order."""
    source = source_of(host, pid)
    for run in ("20261018_090000_2", "20261018_090000"):
        (tmp_path / f"{run}{source.suffix}").write_bytes(b"")
    assert source.latest(tmp_path) == \
        tmp_path / f"20261018_090000_2{source.suffix}"


def opened(source, path, window_us=20_000, **extra):
    playback = source.open(path, window_us, **extra)
    for attr in ("count", "done", "error", "window_us", "frame", "close"):
        assert hasattr(playback, attr), attr
    return playback


@pytest.mark.parametrize("pid, folder", SOURCES, ids=IDS)
def test_it_opens_a_run_file_as_windows_the_slider_polls(host, tmp_path,
                                                        pid, folder):
    source = source_of(host, pid)
    runs = tmp_path / "runs"
    runs.mkdir()
    path = runs / f"run{source.suffix}"
    path.write_bytes(b"% end\n")
    before = sorted(p.name for p in runs.iterdir())
    temp_before = set(os.listdir(tempfile.gettempdir()))
    playback = opened(source, path, hal=FakeHal(window_batches()))
    try:
        assert playback.wait_done(10)
        assert playback.error == "" and playback.done
        assert playback.window_us == 20_000
        assert playback.count == 6
        for k in (5, 0, 3):
            picture = playback.frame(k)
            assert isinstance(picture, np.ndarray)
            assert picture.dtype == np.uint8 and picture.ndim == 2
            assert picture[k, k] == 255, "window k holds its own event"
    finally:
        playback.close()
    assert sorted(p.name for p in runs.iterdir()) == before, \
        "nothing written beside the file"
    left = set(os.listdir(tempfile.gettempdir())) - temp_before
    assert not [n for n in left if n.startswith("event_playback_")], \
        "close() leaves nothing of the playback behind"


@pytest.mark.parametrize("pid, folder", SOURCES, ids=IDS)
def test_a_file_that_is_not_there_is_said_never_raised(host, tmp_path, pid,
                                                       folder):
    source = source_of(host, pid)
    playback = opened(source, tmp_path / f"gone{source.suffix}",
                      hal=FakeHal([]))
    try:
        assert playback.wait_done(10)
        assert "does not exist" in playback.error
        assert playback.count == 0
    finally:
        playback.close()


@pytest.mark.parametrize("pid, folder", SOURCES, ids=IDS)
def test_without_the_sdk_opening_says_what_is_missing(host, tmp_path,
                                                      monkeypatch, pid,
                                                      folder):
    """Said AT ONCE, as a RuntimeError naming what to install (the raw
    view's own rule: no playback handed back that will only ever fail),
    or in the playback's `error` — never a crash later."""
    source = source_of(host, pid)
    monkeypatch.setitem(sys.modules, "metavision_hal", None)
    path = tmp_path / f"run{source.suffix}"
    path.write_bytes(b"% end\n")
    try:
        playback = source.open(path, 20_000)
    except RuntimeError as exc:
        assert "Metavision SDK" in str(exc)
        return
    try:
        assert playback.wait_done(10)
        assert "Metavision SDK" in playback.error
    finally:
        playback.close()


def _real_sdk_skip():
    return "" if fake_rig.metavision() is not None else \
        "the Metavision SDK is importable only in the pylon env with OpenEB"


@pytest.mark.skipif(bool(_real_sdk_skip()), reason=_real_sdk_skip() or "-")
@pytest.mark.parametrize("pid, folder", SOURCES, ids=IDS)
def test_a_real_raw_file_opens_through_it(host, tmp_path, pid, folder):
    source = source_of(host, pid)
    events = fake_rig.random_events(2_000, 0, 100_000, seed=3)
    path = fake_rig.write_raw(tmp_path / f"run{source.suffix}", events)
    before = sorted(p.name for p in tmp_path.iterdir())
    playback = opened(source, path)
    try:
        assert playback.wait_done(30)
        assert playback.error == "", playback.error
        assert playback.count >= 4
        picture = playback.frame(0)
        assert picture.dtype == np.uint8 and picture.ndim == 2
    finally:
        playback.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == before, \
        "no index written beside the file (build_index off)"
