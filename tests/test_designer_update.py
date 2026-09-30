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


def hand_edit(pdir: Path, handler: str) -> str:
    """Put EDIT at the top of `handler`'s body; return its new text."""
    path = pdir / "handlers.py"
    source = path.read_text(encoding="utf-8")
    old = method(source, handler)
    # Just after the docstring's closing quotes and their newline.
    doc_end = old.index('"""', old.index('"""') + 3) + 4
    new = old[:doc_end] + EDIT + old[doc_end:]
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
    assert changes.added == ["Pop out (s57)", "Settings ▾ (s58)"]
    assert changes.removed == []
    rewired = " | ".join(changes.rewired)
    assert "Start capture (s46)" in rewired and "frame_rate)" in rewired
    assert any(line.startswith("spinbox s08: no link → "
                               "frame_camera.apply_frame_rate(frame_rate)")
               for line in changes.rewired)
    assert changes.ports == ["s08: port frame_count → frame_rate"]
    assert "s07: 'Frame count' → 'FPS (0 = camera default)'" in \
        changes.relabelled
    lines = changes.lines()
    assert len(lines) <= 8, "a summary, not a dump"
    assert lines[0].startswith("  shapes: 2 added, 0 removed, 2 rewired")


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
    edited = hand_edit(pdir, "on_btn_connect")
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
    assert method(handlers, "on_btn_connect") == edited
    start = method(handlers, "on_btn_start_capture")
    assert "self.ports.frame_rate.get())" in start
    fps = method(handlers, "on_spn_spinbox_3")
    assert "apply_frame_rate(self.ports.frame_rate.get())" in fps
    assert "self.ports.capture_status.set(result[\"summary\"])" in fps
    assert "settings_menu()" in method(handlers, "on_btn_settings")
    assert "pop_out()" in method(handlers, "on_btn_pop_out")

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
        "frame_camera.disconnect()\n")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", COUNCIL_NO_DIALOGS="1",
               COUNCIL_VAULT_ROOT=str(vault), PYTHONDONTWRITEBYTECODE="1")
    done = subprocess.run([sys.executable, "-c", code], cwd=str(pdir), env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == "True False True True"


# ============================================================
# The CLI: --update, deleting nothing
# ============================================================

def test_the_cli_updates_in_place(tmp_path, monkeypatch, capsys):
    import run_example_gui as rex
    vault = tmp_path / "vault"
    pdir = build_old(vault, monkeypatch)
    edited = hand_edit(pdir, "on_btn_connect")
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
    assert rex.main(["typhon", "--update", "--no-run"]) == 0
    printed = capsys.readouterr().out
    assert "updated example_typhon from example typhon" in printed
    assert "ready" in printed
    handlers = (pdir / "handlers.py").read_text(encoding="utf-8")
    assert method(handlers, "on_btn_connect") == edited
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
    edited = hand_edit(pdir, "on_btn_connect")
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

    assert len(tab.canvas.scene.shapes) == 59
    assert not tab.canvas.scene.dirty
    log = tab.log_view.toPlainText()
    assert "updated example_typhon from example typhon" in log
    assert "rewired on_btn_start_capture" in log
    assert "policy: OK" in log
    handlers = (pdir / "handlers.py").read_text(encoding="utf-8")
    assert method(handlers, "on_btn_connect") == edited
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
