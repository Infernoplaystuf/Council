"""
Shared pytest fixtures.

ONE Tk root per test SESSION — and that is a correctness requirement, not a
speed optimisation.

Creating a second Tk root after the first has been destroyed fails on the
project's own Python 3.11 floor (miniconda3\\envs\\council, Tcl/Tk 8.6.15):

    TclError: Can't find a usable init.tcl ...
    couldn't read file ".../Library/lib/tcl8.6/init.tcl": No error

Destroying a root leaves the interpreter unable to re-initialise Tcl. With a
root per test FILE, whichever Tk-using file pytest happened to run second lost
its whole fixture — and because the fixture treats a failed root as "no
display" and skips, the run still reported green. That is the worst possible
shape for a test failure: 13 tests silently not running, on the one interpreter
version this project has actually shipped a bug against.

A single session-scoped root is created once, never destroyed mid-run, and
shared. Widgets built on it are still torn down per test.

The skip branch is kept for genuinely headless machines, which is what it was
meant for.
"""
from __future__ import annotations

# FIRST, before anything can resolve the vault: every session writes to a
# throwaway app folder and vault, never the user's ~/.council. See
# tests/sandbox_vault.py. `sandbox_env` re-asserts it after every test.
from tests.sandbox_vault import sandbox_env  # noqa: E402,F401

import pytest

# Every test runs with the desktop's openers blocked (Explorer, browsers,
# os.startfile, QDesktopServices) and Qt offscreen — see tests/desktop_guard.py
# for the Explorer windows that made this necessary. Imported, not declared as
# a plugin, so inferno_local/tests can share the same fixture the same way.
from tests.desktop_guard import desktop_openers  # noqa: E402,F401


@pytest.fixture(scope="session")
def tk_root():
    """The one Tk root. Withdrawn, so nothing flashes on screen."""
    try:
        import tkinter as tk
    except Exception as exc:                       # pragma: no cover
        pytest.skip(f"tkinter unavailable: {exc!r}")
    try:
        root = tk.Tk()
        root.withdraw()
    except Exception as exc:                       # pragma: no cover
        pytest.skip(f"no display: {exc!r}")
    try:
        yield root
    finally:
        try:
            root.destroy()
        except Exception:
            pass
