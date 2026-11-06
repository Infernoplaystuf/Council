"""
Finance's projection: the plan carried forward year by year, with what goes
into each account and what each account grows to.

    project(plan) -> Projection

The settings live in plan["projection"]:

    years              how many years to show (1..70), from the plan's year
    raise_percent      each earner's pay rises this much a year
    inflation_percent  prices rise this much a year; tax brackets, limits
                       and the fixed dollar amounts in the plan (insurance,
                       HSA and FSA dollars, after-tax deductions, extras)
                       rise with it
    return_percent     the growth an account gets when it has none of its own
    accounts           list of:
        name, kind (see KINDS), owner (earner index, or -1 for the
        household), balance (today), return_percent (blank: the default),
        extra_per_year (put in from take-home pay, in today's dollars)

Pay goes in by itself: an earner's traditional 401(k) and employer match go
to their first "k401" account, their Roth 401(k) to their first
"roth401k", their HSA and the employer's HSA money to their first "hsa".
An account is added (balance 0) when pay goes to a kind they have none of.
An earner's pay, and so their payroll contributions, stop the year they
reach their retirement age.

How the years are taxed: each year is worked out in today's dollars (pay
grows by the raise over inflation; everything else holds still) with
today's tax tables, then turned back into that year's dollars. That is
the same as tax brackets and limits rising with inflation, which is how
the law indexes most of them.

Growth: an account's balance grows a full year; the year's money goes in
across the year, so it gets half a year's growth.

Not modeled yet: withdrawals in retirement, Social Security benefits,
required minimum distributions, taxes on brokerage gains, family expenses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

from . import paycheck, tables
from .paycheck import num

KINDS: Dict[str, str] = {
    "k401": "401(k) traditional",
    "roth401k": "401(k) Roth",
    "hsa": "HSA",
    "ira": "Traditional IRA",
    "roth_ira": "Roth IRA",
    "brokerage": "Brokerage",
    "savings": "Savings / cash",
}
#: The kinds pay goes into by itself, and which plan line feeds them.
PAYROLL_KINDS = ("k401", "roth401k", "hsa")

#: The plan's fixed dollar amounts (they rise with inflation, so in
#: today's dollars they stay put; pay is the one that moves).
PAY_FIELDS = ("salary", "bonus")

MAX_YEARS = 70


def default_settings() -> Dict[str, Any]:
    return {"years": 30, "raise_percent": 3.0, "inflation_percent": 2.5,
            "return_percent": 6.0, "accounts": []}


def default_account(kind: str = "k401", owner: int = 0,
                    name: str = "") -> Dict[str, Any]:
    return {"name": name or KINDS.get(kind, kind), "kind": kind,
            "owner": owner, "balance": 0.0, "return_percent": None,
            "extra_per_year": 0.0}


def settings_of(plan: Mapping[str, Any]) -> Dict[str, Any]:
    """plan["projection"] made whole: every field there and in range, and
    an account for every kind of account the plan's pay goes into."""
    raw = plan.get("projection") if isinstance(plan, Mapping) else None
    raw = raw if isinstance(raw, Mapping) else {}
    out = default_settings()
    out["years"] = min(max(int(num(raw.get("years"), 30)), 1), MAX_YEARS)
    for key, low, high in (("raise_percent", -20.0, 50.0),
                           ("inflation_percent", -10.0, 50.0),
                           ("return_percent", -50.0, 50.0)):
        out[key] = min(max(num(raw.get(key), out[key]), low), high)
    earners = plan.get("earners") or []
    accounts = []
    for a in raw.get("accounts") or []:
        if not isinstance(a, Mapping):
            continue
        one = default_account()
        one.update({k: a[k] for k in one if k in a})
        if one["kind"] not in KINDS:
            one["kind"] = "savings"
        owner = int(num(one["owner"], 0))
        one["owner"] = owner if -1 <= owner < len(earners) else -1
        one["balance"] = max(num(one["balance"]), 0.0)
        one["extra_per_year"] = max(num(one["extra_per_year"]), 0.0)
        r = one["return_percent"]
        one["return_percent"] = None if r in (None, "") else \
            min(max(num(r), -50.0), 50.0)
        one["name"] = str(a.get("name") or KINDS[one["kind"]])
        accounts.append(one)
    for i, e in enumerate(earners):
        feeds = {"k401": num(e.get("k401_percent")) > 0
                 or num(e.get("match_rate")) > 0,
                 "roth401k": num(e.get("roth_percent")) > 0,
                 "hsa": num(e.get("hsa_employee")) > 0
                 or num(e.get("hsa_employer")) > 0}
        for kind, fed in feeds.items():
            if fed and not any(a["kind"] == kind and a["owner"] == i
                               for a in accounts):
                who = str(e.get("name") or f"Earner {i + 1}")
                accounts.append(default_account(
                    kind, i, f"{who}'s {KINDS[kind]}"))
    out["accounts"] = accounts
    return out


@dataclass
class YearRow:
    index: int                      # 0 = the plan's year
    year: int
    ages: List[int]
    working: List[bool]
    gross: float
    take_home: float
    from_pay: float                 # what the earners put in through pay
    employer: float                 # match and employer HSA
    extra: float                    # put in from take-home
    left: float                     # take-home less the extras
    put_in: List[float]             # per account, this year
    balances: List[float]           # per account, end of this year
    growth: float                   # what the accounts earned this year
    total: float
    total_today: float              # total in the plan year's dollars
    taxes: float


