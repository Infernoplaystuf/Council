"""Tests for inferno_local.model_runner — the factory + Ollama loopback guard."""
from __future__ import annotations

import unittest
from unittest import mock

from inferno_local import model_runner, security


class TestBuildRunnerCloudBlocked(unittest.TestCase):
    """Every cloud backend keyword must raise EgressBlocked at the factory."""

    def test_openai_blocked(self):
        with self.assertRaises(security.EgressBlocked):
            model_runner.build_runner({"backend": "openai"})

    def test_anthropic_blocked(self):
        with self.assertRaises(security.EgressBlocked):
            model_runner.build_runner({"backend": "anthropic"})

    def test_gemini_blocked(self):
        with self.assertRaises(security.EgressBlocked):
            model_runner.build_runner({"backend": "gemini"})

    def test_openrouter_blocked(self):
        with self.assertRaises(security.EgressBlocked):
            model_runner.build_runner({"backend": "openrouter"})

    def test_copilot_blocked(self):
        with self.assertRaises(security.EgressBlocked):
            model_runner.build_runner({"backend": "copilot"})

    def test_azure_openai_blocked(self):
        with self.assertRaises(security.EgressBlocked):
            model_runner.build_runner({"backend": "azure_openai"})

    def test_groq_blocked(self):
        with self.assertRaises(security.EgressBlocked):
            model_runner.build_runner({"backend": "groq"})

    def test_unknown_backend_raises_value_error(self):
        with self.assertRaises(ValueError):
            model_runner.build_runner({"backend": "magical-new-thing"})

    def test_missing_backend_raises_value_error(self):
        with self.assertRaises(ValueError):
            model_runner.build_runner({})


class TestOllamaRunnerLoopback(unittest.TestCase):
    """OllamaRunner must refuse non-loopback URLs at construction AND on calls."""

    def test_rejects_public_url_at_construct(self):
        with self.assertRaises(security.EgressBlocked):
            model_runner.OllamaRunner(
                url="http://api.openai.com:443",
                model="anything",
            )

    def test_rejects_private_lan_at_construct(self):
        # Even your office's intranet Ollama is not allowed.
        with self.assertRaises(security.EgressBlocked):
            model_runner.OllamaRunner(
                url="http://10.0.0.5:11434",
                model="anything",
            )

    def test_accepts_127_loopback(self):
        # Constructs fine; the real chat call would need a daemon.
        r = model_runner.OllamaRunner(
            url="http://127.0.0.1:11434",
            model="granite",
        )
        self.assertEqual(r.url, "http://127.0.0.1:11434")
        self.assertEqual(r.model, "granite")

    def test_accepts_localhost(self):
        r = model_runner.OllamaRunner(
            url="http://localhost:11434",
            model="granite",
        )
        self.assertEqual(r.model, "granite")

    def test_missing_model_raises(self):
        with self.assertRaises(ValueError):
            model_runner.OllamaRunner(url="http://localhost:11434", model="")

    def test_chat_revalidates_loopback(self):
        """If somebody mutates self.url after construction, calls still
        re-check. (Belt-and-braces — config is dict-typed so mutation is
        a real possibility.)"""
        r = model_runner.OllamaRunner(url="http://localhost:11434", model="x")
        r.url = "http://api.openai.com"
        with self.assertRaises(security.EgressBlocked):
            r.chat([{"role": "user", "content": "hi"}])


class TestBuildRunnerOllamaPath(unittest.TestCase):

    def test_ollama_factory_routes_to_runner(self):
        r = model_runner.build_runner({
            "backend": "ollama",
            "url":     "http://127.0.0.1:11434",
            "model":   "granite3.1-dense:8b",
        })
        self.assertIsInstance(r, model_runner.OllamaRunner)
        self.assertEqual(r.model, "granite3.1-dense:8b")

    def test_ollama_factory_blocks_public_url(self):
        with self.assertRaises(security.EgressBlocked):
            model_runner.build_runner({
                "backend": "ollama",
                "url":     "http://api.openai.com",
                "model":   "foo",
            })


if __name__ == "__main__":
    unittest.main()
