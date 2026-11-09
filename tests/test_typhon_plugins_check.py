"""
The plugin author's check, `python -m typhon_plugin_api.check <folder>`
(stage P5 row 2; docs/typhon_camera_plugins.md), run on the guide's worked
example — the webcam plugin of examples/typhon_plugins/example_webcam —
over a stand-in OpenCV, and on folders it must fail.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from typhon_plugin_api import check  # noqa: E402

EXAMPLE = ROOT / "examples" / "typhon_plugins" / "example_webcam"


def _fake_cv2_module():
    spec = importlib.util.spec_from_file_location(
        "webcam_tests", EXAMPLE / "tests" / "test_webcam.py")
    tests = importlib.util.module_from_spec(spec)
    kept = sys.dont_write_bytecode
    sys.dont_write_bytecode = True        # nothing into the plugin's folder
    try:
        spec.loader.exec_module(tests)
    finally:
        sys.dont_write_bytecode = kept
    return tests.FakeCv2(cameras=1)


@pytest.fixture
def fake_cv2(monkeypatch):
    monkeypatch.setitem(sys.modules, "cv2", _fake_cv2_module())


def statuses(report):
    return {what: status for status, what, _d in report.lines}


def test_the_webcam_example_passes_every_camera_check(fake_cv2):
    report = check.run_check(EXAMPLE, own_tests=False, out=lambda s: None)
    got = statuses(report)
    assert report.failed == 0, report.lines
    assert got["open, area, settings, start, read, stop, close (twice)"] \
        == check.PASS
    assert got["Typhon connects it and records 3 frames"] == check.PASS
    assert not list(EXAMPLE.rglob("__pycache__"))


def test_without_its_sdk_the_camera_checks_are_skipped_and_said(
        monkeypatch):
    monkeypatch.setitem(sys.modules, "cv2", None)       # import fails
    report = check.run_check(EXAMPLE, own_tests=False, out=lambda s: None)
    assert report.failed == 0
    assert statuses(report)["available() answers"] == check.SKIP


def test_a_plugin_reaching_into_typhon_fails(tmp_path, fake_cv2):
    folder = tmp_path / "bad"
    shutil.copytree(EXAMPLE, folder)
    code = (folder / "plugin.py").read_text(encoding="utf-8")
    (folder / "plugin.py").write_text(
        code.replace("import typhon_plugin_api as api",
                     "import typhon_plugin_api as api\n"
                     "from council_core import cameras"), encoding="utf-8")
    report = check.run_check(folder, own_tests=False, out=lambda s: None)
    assert statuses(report)["its imports"] == check.FAIL


def test_a_camera_confirming_more_than_its_manifest_fails(tmp_path,
                                                          fake_cv2):
    folder = tmp_path / "bad"
    shutil.copytree(EXAMPLE, folder)
    doc = json.loads((folder / "plugin.json").read_text(encoding="utf-8"))
    doc["capabilities"] = ["frames"]                # not "restartable"
    (folder / "plugin.json").write_text(json.dumps(doc), encoding="utf-8")
    report = check.run_check(folder, own_tests=False, out=lambda s: None)
    line = [d for s, w, d in report.lines if w.startswith("open, area")]
    assert report.failed and "restartable" in line[0]


def test_the_command_runs_the_examples_own_tests():
    done = subprocess.run(
        [sys.executable, "-m", "typhon_plugin_api.check", str(EXAMPLE)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "PASS  its own tests (pytest)" in done.stdout
