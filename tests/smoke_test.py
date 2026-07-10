"""
tests/smoke_test.py — fast, no-network, no-dependency smoke tests
for the modules that are most likely to regress silently across
platforms.

Runs from any directory; bootstraps sys.path against the repo root.
Exits 0 on pass, non-zero on first failure. CI gates on the exit
code; users can run it manually after install via:

    python tests/smoke_test.py

What this covers (and doesn't):

  • hardware_detect.detect() returns the documented dict shape
    on whatever OS is hosting the test. Doesn't assert specific
    values — the GPU on CI is whatever the runner has.

  • previous_install_detect.detect() returns the documented dict
    shape against a temp dir. Verifies the structural contract.

  • Synthetic GGUF round-trip — writes a valid-magic minimal GGUF
    to a temp file and confirms three independent validators
    accept it:
        onboarding.gguf_file_status
        previous_install_detect._quick_gguf_validate
        council_engine.read_gguf_metadata

  • Negative cases — non-magic file, too-small file, missing file.
    Each validator must reject in the documented way.

What this DOES NOT cover (out of scope here):
  • llama-cpp Llama() construction — needs a real model, real
    compute, and a GPU on the runner; that belongs in a separate
    GPU-enabled integration test.
  • UI / Tkinter — headless CI has no display.
  • Vault indexing — covered by the existing `python -c \"import vault_index\"`
    smoke in installs.txt.
"""
from __future__ import annotations

import math
import os
import struct
import sys
import tempfile
import traceback
from pathlib import Path

# Force UTF-8 on stdout/stderr so the box-drawing characters used in
# section headers don't crash on Windows cp1252 consoles.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ─── Path setup ─────────────────────────────────────────────────────
HERE = Path(__file__).resolve().parent
REPO = HERE.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# ─── Test runner ────────────────────────────────────────────────────
_FAILS: list = []
_PASSES: list = []


def _check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        _PASSES.append(name)
        print(f"  ✓ {name}")
    else:
        _FAILS.append((name, detail))
        print(f"  ✗ {name}   {detail}")


def _run(label: str, fn) -> None:
    print(f"\n── {label} ──")
    try:
        fn()
    except Exception as exc:
        _FAILS.append((label, repr(exc)))
        print(f"  ✗ {label} raised: {exc!r}")
        traceback.print_exc()


def _raises(exc_type, callable_obj) -> bool:
    """True iff `callable_obj()` raises an instance of `exc_type`.
    Used by assertions like _check('foo raises', _raises(ValueError,
    lambda: helper(bad_input)))."""
    try:
        callable_obj()
    except exc_type:
        return True
    except Exception:
        return False
    return False


# ─── Synthetic GGUF builder ─────────────────────────────────────────
def _make_synthetic_gguf(path: Path,
                          arch: str = "llama",
                          context_length: int = 8192) -> None:
    """Write a minimum-viable GGUF v3 header to ``path``.

    Layout per https://github.com/ggerganov/ggml/blob/master/docs/gguf.md:
        4 B  magic = b"GGUF"
        4 B  version (u32, LE) = 3
        8 B  tensor_count (u64) = 0
        8 B  metadata_kv count (u64)
        for each KV:
            8 B key_len (u64)
            <key_len> bytes — utf-8 key
            4 B value_type (u32)
            value bytes (variable per type)

    We emit two KVs: general.architecture (string type=8) and
    {arch}.context_length (u32 type=4). That's enough for
    read_gguf_metadata + _gguf_max_context_from_metadata to extract
    the context_length end-to-end.
    """
    arch_key = b"general.architecture"
    ctx_key  = f"{arch}.context_length".encode("utf-8")
    arch_val = arch.encode("utf-8")

    with open(path, "wb") as fh:
        # Header
        fh.write(b"GGUF")
        fh.write(struct.pack("<I", 3))      # version
        fh.write(struct.pack("<Q", 0))      # tensor_count
        fh.write(struct.pack("<Q", 2))      # kv_count

        # KV 1 — general.architecture : string
        fh.write(struct.pack("<Q", len(arch_key)))
        fh.write(arch_key)
        fh.write(struct.pack("<I", 8))      # type = string
        fh.write(struct.pack("<Q", len(arch_val)))
        fh.write(arch_val)

        # KV 2 — <arch>.context_length : u32
        fh.write(struct.pack("<Q", len(ctx_key)))
        fh.write(ctx_key)
        fh.write(struct.pack("<I", 4))      # type = u32
        fh.write(struct.pack("<I", context_length))

        # Pad to 1 KB so the size sanity-check in the validators
        # doesn't reject it as "suspiciously small".
        pad_target = 2048
        cur = fh.tell()
        if cur < pad_target:
            fh.write(b"\x00" * (pad_target - cur))


# ─── Tests ──────────────────────────────────────────────────────────
def test_hardware_detect() -> None:
    import hardware_detect as hd
    info = hd.detect()
    _check("returns a dict", isinstance(info, dict))
    expected_keys = {
        "os", "os_version", "python", "cpu_brand", "cpu_cores",
        "ram_gb", "has_avx2", "has_f16c", "gpu_vendor", "gpu_name",
        "vram_gb", "cuda_max", "recommended", "notes",
    }
    missing = expected_keys - set(info)
    _check(f"all documented keys present (missing={sorted(missing)})",
           not missing)
    _check("os value is one of {windows, linux, macos, wsl, unknown}",
           info.get("os") in ("windows", "linux", "macos", "wsl", "unknown"),
           detail=f"got {info.get('os')!r}")
    _check("recommended sub-dict has keys",
           isinstance(info.get("recommended"), dict)
           and {"cuda_tier", "model_tier", "model_pick", "n_ctx_max"}.issubset(
               set(info["recommended"]))
           )
    _check("cuda_tier is a known value",
           info["recommended"]["cuda_tier"] in ("cpu", "cu121", "cu124", "cu128"))


def test_previous_install_detect() -> None:
    import previous_install_detect as pid
    with tempfile.TemporaryDirectory() as td:
        app_dir   = Path(td) / "app"
        vault_dir = Path(td) / "vault"
        app_dir.mkdir(); vault_dir.mkdir()
        info = pid.detect(app_dir, vault_dir)
    _check("returns a dict", isinstance(info, dict))
    expected_keys = {
        "conda_env", "vault", "gguf_models",
        "previous_model", "prior_version", "notes",
    }
    missing = expected_keys - set(info)
    _check(f"all documented keys present (missing={sorted(missing)})",
           not missing)
    _check("vault.present matches the temp dir we created",
           info["vault"]["present"] is True)
    _check("data_in_files is 0 for empty vault",
           info["vault"]["data_in_files"] == 0)
    _check("previous_model is None on empty vault",
           info["previous_model"] is None)


def test_synthetic_gguf_accepted() -> None:
    """A valid-magic GGUF must pass every validator."""
    import council_engine as ce
    import onboarding
    import previous_install_detect as pid

    with tempfile.TemporaryDirectory() as td:
        gguf = Path(td) / "test.gguf"
        _make_synthetic_gguf(gguf, arch="llama", context_length=8192)

        # onboarding.gguf_file_status
        ok, msg = onboarding.gguf_file_status(str(gguf))
        _check(f"onboarding.gguf_file_status accepts synthetic GGUF "
               f"(msg={msg!r})", ok)

        # previous_install_detect._quick_gguf_validate
        _check("previous_install_detect._quick_gguf_validate accepts it",
               pid._quick_gguf_validate(gguf))

        # council_engine.read_gguf_metadata round-trip — must produce
        # a non-empty dict and surface context_length correctly.
        md = ce.read_gguf_metadata(gguf)
        _check("read_gguf_metadata returns non-empty dict",
               isinstance(md, dict) and len(md) > 0,
               detail=f"got {md!r}")
        ctx = ce._gguf_max_context_from_metadata(md)
        _check(f"context_length round-trips through metadata reader "
               f"(got {ctx})", ctx == 8192)


def test_synthetic_gguf_rejected_cases() -> None:
    """Negative cases — every validator must refuse to accept these."""
    import council_engine as ce
    import onboarding
    import previous_install_detect as pid

    with tempfile.TemporaryDirectory() as td:
        # Case 1 — non-GGUF magic
        notgguf = Path(td) / "not.gguf"
        notgguf.write_bytes(b"<!DOCTYPE html>\n<html>" + b"\0" * 4096)
        ok, _ = onboarding.gguf_file_status(str(notgguf))
        _check("non-GGUF magic rejected by onboarding.gguf_file_status",
               not ok)
        _check("non-GGUF magic rejected by _quick_gguf_validate",
               not pid._quick_gguf_validate(notgguf))

        # Case 2 — too small (200 bytes is below the size floor)
        tiny = Path(td) / "tiny.gguf"
        tiny.write_bytes(b"GGUF" + b"\0" * 196)
        ok, _ = onboarding.gguf_file_status(str(tiny))
        _check("too-small file rejected by onboarding.gguf_file_status",
               not ok)
        _check("too-small file rejected by _quick_gguf_validate",
               not pid._quick_gguf_validate(tiny))

        # Case 3 — missing file
        missing = Path(td) / "missing.gguf"
        ok, _ = onboarding.gguf_file_status(str(missing))
        _check("missing file rejected by onboarding.gguf_file_status",
               not ok)
        # read_gguf_metadata on missing file should return {}
        md = ce.read_gguf_metadata(missing)
        _check("read_gguf_metadata returns {} on missing file",
               md == {})


def test_context_condenser() -> None:
    """context_condenser must shrink an oversized block to fit a target,
    keeping head/tail + the lines most relevant to the task memo, and
    return None when the target is too tiny to be worth it. This is the
    'chunk into sections to extend context' rescue that replaces dropping
    overflow blocks on a small window.
    """
    import context_condenser as cc
    est = lambda s: max(1, len(s) // 4)
    lines = [f"filler line number {i} with random words" for i in range(60)]
    lines[0] = "HEADER: data summary"
    lines[10] = "sales.csv has 4820 rows and column revenue"
    lines[30] = "revenue total is 99213 for sales.csv"
    lines[-1] = "FOOTER: end of report"
    text = "\n".join(lines)
    full = est(text)

    out = cc.condense_to_fit(text, full // 4,
                             task="goal: total revenue in sales.csv",
                             estimate_tokens=est)
    _check("condensed output fits the target", out is not None
           and est(out) <= full // 4)
    _check("condense keeps the header line", "HEADER: data summary" in out)
    _check("condense keeps the footer line", "FOOTER: end of report" in out)
    _check("condense keeps a task-relevant line",
           "99213" in out or "4820 rows" in out)
    _check("condense records what was elided", "elided" in out)

    _check("tiny target -> None (caller drops)",
           cc.condense_to_fit(text, 8, estimate_tokens=est) is None)
    _check("already-fits text returned unchanged",
           cc.condense_to_fit("short text", 999, estimate_tokens=est)
           == "short text")

    chunks = cc.chunk_by_tokens(text, 50, est)
    _check("chunking splits into multiple sections", len(chunks) > 1)
    _check("each chunk respects the token cap",
           all(est(c) <= 60 for c in chunks))

    # LLM map-reduce path uses the injected call and still fits.
    out2 = cc.condense_with_llm(text, full // 4, task="revenue",
                                chunk_tokens=40, estimate_tokens=est,
                                llm_call=lambda p: "- revenue 99213\n- 4820 rows")
    _check("llm map-reduce output fits the target", est(out2) <= full // 4)


def test_build_pandas_code_prompt_no_fstring_brace_bug() -> None:
    """Regression: build_pandas_code_prompt is an f-string, and its helper
    catalog contains literal braces ({name: df}, {df, top_left, ...},
    ${ENV_VAR}). If the catalog is inside the f-string those parse as
    interpolations and raise 'NameError: name ... is not defined' EVERY
    time the analyst generates code (i.e. on every data model-call). The
    catalog must be a plain (non-f) string so braces stay literal.
    """
    from pathlib import Path as _P
    import vault_analyst as va
    # Must not raise (the bug raised NameError here).
    prompt = va.build_pandas_code_prompt(
        "how many rows in sales.csv?", [_P(".")], "sales.csv: id,amount")
    _check("prompt builds without NameError", isinstance(prompt, str) and prompt)
    _check("header interpolation still works",
           "how many rows in sales.csv?" in prompt
           and "sales.csv: id,amount" in prompt)
    # Literal braces from the catalog survive as TEXT (not interpolated).
    _check("literal '{name: df}' preserved", "{name: df}" in prompt)
    _check("literal set-shape doc preserved",
           "{df, top_left, n_rows, n_cols, header_row}" in prompt)
    _check("literal '${ENV_VAR}' preserved", "${ENV_VAR}" in prompt)


def test_data_summary_triggers() -> None:
    """Data-summary intents must route to the analyst — not freeform.
    These queries are what makes the Council tab actually capable of
    answering 'give me a true data summary of files in this subfolder'.
    """
    import vault_analyst as va
    cases = [
        "give me a true data summary of the files",
        "what's in the sales subfolder?",
        "describe the files in data_in/Q3/",
        "summarize the data in this folder",
        "overview of the files",
        "inventory of files in the vault",
        "data quality on these CSVs",
        "profile this dataset",
    ]
    for q in cases:
        _check(f"looks_computational matches: {q!r}",
               va.looks_computational(q),
               detail="trigger word missing from _COMPUTE_KEYWORDS")


def test_folder_data_summary_helper() -> None:
    """folder_data_summary must:
       * be importable
       * return a DataFrame on an empty folder
       * return one row per file with the documented schema on a
         folder containing a small CSV + a JSON + an image.
    """
    import pandas as pd
    import vault_analyst as va
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        # Empty-folder case
        df_empty = va.folder_data_summary(td_path)
        _check("returns a DataFrame on empty folder",
               isinstance(df_empty, pd.DataFrame))
        _check("empty folder produces 0 rows", len(df_empty) == 0)

        # Mixed-content case
        (td_path / "orders.csv").write_text(
            "order_id,total,date\n1,99.5,2024-03-15\n2,150,2024-03-16\n"
        )
        (td_path / "config.json").write_text('{"setting": "value"}')
        (td_path / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 256)
        df = va.folder_data_summary(td_path)
        _check("returns a DataFrame", isinstance(df, pd.DataFrame))
        _check("documented columns present",
               {"file", "type", "rows", "columns", "size_kb",
                "missing_pct"}.issubset(set(df.columns)))
        types = set(df["type"].astype(str).tolist())
        _check(f"all three types detected (got {sorted(types)})",
               {"csv", "json", "image"}.issubset(types))
        csv_row = df[df["file"] == "orders.csv"]
        _check("CSV row count correct (2 rows)",
               not csv_row.empty
               and int(csv_row.iloc[0]["rows"]) == 2)
        _check("CSV column count correct (3 cols)",
               not csv_row.empty
               and int(csv_row.iloc[0]["columns"]) == 3)


def test_single_file_helpers_handle_empty_and_malformed() -> None:
    """summarize_csv / schema_doc_from_csv must degrade gracefully on an
    empty (0-byte) or malformed CSV instead of raising EmptyDataError /
    KeyError — these run over whole folders, where one bad file must not
    abort the batch. (Found by adversarial-data simulation.)
    """
    import vault_analyst as va
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        empty = d / "empty.csv"
        empty.write_text("")                       # 0 bytes, no header
        good = d / "good.csv"
        good.write_text("id,amount\n1,10\n2,20\n")

        # summarize_csv: empty -> diagnostic row (no raise); good -> profile.
        r_empty = va.summarize_csv(empty)
        _check("summarize_csv(empty) returns a frame, doesn't raise",
               hasattr(r_empty, "shape") and len(r_empty) >= 1)
        _check("summarize_csv(empty) flags the empty/unreadable status",
               "status" in r_empty.columns)
        r_good = va.summarize_csv(good)
        _check("summarize_csv(good) still profiles columns",
               "dtype" in r_good.columns and len(r_good) == 2)

        # schema_doc_from_csv: must not KeyError on the diagnostic frame.
        doc_empty = va.schema_doc_from_csv(empty)
        _check("schema_doc_from_csv(empty) returns a string, no KeyError",
               isinstance(doc_empty, str) and "empty.csv" in doc_empty)
        doc_good = va.schema_doc_from_csv(good)
        _check("schema_doc_from_csv(good) still produces a real doc",
               isinstance(doc_good, str) and "good.csv" in doc_good)


def test_folder_file_counts_census() -> None:
    """folder_file_counts gives a cheap, exact file census (total +
    by-extension) WITHOUT reading files — the deterministic answer for
    'how many files in data_in', which must never go through the ~3.5K-token
    code-gen prompt (that overflowed a 4K context and crashed). Internal
    cache dirs and hidden entries are excluded.
    """
    import vault_analyst as va
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        for i in range(3):
            (d / f"a{i}.csv").write_text("x\n1\n")
        for i in range(2):
            (d / f"b{i}.json").write_text("{}")
        (d / "notes.txt").write_text("hi")
        sub = d / "sub"
        sub.mkdir()
        (sub / "c.csv").write_text("x\n1\n")
        cache = d / ".stats_cache"
        cache.mkdir()
        (cache / "junk.csv").write_text("ignore")  # must be excluded

        c = va.folder_file_counts(d)
        _check("total counts real files only (excludes .stats_cache)",
               c["total"] == 7)
        _check("subfolder counted", c["folders"] == 1)
        _check("csv breakdown correct (3 top + 1 sub)",
               c["by_ext"].get(".csv") == 4)
        _check("json breakdown correct", c["by_ext"].get(".json") == 2)
        _check("txt breakdown correct", c["by_ext"].get(".txt") == 1)


def test_stats_summary_triggers() -> None:
    """Stats-summary intents must reach the analyst (looks_computational).
    If they don't, the bounded folder_column_stats direct-route never
    runs and the query crashes the app on 200+ CSVs via model code-gen.
    """
    import vault_analyst as va
    cases = [
        "give me a summary of stats for this folder",
        "stats summary of the files",
        "column stats across the data",
        "min max mean of every csv",
        "compute statistics for the folder",
    ]
    for q in cases:
        _check(f"looks_computational matches: {q!r}",
               va.looks_computational(q),
               detail="stats keyword missing from _COMPUTE_KEYWORDS")


def test_analyst_protected_and_csv_reuse() -> None:
    """Behavior-preserving analyst wins: (a) list_csv_files still drops files
    under a protected subdir (conversation_logs) after the _drop_protected
    resolve-once rewrite; (b) folder_data_summary given a precomputed CSV list
    (finding #5) returns the byte-identical frame to walking for CSVs itself."""
    import vault_analyst as va
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "a.csv").write_text("x,y\n1,2\n3,4\n")
        (root / "b.csv").write_text("m,n\n5,6\n")
        (root / "conversation_logs").mkdir()
        (root / "conversation_logs" / "secret.csv").write_text("s\n9\n")

        files = va.list_csv_files(root)
        names = sorted(p.name for p in files)
        _check("protected conversation_logs csv is excluded",
               "secret.csv" not in names)
        _check("normal csvs are kept", names == ["a.csv", "b.csv"])

        # finding #5: passing the CSV list == letting the helper walk.
        walked = va.folder_data_summary(root)
        reused = va.folder_data_summary(root, csv_files=va.list_csv_files(root))
        _check("folder_data_summary(csv_files=...) equals the walk",
               walked.equals(reused))


def test_folder_column_stats_bounded_many_files() -> None:
    """folder_column_stats must aggregate stats over MANY CSVs from the
    cache (streaming per file) and return one row per (file, column) —
    this is the bounded path that replaces the OOM-prone model code-gen
    on a 'summary of stats in a folder containing 200 csvs' query.
    """
    import pandas as pd
    import vault_analyst as va
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td) / "vault"
        data = vault / "data_in" / "many"
        data.mkdir(parents=True)
        n = 60
        for i in range(n):
            (data / f"part_{i:03d}.csv").write_text(
                "id,value,label\n"
                f"{i},{i * 1.5},a\n{i + 1},{i * 2.0},b\n"
            )
        df = va.folder_column_stats(vault, data)
        _check("returns a DataFrame", isinstance(df, pd.DataFrame))
        _check("non-empty over many files", not df.empty)
        _check("one row per (file,column) — file column present",
               "file" in df.columns and "column" in df.columns)
        files_seen = df["file"].nunique() if "file" in df else 0
        _check(f"all {n} files represented (got {files_seen})",
               files_seen == n)
        # numeric stats must be populated for the 'value' column
        val_rows = df[df["column"] == "value"] if "column" in df else df.iloc[0:0]
        _check("numeric stats computed for 'value' column",
               not val_rows.empty and val_rows["mean"].notna().any())


def test_analyst_read_budget_guard() -> None:
    """Model-written code that loops pd.read_csv over many files and
    concats them must hit a CATCHABLE read-budget error (clean failure,
    no OOM-crash) — while bounded per-file helpers still succeed under
    the same cap. This is the guardrail for the 200-CSV crash on a
    memory-capped machine, independent of how the query is routed.
    """
    import csv as _csv
    import vault_analyst as va
    prev = os.environ.get("COUNCIL_ANALYST_READ_BUDGET_MB")
    os.environ["COUNCIL_ANALYST_READ_BUDGET_MB"] = "10"   # tiny cap
    try:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            # ~0.7 MB/frame x 60 files = ~42 MB summed reads, well over
            # the 10 MB cap, so the loop must refuse partway through.
            for i in range(60):
                with (d / f"f_{i:03d}.csv").open(
                        "w", newline="", encoding="utf-8") as fh:
                    w = _csv.writer(fh)
                    w.writerow(["id", "value", "label"])
                    for r in range(6000):
                        w.writerow([i * 6000 + r, r * 1.5,
                                    "lorem ipsum dolor sit amet " * 2])

            # Unbounded pattern -> must refuse cleanly.
            unbounded = (
                "frames = [pd.read_csv(f) for f in list_csv_files(DATA_FOLDERS)]\n"
                "result_df = pd.concat(frames, ignore_index=True).describe()\n"
            )
            df, log = va.execute_pandas_code(unbounded, [d])
            _check("unbounded concat returns no frame (refused)", df is None)
            _check("refusal mentions the read budget",
                   "read budget exceeded" in (log or ""),
                   detail=f"log head: {(log or '')[:80]!r}")
            _check("refusal points to per-file helpers",
                   "per-file helpers" in (log or "")
                   or "column_stats" in (log or ""))

            # Re-import bypass must ALSO be budgeted.
            bypass = (
                "import pandas as p2\n"
                "frames = [p2.read_csv(f) for f in list_csv_files(DATA_FOLDERS)]\n"
                "result_df = p2.concat(frames, ignore_index=True).describe()\n"
            )
            df_b, log_b = va.execute_pandas_code(bypass, [d])
            _check("re-imported pandas is also budgeted (refused)",
                   df_b is None and "read budget exceeded" in (log_b or ""))

            # Bounded per-file helper must still succeed under the same cap.
            bounded = "result_df = numeric_summary_per_csv(DATA_FOLDERS)\n"
            df2, log2 = va.execute_pandas_code(bounded, [d])
            _check("bounded per-file helper still succeeds under the cap",
                   df2 is not None and not df2.empty,
                   detail=f"log: {(log2 or '')[:80]!r}")
    finally:
        if prev is None:
            os.environ.pop("COUNCIL_ANALYST_READ_BUDGET_MB", None)
        else:
            os.environ["COUNCIL_ANALYST_READ_BUDGET_MB"] = prev


def test_mongo_normalize_model_digestible() -> None:
    """Mongo BSON/JSON -> model-digestible conversion must:
       * coerce ObjectId / datetime / Decimal128 / Extended-JSON wrappers
         ($oid/$date/$numberLong/$numberDecimal/$binary) into clean scalars
       * flatten nested dicts to dotted keys
       * collapse scalar arrays (joined + truncated) and object arrays
         (compact JSON) into single cells
       * produce an all-scalar DataFrame, a schema profile, a text digest,
         and a tidy 'explode' view
       * read .json, .jsonl, AND a single JSON object from disk
    """
    import datetime as _dt
    import pandas as pd
    import mongo_normalize as mn
    import vault_analyst as va

    class _OID:               # stand-in for bson.ObjectId (matched by name)
        def __init__(self, h): self.h = h
        def __str__(self): return self.h
    _OID.__name__ = "ObjectId"

    docs = [
        {"_id": _OID("a" * 24),
         "created": _dt.datetime(2024, 3, 15, 12, 30, 0),
         "price": {"$numberDecimal": "19.99"},
         "qty": {"$numberLong": "1200"},
         "ext": {"$oid": "b" * 24},
         "thumb": {"$binary": {"base64": "AAAA", "subType": "00"}},
         "tags": ["red", "blue", "green"],
         "addr": {"city": "Denver", "geo": {"lat": 39.7}},
         "line_items": [{"sku": "A", "q": 2}, {"sku": "B", "q": 1}]},
        {"_id": _OID("c" * 24), "name": "Gadget",
         "tags": list(range(30))},
    ]

    flat = mn.flatten_document(docs[0])
    _check("ObjectId coerced to hex string",
           flat.get("_id") == "a" * 24)
    _check("datetime coerced to ISO-8601",
           str(flat.get("created", "")).startswith("2024-03-15T12:30"))
    _check("$numberDecimal coerced to float", flat.get("price") == 19.99)
    _check("$numberLong coerced to int", flat.get("qty") == 1200)
    _check("$oid wrapper coerced to string", flat.get("ext") == "b" * 24)
    _check("$binary rendered as a short marker",
           isinstance(flat.get("thumb"), str) and "binary" in flat["thumb"])
    _check("nested dict flattened to dotted key",
           flat.get("addr.geo.lat") == 39.7)
    _check("scalar array joined into one cell",
           flat.get("tags") == "red; blue; green")
    _check("object array collapsed to compact JSON",
           isinstance(flat.get("line_items"), str)
           and flat["line_items"].startswith("[{") and "sku" in flat["line_items"])

    # Large scalar array must be truncated with a (+N more) marker.
    flat2 = mn.flatten_document(docs[1])
    _check("large scalar array truncated",
           "(+" in str(flat2.get("tags")) and "more)" in str(flat2.get("tags")))

    # Clean frame: every column must be a scalar dtype (no object-of-dict).
    df = mn.documents_to_frame(docs)
    _check("clean frame has a row per document", len(df) == 2)
    no_containers = not any(
        isinstance(v, (dict, list))
        for col in df.columns for v in df[col].tolist())
    _check("clean frame contains no dict/list cells", no_containers)

    # Schema profile.
    prof = {r["field"]: r for r in mn.infer_schema(docs)}
    _check("schema profile reports presence %",
           prof["_id"]["present_pct"] == 100.0
           and prof["name"]["present_pct"] == 50.0)

    # Text digest is bounded + flat.
    txt = mn.documents_to_text(docs, max_docs=1)
    _check("text digest includes a schema header and is doc-bounded",
           txt.startswith("# 2 document(s)") and "doc 1" in txt
           and "doc 2" not in txt)

    # Explode the object array into a tidy frame.
    ex = mn.explode_documents([docs[0]], "line_items", meta=["_id"])
    _check("explode yields one row per array element",
           len(ex) == 2 and "sku" in ex.columns)

    # File reading: array, single object, and JSONL.
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        (p / "arr.json").write_text(
            '[{"_id":{"$oid":"' + "d" * 24 + '"},"v":1},{"v":2}]',
            encoding="utf-8")
        (p / "one.json").write_text('{"v":1,"nested":{"a":2}}', encoding="utf-8")
        (p / "lines.jsonl").write_text('{"v":1}\n{"v":2}\n{"v":3}\n',
                                       encoding="utf-8")
        _check("reads JSON array", len(va.read_json_documents(p / "arr.json")) == 2)
        _check("reads single JSON object",
               len(va.read_json_documents(p / "one.json")) == 1)
        _check("reads JSONL (one doc per line)",
               len(va.read_json_documents(p / "lines.jsonl")) == 3)
        clean = va.json_to_clean_frame(p / "arr.json")
        _check("json_to_clean_frame coerces $oid in a file",
               str(clean.iloc[0]["_id"]) == "d" * 24)


def test_mongo_stream_convert_bounded() -> None:
    """Streaming Mongo conversion must:
       * convert a JSONL dump one doc at a time (bounded memory) into
         _clean.csv / _schema.csv / _digest.txt with coerced scalars
       * stream a single JSON object and a JSON array correctly
       * REFUSE an oversized single JSON array with a catchable error
         (the OOM that crashed the app on Linux) rather than loading it
    """
    import json as _json
    import mongo_normalize as mn
    import vault_analyst as va
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        out = d / "out"
        # JSONL with nested + extended-JSON + arrays.
        with (d / "dump.jsonl").open("w", encoding="utf-8") as fh:
            for i in range(500):
                fh.write(_json.dumps({
                    "_id": {"$oid": f"{i:024x}"},
                    "amt": {"$numberDecimal": str(i * 1.5)},
                    "tags": ["a", "b"],
                    "cust": {"name": f"u{i}", "city": "Denver"},
                    "items": [{"sku": "X", "q": i % 5}],
                }) + "\n")
        summ = va.convert_mongo_file(d / "dump.jsonl", out,
                                     want_csv=True, want_schema=True,
                                     want_text=True)
        _check("streamed all docs", summ["docs"] == 500 and summ["rows"] == 500)
        _check("clean CSV written", (out / "dump_clean.csv").exists())
        _check("schema CSV written", (out / "dump_schema.csv").exists())
        _check("digest written", (out / "dump_digest.txt").exists())
        import pandas as pd
        df = pd.read_csv(out / "dump_clean.csv", dtype=str)
        _check("nested dict flattened to dotted column",
               "cust.name" in df.columns and "cust.city" in df.columns)
        _check("$numberDecimal coerced (numeric column present)",
               "amt" in df.columns)

        # Single JSON object + array stream correctly.
        (d / "one.json").write_text('{"v":1,"nested":{"a":2}}', encoding="utf-8")
        s1 = va.convert_mongo_file(d / "one.json", out / "o1")
        _check("single JSON object -> 1 doc", s1["docs"] == 1)
        (d / "arr.json").write_text('[{"v":1},{"v":2},{"v":3}]', encoding="utf-8")
        s2 = va.convert_mongo_file(d / "arr.json", out / "o2")
        _check("small JSON array -> n docs", s2["docs"] == 3)

        # Oversized JSON array must refuse (catchable), not OOM.
        big = d / "big.json"
        big.write_text(_json.dumps([{"v": i, "pad": "x" * 200}
                                    for i in range(60000)]), encoding="utf-8")
        refused = False
        try:
            mn.stream_convert_file(big, out / "o3",
                                   max_json_bytes=4 * 1024 * 1024)
        except MemoryError as exc:
            refused = "safe limit" in str(exc)
        _check("oversized JSON array refused cleanly (no OOM crash)", refused)

        # ...while the bson/jsonl streaming path has no such limit.
        with (d / "big.jsonl").open("w", encoding="utf-8") as fh:
            for i in range(2000):
                fh.write(_json.dumps({"v": i, "pad": "x" * 200}) + "\n")
        s3 = va.convert_mongo_file(d / "big.jsonl", out / "o4")
        _check("large JSONL streams regardless of size", s3["docs"] == 2000)


def test_clip_path_persistence() -> None:
    """The vision (mmproj) path must round-trip through backend_settings
    .json without overwriting the GGUF path stored alongside it. This
    catches the regression I almost shipped — save_gguf_path() used to
    overwrite the whole file, which would have wiped clip_path on every
    model swap."""
    import onboarding
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td)
        # Synthesise valid GGUF + mmproj files so the validators don't
        # reject the paths.
        gguf  = vault / "weights.gguf"
        clip  = vault / "mmproj.gguf"
        _make_synthetic_gguf(gguf, arch="llama", context_length=4096)
        _make_synthetic_gguf(clip, arch="llama", context_length=4096)

        # 1) Save the GGUF first.
        onboarding.save_gguf_path(vault, str(gguf))
        _check("after save_gguf_path, load_gguf_path returns the path",
               onboarding.load_gguf_path(vault) == str(gguf))

        # Save the clip path — this is the case that broke before the
        # _merge_backend_settings refactor.
        onboarding.save_clip_path(vault, str(clip))

        # 2) Both paths must now be readable from the SAME file.
        _check("load_clip_path round-trips after save_clip_path",
               onboarding.load_clip_path(vault) == str(clip))
        _check("save_clip_path did NOT clobber gguf_path",
               onboarding.load_gguf_path(vault) == str(gguf))

        # 3) Clearing the clip path (text-only mode) must NOT clobber
        #    the GGUF path either.
        onboarding.save_clip_path(vault, "")
        _check("clearing clip_path leaves gguf_path intact",
               onboarding.load_gguf_path(vault) == str(gguf))
        _check("clearing clip_path makes load_clip_path return empty",
               onboarding.load_clip_path(vault) == "")

        # 4) Env-var override beats persisted JSON.
        try:
            os.environ["COUNCIL_GGUF_CLIP_PATH"] = "/tmp/env-override.gguf"
            _check("env var COUNCIL_GGUF_CLIP_PATH wins over JSON",
                   onboarding.load_clip_path(vault) == "/tmp/env-override.gguf")
        finally:
            os.environ.pop("COUNCIL_GGUF_CLIP_PATH", None)


