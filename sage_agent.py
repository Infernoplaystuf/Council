"""
sage_agent.py — The Sage: a tunable, vault-aware council personality.

The Sage differs from other council members in three ways:
  1. It reads directly from a dedicated knowledge base in the vault
     (~/.council/vault/sage_knowledge/) and uses it when answering.
  2. It explicitly flags knowledge gaps when it cannot answer confidently,
     so the user knows what data to pull to improve the council.
  3. It accepts corrections and new knowledge through a simple tuning API
     that writes to the vault — accumulating expertise over time.

Files it manages (all inside VAULT_DIR/sage_knowledge/):
  facts.jsonl        — structured facts: {topic, fact, source, ts, confidence}
  corrections.jsonl  — user corrections: {query, wrong_answer, correction, ts}
  domains.jsonl      — domain declarations: {domain, description, ts}
  gaps.jsonl         — logged gaps: {query, ts, reason}
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# TF-IDF tokenizer (3+ alphanumerics), compiled once — used for the query and
# once per record during scoring.
_SAGE_TOKEN_RE = re.compile(r"[a-zA-Z0-9]{3,}")
from typing import Any, Callable, Dict, List, Optional


# ── Helpers ─────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_jsonl(path: Path) -> List[Dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _append_jsonl(path: Path, record: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ── Knowledge store ──────────────────────────────────────────────────────────

class SageKnowledge:
    """
    Manages the Sage's persistent knowledge base inside the vault.
    All writes are append-only JSONL so nothing is ever lost.
    """

    def __init__(self, sage_dir: Path):
        self.sage_dir      = sage_dir
        self.facts_path    = sage_dir / "facts.jsonl"
        self.corr_path     = sage_dir / "corrections.jsonl"
        self.domains_path  = sage_dir / "domains.jsonl"
        self.gaps_path     = sage_dir / "gaps.jsonl"
        sage_dir.mkdir(parents=True, exist_ok=True)

    # ── Read ─────────────────────────────────────────────────────────────

    def get_facts(self) -> List[Dict]:
        return _read_jsonl(self.facts_path)

    def get_corrections(self) -> List[Dict]:
        return _read_jsonl(self.corr_path)

    def get_domains(self) -> List[Dict]:
        return _read_jsonl(self.domains_path)

    def get_gaps(self) -> List[Dict]:
        return _read_jsonl(self.gaps_path)

    def search_relevant(self, query: str, max_items: int = 12) -> List[Dict]:
        """
        TF-IDF search against facts and corrections.
        Scores by term frequency * inverse document frequency so rare,
        specific terms in the Sage's knowledge base rank higher than common ones.
        Falls back to simple hit-count if math unavailable.
        """
        import math as _math

        q_words = set(re.findall(r"[a-zA-Z0-9]{3,}", query.lower()))
        if not q_words:
            return []

        facts       = self.get_facts()
        corrections = self.get_corrections()
        all_records = (
            [{**r, "record_type": "fact",       "_text": " ".join([r.get("topic",""), r.get("fact",""), r.get("source","")])} for r in facts] +
            [{**r, "record_type": "correction", "_text": " ".join([r.get("query",""), r.get("correction","")])} for r in corrections]
        )
        n_docs = max(len(all_records), 1)

        # Lowercase + tokenize each record ONCE into parallel arrays (both the
        # DF pass and the scoring pass need them). Kept OFF the rec dict — the
        # result cleaner below only strips "_text", so extra keys would leak.
        rec_text_low = [rec["_text"].lower() for rec in all_records]
        rec_terms = [set(_SAGE_TOKEN_RE.findall(tl)) for tl in rec_text_low]

        # Build document frequency for IDF
        df: Dict[str, int] = {}
        for terms in rec_terms:
            for t in terms:
                df[t] = df.get(t, 0) + 1

        scored: List[tuple] = []
        for _i, rec in enumerate(all_records):
            text_low = rec_text_low[_i]
            terms    = rec_terms[_i]
            n_terms  = max(len(terms), 1)

            score = 0.0
            for w in q_words:
                if w not in terms:
                    continue
                tf  = text_low.count(w) / n_terms
                idf = _math.log((n_docs + 1) / (df.get(w, 0) + 1)) + 1.0
                score += tf * idf

                # Boost: exact topic/query match is more valuable
                if rec["record_type"] == "fact" and w in rec.get("topic","").lower():
                    score += idf * 0.5
                if rec["record_type"] == "correction" and w in rec.get("query","").lower():
                    score += idf * 0.5

            # Boost high-confidence facts — ONCE per record. Inside the term
            # loop it compounded per matched word (1.2**4 for four matches),
            # turning a confidence weight into a term-count weight.
            if rec.get("confidence") == "high":
                score *= 1.2
            elif rec.get("confidence") == "low":
                score *= 0.8

            if score > 0:
                scored.append((score, rec))

        scored.sort(key=lambda x: x[0], reverse=True)
        # Strip internal _text field before returning
        results = []
        for _, rec in scored[:max_items]:
            clean = {k: v for k, v in rec.items() if k != "_text"}
            results.append(clean)
        return results

    def build_context_block(self, query: str) -> str:
        """Build a formatted context string to prepend to the Sage's prompt."""
        relevant = self.search_relevant(query)
        domains  = self.get_domains()

        parts: List[str] = []

        if domains:
            dom_lines = [
                f"  • {d['domain']}: {d['description']}"
                for d in domains[-10:]  # last 10 domain declarations
            ]
            parts.append("SAGE KNOWN DOMAINS:\n" + "\n".join(dom_lines))

        if relevant:
            fact_lines: List[str] = []
            for r in relevant:
                if r["record_type"] == "fact":
                    conf = r.get("confidence", "medium")
                    fact_lines.append(
                        f"  [{r.get('topic','?')}] {r.get('fact','')} "
                        f"(source: {r.get('source','unknown')}, confidence: {conf})"
                    )
                elif r["record_type"] == "correction":
                    fact_lines.append(
                        f"  [CORRECTION] For '{r.get('query','')}': "
                        f"{r.get('correction','')}"
                    )
            parts.append("SAGE RELEVANT KNOWLEDGE:\n" + "\n".join(fact_lines))

        if not parts:
            return ""

        return "\n\n".join(parts)

    # ── Write ────────────────────────────────────────────────────────────

    def add_fact(self, topic: str, fact: str, source: str = "user",
                 confidence: str = "high") -> None:
        _append_jsonl(self.facts_path, {
            "topic": topic.strip(),
            "fact":  fact.strip(),
            "source": source.strip(),
            "confidence": confidence,
            "ts": _now_iso(),
        })

    def add_correction(self, query: str, wrong_answer: str,
                       correction: str) -> None:
        _append_jsonl(self.corr_path, {
            "query":        query.strip(),
            "wrong_answer": wrong_answer.strip(),
            "correction":   correction.strip(),
            "ts":           _now_iso(),
        })

    def add_domain(self, domain: str, description: str) -> None:
        _append_jsonl(self.domains_path, {
            "domain":      domain.strip(),
            "description": description.strip(),
            "ts":          _now_iso(),
        })

    def log_gap(self, query: str, reason: str = "") -> None:
        _append_jsonl(self.gaps_path, {
            "query":  query.strip(),
            "reason": reason.strip(),
            "ts":     _now_iso(),
        })

    # ── Stats ─────────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, int]:
        return {
            "facts":       len(self.get_facts()),
            "corrections": len(self.get_corrections()),
            "domains":     len(self.get_domains()),
            "gaps":        len(self.get_gaps()),
        }


