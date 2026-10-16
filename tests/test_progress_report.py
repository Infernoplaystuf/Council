"""
progress_report — progress lines in the terminal for the Ledger and the
knowledge graph: percentages, and a "still working" line when nothing moves.

Run:  python -m pytest tests/test_progress_report.py -q
"""
from __future__ import annotations

import io
import time

from progress_report import NullProgress, Progress


def _lines(buf):
    return [ln for ln in buf.getvalue().splitlines() if ln]


def test_percentages_per_step_and_a_done_line():
    out = io.StringIO()
    with Progress("ledger extract", stream=out, heartbeat=0, every=0) as p:
        p.stage("reading workbooks", total=4, unit="workbooks")
        for name in "abcd":
            p.update(f"opening {name}.xlsx")
            p.advance(what=f"{name}.xlsx read")
    lines = _lines(out)
    assert lines[0].startswith("[ledger extract] reading workbooks: 0/4 workbooks (0%)")
    assert any("2/4 workbooks (50%) · b.xlsx read" in ln for ln in lines)
    assert any("4/4 workbooks (100%) · d.xlsx read" in ln for ln in lines)
    assert lines[-1].startswith("[ledger extract] done in 0:00")


def test_a_long_stage_is_throttled_but_every_tenth_is_shown():
    out = io.StringIO()
    p = Progress("kg rebuild", stream=out, heartbeat=0, every=3600)
    p.stage("documents", total=1000, unit="files")
    for _ in range(1000):
        p.advance()
    p.finish()
    pcts = [ln for ln in _lines(out) if "%" in ln]
    assert len(pcts) == 11                                  # 0%, 10% ... 100%
    assert pcts[-1].split(" · ")[0].endswith("1000/1000 files (100%)")


def test_a_stall_prints_still_working_and_progress_resets_it():
    out = io.StringIO()
    p = Progress("ledger extract", stream=out, heartbeat=0.15, every=0)
    p.stage("reading workbooks", total=2, unit="workbooks")
    p.update("opening locked.xlsx through Excel")
    time.sleep(0.5)
    stalls = [ln for ln in _lines(out) if "still working" in ln]
    assert stalls and "opening locked.xlsx through Excel" in stalls[0]
    assert "nothing new for 0.0 s" not in stalls[0]
    p.advance(what="locked.xlsx: not read")
    n = len([ln for ln in _lines(out) if "still working" in ln])
    p.finish()
    time.sleep(0.3)                                          # stopped: no more beats
    assert len([ln for ln in _lines(out) if "still working" in ln]) <= n + 1


def test_the_listener_gets_each_line_and_the_fraction():
    got = []
    p = Progress("ledger apply", stream=io.StringIO(), heartbeat=0, every=0,
                 listener=lambda line, frac: got.append((line, frac)))
    p.stage("filling templates", total=2, unit="parts")
    p.advance()
    p.finish("2 templates")
    assert got[0][1] == 0.0 and got[1][1] == 0.5
    assert got[1][0].startswith("filling templates: 1/2 parts (50%)")   # no [task] prefix
    assert got[-1] == ("done in 0:00 · 2 templates", 1.0)


def test_an_exception_is_reported_as_stopped(monkeypatch):
    out = io.StringIO()
    try:
        with Progress("ledger extract", stream=out, heartbeat=0) as p:
            p.stage("reading workbooks", total=3)
            raise ValueError("bad workbook")
    except ValueError:
        pass
    assert _lines(out)[-1].startswith("[ledger extract] stopped after 0:00: ValueError: bad workbook")


def test_quiet_switch_and_null_progress(monkeypatch):
    monkeypatch.setenv("COUNCIL_PROGRESS", "0")
    got = []
    p = Progress("x", listener=lambda line, f: got.append(line), heartbeat=0)
    assert p.stream is None
    p.stage("s", total=1)
    assert got                                             # the tab still hears it
    n = NullProgress()
    n.stage("s", total=2)
    n.advance()
    n.finish()
    assert n._beater is None


def test_no_still_working_line_after_done():
    """The heartbeat can be past its wait when the run ends; holding the lock
    while finish() writes 'done', it must then stay quiet."""
    out = io.StringIO()
    p = Progress("kg rebuild", stream=out, heartbeat=0.05, every=0)
    p.stage("documents", total=1)
    p.last_activity -= 10                       # long quiet: a beat is due
    with p._lock:                               # the heartbeat waits here...
        time.sleep(0.12)
        p._stop.set()                           # ...while the run ends
        p._write("[kg rebuild] done", 1.0)
    time.sleep(0.15)
    lines = _lines(out)
    assert lines[-1] == "[kg rebuild] done"
