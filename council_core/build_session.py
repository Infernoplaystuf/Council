"""
council_core.build_session — a Build tab session: the model's accepted edits
on a branch of their own, one commit per turn, Undo turn, a grouped review,
then Merge.

    Start session   git worktree add -b council/build-<id>
                    <vault>/.council_build/sessions/<id>  HEAD      (council_core.worktree)
    each turn       the user's own edits since the last turn are committed
                    first ("Your edits"), then the accepted edits are
                    committed as "Turn N: <the request>", so Undo turn
                    reverts exactly the model's change.
    Undo turn       git revert of the newest turn not yet undone (a new commit,
                    so later commits are kept; a conflict is aborted and said).
    Review          the whole session against where it started, grouped by
                    file and by the function or class each change is in, one
                    plain line per group (what changed, and the model's "why"
                    lines from the turns that touched it), lint findings first.
    Merge           worktree.merge into what the project folder has checked
                    out, refused over uncommitted work there; then the worktree
                    and branch are removed.
    End session     the worktree and branch are removed without merging.

The project folder is only read until Merge. The session's record (its
repository, base commit and turns) is <vault>/.council_build/sessions/<id>.json,
written atomically, so a session survives a restart.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import worktree as wt
from .build_edits import enclosing

#: A dot-folder of the vault, so its search never indexes the worktrees.
SESSIONS = ".council_build/sessions"


@dataclass
class EditRecord:
    path: str
    line: int                    # where it starts in the file after the turn
    where: str                   # the def or class it is in ("" = top level)
    reason: str
    added: int
    removed: int


@dataclass
class TurnRecord:
    number: int
    request: str
    commit: str
    time: float = field(default_factory=time.time)
    edits: List[EditRecord] = field(default_factory=list)
    undone: bool = False


def sessions_dir(vault_dir: Path) -> Path:
    return Path(vault_dir) / SESSIONS


class Session:
    """One session: its worktree, and the turns committed on it."""

    def __init__(self, vault_dir: Path, sid: str, repo: Path, base: str,
                 folder: str = "", turns: Optional[List[TurnRecord]] = None):
        self.vault_dir = Path(vault_dir)
        self.id = sid
        self.job = wt.Job(Path(repo), sessions_dir(vault_dir) / sid,
                          wt.BRANCH_PREFIX + sid, base)
        self.folder = folder or str(repo)       # the folder the user opened
        self.turns: List[TurnRecord] = turns or []

    # -- where things are -------------------------------------------------
    @property
    def repo(self) -> Path:
        return self.job.repo

    @property
    def tree(self) -> Path:
        return self.job.path

    @property
    def branch(self) -> str:
        return self.job.branch

    def record_path(self) -> Path:
        return sessions_dir(self.vault_dir) / f"{self.id}.json"

    def tree_folder(self) -> Path:
        """The opened folder's place inside the worktree (the user may have
        opened a subfolder of the repository)."""
        try:
            rel = Path(self.folder).resolve().relative_to(self.repo.resolve())
        except ValueError:
            rel = Path()
        return self.tree / rel

    # -- start, reopen, save ---------------------------------------------
    @classmethod
    def start(cls, vault_dir: Path, folder: Path) -> "Session":
        """A new session on the repository ``folder`` is in. WorktreeError
        (in words) when it is not a repository or git is missing."""
        top = wt.require_repo(Path(folder))
        sid = "build-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        job = wt.create(top, sessions_dir(vault_dir), sid)
        s = cls(vault_dir, sid, top, job.base, str(folder))
        s.save()
        return s

    @classmethod
    def load(cls, vault_dir: Path, sid: str) -> Optional["Session"]:
        """The session ``sid`` as saved, or None when its record or its
        worktree is gone."""
        path = sessions_dir(vault_dir) / f"{sid}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            turns = [TurnRecord(**{**t, "edits": [EditRecord(**e) for e in t.get("edits", [])]})
                     for t in data.get("turns", [])]
            s = cls(vault_dir, sid, Path(data["repo"]), data["base"],
                    data.get("folder", ""), turns)
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return s if s.tree.is_dir() else None

    def save(self) -> None:
        path = self.record_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"id": self.id, "repo": str(self.repo), "base": self.job.base,
                "folder": self.folder, "branch": self.branch,
                "turns": [asdict(t) for t in self.turns]}
        fd, tmp = tempfile.mkstemp(prefix=".session.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- turns -------------------------------------------------------------
    def commit_own_edits(self) -> Optional[str]:
        """Commit what the user changed by hand since the last commit, so
        the next turn's commit holds only the model's edits."""
        return self.job.commit("Your edits")

    def commit_turn(self, request: str, edits: List[EditRecord]) -> Optional[TurnRecord]:
        """Commit the accepted edits (already written into the worktree) as
        one turn; None when they changed nothing."""
        number = 1 + max((t.number for t in self.turns), default=0)
        title = " ".join((request or "").split())[:72] or "edits"
        commit = self.job.commit(f"Turn {number}: {title}")
        if commit is None:
            return None
        turn = TurnRecord(number, request, commit, edits=edits)
        self.turns.append(turn)
        self.save()
        return turn

    def last_turn(self) -> Optional[TurnRecord]:
        return next((t for t in reversed(self.turns) if not t.undone), None)

    def undo_turn(self) -> TurnRecord:
        """Revert the newest turn not undone yet. The user's own uncommitted
        edits are committed first, so they are kept."""
        turn = self.last_turn()
        if turn is None:
            raise wt.WorktreeError("No turn to undo.")
        self.commit_own_edits()
        res = self.job.git("revert", "--no-edit", turn.commit, check=False)
        if res.returncode != 0:
            self.job.git("revert", "--abort", check=False)
            raise wt.WorktreeError(
                f"Turn {turn.number} cannot be undone on its own: later edits "
                "changed the same lines. Nothing was changed.")
        turn.undone = True
        self.save()
        return turn

    # -- the review --------------------------------------------------------
    def review(self) -> List["Group"]:
        self.commit_own_edits()
        return review_groups(self)

    def merge(self) -> str:
        """Merge into the project folder (refused over its uncommitted
        work), then end the session."""
        self.commit_own_edits()
        made = [t for t in self.turns if not t.undone]
        msg = (f"Merge {self.branch}: " + "; ".join(t.request.split("\n")[0][:50]
                                                     for t in made[:5]))[:200]
        line = wt.merge(self.job, message=msg)
        self.end()
        return line

    def end(self) -> None:
        """Remove the worktree and branch (nothing is merged)."""
        wt.discard(self.job)
        try:
            self.record_path().unlink()
        except OSError:
            pass


def edit_record(path: str, after: str, start_line: int, new_text: str,
                old_text: str, reason: str) -> EditRecord:
    """What one accepted edit did, for the turn's record."""
    added = len(new_text.split("\n")) if new_text else 0
    removed = len(old_text.split("\n")) if old_text else 0
    where = enclosing(after, start_line) if path.endswith(".py") else ""
    return EditRecord(path, start_line, where, reason, added, removed)


