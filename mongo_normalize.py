"""
mongo_normalize.py — turn MongoDB BSON / JSON into model-digestible form.

MongoDB documents are deeply nested and carry types a language model reads
poorly: ``ObjectId('66f0…')``, raw ``datetime`` objects, ``Decimal128``,
binary blobs, and arrays of sub-documents that show up in a cell as
``[{'sku': 'A', 'qty': 2}, {'sku': 'B', 'qty': 1}]``. Feeding that to a
model wastes tokens and invites misreads.

This module flattens and coerces documents into clean, bounded scalars:

  • ObjectId / UUID / Decimal128 / Int64 / Binary / Regex  -> readable str
  • datetime / date / Timestamp                            -> ISO-8601 str
  • nested dicts                                           -> dotted keys
  • arrays of scalars                                      -> "a; b; c"
  • arrays of objects                                      -> compact JSON
                                                              (bounded) or
                                                              "[N items]"

Crucially it handles BOTH representations of those types:
  1. native ``bson`` Python objects (from a live collection / .bson file), and
  2. MongoDB *Extended JSON* wrappers (``{"$oid": "…"}``, ``{"$date": …}``,
     ``{"$numberDecimal": "…"}`` …) found in exported ``.json`` / ``.jsonl``.

So it works whether the source is pymongo, a .bson dump, or a JSON export —
and it needs NO bson import to handle the Extended-JSON case.

Pure functions (no file or network I/O); pandas is imported lazily only by
the to-frame helpers, so the coercion/flatten/schema logic stays testable
without pandas. File reading + sandbox wiring live in vault_analyst.
"""
from __future__ import annotations

import base64
import json
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional

# Default bounds — keep a single document's contribution to a prompt small.
DEFAULT_MAX_ARRAY_ITEMS = 25      # scalar-array elements to keep before "(+N)"
DEFAULT_MAX_ARRAY_CHARS = 300     # object-array JSON length before "[N items]"
DEFAULT_MAX_DEPTH = 12            # guard against pathological / cyclic nesting


# ============================================================
# Scalar coercion (handles native bson AND Extended JSON)
# ============================================================

# Extended-JSON wrapper keys -> how to render. A dict with exactly one of
# these keys is a typed scalar, not a real sub-document.
def _coerce_extended_json(d: Dict[str, Any]) -> Any:
    """If ``d`` is a MongoDB Extended-JSON scalar wrapper, return the
    coerced scalar; otherwise return the sentinel ``_NOT_WRAPPER``."""
    if len(d) == 1:
        (k, v), = d.items()
        if k == "$oid":
            return str(v)
        if k == "$date":
            # Three forms in the wild:
            #   relaxed   : {"$date": "2024-03-15T12:30:00Z"}  (ISO string)
            #   relaxed   : {"$date": 1710505800000}           (epoch-ms int)
            #   canonical : {"$date": {"$numberLong": "1710505800000"}}
            #               (what modern `mongoexport` emits by default)
            if isinstance(v, dict) and "$numberLong" in v:
                try:
                    v = int(v["$numberLong"])
                except (TypeError, ValueError):
                    return str(v)
            if isinstance(v, (int, float)):
                try:
                    return datetime.utcfromtimestamp(v / 1000.0).isoformat() + "Z"
                except (OverflowError, OSError, ValueError):
                    return str(v)
            return str(v)
        if k == "$numberLong" or k == "$numberInt":
            try:
                return int(v)
            except (TypeError, ValueError):
                return str(v)
        if k == "$numberDouble":
            try:
                return float(v)
            except (TypeError, ValueError):
                return str(v)
        if k == "$numberDecimal":
            try:
                return float(v)
            except (TypeError, ValueError):
                return str(v)
        if k == "$binary":
            # {"$binary": {"base64": "...", "subType": "00"}} (v2) or legacy
            return _summarize_binary(v)
        if k in ("$regularExpression", "$regex"):
            if isinstance(v, dict):
                return v.get("pattern", "") or json.dumps(v)
            return str(v)
        if k == "$timestamp":
            if isinstance(v, dict):
                return f"Timestamp({v.get('t')},{v.get('i')})"
            return str(v)
        if k == "$undefined":
            return None
    # {"$date": ...} handled; two-key {"$binary": {...}} also handled above.
    return _NOT_WRAPPER


_NOT_WRAPPER = object()


def _summarize_binary(v: Any) -> str:
    """Render binary as a short marker, never the raw bytes."""
    try:
        if isinstance(v, dict) and "base64" in v:
            raw = base64.b64decode(v.get("base64", "") or "")
            return f"<binary {len(raw)} bytes>"
        if isinstance(v, (bytes, bytearray, memoryview)):
            return f"<binary {len(bytes(v))} bytes>"
    except Exception:
        pass
    return "<binary>"


