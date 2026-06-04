"""
analyst_eval.py — pretend-senior-analyst evaluation of Council.

Exercises every subsystem against the synthetic corpus in
~/.council/vault/analyst_eval/:

  Suite A: VaultIndex keyword search (DSL: phrase / boolean / regex /
           ext: / size: / mtime:)
  Suite B: DataIndex cross-file value lookup (the "Find C0042" path)
  Suite C: vault_rag.py ChromaDB semantic chunking
  Suite D: Granite Q&A grounded on top hits
  Suite E: Image inspection (does the system find/handle images at all?)

Each test records: timing, what was returned, expected vs actual,
and a verdict (PASS / DEGRADED / FAIL / N/A).
"""
from __future__ import annotations

import _windll_bootstrap  # noqa: F401

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

VAULT = Path.home() / ".council" / "vault"
CORPUS = VAULT / "analyst_eval"
GGUF = Path(__file__).parent / "models" / "granite-3.1-8b-instruct-Q4_K_M.gguf"

CTX_HIT_CHARS = 1500
LLM_MAX_TOKENS = 320


def _hdr(s: str) -> None:
    print("\n" + "=" * 80)
    print(f"  {s}")
    print("=" * 80)


def _sub(s: str) -> None:
    print("\n" + "-" * 80)
    print(f"  {s}")
    print("-" * 80)


def _result(label: str, status: str, detail: str = "") -> Dict[str, str]:
    print(f"  >>> {label}: {status}" + (f"   ({detail})" if detail else ""))
    return {"label": label, "status": status, "detail": detail}


# ────────────────────────────────────────────────────────────────
# Suite A — VaultIndex keyword + DSL
# ────────────────────────────────────────────────────────────────
def suite_a(idx, results: List[Dict[str, str]]) -> None:
    _hdr("Suite A — VaultIndex keyword DSL")

    cases = [
        # (label, query, hit_must_be_under_analyst_eval, expected_filename_substring)
        ("plain term: 'returns'",          "returns",                        True,  "returns"),
        ("phrase: 'Acme Federal'",         '"Acme Federal"',                 True,  None),
        ("boolean OR: Obsidian OR Quartz", "Obsidian OR Quartz",             True,  None),
        ("regex: SKU code",                r"/SKU-\d{4}/",                   True,  None),
        ("filter ext:xlsx",                "price ext:xlsx",                 True,  "products.xlsx"),
        ("filter ext:json",                "credit ext:json",                True,  "customers.json"),
        ("filter ext:csv + size",          "sales_rep ext:csv size:>10kb",   True,  "orders.csv"),
        ("filter ext:pdf",                 "warranty ext:pdf",               True,  "product_specs.pdf"),
        ("filter ext:md",                  "RFP ext:md",                     True,  "q3_summary.md"),
        ("image extension support",        "SKU ext:png",                    True,  ".png"),
    ]
    for label, q, scoped, expect_sub in cases:
        t0 = time.time()
        try:
            hits, _ = idx.search(q, k=8)
        except Exception as e:
            results.append(_result(label, "FAIL", f"exception: {e!r}"))
            continue
        dt = (time.time() - t0) * 1000
        rels = []
        scoped_hits = []
        for score, rec in hits:
            p = rec.get("path") or rec.get("relpath") or ""
            try:
                rel = str(Path(p).relative_to(VAULT))
            except Exception:
                rel = str(p)
            rels.append((score, rel))
            if "analyst_eval" in rel:
                scoped_hits.append((score, rel))
        print(f"  query: {q}")
        print(f"    {len(hits)} hits in {dt:.0f} ms, "
              f"{len(scoped_hits)} from analyst_eval/")
        for sc, r in rels[:5]:
            print(f"      [{sc:6.2f}] {r}")

        if not hits:
            results.append(_result(label, "FAIL", "0 hits"))
            continue
        if expect_sub is None:
            results.append(_result(label, "PASS", f"{len(hits)} hits, {dt:.0f} ms"))
            continue
        ok = any(expect_sub.lower() in r.lower() for _, r in scoped_hits or rels)
        if ok:
            results.append(_result(label, "PASS",
                                   f"matched '{expect_sub}' in top hits, {dt:.0f} ms"))
        else:
            top = rels[0][1] if rels else "(none)"
            results.append(_result(label, "DEGRADED",
                                   f"expected '{expect_sub}', top was '{top}'"))


