"""Standalone re-test of P5 row-level citations after per-id-cap fix."""
from __future__ import annotations

import _windll_bootstrap  # noqa: F401

from pathlib import Path
from row_citations import extract_identifiers, cite_rows

CORPUS = Path.home() / ".council" / "vault" / "mfg_eval"

answer = (
    "The non-conformance NCR-99001 was logged against WO-99002 for "
    "PART-3001, citing DEF-001 (dimensional out of tolerance). It "
    "triggered ECN-2026-017."
)
print("Identifiers:", extract_identifiers(answer))

cited = [CORPUS / "work_orders.csv", CORPUS / "defects.json",
         CORPUS / "parts_master.xlsx"]
block = cite_rows(answer, cited)
print("\n--- cite_rows block ---\n")
print(block)
print("\n--- assertions ---")
assert "WO-99002" in block, "WO-99002 not in block (per-id cap fix failed)"
assert "NCR-99001" in block, "NCR-99001 not in block"
assert "PART-3001" in block, "PART-3001 not in block"
print("  PASS: WO-99002, NCR-99001, PART-3001 all surfaced.")
