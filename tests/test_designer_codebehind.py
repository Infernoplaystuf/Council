"""
"Write it with the model…" on a real project, with a scripted model.

Real vault (temp), real Generate, real policy gate, real smoke subprocess —
only the model is a stub. What is proven here is the PROJECT contract:

  * nothing is written until apply(), and apply() refuses a file that
    changed since the model was shown it;
  * apply backs up, writes atomically and records a sha256 of what it wrote;
  * function mode wires the widget to logic.<fn>, and the next Generate turns
    the untouched TODO stub into the linked stub — gui_spec accepts the
    project-local module only because Generate now passes the project;
  * handler mode writes only over an untouched stub or an untouched model
    body, wrapped in the standard try/report_error envelope, and Generate
    treats an untouched model body as its own: removed with its widget,
    rewritten when the widget is wired — and an EDITED one as the user's.
"""
from __future__ import annotations

import ast
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gui_codebehind as gcb  # noqa: E402
import gui_emit  # noqa: E402
import gui_layout  # noqa: E402
import gui_projects  # noqa: E402
import gui_spec  # noqa: E402
from council_core import designer_codebehind as dc  # noqa: E402
from council_core import designer_project as dp  # noqa: E402
from council_core import designer_wiring as dw  # noqa: E402
from gui_shapes import new_shape  # noqa: E402


def mk(kind, label, x, y, **props):
    s = new_shape(kind, x, y)
    s.label = label
    s.props.update(props)
    return s


@pytest.fixture
def demo(tmp_path):
    """A small linked Qt project, generated once: a folder picker, a status
    label (port "status"), a file list and a Count button with a note."""
    vault = tmp_path / "vault"
    folder = mk("file_picker", "Image folder", 40, 40, mode="folder")
    status = mk("label", "", 40, 100)
    status.port = {"name": "status"}
    files = mk("listbox", "Files", 40, 160)
    button = mk("button", "Count images", 400, 40)
    button.note = "count the PNG files in the image folder and list them"
    shapes = [folder, status, files, button]
    assert dp.create("demo", "linked", vault, toolkit="qt").ok
    dp.save("demo", shapes, vault)
    pdir = gui_projects.project_path("demo", vault)
    assert dp.generate("demo", shapes, pdir, vault).ok
    return types.SimpleNamespace(vault=vault, pdir=pdir, shapes=shapes,
                                 button=button, folder=folder, files=files,
                                 status=status)


GOOD = '''```python
def count_images(image_folder: str) -> dict:
    """Count the PNG files."""
    import os
    names = sorted(n for n in os.listdir(image_folder)
                   if n.lower().endswith(".png"))
    return {"status": f"{len(names)} PNG file(s)", "files": names}
```'''


class Script:
    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def __call__(self, prompt, **_kw):
        self.prompts.append(prompt)
        return self.replies[min(len(self.prompts), len(self.replies)) - 1]


def request(demo, **kw):
    base = dict(project_dir=demo.pdir, shapes=demo.shapes,
                shape_id=demo.button.id, instruction=demo.button.note,
                inputs=["image_folder"],
                outputs={"status": "status", "files": "files"})
    base.update(kw)
    return dc.Request(**base)


def generate(demo):
    dp.save("demo", demo.shapes, demo.vault)
    return dp.generate("demo", demo.shapes, demo.pdir, demo.vault)


def handlers_of(demo) -> str:
    return (demo.pdir / "handlers.py").read_text(encoding="utf-8")


def test_the_module_imports_no_toolkit():
    """Decisions here, widgets in council_qt — testable with no display."""
    tree = ast.parse((ROOT / "council_core" / "designer_codebehind.py")
                     .read_text(encoding="utf-8"))
    roots = {(a.name if isinstance(n, ast.Import) else n.module or "")
             .split(".")[0] for n in ast.walk(tree)
             if isinstance(n, (ast.Import, ast.ImportFrom))
             for a in n.names}
    assert not roots & {"PySide6", "tkinter", "council_qt"}


# ============================================================
# The manifest record and the backup
# ============================================================

