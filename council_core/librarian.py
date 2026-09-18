"""
council_core.librarian — the vault root: what is in it, and committing it.

A small tab: list the loose files at the vault root, preview one, commit the
whole vault to git, open the folder. Four operations, and three of them are
wrong in the Tk build.

A NAME IS NOT A KEY
`Librarian.list_items()` returns `p.name` verbatim, and `read_text(name)` looks
the file up as `vault_dir / safe_name(name)` — which rewrites every run of
characters outside [A-Za-z0-9._-] to "_". So the list shows "Q3 notes.md",
Preview asks for "Q3 notes.md", read_text looks for "Q3_notes.md", and the user
gets "Vault item not found" for a file they can see on the same screen. Every
vault file with a space in its name is unopenable.

`entries()` returns real Paths, so the name never makes that round trip. This
is the same defect class the chart overlay, the specialist pin, the job queue
and the session list all had: AN IDENTIFIER RECOVERED FROM DISPLAY TEXT.

ONE RULE FOR WHAT THE LIBRARIAN SHOWS
Tk's rule is `p.is_file() and p.name != ".gitignore"` — dotted files included,
directories excluded. The Qt Vault tab's `walk()` skips every dotted entry and
includes directories. Two front ends disagreeing about what the vault contains
is exactly what this package exists to prevent, so the rule is written once.

"NOTHING TO COMMIT" IS NOT A FAILURE
`git commit` exits 1 on a clean tree and prints "nothing to commit, working
tree clean". The Tk code treats any non-zero return as a failure, so pressing
Commit twice in a row reports FAIL for a repository that is perfectly fine.

EMBEDDED REPOSITORIES ARE REPORTED, NOT SILENTLY SWALLOWED
The vault holds `.git_clones/<name>/` — full clones with their own `.git` —
put there by `vault_ops.clone_repo`. `git add -A` records those as gitlinks:
a pointer, not the files. The commit succeeds and the cloned material it looked
like it was backing up is not in it. This does not write a `.gitignore` into
the user's vault to "fix" that — their data is theirs — it says which
directories went in as links so the claim on screen is true.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from council_core.changelog import GIT_TIMEOUT, _git, is_repo

#: How much of a file Preview reads. The vault root holds append-only logs —
#: `verdict_history.jsonl` among them — that every other reader truncates
#: deliberately. Loading one whole into a text widget is a frozen window.
PREVIEW_BYTES = 64_000

#: Not shown: git's own ignore file is machinery, not a vault item.
HIDDEN = (".gitignore",)


def entries(vault_dir: Path) -> List[Path]:
    """The loose files at the vault root, sorted, as real Paths.

    Files only — a directory in this list would have no preview and no size —
    and dotted files ARE included, because `personality_backends.json` and its
    neighbours are exactly what someone opens this tab to look at.
    """
    vault_dir = Path(vault_dir)
    try:
        found = [p for p in vault_dir.iterdir()
                 if p.is_file() and p.name not in HIDDEN]
    except OSError:
        # A vault that is not there yet is empty, not an error: the tab opens
        # before the first run has created anything.
        return []
    return sorted(found, key=lambda p: p.name.lower())


def preview(path: Path, limit: int = PREVIEW_BYTES) -> str:
    """The first `limit` bytes of a file, decoded, with a note if truncated.

    Capped because the caller is a text widget on the GUI thread. `read_text()`
    with no limit is how a 1 MB JSON index freezes the window.
    """
    path = Path(path)
    try:
        with path.open("rb") as handle:
            raw = handle.read(limit + 1)
    except OSError as exc:
        return f"Could not read {path.name}: {exc}"
    truncated = len(raw) > limit
    text = raw[:limit].decode("utf-8", errors="replace")
    if truncated:
        text += (f"\n\n… truncated at {limit:,} bytes. "
                 f"Open the file itself to see the rest.")
    return text


def embedded_repos(vault_dir: Path) -> List[Path]:
    """Directories under the vault that are their own git repositories.

    `git add -A` records these as GITLINKS — a commit pointer, not the files —
    so a commit that looks like it backed them up did not. Reported rather
    than worked around: what to do about it is the user's call.
    """
    vault_dir = Path(vault_dir)
    found: List[Path] = []
    try:
        # `*/**/.git` needs at least one directory level, so the vault's OWN
        # `.git` is excluded by the pattern. A `repo != vault_dir` guard on top
        # would be dead code — mutation showed it could not be made to fail.
        for git_dir in vault_dir.glob("*/**/.git"):
            found.append(git_dir.parent)
    except OSError:
        return []
    return sorted(set(found))


@dataclass
class CommitResult:
    ok: bool
    message: str = ""
    #: True when git refused because there was nothing to commit. That is a
    #: SUCCESS — the vault is already saved — and reporting it as a failure is
    #: the Tk behaviour this replaces.
    nothing_to_commit: bool = False
    #: Repositories inside the vault that went in as pointers, not files.
    linked_repos: List[Path] = field(default_factory=list)


#: git's wording when the tree is clean. Matched on the phrase rather than the
#: exit code, because the exit code is 1 either way — the same 1 a real error
#: gets.
CLEAN_TREE = "nothing to commit"


def commit_all(vault_dir: Path, message: str) -> CommitResult:
    """Commit the whole vault. Blocking — call it from a worker.

    Three git calls, every one of them with a timeout. The Tk version has none
    on any of the three, and `git add -A` over a live vault walks `.chromadb/`,
    `conversation_logs/` and every cloned reference repo — so "no timeout"
    means the window is gone for as long as that takes.
    """
    vault_dir = Path(vault_dir)
    if not message.strip():
        return CommitResult(False, "Give the commit a message.")

    started, problem = _ensure_repo(vault_dir)
    if not started:
        return CommitResult(False, problem)

    try:
        added = _git(["add", "-A"], vault_dir)
        if added.returncode != 0:
            # `git add -A` FAILS OUTRIGHT, not silently, when the vault holds a
            # repository with no commits yet: "does not have a commit checked
            # out / fatal: adding files failed". Naming the directories is the
            # difference between that message and an actionable one.
            return CommitResult(False, _said(added) or "git add failed.",
                                linked_repos=embedded_repos(vault_dir))
        done = _git(["commit", "-m", message], vault_dir)
    except subprocess.TimeoutExpired:
        return CommitResult(
            False, f"git took longer than {GIT_TIMEOUT}s and was stopped. "
                   f"A vault with a large .chromadb or many cloned repos in it "
                   f"can exceed this.")
    except FileNotFoundError:
        return CommitResult(False, "git is not installed, so the vault "
                                   "cannot be committed.")

    linked = embedded_repos(vault_dir)
    if done.returncode != 0:
        said = _said(done)
        if CLEAN_TREE in said.lower():
            return CommitResult(True, "Nothing to commit — the vault is "
                                      "already saved.",
                                nothing_to_commit=True, linked_repos=linked)
        return CommitResult(False, said or "git commit failed.",
                            linked_repos=linked)
    return CommitResult(True, said_or(done, "commit OK."), linked_repos=linked)


def _ensure_repo(vault_dir: Path) -> tuple:
    """(ready, why not). Creates the repository if there is not one."""
    if is_repo(vault_dir):
        return True, ""
    try:
        done = _git(["init"], vault_dir)
    except subprocess.TimeoutExpired:
        return False, f"git init took longer than {GIT_TIMEOUT}s."
    except FileNotFoundError:
        return False, "git is not installed, so the vault cannot be committed."
    except OSError as exc:
        return False, f"could not start git: {exc}"
    if done.returncode != 0:
        return False, _said(done) or "git init failed."
    return True, ""


def _said(done: subprocess.CompletedProcess) -> str:
    """Everything git printed, both streams.

    Both, because `git commit` puts "nothing to commit" on STDOUT while real
    errors go to stderr — reading only one of them loses half the cases.
    """
    return "\n".join(part.strip() for part in
                     ((done.stdout or ""), (done.stderr or "")) if part.strip())


def said_or(done: subprocess.CompletedProcess, fallback: str) -> str:
    return _said(done) or fallback


def describe(result: CommitResult) -> str:
    """The line the transcript shows.

    The commit output is carried through on SUCCESS too, not only on failure:
    it holds the SHA and the files-changed count, which is the whole receipt.
    """
    if not result.linked_repos:
        return result.message
    names = ", ".join(p.name for p in result.linked_repos)
    if result.ok:
        note = (f"Note: {names} are git repositories of their own, so they "
                f"were recorded as links rather than as files. Their contents "
                f"are NOT in this commit.")
    else:
        # git's own wording is "does not have a commit checked out", which
        # says nothing about what to do. The vault holds these because
        # vault_ops.clone_repo put them there.
        note = (f"The vault contains git repositories of its own: {names}. "
                f"git will not add a repository that has no commits yet, "
                f"which is what stopped this.")
    return "\n".join(line for line in (result.message, note) if line)