# ────────────────────────────────────────────────────────────────
# Suite B — DataIndex (every-cell lookup, cross-file)
# ────────────────────────────────────────────────────────────────
def suite_b(results: List[Dict[str, str]]) -> None:
    _hdr("Suite B — DataIndex cross-file lookup")

    try:
        from data_index import DataIndex
    except Exception as e:
        results.append(_result("DataIndex import", "FAIL", repr(e)))
        return

    # search_roots = corpus folder; write_root MUST be outside the corpus
    # (DataIndex refuses overlap to enforce "inputs are never overwritten").
    write_dir = VAULT / "_data_out_eval"
    write_dir.mkdir(exist_ok=True)
    try:
        di = DataIndex(search_roots=[CORPUS], write_root=write_dir)
    except Exception as e:
        results.append(_result("DataIndex init", "FAIL", repr(e)))
        return
    print(f"  LOADABLE_EXTS: {sorted(DataIndex.LOADABLE_EXTS)}  "
          f"(note: .xlsx is NOT in this set)")

    # Build profiles (CSV/JSON/XLSX cell scans)
    t0 = time.time()
    try:
        di.refresh()
    except Exception as e:
        results.append(_result("DataIndex refresh", "FAIL", repr(e)))
        return
    print(f"  refresh: {(time.time()-t0)*1000:.0f} ms, "
          f"profiles={len(list(di.all_profiles()))}")
    for prof in di.all_profiles():
        err = f"  ERR: {prof.error}" if getattr(prof, "error", None) else ""
        print(f"   - {prof.name}: {prof.row_count} rows, cols={prof.columns[:5]}…{err}")

    # B1: lookup an ID that lives in customers.json AND orders.csv AND returns.csv
    _sub("B1: cross-file lookup for 'C0042'")
    t0 = time.time()
    try:
        hits = di.search_value("C0042")
    except Exception as e:
        results.append(_result("B1 search_value('C0042')", "FAIL", repr(e)))
    else:
        dt = (time.time() - t0) * 1000
        files = sorted({Path(h["file"]).name for h in hits})
        print(f"  {len(hits)} file(s) matched in {dt:.0f} ms: {files}")
        for h in hits:
            print(f"   - {h['file']}: matched_count={h['matched_count']}  cols={h['column_hits']}")
        wanted = {"orders.csv", "returns.csv", "customers.json"}
        missing = wanted - set(files)
        if not missing:
            results.append(_result("B1 finds C0042 in all 3 files", "PASS",
                                   f"files={files}, {dt:.0f} ms"))
        else:
            results.append(_result("B1 finds C0042 in all 3 files", "DEGRADED",
                                   f"missing: {sorted(missing)}"))

    # B2: SKU lookup
    _sub("B2: cross-file lookup for 'SKU-1004'")
    t0 = time.time()
    try:
        hits = di.search_value("SKU-1004")
    except Exception as e:
        results.append(_result("B2 search_value('SKU-1004')", "FAIL", repr(e)))
        return
    dt = (time.time() - t0) * 1000
    files = sorted({Path(h["file"]).name for h in hits})
    print(f"  {len(hits)} files matched in {dt:.0f} ms: {files}")
    wanted = {"orders.csv", "returns.csv", "products.xlsx"}
    missing = wanted - set(files)
    if not missing:
        results.append(_result("B2 SKU spans products/orders/returns", "PASS",
                               f"files={files}, {dt:.0f} ms"))
    else:
        results.append(_result("B2 SKU spans products/orders/returns", "DEGRADED",
                               f"missing: {sorted(missing)}"))

    # B3: lookup_related from a known source
    _sub("B3: lookup_related from orders.csv for C0042")
    try:
        rel = di.lookup_related("orders.csv", "C0042")
        rel_files = [Path(h["file"]).name for h in rel.get("related", [])]
        src = rel.get("source", {})
        src_name = Path(src.get("file", "")).name if src else "(none)"
        print(f"  source: {src_name}")
        print(f"  related: {rel_files}")
        if src_name == "orders.csv" and rel_files:
            results.append(_result("B3 lookup_related returns sibling files", "PASS",
                                   f"related={rel_files}"))
        else:
            results.append(_result("B3 lookup_related returns sibling files", "DEGRADED",
                                   f"src={src_name}, related={rel_files}"))
    except Exception as e:
        results.append(_result("B3 lookup_related", "FAIL", repr(e)))


