"""A file that stalls the Council is skipped, and stays skipped until it
changes (council_core.file_guard) - user 2026-10-22: "skip files that take
too long to process and cause the app to stall out"."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from council_core import file_guard as fg  # noqa: E402


@pytest.fixture
def guard(tmp_path, monkeypatch):
    """Its own app folder: the skip list and the reading notes in tmp."""
    monkeypatch.setenv("COUNCIL_APP_DIR", str(tmp_path / "app"))
    monkeypatch.setattr(fg, "_recovered", False)
    f = tmp_path / "book.xlsx"
    f.write_bytes(b"x" * 10)
    return f


def test_a_quick_read_is_its_answer(guard):
    assert fg.read(guard, lambda: 42, tab="Ledger") == 42
    assert fg.entries() == []
    assert json.loads(fg.reading_file().read_text()) == {}     # crossed out


def test_a_reader_s_own_error_passes_through_and_lists_nothing(guard):
    def boom():
        raise ValueError("bad zip")
    with pytest.raises(ValueError, match="bad zip"):
        fg.read(guard, boom)
    assert fg.entries() == []


def test_a_slow_read_is_left_behind_and_the_file_skipped_from_then_on(guard):
    release = threading.Event()
    started = time.monotonic()
    with pytest.raises(fg.Skipped, match="took longer than 0.3 s"):
        fg.read(guard, lambda: release.wait(30), tab="Connections", limit=0.3)
    assert time.monotonic() - started < 5               # the run went on
    [e] = fg.entries()
    assert e.path == str(guard.resolve()) and e.tab == "Connections"
    # the next read does not open it at all
    opened = []
    with pytest.raises(fg.Skipped, match="Retry skipped files"):
        fg.read(guard, lambda: opened.append(1))
    assert opened == []
    release.set()


def test_a_changed_file_is_read_again(guard):
    fg.add(guard, "took longer than 180 s to read")
    assert fg.skipped(guard) is not None
    guard.write_bytes(b"y" * 20)                        # saved again, fixed
    assert fg.skipped(guard) is None
    assert fg.read(guard, lambda: "read") == "read"


def test_retry_takes_files_off_the_list(guard, tmp_path):
    other = tmp_path / "other.pptx"
    other.write_bytes(b"z")
    fg.add(guard, "slow")
    fg.add(other, "slow")
    assert fg.clear([guard]) == 1 and [e.path for e in fg.entries()] == [str(other.resolve())]
    assert fg.clear() == 1 and fg.entries() == []


def test_a_cancel_stops_the_wait_but_blames_no_file(guard):
    release = threading.Event()
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    with pytest.raises(fg.Skipped, match="cancelled"):
        fg.read(guard, lambda: release.wait(30), limit=0, should_stop=stop.is_set)
    assert fg.entries() == []
    release.set()


def test_no_limit_when_set_to_zero(guard, monkeypatch):
    monkeypatch.setenv("COUNCIL_FILE_TIME_LIMIT_S", "0")
    assert fg.time_limit() == 0
    assert fg.read(guard, lambda: (time.sleep(0.2), "done")[1]) == "done"
    monkeypatch.setenv("COUNCIL_FILE_TIME_LIMIT_S", "nonsense")
    assert fg.time_limit() == fg.DEFAULT_TIME_LIMIT_S


CHILD = r"""
import os, sys, time
sys.path.insert(0, {root!r})
from council_core import file_guard as fg
fg.read({path!r}, lambda: os._exit(1) if {crash} else time.sleep(60), tab="Ledger")
"""


@pytest.mark.parametrize("crash", [True, False], ids=["crashed", "killed while frozen"])
def test_a_file_that_stopped_the_app_is_skipped_by_the_next_session(guard, crash):
    """The app crashed in the read, or froze and was killed: its note of what
    it was reading is left, and the next session puts that file on the list."""
    code = CHILD.format(root=str(ROOT), path=str(guard), crash=crash)
    env = dict(os.environ)
    child = subprocess.Popen([sys.executable, "-c", code], env=env)
    if not crash:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not (
                fg.reading_file().exists() and json.loads(fg.reading_file().read_text())):
            time.sleep(0.05)
        child.kill()
    child.wait(30)
    assert json.loads(fg.reading_file().read_text())        # the note was left
    with pytest.raises(fg.Skipped, match="stopped while reading it"):
        fg.read(guard, lambda: "never")
    assert fg.entries()[0].tab == "Ledger"


def test_a_normal_exit_leaves_no_blame(guard):
    code = (f"import sys; sys.path.insert(0, {str(ROOT)!r})\n"
            "from council_core import file_guard as fg\n"
            f"fg._note('t', {str(guard)!r}, 'Ledger')\n")    # exits with the note open
    subprocess.run([sys.executable, "-c", code], check=True, timeout=60)
    assert json.loads(fg.reading_file().read_text()) == {}
    assert fg.read(guard, lambda: "read") == "read"


def test_another_running_council_s_reads_are_not_blamed(guard):
    fg._save(fg.reading_file(), {"t": {"path": str(guard.resolve()), "tab": "Vault",
                                       "session": "other", "pid": os.getppid()}})
    assert fg.read(guard, lambda: "read") == "read"
    assert fg.entries() == []


def test_the_read_sees_the_asking_thread_s_state(guard):
    """The read runs on a thread of its own: a Ledger run's deck links
    (a threading.local) and the graph's image reader (a context variable)
    must go with it, and what the read sets must come back."""
    import contextvars
    local = threading.local()
    fg.carry(local)
    var = contextvars.ContextVar("reader", default=None)
    local.links = {"d.pptx": [2]}
    token = var.set("tesseract")

    def reader():
        assert threading.current_thread() is not threading.main_thread()
        local.excel_down = True
        return local.links, var.get()
    try:
        assert fg.read(guard, reader) == ({"d.pptx": [2]}, "tesseract")
    finally:
        var.reset(token)
    assert local.excel_down is True                    # came back
    fg._CARRIED.remove(local)


def test_the_ledger_s_thread_locals_are_carried():
    import excel_open
    import pptx_open
    assert any(x is pptx_open._LOCAL for x in fg._CARRIED)
    assert any(x is excel_open._SCOPE for x in fg._CARRIED)


def test_an_office_file_that_unpacks_too_big_is_not_opened(tmp_path, monkeypatch, guard):
    import zipfile
    bomb = tmp_path / "huge.xlsx"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("xl/worksheets/sheet1.xml", b"0" * 3_000_000)   # 3 MB, packs tiny
    assert bomb.stat().st_size < 50_000 and fg.unpacked_mb(bomb) == pytest.approx(3.0)
    monkeypatch.setenv("COUNCIL_FILE_UNPACKED_LIMIT_MB", "2")
    opened = []
    with pytest.raises(fg.Skipped, match="unpacks to 3 MB, more than 2 MB"):
        fg.read(bomb, lambda: opened.append(1), tab="Ledger")
    assert opened == [] and fg.skipped(bomb) is not None
    assert fg.unpacked_mb(guard) is None                      # not a zip: not asked


def test_a_read_that_eats_memory_is_left_behind(guard, monkeypatch):
    if fg.process_memory_mb() is None:
        pytest.skip("this system's memory cannot be read")
    monkeypatch.setenv("COUNCIL_FILE_MEMORY_LIMIT_MB", "50")
    release = threading.Event()
    held = []

    def hog():
        held.append(bytearray(200_000_000))                   # 200 MB, touched
        for i in range(0, len(held[0]), 4096):
            held[0][i] = 1
        release.wait(30)
    with pytest.raises(fg.Skipped, match="more than 50 MB of memory"):
        fg.read(guard, hog, tab="Connections")
    assert "memory" in fg.entries()[0].reason
    release.set()
    held.clear()
