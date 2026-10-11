"""Phase 2: gradient boosting (LightGBM or scikit-learn behind one
interface) and the comparison against the baselines."""
from __future__ import annotations

import copy

import numpy as np
import pandas as pd
import pytest

from quant_duel.backtest.run import (compare_models, evaluate, paired_edge,
                                     predict, suspicious)
from quant_duel.features.build import LeakageError, feature_columns
from quant_duel.models import base as mb

BACKENDS = ["sklearn"] + (["lightgbm"] if mb.lightgbm_available() else [])


def _small(cfg, backend):
    """The configured boosting settings with fewer trees, so tests are fast."""
    raw = copy.deepcopy(cfg.raw)
    raw["model"]["params"]["boosting"].update(max_iter=40, backend=backend)
    return raw


def _toy(n=4000, seed=0, signal=True):
    rng = np.random.default_rng(seed)
    X = pd.DataFrame({"a": rng.normal(size=n), "b": rng.normal(size=n)})
    y = (X["a"].to_numpy() + 0.5 * rng.normal(size=n) > 0) if signal else \
        rng.random(n) > 0.5
    return X, y.astype(int)


@pytest.mark.parametrize("backend", BACKENDS)
def test_boosting_learns_a_real_signal(backend):
    X, y = _toy()
    m = mb.Boosting(max_iter=50, min_samples_leaf=20, backend=backend).fit(
        X.iloc[:3000], y[:3000])
    p = m.predict_proba(X.iloc[3000:])
    assert m.backend == backend
    assert ((p > 0.5) == y[3000:]).mean() > 0.8
    assert p.min() >= mb.EPS and p.max() <= 1 - mb.EPS
    imp = m.importances()
    assert imp.index[0] == "a" and set(imp.index) == {"a", "b"}


@pytest.mark.parametrize("backend", BACKENDS)
def test_boosting_is_deterministic(backend):
    X, y = _toy(signal=False)
    p1 = mb.Boosting(max_iter=30, backend=backend, seed=3).fit(X, y) \
        .predict_proba(X)
    p2 = mb.Boosting(max_iter=30, backend=backend, seed=3).fit(X, y) \
        .predict_proba(X)
    np.testing.assert_array_equal(p1, p2)


def test_the_scikit_learn_fallback_when_lightgbm_is_missing(monkeypatch):
    monkeypatch.setattr(mb, "lightgbm_available", lambda: False)
    m = mb.make_model("boosting", {"boosting": {"backend": "auto"}})
    assert m.backend == "sklearn"
    with pytest.raises(ValueError, match="not installed"):
        mb.make_model("boosting", {"boosting": {"backend": "lightgbm"}})
    X, y = _toy(n=1000)
    assert len(m.fit(X, y).predict_proba(X)) == 1000


def test_auto_prefers_lightgbm_when_present(monkeypatch):
    monkeypatch.setattr(mb, "lightgbm_available", lambda: True)
    assert mb.resolve_backend("auto") == "lightgbm"
    with pytest.raises(ValueError, match="backend"):
        mb.resolve_backend("xgboost")


def test_the_config_settings_reach_the_model(cfg):
    m = mb.make_model("boosting", cfg["model"]["params"], seed=11)
    b = cfg["model"]["params"]["boosting"]
    assert (m.max_depth, m.max_iter, m.min_samples_leaf, m.seed) == \
        (b["max_depth"], b["max_iter"], b["min_samples_leaf"], 11)
    assert set(mb.KINDS) == {"always_up", "persistence", "logistic",
                             "boosting"}


@pytest.mark.parametrize("backend", BACKENDS)
def test_canary_boosting_on_a_shuffled_target_scores_about_half(
        table, cfg, backend):
    rng = np.random.default_rng(1)
    shuffled = table.copy()
    labelled = shuffled["target"].notna()
    shuffled.loc[labelled, "target"] = rng.permutation(
        shuffled.loc[labelled, "target"].to_numpy())
    raw = _small(cfg, backend)
    acc = evaluate(predict(shuffled, "boosting", raw), raw)["accuracy"]
    assert 0.47 < acc < 0.53


def test_canary_boosting_is_guarded_too(table, cfg):
    leaky = table.copy()
    leaky["peek"] = (leaky["fwd_return"] > 0).astype(float)
    with pytest.raises(LeakageError, match="peek"):
        predict(leaky, "boosting", _small(cfg, "sklearn"),
                features=feature_columns(table) + ["peek"])


def test_no_signal_means_no_edge_for_boosting(table, cfg):
    raw = _small(cfg, BACKENDS[-1])
    kinds = ["always_up", "persistence", "logistic", "boosting"]
    res = compare_models(table, kinds, raw)
    assert 0.45 < res.loc["boosting", "accuracy"] < 0.56
    assert 0.67 < res.loc["boosting", "log_loss"] < 0.72
    # Learned models get a paired edge vs the best baseline; with no signal
    # it must be indistinguishable from zero (or negative: overfitting).
    assert res.attrs["best_baseline"] in ("always_up", "persistence")
    for k in ("logistic", "boosting"):
        assert res.loc[k, "ll_edge_t"] < 2.5
    assert np.isnan(res.loc["always_up", "ll_edge"])
    assert suspicious(res) == []
    # Same days, same rows for every model.
    preds = res.attrs["preds"]
    keys = {k: set(zip(p["date"], p["ticker"])) for k, p in preds.items()}
    assert all(v == keys["always_up"] for v in keys.values())


def test_paired_edge_on_known_predictions():
    rows = pd.DataFrame({"date": [1, 1, 2, 2], "ticker": ["A", "B"] * 2,
                         "target": [1.0, 0.0, 1.0, 0.0]})
    good = rows.assign(p=[0.6, 0.4, 0.6, 0.4])
    coin = rows.assign(p=0.5)
    e = paired_edge(good, coin)
    assert e["ll_edge"] == pytest.approx(np.log(0.5) * -1 + np.log(0.6))
    assert paired_edge(coin, good)["ll_edge"] < 0
    with pytest.raises(ValueError, match="same rows"):
        paired_edge(good, coin.iloc[:3])
