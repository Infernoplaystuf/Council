"""
council_core.vault_setup — the vault's folders, and an upgrader's old files
moved into it. Run at every start by both front ends.

WHAT THE QT APP WAS SKIPPING
The Tk engine does two things at startup that have nothing to do with Tk:

  * at IMPORT, `_migrate_old_paths_to_vault`: an older build kept its log,
    node registry, model pins, workspace, chart output and Chroma store
    directly in the app folder (~/.council), and the Dream3D docs next to the
    script. Each is moved into the vault, once, if the vault does not already
    have one. It also makes logs/, workspace/ and tmp/.
  * in CouncilConsole.__init__, the data_in/ / data_out/ split: both folders,
    their subfolders and a README in each (data_index.init_data_dirs), the
    sweep that removes app-config files an earlier version copied into
    data_in/ (byte-identical copies only), and the copy of loose CSV/TSV/JSON
    at the vault root into data_in/ (originals kept).

The Qt app ran none of it. A user upgrading straight into Qt found their node
registry and model pins "gone" (still in ~/.council, unread), and a fresh Qt
vault had no data_in/ — the folder every message about adding data points at.

So the logic lives here, unchanged, and both call it: the Tk module and console
through the same names they always used, council_qt.launch before the window.

NOTHING HERE IS NEW BEHAVIOUR, WITH ONE EXCEPTION
The move list, the "never overwrite what the vault already has" rule, and
COUNCIL_SKIP_PATH_MIGRATION (which tests/sandbox_vault.py sets, because the
test vault is deleted at the end of a run and a migration into it MOVED the
user's legacy files there — measured in a scratch home) are the Tk ones,
moved. The data-folder steps follow data_index as the console did, except
that the loose-file copy skips the app's own vault-root state
(VAULT_ROOT_STATE): it used to file backend_settings.json, model_slots.json
and the vault indices in data_in/ as the user's datasets — in both shells.
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

#: The checkout this module belongs to: the Tk engine's own folder, which is
#: where an old build scraped the Dream3D docs (<repo>/vault/dream3d_docs).
#: Read at call time, so a test can point it somewhere harmless.
REPO_ROOT = Path(__file__).resolve().parent.parent

LogFn = Callable[[str], None]


def _print(message: str) -> None:
    """print, but a console that cannot show a character is not an error.

    MEASURED: the move lines carry "→", and with stdout a pipe on Windows
    (cp1252) print raised UnicodeEncodeError AFTER the files had moved — so
    the caller saw a failed migration that had in fact happened."""
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(message.encode(encoding, "replace").decode(encoding),
              flush=True)


def _guarded(log: Optional[LogFn]) -> LogFn:
    """The caller's logger (default: print). One that fails costs the line,
    never the work it is reporting on."""
    target = log or _print

    def say(message: str) -> None:
        try:
            target(message)
        except Exception:                                 # noqa: BLE001
            pass
    return say


def migration_enabled() -> bool:
    """False when COUNCIL_SKIP_PATH_MIGRATION is set to anything but 0 —
    the Tk engine's own test, kept exactly."""
    return os.environ.get("COUNCIL_SKIP_PATH_MIGRATION", "").strip() in ("",
                                                                       "0")


def ensure_folders(vault_dir: Path) -> None:
    """The vault and the three folders the engine writes into from the start
    (logs/, workspace/, tmp/). Idempotent."""
    vault = Path(vault_dir)
    for folder in (vault, vault / "logs", vault / "workspace", vault / "tmp"):
        folder.mkdir(parents=True, exist_ok=True)


def legacy_moves(vault_dir: Path, *, app_dir: Path,
                 repo_root: Path) -> List[Tuple[Path, Path]]:
    """(old location, new location) for each thing an older build kept
    outside the vault. Moved verbatim from council_gui_engine."""
    vault, app = Path(vault_dir), Path(app_dir)
    return [
        (app / "council.log",               vault / "logs" / "council.log"),
        (app / "node_registry.json",        vault / "node_registry.json"),
        (app / "personality_backends.json", vault / "personality_backends.json"),
        (app / "workspace",                 vault / "workspace"),
        (app / "graph_output",              vault / "graph_output"),
        (app / ".chromadb",                 vault / ".chromadb"),
        # dream3d docs scraped next to the script
        (Path(repo_root) / "vault" / "dream3d_docs", vault / "dream3d_docs"),
    ]


