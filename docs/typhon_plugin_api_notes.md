# Typhon plugin API 0.x: working notes

These notes describe the plugin API as it stands on branch `typhon-plugins`.
The design is `docs/typhon_plugins_design.md` (sections 3, 4 and 10). The
API is numbered 0.x while the built-in plugins shape it, and any change is
still allowed. It is frozen as 1.0 at stage P5, together with the author's
guide of design 3.11, and only then are plugins from other people loaded.
Until then these notes are the record of what exists.

**Version now: 0.2** (stage P2: rows 1 and 2 on 16 October 2026, rows 3
and 4 on 16-18 October). 0.1 was stage P1 row 2 (15 October 2026); 0.2 adds the camera
plugins and, with rows 3 and 4, their set-up.

## The pieces

| Piece | Where | What it is |
|---|---|---|
| The API a plugin imports | `typhon_plugin_api/__init__.py` | `API_VERSION`, `has()`, `EVENTS`, the types (`CameraHandle`, `FrameView`, `CropBox`, `CategoryHints`, `ReviewSource`), the protocols (`PluginContext`, `FeaturePlugin`, `CameraPlugin`, `CameraSetup`), the capability names and rule, the set-up types (`Guide`, `Step`, `Check`, `Readiness`), and today's classes re-exported (below) |
| The built-in adapters' reach | `typhon_plugin_api/builtin.py` | What the built-in camera plugins reach of the code that stays in `council_core` (BaslerBackend, EvkBackend, SyntheticBackend and their devices, the raw view's `open_raw`, the settings tabs' tables). Not part of the API a plugin from someone else may use; it goes at stage P7 |
| Capabilities | `council_core/camera_capabilities.py` | The names of design 3.4 and the one rule that reads them off an open camera's own flags |
| The manifest reader | `council_core/plugin_manifest.py` | Reads and checks `plugin.json` as data and imports nothing |
| The base set and project lists | `council_core/plugin_lists.py` | `<plugin home>/base_set.json` and `<project>/typhon_plugins.json`, and the plugin home itself |
| The host | `council_core/plugin_host.py` | The built-in list, discovery, lazy loading, the guard and faults, contexts, events, the UI queue, live-frame feeds and the watchdog; from 0.2 the camera-plugin registry |
| The rig | `council_core/camera_rig.py` | `rig.plugins` is the host, made on first use. Each slot's `plugin_state` holds a plugin's per-camera state; its `capabilities` say what its camera can do; its name is its camera's name in the rig (`rename_slot`), and `holding` says which slot has a camera open |
| The rig's names and rig presets | `council_core/camera_rig_file.py` | `<project>/camera_rig.json` (P2 row 3) |
| The set-up's families | `council_core/camera_setup.py` | `camera_setup.json`'s families (`load_families`, `save_families`, still FORMAT 1) and `Family`, one family as the wizard gets it (P2 row 4) |
| The families wizard | `council_qt/widgets/camera_wizard.py` | `CameraFamiliesWizard`, `run_families_wizard`: check boxes, each family's steps, checks and scan (P2 rows 3 and 4); today's `CameraSetupWizard` unchanged |
| The built-in plugins | `typhon_plugins/<name>/` | Seven adapters. The three camera plugins (Basler, Prophesee, Simulated) are adapters over today's classes from 0.2; the four feature plugins are still a manifest and an empty `plugin.py` |
| The camera contract | `tests/contracts/test_camera_plugins.py` | What every camera plugin must do (design 9.1), run against every built-in camera plugin |

## The rules that already hold

- **A plugin imports only `typhon_plugin_api`**, plus the standard library
  and its own files. The built-in plugins obey this too, and a test checks
  every import in `typhon_plugins/`. A built-in adapter reaches the code
  that stays in `council_core` through `typhon_plugin_api.builtin`, whose
  names are looked up at every use and never kept, so an adapter always
  reaches the code as its module has it.
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
| `cameras()` | Every slot as a `CameraHandle`. Its capabilities are the slot's: what the camera plugin that opened it confirmed (0.2), else what today's `Device` says of itself (`frames` or `events`, `restartable`, `area_live`, `grab_failures`, `raw_recording`). `facet()` answers `None` until stage P3 |
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

## Camera plugins (0.2, stage P2 rows 1 and 2)

**What a camera plugin is** (`CameraPlugin`, design 3.3): `backend` (its
cameras' `CameraInfo.backend`; a plugin from someone else's carries a dot,
so it can never claim a built-in's preset keys), `available()` (ready?,
what to install), `discover()`, `open(info)` (today's `Device`),
`capabilities(device)`, `facet(device, name)` (None until P3),
`categories()` (`CategoryHints`), `record_fields(device)` (plain JSON;
wired at P3) and `review_source()` (`ReviewSource` or None). Its set-up
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

| Stage | Adds to the API |
|---|---|
| P2 | Done (0.2, above). Rows 1 and 2: the camera plugins over today's Backend, Device and Provider classes, capabilities declared in the manifest and confirmed by the opened camera, the registry, category hints, the raw view as a review source, `Provider.extra_stops`, N simulated cameras and the rig's test fakes. Rows 3 and 4: `camera_rig.json` (names, rig presets), several camera families in the set-up as the enabled camera plugins, the wizard's check boxes, "in use as cam2" in the scan, each camera plugin's guide, checks and (Basler) scan page in the wizard, and `camera_setup.json`'s choice read as the project's plugin list |
| P3 | The run hooks (`on_run_started`, `on_run_finished`; `check_start` wired into Start, and `has("hook.check_start")`). `RunPlan` and `RunInfo`. The facets (`SoftwareTrigger`, `HardwareTrigger`, `Binning`) and `CaptureOptions` and `CaptureSetup` in the API. The record gate wired through `record_gate`. The run-file suffix registry. Record blocks, and the run records' `plugins` block. The two file services, and `<project>/plugin_data/<id>/` for a plugin's own data in a project (design 4.1; nothing makes it in 0.1). `FrameView.to_sensor` and its coordinate fields, and `saved`. The finer timer while recording (row 5), decided on a quiet PC and on the bench PC. Whether plugins need the frame's own array read-only from `read()` (design 3.8), if they get frames any other way than a `FrameView`. With the registry, the Prophesee manifest declares `_events.raw` in `files` (the manifest reader refuses that core ending today), and the contract suite's allowance for it goes: a review source's suffix must then be among its manifest's files (P2 review). Whether the core asks the slot's capabilities for restarting, raw recording and the live area rather than the Device's own flags (meanwhile a third-party camera whose flags disagree is refused at open) |
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
