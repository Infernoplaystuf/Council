"""
council_core.worktree — every coding job works in its own git worktree, on
its own branch, never in the user's folder.

    git worktree add -b council/<job> <project dir>/worktrees/<job> <base>

The agent edits, tests and commits there (one commit per step that passed),
so the result is an ordinary branch: `git log council/<job>` shows each
step, `diff()` shows the whole change against where it started. The user
then MERGES it (into the branch the project folder has checked out, and
only when that folder has no uncommitted changes) or DISCARDS it (worktree
and branch removed). Nothing touches the user's files until they merge.

The project folder must be a git repository; `require_repo` says so in
words when it is not. git runs as a child process with a time limit; the
commits are authored "Council <council@localhost>".
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

BRANCH_PREFIX = "council/"
GIT_TIMEOUT_S = 120
AUTHOR = ("Council", "council@localhost")


class WorktreeError(RuntimeError):
    pass


def _git(args: Sequence[str], cwd: Path, *, check: bool = True,
         timeout: int = GIT_TIMEOUT_S) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update({"GIT_AUTHOR_NAME": AUTHOR[0], "GIT_AUTHOR_EMAIL": AUTHOR[1],
                "GIT_COMMITTER_NAME": AUTHOR[0],
                "GIT_COMMITTER_EMAIL": AUTHOR[1],
                "GIT_TERMINAL_PROMPT": "0"})
    try:
        res = subprocess.run(["git", *args], cwd=str(cwd), env=env,
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace",
                             timeout=timeout)
    except FileNotFoundError as exc:
        raise WorktreeError("git is not installed on this PC") from exc
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError(f"git {' '.join(args)} took longer than "
                            f"{timeout} s") from exc
    if check and res.returncode != 0:
        raise WorktreeError(f"git {' '.join(args)}: "
                            f"{(res.stderr or res.stdout).strip()[:600]}")
    return res


def is_repo(path: Path) -> bool:
    try:
        return _git(["rev-parse", "--is-inside-work-tree"], Path(path),
                    check=False).stdout.strip() == "true"
    except WorktreeError:
        return False


def require_repo(path: Path) -> Path:
    """The repository's top folder, or WorktreeError in plain words."""
    path = Path(path)
    if not path.is_dir():
        raise WorktreeError(f"{path} is not a folder")
    if not is_repo(path):
        raise WorktreeError(
            f"{path} is not a git repository. The council works on a branch "
            "of its own and you merge it, so the project needs git: run "
            "`git init` and commit once in that folder.")
    return Path(_git(["rev-parse", "--show-toplevel"], path).stdout.strip())


def current_branch(repo: Path) -> str:
    return _git(["rev-parse", "--abbrev-ref", "HEAD"], repo).stdout.strip()


def is_clean(repo: Path) -> bool:
    return not _git(["status", "--porcelain"], repo).stdout.strip()


@dataclass
class Job:
    repo: Path          # the user's repository (top folder)
    path: Path          # the worktree
    branch: str
    base: str           # the commit the job started from

    def git(self, *args: str, check: bool = True):
        return _git(list(args), self.path, check=check)

    def commit(self, message: str) -> Optional[str]:
        """Commit everything changed in the worktree; the new commit's
        hash, or None when nothing changed."""
        self.git("add", "-A")
        if not self.git("status", "--porcelain").stdout.strip():
            return None
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def reset_uncommitted(self) -> None:
        """Throw away edits since the last commit (a step that failed)."""
        self.git("reset", "-q", "--hard")
        self.git("clean", "-q", "-fd")

    def changed_files(self) -> List[str]:
        out = self.git("diff", "--name-only", self.base, "HEAD").stdout
        return [l for l in out.splitlines() if l.strip()]

    def diff(self, uncommitted: bool = False) -> str:
        """The job's whole change against its start (committed only, or
        including what is not committed yet)."""
        if uncommitted:
            self.git("add", "-A")
            return self.git("diff", "--cached", self.base).stdout
        return self.git("diff", self.base, "HEAD").stdout

    def log(self) -> List[str]:
        out = self.git("log", "--format=%h %s", f"{self.base}..HEAD").stdout
        return [l for l in out.splitlines() if l.strip()]


def job_id(task: str = "") -> str:
    words = re.sub(r"[^a-z0-9]+", "-", (task or "").lower()).strip("-")
    return (time.strftime("%Y%m%d-%H%M%S") + "-" + words[:30].strip("-")
            + "-" + uuid.uuid4().hex[:4]).strip("-")


def create(repo: Path, where: Path, job: str, base: str = "HEAD") -> Job:
    """A new worktree for `job` at `where`/<job>, on branch council/<job>."""
    repo = require_repo(repo)
    commit = _git(["rev-parse", base], repo).stdout.strip()
    path = Path(where) / job
    path.parent.mkdir(parents=True, exist_ok=True)
    branch = BRANCH_PREFIX + job
    _git(["worktree", "add", "-q", "-b", branch, str(path), commit], repo)
    return Job(repo, path, branch, commit)


def open_job(repo: Path, where: Path, job: str, base: str) -> Job:
    """An existing job's worktree (after a restart); `base` is the commit
    it started from, as the job's record keeps it."""
    repo = require_repo(repo)
    path = Path(where) / job
    if not path.is_dir():
        raise WorktreeError(f"no worktree for job {job}")
    return Job(repo, path, BRANCH_PREFIX + job, base)


def discard(job: Job) -> None:
    """Remove the worktree and delete its branch."""
    _git(["worktree", "remove", "--force", str(job.path)], job.repo,
         check=False)
    if job.path.exists():
        shutil.rmtree(job.path, ignore_errors=True)
    _git(["worktree", "prune"], job.repo, check=False)
    _git(["branch", "-D", job.branch], job.repo, check=False)


def merge(job: Job, *, message: str = "") -> str:
    """Merge the job's branch into whatever the project folder has checked
    out. Refuses when that folder has uncommitted changes — a merge must
    never mix with the user's own unsaved work. Returns the merge result
    line."""
    if not is_clean(job.repo):
        raise WorktreeError(
            "The project folder has uncommitted changes. Commit or stash "
            "them first; the council's merge must not mix with your work.")
    if not job.log():
        raise WorktreeError("The job made no commits; nothing to merge.")
    msg = message or f"Merge {job.branch}"
    res = _git(["merge", "--no-ff", "-m", msg, job.branch], job.repo,
               check=False)
    if res.returncode != 0:
        _git(["merge", "--abort"], job.repo, check=False)
        raise WorktreeError("The merge conflicts with changes made since the "
                            "job started; nothing was changed. "
                            + (res.stdout or res.stderr).strip()[:400])
    return (res.stdout or "merged").strip().splitlines()[-1]


__all__ = ["Job", "WorktreeError", "create", "open_job", "discard", "merge",
           "require_repo", "is_repo", "is_clean", "current_branch", "job_id"]
