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


# ============================================================
# The toolkit, chosen at creation
# ============================================================

def test_a_new_project_records_the_toolkit_it_was_made_for(vault):
    assert dp.create("q", "standalone", vault, "qt").ok
    pdir = gui_projects.project_path("q", vault)
    assert gui_projects.load_manifest(pdir).toolkit == "qt"
    assert dp.toolkit_label(pdir) == "Qt"


def test_a_new_project_defaults_to_tk(vault):
    """Every caller that predates the choice keeps making what it made."""
    pdir = project(vault)
    assert gui_projects.load_manifest(pdir).toolkit == "tk"
    assert dp.toolkit_label(pdir) == "Tk"


def test_an_unknown_toolkit_is_refused_and_leaves_nothing(vault):
    result = dp.create("q", "standalone", vault, "pyside6")
    assert not result.ok and "toolkit" in result.message
    assert not gui_projects.project_path("q", vault).exists()


def test_a_new_project_is_drawn_on_the_canvas_the_user_drags_on(vault):
    """gui_projects' default is 1280x800. Layout inference measures edge
    anchoring against the canvas, so New used to infer "anchored right" for a
    shape 180 px short of the edge the user could see."""
    project(vault)
    saved = gui_projects.open_project("demo", vault_dir=vault)
    assert (saved.canvas.w, saved.canvas.h) == (dp.CANVAS_W, dp.CANVAS_H)


def test_a_wizard_result_carries_its_toolkit(vault):
    class _Result:
        name, mode, title, toolkit = "wiz", "standalone", "t", "qt"
        min_w, min_h = 400, 300
        shapes = [mk(label="One")]

    assert dp.create_from_wizard(_Result(), vault).ok
    pdir = gui_projects.project_path("wiz", vault)
    assert gui_projects.load_manifest(pdir).toolkit == "qt"


def test_a_wizard_apply_that_fails_half_way_can_be_retried(vault):
    """The directory is made first. A failure after that used to strand it,
    and the obvious retry — same answers — said "already exists"."""

    class _Broken:
        name, mode, title = "wiz", "standalone", "t"
        min_w, min_h = 400, 300
        shapes = 5                                  # list(5) raises

    assert not dp.create_from_wizard(_Broken(), vault).ok
    assert not gui_projects.project_path("wiz", vault).exists()

    class _Fixed(_Broken):
        shapes = [mk()]

    assert dp.create_from_wizard(_Fixed(), vault).ok


def test_the_cleanup_leaves_a_directory_it_did_not_make_alone(vault):
    """Only what create() wrote is ever removed. A directory with anything
    else in it is somebody's work."""
    project(vault)
    pdir = gui_projects.project_path("demo", vault)
    (pdir / "notes.txt").write_text("mine", encoding="utf-8")
    dp._discard_new("demo", vault)
    assert (pdir / "notes.txt").exists()


def test_a_qt_project_passes_the_policy_gate_at_generate(vault):
    """Checked as Tk, every correct Qt project said "policy REFUSED —
    'PySide6' is not on the linked allowlist" here, while Run — which passes
    the toolkit — launched it without complaint."""
    dp.create("q", "standalone", vault, "qt")
    pdir = gui_projects.project_path("q", vault)
    result = dp.generate("q", [mk()], pdir, vault)
    assert result.ok, result.lines
    assert gui_projects.toolkit_of(pdir) == "qt"
    assert "policy: OK" in result.lines, result.lines
    assert not any("REFUSED" in line for line in result.lines)


def test_a_qt_review_is_not_told_it_is_reading_tkinter(vault):
    dp.create("q", "standalone", vault, "qt")
    pdir = gui_projects.project_path("q", vault)
    dp.generate("q", [mk()], pdir, vault)
    prompt = dp.review_prompt(pdir)
    assert prompt.startswith(dp.REVIEW_PROMPT_QT)
    assert "Tkinter" not in prompt.split("\n", 1)[0]
    assert "Do not rewrite it" in dp.REVIEW_PROMPT_QT


