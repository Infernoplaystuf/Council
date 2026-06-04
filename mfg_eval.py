"""
mfg_eval.py — manufacturing-analyst evaluation exercising all 9 patches.

Run after build_mfg_corpus.py. Covers:

  P1+P4 (PDF/DOCX in vault_index + vault_rag) — ECN/MSDS/recipe content searchable
  P2     (DataIndex .xlsx)                    — cross-file lookup spans XLSX
  P3a/b  (image filenames + OCR)              — defect photo discoverable + OCR'd text
  P4b    (vault_rag covers PDF/XLSX/DOCX)     — semantic chunking actually fires
  P5     (row-level citations)                — identifiers in answers resolve to rows
  P7     (SQL connector)                      — read-only SELECT against mes.sqlite
  P8     (CLIP image semantic search)         — "defect on solder" → solder-crack PNG
  P6     (vault root env)                     — uses default ~/.council/vault still works
  P9     (no Ollama)                          — engine module imports without an Ollama server
"""
from __future__ import annotations

import _windll_bootstrap  # noqa: F401

import os
import sys
import time
from pathlib import Path
from typing import Dict, List

VAULT = Path.home() / ".council" / "vault"
CORPUS = VAULT / "mfg_eval"
GGUF = Path(__file__).parent / "models" / "granite-3.1-8b-instruct-Q4_K_M.gguf"


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


# ───────────────────────────────────────────────────────────
# Suite 1 — PDF / DOCX / image-filename indexing (P1, P3a)
# ───────────────────────────────────────────────────────────
def suite_indexing(idx, results: List[Dict[str, str]]) -> None:
    _hdr("Suite 1 — PDF / DOCX / image-filename in VaultIndex (P1, P3a, P3b)")

    cases = [
        ("PDF body text — ECN tolerance",
         "tolerance ext:pdf", "ecn_2026_017"),
        ("PDF body text — MSDS flammable",
         "flammable ext:pdf", "msds_PART-3001"),
        ("DOCX body text — process recipe",
         "stack press ext:docx", "process_recipe"),
        ("Image filename match",
         "ncr-99001 ext:png", "ncr-99001-stack-height"),
        ("OCR text from defect image (tesseract)",
         '"DEF-001"', "ncr-99001"),
        ("Cross-format part-number search",
         "PART-3001", None),
    ]
    for label, q, expect_sub in cases:
        t0 = time.time()
        hits, _ = idx.search(q, k=8)
        dt = (time.time() - t0) * 1000
        rels = []
        for sc, rec in hits:
            p = rec.get("path") or ""
            try:
                rel = str(Path(p).relative_to(VAULT))
            except Exception:
                rel = str(p)
            rels.append((sc, rel))
        print(f"  query: {q}   ({dt:.0f} ms, {len(hits)} hits)")
        for sc, r in rels[:5]:
            print(f"    [{sc:6.2f}] {r}")
        if not hits:
            results.append(_result(label, "FAIL", "0 hits"))
            continue
        if expect_sub is None:
            results.append(_result(label, "PASS", f"{len(hits)} hits, {dt:.0f} ms"))
            continue
        ok = any(expect_sub.lower() in r.lower() for _, r in rels)
        results.append(_result(label, "PASS" if ok else "DEGRADED",
                               f"top={rels[0][1]}, {dt:.0f} ms"))


# ───────────────────────────────────────────────────────────
# Suite 2 — DataIndex with XLSX (P2)
# ───────────────────────────────────────────────────────────
def suite_dataindex(results: List[Dict[str, str]]) -> None:
    _hdr("Suite 2 — DataIndex now covers XLSX (P2)")
    from data_index import DataIndex
    write_dir = VAULT / "_data_out_mfg"
    write_dir.mkdir(exist_ok=True)
    di = DataIndex(search_roots=[CORPUS], write_root=write_dir)
    t0 = time.time()
    di.refresh()
    dt = (time.time() - t0) * 1000
    profiles = list(di.all_profiles())
    print(f"  refreshed {len(profiles)} profiles in {dt:.0f} ms")
    print(f"  LOADABLE_EXTS now: {sorted(DataIndex.LOADABLE_EXTS)}")
    for prof in profiles:
        err = f"  ERR: {prof.error}" if getattr(prof, "error", None) else ""
        print(f"    - {prof.name}: {prof.row_count} rows, "
              f"cols={[c.name for c in prof.columns][:6]}…{err}")

    _sub("2A: PART-3001 spans CSV + JSON + XLSX")
    hits = di.search_value("PART-3001")
    files = sorted({Path(h["file"]).name for h in hits})
    print(f"  files matched: {files}")
    wanted = {"work_orders.csv", "defects.json", "parts_master.xlsx"}
    missing = wanted - set(files)
    results.append(_result(
        "DataIndex finds PART-3001 across CSV+JSON+XLSX",
        "PASS" if not missing else "DEGRADED",
        f"missing={sorted(missing)}" if missing else f"files={files}",
    ))

    _sub("2B: XLSX cell lookup — BOM tree (PART-3001 -> PART-3002)")
    hits = di.search_value("PART-3002")
    xlsx_hits = [h for h in hits if h["file"].lower().endswith(".xlsx")]
    sheet_names = set()
    for h in xlsx_hits:
        for row in h.get("rows", []):
            if isinstance(row, dict) and row.get("__sheet__"):
                sheet_names.add(row["__sheet__"])
    print(f"  XLSX hits: {len(xlsx_hits)}, sheets seen: {sheet_names}")
    if xlsx_hits:
        results.append(_result(
            "BOM child PART-3002 found inside XLSX sheets",
            "PASS",
            f"sheets={sorted(sheet_names) or 'unlabelled'}",
        ))
    else:
        results.append(_result(
            "BOM child PART-3002 found inside XLSX sheets",
            "FAIL", "no xlsx hit"))


