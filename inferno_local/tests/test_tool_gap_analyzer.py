"""Tests for tool_gap_analyzer.

Covers:
  • aggregate() correctness (deterministic, no model needed)
  • analyze() with a scripted runner — proposals written, threshold respected
  • Already-listed tool names are skipped
  • Status changes are recorded but the registry is never mutated
  • THE BIG NEGATIVE: the analyzer registers zero tools and imports
    nothing model-authored — across all monkeypatched escape hatches.
"""
from __future__ import annotations

import builtins
import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_logs
import safe_agent
import tool_gap_analyzer
import tool_registry


class ScriptedRunner:
    name = "scripted"
    def __init__(self, replies):
        self.replies = list(replies); self.calls = 0
    def chat(self, messages, *, temperature=0.2, max_tokens=600):
        if self.calls >= len(self.replies):
            return '{"action":"final","answer":"x"}'
        r = self.replies[self.calls]; self.calls += 1
        return r
    def stream_chat(self, *_a, **_kw):
        yield from self.replies
    def describe(self): return {"backend": "scripted"}


def _make_policy(td: tempfile.TemporaryDirectory) -> safe_agent.AgentPolicy:
    root = Path(td.name) / "root"; root.mkdir(exist_ok=True)
    out  = Path(td.name) / "out";  out.mkdir(exist_ok=True)
    return safe_agent.AgentPolicy(
        allowed_tools=("read_local_file", "run_pandas_analysis", "query_memory"),
        file_root=root, output_dir=out,
        max_steps=4, default_timeout_s=2.0,
    )


# ============================================================
# Aggregation
# ============================================================

class TestAggregateDeterministic(unittest.TestCase):

    def _gap_records(self):
        return [
            {"ts": 1000, "requested_name": "send_email",
             "args_shape": {"to": "str", "subject": "str"},
             "args_sample": {"to": "x@y", "subject": "Q3"},
             "task": "email Q3", "step": 1, "context": {}},
            {"ts": 2000, "requested_name": "Send_Email",   # different case
             "args_shape": {"to": "str", "subject": "str",
                            "body": "str"},
             "args_sample": {}, "task": "email update", "step": 2,
             "context": {}},
            {"ts": 3000, "requested_name": "upload_to_s3",
             "args_shape": {"path": "str"},
             "args_sample": {"path": "out.csv"}, "task": "upload",
             "step": 1, "context": {}},
        ]

    def test_normalised_grouping(self):
        b = tool_gap_analyzer.aggregate(self._gap_records())
        self.assertIn("send_email", b)
        self.assertEqual(b["send_email"].count, 2)
        self.assertIn("send_email", b["send_email"].requested_variants)
        self.assertIn("Send_Email", b["send_email"].requested_variants)
        self.assertEqual(b["upload_to_s3"].count, 1)

    def test_args_shapes_deduplicated(self):
        b = tool_gap_analyzer.aggregate(self._gap_records())
        shapes = b["send_email"].args_shapes
        self.assertEqual(len(shapes), 2)   # 2-field and 3-field variants

    def test_first_and_last_seen(self):
        b = tool_gap_analyzer.aggregate(self._gap_records())
        self.assertEqual(b["send_email"].first_seen_ts, 1000)
        self.assertEqual(b["send_email"].last_seen_ts, 2000)

    def test_context_sampling_capped(self):
        recs = [
            {"ts": i, "requested_name": "x", "args_shape": {},
             "args_sample": {}, "task": f"t{i}", "step": i, "context": {}}
            for i in range(20)
        ]
        b = tool_gap_analyzer.aggregate(recs, context_sample_max=3)
        self.assertEqual(len(b["x"].sample_contexts), 3)

    def test_empty_input(self):
        self.assertEqual(tool_gap_analyzer.aggregate([]), {})


# ============================================================
# analyze() — proposals written, threshold respected
# ============================================================

