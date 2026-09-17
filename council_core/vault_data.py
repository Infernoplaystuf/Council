"""
council_core.vault_data — the Vault tab's data operations.

The rest of what the Vault manager does, with no user interface attached:
reading the deferred-task and collection stores, deleting an item, the RAG
miss log, and Mongo conversion.

THE PATTERN THESE ALL SHARE
Each returns the ROWS a table needs plus the SENTENCE that goes under it, and
neither front end decides either. The Tk shell and the Qt tab had begun to
disagree about small things — whether an empty list says "No collections yet"
or nothing at all — and those differences are invisible in review and obvious
to a user who switches.

Rows are plain tuples of strings and the ids travel beside them, because a
Treeview and a QTreeWidget agree on nothing except that.

DELETING IS DIFFERENT, AND IS TREATED DIFFERENTLY
delete_path removes a user's files. It takes an already-resolved path, refuses
anything outside the vault, and never asks a question — the confirmation stays
in the front end where the user can see what they are agreeing to. The rule
this codebase works to is that nothing deletes user data without the user
saying so, and a shared helper that could prompt would make that harder to
verify, not easier.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, Tuple

LogFn = Callable[[str], None]

#: kind -> what a person calls it. Shown in the deferred-tasks table.
DEFERRED_LABELS = {
    "bigger_summary": "Bigger summary",
    "deeper_stats": "Deeper stats",
    "tool_request": "Tool request",
    "other": "Other",
}


@dataclass
class ListResult:
    """Rows for a table, the ids behind them, and the line underneath."""
    ok: bool
    message: str
    rows: List[Tuple[str, ...]] = field(default_factory=list)
    ids: List[Any] = field(default_factory=list)
    error: Optional[BaseException] = None


@dataclass
class OpResult:
    ok: bool
    message: str
    error: Optional[BaseException] = None


# ============================================================
# Deferred tasks
# ============================================================

def pending_tasks(vault_dir: Path) -> ListResult:
    """Tasks sent over from the Council tab and not yet dealt with."""
    try:
        import deferred_tasks
        pending = deferred_tasks.DeferredTaskStore(Path(vault_dir)).pending()
    except Exception as exc:                              # noqa: BLE001
        return ListResult(False, f"Could not load tasks: {exc!r}", error=exc)
    rows = [(DEFERRED_LABELS.get(t.kind, t.kind), t.label()) for t in pending]
    ids = [t.id for t in pending]
    return ListResult(
        True,
        (f"{len(pending)} pending task(s)." if pending else
         "No pending tasks. Send some from the Council tab (⤓ Defer to Vault)."),
        rows=rows, ids=ids)


def set_task_status(vault_dir: Path, task_id: Any, status: str) -> OpResult:
    """Mark one deferred task done or dismissed."""
    if not task_id:
        return OpResult(False, "Select a task first.")
    try:
        import deferred_tasks
        store = deferred_tasks.DeferredTaskStore(Path(vault_dir))
        if status == "done":
            store.mark_done(task_id)
        else:
            store.dismiss(task_id)
    except Exception as exc:                              # noqa: BLE001
        return OpResult(False, f"Update failed: {exc!r}", error=exc)
    return OpResult(True, f"Task marked {status}.")


# ============================================================
# Collections
# ============================================================

def all_collections(vault_dir: Path) -> ListResult:
    """Every saved collection, by name, with how many files each groups."""
    try:
        import vault_collections
        collections = vault_collections.CollectionStore(Path(vault_dir)).all()
    except Exception as exc:                              # noqa: BLE001
        return ListResult(False, f"Could not load collections: {exc!r}",
                          error=exc)
    ordered = sorted(collections, key=lambda c: c.name.lower())
    rows = [(c.name, str(len(c.files))) for c in ordered]
    return ListResult(
        True,
        (f"{len(ordered)} collection(s)." if ordered else
         "No collections yet. ➕ New… groups files into a project."),
        rows=rows, ids=[c.name for c in ordered])


def confirm_delete_collection(name: str) -> str:
    """What to ask before deleting a collection.

    The reassurance matters and is easy to drop when re-implementing a dialog:
    a collection is a grouping, and deleting one does not touch a single file.
    """
    return (f"Delete the collection “{name}”?\n"
            "(The files themselves are NOT touched — this only removes the "
            "grouping.)")


def delete_collection(vault_dir: Path, name: str) -> OpResult:
    if not name:
        return OpResult(False, "Select a collection first.")
    try:
        import vault_collections
        vault_collections.CollectionStore(Path(vault_dir)).delete(name)
    except Exception as exc:                              # noqa: BLE001
        return OpResult(False, f"Delete failed: {exc!r}", error=exc)
    return OpResult(True, f"✓ Collection “{name}” removed.")


# ============================================================
# Deleting an item from the vault
# ============================================================

def confirm_delete_text(path: Path) -> str:
    path = Path(path)
    what = "folder and all its contents" if path.is_dir() else "file"
    return f"Delete {what}:\n{path.name}?"


def delete_path(path: Path, vault_dir: Path) -> OpResult:
    """Delete one file or folder from the vault.

    REFUSES ANYTHING OUTSIDE THE VAULT. The Tk version deletes whatever path
    the tree hands it, which is safe only because the tree is built from the
    vault — an invariant nothing checks and a future caller could break. The
    check costs one comparison and removes a whole class of accident.

    Never asks. The confirmation belongs in the front end, where the user can
    see what they are agreeing to.
    """
    path = Path(path)
    vault_dir = Path(vault_dir).resolve()
    try:
        resolved = path.resolve()
    except OSError as exc:
        return OpResult(False, f"✗ Delete failed: {exc}", error=exc)
    if resolved == vault_dir:
        return OpResult(False, "✗ Refusing to delete the vault itself.")
    if vault_dir not in resolved.parents:
        return OpResult(
            False, f"✗ Refusing to delete something outside the vault: {resolved}")
    try:
        if resolved.is_dir():
            shutil.rmtree(resolved)
        else:
            resolved.unlink()
    except Exception as exc:                              # noqa: BLE001
        return OpResult(False, f"✗ Delete failed: {exc}", error=exc)
    return OpResult(True, f"✓ Deleted: {path.name}")


# ============================================================
# The RAG miss log
# ============================================================

def miss_log_path(vault_dir: Path) -> Path:
    return Path(vault_dir) / "vault_rag_misses.txt"


def read_misses(vault_dir: Path) -> ListResult:
    """Queries that found no vault context — i.e. what the vault is missing.

    Newest first, because the interesting question is what the user asked
    recently and could not get answered.
    """
    nothing_yet = ("No RAG misses recorded yet.\nMisses are logged when RAG "
                   "finds no relevant vault context.")
    path = miss_log_path(vault_dir)
    if not path.exists():
        return ListResult(True, nothing_yet)
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln]
    except Exception as exc:                              # noqa: BLE001
        return ListResult(False, f"Error reading miss log: {exc}", error=exc)
    if not lines:
        # A cleared log truncates rather than deletes, so "empty" and "never
        # written" are the same thing to a user and should read the same.
        return ListResult(True, nothing_yet)
    rows = []
    for line in reversed(lines):
        parts = line.split("\t", 1)
        stamp = parts[0][:16] if parts else ""
        query = parts[1] if len(parts) > 1 else line
        rows.append((stamp, query))
    return ListResult(
        True,
        f"{len(lines)} missed queries — these topics are not covered by your "
        f"vault.",
        rows=rows)


def clear_misses(vault_dir: Path) -> OpResult:
    """Empty the miss log. Truncates rather than deletes, so whatever is
    appending to it keeps working."""
    try:
        miss_log_path(vault_dir).write_text("", encoding="utf-8")
    except Exception as exc:                              # noqa: BLE001
        return OpResult(False, f"Could not clear the miss log: {exc}", error=exc)
    return OpResult(True, "Miss log cleared.")


# ============================================================
# Mongo conversion
# ============================================================

MONGO_SUFFIXES = ("*.bson", "*.json", "*.jsonl")


def check_mongo_request(selected: str, want_csv: bool, want_schema: bool,
                        want_text: bool, scan_all: bool) -> Optional[str]:
    """Why this conversion cannot start, or None."""
    if not (want_csv or want_schema or want_text):
        return "Pick at least one output (Clean CSV / Schema / Text digest)."
    if not scan_all and not (selected or "").strip():
        return "Select a .bson/.json/.jsonl file, or use “Convert ALL”."
    return None


def find_mongo_files(input_dir: Path, out_root: Path) -> List[Path]:
    """Every Mongo dump under ``input_dir``, minus our own output.

    Excluding out_root is not tidiness: a converted .json left in scope would
    be re-converted on the next run, and again on the one after that.
    """
    input_dir, out_root = Path(input_dir), Path(out_root)
    found: List[Path] = []
    for pattern in MONGO_SUFFIXES:
        found += list(input_dir.rglob(pattern))
    return [f for f in found
            if out_root not in f.parents and f.parent != out_root]


def converting_line(done: int, total: int, name: str) -> str:
    return f"Converting {done}/{total} — {name[:40]}"


def convert_mongo(files: Sequence[Path], out_root: Path, *,
                  want_csv: bool = True, want_schema: bool = True,
                  want_text: bool = False,
                  on_progress: Optional[Callable[[int, int, str], None]] = None
                  ) -> OpResult:
    """Convert Mongo dumps into model-readable artefacts. Sources are only read.

    One file failing does not stop the rest; the last error is reported at the
    end, which is what the Tk version does and is right for a batch — a dump
    with one bad document should not cost the other forty.

    The conversion itself streams, and that is load-bearing: loading a whole
    dump OOM-crashed the app on Linux.
    """
    files = [Path(f) for f in files]
    if not files:
        return OpResult(True, "No .bson/.json/.jsonl files found in the vault.")
    try:
        import vault_analyst
    except Exception as exc:                              # noqa: BLE001
        return OpResult(False, f"Convert failed: {exc!r}", error=exc)

    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    done = ok = total_rows = 0
    last_error = ""
    for path in files:
        try:
            summary = vault_analyst.convert_mongo_file(
                path, out_root, want_csv=want_csv, want_schema=want_schema,
                want_text=want_text)
            if summary.get("docs"):
                total_rows += summary.get("rows", 0)
                ok += 1
        except Exception as exc:                          # noqa: BLE001
            last_error = f"{path.name}: {exc!r}"
        done += 1
        if on_progress:
            on_progress(done, len(files), path.name)

    tail = f"  (last error — {last_error})" if last_error else ""
    return OpResult(
        True,
        f"Done — {ok}/{len(files)} file(s), {total_rows} rows → "
        f"data_in/converted_mongo/.{tail}  "
        "Run “1. Build Keyword Index” to make it searchable.")
