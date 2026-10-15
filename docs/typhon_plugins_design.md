# Typhon as a plugin system, merged with multi-camera M1: design

Date: 15 October 2026 (research and fact-checks done the same day). Status:
design only; nothing is built, and it awaits your approval, like the
velocity and multi-camera designs it builds on
(`docs/typhon_velocity_design.md`, `docs/typhon_multicamera_design.md`).
This doc lives on branch `docs/typhon-plugins`, which starts at
`qt-migration` 393671b8 ("Merge typhon/trigger-downsample-fixed2", 15
October 2026, 09:35). **Every Typhon file and line reference below is to
393671b8**, so they resolve on the branch the doc lives on.

**What you asked for (15 October 2026, your words):** "Now here is the last
sort of restructuring I am thinking of for typhon. We switch to a plugin kind
of approach for it. So for example each camera type is its own plugin. FOr
example you can load in the Basler plugin which handles the settings for
Basler cameras. Then there can be a triggering plugin to accept a signal from
a source and then trigger the cameras to capture frames. With a plugin system
we should be able to add more things over time without bloating the overall
gui with too many things everytime we make a change. Basically we are
sectioning off features so when working on them they only have to be checked
against the main gui. For example the classification stuff can be a plugin."

Your answers to the follow-up questions are quoted word for word in
section 1. In short: Barbie is left behind; plugins may later be written by
other people; plugins are app-wide (the base every new project starts with)
and per project (added over time); classification becomes a pop-out panel;
and the work merges with multi-camera stage M1 on its own branch, so the
Typhon you use today keeps working while it is built.

**Read this first.** No physical camera was used. The plugin interface was
tried as a throwaway prototype outside the repo, wrapping Typhon's real
camera, settings, capture and classifier code unchanged, on simulated
cameras, pylon's camera emulator (`PYLON_CAMEMU=2`) and the fake EVK4
(`tests/fake_evk4.py`). Every number carries a label in brackets, as one of
these kinds. A label at the head of a table or list covers everything in it.
Settings, names, defaults and file line numbers are choices or places, not
results, so they carry no label.

- **measured**: run or looked up on this laptop (HP OMEN 16, i7-14700HX,
  32 GB of RAM, Windows 11), in the `council` Python 3.11 environment or the
  `pylon` environment. "Measured, synthetic" means simulated or emulated
  cameras.
- **computed**: arithmetic on measured or quoted figures.
- **estimate**: judgement, not measured. Every effort figure is one.
- **code**: read from Typhon at 393671b8, given as `file:line`.
- **project docs**: read from this repo's own design documents.
- **vendor/project docs**: from another project's or maker's published
  documentation (napari, QGIS, Micro-Manager, Spyder, Blender, VS Code, OBS,
  pluggy, Python, Microsoft); not re-checkable offline.

**How the research was done.** Three reports fed this design: an audit of
where Typhon's code would split into a core and plugins ("seams"); a survey
of how established plugin systems work ("prior art"); and a working
prototype of the interface with contract tests and benchmarks ("interface").
Each was checked by a separate fact-check that re-ran most measurements with
its own scripts. **Where a report and its fact-check disagree, this doc uses
the fact-check.** Where neither settled a point, the doc says "not settled"
and gives both figures.

The research read `origin/qt-migration` at 67cad625 and the trigger branch
at f0e726b8 and then 9de2e2b. That branch was merged into `qt-migration`
during the research (393671b8). The Typhon files at 393671b8 are identical
to the trigger branch's tip 9de2e2b (measured, `git diff`). Only 14
non-test files changed between 67cad625 and 393671b8 (the trigger work:
`frame_camera.py`, eight `council_core` modules, three widgets, the
wireframe and the camera quick-start), and every reference into them has
been re-located here (code). Measurements taken on 67cad625 say so.

Four plain truths up front:

1. **Most of the plugin shape already exists in Typhon, unnamed.** The
   camera classes (`Device`, `Backend`) already work as a camera-plugin
   contract: the same contract tests passed for the simulated cameras, the
   fake EVK4 and two emulated Baslers (measured, synthetic). The settings
   views already take an `api` object instead of importing the camera
   module. What is missing is a registry, a fixed set of places for
   plugins to attach, and a core that stops importing its features (the
   trigger work made `capture.py` import `triggered` and `reduce` directly,
   `council_core/capture.py:43`).
2. **Plugins written by other people would run as ordinary Python with
   Typhon's full rights.** Python cannot fence code inside its own process
   (vendor docs), and the Council's own policy gate says it is "a gate
   against generated code going wrong, not a sandbox" (`gui_policy.py:24-39`).
   Trust therefore rests on you approving a plugin, pinned to the exact bytes
   of its files. Section 4.6 says plainly what an approved plugin can do.
3. **A plugin on its own thread is not isolated from the camera's grab
   thread.** A plugin holding Python's lock for 20 ms per call made 313-340
   of about 575 frames of a 150 fps camera arrive late (measured, synthetic).
   Most of that comes from Windows' 15.6 ms timer tick and can be fixed in the
   core (section 10), but heavy live work such as classifying every frame
   stays off the recording path by default.
4. **It is a large piece of work.** About 34-50 days for the plugin system
   on top of M1's 24-37 days, about 55-83 days together, plus merge upkeep
   (estimates, section 12). The first stages leave Typhon behaving exactly as
   today; the screen changes only at stage P4.

---

## Terms used here

