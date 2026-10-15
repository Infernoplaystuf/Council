# Typhon plugin API 0.x: working notes

These notes describe the plugin API as it stands on branch `typhon-plugins`.
The design is `docs/typhon_plugins_design.md` (sections 3, 4 and 10). The
API is numbered 0.x while the built-in plugins shape it, and any change is
still allowed. It is frozen as 1.0 at stage P5, together with the author's
guide of design 3.11, and only then are plugins from other people loaded.
Until then these notes are the record of what exists.

**Version now: 0.1** (stage P1 row 2, 15 October 2026).

## The pieces

| Piece | Where | What it is |
|---|---|---|
| The API a plugin imports | `typhon_plugin_api/__init__.py` | `API_VERSION`, `has()`, `EVENTS`, the types (`CameraHandle`, `FrameView`, `CropBox`), the protocols (`PluginContext`, `FeaturePlugin`), and today's classes re-exported (below) |
| The manifest reader | `council_core/plugin_manifest.py` | Reads and checks `plugin.json` as data and imports nothing |
| The base set and project lists | `council_core/plugin_lists.py` | `<plugin home>/base_set.json` and `<project>/typhon_plugins.json`, and the plugin home itself |
| The host | `council_core/plugin_host.py` | The built-in list, discovery, lazy loading, the guard and faults, contexts, events, the UI queue, live-frame feeds and the watchdog |
| The rig | `council_core/camera_rig.py` | `rig.plugins` is the host, made on first use. Each slot's `plugin_state` holds a plugin's per-camera state |
| The built-in plugins | `typhon_plugins/<name>/` | Seven adapters, each a manifest plus an empty `plugin.py` at this stage |

## The rules that already hold

- **A plugin imports only `typhon_plugin_api`**, plus the standard library
  and its own files. The built-in plugins obey this too, and a test checks
  every import in `typhon_plugins/`.
- **Built-in means on the list.** `plugin_host.BUILTIN_PLUGINS` names the
  seven built-in plugins. Any other folder in `typhon_plugins/` is refused
  and never loaded.
- **Third-party plugins** are folders under `<plugin home>/plugins/`, each
  named after its id with the dots turned into underscores
  (`acme.thermal` goes in `acme_thermal/`), as question 7 asks. They are
  found and listed with the state "needs approval", and they are never
  imported before stage P5. A folder that holds compiled code (`.pyc`,
  `__pycache__`, `.pyd`, `.dll`, `.so`) is refused.
- **The plugin home** is `TYPHON_PLUGIN_HOME` when that is set. Otherwise
  it is the folder the project recorded (written home-relative), and
  otherwise `~/.council/typhon`. Reading the home never creates anything.
- **Lazy loading.** Start-up reads manifests only. A plugin is imported as
  `typhon_plugins.<name>.<module>` the first time it is needed, which is
  `host.instance()` or a call into it. Its object is made with no arguments,
  and then `activate(ctx)` runs.
- **No pickle anywhere.** JSON only, and a test checks every module of the
  plugin system.

## The manifest, schema 1

The fields are those of design 3.11: `schema`, `id`, `short`, `name`,
`version`, `api`, `kind`, `entry` (all required), plus `author`,
`description` and `licence`, which third-party plugins must also give. The
optional fields are `scope`, `needs`, `python`, `contributes`, `files`,
`uses`, `network` and `budget_ms`. The reader is strict:

- an unknown field, a duplicate key, NaN or Infinity, a file over 64 KB,
  text that is not UTF-8, or anything but one JSON object is refused, with
  the field and the reason;
- `typhon.` ids are kept for built-in plugins. `record_gate` and
  `frame_stage` are for built-in plugins only, and `review_source` is for
  camera plugins only. `overlay` is reserved and refused until it is built;
- a third-party plugin's run files are named `_<short>_<what>.<ext>`. No
  plugin may claim a file the core writes (`_frame_`, `_frames.csv`,
  `_camera.json`, `_run_end.json`, `_events.raw`, `_rig.json`,
  `_rig_end.json`);
