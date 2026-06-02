"""
vault_search_query.py — query DSL for ``vault_index.search``.

Backwards-compatible with the legacy "free-text" input — a query
that contains nothing special parses to a flat list of plain terms,
which is what ``search`` already handled.

Special syntax (all opt-in, all composable):

  "Q4 revenue"        phrase — must appear contiguously
  /\\bemail.*@/       regex — pattern match against keywords +
                       headers + sample text
  AND                 logical AND (default between terms)
  OR                  logical OR
  NOT                 negation; binds tightest
  ( ... )             grouping
  size:>10mb          filter — file byte size comparison
  size:<2kb
  mtime:<7d           filter — modified within last N days/hours
  mtime:>30d
  ext:csv             filter — extension restrictor

Examples
--------
  revenue                              → plain terms (legacy)
  "Q4 revenue" AND customer            → phrase + token
  /\\bemail.*@/ NOT test               → regex with negation
  revenue size:>1mb mtime:<7d ext:csv  → filtered scope
  (customer OR client) AND NOT test    → grouped boolean

Output
------
``parse_query(s)`` returns a ``Query`` dataclass:

    Query(
        root:    Node | None        # boolean AST over Term + Phrase + Regex
        filters: List[Filter]       # gates applied before scoring
        raw_terms: List[str]        # legacy fallback (flat plain terms)
        is_legacy: bool             # True when no special syntax was seen
    )

``search`` walks the tree to gate candidate files and then scores them
with its existing TF-IDF over the leaf Term nodes; phrase + regex
contribute a fixed bonus per match.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple


# ============================================================
# AST nodes
# ============================================================

@dataclass
class Term:
    """Plain token leaf."""
    text: str


@dataclass
class Phrase:
    """Quoted exact phrase leaf."""
    text: str


@dataclass
class Regex:
    """Compiled regex leaf. ``pattern`` is the source string for diagnostics."""
    pattern: str
    compiled: Any = None        # re.Pattern, populated in parse_query


@dataclass
class Node:
    """Boolean combinator. ``op`` ∈ {'AND', 'OR', 'NOT'}; NOT has
    exactly one child, AND / OR have two or more."""
    op: str
    children: List[Any] = field(default_factory=list)


@dataclass
class Filter:
    """A gating filter. Applied per candidate file before scoring."""
    kind: str       # "size" | "mtime" | "ext"
    op:   str       # "<" | ">" | "<=" | ">=" | "==" | "ext-in"
    value: Any      # int (bytes for size, epoch-seconds for mtime),
                    # tuple/set of strings for ext-in


@dataclass
class Query:
    root:      Any = None
    filters:   List[Filter] = field(default_factory=list)
    raw_terms: List[str] = field(default_factory=list)
    is_legacy: bool = True


# ============================================================
# Tokenisation
# ============================================================

# Token kinds: WORD, PHRASE, REGEX, LPAREN, RPAREN, AND, OR, NOT,
# FILTER, EOF. The lexer scans left-to-right; ambiguities resolve
# in this order (the longest valid prefix wins).

_FILTER_RE = re.compile(
    r"(?P<kind>size|mtime|ext)\s*:\s*"
    r"(?P<op><=|>=|<|>|=|==)?\s*"
    r"(?P<val>[A-Za-z0-9.,_\-]+)",
    re.IGNORECASE,
)

# Plain word: letters/digits/underscores/hyphens/dots; no whitespace.
_WORD_RE = re.compile(r"[A-Za-z0-9_\-./]+")


def _next_token(s: str, pos: int) -> Tuple[str, str, int]:
    """Lex one token starting at ``pos``. Returns (kind, value, new_pos).
    ``kind`` ∈ {'WORD', 'PHRASE', 'REGEX', 'LPAREN', 'RPAREN', 'AND',
    'OR', 'NOT', 'FILTER', 'EOF'}.
    """
    n = len(s)
    while pos < n and s[pos].isspace():
        pos += 1
    if pos >= n:
        return ("EOF", "", pos)
    c = s[pos]
    # Grouping
    if c == "(":
        return ("LPAREN", "(", pos + 1)
    if c == ")":
        return ("RPAREN", ")", pos + 1)
    # Phrase — "..."
    if c == '"':
        end = s.find('"', pos + 1)
        if end == -1:
            # Unclosed phrase — treat the rest of the input as the phrase
            return ("PHRASE", s[pos + 1:], n)
        return ("PHRASE", s[pos + 1:end], end + 1)
    # Regex — /.../
    if c == "/":
        end = pos + 1
        while end < n:
            if s[end] == "\\" and end + 1 < n:
                end += 2
                continue
            if s[end] == "/":
                break
            end += 1
        if end >= n or s[end] != "/":
            # Treat the whole thing as a word (not a closed regex)
            m = _WORD_RE.match(s, pos)
            if m:
                return ("WORD", m.group(0), m.end())
            return ("WORD", c, pos + 1)
        return ("REGEX", s[pos + 1:end], end + 1)
    # Filter (size: / mtime: / ext:) — looked at BEFORE word so the
    # colon isn't consumed as part of an identifier
    m_f = _FILTER_RE.match(s, pos)
    if m_f and (pos == 0 or not s[pos - 1].isalnum()):
        return ("FILTER", m_f.group(0), m_f.end())
    # Word — also catches AND / OR / NOT (recognised below)
    m_w = _WORD_RE.match(s, pos)
    if m_w:
        word = m_w.group(0)
        up = word.upper()
        if up in ("AND", "OR", "NOT"):
            return (up, up, m_w.end())
        return ("WORD", word, m_w.end())
    # Unknown — skip
    return ("WORD", c, pos + 1)


# ============================================================
# Filter parser
# ============================================================

_SIZE_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*([kmgKMG]?[bB]?)$")
_MTIME_RE = re.compile(r"^([0-9]+)\s*([hdwmyHDWMY])$")

_SIZE_MULTIPLIERS = {"":1, "b":1, "k":1024, "kb":1024,
                     "m":1024**2, "mb":1024**2,
                     "g":1024**3, "gb":1024**3}

# Per-unit seconds
_MTIME_MULTIPLIERS = {"h": 3600, "d": 86400, "w": 604800,
                      "m": 2592000, "y": 31536000}


def _parse_size(raw: str) -> Optional[int]:
    m = _SIZE_RE.match(raw.strip())
    if not m:
        return None
    try:
        val = float(m.group(1))
    except ValueError:
        return None
    unit = m.group(2).lower()
    mul = _SIZE_MULTIPLIERS.get(unit)
    if mul is None:
        return None
    return int(val * mul)


def _parse_mtime_delta(raw: str) -> Optional[float]:
    """Parse ``Nu`` (N + unit) → seconds offset from now."""
    m = _MTIME_RE.match(raw.strip())
    if not m:
        return None
    try:
        n = int(m.group(1))
    except ValueError:
        return None
    unit = m.group(2).lower()
    mul = _MTIME_MULTIPLIERS.get(unit)
    if mul is None:
        return None
    return float(n * mul)


def _parse_filter(token_value: str) -> Optional[Filter]:
    m = _FILTER_RE.match(token_value)
    if not m:
        return None
    kind = m.group("kind").lower()
    # Normalise the op: missing op defaults to "==", bare "=" becomes "==".
    # The previous one-liner ran `(... or "==").replace("=", "==")` which
    # mangled the default into "====" (each "=" doubled), and the downstream
    # _cmp then fell through to `return True` — so `size:5mb` (no operator)
    # silently no-op'd as a filter. Plain if/else makes the intent obvious.
    op_raw = m.group("op")
    if not op_raw or op_raw == "=":
        op = "=="
    else:
        op = op_raw
    val_raw = m.group("val")
    if kind == "size":
        size = _parse_size(val_raw)
        if size is None:
            return None
        return Filter(kind="size", op=op, value=size)
    if kind == "mtime":
        delta = _parse_mtime_delta(val_raw)
        if delta is None:
            return None
        # store as epoch-seconds cutoff so search just compares stat().st_mtime
        return Filter(kind="mtime", op=op, value=time.time() - delta)
    if kind == "ext":
        # comma-separated list, normalised to lowercase ".csv" form
        parts = [p.strip().lower() for p in val_raw.split(",") if p.strip()]
        parts = [p if p.startswith(".") else "." + p for p in parts]
        return Filter(kind="ext", op="ext-in", value=tuple(parts))
    return None


# ============================================================
# Recursive-descent parser
# ============================================================

class _Parser:
    """Parses the lexer stream into a (Node, filters) pair.

    Grammar (precedence low → high):

        or_expr   := and_expr (OR and_expr)*
        and_expr  := unary (AND unary | unary)*    # juxtaposition = AND
        unary     := NOT unary | primary
        primary   := LPAREN or_expr RPAREN | leaf
        leaf      := WORD | PHRASE | REGEX

    Filters are collected as a side-band list — they don't participate
    in the boolean tree because they're per-file gates, not term
    membership tests.
    """

    def __init__(self, src: str):
        self.src = src
        self.pos = 0
        self.filters: List[Filter] = []
        self._tok = self._scan()

    def _scan(self) -> Tuple[str, str]:
        kind, val, self.pos = _next_token(self.src, self.pos)
        # Capture filters before the parser proper ever sees them so
        # they don't break the boolean grammar.
        while kind == "FILTER":
            f = _parse_filter(val)
            if f is not None:
                self.filters.append(f)
            kind, val, self.pos = _next_token(self.src, self.pos)
        return (kind, val)

    def _eat(self) -> Tuple[str, str]:
        tok = self._tok
        self._tok = self._scan()
        return tok

    def parse(self) -> Any:
        if self._tok[0] == "EOF":
            return None
        node = self._or_expr()
        return node

    def _or_expr(self) -> Any:
        left = self._and_expr()
        while self._tok[0] == "OR":
            self._eat()
            right = self._and_expr()
            if right is None:
                break
            if isinstance(left, Node) and left.op == "OR":
                left.children.append(right)
            else:
                left = Node(op="OR", children=[left, right])
        return left

    def _and_expr(self) -> Any:
        left = self._unary()
        while True:
            kind = self._tok[0]
            if kind == "AND":
                self._eat()
            elif kind in ("WORD", "PHRASE", "REGEX", "LPAREN", "NOT"):
                # juxtaposition implies AND
                pass
            else:
                break
            right = self._unary()
            if right is None:
                break
            if isinstance(left, Node) and left.op == "AND":
                left.children.append(right)
            else:
                left = Node(op="AND", children=[left, right])
        return left

    def _unary(self) -> Any:
        if self._tok[0] == "NOT":
            self._eat()
            child = self._unary()
            if child is None:
                return None
            return Node(op="NOT", children=[child])
        return self._primary()

    def _primary(self) -> Any:
        kind, val = self._tok
        if kind == "LPAREN":
            self._eat()
            inner = self._or_expr()
            if self._tok[0] == "RPAREN":
                self._eat()
            return inner
        if kind == "WORD":
            self._eat()
            return Term(text=val.lower())
        if kind == "PHRASE":
            self._eat()
            return Phrase(text=val.lower())
        if kind == "REGEX":
            self._eat()
            try:
                compiled = re.compile(val, re.IGNORECASE)
            except re.error:
                compiled = None
            return Regex(pattern=val, compiled=compiled)
        # Stray AND/OR/RPAREN at primary position — silently drop
        if kind in ("AND", "OR", "RPAREN", "EOF"):
            return None
        self._eat()
        return None


# ============================================================
# Public entry point
# ============================================================

# A query is "legacy" if it has no special syntax — purely plain words
# that the original search() already handled. We detect this and let
# the caller short-circuit to its existing fast path.
_SPECIAL_CHARS = set('"/()')
_BOOL_KEYWORDS = {"AND", "OR", "NOT"}


def _looks_legacy(s: str) -> bool:
    if not s:
        return True
    if any(c in s for c in _SPECIAL_CHARS):
        return False
    if _FILTER_RE.search(s):
        return False
    upper_tokens = {t for t in re.findall(r"\b[A-Z]{2,}\b", s)}
    if upper_tokens & _BOOL_KEYWORDS:
        return False
    return True


def parse_query(query: str) -> Query:
    """Parse a search query into a structured ``Query`` object.

    Legacy plain-text input round-trips into ``raw_terms`` with no
    AST. Anything containing phrase quotes, regex slashes, boolean
    keywords, parens, or ``size:`` / ``mtime:`` / ``ext:`` triggers
    the full grammar.
    """
    q = (query or "").strip()
    if not q:
        return Query()
    if _looks_legacy(q):
        # Plain text — split into words, drop empties
        terms = [t.lower() for t in re.split(r"\s+", q) if t]
        return Query(raw_terms=terms, is_legacy=True)
    p = _Parser(q)
    root = p.parse()
    return Query(
        root=root,
        filters=p.filters,
        raw_terms=[],
        is_legacy=False,
    )


# ============================================================
# Evaluation helpers — used by vault_index.search
# ============================================================

def filter_passes(filt: Filter, *, size: Optional[int],
                   mtime: Optional[float], ext: Optional[str]) -> bool:
    """Apply ``filt`` to a candidate file's stat info. Missing data
    counts as a pass — we don't want one unavailable stat to silently
    nuke whole result sets.
    """
    if filt.kind == "size":
        if size is None:
            return True
        return _cmp(size, filt.op, filt.value)
    if filt.kind == "mtime":
        if mtime is None:
            return True
        # filt.value is the epoch-seconds cutoff (now - delta)
        # mtime:<7d means "within last 7 days" → file's mtime > cutoff
        if filt.op in ("<", "<="):
            return mtime >= filt.value
        if filt.op in (">", ">="):
            return mtime <= filt.value
        return abs(mtime - filt.value) < 86400
    if filt.kind == "ext":
        if ext is None:
            return True
        return ext.lower() in filt.value
    return True


def _cmp(a: float, op: str, b: float) -> bool:
    if op == "<":  return a < b
    if op == "<=": return a <= b
    if op == ">":  return a > b
    if op == ">=": return a >= b
    if op == "==": return a == b
    return True


def evaluate_node(node: Any, haystack_lc: str, tokens_lc: set) -> bool:
    """Boolean evaluation of an AST against one candidate's bag of
    tokens (``tokens_lc``) and its concatenated text blob (``haystack_lc``).

    Returns True if the candidate matches the boolean expression.
    Pure logic — no scoring. Score-side TF-IDF still runs on the
    Term leaves in the caller.
    """
    if node is None:
        return True
    if isinstance(node, Term):
        return node.text in tokens_lc or node.text in haystack_lc
    if isinstance(node, Phrase):
        return node.text in haystack_lc
    if isinstance(node, Regex):
        if node.compiled is None:
            return False
        return bool(node.compiled.search(haystack_lc))
    if isinstance(node, Node):
        if node.op == "NOT":
            return not evaluate_node(node.children[0], haystack_lc, tokens_lc)
        if node.op == "AND":
            return all(evaluate_node(c, haystack_lc, tokens_lc)
                       for c in node.children)
        if node.op == "OR":
            return any(evaluate_node(c, haystack_lc, tokens_lc)
                       for c in node.children)
    return False


def collect_terms(node: Any) -> List[str]:
    """Walk the AST and return every plain ``Term.text`` in it.

    The caller uses these as input to the legacy TF-IDF scorer so a
    candidate that passes the boolean gate still ranks by how well
    it matches the actual content terms (not phrases or regex —
    those contribute a fixed bonus instead).
    """
    if node is None:
        return []
    if isinstance(node, Term):
        return [node.text]
    if isinstance(node, (Phrase, Regex)):
        return []
    if isinstance(node, Node):
        if node.op == "NOT":
            # Negated terms shouldn't reward matches; skip them.
            return []
        out: List[str] = []
        for c in node.children:
            out.extend(collect_terms(c))
        return out
    return []


def collect_phrase_and_regex(node: Any) -> Tuple[List[str], List[Any]]:
    """Walk the AST and pull out every Phrase string and every
    compiled Regex. Used by the scorer for per-match bonuses."""
    if node is None:
        return ([], [])
    if isinstance(node, Phrase):
        return ([node.text], [])
    if isinstance(node, Regex):
        return ([], [node.compiled] if node.compiled is not None else [])
    if isinstance(node, Node):
        if node.op == "NOT":
            return ([], [])
        phrases: List[str] = []
        regexes: List[Any] = []
        for c in node.children:
            p, r = collect_phrase_and_regex(c)
            phrases.extend(p); regexes.extend(r)
        return (phrases, regexes)
    return ([], [])
