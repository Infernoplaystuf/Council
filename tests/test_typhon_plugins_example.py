"""
"Typhon (plugins)": the new example, examples/gui/typhon_plugins.gspec
(plugin design 6.1, stage P1 row 4).

At first it is today's typhon.gspec under the new name, so it looks and
behaves exactly like today's Typhon (the window changes only at stage P4).
What is new: its projects keep their classifiers in a store of their own,
pinned to the vault they were built in by classifier_store.json (design 7.1
trap 3, question 10), and it is never built into the Council's own vault.

Everything offscreen, in temporary vaults; apps are constructed and
grabbed, never shown.
"""
from __future__ import annotations

import csv
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

import frame_camera
import frame_classes
import gui_examples as gx
import gui_policy
import gui_projects as gpj
import python_envs as pe
import run_example_gui as rex
from council_core import camera_setup as cs
from council_core import capture
from council_core import designer_examples as dx
from tests.sim_cameras import simulated_index
from PySide6.QtWidgets import QApplication

EXAMPLES = ROOT / "examples" / "gui"
NAME = "typhon_plugins"
TITLE = "Typhon (plugins)"
TYPHON_BG = "#045f80"
GENERATED = ("app", "handlers", "ui", "ui.main_ui", "ui.ports", "ui.widgets")


def gspec(name):
    return json.loads((EXAMPLES / f"{name}.gspec").read_text(encoding="utf-8"))


# ======================================================================
# The wireframe and its registration
# ======================================================================
def test_it_is_typhons_wireframe_under_the_new_name():
    """Only the name changes until P4: the file's project, the window's
    title and the heading across the top (an offscreen render of an early
    Typhon read "Barbie Capture" because only the title had changed)."""
    old, new = gspec("typhon"), gspec(NAME)
    assert new["project"] == NAME and new["window"]["title"] == TITLE
    assert {k: v for k, v in new["window"].items() if k != "title"} == \
        {k: v for k, v in old["window"].items() if k != "title"}
    assert {k: v for k, v in new.items()
            if k not in ("project", "window", "shapes")} == \
        {k: v for k, v in old.items()
         if k not in ("project", "window", "shapes")}
    assert [s["id"] for s in new["shapes"]] == [s["id"] for s in old["shapes"]]
    changed = [(a["id"], a["label"], b["label"])
               for a, b in zip(old["shapes"], new["shapes"]) if a != b]
    assert changed == [("s00", "Typhon", TITLE)]
    a = next(s for s in old["shapes"] if s["id"] == "s00")
    b = next(s for s in new["shapes"] if s["id"] == "s00")
    assert {k: v for k, v in a.items() if k != "label"} == \
        {k: v for k, v in b.items() if k != "label"}


def test_it_is_registered_beside_typhon():
    assert NAME in gx.names() and "typhon" in gx.names()
    assert gx.intended_toolkit(NAME) == "qt"
    assert "--target qt" in gx.NOTES[NAME]
    assert gx.own_classifier_store(NAME)
    assert not gx.own_classifier_store("typhon"), "today's Typhon: as before"
    info = {i.name: i for i in dx.offered()}
    assert info[NAME].toolkit == "qt"
    assert info[NAME].default_project == "example_typhon_plugins"
    assert info[NAME].note == gx.NOTES[NAME]


def test_the_designer_tells_the_two_typhons_apart_by_name(tmp_path):
    for project, example in (("example_typhon_plugins", NAME),
                             ("typhon_plugins_bench", NAME),
                             ("example_typhon", "typhon"),
                             ("my_typhon", "typhon")):
        assert dx.guess_example(tmp_path / project) == example, project


# ======================================================================
# Building it
# ======================================================================
@pytest.fixture(scope="module")
def vault(tmp_path_factory):
    return tmp_path_factory.mktemp("tp_vault")


@pytest.fixture(scope="module")
def pdir(vault):
    return rex.build(NAME, project=NAME, vault_dir=vault, target="qt")


