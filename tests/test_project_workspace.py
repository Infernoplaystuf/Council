"""A code project's brief, code map and references
(council_core/project.py, council_core/code_map.py)."""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from council_core import code_map, project as pj  # noqa: E402


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "camera.py").write_text(
        "class CameraTab:\n"
        "    def set_exposure(self, micros: int) -> None:\n"
        "        self.exposure = micros\n\n"
        "def open_camera(serial: str, timeout=5):\n    return serial\n",
        encoding="utf-8")
    (repo / "app" / "util.py").write_text("def clamp(x, lo, hi):\n"
                                          "    return max(lo, min(hi, x))\n",
                                          encoding="utf-8")
    (repo / ".hidden").mkdir()
    (repo / ".hidden" / "x.py").write_text("def secret(): pass\n")
    (repo / "CLAUDE.md").write_text("# Rules\nNever use tkinter.\n",
                                    encoding="utf-8")
    return repo


def test_projects_round_trip_and_live_in_the_vault(tmp_path):
    repo = make_repo(tmp_path)
    vault = tmp_path / "vault"
    p = pj.Project("Typhon GUI", str(repo), test_command="python -m pytest -q",
                   gui_checks=[pj.GuiCheck("app.camera", "CameraTab")])
    path = pj.save(vault, p)
    assert path.parent.name == "typhon-gui" and ".council_projects" in path.parts
    back = pj.load(vault, "Typhon GUI")
    assert back.gui_checks[0].cls == "CameraTab"
    assert [x.name for x in pj.list_projects(vault)] == ["Typhon GUI"]


def test_the_brief_reads_the_project_and_proposals_wait(tmp_path):
    repo = make_repo(tmp_path)
    vault = tmp_path / "vault"
    p = pj.Project("t", str(repo), test_command="pytest")
    text = pj.brief(vault, p)
    assert "Never use tkinter." in text and "TESTS: pytest" in text
    pj.propose_brief(vault, p, "Tabs live in app/.")
    assert "Tabs live in app/." not in pj.brief(vault, p)
    assert pj.accept_brief(vault, p)
    assert "Tabs live in app/." in pj.brief(vault, p)
    pj.propose_brief(vault, p, "Second version.")
    assert pj.accept_brief(vault, p)
    olds = list(pj.project_dir(vault, p).glob("brief-*.md"))
    assert len(olds) == 1 and "Tabs live" in olds[0].read_text()
    assert (repo / "CLAUDE.md").read_text().startswith("# Rules")


def test_the_code_map_has_signatures_and_skips_dot_folders(tmp_path):
    repo = make_repo(tmp_path)
    m = code_map.for_project(repo, tmp_path / "cache.json")
    assert set(m.files) == {"app/camera.py", "app/util.py"}
    assert any("CameraTab.set_exposure(self, micros: int) -> None" in i
               for i in m.files["app/camera.py"].items)
    assert m.find("open_camera") == [
        "app/camera.py:5 function open_camera(serial: str, timeout=5)"]
    text = m.render(focus="change the camera exposure", max_chars=2000)
    assert text.index("app/camera.py") < text.index("app/util.py")


def test_the_map_cache_skips_unchanged_files(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    cache = tmp_path / "cache.json"
    code_map.for_project(repo, cache)
    calls = []
    real = code_map._outline
    monkeypatch.setattr(code_map, "_outline",
                        lambda p: calls.append(p.name) or real(p))
    (repo / "app" / "util.py").write_text("def clamp2(x):\n    return x\n")
    os.utime(repo / "app" / "util.py", (time.time() + 5, time.time() + 5))
    m = code_map.for_project(repo, cache)
    assert calls == ["util.py"]
    assert m.find("clamp2")


def test_references_find_the_right_passage(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "camera_spec.md").write_text(
        "# Exposure\nThe EVK4 exposure range is 10 to 100000 microseconds.\n\n"
        + "filler text about nothing. " * 200, encoding="utf-8")
    (docs / "other.txt").write_text("Shipping addresses and invoices.",
                                    encoding="utf-8")
    (docs / "example.py").write_text(
        "def set_roi(x, y, w, h):\n    '''Crop the sensor area.'''\n",
        encoding="utf-8")
    refs = pj.ProjectReferences([str(docs)])
    hits = refs.search("what exposure range does the EVK4 take", k=2)
    assert hits and hits[0]["source"].endswith("camera_spec.md")
    assert refs.search("crop roi")[0]["source"].endswith("example.py")
    block = refs.block("EVK4 exposure")
    assert block.startswith("REFERENCES") and "100000" in block


def test_reference_changes_are_picked_up(tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("alpha", encoding="utf-8")
    refs = pj.ProjectReferences([str(f)])
    assert not refs.search("zebra")
    f.write_text("zebra crossing rules", encoding="utf-8")
    os.utime(f, (time.time() + 5, time.time() + 5))
    assert refs.search("zebra")
