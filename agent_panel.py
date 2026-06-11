"""
agent_panel.py — Tkinter "Agent & Tool Proposals" panel.

Two panes:

  • Live trace — runs a ConstrainedAgent on a worker thread, streams
    StepEvents through a queue.Queue, drains the queue on the Tk loop
    via root.after(50, …). Inference never blocks the UI thread.

  • Proposal review — lists pending ToolProposal records from the
    review queue, with Approve / Dismiss buttons. Approval changes ONLY
    the proposal's status field. There is no UI code path that
    registers a tool. The "Approved" status is, per the brief, a hand-
    off for ME (the operator) to implement the tool in code.

Public entry points:

    open_panel(parent=root, agent_factory=lambda: ConstrainedAgent(...))
    AgentPanel(parent, agent_factory)

The factory pattern keeps the panel decoupled from process-start
wiring — the host app builds the registry once and hands the panel a
function that produces a ConstrainedAgent on demand.

No Tk import happens at module-import time of any non-panel module —
the host app imports agent_panel lazily when the user opens the panel.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import asdict
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable, Dict, List, Optional

import agent_logs
import safe_agent
import tool_gap_analyzer


_DRAIN_INTERVAL_MS = 50           # how often we pull StepEvents from the queue


class AgentPanel(tk.Toplevel):
    """Live trace + proposal review for the constrained agent."""

    def __init__(self,
                 parent: Optional[tk.Misc],
                 *,
                 agent_factory: Callable[[], "safe_agent.ConstrainedAgent"],
                 proposal_queue: Optional[tool_gap_analyzer.ProposalQueue] = None,
                 conversation_log: Optional[agent_logs.ConversationLog] = None,
                 ) -> None:
        super().__init__(parent)
        self.title("Agent & Tool Proposals — Data's Inferno")
        try:
            self.geometry("980x680")
        except Exception:
            pass
        self.minsize(720, 480)

        self._agent_factory = agent_factory
        self._proposal_queue = proposal_queue or tool_gap_analyzer.ProposalQueue.default()
        self._conversation_log = conversation_log or agent_logs.ConversationLog.default()

        self._step_queue: "queue.Queue[Any]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._run_in_progress = False

        self._build()
        self._after_id: Optional[str] = None
        self._schedule_drain()
        self._refresh_proposals()

    # ── layout ─────────────────────────────────────────────
    def _build(self) -> None:
        bg, fg = "#1a1d28", "#e6e8ee"
        self.configure(bg=bg)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=10)

        # Tab 1: live trace
        self._trace_tab = tk.Frame(nb, bg=bg)
        nb.add(self._trace_tab, text="Live trace")
        self._build_trace_tab(bg, fg)

        # Tab 2: proposal review
        self._proposals_tab = tk.Frame(nb, bg=bg)
        nb.add(self._proposals_tab, text="Tool proposals")
        self._build_proposals_tab(bg, fg)

    def _build_trace_tab(self, bg: str, fg: str) -> None:
        f = self._trace_tab
        top = tk.Frame(f, bg=bg)
        top.pack(fill="x", padx=10, pady=8)
        tk.Label(top, text="Task:", bg=bg, fg=fg,
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        self._task_var = tk.StringVar(value="Summarise the notes file in the data folder.")
        entry = tk.Entry(top, textvariable=self._task_var, width=70,
                         font=("Segoe UI", 10))
        entry.pack(side="left", fill="x", expand=True, padx=8)
        self._run_btn = ttk.Button(top, text="▶ Run agent",
                                    command=self._on_run_clicked)
        self._run_btn.pack(side="left")

        self._status_var = tk.StringVar(value="idle.")
        tk.Label(f, textvariable=self._status_var, bg=bg, fg="#cbd1de",
                 anchor="w").pack(fill="x", padx=10)

        self._trace_text = tk.Text(f, bg="#0e1018", fg=fg, wrap="word",
                                    font=("Consolas", 10),
                                    relief="flat", padx=10, pady=10)
        self._trace_text.pack(fill="both", expand=True, padx=10, pady=8)
        self._trace_text.tag_configure("tool", foreground="#9adcfa")
        self._trace_text.tag_configure("final", foreground="#7be07b")
        self._trace_text.tag_configure("gap", foreground="#f5a062")
        self._trace_text.tag_configure("error", foreground="#e07e7e")

    def _build_proposals_tab(self, bg: str, fg: str) -> None:
        f = self._proposals_tab
        toolbar = tk.Frame(f, bg=bg)
        toolbar.pack(fill="x", padx=10, pady=8)
        ttk.Button(toolbar, text="🔄 Refresh",
                   command=self._refresh_proposals).pack(side="left")
        tk.Label(toolbar, text="  Approval flips status only — no tool is "
                              "registered by this panel.",
                 bg=bg, fg="#cbd1de").pack(side="left", padx=8)

        cols = ("status", "kind", "name", "count", "description")
        self._proposal_tree = ttk.Treeview(f, columns=cols,
                                            show="headings", height=12)
        for c, w in zip(cols, (90, 95, 200, 80, 400)):
            self._proposal_tree.heading(c, text=c.upper())
            self._proposal_tree.column(c, width=w, anchor="w")
        self._proposal_tree.pack(fill="both", expand=True, padx=10, pady=4)

        # Detail pane
        det_frame = tk.Frame(f, bg=bg)
        det_frame.pack(fill="x", padx=10, pady=4)
        self._detail_text = tk.Text(det_frame, height=10, bg="#0e1018",
                                     fg=fg, font=("Consolas", 9),
                                     wrap="word", relief="flat",
                                     padx=10, pady=6)
        self._detail_text.pack(fill="both", expand=True)
        self._proposal_tree.bind("<<TreeviewSelect>>", self._on_select_proposal)

        # Action buttons
        actions = tk.Frame(f, bg=bg)
        actions.pack(fill="x", padx=10, pady=(0, 10))
        self._approve_btn = ttk.Button(actions, text="✅ Approve (status only)",
                                        command=self._on_approve)
        self._approve_btn.pack(side="left")
        self._dismiss_btn = ttk.Button(actions, text="🗑 Dismiss",
                                        command=self._on_dismiss)
        self._dismiss_btn.pack(side="left", padx=6)
        self._analyze_btn = ttk.Button(actions,
                                        text="🔬 Analyze gaps + failures (no model)",
                                        command=self._on_analyze)
        self._analyze_btn.pack(side="right")

    # ── live trace path ─────────────────────────────────────
    def _on_run_clicked(self) -> None:
        if self._run_in_progress:
            messagebox.showinfo("Run in progress",
                                "Wait for the current run to finish.",
                                parent=self)
            return
        task = self._task_var.get().strip()
        if not task:
            return
        self._run_in_progress = True
        self._status_var.set("starting...")
        self._trace_text.delete("1.0", "end")
        self._trace_text.insert("end", f"task: {task}\n\n")
        try:
            self._run_btn.configure(state="disabled")
        except Exception:
            pass
        # Worker thread does inference; queues StepEvents back.
        t = threading.Thread(target=self._run_worker, args=(task,),
                              daemon=True)
        self._worker = t
        t.start()

    def _run_worker(self, task: str) -> None:
        try:
            agent = self._agent_factory()
        except Exception as exc:
            self._step_queue.put(("error", f"agent_factory failed: {exc!r}"))
            self._step_queue.put(("done", None))
            return

        def _on_step(ev: "safe_agent.StepEvent", run: "safe_agent.AgentRun") -> None:
            # CALLED ON THE WORKER THREAD. Never touch Tk from here —
            # only queue the event for the main loop to render.
            try:
                self._step_queue.put_nowait(("step", ev))
            except queue.Full:
                pass

        try:
            run = agent.run(task, on_step=_on_step)
            self._step_queue.put(("complete", run))
        except Exception as exc:
            self._step_queue.put(("error", f"run failed: {exc!r}"))
        finally:
            self._step_queue.put(("done", None))

    def _schedule_drain(self) -> None:
        # Liveness guard — after the Toplevel is destroyed, self.after()
        # raises TclError. Without this check the drain loop's `finally`
        # reschedule throws into Tk's callback error handler on every
        # tick after the user closes the panel.
        try:
            if not self.winfo_exists():
                self._after_id = None
                return
            self._after_id = self.after(_DRAIN_INTERVAL_MS, self._drain_queue)
        except tk.TclError:
            self._after_id = None

    def destroy(self) -> None:
        # Cancel the pending drain callback so a destroyed panel leaves
        # nothing in Tk's after-queue. Worker threads are daemons and
        # write only to self._step_queue, so they can finish harmlessly.
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        super().destroy()

    def _drain_queue(self) -> None:
        try:
            while True:
                try:
                    kind, payload = self._step_queue.get_nowait()
                except queue.Empty:
                    break
                if kind == "step":
                    self._render_step(payload)
                elif kind == "complete":
                    self._render_complete(payload)
                elif kind == "error":
                    self._trace_text.insert("end",
                        f"\n[error] {payload}\n", "error")
                    self._status_var.set("error.")
                elif kind == "done":
                    self._run_in_progress = False
                    try:
                        self._run_btn.configure(state="normal")
                    except Exception:
                        pass
        finally:
            self._schedule_drain()

    def _render_step(self, ev: "safe_agent.StepEvent") -> None:
        if ev.action == "tool":
            tag = "gap" if not ev.available else "tool"
            self._trace_text.insert("end",
                f"step {ev.step}: tool={ev.tool}  args={ev.args}\n", tag)
            if ev.observation:
                self._trace_text.insert("end",
                    f"  → {ev.observation[:600]}\n", tag)
        elif ev.action == "final":
            self._trace_text.insert("end",
                f"step {ev.step}: FINAL → {ev.final_answer}\n", "final")
        if ev.error:
            self._trace_text.insert("end",
                f"  error: {ev.error}\n", "error")
        self._trace_text.see("end")
        self._status_var.set(f"step {ev.step}: {ev.action}")

    def _render_complete(self, run: "safe_agent.AgentRun") -> None:
        self._trace_text.insert("end",
            f"\n=== {run.stopped_reason} ===\n"
            f"tools_used:    {run.tools_used}\n"
            f"tools_missing: {run.tools_missing}\n", "final")
        self._status_var.set(f"done: {run.stopped_reason}")
        # The gap log was already written inside the agent; refresh the
        # proposal pane so any new gap shows up if the user clicks
        # "Analyze gaps now".
        self._refresh_proposals()

    # ── proposal review path ───────────────────────────────
    def _refresh_proposals(self) -> None:
        for row in self._proposal_tree.get_children():
            self._proposal_tree.delete(row)
        try:
            proposals = self._proposal_queue.current_status()
        except Exception as exc:
            messagebox.showerror("Read failed", repr(exc), parent=self)
            return
        # Newest first
        proposals.sort(key=lambda p: -int(p.get("ts", 0)))
        for p in proposals:
            self._proposal_tree.insert("", "end",
                iid=p.get("proposal_id", ""),
                values=(
                    p.get("status", "pending").upper(),
                    p.get("kind", "tool_gap"),
                    p.get("proposed_name", ""),
                    p.get("observed_count", 0),
                    (p.get("description", "") or "")[:120],
                ))

    def _on_select_proposal(self, _event=None) -> None:
        sel = self._proposal_tree.selection()
        if not sel:
            return
        proposals = {p["proposal_id"]: p
                     for p in self._proposal_queue.current_status()}
        p = proposals.get(sel[0])
        if not p:
            return
        self._detail_text.config(state="normal")
        self._detail_text.delete("1.0", "end")
        self._detail_text.insert("end",
            f"id:          {p.get('proposal_id')}\n"
            f"kind:        {p.get('kind', 'tool_gap')}\n"
            f"name:        {p.get('proposed_name')}\n"
            f"observed:    {p.get('observed_count')}\n"
            f"status:      {p.get('status')}\n"
            f"input_params: {p.get('input_params')}\n"
            f"\ndescription:\n{p.get('description')}\n"
            f"\nrationale:\n{p.get('rationale')}\n"
            f"\nexample contexts:\n")
        for c in (p.get("example_contexts") or [])[:5]:
            self._detail_text.insert("end", f"  • {c}\n")
        self._detail_text.config(state="disabled")

    def _on_approve(self) -> None:
        sel = self._proposal_tree.selection()
        if not sel:
            return
        pid = sel[0]
        self._proposal_queue.update_status(pid, "approved")
        messagebox.showinfo(
            "Approved (status only)",
            "Status set to APPROVED.\n\nNo tool was registered. To turn "
            "this proposal into a real tool, implement it in source "
            "(see safe_agent.default_tools) and re-launch the app — that "
            "is the only path that adds a tool to the allow-list.",
            parent=self,
        )
        self._refresh_proposals()

    def _on_dismiss(self) -> None:
        sel = self._proposal_tree.selection()
        if not sel:
            return
        self._proposal_queue.update_status(sel[0], "dismissed")
        self._refresh_proposals()

    def _on_analyze(self) -> None:
        try:
            # Build the analyzer with a RegistryView — it has no register.
            registry_view = None
            try:
                # If the host wired an agent factory, it probably also
                # exposes the registry via a known attribute; we don't
                # require this. The analyzer is happy with default logs +
                # an empty view derived from a throw-away registry.
                # Practical path: callers can override this method to
                # wire their own analyzer.
                agent = self._agent_factory()
                registry_view = agent.registry.view()
            except Exception:
                from tool_registry import ToolRegistry
                tmp = ToolRegistry(); tmp.freeze()
                registry_view = tmp.view()

            analyzer = tool_gap_analyzer.ToolGapAnalyzer(
                registry_view,
                queue=self._proposal_queue,
                conversation_log=self._conversation_log,
                threshold=2,
            )
            report = analyzer.analyze()
            # Second half of the improvement loop: recurring failure
            # signatures (analyst errors, model-load failures, DB test
            # failures) become kind="failure_fix" proposals in the same
            # human-reviewed queue. Deterministic template — no model
            # call from the panel button.
            fail_analyzer = tool_gap_analyzer.FailureAnalyzer(
                queue=self._proposal_queue,
                threshold=3,
            )
            fail_report = fail_analyzer.analyze()
            messagebox.showinfo(
                "Analyze complete",
                f"— tool gaps —\n"
                f"buckets:        {report.bucket_count}\n"
                f"over threshold: {report.over_threshold}\n"
                f"proposals new:  {report.proposals_written}\n"
                f"already listed: {report.skipped_already_listed or '—'}\n"
                f"\n— recurring failures —\n"
                f"signatures:     {fail_report.bucket_count}\n"
                f"over threshold: {fail_report.over_threshold}\n"
                f"proposals new:  {fail_report.proposals_written}",
                parent=self)
            self._refresh_proposals()
        except Exception as exc:
            messagebox.showerror("Analyze failed", repr(exc), parent=self)


def open_panel(parent: Optional[tk.Misc] = None,
               *,
               agent_factory: Callable[[], "safe_agent.ConstrainedAgent"],
               proposal_queue: Optional[tool_gap_analyzer.ProposalQueue] = None,
               conversation_log: Optional[agent_logs.ConversationLog] = None,
               ) -> AgentPanel:
    return AgentPanel(parent, agent_factory=agent_factory,
                      proposal_queue=proposal_queue,
                      conversation_log=conversation_log)