# ────────────────────────────────────────────────────────────────
# Suite C — vault_rag semantic ChromaDB
# ────────────────────────────────────────────────────────────────
def suite_c(results: List[Dict[str, str]]) -> None:
    _hdr("Suite C — vault_rag ChromaDB semantic")
    try:
        from vault_rag import VaultRAG, INDEXABLE_EXTENSIONS
    except Exception as e:
        results.append(_result("vault_rag import", "FAIL", repr(e)))
        return
    print(f"  declared INDEXABLE_EXTENSIONS: {sorted(INDEXABLE_EXTENSIONS)}")
    missing = {".pdf", ".xlsx", ".docx", ".png", ".jpg"} - INDEXABLE_EXTENSIONS
    if missing:
        results.append(_result("RAG supports analyst formats (pdf/xlsx/docx/image)",
                               "FAIL",
                               f"semantic RAG ignores: {sorted(missing)}"))
    else:
        results.append(_result("RAG supports analyst formats", "PASS", ""))

    # Sanity: count files RAG WOULD index in the analyst folder
    cands = [p for p in CORPUS.rglob("*") if p.is_file()
             and p.suffix.lower() in INDEXABLE_EXTENSIONS]
    print(f"  analyst_eval files RAG would actually index: {len(cands)}")
    for p in cands:
        print(f"    - {p.relative_to(VAULT)}")


