# Typhon plugin API 0.x: working notes

These notes describe the plugin API as it stands on branch `typhon-plugins`.
The design is `docs/typhon_plugins_design.md` (sections 3, 4 and 10). The
API is numbered 0.x while the built-in plugins shape it, and any change is
still allowed. It is frozen as 1.0 at stage P5, together with the author's
guide of design 3.11, and only then are plugins from other people loaded.
Until then these notes are the record of what exists.

**Version now: 0.3**, stage P3 complete (18 October 2026: rows 1 and 4 on
branch `plugins/p3a`, rows 2, 3 and the first part of 6 on `plugins/p3b`,
row 5 and the rest of 6 on `plugins/p3c`). 0.1 was stage P1 row 2 (15
October 2026); 0.2 stage P2, the camera plugins and, with rows 3 and 4,
their set-up (16-18 October). 0.3 adds the run hooks, the file services,
the record blocks and the preset namespaces (P3 rows 1 and 4: "A run, as
the plugins see it", "Presets and records by namespace"), the facets, the
capture set-up's types, Downsample as the core's frame stage, a FrameView's
coordinates and the triggering plugin (rows 2 and 3: "The capture set-up,
the facets and Downsample", "Triggering"), and a built-in plugin's frame
stage after Downsample, read-only frames on the recording path, the fake
host an author can load a plugin's folder into and a contract suite for
every kind (row 6: "The fake host and the contract suites"), each named by
`has()`. Row 5, the finer timer, is the core's own and no API: Python's
switch interval at 1 ms while a run records ("What is wired, and what comes
when"). The number stays 0.3: everything in part C is an addition, named by
`has()`, and nothing outside the branch names 0.3.

**The P3 review (19 October 2026, branch `plugins/p3-fixed`)** changed what
the notes below say in these places, each marked "(P3 review)" where it is
described: every call Start, Stop and the end of a run make into a plugin is
bounded in time; a block is cleaned inside the guarded, bounded call that
gave it, measured as the record writes it; a record gate's tags are checked
and its summary guarded; a frame stage keeps one picture per run and is
watched; `FrameView.to_sensor` converts the view's own pixels and
`saved_to_sensor` the saved picture's, the camera's own terms read from its
settings, and `has("frame.saved")` is False until P4; a trigger-source
plugin is refused until P6; a frame camera with no capture set-up gets the
core's; a plugin switched off has its context's services closed; a run of
several cameras files each camera's counts; a fault's words are cut in the
records. The number stays 0.3: nothing outside the branch names it, and
`has()` names what changed.

## The pieces

| Piece | Where | What it is |
|---|---|---|
| The API a plugin imports | `typhon_plugin_api/__init__.py` | `API_VERSION`, `has()`, `EVENTS`, the types (`CameraHandle`, `FrameView`, `CropBox`, `CategoryHints`, `ReviewSource`), the protocols (`PluginContext`, `FeaturePlugin`, `CameraPlugin`, `CameraSetup`), the capability names and rule, the set-up types (`Guide`, `Step`, `Check`, `Readiness`), and today's classes re-exported (below) |
| The built-in adapters' reach | `typhon_plugin_api/builtin.py` | What the built-in camera plugins reach of the code that stays in `council_core` (BaslerBackend, EvkBackend, SyntheticBackend and their devices, the raw view's `open_raw`, the settings tabs' tables); from P3 the triggering plugin's code (TriggerRun, the set-up's trigger rows, frame_camera's trigger functions) and the built-in camera plugins' facets over today's Device methods (`DeviceFacet`, `device_facet`). Not part of the API a plugin from someone else may use; it goes at stage P7 |
| The facets and the capture set-up's types | `typhon_plugin_api/__init__.py` | `SoftwareTrigger`, `HardwareTrigger`, `Binning` (`FACETS`, by capability name); `CaptureSetup`, `CaptureOptions`, the modes and Typhon's own sources (re-exported from `council_core/triggered.py`); `TriggerSource`, `TriggerEvent`, `Action` (stage P3 rows 2 and 3) |
| The triggering plugin | `typhon_plugins/trigger/` | `plugin.py`: an adapter over today's trigger code, its trigger slot (`RunTriggers`), Typhon's own sources (`TyphonSource`), its rows, Capture now, status and toolbar; `panel.py`: its pop-out's widget (stage P3 row 2) |
| The fake host | `typhon_plugin_api/fake_host.py` | A stand-in rig to test a plugin on its own: simulated cameras, a temporary project, no window, Typhon's own Start and Stop; `load(folder)` takes a plugin's folder as its author has it (design 9.2, 3.11; P3 row 6) |
| Capabilities | `council_core/camera_capabilities.py` | The names of design 3.4 and the one rule that reads them off an open camera's own flags |
| The manifest reader | `council_core/plugin_manifest.py` | Reads and checks `plugin.json` as data and imports nothing |
| The base set and project lists | `council_core/plugin_lists.py` | `<plugin home>/base_set.json` and `<project>/typhon_plugins.json`, and the plugin home itself |
| The host | `council_core/plugin_host.py` | The built-in list, discovery, lazy loading, the guard and faults, contexts, events, the UI queue, live-frame feeds and the watchdog; from 0.2 the camera-plugin registry |
| The rig | `council_core/camera_rig.py` | `rig.plugins` is the host, made on first use. Each slot's `plugin_state` holds a plugin's per-camera state; its `capabilities` say what its camera can do; its name is its camera's name in the rig (`rename_slot`), and `holding` says which slot has a camera open |
| The rig's names and rig presets | `council_core/camera_rig_file.py` | `<project>/camera_rig.json` (P2 row 3) |
| The set-up's families | `council_core/camera_setup.py` | `camera_setup.json`'s families (`load_families`, `save_families`, still FORMAT 1) and `Family`, one family as the wizard gets it (P2 row 4) |
| The families wizard | `council_qt/widgets/camera_wizard.py` | `CameraFamiliesWizard`, `run_families_wizard`: check boxes, each family's steps, checks and scan (P2 rows 3 and 4); today's `CameraSetupWizard` unchanged |
| The built-in plugins | `typhon_plugins/<name>/` | Seven adapters. The three camera plugins (Basler, Prophesee, Simulated) are adapters over today's classes from 0.2, and give facets from 0.3; Triggering is an adapter over today's trigger code from 0.3; Classification, the Timing scan and Cropped export are still a manifest and an empty `plugin.py` (stage P4) |
| The contract suites | `tests/contracts/` | What every plugin of a kind must do (design 9.1), each run against the built-ins: camera plugins (`test_camera_plugins.py`, P2), trigger sources (`test_trigger_sources.py`), feature plugins (`test_feature_plugins.py`), frame stages (`test_frame_stages.py`), record gates (`test_record_gates.py`), review sources (`test_review_sources.py`), the file services (`test_file_services.py`) and the fake host itself (`test_fake_host.py`) (P3 row 6) |
| A run, as the plugins see it | `council_core/plugin_runs.py` | The run in progress (`ActiveRun`), the two file services (`create_file`, `write_new`) and a plugin's data folder (stage P3 row 1); the host drives the hooks |
| Whose a preset key is | `council_core/plugin_keys.py` | `owner`, `namespaced`, the split of a preset by owner and the merge that keeps a missing plugin's keys (stage P3 row 4) |
| The core's points | `council_core/capture.py`, `council_core/camera_record.py` | The run-file suffix registry (`register_run_files`, `run_markers`), the frame-stage point (`Recorder.frame_stage`), the end record's plugin blocks (`Recorder.end_blocks`); block cleaning and size checks (`clean_block`, `plugins_block`, `json_size`: as the record writes it, P3 review) and the camera record's `plugins` block (stage P3 row 1). Core code: today's Typhon runs it too, and none of it does anything there |
| The core's capture set-up for a plain frame camera | `council_core/plugin_capture.py` | `CoreCapture`: a frame camera whose camera plugin gives it no capture set-up gets the core's — the trigger rows, the core's Downsample row, snapshot runs — while its plugin is still handed its own Device (`plugin_device`) (P3 review; design 2.5, 3.6). Reached only through the host |
| The core's Downsample | `council_core/cameras.py`, `council_core/camera_settings.py` | `cameras.frame_stage`, the one frame stage, which a recorder with no stage of its own saves through; `camera_settings.downsample_setting`, the Downsample row of every frame camera's "Capture" group (stage P3 row 3). Today's Typhon runs both, and they do what today's code did |

## The rules that already hold

- **A plugin imports only `typhon_plugin_api`**, plus the standard library
  and its own files. The built-in plugins obey this too, and a test checks
  every import in `typhon_plugins/`. A built-in adapter reaches the code
  that stays in `council_core` through `typhon_plugin_api.builtin`, whose
  names are looked up at every use and never kept, so an adapter always
  reaches the code as its module has it. A plugin's panel module (never
  its entry module) may also import PySide6's `QtCore`, `QtGui` and
  `QtWidgets` (design 3.11; from P3 row 2, the triggering panel).
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
`uses`, `network`, `budget_ms` and, from 0.2, `capabilities`: what a camera
plugin's cameras can have (design 3.4), read before any of its code runs.
Only a camera plugin gives it; a name Typhon does not know is kept as
written and ignored. The reader is strict:

- an unknown field, a duplicate key, NaN or Infinity, a file over 64 KB,
  text that is not UTF-8, or anything but one JSON object is refused, with
  the field and the reason;
- `typhon.` ids are kept for built-in plugins. `record_gate` and
  `frame_stage` are for built-in plugins only, and `review_source` is for
  camera plugins only. `overlay` is reserved and refused until it is built;
- a third-party plugin's run files are named `_<short>_<what>.<ext>`. No
  plugin may claim a file the core writes (`_frame_`, `_frames.csv`,
  `_camera.json`, `_run_end.json`, `_rig.json`, `_rig_end.json`), nor a
  name that holds one of those endings anywhere: the core splits a file
  name at the first ending it holds, so `<run>_ab_frame_000001.png` would
  read as a frame of a run named `<run>_ab`. A third-party name may not
  hold a built-in plugin's run file either (`plugin_manifest.
  BUILTIN_FILES`): `_capture_triggers.csv`, the triggering plugin's, and
  from stage P3 `_events.raw`, which the Prophesee plugin's manifest now
  declares (before P3 it was among the core's endings, and no manifest
  could). No two plugins, built-in ones included, claim one run file
  (the registry refuses the second);
- `"presets"` (stage P3 row 4) is a contribution point open to third
  parties: values of the plugin's own in each camera's presets;
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
| `cameras()` | Every slot as a `CameraHandle`. Its capabilities are the slot's: what the camera plugin that opened it confirmed (0.2), else what today's `Device` says of itself (`frames` or `events`, `restartable`, `area_live`, `grab_failures`, `raw_recording`). From P3 its `facet(name)` gives the camera's facet, asked of the camera's plugin when called (below, "The capture set-up, the facets and Downsample") |
| `active_camera()` | the active slot's name |
| `capture_folder(camera=None)` | The slot's folder: the plugin's own slot for a camera-scoped feature, otherwise the active slot |
| `current_frame()`, `crop_box()` | `None` until the window wires them (stage P4) |
| `on(event, fn)` | For one of `EVENTS`. `fn` gets one read-only mapping. Returns a function that stops listening |
| `settings()`, `save_settings(values)` | See the previous section |
| `state(camera=None)` | the plugin's dictionary on that slot (session only, never saved) |
| `temp_dir()` | A folder of the plugin's own, which the host removes at `close()` |
| `create_file(folder, name, binary=False)` | From P3 row 1. (a) A streamed run file: made exclusively in a folder of the run in progress, named `<run><ending>` with an ending the manifest declares; text (UTF-8, `newline=""`, LINE-BUFFERED since the P3 review: each row reaches the file as its line ends, so a crash leaves every row written so far) or binary (buffered: a plugin flushes it where a crash must not cut a record). Refused (`PluginFileError`) with no run in progress, in another folder, under another name, or when the file is there. Typhon closes what the plugin leaves open at the run's end — or, when the recording ended by itself before Stop, at Stop (P3 row 2: the triggers CSV has a row for every trigger until Stop) |
| `write_new(folder, name, data)` | From P3 row 1. (b) A whole file, `data` bytes: a temporary name of its own beside it (process, thread, a count), flushed, then a rename that refuses an existing name; `<stem>_2<ext>`, `_3` ... on a rerun. Beside a run (its frames CSV in `folder`, past runs included; `name` is `<run><declared ending>`), or in the plugin's `data_dir()` (any plain file name but code). Never over anything, never a delete; a temporary file that did not become the file is removed |
| `data_dir()` | From P3 row 1. `<project>/plugin_data/<id>/` (design 4.1), made on first ask; holds no code (`.py` and the like are refused: the project's policy check reads every `.py` under a project) |
| `capture_setup(camera=None)` | From P3 row 2. A copy of the camera's capture set-up (a `CaptureSetup`) as it is now — the plugin's camera, or the active one — or None (an event camera, no camera). Read in place: the camera's set-up is the storage today's code reads (design 2.5); a change goes through the camera's settings, its `capture.*` keys, never through the copy |

`read_data` (a plugin's own checked files) comes at stage P5.

**A plugin switched off is off** (design 3.10; P3 review). Once its plugin
is faulted, or its instance let go (switched off in the project, another
project opened, the window closing), a context's `create_file`,
`write_new` and `data_dir` refuse (`PluginFileError`), `save_settings`
refuses (`PluginListError`) and `on` listens for nothing. Before, a thread
the plugin had started went on making run files and saving settings after
its plugin was switched off; nothing was ever written over.

**`has()` names a hook only once Typhon calls it:** `hook.activate`,
`hook.deactivate` (the host) and `hook.on_frame` (the host's live feeds).
`hook.check_start` joined at P3 row 1, when Start began to ask it, with
`hook.on_run_started`, `hook.on_run_finished`, `camera.record_fields`,
the context's three new calls and the types `RunPlan`, `RunInfo` and
`RunCamera`; at P3 row 4 `hook.preset_values` and
`hook.apply_preset_values`; at P3 rows 2 and 3 `camera.facet`, the types
`CaptureSetup`, `CaptureOptions`, `SoftwareTrigger`, `HardwareTrigger`,
`Binning`, `TriggerSource`, `TriggerEvent` and `Action`, `frame.to_sensor`
and `frame.saved`, `context.capture_setup`, and the triggering plugin's
`hook.trigger_slot` and `hook.capture_now` (Start, Stop and F9 call them);
at P3 row 6 `hook.frame_stage` (Start asks a built-in plugin for its
run's frame stage). At the P3 review `frame.saved` LEFT the list —
nothing fills `FrameView.saved` until the window feeds live frames (P4),
so it is always None — and `frame.saved_to_sensor` joined it.
`hook.status`, `hook.panel`, `hook.toolbar` and `hook.settings_rows` join
when the window asks them (P4): the triggering plugin has all four, and
nothing calls them yet. status and check_start answered True in 0.1
before the P1 review, although nothing called them.

## Camera plugins (0.2, stage P2 rows 1 and 2)

**What a camera plugin is** (`CameraPlugin`, design 3.3): `backend` (its
cameras' `CameraInfo.backend`; a plugin from someone else's carries a dot,
so it can never claim a built-in's preset keys), `available()` (ready?,
what to install), `discover()`, `open(info)` (today's `Device`),
`capabilities(device)`, `facet(device, name)` (from P3: "The capture
set-up, the facets and Downsample", below),
`categories()` (`CategoryHints`), `record_fields(device)` (plain JSON;
from P3 row 1 asked at Start for its entry in the camera record's
`plugins` block) and `review_source()` (`ReviewSource` or None). Its set-up
guide and checks join with the wizard's camera pages (P2 row 4). `has()`
names the hooks the host calls: `camera.available`, `camera.discover`,
`camera.open`, `camera.capabilities`, `camera.categories`,
`camera.review_source`, and, from rows 3 and 4, `camera.setup_guide`,
`camera.setup_checks` and `camera.setup_scan` (below).

**The built-in camera plugins are adapters** over code that does not move
(design 7.2): `typhon.basler` over `BaslerBackend`, `BaslerDevice` and
`BaslerSettings`; `typhon.prophesee` over `EvkBackend`, `EvkDevice`,
`identify`, `EvkSettings` and `event_playback`; `typhon.simulated` over
`SyntheticBackend`, `SyntheticDevice` and `SyntheticSettings`. An SDK
plugin's `sdk` attribute takes a stand-in for pypylon or `metavision_hal`
(the contract suite's fakes); None is the real SDK, imported on first use.

**Capabilities, in two steps** (design 3.4). The manifest says what the
plugin's cameras can have; the opened camera confirms its own:

| Plugin | Manifest | Confirmed on the opened camera |
|---|---|---|
| Basler | `frames`, `restartable`, `grab_failures`, `software_trigger`, `hardware_trigger`, `binning` | the camera's own flags; `software_trigger` when an exact Software run can be armed (`trigger_problem` says nothing); `hardware_trigger` when its trigger sources list an input (`capture_options().hardware`); `binning` when `can_bin(2)` (pylon's emulator accepts binning and does nothing, so the run record still says what was done) |
| Prophesee | `events`, `area_live`, `raw_recording` | the camera's own flags (never `restartable`); Trigger In, sync and the event stream come later |
| Simulated | `frames`, `events`, `restartable`, `area_live`, `software_trigger` | the camera's own flags, and `software_trigger` for the frame camera, which has the capture set-up a trigger is fired through |

The host keeps only what the manifest declares and Typhon knows
(`camera_capabilities.known`), so a plugin can never claim more. The one
rule for an open camera's own flags is `camera_capabilities.of_device`.
The core still acts on those flags (frame_camera restarts a stream when
`restartable` says so, records raw by `records_raw`, sets the area live by
`area_live`), so a camera of a plugin from someone else whose flags
disagree with what its plugin confirmed is closed again and refused at
open, naming each capability (P2 review); the built-ins are held to the
same by the contract suite. A slot forgets what was confirmed when
Disconnect empties it (before the review it held the closed device until
the next Connect).
**A camera slot answers** what its camera can do
(`CameraSlot.capabilities`): what its plugin confirmed when it opened it
(`CameraSlot.confirm`, done by `host.open_camera(info, camera=...)`), else
the rule. `frame_camera`'s four "is it an event camera?" checks, the
simulated camera's settings and the Council's Capture tab now ask for
`events`; what they say is unchanged.

**The registry** (`PluginHost`): `camera_plugins()` (this project's, in its
list's order: Basler, Prophesee, Simulated, as `cameras.backends()` has
them); `backends()` (each camera plugin as today's `cameras.Backend`,
`PluginBackend`, so `cameras.discover(known=)` and
`cameras.open_camera(known=)` run on top of the registry with the same
order and the same notes — a test compares the two scans);
`discover_cameras()`; `open_camera(info, camera=)`;
`camera_capabilities(pid, device)`; `category_tables()`;
`review_sources()`. `cameras.backends()` and `discover()` themselves are
unchanged: the Council's Capture tab keeps them.

**Errors in a camera job** (`camera_call`). A camera's own refusal, a
`CameraError` (`NeedsStop`, `GrabFailed`, `SettingError`), reaches the
caller as it is: the camera said no, which is never the plugin's fault.
Anything else is the plugin's: logged in `plugin_errors.log` and said, and
the caller gets a `CameraError` naming the plugin, so the code that handles
a camera's refusal handles it too. A plugin from someone else is then
switched off for the session; a built-in camera plugin is not (design 3.10:
never switched off without asking). Camera calls are not watched by the
watchdog: they wait on an SDK and a camera for seconds (pypylon's import
alone took 0.6-2.0 s, design 3.3). The same holds when a built-in camera
plugin fails to **load or activate** (P2 review): it is logged and said,
shows as faulted with "tried again at the next scan", and a scan or the
set-up wizard's families try it again (`host.retry_cameras()`). Before the
review the generic guard switched it off for the session, and its cameras
dropped out of the next scan with no note. A hang in `activate` is still
the watchdog's, and switches it off.

**What a scan says** (P2 review). The project's scan
(`host.discover_cameras()`) gives today's notes and, beyond them, one note
for each camera plugin the project runs that cannot run now (faulted,
failed to load, incompatible, waiting for approval), unless it is a family
the set-up did not tick, and one for each ticked family no camera plugin
the project runs opens now ("Prophesee EVK4: ticked in Camera setup…, but
no camera plugin this project runs can open its cameras now ..."): such a
scan said "Every backend was searched.". With every camera plugin running
nothing is added, so the scan stays today's, note for note (a test).

**What a plugin may list and be called** (P2 review). A camera plugin's
backend name is the family `camera_setup.json` saves: `camera_setup.
FAMILY_RE` (lower case, at most 64 characters, so never the "|" preset
keys are split at), with a dot for a plugin from someone else's
(`plugin_host.backend_problem`); such a plugin naming another is switched
off with the reason, where "Acme.Thermal" used to be offered as a check box
whose Finish raised ValueError. `discover()` keeps only `CameraInfo`s of the
plugin's own backend, with a key and the kind `frame` or `event`; each one
left out is a note of the scan.

**Category hints** (design 2.4 item 9). Each built-in camera plugin names
the settings groups that are its own, and `typhon_plugin_api.builtin.
hints_for` gives it those groups' rows of today's tables (one table while
the branch lives). `camera_categories.merge` merges every plugin's hints:
a shared tab two plugins name holds both plugins' groups, each plugin's
own order kept; a tooltip or a group's most-used keys come from the first
plugin that gives them. A group stays in the first shared tab that names
it, and `plan` puts a shared tab's groups where `tab_of` does (P2 review:
a third party's `("Thermal", ("Thermal", "Status"))` planned Status twice). The three built-ins' hints merge into today's
tables exactly, and `plan(..., tables=)` plans the same tabs for every
built-in camera from the merged hints and from each plugin's own alone. A
group no plugin names still gets a tab of its own. The settings tabs still
plan from today's tables until the slot's `api` hands them the merged ones
(P4).

**The Prophesee raw view is a review source** (design 2.4 item 13, 3.7):
name `raw`, label `Raw`, suffix `_events.raw`, opened with
`event_playback.open_raw` exactly as the slider's default opener calls it,
and not while the file is being recorded. `ReviewSource` also finds a
run's file and the newest one, as the slider does. The slider keeps its
own code until P4.

**N simulated cameras** (row 2; multi-camera design 2.10):
`COUNCIL_SIM_CAMERAS=frame,frame,event` makes the Simulated plugin list that
many: the first of each kind keeps today's key and serial (`sim-frame`
`SIM-1`, `sim-event` `SIM-2`) in every list, and the next ones are
`sim-frame-2`, ... with a serial made from the key (`SIM-FRAME-2`). At most
8. Unset or blank, today's two exactly. A value Typhon cannot read is said
in the scan's notes, never guessed at. Until the P2 review the serials
numbered the list in order, so `frame,frame,event` made today's event
camera `SIM-3`: its presets read as saved on another unit, and
`camera_rig.json` gave it a second name and its old name to the frame camera
listed second.

**Test fakes for a rig** (row 2): `tests/fake_rig.py` — fake EVK4 units with
serials of their own behind one fake HAL (several at once, in
`frame_camera`'s scan too), Trigger In, the sync facility and the
external-trigger decoder shaped like OpenEB 5.2's bindings (checked where
they are importable), a trigger line, a sync clock, and replay rigs (recorded
events, or real `.raw` files where the Metavision SDK is).

## The set-up, and the rig's names (0.2, stage P2 rows 3 and 4)

**A camera plugin's set-up** (`CameraSetup`, design 2.3, 2.4 item 15). A
camera plugin whose manifest contributes `setup` (camera plugins only) is
a camera FAMILY: a check box in the set-up wizard of a "Typhon (plugins)"
project. It gives `setup_label` (the family's short name, "Basler"),
`setup_guide(python)` (a `Guide` of `Step`s, its install commands written
for the interpreter the app runs under), `setup_checks()` (a `Readiness` of
`Check`s, each True, False or None; it takes seconds and runs off the UI
thread; it must not raise for a missing SDK) and, optionally,
`setup_scan(held)` with `setup_scan_label` (a scan page: every camera of the
family and whether each will run, shaped like `basler_scan.ScanReport`;
`held` maps the key of every camera this window holds open to its name in
the rig, and the scan must not open those again). The types are the wizard
shell's, which stays core (design 2.1), re-exported. The Basler and
Prophesee plugins reach today's guides and checks in
`council_core/camera_setup.py`, and the Basler one the scan page in
`council_core/basler_scan.py`, through `typhon_plugin_api.builtin`: the
code does not move (design 7.2). The Basler scan says which camera of the
rig it found in use ("in use as cam2"). The simulated cameras have no
set-up and are always listed. The contract suite checks every camera
plugin's set-up.

**The families a project ticked** are kept in its `camera_setup.json`,
still `FORMAT` 1: a `cameras` list, in the order offered, beside the one
`camera` today's reader knows (the first family it knows; none when it
knows none, and that reader then asks again). A file written by today's
single-choice wizard is read as a list of one (`load_families`), so a
project set up today opens unchanged and is not asked again; today's
Typhon reads a families file's first family it knows (`load_choice`).

**The set-up read as the project's plugin list.** `host.setup_choice()`
reads those families (None: never set up). `camera_plugins()` is then the
camera plugins the project runs, less those with a set-up whose family was
not ticked; never set up, it is every one of them, as today's Typhon scans
everything until it is set up. So for the same set-up the scan through the
plugins is today's scan, row for row and note for note (a test compares
the two). `camera_families()` gives the wizard every family the project
runs, ticked or not, as `camera_setup.Family`; each part is guarded, so a
plugin's guide or check that fails comes back as a guide or a failed check
saying so (logged; a built-in camera plugin stays on). A family switched
off in `typhon_plugins.json` is not offered. `family_plugins()` names the
families offered from the manifests alone, loading nothing; with none (every
camera plugin with a set-up switched off, so only the simulated cameras
are left) a project never set up is not asked at every start.

**The wizard** (`CameraFamiliesWizard`): a check box per family, then the
install steps of every family ticked (with several, each under its own
heading with a copy button of its own: a pip command and an EVK4's
administrator commands do not go into one prompt), then each family's
checks and, for a family with a scan page, its scan, each off the UI
thread. A check or scan from an earlier visit is never shown on a later
one. With one family ticked, the pages read exactly as today's wizard.
`run_families_wizard` saves what was ticked, and keeps after it a family
ticked before that has no check box now (its plugin switched off): the
wizard did not ask about it, so the answer is not lost. Today's Typhon
keeps its single-choice `CameraSetupWizard`, unchanged.

**`camera_rig.json`** (`council_core/camera_rig_file.py`; multi-camera
design 2.2, 2.5): the name each camera goes by in the rig, cam1, cam2, ...
by default, renameable, safe in a folder name; a camera is known by its
backend and serial (an EVK4's model is read only once it is open), or by
its key with no serial, never across kinds (a frame camera is not an event
camera whatever its serial; a known kind is never rewritten, P2 review),
and keeps its name from one session to the next.
And rig presets: which preset goes on which camera; `plan()` says what
putting one on the open cameras would do (applying one is M1 row 5, stage
P6). The file follows `camera_presets.json`'s rules: JSON only, an OS lock
on a sidecar, written whole under a temporary name of its own, flushed and
renamed; a damaged or newer file is refused and left as it is; reading
never writes. Connect's note on a damaged one says how to mend it (by hand,
or move it aside and the next Connect starts a new one), and
`RigFile.set_aside()` moves a damaged one aside byte for byte, to
`camera_rig.damaged-<time>.json`, for the rig controls' window to offer
when the user asks (P6; multi-camera design 4.12, as `camera_presets`'
repair does); a newer Typhon's file is not damage and is never moved. It is additive: what it does not know (a later block such as
the triggering plugin's rig-wide set-up, design 3.6, or a field of an
entry) is kept when it rewrites.

**In a "Typhon (plugins)" project** `frame_camera` scans through the host,
opens through the plugin of the camera's family (which confirms its
capabilities onto the slot), and names the camera from `camera_rig.json`
(`host.name_camera`: the slot takes the camera's name; a damaged or
unwritable file is said in Connect's line and left alone, and the camera
still connects). `host.rename_camera` renames the camera open in a slot,
in the file (its rig presets follow) and in the slot together, and refuses
an unsafe or taken name, or a slot with no camera, before anything
changes; the window that asks for a name comes with the rig controls
(stage P6). With a second camera slot the scan marks a camera the
window holds "· in use as cam2", and Connect refuses it in another slot
before anything opens; a one-camera window's rows read as they always
have. The wizard's scan is told about every open slot. Today's Typhon
keeps its scan, its wizard and its first-run question, and never makes the
host.

## A run, as the plugins see it (stage P3 row 1)

**The order** (design 3.6), the core's, with the host filling it in. A
"Typhon (plugins)" project only; today's Typhon makes no host, so its
Start and Stop are as before.

1. **`check_start(plan)`**, for every plugin whose manifest contributes
   `run_hooks`, before anything is changed, stopped or reserved
   (`frame_camera.start` asks it right after its own refusals and before
   the boxes beside Start are written). A `RunPlan`: `base` (the run's
   name to be; `_2` ... when that name is taken) and each camera of it
   as a `RunCamera` — name, kind, `info`, capabilities, folder, area,
   Downsample's factor and method, and the capture set-up as plain values
   (`{}` for an event camera). A reason refuses Start ("Start refused —
   Hooks: ..."); a check that fails refuses it and switches the plugin
   off — and so does one that has not answered in `RUN_HOOK_SECONDS` (P3
   review: "did not answer its start check within 2 s"). Every plugin's
   declared run files are registered then, before the name is reserved.
2. The run's name reserved (its frames CSV made exclusively), then
   `begin_run`: the run in progress for its camera.
3. The camera record's `plugins` block (`record_plugins`), built with the
   record before the recording begins and written after, as the record
   always was: each plugin this project runs that is on, loaded or
   switched off after a fault — `name`, `version`, `short`, `kind`, and
   `faulted` with why (cut to 2,000 characters, P3 review) — and, for the
   camera plugin that opened the camera, its `record_fields(device)` as
   `fields` (cleaned; a plugin from someone else whose fields cannot go in
   is switched off, a built-in camera plugin only said, design 3.10). The
   call and the cleaning of what it gives run inside the camera guard, on
   a thread of their own, bounded by `RUN_HOOK_SECONDS` (P3 review: a
   block that raised when read refused Start, a SystemExit escaped
   start(), a slow one held Start). An entry the record cannot hold is
   said in Start's summary, never dropped in silence.
4. The recording starts, then **`on_run_started(run)`** (a `RunInfo`:
   `run`, `folders` by camera, `started`) for every run-hook plugin — a
   camera-scoped one once for each camera of the run — then
   `run_started` is told, then the trigger source starts. Best effort: a
   fault is said in Start's summary, the plugin is switched off, the
   capture carries on. BOUNDED (P3 review): each hook and each listener on
   a thread of its own, waited for `RUN_HOOK_SECONDS` at most; one that
   has not returned is switched off as a hang and Start goes on (a hook
   that slept 6 s held Start, and the trigger source, 6 s).
5. Stop: once the trigger source has stopped and its last frames are
   waited for — and, for a frame camera, once its stream has stopped (P3
   review) — `run_stopped` (no new streamed file from then on;
   "run_stopped" told, once, each listener bounded as in 4) — before the
   recording ends. So a listener that hangs holds Stop no longer than its
   bound and adds no frame to the run (one that slept 6 s let the run
   record 6 s more). An event camera's stream runs on after a run, so its
   plugins are told before the recording ends, as before. Disconnect in
   the middle of a run tells it first, bounded.
6. When the run's last frame is on disk and its end record is written
   (at Stop, or earlier when a failed write ended the recording),
   **`on_run_finished(run)`** for every plugin asked on_run_started and
   still on, with `ended` and `counts` — each camera's counts from its
   end record (`frames_saved`, `frames_not_saved`, `grab_failures`,
   `grab_attempts`, `first_index`, `last_index`, `recording_failed`,
   `stream_ended`). BOUNDED: one that has not returned in
   `RUN_HOOK_SECONDS` (2 s) is switched off as a hang and the record is
   written without it. It runs on a thread of its own while the thread
   writing the end record waits for it (P3 review: these notes said it ran
   on the writing thread). The mapping it returns is its block in
   `<run>_run_end.json`'s `plugins`, CLEANED INSIDE THAT SAME GUARDED,
   BOUNDED CALL (P3 review): reading a block can run the plugin's own code
   (a mapping's `items()`, a number's `__float__`), and before, a
   SystemExit there lost the end record and left the frames CSV open, an
   error lost every plugin's block with nobody named, and an oversized
   one held the end record for tens of seconds; now any of these is that
   plugin's fault — the end record is written, the other blocks kept, the
   culprit named and switched off. The faults of plugins during the run
   are its `plugin_faults` (`id: why`). Then the files the plugins
   streamed into the run and left open are closed.

A run whose recording ended by itself before its plugins were told it
started is not started: they are told nothing about it. A Start that fails
after the name was reserved abandons the run: nothing is told.

**A run of several cameras** (ready for M1's rig runs, P6; P3 review):
each camera's recorder is bound to its own camera (`end_blocks(active,
camera)`) and files its own counts. A camera-scoped plugin is asked
`on_run_finished` with its camera's end record, its block in that one; a
rig plugin is asked ONCE, with the end record of the run's last camera to
end, when every camera's counts are in — its block is in that end record.
(Before, every camera's counts were filed under the run's first camera,
and each plugin was asked once per camera's recorder.) Every run of P3
has one camera, for which nothing changes; M1's rig end record
(`<run>_rig_end.json`) is where a rig plugin's block goes at P6.

**Records, one rule** (design 3.9). Keys format 1 already has stay where
they are: the camera record's `capture` and `downsample`, the end record's
counts. A plugin's data is under `plugins`, by id, and nowhere else — a
block holding `"capture"` stays inside `plugins[id]`. Every block is
CLEANED (`camera_record.clean_block`: NaN and the infinities become null,
tuples lists, any number type a plain number, numpy's included — np.bool_
too, which numpy 2 names "bool" and the name test before the P3 review
refused) and SIZE-CHECKED: at most 32 deep, 256 KB a block and 1 MB for
all of a record's blocks, so the record stays under its 4 MB read limit.
THE SIZE IS THE BYTES THE RECORD WRITES (P3 review): indented, as
`write_once` writes a record, at the block's own depth in it
(`camera_record.json_size`, exact; a test compares it with json.dumps).
Measured compactly before, four blocks of [x, y] points inside the limits
made a 4.6 MB record and one 30-deep block a 6.2 MB one, and `read()` gave
None. The walk stops at the first byte past the limit, so an oversized
block is refused at once (a million points took 10.4 s before, a tree of
shared lists 19.5 s), and it reads dicts, lists, text and numbers with the
built-in types' own methods, so cleaning runs as little of the plugin's
code as it can. A block that is not JSON (a set, bytes, an object, a key
that is not text, an array of more than one number) or too big is
refused: its plugin is switched off and the end record says why. A
plugins' part that still cannot be written leaves the end record written
without it, saying so; and since the P3 review a capture block that is not
JSON (a NaN from a run's books) is written made plain, so the end record
is always written. `<run>_run_end.json` stays format 1: `plugins` and
`plugin_faults` are new keys of version 1, written only when there is
something in them, so a run of a "Typhon (plugins)" project with no
run-hook plugin and no fault has today's end record; its camera record
has the `plugins` block.

**The run-file suffix registry** (`capture.register_run_files`,
`capture.run_markers`): today's `RUN_MARKERS` (unchanged, the raw file and
the triggers CSV among them) and every ending a plugin's manifest declares
— every plugin found, on or off, approved or not — mark a run's name as
taken, so a run whose only file left is a plugin's keeps its name (`run`
taken: the next Start is `run_2`). The host registers them when it reads
the manifests if the capture code is loaded (the host loads no camera code
itself), and always before a run's name is reserved; `close()` forgets
them. Since the P3 review a registered ending's rerun forms
(`<run><stem>_2<ext>`, what `write_new` names a file written again) mark
their run too — a run whose only file left was such a one was free again —
and discovery (and the fake host's `load`) refuses an ending that holds
another plugin's or is held in one (`ending_clash`): with `_ab_x.csv`
taken, `<run>_zz_ab_x.csv` would read as a file of a run `<run>_zz`. The
manifest already refused that of the core's and the built-ins' endings;
now it holds between plugins too.

**The two file services and the data folder** are in the context table
above (`create_file`, `write_new`, `data_dir`), with `PluginFileError`
for what they refuse.

**The frame stage** (design 3.8): `capture.Recorder.frame_stage`, the one
step that changes a picture before it is saved (`stage(image, meta) ->
image`, on the writer threads), None for today's Downsample;
`host.frame_stage(pid, stage, session)` guards a built-in plugin's on the
recording path (below). Row 3 makes Downsample the core's frame stage.
From row 6 (part C) Start asks every built-in plugin this project runs
whose manifest contributes `frame_stage` for its run's stage
(`frame_stage(camera)`: a stage, or None; `host.frame_stages`,
`has("hook.frame_stage")`), before anything stops; each one given runs
AFTER the core's Downsample, on the picture Downsample made, guarded
(`StagedAfterDownsample`), so the records' Downsample is always what was
done. One that cannot be had (its hook raises, or gives something not
callable) switches its plugin off and refuses that Start, as the trigger
slot's gate does. No built-in plugin gives one yet; plugins from someone
else cannot (the manifest refuses `frame_stage`).

## Presets and records by namespace (stage P3 row 4)

**Whose a key is** (`plugin_keys.owner`): `"<id>/<key>"` is plugin
`<id>`'s; the built-ins keep today's names — `capture.*` the triggering
plugin's, `save.downsample` the core's; every other key is the camera's
(a camera's own keys never hold "/": GenICam names, an EVK4's
`erc.rate`).

**Applying a preset** in a "Typhon (plugins)" project splits it before
`camera_settings.apply` (`host.split_preset`): the camera gets its own
keys and a running built-in's under today's names (its code reads and
writes them in place through the camera's provider while the code stays,
design 2.5); a running plugin whose manifest contributes `presets` is
handed its own values, without its prefix, once the set is applied
(`apply_preset_values(camera, values)`, best effort); and the keys of
every plugin not running — not in the project's list, not on this PC,
switched off after a fault, or running without `presets` — are never
handed to the camera (which reported them refused) and are said: "Kept
for plugins not running here, not applied: acme.thermal: 2 settings." —
and, for a plugin that runs but takes no preset values (P3 review: the
Timing scan was said as not running), "Kept for plugins running here that
take no preset values, not applied: ...". A preset plugin's values are
copied plain inside its guard (P3 review: their `items()` ran outside it).
The preset file is not touched.

**Saving a preset** adds each running `presets` plugin's own values
(`preset_values(camera)`) as `<id>/<key>`, plain values only (true or
false, text, a 64-bit whole number, a finite number — what a preset
holds); the rest is said as not saved. From the preset of that name it
replaces, the keys of every plugin not running, and of a running one that
gave nothing, are kept exactly as they were (`plugin_keys.merged`); with
the triggering plugin off, its `capture.*` keys too, whatever the camera's
Capture group says now. The preset in use (what a run's record compares
the camera with at Start) holds the camera's own keys only.

**Records** keep plugin data under their `plugins` block, by id (above).
`typhon_plugins.json` already kept another plugin's settings on a save,
a plugin not on this PC included (a test pins it).

## The capture set-up, the facets and Downsample (stage P3 row 3)

**The types are the API's** (design 2.5, 3.11): `CaptureSetup` (a run's
trigger mode, source, frames per trigger, timer, edge, delay and debounce,
and Downsample's factor), `CaptureOptions` (what the camera offers it: its
hardware sources, its edges, delay and debounce ranges, whether
Downsample can change while it streams and how it is made there), the
three modes and Typhon's own sources (`SOFTWARE`, `TIMER`). While the code
stays where it is they are today's classes in `council_core/triggered.py`,
re-exported, so the camera's `device.capture` stays one object and a
camera plugin implements its facets against the API alone. The object is
divided when the files move, at P7 (design 2.5).

**The facets** (design 3.4, 3.11), today's Device methods regrouped:

| Facet (capability) | Its calls | Today's Device methods |
|---|---|---|
| `SoftwareTrigger` (`software_trigger`) | `problem(setup)`, `arm(setup)`, `ready(timeout_s)`, `fire()`, `disarm(armed)` | `trigger_problem`, `arm_trigger`, `trigger_ready`, `fire_trigger`, `disarm_trigger` |
| `HardwareTrigger` (`hardware_trigger`) | `inputs()`, `problem(setup)`, `arm(setup)`, `ready(timeout_s)`, `disarm(armed)`, `count_edges(armed, seen)`, `count_from_now(armed)`, `count_to_now(armed)` | `capture_options`, the four above, `count_edges`, `count_from_now`, `count_to_now` |
| `Binning` (`binning`) | `can_bin(factor)`, `apply(factor)`, `check(rebin)`, `info()` | `can_bin`, `apply_downsample`, `check_downsample`, `downsample_info` |

The Basler plugin gives all three for every Basler: whether this camera
can do each is its confirmed capabilities, and the facet's own check says
why not, in today's words. The simulated frame camera gives a software
trigger, an event camera none. The built-ins' facets are adapters over the
device's own methods (`typhon_plugin_api.builtin.device_facet`). A
feature plugin reaches them through a `CameraHandle`: `facet(name)` asks
the camera's plugin when it is called (`host.camera_facet`), and gives
None for a camera no plugin of the project opened, a name with no facet,
or a plugin that failed to give one (said and logged; a built-in camera
plugin stays on).

**Downsample is the core's one frame stage** (design 2.1, 2.4 item 6,
3.8). `cameras.frame_stage(image, meta)` — Typhon's averaging of the
blocks a frame's meta asks for (`reduce.prepared`), else the picture as it
came — is what a `Recorder` with no stage of its own (`frame_stage` None)
saves through. `capture.py` imports neither `reduce` nor `triggered`: the
frame stage is the camera layer's, the record gate is handed in with the
recorder, and the triggers CSV is a run marker by name (a test holds its
name to `triggered.TRIGGERS_SUFFIX`). The Downsample row of every frame
camera's "Capture" group is the core's (`camera_settings.
downsample_setting`): today's providers build their Capture group with it,
in its place among today's rows (`triggered.capture_settings`), so both
Typhons describe it alike, and `triggered.trigger_settings` are the
trigger rows alone, the triggering plugin's. The key, its choices and its
words stay in `triggered.py` until P7. The camera half is the `Binning`
facet: a "Typhon (plugins)" Start checks the camera's binning through it.
Choosing Downsample still writes `save.downsample` through the camera's
provider, in place, until the notebook's Capture group holds the core's
row alone (P4).

**A frame camera with no capture set-up of its own gets the core's** (P3
review; design 2.5 "the core adds the Downsample row to every frame
camera", 3.6 "a third-party frame camera with no trigger at all still
captures snapshot runs"). A camera plugin written against the API alone
has a Device whose `capture` is None (the base Device's): before the
review its camera got no Downsample row, no trigger rows and no snapshot
runs — `capture.*` and `save.downsample` were "no such setting" — and
Capture now called it an event camera. Now, when a "Typhon (plugins)"
project opens such a frame camera, the host stands the core's capture
set-up in front of it (`council_core.plugin_capture.CoreCapture`): a
`CaptureSetup` of the core's own, the trigger rows and the core's
Downsample row after the camera's own settings (`triggered.
capture_settings`, read and written in place, so presets, the settings
window and the records carry them as for today's cameras), Downsample by
Typhon's averaging, and every other attribute and call the camera's own
Device's. The camera has no trigger facet, so an exact run is refused by
the triggering plugin's start check, with why, and a snapshot from
Software or the Timer runs. Its plugin is always handed its own Device
back (`plugin_device`: facet, record_fields), never the core's stand-in.
An author therefore provides no `device.capture`, and nothing of
`triggered` or `camera_settings` needs exporting for it.

**A FrameView's coordinates** (design 3.8, 3.11, 3.12): `area`,
`binning`, `decimation`, `mirror`, `downsample` and `downsample_method`,
filled from the frame (its `aoi`, its Downsample) and, for the camera's
own binning, decimation and mirroring, FROM THE CAMERA'S SETTINGS (P3
review: they were never filled in, so a mirrored picture's to_sensor gave
a wrong place instead of refusing): a live feed reads GenICam's
`BinningHorizontal`/`Vertical`, `DecimationHorizontal`/`Vertical` and
`ReverseX`/`Y` once per stream (`host.camera_geometry`; the stream must be
stopped to change them), a camera without such a setting does not do it,
and one whose setting cannot be read leaves the term None — unknown. With
the camera's binning as the Downsample, the binning is the factor.
`to_sensor(x, y)` turns a pixel of the VIEW'S OWN picture — `image`, the
full-size frame a plugin is handed — into sensor pixels (P3 review: it
converted the saved picture's, so with Downsample 2x by Typhon a live
plugin's own last pixel (639, 479) went to (1278.5, 958.5), off a 640 x
480 sensor); `saved_to_sensor(x, y)` turns a pixel of the SAVED picture
(the PNG, the live view, a box drawn on either) into sensor pixels —
Typhon's factor first, then the area's origin, then the binning, with
`reduce.sensor_position` on each axis. Both refuse a mirrored or
decimated picture, or one whose terms are unknown, rather than guess.
`FrameView.saved` is not filled yet (always None): the window feeds live
frames with the file each is saved as at P4, and `has("frame.saved")` is
False until then. A test puts
a spot on a frame averaged 2x and 3x with an area offset and finds it at
its sensor pixel within 0.01 px (design 9.4).

## Triggering (stage P3 row 2)

**An adapter over today's code** (design 7.2). `typhon.trigger` drives
`council_core/triggered.py`'s `TriggerRun` and calls frame_camera's
trigger functions (`_arm`, `_disarm`, `_count_from_ready`,
`_let_frames_land`, `_end_triggers`, `_capture_record`, `_trigger_timing`,
`_trigger_start_words`, `_capture_now_idle`), reached through
`typhon_plugin_api.builtin`, with a camera made of its facets
(`FacetCamera`: the names that code calls on a camera, each sent to the
facet the run's source needs) — never the Device. So "Typhon (plugins)"
does what today's Typhon does, and only the calls change. A probe of
eleven runs (exact and snapshot, frames per trigger, Timer, Downsample
by Typhon and by binning, a Basler's line, burst and counter, refusals)
gives the same files, CSV columns and records in both Typhons, and those
of today's Typhon are those of typhon-plugins before this stage
(`docs/typhon_plugins_branch.md`, the runs).

**The core owns the order; the plugin fills its slot** (design 3.6). In a
"Typhon (plugins)" project frame_camera asks the host for the plugin's
part of the run (`host.trigger_slot(camera)`: the plugin's
`trigger_slot(camera)`, a `RunTriggers` kept in its per-camera state,
wrapped as a `TriggerSlot`) and calls it where today's Start and Stop call
their trigger code:

| Start | Stop |
|---|---|
| check_start, before anything stops → the run's name reserved → `trigger_slot` → `arm` (an exact run) → `record` (the camera record's `capture` block) → `gate` (the run's books, guarded by P1's GuardedGate; the triggers CSV made through `create_file`) → the recording → `ready` (an input line's edge count) → on_run_started → `start` (the source) → `words` (Start's summary) | `halt` (the source stops, the last frames are waited for, an input line is closed and its frames let land) → the stream stops (a frame camera; P3 review: before, it stopped after run_stopped) → run_stopped → `close` (the books closed, the edges counted) → the recording ends → on_run_finished → `disarm` |

Disconnect calls `abort`, and a Start that fails after the run's name was
reserved calls `undo`. How each step's fault ends (`TriggerSlot`): before
the recording, `arm` and `gate` refuse the Start with what they raised, in
today's words, and the core undoes what was armed; `record` and `words`
are best effort (the core writes today's block itself). Once recording,
`ready` and `start` are the recording path: a fault ends that recording at
once, its reason naming the plugin, and switches it off. Stop's steps,
`abort`, `undo` and Capture now are SERVED OUT: called even after a
best-effort fault of the plugin's elsewhere, so the books close and the
camera is put back; a fault there is logged and said with Stop's result.
None of them is watched by the watchdog: they wait on a camera.

**What a run needs, per mode and source** (design 3.6), in `check_start`:
a snapshot from Software or the Timer needs frames only; a snapshot from a
camera line is refused; an exact run from Software or the Timer needs the
camera's software trigger, from a line its hardware trigger. A camera with
a trigger facet is asked in its own words (the facet the run needs, else
its other: both are today's `trigger_problem`), so the refusal reads as
today's after "Start refused — Triggering: "; a camera with none is held
to the rule (`problem`, which a panel greys modes with). A triggered
set-up in a project whose triggering plugin does not run (switched off,
or faulted) refuses Start before anything stops ("Start refused — an
exact, Software run needs the Triggering plugin ...": the P3 review gave it
its "Start refused —" and its "an"), and Capture now says so. Capture now
never calls a frame camera an event camera (P3 review).

**Typhon's own sources** (`TyphonSource`, a `TriggerSource`): Software and
the Timer, `needs(mode)` per design 3.6. The run's books keep the source:
TriggerRun's worker keeps the Timer's schedule and fires the presses, as
today, so a tick that comes while the camera is busy is counted missed as
today. `start(fire)` starts that worker and tells `fire` each trigger it
numbers (`TriggerRun.on_trigger`, None in today's Typhon); `stop()` halts
it within its bound, and a stopped source stays stopped (a source belongs
to one run). Design 9.1's "start and stop can be repeated" is read that
way (settled at the P3 review): repeating either call is harmless — a
second start while it runs starts nothing more, a second stop returns at
once, a start after stop starts nothing — and a run makes new sources.

**Trigger-source plugins are not wired yet** (P3 review). Design 2.2 names
a source that is a small plugin of its own (a trigger box). Nothing picks
one for a run yet: the triggering plugin's sources are Typhon's own, and
the books' triggers come from its worker, so a plugin's `fire()` would
cause none. Its manifest kind, "source", is therefore REFUSED with that
reason (`plugin_manifest.NOT_WIRED_KINDS`) — before, such a plugin loaded,
became active and was never used, and setting `capture.source` to it was
refused. It is wired with the rig's sources at stage P6 (one source firing
several cameras, design 3.6's fan-out and P6 row 1): the triggering plugin
lists source plugins among its sources, and a source plugin's `fire(event)`
presses the run's books as Capture now does. The trigger-source contract
suite already takes any source through a factory — Typhon's two, and a
reference source written against the API alone — so a source plugin is
held to it then.

**Capture now (F9)**: frame_camera's `capture_now` in a "Typhon (plugins)"
project asks the host (`host.capture_now`), which asks the run's slot
during a run (served out) and the plugin's `capture_now(camera)`
otherwise; its words are today's. **The status segment**, `status()`, is
the run's triggers while one runs, cut to 80 characters at a " · ", else
"". **Its rows**, `settings_rows()`, are the trigger rows of the camera's
set-up read in place (`ctx.capture_setup`, the hardware facet's
`inputs()`), and `write_setting(key, value)` writes one as the Basic tab's
boxes do (frame_camera.set_camera_setting: refused while capturing, the
views told). **Its toolbar** is one `Action`, "Capture now (F9)" on F9;
**its panel** (`panel.py`) holds the rows, Capture now and the status.
Typhon asks none of these four before P4, and the window shows today's
trigger boxes and Capture now button until then.

**Its records and files.** The camera record's and the end record's
`capture` blocks are format 1's, made by today's code; the camera record's
`plugins` block names the plugin; its `on_run_finished` gives no block.
The triggers CSV keeps today's name and is made exclusively through the
file service. A recording that ends by itself before Stop (a failed
write) leaves the plugins' streamed files open until Stop, because the
books write a row for every trigger until Stop, as today's do ("not saved:
recording stopped"); P3 row 1 closed them with the end record.

## The fake host and the contract suites (stage P3 row 6)

**The fake host** (`typhon_plugin_api.fake_host.FakeHost`, design 9.2):
simulated cameras, a temporary "Typhon (plugins)" project and plugin home
under the folder it is given, and no window; Typhon's own Start and Stop
drive the plugin, so it meets the core's order, records and faults as in
the app. `adopt(manifest, plugin)`, `connect(kind, label=)`, `live_view()`
(the live view, as the window's tick starts it), `set(key, value)`,
`feed(pid)` (live frames to a plugin's on_frame, which the window wires at
P4), `start`, `stop`, `capture(frames, during)` (a `Run`: its frames CSV,
triggers CSV, end record, camera record and sidecars as today's readers
read them, and `problems()`: the data-safety sums, frames saved = CSV rows
= the end record's count — and, since the P3 review, every picture of one
size and mode, read from the PNGs' headers — in one call), `faults`,
`said` and `close()`. One at a time in a process (a second is refused).

**For a plugin's author** (P3 row 6, part C; design 3.11). `load(folder)`
takes the plugin's folder as its author has it: the manifest read by the
third-party rules (no record gate or frame stage; `_<short>_<what>` run
files), refused with why where Typhon would refuse it (another plugin's
short name or run file — every plugin Typhon found, built-ins this project
switches off among them, and an ending held in another's: P3 review —
compiled code in the folder, a wrong API number, a kind not wired yet),
and its code imported from the folder's own files as a package of its
own (`typhon_fakehost.<id>`, relative imports working) with no bytecode
written into the folder — a `__pycache__` there would get the folder
refused (design 4.5). Typhon itself still loads plugins from someone else
only from P5. Only the simulated cameras are scanned unless
`real_cameras=True` (the temporary project switches the other built-in
camera plugins off: no SDK imported, no real camera touched), and
`TYPHON_PLUGIN_HOME`, `COUNCIL_VAULT_ROOT` and `COUNCIL_SIM_CAMERAS` point
into its folder until `close()`, which also forgets the modules `load`
imported. With a Council checkout on `PYTHONPATH`, a plugin's own tests
can use it today; `python -m typhon_plugin_api.check <folder>`, which runs
the kind's contract suite against a folder, is P5's author's kit.

**The contract suites** (design 9.1), in `tests/contracts/`, one per kind
and point, each run against every built-in plugin it applies to:

| Suite | Held to it | What it checks |
|---|---|---|
| `test_camera_plugins.py` (P2; facets P3b; faults P3c) | Simulated, Prophesee (fake EVK4), Basler (fake, and pylon's emulator in the `pylon` env) | discover, open, read, the area, restarting, settings, capabilities and their facets, hints, record fields, review source, set-up; a camera whose read raises or exits mid-run costs no frame the run says it saved, the end record says why; a scan that fails is said while the others scan |
| `test_trigger_sources.py` (P3b; any source from the P3 review) | Software, Timer, and a reference source written against the API alone (`Metronome`; a source plugin adds its case beside it) | a TriggerSource; start tells fire each TriggerEvent; stop within 1.5 s, in a burst too, nothing after it; start and stop repeated harmlessly, a start after stop starting nothing |
| `test_feature_plugins.py` (P3b; P3c; P3 review) | Triggering, Classification, Timing scan, Cropped export | the manifest and its hooks; the core's order; refusals; a failed Start undone; snapshot on a camera with no trigger facet and no capture set-up of its own (the core's, P3 review), Downsample on it; eight faults; hangs in check_start, on_run_started and a run_stopped listener, bounded, and a block that raises, exits or hangs when read costing only its plugin (P3 review); and from P3c its blocks plain and under the limit, its preset keys round-tripping under today's names, its settings saves under the lock, its panel built offscreen and fitting the smallest window, F9 claimed once, its status within 80 characters, its rows read in place |
| `test_frame_stages.py` (P3c; P3 review) | the core's Downsample; every built-in plugin with a stage (none yet) | a picture of the dtype given; as it came with nothing asked; block means; a Bayer frame refused; the input unchanged; the same on eight threads; well within half the watchdog's limit on a full boA5320 frame (P3 review); through Typhon's Start and Stop a plugin's stage after Downsample on what is saved, one that cannot be had refusing Start before anything stops, and seven faults (raising, SystemExit, writing to its picture or its meta, no picture, and a picture of another size or dtype mid-run: P3 review) ending the recording with the plugin named; a slow stage costing nothing, a hung one switched off by the watchdog with the run whole (P3 review: it "cost nothing", unseen) |
| `test_record_gates.py` (P3c; P3 review) | Triggering's books, exact and snapshot from Software and the Timer | tags only the CSV's trigger columns, every tag told saved once, the summary the end record's capture block, a tag under 1 ms, a frame it cannot change; faults (raising, SystemExit, writing to pixels or meta, junk tags and a core meta key: P3 review, a fault hearing a save) ending the recording with the frame marked and the camera put back; a summary that exits or gives NaN still leaving the end record (P3 review); slow and hung gates costing nothing |
| `test_review_sources.py` (P3c) | Prophesee's raw view (a stand-in SDK; a real .raw where OpenEB is importable) | a ReviewSource its manifest declares and a run marker; a run's file and the newest; windows the slider polls, in any order; a missing file said; no SDK said at once; nothing written beside the file or left behind |
| `test_file_services.py` (P3c; P3 review) | a plugin from someone else loaded from its folder; Triggering's triggers CSV; every built-in's run files | streamed: once, in the run's folder, only while it lasts, text (reaching the file line by line: P3 review) or binary, closed at the end; whole: numbered, never over anything, beside past runs, in the data folder (no code), several at once; names that are not plain refused; no delete or move on the context; run files run markers and their plugin's own; a plugin switched off has its services closed (P3 review) |
| `test_fake_host.py` (P3c; P3 review) | the fake host | `load` of an author's folder (and what it refuses: a switched-off built-in's short name and an ending held in another's too, P3 review), simulated cameras only, everything put back, one at a time, `Run.problems` (one picture size and mode per run, P3 review) |

Cropped export's "the same files as today's link" waits for its plugin
(P4 row 5); "each UI contribution lands only in its host, the main
window's widgets unchanged" waits for the window asking them (P4).

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
   listeners are skipped. From P3: the run hooks (`on_run_started`,
   `on_run_finished` — the latter also bounded in time, a hang switched
   off and the end record written without it), `record_fields` and the
   preset hooks are best effort; their faults are also said in Start's
   summary, or in the run's end record (`plugin_faults`, every plugin
   that faulted while the run was not over). From the P3 review every one
   of these that Start, Stop or the end of a run waits on — check_start,
   on_run_started, a run_started or run_stopped listener, record_fields,
   on_run_finished — runs on a thread of its own and is waited for
   `RUN_HOOK_SECONDS` at most (`_bounded`), what it gives cleaned inside
   that same call, so a hang or a junk result costs only its plugin. A
   fault's words are cut to `FAULT_TEXT` (2,000) characters wherever they
   are kept or said — the status line, Start's and Stop's lines,
   `plugin_faults`, `faulted`, a recording's reason — and the whole text
   and its traceback go to `plugin_errors.log` (a 6 MB message made a 6 MB
   end record, over its read limit).
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
   - **Its tags are checked** (P3 review; `plugin_host.gate_tags`): None,
     or a mapping of the frames CSV's trigger columns (`GATE_COLUMNS`,
     `triggered.frame_columns()`) with text, whole or finite numbers or
     nothing. Anything else is a tag fault as above, the frame marked —
     before, a str for tags stopped the grab thread with no word, and a
     `reduce` among them changed the size of the pictures saved with
     nothing in the records. Its `summary()`, which the end record's
     capture block is made of on a writer thread, is guarded and cleaned:
     what it raises (SystemExit included) is its plugin's best-effort
     fault, the block says so, and a NaN is written as null — before,
     either left the run with no end record. The recorder itself now
     catches BaseException around the books' summary and the plugins'
     part, and closes the frames CSV whatever the end record raised.
     A gate is NOT watched by the watchdog: its books write the triggers
     CSV under the lock its `tag` takes, so a slow disk would read as a
     hang and end the run.
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
   - **A frame stage** (P3 row 1, `host.frame_stage`, `GuardedStage`)
     follows the gate's rule, with one difference: a stage that fails
     made no picture, so the frame it was making is NOT saved, and the
     recording ends there as at a failed write — the frames queued behind
     it are not saved either (they were not yet staged, and saving them
     as they came would leave half a run staged and half not). The end
     record's `recording_failed` names the plugin and says "its frame
     stage failed"; the frames before it are whole. From the P3 review a
     stage keeps ONE PICTURE PER RUN — every picture it gives has the
     size, dtype and colour mode of its first, or that is its fault (a
     stage that cropped or changed dtype mid-run left one run with
     pictures of two sizes, its records describing one) — and is
     WATCHED: each call is in the busy table, so the watchdog switches off
     one that has not returned in `WATCHDOG_SECONDS`, the recording ends
     at the next frame staged with the plugin named, and the hung frame is
     not saved when its call comes back. A stage that never returns still
     holds its run's end record (the frames CSV is published in grab
     order), so a built-in stage must return well within the watchdog's
     limit: the frame-stage suite times every stage on a full boA5320
     frame (the core's Downsample: about 0.07 s at 2x).
   - **Read-only frames on the recording path** (P3 row 6, part C). A
     frame stage is handed its picture behind a read-only buffer
     (`locked_pixels`, nothing copied) and the meta as a read-only view
     (`read_only`), and what it gives back must be a picture — a numpy
     array, uint8 or uint16, (h, w) or (h, w, 3), not empty
     (`picture_problem`); a record gate's `tag` is handed the frame the
     same way (`read_only_frame`: a small new Frame over the same
     pixels). A stage or gate that writes into a frame, or a stage that
     gives no picture, is a fault on the recording path like any other,
     so what is saved and recorded is what the camera sent. Before, a
     gate could change the pixels about to be saved, and a stage the
     frame's meta its CSV row is made from.
3. **The start check.** `host.check_start(plan)` asks only the plugins whose
   manifest lists `run_hooks`. A plugin returns a reason to refuse Start. A
   check that **fails** refuses that Start with the fault and switches the
   plugin off, so the next Start runs without it. One that has not
   answered in `RUN_HOOK_SECONDS` does the same (P3 review: it held Start
   for as long as it hung).

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
From P3 `offer(frame, saved=)` says which file the frame is saved as
(`FrameView.saved`; None: not saved) — no caller passes it before P4, so
`has("frame.saved")` is False (P3 review) — and the view carries the
frame's coordinates (`to_sensor` and `saved_to_sensor`, above), the
camera's own terms read from its settings once per stream. Typhon's window
feeds live frames from P4; the fake host does now (`FakeHost.feed`).

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
not needed for it. Looked at again at P3 (rows 2 and 3): plugins still
get frames only as a FrameView, and the one frame stage is the core's
Downsample, so the frame's own array need not be made read-only. Settled
at P3 row 6 (part C): it is not. Every way a plugin is handed a frame is
read-only instead — a FrameView (since P1), a built-in frame stage's input
and a record gate's frame (since P3c, above) — which is design 3.8's aim
(a plugin cannot change what is saved by accident) without touching the
frame's own array, which the camera code writes and the frame pool reuses
in today's Typhon too.

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
records the PC's load. P3 row 5 (part C) measured again: the window's own
Qt timers already give the process Windows' 1 ms timer, so the request
cannot add anything there, and the 1 ms switch interval is what the core
now sets while a run records ("What is wired"); a minimized window and the
bench PC are still to measure.

## What is wired, and what comes when

Today's Typhon never calls the host, so it behaves exactly as before, and a
test runs a whole capture and finds the host never made or imported. From
0.2 `frame_camera` asks its camera slot what the camera can do; without a
host the slot answers from the camera's own flags, which is what the old
checks read. From P2 rows 3 and 4 a "Typhon (plugins)" project — one whose
folder holds `typhon_plugins.json` — scans, opens and sets up its cameras
through the host's camera registry (above, "The set-up, and the rig's
names"); `frame_camera` names that file rather than importing the plugin
lists. The recording code has two additions, and neither does
anything for today's Typhon: `FrameWriter.end_recording`, which only the
host calls, and `record_to` handing a gate its run's writer
(`bind_writer`), which today's `TriggerRun` does not have. The rig reaches
the host by a name built at run time, so the host's modules are not on
today's Typhon's Settings > Python Scripts list either (the P1 review
found 6 extra files there, 35 to 41, and a slower cold list). The list
does hold `council_core.camera_capabilities` from P2 row 1 (37 files on
the typhon example), because today's Typhon uses it: `frame_camera._can()`,
`CameraSlot.capabilities` and `SyntheticSettings` ask it. A test holds the
branch's modules on that list to the rig and the capabilities, and
frame_camera's Designer link list gains no function (the plugins path's
`_current_families` is private, P2 review).

**The grab loop** (P2 review, `council_core/capture.py`, core code both
Typhons run): `CaptureSession._loop` catches `BaseException`, so a
camera plugin's `read()` raising `SystemExit` ends the loop with a note, as
any unexpected error does, where it used to end the thread without a word
(design 3.10 counts SystemExit as a plugin fault). When the loop stops by
itself (the camera gone, its recording over, a run of failed reads, an
unexpected error), the recording's `<run>_run_end.json` says why in a
`stream_ended` key; a run ended by Stop has none, so today's end records
are unchanged. This is a bug fix for today's Typhon too.

**The settings tabs' words** (P2 review): the box beside Start that is
off, its "n/a", the capture boxes' tip and the Basic note name an event
camera only when the camera's kind is `event`; for any other camera they
say what is missing.

**From P3 rows 1 and 4** a "Typhon (plugins)" project's Start, Stop,
Disconnect, Save preset and Apply preset go through the host as the two
sections above say; for today's Typhon `frame_camera` reaches no host
(`_plugin_host()` is None), so its records, its run names and its presets
are as before (a test captures in a project of each kind). The recording
code gains three points that do nothing for today's Typhon: the run-file
registry (empty without a host), `Recorder.frame_stage` (None: today's
Downsample) and `Recorder.end_blocks` (None). The two new modules,
`plugin_runs` and `plugin_keys`, are reached only through the host, so
today's Typhon's Settings > Python Scripts list does not grow: the list of
an app that imports `frame_camera` was measured the same before and after,
27 names, so the typhon example's stays 37; the P2 review's guard now
holds the two new modules off it too.

**From P3 rows 2 and 3** a "Typhon (plugins)" project's triggered runs,
Capture now and its Start's binning check go through the triggering
plugin and the camera's facets (above); today's Typhon runs today's
trigger code, unchanged, and makes no host. The recording code's frame
stage is the core's Downsample (`cameras.frame_stage`), and its Capture
group's Downsample row the core's (`camera_settings.downsample_setting`),
for both Typhons; what they save and show is what they did. frame_camera
gains private functions only (its Designer link list is as it was) and
imports nothing new, so today's Python Scripts list stays 27 names for an
app that imports frame_camera, 37 on the typhon example.

**P3 row 5, the finer timer while recording** (design 10.2; part C,
`docs/typhon_plugins_branch.md`, "Measured again for P3 row 5"). The
measurement supports the switch interval alone, so that is what is built:
in a "Typhon (plugins)" project the host holds Python's switch interval at
1 ms (`plugin_host.RECORDING_SWITCH_S`, `SwitchInterval`, process-wide,
shared by every run that records) from `run_started` to `run_stopped`, or
to the end record when a failed write ends the recording first, and on a
Start abandoned, a Disconnect or the host's close; it puts back what was
there, and leaves alone an interval already finer or one someone else set
meanwhile. With a plugin busy 20 ms a frame the grab thread then read
every frame (344-430 of 600 at Python's 5 ms), with no plugin it changed
nothing. Windows' 1 ms timer is not asked for: inside the real Qt Typhon a
1 ms wait took 1.0-1.9 ms in every run with nobody asking, because Qt
gives its timers of 20 ms or less that timer (a bare process: 15.5-15.8
ms; with a 15 ms QTimer running: 1.1-1.4 ms), and the request alone
changed nothing. Still to measure: the bench PC, and a minimized window
(Windows 11 may not honour a minimized window's timer requests): the
bench's `--window minimized`. Today's Typhon makes no host and keeps
Python's 5 ms.

**From P3 row 6 (part C)** a "Typhon (plugins)" Start also asks the
built-in plugins for a frame stage (none gives one), and the recording
path hands a frame stage and a record gate read-only frames (above). For
today's Typhon nothing changes: it makes no host, so neither is asked.
frame_camera gained no function (the frame stage is asked inside
`start`) and imports nothing new, so today's Python Scripts list is still
27 names for an app that imports frame_camera, 37 on the typhon example
(counted again on plugins/p3c).

**From the P3 review** (19 October 2026, `plugins/p3-fixed`), for today's
Typhon: frame_camera imports `council_core.reduce` and `.triggered` with
itself again — they came with the capture code until P3 row 3 took them
out of `capture.py`'s imports — so Settings > Python Scripts shows them
loaded from the start, as before: 41 of 54 at start, 43 after Connect, 46
after a run, exactly as on typhon-plugins 7ec6bc4 (they showed "not yet"
until Connect on P3); the list's names are the same 27 for an app that
imports frame_camera and 37 on the typhon example, with the same groups
and descriptions (counted with the review's own probes). Core code both
Typhons run: `Recorder.finish` catches BaseException around a run's books'
summary and the plugins' part, and a capture block that is not JSON is
written made plain; `FrameWriter._close_recorder` closes the frames CSV
whatever the end record raised; `capture.taken_runs` also counts a
registered run file's numbered rerun forms — nothing is registered in
today's Typhon, so its run names are as before. Its records are the same:
its books' summary is plain JSON, and nothing raises there. Everything
else of the review is in a "Typhon (plugins)" project only.

| Stage | Adds to the API |
|---|---|
| P2 | Done (0.2, above). Rows 1 and 2: the camera plugins over today's Backend, Device and Provider classes, capabilities declared in the manifest and confirmed by the opened camera, the registry, category hints, the raw view as a review source, `Provider.extra_stops`, N simulated cameras and the rig's test fakes. Rows 3 and 4: `camera_rig.json` (names, rig presets), several camera families in the set-up as the enabled camera plugins, the wizard's check boxes, "in use as cam2" in the scan, each camera plugin's guide, checks and (Basler) scan page in the wizard, and `camera_setup.json`'s choice read as the project's plugin list |
| P3 | Done (0.3, above). **Rows 1 and 4** (branch `plugins/p3a`): the run hooks (`on_run_started`, `on_run_finished`; `check_start` wired into Start); `RunPlan`, `RunInfo`, `RunCamera`; the run-file suffix registry, with the Prophesee manifest declaring `_events.raw`; record blocks, cleaned and size-checked, and the run records' `plugins` blocks; the two file services and `<project>/plugin_data/<id>/`; the frame-stage point, and the fault rule for it and for the run hooks; preset and record namespacing, a missing plugin's keys kept untouched. **Rows 2, 3 and 6** (branch `plugins/p3b`): the facets (`SoftwareTrigger`, `HardwareTrigger`, `Binning`) given by the Basler and Simulated plugins and reached through a camera handle; `CaptureOptions`, `CaptureSetup`, the modes and sources in the API; the triggering plugin over today's code — TriggerRun's books driven by the rig through its trigger slot, the record gate, its sources, check_start per mode and source, the `capture.*` keys read and written in place, Capture now (F9), its status segment, rows, toolbar and panel; Downsample as the core's frame stage and its Capture row the core's, `capture.py` importing neither reduce nor triggered; `FrameView`'s coordinates, `to_sensor` and `saved`; the fake host and the contract suites for trigger sources and feature plugins. **Rows 5 and 6** (branch `plugins/p3c`): Python's switch interval at 1 ms while a run records (the core's, no API; Windows' timer not asked for, Qt holds it already); a built-in plugin's frame stage after Downsample (`hook.frame_stage`); read-only frames for a frame stage and a record gate; the fake host for a plugin's author (`load`, simulated cameras only, `Run.problems`); contract suites for frame stages, record gates, review sources and the file services, and the faults the feature and camera suites lacked. Settled from P2's open list: the frame's own array stays writeable, every way a plugin is handed a frame being read-only instead; the core keeps asking the Device's own flags for restarting, raw recording and the live area while the code stays (the open-time check and the camera suite hold them equal to the slot's), until M1's per-slot functions (P6). **The P3 review** (branch `plugins/p3-fixed`): every call Start, Stop and the end of a run make into a plugin bounded (`RUN_HOOK_SECONDS`), a frame camera's run_stopped told once its stream has stopped; blocks cleaned inside their plugin's bounded guard, measured as the record writes them (`json_size`), numpy's scalars included; a record gate's tags checked (`GATE_COLUMNS`) and its summary guarded; a frame stage of one picture per run, watched; `FrameView.to_sensor` the view's own pixels, `saved_to_sensor` the saved picture's (`has("frame.saved_to_sensor")`), the camera's own terms from its settings, `has("frame.saved")` off until P4; kind "source" refused until P6, the trigger-source suite taking any source; the core's capture set-up for a frame camera with none (`council_core.plugin_capture`); a switched-off plugin's context closed; each camera of a run its own end-record counts; a fault's words cut in the records; streamed text files line-buffered; endings held in one another refused, rerun files marking their run |
| P4 | The screen: pop-out panels, toolbar and status strips, the Plugins window (base set, additions, switch-offs, settings, approval states, each plugin's over-budget and dropped-frame counts), `current_frame` and `crop_box` wired. `status()` asked by the status strip and held to its limits, 80 characters and 5 ms (design 3.5, 3.11), with `has("hook.status")`. The crash marker and "asked after a crash". A plugin's Python packages checked in the project's own interpreter (`python_envs`) when the Council lists a project's plugins; inside the app the running interpreter is the project's. The settings tabs planned from `host.category_tables()` (they plan from today's tables until then, so a third party's own shared tab is not yet shown as planned) |
| P5 | The hardened loader for third-party plugins (approval, `approvals.json`, the hash-then-compile importer, the `typhon_thirdparty.<folder>` package, `read_data`), the network field and its audit hook, the manifest's `uses` acted on (11.5), the API frozen as 1.0, and the author's guide and `python -m typhon_plugin_api.check`. An import of `typhon_plugin_api.builtin` (the built-in adapters' reach: brand classes, `CaptureSetup`, the Basler scan) refused to a plugin from someone else: it is a submodule of the one module a plugin may import, and nothing refuses it yet (P2 review) |

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

Taken at stage P2 (rows 1 and 2, 16 October 2026):

- **The manifest gains `capabilities`** (camera plugins only). Design 3.4
  says the manifest's list lets the screen offer features before anything
  is imported, but 3.11's field table has no field for it.
- **A built-in camera plugin's SDK is not in its `python`.** With pypylon
  there, a PC without pypylon would show the Basler plugin "incompatible"
  and its scan note would go; `available()` says what to install, as the
  scan always has.
- **`cameras.backends()` and `discover()` stay as they are** for the
  Council's Capture tab; the registry gives its plugins to
  `cameras.discover(known=)` as `PluginBackend`s rather than
  `backends()` being built from the registry. Building `backends()` from
  the plugins would have put the plugin host under the Capture tab and on
  today's Typhon's Python Scripts list, and changed `cameras.py`.
- **A camera's `CameraError` is the camera's answer; any other error of a
  built-in camera plugin is logged and said, and it stays on** (design
  3.10's "never switched off without asking"); a third-party camera
  plugin's is a fault. Camera calls are not watched by the watchdog.
- **The capability question goes to the slot**: the plugin-confirmed set
  when a plugin opened the camera, else the camera's own flags. The host
  keeps a plugin's answer within its manifest and what Typhon knows.
  `frame_camera` asks `events` only where a frame is shown with it, as the
  frame's meta check did ("no frame, no event count").
- **`Provider.extra_stops` is asked only about a set's keys that no
  described row has**, so a settings window's one-key writes make no
  provider for it. Design 2.1 gave it `(values, described)`; that is kept.
- **The built-in adapters' reach is a submodule**,
  `typhon_plugin_api.builtin`, looked up at every use, rather than more
  names on the API itself: they are brand code that goes at P7, and the
  1.0 API should not carry them.
- **Category hints are cut from today's tables**: each built-in plugin
  names its groups; the rows stay in `camera_categories` until P7.
- **`COUNCIL_SIM_CAMERAS`**: the first camera of each kind keeps today's
  key and serial, and every other serial comes from its key (changed in
  the P2 review: they numbered the list in order), so the default
  `frame,event` is today's two exactly; at most 8; anything else is said.
- **The fake EVK4's sync mode is its own enum**: in OpenEB 5.2's Python
  bindings `get_mode()` is declared to return the C++
  `I_CameraSynchronization::SyncMode`, and no `SyncMode` is bound anywhere
  in `metavision_hal` (read from the bindings in the `pylon` env; not
  called, there being no EVK4), so there is no real value to compare.

Taken at stage P2 (rows 3 and 4, 16-18 October 2026):

- **Today's Typhon keeps its single-choice wizard; only a "Typhon
  (plugins)" project gets the check boxes.** "What you see" after P2 is
  the same Typhon with a wizard that ticks camera families. Giving today's
  Typhon check boxes too would have changed its wizard's tests (ticking a
  second family leaves the first ticked), which the gate keeps unchanged.
  A project is a "Typhon (plugins)" one when its folder holds
  `typhon_plugins.json`.
- **A family is its camera plugin's backend name** (`basler`, `prophesee`,
  a third party's dotted one), the word today's `camera` already holds.
- **The families stay in `camera_setup.json`**, not in
  `typhon_plugins.json`'s `added` and `off`: the set-up's choice is read as
  part of the plugin list, so today's file needs no rewrite, and a family
  switched off in the plugin list is off whatever the set-up says.
- **The manifest says whether a camera plugin has a set-up** (`setup` in
  `contributes`), so the scan knows which plugins the set-up narrows
  without loading any for that.
- **The API stays 0.2.** The set-up hooks are additions within stage P2's
  version: nothing outside the branch names 0.2 (third-party plugins load
  from P5), and `has()` names each hook.
- **"In use as cam2" only once the rig has a second camera slot**, so a
  one-camera window's rows read as they always have (multi-camera design
  2.9: a project that never adds a second camera never shows rig
  controls). Until stage P6 adds cameras from the window, a second slot is
  made by code only (the tests).
- **A camera's name is given at Connect, and the slot takes it.** A
  one-camera window's slot can therefore be called cam2 when its camera is
  cam2 in the rig. Nothing on today's screen shows the name.
- **Rig presets are stored and planned, not yet applied.** Putting a rig
  preset on several open cameras needs the slot's own settings `api` (M1
  row 5, stage P6).
- **A name belongs to a camera**, so `rename_camera` needs one open in the
  slot: an empty slot has no camera for camera_rig.json to give the name
  to.
- **A family the wizard cannot offer stays ticked** (18 October 2026).
  Dropping it would have changed an answer the wizard never asked about;
  the plugin list, not the set-up, says whether it runs.
- **Nothing to tick, nothing asked** (18 October 2026). With every camera
  plugin with a set-up switched off, the first-run wizard could only be
  cancelled, and would have come back at every start.
- **`NAME_RE` ends in `\Z`**: "$" also matches before a final newline,
  so a camera name ending in a newline passed (found while testing
  `camera_rig.json`).

Taken in the P2 review (18 October 2026):

- **A ticked family switched off is said, not asked again.** The review
  offered two ways: ask the set-up again when no ticked family is offered,
  or say it in the scan. Asking would bring the wizard back at every start
  for a project whose user switched that plugin off on purpose and has no
  other camera, which is what "nothing to tick, nothing asked" avoids; so
  the scan says it, and `_needs_setup` is unchanged.
- **A built-in camera plugin that fails to load is tried again at the next
  scan**, not at every call: one scan makes one attempt and gives one note,
  and the next scan (or the set-up wizard) tries again. Between scans its
  state reads "faulted ... tried again at the next scan".
- **A third-party backend name that the set-up cannot save switches the
  plugin off**, with the reason in its state (it can never work here),
  rather than being dropped in silence as one without a dot was before.
- **The flags check at open is for plugins from someone else only.** The
  built-ins' confirmed set is computed from the same flags and the
  contract suite checks they agree, so a refusal there could only come
  from a bug, and would take today's cameras away in a "Typhon (plugins)"
  project.
- **`stream_ended` is a new key of the end record, written only when the
  stream ended by itself**, not `recording_failed` (which is "the write
  that ended the recording") and not a key on every record, so a run ended
  by Stop writes exactly today's record.
- **Today's Python Scripts list keeps `camera_capabilities`**: today's
  Typhon uses it, so hiding it by a name built at run time (as the rig does
  for the host, which today's Typhon never loads) would make the list
  wrong. The record says 37 files.
- **A damaged camera_rig.json is moved aside only when asked**
  (`set_aside`); Connect, which runs without asking, only says how to mend
  it. Nothing moves user data on its own.

Taken at stage P3 rows 1 and 4 (18 October 2026, branch `plugins/p3a`).
The design's own answers, used as it recommends them:

- **A record gate that raises** leaves the frame it was handling saved and
  marked by the gate's guard, and the recording ends with it (design 3.6
  against 3.10; `FrameWriter.end_recording` has done this since P1).
- **A best-effort hook's fault** — `record_fields`, `on_run_started`,
  `on_run_finished`, the preset hooks, a listener — is reported (Start's
  summary, or the end record's `plugin_faults`), that plugin is switched
  off, and the run carries on; only `check_start` refuses a Start (3.10).

Decided here, where the design left it open:

- **The API stays 0.2** for rows 1 and 4: everything is an addition, named
  by `has()`, and nothing outside the branch names 0.2. A move to 0.3, if
  wanted, comes with the merge of P3's other rows, because it changes
  every manifest's `api` and is best made once.
- **`on_run_finished` is called with the end record**, once the run's last
  frame is on disk — not on the Stop thread, as design 3.11's table has it
  — so its counts are the end record's and its block goes in that record,
  which is written once. It runs on a thread of its own while the thread
  that writes the end record waits for it (corrected at the P3 review:
  this said "on the thread that writes it"), bounded (`RUN_HOOK_SECONDS`,
  2 s, the watchdog's limit): the end record is never held up by a
  plugin. With a disk that keeps up this is during Stop; with a slow one
  it is after Stop returns.
- **"run_stopped" is told as Stop ends the run, before its recording
  ends**, so always before `on_run_finished`, and once, also for a run
  whose recording ended by itself. A run whose recording ended before its
  plugins were told it started is not started at all.
- **A frame stage that fails saves nothing more**: unlike a record gate's
  fault, whose frame is whole and is saved marked, a failed stage made no
  picture, so that frame and those queued behind it are not saved, as at
  a failed write, and the end record names the plugin.
- **A block is cleaned where it can be and refused where it cannot.**
  NaN and the infinities become null, tuples lists, number types plain
  numbers; a set, bytes, an object, a key that is not text, more than 32
  deep, over 256 KB, or past 1 MB for all of a record's blocks is refused,
  and that is a best-effort fault of its plugin (switched off, said).
- **The end record's new keys are `plugins` and `plugin_faults`**, written
  only when not empty, so a "Typhon (plugins)" run with no run-hook plugin
  and no fault has today's end record. The camera record's `plugins` block
  is always there for such a run: it says what made it.
- **The camera record lists each plugin that is on, loaded or faulted**
  (name, version, short name, kind; why it is faulted; the camera
  plugin's `fields`), not only the loaded ones: a plugin that is on may
  load during the run and write a sidecar. No sha256 yet: plugins from
  someone else load only from P5.
- **`RunInfo.counts` is by camera** (`counts["cam1"]["frames_saved"]`),
  ready for the rig's runs of M1. (It was not, until the P3 review: every
  camera's counts were filed under the run's first camera, and each
  plugin was asked once per camera's recorder; "A run of several cameras",
  above.)
- **The plan's name is the reservation's**: in a "Typhon (plugins)"
  project Start reserves from the same time stamp it told the start
  check (`RunPlan.base`), so the run is that name or its `_2`.
- **`check_start` is asked before the boxes beside Start are written**,
  so a refused Start changes nothing, not only stops nothing.
- **`create_file` is open from the reservation until Stop**, under the
  exact name `<run><declared ending>`, text (UTF-8, `newline=""`) or
  binary.
- **`write_new` writes beside a run whose frames CSV is in the folder**
  (or the run in progress), or in the plugin's data folder; the rerun
  number goes before the extension (`<run>_tracks_2.csv`, the velocity
  design's names); the data folder takes no code.
- **The data folder is named by the plugin's id as it is**
  (`plugin_data/typhon.trigger/`).
- **The suffix registry is the capture code's, for the process**, beside
  today's `RUN_MARKERS`, which do not change. Every plugin found, on or
  off, approved or not, has its files registered (more names taken is
  never a risk). The host registers them only once the capture code is
  loaded — it loads no camera code itself — and always before a run's
  name is reserved; `close()` forgets them. No two plugins, built-in
  ones included, may claim one run file.
- **`_events.raw` is a built-in plugin's file now**, the Prophesee
  plugin's (`BUILTIN_FILES` with the triggers CSV), not one of the core's
  endings; the core still writes it while the code stays in place.
- **With the triggering plugin off, `capture.*` are kept and listed**
  (design 2.5), on apply and on save over a preset; the camera's Capture
  group still shows its rows until P4 moves them to the triggering
  plugin's pop-out.
- **Only a plugin that contributes `presets` is asked for or handed preset
  values.** A running plugin without it keeps its keys as one not running
  does. A `presets` plugin's keys in a saved preset are exactly what it
  gives now; one that gives nothing, or faults, keeps its old keys.
- **The preset in use compares the camera's own keys only**, so a preset
  holding a plugin's keys is still "in use" in the run's record.

Taken at stage P3 rows 2, 3 and 6 (18 October 2026, branch `plugins/p3b`).
The design's own answers, used as it recommends them:

- **The core owns Start's and Stop's order and the triggering plugin fills
  the slot in it** (design 3.6), driving TriggerRun's books, never
  replacing them; the record gate is the triggering plugin's, one per
  camera per run, guarded by P1's GuardedGate, and a gate that raises
  still saves its frame, marked, and ends the recording with it (3.6
  against 3.10, as at P1 and P3a).
- **A best-effort fault never stops a capture** (3.10): the run in
  progress is served out — its gate tags it whole, Capture now still fires
  it, and Stop still closes its books and puts the camera back; a fault on
  the recording path (the gate, the source that did not start) ends it
  with its reason.
- **Snapshot from Software or the Timer needs frames only; a mode or
  source the camera cannot do is refused at Start with why, and the
  facet's own check still runs there** (3.6).
- **Downsample stays the core's** (question 2): its key, its row and the
  one frame stage are Typhon's; binning is a Basler facet (2.1, 2.5).

Decided here, where the design left it open:

- **The API is 0.3**, the number of stage P3 (as 0.1 was P1's and 0.2
  P2's), taken once P3's two parts are together: every addition is named
  by `has()`, and every built-in manifest names 0.3.
- **The facets add to design 3.11's signatures**: `problem(setup)` on both
  trigger facets (the camera's own check, which 3.6 says still runs at
  Start), `ready(timeout_s)` on the hardware one (the edge count starts
  once the camera is ready, frame_camera._count_from_ready), and
  `check(rebin)` and `info()` on Binning (the core's Start check and its
  records ask them).
- **A Basler gives all three facets, whatever it can do**; its confirmed
  capabilities say what it can, and the facet's own check says why not in
  today's words. Returning no facet when a capability is not confirmed
  would have replaced the camera's words with the rule's.
- **`TriggerSource.needs(mode)`** takes the mode: design 3.6 makes what a
  run needs depend on its mode, which the protocol's `needs()` could not
  say.
- **Typhon's sources are the run's books' own worker.** TriggerRun keeps
  the Timer's schedule and its missed ticks, as today; a source with a
  schedule of its own would have counted them differently. The books tell
  a source's `fire` each trigger through `TriggerRun.on_trigger`, a hook
  added for it (None in today's Typhon).
- **The trigger slot has a rule per step** (`TriggerSlot`): arm and gate
  refuse the Start; record and words are best effort; ready and start are
  the recording path; Stop's steps, abort, undo and Capture now are served
  out. Not watched by the watchdog (they wait on a camera).
- **on_run_finished comes before disarm** when the disk keeps up: the end
  record is written as the recording ends (P3a's decision), and the
  camera's triggers are put back after it, as today's Stop does. Design
  3.6's text lists disarm first.
- **A refusal in "Typhon (plugins)" reads "Start refused — Triggering:
  <today's words>"**: it is the triggering plugin's start check that
  refuses.
- **A triggered set-up whose triggering plugin does not run refuses Start**
  (switched off or faulted), rather than recording a Continuous run the
  user did not ask for; Capture now says the plugin is needed.
- **A recording that ended by itself leaves the plugins' streamed files
  open until Stop**, so the triggers CSV keeps a row for every trigger
  until Stop, as today's does. P3a closed them with the end record.
- **The recorder's default stage is the camera layer's**
  (`cameras.frame_stage`, `Recorder.frame_stage` None): a bare recorder
  still saves Downsample by Typhon (a test of today's Typhon makes one),
  and capture.py names no feature.
- **The Downsample key, choices and words stay in triggered.py** until
  P7; the core's row function reads them there.
- **Choosing Downsample still writes through the camera's provider** (in
  place) until P4; the Binning facet is asked at Start (the binning check)
  in a "Typhon (plugins)" project.
- **A FrameView's binning, decimation and mirroring come from the slot**,
  and no camera reports them yet; `to_sensor` refuses a mirrored or
  decimated picture rather than guess.
- **The panel may import QtCore, QtGui and QtWidgets** (design 3.11), never
  in the plugin's entry module; the branch test of the built-ins' imports
  says so.
- **The fake host is `typhon_plugin_api.fake_host`** and drives
  frame_camera's real Start and Stop, so a plugin is tested in the order it
  meets in the app; it ships with the author's kit at P5.
- **A plugin's per-camera state keeps the last run's slot** (for what is
  said after Stop) until the next run, or until Disconnect, when the
  triggering plugin lets go of it (a slot's `plugin_state` lives for the
  session).
- **From P2's open list**: plugins need not get the frame's own array
  read-only (FrameViews only; the one frame stage is the core's); the
  core keeps asking the Device's own flags for restarting, raw recording
  and the live area while the code stays, the open-time check holding them
  equal to the slot's, until the rig's slots carry the cameras (P6).
- **Row 5 is not built**: P1 row 3 did not confirm the finer timer (above,
  "What is wired"). (Superseded at P3c, below: measured again, the switch
  interval is built.)

Taken at stage P3 rows 5 and 6 (18 October 2026, branch `plugins/p3c`).
The design's own answers, used as it recommends them:

- **The core sets the finer setting while it records, once a measurement
  inside the real Qt Typhon confirms the gain** (design 10.2 item 1): the
  switch interval's gain was measured three times (P1, the P1 review, P3c)
  and is built; the timer request's was not, and is not.
- **A fault on the recording path ends the recording; a best-effort one
  never stops a capture** (3.10), now also for a built-in plugin's frame
  stage asked at Start and for a gate or stage that writes into a frame.
- **Every contract suite runs first against the built-ins, and the fake
  host drives Typhon's own Start and Stop** (9.1, 9.2).

Decided here, where the design left it open:

- **Only the switch interval, only while a run records, only in "Typhon
  (plugins)"**: process-wide and shared by every run recording at once;
  never coarser than it was; a change someone else made meanwhile is left
  alone; it goes back at Stop, at the end record when a failed write ends
  the recording first, and at an abandoned Start, a Disconnect or the
  host's close. A plugin need not ask for it (no API): the host holds it
  whether or not a live plugin is fed (none is before P4), as the design
  says "while it records", and with no plugin it changed nothing measured.
- **Windows' 1 ms timer is not asked for**: inside the real Qt Typhon Qt
  already gives the process that timer (its timers of 20 ms or less), and
  asking changed nothing; whether a minimized window keeps it is the one
  case left, for the bench PC (`--window minimized`).
- **The bench's rows keep their meaning**: they hold the plugin Typhon's
  own interval off, and a `typhon` row is the plugin Typhon as it is.
- **A built-in plugin's frame stage runs after the core's Downsample**, not
  in its place: Downsample is the core's one frame stage (question 2), and
  a stage that could skip it would make the records' Downsample untrue.
  Asked once per run at Start, before anything stops; one that cannot be
  had refuses that Start (the gate's rule), since a run without the stage
  asked for is not the run asked for. The contribution was accepted by the
  manifest since P1 and asked by nothing until now.
- **A recording-path plugin is handed read-only frames** (the frame stage's
  picture and meta, the gate's frame), and a stage must give back a
  picture: the cheapest way (a memoryview and a small Frame, nothing of the
  picture copied) to keep design 10.3's "a plugin cannot change a frame's
  pixels" for the two points that get the frame itself. The frame's own
  array stays writeable (settled from P2's list).
- **The core keeps asking the Device's own flags** for restarting, raw
  recording and the live area (settled from P2's list): no P3 row moves
  them (design 2.4 item 4 is the event-camera checks, done at P2); the
  open-time check (third parties) and the camera suite (built-ins) hold
  them equal to the slot's capabilities, so the answer is the same; and
  several of those checks ask the session's device, which is not always
  the active slot's once the rig has several, so they move with M1's
  per-slot functions (P6), each asking its own slot.
- **The fake host loads an author's folder** with the third-party rules and
  no bytecode written, but without approval (that is P5's, for Typhon):
  the author is running their own code already. Only the simulated cameras
  unless asked, and the vault pointed into its folder too.
- **`python -m typhon_plugin_api.check` stays P5's** (the author's kit): the
  suites live in `tests/contracts/` until then, run against the built-ins;
  the fake host is what an author's own tests can use now.
- **The review source's newest file follows today's slider** (file names,
  naturally sorted), which puts `<run>_events.raw` after
  `<run>_2_events.raw` for two runs started in one second: a P2 test holds
  the two equal, and the slider is today's Typhon's, which takes bug fixes
  on qt-migration. The contract asks only what both agree on.
- **The API stays 0.3**: part C's additions are named by `has()`
  (`hook.frame_stage`) or are not API at all (the switch interval, the fake
  host's `load`).

Taken at the P3 review (19 October 2026, branch `plugins/p3-fixed`). The
design's own answers, used as it recommends them:

- **A record gate that raises** leaves the frame it was handling saved and
  marked by the gate's guard, and the recording ends with it (design 3.6
  against 3.10; `FrameWriter.end_recording` does this) — and so do tags
  that are not the trigger columns with plain values, now a fault like a
  raise.
- **A best-effort hook's fault is reported, that plugin is switched off,
  and the run carries on** (3.10) — now also when the hook hangs (each is
  bounded) or what it gives fails when it is read (cleaned inside its
  guard), and for a gate's summary.
- **Start-time and end-of-run hooks get a time limit** (3.11's table:
  500 ms each, the watchdog's 2 s): `RUN_HOOK_SECONDS`, 2 s, for every one
  Start, Stop and the end of a run wait on.
- **The core adds the Downsample row to every frame camera, and a frame
  camera with no trigger still captures snapshot runs** (2.5, 3.6): a
  frame camera whose plugin gives it no capture set-up gets the core's.
- **A 2 s watchdog switches a stuck plugin off** (3.10): a frame stage is
  watched too.
- **Everything a plugin draws or stores is in sensor coordinates, and a
  position is never guessed** (3.12): `to_sensor` for the view's pixels,
  `saved_to_sensor` for the saved picture's, the camera's terms read from
  its settings, unknown ones refused.

Decided here, where the design left it open:

- **A hung start check refuses that Start**, as a failed one does, and the
  next Start runs without it: design 3.10 lets only check_start refuse, and
  a check that cannot answer gives no permission. Before the review the
  watchdog already switched it off after 2 s; now Start does not wait for
  it either.
- **The hooks are bounded one at a time**, each on a thread of its own:
  one hung plugin holds Start or Stop 2 s, several hung ones 2 s each, and
  the order of the calls stays the project list's. A hook that runs on
  after its bound is not stopped (Python cannot); nothing waits for it.
- **run_stopped is told once a frame camera's stream has stopped**, still
  before its recording ends and before on_run_finished, so the order the
  feature suite holds (halt, run_stopped, close, on_run_finished, disarm)
  is kept and a listener that hangs adds no frame; an event camera's stream
  runs on after a run, so there it is told first, as before, and so is it
  at Disconnect (bounded).
- **A record gate is not watched; a frame stage is.** A gate's `tag` takes
  the books' lock, under which the books write the triggers CSV, so a slow
  disk would read as a hang and end a run over nothing; a stage only
  computes. A stage that never returns still holds its run's end record
  (the frames CSV is published in grab order): handing that frame over
  would need the writer pool to give up on a frame in flight, which is
  today's capture code; instead a built-in stage must return well within
  the watchdog's limit, and the frame-stage suite times each one.
- **One picture per run** for a frame stage: its first picture's size,
  dtype and colour mode for the whole run. A stage may still crop for a
  whole run (P3a's own test does); it may not change mid-run.
- **Cleaning runs as little of a block's code as it can**: a dict's (and a
  dict subclass's) items, a list's, a tuple's, a str's and a number's
  value are read with the built-in types' own methods; another mapping's
  items() and an unusual number's conversion still run its code — inside
  the plugin's guard and bound. An n-dimensional numpy array is still
  refused: a block holds plain values (a sidecar holds arrays).
- **A block's size is the record's bytes**, indented at its depth, so a
  block of 30,000 [x, y] points (240 KB on one line) is refused at about
  1.2 MB as written: such data goes in a sidecar file (create_file or
  write_new), as design 3.9 says of per-frame data.
- **A fault's words are cut at 2,000 characters** in the records, the
  status line and Start's and Stop's lines, the whole text and traceback
  kept in `plugin_errors.log`.
- **A plugin switched off is off for its context**: file services and
  save_settings refuse, `on` listens for nothing; `say`, `settings`,
  `state` and the camera handles still answer (they change nothing).
- **Streamed text files are line-buffered; binary ones are buffered as
  usual**, their plugin flushing them where a crash must not cut a record:
  an unbuffered binary file may write only part of what it is given.
- **`FrameView.saved` stays in the type, None until P4**, and
  `has("frame.saved")` is False until the window fills it, rather than the
  fake host guessing which frames were taken: what a frame is saved as is
  known only once the writer has it.
- **The camera's own terms are read once per stream** (GenICam locks
  binning, decimation and ReverseX while grabbing; so does the simulated
  camera), a camera without such a setting does not do it, and one whose
  setting cannot be read leaves the term unknown.
- **Trigger-source plugins wait for stage P6**, refused in the manifest
  until then, with the rig's sources (one source firing several cameras):
  that is where the triggering plugin first needs to pick a source that is
  not its own. Design 9.1's "start and stop can be repeated" is read as
  "repeating either is harmless; a start after stop starts nothing".
- **A run of several cameras**: a camera-scoped plugin's block goes in its
  camera's end record, a rig plugin's in the end record of the run's last
  camera to end (asked once, every camera's counts in) — until M1's rig
  end record (`<run>_rig_end.json`, P6) takes it.
- **The kept-keys words name two cases**: "not running here" and "running
  here that take no preset values".
- **The API stays 0.3**: nothing outside the branch names it; what changed
  is named by `has()` (`frame.saved_to_sensor` in, `frame.saved` out until
  P4), and `to_sensor`'s meaning changed within 0.x, which allows any
  change.
