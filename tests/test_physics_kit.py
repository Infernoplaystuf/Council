"""physics_kit, the tested physics blocks Council's physics models are built
from (user, 2026-11-05: physics models in the Build tab, e.g. a PVC pipe
organ, radio antennas, eddy currents, water erosion). Textbook reference
values; the examples' own tests are run by test_physics_project.py."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from physics_kit import Chart, Data, Fit, Input, Model, Result, to_number  # noqa: E402
from physics_kit import acoustics as ac  # noqa: E402
from physics_kit import antennas as an  # noqa: E402
from physics_kit import eddy as ed  # noqa: E402
from physics_kit import erosion as er  # noqa: E402
from physics_kit import materials as mat  # noqa: E402
from physics_kit import run as kit_run  # noqa: E402
from physics_kit.units import ft_in  # noqa: E402


# ── acoustics ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("note,midi", [("C4", 60), ("A4", 69), ("Bb3", 58), ("F#2", 42),
                                        ("Cb4", 59), ("B#3", 60), ("C-1", 0)])
def test_note_names_are_midi_numbers(note, midi):
    assert ac.midi_number(note) == midi


def test_equal_temperament_and_the_nearest_note():
    assert math.isclose(ac.note_frequency("A3"), 220.0)
    assert math.isclose(ac.note_frequency("C4"), 261.6256, rel_tol=1e-6)
    name, off = ac.nearest_note(445.0)
    assert name == "A4" and math.isclose(off, 1200 * math.log2(445 / 440), rel_tol=1e-9)
    with pytest.raises(ValueError):
        ac.midi_number("H2")


def test_a_major_scale_from_c4_is_the_white_notes():
    assert ac.scale_notes("C4", "Major", 8) == ["C4", "D4", "E4", "F4", "G4", "A4", "B4", "C5"]


def test_pipe_physics_matches_the_textbook():
    assert math.isclose(ac.speed_of_sound(0), 331.3)
    # a stopped pipe sounds an octave below an open pipe of the same length
    f_open = ac.open_pipe_frequency(0.5, 0.0)
    f_stop = ac.stopped_pipe_frequency(0.5, 0.0)
    assert math.isclose(f_open / f_stop, 2.0)
    # the end correction makes a pipe sound flatter
    assert ac.open_pipe_frequency(0.5, 0.02) < f_open


def test_schedule_40_half_inch_pvc_is_0_840_by_0_622_inch():
    od, idia = ac.pvc_sch40("1/2")
    assert math.isclose(od / 0.0254, 0.840) and math.isclose(idia / 0.0254, 0.622)


def test_free_free_bar_overtone_ratios():
    f = ac.free_bar_frequencies(1.0, 70e9, 2700, *ac.bar_section(0.05, 0.01), modes=3)
    assert math.isclose(f[1] / f[0], 2.7565, rel_tol=1e-3)
    L = ac.bar_length(f[0], 70e9, 2700, *ac.bar_section(0.05, 0.01))
    assert math.isclose(L, 1.0, rel_tol=1e-9)


def test_a_one_litre_bottle_rings_near_100_hz():
    # Helmholtz: V = 1 L, neck 2 cm across and 5 cm long -> about 120 Hz
    f = ac.helmholtz_frequency(1e-3, math.pi * 0.01 ** 2, 0.05)
    assert 90 < f < 140


def test_feet_and_inches_round_to_a_sixteenth():
    assert ft_in(0.3048) == "1' 0\""
    assert ft_in(0.0254 * 13.5) == "1' 1 1/2\""


# ── antennas ─────────────────────────────────────────────────────────────
def test_the_half_wave_dipole_is_73_ohm_and_2_15_dbi():
    f = 100e6
    lam = an.wavelength(f)
    L = an.resonant_dipole_length(f, lam * 1e-4)
    s = an.dipole(f, L, lam * 1e-4)
    assert 0.48 < L / lam < 0.495
    assert 65 < s.impedance.real < 76 and abs(s.impedance.imag) < 1
    assert math.isclose(s.directivity_dbi, 2.15, abs_tol=0.1)
    # broadside: the most gain is at right angles to the wire
    assert math.isclose(s.max_direction[0], 90.0, abs_tol=2.1)


def test_the_solver_converges_as_segments_are_added():
    f = 100e6
    lam = an.wavelength(f)
    z = [an.dipole(f, 0.5 * lam, lam * 1e-3, segments=n).impedance for n in (40, 80, 160)]
    assert abs(z[2] - z[1]) < abs(z[1] - z[0])
    assert abs(z[2] - z[1]) < 0.02 * abs(z[2])          # within 2 %


def test_a_yagi_beats_a_dipole_and_points_at_its_directors():
    _l, _p, s = an.yagi_design(100e6, 4, 0.003)
    assert s.directivity_dbi > 7.0
    assert s.front_to_back_db and s.front_to_back_db > 5
    assert s.max_direction == (90.0, 0.0)
    assert abs(s.impedance.imag) < 1


def test_swr_and_its_bandwidth():
    assert math.isclose(an.swr(50, 50), 1.0)
    assert math.isclose(an.swr(100, 50), 2.0)
    sweep = [(1.0, 3.0, 0j), (2.0, 1.8, 0j), (3.0, 1.1, 0j), (4.0, 1.9, 0j), (5.0, 2.5, 0j)]
    assert an.swr_bandwidth(sweep) == (2.0, 4.0)


# ── eddy currents ────────────────────────────────────────────────────────
def test_skin_depth_of_copper_and_aluminium():
    assert math.isclose(ed.skin_depth(60, mat.get("copper").resistivity), 8.42e-3, rel_tol=0.01)
    d = ed.skin_depth(1e5, mat.get("Aluminium 6061-T6").resistivity)
    assert math.isclose(d, 0.318e-3, rel_tol=0.01)
    assert math.isclose(ed.frequency_for_depth(d, mat.get("al").resistivity), 1e5, rel_tol=1e-9)


def test_the_plate_solution_meets_its_two_limits():
    rho, f = 4e-8, 1e3
    d = ed.skin_depth(f, rho)
    assert math.isclose(ed.plate_loss_per_area(10, f, 60 * d, rho), rho * 100 / (2 * d))
    t = 0.02 * d
    assert math.isclose(ed.plate_loss_per_area(10, f, t, rho),
                        ed.thin_plate_loss_density(ed.MU_0 * 10, f, t, rho) * t / 2, rel_tol=1e-3)


def test_reitz_lift_takes_over_from_drag_at_high_speed():
    _l, _d, w = ed.reitz_forces(1.0, 0.02, 1.0, 0.001, 1.68e-8)
    lift, drag, _ = ed.reitz_forces(1.0, 0.02, 10 * w, 0.001, 1.68e-8)
    assert lift > drag
    lift, drag, _ = ed.reitz_forces(1.0, 0.02, 0.1 * w, 0.001, 1.68e-8)
    assert drag > lift


# ── erosion ──────────────────────────────────────────────────────────────
def test_finnie_peaks_near_15_to_20_degrees():
    best = max(range(1, 90), key=er.finnie_angle)
    assert 10 <= best <= 20
    assert math.isclose(er.finnie_angle(best), 1.0, abs_tol=0.01)


def test_shields_threshold_rises_with_grain_size_for_gravel():
    assert er.shields_critical_stress(0.01) > er.shields_critical_stress(0.001)
    assert 6 < er.shields_critical_stress(0.01) < 11        # ~8 Pa for 10 mm gravel


def test_hanson_simon_erodibility():
    assert math.isclose(er.erodibility_hanson_simon(4.0), 0.1e-6)
    assert math.isclose(er.excess_shear_rate(6.0, 4.0), 0.2e-6)


def test_archard():
    assert math.isclose(er.archard_volume(1e-4, 10, 1000, 1e9), 1e-9)


# ── materials ────────────────────────────────────────────────────────────
def test_materials_by_name_and_alias_and_iacs():
    assert mat.get("Cu") is mat.get("copper")
    assert math.isclose(mat.get("copper").iacs_percent, 102.6, abs_tol=0.5)
    assert mat.get("PVC").resistivity is None
    with pytest.raises(KeyError):
        mat.get("unobtainium")
    for m in (mat.get(n) for n in mat.names()):
        assert m.sure in ("good", "fair", "rough")


# ── the model format and its runner ─────────────────────────────────────
def _toy_model(tmp_path: Path) -> Path:
    (tmp_path / "model.py").write_text(
        "from physics_kit import Chart, Fit, Input, Model, Result\n"
        "def compute(p, data):\n"
        "    r = Result()\n"
        "    r.values['double'] = (2 * p['x'], 'm')\n"
        "    r.values['never'] = float('inf')\n"
        "    r.tables['rows'] = [{'a': 1, 'b': float('nan')}]\n"
        "    r.charts.append(Chart('c', [1, 2], {'y': [1, float('nan')]}))\n"
        "    r.notes.append(f\"tables: {data.names()}\")\n"
        "    return r\n"
        "MODEL = Model('Toy', 'a toy', [Input('x', 1.5, 'm'), Input('n', 3)], compute,\n"
        "              fits=[Fit('f', 'y = a*x', 'x', sets={'a': 'x'})])\n",
        encoding="utf-8")
    return tmp_path / "model.py"


def test_inputs_are_coerced_to_the_defaults_type():
    m = Model("t", "", [Input("n", 3), Input("x", 1.0), Input("b", True)],
              lambda p, d: Result(values={"p": (p["n"], p["x"], p["b"])}))
    assert m.run({"n": 4.6, "x": "2", "b": "no", "other": 1}).values["p"] == (5, 2.0, False)


def test_the_runner_describes_and_runs_a_model_as_json(tmp_path, capsys):
    path = _toy_model(tmp_path)
    assert kit_run.main([str(path), "--describe"]) == 0
    d = json.loads(capsys.readouterr().out)
    assert [i["name"] for i in d["inputs"]] == ["x", "n"] and d["fits"][0]["sets"] == {"a": "x"}
    (tmp_path / "in.json").write_text('{"x": 2}', encoding="utf-8")
    (tmp_path / "data.json").write_text('{"tables": {"t1": {"columns": [], "rows": []}}}',
                                        encoding="utf-8")
    out = tmp_path / "out.json"
    assert kit_run.main([str(path), "--inputs", str(tmp_path / "in.json"), "--data",
                         str(tmp_path / "data.json"), "--out", str(out)]) == 0
    r = json.loads(out.read_text(encoding="utf-8"))       # strict JSON: no NaN or Infinity
    assert r["values"]["double"] == {"value": 4.0, "unit": "m"}
    assert r["values"]["never"]["value"] == "∞"
    assert r["tables"]["rows"][0]["b"] is None and r["charts"][0]["series"]["y"] == [1.0, None]
    assert r["notes"] == ["tables: ['t1']"]


def test_a_broken_model_reports_its_error(tmp_path, capsys):
    (tmp_path / "model.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    assert kit_run.main([str(tmp_path / "model.py"), "--describe"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert "boom" in out["error"]


def test_data_helpers():
    d = Data({"m": {"columns": ["Length (mm)", "Freq"], "units": {"Length (mm)": "mm"},
                    "rows": [{"Length (mm)": "12.5 mm", "Freq": 3}, {"Length (mm)": "", "Freq": 4}]}})
    assert d.find_column("length") == ("m", "Length (mm)")
    assert d.pairs("m", "Length (mm)", "Freq") == [(12.5, 3.0)]
    assert d.unit("m", "Length (mm)") == "mm" and not Data()
    assert to_number("1,234.5") == 1234.5 and to_number(None) is None
