"""Every call a member makes within one question starts with the same text
(council_engine.PersonalityModel._stitched), so a server that caches the
prompt prefix (Ollama keeps the last prompt's KV cache per loaded model)
skips re-reading the member's memory, history and the question's standing
context on each of its calls."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import council_engine as ce  # noqa: E402

STANDING = ("[TASK MEMO]\ngoal: size the pump\n\nVAULT CONTEXT:\n[1] manual.md"
            "\nThe pump must be primed before every start of the day.")


def _model(role):
    m = ce.PersonalityModel(name=role, system_prompt="sys", weights={},
                            registry=None, trace=False)
    m.extra_context = STANDING
    return m


def _common(a: str, b: str) -> str:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return a[:n]


def test_the_standing_context_comes_before_anything_per_call(monkeypatch):
    monkeypatch.setenv("COUNCIL_DEMO_SILO", "")
    for role in ("writer", "coder", "sage"):
        m = _model(role)
        draft = m._stitched("USER REQUEST:\nhow big a pump?")
        rebut = m._stitched("Produce your rebuttal now.",
                            "DEBATE CONTEXT:\nother answers …")
        cross = m._stitched("Post your cross-fire message now.",
                            "CROSS-FIRE CONTEXT — Turn 1/2 …")
        shared = _common(_common(draft, rebut), cross)
        assert "The pump must be primed" in shared, role
        assert "[TASK MEMO]" in shared, role
        # And nothing that changes between calls is inside it.
        assert "DEBATE" not in shared and "CROSS-FIRE" not in shared
