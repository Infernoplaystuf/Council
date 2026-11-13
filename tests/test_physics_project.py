"""Physics models in the Build tab (council_core.physics_project and
council_core.physics_fit): making a model, connecting data (a folder, a
Ledger, Connections), running it in the sandbox, calibrating it, and the
four worked examples (user, 2026-11-05: a PVC pipe organ, radio antennas,
eddy currents, water erosion) each passing its own reference tests."""
from __future__ import annotations

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("scipy")

from council_core import physics_fit as pf  # noqa: E402
from council_core import physics_project as pp  # noqa: E402

PY = sys.executable


# ── the equation fitter ─────────────────────────────────────────────────
def test_an_equation_is_parsed_safely():
    eq = pf.parse("k = A*exp(-Ea/(R*T))", ("T",))
    assert eq.params == ("A", "Ea") and eq.constants == ("R",)
    for bad in ("y = __import__('os').system('x')", "y = x.real", "y = [x][0]",
                "y = (lambda q: q)(x)", "y = open(x)"):
        with pytest.raises(pf.FitError):
            pf.parse(bad)
    with pytest.raises(pf.FitError):
        pf.parse("y = a*t")                                # the input x is not used


def test_fixed_values_from_the_model_are_not_fitted():
    eq = pf.parse("f = c/(2*(L + 2*k*r))", ("L",), fixed={"c": 343.0, "r": 0.008, "zz": 1})
    assert eq.params == ("k",) and eq.fixed == {"c": 343.0, "r": 0.008}


def test_arrhenius_is_fitted_with_its_95_percent_range():
    t = [300, 320, 340, 360, 380, 400, 420]
    true_a, true_ea = 1e7, 50_000.0
    y = [true_a * math.exp(-true_ea / (8.314462618 * x)) * (1 + 0.01 * (-1) ** i)
         for i, x in enumerate(t)]
    fit = pf.fit_law("arrhenius", t, y, x_unit="K")
    ea = next(p for p in fit.params if p.name == "Ea")
    assert ea.low < true_ea < ea.high and math.isclose(ea.value, true_ea, rel_tol=0.02)
    assert ea.unit == "J/mol" and fit.r2 > 0.999


def test_celsius_is_converted_to_kelvin_for_arrhenius_and_predictions_follow():
    tc = [27, 47, 67, 87, 107]
    y = [2e6 * math.exp(-40_000 / (8.314462618 * (x + 273.15))) for x in tc]
    fit = pf.fit_law("arrhenius", tc, y, x_unit="°C")
    assert any("°C" in n for n in fit.notes)
    pr = pf.predict(fit, 77)
    assert math.isclose(pr.value, 2e6 * math.exp(-40_000 / (8.314462618 * 350.15)), rel_tol=1e-3)
    assert pr.new_low <= pr.value <= pr.new_high and not pr.outside
    assert pf.predict(fit, 500).outside


def test_ranking_prefers_the_true_law():
    x = [0.5 * i for i in range(1, 15)]
    y = [3.0 * math.exp(-v / 2.0) + 1.0 for v in x]
    ranked = pf.try_all(["decay", "line", "quadratic"], x, y)
    assert ranked[0].key == "decay"
    assert pf.ranking_rows(ranked)[0]["Verdict"] == "best"


def test_too_few_points_is_said_plainly():
    with pytest.raises(pf.FitError, match="more points than parameters"):
        pf.fit_law("quadratic", [1, 2, 3], [1, 4, 9])


def test_a_fit_is_saved_as_json(tmp_path):
    fit = pf.fit_law("line", [1, 2, 3, 4], [2.1, 3.9, 6.2, 7.8])
    out = pf.save(fit, tmp_path / "models.json", source="test")
    pf.save(fit, out)
    items = json.loads(out.read_text(encoding="utf-8"))
    assert len(items) == 2 and items[0]["equation"] == "y = a + b*x" and items[0]["source"] == "test"


