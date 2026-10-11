"""When a headline counts.

Day t's feature cutoff is ``news.cutoff`` (default 16:00) New York time on
trading day t. A headline counts for the first trading day whose cutoff is
strictly after its publish time: published 15:59 on a Tuesday → Tuesday;
16:00 or later → Wednesday; Saturday → Monday. A headline with no usable
publish time never counts (we cannot prove it came before a cutoff), and a
publish time later than when we fetched it is pulled back to the fetch time.
"""
from __future__ import annotations

import datetime as dt
from email.utils import parsedate_to_datetime
from typing import Optional
from zoneinfo import ZoneInfo

from ..market_calendar import is_trading_day, next_trading_day

NY = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
CLOCK_SKEW = dt.timedelta(minutes=10)


def cutoff_at(day: dt.date, cutoff: str = "16:00") -> dt.datetime:
    """Day ``day``'s cutoff as an aware UTC datetime."""
    h, m = (int(x) for x in cutoff.split(":"))
    return dt.datetime(day.year, day.month, day.day, h, m,
                       tzinfo=NY).astimezone(UTC)


def sentiment_day(published: dt.datetime, cutoff: str = "16:00") -> dt.date:
    """The trading day a headline published at ``published`` counts for."""
    local = published.astimezone(NY)
    day = local.date()
    if is_trading_day(day) and published < cutoff_at(day, cutoff):
        return day
    return next_trading_day(day)


def parse_time(text: Optional[str]) -> Optional[dt.datetime]:
    """RFC 822 (RSS) or ISO 8601 (Atom) → aware UTC; None if unusable.
    A time without a zone is refused rather than guessed."""
    if not text or not text.strip():
        return None
    s = text.strip()
    try:
        t = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        try:
            t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if t is None or t.tzinfo is None:
        return None
    return t.astimezone(UTC)


def effective_time(published: Optional[dt.datetime],
                   fetched: dt.datetime) -> Optional[dt.datetime]:
    if published is None:
        return None
    if published > fetched + CLOCK_SKEW:
        return fetched
    return published