- `python` lists **import names** with optional version bounds
  (`"sklearn"`, `"numpy>=1.24"`), not pip names. The reason is that pip
  names do not always match what is installed: `PySide6` is installed as
  `PySide6-Essentials` in the `council` environment, so a pip-name check
  wrongly said it was missing. Each module is located in the running
  interpreter without being imported, and its version is read from the
  package that provides it. A missing package makes the plugin
  "incompatible", and the reason says what to install. Typhon installs
  nothing (question 8).

**The API version rule.** While the API is 0.x, a plugin must name exactly
this 0.x. From 1.0 on, it must name the same major version and a minor no
later than Typhon's (design 3.2). A refusal names both numbers.

## Plugin states

The states are those of design 4.3: discovered (no project open),
incompatible, needs approval, missing (a list names a plugin that is not on
this PC), on, off, active (loaded) and faulted. "Asked after a crash" comes
with the crash marker at stage P4.

## The base set and a project's list (questions 1 and 14)

- `base_set.json` holds the plugins a new project starts with and each
  plugin's **default** settings. Without the file, the base set is every
  built-in plugin with no defaults, and reading never writes the file.
- `typhon_plugins.json` holds a **copy** of the base set taken when the
  project was made (`base_set`), plus `added`, `off`, `settings` (this
  project's overrides), `plugin_home` and `made`. It is written when a
  "Typhon (plugins)" project is built (`gui_examples.PLUGIN_LIST`), or by
  `host.open_project(dir, create=True)`.
- **What a project runs** is its copy of the base set, plus `added`, minus
  `off`. A later change to the base set's list reaches only projects made
  after it. This is question 1, and it holds for additions and removals
  alike.
- **A plugin's settings** are the base set's defaults with the project's
  overrides laid over them, key by key. `ctx.save_settings()` writes this
  project's overrides only (question 14). `host.make_default(pid, keys)` is
  the later "make this the default for every project" button: it moves
  those values into the base set and drops them from this project, so every
  project that has not overridden them follows the new defaults.
- Values must be plain: true or false, text up to 4,096 characters, a whole
  number that fits in 64 bits, or a finite number. A plugin's settings may
  be up to 64 KB.
- Every change re-reads the file under an OS lock on a `.<name>.lock`
  sidecar file. The file is then written whole, under a temporary name
  unique to that writer (process, thread and a counter), and renamed into
  place. A file that is damaged or was written by a newer Typhon is refused
  and left as it is.

## The context (`activate(ctx)`)

| Call | 0.1 |
|---|---|
| `ctx.plugin_id` | the plugin's id |
| `say(text)` | Puts a line in the status line, named as the plugin's. From a thread other than the UI thread, the line goes through the UI queue |
| `call_in_ui(fn)` | Runs `fn` on the UI thread: through the window's `ui_caller` once it is wired (stage P4), and through `host.pump_ui()` until then |
| `cameras()` | Every slot as a `CameraHandle`. Capabilities come from what today's `Device` says of itself (`frames` or `events`, `restartable`, `area_live`, `grab_failures`, `raw_recording`). `facet()` answers `None` until stage P3 |
| `active_camera()` | the active slot's name |
| `capture_folder(camera=None)` | The slot's folder: the plugin's own slot for a camera-scoped feature, otherwise the active slot |
| `current_frame()`, `crop_box()` | `None` until the window wires them (stage P4) |
| `on(event, fn)` | For one of `EVENTS`. `fn` gets one read-only mapping. Returns a function that stops listening |
| `settings()`, `save_settings(values)` | See the previous section |
| `state(camera=None)` | the plugin's dictionary on that slot (session only, never saved) |
| `temp_dir()` | A folder of the plugin's own, which the host removes at `close()` |

The two file services of design 3.9 (`create_file`, `write_new`) and
`read_data` are not in 0.1. They come at stages P3 and P5.
`has("context.write_new")` answers False until then.

## Calls into a plugin, and faults (design 3.10)

A fault ends in one of three ways:

1. **Best effort.** This covers every hook except the two below. Any
   exception, `SystemExit` included, switches the plugin off for the
   session ("faulted"). The fault is said in the status line, its traceback
   is appended to `<project>/plugin_errors.log`, and the caller carries on
   with a default value. A faulted plugin is never called again. Its feeds
   stop and its listeners are skipped.
2. **The recording path.** `host.record_gate(pid, gate, session)` wraps a
   plugin's record gate, which is the `gate=` of
   `CaptureSession.record_to`. If the gate fails, the plugin is faulted and
   the recording **ends at that frame**, through the new
   `FrameWriter.end_recording(reason, frame)`. Frames queued before the
   fault still land, because they were whole. The frame at the fault is
   counted once, as offered and not saved. Nothing after it is taken, and
   `<run>_run_end.json`'s `recording_failed` gives the reason, naming the
   plugin, the hook and the frame. A gate whose plugin was faulted elsewhere
   ends the recording at the next frame.
3. **The start check.** `host.check_start(plan)` asks only the plugins whose
   manifest lists `run_hooks`. A plugin returns a reason to refuse Start. A
   check that **fails** refuses that Start with the fault and switches the
   plugin off, so the next Start runs without it.

**Hangs.** `host.watchdog()`, or `start_watchdog()` on its own thread,
switches off a call that has run longer than 2 s. Python cannot interrupt
the call itself.

## Live frames (design 3.8)

`host.feed(pid)` returns a `LiveFeed`. The grab thread's part, `offer(frame)`,
puts the frame into a two-slot queue: when the queue is full, the oldest
frame is dropped and counted, and nothing is copied. On the plugin's own
thread, `on_frame` then gets a `FrameView`. Its `image` is a read-only view
of the frame's pixels, and its `meta` is read-only. `saved` is `None` in 0.1.
`duty=0.25` caps the plugin's busy share of the time. `budget_ms` counts the
calls that ran over the budget and does not stop them.

**What a live plugin costs inside the real Qt Typhon** is measured by
`tools/typhon_plugins_timing.py`; the numbers and what they mean for stage
P3 are in `docs/typhon_plugins_branch.md` ("Frame timing with a slow
plugin"). In short, a live plugin is safe only with the 1 ms switch
interval and timer while recording (P3 row 5) and with a duty cap.

## What is wired, and what comes when

Nothing in `frame_camera` calls the host yet, so today's Typhon behaves
exactly as before, and a test runs a whole capture and finds the host never
made or imported. `FrameWriter.end_recording` is the one addition to the
recording code, and only the host calls it.

| Stage | Adds to the API |
|---|---|
| P2 | The camera plugins (`CameraPlugin`: `available`, `discover`, `open`, `capabilities`, `categories`, `setup_guide`, `record_fields`, `review_source`) over today's Backend, Device and Provider classes. Capabilities declared in the manifest and confirmed by the opened camera |
| P3 | The run hooks (`on_run_started`, `on_run_finished`; `check_start` wired into Start). `RunPlan` and `RunInfo`. The facets (`SoftwareTrigger`, `HardwareTrigger`, `Binning`) and `CaptureOptions` and `CaptureSetup` in the API. The record gate wired through `record_gate`. The run-file suffix registry. Record blocks. The two file services. `FrameView.to_sensor` and its coordinate fields. The finer timer while recording (row 5) |
| P4 | The screen: pop-out panels, toolbar and status strips, the Plugins window (base set, additions, switch-offs, settings, approval states), `current_frame` and `crop_box` wired |
| P5 | The hardened loader for third-party plugins (approval, the hash-then-compile importer, `read_data`), the API frozen as 1.0, and the author's guide and `python -m typhon_plugin_api.check` |

## Decisions taken here that the design left open

- The project file records the base set it was made with as `base_set`. The
  plugin home is recorded as `plugin_home`. The design's example used
  `base` for the plugin home, before question 1 was answered; once each
  project needed its own copy of the base set, `base` would have been
  ambiguous.
- A removal from the base set follows the same rule as an addition
  (question 1): it reaches only projects made afterwards.
- `make_default` moves the values into the base set and drops them from the
  project, so the project follows the defaults from then on.
- Built-in plugins' versions are their own (`0.1.0`), not the Council's.
- A `typhon_plugins` project is never built with a plugin home under
  `~/.council` while it runs beside today's Typhon. This is the same refusal
  as for its vault.
