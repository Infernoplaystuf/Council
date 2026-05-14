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
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

INDEX_FILENAME = "vault_index.json"
DENYLIST_FILENAME = "fuzzy_denylist.json"

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
    out: Set[str] = {t, _stem(t)}
    # Look up both raw and stemmed forms
    for w in (t, _stem(t)):
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


_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_HEX_RE = re.compile(r"^[0-9a-f]+$", re.IGNORECASE)


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
    return [t.lower() for t in _TOKEN_RE.findall(text or "")
            if _is_useful_token(t.lower())]


# ---------- file parsers ----------------------------------------------------

_TEXT_SUFFIXES = {
    ".txt", ".md", ".log", ".rst", ".yaml", ".yml",
    ".ini", ".cfg", ".toml", ".html", ".htm",
}
_EXCEL_SUFFIXES = {".xlsx", ".xls", ".xlsm"}
_TABULAR_EXTRA  = {".tsv", ".parquet"}     # parquet needs pyarrow at runtime
# SQLite databases (.db / .sqlite / .sqlite3) — indexed at the table level
_SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
_PARSEABLE = ({".csv", ".json"} | _TEXT_SUFFIXES | _EXCEL_SUFFIXES
              | _TABULAR_EXTRA | _SQLITE_SUFFIXES)


def _is_gz_csv(p: Path) -> bool:
    """`.csv.gz` style: .gz with a sibling implication that it's a CSV."""
    return p.suffix.lower() == ".gz" and p.stem.lower().endswith(".csv")


def _parse_csv(p: Path) -> Dict[str, Any]:
    headers: List[str] = []
    sample_rows: List[str] = []
    keywords: Set[str] = set()
    try:
        with open(p, newline="", encoding="utf-8", errors="replace") as fh:
            reader = csv.reader(fh)
            for i, row in enumerate(reader):
                if i == 0:
                    headers = [c.strip() for c in row]
                    keywords.update(_tokenize(" ".join(headers)))
                    continue
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
                        for part in re.split(r"[|;,/]", cv):
                            part = part.strip()
                            if 2 <= len(part) <= 80:
                                keywords.update(_tokenize(part))
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
        "keywords": sorted(keywords)[:5000],
    }


def _parse_json(p: Path) -> Dict[str, Any]:
    keys: Set[str] = set()
    string_values: Set[str] = set()
    sample_text = ""
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
        sample_text = text[:2000]
        try:
            data = json.loads(text)
        except Exception:
            data = None

        def walk(node: Any, depth: int = 0) -> None:
            if depth > 5:
                return
            if isinstance(node, dict):
                for k, v in node.items():
                    keys.add(str(k))
                    walk(v, depth + 1)
            elif isinstance(node, list):
                for item in node[:25]:
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
    keywords.update(_tokenize(" ".join(list(string_values)[:200])))

    return {
        "type": "json",
        "keys": sorted(keys)[:120],
        "sample_text": sample_text[:1200],
        "keywords": sorted(keywords)[:300],
    }


def _parse_excel(p: Path) -> Dict[str, Any]:
    """Index an Excel workbook — sheet names + per-sheet headers + a few
    sample values. Each sheet's content contributes to the keyword set."""
    sheets_meta: List[Dict[str, Any]] = []
    sample_rows: List[str] = []
    keywords: Set[str] = set()
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
                    for part in re.split(r"[|;,/]", cv):
                        part = part.strip()
                        if 2 <= len(part) <= 80:
                            keywords.update(_tokenize(part))
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
        "keywords":    sorted(keywords)[:5000],
    }


