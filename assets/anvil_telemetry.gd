# ============================================================
# anvil_telemetry.gd  —  drop-in Godot 4 autoload for Anvil sims
# ============================================================
#
# Install
# -------
# 1. Copy this file into your Godot project (anywhere — the convention
#    is "res://autoloads/anvil_telemetry.gd").
# 2. Project Settings → Autoload, add it with the node name "Anvil".
#    Save the dialog with "Enable" ticked.
# 3. From any script, call Anvil.metric() / Anvil.event() to feed
#    telemetry back to Anvil.
#
# What it does
# ------------
# * On _ready, reads `user://anvil_params.json` (written by Anvil's
#   GodotSimRunner before each run). Param values land in the
#   `params` dict on this node, accessible as `Anvil.get_param("name",
#   default)`.
# * Also reads `res://anvil_params.json` — the runner writes there
#   for projects that don't want to use the user:// scheme. The
#   res:// copy wins when both exist so an explicit override is
#   easy.
# * Exposes the print-line API Anvil's stdout parser understands:
#     Anvil.metric("score", 1234)
#     Anvil.event("died", {"x": 100.5, "y": 42, "cause": "spike"})
# * Optionally auto-emits common metrics (fps, vram) every N
#   seconds when `auto_emit_perf` is true.
#
# Convention
# ----------
# Metric line:  ANVIL_METRIC: <name> = <value>
# Event line:   ANVIL_EVENT: <name> [k=v ...]
#
# Anvil reads stdout, parses these prefixes, and ignores everything
# else. So adding this autoload is non-invasive — your existing
# print() calls keep working.
# ============================================================

extends Node

# When true, Anvil.auto_perf_seconds determines how often a
# fps/vram metric pair is emitted. Set this to false in the editor
# inspector or via code before _ready if you don't want it.
@export var auto_emit_perf: bool = false
@export var auto_perf_seconds: float = 1.0

# Filled by _ready() from anvil_params.json. Read via get_param().
var params: Dictionary = {}

# Internal — accumulates time between auto-perf emits.
var _perf_accum: float = 0.0


func _ready() -> void:
	_load_params()
	if auto_emit_perf:
		set_process(true)
	else:
		set_process(false)
	event("anvil_ready", {"params": JSON.stringify(params)})


# ----------------------------------------------------------------
# Telemetry API
# ----------------------------------------------------------------

## Emit a single metric.
## ``value`` can be a number (int, float) or a string. Strings make
## sense for categorical metrics like a tier name.
func metric(name: String, value) -> void:
	print("ANVIL_METRIC: %s = %s" % [name, str(value)])


## Emit an event with an optional key→value payload dict.
## Anvil parses ``key=value`` pairs; non-ASCII keys are quoted.
func event(name: String, data: Dictionary = {}) -> void:
	if data.is_empty():
		print("ANVIL_EVENT: %s" % name)
		return
	var parts := PackedStringArray()
	for k in data.keys():
		var v = data[k]
		var v_str := ""
		# Numbers print plainly; strings get wrapped in double quotes
		# when they contain whitespace so the parser sees one token.
		if typeof(v) == TYPE_INT or typeof(v) == TYPE_FLOAT:
			v_str = str(v)
		else:
			var s := str(v)
			if " " in s or "\t" in s or s == "":
				v_str = '"' + s.replace('"', "'") + '"'
			else:
				v_str = s
		parts.append("%s=%s" % [str(k), v_str])
	print("ANVIL_EVENT: %s %s" % [name, " ".join(parts)])


# ----------------------------------------------------------------
# Param lookup
# ----------------------------------------------------------------

## Return the value Anvil sent for ``name``, or ``default`` if the
## sim was launched without that key.
func get_param(name: String, default = null):
	if params.has(name):
		return params[name]
	return default


# ----------------------------------------------------------------
# Per-frame work for auto-perf emission
# ----------------------------------------------------------------

func _process(delta: float) -> void:
	if not auto_emit_perf:
		return
	_perf_accum += delta
	if _perf_accum < auto_perf_seconds:
		return
	_perf_accum = 0.0
	metric("fps", Engine.get_frames_per_second())
	# RenderingServer.get_rendering_info() returns a Dictionary in
	# Godot 4; pick a couple of useful keys.
	var vram := RenderingServer.get_rendering_info(
		RenderingServer.RENDERING_INFO_VIDEO_MEM_USED
	)
	if vram > 0:
		metric("vram_bytes", vram)


# ----------------------------------------------------------------
# Param loading
# ----------------------------------------------------------------

func _load_params() -> void:
	# Try res:// first (override), then user:// (default location).
	for p in ["res://anvil_params.json", "user://anvil_params.json"]:
		if not FileAccess.file_exists(p):
			continue
		var f := FileAccess.open(p, FileAccess.READ)
		if f == null:
			continue
		var text := f.get_as_text()
		f.close()
		if text.is_empty():
			continue
		var parsed = JSON.parse_string(text)
		if typeof(parsed) == TYPE_DICTIONARY:
			# Merge — later wins (res:// is checked first, so a
			# user:// fallback only fills in unset keys).
			for k in parsed.keys():
				if not params.has(k):
					params[k] = parsed[k]
