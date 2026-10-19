"""
Two runs started in the same second are <stamp> and <stamp>_2
(frame_camera.start). The review slider must keep them in that order: the
first run's frames before the second's, and the newest raw recording is the
second run's. Sorting whole file names put "_2_..." before "_events" and
"_frame_" (an underscore sorts before a letter), so the second run came
first and the raw view opened the older recording.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_qt.widgets import capture_review as cr  # noqa: E402


def _touch(folder, *names):
    for name in names:
        (folder / name).write_bytes(b"")


def test_the_newest_raw_is_the_second_run_of_the_same_second(tmp_path):
    _touch(tmp_path, "20261018_120000_events.raw",
           "20261018_120000_2_events.raw")
    assert cr.latest_raw(tmp_path).name == "20261018_120000_2_events.raw"


def test_the_tenth_run_of_a_second_is_newer_than_the_ninth(tmp_path):
    _touch(tmp_path, "20261018_120000_9_events.raw",
           "20261018_120000_10_events.raw", "20261018_120000_events.raw")
    assert cr.latest_raw(tmp_path).name == "20261018_120000_10_events.raw"


def test_a_later_second_is_still_newer_than_a_rerun(tmp_path):
    _touch(tmp_path, "20261018_120000_2_events.raw",
           "20261018_120001_events.raw")
    assert cr.latest_raw(tmp_path).name == "20261018_120001_events.raw"


def test_the_frames_of_two_runs_in_one_second_keep_their_run_order(tmp_path):
    names = ["20261018_120000_frame_000001.png",
             "20261018_120000_frame_000002.png",
             "20261018_120000_frame_000010.png",
             "20261018_120000_2_frame_000001.png",
             "20261018_120000_2_frame_000002.png"]
    _touch(tmp_path, *reversed(names))
    got = [Path(p).name for p in cr.list_images(str(tmp_path))]
    assert got == names


def test_frames_of_one_run_are_still_in_frame_order(tmp_path):
    names = [f"20261018_120000_frame_{i:06d}.png" for i in (1, 2, 9, 10, 11)]
    _touch(tmp_path, *reversed(names))
    got = [Path(p).name for p in cr.list_images(str(tmp_path))]
    assert got == names
