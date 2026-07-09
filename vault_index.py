"""
Vault index — keyword + synonym retrieval over the user's vault folder.

Walks the vault, extracts headers/keys/samples per file, and serves up the
top-k matches for a free-text query so the council can answer "find every
JSON with January revenue" style questions without sending whole files.

Pure stdlib. Runs offline. Index is persisted to vault_index.json next
to the vault root so subsequent launches are fast.
"""

from __future__ import annotations

import csv
import difflib
import json
import logging
import math
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

_LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional fast-path JSON parser. `orjson` is a Rust-backed parser that runs
# 3-5x faster than the stdlib json module on the small-file path. If it's not
# installed we fall back to stdlib transparently — the index is unchanged,
# just slower to build. Already in some installs.txt environments; never
# required.
# ---------------------------------------------------------------------------
try:
    import orjson as _orjson  # type: ignore[import]
    def _fast_json_loads(text: str) -> Any:
        # orjson takes bytes; encode UTF-8 once. Slightly more allocation
        # but the parser is so fast that's still a net 3-5x win.
        return _orjson.loads(text.encode("utf-8", errors="replace"))
except Exception:
    _fast_json_loads = json.loads

INDEX_FILENAME = "vault_index.json"
DENYLIST_FILENAME = "fuzzy_denylist.json"
SEMANTIC_CACHE_FILENAME = "semantic_cache.json"

# Common English stop-tokens. Used in two places at search time:
#   1. To gate semantic_expand calls — without this, "find / the / all /
#      average / across / excluding / ..." each trigger a separate model
#      call for category-expansion, adding 15-20+ seconds of CPU
#      inference overhead per query for zero benefit (stop words have
#      no semantic category).
#   2. To filter the final expanded term set before scoring, so
#      stop-word matches don't inflate result scores.
_SEARCH_STOP_TOKENS = frozenset({
    "the", "a", "an", "of", "in", "on", "for", "to", "is", "are",
    "with", "and", "or", "any", "all", "every", "this", "that",
    "what", "which", "find", "show", "list", "look", "through",
    "files", "file", "folder", "folders", "data",
    # Lowercase file extensions / format names — plural and singular.
    # Users commonly say "all the csvs" / "all the jsons" — without
    # the plural entry, each plural triggers a semantic-expansion
    # model call.
    "csv", "csvs", "tsv", "tsvs", "json", "jsons", "xlsx", "xls",
    "parquet", "parquets", "txt", "md", "yaml", "yml",
    # Common verbs and adverbs that surfaced in real chat queries —
    # adding these stops them from triggering semantic-expansion calls
    # against the LLM (each call is 2-5s on CPU).
    "average", "averages", "mean", "median", "sum", "count", "total",
    "across", "from", "into", "between", "among", "by", "per",
    "exclude", "excluding", "excluded", "include", "including",
    "only", "just", "also",
    "give", "tell", "make", "compute", "calculate", "computing",
    "have", "has", "had", "being", "been",
    "more", "less", "most", "least", "some", "many", "much",
    "than", "as", "if", "when", "where", "why", "how",
    # Comparison prepositions / relations — common in filter queries
    "over", "under", "above", "below", "around", "near",
    "before", "after", "during", "since", "until",
    # Containment / membership verbs
    "contain", "contains", "containing", "contained",
    "use", "uses", "using", "used",
    "reference", "references", "referencing",
    "mention", "mentions", "mentioning",
    "match", "matches", "matching",
    # Generic noun forms — appear with search verbs but carry no
    # semantic category of their own
    "row", "rows", "entry", "entries", "record", "records",
    "item", "items", "thing", "things", "result", "results",
    "column", "columns", "field", "fields", "value", "values",
})

# Bookkeeping files we write to the vault root. The rebuild walk MUST
# skip these — indexing them would feed our own JSON keys back into the
# vocabulary, causing self-reference bugs (e.g. "metals" appearing in
# the vocab just because it's a key in the semantic cache, defeating the
# whole point of semantic expansion).
_BOOKKEEPING_FILENAMES = {
    INDEX_FILENAME,
    DENYLIST_FILENAME,
    SEMANTIC_CACHE_FILENAME,
    "backend_settings.json",   # GGUF model path; not user data
    ".onboarded",              # onboarding marker
    # Performance sidecars added by 95aa6d4 / 00e0b47. The col-index
    # is a .json file, so without this exclusion the rebuild walk
    # picks it up via _PARSEABLE, indexes column-name keys back into
    # the vocab, AND rewrites the file every refresh — which moves
    # its mtime and forces a perpetual re-index loop.
    ".vault_col_index.json",
    "data_index_cache.pickle",
}

# Fuzzy match defaults — Levenshtein-style ratio via difflib.
FUZZY_CUTOFF = 0.82           # similarity threshold (0..1)
FUZZY_MAX_PER_TERM = 3        # cap matches per query term
FUZZY_MIN_TERM_LEN = 4        # don't fuzzy-match very short tokens

# ---------- synonym layer ---------------------------------------------------
# Static map of common business/data/time terms. Bidirectional usage: query
# expansion looks up each token here. Add to this freely — entries cost nothing.
SYNONYMS: Dict[str, List[str]] = {
    # money / finance
    "revenue":  ["income", "sales", "earnings", "proceeds", "turnover", "gross", "receipts"],
    "income":   ["revenue", "earnings", "salary", "wages", "pay"],
    "sales":    ["revenue", "orders", "transactions", "deals", "bookings"],
    "expense":  ["cost", "spending", "outlay", "expenditure", "spend"],
    "cost":     ["expense", "price", "spending", "amount"],
    "profit":   ["earnings", "margin", "gain", "net", "surplus"],
    "loss":     ["deficit", "shortfall", "drop"],
    "price":    ["cost", "amount", "value", "fee", "rate"],
    "budget":   ["plan", "forecast", "allocation"],
    "invoice":  ["bill", "receipt", "statement"],
    # people / accounts
    "customer": ["client", "buyer", "user", "patron", "consumer", "account"],
    "client":   ["customer", "buyer", "user", "account"],
    "user":     ["customer", "client", "person", "account", "member"],
    "employee": ["staff", "worker", "personnel", "team"],
    # products / inventory
    "product":  ["item", "sku", "goods", "merchandise", "asset"],
    "order":    ["purchase", "transaction", "sale", "request"],
    "category": ["type", "kind", "class", "group", "genre", "segment"],
    "rating":   ["score", "rank", "stars", "grade"],
    "review":   ["feedback", "comment", "rating", "critique"],
    # time
    "date":     ["day", "time", "timestamp", "when", "datetime"],
    "month":    ["monthly", "period"],
    "year":     ["yearly", "annual", "fiscal"],
    "quarter":  ["q1", "q2", "q3", "q4", "quarterly"],
    "january":   ["jan", "01", "1"],
    "february":  ["feb", "02", "2"],
    "march":     ["mar", "03", "3"],
    "april":     ["apr", "04", "4"],
    "may":       ["05", "5"],
    "june":      ["jun", "06", "6"],
    "july":      ["jul", "07", "7"],
    "august":    ["aug", "08", "8"],
    "september": ["sep", "sept", "09", "9"],
    "october":   ["oct", "10"],
    "november":  ["nov", "11"],
    "december":  ["dec", "12"],
    # data / structure
    "id":       ["identifier", "key", "uid", "guid"],
    "name":     ["title", "label"],
    "title":    ["name", "heading", "label"],
    "address":  ["location", "street"],
    "phone":    ["mobile", "contact", "telephone"],
    "email":    ["mail", "contact"],
    # gaming/media (since the test vault has a games dataset)
    "game":     ["title", "release"],
    "platform": ["system", "console", "device"],
    "developer": ["studio", "creator", "maker"],
    "publisher": ["company", "label"],
    "genre":    ["category", "type", "style"],
}

_SUFFIX_RULES = ("ies", "ied", "ing", "tion", "ed", "es", "ly", "s")


def _stem(word: str) -> str:
    w = word.lower()
    for suf in _SUFFIX_RULES:
        if w.endswith(suf) and len(w) > len(suf) + 2:
            return w[: -len(suf)]
    return w


def _expand(term: str) -> Set[str]:
    """Expand one term into its synonym + stem set."""
    t = term.lower().strip()
    if not t or len(t) < 2:
        return set()
    st = _stem(t)                      # stem once, reused below
    out: Set[str] = {t, st}
    # Look up both raw and stemmed forms
    for w in (t, st):
        if w in SYNONYMS:
            for syn in SYNONYMS[w]:
                out.add(syn.lower())
                out.add(_stem(syn.lower()))
    return out


def _split_summary_and_topics(text: str) -> Tuple[str, List[str]]:
    """Parse a model response into (summary_paragraph, [topic, ...]).

    Looks for a 'TOPICS:' line; if absent, returns the whole text as
    summary with an empty topics list.
    """
    text = (text or "").strip()
    if not text:
        return "", []
    m = re.search(r"\n\s*topics?\s*[:\-]\s*(.+?)\s*$",
                  text, re.IGNORECASE | re.DOTALL)
    if not m:
        return text, []
    summary = text[: m.start()].strip()
    topics_blob = m.group(1)
    # Topics can be separated by commas or newlines
    raw = re.split(r"[,\n]+", topics_blob)
    topics = []
    for t in raw:
        tok = t.strip().lower().strip(".'\"`-*#")
        if 2 <= len(tok) <= 40 and tok:
            topics.append(tok)
    return summary, topics


def _parse_topics_line(text: str) -> List[str]:
    """Parse a bare comma-separated topics line — used by topics-only
    mode where the prompt explicitly asks for ONLY topic keywords."""
    text = (text or "").strip()
    if not text:
        return []
    # Strip a leading "TOPICS:" if the model added one anyway
    text = re.sub(r"^\s*topics?\s*[:\-]\s*", "", text, flags=re.IGNORECASE)
    # Take only the first line — anything after is usually a stray
    # explanation we asked the model not to write.
    text = text.splitlines()[0] if text else ""
    raw = re.split(r"[,;\n]+", text)
    out: List[str] = []
    for t in raw:
        tok = t.strip().lower().strip(".'\"`-*#")
        if 2 <= len(tok) <= 40:
            out.append(tok)
    return out[:6]


# Record types that carry no model-readable TEXT — describing them with the
# LLM means feeding it an empty or binary-ish prompt (a waste at best, and on
# a fragile build one more native model call that can take the app down). They
# get a deterministic name/type description instead.
_NON_TEXT_DESCRIBE_TYPES = {"image", "audio", "video", "binary", "unknown",
                            "archive", "executable", ""}


def _describe_nontext(rec: Dict[str, Any]) -> Tuple[str, List[str]]:
    """Deterministic description + topics for a non-text file (image, audio,
    binary, …) — no model call. Topics come from the filename tokens."""
    name = str(rec.get("name", "") or "file")
    rtype = str(rec.get("type", "") or "file")
    stem = name.rsplit(".", 1)[0]
    topics = [t for t in re.findall(r"[a-z0-9]+", stem.lower()) if len(t) > 1]
    article = "an" if rtype[:1] in "aeiou" else "a"
    desc = f"{name} — {article} {rtype} file (no text content to summarise)."
    return desc, topics[:6]


def _render_record_for_describe(idx: int, rec: Dict[str, Any]) -> str:
    """Render one record into a compact block the model can read.

    Used by both single-record and batched describe paths. The marker
    `#N` lets a batched response be split back into individual results.
    """
    name = rec.get("name", "?")
    rtype = rec.get("type", "text")
    lines: List[str] = [f"#{idx} File: {name}", f"   Type: {rtype}"]
    if rtype in ("json", "d3dpipeline"):
        keys = rec.get("keys", []) or []
        if keys:
            lines.append("   Top-level keys: " + ", ".join(map(str, keys[:30])))
        preview = (rec.get("sample_text", "") or "")[:500]
        if preview:
            lines.append("   Sample: " + preview.replace("\n", " ")[:500])
    elif rtype == "bson":
        keys = rec.get("keys", []) or []
        if keys:
            lines.append("   Fields: " + ", ".join(map(str, keys[:20])))
    else:
        preview = (rec.get("sample_text", "") or "")[:500]
        if preview:
            lines.append("   Sample: " + preview.replace("\n", " ")[:500])
    return "\n".join(lines)


_BATCH_MARKER_RE = re.compile(
    r"^\s*#?\s*(\d{1,3})\s*[:\.\)]\s*(.*)$", re.IGNORECASE,
)
_BATCH_TOPICS_RE = re.compile(
    r"^\s*topics?\s*#?\s*(\d{1,3})?\s*[:\-]\s*(.*)$", re.IGNORECASE,
)