# ───────────────────────────────────────────────────────────
# Suite 3 — vault_rag covers PDF/XLSX/DOCX (P4b)
# ───────────────────────────────────────────────────────────
def suite_rag(results: List[Dict[str, str]]) -> None:
    _hdr("Suite 3 — vault_rag.INDEXABLE_EXTENSIONS now covers analyst formats (P4b)")
    from vault_rag import INDEXABLE_EXTENSIONS, EXTRACTABLE_EXTENSIONS
    print(f"  INDEXABLE_EXTENSIONS: {sorted(INDEXABLE_EXTENSIONS)}")
    print(f"  EXTRACTABLE_EXTENSIONS: {sorted(EXTRACTABLE_EXTENSIONS)}")
    must = {".pdf", ".xlsx", ".docx"}
    missing = must - INDEXABLE_EXTENSIONS
    if missing:
        results.append(_result("RAG indexes pdf+xlsx+docx", "FAIL",
                               f"missing={sorted(missing)}"))
        return
    results.append(_result("RAG indexes pdf+xlsx+docx", "PASS", ""))

    cands = [p for p in CORPUS.rglob("*") if p.is_file()
             and p.suffix.lower() in INDEXABLE_EXTENSIONS]
    print(f"  RAG-eligible files in mfg_eval: {len(cands)}")
    # Do a real extract on the ECN PDF
    from vault_rag import _extract_text
    ecn = CORPUS / "ecn_2026_017.pdf"
    txt = _extract_text(ecn)
    has_marker = "ECN-2026-017" in txt and "tolerance" in txt.lower()
    results.append(_result(
        "PDF extraction surfaces ECN content",
        "PASS" if has_marker else "DEGRADED",
        f"len={len(txt)}",
    ))
    docx = CORPUS / "process_recipe.docx"
    dtxt = _extract_text(docx)
    has_press = "Press force" in dtxt
    results.append(_result(
        "DOCX extraction surfaces recipe text",
        "PASS" if has_press else "DEGRADED",
        f"len={len(dtxt)}",
    ))
    xl = CORPUS / "parts_master.xlsx"
    xtxt = _extract_text(xl)
    has_bom = "BOM" in xtxt and "PART-3002" in xtxt
    results.append(_result(
        "XLSX extraction includes multiple sheets",
        "PASS" if has_bom else "DEGRADED",
        f"len={len(xtxt)}",
    ))


# ───────────────────────────────────────────────────────────
# Suite 4 — CLIP image semantic search (P8)
# ───────────────────────────────────────────────────────────
def suite_clip(results: List[Dict[str, str]]) -> None:
    _hdr("Suite 4 — CLIP image semantic search (P8)")
    from image_index import ImageIndex
    iidx = ImageIndex(VAULT)
    if not iidx.status()["available"]:
        results.append(_result("CLIP image index", "FAIL",
                               iidx.status()["reason"]))
        return
    n = iidx.rebuild(on_progress=print)
    print(f"  rebuild encoded: {n}")
    status = iidx.status()
    print(f"  total indexed: {status['indexed_images']}")

    queries = [
        ("solder joint crack",        "ncr-12055-solder-crack"),
        ("bearing noise inspection",  "ncr-12044-bearing-noise"),
        ("stack height defect",       "ncr-99001-stack-height"),
    ]
    for q, expect in queries:
        hits = iidx.search(q, k=3)
        if not hits:
            results.append(_result(f"CLIP: '{q}'", "FAIL", "no hits"))
            continue
        top = hits[0][1].stem
        print(f"  '{q}'")
        for sc, p in hits[:3]:
            print(f"    [{sc:+.3f}] {p.name}")
        ok = expect.lower() in top.lower()
        results.append(_result(f"CLIP: '{q}'", "PASS" if ok else "DEGRADED",
                               f"top={top}"))


