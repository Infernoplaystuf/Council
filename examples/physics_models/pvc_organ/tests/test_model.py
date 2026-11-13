"""Reference cases the PVC organ model must pass (run them with Test)."""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from physics_kit import Data, acoustics as ac       # noqa: E402
from model import CHIMES, MODEL, STOPPED           # noqa: E402


def test_a4_is_440_and_middle_c_is_261_6():
    assert ac.note_frequency("A4") == 440.0
    assert math.isclose(ac.note_frequency("C4"), 261.6256, rel_tol=1e-6)


def test_speed_of_sound_at_20_c_is_343_m_per_s():
    assert math.isclose(ac.speed_of_sound(20), 343.2, abs_tol=0.2)


def test_an_open_pipe_cut_for_a_note_sounds_that_note():
    _od, d = ac.pvc_sch40("1/2")
    L = ac.pipe_length(440.0, d)
    assert math.isclose(ac.open_pipe_frequency(L, d), 440.0, rel_tol=1e-9)


def test_textbook_open_pipe_without_end_correction_is_half_a_wavelength():
    # c / (2 L): a 0.39 m pipe with no end correction at 20 °C sounds 440 Hz
    assert math.isclose(ac.open_pipe_frequency(0.39002, 0.0, 20), 440.0, rel_tol=1e-3)


def test_a_stopped_pipe_is_half_the_length_for_the_same_note():
    _od, d = ac.pvc_sch40("1/2")
    open_L = ac.pipe_length(262.0, d)
    stopped_L = ac.pipe_length(262.0, d, stopped=True)
    assert 0.45 < stopped_L / open_L < 0.52


def test_the_cut_list_has_a_row_per_note_and_gets_shorter_up_the_scale():
    r = MODEL.run({"notes": 8})
    rows = r.tables["Cut list"]
    assert [x["Note"] for x in rows] == ["C4", "D4", "E4", "F4", "G4", "A4", "B4", "C5"]
    lengths = [x["Cut length (mm)"] for x in rows]
    assert lengths == sorted(lengths, reverse=True)
    # the octave is close to half the length (the end correction makes it a bit more)
    assert 0.48 < lengths[-1] / lengths[0] < 0.52


def test_chimes_follow_the_free_free_beam_and_one_over_length_squared():
    r = MODEL.run({"instrument": CHIMES, "notes": 2, "scale": "Chromatic"})
    a, b = [x["Cut length (mm)"] for x in r.tables["Cut list"]]
    # a semitone up: f x 2^(1/12) -> L / 2^(1/24)
    assert math.isclose(a / b, 2 ** (1 / 24), rel_tol=1e-3)
    # overtones of a free-free bar are 2.756x and 5.404x the fundamental
    od, d = ac.pvc_sch40("1/2")
    I, A = ac.tube_section(od, d)
    f = ac.free_bar_frequencies(1.0, 3e9, 1380, I, A, 3)
    assert math.isclose(f[1] / f[0], 2.7565, rel_tol=1e-3)
    assert math.isclose(f[2] / f[0], 5.4039, rel_tol=1e-3)


def test_stopped_organ_fits_are_offered_for_stopped_pipes():
    r = MODEL.run({"pipe_type": STOPPED})
    assert set(r.fit_constants) == {"c", "r"}


def test_measured_pipes_are_compared_in_cents():
    data = Data({"pipes": {"columns": ["Length (mm)", "Frequency (Hz)"],
                           "units": {"Length (mm)": "mm"},
                           "rows": [{"Length (mm)": 640, "Frequency (Hz)": 262.9}]}})
    r = MODEL.run({}, data)
    rows = r.tables["Measured vs model (pipes)"]
    assert len(rows) == 1 and abs(rows[0]["Off (cents)"]) < 40