def coerce_value(value: Any, _depth: int = 0) -> Any:
    """Recursively convert a value into JSON-/model-friendly Python.

    Native bson objects are detected by type-name (no hard bson import) so
    this works on a CPU-only bundle that never installed pymongo.
    """
    # Primitives pass through.
    if value is None or isinstance(value, (bool, int, float, str)):
        return value

    # datetime / date -> ISO-8601.
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()

    # bytes -> marker.
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary {len(bytes(value))} bytes>"

    # Native bson / library objects, identified by class name so we don't
    # need to import bson / uuid / decimal here.
    tname = type(value).__name__
    if tname == "ObjectId":
        return str(value)
    if tname in ("Decimal128", "Decimal"):
        try:
            return float(str(value))
        except (ValueError, TypeError):
            return str(value)
    if tname == "Int64":
        try:
            return int(value)
        except (ValueError, TypeError):
            return str(value)
    if tname == "UUID":
        return str(value)
    if tname in ("Binary", "Timestamp", "Regex", "Code", "DBRef", "MinKey",
                 "MaxKey"):
        if tname == "Binary":
            return f"<binary {len(bytes(value))} bytes>" \
                if hasattr(value, "__len__") else "<binary>"
        return str(value)

    if _depth >= DEFAULT_MAX_DEPTH:
        return str(value)

    # dict -> maybe an Extended-JSON scalar wrapper, else recurse.
    if isinstance(value, dict):
        wrapped = _coerce_extended_json(value)
        if wrapped is not _NOT_WRAPPER:
            return wrapped
        return {str(k): coerce_value(v, _depth + 1) for k, v in value.items()}

    # list / tuple / set -> list of coerced items.
    if isinstance(value, (list, tuple, set)):
        return [coerce_value(v, _depth + 1) for v in value]

    # Anything else (custom object) -> its string form.
    return str(value)


# ============================================================
# Flatten one coerced document into scalar columns
# ============================================================

def _render_array(arr: List[Any],
                  max_items: int,
                  max_chars: int) -> Any:
    """Collapse a coerced array into one bounded, readable cell."""
    if not arr:
        return ""
    all_scalar = all(
        x is None or isinstance(x, (bool, int, float, str)) for x in arr
    )
    if all_scalar:
        shown = [("" if x is None else str(x)) for x in arr[:max_items]]
        extra = len(arr) - len(shown)
        joined = "; ".join(shown)
        return joined + (f" (+{extra} more)" if extra > 0 else "")
    # Array of objects / nested arrays: compact JSON, bounded.
    try:
        compact = json.dumps(arr, ensure_ascii=False, separators=(",", ":"),
                             default=str)
    except (TypeError, ValueError):
        compact = str(arr)
    if len(compact) <= max_chars:
        return compact
    return f"[{len(arr)} items]"


def flatten_document(doc: Dict[str, Any], *,
                     sep: str = ".",
                     max_array_items: int = DEFAULT_MAX_ARRAY_ITEMS,
                     max_array_chars: int = DEFAULT_MAX_ARRAY_CHARS,
                     ) -> Dict[str, Any]:
    """Coerce + flatten ONE document into a flat ``{dotted_key: scalar}``.

    Nested dicts become dotted keys (``addr.city``); arrays collapse to a
    single bounded cell. Always coerces BSON / Extended-JSON scalars first.
    """
    coerced = coerce_value(doc)
    if not isinstance(coerced, dict):
        return {"value": coerced}
    if not coerced:
        return {}                 # empty document -> empty row, not {"": "{}"}
    out: Dict[str, Any] = {}

    def _walk(prefix: str, val: Any) -> None:
        if isinstance(val, dict):
            if not val:
                out[prefix] = "{}"
                return
            for k, v in val.items():
                key = f"{prefix}{sep}{k}" if prefix else str(k)
                _walk(key, v)
        elif isinstance(val, list):
            out[prefix] = _render_array(val, max_array_items, max_array_chars)
        else:
            out[prefix] = val

    _walk("", coerced)
    return out


def normalize_documents(docs: Iterable[Dict[str, Any]], *,
                        sep: str = ".",
                        max_array_items: int = DEFAULT_MAX_ARRAY_ITEMS,
                        max_array_chars: int = DEFAULT_MAX_ARRAY_CHARS,
                        ) -> List[Dict[str, Any]]:
    """Flatten + coerce a sequence of documents into flat row-dicts."""
    rows: List[Dict[str, Any]] = []
    for d in docs:
        if isinstance(d, dict):
            rows.append(flatten_document(
                d, sep=sep, max_array_items=max_array_items,
                max_array_chars=max_array_chars))
        else:
            rows.append({"value": coerce_value(d)})
    return rows