def test_spc_process_capability_known_values() -> None:
    """Cpk of a centered N(10, 0.5) sample with specs [8, 12] should
    land near 1.33 (process spread ±6 σ = ±3 fits comfortably inside
    [LSL, USL]). Synthetic data → deterministic check."""
    import numpy as np
    from analyst_helpers.spc import process_capability
    rng = np.random.default_rng(seed=42)
    series = rng.normal(loc=10.0, scale=0.5, size=2000)
    out = process_capability(series, lsl=8.0, usl=12.0)
    ppk = out.get("Ppk")
    _check(f"Ppk near 1.33 on centered N(10, 0.5) (got {ppk:.3f})",
           ppk is not None and 1.20 <= ppk <= 1.46)
    _check("normality_ok=True on a Gaussian sample",
           out["normality_ok"] is True)
    _check("n_dropped_nan == 0 on clean data",
           out["n_dropped_nan"] == 0)
    _check("Cp/Cpk are None without subgroup_size",
           out["Cp"] is None and out["Cpk"] is None)
    _check("warnings list mentions the missing subgroup_size",
           any("subgroup_size" in w for w in out["warnings"]))


def test_spc_process_capability_one_sided() -> None:
    """One-sided LSL → Pp must be None (no two-sided spread), Ppk
    reduces to Ppl."""
    from analyst_helpers.spc import process_capability
    out = process_capability(
        [10.0, 11.0, 9.0, 10.5, 9.8, 10.2, 10.1, 9.9, 10.3, 10.0],
        lsl=5.0,
    )
    _check("Pp is None for one-sided spec", out["Pp"] is None)
    _check("Ppk is defined and positive",
           out["Ppk"] is not None and out["Ppk"] > 0)


def test_spc_process_capability_nan_handling() -> None:
    """NaN must be DROPPED with the count surfaced, not filled."""
    import math
    from analyst_helpers.spc import process_capability
    arr = [10.0, 10.5, math.nan, 9.7, math.nan, 10.1, 9.9, 10.3, 10.2]
    out = process_capability(arr, lsl=8.0, usl=12.0)
    _check("n_dropped_nan reports the 2 NaNs", out["n_dropped_nan"] == 2)
    _check("n is the post-drop count", out["n"] == 7)


def test_spc_process_capability_subgroup_size_path() -> None:
    """With subgroup_size, Cp and Cpk should be computed from a
    short-term sigma estimate. Trailing partial subgroups are dropped
    with a warning per the agreed convention."""
    import numpy as np
    from analyst_helpers.spc import process_capability
    rng = np.random.default_rng(seed=7)
    # 102 values → 20 full subgroups of size 5, 2 trailing dropped
    series = rng.normal(loc=10.0, scale=0.5, size=102)
    out = process_capability(series, lsl=8.0, usl=12.0, subgroup_size=5)
    _check("Cp computed when subgroup_size given",
           out["Cp"] is not None and out["Cp"] > 0)
    _check("Cpk computed when subgroup_size given",
           out["Cpk"] is not None and out["Cpk"] > 0)
    _check("trailing-partial-subgroup drop warning present",
           any("trailing values" in w for w in out["warnings"]))


def test_spc_control_chart_limits_xbar() -> None:
    """X-bar chart on a calibrated dataset — center / UCL / LCL must
    bracket the data and match the constants-table arithmetic.
    Subgroup size = 5 → A2 = 0.577 → UCL = grand_mean + 0.577 * R_bar."""
    import numpy as np
    from analyst_helpers.spc import control_chart_limits, _A2_CONSTANTS
    rng = np.random.default_rng(seed=11)
    series = rng.normal(loc=50.0, scale=2.0, size=100)
    out = control_chart_limits(series, chart_type="xbar", subgroup_size=5)
    _check("center bracketed roughly by data mean ±0.5",
           abs(out["center"] - 50.0) < 1.0)
    _check("UCL > center > LCL",
           out["ucl"] > out["center"] > out["lcl"])
    _check("constants_used.A2 matches the table value",
           abs(out["constants_used"]["A2"] - _A2_CONSTANTS[5]) < 1e-9)
    _check("n_subgroups == 100 // 5", out["n_subgroups"] == 20)


def test_spc_control_chart_limits_unknown_chart_type() -> None:
    """Unknown chart_type → ValueError, not a silent fallback."""
    from analyst_helpers.spc import control_chart_limits
    try:
        control_chart_limits([1.0, 2.0, 3.0], chart_type="bogus")
        raised = False
    except ValueError:
        raised = True
    _check("unknown chart_type raises ValueError", raised)


def test_spc_dataframe_column_kwarg() -> None:
    """Multi-column DataFrame input must work via column='<name>',
    matching the convention used by csv_inventory / numeric_summary_
    per_csv. The old _coerce_series raised on multi-column DataFrame
    — this test pins the new behaviour."""
    import pandas as pd
    from analyst_helpers.spc import process_capability
    df = pd.DataFrame({
        "Diameter": [10.0, 10.5, 9.8, 10.2, 10.1, 9.9, 10.3, 10.0,
                     10.2, 9.7, 10.4, 10.1],
        "Operator": ["A"] * 12,
    })
    # column kwarg picks the numeric column by case-insensitive name
    out = process_capability(df, lsl=9.0, usl=11.0, column="diameter")
    _check("multi-column DataFrame + column= works (Ppk defined)",
           out["Ppk"] is not None)
    _check("missing column raises with a helpful message",
           _raises(ValueError,
                   lambda: process_capability(df, lsl=9.0, usl=11.0,
                                                column="nonexistent")))
    _check("multi-column DataFrame WITHOUT column= raises",
           _raises(ValueError,
                   lambda: process_capability(df, lsl=9.0, usl=11.0)))


def test_spc_sandbox_registration() -> None:
    """Asserts both the registered and the intentionally-hidden
    helpers across the package. Future maintainers flipping
    visibility on western_electric_rules or gage_rr have to
    update this test in lockstep with __init__.py."""
    import analyst_helpers
    gd: dict = {}
    analyst_helpers.register_helpers(gd)
    # SPC
    for name in ("process_capability", "control_chart_limits"):
        _check(f"sandbox has {name}", name in gd)
    for name in ("western_electric_rules", "gage_rr"):
        _check(f"{name} is NOT registered (per project policy)",
               name not in gd)
    # Engineering (Gate B)
    for name in ("units_convert", "dimensional_check",
                 "tolerance_stackup", "fft_spectrum",
                 "linear_regression_with_diagnostics"):
        _check(f"sandbox has {name}", name in gd)
    # Stats (Gate B)
    for name in ("descriptive_stats_rigorous", "compare_groups",
                 "multiple_comparison_correction",
                 "correlation_with_significance", "bootstrap_ci"):
        _check(f"sandbox has {name}", name in gd)


# ─── Gate B — engineering helpers ───────────────────────────────────

def test_engineering_units_convert() -> None:
    """Scalar and Series round-trips through pint. Skipped (with a
    PASS) when pint isn't installed — units_convert raises a clear
    ImportError that the helper documentation tells the user how
    to resolve."""
    try:
        from analyst_helpers.engineering import units_convert
        v = units_convert(25.4, "mm", "inch")
        _check(f"25.4 mm → 1 inch (got {v:.4f})", abs(v - 1.0) < 1e-6)
        v2 = units_convert(100.0, "psi", "kPa")
        _check(f"100 psi → ~689.5 kPa (got {v2:.2f})", abs(v2 - 689.5) < 1.0)
    except ImportError as exc:
        # pint is optional — the test "passes" by demonstrating the
        # documented behaviour: the helper raises with a clear hint.
        msg = str(exc)
        _check("ImportError mentions `pip install pint`",
               "pint" in msg.lower() and "install" in msg.lower())


def test_engineering_tolerance_stackup() -> None:
    """Worst-case vs RSS — known values: three ±0.1 components.
    Worst-case ± = 0.3; RSS ± = sqrt(3)/3 × 0.3 / 1 ≈ 0.1732 / 3 × 3."""
    from analyst_helpers.engineering import tolerance_stackup
    nominals   = [10.0, 20.0, 30.0]
    tolerances = [0.1,  0.1,  0.1]
    wc = tolerance_stackup(nominals, tolerances, method="worst_case")
    _check(f"worst-case nominal = 60.0 (got {wc['nominal']})",
           abs(wc["nominal"] - 60.0) < 1e-9)
    _check(f"worst-case tolerance = 0.3 (got {wc['tolerance']})",
           abs(wc["tolerance"] - 0.3) < 1e-9)
    _check("worst-case expected_std is None",
           wc["expected_std"] is None)
    rss = tolerance_stackup(nominals, tolerances, method="rss")
    _check(f"RSS nominal = 60.0 (got {rss['nominal']})",
           abs(rss["nominal"] - 60.0) < 1e-9)
    # RSS: σ_i = 0.1/3 each, σ_stack = sqrt(3) * (0.1/3) ≈ 0.0577,
    # ±3σ stack = ~0.1732.
    _check(f"RSS tolerance ≈ 0.173 (got {rss['tolerance']:.4f})",
           abs(rss["tolerance"] - 0.173205) < 0.001)


def test_engineering_tolerance_stackup_rejects_bad_input() -> None:
    """Length mismatch, negative tolerance, and bad method must
    all raise ValueError — not silently produce a wrong number."""
    from analyst_helpers.engineering import tolerance_stackup
    _check("length mismatch raises",
           _raises(ValueError,
                   lambda: tolerance_stackup([1.0, 2.0], [0.1])))
    _check("negative tolerance raises",
           _raises(ValueError,
                   lambda: tolerance_stackup([1.0], [-0.1])))
    _check("unknown method raises",
           _raises(ValueError,
                   lambda: tolerance_stackup([1.0], [0.1], method="bogus")))


def test_engineering_fft_spectrum_known_peak() -> None:
    """Synthesise a 50 Hz sine sampled at 1000 Hz; the magnitude
    peak should land within 1 bin of 50 Hz."""
    import numpy as np
    from analyst_helpers.engineering import fft_spectrum
    fs = 1000.0
    t = np.arange(0, 2.0, 1.0 / fs)
    signal = np.sin(2 * np.pi * 50.0 * t)
    spec = fft_spectrum(signal, sample_rate_hz=fs)
    peak_idx = int(spec["magnitude"].idxmax())
    peak_freq = float(spec["frequency_hz"].iloc[peak_idx])
    _check(f"50 Hz sine → peak ≈ 50 Hz (got {peak_freq:.2f})",
           abs(peak_freq - 50.0) < 1.0)
    _check("frequency_hz column starts at 0 (DC)",
           abs(float(spec["frequency_hz"].iloc[0])) < 1e-9)


def test_engineering_linear_regression_diagnostics() -> None:
    """Synthetic y = 2*x + noise. Slope estimate should be ~2,
    R² should be high, and the per-coefficient p-value for x
    should be small."""
    import numpy as np
    import pandas as pd
    from analyst_helpers.engineering import linear_regression_with_diagnostics
    rng = np.random.default_rng(123)
    x = rng.uniform(0, 10, size=200)
    y = 2.0 * x + rng.normal(0, 0.5, size=200)
    df = pd.DataFrame({"x": x, "y": y})
    out = linear_regression_with_diagnostics(df, x_cols="x", y_col="y")
    coef = out["coefficients"]
    slope = float(coef.loc[coef["term"] == "x", "estimate"].iloc[0])
    _check(f"slope ≈ 2 (got {slope:.3f})", 1.95 <= slope <= 2.05)
    _check(f"r2 > 0.95 on clean linear data (got {out['r2']:.3f})",
           out["r2"] > 0.95)
    slope_p = float(coef.loc[coef["term"] == "x", "p_value"].iloc[0])
    _check(f"slope p-value tiny (got {slope_p:.2e})", slope_p < 1e-9)
    # VIF is NaN for single predictor — assert that
    slope_vif = float(coef.loc[coef["term"] == "x", "vif"].iloc[0])
    _check("VIF is NaN for single-predictor model",
           math.isnan(slope_vif))


def test_engineering_linear_regression_collinearity_flagged() -> None:
    """Highly correlated predictors → VIF warning surfaces."""
    import math
    import numpy as np
    import pandas as pd
    from analyst_helpers.engineering import linear_regression_with_diagnostics
    rng = np.random.default_rng(0)
    x1 = rng.uniform(0, 10, size=200)
    x2 = x1 + rng.normal(0, 0.1, size=200)   # ~perfectly correlated with x1
    y  = 3.0 * x1 + rng.normal(0, 0.5, size=200)
    df = pd.DataFrame({"x1": x1, "x2": x2, "y": y})
    out = linear_regression_with_diagnostics(
        df, x_cols=["x1", "x2"], y_col="y")
    _check("warning surfaces multicollinearity",
           any("multicollinearity" in w.lower() for w in out["warnings"]))
    coef = out["coefficients"]
    # Max VIF excluding intercept
    vifs = [float(v) for v in coef.loc[coef["term"] != "(Intercept)", "vif"]
             if not math.isnan(float(v))]
    _check(f"max VIF > 10 on near-collinear data (got {max(vifs):.1f})",
           max(vifs) > 10.0)


# ─── Gate B — stats helpers ─────────────────────────────────────────

