# ============================================================
# vault_rag.py  —  Semantic RAG over the Council Vault
# ============================================================
# Indexes vault files into ChromaDB (local, embedded, no server).
# Writer and other roles can do semantic search before responding.
#
# Install:
#   pip install chromadb
#
# Falls back to keyword search if chromadb not installed.
# ============================================================

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── ChromaDB (optional) ──────────────────────────────────────
try:
    import chromadb
    from chromadb.config import Settings
    _CHROMA_OK = True
except ImportError:
    _CHROMA_OK = False

# ── Sentence transformers for embeddings (optional) ──────────
try:
    from sentence_transformers import SentenceTransformer
    _ST_OK = True
except ImportError:
    _ST_OK = False

import council_engine as ce


# ============================================================
# Config
# ============================================================

CHUNK_SIZE     = 600    # characters per chunk
CHUNK_OVERLAP  = 100    # overlap between chunks
MAX_RESULTS    = 6      # default number of results to return
EMBED_MODEL    = "all-MiniLM-L6-v2"   # small, fast, good quality

# File types to index
INDEXABLE_EXTENSIONS = {
    ".py", ".md", ".txt", ".json", ".yaml", ".yml",
    ".html", ".rst", ".csv", ".log", ".toml", ".ini",
}

# Files to skip
SKIP_PATTERNS = {".git", "__pycache__", ".chromadb", "node_modules"}


# ============================================================
# Data classes
# ============================================================

@dataclass
class RAGResult:
    query: str
    chunks: List[Dict[str, Any]] = field(default_factory=list)
    # Each chunk: {"text": str, "source": str, "score": float, "chunk_id": str}
    formatted: str = ""
    backend: str = "none"


@dataclass
class IndexStats:
    total_files: int = 0
    total_chunks: int = 0
    skipped_files: int = 0
    indexed_at: str = ""
    backend: str = "none"


# ============================================================
# Chunking
# ============================================================

def _chunk_text(text: str, source: str, chunk_size: int = CHUNK_SIZE,
                overlap: int = CHUNK_OVERLAP) -> List[Dict[str, str]]:
    """Split text into overlapping chunks with metadata."""
    chunks: List[Dict[str, str]] = []
    text = text.strip()
    if not text:
        return chunks

    start = 0
    idx = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunk_text = text[start:end].strip()
        if chunk_text:
            chunk_id = hashlib.md5(f"{source}:{idx}:{chunk_text[:50]}".encode()).hexdigest()[:16]
            chunks.append({
                "text": chunk_text,
                "source": source,
                "chunk_id": chunk_id,
                "chunk_index": idx,
            })
            idx += 1
        if end >= len(text):
            break
        start = end - overlap

    return chunks


def _collect_files(vault_dir: Path) -> List[Path]:
    """Walk vault_dir and collect indexable files."""
    files: List[Path] = []
    for p in vault_dir.rglob("*"):
        # Skip hidden/system dirs
        if any(part in SKIP_PATTERNS for part in p.parts):
            continue
        if not p.is_file():
            continue
        if p.suffix.lower() not in INDEXABLE_EXTENSIONS:
            continue
        files.append(p)
    return sorted(files)


def _file_hash(p: Path) -> str:
    """Fast hash of file content for change detection."""
    try:
        content = p.read_bytes()
        return hashlib.md5(content).hexdigest()
    except Exception:
        return ""


# ============================================================
# ChromaDB backend
# ============================================================

