"""
A mutation harness for this repo. Not a test — a tool the tests are checked with.

WHY IT EXISTS
Every behavioural guarantee in this port is checked by breaking it and watching
a test fail. Doing that by hand is where three separate mistakes have come
from, so the loop is written once:

  - `subprocess.run(text=True)` decodes with the LOCALE encoding, which on
    Windows is cp1252. Test output here contains ●, ✕, —, and em dashes, so the
    reader thread dies with UnicodeDecodeError and the whole run is lost —
    three times, in three different harnesses. This passes utf-8 explicitly.
  - A mutation whose `old` string is not in the file silently does nothing and
    looks like a pass. It is reported as NOT APPLIED and counts as a failure.
  - A run that dies partway leaves the source mutated. The restore is in a
    `finally`, and `verify()` re-checks every file afterwards.

USAGE
    from tests.mutate import Mutation, run
    run([Mutation(path, "why", old, new, "tests/test_x.py::test_y")])
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence

#: Long enough for a Qt tab test that pumps an event loop, short enough that a
#: hung mutation does not cost the whole run.
TIMEOUT = 300


@dataclass
class Mutation:
    """One break, and the test that must notice it."""
    path: Path
    why: str
    old: str
    new: str
    test: str


def _pytest(test: str) -> int:
    done = subprocess.run(
        [sys.executable, "-m", "pytest", test, "-q", "-p", "no:randomly"],
        capture_output=True, text=True,
        # NOT the locale encoding. See the module docstring.
        encoding="utf-8", errors="replace", timeout=TIMEOUT)
    return done.returncode


def run(mutations: Sequence[Mutation], *, verbose: bool = True) -> int:
    """Apply each mutation, run its test, restore. Returns the survivor count.

    A survivor is a test that PASSED while the thing it checks was broken —
    which means it checks nothing. Exit code is non-zero if any survived or any
    failed to apply.
    """
    originals = {m.path: m.path.read_text(encoding="utf-8") for m in mutations}
    survived: List[str] = []
    unapplied: List[str] = []

    for mutation in mutations:
        original = mutation.path.read_text(encoding="utf-8")
        if mutation.old not in original:
            unapplied.append(mutation.why)
            if verbose:
                print(f"NOT APPLIED  {mutation.why}")
            continue
        mutation.path.write_text(original.replace(mutation.old, mutation.new,
                                                  1), encoding="utf-8")
        try:
            failed = bool(_pytest(mutation.test))
        except subprocess.TimeoutExpired:
            failed = True
        finally:
            mutation.path.write_text(original, encoding="utf-8")
        if failed:
            if verbose:
                print(f"CAUGHT       {mutation.why}")
        else:
            survived.append(mutation.why)
            if verbose:
                print(f"SURVIVED     {mutation.why}")

    # Every file back as it was, whatever happened above.
    for path, text in originals.items():
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            print(f"RESTORED     {path}")

    caught = len(mutations) - len(survived) - len(unapplied)
    if verbose:
        print()
        print(f"{caught}/{len(mutations)} caught")
    return len(survived) + len(unapplied)