def test_the_two_review_headers_share_a_cap():
    """The cap test measures against REVIEW_PROMPT; the Qt header must not be
    the one that overflows it."""
    assert len(dp.REVIEW_PROMPT_QT) <= len(dp.REVIEW_PROMPT) + 16


# ============================================================
# The Council review backend
# ============================================================

class _Models:
    judge = object()
    coder = object()
    writer = object()


def test_review_asks_the_coder_and_the_writer_for_one_round(monkeypatch):
    from council_core import deliberation
    from council_core.deliberation import AgentEvent
    seen = {}

    class _Orch:
        def __init__(self, judge_model, agents, max_rounds, debate_turns):
            seen.update(agents=sorted(agents), rounds=max_rounds,
                        turns=debate_turns)

        def run(self, prompt, panel, synth):
            seen.update(prompt=prompt, panel=panel, synth=synth)
            return [AgentEvent(who="Writer", kind="final",
                               text="pad the buttons")]

    monkeypatch.setattr(deliberation, "DeliberationOrchestrator", _Orch)
    assert dp.review("look at this", _Models()) == "pad the buttons"
    assert seen["agents"] == ["coder", "writer"]
    assert (seen["rounds"], seen["turns"], seen["synth"]) == (1, 1, "writer")


def test_review_with_no_judge_says_so_rather_than_raising():
    class _NoJudge(_Models):
        judge = None

    assert "no judge" in dp.review("x", _NoJudge())


def test_review_with_no_writer_says_so_rather_than_raising():
    class _NoWriter(_Models):
        writer = None

    assert "no writer" in dp.review("x", _NoWriter())


# ============================================================
# Describe it
# ============================================================

def test_describe_designs_for_the_projects_toolkit_and_the_real_canvas(
        vault, monkeypatch):
    import gui_describe
    seen = {}
    monkeypatch.setattr(gui_describe, "describe",
                        lambda text, **kw: seen.update(text=text, **kw) or "r")
    dp.create("q", "standalone", vault, "qt")
    out = dp.describe("a login form", gui_projects.project_path("q", vault),
                      model_call=lambda p: "{}")
    assert out == "r"
    assert seen["toolkit"] == "qt"
    assert (seen["canvas_w"], seen["canvas_h"]) == (dp.CANVAS_W, dp.CANVAS_H)


def test_describe_never_reaches_a_model_it_was_not_given(vault, monkeypatch):
    """The default call is the engine's; a test must be able to replace it."""
    import gui_describe
    calls = []
    monkeypatch.setattr(gui_describe, "describe",
                        lambda text, **kw: calls.append(kw["model_call"]))
    stub = lambda p: "{}"                               # noqa: E731
    dp.describe("x", None, model_call=stub)
    assert calls == [stub]


def test_the_describe_call_is_budgeted_for_a_whole_wireframe():
    """700 tokens — the classifier's — cuts a 20-shape reply off mid-list."""
    assert dp.DESCRIBE_NUM_PREDICT >= 1500
    assert dp.DESCRIBE_ROLE == "coder"
    import gui_describe
    assert dp.DESCRIBE_NUM_PREDICT == gui_describe.REPLY_TOKENS


# ============================================================
# Small local models: the engine call, the profile, the classifier
# ============================================================

class _OldEngine:
    """An engine from before the json_schema contract."""

    def __init__(self):
        self.calls = []

    def local_chat(self, messages, *, temperature=0.2, num_predict=600,
                   model=None, host=None, timeout=120, role=None):
        self.calls.append(dict(temperature=temperature,
                               num_predict=num_predict, timeout=timeout,
                               role=role))
        return "old"


class _NewEngine(_OldEngine):
    def local_chat(self, messages, *, temperature=0.2, num_predict=600,
                   model=None, host=None, timeout=120, role=None,
                   json_schema=None, seed=None, stop=None, should_stop=None):
        self.calls.append(dict(temperature=temperature, role=role,
                               num_predict=num_predict, seed=seed,
                               json_schema=json_schema))
        return "new"


