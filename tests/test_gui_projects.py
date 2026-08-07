"""
Project-storage tests for gui_projects.

The acceptance criterion the brief names for Phase 2 is here: save a wireframe,
reopen it, and get a byte-identical .gspec. The rest guards the two things that
would quietly destroy work — deleting a project, and regenerating over
hand-written code that still references a widget the new wireframe dropped.

Run:  python -m pytest tests/test_gui_projects.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui_projects as gp   # noqa: E402
import gui_shapes as gs     # noqa: E402


@pytest.fixture()
def vault(tmp_path):
    return tmp_path / "vault"


def test_create_open_list(vault):
    p = gp.create("layer_monitor", "linked", vault_dir=vault)
    assert p.is_dir() and (p / "ui").is_dir()
    assert (p / gp.GSPEC_NAME).exists() and (p / gp.MANIFEST_NAME).exists()

    m = gp.load_manifest(p)
    assert m.mode == "linked" and m.detached is False
    assert gp.list_projects(vault_dir=vault) == ["layer_monitor"]

    proj = gp.open_project("layer_monitor", vault_dir=vault)
    assert proj.project == "layer_monitor"


def test_create_refuses_to_overwrite_and_validates_mode(vault):
    gp.create("a", vault_dir=vault)
    with pytest.raises(gp.ProjectError):
        gp.create("a", vault_dir=vault)
    with pytest.raises(gp.ProjectError):
        gp.create("b", "webassembly", vault_dir=vault)


@pytest.mark.parametrize("bad", [
    "../escape", "..", ".", "", "  ", "a/b", "a\\b", ".hidden", "x" * 80,
])
def test_traversal_and_junk_names_are_rejected(vault, bad):
    """Rejected, not sanitised — quietly turning '../../etc' into 'etc' would
    open a project the user never asked for."""
    with pytest.raises(gp.ProjectError):
        gp.create(bad, vault_dir=vault)


def test_gspec_round_trip_is_byte_identical(vault):
    """Phase 2 acceptance: draw, save, reopen, save -> identical bytes."""
    gp.create("m", vault_dir=vault)
    proj = gp.open_project("m", vault_dir=vault)
    proj.shapes = [gs.new_shape("image_canvas", 0, 40),
                   gs.new_shape("treeview", 700, 40)]
    proj.window.title = "Layer Monitor"
    path = gp.save_project("m", proj, vault_dir=vault)
    first = path.read_bytes()

    reopened = gp.open_project("m", vault_dir=vault)
    assert [s.kind for s in reopened.shapes] == ["image_canvas", "treeview"]
    assert reopened.shapes[0].id == proj.shapes[0].id
    gp.save_project("m", reopened, vault_dir=vault)
    assert path.read_bytes() == first, "a reopened project must re-save identically"


# ---- destruction safety -------------------------------------------------

def test_delete_archives_and_never_destroys(vault):
    p = gp.create("keepme", vault_dir=vault)
    (p / "app.py").write_text("# hand-written work\n", encoding="utf-8")

    dest = gp.delete("keepme", vault_dir=vault)

    assert not p.exists(), "the project is gone from the active list"
    assert gp.list_projects(vault_dir=vault) == []
    # ...but every byte still exists, one rename from recovery.
    assert dest.is_dir() and dest.parent.name == gp.TRASH_DIRNAME
    assert (dest / "app.py").read_text(encoding="utf-8") == "# hand-written work\n"


def test_delete_of_a_missing_project_is_an_error_not_a_no_op(vault):
    gp.create("real", vault_dir=vault)
    with pytest.raises(gp.ProjectError):
        gp.delete("imaginary", vault_dir=vault)


def test_backup_captures_ui_and_handwritten_files(vault):
    p = gp.create("b", vault_dir=vault)
    (p / "ui" / "main_ui.py").write_text("class MainUi: pass\n", encoding="utf-8")
    (p / "app.py").write_text("class App: pass\n", encoding="utf-8")

    dest = gp.backup(p)
    assert (dest / "ui" / "main_ui.py").exists()
    assert (dest / "app.py").exists()
    assert (dest / gp.GSPEC_NAME).exists()


# ---- checksums ----------------------------------------------------------

def test_hand_edits_to_generated_ui_are_detected(vault):
    """ui/ is overwritten on every regeneration, so an edit there is about to
    be lost. It has to be surfaced BEFORE anything is written."""
    p = gp.create("c", vault_dir=vault)
    f = p / "ui" / "main_ui.py"
    f.write_text("generated\n", encoding="utf-8")

    m = gp.load_manifest(p)
    m.ui_checksums = gp.ui_checksums(p)
    gp.save_manifest(p, m)
    assert gp.hand_edited_ui_files(p) == [], "freshly recorded == clean"

    f.write_text("generated\n# my edit\n", encoding="utf-8")
    assert gp.hand_edited_ui_files(p) == ["main_ui.py"]

    f.unlink()
    assert gp.hand_edited_ui_files(p) == ["main_ui.py"], "a deleted file counts too"


def test_checksum_keys_are_posix_on_every_platform(vault):
    p = gp.create("d", vault_dir=vault)
    (p / "ui" / "panels").mkdir(parents=True)
    (p / "ui" / "panels" / "left_ui.py").write_text("x\n", encoding="utf-8")
    keys = list(gp.ui_checksums(p))
    assert keys == ["panels/left_ui.py"], (
        "os.sep keys would mark every file changed after moving machines")


# ---- orphan detection (spec 7.3) ---------------------------------------

APP_PY = """\
class App(MainUi):
    def on_btn_start(self):
        self.btn_start.configure(state="disabled")
        self.lbl_status.configure(text="running")
        self._internal_counter += 1

    def on_btn_removed(self):
        self.btn_removed.configure(state="normal")
