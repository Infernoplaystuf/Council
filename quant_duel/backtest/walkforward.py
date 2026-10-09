"""Walk-forward splits: time-ordered only, never random.

Each fold trains on the ``train_days`` trading days before its test block
(at least ``min_train_days``), skips ``embargo`` days, then tests on the next
``test_days``. A training row's label is the return to the NEXT day, so the
last training day's label reaches one day forward; the one-day embargo keeps
that day out of the test block's reach entirely.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, List, Sequence

import datetime as dt


@dataclass(frozen=True)
class Fold:
    index: int
    train: List[dt.date]
    test: List[dt.date]


def folds(dates: Sequence[dt.date], *, train_days: int, test_days: int,
          min_train_days: int, embargo: int = 1,
          start_after: dt.date | None = None) -> Iterator[Fold]:
    """Yield folds over the sorted unique ``dates``."""
    days = sorted(set(dates))
    first_test = max(min_train_days + embargo, 0)
    if start_after is not None:
        first_test = max(first_test, next(
            (i for i, d in enumerate(days) if d > start_after), len(days)))
    i, k = first_test, 0
    while i < len(days):
        train_end = i - embargo                  # exclusive
        train_start = max(0, train_end - train_days)
        train = days[train_start:train_end]
        test = days[i:i + test_days]
        if len(train) >= min_train_days and test:
            yield Fold(k, train, test)
            k += 1
        i += test_days
