"""Integration tests for Odysseus T1+T2 — get_runner wired into council_engine,
system_panel.snapshot, and end-to-end factory behaviour."""
from __future__ import annotations

import os
import unittest

from inferno_local import model_runner, security


class TestCouncilEngineGetRunner(unittest.TestCase):
    """council_engine.get_runner returns an inferno_local ModelRunner.

    We don't actually load the GGUF here (slow + needs the model file)
    — we just verify the factory wiring and that cloud backends bounce."""

    def test_default_returns_llama_cpp_runner(self):
        # Don't trigger model load: pass a config that is structurally valid
        # but with a bogus gguf_path so .chat() would fail. We only care
        # the FACTORY returns a LlamaCppRunner instance.
        # Save env so we don't disturb other tests / the running app.
        prev = os.environ.get("COUNCIL_GGUF_PATH")
        try:
            os.environ["COUNCIL_GGUF_PATH"] = "/nonexistent/test.gguf"
            import council_engine
            r = council_engine.get_runner()
            self.assertIsInstance(r, model_runner.LlamaCppRunner)
            self.assertEqual(r.name, "llama_cpp")
        finally:
            if prev is None:
                os.environ.pop("COUNCIL_GGUF_PATH", None)
            else:
                os.environ["COUNCIL_GGUF_PATH"] = prev

    def test_cloud_backend_via_get_runner_blocked(self):
        import council_engine
        with self.assertRaises(security.EgressBlocked):
            council_engine.get_runner({"backend": "openai"})


class TestSystemPanelSnapshot(unittest.TestCase):
    """The snapshot used by the System panel must be pure-data and
    side-effect-free apart from local hardware probing."""

    def test_snapshot_keys(self):
        import system_panel
        snap = system_panel.snapshot()
        self.assertIn("hardware", snap)
        self.assertIn("models", snap)
        self.assertIn("primary_vram_gb", snap)
        self.assertGreater(len(snap["models"]), 0)
        # Every catalog entry must have a verdict.
        for m in snap["models"]:
            self.assertIn("verdict", m)
            self.assertIn(m["verdict"]["fit"],
                          ("clean", "tight", "oom", "cpu-only"))

    def test_render_cli_does_not_explode(self):
        import system_panel
        snap = system_panel.snapshot()
        s = system_panel.render_cli(snap)
        self.assertIsInstance(s, str)
        self.assertIn("Hardware", s)
        self.assertIn("Catalog", s)


class TestNoEgressDuringSnapshot(unittest.TestCase):
    """snapshot() must not phone home. Egress guard verifies."""

    def test_snapshot_under_egress_guard(self):
        import system_panel
        security.install_socket_guard()
        try:
            snap = system_panel.snapshot()
            self.assertIn("hardware", snap)
        finally:
            security.uninstall_socket_guard()


if __name__ == "__main__":
    unittest.main()
