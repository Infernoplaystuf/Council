"""
Equations fitted to a Ledger's numbers (council_core.ledger_equations) and
the Analyze window's Equations page - Andy, 2026-11-13, from his LMM
Analysis Studio script: "I like what it does for ... equation deriving".

Every Ledger is synthetic, built in tmp_path, with relations planted in it.

Run:  python -m pytest tests/test_ledger_equations.py -q
"""
from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")
sys.path.insert(0, str(Path(__file__).parent))

from council_core import ledger_analytics as la  # noqa: E402
from council_core import ledger_equations as le  # noqa: E402
from council_core import ledger_ingest as li  # noqa: E402
from ledger_fixtures import make_template  # noqa: E402
from sample_ledger import LedgerError, ParentCSV  # noqa: E402

FOD = "Flaw Attributes: Laser Team FOD (um)"
LENGTH = "Flaw Attributes: Flaw Length (in)"
POWER, SPEED, FREQ = "Power (W)", "Scan Speed (mm/s)", "Frequency (kHz)"
ITEMS = ("1A", "2A", "3A", "1B", "2B", "3B")


@pytest.fixture
def app_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNCIL_APP_DIR", str(tmp_path / "app"))
    return tmp_path / "app"


@pytest.fixture
def ledger(tmp_path, app_dir):
    """24 parts of 6 flaws: FOD = 200 x Power / Speed (+ noise), Length =
    0.01 + 0.0004 x Power + 0.00002 x FOD; Frequency 50 on every part."""
    path = li.create_parent(make_template(tmp_path / "T.xlsx"), tmp_path / "l.csv").csv
    rnd = random.Random(3)
    rows = []
    for p in range(1, 25):
        power, speed = rnd.choice([20, 30, 40, 50]), rnd.choice([100, 200, 300, 400])
        for item in ITEMS:
            fod = 200 * power / speed * (1 + rnd.gauss(0, 0.02))
            rows.append({"Part Number": f"S-{p}", "Flaw #": item, POWER: str(power),
                         SPEED: str(speed), FREQ: "50", FOD: f"{fod:.3f}",
                         LENGTH: f"{0.01 + 0.0004 * power + 0.00002 * fod:.6f}",
                         "source_file": f"b{p}.xlsx"})
    ParentCSV(path).add_samples(rows)
    return Path(path)


def test_a_power_law_finds_a_ratio_and_beats_the_others(ledger):
    d = la.load(ledger)
    eq = le.fit(d, FOD, [POWER, SPEED], le.POWER)
    a, b = eq.coefficients
    assert a == pytest.approx(1.0, abs=0.05) and b == pytest.approx(-1.0, abs=0.05)
    assert np.exp(eq.intercept) == pytest.approx(200, rel=0.1)
    assert eq.scores["cv_r2"] > 0.98 and eq.parts == 24 and eq.rows == 24 * len(ITEMS)
    ranked = le.compare(d, FOD, [POWER, SPEED])
    assert ranked.iloc[0]["Kind"] == le.POWER
    assert ranked.iloc[0]["R² on unseen parts"] > ranked.iloc[-1]["R² on unseen parts"]


def test_a_linear_relation_comes_back_in_the_fields_own_units(ledger):
    d = la.load(ledger)
    eq = le.fit(d, LENGTH, [POWER, FOD], le.LINEAR)
    got = dict(zip(eq.terms, eq.coefficients))
    assert got[POWER] == pytest.approx(0.0004, rel=1e-3)
    assert got[FOD] == pytest.approx(0.00002, rel=1e-2)
    assert eq.intercept == pytest.approx(0.01, abs=1e-4)
    ridge = le.fit(d, LENGTH, [POWER, FOD], le.RIDGE)
    assert dict(zip(ridge.terms, ridge.coefficients))[POWER] == pytest.approx(0.0004, rel=0.1)
    assert "Power (W)" in eq.text() and eq.text().startswith(LENGTH + " = ")


def test_unseen_parts_are_whole_parts(ledger):
    """A part's flaws are never split between the fit and the test."""
    parts = np.repeat(np.array([f"p{i}" for i in range(7)]), 5)
    folds = le._folds(parts, 5)
    assert sorted(np.concatenate(folds).tolist()) == list(range(len(parts)))
    for f in folds:
        others = np.setdiff1d(np.arange(len(parts)), f)
        assert not set(parts[f]) & set(parts[others])


def test_an_input_that_never_varies_is_held_not_fitted(ledger):
    eq = le.fit(la.load(ledger), FOD, [POWER, SPEED, FREQ], le.POWER)
    assert eq.fixed == {FREQ: 50.0} and FREQ not in eq.inputs
    assert "Held the same in every row" in le.describe(eq)