# ── making a model ──────────────────────────────────────────────────────
@pytest.mark.parametrize("example", list(pp.EXAMPLES))
def test_each_example_is_made_with_the_kit_and_a_git_commit(tmp_path, example):
    dest = pp.create(tmp_path / example, example=example)
    assert pp.is_model(dest) and (dest / "physics_kit" / "antennas.py").is_file()
    assert not (dest / "physics_kit" / "__pycache__").exists()
    assert pp.links(dest) and pp.links(dest)[0].kind == pp.FOLDER
    if subprocess.run(["git", "--version"], capture_output=True).returncode == 0:
        log = subprocess.run(["git", "log", "--oneline"], cwd=dest, capture_output=True, text=True)
        assert "New physics model" in log.stdout


def test_a_goal_makes_the_blank_model_and_the_agent_request(tmp_path):
    dest = pp.create(tmp_path / "bars", goal="Notes of struck steel bars", git=False,
                     links=[pp.Link(pp.LEDGER, "C:/x/bars.csv", "Bar tests")])
    assert "Notes of struck steel bars" in (dest / "model.py").read_text(encoding="utf-8")
    assert pp.load_manifest(dest)["goal"] == "Notes of struck steel bars"
    req = pp.goal_request("Notes of struck steel bars", pp.links(dest))
    assert "README.md" in req and "Ledger: Bar tests" in req
    res = pp.run(dest, PY, {})
    assert res.ok, res.error
    assert "not been built yet" in res.payload["notes"][0]
    with pytest.raises(pp.PhysicsError):
        pp.create(dest, goal="again")                      # not over a model that's there
    with pytest.raises(pp.PhysicsError):
        pp.create(tmp_path / "empty", goal="  ")


def test_the_physics_modeller_is_a_built_in_agent_reading_the_examples():
    from council_core import code_profiles as cp
    prof = next(p for p in cp.BUILT_INS if p.id == pp.PROFILE_ID)
    assert prof.name == "Physics modeller" and "physics_kit" in prof.instructions
    assert prof.references == [str(pp.EXAMPLES_DIR)] and prof.output == "patch"


# ── connected data ──────────────────────────────────────────────────────
def test_a_folder_gives_tables_with_units_and_texts(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "pipes.csv").write_text("Length (mm),Frequency (Hz)\n640,263\n512,327\n",
                                    encoding="utf-8")
    (data / "notes.md").write_text("PVC from supplier X", encoding="utf-8")
    (data / "~$lock.csv").write_text("junk", encoding="utf-8")
    g = pp.gather(tmp_path, items=[pp.Link(pp.FOLDER, str(data))])
    assert set(g.tables) == {"pipes"} and g.texts == {"notes.md": "PVC from supplier X"}
    t = g.tables["pipes"]
    assert t["units"] == {"Length (mm)": "mm", "Frequency (Hz)": "Hz"} and len(t["rows"]) == 2
    missing = pp.gather(tmp_path, items=[pp.Link(pp.FOLDER, str(tmp_path / "nope"))])
    assert missing.notes and not missing.tables


def test_a_ledger_gives_its_rows_with_numbers_and_units(tmp_path):
    from sample_ledger import ParentCSV
    csv = tmp_path / "bars.csv"
    csv.write_text("Part,Length,Frequency\nB1,300 mm,520\nB2,250 mm,749\n", encoding="utf-8")
    try:
        ParentCSV(csv)
    except Exception as exc:                               # noqa: BLE001
        pytest.skip(f"this Ledger CSV shape isn't readable here: {exc}")
    g = pp.gather(tmp_path, items=[pp.Link(pp.LEDGER, str(csv), "Bar tests")])
    assert "Bar tests" in g.tables, g.notes
    rows = g.tables["Bar tests"]["rows"]
    assert rows[0]["Length"] == 300.0 and rows[1]["Frequency"] == 749.0


def test_connections_knowledge_is_asked_about_the_question(tmp_path, monkeypatch):
    from council_core import kg_query
    asked = {}

    def fake_gather(vault, question, graphs=None, **kw):
        asked.update(vault=vault, question=question, graphs=graphs)
        return [{"text": "[1] Alloy 6061 conductivity 43 %IACS (datasheet.pdf)"}]

    monkeypatch.setattr(kg_query, "gather", fake_gather)
    g = pp.gather(tmp_path, tmp_path / "vault",
                  items=[pp.Link(pp.KNOWLEDGE, "g1", "Materials", "conductivity of 6061")])
    assert asked == {"vault": tmp_path / "vault", "question": "conductivity of 6061",
                     "graphs": ["g1"]}
    assert "43 %IACS" in next(iter(g.texts.values()))


