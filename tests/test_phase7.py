"""Phase 7: the schedule (run-due), the on-demand LLM server, the
pre-flight check, export syncing and the deployment files."""
from __future__ import annotations

import datetime as dt
import json
import shutil
import socket
import sys
import textwrap
import time
from zoneinfo import ZoneInfo

import pytest

from quant_duel import cli
from quant_duel import config as cfgmod
from quant_duel.check import run_checks
from quant_duel.llm.client import LLMError
from quant_duel.llm.server import is_up, llm_server
from quant_duel.scheduler import plan, run_due

from .conftest import ROOT

NY = ZoneInfo("America/New_York")
SCHED = {"news_from": "09:00", "daily_at": "16:45", "tune_weekday": 5}
TUE, MON = dt.date(2026, 10, 6), dt.date(2026, 10, 5)
FRI, THU = dt.date(2026, 10, 9), dt.date(2026, 10, 8)
SAT, SUN = dt.date(2026, 10, 10), dt.date(2026, 10, 11)


def at(day, hh, mm=0):
    return dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=NY)


def P(now, news=True, state=None, last_run=None, report_done=False,
      tuned_for=None, window=None):
    return plan(now, news=news, schedule=SCHED, state=state or {},
                last_run=last_run, report_done=report_done,
                tuned_for=tuned_for, window=window)


# ============================================================
# The schedule
# ============================================================

def test_news_polls_hourly_in_market_hours_on_node_a_only():
    assert P(at(TUE, 10, 5), last_run=MON) == ["news-poll"]
    assert P(at(TUE, 10, 5), news=False, last_run=MON) == []
    polled = {"news-poll": at(TUE, 10, 5).isoformat()}
    assert P(at(TUE, 10, 30), state=polled, last_run=MON) == []
    assert P(at(TUE, 11, 1), state=polled, last_run=MON) == ["news-poll"]
    assert P(at(TUE, 8, 45), last_run=MON) == []           # before 09:00
    assert P(at(TUE, 16, 0), last_run=MON) == []           # cutoff
    assert P(at(SAT, 10), last_run=FRI, tuned_for=FRI) == []
    assert P(at(dt.date(2026, 11, 26), 10), last_run=MON) == []  # Thanksgiving


def test_a_last_poll_just_before_the_cutoff():
    polled = {"news-poll": at(TUE, 15, 10).isoformat()}
    assert P(at(TUE, 15, 46), state=polled, last_run=MON) == ["news-poll"]
    polled = {"news-poll": at(TUE, 15, 46).isoformat()}
    assert P(at(TUE, 15, 55), state=polled, last_run=MON) == []


def test_daily_then_report_once():
    assert P(at(TUE, 16, 40), news=False, last_run=MON) == []
    assert P(at(TUE, 16, 50), news=False, last_run=MON) == ["daily", "report"]
    assert P(at(TUE, 17, 5), news=False, last_run=TUE) == ["report"]
    assert P(at(TUE, 17, 5), news=False, last_run=TUE,
             report_done=True) == []
    # a missed day is caught up the next evening (daily processes both)
    assert P(at(TUE, 20, 0), news=False, last_run=dt.date(2026, 10, 2)) == \
        ["daily", "report"]


def test_no_daily_on_weekends_or_holidays():
    assert P(at(SAT, 17), news=False, last_run=FRI, tuned_for=FRI) == []
    assert P(at(dt.date(2026, 11, 26), 17), news=False,
             last_run=dt.date(2026, 11, 25)) == []     # Thanksgiving


def test_paper_trading_only_inside_the_experiment_window():
    window = (dt.date(2026, 10, 19), dt.date(2026, 11, 16))
    assert P(at(TUE, 16, 50), window=window) == []          # warm-up: no daily
    assert P(at(TUE, 10, 5), window=window) == ["news-poll"]  # but news
    assert P(at(dt.date(2026, 10, 19), 16, 50), news=False,
             window=window) == ["daily", "report"]
    assert P(at(dt.date(2026, 11, 17), 16, 50), news=False,
             last_run=dt.date(2026, 11, 16), window=window) == []