def test_the_ai_record_survives_a_save_and_an_old_manifest_has_none(tmp_path):
    gui_projects.create("p", "linked", vault_dir=tmp_path)
    pdir = gui_projects.project_path("p", tmp_path)
    m = gui_projects.load_manifest(pdir)
    assert m.ai_handlers == {}
    m.ai_handlers["on_btn_go"] = {"kind": "handler", "sha256": "ab"}
    m.ai_handlers["logic.f"] = {"kind": "function", "sha256": "cd"}
    gui_projects.save_manifest(pdir, m)
    back = gui_projects.load_manifest(pdir)
    assert back.ai_handlers["logic.f"]["sha256"] == "cd"
    assert gui_projects.ai_bodies(back) == {"on_btn_go": "ab"}


def test_a_backup_includes_logic_py(tmp_path):
    gui_projects.create("p", "linked", vault_dir=tmp_path)
    pdir = gui_projects.project_path("p", tmp_path)
    (pdir / "logic.py").write_text("def f():\n    return {}\n",
                                   encoding="utf-8")
    dest = gui_projects.backup(pdir)
    assert (dest / "logic.py").is_file()


# ============================================================
# gui_spec and the Wiring group accept the project's own module
# ============================================================

def _spec_with_link(demo, link):
    demo.button.script = link
    proj = gui_projects.open_project("demo", vault_dir=demo.vault)
    tree = gui_layout.infer(demo.shapes, proj.canvas.w, proj.canvas.h)
    m = gui_projects.load_manifest(demo.pdir)
    return gui_spec.build(demo.shapes, tree, None,
                          registry=m.widget_names,
                          port_registry=m.port_names, mode=m.mode)


def test_a_logic_link_needs_the_project_to_validate(demo):
    (demo.pdir / "logic.py").write_text(
        "def count_images(folder):\n    return {}\n", encoding="utf-8")
    spec = _spec_with_link(demo, {"module": "logic",
                                  "function": "count_images",
                                  "inputs": ["image_folder"],
                                  "outputs": {"status": "status"}})
    ok, errs = gui_spec.validate(spec)
    assert not ok and any("'logic' is not allowed" in e for e in errs)
    ok, errs = gui_spec.validate(spec, demo.pdir)
    assert ok, errs


def test_a_logic_link_to_a_missing_function_is_refused(demo):
    (demo.pdir / "logic.py").write_text("def other():\n    return {}\n",
                                        encoding="utf-8")
    spec = _spec_with_link(demo, {"module": "logic", "function": "nope",
                                  "inputs": [], "outputs": {}})
    ok, errs = gui_spec.validate(spec, demo.pdir)
    assert not ok and any("logic has no function 'nope'" in e for e in errs)


def test_a_project_file_named_like_a_council_module_is_not_local(demo):
    """A linked main.py puts the Council's folder FIRST on sys.path, so a
    project's frame_timing.py would never be the one imported."""
    (demo.pdir / "frame_timing.py").write_text("def mine():\n    pass\n",
                                               encoding="utf-8")
    (demo.pdir / "helpers.py").write_text("def mine():\n    pass\n",
                                          encoding="utf-8")
    assert dw.local_modules(demo.pdir) == ["helpers"]


def test_the_module_list_offers_logic_first(demo):
    (demo.pdir / "logic.py").write_text("def f(x):\n    return {'a': x}\n",
                                        encoding="utf-8")
    mods = dw.linkable_modules("linked", (), demo.pdir)
    assert mods[0] == "logic" and "frame_timing" in mods
    info = dw.module_info_for("logic", demo.pdir)
    assert info.found and info.function("f").result_keys == ("a",)
    ports = dw.project_ports(demo.shapes)
    link = {"module": "logic", "function": "f", "inputs": ["image_folder"],
            "outputs": {"status": "a"}}
    assert dw.problems(link, ports, project_dir=demo.pdir) == []
    assert any("not a module this app may import" in p
               for p in dw.problems(link, ports))


def test_typed_signatures_and_docstring_keys_are_read():
    info = dw.module_info("frame_timing")
    fn = info.function("scan_report")
    assert fn.typed_signature() == "scan_report(folder: Any) -> Dict[str, Any]"
    setup = dw.module_info("frame_camera").function("setup")
    assert set(setup.result_keys) == {"rows", "summary", "notes"}
    assert "summary()" in info.members("FolderReport")


