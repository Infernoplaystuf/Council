"""
council_core.vault_import — bringing files into the vault.

Zip extraction and folder copying, with the filter that decides what a vault
takes and what it leaves behind. Moved out of council_gui_engine VERBATIM: both
helpers had zero Tk in them, and the filter rules — which extensions count as
indexable, which directories are skipped, how big a single file may be — are
the accumulated answer to "what is worth keeping", not UI.

The 1 GiB per-file cap is deliberately a runaway guard rather than a data
limit; the comment on _import_max_bytes records that it replaced a 500 KB cap
which had been silently dropping real data files.
"""
from __future__ import annotations

import os
import shutil
import zipfile
from pathlib import Path
from typing import Callable, Optional, Tuple

LogFn = Callable[[str], None]

_IMPORT_INDEXABLE = {
    # text / code / config
    ".py", ".md", ".txt", ".json", ".yaml", ".yml", ".html", ".rst",
    ".csv", ".log", ".toml", ".ini", ".xml", ".cfg", ".conf", ".tex",
    ".r", ".m", ".ipynb",
    # tabular / structured data the analyst reads
    ".tsv", ".xlsx", ".xls", ".xlsm", ".parquet", ".feather", ".orc",
    ".arrow", ".db", ".sqlite", ".sqlite3", ".duckdb", ".bson",
    ".jsonl", ".ndjson", ".gz",
    # images (parsed for metadata / vision)
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff",
    # documents
    ".pdf",
}

_IMPORT_SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".venv", "venv",
                     "dist", "build", ".eggs", ".tox", ".idea", ".vscode"}

def _import_max_bytes() -> int:
    """Max size of a SINGLE file kept on import — a runaway guard, NOT a data
    limit. Default 1 GiB (vs the old 500 KB, which silently dropped any real
    data file). Override with COUNCIL_IMPORT_MAX_MB; set it very high to
    effectively disable the cap."""
    import os as _os
    ov = _os.environ.get("COUNCIL_IMPORT_MAX_MB", "").strip()
    if ov:
        try:
            return max(1, int(ov)) * 1024 * 1024
        except ValueError:
            pass
    return 1024 * 1024 * 1024

def _vmgr_extract_zip(
    zip_path: Path,
    *,
    vault_dir: Path,
    subfolder: str | None = None,
    log_cb=None,
) -> tuple:
    """
    Extract a zip archive into a vault subfolder, keeping only indexable files.
    Returns (dest_dir, copied_count, skipped_count).
    """
    import zipfile
    import shutil as _shutil
    import re as _re

    def _log(m):
        if log_cb: log_cb(m)
        else: print(m)

    INDEXABLE = _IMPORT_INDEXABLE
    SKIP_DIRS = _IMPORT_SKIP_DIRS
    MAX_BYTES = _import_max_bytes()

    if not subfolder:
        subfolder = zip_path.stem
    subfolder = _re.sub(r"[^A-Za-z0-9._-]", "_", subfolder) or "import"
    dest_dir = vault_dir / subfolder
    dest_dir.mkdir(parents=True, exist_ok=True)
    _dest_root = dest_dir.resolve()   # Zip Slip containment boundary

    if not zipfile.is_zipfile(zip_path):
        raise ValueError(f"{zip_path.name} is not a valid zip file")

    copied = skipped = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        members = [m for m in zf.infolist() if not m.filename.endswith("/")]
        _log(f"  {len(members)} files in archive")

        # Detect common top-level prefix to strip (e.g. "repo-main/")
        all_parts = [Path(m.filename).parts for m in members]
        strip_prefix = ""
        if all_parts and len(set(p[0] for p in all_parts if p)) == 1:
            strip_prefix = all_parts[0][0]

        for member in members:
            parts = Path(member.filename).parts
            if any(p in SKIP_DIRS for p in parts): skipped += 1; continue
            if any(p.startswith(".") for p in parts): skipped += 1; continue
            if Path(member.filename).suffix.lower() not in INDEXABLE: skipped += 1; continue
            if member.file_size > MAX_BYTES:
                _log(f"  SKIP (too large {member.file_size//1024}KB): {member.filename}")
                skipped += 1; continue

            # Strip the common prefix so files land at vault/subfolder/file, not vault/subfolder/repo-main/file
            rel_parts = parts[1:] if (strip_prefix and parts and parts[0] == strip_prefix) else parts
            if not rel_parts:
                rel_parts = (Path(member.filename).name,)
            dest_file = dest_dir / Path(*rel_parts)
            # ── Zip Slip guard ──────────────────────────────────────────
            # zf.open()+manual write bypasses ZipFile.extractall()'s built-in
            # sanitisation, so a crafted entry with an ABSOLUTE path (pathlib
            # resets the join) or ../ / symlink parts could write OUTSIDE the
            # target. Refuse anything that doesn't resolve inside dest_dir.
            try:
                _resolved = dest_file.resolve()
                _resolved.relative_to(_dest_root)
            except (ValueError, OSError):
                _log(f"  SKIP (unsafe path escapes target): {member.filename}")
                skipped += 1
                continue
            dest_file = _resolved
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(dest_file, "wb") as dst:
                _shutil.copyfileobj(src, dst)
            copied += 1

    return dest_dir, copied, skipped

