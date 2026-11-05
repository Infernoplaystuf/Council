"""Reference cases the water erosion model must pass (run them with Test)."""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from physics_kit import erosion as er             # noqa: E402
from model import BED, MODEL, SLIDING             # noqa: E402


def test_finnie_ductile_erosion_is_worst_at_shallow_angles_and_small_at_90():
    assert er.finnie_angle(15) > er.finnie_angle(45) > er.finnie_angle(89)
    assert er.finnie_angle(90, ductile=False) == 1.0


def test_erosion_grows_with_speed_to_the_power_n():
    a = er.particle_erosion_ratio(10, 30, 1e-3, 2.5)
    b = er.particle_erosion_ratio(20, 30, 1e-3, 2.5)
    assert math.isclose(b / a, 2 ** 2.5, rel_tol=1e-9)


def test_shields_threshold_for_1_mm_sand_is_about_half_a_pascal():
    # Shields' curve: theta_c ~ 0.03 for d* ~ 25 -> tau_c ~ 0.5 Pa
    assert 0.35 < er.shields_critical_stress(0.001) < 0.65


def test_open_channel_shear_is_rho_g_h_s():
    assert math.isclose(er.bed_shear_stress(2.0, 0.001), 998.2 * 9.80665 * 2.0 * 0.001)


def test_nothing_erodes_below_the_threshold():
    assert er.excess_shear_rate(0.4, 0.5) == 0.0


def test_the_materials_are_compared_and_sorted_by_wear_rate():
    r = MODEL.run({})
    rows = r.tables["Materials compared (same water, speed and angle)"]
    rates = [x["Wear rate (mm/year)"] for x in rows]
    assert rates == sorted(rates) and len(rows) >= 8


def test_slow_water_does_not_move_gravel_but_fast_water_does():
    slow = MODEL.run({"mechanism": BED, "soil": "Gravel (10 mm)", "speed_m_s": 0.3})
    fast = MODEL.run({"mechanism": BED, "soil": "Gravel (10 mm)", "speed_m_s": 3.0})
    assert slow.values["Moving?"].startswith("no") and fast.values["Moving?"] == "yes"


def test_sliding_wear_runs():
    assert MODEL.run({"mechanism": SLIDING}).values["Depth worn"][0] > 0
