"""
tests.desktop_guard — nothing a test does may open a window on the desktop.

WHY THIS EXISTS
Tests ran with the user's real desktop underneath them, and one of them opened
an Explorer window on its own temp folder every run: a probe that calls every
Vault action with no arguments reached "Open vault folder", which is
os.startfile(tmp_path). Measured on 2026-09-29: 25 Explorer windows open on
%TEMP%\\pytest-of-apkun\\pytest-N\\test_an_unextracted_action_say0 — and each
one also held a handle inside its pytest-N folder, so NTFS refused the rename
pytest's cleanup starts with, pytest swallowed the error, and the temp folders
piled up (660 MB, 70 runs).

So for the length of EVERY test this blocks, records and warns about:
    os.startfile
    os.system / subprocess of explorer, start, a browser, notepad, mshta...,
        including inside a compound command line (`x & start notepad`) and a
        document or URL run by a shell, which opens its default app
    webbrowser.open / open_new / open_new_tab / get
    QDesktopServices.openUrl (when a test has imported Qt)

and it makes the offscreen Qt platform the default, so a Qt dialog a test
opens is never drawn on the real screen either.

WHAT IT DELIBERATELY LETS THROUGH
Console tools and interpreters: python (gui_runner previews, workers), git,
nvidia-smi, wmic, pnputil, `cmd /c ver` (platform.platform() runs it),
powershell queries, and a program that merely MENTIONS an opener as an
argument (`sc start Spooler`, `tasklist /FI "IMAGENAME eq explorer.exe"`). A
spy that blocked every non-python program broke test_basler_scan and hid the
GPU from half the suite.

A TEST THAT STUBS AN OPENER ITSELF STILL WINS
The guard has its own MonkeyPatch and is set up before the test's, so a
test's `monkeypatch.setattr` lands on top of it and is undone first — a test
can assert its own fake was called — and a test's `monkeypatch.undo()` undoes
only the test's own patches. A test can also ask for the `desktop_openers`
fixture by name and assert on what was blocked.

NOT COVERED (none of these is reached by a test today):
- names bound before the guard ran: `from os import startfile`, a default
  argument such as Dream3DTab's open_url=QDesktopServices.openUrl (offscreen
  makes that one a no-op), anything done at import or collection time;
- module-, class- and session-scoped fixtures, and unittest setUpClass —
  the guard is per test;
- ctypes ShellExecute, QProcess.start / startDetached;
- python CHILD processes: the guard is in-process only;
- Tk windows: those are refused outright, in this process and in python
  children, by tests/no_tk_guard.py (the Tk GUIs are deprecated).
"""
from __future__ import annotations

import base64
import os
import re
import shlex
import subprocess
import sys
import warnings
import webbrowser

import pytest

# Before the first QApplication. conftest imports this before any test module,
# and python children (gui_runner previews) inherit it. Seven Qt test files do
# not set it themselves; run alone, they drew real dialogs on the screen.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class DesktopOpenBlocked(UserWarning):
    """A test reached a call that would open a window on the real desktop."""


#: Programs whose job is to show something to the user.
OPENER_EXES = frozenset({
    "explorer", "start", "xdg-open", "open", "gio", "gnome-open", "kde-open",
    "notepad", "notepad++", "wordpad", "write", "mspaint", "rundll32",
    "mshta", "wscript", "cscript", "control", "mmc", "calc", "winword",
    "excel", "powerpnt", "msedge", "chrome", "firefox", "iexplore"})
#: PowerShell commands that open a window.
PS_OPEN_VERBS = frozenset({"start-process", "saps", "start", "invoke-item",
                           "ii"})
#: Extensions that name a program or a script — whatever the file is called,
#: "start.bat" is a script, not cmd's start.
_RUNNABLE_EXTS = frozenset({".exe", ".com", ".bat", ".cmd", ".ps1", ".py",
                            ".pyw", ".sh"})
