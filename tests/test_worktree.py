"""Coding jobs on their own git branch (council_core/worktree.py)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from council_core import worktree as wt  # noqa: E402


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "u@x")
    git(r, "config", "user.name", "u")
    (r / "a.py").write_text("x = 1\n")
    git(r, "add", ".")
    git(r, "commit", "-q", "-m", "start")
    return r


def test_a_job_works_on_its_own_branch(repo, tmp_path):
    job = wt.create(repo, tmp_path / "wts", wt.job_id("add y"))
    (job.path / "a.py").write_text("x = 1\ny = 2\n")
    assert (repo / "a.py").read_text() == "x = 1\n"       # user's untouched
    sha = job.commit("step 1: add y")
    assert sha and job.changed_files() == ["a.py"]
    assert "+y = 2" in job.diff()
    assert job.log()[0].endswith("step 1: add y")
    assert job.commit("nothing") is None


def test_a_failed_step_is_thrown_away(repo, tmp_path):
    job = wt.create(repo, tmp_path / "wts", "j1")
    (job.path / "a.py").write_text("broken(\n")
    (job.path / "new.py").write_text("junk")
    job.reset_uncommitted()
    assert (job.path / "a.py").read_text() == "x = 1\n"
    assert not (job.path / "new.py").exists()


def test_merge_and_discard(repo, tmp_path):
    job = wt.create(repo, tmp_path / "wts", "j2")
    (job.path / "b.py").write_text("b = 1\n")
    job.commit("add b")
    wt.merge(job)
    assert (repo / "b.py").read_text() == "b = 1\n"
    wt.discard(job)
    assert not job.path.exists()
    assert "council/j2" not in git(repo, "branch")


def test_merge_refuses_over_uncommitted_work(repo, tmp_path):
    job = wt.create(repo, tmp_path / "wts", "j3")
    (job.path / "b.py").write_text("b = 1\n")
    job.commit("add b")
    (repo / "a.py").write_text("x = 99\n")                # user's own edit
    with pytest.raises(wt.WorktreeError, match="uncommitted"):
        wt.merge(job)
    assert (repo / "a.py").read_text() == "x = 99\n"


def test_a_conflicting_merge_changes_nothing(repo, tmp_path):
    job = wt.create(repo, tmp_path / "wts", "j4")
    (job.path / "a.py").write_text("x = 2\n")
    job.commit("job edit")
    (repo / "a.py").write_text("x = 3\n")
    git(repo, "commit", "-qam", "user edit")
    with pytest.raises(wt.WorktreeError, match="conflicts"):
        wt.merge(job)
    assert (repo / "a.py").read_text() == "x = 3\n"
    assert wt.is_clean(repo)


def test_not_a_repository_is_said_plainly(tmp_path):
    with pytest.raises(wt.WorktreeError, match="git init"):
        wt.require_repo(tmp_path)
