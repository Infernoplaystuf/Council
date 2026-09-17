"""
council_core.vault_jobs — the three Vault operations that produce a derived file.

WHY THIS MODULE EXISTS, AND WHY IT IS AN ADMISSION
The Qt Vault tab says, in three places, that these operations "need the Council
tab" and cannot run yet. That was wrong, and it was my mistake: I read
"Council" in `_vmgr_new_collection`'s docstring as the model council and
assumed a dependency on the deliberation plumbing. It means the application.

Checked rather than assumed the second time: `vault_collections.py`,
`deferred_tasks.py` and `derived_results.py` contain no model call of any kind,
and `vault_analyst`'s only matches for one are the letters "ce" inside words
like "difference" and "confidence". `propose_members` is deterministic scoring
over filename slugs and index lookups. All three of these are pandas and disk.

So they belong here with the rest of the Vault, and the Qt tab can have them
now rather than after phase 6.

WHAT THEY SHARE
Each one computes something expensive, writes it to `data_in/derived/`, and
registers it in the DerivedStore with a fingerprint of its sources. That last
step is the point: the Council's precomputed-answer route reads the store and
reuses a result ONLY while its sources are unchanged, so "run this deferred
task" and "ask the same question again next week" are connected. Dropping the
record would not break anything visibly — it would just quietly make every
re-ask recompute.

WHY THE OUTPUT IS NOT IN A HIDDEN FOLDER
A `.deferred_results` dot-directory is skipped by the vault index and the
analyst, so the council could never find what it had just computed. The comment
recording that is carried over verbatim; it is exactly the kind of decision
that looks like untidiness and gets "fixed".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, Tuple

LogFn = Callable[[str], None]


@dataclass
class JobResult:
    """A computed artefact, or why there isn't one."""
    ok: bool
    message: str
    output_path: Optional[Path] = None
    summary: str = ""
    error: Optional[BaseException] = None


@dataclass
class ProposalResult:
    """Files a collection might contain, with why each was proposed."""
    ok: bool
    message: str
    rows: List[Tuple[str, float, List[str]]] = field(default_factory=list)
    error: Optional[BaseException] = None


def _slug(text: str, limit: int = 48) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")[:limit]


# ============================================================
# Deferred tasks
# ============================================================

#: Shown when the selected task is a note rather than something to run. Kept
#: as a constant because both front ends must say the same thing, and because
#: it is the only place a user learns that ✓ Done is what they want.
NOT_RUNNABLE = ("This is a tool request / note — it's logged for the "
                "developer, not auto-runnable. Use ✓ Done when handled.")


def deferred_output_name(task: Any) -> str:
    """The filename a deferred run writes to.

    Built from the task's own words rather than its internal id, so the vault
    contains "summary__Q3__bigger_summary_of_sales__a1b2.csv" and not an opaque
    hash. The last four characters of the id keep repeat runs distinct without
    being noisy.
    """
    import deferred_tasks

    description = (getattr(task, "question", None)
                   or getattr(task, "folder", None) or "deferred")
    folder = getattr(task, "folder", "") or ""
    parts = (
        "summary" if task.kind == deferred_tasks.KIND_BIGGER_SUMMARY else "stats",
        re.sub(r"[^A-Za-z0-9]+", "_", folder).strip("_") if folder else "",
        _slug(description) or "deferred",
        str(getattr(task, "id", ""))[-4:],
    )
    return "__".join(p for p in parts if p)


def check_deferred_runnable(vault_dir: Path, task_id: Any):
    """(task, problem). Exactly one of them is None."""
    if not task_id:
        return None, "Select a task first."
    try:
        import deferred_tasks
        task = deferred_tasks.DeferredTaskStore(Path(vault_dir)).get(task_id)
    except Exception as exc:                              # noqa: BLE001
        return None, f"Load failed: {exc!r}"
    if task is None:
        return None, "Task not found (refresh)."
    if task.kind not in deferred_tasks.RUNNABLE_KINDS:
        return None, NOT_RUNNABLE
    return task, None


