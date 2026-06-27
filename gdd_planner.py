"""
gdd_planner.py — turn a parsed ``GDD`` into a typed ``Plan``.

The orchestrator (``gdd_builder``, commit 3) consumes the Plan and
executes it: each ``ScenePlan`` becomes a ``.tscn`` file, each
``ScriptPlan`` becomes a ``.gd`` written by ``GodotCoder`` against
the shared entity registry + signal contracts, and the
``AutoloadPlan`` entries become res:// singletons.

The planner is genre-aware: a top-down shooter Plan is structured
differently from a platformer Plan, which is different from a puzzle
Plan, which is different from a visual novel Plan. The mapping
lives in ``_GENRE_TEMPLATES`` — same five buckets demo_builder
already uses (platformer / topdown / puzzle / vn / generic), so the
demo skeleton lines up with what the planner expects.

Why the planner is opinionated about file layout
------------------------------------------------
The orchestrator calls GodotCoder with a goal-anchor + a structured
context (entity names, signal contracts the script must emit/handle,
parent scene). Coder calls are far more reliable when the plan is
small and concrete than when it's loose ("write the player script").
So we accept the loss of flexibility — Anvil ships ONE house style
per genre — in exchange for AI orchestration that actually converges.

Asset placeholder strategy
--------------------------
Per the user's preference: **ColorRect + Label**, never AI art.
Each entity in a scene becomes a ColorRect of an arbitrary
genre-appropriate colour with a Label showing the entity name,
plus a CollisionShape2D when the entity is a player / enemy /
item. The user replaces the ColorRect with a hand-painted
sprite later via the 🎨 Pixel Art tab.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from gdd_parser import GDD, Entity, Scene


# ============================================================
# Plan data model
# ============================================================

@dataclass
class NodeSpec:
    """One node inside a scene tree. Recursive — children are
    rendered as nested [node] sections in the .tscn file."""
    name:     str
    type:     str
    parent:   Optional[str] = None       # NodePath relative to root
    props:    Dict[str, str] = field(default_factory=dict)
    script:   Optional[str] = None        # res:// path to the script
    children: List["NodeSpec"] = field(default_factory=list)


@dataclass
class ScenePlan:
    """One .tscn file the orchestrator will write."""
    name:        str                  # "Main", "Player", "Enemy"
    file:        str                  # "scenes/main.tscn"
    root:        NodeSpec
    purpose:     str = ""             # human-readable description
    notes:       List[str] = field(default_factory=list)


@dataclass
class SignalContract:
    """A signal someone emits + someone handles. Used by the
    orchestrator to pass cross-file context to GodotCoder so the
    generated scripts agree on names + signatures."""
    name:        str
    emitter:     str                  # entity / autoload that emits
    args:        List[str] = field(default_factory=list)
    handlers:    List[str] = field(default_factory=list)


@dataclass
class ScriptPlan:
    """One .gd file the orchestrator will hand to GodotCoder.

    The Coder gets ``purpose`` as the high-level task, ``extends``
    as a hard constraint, and ``signals_emit`` / ``signals_handle``
    as exact name + signature contracts.
    """
    file:           str               # "scripts/player.gd"
    extends:        str               # "CharacterBody2D"
    class_name:     str = ""
    purpose:        str = ""
    entity:         Optional[str] = None  # entity slug this script controls
    signals_emit:   List[SignalContract] = field(default_factory=list)
    signals_handle: List[SignalContract] = field(default_factory=list)
    exported_vars:  List[Tuple[str, str, str]] = field(default_factory=list)
                                      # (name, type, default_value)


@dataclass
class AutoloadPlan:
    """Game-wide manager singleton."""
    name:           str               # "GameManager"
    file:           str               # "autoloads/game_manager.gd"
    purpose:        str = ""
    signals_emit:   List[SignalContract] = field(default_factory=list)


@dataclass
class LedgerEntry:
    """One row of the dev ledger — a per-artifact 'what still needs
    doing' record. Lets the user (and Anvil) see which files still
    need a tool applied and which assets need creating, with their
    temporary placeholder standing in until real work lands.
    """
    path:            str                        # file path or scene node path
    kind:            str                        # script|scene|autoload|asset|sim
    purpose:         str = ""
    status:          str = "planned"            # planned|placeholder|real|failed
    tools_suggested: List[str] = field(default_factory=list)
    placeholder:     Optional[str] = None       # what temporarily stands in
    # For assets only: where the real (hand-painted) asset will go. Stays
    # None until the user paints it in the Pixel Art tab — Anvil NEVER
    # auto-generates art.
    replace_with:    Optional[str] = None


@dataclass
class Plan:
    """Output of ``plan_from_gdd``. Input to ``gdd_builder``."""
    title:          str = ""
    genre:          str = ""
    template:       str = "generic"   # which demo_builder template fits
    scenes:         List[ScenePlan] = field(default_factory=list)
    scripts:        List[ScriptPlan] = field(default_factory=list)
    autoloads:      List[AutoloadPlan] = field(default_factory=list)
    entity_registry: Dict[str, Entity] = field(default_factory=dict)
    signal_contracts: List[SignalContract] = field(default_factory=list)
    ledger:         List[LedgerEntry] = field(default_factory=list)
    notes:          List[str] = field(default_factory=list)

    def file_summary(self) -> List[Tuple[str, str]]:
        """``[(file_path, one_line_purpose)]`` for the plan viewer."""
        out: List[Tuple[str, str]] = []
        for s in self.scenes:
            out.append((s.file, s.purpose or "scene"))
        for s in self.scripts:
            out.append((s.file, s.purpose or "script"))
        for a in self.autoloads:
            out.append((a.file, a.purpose or "autoload"))
        return out


# ============================================================
# Genre routing
# ============================================================

_GENRE_TEMPLATES = (
    ("platformer", ("platformer", "metroidvania", "puzzle-platformer",
                     "side-scroll", "2d platformer", "jump")),
    ("topdown",    ("topdown", "top-down", "top down", "twin-stick",
                     "shooter", "roguelike", "action rpg")),
    ("puzzle",     ("puzzle", "match", "deck", "tower defense",
                     "tower defence", "puzzle-platformer", "logic")),
    ("vn",         ("visual novel", "narrative", "walking sim",
                     "interactive fiction", "story")),
)


def _detect_template(genre: str) -> str:
    g = (genre or "").lower()
    for tpl, hints in _GENRE_TEMPLATES:
        for h in hints:
            if h in g:
                return tpl
    return "generic"


# ============================================================
# Helpers — slugs, colours, node-tree builders
# ============================================================

_ENEMY_COLORS = ["#d32f2f", "#e0884a", "#b71c1c", "#7a1818"]
_NPC_COLORS   = ["#7ea16d", "#5a7d4a", "#a98a8a"]
_ITEM_COLORS  = ["#f6c14a", "#e0884a", "#9bbc0f"]
_OBST_COLORS  = ["#3a2828", "#231a1a", "#7a7575"]
_PLAYER_COLOR = "#d4d4d4"


def _color_for(entity: Entity, idx: int = 0) -> str:
    if entity.role == "player":
        return _PLAYER_COLOR
    pool = {
        "enemy":    _ENEMY_COLORS,
        "npc":      _NPC_COLORS,
        "item":     _ITEM_COLORS,
        "obstacle": _OBST_COLORS,
    }.get(entity.role, ["#a98a8a"])
    return pool[idx % len(pool)]


def _placeholder_visual(entity: Entity, color: str) -> NodeSpec:
    """ColorRect + centred Label so each entity reads in the editor
    AND the running game without art. Sized 32×32 — typical sprite
    cell. The Coder script + the user later replace the ColorRect
    with a Sprite2D pointing at a real PNG."""
    return NodeSpec(
        name="Visual", type="Control", parent=".",
        props={
            "offset_left":   "-16.0",
            "offset_top":    "-16.0",
            "offset_right":  "16.0",
            "offset_bottom": "16.0",
        },
        children=[
            NodeSpec(
                name="ColorRect", type="ColorRect", parent="Visual",
                props={
                    "anchor_right":  "1.0",
                    "anchor_bottom": "1.0",
                    "color":         f"Color({_rgb_decimal(color)}, 1)",
                },
            ),
            NodeSpec(
                name="Label", type="Label", parent="Visual",
                props={
                    "anchor_right":  "1.0",
                    "anchor_bottom": "1.0",
                    "text":           f'"{entity.name[:8]}"',
                    "horizontal_alignment": "1",
                    "vertical_alignment":   "1",
                },
            ),
        ],
    )


def _rgb_decimal(hex_color: str) -> str:
    """``#rrggbb`` → ``r/255.0, g/255.0, b/255.0`` for the
    Godot ``Color(r, g, b, a)`` literal."""
    s = hex_color.lstrip("#")
    r = int(s[0:2], 16) / 255.0
    g = int(s[2:4], 16) / 255.0
    b = int(s[4:6], 16) / 255.0
    return f"{r:.3f}, {g:.3f}, {b:.3f}"


# ============================================================
# Per-genre node-tree builders for the main scene
# ============================================================

def _root_type_for(template: str) -> str:
    return {
        "platformer": "Node2D",
        "topdown":    "Node2D",
        "puzzle":     "Control",
        "vn":         "Control",
    }.get(template, "Node2D")


def _build_main_scene(plan: Plan, gdd: GDD) -> ScenePlan:
    """Top-level ``Main.tscn``. Instances the player, each enemy
    type, and stamps a HUD label. Layout differs by template but
    the structure is consistent: a Main root, a Player child, an
    Entities folder, and a HUD."""
    root_type = _root_type_for(plan.template)
    main_root = NodeSpec(
        name="Main", type=root_type, parent=None,
        script="res://scripts/main.gd",
    )
    # HUD
    hud = NodeSpec(
        name="HUD", type="Control", parent=".",
        props={
            "anchor_right":  "1.0",
            "anchor_bottom": "1.0",
        },
        children=[
            NodeSpec(
                name="Title", type="Label", parent="HUD",
                props={
                    "offset_left":  "16.0",
                    "offset_top":   "16.0",
                    "offset_right": "600.0",
                    "offset_bottom":"40.0",
                    "text": f'"{gdd.title or plan.title}"',
                },
            ),
            NodeSpec(
                name="Status", type="Label", parent="HUD",
                props={
                    "offset_left":  "16.0",
                    "offset_top":   "40.0",
                    "offset_right": "600.0",
                    "offset_bottom":"64.0",
                    "text": '"[score]  [hp]"',
                },
            ),
        ],
    )
    main_root.children.append(hud)
    # Player instance
    player = next((e for e in gdd.entities if e.role == "player"), None)
    if player is not None:
        # We could load the Player.tscn via [ext_resource] but the
        # simplest reliable shape for a starter is to in-line a
        # CharacterBody2D + visual + collision in the same scene.
        # The orchestrator can refactor later when the user asks
        # the Coder to "split player into its own scene".
        main_root.children.append(_inline_entity_node(player))
    # Enemies — group them under Entities/Enemies for clarity
    enemies = [e for e in gdd.entities if e.role == "enemy"]
    if enemies:
        enemies_root = NodeSpec(
            name="Enemies", type="Node", parent=".",
        )
        for i, enemy in enumerate(enemies):
            enemies_root.children.append(_inline_entity_node(enemy, idx=i))
        main_root.children.append(enemies_root)
    return ScenePlan(
        name="Main",
        file="main.tscn",
        root=main_root,
        purpose=f"Top-level scene for {gdd.title or plan.title}.",
    )


def _inline_entity_node(entity: Entity, idx: int = 0) -> NodeSpec:
    """A body + Visual + (optional) CollisionShape2D for an entity
    inlined into a parent scene."""
    body_type = {
        "player": "CharacterBody2D",
        "enemy":  "CharacterBody2D",
        "npc":    "StaticBody2D",
        "item":   "Area2D",
        "obstacle": "StaticBody2D",
    }.get(entity.role, "Node2D")
    color = _color_for(entity, idx)
    node = NodeSpec(
        name=entity.name.split()[0],  # first word so " " doesn't break .tscn
        type=body_type, parent=".",
        props={"position": f"Vector2({100 + idx * 80}, {200})"},
        children=[_placeholder_visual(entity, color)],
    )
    if body_type in ("CharacterBody2D", "StaticBody2D", "Area2D"):
        node.children.append(NodeSpec(
            name="Collision", type="CollisionShape2D", parent=node.name,
        ))
    return node


# ============================================================
# Script planning
# ============================================================

def _main_extends_for(template: str) -> str:
    return {
        "platformer": "Node2D",
        "topdown":    "Node2D",
        "puzzle":     "Control",
        "vn":         "Control",
    }.get(template, "Node2D")


def _entity_extends_for(entity: Entity) -> str:
    return {
        "player":   "CharacterBody2D",
        "enemy":    "CharacterBody2D",
        "npc":      "StaticBody2D",
        "item":     "Area2D",
        "obstacle": "StaticBody2D",
    }.get(entity.role, "Node2D")


def _build_scripts(plan: Plan, gdd: GDD) -> None:
    """Populate ``plan.scripts`` and the signal_contracts list."""
    # ── Main script ──
    main_signals_handle: List[SignalContract] = []
    for ent in gdd.entities:
        if ent.role == "enemy":
            sig = SignalContract(
                name="died",
                emitter=ent.slug,
                args=["pos: Vector2"],
                handlers=["main"],
            )
            plan.signal_contracts.append(sig)
            main_signals_handle.append(sig)
        if ent.role == "item":
            sig = SignalContract(
                name="collected",
                emitter=ent.slug,
                args=["value: int"],
                handlers=["main"],
            )
            plan.signal_contracts.append(sig)
            main_signals_handle.append(sig)
    plan.scripts.append(ScriptPlan(
        file="scripts/main.gd",
        extends=_main_extends_for(plan.template),
        purpose=(
            f"Main controller for {gdd.title or plan.title}. "
            f"Wires the HUD, listens to enemy 'died' + item "
            f"'collected' signals from child entities, updates score "
            f"and HP labels. Game flow: "
            f"{(gdd.win_condition or 'reach the end').strip()} on win, "
            f"{(gdd.lose_condition or 'player HP zero').strip()} on "
            f"lose."
        ),
        signals_handle=main_signals_handle,
        exported_vars=[
            ("starting_hp", "int", "100"),
        ],
    ))
    # ── Entity scripts ──
    for ent in gdd.entities:
        purpose = _purpose_for_entity(ent, plan.template, gdd)
        emits: List[SignalContract] = []
        if ent.role == "enemy":
            sig = next((s for s in plan.signal_contracts
                        if s.name == "died" and s.emitter == ent.slug), None)
            if sig:
                emits.append(sig)
        if ent.role == "item":
            sig = next((s for s in plan.signal_contracts
                        if s.name == "collected" and s.emitter == ent.slug), None)
            if sig:
                emits.append(sig)
        plan.scripts.append(ScriptPlan(
            file=f"scripts/{ent.slug}.gd",
            extends=_entity_extends_for(ent),
            class_name=re.sub(r"[^A-Za-z0-9]+", "", ent.name) or "Entity",
            purpose=purpose,
            entity=ent.slug,
            signals_emit=emits,
            exported_vars=_exports_for_entity(ent, plan.template),
        ))


def _purpose_for_entity(ent: Entity, template: str, gdd: GDD) -> str:
    """Build a sentence the GodotCoder will read as its task brief."""
    base = (
        f"Script for the {ent.role} entity {ent.name!r}. "
        f"Description from GDD: {ent.description or '(no description)'}"
    )
    if ent.role == "player":
        if template == "platformer":
            base += (
                ". Implement horizontal movement via ui_left/ui_right, "
                "jump via ui_up when on floor, and gravity. Use @export "
                "for tunables (move_speed, jump_velocity, gravity)."
            )
        elif template == "topdown":
            base += (
                ". Implement 8-way movement via Input.get_axis pairs. "
                "Use @export for move_speed. Read aim direction from "
                "the mouse if a relevant control is mapped."
            )
        elif template == "puzzle":
            base += (
                ". Implement click-driven interaction with the puzzle "
                "grid. No physics."
            )
        elif template == "vn":
            base += ". No movement; respond to dialogue advance input."
    elif ent.role == "enemy":
        base += (
            f". Behavior: {', '.join(ent.behaviors[:3]) or 'patrol + chase'}"
            f". Emit a 'died' signal with the position when HP reaches "
            f"zero."
        )
    elif ent.role == "item":
        base += (
            ". On overlap with the player, emit a 'collected' signal "
            "with the value and free self via queue_free()."
        )
    elif ent.role == "npc":
        base += ". On player overlap, surface a dialogue label."
    else:
        base += ". Implement the minimal interaction described above."
    return base


def _exports_for_entity(ent: Entity, template: str
                          ) -> List[Tuple[str, str, str]]:
    if ent.role == "player":
        if template == "platformer":
            return [
                ("move_speed",    "float", "200.0"),
                ("jump_velocity", "float", "-400.0"),
                ("gravity",       "float", "980.0"),
            ]
        if template == "topdown":
            return [("move_speed", "float", "220.0")]
        return [("move_speed", "float", "100.0")]
    if ent.role == "enemy":
        return [
            ("hp",      "int",   "30"),
            ("speed",   "float", "60.0"),
            ("damage",  "int",   "8"),
        ]
    if ent.role == "item":
        return [("value", "int", "1")]
    return []


# ============================================================
# Autoload planning
# ============================================================

def _build_autoloads(plan: Plan, gdd: GDD) -> None:
    """Add a tiny GameManager autoload for cross-scene state.

    Score + HP live here so the main script can read them in
    multiple scenes without re-instantiating. Kept minimal — the
    user can split it later if it grows.
    """
    plan.autoloads.append(AutoloadPlan(
        name="GameManager",
        file="autoloads/game_manager.gd",
        purpose=(
            "Global state singleton. Holds the running score, "
            "current HP, and game-flow flags (paused / game_over). "
            "Emits 'score_changed(value: int)' when score updates "
            "and 'game_over(reason: String)' on win or loss."
        ),
        signals_emit=[
            SignalContract(name="score_changed",
                            emitter="GameManager", args=["value: int"],
                            handlers=["main"]),
            SignalContract(name="game_over",
                            emitter="GameManager",
                            args=["reason: String"],
                            handlers=["main"]),
        ],
    ))


# ============================================================
# Public entry point
# ============================================================

def plan_from_gdd(gdd: GDD) -> Plan:
    """Build a typed Plan from a parsed GDD.

    Falls through to a single-scene "generic" Plan when the GDD is
    too thin for richer planning. The orchestrator always gets
    SOMETHING runnable, even if minimal.
    """
    plan = Plan(
        title=gdd.title or "Untitled",
        genre=gdd.genre or "(unspecified)",
        template=_detect_template(gdd.genre),
    )
    plan.entity_registry = {e.slug: e for e in gdd.entities}
    if not gdd.entities:
        # Inject a default player so the planner can produce a
        # runnable scene. Note this in plan.notes for the viewer.
        default_player = Entity(
            name="Player", role="player",
            description="default player inserted by planner — "
                        "GDD had no entities section",
        )
        gdd.entities.append(default_player)
        plan.entity_registry[default_player.slug] = default_player
        plan.notes.append(
            "GDD had no entities — injected a default Player."
        )
    # Scenes
    plan.scenes.append(_build_main_scene(plan, gdd))
    # Scripts + signal contracts
    _build_scripts(plan, gdd)
    # Autoloads
    _build_autoloads(plan, gdd)
    if not gdd.win_condition:
        plan.notes.append(
            "No win condition in GDD — main.gd will default to "
            "'all enemies dead'."
        )
    if not gdd.lose_condition:
        plan.notes.append(
            "No lose condition in GDD — main.gd will default to "
            "'HP zero'."
        )
    _build_ledger(plan)
    return plan


def _build_ledger(plan: Plan) -> None:
    """Populate ``plan.ledger`` from the rest of the plan — a per-file
    worklist of what still needs a tool applied + which assets need
    creating (with their ColorRect/Label placeholder).
    """
    ledger: List[LedgerEntry] = []
    # Scripts: placeholders until GodotCoder fleshes them out.
    for sp in plan.scripts:
        ledger.append(LedgerEntry(
            path=sp.file, kind="script",
            purpose=(sp.purpose or "")[:120],
            status="placeholder",
            tools_suggested=["GodotCoder"],
            placeholder="parseable stub (_ready only)",
        ))
    # Autoloads: minimal stubs.
    for al in plan.autoloads:
        ledger.append(LedgerEntry(
            path=al.file, kind="autoload",
            purpose=(al.purpose or "")[:120],
            status="placeholder",
            tools_suggested=["GodotCoder"],
            placeholder="state vars + signal decls",
        ))
    # Scenes: fully rendered by the builder, no further tool needed.
    for scn in plan.scenes:
        ledger.append(LedgerEntry(
            path=scn.file, kind="scene",
            purpose=(scn.purpose or "scene tree")[:120],
            status="real",
        ))
    # Assets: every entity's visual is a ColorRect+Label placeholder the
    # user later hand-paints in the Pixel Art tab. replace_with stays
    # None — Anvil never generates art.
    for slug, ent in plan.entity_registry.items():
        name = (getattr(ent, "name", "") or slug).split()[0] if getattr(ent, "name", "") else slug
        ledger.append(LedgerEntry(
            path=f"Main/{name}/Visual", kind="asset",
            purpose=f"{getattr(ent, 'role', 'entity')} sprite for {name}",
            status="placeholder",
            tools_suggested=["Pixel Art"],
            placeholder="ColorRect + Label",
            replace_with=None,
        ))
    # The generated sim harness.
    ledger.append(LedgerEntry(
        path="scripts/sim/Sim.gd", kind="sim",
        purpose="headless balance-sim harness",
        status="planned",
        tools_suggested=["SimHarness"],
    ))
    plan.ledger = ledger