# ── the grouped review ──────────────────────────────────────────────────
@dataclass
class Group:
    path: str
    where: str                   # def or class, "" = top level
    added: int = 0
    removed: int = 0
    diff: str = ""
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    status: str = "changed"      # changed | new | deleted

    def title(self) -> str:
        return f"{self.path} · {self.where}" if self.where else f"{self.path} · top level"

    def summary(self) -> str:
        """One plain line: what changed, and why."""
        size = f"+{self.added} −{self.removed} lines"
        what = {"new": "New file", "deleted": "Deleted"}.get(self.status, "Changed")
        place = f" in `{self.where}`" if self.where else ""
        why = "; ".join(dict.fromkeys(r for r in self.reasons if r))
        return f"{what}{place} ({size})" + (f": {why}" if why else ".")


#: Lines of the file shown around each change in the review.
CONTEXT = 2
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def review_groups(session: Session) -> List[Group]:
    """The session's whole change grouped by file and enclosing def/class,
    groups with lint findings first, then in file order."""
    job = session.job
    names = job.git("diff", "--name-status", job.base, "HEAD").stdout
    status: Dict[str, str] = {}
    for ln in names.splitlines():
        parts = ln.split("\t")
        if len(parts) >= 2:
            status[parts[-1]] = {"A": "new", "D": "deleted"}.get(parts[0][:1], "changed")
    reasons: Dict[tuple, List[str]] = {}
    for t in session.turns:
        if t.undone:
            continue
        for e in t.edits:
            reasons.setdefault((e.path, e.where), []).append(e.reason or t.request.split("\n")[0][:80])
            reasons.setdefault((e.path, None), []).append(e.reason or t.request.split("\n")[0][:80])
    groups: List[Group] = []
    from .tool_kit import lint
    for path, st in status.items():
        after = ""
        if st != "deleted":
            try:
                after = (job.path / path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                after = ""
        # -U0 so two changes a few lines apart stay two (each in its own
        # def); the context shown around each is added from the file
        diff = job.git("diff", "-U0", job.base, "HEAD", "--", path).stdout
        head, hunks = _split_hunks(diff)
        after_lines = after.split("\n")
        by_where: Dict[str, Group] = {}
        for h in hunks:
            m = _HUNK.match(h)
            new_start = int(m.group(3)) if m else 1
            new_count = int(m.group(4)) if m and m.group(4) is not None else 1
            body = h.split("\n")[1:]
            # a pure deletion's new_start is the line before it
            first_changed = new_start if new_count else new_start + 1
            where = enclosing(after, max(1, first_changed)) \
                if path.endswith(".py") and after else ""
            g = by_where.get(where)
            if g is None:
                g = Group(path, where, status=st)
                g.reasons = list(dict.fromkeys(reasons.get((path, where)) or
                                               reasons.get((path, None)) or []))
                by_where[where] = g
            g.added += sum(1 for ln in body if ln.startswith("+"))
            g.removed += sum(1 for ln in body if ln.startswith("-"))
            top = new_start if new_count else new_start + 1
            last = top + new_count - 1
            end = len(after_lines) - (1 if after_lines and after_lines[-1] == "" else 0)
            before_ctx = [" " + after_lines[i - 1]
                          for i in range(max(1, top - CONTEXT), min(top, end + 1))]
            after_ctx = [" " + after_lines[i - 1]
                         for i in range(last + 1, min(end, last + CONTEXT) + 1)]
            shown = "\n".join([h.split("\n")[0]] + before_ctx + body + after_ctx).rstrip("\n")
            g.diff += ("\n" if g.diff else "") + shown
        if not hunks:                   # a binary or empty file
            by_where[""] = Group(path, "", status=st)
        found = lint(after, path) if path.endswith(".py") and after else []
        for g in by_where.values():
            g.warnings = list(found) if found else []
        if found:                       # the file's findings, on its first group
            for g in list(by_where.values())[1:]:
                g.warnings = []
        groups.extend(by_where.values())
    groups.sort(key=lambda g: (not g.warnings,))
    return groups


def review_markdown(groups: List[Group]) -> str:
    """The review as Markdown: a line of totals, then each group (those with
    lint findings first) with its plain line, its findings and its diff, and
    an "Ask about this" link (ask:<n>)."""
    if not groups:
        return "This session has no changes to merge."
    files = len({g.path for g in groups})
    warned = sum(1 for g in groups if g.warnings)
    head = (f"**{len(groups)} change{'s' if len(groups) != 1 else ''} in {files} "
            f"file{'s' if files != 1 else ''}**")
    if warned:
        head += f" · {warned} with warnings, shown first"
    out = [head]
    for n, g in enumerate(groups):
        part = [f"### {g.title()}", g.summary()]
        for w in g.warnings[:8]:
            part.append(f"- ⚠ {w}")
        if g.diff:
            part.append(f"```diff\n{g.diff}\n```")
        part.append(f"[Ask about this](ask:{n})")
        out.append("\n\n".join(part))
    return "\n\n---\n\n".join(out)


def _split_hunks(diff: str):
    head, hunks, cur = [], [], None
    for ln in diff.split("\n"):
        if ln.startswith("@@"):
            if cur is not None:
                hunks.append("\n".join(cur))
            cur = [ln]
        elif cur is None:
            head.append(ln)
        else:
            cur.append(ln)
    if cur is not None:
        hunks.append("\n".join(cur).rstrip("\n"))
    return head, hunks


def is_repo(folder: Path) -> bool:
    return wt.is_repo(Path(folder))


__all__ = ["Session", "TurnRecord", "EditRecord", "Group", "edit_record",
           "review_groups", "review_markdown", "sessions_dir", "is_repo"]
