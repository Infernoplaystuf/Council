"""
Vector embedding index — the practical alternative to fine-tuning.

True fine-tuning a 7B+ GGUF on a laptop GPU (RTX 4070, 8 GB VRAM) is
slow (~30 min per run with QLoRA) and has to be redone every time the
vault changes — not viable as a chat-time speed-up. A sentence-
transformer embedding model is the modern answer: ~80 MB, runs in
milliseconds per query on the same GPU, and gives semantic retrieval
quality that's strictly better than the keyword index alone.

How it slots in:
  - Each vault record gets a 384-dim vector built from its name +
    LLM-generated description (if present) + headers / sheet names /
    table names + a sample of values.
  - At query time, the user's question is embedded once; cosine
    similarity against every cached vector is essentially free.
  - Results blend into vault_index.search() — keyword + fuzzy +
    embedding, with each layer contributing to the final score.

Cache: vault/vault_embeddings.json sits next to vault_index.json.
Same lazy regenerate pattern as the LLM-descriptions layer — only
records whose mtime changed get re-embedded.

Why JSON and not ChromaDB
-------------------------
ChromaDB is also in the project (used by vault_rag.py for full-document
RAG over text/markdown/PDF files — that's a separate persistence
concern from per-record vault embeddings).  The choice for THIS module
was reconsidered as part of the context-window work order:

  * For ~1000s of vault records at 384 dims that's ~12-30 MB JSON.
    Lazy-loaded at first read; cosine via numpy over the full set is
    sub-millisecond for that size. ChromaDB would add startup
    initialization (open the on-disk index, create the collection,
    handle migrations) without a real performance win at this scale.

  * Migrating would couple the keyword/embedding blend in
    vault_index.search() to chroma's query API — currently the blend
    just consults this index in-memory which is simpler, faster, and
    has zero external dependencies on the read path.

  * Air-gapped users: chroma works air-gapped but has more moving
    parts (sqlite-vss, optional ANN backends) than a plain JSON+numpy
    blob. Fewer moving parts = fewer "doesn't start cleanly on weird
    systems" reports.

Decision: keep JSON here, keep ChromaDB for vault_rag.py. If we hit
10K+ records and the in-memory cosine becomes a bottleneck, swap to
a `.npy` binary cache first; ChromaDB would only earn its keep when
we need persistent metadata filtering, multi-collection routing, or
remote queries — none of which apply to this module's job.

Model: 'all-MiniLM-L6-v2' by default (22M params, 384 dims, CPU/GPU
fine). Override with COUNCIL_EMBED_MODEL env var if you have a heavier
local model cached (BGE-base, e5-base, etc).

Air-gapped users: pre-download the model on a connected machine with
`from sentence_transformers import SentenceTransformer;
SentenceTransformer('all-MiniLM-L6-v2')`, then copy the HF cache
folder (~/.cache/huggingface/) to the offline machine.
"""

from __future__ import annotations

import json
import math
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

# Numpy is required for the cosine math; if it isn't present we just
# disable the whole module gracefully.
try:
    import numpy as _np
    _NUMPY_OK = True
except Exception:
    _np = None
    _NUMPY_OK = False


EMBEDDINGS_FILENAME = "vault_embeddings.json"
DEFAULT_MODEL = os.environ.get("COUNCIL_EMBED_MODEL", "all-MiniLM-L6-v2")


# ============================================================
# Per-record text representation
# ============================================================

def _record_to_text(rec: Dict[str, Any], max_chars: int = 1200) -> str:
    """Build the text snippet we embed for one vault record.

    Designed to capture *what the file is* without exhausting the
    embedding context window. Order matters — descriptions and topics
    (LLM-summarised) front-load the semantic signal.
    """
    parts: List[str] = []
    name = str(rec.get("name", "")).strip()
    if name:
        parts.append(f"File: {name}")
    rtype = rec.get("type", "")
    if rtype:
        parts.append(f"Type: {rtype}")
    desc = (rec.get("description") or "").strip()
    if desc:
        parts.append(desc)
    topics = rec.get("topics") or []
    if topics:
        parts.append("Topics: " + ", ".join(map(str, topics[:8])))
    headers = rec.get("headers") or []
    if headers:
        parts.append("Columns: " + ", ".join(map(str, headers[:25])))
    keys = rec.get("keys") or []
    if keys:
        parts.append("Keys: " + ", ".join(map(str, keys[:25])))
    # Excel
    for s in (rec.get("sheets") or []):
        cols = ", ".join(map(str, (s.get("headers") or [])[:15]))
        parts.append(f"Sheet {s.get('sheet','?')}: {cols}")
    # SQLite tables
    for t in (rec.get("tables") or []):
        cols = ", ".join((t.get("columns") or [])[:15])
        parts.append(f"Table {t.get('table','?')}: {cols}")
    # Sample
    sample_rows = rec.get("sample_rows") or []
    if sample_rows:
        parts.append("Sample: " + " | ".join(map(str, sample_rows[:3])))
    sample_text = (rec.get("sample_text") or "").strip()
    if sample_text:
        parts.append(sample_text[:300])
    blob = "\n".join(parts)
    if len(blob) > max_chars:
        blob = blob[:max_chars]
    return blob


