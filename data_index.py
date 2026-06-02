# ============================================================
# data_index.py  —  cross-file value/column/relationship search
# ============================================================
# A small in-memory index over every loadable data file in the
# configured input folders. Built lazily on first use, then
# refreshed on demand.
#
# Read/write separation (the safety guarantee):
#   • Reads happen ONLY from configured input paths — typically
#     vault/data_in/ and assets/sample_data/.
#   • Writes (derived data, exports, joined tables) go ONLY to
#     vault/data_out/ via safe_write_path().
#   • The two never overlap. The DataIndex constructor validates
#     this at startup and refuses to instantiate if they do.
#
# What this module does:
#   • Profile each input file: columns, types, row count, sample
#     values per column
#   • Find a value across all files (full-text scan with column hits)
#   • Find files that share a column name (relationship detection)
#   • Cross-reference: given (file, key_col, key_value), pull rows
#     from OTHER files where any column matches that value
#
# What this module does NOT do:
#   • Modify any input file (impossible by construction — the index
#     never opens an input file in write mode)
#   • Run actual SQL joins (we surface matching rows; the Council
#     interprets them)
#   • Index XLSX robustly — best-effort, CSV/TSV/JSON is the primary
#     target
# ============================================================

from __future__ import annotations

import csv
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


# ============================================================
# Folder constants — the read/write contract
# ============================================================
# These are subfolder *names*; resolve them against a vault root via
# input_dirs(vault) / output_dir(vault) below.

INPUT_SUBFOLDER  = "data_in"     # user puts CSVs here, app NEVER writes
OUTPUT_SUBFOLDER = "data_out"    # app writes here, never reads for index

# README content written into each folder so the user understands the
# split when they open them in a file browser.
_README_INPUT = """\
data_in/  —  read-only by Data's Inferno
==========================================

Drop your CSV / TSV / JSON files in here. The app reads them but will
NEVER overwrite, rename, or delete them. They're yours.

Supported formats:
  • .csv  (most common — what every accounting tool exports)
  • .tsv  (tab-separated)
  • .json (list-of-objects shape)

Files dropped here become searchable in the Council:
  • Cross-file value lookup ("find C1234")
  • Foreign-key style relationship detection (columns shared by 2+ files)
  • Auto-charting via the Grapher tab

Anything Data's Inferno produces from these files (charts, exports,
joined tables) goes to ../data_out/ — never back into this folder.
"""

_README_OUTPUT = """\
data_out/  —  app-managed output folder
==========================================

This is where Data's Inferno writes anything derived from your input
data:

  • charts/      — exported chart images
  • exports/     — CSV exports from the Grapher
  • joined/      — derived datasets when you join multiple inputs

You can clean this folder out any time without losing original data.
The originals live in ../data_in/ and are never touched.
"""


# ============================================================
# Folder-API helpers
# ============================================================

def input_dir(vault_dir: Path) -> Path:
    """The user's read-only input folder under the vault."""
    return Path(vault_dir) / INPUT_SUBFOLDER


def output_dir(vault_dir: Path) -> Path:
    """The app's write-only output folder under the vault."""
    return Path(vault_dir) / OUTPUT_SUBFOLDER


def bundled_samples_dir() -> Path:
    """The read-only sample-data folder shipped alongside the app."""
    return Path(__file__).parent / "assets" / "sample_data"


def init_data_dirs(vault_dir: Path) -> None:
    """
    Create vault/data_in/ and vault/data_out/ on first launch and drop
    a README in each so the user understands the split. Idempotent —
    safe to call on every start-up.
    """
    in_d  = input_dir(vault_dir)
    out_d = output_dir(vault_dir)
    in_d.mkdir(parents=True, exist_ok=True)
    out_d.mkdir(parents=True, exist_ok=True)
    # Common subfolders the app may write to
    (out_d / "charts").mkdir(exist_ok=True)
    (out_d / "exports").mkdir(exist_ok=True)
    # Write README only if not present (don't clobber user edits)
    rd_in  = in_d  / "README.txt"
    rd_out = out_d / "README.txt"
    if not rd_in.exists():
        try: rd_in.write_text(_README_INPUT, encoding="utf-8")
        except Exception: pass
    if not rd_out.exists():
        try: rd_out.write_text(_README_OUTPUT, encoding="utf-8")
        except Exception: pass