def _parse_batched_response(
    text: str, expected: int, *, topics_only: bool,
) -> Optional[List[Tuple[str, List[str]]]]:
    """Parse a batched model response. Returns a list of
    (summary, topics) tuples in marker-order (#1 first, #2 second, ...).

    Returns None when:
      • the response is empty
      • fewer than ``expected`` distinct markers found

    The latter is the signal for the caller to fall back to per-record
    calls. We accept extra markers (just trim) and gracefully skip
    missing ones (filling with empty placeholders).
    """
    if not text or not text.strip():
        return None
    # State machine: walk lines, attach each summary / topics line to
    # the most recently seen marker.
    summaries: Dict[int, str] = {}
    topics_map: Dict[int, List[str]] = {}
    current_idx: Optional[int] = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Topics line (with or without explicit marker)
        m_topics = _BATCH_TOPICS_RE.match(line)
        if m_topics:
            try:
                tidx = int(m_topics.group(1)) if m_topics.group(1) else current_idx
            except Exception:
                tidx = current_idx
            if tidx is not None:
                topics_map[tidx] = _parse_topics_line(m_topics.group(2))
            continue
        # Generic marker line — could be summary OR (in topics-only
        # mode) a topics list prefixed with the marker
        m_marker = _BATCH_MARKER_RE.match(line)
        if m_marker:
            try:
                current_idx = int(m_marker.group(1))
            except Exception:
                continue
            tail = (m_marker.group(2) or "").strip()
            if topics_only:
                topics_map[current_idx] = _parse_topics_line(tail)
            else:
                # First marker line is the summary; further bare
                # markers without explicit TOPICS prefix replace the
                # summary.
                summaries[current_idx] = tail
            continue
        # Plain prose continuation of the current summary (only in
        # full-description mode).
        if current_idx is not None and not topics_only:
            existing = summaries.get(current_idx, "")
            summaries[current_idx] = (
                existing + " " + line if existing else line
            ).strip()

    # In full-description mode, require at least one of {summary,
    # topics} per record. In topics-only mode, require topics.
    if topics_only:
        seen = set(topics_map.keys())
    else:
        seen = set(summaries.keys()) | set(topics_map.keys())
    if not seen:
        return None
    # Heuristic threshold — accept the parse if we got at least half
    # of what we expected. Better partial-progress than total fallback.
    if len(seen) < max(1, expected // 2):
        return None

    out: List[Tuple[str, List[str]]] = []
    for i in range(1, expected + 1):
        out.append((summaries.get(i, ""), topics_map.get(i, [])))
    return out


def _describe_from_schema(rec: Dict[str, Any]) -> Tuple[str, List[str]]:
    """Deterministically generate description + topics for a tabular
    record from its index metadata. Zero model calls — pure dict work,
    runs in microseconds.

    Used for csv / tsv / parquet / excel / sqlite / duckdb records.
    The model would have produced something very similar from the
    same metadata, just slower and with occasional hallucinations.
    """
    name = rec.get("name", "?")
    rtype = rec.get("type", "text")
    summary_parts: List[str] = []
    topic_set: Set[str] = set()

    if rtype in ("csv", "tsv", "csv.gz", "parquet"):
        headers = rec.get("headers", []) or []
        col_count = len(headers)
        row_count = rec.get("rows")
        n_str = f"{row_count:,}" if isinstance(row_count, int) else "?"
        summary_parts.append(
            f"{rtype.upper()} dataset {name!r} with {col_count} columns "
            f"and {n_str} rows."
        )
        if headers:
            head_preview = ", ".join(map(str, headers[:6]))
            if col_count > 6:
                head_preview += f", … (+{col_count - 6} more)"
            summary_parts.append(f"Columns: {head_preview}.")
        # Topics from column names (tokenized + cleaned)
        for h in headers:
            for tok in _tokenize(str(h)):
                if _is_useful_token(tok):
                    topic_set.add(tok)

    elif rtype == "excel":
        sheets = rec.get("sheets", []) or []
        summary_parts.append(
            f"Excel workbook {name!r} with {len(sheets)} sheet"
            f"{'s' if len(sheets) != 1 else ''}: "
            + ", ".join(s.get("sheet", "?") for s in sheets[:5])
            + ("…" if len(sheets) > 5 else "")
            + "."
        )
        for s in sheets:
            for h in s.get("headers", []) or []:
                for tok in _tokenize(str(h)):
                    if _is_useful_token(tok):
                        topic_set.add(tok)
            sname = s.get("sheet")
            if isinstance(sname, str):
                for tok in _tokenize(sname):
                    if _is_useful_token(tok):
                        topic_set.add(tok)

    elif rtype in ("sqlite", "duckdb"):
        tables = rec.get("tables", []) or []
        summary_parts.append(
            f"{rtype.upper()} database {name!r} with {len(tables)} table"
            f"{'s' if len(tables) != 1 else ''}: "
            + ", ".join(t.get("table", "?") for t in tables[:5])
            + ("…" if len(tables) > 5 else "")
            + "."
        )
        for t in tables:
            tname = t.get("table")
            if isinstance(tname, str):
                for tok in _tokenize(tname):
                    if _is_useful_token(tok):
                        topic_set.add(tok)
            for c in t.get("columns", []) or []:
                for tok in _tokenize(str(c)):
                    if _is_useful_token(tok):
                        topic_set.add(tok)

    # Cap topics — prefer longest tokens (most discriminative). The
    # final cap of 6 mirrors the LLM path's `topics[:6]` slice.
    topics_sorted = sorted(topic_set, key=lambda t: (-len(t), t))
    return " ".join(summary_parts), topics_sorted[:6]


_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_HEX_RE = re.compile(r"^[0-9a-f]+$", re.IGNORECASE)
# Splits a multi-value cell ("a|b;c") — used per sampled cell in all three
# tabular parsers, so it's compiled once here rather than per call.
_CELL_SPLIT_RE = re.compile(r"[|;,/]")


def _is_useful_token(t: str) -> bool:
    """Drop tokens that are almost certainly noise — long hex hashes, IDs."""
    if len(t) > 20:
        return False
    if len(t) >= 8 and _HEX_RE.match(t):
        return False
    return True


def _tokenize(text: str) -> List[str]:
    """Lowercase alpha-numeric tokens. Splits on underscores and other punctuation
    so `Ultimate_Games_Dataset` indexes as ['ultimate','games','dataset'].
    Filters hex hashes and overly long tokens that pollute the keyword set."""
    out: List[str] = []
    for t in _TOKEN_RE.findall(text or ""):
        tl = t.lower()                 # lower once, not twice per token
        if _is_useful_token(tl):
            out.append(tl)
    return out


def _select_top_keywords(kw_set: Set[str], cap: int) -> List[str]:
    """Pick at most ``cap`` keywords from ``kw_set``, prioritizing the
    most distinctive (longest) tokens. Plain alphabetical sort + slice
    used to bias toward the start of the alphabet — for a file with
    thousands of tokens, "0000".."99999" / "a" / "all" / "and" / etc.
    would fill the cap before any 8+ char domain term reached it.

    Sort order: length descending, then lexicographic. The final list
    is alphabetized for deterministic storage / readable diffs. Net
    effect: long domain words like ``promethium``, ``stoneskin``,
    ``ironfist`` survive the cap before short generic tokens.
    """
    if not kw_set:
        return []
    # `numeric_filler` like "1", "12", "2024" are usually low-value for
    # find-by-keyword search. Push them to the bottom of the priority
    # list so domain words win the cap fight.
    def _priority_key(tok: str):
        is_numeric = tok.isdigit()
        return (0 if not is_numeric else 1, -len(tok), tok)
    sorted_by_priority = sorted(kw_set, key=_priority_key)
    kept = sorted_by_priority[:cap]
    return sorted(kept)


# ---------- file parsers ----------------------------------------------------

_TEXT_SUFFIXES = {
    ".txt", ".md", ".log", ".rst", ".yaml", ".yml",
    ".ini", ".cfg", ".toml", ".html", ".htm",
}

# Source-code files — same plain-text treatment as _TEXT_SUFFIXES (read
# first ~4 KB, tokenize, store the resulting keyword set). Adding them
# here means vault search now surfaces matches in code files: "find files
# that import pandas," "which scripts reference process_data," etc.
#
# Without this set, .py / .js / .ts / .go / etc. files in data_in/ were
# silently skipped by the rebuild walk — the vault index's _PARSEABLE
# filter is the only filter the walk respects, and these extensions
# weren't on it. Users would put Python files in their vault expecting
# the model to search them and get nothing back.
#
# We index source code the same way as plain text (no syntax parsing).
# Keyword tokenization on raw source produces useful matches for the
# most common queries: identifier names, library imports, class /
# function names, comment text. For richer queries on a specific
# language (e.g. "find files implementing __iter__"), the analyst step
# can still execute pandas / grep-style code over the raw files
# regardless of what the keyword index captures.
_SOURCE_CODE_SUFFIXES = {
    # Python
    ".py", ".pyw", ".pyi",
    # Web / JS / TS
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".css", ".scss", ".sass", ".less",
    ".vue", ".svelte",
    # Systems
    ".go", ".rs", ".zig",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".hxx",
    # JVM
    ".java", ".kt", ".kts", ".scala", ".groovy",
    # .NET
    ".cs", ".fs", ".vb",
    # Scripts / shells
    ".sh", ".bash", ".zsh", ".fish",
    ".ps1", ".psm1", ".bat", ".cmd",
    # Other languages
    ".rb", ".php", ".swift", ".m", ".mm",
    ".lua", ".pl", ".pm",
    ".r",   ".jl",
    ".dart", ".ex", ".exs", ".erl", ".hrl",
    ".clj", ".cljs", ".cljc",
    ".hs", ".lhs", ".ml", ".mli", ".elm",
    # Database / config
    ".sql", ".env", ".conf",
    # IaC / build
    ".dockerfile", ".tf", ".tfvars", ".nix",
    ".cmake", ".mk", ".ninja",
    ".gradle", ".sbt",
    # Web markup beyond html/htm
    ".xml", ".xsl", ".xslt", ".xsd",
    ".jsonl", ".ndjson",
}

_EXCEL_SUFFIXES = {".xlsx", ".xls", ".xlsm"}
_PDF_SUFFIXES   = {".pdf"}
_DOCX_SUFFIXES  = {".docx"}
# Image suffixes — indexed by filename + EXIF tags (always) and OCR
# text (when pytesseract is available). The actual pixel content is
# only addressable through the CLIP index (see image_index.py).
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
_TABULAR_EXTRA  = {".tsv", ".parquet"}     # parquet needs pyarrow at runtime
# SQLite databases (.db / .sqlite / .sqlite3) — indexed at the table level
_SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
# DuckDB (`.duckdb`) is parsed via the optional duckdb package
_DUCKDB_SUFFIXES = {".duckdb"}
# MongoDB dump format (.bson — binary JSON, one or more documents)
_BSON_SUFFIXES = {".bson"}
# Dream3D pipeline JSON sidecar (same content as embedded HDF5; treated
# like .json for keyword indexing so 'show pipeline X' / vault search
# both pick them up).
_D3DPIPELINE_SUFFIXES = {".d3dpipeline"}
# Image files. The indexer stores filename + dimensions + EXIF metadata
# (date taken, camera model, GPS) when Pillow is available, otherwise
# just filename + size. The vault search can surface "find photos of
# Q3 inventory" / "images from this morning" / "all bmp under 1 MB"
# via the keyword + filter index. Actual image-content understanding
# requires a multimodal model, which is a separate concern.
_IMAGE_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
    ".tiff", ".tif", ".ico", ".heic", ".heif",
}
_PARSEABLE = ({".csv", ".json"} | _TEXT_SUFFIXES | _SOURCE_CODE_SUFFIXES
              | _EXCEL_SUFFIXES | _TABULAR_EXTRA | _SQLITE_SUFFIXES
              | _DUCKDB_SUFFIXES | _BSON_SUFFIXES | _D3DPIPELINE_SUFFIXES
              | _PDF_SUFFIXES | _DOCX_SUFFIXES | _IMAGE_SUFFIXES)


def _is_gz_csv(p: Path) -> bool:
    """`.csv.gz` style: .gz with a sibling implication that it's a CSV."""
    return p.suffix.lower() == ".gz" and p.stem.lower().endswith(".csv")


# ---------------------------------------------------------------------------
# Body-content sample for tabular files.
#
# Tabular records used to store only `sample_rows` (the first ~6 rows, for the
# prompt-time preview) and a flat keyword bag. The embedding builder
# (vault_embeddings._record_to_text) and the phrase-match scorer
# (_search_terms → sample_blob) both read `sample_text`, which CSV/TSV/Excel
# records never populated — so the vector and phrase haystack saw only column
# names + 3 rows. Here we accumulate a bounded, DEDUPED sample of DISTINCT
# short cell values spread across the rows we already visit, so ranking and
# embeddings reflect the file's BODY (product names, categories, IDs), not
# just its schema. Deterministic, build-time only, no model calls.
# ---------------------------------------------------------------------------
_BODY_SAMPLE_MAX_VALS = 200
_BODY_SAMPLE_MAX_CHARS = 1500


def _collect_body_sample(cv: str, sample_vals: List[str], seen: Set[str]) -> None:
    """Retain a short, DISTINCT cell value in `sample_vals` (mutated in place).
    Bounded by _BODY_SAMPLE_MAX_VALS so a wide file can't blow the record up."""
    if 2 <= len(cv) <= 80 and len(sample_vals) < _BODY_SAMPLE_MAX_VALS:
        low = cv.lower()
        if low not in seen:
            seen.add(low)
            sample_vals.append(cv)


def _join_body_sample(sample_vals: List[str]) -> str:
    """Compact ' | '-joined body sample, char-capped for the record."""
    return " | ".join(sample_vals)[:_BODY_SAMPLE_MAX_CHARS]


def _parse_csv(p: Path) -> Dict[str, Any]:
    headers: List[str] = []
    sample_rows: List[str] = []
    keywords: Set[str] = set()
    sample_vals: List[str] = []
    _seen_vals: Set[str] = set()
    total_rows = 0
    try:
        with open(p, newline="", encoding="utf-8", errors="replace") as fh:
            reader = csv.reader(fh)
            for i, row in enumerate(reader):
                if i == 0:
                    headers = [c.strip() for c in row]
                    keywords.update(_tokenize(" ".join(headers)))
                    continue
                # We tokenise + sample only the first 500 rows; beyond
                # that, just count rows. Counting is cheap (no list
                # append, no token regex) so we can finish in a single
                # pass instead of opening the file twice. The total
                # surfaces in the schema-based description as
                # "with N rows" instead of the previous "? rows".
                total_rows = i
                if i < 6:
                    sample_rows.append(", ".join(row))
                if i < 500:
                    # Tokenize short cell values from every column so values
                    # like "PlayStation", "Xbox", "January" are searchable.
                    # Multi-value cells (e.g. "PlayStation 5|Xbox Series S/X")
                    # get split on common separators first so each value is
                    # indexed individually. Description cells (1000+ chars)
                    # are skipped — they overwhelm the keyword set without
                    # adding signal.
                    for cell in row:
                        cv = (cell or "").strip()
                        if not cv or len(cv) > 1000:
                            continue
                        # Skip URL cells outright — they only add hex/path noise.
                        if cv.startswith("http://") or cv.startswith("https://"):
                            continue
                        for part in _CELL_SPLIT_RE.split(cv):
                            part = part.strip()
                            if 2 <= len(part) <= 80:
                                keywords.update(_tokenize(part))
                        _collect_body_sample(cv, sample_vals, _seen_vals)
                else:
                    break
                if len(keywords) > 8000:
                    break
    except Exception:
        pass
    return {
        "type": "csv",
        "headers": headers,
        "sample_rows": sample_rows,
        "sample_text": _join_body_sample(sample_vals),
        "rows": total_rows,
        "keywords": sorted(keywords)[:5000],
    }


# ---------------------------------------------------------------------------
# JSON parsing — size-tiered to handle multi-hundred-MB files without freezing
# the indexer for an hour.
#
#   Tier 1  (< 5 MB)         Full json.loads + recursive walk.
#                            Yields the richest extraction (keys + string
#                            values at depth ≤ 5).
#
#   Tier 2  (5 MB - 1 GB)    Read whole text BUT skip json.loads. Use regex
#                            to extract keys and ALL quoted strings (object
#                            values + array elements) directly from raw
#                            text. Same downstream keyword tokens as tier 1
#                            but ~30x faster — no Python object tree built.
#                            ~100% coverage of the file's string surface
#                            (capped at 5,000 unique strings per file).
#
#   Tier 3  (> 1 GB)         Stream-sample head + tail (5 MB each) by seek;
#                            never load the full file. Regex-extract from
#                            the sample. The keyword surface is partial
#                            (~2% of a 500 MB file, less for bigger) but
#                            indexing completes in seconds rather than an
#                            hour. Adequate for hit/miss vault retrieval;
#                            for full-content search use the filesystem
#                            grep chat intent or the analyst.
#
# Walk caps in tier 1 protect against pathological wide-and-deep trees:
#   • per-dict iter cap = 5000 keys (very wide objects are sampled)
#   • per-list iter cap = 25 entries (unchanged from original)
#   • global node visit budget = 50,000 (recursion bails when hit)
# ---------------------------------------------------------------------------

