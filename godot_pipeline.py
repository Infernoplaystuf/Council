"""
godot_pipeline.py — parse and validate Godot project artefacts.

Analog to ``pipeline_scanner.py`` (which targeted Dream3D) but for the
``.tscn`` / ``.gd`` / ``project.godot`` file format.

The .tscn parser here is deliberately tolerant: Godot's text scene
format has plenty of edge cases (sub-resources, NodePath shorthand,
SubResource references) and we're not trying to be a faithful
deserialiser. We want enough structure to:

  • Render a scene tree in the Workspace tab
  • Report broken ``ext_resource`` paths
  • Feed the Game Designer a structured node list

The script parser is a regex pass — fast enough to run on every save
without launching Godot. The authoritative parser (Godot itself, via
``godot --headless --check-only``) is the validate fallback.
"""

from __future__ import annotations

import re
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
    parent_path: Optional[str] = None       # NodePath relative to root, ``.`` for root
    properties:  Dict[str, str] = field(default_factory=dict)
    script_id:   str = ""                   # ExtResource id of attached script
    children:    List["SceneNode"] = field(default_factory=list)

    def walk(self):
        """Depth-first iterator over self + descendants."""
        yield self
        for c in self.children:
            yield from c.walk()


@dataclass
class ParsedScene:
    """Result of ``parse_scene(path)``."""
    path:          Path
    root:          Optional[SceneNode] = None
    ext_resources: List[Tuple[str, str, str]] = field(default_factory=list)   # (id, type, path)
    issues:        List[str] = field(default_factory=list)


# ============================================================
# .tscn parser
# ============================================================

# [ext_resource type="Script" path="res://main.gd" id="1_abc"]
_EXT_RESOURCE_RE = re.compile(
    r'\[ext_resource\s+'
    r'(?:type\s*=\s*"([^"]+)"\s+)?'
    r'(?:path\s*=\s*"([^"]+)"\s+)?'
    r'(?:[^\]]*\bid\s*=\s*"([^"]+)")?'
    r'[^\]]*\]'
)

# [node name="Player" type="CharacterBody2D" parent="." groups=[...]]
_NODE_HEADER_RE = re.compile(
    r'\[node\s+'
    r'(?:name\s*=\s*"([^"]+)")?'
    r'(?:[^\]]*\btype\s*=\s*"([^"]+)")?'
    r'(?:[^\]]*\bparent\s*=\s*"([^"]*)")?'
    r'[^\]]*\]'
)

# inside a [node] block, ``script = ExtResource("1_abc")``
_NODE_SCRIPT_RE = re.compile(r'^\s*script\s*=\s*ExtResource\(\s*"([^"]+)"\s*\)\s*$')

# generic property line ``key = value`` (value kept raw — we just want presence)
_NODE_PROP_RE = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_/]*)\s*=\s*(.+?)\s*$')

# the [node parent="."] is the root; subsequent parents are like "Player"
# or "Player/Sprite2D" — a path relative to the root.


def parse_scene(path: Any) -> ParsedScene:
    """Parse a .tscn file into a SceneNode tree + ext_resource list.

    Tolerant parser — unknown sections are skipped, malformed lines
    add an entry to ``parsed.issues`` and continue.
    """
    p = Path(path)
    parsed = ParsedScene(path=p)

    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        parsed.issues.append(f"could not read file: {exc!r}")
        return parsed

    # Pass 1: collect ext_resources
    for m in _EXT_RESOURCE_RE.finditer(text):
        rtype = m.group(1) or ""
        rpath = m.group(2) or ""
        rid   = m.group(3) or ""
        if rid or rpath:
            parsed.ext_resources.append((rid, rtype, rpath))

    # Pass 2: walk lines, building a flat list of (parent_path, SceneNode)
    flat: List[Tuple[str, SceneNode]] = []
    current: Optional[SceneNode] = None
    in_node_section = False

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        # Section header — end of any in-progress node
        if line.startswith("["):
            current = None
            in_node_section = False
            hdr = _NODE_HEADER_RE.match(line)
            if hdr:
                name = hdr.group(1) or "?"
                ntype = hdr.group(2) or ""
                parent = hdr.group(3)   # may be None (root) or "" or "."
                node = SceneNode(name=name, type=ntype, parent_path=parent)
                flat.append((parent if parent is not None else ".", node))
                current = node
                in_node_section = True
            continue
        # Property lines inside a node
        if in_node_section and current is not None:
            m_script = _NODE_SCRIPT_RE.match(line)
            if m_script:
                current.script_id = m_script.group(1)
                continue
            m_prop = _NODE_PROP_RE.match(line)
            if m_prop and not m_prop.group(1).startswith("__"):
                current.properties[m_prop.group(1)] = m_prop.group(2)

    if not flat:
        parsed.issues.append("no [node] sections found")
        return parsed

    # Build the tree. The first [node] is the root (parent=None or ".").
    # Subsequent nodes have parent="X" or "X/Y" — paths relative to root.
    # We index by the path-from-root so children can find their parent.
    by_path: Dict[str, SceneNode] = {}
    root_parent, root_node = flat[0]
    parsed.root = root_node
    by_path["."] = root_node

    for parent_path, node in flat[1:]:
        # The node's own path is parent_path + "/" + node.name. When
        # parent_path is "." it just becomes node.name.
        own_path = node.name if parent_path == "." else f"{parent_path}/{node.name}"
        by_path[own_path] = node
        parent = by_path.get(parent_path)
        if parent is None:
            parsed.issues.append(
                f"node {node.name!r} references unknown parent "
                f"{parent_path!r} — flattening to root"
            )
            parent = root_node
        parent.children.append(node)

    return parsed


