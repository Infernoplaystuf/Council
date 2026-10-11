"""Phase 5: news collection, timing, scoring, daily sentiment, the overlay
on node A's live book, and the cutoff canary."""
from __future__ import annotations

import copy
import datetime as dt
import shutil
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import pandas as pd
import pytest

from quant_duel import cli
from quant_duel import config as cfgmod
from quant_duel.llm.client import LLMError
from quant_duel.models.base import EPS
from quant_duel.news.feeds import FeedError, Item, parse
from quant_duel.news.mapping import map_tickers
from quant_duel.news.poll import poll
from quant_duel.news.score import parse_scores, score_pending
from quant_duel.news.sentiment import daily_sentiment, overlay
from quant_duel.news.store import NewsStore
from quant_duel.news.timing import (cutoff_at, effective_time, parse_time,
                                    sentiment_day)
from quant_duel.paper.books import BookConfig
from quant_duel.paper.daily import run_through, step
from quant_duel.paper.ledger import Ledger
from quant_duel.tuner import gate as gatemod
from quant_duel.tuner.run import tune

from .conftest import ROOT, TICKERS

UTC = dt.timezone.utc
DAY = dt.date(2021, 9, 14)            # a Tuesday


def et(day, hh, mm):
    """An aware UTC datetime for New York local time on ``day``."""
    from zoneinfo import ZoneInfo
    return dt.datetime(day.year, day.month, day.day, hh, mm,
                       tzinfo=ZoneInfo("America/New_York")).astimezone(UTC)


class FakeLLM:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, *, json_mode=False, max_tokens=None):
        self.calls.append(messages)
        r = self.replies.pop(0) if self.replies else LLMError("no more")
        if isinstance(r, Exception):
            raise r
        return r


# ============================================================
# Timing: which day a headline counts for
# ============================================================

@pytest.mark.parametrize("when, expect", [
    (et(DAY, 15, 59), DAY),                               # before the cutoff
    (et(DAY, 16, 0), dt.date(2021, 9, 15)),               # at it → next day
    (et(DAY, 23, 0), dt.date(2021, 9, 15)),
    (et(dt.date(2021, 9, 17), 17, 0), dt.date(2021, 9, 20)),   # Fri eve → Mon
    (et(dt.date(2021, 9, 18), 10, 0), dt.date(2021, 9, 20)),   # Saturday
    (et(dt.date(2024, 7, 4), 10, 0), dt.date(2024, 7, 5)),     # holiday
    (et(dt.date(2024, 1, 10), 15, 59), dt.date(2024, 1, 10)),  # winter (EST)
    (et(dt.date(2024, 7, 10), 15, 59), dt.date(2024, 7, 10)),  # summer (EDT)
])
def test_sentiment_day(when, expect):
    assert sentiment_day(when) == expect


def test_cutoff_follows_daylight_saving():
    assert cutoff_at(dt.date(2024, 1, 10)).hour == 21      # 16:00 EST
    assert cutoff_at(dt.date(2024, 7, 10)).hour == 20      # 16:00 EDT


def test_publish_times():
    assert parse_time("Tue, 14 Sep 2021 19:30:00 +0000") == \
        dt.datetime(2021, 9, 14, 19, 30, tzinfo=UTC)
    assert parse_time("2021-09-14T15:30:00-04:00") == \
        dt.datetime(2021, 9, 14, 19, 30, tzinfo=UTC)
    assert parse_time("2021-09-14T19:30:00Z").hour == 19
    assert parse_time("2021-09-14T19:30:00") is None       # no zone: refuse
    assert parse_time("yesterday") is None and parse_time("") is None
    fetched = dt.datetime(2021, 9, 14, 20, 0, tzinfo=UTC)
    future = fetched + dt.timedelta(hours=3)
    assert effective_time(future, fetched) == fetched      # pulled back
    assert effective_time(None, fetched) is None


