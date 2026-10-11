"""``run-due``: called every ~15 minutes by a systemd timer (or cron, or
Windows Task Scheduler), it works out which jobs are due for this node and
runs them in order, each as its own process with a timeout, output going to
``logs/<node>.log`` (rotated). All times are New York time; weekends and
market holidays are skipped by the calendar, and a job missed while the
machine was off runs at the next call.

Jobs (and when they are due):

``news-poll``  news node only, trading days, hourly from ``news_from`` to
               the news cutoff (default 09:00–16:00), plus one final poll
               in the 15 minutes before the cutoff so late headlines get
               scored in time. Runs before and during the experiment
               (before = the warm-up).
``daily``      trading days at/after ``daily_at`` (16:45) once the day has
               not been processed — inside the experiment window if
               ``experiment.yaml`` exists (before its start: warm-up only).
``report``     after ``daily`` has processed the day, once.
``tune``       on ``tune_weekday`` (5 = Saturday; Sunday catches up) once
               per week, for the week's last completed trading day — inside
               the experiment window. A round the LLM missed is retried, at
               most every 3 hours.

``plan`` is pure (time and state in, job list out), so it is tested
directly; ``run_due`` does the I/O.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from .market_calendar import is_trading_day, previous_trading_day

NY = ZoneInfo("America/New_York")
JOB_TIMEOUT_S = {"news-poll": 40 * 60, "daily": 90 * 60, "report": 30 * 60,
                 "tune": 3 * 3600}


def _hm(text: str) -> tuple:
    h, m = (int(x) for x in text.split(":"))
    return h, m


def last_completed_day(now: dt.datetime, close: str = "16:30") -> dt.date:
    local = now.astimezone(NY)
    day = local.date()
    if is_trading_day(day) and (local.hour, local.minute) >= _hm(close):
        return day
    return previous_trading_day(day)


def plan(now: dt.datetime, *, news: bool, schedule: Dict[str, Any],
         state: Dict[str, Any], last_run: Optional[dt.date],
         report_done: bool, tuned_for: Optional[dt.date],
         window: Optional[tuple] = None, cutoff: str = "16:00"
         ) -> List[str]:
    """The jobs due at ``now`` (aware datetime), in run order.

    ``state``: {"news-poll": iso time of the last poll}; ``last_run``: the
    ledger's last processed day; ``report_done``: today's report exists;
    ``tuned_for``: cutoff day of the newest tuning round; ``window``:
    (start, end) of the experiment, or None.
    """
    local = now.astimezone(NY)
    today = local.date()
    hm = (local.hour, local.minute)
    jobs: List[str] = []
    in_window = (lambda d: True) if window is None else \
        (lambda d: window[0] <= d <= window[1])
    trading = is_trading_day(today)

    if news and trading:
        start = _hm(schedule.get("news_from", "09:00"))
        cut = _hm(cutoff)
        last = state.get("news-poll")
        last_t = dt.datetime.fromisoformat(last) if last else None
        since = (now - last_t).total_seconds() / 60 if last_t else 1e9
        final_from = (cut[0] * 60 + cut[1] - 15)
        in_final = final_from <= hm[0] * 60 + hm[1] < cut[0] * 60 + cut[1]
        polled_final = last_t is not None and last_t.astimezone(NY).date() \
            == today and (lambda t: final_from <= t.hour * 60 + t.minute)(
                last_t.astimezone(NY))
        if start <= hm < cut and (since >= 55 or (in_final and
                                                  not polled_final)):
            jobs.append("news-poll")

    daily_at = _hm(schedule.get("daily_at", "16:45"))
    if trading and hm >= daily_at and in_window(today) and \
            (last_run is None or last_run < today):
        jobs.append("daily")
    ran_today = (last_run == today) or "daily" in jobs
    if trading and hm >= daily_at and ran_today and not report_done:
        jobs.append("report")

    wd = int(schedule.get("tune_weekday", 5))
    days = {wd} | ({6} if wd == 5 else set())       # Sunday catches up
    if local.weekday() in days:
        week_day = last_completed_day(now)
        processed = last_run is not None and last_run >= week_day
        tried = state.get("tune") or {}
        recent = tried.get("day") == week_day.isoformat() and (
            now - dt.datetime.fromisoformat(tried["at"])
        ).total_seconds() < 3 * 3600
        if in_window(week_day) and processed and not recent and \
                (tuned_for is None or tuned_for < week_day):
            jobs.append("tune")
    return jobs


# ---------------------------------------------------------------- I/O ---

def _logger(root: Path, node: str) -> logging.Logger:
    log = logging.getLogger(f"quant_duel.run_due.{node}")
    if not log.handlers:
        path = Path(root) / "logs" / f"{node}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        h = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5,
                                encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s "
                                         "%(message)s"))
        log.addHandler(h)
        log.setLevel(logging.INFO)
    return log


def _state_path(cfg) -> Path:
    return cfg.node_dir / "schedule.json"


def load_state(cfg) -> Dict[str, Any]:
    try:
        return json.loads(_state_path(cfg).read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_state(cfg, state: Dict[str, Any]) -> None:
    path = _state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    os.replace(tmp, path)


def facts(cfg) -> Dict[str, Any]:
    """What the plan needs from the node's files."""
    from . import experiment as ex
    from .paper.ledger import Ledger
    out: Dict[str, Any] = {"last_run": None, "tuned_for": None,
                           "window": None}
    path = cfg.node_dir / "paper.sqlite"
    if path.exists():
        led = Ledger(path)
        try:
            out["last_run"] = led.last_run()
            t = led.frame("tuner_log")
            # a round the LLM missed (or that errored) is retried next call
            t = t[~t["status"].isin(["llm_failed", "error"])]
            if len(t):
                out["tuned_for"] = dt.date.fromisoformat(max(t["day"]))
        finally:
            led.close()
    exp_path = cfg.root / "experiment.yaml"
    if exp_path.exists():
        e = ex.load(exp_path)
        out["window"] = (e.start, e.end)
    return out