- **Core**: the part of Typhon every project always has: the window, live view, Start and Stop, run folders and records, presets, the settings host, the slider, data-safety rules and the plugin host.
- **Plugin**: a folder of Python files plus a manifest that adds one thing to Typhon (a camera family, triggering, classification), attached only at fixed places.
- **Built-in plugin**: a plugin that ships inside the Council checkout and is trusted like core code (Basler, Prophesee, Simulated, Triggering, Classification, Timing scan, Cropped export).
- **Third-party plugin**: a plugin written by someone else and copied onto the PC by you; it runs only after you approve it.
- **Plugin host**: the core object that reads manifests, loads plugins, calls them safely and gives them their context; in this design it is the multi-camera `Rig`.
- **Manifest**: `plugin.json` in a plugin's folder: its id, version, the plugin API version it needs, its kind, what it provides and needs, and where it attaches. Typhon reads it without running any plugin code.
- **Plugin API**: the one Typhon module a plugin may import (`typhon_plugin_api`), with a version number of its own.
- **Major / minor version**: in "1.2", the 1 is major and the 2 minor. An addition raises the minor number; a removal, rename or changed call raises the major.
- **Camera plugin**: a plugin that finds and opens one family of cameras (Basler, Prophesee, Simulated, later thermal) and describes their settings.
- **Feature plugin**: a plugin that adds a tool working on cameras or recordings (triggering, classification, timing scan, cropped export, later velocity).
- **Trigger source**: something that says "capture now": Typhon's own Software and Timer sources, a camera's input line, later a serial or USB box.
- **Capability**: a named thing a camera can do (`frames`, `events`, `hardware_trigger`, `binning`), declared by its camera plugin and confirmed by the opened camera.
- **Facet**: the small object through which a feature uses a capability, for example a camera's `SoftwareTrigger` with arm, ready, fire and disarm.
- **Contribution point**: one of the fixed places a plugin may attach to: a pop-out panel, a toolbar button, a status segment, a "Plugins…" menu entry, a run hook, a record block, a run file suffix.
- **Pop-out panel**: a separate window owned by Typhon that holds one plugin's controls, opened from Settings → Plugins… or a toolbar button. Classification becomes one.
- **Toolbar strip / status strip**: two areas the new wireframe draws empty, which plugins fill with buttons and short status texts at run time.
- **Hook**: a call Typhon makes into a plugin at a fixed moment (before Start, run started, each live frame, run finished, after Stop).
- **Run lifecycle**: the fixed order of a run: check, prepare and arm, reserve the run name, start recording, start the trigger source; and on Stop the reverse.
- **Record gate**: the per-frame check on the grab thread that decides whether a frame is saved and what its trigger columns say; today `TriggerRun.tag` (`council_core/triggered.py:990`).
- **Frame stage**: a step that changes a picture before it is saved and shown; Downsample is the only one.
- **Namespace**: a prefix that keeps one plugin's keys and files apart from everyone else's, for example `typhon.trigger/mode`.
- **Sidecar file**: a file a plugin writes beside a run's files, named `<run>_<plugin>_<what>`, created only if the name is free.
- **App-wide base set**: the plugins every Typhon project on this PC starts with.
- **Project additions**: plugins one project adds on top of the base set.
- **Approved plugins folder**: the one folder on the PC where third-party plugins live after you copy them in (proposed: `~/.council/typhon/plugins/`).
- **Approval**: your decision to let a third-party plugin run, recorded with a fingerprint of every file in it.
- **sha256**: a fingerprint of a file's bytes; any change to the content gives a different value.
- **Lazy loading**: importing a plugin's code only when one of its features is first used, not at start-up.
- **Guard**: the wrapper around every call into a plugin, which catches its errors so Typhon carries on.
- **Faulted**: a plugin that raised an error and is switched off for the rest of the session.
- **Watchdog**: a check that notices a plugin call that has run too long.
- **Crash marker**: a small file written before a plugin loads or first opens a camera and removed afterwards; if it is still there at the next start, that plugin crashed Typhon.
- **Contract test**: one test suite that every plugin of a kind must pass, run against each plugin.
- **Fake host**: a stand-in for Typhon used by contract tests, so a plugin can be tested without the window.
- **Reference plugin**: the Simulated camera plugin, which every contract suite runs against first (as Micro-Manager's DemoCamera is used).
- **GIL**: Python's global lock; code that holds it stops other Python threads in the same program from running.
- **Timer resolution**: how finely Windows wakes sleeping threads; 15.6 ms by default.
- **Rig / camera slot / active camera**: from the multi-camera design: all the cameras one Typhon window runs; one camera inside it; the camera the settings tabs currently act on.
- **Compatibility view (facade)**: an old name kept working by forwarding to the new code, so existing callers and tests need no change (for example `frame_camera._LIVE`).
- **`api` object**: the object the settings tabs, settings windows and preset picker are handed, today the whole `frame_camera` module.
- **Wireframe / gspec**: the Designer's description of a window (`examples/gui/typhon.gspec`), from which the app's code is generated.
- **Port**: a named connection on a wireframe shape that code finds it by (for example `live_view`).
- **Script link**: a wireframe shape wired to call a function in a Council module (for example the Train button to `frame_classes.train`).
- **Update from example**: the Designer action that brings your own project's window up to date with an example's newer wireframe, keeping your code.
- **Policy gate**: `gui_policy`, the static check that a generated project's code does not do dangerous things (no pickle, no subprocess, no network modules).
- **Linked module**: a Council module a generated app may import (`frame_camera`, `frame_classes` and others, `gui_policy.py:59-64`).
- **Vault**: the Council's data folder (`~/.council/vault`), holding Designer projects and the shared classifier store; `COUNCIL_VAULT_ROOT` points the Council at another one.
- **Checkout / worktree**: a folder holding one branch of the repo; a git worktree is a second checkout sharing the same repository.
- **Long-lived branch**: a branch that lives for weeks or months and takes in `qt-migration`'s changes regularly; here `typhon-plugins`.
- **Typhon (plugins)**: the new app, built from a new example on the `typhon-plugins` branch, run beside today's Typhon until the switch-over.
- **Switch-over**: the day the plugin Typhon replaces today's Typhon on `qt-migration`, with your projects, presets, runs and classifier libraries moved across.
- **Barbie**: the older capture example apps (`examples/gui/barbie_capture*.gspec`); v4 and v5 share Typhon's camera module.
- **Helper process**: a separate Python program started by Typhon and spoken to in JSON lines, never with pickle (multi-camera design 2.8).
- **p99**: the 99th percentile; 99 % of the values are at or below it.

---

## 1. Summary

### What changes for you

- **A smaller main window.** It keeps what every capture needs: the camera
  list, Connect, Start and Stop, the live view, the slider, the crop box,
  presets, the settings notebook, and two new empty strips (a toolbar strip
  and a status strip) that plugins fill. The new wireframe drops 53 of
  today's 93 shapes: 32 for classification, 5 for the timing scan, 4 for
  cropped export, 1 PNG / Raw button and 11 for triggering and Downsample
  (measured from `examples/gui/typhon.gspec`; section 6).
- **Classification in its own window.** A pop-out panel, opened from a
  toolbar button or Settings → Plugins…, with everything the main window
  offers today (open, add and remove classes, mark, train, predict, classify
  all, export, import, tags) and the same shared library of classifiers.
- **Each camera family is a plugin:** Basler, Prophesee and Simulated. A
  project's set-up ticks the families it uses, and each brings its own
  settings, set-up guide, install checks and (for Basler) camera scan.
- **Triggering is a plugin.** Its panel holds Capture, From, Per trigger and
  the timer; "Capture now (F9)" is its toolbar button; its status segment
  says when the cameras are armed and waiting. Trigger sources are a plugin
  kind of their own, so a future trigger box or serial input is a small
  plugin that offers one more source.
- **Timing scan and Cropped export** become two small plugins.
- **A Plugins window** (Settings → Plugins…): the app-wide base set, this
  project's additions, each plugin's version and state (on, off, needs
  approval, switched off after an error), and the approval of a third-party
  plugin you have copied into the plugins folder.
- **Multi-camera M1 arrives in the same branch:** 2-4 cameras in one window,
  small live tiles, settings that follow the active camera, one Start and
  one Stop (multi-camera design, section 1 and stage M1).

### What stays the same

- **Run files.** Same names, the frames CSV's columns in the same order (new
  ones only at the end, `council_core/capture.py:361-367`), the camera record
  at `format_version` 1 (`council_core/camera_record.py:71`). The slider, raw
  view, timing scan, cropped export and classifiers read new runs as they
  read old ones; a test on the branch proves that today's readers open a run
  written by the plugin Typhon (section 7.5).