def test_what_cannot_be_fitted_says_why(ledger):
    d = la.load(ledger)
    with pytest.raises(LedgerError, match="pick one or more inputs"):
        le.fit(d, FOD, [], le.LINEAR)
    with pytest.raises(LedgerError, match="none of the inputs varies"):
        le.fit(d, FOD, [FREQ], le.LINEAR)
    with pytest.raises(LedgerError, match="not number fields: Date"):
        le.fit(d, FOD, [POWER, "Date"], le.LINEAR)
    with pytest.raises(LedgerError, match="a power law needs every value above 0"):
        bad = la.load(ledger)
        bad.frame.loc[0, POWER] = "-5"
        le.fit(bad, FOD, [POWER, SPEED], le.POWER)


def test_a_quadratic_takes_four_varying_inputs_at_most(ledger, monkeypatch):
    monkeypatch.setattr(le, "MAX_QUADRATIC", 1)
    with pytest.raises(LedgerError, match="a quadratic takes 1 varying inputs at most"):
        le.fit(la.load(ledger), FOD, [POWER, SPEED], le.QUADRATIC)


def test_predicting_outside_the_data_says_so(ledger):
    eq = le.fit(la.load(ledger), FOD, [POWER, SPEED], le.POWER)
    y, out = le.predict(eq, {POWER: 40, SPEED: 200})
    assert y == pytest.approx(40, rel=0.05) and out == []
    y, out = le.predict(eq, {POWER: 80, SPEED: 200})
    assert out and out[0].startswith("Power (W) 80 is outside 20 to 50")
    curve = le.sweep(eq, POWER, 10, 60, {SPEED: 200})
    assert curve["Outside the data"].iloc[0] and not curve["Outside the data"].iloc[100]
    gx, gy, z = le.surface(eq, POWER, SPEED, (20, 50), (100, 400), n=10)
    assert z.shape == (10, 10) and z[0, -1] > z[-1, 0]


def test_a_saved_equation_is_plain_numbers_and_loads_back(ledger):
    d = la.load(ledger)
    eq = le.fit(d, FOD, [POWER, SPEED], le.POWER)
    p = le.save(eq, ledger)
    assert p == le.saved_path(ledger) and p.name == "l.equations.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw[0]["kind"] == le.POWER and "actual" not in raw[0]
    back = le.saved(ledger)[0]
    assert back.text() == eq.text()
    assert le.predict(back, {POWER: 30, SPEED: 300})[0] == pytest.approx(
        le.predict(eq, {POWER: 30, SPEED: 300})[0])
    raw.append({"kind": "__import__('os')", "schema_version": 1})
    p.write_text(json.dumps(raw), encoding="utf-8")
    assert len(le.saved(ledger)) == 1                 # anything else is skipped


def test_suggested_inputs_are_the_most_related_and_said_so(ledger):
    d = la.load(ledger)
    assert le.suggest_inputs(d, LENGTH, 2) == [POWER, FOD] or \
        set(le.suggest_inputs(d, LENGTH, 2)) == {POWER, FOD}
    eq = le.fit(d, LENGTH, le.suggest_inputs(d, LENGTH, 2), le.LINEAR, suggested=True)
    assert "flatters the score" in le.describe(eq)


def test_the_equations_page_fits_compares_predicts_and_exports(ledger):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from council_qt.tabs.ledger_analytics_dialog import AnalyticsDialog
    QApplication.instance() or QApplication([])
    dlg = AnalyticsDialog(la.load(ledger))
    pg = dlg.equations
    pg.target.setCurrentText(FOD)
    for i in range(pg.inputs.count()):
        it = pg.inputs.item(i)
        it.setCheckState(Qt.Checked if it.text() in (POWER, SPEED) else Qt.Unchecked)
    res = pg.on_compare()
    assert res.iloc[0]["Kind"] == le.POWER and pg.kind.currentText() == le.POWER
    assert pg.eq is not None and "parts it did not see" in pg.result.toPlainText()
    pg.inputs_at[POWER].setValue(80)
    y, out = pg.on_predict()
    assert out and "guessing" in pg.predict_out.text()
    from council_qt.tabs.ledger_equations_page import GRAPHS
    for g in GRAPHS:
        pg.graph.setCurrentText(g)
        pg.draw()
        assert pg.canvas is not None
    csv_out = Path(pg.on_export_values())
    assert csv_out.is_file() and csv_out.read_text(encoding="utf-8-sig").startswith(POWER)
    assert Path(pg.on_export_figure()).is_file()
    pg.on_save()
    assert pg.saved_choice.count() == 1
    pg.on_load()
    assert pg.eq.kind == le.POWER
    dlg.close()
