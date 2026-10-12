"""
council_core.turn_context — what a turn is given besides the question.

MOVED FROM council_gui_engine.py (lines 330-3515 of qt-migration @ be07131),
so both shells build a turn's context with the same code. The Tk engine
re-exports every name, so its 285-odd call sites and the smoke tests keep
working; the Qt shell imports this module on its turn worker. Toolkit-free:
nothing here touches a widget.

What it does, in the order a turn uses it (see `prepare_turn` at the end):
the analyst step (deterministic answers computed from the data — counts,
sums, a fresh derived result, a collection), and `_inject_file_contents`,
which puts the vault in front of the question: files the user named, folders,
vault matches with a summary of every matching file, value matches from the
data index, a dataset overview, the analyst's result or its refusal, and the
task memo — each block tagged with its token cost and capped, the droppable
ones dropped to fit the context window.

THE EDITS THE MOVE MADE (everything else is byte-for-byte):
  * `VAULT_DIR` reads became `_vault()` (22 places). The Tk engine
    registers its own VAULT_DIR (`use_vault_dir`), so a test that sets
    `council_gui_engine.VAULT_DIR` still steers these functions; a turn
    prepared by `prepare_turn` uses its session's vault; otherwise
    council_core.paths.vault_dir().
  * `_get_vault_index`, `_get_data_index` and `_resolve_referenced_path`
    first ask the session a turn is being prepared for (`TurnState`, set
    for the length of `prepare_turn` on that thread). Outside a prepared
    turn they keep their module-wide instances, as before. Without this a
    second vault in one process (two Qt windows, a test after a test) was
    searched through the first vault's index.
  * The module-wide instances (`_VAULT_INDEX_INSTANCE`,
    `_DATA_INDEX_INSTANCE`, `_REFERENCED_FILES_MAP`) are NOT re-exported
    by the Tk engine: a copy of a name that is rebound later would go stale
    silently. Set them here (`council_core.turn_context`).
"""
from __future__ import annotations

import contextvars
import os
import re as _re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import council_engine as ce
import data_index

from . import paths as _paths

# ============================================================
# The vault, and the session a turn is prepared for
# ============================================================

#: The TurnState of the turn being prepared on THIS thread (see
#: prepare_turn), or None.
_ACTIVE: "contextvars.ContextVar[Any]" = contextvars.ContextVar(
    "council_turn_state", default=None)

_VAULT_DIR_GETTER: Optional[Callable[[], Any]] = None


def use_vault_dir(getter: Optional[Callable[[], Any]]) -> None:
    """The Tk engine's hook: these functions read ITS VAULT_DIR (a test that
    sets council_gui_engine.VAULT_DIR steers them) outside a prepared turn."""
    global _VAULT_DIR_GETTER
    _VAULT_DIR_GETTER = getter


def _vault() -> Path:
    """The vault these functions read: the prepared turn's, else the one the
    Tk engine registered, else council_core.paths.vault_dir()."""
    state = _ACTIVE.get()
    if state is not None:
        return Path(state.vault_dir)
    if _VAULT_DIR_GETTER is not None:
        return Path(_VAULT_DIR_GETTER())
    return _paths.vault_dir()


# ============================================================
# Moved from council_gui_engine.py
# ============================================================

# A POSIX path segment must start at a word BOUNDARY. The `/` in "pass/fail" is
# not the start of a path, and the lookbehind is what says so.
#
# The pattern was `/\S+`, unanchored, so a slash anywhere inside a word matched
# and everything after it became a "file path the user referenced". In this
# app's own domain that fires constantly — and it is not a harmless
# over-match, because a path that doesn't exist is fed to the [NO DATA
# AVAILABLE] block, which is PRIO_NODATA (0), the highest priority in the whole
# injector and never dropped. It instructs the model to *refuse to answer*:
#
#     user   : "Did the tensile bar pass/fail?"
#     becomes: [NO DATA AVAILABLE - ... they do NOT exist on this machine:]
#                - /fail
#              [Refuse to give specific values for these files. Tell the user
#               the file is not present on this machine.]
#
# So an ordinary question got an outright refusal about a file the user never
# mentioned. Measured triggers, all plain English an AM engineer types daily:
# pass/fail, W/mm2, g/cm3, and/or, 50/50, as-built/HIP, plus every relative
# path ("data/jobs/x.csv" -> "/jobs/x.csv").
#
# `~/data/x.csv` was the same defect wearing a different hat: no `~` branch
# existed, so it matched the inner `/data/x.csv` — silently dropping the home
# directory and turning a REAL path into a refusal. `~` is deliberately
# preserved by the cleaning step above, so the intent to support it was there;
# only the pattern was missing. It is expanded at extraction so the three
# consumers (all of which do a bare `Path(...)`, none of which expanduser) get
# something openable.
_FILE_PATH_RE = _re.compile(
    r'[a-zA-Z]:[/\\]\S+'        # C:\... / C:/...  — drive-letter absolute
    r'|~[/\\]\S+'               # ~/...            — home-relative
    r'|(?<![\w.~])/\S+'         # /...             — POSIX absolute, at a
)                               #                    word boundary only
_FILE_READ_CHAR_LIMIT = 12000  # total chars per file in the injected block
# Extracts the file/folder name out of an injection cost label like
# "[VAULT MATCH: sales.csv]" / "[FILE: x]" / "[FOLDER: y]" — used per-label in
# the per-turn provenance source assembly (hoisted out of that loop).
_SOURCE_LABEL_RE = _re.compile(
    r"\[(?:VAULT MATCH(?:\s*\(pinned\))?|FILE|FOLDER):\s*(.+?)\]")


# ----- Token-aware injection helpers ---------------------------------------
# These exist because llama-cpp-python silently clips any prompt that exceeds
# `n_ctx`, which is the #1 cause of "the model hallucinated even though I gave
# it the file." Instead of letting the runtime do silent truncation, we:
#   • tag every block with its token cost so the model sees what it's getting
#   • cap each block at n_ctx/8 tokens (head + tail with a visible marker)
#   • drop the lowest-priority blocks (vault matches) when the assembled
#     prompt would still exceed the safe input budget
# All callers go through `_inject_file_contents`, which orchestrates this.

_BLOCK_HEADER_RE = _re.compile(r"\s*~[\d,]+\s+tokens\s*")


from functools import lru_cache as _lru_cache


