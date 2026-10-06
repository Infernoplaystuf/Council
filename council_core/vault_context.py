"""
council_core.vault_context — the vault passages a question needs, handed to
the council for that one question.

Until now the Qt council answered from the typed question alone (the Council
Map's biggest gap). Before a turn this:

  1. searches the vault index for the question (the process-wide VaultRAG,
     council_core.rag_jobs.for_vault — the one the Agents tab re-indexes);
  2. formats the best passages as a "VAULT CONTEXT:" block that names each
     file, so members can cite it;
  3. sets that block on the models for the length of the turn (`applied`),
     and restores what they had after.

WHO SEES IT is the engine's existing rule, not a new one: each model's
standing extra_context is folded into every respond(), and
council_engine.ROLE_CONTEXT_PROFILES gates a "VAULT CONTEXT:" block per role
— in full for the Writer, Coder, Sage and Strategist, the first 1,500
characters for the Peasant, none for the Intern, Artist, Skeptic and Judge
(the Judge stays evidence-blind by design; giving it evidence is a separate
step).

THE SEARCH: semantic when the vault already has a semantic index (the
Agents tab builds it); otherwise keyword (TF-IDF) search, which loads no
model and downloads nothing. A question never builds a semantic index or
loads an embedding model — that can take minutes and may fetch the model.

Never raises: a search that fails means no block and a `problem` to show.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, List, Optional

MARKER = "VAULT CONTEXT:"
MAX_PASSAGES = 6
MAX_CHARS = 8_000
PASSAGE_CHARS = 1_600

HEADER = (MARKER + "\nPassages from the user's vault that matched the "
          "question. Use them when they bear on it and name the file you "
          "used; if they do not answer it, say so rather than guess.\n")


@dataclass
class Brief:
    text: str = ""                     # the block; "" = nothing to add
    sources: List[str] = field(default_factory=list)
    backend: str = ""
    problem: str = ""

    def note(self) -> str:
        """One line for the transcript."""
        if self.problem:
            return f"Vault: {self.problem}"
        if not self.sources:
            return "Vault: nothing in the vault matched this question."
        shown = ", ".join(self.sources[:5])
        more = f" and {len(self.sources) - 5} more" if len(self.sources) > 5 \
            else ""
        return (f"Vault: {len(self.sources)} passage(s) given to the council "
                f"— {shown}{more}.")


KEYWORD_REFRESH_S = 300.0


class _Keyword:
    """The vault's keyword (TF-IDF) search: pure Python, no model, nothing
    downloaded. Indexes on first use and again when older than
    KEYWORD_REFRESH_S, so files added during a session are found."""

    backend_name = "keyword"

    def __init__(self, vault_dir: Path):
        import vault_rag
        self._backend = vault_rag._TFIDFBackend(Path(vault_dir))
        self._built = 0.0
        self._lock = threading.Lock()

    def search(self, query: str, n_results: int = MAX_PASSAGES):
        import time
        with self._lock:
            if time.monotonic() - self._built > KEYWORD_REFRESH_S:
                self._backend._dirty = True
                self._built = time.monotonic()
            return self._backend.search(query, n_results=n_results)


_keyword: dict = {}
_keyword_lock = threading.Lock()


def _rag_for(vault_dir: Path) -> Any:
    """Semantic search when the vault already HAS a semantic index (built in
    the Agents tab, this session or before); otherwise keyword search.

    Building the semantic index's search object loads an embedding model and,
    when it is not on this PC, tries to download it — measured: 7.8 s and a
    blocked download in a fresh vault. That must never happen because a
    question was asked, so only an index the user built is used."""
    from . import rag_jobs
    vault_dir = Path(vault_dir)
    index = rag_jobs.for_vault(vault_dir)
    if index.rag is not None:
        return index.rag
    chroma = index.chroma_dir
    try:
        has_index = chroma.is_dir() and any(chroma.iterdir())
    except OSError:
        has_index = False
    if has_index:
        return index.ensure()
    key = vault_dir.resolve()
    with _keyword_lock:
        if key not in _keyword:
            _keyword[key] = _Keyword(vault_dir)
        return _keyword[key]


def build(question: str, vault_dir: Path, *, rag: Any = None,
          max_passages: int = MAX_PASSAGES,
          max_chars: int = MAX_CHARS) -> Brief:
    """The VAULT CONTEXT block for `question`. Blocking (a search, and on
    the semantic backend an embedding) — call it from a worker."""
    question = (question or "").strip()
    if not question:
        return Brief()
    try:
        rag = rag if rag is not None else _rag_for(vault_dir)
    except Exception as exc:                              # noqa: BLE001
        return Brief(problem=f"the vault index could not be opened ({exc})")
    backend = str(getattr(rag, "backend_name", "") or "")
    if backend == "chromadb":
        try:
            if int(rag.collection_count()) == 0:
                return Brief(backend=backend, problem=(
                    "not indexed yet — Agents tab ▸ Re-index Vault Now, then "
                    "the council will read it."))
        except Exception:                                 # noqa: BLE001
            pass
    try:
        result = rag.search(question, n_results=max_passages)
    except Exception as exc:                              # noqa: BLE001
        return Brief(backend=backend,
                     problem=f"the vault search failed ({exc})")
    chunks = list(getattr(result, "chunks", result) or [])
    parts, sources, used = [HEADER], [], len(HEADER)
    for i, c in enumerate(chunks[:max_passages], start=1):
        text = str(c.get("text", "")).strip()
        source = str(c.get("source", "?"))
        if not text:
            continue
        if len(text) > PASSAGE_CHARS:
            text = text[:PASSAGE_CHARS] + " …"
        try:
            score = f" (match {float(c.get('score', 0)):.2f})"
        except (TypeError, ValueError):
            score = ""
        piece = f"\n[{i}] {source}{score}\n{text}\n"
        if used + len(piece) > max_chars:
            break
        parts.append(piece)
        used += len(piece)
        if source not in sources:
            sources.append(source)
    if not sources:
        return Brief(backend=backend)
    return Brief("".join(parts).rstrip(), sources, backend)


_apply_lock = threading.Lock()


@contextmanager
def applied(models: Iterable[Any], block: str) -> Iterator[None]:
    """For the length of the `with`, every model's standing extra_context
    also carries `block` (the engine then gates it per role). Restored after,
    whatever happens. A model without the attribute is left alone."""
    if not (block or "").strip():
        yield
        return
    seen, saved = set(), []
    with _apply_lock:
        for model in models:
            if model is None or id(model) in seen \
                    or not hasattr(model, "extra_context"):
                continue
            seen.add(id(model))
            before = model.extra_context or ""
            saved.append((model, before))
            model.extra_context = (f"{before}\n\n{block}".strip()
                                   if before.strip() else block)
    try:
        yield
    finally:
        with _apply_lock:
            for model, before in saved:
                model.extra_context = before


def turn_models(models: Any, roles: Optional[Iterable[str]] = None
                ) -> List[Any]:
    """The model objects a turn can call: every council role the build has."""
    from .council_turn import AGENT_NAMES
    names = list(roles) if roles is not None else list(AGENT_NAMES)
    out = [getattr(models, n, None) for n in names]
    sage = getattr(models, "sage_agent_obj", None)
    if sage is not None:
        out.append(getattr(sage, "model", None))
    return [m for m in out if m is not None]


__all__ = ["MARKER", "Brief", "build", "applied", "turn_models"]
