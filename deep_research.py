"""
deep_research.py — iterative local-corpus research over LocalMemory.

The "search engine" is ``inferno_local.local_memory.LocalMemory`` indexed
over the user's own documents. There is no web, no SearXNG, no external
framework. Every citation is a local chunk id (``doc:{sha1_8}:{chunk}``)
or an analysis id (``analysis:{session}:{turn}``); never a URL.

Pipeline:

    index_folder(folder, mem, chunk_size, overlap)
        Walk ``folder`` for supported text-like files, chunk them, and
        add each chunk via ``council_memory.record_document_chunk`` so
        ids stay consistent with the rest of the app.

    deep_research(question, runner, mem, *, max_iters=3, sections=4)
        Loop:
          1. Ask the runner to break ``question`` into ``sections``
             sub-questions, returned as a JSON list.
          2. For each sub-question retrieve top-k chunks from memory.
          3. Ask the runner to draft that section using ONLY those
             chunks, citing chunk ids inline.
          4. Assemble the sections into a markdown report with a final
             "Sources" footer listing every cited id.

Returns a ``Report`` dataclass with text + cited_ids + per-section
provenance. Pure data — easy to render to terminal, file, or Tk
text widget.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from inferno_local.local_memory import LocalMemory, MemoryHit, make_doc_id

_LOG = logging.getLogger("deep_research")


# ────────────────────────────────────────────────────────
# Indexing
# ────────────────────────────────────────────────────────

_TEXTLIKE_SUFFIXES = {
    ".txt", ".md", ".rst", ".log",
    ".csv", ".tsv",
    ".json", ".yaml", ".yml", ".ini", ".toml",
    ".html",
    ".py", ".sql", ".sh", ".bat",
}


def _chunk_text(text: str, *, chunk_size: int = 1200,
                overlap: int = 200) -> List[str]:
    """Sliding-window chunking, char-based. Boundaries respect paragraphs
    where possible — we look back up to ``overlap`` chars for a blank
    line; if found, we break there instead of mid-sentence."""
    if not text:
        return []
    chunks: List[str] = []
    n = len(text)
    i = 0
    while i < n:
        end = min(n, i + chunk_size)
        # Try to backtrack to a paragraph break inside the overlap window.
        if end < n:
            window_start = max(i + chunk_size - overlap, i)
            window = text[window_start:end]
            br = window.rfind("\n\n")
            if br > 0:
                end = window_start + br
        chunks.append(text[i:end].strip())
        if end >= n:
            break
        i = max(end - overlap, i + 1)
    return [c for c in chunks if c]


def index_folder(folder: Path,
                 mem: LocalMemory,
                 *,
                 chunk_size: int = 1200,
                 overlap: int = 200,
                 on_progress: Optional[Callable[[str], None]] = None,
                 max_files: int = 5000) -> Dict[str, Any]:
    """Walk ``folder``, chunk text-like files, store every chunk in
    ``mem`` using the canonical ``doc:{sha1_8}:{chunk}`` id scheme.

    Returns a tally so the caller can show progress.
    """
    folder = Path(folder).expanduser().resolve()
    if not folder.exists():
        raise FileNotFoundError(f"index_folder: {folder} does not exist")
    if not folder.is_dir():
        raise NotADirectoryError(f"index_folder: {folder} is not a directory")

    tally = {"files": 0, "chunks": 0, "skipped": 0}
    for f in sorted(folder.rglob("*")):
        if tally["files"] >= max_files:
            break
        if not f.is_file():
            continue
        if f.suffix.lower() not in _TEXTLIKE_SUFFIXES:
            tally["skipped"] += 1
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            tally["skipped"] += 1
            continue
        chunks = _chunk_text(text, chunk_size=chunk_size, overlap=overlap)
        if not chunks:
            tally["skipped"] += 1
            continue
        rel = str(f.relative_to(folder))
        for idx, body in enumerate(chunks):
            rid = make_doc_id(rel, idx)
            try:
                mem.add(rid, body, {
                    "source":    rel,
                    "chunk_idx": idx,
                    "abs_path":  str(f),
                    "kind":      "doc",
                })
                tally["chunks"] += 1
            except Exception as exc:
                _LOG.warning("add failed for %s chunk %d: %r", rel, idx, exc)
                tally["skipped"] += 1
        tally["files"] += 1
        if on_progress and tally["files"] % 25 == 0:
            on_progress(f"indexed {tally['files']} files, "
                        f"{tally['chunks']} chunks")
    if on_progress:
        on_progress(f"done: {tally['files']} files, "
                    f"{tally['chunks']} chunks, {tally['skipped']} skipped")
    return tally


# ────────────────────────────────────────────────────────
# Research loop
# ────────────────────────────────────────────────────────

@dataclass
class SectionDraft:
    sub_question: str
    citations: List[MemoryHit] = field(default_factory=list)
    body: str = ""


@dataclass
class Report:
    question: str
    sections: List[SectionDraft] = field(default_factory=list)
    cited_ids: List[str] = field(default_factory=list)

    def to_markdown(self) -> str:
        out: List[str] = [f"# {self.question}\n"]
        for i, sec in enumerate(self.sections, 1):
            out.append(f"## {i}. {sec.sub_question}\n")
            out.append(sec.body.strip() or "(no draft produced)")
            if sec.citations:
                ids = ", ".join(f"`{h.id}`" for h in sec.citations)
                out.append(f"\n*Sources for this section:* {ids}")
            out.append("")
        if self.cited_ids:
            out.append("---\n## Sources")
            for cid in self.cited_ids:
                out.append(f"- `{cid}`")
        return "\n".join(out)


_JSON_LIST_RX = re.compile(r"\[\s*\".*?\"\s*(?:,\s*\".*?\"\s*)*\]", re.DOTALL)


def _parse_subquestions(reply: str, *, want: int) -> List[str]:
    """Best-effort: find the first JSON list of strings. If the model
    didn't return JSON, split on newlines and trim numbering."""
    m = _JSON_LIST_RX.search(reply or "")
    if m:
        try:
            arr = json.loads(m.group(0))
            return [str(x).strip() for x in arr if str(x).strip()][:want]
        except Exception:
            pass
    # Fallback — numbered list
    lines = [
        re.sub(r"^\s*[\-\*\d\.\)]+\s*", "", ln).strip()
        for ln in (reply or "").splitlines()
    ]
    lines = [ln for ln in lines if ln]
    return lines[:want]