- **Presets.** `camera_presets.json` is unchanged, kept per camera
  (backend|model|serial, `council_core/camera_presets.py:137`), and the
  built-in camera plugins keep the backend names `basler`, `prophesee` and
  `simulated` (`council_core/cameras.py:648`, 1659, 2413), so every saved
  preset still matches.
- **Classifier libraries.** Same files, same store, same formats.
- **`app.py`.** Every project's `app.py` calls `frame_camera.attach(self)`
  and is never rewritten (`gui_emit_qt.py:1470-1488`). The module name and
  that one call stay, so no project needs a hand edit (code).
- **Today's Typhon keeps working throughout,** from its own checkout on
  `qt-migration`, until you choose to switch over (section 7).

### What it buys, and what it does not

- **What it buys.** A new feature becomes a new folder with a manifest. It
  attaches only at fixed places, so it cannot rearrange the main window, and
  it is checked by its own tests, its kind's contract suite and the main
  window's layout tests. For comparison, triggered capture and Downsample
  under today's structure changed 10 existing source files plus the
  wireframe (10,518 lines inserted across 29 files), and `start()` calls into
  them at 7 places (measured, `git diff` of the trigger branch; code
  `frame_camera.py:665-666`, 742-743, 752, 759, 764, 807, 822).
- **What it does not buy.** Some features touch each other, so "only checked
  against the main GUI" holds for most plugins but not all:
  - Downsample changes the picture size that boxes, classification and
    velocity work on (`council_core/reduce.py:1-20`);
  - triggering needs each camera's arm, ready and fire methods
    (`council_core/cameras.py:491-535`);
  - in snapshot mode, frames between triggers are shown live but not saved
    (`council_core/capture.py:1521-1537`), and the live-frame hook still
    receives them (`council_core/capture.py:1456-1458`).

  Those pairs are tested together (section 9.4).

### Your answers (15 October 2026, your words)

> "Barbie gets left behind and no longer developed. 1: Plugins could be
> written by others in the future. 2: both typhon project and app wide.
> Think app wide as the new project starts with those plugins but can add
> more over time. App wide is the basic level the user wants to function at.
> 3: plugin pop-out panel. 4: I'd like this to merge with M1 however I would
> like to have this branch or fork or whatever the correct terminology is so
> that current typhon isn't broken by this. Current Typhon needs to be usable
> while this is working"

What the design does with each:

- **Barbie:** no further Barbie work. Barbie v4 and v5, the two that share
  Typhon's camera module, are retired at the switch-over; v1-v3 and the
  image viewer never touch it and stay as they are (section 8).
- **1, plugins by others:** one versioned plugin API, a data-only manifest,
  approval by you pinned to every file's sha256, contract suites an author
  can run, and nothing ever downloaded or installed by Typhon (sections 3
  and 4).
- **2, app-wide and per project:** an app-wide base set that every new
  project starts with, plus each project's own additions (section 4.2).
  Whether a later change to the base set reaches existing projects is
  question 1.
- **3, pop-out panel:** plugin controls live in pop-out windows that Typhon
  owns; plugins never add widgets to the main window (section 3.7).
- **4, merge with M1 on a separate branch:** a long-lived branch
  `typhon-plugins` in its own checkout and its own vault; the new app is a
  separately named example, "Typhon (plugins)", run beside today's Typhon;
  `qt-migration`'s fixes are merged into the branch every week; a switch-over
  with migration when you are ready (sections 5 and 7).

### The honest limits

- **No sandbox.** An approved plugin can read and write files, use the
  network and start programs, whatever its manifest says. The policy gate
  lets through `os._exit`, `asyncio` network calls, `socketserver`,
  `xmlrpc`, `ProcessPoolExecutor`, `os.startfile` and `webbrowser`
  (measured), and a plugin handed only its own panel disabled the main
  window's Start button in a test (measured). Approval is the protection
  (section 4.6).
- **Live work competes with the camera.** A slow plugin delays frames even
  on its own thread (measured, section 10.2). Live classification of every
  640 x 480 frame took 17-49 ms per frame and made 95-205 of about 575
  frames late at 150 fps (measured, synthetic), so classification runs after
  Stop by default.
- **Plugin windows are built in code,** not drawn in the Designer
  (question 4).
- **Moving a project whose classifier, timing or crop handlers you edited is
  blocked by today's Update from example** (measured; the project was left
  half-switched). The switch-over needs a step that sets those handlers
  aside without deleting them (sections 6.4 and 7.5).
- **A hung plugin is not always stoppable:** a plugin stuck in a pure-Python
  loop kept delaying frames after the watchdog had switched it off: 346 of
  587 frames late (measured, synthetic). Only a helper process can contain
  that (section 3.10).
- **Effort:** about 55-83 days with M1, plus about half a day a week of
  merging while the branch lives (estimates, section 12).

---

## 2. Core vs plugins

### 2.1 What stays in the core

The core is what every Typhon project needs whatever its cameras and
features, plus the rules that keep recorded data safe. Sizes are lines of
code (measured with `wc -l` and Python's `ast` at 393671b8, or at 67cad625
where marked).

| Core piece | Where it is today | Size | Why it stays core |
|---|---|---|---|
| Rig and camera slots: connect, start, stop, the background job, area, settings access, the boxes beside Start, status lines | `_Live` (`frame_camera.py:175`, the one instance at 373) and `frame_camera`'s session functions | About 2,300 lines of `frame_camera`'s functions (2,276 at 67cad625, measured) | Every camera and feature hangs off it; it becomes the plugin host (section 5) |
| Recording: grab loop, writer threads, run reservation | `council_core/capture.py` (1,604 lines): `CaptureSession` (1128), `FrameWriter` (778), `Recorder` (485), `claim_run` (416) | 1,604 | The data-safety rules live here: exclusive creation, the first failed write ends the recording |
| Run records | `council_core/camera_record.py` (351) and `frame_camera`'s record code (2569-2650) | About 430 | Written once, format 1; plugins add namespaced blocks only |
| Presets | `council_core/camera_presets.py` (964), `council_qt/widgets/preset_picker.py` (247), `PresetsMixin` (`camera_settings_window.py:1581`), `frame_camera`'s preset functions | About 1,900 (377 in `frame_camera` at 67cad625, measured) | One file per project, one lock; plugins own key prefixes |
| Settings host | `settings_tabs.py` (904), `camera_settings_window.py` (2,561), `camera_categories.plan` (`camera_categories.py:140`), and the generic part of `camera_settings.py`: `Setting` (99), `Change` (178), `Provider` (492), `apply` (837) | About 4,200 | Draws any camera's settings from its description; already camera-neutral |
| Slider and live view | `capture_review.py` (1,033, less the raw view), `council_core/live_display.py` (153), `pop_out.py` (126) | About 1,100 | Every recording is reviewed the same way |
| Crop box | the `roi` port and `frame_roi.parse_roi`, which the core's area parser uses (`frame_camera.py:3112-3113`) | Small | The box is drawn on the live view and the slider; only the export moves out |
| Set-up wizard shell | the generic parts of `council_core/camera_setup.py` (`setup_path` 365, `load_choice` 369, `save_choice` 386) and the wizard's pages (`camera_wizard.py`) | About 600 | Each camera plugin brings its own guide and checks into it |
| Downsample (recommended; question 2) | `council_core/reduce.py` (165) and its calls on the writer threads and display (`capture.py:571-573`) | 165 plus the calls | It changes what is saved and the picture size every feature sees (`reduce.py:1-20`); the camera half (binning) is a camera facet |
| Plugin host | new | 200-400 lines plus tests (estimate) | Reads manifests, loads, guards and calls plugins |

