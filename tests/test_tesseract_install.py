"""Installing Tesseract from the Models tab (council_core.tesseract_install) -
user 2026-10-22: "please also add into the Models tab an easy way to install
tesseract". Nothing here downloads anything: the installers are stand-ins."""
from __future__ import annotations

import os
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import source_extract as se  # noqa: E402
from council_core import tesseract_install as ti  # noqa: E402


@pytest.fixture
def app_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("COUNCIL_APP_DIR", str(tmp_path / "app"))
    monkeypatch.delenv(se.TESSERACT_ENV, raising=False)
    return tmp_path


def fake_tesseract(where: Path, answer: str = "tesseract 5.4.0") -> Path:
    """A program that answers --version as Tesseract does."""
    if os.name == "nt":
        exe = where / "tesseract.bat"
        exe.write_text(f"@echo {answer}\n", encoding="utf-8")
    else:
        exe = where / "tesseract"
        exe.write_text(f"#!/bin/sh\necho '{answer}'\n", encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return exe


def which(found: dict):
    return lambda name: found.get(name)


def test_windows_asks_winget_for_the_official_package(monkeypatch):
    monkeypatch.setattr(ti.shutil, "which", which({"winget": r"C:\winget.exe"}))
    p = ti.plan("win32")
    assert p.kind == "winget" and p.runnable
    assert p.argv[:4] == [r"C:\winget.exe", "install", "--id", "UB-Mannheim.TesseractOCR"]
    assert "Windows may ask you to allow it" in p.says


def test_without_winget_conda_needs_no_administrator(monkeypatch):
    monkeypatch.setattr(ti.shutil, "which", which({"conda": "/opt/conda/bin/conda"}))
    p = ti.plan("win32")
    assert p.kind == "conda" and "administrator" in p.says
    assert p.argv[:4] == ["/opt/conda/bin/conda", "create", "-y", "-n"]
    assert "tesseract" in p.argv and "conda-forge" in p.argv


def test_mac_and_linux(monkeypatch):
    monkeypatch.setattr(ti.shutil, "which", which({"brew": "/opt/homebrew/bin/brew"}))
    monkeypatch.setattr(ti, "_conda", lambda: None)
    assert ti.plan("darwin").argv == ["/opt/homebrew/bin/brew", "install", "tesseract"]
    linux = ti.plan("linux")
    assert not linux.runnable and "sudo apt install tesseract-ocr" in linux.manual
    monkeypatch.setattr(ti.shutil, "which", which({}))
    assert "winget is not on this PC" in ti.plan("win32").manual


def test_an_install_streams_its_output_and_finds_tesseract_after(app_dir, monkeypatch):
    exe = fake_tesseract(app_dir)
    script = app_dir / "installer.py"
    script.write_text("print('Found Tesseract OCR [UB-Mannheim.TesseractOCR]')\n"
                      "print('Successfully installed')\n", encoding="utf-8")
    found = {"exe": None}
    monkeypatch.setattr(ti, "find", lambda: found["exe"])
    lines = []

    def on_line(text):
        lines.append(text)
        found["exe"] = exe                  # 'installed' as the installer ran
    r = ti.install(ti.Plan("winget", [sys.executable, str(script)]), on_line=on_line)
    assert lines == ["Found Tesseract OCR [UB-Mannheim.TesseractOCR]", "Successfully installed"]
    assert r.ok and r.found == exe and "tesseract 5.4.0 is installed" in r.message


def test_an_install_that_fails_says_so(app_dir, monkeypatch):
    monkeypatch.setattr(ti, "find", lambda: None)
    r = ti.install(ti.Plan("winget", [sys.executable, "-c", "import sys; sys.exit(3)"]))
    assert not r.ok and r.message.startswith("winget exited 3") and "Locate" in r.message
    manual = ti.install(ti.Plan("manual", manual="sudo apt install tesseract-ocr"))
    assert not manual.ok and manual.message == "sudo apt install tesseract-ocr"


def test_located_tesseract_is_used_by_the_ledger(app_dir, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    exe = fake_tesseract(app_dir)
    assert ti.locate(exe) == exe
    assert se.find_tesseract() == exe
    assert ti.version() == "tesseract 5.4.0"
    assert ti.status() == f"Tesseract: tesseract 5.4.0 — {exe}"
    other = app_dir / "notes.txt"
    other.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        ti.locate(other)


# ── the Models tab ──────────────────────────────────────────────────────
class Actions:
    """The Models tab's actions, with no hardware probe and no installer."""

    def __init__(self, plan, result):
        self._plan, self._result = plan, result
        self.installed = []
        self.state = "Tesseract (reads text and tables in pictures, for the Ledger and " \
                     "Connections): not installed"

    def detect(self):
        return SimpleNamespace(summary="test PC")

    def find(self, hardware, task="", online=False):
        return SimpleNamespace(message="", rows=[])

    def upgrade(self, hardware):
        return "", None

    def tesseract_status(self):
        return self.state

    def tesseract_plan(self):
        return self._plan

    def install_tesseract(self, plan, *, on_line=None, should_stop=None):
        on_line("Successfully installed")
        self.installed.append(plan.kind)
        self.state = "Tesseract: tesseract 5.4.0 — C:/Program Files/Tesseract-OCR/tesseract.exe"
        return self._result


@pytest.fixture
def qapp(monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QWidget
    app = QApplication.instance() or QApplication([])
    # the per-role panel asks the local Ollama for its models: not here
    from council_qt.widgets import role_models

    class NoRoles(QWidget):
        def __init__(self, *a, parent=None, **k):
            super().__init__(parent)
    monkeypatch.setattr(role_models, "RoleModelsPanel", NoRoles)
    return app


def settle(qapp, until, timeout=10.0):
    end = time.monotonic() + timeout
    while not until() and time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()
    return until()


def test_the_models_tab_installs_it_after_asking(qapp, monkeypatch):
    from council_qt import dialogs
    from council_qt.tabs.models import ModelsTab
    plan = ti.Plan("winget", ["winget"], says="Windows' own installer (winget) will…")
    acts = Actions(plan, ti.Result(True, Path("t.exe"), [], "tesseract 5.4.0 is installed"))
    tab = ModelsTab(actions=acts, role_actions=None)
    assert settle(qapp, lambda: "not installed" in tab.tesseract.text())
    assert tab.tesseract_btn.text() == "Install Tesseract"
    asked = []
    monkeypatch.setattr(dialogs, "confirm", lambda *a, **k: asked.append(a) or False)
    tab.tesseract_btn.click()                      # No: nothing runs
    assert asked and acts.installed == []
    monkeypatch.setattr(dialogs, "confirm", lambda *a, **k: True)
    tab.tesseract_btn.click()
    assert settle(qapp, lambda: tab._tesseract_worker is None and acts.installed)
    assert acts.installed == ["winget"] and "5.4.0" in tab.tesseract.text()
    assert tab.tesseract_btn.text() == "Reinstall Tesseract"
    assert tab.status.text() == "tesseract 5.4.0 is installed"


def test_unattended_it_never_installs(qapp, monkeypatch):
    from council_qt.tabs.models import ModelsTab
    monkeypatch.setenv("COUNCIL_NO_DIALOGS", "1")
    acts = Actions(ti.Plan("winget", ["winget"]), ti.Result(True, None, [], ""))
    tab = ModelsTab(actions=acts, role_actions=None)
    tab.tesseract_btn.click()
    settle(qapp, lambda: False, timeout=0.3)
    assert acts.installed == []


def test_a_manual_plan_shows_the_command(qapp, monkeypatch):
    from council_qt.tabs.models import ModelsTab
    monkeypatch.setenv("COUNCIL_NO_DIALOGS", "1")
    acts = Actions(ti.Plan("manual", manual="sudo apt install tesseract-ocr"),
                   ti.Result(False, None, [], ""))
    tab = ModelsTab(actions=acts, role_actions=None)
    tab.tesseract_btn.click()
    assert tab.status.text() == "sudo apt install tesseract-ocr" and acts.installed == []