# ── Gap detection ────────────────────────────────────────────────────────────

# Phrases the model uses when it doesn't know something.
# If the Sage's response contains these, a gap is automatically logged.
_GAP_SIGNALS = [
    "i don't know",
    "i do not know",
    "i'm not sure",
    "i am not sure",
    "i cannot say",
    "i can't say",
    "unclear to me",
    "not certain",
    "no information",
    "no data",
    "outside my knowledge",
    "beyond my knowledge",
    "i lack",
    "don't have enough",
    "insufficient information",
    "cannot confirm",
    "i'm uncertain",
    "GAP:",                   # explicit gap declaration from the prompt
]

_CONFIDENCE_SIGNALS = [
    "GAP:", "UNCERTAIN:", "UNKNOWN:", "MISSING KNOWLEDGE:"
]


def detect_gap(response: str) -> Optional[str]:
    """
    Returns a gap reason string if the response signals a knowledge gap,
    or None if the response appears confident.
    """
    r_low = response.lower()
    for sig in _GAP_SIGNALS:
        if sig.lower() in r_low:
            return sig
    return None


# ── Sage personality ─────────────────────────────────────────────────────────

SAGE_SYSTEM_PROMPT = """\
You are the SAGE of the Council — a domain expert who learns over time.

Your role:
- Answer questions using your accumulated knowledge base (provided as context).
- When you know something with high confidence, state it clearly and directly.
- When you are uncertain or lack knowledge, say so EXPLICITLY using this format:
    GAP: <what you don't know and why it matters>
  This is not a failure — it is essential feedback that helps the council improve.

Rules:
- Always use the SAGE RELEVANT KNOWLEDGE block if it is present in your context.
  This knowledge has been verified and corrected by the user over time. Trust it.
- If the knowledge block contradicts your training, prefer the knowledge block.
- Never fabricate facts. If you don't know, say GAP: rather than guess.
- For conversational queries: answer in clear prose. For technical queries: be precise.
- After your answer, if relevant, note what additional data would make your answer better.

Format when you have a gap:
  [your best answer based on what you know]
  GAP: [specific thing you lack] — to improve this answer, provide: [what data would help]
"""


@dataclass
class SageAgent:
    """
    The Sage — wraps a PersonalityModel with vault knowledge injection
    and automatic gap detection/logging.
    """
    model:         Any           # PersonalityModel instance
    knowledge:     SageKnowledge
    on_gap:        Optional[Callable[[str, str], None]] = None  # callback(query, reason)

    def respond(self, query: str, *, extra_context: str = "",
                token_callback=None) -> str:
        """
        Run the Sage on a query. Injects vault knowledge, detects gaps,
        and logs them automatically.
        """
        kb_context = self.knowledge.build_context_block(query)

        combined_context = "\n\n".join(filter(None, [kb_context, extra_context]))

        response = self.model.respond(
            query,
            extra_context=combined_context,
            token_callback=token_callback,
        )

        # Auto-detect and log gaps
        gap_reason = detect_gap(response)
        if gap_reason:
            self.knowledge.log_gap(query, reason=gap_reason)
            if self.on_gap:
                self.on_gap(query, gap_reason)

        return response

    def teach(self, topic: str, fact: str, source: str = "user",
              confidence: str = "high") -> None:
        """Add a new fact to the Sage's knowledge base."""
        self.knowledge.add_fact(topic, fact, source, confidence)

    def correct(self, query: str, wrong_answer: str, correction: str) -> None:
        """Record a correction for a specific query."""
        self.knowledge.add_correction(query, wrong_answer, correction)

    def declare_domain(self, domain: str, description: str) -> None:
        """Declare a domain the Sage should be expert in."""
        self.knowledge.add_domain(domain, description)