**Recommendation on Downsample.** The interface research and its fact-check
both found that Downsample is half camera (Basler binning, done in the
camera) and half a change to the picture before it is saved and shown (done
by Typhon on the writer threads and the display's thread pool,
`reduce.py:17-22`). The seams research had it as a feature plugin. This doc
keeps it in the core as the one "frame stage", because a plugin that changes
saved pixels would sit inside the data-safety rules, and because every other
feature must know the factor. The camera half becomes a `Binning` facet of
the Basler plugin. Its control leaves the main window for the settings
notebook, where the "Capture" group already shows it
(`camera_categories.py:41-45`). Not settled between the reports; question 2.

### 2.2 The plugin kinds

- **Camera plugins** find, open and describe one family of cameras.
- **Trigger sources** say "capture now". A source can come from a camera
  plugin (a camera's input line), from the triggering plugin (Typhon's own
  Software and Timer sources) or from a small plugin of its own (a future
  trigger box).
- **Feature plugins** add a tool. Each says whether it works on one camera
  slot (classification, the timing scan) or on the whole rig (triggering
  several cameras, later alignment).

UI is not a kind: any plugin may contribute a pop-out panel, toolbar
button or status segment (section 3.7).

### 2.3 The built-in plugins, and the code that moves into them

| Plugin | Code that moves (today's place) | Size (measured) | What it provides |
|---|---|---|---|
| **Basler** (`typhon.basler`) | `BaslerBackend` and `BaslerDevice` (`council_core/cameras.py:637-1634`); `BaslerSettings` (`camera_settings.py:1024-2125`); the camera-trigger helpers (`Trigger` 332, `_triggers_in_the_way` 753); `council_core/basler_scan.py`; the Basler set-up guide and checks (`camera_setup.py:97`, 239); the wizard's scan page (`camera_wizard.py:206-210`); its rows of the category tables | About 996 + 1,102 + 1,265 + set-up code: about 3,900 | Capabilities `frames`, `grab_failures`, `restartable`, `software_trigger`, `hardware_trigger`, `binning`; facets for triggering and binning |
| **Prophesee** (`typhon.prophesee`) | `EvkBackend`, `EvkDevice`, `_EventSink`, `identify`, `accumulate_events` (`cameras.py:1640-2277`, 329-369); `EvkSettings` and the display settings (`camera_settings.py:2415-2769`, 2301-2331); `council_core/event_playback.py`; the raw view inside the slider (`capture_review.py:649`, 756, 774); the PNG / Raw button; the EVK set-up guide and checks (`camera_setup.py:272`); its category rows (the Filters tab) | About 620 + 470 + 468 + 190 | Capabilities `events`, `raw_recording`, `area_live` (and not `restartable`); a "review source" for the slider; later `event_stream`, `trigger_in`, `sync` (sections 3.4, 11) |
| **Simulated** (`typhon.simulated`) | `SyntheticBackend`, `SyntheticDevice` (`cameras.py:2404-2802`), `SyntheticSettings` (`camera_settings.py:2864-3003`) | About 400 + 140 | The reference plugin for every contract suite; gains N cameras through `COUNCIL_SIM_CAMERAS=frame,frame,event` (the multi-camera design's name, its 2.10) |
| **Triggering** (`typhon.trigger`) | `council_core/triggered.py` (1,227); `frame_camera`'s trigger code (462 lines on the trigger branch, measured), among it `capture_now` (946), `set_capture_mode` (1007), `set_trigger_source` (1018), `set_frames_per_trigger` (1028), `set_timer` (1038); the "camera waits for a trigger" diagnostics (67 lines); `settings_tabs.CAPTURE_BOXES` (`settings_tabs.py:112`); the description of the "Capture" group, which today `BaslerSettings` builds by importing `triggered` (`camera_settings.py:1071-1209`) | About 1,800 | Software and Timer sources; the record gate; the run hooks of section 3.8; the `<run>_capture_triggers.csv` file (`triggered.py:350`); the trigger columns of the frames CSV, kept under their present names; a panel, the Capture now (F9) button and a status segment |
| **Classification** (`typhon.classify`) | `council_qt/widgets/classifier_picker.py` (588) inside a pop-out; `frame_classes.py` (4,640) stays an unchanged library; 32 shapes and 17 script links leave the wireframe | 588 + a new panel | A pop-out panel; "Classify all" after Stop; its `sklearn` requirement, today declared by the whole app (`typhon.gspec` "requires") |
| **Timing scan** (`typhon.timing`) | `frame_timing.scan_report` (`frame_timing.py`, 289 lines, unchanged); 5 shapes | 289 | A small panel with Scan and the list of bad frames |
| **Cropped export** (`typhon.crop`) | `frame_roi.export_roi` (`frame_roi.py`, 192 lines, unchanged); 4 shapes | 192 | A small panel: output folder, Save cropped frames, status |

Two notes on the table:

- **`frame_classes`, `frame_timing` and `frame_roi` do not move or change.**
  They are stable libraries that Barbie v1-v3 also use (section 8). The
  plugins wrap them.
- **The Classification plugin needs only three things from the core:** the
  active camera's capture folder, the current frame, and a "frames were
  listed again" event (today `CaptureReviewer.on_reload`,
  `capture_review.py:364`). Those become part of the plugin context
  (section 3.5).

### 2.4 What the core stops knowing

Each line is a place where today's core names a camera or a feature, and
what it becomes. Places are code at 393671b8.

1. **The one global camera state** `_LIVE` (`frame_camera.py:373`) becomes
   the `Rig` and its slots (section 5). Each slot keeps a dictionary of
   per-plugin state, which is where the trigger work's seven fields go
   (`trigger_run`, `last_trigger_run`, `armed`, `shown_reduce`,
   `disarm_note` and others, `frame_camera.py:327-370`).
2. **The settings-window dictionary** `_HELD`
   (`camera_settings_window.py:2478`) gets keys per camera, as the
   multi-camera design asks (its 2.4).
3. **Brand lists** become one camera-plugin registry: `cameras.backends()`
   (`cameras.py:2808`), `frame_camera._backends_for` (406-418),
   `camera_setup.CHOICES` (`camera_setup.py:41`), the Basler-only hint in
   `cameras.discover` (`cameras.py:2840`) and the Prophesee case in the
   presets' legacy keys (`camera_presets.py:155`). `backends()`,
   `discover()` and `open_camera()` keep working on top of the registry,
   because the Council's own Capture tab calls them
   (`council_qt/tabs/capture.py`).
4. **Checks on "is this an event camera"** become capabilities: four in
   `frame_camera.py` (1707, 2931, 4419, 4490) and two in the Capture tab
   (`council_qt/tabs/capture.py:309`, 398).
5. **The ten trigger and Downsample methods on the base `Device`**
   (`cameras.py:491-535`) become facets that a camera plugin returns or not
   (section 3.4).
6. **`capture.py` importing `reduce` and `triggered`** (`capture.py:43`)
   becomes things registered at Start: the record gate (already handed in
   as `gate=`), the frame stage, end-record blocks, run file suffixes.
7. **The list of names that mark a run as taken**, `RUN_MARKERS`
   (`capture.py:387-388`), becomes a registry each plugin adds its suffixes
   to. Creating `<run>_frames.csv` exclusively stays the real reservation.
8. **`camera_record.build(capture=..., downsample=...)`**
   (`camera_record.py:103-112`) becomes `plugins={id: block}` at the same
   `format_version` 1 (section 3.9).
9. **The category tables** (`SHARED_TABS` 38, `TIPS` 62, `MOST_USED` 75 in
   `camera_categories.py`) take rows from each camera plugin; `plan()`
   stays core. A group the tables do not know already gets a tab of its own
   (`camera_categories.py:25-29`), so a third-party camera works without
   them; the limit is the width of the tab strip (section 3.7).
10. **`settings_tabs.CAPTURE_BOXES`** (`settings_tabs.py:112`) moves to the
    triggering plugin's panel.
11. **`attach()`'s classifier ports** (`frame_camera.py:2170`, 2417) move
    to the classification panel.
12. **The Capture now button found by its visible text**
    (`frame_camera.py:2310`) becomes a toolbar button the triggering plugin
    owns. Finding a widget by its text is the fault the code warns against
    elsewhere.
13. **The raw view inside the slider** becomes the Prophesee plugin's
    review source; the slider's live-end logic stays core.
14. **Per-tick calls in `_tick`** (`frame_camera.py:2000`) become status
    segments with a time budget.
15. **`camera_setup.json`'s single camera choice** becomes the project's
    plugin list; `FORMAT` 1 files are still read (`camera_setup.py:47`).
16. **Feature script links in the wireframe** (17 to `frame_classes`, 1 to
    `frame_timing`, 1 to `frame_roi`, 6 trigger and Downsample links to
    `frame_camera`) go; plugin UI is built at run time.
17. **The two names the settings tabs read with a default**, `START_BOXES`
    and `box_shows_camera` (`settings_tabs.py:783`, 793), become part of the
    camera slot's `api`. A slot that left them out would silently lose the
    link between the boxes beside Start and the settings tabs, because the
    tabs read them with `getattr` and a default (seams fact-check, code).

---

## 3. The plugin interface

All sketches are Python-style outlines of plugin API 1.0, not finished code.
The interface was prototyped with the real Typhon classes; its contract
tests passed in both Python environments against both research snapshots
(35-37 tests each, 1-3 skipped where pypylon or the trigger methods were
absent; measured, synthetic).

### 3.1 The manifest

Each plugin is a folder with a `plugin.json`. Typhon reads every manifest at
start-up **without importing any plugin code**, so it can list plugins, grey
out features and refuse an incompatible plugin before anything runs.

```json
{
  "schema": 1,
  "id": "typhon.trigger",  "short": "trig",
  "name": "Triggering",    "version": "1.0.0",   "api": "1.0",
  "kind": "feature",       "scope": "rig",
  "entry": "plugin:TriggerPlugin",
  "needs": ["software_trigger|hardware_trigger"],
  "python": [],
  "contributes": ["panel", "toolbar", "status", "run_hooks", "record_gate"],
  "files": ["_capture_triggers.csv"],
  "network": "none",
  "budget_ms": 5
}
```

- **Fields:** `schema` (the manifest's own format), `id` (reverse-domain;
  `typhon.` is reserved for built-ins), `short` (unique among loaded
  plugins; used in file names), `version`, `api` (the plugin API it needs),
  `kind` (`camera`, `source` or `feature`), `scope` (`camera` or `rig`),
  `entry` (`module:object`, imported on first use), `needs` (capabilities;
  `a|b` means either), `python` (packages it needs, checked in the
  project's interpreter, never installed), `contributes`, `files` (run file
  suffixes), `network` (`none`, `camera-link` or `internet`; section 4.6),
  `budget_ms` for live calls.
- **JSON, and no pip entry points.** Every other Typhon file is JSON
  (`tomllib` would also work, vendor docs). Entry points would let any
  package installed into a shared conda environment register itself without
  your approval; one folder you can see and approve is simpler (prior-art
  fact-check).

### 3.2 The plugin API and its version

- **One module, `typhon_plugin_api`,** on the Council side next to
  `frame_camera`, is the only Typhon code a plugin imports. Built-in plugins
  obey the same rule (a test checks their imports), which is the real proof
  that the API is enough for outside authors.
- **Version rule** (Spyder's, vendor docs): an addition raises the minor
  number; a removal, rename or changed call raises the major. `"api": "1.2"`
  means "1.2 or later, before 2.0". A plugin needing another major version
  is refused with a message naming both numbers, as Micro-Manager does
  (vendor docs). Deprecated names keep working for one major version.

### 3.3 Camera plugins

```python
class CameraPlugin(Protocol):
    backend: str                          # "basler"; third parties: "acme.thermal"
    def available(self) -> tuple[bool, str]: ...    # (ready?, install hint)
    def discover(self) -> list[CameraInfo]: ...
    def open(self, info: CameraInfo) -> Device: ...  # today's cameras.Device
    def capabilities(self, device: Device) -> frozenset[str]: ...
    def facet(self, device: Device, name: str) -> object | None: ...
    def categories(self) -> CategoryHints: ...      # tab rows, most used, tips
    def setup_guide(self, python: str) -> Guide: ...  # install steps + checks
    def record_fields(self, device: Device) -> dict: ...  # plain JSON, at Start
    def review_source(self) -> ReviewSource | None: ...   # Prophesee: raw view
```

- **`open()` returns today's `Device` unchanged** (`council_core/cameras.py:451`:
  `start`, `stop`, `read`, area, `settings_provider`, the flags). The same
  contract test passed for the simulated frame and event cameras, the fake
  EVK4 and two emulated Baslers (measured, synthetic).
- **Category hints per plugin,** merged by the core's `plan()`, gave the
  same tabs as today's central table for every built-in camera (measured).
  Backend names stay `basler`, `prophesee`, `simulated`; third-party names
  must carry a dot, so they cannot claim an existing preset key.
- **Scan asks only enabled camera plugins:** `BaslerBackend.available()`
  imports pypylon (`cameras.py:666`), 1.2-2.0 s (measured).

### 3.4 Capabilities and facets

| Capability | Meaning | Facet |
|---|---|---|
| `frames` / `events` | picture frames / an event stream drawn as windows | |
| `restartable` | the stream may be stopped and started on one connection (an EVK4's may not, `cameras.py:1945`) | |
| `area_live` | the area can change while streaming | |
| `grab_failures` | failed grabs are counted (M0) | |
| `raw_recording` | records its own file (`.raw`) | |
| `software_trigger` | Typhon can fire it | `SoftwareTrigger`: arm, ready, fire, disarm |
| `hardware_trigger` | waits for an input line | `HardwareTrigger`: inputs, arm, disarm, count edges |
| `binning` | the camera can average its own pixels | `Binning`: can_bin, apply |
| `camera_clock` | frames carry a usable camera time stamp | known only after the first frame: a boost CXP camera and the emulator give 0 (`cameras.py:213-218`) |
| `event_stream` | raw event arrays can be passed on | `EventStream`: subscribe; reserved in 1.0, built with velocity (section 11) |

- **Two steps.** The manifest's list lets the screen offer features before
  anything is imported; the opened camera confirms it. The facets are
  today's base-`Device` methods regrouped (`cameras.py:491-535`); a camera
  that has none returns no facet instead of ten "cannot" methods.
- **Unknown names:** a capability a camera *offers* that Typhon does not
  know is ignored; a capability a feature *needs* that Typhon does not know
  counts as missing, so the feature stays off with a reason (prior-art
  fact-check).
- **A capability can claim more than the camera does.** pylon's emulator
  accepts binning and does nothing (`reduce.py:10-13`), so the run record
  says what was actually done, not what was asked.
- **Feature plugins get a `CameraHandle`** (name, kind, capabilities,
  `facet()`), never the `Device`; in one process that is a convention only
  (measured: reachable through a private attribute).

### 3.5 Feature plugins and the plugin context

```python
class FeaturePlugin(Protocol):        # every method optional; listed in the manifest
    def activate(self, ctx: PluginContext) -> None: ...
    def deactivate(self) -> None: ...
    def check_start(self, plan: RunPlan) -> str | None: ...   # a reason refuses Start
    def on_run_started(self, run: RunInfo) -> None: ...
    def on_frame(self, frame: FrameView) -> None: ...         # own thread (3.8)
    def on_run_finished(self, run: RunInfo) -> dict | None: ...  # end-record block
    def analyse(self, folder: Path, progress: Progress) -> dict: ...  # offline
    def panel(self, parent: QWidget) -> QWidget: ...          # built on first open
    def toolbar(self) -> list[Action]: ...
    def status(self) -> str: ...                              # at most 80 characters

class PluginContext(Protocol):
    plugin_id: str
    def say(self, text: str) -> None: ...                     # the status line
    def cameras(self) -> Mapping[str, CameraHandle]: ...
    def active_camera(self) -> str | None: ...
    def capture_folder(self, camera: str | None = None) -> Path | None: ...
    def current_frame(self) -> Path | None: ...
    def on(self, event: str, fn: Callable) -> Callable[[], None]: ...
        # "frames_reloaded", "camera_changed", "run_started", "run_stopped"
    def settings(self) -> dict: ...  /  def save_settings(self, values: dict) -> None: ...
    def create_file(self, folder: Path, run: str, what: str) -> IO: ...
    def temp_dir(self) -> Path: ...                           # removed by Typhon
```

`temp_dir()` is a service because Typhon's own plugin code deletes its
temporary files (`council_core/event_playback.py:226`; `frame_classes.py:915`,
1761, 2975) and the Basler scan runs `pnputil` through `subprocess`
(`council_core/basler_scan.py:1134`); a trust check refusing those calls
would refuse Typhon's own plugins (section 4.6).

### 3.6 Trigger sources and the triggering plugin

```python
class TriggerSource(Protocol):
    name: str                        # "Software", "Timer", "Line1", "Box A"
    camera_line: bool                # True: wired to the camera; nothing to fire
    def needs(self) -> frozenset[str]: ...
    def start(self, fire: Callable[[TriggerEvent], None]) -> None: ...
    def stop(self) -> None: ...
```

- **The core owns the order;** the triggering plugin fills the slot in it.
  Start: every plugin's `check_start` (before anything stops) → reserve the
  run name (the frames CSV header is written here) → arm each camera's
  facet → start recording with the record gate → `on_run_started` → start
  the source. Stop: stop the source → wait for the last frames → end
  recording → disarm → `on_run_finished`. This is today's order in
  `start()` (`frame_camera.py:665-822`) made explicit, and the multi-camera
  design's rig order (its 2.3) fits inside it.
- **The plugin keeps `TriggerRun`'s books** (`council_core/triggered.py:427`,
  `tag` at 990): exact and snapshot modes, bursts, late frames, edge counts.
  The prototype that bypassed them saved 40/40 frames but left the trigger
  columns blank and wrote no triggers file (measured); a rig must drive
  these books, not replace them.
- **Fan-out:** one source fired two cameras through their software-trigger
  facets, 40/40 frames each at 20 Hz; with no shared ready deadline only
  29-30 of 40 triggers were fired (measured, synthetic).
- **The record gate** is filled only by the triggering plugin, one per
  camera per run; a gate that raises still saves the frame and marks it, as
  today (`capture.py:1527-1531`). Trigger settings are fixed while recording
  (`triggered.py:39-40`) and rig-wide ones live in M1's rig presets.

### 3.7 UI contribution points, including the pop-out panel

| Point | Where | Notes |
|---|---|---|
| Pop-out panel | its own window, created and titled by Typhon ("Classification — cam1") | The plugin supplies the inner widget on first open (68-200 ms for the classifier panel, offscreen, measured). The window remembers its place and hides on close; today's `PopOutWindow` deletes itself on close (`council_qt/widgets/pop_out.py:45`), so this is new |
| Toolbar button | the toolbar strip | The emitted `Toolbar` (`gui_emit_qt.py:2332`) accepted buttons added at run time (measured) |
| Status segment | the status strip | The emitted `StatusBar` (`gui_emit_qt.py:2305`) accepted labels added at run time (measured); refreshed by the core's timer within a budget |
| "Plugins…" entry | the Settings menu | `gui_settings.settings_menu` has a fixed item list (`gui_settings.py:122-146`) shared by every Designer app; it gains a small registration call |
| Shortcut | application-wide | F9 for Capture now; a second claim is refused |
| Canvas overlay | the live view | Reserved for velocity's arrows and the alignment overlay (section 11) |
| Review source | the slider | Camera plugins only (the Prophesee raw view) |

- **Plugin settings go in the plugin's pop-out,** drawn by Typhon from
  `Setting` rows where possible, not in new notebook tabs: 441 px of tabs
  already sit in a 431 px column at the smallest window
  (`camera_categories.py:39-41`), and a shared "Plugins" tab made it 437 px
  with a Basler and 491 px with an EVK4 (measured, offscreen; question 3).
- **The fixed places are a rule, not a wall:** a test plugin handed only its
  panel disabled the main window's Start button (measured). A contract test
  compares the main window's widgets before and after each plugin starts.

### 3.8 Hooks on the frame path, and offline work

- **Live frames** go to `on_frame` on the plugin's own thread through a
  two-slot queue that drops the oldest frame and counts the drop. The grab
  thread pays 5.0, 8.4 and 16.9 µs for 1, 3 and 8 plugins, the same at full
  boA5320 size, because nothing is copied (measured).
- **Frames must not be changeable by plugins.** The frame a plugin sees is
  the array the writer saves moments later (`capture.py:1450-1458`); a
  read-only view was made writeable again with one call, and the saved PNG
  changed (measured). The core therefore makes the frame's own array
  read-only when `read()` returns, and the frame pool makes it writeable
  again only when it reuses it (estimate: a flag change; not measured).
- **What a `FrameView` says:** camera name, frame number, the saved file
  name or "not saved" (snapshot mode passes frames between triggers), and
  the Downsample factor, because plugins see the full-size frame while the
  saved PNG and the live view are reduced.
- **No new frames-CSV columns in API 1.0.** The CSV header is written the
  moment the run name is reserved (`capture.py:416`), and readers take its
  columns by name (`capture.py:361-363`). A plugin's per-frame data go to
  its own sidecar CSV keyed by frame file name. The trigger columns stay
  where they are.
- **Offline work** (`analyse`) runs on a thread for short jobs (Classify all
  over 240 frames: 0.29-0.43 s, almost no effect on a running capture) and
  in a helper process for long ones, because `features()` is a per-file
  Python loop (`frame_classes.py:1432`); a helper took 4.1-4.2 s to start,
  mostly sklearn (measured).
- **In-process only** in API 1.0: the record gate, the frame stage and
  live frames. If M1 row 9 puts busy EVK4s in helper processes, the
  Prophesee plugin owns them (not prototyped).

### 3.9 Data namespaces

- **Presets:** a plugin's per-camera values ride in the camera's preset as
  `<id>/<key>`, plain values only (bool, str, 64-bit int, finite float;
  `camera_presets.py:248-253`), split off by the core before
  `camera_settings.apply`, which otherwise reports them refused (measured).
  Rig-wide values go to rig presets. Keys of a plugin that is not loaded
  are kept untouched and listed as such. Today's `capture.*` and
  `save.downsample` keys (`triggered.py:80-88`) are read as they are and
  never rewritten in place.
- **Records:** Start-time fields go in `<run>_camera.json` under
  `"plugins": {id: {...}}`, `format_version` still 1; results known after
  the run go in `<run>_run_end.json` (M1: `<run>_rig_end.json`) or in
  sidecars, because the camera record is written once at Start. The core
  cleans and size-checks each block: in the prototype a NaN stopped the
  record being written, and a 14.3 MB block made it unreadable, over the
  4 MB read limit (`camera_record.py:74`; measured).
- **Files:** `<run>_<short>_<what>`, created exclusively, only in the
  active run's folders, with the suffix registered so the run name counts
  as taken.
- **Project file** `<project>/typhon_plugins.json`: the plugin list and each
  plugin's settings block, written whole then renamed, under an OS lock and
  a temporary name unique to each writer. Two threads sharing one
  temporary name lost a block and failed 47 of 80 saves (measured);
  `camera_presets` already learned this lesson.

### 3.10 Errors

- **Every call into a plugin is guarded:** any exception, `SystemExit`
  included, switches that plugin off for the session ("faulted"), names it
  in the status line and appends the traceback to
  `<project>/plugin_errors.log`. A guarded call cost 0.39-0.41 µs against
  0.13-0.15 µs plain (measured).
- **On the recording path, a fault ends the recording cleanly** with the
  reason in `<run>_run_end.json`, as a failed write does since M0. Carrying
  on would leave half a run tagged or reduced and half not.
- **Hangs:** a 2 s watchdog switches a stuck plugin off. That contains only
  a hang that releases Python's lock: after a sleeping hang no frame was
  late, after a pure-Python hang 346 of 587 were (measured, synthetic).
- **Crashes:** `os._exit` and a crash inside an SDK cannot be caught. A
  crash marker around a plugin's first load and first camera open lets the
  next start ask whether to keep it on (QGIS does this, vendor docs); a
  built-in camera plugin is never switched off without asking.
- **A missing or incompatible plugin** never stops Typhon starting: it is
  listed with its reason, its features say "needs plugin X", and its data
  are kept untouched.

---

## 4. Loading, enabling and trust

### 4.1 Where plugins live

| What | Where | Why there |
|---|---|---|
| Built-in plugins | in the Council checkout, `typhon_plugins/<name>/`, each with its `plugin.json` | They are reviewed and tested with the Council, and updated with it |
| Third-party plugins | `~/.council/typhon/plugins/<id>/`, copied in by you | One folder you can see; Typhon never downloads or installs anything |
| The app-wide base set | `~/.council/typhon/base_set.json` | Read by every Typhon project on this PC |
| Approvals | `~/.council/typhon/approvals.json` | Once per plugin version, for the whole PC |
| A project's list and plugin settings | `<project>/typhon_plugins.json` | Travels with the project; names plugins, never holds code |
| A plugin's own data for one project | `<project>/plugin_data/<id>/` (no `.py` files) | Kept apart from the project's code |

- **Never plugin code inside a project folder.** The policy gate checks every
  `.py` file under a project, subfolders included, on Generate and Run
  (`gui_policy.py:490-529`). A plugin under `<project>/plugins/` that
  imported `subprocess` or `ctypes`, as camera code does, blocked the whole
  app (measured), and one in a folder whose name starts with a dot was
  silently skipped (measured; `gui_policy.py:502`), which is a loophole,
  not a feature. VS Code reached the same rule from the other side: opening
  a folder must never run code that came inside it (vendor docs, Workspace
  Trust). So a project names its plugins by id, version and, for
  third-party ones, sha256.
- **Plain JSON files, no Council settings.** A generated app runs as its own
  process, often without the Council open, and may not use `QSettings`
  (`gui_policy.py:99`). Every list is a JSON file written whole to a
  temporary file unique to the writer, then renamed, under an OS lock, as
  `camera_presets.json` is.
- **One place, pinned.** The classifier store moves with
  `COUNCIL_VAULT_ROOT` when an app is launched from the Council
  (`gui_runner.py:287` copies the environment; `frame_classes.py:712-718`
  reads it) but not when it is double-clicked, so one app can see two
  stores (prior-art fact-check). To avoid that here, a project's
  `typhon_plugins.json` records the absolute path of the app-wide folder
  when the project is created, and always reads that one. Tests point it at
  a temporary folder.

### 4.2 The app-wide base set and a project's additions

```json
{ "format": "typhon-plugins", "format_version": 1,
  "base": "C:/Users/<you>/.council/typhon",
  "added": ["acme.thermal"],
  "off":   [],
  "settings": { "typhon.classify": { "show_after_stop": true } } }
```

- **What a project runs** = the base set, plus `added`, minus `off`.
- **The base set** starts as all built-in plugins. You change it in the
  Plugins window ("for every project"). A new project starts with it,
  which is your answer 2.
- **Recommendation: the base set is followed, not copied.** A plugin you add
  to the base set appears in every existing project at its next start; a
  project can still switch one off for itself (`off`). The other reading of
  your answer, "copied once when the project is made", freezes old projects
  instead. Question 1 asks which you want.
- **The camera set-up joins the list.** Today `camera_setup.json` holds one
  choice, `basler` or `prophesee` (`camera_setup.py:41`, `FORMAT` 1 at 47).
  Its choice becomes "this camera plugin is on", and `FORMAT` 1 files keep
  being read, so an old project is not asked its set-up questions again.
- **Each run records what made it:** the id and version of every active
  plugin, and the sha256 of third-party ones, in the camera record's
  `plugins` block.

### 4.3 A plugin's states

The Plugins window shows each plugin in one state, with the reason:

- **discovered**: its manifest was read;
- **incompatible**: wrong API major version, a bad manifest, or a Python
  package missing from the project's interpreter;
- **needs approval**: third-party and not yet approved, or a file changed;
- **on**: in this project's list; **off**: not in it;
- **active**: its code has been imported because a feature was used;
- **faulted**: it raised an error and is off until the next start;
- **asked after a crash**: its crash marker survived (section 3.10).

### 4.4 Lazy loading

- **Start-up reads manifests only.** Scanning six `plugin.json` files took
  3.9-4.8 ms (measured). Reading 20 synthetic plugins and hashing all their
  files took 33-50 ms warm and about 250 ms just after the files were
  copied in or after a reboot; 100 plugins took about 1.2 s cold (measured,
  synthetic).
- **A plugin's code is imported when it is first needed:** its panel is
  opened, a Scan asks for its cameras, or a run uses one of its hooks.
- **The point is to keep today's start time, not to beat it.** Today's
  Typhon already imports sklearn only inside the functions that use it
  (`frame_classes.py:1465`, 1499), and its core modules import in
  0.24-0.50 s with neither sklearn nor pypylon loaded (measured). What
  lazy loading keeps off start-up: sklearn 3.3-5.2 s, pypylon 0.6-2.0 s,
  the classification panel's first open 239 ms (measured). The plugin host
  itself imported in 125-150 ms in a bare interpreter, mostly modules a
  running Typhon already has (measured; the extra cost inside Typhon was
  not measured).

### 4.5 How a plugin's code is loaded

- **On the Council side,** beside `frame_camera`, never in the generated
  project, whose code may not import `importlib` (`gui_policy.py:120-127`).
  `frame_camera` already loads the settings window this way
  (`frame_camera.py:3464-3467`). No change to the linked-module list is
  needed, because the app never imports a plugin; only the trust check's
  plugin mode allows `typhon_plugin_api`.
- **As a package named `typhon_plugins.<id>`,** without touching `sys.path`
  (5.6-16 ms per plugin, measured); files inside a plugin import each other
  with relative imports.
- **The bytes that were checked are the bytes that run.** A `.pyc` file
  planted in a plugin's `__pycache__` ran instead of the approved source
  while the source's sha256 still matched (measured). So the loader reads
  each file once, checks its sha256, compiles exactly those bytes, and
  never reads or writes `.pyc` files. It hashes every file in the folder,
  not only `.py`, and refuses a folder holding `__pycache__`, `.pyc`, `.pyd`
  or `.dll` files. Native code comes only from packages you install into
  the environment yourself (pypylon, OpenEB).
- **Python packages a plugin needs** are checked in the project's own
  interpreter (`python_envs`), never installed. A missing package switches
  that plugin off with a hint and the app starts, where today a missing
  `requires` entry such as sklearn stops the whole app
  (`gui_emit.py:3212`, `raise SystemExit(3)`).

### 4.6 Plugins written by others: trust

**What to say plainly, on screen and here:** an approved plugin is ordinary
Python running with Typhon's rights. It can read and write your files, use
the network and start programs, whatever its manifest says. The steps below
protect against mistakes and make you look before trusting; they do not
fence a plugin written to do harm.

1. **You copy the plugin's folder** into `~/.council/typhon/plugins/` (from
   a zip, a USB stick or a git checkout; question 7). Typhon never fetches
   one.
