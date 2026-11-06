"""
tests/run_all.py gives every file's pytest a temp root of its own and
removes it afterwards: four pytest processes sharing pytest-of-<user> race
in their clean-up of the pytest-current link, and the loser fails at session
end with FileNotFoundError (Windows CI on PR #70, tests/test_capture.py).
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
            root = env["PYTEST_DEBUG_TEMPROOT"]
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