def test_links_are_kept_in_physics_json(tmp_path):
    dest = pp.create(tmp_path / "m", example="antenna", git=False)
    pp.save_links(dest, [pp.Link(pp.KNOWLEDGE, "", "Main", "antenna notes")])
    assert pp.links(dest)[0].label() == 'Connections: Main: "antenna notes"'
    assert pp.load_manifest(dest)["example"] == "antenna"


# ── running and calibrating each example ───────────────────────────────
@pytest.mark.parametrize("example", list(pp.EXAMPLES))
def test_each_example_runs_in_the_sandbox_with_its_sample_data(tmp_path, example):
    dest = pp.create(tmp_path / example, example=example, git=False)
    d = pp.describe(dest, PY)
    assert d.ok, d.error
    assert d.payload["inputs"] and d.payload["limits"]
    g = pp.gather(dest)
    assert g.tables, g.notes
    r = pp.run(dest, PY, {}, g)
    assert r.ok, r.output
    assert r.payload["values"] and r.payload["tables"] and r.payload["charts"]
    json.dumps(r.payload, allow_nan=False)


@pytest.mark.parametrize("example", list(pp.EXAMPLES))
def test_each_examples_own_reference_tests_pass(tmp_path, example):
    dest = pp.create(tmp_path / example, example=example, git=False)
    out = subprocess.run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=dest,
                         capture_output=True, text=True, timeout=300)
    assert out.returncode == 0, out.stdout + out.stderr


def _calibrate(tmp_path, example, inputs, table, x, y):
    dest = pp.create(tmp_path / example, example=example, git=False)
    d = pp.describe(dest, PY).payload
    g = pp.gather(dest)
    r = pp.run(dest, PY, inputs, g).payload
    spec = pp.fits_for(d, {**{i["name"]: i["default"] for i in d["inputs"]}, **inputs})[0]
    t = g.tables[table]
    from physics_kit import to_number
    xs = [to_number(row[x]) for row in t["rows"]]
    ys = [to_number(row[y]) for row in t["rows"]]
    return pp.calibrate(spec, xs, ys, r["fit_constants"], t["units"].get(x, ""))


def test_the_organ_calibrates_its_end_correction_from_measured_pipes(tmp_path):
    cal = _calibrate(tmp_path, "pvc_organ", {}, "measured_pipes", "Length (mm)", "Frequency (Hz)")
    # the sample pipes were made with k = 0.80 (and a little tuner noise)
    assert math.isclose(cal.sets["end_correction"], 0.80, abs_tol=0.08)
    assert cal.fit.r2 > 0.999


def test_the_eddy_model_calibrates_conductivity_from_phase_readings(tmp_path):
    cal = _calibrate(tmp_path, "eddy_current", {}, "reference_block_phase",
                     "Flaw depth (mm)", "Phase lag (deg)")
    assert math.isclose(cal.sets["conductivity_ms_m"], 24.0, rel_tol=0.08)


def test_the_erosion_model_calibrates_k_and_n_from_tests(tmp_path):
    cal = _calibrate(tmp_path, "water_erosion", {}, "erosion_tests", "Speed (m/s)",
                     "Erosion ratio (kg/kg)")
    assert math.isclose(cal.sets["exponent_override"], 2.6, abs_tol=0.3)
    assert math.isclose(cal.sets["erosion_ratio_override"], 3e-4, rel_tol=0.3)


def test_units_are_converted_for_a_fit():
    assert pp.convert_x([640.0], "mm", "m") == [0.64]
    assert pp.convert_x([1.0], "", "m") == [1.0]
    assert pp.unit_of("Speed (m/s)") == "m/s" and pp.unit_of("Speed") == ""


def test_a_broken_model_is_reported_not_raised(tmp_path):
    dest = pp.create(tmp_path / "m", goal="anything", git=False)
    (dest / "model.py").write_text("import nonexistent_module_xyz\n", encoding="utf-8")
    res = pp.describe(dest, PY)
    assert not res.ok and "nonexistent_module_xyz" in res.error
