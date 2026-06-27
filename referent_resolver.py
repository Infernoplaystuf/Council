"""referent_resolver.py — decide what the user's ambiguous nouns refer to.

When the user types something like "what does this app do?" the word
"app" could refer to:
  • Anvil itself — the application the user is talking to
  • a different app the user has been discussing in this conversation
  • a generic noun in a vault document

Pre-LLM keyword search has no way to tell those apart. This module runs
a small, fast, rule-based pass over the user's text (and optionally a
few recent prior turns + the vault vocab) and returns:

  • inject_identity: should the [APP IDENTITY] block be added?
  • drop_terms:      which tokens should NOT participate in vault
                     search/expansion (they don't refer to vault
                     content)
  • referent:        "self" | "domain" | "ambiguous" | "none"

When the signal is weak, we choose ``ambiguous``: inject the identity
block AND search the vault normally, and let the Writer reconcile from
the rest of the question. Failing open is far less harmful than
silently choosing the wrong referent — which is exactly the bug this
module exists to fix.

This deliberately does NOT call an LLM. The whole point is to disambiguate
BEFORE retrieval, so an unreliable small model can't pollute the
context with hallucinated expansions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Set


# ----------------------------------------------------------------------
# Outputs
# ----------------------------------------------------------------------

@dataclass
class RefResolution:
    """Result of resolving a query's referents.

    ``referent`` is the categorical decision; ``inject_identity`` and
    ``drop_terms`` are the operational outputs callers act on.
    ``signals`` is a list of human-readable strings (e.g.
    ``"strong-dem:'this app'"``) for debug/log surfaces.
    """
    referent: str = "none"          # "self" | "domain" | "ambiguous" | "none"
    inject_identity: bool = False
    drop_terms: Set[str] = field(default_factory=set)
    signals: List[str] = field(default_factory=list)
    self_score: float = 0.0
    domain_score: float = 0.0


# ----------------------------------------------------------------------
# Phrase tables
# ----------------------------------------------------------------------

# Strong self phrases — when present, almost certainly the user is
# asking about Anvil itself, not vault content. Score +3 each.
_SELF_PHRASES = (
    "what can you do",
    "what do you do",
    "who are you",
    "what are you",
    "what is your purpose",
    "what's your purpose",
    "describe yourself",
    "introduce yourself",
    "tell me about yourself",
    "tell me what you do",
    "what are your capabilit",   # capability / capabilities
    "your capabilit",
    "your features",
    "your abilit",
    "how do you work",
    "how does this work",
    "how does it work",          # weaker but still mostly self
)

# Direct app-name references — when "Anvil" / "Council" appears as a
# standalone word (boundary-matched), it's the app. Score +3.
_APP_NAMES = ("anvil", "council")

# Nouns that could refer to the app itself OR to a vault entity. The
# referent depends on surrounding demonstratives + context.
_AMBIGUOUS_NOUNS = (
    "app", "apps", "tool", "tools", "system", "systems",
    "program", "programs", "software",
    "thing", "site", "page",
)

# Demonstratives that, paired with an ambiguous noun, strongly imply
# self-reference. Score +2 per matched pair.
_STRONG_DEMONSTRATIVES = ("this", "your", "yours")

# Weak demonstratives — "the app" might be self or might be a
# previously-mentioned domain entity. Score +0.5 per pair, then let
# domain signals (anaphor, vault evidence) override.
_WEAK_DEMONSTRATIVES = ("the", "that")

# Search-shape verbs — when the user is explicitly searching the
# vault, the ambiguous noun belongs to the domain, not self. Score +3.
_SEARCH_VERBS = (
    "find docs about", "find files about", "search for",
    "show me files", "show me documents",
    "look for files", "look up", "look for the",
    "files about", "documents about", "docs about",
    "find anything about", "find references to",
)

# Generative framing — "write a function", "build a scene" — the user
# is asking Anvil to PRODUCE something, not to describe itself. Score
# -1 per match against the self bucket.
_GENERATIVE_FRAMING = (
    "write a", "write me", "generate", "create a", "make a",
    "build a", "give me a", "draft a", "implement a",
)

# Compiled regex for proper-noun antecedent detection in conversation
# history. We look for capitalised words 3+ chars long that aren't
# Anvil/Council itself.
_PROPER_NOUN_RE = re.compile(r"\b([A-Z][a-zA-Z]{2,})\b")


# ----------------------------------------------------------------------
# Main entry point
# ----------------------------------------------------------------------

def resolve_referent(
    user_text: str,
    recent_user_messages: Optional[List[str]] = None,
    vault_vocab_has: Optional[Callable[[str], bool]] = None,
) -> RefResolution:
    """Decide whether the query refers to THIS app, a domain entity, or both.

    Args
    ----
    user_text :
        The current user message (raw).
    recent_user_messages :
        Up to a few recent prior user turns (order doesn't matter — we
        just scan for proper-noun antecedents). Pass ``None`` if not
        tracking history; the rest of the resolver still works.
    vault_vocab_has :
        Optional predicate. When provided, ``vault_vocab_has(token)``
        returns True if the vault index's vocabulary contains that
        token — a real signal that the noun has a domain referent. Pass
        ``None`` if the vocab isn't conveniently available.

    Returns
    -------
    RefResolution. Callers care about ``inject_identity`` and
    ``drop_terms``; ``signals`` is for debug.
    """
    res = RefResolution()
    if not user_text:
        return res

    low = user_text.lower()

    # ---- Strong self phrases (+3 each) -------------------------------
    for p in _SELF_PHRASES:
        if p in low:
            res.self_score += 3.0
            res.signals.append(f"self-phrase:{p!r}")

    # ---- Self-named references (+3 each) -----------------------------
    for name in _APP_NAMES:
        if re.search(rf"\b{name}\b", low):
            res.self_score += 3.0
            res.signals.append(f"named-self:{name}")

    # ---- Demonstrative + ambiguous noun ------------------------------
    # Strong: "this app", "your tool" → +2
    # Weak:   "the app", "that program" → +0.5 (often domain-coupled)
    for noun in _AMBIGUOUS_NOUNS:
        for dem in _STRONG_DEMONSTRATIVES:
            if f"{dem} {noun}" in low:
                res.self_score += 2.0
                res.signals.append(f"strong-dem:{dem!r}+{noun!r}")
        for dem in _WEAK_DEMONSTRATIVES:
            if f"{dem} {noun}" in low:
                res.self_score += 1.0
                res.signals.append(f"weak-dem:{dem!r}+{noun!r}")

    # ---- Search-shape verbs (domain +3) ------------------------------
    for verb in _SEARCH_VERBS:
        if verb in low:
            res.domain_score += 3.0
            res.signals.append(f"search-verb:{verb!r}")

    # ---- Generative framing (self -1) --------------------------------
    # "write a function that ..." → the user wants code, not a
    # self-description; suppress the self bias.
    for g in _GENERATIVE_FRAMING:
        if g in low:
            res.self_score -= 1.0
            res.signals.append(f"generative-framing:{g!r}")

    # ---- Anaphor: "the X" near a recent proper noun (+2) -------------
    # If the prior turn named a specific entity (e.g. "Spotify") and
    # the current query uses a weak demonstrative + an ambiguous noun,
    # the referent is probably that named entity, not Anvil.
    if recent_user_messages:
        recent_joined = " ".join(recent_user_messages[-3:])
        proper_nouns = set(_PROPER_NOUN_RE.findall(recent_joined))
        proper_nouns = {n for n in proper_nouns if n.lower() not in _APP_NAMES}
        if proper_nouns:
            has_weak_anaphor = any(
                f"{dem} {noun}" in low
                for dem in ("the", "that", "it", "this")
                for noun in _AMBIGUOUS_NOUNS
            )
            if has_weak_anaphor:
                res.domain_score += 2.0
                res.signals.append(
                    f"anaphor:{sorted(proper_nouns)[:3]}"
                )

    # ---- Vault-vocab signal (domain +1 per known ambiguous noun) -----
    # If the vault really has docs about "the app", the user might
    # genuinely be searching them. We can only check when the caller
    # gave us a predicate.
    if vault_vocab_has is not None:
        for noun in _AMBIGUOUS_NOUNS:
            if noun in low:
                try:
                    if vault_vocab_has(noun):
                        res.domain_score += 1.0
                        res.signals.append(f"vault-has:{noun}")
                except Exception:
                    # Predicate threw — ignore; don't let a misbehaving
                    # vocab source poison the resolver.
                    pass

    # ---- Decide ------------------------------------------------------
    # Self wins only when above domain by a clear margin. Otherwise
    # ambiguous, which means "do both" downstream. A single "this app"
    # alone (no other self phrase) scores 2.0 — that's enough to be
    # confident; the domain side has to push back to override.
    if res.self_score >= 2.0 and res.self_score > res.domain_score + 1.0:
        res.referent = "self"
    elif res.domain_score >= 2.0 and res.domain_score > res.self_score:
        res.referent = "domain"
    elif res.self_score > 0 or res.domain_score > 0:
        res.referent = "ambiguous"
    else:
        res.referent = "none"

    # ---- Operational outputs ----------------------------------------
    # Inject identity whenever there's any self signal (self OR
    # ambiguous). Cheaper to inject + correct than to omit + wrong.
    res.inject_identity = res.referent in ("self", "ambiguous")

    # Drop ambiguous tokens from vault search ONLY when clearly self.
    # When ambiguous, vault search runs normally so a genuine domain
    # entity still surfaces.
    if res.referent == "self":
        for noun in _AMBIGUOUS_NOUNS:
            if noun in low:
                res.drop_terms.add(noun)
        for name in _APP_NAMES:
            if re.search(rf"\b{name}\b", low):
                res.drop_terms.add(name)

    return res
