"""T5 tests — constrained agent. Path traversal, symlink escape,
max_steps, denied-tool, timeout, byte budget, allow-list enforcement."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

import safe_agent


# ============================================================
# Fixtures
# ============================================================

class FakeRunner:
    """Replays a scripted reply sequence — lets us drive the agent's
    safety machinery without an LLM."""
    name = "fake"
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
    def chat(self, messages, *, temperature=0.2, max_tokens=600):
        if self.calls >= len(self.replies):
            return "all done."     # no tool block → agent stops
        r = self.replies[self.calls]
        self.calls += 1
        return r
    def stream_chat(self, *_a, **_kw):
        yield from self.replies
    def describe(self):
        return {"backend": "fake"}


def _policy(file_root: Path, output_dir: Path, *,
            allowed=("read_local_file", "run_pandas_analysis", "query_memory"),
            max_steps=4):
    return safe_agent.AgentPolicy(
        allowed_tools=allowed,
        file_root=file_root,
        output_dir=output_dir,
        max_steps=max_steps,
        default_timeout_s=2.0,
    )


# ============================================================
# Path-traversal + symlink tests for read_local_file
# ============================================================

class TestFileToolPathSafety(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.td.name) / "root"
        self.out  = Path(self.td.name) / "out"
        self.root.mkdir()
        self.out.mkdir()
        (self.root / "ok.txt").write_text("hello inside", encoding="utf-8")
        # File OUTSIDE the root
        self.outside = Path(self.td.name) / "outside.txt"
        self.outside.write_text("SECRET", encoding="utf-8")

    def tearDown(self):
        self.td.cleanup()

    def test_basic_read_works(self):
        policy = _policy(self.root, self.out)
        tools = safe_agent.default_tools(policy)
        trace = safe_agent.AgentTrace()
        call = safe_agent.dispatch_tool(
            "read_local_file", {"path": "ok.txt"},
            policy, tools, trace, step=1,
        )
        self.assertIsNone(call.error)
        self.assertEqual(call.result["text"], "hello inside")

    def test_dotdot_traversal_refused(self):
        policy = _policy(self.root, self.out)
        tools = safe_agent.default_tools(policy)
        trace = safe_agent.AgentTrace()
        call = safe_agent.dispatch_tool(
            "read_local_file", {"path": "../outside.txt"},
            policy, tools, trace, step=1,
        )
        self.assertIsNotNone(call.error)
        self.assertIn("outside", call.error)
        # And NOT a read of the secret
        self.assertNotIn("SECRET", call.error)

    def test_absolute_path_outside_root_refused(self):
        policy = _policy(self.root, self.out)
        tools = safe_agent.default_tools(policy)
        trace = safe_agent.AgentTrace()
        call = safe_agent.dispatch_tool(
            "read_local_file", {"path": str(self.outside)},
            policy, tools, trace, step=1,
        )
        self.assertIsNotNone(call.error)
        self.assertIn("outside", call.error)

    def test_absolute_path_INSIDE_root_works(self):
        """Absolute paths that happen to land inside the root are fine —
        we only reject ones that escape after resolution."""
        policy = _policy(self.root, self.out)
        tools = safe_agent.default_tools(policy)
        trace = safe_agent.AgentTrace()
        target = self.root / "ok.txt"
        call = safe_agent.dispatch_tool(
            "read_local_file", {"path": str(target)},
            policy, tools, trace, step=1,
        )
        self.assertIsNone(call.error)
        self.assertEqual(call.result["text"], "hello inside")

    def test_symlink_escape_refused(self):
        """A symlink inside the root pointing OUT of the root must be
        refused — that's what makes ``_safe_resolve`` use realpath."""
        if sys.platform == "win32":
            # Symlinks on Windows need Developer Mode / admin. Try the
            # creation; skip the test if we lack the privilege.
            link = self.root / "escape"
            try:
                os.symlink(self.outside, link)
            except (OSError, NotImplementedError):
                self.skipTest("cannot create symlink without Developer Mode / admin")
        else:
            link = self.root / "escape"
            os.symlink(self.outside, link)

        policy = _policy(self.root, self.out)
        tools = safe_agent.default_tools(policy)
        trace = safe_agent.AgentTrace()
        call = safe_agent.dispatch_tool(
            "read_local_file", {"path": "escape"},
            policy, tools, trace, step=1,
        )
        self.assertIsNotNone(call.error,
                              f"symlink escape leaked: {call.result!r}")
        self.assertIn("outside", call.error)


# ============================================================
# Allow-list enforcement
# ============================================================