def migrate_legacy_paths(vault_dir: Path, *, app_dir: Path,
                         repo_root: Optional[Path] = None,
                         log: Optional[LogFn] = None) -> List[str]:
    """Move an older build's files into the vault. Safe to run every start:
    anything already moved, or already present in the vault, is skipped — the
    vault's copy is never overwritten. Returns a line per thing moved.

    NOTE: an EMPTY folder in the vault counts as "already present". Tk's rule,
    kept: ensure_folders makes workspace/ before this runs in Tk's import
    order too, so an old ~/.council/workspace was never moved there either.
    """
    log = _guarded(log)
    vault = Path(vault_dir)
    moved: List[str] = []
    for old, new in legacy_moves(vault, app_dir=app_dir,
                                 repo_root=repo_root or REPO_ROOT):
        if not old.exists() or old == new:
            continue
        if new.exists():
            continue                 # the vault already has one: keep it
        try:
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old), str(new))
            moved.append(f"  {old.name} → vault/{new.relative_to(vault)}")
        except Exception as exc:                          # noqa: BLE001
            log(f"[Migration] Could not move {old.name}: {exc}")
    if moved:
        log("[Migration] Moved old data into vault:")
        for line in moved:
            log(line)
    return moved


@dataclass
class DataDirs:
    """What prepare_data_dirs did, for a caller that wants to say so."""
    cleaned: List[Path] = field(default_factory=list)
    copied: List[Path] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)


#: App state the app writes to the vault ROOT, beside the user's loose data,
#: that data_index's own skip list (_APP_INTERNAL_FILENAMES) does not name.
#: FOUND IN REVIEW: one Qt launch copied backend_settings.json (the GGUF path),
#: model_slots.json, vault_index.json and vault_embeddings.json into data_in/,
#: and the data index then offered them as the user's datasets — every user
#: who sets a model. Tk did the same through the same call; the real vault
#: here has data_in/vault_index.json and semantic_cache.json from it.
#: data_index.py is another branch's to change, so the extra names live here
#: until its list is extended. Each one checked to be written at the root.
VAULT_ROOT_STATE = frozenset({
    "backend_settings.json",      # onboarding / engine settings
    "model_slots.json",           # council_core.model_slots
    "model_bench.json",           # council_core.pc_check
    "vault_index.json",           # vault_index.INDEX_FILENAME
    "vault_embeddings.json",      # vault_embeddings.EMBEDDINGS_FILENAME
    "semantic_cache.json",        # vault_index.SEMANTIC_CACHE_FILENAME
    "fuzzy_denylist.json",        # vault_index.DENYLIST_FILENAME
    "graph_presets.json",         # the Grapher's presets
})


def app_state_names() -> frozenset:
    """Every basename at the vault root that is the app's, not the user's:
    VAULT_ROOT_STATE, data_index's skip list, and
    conversation_logger.PROTECTED_STATE_FILES — the list the vault search
    already treats as app state (vault_index reads it the same way), so a
    name added there is never copied in as data either. Lower-cased."""
    names = set(VAULT_ROOT_STATE)
    try:
        import data_index
        names |= set(getattr(data_index, "_APP_INTERNAL_FILENAMES", ()))
    except Exception:                                     # noqa: BLE001
        pass
    try:
        from conversation_logger import PROTECTED_STATE_FILES
        names |= set(PROTECTED_STATE_FILES)
    except Exception:                                     # noqa: BLE001
        pass
    return frozenset(name.lower() for name in names)


def copy_loose_data_files(vault_dir: Path) -> List[Path]:
    """data_index.migrate_loose_vault_files' rule — CSV/TSV/JSON files at
    the vault root (not in a subfolder) copied into data_in/, the originals
    kept, nothing in data_in/ overwritten — skipping app_state_names().
    Returns the root files copied. Written out here rather than called
    because that function has no way to be told about more names."""
    import data_index

    vault = Path(vault_dir)
    if not vault.is_dir():
        return []
    skip = app_state_names()
    target = data_index.input_dir(vault)
    target.mkdir(parents=True, exist_ok=True)
    copied: List[Path] = []
    for path in sorted(vault.iterdir()):
        if (not path.is_file()
                or path.suffix.lower() not in (".csv", ".tsv", ".json")
                or path.name.lower() in skip):
            continue
        dest = target / path.name
        if dest.exists():
            continue                     # never overwrite what is in data_in/
        try:
            shutil.copy2(path, dest)
            copied.append(path)
        except Exception:                                 # noqa: BLE001
            pass
    return copied


