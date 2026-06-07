"""
council_memory.py — LocalMemory integration for the Council deliberation loop.

Wraps ``inferno_local.local_memory.LocalMemory`` with the Council-side
conventions:

  • single persistent store rooted at  ~/.council/vault/.local_memory/
  • stable id scheme:
        analysis:{session}:{turn}    Council deliberation outputs
        doc:{sha1_8}:{chunk}         indexed user documents
        note:{slug}                  one-shot user notes
  • optional sentence-transformer embedder (CPU by default — see brief §0
    on the 5080 cu124 PTX-fallback flakiness; native sm_89 hardware can
    flip COUNCIL_MEMORY_DEVICE=cuda).
  • a ``before_deliberation(question, k)`` retriever that returns a
    citation-formatted block ready to splice into a Council system prompt
  • a ``record_deliberation(session, turn, question, answer)`` ingest that
    keeps the model's own past output retrievable for the next turn.

Call sites in council_engine should be:

    # ── before the Council turn fires ──
    ctx = council_memory.before_deliberation(user_question)
    if ctx:
        messages.insert(0, {"role": "system", "content": ctx})

    # ── after the answer settles ──
    council_memory.record_deliberation(session_id, turn_idx,
                                       user_question, final_answer)

The wrapper is **idempotent** — every helper here can be called without
worrying about init order. The underlying ChromaDB collection is lazy.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable, List, Optional

from inferno_local.local_memory import (
    LocalMemory, MemoryHit, make_analysis_id, make_doc_id,
)

_LOG = logging.getLogger("council_memory")

# ── singleton ────────────────────────────────────────────
_INSTANCE: Optional[LocalMemory] = None
_EMBEDDER = None


def _persist_dir() -> Path:
    """Where memory lives. Honours COUNCIL_VAULT_ROOT so the env override
    a user set in the GUI/wizard is respected here too."""
    if os.environ.get("COUNCIL_VAULT_ROOT"):
        return Path(os.environ["COUNCIL_VAULT_ROOT"]).expanduser().resolve() / ".local_memory"
    return Path.home() / ".council" / "vault" / ".local_memory"


def _build_embedder():
    """Lazy embedder. None on import error — LocalMemory falls back to
    substring search and the rest of the app keeps working. Honour
    COUNCIL_MEMORY_DEVICE so the user can pin cpu/cuda."""
    global _EMBEDDER
    if _EMBEDDER is not None:
        return _EMBEDDER
    try:
        from inferno_local.local_memory import sbert_embedder
        device = os.environ.get("COUNCIL_MEMORY_DEVICE", "cpu")
        model = os.environ.get("COUNCIL_MEMORY_MODEL", "all-MiniLM-L6-v2")
        _EMBEDDER = sbert_embedder(model, device=device)
    except Exception as exc:
        _LOG.warning("council_memory: embedder unavailable, falling back to "
                     "substring search (%r)", exc)
        _EMBEDDER = None
    return _EMBEDDER


def get_memory() -> LocalMemory:
    """Return the singleton LocalMemory. Constructs on first call."""
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = LocalMemory(
            _persist_dir(),
            embedder=_build_embedder(),
            collection="council_memory",
        )
    return _INSTANCE


def reset_for_tests(persist_dir: Optional[Path] = None,
                    embedder=None) -> LocalMemory:
    """Re-bind the singleton — used by tests so they can swap a temp dir
    + fake embedder. Production callers should never hit this."""
    global _INSTANCE, _EMBEDDER
    _EMBEDDER = embedder
    _INSTANCE = LocalMemory(
        persist_dir or _persist_dir(),
        embedder=embedder,
        collection="council_memory_test",
    )
    return _INSTANCE


# ── retrieval ────────────────────────────────────────────
def before_deliberation(question: str, *, k: int = 5,
                        max_chars: int = 1800) -> str:
    """Return a citation-formatted memory block to inject into the
    Council's system prompt before deliberation starts.

    Empty string on no hits — caller can just not splice anything in.
    Bounded by ``max_chars`` so we don't crowd the model's context
    window with retrieval slop. Citations look like:

        [memory analysis:session-42:7]
        ...stored text excerpt...

    so the model can mention them inline and an auditor can pull up the
    original via ``LocalMemory.query(text)`` / `chromadb get(ids=...)``.
    """
    if not (question or "").strip():
        return ""
    try:
        hits = get_memory().query(question, k=k)
    except Exception as exc:
        _LOG.warning("memory query failed: %r", exc)
        return ""
    if not hits:
        return ""
    blocks: List[str] = ["# Prior context — Council memory"]
    used = 0
    for h in hits:
        # Cap excerpt length per hit so one mega-record can't take it all.
        excerpt = (h.text or "").strip()
        if len(excerpt) > 600:
            excerpt = excerpt[:600] + "…"
        block = f"\n[memory {h.id}] (relevance {1.0 - h.distance:.2f})\n{excerpt}\n"
        if used + len(block) > max_chars:
            break
        blocks.append(block)
        used += len(block)
    return "\n".join(blocks)


# ── ingest ───────────────────────────────────────────────
def record_deliberation(session: str, turn: int,
                        question: str, answer: str,
                        *, extra_metadata: Optional[dict] = None) -> str:
    """Persist a Council turn so future deliberations in the same (or any)
    session can retrieve it. Returns the id used.

    The stored text is "Q: ... \\nA: ..." — preserving both sides means a
    retrieval against "who asked about X" still works against the answer
    text, and one against "what did we conclude" works against the answer
    side. Cheap and durable."""
    rid = make_analysis_id(session, turn)
    md = {"session": session, "turn": int(turn),
          "kind": "deliberation"}
    if extra_metadata:
        md.update(extra_metadata)
    text = f"Q: {question.strip()}\nA: {answer.strip()}"
    try:
        get_memory().add(rid, text, md)
    except Exception as exc:
        _LOG.warning("memory add failed for %s: %r", rid, exc)
    return rid


def record_document_chunk(source: str, chunk_idx: int, text: str,
                          *, extra_metadata: Optional[dict] = None) -> str:
    """Persist one chunk of an indexed user document. Returns the id."""
    rid = make_doc_id(source, chunk_idx)
    md = {"source": source, "chunk_idx": int(chunk_idx), "kind": "doc"}
    if extra_metadata:
        md.update(extra_metadata)
    try:
        get_memory().add(rid, text, md)
    except Exception as exc:
        _LOG.warning("memory add failed for %s: %r", rid, exc)
    return rid


def status() -> dict:
    return get_memory().status()