# ============================================================
# Feeds and mapping
# ============================================================

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Apple   unveils new iPhone</title><link>http://x/1</link>
<pubDate>Tue, 14 Sep 2021 17:00:00 +0000</pubDate></item>
<item><title>Markets drift</title><link>http://x/2</link></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Nvidia (NVDA) beats estimates</title>
<link href="http://y/1"/><published>2021-09-14T12:00:00Z</published></entry>
</feed>"""


def test_rss_and_atom_parse():
    items = parse(RSS)
    assert [i.title for i in items] == ["Apple unveils new iPhone",
                                        "Markets drift"]
    assert items[0].raw_time.startswith("Tue") and items[1].raw_time is None
    a = parse(ATOM)[0]
    assert a.link == "http://y/1" and a.raw_time == "2021-09-14T12:00:00Z"


def test_hostile_or_broken_feeds_are_refused():
    bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><rss/>'
    with pytest.raises(FeedError, match="DOCTYPE"):
        parse(bomb)
    with pytest.raises(FeedError, match="not valid XML"):
        parse(b"<rss><channel>")


@pytest.mark.parametrize("title, expect", [
    ("Apple unveils a new iPhone", {"AAPL"}),
    ("Pineapple prices soar", set()),
    ("$TSLA jumps; Tesla deliveries up", {"TSLA"}),
    ("Nvidia (NVDA) and Microsoft rally", {"NVDA", "MSFT"}),
    ("NYSE: V hits a high", {"V"}),
    ("Visa and Mastercard face probe", {"V", "MA"}),
    ("Value stocks lead", set()),
    ("Berkshire Hathaway (BRK-B) buys more", {"BRK-B"}),
    ("Coca-Cola raises dividend", {"KO"}),
])
def test_ticker_mapping(title, expect):
    real = cfgmod.load(None, root=ROOT)          # the real universe
    assert map_tickers(title, real.tickers, real["news"]["aliases"]) == expect


# ============================================================
# Store and poll
# ============================================================

def _store(tmp_path):
    return NewsStore(tmp_path / "news.sqlite")


def _add(store, title, when, tickers, source="feed"):
    raw = when.strftime("%a, %d %b %Y %H:%M:%S +0000") if when else None
    store.add(source, [Item(title, "http://x", raw)], {title: tickers},
              fetched=(when or dt.datetime(2021, 9, 14, tzinfo=UTC))
              + dt.timedelta(minutes=5))


def test_the_same_story_from_two_feeds_is_stored_once(tmp_path):
    s = _store(tmp_path)
    _add(s, "Apple beats", et(DAY, 14, 0), ["AAPL"], "ticker:AAPL")
    _add(s, "apple  BEATS", et(DAY, 13, 0), ["MSFT"], "general")
    h = s.frame("headlines")
    assert len(h) == 1
    assert h["published"][0] == et(DAY, 13, 0).isoformat()   # earliest
    assert sorted(s.pairs()["ticker"]) == ["AAPL", "MSFT"]
    assert (s.pairs()["status"] == "pending").all()


def test_poll_maps_feeds_and_survives_a_broken_feed(tmp_path, cfg):
    s = _store(tmp_path)
    news = dict(cfg["news"], ticker_feed="http://feeds/{ticker}",
                feeds=[{"name": "general", "url": "http://general"}])

    def fetcher(url, timeout):
        if url == "http://feeds/AAPL":
            return RSS
        if url == "http://general":
            return ATOM
        raise FeedError("connection refused")
    now = dt.datetime(2021, 9, 14, 20, 0, tzinfo=UTC)
    rep = poll(s, news, ["AAPL", "MSFT", "NVDA", "SPY"], fetcher,
               now=lambda: now)
    assert rep["sources"] == 4 and rep["new"] == 3        # SPY feed skipped
    assert len(rep["errors"]) == 2                        # MSFT, NVDA feeds
    pairs = s.pairs()
    assert set(zip(pairs["title"], pairs["ticker"])) == {
        ("Apple unveils new iPhone", "AAPL"), ("Markets drift", "AAPL"),
        ("Nvidia (NVDA) beats estimates", "NVDA")}
    m = s.frame("headline_tickers").set_index("ticker")["method"]
    assert m["NVDA"] == "title"
    polls = s.frame("polls")
    assert polls["error"].notna().sum() == 2


# ============================================================
# Scoring
# ============================================================

def test_parse_scores_keeps_only_valid_items():
    obj = {"scores": [{"id": 0, "s": 0.5}, {"id": 1, "s": 2.0},
                      {"id": 2, "s": "0.3"}, {"id": 9, "s": 0.1},
                      {"id": True, "s": 0.1}, {"id": 3, "s": float("nan")},
                      "junk", {"id": 4, "s": -1}]}
    assert parse_scores(obj, [0, 1, 2, 3, 4]) == {0: 0.5, 4: -1.0}
    assert parse_scores({"scores": "none"}, [0]) == {}


def test_scoring_in_batches_with_retries_and_a_cap(tmp_path, cfg):
    s = _store(tmp_path)
    for k in range(5):
        _add(s, f"Apple story {k}", et(DAY, 9 + k, 0), ["AAPL"])
    news = dict(cfg["news"], max_per_ticker_day=3, batch_size=2,
                max_score_attempts=2)
    # batch 1: both scored; batch 2: one valid, one missing
    llm = FakeLLM('{"scores": [{"id": 0, "s": 0.5}, {"id": 1, "s": -0.5}]}',
                  '{"scores": [{"id": 0, "s": 0.2}]}')
    rep = score_pending(s, llm, news)
    assert rep == {"scored": 3, "unscored": 0, "capped": 2, "batches": 2,
                   "llm_failed": 0}
    st = s.pairs().set_index("title")["status"]
    # the three most recent are kept, the two oldest are capped
    assert set(st[st == "capped"].index) == {"Apple story 0", "Apple story 1"}
    sent = llm.calls[0][1]["content"]
    assert '"ticker": "AAPL"' in sent and "Apple story 4" in sent


def test_unscored_items_retry_then_fail_and_a_down_llm_changes_nothing(
        tmp_path, cfg):
    s = _store(tmp_path)
    _add(s, "Apple story", et(DAY, 10, 0), ["AAPL"])
    news = dict(cfg["news"], max_score_attempts=2)
    score_pending(s, FakeLLM('{"scores": []}'), news)
    assert s.pairs()["status"][0] == "pending"
    score_pending(s, FakeLLM(LLMError("down"), LLMError("down")), news)
    assert s.pairs()["attempts"][0] == 1                  # LLM down: no change
    score_pending(s, FakeLLM('{"scores": []}'), news)
    assert s.pairs()["status"][0] == "failed"


# ============================================================
# Daily sentiment and the cutoff canary
# ============================================================

def _scored(tmp_path, rows):
    """rows: (title, published, ticker, score)."""
    s = _store(tmp_path)
    for title, when, t, score in rows:
        _add(s, title, when, [t])
    hid = s.pairs().set_index("title")["headline_id"]
    s.set_scores([{"headline_id": hid[title], "ticker": t, "score": score,
                   "max_attempts": 3} for title, _, t, score in rows], "m")
    return s


def test_daily_sentiment_means_and_cap(tmp_path):
    s = _scored(tmp_path, [
        ("a", et(DAY, 9, 0), "AAPL", 1.0),
        ("b", et(DAY, 10, 0), "AAPL", 0.0),
        ("c", et(DAY, 11, 0), "AAPL", -0.5),
        ("d", et(dt.date(2021, 9, 13), 17, 0), "AAPL", 0.8),   # Monday eve
        ("e", et(DAY, 12, 0), "MSFT", 0.4)])
    d = daily_sentiment(s.pairs(), cap=3).set_index(["date", "ticker"])
    # four headlines count for Tuesday (d from Monday evening is the oldest);
    # the cap keeps the newest three: c, b, a
    assert d.loc[(DAY, "AAPL"), "sentiment"] == pytest.approx(
        (1.0 + 0.0 - 0.5) / 3)
    d4 = daily_sentiment(s.pairs(), cap=4).set_index(["date", "ticker"])
    assert d4.loc[(DAY, "AAPL"), "sentiment"] == pytest.approx(1.3 / 4)
    assert d.loc[(DAY, "AAPL"), "n"] == 3
    assert d.loc[(DAY, "MSFT"), "sentiment"] == pytest.approx(0.4)


def test_canary_a_headline_after_the_cutoff_does_not_affect_that_day(
        tmp_path):
    early = [("before", et(DAY, 15, 59), "AAPL", 0.2)]
    s1 = _scored(tmp_path / "1", early)
    s2 = _scored(tmp_path / "2", early + [("late", et(DAY, 16, 0), "AAPL",
                                           1.0)])
    d1 = daily_sentiment(s1.pairs())
    d2 = daily_sentiment(s2.pairs())
    pd.testing.assert_frame_equal(d1[d1["date"] == DAY],
                                  d2[d2["date"] == DAY])
    nxt = d2[d2["date"] == dt.date(2021, 9, 15)]
    assert nxt["sentiment"].tolist() == [1.0]     # it counts tomorrow


def test_overlay_is_bounded():
    p = np.array([0.5, 0.99995, 0.00005, 0.6])
    out = overlay(p, np.array([1.0, 1.0, -1.0, np.nan]), 0.05)
    assert out[0] == pytest.approx(0.55)
    assert out[1] == 1 - EPS and out[2] == EPS and out[3] == 0.6


# ============================================================
# The overlay in the paper ledger
# ============================================================

def _raw(cfg, w=0.05):
    raw = copy.deepcopy(cfg.raw)
    raw["backtest"]["validation_years"] = 1
    raw["news"]["sentiment_weight"] = w
    return raw


def _ledger(path, cfg, w=0.05):
    led = Ledger(path / "paper.sqlite")
    book = BookConfig.from_dict(dict(BookConfig.from_config(cfg).to_dict(),
                                     sentiment_weight=w))
    led.init("A", 100_000.0, book, "SPY")
    return led


def test_the_overlay_moves_only_the_live_book(tmp_path, cfg, table, prices):
    sent = pd.DataFrame({"date": [DAY, DAY], "ticker": ["AAA", "BBB"],
                         "sentiment": [1.0, -0.5], "n": [2, 1]})
    led = _ledger(tmp_path, cfg)
    step(led, _raw(cfg), table, prices, DAY, sentiment=sent)
    p = led.frame("predictions").set_index(["book", "ticker"])
    live, ctrl = p.loc["live"], p.loc["control"]
    assert live.loc["AAA", "p"] == pytest.approx(
        min(live.loc["AAA", "p_base"] + 0.05, 1 - EPS))
    assert live.loc["BBB", "p"] == pytest.approx(
        live.loc["BBB", "p_base"] - 0.025)
    assert np.isnan(live.loc["CCC", "sentiment"])
    assert live.loc["CCC", "p"] == live.loc["CCC", "p_base"]
    assert ctrl["p_base"].isna().all() and ctrl["sentiment"].isna().all()
    assert np.allclose(ctrl["p"], live["p_base"])         # same base model


def test_canary_end_to_end_a_late_headline_never_reaches_that_days_trade(
        tmp_path, cfg, table, prices):
    rows = [("before", et(DAY, 11, 0), "AAA", 0.6)]
    late = rows + [("late", et(DAY, 16, 30), "AAA", -1.0)]
    out = []
    for name, r in (("1", rows), ("2", late)):
        sent = daily_sentiment(_scored(tmp_path / name, r).pairs())
        led = _ledger(tmp_path / name, cfg)
        step(led, _raw(cfg), table, prices, DAY, sentiment=sent)
        out.append(led.frame("predictions"))
    pd.testing.assert_frame_equal(out[0], out[1])


def test_node_b_never_reads_news(tmp_path, cfg):
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    shutil.copytree(ROOT / "nodes", tmp_path / "nodes")
    for node in ("A", "B"):
        s = NewsStore(tmp_path / "data" / node / "news.sqlite")
        _add(s, "Apple soars", et(DAY, 10, 0), ["AAPL"])
        s.close()
    b = cfgmod.load("B", root=tmp_path)
    a = cfgmod.load("A", root=tmp_path)
    assert cli.load_sentiment(b) is None
    assert cli.load_sentiment(a) is not None
    for cmd in ("news-poll", "news-score", "news-status"):
        with pytest.raises(SystemExit, match="never sees news"):
            cli.main(["--root", str(tmp_path), "--node", "B", cmd])


# ============================================================
# The tuner and the sentiment weight
# ============================================================

def _cheat_sentiment(table, days):
    """Sentiment that agrees with the next day's direction 65% of the time
    — a stand-in for useful news, to exercise the gate (not realistic)."""
    rng = np.random.default_rng(3)
    t = table[table["date"].isin(days) & table["target"].notna()]
    right = rng.random(len(t)) < 0.65
    s = np.where(right, 2 * t["target"] - 1, 1 - 2 * t["target"])
    return pd.DataFrame({"date": t["date"].to_numpy(),
                         "ticker": t["ticker"].to_numpy(),
                         "sentiment": s * 0.8, "n": 3})


def test_the_gate_judges_a_weight_on_sentiment_days_only(table, cfg):
    raw = _raw(cfg, w=0.0)
    book = BookConfig.from_config(cfg)
    base = BookConfig.from_dict(dict(book.to_dict(), sentiment_weight=0.0))
    more = BookConfig.from_dict(dict(book.to_dict(), sentiment_weight=0.08))
    dates = sorted(d for d in table["date"].unique() if d < DAY)
    short = _cheat_sentiment(table, dates[-5:])
    with pytest.raises(ValueError, match="only 5 days of sentiment"):
        gatemod.gate(table, base, more, raw, 0, DAY, sentiment=short)
    with pytest.raises(ValueError, match="only 0 days"):
        gatemod.gate(table, base, more, raw, 0, DAY, sentiment=None)
    good = _cheat_sentiment(table, dates[-40:])
    v = gatemod.gate(table, base, more, raw, 0, DAY, sentiment=good)
    assert v.adopt and v.improvement > 0.005
    # the improvement is measured on the 40 sentiment days, not diluted
    # over the whole year
    noise = good.assign(sentiment=np.random.default_rng(1).uniform(
        -1, 1, len(good)))
    v = gatemod.gate(table, base, more, raw, 0, DAY, sentiment=noise)
    assert not v.adopt


def test_tuning_the_weight_on_node_a(tmp_path, cfg, table, prices):
    raw = _raw(cfg, w=0.0)
    led = _ledger(tmp_path, cfg, w=0.0)
    run_through(led, raw, table, prices, dt.date(2021, 9, 1), DAY)
    dates = sorted(d for d in table["date"].unique() if d < DAY)
    good = _cheat_sentiment(table, dates[-40:])
    llm = FakeLLM('{"changes": {"sentiment_weight": 0.08}, "reason": "news '
                  'helps"}')
    row = tune(led, raw, table, DAY, news=True, llm=llm, sentiment=good)
    assert row["status"] == "adopted", row
    assert led.book_config("live").sentiment_weight == 0.08
    assert led.book_config("control").sentiment_weight == 0.0
    assert '"SENTIMENT_HISTORY"' in llm.calls[0][1]["content"]
    # node B: the same reply is invalid, and sentiment is ignored
    led_b = Ledger(tmp_path / "b.sqlite")
    led_b.init("B", 100_000.0, BookConfig.from_config(cfg), "SPY")
    run_through(led_b, raw, table, prices, dt.date(2021, 9, 1), DAY)
    llm = FakeLLM('{"changes": {"sentiment_weight": 0.08}}')
    row = tune(led_b, raw, table, DAY, news=False, llm=llm, sentiment=good)
    assert row["status"] == "invalid"
    assert "SENTIMENT" not in llm.calls[0][1]["content"]


# ============================================================
# CLI on node A
# ============================================================

class _Feed:
    def __init__(self, body):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/rss+xml")
                self.end_headers()
                self.wfile.write(body)
        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_news_cli_on_node_a(tmp_path, capsys):
    feed = _Feed(RSS)
    try:
        shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
        shutil.copytree(ROOT / "nodes", tmp_path / "nodes")
        text = (tmp_path / "config.yaml").read_text()
        text = text.replace(
            'ticker_feed: "https://feeds.finance.yahoo.com/rss/2.0/headline'
            '?s={ticker}&region=US&lang=en-US"',
            f'ticker_feed: "{feed.url}/{{ticker}}"')
        text = text.replace("url: http://localhost:11434/v1",
                            "url: http://127.0.0.1:9/v1")
        text = text.replace("retries: 2", "retries: 0")
        text = text.replace('history_start: "2015-01-01"',
                            'history_start: "2019-01-01"')
        (tmp_path / "config.yaml").write_text(text)
        base = ["--root", str(tmp_path), "--node", "A"]
        assert cli.main(base + ["news-status"]) == 0
        assert "no headlines yet" in capsys.readouterr().out
        assert cli.main(base + ["news-poll"]) == 0
        out = capsys.readouterr().out
        assert "news-poll: 40 feeds" in out and "2 new" in out
        assert "LLM unavailable" in out
        assert cli.main(base + ["news-status"]) == 0
        assert "2 headlines" in capsys.readouterr().out
        assert cli.main(base + ["daily", "--source", "synthetic", "--start",
                                "2023-03-01", "--end", "2023-03-02"]) == 0
        ex = tmp_path / "exports" / "A"
        heads = pd.read_csv(ex / "headlines.csv")
        # 2 stories x 40 ticker feeds; the undated one is kept, never counts
        assert len(heads) == 80
        assert heads[heads["title"] == "Markets drift"]["day"].isna().all()
        assert (ex / "sentiment_daily.csv").exists()
        led = Ledger(tmp_path / "data" / "A" / "paper.sqlite")
        p = led.frame("predictions")
        led.close()
        live = p[p["book"] == "live"]
        assert live["p_base"].notna().all()
        assert np.allclose(live["p"], live["p_base"])     # nothing scored
    finally:
        feed.close()


def test_the_report_mentions_news_on_node_a(tmp_path, cfg, table, prices):
    from quant_duel.report.daily import facts, write_report
    sent = pd.DataFrame({"date": [DAY], "ticker": ["AAA"],
                         "sentiment": [1.0], "n": [2]})
    led = _ledger(tmp_path, cfg)
    step(led, _raw(cfg), table, prices, DAY, sentiment=sent)
    f = facts(led, DAY)
    assert f["news"]["tickers_with_sentiment"] == 1
    text = write_report(led, DAY, tmp_path / "r", None).read_text()
    assert "News: sentiment for 1 tickers" in text
