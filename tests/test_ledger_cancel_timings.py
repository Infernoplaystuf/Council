"""
Tests for the Ledger's Cancel button + per-workbook timings
(the "What's slow?" line in the batch summary).

excel_open.kill_active_excels is a Windows-only no-op without pywin32;
these tests exercise the Python-level behavior that is OS-independent.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import excel_open                                                     # noqa: E402
from council_core.ledger_ingest import (ExtractSummary,               # noqa: E402
                                        summarize_slowest)


# ----------------------------------------- kill_active_excels

def test_kill_active_excels_is_zero_when_no_excel_is_registered(monkeypatch):
    monkeypatch.setattr(excel_open, "_ACTIVE_EXCEL_PIDS", set())
    assert excel_open.kill_active_excels() == 0


def test_outside_windows_kill_active_excels_is_always_zero(monkeypatch):
    monkeypatch.setattr(excel_open.os, "name", "posix")
    monkeypatch.setattr(excel_open, "_ACTIVE_EXCEL_PIDS", {12345})
    assert excel_open.kill_active_excels() == 0


def test_on_windows_each_registered_pid_is_taskkilled(monkeypatch):
    """When pywin32 would normally land a PID in the set, the Cancel
    path calls taskkill once per entry. The subprocess call is replaced
    so the test does not actually spawn taskkill."""
    monkeypatch.setattr(excel_open.os, "name", "nt")
    pids = {11111, 22222, 33333}
    monkeypatch.setattr(excel_open, "_ACTIVE_EXCEL_PIDS", set(pids))

    seen = []

    class _FakeCompleted:
        returncode = 0

    def fake_run(cmd, **_kw):
        seen.append(cmd)
        return _FakeCompleted()

    import subprocess as _sp
    monkeypatch.setattr(_sp, "run", fake_run)
    killed = excel_open.kill_active_excels()
    assert killed == 3
    # Every call was taskkill with /F /PID + a pid string, and every
    # one of our pids appears.
    pids_in_calls = {int(cmd[-1]) for cmd in seen}
    assert pids_in_calls == pids
    for cmd in seen:
        assert cmd[0] == "taskkill"
        assert "/F" in cmd


# ----------------------------------------- summarize_slowest

def test_summarize_slowest_lists_the_top_descending():
    text = summarize_slowest([("a.xlsx", 1.2), ("b.xlsx", 90.4),
                              ("c.xlsx", 0.3), ("d.xlsx", 44.5)])
    assert "b.xlsx 90.4s" in text
    assert "d.xlsx 44.5s" in text
    # ascending order would put a.xlsx before d.xlsx; descending puts d first.
    assert text.index("b.xlsx") < text.index("d.xlsx")
    assert text.startswith("Slowest 4 of 4 workbooks")


def test_summarize_slowest_caps_at_top():
    timings = [(f"w{i}.xlsx", float(i)) for i in range(20)]
    text = summarize_slowest(timings, top=3)
    assert text.startswith("Slowest 3 of 20 workbooks")
    assert "w19.xlsx 19.0s" in text
    # w17, w18, w19 are the three slowest; w16 should not appear.
    assert "w16.xlsx" not in text


def test_summarize_slowest_empty_returns_empty_string():
    assert summarize_slowest([]) == ""


# ----------------------------------------- ExtractSummary.describe includes slowest

def test_describe_appends_the_slowest_line_when_timings_exist():
    s = ExtractSummary(review="r.xlsx", files=2, values=10, pre_accepted=8,
                       to_review=2, unmatched=0, images=0, images_read=0,
                       problems=[], open_failures=[],
                       timings=[("a.xlsx", 2.1), ("b.xlsx", 12.4)])
    text = s.describe()
    assert "Slowest 2 of 2 workbooks" in text
    assert "b.xlsx 12.4s" in text


def test_describe_marks_a_cancelled_run():
    s = ExtractSummary(review="r.xlsx", files=1, values=0, pre_accepted=0,
                       to_review=0, unmatched=0, images=0, images_read=0,
                       problems=["Cancelled by user before 'x.xlsx'."],
                       open_failures=[], timings=[], cancelled=True)
    text = s.describe()
    assert text.startswith("CANCELLED — ")


# ----------------------------------------- extract honors cancel

def test_extract_sees_a_preset_cancel_event_and_exits_without_reading(tmp_path):
    """A cancel already set before extract() is called → the loop breaks
    on the first check, returns an Extraction with cancelled=True, and
    no workbook is opened."""
    from source_extract import extract

    class _Schema:
        id_field = "part"
        fields = []
        items = []

    cancel = threading.Event()
    cancel.set()
    run = extract(_Schema(), [tmp_path / "a.xlsx", tmp_path / "b.xlsx"],
                  cancel=cancel)
    assert run.cancelled is True
    assert run.timings == []
    assert any("Cancelled by user" in p for p in run.problems)
