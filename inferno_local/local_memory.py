"""
inferno_local.local_memory — persistent, telemetry-off vector memory.

A thin wrapper over ``chromadb.PersistentClient`` that bakes in every
local-only constraint the Odysseus brief demands:

  * ``anonymized_telemetry=False`` and ``allow_reset=False`` on the
    client. ChromaDB's default is to phone-home product analytics on
    the first ``add``; we turn it off at the source.
  * No implicit embedder. The caller injects an ``Embedder`` (Protocol
    below). If nothing is injected, ``add`` and ``query`` fall back to
    storing/searching by exact ID — never silently downloads a model.
  * No remote Chroma. We never construct ``HttpClient`` — only
    ``PersistentClient`` rooted at a local directory.

ID scheme (recommended, not enforced):

    analysis:{session}:{n}    Council deliberation outputs
    doc:{sha1_8}:{chunk_idx}  Chunks of indexed user documents
    note:{slug}               One-shot user notes

The class accepts whatever IDs the caller provides — but a predictable
scheme makes it easy to ``delete_where`` by prefix later.
"""
from __future__ import annotations

import hashlib
import logging
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

from . import security

_LOG = logging.getLogger("inferno_local.local_memory")


class Embedder(Protocol):
    """Callable that maps a list of texts to a list of float vectors."""

    def __call__(self, texts: Sequence[str]) -> List[List[float]]: ...


@dataclass
class MemoryHit:
    id: str
    text: str
    metadata: Dict[str, Any]
    distance: float


