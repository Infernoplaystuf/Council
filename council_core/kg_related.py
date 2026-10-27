"""Related by meaning (Part B of the proposal agreed with the user,
2026-10-27: "We should have these are related connections").

Toolkit-free. A local embedding model turns each passage of a document's
text, and each table's card (kg_tables), into a vector. Two items - two
documents, two tables, or one of each - are RELATED_TO when their vectors are
close: the cosine of their averages is at least the graph's threshold AND
each is among the other's ``TOP_K`` closest (one generic file then cannot
relate to everything). The link starts suggested; its evidence is the two
passages closest to each other, quoted side by side.

Only models already on this PC are used - an Ollama embedding model, or a
sentence-transformers model already in the local Hugging Face cache, loaded
with local_files_only. The Council never downloads one. The ones to
recommend are US-origin: granite-embedding (IBM) and nomic-embed-text
(Nomic).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

#: Closest items each item is compared with: a link needs both ends to be in
#: the other's top TOP_K.
TOP_K = 5
#: The cosine a link needs, unless the graph sets its own.
DEFAULT_THRESHOLD = 0.75
#: Words in a passage of a document's text.
PASSAGE_WORDS = 200
#: Passages of one document embedded (a 2,000-page manual is its first
#: MAX_PASSAGES passages, about 200,000 words).
MAX_PASSAGES = 1000
#: Characters of a passage quoted in a link's evidence.
QUOTE_CHARS = 160
#: Texts sent to the model in one call.
BATCH = 32
RECOMMENDED = ("granite-embedding", "nomic-embed-text")
_EMBED_FAMILIES = {"bert", "nomic-bert", "xlm-roberta", "gte", "e5"}

Embedder = Callable[[List[str]], np.ndarray]

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS embeddings (
    item TEXT NOT NULL,                -- a document's id or a TABLE entity's id
    kind TEXT NOT NULL,                -- document | table
    document_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,        -- what was embedded (the file, or the card)
    model TEXT NOT NULL, idx INTEGER NOT NULL,
    locator TEXT NOT NULL, text TEXT NOT NULL,
    vec BLOB NOT NULL,                 -- float32, normalised
    PRIMARY KEY (item, model, idx)
);
CREATE INDEX IF NOT EXISTS embeddings_model ON embeddings(model);
"""


# ── the models on this PC ────────────────────────────────────────────────
def available(host: Optional[str] = None) -> List[Tuple[str, str]]:
    """(id, label) of every embedding model already on this PC: Ollama's
    ('ollama:granite-embedding:30m') and cached sentence-transformers ones
    ('st:sentence-transformers/all-MiniLM-L6-v2'). Recommended ones first."""
    out: List[Tuple[str, str]] = []
    try:
        from council_core import local_models as lm
        for m in lm.ollama_tags(host) or []:
            name = str(m.get("name") or m.get("model") or "")
            fam = str((m.get("details") or {}).get("family") or "").lower()
            if name and ("embed" in name.lower() or fam in _EMBED_FAMILIES):
                out.append((f"ollama:{name}", f"{name} (Ollama)"))
    except Exception:                                     # noqa: BLE001
        pass
    for name in cached_sentence_transformers():
        out.append((f"st:{name}", f"{name} (on this PC)"))

    def rank(x):
        return (not any(r in x[0] for r in RECOMMENDED), x[1].lower())
    return sorted(out, key=rank)


def _hf_cache() -> Path:
    hub = os.environ.get("HF_HUB_CACHE", "").strip()
    if hub:
        return Path(hub)
    home = os.environ.get("HF_HOME", "").strip()
    return (Path(home) if home else Path.home() / ".cache" / "huggingface") / "hub"


def cached_sentence_transformers() -> List[str]:
    """Sentence-transformers models already in the local cache: a
    'models--org--name' folder with a config_sentence_transformers.json."""
    root = _hf_cache()
    out = []
    try:
        for d in sorted(root.glob("models--*")):
            if any(d.glob("snapshots/*/config_sentence_transformers.json")):
                out.append(d.name[len("models--"):].replace("--", "/"))
    except OSError:
        pass
    return out


def make(model_id: str, host: Optional[str] = None) -> Embedder:
    """The embedder ``model_id`` names (an id from available())."""
    if model_id.startswith("ollama:"):
        return _ollama(model_id[len("ollama:"):], host)
    if model_id.startswith("st:"):
        return _sentence_transformer(model_id[len("st:"):])
    raise ValueError(f"not an embedding model id: {model_id!r}")


def _ollama(name: str, host: Optional[str]) -> Embedder:
    from council_core import local_models as lm
    url = (host or lm.ollama_host()).rstrip("/")
    lm.require_local(url)

    def embed(texts: List[str]) -> np.ndarray:
        got = lm._get_json(url + "/api/embed", 300.0, {"model": name, "input": list(texts)})
        return normalise(np.asarray(got.get("embeddings") or [], dtype=np.float32))
    return embed