2. **The Plugins window lists it as "needs approval"** and shows its
   manifest (author, version, what it attaches to, files it writes, network
   use), every file with its size and sha256, the Python packages it needs
   and whether the project's interpreter has them, and a scan for risky
   patterns. The scan builds on `council_core/ide_trust.py`, which already
   keys trust on sha256 and calls itself "a speed bump, not a sandbox"
   (`ide_trust.py:14-24`), rather than on the policy gate, which would flag
   Typhon's own camera code (`frame_camera` imports `ctypes` at 2537,
   `socket` at 2795 and `importlib` at 3464).
3. **You approve it once per version, for the whole PC.** The approval
   records every file's sha256 in `approvals.json`. Any changed file sends
   it back to "needs approval". Switching it on stays per project.
4. **Network:** the manifest says `none`, `camera-link` (a GigE camera on a
   local network) or `internet`. Typhon refuses `internet` while the
   Council is offline. A Python audit hook reports a plugin's connections
   to anything but this PC or a declared camera network; it catches
   mistakes only, and a camera SDK's own native traffic never reaches it.
5. **Its contract suite runs after you approve it,** and the result is
   shown as information, never as proof of safety. Running it before
   approval would run code you have not approved; the Council's fenced test
   runner blocks PySide6 and does not fence `ctypes`
   (`gui_smoke.py:29-32`), so panels could not be tested there anyway.
