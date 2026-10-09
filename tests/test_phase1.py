"""Phase 1: config, calendar, ingest, features, walk-forward, costs, metrics
and the leakage canaries."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest
import yaml

from quant_duel import config as cfgmod
from quant_duel import market_calendar as mc
from quant_duel.backtest import metrics as mx
from quant_duel.backtest import strategy as st
from quant_duel.backtest.run import compare_models, evaluate, predict, suspicious
from quant_duel.backtest.walkforward import folds
from quant_duel.features.build import (LeakageError, build, feature_columns,
                                       guard)
from quant_duel.ingest import validate
from quant_duel.ingest.sources import SyntheticSource
from quant_duel.ingest.store import PriceStore, ingest

from .conftest import ROOT, TICKERS


# ============================================================
# Config: the nodes differ only in news
# ============================================================

def test_the_two_nodes_differ_only_in_news():
    a, b = cfgmod.load("A", root=ROOT), cfgmod.load("B", root=ROOT)
    assert a.news_enabled and not b.news_enabled
    cfgmod.check_identical(a, b)
    assert a.node_dir.name == "A" and a.shared_dir == b.shared_dir


def test_a_node_file_cannot_change_shared_settings(tmp_path):
    (tmp_path / "nodes").mkdir()
    (tmp_path / "config.yaml").write_text((ROOT / "config.yaml").read_text())
    (tmp_path / "nodes" / "A.yaml").write_text(yaml.safe_dump(
        {"node_id": "A", "news_enabled": True, "seed": 1}))
    with pytest.raises(cfgmod.ConfigError, match="seed"):
        cfgmod.load("A", root=tmp_path)


# ============================================================
# Calendar
# ============================================================

@pytest.mark.parametrize("day", [
    "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27",
    "2024-06-19", "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25",
    "2025-01-09", "2025-04-18", "2022-12-26", "2021-12-24", "2023-01-02"])
def test_known_nyse_holidays(day):
    assert not mc.is_trading_day(dt.date.fromisoformat(day))


@pytest.mark.parametrize("day", ["2024-07-03", "2021-12-31", "2024-11-29",
                                 "2022-06-17"])
def test_known_trading_days(day):
    # 2021-12-31: New Year 2022 fell on a Saturday and was NOT moved.
    assert mc.is_trading_day(dt.date.fromisoformat(day))


def test_a_year_has_about_252_trading_days():
    n = len(mc.trading_days(dt.date(2024, 1, 1), dt.date(2024, 12, 31)))
    assert n == 252


# ============================================================
# Ingest
# ============================================================

def test_incremental_ingest_and_hashes(tmp_path):
    store = PriceStore(tmp_path)
    src = SyntheticSource(seed=1)
    ingest(store, src, ["AAA", "SPY"], dt.date(2020, 1, 2), dt.date(2020, 6, 30))
    first = store.day_hashes(["AAA", "SPY"])
    rep = ingest(store, src, ["AAA", "SPY"], dt.date(2020, 1, 2),
                 dt.date(2020, 9, 30))
    assert rep[0]["added"] > 0 and not rep[0]["problems"]
    second = store.day_hashes(["AAA", "SPY"])
    merged = first.merge(second, on="date", suffixes=("_1", "_2"))
    assert (merged["hash_1"] == merged["hash_2"]).all()   # history unchanged
    assert (tmp_path / "day_hashes.csv").exists()


def test_a_readjusted_history_is_refetched(tmp_path):
    store = PriceStore(tmp_path)

    class Adjusting(SyntheticSource):
        factor = 1.0

        def fetch(self, ticker, start, end):
            df = super().fetch(ticker, start, end).copy()
            df["adj_close"] = df["close"] * self.factor
            return df
    src = Adjusting(seed=2)
    store.update("AAA", src, dt.date(2020, 1, 2), dt.date(2020, 6, 30))
    src.factor = 0.98                      # a dividend re-adjusted history
    rep = store.update("AAA", src, dt.date(2020, 1, 2), dt.date(2020, 7, 31))
    assert rep["refetched"]
    df = store.load("AAA")
    assert np.allclose(df["adj_close"] / df["close"], 0.98)


def test_validation_finds_gaps_duplicates_and_bad_prices():
    src = SyntheticSource(seed=3)
    df = src.fetch("AAA", dt.date(2021, 1, 4), dt.date(2021, 3, 31))
    broken = pd.concat([df.iloc[:10], df.iloc[12:], df.iloc[[5]]])
    broken.iloc[3, broken.columns.get_loc("high")] = 0.01
    found = " ".join(validate.problems("AAA", broken))
    assert "duplicate" in found and "missing trading day" in found
    assert "high below low" in found
    assert validate.problems("AAA", df) == []


# ============================================================
# Features: day t uses data up to t only
# ============================================================

def test_features_do_not_change_when_the_future_is_removed(prices, cfg, table):
    """The structural timing test: cut every price series at day T and
    rebuild — every feature on days <= T must be identical."""
    cut = dt.date(2019, 6, 14)
    short = {t: df[df.index <= cut] for t, df in prices.items()}
    t2 = build(short, cfg["features"], TICKERS, context="SPY")
    feats = feature_columns(table)
    a = table[table["date"] <= cut].set_index(["date", "ticker"])[feats]
    b = t2.set_index(["date", "ticker"])[feats].loc[a.index]
    pd.testing.assert_frame_equal(a, b)


def test_the_target_is_tomorrows_direction(prices, table):
    row = table[(table["ticker"] == "AAA")].iloc[100]
    close = prices["AAA"]["adj_close"]
    i = close.index.get_loc(row["date"])
    assert row["fwd_return"] == pytest.approx(close.iloc[i + 1] / close.iloc[i] - 1)
    assert row["target"] == float(close.iloc[i + 1] > close.iloc[i])


def test_the_newest_day_is_kept_without_a_target(table):
    last = table["date"].max()
    assert table[table["date"] == last]["target"].isna().all()


# ============================================================
# Walk-forward
# ============================================================

def test_folds_are_time_ordered_with_an_embargo(table):
    dates = sorted(table["date"].unique())
    fs = list(folds(dates, train_days=504, test_days=21, min_train_days=252))
    assert len(fs) > 10
    for f in fs:
        assert max(f.train) < min(f.test)
        gap = dates.index(min(f.test)) - dates.index(max(f.train))
        assert gap == 2                   # one embargoed day between
        assert len(f.train) <= 504
    tests = [d for f in fs for d in f.test]
    assert len(tests) == len(set(tests))   # no day tested twice


# ============================================================
# Costs and metrics
# ============================================================

def test_costs_are_charged_on_every_change_of_position():
    pred = pd.DataFrame({
        "date": [1, 1, 2, 2, 3, 3], "ticker": ["A", "B"] * 3,
        "fwd_return": [0.01, 0.0, 0.0, 0.02, 0.0, 0.0],
        "p": [0.9, 0.1, 0.9, 0.9, 0.1, 0.1]})
    pos = st.positions(pred, 0.5)
    net, turnover = st.daily_returns(pred, pos, cost_bps=10)
    # Day 1: A enters (weight 1/2) → 0.5% gross, 5 bps * 0.5 traded.
    assert turnover.tolist() == [0.5, 0.5, 1.0]
    assert net.loc[1] == pytest.approx(0.005 - 0.5 * 0.001)
    assert net.loc[2] == pytest.approx(0.01 - 0.5 * 0.001)
    assert net.loc[3] == pytest.approx(-1.0 * 0.001)


def test_strategy_metrics_on_a_known_series():
    r = pd.Series([0.1, -0.5, 0.2])
    m = mx.strategy_metrics(r)
    assert m["cum_return"] == pytest.approx(1.1 * 0.5 * 1.2 - 1)
    assert m["max_drawdown"] == pytest.approx(-0.5)


def test_prediction_metrics():
    m = mx.prediction_metrics(np.array([0.9, 0.2, 0.6]), np.array([1, 0, 0]))
    assert m["accuracy"] == pytest.approx(2 / 3)
    assert m["brier"] == pytest.approx((0.01 + 0.04 + 0.36) / 3)


# ============================================================
# Leakage canaries
# ============================================================

def test_canary_a_feature_equal_to_the_future_target_is_caught(table, cfg):
    leaky = table.copy()
    leaky["peek"] = leaky["fwd_return"]               # tomorrow's return
    with pytest.raises(LeakageError, match="peek"):
        predict(leaky, "logistic", cfg.raw,
                features=feature_columns(table) + ["peek"])
    with pytest.raises(LeakageError, match="label"):
        guard(leaky, ["target"])


def test_canary_a_shuffled_target_scores_about_half(table, cfg):
    rng = np.random.default_rng(0)
    shuffled = table.copy()
    labelled = shuffled["target"].notna()
    shuffled.loc[labelled, "target"] = rng.permutation(
        shuffled.loc[labelled, "target"].to_numpy())
    pred = predict(shuffled, "logistic", cfg.raw)
    acc = evaluate(pred, cfg.raw)["accuracy"]
    assert 0.47 < acc < 0.53


def test_no_signal_means_no_edge(table, cfg):
    """Synthetic prices carry no information about tomorrow: every model
    must land near a coin flip, and nothing may look suspicious."""
    res = compare_models(table, ["always_up", "persistence", "logistic"],
                         cfg.raw)
    for name in ("always_up", "persistence", "logistic"):
        assert 0.45 < res.loc[name, "accuracy"] < 0.56
        assert 0.67 < res.loc[name, "log_loss"] < 0.71
    assert {"buy_and_hold", "hold_SPY"} <= set(res.index)
    assert suspicious(res) == []


def test_suspiciously_good_results_are_flagged():
    res = pd.DataFrame({"accuracy": [0.62, np.nan], "sharpe": [3.0, 0.5]},
                       index=["logistic", "buy_and_hold"])
    flags = suspicious(res)
    assert any("accuracy" in f for f in flags) and any("Sharpe" in f
                                                       for f in flags)
