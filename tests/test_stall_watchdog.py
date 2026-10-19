"""
Tests for council_core.stall_watchdog — "the app froze and I don't
know why" diagnostic.

The watchdog is a background daemon; tests use short stall_s so a
test case does not actually wait 15 s.
"""
from __future__ import annotations

import threading
import time

from council_core import stall_watchdog


def test_a_block_that_beats_regularly_does_not_bark():
    logs: list = []
    with stall_watchdog.watch("fast loop", logs.append,
                              stall_s=0.4, sample_s=0.05) as w:
        for _ in range(6):
            time.sleep(0.05)
            w.beat()
    assert all("stall-watchdog" not in m for m in logs), logs


def test_a_block_that_stops_beating_gets_a_stack_dump():
    logs: list = []
    with stall_watchdog.watch("slow loop", logs.append,
                              stall_s=0.2, sample_s=0.1) as w:
        w.beat()
        # Give the watchdog a comfortable window: stall_s + 2 samples.
        time.sleep(1.0)
    hits = [m for m in logs if "stall-watchdog" in m]
    assert hits, f"expected a stack dump when progress stops; got {logs!r}"
    # Every live thread in the process should appear in the dump.
    assert "--- thread id=" in hits[0]


def test_only_one_report_per_sustained_stall():
    """A stall that never resumes should not spam the log."""
    logs: list = []
    with stall_watchdog.watch("stuck", logs.append,
                              stall_s=0.1, sample_s=0.05) as w:
        w.beat()
        time.sleep(0.8)
    hits = [m for m in logs if "stall-watchdog" in m]
    assert 1 <= len(hits) <= 2, hits   # one report, maybe a race-case extra


def test_a_fresh_beat_lets_the_watchdog_report_the_next_stall():
    logs: list = []
    with stall_watchdog.watch("resume", logs.append,
                              stall_s=0.2, sample_s=0.1) as w:
        w.beat()
        time.sleep(1.0)        # stall long enough for one report
        w.beat()               # resume — the next stall should be caught
        time.sleep(1.0)
    hits = [m for m in logs if "stall-watchdog" in m]
    assert len(hits) >= 2, f"expected two reports; got {hits!r}"


def test_format_all_stacks_shows_every_live_thread():
    def _sit():
        time.sleep(0.3)
    t = threading.Thread(target=_sit, name="sitter", daemon=True)
    t.start()
    time.sleep(0.05)
    text = stall_watchdog.format_all_stacks()
    assert "'sitter'" in text
    t.join()


def test_system_info_runs_and_returns_a_short_block():
    info = stall_watchdog.system_info()
    assert "python=" in info
    assert "os.name=" in info
