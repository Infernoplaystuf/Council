"""ConstrainedAgent integration tests — action protocol + registry +
gap surfacing without execution."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import safe_agent
import tool_registry


class ScriptedRunner:
    name = "scripted"
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
    def chat(self, messages, *, temperature=0.2, max_tokens=600):
        if self.calls >= len(self.replies):
            return '{"action": "final", "answer": "(out of replies)"}'
        r = self.replies[self.calls]
        self.calls += 1
        return r
    def stream_chat(self, *_a, **_kw):
        yield from self.replies
    def describe(self): return {"backend": "scripted"}


def _policy(td: tempfile.TemporaryDirectory) -> safe_agent.AgentPolicy:
    root = Path(td.name) / "root"; root.mkdir(exist_ok=True)
    out  = Path(td.name) / "out";  out.mkdir(exist_ok=True)
    return safe_agent.AgentPolicy(
        allowed_tools=("read_local_file", "run_pandas_analysis", "query_memory"),
        file_root=root, output_dir=out,
        max_steps=4, default_timeout_s=2.0,
    )


class TestRequiresFrozenRegistry(unittest.TestCase):

    def test_unfrozen_registry_rejected(self):
        td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            policy = _policy(td)
            reg = tool_registry.ToolRegistry()
            # Forgot to freeze.
            with self.assertRaises(ValueError):
                safe_agent.ConstrainedAgent(
                    ScriptedRunner([]), reg, policy)
        finally:
            td.cleanup()

    def test_wrong_type_rejected(self):
        td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            policy = _policy(td)
            with self.assertRaises(TypeError):
                safe_agent.ConstrainedAgent(
                    ScriptedRunner([]),
                    {"read_local_file": None},          # type: ignore[arg-type]
                    policy,
                )
        finally:
            td.cleanup()


class TestEndToEnd(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        (Path(self.td.name)).mkdir(exist_ok=True)
        root = Path(self.td.name) / "root"
        root.mkdir(exist_ok=True)
        (root / "notes.txt").write_text("Q3 revenue rose 12%.",
                                          encoding="utf-8")
        self.policy = _policy(self.td)
        self.registry = tool_registry.build_default_registry(self.policy)

    def tearDown(self):
        self.td.cleanup()

    def test_two_step_run_completes(self):
        replies = [
            '{"action": "tool", "tool": "read_local_file", '
            '"args": {"path": "notes.txt"}}',
            '{"action": "final", "answer": "Q3 revenue rose 12%."}',
        ]
        agent = safe_agent.ConstrainedAgent(
            ScriptedRunner(replies), self.registry, self.policy)
        run = agent.run("summarise notes")
        self.assertEqual(run.stopped_reason, "done")
        self.assertEqual(run.final_answer, "Q3 revenue rose 12%.")
        self.assertEqual(run.tools_used, ["read_local_file"])
        self.assertEqual(run.tools_missing, [])
        self.assertEqual(len(run.steps), 2)

    def test_unlisted_tool_records_gap_without_executing(self):
        """The brief's hardest requirement: the tool's fn must NEVER run
        when the model requests an unlisted name. We assert it via a
        spy on the would-be-tool callable."""
        spy = mock.Mock(return_value={"never": "called"})
        # Note: we deliberately DON'T register this tool in the
        # registry. The agent should refuse it.
        replies = [
            '{"action": "tool", "tool": "shell_exec", '
            '"args": {"cmd": "rm -rf /"}}',
            '{"action": "final", "answer": "OK, falling back."}',
        ]
        agent = safe_agent.ConstrainedAgent(
            ScriptedRunner(replies), self.registry, self.policy)
        run = agent.run("do something risky")
        # Run completes; agent didn't crash.
        self.assertEqual(run.stopped_reason, "done")
        self.assertIn("shell_exec", run.tools_missing)
        # Step 1 has available=False
        self.assertFalse(run.steps[0].available)
        # The would-be tool callable was never called — the spy proves it
        spy.assert_not_called()

    def test_max_steps_enforced_on_looping_model(self):
        replies = ['{"action": "tool", "tool": "read_local_file", '
                   '"args": {"path": "notes.txt"}}'] * 20
        agent = safe_agent.ConstrainedAgent(
            ScriptedRunner(replies), self.registry, self.policy)
        run = agent.run("loop")
        self.assertEqual(run.stopped_reason, "max_steps")
        self.assertEqual(len(run.steps), self.policy.max_steps)

    def test_step_event_callback_fires_per_step(self):
        events = []
        replies = [
            '{"action": "tool", "tool": "read_local_file", '
            '"args": {"path": "notes.txt"}}',
            '{"action": "final", "answer": "done"}',
        ]
        agent = safe_agent.ConstrainedAgent(
            ScriptedRunner(replies), self.registry, self.policy)
        agent.run("x", on_step=lambda ev, run: events.append(ev))
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].action, "tool")
        self.assertEqual(events[1].action, "final")


class TestGracefulDegrade(unittest.TestCase):
    """A chatty model that emits prose without JSON shouldn't crash the
    loop — it becomes a final answer."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.policy = _policy(self.td)
        self.registry = tool_registry.build_default_registry(self.policy)

    def tearDown(self):
        self.td.cleanup()

    def test_prose_reply_finalises(self):
        agent = safe_agent.ConstrainedAgent(
            ScriptedRunner(["I think the answer is 42."]),
            self.registry, self.policy)
        run = agent.run("?")
        self.assertEqual(run.stopped_reason, "done")
        self.assertIn("42", run.final_answer)
        self.assertEqual(len(run.steps), 1)


if __name__ == "__main__":
    unittest.main()
