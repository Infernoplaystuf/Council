"""
Finance's paycheck engine: one household's year of pay, and each earner's
check, from a plan (a plain dict, the same one the tab saves as plan.json).

    compute(plan) -> Result

A plan:

    year              2026
    filing_status     "single" | "married_joint" | "head_of_household"
    state             two letters ("OH"); "" for none
    home_city         Ohio municipality FIPS code, or ""
    home_city_credit  fraction of a work city's tax the home city credits
                      (0..1; most Ohio cities give 1.0, some 0.5 or 0)
    school_district   Ohio school district number ("4704"), or ""
    children_under_17, other_dependents
    earners           list of:
        name, age, salary (a year), bonus (a year), pay_periods (24)
        work_city (FIPS) or work_jedd (JEDD code), or neither
        medical, dental, vision, other_pretax     per check, Section 125
        hsa_employee      per check (payroll, Section 125)
        hsa_employer      a year
        hsa_coverage      "none" | "self" | "family"
        health_fsa, dc_fsa                         a year
        k401_percent      traditional, % of pay
        roth_percent      Roth 401(k), % of pay
        match_rate, match_up_to   employer match: match_rate % of what
                          the earner puts in, on up to match_up_to % of pay
        after_tax         per check, after-tax deductions (life insurance…)

What it is, and is not: a planner. Federal tax with the standard deduction
and the child and other-dependent credits; Social Security and Medicare
(with the Additional Medicare Tax); state tax from the state's brackets,
standard deduction and exemptions; Ohio city, JEDD and school district
tax. No itemizing, AMT, self-employment, capital gains, or state credits
beyond the exemption; anything the state table footnotes is listed as not
modeled. Household taxes are shared out to each earner by their share of
the pay, then split over that earner's checks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import tables

STATUSES = ("single", "married_joint", "head_of_household")
STATUS_NAMES = {"single": "Single", "married_joint": "Married filing jointly",
                "head_of_household": "Head of household"}

#: The lines of a check, in order: (key, label, sign). Sign -1 lines are
#: taken out of pay.
LINES: Tuple[Tuple[str, str, int], ...] = (
    ("gross", "Gross pay", 1),
    ("medical", "Medical", -1),
    ("dental", "Dental", -1),
    ("vision", "Vision", -1),
    ("other_pretax", "Other pre-tax (Section 125)", -1),
    ("hsa", "HSA (you)", -1),
    ("health_fsa", "Health FSA", -1),
    ("dc_fsa", "Dependent care FSA", -1),
    ("k401", "401(k) traditional", -1),
    ("roth", "401(k) Roth", -1),
    ("federal", "Federal income tax", -1),
    ("social_security", "Social Security", -1),
    ("medicare", "Medicare", -1),
    ("state", "State income tax", -1),
    ("city", "City / JEDD tax", -1),
    ("school", "School district tax", -1),
    ("after_tax", "After-tax deductions", -1),
    ("net", "Take-home pay", 1),
)


def num(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out else default            # NaN -> default


def bracket_tax(income: float, brackets: Sequence[Mapping[str, Any]]) -> float:
    """Tax on `income` under marginal brackets [{rate, over}] (income at or
    below the first 'over' is untaxed)."""
    rows = sorted(brackets, key=lambda b: num(b.get("over")))
    tax = 0.0
    for i, row in enumerate(rows):
        low = num(row.get("over"))
        if income <= low:
            break
        high = num(rows[i + 1].get("over")) if i + 1 < len(rows) else income
        tax += num(row.get("rate")) * (min(income, high) - low)
    return max(tax, 0.0)


@dataclass
class EarnerResult:
    name: str
    pay_periods: int
    annual: Dict[str, float]
    employer: Dict[str, float]                 # match, hsa employer
    wages: Dict[str, float]                    # the bases each tax used

    def per_check(self) -> Dict[str, float]:
        n = max(self.pay_periods, 1)
        return {k: v / n for k, v in self.annual.items()}


@dataclass
class Result:
    plan: Dict[str, Any]
    earners: List[EarnerResult]
    household: Dict[str, float]               # annual totals per line
    details: Dict[str, Any]                   # taxable incomes, credits…
    warnings: List[str] = field(default_factory=list)
    not_modeled: List[str] = field(default_factory=list)


# ======================================================================
# The plan
# ======================================================================
def default_earner(name: str = "Me") -> Dict[str, Any]:
    return {"name": name, "age": 35, "salary": 0.0, "bonus": 0.0,
            "pay_periods": 24, "work_city": "", "work_jedd": "",
            "medical": 0.0, "dental": 0.0, "vision": 0.0,
            "other_pretax": 0.0, "hsa_employee": 0.0, "hsa_employer": 0.0,
            "hsa_coverage": "none", "health_fsa": 0.0, "dc_fsa": 0.0,
            "k401_percent": 0.0, "roth_percent": 0.0, "match_rate": 0.0,
            "match_up_to": 0.0, "after_tax": 0.0}


def default_plan() -> Dict[str, Any]:
    have = tables.years()
    return {"year": have[-1] if have else 2026, "filing_status": "single",
            "state": "OH", "home_city": "", "home_city_credit": 1.0,
            "school_district": "", "children_under_17": 0,
            "other_dependents": 0, "earners": [default_earner()]}


def normalized(plan: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """The plan with every field present and of the right kind."""
    base = default_plan()
    if isinstance(plan, Mapping):
        for key in base:
            if key in plan and key != "earners":
                base[key] = plan[key]
        earners = plan.get("earners")
        if isinstance(earners, list) and earners:
            base["earners"] = []
            for i, e in enumerate(earners):
                one = default_earner(f"Earner {i + 1}")
                if isinstance(e, Mapping):
                    one.update({k: e[k] for k in one if k in e})
                base["earners"].append(one)
    if base["filing_status"] not in STATUSES:
        base["filing_status"] = "single"
    base["year"] = int(num(base["year"], 2026))
    base["state"] = str(base["state"] or "").upper()
    base["home_city_credit"] = min(max(num(base["home_city_credit"], 1.0),
                                       0.0), 1.0)
    for key in ("children_under_17", "other_dependents"):
        base[key] = max(int(num(base[key])), 0)
    for e in base["earners"]:
        e["pay_periods"] = max(int(num(e["pay_periods"], 24)), 1)
        e["age"] = int(num(e["age"], 35))
        for key, value in list(e.items()):
            if key not in ("name", "work_city", "work_jedd", "hsa_coverage",
                           "pay_periods", "age"):
                e[key] = max(num(value), 0.0)
        e["name"] = str(e["name"] or "Earner")
        e["work_city"] = str(e["work_city"] or "")
        e["work_jedd"] = str(e["work_jedd"] or "")
        if e["hsa_coverage"] not in ("none", "self", "family"):
            e["hsa_coverage"] = "none"
    return base


# ======================================================================
# The sums
# ======================================================================
def _earner_pay(e: Mapping[str, Any]) -> Dict[str, float]:
    n = e["pay_periods"]
    gross = e["salary"] + e["bonus"]
    k401 = gross * e["k401_percent"] / 100.0
    roth = gross * e["roth_percent"] / 100.0
    section125 = {
        "medical": e["medical"] * n, "dental": e["dental"] * n,
        "vision": e["vision"] * n, "other_pretax": e["other_pretax"] * n,
        "hsa": e["hsa_employee"] * n, "health_fsa": e["health_fsa"],
        "dc_fsa": e["dc_fsa"]}
    s125 = sum(section125.values())
    medicare_wages = max(gross - s125, 0.0)
    federal_wages = max(gross - s125 - k401, 0.0)
    put_in = (k401 + roth) / gross * 100.0 if gross else 0.0
    match = gross * min(put_in, e["match_up_to"]) / 100.0 \
        * e["match_rate"] / 100.0
    return {"gross": gross, "k401": k401, "roth": roth, **section125,
            "medicare_wages": medicare_wages, "federal_wages": federal_wages,
            "match": match, "after_tax": e["after_tax"] * n}


def _federal(plan, pays, fed, details) -> float:
    status = plan["filing_status"]
    agi = sum(p["federal_wages"] for p in pays)
    taxable = max(agi - num(fed["standard_deduction"][status]), 0.0)
    tax = bracket_tax(taxable, fed["brackets"][status])
    ctc = fed["child_tax_credit"]
    kids, others = plan["children_under_17"], plan["other_dependents"]
    credit = kids * num(ctc["per_child"]) + others * num(ctc["other_dependent"])
    over = max(agi - num(ctc["phaseout_start"][status]), 0.0)
    steps = -(-over // 1000) if over else 0              # each $1,000 or part
    credit = max(credit - steps * num(ctc["phaseout_per_1000"]), 0.0)
    used = min(credit, tax)
    # The refundable part (ACTC) is only for the children's share.
    child_left = min(credit - used, kids * num(ctc["per_child"]))
    earned = sum(p["federal_wages"] for p in pays)
    refundable = min(child_left, kids * num(ctc["refundable_per_child"]),
                     num(ctc["refundable_rate"])
                     * max(earned - num(ctc["refundable_earned_floor"]), 0.0))
    details.update({"federal_agi": agi, "federal_taxable": taxable,
                    "federal_before_credits": tax,
                    "dependent_credits": used,
                    "refundable_child_credit": refundable})
    return tax - used - refundable


def _ohio_exemption(agi: float) -> float:
    """Ohio's exemption per person by AGI (Tax Foundation footnote dd)."""
    if agi <= 40000:
        return 2400.0
    if agi <= 80000:
        return 2150.0
    if agi < 500000:
        return 1900.0
    return 0.0