class _HiddenEngine(_OldEngine):
    """Its signature says **kwargs, but the real callee is the old one."""

    def local_chat(self, *args, **kwargs):
        return _OldEngine.local_chat(self, *args, **kwargs)


def test_contract_keywords_are_dropped_for_an_engine_without_them():
    engine = _OldEngine()
    assert dp.local_chat(engine, messages=[], role="coder", json_schema={},
                         seed=3) == "old"
    assert len(engine.calls) == 1, "no wasted generation on a TypeError"


def test_contract_keywords_reach_an_engine_that_takes_them():
    engine = _NewEngine()
    assert dp.local_chat(engine, messages=[], json_schema={"a": 1},
                         seed=7) == "new"
    assert engine.calls[0]["json_schema"] == {"a": 1}
    assert engine.calls[0]["seed"] == 7


def test_a_hidden_signature_is_retried_once_without_the_keywords():
    engine = _HiddenEngine()
    assert dp.local_chat(engine, messages=[], json_schema={}) == "old"
    assert len(engine.calls) == 1


def test_the_describe_and_classify_calls_ask_the_coder_role(monkeypatch):
    engine = _NewEngine()
    monkeypatch.setitem(sys.modules, "council_engine", engine)
    dp.describe_model_call("p", json_schema={"s": 1}, seed=2,
                           temperature=0.5, num_predict=900)
    dp.default_model_call("p", json_schema={"c": 1}, num_predict=2000)
    d, c = engine.calls
    assert (d["role"], d["seed"], d["temperature"], d["num_predict"]) == (
        "coder", 2, 0.5, 900)
    assert (c["role"], c["num_predict"]) == ("coder", 2000)
    assert c["json_schema"] == {"c": 1}


class _FakeSlots:
    def __init__(self, path):
        from council_core.model_slots import Slot, SlotConfig
        self.cfg = SlotConfig({"main": Slot("main"),
                               "small": Slot("small", path)},
                              {"coder": "small"})

    def current(self):
        return self.cfg


def _fake_engine(models=(), n_ctx=8192):
    import types
    eng = types.ModuleType("council_engine")
    eng.list_local_models = lambda: list(models)
    eng.effective_n_ctx = lambda slot="main": n_ctx
    eng.read_gguf_metadata = lambda path: {}
    return eng


def test_the_role_model_is_read_from_the_slot_and_the_engine(monkeypatch):
    from council_core import model_slots
    monkeypatch.setattr(model_slots, "current",
                        _FakeSlots("ollama:phi3.5").current)
    monkeypatch.setitem(sys.modules, "council_engine", _fake_engine(
        [{"id": "ollama:phi3.5", "name": "phi3.5", "params_b": 3.8}]))
    info = dp.role_model("coder")
    assert info["slot"] == "small" and info["params_b"] == 3.8
    assert info["n_ctx"] == 8192 and info["name"] == "phi3.5"
    prof = dp.describe_profile("a login form")
    assert (prof.mode, prof.n_best, prof.n_ctx) == ("tree", 3, 8192)
    assert "phi3.5" in prof.reason


def test_without_list_local_models_the_size_comes_from_the_name(monkeypatch):
    from council_core import model_slots
    monkeypatch.setattr(model_slots, "current",
                        _FakeSlots("ollama:llama3.1:8b").current)
    eng = _fake_engine()
    del eng.list_local_models
    monkeypatch.setitem(sys.modules, "council_engine", eng)
    assert dp.role_model("coder")["params_b"] == 8.0


def test_the_profile_can_be_forced_for_measuring(monkeypatch):
    from council_core import model_slots
    monkeypatch.setattr(model_slots, "current",
                        _FakeSlots("ollama:phi3.5").current)
    monkeypatch.setitem(sys.modules, "council_engine", _fake_engine())
    monkeypatch.setenv(dp.ENV_MODE, "pixel")
    monkeypatch.setenv(dp.ENV_BEST_OF, "1")
    monkeypatch.setenv(dp.ENV_CONSTRAINED, "0")
    prof = dp.describe_profile("x")
    assert (prof.mode, prof.n_best, prof.constrained) == ("pixel", 1, False)


