"""Reference cases the eddy-current model must pass (run them with Test)."""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from physics_kit import Data, eddy as ed, materials as mat     # noqa: E402
from model import BRAKE, HEAT, MODEL, PLATE                     # noqa: E402


def test_copper_skin_depth_at_60_hz_is_8_4_mm():
    assert math.isclose(ed.skin_depth(60, 1.68e-8) * 1000, 8.42, abs_tol=0.05)


def test_skin_depth_halves_when_frequency_quadruples():
    assert math.isclose(ed.skin_depth(4e3, 1e-7) / ed.skin_depth(1e3, 1e-7), 0.5, rel_tol=1e-9)


def test_the_plate_loss_meets_both_textbook_limits():
    rho, f, h0 = 4e-8, 1e3, 100.0
    d = ed.skin_depth(f, rho)
    thick = ed.plate_loss_per_area(h0, f, 100 * d, rho)
    assert math.isclose(thick, rho * h0 ** 2 / (2 * d), rel_tol=1e-9)
    t = 0.01 * d
    thin = ed.plate_loss_per_area(h0, f, t, rho)
    sheet = ed.thin_plate_loss_density(ed.MU_0 * h0, f, t, rho) * t / 2
    assert math.isclose(thin, sheet, rel_tol=1e-3)


def test_reitz_drag_peaks_at_the_characteristic_speed():
    _l, _d, w = ed.reitz_forces(1.0, 0.01, 1.0, 0.002, 1.68e-8)
    drags = [ed.reitz_forces(1.0, 0.01, v, 0.002, 1.68e-8)[1] for v in (w / 2, w, 2 * w)]
    assert drags[1] > drags[0] and drags[1] > drags[2]


def test_each_task_runs():
    for task in (HEAT, PLATE, BRAKE):
        r = MODEL.run({"task": task})
        assert r.values


def test_a_measured_conductivity_in_the_data_wins_over_the_table():
    data = Data({"meter": {"columns": ["Material", "Conductivity (%IACS)"],
                           "rows": [{"Material": "Aluminium 6061-T6", "Conductivity (%IACS)": 40.0}]}})
    r = MODEL.run({}, data)
    assert r.values["Conductivity (%IACS)"] == 40.0
    assert any("your measurement" in n for n in r.notes)
