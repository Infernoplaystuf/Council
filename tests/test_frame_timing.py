"""
frame_timing — separating a bad-timing capture from a normal letterboxed one.

No real frames were available, so these are SYNTHESISED from the user's
description:

  normal     a widescreen picture: bright content across the middle, black
             bars above and below
  bad timing almost entirely black, with one or two white bars low in the
             frame — the rows that got exposed before readout ended

That means the thresholds in frame_timing are tuned against synthetic data,
not against a real camera. The separation here is large (a normal frame is
~60% lit, a bad one ~97% dark with a thin bar cluster at 0.9 of frame height),
so the ordering is not in doubt — but a real sensor with noise, a different
aspect ratio, or a partially-lit readout could sit closer to the line. The
constants are named at module top for exactly that reason.

Run:  python -m pytest tests/test_frame_timing.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import frame_timing as ft   # noqa: E402

np = pytest.importorskip("numpy")
PIL = pytest.importorskip("PIL")
from PIL import Image       # noqa: E402


W, H = 320, 180


def _save(arr, path):
    Image.fromarray((arr * 255).clip(0, 255).astype("uint8"), "L").save(path)


def normal_frame(brightness: float = 0.75):
    """Letterboxed: black bars top and bottom, picture across the middle."""
    a = np.zeros((H, W), dtype=np.float32)
    top, bot = int(H * 0.18), int(H * 0.82)
    # Some horizontal variation so it reads as picture content, not a flat box.
    band = np.linspace(brightness * 0.8, brightness, W, dtype=np.float32)
    a[top:bot, :] = band
    return a


def bad_timing_frame(bars=((0.86, 0.90), (0.93, 0.96))):
    """Almost all black with narrow white bars low in the frame."""
    a = np.zeros((H, W), dtype=np.float32)
    for lo, hi in bars:
        a[int(H * lo):int(H * hi), :] = 1.0
    return a


def dropped_frame():
    """Entirely black — a different fault, and not what we are counting."""
    return np.zeros((H, W), dtype=np.float32)


# ============================================================
# The decision, on profiles directly
# ============================================================

def test_a_normal_letterboxed_frame_is_not_flagged():
    prof = normal_frame().mean(axis=1)
    bad, reason, m = ft.classify_profile(prof)
    assert not bad, (reason, m)


def test_a_bad_timing_frame_is_flagged():
    prof = bad_timing_frame().mean(axis=1)
    bad, reason, m = ft.classify_profile(prof)
    assert bad, (reason, m)
    assert m["dark_fraction"] > ft.MOSTLY_DARK
    assert m["lit_centre"] > 1.0 - ft.BOTTOM_BAND


def test_a_single_bar_still_counts():
    prof = bad_timing_frame(bars=((0.90, 0.94),)).mean(axis=1)
    bad, _r, _m = ft.classify_profile(prof)
    assert bad


def test_a_wholly_black_frame_is_NOT_called_a_bad_timing():
    """A dropped frame is a real fault but a DIFFERENT one. Lumping the two
    together would make the count useless for diagnosing either."""
    bad, reason, _m = ft.classify_profile(dropped_frame().mean(axis=1))
    assert not bad
    assert "dropped" in reason


def test_bright_rows_in_the_MIDDLE_are_normal_letterboxing():
    """The whole discriminator is WHERE the lit rows are. A mostly-dark frame
    whose thin lit band sits mid-frame is a dim picture, not a bad timing."""
    a = np.zeros((H, W), dtype=np.float32)
    a[int(H * 0.45):int(H * 0.55), :] = 1.0
    bad, reason, _m = ft.classify_profile(a.mean(axis=1))
    assert not bad
    assert "mid-frame" in reason


def test_many_lit_bands_read_as_picture_detail():
    """A finely striped pattern is MOSTLY DARK and still has lit rows near the
    bottom — so it passes the darkness and position guards, and only the band
    COUNT stops it being mistaken for readout bars.

    The stripes have to be thin (1 row in 12) to get past the darkness guard
    at all: a coarser pattern is only ~67% dark and is rejected one rule
    earlier as an ordinary exposure."""
    a = np.zeros((H, W), dtype=np.float32)
    for i in range(0, H, 12):
        a[i:i + 1, :] = 1.0
    prof = a.mean(axis=1)
    bad, reason, m = ft.classify_profile(prof)
    assert m["dark_fraction"] > ft.MOSTLY_DARK, "should reach the band rule"
    assert m["lit_bands"] > ft.MAX_BARS
    assert not bad
    assert "picture detail" in reason


def test_an_empty_profile_does_not_raise():
    bad, _r, _m = ft.classify_profile(np.zeros((0,), dtype=np.float32))
    assert not bad


# ============================================================
# End to end over a folder
# ============================================================

def test_classify_folder_counts_only_the_bad_ones(tmp_path):
    for i in range(5):
        _save(normal_frame(), tmp_path / f"good_{i:03d}.png")
    for i in range(2):
        _save(bad_timing_frame(), tmp_path / f"bad_{i:03d}.png")
    _save(dropped_frame(), tmp_path / "dropped.png")

    rep = ft.classify_folder(tmp_path)
    assert rep.total == 8
    assert rep.bad == 2, [v.describe() for v in rep.verdicts]
    assert abs(rep.bad_fraction - 0.25) < 1e-6
    assert "2 of 8" in rep.summary()


def test_count_bad_frames_is_the_one_number_a_gui_binds(tmp_path):
    _save(normal_frame(), tmp_path / "a.png")
    _save(bad_timing_frame(), tmp_path / "b.png")
    assert ft.count_bad_frames(tmp_path) == 1


def test_a_missing_folder_reports_rather_than_raising(tmp_path):
    """A GUI button must not blow up on a path the user mistyped."""
    rep = ft.classify_folder(tmp_path / "nope")
    assert rep.total == 0
    assert "not a folder" in rep.summary()


def test_one_corrupt_file_does_not_abort_the_scan(tmp_path):
    _save(normal_frame(), tmp_path / "good.png")
    _save(bad_timing_frame(), tmp_path / "bad.png")
    (tmp_path / "broken.png").write_bytes(b"not actually a png")

    rep = ft.classify_folder(tmp_path)
    assert rep.bad == 1, "the good frames must still be scanned"
    assert rep.unreadable == 1
    assert any("broken.png" in e for e in rep.errors)


def test_non_images_are_ignored(tmp_path):
    _save(bad_timing_frame(), tmp_path / "b.png")
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    rep = ft.classify_folder(tmp_path)
    assert rep.total == 1


def test_the_separation_is_wide_not_marginal(tmp_path):
    """Guards the tuning. If a future change narrows the gap between the two
    populations, this fails before anyone trusts a borderline count."""
    good = ft.classify_profile(normal_frame().mean(axis=1))[2]
    bad = ft.classify_profile(bad_timing_frame().mean(axis=1))[2]
    assert bad["dark_fraction"] - good["dark_fraction"] > 0.5, (good, bad)
    assert bad["lit_centre"] - good["lit_centre"] > 0.3, (good, bad)
