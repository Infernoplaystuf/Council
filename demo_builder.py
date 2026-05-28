"""
demo_builder.py — turn a game concept into a runnable Godot skeleton.

Takes an ``IdeaItem`` from ``idea_engine`` (retargeted for game
concepts in phase B) and produces a Godot project directory with:

  • project.godot manifest
  • A main scene (.tscn) with placeholder nodes appropriate to genre
  • A main.gd script stub with TODO markers at the right places
  • Optional placeholder assets generated via ``image_engine`` and
    ``music_renderer`` (sprite mock, looping ambient track)
  • A README.md describing the concept and what each stub does

Output lives at ``vault/projects/<slug>/`` so the user can open it
straight in the Godot Workspace tab and hit Run.

PHASE A: stub. PHASE E will land:

  • Genre → scene-skeleton mapping (platformer / top-down / puzzle /
    visual-novel / etc.)
  • GodotCoder integration for filling in TODO stubs from the concept
  • Optional headless validate before declaring the demo "ready"
  • A "send to Workspace" handoff that opens the new project tab
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class DemoBuildResult:
    """Outcome of one demo build."""
    project_path:    Optional[Path] = None
    files_written:   List[Path] = field(default_factory=list)
    placeholder_assets: List[Path] = field(default_factory=list)
    notes:           List[str] = field(default_factory=list)
    error:           str = ""


# ============================================================
# Public API — phase E
# ============================================================

def build_demo(
    concept: Any,            # idea_engine.IdeaItem (retargeted in phase B)
    vault_dir: Any,
    *,
    include_assets: bool = True,
) -> DemoBuildResult:
    """Generate a Godot skeleton for ``concept`` under
    ``vault_dir/projects/<slug>/``.

    PHASE E. Returns an empty result for now.
    """
    return DemoBuildResult(
        error="build_demo: stub — phase E",
    )