def remove_state_copies(vault_dir: Path) -> List[Path]:
    """Take out a copy of a VAULT_ROOT_STATE file that an earlier start put
    in data_in/ — ONLY when it is byte-identical to the app's file still at
    the root, data_index.cleanup_misplaced_internals' proof. A data_in/ file
    of that name with other content is the user's, and is kept."""
    import filecmp

    import data_index

    vault = Path(vault_dir)
    folder = data_index.input_dir(vault)
    removed: List[Path] = []
    if not folder.is_dir():
        return removed
    for name in sorted(VAULT_ROOT_STATE):
        copy, original = folder / name, vault / name
        if not copy.is_file() or not original.is_file():
            continue
        try:
            if filecmp.cmp(copy, original, shallow=False):
                copy.unlink()
                removed.append(copy)
        except Exception:                                 # noqa: BLE001
            pass
    return removed


def prepare_data_dirs(vault_dir: Path,
                      log: Optional[LogFn] = None) -> DataDirs:
    """data_in/ and data_out/ with their READMEs, the stray-config sweep, and
    the copy of loose root data files into data_in/ — CouncilConsole.__init__'s
    three steps, in its order, with its log lines. Each step is guarded on its
    own, as there: one failing must not stop the next.

    The sweep and the copy also know the app's own vault-root state
    (VAULT_ROOT_STATE): without that, the copy filed the app's settings in
    data_in/ as the user's data."""
    import data_index

    log = _guarded(log)
    vault = Path(vault_dir)
    out = DataDirs()
    try:
        data_index.init_data_dirs(vault)
    except Exception as exc:                              # noqa: BLE001
        out.problems.append(f"data folders: {exc!r}")
        log(f"[DataIndex] Could not create data_in/ and data_out/: {exc}")
    try:
        out.cleaned = (list(data_index.cleanup_misplaced_internals(vault))
                       + remove_state_copies(vault))
        if out.cleaned:
            log(f"[DataIndex] Removed {len(out.cleaned)} stray app-config "
                f"file(s) from data_in/")
    except Exception as exc:                              # noqa: BLE001
        out.problems.append(f"cleanup: {exc!r}")
        log(f"[DataIndex] Cleanup skipped: {exc}")
    try:
        out.copied = copy_loose_data_files(vault)
        if out.copied:
            log(f"[DataIndex] Copied {len(out.copied)} loose data file(s) "
                f"from vault root into data_in/")
    except Exception as exc:                              # noqa: BLE001
        out.problems.append(f"loose files: {exc!r}")
        log(f"[DataIndex] Migration skipped: {exc}")
    return out


def prepare(vault_dir: Path, *, app_dir: Optional[Path] = None,
            repo_root: Optional[Path] = None,
            log: Optional[LogFn] = None) -> DataDirs:
    """Everything the Tk startup does to the vault, in its order: folders,
    then (unless COUNCIL_SKIP_PATH_MIGRATION) the legacy moves, then the data
    folders. For the Qt launch, which has no import-time half to split it
    across. Never raises: a vault that cannot be tidied still opens."""
    log = _guarded(log)
    vault = Path(vault_dir)
    try:
        ensure_folders(vault)
    except Exception as exc:                              # noqa: BLE001
        log(f"[startup] vault folders could not be created: {exc!r}")
    if migration_enabled():
        try:
            if app_dir is None:
                from . import paths
                app_dir = paths.app_dir()
            migrate_legacy_paths(vault, app_dir=Path(app_dir),
                                 repo_root=repo_root, log=log)
        except Exception as exc:                          # noqa: BLE001
            log(f"[Migration] skipped: {exc!r}")
    try:
        return prepare_data_dirs(vault, log=log)
    except Exception as exc:                              # noqa: BLE001
        log(f"[DataIndex] data folders skipped: {exc!r}")
        return DataDirs(problems=[repr(exc)])