def run_deferred_task(vault_dir: Path, task_id: Any) -> JobResult:
    """Run one deferred task with the deterministic tooling and file the result.

    No model is involved; this is pandas over the vault's data files.

    SCOPED TO THE FILES THE TASK IS ABOUT. The capture dialog resolves
    filenames out of the question into ``task.files``; without honouring that,
    a run summarises the whole folder regardless of which file the user asked
    about, which is the "wrong files" bug this scoping fixed.
    """
    task, problem = check_deferred_runnable(vault_dir, task_id)
    if problem:
        return JobResult(False, problem)

    try:
        import pandas as pd

        import data_index
        import deferred_tasks
        import derived_results
        import vault_analyst

        vault_dir = Path(vault_dir)
        store = deferred_tasks.DeferredTaskStore(vault_dir)
        in_dir = data_index.input_dir(vault_dir)

        target = in_dir
        if task.folder:
            candidate = in_dir / task.folder
            if candidate.exists():
                target = candidate

        # NON-hidden folder on purpose: a ".deferred_results" dot-dir is
        # skipped by the vault index/analyst, so the council could never find
        # the saved result. This way, re-asking the same question surfaces it.
        out_path = (derived_results.derived_dir(vault_dir)
                    / f"{deferred_output_name(task)}.csv")

        wanted = {name.lower() for name in (task.files or []) if name}
        named_paths = []
        if wanted:
            # list_data_files = CSV ∪ Excel (a superset of list_csv_files), so
            # a CSV-only re-walk fallback would be logically dead — a name that
            # does not match here cannot match the CSV subset.
            for path in vault_analyst.list_data_files([target]):
                if path.name.lower() in wanted:
                    named_paths.append(path)

        if task.kind == deferred_tasks.KIND_BIGGER_SUMMARY:
            if named_paths:
                # A per-COLUMN profile of each named file — genuinely "bigger"
                # than what chat gave, and on the right files.
                frames = [vault_analyst.summarize_csv(p) for p in named_paths]
                frame = (pd.concat(frames, ignore_index=True) if frames
                         else vault_analyst.folder_data_summary([target]))
                summary = ("profiled %d named file(s): %s"
                           % (len(named_paths),
                              ", ".join(p.name for p in named_paths[:5])))
            else:
                frame = vault_analyst.folder_data_summary([target])
                summary = (f"{len(frame)} file(s) profiled"
                           + (" (named file(s) not found — used the whole "
                              "folder)" if wanted else ""))
        else:                                             # deeper_stats
            frame = vault_analyst.folder_column_stats(vault_dir, [target])
            if wanted and "file" in frame.columns:
                subset = frame[frame["file"].str.lower().isin(wanted)]
                if not subset.empty:
                    frame = subset.reset_index(drop=True)
            file_count = int(frame["file"].nunique()) if "file" in frame else 0
            summary = f"stats for {file_count} file(s)"

        frame.to_csv(out_path, index=False)
        store.mark_done(task.id, result_path=str(out_path),
                        result_summary=summary)

        # Catalogue the output with its SOURCE FINGERPRINT so a future re-ask
        # reuses it only while the sources are unchanged.
        try:
            sources = ([str(p) for p in named_paths] if named_paths
                       else [str(target)])
            derived_results.DerivedStore(vault_dir).record(
                label=(task.question or task.label() or out_path.stem),
                output=str(out_path), sources=sources,
                operation=task.kind,
                columns=[str(c) for c in frame.columns],
                rows=int(len(frame)))
        except Exception:                                 # noqa: BLE001
            pass                                          # cataloguing is a bonus

        return JobResult(
            True,
            f"Done — {summary} → {out_path.name} (in data_in/derived/). "
            "Re-ask in the Council tab to use it.",
            output_path=out_path, summary=summary)
    except Exception as exc:                              # noqa: BLE001
        return JobResult(False, f"Run failed: {exc!r}", error=exc)


# ============================================================
# Collections
# ============================================================

