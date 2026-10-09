"""
tests.no_tk_guard — no test may open a Tk window.

The Tk GUIs are deprecated: the Tk console (council_gui_engine.CouncilConsole)
with its tabs and dialogs, the gui_canvas designer and the Tk wizards, and the
apps gui_emit generates for target "tk". The front end is the Qt one in
council_qt/. On 2026-10-09 every test that opened a Tk window was removed,
ported to Qt or to toolkit-free code, or pointed at the Qt target instead
(tests/README.md says which way to go), and the session-wide tk_root fixture
went with them.

This keeps it that way. For the whole run, tkinter.Tk() — and so Tcl() and
a default root — and tkinter.Toplevel() raise

    TkWindowRefused: Tk GUIs are deprecated - tests must not open a Tk window
    (see docs/qt_migration/measurements.md section 4 and tests/README.md)

IN THIS PROCESS (patched now, or as tkinter is imported) and IN EVERY PYTHON
CHILD a test starts (tests/_no_tk/ is put first on PYTHONPATH, and its
sitecustomize installs the same refusal at the child's startup). A generated
app, a gui_runner preview, a test's driver script — all children. A
probe that measured the run before this found 15 of the 72 tests that made
Tk windows doing it only in a child, where a guard in this process alone
would never have seen them.

COUNCIL_NO_TK_LOG=<file> records every refusal there too — pid, the test
that was running (a child inherits PYTEST_CURRENT_TEST), and what was called
— so a whole run can show the guard fired nowhere but in its own tests.

NOT COVERED: a child started with -I or -E, or with an environment that
drops PYTHONPATH (none of those makes a Tk window today); a different
interpreter that cannot import the hook (it is plain stdlib Python, so any
3.x can).

Importing tkinter, or a module built on it (gui_canvas, council_gui_engine),
is still allowed — many tests read those modules' functions or source and
never make a window.

Imported by tests/conftest.py and inferno_local/tests/conftest.py, and by
tests/smoke_test.py when it runs as a script (which loads no conftest).
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

#: The folder put first on every child's PYTHONPATH.
HOOK_DIR = Path(__file__).resolve().parent / "_no_tk"


def _core():
    """tests/_no_tk/council_no_tk.py, loaded by path: the folder itself is
    NOT put on this process's sys.path — its sitecustomize is for children."""
    name = "council_no_tk"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(
            name, HOOK_DIR / "council_no_tk.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


_CORE = _core()
MESSAGE: str = _CORE.MESSAGE
TkWindowRefused = _CORE.TkWindowRefused


def install() -> None:
    """Refuse Tk windows here and in every python child started from now."""
    _CORE.install()
    hook = str(HOOK_DIR)
    parts = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep)
             if p and p != hook]
    os.environ["PYTHONPATH"] = os.pathsep.join([hook] + parts)


def installed() -> bool:
    """Whether this process refuses Tk windows, and its children will too."""
    first = os.environ.get("PYTHONPATH", "").split(os.pathsep)[0]
    return _CORE.installed() and first == str(HOOK_DIR)


# At IMPORT, i.e. when conftest is loaded: before any test module is
# collected, so a Tk window made at import or collection time is refused too.
install()


try:
    import pytest
except ImportError:                      # script mode (smoke_test's main())
    pytest = None

if pytest is not None:
    @pytest.fixture(scope="session", autouse=True)
    def no_tk_windows():
        """The guard, for the whole session (it is installed on import; this
        asserts it is still in force when the first test starts)."""
        install()
        assert installed()
        yield
