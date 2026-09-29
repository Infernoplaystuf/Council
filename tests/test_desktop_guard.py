"""
tests/desktop_guard.py — the fixture that keeps tests off the user's desktop.

Every test here CHECKS THE GUARD IS IN PLACE BEFORE making a call that would
open a window without it. A broken guard must fail these tests, not open
Explorer on the user's screen while they run.
"""
from __future__ import annotations

import os
import subprocess
import sys
import webbrowser

import pytest

from tests import desktop_guard as dg

# Imported at COLLECTION, before any test's guard is installed, so the guard
# finds Qt loaded and patches QDesktopServices for every test in this file.
try:
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
except Exception:                                         # noqa: BLE001
    QUrl = QDesktopServices = None


def _guarded(fn) -> bool:
    return getattr(fn, "__module__", "") == dg.__name__


# ============================================================
# Which programs count as openers
# ============================================================

@pytest.mark.parametrize("args", [
    ["explorer", r"C:\temp"],
    [r"C:\Windows\explorer.exe", r"C:\temp"],
    "explorer C:\\temp",
    ["cmd", "/c", "start", "", r"C:\temp\report.html"],
    ["powershell", "-NoProfile", "Start-Process", "notepad"],
    ["xdg-open", "/tmp"],
    ["notepad.exe", "a.txt"],
    ["msedge", "https://example.com"],
])
def test_programs_that_show_something_are_openers(args):
    assert dg.is_desktop_opener(args)


@pytest.mark.parametrize("args", [
    [sys.executable, "-u", "main.py"],        # a gui_runner preview
    ["git", "status"],
    ["nvidia-smi", "--query-gpu=memory.free", "--format=csv"],
    ["wmic", "cpu", "get", "name"],
    [r"C:\Windows\System32\pnputil.exe", "/enum-devices"],
    ["cmd", "/c", "ver"],                     # platform.platform() runs this
    ["powershell", "-NoProfile", "Get-CimInstance", "Win32_VideoController"],
    "taskkill /F /T /PID 1234",
])
def test_console_tools_and_interpreters_are_not(args):
    """A spy that blocked every non-python program broke test_basler_scan
    and hid the GPU from half the suite. These must run."""
    assert not dg.is_desktop_opener(args)


# Found by the review of the first version: openers it missed ...
@pytest.mark.parametrize("line", [
    'echo x & start "" notepad',              # not the first command
    r'cd /d C:\x && explorer .',
    r'"C:\x\report.html"',                    # a document opens its app
    r'"C:\Program Files (x86)\Microsoft\Edge\msedge.exe" https://x',
    "mshta about:blank",
    "wscript x.vbs",
    "control",
])
def test_every_command_in_a_shell_line_is_checked(line):
    if line.endswith('.html"') and os.name != "nt":
        pytest.skip("opening by association is a Windows shell behaviour")
    assert dg.is_desktop_opener(line, shell=True)


@pytest.mark.parametrize("args", [
    ["cmd", "/c", r"C:\x\report.html"],
    ["cmd", "/cstart", "notepad"],            # the switch glued on
    ["cmd", "/c", "start/b", "notepad"],
    ["powershell", "-Command", r"Invoke-Item C:\x"],
    # "Start-Process notepad", as -EncodedCommand (UTF-16LE, base64)
    ["powershell", "-EncodedCommand",
     "UwB0AGEAcgB0AC0AUAByAG8AYwBlAHMAcwAgAG4AbwB0AGUAcABhAGQA"],
])
def test_openers_inside_cmd_and_powershell(args):
    if args[-1].endswith(".html") and os.name != "nt":
        pytest.skip("opening by association is a Windows shell behaviour")
    assert dg.is_desktop_opener(args)


# ... and harmless commands it blocked, because the word appeared ANYWHERE.
@pytest.mark.parametrize("args", [
    ["cmd", "/c", "echo", "start"],
    ["cmd", "/c", "python", "run.py", "start"],
    ["cmd", "/c", "sc", "start", "Spooler"],
    ["cmd", "/c", "tasklist", "/FI", "IMAGENAME eq explorer.exe"],
    ["powershell", "-Command", "Get-Process explorer"],
    ["powershell", "-Command", "Start-Process python -NoNewWindow -Wait"],
    ["powershell", "-Command", "(Get-Item ii).Length"],
    ["powershell", "-Command", "& ./start.ps1"],
    ["powershell", "-File", r"C:\scripts\start.ps1"],
    [r"C:\proj\scripts\start.bat"],
    ["code", "--version"],
    ["python", "-m", "foo", "start"],
    ["git", "log", "--", "open"],
    ["python3.11", "-c", "pass"],
])
def test_a_program_that_only_mentions_an_opener_is_not_one(args):
    assert not dg.is_desktop_opener(args)


def test_an_executable_argument_decides_it():
    assert dg.is_desktop_opener(["whatever"], executable="explorer.exe")
    assert not dg.is_desktop_opener(["explorer"], executable=sys.executable)


# ============================================================
# Blocked, recorded, warned
# ============================================================

