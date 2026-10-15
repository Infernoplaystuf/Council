"""
"Update from example…" — an EXISTING project gets its example's current
layout, keeping its handlers.py and app.py.

The user's case, pinned: their Typhon was built from the "Frame count"
wireframe (tests/data/typhon_frame_count.gspec is examples/gui/typhon.gspec
at 6c7c23b, byte for byte), so it never got the FPS box, the live frame-rate
link or the Settings menu. A project never hears about a newer example, and
the only way to catch up was run_example_gui --force — which deletes app.py
and handlers.py. Measured before this file existed: no function, no button
and no CLI flag could update a project in place.

Every test builds into its own temp vault. Nothing here touches the real one,
opens a window on the desktop, or calls a model.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")

import gui_examples as gx  # noqa: E402
import gui_projects as gpj  # noqa: E402
import gui_shapes as gs  # noqa: E402
from council_core import designer_examples as dx  # noqa: E402

OLD = ROOT / "tests" / "data" / "typhon_frame_count.gspec"
EXAMPLE = ROOT / "examples" / "gui" / "typhon.gspec"

#: A hand edit, in a handler whose link does NOT change between the two
#: wireframes — Generate must leave it exactly as it is, and say nothing.
EDIT = "        # my own note: the EVK4 on this bench needs a second to wake\n"


def build_old(vault: Path, monkeypatch, name: str = "example_typhon") -> Path:
    """Typhon as the user built it: from the "Frame count" wireframe, and
    with no example recorded (projects older than New from example)."""
    examples = vault.parent / f"{name}_examples"
    examples.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(OLD, examples / "typhon.gspec")
    with monkeypatch.context() as m:
        m.setattr(gx, "EXAMPLES_DIR", examples)
        built = dx.build_project("typhon", name, vault, toolkit="qt")
    manifest = gpj.load_manifest(built.project_dir)
    manifest.example = ""
    gpj.save_manifest(built.project_dir, manifest)
    return built.project_dir


def method(source: str, name: str) -> str:
    """One handler's whole definition, as text."""
    found = re.search(rf"\n    def {name}\(.*?(?=\n    def |\n    # ---|\Z)",
                      source, re.S)
    assert found, f"no {name} in handlers.py"
    return found.group(0)


def hand_edit(pdir: Path, handler: str, text: str = EDIT) -> str:
    """Put `text` (EDIT) at the top of `handler`'s body; return its new
    text."""
    path = pdir / "handlers.py"
    source = path.read_text(encoding="utf-8")
    old = method(source, handler)
    # Just after the docstring's closing quotes and their newline.
    doc_end = old.index('"""', old.index('"""') + 3) + 4
    new = old[:doc_end] + text + old[doc_end:]
    path.write_text(source.replace(old, new), encoding="utf-8")
    return new