def test_the_engine_describe_path_detects_the_profile_and_reports_stats(
        vault, monkeypatch):
    """No model_call given: the profile comes from the role's model, the
    schema reaches the engine, and the engine's own per-call stats (shared
    contract) become one line of the result's notes."""
    import json
    from council_core import model_slots
    monkeypatch.setattr(model_slots, "current",
                        _FakeSlots("ollama:phi3.5").current)
    eng = _fake_engine([{"id": "ollama:phi3.5", "params_b": 3.8}], 4096)
    seen = []
    tree = {"window": {"title": "Login"}, "layout": {
        "kind": "column", "children": [
            {"kind": "label", "label": "User"},
            {"kind": "entry", "label": "User box"},
            {"kind": "button", "label": "Sign in"}]}}

    def local_chat(messages, *, temperature=0.2, num_predict=600, model=None,
                   host=None, timeout=120, role=None, json_schema=None,
                   seed=None, stop=None, should_stop=None):
        seen.append(dict(schema=json_schema, seed=seed, role=role,
                         num_predict=num_predict))
        return json.dumps(tree)
    eng.local_chat = local_chat
    eng.last_call_stats = lambda role=None: {
        "backend": "ollama", "model": "phi3.5", "prompt_tokens": 2000,
        "gen_tokens": 120, "seconds": 3.0, "constrained": True}
    monkeypatch.setitem(sys.modules, "council_engine", eng)
    pdir = project(vault, name="q")
    result = dp.describe("a login form", pdir)
    assert result.ok, result.errors
    assert result.mode == "tree" and len(seen) == 1
    assert seen[0]["role"] == "coder" and seen[0]["seed"] == 1
    assert "layout" in seen[0]["schema"]["properties"]
    assert any(n.startswith("design: phi3.5 (3.8B): tree mode")
               for n in result.notes)
    assert any("1 of 1 schema-constrained" in n and "40.0 tok/s" in n
               for n in result.notes)


def _generic(label, x, y, w=200, h=60):
    import uuid
    from gui_shapes import GENERIC_KIND, Shape
    return Shape(id=uuid.uuid4().hex, kind=GENERIC_KIND, x=x, y=y, w=w,
                 h=h, label=label)


def test_generate_writes_the_classifiers_answers_back_and_asks_once(vault):
    """gui_classify promised a box is asked about once; nothing wrote the
    answer down, so every Generate called the model again."""
    import json
    boxes = [_generic("Name", 40, 40), _generic("Save", 40, 140)]
    pdir = project(vault, shapes=boxes)
    calls = []

    def model(prompt, *, json_schema=None, num_predict=None, **_kw):
        calls.append((json_schema, num_predict))
        return json.dumps({"shapes": [
            {"box": 1, "kind": "entry", "confidence": 0.9},
            {"box": 2, "kind": "button", "confidence": 0.5}]})

    first = dp.generate("demo", boxes, pdir, vault, model_call=model)
    assert first.ok, first.lines
    assert len(calls) == 1 and calls[0][0]["properties"]["shapes"]
    assert {v["kind"] for v in first.classified.values()} == {"entry",
                                                              "button"}
    saved = dp.open_named("demo", vault)
    assert sorted(s.kind for s in saved.shapes) == ["button", "entry"]
    entry = next(s for s in saved.shapes if s.kind == "entry")
    assert entry.props["justify"] == "left", "catalogue defaults filled in"
    project_file = gui_projects.open_project("demo", vault_dir=vault)
    assert [c.shape_id for c in project_file.clarifications] == [
        next(s.id for s in saved.shapes if s.kind == "button")], \
        "the unsure answer is recorded as a clarification"
    second = dp.generate("demo", saved.shapes, pdir, vault, model_call=model)
    assert second.ok and len(calls) == 1, "the model was asked again"
    assert any("no model call" in line for line in second.lines)


