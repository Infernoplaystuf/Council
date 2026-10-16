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
  `__pycache__`, `.pyd`, `.dll`, `.so`) is refused, and so is one with
  more than 2,000 files, with a reason of its own.
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
  `_rig_end.json`), nor a name that holds one of those endings anywhere:
  the core splits a file name at the first ending it holds, so
  `<run>_ab_frame_000001.png` would read as a frame of a run named
  `<run>_ab`. A third-party name may not hold `_capture_triggers.csv`
  either, which only the built-in triggering plugin writes;
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
- **A damaged project list runs only the built-in camera plugins** until
  it is mended, so the project's cameras still open. It never runs the
  base set as it is now: that would run plugins the project switched off,
  and plugins added to the base set after the project was made (question
  1). The window says so, and changes nothing in the file.
- **Nothing is written under `~/.council` beside today's Typhon.** While
  the branch runs beside today's Typhon (`plugin_lists.
  BESIDE_TODAYS_TYPHON`, until P7), a base set under `~/.council` is
  refused, before even its lock file is made, and so is a new project
  that would record a plugin home there. This matters for a
  double-clicked app whose recorded plugin home is gone: the home falls
  back to `~/.council/typhon` (design 4.1). The build refuses the same
  home (`designer_examples.plugin_home_problem`, which uses the same
  check).

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

**`has()` names a hook only once Typhon calls it:** `hook.activate`,
`hook.deactivate` (the host) and `hook.on_frame` (the host's live feeds).
`hook.check_start` joins when Start asks it (P3) and `hook.status` when
the status strip does (P4); both answered True in 0.1 before the P1
review, although nothing called them.

## Calls into a plugin, and faults (design 3.10)

**What counts as a fault.** Every call into a plugin is guarded, and so is
looking the hook up: `getattr` runs plugin code (a property, a
`__getattr__`), and before the P1 review its error escaped `call()` and
`check_start()`. Any exception counts, `SystemExit` and
`KeyboardInterrupt` included. The one exception is a `KeyboardInterrupt`
on the main thread outside the recording path: that is the user's Ctrl+C,
wherever it landed, so it goes on up and no plugin is blamed for it.

A fault ends in one of three ways:

1. **Best effort.** This covers every hook except the two below. The
   plugin is switched off for the session ("faulted"). The fault is said
   in the status line, its traceback is appended to
   `<project>/plugin_errors.log`, and the caller carries on with a default
   value. A faulted plugin is never called again, with one exception: its
   record gate serves out the run in progress (2). Its feeds stop and its
   listeners are skipped.
2. **The recording path.** `host.record_gate(pid, gate, session)` wraps a
   plugin's record gate for one run; it is the `gate=` of
   `CaptureSession.record_to`.
   - **It belongs to its run's writer.** `record_to` hands the gate the
     run's `FrameWriter` before it calls anything of the gate (a
     three-line addition to `capture.py`; today's `TriggerRun` has no
     `bind_writer`, so nothing changes for it). A fault ends that
     writer's recording, never a later run's. A fault that comes after
     its run's writer has closed, such as a late `saved()` while the last
     frames of a stopped run land, is logged and switches the plugin off,
     and changes nothing else.
   - **When the gate fails**, in `tag`, `saved`, `failed_grab` or
     `confirm_saves`, on any thread, it is marked ended first, so the grab
     thread asks the plugin nothing more from that moment. Then its
     writer's recording ends (`FrameWriter.end_recording`), then the host
     switches the plugin off and logs it. `<run>_run_end.json`'s
     `recording_failed` gives the reason, naming the plugin, the hook and,
     for a tag, the frame.
   - **The frame at a tag fault is saved and marked.** Its row in the
     frames CSV says `error: plugin fault`, as today's capture marks a
     gate that raises (design 3.6), and the recording ends with it:
     nothing after it is taken (3.10). It counts as saved, not as a failed
     write. A fault in `confirm_saves`, `saved` or `failed_grab` has no
     frame of its own, so the recording ends at once. Frames already
     queued still land, because they were whole; the plugin is not told of
     them, so its books stop where the reason says.
   - **When its plugin is switched off elsewhere**, it depends on why. A
     fault in a best-effort hook (`status`, a panel, a listener,
     `on_run_started`) never stops a capture (3.10): the gate serves out
     the run in progress, so the run stays tagged whole, and the plugin
     gets no gate in the next run. A hang (the watchdog), a fault on the
     recording path (another camera's gate of the same plugin), a failed
     load, or a fault the plugin already had when the gate was made all
     end the run at the next frame instead, without asking the plugin
     anything, because a hung plugin may hold what its gate needs. That
     frame is saved and marked as above.
3. **The start check.** `host.check_start(plan)` asks only the plugins whose
   manifest lists `run_hooks`. A plugin returns a reason to refuse Start. A
   check that **fails** refuses that Start with the fault and switches the
   plugin off, so the next Start runs without it.

**Hangs.** The watchdog switches off a call that has run longer than 2 s.
Python cannot interrupt the call itself. The host starts the watchdog on
a thread of its own when the first plugin is loaded or fed frames, and
stops it at `close()`; a host that loads nothing starts no thread.
`host.watchdog()` still runs one look by hand.

**Loading holds up no other plugin.** A plugin is imported and activated
outside the host's lock. One that is slow (sklearn takes 3-5 s to import)
or hangs in `activate` holds up only callers of that same plugin, who wait
for it and are let go when the watchdog switches it off. Before the P1
review, calls into every other plugin, `check_start` and `open_project`
waited too. A plugin still being activated when its project is closed or
it is switched off is deactivated when its `activate` returns.

## Live frames (design 3.8)

`host.feed(pid)` returns a `LiveFeed`. The grab thread's part, `offer(frame)`,
puts the frame into a two-slot queue: when the queue is full, the oldest
frame is dropped and counted, and nothing is copied. On the plugin's own
thread, `on_frame` then gets a `FrameView`. Its `meta` is read-only.
`saved` is `None` in 0.1.

**The pixels cannot be unlocked by accident.** `image` is an array over a
read-only buffer of the frame's pixels (`plugin_host.locked_pixels`), so
writing to it raises, and so do `flags.writeable = True` and
`setflags(write=True)`. Nothing is copied. Before the P1 review it was a
plain read-only view, which NumPy lets anyone make writeable again while
the frame's own array is writeable, and the review changed the pixels the
writer saved that way. The frame's own array is left as it is, so the core
is unchanged. A plugin that reaches through `.base.obj` on purpose still
finds the frame's array: design 10.3 lists that among what a plugin in
Typhon's process can still do. This protects the view that `on_frame`
gets; design 3.8's "read-only from `read()` until the pool reuses it" is
not needed for it, and is looked at again at P3 if plugins get frames any
other way (the frame stage is built-in only).

**The duty cap is on by default:** `feed()` caps a plugin's busy share of
the time at 25 % (`DUTY_CAP`, design 3.11's hook table). `duty=None` turns
it off; the timing bench does so to measure the uncapped case.
`budget_ms` counts the calls that ran over the budget and does not stop
them: the duty cap and the watchdog are what act.

**What a live plugin costs inside the real Qt Typhon** is measured by
`tools/typhon_plugins_timing.py`; the numbers and what they mean for stage
P3 are in `docs/typhon_plugins_branch.md` ("Frame timing with a slow
plugin"). In short, a plugin that is always busy starves the grab thread
at today's settings, and a 1 ms switch interval with the duty cap prevents
that. A 5 ms plugin was harmless in some runs and made most frames late in
others on the same laptop, depending on what else the PC was doing, and
the 1 ms timer request showed no effect of its own. So each run now
records the PC's load, and P3 row 5 is decided on a quiet PC and on the
bench PC.

## What is wired, and what comes when

Nothing in `frame_camera` calls the host yet, so today's Typhon behaves
exactly as before, and a test runs a whole capture and finds the host never
made or imported. The recording code has two additions, and neither does
anything for today's Typhon: `FrameWriter.end_recording`, which only the
host calls, and `record_to` handing a gate its run's writer
(`bind_writer`), which today's `TriggerRun` does not have. The rig reaches
the host by a name built at run time, so the host's modules are not on
today's Typhon's Settings > Python Scripts list either (the P1 review
found 6 extra files there, 35 to 41, and a slower cold list).

| Stage | Adds to the API |
|---|---|
| P2 | The camera plugins (`CameraPlugin`: `available`, `discover`, `open`, `capabilities`, `categories`, `setup_guide`, `record_fields`, `review_source`) over today's Backend, Device and Provider classes. Capabilities declared in the manifest and confirmed by the opened camera. `camera_setup.json`'s choice read as the project's plugin list |
| P3 | The run hooks (`on_run_started`, `on_run_finished`; `check_start` wired into Start, and `has("hook.check_start")`). `RunPlan` and `RunInfo`. The facets (`SoftwareTrigger`, `HardwareTrigger`, `Binning`) and `CaptureOptions` and `CaptureSetup` in the API. The record gate wired through `record_gate`. The run-file suffix registry. Record blocks, and the run records' `plugins` block. The two file services, and `<project>/plugin_data/<id>/` for a plugin's own data in a project (design 4.1; nothing makes it in 0.1). `FrameView.to_sensor` and its coordinate fields, and `saved`. The finer timer while recording (row 5), decided on a quiet PC and on the bench PC. Whether plugins need the frame's own array read-only from `read()` (design 3.8), if they get frames any other way than a `FrameView` |
| P4 | The screen: pop-out panels, toolbar and status strips, the Plugins window (base set, additions, switch-offs, settings, approval states, each plugin's over-budget and dropped-frame counts), `current_frame` and `crop_box` wired. `status()` asked by the status strip and held to its limits, 80 characters and 5 ms (design 3.5, 3.11), with `has("hook.status")`. The crash marker and "asked after a crash". A plugin's Python packages checked in the project's own interpreter (`python_envs`) when the Council lists a project's plugins; inside the app the running interpreter is the project's |
| P5 | The hardened loader for third-party plugins (approval, `approvals.json`, the hash-then-compile importer, the `typhon_thirdparty.<folder>` package, `read_data`), the network field and its audit hook, the manifest's `uses` acted on (11.5), the API frozen as 1.0, and the author's guide and `python -m typhon_plugin_api.check` |

Done in the P1 review (16 October 2026), so not on the table: the
watchdog starting by itself, the duty cap as the default, `has()` only for
hooks Typhon calls, and frames a plugin cannot unlock by accident.

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
  as for its vault. The host keeps the same rule at run time (above).
- **Design 3.6 against 3.10 (to confirm).** A record gate that raises
  saves the frame it raised on, marked `error: plugin fault`, as today's
  capture does (3.6), and the recording ends with that frame (3.10). The
  frame counts as saved, not as a failed write.
- **A best-effort fault of the gate's own plugin (to confirm).** It does
  not end the run in progress: the gate serves it out, as 3.10 says
  ("never stop a capture"), and the run stays tagged whole. A hang ends
  it at the next frame, because the hung plugin may hold what the gate
  needs. Until the P1 review, any fault of the gate's plugin ended the run.
- **`budget_ms` is counted, never enforced:** the duty cap and the
  watchdog are what act on a slow live plugin.
- **A damaged project list runs only the built-in camera plugins** (above),
  not the base set as it is now.