def test_weekly_tune_on_saturday_with_sunday_catch_up():
    assert P(at(SAT, 9), news=False, last_run=FRI) == ["tune"]
    assert P(at(SAT, 9), news=False, last_run=FRI, tuned_for=FRI) == []
    assert P(at(SUN, 9), news=False, last_run=FRI) == ["tune"]
    assert P(at(dt.date(2026, 10, 12), 9), news=False, last_run=FRI) == []
    # not before Friday has been processed
    assert P(at(SAT, 9), news=False, last_run=THU) == []
    # outside the window: no tuning
    assert P(at(SAT, 9), news=False, last_run=FRI,
             window=(dt.date(2026, 10, 12), dt.date(2026, 11, 9))) == []


def test_a_failed_tune_is_retried_at_most_every_three_hours():
    tried = {"tune": {"day": FRI.isoformat(), "at": at(SAT, 9).isoformat()}}
    assert P(at(SAT, 10), news=False, last_run=FRI, state=tried) == []
    assert P(at(SAT, 12, 5), news=False, last_run=FRI, state=tried) == \
        ["tune"]


# ============================================================
# run-due
# ============================================================

@pytest.fixture
def root(tmp_path):
    shutil.copy(ROOT / "config.yaml", tmp_path / "config.yaml")
    shutil.copytree(ROOT / "nodes", tmp_path / "nodes")
    return tmp_path


def test_run_due_runs_jobs_in_order_and_logs(root):
    cfg = cfgmod.load("A", root=root)
    calls = []

    def runner(cmd, timeout):
        calls.append(cmd)
        return 0, f"ok {cmd[-1]}\n"
    now = at(TUE, 16, 50).astimezone(dt.timezone.utc)
    done = run_due(cfg, now=now, runner=runner)
    assert [d["job"] for d in done] == ["daily", "report"]
    assert calls[0][1:3] == ["-m", "quant_duel.cli"]
    assert calls[0][-3:] == ["--node", "A", "daily"]
    log = (root / "logs" / "A.log").read_text()
    assert "run daily" in log and "daily | ok daily" in log
    # news-poll time is remembered
    done = run_due(cfg, now=at(TUE, 10, 5), runner=runner)
    assert [d["job"] for d in done] == ["news-poll"]
    state = json.loads((root / "data" / "A" / "schedule.json").read_text())
    assert state["news-poll"].startswith("2026-10-06T10:05")
    assert run_due(cfg, now=at(TUE, 10, 20), runner=runner) == []


def test_a_failed_daily_stops_the_report(root):
    cfg = cfgmod.load("B", root=root)
    done = run_due(cfg, now=at(TUE, 16, 50),
                   runner=lambda c, t: (1, "boom"))
    assert [(d["job"], d["rc"]) for d in done] == [("daily", 1)]
    assert "daily finished rc=1" in (root / "logs" / "B.log").read_text()


def test_run_due_passes_the_window_start_to_daily(root):
    (root / "experiment.yaml").write_text(
        "start: '2026-10-05'\nend: '2026-11-02'\n")
    cfg = cfgmod.load("B", root=root)
    done = run_due(cfg, now=at(TUE, 16, 50), dry_run=True)
    assert done[0]["args"] == ["daily", "--start", "2026-10-05"]


def test_run_due_skips_while_another_holds_the_lock(root):
    from quant_duel.locks import file_lock
    cfg = cfgmod.load("B", root=root)
    with file_lock(cfg.node_dir / "run-due.lock"):
        assert run_due(cfg, now=at(TUE, 16, 50), dry_run=True) == []


def test_run_due_cli_dry_run(root, capsys):
    assert cli.main(["--root", str(root), "--node", "B", "run-due",
                     "--dry-run"]) == 0
    assert capsys.readouterr().out.strip()


# ============================================================
# The on-demand LLM server
# ============================================================

FAKE_SERVER = textwrap.dedent('''
    import sys
    from http.server import BaseHTTPRequestHandler, HTTPServer
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_GET(self):
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers(); self.wfile.write(b"ok")
    HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
''')


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_the_server_is_started_for_a_job_and_stopped_after(tmp_path):
    script = tmp_path / "fake_server.py"
    script.write_text(FAKE_SERVER)
    port = _free_port()
    url = f"http://127.0.0.1:{port}/v1"
    cfg = {"url": url, "server": {"command": [sys.executable, str(script),
                                              str(port)],
                                  "start_timeout_s": 20}}
    assert not is_up(url)
    with llm_server(cfg, tmp_path / "srv.log") as info:
        assert info["started"] and is_up(url)
    deadline = time.time() + 5
    while is_up(url) and time.time() < deadline:
        time.sleep(0.1)
    assert not is_up(url)