def digest(pdir: Path) -> dict:
    return {str(p.relative_to(pdir)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(pdir.rglob("*")) if p.is_file()}


# ============================================================
# The fixture is what the user has
# ============================================================

def test_the_fixture_is_the_frame_count_typhon():
    shapes = {s["id"]: s for s in json.loads(
        OLD.read_text(encoding="utf-8"))["shapes"]}
    assert shapes["s07"]["label"] == "Frame count"
    assert shapes["s08"]["port"] == {"name": "frame_count"}
    assert shapes["s08"]["script"] == {}
    assert "frame_rate" not in shapes["s46"]["script"]["inputs"]
    assert "s57" not in shapes and "s58" not in shapes


# ============================================================
# Which example
# ============================================================

def test_the_example_is_read_from_the_manifest_else_guessed(tmp_path,
                                                            monkeypatch):
    vault = tmp_path / "vault"
    pdir = build_old(vault, monkeypatch)
    assert dx.recorded_example(pdir) == ""
    assert dx.guess_example(pdir) == "typhon"
    manifest = gpj.load_manifest(pdir)
    manifest.example = "barbie_capture_v5"
    gpj.save_manifest(pdir, manifest)
    assert dx.recorded_example(pdir) == "barbie_capture_v5"
    assert dx.guess_example(pdir) == "barbie_capture_v5"


@pytest.mark.parametrize("name,want", [
    ("example_typhon", "typhon"), ("my_typhon", "typhon"),
    ("barbie_capture_v5_bench", "barbie_capture_v5"),
    ("example_barbie_capture", "barbie_capture"), ("my_drawing", ""),
])
def test_an_older_projects_example_is_guessed_from_its_name(tmp_path, name,
                                                            want):
    gpj.create(name, vault_dir=tmp_path)
    assert dx.guess_example(gpj.project_path(name, tmp_path)) == want


def test_what_cannot_be_updated_is_refused_with_a_reason(tmp_path,
                                                         monkeypatch):
    vault = tmp_path / "vault"
    pdir = build_old(vault, monkeypatch)
    assert dx.update_problem("example_typhon", "typhon", vault) == ""
    assert "no such example" in dx.update_problem("example_typhon", "nope",
                                                  vault)
    assert dx.update_problem("", "typhon", vault) == "No project open."
    assert "no manifest" in dx.update_problem("absent", "typhon", vault)
    manifest = gpj.load_manifest(pdir)
    manifest.detached = True
    gpj.save_manifest(pdir, manifest)
    before = digest(pdir)
    out = dx.update_from_example("example_typhon", "typhon", vault)
    assert not out.ok and "detached" in out.lines[0]
    assert digest(pdir) == before


# ============================================================
# What changes, said briefly
# ============================================================

def test_the_summary_names_what_changed_between_the_two_typhons():
    old = gs.load_gspec(OLD).shapes
    new = gs.load_gspec(EXAMPLE).shapes
    changes = dx.layout_changes(old, new)
    # Pop out, Settings, then the camera's own set-up: the area line, the
    # presets heading, the preset picker, Save preset, Camera settings;
    # then the classifier library (s64-s78); then the camera settings tabs
    # under the image folder (s79 the notebook, s80 Basic, s81 its note);
    # then the capture set-up in Basic and Capture now (s82-s92).
    assert [a.rsplit(" (", 1)[-1].rsplit(" ", 1)[-1].rstrip(")")
            for a in changes.added] == [f"s{i}" for i in range(57, 93)]
    assert {"Downsample (s83)", "Capture (s85)", "From (s87)",
            "Capture now (F9) (s92)"} <= set(changes.added), changes.added
    assert changes.added[:2] == ["Pop out (s57)", "Settings ▾ (s58)"]
    assert {"Preset (s61)", "Save preset (s62)",
            "Camera settings… (s63)"} <= set(changes.added)
    assert {"Filter (s65)", "Save as (s67)", "Rename (s68)", "Delete (s69)",
            "Export (s71)", "Export this app's classifiers (s72)",
            "Import (s74)"} <= set(changes.added)
    assert changes.removed == []
    rewired = " | ".join(changes.rewired)
    assert "Start capture (s46)" in rewired and "frame_rate)" in rewired
    assert any(line.startswith("spinbox s08: no link → "
                               "frame_camera.apply_frame_rate(frame_rate)")
               for line in changes.rewired)
    # The name box became the model dropdown, opening what is picked.
    assert any(line.startswith("Model (s26): no link → "
                               "frame_classes.open_classifier(")
               for line in changes.rewired)
    assert changes.ports == ["s08: port frame_count → frame_rate"]
    assert "s07: 'Frame count' → 'FPS (0 = camera default)'" in \
        changes.relabelled
    lines = changes.lines()
    assert len(lines) <= 8, "a summary, not a dump"
    assert lines[0].startswith("  shapes: 36 added, 0 removed, 8 rewired")


def test_an_unchanged_layout_says_nothing_changed():
    shapes = gs.load_gspec(EXAMPLE).shapes
    assert not dx.layout_changes(shapes, shapes).any
    assert "nothing changed" in dx.layout_changes(shapes, shapes).lines()[0]


# ============================================================
# The update itself
# ============================================================

def test_the_frame_count_typhon_gets_the_fps_box_and_settings(tmp_path,
                                                              monkeypatch):
    vault = tmp_path / "vault"
    pdir = build_old(vault, monkeypatch)
    edited = hand_edit(pdir, "on_btn_disconnect")
    app_before = (pdir / "app.py").read_bytes()
    old_gspec = (pdir / gpj.GSPEC_NAME).read_text(encoding="utf-8")

    started = time.perf_counter()
    out = dx.update_from_example("example_typhon", "typhon", vault,
                                 stamp="20260930_120000")
    took = time.perf_counter() - started
    print(f"\nupdate + generate: {took:.2f}s")
    assert out.ok, out.lines
    said = "\n".join(out.lines)

    # The drawing is the example's now.
    project = gpj.open_project("example_typhon", vault)
    shapes = {s.id: s for s in project.shapes}
    assert len(shapes) == len(gs.load_gspec(EXAMPLE).shapes)
    assert shapes["s07"].label.startswith("FPS")
    assert shapes["s08"].script["function"] == "apply_frame_rate"
    assert shapes["s58"].script["module"] == "gui_settings"
    assert project.project == "example_typhon"

    # The old one is kept beside it.
    backup = pdir / "project.gspec.20260930_120000.bak"
    assert out.backup == backup
    assert backup.read_text(encoding="utf-8") == old_gspec
    assert "Frame count" in backup.read_text(encoding="utf-8")

    # app.py untouched; handlers.py kept, with the generator's own stubs
    # brought up to date and the hand edit left alone.
    assert (pdir / "app.py").read_bytes() == app_before
    handlers = (pdir / "handlers.py").read_text(encoding="utf-8")
    assert method(handlers, "on_btn_disconnect") == edited
    start = method(handlers, "on_btn_start_capture")
    assert "self.ports.frame_rate.get())" in start
    fps = method(handlers, "on_spn_spinbox_3")
    assert "apply_frame_rate(self.ports.frame_rate.get())" in fps
    assert "self.ports.capture_status.set(result[\"summary\"])" in fps
    assert "settings_menu()" in method(handlers, "on_btn_settings")
    assert "pop_out()" in method(handlers, "on_btn_pop_out")
    # The camera's area no longer lands in the crop box: Connect leaves the
    # box alone, and the area buttons clear it.
    assert "self.ports.roi" not in method(handlers, "on_btn_connect")
    for name in ("on_btn_apply_area_to_camera", "on_btn_full_sensor"):
        assert 'self.ports.roi.set(result["crop"])' in method(handlers, name)
    # The camera's own set-up: the preset picker, Save preset and Camera
    # settings reach their frame_camera functions in the updated project.
    assert "pick_preset(self.ports.preset.get())" in method(handlers,
                                                            "on_cmb_preset")
    assert "save_preset(self.ports.preset.get())" in method(
        handlers, "on_btn_save_preset")
    assert "camera_settings()" in method(handlers, "on_btn_camera_settings")
    ports_py = (pdir / "ui" / "ports.py").read_text(encoding="utf-8")
    assert "preset" in ports_py and "camera_area" in ports_py

    # The manifest remembers, and the port was renamed, not orphaned.
    manifest = gpj.load_manifest(pdir)
    assert manifest.example == "typhon"
    assert manifest.port_names["s08"] == "frame_rate"
    assert "frame_count" not in (pdir / "ui" / "ports.py").read_text(
        encoding="utf-8")

    # What the log says.
    assert "the old one is project.gspec.20260930_120000.bak" in said
    assert "added: Pop out (s57); Settings ▾ (s58)" in said
    assert "rewired on_btn_start_capture" in said
    assert "rewired on_spn_spinbox_3" in said
    assert "port renamed: frame_count -> frame_rate" in said
    assert "policy: OK" in said
    assert "WARNING" not in said, "an unchanged link's edit is not a warning"


def test_an_edited_handler_whose_link_changed_is_kept_and_named(
        tmp_path, monkeypatch):
    """Start gained frame_rate. A Start the user edited is theirs: kept as
    it is, and named with file:line so they know it still calls the old
    way — a warning, not a refusal."""
    vault = tmp_path / "vault"
    pdir = build_old(vault, monkeypatch)
    edited = hand_edit(pdir, "on_btn_start_capture")
    out = dx.update_from_example("example_typhon", "typhon", vault)
    assert out.ok, out.lines
    handlers = (pdir / "handlers.py").read_text(encoding="utf-8")
    assert method(handlers, "on_btn_start_capture") == edited
    warning = next(line for line in out.lines if line.startswith("WARNING"))
    assert re.search(r"handlers\.py:\d+ on_btn_start_capture", warning)


def test_shapes_the_user_drew_in_the_designer_are_kept(tmp_path,
                                                      monkeypatch):
    """A Typhon the user added a button of their own to, with its handler
    written: the update dropped the button with the rest of the old drawing,
    and Generate then BLOCKED on the handler it left behind ("hand-written
    code still uses these: on_btn_snapshot") — measured — leaving the
    project replaced but not generated. A shape drawn in the Designer (a
    uuid id; the examples' are s00...) is the user's, not the example's: it
    stays, and so does the code behind it."""
    from council_core import designer_project as dp

    vault = tmp_path / "vault"
    pdir = build_old(vault, monkeypatch)
    project = gpj.open_project("example_typhon", vault_dir=vault)
    mine = gs.new_shape("button", 600, 960, label="Snapshot")
    project.shapes.append(mine)
    gpj.save_project("example_typhon", project, vault_dir=vault)
    assert dp.generate("example_typhon", project.shapes, pdir, vault).ok
    path = pdir / "handlers.py"
    source = path.read_text(encoding="utf-8")
    stub = method(source, "on_btn_snapshot")
    # Real code, not a comment: a comment-only edit is still the stub.
    edited = stub.replace("        pass\n", "        print('snap')\n")
    assert edited != stub
    path.write_text(source.replace(stub, edited), encoding="utf-8")

    out = dx.update_from_example("example_typhon", "typhon", vault)
    said = "\n".join(out.lines)
    assert out.ok, said
    shapes = {s.id: s for s in gpj.open_project("example_typhon",
                                                vault_dir=vault).shapes}
    assert mine.id in shapes and shapes[mine.id].label == "Snapshot"
    assert len(shapes) == len(gs.load_gspec(EXAMPLE).shapes) + 1
    assert mine.id in {s.id for s in out.shapes}, "the canvas shows it too"
    assert "kept, drawn by you: Snapshot" in said
    assert out.changes.removed == [], "the user's own shape is not removed"
    handlers = (pdir / "handlers.py").read_text(encoding="utf-8")
    assert method(handlers, "on_btn_snapshot") == edited
    assert shapes["s58"].script["module"] == "gui_settings"


def test_a_shape_that_changed_kind_gets_a_widget_named_for_its_kind(
        tmp_path, monkeypatch):
    """MEASURED before the fix (a Typhon built before the classifier
    library, then updated): s26, the model name box, became a dropdown but
    kept the entry's widget name — a QComboBox called self.ent_entry_3, its
    handler on_ent_entry_3 — where a fresh build calls them cmb_model and
    on_cmb_model. Another kind of shape is another widget: it is named
    afresh, as a fresh build names it, and the log says from what to what
    (code that used the old name must use the new one)."""
    vault = tmp_path / "vault"
    pdir = build_old(vault, monkeypatch)
    old = gpj.load_manifest(pdir).widget_names["s26"]
    assert old.startswith("ent_"), old
    fresh = dx.build_project("typhon", "fresh_typhon", vault, toolkit="qt")
    wanted = gpj.load_manifest(fresh.project_dir).widget_names["s26"]
    assert wanted == "cmb_model"
    out = dx.update_from_example("example_typhon", "typhon", vault)
    said = "\n".join(out.lines)
    assert out.ok, said
    assert gpj.load_manifest(pdir).widget_names["s26"] == wanted
    ui = (pdir / "ui" / "main_ui.py").read_text(encoding="utf-8")
    assert f"self.{wanted} = QComboBox" in ui and f"self.{old}" not in ui
    assert f"def on_{wanted}(" in (pdir / "handlers.py").read_text(
        encoding="utf-8")
    assert "new kind: Model (s26): entry → combobox" in said, said
    assert f"{old} → {wanted}" in said, said


def test_a_second_update_keeps_the_first_backup(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    pdir = build_old(vault, monkeypatch)
    first = dx.update_from_example("example_typhon", "typhon", vault,
                                   stamp="20260930_120000")
    second = dx.update_from_example("example_typhon", "typhon", vault,
                                    stamp="20260930_120000")
    assert first.ok and second.ok
    assert first.backup != second.backup
    assert "Frame count" in first.backup.read_text(encoding="utf-8")
    assert "nothing changed" in "\n".join(second.lines)
    assert sorted(p.name for p in pdir.glob("project.gspec.*.bak")) == [
        "project.gspec.20260930_120000.bak",
        "project.gspec.20260930_120000_2.bak"]


def test_the_updated_app_starts_offscreen(tmp_path, monkeypatch):
    """Imported and constructed in a fresh interpreter, as Run would start
    it: the FPS box says FPS, Settings is top right, and its handlers are
    the new ones."""
    vault = tmp_path / "vault"
    pdir = build_old(vault, monkeypatch)
    assert dx.update_from_example("example_typhon", "typhon", vault).ok
    code = (
        "import sys\n"
        f"sys.path[:0] = [{str(pdir)!r}, {str(ROOT)!r}]\n"
        "from PySide6.QtWidgets import QApplication, QLabel, QPushButton\n"
        "app = QApplication([])\n"
        "import app as generated, frame_camera\n"
        "ui = generated.App(); ui.resize(1504, 1016); ui.grab()\n"
        "labels = [l.text() for l in ui.findChildren(QLabel)]\n"
        "b = next(b for b in ui.findChildren(QPushButton)"
        " if b.text().startswith('Settings'))\n"
        "print(any(t.startswith('FPS') for t in labels),"
        " any('Frame count' in t for t in labels),"
        " b.geometry().x() > 1300 and b.geometry().y() < 40,"
        " callable(getattr(ui, 'on_spn_spinbox_3')))\n"
        "print(ui.ports.preset.widget.isEditable(),"
        " type(ui._camera_presets).__name__,"
        " ui.ports.camera_area.get().startswith(\"Camera's area\"))\n"
        "print(type(ui._classifier_picker).__name__,"
        " type(ui.ports.classifier_name.widget).__name__,"
        " ui.ports.classifier_name.get() == '',"
        " ui.ports.classifier_filter.get() == 'All classifiers')\n"
        "frame_camera.disconnect()\n")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", COUNCIL_NO_DIALOGS="1",
               COUNCIL_VAULT_ROOT=str(vault), PYTHONDONTWRITEBYTECODE="1")
    done = subprocess.run([sys.executable, "-c", code], cwd=str(pdir), env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    printed = done.stdout.strip().splitlines()
    assert printed[-3] == "True False True True"
    # The preset picker and the camera's area line arrive with the update,
    # kept current by attach (app.py itself is untouched).
    assert printed[-2] == "True PresetPicker True"
    # So does the model dropdown: the name box is a combobox now, filled by
    # attach, and empty until a model is picked — no default model, which in
    # a shared store may be another app's (typhon.gspec's s26).
    assert printed[-1] == "ClassifierPicker QComboBox True True"


# ============================================================
# The CLI: --update, deleting nothing
# ============================================================

def test_the_cli_updates_in_place(tmp_path, monkeypatch, capsys):
    import run_example_gui as rex
    vault = tmp_path / "vault"
    pdir = build_old(vault, monkeypatch)
    edited = hand_edit(pdir, "on_btn_disconnect")
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
    assert rex.main(["typhon", "--update", "--no-run"]) == 0
    printed = capsys.readouterr().out
    assert "updated example_typhon from example typhon" in printed
    assert "ready" in printed
    handlers = (pdir / "handlers.py").read_text(encoding="utf-8")
    assert method(handlers, "on_btn_disconnect") == edited
    assert list(pdir.glob("project.gspec.*.bak"))


def test_the_cli_update_needs_a_project_and_refuses_force(tmp_path,
                                                          monkeypatch):
    import run_example_gui as rex
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(tmp_path / "vault"))
    with pytest.raises(SystemExit, match="Build it first"):
        rex.main(["typhon", "--update", "--no-run"])
    with pytest.raises(SystemExit, match="use one or the other"):
        rex.main(["typhon", "--update", "--force", "--no-run"])


# ============================================================
# The camera settings tabs (2026-10-07) reach an existing Typhon
# ============================================================
#: examples/gui/typhon.gspec at 794f8b6, byte for byte: the Typhon the user
#: has the day the tabs arrived (presets, the classifier library, no tabs).
BEFORE_TABS = ROOT / "tests" / "data" / "typhon_before_tabs.gspec"


def build_before_tabs(vault: Path, monkeypatch,
                      name: str = "tabs_typhon") -> Path:
    examples = vault.parent / f"{name}_examples"
    examples.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(BEFORE_TABS, examples / "typhon.gspec")
    with monkeypatch.context() as m:
        m.setattr(gx, "EXAMPLES_DIR", examples)
        built = dx.build_project("typhon", name, vault, toolkit="qt")
    return built.project_dir


def test_the_fixture_is_typhon_before_the_tabs():
    shapes = {s["id"]: s for s in json.loads(
        BEFORE_TABS.read_text(encoding="utf-8"))["shapes"]}
    assert "s79" not in shapes and len(shapes) == 79
    assert (shapes["s04"]["x"], shapes["s04"]["y"]) == (24, 200)
    assert shapes["s08"]["script"]["function"] == "apply_frame_rate"


def test_a_typhon_from_before_the_tabs_gets_them_and_keeps_its_code(
        tmp_path, monkeypatch):
    """The boxes beside Start move into the Basic tab with their ports,
    links and widget names: the user's own code in the FPS box's handler
    and in Start is kept as it is, with no warning (neither link changed),
    app.py is untouched — and the updated app has the tabs, filled for the
    camera it connects."""
    vault = tmp_path / "vault"
    pdir = build_before_tabs(vault, monkeypatch)
    names_before = dict(gpj.load_manifest(pdir).widget_names)
    fps = hand_edit(pdir, "on_spn_spinbox_3")
    start = hand_edit(pdir, "on_btn_start_capture")
    app_before = (pdir / "app.py").read_bytes()

    out = dx.update_from_example("tabs_typhon", "typhon", vault,
                                 stamp="20261007_120000")
    assert out.ok, out.lines
    said = "\n".join(out.lines)
    assert "WARNING" not in said, said
    assert "(s79)" in said and "(s80)" in said, "the tabs were not added"
    handlers = (pdir / "handlers.py").read_text(encoding="utf-8")
    assert method(handlers, "on_spn_spinbox_3") == fps
    assert method(handlers, "on_btn_start_capture") == start
    assert (pdir / "app.py").read_bytes() == app_before
    names = gpj.load_manifest(pdir).widget_names
    for sid in ("s03", "s04", "s05", "s06", "s07", "s08"):
        assert names[sid] == names_before[sid], sid
    ports_py = (pdir / "ui" / "ports.py").read_text(encoding="utf-8")
    assert "settings_tabs" in ports_py and "settings_note" in ports_py

    code = (
        "import sys\n"
        f"sys.path[:0] = [{str(pdir)!r}, {str(ROOT)!r}]\n"
        "from PySide6.QtWidgets import QApplication\n"
        "app = QApplication([])\n"
        "import app as generated, frame_camera\n"
        "ui = generated.App(); ui.resize(1400, 820); ui.grab()\n"
        "tabs = ui._settings_tabs\n"
        "page = ui.ports.settings_tabs.widget.widget(0)\n"
        "print(type(tabs).__name__, tabs.titles())\n"
        "print(all(page.isAncestorOf(getattr(ui.ports, p).widget)\n"
        "          for p in ('exposure', 'gain', 'frame_rate')))\n"
        # An EVK4 listed FIRST, as discovery lists a real one plugged in
        # (the fake EVK4 stands in for it, so nothing real is opened):
        # this test means the SIMULATED event camera.
        "import pytest\n"
        "from tests import fake_evk4\n"
        "fake_evk4.install(pytest.MonkeyPatch(), frame_camera)\n"
        "ui.on_btn_scan_for_cameras()\n"
        "rows = ui.ports.cameras.items()\n"
        "ui.ports.cameras.widget.setCurrentRow(\n"
        "    next(i for i, r in enumerate(rows)\n"
        "         if 'Simulated' in r and r.endswith('event')))\n"
        "ui.on_btn_connect()\n"
        "print(type(frame_camera._LIVE.device).__name__)\n"
        "print(tabs.titles())\n"
        # An arrow click to 25: the box's link runs on a step or a
        # committed number (_SpinBox.committed), not on every setValue.
        "box = ui.ports.frame_rate.widget\n"
        "box.setValue(24); box.stepBy(1)\n"
        "print(frame_camera._LIVE.device.accumulate_ms)\n"
        "frame_camera.disconnect()\n")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", COUNCIL_NO_DIALOGS="1",
               COUNCIL_VAULT_ROOT=str(vault), PYTHONDONTWRITEBYTECODE="1")
    done = subprocess.run([sys.executable, "-c", code], cwd=str(pdir), env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    printed = done.stdout.strip().splitlines()
    assert printed[-5] == "SettingsTabs ['Basic', 'Camera', 'Presets']"
    assert printed[-4] == "True", "a box beside Start is not in Basic"
    assert printed[-3] == "SyntheticDevice", \
        "connected to the first event camera listed, not the simulated one"
    assert printed[-2] == ("['Basic', 'Biases', 'Filters', 'Display', "
                           "'Camera', 'Presets']")
    # The FPS box still applies its rate through its (hand-edited) link:
    # 25 pictures a second is a 40 ms window on an event camera.
    assert printed[-1] == "40.0"


# ============================================================
# An update finishes or changes nothing (15 October 2026)
# ============================================================
# MEASURED before (docs/typhon_plugins_design.md, "The honest limits" and
# 6.4): with on_btn_train edited, moving a Typhon to an example without
# the classifier said "BLOCKED — port rename cannot proceed: port
# classifier_name is used in handlers.py" — after project.gspec (with a
# backup) and the manifest's example had already been written, and before
# ui/ was regenerated. The project was left half-switched.

#: Typhon's 32 classifier shapes — the classifier column (s24-s40) and the
#: classifier library (s64-s78) — which the plugin design's new wireframe
#: moves into a pop-out panel. Today's Typhon less these stands in for it.
CLASSIFIER_SHAPES = {f"s{i}" for i in [*range(24, 41), *range(64, 79)]}

#: Real code, not a comment: a comment-only edit is still the generator's
#: stub, and goes with its widget.
CODE = "        print('my own training note')\n"
STAMP = "20261015_120000"


def without_classifier(tmp_path, monkeypatch) -> Path:
    """An examples folder holding today's Typhon, in use for the rest of
    the test. typhon_lite (written by lite()) is the stand-in."""
    examples = tmp_path / "examples"
    examples.mkdir()
    shutil.copyfile(EXAMPLE, examples / "typhon.gspec")
    monkeypatch.setattr(gx, "EXAMPLES_DIR", examples)
    return examples


def lite(examples: Path, name: str = "typhon_lite",
         drop=frozenset(CLASSIFIER_SHAPES)) -> str:
    """Write today's Typhon less the shapes `drop` as example `name` —
    under typhon.gspec itself, that is a plain Update after the example
    dropped widgets. Returns the name."""
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    raw["shapes"] = [s for s in raw["shapes"] if s["id"] not in drop]
    raw["project"] = name
    (examples / f"{name}.gspec").write_text(json.dumps(raw, indent=2),
                                            encoding="utf-8")
    return name


def build_typhon(vault: Path, name: str = "my_typhon") -> Path:
    return dx.build_project("typhon", name, vault, toolkit="qt").project_dir


def outside_backups(pdir: Path) -> dict:
    """digest() of the project's own files: .backups/ is where an update
    keeps its copies, so it is the one place that may gain files."""
    return {k: v for k, v in digest(pdir).items()
            if not Path(k).parts[0] == ".backups"}


@pytest.mark.parametrize("plain", [False, True],
                         ids=["switch-example", "plain-update"])
def test_an_edited_handler_of_a_removed_widget_changes_nothing(
        tmp_path, monkeypatch, plain):
    """Switching to a differently named example, and a plain Update after
    the example dropped widgets: every check runs first, nothing in the
    project is written, and the refusal names the handler — where it is in
    the file, its widget, the ports it uses — and what the user can do."""
    examples = without_classifier(tmp_path, monkeypatch)
    vault = tmp_path / "vault"
    pdir = build_typhon(vault)
    example = lite(examples, "typhon" if plain else "typhon_lite")
    edited = hand_edit(pdir, "on_btn_train", CODE)
    before = digest(pdir)

    out = dx.update_from_example("my_typhon", example, vault, stamp=STAMP)
    said = "\n".join(out.lines)
    assert digest(pdir) == before, "the project changed"
    assert not out.ok and out.blocked and not out.replaced, said
    assert gpj.load_manifest(pdir).example == "typhon"
    assert out.backup is None and out.restore_point is None
    assert said.startswith("not updated — my_typhon was left exactly as it "
                           "was: nothing was written"), said

    # The handler, as it is ON DISK: Generate's own BLOCKED line cites the
    # file as Generate would leave it (here line 370 for a use at 668).
    [blocker] = out.blockers
    assert (blocker.file, blocker.function, blocker.widget) == (
        "handlers.py", "on_btn_train", "Train (s35)")
    lines = (pdir / "handlers.py").read_text(
        encoding="utf-8").splitlines(keepends=True)
    whole = "".join(lines[blocker.first_line - 1:blocker.last_line])
    # Its WHOLE definition, word for word — what stage P4 will set aside.
    assert whole.rstrip("\n") == edited.strip("\n")
    assert CODE in whole
    assert (f"on_btn_train, handlers.py:{blocker.first_line}-"
            f"{blocker.last_line} — you edited it; its widget, Train (s35), "
            f"is not in {example}; it uses self.ports.classifier_name, "
            f"self.ports.classifier_status, which {example} does not have"
            ) in said, said
    assert "What you can do:" in said
    assert "Update again" in said and "New from example" in said

    # What it says to do works: with the code moved out (here: dropped),
    # the update finishes — the handlers nobody edited go with their
    # widgets.
    path = pdir / "handlers.py"
    path.write_text(path.read_text(encoding="utf-8").replace(edited, ""),
                    encoding="utf-8")
    again = dx.update_from_example("my_typhon", example, vault, stamp=STAMP)
    assert again.ok, again.lines
    assert gpj.load_manifest(pdir).example == example
    assert len(gpj.open_project("my_typhon", vault).shapes) == 93 - 32
    assert "def on_btn_add_class(" not in path.read_text(encoding="utf-8")


def test_every_piece_of_code_that_blocks_is_named(tmp_path, monkeypatch):
    """Two edited handlers, a helper of the user's own in handlers.py, a
    line in app.py, and an app.py call to a stub nobody edited: each is
    named, once, and none of it is touched."""
    examples = without_classifier(tmp_path, monkeypatch)
    vault = tmp_path / "vault"
    pdir = build_typhon(vault)
    example = lite(examples)
    hand_edit(pdir, "on_btn_train", CODE)
    hand_edit(pdir, "on_btn_add_class", CODE)
    handlers = pdir / "handlers.py"
    handlers.write_text(
        handlers.read_text(encoding="utf-8").rstrip("\n")
        + "\n\n    def refresh_predictions(self):\n"
          "        return self.ports.predictions.get()\n", encoding="utf-8")
    app = pdir / "app.py"
    source = app.read_text(encoding="utf-8")
    assert "frame_camera.attach(self)\n" in source
    app.write_text(source.replace(
        "frame_camera.attach(self)\n",
        "frame_camera.attach(self)\n        self.ports.classes.get()\n"
        "        self.on_btn_open()\n"), encoding="utf-8")
    call = next(n for n, line in enumerate(
        app.read_text(encoding="utf-8").splitlines(), 1)
        if "self.on_btn_open()" in line)
    before = digest(pdir)

    out = dx.update_from_example("my_typhon", example, vault)
    said = "\n".join(out.lines)
    assert digest(pdir) == before, said
    assert out.blocked, said
    named = [(b.file, b.function) for b in out.blockers]
    assert sorted(named) == sorted([
        ("handlers.py", "on_btn_add_class"), ("handlers.py", "on_btn_open"),
        ("handlers.py", "on_btn_train"),
        ("handlers.py", "refresh_predictions"), ("app.py", "__init__")])
    assert named[-1] == ("app.py", "__init__"), "handlers.py first"
    by_name = {b.function: b for b in out.blockers}
    assert by_name["on_btn_open"].describe(example).endswith(
        f"— a stub you never edited, kept because app.py:{call} calls it: "
        f"take that call out")
    assert "refresh_predictions, handlers.py:" in said
    assert by_name["refresh_predictions"].describe(example).endswith(
        "— your own code; it uses self.ports.predictions, which "
        "typhon_lite does not have")
    assert by_name["__init__"].describe(example).startswith(
        "  __init__, app.py:")
    assert "self.ports.classes" in by_name["__init__"].describe(example)
    assert "Add class (s30)" in by_name["on_btn_add_class"].describe(example)


def test_a_write_that_fails_part_way_puts_every_file_back(tmp_path,
                                                          monkeypatch):
    """Every check passed, then a write failed after ui/ and handlers.py
    were written (a full disk): every file is put back — the user's edited
    handler included — and the copies stay in .backups/update_<stamp>/."""
    import gui_emit

    examples = without_classifier(tmp_path, monkeypatch)
    vault = tmp_path / "vault"
    pdir = build_typhon(vault)
    example = lite(examples)
    edited = hand_edit(pdir, "on_btn_disconnect", CODE)
    before = outside_backups(pdir)
    real = gui_emit.emit
    part_way = []

    def emit_then_fail(*args, **kwargs):
        real(*args, **kwargs)
        part_way.append(outside_backups(pdir) != before)
        raise OSError(28, "No space left on device (test)")

    monkeypatch.setattr(gui_emit, "emit", emit_then_fail)
    out = dx.update_from_example("my_typhon", example, vault, stamp=STAMP)
    said = "\n".join(out.lines)
    assert part_way == [True], "the failure was not part-way"
    assert outside_backups(pdir) == before, "a file was not put back"
    assert method((pdir / "handlers.py").read_text(encoding="utf-8"),
                  "on_btn_disconnect") == edited
    assert gpj.load_manifest(pdir).example == "typhon"
    assert not out.ok and not out.blocked and not out.replaced, said
    assert not list(pdir.glob("project.gspec.*.bak")), \
        "the update's own backup of the drawing is not needed any more"
    assert said.startswith("not updated — writing my_typhon failed part-way:"
                           " OSError: [Errno 28] No space left on device "
                           "(test), so every file it had written was put "
                           "back"), said
    assert f".backups/update_{STAMP}" in said
    index = json.loads((pdir / ".backups" / f"update_{STAMP}"
                        / dx.RESTORE_INDEX).read_text(encoding="utf-8"))
    assert index["state"] == "put back"
    folder = pdir / ".backups" / f"update_{STAMP}"
    assert out.restore_point == folder
    index = json.loads((folder / dx.RESTORE_INDEX).read_text(
        encoding="utf-8"))
    assert {"project.gspec", "manifest.json", "handlers.py",
            "ui/main_ui.py"} <= set(index["saved"])


def test_a_failure_that_cannot_be_put_back_says_how_to(tmp_path,
                                                       monkeypatch):
    """The last write (the manifest) failed, and putting project.gspec and
    manifest.json back failed too: the message names what was not put back,
    the restore point, the old drawing's backup and the example the
    manifest now records — and deletes nothing."""
    examples = without_classifier(tmp_path, monkeypatch)
    vault = tmp_path / "vault"
    pdir = build_typhon(vault)
    example = lite(examples)
    old_gspec = (pdir / gpj.GSPEC_NAME).read_bytes()
    real_save = gpj.save_manifest
    real_copy = getattr(dx, "_copy_over", None)

    def save_then_fail(directory, manifest):
        real_save(directory, manifest)
        raise OSError("the disk went away (test)")

    def held_open(source, target):
        if target.name in (gpj.GSPEC_NAME, gpj.MANIFEST_NAME):
            raise PermissionError(f"{target.name} is held open (test)")
        real_copy(source, target)

    monkeypatch.setattr(gpj, "save_manifest", save_then_fail)
    monkeypatch.setattr(dx, "_copy_over", held_open, raising=False)
    out = dx.update_from_example("my_typhon", example, vault, stamp=STAMP)
    said = "\n".join(out.lines)
    assert "NOT everything could be put back" in said, said
    assert not out.ok and out.replaced, said
    assert "project.gspec: project.gspec is held open (test)" in said
    assert "manifest.json: manifest.json is held open (test)" in said
    assert f"copy each file in .backups/update_{STAMP} back" in said
    assert f"The old drawing is also in project.gspec.{STAMP}.bak" in said
    assert ("manifest.json records the example as typhon_lite (it was "
            "typhon)") in said
    assert (pdir / f"project.gspec.{STAMP}.bak").read_bytes() == old_gspec
    assert (out.restore_point / gpj.GSPEC_NAME).read_bytes() == old_gspec


def test_every_file_an_update_writes_is_in_its_restore_point(tmp_path,
                                                            monkeypatch):
    """A finished switch: each file it changed or made is one the restore
    point copied first or lists as made — so a failure anywhere could have
    been put back. Fails when Generate starts writing a file the update
    does not know of (UPDATE_WRITES)."""
    examples = without_classifier(tmp_path, monkeypatch)
    vault = tmp_path / "vault"
    pdir = build_typhon(vault)
    example = lite(examples)
    before = outside_backups(pdir)
    out = dx.update_from_example("my_typhon", example, vault, stamp=STAMP)
    assert out.ok, out.lines
    after = outside_backups(pdir)
    touched = {k for k in set(before) | set(after)
               if before.get(k) != after.get(k)}
    assert getattr(out, "restore_point", None) is not None, "none taken"
    index = json.loads((out.restore_point / dx.RESTORE_INDEX).read_text(
        encoding="utf-8"))
    covered = {str(Path(p)) for p in index["saved"] + index["absent"]}
    assert touched and touched <= covered, touched - covered
    assert (out.restore_point / "manifest.json").is_file()
    assert json.loads((out.restore_point / "manifest.json").read_text(
        encoding="utf-8"))["example"] == "typhon"


def test_a_toolkit_mismatch_refuses_the_update_before_writing(tmp_path,
                                                              monkeypatch):
    """A Qt project whose manifest says tk: Generate refuses that, and the
    update now asks before writing — it used to write the drawing first."""
    without_classifier(tmp_path, monkeypatch)
    vault = tmp_path / "vault"
    pdir = build_typhon(vault)
    manifest = gpj.load_manifest(pdir)
    manifest.toolkit = "tk"
    gpj.save_manifest(pdir, manifest)
    before = digest(pdir)
    out = dx.update_from_example("my_typhon", "typhon", vault)
    said = "\n".join(out.lines)
    assert digest(pdir) == before, said
    assert out.blocked and not out.blockers, said
    assert "does not generate in it" in said
    assert "app.py is written in qt" in said


def test_an_example_that_does_not_generate_changes_nothing(tmp_path,
                                                           monkeypatch):
    """An example whose links ask for a port it no longer has (no live
    view): Generate's validation refuses it, before anything is written."""
    examples = without_classifier(tmp_path, monkeypatch)
    vault = tmp_path / "vault"
    pdir = build_typhon(vault)
    broken = lite(examples, "typhon_broken", drop={"s10"})
    before = digest(pdir)
    out = dx.update_from_example("my_typhon", broken, vault)
    said = "\n".join(out.lines)
    assert digest(pdir) == before, said
    assert out.blocked, said
    assert "cannot generate:" in said and "live_view" in said


def _untyped_box():
    import uuid
    return gs.Shape(id=uuid.uuid4().hex, kind=gs.GENERIC_KIND, x=600, y=960,
                    w=160, h=40, label="Snap")


def _answers(calls):
    def model(prompt, **_kw):
        calls.append(prompt)
        return json.dumps({"shapes": [
            {"box": 1, "kind": "button", "confidence": 0.9}]})
    return model


def test_a_refused_update_does_not_save_the_classifiers_answers(
        tmp_path, monkeypatch):
    """A box of the user's own, never classified: the check asks the model
    about it (Generate would), but a refused update writes its answer
    nowhere — project.gspec is untouched like everything else."""
    examples = without_classifier(tmp_path, monkeypatch)
    vault = tmp_path / "vault"
    pdir = build_typhon(vault)
    example = lite(examples)
    project = gpj.open_project("my_typhon", vault)
    project.shapes.append(_untyped_box())
    gpj.save_project("my_typhon", project, vault_dir=vault)
    hand_edit(pdir, "on_btn_train", CODE)
    before = digest(pdir)
    calls = []
    out = dx.update_from_example("my_typhon", example, vault,
                                 model_call=_answers(calls))
    assert digest(pdir) == before, out.lines
    assert out.blocked and len(calls) == 1, out.lines


def test_a_finished_update_saves_the_classifiers_answers(tmp_path,
                                                         monkeypatch):
    """...and a finished one writes it into the new drawing, as Generate
    does, so the next Generate asks nothing."""
    examples = without_classifier(tmp_path, monkeypatch)
    vault = tmp_path / "vault"
    build_typhon(vault)
    example = lite(examples)
    project = gpj.open_project("my_typhon", vault)
    box = _untyped_box()
    project.shapes.append(box)
    gpj.save_project("my_typhon", project, vault_dir=vault)
    calls = []
    out = dx.update_from_example("my_typhon", example, vault,
                                 model_call=_answers(calls))
    said = "\n".join(out.lines)
    assert out.ok and len(calls) == 1, said
    # Said where Generate says it, though it is written later now.
    at = out.lines.index("classified 1 untyped shape(s)")
    assert out.lines[at + 1].startswith(
        "saved 1 classification(s) into the wireframe"), said
    saved = {s.id: s for s in gpj.open_project("my_typhon", vault).shapes}
    assert saved[box.id].kind == "button"


def test_the_cli_says_a_refused_update_changed_nothing(tmp_path, monkeypatch,
                                                       capsys):
    import run_example_gui as rex
    examples = without_classifier(tmp_path, monkeypatch)
    vault = tmp_path / "vault"
    pdir = build_typhon(vault)
    example = lite(examples)
    hand_edit(pdir, "on_btn_train", CODE)
    before = digest(pdir)
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
    with pytest.raises(SystemExit, match="nothing in it was changed"):
        rex.main([example, "--update", "--project", "my_typhon", "--no-run"])
    assert "on_btn_train, handlers.py:" in capsys.readouterr().out
    assert digest(pdir) == before


# ============================================================
# What the review of the all-or-nothing update found (15 October 2026)
# ============================================================

#: A use of a port typhon_lite does not have, in a handler of a widget it
#: keeps.
STATUS_USE = "        self.ports.classifier_status.set('x')\n"


def test_every_handler_that_uses_a_removed_port_is_named(tmp_path,
                                                         monkeypatch):
    """Two edited handlers of KEPT widgets use one port the example removes:
    both are named. Measured before: only on_cmb_capture, the first use
    find_orphans met (it reports each port once)."""
    examples = without_classifier(tmp_path, monkeypatch)
    vault = tmp_path / "vault"
    pdir = build_typhon(vault)
    example = lite(examples)
    for handler in ("on_cmb_capture", "on_cmb_downsample"):
        hand_edit(pdir, handler, STATUS_USE)
    before = digest(pdir)
    out = dx.update_from_example("my_typhon", example, vault)
    assert digest(pdir) == before and out.blocked, out.lines
    assert sorted(b.function for b in out.blockers) == [
        "on_cmb_capture", "on_cmb_downsample"], out.lines
    assert all(b.uses == ["self.ports.classifier_status"]
               for b in out.blockers), out.blockers


def test_doing_what_one_refusal_says_is_enough(tmp_path, monkeypatch):
    """One refusal names every piece of code that blocks, with every port
    each one uses, so deleting what it names once lets the update finish.
    Measured before: it named on_btn_train and on_cmb_capture (on_btn_train
    without classifier_status); with those deleted it refused again, naming
    my_status, which had blocked all along."""
    examples = without_classifier(tmp_path, monkeypatch)
    vault = tmp_path / "vault"
    pdir = build_typhon(vault)
    example = lite(examples)
    hand_edit(pdir, "on_btn_train", CODE)
    hand_edit(pdir, "on_cmb_capture", STATUS_USE)
    handlers = pdir / "handlers.py"
    handlers.write_text(
        handlers.read_text(encoding="utf-8").rstrip("\n")
        + "\n\n    def my_status(self, text):\n"
          "        self.ports.classifier_status.set(text)\n", encoding="utf-8")
    out = dx.update_from_example("my_typhon", example, vault)
    assert out.blocked, out.lines
    by_name = {b.function: b for b in out.blockers}
    assert sorted(by_name) == ["my_status", "on_btn_train",
                               "on_cmb_capture"], out.lines
    assert by_name["on_btn_train"].uses == [
        "self.ports.classifier_name", "self.ports.classifier_status"]
    source = handlers.read_text(encoding="utf-8")
    for name in by_name:
        source = source.replace(method(source, name), "")
    handlers.write_text(source, encoding="utf-8")
    again = dx.update_from_example("my_typhon", example, vault)
    assert again.ok, again.lines


def test_the_lines_named_are_the_definition_the_use_is_in(tmp_path,
                                                          monkeypatch):
    """Two definitions with one name, in app.py and in handlers.py: the
    lines named are those of the one the use is in — what stage P4 would
    cut out. Measured before: app.py's Helper.__init__, the first __init__
    in the file, for a use in App.__init__."""
    examples = without_classifier(tmp_path, monkeypatch)
    vault = tmp_path / "vault"
    pdir = build_typhon(vault)
    example = lite(examples)
    app = pdir / "app.py"
    source = app.read_text(encoding="utf-8")
    assert "frame_camera.attach(self)\n" in source and "\nclass App(" in source
    app.write_text(source.replace(
        "frame_camera.attach(self)\n",
        "frame_camera.attach(self)\n        self.ports.classes.get()\n"
    ).replace("\nclass App(", "\nclass Helper:\n    def __init__(self):\n"
                              "        self.x = 1\n\n\nclass App("),
        encoding="utf-8")
    handlers = pdir / "handlers.py"
    source = handlers.read_text(encoding="utf-8")
    assert "\nclass HandlerMixin:" in source
    handlers.write_text(source.replace(
        "\nclass HandlerMixin:",
        "\ndef show(text):\n    return text\n\n\nclass HandlerMixin:"
    ).rstrip("\n") + "\n\n    def show(self):\n"
                     "        return self.ports.predictions.get()\n",
        encoding="utf-8")
    out = dx.update_from_example("my_typhon", example, vault)
    assert out.blocked, out.lines

    def whole(blocker) -> str:
        lines = (pdir / blocker.file).read_text(
            encoding="utf-8").splitlines()
        return "\n".join(lines[blocker.first_line - 1:blocker.last_line])

    [in_app] = [b for b in out.blockers if b.file == "app.py"]
    assert whole(in_app).lstrip().startswith("def __init__(self, parent"), \
        (in_app.where, whole(in_app))
    assert "self.ports.classes.get()" in whole(in_app)
    [show] = [b for b in out.blockers if b.function == "show"]
    assert whole(show).lstrip().startswith("def show(self):"), \
        (show.where, whole(show))
    assert "self.ports.predictions.get()" in whole(show)


def test_code_using_a_widget_that_changed_kind_is_told_its_new_name(
        tmp_path, monkeypatch):
    """A frame-count Typhon whose app.py uses the Model entry: today's
    Typhon has that box as a dropdown, cmb_model. The refusal says so and
    says to rename it. Measured before: "self.ent_entry_3, which typhon does
    not have", and advice to delete the code."""
    examples = tmp_path / "examples"
    examples.mkdir()
    shutil.copyfile(OLD, examples / "typhon.gspec")
    monkeypatch.setattr(gx, "EXAMPLES_DIR", examples)
    vault = tmp_path / "vault"
    pdir = build_typhon(vault)
    app = pdir / "app.py"
    app.write_text(app.read_text(encoding="utf-8").replace(
        "frame_camera.attach(self)\n",
        "frame_camera.attach(self)\n        self.ent_entry_3.setText('m1')\n"),
        encoding="utf-8")
    shutil.copyfile(EXAMPLE, examples / "typhon.gspec")
    before = digest(pdir)
    out = dx.update_from_example("my_typhon", "typhon", vault)
    said = "\n".join(out.lines)
    assert digest(pdir) == before and out.blocked, said
    [blocker] = out.blockers
    assert blocker.file == "app.py"
    assert ("self.ent_entry_3 is now self.cmb_model in typhon (a Combobox): "
            "rename it") in blocker.describe("typhon"), said
    assert "self.ent_entry_3, which typhon does not have" not in said
    assert "rename each self.<name> the list says was renamed" in said


#: A child process that runs Update from example and dies part-way, as a
#: power cut or a closed Council does (the Designer runs the update on a
#: daemon thread). argv: repo root, vault, examples folder, where to die —
#: as it starts writing one of the project's files (the file truncated, as
#: a write that is cut off leaves it), or "after-emit": with ui/ and
#: handlers.py written and the manifest not.
DIE_PART_WAY = r'''
import builtins, io, os, sys
from pathlib import Path
root, vault, examples, point = sys.argv[1:5]
sys.path.insert(0, root)
import gui_emit, gui_examples as gx, gui_projects as gpj
from gui_shapes import ATOMIC_SUFFIX
from council_core import designer_examples as dx
gx.EXAMPLES_DIR = Path(examples)
pdir = gpj.project_path("my_typhon", vault).resolve()
real_open = builtins.open

def dying_open(file, mode="r", *args, **kwargs):
    if any(c in mode for c in "wa+") and isinstance(file, (str, os.PathLike)):
        path = Path(os.fspath(file)).resolve()
        if path.parent == pdir and path.name in (point, point + ATOMIC_SUFFIX):
            real_open(file, mode, *args, **kwargs).close()
            os._exit(77)
    return real_open(file, mode, *args, **kwargs)

if point == "after-emit":
    real_emit = gui_emit.emit
    def emit_then_die(*args, **kwargs):
        real_emit(*args, **kwargs)
        os._exit(77)
    gui_emit.emit = emit_then_die
else:
    builtins.open = io.open = dying_open
dx.update_from_example("my_typhon", "typhon_lite", vault)
sys.exit(0)
'''


def die_part_way(tmp_path, monkeypatch, point: str):
    """A Typhon with an edited handler of a kept widget, and an update to
    typhon_lite killed at `point`. Returns (vault, project dir, example,
    the project's files before, the edited handler)."""
    examples = without_classifier(tmp_path, monkeypatch)
    vault = tmp_path / "vault"
    pdir = build_typhon(vault)
    example = lite(examples)
    edited = hand_edit(pdir, "on_btn_disconnect", CODE)
    before = outside_backups(pdir)
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
               COUNCIL_NO_DIALOGS="1", PYTHONDONTWRITEBYTECODE="1")
    done = subprocess.run(
        [sys.executable, "-c", DIE_PART_WAY, str(ROOT), str(vault),
         str(examples), point],
        env=env, capture_output=True, text=True, timeout=300)
    assert done.returncode == 77, (done.stdout, done.stderr)
    assert outside_backups(pdir) != before, "nothing was half-written"
    return vault, pdir, example, before, edited


