"""
tests/run_all.py gives every file's pytest a temp folder of its own and
removes it afterwards: four pytest processes sharing pytest-of-<user> race
in their clean-up of the pytest-current link, and the loser fails at session
end with FileNotFoundError (Windows CI on PR #70, tests/test_capture.py).
The folder is no longer than pytest's own: test_dream3d_e2e's 260-character
path test failed when it was (qt-migration CI after PR #70).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import run_all  # noqa: E402


def test_each_file_gets_its_own_temp_root_and_it_is_removed(tmp_path,
                                                            monkeypatch):
    seen = {}

    class FakeProc:
        def __init__(self, argv, cwd=None, env=None, stdout=None,
                     stderr=None):
            flag = next(a for a in argv if a.startswith("--basetemp="))
            base = flag.split("=", 1)[1]
            root = os.path.dirname(base)
            seen.setdefault("bases", []).append(base)
            seen.setdefault("roots", []).append(root)
            seen.setdefault("existed", []).append(os.path.isdir(root))
            seen["shared"] = os.environ.get("PYTEST_DEBUG_TEMPROOT")

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(run_all.subprocess, "Popen", FakeProc)
    test_file = ROOT / "tests" / "test_run_all.py"
    for _ in range(2):
        _path, status, _s = run_all.run_one(test_file, tmp_path, 60)
        assert status == "passed"
    first, second = seen["roots"]
    assert first != second, "one root per file"
    assert all(seen["existed"])
    assert not os.path.exists(first) and not os.path.exists(second), \
        "removed when the file ends"
    import getpass
    import tempfile
    pytests_own = os.path.join(tempfile.gettempdir(),
                               f"pytest-of-{getpass.getuser()}", "pytest-0")
    assert all(len(b) <= len(pytests_own) for b in seen["bases"]), \
        "no longer than pytest's own temp folder (Windows path limits)"
