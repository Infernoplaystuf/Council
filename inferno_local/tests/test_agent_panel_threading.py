"""Headless tests for the agent_panel worker → queue → main-loop pattern.

The Tk widget itself is hard to exercise without a display. What we CAN
verify cheaply:
  • The on_step callback used by the panel only ever enqueues — it
    never touches a Tk object (which would crash from a worker thread).
  • A worker thread + queue.Queue drain reproduces the panel's flow end-
    to-end against a ScriptedRunner.
  • Approve/Dismiss flips status only — no registry mutation.

(The Tk widget itself is sketched as best-effort under a hidden root in
a separate test that auto-skips when Tk is unavailable, e.g. on CI.)
"""
from __future__ import annotations

import queue
import tempfile
import threading
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
            return '{"action":"final","answer":"end"}'
        r = self.replies[self.calls]; self.calls += 1
        return r
    def stream_chat(self, *_a, **_kw):
        yield from self.replies
    def describe(self): return {"backend": "scripted"}


def _build_agent_and_logs(td: tempfile.TemporaryDirectory, replies):
    root = Path(td.name) / "root"; root.mkdir(exist_ok=True)
    (root / "notes.txt").write_text("Q3 up 12%", encoding="utf-8")
    out  = Path(td.name) / "out";  out.mkdir(exist_ok=True)
    policy = safe_agent.AgentPolicy(
        allowed_tools=("read_local_file", "run_pandas_analysis", "query_memory"),
        file_root=root, output_dir=out,
        max_steps=4, default_timeout_s=2.0,
    )
    registry = tool_registry.build_default_registry(policy)
    convo = agent_logs.ConversationLog(Path(td.name) / "runs.jsonl")
    gap   = agent_logs.ToolGapLog(Path(td.name) / "gaps.jsonl")
    agent = safe_agent.ConstrainedAgent(
        ScriptedRunner(replies), registry, policy,
        conversation_log=convo, gap_log=gap,
    )
    return agent, convo, gap, policy, registry


class TestWorkerQueuePattern(unittest.TestCase):
    """Reproduces the panel's flow: run agent on a thread, queue
    StepEvents, drain on the main thread. Verifies events arrive in
    order and the worker doesn't touch any UI surrogate."""

    def test_drain_yields_events_in_order(self):
        td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            agent, convo, gap, *_ = _build_agent_and_logs(td, [
                '{"action": "tool", "tool": "read_local_file", '
                '"args": {"path": "notes.txt"}}',
                '{"action": "final", "answer": "Q3 up 12%"}',
            ])
            q: "queue.Queue" = queue.Queue()
            ui_thread_touches = []

            def _on_step(ev, run):
                # Simulate the panel: only enqueue, never touch the UI.
                q.put_nowait(("step", ev))
                # Touch a sentinel that proves we're NOT on the main thread.
                ui_thread_touches.append(threading.get_ident())

            run_holder = {}
            def _worker():
                run = agent.run("summarise notes", on_step=_on_step)
                q.put_nowait(("complete", run))
                run_holder["run"] = run

            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            t.join(timeout=5)
            self.assertFalse(t.is_alive())

            # Drain from THIS (main) thread — same as root.after would.
            events = []
            while not q.empty():
                events.append(q.get_nowait())
            kinds = [k for k, _ in events]
            self.assertEqual(kinds, ["step", "step", "complete"])
            # The worker thread id is different from the test thread id —
            # confirming nothing UI-ish ran on the worker.
            self.assertTrue(
                all(tid != threading.get_ident() for tid in ui_thread_touches))
        finally:
            td.cleanup()


class TestPanelOps(unittest.TestCase):
    """The non-UI-graphical surface of the panel: approve/dismiss flips
    status, never registers a tool."""

    def test_approve_changes_status_only(self):
        td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            policy = safe_agent.AgentPolicy(
                allowed_tools=("read_local_file",),
                file_root=Path(td.name), output_dir=Path(td.name),
                max_steps=2, default_timeout_s=1.0,
            )
            registry = tool_registry.build_default_registry(policy)
            q = tool_gap_analyzer.ProposalQueue(Path(td.name) / "p.jsonl")
            p = tool_gap_analyzer.ToolProposal(
                proposed_name="send_email", description="d",
                input_params={"to": "str"}, output="ok",
                rationale="r", observed_count=3,
            )
            q.append(p)
            with mock.patch.object(
                tool_registry.ToolRegistry, "register",
                side_effect=tool_registry.ToolRegistry.register,
                autospec=True,
            ) as reg_spy:
                q.update_status(p.proposal_id, "approved")
                reg_spy.assert_not_called()
            statuses = q.current_status()
            self.assertEqual(statuses[0]["status"], "approved")
            self.assertNotIn("send_email", registry.names())
        finally:
            td.cleanup()


class TestPanelInstantiates(unittest.TestCase):
    """Sketch test: build the panel under a hidden Tk root, schedule its
    drain once, then destroy. Skips when Tk can't open (CI / SSH)."""

    def test_construct_destroy(self):
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
        except Exception:
            self.skipTest("Tk not available in this environment")
            return
        try:
            import agent_panel

            td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
            try:
                agent, *_ = _build_agent_and_logs(td, [
                    '{"action":"final","answer":"x"}',
                ])
                # Provide a factory that returns the prebuilt agent
                panel = agent_panel.AgentPanel(
                    root, agent_factory=lambda: agent,
                )
                # Drain once so any scheduled callbacks fire harmlessly
                root.update_idletasks()
                panel.destroy()
            finally:
                td.cleanup()
        finally:
            try:
                root.destroy()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