# ============================================================
# Schema profile — what fields exist, how often, example value
# ============================================================

def infer_schema(docs: Iterable[Dict[str, Any]], *,
                 sample: int = 1000) -> List[Dict[str, Any]]:
    """Profile a collection's shape AFTER flattening: one entry per field
    with the types seen, how many docs carry it, presence %, and a short
    example. This is the single most model-digestible artefact — it lets
    the model learn a collection's structure without reading the data."""
    rows = normalize_documents(_take(docs, sample))
    total = len(rows)
    fields: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for r in rows:
        for k, v in r.items():
            info = fields.get(k)
            if info is None:
                info = {"count": 0, "types": set(), "example": ""}
                fields[k] = info
                order.append(k)
            info["count"] += 1
            info["types"].add(type(v).__name__)
            if not info["example"] and v not in (None, ""):
                ex = str(v)
                info["example"] = ex[:80] + ("…" if len(ex) > 80 else "")
    out: List[Dict[str, Any]] = []
    for k in order:
        info = fields[k]
        out.append({
            "field": k,
            "types": ", ".join(sorted(info["types"])),
            "present": info["count"],
            "present_pct": round(100.0 * info["count"] / total, 1) if total else 0.0,
            "example": info["example"],
        })
    return out


def _take(it: Iterable[Any], n: int) -> List[Any]:
    out: List[Any] = []
    for i, x in enumerate(it):
        if i >= n:
            break
        out.append(x)
    return out


# ============================================================
# Compact text digest — drop straight into a model prompt
# ============================================================

def documents_to_text(docs: Iterable[Dict[str, Any]], *,
                      max_docs: int = 50,
                      max_value_chars: int = 160,
                      include_schema: bool = True,
                      ) -> str:
    """Render documents as a compact, flat text block for a model prompt.

    Each document becomes a short ``key: value`` group; values are coerced
    (no ObjectId noise) and truncated. An optional schema header tells the
    model the field set up front. Bounded by ``max_docs`` so a huge
    collection can't blow the context window."""
    rows = normalize_documents(docs)
    shown = rows[:max_docs]
    parts: List[str] = []
    if include_schema and rows:
        schema = infer_schema(rows, sample=len(rows))
        hdr = ", ".join(f"{s['field']}({s['present_pct']:.0f}%)"
                        for s in schema[:40])
        parts.append(f"# {len(rows)} document(s); fields: {hdr}")
        if len(rows) > max_docs:
            parts.append(f"# showing first {max_docs} of {len(rows)}")
    for i, r in enumerate(shown):
        lines = [f"- doc {i + 1}:"]
        for k, v in r.items():
            sval = "" if v is None else str(v)
            if len(sval) > max_value_chars:
                sval = sval[:max_value_chars] + "…"
            lines.append(f"    {k}: {sval}")
        parts.append("\n".join(lines))
    return "\n".join(parts)


# ============================================================
# pandas helpers (lazy import)
# ============================================================

def documents_to_frame(docs: Iterable[Dict[str, Any]], *,
                       sep: str = ".",
                       max_array_items: int = DEFAULT_MAX_ARRAY_ITEMS,
                       max_array_chars: int = DEFAULT_MAX_ARRAY_CHARS,
                       ) -> "Any":
    """Flatten/coerce documents into a clean, all-scalar DataFrame —
    one row per document, dotted columns, arrays collapsed. Safe to feed
    to any tabular helper or show to a model."""
    import pandas as pd
    rows = normalize_documents(
        docs, sep=sep, max_array_items=max_array_items,
        max_array_chars=max_array_chars)
    return pd.DataFrame(rows) if rows else pd.DataFrame()


# ============================================================
# Streaming conversion to files — bounded memory for huge dumps
# ============================================================
# A real Mongo dump can be gigabytes. Loading it whole (bson.decode_all /
# json.loads) and then building a full DataFrame holds ~3 copies in RAM and
# OOM-kills the app on a memory-capped box (Linux / WSL). These helpers
# stream documents one at a time and write the CSV row-by-row, so peak
# memory stays flat regardless of collection size.

def iter_bson_documents(path: Any) -> "Iterable[Dict[str, Any]]":
    """Yield documents from a .bson file ONE at a time (constant memory),
    using bson.decode_file_iter instead of decode_all."""
    try:
        import bson
    except ImportError as exc:
        raise RuntimeError(
            "Reading .bson files needs pymongo (provides the `bson` "
            f"module). Install with: pip install pymongo (original: {exc})"
        ) from exc
    with open(path, "rb") as fh:
        for doc in bson.decode_file_iter(fh):
            yield doc