def test_startfile_is_blocked_and_recorded(desktop_openers, tmp_path):
    assert _guarded(os.startfile), "the guard is not installed"
    with pytest.warns(dg.DesktopOpenBlocked, match="os.startfile"):
        os.startfile(str(tmp_path))
    assert desktop_openers == [("os.startfile", str(tmp_path))]


def test_an_explorer_child_becomes_a_harmless_one(desktop_openers, tmp_path):
    """Blocked by swapping the child for an instant no-op python, so a caller
    that waits on the Popen still gets a real one that exits 0."""
    assert _guarded(subprocess.Popen.__init__), "the guard is not installed"
    with pytest.warns(dg.DesktopOpenBlocked, match="subprocess.Popen"):
        proc = subprocess.Popen(["explorer", str(tmp_path)])
    assert proc.wait(timeout=30) == 0
    assert desktop_openers[0][0] == "subprocess.Popen"


def test_a_console_child_still_runs(desktop_openers):
    out = subprocess.run([sys.executable, "-c", "print('ran')"],
                         capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "ran"
    assert desktop_openers == []


def test_os_system_blocks_only_openers(desktop_openers):
    assert _guarded(os.system), "the guard is not installed"
    with pytest.warns(dg.DesktopOpenBlocked):
        assert os.system("start notepad") == 0
    # A console command goes through to the real os.system. (No quoted exe
    # path: cmd.exe strips the outer quotes of a command that starts with one.
    # "exit 0" means the same to cmd and to sh.)
    assert os.system("exit 0") == 0
    assert [api for api, _t in desktop_openers] == ["os.system"]


def test_the_browser_is_blocked(desktop_openers):
    # EVERY call below is checked first: a pre-check of open() alone let a
    # lost get() patch open a real tab before the assertion could fail.
    assert _guarded(webbrowser.open), "the guard is not installed"
    assert _guarded(webbrowser.get), "the guard is not installed"
    assert _guarded(type(webbrowser.get()).open_new_tab), \
        "the guard is not installed"
    with pytest.warns(dg.DesktopOpenBlocked):
        assert webbrowser.open("https://example.com")
        assert webbrowser.get().open_new_tab("https://example.com/2")
    assert [t for _api, t in desktop_openers] == ["https://example.com",
                                                  "https://example.com/2"]


def test_qdesktopservices_is_blocked_once_qt_is_imported(desktop_openers):
    if QDesktopServices is None:
        pytest.skip("PySide6 is not installed")
    assert _guarded(QDesktopServices.openUrl), "the guard is not installed"
    with pytest.warns(dg.DesktopOpenBlocked):
        assert QDesktopServices.openUrl(QUrl("https://example.com"))


def test_qt_defaults_to_offscreen():
    """setdefault: an explicit QT_QPA_PLATFORM from the caller still wins."""
    assert os.environ.get("QT_QPA_PLATFORM")


def test_a_tests_own_stub_wins_and_can_assert(monkeypatch, desktop_openers):
    seen = []
    monkeypatch.setattr(os, "startfile", lambda p, *a, **k: seen.append(p),
                        raising=False)
    os.startfile("C:/somewhere")
    assert seen == ["C:/somewhere"]
    assert desktop_openers == []              # the test's stub ran, not ours


def test_a_tests_own_undo_does_not_take_the_guard_down(monkeypatch):
    """The guard used to share the test's monkeypatch, so a test's
    monkeypatch.undo() removed it for the rest of that test."""
    monkeypatch.setattr(os, "sep", os.sep)
    monkeypatch.undo()
    assert _guarded(os.startfile)
    assert _guarded(subprocess.Popen.__init__)


def test_generator_arguments_still_reach_the_real_popen(desktop_openers):
    """The check iterates the arguments; a generator used to reach the real
    Popen already spent, as an empty command line."""
    parts = (a for a in [sys.executable, "-c", "print('gen')"])
    out = subprocess.run(parts, capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "gen"
    assert desktop_openers == []


def test_a_blocked_call_never_gets_a_console_of_its_own(desktop_openers,
                                                        tmp_path):
    """The no-op replacement used to inherit the caller's creationflags, so
    CREATE_NEW_CONSOLE put the replacement itself on screen."""
    import inspect
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    if not flags:
        pytest.skip("creationflags are Windows-only")
    assert _guarded(subprocess.Popen.__init__), "the guard is not installed"
    # What the process launch itself receives, below both Popen.__init__s.
    real_exec = subprocess.Popen._execute_child
    names = list(inspect.signature(real_exec).parameters)[1:]
    launched = []

    def recording(self, *a, **k):
        bound = dict(zip(names, a), **k)
        launched.append((bound.get("args"), bound.get("creationflags")))
        return real_exec(self, *a, **k)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(subprocess.Popen, "_execute_child", recording)
        with pytest.warns(dg.DesktopOpenBlocked):
            proc = subprocess.Popen(["explorer", str(tmp_path)],
                                    creationflags=flags)
    assert proc.wait(timeout=30) == 0
    (args, got), = launched
    assert args == [sys.executable, "-c", "pass"]
    assert not got & flags, "the replacement was given a console of its own"
