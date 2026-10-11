"""Phase 3: the paper ledger, the frozen control, the SPY benchmark, the
daily step and the exports."""
from __future__ import annotations

import copy
import datetime as dt
import shutil

import numpy as np
import pandas as pd
import pytest

from quant_duel import cli
from quant_duel.export.csv import export_node, metrics
from quant_duel.features.build import build
from quant_duel.market_calendar import trading_days
from quant_duel.paper import artifact
from quant_duel.paper.books import BookConfig, group_of
from quant_duel.paper.daily import as_of, node_lock, run_through, step
from quant_duel.paper.ledger import Ledger

from .conftest import ROOT, TICKERS

START, END = dt.date(2021, 9, 1), dt.date(2021, 10, 29)


def _cfg(cfg, **paper):
    raw = copy.deepcopy(cfg.raw)
    raw["paper"].update(paper)
    return raw


def _ledger(tmp_path, cfg, name="A", book=None):
    led = Ledger(tmp_path / name / "paper.sqlite")
    led.init(name, 100_000.0, book or BookConfig.from_config(cfg), "SPY")
    return led


@pytest.fixture(scope="module")
def run(tmp_path_factory, cfg, table, prices):
    """Two months of paper trading on the synthetic market, node A."""
    led = _ledger(tmp_path_factory.mktemp("run"), cfg)
    res = run_through(led, _cfg(cfg), table, prices, START, END)
    yield led, res
    led.close()


# ============================================================
# Timing
# ============================================================

def test_as_of_hides_the_future_and_todays_label(table):
    day = dt.date(2021, 6, 15)
    cut = as_of(table, day)
    assert cut["date"].max() == day
    assert cut[cut["date"] == day]["target"].isna().all()
    assert cut[cut["date"] < day]["target"].notna().all()
    # the table itself is untouched
    assert table[table["date"] == day]["target"].notna().all()


def test_training_window_matches_the_walk_forward_embargo(table):
    day = dt.date(2021, 6, 15)
    rows = artifact.training_rows(as_of(table, day), day, train_days=100)
    dates = sorted(table["date"].unique())
    i = dates.index(day)
    assert max(rows["date"]) == dates[i - 2]          # i-1 is embargoed
    assert rows["date"].nunique() == 100


def test_catching_up_equals_running_each_day_with_only_its_data(
        tmp_path, cfg, prices, table):
    """The structural test: stepping through days with the full table must
    give exactly the ledger of daily runs that only had prices up to that
    day — nothing from the future can leak into a day's predictions."""
    raw = _cfg(cfg)
    days = [d for d in sorted(table["date"].unique())
            if dt.date(2021, 9, 1) <= d <= dt.date(2021, 9, 14)]
    caught = _ledger(tmp_path, cfg, "A")
    run_through(caught, raw, table, prices, days[0], days[-1])
    live = _ledger(tmp_path, cfg, "B")
    for d in days:
        cut = {t: df[df.index <= d] for t, df in prices.items()}
        t2 = build(cut, raw["features"], TICKERS, context="SPY")
        step(live, raw, t2, cut, d)
    for name in ("predictions", "equity", "fills", "models"):
        a = caught.frame(name).drop(columns=["note"], errors="ignore")
        b = live.frame(name).drop(columns=["note"], errors="ignore")
        pd.testing.assert_frame_equal(a, b, check_exact=False, rtol=1e-12)


# ============================================================
# Ledger accounting
# ============================================================

def test_cash_plus_holdings_is_equity_every_day(run):
    led, res = run
    eq = led.frame("equity")
    assert set(eq["book"]) == {"live", "control", "spy"}
    assert np.allclose(eq["cash"] + eq["holdings"], eq["equity"])
    # and the current holdings agree with the last equity row
    last = eq[eq["date"] == eq["date"].max()].set_index("book")
    for book in ("live", "control", "spy"):
        held = sum(led.holdings(book).values())
        assert held == pytest.approx(last.loc[book, "holdings"])
        assert led.cash(book) == pytest.approx(last.loc[book, "cash"])


def test_costs_match_the_traded_dollars(run):
    led, _ = run
    fills = led.frame("fills")
    assert len(fills)
    assert np.allclose(fills["cost"], fills["notional"].abs() * 5e-4)
    eq = led.frame("equity")
    traded = fills.assign(a=fills["notional"].abs()).groupby(
        ["book", "date"])["a"].sum()
    e = eq.set_index(["book", "date"])["traded"]
    assert np.allclose(e.loc[traded.index], traded)


