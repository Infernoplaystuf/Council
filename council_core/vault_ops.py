"""
council_core.vault_ops — vault operations, with no user interface attached.

The first extraction of the shell port. `_vmgr_build_keyword_index` lived on
CouncilConsole and did four things at once: fetched the index, set a Tk
variable, started a thread, and marshalled progress back with `self.after(0,
...)`. Only one of those four is the actual operation.

WHAT MOVED AND WHAT DID NOT
Moved here: getting the index, running the rebuild, turning the outcome into a
sentence a person can read. Left with the caller: the thread, and the decision
about which thread the progress callback lands on. That split is deliberate —
Tk marshals with `after(0, ...)` and Qt with a queued signal, and a function
that tried to do it for both would have to know which front end it was under,
which is exactly the coupling this package exists to remove.

WHY PROGRESS IS A CALLBACK AND NOT A QUEUE
A queue would force one shape on both front ends (and on any script that wants
to build an index with no UI at all). A callback is what `VaultIndex.rebuild`
already takes, so this layer passes it straight through and adds nothing.

THREADING, STATED PLAINLY
Nothing in here starts a thread. `build_keyword_index` blocks for as long as the
walk takes — minutes on a large vault — so a UI caller runs it on a worker and
delivers `on_progress` and the returned result back to its own UI thread. Both
front ends do exactly that, in five lines each.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

#: progress(done, total, name) — the signature VaultIndex.rebuild already fires.
ProgressFn = Callable[[int, int, str], None]

#: The longest filename a progress line shows before it is trimmed. A vault
#: holds paths long enough to wrap a status bar, and a wrapping status bar
#: resizes the layout under the user's cursor.
NAME_LIMIT = 48


@dataclass
class IndexResult:
    """What happened, in a form either front end can render without deciding
    anything. `message` is the sentence the user sees; `ok` is what decides
    whether it is shown as success or failure."""
    ok: bool
    message: str
    indexed: int = 0
    total: int = 0
    error: Optional[BaseException] = None


def short_name(name: str, limit: int = NAME_LIMIT) -> str:
    """Trim a filename for a one-line status. Kept here rather than in either
    view because both would otherwise trim differently and the same index run
    would read differently depending on which front end ran it."""
    name = str(name)
    return name if len(name) <= limit else name[:limit - 3] + "…"


def progress_line(done: int, total: int, name: str) -> str:
    """The status sentence for one indexed file."""
    return f"Indexing {done}/{total} — {short_name(name)}"


def build_keyword_index(index: Any,
                        on_progress: Optional[ProgressFn] = None) -> IndexResult:
    """Walk the vault and (re)build the keyword index. Fast — no LLM.

    ``index`` is a VaultIndex (or None, which is what the shell's lazy getter
    returns when the index could not be constructed — an unreadable vault, a
    missing dependency). Returning a result for that case rather than raising
    keeps the caller's error path and its success path the same shape.

    BLOCKS. Run it on a worker thread; see the module docstring.
    """
    if index is None:
        return IndexResult(False, "Vault index unavailable.")
    try:
        indexed = index.rebuild(progress=on_progress)
    except Exception as exc:                              # noqa: BLE001
        # The operation failing is a normal outcome here — an unreadable file,
        # a vanished folder — and the user needs the reason, not a traceback in
        # a console they are not looking at.
        return IndexResult(False, f"Keyword index failed: {exc!r}", error=exc)
    total = len(getattr(index, "records", ()) or ())
    return IndexResult(
        True,
        f"Keyword index built — {indexed} files (re)indexed, {total} total.",
        indexed=indexed, total=total)


# ============================================================
# Descriptions and embeddings — the other two index layers
# ============================================================

def describing_line(done: int, total: int) -> str:
    return f"Descriptions: {done}/{total}…"


def embedding_line(done: int, total: int) -> str:
    return f"Embeddings: {done}/{total}…"


def build_descriptions(index: Any,
                       on_progress: Optional[ProgressFn] = None) -> IndexResult:
    """Generate per-file LLM descriptions for records that have none.

    Refreshes the keyword index first and IGNORES a failure there — which is
    the shell's behaviour and is deliberate: one unreadable file should not
    stop the other nine hundred being described.

    BLOCKS, and far longer than the keyword walk: 3-10 s per file on a 7B GGUF,
    so a thousand-file vault is hours. The caller runs it on a worker.
    """
    if index is None:
        return IndexResult(False, "Vault index unavailable.")
    try:
        index.rebuild()
    except Exception:                                     # noqa: BLE001
        pass
    records = getattr(index, "records", {}) or {}
    pending = sum(1 for r in records.values() if not r.get("description"))
    if pending == 0:
        return IndexResult(True,
                           f"All {len(records)} files already have descriptions.",
                           indexed=0, total=len(records))
    try:
        done = index.generate_descriptions(on_progress=on_progress)
    except Exception as exc:                              # noqa: BLE001
        return IndexResult(False, f"Description build failed: {exc!r}", error=exc)
    return IndexResult(True, f"Descriptions complete — {done} files summarized.",
                       indexed=done, total=len(records))


def starting_descriptions(index: Any) -> IndexResult:
    """What to show BEFORE the run, which needs the pending count.

    Returned as a result rather than a string so the "nothing to do" case has
    one shape: both front ends then either show the message and stop, or show
    it and start the worker.
    """
    if index is None:
        return IndexResult(False, "Vault index unavailable.")
    records = getattr(index, "records", {}) or {}
    pending = sum(1 for r in records.values() if not r.get("description"))
    # `total` is the PENDING count, not the record count, because that is what
    # the callers branch on: both front ends do `if not start.ok or
    # start.total == 0: return`. Reporting len(records) here sent both of them
    # off to start a worker with nothing for it to do.
    return IndexResult(
        True,
        (f"All {len(records)} files already have descriptions." if pending == 0
         else f"Generating descriptions for {pending} files… (each ~3-10s)"),
        indexed=0, total=pending)


def build_embeddings(index: Any,
                     on_progress: Optional[ProgressFn] = None) -> IndexResult:
    """Build vector embeddings for every record.

    sentence-transformers being absent is a RESULT, not an error: it is an
    optional dependency, and the honest answer is a sentence naming what to
    install rather than a traceback.
    """
    if index is None:
        return IndexResult(False, "Vault index unavailable.")
    try:
        index.rebuild()
    except Exception:                                     # noqa: BLE001
        pass
    embeddings = index.embeddings()
    if embeddings is None:
        return IndexResult(
            False, "sentence-transformers not available — pip install it first.")
    try:
        done = index.build_embeddings(on_progress=on_progress)
    except Exception as exc:                              # noqa: BLE001
        return IndexResult(False, f"Embedding build failed: {exc!r}", error=exc)
    stats = embeddings.stats()
    return IndexResult(
        True,
        f"Vectors ready — {stats['vectors']} files "
        f"({stats['dim']}-dim, {stats['size_kb']} KB on disk).",
        indexed=done, total=stats["vectors"])


def starting_embeddings(index: Any) -> IndexResult:
    """The line shown before embedding starts; it needs the model name."""
    if index is None:
        return IndexResult(False, "Vault index unavailable.")
    embeddings = index.embeddings()
    if embeddings is None:
        return IndexResult(
            False, "sentence-transformers not available — pip install it first.")
    count = len(getattr(index, "records", ()) or ())
    return IndexResult(
        True, f"Embedding {count} files (model: {embeddings.model_name})…",
        total=count)


# ============================================================
# Repositories
# ============================================================

#: What a clone copies into the vault. A repo is mostly things a vault has no
#: use for, and copying them makes every later index slower for no benefit.
INDEXABLE = {".py", ".md", ".txt", ".json", ".yaml", ".yml",
             ".html", ".rst", ".csv", ".log", ".toml", ".ini"}
SKIP_DIRS = {".git", ".github", "__pycache__", "node_modules", ".tox", "dist",
             "build", ".venv", "venv", "env", ".eggs", ".mypy_cache",
             ".pytest_cache"}
SKIP_FILES = {".gitignore", ".gitattributes", ".gitmodules", "poetry.lock",
              "package-lock.json", "yarn.lock", "Pipfile.lock", ".DS_Store"}
MAX_BYTES = 500_000

LogFn = Callable[[str], None]


@dataclass
class RepoResult:
    ok: bool
    message: str
    dest: Optional[Path] = None
    error: Optional[BaseException] = None


def repo_subfolder(url: str) -> str:
    """The vault folder a repo URL lands in, sanitised.

    CARRIES A KNOWN QUIRK, ON PURPOSE. `rstrip(".git")` strips CHARACTERS, not
    the suffix, so any trailing run of '.', 'g', 'i' or 't' is eaten:

        https://host/          -> "hos"     (the 't' of "host" goes)
        https://host/my-git    -> "my-"

    That is the behaviour the shell has always had, and an extraction is the
    wrong moment to change what folder an existing clone lands in — a "fix"
    here would silently re-clone every affected repo into a new directory and
    leave the old copy behind. Worth fixing separately, deliberately, with a
    migration; test_a_repo_url_becomes_a_safe_folder_name pins it until then.
    """
    name = url.rstrip("/").rstrip(".git").rsplit("/", 1)[-1]
    return re.sub(r"[^A-Za-z0-9._-]", "_", name) or "repo"


def clone_repo(url: str, *, vault_dir: Path, subfolder: Optional[str] = None,
               branch: Optional[str] = None, depth: int = 1,
               log: Optional[LogFn] = None) -> Path:
    """Clone or update a repo and copy its indexable files into the vault.

    Moved here from council_gui_engine, where it sat as a module-level function
    with no Tk in it at all — shared logic already, in the wrong file. Returns
    the destination vault subfolder.
    """
    def _log(message: str) -> None:
        if log:
            log(message)
        else:
            print(message)

    vault_dir = Path(vault_dir)
    subfolder = subfolder or repo_subfolder(url)
    clone_dir = vault_dir / ".git_clones" / subfolder
    dest_dir = vault_dir / subfolder

    if clone_dir.exists():
        _log(f"Updating existing clone: {subfolder}")
        r = subprocess.run(["git", "pull"], cwd=str(clone_dir),
                           capture_output=True, text=True, timeout=120,
                           encoding="utf-8", errors="replace")
        _log(r.stdout.strip() or r.stderr.strip() or "Already up to date.")
    else:
        clone_dir.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["git", "clone", f"--depth={depth}"]
        if branch:
            cmd += ["--branch", branch]
        cmd += [url, str(clone_dir)]
        _log(f"Cloning {url} …")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip() or "git clone failed")
        _log(f"Cloned to {clone_dir.name}")

    dest_dir.mkdir(parents=True, exist_ok=True)
    copied = skipped = 0
    for src in clone_dir.rglob("*"):
        if not src.is_file():
            continue
        parts = set(src.relative_to(clone_dir).parts)
        if parts & SKIP_DIRS or src.name in SKIP_FILES:
            skipped += 1
            continue
        if src.suffix.lower() not in INDEXABLE:
            skipped += 1
            continue
        try:
            if src.stat().st_size > MAX_BYTES:
                skipped += 1
                continue
        except OSError:
            skipped += 1
            continue
        dst = dest_dir / src.relative_to(clone_dir)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1

    _log(f"Copied {copied} files → vault/{subfolder}  ({skipped} skipped)")
    return dest_dir


def check_clone_url(url: str) -> Optional[str]:
    """Why this URL cannot be cloned, or None if it can.

    Validation, not a dialog: both front ends showed the same two messages in
    their own log widget, so the rule lives here and the wording with it.
    """
    url = (url or "").strip()
    if not url:
        return "✗ Please enter a GitHub URL."
    if not url.startswith("http"):
        return "✗ URL must start with https://"
    return None


def clone(url: str, *, vault_dir: Path, subfolder: Optional[str] = None,
          branch: Optional[str] = None,
          log: Optional[LogFn] = None) -> RepoResult:
    """Validate, clone, and report. BLOCKS — run it on a worker."""
    problem = check_clone_url(url)
    if problem:
        return RepoResult(False, problem)
    try:
        dest = clone_repo(url, vault_dir=vault_dir, subfolder=subfolder,
                          branch=branch, log=log)
    except Exception as exc:                              # noqa: BLE001
        return RepoResult(False, f"✗ Clone failed: {exc}", error=exc)
    return RepoResult(True, f"✓ Done → {dest.name}", dest=dest)


def pull(vault_dir: Path, subfolder: str,
         log: Optional[LogFn] = None) -> RepoResult:
    """Pull a previously cloned repo and re-copy its files into the vault.

    WHICH folder is selected stays with the view — that is a tree selection,
    not an operation — so this takes the subfolder name and nothing else.
    """
    vault_dir = Path(vault_dir)
    clone_dir = vault_dir / ".git_clones" / subfolder
    if not clone_dir.exists():
        return RepoResult(
            False,
            f"✗ No git clone found for '{subfolder}'. Use Clone Repo first.")

    def _log(message: str) -> None:
        if log:
            log(message)

    try:
        r = subprocess.run(["git", "pull"], cwd=str(clone_dir),
                           capture_output=True, text=True, timeout=120,
                           encoding="utf-8", errors="replace")
        _log(f"git pull: {r.stdout.strip() or r.stderr.strip() or 'Done.'}")
        origin = subprocess.run(["git", "remote", "get-url", "origin"],
                                cwd=str(clone_dir), capture_output=True,
                                text=True, timeout=15, encoding="utf-8",
                                errors="replace")
        if origin.returncode == 0 and origin.stdout.strip():
            clone_repo(origin.stdout.strip(), vault_dir=vault_dir,
                       subfolder=subfolder, log=log)
    except Exception as exc:                              # noqa: BLE001
        return RepoResult(False, f"✗ Pull failed: {exc}", error=exc)
    return RepoResult(True, f"✓ {subfolder} updated", dest=vault_dir / subfolder)


# ============================================================
# Column statistics
# ============================================================

def stats_progress_line(done: int, total: int, name: str) -> Optional[str]:
    """A line every 25 files, and one at the end. None means "say nothing".

    The throttle belongs to the operation, not to either UI: at one line per
    file a cold vault writes thousands of lines into the activity log and the
    useful ones scroll away.
    """
    if total and (done == total or done % 25 == 0):
        return f"  stats: {done}/{total} ({name})"
    return None


def stats_summary(result: dict) -> str:
    return (f"✓ data stats ready — processed {result['processed']} new, "
            f"{result['already_current']} already cached "
            f"({result['seen']} CSVs).")


def build_stats(run: Callable, on_line: Optional[LogFn] = None) -> RepoResult:
    """Run the incremental column-stats precompute and report.

    ``run(on_progress=...)`` is passed in rather than imported: the shell's
    _build_stats_index reaches into the console's own caches, so it is the one
    part of this operation still to be extracted. What IS here is the
    throttling and the wording, which both front ends have to agree on.
    """
    def progress(done: int, total: int, name: str) -> None:
        line = stats_progress_line(done, total, name)
        if line and on_line:
            on_line(line)

    try:
        result = run(on_progress=progress)
    except Exception as exc:                              # noqa: BLE001
        return RepoResult(False, f"✗ stats build failed: {exc!r}", error=exc)
    return RepoResult(True, stats_summary(result))
