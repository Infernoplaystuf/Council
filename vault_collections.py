"""
collections.py — virtual "project" collections over the vault.

A Collection is a NAMED set of files that belong together (e.g. "Job Blue":
a spreadsheet, a few CSVs, a photo, a PDF). It is VIRTUAL — a manifest, not
a folder move — so nothing is duplicated, a file can belong to several
collections, and it's reversible.

Two parts:
  • CollectionStore — persist {name -> [relative file paths]} in
    <vault>/collections.json (mirrors deferred_tasks.py's storage).
  • propose_members() — given a name like "Job Blue", suggest which vault
    files likely belong, combining signals: filename match, value match
    (the name appears as a value in the file, via the data index), and
    relationship expansion (files that share a join column with the matched
    set). The council proposes; the user confirms before saving.

Paths are stored RELATIVE to data_in so they survive vault moves and read
back cleanly. The store is pure + dependency-free; propose_members takes an
optional `index` (anything with search_value()/find_relationships()) so it's
testable without a live index.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_STORE_NAME = "collections.json"
_LOCK = threading.Lock()


def _vault_root(vault_dir: Optional[Any] = None) -> Path:
    if vault_dir is not None:
        return Path(vault_dir).expanduser().resolve()
    env = os.environ.get("COUNCIL_VAULT_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".council" / "vault"


def _input_dir(vault_dir: Optional[Any] = None) -> Path:
    """data_in/ under the vault — the analyst/index scope."""
    try:
        import data_index
        return data_index.input_dir(_vault_root(vault_dir))
    except Exception:
        return _vault_root(vault_dir) / "data_in"


def _norm_rel(p: Any, in_dir: Path) -> str:
    """A file path normalised to a forward-slash path relative to data_in.
    Accepts absolute paths, already-relative paths, or bare names."""
    if not p:
        return ""
    try:
        pp = Path(str(p))
        if pp.is_absolute():
            try:
                pp = pp.relative_to(in_dir)
            except ValueError:
                pp = Path(pp.name)
        return str(pp).replace("\\", "/").lstrip("./")
    except Exception:
        return str(p).replace("\\", "/")


@dataclass
class Collection:
    name: str
    files: List[str] = field(default_factory=list)   # relative to data_in
    note: str = ""
    created_ts: float = field(default_factory=time.time)
    updated_ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Collection":
        known = set(Collection.__dataclass_fields__)  # type: ignore
        return Collection(**{k: v for k, v in d.items() if k in known})


class CollectionStore:
    """Per-vault store of named file collections (collections.json)."""

    def __init__(self, vault_dir: Optional[Any] = None) -> None:
        self._vault = vault_dir
        self.path = _vault_root(vault_dir) / _STORE_NAME

    def _read(self) -> List[Dict[str, Any]]:
        try:
            if self.path.exists():
                d = json.loads(self.path.read_text(encoding="utf-8"))
                return d if isinstance(d, list) else []
        except Exception:
            pass
        return []

    def _write(self, items: List[Dict[str, Any]]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(items, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            pass

    def all(self) -> List[Collection]:
        return [Collection.from_dict(d) for d in self._read()]

    def names(self) -> List[str]:
        return [c.name for c in self.all()]

    def get(self, name: str) -> Optional[Collection]:
        nl = (name or "").strip().lower()
        for c in self.all():
            if c.name.lower() == nl:
                return c
        return None

    def upsert(self, name: str, files: List[str], *, note: str = "") -> Collection:
        """Create or replace the collection's file set (files normalised to
        data-in-relative paths, de-duplicated, order preserved)."""
        in_dir = _input_dir(self._vault)
        rels: List[str] = []
        seen = set()
        for f in files or []:
            r = _norm_rel(f, in_dir)
            if r and r.lower() not in seen:
                seen.add(r.lower())
                rels.append(r)
        with _LOCK:
            items = self._read()
            nl = (name or "").strip().lower()
            now = time.time()
            hit = None
            for it in items:
                if str(it.get("name", "")).lower() == nl:
                    hit = it
                    break
            if hit is not None:
                hit["files"] = rels
                hit["updated_ts"] = now
                if note:
                    hit["note"] = note
                col = Collection.from_dict(hit)
            else:
                col = Collection(name=name.strip(), files=rels, note=note,
                                 created_ts=now, updated_ts=now)
                items.append(col.to_dict())
            self._write(items)
        return col

    def add_files(self, name: str, files: List[str]) -> Optional[Collection]:
        c = self.get(name)
        if c is None:
            return self.upsert(name, files)
        return self.upsert(name, list(c.files) + list(files), note=c.note)

    def remove_files(self, name: str, files: List[str]) -> Optional[Collection]:
        c = self.get(name)
        if c is None:
            return None
        in_dir = _input_dir(self._vault)
        drop = {_norm_rel(f, in_dir).lower() for f in files}
        keep = [f for f in c.files if f.lower() not in drop]
        return self.upsert(name, keep, note=c.note)

    def rename(self, old: str, new: str) -> bool:
        with _LOCK:
            items = self._read()
            ol = (old or "").strip().lower()
            for it in items:
                if str(it.get("name", "")).lower() == ol:
                    it["name"] = new.strip()
                    it["updated_ts"] = time.time()
                    self._write(items)
                    return True
        return False

    def delete(self, name: str) -> bool:
        with _LOCK:
            items = self._read()
            nl = (name or "").strip().lower()
            new = [it for it in items if str(it.get("name", "")).lower() != nl]
            if len(new) != len(items):
                self._write(new)
                return True
        return False

    def find_in_text(self, text: str) -> Optional[Collection]:
        """Return a saved collection whose name appears in `text` (longest
        name wins) — used to detect "show me Job Blue" in a council query."""
        t = (text or "").lower()
        best = None
        for c in self.all():
            n = c.name.lower().strip()
            if n and n in t and (best is None or len(n) > len(best.name)):
                best = c
        return best

    def abs_paths(self, name: str) -> List[Path]:
        """Resolve a collection's members to absolute paths that still exist."""
        c = self.get(name)
        if c is None:
            return []
        in_dir = _input_dir(self._vault)
        out = []
        for r in c.files:
            p = in_dir / r
            if p.exists():
                out.append(p)
        return out