6. **Each run names the plugins and versions that made it** (4.2).

What the policy gate does not stop, measured with snippets that passed it:
writing over files (`open(..., "w")`, `os.replace`, `shutil.move`,
`Path.write_bytes`), `os.chmod`, `os._exit`, network access through
`asyncio`, `socketserver`, `xmlrpc` or `ssl`, `ProcessPoolExecutor`,
`os.startfile` and `webbrowser`, and Council internals reached through
`frame_camera`'s attributes. These are gaps for every generated app, not
only plugins, and are worth fixing on their own.

**An optional extra guard against accidents:** mark each saved frame and each
finished CSV read-only on the writer threads, 0.23-0.27 ms per file
(measured). Afterwards writing, appending, replacing and deleting all
failed; `os.chmod` undoes it, and Windows asks before deleting read-only
files (question 9).

### 4.7 Versions and updates

- **Built-in plugins** update with the Council checkout and carry its
  version.
- **Third-party plugins** are approved per version; an update is a new
  folder or new files, so it asks again.
- **The plugin API's major version** is checked at scan time; a plugin for
  another major version is listed as incompatible, with both numbers.
- **Old runs stay readable** after a plugin is removed: its record blocks
  and sidecar files are kept and shown as "written by plugin X 1.2, not
  installed".

---