class _ChromaBackend:
    def __init__(self, persist_dir: Path):
        self.persist_dir = persist_dir
        self.persist_dir.mkdir(parents=True, exist_ok=True)

        client_settings = Settings(
            anonymized_telemetry=False,
            allow_reset=True,
        )
        self._client = chromadb.PersistentClient(
            path=str(persist_dir),
            settings=client_settings,
        )
        self._collection = self._client.get_or_create_collection(
            name="vault",
            metadata={"hnsw:space": "cosine"},
        )
        self._hash_store_path = persist_dir / "file_hashes.json"
        self._hashes: Dict[str, str] = self._load_hashes()

        # Embedding function
        if _ST_OK:
            self._embedder = SentenceTransformer(EMBED_MODEL)
            print(f"[VaultRAG] Using sentence-transformers ({EMBED_MODEL})")
        else:
            self._embedder = None
            print("[VaultRAG] sentence-transformers not installed — using ChromaDB default embeddings")

    def _load_hashes(self) -> Dict[str, str]:
        if self._hash_store_path.exists():
            try:
                return json.loads(self._hash_store_path.read_text())
            except Exception:
                pass
        return {}

    def _save_hashes(self):
        self._hash_store_path.write_text(json.dumps(self._hashes, indent=2))

    def _embed(self, texts: List[str]) -> Optional[List[List[float]]]:
        if self._embedder is None:
            return None   # ChromaDB will use its own default
        return self._embedder.encode(texts, show_progress_bar=False).tolist()

    def index_vault(self, vault_dir: Path) -> IndexStats:
        stats = IndexStats(backend="chromadb", indexed_at=ce.now_iso())
        files = _collect_files(vault_dir)
        stats.total_files = len(files)

        for p in files:
            rel = str(p.relative_to(vault_dir))
            current_hash = _file_hash(p)

            # Skip if unchanged
            if self._hashes.get(rel) == current_hash:
                continue

            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                stats.skipped_files += 1
                continue

            chunks = _chunk_text(text, source=rel)
            if not chunks:
                continue

            # Remove old chunks for this file
            try:
                existing = self._collection.get(where={"source": rel})
                if existing["ids"]:
                    self._collection.delete(ids=existing["ids"])
            except Exception:
                pass

            # Add new chunks
            ids      = [c["chunk_id"] for c in chunks]
            texts    = [c["text"]      for c in chunks]
            metas    = [{"source": c["source"], "chunk_index": c["chunk_index"]} for c in chunks]
            embeddings = self._embed(texts)

            if embeddings:
                self._collection.add(ids=ids, documents=texts, metadatas=metas, embeddings=embeddings)
            else:
                self._collection.add(ids=ids, documents=texts, metadatas=metas)

            self._hashes[rel] = current_hash
            stats.total_chunks += len(chunks)

        self._save_hashes()
        return stats

    def search(self, query: str, n_results: int = MAX_RESULTS) -> List[Dict[str, Any]]:
        try:
            count = self._collection.count()
            if count == 0:
                return []

            n = min(n_results, count)
            query_embedding = self._embed([query])

            if query_embedding:
                results = self._collection.query(
                    query_embeddings=query_embedding,
                    n_results=n,
                )
            else:
                results = self._collection.query(
                    query_texts=[query],
                    n_results=n,
                )

            chunks: List[Dict[str, Any]] = []
            docs      = results.get("documents", [[]])[0]
            metas     = results.get("metadatas",  [[]])[0]
            distances = results.get("distances",  [[]])[0]

            for doc, meta, dist in zip(docs, metas, distances):
                score = 1.0 - float(dist)   # cosine → similarity
                chunks.append({
                    "text":   doc,
                    "source": meta.get("source", "?"),
                    "score":  round(score, 3),
                    "chunk_id": "",
                })
            return chunks
        except Exception as e:
            print(f"[VaultRAG] search error: {e}")
            return []

    def collection_count(self) -> int:
        try:
            return self._collection.count()
        except Exception:
            return 0


# ============================================================
# Keyword fallback backend
# ============================================================