@dataclass
class Projection:
    settings: Dict[str, Any]
    accounts: List[Dict[str, Any]]
    rows: List[YearRow]
    notes: List[str] = field(default_factory=list)

    @property
    def final(self) -> Optional[YearRow]:
        return self.rows[-1] if self.rows else None


TAX_LINES = ("federal", "social_security", "medicare", "state", "city",
             "school")


def _payroll_target(accounts, kind: str, owner: int) -> Optional[int]:
    for i, a in enumerate(accounts):
        if a["kind"] == kind and a["owner"] == owner:
            return i
    return None


def project(plan: Optional[Mapping[str, Any]]) -> Projection:
    plan = paycheck.normalized(plan)
    settings = settings_of(plan)
    accounts = settings["accounts"]
    raise_ = settings["raise_percent"] / 100.0
    infl = settings["inflation_percent"] / 100.0
    default_r = settings["return_percent"] / 100.0
    returns = [default_r if a["return_percent"] is None
               else a["return_percent"] / 100.0 for a in accounts]
    balances = [a["balance"] for a in accounts]
    real_growth = (1.0 + raise_) / (1.0 + infl) if infl > -1 else 1.0
    rows: List[YearRow] = []
    notes: List[str] = []
    short_years: List[int] = []
    for y in range(settings["years"]):
        f = (1.0 + infl) ** y                 # today's dollars -> year y's
        ages = [e["age"] + y for e in plan["earners"]]
        working = [age < e["retire_age"]
                   for age, e in zip(ages, plan["earners"])]
        this = dict(plan)
        this["earners"] = []
        for e, on in zip(plan["earners"], working):
            one = dict(e)
            if on:
                for key in PAY_FIELDS:
                    one[key] = e[key] * real_growth ** y
            else:
                for key in list(one):
                    if isinstance(one[key], float):
                        one[key] = 0.0
            this["earners"].append(one)
        result = paycheck.compute(this)
        put_in = [0.0] * len(accounts)
        from_pay = employer = 0.0
        for i, (er, e) in enumerate(zip(result.earners, this["earners"])):
            a = er.annual
            for kind, mine, theirs in (
                    ("k401", a["k401"], er.employer["match"]),
                    ("roth401k", a["roth"], 0.0),
                    ("hsa", a["hsa"], er.employer["hsa"])):
                if not (mine or theirs):
                    continue
                target = _payroll_target(accounts, kind, i)
                if target is not None:
                    put_in[target] += (mine + theirs) * f
                from_pay += mine * f
                employer += theirs * f
        extra = 0.0
        for i, a in enumerate(accounts):
            owner = a["owner"]
            if owner >= 0 and owner < len(working) and not working[owner]:
                continue                      # extras stop with their pay
            put_in[i] += a["extra_per_year"] * f
            extra += a["extra_per_year"] * f
        growth = 0.0
        for i in range(len(accounts)):
            start = balances[i]
            end = start * (1.0 + returns[i]) + \
                put_in[i] * (1.0 + returns[i]) ** 0.5
            growth += end - start - put_in[i]
            balances[i] = end
        take_home = result.household["net"] * f
        left = take_home - extra
        if left < 0:
            short_years.append(plan["year"] + y)
        total = sum(balances)
        rows.append(YearRow(
            index=y, year=plan["year"] + y, ages=ages, working=working,
            gross=result.household["gross"] * f, take_home=take_home,
            from_pay=from_pay, employer=employer, extra=extra, left=left,
            put_in=put_in, balances=list(balances), growth=growth,
            total=total, total_today=total / f if f else total,
            taxes=sum(result.household[k] for k in TAX_LINES) * f))
    if short_years:
        notes.append("The extras put in from take-home pay are more than "
                     "the take-home pay in " +
                     _years_text(short_years) + ".")
    lim = tables.federal(plan["year"])["limits"]
    for a in accounts:
        if a["kind"] not in ("ira", "roth_ira") or not a["extra_per_year"]:
            continue
        cap = num(lim["ira"])
        owner = a["owner"]
        if 0 <= owner < len(plan["earners"]) and \
                plan["earners"][owner]["age"] >= 50:
            cap += num(lim["ira_catchup_50"])
        if a["extra_per_year"] > cap + 0.005:
            notes.append(f"{a['name']}: ${a['extra_per_year']:,.0f} a year "
                         f"is over the ${cap:,.0f} IRA limit (a person's "
                         "traditional and Roth IRAs share one limit).")
    for e in plan["earners"]:
        if e["age"] < e["retire_age"] <= e["age"] + settings["years"] - 1:
            notes.append(f"{e['name']} retires at {e['retire_age']} in "
                         f"{plan['year'] + e['retire_age'] - e['age']}: pay "
                         "and contributions stop; balances keep growing.")
    notes.append("Not modeled yet: withdrawals in retirement, Social "
                 "Security benefits, required minimum distributions, tax "
                 "on brokerage gains, family expenses.")
    return Projection(settings=settings, accounts=accounts, rows=rows,
                      notes=notes)


def _years_text(years: List[int]) -> str:
    if len(years) <= 3:
        return ", ".join(map(str, years))
    return f"{years[0]}–{years[-1]} ({len(years)} years)"