def _parse_tabular_df(p: Path, df, *, kind: str) -> Dict[str, Any]:
    """Shared CSV/TSV/Parquet/gz indexing — takes a loaded DataFrame and
    extracts headers, sample rows, and keyword tokens with the same rules
    as _parse_csv (URL skip, multi-value split, hex filter)."""
    headers: List[str] = [str(c).strip() for c in df.columns]
    sample_rows: List[str] = []
    keywords: Set[str] = set(_tokenize(" ".join(headers)))
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
            for part in re.split(r"[|;,/]", cv):
                part = part.strip()
                if 2 <= len(part) <= 80:
                    keywords.update(_tokenize(part))
        if len(keywords) > 8000:
            break
    return {
        "type":        kind,
        "headers":     headers,
        "sample_rows": sample_rows,
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


def _index_file(p: Path) -> Optional[Dict[str, Any]]:
    suffix = p.suffix.lower()
    if suffix == ".csv":
        rec = _parse_csv(p)
    elif suffix == ".tsv":
        rec = _parse_tsv(p)
    elif suffix == ".parquet":
        rec = _parse_parquet(p)
    elif suffix == ".gz" and _is_gz_csv(p):
        rec = _parse_csv_gz(p)
    elif suffix == ".json":
        rec = _parse_json(p)
    elif suffix in _EXCEL_SUFFIXES:
        rec = _parse_excel(p)
    elif suffix in _SQLITE_SUFFIXES:
        rec = _parse_sqlite(p)
    elif suffix in _TEXT_SUFFIXES:
        rec = _parse_text(p, suffix)
    else:
        return None
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
    def rebuild(self, *, scope: Optional[Path] = None) -> int:
        """Walk the vault (or a subfolder), reindex changed/new files.

        Returns count of files (re)indexed in this pass.
        """
        root = Path(scope) if scope else self.vault_dir
        if not root.exists():
            return 0

        seen: Set[str] = set()
        n_updated = 0

        # Protected subdirs the model must NEVER index/read.
        # conversation_logs is the human-only debugging log; pipelines/out
        # is the modified-version dump that shouldn't leak as context.
        try:
            from conversation_logger import is_protected_path as _is_protected
        except Exception:
            _is_protected = lambda *_a, **_k: False

        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if p.name == INDEX_FILENAME:
                continue
            # HARD GUARD — conversation logs and other protected vault
            # subfolders. Checked first so nothing under them can slip
            # through any of the other filters.
            if _is_protected(p, self.vault_dir):
                continue
            # Skip pipelines/out/ — modified pipeline versions live there
            # and must not leak back into the model's context.
            parts = {part.lower() for part in p.parts}
            if "out" in parts and "pipelines" in parts:
                lower_str = str(p).lower().replace("\\", "/")
                if "/pipelines/out/" in lower_str or lower_str.endswith("/pipelines/out"):
                    continue
            suf = p.suffix.lower()
            if suf not in _PARSEABLE and not (suf == ".gz" and _is_gz_csv(p)):
                continue
            spath = str(p)
            seen.add(spath)
            try:
                mtime = p.stat().st_mtime
            except Exception:
                continue
            existing = self.records.get(spath)
            if existing and existing.get("mtime") == mtime:
                continue
            rec = _index_file(p)
            if rec:
                self.records[spath] = rec
                n_updated += 1

        # Drop stale records for files that have been removed (only on full-tree walks)
        if scope is None or Path(scope) == self.vault_dir:
            for stale in [k for k in list(self.records)
                          if not Path(k).exists()]:
                del self.records[stale]

        self.save()
        # Invalidate cached vocab — record set just changed.
        self._vocab_cache = None
        return n_updated

    # ---- search ----
    def search(
        self,
        query: str,
        k: int = 5,
        *,
        folder: Optional[str] = None,
        use_fuzzy: bool = True,
    ) -> Tuple[List[Tuple[float, Dict[str, Any]]], Dict[str, List[Tuple[str, float]]]]:
        """Return (top-k results, fuzzy_matches) for a free-text query.

        `folder` restricts results to records whose path contains that
        substring (case-insensitive) — use it to scope to e.g. "data_in".

        `fuzzy_matches` is a dict mapping each unmatched query token to a
        list of (suggested_token, similarity) tuples that were used to
        expand the search. The caller can surface this to the user so
        misspellings can be reviewed and rejected.
        """
        terms = _tokenize(query)
        fuzzy_matches: Dict[str, List[Tuple[str, float]]] = {}
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

        # Strip trivial stop-tokens that explode match counts
        _stop = {"the", "a", "an", "of", "in", "on", "for", "to", "is", "are",
                 "with", "and", "or", "any", "all", "every", "this", "that",
                 "what", "which", "find", "show", "list", "look", "through",
                 "files", "file", "folder", "data", "csv", "json"}
        expanded = {t for t in expanded if t and t not in _stop and len(t) > 1}
        if not expanded:
            return [], fuzzy_matches

        # candidate set (folder-scoped if requested)
        cand: List[Tuple[str, Dict[str, Any]]] = []
        for spath, rec in self.records.items():
            if folder and folder.lower() not in spath.lower():
                continue
            cand.append((spath, rec))
        if not cand:
            return [], fuzzy_matches

        # IDF over candidate set
        N = len(cand)
        df: Dict[str, int] = defaultdict(int)
        for _spath, rec in cand:
            kws = set(rec.get("keywords", []))
            stems_kw = {_stem(w) for w in kws}
            headers_lc = {(h or "").lower() for h in rec.get("headers", [])}
            keys_lc = {str(k).lower() for k in rec.get("keys", [])}
            haystack = kws | stems_kw | headers_lc | keys_lc
            for term in expanded:
                if term in haystack or any(term in h for h in headers_lc) \
                        or any(term in k for k in keys_lc):
                    df[term] += 1
        idf = {
            term: math.log((N + 1) / (df.get(term, 0) + 1)) + 1.0
            for term in expanded
        }

        results: List[Tuple[float, Dict[str, Any]]] = []
        for _spath, rec in cand:
            kws = set(rec.get("keywords", []))
            stems_kw = {_stem(w) for w in kws}
            # Flatten CSV `headers` and Excel `sheets[*].headers` into one list
            headers_lc = [(h or "").lower() for h in rec.get("headers", [])]
            for s in rec.get("sheets", []) or []:
                for h in s.get("headers", []) or []:
                    headers_lc.append(str(h).lower())
            keys_lc = [str(k).lower() for k in rec.get("keys", [])]
            name_tokens = set(_tokenize(rec.get("name", "")))
            name_stems = {_stem(t) for t in name_tokens}
            sample_blob = (rec.get("sample_text", "") or "").lower()
            # LLM-generated layer (may be empty if generate_descriptions
            # hasn't run yet)
            desc_blob = (rec.get("description", "") or "").lower()
            topic_set = {str(t).lower() for t in rec.get("topics", []) or []}

            score = 0.0
            for term in expanded:
                w = idf.get(term, 1.0)
                if term in kws or term in stems_kw:
                    score += 1.0 * w
                if term in name_tokens or term in name_stems:
                    score += 2.5 * w
                if any(term == h or term in h.split() for h in headers_lc):
                    score += 3.0 * w
                if any(term == k or term in k.split("_") for k in keys_lc):
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
                        # New path the keyword pass missed
                        rec = self.records.get(p)
                        if rec:
                            results.append((boost, rec))

        results.sort(key=lambda r: r[0], reverse=True)
        return results[:k], fuzzy_matches

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
        on_progress: Optional[Any] = None,
    ) -> int:
        """Fill in description + topics for every record that doesn't have
        them yet. Returns the count of records updated.

        - max_files caps how many we touch this call (useful from the GUI
          to do background work in small batches).
        - force=True regenerates even records that already have a desc.
        - on_progress(i, total, name) callable runs once per record.
        """
        import council_engine as ce

        candidates: List[Tuple[str, Dict[str, Any]]] = []
        for spath, rec in self.records.items():
            if force or not rec.get("description"):
                candidates.append((spath, rec))
        if max_files:
            candidates = candidates[:int(max_files)]

        updated = 0
        for i, (spath, rec) in enumerate(candidates, start=1):
            try:
                desc, topics = self._describe_record(ce, rec)
            except Exception as exc:
                desc = ""
                topics = []
                rec["_describe_error"] = repr(exc)
            rec["description"] = (desc or "")[:800]
            rec["topics"] = topics[:6]
            updated += 1
            if on_progress:
                try:
                    on_progress(i, len(candidates), rec.get("name", spath))
                except Exception:
                    pass
        if updated:
            self.save()
            self._vocab_cache = None  # description tokens can extend vocab
        return updated

    def _describe_record(self, ce_mod, rec: Dict[str, Any]) -> Tuple[str, List[str]]:
        """Build the prompt for one record and call the local model."""
        name = rec.get("name", "?")
        rtype = rec.get("type", "text")

        body_lines: List[str] = [f"File: {name}", f"Type: {rtype}"]
        if rtype == "csv":
            headers = rec.get("headers", []) or []
            body_lines.append("Columns: " + ", ".join(map(str, headers[:30])))
            for row in (rec.get("sample_rows", []) or [])[:3]:
                body_lines.append("  sample: " + str(row)[:200])
        elif rtype == "json":
            keys = rec.get("keys", []) or []
            body_lines.append("Top-level keys: " + ", ".join(map(str, keys[:30])))
            preview = (rec.get("sample_text", "") or "")[:600]
            if preview:
                body_lines.append("Sample:")
                body_lines.append(preview)
        else:
            preview = (rec.get("sample_text", "") or "")[:600]
            if preview:
                body_lines.append("Sample:")
                body_lines.append(preview)

        prompt = (
            "Summarize this file in one short paragraph (1-3 sentences). "
            "Focus on what the file represents and what could be answered "
            "from it. Then on a new line write 'TOPICS:' followed by "
            "3-5 lowercase keywords separated by commas.\n\n"
            + "\n".join(body_lines)
            + "\n\nSummary:"
        )
        raw = ce_mod.local_chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.15,
            num_predict=200,
            timeout=120,
        )
        return _split_summary_and_topics(raw or "")

    # ---- prompt formatting ----
    def summary_block(self, rec: Dict[str, Any], max_chars: int = 1500) -> str:
        """Format a record as a [VAULT MATCH] block for prompt injection."""
        lines = [
            f"[VAULT MATCH: {rec.get('name', '?')}]",
            f"path: {rec.get('path', '')}",
        ]
        desc = rec.get("description", "")
        if desc:
            lines.append("summary: " + desc.replace("\n", " ").strip())
        topics = rec.get("topics", [])
        if topics:
            lines.append("topics: " + ", ".join(map(str, topics)))
        rtype = rec.get("type", "text")
        if rtype in ("csv", "tsv", "csv.gz", "parquet"):
            lines.append(f"type: {rtype}")
            if rec.get("headers"):
                lines.append("columns: " + ", ".join(rec["headers"]))
            if rec.get("sample_rows"):
                lines.append("sample rows:")
                lines.extend(rec["sample_rows"][:3])
        elif rtype == "sqlite":
            lines.append("type: sqlite")
            tables = rec.get("tables", []) or []
            lines.append(f"tables ({len(tables)}):")
            for t in tables[:8]:
                cols = ", ".join(t.get("columns", [])[:15])
                nrows = t.get("rows")
                lines.append(f"  - {t.get('table','?')} "
                             f"({nrows if nrows is not None else '?'} rows): {cols}")
        elif rtype == "json":
            lines.append("type: json")
            if rec.get("keys"):
                lines.append("keys: " + ", ".join(rec["keys"][:30]))
            if rec.get("sample_text"):
                lines.append("preview:")
                lines.append(rec["sample_text"][:600])
        elif rtype == "excel":
            lines.append("type: excel")
            sheets = rec.get("sheets", []) or []
            lines.append(f"sheets ({len(sheets)}):")
            for s in sheets[:6]:
                cols = ", ".join(map(str, s.get("headers", [])[:15]))
                lines.append(f"  - {s.get('sheet','?')} ({s.get('rows', 0)} rows): {cols}")
            if rec.get("sample_rows"):
                lines.append("samples:")
                for r in rec["sample_rows"][:3]:
                    lines.append(f"  {r}")
        else:
            lines.append(f"type: {rtype}")
            if rec.get("sample_text"):
                lines.append("preview:")
                lines.append(rec["sample_text"][:600])
        lines.append("[END MATCH]")
        block = "\n".join(lines)
        if len(block) > max_chars:
            block = block[:max_chars] + "\n... (truncated)"
        return block