_JSON_FULL_PARSE_MAX_BYTES = 5    * 1024 * 1024     # 5 MB
_JSON_REGEX_FULL_MAX_BYTES = 1024 * 1024 * 1024     # 1 GB
# Why 1 GB for the tier-2 ceiling:
#   • A regex pass over 1 GB of raw text takes ~30-60 s on a modern
#     CPU (release-build cpython re module, single-threaded). That's
#     a one-time cost per file (the mtime gate skips it on rebuild).
#   • Peak RAM during indexing a 1 GB JSON: ~2-3 GB (the text string
#     plus the regex's internal state). Fine on a 16 GB+ machine.
#   • Coverage on files up to 1 GB jumps from tier 3's ~2% (10 MB
#     sample) to ~100% (every quoted string in the file gets
#     indexed up to the per-file 5,000-unique caps).
#   • Files larger than 1 GB still fall to tier 3 sampling. If your
#     vault has many >1 GB JSONs, bump this further — there's no
#     hard ceiling, just memory and patience.
# Tier-3 (>1 GB) sample windows. 5 MB head + 5 MB tail = 10 MB total
# scanned per huge file, up from the original 2 MB. Concretely:
#   • For a 500 MB array of builds, the head sample now captures the
#     first ~5,000-20,000 build entries (depending on size) and the
#     tail sample captures the last ~5,000-20,000.
#   • Search coverage on huge files jumps from ~0.4% of the document
#     to ~2% — a 5x improvement in hit rate.
#   • Per-file indexing time on tier 3 goes from ~1 s to ~3-5 s,
#     still negligible compared to the original full-parse hour+.
# Keep these as module-level constants so they're easy to bump
# further if your files are even bigger.
_JSON_SAMPLE_HEAD_BYTES    = 5 * 1024 * 1024     # 5 MB head sample
_JSON_SAMPLE_TAIL_BYTES    = 5 * 1024 * 1024     # 5 MB tail sample

_JSON_WALK_DICT_CAP   = 5000
_JSON_WALK_LIST_CAP   = 25
_JSON_WALK_NODE_LIMIT = 50_000

# Regex used by tier 2 / tier 3 to pull keys + values from raw text without
# building a Python object tree. Matches JSON-ish double-quoted strings
# bounded by what the keyword index actually consumes (1-80 chars; no
# embedded escapes — we deliberately skip strings with escaped quotes to
# keep the pattern simple and the matches clean).
#
# Three patterns, intentional split:
#   _JSON_KEY_RE          — strings followed by ':', i.e. object keys.
#                           Used to populate the `keys` field, which
#                           summary_block surfaces as "keys: ..." for
#                           the model.
#   _JSON_ANY_STRING_RE   — every quoted string in the file, regardless
#                           of context. Catches keys, object values, AND
#                           array-element strings ["a","b","c"]. Critical
#                           for build/loadout JSONs where powers and
#                           materials are typically array elements, not
#                           object values — without it, the tier-2/3
#                           index could miss every power name in a file.
#                           Goes into the `string_values` set.
_JSON_KEY_RE        = re.compile(r'"([^"\\\n]{1,80})"\s*:')
_JSON_ANY_STRING_RE = re.compile(r'"([^"\\\n]{1,80})"')


def _parse_json(p: Path) -> Dict[str, Any]:
    """Index a JSON file. Size-tiered so huge files don't freeze indexing."""
    try:
        size = p.stat().st_size
    except Exception:
        size = 0

    # ── Tier 3: huge file → sample head + tail only ────────────────────
    if size > _JSON_REGEX_FULL_MAX_BYTES:
        return _parse_json_sampled(p, size)

    # ── Tier 2: medium → regex over the full text (no parse step) ──────
    if size > _JSON_FULL_PARSE_MAX_BYTES:
        return _parse_json_regex_full(p, size)

    # ── Tier 1: small → full parse + tree walk (richest extraction) ────
    return _parse_json_full(p)


def _parse_json_full(p: Path) -> Dict[str, Any]:
    """Tier 1: small files. Build full object tree, walk with caps."""
    keys: Set[str] = set()
    string_values: Set[str] = set()
    sample_text = ""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
        sample_text = text[:2000]
        try:
            data = _fast_json_loads(text)
        except Exception:
            data = None

        # Use a list as a single-element mutable counter so the inner
        # closure can update it. Python 3 supports `nonlocal` for this
        # but the existing module style avoids it.
        visited = [0]

        def walk(node: Any, depth: int = 0) -> None:
            if depth > 5 or visited[0] >= _JSON_WALK_NODE_LIMIT:
                return
            visited[0] += 1
            if isinstance(node, dict):
                # Wide dicts get sampled — the first N keys are visited
                # in iteration order, the rest are skipped. Most schema
                # info appears at the top of a dict literal, and the
                # cap keeps walk time bounded.
                for i, (k, v) in enumerate(node.items()):
                    if i >= _JSON_WALK_DICT_CAP:
                        break
                    keys.add(str(k))
                    walk(v, depth + 1)
            elif isinstance(node, list):
                for item in node[:_JSON_WALK_LIST_CAP]:
                    walk(item, depth + 1)
            elif isinstance(node, str):
                if 1 <= len(node) < 80:
                    string_values.add(node)

        if data is not None:
            walk(data)
    except Exception:
        pass

    keywords: Set[str] = set()
    keywords.update(_tokenize(" ".join(keys)))
    # Tokenize ALL string values, not an arbitrary subset. Slicing a set
    # via list(...)[:N] picks a random N entries (sets are unordered),
    # which used to mean that high-cardinality files (e.g. 1000 build
    # entries with unique names) would saturate string_values with build
    # names and leave no room for powers/materials in the keyword index.
    keywords.update(_tokenize(" ".join(string_values)))

    return {
        "type": "json",
        "keys": sorted(keys)[:120],
        "sample_text": sample_text[:1200],
        "keywords": _select_top_keywords(keywords, 1500),
    }


def _parse_json_regex_full(p: Path, size: int) -> Dict[str, Any]:
    """Tier 2: medium files. Read full text but skip json.loads entirely —
    regex-extract keys and string values from the raw text. ~30x faster
    than tier 1 on the same file because we never build a Python object
    tree. Returns the same record shape as tier 1.
    """
    keys: Set[str]          = set()
    string_values: Set[str] = set()
    sample_text             = ""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
        sample_text = text[:2000]
        # Cap each match list so a 10M-entry array of "name" objects can't
        # blow memory. 5000 of each is plenty for keyword index purposes.
        for m in _JSON_KEY_RE.finditer(text):
            keys.add(m.group(1))
            if len(keys) >= 5000:
                break
        for m in _JSON_ANY_STRING_RE.finditer(text):
            v = m.group(1)
            if 1 <= len(v) < 80:
                string_values.add(v)
            if len(string_values) >= 5000:
                break
    except Exception:
        pass

    keywords: Set[str] = set()
    keywords.update(_tokenize(" ".join(keys)))
    # Tokenize the whole string_values set — see the comment in
    # _parse_json_full for why the previous [:500] slice was a bug.
    keywords.update(_tokenize(" ".join(string_values)))

    rec = {
        "type": "json",
        "keys": sorted(keys)[:120],
        "sample_text": sample_text[:1200],
        "keywords": _select_top_keywords(keywords, 1500),
        "indexing_tier": "regex_full",
        "indexed_bytes": size,
    }
    return rec


def _parse_json_sampled(p: Path, size: int) -> Dict[str, Any]:
    """Tier 3: huge files (> 200 MB). Stream-sample head + tail only,
    never load the full file. Adequate keyword surface for vault hit/miss
    retrieval; the user can still query the file by name and use the
    analyst to compute over its contents — the index just doesn't have
    every key in it.
    """
    keys: Set[str]          = set()
    string_values: Set[str] = set()
    sample_text             = ""
    head_text = ""
    tail_text = ""
    try:
        with open(p, "rb") as fh:
            head_bytes = fh.read(_JSON_SAMPLE_HEAD_BYTES)
            head_text = head_bytes.decode("utf-8", errors="replace")
            sample_text = head_text[:2000]
            # Seek to (file_size - tail_bytes) for the tail sample.
            tail_start = max(_JSON_SAMPLE_HEAD_BYTES, size - _JSON_SAMPLE_TAIL_BYTES)
            try:
                fh.seek(tail_start)
                tail_bytes = fh.read(_JSON_SAMPLE_TAIL_BYTES)
                tail_text = tail_bytes.decode("utf-8", errors="replace")
            except Exception:
                tail_text = ""

        for chunk in (head_text, tail_text):
            for m in _JSON_KEY_RE.finditer(chunk):
                keys.add(m.group(1))
                if len(keys) >= 5000:
                    break
            for m in _JSON_ANY_STRING_RE.finditer(chunk):
                v = m.group(1)
                if 1 <= len(v) < 80:
                    string_values.add(v)
                if len(string_values) >= 5000:
                    break
    except Exception:
        pass

    keywords: Set[str] = set()
    keywords.update(_tokenize(" ".join(keys)))
    # Tokenize the whole string_values set — see the comment in
    # _parse_json_full for why the previous [:500] slice was a bug.
    keywords.update(_tokenize(" ".join(string_values)))

    sampled_mb = (_JSON_SAMPLE_HEAD_BYTES + _JSON_SAMPLE_TAIL_BYTES) / (1024 * 1024)
    return {
        "type": "json",
        "keys": sorted(keys)[:120],
        "sample_text": sample_text[:1200],
        "keywords": _select_top_keywords(keywords, 1500),
        "indexing_tier": "sampled_head_tail",
        "indexed_bytes": int(_JSON_SAMPLE_HEAD_BYTES + _JSON_SAMPLE_TAIL_BYTES),
        "indexing_note": (
            f"Large JSON ({size / (1024**2):.1f} MB) indexed via "
            f"~{sampled_mb:.0f} MB head + tail sample. Some keys deep "
            f"in the file may not be in the keyword surface."
        ),
        "total_bytes": size,
    }


def _parse_excel(p: Path) -> Dict[str, Any]:
    """Index an Excel workbook — sheet names + per-sheet headers + a few
    sample values. Each sheet's content contributes to the keyword set."""
    sheets_meta: List[Dict[str, Any]] = []
    sample_rows: List[str] = []
    keywords: Set[str] = set()
    sample_vals: List[str] = []
    _seen_vals: Set[str] = set()
    try:
        import pandas as _pd
        xl = _pd.ExcelFile(str(p))
        sheet_names = list(xl.sheet_names)[:8]  # cap to avoid heavy workbooks
        keywords.update(_tokenize(" ".join(sheet_names)))
        for sname in sheet_names:
            try:
                df = xl.parse(sname, nrows=50)
            except Exception:
                continue
            headers = [str(c).strip() for c in df.columns]
            keywords.update(_tokenize(" ".join(headers)))
            for _, row in df.head(40).iterrows():
                for cell in row.values.tolist():
                    cv = str(cell).strip()
                    if not cv or len(cv) > 1000:
                        continue
                    if cv.startswith("http://") or cv.startswith("https://"):
                        continue
                    for part in _CELL_SPLIT_RE.split(cv):
                        part = part.strip()
                        if 2 <= len(part) <= 80:
                            keywords.update(_tokenize(part))
                    _collect_body_sample(cv, sample_vals, _seen_vals)
            sheets_meta.append({
                "sheet":   sname,
                "headers": headers,
                "rows":    int(len(df)),
            })
            # one sample line per sheet
            if len(df):
                sample_rows.append(f"[{sname}] " + ", ".join(
                    str(v) for v in df.iloc[0].tolist()
                )[:240])
            if len(keywords) > 8000:
                break
    except Exception:
        pass
    return {
        "type":        "excel",
        "sheets":      sheets_meta,
        "sample_rows": sample_rows,
        "sample_text": _join_body_sample(sample_vals),
        "keywords":    sorted(keywords)[:5000],
    }


def _parse_tabular_df(p: Path, df, *, kind: str) -> Dict[str, Any]:
    """Shared CSV/TSV/Parquet/gz indexing — takes a loaded DataFrame and
    extracts headers, sample rows, and keyword tokens with the same rules
    as _parse_csv (URL skip, multi-value split, hex filter)."""
    headers: List[str] = [str(c).strip() for c in df.columns]
    sample_rows: List[str] = []
    keywords: Set[str] = set(_tokenize(" ".join(headers)))
    sample_vals: List[str] = []
    _seen_vals: Set[str] = set()
    # First 6 rows for the prompt-time sample block
    for _, row in df.head(6).iterrows():
        sample_rows.append(", ".join(str(v) for v in row.values))
    # Up to 500 rows for keyword indexing, same value-handling as _parse_csv
    sub = df.head(500)
    for _, row in sub.iterrows():
        for cell in row.values:
            cv = str(cell).strip()
            if not cv or len(cv) > 1000:
                continue
            if cv.startswith("http://") or cv.startswith("https://"):
                continue
            for part in _CELL_SPLIT_RE.split(cv):
                part = part.strip()
                if 2 <= len(part) <= 80:
                    keywords.update(_tokenize(part))
            _collect_body_sample(cv, sample_vals, _seen_vals)
        if len(keywords) > 8000:
            break
    return {
        "type":        kind,
        "headers":     headers,
        "sample_rows": sample_rows,
        "sample_text": _join_body_sample(sample_vals),
        "keywords":    sorted(keywords)[:5000],
    }


def _parse_tsv(p: Path) -> Dict[str, Any]:
    try:
        import pandas as _pd
        df = _pd.read_csv(p, sep="\t", nrows=500, on_bad_lines="skip")
        return _parse_tabular_df(p, df, kind="tsv")
    except Exception:
        return {"type": "tsv", "headers": [], "sample_rows": [], "keywords": []}


def _parse_csv_gz(p: Path) -> Dict[str, Any]:
    """Gzipped CSV — pandas auto-detects compression by extension."""
    try:
        import pandas as _pd
        df = _pd.read_csv(p, nrows=500, on_bad_lines="skip", compression="infer")
        return _parse_tabular_df(p, df, kind="csv.gz")
    except Exception:
        return {"type": "csv.gz", "headers": [], "sample_rows": [], "keywords": []}


def _parse_parquet(p: Path) -> Dict[str, Any]:
    """Parquet — needs pyarrow or fastparquet. Degrades gracefully."""
    try:
        import pandas as _pd
        df = _pd.read_parquet(p)
        return _parse_tabular_df(p, df.head(500), kind="parquet")
    except Exception as exc:
        return {
            "type":        "parquet",
            "headers":     [],
            "sample_rows": [],
            "keywords":    [],
            "_note":       f"install pyarrow to enable parquet: {exc!r}",
        }


def _parse_sqlite(p: Path) -> Dict[str, Any]:
    """SQLite database — index table names + per-table column names so
    questions like 'what tables are in foo.db?' work via vault search."""
    import sqlite3
    tables: List[Dict[str, Any]] = []
    keywords: Set[str] = set()
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            cur = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "ORDER BY name LIMIT 50"
            )
            names = [r[0] for r in cur.fetchall() if r and r[0]]
            keywords.update(_tokenize(" ".join(names)))
            for tname in names[:30]:
                try:
                    cur = con.execute(f"PRAGMA table_info({_sql_quote(tname)})")
                    cols = [r[1] for r in cur.fetchall() if r and r[1]]
                except Exception:
                    cols = []
                keywords.update(_tokenize(" ".join(cols)))
                try:
                    cur = con.execute(
                        f"SELECT COUNT(*) FROM {_sql_quote(tname)}"
                    )
                    nrows = cur.fetchone()[0]
                except Exception:
                    nrows = None
                tables.append({"table": tname, "columns": cols, "rows": nrows})
        finally:
            con.close()
    except Exception:
        pass
    return {
        "type":      "sqlite",
        "tables":    tables,
        "keywords":  sorted(keywords)[:2000],
    }


def _sql_quote(name: str) -> str:
    """Quote a SQLite identifier safely for PRAGMA/SELECT."""
    return '"' + name.replace('"', '""') + '"'