def is_under(child: Path, parent: Path) -> bool:
    """True if `child` is the same as `parent` or nested inside it."""
    try:
        c = Path(child).resolve()
        p = Path(parent).resolve()
    except OSError:
        return False
    try:
        c.relative_to(p)
        return True
    except ValueError:
        return False


# Filenames that are app-internal config and should never be migrated
# into the user's data_in/ folder, even though their extension matches
# the data-file extensions we look for (.json especially).
_APP_INTERNAL_FILENAMES = {
    "specialists.json",
    "license.json",
    "activation.json",
    "node_registry.json",
    "personality_backends.json",
    "council_instructions.json",
    "content_style.json",
    "idea_settings.json",
    "idea_model_config.json",
    "verdict_history.jsonl",
}


def migrate_loose_vault_files(vault_dir: Path,
                               extensions: Iterable[str] = (".csv", ".tsv", ".json"),
                               *, copy_only: bool = True) -> List[Path]:
    """
    Find any user data files at the vault root (NOT inside any subfolder)
    and put them into vault/data_in/ so the new pipeline can see them.

    Skips files whose name matches a known app-internal config so we
    don't pollute data_in/ with state like specialists.json.

    `copy_only=True` (the default) preserves originals at vault root —
    we never silently move user data. The caller can choose to clean up
    after confirming with the user.

    Returns the list of files migrated.
    """
    vault = Path(vault_dir)
    in_d  = input_dir(vault)
    in_d.mkdir(parents=True, exist_ok=True)
    moved: List[Path] = []
    if not vault.exists():
        return moved
    for p in vault.iterdir():
        if not p.is_file():
            continue
        if p.suffix.lower() not in {e.lower() for e in extensions}:
            continue
        if p.name in _APP_INTERNAL_FILENAMES:
            continue
        dest = in_d / p.name
        if dest.exists():
            continue   # don't overwrite anything already in data_in/
        try:
            if copy_only:
                import shutil
                shutil.copy2(p, dest)
            else:
                p.rename(dest)
            moved.append(p)
        except Exception:
            pass
    return moved


def cleanup_misplaced_internals(vault_dir: Path) -> List[Path]:
    """
    A previous version of migrate_loose_vault_files copied app-internal
    config files (specialists.json, node_registry.json, etc.) into
    data_in/. This helper removes them from data_in/ — they're already
    living at the proper place under vault/, so deleting the data_in/
    copy is safe.

    Returns the list of removed paths.
    """
    in_d = input_dir(Path(vault_dir))
    if not in_d.exists():
        return []
    removed: List[Path] = []
    for name in _APP_INTERNAL_FILENAMES:
        p = in_d / name
        if not p.exists():
            continue
        try:
            p.unlink()
            removed.append(p)
        except Exception:
            pass
    return removed


# ============================================================
# Profile types
# ============================================================

@dataclass
class ColumnInfo:
    name:           str
    sample_values:  List[str] = field(default_factory=list)   # up to 8
    distinct_count: int = 0
    null_count:     int = 0
    inferred_type:  str = "text"     # text | number | date | bool


@dataclass
class FileProfile:
    path:        Path
    name:        str                              # filename only, for display
    extension:   str
    row_count:   int = 0
    columns:     List[ColumnInfo] = field(default_factory=list)
    error:       str = ""
    indexed_at:  float = 0.0    # epoch seconds — for cache invalidation
    # Raw rows kept ONLY for files small enough to be useful for lookup.
    # Capped at MAX_ROWS_FULL_INDEX so the vault as a whole stays manageable.
    rows:        List[Dict[str, str]] = field(default_factory=list)


# ============================================================
# Index
# ============================================================

