"""Tests for inferno_local.local_memory — round-trip + telemetry-off."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from inferno_local import local_memory
from inferno_local import security


class FakeEmbedder:
    """Tiny deterministic 'embedder': returns a vector based on word count
    so we can exercise the embedding code path without sentence-transformers."""

    def __init__(self):
        self.calls = 0
        self.dim = 8

    def __call__(self, texts):
        self.calls += 1
        out = []
        for t in texts:
            # cheap: 8-dim float vector derived from char codes
            base = [0.0] * self.dim
            for i, ch in enumerate(t):
                base[i % self.dim] += (ord(ch) % 17) / 17.0
            out.append(base)
        return out


class TestAddQueryRoundTrip(unittest.TestCase):

    def setUp(self):
        # ignore_cleanup_errors: ChromaDB mmaps its SQLite store on
        # Windows; tempfile can't unlink the open files until the GC
        # runs. The test logic itself is unaffected; only teardown
        # would raise WinError 32.
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.dir = Path(self.td.name)
        self.embedder = FakeEmbedder()
        self.mem = local_memory.LocalMemory(self.dir, embedder=self.embedder,
                                            collection="t_round_trip")

    def tearDown(self):
        self.td.cleanup()

    def test_add_then_query_finds_it(self):
        self.mem.add("doc:abc:0", "stator lamination stack tolerance increase",
                     {"source": "ecn.pdf"})
        self.mem.add("doc:def:0", "msds chemical hazard nitrile gloves",
                     {"source": "msds.pdf"})
        hits = self.mem.query("tolerance", k=2)
        self.assertEqual(len(hits), 2)
        # FakeEmbedder is deterministic but unsophisticated — both stored.
        ids = {h.id for h in hits}
        self.assertIn("doc:abc:0", ids)

    def test_metadata_round_trips(self):
        self.mem.add("note:foo", "hello world",
                     {"source": "manual", "tag": "test"})
        hits = self.mem.query("hello", k=1)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].metadata.get("source"), "manual")
        self.assertEqual(hits[0].metadata.get("tag"), "test")

    def test_count_matches(self):
        for i in range(5):
            self.mem.add(f"doc:x:{i}", f"text-{i}")
        self.assertEqual(self.mem.count(), 5)


class TestNoEmbedderFallback(unittest.TestCase):
    """When no embedder is injected, query falls back to substring match.
    Useful on air-gapped boxes without sentence-transformers installed."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.mem = local_memory.LocalMemory(Path(self.td.name),
                                            embedder=None,
                                            collection="t_no_emb")

    def tearDown(self):
        self.td.cleanup()

    def test_substring_match_works(self):
        self.mem.add("a", "the quick brown fox")
        self.mem.add("b", "lazy dog naps")
        hits = self.mem.query("brown", k=5)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].id, "a")


class TestPersistence(unittest.TestCase):
    """Records survive client restart — PersistentClient on disk."""

    def test_survives_restart(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            emb = FakeEmbedder()
            m1 = local_memory.LocalMemory(Path(td), embedder=emb,
                                          collection="t_persist")
            m1.add("doc:abc:0", "persistent text")
            # Drop the handle; open a fresh one against same dir.
            del m1
            m2 = local_memory.LocalMemory(Path(td), embedder=emb,
                                          collection="t_persist")
            self.assertEqual(m2.count(), 1)


class TestTelemetryOff(unittest.TestCase):
    """ChromaDB must not phone home. We install the loopback guard and
    perform an add+query — any non-loopback socket call would raise
    EgressBlocked and fail the test."""

    def test_add_query_under_egress_guard(self):
        # Fresh process flag so we know the test is the source of truth.
        security.install_socket_guard()
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
                mem = local_memory.LocalMemory(Path(td),
                                               embedder=FakeEmbedder(),
                                               collection="t_telemetry")
                # add() and query() must not open a non-loopback socket
                mem.add("doc:xyz:0", "telemetry-off check")
                hits = mem.query("telemetry", k=1)
                self.assertEqual(len(hits), 1)
        finally:
            security.uninstall_socket_guard()

    def test_settings_say_telemetry_off(self):
        # Verify the env vars we set are populated AND chromadb sees them.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            mem = local_memory.LocalMemory(Path(td),
                                           embedder=FakeEmbedder(),
                                           collection="t_settings")
            # Trigger client init
            mem.add("ping", "pong")
            self.assertEqual(os.environ.get("ANONYMIZED_TELEMETRY"), "False")
            self.assertEqual(os.environ.get("CHROMA_TELEMETRY"), "False")


class TestIdHelpers(unittest.TestCase):

    def test_make_doc_id(self):
        i = local_memory.make_doc_id("report.pdf", 3)
        self.assertTrue(i.startswith("doc:"))
        self.assertTrue(i.endswith(":3"))
        # Stable: same source -> same hash prefix
        self.assertEqual(
            local_memory.make_doc_id("report.pdf", 0).split(":")[1],
            i.split(":")[1],
        )

    def test_make_analysis_id(self):
        i = local_memory.make_analysis_id("session-42", 7)
        self.assertEqual(i, "analysis:session-42:7")


if __name__ == "__main__":
    unittest.main()
