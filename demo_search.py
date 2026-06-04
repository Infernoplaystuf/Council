"""
demo_search.py — proof-of-life search demo for the Council vault.

Runs a sequence of queries against the user's ~/.council/vault using
VaultIndex's full query DSL (plain / phrase / boolean / regex / filters)
and prints top hits per query. No GUI required.

Usage:
    .venv\\Scripts\\python.exe demo_search.py
"""
from __future__ import annotations

import _windll_bootstrap  # noqa: F401

import os
import sys
import time
from pathlib import Path

VAULT = Path.home() / ".council" / "vault"


def _hdr(s: str) -> None:
    print()
    print("=" * 78)
    print(f"  {s}")
    print("=" * 78)


def main() -> int:
    if not VAULT.exists():
        print(f"vault not found: {VAULT}", file=sys.stderr)
        return 1

    _hdr(f"Vault: {VAULT}")

    # Build / load index.
    print("loading VaultIndex...")
    t0 = time.time()
    from vault_index import VaultIndex
    idx = VaultIndex(VAULT)
    print(f"  loaded in {time.time()-t0:.2f}s  records={len(idx.records)}")

    if len(idx.records) == 0:
        print("  index empty — running first-time rebuild (this may take a minute)...")
        t0 = time.time()
        idx.rebuild()
        print(f"  rebuilt in {time.time()-t0:.1f}s  records={len(idx.records)}")
    else:
        print("  (use idx.rebuild() in a separate run to refresh if needed)")

    queries = [
        ("Plain free-text",        "transformer attention"),
        ("Phrase match",           '"nanoGPT" AND attention'),
        ("Boolean OR + NOT",       "(embedding OR tokenizer) AND NOT test"),
        ("Regex — class defs",     r"/class\s+\w+Layer/"),
        ("Filter — Python files",  "training ext:py size:>1kb"),
        ("Filter — recent docs",   "tutorial ext:md mtime:<90d"),
        ("Cross-domain lookup",    "axolotl finetuning"),
    ]

    for label, q in queries:
        _hdr(f"{label}:  {q}")
        t0 = time.time()
        try:
            hits, fuzzy = idx.search(q, k=5)
        except Exception as e:
            print(f"  ERROR: {e!r}")
            continue
        dt = time.time() - t0
        print(f"  {len(hits)} hit(s)   in {dt*1000:.0f} ms")
        for rank, (score, rec) in enumerate(hits, 1):
            path = rec.get("path") or rec.get("relpath") or "?"
            try:
                rel = str(Path(path).relative_to(VAULT))
            except Exception:
                rel = str(path)
            size = rec.get("size_bytes")
            size_s = f"{size/1024:.1f} KB" if isinstance(size, int) else ""
            print(f"   {rank}. [{score:6.2f}]  {rel}   {size_s}")
        if fuzzy:
            for term, suggestions in list(fuzzy.items())[:3]:
                joined = ", ".join(f"{t} ({s:.2f})" for t, s in suggestions[:3])
                print(f"    fuzzy-expand '{term}' → {joined}")

    _hdr("Demo complete.")
    print(f"Index records: {len(idx.records)}")
    print(f"Vault root:    {VAULT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
