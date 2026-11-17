"""Pictures made from an EVK4's .raw after a run that recorded it alone
(Andy, 17 November 2026: "can we have the EVK still record the video and
not convert into images while recording?")."""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from council_core import event_playback, raw_pictures   # noqa: E402

RUN = "20261117_100000"


class Playback:
    def __init__(self, count, window_us=20_000):
        self.count = count
        self.window_us = window_us
        self.error = ""
        self.asked = []
        self.closed = False

    def wait_done(self, timeout=None):
        return True

    def frame(self, k):
        self.asked.append(k)
        return np.full((4, 6), k % 256, np.uint8)

    def close(self):
        self.closed = True


def a_run(folder, count=5, every=20_000, named=()):
    with open(folder / f"{RUN}_frames.csv", "w", newline="",
              encoding="utf-8") as handle:
        out = csv.writer(handle)
        out.writerow(("file", "index", "timestamp_us", "raw_t_us", "events",
                      "window_us", "trigger"))
        for i in range(1, count + 1):
            name = f"{RUN}_frame_{i:06d}.png" if i in named else ""
            out.writerow((name, i, 5_000_000 + i * every, i * every, 7,
                          every, "t" if i == 2 else ""))
    raw = folder / f"{RUN}_events.raw"
    raw.write_bytes(b"% end\n")
    return raw


def rows(folder):
    with open(folder / f"{RUN}_frames.csv", newline="",
              encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def opener(playback, seen):
    def open_raw(path, window_us, **kw):
        seen.append((Path(path), window_us, kw))
        return playback
    return open_raw


def test_each_window_becomes_the_png_the_run_would_have_saved(tmp_path):
    raw = a_run(tmp_path)
    assert raw_pictures.wants_pictures(raw) == 5
    assert raw_pictures.pending(tmp_path) == [raw]
    playback, seen = Playback(10), []
    made = raw_pictures.make_pictures(raw, open_raw=opener(playback, seen))
    assert made.error == "" and made.written == 5
    assert seen[0][1] == 20_000 and seen[0][2]["origin_us"] == 5_000_000
    assert playback.closed
    assert playback.asked == [event_playback.window_for(i * 20_000, 20_000)
                              for i in range(1, 6)]
    after = rows(tmp_path)
    assert [r["file"] for r in after] == [f"{RUN}_frame_{i:06d}.png"
                                          for i in range(1, 6)]
    # Everything else in the CSV as it was.
    assert after[1]["trigger"] == "t" and after[0]["events"] == "7"
    assert all((tmp_path / r["file"]).is_file() for r in after)
    assert raw_pictures.wants_pictures(raw) == 0
    assert raw_pictures.pending(tmp_path) == []
    assert "5 pictures made" in made.line()


def test_a_png_already_there_is_left_alone(tmp_path):
    raw = a_run(tmp_path, count=3)
    there = tmp_path / f"{RUN}_frame_000002.png"
    there.write_bytes(b"mine")
    made = raw_pictures.make_pictures(raw, playback=Playback(10))
    assert (made.written, made.existed) == (2, 1)
    assert there.read_bytes() == b"mine"
    assert rows(tmp_path)[1]["file"] == there.name


def test_rows_past_the_end_of_the_raw_are_counted(tmp_path):
    raw = a_run(tmp_path, count=5)
    made = raw_pictures.make_pictures(raw, playback=Playback(3))
    assert made.missing == 2 and made.written == 3
    assert [r["file"] == "" for r in rows(tmp_path)] == [
        False, False, False, True, True]


def test_named_rows_are_not_made_again(tmp_path):
    raw = a_run(tmp_path, count=3, named=(1, 2, 3))
    playback = Playback(10)
    made = raw_pictures.make_pictures(raw, playback=playback)
    assert made.wanted == 0 and playback.asked == []
    assert "made already" in made.line()


def test_an_empty_raw_says_so(tmp_path):
    raw = a_run(tmp_path, count=2)
    made = raw_pictures.make_pictures(raw, playback=Playback(0))
    assert "no events" in made.error
    assert all(r["file"] == "" for r in rows(tmp_path))


def test_no_csv_is_an_error_not_a_raise(tmp_path):
    raw = tmp_path / f"{RUN}_events.raw"
    raw.write_bytes(b"")
    assert raw_pictures.wants_pictures(raw) == 0
    made = raw_pictures.make_pictures(raw, playback=Playback(5))
    assert "cannot read" in made.error


def test_the_command_line_converts_a_folder(tmp_path, monkeypatch, capsys):
    raw = a_run(tmp_path, count=2)
    monkeypatch.setattr(raw_pictures.event_playback, "open_raw",
                        lambda path, window_us, **kw: Playback(5))
    assert raw_pictures.main([str(tmp_path)]) == 0
    assert "2 pictures made" in capsys.readouterr().out
    assert raw_pictures.wants_pictures(raw) == 0
