"""
The Designer's project operations — against a real vault, on disk.

Nothing is mocked here. `generate` writes seven files, backs up what it is
about to overwrite, and refuses in four different ways; a suite that stubbed
gui_projects would prove none of that. Each test gets its own temp vault.

The refusals are the point. "BLOCKED — hand-written code still uses these" is
a promise that a regeneration will not break the user's own app.py, and it is
the only thing standing between renaming a button and a traceback in a file
the Designer never wrote.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gui_projects  # noqa: E402
from council_core import designer_project as dp  # noqa: E402
from gui_shapes import new_shape  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def vault(tmp_path):
    return tmp_path / "vault"


def mk(kind="button", label="Go", x=40, y=40):
    shape = new_shape(kind, x, y)
    shape.label = label
    return shape


def project(vault, name="demo", mode="standalone", shapes=()):
    dp.create(name, mode, vault)
    if shapes:
        dp.save(name, list(shapes), vault)
    return gui_projects.project_path(name, vault)


def test_the_module_imports_no_toolkit():
    source = (ROOT / "council_core" / "designer_project.py").read_text(
        encoding="utf-8")
    for toolkit in ("tkinter", "PySide6", "PyQt5", "messagebox", "simpledialog"):
        assert toolkit not in source


# ============================================================
# New, open, save
# ============================================================

def test_a_new_project_exists_on_disk(vault):
    result = dp.create("demo", "standalone", vault)
    assert result.ok
    assert gui_projects.project_path("demo", vault).exists()


def test_a_duplicate_name_is_refused_not_raised(vault):
    """A raised exception here takes down whatever called it. The Tk build
    catches it and shows a box; a front end that forgot to would lose the
    whole tab to a name the user typed twice."""
    dp.create("demo", "standalone", vault)
    second = dp.create("demo", "standalone", vault)
    assert not second.ok
    assert second.message


def test_shapes_survive_a_save_and_reopen(vault):
    project(vault, shapes=[mk(label="Start"), mk("entry", "Path", 200, 40)])
    reopened = dp.open_named("demo", vault)
    assert reopened.ok
    assert [s.label for s in reopened.shapes] == ["Start", "Path"]


def test_opening_a_project_that_is_not_there_is_refused(vault):
    result = dp.open_named("nope", vault)
    assert not result.ok and result.message


def test_saving_with_no_project_open_says_so(vault):
    result = dp.save("", [mk()], vault)
    assert not result.ok
    assert "No project" in result.message


def test_listing_an_empty_vault_is_empty_not_an_error(vault):
    assert dp.list_names(vault) == []


def test_a_wizard_result_lands_at_the_canvas_the_user_drags_on(vault):
    """NOT gui_projects' 1280x800 default. Layout inference measures edge
    anchoring against the canvas size, so a project created with the default
    infers anchors for a canvas nobody ever drew on."""

    class _Result:
        name, mode, title = "wiz", "standalone", "My App"
        min_w, min_h = 400, 300
        shapes = [mk(label="One")]

    result = dp.create_from_wizard(_Result(), vault)
    assert result.ok
    saved = gui_projects.open_project("wiz", vault_dir=vault)
    assert (saved.canvas.w, saved.canvas.h) == (dp.CANVAS_W, dp.CANVAS_H)
    assert saved.window.title == "My App"
    assert [s.label for s in saved.shapes] == ["One"]


def test_a_failed_wizard_apply_is_refused_not_raised(vault):
    class _Result:
        name, mode, title = "", "standalone", "t"
        min_w, min_h, shapes = 1, 1, []

    assert not dp.create_from_wizard(_Result(), vault).ok


# ============================================================
# The window panel's Apply
# ============================================================

def test_the_window_settings_survive_a_save(vault):
    project(vault)
    dp.apply_window("demo", {"title": "Camera", "bg": "#1e1e2e"},
                    [mk()], vault)
    saved = gui_projects.open_project("demo", vault_dir=vault)
    assert saved.window.title == "Camera"
    assert saved.window.bg == "#1e1e2e"


def test_declared_packages_are_parsed_not_stored_as_typed(vault):
    project(vault)
    dp.apply_window("demo", {"requires": "pypylon, numpy"}, [mk()], vault)
    saved = gui_projects.open_project("demo", vault_dir=vault)
    assert list(saved.requires) == ["pypylon", "numpy"]


def test_the_window_apply_also_saves_the_shapes(vault):
    """They are edited on the same canvas. Saving one and not the other loses
    whichever the user touched more recently."""
    project(vault)
    dp.apply_window("demo", {"title": "x"}, [mk(label="Kept")], vault)
    assert [s.label for s in dp.open_named("demo", vault).shapes] == ["Kept"]


def test_an_unknown_window_key_is_ignored_rather_than_set(vault):
    """The panel and the Window class can drift. Setting an attribute the
    class does not declare writes a field nothing reads and nothing saves."""
    project(vault)
    result = dp.apply_window("demo", {"not_a_window_field": 1}, [mk()], vault)
    assert result.ok
    assert not hasattr(gui_projects.open_project("demo", vault_dir=vault).window,
                       "not_a_window_field")


def test_a_bad_package_name_is_reported_but_does_not_block(vault):
    project(vault)
    result = dp.apply_window("demo", {"requires": "os, sys"}, [mk()], vault)
    assert result.ok, "a warning must not stop the settings being saved"


# ============================================================
# Generate
# ============================================================

def test_a_typed_wireframe_generates_with_no_model_call(vault):
    """The Designer works with no model loaded, and this is why. A model_call
    that raises proves nothing reached it."""
    pdir = project(vault, shapes=[mk()])

    def _explode(_prompt):
        raise AssertionError("a model was consulted for a fully typed design")

    result = dp.generate("demo", [mk()], pdir, vault, model_call=_explode)
    assert result.ok, result.lines
    assert any("no model call" in line for line in result.lines)


def test_generate_writes_a_runnable_project(vault):
    pdir = project(vault, shapes=[mk()])
    result = dp.generate("demo", [mk()], pdir, vault)
    assert result.ok, result.lines
    assert (pdir / "ui" / "main_ui.py").exists()
    assert (pdir / "app.py").exists()


def test_generate_reports_what_it_wrote(vault):
    pdir = project(vault, shapes=[mk()])
    result = dp.generate("demo", [mk()], pdir, vault)
    assert any("wrote" in line and "file(s)" in line for line in result.lines)


def test_generate_reports_the_policy_verdict(vault):
    """Run applies the same gate, so this line is a promise rather than a
    warning — and a silent generation would make Run's refusal a surprise."""
    pdir = project(vault, shapes=[mk()])
    result = dp.generate("demo", [mk()], pdir, vault)
    assert any(line.startswith("policy:") or line.startswith("policy REFUSED")
               for line in result.lines)