def _vmgr_copy_folder(
    src: Path,
    *,
    vault_dir: Path,
    subfolder: str | None = None,
    log_cb=None,
) -> tuple:
    """
    Copy a local folder into the vault, keeping only indexable files.
    Returns (dest_dir, copied_count, skipped_count).
    """
    import shutil as _shutil
    import re as _re

    def _log(m):
        if log_cb: log_cb(m)
        else: print(m)

    INDEXABLE = _IMPORT_INDEXABLE
    SKIP_DIRS = _IMPORT_SKIP_DIRS
    MAX_BYTES = _import_max_bytes()

    if not subfolder:
        subfolder = src.name
    subfolder = _re.sub(r"[^A-Za-z0-9._-]", "_", subfolder) or "import"
    dest_dir = vault_dir / subfolder
    dest_dir.mkdir(parents=True, exist_ok=True)

    copied = skipped = 0
    for src_file in src.rglob("*"):
        if not src_file.is_file(): continue
        rel = src_file.relative_to(src)
        if any(p in SKIP_DIRS for p in rel.parts): skipped += 1; continue
        if any(p.startswith(".") for p in rel.parts): skipped += 1; continue
        if src_file.suffix.lower() not in INDEXABLE: skipped += 1; continue
        try:
            if src_file.stat().st_size > MAX_BYTES: skipped += 1; continue
        except OSError:
            skipped += 1; continue
        dest_file = dest_dir / rel
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        _shutil.copy2(src_file, dest_file)
        copied += 1

    _log(f"  Copied {copied} files from {src.name}")
    return dest_dir, copied, skipped


# Public names. The private spellings above are kept exactly as they were so
# the move is a move; these are what new code should use.
INDEXABLE = _IMPORT_INDEXABLE
SKIP_DIRS = _IMPORT_SKIP_DIRS
max_bytes = _import_max_bytes
extract_zip = _vmgr_extract_zip
copy_folder = _vmgr_copy_folder


# ============================================================
# The operations, as both front ends need them
# ============================================================


class ImportResult:
    """What an import did. A class rather than a tuple because the batch case
    reports four numbers and the single case two, and unpacking the wrong
    arity at a call site is the kind of bug that only shows up on the sad
    path."""

    __slots__ = ("ok", "message", "dest", "copied", "skipped", "failed",
                 "error")

    def __init__(self, ok, message, dest=None, copied=0, skipped=0, failed=0,
                 error=None):
        self.ok = ok
        self.message = message
        self.dest = dest
        self.copied = copied
        self.skipped = skipped
        self.failed = failed
        self.error = error

    def __repr__(self):                                    # pragma: no cover
        return (f"ImportResult(ok={self.ok!r}, copied={self.copied}, "
                f"skipped={self.skipped}, failed={self.failed}, "
                f"message={self.message!r})")


def check_zip(zip_path: str) -> Optional[str]:
    """Why this zip cannot be imported, or None."""
    zip_path = (zip_path or "").strip()
    if not zip_path:
        return "✗ Please select a zip file first."
    if not Path(zip_path).exists():
        return f"✗ File not found: {zip_path}"
    return None


def check_folder(folder: str, what: str = "folder") -> Optional[str]:
    folder = (folder or "").strip()
    if not folder:
        return (f"✗ Pick a folder of zip files first." if what == "zips"
                else "✗ Please select a folder first.")
    path = Path(folder)
    if not path.exists() or not path.is_dir():
        return f"✗ Folder not found: {folder}"
    return None