# ============================================================
# Embedding store
# ============================================================

class EmbeddingIndex:
    """Cached cosine-similarity index over the vault.

    Vectors are stored as list-of-floats in a JSON file (easy to diff,
    no extra dep). For ~500 files at 384 dims that's ~6 MB — well
    within reason. For larger vaults, swap to a binary `.npy` cache.
    """

    def __init__(self, vault_dir: Path, *, model_name: str = DEFAULT_MODEL):
        self.vault_dir = Path(vault_dir)
        self.cache_path = self.vault_dir / EMBEDDINGS_FILENAME
        self.model_name = model_name
        self._model = None
        self._vectors: Dict[str, List[float]] = {}   # {file_path: vector}
        self._mtimes:  Dict[str, float]       = {}   # {file_path: rec.mtime}
        self._dim: Optional[int] = None
        # Lazy load — parsing the JSON cache can be 500ms-1s on big
        # vaults. We defer until either search() / similar_to() is
        # called or build_embeddings starts. The previous eager load
        # at __init__ delayed app startup for users who never opened
        # the embeddings-aware tabs at all.
        self._loaded = False
        # Guards _vectors/_mtimes against the build-vs-search race:
        # build() runs on a background thread (the GUI's embedding
        # builder) and deletes stale keys while search() on a worker
        # thread does check-then-get per path. The GIL makes each dict
        # op atomic but NOT the check-then-get pair. Model encode calls
        # stay OUTSIDE the lock — only dict mutation/snapshot is held.
        self._vec_lock = threading.RLock()

    # ---- persistence ----

    def _ensure_loaded(self) -> None:
        """Lazy guard — any method that reads ``_vectors`` calls this first.
        Idempotent; subsequent calls are a no-op O(1) flag check."""
        if self._loaded:
            return
        self._loaded = True
        if not self.cache_path.exists():
            return
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            self.model_name = data.get("model", self.model_name)
            self._dim       = data.get("dim", None)
            self._vectors   = {k: list(v) for k, v in (data.get("vectors") or {}).items()}
            self._mtimes    = {k: float(v) for k, v in (data.get("mtimes") or {}).items()}
        except Exception:
            self._vectors = {}
            self._mtimes  = {}

    def load(self) -> None:
        """Backwards-compatible alias — original callers used to call
        this from __init__. Now it just forces the lazy load early."""
        self._ensure_loaded()

    def save(self) -> None:
        try:
            data = {
                "model":   self.model_name,
                "dim":     self._dim,
                "vectors": self._vectors,
                "mtimes":  self._mtimes,
            }
            self.cache_path.write_text(
                json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        except Exception as exc:
            print(f"[EmbeddingIndex] save failed: {exc!r}")

    # ---- model lazy-load ----

    def _get_model(self):
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer
        except Exception as exc:
            raise RuntimeError(
                "sentence-transformers not available. Install with "
                "`pip install sentence-transformers`. Original error: "
                f"{exc!r}"
            )
        # CPU-only by default; sentence-transformers picks GPU if torch
        # sees CUDA. Override via COUNCIL_EMBED_DEVICE.
        device = os.environ.get("COUNCIL_EMBED_DEVICE")  # 'cuda' / 'cpu'
        try:
            try:
                self._model = SentenceTransformer(self.model_name, device=device)
            except TypeError:
                self._model = SentenceTransformer(self.model_name)
        except Exception as exc:
            # Most common failure is "can't reach huggingface.co" — corporate
            # proxy, air-gapped machine, etc. Make the recovery path obvious.
            raise RuntimeError(
                f"Could not load embedding model {self.model_name!r}: {exc}\n\n"
                "Fix one of these:\n"
                "  1. On a connected machine, run:\n"
                "       from sentence_transformers import SentenceTransformer\n"
                "       SentenceTransformer('all-MiniLM-L6-v2')\n"
                "     Then copy ~/.cache/huggingface/ to this machine.\n"
                "  2. Set COUNCIL_EMBED_MODEL to a local model path you have on disk.\n"
                "  3. Set HF_HUB_OFFLINE=1 to disable the network call entirely\n"
                "     (only works if the model is already cached).\n"
            ) from exc
        return self._model

    # ---- build / refresh ----

    def build(
        self,
        records: Dict[str, Dict[str, Any]],
        *,
        force: bool = False,
        on_progress: Optional[Callable[[int, int, str], None]] = None,
    ) -> int:
        """Embed each record whose mtime has changed.

        Returns the number of records embedded this call.
        """
        if not _NUMPY_OK:
            raise RuntimeError("numpy required for the embedding index")

        # Lazy-load — the JSON cache may not have been parsed yet if no
        # one searched before now (e.g. user clicked "Build embeddings"
        # without ever opening the search tab).
        self._ensure_loaded()

        # Decide which records need new vectors
        todo: List[Tuple[str, Dict[str, Any]]] = []
        for path, rec in records.items():
            mtime = float(rec.get("mtime", 0))
            cached_mtime = self._mtimes.get(path, -1.0)
            if force or path not in self._vectors or mtime != cached_mtime:
                todo.append((path, rec))
        if not todo:
            return 0

        model = self._get_model()
        # Build in batches of 32 to bound memory
        batch_size = 32
        n_done = 0
        for i in range(0, len(todo), batch_size):
            chunk = todo[i: i + batch_size]
            texts = [_record_to_text(rec) for _path, rec in chunk]
            vecs = model.encode(texts, normalize_embeddings=True,
                                show_progress_bar=False)
            with self._vec_lock:
                for (path, rec), vec in zip(chunk, vecs):
                    self._vectors[path] = [float(x) for x in vec]
                    self._mtimes[path]  = float(rec.get("mtime", 0))
                    if self._dim is None:
                        self._dim = len(self._vectors[path])
                    n_done += 1
            if on_progress:
                for (path, rec), _vec in zip(chunk, vecs):
                    try:
                        on_progress(n_done, len(todo), rec.get("name", path))
                    except Exception:
                        pass

        # Drop vectors for records that no longer exist — atomic swap
        # under the lock so a concurrent search() never sees a dict
        # mid-deletion.
        live = set(records.keys())
        with self._vec_lock:
            self._vectors = {k: v for k, v in self._vectors.items() if k in live}
            self._mtimes  = {k: v for k, v in self._mtimes.items() if k in live}

        self.save()
        return n_done

    # ---- search ----

    def search(
        self,
        query: str,
        *,
        k: int = 5,
        candidates: Optional[Iterable[str]] = None,
        min_score: float = 0.10,
    ) -> List[Tuple[float, str]]:
        """Cosine-similarity search. Returns [(score, file_path), ...]
        sorted descending. Scores are in [-1, 1]; typical hits land
        above ~0.3 for a good match.

        `candidates` restricts the search to a subset of paths (used
        for folder-scoped queries).
        """
        # Lazy-load on first search — saves 500ms-1s at app startup
        # for users who don't open the search tab right away.
        self._ensure_loaded()
        if not _NUMPY_OK or not self._vectors:
            return []
        q = (query or "").strip()
        if not q:
            return []
        try:
            model = self._get_model()
        except Exception:
            return []
        qvec = model.encode([q], normalize_embeddings=True,
                            show_progress_bar=False)[0]
        if hasattr(qvec, "tolist"):
            qvec = qvec.tolist()
        qarr = _np.asarray(qvec, dtype=_np.float32)

        # Snapshot under the lock — build() may be mutating _vectors on
        # a background thread, and the per-element check-then-get below
        # is not atomic without it.
        with self._vec_lock:
            raw_paths = list(candidates) if candidates is not None \
                        else list(self._vectors.keys())
            if not raw_paths:
                return []
            # Build aligned (path, vector) pairs so the matrix row order
            # matches the path list. Skipping a path with `if p in
            # self._vectors` in the matrix-only comprehension would leave
            # cand_paths longer than the matrix and misalign every score.
            aligned: List[Tuple[str, List[float]]] = [
                (p, self._vectors[p]) for p in raw_paths if p in self._vectors
            ]
        if not aligned:
            return []
        mat_paths = [p for p, _ in aligned]
        mat = _np.asarray([v for _, v in aligned], dtype=_np.float32)
        sims = mat @ qarr  # both already L2-normalised
        order = _np.argsort(-sims)[: max(k, 1)]
        out: List[Tuple[float, str]] = []
        for idx in order:
            score = float(sims[int(idx)])
            if score < min_score:
                continue
            out.append((score, mat_paths[int(idx)]))
        return out

    # ---- inspection ----

    def __len__(self) -> int:
        self._ensure_loaded()
        return len(self._vectors)

    def stats(self) -> Dict[str, Any]:
        self._ensure_loaded()
        return {
            "model":    self.model_name,
            "dim":      self._dim,
            "vectors":  len(self._vectors),
            "cache":    str(self.cache_path),
            "size_kb":  round(self.cache_path.stat().st_size / 1024, 1)
                        if self.cache_path.exists() else 0,
        }