def _peek_first_nonspace(path: Any) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        while True:
            ch = fh.read(1)
            if ch == "":
                return ""
            if not ch.isspace():
                return ch


def iter_json_documents(path: Any, *,
                        max_json_bytes: Optional[int] = None
                        ) -> "Iterable[Dict[str, Any]]":
    """Yield documents from .json / .jsonl / .ndjson.

    JSONL / NDJSON stream line-by-line (constant memory). A single JSON
    array or object must be loaded whole — guarded by ``max_json_bytes`` so
    an oversized array raises a clear, CATCHABLE error instead of OOM-killing
    the process (re-export as .jsonl or .bson to stream it)."""
    from pathlib import Path as _P
    p = _P(path)
    ext = p.suffix.lower()
    if ext in (".jsonl", ".ndjson"):
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip().rstrip(",")
                if not line or line in ("[", "]"):
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(d, dict):
                    yield d
        return
    # .json — array or single object; must load, so guard the size.
    first = _peek_first_nonspace(p)
    try:
        size = p.stat().st_size
    except OSError:
        size = 0
    if max_json_bytes and size > max_json_bytes and first == "[":
        raise MemoryError(
            f"JSON array file is {size // (1024 * 1024)} MB (> "
            f"{max_json_bytes // (1024 * 1024)} MB safe limit). Re-export it "
            "as .jsonl / NDJSON or .bson so it can be streamed without "
            "loading the whole file into memory.")
    text = p.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        # Mislabeled NDJSON inside a .json file — fall back to line streaming.
        for line in text.splitlines():
            line = line.strip().rstrip(",")
            if not line or line in ("[", "]"):
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict):
                yield d
        return
    if isinstance(obj, list):
        for d in obj:
            if isinstance(d, dict):
                yield d
    elif isinstance(obj, dict):
        yield obj


def _doc_iter_factory(path: Any, *, max_json_bytes: Optional[int] = None):
    """Return a zero-arg callable that produces a FRESH document iterator
    each call (so we can make two streaming passes over the same file)."""
    from pathlib import Path as _P
    p = _P(path)
    if p.suffix.lower() == ".bson":
        return lambda: iter_bson_documents(p)
    return lambda: iter_json_documents(p, max_json_bytes=max_json_bytes)