def test_a_running_server_is_used_and_left_alone(tmp_path):
    import subprocess
    script = tmp_path / "fake_server.py"
    script.write_text(FAKE_SERVER)
    port = _free_port()
    url = f"http://127.0.0.1:{port}/v1"
    proc = subprocess.Popen([sys.executable, str(script), str(port)])
    try:
        deadline = time.time() + 10
        while not is_up(url) and time.time() < deadline:
            time.sleep(0.1)
        with llm_server({"url": url, "server": {"command": ["nope"]}}) as i:
            assert not i["started"]
        assert is_up(url)
    finally:
        proc.terminate()
        proc.wait()


def test_server_failures_are_llm_errors(tmp_path):
    url = f"http://127.0.0.1:{_free_port()}/v1"
    with pytest.raises(LLMError, match="exited"):
        with llm_server({"url": url, "server": {
                "command": [sys.executable, "-c", "raise SystemExit(3)"]}}):
            pass
    with pytest.raises(LLMError, match="not one string"):
        with llm_server({"url": url, "server": {"command": "llama-server"}}):
            pass
    with pytest.raises(LLMError, match="not found"):
        with llm_server({"url": url, "server": {
                "command": [str(tmp_path / "missing" / "llama-server")]}}):
            pass
    with llm_server({"url": url, "server": {}}) as info:   # nothing to start
        assert not info["started"]


def test_llm_session_yields_none_when_the_server_fails(root, capsys):
    text = (root / "config.yaml").read_text()
    text = text.replace("url: http://localhost:11434/v1",
                        f"url: http://127.0.0.1:{_free_port()}/v1")
    text = text.replace("    command: null", "    command: [" +
                        json.dumps(sys.executable) +
                        ', "-c", "raise SystemExit(2)"]')
    (root / "config.yaml").write_text(text)
    cfg = cfgmod.load("A", root=root)
    with cli.llm_session(cfg) as client:
        assert client is None
    assert "LLM unavailable" in capsys.readouterr().out
    assert not (root / "data" / "llm.lock").exists()


# ============================================================
# check, sync-exports, deployment files
# ============================================================

def test_check_lists_pass_warn_fail(root):
    cfg = cfgmod.load("A", root=root)
    lines = run_checks(cfg, network=False, ntp=lambda: True)
    by = {item: status for status, item, _ in lines}
    assert by["python"] == "PASS" and by["node files"] == "PASS"
    assert by["clock"] == "PASS"
    assert by["prices"] == "WARN"                 # nothing ingested yet
    assert by["experiment"] == "WARN"
    (root / "nodes" / "B.yaml").write_text("node_id: B\nnews_enabled: true\n")
    by = {i: s for s, i, _ in run_checks(cfg, network=False,
                                         ntp=lambda: False)}
    assert by["node files"] == "FAIL" and by["clock"] == "FAIL"


def test_check_cli_exit_code(root, capsys):
    rc = cli.main(["--root", str(root), "--node", "B", "check", "--offline"])
    out = capsys.readouterr().out
    assert "[PASS] python" in out and "FAIL" in out.splitlines()[-1]
    assert rc in (0, 1)


def test_sync_exports_uses_rsync_without_delete(root, capsys):
    cfg = cfgmod.load(None, root=root)
    assert cli.cmd_sync_exports(cfg, None) == 0
    assert "nothing to pull" in capsys.readouterr().out
    cfg.raw["compare"] = {"remote": {"A": "pi@quant-a:qd/exports/A",
                                     "B": "pi@quant-b:qd/exports/B/"}}
    calls = []
    assert cli.cmd_sync_exports(cfg, None,
                                runner=lambda c: calls.append(c) or 0) == 0
    assert calls[0][:2] == ["rsync", "-az"]
    assert all("--delete" not in c for c in calls)
    assert calls[1][-2] == "pi@quant-b:qd/exports/B/"
    assert calls[0][-1].endswith("exports/A/")


def test_deployment_files():
    svc = (ROOT / "deploy" / "systemd" / "quant-duel@.service").read_text()
    tmr = (ROOT / "deploy" / "systemd" / "quant-duel@.timer").read_text()
    assert "--node %i run-due" in svc and "Type=oneshot" in svc
    assert "OnCalendar=*:0/15" in tmr and "Persistent=true" in tmr
    doc = (ROOT / "deploy" / "README_PI.md").read_text()
    for must in ("SSD", "llama-server", "enable-linger", "Warm-up",
                 "start checklist", "experiment-init", "sync-exports"):
        assert must in doc, must