def test_a_detached_project_refuses_to_regenerate(vault):
    """Detach merges ui/ into the project. Regenerating afterwards would
    overwrite the merged code with a fresh ui/ — one way means one way."""
    pdir = project(vault, shapes=[mk()])
    dp.generate("demo", [mk()], pdir, vault)
    dp.detach(pdir)
    result = dp.generate("demo", [mk()], pdir, vault)
    assert result.blocked
    assert any("detached" in line for line in result.lines)
    assert not result.ok


def test_a_blocked_generation_is_not_an_ok_one(vault):
    """`ok` gates the "it worked" path. A refusal that reported ok would let a
    caller clear the dirty flag on a project that was never written."""
    pdir = project(vault, shapes=[mk()])
    dp.generate("demo", [mk()], pdir, vault)
    dp.detach(pdir)
    assert not dp.generate("demo", [mk()], pdir, vault).ok


def _append_to_app(pdir, code):
    app = pdir / "app.py"
    app.write_text(app.read_text(encoding="utf-8") + code, encoding="utf-8")


def test_renaming_a_port_hand_written_code_uses_blocks_the_regeneration(vault):
    """THE PROMISE. Rename a button, regenerate, and the user's own app.py
    would reach for a port that no longer exists — an AttributeError inside a
    callback, in a file the Designer never wrote."""
    pdir = project(vault, shapes=[mk(label="Start")])
    first = dp.generate("demo", [mk(label="Start")], pdir, vault)
    assert first.ok, first.lines
    _append_to_app(pdir, "\n\ndef _mine(self):\n"
                         "    return self.ports.start\n")
    renamed = dp.generate("demo", [mk(label="Totally Different")], pdir, vault)
    assert renamed.blocked, renamed.lines
    assert any("BLOCKED" in line for line in renamed.lines)
    assert any("app.py" in line for line in renamed.lines), (
        "the refusal must name the file, or the user cannot act on it")


