"""
council_core.vault_health — what is in the vault, and how big.

A read-only dashboard: the per-personality memory files, the top level of the
vault directory, and a short summary of the wishlist and project context. All
of it is disk reads, which is exactly why it belongs here — extracting it is
what makes it legal to run on a worker thread, and the Tk version does every
stat() and read_text() on the UI thread including once during startup.

THE PANEL LISTS WHAT IS ON DISK, NOT WHAT A CONSTANT SAYS
The Tk tab iterates a hardcoded tuple of sixteen role names. Three roles that
own memory files are missing from it — coach, ideator and pitcher — and so is
`_user_profile`, the one file holding durable user preferences. A dashboard
whose entire job is showing memory files does not show them.

`RoleMemoryManager.all_roles()` already globs `memory_*.md`. Listing what is
there cannot go stale, which a constant re-derived from `MEMORY_WRITE_ROLES`
still could — a role that writes memory without being in that tuple would
vanish again.

ONE BAD FILE DOES NOT TRUNCATE THE PANEL
Each Tk loop is wrapped in a single broad `except Exception: pass` spanning the
whole loop, so the first unreadable entry silently ends the list. On Windows
`datetime.fromtimestamp()` raises OSError for a negative POSIX timestamp — one
file with a bad mtime and the rest of the vault is simply not shown. Here each
entry is read on its own and a broken one is reported in place.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

#: Files this size or larger are what the Tk formatter reports as megabytes.
GB = 1024 ** 3

#: What a row shows when the file could not be read.
UNREADABLE = "unreadable"


def human_size(size: float) -> str:
    """A byte count in the largest unit that fits.

    THE TK FORMATTER STOPS AT MB. Its loop divides three times and appends
    "MB", so 5 GiB renders as "5MB" — and the vault root is exactly where
    multi-gigabyte CSVs live. This is the Qt Vault tab's formatter, promoted
    so both tabs share one.
    """
    size = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} GB"


@dataclass(frozen=True)
class Entry:
    """One row: a file, its size and when it changed, already in words."""
    label: str
    path: Optional[Path]
    size: str
    modified: str
    #: Set when this entry could not be read. The row still appears — a
    #: missing row is indistinguishable from a file that is not there.
    problem: str = ""


def _describe(path: Path, label: str = "") -> Entry:
    """One entry, never raising.

    Read per file rather than per loop: the Tk try spans the whole loop, so
    the first failure ends the list and the user sees a short vault rather
    than a broken one.
    """
    label = label or path.name
    try:
        stat = path.stat()
    except OSError as exc:
        return Entry(label, path, UNREADABLE, UNREADABLE, str(exc))
    try:
        when = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError) as exc:
        # Windows raises OSError for a negative POSIX timestamp, and one file
        # with a bad mtime truncates the whole Tk panel.
        when = UNREADABLE
        return Entry(label, path, human_size(stat.st_size), when, str(exc))
    return Entry(label, path, human_size(stat.st_size), when)


def memory_files(mem_dir: Path) -> List[Entry]:
    """Every personality memory file that exists, whatever its role is called.

    Globbed rather than looked up from a list of roles, so a role that starts
    writing memory shows up without anyone remembering to add it here.
    """
    mem_dir = Path(mem_dir)
    try:
        found = sorted(mem_dir.glob("memory_*.md"))
    except OSError:
        return []
    return [_describe(path, path.stem.replace("memory_", "", 1))
            for path in found]


def vault_files(vault_dir: Path) -> List[Entry]:
    """The top level of the vault: files and directories, biggest concerns
    first by name."""
    vault_dir = Path(vault_dir)
    try:
        found = sorted(vault_dir.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    rows: List[Entry] = []
    for path in found:
        try:
            is_dir = path.is_dir()
        except OSError:
            is_dir = False
        entry = _describe(path)
        rows.append(Entry(f"{entry.label}/" if is_dir else entry.label,
                          entry.path,
                          "—" if is_dir else entry.size,
                          entry.modified, entry.problem))
    return rows


@dataclass
class Summary:
    """The text panel: what is in the wishlist and the project memory."""
    wishlist_path: Optional[Path] = None
    wishlist_total: int = 0
    wishlist_pending: int = 0
    wishlist_filled: int = 0
    project_exists: bool = False
    project_size: str = "0 B"
    project_lines: int = 0
    trends_modified: str = ""
    notes: List[str] = field(default_factory=list)

    def lines(self) -> List[str]:
        out = [
            f"Wishlist: {self.wishlist_total} item(s) — "
            f"{self.wishlist_pending} pending, {self.wishlist_filled} filled",
        ]
        if self.project_exists:
            out.append(f"Project context: {self.project_lines} line(s), "
                       f"{self.project_size}")
        else:
            out.append("Project context: none yet")
        if self.trends_modified:
            out.append(f"Trends last written: {self.trends_modified}")
        out.extend(self.notes)
        return out


def wishlist_counts(text: str) -> Tuple[int, int, int]:
    """(total, pending, filled) from the wishlist file's text.

    A filled item is one whose checkbox is ticked. Anything that is not a
    checkbox line is not an item — a heading is not a wish.
    """
    total = pending = filled = 0
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("- ["):
            continue
        total += 1
        if stripped[3:4].strip().lower() == "x":
            filled += 1
        else:
            pending += 1
    return total, pending, filled


def summarise(vault_dir: Path, mem_dir: Path, *,
              wishlist_name: str = "wishlist.md",
              project_key: str = "_project",
              trends_name: str = "trends.md") -> Summary:
    """The text panel, gathered. Blocking — call it from a worker."""
    vault_dir, mem_dir = Path(vault_dir), Path(mem_dir)
    summary = Summary()

    wishlist = vault_dir / wishlist_name
    summary.wishlist_path = wishlist
    try:
        if wishlist.exists():
            counts = wishlist_counts(
                wishlist.read_text(encoding="utf-8", errors="replace"))
            (summary.wishlist_total, summary.wishlist_pending,
             summary.wishlist_filled) = counts
    except OSError as exc:
        summary.notes.append(f"Wishlist could not be read: {exc}")

    project = mem_dir / f"memory_{project_key}.md"
    try:
        if project.exists():
            text = project.read_text(encoding="utf-8", errors="replace")
            summary.project_exists = True
            summary.project_lines = len(text.splitlines())
            summary.project_size = human_size(project.stat().st_size)
    except OSError as exc:
        summary.notes.append(f"Project context could not be read: {exc}")

    trends = vault_dir / trends_name
    try:
        if trends.exists():
            summary.trends_modified = datetime.fromtimestamp(
                trends.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        summary.trends_modified = ""
    return summary


@dataclass
class Report:
    """Everything the dashboard shows, gathered in one pass off the UI thread."""
    memory: List[Entry] = field(default_factory=list)
    vault: List[Entry] = field(default_factory=list)
    summary: Summary = field(default_factory=Summary)


def gather(vault_dir: Path, mem_dir: Optional[Path] = None) -> Report:
    """The whole dashboard. Blocking — call it from a worker.

    The Tk version runs this synchronously on the UI thread, including once
    during startup, over a vault that can hold tens of thousands of files.
    """
    vault_dir = Path(vault_dir)
    mem_dir = Path(mem_dir) if mem_dir else vault_dir / "memory"
    return Report(memory=memory_files(mem_dir),
                  vault=vault_files(vault_dir),
                  summary=summarise(vault_dir, mem_dir))
