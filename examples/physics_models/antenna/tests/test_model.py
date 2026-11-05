"""Reference cases the antenna model must pass (run them with Test)."""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from physics_kit import Data, antennas as an        # noqa: E402
from model import MONOPOLE, MODEL                   # noqa: E402

F = 146e6
LAM = an.wavelength(F)


def test_wavelength_at_146_mhz_is_2_05_m():
    assert math.isclose(LAM, 2.0534, rel_tol=1e-4)


def test_a_half_wave_dipole_is_about_73_ohm_and_2_15_dbi():
    # textbook: R ~ 73 ohm at resonance, directivity 1.64 (2.15 dBi)
    L = an.resonant_dipole_length(F, LAM * 1e-4)
    s = an.dipole(F, L, LAM * 1e-4)
    assert 65 < s.impedance.real < 75
    assert abs(s.impedance.imag) < 1.0
    assert math.isclose(s.directivity_dbi, 2.15, abs_tol=0.1)


def test_a_thin_dipole_resonates_a_little_shorter_than_half_a_wavelength():
    L = an.resonant_dipole_length(F, LAM * 1e-3)
    assert 0.47 < L / LAM < 0.485


def test_a_short_dipole_has_directivity_1_5():
    s = an.dipole(F, 0.05 * LAM, LAM * 1e-4)
    assert math.isclose(s.directivity_dbi, 10 * math.log10(1.5), abs_tol=0.05)


def test_a_quarter_wave_monopole_is_half_the_dipole_impedance_and_3_db_more():
    r = MODEL.run({"antenna": MONOPOLE})
    assert 30 < r.values["Input resistance"][0] < 40
    assert math.isclose(r.values["Gain"][0], 2.15 + 3.01, abs_tol=0.15)


def test_a_yagi_has_gain_over_a_dipole_and_points_forward():
    lengths, positions, s = an.yagi_design(F, 3, 0.003)
    assert s.directivity_dbi > 6.0
    assert s.front_to_back_db > 5
    assert s.max_direction[1] in (0.0,)


def test_an_analyzer_sweep_gives_the_measured_resonance_and_trim_advice():
    data = Data({"sweep": {"columns": ["Frequency (MHz)", "SWR"],
                           "rows": [{"Frequency (MHz)": 142 + i, "SWR": 1.1 + abs(144 - (142 + i))}
                                    for i in range(5)]}})
    r = MODEL.run({}, data)
    assert r.values["Measured resonance"][0] == 144.0
    assert any("shorten" in n for n in r.notes)
