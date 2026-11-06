"""
The tax tables Finance ships in its data/ folder, read once and kept.

    federal(year)          brackets, standard deduction, credits, payroll
                           rates and contribution limits (federal_<year>.json)
    states(year)           every state's brackets and exemptions
                           (state_income_tax_<year>.json, Tax Foundation)
    ohio_cities()          Ohio municipal income tax rates by FIPS code
    ohio_school_districts() Ohio school district income tax by SD number
    ohio_jedds()           Ohio JEDD / JEDZ income tax rates
    years()                the years with a federal table

All of it is read from files beside this package; nothing is fetched.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

DATA = Path(__file__).resolve().parent.parent / "data"


def _read(name: str) -> Dict[str, Any]:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def years() -> List[int]:
    found = []
    for path in DATA.glob("federal_*.json"):
        try:
            found.append(int(path.stem.split("_")[1]))
        except (IndexError, ValueError):
            continue
    return sorted(found)


@lru_cache(maxsize=None)
def federal(year: int) -> Dict[str, Any]:
    path = DATA / f"federal_{int(year)}.json"
    if not path.exists():
        raise KeyError(f"no federal tax table for {year} "
                       f"(have {', '.join(map(str, years())) or 'none'})")
    return _read(path.name)


@lru_cache(maxsize=None)
def state_table(year: int) -> Dict[str, Any]:
    path = DATA / f"state_income_tax_{int(year)}.json"
    if not path.exists():
        raise KeyError(f"no state tax table for {year}")
    return _read(path.name)


def states(year: int) -> Dict[str, Dict[str, Any]]:
    return state_table(year)["states"]


def state_footnotes(year: int) -> Dict[str, str]:
    return state_table(year).get("footnotes", {})


@lru_cache(maxsize=None)
def ohio_cities() -> Dict[str, Dict[str, Any]]:
    return _read("ohio_municipal_income_tax.json")["municipalities"]


@lru_cache(maxsize=None)
def ohio_school_districts() -> Dict[str, Dict[str, Any]]:
    return _read("ohio_school_district_income_tax.json")["districts"]


@lru_cache(maxsize=None)
def ohio_jedds() -> Dict[str, Dict[str, Any]]:
    return _read("ohio_jedd_income_tax.json")["districts"]


def rate_on(entry: Dict[str, Any], year: int) -> float:
    """The rate in force on 1 July of `year` from an Ohio rate history
    (start/end as YYYYMMDD), else its current rate."""
    day = int(year) * 10000 + 701
    for span in entry.get("history") or []:
        if int(span.get("start", 0)) <= day <= int(span.get("end", 99991231)):
            return float(span.get("rate") or 0.0)
    return float(entry.get("current_rate") or 0.0)


def school_district_kind(entry: Dict[str, Any]) -> str:
    """'earned' for an earned-income school district, else 'traditional'."""
    return "earned" if "EARNED" in str(entry.get("name", "")).upper() \
        else "traditional"
