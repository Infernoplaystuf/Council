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

# TF-IDF tokenizer (3+ alphanumerics). Compiled once; used per chunk at index
# time and per query at search time.
_TOKEN_RE = re.compile(r"[a-zA-Z0-9]{3,}")

# ── ChromaDB (optional) ──────────────────────────────────────
try:
    import chromadb
    from chromadb.config import Settings
    _CHROMA_OK = True
except ImportError:
    _CHROMA_OK = False

# ── Sentence transformers for embeddings (optional) ──────────
# Availability is probed WITHOUT importing: find_spec only locates the
# package on disk (milliseconds), while actually importing
# sentence_transformers executes torch + sklearn + transformers — 6.7 s
# measured on the dev box. That cost used to land at app startup
# because this module is imported by council_gui_engine at module
# level; now it lands inside VaultRAG.__init__, which already runs on
# the background RAG-indexing thread, off the critical startup path.
import importlib.util as _ilu
_ST_OK = _ilu.find_spec("sentence_transformers") is not None

import council_engine as ce


# ============================================================
# Config
# ============================================================

CHUNK_SIZE     = 800    # characters per chunk (larger = more context per result)
CHUNK_OVERLAP  = 150    # overlap between chunks (more overlap = fewer missed boundaries)
MAX_RESULTS    = 8      # default number of results to return
EMBED_MODEL    = "all-MiniLM-L6-v2"   # small, fast, good quality

# File types to index. The "extractable" formats need a parser pass before
# chunking — handled in _extract_text() below. Plain text/code/markdown is
# read directly. Set COUNCIL_RAG_DISABLE_EXTRACTORS=1 to fall back to
# text-only behaviour if a parser is misbehaving.
TEXT_LIKE_EXTENSIONS = {
    ".py", ".md", ".txt", ".json", ".yaml", ".yml",
    ".html", ".rst", ".csv", ".log", ".toml", ".ini",
    ".tsv", ".jsonl", ".ndjson", ".xml",
}
EXTRACTABLE_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".xlsm", ".xls"}
INDEXABLE_EXTENSIONS = TEXT_LIKE_EXTENSIONS | EXTRACTABLE_EXTENSIONS

# Files to skip
SKIP_PATTERNS = {".git", "__pycache__", ".chromadb", "node_modules"}


def _extract_text(p: Path) -> str:
    """Best-effort text extraction for analyst formats (PDF/DOCX/XLSX).

    Falls back to '' on any error — callers treat an empty extraction
    as "skip this file" so a single bad PDF doesn't poison the index.
    """
    suf = p.suffix.lower()
    try:
        if suf == ".pdf":
            try:
                from pypdf import PdfReader
            except Exception:
                return ""
            reader = PdfReader(str(p))
            parts = []
            for page in reader.pages[:50]:
                try:
                    t = page.extract_text() or ""
                except Exception:
                    t = ""
                if t:
                    parts.append(t)
            return "\n".join(parts)
        if suf == ".docx":
            try:
                from docx import Document
            except Exception:
                return ""
            doc = Document(str(p))
            return "\n".join(par.text for par in doc.paragraphs if par.text)
        if suf in (".xlsx", ".xlsm", ".xls"):
            try:
                import openpyxl as _xl
            except Exception:
                return ""
            wb = _xl.load_workbook(p, read_only=True, data_only=True)
            chunks = []
            for sh in wb.worksheets:
                chunks.append(f"## Sheet: {sh.title}")
                for row in sh.iter_rows(max_row=500, values_only=True):
                    chunks.append("\t".join("" if v is None else str(v) for v in row))
            return "\n".join(chunks)
    except Exception:
        return ""
    return ""


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
    """Fast hash of file content for change detection.

    Streams in 64 KB chunks — the previous read_bytes() loaded the
    ENTIRE file into memory first, which stalls or OOMs the process
    when a vault contains multi-GB media/dataset files."""
    try:
        h = hashlib.md5()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
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

        # Embedding function. COUNCIL_EMBED_DEVICE pins the device
        # ('cpu' / 'cuda'). On RTX 5080 / Blackwell with cu124 wheels the
        # GPU path can segfault during MiniLM init (sm_120 PTX fallback);
        # 'cpu' is the safe default for dev boxes. Native sm_89 (4080)
        # targets can leave this unset to use CUDA.
        if _ST_OK:
            # Env var if set, else auto — but 'cpu' on WSL (GPU embedder +
            # offloaded model = CUDA core dump). Resolved in code so the
            # safe default applies however the app was launched.
            try:
                import hardware_detect as _hd
                device = _hd.resolve_embed_device()
            except Exception:
                device = os.environ.get("COUNCIL_EMBED_DEVICE", "").strip() or None
            try:
                # Deferred heavy import — see the find_spec note at the
                # top of this module. local_files_only when cached: a
                # cached model must load with zero network calls (the
                # default HEAD-check stalls through 5 SSL retries behind
                # intercepting proxies before using the cache anyway).
                from sentence_transformers import SentenceTransformer
                from vault_embeddings import _model_is_cached
                _kw = {"local_files_only": _model_is_cached(EMBED_MODEL)}
                if device:
                    _kw["device"] = device
                try:
                    self._embedder = SentenceTransformer(EMBED_MODEL, **_kw)
                except TypeError:   # older ST without local_files_only
                    self._embedder = SentenceTransformer(EMBED_MODEL, device=device) \
                        if device else SentenceTransformer(EMBED_MODEL)
                print(f"[VaultRAG] Using sentence-transformers ({EMBED_MODEL}, device={device or 'auto'})")
            except Exception as exc:
                print(f"[VaultRAG] embedder load failed ({exc!r}); falling back to ChromaDB default")
                self._embedder = None
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
                if p.suffix.lower() in EXTRACTABLE_EXTENSIONS:
                    text = _extract_text(p)
                    if not text:
                        stats.skipped_files += 1
                        continue
                else:
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