class DataIndex:
    """
    Profile + light cache of every CSV/TSV/JSON we can load. Pure
    stdlib — no pandas dependency so it works on a stripped install.
    """

    LOADABLE_EXTS  = {".csv", ".tsv", ".json"}
    MAX_ROWS_FULL_INDEX = 5000   # rows kept in memory per file
    MAX_FILE_BYTES      = 25 * 1024 * 1024   # 25 MB hard cap
    MAX_SAMPLES_PER_COL = 8

    # Type-detection regexes — kept small and generous
    _RX_INT   = re.compile(r"^-?\d+$")
    _RX_FLOAT = re.compile(r"^-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$")
    _RX_DATE  = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")
    _RX_BOOL  = re.compile(r"^(true|false|yes|no|y|n|1|0)$", re.IGNORECASE)

    def __init__(self, search_roots: Iterable[Path],
                 *, write_root: Optional[Path] = None):
        """
        Construct the index.

          search_roots — folders to read from (read-only by contract)
          write_root   — folder for derived outputs. Used by safe_write_path()
                          and validated against search_roots so a misconfig
                          cannot cause writes into the input area.

        Raises ValueError if any search root overlaps with the write root.
        That's the safety guarantee — there is no way to construct an
        index that could write back over a configured input.
        """
        self.search_roots: List[Path] = [Path(r) for r in search_roots]
        self.write_root: Optional[Path] = Path(write_root) if write_root else None

        # Enforce: no input root may sit underneath the write root, and
        # the write root may not sit underneath any input root.
        if self.write_root is not None:
            for r in self.search_roots:
                if is_under(r, self.write_root):
                    raise ValueError(
                        f"DataIndex misconfig: input root {r} is inside "
                        f"the write root {self.write_root} — input data "
                        f"could be overwritten by the app.")
                if is_under(self.write_root, r):
                    raise ValueError(
                        f"DataIndex misconfig: write root {self.write_root} "
                        f"is inside input root {r} — derived outputs would "
                        f"be picked up as inputs on the next refresh.")

        self._profiles: Dict[Path, FileProfile] = {}

        # ── Warm-start: try to restore the pickle sidecar so the
        # first all_profiles() / find_files_with_column call doesn't
        # have to re-profile every file from cold.  The mtime-delta
        # check inside refresh() still re-profiles anything that
        # actually changed since the cache was written.
        try:
            import data_index_cache as _dic
            if self.search_roots:
                cached = _dic.try_load(self.search_roots[0])
                if cached:
                    # Only adopt entries whose paths are inside our
                    # search roots — defensive against a cache file
                    # left over from a different vault configuration.
                    for path, profile in cached.items():
                        if any(is_under(path, r) for r in self.search_roots):
                            self._profiles[path] = profile
        except Exception as exc:
            print(f"[DataIndex] warm-start cache load failed: {exc!r}")

        # ── find_relationships memo ──────────────────────────────
        # Result is keyed by a hash of (path, mtime) pairs so a
        # changed-file invalidates the cache without forcing a full
        # walk. Cleared on refresh() too.
        self._rel_cache: Optional[List[Dict[str, Any]]] = None
        self._rel_cache_key: Optional[int] = None

        # ── Inverted column index ────────────────────────────────
        # find_files_with_column used to iterate every profile and
        # every column on each call (O(files × cols)). The column
        # index serves the same query in O(1) average via a
        # persistent {col_name_lower → file_paths} dict. The first
        # search root is used as the "vault dir" for the sidecar
        # JSON file. A misconfig that leaves search_roots empty
        # disables the index gracefully (find_files_with_column
        # falls back to the linear scan path).
        self._col_index = None
        try:
            if self.search_roots:
                import vault_col_index as _vci
                self._col_index = _vci.ColumnIndex(self.search_roots[0])
        except Exception as exc:
            print(f"[DataIndex] could not init column index: {exc!r}")
            self._col_index = None

    # ---- Read-only path validator ----------------------------------

    def is_input_path(self, path: Path) -> bool:
        """True if `path` is inside any configured input root."""
        for r in self.search_roots:
            if is_under(path, r):
                return True
        return False

    def safe_write_path(self, filename: str, *, subfolder: str = "") -> Path:
        """
        Return a fully-qualified path under the write root. Refuses to
        produce a path that would land inside any input root, and refuses
        traversal sequences in `filename`.

        Use for any derived output (joined CSVs, exports, etc.):
            out = idx.safe_write_path("orders_with_returns.csv",
                                       subfolder="joined")
            out.write_text(...)
        """
        if self.write_root is None:
            raise RuntimeError(
                "DataIndex has no write_root configured — pass one to "
                "the constructor before calling safe_write_path().")
        # Normalise filename: only the basename allowed
        basename = os.path.basename(filename or "").strip()
        if not basename or basename in (".", ".."):
            raise ValueError(f"Invalid output filename: {filename!r}")
        # Subfolder may be a single name or a/b — but never an absolute
        # path or one with traversal segments
        sub = (subfolder or "").strip()
        if sub:
            sub_parts = [seg for seg in Path(sub).parts
                         if seg and seg not in (".", "..", "/", "\\")]
            target_dir = self.write_root.joinpath(*sub_parts)
        else:
            target_dir = self.write_root
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / basename
        # Triple-check: the resolved path must be under write_root and
        # not under any input root.
        if not is_under(target, self.write_root):
            raise RuntimeError(
                f"safe_write_path resolved outside write_root — refusing.")
        if self.is_input_path(target):
            raise RuntimeError(
                f"safe_write_path resolved inside an input root — refusing "
                f"to overwrite input data.")
        return target

    # ---- Discovery + loading ---------------------------------------

    def discover(self) -> List[Path]:
        """Walk the configured roots and return every loadable data path."""
        seen = set()
        out: List[Path] = []
        for root in self.search_roots:
            if not root.exists():
                continue
            for p in root.rglob("*"):
                if not p.is_file():
                    continue
                if p.suffix.lower() not in self.LOADABLE_EXTS:
                    continue
                # Skip vault internals
                s = str(p).replace("\\", "/")
                if any(skip in s for skip in
                       ("/.git/", "/.chromadb/", "/.cache/", "/node_modules/")):
                    continue
                # Skip oversized files
                try:
                    if p.stat().st_size > self.MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                if p in seen:
                    continue
                seen.add(p)
                out.append(p)
        return out

    def refresh(self) -> None:
        """Rebuild every profile. Skip files that haven't changed since last index.

        Also incrementally maintains the column index sidecar so the
        next launch can start with a warm O(1) lookup table.
        """
        changed = False
        for path in self.discover():
            cached = self._profiles.get(path)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if cached and cached.indexed_at >= mtime:
                continue
            profile = self._profile_file(path)
            self._profiles[path] = profile
            changed = True
            # Update the column index in step with the profile
            if self._col_index is not None:
                try:
                    col_names = [c.name for c in profile.columns]
                    self._col_index.update(path, col_names, mtime=mtime)
                except Exception as exc:
                    print(f"[DataIndex] col_index update failed for {path}: {exc!r}")
        # Drop profiles for files that no longer exist
        for path in list(self._profiles.keys()):
            if not path.exists():
                self._profiles.pop(path, None)
                if self._col_index is not None:
                    try:
                        if self._col_index.remove(path):
                            changed = True
                    except Exception:
                        pass
        if changed and self._col_index is not None:
            try:
                self._col_index.save()
            except Exception as exc:
                print(f"[DataIndex] col_index save failed: {exc!r}")
        # Persist profiles to the warm-start pickle. Sized so even a
        # 500-file vault fits in <50 MB on disk; the next launch
        # restores in <100 ms vs the 2-5 s cold-walk.
        if changed and self.search_roots:
            try:
                import data_index_cache as _dic
                _dic.save(self.search_roots[0], self._profiles)
            except Exception as exc:
                print(f"[DataIndex] warm-start cache save failed: {exc!r}")
        # Any change invalidates find_relationships' memoised result.
        if changed:
            self._rel_cache = None
            self._rel_cache_key = None

    def all_profiles(self) -> List[FileProfile]:
        if not self._profiles:
            self.refresh()
        return list(self._profiles.values())

    # ---- Profiling -------------------------------------------------

    def _profile_file(self, path: Path) -> FileProfile:
        ext = path.suffix.lower()
        prof = FileProfile(path=path, name=path.name, extension=ext,
                            indexed_at=time.time())
        try:
            if ext in (".csv", ".tsv"):
                self._profile_delimited(path, prof, delim="," if ext == ".csv" else "\t")
            elif ext == ".json":
                self._profile_json(path, prof)
            else:
                prof.error = f"Unsupported extension: {ext}"
        except Exception as e:
            prof.error = f"{type(e).__name__}: {e}"
        return prof

    def _profile_delimited(self, path: Path, prof: FileProfile, *, delim: str) -> None:
        """Read a CSV/TSV up to MAX_ROWS_FULL_INDEX, build column stats."""
        with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.DictReader(f, delimiter=delim)
            fieldnames = reader.fieldnames or []
            cols = {n: ColumnInfo(name=n) for n in fieldnames}
            seen_per_col: Dict[str, set] = {n: set() for n in fieldnames}

            for i, row in enumerate(reader):
                if i >= self.MAX_ROWS_FULL_INDEX:
                    # We continue counting rows for the row_count stat
                    # but don't keep them or sample further.
                    prof.row_count = i + 1
                    for _ in reader:
                        prof.row_count += 1
                    return
                prof.rows.append(row)
                prof.row_count = i + 1
                for col in fieldnames:
                    val = (row.get(col) or "").strip()
                    if val == "":
                        cols[col].null_count += 1
                        continue
                    if val not in seen_per_col[col]:
                        seen_per_col[col].add(val)
                        if len(cols[col].sample_values) < self.MAX_SAMPLES_PER_COL:
                            cols[col].sample_values.append(val)

        for n in fieldnames:
            ci = cols[n]
            ci.distinct_count = len(seen_per_col[n])
            ci.inferred_type  = self._infer_type(ci.sample_values)
        prof.columns = list(cols.values())

    def _profile_json(self, path: Path, prof: FileProfile) -> None:
        """JSON: only handle list-of-objects (the most common ad-hoc shape)."""
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:
            prof.error = f"JSON parse: {e}"
            return
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            prof.error = "JSON is not a list-of-objects (skipped for indexing)"
            return
        # Aggregate fieldnames across all records
        fieldnames: List[str] = []
        for rec in data:
            for k in rec.keys():
                if k not in fieldnames:
                    fieldnames.append(k)
        cols = {n: ColumnInfo(name=n) for n in fieldnames}
        seen_per_col: Dict[str, set] = {n: set() for n in fieldnames}
        for i, rec in enumerate(data):
            if i >= self.MAX_ROWS_FULL_INDEX:
                prof.row_count = len(data)
                break
            row_str = {k: ("" if v is None else str(v)) for k, v in rec.items()}
            prof.rows.append(row_str)
            prof.row_count = i + 1
            for col in fieldnames:
                val = row_str.get(col, "").strip()
                if val == "":
                    cols[col].null_count += 1
                    continue
                if val not in seen_per_col[col]:
                    seen_per_col[col].add(val)
                    if len(cols[col].sample_values) < self.MAX_SAMPLES_PER_COL:
                        cols[col].sample_values.append(val)
        for n in fieldnames:
            ci = cols[n]
            ci.distinct_count = len(seen_per_col[n])
            ci.inferred_type  = self._infer_type(ci.sample_values)
        prof.columns = list(cols.values())

    def _infer_type(self, samples: List[str]) -> str:
        """Very fast type detection from a small sample list."""
        if not samples:
            return "text"
        ints = floats = dates = bools = 0
        for s in samples:
            if self._RX_DATE.match(s):  dates  += 1
            elif self._RX_INT.match(s): ints   += 1
            elif self._RX_FLOAT.match(s): floats += 1
            elif self._RX_BOOL.match(s): bools  += 1
        n = len(samples)
        if dates  == n: return "date"
        if bools  == n: return "bool"
        if ints + floats == n: return "number"
        return "text"

    # ============================================================
    # Public queries
    # ============================================================

    def find_files_with_column(self, column_name: str
                                ) -> List[Tuple[FileProfile, str]]:
        """
        Files that have a column matching `column_name` (substring match,
        case-insensitive). Returns (profile, exact_column_name).

        Fast path: when the inverted ColumnIndex is available, this is
        O(distinct_cols + hits) rather than O(files × cols). The
        fallback linear scan still runs if the index is unavailable
        (early init) or hasn't been populated yet.
        """
        target = column_name.strip().lower()
        if not target:
            return []
        # ── Fast path via inverted index ──
        if self._col_index is not None:
            try:
                hits = self._col_index.find_files(target, exact=False)
                if hits:
                    out: List[Tuple[FileProfile, str]] = []
                    # Map (path, col) back to (profile, col). Files known
                    # to the col_index but not yet profiled in memory
                    # trigger an on-demand profile_file call so the
                    # caller gets a usable FileProfile back.
                    for path, exact_col in hits:
                        prof = self._profiles.get(path)
                        if prof is None:
                            # Lazy-profile so the caller always gets a
                            # FileProfile. Most callers will iterate
                            # the profile's columns anyway.
                            try:
                                prof = self._profile_file(path)
                                self._profiles[path] = prof
                            except Exception:
                                continue
                        out.append((prof, exact_col))
                    return out
            except Exception as exc:
                print(f"[DataIndex] col_index lookup failed; "
                      f"falling back to linear: {exc!r}")
        # ── Fallback: original linear scan ──
        out = []
        for prof in self.all_profiles():
            for col in prof.columns:
                if target in col.name.lower():
                    out.append((prof, col.name))
                    break
        return out

    def find_files_with_columns(
        self,
        column_names: Iterable[str],
        *,
        all_required: bool = True,
        exact: bool = False,
    ) -> List[FileProfile]:
        """Schema-fingerprint helper — files whose columns include the
        requested set.

        ``all_required=True`` (default): every requested column must be
        present (AND). ``False``: any one is enough (OR).
        ``exact``: require normalised-equal matching per column name
        instead of the default substring match.

        Use this when you know what shape you're looking for: "find
        every CSV that has at least 'order_id', 'customer_id', and
        'amount' columns" → ``find_files_with_columns(
        ['order_id', 'customer_id', 'amount'])``.
        """
        cols = [c for c in column_names if c and c.strip()]
        if not cols:
            return []
        if self._col_index is not None:
            try:
                paths = self._col_index.find_files_with_columns(
                    cols, all_required=all_required, exact=exact,
                )
                out: List[FileProfile] = []
                for path in paths:
                    prof = self._profiles.get(path)
                    if prof is None:
                        try:
                            prof = self._profile_file(path)
                            self._profiles[path] = prof
                        except Exception:
                            continue
                    out.append(prof)
                return out
            except Exception as exc:
                print(f"[DataIndex] schema-fingerprint lookup failed; "
                      f"falling back: {exc!r}")
        # Fallback: per-column linear search then intersect / union
        per_col_paths: List[set] = []
        for c in cols:
            hits = self.find_files_with_column(c)
            per_col_paths.append({p.path for p, _ in hits})
        if not per_col_paths:
            return []
        result = per_col_paths[0]
        op = (lambda a, b: a & b) if all_required else (lambda a, b: a | b)
        for s in per_col_paths[1:]:
            result = op(result, s)
        return [self._profiles[p] for p in result if p in self._profiles]

    def _profiles_signature(self) -> int:
        """A cheap, stable identity hash over the current profile set.

        Used to memo ``find_relationships``: when no profile has been
        added, removed, or re-indexed, return the cached result
        verbatim. The hash is over (path, indexed_at) pairs — same
        bits the existing refresh() delta-check already maintains.
        """
        return hash(tuple(sorted(
            (str(p), prof.indexed_at) for p, prof in self._profiles.items()
        )))

    def find_relationships(self) -> List[Dict[str, Any]]:
        """
        Detect candidate cross-file relationships by *exact* column name
        match. Returns a list of {column, files: [...]} entries — only
        columns that appear in 2+ files.

        Memoised against the current profile set; second calls in the
        same session return the cached list in O(1) instead of
        rebuilding the column→files map. ``refresh()`` invalidates
        the cache whenever profiles change.
        """
        sig = self._profiles_signature()
        if (self._rel_cache is not None
                and self._rel_cache_key == sig):
            return self._rel_cache

        # column_name → set of (path, exact_column)
        col_map: Dict[str, List[Tuple[FileProfile, str]]] = {}
        for prof in self.all_profiles():
            for col in prof.columns:
                key = col.name.strip().lower()
                if not key:
                    continue
                col_map.setdefault(key, []).append((prof, col.name))
        rels = []
        for key, hits in col_map.items():
            if len(hits) >= 2:
                rels.append({
                    "column":  key,
                    "files":   [{"name": p.name, "exact_column": c}
                                 for p, c in hits],
                    "examples": self._collect_examples(hits, max_n=5),
                })
        rels.sort(key=lambda r: (-len(r["files"]), r["column"]))
        self._rel_cache = rels
        self._rel_cache_key = sig
        return rels

    def _collect_examples(self, hits, max_n: int = 5) -> List[str]:
        seen = []
        for prof, col_name in hits:
            for c in prof.columns:
                if c.name == col_name:
                    for v in c.sample_values:
                        if v not in seen:
                            seen.append(v)
                            if len(seen) >= max_n:
                                return seen
                    break
        return seen

    def search_value(self, value: str, *, max_per_file: int = 25
                      ) -> List[Dict[str, Any]]:
        """
        Find rows where any cell contains `value` (case-insensitive
        substring). Returns a list of:
          {file, column_hits: [colname, ...], rows: [first_few_rows]}
        Only files that match are included.
        """
        needle = value.strip().lower()
        if not needle:
            return []
        results = []
        for prof in self.all_profiles():
            if prof.error:
                continue
            col_hits: set = set()
            matching_rows = []
            for row in prof.rows:
                hit = False
                row_hits: List[str] = []
                for col_name, cell in row.items():
                    if cell and needle in str(cell).lower():
                        hit = True
                        row_hits.append(col_name)
                if hit:
                    col_hits.update(row_hits)
                    if len(matching_rows) < max_per_file:
                        matching_rows.append(row)
            if matching_rows:
                results.append({
                    "file":          prof.name,
                    "path":          str(prof.path),
                    "row_count":     prof.row_count,
                    "matched_count": len(matching_rows),
                    "column_hits":   sorted(col_hits),
                    "rows":          matching_rows,
                })
        # Sort by match count desc — most relevant first
        results.sort(key=lambda r: -r["matched_count"])
        return results

    def lookup_related(self, source_file: str, key_value: str, *,
                       max_per_file: int = 25) -> Dict[str, Any]:
        """
        Given a value found in one file, find rows in *other* files that
        also reference this value. The classic small-business workflow
        ("show me everything about this customer / order / SKU").
        """
        all_hits = self.search_value(key_value, max_per_file=max_per_file)
        # The "source" is the file the user came from; everything else is "related"
        src = next((h for h in all_hits
                    if Path(h["path"]).name == source_file), None)
        related = [h for h in all_hits
                   if Path(h["path"]).name != source_file]
        return {
            "value":   key_value,
            "source":  src,
            "related": related,
        }

    # ---- LLM-friendly summary --------------------------------------

    def summary_for_council(self) -> str:
        """
        Compact text summary of the data index for injection into the
        Council's system context. Tells the panel what tables exist,
        their columns, and which columns connect them.
        """
        profiles = self.all_profiles()
        if not profiles:
            return "DATA INDEX: empty (no loadable files in vault)."
        lines = [f"DATA INDEX: {len(profiles)} loadable file(s) available."]
        for p in profiles:
            if p.error:
                lines.append(f"  • {p.name}  (skip: {p.error})")
                continue
            cols_summary = ", ".join(
                f"{c.name}({c.inferred_type})" for c in p.columns[:10]
            )
            lines.append(
                f"  • {p.name}  ({p.row_count} rows)  cols: {cols_summary}"
                + (" ..." if len(p.columns) > 10 else "")
            )
        rels = self.find_relationships()
        if rels:
            lines.append("")
            lines.append("LIKELY CROSS-FILE LINKS  (column appears in ≥2 files):")
            for r in rels[:8]:
                file_names = " · ".join(f["name"] for f in r["files"])
                lines.append(f"  • {r['column']}  →  {file_names}")
        return "\n".join(lines)