_PS_OPTS_WITH_VALUE = frozenset({
    "-executionpolicy", "-ep", "-windowstyle", "-w", "-configurationname",
    "-inputformat", "-outputformat", "-workingdirectory", "-wd", "-psconsolefile",
    "-version", "-settingsfile"})


def _argv(args):
    if isinstance(args, (str, bytes, os.PathLike)):
        text = os.fsdecode(args)
        try:
            tokens = shlex.split(text, posix=False)
        except ValueError:
            tokens = text.split()
    else:
        tokens = [os.fsdecode(a) if isinstance(a, (bytes, os.PathLike))
                  else str(a) for a in args]
    return [t.strip('"').strip("'") for t in tokens]


def _segments(text: str):
    """A shell command line split into its commands — at & && | || ; ( )
    outside quotes. `echo x & start notepad` is two commands, and the second
    one opens a window."""
    out, cur, quote = [], [], ""
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            cur.append(ch)
        elif ch in "&|;()":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [s.strip() for s in out if s.strip()]


def _opens(word: str, shell: bool) -> bool:
    """Whether running `word` as a command shows something to the user.

    ``shell``: a shell runs it — cmd's `start` builtin (even with a switch
    glued on, `start/b`), a URL, and on Windows a document named as a command
    all open their default app there."""
    w = word.strip().strip('"').strip("'")
    low = w.lower()
    if not low:
        return False
    if shell and re.match(r"^start(/|$)", low):
        return True
    if re.match(r"^[a-z][a-z0-9+.-]*://", low):
        return shell
    base = re.split(r"[\\/]", low.rstrip("\\/"))[-1]
    root, ext = os.path.splitext(base)
    if ext in _RUNNABLE_EXTS:
        return ext in (".exe", ".com") and root in OPENER_EXES
    if not ext or ext[1:].isdigit():            # "git", "python3.11"
        return base in OPENER_EXES
    # Any other file named as the command: a shell on Windows opens it by
    # association (report.html -> the browser). POSIX runs only executables.
    return shell and os.name == "nt"


def _shell_line_opens(text: str) -> bool:
    for segment in _segments(text):
        tokens = _argv(segment)
        if tokens and _command_opens(tokens[0], tokens[1:], shell=True):
            return True
    return False


def _cmd_command(rest):
    """What `cmd` runs: everything after /c or /k (also glued: /cstart)."""
    for i, tok in enumerate(rest):
        low = tok.lower()
        if low in ("/c", "/k", "/r"):
            return " ".join(rest[i + 1:])
        if low[:2] in ("/c", "/k") and len(low) > 2:
            return " ".join([tok[2:]] + list(rest[i + 1:]))
    return ""


def _ps_command(rest):
    """What powershell runs: after -Command (or the first non-option word),
    or a decoded -EncodedCommand. A -File script is judged by nothing we can
    see, so it is let through."""
    i = 0
    while i < len(rest):
        low = rest[i].lower()
        if low in ("-command", "-c"):
            return " ".join(rest[i + 1:])
        if low in ("-encodedcommand", "-enc", "-e", "-ec"):
            try:
                return base64.b64decode(rest[i + 1]).decode("utf-16-le")
            except Exception:                             # noqa: BLE001
                return ""
        if low in ("-file", "-f"):
            return ""
        if low in _PS_OPTS_WITH_VALUE:
            i += 2
            continue
        if low.startswith("-"):
            i += 1
            continue
        return " ".join(rest[i:])
    return ""


def _command_opens(exe: str, rest, shell: bool) -> bool:
    if _opens(exe, shell):
        return True
    base = re.split(r"[\\/]", exe.lower().rstrip("\\/"))[-1]
    stem = os.path.splitext(base)[0] if base.endswith((".exe", ".com")) \
        else base
    if stem == "cmd":
        return _shell_line_opens(_cmd_command(list(rest)))
    if stem in ("powershell", "pwsh"):
        for segment in _segments(_ps_command(list(rest))):
            tokens = _argv(segment)
            if not tokens:
                continue
            verb = tokens[0].lower()
            if verb in PS_OPEN_VERBS and "-nonewwindow" not in \
                    segment.lower():
                return True
            if _opens(tokens[0], shell=True):
                return True
    return False