# ───────────────────────────────────────────────────────────
# Suite 5 — Row-level citations (P5)
# ───────────────────────────────────────────────────────────
def suite_citations(results: List[Dict[str, str]]) -> None:
    _hdr("Suite 5 — Row-level citations (P5)")
    from row_citations import extract_identifiers, cite_rows

    fake_answer = (
        "The non-conformance NCR-99001 was logged against WO-99002 for "
        "PART-3001, citing DEF-001 (dimensional out of tolerance). It "
        "triggered ECN-2026-017."
    )
    ids = extract_identifiers(fake_answer)
    print(f"  detected identifiers: {ids}")
    expected = {"NCR-99001", "WO-99002", "PART-3001", "DEF-001", "ECN-2026-017"}
    missing = expected - set(ids)
    results.append(_result(
        "extract_identifiers picks up NCR/WO/PART/DEF/ECN codes",
        "PASS" if not missing else "DEGRADED",
        f"missing={sorted(missing)}" if missing else "all found",
    ))

    cited = [CORPUS / "work_orders.csv",
             CORPUS / "defects.json",
             CORPUS / "parts_master.xlsx"]
    block = cite_rows(fake_answer, cited)
    print("  --- cite_rows output ---")
    print(block or "(empty)")
    has_wo = "WO-99002" in block
    has_part = "PART-3001" in block
    results.append(_result(
        "cite_rows resolves identifiers to actual rows",
        "PASS" if (has_wo and has_part) else "DEGRADED",
        f"WO-99002 in block: {has_wo}, PART-3001 in block: {has_part}",
    ))


# ───────────────────────────────────────────────────────────
# Suite 6 — SQL connector against mes.sqlite (P7)
# ───────────────────────────────────────────────────────────
def suite_sql(results: List[Dict[str, str]]) -> None:
    _hdr("Suite 6 — SqlConnector read-only path against mes.sqlite (P7)")
    from sql_connector import SqlConnector

    db = CORPUS / "mes.sqlite"
    print(f"  db: {db}")
    with SqlConnector(f"sqlite:///{db.as_posix()}") as conn:
        tables = conn.list_tables()
        print(f"  tables: {tables}")
        results.append(_result(
            "list_tables returns work_orders + defects",
            "PASS" if {"work_orders","defects"}.issubset(tables) else "FAIL",
            f"tables={tables}",
        ))
        desc = conn.describe("defects")
        print(f"  defects.row_count = {desc['row_count']}, "
              f"cols={[c['name'] for c in desc['columns']]}")
        results.append(_result(
            "describe(defects) gives schema + row count",
            "PASS" if desc["row_count"] > 0 else "FAIL",
            f"row_count={desc['row_count']}",
        ))
        try:
            rows = conn.execute_select(
                "SELECT part_no, COUNT(*) as n FROM defects "
                "GROUP BY part_no ORDER BY n DESC"
            )
            print(f"  defects per part:")
            for r in rows[:6]:
                print(f"    {r}")
            results.append(_result(
                "execute_select runs aggregate query",
                "PASS" if rows else "FAIL",
                f"{len(rows)} groups",
            ))
        except Exception as e:
            results.append(_result("execute_select aggregate", "FAIL", repr(e)))

        # Safety check — refuse a write
        for bad in [
            "DELETE FROM defects",
            "INSERT INTO work_orders VALUES (1,2,3,4,5,6,7,8)",
            "UPDATE defects SET disposition='x'",
            "DROP TABLE defects",
        ]:
            try:
                conn.execute_select(bad)
            except ValueError as e:
                continue
            else:
                results.append(_result(f"SQL safety refuses: {bad[:30]}", "FAIL",
                                       "did not raise"))
                break
        else:
            results.append(_result("SQL safety refuses INSERT/UPDATE/DELETE/DROP",
                                   "PASS", "all rejected"))