def import_zip(zip_path, *, vault_dir: Path, subfolder: str = "",
               log: Optional[LogFn] = None) -> ImportResult:
    """Extract one zip into a vault subfolder, keeping only indexable files."""
    problem = check_zip(str(zip_path))
    if problem:
        return ImportResult(False, problem)
    zip_path = Path(str(zip_path).strip())
    subfolder = (subfolder or "").strip() or zip_path.stem
    try:
        dest, copied, skipped = extract_zip(zip_path, vault_dir=Path(vault_dir),
                                            subfolder=subfolder, log_cb=log)
    except Exception as exc:                               # noqa: BLE001
        return ImportResult(False, f"✗ Extraction failed: {exc}", error=exc)
    return ImportResult(
        True,
        f"✓ Extracted {copied} files → vault/{dest.name}  ({skipped} skipped)",
        dest=dest, copied=copied, skipped=skipped)


def import_folder(folder, *, vault_dir: Path,
                  log: Optional[LogFn] = None) -> ImportResult:
    """Copy a local folder into the vault, keeping only indexable files."""
    problem = check_folder(str(folder))
    if problem:
        return ImportResult(False, problem)
    src = Path(str(folder).strip())
    try:
        dest, copied, skipped = copy_folder(src, vault_dir=Path(vault_dir),
                                            subfolder=src.name, log_cb=log)
    except Exception as exc:                               # noqa: BLE001
        return ImportResult(False, f"✗ Copy failed: {exc}", error=exc)
    return ImportResult(
        True,
        f"✓ Copied {copied} files → vault/{dest.name}  ({skipped} skipped)",
        dest=dest, copied=copied, skipped=skipped)


def import_zip_folder(folder, *, input_dir: Path,
                      log: Optional[LogFn] = None) -> ImportResult:
    """Extract EVERY .zip under ``folder`` (recursively), each into its own
    subfolder of ``input_dir``.

    Three behaviours here are load-bearing and were previously buried in the
    Tk handler:

      * each zip is VALIDATED before extraction, so a corrupt or misnamed file
        does not leave an empty subfolder behind;
      * two zips may share a stem, so subfolder names are made unique;
      * one bad zip is logged and skipped — the rest still import. A batch that
        aborts on the first failure is the reason people stop using batches.

    ``input_dir`` rather than the vault root because that is the analyst's
    scope: files extracted to the root are not picked up by the index.
    """
    problem = check_folder(str(folder), what="zips")
    if problem:
        return ImportResult(False, problem)
    src = Path(str(folder).strip())

    def _log(message: str) -> None:
        if log:
            log(message)

    try:
        zips = sorted(src.rglob("*.zip"))
    except Exception as exc:                               # noqa: BLE001
        return ImportResult(False, f"✗ Could not scan folder: {exc}", error=exc)
    if not zips:
        return ImportResult(True, f"No .zip files found under {src.name}.")

    input_dir = Path(input_dir)
    input_dir.mkdir(parents=True, exist_ok=True)
    _log(f"Found {len(zips)} zip(s) under {src.name} — extracting…")

    ok = total_copied = failed = 0
    used: set = set()
    for i, archive in enumerate(zips, 1):
        if not zipfile.is_zipfile(archive):
            failed += 1
            _log(f"  [{i}/{len(zips)}] ✗ {archive.name}: not a valid zip — skipped")
            continue
        sub = archive.stem
        while sub in used:
            sub = f"{archive.stem}_{i}"
        used.add(sub)
        try:
            dest, copied, skipped = extract_zip(archive, vault_dir=input_dir,
                                                subfolder=sub, log_cb=log)
            ok += 1
            total_copied += copied
            _log(f"  [{i}/{len(zips)}] ✓ {archive.name} → data_in/{dest.name} "
                 f"({copied} files, {skipped} skipped)")
        except Exception as exc:                           # noqa: BLE001
            failed += 1
            _log(f"  [{i}/{len(zips)}] ✗ {archive.name}: {exc}")

    tail = f", {failed} failed" if failed else ""
    return ImportResult(
        ok > 0 or failed == 0,
        f"Done — {ok}/{len(zips)} zip(s) extracted, {total_copied} files total{tail}.",
        copied=total_copied, failed=failed)
