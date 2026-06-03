# Anvil Demo — Combat

A complete tiny Godot 4 project that demonstrates Anvil's self-play
pattern: an `AutoPlayer` autoload makes per-tick decisions, reads
weights from whichever persona Anvil is running, and the game
records the outcome via `Anvil.metric()`.

## What it does

Auto-resolves a turn-based fight between the player and one enemy.
Each "tick" of the AutoPlayer is one player turn. The AutoPlayer
asks `main.gd` for a state snapshot (HP, enemy visible, can heal),
runs `AutoPlayer.default_policy(state)`, and picks one of:

- `attack` — damage the enemy
- `defend` — halve the enemy's next hit
- `heal`   — restore some HP
- `wait`   — no action (the enemy still attacks)

Then `main.gd` resolves the enemy's turn and checks for win / loss
/ timeout. When the fight ends it emits:

```
ANVIL_METRIC: outcome = victory|defeat|timeout
ANVIL_METRIC: final_hp = <int>
ANVIL_METRIC: turns = <int>
ANVIL_METRIC: attacks = <int>
ANVIL_METRIC: defends = <int>
ANVIL_METRIC: heals_used = <int>
ANVIL_EVENT:  combat_end ...
```

## Running it through Anvil

1. Copy this folder anywhere on disk (or point Anvil at it in place).
2. Open the 🎲 Simulations tab.
3. Configure:
   - **Sim name:** `combat_demo` (or whatever)
   - **Backend:** `godot`
   - **Project:** path to this folder
   - **Duration:** ~5 seconds is plenty (fights are usually <50 ticks)
4. Edit the sweep JSON to e.g.:

   ```json
   {
     "axes": {
       "playstyle": {"type": "persona", "names": "all"}
     }
   }
   ```

5. Hit ▶ Run sweep. Eight runs (one per built-in persona) record
   their outcome + final HP + turn count.
6. Click any run → see the persona + metrics + the action trace
   in the detail pane.
7. ⚖ Send sweep to Council → ask "which persona had the best
   win rate?" or "how does aggression correlate with turns taken?"

## What you should see

Across the eight built-in personas:

| Persona | What to expect |
|---|---|
| **Aggressive** | Fast results, low final HP, mixed win rate |
| **Cautious** | Long fights, high final HP, strong win rate |
| **Greedy** | Heals only when nearly dead; volatile outcomes |
| **Hardcore** | Pushes through; high win rate even at low HP |
| **Relaxed** | Defends most turns; sometimes hits the turn cap |
| **Speedrunner** | Attacks every turn regardless of damage taken |
| **Completionist** | Similar to Cautious but more healing |
| **Casual** | Frequent waits; loses to chip damage |

That difference IS the balance test — when one persona dominates,
your numbers (`player_atk`, `enemy_atk`, `heal_amount`) are off.

## Parameter passthrough

Combat parameters in `main.gd` use `Anvil.get_param(name, default)`
so a sweep axis can override them:

```json
{
  "axes": {
    "player_atk":  {"type": "range", "start": 8, "stop": 18, "step": 2},
    "enemy_atk":   {"type": "list",  "values": [6, 9, 12]},
    "playstyle":   {"type": "persona", "names": "all"}
  }
}
```

That's 6 × 3 × 8 = 144 runs, each ~5 seconds — about 12 minutes
of compute for a full balance grid. Result: a table of outcomes
the Sim Analyst can rank, group by persona, and correlate against.

## How to adapt this to your game

The pattern is in three pieces:

1. **State snapshot** — `_state_snapshot()` returns the dict the
   AutoPlayer sees each tick. Add whatever fields your decision
   logic cares about.
2. **Action handlers** — one Callable per named action. Register
   with `AutoPlayer.register_action("name", handler)`.
3. **Decision callback** — either reuse `default_policy` (above)
   or write your own. The callback gets the state dict + returns
   either an action name (string) or a `{"action": "name", ...}` dict.

For a richer game, you'd add actions like `cast_spell`,
`use_item`, `swap_weapon`. The decision callback reads
`AutoPlayer.get_weight("aggression")` etc. to bias choices, the
same way `default_policy` does today.

See `autoloads/anvil_auto_player.gd` for the full API.