def test_a_widget_hand_written_code_uses_blocks_the_regeneration(vault):
    """The other half of the same promise. find_orphans reports widgets and
    handlers; plan_ports reports ports. Both must block."""
    pdir = project(vault, shapes=[mk(label="Start")])
    first = dp.generate("demo", [mk(label="Start")], pdir, vault)
    assert first.ok, first.lines
    widget = sorted(gui_projects.load_manifest(pdir).widget_names.values())[0]
    _append_to_app(pdir, f"\n\ndef _mine(self):\n"
                         f"    return self.{widget}\n")
    renamed = dp.generate("demo", [mk("entry", label="Nothing Alike")],
                          pdir, vault)
    assert renamed.blocked, renamed.lines
    assert any("BLOCKED" in line for line in renamed.lines)


def test_a_rename_that_orphans_nothing_is_allowed_through(vault):
    """The refusals exist to protect hand-written code. With none, renaming a
    label must just work — a check that blocked everything would be safe and
    useless."""
    pdir = project(vault, shapes=[mk(label="Start")])
    assert dp.generate("demo", [mk(label="Start")], pdir, vault).ok
    again = dp.generate("demo", [mk(label="Totally Different")], pdir, vault)
    assert again.ok, again.lines


def test_a_blocked_generation_writes_nothing(vault):
    """A refusal that had already emitted half the files would leave the
    project in a state neither the user nor the next generation expects."""
    pdir = project(vault, shapes=[mk()])
    dp.generate("demo", [mk()], pdir, vault)
    dp.detach(pdir)
    before = sorted(p.name for p in (pdir / "ui").glob("*.py")) \
        if (pdir / "ui").exists() else []
    dp.generate("demo", [mk("entry"), mk("label")], pdir, vault)
    after = sorted(p.name for p in (pdir / "ui").glob("*.py")) \
        if (pdir / "ui").exists() else []
    assert before == after


def test_a_broken_project_reports_rather_than_raises(vault):
    """The Tk version runs this on a worker thread. An exception there takes
    the thread and leaves the status stuck on "generating…" forever."""
    result = dp.generate("nope", [mk()], vault / "nothing-here", vault)
    assert not result.ok
    assert any("generate failed" in line for line in result.lines)


def test_regenerating_keeps_the_chosen_interpreter(vault):
    """'Run with' may have been changed while the generation ran. Writing the
    manifest back from a copy taken before it started silently reverts the
    user's choice — and a camera app then runs in the wrong environment."""
    pdir = project(vault, shapes=[mk()])
    manifest = gui_projects.load_manifest(pdir)
    manifest.python = "C:/some/other/python.exe"
    gui_projects.save_manifest(pdir, manifest)
    dp.generate("demo", [mk()], pdir, vault)
    assert gui_projects.load_manifest(pdir).python == "C:/some/other/python.exe"


def test_generate_records_the_widget_names_for_next_time(vault):
    """The registry is how the next generation knows a widget is the SAME
    widget. Without it every regeneration looks like a full rename."""
    pdir = project(vault, shapes=[mk()])
    dp.generate("demo", [mk()], pdir, vault)
    assert gui_projects.load_manifest(pdir).widget_names


def test_questions_are_rendered_with_their_options_and_default(vault):
    class _Q:
        question, options, default = "Is this a path?", ["yes", "no"], "yes"

    line = dp.describe_questions([_Q()])[0]
    assert "Is this a path?" in line
    assert "yes | no" in line
    assert "default: yes" in line


# ============================================================
# Review
# ============================================================

