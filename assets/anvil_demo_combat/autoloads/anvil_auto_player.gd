# ============================================================
# anvil_auto_player.gd  —  drop-in self-play actor for Anvil sims
# ============================================================
#
# Why this exists
# ---------------
# Anvil's simulator runs your project with ``godot --headless``,
# but the game still has to play itself for useful telemetry to
# come out. Anything that requires human input (jumps, clicks,
# attacks) won't produce data unless something INSIDE the game
# is making those decisions.
#
# This autoload is that "something". It gives you a uniform place
# to wire a decision callback per persona, register the action
# verbs your game supports, and a tick clock that calls the
# decision callback at a steady rate. Your game code does NOT
# need to know what persona is active — the AutoPlayer reads
# weights from Anvil and biases choices accordingly.
#
# Install
# -------
# 1. Add anvil_telemetry.gd as the autoload "Anvil" (the AutoPlayer
#    depends on it for persona lookup).
# 2. Add THIS file as the autoload "AutoPlayer".
# 3. In your main scene's _ready(), register your game's actions
#    and (optionally) a custom decision callback. See the worked
#    example at the bottom of this file.
#
# Two ways to use it
# ------------------
# A) Use the built-in ``default_policy(state)`` — a persona-driven
#    choice between attack / defend / heal / wait / explore. Good
#    for prototyping; works for most combat-shaped games.
#
# B) Register your own ``decision_callback`` — takes a state dict
#    you supply per tick, returns either an action name string or
#    a {"action": "name", ...} dict. Total control; per-game logic.
# ============================================================

extends Node

## Per-tick decision callback. Signature: ``func cb(state: Dictionary)``
## returning either a String (action name) or a Dictionary
## ({"action": "...", ...}). Null/empty return means "skip this tick".
var decision_callback: Callable

## Map of action_name → Callable(args: Dictionary). The handler runs
## whenever the decision callback returns that name.
var actions: Dictionary = {}

## Optional state-builder callback. Signature: ``func cb()`` returning
## a Dictionary the decision callback receives. If unset, the
## decision callback gets an empty dict and has to read game state
## itself (via singletons / get_tree() / etc.).
var state_callback: Callable

## Tick interval in seconds. 10 Hz default is enough for turn-based
## or slower games; bump this down for reflex-heavy sims.
@export var tick_seconds: float = 0.1

## When false, _process is a no-op even if a callback is registered.
## Auto-activated in _ready() when Anvil reports a persona is set,
## so a normal player run (no Anvil persona) doesn't get hijacked
## by the AutoPlayer.
var active: bool = false

## Hard cap on total ticks per session — defensive against an
## infinite loop where the decision callback never reaches a
## win/loss state. Default 10k ticks ≈ 17 minutes at 10 Hz.
@export var max_ticks: int = 10000

## When ``true`` and Anvil reports a persona, each picked action is
## emitted as an ANVIL_EVENT so the sim record carries the action
## trace. Off by default — chatty for long sessions.
@export var log_actions: bool = false

var _accum: float = 0.0
var _tick_count: int = 0


func _ready() -> void:
	# Auto-activate when an Anvil persona is set. Without a persona,
	# the game runs normally — useful for both manual playtesting
	# and headless sims where the user didn't pick a persona axis.
	if has_node("/root/Anvil") and Anvil.has_persona():
		active = true
		Anvil.event("autoplayer_active", {
			"persona": Anvil.persona_name("balanced"),
			"tick_seconds": tick_seconds,
		})
	set_process(true)


# ----------------------------------------------------------------
# Public API
# ----------------------------------------------------------------

## Plug your per-tick decision logic in here.
func register_decision(cb: Callable) -> void:
	decision_callback = cb


## Map an action name to a handler. The handler runs whenever the
## decision callback returns that name (as a String or in a
## ``{"action": "name", ...}`` dict).
func register_action(name: String, handler: Callable) -> void:
	actions[name] = handler


## Optional: register a state-builder that snapshots your game's
## state into the dict the decision callback receives each tick.
func register_state_builder(cb: Callable) -> void:
	state_callback = cb


## Convenience pass-through to Anvil.get_persona_weight — saves
## importing Anvil in scripts that already have AutoPlayer.
func get_weight(axis: String, default: float = 0.5) -> float:
	if not has_node("/root/Anvil"):
		return default
	return Anvil.get_persona_weight(axis, default)


## Stop the AutoPlayer. Use this when the game reaches a terminal
## state (game over, mission complete) so it doesn't keep ticking.
## Anvil's runner will see the game quit naturally; you can call
## ``get_tree().quit()`` right after.
func stop(reason: String = "") -> void:
	active = false
	set_process(false)
	if has_node("/root/Anvil"):
		Anvil.event("autoplayer_stopped", {"reason": reason,
			"ticks": _tick_count})


# ----------------------------------------------------------------
# Built-in default policy
# ----------------------------------------------------------------