def test_doc_keys_read_both_forms():
    assert dw._doc_keys("Do it.\n\nKeys: a, b c\n") == ["a", "b", "c"]
    assert dw._doc_keys("Do it.\n\nKeys:\n    count  int  how many\n"
                        "    names  list\n\nMore.") == ["count", "names"]
    assert dw._doc_keys("Keys are stable because ...") == []


# ============================================================
# Plan
# ============================================================

def test_plan_decides_the_signature_and_the_link(demo):
    plan = dc.plan(request(demo))
    assert plan.ok, plan.problems
    assert plan.target.signature() == \
        "def count_images(image_folder: str) -> dict:"
    assert [o.check for o in plan.target.outputs] == ["text", "list"]
    assert plan.link == {"module": "logic", "function": "count_images",
                         "inputs": ["image_folder"],
                         "outputs": {"status": "status", "files": "files"}}
    assert plan.target.params[0].sample == "<FOLDER>"
    assert plan.before == "" and "new function" in plan.replacing


def test_with_no_rows_the_ports_are_read_from_the_instruction(demo):
    plan = dc.plan(request(demo, inputs=(), outputs={},
                           instruction="count the PNG files in the image "
                                       "folder and show them in files and "
                                       "status"))
    assert plan.ok, plan.problems
    assert plan.link["inputs"] == ["image_folder"]
    assert set(plan.link["outputs"]) == {"files", "status"}


@pytest.mark.parametrize("change,why", [
    (dict(instruction="  "), "instruction is empty"),
    (dict(mode="weird"), "unknown mode"),
    (dict(outputs={"count_images": "x"}), "cannot show a result"),
    (dict(inputs=["nope"]), "is not a port"),
])
def test_plan_refusals(demo, change, why):
    plan = dc.plan(request(demo, **change))
    assert not plan.ok and any(why in p for p in plan.problems), plan.problems


def test_a_label_cannot_run_code(demo):
    plan = dc.plan(request(demo, shape_id=demo.files.id))
    assert not plan.ok and "does not run code" in plan.problems[0]


def test_a_detached_project_is_refused(demo):
    m = gui_projects.load_manifest(demo.pdir)
    m.detached = True
    gui_projects.save_manifest(demo.pdir, m)
    assert "detached" in dc.plan(request(demo)).problems[0]


def test_plan_never_raises_on_a_missing_project(tmp_path):
    plan = dc.plan(dc.Request(tmp_path / "nowhere", [], "x", "do it"))
    assert not plan.ok and plan.problems


# ============================================================
# Function mode, end to end
# ============================================================

def test_run_writes_nothing_and_offers_a_diff(demo):
    review = dc.run(dc.plan(request(demo)), model_call=Script(GOOD))
    assert review.ok, review.result.errors
    assert not (demo.pdir / "logic.py").exists()
    assert "+def count_images(image_folder: str) -> dict:" in review.diff
    assert any("smoke run: ok" in g for g in review.result.gates)
    assert "Accept wires it" in "\n".join(review.report())


def test_accept_writes_backs_up_records_and_wires_and_generate_follows(demo):
    review = dc.run(dc.plan(request(demo)), model_call=Script(GOOD))
    applied = dc.apply(review)
    assert applied.ok, applied.message
    logic = (demo.pdir / "logic.py").read_text(encoding="utf-8")
    assert "def count_images(image_folder: str) -> dict:" in logic
    assert Path(applied.backup).is_dir()
    record = gui_projects.load_manifest(demo.pdir).ai_handlers[
        "logic.count_images"]
    assert record["sha256"] == gcb.sha(gcb.function_text(logic,
                                                         "count_images"))
    assert record["widget"] == demo.button.id
    # The person's wiring step, then Generate.
    demo.button.script = applied.link
    result = generate(demo)
    assert result.ok, result.lines
    assert "policy: OK" in result.lines
    assert any("rewired on_btn_count_images" in ln for ln in result.lines)
    src = handlers_of(demo)
    assert "from logic import count_images" in src
    assert "result = count_images(self.ports.image_folder.get())" in src
    assert 'self.ports.files.set(result["files"])' in src


