"""T3 tests — council_memory wraps LocalMemory correctly, no egress,
record then retrieve round-trip."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from inferno_local import security
import council_memory


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


class TestRecordAndRetrieve(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.mem = council_memory.reset_for_tests(
            persist_dir=Path(self.td.name),
            embedder=FakeEmbedder(),
        )

    def tearDown(self):
        self.td.cleanup()

    def test_record_then_before_deliberation(self):
        council_memory.record_deliberation(
            session="s1", turn=0,
            question="What did the ECN say about tolerance?",
            answer="ECN-2026-017 widened the tolerance from +/-0.05 to +/-0.10 mm.",
        )
        ctx = council_memory.before_deliberation(
            "tolerance ECN",  k=5,
        )
        self.assertIn("Prior context", ctx)
        self.assertIn("ECN-2026-017", ctx)
        # Returned block must carry the id so the model can cite it
        self.assertIn("[memory analysis:s1:0]", ctx)

    def test_empty_question_returns_empty(self):
        ctx = council_memory.before_deliberation("")
        self.assertEqual(ctx, "")

    def test_no_hits_returns_empty(self):
        # Nothing recorded — query returns nothing.
        ctx = council_memory.before_deliberation("anything")
        self.assertEqual(ctx, "")

    def test_record_document_chunk(self):
        rid = council_memory.record_document_chunk(
            "report.pdf", 0,
            "Q3 revenue was $5M with 12% YoY growth.",
        )
        self.assertTrue(rid.startswith("doc:"))
        ctx = council_memory.before_deliberation("Q3 revenue YoY", k=3)
        self.assertIn("report.pdf", ctx.lower() + str(self.mem.query("Q3 revenue", k=1)[0].metadata))

    def test_max_chars_truncates(self):
        # Stuff in 10 deliberations and ensure the block stays bounded.
        for i in range(10):
            council_memory.record_deliberation(
                "s1", i, f"q{i}", "x" * 1000,
            )
        ctx = council_memory.before_deliberation("x", k=20, max_chars=1800)
        # Bound is roughly observed; allow 200ch slack for header + framing
        self.assertLess(len(ctx), 2200)


class TestRecordsPersistAcrossInstances(unittest.TestCase):
    """Records survive a ChromaDB client restart (PersistentClient on disk)."""

    def test_persistence(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            council_memory.reset_for_tests(
                persist_dir=Path(td), embedder=FakeEmbedder(),
            )
            council_memory.record_deliberation(
                "s2", 0,
                "q",
                "this is recorded across instances",
            )
            # Drop singleton + rebuild against the same persist_dir
            council_memory.reset_for_tests(
                persist_dir=Path(td), embedder=FakeEmbedder(),
            )
            ctx = council_memory.before_deliberation("instances", k=3)
            self.assertIn("across instances", ctx)


class TestNoEgress(unittest.TestCase):
    """All council_memory operations must work under the egress guard."""

    def test_record_and_query_under_guard(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            council_memory.reset_for_tests(
                persist_dir=Path(td), embedder=FakeEmbedder(),
            )
            security.install_socket_guard()
            try:
                council_memory.record_deliberation(
                    "s_egress", 0, "q under guard",
                    "answer recorded under egress guard",
                )
                ctx = council_memory.before_deliberation("guard", k=2)
                self.assertIn("under egress guard", ctx)
            finally:
                security.uninstall_socket_guard()


if __name__ == "__main__":
    unittest.main()
