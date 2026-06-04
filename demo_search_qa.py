"""
demo_search_qa.py — vault search + Granite Q&A.

Runs a natural-language question through the search pipeline:
  1. VaultIndex finds top-k matching files in ~/.council/vault
  2. We load the matched file content as context
  3. IBM Granite (via llama-cpp-python) answers the question with
     instructions to cite the file paths in its response

Demonstrates the "search function" claim end-to-end: ranked retrieval
+ grounded generation, all local, all GPU.

Usage:
    .venv\\Scripts\\python.exe demo_search_qa.py
"""
from __future__ import annotations

import _windll_bootstrap  # noqa: F401

import os
import sys
import time
from pathlib import Path

VAULT = Path.home() / ".council" / "vault"
GGUF  = Path(__file__).parent / "models" / "granite-3.1-8b-instruct-Q4_K_M.gguf"

CONTEXT_PER_HIT_CHARS = 1500
MAX_CONTEXT_HITS = 4


def _hdr(s: str) -> None:
    print()
    print("=" * 78)
    print(f"  {s}")
    print("=" * 78)


def _safe_read(path: Path, limit: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"[read error: {exc!r}]"
    if len(text) > limit:
        head = text[: limit // 2]
        tail = text[-limit // 2 :]
        text = head + "\n... [truncated] ...\n" + tail
    return text


def build_prompt(question: str, hits) -> str:
    sources = []
    for rank, (score, rec) in enumerate(hits[:MAX_CONTEXT_HITS], 1):
        path = Path(rec.get("path") or rec.get("relpath") or "")
        try:
            rel = path.relative_to(VAULT)
        except Exception:
            rel = path
        body = _safe_read(path, CONTEXT_PER_HIT_CHARS) if path.exists() else "[file not on disk]"
        sources.append(f"[SOURCE #{rank}] {rel}  (score={score:.2f})\n{body}\n")
    src_block = "\n---\n".join(sources)
    return (
        "You are answering a question grounded in the user's local vault. "
        "Use ONLY the sources below. If they don't answer the question, say so. "
        "Cite the source path inline like [SOURCE #N] when you use it.\n\n"
        f"SOURCES:\n---\n{src_block}\n---\n\n"
        f"QUESTION: {question}\n\nANSWER:"
    )


def main() -> int:
    if not VAULT.exists():
        print(f"vault not found: {VAULT}", file=sys.stderr)
        return 1
    if not GGUF.exists():
        print(f"GGUF not found: {GGUF}", file=sys.stderr)
        return 1

    _hdr(f"Vault: {VAULT}")
    print(f"Model: {GGUF.name}")

    from vault_index import VaultIndex
    print("loading index...")
    t0 = time.time()
    idx = VaultIndex(VAULT)
    if len(idx.records) == 0:
        idx.rebuild()
    print(f"  {len(idx.records)} records   ({time.time()-t0:.2f}s)")

    print("loading Granite...")
    t0 = time.time()
    from llama_cpp import Llama
    llm = Llama(model_path=str(GGUF), n_gpu_layers=99, n_ctx=8192, verbose=False)
    print(f"  loaded in {time.time()-t0:.2f}s")

    questions = [
        "Which files in this vault implement the GPT transformer training loop?",
        "What does the axolotl repo provide for fine-tuning Llama-3?",
        "Where are the LLMs-from-scratch chapter READMEs and what do they cover?",
    ]

    for q in questions:
        _hdr(f"Q: {q}")
        t0 = time.time()
        hits, _ = idx.search(q, k=MAX_CONTEXT_HITS)
        print(f"  retrieved {len(hits)} hits in {(time.time()-t0)*1000:.0f} ms:")
        for rank, (score, rec) in enumerate(hits[:MAX_CONTEXT_HITS], 1):
            path = rec.get("path") or rec.get("relpath") or "?"
            try:
                rel = str(Path(path).relative_to(VAULT))
            except Exception:
                rel = str(path)
            print(f"    {rank}. [{score:6.2f}]  {rel}")

        if not hits:
            print("  (no hits — skipping LLM)")
            continue

        prompt = build_prompt(q, hits)
        t0 = time.time()
        try:
            out = llm.create_chat_completion(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=350,
            )
        except Exception as exc:
            print(f"  LLM error: {exc!r}")
            continue
        dt = time.time() - t0
        answer = out["choices"][0]["message"]["content"].strip()
        print(f"\n  --- Granite ({dt:.1f}s) ---")
        for line in answer.splitlines():
            print(f"  {line}")

    _hdr("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