def _state(plan, pays, year, details, not_modeled) -> float:
    code = plan["state"]
    if not code:
        return 0.0
    try:
        row = tables.states(year).get(code)
    except KeyError as exc:
        not_modeled.append(str(exc))
        return 0.0
    if row is None:
        not_modeled.append(f"No state table for {code}.")
        return 0.0
    if not row.get("wage_income_tax", True) or row.get("remark"):
        details["state_taxable"] = 0.0
        return 0.0
    joint = plan["filing_status"] == "married_joint"
    agi = sum(p["federal_wages"] for p in pays)
    deduction = num((row.get("standard_deduction") or {}).get(
        "joint" if joint else "single"))
    exemptions = row.get("personal_exemption") or {}
    dependents = plan["children_under_17"] + plan["other_dependents"]
    tax_credit = 0.0
    if code == "OH":
        people = (2 if joint else 1) + dependents
        exempt = people * _ohio_exemption(agi)
    else:
        exempt = 0.0
        for value, count in ((exemptions.get("joint" if joint else "single"),
                              1), (exemptions.get("dependent"), dependents)):
            if isinstance(value, Mapping):
                tax_credit += num(value.get("credit")) * count
            else:
                exempt += num(value) * count
    taxable = max(agi - deduction - exempt, 0.0)
    brackets = row.get("married_joint" if joint else "single") or []
    tax = max(bracket_tax(taxable, brackets) - tax_credit, 0.0)
    details.update({"state_agi": agi, "state_taxable": taxable,
                    "state_exemptions": exempt,
                    "state_exemption_credits": tax_credit})
    notes = tables.state_footnotes(year)
    for mark in row.get("notes") or []:
        # (a) is the table's note that local taxes are left out: they are
        # modeled here (Ohio) or said not to be.
        if mark in notes and mark != "a" and \
                not (code == "OH" and mark == "dd"):
            not_modeled.append(f"{code} ({mark}): {notes[mark]}")
    return tax