def test_the_generated_handler_really_calls_the_function(demo, tmp_path):
    """Import the project's own logic.py and call it the way the linked stub
    does — the code that was smoke-run is the code that was written."""
    dc.apply(dc.run(dc.plan(request(demo)), model_call=Script(GOOD)))
    sample = tmp_path / "frames"
    sample.mkdir()
    for n in ("a.png", "b.PNG", "c.txt"):
        (sample / n).write_text("x", encoding="utf-8")
    ns = {}
    exec(compile((demo.pdir / "logic.py").read_text(encoding="utf-8"),
                 "logic.py", "exec"), ns)
    assert ns["count_images"](str(sample)) == {
        "status": "2 PNG file(s)", "files": ["a.png", "b.PNG"]}


def test_apply_refuses_when_the_file_changed_meanwhile(demo):
    review = dc.run(dc.plan(request(demo)), model_call=Script(GOOD))
    (demo.pdir / "logic.py").write_text("# typed by the user\n",
                                        encoding="utf-8")
    applied = dc.apply(review)
    assert not applied.ok and "changed since" in applied.message
    assert (demo.pdir / "logic.py").read_text(encoding="utf-8") == \
        "# typed by the user\n"


def test_a_second_write_replaces_only_the_models_unedited_function(demo):
    dc.apply(dc.run(dc.plan(request(demo)), model_call=Script(GOOD)))
    again = dc.plan(request(demo))
    assert "earlier count_images() (unedited)" in again.replacing
    assert again.target.name == "count_images"
    # Edit it by hand: the next write must leave it alone.
    path = demo.pdir / "logic.py"
    path.write_text(path.read_text(encoding="utf-8").replace(
        '"""Count the PNG files."""', '"""Mine now."""'), encoding="utf-8")
    third = dc.plan(request(demo))
    assert third.target.name == "count_images_2"
    review = dc.run(third, model_call=Script(GOOD))
    assert review.ok and dc.apply(review).ok
    text = path.read_text(encoding="utf-8")
    assert '"""Mine now."""' in text and "def count_images_2(" in text


def test_a_smoke_failure_is_repaired_with_the_real_sandbox(demo, tmp_path):
    victim = tmp_path / "outside.txt"
    bad = f'''```python
def count_images(image_folder: str) -> dict:
    import os
    names = sorted(os.listdir(image_folder))
    with open({str(victim)!r}, "w") as fh:
        fh.write("\\n".join(names))
    return {{"status": str(len(names)), "files": names}}
```'''
    model = Script(bad, GOOD)
    review = dc.run(dc.plan(request(demo)), model_call=model)
    assert review.ok and review.result.attempts == 2
    assert "writes outside" in model.prompts[1]
    assert "write files only inside a folder" in model.prompts[1]
    assert not victim.exists()


def test_a_helper_module_the_user_wrote_is_in_the_sandbox_too(demo):
    (demo.pdir / "helpers.py").write_text(
        "def is_png(name):\n    return name.lower().endswith('.png')\n",
        encoding="utf-8")
    reply = GOOD.replace("    import os\n",
                         "    import os\n    from helpers import is_png\n"
                         ).replace('if n.lower().endswith(".png"))',
                                   "if is_png(n))")
    plan = dc.plan(request(demo))
    assert "helpers" in plan.target.local_modules
    review = dc.run(plan, model_call=Script(reply))
    assert review.ok, review.result.errors
    assert "from helpers import is_png" in review.after


def test_a_model_that_never_passes_offers_nothing(demo):
    review = dc.run(dc.plan(request(demo)),
                    model_call=Script("I am not sure how to do that."))
    assert not review.ok and review.after == ""
    assert "NOT OFFERED" in "\n".join(review.report())
    assert not dc.apply(review).ok
    assert not (demo.pdir / "logic.py").exists()


# ============================================================
# Handler mode
# ============================================================

BODY = '''```python
def on_btn_count_images(self, *args) -> None:
    """Say how many files there are."""
    import os
    names = os.listdir(self.ports.image_folder.get())
    self.ports.status.set(f"{len(names)} file(s)")
```'''


def handler_request(demo, **kw):
    return request(demo, mode="handler", inputs=(), outputs={},
                   instruction="say how many files the folder holds", **kw)