def test_it_builds_and_passes_its_own_gate(pdir):
    pf = pe.preflight(pdir, "", "linked", rex._requires_of(pdir),
                      toolkit=gpj.toolkit_for(pdir))
    assert pf.ok, pf.lines
    ok, errors = gui_policy.validate_dir(pdir, "linked",
                                         rex._requires_of(pdir), toolkit="qt")
    assert ok, errors
    assert gpj.load_manifest(pdir).example == NAME
    assert "frame_camera.attach(self)" in (pdir / "app.py").read_text(
        encoding="utf-8")


def test_the_designer_build_reports_policy_ok(tmp_path):
    built = dx.build(dx.ExampleAnswers(example=NAME, project="tp",
                                       toolkit="qt", python=""), tmp_path)
    assert built.ok, built.lines
    assert "policy: OK" in built.lines


def test_its_classifier_store_is_pinned_to_the_vault_it_was_built_in(
        pdir, vault):
    pin = json.loads((pdir / dx.CLASSIFIER_STORE_FILE).read_text(
        encoding="utf-8"))
    store = Path(pin["store"]).expanduser().resolve()
    assert store == Path(vault).resolve() / "classifiers"
    assert dx.CLASSIFIER_STORE_FILE == frame_classes.STORE_CONFIG


def test_a_double_clicked_app_still_finds_its_own_store(
        pdir, vault, monkeypatch):
    """Trap 3 of the design (7.1): started from the Council, an app inherits
    COUNCIL_VAULT_ROOT; double-clicked, it does not, and without the pin it
    would fall back to ~/.council/vault/classifiers — the user's real
    library. With the pin it does not matter how it was started."""
    monkeypatch.delenv("COUNCIL_VAULT_ROOT", raising=False)
    monkeypatch.delenv(frame_classes.STORE_ENV, raising=False)
    for name in ("app", "handlers"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(sys, "argv", [str(pdir / "main.py")])
    assert frame_classes.classifier_store().resolve() == \
        Path(vault).resolve() / "classifiers"
    # Today's Typhon, double-clicked, shares the default store — as before.
    typhon = rex.build("typhon", project="typhon_today", vault_dir=vault,
                       target="qt")
    assert not (typhon / dx.CLASSIFIER_STORE_FILE).exists()
    monkeypatch.setattr(sys, "argv", [str(typhon / "main.py")])
    assert frame_classes.classifier_store() == \
        Path.home() / ".council" / "vault" / "classifiers"


def test_it_is_never_built_into_the_councils_own_vault(tmp_path, monkeypatch):
    """Built into ~/.council/vault, its "own" store would be the user's real
    library. Refused before anything is written, by the CLI's pipeline and
    by the Designer's checks alike."""
    home = tmp_path / "home"
    real_vault = home / ".council" / "vault"
    real_vault.mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    assert Path.home() == home
    with pytest.raises(dx.ExampleError, match="own vault"):
        dx.build_project(NAME, "tp", real_vault, toolkit="qt")
    assert not (real_vault / gpj.PROJECTS_DIRNAME).exists() or not any(
        (real_vault / gpj.PROJECTS_DIRNAME).iterdir())
    answers = dx.ExampleAnswers(example=NAME, project="tp", toolkit="qt",
                                python="")
    assert "own vault" in dx.problem(answers, real_vault)
    assert dx.problem(answers, tmp_path / "side_vault") == ""
    # Today's Typhon builds there as it always has.
    assert dx.problem(dx.ExampleAnswers(example="typhon", project="t",
                                        toolkit="qt", python=""),
                      real_vault) == ""


def test_the_pin_is_written_once_and_never_over_a_file(tmp_path):
    first = dx.pin_classifier_store(tmp_path, tmp_path / "v")
    text = first.read_text(encoding="utf-8")
    from council_core import camera_record
    with pytest.raises(camera_record.RecordExists):
        dx.pin_classifier_store(tmp_path, tmp_path / "other")
    assert first.read_text(encoding="utf-8") == text


def test_a_vault_outside_the_home_folder_is_pinned_absolute(
        tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    (tmp_path / "a").mkdir()
    inside = dx.pin_classifier_store(tmp_path / "a", home / "tp-vault")
    assert json.loads(inside.read_text(encoding="utf-8"))["store"] == \
        "~/tp-vault/classifiers"
    (tmp_path / "b").mkdir()
    outside = dx.pin_classifier_store(tmp_path / "b", tmp_path / "elsewhere")
    stored = json.loads(outside.read_text(encoding="utf-8"))["store"]
    assert Path(stored) == (tmp_path / "elsewhere" / "classifiers").resolve()


# ======================================================================
# The built app, offscreen: today's Typhon under the new name
# ======================================================================
@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def ui(qapp, pdir):
    capture.warm_imports()
    kept = list(sys.path)
    frame_camera.disconnect()
    sys.path.insert(0, str(pdir))
    for name in GENERATED:
        sys.modules.pop(name, None)
    import app as generated
    window = generated.App()
    window.resize(1504, 1016)
    yield window
    frame_camera.disconnect()
    frame_camera._LIVE.setup_path = None
    frame_camera._LIVE.tabs = None
    frame_camera._LIVE.picker = None
    frame_camera._LIVE.reviewer = None
    frame_camera._LIVE.capture_button = None
    window.deleteLater()
    qapp.processEvents()
    sys.path[:] = kept
    for name in GENERATED:
        sys.modules.pop(name, None)


def pump(seconds=0.2, until=None):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        if until is not None and until():
            return True
        time.sleep(0.01)
    return until() if until is not None else True


def test_the_app_is_typhon_in_teal_under_its_own_title(ui, pdir):
    """The title bar (set by app.py's main) and the heading both say which
    Typhon this is — the two run side by side."""
    app_py = (pdir / "app.py").read_text(encoding="utf-8")
    assert f'ui.setWindowTitle("{TITLE}")' in app_py
    from PySide6.QtWidgets import QLabel
    headings = [w.text() for w in ui.findChildren(QLabel)
                if w.text() in (TITLE, "Typhon")]
    assert headings == [TITLE]
    image = ui.grab().toImage()
    assert image.pixelColor(6, image.height() - 6).name() == TYPHON_BG
    assert getattr(ui, "_frame_camera_live", None) is not None
    assert ui._frame_camera_live.isActive()


def test_the_setup_answer_is_kept_beside_its_app(ui, pdir):
    assert frame_camera._LIVE.setup_path == pdir.resolve() / cs.SETUP_FILE


def test_it_captures_a_run_through_the_rig(ui, tmp_path):
    """Connect, Start and Stop in the built app, on the simulated frame
    camera: the run's frames, frames CSV and records, as today's Typhon
    writes them — through the rig's active slot."""
    p = ui.ports
    ui.on_btn_scan_for_cameras()
    rows = ui.ports.cameras.items()
    ui.ports.cameras.widget.setCurrentRow(simulated_index(rows, "frame"))
    ui.on_btn_connect()
    assert p.capture_status.get().startswith("Connected")
    slot = frame_camera._RIG.active
    assert slot.device is not None and frame_camera._RIG.connected() == [slot]
    p.capture_folder.set(str(tmp_path))
    ui.on_btn_start_capture()
    assert pump(3.0, until=lambda: slot.session.stats().recorded >= 3)
    ui.on_btn_stop_capture()
    pump(2.0, until=lambda: not slot.session.saving)
    frames = sorted(tmp_path.glob("*_frame_*.png"))
    assert len(frames) >= 3
    run = frames[0].name.split("_frame_")[0]
    with open(tmp_path / f"{run}_frames.csv", newline="",
              encoding="utf-8") as fh:
        listed = list(csv.DictReader(fh))
    assert len(listed) == len(frames)
    record = json.loads((tmp_path / f"{run}_camera.json").read_text(
        encoding="utf-8"))
    assert record["format_version"] == 1
    assert (tmp_path / f"{run}_run_end.json").is_file()