class TestAnalyzeProposals(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        policy = _make_policy(self.td)
        self.registry = tool_registry.build_default_registry(policy)
        self.gap_log = agent_logs.ToolGapLog(
            Path(self.td.name) / "gaps.jsonl")
        self.convo_log = agent_logs.ConversationLog(
            Path(self.td.name) / "runs.jsonl")
        self.queue = tool_gap_analyzer.ProposalQueue(
            Path(self.td.name) / "props.jsonl")
        # Pre-seed gap log: send_email x3, upload x1.
        for _ in range(3):
            self.gap_log.append(
                requested_name="send_email",
                args={"to": "x@y", "subject": "z"},
                task="email Q3", step=1)
        self.gap_log.append(
            requested_name="upload",
            args={"path": "out.csv"},
            task="upload result", step=2)

    def tearDown(self):
        self.td.cleanup()

    def test_threshold_respected(self):
        analyzer = tool_gap_analyzer.ToolGapAnalyzer(
            self.registry.view(),
            gap_log=self.gap_log, conversation_log=self.convo_log,
            queue=self.queue, threshold=2,
        )
        report = analyzer.analyze()
        self.assertEqual(report.over_threshold, 1)
        self.assertEqual(report.proposals_written, 1)
        self.assertEqual(len(self.queue.all()), 1)

    def test_model_drafted_description_used(self):
        analyzer = tool_gap_analyzer.ToolGapAnalyzer(
            self.registry.view(),
            gap_log=self.gap_log, conversation_log=self.convo_log,
            queue=self.queue, threshold=2,
        )
        runner = ScriptedRunner([
            '{"description": "Send a notification email to a recipient.",'
            ' "rationale": "Three runs asked for outbound email."}',
        ])
        analyzer.analyze(runner=runner)
        rows = self.queue.all()
        self.assertEqual(rows[0]["description"],
                          "Send a notification email to a recipient.")

    def test_already_listed_skipped(self):
        # Pre-seed a gap for a tool that IS registered.
        for _ in range(2):
            self.gap_log.append(
                requested_name="read_local_file",
                args={"path": "a.txt"},
                task="oddly model called the tool wrong", step=1)
        analyzer = tool_gap_analyzer.ToolGapAnalyzer(
            self.registry.view(),
            gap_log=self.gap_log, conversation_log=self.convo_log,
            queue=self.queue, threshold=2,
        )
        report = analyzer.analyze()
        # send_email written (1); read_local_file skipped (already listed)
        self.assertIn("read_local_file", report.skipped_already_listed)


# ============================================================
# Proposal queue status changes
# ============================================================

class TestProposalQueue(unittest.TestCase):

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.queue = tool_gap_analyzer.ProposalQueue(
            Path(self.td.name) / "props.jsonl")

    def tearDown(self):
        self.td.cleanup()

    def test_append_then_update_status(self):
        p = tool_gap_analyzer.ToolProposal(
            proposed_name="send_email",
            description="d", input_params={"to": "str"},
            output="ok", rationale="r", observed_count=3,
        )
        self.queue.append(p)
        pid = p.proposal_id
        self.queue.update_status(pid, "approved")
        statuses = self.queue.current_status()
        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0]["status"], "approved")
        self.assertEqual(statuses[0]["proposed_name"], "send_email")

    def test_status_change_does_not_rewrite_records(self):
        """Append-only — we never destroy past state."""
        p = tool_gap_analyzer.ToolProposal(
            proposed_name="x", description="d",
            input_params={}, output="o", rationale="r",
            observed_count=2,
        )
        self.queue.append(p)
        original_lines = self.queue.path.read_text(encoding="utf-8")
        self.queue.update_status(p.proposal_id, "dismissed")
        after = self.queue.path.read_text(encoding="utf-8")
        # Original line is still present, status_change is appended.
        self.assertTrue(after.startswith(original_lines.rstrip()))

    def test_invalid_status_rejected(self):
        with self.assertRaises(ValueError):
            self.queue.update_status("x", "frobnicated")


# ============================================================
# THE BIG NEGATIVE — the analyzer cannot extend the system
# ============================================================

