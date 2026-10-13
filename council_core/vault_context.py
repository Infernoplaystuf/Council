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
characters for the Peasant, none for the Intern, Artist, Skeptic and Judge.
The Judge gets the same passages another way (`evidence`): only when it
ranks and critiques, never when it routes.

THE SEARCH: semantic when the vault already has a semantic index (the
Agents tab builds it); otherwise keyword (TF-IDF) search, which loads no
model and downloads nothing. A question never builds a semantic index or
loads an embedding model — that can take minutes and may fetch the model.
A hit from the app's own files (`is_app_source`: older conversations, logs,
learned memory, settings, dot-folders) is dropped: an index built before
they were protected still lists them.

BESIDE THE TURN CONTEXT. The Council tab also puts council_core.turn_context
.prepare_turn's blocks — the files the user names, vault and value matches,
the analyst's figures, the task memo — in front of the question, sized to
the Writer's call. These passages come second: `fit` drops any from a file
that is already there and cuts the rest, at a passage, to the room the
Writer's call has left, so the engine never has to cut the call itself.

Never raises: a search that fails means no block and a `problem` to show.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import (Any, Callable, Iterable, Iterator, List, Optional,
                    Tuple)

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
    #: The passages the block is made of, in order: (source, the passage as
    #: it stands in the block) — so `fit` can drop or leave out some.
    pieces: List[Tuple[str, str]] = field(default_factory=list)

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


#: How many hits a search is asked for per passage wanted: room for the
#: app's own files `users_chunks` drops from an index built before they were
#: protected, so a question still gets its passages.
OVERFETCH = 3


def is_app_source(source: Any, vault_dir: Path) -> bool:
    """Whether a search hit's file (as the index names it: relative to the
    vault, or absolute) is the app's own and not the user's: in a dot-folder
    of the vault (.council_memory, .council_projects …), or protected
    (conversation_logger.is_protected_path — older conversations, logs/,
    learned memory, the judged-question history, the app's settings).

    The index's own walk skips these (vault_rag._collect_files), but an index
    built BEFORE they were protected — the Tk shell builds one at every start
    — still lists them until a re-index removes them; the user's own index
    did (memory/ and logs/). A hit from one is never handed over: it would
    put an older session or learned memory into a prompt past "This
    conversation only" (council_core.memory_scope). turn_context drops such
    hits from its own index the same way."""
    raw = str(source or "").strip()
    if not raw or raw == "?":
        return False
    vault_dir = Path(vault_dir)
    path = Path(raw)
    if not path.is_absolute():
        path = vault_dir / path
    try:
        rel = path.resolve().relative_to(vault_dir.resolve())
    except (OSError, ValueError):
        rel = None
    if rel is not None and any(part.startswith(".") for part in rel.parts):
        return True
    try:
        import conversation_logger
        return bool(conversation_logger.is_protected_path(path, vault_dir))
    except Exception:                                     # noqa: BLE001
        return True


def users_chunks(chunks: Iterable[Any], vault_dir: Path) -> List[Any]:
    """`chunks` (search hits with a "source") without the app's own files
    (`is_app_source`), in order."""
    out = []
    for chunk in chunks or ():
        source = chunk.get("source", "?") if isinstance(chunk, dict) else "?"
        if not is_app_source(source, vault_dir):
            out.append(chunk)
    return out


def build(question: str, vault_dir: Path, *, rag: Any = None,
          max_passages: int = MAX_PASSAGES,
          max_chars: int = MAX_CHARS) -> Brief:
    """The VAULT CONTEXT block for `question`. Blocking (a search, and on
    the semantic backend an embedding) — call it from a worker. A hit from
    one of the app's own files (`is_app_source`) is never in it."""
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
        result = rag.search(question,
                            n_results=max(1, max_passages) * OVERFETCH)
    except Exception as exc:                              # noqa: BLE001
        return Brief(backend=backend,
                     problem=f"the vault search failed ({exc})")
    chunks = users_chunks(getattr(result, "chunks", result) or [],
                          vault_dir)
    parts, sources, used = [HEADER], [], len(HEADER)
    pieces: List[Tuple[str, str]] = []
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
        pieces.append((source, piece))
        used += len(piece)
        if source not in sources:
            sources.append(source)
    if not sources:
        return Brief(backend=backend)
    return Brief("".join(parts).rstrip(), sources, backend, pieces=pieces)


def fit(brief: Brief, max_chars: Optional[int], *,
        skip: Callable[[str], bool] = lambda _source: False) -> Brief:
    """`brief` without the passages whose file ``skip(source)`` says the
    question already carries, and cut — at a passage — to ``max_chars``
    (None: no limit). The Qt Council puts council_core.turn_context's
    blocks in front of the question first, sized to the Writer's call, and
    gives this block only the room that call has left: so a passage never
    repeats a file that is already there, and the call is never cut by the
    engine to make it fit. A brief without its passages (built by hand) is
    kept whole when it fits, dropped when it does not."""
    if not (brief.text or "").strip():
        return brief
    limit = None if max_chars is None else max(0, int(max_chars))
    if not brief.pieces:
        if limit is not None and len(brief.text) > limit:
            return Brief(backend=brief.backend, problem=(
                "no room left in the Writer's prompt for vault passages"))
        return brief
    parts, sources, kept, used = [HEADER], [], [], len(HEADER)
    left_out = False
    for source, piece in brief.pieces:
        try:
            dup = skip(source)
        except Exception:                                 # noqa: BLE001
            dup = False
        if dup:
            continue
        if limit is not None and used + len(piece) > limit:
            left_out = True
            break
        parts.append(piece)
        kept.append((source, piece))
        used += len(piece)
        if source not in sources:
            sources.append(source)
    if not kept:
        return Brief(backend=brief.backend, problem=(
            "no room left in the Writer's prompt for vault passages"
            if left_out else
            "the passages it found are from files already in front of the "
            "question"))
    return Brief("".join(parts).rstrip(), sources, brief.backend,
                 pieces=kept)


EVIDENCE_MARKER = "EVIDENCE FROM THE USER'S VAULT:"
EVIDENCE_HEADER = (
    EVIDENCE_MARKER + "\nUse these passages to check the candidates' facts. "
    "Prefer an answer the evidence supports; name any claim it contradicts; "
    "an answer that ignores evidence which bears on the question should not "
    "win. If the passages do not bear on the question, ignore them.\n")


def evidence(brief: Brief) -> str:
    """The same passages, worded for the Judge. A different marker on
    purpose: the engine strips a VAULT CONTEXT block from the Judge (it
    routes without context, by design), but ranking and critique are where
    evidence belongs. Passed through ctx.shared["judge_evidence"]."""
    text = getattr(brief, "text", "") or ""
    if not text.startswith(HEADER):
        return ""
    return EVIDENCE_HEADER + text[len(HEADER):]


def sage_block(question: str, vault_dir: Path) -> str:
    """What the Sage's own knowledge base (<vault>/sage_knowledge, taught
    in the Agents tab) holds for this question — its domains, facts and the
    user's corrections — or "". The SageAgent wrapper injected this; the Qt
    turn never used that wrapper, so the base was built and never read."""
    try:
        import sage_agent
        kb = sage_agent.SageKnowledge(Path(vault_dir) / "sage_knowledge")
        return kb.build_context_block(question or "")
    except Exception:                                     # noqa: BLE001
        return ""


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


__all__ = ["MARKER", "EVIDENCE_MARKER", "Brief", "build", "fit", "applied",
           "turn_models", "evidence", "sage_block", "is_app_source",
           "users_chunks"]