def is_desktop_opener(args, executable=None, shell=False) -> bool:
    """True for anything that would open a window: explorer, start, a
    browser, a document run by a shell. False for python, git, nvidia-smi,
    wmic, pnputil, `cmd /c ver`, powershell queries, and for a program that
    only mentions an opener as an argument."""
    if isinstance(args, (str, bytes, os.PathLike)) and not executable:
        # A whole command line (os.system, shell=True, or a Windows command
        # string): every command in it counts.
        text = os.fsdecode(args)
        if shell or re.search(r"[&|;]", text):
            return _shell_line_opens(text)
    tokens = _argv(args)
    exe = os.fsdecode(executable) if executable else (tokens[0] if tokens
                                                      else "")
    return _command_opens(exe, tokens[1:], shell=bool(shell))


@pytest.fixture(autouse=True)
def desktop_openers():
    """Block every desktop opener for this test; yield what was blocked.

    Its OWN MonkeyPatch, not the test's: a test that calls
    monkeypatch.undo() must not take the guard down with its own patches.
    """
    calls = []
    mp = pytest.MonkeyPatch()

    def blocked(api, target):
        calls.append((api, target))
        warnings.warn(DesktopOpenBlocked(
            f"{api}({target!r}) was blocked: it would open a window on the "
            f"desktop. Stub it in the test."), stacklevel=3)

    def startfile(path, *args, **kwargs):
        blocked("os.startfile", os.fsdecode(path))

    real_system = os.system

    def system(command):
        if is_desktop_opener(command, shell=True):
            blocked("os.system", command)
            return 0
        return real_system(command)

    # __init__ on the CLASS, not the module attribute: `from subprocess import
    # Popen` and subprocess.run / call / check_output all end up here, and
    # isinstance(p, subprocess.Popen) keeps working.
    real_init = subprocess.Popen.__init__
    quiet = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    kept = ("stdin", "stdout", "stderr", "text", "universal_newlines",
            "encoding", "errors")

    def popen_init(self, args, *a, **kw):
        if not isinstance(args, (str, bytes, os.PathLike)):
            args = list(args)       # a generator would be spent by the check
        if is_desktop_opener(args, kw.get("executable"),
                             shell=bool(kw.get("shell"))):
            blocked("subprocess.Popen", args)
            # A clean, instant no-op child with the caller's pipes and text
            # mode — and never the caller's CREATE_NEW_CONSOLE, which would
            # put the replacement itself on screen.
            real_init(self, [sys.executable, "-c", "pass"],
                      creationflags=quiet,
                      **{k: kw[k] for k in kept if k in kw})
            return
        real_init(self, args, *a, **kw)

    def browser_open(url, *args, **kwargs):
        blocked("webbrowser.open", url)
        return True

    class _NoBrowser:
        def open(self, url, *args, **kwargs):
            return browser_open(url)
        open_new = open_new_tab = open

    try:
        mp.setattr(os, "startfile", startfile, raising=False)
        mp.setattr(os, "system", system)
        mp.setattr(subprocess.Popen, "__init__", popen_init)
        for name in ("open", "open_new", "open_new_tab"):
            mp.setattr(webbrowser, name, browser_open)
        mp.setattr(webbrowser, "get", lambda using=None: _NoBrowser())
        # Qt only if a test module already imported it: importing Qt here
        # would be a side effect of its own.
        qtgui = sys.modules.get("PySide6.QtGui")
        if qtgui is not None:
            def open_url(url, *args, **kwargs):
                blocked("QDesktopServices.openUrl",
                        url.toString() if hasattr(url, "toString") else url)
                return True
            mp.setattr(qtgui.QDesktopServices, "openUrl",
                       staticmethod(open_url))
        yield calls
    finally:
        mp.undo()
