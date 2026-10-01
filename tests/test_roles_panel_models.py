"""
The Roles panel with Ollama models next to GGUF files: origin on every entry,
any model assignable to any role (Docs included), "Suggest for this PC" from
what is installed (US-made only), and Save with the Writer on Ollama.
Offscreen; no engine, no network (the model list is handed in).
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from council_core import local_models
from council_core import model_slots as ms
from tests.fake_ollama import DEFAULT_TAGS, tag

pytest.importorskip("PySide6", reason="the Roles section needs PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from council_qt.widgets.role_models import (RoleActions,  # noqa: E402
                                            RoleModelsPanel, stats_line)

GB = ms.GB
OLLAMA = [local_models.ollama_entry(t, fill_from_show=False)
          for t in DEFAULT_TAGS] + [local_models.ollama_entry(tag(
              "phi4:14b", size=9_053_116_391, family="phi3", params="14.7B",
              quant="Q4_K_M", ctx=16384), fill_from_show=False)]


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def pump(qapp, until, seconds=5.0):
    deadline = time.time() + seconds
    while not until() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    assert until(), "timed out"


class Actions(RoleActions):
    """Real map logic over a temp folder; the model list handed in."""

    def __init__(self, tmp_path, models=OLLAMA, current=None):
        super().__init__(tmp_path / "vault")
        self.dir = tmp_path / "models"
        self.dir.mkdir(exist_ok=True)
        (self.dir / "granite.gguf").write_bytes(b"x")
        self._models = list(models)
        self._current = current
        self.saved = []

    def main_path(self):
        return str(self.dir / "granite.gguf")

    def files(self):
        return ms.known_files([self.dir])

    def models(self):
        return list(self._models)

    def hardware(self):
        return 8.0, 31.7

    def free_vram(self):
        return int(7.7 * GB)

    def role_files(self):
        if self._current:
            return dict(self._current)
        return super().role_files()

    def save(self, role_files):
        self.saved.append(dict(role_files))
        return "Saved."


@pytest.fixture
def make(qapp, tmp_path):
    made = []

    def build(actions=None):
        panel = RoleModelsPanel(actions or Actions(tmp_path),
                                confirm=lambda *a, **k: True)
        pump(qapp, lambda: panel._free is not None)
        made.append(panel)
        return panel

    yield build
    for p in made:
        pump(qapp, lambda p=p: not p._busy)
        p.deleteLater()
    qapp.processEvents()


def _items(combo):
    return [(combo.itemText(i), combo.itemData(i))
            for i in range(combo.count())]


def test_ollama_models_are_listed_next_to_gguf_files_with_origin(make):
    panel = make()
    items = dict((data, text) for text, data in _items(panel.combos["docs"]))
    assert any(d.endswith("granite.gguf") for d in items)
    assert "Meta · US · Ollama" in items["ollama:llama3.1:8b"]
    qwen = items["ollama:qwen2.5:7b-instruct-q4_K_M"]
    assert "not US — measure only" in qwen
    # The current choice survived the scan.
    assert panel.combos["writer"].currentData().endswith("granite.gguf")


def test_the_docs_role_has_its_own_choice(make):
    panel = make()
    assert "docs" in panel.combos
    panel.apply_role_files({"docs": "ollama:llama3.1:8b"})
    assert panel.role_files()["docs"] == "ollama:llama3.1:8b"
    assert panel.role_files()["writer"].endswith("granite.gguf")


def test_suggest_fills_every_role_with_an_installed_us_model(make):
    panel = make()
    panel.on_suggest()
    chosen = panel.role_files()
    assert set(chosen.values()) == {"ollama:llama3.1:8b"}
    assert "llama3.1:8b" in panel.status.text()
    assert "Save" in panel.status.text()


def test_suggest_never_picks_a_non_us_model(make, tmp_path):
    only_qwen = [m for m in OLLAMA if "qwen" in m["name"]]
    panel = make(Actions(tmp_path, models=only_qwen))
    before = panel.role_files()
    panel.on_suggest()
    assert panel.role_files() == before
    assert "No installed US-origin model" in panel.status.text()


def test_the_plan_line_says_ollama_runs_it_and_flags_non_us(make):
    panel = make()
    panel.apply_role_files({r: "ollama:llama3.1:8b" for r in panel.combos})
    assert "llama3.1:8b → Ollama (4.6 GB, fits the GPU)" in \
        panel.plan_label.text()
    panel.apply_role_files({"peasant": "ollama:qwen2.5:7b-instruct-q4_K_M"})
    text = panel.plan_label.text()
    assert "not US — measure only" in text
    assert "reloads weights" in text           # two Ollama models
    panel.apply_role_files({"coder": "ollama:phi4:14b"})
    assert "part GPU, part RAM" in panel.plan_label.text()


def test_a_saved_ollama_slot_is_selected_on_reload(make, tmp_path):
    current = {r: "ollama:llama3.1:8b" for r in ms.COUNCIL_ROLES}
    panel = make(Actions(tmp_path, current=current))
    assert panel.combos["docs"].currentData() == "ollama:llama3.1:8b"


def test_save_with_the_writer_on_ollama_leaves_the_gguf_path_alone(
        tmp_path, monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "onboarding", SimpleNamespace(
        save_gguf_path=lambda v, p: calls.append(("main", p))))
    monkeypatch.setitem(sys.modules, "council_engine", SimpleNamespace(
        refresh_backend_config=lambda: calls.append(("refresh",))))
    files = {r: "ollama:llama3.1:8b" for r in ms.COUNCIL_ROLES}
    files["coder"] = str(tmp_path / "coder.gguf")
    line = RoleActions(tmp_path / "vault").save(files)
    assert calls == [("refresh",)]                  # no save_gguf_path
    cfg = ms.load(tmp_path / "vault")
    assert cfg.slots["main"].path == "ollama:llama3.1:8b"
    assert cfg.slot_for("docs") == "main"
    assert cfg.slots[cfg.slot_for("coder")].path.endswith("coder.gguf")
    assert line.startswith("Saved — 2 models")


def test_the_last_answer_line():
    assert stats_line({}) == ""
    line = stats_line({"backend": "ollama", "model": "llama3.1:8b",
                       "gen_tokens": 812, "gen_tok_s": 41.2,
                       "prompt_tokens": 2087, "prompt_tok_s": 1234.5,
                       "seconds": 21.34, "constrained": True,
                       "constraint": "schema", "schema_valid": True})
    assert line.startswith("Last answer: llama3.1:8b via Ollama")
    assert "812 tokens at 41.2 tok/s" in line and "21.3 s" in line
    assert "constrained (schema)" in line


def test_the_models_tab_has_a_check_this_pc_button(qapp, tmp_path,
                                                   monkeypatch):
    from council_core import model_jobs
    from council_qt.tabs.models import ModelsActions, ModelsTab

    class Quiet(ModelsActions):
        def detect(self):
            return model_jobs.Hardware()

        def find(self, *a, **k):
            return model_jobs.FindResult(True, "")

        def upgrade(self, *a, **k):
            return "", None

        def check_this_pc(self, **kw):
            self.ran = threading.current_thread().name
            return model_jobs.CheckResult(True, "1 model measured")

    actions = Quiet(tmp_path)
    tab = ModelsTab(actions=actions, role_actions=Actions(tmp_path))
    pump(qapp, lambda: tab.roles._free is not None)
    tab.on_check_pc()
    pump(qapp, lambda: tab.status.text() == "1 model measured")
    assert actions.ran == "model-check-pc"
    tab.deleteLater()
    qapp.processEvents()
