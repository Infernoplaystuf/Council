"""The coding benchmark from git history (council_core/code_bench.py)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from council_core import code_bench as cb  # noqa: E402
from council_core import project as pj  # noqa: E402


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=True).stdout


@pytest.fixture
def history(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "u@x")
    git(repo, "config", "user.name", "u")
    (repo / "camera.py").write_text("class Camera:\n    exposure = 100\n")
    (repo / "test_camera.py").write_text(
        "from camera import Camera\n\n\ndef test_default():\n"
        "    assert Camera.exposure == 100\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "start")
    # The task: a commit that adds code AND a test for it.
    (repo / "camera.py").write_text(
        "class Camera:\n    exposure = 100\n\n    def reset(self):\n"
        "        self.exposure = 100\n")
    with (repo / "test_camera.py").open("a") as fh:
        fh.write("\n\ndef test_reset():\n    c = Camera()\n    c.exposure = 5"
                 "\n    c.reset()\n    assert c.exposure == 100\n")
    git(repo, "commit", "-q", "-am", "Add Camera.reset\n\nPuts exposure back.")
    # Not a task: docs only.
    (repo / "README.md").write_text("hi")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "docs")
    return repo


def test_candidates_are_commits_with_code_and_tests(history):
    tasks = cb.candidates(history)
    assert [t.message.splitlines()[0] for t in tasks] == ["Add Camera.reset"]
    assert tasks[0].code_files == ["camera.py"]
    assert tasks[0].test_files == ["test_camera.py"]


def test_prepare_undoes_the_code_and_keeps_the_tests(history, tmp_path):
    from council_core import worktree as wt
    task = cb.candidates(history)[0]
    job = cb.prepare(history, tmp_path / "w", task)
    assert "def reset" not in (job.path / "camera.py").read_text()
    assert "test_reset" in (job.path / "test_camera.py").read_text()
    wt.discard(job)


def _plan_chat(messages, **kw):
    if kw.get("json_schema"):
        return json.dumps({"goal": "reset", "steps": [{
            "title": "Add reset", "files": ["camera.py"], "check": "tests",
            "details": "add reset()"}]})
    return "NONE"


def _solver(messages, tools, **kw):
    n = sum(1 for m in messages if m.get("role") == "tool")
    if n == 0:
        return {"content": "", "tool_calls": [{"name": "edit_file", "arguments": {
            "path": "camera.py", "old": "    exposure = 100\n",
            "new": "    exposure = 100\n\n    def reset(self):\n"
                   "        self.exposure = 100\n"}}]}
    return {"content": "", "tool_calls": [
        {"name": "step_done", "arguments": {"summary": "reset"}}]}


def test_a_solved_task_is_scored_and_everything_is_cleaned_up(history, tmp_path):
    vault = tmp_path / "vault"
    project = pj.Project("cam", str(history),
                         test_command="python -m pytest -q -p no:cacheprovider")
    lines = []
    summary = cb.run(vault, project, label="t", chat=_plan_chat,
                     chat_tools=_solver, say=lines.append)
    assert summary["valid"] == 1 and summary["solved"] == 1, lines
    assert "council/" not in git(history, "branch")
    assert "def reset" in (history / "camera.py").read_text()   # untouched
    assert git(history, "status", "--porcelain") == ""


def test_an_unsolved_task_is_not_solved(history, tmp_path):
    vault = tmp_path / "vault"
    project = pj.Project("cam", str(history),
                         test_command="python -m pytest -q -p no:cacheprovider")

    def lazy(messages, tools, **kw):
        return {"content": "", "tool_calls": [
            {"name": "step_done", "arguments": {"summary": "nothing"}}]}
    summary = cb.run(vault, project, label="t", chat=_plan_chat,
                     chat_tools=lazy, say=lambda s: None)
    assert summary["valid"] == 1 and summary["solved"] == 0


def test_the_target_tests_are_not_excused_as_already_failing(history, tmp_path):
    """The task's tests fail at its start; a step that leaves them failing
    must not pass its check as 'only old failures'."""
    vault = tmp_path / "vault"
    project = pj.Project("cam", str(history),
                         test_command="python -m pytest -q -p no:cacheprovider")
    seen = []

    def lazy(messages, tools, **kw):
        seen.append(messages[-1].get("content", ""))
        return {"content": "", "tool_calls": [
            {"name": "step_done", "arguments": {"summary": "nothing"}}]}
    cb.run(vault, project, label="t", chat=_plan_chat, chat_tools=lazy,
           say=lambda s: None)
    assert any("THE CHECK FAILED" in str(m) for m in seen)
