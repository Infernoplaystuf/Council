"""
Provenance memory — short-lived in-session record of what was shown to
the model and what it said in response.

Used to answer "where did this value come from?" after a model reply.
Small 8B models (Granite, Phi, etc.) frequently hallucinate values when
the answer isn't actually in the provided data; this layer lets the
user point at any number / string in the model's reply and either find
its source row in an injected file or get a "not present — likely
hallucinated" verdict.

Storage is purely in-memory. We keep the last N turns to bound the
footprint. Nothing here writes to disk — conversation_logger.py
handles the user-visible debug log, and that one is explicitly
forbidden from the model (see PROTECTED_SUBDIRS).
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Tuple


@dataclass
class InjectedBlock:
    """One file's worth of content that was prepended to a model prompt."""
    file_name: str
    file_path: str          # str so we don't drag pathlib into every consumer
    block: str              # the full [FILE: ...] body that was sent


@dataclass
class TurnRecord:
    turn_id: int
    timestamp: str          # ISO-like local time
    user_text: str          # original user message
    augmented_text: str     # full prompt body the model saw
    injected_files: List[InjectedBlock] = field(default_factory=list)
    model_responses: List[Tuple[str, str]] = field(default_factory=list)
    # ^ [(speaker, text), ...] — usually [("Writer", "...")] but multiple
    # speakers can post in one turn (Council observations, Workflow lines).