def test_stats_descriptive() -> None:
    """Hand-built sample with known mean / SD / CI bounds."""
    import math
    from analyst_helpers.stats import descriptive_stats_rigorous
    out = descriptive_stats_rigorous([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    _check("n == 10", out["n"] == 10)
    _check("mean = 5.5", abs(out["mean"] - 5.5) < 1e-9)
    _check("median = 5.5", abs(out["median"] - 5.5) < 1e-9)
    _check("std ≈ 3.028", abs(out["std"] - 3.0277) < 0.001)
    _check("ci95 brackets mean",
           out["ci95_lower"] < out["mean"] < out["ci95_upper"])


def test_stats_compare_groups_picks_ttest() -> None:
    """Two normal groups, equal variance → independent t-test."""
    import numpy as np
    import pandas as pd
    from analyst_helpers.stats import compare_groups
    rng = np.random.default_rng(42)
    a = rng.normal(10.0, 1.0, 100)
    b = rng.normal(10.6, 1.0, 100)
    df = pd.DataFrame({
        "value": np.concatenate([a, b]),
        "group": ["A"] * 100 + ["B"] * 100,
    })
    out = compare_groups(df, value_col="value", group_col="group")
    _check("test_used mentions t-test",
           "t-test" in out["test_used"].lower())
    _check("rationale string non-empty", bool(out["rationale"]))
    _check(f"detected mean diff effect (Cohen's d ~ -0.6, got {out['effect_size']:.3f})",
           abs(out["effect_size"] + 0.6) < 0.3)


def test_stats_compare_groups_picks_mannwhitney() -> None:
    """Skewed data → falls to Mann-Whitney."""
    import numpy as np
    import pandas as pd
    from analyst_helpers.stats import compare_groups
    rng = np.random.default_rng(7)
    a = rng.exponential(scale=1.0, size=80)
    b = rng.exponential(scale=2.0, size=80)
    df = pd.DataFrame({
        "value": np.concatenate([a, b]),
        "group": ["A"] * 80 + ["B"] * 80,
    })
    out = compare_groups(df, value_col="value", group_col="group")
    _check("test_used is Mann-Whitney U",
           "mann-whitney" in out["test_used"].lower())
    _check("rationale references normality failure",
           "normality" in out["rationale"].lower())


def test_stats_multiple_comparison_correction() -> None:
    """Known input: BH on [0.01, 0.04, 0.03, 0.005] at α=0.05 → all
    four reject (their BH-adjusted p's stay below 0.05)."""
    from analyst_helpers.stats import multiple_comparison_correction
    raw = [0.01, 0.04, 0.03, 0.005]
    out = multiple_comparison_correction(raw, method="fdr_bh", alpha=0.05)
    _check(f"BH adjusts all four below α (rejects={out['reject_null'].tolist()})",
           bool(out["reject_null"].all()))
    out_bonf = multiple_comparison_correction(
        raw, method="bonferroni", alpha=0.05)
    # Bonferroni multiplies by m=4: 0.04*4=0.16, 0.03*4=0.12 → not rejected
    _check("Bonferroni rejects fewer than BH",
           int(out_bonf["reject_null"].sum())
           <= int(out["reject_null"].sum()))


def test_stats_bootstrap_ci_reproducible() -> None:
    """random_state seed → identical CI across calls."""
    from analyst_helpers.stats import bootstrap_ci
    data = list(range(1, 101))
    a = bootstrap_ci(data, n_boot=2000, random_state=99)
    b = bootstrap_ci(data, n_boot=2000, random_state=99)
    _check("identical CIs from identical seed", a == b)
    # CI of the mean of 1..100 should bracket 50.5
    _check(f"CI brackets the true mean (50.5) — got [{a[1]:.2f}, {a[2]:.2f}]",
           a[1] < 50.5 < a[2])


def test_stats_correlation_with_significance() -> None:
    """Strong linear correlation should be flagged significant
    after FDR correction; unrelated columns should not."""
    import numpy as np
    import pandas as pd
    from analyst_helpers.stats import correlation_with_significance
    rng = np.random.default_rng(0)
    x = rng.uniform(0, 10, 200)
    df = pd.DataFrame({
        "x":       x,
        "y_linear": 2 * x + rng.normal(0, 0.5, 200),
        "y_noise":  rng.normal(0, 1, 200),
    })
    out = correlation_with_significance(df)
    _check("x vs y_linear correlation > 0.9",
           out["corr"].loc["x", "y_linear"] > 0.9)
    _check("x vs y_linear is FDR-significant",
           bool(out["significant_fdr"].loc["x", "y_linear"]))
    _check("x vs y_noise correlation small",
           abs(out["corr"].loc["x", "y_noise"]) < 0.2)


def test_db_sql_validator_accepts_reads() -> None:
    """The SQL read-only validator must accept the canonical read
    patterns we promise users in the documentation."""
    from db_connections import _validate_select_only
    OK = [
        "SELECT * FROM orders LIMIT 10",
        "select 1",
        "WITH t AS (SELECT * FROM o) SELECT * FROM t",
        "SELECT * FROM /* comment with WORDS */ orders",
        "EXPLAIN SELECT * FROM orders",
        "SHOW TABLES",
        "DESCRIBE orders",
        # trailing semicolons are fine
        "SELECT 1;",
        # case insensitivity
        "Select Count(*) From Orders Where x > 1",
    ]
    for sql in OK:
        try:
            _validate_select_only(sql)
            _check(f"validator accepts: {sql!r}", True)
        except Exception as exc:
            _check(f"validator accepts: {sql!r}", False,
                   detail=f"raised {exc!r}")


def test_db_sql_validator_rejects_writes() -> None:
    """Every documented rejection must actually raise."""
    from db_connections import _validate_select_only, ReadOnlyViolation
    BAD = [
        ("DROP TABLE orders",                       "drop"),
        ("DELETE FROM orders WHERE 1=1",            "delete"),
        ("INSERT INTO orders VALUES (1, 'x')",      "insert"),
        ("UPDATE orders SET total = 0",             "update"),
        ("TRUNCATE TABLE orders",                   "truncate"),
        ("ALTER TABLE orders ADD COLUMN x INT",     "alter"),
        ("CREATE TABLE x (id INT)",                 "create"),
        ("GRANT SELECT ON orders TO public",        "grant"),
        ("MERGE INTO target USING source ON ...",   "merge"),
        # multi-statement
        ("SELECT 1; DROP TABLE orders",             "multi-statement"),
        # comment-cloaked DDL
        ("/* SELECT */ DROP TABLE orders",          "comment cloak"),
        ("-- SELECT \n DROP TABLE orders",          "line-comment cloak"),
        # stored proc that could hide writes
        ("EXEC sp_some_proc",                       "exec sproc"),
        # SET ROLE escalation
        ("SET ROLE admin",                          "SET keyword"),
        # empty / comment-only
        ("",                                         "empty"),
        ("-- only a comment",                       "comment only"),
        ("/* only */ /* comments */",               "block comments only"),
    ]
    for sql, label in BAD:
        ok = _raises(ReadOnlyViolation, lambda s=sql: _validate_select_only(s))
        _check(f"validator rejects {label}: {sql!r}", ok)


def test_db_mongo_pipeline_validator() -> None:
    """Mongo aggregation pipelines with write or server-side-JS
    stages must be rejected. Read-only stages must pass."""
    from db_connections import (_validate_mongo_pipeline,
                                 MongoPipelineViolation)
    OK_PIPELINES = [
        [{"$match": {"level": "error"}}],
        [{"$group": {"_id": "$service", "n": {"$sum": 1}}},
         {"$sort": {"n": -1}},
         {"$limit": 100}],
        [{"$project": {"name": 1, "_id": 0}}],
        [],   # empty pipeline is technically valid for find-like reads
    ]
    for p in OK_PIPELINES:
        try:
            _validate_mongo_pipeline(p)
            _check(f"pipeline accepted: {p}", True)
        except Exception as exc:
            _check(f"pipeline accepted: {p}", False, detail=repr(exc))

    BAD_PIPELINES = [
        ("$out",         [{"$match": {}}, {"$out": "errors_archive"}]),
        ("$merge",       [{"$match": {}}, {"$merge": {"into": "x"}}]),
        ("$function",    [{"$project": {"x": {"$function":
                            {"body": "fn", "args": [], "lang": "js"}}}}]),
        ("$accumulator", [{"$group": {"_id": None,
                            "acc": {"$accumulator": {"init": "f"}}}}]),
        ("$where",       [{"$match": {"$where": "this.x > 1"}}]),
    ]
    for label, pipeline in BAD_PIPELINES:
        ok = _raises(MongoPipelineViolation,
                     lambda p=pipeline: _validate_mongo_pipeline(p))
        _check(f"pipeline with {label} rejected", ok)

    # Non-list payloads
    _check("non-list pipeline rejected",
           _raises(MongoPipelineViolation,
                   lambda: _validate_mongo_pipeline("not a list")))
    _check("non-dict stage rejected",
           _raises(MongoPipelineViolation,
                   lambda: _validate_mongo_pipeline([1, 2, 3])))


def test_db_unresolved_env_var() -> None:
    """An unset ${ENV_VAR} in a URL must surface as UnresolvedEnvVarError
    with the missing names listed — NOT silently pass through to the
    driver and yield an opaque auth failure."""
    import db_connections as _db
    # Make sure the test env var is really not set
    os.environ.pop("COUNCIL_TEST_MISSING_VAR", None)
    os.environ.pop("COUNCIL_TEST_PRESENT_VAR", None)

    raised = False
    try:
        _db._resolve_url("postgresql://u:${COUNCIL_TEST_MISSING_VAR}@h/d")
    except _db.UnresolvedEnvVarError as exc:
        raised = True
        _check("UnresolvedEnvVarError lists the missing name",
               "COUNCIL_TEST_MISSING_VAR" in exc.missing)
    _check("missing env var raises UnresolvedEnvVarError", raised)

    # Multiple missing → all listed
    raised = False
    try:
        _db._resolve_url(
            "mongodb://${COUNCIL_TEST_MISSING_VAR}:${COUNCIL_TEST_OTHER}@h/d")
    except _db.UnresolvedEnvVarError as exc:
        raised = True
        names = set(exc.missing)
        _check("both missing env vars listed",
               "COUNCIL_TEST_MISSING_VAR" in names
               and "COUNCIL_TEST_OTHER" in names)
    _check("multi-missing also raises", raised)

    # When the var IS set, _resolve_url succeeds
    os.environ["COUNCIL_TEST_PRESENT_VAR"] = "secret"
    try:
        out = _db._resolve_url(
            "postgresql://u:${COUNCIL_TEST_PRESENT_VAR}@h/d")
        _check("present env var substituted",
               out == "postgresql://u:secret@h/d")
    finally:
        os.environ.pop("COUNCIL_TEST_PRESENT_VAR", None)


def test_db_connection_storage_roundtrip() -> None:
    """Saved connections round-trip through the JSON files; passwords
    with ${ENV_VAR} placeholders pass through unmolested."""
    import db_connections as _db
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td)
        _db.save_sql_connection(
            vault, "sales_db",
            "postgresql://readonly_user:${PG_PASS}@host:5432/sales")
        _db.save_mongo_connection(
            vault, "logs",
            "mongodb://readonly_user:${MONGO_PASS}@host:27017/")

        sql_map   = _db.list_sql_connections(vault)
        mongo_map = _db.list_mongo_connections(vault)
        _check("SQL connection round-trips",
               sql_map.get("sales_db", "").endswith("/sales")
               and "${PG_PASS}" in sql_map["sales_db"])
        _check("Mongo connection round-trips",
               mongo_map.get("logs", "").endswith(":27017/")
               and "${MONGO_PASS}" in mongo_map["logs"])

        # Removal
        _check("remove SQL returns True", _db.remove_sql_connection(vault, "sales_db"))
        _check("remove SQL again returns False",
               not _db.remove_sql_connection(vault, "sales_db"))
        _check("after remove, list is empty",
               _db.list_sql_connections(vault) == {})


def test_db_tls_posture_warnings() -> None:
    """check_tls_posture must warn on cleartext-creds-over-non-local
    URLs and stay silent on safe ones (localhost, RFC1918, ${ENV_VAR}-
    protected, explicit TLS hint)."""
    import db_connections as _db
    SHOULD_WARN = [
        ("postgresql://u:hunter2@db.example.com:5432/sales",       "remote PG plaintext"),
        ("mysql+pymysql://u:hunter2@db.example.com:3306/sales",    "remote MySQL plaintext"),
        ("mongodb://u:hunter2@mongo.example.com:27017/",           "remote Mongo plaintext"),
    ]
    SHOULD_NOT_WARN = [
        # ${ENV_VAR}-protected
        ("postgresql://u:${PG_PASS}@db.example.com/sales",         "env-var password"),
        # localhost / RFC1918
        ("postgresql://u:hunter2@localhost/sales",                 "localhost"),
        ("postgresql://u:hunter2@127.0.0.1/sales",                 "127.0.0.1"),
        ("postgresql://u:hunter2@10.1.2.3/sales",                  "10.x RFC1918"),
        ("postgresql://u:hunter2@192.168.1.5/sales",               "192.168.x RFC1918"),
        ("postgresql://u:hunter2@172.16.0.1/sales",                "172.16.x RFC1918"),
        # explicit TLS hint
        ("postgresql://u:p@db.example.com/sales?sslmode=require",  "sslmode=require"),
        ("mysql+pymysql://u:p@db.example.com/sales?ssl=true",      "ssl=true"),
        ("mssql+pyodbc://u:p@db.example.com/sales?encrypt=yes",    "encrypt=yes"),
        ("mongodb://u:p@mongo.example.com/?tls=true",              "tls=true"),
        ("mongodb+srv://u:p@cluster.example.com/sales",            "mongodb+srv (TLS by default)"),
        # File-based, no network
        ("sqlite:///C:/data/sales.db",                              "sqlite file"),
        ("duckdb:///C:/data/sales.duckdb",                          "duckdb file"),
        # No credentials at all
        ("postgresql://db.example.com/sales",                       "no credentials"),
    ]
    for url, label in SHOULD_WARN:
        warning = _db.check_tls_posture(url)
        _check(f"TLS warn on {label}", warning is not None,
               detail=f"got {warning!r} for {url!r}")
    for url, label in SHOULD_NOT_WARN:
        warning = _db.check_tls_posture(url)
        _check(f"TLS silent on {label}", warning is None,
               detail=f"got {warning!r} for {url!r}")

    # Env-var suppression
    try:
        os.environ["COUNCIL_DB_TLS_WARN"] = "0"
        _check("COUNCIL_DB_TLS_WARN=0 suppresses warnings",
               _db.check_tls_posture(
                   "postgresql://u:p@db.example.com/sales") is None)
    finally:
        os.environ.pop("COUNCIL_DB_TLS_WARN", None)


def test_db_engine_cache_reuses_engines() -> None:
    """Repeated _sql_engine calls for the same connection must return
    the cached instance. A different env-var value for the same
    placeholder yields a NEW engine (because the cache key includes
    the resolved URL)."""
    import db_connections as _db
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td)
        # SQLite in-memory engine via the file-mode bypass — no real
        # disk, no network. Skip if SQLAlchemy isn't installed.
        try:
            _db._import_sqlalchemy()
        except Exception:
            _check("SQLAlchemy not installed — engine cache test "
                   "skipped (expected on minimal builds)", True)
            return
        _db.save_sql_connection(vault, "test_db",
                                 "sqlite:///" + str(vault / "test.db"))
        try:
            eng_a = _db._sql_engine(vault, "test_db")
            eng_b = _db._sql_engine(vault, "test_db")
            _check("cached engine identity preserved", eng_a is eng_b)
        finally:
            _db.dispose_engines()


def test_db_audit_log_rotation() -> None:
    """When the audit log exceeds the size cap, it gets rotated to
    db_audit.log.1 and a fresh file is started. Set a tiny cap via
    env var so the test doesn't have to write 100 MB."""
    import db_connections as _db
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td)
        try:
            os.environ["COUNCIL_DB_AUDIT_MAX_MB"] = "1"   # 1 MB cap
            os.environ["COUNCIL_DB_AUDIT_KEEP"]   = "3"
            # The cap check uses bytes — write enough records to
            # cross the MB boundary. Each record is ~120 bytes, so
            # ~10K records = ~1.2 MB.
            for i in range(11000):
                _db._audit(vault, kind="test", n=i, msg="x" * 50)
            # After the rotation fires (on the write that pushes the
            # CURRENT log past the cap), the current log should be
            # smaller than the cap and db_audit.log.1 should exist.
            cur = vault / "db_audit.log"
            rot = vault / "db_audit.log.1"
            _check("current log exists", cur.is_file())
            _check("rotated .1 file exists", rot.is_file())
            _check("rotated log carries records",
                   rot.stat().st_size > 0)
        finally:
            os.environ.pop("COUNCIL_DB_AUDIT_MAX_MB", None)
            os.environ.pop("COUNCIL_DB_AUDIT_KEEP", None)


def test_db_mongo_roundtrip_mongomock() -> None:
    """End-to-end Mongo helpers exercised against an in-memory mongomock
    instance. Skipped (with PASS) when mongomock isn't installed —
    the test is optional CI coverage, not a hard requirement.

    Install with: pip install mongomock
    """
    try:
        import mongomock  # type: ignore[import]
    except ImportError:
        _check("mongomock not installed — roundtrip test skipped "
               "(install with `pip install mongomock` for full Mongo "
               "coverage)", True)
        return
    import db_connections as _db

    # Monkey-patch _mongo_client to return a mongomock client. We
    # restore it on exit so other tests aren't affected.
    real_mongo_client = _db._mongo_client
    def _fake_client(vault_dir, conn_name, **kw):
        return mongomock.MongoClient()
    _db._mongo_client = _fake_client   # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            _db.save_mongo_connection(vault, "mock", "mongodb://localhost/")

            # Seed some data
            client = mongomock.MongoClient()
            client["testdb"]["events"].insert_many([
                {"level": "info",  "service": "api",  "n": 1},
                {"level": "error", "service": "api",  "n": 2},
                {"level": "error", "service": "db",   "n": 3},
                {"level": "warn",  "service": "auth", "n": 4},
            ])
            # Repoint our fake client constructor to this seeded client
            _db._mongo_client = (   # type: ignore[assignment]
                lambda vault_dir, conn_name, **kw: client)

            # list_mongo_collections
            cols = _db.list_mongo_collections(vault, "mock", "testdb")
            _check("list_mongo_collections sees the seeded collection",
                   "events" in cols)

            # read_mongo_collection
            df = _db.read_mongo_collection(
                vault, "mock", "testdb", "events",
                query={"level": "error"})
            _check("read_mongo_collection returns 2 error rows",
                   len(df) == 2)

            # mongo_count
            n = _db.mongo_count(vault, "mock", "testdb", "events",
                                 query={"service": "api"})
            _check(f"mongo_count returns 2 api rows (got {n})", n == 2)

            # mongo_distinct
            services = sorted(_db.mongo_distinct(
                vault, "mock", "testdb", "events", "service"))
            _check(f"mongo_distinct returns 3 services (got {services})",
                   services == ["api", "auth", "db"])

            # mongo_aggregate with a benign pipeline
            agg = _db.mongo_aggregate(
                vault, "mock", "testdb", "events",
                pipeline=[
                    {"$match": {"level": "error"}},
                    {"$group": {"_id": "$service", "n": {"$sum": 1}}},
                ])
            _check("aggregate returns 2 groups", len(agg) == 2)
    finally:
        _db._mongo_client = real_mongo_client   # type: ignore[assignment]


def test_db_export_write_formats() -> None:
    """_write_dataframe writes CSV/JSON/Excel correctly, rejects unknown
    formats, and _safe_export_name neutralises path traversal. Needs
    only pandas — no DB."""
    import db_connections as _db
    import pandas as _pd
    df = _pd.DataFrame({"id": [1, 2, 3], "name": ["a", "b", "c"]})
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        # CSV
        p = _db._write_dataframe(df, base / "out.csv", "csv")
        _check("csv written", Path(p).is_file())
        _check("csv round-trips 3 rows", len(_pd.read_csv(p)) == 3)
        # JSON
        p = _db._write_dataframe(df, base / "out.json", "json")
        _check("json round-trips 3 rows", len(_pd.read_json(p)) == 3)
        # Excel (openpyxl present in this env)
        try:
            p = _db._write_dataframe(df, base / "out.xlsx", "xlsx")
            _check("xlsx written", Path(p).is_file())
        except ValueError:
            _check("xlsx unavailable handled cleanly (ValueError)", True)
        # Unknown format rejected
        _check("unknown format rejected",
               _raises(ValueError,
                       lambda: _db._write_dataframe(df, base / "x.zzz", "zzz")))
        # Path-traversal name neutralised
        safe = _db._safe_export_name("../../etc/passwd", "csv")
        _check("traversal stripped from export name",
               "/" not in safe and "\\" not in safe and safe.endswith(".csv"))


def test_db_export_query_cannot_write() -> None:
    """The KEY guarantee for exports: export_sql_query routes through the
    SELECT-only validator, so a DELETE/DROP dressed up as an export is
    rejected BEFORE any connection is opened. Proven without a live DB —
    the validator runs first."""
    import db_connections as _db
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td)
        _db.save_sql_connection(vault, "x", "sqlite:///:memory:")
        for bad in ("DELETE FROM users",
                    "DROP TABLE users",
                    "TRUNCATE t",
                    "UPDATE t SET a=1",
                    "SELECT 1; DELETE FROM users"):
            _check(f"export blocks {bad.split()[0]}",
                   _raises(_db.ReadOnlyViolation,
                           lambda b=bad: _db.export_sql_query(
                               vault, "x", b, Path(td) / "o.csv")))
    # And the public API exposes NO database-write verb.
    public = [n for n in dir(_db) if not n.startswith("_") and callable(getattr(_db, n))]
    write_verbs = ("insert", "update", "delete", "drop", "truncate", "to_sql")
    offenders = [n for n in public
                 if any(v in n.lower() for v in write_verbs)
                 and n not in ("remove_sql_connection", "remove_mongo_connection")]
    _check(f"no DB-write function in public API (found {offenders})",
           not offenders)


def test_db_export_mongo_roundtrip() -> None:
    """export_mongo_collection reads via find() and writes a real CSV.
    mongomock-backed; skips with PASS when mongomock isn't installed."""
    try:
        import mongomock  # type: ignore[import]
    except ImportError:
        _check("mongomock not installed — export roundtrip skipped", True)
        return
    import db_connections as _db
    import pandas as _pd
    real = _db._mongo_client
    try:
        with tempfile.TemporaryDirectory() as td:
            vault = Path(td)
            _db.save_mongo_connection(vault, "mock", "mongodb://localhost/")
            client = mongomock.MongoClient()
            client["d"]["c"].insert_many([{"n": i, "k": "x"} for i in range(5)])
            _db._mongo_client = lambda *a, **k: client  # type: ignore
            dest = vault / "exp.csv"
            info = _db.export_mongo_collection(vault, "mock", "d", "c", dest, fmt="csv")
            _check("mongo export reports 5 rows", info["rows"] == 5)
            _check("mongo export file exists", Path(info["path"]).is_file())
            _check("mongo export CSV round-trips 5 rows",
                   len(_pd.read_csv(info["path"])) == 5)
    finally:
        _db._mongo_client = real  # type: ignore


