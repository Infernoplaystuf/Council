"""
council_core.grapher_files — choosing a file to chart, and loading it.

TWO CONFIRMED DEFECTS ARE DESIGNED OUT HERE RATHER THAN FIXED

B2 — the overlay dropdown can never resolve a file.
    The file combo's entries are built `data_in`-relative
    (`str(p.relative_to(in_dir))`) and the overlay combo is populated from
    exactly that list. But the overlay's resolver re-derives the path
    VAULT-relative, so `sales.csv` is compared against `data_in/sales.csv` and
    never matches — and the loop falls through with no message at all.

    It is a broken COPY of `_grapher_load_file`, which gets it right by trying
    four conventions in turn. Someone copied the resolver and dropped an arm.
    (Exactly the shape of the `amp()` divergence found in this port's own Qt
    code an hour earlier: two copies, silently unequal.)

    The fix is not a fifth convention. An entry CARRIES ITS RESOLVED PATH.
    A label is for the user to read; it is not an address. Re-deriving a path
    from display text is the bug, and `FileEntry` makes it unavailable.

    The correction the audit made to the original finding matters too: the
    feature is not uniformly dead. Entries added by Browse, by the sample
    picker and by the council's own chart request carry ABSOLUTE labels, and
    those DO resolve. So the overlay works in the rare path and fails silently
    in the common one — which is worse than broken, because the user has seen
    it work.

B3 — the sheet dropdown is decorative.
    Picking a sheet re-loads the file and forwards nothing, so `_load_excel`
    keeps `sheet_name=0`; the combo is then repopulated from the loaded
    metadata, which SNAPS THE USER'S CHOICE BACK with no error. There is no
    parameter anywhere in the chain to carry the sheet.

    So the sheet is part of `LoadRequest`. A loader that cannot be asked for a
    sheet cannot silently ignore one.

AND ONE THE FIRST PASS MISSED
A failed load leaves the failed dataset installed as the current one: the Tk
code assigns `self._grapher_dataset = ds` and only then checks
`ds.load_error`, returning before any control is refreshed. The tab is left
describing a dataset that did not load, and drilling into correlations on it
crashes. `LoadOutcome` separates "what happened" from "what you now have", so
there is nothing to install when it fails.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence, Tuple

#: Directories the file scan never descends into. Generated output, caches and
#: the app's own artefacts — a chart built from last week's chart is not a
#: thing anybody wants offered.
HIDDEN_NAMES = frozenset({
    "derived", "deferred_results", "converted_mongo", "__pycache__",
    ".vault_index", ".stats_cache", "conversation_logs", ".git", ".chromadb",
    "charts",
})


@dataclass(frozen=True)
class FileEntry:
    """One row in the file dropdown: what to show, and what it IS.

    `path` is authoritative. `label` exists only so a human can pick. Nothing
    may reconstruct a path from a label — that reconstruction is B2.
    """
    path: Path
    label: str
    origin: str = "data_in"          # data_in | browsed | sample | council

    def __str__(self) -> str:        # what a combo box displays
        return self.label


def entry_for(path: Any, in_dir: Any, origin: str = "data_in") -> FileEntry:
    """A FileEntry with the shortest honest label for ``path``.

    Inside `data_in` the label is relative to it, which is what makes the
    dropdown readable; anywhere else it is the full path, because a bare
    basename would collide with a file of the same name in the vault.
    """
    path = Path(path)
    try:
        label = str(path.resolve().relative_to(Path(in_dir).resolve()))
        label = label.replace("\\", "/")
    except (ValueError, OSError):
        label = str(path)
    return FileEntry(path=path, label=label, origin=origin)


def scan_input_files(in_dir: Any, *, can_load=None) -> List[FileEntry]:
    """Every chartable file under ``in_dir``, newest first.

    Newest first because the file a user wants is almost always the one they
    just put there, and an alphabetical list buries it.
    """
    in_dir = Path(in_dir)
    found: List[Tuple[float, Path]] = []
    try:
        for base, dirs, names in os.walk(str(in_dir)):
            dirs[:] = [d for d in dirs
                       if d not in HIDDEN_NAMES and not d.startswith(".")]
            for name in names:
                if name.startswith("."):
                    continue
                path = Path(base) / name
                if can_load is not None and not can_load(path):
                    continue
                try:
                    found.append((path.stat().st_mtime, path))
                except OSError:
                    continue
    except OSError:
        return []
    found.sort(key=lambda pair: pair[0], reverse=True)
    return [entry_for(path, in_dir) for _, path in found]


def merge_entries(existing: Sequence[FileEntry],
                  added: Iterable[FileEntry]) -> List[FileEntry]:
    """Keep hand-added entries when the scan is refreshed.

    The Tk version reassigns the combo's values wholesale on every refresh,
    which silently discards anything the user added with Browse or loaded from
    the sample picker. Refreshing the list is not a reason to forget what the
    user chose.
    """
    out = list(existing)
    seen = {entry.path.resolve() if entry.path.exists() else entry.path
            for entry in out}
    for entry in added:
        key = entry.path.resolve() if entry.path.exists() else entry.path
        if key not in seen:
            seen.add(key)
            out.append(entry)
    return out


def resolve(entries: Sequence[FileEntry], label: str) -> Optional[FileEntry]:
    """The entry a dropdown selection means.

    A lookup, not a reconstruction. This is the whole of the B2 fix: there is
    no path arithmetic here to get wrong, and a label that is not in the list
    returns None rather than a path that does not exist.
    """
    label = (label or "").strip()
    if not label:
        return None
    for entry in entries:
        if entry.label == label:
            return entry
    # A caller may still hand us a raw path — the council's chart request does.
    for entry in entries:
        if str(entry.path) == label:
            return entry
    return None


def unresolved_message(label: str) -> str:
    """What to say when a selection names nothing.

    The Tk overlay loop falls through in silence, so the user clicks Overlay
    and nothing whatsoever happens — no chart, no error, no clue. Naming the
    label is the difference between "it is broken" and "that file is gone".
    """
    return (f"Could not find “{label}”. It may have been moved or deleted — "
            "refresh the file list.")


# ============================================================
# Loading
# ============================================================

@dataclass(frozen=True)
class LoadRequest:
    """What to load, INCLUDING which sheet.

    The sheet lives here because it has to travel. In the Tk build the sheet
    combo writes to a variable the loader never reads, so choosing a sheet
    reloads sheet 0 and then resets the combo to sheet 0 — B3. A loader that
    can be asked cannot silently ignore.
    """
    path: Path
    sheet: Optional[Any] = None      # name or index; None means "the first"


@dataclass
class LoadOutcome:
    """What happened, kept separate from what you now have.

    `dataset` is None when the load failed, which is what stops a failed load
    from being installed as the current dataset — the Tk code assigns first and
    checks `load_error` second, leaving the tab describing a file that did not
    open.
    """
    ok: bool
    message: str
    dataset: Any = None
    sheets: List[str] = None         # the sheets this file actually has
    loaded_sheet: Optional[Any] = None
    error: Optional[BaseException] = None

    def __post_init__(self):
        if self.sheets is None:
            self.sheets = []


def load_dataset(request: LoadRequest) -> LoadOutcome:
    """Load one file into a DataSet, honouring the requested sheet.

    Returns the sheet that was ACTUALLY loaded, so the control can show the
    truth rather than being repopulated from metadata that always says the
    first one.
    """
    path = Path(request.path)
    if not path.exists():
        return LoadOutcome(False, f"“{path.name}” no longer exists.")

    try:
        import graph_data
    except Exception as exc:                              # noqa: BLE001
        return LoadOutcome(False, f"The data loader is unavailable: {exc!r}",
                           error=exc)

    kwargs = {}
    if request.sheet is not None:
        kwargs["sheet_name"] = request.sheet
    try:
        dataset = graph_data.DataLoader.load(path, **kwargs)
    except Exception as exc:                              # noqa: BLE001
        return LoadOutcome(False, f"Could not read {path.name}: {exc}",
                           error=exc)

    load_error = getattr(dataset, "load_error", None)
    if load_error:
        return LoadOutcome(False, f"Could not read {path.name}: {load_error}")

    meta = getattr(dataset, "metadata", None) or {}
    sheets = list(meta.get("sheets") or [])
    loaded = meta.get("active_sheet", request.sheet)
    rows, cols = (getattr(dataset, "shape", None) or (0, 0))[:2]
    return LoadOutcome(
        True,
        f"Loaded {path.name} — {rows:,} rows × {cols} columns"
        + (f" (sheet {loaded})" if loaded not in (None, 0) else ""),
        dataset=dataset, sheets=sheets, loaded_sheet=loaded)