"""


def test_find_orphans_blocks_a_destructive_regeneration(vault):
    p = gp.create("e", vault_dir=vault)
    (p / "app.py").write_text(APP_PY, encoding="utf-8")

    # The new wireframe still has btn_start and lbl_status, but btn_removed is
    # gone — regenerating would leave app.py referencing a dead attribute.
    orphans = gp.find_orphans(p, {"btn_start", "lbl_status"})
    names = {o.name for o in orphans}

    assert "btn_removed" in names, "a dropped widget that code still uses"
    assert "on_btn_removed" in names, "and its now-dangling handler"
    assert "btn_start" not in names and "lbl_status" not in names
    assert "_internal_counter" not in names, (
        "ordinary instance attributes are not widgets and must not be reported")
    assert all(o.referenced_in == "app.py" and o.line > 0 for o in orphans)
    assert "btn_removed" in orphans[0].describe()


def test_find_orphans_is_clean_when_nothing_was_dropped(vault):
    p = gp.create("f", vault_dir=vault)
    (p / "app.py").write_text(APP_PY, encoding="utf-8")
    assert gp.find_orphans(p, {"btn_start", "lbl_status", "btn_removed"}) == []


def test_unparseable_handwritten_code_does_not_block_regeneration(vault):
    """Broken hand-written code is the user's to fix, but it must not crash the
    check or falsely report every widget as orphaned."""
    p = gp.create("g", vault_dir=vault)
    (p / "app.py").write_text("class App(  # unclosed\n", encoding="utf-8")
    assert gp.find_orphans(p, {"btn_start"}) == []


def test_orphan_check_ignores_names_in_strings_and_comments(vault):
    p = gp.create("h", vault_dir=vault)
    (p / "app.py").write_text(
        'class App(MainUi):\n'
        '    def go(self):\n'
        '        # self.btn_ghost is only mentioned here\n'
        '        msg = "self.btn_phantom"\n'
        '        self.btn_real.invoke()\n', encoding="utf-8")
    names = {o.name for o in gp.find_orphans(p, set())}
    assert names == {"btn_real"}, (
        f"a regex would have flagged the comment and the string too: {names}")


# ---- detach (spec 7.5) --------------------------------------------------

def test_detach_is_one_way_and_destroys_nothing(vault):
    p = gp.create("i", vault_dir=vault)
    (p / "ui" / "main_ui.py").write_text("class MainUi: pass\n", encoding="utf-8")

    merged = gp.detach(p)
    assert merged.exists() and "class MainUi" in merged.read_text(encoding="utf-8")
    assert gp.load_manifest(p).detached is True
    # Nothing removed: ui/ survives, and a backup was taken first.
    assert (p / "ui" / "main_ui.py").exists()
    assert any((p / gp.BACKUPS_DIRNAME).iterdir())

    with pytest.raises(gp.ProjectError):
        gp.detach(p)


def test_manifest_round_trips(vault):
    p = gp.create("j", vault_dir=vault)
    m = gp.load_manifest(p)
    m.widget_names = {"shape1": "btn_go"}
    gp.save_manifest(p, m)
    again = gp.load_manifest(p)
    assert again.widget_names == {"shape1": "btn_go"}
    assert json.loads((p / gp.MANIFEST_NAME).read_text(encoding="utf-8"))


def test_projects_module_is_pure():
    import ast
    src = (Path(__file__).resolve().parent.parent / "gui_projects.py").read_text(
        encoding="utf-8")
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    banned = {m for m in mods
              if m in {"tkinter", "council_engine"} or m.startswith("vault_")}
    assert not banned, f"gui_projects must stay pure; imports {banned}"
