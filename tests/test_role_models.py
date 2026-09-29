"""
The Models tab's "Which model answers for each role" section
(council_qt.widgets.role_models).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import model_catalog
from council_core import model_slots as ms

pytest.importorskip("PySide6", reason="the Roles section needs PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from council_qt.widgets.role_models import (RoleActions,  # noqa: E402
                                            RoleModelsPanel, display_name)

GB = ms.GB
PHI = model_catalog.by_id("phi-4-q4")
LLAMA3B = model_catalog.by_id("llama-3.2-3b-q5")


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
    """Real map logic over a temp folder; no GPU, no engine, no network."""

    def __init__(self, tmp_path, files=("granite.gguf",), free=16 * GB):
        super().__init__(tmp_path / "vault")
        self.dir = tmp_path / "models"
        self.dir.mkdir(exist_ok=True)
        for name in files:
            (self.dir / name).write_bytes(b"x")
        self.free = free
        self.saved = []
        self.downloaded = []
        self.threads = []

    def main_path(self):
        return str(self.dir / "granite.gguf")

    def files(self):
        return ms.known_files([self.dir])

    def free_vram(self):
        return self.free

    def save(self, role_files):
        self.threads.append(threading.current_thread().name)
        self.saved.append(dict(role_files))
        return "Saved."

    def missing_for(self, preset):
        return ms.preset_missing(preset, [self.dir])

    def download(self, model_id, on_progress=None):
        spec = ms.catalog_spec(model_id)
        on_progress(50, 100)
        (self.dir / spec.hf_file).write_bytes(b"x")
        self.downloaded.append(model_id)
        return self.dir / spec.hf_file

    def preset_role_files(self, preset):
        files = {m: ms.find_catalog_file(m, [self.dir])
                 for m in [preset["main"]] + list(preset["slots"].values())}
        out = {r: str(files[preset["main"]]) for r in ms.COUNCIL_ROLES}
        for role, slot in preset["roles"].items():
            out[role] = str(files[preset["slots"][slot]])
        return out


@pytest.fixture
def make(qapp, tmp_path):
    made = []

    def build(actions=None, confirm=lambda *a, **k: True):
        panel = RoleModelsPanel(actions or Actions(tmp_path), confirm=confirm)
        pump(qapp, lambda: panel._free is not None)
        made.append(panel)
        return panel

    yield build
    for p in made:
        pump(qapp, lambda p=p: not p._busy)
        p.deleteLater()
    qapp.processEvents()


def test_every_role_starts_on_the_current_model(make):
    panel = make()
    assert set(panel.combos) == set(ms.COUNCIL_ROLES)
    for combo in panel.combos.values():
        assert combo.currentData().endswith("granite.gguf")


def test_display_names_use_the_catalog(tmp_path):
    f = tmp_path / PHI.hf_file
    f.write_bytes(b"x")
    assert display_name(f).startswith("Microsoft Phi-4 14B")


def test_the_plan_line_says_where_each_model_runs(make, tmp_path):
    actions = Actions(tmp_path, files=("granite.gguf", "small.gguf"))
    panel = make(actions)
    panel.apply_role_files({"peasant": str(actions.dir / "small.gguf")})
    assert "→ GPU" in panel.plan_label.text()


def test_one_model_for_all(make, tmp_path):
    actions = Actions(tmp_path, files=("granite.gguf", "small.gguf"))
    panel = make(actions)
    panel.apply_role_files({"peasant": str(actions.dir / "small.gguf")})
    panel.on_one_model()
    assert {c.currentData() for c in panel.combos.values()} == \
        {str(actions.dir / "granite.gguf")}


def test_save_runs_off_the_gui_thread(qapp, make):
    panel = make()
    panel.on_save()
    pump(qapp, lambda: panel.status.text() == "Saved.")
    assert panel.actions.threads == ["model-roles-save"]
    assert set(panel.actions.saved[0]) == set(ms.COUNCIL_ROLES)


def test_balanced_downloads_what_is_missing_then_saves(qapp, make, tmp_path):
    panel = make(Actions(tmp_path))
    panel.on_balanced()
    pump(qapp, lambda: panel.actions.saved)
    assert set(panel.actions.downloaded) == {"phi-4-q4", "llama-3.2-3b-q5"}
    saved = panel.actions.saved[0]
    assert saved["writer"].endswith(PHI.hf_file)
    assert saved["judge"].endswith(PHI.hf_file)
    for role in ("peasant", "intern", "artist"):
        assert saved[role].endswith(LLAMA3B.hf_file)


def test_balanced_asks_before_downloading(make, tmp_path):
    panel = make(Actions(tmp_path), confirm=lambda *a, **k: False)
    panel.on_balanced()
    assert panel.actions.downloaded == [] and panel.actions.saved == []


def test_balanced_with_the_files_present_just_saves(qapp, make, tmp_path):
    actions = Actions(tmp_path, files=("granite.gguf", PHI.hf_file,
                                       LLAMA3B.hf_file))
    panel = make(actions, confirm=lambda *a, **k: pytest.fail("asked"))
    panel.on_balanced()
    pump(qapp, lambda: actions.saved)
    assert actions.downloaded == []


# -- RoleActions.save, for real ----------------------------------------------

def test_save_writes_the_map_sets_main_and_reloads(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "onboarding", SimpleNamespace(
        save_gguf_path=lambda v, p: calls.append(("main", p))))
    monkeypatch.setitem(sys.modules, "council_engine", SimpleNamespace(
        refresh_backend_config=lambda: calls.append(("refresh",))))
    big, small = tmp_path / "big.gguf", tmp_path / "small.gguf"
    actions = RoleActions(tmp_path / "vault")
    files = {r: str(big) for r in ms.COUNCIL_ROLES}
    files["peasant"] = str(small)
    line = actions.save(files)
    assert "2 models" in line
    cfg = ms.load(tmp_path / "vault")
    assert cfg.slot_for("peasant") != "main" and cfg.slot_for("writer") == "main"
    assert calls == [("main", str(big)), ("refresh",)]


# -- a backend_settings.json that cannot be read ------------------------------

_KEEP = {"gguf_path": "C:/chosen.gguf", "clip_path": "C:/mmproj.gguf",
         "role_models": {"sage": "C:/sage.gguf"}}
#: PowerShell 5.1's Out-File / `>` default. Not UTF-8, so not readable.
_UTF16 = json.dumps(_KEEP).encode("utf-16")


def test_save_says_when_the_main_model_could_not_be_saved(tmp_path,
                                                          monkeypatch):
    """MEASURED 2026-09-29: onboarding._merge_backend_settings, which Save
    goes through, rewrote a settings file it could not read — UTF-16,
    truncated, cp1252 — with only the key it was saving (clip_path and
    role_models gone), and Save said "Saved" as if the next launch would
    start on the Writer's model. The file is now left alone and Save's line
    says the main model was NOT saved, and where the file is."""
    for name in ("COUNCIL_GGUF_PATH", "COUNCIL_GGUF_PATH_AUTO"):
        monkeypatch.setenv(name, "x")        # recorded, so put back after
        monkeypatch.delenv(name)
    monkeypatch.setitem(sys.modules, "council_engine", SimpleNamespace(
        refresh_backend_config=lambda: None))
    vault = tmp_path / "vault"
    vault.mkdir()
    settings = vault / "backend_settings.json"
    settings.write_bytes(_UTF16)
    big = tmp_path / "big.gguf"
    line = RoleActions(vault).save({r: str(big) for r in ms.COUNCIL_ROLES})
    assert settings.read_bytes() == _UTF16, "a file it could not read was replaced"
    assert "NOT saved for the next launch" in line, line
    assert str(settings) in line
    # The role map itself was saved, and the Writer's model is live now.
    assert ms.load(vault).slot_for("writer") == "main"
    assert os.environ["COUNCIL_GGUF_PATH"] == str(big)


def test_save_says_nothing_extra_when_the_settings_were_saved(tmp_path,
                                                              monkeypatch):
    for name in ("COUNCIL_GGUF_PATH", "COUNCIL_GGUF_PATH_AUTO"):
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)
    monkeypatch.setitem(sys.modules, "council_engine", SimpleNamespace(
        refresh_backend_config=lambda: None))
    vault = tmp_path / "vault"
    vault.mkdir()
    big = tmp_path / "big.gguf"
    line = RoleActions(vault).save({r: str(big) for r in ms.COUNCIL_ROLES})
    assert line.startswith("Saved — 1 model for the council."), line
    data = json.loads((vault / "backend_settings.json").read_text("utf-8"))
    assert data == {"gguf_path": str(big)}


# role_models.RoleModelRegistry (the TOP-LEVEL role_models.py, not this
# widget) reads and writes the same file, and had the same two faults the
# onboarding fix removed: a BOM file read as {}, and a file it could not read
# was replaced by one holding only the role map.

def test_the_role_registry_reads_and_keeps_a_bom_settings_file(tmp_path):
    """MEASURED 2026-09-29: with a BOM, .all() == {} and .set("judge", ...)
    rewrote the file as {"role_models": {"judge": ...}} — gguf_path,
    clip_path and the sage assignment gone."""
    import role_models
    vault = tmp_path / "vault"
    vault.mkdir()
    settings = vault / "backend_settings.json"
    settings.write_bytes(b"\xef\xbb\xbf" + json.dumps(_KEEP).encode("utf-8"))
    registry = role_models.RoleModelRegistry(vault)
    assert registry.all() == {"sage": "C:/sage.gguf"}
    registry.set("judge", "C:/judge.gguf")
    assert json.loads(settings.read_text("utf-8-sig")) == dict(
        _KEEP, role_models={"sage": "C:/sage.gguf", "judge": "C:/judge.gguf"})


def test_the_role_registry_never_replaces_a_file_it_could_not_read(tmp_path):
    """MEASURED 2026-09-29: a UTF-16 file was rewritten as
    {"role_models": {"judge": ...}}."""
    import role_models
    vault = tmp_path / "vault"
    vault.mkdir()
    settings = vault / "backend_settings.json"
    settings.write_bytes(_UTF16)
    try:
        role_models.RoleModelRegistry(vault).set("judge", "C:/judge.gguf")
    except Exception:                                     # noqa: BLE001
        pass           # refusing loudly is as good as refusing quietly
    assert settings.read_bytes() == _UTF16


def test_the_models_tab_carries_the_roles_section(qapp, tmp_path):
    from council_qt.tabs.models import ModelsActions, ModelsTab

    class Quiet(ModelsActions):
        def detect(self):
            from council_core import model_jobs
            return model_jobs.Hardware()

        def find(self, *a, **k):
            from council_core import model_jobs
            return model_jobs.FindResult(True, "")

        def upgrade(self, *a, **k):
            return "", None

    tab = ModelsTab(actions=Quiet(tmp_path), role_actions=Actions(tmp_path))
    assert isinstance(tab.roles, RoleModelsPanel)
    pump(qapp, lambda: tab.roles._free is not None)
    tab.deleteLater()
    qapp.processEvents()
