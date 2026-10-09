"""Shared fixtures: a small synthetic market, built once per session."""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from quant_duel import config as cfgmod  # noqa: E402
from quant_duel.features.build import build  # noqa: E402
from quant_duel.ingest.sources import SyntheticSource  # noqa: E402

TICKERS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "SPY", "QQQ"]
START, END = dt.date(2016, 1, 4), dt.date(2021, 12, 31)


@pytest.fixture(scope="session")
def cfg():
    c = cfgmod.load(None, root=ROOT)
    raw = c.raw
    raw["universe"]["tickers"] = TICKERS
    raw["backtest"].update(train_days=504, test_days=21, min_train_days=252)
    return c


@pytest.fixture(scope="session")
def prices():
    src = SyntheticSource(seed=7)
    return {t: src.fetch(t, START, END) for t in TICKERS}


@pytest.fixture(scope="session")
def table(prices, cfg):
    return build(prices, cfg["features"], TICKERS, context="SPY")
