"""
council_core.changelog — the repo's own history, read out of git.

WHAT THIS TAB ACTUALLY IS
There is no changelog file and nothing in the app writes entries. The entries
ARE the repository's commits: `git log` into a list, `git show` into a detail
pane. That is worth stating because "Changelog" reads like a curated document
and it is not one.

AND IT IS BROKEN IN EVERY INSTALLED BUILD
`.git` is not bundled by council.spec, so a user who installs the app gets a
tab that can never show anything. The Tk version reports whatever git prints to
stderr, which for a missing repo is a sentence about ownership and safe
directories that means nothing to the person reading it.

So `history()` names the situation instead: this build has no repository, which
is expected in an installed copy. A tab that says "not available in this build"
is honest; one that shows a git plumbing error is not.

TWO PERFORMANCE DEFECTS, BOTH FROM THE SAME HABIT
Filtering re-selects row 0 and immediately runs `git show --stat --patch` for
it — synchronously, on the GUI thread. So EVERY KEYSTROKE in the filter box
spawns a subprocess. Filtering is done here against an already-loaded list,
and fetching a commit's detail is a separate call the caller makes on a worker
when a selection settles.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

#: How many commits the list holds. The Tk version's number.
LOG_LIMIT = 100

#: Long enough for a big commit, short enough that a wedged git does not hang
#: the tab forever.
GIT_TIMEOUT = 20

NO_REPO = ("This build has no git repository, so there is no history to show. "
           "That is expected in an installed copy — the changelog is the "
           "repository's own commits, and .git is not bundled.")


@dataclass(frozen=True)
class Commit:
    """One row. `sha` is the address; `label` is for a human to read."""
    sha: str
    label: str
    subject: str = ""

    def __str__(self) -> str:
        return self.label


@dataclass
class HistoryResult:
    ok: bool
    message: str
    commits: List[Commit] = field(default_factory=list)
    error: Optional[BaseException] = None


def _git(args: Sequence[str], repo: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True,
        timeout=GIT_TIMEOUT,
        encoding="utf-8", errors="replace")


def is_repo(repo: Path) -> bool:
    """Whether there is a repository here at all.

    Checked by looking rather than by running git and reading the error, so an
    installed build gets a plain sentence instead of git's plumbing message
    about ownership and safe directories.
    """
    return (Path(repo) / ".git").exists()


def history(repo: Path, *, limit: int = LOG_LIMIT) -> HistoryResult:
    """The last ``limit`` commits, newest first. Blocking — use a worker."""
    repo = Path(repo)
    if not is_repo(repo):
        return HistoryResult(False, NO_REPO)
    try:
        done = _git(["log", f"-{int(limit)}",
                     "--date=short", "--format=%h\x1f%ad\x1f%s"], repo)
    except FileNotFoundError:
        return HistoryResult(False, "git is not installed, so there is no "
                                    "history to read.")
    except subprocess.TimeoutExpired as exc:
        return HistoryResult(False, "git did not answer in time.", error=exc)
    except Exception as exc:                              # noqa: BLE001
        return HistoryResult(False, f"Could not read the history: {exc!r}",
                             error=exc)

    if done.returncode != 0:
        return HistoryResult(False,
                             (done.stderr or "git failed.").strip()[:300])

    commits = []
    for line in done.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3:
            continue
        sha, date, subject = parts
        commits.append(Commit(sha=sha, label=f"{date}  {sha}  {subject}",
                              subject=subject))
    return HistoryResult(
        True,
        f"{len(commits)} commit(s)." if commits else "No commits yet.",
        commits=commits)


def detail(repo: Path, sha: str) -> str:
    """One commit's stat and patch. Blocking — use a worker.

    The Tk version runs this on the GUI thread on every selection change AND
    on every keystroke in the filter, because filtering re-selects row 0.
    """
    if not sha:
        return ""
    repo = Path(repo)
    if not is_repo(repo):
        return NO_REPO
    try:
        done = _git(["show", "--stat", "--patch", sha], repo)
    except Exception as exc:                              # noqa: BLE001
        return f"Could not read {sha}: {exc!r}"
    return done.stdout or (done.stderr or "").strip()


def filter_commits(commits: Sequence[Commit], term: str) -> List[Commit]:
    """Narrow an already-loaded list. No subprocess, no disk, no git.

    This is the whole fix for the per-keystroke `git show`: filtering is a
    string test over what is already in memory.
    """
    term = (term or "").strip().lower()
    if not term:
        return list(commits)
    return [c for c in commits if term in c.label.lower()]