# ============================================================
# Discovery — propose which files belong to a named collection
# ============================================================

# App-generated output dirs under data_in — NOT source data, so they must
# not be proposed as collection members (a collection's own summary lands in
# deferred_results/ and would otherwise be suggested as its own member).
_DISCOVER_SKIP_DIRS = {"deferred_results", "derived", "converted_mongo",
                       "__pycache__"}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def propose_members(vault_dir: Optional[Any], term: str, *,
                    index: Any = None, limit: int = 300
                    ) -> List[Tuple[str, float, List[str]]]:
    """Propose files likely belonging to the collection named ``term``.

    Combines signals (higher score = more confident):
      • value match (4.0) — ``term`` appears as a VALUE in the file
        (index.search_value), the strongest "this file is about X" signal.
      • filename match (3.0 exact slug / 2.0 all words) — the path/name
        contains the term.
      • relationship expansion (1.0) — a file that shares a join column with
        an already-matched file (index.find_relationships).

    Returns ``[(rel_path, score, reasons), ...]`` ranked by score. ``index``
    is optional; without it only filename matching runs (so it works before
    the data index is built, and is unit-testable).
    """
    in_dir = _input_dir(vault_dir)
    term_l = (term or "").strip().lower()
    slug = _slug(term_l)
    words = [w for w in re.findall(r"[a-z0-9]+", term_l) if len(w) > 1]
    scores: Dict[str, List[Any]] = {}

    def bump(rel: str, pts: float, reason: str) -> None:
        if not rel:
            return
        # Never propose app-generated outputs (covers value/relationship
        # signals too, not just the filename walk above).
        if rel.split("/", 1)[0] in _DISCOVER_SKIP_DIRS:
            return
        e = scores.setdefault(rel, [0.0, set()])
        e[0] += pts
        e[1].add(reason)

    # 1) filename / path match (walk data_in; no index needed)
    try:
        for dp, dn, fn in os.walk(str(in_dir)):
            dn[:] = [d for d in dn if not d.startswith(".")
                     and d not in _DISCOVER_SKIP_DIRS]
            for f in fn:
                if f.startswith("."):
                    continue
                full = os.path.join(dp, f)
                rel = _norm_rel(full, in_dir)
                low_slug = _slug(full)
                if slug and slug in low_slug:
                    bump(rel, 3.0, "name match")
                elif words and all(w in full.lower() for w in words):
                    bump(rel, 2.0, "name words")
    except Exception:
        pass

    # 2) value match — term appears as a value somewhere in the file
    if index is not None and term_l:
        try:
            for h in (index.search_value(term) or []):
                rel = _norm_rel(h.get("file") or h.get("path") or h, in_dir)
                bump(rel, 4.0, "value match")
        except Exception:
            pass

    # 3) relationship expansion — pull in files joinable to matched ones
    if index is not None and scores:
        try:
            matched = set(scores.keys())
            for reln in (index.find_relationships() or []):
                files = [_norm_rel(f, in_dir) for f in reln.get("files", [])]
                if any(f in matched for f in files):
                    col = reln.get("column", "?")
                    for f in files:
                        if f and f not in matched:
                            bump(f, 1.0, f"shares '{col}'")
        except Exception:
            pass

    out = [(rel, sc, sorted(rs)) for rel, (sc, rs) in scores.items()]
    out.sort(key=lambda t: (-t[1], t[0]))
    return out[:limit]