def deep_research(question: str,
                  runner,
                  mem: LocalMemory,
                  *,
                  max_iters: int = 1,
                  sections: int = 4,
                  k_per_section: int = 5,
                  temperature: float = 0.2,
                  max_tokens_section: int = 400) -> Report:
    """One-pass research over the local corpus.

    ``max_iters`` is reserved for a future refinement pass — kept in the
    signature so call sites stay stable when we add iterative refinement.
    Bound: even in a future iterative version, total LLM calls stay
    O(sections × max_iters).
    """
    if not (question or "").strip():
        raise ValueError("deep_research: empty question")
    if sections < 1:
        raise ValueError("deep_research: sections must be >= 1")

    # 1) Sub-questions
    plan_prompt = (
        "Break the user question into "
        f"{sections} narrowly-scoped sub-questions for a local-corpus "
        "research report. Reply with a JSON list of strings only.\n\n"
        f"USER QUESTION: {question}\n"
    )
    plan_reply = runner.chat(
        [{"role": "user", "content": plan_prompt}],
        temperature=temperature, max_tokens=300,
    )
    subqs = _parse_subquestions(plan_reply, want=sections)
    if not subqs:
        subqs = [question]      # degenerate fallback — still produces output

    # 2-3) For each sub-question, retrieve and draft
    report = Report(question=question)
    seen_ids: List[str] = []
    for sq in subqs:
        hits = mem.query(sq, k=k_per_section)
        sec = SectionDraft(sub_question=sq, citations=list(hits))
        if not hits:
            sec.body = "(no relevant chunks found in the local corpus)"
            report.sections.append(sec)
            continue
        # Build the section prompt — citations referenced inline as ids.
        sources_block = "\n---\n".join(
            f"[{h.id}] (source: {h.metadata.get('source', '?')}, "
            f"chunk {h.metadata.get('chunk_idx', '?')})\n{h.text}"
            for h in hits
        )
        section_prompt = (
            "Draft this section of a research report using ONLY the "
            "sources below. Cite each source inline as [chunk-id]. If "
            "the sources don't answer the question, say so.\n\n"
            f"REPORT TOPIC: {question}\n"
            f"SECTION: {sq}\n\n"
            f"SOURCES:\n{sources_block}\n\n"
            "ANSWER:"
        )
        sec.body = runner.chat(
            [{"role": "user", "content": section_prompt}],
            temperature=temperature, max_tokens=max_tokens_section,
        ).strip()
        report.sections.append(sec)
        for h in hits:
            if h.id not in seen_ids:
                seen_ids.append(h.id)
    report.cited_ids = seen_ids
    return report
