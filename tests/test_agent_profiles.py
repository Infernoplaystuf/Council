"""Agent profiles (council_core/agent_profiles.py)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from council_core import agent_profiles as ap  # noqa: E402
from council_core import code_agent as ca  # noqa: E402
from council_core import project as pj  # noqa: E402


def test_built_ins_and_user_profiles(tmp_path):
    ids = [p.id for p in ap.list_profiles(tmp_path)]
    assert {"developer", "qt-tab-builder", "reviewer"} <= set(ids)
    mine = ap.save(tmp_path, ap.AgentProfile(
        "", "Camera maintainer", "Owns camera tabs.",
        tools=["read_file", "edit_file", "rm_rf"], role="coder"))
    assert mine.id == "camera-maintainer" and "rm_rf" not in mine.tools
    assert ap.get(tmp_path, "camera-maintainer").instructions == \
        "Owns camera tabs."
    # Saving over a built-in makes a copy; the built-in stays.
    copy = ap.save(tmp_path, ap.get(tmp_path, "developer"))
    assert copy.id == "developer-custom"
    assert ap.get(tmp_path, "developer").built_in


def test_a_report_profile_cannot_edit(tmp_path):
    p = ap.save(tmp_path, ap.AgentProfile("", "Looker", tools=list(
        ap.ALL_TOOLS), output="report"))
    assert "edit_file" not in p.tools and "create_file" not in p.tools


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=True).stdout


@pytest.fixture
def proj(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "u@x")
    git(repo, "config", "user.name", "u")
    (repo / "a.py").write_text("def f():\n    return 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "start")
    return tmp_path / "vault", pj.Project("p", str(repo))


def test_a_report_agent_reads_and_reports_without_changing(proj):
    vault, project = proj
    offered = []

    def chat_tools(messages, tools, **kw):
        offered.append([t["name"] for t in tools])
        if len(offered) == 1:
            return {"content": "", "tool_calls": [
                {"name": "read_file", "arguments": {"path": "a.py"}},
                {"name": "edit_file", "arguments": {"path": "a.py",
                                                    "old": "1", "new": "2"}}]}
        return {"content": "a.py:2 returns a constant.", "tool_calls": []}
    report = ap.run_report(vault, project, ap.get(vault, "reviewer"),
                           "review a.py", chat_tools=chat_tools)
    assert report == "a.py:2 returns a constant."
    assert "edit_file" not in offered[0]
    assert (Path(project.root) / "a.py").read_text() == "def f():\n    return 1\n"


def test_a_patch_profile_limits_the_coders_tools(proj):
    vault, project = proj
    plan = ca.plan_from_json({"goal": "g", "steps": [
        {"title": "s", "files": [], "check": "none", "details": ""}]})
    rec = ca.new_job(vault, project, "t", plan)
    seen = {}

    def chat_tools(messages, tools, **kw):
        seen["tools"] = [t["name"] for t in tools]
        seen["system"] = messages[0]["content"]
        return {"content": "", "tool_calls": [
            {"name": "step_done", "arguments": {"summary": "ok"}}]}
    ca.run_job(vault, project, rec, chat_tools=chat_tools,
               chat=lambda m, **k: "NONE",
               tool_names=["read_file", "search_code"],
               extra_system="Only read.")
    assert set(seen["tools"]) == {"read_file", "search_code", "step_done"}
    assert "YOUR ROLE ON THIS PROJECT:\nOnly read." in seen["system"]
