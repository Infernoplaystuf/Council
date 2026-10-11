"""Phase 6: the frozen experiment file, the paired statistics, compare on
data with a known answer, the data checks, replay, and the CLI."""
from __future__ import annotations

import copy
import datetime as dt
import shutil
import xml.etree.ElementTree as ET

import numpy as np
import pandas as pd
import pytest

from quant_duel import cli
from quant_duel import config as cfgmod
from quant_duel import experiment as ex
from quant_duel import replay as rp
from quant_duel.compare import stats
from quant_duel.compare.report import compare, equity_svg
from quant_duel.export.csv import export_node
from quant_duel.paper.books import BookConfig
from quant_duel.paper.daily import run_through
from quant_duel.paper.ledger import Ledger
from quant_duel.tuner.schema import check

from .conftest import ROOT, TICKERS

START, END = dt.date(2021, 9, 1), dt.date(2021, 9, 30)


# ============================================================
# experiment.yaml
# ============================================================

def test_the_experiment_file_is_frozen(tmp_path, cfg):
    path = tmp_path / "experiment.yaml"
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    shutil.copytree(ROOT / "nodes", tmp_path / "nodes")
    today = dt.date(2026, 10, 30)
    with pytest.raises(ex.ExperimentError, match="not in the future"):
        ex.init(path, tmp_path, cfg, dt.date(2026, 10, 1),
                dt.date(2026, 10, 30), today=today)
    with pytest.raises(ex.ExperimentError, match="not a trading day"):
        ex.init(path, tmp_path, cfg, dt.date(2026, 11, 1),
                dt.date(2026, 12, 1), today=today)
    e = ex.init(path, tmp_path, cfg, dt.date(2026, 11, 2),
                dt.date(2026, 12, 1), today=today)
    assert e.raw["trading_days"] == 21
    assert e.raw["primary_metric"]["name"] == "live_log_loss"
    with pytest.raises(ex.ExperimentError, match="never overwritten"):
        ex.init(path, tmp_path, cfg, dt.date(2026, 11, 2),
                dt.date(2026, 12, 1), today=today)
    loaded = ex.load(path)
    assert loaded.start == dt.date(2026, 11, 2)
    assert loaded.changed_files(tmp_path) == []
    (tmp_path / "config.yaml").write_text(
        (tmp_path / "config.yaml").read_text() + "\n# edited\n")
    assert loaded.changed_files(tmp_path) == ["config.yaml"]


# ============================================================
# Paired statistics with known answers
# ============================================================

def _preds(days=20, tickers=30, seed=0):
    rng = np.random.default_rng(seed)
    rows = [{"date": dt.date(2021, 1, 1) + dt.timedelta(days=d),
             "ticker": f"T{t}", "target": float(rng.random() < 0.5)}
            for d in range(days) for t in range(tickers)]
    return pd.DataFrame(rows)


def test_paired_log_loss_known_answers():
    base = _preds()
    good = base.assign(p=np.where(base["target"] == 1, 0.6, 0.4))
    coin = base.assign(p=0.5)
    r = stats.paired_log_loss(a=good, b=coin, resamples=2000)
    expect = np.log(0.6) - np.log(0.5)              # ll(coin) - ll(good)
    assert r["diff"] == pytest.approx(expect)
    assert r["ci_low"] > 0
    assert "predicted better" in stats.verdict(r, "x", "A predicted better.",
                                               "A predicted worse.")
    r = stats.paired_log_loss(a=coin, b=good, resamples=2000)
    assert r["ci_high"] < 0
    same = stats.paired_log_loss(a=coin, b=coin, resamples=200)
    assert same["identical"] and same["diff"] == 0
    assert "identical predictions" in stats.verdict(same, "x", "", "")


def test_noise_is_reported_as_noise():
    base = _preds(seed=1)
    rng = np.random.default_rng(2)
    a = base.assign(p=np.clip(0.5 + rng.normal(0, 0.02, len(base)), .01, .99))
    b = base.assign(p=np.clip(0.5 + rng.normal(0, 0.02, len(base)), .01, .99))
    r = stats.paired_log_loss(a, b, resamples=4000)
    assert r["ci_low"] < 0 < r["ci_high"]
    assert "NOT distinguishable" in stats.verdict(r, "x", "", "")