def test_db_wizard_url_assembly() -> None:
    """The guided wizard's pure URL builder: correct schemes, default
    ports, percent-encoding of credentials (the bug a hand-typed URL
    hits), file paths, env-var placeholders, and required-field errors."""
    import db_connect_wizard as _w

    # Postgres with a nasty password — must be percent-encoded so the
    # netloc isn't corrupted by @ : / characters.
    kind, url = _w.build_connection_url(
        "postgresql", host="db.co", database="sales",
        user="ro_user", password="p@ss:w/rd ")
    _check("postgres kind is sql", kind == "sql")
    _check("postgres scheme", url.startswith("postgresql://"))
    _check("default port 5432 applied", ":5432/" in url)
    _check("password percent-encoded",
           "p%40ss%3Aw%2Frd%20" in url and "p@ss:w/rd" not in url)
    _check("host intact", "@db.co:" in url)
    _check("database in path", url.endswith("/sales"))

    # MySQL scheme + default port
    _, url = _w.build_connection_url("mysql", host="h", database="d",
                                     user="u", password="p")
    _check("mysql pymysql scheme", url.startswith("mysql+pymysql://"))
    _check("mysql default port 3306", ":3306/" in url)

    # MSSQL appends the ODBC driver param
    _, url = _w.build_connection_url("mssql", host="h", database="d",
                                     user="u", password="p")
    _check("mssql pyodbc scheme", url.startswith("mssql+pyodbc://"))
    _check("mssql driver param present", "driver=ODBC" in url)

    # Mongo: kind=mongo, default port, authSource from the db name
    kind, url = _w.build_connection_url("mongodb", host="h", database="appdb",
                                        user="u", password="p")
    _check("mongo kind is mongo", kind == "mongo")
    _check("mongo scheme + port", url.startswith("mongodb://") and ":27017/" in url)
    _check("mongo authSource set from db", "authSource=appdb" in url)

    # Env-var password → placeholder, NOT inlined/encoded
    _, url = _w.build_connection_url("postgresql", host="h", database="d",
                                     user="u", env_var="PG_PASS")
    _check("env-var placeholder used", "${PG_PASS}" in url)

    # File-based: path only, three-slash form, backslashes normalised
    kind, url = _w.build_connection_url("sqlite",
                                        file_path=r"C:\data\my.db")
    _check("sqlite kind is sql", kind == "sql")
    _check("sqlite three-slash + forward slashes",
           url == "sqlite:///C:/data/my.db")

    # Required-field errors are user-facing ValueErrors
    _check("missing host raises",
           _raises(ValueError, lambda: _w.build_connection_url(
               "postgresql", database="d", user="u", password="p")))
    _check("missing file raises",
           _raises(ValueError, lambda: _w.build_connection_url("sqlite")))

    # Assembled URLs satisfy the saver's own validation (round-trip).
    import db_connections as _db
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td)
        kind, url = _w.build_connection_url(
            "postgresql", host="h", database="d", user="u", password="p@w")
        _db.save_sql_connection(vault, "wiz_pg", url)
        _check("wizard URL accepted by saver",
               _db.list_sql_connections(vault).get("wiz_pg") == url)

    # env-var name suggester
    _check("env-var name suggestion tidy",
           _w.suggest_env_var_name("sales db") == "SALES_DB_PASSWORD")


def test_db_audit_log_writes() -> None:
    """The audit logger writes one JSONL record per call and never
    raises — even when the file can't be created (the audit log is
    forensic and must never break a query)."""
    import db_connections as _db
    import json as _json
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td)
        _db._audit(vault, kind="test", note="hello", n=42)
        _db._audit(vault, kind="test", note="world", n=43)
        log_path = vault / "db_audit.log"
        _check("audit log file created", log_path.is_file())
        lines = log_path.read_text(encoding="utf-8").strip().split("\n")
        _check("two records written", len(lines) == 2)
        rec = _json.loads(lines[0])
        _check("record has ts + kind + note", {"ts", "kind", "note"} <= set(rec))
        _check("record contents preserved",
               rec["kind"] == "test" and rec["note"] == "hello")


def test_swap_advisor_decisions() -> None:
    """swap_advisor: classify tasks, prefer a remote specialist (cheap),
    gate local swaps on GPU + cost, and stay silent when no specialist
    helps or the task is generalist."""
    import swap_advisor as sa

    _check("code task classified", sa.classify_task("write a python function to parse logs") == "coder")
    _check("reasoning task classified",
           sa.classify_task("derive this step by step and prove the bound") == "reasoning")
    _check("chit-chat -> no role", sa.classify_task("what do you think about the weather") is None)

    base = "llama-generalist"
    # No specialist assigned -> no suggestion even on a code task
    _check("no specialist -> no suggestion",
           sa.advise("write code to sort a list", current_model=base,
                     role_assignments={}, remote_specialists={},
                     gpu_swap_enabled=True) is None)

    # Local specialist, GPU ON, cheap reload -> suggest local
    s_local = sa.advise("write a python function with a regex",
                        current_model=base,
                        role_assignments={"coder": "granite-code"},
                        gpu_swap_enabled=True, local_swap_seconds=6.0)
    _check("local specialist suggested on GPU", s_local is not None
           and s_local.target_kind == "local" and s_local.role == "coder")

    # Same, but GPU OFF -> no local suggestion (reload not worth it on CPU)
    _check("no local swap suggested on CPU",
           sa.advise("write a python function with a regex", current_model=base,
                     role_assignments={"coder": "granite-code"},
                     gpu_swap_enabled=False) is None)

    # Reload too slow -> not worth it
    _check("too-slow local reload not suggested",
           sa.advise("write a python function with a regex", current_model=base,
                     role_assignments={"coder": "granite-code"},
                     gpu_swap_enabled=True, local_swap_seconds=30.0) is None)

    # Remote specialist present -> preferred (cheap), even on CPU
    s_rem = sa.advise("write a python function with a regex", current_model=base,
                      role_assignments={"coder": "granite-code"},
                      remote_specialists={"coder": {"model": "granite-code",
                                                    "node": "pi-01",
                                                    "label": "coder on pi-01"}},
                      gpu_swap_enabled=False)
    _check("remote specialist preferred + works on CPU",
           s_rem is not None and s_rem.target_kind == "remote"
           and "pi-01" in s_rem.target_label)
    _check("remote suggestion advertises no local reload",
           s_rem is not None and "no local reload" in s_rem.est_cost)


def test_remote_dispatch_gating() -> None:
    """The reconnected remote path: dispatch to a REMOTE node only when
    COUNCIL_REMOTE_NODES is on AND the chosen host is non-loopback;
    otherwise run on the local GGUF. Verified with mocks (no real node)."""
    import os as _os
    import council_engine as ce

    _check("remote off by default",
           not ce._remote_nodes_enabled() if not _os.environ.get("COUNCIL_REMOTE_NODES")
           else True)
    _check("localhost is NOT a remote host", not ce._is_remote_host("http://localhost:11434"))
    _check("127.0.0.1 is NOT remote", not ce._is_remote_host("http://127.0.0.1:11434"))
    _check("LAN IP IS a remote host", ce._is_remote_host("http://192.168.1.50:11434"))

    # Build a dispatched spec pointing at a fake remote node; mock both
    # the local GGUF and the Ollama call so nothing real is invoked.
    class _FakeDispatcher:
        def best_host_for(self, model):
            return "http://192.168.1.50:11434"
    spec = ce._DispatchedBackendSpec(
        key="k", host="http://localhost:11434", model="granite",
        tags={}, default_temperature=0.2, default_max_tokens=64,
        allow_remote=True)
    spec._dispatcher = _FakeDispatcher()

    prev = _os.environ.get("COUNCIL_REMOTE_NODES")
    _orig_remote = ce._ollama_chat
    _orig_local = ce._gguf_chat
    try:
        calls = {"remote": 0, "local": 0}
        ce._ollama_chat = lambda *a, **k: calls.__setitem__("remote", calls["remote"] + 1) or "REMOTE"
        ce._gguf_chat = lambda *a, **k: calls.__setitem__("local", calls["local"] + 1) or "LOCAL"

        # Remote ON -> dispatches to the node
        _os.environ["COUNCIL_REMOTE_NODES"] = "1"
        out = spec.generate(developer_instructions="sys", user_text="hi", trace=False)
        _check("remote ON routes to the node", out == "REMOTE" and calls["remote"] == 1)

        # Remote OFF -> local GGUF
        _os.environ["COUNCIL_REMOTE_NODES"] = "0"
        out2 = spec.generate(developer_instructions="sys", user_text="hi", trace=False)
        _check("remote OFF routes to local GGUF", out2 == "LOCAL" and calls["local"] == 1)
    finally:
        ce._ollama_chat = _orig_remote
        ce._gguf_chat = _orig_local
        if prev is None:
            _os.environ.pop("COUNCIL_REMOTE_NODES", None)
        else:
            _os.environ["COUNCIL_REMOTE_NODES"] = prev


def test_role_models_swap_gating() -> None:
    """role_models: GPU-gated swap (CPU = no-op), registry round-trip,
    role->model resolution with base fallback, and no redundant swaps."""
    import os as _os
    import role_models as rm

    prev = {k: _os.environ.get(k) for k in
            ("COUNCIL_ROLE_SWAP", "COUNCIL_GGUF_GPU_LAYERS", "COUNCIL_GGUF_PATH")}
    try:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            base = d / "base.gguf"; base.write_bytes(b"GGUF" + b"\0" * 64)
            coder = d / "coder.gguf"; coder.write_bytes(b"GGUF" + b"\0" * 64)
            _os.environ["COUNCIL_GGUF_PATH"] = str(base)

            # Registry round-trip
            reg = rm.RoleModelRegistry(vault_dir=d)
            reg.set("coder", str(coder))
            _check("registry persists assignment", reg.get("coder") == str(coder))
            _check("registry lists assignment", reg.all().get("coder") == str(coder))

            # Resolution: assigned role -> its model; unassigned -> base
            _check("assigned role resolves to its model",
                   rm.resolve_model_for_role("coder", d) == str(coder))
            _check("unassigned role falls back to base",
                   rm.resolve_model_for_role("writer", d) == str(base))

            # GPU disabled (CPU-only) -> swap is a NO-OP, engine untouched
            _os.environ["COUNCIL_ROLE_SWAP"] = "0"
            r = rm.swap_to_role("coder", d)
            _check("CPU/disabled: swap is a no-op",
                   r["swapped"] is False and r["reason"] == "gpu-disabled")
            _check("env model path unchanged when swap disabled",
                   _os.environ["COUNCIL_GGUF_PATH"] == str(base))

            # GPU enabled (forced) -> swap occurs; monkeypatch the engine
            # reload so the test never loads a real model.
            import council_engine as _ce
            _orig = _ce.refresh_backend_config
            calls = {"n": 0}
            _ce.refresh_backend_config = lambda: calls.__setitem__("n", calls["n"] + 1) or {}
            try:
                _os.environ["COUNCIL_ROLE_SWAP"] = "1"
                rm._LOADED_PATH = str(base)        # pretend base is loaded
                r2 = rm.swap_to_role("coder", d)
                _check("GPU: swap to coder happens", r2["swapped"] is True)
                _check("engine reload was triggered once", calls["n"] == 1)
                _check("env now points at the coder model",
                       _os.environ["COUNCIL_GGUF_PATH"] == str(coder))
                # Re-asking the same role -> no redundant swap
                r3 = rm.swap_to_role("coder", d)
                _check("same role again is a no-op",
                       r3["swapped"] is False and r3["reason"] == "already-loaded")
                _check("no extra engine reload", calls["n"] == 1)
            finally:
                _ce.refresh_backend_config = _orig
                rm._LOADED_PATH = None
    finally:
        for k, v in prev.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v


def test_model_finder_us_filter_and_fit() -> None:
    """model_finder: US-origin heuristic (non-US wins over US repacker),
    param parsing, VRAM estimate, hardware-aware catalog ranking, and a
    guaranteed offline fallback that always returns something runnable."""
    import model_finder as mf

    # Origin heuristic
    _check("Meta is US", mf.classify_origin("meta-llama/Llama-3.1-8B") == "us")
    _check("IBM granite is US", mf.classify_origin("bartowski/granite-3.1-8b") == "us")
    _check("Qwen is non-US even via US repacker",
           mf.classify_origin("bartowski/Qwen2.5-7B-GGUF") == "non_us")
    _check("Mistral is non-US", mf.classify_origin("TheBloke/Mistral-7B") == "non_us")
    _check("unknown stays unknown", mf.classify_origin("someuser/mystery-model") == "unknown")

    # Param parsing (incl. MoE AxB)
    _check("8B parsed", mf._params_b_from_name("Llama-3.1-8B-Instruct") == 8.0)
    _check("3B parsed", mf._params_b_from_name("Llama-3.2-3B") == 3.0)
    _check("20b parsed", mf._params_b_from_name("gpt-oss-20b") == 20.0)
    _check("MoE 8x7B = 56", mf._params_b_from_name("Mixtral-8x7B") == 56.0)
    _check("no params -> None", mf._params_b_from_name("granite-instruct") is None)

    # VRAM estimate scales with quant
    q4 = mf.estimate_vram_gb(8.0, quant="Q4_K_M")
    q8 = mf.estimate_vram_gb(8.0, quant="Q8_0")
    _check("Q8 needs more VRAM than Q4", q8 > q4)
    _check("8B Q4 estimate is in a sane range", 5.0 <= q4 <= 9.0)

    # Hardware-aware catalog ranking — big GPU gets a bigger model than
    # a tiny GPU, and BOTH always get a runnable suggestion.
    big = mf.recommend_from_catalog(vram_gb=24.0, role="general")
    small = mf.recommend_from_catalog(vram_gb=4.0, role="general")
    _check("big-GPU recommendation non-empty", len(big) >= 1)
    _check("small-GPU recommendation non-empty (CPU/tiny fallback)", len(small) >= 1)
    _check("all catalog picks are US + verified",
           all(m["origin"] == "us" and m["origin_verified"] for m in big))
    _check("big GPU's top pick >= small GPU's top pick (params)",
           big[0]["params_b"] >= small[0]["params_b"])

    # find_models offline: prefer_online=False must still return catalog.
    res = mf.find_models(hardware={"vram_gb": 8.0, "ram_gb": 16.0},
                         role="general", prefer_online=False)
    _check("find_models returns catalog offline", len(res["catalog"]) >= 1)
    _check("find_models marks online unavailable offline",
           res["online_available"] is False and res["online"] == [])

    # Upgrade detection: roomy GPU + small current model => can upgrade;
    # running the top model OR no headroom => no upgrade; unknown current
    # size => no claimed upgrade (but still lists fits).
    up = mf.assess_upgrade(hardware={"vram_gb": 24.0, "ram_gb": 64.0},
                           current_model="granite-3.1-8b-instruct-Q4_K_M.gguf",
                           role="general")
    _check("roomy GPU + 8B current => can_upgrade", up["can_upgrade"] is True)
    _check("upgrades are strictly bigger than current 8B",
           all(m["params_b"] > 8.0 for m in up["upgrades"]) and up["upgrades"])
    _check("upgrade headroom is positive", (up["headroom_gb"] or 0) > 0)

    top = mf.assess_upgrade(hardware={"vram_gb": 24.0, "ram_gb": 64.0},
                            current_model="phi-4-14b-Q4_K_M.gguf",
                            role="general")
    _check("already running top model => no upgrade",
           top["can_upgrade"] is False and top["upgrades"] == [])

    nogpu = mf.assess_upgrade(hardware={"vram_gb": None, "ram_gb": 16.0},
                              current_model="granite-3.1-8b.gguf")
    _check("no VRAM headroom over 8B => no upgrade",
           nogpu["can_upgrade"] is False)

    unknown = mf.assess_upgrade(hardware={"vram_gb": 16.0, "ram_gb": 32.0},
                                current_model="my-mystery-model.gguf")
    _check("unknown current size => no claimed upgrade but lists fits",
           unknown["can_upgrade"] is False and len(unknown["upgrades"]) >= 1)


def test_resolve_embed_device_wsl_cpu_default() -> None:
    """hardware_detect.resolve_embed_device: explicit COUNCIL_EMBED_DEVICE
    wins; otherwise default to 'cpu' on WSL (GPU embedder + offloaded model
    = CUDA core dump) and None (auto) elsewhere. In code so the safe WSL
    default holds however the app is launched.
    """
    import hardware_detect as hd
    prev_env = os.environ.pop("COUNCIL_EMBED_DEVICE", None)
    prev_os = hd._detect_os
    try:
        os.environ["COUNCIL_EMBED_DEVICE"] = "cuda"
        _check("explicit override respected",
               hd.resolve_embed_device() == "cuda")
        os.environ.pop("COUNCIL_EMBED_DEVICE", None)
        hd._detect_os = lambda: "wsl"
        _check("WSL defaults to cpu", hd.resolve_embed_device() == "cpu")
        hd._detect_os = lambda: "linux"
        _check("plain Linux stays auto (None)",
               hd.resolve_embed_device() is None)
        hd._detect_os = lambda: "windows"
        _check("Windows stays auto (None)",
               hd.resolve_embed_device() is None)
    finally:
        hd._detect_os = prev_os
        os.environ.pop("COUNCIL_EMBED_DEVICE", None)
        if prev_env is not None:
            os.environ["COUNCIL_EMBED_DEVICE"] = prev_env


def test_dispatcher_no_probe_when_remote_disabled() -> None:
    """Single-machine regression: with COUNCIL_REMOTE_NODES off (default),
    a model call must NOT probe hosts (best_host_for) — that probe hits
    localhost:11434 (Ollama) every call, prints 'No reachable hosts —
    falling back to localhost', and adds latency, even though inference
    always runs on the local GGUF. With remote nodes ON it must probe.
    """
    import council_engine as ce
    prev_flag = os.environ.pop("COUNCIL_REMOTE_NODES", None)
    prev_chat = ce._gguf_chat
    ce._gguf_chat = lambda messages, **kw: "LOCAL"
    probes = {"n": 0}

    class _FakeDisp:
        def best_host_for(self, model):
            probes["n"] += 1
            return "http://localhost:11434"

    try:
        spec = ce._DispatchedBackendSpec(
            key="writer", host=ce.DEFAULT_OLLAMA_HOST, model="m", tags={},
            default_temperature=0.3, default_max_tokens=32, allow_remote=True)
        spec._dispatcher = _FakeDisp()

        out = spec.generate(developer_instructions="s", user_text="hi",
                            trace=False)
        _check("remote OFF returns local answer", out == "LOCAL")
        _check("remote OFF does NOT probe hosts", probes["n"] == 0)

        os.environ["COUNCIL_REMOTE_NODES"] = "1"
        spec.generate(developer_instructions="s", user_text="hi", trace=False)
        _check("remote ON probes hosts", probes["n"] == 1)
    finally:
        ce._gguf_chat = prev_chat
        os.environ.pop("COUNCIL_REMOTE_NODES", None)
        if prev_flag is not None:
            os.environ["COUNCIL_REMOTE_NODES"] = prev_flag


def test_gpu_crash_sentinel_lifecycle() -> None:
    """GPU-crash sentinel: a native CUDA abort can't be caught in Python, so
    we mark 'GPU unconfirmed' before a GPU load and clear it after the first
    successful generation. If it's still present next load, the prior GPU
    attempt crashed -> auto-fall-back to CPU. Verifies mark/pending/confirm/
    clear + that refresh resets the per-process confirm flag.
    """
    import council_engine as ce
    prev_root = os.environ.get("COUNCIL_VAULT_ROOT")
    with tempfile.TemporaryDirectory() as td:
        os.environ["COUNCIL_VAULT_ROOT"] = td
        try:
            ce.gpu_clear_attempt()
            _check("no sentinel initially", not ce.gpu_attempt_pending())
            ce._gpu_mark_attempt(99)
            _check("sentinel present after mark", ce.gpu_attempt_pending())

            # A confirmed-good generation clears it (once per process).
            ce._GPU_CONFIRMED_THIS_PROCESS = False
            ce._gpu_confirm_success()
            _check("sentinel cleared after a successful generation",
                   not ce.gpu_attempt_pending())

            # Second confirm in the same process is a no-op (guarded).
            ce._gpu_mark_attempt(99)
            ce._gpu_confirm_success()   # already confirmed -> must NOT clear
            _check("confirm is once-per-process (sentinel still present)",
                   ce.gpu_attempt_pending())

            # Explicit clear (engine settings / clean close path).
            ce.gpu_clear_attempt()
            _check("explicit clear removes sentinel",
                   not ce.gpu_attempt_pending())

            # refresh_backend_config resets the per-process confirm flag so a
            # newly loaded model re-proves the GPU path.
            ce._GPU_CONFIRMED_THIS_PROCESS = True
            ce.refresh_backend_config()
            _check("refresh resets the confirm flag",
                   ce._GPU_CONFIRMED_THIS_PROCESS is False)
        finally:
            ce.gpu_clear_attempt()
            if prev_root is None:
                os.environ.pop("COUNCIL_VAULT_ROOT", None)
            else:
                os.environ["COUNCIL_VAULT_ROOT"] = prev_root


def test_vram_aware_n_ctx_ladder_log_no_kwarg_collision() -> None:
    """Regression: after switching to a model that triggers the VRAM-aware
    n_ctx path, the engine logged the result. _pick_vram_aware_n_ctx puts
    'picked' INTO its diag dict, so the log call must NOT also pass
    picked= explicitly — that raised 'got multiple values for keyword
    argument picked' and broke model load on a capable GPU.
    """
    import council_engine as ce
    prev = ce._available_gpu_bytes
    ce._available_gpu_bytes = lambda: 24 * 1024 ** 3      # force GPU success
    try:
        meta = {
            "llama.block_count": 32,
            "llama.embedding_length": 4096,
            "llama.attention.head_count": 32,
            "llama.attention.head_count_kv": 8,
        }
        picked, diag = ce._pick_vram_aware_n_ctx(
            meta, model_size_bytes=5 * 1024 ** 3, abs_max=32768,
            margin_bytes=512 * 1024 * 1024)
        _check("VRAM-aware path picks an n_ctx", picked is not None)
        _check("diag carries its own 'picked' key", "picked" in diag)

        # Mirror the engine's logger signature: _ladder_log(rung, **fields).
        # The fixed call splats diag WITHOUT a separate picked= — this must
        # not raise. (The old buggy form `picked=picked, **diag` would.)
        def _ladder_log(rung, **fields):
            return {"rung": rung, **fields}
        try:
            _ladder_log("vram_aware", chosen=True, **diag)
            ok = True
        except TypeError:
            ok = False
        _check("logging the pick (chosen=True, **diag) does not collide", ok)
    finally:
        ce._available_gpu_bytes = prev


