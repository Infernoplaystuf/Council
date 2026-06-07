"""Tests for inferno_local.cookbook — no-network hardware + model survey."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from inferno_local import cookbook


class TestDescribeShape(unittest.TestCase):

    def test_describe_returns_expected_keys(self):
        d = cookbook.describe()
        for key in ("os", "python", "cpu", "ram_gb", "gpus",
                    "cuda_runtime", "models_dir", "models_on_disk",
                    "catalog_size"):
            self.assertIn(key, d, f"missing key: {key}")
        self.assertIsInstance(d["gpus"], list)
        self.assertIsInstance(d["models_on_disk"], list)


class TestFitVerdict(unittest.TestCase):

    def _spec(self, vram_gb_q4: float):
        # Minimal spec stand-in matching model_catalog.ModelSpec attrs we read
        class S:
            pass
        s = S()
        s.vram_gb_q4 = vram_gb_q4
        return s

    def test_clean_fit(self):
        v = cookbook.fit_verdict(self._spec(6.5), 16.0)
        self.assertEqual(v["fit"], "clean")

    def test_tight_fit(self):
        # need 11 GB + 1.5 headroom = 12.5; with 12 GB available -> tight
        v = cookbook.fit_verdict(self._spec(11.0), 12.0)
        self.assertEqual(v["fit"], "tight")

    def test_oom(self):
        v = cookbook.fit_verdict(self._spec(24.0), 16.0)
        self.assertEqual(v["fit"], "oom")

    def test_no_gpu(self):
        v = cookbook.fit_verdict(self._spec(6.5), None)
        self.assertEqual(v["fit"], "cpu-only")


class TestBestQuant(unittest.TestCase):

    def test_known_id(self):
        out = cookbook.best_quant("granite-3.1-8b-q4", vram_gb=16.0)
        self.assertEqual(out["model_id"], "granite-3.1-8b-q4")
        self.assertEqual(out["quant"], "Q4_K_M")
        self.assertIn(out["verdict"]["fit"], ("clean", "tight"))

    def test_unknown_id(self):
        out = cookbook.best_quant("not-a-real-id-xyz")
        self.assertIn("error", out)


class TestScanModelsDir(unittest.TestCase):

    def test_empty_dir(self):
        with tempfile.TemporaryDirectory() as td:
            out = cookbook.scan_models_dir(Path(td))
            self.assertEqual(out, [])

    def test_matches_catalog_filename(self):
        # Create a file with the exact filename of a catalog spec.
        import model_catalog as mc
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / mc.MODELS[0].hf_file
            target.write_bytes(b"x" * 1024)  # tiny fake gguf
            out = cookbook.scan_models_dir(Path(td))
            self.assertEqual(len(out), 1)
            self.assertTrue(out[0]["matched"])
            self.assertEqual(out[0]["model_id"], mc.MODELS[0].id)

    def test_sideloaded_unmatched(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "my-custom-model.gguf").write_bytes(b"x" * 1024)
            out = cookbook.scan_models_dir(Path(td))
            self.assertEqual(len(out), 1)
            self.assertFalse(out[0]["matched"])

    def test_missing_dir(self):
        out = cookbook.scan_models_dir(Path("/nonexistent/path/xyz"))
        self.assertEqual(out, [])


class TestNoNetwork(unittest.TestCase):
    """describe() must not touch the network. We install the egress guard
    and verify it doesn't trip."""

    def test_describe_does_not_open_socket(self):
        from inferno_local import security
        security.install_socket_guard()
        try:
            d = cookbook.describe()
            self.assertIsInstance(d, dict)
        finally:
            security.uninstall_socket_guard()


if __name__ == "__main__":
    unittest.main()
