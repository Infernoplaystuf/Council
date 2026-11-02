"""The Build tab, step 5: Agent mode in the chat (plan, leave steps out, Go,
the job on its own branch, review and merge or discard) and the Model box
(the coder role's model, ranked for this PC's GPU memory, US models only).

Everything runs offscreen with scripted models in a temporary git folder and
a temporary vault; nothing is downloaded and no real model is called.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("COUNCIL_NO_DIALOGS", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication  # noqa: E402

from council_core import build_agent as ba  # noqa: E402
from council_core import build_models as bm  # noqa: E402
from council_core import code_agent as ca  # noqa: E402
from council_core import model_slots  # noqa: E402
from council_core import project as pj  # noqa: E402
from council_qt.tabs.build import BuildActions, BuildTab  # noqa: E402
from council_qt.widgets.build_agent import AGENT  # noqa: E402

GB = 1024 ** 3


# ── the coder model ─────────────────────────────────────────────────────
def model(id_, name, gb, origin="US", ctx=32768):
    return {"id": id_, "name": name, "size_bytes": int(gb * GB), "params_b": gb * 1.6,
            "origin": origin, "capabilities": ["completion"], "context_length": ctx,
            "family": "llama", "backend": "ollama"}


MODELS = [model("ollama:llama3.1:8b", "llama3.1:8b", 4.9),
          model("ollama:phi4:14b", "phi4:14b", 9.1),
          model("ollama:qwen2.5-coder:7b", "qwen2.5-coder:7b", 4.7, origin="CN")]


def test_the_choices_are_us_models_best_first_for_the_card():
    got = bm.choices(MODELS, vram_gb=8.0, ram_gb=32.0)
    assert [c.id for c in got] == ["ollama:llama3.1:8b", "ollama:phi4:14b"]
    assert got[0].recommended and not got[1].recommended
    assert got[0].fit == "gpu" and got[1].fit == "partial"
    # a bigger card puts the bigger model first
    assert bm.choices(MODELS, vram_gb=16.0, ram_gb=32.0)[0].id == "ollama:phi4:14b"


def test_catalog_models_are_suggested_only_when_they_fit_and_are_missing():
    lines = bm.to_get(24.0)
    assert lines and all(("ollama pull" in ln or "download" in ln) for ln in lines)
    assert not bm.to_get(None) and not bm.to_get(2.0)
    have = [model_slots.OLLAMA_PREFIX + "llama3.1:8b"]
    assert not any(ln.startswith("Llama 3.1 8B") and "ollama pull llama3.1:8b" in ln
                   for ln in bm.to_get(24.0, have))


def test_the_coder_gets_its_own_slot_and_the_other_roles_keep_theirs():
    cfg = model_slots.SlotConfig(
        {"main": model_slots.Slot("main"), "fast": model_slots.Slot("fast", "ollama:llama3.2:3b")},
        {"intern": "fast"})
    new = bm.with_coder(cfg, "ollama:phi4:14b")
    assert new.roles["intern"] == "fast"
    assert new.slots[new.roles["coder"]].path == "ollama:phi4:14b"
    assert bm.current(new) == "ollama:phi4:14b"
    # the coder joins a slot that already holds the model
    same = bm.with_coder(cfg, "ollama:llama3.2:3b")
    assert same.roles["coder"] == "fast" and len(same.slots) == 2
    # back to the main model: the slot only the coder used goes, the rest stay
    back = bm.with_coder(new, "")
    assert "coder" not in back.roles and bm.current(back) == ""
    assert set(back.slots) == {"main", "fast"} and back.roles["intern"] == "fast"
    # the main model's own file is "the main model"
    main = bm.with_coder(cfg, "C:/models/main.gguf", main="C:/models/main.gguf")
    assert "coder" not in main.roles
    assert cfg.roles == {"intern": "fast"}           # the original is untouched


def test_a_damaged_slot_file_is_never_written_over(tmp_path):
    path = model_slots.config_path(tmp_path)
    path.write_text("{not json", encoding="utf-8")
    line = bm.save_coder(tmp_path, "ollama:phi4:14b")
    assert line.startswith("Not changed")
    assert path.read_text(encoding="utf-8") == "{not json"


def test_saving_the_coder_writes_the_slot_file(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "council_engine", type(sys)("council_engine"))
    sys.modules["council_engine"].refresh_backend_config = lambda: {}
    line = bm.save_coder(tmp_path, "ollama:phi4:14b")
    assert "next question" in line
    cfg = model_slots.load(tmp_path)
    assert cfg.slots[cfg.slot_for("coder")].path == "ollama:phi4:14b"
    assert cfg.slot_for("writer") == "main"


# ── the plan as data ────────────────────────────────────────────────────
def test_a_plan_survives_the_chat_file_and_steps_can_be_left_out():
    plan = ca.Plan("a reset", [ca.Step("Add reset", ["camera.py"], "tests", True, "x"),
                               ca.Step("Docs", [], "none")], "none")
    card = json.loads(json.dumps(ba.plan_steps(plan)))
    back = ba.plan_from_steps(card)
    assert [s.title for s in back.steps] == ["Add reset", "Docs"]
    assert back.steps[0].test_first and back.goal == "a reset"
    card["steps"][1]["use"] = False
    assert [s.title for s in ba.plan_from_steps(card).steps] == ["Add reset"]
    card["steps"][0]["use"] = False
    assert ba.plan_from_steps(card) is None


def test_the_folder_gets_its_project_once(tmp_path):
    vault, root = tmp_path / "vault", tmp_path / "cam"
    root.mkdir()
    agent = ba.Agent(vault)
    p = agent.project_for(root, "C:/py/python.exe", has_tests=True)
    assert p.name == "cam" and p.root == str(root)
    assert p.test_command == '"C:/py/python.exe" -m pytest -q -p no:cacheprovider'
    assert not agent.is_saved(p)
    agent.save_project(pj.Project("Cam", str(root), test_command="make test"))
    again = agent.project_for(root, "python", has_tests=True)
    assert again.name == "Cam" and again.test_command == "make test"
    other = tmp_path / "other" / "cam"
    other.mkdir(parents=True)
    assert agent.project_for(other).name == "cam (2)"   # no clash with "Cam"


# ── Agent mode in the tab ─────────────────────────────────────────────────
PLAN = {"goal": "a reset", "steps": [
    {"title": "Add reset", "files": ["camera.py"], "check": "tests",
     "test_first": False, "details": "reset() sets exposure to 100"},
    {"title": "Unwanted step", "files": [], "check": "none", "details": ""}]}


class Models:
    """Scripted planner and coder; keeps what the planner was asked."""

    def __init__(self):
        self.asked = []

    def chat(self, messages, **kw):
        if kw.get("json_schema"):
            self.asked.append(messages[-1]["content"])
            return json.dumps(PLAN)
        return "NONE"

    def chat_tools(self, messages, tools, **kw):
        if not any(m.get("role") == "tool" for m in messages):
            return {"content": "", "tool_calls": [{"name": "edit_file", "arguments": {
                "path": "camera.py", "old": "    exposure = 100\n",
                "new": "    exposure = 100\n\n    def reset(self):\n"
                       "        self.exposure = 100\n"}}]}
        return {"content": "", "tool_calls": [
            {"name": "step_done", "arguments": {"summary": "reset added"}}]}


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "cam"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "u@x")
    git(repo, "config", "user.name", "u")
    (repo / "camera.py").write_text("class Camera:\n    exposure = 100\n")
    (repo / "test_camera.py").write_text(
        "from camera import Camera\n\ndef test_x():\n    assert Camera.exposure == 100\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "start")
    return repo


def make_tab(tmp_path, folder, models, review=("merge", None), confirm=True,
             source=None):
    vault = tmp_path / "vault"
    reviews = []

    def do_review(md):
        reviews.append(md)
        return review
    actions = BuildActions(
        vault_dir=vault, state_file=tmp_path / "state.json",
        unsaved_dir=tmp_path / "unsaved", confirm=lambda t, x: confirm,
        ask_folder=lambda start: "", ask_name=lambda t, l: None,
        chat=lambda *a, **k: "", review=do_review,
        agent=ba.Agent(vault, chat=models.chat, chat_tools=models.chat_tools),
        models_source=source or (lambda: ([], None, None)))
    actions.save_state({"folder": str(folder)})
    tab = BuildTab(None, actions)
    tab.reviews = reviews
    return tab


def settle(qapp, tab, seconds=120.0):
    deadline = time.time() + seconds
    quiet = 0
    while quiet < 10 and time.time() < deadline:
        qapp.processEvents()
        quiet = quiet + 1 if not tab.chat_panel.is_busy() else 0
        time.sleep(0.02)
    assert not tab.chat_panel.is_busy(), "timed out"


def agent_turns(tab):
    return [t for t in tab.chat_panel.turns() if t.mode == AGENT]


def plan_for(qapp, tab, text="Add Camera.reset"):
    panel = tab.chat_panel
    panel.set_mode(AGENT)
    assert panel.agent_row.isHidden() is False and panel.profile_box.count() >= 5
    panel.profile_box.setCurrentIndex(0)
    panel.send(text)
    settle(qapp, tab)
    return panel


def test_plan_leave_a_step_out_go_review_and_merge(qapp, tmp_path, repo):
    models = Models()
    tab = make_tab(tmp_path, repo, models)
    panel = plan_for(qapp, tab)
    last = panel.turns()[-1]
    assert last.kind == "agent-plan" and not panel.go_btn.isHidden()
    md = panel.markdown()
    assert "**Add reset**" in md and "[leave out](use:1)" in md and "[Go](go:)" in md
    panel.follow("use:1")
    assert "~~Unwanted step~~ (left out)" in panel.markdown()
    assert pj.load(tmp_path / "vault", "cam") is not None     # saved on the first plan

    panel.go()
    settle(qapp, tab)
    job = panel.turns()[-1]
    assert job.kind == "agent-job"
    assert "Job done: 1/1 step(s) committed" in job.content, job.content
    assert any("Step 1/1: Add reset" in s.get("text", "") for s in job.steps), job.steps
    assert "def reset" not in (repo / "camera.py").read_text()   # not yet merged
    job_id = next(s["job"] for s in job.steps if s.get("kind") == "agent-job")
    assert f"[Review and merge](review:{job_id})" in panel.markdown()

    doc = tab.open_file(repo / "camera.py")
    panel.follow(f"review:{job_id}")
    assert "+    def reset(self):" in tab.reviews[-1]
    assert "def reset" in (repo / "camera.py").read_text()
    assert "def reset" in doc.editor.toPlainText()              # the editor shows it
    assert "Merged into your folder" in panel.status.text()
    assert "*The job was merged.*" in panel.markdown()
    # it survives a restart of the chat
    tab.chat_panel.set_folder(repo)
    assert "*The job was merged.*" in tab.chat_panel.markdown()
    tab.deleteLater()


def test_a_change_to_the_plan_asks_again_with_the_first_request(qapp, tmp_path, repo):
    models = Models()
    tab = make_tab(tmp_path, repo, models)
    panel = plan_for(qapp, tab, "Add Camera.reset")
    panel.send("Also log the reset")
    settle(qapp, tab)
    assert "Add Camera.reset" in models.asked[-1]
    assert "Change to the plan: Also log the reset" in models.asked[-1]
    assert panel.turns()[-1].kind == "agent-plan"
    tab.deleteLater()


def test_merge_waits_for_unsaved_tabs_and_discard_asks(qapp, tmp_path, repo):
    models = Models()
    tab = make_tab(tmp_path, repo, models)
    panel = plan_for(qapp, tab)
    panel.go()
    settle(qapp, tab)
    job_id = next(s["job"] for s in panel.turns()[-1].steps if s.get("kind") == "agent-job")
    doc = tab.open_file(repo / "test_camera.py")
    doc.editor.insertPlainText("# mine\n")
    panel.follow(f"review:{job_id}")
    assert "unsaved" in panel.status.text()
    assert "def reset" not in (repo / "camera.py").read_text()
    assert doc.editor.toPlainText().startswith("# mine")     # the user's text is kept
    tab.actions.confirm = lambda t, x: False
    panel.follow(f"discard:{job_id}")
    assert "[Discard]" in panel.markdown()                    # not discarded
    tab.actions.confirm = lambda t, x: True
    panel.follow(f"discard:{job_id}")
    assert "*The job was discarded.*" in panel.markdown()
    assert "council/" not in git(repo, "branch", "--list")
    tab.deleteLater()


def test_agent_mode_needs_a_git_folder_and_no_open_session(qapp, tmp_path, repo):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("x = 1\n")
    tab = make_tab(tmp_path, plain, Models())
    panel = plan_for(qapp, tab)
    assert "git folder" in panel.status.text(), panel.status.text()
    assert panel.turns()[-1].role == "user"                   # no plan
    tab.deleteLater()

    tab = make_tab(tmp_path, repo, Models())
    tab.start_session()
    assert tab.session is not None
    panel = tab.chat_panel
    panel.set_mode(AGENT)
    panel.send("Add Camera.reset")
    assert "End the Edit-mode session first" in panel.status.text()
    assert not panel.turns()
    tab.end_session()
    tab.deleteLater()


def test_a_report_agent_answers_and_changes_nothing(qapp, tmp_path, repo, monkeypatch):
    from council_core import code_profiles as ap
    monkeypatch.setattr(ap, "run_report", lambda *a, **k: "All fine.")
    tab = make_tab(tmp_path, repo, Models())
    panel = tab.chat_panel
    panel.set_mode(AGENT)
    names = [p.output for p in panel.agent_mode._profiles]
    panel.profile_box.setCurrentIndex(names.index("report"))
    panel.send("Review the camera")
    settle(qapp, tab)
    assert panel.turns()[-1].kind == "agent-report"
    assert "All fine." in panel.markdown()
    assert git(repo, "status", "--porcelain") == ""
    tab.deleteLater()


# ── the Model box ────────────────────────────────────────────────────────
def test_the_model_box_ranks_installed_us_models_and_switches_the_coder(
        qapp, tmp_path, repo, monkeypatch):
    monkeypatch.setitem(sys.modules, "council_engine", type(sys)("council_engine"))
    sys.modules["council_engine"].refresh_backend_config = lambda: {}
    tab = make_tab(tmp_path, repo, Models(), source=lambda: (MODELS, 8.0, 32.0))
    panel = tab.chat_panel
    panel.models.fill()
    deadline = time.time() + 30
    while panel.model_box.count() < 2 and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    items = [panel.model_box.itemText(i) for i in range(panel.model_box.count())]
    assert items[0].startswith("Main model") and items[1].startswith("★ llama3.1:8b")
    assert not any("qwen" in i for i in items)                 # not US: not offered
    panel.model_box.setCurrentIndex(2)
    panel.models.chosen()
    deadline = time.time() + 30
    while "next question" not in panel.status.text() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    cfg = model_slots.load(tmp_path / "vault")
    assert cfg.slots[cfg.slot_for("coder")].path == "ollama:phi4:14b"
    tab.deleteLater()


def test_a_quoted_python_path_runs_on_windows_too():
    """The project's test command quotes "Run with"'s Python. Windows splits
    it with posix=False, which used to leave the quotes on the program name
    (a job's every test check then failed to start)."""
    from council_core.project_tools import split_command
    cmd = ba.test_command("C:\\Program Files\\py\\python.exe")
    assert split_command(cmd, windows=True)[:3] == [
        "C:\\Program Files\\py\\python.exe", "-m", "pytest"]
    assert split_command(ba.test_command("/usr/bin/python3"), windows=False)[:3] == [
        "/usr/bin/python3", "-m", "pytest"]
    assert split_command('python -m pytest -k "a and b"', windows=True)[-1] == "a and b"


# ── what the Code tab had besides (it was retired into Build, 2026-10-30) ──
def test_the_project_dialog_reads_checks_and_references(qapp):
    from council_qt.widgets.code_dialogs import ProjectDialog
    d = ProjectDialog(None, pj.Project("x", "/r", gui_checks=[
        pj.GuiCheck("pkg.tab", "Tab")], references=["/docs"]))
    d.gui.setPlainText("pkg.tab:Tab\nother.mod:Win\nnonsense")
    p = d.project()
    assert [(g.module, g.cls) for g in p.gui_checks] == [
        ("pkg.tab", "Tab"), ("other.mod", "Win")]
    assert p.references == ["/docs"]


def test_past_jobs_are_listed_and_can_still_be_merged(qapp, tmp_path, repo):
    tab = make_tab(tmp_path, repo, Models())
    panel = plan_for(qapp, tab)
    panel.go()
    settle(qapp, tab)
    job_id = next(s["job"] for s in panel.turns()[-1].steps if s.get("kind") == "agent-job")
    panel.set_folder(repo)                    # as after a restart
    panel.agent_mode.list_jobs()
    listing = panel.turns()[-1]
    assert listing.kind == "agent-jobs"
    md = panel.markdown()
    assert "**Jobs on cam** (1)" in md and "Add Camera.reset · done" in md
    assert f"[Review and merge](review:{job_id})" in md
    panel.follow(f"review:{job_id}")
    assert "def reset" in (repo / "camera.py").read_text()
    # both the job's own reply and the list now say it was merged
    assert all(s["status"] == "merged" for t in panel.turns() for s in t.steps
               if s.get("kind") == "agent-job" and s.get("job") == job_id)
    assert "Add Camera.reset · merged" in panel.markdown()
    tab.deleteLater()


def test_the_history_benchmark_runs_from_the_chat(qapp, tmp_path, repo):
    tab = make_tab(tmp_path, repo, Models())
    panel = tab.chat_panel
    panel.set_mode(AGENT)
    calls = {}

    def bench(project, count, say, should_stop):
        say("[1/1] abc Add reset")
        calls["count"] = count
        calls["project"] = project.root
        return {"solved": 1, "valid": 1, "tasks": 1, "mean_calls": 4,
                "mean_seconds": 9.5, "file": "code-history-x.jsonl"}
    panel.agent_mode.agent.bench = bench
    panel.agent_mode.bench()
    settle(qapp, tab)
    assert calls == {"count": 5, "project": str(repo)}
    turn = panel.turns()[-1]
    assert turn.kind == "agent-bench" and "Solved 1 of 1" in turn.content
    assert any("[1/1] abc Add reset" in s.get("text", "") for s in turn.steps)
    tab.deleteLater()


def test_the_brief_is_opened_for_the_folders_project(qapp, tmp_path, repo):
    tab = make_tab(tmp_path, repo, Models())
    panel = tab.chat_panel
    panel.set_mode(AGENT)
    panel.on_brief()
    assert pj.load(tmp_path / "vault", "cam") is not None     # saved to have a brief
    assert panel._dialog.project.root == str(repo)
    panel._dialog.close()
    tab.deleteLater()


def test_snapshot_to_vault_saves_the_open_file(qapp, tmp_path, repo):
    saved = []

    class Result:
        error, path = "", tmp_path / "vault" / "council_code_1.py"
    tab = make_tab(tmp_path, repo, Models())
    tab.actions.snapshot = lambda code: saved.append(code) or Result()
    tab.on_snapshot()
    assert "Nothing to snapshot" in tab.status.text()
    tab.open_file(repo / "camera.py")
    tab.on_snapshot()
    deadline = time.time() + 20
    while not saved or "Snapshot saved" not in tab.status.text():
        qapp.processEvents()
        time.sleep(0.01)
        assert time.time() < deadline
    assert saved == ["class Camera:\n    exposure = 100\n"]
    assert "[snapshot saved:" in tab.output.toPlainText()
    tab.deleteLater()