def test_there_is_nothing_to_review_before_a_generation(vault):
    """So a caller says "Generate the project first" rather than sending a
    model an empty prompt and charging the user a round trip for it."""
    pdir = project(vault, shapes=[mk()])
    assert dp.review_prompt(pdir) == ""


def test_the_review_prompt_carries_the_generated_source(vault):
    pdir = project(vault, shapes=[mk()])
    dp.generate("demo", [mk()], pdir, vault)
    prompt = dp.review_prompt(pdir)
    assert "main_ui.py" in prompt
    assert prompt.startswith(dp.REVIEW_PROMPT[:40])


def test_the_review_prompt_is_capped(vault):
    """A whole generated project can be far larger than any context window,
    and a prompt that overflows comes back as a refusal the user reads as the
    critique."""
    pdir = project(vault, shapes=[mk(label=f"B{i}", x=20 * i, y=20 * i)
                                  for i in range(1, 30)])
    dp.generate("demo", [mk(label=f"B{i}", x=20 * i, y=20 * i)
                         for i in range(1, 30)], pdir, vault)
    assert len(dp.review_prompt(pdir)) <= (len(dp.REVIEW_PROMPT)
                                           + dp.REVIEW_SOURCE_LIMIT)


def test_the_review_says_it_never_edits_code():
    """It is advisory. A user who thinks it applied its own suggestions would
    stop reading them."""
    source = (ROOT / "council_core" / "designer_project.py").read_text(
        encoding="utf-8")
    assert "Do not rewrite it" in dp.REVIEW_PROMPT
    assert "NEVER edits code" in source or "never edits" in source.lower()


# ============================================================
# The stub that blocked the flagship flow
# ============================================================
# Generate writes handler stubs INTO handlers.py, and handlers.py is then
# hand-written territory. The orphan check saw the generator's own stub, called
# it hand-written code, and refused — so draw a button, Generate, rename the
# button, Generate again was blocked out of the box on every project, citing a
# handler the user had never seen. Fixed in gui_projects._is_empty_stub.

def test_renaming_a_widget_after_a_generation_is_not_blocked_by_its_own_stub(
        vault):
    """THE FLAGSHIP FLOW: draw, Generate, rename, Generate."""
    pdir = project(vault, shapes=[mk(label="Start")])
    assert dp.generate("demo", [mk(label="Start")], pdir, vault).ok
    assert "on_btn_start" in (pdir / "handlers.py").read_text(encoding="utf-8")
    again = dp.generate("demo", [mk(label="Totally Different")], pdir, vault)
    assert again.ok, again.lines


def test_a_handler_the_user_filled_in_still_blocks(vault):
    """THE PROMISE, unchanged. A stub with nothing in it cannot be broken by a
    rename; one with the user's code in it can."""
    pdir = project(vault, shapes=[mk(label="Start")])
    assert dp.generate("demo", [mk(label="Start")], pdir, vault).ok
    handlers = pdir / "handlers.py"
    handlers.write_text(
        handlers.read_text(encoding="utf-8").replace(
            '        """TODO: implement."""\n        pass',
            '        """Mine now."""\n        print("started")'),
        encoding="utf-8")
    again = dp.generate("demo", [mk(label="Totally Different")], pdir, vault)
    assert again.blocked, again.lines
    assert any("on_btn_start" in line for line in again.lines)


def test_an_untouched_stub_is_recognised_whatever_it_does_nothing_with():
    """pass, an ellipsis and a bare return are all "does nothing". A check
    that only knew `pass` would block on the other two."""
    import ast

    from gui_projects import _is_empty_stub
    for body in ('pass', '...', 'return', 'return None',
                 '"""TODO: implement."""\n    pass',
                 '"""doc only."""'):
        node = ast.parse(f"def on_x(self):\n    {body}\n").body[0]
        assert _is_empty_stub(node), body


def test_a_handler_that_does_anything_at_all_is_not_a_stub():
    import ast

    from gui_projects import _is_empty_stub
    for body in ('print(1)', 'return 1', 'self.x = 1', 'if True:\n        pass',
                 'raise NotImplementedError'):
        node = ast.parse(f"def on_x(self):\n    {body}\n").body[0]
        assert not _is_empty_stub(node), body
