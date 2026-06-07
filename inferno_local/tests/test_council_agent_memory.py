"""Council ↔ ConversationLog integration test.

Default is OFF: no record is appended after local_chat. With the env
flag on, a council_deliberation record is appended.

We monkeypatch _gguf_chat so the test never loads a real model.
"""
from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_logs


class TestAgentMemoryToggle(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        # Point the vault at our temp dir so we don't pollute the real one.
        self._prev_vault = os.environ.get("COUNCIL_VAULT_ROOT")
        os.environ["COUNCIL_VAULT_ROOT"] = self.td.name
        # And snapshot the agent_memory flag so we restore it later.
        self._prev_flag = os.environ.get("COUNCIL_AGENT_MEMORY_ENABLE")
        os.environ.pop("COUNCIL_AGENT_MEMORY_ENABLE", None)

    def tearDown(self):
        if self._prev_vault is None:
            os.environ.pop("COUNCIL_VAULT_ROOT", None)
        else:
            os.environ["COUNCIL_VAULT_ROOT"] = self._prev_vault
        if self._prev_flag is None:
            os.environ.pop("COUNCIL_AGENT_MEMORY_ENABLE", None)
        else:
            os.environ["COUNCIL_AGENT_MEMORY_ENABLE"] = self._prev_flag
        self.td.cleanup()

    def _patch_gguf(self, reply: str = "hello back"):
        # Monkeypatch _gguf_chat so we never load the GGUF model.
        import council_engine
        return mock.patch.object(
            council_engine, "_gguf_chat", return_value=reply, autospec=True)

    def test_default_off_does_not_record(self):
        import council_engine
        log_path = Path(self.td.name) / ".agent_runs.jsonl"
        self.assertFalse(log_path.exists())
        with self._patch_gguf("answer"):
            ans = council_engine.local_chat(
                [{"role": "user", "content": "what is up"}],
                temperature=0.2, num_predict=50,
            )
        self.assertEqual(ans, "answer")
        # Default OFF → no log file created.
        self.assertFalse(log_path.exists(),
                          f"unexpected log: {log_path}")

    def test_toggle_on_records_council_deliberation(self):
        import council_engine
        council_engine.set_agent_memory_enabled(True)
        with self._patch_gguf("Q3 was great"):
            ans = council_engine.local_chat(
                [
                    {"role": "system", "content": "you are helpful"},
                    {"role": "user", "content": "summarise Q3"},
                ],
                temperature=0.2, num_predict=50,
            )
        self.assertEqual(ans, "Q3 was great")
        # And the log should now exist with a council_deliberation record.
        log = agent_logs.ConversationLog.default()
        rows = log.all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "council_deliberation")
        self.assertEqual(rows[0]["task"], "summarise Q3")
        self.assertEqual(rows[0]["final_answer"], "Q3 was great")
        self.assertEqual(rows[0]["outcome"], "done")

    def test_set_helper_round_trips(self):
        import council_engine
        # Off → on → off via the helper.
        council_engine.set_agent_memory_enabled(False)
        self.assertFalse(council_engine._agent_memory_enabled())
        council_engine.set_agent_memory_enabled(True)
        self.assertTrue(council_engine._agent_memory_enabled())
        council_engine.set_agent_memory_enabled(False)
        self.assertFalse(council_engine._agent_memory_enabled())

    def test_record_failure_does_not_propagate(self):
        """Even if the log write blows up, local_chat still returns the
        answer (we never want to fail a chat call over an audit record)."""
        import council_engine
        council_engine.set_agent_memory_enabled(True)
        with self._patch_gguf("hello"):
            with mock.patch.object(
                agent_logs.ConversationLog, "append",
                side_effect=RuntimeError("disk full"),
            ):
                ans = council_engine.local_chat(
                    [{"role": "user", "content": "?"}],
                    temperature=0.2, num_predict=10,
                )
        self.assertEqual(ans, "hello")


if __name__ == "__main__":
    unittest.main()