class _KeywordBackend:
    """
    Pure-Python keyword search over vault files.
    Used when ChromaDB is not installed.
    """

    def __init__(self, vault_dir: Path):
        self.vault_dir = vault_dir

    def index_vault(self, vault_dir: Path) -> IndexStats:
        files = _collect_files(vault_dir)
        return IndexStats(
            total_files=len(files),
            backend="keyword",
            indexed_at=ce.now_iso(),
        )

    def search(self, query: str, n_results: int = MAX_RESULTS) -> List[Dict[str, Any]]:
        keywords = [w.lower() for w in re.findall(r"\w+", query) if len(w) > 2]
        if not keywords:
            return []

        results: List[Dict[str, Any]] = []
        files = _collect_files(self.vault_dir)

        for p in files:
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            tlow = text.lower()
            score = sum(tlow.count(kw) for kw in keywords)
            if score == 0:
                continue

            # Extract best matching excerpt
            best_idx = -1
            best_count = 0
            for kw in keywords:
                idx = tlow.find(kw)
                if idx >= 0:
                    window = tlow[max(0, idx-200):idx+200]
                    count = sum(window.count(k) for k in keywords)
                    if count > best_count:
                        best_count = count
                        best_idx = idx

            if best_idx >= 0:
                start = max(0, best_idx - 200)
                excerpt = text[start:start + 600].strip()
            else:
                excerpt = text[:600].strip()

            rel = str(p.relative_to(self.vault_dir))
            results.append({
                "text": excerpt,
                "source": rel,
                "score": min(1.0, score / (len(keywords) * 5)),
                "chunk_id": "",
            })

        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:n_results]

    def collection_count(self) -> int:
        return len(_collect_files(self.vault_dir))


# ============================================================
# Public API
# ============================================================

class VaultRAG:
    """
    Semantic (or keyword) search over the council vault.

    Usage:
        rag = VaultRAG(vault_dir, chroma_dir)
        rag.index()          # index/update vault
        result = rag.search("how to parse JSON in Python")
        # inject result.formatted into model context
    """

    def __init__(self, vault_dir: Path, chroma_dir: Optional[Path] = None):
        self.vault_dir = vault_dir

        if _CHROMA_OK:
            chroma_path = chroma_dir or (vault_dir.parent / ".chromadb")
            self._backend = _ChromaBackend(chroma_path)
            self._backend_name = "chromadb"
        else:
            print("[VaultRAG] chromadb not installed — using keyword search fallback")
            self._backend = _KeywordBackend(vault_dir)
            self._backend_name = "keyword"

        self._last_indexed: float = 0.0
        self._reindex_interval: float = 300.0   # seconds between auto-reindex

    @property
    def backend_name(self) -> str:
        return self._backend_name

    def index(self, force: bool = False) -> IndexStats:
        """Index or update the vault. Skips unchanged files."""
        now = time.monotonic()
        if not force and (now - self._last_indexed) < self._reindex_interval:
            return IndexStats(backend=self._backend_name)
        stats = self._backend.index_vault(self.vault_dir)
        self._last_indexed = time.monotonic()
        print(f"[VaultRAG] Indexed {stats.total_files} files, "
              f"{stats.total_chunks} chunks ({self._backend_name})")
        return stats

    def search(self, query: str, n_results: int = MAX_RESULTS) -> RAGResult:
        """
        Search the vault for chunks relevant to query.
        Returns RAGResult with .formatted ready to inject into a model prompt.
        """
        result = RAGResult(query=query, backend=self._backend_name)
        chunks = self._backend.search(query, n_results=n_results)
        result.chunks = chunks

        if chunks:
            lines = [f"VAULT SEARCH RESULTS for: {query}\n"]
            for i, c in enumerate(chunks, 1):
                lines.append(f"[{i}] {c['source']} (relevance: {c['score']:.2f})")
                lines.append(c["text"].strip())
                lines.append("")
            result.formatted = "\n".join(lines)
        else:
            result.formatted = f"VAULT SEARCH: no relevant results for '{query}'"

        return result

    def search_for_context(self, query: str, max_tokens_approx: int = 2000) -> str:
        """
        Convenience: search and return formatted string trimmed to approx token budget.
        ~4 chars per token as rough estimate.
        """
        result = self.search(query, n_results=MAX_RESULTS)
        text = result.formatted
        char_limit = max_tokens_approx * 4
        if len(text) > char_limit:
            text = text[:char_limit] + "\n… (truncated)"
        return text

    def collection_count(self) -> int:
        return self._backend.collection_count()

    def auto_index_if_stale(self) -> None:
        """Call this periodically to keep index fresh."""
        now = time.monotonic()
        if (now - self._last_indexed) >= self._reindex_interval:
            self.index()
