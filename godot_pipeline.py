"""
godot_pipeline.py — parse and validate Godot project artefacts.

Analog to ``pipeline_scanner.py`` (which targeted Dream3D) but for the
``.tscn`` / ``.gd`` / ``project.godot`` file format. Used by the Godot
Workspace tab to:

  • Render a scene-tree view from a ``.tscn`` without launching Godot
  • Surface broken ``ext_resource`` / ``preload`` references statically
  • Feed the Game Designer specialist a structured description of the
    project's nodes, scripts, autoloads, and signal connections
  • Run a quick pre-Run static validation pass so obviously broken
    edits don't cost a full Godot launch to discover

PHASE A: stubs only. PHASE C-lite will land the .tscn parser and the
godot --headless --check-only invocation.

Why parse .tscn ourselves instead of always shelling out to Godot:
the headless validator is slow (~1s cold start) and we want a
sub-100ms feedback loop on every save for the editor's hint panel.
The shell-out is the *fallback* / authoritative check; the in-process
parser is the live linter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ============================================================
# Scene-tree model
# ============================================================

@dataclass
class SceneNode:
    """One node in a parsed .tscn scene tree."""
    name:        str
    type:        str            # e.g. "Node2D", "CharacterBody2D"
    parent:      Optional[str] = None       # "." for root
    properties:  Dict[str, Any] = field(default_factory=dict)
    script_path: str = ""                   # ext_resource → script
    children:    List["SceneNode"] = field(default_factory=list)


@dataclass
class ParsedScene:
    """Result of ``parse_scene(path)``."""
    path:         Path
    root:         Optional[SceneNode] = None
    ext_resources: List[Tuple[str, str]] = field(default_factory=list)   # (id, path)
    issues:       List[str] = field(default_factory=list)


# ============================================================
# Parsers — phase C-lite
# ============================================================

def parse_scene(path: Any) -> ParsedScene:
    """Parse a .tscn file into a SceneNode tree + ext_resource list.

    PHASE C-lite. Returns a ParsedScene whose ``root`` is None until
    then, but the path is recorded so callers can stub-render.
    """
    return ParsedScene(path=Path(path), issues=["parse_scene: stub — phase C-lite"])


def parse_script(path: Any) -> Dict[str, Any]:
    """Pull a structural summary out of a .gd file.

    Returns ``{"class_name": str, "extends": str, "signals": [...],
    "funcs": [{"name": ..., "args": [...]}], "exports": [...]}``.
    Used by the editor's hint panel and the Game Designer specialist.
    PHASE C-lite.
    """
    return {"_stub": True, "path": str(path)}


# ============================================================
# Validation — phase C-lite
# ============================================================

@dataclass
class ValidationIssue:
    """One problem found by the validator."""
    severity:  str          # "error" | "warning" | "info"
    file:      str
    line:      int = 0
    message:   str = ""


def validate_project(project_root: Any) -> List[ValidationIssue]:
    """Static validation pass over an entire Godot project.

    Combines:
      • Per-scene parse (broken ext_resource paths, missing parent
        nodes referenced by NodePath)
      • Per-script parse (syntax via godot --check-only, missing
        signals connected in scenes)
      • Autoload manifest cross-check

    PHASE C-lite. Returns an empty list for now.
    """
    return []