def _sentence_transformer(name: str) -> Embedder:
    # never a download: the cache only, and the hub told to stay offline
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(name, local_files_only=True)

    def embed(texts: List[str]) -> np.ndarray:
        return normalise(np.asarray(model.encode(list(texts), show_progress_bar=False),
                                    dtype=np.float32))
    return embed


# ── what is embedded ─────────────────────────────────────────────────────
def normalise(m: np.ndarray) -> np.ndarray:
    if m.size == 0 or m.ndim != 2:
        return np.zeros((0, 0), dtype=np.float32)
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return (m / n).astype(np.float32)


def passages(lines: Sequence[Sequence[Any]], words: int = PASSAGE_WORDS
             ) -> List[Tuple[Dict[str, Any], str]]:
    """A document's text lines ([page, line, text]) as passages of about
    ``words`` words: (locator of its first line, text)."""
    out: List[Tuple[Dict[str, Any], str]] = []
    buf: List[str] = []
    n = 0
    start: Optional[Dict[str, Any]] = None
    for ln in lines:
        page, line, text = (list(ln) + [None, None, ""])[:3]
        text = " ".join(str(text or "").split())
        if not text:
            continue
        if start is None:
            start = {"line": line} if page in (None, "") else {"page": page, "line": line}
        buf.append(text)
        n += len(text.split())
        if n >= words:
            out.append((start, " ".join(buf)))
            buf, n, start = [], 0, None
            if len(out) >= MAX_PASSAGES:
                return out
    if buf and start is not None:
        out.append((start, " ".join(buf)))
    return out


def table_card(db, table, rows: int = 5) -> str:
    """What a table is, in words a model can compare: its file, sheet,
    columns and first rows (kg_tables.Table)."""
    ids = [c.field_id for c in table.columns]
    labels = {c.field_id: c.label for c in table.columns}
    ph = ",".join("?" * len(ids))
    first = [r[0] for r in db.execute(
        f"SELECT DISTINCT row FROM cells WHERE field_id IN ({ph}) ORDER BY row LIMIT ?",
        (*ids, rows))]
    lines = [f"Table {table.name}. Columns: {', '.join(labels[i] for i in ids)}."]
    if first:
        got: Dict[int, List[str]] = {}
        for fid, row, text in db.execute(
                f"SELECT field_id, row, text FROM cells WHERE field_id IN ({ph}) AND row IN"
                f" ({','.join('?' * len(first))}) ORDER BY row, col", (*ids, *first)):
            got.setdefault(row, []).append(f"{labels[fid]}: {text}")
        lines += ["; ".join(v) for _r, v in sorted(got.items())]
    return " ".join(lines)[:4000]


# ── which items are related ──────────────────────────────────────────────
def related_pairs(vecs: np.ndarray, threshold: float, k: int = TOP_K,
                  apart: Optional[Sequence[Any]] = None) -> List[Tuple[int, int, float]]:
    """(i, j, cosine) with i < j for every two rows of ``vecs`` (normalised)
    that are each in the other's top ``k`` and at least ``threshold``.
    ``apart``: a group per row (its document); rows of one group are never
    paired (a document and its own table)."""
    n = len(vecs)
    if n < 2:
        return []
    sim = vecs @ vecs.T
    np.fill_diagonal(sim, -1.0)
    if apart is not None:
        g = np.asarray([str(x) for x in apart])
        sim[g[:, None] == g[None, :]] = -1.0
    kk = min(k, n - 1)
    top = np.argpartition(-sim, kk - 1, axis=1)[:, :kk]
    tops = [set(map(int, r)) for r in top]
    out = []
    for i in range(n):
        for j in tops[i]:
            if j > i and i in tops[j] and sim[i, j] >= threshold:
                out.append((i, j, float(sim[i, j])))
    return sorted(out)


def best_pair(a: np.ndarray, b: np.ndarray) -> Tuple[int, int, float]:
    """The closest passage of ``a`` to one of ``b``: (index in a, index in
    b, cosine)."""
    sim = a @ b.T
    i, j = np.unravel_index(int(np.argmax(sim)), sim.shape)
    return int(i), int(j), float(sim[i, j])


def quote(text: str) -> str:
    t = " ".join(text.split())
    return t if len(t) <= QUOTE_CHARS else t[:QUOTE_CHARS - 1].rstrip() + "…"


def to_blob(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype=np.float32).tobytes()


def from_blob(b: bytes) -> np.ndarray:
    return np.frombuffer(b, dtype=np.float32)


def loc_json(loc: Dict[str, Any]) -> str:
    return json.dumps(loc, sort_keys=True)