def restore_state(pdir: Path) -> str:
    [index] = pdir.glob(f".backups/update_*/{dx.RESTORE_INDEX}")
    return json.loads(index.read_text(encoding="utf-8")).get("state", "")


@pytest.mark.parametrize("point", ["project.gspec", "after-emit",
                                   "handlers.py", "manifest.json"])
def test_an_update_killed_part_way_is_put_back_when_the_project_is_opened(
        tmp_path, monkeypatch, point):
    """MEASURED before: killed after project.gspec was written, the project
    kept the new drawing with the old manifest and ui/ (half-switched);
    killed while writing manifest.json or handlers.py, that file was left
    empty — and the next update then wrote a handlers.py without the
    user's edited handler. Nothing ever put the restore point back.

    Now opening the project puts it back exactly as it was (what the update
    had left is kept beside the restore point), and the update then runs
    as normal, keeping the user's code."""
    from council_core import designer_project as dp

    vault, pdir, example, before, edited = die_part_way(
        tmp_path, monkeypatch, point)
    state = restore_state(pdir)
    opened = dp.open_named("my_typhon", vault)
    assert opened.ok, opened.message
    assert outside_backups(pdir) == before, opened.message
    assert state == "writing"
    assert len(opened.shapes) == 93
    assert opened.message.startswith(
        "an update of my_typhon was cut off while it wrote"), opened.message
    assert "every file is back as it was before it" in opened.message
    assert restore_state(pdir) == "put back"
    [left] = pdir.glob(".backups/update_*/interrupted")
    assert any(p.is_file() for p in left.rglob("*")), "what it left is kept"

    again = dx.update_from_example("my_typhon", example, vault)
    assert again.ok, again.lines
    assert method((pdir / "handlers.py").read_text(encoding="utf-8"),
                  "on_btn_disconnect") == edited