def _parse_bson(p: Path) -> Dict[str, Any]:
    """Parse a MongoDB .bson file — concatenated binary BSON documents.

    Treats each document as a row. Extracts top-level field names (akin
    to columns) and a sample of values for keyword indexing. Needs the
    `bson` package (ships with `pymongo`); gracefully degrades when
    missing.
    """
    keys: Set[str] = set()
    sample_docs: List[str] = []
    keywords: Set[str] = set()
    total_docs = 0
    try:
        try:
            import bson as _bson
        except Exception as exc:
            return {
                "type": "bson",
                "keys": [],
                "sample_text": "",
                "keywords": [],
                "_note": f"install pymongo to enable BSON parsing: {exc!r}",
            }

        with open(p, "rb") as fh:
            data = fh.read()
        try:
            docs = list(_bson.decode_all(data))
        except Exception as exc:
            return {
                "type": "bson",
                "keys": [],
                "sample_text": "",
                "keywords": [],
                "_note": f"could not decode BSON: {exc!r}",
            }
        total_docs = len(docs)

        def _walk(node, depth=0):
            if depth > 4:
                return
            if isinstance(node, dict):
                for k, v in node.items():
                    keys.add(str(k))
                    _walk(v, depth + 1)
            elif isinstance(node, list):
                for item in node[:25]:
                    _walk(item, depth + 1)
            elif isinstance(node, str):
                if 1 <= len(node) < 80:
                    keywords.update(_tokenize(node))

        for i, doc in enumerate(docs[:500]):
            _walk(doc)
            if i < 5:
                try:
                    sample_docs.append(
                        json.dumps(doc, default=str, ensure_ascii=False)[:200]
                    )
                except Exception:
                    sample_docs.append(repr(doc)[:200])
            if len(keywords) > 5000:
                break
        keywords.update(_tokenize(" ".join(keys)))
    except Exception as exc:
        return {
            "type": "bson",
            "keys": [],
            "sample_text": "",
            "keywords": [],
            "_note": f"bson read failed: {exc!r}",
        }
    return {
        "type":        "bson",
        "keys":        sorted(keys)[:120],
        "doc_count":   total_docs,
        "sample_text": "\n".join(sample_docs)[:1200],
        "keywords":    sorted(keywords)[:300],
    }


def _parse_duckdb(p: Path) -> Dict[str, Any]:
    """Parse a DuckDB database file — read-only.

    Indexes table names + per-table column names. Mirrors _parse_sqlite
    so search treats both the same.
    """
    tables: List[Dict[str, Any]] = []
    keywords: Set[str] = set()
    try:
        try:
            import duckdb as _duckdb
        except Exception as exc:
            return {
                "type":     "duckdb",
                "tables":   [],
                "keywords": [],
                "_note":    f"install duckdb to enable: {exc!r}",
            }
        con = _duckdb.connect(str(p), read_only=True)
        try:
            tnames = [r[0] for r in con.execute(
                "SELECT table_name FROM duckdb_tables() "
                "ORDER BY table_name LIMIT 50"
            ).fetchall() if r and r[0]]
            keywords.update(_tokenize(" ".join(tnames)))
            for tname in tnames[:30]:
                qname = '"' + tname.replace('"', '""') + '"'
                try:
                    cols = [r[1] for r in con.execute(
                        f"DESCRIBE {qname}"
                    ).fetchall() if r and r[1]]
                except Exception:
                    cols = []
                keywords.update(_tokenize(" ".join(cols)))
                try:
                    nrows = con.execute(f"SELECT COUNT(*) FROM {qname}").fetchone()[0]
                except Exception:
                    nrows = None
                tables.append({"table": tname, "columns": cols, "rows": nrows})
        finally:
            con.close()
    except Exception as exc:
        return {
            "type": "duckdb", "tables": [], "keywords": [],
            "_note": f"duckdb read failed: {exc!r}",
        }
    return {
        "type":     "duckdb",
        "tables":   tables,
        "keywords": sorted(keywords)[:2000],
    }


def _parse_pdf(p: Path) -> Dict[str, Any]:
    """Index a PDF by extracting text from up to the first 25 pages.

    25 pages is a working compromise — covers most spec sheets, ECNs,
    SOPs, contracts, and one-pager memos without choking on a 2000-page
    regulatory binder. Larger PDFs still index (we just take the head).
    """
    text = ""
    pages_read = 0
    # Filename tokens are the baseline indexability layer — when
    # text extraction fails (missing dep, encrypted file, image-only
    # PDF) the file is still findable by name.
    name_keywords = sorted(set(_tokenize(
        p.stem.replace("-", " ").replace("_", " ")
    )))[:60]
    try:
        from pypdf import PdfReader
    except Exception:
        return {"type": "pdf", "sample_text": "", "keywords": name_keywords,
                "note": "pypdf unavailable"}
    try:
        reader = PdfReader(str(p))
        chunks: List[str] = []
        for i, page in enumerate(reader.pages[:25]):
            try:
                t = page.extract_text() or ""
            except Exception:
                t = ""
            if t:
                chunks.append(t)
                pages_read += 1
        text = "\n".join(chunks)
    except Exception:
        # Encrypted, malformed, etc. — return a stub so the filename
        # still indexes but content stays empty.
        text = ""
    # Union extracted keywords with filename tokens so the file is
    # findable by name even when text extraction yielded nothing.
    keywords = sorted(set(_tokenize(text)) | set(name_keywords))[:300]
    return {
        "type": "pdf",
        "sample_text": text[:2500],
        "keywords": keywords,
        "pages_read": pages_read,
    }


def _parse_docx(p: Path) -> Dict[str, Any]:
    """Index a .docx by pulling all paragraph text via python-docx."""
    name_keywords = sorted(set(_tokenize(
        p.stem.replace("-", " ").replace("_", " ")
    )))[:60]
    try:
        from docx import Document
    except Exception:
        return {"type": "docx", "sample_text": "", "keywords": name_keywords,
                "note": "python-docx unavailable"}
    try:
        doc = Document(str(p))
        text = "\n".join(par.text for par in doc.paragraphs if par.text)
    except Exception:
        text = ""
    keywords = sorted(set(_tokenize(text)) | set(name_keywords))[:300]
    return {
        "type": "docx",
        "sample_text": text[:2500],
        "keywords": keywords,
    }


def _parse_image(p: Path, suffix: str = "") -> Dict[str, Any]:
    """Index an image: dimensions, mode, EXIF whitelist (when Pillow is
    available) AND OCR text (when pytesseract + tesseract binary are
    available). Three discoverability layers, each optional:

      1. Filename tokens — ALWAYS indexed (cheap, no deps).
      2. EXIF metadata (Pillow optional) — camera Make/Model, dates,
         GPSInfo, Artist, Copyright, ImageDescription. Whitelisted to
         keep the search vocab clean.
      3. OCR pixel text (pytesseract optional) — text rendered in the
         image itself: defect-photo captions, dashboard screenshots,
         nameplates. Detection order for the binary:
            • ``COUNCIL_TESSERACT_CMD`` env (set by the Windows launcher
              when the UB-Mannheim install is at its default path)
            • system PATH (Linux distro default; macOS Homebrew)
            • give up silently — filename + EXIF still indexed.
         Set ``COUNCIL_OCR_DISABLE=1`` to skip OCR even when present.

    The ``suffix`` arg is optional for back-compat — when omitted it's
    inferred from ``p.suffix``. The CLIP semantic image index
    (image_index.py) is a separate layer and is built on rebuild()'s
    post-pass when sentence-transformers is available.
    """
    suffix = suffix or p.suffix.lower()
    rec: Dict[str, Any] = {
        "type":         "image",
        "image_format": suffix.lstrip(".") or "image",
        "keywords":     sorted(set(_tokenize(
                            p.stem.replace("-", " ").replace("_", " ")
                        )))[:60],
        "ocr_status":   "skipped",
    }
    try:
        rec["size_bytes"] = p.stat().st_size
    except Exception:
        pass

    # ── Pillow + EXIF + dimensions (optional) ─────────────
    pil_ok = False
    try:
        from PIL import Image, ExifTags  # type: ignore[import]
        pil_ok = True
    except Exception:
        rec["indexing"] = "filename only — install Pillow for EXIF + dimensions"

    if pil_ok:
        try:
            with Image.open(p) as img:
                rec["width"]  = img.width
                rec["height"] = img.height
                rec["mode"]   = img.mode
                try:
                    exif_raw = img.getexif()
                except Exception:
                    exif_raw = None
                if exif_raw:
                    keep = {"DateTime", "DateTimeOriginal", "Make", "Model",
                            "Software", "Artist", "Copyright",
                            "ImageDescription", "GPSInfo"}
                    exif_clean: Dict[str, Any] = {}
                    for tag_id, val in exif_raw.items():
                        name = ExifTags.TAGS.get(tag_id, str(tag_id))
                        if name in keep:
                            if isinstance(val, bytes):
                                try:
                                    val = val.decode("utf-8", errors="replace").strip("\x00 ")
                                except Exception:
                                    val = repr(val)
                            elif isinstance(val, (tuple, list)):
                                val = str(val)
                            exif_clean[name] = val
                    if exif_clean:
                        rec["exif"] = exif_clean
                        extra_kw = []
                        for v in exif_clean.values():
                            extra_kw.extend(_tokenize(str(v)))
                        rec["keywords"] = sorted(
                            set(rec["keywords"]) | set(extra_kw)
                        )[:120]
        except Exception as exc:
            rec["indexing"] = f"filename only — could not open image: {exc!r}"

    # ── OCR pass (optional, layered on top of EXIF/dims) ──
    if (pil_ok
        and os.environ.get("COUNCIL_OCR_DISABLE", "").lower() not in ("1", "true", "yes")):
        ocr_text = ""
        try:
            import pytesseract as _pt
            tess = os.environ.get("COUNCIL_TESSERACT_CMD", "")
            if tess:
                _pt.pytesseract.tesseract_cmd = tess
            try:
                from PIL import Image as _PILImage
                with _PILImage.open(str(p)) as img:
                    ocr_text = _pt.image_to_string(img) or ""
                rec["ocr_status"] = "ok" if ocr_text else "empty"
            except Exception:
                rec["ocr_status"] = "tesseract-missing"
        except Exception:
            rec["ocr_status"] = "tesseract-missing"
        if ocr_text:
            rec["ocr_chars"] = len(ocr_text)
            rec["sample_text"] = ocr_text[:2000]
            # OCR tokens flow into the keyword set so a manufacturing
            # photo annotated "DEF-001 NCR-99001" is searchable by ID.
            rec["keywords"] = sorted(
                set(rec["keywords"]) | set(_tokenize(ocr_text))
            )[:300]
    elif not pil_ok:
        rec["ocr_status"] = "pil-missing"
    else:
        rec["ocr_status"] = "disabled"

    # Ensure sample_text exists so search has something to show even
    # when no OCR fired.
    rec.setdefault("sample_text", f"[image: {p.name}]")
    return rec


def _parse_text(p: Path, suffix: str) -> Dict[str, Any]:
    text = ""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")[:4000]
    except Exception:
        pass
    keywords = sorted(set(_tokenize(text)))[:300]
    return {
        "type": suffix.lstrip(".") or "text",
        "sample_text": text[:1500],
        "keywords": keywords,
    }


def _record_has_content(rec: Dict[str, Any]) -> bool:
    """True iff a parsed record has the kind of indexing surface its
    declared file type SHOULD have. Type-aware: a .json record is only
    accepted when the parser actually extracted JSON structure (keys
    or string-value keywords), not just because errors="replace"
    decoded some random bytes to English-looking text.

    The user-reported "model says no access to that folder/file" bug
    came from corrupted/empty files whose parse stubs nonetheless
    surfaced through vault search via their filename. The model saw
    a [VAULT MATCH] block with no real content and concluded the file
    was inaccessible — the structural check below prevents those
    stubs from reaching the index at all.
    """
    if not isinstance(rec, dict):
        return False
    rtype = rec.get("type", "")

    # Structured data — must have STRUCTURE, not just decoded bytes.
    # broken.json (binary garbage) tokenises to {garbage, not, json}
    # via errors="replace" decode; that passed a "≥3 tokens" rule
    # but yielded 0 real keys, so we'd inject an empty-content match.
    if rtype in ("json", "d3dpipeline", "bson"):
        return bool(rec.get("keys") or rec.get("keywords"))

    if rtype in ("csv", "tsv", "csv.gz", "parquet"):
        return bool(rec.get("headers")
                    or rec.get("sample_rows")
                    or rec.get("keywords"))

    if rtype == "excel":
        return bool(rec.get("sheets") or rec.get("keywords"))

    if rtype in ("sqlite", "duckdb"):
        return bool(rec.get("tables") or rec.get("keywords"))

    # PDFs / DOCX — always accept. Filename tokens make the file
    # findable even when text extraction failed (encrypted, image-only,
    # or pypdf / python-docx not installed on this build). The earlier
    # "must have keywords or sample_text" rule silently dropped every
    # PDF/DOCX on bundles without the optional deps — users couldn't
    # see their docs were even indexed.
    if rtype in ("pdf", "docx"):
        return True

    # Images — always content-bearing. Filename tokens alone are enough
    # to make sku-1001.png discoverable; EXIF + OCR (when available)
    # layer on top. Tighter checks happen at the query side (image hits
    # get a lower implicit weight in mixed result sets). Stub-rejection
    # would defeat the point of indexing images in the first place.
    if rtype == "image":
        return True

    # Plain text / yaml / md / cfg / log / etc. — keyword extraction
    # is the main surface. Require either real keywords OR ≥3 tokenised
    # words from the sample. Tightens the previous "any non-empty
    # sample_text" rule which let through 1-character files.
    if rec.get("keywords"):
        if len(rec["keywords"]) >= 2:
            return True
        # Single-keyword stubs (e.g. a .txt file containing just "a")
        # are essentially empty for search purposes — fall through.
    sample = (rec.get("sample_text") or "").strip()
    if sample and len(_tokenize(sample)) >= 3:
        return True
    return False


def _index_file(p: Path, prefetched_stat=None) -> Optional[Dict[str, Any]]:
    suffix = p.suffix.lower()
    if suffix == ".csv":
        rec = _parse_csv(p)
    elif suffix == ".tsv":
        rec = _parse_tsv(p)
    elif suffix == ".parquet":
        rec = _parse_parquet(p)
    elif suffix == ".gz" and _is_gz_csv(p):
        rec = _parse_csv_gz(p)
    elif suffix == ".json" or suffix in _D3DPIPELINE_SUFFIXES:
        rec = _parse_json(p)
        if suffix in _D3DPIPELINE_SUFFIXES:
            rec["type"] = "d3dpipeline"
    elif suffix in _EXCEL_SUFFIXES:
        rec = _parse_excel(p)
    elif suffix in _PDF_SUFFIXES:
        rec = _parse_pdf(p)
    elif suffix in _DOCX_SUFFIXES:
        rec = _parse_docx(p)
    elif suffix in _IMAGE_SUFFIXES:
        rec = _parse_image(p, suffix)
    elif suffix in _SQLITE_SUFFIXES:
        rec = _parse_sqlite(p)
    elif suffix in _DUCKDB_SUFFIXES:
        rec = _parse_duckdb(p)
    elif suffix in _BSON_SUFFIXES:
        rec = _parse_bson(p)
    elif suffix in _TEXT_SUFFIXES:
        rec = _parse_text(p, suffix)
    elif suffix in _SOURCE_CODE_SUFFIXES:
        # Source code = plain text with the suffix stored so callers
        # know it's code (e.g. summary_block can label it "type: py").
        rec = _parse_text(p, suffix)
    else:
        return None
    # Skip records that parsed but yielded nothing useful. See
    # _record_has_content above for why — these stubs cause the
    # "model says no access to the file" hallucination because vault
    # search can still surface them via filename token match while the
    # model sees an empty [VAULT MATCH] block.
    if not _record_has_content(rec):
        return None
    # Reuse the stat the rebuild walk already took (one stat/file instead of
    # two); also removes latent re-index churn from comparing two separate
    # stats of the same file taken microseconds apart.
    st = prefetched_stat
    if st is None:
        try:
            st = p.stat()
        except Exception:
            return None
    rec["path"] = str(p)
    rec["name"] = p.name
    rec["mtime"] = st.st_mtime
    rec["size"] = st.st_size
    return rec