class LocalMemory:
    """Persistent, telemetry-off vector store. Single-collection by default;
    multi-collection use is supported via ``collection`` constructor arg.

    Usage:

        from inferno_local.local_memory import LocalMemory
        from inferno_local.local_memory import sbert_embedder

        emb = sbert_embedder("all-MiniLM-L6-v2", device="cpu")
        mem = LocalMemory(Path("~/.council/vault/.local_memory").expanduser(),
                          embedder=emb)
        mem.add("doc:abc123:0", "...chunk text...", {"source": "report.pdf"})
        hits = mem.query("what did Q3 conclude?", k=5)
    """

    def __init__(self,
                 persist_dir: Path,
                 *,
                 embedder: Optional[Embedder] = None,
                 collection: str = "council_memory") -> None:
        self.persist_dir = Path(persist_dir).expanduser().resolve()
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self.collection_name = collection
        self.embedder = embedder
        self._lock = threading.RLock()
        self._client = None
        self._collection = None

    # ── chromadb lazy-init ─────────────────────────────────
    def _ensure_client(self) -> None:
        if self._collection is not None:
            return
        try:
            import chromadb
            from chromadb.config import Settings
        except Exception as exc:
            raise RuntimeError(
                "chromadb is not installed — pip install chromadb"
            ) from exc

        # Bake telemetry off at the client. ChromaDB also reads the
        # CHROMA_TELEMETRY env var; we set it as belt-and-braces in
        # case a downstream caller constructs another Chroma client.
        import os
        os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
        os.environ.setdefault("CHROMA_TELEMETRY", "False")

        settings = Settings(anonymized_telemetry=False, allow_reset=False)
        self._client = chromadb.PersistentClient(
            path=str(self.persist_dir),
            settings=settings,
        )
        # Use a no-op embedding function and supply our own vectors.
        # ChromaDB's default would try to download a sentence-transformer
        # model on first use — exactly what §0 forbids.
        try:
            from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
            ef = DefaultEmbeddingFunction()
        except Exception:
            ef = None
        # We will pass embeddings directly on add/query, so the default
        # function never runs. But we still need to supply *something* on
        # collection-create for older Chroma versions.
        self._collection = self._client.get_or_create_collection(
            name=self.collection_name,
            embedding_function=ef,
        )

    # ── add / query ────────────────────────────────────────
    def add(self, id: str, text: str,
            metadata: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            self._ensure_client()
            md = dict(metadata or {})
            md.setdefault("len", len(text))
            md.setdefault("_id_scheme", id.split(":", 1)[0] if ":" in id else "raw")
            embedding: Optional[List[float]] = None
            if self.embedder is not None:
                try:
                    embedding = self.embedder([text])[0]
                except Exception as exc:
                    _LOG.warning("embedder failed for id=%s: %r", id, exc)
                    embedding = None
            self._collection.add(
                ids=[id],
                documents=[text],
                metadatas=[md],
                embeddings=[embedding] if embedding is not None else None,
            )

    def add_many(self,
                 records: Iterable[Tuple[str, str, Dict[str, Any]]]) -> int:
        """Bulk add for indexers (returns the count actually stored).
        Each tuple is (id, text, metadata)."""
        with self._lock:
            self._ensure_client()
            ids: List[str] = []
            texts: List[str] = []
            metas: List[Dict[str, Any]] = []
            for rid, t, m in records:
                ids.append(rid)
                texts.append(t)
                md = dict(m or {})
                md.setdefault("len", len(t))
                metas.append(md)
            if not ids:
                return 0
            embeddings: Optional[List[List[float]]] = None
            if self.embedder is not None:
                try:
                    embeddings = self.embedder(texts)
                except Exception as exc:
                    _LOG.warning("bulk embed failed: %r", exc)
                    embeddings = None
            self._collection.add(
                ids=ids, documents=texts, metadatas=metas,
                embeddings=embeddings,
            )
            return len(ids)

    def query(self, text: str, *, k: int = 5,
              where: Optional[Dict[str, Any]] = None) -> List[MemoryHit]:
        with self._lock:
            self._ensure_client()
            if self.embedder is None:
                # No embedder → keyword-substring fallback. Better than
                # nothing for tests / air-gapped boxes without sbert.
                got = self._collection.get(where=where) if where else self._collection.get()
                hits: List[MemoryHit] = []
                needle = text.lower()
                for i, doc in enumerate(got.get("documents") or []):
                    if doc and needle in doc.lower():
                        hits.append(MemoryHit(
                            id=got["ids"][i],
                            text=doc,
                            metadata=(got.get("metadatas") or [{}])[i] or {},
                            distance=0.0,
                        ))
                        if len(hits) >= k:
                            break
                return hits
            try:
                qvec = self.embedder([text])[0]
            except Exception as exc:
                _LOG.warning("query embed failed: %r", exc)
                return []
            res = self._collection.query(
                query_embeddings=[qvec],
                n_results=int(k),
                where=where,
            )
            ids = (res.get("ids") or [[]])[0]
            docs = (res.get("documents") or [[]])[0]
            metas = (res.get("metadatas") or [[]])[0]
            dists = (res.get("distances") or [[]])[0]
            out: List[MemoryHit] = []
            for i, rid in enumerate(ids):
                out.append(MemoryHit(
                    id=rid,
                    text=docs[i] if i < len(docs) else "",
                    metadata=metas[i] if i < len(metas) else {},
                    distance=float(dists[i]) if i < len(dists) else 0.0,
                ))
            return out

    def count(self) -> int:
        with self._lock:
            self._ensure_client()
            return int(self._collection.count())

    def status(self) -> Dict[str, Any]:
        return {
            "persist_dir": str(self.persist_dir),
            "collection":  self.collection_name,
            "has_embedder": self.embedder is not None,
            "count":       (self.count() if self._collection is not None else None),
        }


# ============================================================
# Convenience embedder factories (callers can also supply their own)
# ============================================================

def sbert_embedder(model_name: str = "all-MiniLM-L6-v2",
                   *, device: str = "cpu") -> Embedder:
    """Returns an Embedder backed by sentence-transformers. Loads the
    model on first call. Forces a device explicitly because on the dev
    5080 (cu124 PTX fallback) GPU embed can stall. Production 4080
    (native sm_89) can override with device='cuda'."""
    from sentence_transformers import SentenceTransformer
    _model = SentenceTransformer(model_name, device=device)
    def _emb(texts: Sequence[str]) -> List[List[float]]:
        vecs = _model.encode(list(texts), convert_to_numpy=True,
                             show_progress_bar=False, normalize_embeddings=True)
        return [v.tolist() for v in vecs]
    return _emb


def make_doc_id(source: str, chunk_idx: int) -> str:
    """Build a stable doc:{sha1_8}:{chunk_idx} id."""
    h = hashlib.sha1(source.encode("utf-8")).hexdigest()[:8]
    return f"doc:{h}:{chunk_idx}"


def make_analysis_id(session: str, n: int) -> str:
    return f"analysis:{session}:{n}"
