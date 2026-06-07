"""Tests for agent_logs — append-only JSONL conversation + gap stores."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

import agent_logs
import safe_agent
from inferno_local import security


class TestConversationLog(unittest.TestCase):

    def test_empty_file_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as td:
            log = agent_logs.ConversationLog(Path(td) / "nope.jsonl")
            self.assertEqual(log.all(), [])

    def test_append_then_read(self):
        with tempfile.TemporaryDirectory() as td:
            log = agent_logs.ConversationLog(Path(td) / "runs.jsonl")
            rec = log.append(
                task="hello",
                final_answer="hi",
                outcome="done",
                tools_used=["a"],
                tools_missing=[],
                step_count=1,
            )
            self.assertIn("ts", rec)
            self.assertEqual(rec["task"], "hello")
            rows = log.all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["final_answer"], "hi")
            self.assertEqual(rows[0]["outcome"], "done")
            self.assertEqual(rows[0]["kind"], "agent_run")

    def test_append_run_from_agent_run_object(self):
        run = safe_agent.AgentRun(
            task="t", final_answer="a",
            stopped_reason="done",
            tools_used=["read_local_file"], tools_missing=[],
        )
        with tempfile.TemporaryDirectory() as td:
            log = agent_logs.ConversationLog(Path(td) / "runs.jsonl")
            log.append_run(run)
            rows = log.all()
            self.assertEqual(rows[0]["outcome"], "done")
            self.assertEqual(rows[0]["tools_used"], ["read_local_file"])

    def test_council_kind(self):
        with tempfile.TemporaryDirectory() as td:
            log = agent_logs.ConversationLog(Path(td) / "runs.jsonl")
            log.append(
                task="q", final_answer="a", outcome="done",
                kind="council_deliberation",
                session="s-42",
            )
            rows = log.all()
            self.assertEqual(rows[0]["kind"], "council_deliberation")
            self.assertEqual(rows[0]["session"], "s-42")

    def test_append_only_never_rewrites(self):
        with tempfile.TemporaryDirectory() as td:
            log = agent_logs.ConversationLog(Path(td) / "runs.jsonl")
            log.append(task="a", final_answer="x", outcome="done")
            first = log.all()
            log.append(task="b", final_answer="y", outcome="done")
            both = log.all()
            self.assertEqual(len(both), 2)
            # First record bytes-identical to what we appended initially
            self.assertEqual(first[0]["task"], "a")
            self.assertEqual(both[0]["task"], "a")
            self.assertEqual(both[1]["task"], "b")

    def test_corrupt_line_skipped_not_crashed(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "runs.jsonl"
            p.write_text("not json at all\n"
                          + json.dumps({"ts": 1, "task": "ok",
                                        "final_answer": "yes",
                                        "outcome": "done",
                                        "tools_used": [], "tools_missing": [],
                                        "step_count": 0, "kind": "agent_run"})
                          + "\n",
                          encoding="utf-8")
            log = agent_logs.ConversationLog(p)
            rows = log.all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["task"], "ok")


class TestToolGapLog(unittest.TestCase):

    def test_args_shape_normalisation(self):
        with tempfile.TemporaryDirectory() as td:
            log = agent_logs.ToolGapLog(Path(td) / "gaps.jsonl")
            log.append(
                requested_name="send_email",
                args={"to": "ops@example.com",
                      "subject": "Q3 review",
                      "attach_count": 3},
                task="email me Q3",
                step=2,
            )
            rec = log.all()[0]
            self.assertEqual(rec["requested_name"], "send_email")
            # Shape carries TYPE names, not values
            self.assertEqual(
                rec["args_shape"],
                {"to": "str", "subject": "str", "attach_count": "int"},
            )
            # Sample carries truncated values
            self.assertEqual(rec["args_sample"]["to"], "ops@example.com")

    def test_args_sample_truncated(self):
        with tempfile.TemporaryDirectory() as td:
            log = agent_logs.ToolGapLog(Path(td) / "gaps.jsonl")
            log.append(
                requested_name="upload",
                args={"blob": "x" * 5000},
                task="upload a file",
            )
            rec = log.all()[0]
            self.assertLess(len(rec["args_sample"]["blob"]), 300)
            self.assertTrue(rec["args_sample"]["blob"].endswith("…"))

    def test_empty_args_ok(self):
        with tempfile.TemporaryDirectory() as td:
            log = agent_logs.ToolGapLog(Path(td) / "gaps.jsonl")
            log.append(requested_name="ping", args={}, task="ping it")
            rec = log.all()[0]
            self.assertEqual(rec["args_shape"], {})
            self.assertEqual(rec["args_sample"], {})

    def test_context_passthrough(self):
        with tempfile.TemporaryDirectory() as td:
            log = agent_logs.ToolGapLog(Path(td) / "gaps.jsonl")
            log.append(requested_name="x", args={}, task="t",
                       context={"session": "s1", "role": "writer"})
            rec = log.all()[0]
            self.assertEqual(rec["context"]["session"], "s1")


class TestVaultRootEnv(unittest.TestCase):

    def test_env_override_respected(self):
        with tempfile.TemporaryDirectory() as td:
            prev = os.environ.get("COUNCIL_VAULT_ROOT")
            os.environ["COUNCIL_VAULT_ROOT"] = td
            try:
                log = agent_logs.ConversationLog()
                log.append(task="t", final_answer="a", outcome="done")
                expected = Path(td) / ".agent_runs.jsonl"
                self.assertTrue(expected.exists())
            finally:
                if prev is None:
                    os.environ.pop("COUNCIL_VAULT_ROOT", None)
                else:
                    os.environ["COUNCIL_VAULT_ROOT"] = prev


class TestNoEgress(unittest.TestCase):

    def test_append_under_socket_guard(self):
        """No network call during log append. Egress guard verifies."""
        with tempfile.TemporaryDirectory() as td:
            convo = agent_logs.ConversationLog(Path(td) / "runs.jsonl")
            gap = agent_logs.ToolGapLog(Path(td) / "gaps.jsonl")
            security.install_socket_guard()
            try:
                convo.append(task="t", final_answer="a", outcome="done")
                gap.append(requested_name="x", args={"k": "v"}, task="t")
                self.assertEqual(len(convo.all()), 1)
                self.assertEqual(len(gap.all()), 1)
            finally:
                security.uninstall_socket_guard()


if __name__ == "__main__":
    unittest.main()