def test_an_update_after_one_killed_part_way_puts_it_back_first(
        tmp_path, monkeypatch):
    vault, pdir, example, before, edited = die_part_way(
        tmp_path, monkeypatch, "after-emit")
    again = dx.update_from_example("my_typhon", example, vault)
    assert again.ok, again.lines
    assert again.lines[0].startswith(
        "an update of my_typhon was cut off while it wrote"), again.lines
    assert method((pdir / "handlers.py").read_text(encoding="utf-8"),
                  "on_btn_disconnect") == edited
    assert gpj.load_manifest(pdir).example == example


def test_generate_after_an_update_killed_part_way_puts_it_back_and_stops(
        tmp_path, monkeypatch):
    """Generate would build ui/ from a drawing the update had half-replaced:
    it puts the project back, writes nothing else, and says to Open it."""
    from council_core import designer_project as dp

    vault, pdir, example, before, edited = die_part_way(
        tmp_path, monkeypatch, "after-emit")
    shapes = gpj.open_project("my_typhon", vault).shapes
    result = dp.generate("my_typhon", shapes, pdir, vault)
    said = "\n".join(result.lines)
    assert not result.ok and result.blocked, said
    assert outside_backups(pdir) == before, said
    assert "cut off while it wrote" in said
    assert "Open my_typhon again" in said