def _toy_market():
    """Two tickers plus SPY over 12 days with hand-made prices; X has a
    2:1 split on day 9 (raw prices halve, adjusted prices do not)."""
    days = trading_days(dt.date(2022, 3, 1), dt.date(2022, 3, 16))
    rng = np.random.default_rng(5)
    prices = {}
    for t in ("X", "Y", "SPY"):
        adj = 100 * np.cumprod(1 + rng.normal(0.001, 0.01, len(days)))
        opn = adj * (1 + rng.normal(0, 0.003, len(days)))
        raw = adj.copy()
        ro = opn.copy()
        if t == "X":                          # history adjusted for a split
            raw[:9] *= 2
            ro[:9] *= 2
        prices[t] = pd.DataFrame(
            {"open": ro, "high": np.maximum(ro, raw) * 1.01,
             "low": np.minimum(ro, raw) * 0.99, "close": raw,
             "adj_close": adj, "volume": 1e6},
            index=pd.Index(days, name="date"))
    rows = []
    for t in ("X", "Y"):
        a = prices[t]["adj_close"]
        fwd = a.shift(-1) / a - 1
        for i, d in enumerate(days):
            rows.append({"date": d, "ticker": t,
                         "ret_1": float(rng.normal()),
                         "fwd_return": fwd.iloc[i],
                         "target": float(fwd.iloc[i] > 0)
                         if not np.isnan(fwd.iloc[i]) else np.nan})
    return days, prices, pd.DataFrame(rows)


def test_hand_computed_ledger_through_a_split(tmp_path):
    """Always-long over X and Y: fills at the next open with 10 bps on the
    traded dollars; holdings move by adjusted returns, so X's split is
    invisible to the books."""
    days, prices, table = _toy_market()
    book = BookConfig("always_up", {}, {}, 0.5, train_days=50, refit_days=99)
    led = Ledger(tmp_path / "paper.sqlite")
    led.init("A", 1000.0, book, "SPY")
    raw = {"paper": {"fill": "open"}, "backtest": {"cost_bps": 10},
           "seed": 0}
    first = days[3]                         # enough labelled history
    run_through(led, raw, table, prices, first, days[-1])
    c = 1e-3
    cash, hold = 1000.0, {}
    spy_cash, spy = 1000.0, 0.0
    for i, d in enumerate(days[3:], start=3):
        def adj(t, j, field):
            df = prices[t]
            f = df["adj_close"].iloc[j] / df["close"].iloc[j]
            return df[field].iloc[j] * (f if field == "open" else 1.0) \
                if field == "open" else df["adj_close"].iloc[j]
        if i > 3:
            hold = {t: v * adj(t, i, "open") / adj(t, i - 1, "close")
                    for t, v in hold.items()}
            e = cash + sum(hold.values())
            for t in ("X", "Y"):
                trade = 0.5 * e - hold.get(t, 0.0)
                cash -= trade + abs(trade) * c
                hold[t] = 0.5 * e
            hold = {t: v * adj(t, i, "close") / adj(t, i, "open")
                    for t, v in hold.items()}
            spy *= adj("SPY", i, "open") / adj("SPY", i - 1, "close")
            if i == 4:
                spy_cash -= 1000.0 * (1 + c)
                spy = 1000.0
            spy *= adj("SPY", i, "close") / adj("SPY", i, "open")
        eq = led.frame("equity", "date=?", (d.isoformat(),)).set_index("book")
        assert eq.loc["control", "equity"] == pytest.approx(
            cash + sum(hold.values()), rel=1e-12)
        assert eq.loc["spy", "equity"] == pytest.approx(spy_cash + spy,
                                                        rel=1e-12)
    # one SPY purchase, ever
    assert (led.frame("fills")["book"] == "spy").sum() == 1


def test_fill_at_close_option(tmp_path):
    days, prices, table = _toy_market()
    book = BookConfig("always_up", {}, {}, 0.5, train_days=50, refit_days=99)
    led = Ledger(tmp_path / "paper.sqlite")
    led.init("A", 1000.0, book, "SPY")
    raw = {"paper": {"fill": "close"}, "backtest": {"cost_bps": 0},
           "seed": 0}
    run_through(led, raw, table, prices, days[3], days[5])
    f = led.frame("fills")
    f = f[(f["book"] == "spy")]
    assert f["price"].iloc[0] == pytest.approx(
        prices["SPY"]["close"].iloc[4])
    eq = led.frame("equity").set_index(["book", "date"])["equity"]
    # bought at day 4's close with no cost → day 4 equity is unchanged
    assert eq[("spy", days[4].isoformat())] == pytest.approx(1000.0)


