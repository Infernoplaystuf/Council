"""
derived_results.py — a fingerprinted catalogue of COMPUTED outputs.

When the app computes something over the vault (a bigger summary, deeper
stats, "average column X across these CSVs"), the output CSV is written to
data_in/derived/ AND recorded here with the FINGERPRINT of its source files.
A future matching question can then reuse the saved output INSTEAD of
recomputing — but only while it's still valid: every reuse re-checks the
sources' mtimes/sizes, so a stale result (a source changed) is never served.

This is the materialised-views layer: precompute once, reuse until inputs
change. It generalises stats_cache.QueryReportCache (which caches the two
built-in summary/stats routes) to ANY recorded computation, and it's what
makes the deferred / collection / model results reusable AND correct.

Storage: <vault>/derived_results.json (manifest). Outputs live under
data_in/derived/ so they're searchable, but that folder is excluded from the
"source data" census/discovery (it's derived, not raw input).

Pure-ish + dependency-light: only pathlib/hashlib/json. The fingerprint is
(name, int(mtime), size) per source, so an unchanged file set hashes the same.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

_STORE_NAME = "derived_results.json"
_LOCK = threading.Lock()
DERIVED_SUBDIR = "derived"


def _vault_root(vault_dir: Optional[Any] = None) -> Path:
    if vault_dir is not None:
        return Path(vault_dir).expanduser().resolve()
    env = os.environ.get("COUNCIL_VAULT_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".council" / "vault"


def _input_dir(vault_dir: Optional[Any] = None) -> Path:
    try:
        import data_index
        return data_index.input_dir(_vault_root(vault_dir))
    except Exception:
        return _vault_root(vault_dir) / "data_in"


def derived_dir(vault_dir: Optional[Any] = None) -> Path:
    """data_in/derived/ — where computed outputs are written."""
    d = _input_dir(vault_dir) / DERIVED_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def fingerprint(paths: List[Any]) -> str:
    """Stable fingerprint of a set of source files: (name, mtime, size) each,
    order-independent. Two calls hash the same iff every file is unchanged.
    A missing file is encoded distinctly so its disappearance invalidates."""
    items: List[str] = []
    for p in paths or []:
        pp = Path(p)
        try:
            st = pp.stat()
            items.append(f"{pp.name}:{int(st.st_mtime)}:{st.st_size}")
        except Exception:
            items.append(f"{pp.name}:missing")
    if not items:
        return ""
    items.sort()
    return hashlib.sha1("|".join(items).encode("utf-8")).hexdigest()[:16]


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")


_OVERLAP_STOP = {
    "a", "an", "the", "of", "in", "on", "for", "to", "and", "or", "is", "it",
    "what", "how", "with", "by", "at", "from", "give", "me", "my", "please",
    "can", "you", "show", "tell", "get", "do", "i", "want", "need", "that",
}


def _overlap_toks(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
            if w not in _OVERLAP_STOP and len(w) > 1}


def _overlap(a: str, b: str) -> float:
    sa, sb = _overlap_toks(a), _overlap_toks(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


@dataclass
class DerivedResult:
    id: str
    label: str                          # the question/intent it answers
    operation: str = ""                 # e.g. "bigger_summary" / "avg(amount)"
    sources: List[str] = field(default_factory=list)   # abs paths of inputs
    source_fp: str = ""                 # fingerprint of sources at compute time
    output: str = ""                    # path to the computed CSV
    columns: List[str] = field(default_factory=list)
    rows: int = 0
    created_ts: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "DerivedResult":
        known = set(DerivedResult.__dataclass_fields__)  # type: ignore
        return DerivedResult(**{k: v for k, v in d.items() if k in known})

    def is_fresh(self) -> bool:
        """True if the output still exists AND its sources are unchanged."""
        try:
            if not self.output or not Path(self.output).exists():
                return False
        except Exception:
            return False
        return fingerprint(self.sources) == self.source_fp


class DerivedStore:
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

    def all(self) -> List[DerivedResult]:
        return [DerivedResult.from_dict(d) for d in self._read()]

    def record(self, *, label: str, output: Any, sources: List[Any],
               operation: str = "", columns: Optional[List[str]] = None,
               rows: int = 0) -> DerivedResult:
        """Catalogue a computed output with the fingerprint of its sources.
        Replaces any prior entry with the same label+operation."""
        src_abs = [str(Path(s)) for s in (sources or [])]
        entry = DerivedResult(
            id=f"d{int(time.time() * 1000)}",
            label=(label or "").strip(),
            operation=operation or "",
            sources=src_abs,
            source_fp=fingerprint(src_abs),
            output=str(output),
            columns=list(columns or []),
            rows=int(rows),
        )
        with _LOCK:
            items = self._read()
            key = (entry.label.lower(), entry.operation.lower())
            items = [it for it in items
                     if (str(it.get("label", "")).lower(),
                         str(it.get("operation", "")).lower()) != key]
            items.append(entry.to_dict())
            self._write(items)
        return entry

    def find_fresh(self, query: str, *, min_overlap: float = 0.5
                   ) -> Optional[DerivedResult]:
        """Best FRESH derived result whose label matches ``query`` (token
        overlap) AND whose sources are unchanged. None if nothing matches or
        the best match is stale. Prunes nothing — staleness is checked live."""
        best: Optional[DerivedResult] = None
        best_score = float(min_overlap)
        q_tokens = _overlap_toks(query)   # tokenize the query ONCE, not per row
        for r in self.all():
            if q_tokens:
                l_tokens = _overlap_toks(r.label)
                sc = (len(q_tokens & l_tokens) / len(q_tokens | l_tokens)
                      if l_tokens else 0.0)
            else:
                sc = 0.0
            if sc >= best_score and r.is_fresh():
                best, best_score = r, sc
        return best

    def get(self, result_id: str) -> Optional[DerivedResult]:
        for r in self.all():
            if r.id == result_id:
                return r
        return None

    def prune_missing(self) -> int:
        """Drop entries whose output file no longer exists. Returns count."""
        with _LOCK:
            items = self._read()
            keep = [it for it in items
                    if it.get("output") and Path(it["output"]).exists()]
            removed = len(items) - len(keep)
            if removed:
                self._write(keep)
            return removed
