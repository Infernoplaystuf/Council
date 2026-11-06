"""
Export an app built in the GUI Designer to a folder of its own (Andy, 6
November 2026: "the Council is the development suite for Typhon but I can
export it to other machines").

  * the folder: the app as generated, main.py pointing at _council beside
    it, exactly the Council files the app's imports reach (Typhon: the
    camera code, never the engine, the Ledger or the models), run.bat /
    run.sh with a data folder of its own, requirements, README, export.json;
  * it stands alone: started in a separate Python that cannot see the
    Council (check), it imports every copied file and builds its window —
    today's Typhon and "Typhon (plugins)";
  * refusals: an existing folder, a folder in the Council, a project never
    generated; nothing written;
  * the Designer's "Export app…" does the same and says it was checked;
  * a generated main.py finds a moved Council again (its root marker is a
    file the Council still has).

Temporary vaults, plugin homes and folders only; offscreen.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import gui_emit  # noqa: E402
import run_example_gui as rex  # noqa: E402
from council_core import app_export as ae  # noqa: E402
from council_core import camera_record  # noqa: E402

pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def typhon(tmp_path_factory):
    vault = tmp_path_factory.mktemp("export_vault")
    return Path(rex.build("typhon", project="typhon_x", vault_dir=vault,
                          target="qt"))


@pytest.fixture(scope="module")
def exported(typhon, tmp_path_factory):
    dest = tmp_path_factory.mktemp("exports")
    return ae.export_app(typhon, dest, name="Typhon")


def test_the_folder_holds_the_app_and_only_what_it_uses(exported, typhon):
    folder = exported.folder
    assert exported.ok and folder.name == "Typhon"
    for name in ("main.py", "app.py", "handlers.py", "project.gspec",
                 "manifest.json", "run.bat", "run.sh", "requirements.txt",
                 "README.txt", "export.json"):
        assert (folder / name).is_file(), name
    assert (folder / "data").is_dir()
    lib = folder / ae.LIB_DIR
    for wanted in ("frame_camera.py", "frame_classes.py",
                   "council_core/cameras.py", "council_core/capture.py",
                   "council_qt/widgets/capture_review.py"):
        assert (lib / wanted).is_file(), wanted
    for never in ("council_engine.py", "council_qt.py",
                  "council_core/ledger_workers.py", "council_qt/window.py"):
        assert not (lib / never).exists(), never
    assert "frame_camera" in exported.modules
    assert (typhon / "main.py").read_text(encoding="utf-8") != \
        (folder / "main.py").read_text(encoding="utf-8"), "only the copy"


def test_main_points_at_its_own_council_files(exported):
    text = (exported.folder / "main.py").read_text(encoding="utf-8")
    assert "parent / '_council'" in text
    assert "for _p in Path(__file__).resolve().parents" not in text
    standalone = "import sys\nfrom app import main\n"
    assert ae.exported_main(standalone) == standalone


def test_it_runs_with_a_data_folder_of_its_own(exported):
    bat = (exported.folder / "run.bat").read_text(encoding="utf-8")
    for var in ("COUNCIL_VAULT_ROOT=%HERE%data\\vault",
                "TYPHON_PLUGIN_HOME=%HERE%data\\plugin_home",
                "FRAME_CLASSES_STORE=%HERE%data\\classifiers"):
        assert var in bat, var
    sh = (exported.folder / "run.sh").read_text(encoding="utf-8")
    assert 'COUNCIL_VAULT_ROOT="$HERE/data/vault"' in sh


def test_requirements_and_record(exported):
    req = (exported.folder / "requirements.txt").read_text(encoding="utf-8")
    lines = [l for l in req.splitlines() if l and not l.startswith("#")]
    assert lines[0] == "PySide6" and "numpy" in lines
    assert "# pypylon" in req, "a camera SDK is optional"
    record = json.loads((exported.folder / "export.json").read_text(
        encoding="utf-8"))
    assert record["format"] == ae.EXPORT_FORMAT and record["app"] == "Typhon"
    assert record["council_commit"] == camera_record.source_commit(ROOT)
    stamp = exported.folder / ae.LIB_DIR / ae.COMMIT_FILE
    assert camera_record.source_commit(stamp.parent) == \
        record["council_commit"], "a run record names the export's commit"


def test_it_starts_with_no_council_present(exported):
    checked = ae.check(exported.folder)
    assert checked.ok, checked.output[-2000:]
    assert "WINDOW built" in checked.output
    assert "CAMERAS 2" in checked.output, "the simulated cameras listed"


def test_typhon_plugins_exports_and_starts(tmp_path, monkeypatch):
    home = tmp_path / "plugin_home"
    home.mkdir()
    monkeypatch.setenv("TYPHON_PLUGIN_HOME", str(home))
    project = Path(rex.build("typhon_plugins", project="tp_x",
                             vault_dir=tmp_path / "vault", target="qt"))
    result = ae.export_app(project, tmp_path / "out")
    lib = result.folder / ae.LIB_DIR
    assert (lib / "council_core/plugin_host.py").is_file()
    assert (lib / "typhon_plugins/trigger/plugin.json").is_file()
    assert (lib / "typhon_plugins/send_trigger/panel.py").is_file()
    checked = ae.check(result.folder)
    assert checked.ok, checked.output[-2000:]


def test_refusals_write_nothing(typhon, tmp_path):
    taken = tmp_path / "Typhon"
    taken.mkdir()
    (taken / "keep.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(ae.ExportError, match="already exists"):
        ae.export_app(typhon, tmp_path, name="Typhon")
    assert [p.name for p in taken.iterdir()] == ["keep.txt"]
    with pytest.raises(ae.ExportError, match="Council's own folder"):
        ae.export_app(typhon, ROOT / "tests")
    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(ae.ExportError, match="not a generated app"):
        ae.export_app(bare, tmp_path / "out")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Typhon", "bare"]


def test_a_generated_app_finds_a_moved_council_again():
    assert (ROOT / gui_emit._ROOT_MARKER).is_file(), \
        "the marker a generated main.py looks for is still in the Council"


# ======================================================================
# The Designer's "Export app…"
# ======================================================================
def test_the_designer_exports_and_checks(typhon, tmp_path):
    from PySide6.QtWidgets import QApplication
    from council_qt.tabs.designer import DesignerActions, DesignerTab
    qapp = QApplication.instance() or QApplication([])
    vault = typhon.parent.parent
    tab = DesignerTab(actions=DesignerActions(vault),
                      confirm=lambda *a, **k: False)
    try:
        tab.ask_folder = lambda *a: str(tmp_path)
        tab.project = "typhon_x"
        tab.on_export_app()
        deadline = time.time() + 240
        while tab._busy and time.time() < deadline:
            qapp.processEvents()
            time.sleep(0.02)
        qapp.processEvents()
        text = tab.log_view.toPlainText()
        assert "Exported typhon_x" in text and "Checked:" in text, text
        assert (tmp_path / "typhon_x" / "run.bat").is_file()
    finally:
        tab.deleteLater()
        qapp.processEvents()
