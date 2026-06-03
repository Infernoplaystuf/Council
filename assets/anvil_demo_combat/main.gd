# ============================================================
# Anvil Demo — Combat
# ============================================================
# A tiny auto-resolved turn-based fight. Each "tick" of the
# AutoPlayer represents one player turn. The AutoPlayer asks
# us for the state, runs default_policy against the persona
# weights, and picks one of (attack / defend / heal / wait).
# We resolve the enemy turn and check for win/lose.
#
# Run headlessly via Anvil:
#   Backend: godot
#   Project: <this folder>
#   Duration: ~5 seconds is plenty
#   Sweep:    a persona axis ({"names": "all"})
#
# What you should see across the eight built-in personas:
#   Aggressive   — quick wins or quick losses, low final_hp
#   Cautious     — long fights, high final_hp, good win rate
#   Greedy       — flips between attack and heal late
#   Hardcore     — high win rate even at low hp
#   Relaxed      — defends most turns; turn counts inflate
#   Speedrunner  — attacks every turn regardless of damage
#   Completionist— similar to Cautious but more healing
#   Casual       — frequent waits → loses to timeout / chip damage
# ============================================================

extends Node2D


# Tunable game parameters. The runner can override these via the
# Anvil params dict — try {"player_atk": 16} in a sweep axis to
# see the balance shift. We read them in _ready so a sim
# parameter pass-through Just Works.
var player_max_hp := 100
var player_atk    := 12
var player_def    := 2          # damage reduction when defending
var enemy_max_hp  := 80
var enemy_atk     := 8
var heal_amount   := 25
var max_turns     := 50         # bail to avoid eternal fights

# Live state
var player_hp := 100
var enemy_hp  := 80
var turn      := 0
var heals_used := 0
var defends    := 0
var attacks    := 0

# UI references
@onready var status_label: Label = $StatusLabel
@onready var log_label:    Label = $LogLabel
var _log_lines: PackedStringArray = PackedStringArray()


func _ready() -> void:
	# 1) Honour Anvil param overrides if a sweep set them.
	#    Each call falls back to the script default when the key
	#    isn't in this run's anvil_params.json.
	player_max_hp = int(Anvil.get_param("player_max_hp", player_max_hp))
	player_atk    = int(Anvil.get_param("player_atk",    player_atk))
	player_def    = int(Anvil.get_param("player_def",    player_def))
	enemy_max_hp  = int(Anvil.get_param("enemy_max_hp",  enemy_max_hp))
	enemy_atk     = int(Anvil.get_param("enemy_atk",     enemy_atk))
	heal_amount   = int(Anvil.get_param("heal_amount",   heal_amount))
	player_hp = player_max_hp
	enemy_hp  = enemy_max_hp

	# 2) Wire the AutoPlayer.
	AutoPlayer.tick_seconds = 0.05
	AutoPlayer.register_state_builder(_state_snapshot)
	AutoPlayer.register_decision(AutoPlayer.default_policy)
	AutoPlayer.register_action("attack", _do_attack)
	AutoPlayer.register_action("defend", _do_defend)
	AutoPlayer.register_action("heal",   _do_heal)
	AutoPlayer.register_action("wait",   _do_wait)
	AutoPlayer.register_action("explore", _do_wait)   # alias
	AutoPlayer.active = true

	status_label.text = "Persona: %s   |   HP %d/%d  vs  Enemy %d/%d" % [
		Anvil.persona_name("balanced"),
		player_hp, player_max_hp, enemy_hp, enemy_max_hp,
	]


# ----------------------------------------------------------------
# State snapshot — read by AutoPlayer.default_policy each tick
# ----------------------------------------------------------------
func _state_snapshot() -> Dictionary:
	return {
		"hp":            player_hp,
		"max_hp":        player_max_hp,
		"enemy_visible": enemy_hp > 0,
		"can_heal":      true,
		"resources":     100,
	}


# ----------------------------------------------------------------
# Action handlers
# ----------------------------------------------------------------
func _do_attack(_args) -> void:
	attacks += 1
	enemy_hp -= player_atk
	_log("attack  → enemy %d" % enemy_hp)
	_resolve_enemy_turn(false)


func _do_defend(_args) -> void:
	defends += 1
	_log("defend")
	_resolve_enemy_turn(true)


func _do_heal(_args) -> void:
	heals_used += 1
	player_hp = mini(player_max_hp, player_hp + heal_amount)
	_log("heal    → hp %d" % player_hp)
	_resolve_enemy_turn(false)


func _do_wait(_args) -> void:
	_log("wait")
	_resolve_enemy_turn(false)


# ----------------------------------------------------------------
# Enemy turn + end-of-fight detection
# ----------------------------------------------------------------
func _resolve_enemy_turn(player_defended: bool) -> void:
	turn += 1
	if enemy_hp <= 0:
		_finish("victory")
		return
	var dmg := enemy_atk
	if player_defended:
		dmg = max(0, enemy_atk - player_def * 2)
	player_hp -= dmg
	status_label.text = "Persona: %s   |   HP %d/%d  vs  Enemy %d/%d  (turn %d)" % [
		Anvil.persona_name("balanced"),
		player_hp, player_max_hp, enemy_hp, enemy_max_hp, turn,
	]
	if player_hp <= 0:
		_finish("defeat")
	elif turn >= max_turns:
		_finish("timeout")


func _finish(outcome: String) -> void:
	# Cap negative HP at 0 so the metric reads cleanly
	var final_hp := maxi(0, player_hp)
	Anvil.metric("outcome",    outcome)
	Anvil.metric("final_hp",   final_hp)
	Anvil.metric("turns",      turn)
	Anvil.metric("attacks",    attacks)
	Anvil.metric("defends",    defends)
	Anvil.metric("heals_used", heals_used)
	Anvil.event("combat_end", {
		"outcome":  outcome,
		"final_hp": final_hp,
		"turns":    turn,
		"persona":  Anvil.persona_name("balanced"),
	})
	AutoPlayer.stop(outcome)
	# Brief delay before quitting so the autoload's stderr drainers
	# can flush. Anvil's runner caps duration anyway, but a clean
	# self-quit produces the cleanest record.
	get_tree().create_timer(0.1).timeout.connect(_quit)


func _quit() -> void:
	get_tree().quit()


# ----------------------------------------------------------------
# Tiny log buffer for the on-screen text
# ----------------------------------------------------------------
func _log(line: String) -> void:
	_log_lines.append("T%d: %s" % [turn, line])
	# Keep the last ~12 lines visible
	while _log_lines.size() > 12:
		_log_lines.remove_at(0)
	log_label.text = "\n".join(_log_lines)