def test_handler_mode_wraps_the_body_in_the_failure_envelope(demo):
    review = dc.run(dc.plan(handler_request(demo)), model_call=Script(BODY))
    assert review.ok, review.result.errors
    text = review.written
    assert "UNREVIEWED" in text and "try:" in text
    assert "self.clear_ports('status')" in text
    assert "self.report_error('Count images', exc)" in text
    assert dc.apply(review).ok
    src = handlers_of(demo)
    assert gui_emit.handler_text(src, "on_btn_count_images") == text
    rec = gui_projects.load_manifest(demo.pdir).ai_handlers[
        "on_btn_count_images"]
    assert rec["kind"] == "handler" and rec["sha256"] == gcb.sha(text)


def test_quotes_in_the_instruction_cannot_break_handlers_py(demo):
    req = handler_request(demo)
    req.instruction = 'say "how many" files are in C:\\new\\frames""'
    review = dc.run(dc.plan(req), model_call=Script(BODY))
    assert review.ok, review.result.errors
    ast.parse(review.after)


def test_generate_keeps_an_untouched_model_body(demo):
    dc.apply(dc.run(dc.plan(handler_request(demo)), model_call=Script(BODY)))
    before = handlers_of(demo)
    result = generate(demo)
    assert result.ok and handlers_of(demo) == before
    assert not any("WARNING" in ln for ln in result.lines)


def test_deleting_the_widget_removes_an_untouched_model_body(demo):
    dc.apply(dc.run(dc.plan(handler_request(demo)), model_call=Script(BODY)))
    demo.shapes.remove(demo.button)
    result = generate(demo)
    assert result.ok, result.lines
    assert not any("BLOCKED" in ln for ln in result.lines)
    assert "def on_btn_count_images" not in handlers_of(demo)
    assert "on_btn_count_images" not in gui_projects.load_manifest(
        demo.pdir).ai_handlers


def test_an_edited_model_body_is_the_users(demo):
    dc.apply(dc.run(dc.plan(handler_request(demo)), model_call=Script(BODY)))
    path = demo.pdir / "handlers.py"
    path.write_text(path.read_text(encoding="utf-8").replace(
        'file(s)")', 'files here")'), encoding="utf-8")
    plan = dc.plan(handler_request(demo))
    assert not plan.ok and "edited by hand" in plan.problems[0]
    demo.shapes.remove(demo.button)
    result = generate(demo)
    # Not removed: it is the user's now, and find_orphans says so.
    assert any("BLOCKED" in ln for ln in result.lines)
    assert "files here" in handlers_of(demo)


def test_wiring_the_widget_later_replaces_an_untouched_model_body(demo):
    dc.apply(dc.run(dc.plan(handler_request(demo)), model_call=Script(BODY)))
    dc.apply(dc.run(dc.plan(request(demo)), model_call=Script(GOOD)))
    demo.button.script = dc.plan(request(demo)).link
    result = generate(demo)
    assert result.ok, result.lines
    assert any("the model-written body" in ln for ln in result.lines)
    assert "from logic import count_images" in handlers_of(demo)


def test_handler_mode_refuses_a_wired_widget_and_a_missing_handlers_py(
        demo):
    demo.button.script = {"module": "frame_timing",
                          "function": "scan_report",
                          "inputs": ["image_folder"],
                          "outputs": {"status": "summary"}}
    plan = dc.plan(handler_request(demo))
    assert not plan.ok and "Use Function mode" in plan.problems[0]
    demo.button.script = {}
    (demo.pdir / "handlers.py").unlink()
    plan = dc.plan(handler_request(demo))
    assert not plan.ok and "Generate the project first" in plan.problems[0]


def test_the_handler_smoke_run_presses_the_wrapped_method(demo):
    bad = BODY.replace("self.ports.status", "self.ports.result_box")
    good = BODY
    model = Script(bad, good)
    review = dc.run(dc.plan(handler_request(demo)), model_call=model)
    assert review.ok and review.result.attempts == 2
    assert "result_box" in model.prompts[1]


# ============================================================
# The model call, documentation and model size
# ============================================================