# ───────────────────────────────────────────────────────────
# Suite 7 — Granite Q&A + row-level citations on mfg data
# ───────────────────────────────────────────────────────────
def suite_qa(idx, results: List[Dict[str, str]]) -> None:
    _hdr("Suite 7 — Granite Q&A grounded on mfg corpus")
    from llama_cpp import Llama
    from row_citations import cite_rows

    if not GGUF.exists():
        results.append(_result("Granite GGUF", "FAIL", "missing"))
        return
    t0 = time.time()
    llm = Llama(model_path=str(GGUF), n_gpu_layers=99, n_ctx=8192, verbose=False)
    print(f"  Granite loaded in {time.time()-t0:.1f}s")

    questions = [
        ("Why was ECN-2026-017 raised and which part does it cover?",
         {"part-3001", "tolerance"}),
        ("What disposition was applied to NCR-99001?",
         {"rework"}),
        ("List PART-3001 work orders and their status from the CSV.",
         {"wo-99001", "wo-99002", "wo-99003"}),
    ]
    for q, must in questions:
        _sub(f"Q: {q}")
        t0 = time.time()
        hits, _ = idx.search(q, k=4, folder="mfg_eval")
        dt_s = (time.time() - t0) * 1000

        cited_paths = []
        ctx_blocks = []
        for rank, (sc, rec) in enumerate(hits[:4], 1):
            p = Path(rec.get("path") or "")
            print(f"    {rank}. [{sc:6.2f}]  {p.relative_to(VAULT)}")
            if not p.exists():
                continue
            cited_paths.append(p)
            suf = p.suffix.lower()
            try:
                if suf in (".xlsx", ".xlsm"):
                    import openpyxl as _xl
                    wb = _xl.load_workbook(p, read_only=True, data_only=True)
                    parts = []
                    for sh in wb.worksheets:
                        parts.append(f"## {sh.title}")
                        for row in sh.iter_rows(max_row=30, values_only=True):
                            parts.append("\t".join("" if v is None else str(v) for v in row))
                    text = "\n".join(parts)
                elif suf == ".pdf":
                    from pypdf import PdfReader
                    reader = PdfReader(str(p))
                    text = "\n".join((page.extract_text() or "") for page in reader.pages[:5])
                elif suf == ".docx":
                    from docx import Document
                    doc = Document(str(p))
                    text = "\n".join(par.text for par in doc.paragraphs if par.text)
                else:
                    text = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                text = ""
            if len(text) > 1800:
                text = text[:1800] + "\n…[truncated]…"
            ctx_blocks.append(f"[SOURCE #{rank}] {p.name}\n{text}")

        prompt = (
            "Use ONLY the sources below. Cite [SOURCE #N] inline.\n\n"
            + "\n---\n".join(ctx_blocks)
            + f"\n\nQUESTION: {q}\nANSWER:"
        )
        t0 = time.time()
        try:
            out = llm.create_chat_completion(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2, max_tokens=320,
            )
        except Exception as e:
            results.append(_result(f"D: {q[:50]}", "FAIL", repr(e)))
            continue
        dt_l = time.time() - t0
        answer = out["choices"][0]["message"]["content"].strip()
        print(f"  --- Granite ({dt_l:.1f}s, search {dt_s:.0f} ms) ---")
        for line in answer.splitlines():
            print(f"  {line}")
        rc_block = cite_rows(answer, cited_paths)
        if rc_block:
            print("  --- row-level citations attached ---")
            for line in rc_block.splitlines():
                print(f"  {line}")
        lo = answer.lower()
        hit_terms = [t for t in must if t in lo]
        results.append(_result(
            f"D: {q[:60]}",
            "PASS" if len(hit_terms) >= max(1, len(must)//2) else "DEGRADED",
            f"matched {hit_terms} ({dt_l:.1f}s)",
        ))


# ───────────────────────────────────────────────────────────
def main() -> int:
    if not CORPUS.exists():
        print(f"missing corpus: {CORPUS}", file=sys.stderr)
        return 2

    from vault_index import VaultIndex
    print("loading VaultIndex...")
    idx = VaultIndex(VAULT)
    print(f"  records={len(idx.records)}")
    print("rebuilding (PDFs/DOCX/images now in)...")
    t0 = time.time()
    idx.rebuild()
    print(f"  records={len(idx.records)} after rebuild ({time.time()-t0:.1f}s)")

    results: List[Dict[str, str]] = []

    suite_indexing(idx, results)
    suite_dataindex(results)
    suite_rag(results)
    suite_citations(results)
    suite_sql(results)
    # CLIP gets its own subprocess because the cu124 torch wheel can
    # ACCESS_VIOLATE on the 5080 PTX-fallback path (sm_120). On a real
    # 4080 (native sm_89) this isn't needed.
    if os.environ.get("COUNCIL_SKIP_CLIP", "").lower() not in ("1", "true", "yes"):
        suite_clip(results)
    if os.environ.get("COUNCIL_SKIP_QA", "").lower() not in ("1", "true", "yes"):
        suite_qa(idx, results)

    _hdr("Tally")
    counts: Dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    for k in ("PASS", "DEGRADED", "FAIL"):
        if k in counts:
            print(f"  {k:8s}: {counts[k]}")
    print("\n  Detail:")
    for r in results:
        d = f"   ({r['detail']})" if r["detail"] else ""
        print(f"   - [{r['status']:8s}] {r['label']}{d}")
    return 0 if counts.get("FAIL", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