# ============================================================
# .gd script parser
# ============================================================

_SCRIPT_CLASS_NAME_RE = re.compile(r'^\s*class_name\s+([A-Za-z_][A-Za-z0-9_]*)')
_SCRIPT_EXTENDS_RE   = re.compile(r'^\s*extends\s+([A-Za-z_][A-Za-z0-9_.]*)')
_SCRIPT_SIGNAL_RE    = re.compile(r'^\s*signal\s+([A-Za-z_][A-Za-z0-9_]*)\s*(\(.*\))?')
_SCRIPT_FUNC_RE      = re.compile(r'^\s*func\s+([A-Za-z_][A-Za-z0-9_]*)\s*\((.*?)\)')
_SCRIPT_EXPORT_RE    = re.compile(r'^\s*@export\s+var\s+([A-Za-z_][A-Za-z0-9_]*)')


@dataclass
class ParsedScript:
    """Structural summary of a .gd file."""
    path:       Path
    class_name: str = ""
    extends:    str = ""
    signals:    List[str] = field(default_factory=list)
    funcs:      List[Dict[str, str]] = field(default_factory=list)   # [{"name", "args"}]
    exports:    List[str] = field(default_factory=list)
    issues:     List[str] = field(default_factory=list)


def parse_script(path: Any) -> ParsedScript:
    """Pull a structural summary out of a .gd file.

    Regex pass — not a real parser. Good enough for an outline panel
    and for feeding the Game Designer a description like "this script
    extends Node2D, defines signals (died, scored), exports speed,
    and implements _ready, _process, take_damage."
    """
    p = Path(path)
    parsed = ParsedScript(path=p)
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        parsed.issues.append(f"could not read file: {exc!r}")
        return parsed

    for line in text.splitlines():
        if m := _SCRIPT_CLASS_NAME_RE.match(line):
            parsed.class_name = m.group(1)
        elif m := _SCRIPT_EXTENDS_RE.match(line):
            parsed.extends = m.group(1)
        elif m := _SCRIPT_SIGNAL_RE.match(line):
            parsed.signals.append(m.group(1))
        elif m := _SCRIPT_FUNC_RE.match(line):
            parsed.funcs.append({"name": m.group(1), "args": m.group(2).strip()})
        elif m := _SCRIPT_EXPORT_RE.match(line):
            parsed.exports.append(m.group(1))

    return parsed


# ============================================================
# Validation
# ============================================================

@dataclass
class ValidationIssue:
    """One problem found by the validator."""
    severity:  str          # "error" | "warning" | "info"
    file:      str
    line:      int = 0
    message:   str = ""


def static_validate_project(project_root: Any) -> List[ValidationIssue]:
    """In-process static validation — no Godot subprocess.

    Walks every .tscn / .gd, runs the regex parsers, and surfaces:
      • ext_resource paths that don't exist on disk
      • script ids referenced by nodes but missing from ext_resources
      • scripts that don't have an extends clause
      • parse_scene / parse_script issues bubbled up

    Returns a list of ValidationIssue. The full Godot --check-only
    fallback (catches GDScript syntax errors) lives in
    ``godot_workspace.GodotRunner.validate`` and runs as a subprocess
    when the user clicks Validate.
    """
    root = Path(project_root).expanduser().resolve()
    out: List[ValidationIssue] = []

    if not (root / "project.godot").exists():
        out.append(ValidationIssue(
            severity="error", file="project.godot", line=0,
            message="No project.godot found at this path.",
        ))
        return out

    # Walk scenes
    for scene_path in root.rglob("*.tscn"):
        # skip Godot internal scenes
        if ".godot" in scene_path.parts or ".import" in scene_path.parts:
            continue
        scene = parse_scene(scene_path)
        rel = str(scene_path.relative_to(root))

        for issue in scene.issues:
            out.append(ValidationIssue(
                severity="warning", file=rel, message=issue,
            ))

        # ext_resource path existence
        for (_id, _type, ext_path) in scene.ext_resources:
            if not ext_path or not ext_path.startswith("res://"):
                continue
            disk_path = root / ext_path.replace("res://", "")
            if not disk_path.exists():
                out.append(ValidationIssue(
                    severity="error", file=rel,
                    message=f"ext_resource points to missing file: {ext_path}",
                ))

        # node script_id must appear in ext_resources
        if scene.root is not None:
            known_ids = {rid for (rid, _t, _p) in scene.ext_resources}
            for node in scene.root.walk():
                if node.script_id and node.script_id not in known_ids:
                    out.append(ValidationIssue(
                        severity="error", file=rel,
                        message=f"node {node.name!r} references unknown "
                                f"script id {node.script_id!r}",
                    ))

    # Walk scripts
    for script_path in root.rglob("*.gd"):
        if ".godot" in script_path.parts or ".import" in script_path.parts:
            continue
        ps = parse_script(script_path)
        rel = str(script_path.relative_to(root))
        for issue in ps.issues:
            out.append(ValidationIssue(
                severity="warning", file=rel, message=issue,
            ))
        if not ps.extends and not ps.class_name:
            out.append(ValidationIssue(
                severity="info", file=rel,
                message="no extends or class_name — script will inherit RefCounted",
            ))

    return out
