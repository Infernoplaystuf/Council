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

NOTHING HERE IS NEW BEHAVIOUR
The move list, the "never overwrite what the vault already has" rule, and
COUNCIL_SKIP_PATH_MIGRATION (which tests/sandbox_vault.py sets, because the
test vault is deleted at the end of a run and a migration into it MOVED the
user's legacy files there — measured in a scratch home) are the Tk ones,
moved. The data-folder steps call data_index exactly as the console did.
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


def prepare_data_dirs(vault_dir: Path,
                      log: Optional[LogFn] = None) -> DataDirs:
    """data_in/ and data_out/ with their READMEs, the stray-config sweep, and
    the copy of loose root data files into data_in/ — CouncilConsole.__init__'s
    three steps, in its order, with its log lines. Each step is guarded on its
    own, as there: one failing must not stop the next."""
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
        out.cleaned = list(data_index.cleanup_misplaced_internals(vault))
        if out.cleaned:
            log(f"[DataIndex] Removed {len(out.cleaned)} stray app-config "
                f"file(s) from data_in/")
    except Exception as exc:                              # noqa: BLE001
        out.problems.append(f"cleanup: {exc!r}")
        log(f"[DataIndex] Cleanup skipped: {exc}")
    try:
        out.copied = list(data_index.migrate_loose_vault_files(vault))
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
