"""
Tests that were retired on purpose, and must not come back in a merge.

The Tk shell (council_gui_engine.py, council_modules.py and the rest) is a
retired relic: the Qt app is the only one worked on. A test that compares the
Qt app with the Tk shell, or needs tkinter to run, checks nothing anyone
maintains. An older branch can still carry one, and a merge would quietly
bring it back. This file fails when that happens.

If one reappears after a merge: delete it again, unless deleting it breaks
something — then keep it, and take it off this list with a note in CLAUDE.md
saying why. CLAUDE.md ("Retired tests") lists the same names.
"""
from __future__ import annotations

from pathlib import Path

TESTS = Path(__file__).resolve().parent

#: name → why it was retired.
RETIRED = {
    "test_the_standalone_host_matches_the_tk_contract":
        "compared council_qt.host.StandaloneHost with the Tk "
        "council_modules.StandaloneHost; needs tkinter. Its Qt-only checks "
        "live on as test_the_standalone_host_surface.",
}


def test_no_retired_test_has_come_back():
    found = []
    for path in sorted(TESTS.glob("**/*.py")):
        if path.name == Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for name in RETIRED:
            if f"def {name}(" in text:
                found.append(f"{path.relative_to(TESTS)}::{name}")
    assert not found, (
        "retired test(s) back, probably from a merge — delete them unless "
        "that breaks something (see CLAUDE.md, 'Retired tests'): " +
        ", ".join(found))