def job_args(job: str, window: Optional[tuple]) -> List[str]:
    if job == "daily" and window is not None:
        return ["daily", "--start", window[0].isoformat()]
    return [job]


def run_due(cfg, now: Optional[dt.datetime] = None,
            runner: Optional[Callable[[List[str], int], tuple]] = None,
            dry_run: bool = False) -> List[Dict[str, Any]]:
    """Plan and run the due jobs for ``cfg``'s node; returns what ran."""
    from .locks import LockBusy, file_lock
    now = now or dt.datetime.now(dt.timezone.utc)
    log = _logger(cfg.root, cfg.node_id)
    try:
        lock = file_lock(cfg.node_dir / "run-due.lock")
        lock.__enter__()
    except LockBusy:
        log.info("run-due already running; skipped")
        return []
    try:
        state = load_state(cfg)
        f = facts(cfg)
        day = now.astimezone(NY).date()
        report = cfg.root / "reports" / cfg.node_id / f"{day}.md"
        jobs = plan(now, news=cfg.news_enabled, schedule=cfg["schedule"],
                    state=state, last_run=f["last_run"],
                    report_done=report.exists(), tuned_for=f["tuned_for"],
                    window=f["window"], cutoff=cfg["news"]["cutoff"])
        if not jobs:
            log.info("nothing due")
        done = []
        for job in jobs:
            args = job_args(job, f["window"])
            if dry_run:
                done.append({"job": job, "args": args, "rc": None})
                continue
            log.info("run %s", " ".join(args))
            cmd = [sys.executable, "-m", "quant_duel.cli", "--root",
                   str(cfg.root), "--node", cfg.node_id] + args
            rc, output = (runner or _subprocess)(cmd, JOB_TIMEOUT_S[job])
            for line in output.splitlines()[-200:]:
                log.info("  %s | %s", job, line)
            (log.info if rc == 0 else log.error)("%s finished rc=%s", job, rc)
            done.append({"job": job, "args": args, "rc": rc})
            if job == "news-poll":
                state["news-poll"] = now.isoformat(timespec="seconds")
                save_state(cfg, state)
            if job == "tune":            # retried at most every 3 hours
                state["tune"] = {"day": last_completed_day(now).isoformat(),
                                 "at": now.isoformat(timespec="seconds")}
                save_state(cfg, state)
            if job == "daily" and rc != 0:
                break                      # no report on a failed day
        return done
    finally:
        lock.__exit__(None, None, None)


def _subprocess(cmd: List[str], timeout: int) -> tuple:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as exc:
        return 124, f"timed out after {timeout}s\n{exc.stdout or ''}"
