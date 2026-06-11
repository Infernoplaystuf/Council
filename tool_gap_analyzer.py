"""
tool_gap_analyzer.py — recurring tool gaps → reviewable proposals.

Two-phase analysis:

  1. ``aggregate(gap_log)`` — PURE PYTHON. Counts gap-log records by
     normalised intent, captures example contexts. Deterministic,
     model-free, fully unit-tested.

  2. ``analyze(runner, …)`` — uses the LOCAL model to draft a structured
     ``ToolProposal`` per gap that meets the frequency threshold (default
     2). Writes proposals to ``ProposalQueue.append(...)``. Reads a
     ``ConversationLog`` for context enrichment.

Hard invariants enforced by tests:

    The analyzer NEVER registers a tool.
    The analyzer NEVER imports model-authored code.
    The analyzer NEVER calls exec / eval / subprocess.
    The analyzer NEVER opens a non-loopback socket.

The analyzer takes a ``RegistryView`` (read-only) — not a
``ToolRegistry`` — so even reaching for ``register`` is a typed
impossibility from inside this module.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import agent_logs
import safe_agent

_LOG = logging.getLogger("tool_gap_analyzer")


# ============================================================
# Aggregation (pure)
# ============================================================

def _normalize_name(name: str) -> str:
    """Trivial normalisation — strip whitespace, lower-case. Keeps
    similar requests (``Send_Email`` / ``send_email`` ) in one bucket."""
    return (name or "").strip().lower()


@dataclass
class GapBucket:
    """One bucket from ``aggregate``."""
    normalized_name: str
    requested_variants: List[str] = field(default_factory=list)
    count: int = 0
    args_shapes: List[Dict[str, str]] = field(default_factory=list)
    sample_contexts: List[Dict[str, Any]] = field(default_factory=list)
    first_seen_ts: int = 0
    last_seen_ts: int = 0


def aggregate(gap_records: Iterable[Dict[str, Any]],
              *,
              context_sample_max: int = 5) -> Dict[str, GapBucket]:
    """Group gap-log records by normalised name. Pure, deterministic.

    Returns a dict keyed by normalized_name. Each bucket carries:
      • count                       how many times it was requested
      • requested_variants          unique original spellings (order-preserved)
      • args_shapes                 unique {field: type} shapes observed
      • sample_contexts             up to context_sample_max example tasks
      • first_seen_ts, last_seen_ts in milliseconds
    """
    buckets: Dict[str, GapBucket] = {}
    for rec in gap_records:
        raw = str(rec.get("requested_name", ""))
        key = _normalize_name(raw)
        if not key:
            continue
        b = buckets.get(key)
        if b is None:
            b = GapBucket(normalized_name=key)
            buckets[key] = b
        b.count += 1
        if raw not in b.requested_variants:
            b.requested_variants.append(raw)
        shape = rec.get("args_shape") or {}
        if isinstance(shape, dict) and shape not in b.args_shapes:
            b.args_shapes.append(dict(shape))
        ts = int(rec.get("ts", 0))
        if not b.first_seen_ts or ts < b.first_seen_ts:
            b.first_seen_ts = ts
        if ts > b.last_seen_ts:
            b.last_seen_ts = ts
        if len(b.sample_contexts) < context_sample_max:
            b.sample_contexts.append({
                "task":         str(rec.get("task", "")),
                "step":         int(rec.get("step", 0)),
                "args_sample":  rec.get("args_sample") or {},
                "context":      rec.get("context") or {},
            })
    return buckets


# ============================================================
# Proposal queue
# ============================================================

@dataclass
class ToolProposal:
    proposed_name: str
    description: str
    input_params: Dict[str, str]
    output: str
    rationale: str
    observed_count: int
    example_contexts: List[Dict[str, Any]] = field(default_factory=list)
    requested_variants: List[str] = field(default_factory=list)
    args_shapes: List[Dict[str, str]] = field(default_factory=list)
    ts: int = 0
    proposal_id: str = ""
    status: str = "pending"     # "pending" | "approved" | "dismissed"
    # "tool_gap"     — the model asked for an unlisted tool (original flow)
    # "failure_fix"  — a recurring failure signature suggests a fix/tool
    kind: str = "tool_gap"


class ProposalQueue:
    """Append-only JSONL store of ToolProposal records. The UI flips
    ``status`` via ``update_status`` — a text-only mutation that does
    NOT touch the tool registry. There is no code path in this class
    that registers a tool."""

    DEFAULT_FILENAME = ".tool_proposals.jsonl"

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else (
            agent_logs._vault_root() / self.DEFAULT_FILENAME)
        self._lock = threading.Lock()

    @classmethod
    def default(cls) -> "ProposalQueue":
        return cls()

    def append(self, proposal: ToolProposal) -> ToolProposal:
        if not proposal.ts:
            proposal.ts = agent_logs._now_ms()
        if not proposal.proposal_id:
            proposal.proposal_id = f"prop-{proposal.ts}-{proposal.proposed_name}"
        rec = asdict(proposal)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return proposal

    def all(self) -> List[Dict[str, Any]]:
        # Locked so a concurrent append's half-flushed final line is
        # never observed (the JSONL iterator would skip it silently).
        with self._lock:
            return list(agent_logs._iter_jsonl(self.path))

    def update_status(self, proposal_id: str, status: str) -> bool:
        """Flip a proposal's status. Append-only — we don't rewrite past
        records; instead we append a status-change marker line that
        readers compose with newest-wins logic in ``current_status()``.

        Crucially: this method changes ONLY a status field. It does
        NOT register a tool, write a Python file, or do anything else.
        """
        if status not in ("pending", "approved", "dismissed"):
            raise ValueError(f"unknown status: {status!r}")
        marker = {
            "_kind":        "status_change",
            "proposal_id":  proposal_id,
            "status":       status,
            "ts":           agent_logs._now_ms(),
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(marker, ensure_ascii=False) + "\n")
        return True

    def current_status(self) -> List[Dict[str, Any]]:
        """Compose the append-only log into a current-state list:
        one entry per proposal_id with its latest status."""
        proposals: Dict[str, Dict[str, Any]] = {}
        for rec in self.all():
            if rec.get("_kind") == "status_change":
                pid = rec.get("proposal_id")
                if pid in proposals:
                    proposals[pid]["status"] = rec.get("status", "pending")
                    proposals[pid]["_status_ts"] = rec.get("ts", 0)
                continue
            pid = rec.get("proposal_id")
            if pid and pid not in proposals:
                proposals[pid] = dict(rec)
        return list(proposals.values())


# ============================================================
# Analyzer
# ============================================================

@dataclass
class AnalyzeReport:
    bucket_count: int = 0
    over_threshold: int = 0
    proposals_written: int = 0
    skipped_already_listed: List[str] = field(default_factory=list)


class ToolGapAnalyzer:
    """Drive aggregation + (optional) model-drafted proposal generation.

    The analyzer takes a ``registry_view`` (a ``RegistryView``) — NOT a
    ``ToolRegistry``. This means even reaching for ``register()`` from
    inside this module would be a typed impossibility.
    """

    def __init__(self,
                 registry_view,
                 *,
                 gap_log: Optional[agent_logs.ToolGapLog] = None,
                 conversation_log: Optional[agent_logs.ConversationLog] = None,
                 queue: Optional[ProposalQueue] = None,
                 threshold: int = 2) -> None:
        from tool_registry import RegistryView
        if not isinstance(registry_view, RegistryView):
            raise TypeError(
                "ToolGapAnalyzer requires a RegistryView, NOT a "
                "ToolRegistry. Pass registry.view() so the analyzer has "
                "no access to register()/freeze().")
        self.registry_view = registry_view
        self.gap_log = gap_log or agent_logs.ToolGapLog.default()
        self.conversation_log = conversation_log or agent_logs.ConversationLog.default()
        self.queue = queue or ProposalQueue.default()
        self.threshold = int(threshold)

    def aggregate(self) -> Dict[str, GapBucket]:
        return aggregate(self.gap_log.all())

    def analyze(self,
                runner=None,
                *,
                dry_run: bool = False) -> AnalyzeReport:
        """Aggregate the gap log; for each bucket at or above threshold
        whose name isn't already a registered tool, draft a proposal.

        ``runner`` is an inferno_local ModelRunner. When omitted, the
        analyzer falls back to a deterministic template so the proposal
        queue still gets populated on an air-gapped box without a model.

        ``dry_run=True`` builds the proposals but does NOT write them.
        """
        report = AnalyzeReport()
        buckets = self.aggregate()
        report.bucket_count = len(buckets)

        # Names that already have a proposal in the queue (any status) —
        # re-running analyze() must not append a duplicate row per run.
        existing_names = {
            str(p.get("proposed_name", "")).strip().lower()
            for p in self.queue.current_status()
        }

        for name, b in buckets.items():
            if b.count < self.threshold:
                continue
            report.over_threshold += 1
            if self.registry_view.has(name):
                # Already listed — no proposal needed.
                report.skipped_already_listed.append(name)
                continue
            if name in existing_names:
                # Already proposed — human review pending or done.
                continue
            proposal = self._draft_proposal(b, runner=runner)
            if dry_run:
                continue
            try:
                self.queue.append(proposal)
                report.proposals_written += 1
            except Exception as exc:
                _LOG.warning("queue append failed: %r", exc)
        return report

    # ── proposal drafting ───────────────────────────────────
    def _draft_proposal(self, bucket: GapBucket,
                        *, runner=None) -> ToolProposal:
        """Build a ToolProposal. If a runner is supplied we ask the
        LOCAL model for a description + rationale; otherwise we use a
        deterministic template. Either way we ONLY emit data — no
        executable code is generated by the analyzer."""
        # Merge observed shapes into a parameter spec (field: type-union).
        params: Dict[str, str] = {}
        for shape in bucket.args_shapes:
            for k, v in shape.items():
                if k not in params:
                    params[k] = str(v)
                elif params[k] != v:
                    # Track a union when the model used different types
                    parts = sorted(set(params[k].split("|") + [v]))
                    params[k] = "|".join(parts)

        recent_convo = self.conversation_log.all()[-10:]

        description = ""
        rationale = ""
        if runner is not None:
            try:
                description, rationale = self._ask_model(
                    runner, bucket, params, recent_convo)
            except Exception as exc:
                _LOG.warning("model-drafted proposal failed for %s: %r",
                             bucket.normalized_name, exc)

        if not description:
            description = (f"A tool the model wanted to call as "
                           f"{bucket.normalized_name!r} but is not "
                           f"registered. Observed {bucket.count} times.")
        if not rationale:
            rationale = (f"Observed in {bucket.count} agent runs across "
                         f"{len(bucket.sample_contexts)} distinct tasks. "
                         "Human review needed.")

        return ToolProposal(
            proposed_name=bucket.normalized_name,
            description=description,
            input_params=dict(params),
            output="(to be defined by the human implementing this tool)",
            rationale=rationale,
            observed_count=bucket.count,
            example_contexts=list(bucket.sample_contexts),
            requested_variants=list(bucket.requested_variants),
            args_shapes=list(bucket.args_shapes),
        )

    def _ask_model(self, runner, bucket: GapBucket,
                   params: Dict[str, str],
                   convo_tail: List[Dict[str, Any]]) -> tuple:
        """Use the runner to produce {description, rationale} as JSON.

        Important: we ASK for a description and rationale. We never
        ask the model to emit Python code, and we do not parse what it
        emits as code. Even if the model returns code, this method
        treats it as opaque text — at most it appears inside the
        rationale string in the queue file for human reading.
        """
        sample_tasks = "; ".join(c["task"]
                                  for c in bucket.sample_contexts
                                  if c.get("task"))
        recent_summary = "; ".join(
            f"{rec.get('task', '')[:60]}"
            for rec in convo_tail
            if rec.get("task")
        )
        prompt = (
            "You are reviewing recurring tool requests from a constrained "
            "agent. Produce a JSON object with exactly two fields:\n"
            '  {"description": "<one paragraph>", '
            '"rationale": "<one paragraph>"}\n'
            "Do NOT emit code. Do NOT emit anything other than the JSON "
            "object.\n\n"
            f"REQUESTED TOOL: {bucket.normalized_name}\n"
            f"OBSERVED COUNT: {bucket.count}\n"
            f"PARAMETERS: {json.dumps(params)}\n"
            f"SAMPLE TASKS: {sample_tasks}\n"
            f"RECENT CONVERSATION TASKS: {recent_summary}\n"
        )
        reply = runner.chat(
            [{"role": "user", "content": prompt}],
            temperature=0.2, max_tokens=400,
        )
        # Reuse the safe parser; we only want a JSON object back.
        for s, e in safe_agent._iter_brace_balanced(reply or ""):
            try:
                obj = json.loads(reply[s:e])
            except Exception:
                continue
            if isinstance(obj, dict) and "description" in obj and "rationale" in obj:
                return str(obj["description"])[:1500], str(obj["rationale"])[:1500]
        return "", ""


# ============================================================
# Failure analysis — the other half of the improvement loop
# ============================================================
#
# The ToolGapLog captures "the model wanted a tool that doesn't exist."
# The FailureLog (agent_logs.FailureLog) captures "something the app
# tried to do FAILED" — analyst code-gen errors, sandbox rejections,
# model-load failures, DB connection errors. Recurring signatures are
# aggregated here and drafted into the SAME ProposalQueue the human
# already reviews in the Agent panel, tagged kind="failure_fix".
#
# Same cardinal rule as tool gaps, enforced by the same tests:
# identification is automatic; CHANGE is human-gated. Nothing in this
# module registers tools, edits source, or executes model output.

@dataclass
class FailureBucket:
    """One recurring failure signature from ``aggregate_failures``."""
    signature: str
    count: int = 0
    kinds: List[str] = field(default_factory=list)
    subsystems: List[str] = field(default_factory=list)
    sample_messages: List[str] = field(default_factory=list)
    sample_details: List[str] = field(default_factory=list)
    first_seen_ts: int = 0
    last_seen_ts: int = 0


def aggregate_failures(failure_records: Iterable[Dict[str, Any]],
                       *,
                       sample_max: int = 3) -> Dict[str, FailureBucket]:
    """Group failure-log records by normalised signature. Pure,
    deterministic, model-free — mirror of ``aggregate`` for gaps."""
    buckets: Dict[str, FailureBucket] = {}
    for rec in failure_records:
        sig = str(rec.get("signature", "")).strip()
        if not sig:
            continue
        b = buckets.get(sig)
        if b is None:
            b = FailureBucket(signature=sig)
            buckets[sig] = b
        b.count += 1
        kind = str(rec.get("kind", ""))
        if kind and kind not in b.kinds:
            b.kinds.append(kind)
        sub = str(rec.get("subsystem", ""))
        if sub and sub not in b.subsystems:
            b.subsystems.append(sub)
        ts = int(rec.get("ts", 0))
        if not b.first_seen_ts or ts < b.first_seen_ts:
            b.first_seen_ts = ts
        if ts > b.last_seen_ts:
            b.last_seen_ts = ts
        msg = str(rec.get("message", ""))
        if msg and len(b.sample_messages) < sample_max and msg not in b.sample_messages:
            b.sample_messages.append(msg)
        det = str(rec.get("detail", ""))
        if det and len(b.sample_details) < sample_max:
            b.sample_details.append(det[:600])
    return buckets


class FailureAnalyzer:
    """Aggregate the FailureLog; draft a kind="failure_fix" proposal for
    each signature at/above threshold that isn't already queued.

    ``runner`` (optional, an inferno_local ModelRunner) lets the LOCAL
    model write the human-facing description + suggested direction.
    Without one, a deterministic template keeps the loop alive on
    air-gapped boxes. Either way the output is DATA for human review —
    never code, never an automatic change."""

    def __init__(self,
                 *,
                 failure_log=None,
                 queue: Optional[ProposalQueue] = None,
                 threshold: int = 3) -> None:
        self.failure_log = failure_log or agent_logs.FailureLog.default()
        self.queue = queue or ProposalQueue.default()
        self.threshold = int(threshold)

    def aggregate(self) -> Dict[str, FailureBucket]:
        return aggregate_failures(self.failure_log.all())

    def analyze(self, runner=None, *, dry_run: bool = False) -> AnalyzeReport:
        report = AnalyzeReport()
        buckets = self.aggregate()
        report.bucket_count = len(buckets)

        existing_names = {
            str(p.get("proposed_name", "")).strip().lower()
            for p in self.queue.current_status()
        }

        for sig, b in buckets.items():
            if b.count < self.threshold:
                continue
            report.over_threshold += 1
            if sig.strip().lower() in existing_names:
                continue
            proposal = self._draft_proposal(b, runner=runner)
            if dry_run:
                continue
            try:
                self.queue.append(proposal)
                report.proposals_written += 1
            except Exception as exc:
                _LOG.warning("failure-proposal append failed: %r", exc)
        return report

    def _draft_proposal(self, bucket: FailureBucket,
                        *, runner=None) -> ToolProposal:
        description = ""
        rationale = ""
        if runner is not None:
            try:
                description, rationale = self._ask_model(runner, bucket)
            except Exception as exc:
                _LOG.warning("model-drafted failure proposal failed for "
                             "%s: %r", bucket.signature, exc)
        if not description:
            description = (
                f"Recurring failure ({bucket.count}x) in "
                f"{', '.join(bucket.subsystems) or 'unknown subsystem'}: "
                f"{bucket.sample_messages[0] if bucket.sample_messages else bucket.signature}"
            )
        if not rationale:
            rationale = (
                f"Signature observed {bucket.count} times "
                f"(kinds: {', '.join(bucket.kinds) or '?'}). A fix, a "
                "guard, or a purpose-built helper tool would remove this "
                "failure class. Human review needed."
            )
        return ToolProposal(
            proposed_name=bucket.signature,
            description=description,
            input_params={},
            output="(fix / guard / helper tool — defined by the human reviewer)",
            rationale=rationale,
            observed_count=bucket.count,
            example_contexts=[{"message": m} for m in bucket.sample_messages]
                             + [{"detail": d} for d in bucket.sample_details],
            requested_variants=list(bucket.kinds),
            args_shapes=[],
            kind="failure_fix",
        )

    def _ask_model(self, runner, bucket: FailureBucket) -> tuple:
        """Local-model drafting. Same contract as ToolGapAnalyzer._ask_model:
        we ask for prose JSON, never code, and treat the reply as opaque
        text for the human reviewer."""
        prompt = (
            "You are reviewing a recurring failure in a local data-analysis "
            "app. Produce a JSON object with exactly two fields:\n"
            '  {"description": "<one paragraph: what is failing>", '
            '"rationale": "<one paragraph: what kind of fix or helper tool '
            'would prevent it>"}\n'
            "Do NOT emit code. Do NOT emit anything other than the JSON "
            "object.\n\n"
            f"FAILURE SIGNATURE: {bucket.signature}\n"
            f"OBSERVED COUNT: {bucket.count}\n"
            f"SUBSYSTEMS: {', '.join(bucket.subsystems)}\n"
            f"SAMPLE MESSAGES: {' | '.join(bucket.sample_messages)}\n"
        )
        reply = runner.chat(
            [{"role": "user", "content": prompt}],
            temperature=0.2, max_tokens=400,
        )
        for s, e in safe_agent._iter_brace_balanced(reply or ""):
            try:
                obj = json.loads(reply[s:e])
            except Exception:
                continue
            if isinstance(obj, dict) and "description" in obj and "rationale" in obj:
                return str(obj["description"])[:1500], str(obj["rationale"])[:1500]
        return "", ""