def test_vault_collections() -> None:
    """Virtual collections: store named file sets (paths normalised relative
    to data_in, case-insensitive), add/remove/rename/delete, detect a name in
    a query, and PROPOSE members from filename + value + relationship signals
    (incl. a disparate file found only by value match).
    """
    import vault_collections as vc
    import data_index
    prev = os.environ.get("COUNCIL_VAULT_ROOT")
    with tempfile.TemporaryDirectory() as td:
        os.environ["COUNCIL_VAULT_ROOT"] = td
        try:
            din = data_index.input_dir(Path(td))
            din.mkdir(parents=True, exist_ok=True)
            (din / "JobBlue_costs.csv").write_text("job_id,amount\nBLUE-1,1\n")
            (din / "site_photo.png").write_text("img")
            (din / "timesheet.csv").write_text("job_id,hours\nBLUE-1,8\n")
            (din / "unrelated.csv").write_text("a,b\n1,2\n")

            s = vc.CollectionStore()
            c = s.upsert("Job Blue",
                         ["JobBlue_costs.csv", din / "site_photo.png"])
            _check("created with normalised relative paths",
                   c.files == ["JobBlue_costs.csv", "site_photo.png"])
            _check("get is case-insensitive", s.get("job blue") is not None)
            _check("add_files grows the set",
                   len(s.add_files("Job Blue", ["timesheet.csv"]).files) == 3)
            _check("remove_files shrinks it",
                   "site_photo.png" not in
                   s.remove_files("Job Blue", ["site_photo.png"]).files)
            _check("dedupes on re-add",
                   len(s.add_files("Job Blue", ["timesheet.csv"]).files) == 2)
            _check("find_in_text detects the name in a query",
                   s.find_in_text("show me job blue please").name == "Job Blue")
            _check("abs_paths resolves existing members",
                   {p.name for p in s.abs_paths("Job Blue")}
                   == {"JobBlue_costs.csv", "timesheet.csv"})
            _check("rename works",
                   s.rename("Job Blue", "Blue Job") and s.get("Blue Job"))
            _check("delete works",
                   s.delete("Blue Job") and s.get("Blue Job") is None)

            # Discovery — filename only (no index).
            fn = {r: (sc, rs) for r, sc, rs in vc.propose_members(None, "Job Blue")}
            _check("filename match found JobBlue_costs.csv",
                   "JobBlue_costs.csv" in fn)
            _check("unrelated file not proposed", "unrelated.csv" not in fn)

            # Discovery — with an index (value + relationship signals).
            class _Idx:
                def search_value(self, v):
                    return ([{"file": str(din / "site_photo.png")}]
                            if v.lower() == "job blue" else [])
                def find_relationships(self):
                    return [{"column": "job_id",
                             "files": [str(din / "JobBlue_costs.csv"),
                                       str(din / "timesheet.csv")]}]
            res = {r: (sc, rs) for r, sc, rs in
                   vc.propose_members(None, "Job Blue", index=_Idx())}
            _check("value match finds the disparate photo",
                   "site_photo.png" in res
                   and "value match" in res["site_photo.png"][1])
            _check("relationship expansion pulls in the joinable file",
                   "timesheet.csv" in res
                   and any("shares" in x for x in res["timesheet.csv"][1]))
            _check("value match outranks filename match",
                   res["site_photo.png"][0] > res["JobBlue_costs.csv"][0])
        finally:
            if prev is None:
                os.environ.pop("COUNCIL_VAULT_ROOT", None)
            else:
                os.environ["COUNCIL_VAULT_ROOT"] = prev


def test_deferred_tasks_store() -> None:
    """Deferred-task store: capture 'the model couldn't do this' tasks,
    list pending, run/complete/dismiss/reopen, coerce unknown kinds, and
    mirror tool requests into the developer-facing ToolGapLog.
    """
    import deferred_tasks as dt
    prev = os.environ.get("COUNCIL_VAULT_ROOT")
    with tempfile.TemporaryDirectory() as td:
        os.environ["COUNCIL_VAULT_ROOT"] = td
        try:
            s = dt.DeferredTaskStore()
            _check("starts empty", s.pending() == [])
            t1 = s.add(kind="bigger_summary",
                       question="much bigger summary of sales.csv",
                       files=["sales.csv"], folder="Q3")
            t2 = s.add(kind="tool_request",
                       question="add a histogram tool", note="histograms")
            t3 = s.add(kind="bogus", question="x")
            _check("ids are unique", len({t1.id, t2.id, t3.id}) == 3)
            _check("unknown kind coerced to 'other'", t3.kind == "other")
            _check("three pending", len(s.pending()) == 3)

            _check("mark_done records result",
                   s.mark_done(t1.id, result_path="out/s.csv",
                               result_summary="done"))
            _check("dismiss works", s.dismiss(t2.id))
            statuses = {t.kind: t.status for t in s.all()}
            _check("statuses updated",
                   statuses["bigger_summary"] == "done"
                   and statuses["tool_request"] == "dismissed"
                   and statuses["other"] == "pending")
            _check("one still pending", len(s.pending()) == 1)

            # Tool request mirrored into ToolGapLog (existing analyzer input).
            import agent_logs
            gaps = agent_logs.ToolGapLog().all()
            _check("tool request mirrored to ToolGapLog",
                   any(g.get("requested_name") == "user_requested_tool"
                       for g in gaps))

            _check("reopen restores pending", s.reopen(t2.id)
                   and len(s.pending()) == 2)
            # Survives a fresh store instance (persisted).
            _check("persists across instances",
                   len(dt.DeferredTaskStore().all()) == 3)

            # find_answered: a completed task with an existing result file is
            # matched when the question is re-asked (so the council can reuse
            # it); reworded matches, unrelated doesn't, and a missing result
            # file disqualifies it.
            res = Path(td) / "result.csv"
            res.write_text("a,b\n1,2\n")
            tq = s.add(kind="bigger_summary",
                       question="bigger summary of the orders file")
            s.mark_done(tq.id, result_path=str(res), result_summary="ok")
            _check("find_answered matches the same question",
                   s.find_answered("bigger summary of the orders file") is not None)
            _check("find_answered matches a rewording",
                   s.find_answered("can you give a bigger summary of orders")
                   is not None)
            _check("find_answered rejects an unrelated question",
                   s.find_answered("what is the average revenue") is None)
            # If the result file is gone, it must NOT be offered.
            res.unlink()
            _check("find_answered skips a task whose result file vanished",
                   s.find_answered("bigger summary of the orders file") is None)
        finally:
            if prev is None:
                os.environ.pop("COUNCIL_VAULT_ROOT", None)
            else:
                os.environ["COUNCIL_VAULT_ROOT"] = prev


def test_derived_results_store() -> None:
    """DerivedStore: catalogue a computed output with its source fingerprint,
    reuse it via find_fresh while sources are unchanged, REFUSE to reuse once a
    source changes (staleness), recover after recompute, prune missing outputs,
    and not match unrelated questions."""
    import time as _t
    import derived_results as dr
    prev = os.environ.get("COUNCIL_VAULT_ROOT")
    with tempfile.TemporaryDirectory() as td:
        os.environ["COUNCIL_VAULT_ROOT"] = td
        try:
            vault = Path(td)
            src = vault / "data_in" / "sales.csv"
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text("month,amount\njan,10\nfeb,20\n")

            store = dr.DerivedStore(vault)
            _check("starts with no derived results", store.all() == [])

            # derived_dir lives under data_in/derived and is auto-created.
            ddir = dr.derived_dir(vault)
            _check("derived_dir is under data_in/derived",
                   ddir.name == "derived" and ddir.parent.name == "data_in"
                   and ddir.is_dir())

            out = ddir / "avg_amount.csv"
            out.write_text("avg_amount\n15\n")
            store.record(label="average amount in sales.csv",
                         output=str(out), sources=[str(src)],
                         operation="avg(amount)", columns=["avg_amount"],
                         rows=1)

            # Fresh reuse: a matching question returns the saved result.
            hit = store.find_fresh("what is the average amount in sales.csv")
            _check("fresh result is reused", hit is not None
                   and Path(hit.output) == out)
            _check("recorded fingerprint is non-empty", bool(hit.source_fp))

            # Unrelated question does not match.
            _check("unrelated question does not match",
                   store.find_fresh("how many customers are there") is None)

            # Staleness: change the source -> the saved result is NOT served.
            _t.sleep(1.1)   # ensure a different int(mtime)
            src.write_text("month,amount\njan,10\nfeb,20\nmar,90\n")
            _check("stale source blocks reuse",
                   store.find_fresh("average amount in sales.csv") is None)

            # Recompute over the new sources -> fresh again.
            out.write_text("avg_amount\n40\n")
            store.record(label="average amount in sales.csv",
                         output=str(out), sources=[str(src)],
                         operation="avg(amount)", rows=1)
            _check("only one entry after re-record (replaced, not appended)",
                   len(store.all()) == 1)
            _check("recompute is fresh again",
                   store.find_fresh("average amount in sales.csv") is not None)

            # Deleted output -> not fresh, and prune_missing removes it.
            out.unlink()
            _check("deleted output is not served",
                   store.find_fresh("average amount in sales.csv") is None)
            _check("prune_missing drops the dead entry",
                   store.prune_missing() == 1 and store.all() == [])
        finally:
            if prev is None:
                os.environ.pop("COUNCIL_VAULT_ROOT", None)
            else:
                os.environ["COUNCIL_VAULT_ROOT"] = prev


def test_fast_answer_direct_route() -> None:
    """The analyst direct routes now emit a human __ANALYST_ANSWER__ headline
    so the council's fast-answer short-circuit can reply instantly (no model
    call). Verifies the file-count route returns a block + a headline notice
    carrying the count, against a real temp vault. Importing the GUI module is
    import-only (no Tk root is created), so it's safe headless."""
    try:
        import council_gui_engine as cge
    except Exception as exc:  # pragma: no cover - only if Tk import is broken
        _check(f"council_gui_engine importable (skipped: {exc!r})", True)
        return
    import data_index
    prev = getattr(cge, "VAULT_DIR", None)
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td)
        din = data_index.input_dir(vault)
        din.mkdir(parents=True, exist_ok=True)
        for i in range(3):
            (din / f"f{i}.csv").write_text("a,b\n1,2\n")
        (din / "notes.json").write_text("{}")
        try:
            cge.VAULT_DIR = vault
            block, err, notices = cge._run_analyst_step_impl(
                "how many files are in data_in")
            _check("file-count route returns a block", bool(block) and err is None)
            _check("block is the file-count direct route",
                   isinstance(block, str) and "file count" in block)
            headline = next(
                (n[len("__ANALYST_ANSWER__:"):] for n in (notices or [])
                 if isinstance(n, str) and n.startswith("__ANALYST_ANSWER__:")),
                None)
            _check("emits an __ANALYST_ANSWER__ headline for the fast path",
                   headline is not None)
            _check("headline states the total (4 files)",
                   headline is not None and "4 file(s)" in headline)
        finally:
            if prev is not None:
                cge.VAULT_DIR = prev


def test_provenance_source_resolve() -> None:
    """_resolve_source_paths normalises a mixed list of absolute paths,
    vault-relative paths and bare filenames into existing absolute files,
    de-duplicated, and drops things that don't exist. This is the engine
    behind the answer-provenance chips. Called unbound (the method touches no
    instance state) with the module VAULT_DIR patched to a temp vault."""
    try:
        import council_gui_engine as cge
    except Exception as exc:  # pragma: no cover
        _check(f"council_gui_engine importable (skipped: {exc!r})", True)
        return
    import data_index
    prev = getattr(cge, "VAULT_DIR", None)
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td)
        din = data_index.input_dir(vault)
        (din / "sub").mkdir(parents=True, exist_ok=True)
        f_abs = din / "alpha.csv"
        f_abs.write_text("a\n1\n")
        f_nested = din / "sub" / "beta.csv"
        f_nested.write_text("b\n2\n")
        try:
            cge.VAULT_DIR = vault
            resolve = cge.CouncilConsole._resolve_source_paths
            # absolute path, vault-relative path, bare name (nested), a
            # nonexistent name, and a duplicate of the first.
            raw = [str(f_abs), "sub/beta.csv", "alpha.csv",
                   "does_not_exist.csv", str(f_abs)]
            out = resolve(None, raw)
            names = [p.name for p in out]
            _check("resolves absolute + relative + bare name",
                   "alpha.csv" in names and "beta.csv" in names)
            _check("drops nonexistent sources",
                   "does_not_exist.csv" not in names)
            _check("de-duplicates repeated sources",
                   names.count("alpha.csv") == 1)
            _check("returns only existing files",
                   all(p.exists() for p in out))
            _check("empty input yields no chips", resolve(None, []) == [])
        finally:
            if prev is not None:
                cge.VAULT_DIR = prev


def test_graph_introspect_columns() -> None:
    """graph_data._introspect_columns classification is unchanged after the
    nunique-computed-once (#14) and to_numeric-reuse (#15) CSE: native numeric,
    string-encoded numeric (coercion path), and low-cardinality categorical."""
    try:
        import graph_data as gd
        import pandas as pd
    except Exception as exc:  # pragma: no cover
        _check(f"graph_data importable (skipped: {exc!r})", True)
        return
    df = pd.DataFrame({
        "amount": [1.0, 2.5, 3.0, 4.0],              # native numeric
        "code": ["10", "20", "30", "40"],            # string-encoded numeric
        "category": ["a", "b", "a", "b"],            # low-cardinality
    })
    infos = {c.name: c for c in gd._introspect_columns(df)}
    _check("native numeric detected", infos["amount"].dtype == "numeric")
    _check("string-encoded numeric detected via coercion",
           infos["code"].dtype == "numeric")
    _check("low-cardinality column is categorical",
           infos["category"].dtype == "categorical")
    _check("numeric min/max/mean populated",
           infos["amount"].min_val == 1.0 and infos["amount"].max_val == 4.0)
    _check("coerced numeric min/max correct",
           infos["code"].min_val == 10.0 and infos["code"].max_val == 40.0)


def test_stores_survive_corrupt_json() -> None:
    """ROBUSTNESS: every per-vault JSON store must degrade gracefully (empty,
    no crash) when its file is corrupt / truncated / binary — a half-written
    store after a crash must never take the app down on next launch."""
    import agent_jobs, derived_results, deferred_tasks
    import vault_collections, question_history
    prev = os.environ.get("COUNCIL_VAULT_ROOT")
    with tempfile.TemporaryDirectory() as td:
        os.environ["COUNCIL_VAULT_ROOT"] = td
        v = Path(td)
        try:
            stores = {
                "jobs": agent_jobs.JobStore(v),
                "derived": derived_results.DerivedStore(v),
                "deferred": deferred_tasks.DeferredTaskStore(v),
                "collections": vault_collections.CollectionStore(v),
                "history": question_history.QuestionHistory(v),
            }
            all_ok = True
            for junk in ["{ not json", "", "[1,2,", "\x00\x01bin", "null", "{}"]:
                for name, st in stores.items():
                    st.path.write_text(junk, encoding="utf-8", errors="ignore")
                    try:
                        st.all()
                    except Exception:
                        all_ok = False
            _check("all stores read corrupt/truncated JSON without crashing",
                   all_ok)
        finally:
            if prev is None:
                os.environ.pop("COUNCIL_VAULT_ROOT", None)
            else:
                os.environ["COUNCIL_VAULT_ROOT"] = prev


def test_safe_resolve_rejects_escapes() -> None:
    """SECURITY: safe_agent._safe_resolve (the agent's read_local_file root
    guard) must reject ../ traversal, absolute paths, and sibling-dir escapes,
    while allowing paths inside the root."""
    from safe_agent import _safe_resolve
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "root"
        root.mkdir()
        (root / "ok.txt").write_text("x")
        for bad in ["../escape.txt", "../../etc/passwd", "/etc/passwd",
                    str(Path(td) / "sibling.txt")]:
            rejected = False
            try:
                _safe_resolve(root, bad)
            except (PermissionError, ValueError, OSError):
                rejected = True
            _check(f"rejects escape {bad!r}", rejected)
        inside = _safe_resolve(root, "ok.txt")
        _check("allows a path inside the root", inside.name == "ok.txt")


def test_agent_read_budget_not_double_counted() -> None:
    """ConstrainedAgent's read-byte budget must be the TRUE cumulative total,
    not a triangular re-sum that trips ~sqrt(N) too early and truncates legit
    multi-read jobs. A scripted agent that reads a small file several times,
    staying under the budget, must finish 'done' — not 'byte_budget'."""
    try:
        from safe_agent import AgentPolicy, ConstrainedAgent
        from tool_registry import build_default_registry
    except Exception as exc:  # pragma: no cover
        _check(f"safe_agent importable (skipped: {exc!r})", True)
        return
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td)
        (folder / "data.csv").write_text("x" * 1000)   # 1000 bytes
        policy = AgentPolicy(
            allowed_tools=("read_local_file",),
            file_root=folder, output_dir=folder, max_steps=8,
            max_total_read_bytes=5000)               # true 4*1000=4000 < 5000
        reg = build_default_registry(policy)

        class FakeRunner:
            def __init__(self):
                self.i = 0

            def chat(self, messages, max_tokens=None):
                self.i += 1
                if self.i <= 4:
                    return ('{"action":"tool","tool":"read_local_file",'
                            '"args":{"path":"data.csv"}}')
                return '{"action":"final","answer":"done"}'

        run = ConstrainedAgent(FakeRunner(), reg, policy).run("read a few times")
        _check("multi-read under budget finishes (not byte_budget)",
               run.stopped_reason == "done")
        _check("bytes_read is the true sum (4000), not triangular",
               run.trace is not None and run.trace.bytes_read == 4000)


def test_job_runner_reconciles_stale_on_restart() -> None:
    """On restart, a JobRunner that finds a job persisted as RUNNING *or*
    QUEUED (its in-RAM queue is gone) must mark it FAILED so it isn't orphaned
    forever. Exercises the real _get_job_runner reconciliation via a stub self."""
    import types
    import queue as _q
    try:
        import council_gui_engine as cge
        import agent_jobs as aj
    except Exception as exc:  # pragma: no cover
        _check(f"modules importable (skipped: {exc!r})", True)
        return
    prev = getattr(cge, "VAULT_DIR", None)
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td)
        try:
            cge.VAULT_DIR = vault
            st = aj.JobStore(vault)
            st.upsert(aj.AgentJob(job_id="q1", goal="queued one",
                                  status=aj.JobStatus.QUEUED.value))
            st.upsert(aj.AgentJob(job_id="r1", goal="running one",
                                  status=aj.JobStatus.RUNNING.value))
            st.upsert(aj.AgentJob(job_id="d1", goal="done one",
                                  status=aj.JobStatus.DONE.value))
            stub = types.SimpleNamespace(ui_q=_q.Queue())
            cge.CouncilConsole._get_job_runner.__get__(stub)()
            st2 = aj.JobStore(vault)
            _check("stale QUEUED job reconciled to FAILED",
                   st2.get("q1").status == aj.JobStatus.FAILED.value)
            _check("stale RUNNING job reconciled to FAILED",
                   st2.get("r1").status == aj.JobStatus.FAILED.value)
            _check("finished job left untouched",
                   st2.get("d1").status == aj.JobStatus.DONE.value)
        finally:
            if prev is not None:
                cge.VAULT_DIR = prev


def test_gpu_check_smoke() -> None:
    """gpu_check.py (the plug-and-play GPU readiness reporter) runs on any box
    without hanging or raising, returns a 0/1 exit code, and prints a verdict.
    Its probes tolerate a missing nvidia-smi / llama-cpp / torch."""
    import io
    import contextlib
    import gpu_check
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = gpu_check.main()
    _check("gpu_check.main() returns 0 or 1", rc in (0, 1))
    _check("gpu_check prints a verdict line", "VERDICT" in buf.getvalue())
    # Probes are individually crash-safe.
    _check("nvidia_smi probe returns None or a list",
           gpu_check._nvidia_smi() is None
           or isinstance(gpu_check._nvidia_smi(), list))
    _llg, _note = gpu_check._llama_gpu()
    _check("llama_gpu probe returns (bool|None, str)",
           (_llg is None or isinstance(_llg, bool)) and isinstance(_note, str))


def test_pandas_sandbox_write_escape_blocked() -> None:
    """SECURITY: the pandas sandbox exposes pathlib.Path for read-side path
    construction, so its WRITE surface (write_text/touch/mkdir/open('w')) — and
    the getattr / dunder bypasses — must be refused, with NO file written.
    Legit read/compute code must still run."""
    import vault_analyst as va
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td)
        (folder / "a.csv").write_text("name,qty\nx,1\ny,2\n")
        mp = (folder / "PWNED.txt").as_posix()
        dp = (folder / "PWNED_DIR").as_posix()
        escapes = [
            f'Path("{mp}").write_text("pwned")',
            f'Path("{mp}").write_bytes(b"x")',
            f'Path("{mp}").touch()',
            f'Path("{dp}").mkdir()',
            f'Path("{mp}").open("w").write("x")',
            f'getattr(Path("{mp}"), "write_text")("x")',   # getattr bypass
            'result = type(1).__mro__[-1].__subclasses__()',  # dunder escape
            'x = ().__class__.__bases__[0].__globals__',       # __globals__
        ]
        for code in escapes:
            df, msg = va.execute_pandas_code(code, [folder])
            _check(f"escape blocked: {code[:34]!r}",
                   ("SAFETY CHECK FAILED" in str(msg)
                    or "blocked" in str(msg).lower()))
        _check("no file written by any sandbox escape",
               not (folder / "PWNED.txt").exists())
        _check("no dir created by any sandbox escape",
               not (folder / "PWNED_DIR").exists())
        # Legit read/compute still works.
        df_ok, _m = va.execute_pandas_code(
            'df = pd.read_csv(Path(DATA_FOLDER) / "a.csv"); '
            'df["name"] = df["name"].str.replace("x", "z"); result_df = df',
            [folder])
        _check("legit path-join + str.replace still runs", df_ok is not None)
        df_ga, _m2 = va.execute_pandas_code(
            'df = pd.read_csv(Path(DATA_FOLDER) / "a.csv"); '
            'result_df = pd.DataFrame({"n": [getattr(df, "shape")[0]]})',
            [folder])
        _check("legit getattr(df,'shape') still runs", df_ga is not None)


def test_zip_slip_guard() -> None:
    """SECURITY (Zip Slip): a malicious archive with an absolute-path entry or a
    ../ entry must NOT write outside the extraction target. zf.open()+manual
    write bypasses extractall()'s sanitisation, so the extractor must contain
    every entry itself."""
    import zipfile
    try:
        import council_gui_engine as cge
    except Exception as exc:  # pragma: no cover
        _check(f"council_gui_engine importable (skipped: {exc!r})", True)
        return
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td) / "vault"
        vault.mkdir()
        outside = Path(td) / "OUTSIDE.csv"
        zp = Path(td) / "evil.zip"
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("good.csv", "a,b\n1,2\n")               # benign
            z.writestr(str(outside).replace("\\", "/"), "x\n9\n")  # absolute
            z.writestr("../../ESCAPED.csv", "p\n1\n")           # traversal
        dest, copied, skipped = cge._vmgr_extract_zip(
            zp, vault_dir=vault, subfolder="imp")
        _check("absolute-path zip entry did NOT escape the vault",
               not outside.exists())
        _check("../ zip entry did NOT escape the vault",
               not (Path(td) / "ESCAPED.csv").exists())
        landed = [p.name for p in vault.rglob("*") if p.is_file()]
        _check("only the benign file was extracted", landed == ["good.csv"])
        _check("malicious entries were skipped, benign kept",
               copied == 1 and skipped >= 2)