# ============================================================
# Books, control and models
# ============================================================

def test_live_and_control_start_identical_and_control_is_frozen(run):
    led, _ = run
    assert led.book_config("live") == led.book_config("control")
    p = led.frame("predictions")
    a = p[p["book"] == "live"].drop(columns="book").reset_index(drop=True)
    b = p[p["book"] == "control"].drop(columns="book").reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b)
    with pytest.raises(PermissionError):
        led.set_book_config("control", led.book_config("control"))


def test_both_nodes_get_byte_identical_models_and_predictions(
        tmp_path, cfg, table, prices):
    raw = _cfg(cfg)
    a, b = _ledger(tmp_path, cfg, "A"), _ledger(tmp_path, cfg, "B")
    for led in (a, b):
        run_through(led, raw, table, prices, dt.date(2021, 9, 1),
                    dt.date(2021, 9, 8))
    assert a.frame("models")["spec_json"].tolist() == \
        b.frame("models")["spec_json"].tolist()
    pd.testing.assert_frame_equal(a.frame("predictions"),
                                  b.frame("predictions"))


def test_models_refit_on_the_walk_forward_cadence(run, cfg):
    led, res = run
    m = led.frame("models")
    ctrl = m[m["book"] == "control"].sort_values("first_day")
    firsts = [dt.date.fromisoformat(d) for d in ctrl["first_day"]]
    run_days = [r["day"] for r in res]
    assert firsts[0] == run_days[0]
    step_days = cfg["backtest"]["test_days"]
    for x, y in zip(firsts, firsts[1:]):
        assert run_days.index(y) - run_days.index(x) == step_days
    assert (ctrl["note"].iloc[1:] .str.startswith("scheduled refit")).all()


def test_a_spec_refits_to_the_same_model_and_detects_changed_data(
        table, cfg):
    day = dt.date(2021, 6, 15)
    cut = as_of(table, day)
    book = BookConfig.from_config(cfg)
    model, spec = artifact.fit(book, cut, day, seed=1)
    again = artifact.refit(spec, cut)
    X = cut[cut["date"] == day][spec.data["features"]]
    np.testing.assert_array_equal(model.predict_proba(X),
                                  again.predict_proba(X))
    changed = cut.copy()
    changed.loc[changed["date"] < day, "ret_5"] *= 1.01   # re-adjusted
    with pytest.raises(artifact.ArtifactMismatch, match="training data"):
        artifact.refit(spec, changed)


def test_changing_the_live_settings_fits_a_new_model(tmp_path, cfg, table,
                                                     prices):
    raw = _cfg(cfg)
    led = _ledger(tmp_path, cfg)
    days = sorted(d for d in table["date"].unique()
                  if dt.date(2021, 9, 1) <= d <= dt.date(2021, 9, 10))
    run_through(led, raw, table, prices, days[0], days[3])
    conf = led.book_config("live")
    feats = dict(conf.features_enabled, rsi=False)
    with led.transaction():
        led.set_book_config("live", BookConfig(**dict(
            conf.to_dict(), features_enabled=feats, threshold=0.55)))
    run_through(led, raw, table, prices, days[0], days[-1])
    m = led.frame("models")
    live = m[m["book"] == "live"]
    assert live["note"].tolist() == ["first model", "settings changed"]
    assert len(m[m["book"] == "control"]) == 1          # control untouched
    p = led.frame("predictions")
    later = p[(p["book"] == "live") & (p["date"] > days[3].isoformat())]
    assert (later["signal"] == (later["p"] >= 0.55)).all()


def test_feature_groups_cover_every_column(table):
    from quant_duel.features.build import feature_columns
    groups = {group_of(c) for c in feature_columns(table)}
    assert groups == {"returns", "ma_ratio", "volatility", "rsi",
                      "volume_z", "day_of_week", "spy_context"}
    assert group_of("volume_z") == "volume_z" and group_of("vol_20") == \
        "volatility" and group_of("spy_ret_1") == "spy_context"


# ============================================================
# Running a day
# ============================================================

