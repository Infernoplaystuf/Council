"""
The Finance plugin (council_plugins/finance, id council.finance):

  * it is built in, listed, and OFF until ticked, like every plugin;
  * its engine works a year of pay out by hand-checkable sums: federal tax
    (2026 brackets, standard deduction, child and dependent credits),
    Social Security to the wage base, Medicare and the Additional Medicare
    Tax, the state's brackets and exemptions, and Ohio city, JEDD and
    school district tax with the home city's credit;
  * it says when a limit is passed or a match is left unclaimed;
  * its tab builds offscreen from the plugin folder, and what is typed is
    saved in the plugin's own data folder and read back next time.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

from council_core import app_plugins as ap  # noqa: E402
from council_plugins.finance.engine import paycheck, tables  # noqa: E402

COLUMBUS, DUBLIN = "18000", "22694"


@pytest.fixture(autouse=True)
def app_dir(tmp_path, monkeypatch):
    home = tmp_path / "app"
    monkeypatch.setenv("COUNCIL_APP_DIR", str(home))
    return home


def earner(**over):
    e = paycheck.default_earner()
    e.update(over)
    return e


def plan(*earners, **over):
    p = paycheck.default_plan()
    p["earners"] = list(earners) or [earner()]
    p.update(over)
    return p


# ------------------------------------------------------------- the plugin
def test_finance_is_built_in_and_starts_off():
    (found,) = [f for f in ap.discover() if f.id == "council.finance"]
    assert found.builtin and found.can_turn_on, found.problem
    assert found.manifest.network == "none"
    assert found.manifest.kind == "tab"
    assert ap.switched_on([found]) == []


def test_the_tables_ship_with_the_plugin():
    assert 2026 in tables.years()
    assert len(tables.states(2026)) == 51
    assert tables.ohio_cities()[COLUMBUS]["name"] == "COLUMBUS"
    fed = tables.federal(2026)
    assert fed["standard_deduction"]["single"] == 16100
    assert fed["social_security"]["wage_base"] == 184500
    assert fed["limits"]["k401_deferral"] == 24500
    with pytest.raises(KeyError):
        tables.federal(1990)


# ------------------------------------------------------------- the sums
def test_brackets_are_marginal():
    rows = [{"rate": 0.1, "over": 0}, {"rate": 0.2, "over": 100}]
    assert paycheck.bracket_tax(50, rows) == pytest.approx(5)
    assert paycheck.bracket_tax(150, rows) == pytest.approx(20)
    # Income at or below the first 'over' is untaxed (Ohio's 26,050).
    assert paycheck.bracket_tax(20000, [{"rate": 0.0275, "over": 26050}]) == 0


def test_a_single_ohio_earner_by_hand():
    r = paycheck.compute(plan(earner(salary=80000, medical=100,
                                     k401_percent=6)))
    a = r.earners[0].annual
    # Federal wages 80,000 - 2,400 medical - 4,800 401(k) = 72,800;
    # taxable 72,800 - 16,100 = 56,700 -> 1,240 + 4,560 + 22% of 6,300.
    assert a["federal"] == pytest.approx(7186.0)
    # FICA wages keep the 401(k): 77,600.
    assert a["social_security"] == pytest.approx(77600 * 0.062)
    assert a["medicare"] == pytest.approx(77600 * 0.0145)
    # Ohio: AGI 72,800 -> exemption 2,150; 2.75% over 26,050.
    assert a["state"] == pytest.approx((72800 - 2150 - 26050) * 0.0275)
    assert a["net"] == pytest.approx(80000 - 2400 - 4800 - 7186.0
                                     - 77600 * 0.0765
                                     - (72800 - 2150 - 26050) * 0.0275)
    assert r.earners[0].per_check()["net"] == pytest.approx(a["net"] / 24)


def test_a_married_couple_with_children_by_hand():
    r = paycheck.compute(plan(earner(name="A", salary=120000),
                              earner(name="B", salary=60000),
                              filing_status="married_joint",
                              children_under_17=2))
    # Taxable 180,000 - 32,200 = 147,800 -> 11,600 + 22% of 47,000;
    # less two $2,200 child credits.
    assert r.household["federal"] == pytest.approx(21940 - 4400)
    # Shared by pay: two thirds and one third.
    assert r.earners[0].annual["federal"] == pytest.approx(17540 * 2 / 3)
    # Ohio: four exemptions of 1,900 (AGI between 80,000 and 500,000).
    assert r.household["state"] == pytest.approx(
        (180000 - 7600 - 26050) * 0.0275)


def test_the_refundable_child_credit_can_go_below_zero():
    r = paycheck.compute(plan(earner(salary=30000),
                              filing_status="head_of_household",
                              children_under_17=2, state="TX"))
    # Taxable 5,850 -> 585 owed, all cancelled; then up to 1,700 a child back.
    assert r.details["refundable_child_credit"] == pytest.approx(3400)
    assert r.household["federal"] == pytest.approx(-3400)
    assert r.household["state"] == 0


def test_social_security_stops_at_the_wage_base_and_extra_medicare_starts():
    r = paycheck.compute(plan(earner(salary=250000), state=""))
    a = r.earners[0].annual
    assert a["social_security"] == pytest.approx(184500 * 0.062)
    assert a["medicare"] == pytest.approx(250000 * 0.0145 + 50000 * 0.009)


def test_ohio_city_tax_and_the_home_city_credit():
    wages = 100000
    work = earner(salary=wages, work_city=COLUMBUS)
    full = paycheck.compute(plan(work, home_city=DUBLIN,
                                 home_city_credit=1.0))
    assert full.earners[0].annual["city"] == pytest.approx(wages * 0.025)
    half = paycheck.compute(plan(work, home_city=DUBLIN,
                                 home_city_credit=0.5))
    # Dublin wants 2%; it credits half of the lesser of 2.5% and 2%.
    assert half.earners[0].annual["city"] == pytest.approx(
        wages * 0.025 + wages * (0.02 - 0.5 * 0.02))
    home_only = paycheck.compute(plan(earner(salary=wages),
                                      home_city=DUBLIN))
    assert home_only.earners[0].annual["city"] == pytest.approx(wages * 0.02)
    same = paycheck.compute(plan(earner(salary=wages, work_city=DUBLIN),
                                 home_city=DUBLIN))
    assert same.earners[0].annual["city"] == pytest.approx(wages * 0.02)


def test_a_jedd_is_taxed_in_place_of_a_city():
    code, entry = next((k, v) for k, v in tables.ohio_jedds().items()
                       if v.get("current_rate"))
    r = paycheck.compute(plan(earner(salary=50000, work_jedd=code)))
    assert r.earners[0].annual["city"] == pytest.approx(
        50000 * tables.rate_on(entry, 2026))


def test_school_district_tax_uses_the_right_base():
    districts = tables.ohio_school_districts()
    trad = next(k for k, v in districts.items() if v.get("current_rate")
                and tables.school_district_kind(v) == "traditional")
    earned = next(k for k, v in districts.items() if v.get("current_rate")
                  and tables.school_district_kind(v) == "earned")
    r = paycheck.compute(plan(earner(salary=70000, k401_percent=10),
                              school_district=trad))
    assert r.household["school"] == pytest.approx(
        tables.rate_on(districts[trad], 2026) * r.details["state_taxable"])
    r = paycheck.compute(plan(earner(salary=70000, k401_percent=10),
                              school_district=earned))
    assert r.household["school"] == pytest.approx(
        tables.rate_on(districts[earned], 2026) * 63000)


def test_a_state_with_credit_exemptions_takes_them_off_the_tax():
    row = tables.states(2026)["CA"]
    credit = row["personal_exemption"]["single"]["credit"]
    r = paycheck.compute(plan(earner(salary=60000), state="CA"))
    taxable = 60000 - row["standard_deduction"]["single"]
    assert r.household["state"] == pytest.approx(
        paycheck.bracket_tax(taxable, row["single"]) - credit)


def test_no_income_tax_state_and_others_not_modeled_locally():
    r = paycheck.compute(plan(earner(salary=60000), state="TX",
                              home_city=DUBLIN))
    assert r.household["state"] == 0 and r.household["city"] == 0
    assert any("only modeled for Ohio" in n for n in r.not_modeled)


# ------------------------------------------------------------- warnings
def test_limits_and_conflicts_are_flagged():
    r = paycheck.compute(plan(earner(salary=200000, k401_percent=15,
                                     hsa_coverage="self", hsa_employee=200,
                                     hsa_employer=1000, health_fsa=3500,
                                     dc_fsa=8000)))
    text = "\n".join(r.warnings)
    assert "401(k) $30,000 is over the $24,500 limit" in text
    assert "HSA $5,800" in text and "$4,400 limit" in text
    assert "general health FSA stops HSA" in text
    assert "health FSA $3,500 is over" in text
    assert "Dependent care FSA $8,000" in text


def test_catch_up_raises_the_401k_limit():
    young = paycheck.compute(plan(earner(salary=200000, k401_percent=15)))
    older = paycheck.compute(plan(earner(salary=200000, k401_percent=15,
                                         age=61)))
    assert any("401(k)" in w for w in young.warnings)
    assert not any("401(k)" in w for w in older.warnings)


def test_an_unclaimed_match_is_flagged_and_the_match_counted():
    r = paycheck.compute(plan(earner(salary=100000, k401_percent=3,
                                     match_rate=100, match_up_to=5)))
    assert r.earners[0].employer["match"] == pytest.approx(3000)
    assert any("$2,000 of employer match unclaimed" in w for w in r.warnings)


def test_a_bad_plan_is_made_whole():
    p = paycheck.normalized({"filing_status": "nonsense", "earners": [
        {"salary": "abc", "pay_periods": 0, "hsa_coverage": "x"}]})
    assert p["filing_status"] == "single"
    e = p["earners"][0]
    assert e["salary"] == 0 and e["pay_periods"] == 1
    assert e["hsa_coverage"] == "none"
    paycheck.compute(p)


# ------------------------------------------------------------- the tab
@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _finance():
    (found,) = [f for f in ap.discover() if f.id == "council.finance"]
    return found


def _rows(tab):
    out = {}
    for i in range(tab.table.topLevelItemCount()):
        top = tab.table.topLevelItem(i)
        for j in range(top.childCount()):
            child = top.child(j)
            out.setdefault(child.text(0), (child.text(1), child.text(2)))
    return out


def test_the_tab_builds_saves_and_reads_back(qapp, app_dir):
    from council_qt import plugin_tabs

    tab = plugin_tabs.build_page(_finance())
    assert type(tab).__name__ == "FinanceTab"
    fields = tab.earners.widget(0).fields
    fields["salary"][0].setValue(80000)
    fields["medical"][0].setValue(100)
    fields["k401_percent"][0].setValue(6)
    rows = _rows(tab)
    assert rows["Federal income tax"] == ("-$299.42", "-$7,186.00")
    assert rows["Gross pay"] == ("$3,333.33", "$80,000.00")
    tab.save()
    saved = json.loads((app_dir / "plugin_data" / "council.finance"
                        / "plan.json").read_text(encoding="utf-8"))
    assert saved["earners"][0]["salary"] == 80000

    again = plugin_tabs.build_page(_finance())
    assert again.earners.widget(0).fields["salary"][0].value() == 80000
    assert _rows(again)["Federal income tax"] == rows["Federal income tax"]


def test_a_second_earner_switches_to_joint_and_shows_the_household(qapp):
    from council_qt import plugin_tabs

    tab = plugin_tabs.build_page(_finance())
    tab.on_add_earner()
    assert tab.earners.count() == 2
    assert tab.status.currentData() == "married_joint"
    tops = [tab.table.topLevelItem(i).text(0)
            for i in range(tab.table.topLevelItemCount())]
    assert tops[-1] == "Household"
    tab.on_remove_earner()
    assert tab.earners.count() == 1