# ────────────────────────────────────────────────────────────────
# Suite D — Granite Q&A grounded on vault hits
# ────────────────────────────────────────────────────────────────
def suite_d(idx, results: List[Dict[str, str]]) -> None:
    _hdr("Suite D — Granite Q&A on analyst corpus")
    if not GGUF.exists():
        results.append(_result("Granite GGUF present", "FAIL", str(GGUF)))
        return
    from llama_cpp import Llama
    t0 = time.time()
    llm = Llama(model_path=str(GGUF), n_gpu_layers=99, n_ctx=8192, verbose=False)
    print(f"  Granite loaded in {time.time()-t0:.1f}s")

    questions = [
        ("Who is customer C0042 and what's their pipeline status?",
         {"acme", "federal", "logistics"}),
        ("Which sales rep owns the C0042 account based on the orders file?",
         {"patel"}),
        ("What return reason was logged for the C0042 Obsidian array order?",
         {"spec", "mismatch", "did not match"}),
        ("What products does the PriceHistory sheet cover and what change reasons appear?",
         {"tariff", "pass-through", "q2", "launch"}),
    ]
    for q, must_contain in questions:
        _sub(f"Q: {q}")
        t0 = time.time()
        hits, _ = idx.search(q, k=4, folder="analyst_eval")
        dt_search = (time.time() - t0) * 1000
        for rank, (sc, rec) in enumerate(hits, 1):
            p = rec.get("path") or ""
            try:
                rel = str(Path(p).relative_to(VAULT))
            except Exception:
                rel = str(p)
            print(f"    {rank}. [{sc:6.2f}]  {rel}")

        # Build context (read top 4 hit files; extract XLSX via openpyxl)
        ctx_blocks = []
        for rank, (sc, rec) in enumerate(hits[:4], 1):
            p = Path(rec.get("path") or "")
            if not p.exists():
                continue
            try:
                if p.suffix.lower() in (".xlsx", ".xlsm"):
                    import openpyxl as _xl
                    wb = _xl.load_workbook(p, read_only=True, data_only=True)
                    chunks = []
                    for sh in wb.worksheets:
                        chunks.append(f"## Sheet: {sh.title}")
                        for row in sh.iter_rows(max_row=30, values_only=True):
                            chunks.append("\t".join("" if v is None else str(v) for v in row))
                    text = "\n".join(chunks)
                else:
                    text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if len(text) > CTX_HIT_CHARS:
                text = text[:CTX_HIT_CHARS] + "\n…[truncated]…"
            ctx_blocks.append(f"[SOURCE #{rank}] {p.name}\n{text}")
        if not ctx_blocks:
            results.append(_result(f"D: {q[:40]}…", "DEGRADED", "no readable hits"))
            continue
        prompt = (
            "Use ONLY the sources below. Cite source numbers [SOURCE #N] inline.\n\n"
            + "\n---\n".join(ctx_blocks)
            + f"\n\nQUESTION: {q}\nANSWER:"
        )
        t0 = time.time()
        try:
            out = llm.create_chat_completion(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2, max_tokens=LLM_MAX_TOKENS,
            )
        except Exception as e:
            results.append(_result(f"D: {q[:40]}…", "FAIL", repr(e)))
            continue
        dt_llm = time.time() - t0
        answer = out["choices"][0]["message"]["content"].strip()
        print(f"  --- Granite ({dt_llm:.1f}s, search {dt_search:.0f} ms) ---")
        for line in answer.splitlines():
            print(f"  {line}")
        lo = answer.lower()
        hit_terms = [t for t in must_contain if t in lo]
        if len(hit_terms) >= max(1, len(must_contain) // 2):
            results.append(_result(f"D: {q[:50]}…", "PASS",
                                   f"matched {hit_terms} ({dt_llm:.1f}s)"))
        else:
            results.append(_result(f"D: {q[:50]}…", "DEGRADED",
                                   f"expected any of {must_contain}, got '{lo[:80]}…'"))


# ────────────────────────────────────────────────────────────────
# Suite E — Images
# ────────────────────────────────────────────────────────────────
def suite_e(idx, results: List[Dict[str, str]]) -> None:
    _hdr("Suite E — Images")

    img_dir = CORPUS / "images"
    pngs = sorted(img_dir.glob("*.png"))
    print(f"  {len(pngs)} images on disk:")
    for p in pngs:
        print(f"    - {p.relative_to(VAULT)} ({p.stat().st_size:,} B)")

    # E1: can vault index find an image by SKU mentioned in its filename?
    _sub("E1: keyword search for 'sku-1001' (filename match)")
    hits, _ = idx.search("sku-1001", k=5)
    img_hits = [
        rec for _, rec in hits
        if (rec.get("path") or "").lower().endswith(".png")
    ]
    print(f"  total hits: {len(hits)}   image hits: {len(img_hits)}")
    for sc, rec in hits[:3]:
        p = rec.get("path") or ""
        print(f"    [{sc:6.2f}] {p}")
    if img_hits:
        results.append(_result("E1 image discoverable by filename", "PASS",
                               f"{len(img_hits)} image hit(s)"))
    else:
        results.append(_result("E1 image discoverable by filename", "FAIL",
                               "no .png returned"))

    # E2: any text on the image surfaces (would need OCR)
    _sub("E2: keyword search for text rendered ON image ('PROPRIETARY')")
    hits, _ = idx.search('"PROPRIETARY"', k=5)
    pic_hits = [r for _, r in hits if (r.get("path") or "").lower().endswith(".png")]
    print(f"  total hits: {len(hits)}   image hits: {len(pic_hits)}")
    if pic_hits:
        results.append(_result("E2 OCR over rendered image text", "PASS", "image returned"))
    else:
        results.append(_result("E2 OCR over rendered image text", "FAIL",
                               "no OCR — image pixel content invisible to index"))


# ────────────────────────────────────────────────────────────────
# Driver
# ────────────────────────────────────────────────────────────────
def main() -> int:
    if not CORPUS.exists():
        print(f"missing corpus: {CORPUS}", file=sys.stderr)
        return 2

    from vault_index import VaultIndex
    print("loading VaultIndex...")
    t0 = time.time()
    idx = VaultIndex(VAULT)
    print(f"  records={len(idx.records)}   ({time.time()-t0:.2f}s)")
    print("rebuilding (so the new corpus is picked up)...")
    t0 = time.time()
    idx.rebuild()
    print(f"  records={len(idx.records)}   ({time.time()-t0:.1f}s)")

    results: List[Dict[str, str]] = []

    suite_a(idx, results)
    suite_b(results)
    suite_c(results)
    suite_e(idx, results)
    suite_d(idx, results)

    # ── Tally ────────────────────────────────────────────────
    _hdr("Tally")
    counts: Dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    for k in ("PASS", "DEGRADED", "FAIL", "N/A"):
        if k in counts:
            print(f"  {k:8s}: {counts[k]}")
    print("\n  Detail:")
    for r in results:
        d = f"   ({r['detail']})" if r["detail"] else ""
        print(f"   - [{r['status']:8s}] {r['label']}{d}")
    return 0 if counts.get("FAIL", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
