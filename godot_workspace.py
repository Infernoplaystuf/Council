"""
godot_workspace.py — open-Godot-project state + Run/Validate orchestration.

This module owns the *project-level* model that the Godot Workspace tab
binds to:

  • Which folder is the current Godot project (contains project.godot)
  • Which Godot binary to invoke
  • The dirty-file set and the save-on-run policy
  • The Run / Validate subprocess lifecycle and stdout/stderr capture

It deliberately knows nothing about Tk or the council. The Tk tab
holds the references; the council subscribes to events emitted here
so a Godot stderr line can become a deliberation trigger.

PHASE A: stubs only. No subprocess yet.
PHASE C-lite (next): land project detection, GodotRunner subprocess
helper, console-line callback, and the GODOT_BINARY environment +
backend-settings persistence so the Workspace tab can drive a real
edit-test-visualise loop.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional


# ============================================================
# Environment / settings — read by phase C-lite
# ============================================================

#: Override for the Godot binary path. When unset, GodotRunner falls
#: back to whichever `godot` is on PATH. Set during onboarding and
#: persisted in vault/backend_settings.json under key "godot_path".
GODOT_BINARY_ENV = "ANVIL_GODOT_BINARY"


def get_godot_binary() -> str:
    """Return the configured Godot binary path or "godot" as fallback."""
    return os.environ.get(GODOT_BINARY_ENV, "godot")


# ============================================================
# Project model
# ============================================================

@dataclass
class GodotProject:
    """Snapshot of an open Godot project.

    Built by ``open_project(path)`` once the path is confirmed to
    contain a ``project.godot`` manifest. The workspace tab uses it
    to drive the file tree, the editor, and the Run button.
    """
    root:        Path
    name:        str = ""
    main_scene:  str = ""           # e.g. "res://scenes/main.tscn"
    scripts:     List[Path] = field(default_factory=list)
    scenes:      List[Path] = field(default_factory=list)
    autoloads:   List[str] = field(default_factory=list)

    @property
    def manifest_path(self) -> Path:
        return self.root / "project.godot"


def open_project(path: Any) -> Optional[GodotProject]:
    """Probe ``path`` for a Godot 4.x project manifest.

    Returns a populated ``GodotProject`` or ``None`` if the folder
    is not a Godot project. PHASE C-lite will land the real parse —
    until then this is a stub that just confirms the manifest exists.
    """
    p = Path(path).expanduser().resolve()
    manifest = p / "project.godot"
    if not manifest.exists():
        return None
    return GodotProject(root=p, name=p.name)


# ============================================================
# Runner — phase C-lite
# ============================================================

class GodotRunner:
    """Subprocess wrapper around the Godot binary.

    Responsibilities (to be implemented in phase C-lite):

      • ``run(project)`` — launch the project's main scene and stream
        stdout / stderr through ``on_line(stream, text)`` callbacks
        so the Workspace console + the council can both observe.
      • ``validate(project)`` — invoke ``godot --headless --check-only
        project.godot`` for a fast parse-only check.
      • ``stop()`` — terminate the running subprocess cleanly so the
        user can stop a runaway scene without killing Anvil.

    Designed so the Tk tab never sees subprocess details; it only
    deals with line callbacks and lifecycle events.
    """

    def __init__(
        self,
        on_line: Optional[Callable[[str, str], None]] = None,
        on_exit: Optional[Callable[[int], None]] = None,
    ):
        self.on_line = on_line or (lambda stream, text: None)
        self.on_exit = on_exit or (lambda rc: None)
        self._proc = None     # subprocess.Popen — phase C-lite

    def run(self, project: GodotProject) -> None:
        raise NotImplementedError("GodotRunner.run lands in phase C-lite")

    def validate(self, project: GodotProject) -> None:
        raise NotImplementedError("GodotRunner.validate lands in phase C-lite")

    def stop(self) -> None:
        raise NotImplementedError("GodotRunner.stop lands in phase C-lite")