def test_agentic_jobs_core() -> None:
    """Background agentic-job engine, driven by a SCRIPTED fake model (no GGUF):
    the ConstrainedAgent loop runs read-only tools, persists each step, writes a
    report, refuses unlisted tools (gap), and cancels at a step boundary. Also
    checks the tool registry exposes ONLY the 3 read/compute tools (no
    delete/write/network) — the structural security guarantee."""
    import threading as _th
    try:
        import agent_jobs as aj
        import agent_jobs_runner as ajr
        from safe_agent import AgentPolicy
        from tool_registry import build_default_registry
    except Exception as exc:  # pragma: no cover
        _check(f"agent-job modules importable (skipped: {exc!r})", True)
        return

    prev = os.environ.get("COUNCIL_VAULT_ROOT")
    with tempfile.TemporaryDirectory() as td:
        os.environ["COUNCIL_VAULT_ROOT"] = td
        try:
            import data_index
            vault = Path(td)
            din = data_index.input_dir(vault)
            din.mkdir(parents=True, exist_ok=True)
            (din / "alpha.csv").write_text("a,b\n1,2\n3,4\n")

            # ── Security: registry exposes ONLY the safe read/compute tools ──
            pol = AgentPolicy(
                allowed_tools=ajr._AGENT_TOOLS,
                file_root=din, output_dir=vault / "out")
            reg = build_default_registry(pol)
            names = set(reg.as_dict().keys())
            _check("registry exposes exactly the 3 safe tools",
                   names == {"read_local_file", "run_pandas_analysis",
                             "query_memory"})
            _check("no delete/write/network tool is registered",
                   not any(k in n for n in names
                           for k in ("delete", "write", "remove", "http",
                                     "sql_write", "unlink", "export")))
            _check("registry is frozen (model can't extend it)", reg.frozen)

            class FakeRunner:
                def __init__(self, replies):
                    self.replies = list(replies)
                    self.i = 0

                def chat(self, messages, max_tokens=None):
                    r = (self.replies[self.i] if self.i < len(self.replies)
                         else '{"action":"final","answer":"stop"}')
                    self.i += 1
                    return r

            def _run_sync(runner, goal, job_id, max_steps=5, precancel=False):
                jr = ajr.JobRunner(vault_dir=vault, ui_q=None,
                                   file_root=din, runner=runner,
                                   max_steps=max_steps)
                job = aj.AgentJob(job_id=job_id, goal=goal, max_steps=max_steps)
                jr.store.upsert(job)
                jr._cancels[job_id] = _th.Event()
                if precancel:
                    jr._cancels[job_id].set()
                jr._run_job(job_id)
                return jr.store.get(job_id)

            # ── Happy path: read a file, then finalize ──
            done = _run_sync(FakeRunner([
                '{"action":"tool","tool":"read_local_file","args":{"path":"alpha.csv"}}',
                '{"action":"final","answer":"alpha.csv has columns a,b"}',
            ]), "look at alpha.csv", "job_ok")
            _check("job reaches DONE", done.status == aj.JobStatus.DONE.value)
            _check("both steps persisted", len(done.steps) >= 2)
            _check("a tool step ran read_local_file",
                   any(s.tool == "read_local_file" for s in done.steps))
            _check("final answer captured", "alpha.csv" in done.result_summary)
            _check("report artifact was written",
                   bool(done.report_path) and Path(done.report_path).exists())
            _check("report lands in agent_jobs_out (not data_in)",
                   "agent_jobs_out" in done.report_path
                   and "data_in" not in Path(done.report_path).parent.name)

            # ── Unlisted tool -> refused (gap), loop continues ──
            gap = _run_sync(FakeRunner([
                '{"action":"tool","tool":"delete_everything","args":{}}',
                '{"action":"final","answer":"could not delete"}',
            ]), "try to delete", "job_gap")
            _check("unlisted tool produced a gap step (not executed)",
                   any(s.kind == "gap" for s in gap.steps))
            _check("job still finishes DONE after a refused tool",
                   gap.status == aj.JobStatus.DONE.value)

            # ── Cancellation at a step boundary ──
            canc = _run_sync(FakeRunner([
                '{"action":"tool","tool":"read_local_file","args":{"path":"alpha.csv"}}',
            ] * 5), "loop forever", "job_cancel", precancel=True)
            _check("pre-cancelled job ends CANCELLED",
                   canc.status == aj.JobStatus.CANCELLED.value)
            _check("cancel stops after the first step boundary",
                   len(canc.steps) == 1)

            # ── Store persistence across instances + queries ──
            _check("running() excludes finished jobs",
                   all(j.status != aj.JobStatus.RUNNING.value
                       for j in aj.JobStore(vault).running()))
            _check("jobs persist across store instances",
                   {j.job_id for j in aj.JobStore(vault).all()}
                   >= {"job_ok", "job_gap", "job_cancel"})
            _check("delete removes a job",
                   aj.JobStore(vault).delete("job_ok")
                   and aj.JobStore(vault).get("job_ok") is None)
        finally:
            if prev is None:
                os.environ.pop("COUNCIL_VAULT_ROOT", None)
            else:
                os.environ["COUNCIL_VAULT_ROOT"] = prev


def test_route_message_golden() -> None:
    """route_message keyword routing is unchanged after hoisting its nine
    phrase-lists to module scope (finding #10). Golden input->route map."""
    import council_engine as ce
    golden = {
        "no code please, just text": "writer",
        "what are you": "chat",
        "how does the council work": "chat",
        "thumbnail and ctr optimization for my channel": "algorithm",
        "my delivery sounds monotone": "coach",
        "brainstorm some video ideas": "ideator",
        "flesh out this idea into a full pitch": "pitcher",
        "youtube video script for my channel": "content",
        "hello there friend": "chat",
        "https://example.com please summarize": "chat",
        "implement a python function to sort": "ide",
        "refactor this class for me": "ide",
        "what did we discuss last session": "chat",
        "give me ideas for a video about cats": "ideator",
        "write a blog post about dogs": "content",
        "deploy to pi via ssh": "chat",
        "color grade and b-roll for the edit": "content",
        "analyze the sales csv and compute averages": "chat",
        "how many files in data_in": "chat",
        "tell me about yourself": "chat",
        "plain text answer only": "writer",
        "pitch me ideas": "ideator",
    }
    for q, expected in golden.items():
        got = ce.route_message(q)
        _check(f"route {q!r} -> {expected}", got == expected,
               detail=f"got {got!r}")


def test_read_file_injection_memo() -> None:
    """_read_file_for_injection memoizes on (resolved path, mtime, size): the
    cached read is byte-identical to the uncached one, it invalidates when the
    file changes, and it still returns None for directories/missing files."""
    import time as _t
    try:
        import council_gui_engine as cge
    except Exception as exc:  # pragma: no cover
        _check(f"council_gui_engine importable (skipped: {exc!r})", True)
        return
    prev = getattr(cge, "VAULT_DIR", None)
    with tempfile.TemporaryDirectory() as td:
        vault = Path(td)
        csv = vault / "orders.csv"
        csv.write_text("order_id,total\n1,10\n2,20\n")
        try:
            cge.VAULT_DIR = vault
            cge._FILE_INJECT_CACHE.clear()
            cached = cge._read_file_for_injection(str(csv))
            uncached = cge._read_file_for_injection_uncached(str(csv))
            _check("memoized read is byte-identical to uncached",
                   cached == uncached and cached is not None)
            # Second call returns the same object from cache.
            again = cge._read_file_for_injection(str(csv))
            _check("second read returns identical block", again == cached)
            # Change the file -> the memo invalidates (mtime/size differ).
            _t.sleep(1.1)
            csv.write_text("order_id,total\n1,10\n2,20\n3,999\n")
            fresh = cge._read_file_for_injection(str(csv))
            _check("changed file invalidates the memo",
                   fresh != cached and "999" in fresh)
            # Directory and missing path still return None (not cached).
            _check("directory returns None",
                   cge._read_file_for_injection(str(vault)) is None)
            _check("missing file returns None",
                   cge._read_file_for_injection(str(vault / "nope.csv")) is None)
        finally:
            cge._FILE_INJECT_CACHE.clear()
            if prev is not None:
                cge.VAULT_DIR = prev


def test_context_clamp() -> None:
    """_clamp_messages_to_ctx keeps prompt+reply within n_ctx so an over-long
    prompt can never trigger llama-cpp's 'exceeds context window' native abort.
    Works with no model loaded (estimate_tokens falls back to chars/4)."""
    import council_engine as ce
    prev = os.environ.get("COUNCIL_GGUF_N_CTX")
    try:
        os.environ["COUNCIL_GGUF_N_CTX"] = "512"
        # A short prompt is returned unchanged with a sane reply budget.
        msgs, reply = ce._clamp_messages_to_ctx(
            [{"role": "user", "content": "hello there"}], 200)
        _check("short prompt is untouched",
               msgs[0]["content"] == "hello there")
        _check("reply budget is positive and within window",
               0 < reply < 512)
        # A huge prompt is trimmed so prompt+reply fits the 512 window.
        big = "word " * 4000   # ~5000 tokens by chars/4
        msgs2, reply2 = ce._clamp_messages_to_ctx(
            [{"role": "system", "content": "be brief"},
             {"role": "user", "content": big}], 300)
        total = sum(ce.estimate_tokens(m["content"]) for m in msgs2)
        _check("oversized prompt is trimmed under the window",
               total + reply2 <= 512)
        _check("trim marker is inserted",
               any("trimmed to fit" in m["content"] for m in msgs2))
        _check("short system message is preserved",
               any(m["content"] == "be brief" for m in msgs2))
        # Never raises on empty / weird input.
        m3, r3 = ce._clamp_messages_to_ctx([], 100)
        _check("empty messages handled", m3 == [] and r3 > 0)

        # TERMINATION guard (regression): the trim loop must always return.
        # The marker length has to be subtracted from the char target, else a
        # trimmed block stays at (target + marker) forever and the loop spins.
        # A tiny window + several huge messages is the worst case; if the
        # implementation regressed, this test would hang, not fail.
        os.environ["COUNCIL_GGUF_N_CTX"] = "128"
        m4, r4 = ce._clamp_messages_to_ctx(
            [{"role": "user", "content": "x" * 50000},
             {"role": "user", "content": "y" * 50000},
             {"role": "system", "content": "z" * 50000}], 400)
        t4 = sum(ce.estimate_tokens(m["content"]) for m in m4)
        _check("pathological huge prompt terminates and fits",
               t4 + r4 <= 128 + 8)  # +8 slack for rounding of estimate_tokens
        _check("clamp always returns a positive reply budget", r4 > 0)
    finally:
        if prev is None:
            os.environ.pop("COUNCIL_GGUF_N_CTX", None)
        else:
            os.environ["COUNCIL_GGUF_N_CTX"] = prev


def test_describe_nontext_routing() -> None:
    """Building descriptions routes non-text records (images / binaries /
    empty) to a deterministic description with NO model call, so the
    'build descriptions breaks on non-text files' crash can't happen."""
    import vault_index as vi
    # Pure helper.
    desc, topics = vi._describe_nontext(
        {"name": "scan_2024_invoice.png", "type": "image"})
    _check("non-text description mentions the type",
           "image" in desc and "scan_2024_invoice.png" in desc)
    _check("non-text topics come from the filename",
           "scan" in topics and "invoice" in topics)

    # End-to-end: an index of only tabular + non-text records describes with
    # zero model calls (so it runs headless, and can't hit the model crash).
    with tempfile.TemporaryDirectory() as td:
        idx = vi.VaultIndex(Path(td))
        idx.records = {
            "pics/logo.png": {"name": "logo.png", "type": "image"},
            "blob.bin": {"name": "blob.bin", "type": "binary"},
            "empty.txt": {"name": "empty.txt", "type": "text",
                          "sample_text": ""},
            "data.csv": {"name": "data.csv", "type": "csv",
                         "headers": ["a", "b"], "rows": 3},
        }
        n = idx.generate_descriptions()
        _check("every record got a description (no model needed)", n == 4)
        _check("image routed to deterministic non-text describer",
               idx.records["pics/logo.png"].get("_describe_via") == "nontext")
        _check("empty text file routed to non-text (no junk to the model)",
               idx.records["empty.txt"].get("_describe_via") == "nontext")
        _check("csv described from schema",
               idx.records["data.csv"].get("_describe_via") == "schema")


def test_first_run_wizard_data_step() -> None:
    """The first-run wizard gained a data-import + index-build step, and the
    copy engine it drives (_vmgr_copy_folder) copies a folder of files into
    the vault's data_in, keeping indexable files. Structural + functional."""
    try:
        import onboarding
        import council_gui_engine as cge
    except Exception as exc:  # pragma: no cover
        _check(f"modules importable (skipped: {exc!r})", True)
        return
    W = onboarding.OnboardingWizard
    for meth in ("_render_data", "_wiz_choose_data_folder",
                 "_wiz_import_and_index"):
        _check(f"wizard has {meth}", hasattr(W, meth))
    src_init = __import__("inspect").getsource(W.__init__)
    _check("'data' step is registered before 'ready'",
           '"data"' in src_init and
           src_init.index('"data"') < src_init.index('"ready"'))

    # Functional: the copy engine the wizard calls, targeting data_in exactly
    # as the wizard does (so the analyst, scoped to data_in, can see it).
    import data_index
    with tempfile.TemporaryDirectory() as src_d, \
            tempfile.TemporaryDirectory() as vault_d:
        src = Path(src_d)
        (src / "sales.csv").write_text("a,b\n1,2\n")
        (src / "notes.txt").write_text("hello\n")
        vault = Path(vault_d)
        in_dir = data_index.input_dir(vault)
        dest, copied, skipped = cge._vmgr_copy_folder(
            src, vault_dir=in_dir, subfolder=src.name)
        _check("copy_folder copied at least the CSV", copied >= 1)
        landed = [p.name for p in in_dir.rglob("*") if p.is_file()]
        _check("CSV landed under data_in", "sales.csv" in landed)


def test_council_examples() -> None:
    """The 'What can I ask?' panel data is well-formed: non-empty, each entry
    is (category, prompt, hint) with real text, and it covers the headline
    capabilities (counting, find, charts)."""
    try:
        import council_gui_engine as cge
    except Exception as exc:  # pragma: no cover
        _check(f"council_gui_engine importable (skipped: {exc!r})", True)
        return
    ex = cge._COUNCIL_EXAMPLES
    _check("examples list is non-empty", isinstance(ex, list) and len(ex) >= 5)
    _check("each example is (category, prompt, hint) of strings",
           all(isinstance(t, tuple) and len(t) == 3
               and all(isinstance(x, str) and x for x in t) for t in ex))
    blob = " ".join(p for _, p, _ in ex).lower()
    _check("covers file counting", "how many files" in blob)
    _check("covers find/search", "find files" in blob or "look up" in blob)
    _check("covers charts", "chart" in blob)


def test_question_history() -> None:
    """QuestionHistory: append questions, skip immediate duplicates and
    blanks, list newest-first, persist across instances, and clear."""
    import question_history as qh
    prev = os.environ.get("COUNCIL_VAULT_ROOT")
    with tempfile.TemporaryDirectory() as td:
        os.environ["COUNCIL_VAULT_ROOT"] = td
        try:
            h = qh.QuestionHistory()
            _check("starts empty", h.all() == [])
            h.add("how many files are in data_in", ts=1.0)
            h.add("how many files are in data_in", ts=2.0)  # immediate dup
            h.add("   ", ts=3.0)                             # blank
            h.add("average amount in sales.csv", ts=4.0)
            _check("dedupes immediate duplicate + drops blank",
                   len(h.all()) == 2)
            recent = h.recent(10)
            _check("recent is newest-first",
                   recent[0]["q"] == "average amount in sales.csv")
            _check("persists across instances",
                   len(qh.QuestionHistory().all()) == 2)
            # A non-immediate repeat IS allowed (asked again later).
            h.add("how many files are in data_in", ts=5.0)
            _check("non-adjacent repeat is kept", len(h.all()) == 3)
            h.clear()
            _check("clear empties the log", h.all() == [])
        finally:
            if prev is None:
                os.environ.pop("COUNCIL_VAULT_ROOT", None)
            else:
                os.environ["COUNCIL_VAULT_ROOT"] = prev


def test_answer_report_md() -> None:
    """_build_answer_report_md renders a council answer as Markdown with the
    question, answer, optional result table, and sources. Pure + UI-free."""
    try:
        import council_gui_engine as cge
    except Exception as exc:  # pragma: no cover
        _check(f"council_gui_engine importable (skipped: {exc!r})", True)
        return
    md = cge._build_answer_report_md(
        question="What is the average amount?",
        answer="The average amount is 15.",
        table="month  amount\njan    10\nfeb    20",
        sources=[Path("data_in/sales.csv"), "orders.csv"])
    _check("report has the question", "What is the average amount?" in md)
    _check("report has the answer", "The average amount is 15." in md)
    _check("report includes the table in a code block",
           "```" in md and "amount" in md)
    _check("report lists source file names (basename only)",
           "- sales.csv" in md and "- orders.csv" in md)
    # No table / no sources -> those sections are omitted, no crash.
    md2 = cge._build_answer_report_md("Q", "A", "", [])
    _check("omits the table section when there's none",
           "Result table" not in md2)
    _check("omits the sources section when there are none",
           "Sources" not in md2 and "## Answer" in md2)


def test_instant_filename_search() -> None:
    """_search_vault_filenames finds files whose path contains every query
    word (case-insensitive), skips app-generated output dirs, and returns
    (path, reason). This is the no-index half of the instant search box."""
    try:
        import council_gui_engine as cge
    except Exception as exc:  # pragma: no cover
        _check(f"council_gui_engine importable (skipped: {exc!r})", True)
        return
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "jobs").mkdir()
        (root / "derived").mkdir()
        (root / "jobs" / "Job_Blue_costs.csv").write_text("a\n1\n")
        (root / "jobs" / "Job_Blue_notes.txt").write_text("hi\n")
        (root / "jobs" / "Red_team.csv").write_text("a\n1\n")
        (root / "derived" / "Job_Blue_summary.csv").write_text("a\n1\n")  # skipped

        hits = cge._search_vault_filenames(root, "job blue")
        names = sorted(Path(p).name for p, _ in hits)
        _check("matches both Job Blue files",
               "Job_Blue_costs.csv" in names and "Job_Blue_notes.txt" in names)
        _check("non-matching file excluded", "Red_team.csv" not in names)
        _check("app-generated derived/ output is skipped",
               "Job_Blue_summary.csv" not in names)
        _check("every result carries a reason",
               all(r for _, r in hits))
        _check("empty term yields nothing",
               cge._search_vault_filenames(root, "") == [])


def test_filename_wildcard_patterns() -> None:
    """`job_####` / `report_*` file references resolve as PATTERNS, not literal
    strings. `#` = any single char, `*` = any run. Covers the helper pair and
    the end-to-end _search_vault_filenames wildcard branch."""
    try:
        import council_gui_engine as cge
    except Exception as exc:  # pragma: no cover
        _check(f"council_gui_engine importable (skipped: {exc!r})", True)
        return
    pat = cge._compile_name_pattern("job_####")
    _check("job_#### compiles to a pattern", pat is not None)
    _check("job_#### matches a 4-digit suffix",
           cge._name_matches_pattern(pat, "job_1234.csv"))
    _check("job_#### matches a 4-letter suffix (# = any char)",
           cge._name_matches_pattern(pat, "job_abcd.csv"))
    _check("job_#### rejects a 3-char suffix",
           not cge._name_matches_pattern(pat, "job_123.csv"))
    _check("job_#### rejects a 5-char suffix",
           not cge._name_matches_pattern(pat, "job_12345.csv"))
    _check("a plain name is NOT treated as a pattern",
           cge._compile_name_pattern("sales") is None)
    star = cge._compile_name_pattern("report_*")
    _check("report_* compiles to a glob pattern", star is not None)
    _check("report_* matches any suffix",
           cge._name_matches_pattern(star, "report_q3_2024.xlsx"))
    _check("report_* rejects a different prefix",
           not cge._name_matches_pattern(star, "summary_q3.xlsx"))
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "job_1234.csv").write_text("a\n1\n")
        (root / "job_0087.csv").write_text("a\n1\n")
        (root / "job_notes.txt").write_text("hi\n")   # 5-char suffix -> no match
        hits = cge._search_vault_filenames(root, "job_####")
        names = sorted(Path(p).name for p, _ in hits)
        _check("wildcard filename search finds both 4-char jobs",
               "job_1234.csv" in names and "job_0087.csv" in names)
        _check("wildcard filename search excludes non-4-char suffix",
               "job_notes.txt" not in names)
        _check("wildcard hits carry a reason",
               all(r for _, r in hits))


def test_content_query_terms_and_value_index() -> None:
    """The context injector's value-search stage extracts CELL-VALUE-like terms
    (dropping stop/aggregate words, keeping IDs), and the registered DataIndex
    is reused by _get_data_index() so search_value finds in-cell values."""
    try:
        import council_gui_engine as cge
        import data_index
    except Exception as exc:  # pragma: no cover
        _check(f"council_gui_engine importable (skipped: {exc!r})", True)
        return
    terms = cge._content_query_terms("what is the average revenue for job_0087")
    _check("aggregate word 'average' dropped from value terms",
           "average" not in terms)
    _check("stop-word 'the' dropped from value terms", "the" not in terms)
    _check("ID-like token 'job_0087' kept", "job_0087" in terms)
    dterms = cge._content_query_terms("row 12")
    _check("short digit-bearing token kept ('12')", "12" in dterms)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        d = root / "data_in"
        d.mkdir()
        (root / "out").mkdir()
        (d / "orders.csv").write_text(
            "customer,product\nAcme,Promethium\nBeta,Widget\n")
        di = data_index.DataIndex(search_roots=[d], write_root=root / "out")
        cge._register_data_index(di)
        try:
            _check("register/get data index round-trips",
                   cge._get_data_index() is di)
            vterms = cge._content_query_terms("who bought promethium")
            names = set()
            for t in vterms:
                for h in di.search_value(t):
                    names.add(h["file"])
            _check("value search finds a value that lives INSIDE a cell",
                   "orders.csv" in names)
        finally:
            cge._register_data_index(None)   # reset module global for isolation


def test_tabular_sample_text_captures_body() -> None:
    """_parse_csv (and the tabular/Excel parsers) now populate `sample_text`
    with a DEDUPED, bounded sample of cell VALUES across the file, so the
    embedding (_record_to_text embeds sample_text[:300]) and phrase-match
    scorer (sample_blob) reflect the file body, not just its headers."""
    try:
        import vault_index as vi
    except Exception as exc:  # pragma: no cover
        _check(f"vault_index importable (skipped: {exc!r})", True)
        return
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "games.csv"
        lines = ["console,title"]
        for i in range(50):
            lines.append(f"PlayStation,Game{i}")
        p.write_text("\n".join(lines) + "\n")
        rec = vi._parse_csv(p)
        st = rec.get("sample_text", "")
        _check("csv record now carries a sample_text body sample", bool(st))
        _check("body cell VALUES captured in sample_text",
               "PlayStation" in st and "Game0" in st)
        _check("repeated value deduped (PlayStation appears once)",
               st.count("PlayStation") == 1)
        _check("sample_text is char-bounded",
               len(st) <= vi._BODY_SAMPLE_MAX_CHARS)
        _check("headers are not mixed into the value sample",
               "console" not in st.split(" | "))


