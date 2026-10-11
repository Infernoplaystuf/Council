"""Phase 4: the LLM client and JSON handling, the tuner schema and bounds,
the validation gate, the change log, and the daily report."""
from __future__ import annotations

import copy
import datetime as dt
import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import numpy as np
import pandas as pd
import pytest

from quant_duel import cli
from quant_duel.llm.client import (LLMError, OpenAIClient, chat_json,
                                   extract_json)
from quant_duel.paper.books import BookConfig
from quant_duel.paper.daily import run_through
from quant_duel.paper.ledger import Ledger
from quant_duel.report.daily import clean_summary, facts, write_report
from quant_duel.tuner import gate as gatemod
from quant_duel.tuner.run import prompt, tune
from quant_duel.tuner.schema import ProposalError, check

from .conftest import ROOT

CUTOFF = dt.date(2021, 9, 14)


def _raw(cfg, years=1):
    raw = copy.deepcopy(cfg.raw)
    raw["backtest"]["validation_years"] = years
    return raw


class FakeLLM:
    """Scripted replies (a string, or an exception to raise)."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, *, json_mode=False, max_tokens=None):
        self.calls.append(messages)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def ledger(tmp_path, cfg, table, prices):
    led = Ledger(tmp_path / "paper.sqlite")
    led.init("A", 100_000.0, BookConfig.from_config(cfg), "SPY")
    run_through(led, _raw(cfg), table, prices, dt.date(2021, 9, 1), CUTOFF)
    yield led
    led.close()


# ============================================================
# LLM JSON
# ============================================================

@pytest.mark.parametrize("text", [
    '{"changes": {"threshold": 0.53}, "reason": "x"}',
    '```json\n{"changes": {"threshold": 0.53}, "reason": "x"}\n```',
    'Sure! Here it is: {"changes": {"threshold": 0.53}, "reason": "x"} Hope '
    'that helps.',
])
def test_json_is_found_in_common_reply_shapes(text):
    assert extract_json(text)["changes"] == {"threshold": 0.53}


@pytest.mark.parametrize("text, why", [
    ("", "empty"), ("   ", "empty"),
    ("I think you should lower the threshold.", "no JSON"),
    ('{"changes": {"threshold": 0.5', "not valid"),       # truncated
    ('{"changes": {"threshold": 0.5}', "not valid"),      # unbalanced
    ('[{"changes": {}}]', "not an object"),
    ('{"changes": {"threshold": NaN}}', "not valid"),
    ('{"a": 1} {"b": 2}', "not valid"),
    ("{'changes': {}}", "not valid"),                    # Python, not JSON
])
def test_malformed_replies_are_refused(text, why):
    with pytest.raises(LLMError, match=why):
        extract_json(text)


def test_chat_json_re_asks_once_then_gives_up():
    llm = FakeLLM("no idea", '{"changes": {}}')
    obj, raw = chat_json(llm, [{"role": "user", "content": "x"}])
    assert obj == {"changes": {}} and len(llm.calls) == 2
    assert "not usable" in llm.calls[1][-1]["content"]
    with pytest.raises(LLMError):
        chat_json(FakeLLM("nope", "still nope"), [])


class _Server:
    """A local OpenAI-compatible server with scripted behaviour."""

    def __init__(self, script):
        self.script = list(script)
        self.bodies = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers["Content-Length"])
                outer.bodies.append(json.loads(self.rfile.read(n)))
                code, content = outer.script.pop(0)
                payload = json.dumps({"choices": [{"message": {
                    "role": "assistant", "content": content}}]}).encode() \
                    if code == 200 else b"error"
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)
        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_the_http_client_retries_and_sends_json_mode():
    srv = _Server([(500, ""), (200, '{"ok": true}')])
    try:
        c = OpenAIClient(srv.url, "m", retries=2, backoff_s=0.01)
        out = c.chat([{"role": "user", "content": "hi"}], json_mode=True)
        assert json.loads(out) == {"ok": True}
        assert len(srv.bodies) == 2
        assert srv.bodies[1]["response_format"] == {"type": "json_object"}
        assert srv.bodies[1]["temperature"] == 0.0
    finally:
        srv.close()


def test_the_http_client_fails_cleanly_when_the_server_is_down():
    srv = _Server([])
    url = srv.url
    srv.close()
    c = OpenAIClient(url, "m", retries=1, backoff_s=0.01, timeout_s=2)
    with pytest.raises(LLMError, match="failed"):
        c.chat([{"role": "user", "content": "hi"}])


def test_a_remote_llm_url_is_refused_unless_opted_in():
    with pytest.raises(LLMError, match="not on this machine"):
        OpenAIClient("https://api.example.com/v1", "m")
    OpenAIClient("http://192.168.1.20:8080/v1", "m", allow_remote=True)
    OpenAIClient("http://localhost:11434/v1", "m")


# ============================================================
# Schema and bounds
# ============================================================

@pytest.fixture
def book(cfg):
    return BookConfig.from_config(cfg)


@pytest.fixture
def bounds(cfg):
    return cfg["tuner"]["bounds"]


def test_a_valid_proposal(book, bounds):
    c = check({"changes": {"threshold": 0.54, "model_params": {"C": 0.05},
                           "features_enabled": {"rsi": False}},
               "reason": "less noise"}, book, bounds, news=False)
    assert c.book.threshold == 0.54 and c.book.model_params["C"] == 0.05
    assert c.book.features_enabled["rsi"] is False
    assert set(c.changed) == {"threshold", "model_params.C",
                              "features_enabled.rsi"}
    assert c.book.refit_days == book.refit_days      # untouched


@pytest.mark.parametrize("changes, why", [
    ({"threshold": 0.9}, "outside"),
    ({"threshold": "0.53"}, "must be a number"),
    ({"threshold": True}, "must be a number"),
    ({"train_days": 500.5}, "whole number"),
    ({"train_days": 100}, "outside"),
    ({"model_kind": "boosting"}, "cannot change"),
    ({"seed": 1}, "cannot change"),
    ({"cost_bps": 0}, "cannot change"),
    ({"model_params": {"max_depth": 4}}, "not tunable for logistic"),
    ({"model_params": {"C": 1e9}}, "outside"),
    ({"model_params": "C=1"}, "must be an object"),
    ({"features_enabled": {"news": True}}, "unknown feature group"),
    ({"features_enabled": {"rsi": "no"}}, "true or false"),
    ({"features_enabled": {g: False for g in (
        "returns", "ma_ratio", "volatility", "rsi", "volume_z",
        "day_of_week")}}, "at least 2"),
    ({"sentiment_weight": 0.05}, "only for the news node"),
    ({"threshold": 0.52}, "changes nothing"),         # the current value
    ({"threshold": 0.55, "train_days": 600, "model_params": {"C": 1.0},
      "features_enabled": {"rsi": False}}, "at most 3"),
])
def test_proposals_outside_the_schema_are_rejected(book, bounds, changes, why):
    with pytest.raises(ProposalError, match=why):
        check({"changes": changes}, book, bounds, news=False)


def test_schema_top_level_and_news_node(book, bounds):
    with pytest.raises(ProposalError, match="unknown top-level"):
        check({"changes": {"threshold": 0.55}, "extra": 1}, book, bounds,
              news=False)
    with pytest.raises(ProposalError, match="'changes'"):
        check({"threshold": 0.55}, book, bounds, news=False)
    c = check({"changes": {"sentiment_weight": 0.05}}, book, bounds,
              news=True)
    assert c.book.sentiment_weight == 0.05
    with pytest.raises(ProposalError, match="outside"):
        check({"changes": {"sentiment_weight": 0.5}}, book, bounds, news=True)


def test_integer_parameters_accept_whole_floats(cfg, bounds):
    b = BookConfig.from_dict(dict(BookConfig.from_config(cfg).to_dict(),
                                  model_kind="boosting",
                                  model_params=dict(cfg["model"]["params"]
                                                    ["boosting"])))
    c = check({"changes": {"model_params": {"max_depth": 4.0}}}, b, bounds,
              news=False)
    assert c.book.model_params["max_depth"] == 4
    assert isinstance(c.book.model_params["max_depth"], int)


# ============================================================
# The validation gate
# ============================================================

def _with_signal(table, seed=0):
    """A copy whose target really depends on ret_1 (mean reversion, 60/40)
    — honest, not leaky: ret_1 is known at the close of day t."""
    t = table.copy()
    rng = np.random.default_rng(seed)
    lab = t["target"].notna()
    revert = (t.loc[lab, "ret_1"] < 0).to_numpy()
    flip = rng.random(lab.sum()) < 0.4
    t.loc[lab, "target"] = np.where(flip, ~revert, revert).astype(float)
    return t


def test_the_gate_adopts_a_real_improvement(table, cfg, book):
    t = _with_signal(table)
    off = BookConfig.from_dict(dict(book.to_dict(), features_enabled=dict(
        book.features_enabled, returns=False)))
    on = BookConfig.from_dict(dict(off.to_dict(), features_enabled=dict(
        off.features_enabled, returns=True)))
    v = gatemod.gate(t, off, on, _raw(cfg), seed=0, cutoff=CUTOFF)
    assert v.adopt and v.improvement > 0.003 and v.edge_t > 2
    # and the reverse is refused
    v = gatemod.gate(t, on, off, _raw(cfg), seed=0, cutoff=CUTOFF)
    assert not v.adopt and v.improvement < 0


def test_the_gate_needs_consistency_not_just_the_margin(table, cfg, book):
    """An improvement over the margin that is not consistent across days
    (t below tuner.min_edge_t) is refused."""
    t = _with_signal(table)
    off = BookConfig.from_dict(dict(book.to_dict(), features_enabled=dict(
        book.features_enabled, returns=False)))
    on = BookConfig.from_dict(dict(off.to_dict(), features_enabled=dict(
        off.features_enabled, returns=True)))
    raw = _raw(cfg)
    raw["tuner"]["min_edge_t"] = 50.0
    v = gatemod.gate(t, off, on, raw, seed=0, cutoff=CUTOFF)
    assert v.improvement > v.margin and v.edge_t < 50 and not v.adopt


def test_the_gate_refuses_no_change_and_threshold_only(table, cfg, book):
    v = gatemod.gate(table, book, book, _raw(cfg), seed=0, cutoff=CUTOFF)
    assert v.improvement == 0 and not v.adopt
    t2 = BookConfig.from_dict(dict(book.to_dict(), threshold=0.58))
    v = gatemod.gate(table, book, t2, _raw(cfg), seed=0, cutoff=CUTOFF)
    assert v.improvement == 0 and not v.adopt


def test_the_gate_never_sees_data_after_the_cutoff(table, cfg, book):
    """Scramble every row after the cutoff (and the cutoff day's labels):
    the verdict must not move."""
    t = _with_signal(table)
    other = BookConfig.from_dict(dict(book.to_dict(), model_params={"C": 1.0}))
    v1 = gatemod.gate(t, book, other, _raw(cfg), 0, CUTOFF)
    poisoned = t.copy()
    later = poisoned["date"] > CUTOFF
    rng = np.random.default_rng(9)
    poisoned.loc[later, "target"] = rng.integers(0, 2, later.sum())
    poisoned.loc[later, "ret_1"] = rng.normal(size=later.sum())
    poisoned.loc[poisoned["date"] == CUTOFF, "target"] = 1.0
    v2 = gatemod.gate(poisoned, book, other, _raw(cfg), 0, CUTOFF)
    assert v1.to_dict() == v2.to_dict()
    assert v1.end < str(CUTOFF)           # the cutoff day has no label yet


def test_the_validation_window_is_the_configured_length(table, cfg, book):
    r = gatemod.walk_forward(table, book, _raw(cfg, years=2), 0, CUTOFF)
    assert r.pred["date"].nunique() == 2 * 252
    with pytest.raises(ValueError, match="not enough history"):
        gatemod.walk_forward(table, book, _raw(cfg, years=8), 0, CUTOFF)


# ============================================================
# A tuning round and the change log
# ============================================================

def test_a_round_with_an_llm_failure_is_logged_and_harmless(ledger, cfg,
                                                            table):
    before = ledger.book_config("live")
    row = tune(ledger, _raw(cfg), table, CUTOFF, news=False,
               llm=FakeLLM(LLMError("connection refused")))
    assert row["status"] == "llm_failed"
    assert "refused" in row["errors"]
    assert ledger.book_config("live") == before
    assert ledger.frame("tuner_log")["status"].tolist() == ["llm_failed"]


def test_an_invalid_llm_proposal_is_logged_with_its_reasons(ledger, cfg,
                                                            table):
    llm = FakeLLM('{"changes": {"threshold": 0.95, "seed": 3}}')
    row = tune(ledger, _raw(cfg), table, CUTOFF, news=False, llm=llm)
    assert row["status"] == "invalid"
    assert "outside" in row["errors"] and "seed" in row["errors"]
    assert row["raw_reply"].startswith('{"changes"')


def test_no_change_and_rejection(ledger, cfg, table):
    row = tune(ledger, _raw(cfg), table, CUTOFF, news=False,
               llm=FakeLLM('{"changes": {}, "reason": "all fine"}'))
    assert row["status"] == "no_change" and row["reason"] == "all fine"
    row = tune(ledger, _raw(cfg), table, CUTOFF, news=False,
               proposal={"changes": {"threshold": 0.55}})
    assert row["status"] == "rejected" and row["improvement"] == 0
    assert ledger.book_config("live").threshold == 0.52


def test_adoption_updates_live_only_and_once_a_week(ledger, cfg, table,
                                                    prices):
    t = _with_signal(table)
    raw = _raw(cfg)
    # start the live book without the return (and RSI) features
    conf = ledger.book_config("live")
    off = BookConfig.from_dict(dict(conf.to_dict(), features_enabled=dict(
        conf.features_enabled, returns=False, rsi=False)))
    with ledger.transaction():
        ledger.set_book_config("live", off)
    llm = FakeLLM('{"changes": {"features_enabled": {"returns": true}}, '
                  '"reason": "returns carry signal"}')
    row = tune(ledger, raw, t, CUTOFF, news=False, llm=llm)
    assert row["status"] == "adopted", row
    assert ledger.book_config("live").features_enabled["returns"] is True
    assert ledger.book_config("control") == BookConfig.from_config(cfg)
    log = ledger.frame("tuner_log").iloc[-1]
    assert json.loads(log["changes_json"]) == {
        "features_enabled.returns": [False, True]}
    assert log["ll_proposed"] < log["ll_current"]
    # the prompt carried the schema, the current backtest and live results
    sent = json.loads(llm.calls[0][1]["content"])
    assert {"CURRENT", "SCHEMA", "BACKTEST_OF_CURRENT", "LIVE_RECENT",
            "RECENT_PROPOSALS"} <= set(sent)
    # a second adoption in the same ISO week is not even attempted
    row = tune(ledger, raw, t, CUTOFF + dt.timedelta(days=2), news=False,
               llm=FakeLLM(AssertionError("must not be called")))
    assert row["status"] == "skipped"
    # the next daily fits a new live model with the adopted settings
    run_through(ledger, raw, table, prices, CUTOFF, dt.date(2021, 9, 15))
    m = ledger.frame("models")
    assert m[m["book"] == "live"]["note"].iloc[-1] == "settings changed"


def test_the_no_news_prompt_never_mentions_sentiment(book, cfg):
    msgs = prompt(cfg.raw, book, news=False, live={}, backtest={}, past=[])
    assert "sentiment" not in json.dumps(msgs)
    msgs = prompt(cfg.raw, book, news=True, live={}, backtest={}, past=[])
    assert "sentiment_weight" in msgs[1]["content"]


# ============================================================
# Daily report
# ============================================================

def test_report_with_llm(ledger, tmp_path):
    llm = FakeLLM("## Summary\nThe live book " + "word " * 300)
    path = write_report(ledger, CUTOFF, tmp_path / "reports", llm)
    text = path.read_text()
    assert path.name == f"{CUTOFF}.md"
    assert "## Numbers (from the ledger)" in text and "| spy |" in text
    assert "## Summary" not in text and "…" in text     # cleaned, capped
    facts_sent = json.loads(llm.calls[0][1]["content"][len("FACTS "):])
    eq = ledger.frame("equity", "date=? AND book='live'", (str(CUTOFF),))
    assert facts_sent["pnl"]["live"]["equity"] == round(eq["equity"][0], 2)


def test_report_without_llm_still_has_the_numbers(ledger, tmp_path):
    path = write_report(ledger, CUTOFF, tmp_path, FakeLLM(LLMError("down")))
    text = path.read_text()
    assert "LLM summary skipped: down" in text and "| live |" in text
    with pytest.raises(ValueError, match="no paper-trading day"):
        facts(ledger, dt.date(2021, 12, 25))


def test_clean_summary():
    assert clean_summary("```x```\n# Title\nHello there.") == \
        "Title\nHello there."
    with pytest.raises(LLMError):
        clean_summary("   ")


# ============================================================
# CLI
# ============================================================

def test_tune_and_report_cli(tmp_path, capsys):
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    shutil.copytree(ROOT / "nodes", tmp_path / "nodes")
    text = (tmp_path / "config.yaml").read_text()
    text = text.replace("history_start: \"2015-01-01\"",
                        "history_start: \"2019-01-01\"")
    text = text.replace("validation_years: 3", "validation_years: 1")
    text = text.replace("url: http://localhost:11434/v1",
                        "url: http://127.0.0.1:9/v1")       # nothing there
    text = text.replace("retries: 2", "retries: 0")
    (tmp_path / "config.yaml").write_text(text)
    base = ["--root", str(tmp_path), "--node", "B"]
    assert cli.main(base + ["daily", "--source", "synthetic", "--start",
                            "2023-03-01", "--end", "2023-03-03",
                            "--report"]) == 0
    out = capsys.readouterr().out
    assert "report:" in out
    rep = (tmp_path / "reports" / "B" / "2023-03-03.md").read_text()
    assert "LLM summary skipped" in rep
    # LLM down → the round is logged as llm_failed, nothing breaks
    assert cli.main(base + ["tune"]) == 0
    assert "LLM_FAILED" in capsys.readouterr().out
    prop = tmp_path / "p.json"
    prop.write_text('{"changes": {"model_params": {"C": 0.02}}}')
    assert cli.main(base + ["tune", "--proposal", str(prop)]) == 0
    out = capsys.readouterr().out
    assert ("ADOPTED" in out or "REJECTED" in out) and "log loss" in out
    log = pd.read_csv(tmp_path / "exports" / "B" / "tuner_log.csv")
    assert log["status"].tolist()[0] == "llm_failed"
    assert set(log["node"]) == {"B"}
