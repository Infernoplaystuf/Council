"""Integration: ConstrainedAgent + ToolGapLog. When the model requests a
tool that is NOT registered, the gap log gets a record and the would-be
tool callable never runs."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_logs
import safe_agent
import tool_registry


class ScriptedRunner:
    name = "scripted"
    def __init__(self, replies):
        self.replies = list(replies); self.calls = 0
    def chat(self, messages, *, temperature=0.2, max_tokens=600):
        if self.calls >= len(self.replies):
            return '{"action":"final","answer":"out of replies"}'
        r = self.replies[self.calls]; self.calls += 1
        return r
    def stream_chat(self, *_a, **_kw):
        yield from self.replies
    def describe(self): return {"backend": "scripted"}


class TestGapLoggedNotExecuted(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        root = Path(self.td.name) / "root"; root.mkdir()
        out  = Path(self.td.name) / "out";  out.mkdir()
        self.policy = safe_agent.AgentPolicy(
            allowed_tools=("read_local_file", "run_pandas_analysis", "query_memory"),
            file_root=root, output_dir=out,
            max_steps=4, default_timeout_s=2.0,
        )
        self.registry = tool_registry.build_default_registry(self.policy)
        self.convo_log = agent_logs.ConversationLog(
            Path(self.td.name) / "runs.jsonl")
        self.gap_log = agent_logs.ToolGapLog(
            Path(self.td.name) / "gaps.jsonl")

    def tearDown(self):
        self.td.cleanup()

    def test_unlisted_tool_logs_gap_and_does_not_run(self):
        replies = [
            '{"action": "tool", "tool": "send_email", '
            '"args": {"to": "x@y", "subject": "Q3"}}',
            '{"action": "final", "answer": "I would have emailed Q3 numbers"}',
        ]
        agent = safe_agent.ConstrainedAgent(
            ScriptedRunner(replies), self.registry, self.policy,
            conversation_log=self.convo_log, gap_log=self.gap_log,
        )
        run = agent.run("email me Q3", context={"session": "test"})
        # Run completed gracefully
        self.assertEqual(run.stopped_reason, "done")
        # The gap is in the structured tools_missing list AND on disk
        self.assertIn("send_email", run.tools_missing)
        gap_rows = self.gap_log.all()
        self.assertEqual(len(gap_rows), 1)
        self.assertEqual(gap_rows[0]["requested_name"], "send_email")
        self.assertEqual(gap_rows[0]["args_shape"],
                         {"to": "str", "subject": "str"})
        # And the conversation log carries the run
        convo_rows = self.convo_log.all()
        self.assertEqual(len(convo_rows), 1)
        self.assertEqual(convo_rows[0]["tools_missing"], ["send_email"])

    def test_no_callable_executed_for_unlisted_tool(self):
        """Spy on every tool's fn — none should be called when the model
        requests an unlisted name."""
        spies = {}
        for name, tool in self.registry.as_dict().items():
            spies[name] = mock.Mock(side_effect=tool.fn)
            tool.fn = spies[name]      # mutate the in-memory tool (test only)

        replies = [
            '{"action": "tool", "tool": "evil_unregistered", "args": {}}',
            '{"action": "final", "answer": "stopped"}',
        ]
        agent = safe_agent.ConstrainedAgent(
            ScriptedRunner(replies), self.registry, self.policy,
            gap_log=self.gap_log,
        )
        agent.run("?")
        # None of the allow-listed tools' callables ran.
        for name, spy in spies.items():
            spy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