def _local(plan, e, pay, year, warnings) -> Tuple[float, float, float]:
    """(work-place tax, home-city tax after credit, school district tax)
    for one earner, all Ohio. The school district tax is per earner on
    earned income; a traditional district's tax is worked out per
    household by the caller and returned here as 0."""
    wages = pay["medicare_wages"]
    cities, jedds = tables.ohio_cities(), tables.ohio_jedds()
    work_tax = 0.0
    if e["work_jedd"]:
        entry = jedds.get(e["work_jedd"])
        if entry is None:
            warnings.append(f"{e['name']}: JEDD {e['work_jedd']} is not in "
                            "the Ohio table.")
        else:
            work_tax = tables.rate_on(entry, year) * wages
    elif e["work_city"]:
        entry = cities.get(e["work_city"])
        if entry is None:
            warnings.append(f"{e['name']}: city {e['work_city']} is not in "
                            "the Ohio table.")
        else:
            work_tax = tables.rate_on(entry, year) * wages
    home_tax = 0.0
    home = plan["home_city"]
    if home and home in cities:
        if not e["work_jedd"] and e["work_city"] == home:
            home_tax = 0.0                       # one tax, already counted
        else:
            owed = tables.rate_on(cities[home], year) * wages
            credit = plan["home_city_credit"] * min(work_tax, owed)
            home_tax = max(owed - credit, 0.0)
    elif home:
        warnings.append(f"Home city {home} is not in the Ohio table.")
    school = 0.0
    sd = plan["school_district"]
    entry = tables.ohio_school_districts().get(sd) if sd else None
    if entry is not None and tables.school_district_kind(entry) == "earned":
        school = tables.rate_on(entry, year) * pay["federal_wages"]
    return work_tax, home_tax, school