def stream_convert_file(src: Any, out_dir: Any, *,
                        want_csv: bool = True,
                        want_schema: bool = True,
                        want_text: bool = False,
                        max_docs: Optional[int] = None,
                        max_cols: int = 2048,
                        text_sample_docs: int = 50,
                        max_value_chars: int = 160,
                        max_array_items: int = DEFAULT_MAX_ARRAY_ITEMS,
                        max_array_chars: int = DEFAULT_MAX_ARRAY_CHARS,
                        max_json_bytes: Optional[int] = None,
                        stem: Optional[str] = None,
                        ) -> Dict[str, Any]:
    """Convert ONE .bson/.json/.jsonl file to model-digestible artefacts with
    bounded memory, streaming documents one at a time:

      <stem>_clean.csv    flattened all-scalar table (one row per doc)
      <stem>_schema.csv   field / types / presence% / example
      <stem>_digest.txt   compact key:value text digest (first N docs)

    Two streaming passes: pass 1 collects the column union + schema + a small
    text sample; pass 2 writes CSV rows one at a time. Peak memory is the
    field set + one document — flat no matter how big the dump is.
    Returns a summary dict.
    """
    import csv as _csv
    from pathlib import Path as _P
    src = _P(src)
    out_dir = _P(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = stem or src.stem
    make_iter = _doc_iter_factory(src, max_json_bytes=max_json_bytes)
    # A whole-file .json ARRAY is already fully parsed into memory by make_iter
    # each call, so the two-pass design re-reads + re-parses it twice. Parse it
    # ONCE and drive both passes from the cached list (peak memory unchanged:
    # a single copy of the list is held instead of two sequential copies).
    # .jsonl / .ndjson / .bson keep their genuinely-constant-memory streaming.
    _cached_docs = (list(make_iter()) if src.suffix.lower() == ".json"
                    else None)

    def _docs():
        return _cached_docs if _cached_docs is not None else make_iter()

    def _flat(doc):
        if not isinstance(doc, dict):
            return {"value": coerce_value(doc)}
        return flatten_document(doc, max_array_items=max_array_items,
                                max_array_chars=max_array_chars)

    # ---- Pass 1: column union + schema counters + text sample ----
    columns: List[str] = []
    colset: set = set()
    cols_capped = False
    schema: Dict[str, Dict[str, Any]] = {}
    sample_rows: List[Dict[str, Any]] = []
    doc_count = 0
    for doc in _docs():
        if max_docs and doc_count >= max_docs:
            break
        row = _flat(doc)
        for k, v in row.items():
            if k not in colset:
                if len(columns) >= max_cols:
                    cols_capped = True
                    continue          # drop pathological extra fields
                colset.add(k)
                columns.append(k)
            info = schema.get(k)
            if info is None:
                info = {"count": 0, "types": set(), "example": ""}
                schema[k] = info
            info["count"] += 1
            info["types"].add(type(v).__name__)
            if not info["example"] and v not in (None, ""):
                ex = str(v)
                info["example"] = ex[:80] + ("…" if len(ex) > 80 else "")
        if len(sample_rows) < text_sample_docs:
            sample_rows.append(row)
        doc_count += 1

    outputs: List[str] = []

    if want_schema and doc_count:
        sp = out_dir / f"{stem}_schema.csv"
        with open(sp, "w", newline="", encoding="utf-8") as fh:
            w = _csv.writer(fh)
            w.writerow(["field", "types", "present", "present_pct", "example"])
            for k in columns:
                info = schema[k]
                w.writerow([k, ", ".join(sorted(info["types"])), info["count"],
                            round(100.0 * info["count"] / doc_count, 1),
                            info["example"]])
        outputs.append(str(sp))

    if want_text and doc_count:
        tp = out_dir / f"{stem}_digest.txt"
        hdr = ", ".join(f"{k}({schema[k]['count'] * 100 // doc_count}%)"
                        for k in columns[:40])
        lines = [f"# {doc_count} document(s); fields: {hdr}"]
        if doc_count > len(sample_rows):
            lines.append(f"# showing first {len(sample_rows)} of {doc_count}")
        for i, r in enumerate(sample_rows):
            lines.append(f"- doc {i + 1}:")
            for k, v in r.items():
                sval = "" if v is None else str(v)
                if len(sval) > max_value_chars:
                    sval = sval[:max_value_chars] + "…"
                lines.append(f"    {k}: {sval}")
        tp.write_text("\n".join(lines), encoding="utf-8")
        outputs.append(str(tp))

    # ---- Pass 2: stream rows into the CSV (one document at a time) ----
    rows_written = 0
    if want_csv and doc_count:
        cp = out_dir / f"{stem}_clean.csv"
        with open(cp, "w", newline="", encoding="utf-8") as fh:
            w = _csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
            w.writeheader()
            n = 0
            for doc in _docs():
                if max_docs and n >= max_docs:
                    break
                w.writerow(_flat(doc))
                rows_written += 1
                n += 1
        outputs.append(str(cp))

    return {
        "stem": stem, "docs": doc_count, "rows": rows_written,
        "columns": len(columns), "outputs": outputs,
        "truncated": bool(max_docs and doc_count >= max_docs),
        "cols_capped": cols_capped,
    }


def explode_documents(docs: Iterable[Dict[str, Any]], record_path: str, *,
                      meta: Optional[List[str]] = None) -> "Any":
    """One row per element of the array at ``record_path`` (dotted), with
    chosen top-level ``meta`` fields carried down — the tabular ('tidy')
    view of an array-of-subdocuments field. Coerces scalars first so the
    exploded rows are model-clean too."""
    import pandas as pd
    coerced = [coerce_value(d) for d in docs if isinstance(d, dict)]
    path = record_path.split(".")
    try:
        df = pd.json_normalize(
            coerced, record_path=path,
            meta=[m.split(".") for m in (meta or [])] if meta else None,
            errors="ignore", sep=".")
    except Exception:
        # Fallback: pull the array manually if json_normalize can't.
        out_rows: List[Dict[str, Any]] = []
        for d in coerced:
            node: Any = d
            for part in path:
                node = node.get(part) if isinstance(node, dict) else None
            if isinstance(node, list):
                for el in node:
                    row = dict(el) if isinstance(el, dict) else {"value": el}
                    for m in (meta or []):
                        mv: Any = d
                        for part in m.split("."):
                            mv = mv.get(part) if isinstance(mv, dict) else None
                        row[m] = mv
                    out_rows.append(row)
        df = pd.DataFrame(out_rows)
    # Final coercion pass for any nested cells json_normalize left behind.
    for col in df.columns:
        df[col] = df[col].map(
            lambda x: coerce_value(x) if isinstance(x, (dict, list)) else x)
    return df