def test_the_model_call_drops_what_the_engine_does_not_take(monkeypatch):
    seen = {}

    def local_chat(messages, *, temperature=0.2, num_predict=600, model=None,
                   host=None, timeout=120, role=None):
        seen.update(role=role, num_predict=num_predict,
                    temperature=temperature)
        return "ok"

    monkeypatch.setitem(sys.modules, "council_engine",
                        types.SimpleNamespace(local_chat=local_chat))
    assert dc.default_model_call("p", seed=3, temperature=0.45,
                                 should_stop=lambda: False) == "ok"
    assert seen == {"role": "coder", "num_predict": gcb.NUM_PREDICT,
                    "temperature": 0.45}


def test_the_model_call_passes_seed_and_stop_when_the_engine_takes_them(
        monkeypatch):
    seen = {}

    def local_chat(messages, **kw):
        seen.update(kw)
        return "ok"

    monkeypatch.setitem(sys.modules, "council_engine",
                        types.SimpleNamespace(local_chat=local_chat))
    dc.default_model_call("p", seed=7)
    assert seen["seed"] == 7 and list(seen["stop"]) == list(gcb.STOPS)


def test_documentation_is_asked_for_only_when_a_package_is_named(
        demo, monkeypatch):
    asked = []

    def docs_context(question, *, packages=(), servers=None, max_chars=6000):
        asked.append(tuple(packages))
        return [{"server": "docs", "source": "skimage.md",
                 "title": "threshold_otsu",
                 "text": "skimage.filters.threshold_otsu(image) -> float"}]

    fake = types.ModuleType("council_core.docs_qa")
    fake.docs_context = docs_context
    monkeypatch.setitem(sys.modules, "council_core.docs_qa", fake)
    import council_core
    monkeypatch.setattr(council_core, "docs_qa", fake, raising=False)
    plan = dc.plan(request(demo))
    assert asked == [] and plan.target.docs == []
    m = gui_projects.open_project("demo", vault_dir=demo.vault)
    m.requires = ["skimage"]
    gui_projects.save_project("demo", m, vault_dir=demo.vault)
    plan = dc.plan(request(demo, instruction="threshold each frame with "
                                             "skimage and count the PNGs"))
    assert asked == [("skimage",)]
    assert plan.target.docs[0]["title"] == "threshold_otsu"
    prompt, _ = gcb.build_prompt(plan.target)
    assert "threshold_otsu(image) -> float" in prompt


def test_documentation_degrades_to_nothing(monkeypatch):
    monkeypatch.setitem(sys.modules, "council_core.docs_qa", None)
    assert dc.docs_for("use pandas to read it", []) == ([], "")

    def boom(*a, **k):
        raise ConnectionError("server down")
    fake = types.ModuleType("council_core.docs_qa")
    fake.docs_context = boom
    monkeypatch.setitem(sys.modules, "council_core.docs_qa", fake)
    import council_core
    monkeypatch.setattr(council_core, "docs_qa", fake, raising=False)
    docs, note = dc.docs_for("use pandas to read it", [])
    assert docs == [] and "server down" in note


def test_packages_named():
    assert dc.packages_named("read it with pandas", []) == ["pandas"]
    assert dc.packages_named("use cv2.imread(path)", []) == ["cv2"]
    assert dc.packages_named("count the files", ["pypylon"]) == []
    assert dc.packages_named("grab with pypylon", ["pypylon"]) == ["pypylon"]
    assert dc.packages_named("use os.listdir(x)", []) == []


@pytest.mark.parametrize("size,n", [(3.8, 3), (8.0, 2), (14.0, 1),
                                    (None, 1)])
def test_small_models_get_more_first_draws(size, n):
    assert gcb.default_n_best(size) == n


def test_function_names_come_from_the_label_or_the_instruction():
    assert dc.function_name("Count images", "", "btn_x") == "count_images"
    assert dc.function_name("Go", "measure the mean brightness of a frame",
                            "btn_go") == "measure_mean_brightness"
    assert dc.function_name("", "", "btn_go") == "btn_go_logic"
    # Never a builtin's or a keyword's name: logic.py would shadow it.
    assert dc.function_name("List", "", "btn_list") == "list_fn"
    assert dc.function_name("Pass", "", "btn_pass") == "pass_fn"