#: A child that takes a restore point of my_typhon — an update writing —
#: and holds it until its stdin closes. argv: repo root, vault.
HOLD_AN_UPDATE = r'''
import sys
root, vault = sys.argv[1:3]
sys.path.insert(0, root)
import gui_projects as gpj
from council_core import designer_examples as dx
point = dx.RestorePoint.take(gpj.project_path("my_typhon", vault), "child")
print("writing", flush=True)
sys.stdin.read()
point.mark(dx.FINISHED)
'''


@pytest.mark.parametrize("where", ["this process", "another process"])
def test_an_update_still_writing_is_left_alone(tmp_path, monkeypatch,
                                               where):
    """Open, Generate and Update while an update of the project is writing
    — the Designer's worker part-way, or run_example_gui in another window:
    its restore point says WRITING, but its writer holds the lock, so
    nothing puts it back under its feet, and Generate and Update refuse."""
    from council_core import designer_project as dp

    examples = without_classifier(tmp_path, monkeypatch)
    vault = tmp_path / "vault"
    pdir = build_typhon(vault)
    example = lite(examples)
    if where == "this process":
        point, child = dx.RestorePoint.take(pdir, STAMP), None
    else:
        point = None
        child = subprocess.Popen(
            [sys.executable, "-c", HOLD_AN_UPDATE, str(ROOT), str(vault)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        assert child.stdout.readline().strip() == "writing"
    try:
        handlers = pdir / "handlers.py"
        handlers.write_text(handlers.read_text(encoding="utf-8")
                            + "\n# written part-way\n", encoding="utf-8")
        during = outside_backups(pdir)
        opened = dp.open_named("my_typhon", vault)
        assert opened.ok, opened.message
        assert "an update of my_typhon is being written right now" \
            in opened.message
        result = dp.generate("my_typhon", opened.shapes, pdir, vault)
        assert result.blocked and not result.ok, result.lines
        out = dx.update_from_example("my_typhon", example, vault)
        assert out.blocked and not out.ok, out.lines
        assert outside_backups(pdir) == during
    finally:
        if point is not None:
            point.mark(dx.FINISHED)
        if child is not None:
            child.communicate("", timeout=60)
    assert "being written" not in dp.open_named("my_typhon", vault).message


def test_a_restore_point_naming_other_files_is_not_trusted(tmp_path,
                                                           monkeypatch):
    """put_back copies over and removes what a restore point's index names,
    so an index naming anything an update does not write — a file outside
    the project here — is ignored whole: nothing is touched."""
    from council_core import designer_project as dp

    without_classifier(tmp_path, monkeypatch)
    vault = tmp_path / "vault"
    pdir = build_typhon(vault)
    canary = tmp_path / "canary.txt"
    canary.write_text("keep me", encoding="utf-8")
    assert (pdir / ".." / ".." / ".." / "canary.txt").resolve() == \
        canary.resolve()
    folder = pdir / ".backups" / "update_20261015_000000"
    folder.mkdir(parents=True)
    shutil.copyfile(pdir / "manifest.json", folder / "manifest.json")
    (pdir / "manifest.json").write_text("{}", encoding="utf-8")
    for absent in ("../../../canary.txt", str(canary)):
        (folder / dx.RESTORE_INDEX).write_text(json.dumps({
            "state": "writing", "saved": ["manifest.json"],
            "absent": [absent]}), encoding="utf-8")
        before = digest(pdir)
        opened = dp.open_named("my_typhon", vault)
        assert opened.ok and "cut off" not in opened.message, opened.message
        assert canary.read_text(encoding="utf-8") == "keep me"
        assert digest(pdir) == before


def test_ctrl_c_while_writing_puts_every_file_back(tmp_path, monkeypatch,
                                                   capsys):
    """Ctrl+C in run_example_gui --update after ui/ and handlers.py were
    written: every file is put back, and the interrupt still stops the
    command. Measured before: KeyboardInterrupt is not an Exception, so
    nothing was put back — the new drawing with a half-regenerated ui/."""
    import gui_emit
    import run_example_gui as rex

    examples = without_classifier(tmp_path, monkeypatch)
    vault = tmp_path / "vault"
    pdir = build_typhon(vault)
    example = lite(examples)
    hand_edit(pdir, "on_btn_disconnect", CODE)
    before = outside_backups(pdir)
    real = gui_emit.emit
    reached = []

    def emit_then_ctrl_c(*args, **kwargs):
        real(*args, **kwargs)
        reached.append(outside_backups(pdir) != before)
        raise KeyboardInterrupt

    monkeypatch.setattr(gui_emit, "emit", emit_then_ctrl_c)
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
    with pytest.raises(KeyboardInterrupt):
        rex.main([example, "--update", "--project", "my_typhon", "--no-run"])
    assert reached == [True]
    assert outside_backups(pdir) == before
    assert restore_state(pdir) == "put back"
    printed = capsys.readouterr().out
    assert "every file it had written was put back" in printed, printed


def test_a_drawing_backup_cut_off_part_way_is_not_left_behind(tmp_path,
                                                              monkeypatch):
    """Copying project.gspec to its dated .bak failed part-way (a full
    disk): the truncated copy is removed with everything else. Measured
    before: a 1000-byte project.gspec.<time>.bak was left — the file the
    confirm and the quick-start say to copy back to undo an update."""
    examples = without_classifier(tmp_path, monkeypatch)
    vault = tmp_path / "vault"
    pdir = build_typhon(vault)
    example = lite(examples)
    before = outside_backups(pdir)
    real_copy2 = shutil.copy2

    def full_disk(source, target, *args, **kwargs):
        if str(target).endswith(".bak"):
            Path(target).write_bytes(Path(source).read_bytes()[:1000])
            raise OSError(28, "No space left on device (test)")
        return real_copy2(source, target, *args, **kwargs)

    monkeypatch.setattr(shutil, "copy2", full_disk)
    out = dx.update_from_example("my_typhon", example, vault, stamp=STAMP)
    assert not out.ok and out.backup is None, out.lines
    assert not list(pdir.glob("project.gspec.*.bak"))
    assert outside_backups(pdir) == before, out.lines


def test_a_failure_put_back_names_the_file_and_marks_the_log_undone(
        tmp_path, monkeypatch):
    """manifest.json held open by another program: the first line names it,
    and what Generate had done is headed as undone. Measured before:
    "generate failed: PermissionError(13, 'Permission denied')", with no
    file, last, after 19 lines of work presented as done."""
    examples = without_classifier(tmp_path, monkeypatch)
    vault = tmp_path / "vault"
    pdir = build_typhon(vault)
    example = lite(examples)
    held = pdir / gpj.MANIFEST_NAME

    def held_open(directory, manifest):
        raise PermissionError(13, "Permission denied", str(held))

    monkeypatch.setattr(gpj, "save_manifest", held_open)
    out = dx.update_from_example("my_typhon", example, vault, stamp=STAMP)
    assert not out.ok, out.lines
    first = out.lines[0]
    assert first.startswith(
        f"not updated — writing my_typhon failed part-way: PermissionError: "
        f"Permission denied: {held}, so every file it had written was put "
        f"back"), first
    undone = "── what Generate had done before that — all of it undone ──"
    assert undone in out.lines and "── Generate ──" not in out.lines
    assert out.lines.index("policy: OK") > out.lines.index(undone)
    assert not any(line.startswith("generate failed") for line in out.lines)


@pytest.mark.parametrize("fname", ["handlers.py", "app.py"])
def test_code_that_does_not_parse_refuses_the_update(tmp_path, monkeypatch,
                                                     fname):
    """The checks cannot see into a file that does not parse, so the update
    refuses, saying where it stops parsing. Measured before: with
    on_btn_train edited and a half-typed def below it, the update went
    ahead and removed the Train button its handler is for."""
    examples = without_classifier(tmp_path, monkeypatch)
    vault = tmp_path / "vault"
    pdir = build_typhon(vault)
    example = lite(examples)
    hand_edit(pdir, "on_btn_train", CODE)
    path = pdir / fname
    path.write_text(path.read_text(encoding="utf-8").rstrip("\n")
                    + "\n\n    def half_typed(self:\n", encoding="utf-8")
    line = len(path.read_text(encoding="utf-8").splitlines())
    before = digest(pdir)
    out = dx.update_from_example("my_typhon", example, vault)
    said = "\n".join(out.lines)
    assert digest(pdir) == before, said
    assert out.blocked and not out.ok, said
    assert said.startswith("not updated — my_typhon was left exactly as it "
                           "was: nothing was written"), said
    assert f"{fname} does not parse (line {line}:" in said, said


def _fail_writing(monkeypatch, target: Path) -> None:
    """The first write of `target` (or of the temporary file it is written
    through) is cut off as a full disk cuts it: the file is truncated, then
    the write fails."""
    import builtins
    import io

    real_open = builtins.open

    def failing_open(file, mode="r", *args, **kwargs):
        if any(c in mode for c in "wa+") and isinstance(
                file, (str, os.PathLike)):
            path = Path(os.fspath(file))
            if path.parent == target.parent and path.name in (
                    target.name, target.name + gs.ATOMIC_SUFFIX):
                real_open(file, mode, *args, **kwargs).close()
                raise OSError(28, "No space left on device (test)")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", failing_open)
    monkeypatch.setattr(io, "open", failing_open)


@pytest.mark.parametrize("what", ["manifest.json", "project.gspec",
                                  "handlers.py"])
def test_a_cut_off_write_leaves_the_file_as_it_was(tmp_path, monkeypatch,
                                                   what):
    """manifest.json, project.gspec and handlers.py are written through a
    temporary file, so a write cut off part-way leaves each one whole — even
    in a plain Generate, which has no restore point. Measured before: each
    was left empty, and handlers.py holds the user's code."""
    from council_core import designer_project as dp

    examples = without_classifier(tmp_path, monkeypatch)
    vault = tmp_path / "vault"
    pdir = build_typhon(vault)
    hand_edit(pdir, "on_btn_disconnect", CODE)
    target = pdir / what
    was = target.read_bytes()
    project = gpj.open_project("my_typhon", vault)
    # The Generate that rewrites handlers.py: a widget whose stub goes.
    shapes = [s for s in project.shapes if s.id != "s35"]
    manifest = gpj.load_manifest(pdir)
    _fail_writing(monkeypatch, target)
    if what == "manifest.json":
        with pytest.raises(OSError):
            gpj.save_manifest(pdir, manifest)
    elif what == "project.gspec":
        with pytest.raises(OSError):
            gpj.save_project("my_typhon", project, vault_dir=vault)
    else:
        result = dp.generate("my_typhon", shapes, pdir, vault)
        assert not result.ok, result.lines
    monkeypatch.undo()
    assert target.read_bytes() == was
    assert not target.with_name(target.name + gs.ATOMIC_SUFFIX).exists()


# ============================================================
# Inside the Council: the Designer's "Update from example…"
# ============================================================
from PySide6.QtWidgets import QApplication  # noqa: E402

from council_qt.tabs.designer import DesignerActions, DesignerTab  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture
def tab(qapp, tmp_path):
    """A tab over its own temp vault; every dialog answers from a script."""
    script = {"choice": [], "confirm": [], "asked": []}

    def ask_choice(title, prompt, choices):
        script["asked"].append(("choice", title, prompt, list(choices)))
        return script["choice"].pop(0) if script["choice"] else None

    def confirm(title, message):
        script["asked"].append(("confirm", title, message))
        return script["confirm"].pop(0) if script["confirm"] else False

    view = DesignerTab(actions=DesignerActions(tmp_path / "vault"),
                       ask_choice=ask_choice, confirm=confirm)
    view.script = script
    yield view
    deadline = time.time() + 10.0
    while any(t.name.startswith("designer-") and t.is_alive()
              for t in threading.enumerate()) and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    view.deleteLater()
    qapp.processEvents()


def pump(qapp, tab, seconds=60.0):
    deadline = time.time() + seconds
    while tab._busy and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    assert not tab._busy, f"still busy after {seconds}s"


def open_old(tab, monkeypatch) -> Path:
    pdir = build_old(tab.actions.vault_dir, monkeypatch)
    tab.script["choice"].append("example_typhon")
    tab.on_open()
    assert tab.project == "example_typhon"
    tab.script["asked"].clear()
    return pdir


def test_update_from_example_in_the_designer(tab, qapp, monkeypatch):
    """The user's path, inside the Council: open the old Typhon, Update
    from example…, pick Typhon (offered first), confirm — and the canvas,
    the project and its handlers are the new ones."""
    pdir = open_old(tab, monkeypatch)
    edited = hand_edit(pdir, "on_btn_disconnect")
    assert len(tab.canvas.scene.shapes) == 57
    tab.script["choice"].append("typhon")
    tab.script["confirm"].append(True)
    started = time.perf_counter()
    tab.on_update_from_example()
    returned = time.perf_counter() - started
    pump(qapp, tab)
    took = time.perf_counter() - started
    print(f"\nDesigner Update from example: click returned in "
          f"{returned * 1000:.0f} ms, done in {took:.2f}s")
    assert returned < 0.5, "the update ran on the GUI thread"

    kind, _title, prompt, choices = tab.script["asked"][0]
    assert kind == "choice" and choices[0] == "typhon"
    assert "does not record which example" in prompt
    kind, _title, message = tab.script["asked"][1]
    assert kind == "confirm"
    assert "handlers.py and app.py are KEPT" in message
    assert "project.gspec.<time>.bak" in message

    assert len(tab.canvas.scene.shapes) == len(gs.load_gspec(EXAMPLE).shapes)
    assert not tab.canvas.scene.dirty
    log = tab.log_view.toPlainText()
    assert "updated example_typhon from example typhon" in log
    assert "rewired on_btn_start_capture" in log
    assert "policy: OK" in log
    handlers = (pdir / "handlers.py").read_text(encoding="utf-8")
    assert method(handlers, "on_btn_disconnect") == edited
    assert "on_btn_settings" in handlers
    # Recorded now: the next update asks nothing about which example.
    tab.script["asked"].clear()
    tab.script["confirm"].append(False)
    tab.on_update_from_example()
    assert [a[0] for a in tab.script["asked"]] == ["confirm"]


def test_saying_no_changes_nothing(tab, qapp, monkeypatch):
    pdir = open_old(tab, monkeypatch)
    before = digest(pdir)
    tab.script["choice"].append("typhon")
    tab.script["confirm"].append(False)
    tab.on_update_from_example()
    assert not tab._busy
    assert digest(pdir) == before
    assert "not updated" in tab.log_view.toPlainText()


def test_cancelling_the_example_choice_changes_nothing(tab, qapp, monkeypatch):
    pdir = open_old(tab, monkeypatch)
    before = digest(pdir)
    tab.on_update_from_example()
    assert digest(pdir) == before
    assert [a[0] for a in tab.script["asked"]] == ["choice"]


def test_unsaved_canvas_edits_are_saved_into_the_backup(tab, qapp,
                                                        monkeypatch):
    from gui_shapes import new_shape
    pdir = open_old(tab, monkeypatch)
    tab.canvas._obey(tab.canvas.scene.add_shapes(
        [new_shape("button", 600, 960, label="Mine")]))
    assert tab.canvas.scene.dirty
    tab.script["choice"].append("typhon")
    tab.script["confirm"].append(True)
    tab.on_update_from_example()
    pump(qapp, tab)
    assert "unsaved changes" in tab.script["asked"][1][2]
    backup = next(pdir.glob("project.gspec.*.bak"))
    assert '"Mine"' in backup.read_text(encoding="utf-8")


def test_a_refused_update_in_the_designer_changes_nothing(tab, qapp,
                                                          monkeypatch):
    """The Designer's Update, on a Typhon whose example has since dropped
    the classifier and whose Train handler the user edited: the confirm
    says a refusal changes nothing, the log names the handler, and the
    canvas and the project's files are as they were."""
    vault = tab.actions.vault_dir
    examples = without_classifier(vault.parent, monkeypatch)
    pdir = build_typhon(vault)
    lite(examples, "typhon")
    hand_edit(pdir, "on_btn_train", CODE)
    tab.script["choice"].append("my_typhon")
    tab.on_open()
    assert tab.project == "my_typhon"
    assert len(tab.canvas.scene.shapes) == 93
    before = digest(pdir)
    tab.script["confirm"].append(True)
    tab.on_update_from_example()
    pump(qapp, tab)
    kind, _title, message = tab.script["asked"][-1]
    assert kind == "confirm"
    assert "nothing is changed and that code is named" in message
    log = tab.log_view.toPlainText()
    assert "my_typhon was left exactly as it was: nothing was written" in log
    assert "on_btn_train, handlers.py:" in log and "Train (s35)" in log
    assert len(tab.canvas.scene.shapes) == 93
    assert not tab.canvas.scene.dirty
    assert digest(pdir) == before


def test_no_project_open_says_so(tab):
    tab.on_update_from_example()
    assert "No project open" in tab.log_view.toPlainText()
    assert tab.script["asked"] == []


def test_the_update_worker_never_touches_a_widget():
    from tests.source_checks import code_of
    source = (ROOT / "council_qt" / "tabs" / "designer.py").read_text(
        encoding="utf-8")
    body = code_of(source, "on_update_from_example")
    assert "_to_ui" in body
    inner = body.split("def work", 1)[1].split("def show", 1)[0]
    for forbidden in ("self.log(", "self.status.setText", "self.canvas"):
        assert forbidden not in inner