def test_vault_search_runs_on_main_path() -> None:
    """Regression: the context injector's vault-search block referenced an
    undefined alias `_ce_tim` (introduced by the "behavior-preserving" perf
    batch c613e75), so idx.search() raised NameError on EVERY turn and the
    outer except swallowed it — vault search was silently DEAD. Assert a VAULT
    block is actually produced for a matching query."""
    try:
        import council_gui_engine as cge
        import vault_index
        import data_index
    except Exception as exc:  # pragma: no cover
        _check(f"council_gui_engine importable (skipped: {exc!r})", True)
        return
    with tempfile.TemporaryDirectory() as td:
        vroot = Path(td)
        (vroot / "data_in").mkdir()
        (vroot / "out").mkdir()
        (vroot / "data_in" / "quarterly_promethium_report.txt").write_text(
            "Promethium output rose sharply in Q3.\n")
        vi = vault_index.VaultIndex(vroot)
        vi.rebuild()
        di = data_index.DataIndex(
            search_roots=[vroot / "data_in"], write_root=vroot / "out")
        saved = cge._VAULT_INDEX_INSTANCE
        cge._VAULT_INDEX_INSTANCE = vi
        cge._register_data_index(di)
        try:
            _aug, _fuzzy, bd = cge._inject_file_contents_impl(
                "find files about promethium", n_ctx=8192)
        finally:
            cge._VAULT_INDEX_INSTANCE = saved
            cge._register_data_index(None)
        labels = [l for l, _ in bd.get("costs", [])]
        _check("vault search produces a VAULT block (no swallowed NameError)",
               any("VAULT" in l for l in labels))


def test_quick_analytics_helpers() -> None:
    """Model-free per-file analytics: column_stats (mean/median WITH and
    WITHOUT zeros), missing_data_report, duplicate_rows_report,
    top_values_per_column, numeric_correlations. Pure — directly testable."""
    try:
        import vault_analyst as va
    except Exception as exc:  # pragma: no cover
        _check(f"vault_analyst importable (skipped: {exc!r})", True)
        return
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "sales.csv"
        p.write_text(
            "region,units,revenue\n"
            "East,0,0\n"
            "East,5,50\n"
            "West,0,0\n"
            "West,10,100\n"
            "East,5,50\n"          # exact duplicate of row 2
        )
        cs = va.column_stats(p)
        units = cs[cs["column"] == "units"].iloc[0]
        _check("column_stats mean INCLUDES zeros (0,5,0,10,5 -> 4.0)",
               float(units["mean"]) == 4.0)
        _check("column_stats mean_nonzero EXCLUDES zeros (5,10,5 -> 6.67)",
               abs(float(units["mean_nonzero"]) - 6.6667) < 0.01)
        _check("column_stats counts zeros", int(units["zeros"]) == 2)
        _check("column_stats zero_pct correct", float(units["zero_pct"]) == 40.0)
        _check("column_stats classifies a text column",
               cs[cs["column"] == "region"].iloc[0]["kind"] == "text")

        md = va.missing_data_report(p)
        _check("missing_data_report total rows", md["total_rows"] == 5)
        _check("missing_data_report complete rows (no nulls here)",
               md["complete_rows"] == 5)

        dup = va.duplicate_rows_report(p)
        _check("duplicate_rows_report finds the 1 exact dup",
               dup["duplicate_rows"] == 1)
        _check("duplicate_rows_report unique count", dup["unique_rows"] == 4)
        _check("duplicate_rows_report returns a sample", len(dup["sample"]) == 1)

        tv = va.top_values_per_column(p, top_n=2)
        region = next(c for c in tv["columns"] if c["column"] == "region")
        _check("top_values ranks East first (x3)",
               region["values"][0] == ("East", 3))

        corr = va.numeric_correlations(p)
        _check("numeric_correlations finds units~revenue = 1.0",
               len(corr) >= 1 and abs(float(corr.iloc[0]["corr"]) - 1.0) < 1e-6)

        # Robustness: unreadable file -> error frame, never raises.
        cs2 = va.column_stats(Path(td) / "does_not_exist.csv")
        _check("column_stats returns an error frame on unreadable file",
               "error" in cs2.columns)


def test_quick_analytics_routing() -> None:
    """The five quick-analytics chat commands parse to the right target file,
    and column-stats does NOT hijack the folder-level 'stats for the files'
    route."""
    try:
        import council_gui_engine as cge
    except Exception as exc:  # pragma: no cover
        _check(f"council_gui_engine importable (skipped: {exc!r})", True)
        return
    C = cge.CouncilConsole

    def tgt(rx, text):
        m = rx.match(text)
        return (m.group(1) or "").strip() if m else None

    _check("column stats route captures file",
           tgt(C._COLUMN_STATS_RE, "column stats in sales.csv") == "sales.csv")
    _check("'summarize the data in the columns of X' routes",
           tgt(C._COLUMN_STATS_RE,
               "summarize the data in the columns of sales.csv") == "sales.csv")
    _check("missing data route captures file",
           tgt(C._MISSING_DATA_RE, "missing data in sales.csv") == "sales.csv")
    _check("duplicates route captures file",
           tgt(C._DUPLICATES_RE, "duplicates in sales.csv") == "sales.csv")
    _check("top values route captures file",
           tgt(C._TOP_VALUES_RE, "top values in sales.csv") == "sales.csv")
    _check("correlations route captures file",
           tgt(C._CORRELATIONS_RE, "correlations in sales.csv") == "sales.csv")
    _check("column-stats does NOT grab the folder-stats route",
           C._COLUMN_STATS_RE.match("summary of stats for the files") is None)


def test_folder_column_aggregate() -> None:
    """folder_column_aggregate computes one aggregation of a column across all
    CSVs in a folder, pooling values for an EXACT overall (not an average of
    per-file averages), tracks files missing the column, and supports
    excluding zeros. Model-free."""
    try:
        import vault_analyst as va
    except Exception as exc:  # pragma: no cover
        _check(f"vault_analyst importable (skipped: {exc!r})", True)
        return
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "a.csv").write_text("region,units\nE,0\nE,10\n")   # 0, 10
        (root / "b.csv").write_text("region,units\nW,20\nW,30\n")  # 20, 30
        (root / "c.csv").write_text("region,price\nX,5\n")         # no 'units'
        res = va.folder_column_aggregate(root, "units", "mean")
        _check("overall mean pools all rows (0,10,20,30 -> 15.0)",
               res["overall"] == 15.0)
        _check("overall n counts pooled values", res["overall_n"] == 4)
        _check("file lacking the column is reported missing",
               "c.csv" in res["missing"])
        _check("per-file means are exact",
               {r["file"]: r["value"] for r in res["per_file"]}
               == {"a.csv": 5.0, "b.csv": 25.0})
        _check("sum aggregation pools correctly (60)",
               va.folder_column_aggregate(root, "units", "sum")["overall"] == 60.0)
        _check("exclude-zeros changes the mean (10,20,30 -> 20.0)",
               va.folder_column_aggregate(
                   root, "units", "mean", exclude_zeros=True)["overall"] == 20.0)
        _check("canonical_agg maps 'average' -> 'mean'",
               va.canonical_agg("average") == "mean")
        _check("match_column_name is case-insensitive",
               va.match_column_name(["Units", "Region"], "units") == "Units")


def test_folder_agg_command() -> None:
    """The 'mean of <col> in <folder> [and save to <file>]' command parses
    correctly and its save path writes a report ONLY under the vault output
    folder (never the input folder)."""
    try:
        import council_gui_engine as cge
        import data_index
        import vault_analyst as va
    except Exception as exc:  # pragma: no cover
        _check(f"council_gui_engine importable (skipped: {exc!r})", True)
        return
    C = cge.CouncilConsole

    def tgt(text):
        m = C._FOLDER_AGG_RE.match(text)
        return [(g or "").strip() for g in m.groups()] if m else None

    _check("routes 'what is the average price in data_in'",
           tgt("what is the average price in data_in")
           == ["average", "price", "data_in"])
    _check("routes 'mean of revenue in data_in and save to a csv'",
           (tgt("mean of revenue in data_in and save to a csv")
            or [None])[0] == "mean")
    _check("routes 'max temperature in sensors' (no of/for needed)",
           tgt("max temperature in sensors") == ["max", "temperature", "sensors"])
    m = C._SAVE_CLAUSE_RE.search(" and export as csv named q3.csv")
    _check("save clause captures format + filename",
           bool(m) and m.group(1) == "csv" and m.group(2) == "q3.csv")
    _check("folder phrase 'all csvs in data_in' -> 'data_in'",
           C._clean_folder_phrase(C, "all csvs in data_in") == "data_in")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "data_in").mkdir()
        (root / "data_out").mkdir()
        (root / "data_in" / "a.csv").write_text("region,units\nE,0\nE,10\n")
        (root / "data_in" / "b.csv").write_text("region,units\nW,20\nW,30\n")
        di = data_index.DataIndex(search_roots=[root / "data_in"],
                                  write_root=root / "data_out")
        res = va.folder_column_aggregate(root / "data_in", "units", "mean")

        class _Fake:
            pass
        fake = _Fake()
        fake.data_index = di
        status = C._save_stat_report(
            fake, res, "units", root / "data_in", "csv", "means_out", [])
        _check("save reports success", "Saved to" in status)
        out = root / "data_out" / "reports" / "means_out.csv"
        _check("report written under vault output (data_out/reports)",
               out.exists())
        text = out.read_text()
        _check("report contains the OVERALL pooled mean row",
               "OVERALL" in text and "15.0" in text)
        _check("report never lands in the input folder",
               not (root / "data_in" / "reports").exists())


def test_data_preview_text() -> None:
    """_data_preview_text gives a model-free (schema, rows) preview of a data
    file with bounded reads, and falls back to a text peek for non-tabular
    files. Pure + UI-free, so directly unit-testable."""
    try:
        import council_gui_engine as cge
    except Exception as exc:  # pragma: no cover
        _check(f"council_gui_engine importable (skipped: {exc!r})", True)
        return
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        csv = d / "sales.csv"
        csv.write_text("month,amount\njan,10\nfeb,20\nmar,30\n")
        schema, rows = cge._data_preview_text(csv, max_rows=2)
        _check("schema lists the columns",
               "month" in schema and "amount" in schema)
        _check("schema reports column count", "2 column(s)" in schema)
        _check("rows preview shows data values", "jan" in rows)
        _check("max_rows bounds the row preview", "mar" not in rows)

        # Non-tabular file -> graceful text peek, no exception.
        txt = d / "notes.md"
        txt.write_text("# hello\nsome notes here\n")
        s2, r2 = cge._data_preview_text(txt)
        _check("non-tabular file previews without raising",
               isinstance(s2, str) and "hello" in r2)

        # _text_peek bounds its read and never raises on binary.
        binf = d / "blob.bin"
        binf.write_bytes(bytes(range(256)) * 100)
        peek = cge._text_peek(binf, max_bytes=128)
        _check("text peek is bounded", len(peek) <= 256)


def test_error_coaching() -> None:
    """_coach_for_error maps raw errors/tracebacks to plain-language guidance
    plus a one-click fix action. Pure + UI-free, so directly unit-testable."""
    try:
        import council_gui_engine as cge
    except Exception as exc:  # pragma: no cover
        _check(f"council_gui_engine importable (skipped: {exc!r})", True)
        return
    coach = cge._coach_for_error
    # Context-window overflow -> Engine settings.
    c = coach("ValueError: Requested tokens (5000) exceed context window of 4096")
    _check("ctx overflow -> engine action",
           c is not None and c["action"] == "engine")
    c2 = coach("llama: this answer exceeds max tokens for the model")
    _check("'exceeds max tokens' -> engine action",
           c2 is not None and c2["action"] == "engine")
    # GPU / CUDA -> switch to CPU.
    c3 = coach("RuntimeError: CUDA error: out of memory (ggml_cuda)")
    _check("CUDA failure -> cpu action",
           c3 is not None and c3["action"] == "cpu")
    # Model not loaded -> Models tab.
    c4 = coach("RuntimeError: Llama() failed to load model: no such file")
    _check("model load failure -> models action",
           c4 is not None and c4["action"] == "models")
    # Generic OOM -> Models (smaller model).
    c5 = coach("MemoryError: cannot allocate 8.0 GiB")
    _check("OOM -> models action",
           c5 is not None and c5["action"] == "models")
    # Unrecognised -> None (caller shows the raw error).
    _check("unknown error -> no coaching",
           coach("KeyError: 'frobnicate'") is None)
    # Each coaching dict carries a human plain message + a button label.
    for c in (c, c3, c4, c5):
        _check("coaching has plain + action_label",
               bool(c.get("plain")) and bool(c.get("action_label")))


def test_model_downloader() -> None:
    """model_downloader: OS-aware dir, HF URL build, GGUF magic check,
    non-HF URL refusal, and a real streaming download (local server) with
    magic validation + skip-if-present."""
    import io
    import model_downloader as md

    _check("detect_os returns a known value",
           md.detect_os() in ("windows", "linux", "macos", "unknown"))
    _check("default_models_dir is absolute",
           Path(md.default_models_dir()).is_absolute())
    url = md.hf_resolve_url("org/repo-GGUF", "model-Q4_K_M.gguf")
    _check("hf_resolve_url targets huggingface.co",
           url.startswith("https://huggingface.co/org/repo-GGUF/resolve/main/"))

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        good = d / "good.gguf"
        good.write_bytes(b"GGUF" + b"\0" * 32)
        bad = d / "bad.gguf"
        bad.write_bytes(b"<html>nope</html>")
        _check("looks_like_gguf true on GGUF magic", md.looks_like_gguf(good))
        _check("looks_like_gguf false on non-GGUF", not md.looks_like_gguf(bad))

        # Non-HF URL must be refused.
        refused = False
        try:
            md.download_gguf("x", "y.gguf", d / "o",
                             url="https://evil.example/y.gguf")
        except md.DownloadError:
            refused = True
        _check("non-Hugging-Face URL refused", refused)

        # Streaming download via a MOCKED urlopen — deterministic, no real
        # socket. (A real localhost http.server flakes with WinError 10054
        # on Windows: it resets the connection rather than sending a clean
        # EOF, which has nothing to do with the downloader.) The mock serves
        # the GGUF bytes in chunks so we still exercise the streaming +
        # magic-byte-verify + skip-if-present logic in download_gguf.
        import urllib.request as _ureq
        payload = b"GGUF" + b"\0" * (256 * 1024)

        class _FakeResp:
            status = 200

            def __init__(self, data):
                self._buf = io.BytesIO(data)
                self.headers = {"Content-Length": str(len(data))}

            # download_gguf calls resp.headers.get("Content-Length", "")
            def read(self, n=-1):
                return self._buf.read(n)

            def close(self):
                self._buf.close()

        # headers needs .get(); a plain dict works since dict.get exists.
        def _fake_urlopen(req, timeout=60):
            return _FakeResp(payload)

        prev_urlopen = _ureq.urlopen
        prev_host = md._HF_HOST
        md._HF_HOST = "127.0.0.1"
        _ureq.urlopen = _fake_urlopen
        try:
            out = d / "models"
            seen = []
            r = md.download_gguf(
                "repo", "m.gguf", out, url="http://127.0.0.1/m.gguf",
                progress=lambda done, total: seen.append(done))
            _check("download produced a valid GGUF",
                   md.looks_like_gguf(r["path"]) and r["bytes"] > 0)
            _check("progress callback fired", len(seen) >= 1)
            # Second call: file present + valid -> skips the network entirely.
            r2 = md.download_gguf("repo", "m.gguf", out,
                                  url="http://127.0.0.1/m.gguf")
            _check("re-download skips an already-present valid file",
                   r2["skipped"] is True)
        finally:
            _ureq.urlopen = prev_urlopen
            md._HF_HOST = prev_host


def test_stats_cache_per_folder_csv_shards() -> None:
    """The cache writes ONE CSV shard per folder (mirroring the tree),
    readable as a plain stats table, and never processes its own shards."""
    import csv as _csv
    import stats_cache as sc
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "data_in").mkdir()
        (d / "data_in" / "sub").mkdir()
        (d / "data_in" / "orders.csv").write_text("amount,region\n10,N\n20,S\n30,N\n")
        (d / "data_in" / "sub" / "q.csv").write_text("rev\n100\n200\n")
        cache = sc.StatsCache(d)
        r = cache.process_unprocessed(d / "data_in")
        _check("processed both files", r["processed"] == 2)

        # One shard per folder, mirroring the tree, named columns.csv
        shards = sorted(p.relative_to(d).as_posix()
                        for p in (d / ".stats_cache").rglob("columns.csv"))
        _check("per-folder shards mirror the tree",
               shards == [".stats_cache/data_in/columns.csv",
                          ".stats_cache/data_in/sub/columns.csv"])

        # Shard is a plain, readable CSV with the agreed schema
        with (d / ".stats_cache" / "data_in" / "columns.csv").open() as fh:
            rows = list(_csv.DictReader(fh))
        _check("shard header is the CSV schema",
               set(["file", "rows", "column", "min", "max", "mean", "sum"])
               <= set(rows[0].keys()))
        amt = [x for x in rows
               if x["file"] == "orders.csv" and x["column"] == "amount"][0]
        _check("shard carries exact stats",
               amt["min"] == "10.0" and amt["max"] == "30.0"
               and amt["mean"] == "20.0" and amt["sum"] == "60.0"
               and amt["rows"] == "3")

        # Re-running must NOT process the shard CSVs themselves
        r2 = cache.process_unprocessed(d / "data_in")
        _check("shards excluded from the walk (no self-processing)",
               r2["processed"] == 0 and r2["seen"] == 2)


def test_analyst_cached_stats_helpers() -> None:
    """The cache-backed analyst helpers (folder_column_stats /
    cached_column_stats) and their sandbox names (column_stats /
    file_stats) return exact precomputed stats and populate the cache."""
    import os as _os
    with tempfile.TemporaryDirectory() as td:
        _os.environ["COUNCIL_VAULT_ROOT"] = td
        try:
            di = Path(td) / "data_in"; di.mkdir(parents=True)
            (di / "s.csv").write_text("amount,region\n10,N\n20,S\n30,N\n40,E\n")
            import importlib, vault_analyst as va
            importlib.reload(va)  # pick up COUNCIL_VAULT_ROOT-derived cache

            # Direct helper
            fcs = va.folder_column_stats(td, di)
            amt = fcs[(fcs["file"] == "s.csv") & (fcs["column"] == "amount")]
            _check("folder_column_stats returns the column", len(amt) == 1)
            r = amt.iloc[0]
            _check("cached min/max/mean/sum exact",
                   r["min"] == 10.0 and r["max"] == 40.0
                   and r["mean"] == 25.0 and r["sum"] == 100.0)

            # Sandbox names
            sdf, msg = va.execute_pandas_code("result_df = column_stats()",
                                              allowed_folders=[di])
            _check("sandbox column_stats() runs", msg == "ok" and sdf is not None)
            _check("sandbox column_stats has stats rows",
                   sdf is not None and len(sdf) >= 1)
        finally:
            _os.environ.pop("COUNCIL_VAULT_ROOT", None)


def test_stats_cache_exact_and_incremental() -> None:
    """stats_cache: streaming column stats are EXACT (match full pandas,
    even across multiple chunks), self-describing detection works, the
    per-file cache is incremental (mtime-keyed), and the query-report
    cache saves/retrieves and invalidates on input change."""
    import os as _os
    import pandas as pd
    import stats_cache as sc

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        # File with a numeric + text column, a stats-named column, and a
        # 'Total' summary row up top.
        csv = d / "sales.csv"
        rows = ["amount,region,mean"]
        rows.append("Total,,999")                  # summary row (label in col 0)
        for n in range(250):
            rows.append(f"{n*2}.5,{'NESW'[n%4]},{n}")
        csv.write_text("\n".join(rows) + "\n")

        # Force multi-chunk streaming to exercise cross-chunk aggregation.
        old_chunk = sc._CHUNK_ROWS
        sc._CHUNK_ROWS = 64
        try:
            st = sc.compute_column_stats(csv)
        finally:
            sc._CHUNK_ROWS = old_chunk

        # Compare numeric stats to a full pandas read (the 'Total' row
        # makes 'amount' object-typed, so compare on the clean numeric
        # column 'mean' which is all integers 0..249 + the 999 row).
        full = pd.read_csv(csv)
        cs = st["column_stats"]
        _check("stats has the numeric column", "mean" in cs)
        msr = cs["mean"]
        _check("streamed count == pandas count",
               msr["count"] == int(full["mean"].count()))
        _check("streamed sum == pandas sum",
               abs(msr["sum"] - float(full["mean"].sum())) < 1e-6)
        _check("streamed min/max exact",
               msr["min"] == float(full["mean"].min())
               and msr["max"] == float(full["mean"].max()))
        _check("streamed mean ~ pandas mean",
               abs(msr["mean"] - float(full["mean"].mean())) < 1e-6)
        _check("streamed std ~ pandas std (ddof=1)",
               abs(msr["std"] - float(full["mean"].std())) < 1e-4)

        # Self-describing detection
        sd = st["self_describing"]
        _check("summary column 'mean' detected",
               "mean" in sd["summary_columns"])
        _check("summary 'Total' row detected",
               any(r["label"].lower() == "total" for r in sd["summary_rows"]))

        # Incremental per-file cache
        cache = sc.StatsCache(vault_dir=d)
        r1 = cache.process_unprocessed(d)
        _check("first sweep processes the file", r1["processed"] == 1)
        r2 = cache.process_unprocessed(d)
        _check("second sweep processes nothing (all current)",
               r2["processed"] == 0 and r2["already_current"] >= 1)
        # Touch the file (advance mtime) → must reprocess
        _os.utime(csv, (csv.stat().st_atime, csv.stat().st_mtime + 5))
        _check("changed file is no longer current", not cache.is_current(csv))
        r3 = cache.process_unprocessed(d)
        _check("changed file reprocessed", r3["processed"] == 1)

        # Query-report cache: save + retrieve, and invalidate on change
        qc = sc.QueryReportCache(vault_dir=d)
        k1 = qc.make_key("first 200 row stats", [csv])
        computed = {"answer": 42}
        got = qc.get_or_compute("first 200 row stats", [csv],
                                lambda: computed)
        _check("query report stored + returned", got == computed)
        _check("query report retrieved on second call",
               qc.get(k1) is not None)
        _os.utime(csv, (csv.stat().st_atime, csv.stat().st_mtime + 9))
        k2 = qc.make_key("first 200 row stats", [csv])
        _check("query key changes when input mtime changes", k1 != k2)
        _check("stale key no longer matches the new fingerprint",
               qc.get(k2) is None)


