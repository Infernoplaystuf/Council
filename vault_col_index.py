"""
vault_col_index.py — persistent ``{column_name → [file_paths]}`` index.

The data-analyst stack repeatedly answers questions like:
  • "which files have a 'customer_id' column?"
  • "find all CSVs that have both 'revenue' and 'date' columns"
  • "what columns are shared across these three files?"

Before this module, every such call walked every file profile and
scanned every column name (O(files × columns) per query). On a vault
with 100 files × 30 columns per file, that's 3000 string comparisons
per query — fine alone, but it happens 5-10 times per analyst turn.

This module flips the inner loop: build the inverted index once,
serve every subsequent lookup in O(1). Persists to disk so the next
app launch doesn't have to re-profile every file just to rebuild
the index.

Storage layout
--------------
Sidecar JSON at ``vault/.vault_col_index.json``::

    {
      "version": 1,
      "files": {
        "/abs/path/orders.csv": {
          "mtime": 1730000000.12,
          "columns": ["order_id", "customer_id", "total"]
        },
        ...
      }
    }

Concurrency
-----------
Single-writer, single-reader. The DataIndex is the only caller and
its refresh() is single-threaded. The on-disk format is rewritten
atomically (write to ``.tmp`` then os.replace) so a crash mid-save
leaves the previous index intact.
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


INDEX_FILENAME = ".vault_col_index.json"
SCHEMA_VERSION = 1


def _norm(col: str) -> str:
    """Canonical form for column matching: stripped + lowercased.

    Whitespace inside a name is preserved because real data has
    columns like 'Order Total' that we want to match without
    re-tokenising the user's query.
    """
    return str(col or "").strip().lower()


# ============================================================
# ColumnIndex
# ============================================================

class ColumnIndex:
    """Inverted index of column names → file paths.

    Public surface:
      - ``update(path, columns)`` — refresh entry for one file
      - ``remove(path)``         — drop an entry (file deleted)
      - ``find_files(col, exact=False)`` — single-column lookup
      - ``find_files_with_columns(cols, all_required=True)`` —
        schema-fingerprint lookup
      - ``columns_for(path)``    — reverse lookup
      - ``stats()``              — diagnostic snapshot
      - ``save()`` / ``load()``  — persistence
    """

    def __init__(self, vault_dir: Any):
        self.vault_dir = Path(vault_dir).expanduser().resolve()
        self.path = self.vault_dir / INDEX_FILENAME
        # Forward index: per-file entry = {"mtime": float, "columns": [str]}
        self._files: Dict[str, Dict[str, Any]] = {}
        # Inverted index: normalised column → set of file paths
        # Rebuilt from _files on load() and incrementally on update().
        self._by_col: Dict[str, set] = {}
        self._lock = threading.RLock()
        self.load()

    # ----------------------------------------------------------------
    # Persistence
    # ----------------------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[ColumnIndex] could not load {self.path}: {exc!r}")
            return
        if not isinstance(data, dict) or data.get("version") != SCHEMA_VERSION:
            return
        files = data.get("files") or {}
        if not isinstance(files, dict):
            return
        with self._lock:
            self._files = {
                k: v for k, v in files.items()
                if isinstance(v, dict) and isinstance(v.get("columns"), list)
            }
            self._rebuild_inverted_locked()

    def save(self) -> None:
        """Atomic write — temp file + os.replace."""
        with self._lock:
            payload = {
                "version": SCHEMA_VERSION,
                "files": self._files,
            }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            self.vault_dir.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except Exception as exc:
            print(f"[ColumnIndex] save failed: {exc!r}")
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass

    # ----------------------------------------------------------------
    # Mutations
    # ----------------------------------------------------------------

    def update(self, path: Any, columns: Iterable[str],
                mtime: Optional[float] = None) -> bool:
        """Refresh ``path``'s entry. Returns True if something changed.

        Callers typically invoke this from a file-profiler's hot path
        with the columns it just parsed. The mtime is auto-detected
        from the filesystem if not supplied.
        """
        spath = str(Path(path))
        cols_clean = [str(c) for c in columns if str(c).strip()]
        if mtime is None:
            try:
                mtime = Path(path).stat().st_mtime
            except OSError:
                mtime = 0.0
        with self._lock:
            existing = self._files.get(spath)
            if (existing
                    and existing.get("mtime") == mtime
                    and existing.get("columns") == cols_clean):
                return False
            # Drop the previous inverted entries for this file
            if existing:
                self._strip_from_inverted_locked(spath, existing.get("columns") or [])
            self._files[spath] = {"mtime": mtime, "columns": cols_clean}
            self._add_to_inverted_locked(spath, cols_clean)
            return True

    def remove(self, path: Any) -> bool:
        """Drop a file's entry. Returns True if it was present."""
        spath = str(Path(path))
        with self._lock:
            existing = self._files.pop(spath, None)
            if existing is None:
                return False
            self._strip_from_inverted_locked(spath, existing.get("columns") or [])
            return True

    def _add_to_inverted_locked(self, spath: str, cols: List[str]) -> None:
        for c in cols:
            n = _norm(c)
            if not n:
                continue
            bucket = self._by_col.get(n)
            if bucket is None:
                bucket = set()
                self._by_col[n] = bucket
            bucket.add(spath)

    def _strip_from_inverted_locked(self, spath: str, cols: List[str]) -> None:
        for c in cols:
            n = _norm(c)
            bucket = self._by_col.get(n)
            if bucket is None:
                continue
            bucket.discard(spath)
            if not bucket:
                self._by_col.pop(n, None)

    def _rebuild_inverted_locked(self) -> None:
        self._by_col = {}
        for spath, entry in self._files.items():
            self._add_to_inverted_locked(spath, entry.get("columns") or [])

    # ----------------------------------------------------------------
    # Queries
    # ----------------------------------------------------------------

    def find_files(
        self,
        column_name: str,
        *,
        exact: bool = False,
    ) -> List[Tuple[Path, str]]:
        """Files containing a column matching ``column_name``.

        ``exact=False`` (default) — substring match on the column
        name. ``exact=True`` — only exact normalised-equal matches.

        Returns ``[(file_path, exact_column_name), ...]`` so callers
        can show the original column name back to the user.
        """
        target = _norm(column_name)
        if not target:
            return []
        with self._lock:
            if exact:
                hits = self._by_col.get(target, set())
                out: List[Tuple[Path, str]] = []
                for spath in hits:
                    cols = self._files.get(spath, {}).get("columns") or []
                    for c in cols:
                        if _norm(c) == target:
                            out.append((Path(spath), c))
                            break
                return out
            # Substring path — iterate normalised keys
            out = []
            for col_key, hits in self._by_col.items():
                if target not in col_key:
                    continue
                for spath in hits:
                    cols = self._files.get(spath, {}).get("columns") or []
                    for c in cols:
                        if _norm(c) == col_key:
                            out.append((Path(spath), c))
                            break
            return out

    def find_files_with_columns(
        self,
        column_names: Iterable[str],
        *,
        all_required: bool = True,
        exact: bool = False,
    ) -> List[Path]:
        """Schema-fingerprint search: files whose columns include the
        requested set.

        ``all_required=True`` (default) — every requested column must
        be present (AND). ``False`` — any one is enough (OR).
        ``exact`` controls per-column matching (see ``find_files``).
        """
        targets = [_norm(c) for c in column_names if _norm(c)]
        if not targets:
            return []
        with self._lock:
            sets: List[set] = []
            for t in targets:
                if exact:
                    sets.append(set(self._by_col.get(t, set())))
                else:
                    hits: set = set()
                    for col_key, bucket in self._by_col.items():
                        if t in col_key:
                            hits.update(bucket)
                    sets.append(hits)
        if not sets:
            return []
        result = sets[0]
        op = (lambda a, b: a & b) if all_required else (lambda a, b: a | b)
        for s in sets[1:]:
            result = op(result, s)
        return sorted(Path(s) for s in result)

    def columns_for(self, path: Any) -> List[str]:
        """Reverse lookup — every column we know for a file."""
        spath = str(Path(path))
        with self._lock:
            entry = self._files.get(spath)
            return list(entry.get("columns") or []) if entry else []

    def known_files(self) -> List[Path]:
        with self._lock:
            return [Path(s) for s in self._files]

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "n_files":   len(self._files),
                "n_columns": len(self._by_col),
                "path":      str(self.path),
            }