def _limits(plan, e, pay, fed, warnings) -> None:
    lim = fed["limits"]
    age = e["age"]
    k_limit = num(lim["k401_deferral"])
    if 60 <= age <= 63:
        k_limit += num(lim["k401_catchup_60_63"])
    elif age >= 50:
        k_limit += num(lim["k401_catchup_50"])
    put_in = pay["k401"] + pay["roth"]
    if put_in > k_limit + 0.005:
        warnings.append(f"{e['name']}: 401(k) ${put_in:,.0f} is over the "
                        f"${k_limit:,.0f} limit for age {age}.")
    if e["hsa_coverage"] == "none" and (pay["hsa"] or e["hsa_employer"]):
        warnings.append(f"{e['name']}: HSA money is set but HSA coverage is "
                        "none; an HSA needs a high-deductible plan.")
    elif e["hsa_coverage"] != "none":
        h_limit = num(lim["hsa_family" if e["hsa_coverage"] == "family"
                          else "hsa_self"])
        if age >= 55:
            h_limit += num(lim["hsa_catchup_55"])
        total = pay["hsa"] + e["hsa_employer"]
        if total > h_limit + 0.005:
            warnings.append(f"{e['name']}: HSA ${total:,.0f} (you and your "
                            f"employer) is over the ${h_limit:,.0f} limit.")
        if e["health_fsa"]:
            warnings.append(f"{e['name']}: a general health FSA stops HSA "
                            "contributions; only a limited-purpose (dental "
                            "and vision) FSA goes with an HSA.")
    if e["health_fsa"] > num(lim["health_fsa"]) + 0.005:
        warnings.append(f"{e['name']}: health FSA ${e['health_fsa']:,.0f} is "
                        f"over the ${num(lim['health_fsa']):,.0f} limit.")
    if e["match_up_to"] and e["match_rate"]:
        put_pct = put_in / pay["gross"] * 100.0 if pay["gross"] else 0.0
        if put_pct + 1e-9 < e["match_up_to"]:
            missed = pay["gross"] * (e["match_up_to"] - put_pct) / 100.0 \
                * e["match_rate"] / 100.0
            warnings.append(f"{e['name']}: putting in {put_pct:.1f}% leaves "
                            f"${missed:,.0f} of employer match unclaimed "
                            f"(the match goes up to {e['match_up_to']:g}%).")


