"""The Model Arena: weights, fills, marks, and the morning conversation."""
import datetime as dt
import json

import pandas as pd
import pytest

from quant_duel.arena import book as bk
from quant_duel.arena import trader
from quant_duel.arena.cli import plan, NY

D1, D2, D3 = dt.date(2026, 11, 4), dt.date(2026, 11, 5), dt.date(2026, 11, 6)


def bars(rows):
    df = pd.DataFrame(rows, columns=["open", "close"],
                      index=pd.Index([D1, D2, D3], name="date"))
    for c in ("high", "low", "adj_close"):
        df[c] = df["close"]
    df["volume"] = 1e6
    return df


PRICES = {"AAA": bars([(10, 11), (11, 12), (12, 12)]),
          "BBB": bars([(50, 50), (50, 40), (40, 40)])}


def test_clean_weights_drops_unknown_and_scales_over_100():
    w = bk.clean_weights({"aaa": "80%", "BBB": 70, "ZZZ": 10, "CCC": -5},
                         ["AAA", "BBB"])
    assert set(w) == {"AAA", "BBB"}
    assert sum(w.values()) == pytest.approx(100)
    assert bk.clean_weights([{"ticker": "AAA", "percent": 30}],
                            ["AAA"]) == {"AAA": 30}


def test_fill_at_open_with_costs_then_marks():
    b = bk.new_book("T", "m", 10000)
    b["orders"].append({"fill_day": str(D1), "action": "trade",
                        "weights": {"AAA": 50}, "status": "pending"})
    bk.advance(b, PRICES, D1, D3, lambda d: True, cost_bps=10)
    assert b["orders"][0]["status"] == "filled"
    shares = b["shares"]["AAA"]
    assert shares * 10 == pytest.approx(5000 * (1 - 0.001), rel=1e-3)
    assert b["cash"] == pytest.approx(10000 - shares * 10 * 1.001, abs=0.01)
    assert b["marks"][str(D2)] == pytest.approx(b["cash"] + shares * 12)
    assert b["cash"] >= 0


def test_hold_and_waiting_for_bars():
    b = bk.new_book("T", "m", 10000)
    b["orders"].append({"fill_day": str(D2), "action": "hold",
                        "status": "pending"})
    bk.advance(b, PRICES, D1, D3, lambda d: d < D2, cost_bps=5)
    assert b["orders"][0]["status"] == "pending"        # no bar for D2 yet
    assert list(b["marks"]) == [str(D1)]
    bk.advance(b, PRICES, D1, D3, lambda d: True, cost_bps=5)
    assert b["orders"][0]["status"] == "filled"
    assert b["marks"][str(D3)] == 10000


def test_morning_requests_then_trades():
    replies = iter([
        json.dumps({"action": "request", "requests": [
            {"tool": "history", "ticker": "AAA"},
            {"tool": "headlines", "ticker": "AAA"}]}),
        "not json at all",
        json.dumps({"action": "trade", "holdings": {"AAA": 40},
                    "reason": "up"})])
    asked = []
    dec, msgs = trader.morning(lambda m: next(replies), "sys", "packet",
                               lambda r: asked.append(r) or "data",
                               max_requests=5, max_rounds=4)
    assert dec["action"] == "trade" and dec["holdings"] == {"AAA": 40}
    assert len(asked) == 2 and len(dec["requests"]) == 2


def test_morning_request_cap_forces_a_decision():
    calls = []

    def chat(m):
        calls.append(m)
        return json.dumps({"action": "request", "requests": [
            {"tool": "history", "ticker": "AAA"}] * 3})
    dec, _ = trader.morning(chat, "s", "p", lambda r: "d", max_requests=5,
                            max_rounds=4)
    assert dec["action"] == "error"
    assert len(dec["requests"]) == 5


def test_plan_trades_only_before_the_open_on_a_trading_day():
    at = lambda s: dt.datetime.fromisoformat(s).replace(tzinfo=NY)
    assert plan(at("2026-11-09T02:00")) == dt.date(2026, 11, 9)   # Monday
    assert plan(at("2026-11-09T10:00")) is None                   # after open
    assert plan(at("2026-11-08T02:00")) is None                   # Sunday
    assert plan(at("2026-11-26T02:00")) is None                   # Thanksgiving
