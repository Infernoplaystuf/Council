"""Smoke test for gdd_planner — typed Plan from parsed GDD."""
import gdd_parser as gp
import gdd_planner as pl


SAMPLE_TOPDOWN = """\
# Star Forager

**Genre:** Top-down Shooter
**Hook:** Strip-mine a derelict starship.

## Mechanics
- Twin-stick movement and aim
- Drill tool

## Entities
- **Player** -- slow but durable
- **Maintenance Drone** -- patrols, alerts on sight
- **Salvage Pile** -- pickup item

## Scenes
- Main Deck

## Win Condition
Reach the bridge.

## Lose Condition
HP hits zero.
"""

SAMPLE_PLATFORMER = """\
# Cog Climber

**Genre:** 2D Platformer

## Mechanics
- Jump

## Entities
- **Hero** -- the protagonist
- **Goblin** -- patrols ledges
"""


def main():
    # ── Topdown plan ──
    gdd = gp.parse_gdd(SAMPLE_TOPDOWN)
    plan = pl.plan_from_gdd(gdd)

    assert plan.title == "Star Forager"
    assert plan.template == "topdown", plan.template
    print(f"PASS template detection -> {plan.template}")

    # Scenes
    assert len(plan.scenes) == 1
    main = plan.scenes[0]
    assert main.name == "Main"
    assert main.file == "main.tscn"
    assert main.root.type == "Node2D"  # topdown root
    # Top-level Main has HUD + Player + Enemies group
    child_types = [c.type for c in main.root.children]
    assert "Control" in child_types, child_types  # HUD
    assert "CharacterBody2D" in child_types, child_types  # Player
    # Enemies group present
    enemies_grp = next(
        (c for c in main.root.children if c.name == "Enemies"), None,
    )
    assert enemies_grp is not None and len(enemies_grp.children) == 1
    print(f"PASS main scene tree  "
          f"({len(main.root.children)} top-level children)")

    # Scripts — main + one per entity (3 entities → 3 scripts)
    # Plus main.gd = 4 total
    assert len(plan.scripts) == 4, [s.file for s in plan.scripts]
    main_script = plan.scripts[0]
    assert main_script.file == "scripts/main.gd"
    assert main_script.extends == "Node2D"
    print(f"PASS scripts  {[s.file for s in plan.scripts]}")

    # Player script with topdown-genre exports
    player_script = next(s for s in plan.scripts if s.entity == "player")
    assert player_script.extends == "CharacterBody2D"
    export_names = [e[0] for e in player_script.exported_vars]
    assert "move_speed" in export_names
    print(f"PASS player script  extends={player_script.extends!r}, "
          f"exports={export_names}")

    # Enemy script emits 'died' signal — and main script handles it
    drone_script = next(s for s in plan.scripts
                          if s.entity and "drone" in s.entity)
    emit_names = [c.name for c in drone_script.signals_emit]
    assert "died" in emit_names, emit_names
    handle_names = [c.name for c in main_script.signals_handle]
    assert "died" in handle_names, handle_names
    print(f"PASS signal contract (enemy.died → main)")

    # Item script emits 'collected'
    item_script = next(s for s in plan.scripts
                         if s.entity and "pile" in s.entity)
    item_emits = [c.name for c in item_script.signals_emit]
    assert "collected" in item_emits
    assert "collected" in [c.name for c in main_script.signals_handle]
    print(f"PASS signal contract (item.collected → main)")

    # Autoloads
    assert len(plan.autoloads) == 1
    gm = plan.autoloads[0]
    assert gm.name == "GameManager"
    autoload_signals = [s.name for s in gm.signals_emit]
    assert "score_changed" in autoload_signals
    assert "game_over" in autoload_signals
    print(f"PASS autoload  {gm.name}, emits {autoload_signals}")

    # Entity registry
    assert "player" in plan.entity_registry
    assert "maintenance_drone" in plan.entity_registry
    print(f"PASS entity_registry  {sorted(plan.entity_registry.keys())}")

    # file_summary for the viewer
    summary = plan.file_summary()
    assert len(summary) >= 5
    files = [f for f, _ in summary]
    assert "main.tscn" in files
    assert "scripts/main.gd" in files
    assert "autoloads/game_manager.gd" in files
    print(f"PASS file_summary  {len(summary)} files")

    # ── Platformer plan has platformer exports ──
    plan2 = pl.plan_from_gdd(gp.parse_gdd(SAMPLE_PLATFORMER))
    assert plan2.template == "platformer"
    p_player = next(s for s in plan2.scripts if s.entity == "hero")
    p_exports = [e[0] for e in p_player.exported_vars]
    assert "jump_velocity" in p_exports
    assert "gravity" in p_exports
    print(f"PASS platformer exports  {p_exports}")

    # ── Empty GDD falls through to single-scene Plan ──
    plan3 = pl.plan_from_gdd(gp.parse_gdd("# Bare"))
    assert plan3.title == "Bare"
    assert len(plan3.scenes) == 1
    assert any("default Player" in n for n in plan3.notes)
    print(f"PASS empty-GDD fallback  notes={plan3.notes}")

    print("\nAll gdd_planner smoke tests passed.")


if __name__ == "__main__":
    main()