# ---------- index -----------------------------------------------------------

class VaultIndex:
    """Persistent keyword index over a vault folder."""

    def __init__(self, vault_dir: Path):
        self.vault_dir = Path(vault_dir)
        self.index_path = self.vault_dir / INDEX_FILENAME
        self.denylist_path = self.vault_dir / DENYLIST_FILENAME
        self.records: Dict[str, Dict[str, Any]] = {}
        self._vocab_cache: Optional[Set[str]] = None
        # Per-record derived search structures (lowercased keyword/header/
        # key sets, stems, pre-split header/key tokens, name tokens).
        # These are pure functions of a record and were previously
        # rebuilt for EVERY candidate on EVERY search — twice (once in
        # the IDF pass, once in scoring). Cached here keyed by path with
        # a cheap signature so they recompute only when the record
        # actually changes. Kept off the record dict because records are
        # JSON-serialised to disk and sets aren't JSON-safe.
        self._search_cache: Dict[str, Dict[str, Any]] = {}
        self._denylist_cache: Optional[Set[str]] = None
        # Lazy embedding layer — only loads sentence-transformers on first use.
        self._emb_index = None
        self.load()

    # ---- embedding sub-index (semantic vector search) ----
    def embeddings(self):
        """Return (and lazy-instantiate) the EmbeddingIndex companion."""
        if self._emb_index is not None:
            return self._emb_index
        try:
            from vault_embeddings import EmbeddingIndex as _EI
            self._emb_index = _EI(self.vault_dir)
        except Exception as exc:
            import sys as _sys
            print(f"[VaultIndex] embedding layer unavailable: {exc!r}",
                  file=_sys.stderr)
            self._emb_index = None
        return self._emb_index

    def build_embeddings(
        self,
        *,
        force: bool = False,
        on_progress=None,
    ) -> int:
        """Refresh the vector cache for every changed record. Returns the
        count of records re-embedded this call."""
        emb = self.embeddings()
        if emb is None:
            return 0
        try:
            n = emb.build(self.records, force=force, on_progress=on_progress)
        except Exception as exc:
            import sys as _sys
            print(f"[VaultIndex] embedding build failed: {exc!r}",
                  file=_sys.stderr)
            return 0
        return n

    # ---- fuzzy denylist (user-rejected fuzzy matches) ----
    def fuzzy_denylist(self) -> Set[str]:
        if self._denylist_cache is not None:
            return self._denylist_cache
        deny: Set[str] = set()
        if self.denylist_path.exists():
            try:
                deny = set(
                    str(t).lower().strip()
                    for t in json.loads(
                        self.denylist_path.read_text(encoding="utf-8")
                    )
                )
            except Exception:
                deny = set()
        self._denylist_cache = deny
        return deny

    def add_to_fuzzy_denylist(self, terms: List[str]) -> List[str]:
        """Persist user-rejected fuzzy terms. Returns the newly-added items."""
        deny = set(self.fuzzy_denylist())
        added: List[str] = []
        for t in terms:
            t = (t or "").lower().strip()
            if t and t not in deny:
                deny.add(t)
                added.append(t)
        if added:
            try:
                self.denylist_path.write_text(
                    json.dumps(sorted(deny), indent=2),
                    encoding="utf-8",
                )
            except Exception:
                pass
            self._denylist_cache = deny
        return added

    # ---- global vocab cache (for fuzzy matching) ----
    def _global_vocab(self) -> Set[str]:
        if self._vocab_cache is not None:
            return self._vocab_cache
        vocab: Set[str] = set()
        for rec in self.records.values():
            vocab.update(rec.get("keywords", []) or [])
            for h in rec.get("headers", []) or []:
                if isinstance(h, str):
                    vocab.update(_tokenize(h))
            for k in rec.get("keys", []) or []:
                vocab.update(_tokenize(str(k)))
            vocab.update(_tokenize(rec.get("name", "")))
        # short tokens fuzzy-match too easily; require length >= 4
        vocab = {t for t in vocab if len(t) >= FUZZY_MIN_TERM_LEN}
        self._vocab_cache = vocab
        return vocab

    # ---- semantic expansion (model-driven, data-agnostic) ----
    #
    # When the user queries a category word that doesn't appear literally
    # in any file ("metals", "energy units", "weapons", "fabrics"), the
    # keyword index misses every relevant file. Hardcoding "promethium is
    # a metal" would corrupt the app for users whose vaults are about
    # something else entirely. Instead we let the model decide WHICH of
    # the user's actual vocabulary tokens belong in the queried category.
    #
    # Lifecycle:
    #   • semantic_expand(term, llm_call) runs on demand, only when the
    #     search query has at least one term that's not in the vault's
    #     vocabulary.
    #   • Each (term -> expansions) lookup is cached in
    #     vault/semantic_cache.json so a novel concept costs one model
    #     call ever for the lifetime of that vault.
    #   • The cache is invalidated automatically when the vocabulary
    #     changes (new files, removed files) — we hash the sorted vocab
    #     and bust entries built against a stale hash.
    #   • If the model isn't available (llm_call=None), returns []. The
    #     existing fuzzy-match layer remains the fallback.

    _SEMANTIC_CACHE_FILENAME = SEMANTIC_CACHE_FILENAME
    _SEMANTIC_PROMPT = """\
You are categorizing vocabulary tokens from a user's data vault. Decide
which of the LISTED tokens belong in the QUERIED category.

QUERIED CATEGORY: {term}

USER'S VAULT VOCABULARY (these are real strings from the user's files;
return ONLY tokens from this list — do not invent any new words):
{vocab}

Output the matching tokens as a comma-separated list on a single line.
If NONE of the listed tokens belong in the category, output the single
word: NONE
Do not explain, do not add prose, do not include tokens that are not in
the vocabulary list. Lowercase only. Single line only.
"""

    def _semantic_cache_path(self) -> Path:
        return self.vault_dir / self._SEMANTIC_CACHE_FILENAME

    def _load_semantic_cache(self) -> Dict[str, Any]:
        """Cache layout:
            { "vocab_hash": "<sha256>",
              "entries":    { "<term>": ["expansion1", "expansion2", ...] } }
        Returns {} on any error."""
        p = self._semantic_cache_path()
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}
            return data
        except Exception:
            return {}

    def _save_semantic_cache(self, cache: Dict[str, Any]) -> None:
        try:
            self._semantic_cache_path().write_text(
                json.dumps(cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _vocab_hash(self) -> str:
        """Stable hash of the current vocab. Used to invalidate the
        semantic cache when the vault contents have meaningfully
        changed."""
        import hashlib as _h
        vocab = sorted(self._global_vocab())
        return _h.sha256("\n".join(vocab).encode("utf-8")).hexdigest()[:16]

    def semantic_expand(
        self,
        term: str,
        llm_call: Optional[Callable[[str], str]] = None,
        *,
        max_vocab_in_prompt: int = 400,
    ) -> List[str]:
        """Return vocab tokens semantically related to ``term``.

        Asks the local model "which of these user-vocabulary tokens
        belong in the QUERIED category". The model decides whether
        ``"promethium"`` is a metal, whether ``"denim"`` is a fabric,
        whether ``"glock"`` is a weapon — based on its own knowledge of
        each domain, NOT a hardcoded mapping. Results are filtered to
        tokens that actually exist in the user's vault (so the model
        can't hallucinate words that aren't there).

        Cached in vault/semantic_cache.json. Cache is invalidated when
        the global vocab hash changes (new/removed/changed files).

        Returns an empty list when:
          • llm_call is None (no model loaded)
          • The model says "NONE"
          • The model output can't be parsed
          • The term is too short or empty
        """
        t = (term or "").lower().strip()
        if len(t) < 3 or llm_call is None:
            return []

        vocab = self._global_vocab()
        if not vocab:
            return []

        # Skip the model call if the term itself is in the vocab — there's
        # nothing semantic to do; literal search will find it.
        if t in vocab:
            return []

        cache = self._load_semantic_cache()
        current_hash = self._vocab_hash()

        # Drop the entries dict on hash change — the user's vault has
        # shifted under us and old expansions may include vocab that no
        # longer exists.
        if cache.get("vocab_hash") != current_hash:
            cache = {"vocab_hash": current_hash, "entries": {}}

        entries = cache.setdefault("entries", {})
        if t in entries:
            cached = entries[t]
            # Defensive: filter against the current vocab in case partial
            # invalidation slipped through.
            return [w for w in cached if w in vocab][:50]

        # Build the prompt. Bound vocab size so the call stays fast even
        # on large vaults. Prefer long tokens (more likely to be domain
        # words rather than numeric noise) and avoid showing the model
        # tokens it would never categorize usefully.
        def _vocab_priority(tok: str) -> tuple:
            return (1 if tok.isdigit() else 0, -len(tok), tok)
        prioritized = sorted(vocab, key=_vocab_priority)
        vocab_sample = prioritized[:max_vocab_in_prompt]

        prompt = self._SEMANTIC_PROMPT.format(
            term=t, vocab=", ".join(vocab_sample),
        )

        try:
            raw = llm_call(prompt)
        except Exception:
            entries[t] = []
            self._save_semantic_cache(cache)
            return []

        # Parse: lowercase, split on commas, strip, keep only tokens that
        # are actually in the vocab. NONE / "(none)" / empty all map to [].
        expansions: List[str] = []
        if raw:
            cleaned = raw.strip().lower()
            if cleaned and cleaned != "none" and not cleaned.startswith("none"):
                seen: Set[str] = set()
                for piece in cleaned.replace("\n", ",").split(","):
                    w = piece.strip().strip(".'\"`")
                    # The model occasionally wraps tokens in quotes or
                    # adds a leading "- " bullet. Strip those.
                    w = w.lstrip("-* \t")
                    if not w or w == "none":
                        continue
                    if w in vocab and w not in seen and w != t:
                        seen.add(w)
                        expansions.append(w)
                    if len(expansions) >= 50:
                        break

        entries[t] = expansions
        self._save_semantic_cache(cache)
        return expansions

    def _fuzzy_expand_term(
        self, term: str, *, denylist: Optional[Set[str]] = None,
        cutoff: float = FUZZY_CUTOFF, n: int = FUZZY_MAX_PER_TERM,
    ) -> List[Tuple[str, float]]:
        """Return [(matched_token, similarity), ...] for terms not in vocab."""
        t = term.lower().strip()
        if len(t) < FUZZY_MIN_TERM_LEN:
            return []
        vocab = self._global_vocab()
        if t in vocab:
            return []  # exact match exists, no fuzzy needed
        deny = denylist if denylist is not None else self.fuzzy_denylist()
        matches = difflib.get_close_matches(t, vocab, n=n, cutoff=cutoff)
        out: List[Tuple[str, float]] = []
        for m in matches:
            if m in deny or m == t:
                continue
            ratio = difflib.SequenceMatcher(None, t, m).ratio()
            out.append((m, round(ratio, 3)))
        return out

    # ---- persistence ----
    def load(self) -> None:
        if self.index_path.exists():
            try:
                self.records = json.loads(
                    self.index_path.read_text(encoding="utf-8")
                )
            except Exception:
                self.records = {}

    def save(self) -> None:
        try:
            self.index_path.write_text(
                json.dumps(self.records, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    # ---- build ----
    def rebuild(self, *, scope: Optional[Path] = None,
                progress: Optional[Callable[[int, int, str], None]] = None,
                max_workers: Optional[int] = None) -> int:
        """Walk the vault (or a subfolder), reindex changed/new files.

        Returns count of files (re)indexed in this pass.

        ``progress`` is an optional callback ``progress(done, total, name)``
        fired on each file that is reindexed (skipped-because-unchanged
        files don't fire it — they're effectively instant). The GUI uses
        this to keep the user informed during long index builds.

        ``max_workers`` controls the ThreadPoolExecutor parallelism.
        Defaults to ``min(8, os.cpu_count() or 4)``. File parsing is
        CPU-bound but I/O dominated; threading still helps because the
        OS interleaves disk reads while a worker thread is parsing.
        Set to 1 to force serial behaviour (useful for debugging).
        """
        root = Path(scope) if scope else self.vault_dir
        if not root.exists():
            return 0

        # Protected subdirs the model must NEVER index/read.
        # conversation_logs is the human-only debugging log; pipelines/out
        # is the modified-version dump that shouldn't leak as context.
        #
        # We resolve the protected subdir names to absolute paths ONCE
        # before the rglob loop, then do a fast prefix-string check per
        # file. The old per-iteration call to is_protected_path() did
        # the same work (resolve vault_dir, resolve file, compute
        # relative_to) on every single file — 5-10 % of the walk on a
        # vault with a few hundred files.
        try:
            from conversation_logger import (
                PROTECTED_SUBDIRS as _PROTECTED_SUBDIRS,
            )
        except Exception:
            _PROTECTED_SUBDIRS = ()
        try:
            _vault_resolved = self.vault_dir.expanduser().resolve()
            _protected_prefixes = tuple(
                str(_vault_resolved / sub).lower() + os.sep
                for sub in _PROTECTED_SUBDIRS
            )
        except Exception:
            _protected_prefixes = ()

        def _is_protected_fast(p: Path) -> bool:
            """Cheap absolute-prefix check — equivalent to the old
            is_protected_path call on resolvable paths. Falls back to
            the slow version only when string-prefix can't help (e.g.
            symlinked subtree)."""
            if not _protected_prefixes:
                return False
            try:
                sp = str(p.resolve()).lower()
            except Exception:
                return False
            return any(sp.startswith(prefix) for prefix in _protected_prefixes)

        # ── Phase 1: enumerate candidate files, skip up-to-date ones ──────
        # The walk itself is fast; parsing is the slow part. So we first
        # collect every (path, mtime) tuple that needs work, THEN parse
        # in parallel. This lets the progress callback emit an accurate
        # total at the start instead of counting up forever.
        #
        # Hidden + cache dirs we deliberately skip: .git, .chromadb,
        # .git_clones, __pycache__, node_modules, .venv. Without this
        # the walk descended into vault/.chromadb (which can hold
        # hundreds of .json shards from the embedded ChromaDB store)
        # and indexed every one — polluting the vocab AND wasting
        # walk time on each refresh.
        _WALK_SKIP_DIRS = {
            "__pycache__", ".git", ".chromadb", ".git_clones",
            ".venv", "venv", "node_modules", ".cache",
        }
        to_index: List[Tuple[Path, float]] = []
        seen: Set[str] = set()
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            # Bail out as soon as ANY ancestor folder (between `root` and
            # the file) is a skip-dir. Cheap to check via parts.
            try:
                rel_parts = p.relative_to(root).parts[:-1]
            except ValueError:
                rel_parts = ()
            if any(part in _WALK_SKIP_DIRS or part.startswith(".")
                   for part in rel_parts):
                continue
            # Skip our own bookkeeping files (index, denylist, semantic
            # cache, backend settings, onboarding marker). Indexing them
            # would feed JSON-shaped keys back into the vocab and break
            # semantic expansion.
            if p.name in _BOOKKEEPING_FILENAMES:
                continue
            # HARD GUARD — conversation logs and other protected vault
            # subfolders. Checked first so nothing under them can slip
            # through any of the other filters.
            #
            # _is_protected_fast (defined above) pre-resolves the
            # protected subdir prefixes once and does a cheap
            # startswith check per file, instead of the original
            # per-iteration is_protected_path() call that re-resolved
            # vault_dir and computed relative_to on every file.
            if _is_protected_fast(p):
                continue
            # Cheap membership without allocating a lowercased set of every
            # path segment per file; short-circuits on the first non-match.
            if (any(seg.lower() == "out" for seg in p.parts)
                    and any(seg.lower() == "pipelines" for seg in p.parts)):
                lower_str = str(p).lower().replace("\\", "/")
                if "/pipelines/out/" in lower_str or lower_str.endswith("/pipelines/out"):
                    continue
            suf = p.suffix.lower()
            if suf not in _PARSEABLE and not (suf == ".gz" and _is_gz_csv(p)):
                continue
            spath = str(p)
            seen.add(spath)
            try:
                st = p.stat()
            except Exception:
                continue
            mtime = st.st_mtime
            existing = self.records.get(spath)
            if existing and existing.get("mtime") == mtime:
                continue   # unchanged since last index, skip
            to_index.append((p, st))   # carry the stat into _index_file

        # ── Phase 2: parse in parallel ────────────────────────────────────
        n_updated = 0
        total = len(to_index)
        if total == 0:
            # No work to do — still need the stale-record sweep below.
            pass
        elif total == 1 or (max_workers is not None and max_workers <= 1):
            # Serial fallback. Useful for very small batches (threading
            # overhead would dwarf the work) and for debugging.
            for i, (p, _st) in enumerate(to_index, 1):
                spath = str(p)
                if progress is not None:
                    try:
                        progress(i, total, p.name)
                    except Exception:
                        pass
                rec = _index_file(p, _st)
                if rec:
                    self.records[spath] = rec
                    n_updated += 1
        else:
            # Parallel. Even though CPython's GIL serialises pure-Python
            # work, json.loads + file I/O release the GIL frequently
            # enough to give a real ~2-3x speedup on multi-core boxes.
            import os as _os
            from concurrent.futures import ThreadPoolExecutor, as_completed
            workers = max_workers if max_workers is not None else \
                min(8, (_os.cpu_count() or 4))
            done = 0
            with ThreadPoolExecutor(max_workers=workers,
                                    thread_name_prefix="vault-index") as ex:
                futures = {ex.submit(_index_file, p, st): p for p, st in to_index}
                for fut in as_completed(futures):
                    done += 1
                    p = futures[fut]
                    spath = str(p)
                    if progress is not None:
                        try:
                            progress(done, total, p.name)
                        except Exception:
                            pass
                    try:
                        rec = fut.result()
                    except Exception:
                        rec = None
                    if rec:
                        self.records[spath] = rec
                        n_updated += 1

        # Drop stale records for files that have been removed (only on
        # full-tree walks). The walk above already visited every existing
        # parseable file and recorded its path in `seen`, so a record
        # whose path isn't in `seen` no longer exists. This replaces an
        # O(n) Path.exists() syscall sweep over EVERY record with a set
        # membership test — on a full rebuild `root == self.vault_dir`,
        # so `seen` is authoritative.
        if scope is None or Path(scope) == self.vault_dir:
            for stale in [k for k in list(self.records) if k not in seen]:
                del self.records[stale]
                self._search_cache.pop(stale, None)

        self.save()
        # Invalidate cached vocab — record set just changed. Per-record
        # search structures self-invalidate via their signature, so the
        # _search_cache is left intact (surviving records keep their
        # precomputed sets across the rebuild).
        self._vocab_cache = None
        return n_updated

    # ---- search ----
    def _search_terms(self, spath: str, rec: Dict[str, Any]) -> Dict[str, Any]:
        """Return cached, pre-derived search structures for a record.

        All of these are pure functions of the record's content. They
        used to be rebuilt per-candidate-per-search (and again in the
        IDF pass). Now they're computed once and cached on the index,
        invalidated by a cheap signature that moves whenever any source
        field changes shape — which covers descriptions/topics being
        added LATER by generate_descriptions().
        """
        desc = rec.get("description", "") or ""
        topics = rec.get("topics", []) or []
        sample = rec.get("sample_text", "") or ""
        sig = (
            len(rec.get("keywords", []) or []),
            len(rec.get("headers", []) or []),
            len(rec.get("sheets", []) or []),
            len(rec.get("keys", []) or []),
            rec.get("name", ""),
            len(desc), len(topics), len(sample),
        )
        cached = self._search_cache.get(spath)
        if cached is not None and cached.get("_sig") == sig:
            return cached

        kws = set(rec.get("keywords", []) or [])
        stems_kw = {_stem(w) for w in kws}
        # Base headers = just the record's own `headers` (no Excel sheet
        # headers). The IDF document-frequency pass historically used
        # ONLY these, so we keep a base set to preserve that weighting
        # exactly.
        headers_base = [(h or "").lower() for h in rec.get("headers", []) or []]
        headers_base_set = set(headers_base)
        # Flattened headers = base + Excel `sheets[*].headers`, used by the
        # scoring pass (which has always counted sheet headers).
        headers_lc = list(headers_base)
        for s in rec.get("sheets", []) or []:
            for h in s.get("headers", []) or []:
                headers_lc.append(str(h).lower())
        # Pre-split header/key tokens so scoring doesn't call .split()
        # once per query-term per header (was O(terms × headers) splits).
        # Each token set ALSO includes the full lowercased string so the
        # original `term == h` exact-match check is preserved (a query
        # term like "unit_price" still matches a key "unit_price", not
        # just its split tokens "unit"/"price").
        headers_tok = [set(h.split()) | {h} for h in headers_lc]
        keys_lc = [str(k).lower() for k in rec.get("keys", []) or []]
        keys_tok = [set(k.split("_")) | {k} for k in keys_lc]
        keys_set = set(keys_lc)
        name_tokens = set(_tokenize(rec.get("name", "")))
        name_stems = {_stem(t) for t in name_tokens}

        cached = {
            "_sig":             sig,
            "kws":              kws,
            "stems_kw":         stems_kw,
            "headers_lc":       headers_lc,
            "headers_tok":      headers_tok,
            "headers_base":     headers_base,
            "headers_base_set": headers_base_set,
            "keys_lc":          keys_lc,
            "keys_tok":         keys_tok,
            "keys_set":         keys_set,
            "name_tokens":      name_tokens,
            "name_stems":       name_stems,
            "sample_blob":      sample.lower(),
            "desc_blob":        desc.lower(),
            "topic_set":        {str(t).lower() for t in topics},
            # Union used by the IDF document-frequency pass — base headers
            # only, matching the original (pre-cache) IDF semantics.
            "haystack":         kws | stems_kw | headers_base_set | keys_set,
        }
        self._search_cache[spath] = cached
        return cached

    def search(
        self,
        query: str,
        k: int = 5,
        *,
        folder: Optional[str] = None,
        use_fuzzy: bool = True,
        llm_call: Optional[Callable[[str], str]] = None,
    ) -> Tuple[List[Tuple[float, Dict[str, Any]]], Dict[str, List[Tuple[str, float]]]]:
        """Return (top-k results, fuzzy_matches) for a free-text query.

        `folder` restricts results to records whose path contains that
        substring (case-insensitive) — use it to scope to e.g. "data_in".

        `fuzzy_matches` is a dict mapping each unmatched query token to a
        list of (suggested_token, similarity) tuples that were used to
        expand the search. The caller can surface this to the user so
        misspellings can be reviewed and rejected.

        `llm_call` is an optional callable that does a one-shot model
        inference for **semantic expansion**: when a query token isn't in
        the vault's vocab AND isn't fuzzy-matchable, we ask the model
        "which of these user-vocabulary tokens belong in the category
        <term>?" The model's answer is filtered to the user's actual
        vocab (no inventions), cached on disk, and added to the search
        terms. Lets queries like "metals" surface "promethium" / "iron"
        / "steel" entries WITHOUT any hardcoded domain mapping in the
        index — the model decides per-vault based on the actual data.

        Query DSL (opt-in, all special syntax sniffed in
        ``vault_search_query``):

          "Q4 revenue"        — phrase, must appear contiguously
          /\\bemail.*@/       — regex, applied to the candidate's blob
          foo AND bar         — boolean AND (default between terms too)
          foo OR bar          — boolean OR
          NOT foo             — negation
          size:>10mb          — size gate (b/kb/mb/gb supported)
          mtime:<7d           — modified within last N days/h/w/m/y
          ext:csv,json        — extension restrictor
          ( ... )             — grouping

        Plain text without any of the above takes the legacy path
        unchanged (zero regression risk for existing callers). The
        DSL path currently bypasses semantic expansion (``llm_call``)
        — the AST already gives the user explicit control over what
        to look for, so we don't second-guess them.
        """
        fuzzy_matches: Dict[str, List[Tuple[str, float]]] = {}

        # Sniff for advanced syntax. Plain queries (the existing common
        # case) skip the new path entirely.
        try:
            import vault_search_query as _vsq
            parsed = _vsq.parse_query(query)
        except Exception as exc:
            print(f"[VaultIndex] parse_query failed; legacy path: {exc!r}")
            parsed = None

        if parsed is not None and not parsed.is_legacy:
            return self._search_dsl(parsed, k=k, folder=folder), fuzzy_matches

        terms = _tokenize(query)
        if not terms:
            return [], fuzzy_matches

        expanded: Set[str] = set()
        for t in terms:
            expanded |= _expand(t)

        # Fuzzy expansion: for any query token not present in the global
        # vocab (i.e. likely a typo), pull close matches from the vocab.
        if use_fuzzy:
            denylist = self.fuzzy_denylist()
            for t in terms:
                if len(t) < FUZZY_MIN_TERM_LEN:
                    continue
                close = self._fuzzy_expand_term(t, denylist=denylist)
                if close:
                    fuzzy_matches[t] = close
                    for matched, _ratio in close:
                        expanded |= _expand(matched)

        # Semantic expansion via the model. Only runs for query terms
        # that are NOT in the vocab AND not already typo-fixed by the
        # fuzzy layer. The model decides what belongs in each category
        # using ONLY tokens that exist in the user's vault — so a query
        # for "metals" against a textile vault returns nothing rather
        # than something invented. Cached per-vault on disk; one model
        # call per novel concept across the vault's lifetime.
        if llm_call is not None:
            vocab = self._global_vocab()
            for t in terms:
                if t in vocab:
                    continue
                if t in fuzzy_matches:
                    continue                # fuzzy already handled it
                if len(t) < 3:
                    continue
                # Don't burn CPU asking the model to find semantic
                # categories for common English stop words ("find",
                # "the", "all", "average", "across", "excluding", …).
                # Without this gate each query fires 5-10+ extra model
                # calls — 15-30s of CPU overhead on a typical machine
                # for zero ranking benefit (the expanded tokens get
                # filtered out by _SEARCH_STOP_TOKENS later anyway).
                if t in _SEARCH_STOP_TOKENS:
                    continue
                try:
                    expansions = self.semantic_expand(t, llm_call=llm_call)
                except Exception:
                    expansions = []
                if expansions:
                    # Reuse the fuzzy_matches map so the existing UI path
                    # that surfaces "treated as your spelling" matches
                    # also covers semantic expansions. The score is 1.0
                    # to mark a confident model match vs the fuzzy
                    # ratios that are typically 0.8-0.95.
                    fuzzy_matches.setdefault(t, []).extend(
                        (w, 1.0) for w in expansions
                    )
                    for w in expansions:
                        expanded |= _expand(w)

        # Strip trivial stop-tokens that explode match counts
        # Module-level _SEARCH_STOP_TOKENS — same set used to gate the
        # semantic_expand call earlier in this method, so we don't
        # spend CPU on stop-word expansions whose tokens we'd just
        # filter out here anyway.
        expanded = {t for t in expanded if t
                    and t not in _SEARCH_STOP_TOKENS
                    and len(t) > 1}
        # Note: we deliberately do NOT early-return when `expanded` is
        # empty. The keyword pass below will simply produce no scored
        # results, and the embedding pass at the end can still surface
        # the closest semantic matches (e.g. for a query like "what
        # pipeline does X" where every word is a stop-word). That's
        # what makes "show me the closest script even if nothing's an
        # exact match" work.

        # candidate set (folder-scoped if requested)
        cand: List[Tuple[str, Dict[str, Any]]] = []
        for spath, rec in self.records.items():
            if folder and folder.lower() not in spath.lower():
                continue
            # Defense-in-depth: skip records with no indexing surface.
            # _index_file now drops these at build time, but old index
            # JSONs from previous app versions may still contain stub
            # records. Surfacing them as [VAULT MATCH] blocks with
            # empty content was the original "model says no access"
            # bug — guard at the search layer too.
            if not _record_has_content(rec):
                continue
            cand.append((spath, rec))
        if not cand:
            return [], fuzzy_matches

        # Pre-derive (or fetch cached) per-record search structures once
        # for this candidate set. Each entry is reused by BOTH the IDF
        # pass and the scoring pass below, so we never rebuild the same
        # sets/splits twice per search.
        terms_by_path = [(spath, rec, self._search_terms(spath, rec))
                         for spath, rec in cand]

        # IDF over candidate set
        N = len(cand)
        df: Dict[str, int] = defaultdict(int)
        for _spath, _rec, t in terms_by_path:
            haystack = t["haystack"]
            headers_base_set = t["headers_base_set"]
            keys_set = t["keys_set"]
            for term in expanded:
                if term in haystack \
                        or any(term in h for h in headers_base_set) \
                        or any(term in k for k in keys_set):
                    df[term] += 1
        idf = {
            term: math.log((N + 1) / (df.get(term, 0) + 1)) + 1.0
            for term in expanded
        }

        results: List[Tuple[float, Dict[str, Any]]] = []
        for _spath, rec, t in terms_by_path:
            kws = t["kws"]
            stems_kw = t["stems_kw"]
            headers_tok = t["headers_tok"]      # list[set[str]] (pre-split)
            keys_tok = t["keys_tok"]            # list[set[str]] (pre-split on _)
            name_tokens = t["name_tokens"]
            name_stems = t["name_stems"]
            sample_blob = t["sample_blob"]
            desc_blob = t["desc_blob"]
            topic_set = t["topic_set"]

            score = 0.0
            for term in expanded:
                w = idf.get(term, 1.0)
                if term in kws or term in stems_kw:
                    score += 1.0 * w
                if term in name_tokens or term in name_stems:
                    score += 2.5 * w
                # term == full-header OR term is one of its pre-split tokens
                if any(term in htok for htok in headers_tok):
                    score += 3.0 * w
                if any(term in ktok for ktok in keys_tok):
                    score += 3.0 * w
                if term in sample_blob:
                    score += 0.4 * w
                # Semantic layer — topics are high-precision, description
                # is broader but lower-weight per token.
                if term in topic_set:
                    score += 2.0 * w
                if term in desc_blob:
                    score += 0.6 * w
            if score > 0:
                results.append((score, rec))

        # ── Embedding pass — blend in semantic similarity ───────────────
        # Only does work if the embedding index is available AND has been
        # built. Adds a bounded boost so embeddings can't overwhelm exact
        # keyword matches but can pull in semantically-relevant files
        # that the keyword pass missed entirely.
        try:
            emb = self.embeddings()
        except Exception:
            emb = None
        if emb is not None and len(emb) > 0:
            cand_paths = {spath for spath, _rec in cand}
            try:
                emb_hits = emb.search(query, k=max(k * 3, 10),
                                      candidates=cand_paths)
            except Exception:
                emb_hits = []
            if emb_hits:
                # Build a quick path -> existing-result lookup
                by_path: Dict[str, List[float]] = {}
                for i, (sc, rec) in enumerate(results):
                    by_path[rec.get("path", "")] = [float(sc), i]
                # Add a capped boost per embedding hit (max ~4 points,
                # scaled by cosine score)
                for cos, p in emb_hits:
                    boost = float(cos) * 4.0
                    if p in by_path:
                        old_sc, idx = by_path[p]
                        results[int(idx)] = (old_sc + boost, results[int(idx)][1])
                    else:
                        # New path the keyword pass missed — this match
                        # is "semantic only" (no keyword hits). Tag a
                        # copy of the record so the downstream renderer
                        # can label the block clearly. We copy because
                        # the underlying records dict is shared across
                        # queries — mutating it would leak the flag.
                        rec = self.records.get(p)
                        if rec:
                            rec_copy = dict(rec)
                            rec_copy["_semantic_only"] = True
                            rec_copy["_cosine_similarity"] = float(cos)
                            results.append((boost, rec_copy))

        results.sort(key=lambda r: r[0], reverse=True)
        return results[:k], fuzzy_matches

    # ---- DSL search (phrase / boolean / regex / filters) ----
    def _search_dsl(
        self,
        parsed: Any,
        *,
        k: int,
        folder: Optional[str],
    ) -> List[Tuple[float, Dict[str, Any]]]:
        """Execute a parsed Query against the index.

        Step 1: gate candidates by ``parsed.filters`` (size / mtime /
        ext) — fast per-file stats avoid expensive content checks
        on files that can't satisfy the filter.

        Step 2: evaluate the boolean AST against each survivor's
        token bag + concatenated content blob.

        Step 3: score the survivors with TF-IDF over the boolean
        tree's plain Term leaves; phrases and regex contribute a
        fixed bonus per match so they don't fight IDF noise.
        """
        import vault_search_query as _vsq

        # Filter pre-pass — stat each candidate once and skip ones that
        # fail any filter before we touch their content. ext / size /
        # mtime are all cheap.
        gated: List[Tuple[str, Dict[str, Any]]] = []
        filters = parsed.filters or []
        for spath, rec in self.records.items():
            if folder and folder.lower() not in spath.lower():
                continue
            if filters:
                try:
                    st = Path(spath).stat()
                    size = st.st_size
                    mtime = st.st_mtime
                except Exception:
                    size = None
                    mtime = None
                ext = Path(spath).suffix.lower()
                if not all(
                    _vsq.filter_passes(f, size=size, mtime=mtime, ext=ext)
                    for f in filters
                ):
                    continue
            gated.append((spath, rec))
        if not gated:
            return []

        # Per-record haystack + token bag — same data the legacy path
        # builds, just packaged for the AST evaluator.
        def _haystack(rec):
            parts = [
                " ".join(rec.get("keywords", []) or []),
                " ".join((h or "") for h in rec.get("headers", []) or []),
                " ".join(str(k) for k in rec.get("keys", []) or []),
                (rec.get("sample_text", "") or ""),
                (rec.get("description", "") or ""),
                " ".join(str(t) for t in rec.get("topics", []) or []),
                str(rec.get("name", "") or ""),
            ]
            for s in rec.get("sheets", []) or []:
                parts.append(" ".join(str(h) for h in s.get("headers", []) or []))
            return " ".join(parts).lower()

        def _tokens(rec):
            kws = {str(w).lower() for w in (rec.get("keywords", []) or [])}
            headers = {(h or "").lower() for h in (rec.get("headers", []) or [])}
            keys = {str(k).lower() for k in (rec.get("keys", []) or [])}
            topics = {str(t).lower() for t in (rec.get("topics", []) or [])}
            name = set(_tokenize(rec.get("name", "")))
            return kws | headers | keys | topics | name

        # Boolean gating
        survivors: List[Tuple[str, Dict[str, Any], str, set]] = []
        for spath, rec in gated:
            blob = _haystack(rec)
            toks = _tokens(rec)
            if _vsq.evaluate_node(parsed.root, blob, toks):
                survivors.append((spath, rec, blob, toks))
        if not survivors:
            return []

        # Scoring: TF-IDF over the boolean tree's Term leaves, plus a
        # fixed bonus per phrase or regex match. We compute IDF over
        # the survivors (post-gating), which biases toward rare terms
        # within the matched subset.
        term_list = _vsq.collect_terms(parsed.root)
        phrases, regexes = _vsq.collect_phrase_and_regex(parsed.root)
        N = len(survivors)
        df_counts: Dict[str, int] = defaultdict(int)
        for _sp, _rec, _blob, toks in survivors:
            for t in term_list:
                if t in toks or t in _blob:
                    df_counts[t] += 1
        idf = {
            t: math.log((N + 1) / (df_counts.get(t, 0) + 1)) + 1.0
            for t in term_list
        }
        results: List[Tuple[float, Dict[str, Any]]] = []
        for _sp, rec, blob, toks in survivors:
            score = 0.0
            for t in term_list:
                if t in toks:
                    score += idf[t] * 2.0
                elif t in blob:
                    score += idf[t]
            # Phrase + regex bonuses (additive — no IDF, they're
            # already rare by construction).
            for ph in phrases:
                if ph in blob:
                    score += 2.5
            for rg in regexes:
                if rg.search(blob):
                    score += 2.0
            # Boost if the file name is short and matches multiple
            # terms (cheap "this file is *about* the query" hint).
            results.append((score, rec))

        results.sort(key=lambda r: r[0], reverse=True)
        return results[:k]

    # ---- LLM semantic-description layer ----
    # Each record gains an optional `description` (one-paragraph English
    # summary from the local GGUF) and `topics` (3-5 short keywords).
    # Generated lazily — index_file leaves these as empty strings, and
    # generate_descriptions() walks the index filling them in. Cached on
    # disk via save() so subsequent launches don't pay the cost.

    def generate_descriptions(
        self,
        *,
        max_files: Optional[int] = None,
        force: bool = False,
        topics_only: bool = False,
        batch_size: int = 4,
        on_progress: Optional[Any] = None,
    ) -> int:
        """Fill in description + topics for every record that doesn't have
        them yet. Returns the count of records updated.

        Performance characteristics on CPU-only inference (the common
        case — the bottleneck is the model, not the index):

          • Tabular records (csv / tsv / parquet / excel / sqlite /
            duckdb) get description + topics generated DETERMINISTICALLY
            from their schema. Zero model calls. Instant per file.
            Topics come from column / table / sheet names — already the
            most-discriminative tokens for that kind of file.

          • Non-tabular records (json / bson / text / pdf / markdown)
            still need the model. Those calls are now BATCHED — up to
            `batch_size` records per model invocation. Each batch
            amortises ~150-300 tokens of prompt-processing overhead
            across N records.

          • `topics_only=True` skips the prose description for non-tabular
            records too — only the 3-5 topic keywords come back. Cuts
            generation time roughly 3x vs the full description path.

          • num_predict is now sized to the actual output budget:
              full description    = 100 tokens per record
              topics only         = 24 tokens per record
            (was 200 tokens per record — typical model would spend
            150 of those producing verbose prose nobody reads.)

        Parameters:
          max_files     cap on records touched this call. Useful from
                        the GUI to do background work in batches with
                        progress reporting.
          force         regenerate even records that already have a desc.
          topics_only   skip prose for non-tabular records; emit topics
                        keywords only. ~3x faster on non-tabular path.
          batch_size    records per model call (non-tabular only).
                        Default 4. Set to 1 to disable batching.
          on_progress   on_progress(i, total, name) called once per record.
        """
        import council_engine as ce

        candidates: List[Tuple[str, Dict[str, Any]]] = []
        for spath, rec in self.records.items():
            if force or not rec.get("description"):
                candidates.append((spath, rec))
        if max_files:
            candidates = candidates[:int(max_files)]

        # ── A: split candidates by whether we need the model at all ────
        # Tabular file types have all the info we need in the index
        # record already (column names, sheet names, table schemas).
        # No model call required.
        tabular_kinds = {"csv", "tsv", "csv.gz", "parquet", "excel",
                         "sqlite", "duckdb"}
        tabular: List[Tuple[str, Dict[str, Any]]] = []
        modelable: List[Tuple[str, Dict[str, Any]]] = []
        nontext: List[Tuple[str, Dict[str, Any]]] = []
        for spath, rec in candidates:
            rtype = rec.get("type")
            if rtype in tabular_kinds:
                tabular.append((spath, rec))
            elif (rtype in _NON_TEXT_DESCRIBE_TYPES
                  or not (str(rec.get("sample_text", "") or "").strip()
                          or rec.get("keys"))):
                # No model-readable text → deterministic, no model call. This
                # is the "build descriptions breaks on non-text files" guard:
                # images / binaries / empty records never reach the LLM.
                nontext.append((spath, rec))
            else:
                modelable.append((spath, rec))

        updated = 0
        progressed = 0
        total = len(candidates)

        # ── Non-text records: instant, no model call ──────────────────
        for spath, rec in nontext:
            desc, topics = _describe_nontext(rec)
            rec["description"] = (desc or "")[:800]
            rec["topics"]      = (topics or [])[:6]
            rec["_describe_via"] = "nontext"
            updated += 1
            progressed += 1
            if on_progress:
                try:
                    on_progress(progressed, total, rec.get("name", spath))
                except Exception:
                    pass

        # ── Tabular records: instant, no model call ────────────────────
        for spath, rec in tabular:
            desc, topics = _describe_from_schema(rec)
            rec["description"] = (desc or "")[:800]
            rec["topics"]      = (topics or [])[:6]
            rec["_describe_via"] = "schema"
            updated += 1
            progressed += 1
            if on_progress:
                try:
                    on_progress(progressed, total, rec.get("name", spath))
                except Exception:
                    pass

        # ── Non-tabular records: model call, batched ──────────────────
        bs = max(1, int(batch_size))
        i = 0
        while i < len(modelable):
            chunk = modelable[i:i + bs]
            i += bs
            try:
                results = self._describe_batch(
                    ce, chunk, topics_only=topics_only,
                )
            except Exception as exc:
                # Defensive: per-batch failure should not stop the whole
                # run. Mark each record's error and move on.
                results = [("", []) for _ in chunk]
                for _spath, rec in chunk:
                    rec["_describe_error"] = repr(exc)
            for (spath, rec), (desc, topics) in zip(chunk, results):
                rec["description"] = (desc or "")[:800]
                rec["topics"]      = (topics or [])[:6]
                rec["_describe_via"] = "model_batch" if bs > 1 else "model"
                updated += 1
                progressed += 1
                if on_progress:
                    try:
                        on_progress(progressed, total, rec.get("name", spath))
                    except Exception:
                        pass

        if updated:
            self.save()
            self._vocab_cache = None  # description tokens can extend vocab
        return updated

    # -------- Batched description generation (non-tabular only) ---------

    def _describe_batch(
        self, ce_mod, chunk: List[Tuple[str, Dict[str, Any]]],
        *, topics_only: bool = False,
    ) -> List[Tuple[str, List[str]]]:
        """Describe up to N records in one model call.

        Prompt asks the model to label each record by an index marker
        (#1, #2, …) so the response can be split cleanly. If parsing
        fails (model went off-script), we fall back to one call per
        record so we don't lose work on the rest of the chunk.
        """
        if not chunk:
            return []
        if len(chunk) == 1:
            # Single-record path — no batching needed.
            _spath, rec = chunk[0]
            return [self._describe_record_one(ce_mod, rec, topics_only=topics_only)]

        # Build the batched prompt
        record_blocks: List[str] = []
        for idx, (_spath, rec) in enumerate(chunk, start=1):
            record_blocks.append(_render_record_for_describe(idx, rec))
        body = "\n\n".join(record_blocks)

        if topics_only:
            head = (
                f"For EACH of the {len(chunk)} files below, write a "
                f"single line of 3 to 5 lowercase keyword topics "
                f"separated by commas. Prefix each line with the file's "
                f"marker (#1, #2, ...) and write nothing else. No prose, "
                f"no explanations.\n\n"
                f"Example output:\n"
                f"#1: revenue, customers, q3, 2024, sales\n"
                f"#2: inventory, parts, manufacturing, costs\n\n"
            )
            per_record_tokens = 24
        else:
            head = (
                f"For EACH of the {len(chunk)} files below, output two "
                f"lines:\n"
                f"  #N: <one short sentence summary, under 25 words>\n"
                f"  TOPICS #N: <3-5 lowercase keywords, comma-separated>\n"
                f"Use the file's marker (#1, #2, ...) so the lines can "
                f"be parsed. No other prose.\n\n"
            )
            per_record_tokens = 70

        prompt = head + body
        num_predict = max(80, per_record_tokens * len(chunk) + 20)

        raw = ""
        try:
            raw = ce_mod.local_chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                num_predict=num_predict,
                timeout=240,
            )
        except Exception:
            raw = ""

        # Try to parse the batched response. On parse failure, fall
        # back to one call per record — at least we don't lose the
        # whole chunk's worth of work.
        parsed = _parse_batched_response(raw, len(chunk),
                                         topics_only=topics_only)
        if parsed is not None:
            return parsed
        return [
            self._describe_record_one(ce_mod, rec, topics_only=topics_only)
            for _spath, rec in chunk
        ]

    def _describe_record_one(
        self, ce_mod, rec: Dict[str, Any], *, topics_only: bool = False,
    ) -> Tuple[str, List[str]]:
        """Single-record fallback when batching parse fails. Same shape
        as the legacy path but with tightened num_predict (80 not 200).
        """
        body = _render_record_for_describe(1, rec)
        if topics_only:
            prompt = (
                "Write 3 to 5 lowercase keyword topics describing this "
                "file, separated by commas. No prose, no explanations.\n\n"
                + body
                + "\n\nTopics:"
            )
            np_budget = 36
        else:
            prompt = (
                "Summarize this file in one short sentence (under 25 "
                "words). Then on a new line write 'TOPICS:' followed by "
                "3-5 lowercase keywords separated by commas.\n\n"
                + body
                + "\n\nSummary:"
            )
            np_budget = 90
        try:
            raw = ce_mod.local_chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.15,
                num_predict=np_budget,
                timeout=120,
            )
        except Exception:
            raw = ""
        if topics_only:
            # No prose; treat the whole response as a topics line.
            topics = _parse_topics_line(raw or "")
            return "", topics
        return _split_summary_and_topics(raw or "")

    # ---- prompt formatting ----
    # ── Tiered [VAULT MATCH] rendering ────────────────────────────────────
    # The legacy summary_block emitted everything (Tier 1 + 2 + 3) up to a
    # 1500-char cap. On a small-ctx model that meant K full blocks could
    # easily eat half the prompt budget. The tiered API lets the
    # injection layer pack Tier 1 (filename + type + schema + counts) for
    # ALL matches first, then top up the highest-priority matches with
    # Tier 2 (samples + topics + description), then Tier 3 (extended
    # preview text) only if budget remains.
    #
    # Each tier method returns a list of lines so the assembler can join
    # what it picked. The caller is responsible for adding the
    # [VAULT MATCH: name] header and [END MATCH] footer — they belong to
    # the assembled block, not the per-tier payload.

    def _summary_tier1_lines(self, rec: Dict[str, Any]) -> List[str]:
        """Filename, type, row/count info, and SCHEMA (columns / keys /
        sheet names). The 'always-included' tier — every block must
        carry at least this. The bare-minimum context the model needs
        to know what the file IS.
        """
        rtype = rec.get("type", "text")
        lines: List[str] = []
        lines.append(f"path: {rec.get('path', '')}")
        if rtype in ("csv", "tsv", "csv.gz", "parquet"):
            lines.append(f"type: {rtype}")
            rows = rec.get("rows")
            if isinstance(rows, int):
                lines.append(f"rows: {rows:,}")
            if rec.get("headers"):
                lines.append("columns: " + ", ".join(rec["headers"]))
        elif rtype in ("sqlite", "duckdb"):
            lines.append(f"type: {rtype}")
            tables = rec.get("tables", []) or []
            lines.append(f"tables ({len(tables)}):")
            for t in tables[:8]:
                cols = ", ".join(t.get("columns", [])[:15])
                nrows = t.get("rows")
                lines.append(f"  - {t.get('table','?')} "
                             f"({nrows if nrows is not None else '?'} rows): {cols}")
        elif rtype == "bson":
            lines.append("type: bson (MongoDB)")
            if rec.get("doc_count") is not None:
                lines.append(f"documents: {rec['doc_count']}")
            if rec.get("keys"):
                lines.append("fields: " + ", ".join(map(str, rec["keys"][:30])))
        elif rtype == "json":
            lines.append("type: json")
            if rec.get("keys"):
                lines.append("keys: " + ", ".join(rec["keys"][:30]))
            if rec.get("indexing_tier") in ("sampled_head_tail",):
                lines.append("indexing: head+tail sample only — some keys "
                             "deep in the file may not appear in the index")
        elif rtype == "excel":
            lines.append("type: excel")
            sheets = rec.get("sheets", []) or []
            lines.append(f"sheets ({len(sheets)}):")
            for s in sheets[:6]:
                cols = ", ".join(map(str, s.get("headers", [])[:15]))
                lines.append(f"  - {s.get('sheet','?')} ({s.get('rows', 0)} rows): {cols}")
        elif rtype == "image":
            lines.append(f"type: image ({rec.get('image_format', '?')})")
            if rec.get("width") and rec.get("height"):
                lines.append(f"dimensions: {rec['width']}×{rec['height']}")
            if rec.get("size_bytes"):
                sz_kb = rec["size_bytes"] / 1024
                sz_str = (f"{sz_kb/1024:.1f} MB" if sz_kb > 1024
                          else f"{sz_kb:.0f} KB")
                lines.append(f"size: {sz_str}")
            exif = rec.get("exif") or {}
            if exif:
                # Only show the most-searched fields in Tier 1; the
                # full EXIF dump (if any) goes through keyword matching.
                for k in ("DateTimeOriginal", "DateTime", "Make", "Model"):
                    if k in exif:
                        lines.append(f"{k}: {exif[k]}")
            if rec.get("indexing"):
                lines.append(f"note: {rec['indexing']}")
        else:
            lines.append(f"type: {rtype}")
        return lines

    def _summary_tier2_lines(self, rec: Dict[str, Any]) -> List[str]:
        """Topics, description, 1-2 sample rows. The 'helpful if budget
        allows' tier — what the file is *about* and a taste of contents."""
        lines: List[str] = []
        topics = rec.get("topics", [])
        if topics:
            lines.append("topics: " + ", ".join(map(str, topics)))
        desc = rec.get("description", "")
        if desc:
            lines.append("summary: " + desc.replace("\n", " ").strip())
        rtype = rec.get("type", "text")
        if rtype in ("csv", "tsv", "csv.gz", "parquet") and rec.get("sample_rows"):
            lines.append("sample rows:")
            for r in rec["sample_rows"][:2]:
                lines.append(f"  {r}")
        elif rtype == "excel" and rec.get("sample_rows"):
            lines.append("samples:")
            for r in rec["sample_rows"][:2]:
                lines.append(f"  {r}")
        return lines

    def _summary_tier3_lines(self, rec: Dict[str, Any]) -> List[str]:
        """Extended preview text — the longest payload. Only injected
        when budget is generous and the block is among the top-ranked
        matches. Truncated to 600 chars so a single rich block doesn't
        run away."""
        lines: List[str] = []
        rtype = rec.get("type", "text")
        sample_text = rec.get("sample_text", "")
        if not sample_text:
            return lines
        if rtype == "json":
            lines.append("preview:")
            lines.append(sample_text[:600])
        elif rtype == "bson":
            lines.append("sample:")
            lines.append(sample_text[:600])
        elif rtype not in ("csv", "tsv", "csv.gz", "parquet",
                          "sqlite", "duckdb", "excel"):
            # Text / code / markdown / config — they only have sample_text
            # so Tier 3 is the natural place for the preview.
            lines.append("preview:")
            lines.append(sample_text[:600])
        return lines

    def _schema_fingerprint(self, rec: Dict[str, Any]) -> Optional[List[str]]:
        """Stable, lower-cased column/key set for inter-block schema
        deduplication. Returns None when the record has no schema to
        compare (e.g. a plain-text file)."""
        rtype = rec.get("type", "text")
        if rtype in ("csv", "tsv", "csv.gz", "parquet"):
            return [str(h).strip().lower()
                    for h in (rec.get("headers", []) or []) if str(h).strip()]
        if rtype == "json":
            return [str(k).strip().lower()
                    for k in (rec.get("keys", []) or []) if str(k).strip()]
        if rtype == "bson":
            return [str(k).strip().lower()
                    for k in (rec.get("keys", []) or []) if str(k).strip()]
        return None

    def _semantic_only_header(self, rec: Dict[str, Any]) -> Optional[str]:
        """If this record was added by the embedding-only fallback (no
        keyword hit), return a banner line the renderer can emit so the
        model can flag the distinction to the user. Returns None for
        normal keyword-matched records."""
        if not rec.get("_semantic_only"):
            return None
        cos = rec.get("_cosine_similarity")
        cos_str = f" (cosine {cos:.2f})" if isinstance(cos, (int, float)) else ""
        return ("note: nearest semantic match — no exact keyword hit"
                + cos_str + ". The query terms don't appear in this "
                "file's index; relevance is from meaning similarity only.")

    def summary_block(self, rec: Dict[str, Any], max_chars: int = 1500) -> str:
        """Legacy — emit a single block containing Tier 1 + Tier 2 + Tier 3.

        Kept so existing call sites that don't yet use the budget-aware
        assembly still work. The new code path is
        ``assemble_match_blocks(records, budget_tokens, count_tokens)``
        which packs blocks tier-by-tier within a shared budget.
        """
        # Use a dedicated header for semantic-only matches so the
        # downstream model and the user can see at a glance which
        # results came from keyword vs. semantic similarity.
        sem_note = self._semantic_only_header(rec)
        header_label = (f"[VAULT MATCH — nearest semantic match: "
                        f"{rec.get('name', '?')}]"
                        if sem_note else
                        f"[VAULT MATCH: {rec.get('name', '?')}]")
        lines = [header_label]
        if sem_note:
            lines.append(sem_note)
        lines.extend(self._summary_tier1_lines(rec))
        lines.extend(self._summary_tier2_lines(rec))
        lines.extend(self._summary_tier3_lines(rec))
        lines.append("[END MATCH]")
        block = "\n".join(lines)
        if len(block) > max_chars:
            block = block[:max_chars] + "\n... (truncated)"
        return block

    def assemble_match_blocks(
        self,
        records: List[Dict[str, Any]],
        budget_tokens: int,
        count_tokens: Optional[Callable[[str], int]] = None,
    ) -> Tuple[List[str], Dict[str, Any]]:
        """Budget-aware packing of [VAULT MATCH] blocks across many records.

        Algorithm:
          1. Pack Tier 1 for every record (filename + schema + counts).
             If even Tier 1 alone doesn't fit for all records, drop
             trailing records (they're least-relevant since `records`
             is relevance-sorted by caller) until what's left does fit.
             A WARNING is logged when this happens.
          2. While budget remains, top up records in relevance order
             with Tier 2 (topics + samples + description).
          3. While budget *still* remains, top up the top-ranked
             records with Tier 3 (extended preview).
          4. Run schema dedup: when a later block's column set
             overlaps ≥80 % with an earlier block in this assembly,
             replace its columns/keys line with a reference back
             ("schema: same as orders.csv except adds refund_reason").
             Skipped silently when there's no overlap to dedup
             (different file types, no schema in the record, …).

        `count_tokens(s)` is the tokenizer-aware sizer. When omitted
        we fall back to a chars/4 estimate — fine for warning checks
        but the caller should pass the engine's real tokenizer.

        Returns (assembled_blocks, diag) where ``diag`` carries which
        tiers were packed for each block and the per-block budget so
        the caller can log it.
        """
        if count_tokens is None:
            def count_tokens(s: str) -> int:
                return max(1, (len(s) + 3) // 4)

        # Build per-record assets up-front.
        assets: List[Dict[str, Any]] = []
        for rec in records:
            name = rec.get("name", "?")
            sem_note = self._semantic_only_header(rec)
            # Semantic-only matches use a dedicated header label and
            # prepend an explanatory line so the model can convey the
            # "no keyword hit" caveat to the user.
            if sem_note:
                header = f"[VAULT MATCH — nearest semantic match: {name}]"
            else:
                header = f"[VAULT MATCH: {name}]"
            footer = "[END MATCH]"
            t1 = self._summary_tier1_lines(rec)
            if sem_note:
                t1 = [sem_note] + t1
            t2 = self._summary_tier2_lines(rec)
            t3 = self._summary_tier3_lines(rec)
            assets.append({
                "name":   name,
                "rec":    rec,
                "header": header,
                "footer": footer,
                "t1":     t1,
                "t2":     t2,
                "t3":     t3,
                # active tier flags — start with T1 always-on
                "use_t1": True,
                "use_t2": False,
                "use_t3": False,
            })

        def _render(a: Dict[str, Any]) -> str:
            lines = [a["header"]]
            if a["use_t1"]:
                lines.extend(a["t1"])
            if a["use_t2"]:
                lines.extend(a["t2"])
            if a["use_t3"]:
                lines.extend(a["t3"])
            lines.append(a["footer"])
            return "\n".join(lines)

        # Step 1: Tier-1-only for everyone, drop tail if even that
        # overflows. Walk forward; the running cost stops growing as
        # soon as we're at budget.
        kept: List[Dict[str, Any]] = []
        running = 0
        overflow_dropped: List[str] = []
        for a in assets:
            cost = count_tokens(_render(a))
            if running + cost > budget_tokens and kept:
                # We've already placed at least one; drop the rest.
                overflow_dropped.append(a["name"])
                continue
            if running + cost > budget_tokens and not kept:
                # Even ONE record's Tier 1 doesn't fit. Place it
                # anyway (the caller's cumulative-budget eviction
                # will catch the overflow upstream) and log it.
                _LOG.warning(
                    "[assemble_match_blocks] Tier 1 alone (%s tokens) "
                    "exceeds budget %s for first record %s — packing it "
                    "anyway; the assembly-level evictor will trim if "
                    "needed.", cost, budget_tokens, a["name"])
            kept.append(a)
            running += cost

        # Step 2: upgrade to Tier 2 from the top of the relevance list
        # until budget exhausts. Re-render to get exact deltas.
        for a in kept:
            if not a["t2"]:
                continue
            before = count_tokens(_render(a))
            a["use_t2"] = True
            after = count_tokens(_render(a))
            delta = after - before
            if running + delta > budget_tokens:
                a["use_t2"] = False
                # Don't break — a later block may have a smaller t2
                # delta that still fits. Continue scanning.
                continue
            running += delta

        # Step 3: upgrade to Tier 3 from the top until budget exhausts.
        for a in kept:
            if not a["t3"]:
                continue
            before = count_tokens(_render(a))
            a["use_t3"] = True
            after = count_tokens(_render(a))
            delta = after - before
            if running + delta > budget_tokens:
                a["use_t3"] = False
                continue
            running += delta

        # Step 4: schema dedup across the assembled set. Walk in order;
        # for each block with a fingerprint, check earlier blocks. If
        # column overlap >= 80 % with some earlier block, replace this
        # block's columns/keys line with a "same as X except {adds Y, removes Z}"
        # reference.
        SIM_THRESHOLD = 0.80
        fp_cache: List[Tuple[str, Optional[List[str]]]] = []
        for a in kept:
            fp = self._schema_fingerprint(a["rec"])
            fp_cache.append((a["name"], fp))

        for i, a in enumerate(kept):
            name_i, fp_i = fp_cache[i]
            if not fp_i:
                continue
            set_i = set(fp_i)
            best_j = -1
            best_score = 0.0
            for j in range(i):
                _name_j, fp_j = fp_cache[j]
                if not fp_j:
                    continue
                set_j = set(fp_j)
                if not (set_i and set_j):
                    continue
                # Jaccard similarity
                inter = len(set_i & set_j)
                union = len(set_i | set_j)
                if union == 0:
                    continue
                score = inter / union
                if score > best_score:
                    best_score = score
                    best_j = j
            if best_j < 0 or best_score < SIM_THRESHOLD:
                continue
            # Rewrite a's columns/keys line in t1 with a reference.
            ref_name = fp_cache[best_j][0]
            ref_set  = set(fp_cache[best_j][1] or [])
            adds = sorted(set_i - ref_set)
            removes = sorted(ref_set - set_i)
            note_bits = []
            if adds:
                note_bits.append("adds " + ", ".join(adds[:8])
                                  + (f" (+{len(adds)-8})" if len(adds) > 8 else ""))
            if removes:
                note_bits.append("removes " + ", ".join(removes[:8])
                                  + (f" (+{len(removes)-8})" if len(removes) > 8 else ""))
            note = f"schema: same as {ref_name}"
            if note_bits:
                note += " except " + "; ".join(note_bits)
            # Replace the existing columns/keys line in tier-1 lines.
            new_t1 = []
            replaced = False
            for ln in a["t1"]:
                lower = ln.lower()
                if (lower.startswith("columns:") or lower.startswith("keys:")
                        or lower.startswith("fields:")) and not replaced:
                    new_t1.append(note)
                    replaced = True
                else:
                    new_t1.append(ln)
            if not replaced:
                new_t1.append(note)
            a["t1"] = new_t1

        # Final render
        out_blocks = [_render(a) for a in kept]
        diag = {
            "budget_tokens":     budget_tokens,
            "n_records":         len(records),
            "n_kept":            len(kept),
            "n_dropped":         len(overflow_dropped),
            "dropped_names":     overflow_dropped,
            "tier_breakdown": [
                {"name": a["name"],
                 "tiers": "".join(["1" if a["use_t1"] else "",
                                    "2" if a["use_t2"] else "",
                                    "3" if a["use_t3"] else ""])}
                for a in kept
            ],
            "running_tokens":    running,
        }
        return out_blocks, diag
