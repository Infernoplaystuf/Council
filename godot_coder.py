"""
godot_coder.py — GDScript-targeted variant of CoderAgent.

The existing ``coder_agent.CoderAgent`` is a self-correcting ReAct loop
that writes Python, executes it, reads the traceback, and reflects.
For Godot we want the same shape but:

  • Output is GDScript (or a .tscn fragment), not Python
  • "Execute" means ``godot --headless --check-only`` against a
    throwaway scene that includes the generated script
  • "Stderr" parsing knows GDScript-specific error patterns
    (``Parse Error: …``, ``Invalid get index 'foo' on base: 'Node'``,
    autoload-resolution failures)
  • The FIRST_ATTEMPT / FIX prompts are GDScript-shaped — Godot API
  • Goal anchor (from ``goal_anchor``) is threaded the same way as
    in ``coder_agent`` so retries stay locked on intent

PHASE A: stub. PHASE C-lite will land the full ReAct loop and the
GDScript-aware prompts + error parser.

Why a separate module rather than a subclass:
  • Prompt templates are substantially different
  • The "runner" is Godot, not the local Python interpreter
  • Validation step lives in godot_pipeline.validate_project, not
    in ``runner.workspace``
A class hierarchy would force one side to bend; two siblings keep
each one readable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional


@dataclass
class GodotAgentState:
    """ReAct loop state for GDScript generation."""
    task:          str
    goal:          str = ""               # distilled user-intent anchor
    filename:      str = "main.gd"
    code:          str = ""
    stdout:        str = ""
    stderr:        str = ""
    returncode:    int = -1
    attempt:       int = 0
    max_attempts:  int = 8
    passed:        bool = False
    history:       List[dict] = field(default_factory=list)
    final_code:    str = ""
    explanation:   str = ""


class GodotCoder:
    """GDScript ReAct loop. PHASE C-lite implementation pending.

    Public shape (so the Godot Workspace tab can be wired against
    a stable signature before C-lite lands):

        agent = GodotCoder(personality_model, project_root, on_event)
        state = agent.run(task, goal="")
        if state.passed:
            project_root / state.filename ← state.final_code
    """

    def __init__(
        self,
        personality_model: Any,
        project_root: Any,
        max_attempts: int = 8,
        event_callback: Optional[Callable[[str, str], None]] = None,
    ):
        self.model = personality_model
        self.project_root = Path(project_root)
        self.max_attempts = max_attempts
        self.event_callback = event_callback

    def run(self, task: str, goal: str = "") -> GodotAgentState:
        raise NotImplementedError("GodotCoder.run lands in phase C-lite")