def test_outcomes_must_agree():
    base = _preds(days=3)
    a = base.assign(p=0.5)
    b = a.copy()
    b.loc[0, "target"] = 1 - b.loc[0, "target"]
    with pytest.raises(ValueError, match="disagree on outcomes"):
        stats.paired_log_loss(a, b)


def test_the_bootstrap_resamples_days():
    sums = np.array([1.0, 3.0])
    counts = np.array([1.0, 1.0])
    lo, hi = stats.day_bootstrap(sums, counts, 5000, 0, 0.95)
    assert lo == pytest.approx(1.0) and hi == pytest.approx(3.0)


def test_paired_returns():
    idx = [dt.date(2021, 1, d) for d in range(1, 21)]
    rng = np.random.default_rng(0)
    rb = pd.Series(rng.normal(0, 0.01, 20), index=idx)
    ra = rb + 0.002 + rng.normal(0, 0.0005, 20)
    r = stats.paired_returns(ra, rb, resamples=2000)
    assert r["mean_diff"] == pytest.approx(0.002, abs=3e-4)
    assert r["ci_low"] > 0 and r["t"] > 5


# ============================================================
# compare through the real pipeline, with a known answer
# ============================================================

def _cfgs(years=1):
    out = []
    for node in ("A", "B"):
        c = cfgmod.load(node, root=ROOT)
        c.raw["universe"]["tickers"] = TICKERS
        c.raw["backtest"].update(train_days=504, test_days=21,
                                 min_train_days=252, validation_years=years)
        out.append(c)
    return out


def _run_nodes(tmp_path, table, prices, sentiment_a=None, w=0.1):
    """Two ledgers over Sept 2021; node A optionally with sentiment."""
    dirs = {}
    for c in _cfgs():
        led = Ledger(tmp_path / c.node_id / "paper.sqlite")
        book = BookConfig.from_dict(dict(BookConfig.from_config(c).to_dict(),
                                         sentiment_weight=w))
        led.init(c.node_id, 100_000.0, book, "SPY")
        run_through(led, c.raw, table, prices, START, END,
                    hashes={d: "h" + str(d) for d in table["date"].unique()},
                    sentiment=sentiment_a if c.node_id == "A" else None)
        dirs[c.node_id] = tmp_path / "exports" / c.node_id
        export_node(led, dirs[c.node_id])
        led.close()
    return dirs


def _exp(tmp_path, cfg):
    return ex.init(tmp_path / "experiment.yaml", ROOT, cfg, START,
                   dt.date(2021, 9, 29), allow_past=True)


def test_compare_finds_news_that_really_helps(tmp_path, cfg, table, prices):
    """Known answer: node A's 'news' tells it tomorrow's direction (a
    stand-in for very good news); compare must say A predicted better —
    and every data check must pass."""
    t = table[(table["date"] >= START) & (table["date"] <= END)]
    oracle = pd.DataFrame({"date": t["date"], "ticker": t["ticker"],
                           "sentiment": 2 * t["target"] - 1, "n": 1})
    dirs = _run_nodes(tmp_path, table, prices, oracle)
    res = compare(dirs["A"], dirs["B"], _exp(tmp_path, cfg),
                  tmp_path / "out")
    assert res["warnings"] == []
    assert res["log_loss"]["ci_low"] > 0
    assert res["verdict"].startswith("Node A (news) predicted better")
    out = tmp_path / "out"
    for f in ("compare.md", "metrics.csv", "paired.csv", "price_check.csv",
              "tuner_timeline.csv", "equity_curves.csv", "equity.svg"):
        assert (out / f).exists(), f
    md = (out / "compare.md").read_text()
    assert "Checks passed" in md and "## Verdict" in md
    m = pd.read_csv(out / "metrics.csv")
    assert set(m["book"]) == {"live", "control", "spy"}


def test_compare_without_news_is_identical(tmp_path, cfg, table, prices):
    dirs = _run_nodes(tmp_path, table, prices, None)
    res = compare(dirs["A"], dirs["B"], _exp(tmp_path, cfg),
                  tmp_path / "out")
    assert res["log_loss"]["identical"]
    assert "identical predictions" in res["verdict"]