class TestAnalyzerCannotExtendItself(unittest.TestCase):
    """The brief's single most important rule.

    These tests pry every escape hatch the analyzer might use:
      • ToolRegistry.register   — monkeypatched to count calls
      • importlib.import_module — counts calls
      • builtins.exec / builtins.eval — counts calls
      • Any .py file written under the queue's directory — none allowed
    Then runs analyze() against gap data and asserts every counter is 0.
    """

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        policy = _make_policy(self.td)
        self.registry = tool_registry.build_default_registry(policy)
        self.gap_log = agent_logs.ToolGapLog(
            Path(self.td.name) / "gaps.jsonl")
        self.convo_log = agent_logs.ConversationLog(
            Path(self.td.name) / "runs.jsonl")
        self.queue = tool_gap_analyzer.ProposalQueue(
            Path(self.td.name) / "props.jsonl")
        # Five distinct gaps each over threshold 2.
        for n in ("send_email", "upload_to_s3", "fetch_url",
                  "run_shell", "render_chart"):
            for _ in range(3):
                self.gap_log.append(
                    requested_name=n,
                    args={"k": "v"},
                    task="t", step=1,
                )

    def tearDown(self):
        self.td.cleanup()

    def test_no_tool_registered_no_code_imported(self):
        # Spy on registration via the live ToolRegistry class.
        with mock.patch.object(
            tool_registry.ToolRegistry, "register",
            side_effect=tool_registry.ToolRegistry.register,
            autospec=True,
        ) as reg_spy, \
             mock.patch.object(
                 importlib, "import_module",
                 side_effect=importlib.import_module
             ) as import_spy, \
             mock.patch.object(builtins, "exec",
                                side_effect=builtins.exec) as exec_spy, \
             mock.patch.object(builtins, "eval",
                                side_effect=builtins.eval) as eval_spy:

            analyzer = tool_gap_analyzer.ToolGapAnalyzer(
                self.registry.view(),
                gap_log=self.gap_log,
                conversation_log=self.convo_log,
                queue=self.queue,
                threshold=2,
            )
            # No runner — exercises the template fallback path
            # (the brief's "even on an air-gapped box without a model").
            report = analyzer.analyze()

            self.assertGreater(report.proposals_written, 0,
                                "analyzer should have written proposals")
            # Hard assertions: zero escape-hatch use.
            reg_spy.assert_not_called()
            exec_spy.assert_not_called()
            eval_spy.assert_not_called()
            # importlib.import_module: analyzer itself doesn't call it
            self.assertEqual(import_spy.call_count, 0,
                              f"unexpected import_module calls: "
                              f"{[c.args for c in import_spy.call_args_list]}")

    def test_no_py_file_written_during_analyze(self):
        before = set((Path(self.td.name).rglob("*.py")))
        analyzer = tool_gap_analyzer.ToolGapAnalyzer(
            self.registry.view(),
            gap_log=self.gap_log,
            conversation_log=self.convo_log,
            queue=self.queue,
            threshold=2,
        )
        analyzer.analyze()
        after = set((Path(self.td.name).rglob("*.py")))
        self.assertEqual(after - before, set(),
                          "analyzer created a .py file — quarantine path "
                          "was NOT supposed to be enabled.")

    def test_status_change_does_not_register_a_tool(self):
        """Approving a proposal flips status text only — the registry is
        untouched and remains frozen."""
        analyzer = tool_gap_analyzer.ToolGapAnalyzer(
            self.registry.view(),
            gap_log=self.gap_log,
            conversation_log=self.convo_log,
            queue=self.queue,
            threshold=2,
        )
        analyzer.analyze()
        proposals = self.queue.current_status()
        self.assertGreater(len(proposals), 0)
        before_names = set(self.registry.names())
        self.queue.update_status(proposals[0]["proposal_id"], "approved")
        after_names = set(self.registry.names())
        self.assertEqual(before_names, after_names)
        # Registry is still frozen.
        self.assertTrue(self.registry.frozen)
        with self.assertRaises(tool_registry.RegistryFrozen):
            self.registry.register(safe_agent.Tool(
                name=proposals[0]["proposed_name"],
                fn=lambda a, p: None,
            ))

    def test_analyzer_rejects_full_registry(self):
        """Passing the ToolRegistry itself (vs. a RegistryView) is a
        typed error — the analyzer NEVER has direct register access."""
        with self.assertRaises(TypeError):
            tool_gap_analyzer.ToolGapAnalyzer(
                self.registry,    # type: ignore[arg-type] — wrong type
                gap_log=self.gap_log,
                conversation_log=self.convo_log,
                queue=self.queue,
            )


if __name__ == "__main__":
    unittest.main()