def test_a_failed_classification_is_not_written_back(vault):
    boxes = [_generic("???", 40, 40)]
    pdir = project(vault, shapes=boxes)
    result = dp.generate("demo", boxes, pdir, vault,
                         model_call=lambda p: "no idea")
    assert result.classified == {}
    assert [s.kind for s in dp.open_named("demo", vault).shapes] == [
        "generic"]


def test_the_described_window_skips_the_placeholder_title():
    assert dp.described_window({"title": "Untitled", "bg": "#112233",
                                "fg": "", "font": ""}) == {"bg": "#112233"}
    assert dp.described_window(None) == {}


def test_a_failed_call_does_not_report_the_previous_calls_stats(
        vault, monkeypatch):
    """REVIEW: the stats were read in a ``finally`` after EVERY call, and a
    call that raised (timeout, Stop, a dead server) records nothing — so the
    log counted the PREVIOUS call twice and called it this one's cost."""
    from council_core import model_slots
    monkeypatch.setattr(model_slots, "current",
                        _FakeSlots("ollama:phi3.5").current)
    eng = _fake_engine([{"id": "ollama:phi3.5", "params_b": 3.8}], 4096)
    calls = []

    def local_chat(messages, *, temperature=0.2, num_predict=600, model=None,
                   host=None, timeout=120, role=None, json_schema=None,
                   seed=None, stop=None, should_stop=None):
        calls.append(seed)
        if len(calls) == 1:
            return "I cannot draw that."
        raise TimeoutError("no progress for 300 s")
    eng.local_chat = local_chat
    eng.last_call_stats = lambda role=None: {
        "backend": "ollama", "model": "phi3.5", "prompt_tokens": 2000,
        "gen_tokens": 6, "seconds": 2.0, "constrained": True}
    monkeypatch.setitem(sys.modules, "council_engine", eng)
    result = dp.describe("a login form", project(vault, name="q"))
    assert not result.ok and len(calls) == 2
    line = next(n for n in result.notes if n.startswith("model calls:"))
    assert line.startswith("model calls: 1 on phi3.5"), line
    assert "2000 prompt + 6 reply tokens" in line, line


def test_the_cost_line_survives_the_graded_harness_note_cut(
        vault, monkeypatch):
    """REVIEW: run_describe_prompts records ``notes[:8]`` — the only place
    the per-call tokens and seconds reach its jsonl — and the cost line was
    appended LAST, so a small model's reply with a few synonyms and moved
    props pushed it out of what the measuring phase keeps."""
    import json
    from council_core import model_slots
    monkeypatch.setattr(model_slots, "current",
                        _FakeSlots("ollama:phi3.5").current)
    eng = _fake_engine([{"id": "ollama:phi3.5", "params_b": 3.8}], 4096)
    sloppy = {"window": {"title": "Login"}, "layout": {
        "kind": "column", "children": [
            {"kind": "dropdown", "label": "Mode", "values": ["A", "B"]},
            {"kind": "textbox", "label": "Name", "x": 10, "y": 10},
            {"kind": "password", "label": "Secret"},
            {"kind": "checkbox", "label": "Remember", "text": "Remember"},
            {"kind": "btn", "label": "Sign in", "script": "x.y"},
            {"kind": "toolbar", "label": "Tools",
             "children": ["Open", "Save"]}]}}

    def local_chat(messages, **_kw):
        return json.dumps(sloppy)
    eng.local_chat = local_chat
    eng.last_call_stats = lambda role=None: {
        "backend": "ollama", "model": "phi3.5", "prompt_tokens": 2000,
        "gen_tokens": 120, "seconds": 3.0, "constrained": True}
    monkeypatch.setitem(sys.modules, "council_engine", eng)
    result = dp.describe("a login form", project(vault, name="q"))
    assert result.ok, result.errors
    assert len(result.notes) > 8, result.notes
    assert any(n.startswith("model calls:") for n in result.notes[:8]),         result.notes
