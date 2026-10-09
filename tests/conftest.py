"""
Shared pytest fixtures.

NO TEST MAY OPEN A TK WINDOW. The Tk GUIs — the Tk console and its tabs and
dialogs, the gui_canvas designer, the Tk wizards, and apps generated for the
"tk" target — are deprecated in favour of the Qt front end (council_qt/). For
the whole run tkinter.Tk() and tkinter.Toplevel() raise

    TkWindowRefused: Tk GUIs are deprecated - tests must not open a Tk window
    (see docs/qt_migration/measurements.md section 4 and tests/README.md)

in this process and in every python child a test starts (tests/no_tk_guard.py).
Importing a Tk module, or checking the code the Tk target generates as text,
is still fine. tests/README.md says what to do instead of opening a window.

The session-scoped tk_root fixture that used to live here — one Tk root per
session, because this Tcl/Tk could not re-create a root after destroying one
— went with the tests that needed it (2026-10-09).
"""
from __future__ import annotations

# FIRST, before anything can resolve the vault: every session writes to a
# throwaway app folder and vault, never the user's ~/.council. See
# tests/sandbox_vault.py. `sandbox_env` re-asserts it after every test.
from tests.sandbox_vault import sandbox_env  # noqa: E402,F401

# Every test runs with the desktop's openers blocked (Explorer, browsers,
# os.startfile, QDesktopServices) and Qt offscreen — see tests/desktop_guard.py
# for the Explorer windows that made this necessary. Imported, not declared as
# a plugin, so inferno_local/tests can share the same fixture the same way.
from tests.desktop_guard import desktop_openers  # noqa: E402,F401

# No Tk window, in this process or any python child — installed on import,
# before a test module is collected. See tests/no_tk_guard.py.
from tests.no_tk_guard import no_tk_windows  # noqa: E402,F401
