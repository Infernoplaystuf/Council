"""
council_core.shutdown — what has to happen before the window goes away.

THE QT BUILD WAS NOT DOING ANY OF THIS
`CouncilWindow.on_close` is an empty base method and nothing ever assigned it,
so closing the Qt app skipped every one of the four jobs below. None of them is
visible, which is why it went unnoticed: the symptom is a conversation log with
no end marker, a GPU-crash sentinel that survives a clean run and forces CPU on
the next launch, self-improvement proposals that never accumulate, and pooled
database connections left to socket teardown.

It is all toolkit-free — the Tk handler's only widget line is `self.destroy()`
at the end — so it belongs here and both front ends call it.

EVERY STEP IS INDEPENDENT AND NONE MAY RAISE
Close must not fail. A user who clicks X and gets a traceback, or a window that
refuses to close because an optional analyzer is unhappy, is a worse outcome
than any of these jobs being skipped. So each step is guarded on its own and
the failures are collected and reported rather than raised — the Tk version
swallows them silently, and a report that says which step failed costs nothing.

DETERMINISTIC ONLY, AND THAT IS LOAD-BEARING
The analyzers use templates, not the model. The model may already be unloaded
by this point, and close has to stay fast.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional


@dataclass
class ShutdownReport:
    """What happened on the way out."""
    notes: List[str] = field(default_factory=list)
    failures: List[str] = field(default_factory=list)
    proposals_written: int = 0
    engines_disposed: int = 0

    def lines(self) -> List[str]:
        """What the Tk build printed, as lines a caller can print or log."""
        out = []
        if self.proposals_written:
            out.append(f"[shutdown] self-improvement: {self.proposals_written} "
                       "new proposal(s) drafted — review in the Agent panel "
                       "next launch.")
        if self.engines_disposed:
            out.append(f"[shutdown] disposed {self.engines_disposed} cached "
                       "DB engine(s)")
        return out


def end_conversation_log(conv_logger: Any, reason: str = "user_close") -> Optional[str]:
    """Mark the session closed. Returns a failure description, or None."""
    if not conv_logger:
        return None
    try:
        conv_logger.end_session(reason)
    except Exception as exc:                              # noqa: BLE001
        return f"conversation log: {exc!r}"
    return None


def clear_gpu_sentinel() -> Optional[str]:
    """Record that the GPU survived this run.

    Reaching a clean close means the GPU load and inference did not crash, so
    the sentinel is cleared and the next launch may use the GPU again. A real
    CUDA core dump never reaches this handler, so its sentinel correctly
    survives and forces CPU — that asymmetry is the whole mechanism.
    """
    try:
        import council_engine
        council_engine.gpu_clear_attempt()
    except Exception as exc:                              # noqa: BLE001
        return f"gpu sentinel: {exc!r}"
    return None


def run_analyzers() -> tuple:
    """Aggregate this session's tool gaps and failures into proposals.

    Runs on close so they accumulate without anyone remembering to press the
    panel button. Both analyzers dedup against the queue, so closing the app
    twice never writes the same proposal twice.

    Returns (count, failure-or-None).
    """
    try:
        import tool_gap_analyzer
        from tool_registry import ToolRegistry

        registry = ToolRegistry()
        registry.freeze()
        gaps = tool_gap_analyzer.ToolGapAnalyzer(registry.view(),
                                                 threshold=2).analyze()
        failures = tool_gap_analyzer.FailureAnalyzer(threshold=3).analyze()
        return (gaps.proposals_written + failures.proposals_written), None
    except Exception as exc:                              # noqa: BLE001
        return 0, f"analyzers: {exc!r}"


def dispose_db_engines() -> tuple:
    """Return pooled SQLAlchemy connections cleanly.

    Without this they are closed by socket teardown, which some servers log as
    an error and some connection poolers count against a limit.

    Returns (count, failure-or-None).
    """
    try:
        import db_connections
        return int(db_connections.dispose_engines() or 0), None
    except Exception as exc:                              # noqa: BLE001
        return 0, f"db engines: {exc!r}"


def close_session(conv_logger: Any = None, *,
                  reason: str = "user_close") -> ShutdownReport:
    """Every close-time job, in the Tk build's order, none of them fatal.

    Order matters for the first two: the conversation log is ended before
    anything else so a failure later still leaves a closed session, and the GPU
    sentinel is cleared before the slow analyzers so a hang in them does not
    cost the next launch its GPU.
    """
    report = ShutdownReport()

    for failure in (end_conversation_log(conv_logger, reason),
                    clear_gpu_sentinel()):
        if failure:
            report.failures.append(failure)

    report.proposals_written, failure = run_analyzers()
    if failure:
        report.failures.append(failure)

    report.engines_disposed, failure = dispose_db_engines()
    if failure:
        report.failures.append(failure)

    return report