class _TFIDFBackend:
    """
    TF-IDF keyword search over vault files — significantly better than
    raw keyword counting. Scores each chunk by term frequency * inverse
    document frequency so rare, specific terms rank higher than common ones.
    Falls back gracefully when math is unavailable.
    Used when ChromaDB is not installed.
    """

    def __init__(self, vault_dir: Path):
        self.vault_dir = vault_dir
        self._index: Dict[str, List[Dict]] = {}   # rel_path → list of chunks
        self._df: Dict[str, int] = {}             # term → doc frequency
        self._n_docs: int = 0
        self._indexed_hashes: Dict[str, str] = {}
        self._dirty = True

    def index_vault(self, vault_dir: Path) -> IndexStats:
        files = _collect_files(vault_dir)
        stats = IndexStats(
            total_files=len(files),
            backend="tfidf",
            indexed_at=ce.now_iso(),
        )
        new_index: Dict[str, List[Dict]] = {}
        new_df: Dict[str, int] = {}

        for p in files:
            rel = str(p.relative_to(vault_dir))
            current_hash = _file_hash(p)

            # Reuse existing index for unchanged files
            if self._indexed_hashes.get(rel) == current_hash and rel in self._index:
                new_index[rel] = self._index[rel]
                for chunk in new_index[rel]:
                    for term in chunk.get("terms", set()):
                        new_df[term] = new_df.get(term, 0) + 1
                continue

            try:
                if p.suffix.lower() in EXTRACTABLE_EXTENSIONS:
                    text = _extract_text(p)
                    if not text:
                        stats.skipped_files += 1
                        continue
                else:
                    text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                stats.skipped_files += 1
                continue

            chunks = _chunk_text(text, source=rel)
            if not chunks:
                continue

            # Build term sets per chunk
            chunk_records = []
            for c in chunks:
                low = c["text"].lower()          # keep for search-time reuse
                terms = set(_TOKEN_RE.findall(low))
                chunk_records.append({**c, "terms": terms, "text_low": low})
                for term in terms:
                    new_df[term] = new_df.get(term, 0) + 1
                stats.total_chunks += 1

            new_index[rel] = chunk_records
            self._indexed_hashes[rel] = current_hash

        self._index = new_index
        self._df = new_df
        # N must be counted in the SAME unit as df, and df is incremented once
        # per CHUNK above — not per file. Using len(new_index) (a FILE count)
        # made log((N+1)/(df+1)) go NEGATIVE as soon as a term appeared in more
        # chunks than the vault has files, which for any common term is
        # immediate: in a 5-file vault a term in 20 chunks scored idf=-0.25, so
        # `if score > 0` dropped it and the search reported "no relevant
        # results" for its most on-topic chunks. The more relevant the term,
        # the more certain the miss.
        self._n_docs = sum(len(chunks) for chunks in new_index.values())
        self._dirty = False
        return stats

    def search(self, query: str, n_results: Optional[int] = MAX_RESULTS,
               *, stats: Optional[Dict[str, Any]] = None
               ) -> List[Dict[str, Any]]:
        """The ranked chunks for ``query``.

        ``n_results=None`` returns the FULL ranked set (every chunk that scored
        above zero) — use it when the caller intends to write the whole list
        somewhere rather than read it into a prompt.

        ``stats`` is filled with the coverage of the search:
            matched   chunks that scored > 0
            returned  chunks handed back
            truncated True when matched > returned
        Measured need for this: with 12 equally-relevant files in a 52-file
        vault, the Council's top-4 returned an ARBITRARY 4 of the 12 (33%
        recall) and nothing said so. When many chunks tie, a top-N cap is a
        coin toss the user cannot see.
        """
        import math

        if self._dirty or not self._index:
            self.index_vault(self.vault_dir)

        if stats is not None:
            stats.update({"matched": 0, "returned": 0, "truncated": False})
        q_terms = set(_TOKEN_RE.findall(query.lower()))
        if not q_terms:
            return []

        n_docs = max(self._n_docs, 1)
        scored: List[tuple] = []

        for rel, chunks in self._index.items():
            for chunk in chunks:
                chunk_terms = chunk.get("terms", set())
                if not chunk_terms:
                    continue

                # Reuse the lowercased text cached at index time; fall back for
                # any record built before the field existed.
                text_low = chunk.get("text_low") or chunk["text"].lower()
                # TF-IDF score: sum over query terms of tf * idf
                score = 0.0
                for term in q_terms:
                    if term not in chunk_terms:
                        continue
                    # TF: term appears in chunk (binary presence, can extend to freq)
                    tf = text_low.count(term) / max(len(chunk_terms), 1)
                    # IDF: log(N / df+1) — rare terms score higher
                    df = self._df.get(term, 0)
                    idf = math.log((n_docs + 1) / (df + 1)) + 1.0
                    score += tf * idf

                if score > 0:
                    # Boost: prefer chunks where query terms appear close together
                    # Find best window where most terms co-occur
                    best_idx = 0
                    best_density = 0
                    for term in q_terms:
                        idx = text_low.find(term)
                        if idx >= 0:
                            window = text_low[max(0, idx-150):idx+150]
                            density = sum(1 for t in q_terms if t in window)
                            if density > best_density:
                                best_density = density
                                best_idx = max(0, idx - 150)

                    excerpt_start = best_idx
                    excerpt = chunk["text"][excerpt_start:excerpt_start + 600].strip()
                    if not excerpt:
                        excerpt = chunk["text"][:600].strip()

                    # Normalise score to 0-1 range
                    norm_score = min(1.0, score / (len(q_terms) * 3.0))
                    scored.append((norm_score, {
                        "text":     excerpt,
                        "source":   rel,
                        "score":    round(norm_score, 3),
                        "chunk_id": chunk.get("chunk_id", ""),
                    }))

        scored.sort(key=lambda x: x[0], reverse=True)
        out = [r for _, r in scored] if n_results is None \
            else [r for _, r in scored[:n_results]]
        if stats is not None:
            stats["matched"] = len(scored)
            stats["returned"] = len(out)
            stats["truncated"] = len(out) < len(scored)
        return out

    def search_adaptive(self, query: str, *, batch: int = 4,
                        floor_ratio: float = 0.8, hard_max: int = 200,
                        stats: Optional[Dict[str, Any]] = None
                        ) -> List[Dict[str, Any]]:
        """Grow the result window while the results keep earning their place.

        Take a batch; if EVERY hit in it is still strong (>= floor_ratio of the
        best score), take another. Stop at the first batch that does not fill
        with strong hits, keeping the strong ones from it. The size of the
        answer becomes a consequence of the data instead of a constant someone
        guessed.

        Why this beats any fixed N: a constant is wrong in both directions. On
        a vault with 12 equally-relevant files, n=4 returned an arbitrary third
        of them; on a question with 2 relevant files, n=12 pads the prompt with
        ten irrelevant ones. The relevance cliff is what separates signal from
        noise, and it is sharp in practice — measured on a 52-file vault, the
        on-topic chunks scored 0.219 and everything else 0.154.

        floor_ratio is RELATIVE to the top hit, never absolute: scores are
        query-dependent, and an absolute floor also cuts the weaker tail chunks
        OF a relevant file (measured at 0.110 — below an irrelevant file's
        0.154) while keeping the noise.

        The whole ranking is already in memory, so the batching costs nothing:
        it is how far down the list we walk, not extra retrieval.
        """
        full = self.search(query, n_results=None)
        if stats is not None:
            stats.update({"matched": len(full), "returned": 0,
                          "truncated": False, "batches": 0, "cutoff": 0.0})
        if not full:
            return []
        top = full[0].get("score", 0.0) or 0.0
        floor = top * floor_ratio
        kept: List[Dict[str, Any]] = []
        batches = 0
        for start in range(0, min(len(full), hard_max), batch):
            window = full[start:start + batch]
            strong = [h for h in window
                      if (h.get("score", 0.0) or 0.0) >= floor]
            kept.extend(strong)
            batches += 1
            if len(strong) < len(window):
                break            # the batch stopped filling — the cliff
        if stats is not None:
            stats["returned"] = len(kept)
            stats["truncated"] = len(kept) < len(full)
            stats["batches"] = batches
            stats["cutoff"] = round(floor, 4)
        return kept

    def collection_count(self) -> int:
        if self._dirty:
            return len(_collect_files(self.vault_dir))
        return sum(len(chunks) for chunks in self._index.values())


# Keep old name as alias for backwards compatibility
_KeywordBackend = _TFIDFBackend


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
            print("[VaultRAG] chromadb not installed — using TF-IDF search fallback")
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