class TestAllowList(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.td.name) / "root"; self.root.mkdir()
        self.out  = Path(self.td.name) / "out";  self.out.mkdir()

    def tearDown(self):
        self.td.cleanup()

    def test_unlisted_tool_refused(self):
        policy = _policy(self.root, self.out, allowed=("read_local_file",))
        tools = safe_agent.default_tools(policy)
        trace = safe_agent.AgentTrace()
        with self.assertRaises(safe_agent.ToolDenied):
            safe_agent.dispatch_tool(
                "run_pandas_analysis", {"code": "DATA_FOLDER"},
                policy, tools, trace, step=1,
            )
        # Refusal logged in trace
        self.assertEqual(len(trace.denied), 1)
        self.assertEqual(trace.denied[0][0], "run_pandas_analysis")

    def test_unknown_tool_name_refused(self):
        policy = _policy(self.root, self.out)
        tools = safe_agent.default_tools(policy)
        trace = safe_agent.AgentTrace()
        with self.assertRaises(safe_agent.ToolDenied):
            safe_agent.dispatch_tool(
                "shell_exec", {"cmd": "rm -rf /"},
                policy, tools, trace, step=1,
            )

    def test_agent_loop_logs_denied(self):
        """The whole agent loop refuses unlisted tools but DOESN'T crash —
        the model gets a [tool-denied] observation and can recover."""
        policy = _policy(self.root, self.out, allowed=("read_local_file",))
        replies = [
            # Step 1: model asks for shell_exec — denied
            '{"tool": "shell_exec", "args": {"cmd": "ls /"}}',
            # Step 2: model gives a safe answer
            "OK, I'll just summarise from memory.",
        ]
        runner = FakeRunner(replies)
        result = safe_agent.run_agent(
            [{"role": "user", "content": "do something"}],
            runner, policy,
        )
        self.assertEqual(result.stopped_reason, "done")
        # The denied call was recorded
        denied_names = [d[0] for d in result.trace.denied]
        self.assertIn("shell_exec", denied_names)


# ============================================================
# Loop bounds: max_steps, timeout
# ============================================================

class TestLoopBounds(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.td.name) / "root"; self.root.mkdir()
        (self.root / "x.txt").write_text("ok")
        self.out  = Path(self.td.name) / "out";  self.out.mkdir()

    def tearDown(self):
        self.td.cleanup()

    def test_max_steps_enforced(self):
        # Model keeps asking for a tool forever; we stop at policy.max_steps
        policy = _policy(self.root, self.out, max_steps=3)
        replies = ['{"tool": "read_local_file", "args": {"path": "x.txt"}}'] * 10
        result = safe_agent.run_agent(
            [{"role": "user", "content": "loop"}],
            FakeRunner(replies), policy,
        )
        self.assertEqual(result.stopped_reason, "max_steps")
        self.assertEqual(len(result.trace.calls), 3)   # one per step

    def test_per_tool_timeout(self):
        """Replace one tool with a sleeper; verify TimeoutError surfaces
        as ToolTimeout, error string is set, trace records the call."""
        policy = _policy(self.root, self.out)
        # Custom slow tool
        def slow_fn(args, pol):
            time.sleep(5.0)
            return "never"
        tools = safe_agent.default_tools(policy)
        tools["slow"] = safe_agent.Tool(
            name="slow", fn=slow_fn, timeout_s=0.2,
        )
        policy2 = safe_agent.AgentPolicy(
            allowed_tools=("slow",),
            file_root=self.root, output_dir=self.out,
            max_steps=1, default_timeout_s=0.2,
        )
        trace = safe_agent.AgentTrace()
        with self.assertRaises(safe_agent.ToolTimeout):
            safe_agent.dispatch_tool("slow", {}, policy2, tools, trace, step=1)
        # And the trace recorded the attempt
        self.assertEqual(len(trace.calls), 1)
        self.assertIn("timeout", trace.calls[0].error.lower())


# ============================================================
# Tool-call parser
# ============================================================

class TestParser(unittest.TestCase):

    def test_basic_block(self):
        reply = 'I will read the file. {"tool": "read_local_file", "args": {"path": "a.txt"}}'
        calls = safe_agent.parse_tool_calls(reply)
        self.assertEqual(calls, [("read_local_file", {"path": "a.txt"})])

    def test_multiple_blocks(self):
        reply = ('{"tool": "read_local_file", "args": {"path": "a.txt"}}\n'
                 '{"tool": "query_memory", "args": {"text": "rev"}}')
        calls = safe_agent.parse_tool_calls(reply)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], "read_local_file")
        self.assertEqual(calls[1][0], "query_memory")

    def test_text_only_returns_empty(self):
        self.assertEqual(safe_agent.parse_tool_calls("just text"), [])

    def test_malformed_block_ignored(self):
        # Missing closing brace mid-JSON
        reply = 'before {"tool": "read_local_file", "args": {"path": ok}} after'
        calls = safe_agent.parse_tool_calls(reply)
        # Parser is lenient — broken JSON is silently dropped, not crashed
        self.assertEqual(len(calls), 0)


# ============================================================
# Smoke: end-to-end with default tools, no LLM
# ============================================================

class TestEndToEnd(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.td.name) / "root"; self.root.mkdir()
        (self.root / "notes.txt").write_text("Q3 revenue was up 12%",
                                              encoding="utf-8")
        self.out = Path(self.td.name) / "out"; self.out.mkdir()

    def tearDown(self):
        self.td.cleanup()

    def test_agent_reads_then_answers(self):
        policy = _policy(self.root, self.out)
        replies = [
            '{"tool": "read_local_file", "args": {"path": "notes.txt"}}',
            "Q3 revenue was up 12%.",
        ]
        runner = FakeRunner(replies)
        result = safe_agent.run_agent(
            [{"role": "user", "content": "summarise notes"}],
            runner, policy,
        )
        self.assertEqual(result.stopped_reason, "done")
        self.assertEqual(len(result.trace.calls), 1)
        self.assertEqual(result.trace.calls[0].name, "read_local_file")
        self.assertEqual(result.final_answer, "Q3 revenue was up 12%.")


if __name__ == "__main__":
    unittest.main()