def collection_candidate_files(vault_dir: Path) -> List[str]:
    """Every data file under data_in, as forward-slashed relative paths.

    Forward slashes on every platform because these strings are stored in the
    collection and compared as text; a collection built on Windows must still
    match on the same vault opened elsewhere.
    """
    import os

    try:
        import data_index
        in_dir = data_index.input_dir(Path(vault_dir))
    except Exception:                                     # noqa: BLE001
        return []
    found = []
    try:
        for base, dirs, names in os.walk(str(in_dir)):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in names:
                if name.startswith("."):
                    continue
                found.append(str(Path(base, name).relative_to(in_dir))
                             .replace("\\", "/"))
    except Exception:                                     # noqa: BLE001
        return []
    return sorted(set(found))


def propose_collection_members(vault_dir: Path, name: str,
                               *, index: Any = None) -> ProposalResult:
    """Files that probably belong in the collection called ``name``.

    Deterministic — no model. vault_collections.propose_members combines a
    value match in the data (strongest), a filename match, and expansion along
    shared join columns. Without an index only the filename half runs, which is
    why this works before the vault is indexed.
    """
    if not (name or "").strip():
        return ProposalResult(False, "Name the collection first.")
    try:
        import vault_collections
        rows = vault_collections.propose_members(Path(vault_dir), name,
                                                 index=index)
    except Exception as exc:                              # noqa: BLE001
        return ProposalResult(False, f"Discover failed: {exc!r}", error=exc)
    if not rows:
        return ProposalResult(
            True,
            f"Nothing matched “{name}”. Add files by name, or build the "
            "keyword index so the contents can be searched too.")
    return ProposalResult(True, f"{len(rows)} suggestion(s).", rows=rows)


def save_collection(vault_dir: Path, name: str, files: Sequence[str],
                    *, renaming_from: Optional[str] = None) -> JobResult:
    """Create or update a collection.

    ``renaming_from`` is how the edit dialog changes a collection's name
    without orphaning the old one — the Tk dialog renames first and then
    upserts, and a front end that forgets the rename leaves two collections
    where the user expected one.
    """
    name = (name or "").strip()
    if not name:
        return JobResult(False, "Name the collection first.")
    try:
        import vault_collections
        store = vault_collections.CollectionStore(Path(vault_dir))
        if renaming_from and renaming_from != name:
            store.rename(renaming_from, name)
        store.upsert(name, list(files))
    except Exception as exc:                              # noqa: BLE001
        return JobResult(False, f"Save failed: {exc!r}", error=exc)
    return JobResult(
        True,
        f"Saved “{name}” — {len(files)} file(s). Ask the council: "
        f"“show me {name}”.")


def summarize_collection(vault_dir: Path, name: str) -> JobResult:
    """Profile every file in a collection, per column, into one derived CSV."""
    if not name:
        return JobResult(False, "Select a collection first.")
    try:
        import pandas as pd

        import derived_results
        import vault_analyst
        import vault_collections

        vault_dir = Path(vault_dir)
        paths = vault_collections.CollectionStore(vault_dir).abs_paths(name)
        if not paths:
            return JobResult(False, "No existing files in that collection.")

        frames = []
        for path in paths:
            try:
                frames.append(vault_analyst.summarize_csv(path))
            except Exception:                             # noqa: BLE001
                pass            # one unreadable file must not lose the rest
        frame = (pd.concat(frames, ignore_index=True) if frames
                 else pd.DataFrame())

        out_path = (derived_results.derived_dir(vault_dir)
                    / f"collection__{_slug(name) or 'collection'}.csv")
        frame.to_csv(out_path, index=False)

        try:
            derived_results.DerivedStore(vault_dir).record(
                label=f"summary of the {name} collection",
                output=str(out_path), sources=[str(p) for p in paths],
                operation="collection_summary",
                columns=[str(c) for c in frame.columns],
                rows=int(len(frame)))
        except Exception:                                 # noqa: BLE001
            pass

        skipped = len(paths) - len(frames)
        tail = f" ({skipped} unreadable, skipped)" if skipped else ""
        return JobResult(
            True,
            f"Summarized {len(frames)} file(s){tail} → "
            f"data_in/derived/{out_path.name}",
            output_path=out_path)
    except Exception as exc:                              # noqa: BLE001
        return JobResult(False, f"Summarize failed: {exc!r}", error=exc)
