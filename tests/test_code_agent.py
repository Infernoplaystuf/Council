"""The council's coding jobs: plan, steps with tools, checks, commits,
notes (council_core/code_agent.py), with scripted models."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from council_core import code_agent as ca  # noqa: E402
from council_core import project as pj  # noqa: E402

CAMERA = "class Camera:\n    def __init__(self):\n        self.exposure = 100\n"
TESTS = ("from camera import Camera\n\n\ndef test_default():\n"
         "    assert Camera().exposure == 100\n")


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=True).stdout


@pytest.fixture
def setup(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "u@x")
    git(repo, "config", "user.name", "u")
    (repo / "camera.py").write_text(CAMERA)
    (repo / "test_camera.py").write_text(TESTS)
    (repo / "CLAUDE.md").write_text("Keep it simple.\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "start")
    vault = tmp_path / "vault"
    project = pj.Project("cam", str(repo),
                         test_command="python -m pytest -q -p no:cacheprovider")
    pj.save(vault, project)
    return vault, project, repo


def chat_factory(plan_obj, note="Done: reset added.", brief="- Tests live "
                 "beside the code."):
    def chat(messages, **kw):
        if kw.get("json_schema"):
            return json.dumps(plan_obj)
        system = messages[0]["content"]
        if "handoff note" in system:
            return note
        if "EVERY future" in system:
            return brief
        return ""
    return chat


def scripted(*turns):
    """chat_tools that replays `turns` (lists of tool calls, or text)."""
    seen = []
    queue = list(turns)

    def chat_tools(messages, tools, **kw):
        seen.append([dict(m) for m in messages])
        if not queue:
            return {"content": "I am stuck.", "tool_calls": []}
        t = queue.pop(0)
        if isinstance(t, str):
            return {"content": t, "tool_calls": []}
        return {"content": "", "tool_calls": [
            {"name": n, "arguments": a} for n, a in t]}
    chat_tools.seen = seen
    return chat_tools


PLAN = {"goal": "a reset method", "steps": [
    {"title": "Add Camera.reset", "files": ["camera.py", "test_camera.py"],
     "check": "tests", "test_first": True,
     "details": "reset() sets exposure back to 100"}]}

ADD_TEST = ("edit_file", {"path": "test_camera.py",
                          "old": "    assert Camera().exposure == 100\n",
                          "new": "    assert Camera().exposure == 100\n\n\n"
                                 "def test_reset():\n    c = Camera()\n"
                                 "    c.exposure = 5\n    c.reset()\n"
                                 "    assert c.exposure == 100\n"})
ADD_CODE = ("edit_file", {"path": "camera.py",
                          "old": "        self.exposure = 100\n",
                          "new": "        self.exposure = 100\n\n"
                                 "    def reset(self):\n"
                                 "        self.exposure = 100\n"})


def test_the_planner_plan_is_read_and_shown(setup):
    vault, project, _repo = setup
    plan = ca.make_plan(vault, project, "add reset",
                        chat=chat_factory(PLAN), role="judge")
    assert plan.steps[0].test_first and plan.steps[0].check == "tests"
    assert "1. Add Camera.reset — check: tests [test first]" in plan.text()


def test_the_planning_prompt_has_the_brief_map_and_note(setup):
    vault, project, _repo = setup
    seen = {}

    def chat(messages, **kw):
        seen["prompt"] = messages[1]["content"]
        return json.dumps(PLAN)
    (pj.project_dir(vault, project) / "notes").mkdir(parents=True)
    (pj.project_dir(vault, project) / "notes" / "1.md").write_text(
        "Left to do: the GUI.")
    ca.make_plan(vault, project, "add reset", chat=chat, role="judge")
    p = seen["prompt"]
    assert "Keep it simple." in p and "Camera" in p
    assert "Left to do: the GUI." in p


def test_a_job_runs_test_first_and_commits_on_its_branch(setup):
    vault, project, repo = setup
    plan = ca.plan_from_json(PLAN)
    rec = ca.new_job(vault, project, "add reset", plan)
    events = []
    tools = scripted(
        [("find_symbol", {"name": "Camera"})],
        [ADD_TEST],
        [("run_tests", {})],                       # the new test fails
        [ADD_CODE],
        [("run_tests", {})],
        [("step_done", {"summary": "Camera.reset with its test"})])
    rec = ca.run_job(vault, project, rec, chat_tools=tools,
                     chat=chat_factory(PLAN),
                     on_event=lambda k, t: events.append((k, t)))
    assert rec.status == "done", events
    step = ca.plan_of(rec).steps[0]
    assert step.status == "done" and step.commit
    assert "def reset" not in (repo / "camera.py").read_text()   # untouched
    log = git(repo, "log", "--format=%s", rec.branch)
    assert "Step 1: Add Camera.reset" in log
    # The first prompt said test-first; a failing run reached the model.
    first = tools.seen[0][1]["content"]
    assert "TEST-FIRST" in first
    assert any("TESTS FAILED" in str(m.get("content")) for m in tools.seen[3])
    note = (pj.project_dir(vault, project) / "notes" / f"{rec.id}.md").read_text()
    assert "Done: reset added." in note and "[done] Add Camera.reset" in note
    assert "Tests live beside" in pj.proposed_brief(vault, project)
    assert pj.council_brief(vault, project) == ""      # proposed, not applied
    # The job can be merged into the project.
    from council_core import worktree as wt
    job = wt.open_job(project.root, pj.project_dir(vault, project)
                      / "worktrees", rec.id, rec.base)
    wt.merge(job)
    assert "def reset" in (repo / "camera.py").read_text()


def test_a_step_that_cannot_pass_is_undone_and_stops_the_job(setup):
    vault, project, repo = setup
    two = {"goal": "g", "steps": [
        dict(PLAN["steps"][0]), {"title": "Second", "files": [],
                                 "check": "tests", "details": ""}]}
    rec = ca.new_job(vault, project, "break it", ca.plan_from_json(two))
    bad = ("edit_file", {"path": "camera.py",
                         "old": "        self.exposure = 100\n",
                         "new": "        self.exposure = 1\n"})
    turns = [[bad], [("step_done", {"summary": "x"})]]
    turns += [[("step_done", {"summary": "x"})]] * 5
    rec = ca.run_job(vault, project, rec, chat_tools=scripted(*turns),
                     chat=chat_factory(two))
    steps = ca.plan_of(rec).steps
    assert rec.status == "failed"
    assert steps[0].status == "failed" and steps[1].status == "pending"
    from council_core import worktree as wt
    job = wt.open_job(project.root, pj.project_dir(vault, project)
                      / "worktrees", rec.id, rec.base)
    assert "self.exposure = 100" in (job.path / "camera.py").read_text()
    assert job.log() == []


def test_failures_from_before_the_job_do_not_count(setup):
    vault, project, repo = setup
    (repo / "test_old.py").write_text("def test_known_bad():\n    assert 0\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "a known failure")
    rec = ca.new_job(vault, project, "add reset", ca.plan_from_json(PLAN))
    rec = ca.run_job(vault, project, rec, chat_tools=scripted(
        [ADD_TEST], [ADD_CODE], [("step_done", {"summary": "ok"})]),
        chat=chat_factory(PLAN))
    assert rec.status == "done"
    assert rec.baseline == ["test_old.py::test_known_bad"]


def test_prose_instead_of_tools_is_nudged(setup):
    vault, project, repo = setup
    rec = ca.new_job(vault, project, "add reset", ca.plan_from_json(PLAN))
    tools = scripted("I would add a reset method.", [ADD_TEST], [ADD_CODE],
                     [("step_done", {"summary": "ok"})])
    rec = ca.run_job(vault, project, rec, chat_tools=tools,
                     chat=chat_factory(PLAN))
    assert rec.status == "done"
    assert "Use the tools" in tools.seen[1][-1]["content"]


def test_long_conversations_are_trimmed():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    msgs += [{"role": "tool", "content": "x" * 5000} for _ in range(10)]
    ca._trim(msgs, 12000)
    assert sum(len(m["content"]) for m in msgs) <= 12000 + 200
    assert msgs[-1]["content"] == "x" * 5000            # newest kept