@_lru_cache(maxsize=4096)
def _estimate_block_tokens_cached(block_text: str) -> int:
    """Inner cacheable form. Tokenisation is pure with respect to its
    input string — the same block always counts to the same number of
    tokens for a given model, and the model isn't hot-swapped during
    a session. 4096 entries × few hundred bytes each ~= 1-2 MB cap."""
    try:
        import council_engine as _ce
        return _ce.estimate_tokens(block_text or "")
    except Exception:
        return max(1, (len(block_text or "") + 3) // 4)


def _estimate_block_tokens(block_text: str) -> int:
    """Cheap token estimator — uses the loaded llama tokenizer when
    available, falls back to chars/4. Never raises.

    Wrapper around the lru_cache'd impl so callers can pass non-string
    inputs (None, bytes from a misbehaving caller) without poisoning
    the cache with un-hashable keys.
    """
    if not isinstance(block_text, str):
        block_text = "" if block_text is None else str(block_text)
    return _estimate_block_tokens_cached(block_text)


def _tag_block_header(block_text: str, token_count: int) -> str:
    """Stamp `  ~N tokens` into the [LABEL: ...] header on the first line so
    the model — and the user, via `context info` — can see how many tokens
    each injection costs. Idempotent: re-tagging strips any prior tag first.

    Recognises both `[LABEL]` and `[LABEL: name]` openings. If the first
    line isn't bracketed, we synthesize an `[INJECTION ~N tokens]` header.
    """
    if not block_text:
        return block_text
    nl = block_text.find('\n')
    first = block_text[:nl] if nl >= 0 else block_text
    rest = block_text[nl:] if nl >= 0 else ''
    tag = f"  ~{token_count:,} tokens"
    if first.lstrip().startswith('[') and ']' in first:
        cleaned = _BLOCK_HEADER_RE.sub("", first, count=1)
        close = cleaned.find(']')
        if close > 0:
            tagged = cleaned[:close] + tag + cleaned[close:]
        else:
            tagged = cleaned + tag
    else:
        tagged = f"[INJECTION{tag}]\n" + first
    return tagged + rest


def _smart_truncate_text(text: str, max_chars: int) -> tuple:
    """Trim long text to head + tail with a visible elision marker.

    Returns (trimmed, was_truncated). The marker tells the model what
    fraction is missing so it doesn't pretend it saw the whole file —
    that's the whole point vs llama-cpp's silent tail-clip.
    """
    if not text or len(text) <= max_chars:
        return text, False
    half = max(200, (max_chars - 120) // 2)
    head = text[:half]
    tail = text[-half:]
    skipped = len(text) - len(head) - len(tail)
    marker = (
        f"\n... [truncated: {skipped:,} characters elided from the middle — "
        f"ask for specific lines or use the analyst for exact answers] ...\n"
    )
    return head + marker + tail, True


def _text_peek(path, max_bytes: int = 4096) -> str:
    """First ~max_bytes of a file decoded as text, or a binary note. Bounded."""
    p = Path(path)
    try:
        data = p.read_bytes()[:max_bytes]
    except Exception as e:
        return f"(could not read: {e})"
    try:
        txt = data.decode("utf-8")
    except Exception:
        try:
            txt = data.decode("utf-8", errors="replace")
        except Exception:
            try:
                return f"(binary file, {p.stat().st_size} bytes)"
            except Exception:
                return "(binary file)"
    return txt


def _data_preview_text(path, max_rows: int = 50):
    """Return ``(schema_text, rows_text)`` for a quick, MODEL-FREE preview of a
    data file — column dtypes + the first ``max_rows`` rows. Bounded reads.
    Best-effort across CSV/TSV/parquet/Excel/JSON; falls back to a short text
    peek for anything non-tabular. Pure + UI-free so it's unit-testable."""
    p = Path(path)
    suf = p.suffix.lower()
    try:
        import pandas as pd
    except Exception:
        return ("(pandas unavailable — showing a raw text peek.)", _text_peek(p))
    df = None
    try:
        if suf in (".csv", ".tsv", ".txt"):
            sep = "\t" if suf == ".tsv" else None
            df = pd.read_csv(p, nrows=max_rows, sep=sep, engine="python")
        elif suf in (".parquet", ".pq"):
            df = pd.read_parquet(p).head(max_rows)
        elif suf in (".xlsx", ".xls"):
            df = pd.read_excel(p, nrows=max_rows)
        elif suf == ".json":
            try:
                df = pd.read_json(p).head(max_rows)
            except Exception:
                df = None
    except Exception as e:
        return (f"Could not parse {p.name} as a table: {e}", _text_peek(p))
    if df is not None:
        try:
            schema_lines = [f"{c}: {t}"
                            for c, t in df.dtypes.astype(str).items()]
            schema = (f"{len(df.columns)} column(s) (dtypes from the first "
                      f"{max_rows} rows):\n\n" + "\n".join(schema_lines))
            rows = df.to_string(index=False, max_rows=max_rows, max_cols=40)
            return (schema, rows)
        except Exception:
            pass
    return ("Not a recognised table format — showing a raw text peek.",
            _text_peek(p))


# Discoverability: example prompts shown by the "What can I ask?" panel.
# Grouped (category, prompt, hint). Hints explain WHY a prompt is useful and
# which capability it exercises — most of these are instant direct routes.
_COUNCIL_EXAMPLES = [
    ("Counting & summaries",
     "how many files are in data_in",
     "Instant exact count + by-type breakdown (no model)."),
    ("Counting & summaries",
     "give me a data summary of the files",
     "One row per file: rows, columns, types (no model)."),
    ("Counting & summaries",
     "summary of stats for the files",
     "Per-column min/max/mean across the files (no model)."),
    ("Find & explore",
     "find files containing Job Blue",
     "Deterministic search by name + indexed content; click to preview."),
    ("Find & explore",
     "look up the value 12345 across files",
     "Cross-file value search — which files mention it, and where."),
    ("Charts",
     "chart sales by month",
     "Finds the right CSV and plots it for you."),
    ("Projects (Collections)",
     "summarise the Job Blue files",
     "Answer about a whole project once you've saved it as a Collection "
     "(Vault tab → Collections)."),
    ("Column analytics",
     "column stats in sales.csv",
     "Per-column min/max/mean/median/std/sum + zero counts, with mean & "
     "median BOTH including and excluding zeros (no model)."),
    ("Column analytics",
     "missing data in sales.csv",
     "Nulls per column + how many rows are fully complete (no model)."),
    ("Column analytics",
     "duplicates in sales.csv",
     "How many exact duplicate rows, with a few examples (no model)."),
    ("Column analytics",
     "top values in sales.csv",
     "Most frequent values in each column — a fast frequency table (no model)."),
    ("Column analytics",
     "correlations in sales.csv",
     "Strongest pairwise correlations between numeric columns (no model)."),
    ("Column analytics",
     "mean of revenue in all csvs in data_in and save to a csv",
     "Compute a stat (mean/sum/min/max/median/std/count) of one column across "
     "every CSV in a folder; add 'save to a csv/text file' to write the result "
     "into the vault's data_out/reports/ (no model)."),
    ("Images",
     "image stats of layer_0345.png",
     "Pixel statistics (brightness, contrast, per-channel, dominant colours) "
     "of one image, or 'image stats in <folder>' for a whole-folder rollup "
     "(no model). Also: 'ocr <image>' to read text inside it."),
    ("Images",
     "count features in layer_0345.png expecting 12",
     "Detect + count discrete features/objects (pores, spatter, spots) in an "
     "image, check against an expected number, and save an annotated image with "
     "each one boxed + numbered (classical CV, no model). Use 'bright'/'dark' "
     "to pick feature polarity."),
    ("Build a tool",
     "create a tool that flags rows where quantity is 0",
     "Opens the Tool Creation tab; the local model writes the tool, the "
     "sandbox validates it (read-only), and it's saved (UNREVIEWED) for reuse."),
    ("Find in files",
     "which files mention bacon",
     "Layered vault search — file summaries first, then a deeper scan of the "
     "actual text; app-state files are excluded."),
    ("Find in files",
     "how many files are associated with job 317",
     "Counts + lists files whose FOLDER name or FILE name includes the term "
     "(ignoring spaces / underscores / hyphens) — not the whole vault."),
    ("When an answer is weak",
     "(click ⤓ Defer to Vault)",
     "Save what the model couldn't do; run it from the Vault tab, then "
     "re-ask — the saved result is reused."),
]


def _build_answer_report_md(question, answer, table, sources):
    """Render a council answer as a Markdown report (question, answer, result
    table, sources). Pure + UI-free so it's unit-testable. ``sources`` may be
    Path objects or strings."""
    lines = ["# Council answer", ""]
    q = (question or "").strip()
    if q:
        lines += ["## Question", "", q, ""]
    lines += ["## Answer", "", (answer or "").strip(), ""]
    t = (table or "").strip()
    if t:
        lines += ["## Result table", "", "```", t, "```", ""]
    srcs = list(sources or [])
    if srcs:
        lines += ["## Sources", ""]
        for s in srcs:
            name = getattr(s, "name", None) or Path(str(s)).name
            lines.append(f"- {name}")
        lines.append("")
    return "\n".join(lines)


# ── Filename wildcard patterns ─────────────────────────────────────────
# Moved to council_core.vault_search, with the explanation of what `#` and
# `*` mean; both front ends resolve a name the same way.
from council_core.vault_search import (           # noqa: E402
    compile_name_pattern as _compile_name_pattern,
    name_matches_pattern as _name_matches_pattern,
)


def _tokenize_ident(s) -> list:
    """Typed tokens from a name: numeric runs -> ('n', int) so LEADING ZEROS
    don't matter (0317 == 317 == 00317), alpha runs -> ('a', str). Separators
    (space / _ / - / . etc.) are dropped, so 'Job 317', 'job_0317', 'job-317'
    and 'job317' all tokenize compatibly."""
    out = []
    for m in _re.finditer(r"[a-z]+|[0-9]+", str(s or "").lower()):
        t = m.group(0)
        out.append(("n", int(t)) if t.isdigit() else ("a", t))
    return out


def _ident_contains(seg_tokens, q_tokens) -> bool:
    """True if ``q_tokens`` appears as a CONTIGUOUS sub-sequence of
    ``seg_tokens`` — numeric tokens compared by VALUE, alpha by equality. So
    'job 317' matches 'job_0317' and 'summary_job317' but NOT 'job 3170'
    (317 != 3170) and not 'myjob317'."""
    m = len(q_tokens)
    if m == 0:
        return False
    for i in range(len(seg_tokens) - m + 1):
        if seg_tokens[i:i + m] == q_tokens:
            return True
    return False


def _files_associated_with(in_dir, entity, *, limit: int = 3000):
    """Files under ``in_dir`` ASSOCIATED with ``entity``: the entity's typed
    token sequence appears in a FOLDER segment of the file's path OR in the
    FILENAME. Answers 'how many files are associated with job 317' — a folder
    `Job_317/` or `Job_0317/` and a file `job317_data.csv` all count, but
    `Job 318/` and `job 3170` do not. Numbers match by value (leading zeros
    ignored).

    Returns a list of ``(abs_path, where)`` where ``where`` is 'folder' or
    'filename'. App-state / protected files (question_history.json, indices,
    conversation logs) are excluded. Pure + UI-free so it's unit-testable."""
    import os as _os
    q_tokens = _tokenize_ident(entity)
    if not q_tokens:
        return []
    try:
        import conversation_logger as _cl
    except Exception:
        _cl = None
    skip = {"__pycache__", ".git", ".vault_index", ".stats_cache",
            "conversation_logs", "conversations", ".chromadb", "derived"}
    out = []
    root = Path(in_dir)
    try:
        for dp, dn, fn in _os.walk(str(root)):
            dn[:] = [d for d in dn if d not in skip and not d.startswith(".")]
            try:
                rel_parts = Path(dp).relative_to(root).parts
            except Exception:
                rel_parts = ()
            folder_match = any(_ident_contains(_tokenize_ident(seg), q_tokens)
                               for seg in rel_parts)
            for f in fn:
                if f.startswith("."):
                    continue
                full = Path(dp) / f
                if _cl is not None:
                    try:
                        if _cl.is_protected_path(full, _vault()):
                            continue
                    except Exception:
                        pass
                file_match = _ident_contains(_tokenize_ident(f), q_tokens)
                if folder_match or file_match:
                    out.append((str(full),
                                "filename" if file_match else "folder"))
                    if len(out) >= limit:
                        return out
    except Exception:
        pass
    return out


# Moved to council_core.vault_search so the Qt tab searches the same way.
# The private names stay: other call sites and the smoke suite use them.
from council_core.vault_search import (           # noqa: E402
    search_vault_filenames as _search_vault_filenames,
)


def _coach_for_error(msg: str):
    """Map a raw error / traceback string to plain-language guidance plus a
    one-click fix. Returns ``dict(plain, action_label, action)`` for a
    recognised failure, or ``None`` for an unrecognised one (the caller then
    shows the raw error). Pure + UI-free so it's unit-testable.

    Actions the GUI knows how to run: ``"engine"`` (open Engine settings),
    ``"models"`` (open the Models tab), ``"cpu"`` (force CPU + retry).
    """
    m = (msg or "").lower()
    # 1) Context-window overflow — the most common confusing failure on a
    #    small model / 4K-ctx box ("exceeds max tokens").
    if (("exceed" in m and ("context" in m or "token" in m))
            or "requested tokens" in m
            or "exceeds max tokens" in m
            or "context window" in m):
        return {
            "plain": ("The question plus its data was larger than the model's "
                      "context window. Raise the max context in Engine "
                      "settings, or ask about fewer files at once."),
            "action_label": "⚙ Open Engine settings",
            "action": "engine",
        }
    # 2) GPU / CUDA failure — VRAM exhaustion, a driver fault, or a prior
    #    core-dump sentinel. Degrade to CPU (slower but reliable).
    if ("cuda" in m or "cublas" in m or "ggml_cuda" in m
            or "device-side assert" in m
            or ("gpu" in m and "memory" in m)):
        return {
            "plain": ("The GPU run failed — usually not enough VRAM. Switch "
                      "the model to CPU (slower but reliable) and retry."),
            "action_label": "Switch to CPU and retry",
            "action": "cpu",
        }
    # 3) Model not loaded / missing file.
    if (("llama" in m and ("failed" in m or "could not" in m or "load" in m))
            or "no such file" in m
            or "model not found" in m
            or "council_gguf_path" in m
            or ("gguf" in m and "not found" in m)):
        return {
            "plain": ("The model could not be loaded. Choose a model file, or "
                      "download one that fits your hardware."),
            "action_label": "🇺🇸 Open Models",
            "action": "models",
        }
    # 4) Generic out-of-memory (CPU RAM).
    if ("memoryerror" in m or "out of memory" in m
            or "cannot allocate" in m):
        return {
            "plain": ("Ran out of memory. Try asking about fewer or smaller "
                      "files, or switch to a smaller model."),
            "action_label": "🇺🇸 Open Models",
            "action": "models",
        }
    return None


def _smart_truncate_block_to_tokens(block_text: str, max_tokens: int) -> str:
    """Truncate a rendered injection block to fit a per-block token cap.

    Uses `estimate_tokens` and falls back to char-budget trimming. Always
    leaves a visible marker so the model knows content was elided.
    """
    cur = _estimate_block_tokens(block_text)
    if cur <= max_tokens:
        return block_text
    # ~4 chars/token works for English; over-estimate a touch by using 4.
    target_chars = max(800, max_tokens * 4)
    trimmed, _ = _smart_truncate_text(block_text, target_chars)
    return trimmed


def _sample_rows_head_mid_tail(rows: list, max_rows: int) -> tuple:
    """Sample a long row list into head + middle + tail slices.

    Used by CSV / TSV / Parquet / Excel renderers so the model sees the
    SHAPE of all three regions (a CSV where column dtypes change after
    row 5,000 is now visible) instead of just the first N rows.

    Returns (head_rows, mid_rows, tail_rows, skipped_before_mid,
    skipped_between_mid_and_tail). Each slice is roughly max_rows/3 long.
    """
    n = len(rows)
    if n <= max_rows:
        return rows, [], [], 0, 0
    slice_size = max(3, max_rows // 3)
    head = rows[:slice_size]
    tail = rows[-slice_size:]
    mid_center = n // 2
    mid_start = max(slice_size, mid_center - slice_size // 2)
    mid_end = min(n - slice_size, mid_start + slice_size)
    mid = rows[mid_start:mid_end]
    skipped_before_mid = mid_start - len(head)
    skipped_between = (n - len(tail)) - mid_end
    return head, mid, tail, skipped_before_mid, skipped_between


def _render_dataframe_block(kind_label, df, max_rows=20):
    """Render a small DataFrame as a CSV-shaped text block. Used by the
    injection layer for TSV / Parquet / gzipped CSV so the model sees
    the same structure regardless of source format.

    When the frame has more than `max_rows` rows, samples HEAD + MIDDLE +
    TAIL (with explicit skip markers) so the model sees the SHAPE of the
    full file — not just the top rows. This matters for files where
    column dtypes change deep in the data, or where the last rows hold
    totals/summaries.
    """
    headers = [str(c) for c in df.columns]
    total = len(df)
    rows = [r.tolist() for _, r in df.iterrows()]

    def _fmt(r):
        cells = []
        for v in r:
            cv = str(v).replace('\n', ' ').strip()
            if len(cv) > 80:
                cv = cv[:77] + '...'
            cells.append(cv)
        return '  ' + ' | '.join(cells)

    lines = [
        f"({kind_label} — already parsed into plain text below. "
        f"Read each row as if it were a CSV row.)",
        f"Columns ({len(headers)}): " + ', '.join(headers),
    ]
    if total <= max_rows:
        # Note: `total` is the count this renderer received — callers often
        # pass a .head(200)'d slice of a larger source, so we don't claim
        # this is the "full file." Just report what we can see.
        lines.append(f"Total rows shown: {total} (of {total} loaded)")
        lines.append("Sample rows:")
        for r in rows:
            lines.append(_fmt(r))
    else:
        head, mid, tail, sk_b, sk_a = _sample_rows_head_mid_tail(rows, max_rows)
        shown = len(head) + len(mid) + len(tail)
        lines.append(
            f"Total rows shown: {shown} of {total} loaded "
            f"(HEAD + MIDDLE + TAIL sampled — middle rows elided)"
        )
        lines.append("Sample rows (head):")
        for r in head:
            lines.append(_fmt(r))
        if mid:
            lines.append(f"  ... [skipped {sk_b:,} rows] ...")
            lines.append("Sample rows (middle):")
            for r in mid:
                lines.append(_fmt(r))
        if tail:
            lines.append(f"  ... [skipped {sk_a:,} rows] ...")
            lines.append("Sample rows (tail):")
            for r in tail:
                lines.append(_fmt(r))
    return '\n'.join(lines)


def _render_sqlite_block(p):
    """Render a SQLite database as table name + columns + row count + a
    small sample from each table. Read-only connection."""
    import sqlite3 as _sq
    lines = [
        "(SQLite database — already parsed into plain text below. "
        "Each table is shown with its columns and a few sample rows.)",
    ]
    try:
        con = _sq.connect(f"file:{p}?mode=ro", uri=True)
        try:
            cur = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "ORDER BY name LIMIT 20"
            )
            names = [r[0] for r in cur.fetchall() if r and r[0]]
            lines.append(f"Tables ({len(names)}): " + ', '.join(names))
            lines.append("")
            for tname in names[:10]:
                qname = '"' + tname.replace('"', '""') + '"'
                try:
                    cur = con.execute(f"PRAGMA table_info({qname})")
                    cols = [r[1] for r in cur.fetchall() if r and r[1]]
                except Exception:
                    cols = []
                try:
                    cur = con.execute(f"SELECT COUNT(*) FROM {qname}")
                    nrows = cur.fetchone()[0]
                except Exception:
                    nrows = None
                lines.append(f'Table "{tname}" ({nrows if nrows is not None else "?"} rows):')
                lines.append("  columns: " + ', '.join(cols))
                try:
                    cur = con.execute(f"SELECT * FROM {qname} LIMIT 5")
                    sample = cur.fetchall()
                    if sample:
                        lines.append("  rows:")
                        for r in sample:
                            cells = []
                            for v in r:
                                cv = str(v).replace('\n', ' ').strip()
                                if len(cv) > 80:
                                    cv = cv[:77] + '...'
                                cells.append(cv)
                            lines.append('    ' + ' | '.join(cells))
                except Exception:
                    pass
                lines.append("")
        finally:
            con.close()
    except Exception as exc:
        lines.append(f"(could not open database: {exc!r})")
    return '\n'.join(lines).rstrip()


def _detect_excel_header_rows(p, sheet_name, max_check=3):
    """Look at the first `max_check` rows of `sheet_name` via openpyxl
    and detect whether the workbook uses MERGED CELLS in the top rows
    (the common "group headers above sub-headers" pattern). Returns
    the integer number of header rows to feed pandas' header= parameter.

    Returns 1 (single-row header) when no merging is detected. Returns
    2 when row 1 has any merged cells spanning multiple columns
    (interpreted as a group-header row above the actual column names).
    Returns 3 if rows 1 AND 2 both have merging.
    """
    try:
        import openpyxl
        wb = openpyxl.load_workbook(p, read_only=False, data_only=True)
    except Exception:
        return 1
    try:
        if isinstance(sheet_name, int) and 0 <= sheet_name < len(wb.worksheets):
            ws = wb.worksheets[sheet_name]
        elif isinstance(sheet_name, str) and sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
        else:
            ws = wb.worksheets[0]
        merged_rows = set()
        for rng in ws.merged_cells.ranges:
            if rng.max_col - rng.min_col >= 1:  # spans multiple columns
                for r in range(rng.min_row, min(rng.max_row, max_check) + 1):
                    merged_rows.add(r)
        if not merged_rows:
            return 1
        # If row 1 is merged, we have at least 2 header rows (the merged
        # group + the column-name row below). Extend if row 2 also has
        # merges that span columns.
        max_merged = max(merged_rows)
        return max(2, min(max_merged + 1, max_check))
    finally:
        try: wb.close()
        except Exception: pass


def _flatten_multi_header(df, sep=" / "):
    """Collapse a MultiIndex column header into single strings.

    pandas read_excel(header=[0,1]) yields tuples like ("Site A", "energy");
    we render them as "Site A / energy" for the prompt and as a single
    column name for the analyst. Empty group labels (Unnamed: 0_level_0)
    get cleaned up.
    """
    import pandas as _pd_fi
    if not isinstance(df.columns, _pd_fi.MultiIndex):
        return df
    new_cols = []
    for tup in df.columns:
        parts = [str(x) for x in tup
                 if x is not None and not str(x).startswith("Unnamed:")]
        new_cols.append(sep.join(parts) if parts else "(unnamed)")
    df = df.copy()
    df.columns = new_cols
    return df


def _render_excel_block(p, char_limit):
    """Render an .xlsx/.xls workbook with awareness of merged-cell
    headers — the common 'top row groups, second row sub-columns'
    layout. Without this, pandas mangles the columns to 'Unnamed: N'
    and the model can't tell what each value represents.
    """
    import pandas as _pd_fi
    xl = _pd_fi.ExcelFile(str(p))
    lines = [
        "(Excel workbook — already parsed into plain text below. "
        "Read each sheet as if it were a CSV table. Merged-cell group "
        "headers are joined with ' / ' so each column has a unique name.)",
        "Total sheets: " + str(len(xl.sheet_names)),
        "",
    ]
    sample_per_sheet = 10
    for sname in xl.sheet_names[:8]:
        header_rows = _detect_excel_header_rows(p, sname)
        try:
            if header_rows >= 2:
                df = xl.parse(sname, header=list(range(header_rows)),
                              nrows=sample_per_sheet * 2)
                df = _flatten_multi_header(df)
                merged_note = (f" (DETECTED {header_rows} header rows — "
                               f"top rows have merged cells, joined with ' / ')")
            else:
                df = xl.parse(sname, nrows=sample_per_sheet * 2)
                merged_note = ""
        except Exception as _ex:
            lines.append(f'Sheet "{sname}": read error: {_ex}')
            lines.append("")
            continue
        headers = [str(c) for c in df.columns]
        lines.append(f'Sheet "{sname}"{merged_note} '
                     f'({len(df)} rows shown, {len(headers)} columns):')
        lines.append('  columns: ' + ', '.join(headers))
        lines.append('  rows:')
        for _, row in df.head(sample_per_sheet).iterrows():
            cells = []
            for v in row.tolist():
                cv = str(v).replace('\n', ' ').strip()
                if len(cv) > 80:
                    cv = cv[:77] + '...'
                cells.append(cv)
            lines.append('    ' + ' | '.join(cells))
        lines.append("")
    content = '\n'.join(lines).rstrip()
    # Visible head+tail trim so the model sees the start AND end of the
    # workbook listing rather than silently losing the last sheets.
    if len(content) > char_limit:
        content, _ = _smart_truncate_text(content, char_limit)
    return content


def _short_count(n: int) -> str:
    """Compact integer rendering used in the folder summary's per-file
    schema preview. Keeps lines short enough that 47 files fit in
    a token budget."""
    if n is None:
        return "?"
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if abs(n) >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _schema_preview_for_file(rel_path: Path, full_path: Path,
                              vault_index, include_columns: bool) -> str:
    """One-line schema preview for a single file. Looks up the file's
    record in the vault index and renders the columns / row count /
    sheet names / JSON keys. Falls back to a size-only line when the
    file isn't in the index (e.g. a .gguf, a binary blob).

    ``include_columns`` controls whether to spell out the first ~8
    column names. We turn this OFF for files past the per-folder
    detail cap so a 200-file folder still fits in a token budget.

    The fix for "model can see files exist but can't read data inside":
    the old compact renderer showed JUST the filename, so the model had
    no way to know what columns or how many rows each CSV had. Now
    every line carries the schema — model can answer questions like
    "what columns does orders.csv have?" purely from the folder block.
    """
    rec = None
    if vault_index is not None:
        try:
            rec = vault_index.records.get(str(full_path))
            # Fallback — index keys often use resolved paths
            if rec is None:
                rec = vault_index.records.get(str(full_path.resolve()))
        except Exception:
            rec = None

    # No index record → just filename + size
    if not isinstance(rec, dict):
        try:
            sz = full_path.stat().st_size
            sz_str = (f"{sz/1024/1024:.1f} MB"
                      if sz >= 1024*1024
                      else (f"{sz/1024:.0f} KB" if sz >= 1024
                            else f"{sz} B"))
            return f"  {rel_path}  ·  {sz_str}"
        except Exception:
            return f"  {rel_path}"

    rtype = rec.get("type") or "?"

    if rtype in ("csv", "tsv", "csv.gz", "parquet"):
        rows = _short_count(rec.get("rows"))
        cols_full = rec.get("headers") or []
        ncols = len(cols_full)
        suffix = f"  ·  {rtype} · {rows} rows × {ncols} cols"
        if include_columns and cols_full:
            shown = cols_full[:8]
            extra = (f", +{ncols - 8}" if ncols > 8 else "")
            suffix += f" [{', '.join(map(str, shown))}{extra}]"
        return f"  {rel_path}{suffix}"

    if rtype == "excel":
        sheets = rec.get("sheets") or []
        suffix = f"  ·  excel · {len(sheets)} sheet{'s' if len(sheets) != 1 else ''}"
        if include_columns and sheets:
            sheet_names = [str(s.get("sheet", "?")) for s in sheets[:5]]
            extra = (f", +{len(sheets) - 5}" if len(sheets) > 5 else "")
            suffix += f" [{', '.join(sheet_names)}{extra}]"
        return f"  {rel_path}{suffix}"

    if rtype in ("json", "d3dpipeline", "bson"):
        keys = rec.get("keys") or []
        nkeys = len(keys)
        suffix = f"  ·  {rtype} · {nkeys} key{'s' if nkeys != 1 else ''}"
        if include_columns and keys:
            shown = keys[:8]
            extra = (f", +{nkeys - 8}" if nkeys > 8 else "")
            suffix += f" [{', '.join(map(str, shown))}{extra}]"
        return f"  {rel_path}{suffix}"

    if rtype in ("sqlite", "duckdb"):
        tables = rec.get("tables") or []
        suffix = f"  ·  {rtype} · {len(tables)} table{'s' if len(tables) != 1 else ''}"
        if include_columns and tables:
            table_names = [str(t.get("table", "?")) for t in tables[:5]]
            extra = (f", +{len(tables) - 5}" if len(tables) > 5 else "")
            suffix += f" [{', '.join(table_names)}{extra}]"
        return f"  {rel_path}{suffix}"

    if rtype == "image":
        w = rec.get("width")
        h = rec.get("height")
        if w and h:
            return f"  {rel_path}  ·  image · {w}×{h}"
        return f"  {rel_path}  ·  image"

    # Plain text / source code / etc.
    if rec.get("rows"):
        return f"  {rel_path}  ·  {rtype} · {_short_count(rec.get('rows'))} lines"
    return f"  {rel_path}  ·  {rtype}"


def _render_folder_summary_compact(folder, max_files=120, max_chars=4500,
                                    *, vault_index=None,
                                    detailed_top_n: int = 40):
    """Compact folder summary — counts + subfolder breakdown + filename
    list WITH per-file schema previews pulled from the vault index.

    Previously this rendered filenames ONLY. The model saw "orders.csv
    exists" but had no way to see the columns or row count, which led
    to the "model can see files exist but can't read data inside"
    reports — perfectly true from the model's perspective, since the
    block we injected literally only contained filenames.

    Now each line carries a one-line schema preview:
        orders.csv     ·  csv · 1.2K rows × 8 cols [order_id, customer, total, ...]
        customers.csv  ·  csv · 503 rows × 5 cols  [id, name, email, joined, segment]
        config.json    ·  json · 12 keys [settings, sources, outputs, ...]
        report.xlsx    ·  excel · 3 sheets [Q1, Q2, Q3]

    The top ``detailed_top_n`` files get the full schema with column
    names; the remainder get a shorter `name · type · rows × cols`
    line so a 500-file folder still fits in a few hundred lines.

    Used by the vault-trigger auto-injection so a small-context model
    (e.g. 4 K window) doesn't get its entire prompt budget eaten by one
    rich folder block. About 5-10× smaller than _render_folder_for_injection
    on the same vault.

    Returns ``None`` when the folder is missing — caller treats as a
    no-op and skips the block.
    """
    folder = Path(folder)
    if not folder.exists() or not folder.is_dir():
        return None

    # Lazy-fetch the vault index if the caller didn't pass one. The
    # injector usually has it in hand already; standalone callers
    # (the wizard / debug paths) get it via the existing helper.
    if vault_index is None:
        try:
            vault_index = _get_vault_index()
        except Exception:
            vault_index = None
        if vault_index is not None:
            # Make sure the index actually reflects what's on disk —
            # delta-aware so this is cheap if nothing changed.
            try:
                vault_index.rebuild()
            except Exception:
                pass

    try:
        import conversation_logger as _cl
    except Exception:
        _cl = None

    SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "node_modules"}
    # Higher than the rich renderer's 5K — we don't parse files, so
    # walking 10K dir entries is still cheap.
    SCAN_LIMIT = 10000

    from collections import defaultdict as _dd
    files: list = []          # list of (rel_path, full_path)
    subfolders: dict = _dd(int)
    by_suffix: dict = _dd(int)
    scanned = 0
    walk_truncated = False
    for p in folder.rglob("*"):
        scanned += 1
        if scanned > SCAN_LIMIT:
            walk_truncated = True
            break
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(folder)
        except ValueError:
            continue
        if any(part in SKIP_DIRS or part.startswith(".")
               for part in rel.parts[:-1]):
            continue
        if _cl is not None:
            try:
                if _cl.is_protected_path(p, folder.parent):
                    continue
            except Exception:
                pass
        if len(rel.parts) > 1:
            subfolders[rel.parts[0]] += 1
        suf = p.suffix.lower() or "(no ext)"
        by_suffix[suf] += 1
        files.append((rel, p))

    total = len(files)
    total_label = f"{total}" + ("+" if walk_truncated else "")
    lines = [f"[FOLDER SUMMARY: {folder.name or folder}]"]
    lines.append(f"Total files: {total_label}"
                 + (f" (walked first {SCAN_LIMIT:,} entries — more may exist)"
                    if walk_truncated else ""))

    if subfolders:
        lines.append("")
        lines.append("Subfolder file counts:")
        for sub, count in sorted(subfolders.items(),
                                  key=lambda x: (-x[1], x[0]))[:30]:
            lines.append(f"  {sub}/: {count} file{'s' if count != 1 else ''}")
        if len(subfolders) > 30:
            lines.append(f"  ... ({len(subfolders) - 30} more subfolders)")

    if by_suffix:
        type_bits = [f"{s}: {c}" for s, c
                     in sorted(by_suffix.items(), key=lambda x: -x[1])[:12]]
        lines.append("")
        lines.append("Files by type: " + ", ".join(type_bits))

    if files:
        lines.append("")
        shown = files[:max_files]
        lines.append(
            f"Files (first {len(shown)} of {total}) — schema preview per "
            f"file so you can answer 'what's in X' without a follow-up "
            f"tool call:")
        for idx, (rel_path, full_path) in enumerate(shown):
            include_cols = (idx < detailed_top_n)
            lines.append(_schema_preview_for_file(
                rel_path, full_path, vault_index,
                include_columns=include_cols,
            ))
        if total > max_files:
            lines.append(f"  ... ({total - max_files} more files not shown — "
                         f"ask about specific files or subfolders.)")

    text = "\n".join(lines)
    if len(text) > max_chars:
        text, _ = _smart_truncate_text(text, max_chars)
    return text


def _render_folder_for_injection(folder, max_files=40, max_chars=12000):
    """Build a comprehensive [FOLDER: ...] injection block listing every
    file in the folder. For tabular files, include headers + first 3
    rows so the model sees the SHAPE of every file, not just top-5
    vault-search hits.

    Honors PROTECTED_SUBDIRS: files under conversation_logs/ /
    conversations/ are never included.
    """
    folder = Path(folder)
    if not folder.exists() or not folder.is_dir():
        return None
    try:
        import conversation_logger as _cl
    except Exception:
        _cl = None

    lines = [f"[FOLDER: {folder}]"]
    lines.append("(All files in this folder are listed below — already "
                 "scanned. Treat this as the COMPLETE inventory of what's "
                 "available; do not invent files that aren't shown here.)")
    lines.append("")

    # Collect files (recursive, skip hidden/build/protected).
    # Bound the walk: previously `for p in folder.rglob("*")` traversed
    # the entire tree even on folders with 50k+ entries (the user only
    # ever sees 40). On corporate file shares this is multi-second
    # latency for nothing. SCAN_LIMIT caps the walk; if hit, we tell
    # the model the listing is partial so it doesn't claim to have
    # seen everything.
    SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv", "node_modules"}
    SCAN_LIMIT = 5000   # entries walked, not files kept
    files = []
    scanned = 0
    walk_truncated = False
    for p in folder.rglob("*"):
        scanned += 1
        if scanned > SCAN_LIMIT:
            walk_truncated = True
            break
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS or part.startswith(".")
               for part in p.relative_to(folder).parts[:-1]):
            continue
        if _cl is not None:
            try:
                if _cl.is_protected_path(p, folder.parent):
                    continue
            except Exception:
                pass
        try:
            size = p.stat().st_size
        except Exception:
            continue
        files.append((p, size))
    files.sort(key=lambda fs: (fs[0].suffix.lower(), fs[0].name.lower()))

    if not files:
        lines.append("(folder is empty)")
        return "\n".join(lines)

    # Truncate if too many
    truncated_n = max(0, len(files) - max_files)
    files_shown = files[:max_files]
    total_label = f"{len(files)}" + ("+ " if walk_truncated else "")
    suffix = ""
    if walk_truncated:
        suffix = (f" — walked first {SCAN_LIMIT:,} entries; the folder "
                  f"contains more files not scanned")
    elif truncated_n:
        suffix = f" (showing first {max_files})"
    lines.append(f"Total files: {total_label}{suffix}")
    lines.append("")

    # Per-file summary — short for tabular, name-only for binary
    for p, size in files_shown:
        rel = p.relative_to(folder)
        suf = p.suffix.lower()
        size_kb = size / 1024
        size_str = f"{size_kb:>8.1f} KB" if size_kb < 1024 else f"{size_kb/1024:>7.1f} MB"
        head = f"  {size_str}  {rel}"
        try:
            if suf == ".csv":
                import pandas as _pd
                df = _pd.read_csv(p, nrows=3)
                cols = ", ".join(str(c) for c in df.columns[:12])
                more = "" if len(df.columns) <= 12 else f" + {len(df.columns)-12} more"
                lines.append(head + "  columns: " + cols + more)
            elif suf in (".xlsx", ".xls", ".xlsm"):
                import pandas as _pd
                xl = _pd.ExcelFile(p)
                lines.append(head + f"  sheets: {', '.join(xl.sheet_names[:5])}"
                             + (f" (+{len(xl.sheet_names)-5})" if len(xl.sheet_names) > 5 else ""))
            elif suf == ".json":
                import json as _json
                try:
                    obj = _json.loads(p.read_text(encoding="utf-8", errors="replace")[:2048] + "}")
                except Exception:
                    obj = None
                if isinstance(obj, dict):
                    keys = ", ".join(str(k) for k in list(obj.keys())[:8])
                    lines.append(head + f"  keys: {keys}")
                else:
                    lines.append(head)
            elif suf in (".txt", ".md", ".rst", ".log", ".yaml", ".yml", ".toml", ".ini"):
                snippet = p.read_text(encoding="utf-8", errors="replace")[:80].replace("\n", " ")
                lines.append(head + f"  preview: {snippet}...")
            else:
                lines.append(head)
        except Exception:
            lines.append(head)

        if sum(len(ln) for ln in lines) > max_chars:
            lines.append("  ... (folder listing truncated)")
            break

    if truncated_n:
        lines.append(f"  ... ({truncated_n} additional files not shown — "
                     f"ask about specific files or subfolders to see them)")
    lines.append("[END FOLDER]")
    return "\n".join(lines)


def _render_duckdb_block(p):
    """DuckDB database — read-only listing of tables + columns + samples."""
    lines = [
        "(DuckDB database — already parsed into plain text below. "
        "Each table is shown with its columns and a few sample rows.)",
    ]
    try:
        import duckdb as _duckdb
    except Exception as exc:
        return ("(DuckDB file — install duckdb with `pip install duckdb` "
                f"to view: {exc!r})")
    try:
        con = _duckdb.connect(str(p), read_only=True)
        try:
            tnames = [r[0] for r in con.execute(
                "SELECT table_name FROM duckdb_tables() "
                "ORDER BY table_name LIMIT 20"
            ).fetchall() if r and r[0]]
            lines.append(f"Tables ({len(tnames)}): " + ', '.join(tnames))
            lines.append("")
            for tname in tnames[:10]:
                qname = '"' + tname.replace('"', '""') + '"'
                try:
                    rows = con.execute(f"DESCRIBE {qname}").fetchall()
                    cols = [r[1] for r in rows if r and r[1]]
                except Exception:
                    cols = []
                try:
                    nrows = con.execute(f"SELECT COUNT(*) FROM {qname}").fetchone()[0]
                except Exception:
                    nrows = None
                lines.append(f'Table "{tname}" ({nrows if nrows is not None else "?"} rows):')
                lines.append("  columns: " + ', '.join(cols))
                try:
                    sample = con.execute(
                        f"SELECT * FROM {qname} LIMIT 5"
                    ).fetchall()
                    if sample:
                        lines.append("  rows:")
                        for r in sample:
                            cells = []
                            for v in r:
                                cv = str(v).replace('\n', ' ').strip()
                                if len(cv) > 80:
                                    cv = cv[:77] + '...'
                                cells.append(cv)
                            lines.append('    ' + ' | '.join(cells))
                except Exception:
                    pass
                lines.append("")
        finally:
            con.close()
    except Exception as exc:
        lines.append(f"(could not open database: {exc!r})")
    return '\n'.join(lines).rstrip()


def _render_bson_block(p):
    """MongoDB BSON dump — list field names and a few sample documents."""
    lines = [
        "(MongoDB BSON file — already parsed into plain text below. "
        "Each line below is one document.)",
    ]
    try:
        import bson as _bson
    except Exception as exc:
        return ("(BSON file — install pymongo with `pip install pymongo` "
                f"to view: {exc!r})")
    try:
        with open(p, "rb") as fh:
            data = fh.read()
        docs = _bson.decode_all(data)
    except Exception as exc:
        return f"(BSON read failed: {exc!r})"
    lines.append(f"Total documents: {len(docs)}")
    # Field-name summary
    keys = set()
    for d in docs[:200]:
        if isinstance(d, dict):
            keys.update(d.keys())
    lines.append("Fields: " + ", ".join(sorted(map(str, keys))[:30]))
    lines.append("")
    lines.append("Sample documents:")
    import json as _j
    for i, d in enumerate(docs[:10]):
        try:
            line = _j.dumps(d, default=str, ensure_ascii=False)
        except Exception:
            line = repr(d)
        if len(line) > 300:
            line = line[:297] + "..."
        lines.append(f"  {i+1}. {line}")
    return '\n'.join(lines)


# Small memo so a file read once during injection isn't re-opened and
# re-parsed a second time in the same turn (the provenance pass reads the same
# explicit paths). Keyed on (resolved path, mtime_ns, size) — a single stat()
# is far cheaper than re-parsing a 50k-row CSV, and the key invalidates when
# the file changes across turns. None results (missing/protected/dir) are NOT
# cached, so protection is always re-checked.
_FILE_INJECT_CACHE: dict = {}
_FILE_INJECT_CACHE_MAX = 64


def _read_file_for_injection(path_str):
    """Memoizing wrapper over ``_read_file_for_injection_uncached`` (see it for
    the real read + the protected-path guard). Returns byte-identical blocks;
    falls through to the uncached read whenever the path can't be stat'd."""
    try:
        p = Path(str(path_str).strip())
        st = p.stat()
        key = (str(p.resolve()).lower(), st.st_mtime_ns, st.st_size)
    except Exception:
        return _read_file_for_injection_uncached(path_str)
    hit = _FILE_INJECT_CACHE.get(key)
    if hit is not None:
        return hit
    block = _read_file_for_injection_uncached(path_str)
    if block is not None:
        if len(_FILE_INJECT_CACHE) >= _FILE_INJECT_CACHE_MAX:
            _FILE_INJECT_CACHE.clear()
        _FILE_INJECT_CACHE[key] = block
    return block


def _read_file_for_injection_uncached(path_str):
    """Read a file into a compact prompt block. CSVs get headers + sample
    rows; large rows are abbreviated so the header line always survives the
    model's context window.

    HARD GUARD: refuses to read anything under a protected vault subdir
    (conversation_logs, etc.). The model must never see those files.
    """
    try:
        import csv as _csv_fi
        p = Path(path_str.strip())
        if not p.exists() or not p.is_file():
            return None
        # Defense-in-depth: even if the user pastes a path inside
        # conversation_logs/, we refuse to inject it.
        try:
            import conversation_logger as _cl
            if _cl.is_protected_path(p, _vault()):
                return None
        except Exception:
            pass
        suffix = p.suffix.lower()
        if suffix == '.csv':
            # Bound the read: previously this did `list(reader)` of the
            # entire file, which on a 500MB / 5M-row CSV materialises
            # hundreds of MB of Python tuples and can OOM the GUI. The
            # injection only needs ~24 rows (head + middle + tail), so
            # we cap reading at 50k rows. For larger files the model
            # sees the shape of the first 50k rows and is told the
            # actual file is bigger — the analyst is the right tool
            # for anything that needs full-file counts.
            import itertools as _it
            CSV_ROW_HARD_CAP = 50_000
            rows: list = []
            csv_truncated = False
            try:
                with open(p, newline='', encoding='utf-8', errors='replace') as fh:
                    reader = _csv_fi.reader(fh)
                    # Read up to cap + 1 so we can detect overflow
                    for i, row in enumerate(_it.islice(reader, CSV_ROW_HARD_CAP + 1)):
                        if i >= CSV_ROW_HARD_CAP:
                            csv_truncated = True
                            break
                        rows.append(row)
            except Exception:
                # Fall through with whatever we managed to read
                pass
            if not rows:
                return None
            header = rows[0]
            data_rows = rows[1:]
            total_rows = len(data_rows)
            if csv_truncated:
                # Surface the cap to the model — without this it would
                # report "total rows: 49,999" as if that were the file's
                # actual size, which is a subtle hallucination.
                total_rows_str = f"{total_rows:,}+ (file exceeds the {CSV_ROW_HARD_CAP:,}-row injection cap; ask the analyst for exact full-file counts)"
            else:
                total_rows_str = f"{total_rows:,}"

            lines = ['Columns (' + str(len(header)) + '): ' + ', '.join(header)]
            lines.append('Total data rows: ' + total_rows_str)

            def _fmt(r):
                cells = []
                for c in r:
                    c = str(c).replace('\n', ' ').strip()
                    if len(c) > 80:
                        c = c[:77] + '...'
                    cells.append(c)
                return ' | '.join(cells)

            sample_target = 24
            if total_rows <= sample_target:
                if total_rows:
                    lines.append(f'Sample rows ({total_rows}, full file):')
                    for r in data_rows:
                        lines.append(_fmt(r))
            else:
                head, mid, tail, sk_b, sk_a = _sample_rows_head_mid_tail(
                    data_rows, sample_target,
                )
                shown = len(head) + len(mid) + len(tail)
                lines.append(
                    f'Sample rows ({shown} of {total_rows} — HEAD + MIDDLE + '
                    f'TAIL sampled; middle rows elided):'
                )
                lines.append('--- head ---')
                for r in head:
                    lines.append(_fmt(r))
                if mid:
                    lines.append(f'... [skipped {sk_b:,} rows] ...')
                    lines.append('--- middle ---')
                    for r in mid:
                        lines.append(_fmt(r))
                if tail:
                    lines.append(f'... [skipped {sk_a:,} rows] ...')
                    lines.append('--- tail ---')
                    for r in tail:
                        lines.append(_fmt(r))
            content = '\n'.join(lines)
            if len(content) > _FILE_READ_CHAR_LIMIT:
                content, _ = _smart_truncate_text(content, _FILE_READ_CHAR_LIMIT)
        elif suffix in ('.tsv',):
            # TSV — same rendering as CSV, just tab-separated
            import pandas as _pd_fi
            df = _pd_fi.read_csv(p, sep='\t', nrows=200, on_bad_lines='skip')
            content = _render_dataframe_block('TSV', df)
        elif suffix == '.parquet':
            try:
                import pandas as _pd_fi
                df = _pd_fi.read_parquet(p).head(200)
                content = _render_dataframe_block('Parquet', df)
            except Exception as _ex:
                content = (f"(Parquet file — could not read: {_ex!r}. "
                           f"Install pyarrow with `pip install pyarrow`.)")
        elif suffix == '.gz' and p.stem.lower().endswith('.csv'):
            import pandas as _pd_fi
            df = _pd_fi.read_csv(p, nrows=200, on_bad_lines='skip', compression='infer')
            content = _render_dataframe_block('Gzipped CSV', df)
        elif suffix in ('.db', '.sqlite', '.sqlite3'):
            content = _render_sqlite_block(p)
        elif suffix == '.duckdb':
            content = _render_duckdb_block(p)
        elif suffix == '.bson':
            content = _render_bson_block(p)
        elif suffix in ('.xlsx', '.xls', '.xlsm'):
            content = _render_excel_block(p, _FILE_READ_CHAR_LIMIT)
        else:
            # Read up to 2× the budget so we can show head + tail with a
            # visible elision marker (silent tail-clip caused the model
            # to invent the "missing" portion).
            with open(p, encoding='utf-8', errors='replace') as fh:
                raw = fh.read(_FILE_READ_CHAR_LIMIT * 2)
            if len(raw) > _FILE_READ_CHAR_LIMIT:
                content, _ = _smart_truncate_text(raw, _FILE_READ_CHAR_LIMIT)
            else:
                content = raw
        if not content.strip():
            return None
        return '[FILE: ' + p.name + ']\n' + content + '\n[END FILE]'
    except Exception:
        return None


def _extract_file_paths(text):
    """Find file paths in the user message. Protected paths (conversation
    logs) are filtered before they reach injection — see PROTECTED_SUBDIRS."""
    import unicodedata as _ud
    clean_chars = []
    for ch in text:
        if ch in ('\\', '/', '.', '~', ':'):
            clean_chars.append(ch)
        elif _ud.category(ch) in ('Pi', 'Pf', 'Po', 'Ps', 'Pe') or ch in ('<', '>'):
            clean_chars.append(' ')
        else:
            clean_chars.append(ch)
    clean = ''.join(clean_chars)
    paths = []
    seen = set()
    for m in _FILE_PATH_RE.finditer(clean):
        candidate = m.group(0).strip()
        while candidate and not (candidate[-1].isalnum() or candidate[-1] in '\\/'):
            candidate = candidate[:-1]
        # Expand ~ HERE, once, rather than in each consumer: all three call
        # sites do a bare Path(...), so an unexpanded "~/data/x.csv" is a
        # path that cannot be opened and would be reported to the model as
        # missing — the exact wrong answer this regex change is fixing.
        if candidate.startswith('~'):
            try:
                candidate = str(Path(candidate).expanduser())
            except Exception:
                pass
        if candidate and candidate not in seen:
            seen.add(candidate)
            paths.append(candidate)
    # HARD GUARD — drop any path under a protected vault subdir before
    # it can reach the injection layer. The model must never see files
    # under conversation_logs/.
    try:
        import conversation_logger as _cl
        paths = [p for p in paths if not _cl.is_protected_path(p, _vault())]
    except Exception:
        pass
    return paths


# ── Session "recently referenced files" registry ────────────────────────────
# When a command lists files to the user (e.g. "files associated with job 456"),
# the console records their ABSOLUTE paths here, keyed by lowercase basename. A
# follow-up that names one ("summarize report.csv") then resolves to the real
# file even though _FILE_PATH_RE matches only absolute paths and the file lives
# in a subfolder. Populated by CouncilConsole._remember_files; read by the
# injector. This is the app's short-term memory of "the files I just showed you".
_REFERENCED_FILES_MAP = None


def _register_referenced_files(m) -> None:
    global _REFERENCED_FILES_MAP
    _REFERENCED_FILES_MAP = m


def _extract_bare_filenames(text):
    """Bare filename tokens (name.ext) the user typed. _FILE_PATH_RE only
    catches absolute paths, so this catches 'report.csv' in a follow-up. Over-
    matching (e.g. 'e.g') is harmless — only tokens that resolve to a real file
    are used by the caller."""
    out, seen = [], set()
    for m in _re.finditer(r"[\w\-]{1,60}\.[A-Za-z0-9]{1,8}", text or ""):
        tok = m.group(0)
        low = tok.lower()
        if low not in seen:
            seen.add(low)
            out.append(tok)
    return out


def _resolve_referenced_path(name):
    """Resolve a bare filename / path to an existing absolute path:
    (1) keep it if it already exists; (2) the session referenced-files registry
    by basename (the file the user was just shown); (3) a vault filename lookup
    under data_in. Returns the input unchanged if nothing resolves."""
    try:
        p = Path(str(name)).expanduser()
        if p.is_absolute() and p.exists():
            return str(p)
    except Exception:
        return str(name)
    base = p.name.lower()
    state = _ACTIVE.get()                      # [turn_context] a session's own
    referenced = (state.referenced_files if state is not None
                  else _REFERENCED_FILES_MAP)
    if referenced:
        hit = referenced.get(base)
        try:
            if hit and Path(hit).exists():
                return hit
        except Exception:
            pass
    try:
        import data_index as _di
        root = _di.input_dir(_vault())
        for q in root.rglob(p.name):
            if q.is_file():
                return str(q)
        low = p.name.lower()
        for q in root.rglob("*"):
            if q.is_file() and q.name.lower() == low:
                return str(q)
    except Exception:
        pass
    return str(name)


# Phrase substrings that mark a CONCEPTUAL search query — "show me
# files that …", "find scripts about …", etc. When matched, the
# injector defaults to search-headers mode (compact one-line entries
# per match) rather than full [VAULT MATCH] blocks. The model
# typically wants to acknowledge what's there before zooming into
# specific files, and headers let it see far more matches per token.
_SEARCH_HEADERS_PHRASES = (
    "find files", "find any file", "find every", "find a file",
    "find scripts", "find pipelines", "find docs",
    "look through", "look for files",
    "search through", "search for files", "search for any",
    "show me files", "show me the files", "list files", "list of files",
    "which files", "what files", "any files",
    "files that contain", "files with", "files about",
    "scripts that", "pipelines that",
)


def _wants_search_headers(text: str) -> bool:
    """Heuristic: does this query look like 'show me what's there' rather
    than 'tell me about X specifically'? Conceptual-search queries get
    compact one-line headers instead of full blocks so the model can see
    more matches per token. Manual override via
    COUNCIL_FORCE_FULL_VAULT_BLOCKS=1 (or =yes/true/on) skips this and
    keeps the legacy full-block behaviour."""
    if os.environ.get("COUNCIL_FORCE_FULL_VAULT_BLOCKS", "").strip().lower() in (
        "1", "true", "yes", "on"
    ):
        return False
    t = (text or "").lower()
    return any(p in t for p in _SEARCH_HEADERS_PHRASES)


def _build_search_header_block(rec, score=None) -> str:
    """A single-line header for a vault match. ~30 tokens.

    Format: ``name  ·  type, R rows  ·  topics: a, b, c  ·  score 4.2``
    Used by search-headers mode in place of a full [VAULT MATCH] block.

    Semantic-only matches (no keyword hit) get a leading "[semantic] "
    tag so the model knows the file matched by meaning, not by query
    terms appearing in it.
    """
    name = rec.get("name") or "?"
    rtype = rec.get("type") or "?"
    bits = [rtype]
    if rtype in ("csv", "tsv", "csv.gz", "parquet"):
        rows = rec.get("rows")
        if isinstance(rows, int):
            bits.append(f"{rows:,} rows")
        n_cols = len(rec.get("headers") or [])
        if n_cols:
            bits.append(f"{n_cols} cols")
    elif rtype == "excel":
        sheets = rec.get("sheets") or []
        bits.append(f"{len(sheets)} sheets")
    elif rtype in ("sqlite", "duckdb"):
        tables = rec.get("tables") or []
        bits.append(f"{len(tables)} tables")
    topics = rec.get("topics") or []
    sem_tag = "[semantic] " if rec.get("_semantic_only") else ""
    parts = [f"{sem_tag}{name}", ", ".join(bits)]
    if topics:
        parts.append("topics: " + ", ".join(str(t) for t in topics[:4]))
    if score is not None:
        parts.append(f"score {score:.1f}")
    return "  ·  ".join(parts)


def _inject_file_contents(user_text, analyst_block=None, n_ctx=None,
                           task_memo_block=None, pinned_files=None):
    """Public entry point — wraps `_inject_file_contents_impl` in a
    defensive try/except so unexpected exceptions during injection
    (vault index corruption, network-share disconnect mid-walk,
    broken pdf parser, etc.) degrade to "no injection" rather than
    crashing `_send` and leaving the transcript hung on the user's
    last typed line.
    """
    try:
        return _inject_file_contents_impl(
            user_text, analyst_block=analyst_block, n_ctx=n_ctx,
            task_memo_block=task_memo_block,
            pinned_files=pinned_files,
        )
    except Exception as _top_e:
        import sys as _sys_dbg
        import traceback as _tb
        print(f"[inject] unexpected top-level exception: {_top_e!r}",
              file=_sys_dbg.stderr)
        _tb.print_exc(file=_sys_dbg.stderr)
        # Build a minimal breakdown so the caller doesn't crash on
        # `_injection_breakdown.get("costs", [])`.
        try:
            import council_engine as _ce_safe
            n_ctx_safe = int(n_ctx if n_ctx is not None else _ce_safe.get_n_ctx())
        except Exception:
            n_ctx_safe = int(n_ctx or 4096)
        breakdown = {
            "costs": [],
            "dropped": [],
            "n_ctx": n_ctx_safe,
            "remaining": n_ctx_safe,
            "per_block_cap": 2048,
            "running": 0,
            "user_text_tokens": 0,
            "injection_error": repr(_top_e),
        }
        # Optionally surface the failure as a synthetic NO_DATA-style
        # block so the model is told context retrieval failed and
        # refuses to invent values from training memory.
        warn = (
            "[INJECTION FAILURE — the vault / file readers raised an "
            "unexpected error while gathering context for this query.]\n"
            f"  error: {_top_e!r}\n"
            "[Treat this turn as if NO data has been provided. Do NOT "
            "invent specific values, file names, or row counts.]"
        )
        return (warn + "\n\n" + (user_text or "")), {}, breakdown


def _inject_file_contents_impl(user_text, analyst_block=None, n_ctx=None,
                                task_memo_block=None, pinned_files=None):
    """Augment the user message with file/vault context before deliberation.

    Returns ``(augmented_text, fuzzy_matches, breakdown)`` where:
      • ``breakdown["costs"]`` — list of ``(label, token_cost)`` for each
        block that made it into the assembled prompt (in order).
      • ``breakdown["dropped"]`` — list of ``(label, token_cost)`` for
        blocks that exceeded the cumulative budget. Only droppable-class
        blocks (vault matches) ever appear here.
      • ``breakdown["n_ctx"]`` / ``["remaining"]`` — the window the
        assembly was sized against.

    Priority order (lower number = higher priority; ties impossible):
      0. ``[NO DATA AVAILABLE]`` marker (must always surface)
      1. ``[TASK MEMO]`` — RAM-resident sticky note (goal/constraints/
         forbidden) so small models don't forget the original question
         once the context window fills up with file blocks
      2. ``[ANALYST RESULT]`` — the computed answer
      3. ``[FILE: ...]`` — explicit user-pasted paths
      4. ``[FOLDER: ...]`` — explicit user-pasted directory paths
      5. ``[VAULT MATCH: ...]`` — speculative search hits (droppable)

    Per-block cap: each block is head/tail-trimmed to roughly ``n_ctx//8``
    tokens before assembly. Cumulative cap: vault-match blocks stop being
    added once the running total would exceed the safe input budget
    (``n_ctx`` minus ``max(256, n_ctx*0.25)`` reply reserve, minus the
    typed user text and a 500-token writer-prompt overhead).

    ``analyst_block`` is optional and injected at priority 2 when given.
    When explicit paths are present, the vault search is skipped entirely
    (#4 — explicit paths take precedence; the user can re-ask without a
    path if they want fuzzy vault matches).
    """
    import sys as _sys_dbg
    print('[DEBUG inject] called with: ' + repr(user_text[:80]), file=_sys_dbg.stderr)

    # -- Budgets ------------------------------------------------------------
    try:
        import council_engine as _ce
        n_ctx = int(n_ctx if n_ctx is not None else _ce.get_n_ctx())
    except Exception:
        n_ctx = int(n_ctx or 4096)
    # Per-block cap is a *safety net* against pathological inputs (e.g. a
    # 1MB file that slipped past the renderer's own char-limit). The
    # renderers already cap themselves at ~12KB (~3,000 tokens), so the
    # cap floor of 2048 tokens means well-behaved blocks pass through
    # untouched while a runaway block still gets trimmed.
    # Per-block cap. Was max(2048, n_ctx // 4) which floored at 2048 for
    # n_ctx <= 8192 — meaning a single non-droppable block could eat 50%
    # of a 4K context, and several stacked could push past the window
    # entirely (the assembly only drops blocks at DROPPABLE_FROM and
    # above). Scale linearly with n_ctx so a 4K-ctx model gets ~512-token
    # blocks while a 32K-ctx model still gets 5K-token blocks.
    per_block_cap = max(512, n_ctx // 6)
    reply_reserve = max(256, int(n_ctx * 0.25))
    safe_input = max(1, n_ctx - reply_reserve)
    user_cost = _estimate_block_tokens(user_text)
    writer_overhead = 500   # rough Writer system prompt allowance
    remaining = max(256, safe_input - user_cost - writer_overhead)

    # Priority bands — lower = higher priority
    # VAULT_SUMMARY lists EVERY matching file by name (with brief
    # metadata: type, row count, topics) so the model knows the full
    # set even when individual VAULT MATCH content blocks get dropped
    # to fit the budget. Slotted ABOVE individual matches so the
    # filename list always reaches the model. Not droppable; tiny in
    # tokens (~15/file × ~50 files = ~750 tokens worst case).
    (PRIO_NODATA, PRIO_TASK_MEMO, PRIO_ANALYST,
     PRIO_EXPLICIT, PRIO_FOLDER,
     PRIO_VAULT_SUMMARY, PRIO_VAULT) = 0, 1, 2, 3, 4, 5, 6
    DROPPABLE_FROM = PRIO_VAULT   # only individual vault matches are droppable

    explicit_paths = _extract_file_paths(user_text)
    # Resolve BARE filenames the user names in a follow-up ("summarize
    # report.csv"): _FILE_PATH_RE only matches absolute paths, so a bare name in
    # a subfolder used to be invisible and the model refused. Look each up in
    # the session's recently-referenced files, then a vault filename search, and
    # inject the resolved file. Protected/app-state files are never injected.
    try:
        import conversation_logger as _cl_ref
    except Exception:
        _cl_ref = None
    for _bare in _extract_bare_filenames(user_text):
        _rp = _resolve_referenced_path(_bare)
        try:
            if (_rp and _rp not in explicit_paths and Path(_rp).is_file()
                    and (_cl_ref is None
                         or not _cl_ref.is_protected_path(_rp, _vault()))):
                explicit_paths.append(_rp)
        except Exception:
            pass
    print('[DEBUG inject] explicit paths: ' + str(explicit_paths), file=_sys_dbg.stderr)

    candidates = []   # list of (priority, label, content)
    fuzzy_matches = {}
    missing_paths = []

    # -- Task memo (priority 1) ----------------------------------------------
    # A short RAM-resident sticky note carrying the user's original goal +
    # constraints + forbidden actions. Re-injected on every turn so even
    # if later context blocks push the user's typed message past the
    # window's tail, the model still sees what was asked. ~60-100 tokens.
    if task_memo_block:
        candidates.append((PRIO_TASK_MEMO, "[TASK MEMO]", task_memo_block))

    # -- Analyst result (priority 2) ----------------------------------------
    if analyst_block:
        candidates.append((PRIO_ANALYST, "[ANALYST RESULT]", analyst_block))

    # -- Explicit paths (priority 3 or 4) -----------------------------------
    # Rich folder rendering (per-file column previews / sheet names / JSON
    # key heads) is ~12 KB worst-case; compact rendering (counts + filename
    # list) is ~3.5 KB. On a tight 4 K-ctx model, picking rich for a pasted
    # folder eats the whole window. We pick adaptively:
    #   • n_ctx >= 16384 → rich (the original behaviour)
    #   • n_ctx <  16384 → compact (matches the auto-injection path's choice)
    # The user can always paste an individual file path inside the folder
    # to get full rich rendering for that one file.
    _use_compact_folder = n_ctx < 16384
    for path_str in explicit_paths:
        p_check = Path(path_str.strip())
        # Directory → folder listing block (priority 4, not droppable).
        if p_check.is_dir():
            if _use_compact_folder:
                folder_block = _render_folder_summary_compact(p_check)
                lbl_prefix = "[FOLDER SUMMARY: "
            else:
                folder_block = _render_folder_for_injection(p_check)
                lbl_prefix = "[FOLDER: "
            if folder_block:
                candidates.append((
                    PRIO_FOLDER, f"{lbl_prefix}{p_check.name or path_str}]",
                    folder_block,
                ))
            continue
        snippet = _read_file_for_injection(path_str)
        if snippet:
            candidates.append((
                PRIO_EXPLICIT, f"[FILE: {p_check.name or path_str}]", snippet,
            ))
        else:
            missing_paths.append(path_str)

    # -- Vault folder context (priority 4) ----------------------------------
    # When the user mentions "the vault", "my vault", "vault folder", etc.
    # but didn't paste an explicit path, inject a COMPACT data_in/ summary
    # (counts + subfolder breakdown + filename list — no per-file column
    # previews) so the model knows what files exist. Without this, vault
    # search may return zero matches for generic queries ("show me what's
    # in the vault") and the model defaults to "I don't have access to
    # your filesystem".
    #
    # Skipped when:
    #   • the user pasted an explicit path (FILE/FOLDER block already
    #     covers their intent)
    #   • the analyst block is present (analyst has authoritative counts;
    #     adding a second full listing wastes the prompt budget — this
    #     was the crash trigger on the 4K-ctx laptop: "how many files in
    #     data_in" → analyst answers, then injection ALSO rendered the
    #     full folder = 3K+ tokens of duplicate context.)
    #
    # Compact renderer caps at ~3.5 KB even on 1000-file vaults, so a
    # 4 K-ctx model has headroom for the user text + reply reserve.
    #
    # Rebuild the vault index ONCE per turn when we'll consult it (no explicit
    # paths). The folder-summary render(s) below and the vault-search branch
    # each used to trigger their own rebuild() on the same singleton — 2-3 full
    # rglob walks + a stat() per file per turn. rebuild() is delta-aware and
    # idempotent, so one consolidated call yields byte-identical folder + search
    # blocks. Passed into the renders via vault_index= so they skip re-walking.
    _vault_idx = None
    if not explicit_paths:
        _vault_idx = _get_vault_index()
        if _vault_idx is not None:
            try:
                import council_engine as _ce_tim0
                with _ce_tim0._TimingScope("vault.rebuild"):
                    _vault_idx.rebuild()
            except Exception as _rb_exc:
                print('[DEBUG inject] vault index rebuild failed: '
                      + repr(_rb_exc), file=_sys_dbg.stderr)
    if (_vault_search_keywords(user_text)
            and not explicit_paths
            and analyst_block is None):
        try:
            import data_index as _di
            vault_data_in = _di.input_dir(_vault())
        except Exception:
            vault_data_in = _vault()
        try:
            vault_folder_block = _render_folder_summary_compact(
                vault_data_in, vault_index=_vault_idx)
        except Exception as _e:
            print('[DEBUG inject] vault folder render failed: ' + repr(_e),
                  file=_sys_dbg.stderr)
            vault_folder_block = None
        if vault_folder_block:
            vf_label = f"[FOLDER SUMMARY: {Path(vault_data_in).name or str(vault_data_in)}]"
            already_have_vault_folder = any(
                lbl == vf_label for _prio, lbl, _content in candidates
            )
            if not already_have_vault_folder:
                candidates.append((PRIO_FOLDER, vf_label, vault_folder_block))
                print('[DEBUG inject] vault folder injected: '
                      + str(vault_data_in), file=_sys_dbg.stderr)

        # Sub-folder reference: user said something like "look in the
        # projects subfolder" or "files in Q3_2024". Compact-render
        # that subfolder so the user gets focused context.
        try:
            import vault_analyst as _va_sub
            sub = _va_sub.resolve_subfolder_hint(user_text, vault_data_in)
        except Exception:
            sub = None
        if sub is not None and sub != vault_data_in:
            try:
                sub_block = _render_folder_summary_compact(
                    sub, vault_index=_vault_idx)
            except Exception:
                sub_block = None
            if sub_block:
                sub_label = f"[FOLDER SUMMARY: {sub.name or str(sub)}]"
                already = any(lbl == sub_label
                              for _prio, lbl, _content in candidates)
                if not already:
                    candidates.append((PRIO_FOLDER, sub_label, sub_block))
                    print('[DEBUG inject] vault subfolder injected: '
                          + str(sub), file=_sys_dbg.stderr)

    # -- Vault search (priority 5, droppable) -------------------------------
    # #4: when the user pasted explicit paths, trust them and skip vault
    # search entirely. The vault hits competed with the explicit file for
    # budget and frequently caused the explicit file to get truncated.
    folder_scope = _detect_folder_scope(user_text)
    if not explicit_paths:
        idx = _vault_idx   # rebuilt once at the top of this region
        if idx is not None:
            try:
                # When analyst already answered (#7), reduce the vault-match
                # pull from 5 to 1 — the analyst's CSV is the authoritative
                # source and 5 fuzzy matches just consume budget.
                #
                # K and TAIL_K both scale with n_ctx so a 4 K-ctx model
                # doesn't get 5 × ~400-token VAULT MATCH blocks (2 KB just
                # in matches, half its window). 8 K → 3 / 30, 16 K → 5 / 45.
                if n_ctx <= 4096:
                    base_k, base_tail = 2, 15
                elif n_ctx <= 8192:
                    base_k, base_tail = 3, 30
                else:
                    base_k, base_tail = 5, 45
                k = 1 if analyst_block else base_k
                # Semantic expansion via the local model: when a query
                # term isn't in the vault's vocab, the index calls the
                # model to ask "which of these vocab tokens belong in
                # the queried category?" The model decides per-vault
                # (so "metals" returns "promethium" only if it's
                # actually in this user's files). Cached on disk; one
                # call per novel concept ever.
                def _semantic_llm_call(prompt: str) -> str:
                    import council_engine as _ce_sem
                    return _ce_sem.local_chat(
                        messages=[{"role": "user", "content": prompt}],
                        temperature=0.0,
                        num_predict=180,
                        timeout=45,
                    )
                # We pull k + TAIL_K total hits. The top-k get FULL
                # [VAULT MATCH] content blocks; the rest go into a
                # single compact [VAULT SEARCH SUMMARY] block that
                # lists every matching file by name + brief metadata.
                # This is the fix for the "model only sees 5 files
                # even though 47 match" failure mode: the summary is
                # ~15 tokens per file, so 50 files cost ~750 tokens
                # vs ~2000 tokens for 5 full blocks. The model now
                # KNOWS the full set of matching files even when only
                # a few get full content.
                TAIL_K = base_tail
                # NOTE: `_ce_tim` was referenced here but never imported
                # (introduced by the "behavior-preserving" perf batch c613e75),
                # so this whole block raised NameError on EVERY turn and the
                # outer except swallowed it — vault search was silently dead.
                # Import the alias locally, matching _ce_sem / _ce_pack nearby.
                import council_engine as _ce_tim
                with _ce_tim._TimingScope("vault.search"):
                    all_hits, fuzzy_matches = idx.search(
                        user_text, k=k + TAIL_K, folder=folder_scope,
                        llm_call=_semantic_llm_call,
                    )
                # Drop any hit that's already explicit (won't happen
                # in practice — we skip vault search entirely when
                # explicit_paths is non-empty — but safe to keep the
                # filter for future call sites).
                all_hits = [(s, r) for s, r in all_hits
                            if r.get("path") not in explicit_paths]
                full_hits = all_hits[:k]
                tail_hits = all_hits[k:]

                print('[DEBUG inject] vault hits: total='
                      + str(len(all_hits))
                      + ' full=' + str(len(full_hits))
                      + ' tail=' + str(len(tail_hits)),
                      file=_sys_dbg.stderr)
                if fuzzy_matches:
                    print('[DEBUG inject] fuzzy: ' + repr(fuzzy_matches),
                          file=_sys_dbg.stderr)

                # Decide between full [VAULT MATCH] blocks and compact
                # search-headers, based on intent. Conceptual queries
                # ("show me files about X", "find scripts that …") get
                # one-line headers: the model sees a wider set of
                # matches per token and can ask follow-up "tell me more
                # about file Y" in the next turn — those references
                # will trigger the zoom-in via pinned_files (see
                # CouncilConsole._send wire-up).
                use_search_headers = _wants_search_headers(user_text)

                # Pinned files from a previous turn — the user's last
                # answer referenced these by name, so promote them out
                # of headers mode into full blocks even when the
                # current query looks conceptual. Pins expire after a
                # few turns to avoid bloat (managed by the caller).
                pinned_set = {str(n).lower() for n in (pinned_files or [])}

                try:
                    import council_engine as _ce_pack
                    _count_tokens = _ce_pack.estimate_tokens
                except Exception:
                    _count_tokens = None
                vault_block_budget = max(256, remaining // 2)

                if use_search_headers and not pinned_set:
                    # Pure headers mode — no per-file rich blocks.
                    # Pack everything we found into one compact block.
                    header_lines = ["[VAULT MATCHES (headers — ask about a "
                                     "file by name to zoom in)]"]
                    for score, rec in all_hits:
                        header_lines.append("  • " + _build_search_header_block(rec, score))
                    headers_block = "\n".join(header_lines)
                    candidates.append((
                        PRIO_VAULT, "[VAULT MATCHES — headers]", headers_block,
                    ))
                    print('[DEBUG inject] search-headers mode: '
                          + str(len(all_hits)) + ' headers',
                          file=_sys_dbg.stderr)
                else:
                    # Full-block mode (legacy + pinned-zoom path).
                    # Split hits into "pinned" (always full) and
                    # "everyone else" (headers if search-headers mode).
                    pinned_recs = []
                    other_full_hits = []
                    for score, rec in full_hits:
                        if str(rec.get("name", "")).lower() in pinned_set:
                            pinned_recs.append(rec)
                        else:
                            other_full_hits.append((score, rec))

                    packed_blocks, pack_diag = idx.assemble_match_blocks(
                        pinned_recs + [rec for _s, rec in other_full_hits],
                        budget_tokens=vault_block_budget,
                        count_tokens=_count_tokens,
                    )
                    print('[DEBUG inject] vault match assembly: '
                          + repr(pack_diag), file=_sys_dbg.stderr)
                    ordered_recs = pinned_recs + [r for _s, r in other_full_hits]
                    for rec, block in zip(ordered_recs, packed_blocks):
                        name = rec.get("name") or "?"
                        label_prefix = ("[VAULT MATCH (pinned): "
                                        if str(name).lower() in pinned_set
                                        else "[VAULT MATCH: ")
                        candidates.append((PRIO_VAULT, f"{label_prefix}{name}]", block))

                # Summary block — emitted ONLY when not in headers mode,
                # because the headers block already covers the same
                # ground (compact per-file listing). When tail_hits is
                # empty the summary is redundant; when search-headers
                # is on, ALL hits already render as headers, so the
                # tail summary is also redundant.
                if tail_hits and not use_search_headers:
                    summary_block = _build_vault_search_summary(
                        all_hits, full_count=len(full_hits),
                    )
                    candidates.append((
                        PRIO_VAULT_SUMMARY,
                        "[VAULT SEARCH SUMMARY]",
                        summary_block,
                    ))
            except Exception as _e:
                print('[DEBUG inject] vault search failed: ' + repr(_e),
                      file=_sys_dbg.stderr)

    # -- Cell-value matches (compact, high-priority) ------------------------
    # The vault search above is filename / header / topic / embedding biased,
    # so a value that lives INSIDE a file (a cell in row 400, a code, a name)
    # frequently isn't surfaced — the app's core "can't find data inside my
    # files" complaint. Here we run the deterministic cell scanner
    # (data_index.search_value — pure pandas/dict, NO model call, so the
    # single serialized-inference lock is untouched) over the query's content
    # terms and fold real hits into the context as a compact block. Slotted at
    # PRIO_VAULT_SUMMARY: above droppable vault matches (these are ACTUAL cell
    # hits, more authoritative than fuzzy matches) and kept small by self-cap.
    if not explicit_paths and analyst_block is None:
        try:
            _val_terms = _content_query_terms(user_text)
            _di_inst = _get_data_index() if _val_terms else None
            if _di_inst is not None:
                try:
                    _di_inst.refresh()
                except Exception:
                    pass
                _val_merged: dict = {}
                _val_cov: dict = {}
                for _t in _val_terms:
                    try:
                        # search_value STREAMS every row now; the 5,000-row
                        # cache is only a preview sample. Before, a value past
                        # row 5,000 was invisible while the file still called
                        # itself 40,000 rows, so the model was handed a
                        # confident "not found".
                        _vhits = _di_inst.search_value(_t, max_per_file=5,
                                                       stats=_val_cov)
                    except Exception:
                        _vhits = []
                    for _h in _vhits:
                        _ent = _val_merged.setdefault(_h["path"], {
                            "file": _h["file"], "cols": [], "rows": [],
                            "terms": set(), "matched": 0,
                        })
                        _ent["terms"].add(_t)
                        _ent["matched"] = max(_ent["matched"],
                                              _h.get("matched_count", 0))
                        for _c in _h["column_hits"]:
                            if _c not in _ent["cols"]:
                                _ent["cols"].append(_c)
                        for _r in _h["rows"]:
                            if len(_ent["rows"]) < 3:
                                _ent["rows"].append(_r)
                if _val_merged:
                    # Most terms matched + most rows first; cap files for size.
                    _ranked = sorted(
                        _val_merged.values(),
                        key=lambda e: (-len(e["terms"]), -len(e["rows"])),
                    )[:4]
                    _vlines = ["[VALUE MATCHES — rows inside your vault files "
                               "that contain the search terms]"]
                    # Tell the model the COVERAGE and the TRUE match counts, so
                    # it cannot present a sample as the whole story. The rows
                    # below are a sample; matched_count is the real total.
                    try:
                        _cov_line = data_index.DataIndex.search_coverage_line(
                            _val_cov)
                        if _cov_line:
                            _vlines.append(f"  ({_cov_line})")
                    except Exception:
                        pass
                    for _ent in _ranked:
                        _tot = _ent.get("matched", 0)
                        _shown = len(_ent["rows"])
                        _more = (f"; showing {_shown} of {_tot} matching rows"
                                 if _tot > _shown else "")
                        _vlines.append(
                            f"  {_ent['file']}  (matched in column(s): "
                            f"{', '.join(_ent['cols'][:6])}{_more})"
                        )
                        for _r in _ent["rows"]:
                            _cells = "  |  ".join(
                                f"{_k}={_v}" for _k, _v in _r.items()
                                if _v not in (None, "", "nan")
                            )
                            _vlines.append("    " + _cells[:160])
                    candidates.append(
                        (PRIO_VAULT_SUMMARY, "[VALUE MATCHES]", "\n".join(_vlines))
                    )
                    print('[DEBUG inject] value matches: '
                          + str(len(_ranked)) + ' file(s) from terms '
                          + repr(_val_terms), file=_sys_dbg.stderr)
        except Exception as _ve:
            print('[DEBUG inject] value search failed: ' + repr(_ve),
                  file=_sys_dbg.stderr)

    # -- Dataset overview (expert-mode grounding) ---------------------------
    # A compact, always-current map of ALL of data_in so the model knows the
    # whole dataset it is meant to be an expert on; the retrieval blocks above
    # then supply the specifics. Model-free + cached (rebuilds only when data_in
    # changes), size-scaled to n_ctx, and gated to real data questions.
    if (not explicit_paths and analyst_block is None
            and _content_query_terms(user_text)):
        try:
            import dataset_digest as _dd
            _digest_cap = (1200 if n_ctx <= 4096
                           else 2500 if n_ctx <= 8192 else 4000)
            _digest = _dd.get_digest(_vault(), max_chars=_digest_cap)
            if _digest:
                candidates.append((
                    PRIO_VAULT_SUMMARY, "[DATASET OVERVIEW]",
                    "[DATASET OVERVIEW — every data file currently in the "
                    "vault; ask about any of them]\n" + _digest))
                print('[DEBUG inject] dataset overview injected ('
                      + str(len(_digest)) + ' chars)', file=_sys_dbg.stderr)
        except Exception as _de:
            print('[DEBUG inject] dataset overview failed: ' + repr(_de),
                  file=_sys_dbg.stderr)

    # -- NO DATA marker (priority 1, never dropped) -------------------------
    # Critical defense against cross-machine hallucination: when the user
    # mentioned a path that doesn't exist locally, surface the gap loudly
    # so the Writer's ABSOLUTE RULE clause kicks in and refuses values.
    if missing_paths:
        miss_block = (
            "[NO DATA AVAILABLE — the user's message referenced these "
            "file paths, but they do NOT exist on this machine:]"
        )
        for mp in missing_paths:
            miss_block += "\n  - " + str(mp)
        miss_block += (
            "\n[Refuse to give specific values for these files. Tell "
            "the user the file is not present on this machine. Do NOT "
            "invent column names, row counts, or values from memory.]"
        )
        candidates.append((PRIO_NODATA, "[NO DATA AVAILABLE]", miss_block))

    # -- Assemble: priority sort, per-block cap, cumulative cap, tag --------
    candidates.sort(key=lambda t: t[0])

    placed: list = []   # list of (prio, label, capped, cost)
    dropped = []
    running = 0
    # NO_DATA / TASK_MEMO / ANALYST are exempt from the per-block cap:
    #   • NO_DATA is tiny by construction.
    #   • TASK_MEMO is intentionally short (~80 tokens) and trimming it
    #     would defeat the "remember the original question" guarantee.
    #   • ANALYST is already capped at ~4 KB by format_result_for_prompt;
    #     trimming further could elide the answer the user asked for.
    # VAULT_SUMMARY is uncapped on the per-block axis (it scales with hits
    # already) but DOES participate in cumulative-budget eviction below.
    UNCAPPED_PRIOS = {PRIO_NODATA, PRIO_TASK_MEMO, PRIO_ANALYST,
                      PRIO_VAULT_SUMMARY}
    # Truly undroppable — these must reach the model no matter what.
    # Everything else is sacrificeable in reverse-priority order if the
    # cumulative budget gets blown by stacked non-droppable blocks (the
    # old assembly let those overflow the window unconditionally).
    HARD_KEEP = {PRIO_NODATA, PRIO_TASK_MEMO, PRIO_ANALYST}
    for (prio, label, content) in candidates:
        if prio in UNCAPPED_PRIOS:
            capped = content
        else:
            capped = _smart_truncate_block_to_tokens(content, per_block_cap)
        cost = _estimate_block_tokens(capped)
        # First-pass cumulative gate — only droppable blocks are filtered
        # here. Non-droppable ones go into `placed` unconditionally and
        # the rescue pass below handles overflow.
        if prio >= DROPPABLE_FROM and running + cost > remaining:
            dropped.append((label, cost))
            continue
        placed.append((prio, label, capped, cost))
        running += cost

    # -- Cumulative-overflow rescue ------------------------------------------
    # The first pass enforces budget only for droppable blocks. If multiple
    # non-droppable blocks (EXPLICIT / FOLDER / VAULT_SUMMARY) stack up on
    # a small context window, `running` can still exceed `remaining` — the
    # crash mode reported on the 4 K-ctx laptop. Walk back over the placed
    # blocks in REVERSE priority order (lowest priority first), evicting
    # until the budget fits. HARD_KEEP blocks are never evicted.
    if running > remaining:
        # Before evicting an overflow block, try to CONDENSE it to fit —
        # chunk it and keep the parts most relevant to the user's task
        # (the [TASK MEMO]) instead of losing the whole block. This is the
        # "extend the model's effective context" path: a big file or wide
        # vault-match set survives in digest form on a small window.
        # Deterministic by default (no latency); COUNCIL_CONDENSE_LLM=1
        # switches to a model map-reduce. Disable with
        # COUNCIL_CONDENSE_OVERFLOW=0 to restore plain eviction.
        _condense_on = os.environ.get(
            "COUNCIL_CONDENSE_OVERFLOW", "1").strip().lower() \
            not in ("0", "false", "no", "off")
        _cond = None
        if _condense_on:
            try:
                import context_condenser as _cond
            except Exception:
                _cond = None
        _cond_llm = None
        if _cond is not None and os.environ.get(
                "COUNCIL_CONDENSE_LLM", "").strip().lower() in (
                "1", "true", "yes", "on"):
            def _cond_llm(_p):
                import council_engine as _cc
                return _cc.local_chat(
                    messages=[{"role": "user", "content": _p}],
                    temperature=0.0, num_predict=256, timeout=60)
        _task_src = task_memo_block or user_text or ""

        evictable_order = sorted(
            range(len(placed)),
            key=lambda i: (-placed[i][0], -i),
        )
        keep_mask = [True] * len(placed)
        for i in evictable_order:
            if running <= remaining:
                break
            prio_i, label_i, _capped_i, cost_i = placed[i]
            if prio_i in HARD_KEEP:
                continue
            # How much room this block may occupy if it's the one that fits.
            headroom = remaining - (running - cost_i)
            if _cond is not None and headroom >= 64:
                try:
                    condensed = _cond.condense_to_fit(
                        _capped_i, headroom, task=_task_src,
                        estimate_tokens=_estimate_block_tokens,
                        llm_call=_cond_llm)
                except Exception as _cx:
                    print('[inject] condense failed: ' + repr(_cx),
                          file=_sys_dbg.stderr)
                    condensed = None
                if condensed:
                    new_cost = _estimate_block_tokens(condensed)
                    if new_cost < cost_i and new_cost <= headroom:
                        placed[i] = (prio_i, label_i, condensed, new_cost)
                        running -= (cost_i - new_cost)
                        print(f'[inject] condensed {label_i}: '
                              f'{cost_i}->{new_cost} tok', file=_sys_dbg.stderr)
                        continue
            keep_mask[i] = False
            dropped.append((label_i + " (budget overflow)", cost_i))
            running -= cost_i
        placed = [b for i, b in enumerate(placed) if keep_mask[i]]

    # Re-sort by priority (eviction can reorder via mask removal — keep
    # the assembled order priority-stable for the model).
    placed.sort(key=lambda t: t[0])

    final_blocks = []
    per_block_costs = []
    for prio, label, capped, cost in placed:
        tagged = _tag_block_header(capped, cost)
        final_blocks.append(tagged)
        per_block_costs.append((label, cost))

    breakdown = {
        "costs":    per_block_costs,
        "dropped":  dropped,
        "n_ctx":    n_ctx,
        "remaining": remaining,
        "per_block_cap": per_block_cap,
        "running":  running,
        "user_text_tokens": user_cost,
    }

    if not final_blocks:
        return user_text, fuzzy_matches, breakdown
    return ('\n\n'.join(final_blocks) + '\n\n' + user_text,
            fuzzy_matches, breakdown)


_FOLDER_RE = _re.compile(
    r"(?:in(?:side)?|under|within|from)\s+(?:my|the)?\s*"
    r"(?:folder|directory|dir|subfolder)\s+"
    r"['\"]?([A-Za-z0-9_\-./\\]+)['\"]?",
    _re.IGNORECASE,
)
_FOLDER_KEYWORD_RE = _re.compile(
    r"\bfolder[: ]+['\"]?([A-Za-z0-9_\-./\\]+)['\"]?",
    _re.IGNORECASE,
)


def _build_vault_search_summary(all_hits, full_count: int) -> str:
    """Format a compact [VAULT SEARCH SUMMARY] block listing every
    matching file by name + brief metadata. ``full_count`` is how many
    of the hits also got an individual [VAULT MATCH] block above this
    one — so the summary can say "showing full content for the top N".

    Per-row format (keeps total cost ~15 tokens per file):
        N.  <name>   [type, R rows]   topics: t1, t2, t3
    or for non-tabular:
        N.  <name>   [type, K keys]   topics: ...

    The block always carries the TOTAL count and a hint that the
    user can ask about a specific file to get its full content.
    """
    total = len(all_hits)
    lines: list = ["[VAULT SEARCH SUMMARY]"]
    lines.append(f"Total files matching the query: {total}")
    if full_count > 0:
        lines.append(
            f"Full content for the top {full_count} match"
            f"{'es' if full_count != 1 else ''} is shown in the "
            f"[VAULT MATCH] blocks below. The remaining "
            f"{total - full_count} match"
            f"{'es are' if total - full_count != 1 else ' is'} "
            f"listed by filename only — ask about a specific file "
            f"by name to see its content."
        )
    else:
        lines.append("Each entry is listed by filename only — ask "
                     "about a specific file by name to see its content.")
    lines.append("")
    lines.append("Files matched (relevance-ordered):")

    for i, (_score, rec) in enumerate(all_hits, start=1):
        name = rec.get("name") or "?"
        rtype = rec.get("type") or "?"
        # Compact metadata that varies by type.
        meta_bits: list = [rtype]
        if rtype in ("csv", "tsv", "csv.gz", "parquet"):
            rows = rec.get("rows")
            if isinstance(rows, int):
                meta_bits.append(f"{rows:,} rows")
            n_cols = len(rec.get("headers") or [])
            if n_cols:
                meta_bits.append(f"{n_cols} cols")
        elif rtype == "excel":
            sheets = rec.get("sheets") or []
            meta_bits.append(f"{len(sheets)} sheets")
        elif rtype in ("sqlite", "duckdb"):
            tables = rec.get("tables") or []
            meta_bits.append(f"{len(tables)} tables")
        elif rtype in ("json", "d3dpipeline", "bson"):
            keys = rec.get("keys") or []
            if keys:
                meta_bits.append(f"{len(keys)} keys")
            if rec.get("indexing_tier") == "sampled_head_tail":
                meta_bits.append("sampled")

        topics = rec.get("topics") or []
        topic_str = ""
        if topics:
            preview = ", ".join(str(t) for t in topics[:5])
            if len(topics) > 5:
                preview += f", +{len(topics) - 5}"
            topic_str = f"  topics: {preview}"

        meta = ", ".join(meta_bits)
        lines.append(f"  {i:>3}. {name}   [{meta}]{topic_str}")

    lines.append("[END VAULT SEARCH SUMMARY]")
    return "\n".join(lines)


def _detect_folder_scope(text):
    """If the user references a folder by name, return that folder substring."""
    for rx in (_FOLDER_KEYWORD_RE, _FOLDER_RE):
        m = rx.search(text or "")
        if m:
            cand = m.group(1).strip().strip("'\"`")
            if "." not in Path(cand).name or Path(cand).suffix == "":
                return cand
    return None


_VAULT_TRIGGER_PHRASES = (
    "look through", "search through", "find files", "find every", "find any",
    "every file", "any file with", "all files", "across files",
    "in my vault", "in the vault", "from the vault", "the vault folder",
    "my vault folder", "vault folder", "vault directory", "the vault",
    "data_in", "data folder", "input folder",
    "what's in the vault", "whats in the vault", "what is in the vault",
    "files that contain", "files with", "which files",
    "list files", "list of files", "show files", "show me the files",
    "show me what's", "scan", "index",
)


def _vault_search_keywords(text):
    t = (text or "").lower()
    return any(p in t for p in _VAULT_TRIGGER_PHRASES)


_VAULT_INDEX_INSTANCE = None


def _get_vault_index():
    """Lazy-init the vault index. Returns None on import/setup failure."""
    global _VAULT_INDEX_INSTANCE
    state = _ACTIVE.get()                      # [turn_context] a session's own
    if state is not None:
        return state.get_vault_index()
    if _VAULT_INDEX_INSTANCE is not None:
        return _VAULT_INDEX_INSTANCE
    try:
        import vault_index as _vi
        _VAULT_INDEX_INSTANCE = _vi.VaultIndex(_vault())
    except Exception as _e:
        import sys as _sys_dbg
        print('[VaultIndex] init failed: ' + repr(_e), file=_sys_dbg.stderr)
        _VAULT_INDEX_INSTANCE = None
    return _VAULT_INDEX_INSTANCE


_DATA_INDEX_INSTANCE = None


def _register_data_index(di) -> None:
    """Let module-level helpers (the context injector's value-search stage)
    reuse the SAME DataIndex the console built + refreshes, instead of
    constructing a duplicate one."""
    global _DATA_INDEX_INSTANCE
    _DATA_INDEX_INSTANCE = di


def _get_data_index():
    """Return a DataIndex over the vault's data_in/ + bundled samples.

    Prefers the instance the console registered (already warm + refreshed);
    otherwise lazily builds one so headless / agent contexts still get value
    search. Returns None on failure — callers must degrade gracefully.
    """
    global _DATA_INDEX_INSTANCE
    state = _ACTIVE.get()                      # [turn_context] a session's own
    if state is not None:
        return state.get_data_index()
    if _DATA_INDEX_INSTANCE is not None:
        return _DATA_INDEX_INSTANCE
    try:
        import data_index as _di
        _DATA_INDEX_INSTANCE = _di.DataIndex(
            search_roots=[
                _di.input_dir(_vault()),
                _di.bundled_samples_dir(),
            ],
            write_root=_di.output_dir(_vault()),
        )
    except Exception as _e:
        import sys as _sys_dbg
        print('[DataIndex] module init failed: ' + repr(_e), file=_sys_dbg.stderr)
        _DATA_INDEX_INSTANCE = None
    return _DATA_INDEX_INSTANCE


# Stop-words for the value-search stage's content-term extraction. Module-level
# because the console's _query_keywords is a method, unavailable to the
# module-level injector. Broad by design: we want proper-noun / ID-like tokens
# likely to appear as CELL VALUES, not generic verbs or aggregate words (which
# describe an OPERATION on the data, not a value stored in it).
_VALUE_QUERY_STOPS = frozenset({
    "the", "a", "an", "of", "for", "by", "in", "on", "at", "and", "or", "to",
    "with", "what", "which", "who", "whose", "how", "many", "much", "do",
    "does", "did", "is", "are", "was", "were", "have", "has", "had", "from",
    "this", "that", "these", "those", "all", "any", "show", "tell", "give",
    "find", "list", "look", "up", "lookup", "search", "me", "my", "our",
    "your", "us", "i", "about", "across", "between", "into", "per", "over",
    "under", "most", "least", "more", "less", "than", "when", "where", "why",
    "value", "values", "row", "rows", "record", "records", "file", "files",
    "data", "column", "columns", "field", "fields", "get", "see", "please",
    "name", "named", "called",
    # Aggregate / analytic words describe an operation, not a stored value.
    "average", "avg", "total", "sum", "count", "mean", "median", "mode",
    "maximum", "minimum", "max", "min", "percentage", "percent", "number",
    "amount", "compare", "trend", "group", "grouped",
})


def _content_query_terms(text, *, max_terms: int = 6):
    """Content terms likely to be CELL VALUES: strip punctuation + stop-words,
    keep tokens length >= 3 (or ANY token containing a digit — an ID/code).
    Deduped, order-preserving, capped at ``max_terms``."""
    out: List[str] = []
    seen: Set[str] = set()
    for raw in (text or "").split():
        t = raw.strip(".,!?;:()[]{}\"'`").lower()
        if not t or t in _VALUE_QUERY_STOPS:
            continue
        if len(t) < 3 and not any(ch.isdigit() for ch in t):
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= max_terms:
            break
    return out


# ---- Data analyst step ----------------------------------------------------
# Computational questions ("how many", "what percentage", "average X") deserve
# real numbers, not the model's best guess from a sample. This step asks a
# local model to write pandas code calling our helpers, executes it in a
# locked-down sandbox, and returns the result as text the Writer can quote.

_FILE_COUNT_TRIGGERS = (
    "how many files", "how many data files", "how many csv",
    "how many csvs", "how many spreadsheets", "how many documents",
    "how many json", "number of files", "count of files",
    "count the files", "file count", "total files", "how many files are",
)
# Intents that need per-file work, not a census: "how many rows in the files".
_FILE_COUNT_EXCLUSIONS = ("row", "record", "column", "value", "cell")


def looks_like_file_count(query) -> bool:
    """Is this a plain 'how many files' census question?

    Module-level because TWO places need the same answer and they used to
    disagree. The analyst owns the deterministic file-count route - a cheap
    os.walk census, no model, no file reads - and it is correct. But the vault
    intent chain runs FIRST, and its field routes were claiming the question
    before the analyst ever saw it: "how many files are in data_in" resolved
    `data` (a real column name somewhere in a broad vault) out of the folder
    name `data_in` and answered with a 151-second value tally of unrelated
    strings, against a documented promise of an instant exact count.

    The intent was already written down next to the field route - "Only fires
    when a real field is named, so a generic 'how many files' still falls
    through" - it just had no way to test for it. This is that test.
    """
    q = str(query or "").lower()
    return (any(t in q for t in _FILE_COUNT_TRIGGERS)
            and not any(x in q for x in _FILE_COUNT_EXCLUSIONS))


def _run_analyst_step(query):
    """Public entry point — wraps `_run_analyst_step_impl` in a defensive
    try/except so that any unexpected exception (network share dropped
    mid-call, malformed vault state, OOM mid-tokenize, etc.) degrades
    cleanly to "no analyst" rather than crashing `_send`. The caller's
    transcript stays interactive instead of hanging on "computing…".
    """
    try:
        return _run_analyst_step_impl(query)
    except Exception as _top_e:
        import sys as _sys_dbg
        import traceback as _tb
        print(f"[analyst] unexpected top-level exception: {_top_e!r}",
              file=_sys_dbg.stderr)
        _tb.print_exc(file=_sys_dbg.stderr)
        return None, None, []


def _run_analyst_step_impl(query):
    """If `query` looks computational, generate pandas code via a local model
    and execute it sandboxed against the vault's data_in/ folder.

    Returns ``(block, error_msg, notices)``:
      • ``(None, None, [])``             — query isn't computational.
      • ``(success_block, None, [n…])``  — code executed; notices is a
        list of short human-friendly strings about how the analyst
        resolved filename / subfolder references (so the user can spot
        wrong matches: "I asked about sales.csv but the analyst picked
        Sales_Q3_2024.csv").
      • ``(failure_block, msg, [n…])``   — analyst was attempted but
        failed. The failure block carries an explicit refusal directive
        so the Writer doesn't invent a number; ``msg`` is a one-line
        summary the caller posts to the transcript.
    """
    import sys as _sys_dbg
    notices: list = []
    try:
        import vault_analyst as _va
    except Exception as _e:
        print('[analyst] import failed: ' + repr(_e), file=_sys_dbg.stderr)
        return None, None, notices

    # ── Direct-route: PRECOMPUTED ANSWER from a deferred task ────────
    # If the user re-asks something they previously deferred to the Vault
    # tab and ran there, answer FROM that saved result instead of
    # recomputing — that is the whole point of "Defer to Vault": ask again
    # later, get the answer.
    #
    # This runs BEFORE the looks_computational gate on purpose: a re-asked
    # deferred question must surface its saved answer even when the phrasing
    # doesn't read as "computational" (e.g. "bigger summary of sales.csv"
    # has no compute keyword and would otherwise be dropped here).
    # Reuse goes through the DerivedStore, which only returns a result whose
    # SOURCES ARE UNCHANGED — so a precomputed average is never served after
    # the underlying CSVs were edited (the staleness fix).
    try:
        import derived_results as _dr
        _ans = _dr.DerivedStore(_vault()).find_fresh(query)
    except Exception:
        _ans = None
    if _ans is not None and _ans.output:
        try:
            import pandas as _pd_pre
            _rp = Path(_ans.output)
            _rdf = _pd_pre.read_csv(_rp)
            try:
                _n_ctx_pre = ce.get_n_ctx()
            except Exception:
                _n_ctx_pre = 4096
            _ptable = _va.format_result_for_prompt(
                _rdf, max_rows=300, max_chars=12000,
                max_tokens=max(150, _n_ctx_pre // 4),
                count_tokens=ce.estimate_tokens)
            block = (f"[ANALYST RESULT — precomputed (sources unchanged)]\n"
                     f"# This was computed earlier ({_ans.operation or _ans.label}) "
                     f"and its source files are UNCHANGED, so answer from this "
                     f"saved result ({_rp.name}); do NOT recompute.\n"
                     f"{_ptable}")
            notices.append(
                f"Reused a fresh precomputed result — {_rp.name}.")
            try:
                notices.append("__ANALYST_TABLE__:" + _rdf.to_string(
                    index=False, max_rows=80, max_cols=20))
            except Exception:
                pass
            notices.append(
                "__ANALYST_ANSWER__:"
                f"Reused a saved result that is still current "
                f"({_rp.name}) — its source files are unchanged, so this "
                f"was not recomputed (see the table above).")
            # Source files behind this answer → provenance chips.
            try:
                import json as _js
                _srcs = list(getattr(_ans, "sources", []) or [])
                _srcs.append(str(_rp))
                notices.append("__ANALYST_SOURCES__:" + _js.dumps(_srcs))
            except Exception:
                pass
            return block, None, notices
        except Exception as _pe:
            print('[analyst] precomputed-answer load failed: ' + repr(_pe),
                  file=_sys_dbg.stderr)
            # fall through to normal routing if the saved file is unreadable

    # ── Direct-route: a saved COLLECTION named in the query ─────────
    # "show me Job Blue" / "summarise the Job Blue files" — if the query
    # names a saved collection, tell the council exactly which files make up
    # that project so it answers about the whole set (and only that set).
    # Runs before the looks_computational gate ("show me X" isn't a compute
    # phrase). The Vault tab's Collections panel is where the set is built.
    try:
        import vault_collections as _vc_q
        _coll = _vc_q.CollectionStore(_vault()).find_in_text(query)
    except Exception:
        _coll = None
    if _coll is not None and len(_coll.name.strip()) >= 3 and _coll.files:
        _flist = "\n".join(f"  - {f}" for f in _coll.files[:200])
        _more = (f"\n  …(+{len(_coll.files) - 200} more)"
                 if len(_coll.files) > 200 else "")
        block = (f"[ANALYST RESULT — collection “{_coll.name}”]\n"
                 f"# “{_coll.name}” is a saved collection the user grouped "
                 f"together: {len(_coll.files)} file(s). When answering about "
                 f"“{_coll.name}”, use ONLY these files:\n{_flist}{_more}")
        notices.append(
            f"Recognised the “{_coll.name}” collection — {len(_coll.files)} file(s).")
        notices.append(
            "__ANALYST_ANSWER__:"
            f"“{_coll.name}” is a saved collection of {len(_coll.files)} "
            f"file(s):\n{_flist}{_more}")
        try:
            import json as _js
            notices.append("__ANALYST_SOURCES__:" + _js.dumps(
                list(_coll.files or [])))
        except Exception:
            pass
        return block, None, notices

    # No precomputed answer / collection — only continue into the data
    # routes when the question actually looks computational.
    if not _va.looks_computational(query):
        return None, None, notices

    try:
        allowed_folders = [data_index.input_dir(_vault())]
    except Exception:
        allowed_folders = [_vault()]

    # ── Direct-intent shortcut for "true data summary" queries ──────
    # These map deterministically to folder_data_summary() — no model
    # codegen needed. Saves ~2-5 s per call AND removes a class of
    # failure mode (the model picking the wrong helper). The trigger
    # phrases below are a subset of _COMPUTE_KEYWORDS that ONLY make
    # sense as "summarise everything"; anything ambiguous still goes
    # through the model.
    _DIRECT_SUMMARY_TRIGGERS = (
        "true data summary",
        "data summary",
        "summary of the files",
        "summary of files",
        "summarize the files",
        "summarize files",
        "describe the files",
        "overview of files",
        "overview of the files",
        "overview of the data",
        "inventory of files",
        "file inventory",
        "what's in this folder",
        "what is in this folder",
        "schema of the files",
        "schemas of",
        "profile the data",
        "profile this folder",
    )
    qlower = (query or "").lower()

    # ── Direct-route: FILE COUNT ("how many files in data_in") ──────
    # A trivial question that must NOT go through model code-gen — the
    # code-gen prompt is ~3.5K tokens and overflows a small context
    # window ("exceeds max tokens" crash on the 4K-ctx machine). Answer
    # it deterministically with a cheap census (no file reads, tiny
    # prompt). Guarded against row/record/column intents, which need
    # per-file work, not a file count.
    if looks_like_file_count(query):
        try:
            _csub = _va.resolve_subfolder_hint(query, allowed_folders[0])
        except Exception:
            _csub = None
        _ctarget = _csub if _csub is not None else allowed_folders[0]
        _cscope = (f" — scope: {_csub.relative_to(allowed_folders[0])}"
                   if _csub is not None else "")
        try:
            counts = _va.folder_file_counts(_ctarget)
        except Exception as _ce_exc:
            print('[analyst] folder_file_counts failed: ' + repr(_ce_exc),
                  file=_sys_dbg.stderr)
            counts = None
        if counts is not None:
            by_ext = counts.get("by_ext", {})
            breakdown = ", ".join(
                f"{n} {ext}" for ext, n in
                sorted(by_ext.items(), key=lambda kv: -kv[1])) or "no files"
            folder_name = (str(_csub.relative_to(allowed_folders[0]))
                           if _csub is not None else "data_in")
            block = (f"[ANALYST RESULT — file count{_cscope}]\n"
                     f"# Direct census (no model code-gen, no file reads).\n"
                     f"{folder_name} contains {counts.get('total', 0)} file(s) "
                     f"across {counts.get('folders', 0)} subfolder(s).\n"
                     f"By type: {breakdown}")
            notices.append(
                f"Analyst direct-routed to file count"
                + (f" on subfolder {folder_name}" if _csub is not None else "")
                + f" — {counts.get('total', 0)} file(s).")
            notices.append(
                "__ANALYST_ANSWER__:"
                f"{folder_name} contains {counts.get('total', 0)} file(s) "
                f"across {counts.get('folders', 0)} subfolder(s).\n"
                f"By type: {breakdown}")
            return block, None, notices
        # Census failed → fall through to the model path.

    if any(phrase in qlower for phrase in _DIRECT_SUMMARY_TRIGGERS):
        # Honour subfolder hints exactly like the model path would
        # (we resolve scope_folder below for the model branch too).
        try:
            sub = _va.resolve_subfolder_hint(query, allowed_folders[0])
        except Exception:
            sub = None
        target_folders = [sub] if sub is not None else allowed_folders
        scope_str = (f" — scope: {sub.relative_to(allowed_folders[0])}"
                      if sub is not None else "")

        # Query-report cache: an identical summary over an UNCHANGED set
        # of files is served from disk instantly. The key fingerprints
        # each input file's mtime, so adding / editing / removing a file
        # changes the key and forces a fresh compute — correct staleness.
        _qc = None
        _qkey = None
        _csv_paths = None
        try:
            import stats_cache as _sc
            _qc = _sc.QueryReportCache(_vault())
            # Keep the raw Path list so the helper below doesn't walk for CSVs
            # a second time (it's the same file set that built the cache key).
            _csv_paths = _va.list_csv_files(target_folders)
            _inputs = [str(p) for p in _csv_paths]
            _qkey = _qc.make_key("folder_data_summary" + scope_str, _inputs)
        except Exception:
            _qc = None

        rep = None
        _hit = _qc.get(_qkey) if (_qc and _qkey) else None
        if _hit is not None:
            rep = _hit.get("report")

        if rep is None:
            try:
                result_df = _va.folder_data_summary(
                    target_folders, csv_files=_csv_paths)
            except Exception as _e:
                print('[analyst] direct folder_data_summary failed: '
                      + repr(_e), file=_sys_dbg.stderr)
                result_df = None
            if result_df is not None and not result_df.empty:
                try:
                    _n_ctx_ds = ce.get_n_ctx()
                except Exception:
                    _n_ctx_ds = 4096
                ds_max_tokens = max(150, _n_ctx_ds // 4)
                table_text = _va.format_result_for_prompt(
                    result_df, max_rows=250, max_chars=12000,
                    max_tokens=ds_max_tokens, count_tokens=ce.estimate_tokens,
                )
                try:
                    _user_render = result_df.to_string(index=False,
                                                        max_rows=80, max_cols=20)
                except Exception:
                    _user_render = ""
                rep = {"table_text": table_text, "user_render": _user_render,
                       "n_files": int(len(result_df))}
                if _qc and _qkey:
                    try:
                        _qc.put(_qkey, query="folder_data_summary" + scope_str,
                                report=rep)
                    except Exception:
                        pass

        if rep is not None:
            block = (f"[ANALYST RESULT — folder_data_summary{scope_str}]\n"
                     f"# Direct call (no model code-gen). One row per file.\n"
                     f"{rep.get('table_text', '')}")
            notices.append(
                f"Analyst direct-routed to folder_data_summary"
                + (f" on subfolder {sub.relative_to(allowed_folders[0])}"
                   if sub is not None else "")
                + f" — {rep.get('n_files', 0)} file(s) profiled"
                + (" (cached)" if _hit is not None else "") + "."
            )
            # Surface the table to the transcript via the same
            # __ANALYST_TABLE__ payload the model path uses.
            if rep.get("user_render"):
                notices.append("__ANALYST_TABLE__:" + rep["user_render"])
            notices.append(
                "__ANALYST_ANSWER__:"
                f"Here is a per-file data summary of {rep.get('n_files', 0)} "
                f"file(s) — one row per file (see the table above).")
            return block, None, notices
        # Fall through to the model path if the direct call returned
        # nothing (empty folder, hint resolved to non-existent path, etc).

    # ── Direct-route: STATS summary over a folder ───────────────────
    # "summary of stats", "column statistics", "min/max/mean of the
    # files" etc. used to fall through to the MODEL, which would write
    # arbitrary pandas — and over 200+ CSVs a `pd.concat([read_csv(f)
    # for f in files])` blows memory and crashes the app. Route these
    # to folder_column_stats(), which reads ONLY from the precomputed
    # stats cache (streaming compute on a miss, one file at a time) so
    # peak memory is bounded no matter how many files there are.
    _STATS_SUMMARY_TRIGGERS = (
        "summary of stats", "summary of the stats", "stats summary",
        "statistics summary", "summary statistics", "statistical summary",
        "column stats", "column statistics", "stats of the files",
        "stats for the files", "statistics of the files",
        "statistics for the files", "stats of the folder",
        "stats on the files", "min max mean", "min/max/mean",
        "summarize the stats", "summarise the stats", "summarize stats",
        "give me stats", "compute stats", "calculate stats",
        "stats across", "statistics across",
    )
    if any(phrase in qlower for phrase in _STATS_SUMMARY_TRIGGERS):
        try:
            _ssub = _va.resolve_subfolder_hint(query, allowed_folders[0])
        except Exception:
            _ssub = None
        _starget = [_ssub] if _ssub is not None else allowed_folders
        _sscope = (f" — scope: {_ssub.relative_to(allowed_folders[0])}"
                   if _ssub is not None else "")

        # Query-report cache, fingerprinted on each input file's mtime —
        # an unchanged folder serves the prior stats table instantly.
        _sqc = _sqkey = None
        _scsv_paths = None
        try:
            import stats_cache as _sc2
            _sqc = _sc2.QueryReportCache(_vault())
            # Reuse this CSV Path list below instead of re-walking for it.
            _scsv_paths = _va.list_csv_files(_starget)
            _sinputs = [str(p) for p in _scsv_paths]
            _sqkey = _sqc.make_key("folder_column_stats" + _sscope, _sinputs)
        except Exception:
            _sqc = None

        srep = None
        _shit = _sqc.get(_sqkey) if (_sqc and _sqkey) else None
        if _shit is not None:
            srep = _shit.get("report")

        if srep is None:
            try:
                _sdf = _va.folder_column_stats(
                    _vault(), _starget, csv_files=_scsv_paths)
            except Exception as _se:
                print('[analyst] direct folder_column_stats failed: '
                      + repr(_se), file=_sys_dbg.stderr)
                _sdf = None
            if _sdf is not None and not _sdf.empty:
                try:
                    _n_ctx_st = ce.get_n_ctx()
                except Exception:
                    _n_ctx_st = 4096
                _st_max_tokens = max(150, _n_ctx_st // 4)
                _stable = _va.format_result_for_prompt(
                    _sdf, max_rows=400, max_chars=12000,
                    max_tokens=_st_max_tokens, count_tokens=ce.estimate_tokens,
                )
                try:
                    _srender = _sdf.to_string(index=False,
                                              max_rows=120, max_cols=20)
                except Exception:
                    _srender = ""
                _nfiles = int(_sdf["file"].nunique()) if "file" in _sdf else 0
                srep = {"table_text": _stable, "user_render": _srender,
                        "n_files": _nfiles}
                if _sqc and _sqkey:
                    try:
                        _sqc.put(_sqkey,
                                 query="folder_column_stats" + _sscope,
                                 report=srep)
                    except Exception:
                        pass

        if srep is not None:
            block = (f"[ANALYST RESULT — folder_column_stats{_sscope}]\n"
                     f"# Direct call (no model code-gen). Stats served from "
                     f"the precomputed cache — one row per (file, column).\n"
                     f"{srep.get('table_text', '')}")
            notices.append(
                "Analyst direct-routed to folder_column_stats"
                + (f" on subfolder {_ssub.relative_to(allowed_folders[0])}"
                   if _ssub is not None else "")
                + f" — stats for {srep.get('n_files', 0)} file(s)"
                + (" (cached)" if _shit is not None else "") + "."
            )
            if srep.get("user_render"):
                notices.append("__ANALYST_TABLE__:" + srep["user_render"])
            notices.append(
                "__ANALYST_ANSWER__:"
                f"Here are per-column statistics across "
                f"{srep.get('n_files', 0)} file(s) — one row per "
                f"(file, column) (see the table above).")
            return block, None, notices
        # Empty result → fall through to the model path.

    # ── Upgrade A+B: pre-resolve filename + subfolder hints ──────────
    # Before sending the prompt to the model, look for explicit
    # filename references ("sales.csv", quoted strings) and folder
    # references ("in the test_data folder") in the user's question.
    # Resolve them against the actual on-disk inventory so the model
    # sees the real paths instead of guessing — guesses are how
    # FileNotFoundError / wrong-file-grabbed failures used to creep in.
    base_folder = allowed_folders[0] if allowed_folders else None
    scope_folder = None
    if base_folder is not None:
        try:
            scope_folder = _va.resolve_subfolder_hint(query, base_folder)
        except Exception as _e:
            print('[analyst] subfolder resolve failed: ' + repr(_e),
                  file=_sys_dbg.stderr)
    if scope_folder is not None:
        allowed_folders = [scope_folder]
        try:
            rel = scope_folder.relative_to(base_folder) if base_folder else scope_folder
        except Exception:
            rel = scope_folder
        notices.append(f"Analyst scoped to subfolder: {rel}")
        print('[analyst] scope restricted to: ' + str(scope_folder),
              file=_sys_dbg.stderr)

    filename_hints_pairs = []
    try:
        filename_hints_pairs = _va.resolve_filename_hints(query, allowed_folders)
    except Exception as _e:
        print('[analyst] filename hint resolve failed: ' + repr(_e),
              file=_sys_dbg.stderr)
    filename_hints_text = ""
    if filename_hints_pairs:
        # Build a one-line summary for the transcript so the user can spot
        # a wrong match before reading the answer. Suppress trivial
        # resolutions where the token == filename — those look like
        # typos to the user ("Filename hints: 'sales.csv' → sales.csv")
        # and add no information. Only surface non-trivial resolutions
        # and "no match" cases.
        bits: list = []
        for tok, resolved in filename_hints_pairs:
            if resolved is None:
                bits.append(f"'{tok}' → no match")
            elif tok.lower() != resolved.name.lower():
                # Non-trivial resolution — the user said one thing, the
                # resolver picked something different. This is the case
                # they need to see.
                bits.append(f"'{tok}' → {resolved.name}")
            # else: exact match between user token and filename → silent
        if bits:
            notices.append("Filename hints: " + "; ".join(bits))

        filename_hints_text = _va.format_filename_hints(
            filename_hints_pairs,
            base_folder=allowed_folders[0] if allowed_folders else None,
        )
        print('[analyst] filename hints:\n' + filename_hints_text,
              file=_sys_dbg.stderr)

    inventory = _va.preview_csv_inventory(allowed_folders, max_files=15, max_cols=30)
    prompt = _va.build_pandas_code_prompt(
        query, allowed_folders, inventory,
        filename_hints=filename_hints_text or None,
        subfolder_scope=scope_folder,
    )

    try:
        import council_engine as _ce
        # Backend-agnostic helper — works in both Ollama and GGUF modes.
        with _ce._TimingScope("analyst.codegen"):
            raw = _ce.local_chat(
                messages=[{'role': 'user', 'content': prompt}],
                temperature=0.0,
                num_predict=600,
                timeout=90,
            )
    except Exception as _e:
        msg = f"code generation failed: {_e!r}"
        print('[analyst] ' + msg, file=_sys_dbg.stderr)
        _record_app_failure("analyst.codegen_error", "council_gui_engine",
                            msg, context={"query": query[:200]})
        return _build_analyst_failure_block("(no code generated)", msg), msg, notices

    code = _va.extract_python_code(raw)
    if not code.strip():
        msg = "the model produced no executable pandas code"
        print('[analyst] empty code from model', file=_sys_dbg.stderr)
        _record_app_failure("analyst.empty_code", "council_gui_engine",
                            msg, context={"query": query[:200]})
        return _build_analyst_failure_block("(empty)", msg), msg, notices
    print('[analyst] generated code (first 300):\n' + code[:300], file=_sys_dbg.stderr)

    # Hand the analyst a way to BUILD its own tools when a capability is
    # missing. save_app_tool validates via the sandbox and persists to the
    # vault's App_Built_tools/ (UNREVIEWED); run/list let it reuse them. These
    # are curated app callables — NOT passed to app-built tools themselves, so
    # a tool can't spawn more tools.
    def _save_app_tool(name, description, code):
        import app_built_tools as _abt
        ok, msg, _ = _abt.save_tool(name, description, code,
                                    author="council", vault_dir=_vault())
        return msg

    def _run_app_tool(name, args=None):
        import app_built_tools as _abt
        _df, _msg = _abt.run_tool(name, args or {},
                                  allowed_folders=allowed_folders,
                                  vault_dir=_vault())
        return _df if _df is not None else _msg

    def _list_app_tools():
        import app_built_tools as _abt
        return _abt.list_tools(vault_dir=_vault())

    _analyst_extra = {
        "save_app_tool": _save_app_tool,
        "run_app_tool":  _run_app_tool,
        "list_app_tools": _list_app_tools,
    }
    with _ce._TimingScope("analyst.exec"):
        result_df, log = _va.execute_pandas_code(
            code, allowed_folders, extra_globals=_analyst_extra)
    if result_df is None:
        # Extract the first traceback line for a concise transcript message.
        # The full log keeps the entire traceback for debugging in the block.
        first_err = _summarise_analyst_error(log)
        print('[analyst] exec failed: ' + log[:400], file=_sys_dbg.stderr)
        _record_app_failure("analyst.exec_error", "vault_analyst",
                            first_err, detail=log,
                            context={"query": query[:200]})
        return _build_analyst_failure_block(code, log), first_err, notices

    # Budget-scaled rendering. The analyst block is exempt from the
    # per-block cap (UNCAPPED_PRIOS) and below DROPPABLE_FROM, but
    # giving it 12 KB worth of rows on a 4 K-ctx machine still eats the
    # whole window. Cap at 25 % of n_ctx so a 4 K window gets ~1 K
    # tokens for the analyst result and a 32 K window gets ~8 K.
    # The renderer keeps head + tail and elides the middle, which
    # preserves the summary rows pandas appends (Total / Mean / …)
    # that the model needs to give a correct answer.
    try:
        _n_ctx_a = ce.get_n_ctx()
    except Exception:
        _n_ctx_a = 4096
    analyst_max_tokens = max(150, _n_ctx_a // 4)
    table_text = _va.format_result_for_prompt(
        result_df,
        max_rows=250, max_chars=12000,
        max_tokens=analyst_max_tokens,
        count_tokens=ce.estimate_tokens,
    )

    # Build a separate USER-FACING render of the full result (or as
    # much as fits in a transcript box) AND save the complete DataFrame
    # to vault/analyst_results/ so nothing is ever silently lost. The
    # caller posts the transcript render to the chat AND mentions the
    # saved-file path so the user can re-open the result later.
    user_table_lines: list = []
    full_table_path = None
    try:
        nrows = len(result_df)
        ncols = len(result_df.columns)
        user_table_lines.append(
            f"Analyst result: {nrows:,} row{'s' if nrows != 1 else ''} "
            f"× {ncols} column{'s' if ncols != 1 else ''}"
        )
        # Render up to 500 rows directly into the transcript. Beyond
        # that, save a CSV and tell the user where it is — Tk text
        # widgets get sluggish above a few thousand rows of text.
        TRANSCRIPT_DISPLAY_ROWS = 500
        if nrows <= TRANSCRIPT_DISPLAY_ROWS:
            user_table_lines.append(
                result_df.to_string(index=False, max_colwidth=80)
            )
        else:
            user_table_lines.append(
                result_df.head(TRANSCRIPT_DISPLAY_ROWS)
                          .to_string(index=False, max_colwidth=80)
            )
            user_table_lines.append(
                f"\n... ({nrows - TRANSCRIPT_DISPLAY_ROWS:,} more rows "
                f"omitted from this transcript view; full table saved "
                f"to file — see path below.)"
            )

        # Save the complete DataFrame as CSV under
        # vault/analyst_results/<timestamp>_<rows>x<cols>.csv. Users
        # can open it from disk to verify or share the full data.
        try:
            from datetime import datetime as _dt
            stamp = _dt.now().strftime("%Y%m%d_%H%M%S")
            results_dir = _vault() / "analyst_results"
            results_dir.mkdir(parents=True, exist_ok=True)
            full_table_path = results_dir / f"{stamp}_{nrows}x{ncols}.csv"
            result_df.to_csv(full_table_path, index=False)
            user_table_lines.append(
                f"\nFull table saved to: {full_table_path}"
            )
        except Exception as _save_exc:
            print(f"[analyst] could not save full table: {_save_exc!r}",
                  file=_sys_dbg.stderr)
            full_table_path = None
    except Exception as _render_exc:
        print(f"[analyst] could not build user-facing table: "
              f"{_render_exc!r}", file=_sys_dbg.stderr)
        user_table_lines = []
    user_table = "\n".join(user_table_lines) if user_table_lines else ""

    if user_table:
        notices.append("__ANALYST_TABLE__:" + user_table)

    block = (
        '[ANALYST RESULT — computed from real CSV data]\n'
        + 'pandas code:\n' + code.strip() + '\n\n'
        + 'output:\n' + table_text + '\n'
        + '[END ANALYST]'
    )
    return block, None, notices


def _summarise_analyst_error(log: str) -> str:
    """Extract a short human-readable summary from a Python traceback.

    Returns the last `ExceptionType: message` line if present (most
    useful for the user), else the first 200 chars of the log.
    """
    if not log:
        return "analyst execution failed"
    # Find the final "ExceptionType: message" line in the traceback.
    last = None
    for line in log.splitlines():
        line = line.strip()
        if line and ":" in line and not line.startswith(("File ", "  ", "Traceback")):
            # Plausible exception line — keep the latest one.
            last = line
    if last:
        return last[:200]
    return log.strip().splitlines()[0][:200] if log.strip() else "analyst failed"


def _record_app_failure(kind: str, subsystem: str, message: str,
                        detail: str = "",
                        context: "Optional[dict]" = None) -> None:
    """Best-effort failure capture into the self-improvement loop.

    Forwards to agent_logs.record_failure (append-only JSONL in the
    vault). The FailureAnalyzer aggregates recurring signatures into
    human-reviewed improvement proposals in the Agent panel. Import-
    guarded and exception-proof so the failing code path is never made
    worse by the act of recording it."""
    try:
        import agent_logs as _al
        _al.record_failure(kind, subsystem, message,
                           detail=detail, context=context)
    except Exception:
        pass


def _build_analyst_failure_block(code: str, error_log: str) -> str:
    """Build an [ANALYST RESULT — FAILED] block that tells the model the
    computation was attempted but failed, and explicitly refuses any
    invented numeric answer. This is the critical anti-hallucination
    fallback: without it, the model would silently produce a freeform
    answer that looks confident but invents numbers from training data.
    """
    code_part = code.strip() or "(no code)"
    err_part = (error_log or "").strip() or "(no traceback captured)"
    if len(err_part) > 1200:
        err_part = err_part[:1200] + "\n... (truncated)"
    return (
        "[ANALYST RESULT — COMPUTATION FAILED]\n"
        "The analyst attempted a deterministic pandas calculation but the "
        "sandbox raised an error. The numeric answer the user asked for "
        "is NOT AVAILABLE.\n\n"
        "pandas code attempted:\n"
        + code_part + "\n\n"
        "error:\n"
        + err_part + "\n\n"
        "[INSTRUCTION TO THE WRITER: Do NOT invent a numeric answer to "
        "compensate. Tell the user the deterministic computation failed, "
        "quote the error type briefly, and suggest they rephrase the "
        "question or check that the file/column they mentioned exists. "
        "Do NOT pull values from training-data memory of similarly-named "
        "datasets.]\n"
        "[END ANALYST]"
    )