class ProvenanceTracker:
    """Bounded deque of turn records, with value-search helpers.

    Thread-safe for single-threaded UI use (the GUI never modifies in
    parallel — workers only call observation methods, not record_turn).
    """

    def __init__(self, max_turns: int = 20):
        self.max_turns = max_turns
        self._turns: Deque[TurnRecord] = deque(maxlen=max_turns)
        self._next_id = 1

    # -- recording ----------------------------------------------------

    def record_turn(
        self,
        user_text: str,
        augmented_text: str,
        injected_files: List[InjectedBlock],
    ) -> TurnRecord:
        turn = TurnRecord(
            turn_id=self._next_id,
            timestamp=datetime.now().strftime("%H:%M:%S"),
            user_text=user_text,
            augmented_text=augmented_text,
            injected_files=list(injected_files),
        )
        self._next_id += 1
        self._turns.append(turn)
        return turn

    def add_response(self, speaker: str, text: str) -> None:
        """Attach a model/observer response to the most recent turn."""
        if not self._turns:
            return
        self._turns[-1].model_responses.append((str(speaker), str(text)))

    # -- inspection ---------------------------------------------------

    def last_turn(self) -> Optional[TurnRecord]:
        return self._turns[-1] if self._turns else None

    def turns(self) -> List[TurnRecord]:
        return list(self._turns)

    # -- value lookup -------------------------------------------------

    def search_value(
        self,
        value: str,
        *,
        max_turns_back: int = 5,
        max_hits: int = 8,
    ) -> List[Dict[str, Any]]:
        """Find where `value` appears in any recently-injected file.

        Performs two passes:
          1. exact substring match (case-insensitive)
          2. numeric-normalized match — strip commas, currency, %,
             trailing zeros; compare as float for any value that looks
             like a number

        Returns a list of hits, each with file_name, file_path,
        turn_id, line_index (0-based within the [FILE:] block), and a
        short context_snippet around the match.
        """
        v = (value or "").strip()
        if not v:
            return []
        hits: List[Dict[str, Any]] = []
        v_lower = v.lower()
        v_numeric = _normalize_numeric(v)

        recent = list(self._turns)[-max_turns_back:]
        for turn in reversed(recent):  # newest first
            for ib in turn.injected_files:
                for i, line in enumerate(ib.block.split("\n")):
                    line_lower = line.lower()
                    matched = False
                    # Exact substring
                    if v_lower in line_lower:
                        matched = True
                    elif v_numeric is not None:
                        # Numeric pass — find any number-like token on the line
                        for tok in _NUM_TOKEN.findall(line):
                            n = _normalize_numeric(tok)
                            if n is not None and abs(n - v_numeric) < 1e-9:
                                matched = True
                                break
                    if matched:
                        hits.append({
                            "turn_id":         turn.turn_id,
                            "file_name":       ib.file_name,
                            "file_path":       ib.file_path,
                            "line_index":      i,
                            "context_snippet": _excerpt(ib.block.split("\n"), i, n=2),
                            "match_kind":      "exact" if v_lower in line_lower else "numeric",
                        })
                        if len(hits) >= max_hits:
                            return hits
        return hits

    # -- response auto-verification ----------------------------------

    def verify_response(
        self,
        text: str,
        *,
        turn_id: Optional[int] = None,
        max_check: int = 12,
    ) -> Dict[str, Any]:
        """Extract candidate numeric values from `text` (or the most
        recent model response if not provided) and check each against
        the matching turn's injected files.

        Returns a dict with `checked`, `found`, `missing` lists.
        """
        if not text:
            text = ""
            turn = self._turns[-1] if self._turns else None
            if turn and turn.model_responses:
                text = "\n".join(r for _, r in turn.model_responses)
        target_turn = None
        if turn_id is not None:
            for t in self._turns:
                if t.turn_id == turn_id:
                    target_turn = t
                    break
        if target_turn is None:
            target_turn = self._turns[-1] if self._turns else None

        # Extract numeric tokens longer than 1 digit (skip 0/1/2 type noise)
        candidates: List[str] = []
        for m in _NUM_TOKEN.finditer(text or ""):
            tok = m.group(0)
            if len(re.sub(r"\D", "", tok)) >= 2:
                candidates.append(tok)
                if len(candidates) >= max_check:
                    break

        found: List[Dict[str, Any]] = []
        missing: List[str] = []
        for tok in candidates:
            hits = self.search_value(tok, max_turns_back=1, max_hits=1) if target_turn is None \
                   else self._search_in_turn(tok, target_turn)
            if hits:
                found.append({"value": tok, "where": hits[0]})
            else:
                missing.append(tok)
        return {
            "turn_id": target_turn.turn_id if target_turn else None,
            "checked": candidates,
            "found":   found,
            "missing": missing,
        }

    def _search_in_turn(self, value: str, turn: TurnRecord) -> List[Dict[str, Any]]:
        v_lower = value.lower()
        v_numeric = _normalize_numeric(value)
        out: List[Dict[str, Any]] = []
        for ib in turn.injected_files:
            for i, line in enumerate(ib.block.split("\n")):
                line_lower = line.lower()
                matched = False
                if v_lower in line_lower:
                    matched = True
                elif v_numeric is not None:
                    for tok in _NUM_TOKEN.findall(line):
                        n = _normalize_numeric(tok)
                        if n is not None and abs(n - v_numeric) < 1e-9:
                            matched = True
                            break
                if matched:
                    out.append({
                        "turn_id": turn.turn_id,
                        "file_name": ib.file_name,
                        "file_path": ib.file_path,
                        "line_index": i,
                        "context_snippet": _excerpt(ib.block.split("\n"), i, n=2),
                    })
                    if len(out) >= 1:
                        return out
        return out


# ============================================================
# Helpers
# ============================================================

_NUM_TOKEN = re.compile(
    # Either: digits with at least one thousands separator (1,234 or 1 234),
    # optional decimal, optional %.  Or: plain digits with optional decimal.
    r"-?\$?(?:\d{1,3}(?:[,\s]\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)%?"
)


def _normalize_numeric(s: str) -> Optional[float]:
    """Convert '$1,234.50' or '12.5%' or '1500' to 1234.5 / 12.5 / 1500.0.
    Returns None for anything that doesn't parse cleanly as a number."""
    if s is None:
        return None
    t = str(s).strip()
    if not t:
        return None
    # strip currency, percent, thousands separators, whitespace
    cleaned = re.sub(r"[\s,$£€¥]", "", t).rstrip("%")
    try:
        return float(cleaned)
    except (TypeError, ValueError):
        return None


def _excerpt(lines: List[str], idx: int, n: int = 2) -> str:
    lo = max(0, idx - n)
    hi = min(len(lines), idx + n + 1)
    out: List[str] = []
    for k in range(lo, hi):
        marker = ">>" if k == idx else "  "
        out.append(f"{marker} {lines[k]}")
    return "\n".join(out)