## A baseline persona-driven decision policy. Returns one of:
##   "attack", "defend", "heal", "wait", "explore"
##
## Reads these fields from ``state`` (all optional, sensible
## defaults supplied):
##
##   hp           current HP (default 100)
##   max_hp       max HP (default 100)
##   enemy_visible whether something to attack is in range
##   can_heal     whether a heal action is available
##   resources    how much of the limiting resource is on hand
##
## Most prototypes can adopt this without writing their own logic:
##
##     AutoPlayer.register_decision(AutoPlayer.default_policy)
##
## The policy is intentionally simple — it's a starting point, not
## a tournament agent. For competitive sweeps you'll want to
## register your own callback that exploits your game's mechanics.
func default_policy(state: Dictionary) -> String:
	var hp           : float = float(state.get("hp", 100))
	var max_hp       : float = max(1.0, float(state.get("max_hp", 100)))
	var enemy_visible: bool  = bool(state.get("enemy_visible", false))
	var can_heal     : bool  = bool(state.get("can_heal", false))
	var resources    : float = float(state.get("resources", 100))

	# Read the persona weights once per tick
	var aggression := get_weight("aggression", 0.5)
	var caution    := get_weight("caution", 0.5)
	var greed      := get_weight("greed", 0.5)
	var pace       := get_weight("pace", 0.5)

	# Low HP — cautious personas heal first; aggressive push through
	var hp_ratio := hp / max_hp
	if hp_ratio < 0.3 and can_heal and caution > 0.4:
		return "heal"
	# Critical HP — even aggressive players heal when there's a
	# real chance of dying THIS tick.
	if hp_ratio < 0.15 and can_heal:
		return "heal"

	# Enemy in range — decide attack vs defend
	if enemy_visible:
		# Higher aggression than caution → attack. Pace bumps the
		# threshold toward attacking (slower personas defend longer).
		var attack_threshold := 0.5 + (caution - aggression) * 0.4 + (0.5 - pace) * 0.2
		if randf() > attack_threshold:
			return "attack"
		return "defend"

	# No enemy visible
	if greed > 0.6 and resources > 10:
		return "explore"
	if pace < 0.4:
		return "wait"
	return "explore"


# ----------------------------------------------------------------
# Tick loop
# ----------------------------------------------------------------

func _process(delta: float) -> void:
	if not active:
		return
	if decision_callback.is_null() and actions.is_empty():
		return
	_accum += delta
	if _accum < tick_seconds:
		return
	_accum = 0.0

	# Bail on runaway loops
	_tick_count += 1
	if _tick_count > max_ticks:
		stop("max_ticks reached")
		return

	# Snapshot state
	var state := {}
	if not state_callback.is_null():
		var built = state_callback.call()
		if typeof(built) == TYPE_DICTIONARY:
			state = built

	# Decide
	var decision
	if not decision_callback.is_null():
		decision = decision_callback.call(state)
	else:
		decision = default_policy(state)

	if decision == null:
		return
	var action_name := ""
	var args := {}
	if typeof(decision) == TYPE_STRING:
		action_name = decision
	elif typeof(decision) == TYPE_DICTIONARY:
		action_name = str(decision.get("action", ""))
		# Pass everything except the action key through to the
		# handler — gives the decision callback a structured way to
		# parameterise the action (e.g. {"action": "attack",
		# "target": enemy_id}).
		args = decision.duplicate()
		args.erase("action")

	if action_name == "":
		return
	if log_actions and has_node("/root/Anvil"):
		Anvil.event("auto_action", {"action": action_name,
			"tick": _tick_count})
	if actions.has(action_name):
		actions[action_name].call(args)
	else:
		if has_node("/root/Anvil"):
			Anvil.event("auto_action_missing", {"action": action_name})


# ============================================================
# Worked example — turn-based combat
# ============================================================
#
# Add this to your main scene's _ready (e.g. main.gd):
#
#     extends Node2D
#
#     var player_hp := 100
#     var enemy_hp := 80
#     var turn := 0
#     const PLAYER_ATK := 12
#     const ENEMY_ATK := 8
#     const HEAL_AMOUNT := 25
#     var defending := false
#
#     func _ready() -> void:
#         AutoPlayer.register_state_builder(_state)
#         AutoPlayer.register_decision(AutoPlayer.default_policy)
#         AutoPlayer.register_action("attack", _do_attack)
#         AutoPlayer.register_action("defend", _do_defend)
#         AutoPlayer.register_action("heal",   _do_heal)
#         AutoPlayer.register_action("wait",   _do_wait)
#         AutoPlayer.tick_seconds = 0.05
#         AutoPlayer.active = true
#
#     func _state() -> Dictionary:
#         return {
#             "hp":            player_hp,
#             "max_hp":        100,
#             "enemy_visible": enemy_hp > 0,
#             "can_heal":      true,
#         }
#
#     func _do_attack(_args) -> void:
#         enemy_hp -= PLAYER_ATK
#         _resolve_enemy_turn()
#
#     func _do_defend(_args) -> void:
#         defending = true
#         _resolve_enemy_turn()
#         defending = false
#
#     func _do_heal(_args) -> void:
#         player_hp = min(100, player_hp + HEAL_AMOUNT)
#         _resolve_enemy_turn()
#
#     func _do_wait(_args) -> void:
#         _resolve_enemy_turn()
#
#     func _resolve_enemy_turn() -> void:
#         turn += 1
#         var dmg := ENEMY_ATK if not defending else int(ENEMY_ATK / 2)
#         player_hp -= dmg
#         if player_hp <= 0:
#             Anvil.metric("outcome", "defeat")
#             Anvil.metric("final_hp", 0)
#             Anvil.metric("turns",    turn)
#             AutoPlayer.stop("defeat")
#             get_tree().quit()
#         elif enemy_hp <= 0:
#             Anvil.metric("outcome", "victory")
#             Anvil.metric("final_hp", player_hp)
#             Anvil.metric("turns",    turn)
#             AutoPlayer.stop("victory")
#             get_tree().quit()
#
# Run a sweep over a persona axis and you'll see how the four
# default personas behave differently: Aggressive trades hits
# fastest but eats the most damage; Cautious heals through and
# wins on stamina; Greedy ignores hp until it's critical; Relaxed
# defends most turns and either grinds out a win or runs out of
# turns. That difference IS the balance test.
# ============================================================
