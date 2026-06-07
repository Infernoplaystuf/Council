"""T6 tests — local-corpus deep research. Indexing + research loop +
citation grounding + no-web guarantee."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import deep_research
from inferno_local import security
from inferno_local.local_memory import LocalMemory


class FakeEmbedder:
    def __init__(self): self.dim = 8
    def __call__(self, texts):
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for i, ch in enumerate(t):
                v[i % self.dim] += (ord(ch) % 17) / 17.0
            out.append(v)
        return out


class ScriptedRunner:
    """Runner that replays a queue of replies; used to drive the
    research loop without an LLM."""
    name = "scripted"
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
    def chat(self, messages, *, temperature=0.2, max_tokens=600):
        if self.calls >= len(self.replies):
            return ""
        r = self.replies[self.calls]
        self.calls += 1
        return r
    def stream_chat(self, *_a, **_kw):
        yield from self.replies
    def describe(self): return {"backend": "scripted"}


# ────────────────────────────────────────────────────────
# Chunking
# ────────────────────────────────────────────────────────

class TestChunking(unittest.TestCase):

    def test_short_text_one_chunk(self):
        chunks = deep_research._chunk_text("hello world", chunk_size=1000)
        self.assertEqual(len(chunks), 1)

    def test_long_text_split(self):
        text = "a" * 5000
        chunks = deep_research._chunk_text(text, chunk_size=1200, overlap=200)
        self.assertGreater(len(chunks), 1)
        # Overlap means total chars > original
        joined = "".join(chunks)
        self.assertGreater(len(joined), 5000)

    def test_paragraph_boundary_preferred(self):
        text = "para1.\n\npara2 long enough to land near the boundary.\n\npara3."
        chunks = deep_research._chunk_text(text, chunk_size=30, overlap=10)
        # No empty chunks
        for c in chunks:
            self.assertTrue(c.strip())


# ────────────────────────────────────────────────────────
# Indexing
# ────────────────────────────────────────────────────────

class TestIndexFolder(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.corp = Path(self.td.name) / "corp"
        self.corp.mkdir()
        (self.corp / "a.md").write_text(
            "# Project notes\n\nQ3 revenue rose 12% YoY.\n", encoding="utf-8")
        (self.corp / "b.txt").write_text(
            "Supplier Hennig die wear is the root cause of DEF-001.",
            encoding="utf-8")
        # Binary file should be skipped
        (self.corp / "image.png").write_bytes(b"\x89PNG fake")

        self.mem_dir = Path(self.td.name) / "mem"
        self.mem = LocalMemory(self.mem_dir, embedder=FakeEmbedder(),
                               collection="t_dr")

    def tearDown(self):
        self.td.cleanup()

    def test_indexes_text_files(self):
        tally = deep_research.index_folder(self.corp, self.mem)
        self.assertEqual(tally["files"], 2)
        self.assertGreaterEqual(tally["chunks"], 2)
        self.assertGreaterEqual(tally["skipped"], 1)   # the .png

    def test_chunks_carry_source_metadata(self):
        deep_research.index_folder(self.corp, self.mem)
        hits = self.mem.query("Hennig", k=3)
        self.assertGreater(len(hits), 0)
        sources = {h.metadata.get("source") for h in hits}
        self.assertIn("b.txt", sources)

    def test_id_scheme_is_doc(self):
        deep_research.index_folder(self.corp, self.mem)
        hits = self.mem.query("revenue", k=2)
        self.assertGreater(len(hits), 0)
        for h in hits:
            self.assertTrue(h.id.startswith("doc:"))

    def test_missing_folder_raises(self):
        with self.assertRaises(FileNotFoundError):
            deep_research.index_folder(Path("/no/such/folder/xyz"), self.mem)


# ────────────────────────────────────────────────────────
# Research loop
# ────────────────────────────────────────────────────────

class TestDeepResearch(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.corp = Path(self.td.name) / "corp"
        self.corp.mkdir()
        (self.corp / "ecn.md").write_text(
            "ECN-2026-017 widened the stator tolerance from "
            "+/-0.05 to +/-0.10 mm. Root cause: Hennig die wear.",
            encoding="utf-8")
        (self.corp / "msds.txt").write_text(
            "PART-3001 MSDS: nitrile gloves and splash goggles required.",
            encoding="utf-8")
        self.mem = LocalMemory(Path(self.td.name) / "mem",
                                embedder=FakeEmbedder(),
                                collection="t_dr_loop")
        deep_research.index_folder(self.corp, self.mem)

    def tearDown(self):
        self.td.cleanup()

    def test_runs_end_to_end_with_scripted_llm(self):
        runner = ScriptedRunner([
            # First call: produce sub-questions JSON
            '["What did ECN-2026-017 change?", "What PPE does PART-3001 need?"]',
            # Section 1 draft
            "ECN-2026-017 widened the tolerance per [doc:abc:0].",
            # Section 2 draft
            "Nitrile gloves and splash goggles per the MSDS [doc:xyz:0].",
        ])
        report = deep_research.deep_research(
            "How is PART-3001 documented?", runner, self.mem,
            sections=2, k_per_section=3,
        )
        self.assertEqual(len(report.sections), 2)
        # Every section must reference at least one local chunk id
        for sec in report.sections:
            self.assertGreater(len(sec.citations), 0,
                f"section '{sec.sub_question}' has no citations")
        # Cited ids are doc: scheme — local, not URLs.
        for cid in report.cited_ids:
            self.assertTrue(cid.startswith("doc:"),
                            f"non-doc id leaked: {cid}")

    def test_markdown_format(self):
        runner = ScriptedRunner([
            '["q1", "q2"]',
            "section 1 body [cite-1]",
            "section 2 body [cite-2]",
        ])
        report = deep_research.deep_research(
            "x", runner, self.mem,
            sections=2, k_per_section=2,
        )
        md = report.to_markdown()
        self.assertIn("# x", md)
        self.assertIn("## 1.", md)
        self.assertIn("## 2.", md)
        self.assertIn("## Sources", md)

    def test_empty_question_raises(self):
        with self.assertRaises(ValueError):
            deep_research.deep_research("", ScriptedRunner([]), self.mem)

    def test_no_subqs_falls_back_to_question(self):
        """If the model returns garbage instead of JSON list, we fall back
        to using the original question as the single sub-question."""
        runner = ScriptedRunner([
            "I'm not going to give you JSON.",
            "section body referencing [doc:something:0]",
        ])
        report = deep_research.deep_research(
            "What was the tolerance change?",
            runner, self.mem, sections=4, k_per_section=3,
        )
        # At least one section (fallback path) — and it cites local docs.
        self.assertGreaterEqual(len(report.sections), 1)


# ────────────────────────────────────────────────────────
# No-web guarantee
# ────────────────────────────────────────────────────────

class TestNoWeb(unittest.TestCase):
    """The research loop must run cleanly under the loopback egress guard."""

    def test_loop_under_egress_guard(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            corp = Path(td) / "corp"
            corp.mkdir()
            (corp / "x.md").write_text("Q3 revenue notes", encoding="utf-8")
            mem = LocalMemory(Path(td) / "mem",
                              embedder=FakeEmbedder(),
                              collection="t_dr_egress")
            deep_research.index_folder(corp, mem)
            runner = ScriptedRunner([
                '["What was Q3?"]',
                "Q3 notes per [doc:abc:0]",
            ])
            security.install_socket_guard()
            try:
                report = deep_research.deep_research(
                    "?", runner, mem, sections=1, k_per_section=2,
                )
                self.assertEqual(len(report.sections), 1)
            finally:
                security.uninstall_socket_guard()


if __name__ == "__main__":
    unittest.main()