def test_compare_flags_different_prices_and_controls(tmp_path, cfg, table,
                                                     prices):
    dirs = _run_nodes(tmp_path, table, prices, None)
    ph = pd.read_csv(dirs["B"] / "price_hashes.csv")
    ph.loc[3, "price_hash"] = "tampered"
    ph.to_csv(dirs["B"] / "price_hashes.csv", index=False)
    pr = pd.read_csv(dirs["B"] / "predictions.csv")
    pr.loc[pr["book"] == "control", "p"] += 0.01
    pr.to_csv(dirs["B"] / "predictions.csv", index=False)
    res = compare(dirs["A"], dirs["B"], _exp(tmp_path, cfg),
                  tmp_path / "out")
    text = " ".join(res["warnings"])
    assert "PRICE DATA DIFFERS on 1" in text and "CONTROL books differ" in text
    assert "Checks that failed" in (tmp_path / "out" / "compare.md").read_text()


def test_compare_refuses_swapped_folders(tmp_path, cfg, table, prices):
    from quant_duel.compare.report import CompareError
    dirs = _run_nodes(tmp_path, table, prices, None)
    with pytest.raises(CompareError, match="expected A"):
        compare(dirs["B"], dirs["A"], _exp(tmp_path, cfg), tmp_path / "o")


def test_equity_svg_is_valid_and_labelled():
    df = pd.DataFrame({"date": [dt.date(2021, 9, d) for d in (1, 2, 3)],
                       "A live": [1.0, 1.01, 1.02], "B live": [1.0, 1.0, 0.99],
                       "control": [1.0, 1.005, 1.0], "SPY": [1, 1.02, 1.03]})
    svg = equity_svg(df, "Growth of 1")
    root = ET.fromstring(svg)
    ns = "{http://www.w3.org/2000/svg}"
    assert len(root.findall(f"{ns}polyline")) == 4
    texts = [t.text for t in root.iter(f"{ns}text")]
    assert any(t and t.startswith("A live 1.020") for t in texts)
    assert "prefers-color-scheme: dark" in svg


# ============================================================
# replay
# ============================================================

def test_the_random_proposer_only_makes_valid_proposals(cfg):
    book = BookConfig.from_config(cfg)
    prop = rp.random_proposer(cfg["tuner"]["bounds"], seed=5)
    for k in range(60):
        day = dt.date(2021, 1, 1) + dt.timedelta(days=k)
        p = prop("A", day, book)
        assert p == prop("B", day, book)            # same for both nodes
        check(p, book, cfg["tuner"]["bounds"], news=False)


def test_replay_runs_both_nodes_identically(tmp_path, table, prices):
    a, b = _cfgs()
    out = rp.new_folder(tmp_path, START, dt.date(2021, 9, 21))
    assert rp.new_folder(tmp_path, START, dt.date(2021, 9, 21)).name.endswith(
        "-2")
    prop = rp.random_proposer(a["tuner"]["bounds"], a["seed"])
    res = rp.run(a, b, table, prices, {}, START, dt.date(2021, 9, 21), out,
                 proposer=prop, write_reports=True)
    assert res["days"] == 14
    assert [d for d, _ in res["tunes"]["A"]] == [
        dt.date(2021, 9, 3), dt.date(2021, 9, 10), dt.date(2021, 9, 17),
        dt.date(2021, 9, 21)]
    assert res["tunes"]["A"] == res["tunes"]["B"]
    pa = pd.read_csv(out / "exports" / "A" / "predictions.csv")
    pb = pd.read_csv(out / "exports" / "B" / "predictions.csv")
    pd.testing.assert_frame_equal(pa.drop(columns="node"),
                                  pb.drop(columns="node"))
    assert (out / "reports" / "A" / "2021-09-21.md").exists()


def test_replay_cli_end_to_end(tmp_path, capsys):
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    shutil.copytree(ROOT / "nodes", tmp_path / "nodes")
    text = (tmp_path / "config.yaml").read_text()
    text = text.replace('history_start: "2015-01-01"',
                        'history_start: "2019-01-01"')
    text = text.replace("validation_years: 3", "validation_years: 1")
    (tmp_path / "config.yaml").write_text(text)
    base = ["--root", str(tmp_path)]
    assert cli.main(base + ["ingest", "--source", "synthetic", "--end",
                            "2023-03-31"]) == 0
    assert cli.main(base + ["replay", "--start", "2023-03-01", "--end",
                            "2023-03-10"]) == 0
    out = capsys.readouterr().out
    assert "replay check: PASS" in out
    assert list((tmp_path / "data" / "replay").glob(
        "2023-03-01_2023-03-10/compare/compare.md"))
    with pytest.raises(SystemExit, match="no experiment file"):
        cli.main(base + ["compare"])