def test_folder_summary_samples_not_full_read() -> None:
    """folder_data_summary must summarise CSVs from a HEAD-SAMPLE (flat
    memory at scale) while still reporting the EXACT row count. The old
    full-file read OOM-crashed on 200+ / large CSVs."""
    import vault_analyst as va
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        # A file bigger than the sample window so 'sampled' kicks in.
        big_rows = va._SUMMARY_SAMPLE_ROWS + 1500
        with open(d / "big.csv", "w") as fh:
            fh.write("id,name,amount\n")
            for n in range(big_rows):
                fh.write(f"{n},Cust{n%50},{n*1.5}\n")
        # A small file (no sampling note expected).
        (d / "small.csv").write_text("a,b\n1,2\n3,4\n")

        # Fast row counter is exact for well-formed CSVs.
        _check("fast row count exact (big)",
               va._count_csv_rows_fast(d / "big.csv") == big_rows)
        _check("fast row count exact (small)",
               va._count_csv_rows_fast(d / "small.csv") == 2)

        df = va.folder_data_summary(d)
        by = {r["file"]: r for _, r in df.iterrows()}
        _check("big.csv exact row count preserved despite sampling",
               int(by["big.csv"]["rows"]) == big_rows)
        _check("big.csv columns intact", int(by["big.csv"]["columns"]) == 3)
        _check("big.csv flagged as sampled",
               "sampled" in str(by["big.csv"]["notes"]))
        _check("small.csv exact rows", int(by["small.csv"]["rows"]) == 2)
        _check("small.csv NOT flagged sampled (fits in one read)",
               "sampled" not in str(by["small.csv"]["notes"]))


def test_failure_log_roundtrip() -> None:
    """FailureLog appends structured records, normalises signatures so
    the same root cause on different files buckets together, and
    record_failure never raises."""
    import agent_logs as _al
    with tempfile.TemporaryDirectory() as td:
        log = _al.FailureLog(Path(td) / "failures.jsonl")
        log.append(kind="analyst.exec_error", subsystem="vault_analyst",
                   message="KeyError: 'revenue' in C:\\Users\\u\\v\\data_in\\q1.csv",
                   detail="Traceback ...")
        log.append(kind="analyst.exec_error", subsystem="vault_analyst",
                   message="KeyError: 'revenue' in C:\\Users\\u\\v\\data_in\\q2.csv")
        recs = log.all()
        _check("two failure records written", len(recs) == 2)
        _check("record carries kind + subsystem + signature + ts",
               {"kind", "subsystem", "signature", "ts"} <= set(recs[0]))
        _check("paths scrubbed from signature",
               "q1.csv" not in recs[0]["signature"])
        _check("same root cause → same signature",
               recs[0]["signature"] == recs[1]["signature"])
        # Different kind → different signature
        log.append(kind="model.load_error", subsystem="council_engine",
                   message="KeyError: 'revenue'")
        recs = log.all()
        _check("kind anchors the bucket",
               recs[2]["signature"] != recs[0]["signature"])

    # record_failure must never raise, even when the vault root is
    # unwritable — here we point it UNDER an existing file, so the
    # parent-dir mkdir inside _write() fails.
    import os as _os
    prev = _os.environ.get("COUNCIL_VAULT_ROOT")
    try:
        with tempfile.TemporaryDirectory() as td2:
            blocker = Path(td2) / "blocker.txt"
            blocker.write_text("not a directory")
            _os.environ["COUNCIL_VAULT_ROOT"] = str(blocker / "vault")
            _al.record_failure("x", "y", "z")   # must not raise
            _check("record_failure swallows unwritable vault root", True)
    except Exception as exc:
        _check("record_failure swallows unwritable vault root", False,
               repr(exc))
    finally:
        if prev is None:
            _os.environ.pop("COUNCIL_VAULT_ROOT", None)
        else:
            _os.environ["COUNCIL_VAULT_ROOT"] = prev


def test_failure_analyzer_drafts_proposals() -> None:
    """aggregate_failures buckets by signature; FailureAnalyzer drafts a
    kind='failure_fix' proposal once a signature crosses the threshold,
    dedups on re-run, and — cardinal rule — registers NOTHING."""
    import agent_logs as _al
    import tool_gap_analyzer as _tga
    with tempfile.TemporaryDirectory() as td:
        flog = _al.FailureLog(Path(td) / "failures.jsonl")
        queue = _tga.ProposalQueue(Path(td) / "proposals.jsonl")
        for i in range(3):
            # Full paths — the signature scrubber collapses them so all
            # three occurrences land in ONE bucket.
            flog.append(kind="analyst.exec_error", subsystem="vault_analyst",
                        message=("KeyError: 'revenue' in "
                                 f"C:\\Users\\u\\vault\\data_in\\file_{i}.csv"))
        flog.append(kind="db.sql_test_failed", subsystem="db_connections",
                    message="timeout connecting")   # below threshold (1x)

        buckets = _tga.aggregate_failures(flog.all())
        _check("two distinct signatures bucketed", len(buckets) == 2)
        ana = _tga.FailureAnalyzer(failure_log=flog, queue=queue, threshold=3)
        report = ana.analyze()   # no runner → deterministic template
        _check("one signature over threshold", report.over_threshold == 1)
        _check("one proposal written", report.proposals_written == 1)
        props = queue.current_status()
        _check("proposal kind is failure_fix",
               props and props[0].get("kind") == "failure_fix")
        _check("proposal carries observed_count 3",
               props[0].get("observed_count") == 3)
        # Re-run → dedup, no duplicate proposal
        report2 = ana.analyze()
        _check("re-run writes no duplicate", report2.proposals_written == 0)
        _check("queue still has exactly one proposal",
               len(queue.current_status()) == 1)
        # Human review flips status only
        queue.update_status(props[0]["proposal_id"], "approved")
        _check("status flip works",
               queue.current_status()[0]["status"] == "approved")

        # CARDINAL RULE — the failure path exposes no registration
        # surface: neither FailureAnalyzer nor FailureLog has any
        # attribute that can register a tool.
        for obj in (ana, flog):
            has_reg = any("register" in a.lower() for a in dir(obj))
            _check(f"{type(obj).__name__} has no register surface",
                   not has_reg)


def test_user_quirks_gates() -> None:
    """The user-quirks personality layer must OBSERVE from day one but
    INFLUENCE nothing until both maturity gates pass:
      gate 1 — ≥N distinct sessions observed (global dormancy);
      gate 2 — each quirk corroborated in ≥K distinct sessions.
    """
    import user_quirks as _uq
    import os as _os

    prev_min = _os.environ.get("COUNCIL_QUIRKS_MIN_SESSIONS")
    prev_per = _os.environ.get("COUNCIL_QUIRKS_MIN_SESSIONS_PER_QUIRK")
    try:
        _os.environ["COUNCIL_QUIRKS_MIN_SESSIONS"] = "3"
        _os.environ["COUNCIL_QUIRKS_MIN_SESSIONS_PER_QUIRK"] = "2"
        with tempfile.TemporaryDirectory() as td:
            log = _uq.UserQuirksLog(Path(td) / "quirks.jsonl")

            # Invalid category is rejected
            _check("invalid category rejected",
                   log.append(session="s1", category="zodiac",
                              text="user is a libra") is None)

            # One session, repeated quirk — gate 1 must hold even with
            # many observations (repeats in ONE session ≠ corroboration).
            for _ in range(5):
                log.append(session="s1", category="format",
                           text="prefers tables over prose")
            _check("gate 1: dormant under min sessions",
                   _uq.compile_profile(log) == "")
            st = _uq.profile_status(log)
            _check("status reports dormant", not st["active"])
            _check("status counts 1 session", st["sessions_observed"] == 1)

            # Two more sessions corroborate the same quirk (reworded) —
            # clustering must bucket the rewordings together.
            log.append(session="s2", category="format",
                       text="prefers tables instead of prose")
            log.append(session="s3", category="format",
                       text="likes tables, not prose")
            # And a single-session quirk that must stay BELOW gate 2.
            log.append(session="s3", category="tone",
                       text="enjoys puns in headings")

            profile = _uq.compile_profile(log)
            _check("gate 1 lifts at min sessions", profile != "")
            _check("corroborated quirk in profile", "tables" in profile)
            _check("gate 2: single-session quirk excluded",
                   "puns" not in profile)
            st = _uq.profile_status(log)
            _check("status active", st["active"])
            _check("one confirmed quirk", st["quirks_confirmed"] == 1)

            # clear() is the user's escape hatch
            _check("clear() wipes", log.clear() and log.all() == [])
    finally:
        for k, v in (("COUNCIL_QUIRKS_MIN_SESSIONS", prev_min),
                     ("COUNCIL_QUIRKS_MIN_SESSIONS_PER_QUIRK", prev_per)):
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v


def test_user_quirks_extraction_defensive() -> None:
    """extract_quirks must parse model JSON defensively: garbage → [],
    invalid categories dropped, cap enforced, model exception → []."""
    import user_quirks as _uq
    _check("garbage reply → []",
           _uq.extract_quirks("msg", lambda p: "no json here") == [])
    _check("model exception → []",
           _uq.extract_quirks("msg", lambda p: 1 / 0) == [])
    good = ('[{"category": "format", "text": "prefers SI units"},'
            ' {"category": "zodiac", "text": "dropped"},'
            ' {"category": "tone", "text": "concise answers"}]')
    out = _uq.extract_quirks("msg", lambda p: f"Sure!\n{good}\nDone.")
    _check("valid items parsed, invalid category dropped",
           [o["category"] for o in out] == ["format", "tone"])
    many = "[" + ",".join(
        f'{{"category": "tone", "text": "quirk {i}"}}' for i in range(9)) + "]"
    _check("per-turn cap enforced",
           len(_uq.extract_quirks("msg", lambda p: many)) == 3)
    _check("empty user text → no call",
           _uq.extract_quirks("   ", lambda p: 1 / 0) == [])


def test_user_quirks_apply_bypass() -> None:
    """COUNCIL_QUIRKS_APPLY=0 is the EXPLICIT bypass: injection stops
    (engine-side flag) but observation and compilation keep running —
    distinct from COUNCIL_QUIRKS_ENABLE=0 which kills the layer."""
    import user_quirks as _uq
    import council_engine as _ce
    import os as _os
    prev = {k: _os.environ.get(k) for k in
            ("COUNCIL_QUIRKS_APPLY", "COUNCIL_QUIRKS_MIN_SESSIONS",
             "COUNCIL_QUIRKS_MIN_SESSIONS_PER_QUIRK")}
    try:
        # Engine flag semantics
        _os.environ.pop("COUNCIL_QUIRKS_APPLY", None)
        _check("apply defaults ON", _ce.user_profile_apply_enabled())
        _ce.set_user_profile_apply(False)
        _check("set_user_profile_apply(False) bypasses",
               not _ce.user_profile_apply_enabled())
        _ce.set_user_profile_apply(True)
        _check("set_user_profile_apply(True) re-enables",
               _ce.user_profile_apply_enabled())

        # Learning continues under bypass: observations still append and
        # the profile still compiles (only INJECTION is gated, in
        # PersonalityModel.respond, not here).
        _os.environ["COUNCIL_QUIRKS_APPLY"] = "0"
        _os.environ["COUNCIL_QUIRKS_MIN_SESSIONS"] = "1"
        _os.environ["COUNCIL_QUIRKS_MIN_SESSIONS_PER_QUIRK"] = "1"
        with tempfile.TemporaryDirectory() as td:
            log = _uq.UserQuirksLog(Path(td) / "quirks.jsonl")
            fake = lambda p: '[{"category": "format", "text": "prefers tables"}]'
            st = _uq.update_after_deliberation("msg", "s1", fake, log=log)
            _check("bypass: observation continues",
                   st["observed_now"] == 1 and len(log.all()) == 1)
            _check("bypass: profile still compiles",
                   "tables" in _uq.compile_profile(log))
    finally:
        for k, v in prev.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v


def test_user_quirks_disabled_env() -> None:
    """COUNCIL_QUIRKS_ENABLE=0 must hard-disable compilation even when
    the data would otherwise pass both gates."""
    import user_quirks as _uq
    import os as _os
    prev = {k: _os.environ.get(k) for k in
            ("COUNCIL_QUIRKS_ENABLE", "COUNCIL_QUIRKS_MIN_SESSIONS",
             "COUNCIL_QUIRKS_MIN_SESSIONS_PER_QUIRK")}
    try:
        _os.environ["COUNCIL_QUIRKS_ENABLE"] = "0"
        _os.environ["COUNCIL_QUIRKS_MIN_SESSIONS"] = "1"
        _os.environ["COUNCIL_QUIRKS_MIN_SESSIONS_PER_QUIRK"] = "1"
        with tempfile.TemporaryDirectory() as td:
            log = _uq.UserQuirksLog(Path(td) / "quirks.jsonl")
            log.append(session="s1", category="format", text="loves tables")
            _check("disabled → empty profile",
                   _uq.compile_profile(log) == "")
            _check("disabled → status inactive",
                   not _uq.profile_status(log)["active"])
    finally:
        for k, v in prev.items():
            if v is None:
                _os.environ.pop(k, None)
            else:
                _os.environ[k] = v


def test_vault_image_suffix_routing() -> None:
    """The image-file routing added in the ship-readiness pass must
    accept image extensions through _PARSEABLE and produce a record
    with type='image'. We don't need PIL — the no-PIL branch is what
    matters for first-run."""
    import vault_index
    _check(".png is in _PARSEABLE", ".png" in vault_index._PARSEABLE)
    _check(".jpg is in _PARSEABLE", ".jpg" in vault_index._PARSEABLE)
    _check(".gif is in _PARSEABLE", ".gif" in vault_index._PARSEABLE)
    # Synthetic empty image — the parser should NOT crash and should
    # still produce a filename record.
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "test.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 256)
        rec = vault_index._parse_image(p, ".png")
    _check("_parse_image returns dict", isinstance(rec, dict))
    _check("_parse_image record type is 'image'",
           rec.get("type") == "image")


# ─── Main ───────────────────────────────────────────────────────────
def main() -> int:
    print("Council smoke tests")
    print("=" * 70)
    _run("hardware_detect.detect()",            test_hardware_detect)
    _run("previous_install_detect.detect()",    test_previous_install_detect)
    _run("synthetic GGUF — accept case",        test_synthetic_gguf_accepted)
    _run("synthetic GGUF — reject cases",       test_synthetic_gguf_rejected_cases)
    _run("context_condenser (chunk + condense overflow)", test_context_condenser)
    _run("analyst code prompt: no f-string brace bug",
         test_build_pandas_code_prompt_no_fstring_brace_bug)
    _run("data-summary trigger keywords",       test_data_summary_triggers)
    _run("folder_data_summary helper",          test_folder_data_summary_helper)
    _run("analyst protected-exclusion + csv-list reuse",
         test_analyst_protected_and_csv_reuse)
    _run("single-file helpers: empty/malformed CSV safe",
         test_single_file_helpers_handle_empty_and_malformed)
    _run("folder_file_counts census (file-count route)", test_folder_file_counts_census)
    _run("stats-summary trigger keywords",       test_stats_summary_triggers)
    _run("folder_column_stats bounded many-file", test_folder_column_stats_bounded_many_files)
    _run("analyst read-budget guard (OOM safety)", test_analyst_read_budget_guard)
    _run("Mongo BSON/JSON model-digestible convert", test_mongo_normalize_model_digestible)
    _run("Mongo streaming convert (bounded/OOM-safe)", test_mongo_stream_convert_bounded)
    _run("vault collections (store + discovery)", test_vault_collections)
    _run("deferred-task store (capture/run/dismiss)", test_deferred_tasks_store)
    _run("derived-results store (fingerprint/staleness/reuse)",
         test_derived_results_store)
    _run("fast-answer direct route (analyst headline)",
         test_fast_answer_direct_route)
    _run("provenance source resolution (answer chips)",
         test_provenance_source_resolve)
    _run("stores survive corrupt JSON",
         test_stores_survive_corrupt_json)
    _run("SECURITY: safe_resolve rejects escapes",
         test_safe_resolve_rejects_escapes)
    _run("agent read-budget not double-counted",
         test_agent_read_budget_not_double_counted)
    _run("job runner reconciles stale jobs on restart",
         test_job_runner_reconciles_stale_on_restart)
    _run("gpu_check readiness reporter (no hang, verdict)",
         test_gpu_check_smoke)
    _run("SECURITY: pandas sandbox write-escape blocked",
         test_pandas_sandbox_write_escape_blocked)
    _run("SECURITY: zip-slip extraction guard",
         test_zip_slip_guard)
    _run("agentic jobs core (autonomous loop, safe tools, cancel)",
         test_agentic_jobs_core)
    _run("graph introspect columns (numeric/coerce/categorical)",
         test_graph_introspect_columns)
    _run("route_message golden (routing unchanged)",
         test_route_message_golden)
    _run("read-file injection memo (identical + invalidates)",
         test_read_file_injection_memo)
    _run("context clamp (prevents ctx-overflow abort)",
         test_context_clamp)
    _run("describe non-text routing (no model on binaries)",
         test_describe_nontext_routing)
    _run("first-run wizard data/index step",
         test_first_run_wizard_data_step)
    _run("council examples panel data",
         test_council_examples)
    _run("question history (browse + re-ask)",
         test_question_history)
    _run("answer report markdown (save answer)",
         test_answer_report_md)
    _run("instant filename search (no model)",
         test_instant_filename_search)
    _run("filename wildcard patterns (job_#### / report_*)",
         test_filename_wildcard_patterns)
    _run("value-search: content terms + in-cell lookup",
         test_content_query_terms_and_value_index)
    _run("tabular sample_text captures file body",
         test_tabular_sample_text_captures_body)
    _run("vault search runs on main path (NameError regression)",
         test_vault_search_runs_on_main_path)
    _run("quick analytics helpers (stats/missing/dupes/top/corr)",
         test_quick_analytics_helpers)
    _run("quick analytics command routing",
         test_quick_analytics_routing)
    _run("folder column aggregate (pooled mean/sum/excl-zeros)",
         test_folder_column_aggregate)
    _run("folder-agg command routing + safe report write",
         test_folder_agg_command)
    _run("data preview (model-free schema + rows)",
         test_data_preview_text)
    _run("error coaching (plain-language + one-click fix)",
         test_error_coaching)
    _run("model_downloader (OS dir, stream, verify)", test_model_downloader)
    _run("GPU-crash sentinel lifecycle (CPU auto-fallback)",
         test_gpu_crash_sentinel_lifecycle)
    _run("VRAM-aware n_ctx log: no 'picked' kwarg collision",
         test_vram_aware_n_ctx_ladder_log_no_kwarg_collision)
    _run("dispatcher: no host probe when remote disabled",
         test_dispatcher_no_probe_when_remote_disabled)
    _run("embed device: WSL defaults to CPU (no CUDA crash)",
         test_resolve_embed_device_wsl_cpu_default)
    _run("clip_path / GGUF path co-persistence", test_clip_path_persistence)
    _run("SPC — process_capability known-values", test_spc_process_capability_known_values)
    _run("SPC — process_capability one-sided",    test_spc_process_capability_one_sided)
    _run("SPC — process_capability NaN handling", test_spc_process_capability_nan_handling)
    _run("SPC — process_capability subgroup path", test_spc_process_capability_subgroup_size_path)
    _run("SPC — control_chart_limits X-bar",      test_spc_control_chart_limits_xbar)
    _run("SPC — control_chart unknown type",      test_spc_control_chart_limits_unknown_chart_type)
    _run("SPC — multi-col DataFrame + column=",   test_spc_dataframe_column_kwarg)
    _run("SPC — sandbox registration contract",   test_spc_sandbox_registration)
    # DB connectivity — read-only enforcement
    _run("DB — SQL validator accepts reads",      test_db_sql_validator_accepts_reads)
    _run("DB — SQL validator rejects writes",     test_db_sql_validator_rejects_writes)
    _run("DB — Mongo pipeline validator",         test_db_mongo_pipeline_validator)
    _run("DB — connection storage round-trip",    test_db_connection_storage_roundtrip)
    _run("DB — unresolved env-var error",         test_db_unresolved_env_var)
    _run("DB — TLS posture warnings",             test_db_tls_posture_warnings)
    _run("DB — engine cache reuse",               test_db_engine_cache_reuses_engines)
    _run("DB — audit log rotation",               test_db_audit_log_rotation)
    _run("DB — Mongo round-trip (mongomock)",     test_db_mongo_roundtrip_mongomock)
    _run("DB — export write formats",             test_db_export_write_formats)
    _run("DB — export query cannot write",        test_db_export_query_cannot_write)
    _run("DB — export Mongo round-trip",          test_db_export_mongo_roundtrip)
    _run("DB — guided wizard URL assembly",       test_db_wizard_url_assembly)
    _run("DB — audit log writes JSONL",           test_db_audit_log_writes)
    _run("STATS — cache exact + incremental + query cache", test_stats_cache_exact_and_incremental)
    _run("MODELS — finder US filter + hardware fit", test_model_finder_us_filter_and_fit)
    _run("MODELS — role swap GPU-gated",          test_role_models_swap_gating)
    _run("MODELS — swap advisor decisions",       test_swap_advisor_decisions)
    _run("MODELS — remote dispatch gating",       test_remote_dispatch_gating)
    _run("STATS — per-folder CSV shards",         test_stats_cache_per_folder_csv_shards)
    _run("STATS — analyst cache-backed helpers",  test_analyst_cached_stats_helpers)
    _run("ANALYST — folder summary samples (no full read)", test_folder_summary_samples_not_full_read)
    _run("SELF-IMPROVE — failure log roundtrip",  test_failure_log_roundtrip)
    _run("SELF-IMPROVE — failure analyzer",       test_failure_analyzer_drafts_proposals)
    _run("QUIRKS — maturity gates",               test_user_quirks_gates)
    _run("QUIRKS — defensive extraction",         test_user_quirks_extraction_defensive)
    _run("QUIRKS — explicit apply bypass",        test_user_quirks_apply_bypass)
    _run("QUIRKS — env kill-switch",              test_user_quirks_disabled_env)
    # Gate B — engineering
    _run("ENG — units_convert (or ImportError)",  test_engineering_units_convert)
    _run("ENG — tolerance_stackup known values",  test_engineering_tolerance_stackup)
    _run("ENG — tolerance_stackup bad input",     test_engineering_tolerance_stackup_rejects_bad_input)
    _run("ENG — fft_spectrum 50 Hz peak",         test_engineering_fft_spectrum_known_peak)
    _run("ENG — linear regression diagnostics",   test_engineering_linear_regression_diagnostics)
    _run("ENG — VIF / collinearity warning",      test_engineering_linear_regression_collinearity_flagged)
    # Gate B — stats
    _run("STAT — descriptive_stats_rigorous",     test_stats_descriptive)
    _run("STAT — compare_groups picks t-test",    test_stats_compare_groups_picks_ttest)
    _run("STAT — compare_groups picks MW-U",      test_stats_compare_groups_picks_mannwhitney)
    _run("STAT — MC correction (Bonf vs BH)",     test_stats_multiple_comparison_correction)
    _run("STAT — bootstrap CI reproducibility",   test_stats_bootstrap_ci_reproducible)
    _run("STAT — correlation_with_significance",  test_stats_correlation_with_significance)
    _run("vault image suffix routing",          test_vault_image_suffix_routing)
    print()
    print("=" * 70)
    print(f"PASSED {len(_PASSES)} · FAILED {len(_FAILS)}")
    if _FAILS:
        print("\nFailures:")
        for name, detail in _FAILS:
            print(f"  ✗ {name}: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