def test_a_day_runs_once_and_in_order(tmp_path, cfg, table, prices):
    raw = _cfg(cfg)
    led = _ledger(tmp_path, cfg)
    d1, d2 = dt.date(2021, 9, 1), dt.date(2021, 9, 2)
    step(led, raw, table, prices, d2)
    n = len(led.frame("predictions"))
    assert step(led, raw, table, prices, d2)["skipped"]
    assert len(led.frame("predictions")) == n
    with pytest.raises(ValueError, match="before the last"):
        step(led, raw, table, prices, d1)


def test_a_failing_day_leaves_nothing_behind(tmp_path, cfg, table, prices,
                                             monkeypatch):
    raw = _cfg(cfg)
    led = _ledger(tmp_path, cfg)
    from quant_duel.paper import daily

    def boom(*a, **k):
        raise RuntimeError("model blew up")
    monkeypatch.setattr(daily, "_predict", boom)
    with pytest.raises(RuntimeError):
        step(led, raw, table, prices, dt.date(2021, 9, 1))
    for name in ("equity", "predictions", "orders", "runs", "fills"):
        assert led.frame(name).empty
    assert led.cash("live") == 100_000.0


def test_predictions_are_scored_the_next_day(run, table):
    led, res = run
    p = led.frame("predictions")
    last = res[-1]["day"].isoformat()
    assert p[p["date"] == last]["target"].isna().all()
    done = p[p["date"] < last]
    assert done["target"].notna().all()
    t = table.set_index(["date", "ticker"])["target"]
    keys = list(zip(pd.to_datetime(done["date"]).dt.date, done["ticker"]))
    assert np.array_equal(t.loc[keys].to_numpy(), done["target"].to_numpy())


def test_node_lock(tmp_path):
    path = tmp_path / "daily.lock"
    with node_lock(path):
        with pytest.raises(RuntimeError, match="another run"):
            with node_lock(path):
                pass
    assert not path.exists()


# ============================================================
# Exports
# ============================================================

def test_exports(run, tmp_path):
    led, res = run
    files = export_node(led, tmp_path / "A")
    names = {f.name for f in files}
    assert {"predictions.csv", "fills.csv", "equity.csv",
            "equity_curves.csv", "models.csv", "price_hashes.csv",
            "metrics.csv", "holdings.csv", "events.csv"} <= names
    for f in files:
        df = pd.read_csv(f)
        assert "node" in df.columns
        assert not list(tmp_path.glob("A/*.tmp"))
    m = pd.read_csv(tmp_path / "A" / "metrics.csv").set_index("book")
    assert set(m.index) == {"live", "control", "spy"}
    assert m.loc["live", "n"] == len(res[:-1]) * len(TICKERS)
    assert 0.3 < m.loc["live", "accuracy"] < 0.7
    curves = pd.read_csv(tmp_path / "A" / "equity_curves.csv")
    assert list(curves.columns[:2]) == ["node", "date"]
    assert abs(curves["control"].iloc[0] - 1) < 0.01


def test_metrics_returns_are_computed_from_equity(run):
    led, _ = run
    m = metrics(led).set_index("book")
    eq = led.frame("equity")
    spy = eq[eq["book"] == "spy"]["equity"]
    assert m.loc["spy", "cum_return"] == pytest.approx(
        spy.iloc[-1] / 100_000 - 1)


# ============================================================
# The CLI end to end (synthetic prices, temporary root)
# ============================================================

def test_daily_cli_end_to_end(tmp_path, capsys):
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    shutil.copytree(ROOT / "nodes", tmp_path / "nodes")
    text = (tmp_path / "config.yaml").read_text()
    text = text.replace("history_start: \"2015-01-01\"",
                        "history_start: \"2019-01-01\"")
    (tmp_path / "config.yaml").write_text(text)
    args = ["--root", str(tmp_path), "--node", "B", "daily", "--source",
            "synthetic", "--start", "2023-03-01", "--end", "2023-03-10"]
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert "new ledger for node B" in out and "2023-03-10" in out
    assert (tmp_path / "data" / "B" / "paper.sqlite").exists()
    assert (tmp_path / "exports" / "B" / "equity.csv").exists()
    hashes = pd.read_csv(tmp_path / "exports" / "B" / "price_hashes.csv")
    assert hashes["price_hash"].notna().all() and len(hashes) == 8
    # a second run the same day does nothing new
    assert cli.main(args) == 0
    assert "nothing new" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="--node"):
        cli.main(["--root", str(tmp_path), "daily", "--no-ingest"])
