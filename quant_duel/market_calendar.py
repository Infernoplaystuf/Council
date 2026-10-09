"""NYSE trading days, from the exchange's holiday rules — no package needed.

Full-day closures only. Early closes (13:00 on some holiday eves) are still
trading days with a close, which is all a daily model needs. One-off
closures (national days of mourning, e.g. 2018-12-05, 2025-01-09) are in
``SPECIAL_CLOSURES``; add new ones there.
"""
from __future__ import annotations

import datetime as dt
from functools import lru_cache
from typing import List, Set

import pandas as pd

SPECIAL_CLOSURES = {
    dt.date(2012, 10, 29), dt.date(2012, 10, 30),   # Hurricane Sandy
    dt.date(2018, 12, 5),                           # President Bush
    dt.date(2025, 1, 9),                            # President Carter
}


def _easter(year: int) -> dt.date:
    """Gregorian Easter Sunday (anonymous algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    day = ((h + l_ - 7 * m + 114) % 31) + 1
    return dt.date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """The n-th ``weekday`` (Mon=0) of a month; n=-1 is the last."""
    if n > 0:
        first = dt.date(year, month, 1)
        shift = (weekday - first.weekday()) % 7
        return first + dt.timedelta(days=shift + 7 * (n - 1))
    nxt = dt.date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - dt.timedelta(days=1)
    return last - dt.timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: dt.date) -> dt.date:
    """Saturday holidays are observed Friday, Sunday ones Monday."""
    if day.weekday() == 5:
        return day - dt.timedelta(days=1)
    if day.weekday() == 6:
        return day + dt.timedelta(days=1)
    return day


@lru_cache(maxsize=None)
def holidays(year: int) -> Set[dt.date]:
    """NYSE full-day holidays observed in ``year``."""
    out = {
        _nth_weekday(year, 1, 0, 3),                # Martin Luther King Jr.
        _nth_weekday(year, 2, 0, 3),                # Washington's Birthday
        _easter(year) - dt.timedelta(days=2),       # Good Friday
        _nth_weekday(year, 5, 0, -1),               # Memorial Day
        _observed(dt.date(year, 7, 4)),             # Independence Day
        _nth_weekday(year, 9, 0, 1),                # Labor Day
        _nth_weekday(year, 11, 3, 4),               # Thanksgiving
        _observed(dt.date(year, 12, 25)),           # Christmas
    }
    new_year = dt.date(year, 1, 1)
    if new_year.weekday() != 5:         # a Saturday New Year is not moved
        out.add(_observed(new_year))
    if year >= 2022:
        out.add(_observed(dt.date(year, 6, 19)))    # Juneteenth
    out |= {d for d in SPECIAL_CLOSURES if d.year == year}
    return {d for d in out if d.year == year}


def is_trading_day(day: dt.date) -> bool:
    return day.weekday() < 5 and day not in holidays(day.year)


def trading_days(start: dt.date, end: dt.date) -> List[dt.date]:
    """Every trading day from ``start`` to ``end`` inclusive."""
    days = pd.bdate_range(start, end).date
    return [d for d in days if is_trading_day(d)]


def previous_trading_day(day: dt.date) -> dt.date:
    d = day - dt.timedelta(days=1)
    while not is_trading_day(d):
        d -= dt.timedelta(days=1)
    return d


def next_trading_day(day: dt.date) -> dt.date:
    d = day + dt.timedelta(days=1)
    while not is_trading_day(d):
        d += dt.timedelta(days=1)
    return d
