"""What a coding agent may do in its worktree (council_core/project_tools.py)
and the offscreen GUI check (council_core/gui_check.py)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from council_core import code_map  # noqa: E402
from council_core.project import GuiCheck  # noqa: E402
from council_core.project_tools import Workspace  # noqa: E402

CAMERA = '''class Camera:
    def __init__(self):
        self.exposure = 100

    def set_exposure(self, micros):
        self.exposure = micros

    def reset(self):
        self.exposure = 100
'''

TAB = '''from PySide6.QtWidgets import QWidget, QVBoxLayout, QPushButton


class CameraTab(QWidget):
    def __init__(self):
        super().__init__()
        lay = QVBoxLayout(self)
        self.save = QPushButton("Save")
        self.save.setObjectName("save")
        lay.addWidget(self.save)
'''

TEST = '''from camera import Camera


def test_exposure():
    c = Camera()
    c.set_exposure(50)
    assert c.exposure == 50
'''


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    (root / "camera.py").write_text(CAMERA)
    (root / "camera_tab.py").write_text(TAB)
    (root / "test_camera.py").write_text(TEST)
    (root / ".git").mkdir()
    w = Workspace(root, test_command="python -m pytest -q -p no:cacheprovider",
                  gui_checks=[GuiCheck("camera_tab", "CameraTab")],
                  code_map=lambda: code_map.for_project(root),
                  out_dir=tmp_path / "job")
    return w, w.tools()


def call(tools, _tool, **args):
    return tools[_tool](args)


def test_every_tool_has_help_and_a_schema(ws):
    _w, tools = ws
    assert all(getattr(f, "help", "") and getattr(f, "params", None)
               for f in tools.values())


def test_read_and_find(ws):
    _w, t = ws
    ok, msg, _ = call(t, "read_file", path="camera.py", start=5, end=6)
    assert "    5      def set_exposure(self, micros):" in msg
    ok, msg, _ = call(t, "find_symbol", name="set_exposure")
    assert "camera.py:5 method Camera.set_exposure(self, micros)" in msg
    ok, msg, p = call(t, "search_code", pattern=r"self\.exposure = 100")
    assert p["hits"] == 2


def test_edit_needs_one_exact_match(ws):
    w, t = ws
    ok, msg, _ = call(t, "edit_file", path="camera.py",
                      old="self.exposure = 100", new="self.exposure = 200")
    assert not ok and "appears 2 times (lines 3, 9)" in msg
    ok, msg, _ = call(t, "edit_file", path="camera.py",
                      old="def set_exposur(self, micro):", new="x")
    assert not ok and "closest text is at line 5" in msg
    ok, msg, _ = call(t, "edit_file", path="camera.py",
                      old="    def reset(self):\n        self.exposure = 100",
                      new="    def reset(self):\n        self.exposure = 0")
    assert ok and w.touched == ["camera.py"]
    assert "self.exposure = 0" in (w.root / "camera.py").read_text()
    ok, msg, _ = call(t, "edit_file", path="camera.py",
                      old="def reset(self):", new="def reset(self:")
    assert ok and "no longer parses" in msg


def test_paths_stay_in_the_worktree(ws):
    _w, t = ws
    for bad in ("../outside.py", "/etc/passwd", ".git/config"):
        ok, msg, _ = call(t, "read_file", path=bad)
        assert not ok
    ok, msg, _ = call(t, "create_file", path="camera.py", content="x")
    assert not ok and "already exists" in msg
    ok, msg, _ = call(t, "create_file", path="pkg/new.py", content="a = 1\n")
    assert ok


def test_run_tests(ws):
    w, t = ws
    ok, msg, p = call(t, "run_tests")
    assert p["passed"], msg
    call(t, "edit_file", path="camera.py",
         old="        self.exposure = micros", new="        self.exposure = 1")
    ok, msg, p = call(t, "run_tests", target="test_camera.py")
    assert not p["passed"] and "FAILED" in msg


def test_gui_check(ws):
    pytest.importorskip("PySide6")
    w, t = ws
    ok, msg, p = call(t, "gui_check")
    assert p["ok"] and "QPushButton #save 'Save'" in msg
    assert Path(p["screenshots"][0]).exists()
    call(t, "edit_file", path="camera_tab.py",
         old="        lay.addWidget(self.save)",
         new="        lay.addWidget(self.nope)")
    ok, msg, p = call(t, "gui_check")
    assert not p["ok"] and "AttributeError" in msg


def test_step_done(ws):
    w, t = ws
    call(t, "step_done", summary="added reset")
    assert w.done == "added reset"
