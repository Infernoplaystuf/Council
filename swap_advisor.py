"""
swap_advisor.py — decide whether to SUGGEST a different model for a task.

The app's default model is a generalist. Some tasks (writing code,
heavy reasoning) are served better by a specialist. But switching has a
cost: a local model swap reloads weights (~seconds) and resets the warm
KV cache. This module decides whether the expected benefit is worth that
cost and, if so, returns a suggestion the UI shows the user to confirm —
it never switches anything itself.

A suggested target can be:
  • LOCAL  — a specialist GGUF assigned to the role (GPU-gated swap,
    ~seconds reload), or
  • REMOTE — a registered node already running that model (no local
    reload, no VRAM cost — it runs on another machine), which makes
    "switch" nearly free, so the advisor prefers a remote specialist
    when one is available.

The advisor is PURE (no I/O, no model loads) so it's fully testable; the
caller passes in the current state (assignments, reachable nodes, GPU).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# Task → specialist role, by signal words. Kept deliberately tight: a
# weak/ambiguous match should NOT trigger a swap prompt. Note 'data'
# tasks are intentionally absent — those route to the deterministic
# analyst (pandas/stats cache), not a different LLM.
_ROLE_SIGNALS: Dict[str, List[str]] = {
    "coder": [
        "function", "write code", "python", "javascript", "typescript",
        "regex", "stack trace", "traceback", "refactor", "unit test",
        "compile", "debug", "sql query", "bug in", "syntax error",
        "class that", "def ", "import ", "api endpoint", "code review",
        "script to", "rust", "golang", " java ", "c++",
    ],
    "reasoning": [
        "prove", "derive", "step by step", "logic puzzle", "math proof",
        "optimi", "algorithm", "complexity", "trade-off analysis",
        "plan a multi-step", "reason through", "chain of thought",
    ],
}

# Roughly how long a local model reload costs (weights load + KV alloc).
DEFAULT_LOCAL_SWAP_SECONDS = 6.0
# Don't suggest a LOCAL swap that costs more than this for a short task.
_LOCAL_SWAP_MAX_SECONDS = 12.0


@dataclass
class SwapSuggestion:
    role: str                 # the specialist role the task matches
    target_model: str         # model id/path/name to switch to
    target_kind: str          # "local" | "remote"
    target_label: str         # human label (e.g. "coder model on pi-01")
    reason: str               # why it would help — shown to the user
    est_cost: str             # human cost estimate ("~6s reload" / "no reload")
    confidence: float         # 0..1, strength of the task→role match


def classify_task(query: str) -> Optional[str]:
    """Return the specialist role a query clearly matches, or None when
    the generalist is fine. Requires a real signal — ambiguous prose
    returns None so we don't nag the user with swap prompts."""
    q = (query or "").lower()
    best_role, best_hits = None, 0
    for role, signals in _ROLE_SIGNALS.items():
        hits = sum(1 for s in signals if s in q)
        if hits > best_hits:
            best_role, best_hits = role, hits
    # Need a clear signal: 2+ hits, OR 1 hit in a short, on-point query.
    if best_hits >= 2 or (best_hits == 1 and len(q.split()) <= 12):
        return best_role
    return None


def advise(query: str, *,
           current_model: str,
           role_assignments: Optional[Dict[str, str]] = None,
           remote_specialists: Optional[Dict[str, Dict[str, Any]]] = None,
           gpu_swap_enabled: bool = False,
           local_swap_seconds: float = DEFAULT_LOCAL_SWAP_SECONDS,
           ) -> Optional[SwapSuggestion]:
    """Decide whether to suggest a model switch for ``query``.

    Inputs (all passed in — pure):
      • current_model       — id/path of the loaded model.
      • role_assignments    — {role: local_gguf_path} (from RoleModelRegistry).
      • remote_specialists  — {role: {"model":.., "node":.., "label":..}}
                              for roles served by a reachable remote node.
      • gpu_swap_enabled    — whether a LOCAL swap is allowed right now.
      • local_swap_seconds  — estimated reload cost for a local swap.

    Returns a SwapSuggestion or None. Preference order when a task
    matches a specialist role:
      1. a REMOTE specialist (no local reload / VRAM hit — nearly free),
      2. a LOCAL specialist, only if GPU swap is on AND the reload cost
         is within budget (a multi-second reload isn't worth it for a
         quick question).
    """
    role = classify_task(query)
    if role is None:
        return None
    role_assignments = role_assignments or {}
    remote_specialists = remote_specialists or {}
    n_words = len((query or "").split())
    # Strong, on-point queries earn a higher confidence.
    confidence = 0.9 if n_words <= 20 else 0.7

    # 1) Remote specialist — cheapest "switch" (runs elsewhere).
    rem = remote_specialists.get(role)
    if rem and rem.get("model") and rem.get("model") != current_model:
        return SwapSuggestion(
            role=role, target_model=str(rem["model"]), target_kind="remote",
            target_label=str(rem.get("label")
                              or f"{role} model on {rem.get('node', 'a node')}"),
            reason=(f"This looks like a {role} task. A {role}-specialist model "
                    f"is ready on {rem.get('node', 'another machine')} — using it "
                    "runs on that machine, so there's no reload or memory cost here."),
            est_cost="no local reload (runs on the remote node)",
            confidence=confidence,
        )

    # 2) Local specialist — only worth it on GPU and within a time budget.
    local = role_assignments.get(role)
    if local and local != current_model:
        if not gpu_swap_enabled:
            return None        # swapping on CPU isn't worth the reload
        if local_swap_seconds > _LOCAL_SWAP_MAX_SECONDS:
            return None
        return SwapSuggestion(
            role=role, target_model=str(local), target_kind="local",
            target_label=f"your {role} model",
            reason=(f"This looks like a {role} task and you've assigned a "
                    f"{role}-specialist model. Switching should give a better "
                    "answer — at the cost of a brief reload and losing the "
                    "current model's warm context."),
            est_cost=f"~{local_swap_seconds:.0f}s reload",
            confidence=confidence,
        )

    return None