def compute(plan: Optional[Mapping[str, Any]]) -> Result:
    plan = normalized(plan)
    year = plan["year"]
    fed = tables.federal(year)
    warnings: List[str] = []
    not_modeled: List[str] = []
    details: Dict[str, Any] = {"year": year}
    status = plan["filing_status"]
    earners = plan["earners"]
    if status == "single" and len(earners) > 1:
        warnings.append("Filing single with more than one earner: each "
                        "earner after the first is taxed as if on this "
                        "return. Use married filing jointly for a couple.")
    pays = [_earner_pay(e) for e in earners]

    federal = _federal(plan, pays, fed, details)
    state = _state(plan, pays, year, details, not_modeled)

    # Payroll taxes, per earner (Additional Medicare is a household sum).
    ss_rate, ss_base = num(fed["social_security"]["rate"]), \
        num(fed["social_security"]["wage_base"])
    med = fed["medicare"]
    ss = [ss_rate * min(p["medicare_wages"], ss_base) for p in pays]
    medicare = [num(med["rate"]) * p["medicare_wages"] for p in pays]
    all_medicare = sum(p["medicare_wages"] for p in pays)
    extra = num(med["additional_rate"]) * max(
        all_medicare - num(med["additional_over"][status]), 0.0)
    details["additional_medicare"] = extra
    if extra and status == "married_joint" and len(pays) > 1 and \
            all(p["medicare_wages"] <= num(med["withholding_over"])
                for p in pays):
        warnings.append(f"Additional Medicare Tax of ${extra:,.0f} is owed "
                        "on the joint return but no employer withholds it "
                        "(neither earner is over $200,000): plan to pay it "
                        "at tax time.")

    # Ohio local taxes.
    local = [_local(plan, e, p, year, warnings) if plan["state"] == "OH"
             else (0.0, 0.0, 0.0) for e, p in zip(earners, pays)]
    if plan["state"] != "OH" and (plan["home_city"] or plan["school_district"]
                                  or any(e["work_city"] or e["work_jedd"]
                                         for e in earners)):
        not_modeled.append("City and school district taxes are only modeled "
                           "for Ohio.")
    school_household = 0.0
    sd = plan["school_district"]
    sd_entry = tables.ohio_school_districts().get(sd) \
        if sd and plan["state"] == "OH" else None
    if sd and plan["state"] == "OH" and sd_entry is None:
        warnings.append(f"School district {sd} is not in the Ohio table.")
    if sd_entry is not None and \
            tables.school_district_kind(sd_entry) == "traditional":
        school_household = tables.rate_on(sd_entry, year) * \
            num(details.get("state_taxable"))

    # Share the household taxes out by pay.
    total_wages = sum(p["federal_wages"] for p in pays)
    shares = [p["federal_wages"] / total_wages if total_wages else
              1.0 / len(pays) for p in pays]
    results: List[EarnerResult] = []
    household: Dict[str, float] = {key: 0.0 for key, _l, _s in LINES}
    for i, (e, p) in enumerate(zip(earners, pays)):
        work_tax, home_tax, school = local[i]
        annual = {
            "gross": p["gross"], "medical": p["medical"],
            "dental": p["dental"], "vision": p["vision"],
            "other_pretax": p["other_pretax"], "hsa": p["hsa"],
            "health_fsa": p["health_fsa"], "dc_fsa": p["dc_fsa"],
            "k401": p["k401"], "roth": p["roth"],
            "federal": federal * shares[i],
            "social_security": ss[i],
            "medicare": medicare[i] + extra * shares[i],
            "state": state * shares[i],
            "city": work_tax + home_tax,
            "school": school + school_household * shares[i],
            "after_tax": p["after_tax"]}
        annual["net"] = annual["gross"] - sum(
            v for k, v in annual.items() if k != "gross")
        if annual["net"] < 0:
            warnings.append(f"{e['name']}: deductions are more than pay.")
        _limits(plan, e, p, fed, warnings)
        results.append(EarnerResult(
            name=e["name"], pay_periods=e["pay_periods"], annual=annual,
            employer={"match": p["match"], "hsa": e["hsa_employer"]},
            wages={"federal": p["federal_wages"],
                   "medicare": p["medicare_wages"]}))
        for key, value in annual.items():
            household[key] += value
    dc_total = sum(e["dc_fsa"] for e in earners)
    dc_limit = num(fed["limits"]["dependent_care_fsa"])
    if dc_total > dc_limit + 0.005:
        warnings.append(f"Dependent care FSA ${dc_total:,.0f} for the "
                        f"household is over the ${dc_limit:,.0f} limit.")
    not_modeled.append("Federal: standard deduction only (no itemizing, "
                       "AMT, self-employment or investment income).")
    return Result(plan=plan, earners=results, household=household,
                  details=details, warnings=warnings,
                  not_modeled=not_modeled)
