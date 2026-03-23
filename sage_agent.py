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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
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

        # Build document frequency for IDF
        df: Dict[str, int] = {}
        for rec in all_records:
            terms = set(re.findall(r"[a-zA-Z0-9]{3,}", rec["_text"].lower()))
            for t in terms:
                df[t] = df.get(t, 0) + 1

        scored: List[tuple] = []
        for rec in all_records:
            text_low = rec["_text"].lower()
            terms    = set(re.findall(r"[a-zA-Z0-9]{3,}", text_low))
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

                # Boost high-confidence facts
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


# ── GUI Panel ────────────────────────────────────────────────────────────────

try:
    import tkinter as tk
    from tkinter import ttk, messagebox
    _TK_OK = True
except ImportError:
    _TK_OK = False


if _TK_OK:

    class SageTuningPanel(ttk.Frame):
        """
        GUI panel embedded in the Agents tab.
        Lets the user teach, correct, and review the Sage's knowledge.
        Sections:
          - Stats bar (facts / corrections / domains / gaps)
          - Teach tab: add new facts by topic
          - Correct tab: fix a wrong answer
          - Domains tab: declare expertise areas
          - Gaps tab: review what the Sage doesn't know (import candidates)
          - Knowledge tab: browse all stored facts
        """

        def __init__(self, parent, *, sage_agent: "SageAgent",
                     refresh_cb: Optional[Callable] = None, **kw):
            super().__init__(parent, **kw)
            self.sage     = sage_agent
            self.refresh_cb = refresh_cb
            self._build()

        # ── Build ─────────────────────────────────────────────────────

        def _build(self):
            # Header
            hdr = ttk.Frame(self)
            hdr.pack(fill="x", padx=6, pady=(6, 2))
            ttk.Label(hdr, text="🧙 Sage Tuning", font=("", 11, "bold")).pack(side="left")
            self._stats_label = ttk.Label(hdr, text="", foreground="#89b4fa")
            self._stats_label.pack(side="right")
            self._refresh_stats()

            nb = ttk.Notebook(self)
            nb.pack(fill="both", expand=True, padx=6, pady=4)

            nb.add(self._build_teach_tab(nb),    text="📚 Teach")
            nb.add(self._build_correct_tab(nb),  text="✏️ Correct")
            nb.add(self._build_domains_tab(nb),  text="🗂 Domains")
            nb.add(self._build_gaps_tab(nb),     text="❓ Gaps")
            nb.add(self._build_knowledge_tab(nb),text="📖 Knowledge")

        def _refresh_stats(self):
            s = self.sage.knowledge.stats()
            self._stats_label.config(
                text=f"facts:{s['facts']}  corrections:{s['corrections']}  "
                     f"domains:{s['domains']}  gaps:{s['gaps']}"
            )

        # ── Teach tab ─────────────────────────────────────────────────

        def _build_teach_tab(self, parent):
            f = ttk.Frame(parent)

            ttk.Label(f, text="Topic:").grid(row=0, column=0, sticky="w", padx=4, pady=3)
            self._teach_topic = ttk.Entry(f, width=28)
            self._teach_topic.grid(row=0, column=1, sticky="ew", padx=4)

            ttk.Label(f, text="Fact:").grid(row=1, column=0, sticky="nw", padx=4, pady=3)
            self._teach_fact = tk.Text(f, height=4, width=48, wrap="word")
            self._teach_fact.grid(row=1, column=1, sticky="ew", padx=4, pady=3)

            ttk.Label(f, text="Source:").grid(row=2, column=0, sticky="w", padx=4)
            self._teach_source = ttk.Entry(f, width=28)
            self._teach_source.insert(0, "user")
            self._teach_source.grid(row=2, column=1, sticky="ew", padx=4)

            ttk.Label(f, text="Confidence:").grid(row=3, column=0, sticky="w", padx=4)
            self._teach_conf = ttk.Combobox(f, values=["high", "medium", "low"],
                                            state="readonly", width=12)
            self._teach_conf.set("high")
            self._teach_conf.grid(row=3, column=1, sticky="w", padx=4)

            ttk.Button(f, text="Add Fact", command=self._do_teach).grid(
                row=4, column=1, sticky="e", padx=4, pady=6)

            f.columnconfigure(1, weight=1)
            return f

        def _do_teach(self):
            topic  = self._teach_topic.get().strip()
            fact   = self._teach_fact.get("1.0", "end").strip()
            source = self._teach_source.get().strip() or "user"
            conf   = self._teach_conf.get()
            if not topic or not fact:
                messagebox.showwarning("Teach", "Topic and Fact are required.")
                return
            self.sage.teach(topic, fact, source, conf)
            self._teach_topic.delete(0, "end")
            self._teach_fact.delete("1.0", "end")
            self._refresh_stats()
            self._refresh_knowledge()
            messagebox.showinfo("Teach", f"Fact added under '{topic}'.")

        # ── Correct tab ───────────────────────────────────────────────

        def _build_correct_tab(self, parent):
            f = ttk.Frame(parent)

            ttk.Label(f, text="Query the Sage got wrong:").grid(
                row=0, column=0, columnspan=2, sticky="w", padx=4, pady=(6,2))
            self._corr_query = tk.Text(f, height=2, width=52, wrap="word")
            self._corr_query.grid(row=1, column=0, columnspan=2, sticky="ew", padx=4)

            ttk.Label(f, text="Wrong answer (optional):").grid(
                row=2, column=0, columnspan=2, sticky="w", padx=4, pady=(6,2))
            self._corr_wrong = tk.Text(f, height=2, width=52, wrap="word")
            self._corr_wrong.grid(row=3, column=0, columnspan=2, sticky="ew", padx=4)

            ttk.Label(f, text="Correct answer:").grid(
                row=4, column=0, columnspan=2, sticky="w", padx=4, pady=(6,2))
            self._corr_right = tk.Text(f, height=3, width=52, wrap="word")
            self._corr_right.grid(row=5, column=0, columnspan=2, sticky="ew", padx=4)

            ttk.Button(f, text="Save Correction", command=self._do_correct).grid(
                row=6, column=1, sticky="e", padx=4, pady=6)

            f.columnconfigure(0, weight=1)
            return f

        def _do_correct(self):
            query  = self._corr_query.get("1.0", "end").strip()
            wrong  = self._corr_wrong.get("1.0", "end").strip()
            right  = self._corr_right.get("1.0", "end").strip()
            if not query or not right:
                messagebox.showwarning("Correct", "Query and Correct answer are required.")
                return
            self.sage.correct(query, wrong, right)
            self._corr_query.delete("1.0", "end")
            self._corr_wrong.delete("1.0", "end")
            self._corr_right.delete("1.0", "end")
            self._refresh_stats()
            messagebox.showinfo("Correct", "Correction saved — Sage will use this next time.")

        # ── Domains tab ───────────────────────────────────────────────

        def _build_domains_tab(self, parent):
            f = ttk.Frame(parent)

            ttk.Label(f, text="Domain name:").grid(row=0, column=0, sticky="w", padx=4, pady=3)
            self._dom_name = ttk.Entry(f, width=30)
            self._dom_name.grid(row=0, column=1, sticky="ew", padx=4)

            ttk.Label(f, text="Description:").grid(row=1, column=0, sticky="nw", padx=4)
            self._dom_desc = tk.Text(f, height=3, width=46, wrap="word")
            self._dom_desc.grid(row=1, column=1, sticky="ew", padx=4, pady=3)

            ttk.Button(f, text="Add Domain", command=self._do_domain).grid(
                row=2, column=1, sticky="e", padx=4, pady=4)

            # Existing domains
            ttk.Separator(f, orient="horizontal").grid(
                row=3, column=0, columnspan=2, sticky="ew", pady=6)
            ttk.Label(f, text="Declared domains:").grid(
                row=4, column=0, columnspan=2, sticky="w", padx=4)
            self._dom_list = tk.Text(f, height=6, state="disabled",
                                     wrap="word", foreground="#cdd6f4")
            self._dom_list.grid(row=5, column=0, columnspan=2, sticky="ew", padx=4)
            self._refresh_domains()

            f.columnconfigure(1, weight=1)
            return f

        def _do_domain(self):
            name = self._dom_name.get().strip()
            desc = self._dom_desc.get("1.0", "end").strip()
            if not name:
                messagebox.showwarning("Domain", "Domain name is required.")
                return
            self.sage.declare_domain(name, desc)
            self._dom_name.delete(0, "end")
            self._dom_desc.delete("1.0", "end")
            self._refresh_stats()
            self._refresh_domains()

        def _refresh_domains(self):
            domains = self.sage.knowledge.get_domains()
            self._dom_list.config(state="normal")
            self._dom_list.delete("1.0", "end")
            for d in domains[-20:]:
                self._dom_list.insert("end",
                    f"[{d['ts'][:10]}] {d['domain']}: {d['description']}\n")
            self._dom_list.config(state="disabled")

        # ── Gaps tab ──────────────────────────────────────────────────

        def _build_gaps_tab(self, parent):
            f = ttk.Frame(parent)

            hdr = ttk.Frame(f)
            hdr.pack(fill="x", padx=4, pady=(4,2))
            ttk.Label(hdr, text="Queries the Sage flagged as unknown:").pack(side="left")
            ttk.Button(hdr, text="↺ Refresh", command=self._refresh_gaps).pack(side="right")
            ttk.Button(hdr, text="Clear Gaps", command=self._clear_gaps).pack(side="right", padx=4)

            self._gaps_box = tk.Text(f, height=12, state="disabled",
                                     wrap="word", foreground="#f38ba8")
            self._gaps_box.pack(fill="both", expand=True, padx=4, pady=4)
            self._refresh_gaps()

            ttk.Label(f, text="Use gaps to identify what data to add via Teach or Correct.",
                      foreground="#6c7086").pack(anchor="w", padx=4, pady=(0,4))
            return f

        def _refresh_gaps(self):
            gaps = self.sage.knowledge.get_gaps()
            self._gaps_box.config(state="normal")
            self._gaps_box.delete("1.0", "end")
            if not gaps:
                self._gaps_box.insert("end", "(no gaps logged yet)")
            else:
                for g in reversed(gaps[-40:]):
                    self._gaps_box.insert("end",
                        f"[{g['ts'][:16]}] {g['query']}\n"
                        + (f"  reason: {g['reason']}\n" if g.get('reason') else "")
                        + "\n")
            self._gaps_box.config(state="disabled")

        def _clear_gaps(self):
            if messagebox.askyesno("Clear Gaps", "Clear all logged gaps? This cannot be undone."):
                self.sage.knowledge.gaps_path.write_text("", encoding="utf-8")
                self._refresh_gaps()
                self._refresh_stats()

        # ── Knowledge tab ─────────────────────────────────────────────

        def _build_knowledge_tab(self, parent):
            f = ttk.Frame(parent)

            hdr = ttk.Frame(f)
            hdr.pack(fill="x", padx=4, pady=(4,2))
            ttk.Label(hdr, text="Search:").pack(side="left")
            self._kb_search = ttk.Entry(hdr, width=24)
            self._kb_search.pack(side="left", padx=4)
            self._kb_search.bind("<Return>", lambda _: self._refresh_knowledge())
            ttk.Button(hdr, text="Search", command=self._refresh_knowledge).pack(side="left")
            ttk.Button(hdr, text="Show All", command=lambda: self._refresh_knowledge(all=True)
                       ).pack(side="left", padx=4)

            self._kb_box = tk.Text(f, height=14, state="disabled",
                                   wrap="word", foreground="#cdd6f4")
            self._kb_box.pack(fill="both", expand=True, padx=4, pady=4)
            self._refresh_knowledge()
            return f

        def _refresh_knowledge(self, all: bool = False):
            query = self._kb_search.get().strip() if hasattr(self, "_kb_search") else ""
            if all or not query:
                records = self.sage.knowledge.get_facts()[-50:]
            else:
                records = self.sage.knowledge.search_relevant(query, max_items=30)
                records = [r for r in records if r.get("record_type") == "fact"]

            self._kb_box.config(state="normal")
            self._kb_box.delete("1.0", "end")
            if not records:
                self._kb_box.insert("end", "(no facts yet — use the Teach tab to add some)")
            else:
                for r in records:
                    conf_color = {"high": "✓", "medium": "~", "low": "?"}.get(
                        r.get("confidence", "medium"), "~")
                    self._kb_box.insert("end",
                        f"{conf_color} [{r.get('topic','?')}] {r.get('fact','')}\n"
                        f"   source: {r.get('source','?')}  ts: {r.get('ts','?')[:10]}\n\n"
                    )
            self._kb_box.config(state="disabled")
