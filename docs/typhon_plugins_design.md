# Typhon as a plugin system, merged with multi-camera M1: design

Date: 15 October 2026 (research and fact-checks done the same day). Status:
design only; nothing is built, and it awaits your approval, like the
velocity and multi-camera designs it builds on
(`docs/typhon_velocity_design.md`, `docs/typhon_multicamera_design.md`).
This doc lives on branch `docs/typhon-plugins` (revised after a design
review on `docs/typhon-plugins-revised`), which starts at
`qt-migration` 393671b8 ("Merge typhon/trigger-downsample-fixed2", 15
October 2026, 09:35). **Every Typhon file and line reference below is to
393671b8**, so they resolve on the branch the doc lives on. Your answers to
questions 1, 2, 6, 7 and 13 (15 October 2026) were folded in on branch
`docs/typhon-plugins-answers`, which starts at `qt-migration` 43b317e8. No
file this doc cites changed between 393671b8 and 43b317e8 (measured,
`git diff`), so every reference resolves there too.

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
Typhon you use today keeps working while it is built. Your later answers
to five of section 13's questions are quoted there too: a plugin added to
the base set reaches only projects made afterwards; Downsample, presets,
run records and the crop box stay in the core; today's Typhon takes bug
fixes only, and new features are built as plugins; and a plugin arrives as
its own folder that you drag into the app's plugins folder, approved once.

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

A label may add, after a semicolon, which research report a figure or
finding came from, for example "(measured, synthetic; interface
fact-check)". The kind before the semicolon is always one of the six above.

**How the research was done.** Three reports fed this design: an audit of
where Typhon's code would split into a core and plugins ("seams"); a survey
of how established plugin systems work ("prior art"); and a working
prototype of the interface with contract tests and benchmarks ("interface";
"the prototype" below is that report's prototype). Each was checked by a
separate fact-check that re-ran most measurements with its own scripts, named
after its report ("seams fact-check", "prior-art fact-check", "interface
fact-check"). **Where a report and its fact-check disagree, this doc uses
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
   (vendor/project docs), and the Council's own policy gate says it is "a gate
   against generated code going wrong, not a sandbox" (`gui_policy.py:24-39`).
   Trust therefore rests on you approving a plugin, once per plugin (your
   answer to question 7): a later version or an edited file is not asked
   about again. Section 4.6 says plainly what an approved plugin can do.
3. **A plugin on its own thread is not isolated from the camera's grab
   thread.** A plugin holding Python's lock for 20 ms per call made 313-340
   of about 575 frames of a 150 fps camera arrive late (measured, synthetic).
   Most of that comes from Windows' 15.6 ms timer tick and can be fixed in the
   core (section 10), but heavy live work such as classifying every frame
   stays off the recording path by default.
4. **It is a large piece of work.** About 41.5-64 days for the plugin
   system on top of M1's 24-37 days, about 65.5-101 days together, plus
   merge upkeep (estimates, section 12). The first stages leave Typhon
   behaving exactly as today; the screen changes only at stage P4.

---

## Terms used here

- **Core**: the part of Typhon every project always has: the window, live view, Start and Stop, run folders and records, presets, the settings host, the slider, data-safety rules and the plugin host.
- **Plugin**: a folder of Python files plus a manifest that adds one thing to Typhon (a camera family, triggering, classification), attached only at fixed places.
- **Built-in plugin**: a plugin on the list of built-ins that ships with the Council code and is tested with it (Basler, Prophesee, Simulated, Triggering, Classification, Timing scan, Cropped export), trusted like core code. A plugin is built-in because it is on that list, never because of the folder it sits in: a test checks the list against the folders, and any other folder found among the built-ins is refused, not loaded (4.1, 4.5).
- **Third-party plugin**: a plugin written by someone else, which you drag, as its own folder, into Typhon's plugins folder; it runs only after you approve it, once (4.6).
- **Plugin host**: the core object that reads manifests, loads plugins, calls them safely and gives them their context; in this design it is the multi-camera `Rig`.
- **Manifest**: `plugin.json` in a plugin's folder: its id, version, the plugin API version it needs, its kind, what it provides and needs, and where it attaches. Typhon reads it without running any plugin code.
- **Plugin API**: the one Typhon module a plugin may import (`typhon_plugin_api`), with a version number of its own: 0.x on the branch while the built-ins shape it, frozen as 1.0 at stage P5 (3.2).
- **Major / minor version**: in "1.2", the 1 is major and the 2 minor. From 1.0 on, an addition raises the minor number; a removal, rename or changed call raises the major.
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
- **Recording path**: the record gate, the frame stage and per-frame tags; the only plugin calls whose fault ends a recording (3.10). Start-time and end-of-run hooks are not on it.
- **Namespace**: a prefix that keeps one plugin's keys and files apart from everyone else's, for example `acme.thermal/gain` for a third-party plugin. Built-in plugins keep the keys and names that already exist or that their own designs fixed (3.9).
- **Sidecar file**: a file a plugin writes beside a run's files, never over anything. A built-in plugin keeps the name its design fixed (`<run>_capture_triggers.csv`, `<run>_calibration.json`, `<run>_tracks.csv`); a third-party plugin's are named `<run>_<short>_<what>` (3.9).
- **App-wide base set**: the plugins every new Typhon project on this PC starts with, copied into the project when it is made, with each plugin's default settings; the level you work at (4.2). A plugin added to it later reaches only projects made afterwards; its default settings are still read by every project (4.2, question 14).
- **A project's plugin list**: the base set as it was when the project was made, changed after that only by you, in that project; with its setting overrides, all a project keeps of its own (4.2).
- **Plugin home**: the app-wide folder holding the base set, approvals and third-party plugins: `~/.council/typhon/`, or the folder the `TYPHON_PLUGIN_HOME` variable names (tests, the plugin checkout's side vault; 4.1).
- **Plugins folder** (the approved plugins folder): the app's one folder on the PC where third-party plugins live, each in its own folder that you drag in (proposed: `~/.council/typhon/plugins/`).
- **Approval**: your decision to let a third-party plugin run, made once per plugin (by its id) for the whole PC; a later version or an edited file is not asked about again (4.6). It keeps a fingerprint of every file as it was when you approved, for information only.
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
- **Compatibility view (facade)**: an old name kept working by forwarding to the new code, so existing callers and tests need no change (for example `frame_camera._LIVE`). Used only where the code is new (the rig); code that a plugin takes over is not moved behind a facade while the branch lives, it stays where it is under a thin plugin adapter (7.2).
- **Adapter plugin**: a built-in plugin folder that holds only its manifest and a thin layer over code that stays in `council_core` and `frame_camera`, reached through re-exports in `typhon_plugin_api`, until the files move at stage P7 (7.2).
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
- **A Plugins window** (Settings → Plugins…): the app-wide base set (what
  new projects start with), this project's own list, each plugin's version
  and state (on, off, needs approval, switched off after an error), and the
  one-time approval of a third-party plugin whose folder you have dragged
  into the plugins folder.
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

### Your answers to the follow-up questions (15 October 2026, your words)

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
  image viewer never touch it and stay as they are (section 8). On the
  branch, a Barbie test that fails is retired there and then, on purpose,
  not fixed (8.2).
- **1, plugins by others:** one versioned plugin API (frozen as 1.0 once
  the built-ins have used every attachment point), a data-only manifest,
  approval by you once per plugin (your answer to question 7, below),
  contract suites an author can run, a written guide for authors (3.11),
  and nothing ever downloaded or installed by Typhon (sections 3 and 4).
- **2, app-wide and per project:** the app-wide level is the base you work
  at: a base set of plugins, with each plugin's default settings, that every
  new project starts with. A project adds plugins over time, may remove one
  for itself, and may override a setting (section 4.2). A plugin you add to
  the base set later reaches only projects made afterwards (your answer to
  question 1, below).
- **3, pop-out panel:** plugins add only buttons to the toolbar strip and
  short texts to the status strip; every other control is in a pop-out
  window Typhon owns (section 3.7).
- **4, merge with M1 on a separate branch:** a long-lived branch
  `typhon-plugins` in its own checkout and its own vault; the new app is a
  separately named example, "Typhon (plugins)", run beside today's Typhon;
  `qt-migration`'s fixes are merged into the branch every week; a switch-over
  with migration when you are ready (sections 5 and 7).

### Your answers to section 13 (15 October 2026, your words)

> "Q6: Bug fixes only new features as plugins. Q2/13: Yes they stay in
> core Q1: Only new projects Q7: Plugins will be their own folders dragged
> into the plugins folder for the app. Approval only happens once for each
> plugin"

What the design does with each:

- **Q1, new projects only:** a new project's plugin list is copied from
  the base set when the project is made. A plugin you add to the base set
  later reaches only projects made afterwards; an existing project keeps
  its own list until you add the plugin there, in that project's Plugins
  window (section 4.2).
- **Q2 and Q13, the core:** Downsample, presets, run records and the crop
  box stay in the core; the camera families (Basler, Prophesee,
  Simulated), triggering, classification, the timing scan and cropped
  export become plugins (section 2). Downsample stays the core's one frame
  stage, with its control in the settings notebook (2.1).
- **Q6, today's Typhon:** today's Typhon on `qt-migration` takes bug fixes
  only while the branch lives, and every new Typhon feature is built as a
  plugin on the branch: velocity, calibration, alignment and the trigger
  cable (M2) included (sections 7.2 and 11). The question's second part,
  from when it may take no fixes at all, was not answered and stays open
  (question 6).
- **Q7, plugins from others:** each plugin is its own folder, which you
  drag into the app's one plugins folder, `~/.council/typhon/plugins/`
  (4.1; "the plugins folder for the app" is read as that one folder, not
  one inside each project, and question 7 asks you to confirm the
  reading). You approve each plugin once; a later version dragged in over
  it, or an edited file, runs without being asked about again. That is the
  trade-off, and section 4.6 states it in full. Whether the Plugins window
  should still show "changed since you approved it", as information that
  never blocks, is left open as a suggestion (4.6; question 7).

### The honest limits

- **No sandbox.** An approved plugin can read, write and delete files,
  make a frame's pixels writeable again, use the network, start programs
  and end Typhon (`os._exit`), whatever its manifest says (the full list is
  in 4.6). Plugins are not checked by the policy gate at all: they load on
  the Council side, and the risky-pattern scan you see before approving
  one is a regex speed bump. A plugin handed only its own panel disabled
  the main window's Start button in a test (measured). Approval is the
  protection, and it is given once per plugin, so a later version or an
  edited file runs without you looking at it again (section 4.6).
- **Live work competes with the camera.** A slow plugin delays frames even
  on its own thread (measured, section 10.2). Live classification of every
  640 x 480 frame took 17-49 ms per frame and made 95-205 of about 575
  frames late at 150 fps (measured, synthetic), so classification runs after
  Stop by default.
- **Plugin windows are built in code,** not drawn in the Designer
  (question 4).
- **Moving a project whose classifier, timing or crop handlers you edited is
  blocked by today's Update from example** (measured; the project was left
  half-switched). The half-switched state is a fault in today's Designer,
  so its fix goes onto `qt-migration` now; the switch-over also needs a
  step, inside Update itself, that sets those handlers aside without
  deleting them (sections 6.4 and 7.5).
- **A hung plugin is not always stoppable:** a plugin stuck in a pure-Python
  loop kept delaying frames after the watchdog had switched it off: 346 of
  587 frames late (measured, synthetic). Only a helper process can contain
  that (section 3.10).
- **Effort:** about 65.5-101 days with M1, plus about half a day a week of
  merging while the branch lives (estimates, section 12).

---

## 2. Core vs plugins

### 2.1 What stays in the core

The core is what every Typhon project needs whatever its cameras and
features, plus the rules that keep recorded data safe. **Decided** (your
answer to questions 2 and 13, "Yes they stay in core"): Downsample,
presets, run records and the crop box stay in the core, with the rest of
the table below; the camera families, triggering, classification, the
timing scan and cropped export become plugins (2.3). Sizes are lines of
code (measured with `wc -l` and Python's `ast` at 393671b8, or at 67cad625
where marked).

| Core piece | Where it is today | Size | Why it stays core |
|---|---|---|---|
| Rig and camera slots: connect, start, stop, the background job, area, settings access, the boxes beside Start, status lines | `_Live` (`frame_camera.py:175`, the one instance at 373) and `frame_camera`'s session functions | About 2,300 lines of `frame_camera`'s functions (2,276 at 67cad625, measured) | Every camera and feature hangs off it; it becomes the plugin host (section 5) |
| Recording: grab loop, writer threads, run reservation | `council_core/capture.py` (1,604 lines): `CaptureSession` (1128), `FrameWriter` (778), `Recorder` (485), `claim_run` (416) | 1,604 | The data-safety rules live here: exclusive creation, the first failed write ends the recording |
| Run records | `council_core/camera_record.py` (351) and `frame_camera`'s record code (2569-2650) | About 430 | Written once, format 1; plugins add namespaced blocks only |
| Presets | `council_core/camera_presets.py` (964), `council_qt/widgets/preset_picker.py` (247), `PresetsMixin` (`camera_settings_window.py:1581`), `frame_camera`'s preset functions | About 1,900 (377 in `frame_camera` at 67cad625, measured) | One file per project, one lock; plugins own key prefixes |
| Settings host | `settings_tabs.py` (904), `camera_settings_window.py` (2,561), `camera_categories.plan` (`camera_categories.py:140`), and the generic part of `camera_settings.py`: `Setting` (99), `Change` (178), `Provider` (492), `stops_needed` (721), `apply` (837) | About 4,200 | Draws any camera's settings from its description. Not yet fully camera-neutral: the generic `stops_needed` calls the Basler-shaped `_triggers_in_the_way` directly (`camera_settings.py:739`, 753). It gains a `Provider.extra_stops(values, described) -> list[str]` hook instead, empty by default, which the Basler plugin fills with today's `_triggers_in_the_way`. The SFNC trigger helpers (`Trigger` 332, which the base `Provider.triggers_on` returns at 552; `trigger_key` and `split_trigger_key` 367-400) are GenICam conventions a future GigE plugin needs too, so they stay shared, re-exported as "GenICam helpers" in `typhon_plugin_api` |
| Slider and live view | `capture_review.py` (1,033, less the raw view), `council_core/live_display.py` (153), `pop_out.py` (126) | About 1,100 | Every recording is reviewed the same way |
| Crop box | the `roi` port and `frame_roi.parse_roi`, which the core's area parser uses (`frame_camera.py:3112-3113`) | Small | The box is drawn on the live view and the slider; only the export moves out |
| Set-up wizard shell | the generic parts of `council_core/camera_setup.py` (`setup_path` 365, `load_choice` 369, `save_choice` 386) and the wizard's pages (`camera_wizard.py`) | 403 + 481, less each brand's guide, checks and scan page | Each camera plugin brings its own guide and checks into it |
| Downsample (decided: your answer to question 2) | `council_core/reduce.py` (165) and its calls on the writer threads and display (`capture.py:571-573`) | 165 plus the calls | It changes what is saved and the picture size every feature sees (`reduce.py:1-20`); the camera half (binning) is a camera facet. The slot holds the factor and method, and the core adds the Downsample row to every frame camera's "Capture" group (2.5). Every plugin's coordinates convert through it (3.12) |
| Plugin host | new | 200-400 lines plus tests (estimate) | Reads manifests, loads, guards and calls plugins |

**Decided: Downsample in core.** The interface research and its fact-check
both found that Downsample is half camera (Basler binning, done in the
camera) and half a change to the picture before it is saved and shown (done
by Typhon on the writer threads and the display's thread pool,
`reduce.py:17-22`). The seams research had it as a feature plugin. This doc
keeps it in the core as the one "frame stage", because a plugin that changes
saved pixels would sit inside the data-safety rules, and because every other
feature must know the factor. The camera half becomes a `Binning` facet of
the Basler plugin. Its control leaves the main window for the settings
notebook, where the "Capture" group already shows it
(`camera_categories.py:41-45`); on the branch the core, not the camera
plugin, describes that row (2.5). The reports did not settle it; you did,
on 15 October 2026: "Q2/13: Yes they stay in core" (question 2).

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

### 2.3 The built-in plugins, and the code each takes over

These seven are the split you agreed to (question 13): the three camera
families, triggering, classification, the timing scan and cropped export.

**While the branch lives, this code stays where it is.** Each built-in
plugin is an adapter: a folder with its manifest and a thin layer that
reaches today's code in `council_core` and `frame_camera` through
re-exports in `typhon_plugin_api`. The files move into
`typhon_plugins/<name>/` only at stage P7, once today's Typhon is frozen
(7.2). So the second column says what each plugin takes over, not what is
moved on the branch.

| Plugin | Code it takes over (today's place; moved only at P7) | Size (measured) | What it provides |
|---|---|---|---|
| **Basler** (`typhon.basler`) | `BaslerBackend` and `BaslerDevice` (`council_core/cameras.py:637-1634`); `BaslerSettings` (`council_core/camera_settings.py:1024-2125`); `_triggers_in_the_way` (`camera_settings.py:753`), reached through the new `Provider.extra_stops` hook (2.1), while the SFNC helpers `Trigger` (332), `trigger_key` and `split_trigger_key` (367-400) stay shared GenICam helpers; `council_core/basler_scan.py`; the Basler set-up guide and checks (`camera_setup.py:97`, 239); the wizard's scan page (`camera_wizard.py:206-210`); its rows of the category tables | 996 + 1,102 + 1,265, plus helpers and set-up code: about 3,500-3,900 | Capabilities `frames`, `grab_failures`, `restartable`, `software_trigger`, `hardware_trigger`, `binning`; facets for triggering and binning |
| **Prophesee** (`typhon.prophesee`) | `EvkBackend`, `EvkDevice`, `_EventSink`, `identify`, `accumulate_events` (`cameras.py:1640-2277`, 329-369); `EvkSettings` and the display settings (`camera_settings.py:2415-2769`, 2301-2331); `council_core/event_playback.py`; the raw view inside the slider (`capture_review.py:649`, 756, 774); the PNG / Raw button; the EVK set-up guide and checks (`camera_setup.py:272`); its category rows (the Filters tab) | About 620 + 470 + 468 + 190 | Capabilities `events`, `raw_recording`, `area_live` (and not `restartable`); a "review source" for the slider; later `event_stream`, `trigger_in`, `sync` (sections 3.4, 11) |
| **Simulated** (`typhon.simulated`) | `SyntheticBackend`, `SyntheticDevice` (`cameras.py:2404-2802`), `SyntheticSettings` (`camera_settings.py:2864-3003`) | About 400 + 140 | The reference plugin for every contract suite; gains N cameras through `COUNCIL_SIM_CAMERAS=frame,frame,event` (the multi-camera design's name, its 2.10) |
| **Triggering** (`typhon.trigger`) | `council_core/triggered.py` (1,227); `frame_camera`'s trigger code (462 lines on the trigger branch, measured), among it `capture_now` (946), `set_capture_mode` (1007), `set_trigger_source` (1018), `set_frames_per_trigger` (1028), `set_timer` (1038); the "camera waits for a trigger" diagnostics (67 lines); `settings_tabs.CAPTURE_BOXES` less its Downsample entry (`settings_tabs.py:112`); the trigger rows of the "Capture" group, which today `BaslerSettings` builds by importing `triggered` (`camera_settings.py:1071-1209`); that group's Downsample row stays core (2.5) | 1,227 + 462 + 67, plus parts of `settings_tabs` and `BaslerSettings`: about 1,800 | Software and Timer sources; the record gate; the run hooks of section 3.8; the `<run>_capture_triggers.csv` file (`triggered.py:350`); the trigger columns of the frames CSV, kept under their present names; a panel, the Capture now (F9) button and a status segment |
| **Classification** (`typhon.classify`) | `council_qt/widgets/classifier_picker.py` (588) inside a pop-out; `frame_classes.py` (4,640) stays a library with unchanged file formats, with one public function added (`predict_image`, P4 row 4); 32 shapes and 17 script links leave the wireframe | 588 + a new panel | A pop-out panel; "Classify all" after Stop; its `sklearn` requirement, today declared by the whole app (`typhon.gspec` "requires") |
| **Timing scan** (`typhon.timing`) | `frame_timing.scan_report` (`frame_timing.py`, 289 lines, unchanged); 5 shapes | 289 | A small panel with Scan and the list of bad frames |
| **Cropped export** (`typhon.crop`) | `frame_roi.export_roi` (`frame_roi.py`, 192 lines, unchanged); 4 shapes | 192 | A small panel: output folder, Save cropped frames, status; it reads the crop box, which stays core, through `ctx.crop_box()` (3.5) |

Three notes on the table:

- **`frame_classes`, `frame_timing` and `frame_roi` do not move, and their
  file formats do not change.** They are stable libraries that Barbie v1-v3
  also use (section 8). The plugins wrap them; the only change is one public
  function added to `frame_classes` (`predict_image`, so live guesses need
  no private calls; P4 row 4).
- **The Classification plugin needs only three things from the core:** the
  active camera's capture folder, the current frame, and a "frames were
  listed again" event (today `CaptureReviewer.on_reload`,
  `capture_review.py:364`). Those become part of the plugin context
  (section 3.5).
- **The Cropped export plugin needs a fourth: the crop box.** Today's Save
  cropped frames link takes `capture_folder`, `roi` and `output_folder`
  (measured from `examples/gui/typhon.gspec` at 393671b8, shape `s21` →
  `frame_roi.export_roi`), and the box (the `roi` port) stays in the core.
  So the context gains `crop_box()` and a `crop_box_changed` event (3.5).

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
   (section 3.4). The types they take and return (`CaptureOptions`,
   `CaptureSetup` or its replacement, the armed record) are defined in
   `typhon_plugin_api`, so a camera plugin never imports the triggering
   plugin (2.5).
6. **`capture.py` importing `reduce` and `triggered`** (`capture.py:43`)
   becomes things registered at Start: the record gate (already handed in
   as `gate=`), the frame stage, end-record blocks, run file suffixes.
7. **The list of names that mark a run as taken**, `RUN_MARKERS`
   (`capture.py:387-388`), becomes a registry each plugin adds its suffixes
   to. Creating `<run>_frames.csv` exclusively stays the real reservation.
8. **`camera_record.build(capture=..., downsample=...)`**
   (`camera_record.py:103-112`) keeps both arguments and their top-level
   `capture` and `downsample` keys, which format 1 already defines
   (`camera_record.py:121-135`, 171-174): the triggering plugin fills
   `capture`, the core fills `downsample`. It gains `plugins={id: block}`
   at the same `format_version` 1, for new plugin data only (section 3.9,
   "Records").
9. **The category tables** (`SHARED_TABS` 38, `TIPS` 62, `MOST_USED` 75 in
   `camera_categories.py`) take rows from each camera plugin; `plan()`
   stays core. A group the tables do not know already gets a tab of its own
   (`camera_categories.py:25-29`), so a third-party camera works without
   them; the limit is the width of the tab strip (section 3.7).
10. **`settings_tabs.CAPTURE_BOXES`** (`settings_tabs.py:112`) moves to the
    triggering plugin's panel, less its `downsample` entry, whose box leaves
    the main window for the notebook's "Capture" group (2.1, 2.5).
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
    tabs read them with `getattr` and a default (code; seams fact-check).

### 2.5 Splitting the capture set-up

**Today it is one object on the open camera.** A frame camera's
`device.capture` (`cameras.py:479-483`; made at 778-781 for a Basler and
2503-2506 for the simulated camera) is one `CaptureSetup`
(`triggered.py:145-201`) holding the trigger mode, source, frames per
trigger, timer, the hardware edge, delay and debounce, **and** Downsample.
The camera's settings provider describes it as the "Capture" group "so
presets, the settings tabs and a run's camera record carry it with no
plumbing of their own" (`triggered.py:42-46`). What the camera offers the
set-up, `CaptureOptions` (`triggered.py:209-224`), is defined in
`triggered.py` too, and the Basler and simulated code import that module
(`cameras.py:778`, 2505, 2525; `camera_settings.py:1071-1209`,
1622-1664). With triggering a plugin and Downsample in the core, the
object has to be split, and a camera plugin may import only
`typhon_plugin_api`, never the triggering plugin.

| Piece | Held by | Described by | Preset key | Camera record |
|---|---|---|---|---|
| Downsample: factor and method | the camera slot (core); the slot hands it to the camera's `Binning` facet or to the core's frame stage | the core, which adds the Downsample row to every frame camera's "Capture" group, using the camera's `CaptureOptions.downsample_live` and `downsample_help` | `save.downsample`, as today | top-level `downsample` block, as today, and the factor inside `capture`, as today |
| Trigger set-up: mode, source, frames per trigger, timer, edge, delay, debounce | the triggering plugin, as per-slot state | the triggering plugin, as `Setting` rows drawn in its pop-out (3.7) | `capture.*`, as today | top-level `capture` block, as today |
| What the camera offers: its hardware sources, edge, delay and debounce ranges, whether Downsample can change live | `CaptureOptions`, returned by the camera plugin | | | |
| The types: `CaptureOptions`, `CaptureSetup` (or a slimmer trigger set-up without `downsample`), the armed record, the facets' signatures (3.4) | `typhon_plugin_api` | | | |

- **What you see change, at P4:** the trigger rows (with the camera line's
  edge, delay and debounce) leave the notebook's Exposure tab for the
  triggering pop-out; the "Capture" group in the notebook keeps the
  Downsample row. Until P4 the rows show where they do today.
- **Keys stay readable both ways.** The built-ins keep writing today's keys,
  `capture.*` and `save.downsample` (`triggered.py:81-88`), so a preset
  saved by the plugin Typhon still sets the trigger mode and Downsample in
  today's Typhon, and the other way round. Third-party plugins use
  `<id>/<key>` (3.9). The core splits off `save.downsample` for itself and
  `capture.*` for the triggering plugin before `camera_settings.apply`;
  with the triggering plugin off, the `capture.*` keys are kept untouched
  and listed, like any absent plugin's keys.
- **While the code stays in place** (7.2), `device.capture` stays the
  storage today's code reads (for example the Basler's Downsample,
  `cameras.py:1199`, 1262); the slot and the triggering plugin read and
  write it through the API. The split is in who owns, describes and saves
  each part. The object itself is divided when the files move at stage P7.
- **Where the work goes:** stage P3 row 3, about 1-1.5 days of its
  estimate (estimate; section 12).

---

## 3. The plugin interface

All sketches are Python-style outlines of the plugin API as it would be
frozen at 1.0 (numbered 0.x on the branch until then, 3.2), not finished
code. What a third-party author needs in one place is section 3.11.
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
  "needs": ["frames"],
  "python": [],
  "contributes": ["panel", "toolbar", "status", "run_hooks", "record_gate"],
  "files": ["_capture_triggers.csv"],
  "network": "none",
  "budget_ms": 5
}
```

- **Fields:** `schema` (the manifest's own format), `id` (reverse-domain;
  `typhon.` is reserved for built-ins), `short` (used in a third-party
  plugin's file names; unique, and the built-ins' short names are
  reserved, 3.9), `version`, `api` (the plugin API it needs),
  `kind` (`camera`, `source` or `feature`), `scope` (`camera` or `rig`),
  `entry` (`module:object`, imported on first use), `needs` (capabilities
  that every use of the plugin needs; `a|b` means either), `python`
  (packages it needs, checked in the project's interpreter, never
  installed), `contributes`, `files` (run-file suffixes, 3.9), `uses`
  (other plugins it works with, each optional or required; section 11.5),
  `network` (`none`, `camera-link` or `internet`; section 4.6), `budget_ms`
  for live calls. Third-party manifests also carry `author`, `description`
  and `licence`, shown in the Plugins window. The full schema, with types
  and which fields are required, is in 3.11.
- **`needs` holds only what every use needs.** Triggering needs `frames`
  and nothing more in its manifest, because snapshot capture from Software
  or the Timer needs no camera trigger at all; which facet a run needs
  depends on its mode and source, so `check_start` checks it per run
  (3.6).
- **`files` for a built-in keeps its design's names.** `_capture_triggers.csv`
  is today's name (`triggered.py:350`), so it stays; only third-party
  suffixes follow the `<run>_<short>_<what>` rule (3.9).
- **JSON, and no pip entry points.** Every other Typhon file is JSON
  (`tomllib` would also work, vendor/project docs). Entry points would let any
  package installed into a shared conda environment register itself without
  your approval; one folder you can see and approve is simpler (estimate;
  prior-art fact-check).

### 3.2 The plugin API and its version

- **One module, `typhon_plugin_api`,** on the Council side next to
  `frame_camera`, is the only Typhon code a plugin imports. Built-in plugins
  obey the same rule (a test checks their imports), which is the real proof
  that the API is enough for outside authors.
- **0.x while the branch shapes it.** On the branch the API is numbered
  0.x, and any change is allowed: before stage P4 no camera plugin, run
  hook or UI point has been used for real, and freezing first would force
  2.0, 3.0 and so on within weeks. It is frozen as **1.0 at stage P5**,
  with the author's kit (3.11), after the built-ins have used every
  contribution point. Only then does the rule below hold, and only then
  are plugins from other people accepted.
- **Version rule from 1.0** (Spyder's, vendor/project docs): an addition
  raises the minor number; a removal, rename or changed call raises the
  major. `"api": "1.2"` means "1.2 or later, before 2.0". A plugin needing
  another major version is refused with a message naming both numbers, as
  Micro-Manager does (vendor/project docs). Deprecated names keep working
  for one major version. How a plugin detects an optional feature of a
  later minor version is in 3.11.

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
  imports pypylon (`cameras.py:666`), 0.6-2.0 s (measured: the range over
  the research's cold-import timings; the interface prototype's start-up
  benchmark gave 1.05 s).

### 3.4 Capabilities and facets

| Capability | Meaning | Facet |
|---|---|---|
| `frames` / `events` | picture frames / an event stream drawn as windows | |
| `restartable` | the stream may be stopped and started on one connection (an EVK4's may not, `cameras.py:1945`) | |
| `area_live` | the area can change while streaming | |
| `grab_failures` | failed grabs are counted (M0) | |
| `raw_recording` | records its own file (`.raw`) | |
| `software_trigger` | Typhon can fire it; needed only for an exact run from Software or the Timer (3.6) | `SoftwareTrigger`: arm, ready, fire, disarm |
| `hardware_trigger` | waits for an input line; needed only for an exact run from a camera line (3.6) | `HardwareTrigger`: inputs, arm, disarm, count edges |
| `binning` | the camera can average its own pixels | `Binning`: can_bin, apply |
| `camera_clock` | frames carry a usable camera time stamp | known only after the first frame: a boost CXP camera and the emulator give 0 (`cameras.py:213-218`) |
| `event_stream` | raw event arrays can be passed on | `EventStream`: subscribe; reserved in 1.0, built with velocity (section 11) |

- **Two steps.** The manifest's list lets the screen offer features before
  anything is imported; the opened camera confirms it. The facets are
  today's base-`Device` methods regrouped (`cameras.py:491-535`); a camera
  that has none returns no facet instead of ten "cannot" methods.
- **The facets' types live in the API.** The facet classes and everything
  they take or return (`CaptureOptions`, the trigger set-up, the armed
  record, today in `triggered.py`) are defined in `typhon_plugin_api`
  (2.5). A camera plugin implements a facet against the API alone and never
  imports the triggering plugin. The exact signatures are in 3.11.
- **Unknown names:** a capability a camera *offers* that Typhon does not
  know is ignored; a capability a feature *needs* that Typhon does not know
  counts as missing, so the feature stays off with a reason (the rule the
  prior-art fact-check recommended).
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
    def settings_rows(self) -> list[Setting]: ...  # drawn by Typhon in the pop-out (3.7)
    def panel(self, parent: QWidget) -> QWidget: ...          # built on first open
    def toolbar(self) -> list[Action]: ...
    def status(self) -> str: ...                              # at most 80 characters

class PluginContext(Protocol):
    plugin_id: str
    def say(self, text: str) -> None: ...                     # the status line
    def call_in_ui(self, fn: Callable[[], None]) -> None: ...  # from any thread (3.11)
    def cameras(self) -> Mapping[str, CameraHandle]: ...
    def active_camera(self) -> str | None: ...
    def capture_folder(self, camera: str | None = None) -> Path | None: ...
    def current_frame(self) -> Path | None: ...
    def crop_box(self, camera: str | None = None) -> CropBox | None: ...
        # (x, y, w, h) in the saved pictures' pixels, with the Downsample
        # factor those pictures were saved with (3.12)
    def on(self, event: str, fn: Callable) -> Callable[[], None]: ...
        # "frames_reloaded", "camera_changed", "crop_box_changed",
        # "run_started", "run_stopped"
    def settings(self) -> dict: ...  /  def save_settings(self, values: dict) -> None: ...
    def create_file(self, folder: Path, name: str) -> IO: ...  # (a) streamed, during a run
    def write_new(self, folder: Path, name: str, data: bytes) -> Path: ...
        # (b) whole file, temporary name, then a rename that refuses an
        # existing name; "_2", "_3" ... on a rerun (3.9)
    def read_data(self, relative: str) -> bytes: ...          # the plugin's own checked files (4.5)
    def temp_dir(self) -> Path: ...                           # removed by Typhon
```

**The crop box.** A `CropBox` holds `xywh`, the box as `(x, y, w, h)`, and
`factor`. The Cropped export plugin calls
`frame_roi.export_roi(ctx.capture_folder(), box.xywh, folder)`
exactly as today's link calls it with the `capture_folder`, `roi` and
`output_folder` ports (`frame_roi.py:104`; `parse_roi` takes a tuple,
`frame_roi.py:44-63`). A contract test checks that it writes the same files
as today's link (9.1).

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
- **What a run needs depends on its mode and source,** so the manifest
  needs only `frames` (3.1) and `check_start` checks the rest for each run,
  as today's code does:
  - **snapshot from Software or the Timer:** frames only. Typhon keeps the
    next frames after each trigger and arms nothing on the camera:
    `BaslerSettings.trigger_problem` returns "" for it before it looks at
    any trigger node (`camera_settings.py:1680-1686`), and the simulated
    camera allows it (`cameras.py:2532-2539`);
  - **snapshot from a camera line:** refused, as today
    (`camera_settings.py:1681-1685`);
  - **exact from Software or the Timer:** the `software_trigger` facet;
  - **exact from a camera line:** the `hardware_trigger` facet, with that
    line among the camera's inputs.

  A mode or source the active camera cannot do is greyed out in the panel
  with the reason, before Start; the facet's own check still runs at Start.
  So a third-party frame camera with no trigger at all still captures
  snapshot runs. A contract test covers exactly that (9.1).
- **Fan-out:** one source fired two cameras through their software-trigger
  facets, 40/40 frames each at 20 Hz; with a 0 ms ready wait only 29-30 of
  40 triggers were fired (measured, synthetic).
- **The record gate** is filled only by the triggering plugin, one per
  camera per run; a gate that raises still saves the frame and marks it, as
  today (`capture.py:1527-1531`). Trigger settings are fixed while recording
  (`triggered.py:39-40`).
- **Where the trigger set-up is kept.** In a one-camera project it stays
  per camera, as today: the `capture.*` keys in that camera's preset
  (`triggered.py:42-46`, 81-88). M1 defines a rig preset only as "which
  preset goes on which camera" in `camera_rig.json` (multi-camera design
  2.5 and M1 row 4), so a rig-wide trigger set-up (one source firing
  several cameras) is new: a namespaced `"typhon.trigger"` block in
  `camera_rig.json`, an amendment to M1 row 4, about 0.5-1 day (estimate;
  P6 row 2). A test checks that a one-camera preset saved by today's
  Typhon still sets the trigger mode (9.4).

### 3.7 UI contribution points, including the pop-out panel

| Point | Where | Notes |
|---|---|---|
| Pop-out panel | its own window, created and titled by Typhon ("Classification — cam1") | The plugin supplies the inner widget on first open. For the classifier panel, building the window took 68-200 ms offscreen with PySide6 already loaded (measured; the interface prototype's tests); in a fresh process, importing PySide6, `frame_classes` and the picker first took a further 239 ms (measured; its start-up benchmark). The window remembers its place and hides on close; today's `PopOutWindow` deletes itself on close (`council_qt/widgets/pop_out.py:45`), so this is new |
| Toolbar button | the toolbar strip | The emitted `Toolbar` (`gui_emit_qt.py:2332`) accepted buttons added at run time (measured) |
| Status segment | the status strip | The emitted `StatusBar` (`gui_emit_qt.py:2305`) accepted labels added at run time (measured); `status()` is asked at most twice a second by the core's timer, within a budget |
| "Plugins…" entry | the Settings menu | `gui_settings.settings_menu` has a fixed item list (`gui_settings.py:122-146`) shared by every Designer app; it gains a small registration call |
| Shortcut | application-wide | F9 for Capture now; a second claim is refused |
| Canvas overlay | the live view | Reserved for velocity's arrows and the alignment overlay (section 11). A plugin gives its shapes in sensor coordinates; the core converts them to the picture shown, Downsample included (3.12) |
| Review source | the slider | Camera plugins only (the Prophesee raw view) |

- **Plugin settings go in the plugin's pop-out,** drawn by Typhon from the
  plugin's `settings_rows()` (3.5) where possible, not in new notebook
  tabs. The tab strip is already at its limit: one more tab (Capture) would
  have needed 441 px in the 431 px column at the smallest window, which is
  why the Capture group shares the Exposure tab today
  (`camera_categories.py:39-41`); today's tabs fit. A shared "Plugins" tab
  made the strip 437 px with a Basler and 491 px with an EVK4 (measured,
  offscreen; question 3).
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
  again only when it reuses it. NumPy then refuses to make a view of it
  writeable, which stops the common accident; a plugin that unlocks the
  frame itself is not stopped (estimate, from NumPy's rules; not measured).
- **What a `FrameView` says:** camera name, frame number, the saved file
  name or "not saved" (snapshot mode passes frames between triggers), and
  everything needed to turn its pixels into sensor pixels: the area,
  binning, decimation, mirroring, and the Downsample factor and method,
  because plugins see the full-size frame while the saved PNG, the live
  view and the boxes are reduced. It also offers a picture-to-sensor helper
  (3.12). The full field list is in 3.11.
- **No new frames-CSV columns in API 1.0.** The CSV header is written the
  moment the run name is reserved (`capture.py:416`), and readers take its
  columns by name (`capture.py:361-363`). A plugin's per-frame data go to
  its own sidecar CSV keyed by frame file name. The trigger columns stay
  where they are.
- **Offline work** (`analyse`) runs on a thread for short jobs and in a
  helper process for long ones, because `features()` is a per-file Python
  loop (`frame_classes.py:1432`); a helper took 4.1-4.2 s to start, mostly
  sklearn (measured). Classify all over 240 frames took 0.29-0.43 s
  (measured); done while another capture is recording, it would make 0-3
  frames late among the roughly 45-65 taken in that time, and classifying
  a long recording would contend for Python's lock for minutes (estimate;
  interface fact-check). So while a capture is recording, Classify all is
  offered only for short recordings; long ones are classified after Stop
  only.
- **Helper processes are started through `council_core/child_proc.py`**,
  which puts each child in a Windows Job Object that closes the whole
  process tree on a timeout or a crash (`child_proc.py:34-38`).
  Today it offers only a run-to-completion `run()` (`child_proc.py:401`); a
  long-lived start call for JSON-lines helpers is added beside it.
- **In-process only** in API 1.0: the record gate, the frame stage and
  live frames. If M1 row 9 puts busy EVK4s in helper processes, the
  Prophesee plugin owns them, started the same way (not prototyped).

### 3.9 Data namespaces

- **Presets:** the built-ins keep writing today's keys, `capture.*` and
  `save.downsample` (`triggered.py:81-88`), so presets stay readable by
  today's Typhon both ways (2.5). A third-party plugin's per-camera values
  ride in the camera's preset as `<id>/<key>`, plain values only (bool,
  str, 64-bit int, finite float; `camera_presets.py:248-253`). The core
  splits every plugin's keys off before `camera_settings.apply`, which
  otherwise reports them refused (measured). Keys of a plugin that is not
  loaded are kept untouched and listed as such. Rig-wide values go in the
  plugin's namespaced block in `camera_rig.json`, which is new to M1 (3.6).
- **Records: one rule.** Keys that format 1 already has stay where they
  are: `capture` and `downsample` in `<run>_camera.json`
  (`camera_record.py:121-135`, 171-174), and Downsample's block stays
  core. Keys that the velocity and multi-camera designs fixed stay where
  those designs put them: the top-level `"calibration": {id, name, stage,
  sha256}` pointer (velocity 3.9); the "rig" pointer and an EVK4's sync
  role and Trigger In state (multi-camera 2.6); the rig record's
  per-camera calibration ids (its 2.6, 4.12); and the plate definitions at
  the top of `camera_rig.json` (its 4.12). The `"plugins": {id: {...}}`
  block, at `format_version` 1, holds only new plugin data and third-party
  plugins' data, so no fact is ever kept in two places across old and new
  runs. Results known after the run go in `<run>_run_end.json` (M1:
  `<run>_rig_end.json`) or in sidecars, because the camera record is
  written once at Start. The core cleans and size-checks each block: in
  the prototype a NaN stopped the record being written, and a 14.3 MB
  block made it unreadable, over the 4 MB read limit
  (`camera_record.py:74`; measured).
- **Files: two services, never over anything.**
  - **(a) Streamed, during a run:** `ctx.create_file()` creates a new file
    exclusively in the active run's folders, for data written as frames
    arrive (the triggers CSV).
  - **(b) Whole, at any time:** `ctx.write_new()` writes the file whole to a
    temporary name of its own, then renames it into place with a rename
    that refuses an existing name; a rerun gets `_2`, `_3` and so on. It is
    allowed in any run folder you picked for analysis, past runs included,
    and never replaces anything. This is the velocity design's rule for
    `<run>_tracks.csv`, `<run>_track_points.npy` and `<run>_velocity.json`
    (velocity section 4) and the multi-camera design's for
    `<run>_registration.json` (its 6.4), as `camera_record.write` already
    does.
  - **Names.** Built-in plugins keep the names their designs fixed:
    `_capture_triggers`, `_calibration`, `_tracks`, `_track_points`,
    `_velocity`, `_registration`. A third-party plugin's files are named
    `<run>_<short>_<what>`; the built-ins' short names are reserved, and
    each run's camera record lists every active plugin's short name with
    its id, so a sidecar of an old run is never put down to a later plugin
    that took the same short name. Every suffix is registered, so the run
    name counts as taken.
- **Project file** `<project>/typhon_plugins.json`: the project's own
  plugin list and setting overrides (4.2), written whole then renamed,
  under an OS lock and a temporary name unique to each writer. Two threads
  sharing one temporary name lost a block and failed 47 of 80 saves
  (measured); `camera_presets` already learned this lesson.

### 3.10 Errors

- **Every call into a plugin is guarded:** any exception, `SystemExit`
  included, switches that plugin off for the session ("faulted"), names it
  in the status line and appends the traceback to
  `<project>/plugin_errors.log`. A guarded call cost 0.39-0.41 µs against
  0.13-0.15 µs plain (measured).
- **On the recording path, a fault ends the recording cleanly** with the
  reason in `<run>_run_end.json`, as a failed write does since M0. The
  recording path is only the record gate, the frame stage and per-frame
  tags: carrying on after a fault there would leave half a run tagged or
  reduced and half not.
- **Start-time and end-of-run hooks are best effort.** `record_fields`,
  `on_run_started`, the calibration freeze (`<run>_calibration.json`) and
  `on_run_finished` never stop a capture: a fault there is reported in
  Start's summary or in `<run>_run_end.json`, the plugin is marked faulted,
  and capture carries on. This keeps the velocity design's rule that a
  calibration is "never a reason not to capture" (velocity 3.9) and the
  multi-camera rule that a calibration mismatch "never refuses Start"
  (its 4.1). Only `check_start`, which runs before anything stops, refuses
  Start, and only with a reason.
- **Hangs:** a 2 s watchdog switches a stuck plugin off. That contains only
  a hang that releases Python's lock: after a sleeping hang no frame was
  late, after a pure-Python hang 346 of 587 were (measured, synthetic).
- **Crashes:** `os._exit` and a crash inside an SDK cannot be caught. A
  crash marker around a plugin's first load and first camera open lets the
  next start ask whether to keep it on (QGIS does this, vendor/project
  docs); a built-in camera plugin is never switched off without asking.
- **A crash during a recording** is outside the crash marker's reach. The
  next start finds the run with no `<run>_run_end.json`, flags it as
  interrupted, and lists the plugins that were active in it (from its
  camera record), without blaming any one of them.
- **Helper processes** are started through `child_proc` (3.8), so one that
  hangs or outlives Typhon is closed with its whole process tree.
- **A missing or incompatible plugin** never stops Typhon starting: it is
  listed with its reason, its features say "needs plugin X", and its data
  are kept untouched.

### 3.11 What a plugin author needs

This is the outline of the author's guide that ships with API 1.0 at stage
P5 (row 2). Everything in it is a design choice unless labelled.

**The folder.** One folder per plugin, normally named after its id with
dots turned into underscores (`acme_thermal/`), holding `plugin.json`, the
Python package the `entry` names (`plugin.py` or a subfolder with
`__init__.py`), optional data files, and optional `tests/`. No
`__pycache__`, `.pyc`, `.pyd` or `.dll` files (4.5). The folder is what
the user drags into Typhon's plugins folder, `<plugin home>/plugins/`,
normally giving `~/.council/typhon/plugins/acme_thermal/` (4.1); Typhon
takes the plugin's package name from the manifest's `id`, not from the
folder's name, so a folder dragged in under another name still loads.

**The manifest, in full.**

| Field | Type | Required | Notes |
|---|---|---|---|
| `schema` | int | yes | 1 |
| `id` | str | yes | reverse-domain, lower case, dots; `typhon.` is reserved for built-ins |
| `short` | str | yes | 2-12 lower-case letters or digits; unique; built-ins' reserved |
| `name` | str | yes | shown on screen |
| `version` | str | yes | `major.minor.patch` |
| `api` | str | yes | lowest plugin API it needs, `"1.0"`; below 2.0 is implied |
| `kind` | str | yes | `camera`, `source` or `feature` |
| `scope` | str | features | `camera` (one instance per camera slot) or `rig` (one per window) |
| `entry` | str | yes | `module:object` inside the folder |
| `author`, `description`, `licence` | str | third-party | shown in the Plugins window |
| `needs` | list of str | no | capabilities every use needs; `a\|b` means either (3.1) |
| `python` | list of str | no | requirement strings such as `"numpy>=1.24"`, checked in the project's interpreter, never installed |
| `contributes` | list of str | no | third parties may use `panel`, `toolbar`, `status`, `run_hooks`, `analyse`, `review_source` (camera plugins), `overlay` (once built); `record_gate` and `frame_stage` are built-in only |
| `files` | list of str | no | run-file suffixes, `_<short>_<what>.<ext>` for third parties |
| `uses` | list of {id, required} | no | other plugins (11.5) |
| `network` | str | no | `none` (default), `camera-link` or `internet` (4.6) |
| `budget_ms` | number | no | time per live call; default 5 |

**How many instances, and when.**

- A `scope: rig` feature has one instance per Typhon window; a
  `scope: camera` feature has one per camera slot, which is why its window
  is titled "Classification — cam1". A camera plugin has one instance per
  window and opens one `Device` per slot.
- The constructor takes no arguments; `activate(ctx)` follows. The context
  of a `camera` feature is tied to its slot.
- Connect and Disconnect of a slot's camera send `camera_changed`; the
  instance lives on. Switching the active camera sends `camera_changed` to
  rig features and to the panels on show. Closing the window calls
  `deactivate()` on every instance, after any recording has stopped.

**Which thread runs what, and for how long.**

| Hook | Thread | Time limit |
|---|---|---|
| `activate`, `deactivate`, `check_start`, `settings_rows`, `panel`, `toolbar`, `status` | the UI thread | 50 ms, `status` 5 ms; the watchdog switches off a call over 2 s |
| `on_run_started`, `record_fields`, `on_run_finished` | the thread running Start or Stop | 500 ms each; best effort (3.10) |
| `on_frame` | the plugin's own feed thread (3.8) | `budget_ms`; 25 % duty cap |
| record gate, frame stage | the grab thread and writer threads | built-in only |
| `analyse` | a worker thread, or a helper process for long jobs | none; `progress` can cancel it |
| a trigger source's `fire` callback | the source's own thread | must return at once |

A panel is updated from `on_frame` or `analyse` only through a Qt signal
or `ctx.call_in_ui(fn)`, never by touching a widget from another thread.

**The types.** Each is a frozen dataclass or protocol in `typhon_plugin_api`:

- `RunPlan`: the run name to be, each camera slot with its mode, area,
  Downsample and the trigger set-up, the capture folders.
- `RunInfo`: the run name, its folders by camera, start time, and at the
  end the counts from `<run>_run_end.json`.
- `FrameView`: `camera`, `index`, `image` (a read-only numpy array:
  `uint8` or `uint16`, shape `(h, w)` or `(h, w, 3)`), `timestamp`
  (the camera's own clock, **not always microseconds**: a Basler gives its
  tick count, a boA5320 gives 0, `cameras.py:213-218`), `host_us`, `saved`
  (the file name or None), `meta` (read-only: `aoi`, `format`, and the
  coordinate fields of 3.12) and `to_sensor(x, y)`.
- `CameraHandle`: name, `CameraInfo`, capabilities, `facet(name)`.
- `CameraInfo`: today's (`cameras.py:232-247`: backend, key, model,
  serial, vendor, kind).
- `Action`: a toolbar button's text, tooltip, optional shortcut and
  callback. `Progress`: `report(done, total, text)` and `cancelled`.
- `TriggerEvent`: the source's name, a running number and the host time.
- `Guide`: install steps as text, and checks, each returning
  `(ok, message)`. `CategoryHints`: rows for `SHARED_TABS`, `TIPS` and
  `MOST_USED` (2.4 item 9). `ReviewSource`: how the slider shows one of
  the plugin's run files.
- The facets, with today's methods as their signatures
  (`cameras.py:491-535`): `SoftwareTrigger.arm(setup) -> dict`,
  `ready(timeout_s) -> bool | None`, `fire()`, `disarm(armed) -> list[str]`;
  `HardwareTrigger.inputs() -> CaptureOptions`, `arm`, `disarm`,
  `count_edges(armed, seen) -> int`, `count_from_now`, `count_to_now`;
  `Binning.can_bin(factor) -> bool`, `apply(factor)`.

**Today's classes that become public API** through re-export, and so fall
under the version rule from 1.0: `Device` and `Backend`
(`cameras.py:451`, 616), `Frame` (196) and the `FramePool` rule that an
array is reused only once nothing else holds it (391-404), `CameraInfo`,
`Roi` and `Limits`, `Provider` (`camera_settings.py:492`), `Setting` (99),
`Change` (178), `CameraError`, `NeedsStop` and `GrabFailed`
(`cameras.py:103-139`), `SettingError` (`camera_settings.py:90`), and the
GenICam helpers (2.1). For a camera plugin: `read(timeout_ms)` is called
only from the grab thread, returns a `Frame` that owns its array or None
after the timeout, and must return within the timeout plus 100 ms;
`start_raw` and `stop_raw` (`cameras.py:537-548`) are called only around a
recording, `start_raw` before `start()` where the camera allows it.

**Contribution points open to third parties:** pop-out panel, toolbar
button, status segment, run hooks, `analyse`, review source (camera
plugins), and the canvas overlay once built (11.5). The record gate and the
frame stage are built-in only, because they sit inside the data-safety
rules.

**The environment.** Python 3.11 (the Council's floor) in the project's own
interpreter; PySide6, of which panels may use `QtCore`, `QtGui` and
`QtWidgets` only (6.10.2 in the `council` environment, measured); the
app's font, Arial 11 in today's wireframe (measured from
`examples/gui/typhon.gspec`); a pop-out must fit at the smallest window,
1400 x 820 (the wireframe's minimum), and at 1504 x 1016.

**Optional features of later minor versions** are detected with
`typhon_plugin_api.has("name")`, never by comparing version strings.

**The house rules,** checked by the contract suites where they can be:
offline only (nothing sent from the PC unless you opt that plugin in,
4.6); no telemetry; nothing downloaded or installed; no pickle; never
write over or delete user data (the file services of 3.9 only); no
`QSettings`.

**Running the suites outside the repo.** With a Council checkout on
`PYTHONPATH`, `python -m typhon_plugin_api.check <plugin folder>` runs the
plugin's kind's contract suite and its own `tests/` against the fake host
(9.2), offscreen, with `COUNCIL_SIM_CAMERAS` choosing the simulated cameras
and `TYPHON_PLUGIN_HOME` and `COUNCIL_VAULT_ROOT` pointing at temporary
folders.

**Updating a plugin.** A new version is a new folder dragged in over the
old one, or changed files. It is **not** approved again: the user approves
each plugin once (4.6, 4.7), so an update runs at the next start without
a question. Projects need nothing: a project names a third-party plugin by
id, and each run records the version and sha256 that ran (4.2). One
version of a plugin is installed at a time, and two folders holding the
same id are both refused until one is taken out (4.1); going back to an
older version means dragging its folder back.

### 3.12 Coordinates for plugins

The velocity and multi-camera designs fixed one coordinate rule
(velocity 3.8, multi-camera 4.13): full-sensor, unbinned pixels, with
pixel (x, y) centred at index (x, y), and conversions for area, binning,
decimation and mirroring. They predate Downsample. The code already defines
its step: a saved pixel x covers camera pixels x·f .. x·f + f − 1 from the
area's origin (`reduce.py:156-165`, `sensor_position`;
`camera_record.py:128-135`).

- **Everything a plugin stores or draws is in those sensor coordinates:**
  record blocks, sidecar files, canvas overlays, calibration points, boxes
  sent to another plugin.
- **`FrameView` carries every term** of the conversion: area, binning,
  decimation, mirroring, and the Downsample factor and method
  (`typhon_block_mean` or `camera_binning_average`, `reduce.py:53-54`). Its
  `to_sensor()` re-exports `reduce.sensor_position`. With camera binning as
  Downsample, the factor is the existing binning b and nothing is added;
  with Typhon's averaging, the factor f is applied to the picture pixel
  first and the area origin added after, as `sensor_position` does.
- **Amendments to the other designs,** listed here so they are not missed:
  multi-camera 4.13's conversion chain and its warp-table keys gain the
  Downsample factor and method, and the velocity calibration's
  `captured_with` (velocity 3.7) gains them too, so a calibration made on
  reduced frames is never applied to full-size ones without the
  conversion.
- **A test** puts a box and a calibration point on a frame averaged by
  Typhon and checks they map to the right sensor pixel (9.4).

---

## 4. Loading, enabling and trust

### 4.1 Where plugins live

| What | Where | Why there |
|---|---|---|
| Built-in plugins | in the Council checkout, `typhon_plugins/<name>/`, each with its `plugin.json`, and named in the built-in list shipped with the code | They are reviewed and tested with the Council, and updated with it. A folder there that is not on the list is refused, so a copied-in plugin cannot skip approval |
| Third-party plugins | `<plugin home>/plugins/<folder>/`: each plugin its own folder, which you drag in (your answer to question 7); loaded as the package `typhon_thirdparty.<name>`, where `<name>` is the manifest's id with dots turned into underscores (for example `typhon_thirdparty.acme_thermal`), whatever the folder is called | One folder you can see; Typhon never downloads, unpacks or installs anything. A separate package root never collides with the built-in `typhon_plugins` package |
| The app-wide base set | `<plugin home>/base_set.json`: the list of plugins and each plugin's default settings | Its list is what every new Typhon project on this PC starts with (your answer to question 1; 4.2); its default settings are read by every project (question 14); the level you work at |
| Approvals | `<plugin home>/approvals.json` | Once per plugin, for the whole PC (your answer to question 7): one entry per plugin id, holding the version and every file's sha256 as they were when you approved, kept for information, never to ask again |
| A project's own choices | `<project>/typhon_plugins.json`: the project's plugin list, copied from the base set when the project was made and changed after that only by you, and setting overrides | Travels with the project; names plugins, never holds code |
| A plugin's own data for one project | `<project>/plugin_data/<id>/` (no `.py` files) | Kept apart from the project's code |

The **plugin home** is `~/.council/typhon/`, or the folder the
`TYPHON_PLUGIN_HOME` variable names.

- **Plugins arrive as folders you drag in** (your answer to question 7).
  "The plugins folder for the app" is read here as Typhon's one plugins
  folder on the PC, `<plugin home>/plugins/`, not a folder inside each
  project, which the next point rules out. Settings → Plugins… has an
  "Open plugins folder" button that opens it in Explorer, so a plugin's
  folder can be dragged there from a USB stick, an unpacked zip or a git
  checkout. Typhon reads only folders: anything else there (a zip, a loose
  file) is listed as "not a plugin folder" and left alone. A folder dragged
  in is found at the next start or when the Plugins window is opened. The
  folder's name does not matter; the manifest's id does. Two folders
  holding the same id are both refused, and listed with both folder names,
  until you take one out; Typhon never removes either itself.
- **Never plugin code inside a project folder.** The policy gate checks every
  `.py` file under a project, subfolders included, on Generate and Run
  (`gui_policy.py:490-529`). A plugin under `<project>/plugins/` that
  imported `subprocess` or `ctypes`, as camera code does, blocked the whole
  app (measured), and one in a folder whose name starts with a dot was
  silently skipped (measured; `gui_policy.py:502`), which is a loophole,
  not a feature. VS Code reached the same rule from the other side: opening
  a folder must never run code that came inside it (vendor/project docs,
  Workspace Trust). So a project names its plugins by id only, and each
  run records the version and, for third-party ones, the sha256 of what
  actually ran (4.2).
- **Plain JSON files, no Council settings.** A generated app runs as its own
  process, often without the Council open, and may not use `QSettings`
  (`gui_policy.py:99`). Every list is a JSON file written whole to a
  temporary file unique to the writer, then renamed, under an OS lock, as
  `camera_presets.json` is.
- **One place, recorded home-relative.** The classifier store moves with
  `COUNCIL_VAULT_ROOT` when an app is launched from the Council
  (`gui_runner.py:287` copies the environment; `frame_classes.py:712-718`
  reads it) but not when it is double-clicked, so one app can see two
  stores (code; prior-art fact-check). To avoid that here, a project's
  `typhon_plugins.json` records the plugin home it was made with, written
  home-relative (`"~/.council/typhon"`), so the same project still finds it
  on the bench PC or under another user account, whether it was started
  from the Council or double-clicked. `TYPHON_PLUGIN_HOME`, when set,
  overrides the recorded folder: tests set it, as they set
  `COUNCIL_VAULT_ROOT`, and the plugin checkout's Council sets it for its
  side vault (7.1), so projects made there record the side vault's plugin
  home. If the recorded folder is missing, Typhon warns, names it, and
  falls back to `~/.council/typhon/`, never creating a second base set
  silently.

### 4.2 The app-wide base set and a project's own list

The app-wide `base_set.json`, in the plugin home:

```json
{ "format": "typhon-base-set", "format_version": 1,
  "plugins": ["typhon.basler", "typhon.prophesee", "typhon.simulated",
              "typhon.trigger", "typhon.classify", "typhon.timing",
              "typhon.crop"],
  "settings": { "typhon.classify": { "show_after_stop": true } } }
```

A project's `typhon_plugins.json`, here one made from that base set and
then given a third-party plugin:

```json
{ "format": "typhon-plugins", "format_version": 1,
  "base": "~/.council/typhon",
  "plugins": ["typhon.basler", "typhon.prophesee", "typhon.simulated",
              "typhon.trigger", "typhon.classify", "typhon.timing",
              "typhon.crop", "acme.thermal"],
  "settings": { "typhon.classify": { "show_after_stop": false } } }
```

**Decided** (your answer to question 1, "Only new projects"): the base set
is copied into a project when the project is made, not followed after
that.

- **What a project runs** = its own `plugins` list, and nothing else.
- **The base set** starts as all built-in plugins. You change it in the
  Plugins window ("for new projects"). A new project starts with it,
  which is your answer 2.
- **A project's list is copied from the base set once:** the first time
  the plugin Typhon opens a project that has no `typhon_plugins.json`,
  which for a new project is when it is made and first run, and for an
  older one is when it is moved across at the switch-over (7.5). The file
  is created, never written over; after that the list is the project's
  own, and only you change it, in that project's Plugins window.
- **A change to the base set reaches only projects made afterwards.** A
  plugin you add to the base set is not turned on in any existing
  project; each keeps its list until you add the plugin there. Taking a
  plugin out of the base set likewise leaves existing projects as they
  are. So that adding one is easy, a project's Plugins window marks each
  base-set plugin the project does not have, "in the base set, not in this
  project", with a tick box to add it here.
- **What a plugin's settings are** = the base set's defaults, with this
  project's `settings` laid over them key by key. A project that changed
  nothing follows every later change of a default. Your answer to
  question 1 was about which plugins a project has; whether default
  settings should also reach only new projects is part of question 14,
  still open.
- **The camera set-up joins the list.** Today `camera_setup.json` holds one
  choice, `basler` or `prophesee` (`camera_setup.py:41`, `FORMAT` 1 at 47).
  When an older project's list is copied, the camera family it chose is in
  it even if the base set lacks it, and `FORMAT` 1 files keep being read,
  so an old project is not asked its set-up questions again.
- **Each run records what made it:** the id, version and short name of
  every active plugin, and the sha256 of third-party ones, in the camera
  record's `plugins` block. This is new data, so it belongs there under
  the one rule of 3.9; the short names let an old run's sidecar files be
  put down to the right plugin later.

### 4.3 A plugin's states

The Plugins window shows each plugin in one state, with the reason:

- **discovered**: its manifest was read;
- **incompatible**: wrong API major version, a bad manifest, a Python
  package missing from the project's interpreter, or a second folder
  holding the same id (4.1);
- **needs approval**: third-party, and its id has never been approved on
  this PC (a later version or a changed file does not bring this state
  back, 4.6);
- **on**: in this project's list; **off**: not in it;
- **active**: its code has been imported because a feature was used;
- **faulted**: it raised an error and is off until the next start;
- **asked after a crash**: its crash marker survived (section 3.10).

**Where you see a plugin's code.** Settings → Python Scripts lists the
app's own files and the Council modules it links to (`gui_settings.py:275-345`,
`scripts_in_use`), so it would show built-in plugins once loaded but never
third-party plugins in the plugin home, which sit outside both the app and
the Council folders (code; seams fact-check). The Plugins window therefore
lists every file of every plugin with its size and sha256, and opens any of
them read-only; for plugins it takes the place of Python Scripts.

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
  and the classification panel's first open in a fresh process, 239 ms
  for importing PySide6, `frame_classes` and the picker and creating the
  Qt application (measured; the interface prototype's start-up benchmark;
  building the window itself took a further 68-200 ms, 3.7). The plugin host
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
- **As a package, without touching `sys.path`** (5.6-16 ms per plugin,
  measured); files inside a plugin import each other with relative
  imports. Built-in plugins are `typhon_plugins.<name>`, and only those on
  the built-in list load (Terms). Third-party plugins get a separate
  package root, `typhon_thirdparty.<folder>`, where `<folder>` is the id
  with dots turned into underscores (`acme.thermal` →
  `typhon_thirdparty.acme_thermal`): a dotted id cannot be a package name
  as it is, and it must never collide with the built-in package.
- **The bytes that were hashed are the bytes that run.** A `.pyc` file
  planted in a plugin's `__pycache__` ran instead of the approved source
  while the source's sha256 still matched (measured). Approval is once per
  plugin (4.6), so this no longer guards an approval; it keeps each run's
  record true, because the sha256 a run records (4.2) is of the code that
  ran. So for third-party plugins (built-in plugins load like the rest of
  the Council's code):
  - **one importer per plugin package** serves its modules, sub-modules
    included, from the bytes read and hashed at load time. Python's normal
    file finder never sees the package, because it would re-read the disk
    for every relative import and read and write `.pyc` files; this
    importer compiles exactly the checked bytes and writes no bytecode;
  - **data files** (models, tables, JSON) are read through `ctx.read_data()`,
    which checks each against the sha256 taken when the plugin loaded and
    refuses one changed while Typhon is running; the changed file is used
    from the next start, without asking (4.6). A plugin that opens its
    files directly is breaking a house rule (3.11);
  - **every file in the folder is hashed** at load, not only `.py`, and a
    folder holding `__pycache__`, `.pyc`, `.pyd` or `.dll` files is
    refused;
  - **a test** changes a sub-module and a data file after the plugin has
    loaded, and checks that in that session the sub-module still comes
    from the bytes read at load and the data file is refused; and that at
    the next start both load without an approval question and the run
    record carries their new sha256 (9.1).

  Native code comes only from packages you install into the environment
  yourself (pypylon, OpenEB).
- **Python packages a plugin needs** are checked in the project's own
  interpreter (`python_envs`), never installed. A missing package switches
  that plugin off with a hint and the app starts, where today a missing
  `requires` entry such as sklearn stops the whole app
  (`gui_emit.py:3212`, `raise SystemExit(3)`).

### 4.6 Plugins written by others: trust

**What to say plainly, on screen and here:** an approved plugin is ordinary
Python running with Typhon's rights, whatever its manifest says. It can:

- read, write over and delete your files (`open(..., "w")`, `os.replace`,
  `shutil.move`, `os.remove`), and undo a read-only flag with `os.chmod`;
- make a frame's pixels writeable again (`setflags` on the array's
  `.base`, 3.8) and change them before they are saved;
- use the network, including through `asyncio`, `socketserver`, `xmlrpc`
  or `ssl`;
- start programs (`subprocess`, `os.startfile`, `webbrowser`,
  `ProcessPoolExecutor`);
- end Typhon at once (`os._exit`), with no recording cleanly closed;
- reach the main window, other plugins and the camera's `Device`.

(The research measured the overwriting, `os.chmod`, and a plugin reaching
the main window and the `Device`; the rest follows from Python's and
NumPy's own rules, vendor/project docs.)

**Plugins are not checked by the policy gate at all.** They load on the
Council side (4.5), and the gate checks only a generated project's own
files. The risky-pattern scan of step 2 is a regex speed bump. The steps
below protect against mistakes and make you look before trusting; they do
not fence a plugin written to do harm.

1. **You drag the plugin's folder** into the app's plugins folder, the
   plugin home's `plugins/`, normally `~/.council/typhon/plugins/` (your
   answer to question 7). Each plugin is its own folder; the Plugins
   window's "Open plugins folder" button opens it for you (4.1). Typhon
   never fetches, unpacks or installs one.
2. **The Plugins window lists it as "needs approval"** and shows its
   manifest (author, version, what it attaches to, files it writes, network
   use), every file with its size and sha256, the Python packages it needs
   and whether the project's interpreter has them, and a scan for risky
   patterns. The scan is a regex speed bump: it builds on
   `council_core/ide_trust.py`, which already keys trust on sha256 and
   calls itself "a speed bump, not a sandbox" (`ide_trust.py:14-24`),
   rather than on the policy gate, which would flag Typhon's own camera
   code (`frame_camera` imports `ctypes` at 2537, `socket` at 2795 and
   `importlib` at 3464).
3. **You approve it once, for the whole PC** (your answer to question 7:
   "Approval only happens once for each plugin"). The approval is keyed by
   the plugin's id and is never asked for again: not for a later version,
   not for a changed or added file, not for a folder dragged in over the
   old one. `approvals.json` keeps the version and every file's sha256 as
   they were when you approved, for information only. Approval lets it
   run; where it runs follows 4.2: adding it to the base set puts it in
   every project made afterwards, adding it to one project turns it on
   there only.
4. **Network:** the manifest says `none`, `camera-link` (a GigE camera on a
   local network) or `internet`. `internet` is refused unless you opt in
   for that plugin in the Plugins window, by the standing rule that nothing
   leaves the PC unless you choose it. A Python audit hook watches
   connections to anything but this PC or a declared camera network. It
   is one hook for the whole process, so it sees core code too (Typhon's
   own `frame_camera` uses `socket`, `frame_camera.py:2795`), and it can
   tie a connection to a plugin only by looking at the call stack. It is a
   tripwire for mistakes only; a camera SDK's own native traffic never
   reaches it.
5. **Its contract suite runs after you approve it,** and the result is
   shown as information, never as proof of safety. Running it before
   approval would run code you have not approved; the Council's fenced test
   runner blocks PySide6 and does not fence `ctypes`
   (`gui_smoke.py:29-32`), so panels could not be tested there anyway.
6. **Each run names the plugins and versions that made it** (4.2).

**The trade-off of approving once, said plainly.** Your rule is the
design: a plugin is approved once, and that approval stands. What it
costs:

- **A plugin folder later replaced or edited is not asked about again.**
  If a plugin's files are changed, by its author's update, by you, by
  another program or by mistake, the new code runs at Typhon's next start,
  with every right listed at the top of this section, and you are not
  shown it first.
- **The approval follows the id, not the author.** A different plugin
  that uses an approved plugin's id, dragged in over it, also runs without
  a question.
- **What still holds:** Typhon never downloads or installs anything; a
  new plugin id always needs your approval; native files (`.pyd`, `.dll`)
  and bytecode are still refused (4.5); a plugin whose manifest newly
  declares `internet` is still refused until you opt it in, because that
  opt-in is the standing offline rule, not the approval (step 4); and
  each run still records the version and every file's sha256 that ran
  (4.2), so what ran can always be told afterwards.

**Open suggestion, not a decision:** the Plugins window could still show
"changed since you approved it" beside a plugin whose files no longer match
the sha256s kept in `approvals.json`, listing the files that differ. It
would be information only: it would never block the plugin, never send it
back to "needs approval" and never ask a question, so it keeps your rule.
It would add almost nothing at start-up, since every file is already
hashed at load (4.4, 4.5; estimate). Whether to have it is yours to say
(question 7).

*Footnote, about generated apps, not plugins:* the research also found what
the policy gate lets through in a generated project's own code, measured
with snippets that passed it: writing over files (`open(..., "w")`,
`os.replace`, `shutil.move`, `Path.write_bytes`), `os.chmod`, `os._exit`,
network access through `asyncio`, `socketserver`, `xmlrpc` or `ssl`,
`ProcessPoolExecutor`, `os.startfile` and `webbrowser`, and Council
internals reached through `frame_camera`'s attributes. These are gaps for
every generated app and are worth fixing on their own; they say nothing
about plugins, which the gate never checks.

**An optional extra guard against accidents:** mark each saved frame and each
finished CSV read-only on the writer threads, 0.23-0.27 ms per file
(measured). Afterwards writing, appending, replacing and deleting all
failed; `os.chmod` undoes it, and Windows asks before deleting read-only
files (question 9).

### 4.7 Versions and updates

- **Built-in plugins** update with the Council checkout and carry its
  version.
- **Third-party plugins** are approved once per plugin, not per version
  (your answer to question 7). An update is a new folder dragged in over
  the old one, or new files; it runs at the next start without asking
  (4.6 says what that costs). Projects need nothing: they name the plugin
  by id, and each run records the version and sha256 that actually ran
  (4.2, 3.11).
- **The plugin API's major version** is checked at scan time; a plugin for
  another major version is listed as incompatible, with both numbers.
- **Old runs stay readable** after a plugin is removed: its record blocks
  and sidecar files are kept and shown as "written by plugin X 1.2, not
  installed", using the short-name list in that run's camera record (3.9).

---

## 5. How multi-camera M1 merges in

### 5.1 One refactor, not two

M1 replaces the one global camera with a `Rig` of camera slots (multi-camera
design 2.2). The plugin system needs a host object that owns cameras, runs
and plugins. They are the same object, so they are built once:

- **The `Rig` is the plugin host.** It holds the registry, the project's
  plugin list, the one timer, the toolbar and status strips, the pop-out
  windows, the run name, and Start and Stop with the trigger slot of 3.6.
- **A `CameraSlot` is one camera from one camera plugin.** It holds the
  `Device` its plugin opened, the session, folder, run, status, background
  job and lock (as M1 plans), plus the camera's capabilities and facets and
  a dictionary of per-plugin state.
- **Five M1 rows are the same edits the plugin core needs:** row 1 (the
  `Rig`, slots and the `_LIVE` view, 4.5-7 days), row 4 (several camera
  families in the set-up, 2-3), row 5 (settings following the active camera,
  2-3), row 7 (the wireframe, 1-2) and row 8 (N simulated cameras and test
  fakes, 3-4): 12.5-19 of M1's 24-37 days (project docs, estimates;
  `docs/typhon_multicamera_design.md:2197-2216`). Built separately, each of
  these files would be rewritten twice.

### 5.2 What the slot hands the settings views

The settings tabs, settings windows and preset picker take an `api` object
(`settings_tabs.py:120`; `frame_camera.py:2384`, 2412). Between them they use
18 functions (measured with `grep` at 393671b8): `on_camera_change`,
`camera_state`, `settings_list`, `set_camera_setting`, `reset_setting`,
`camera_defaults`, the seven preset calls (`list_presets`, `save_preset`,
`apply_preset`, `delete_preset`, `rename_preset`, `export_preset`,
`import_preset`), `reset_camera_settings`, `load_camera_defaults`,
`current_area`, `set_camera_area` and `full_frame`. Two more names are read
with a default, `START_BOXES` and `box_shows_camera`
(`settings_tabs.py:783`, 793). **A slot provides all 20.** Leaving out the
last two would raise no error but would silently break the link between the
boxes beside Start and the settings tabs. Today the `api` is the whole
`frame_camera` module, `_LIVE` included, so a narrow slot object is new work,
not a rename (code; prior-art fact-check).

### 5.3 Which pieces sit on the rig and which on a slot

| On the rig (one per window) | On each slot (one per camera) |
|---|---|
| Plugin host, registry, base set and project list | The `Device` from its camera plugin; capabilities and facets |
| Toolbar strip, status strip, "Plugins…" entry, pop-out windows | Its settings provider, settings tabs when active, presets |
| Run name and reservation, `<run>_rig.json`, `<run>_rig_end.json` with plugin blocks | Its run folder, `<run>_camera.json` and `<run>_run_end.json` with plugin blocks |
| Start and Stop order, the trigger slot, rig-scoped hooks | Its record gate, live-frame feed, per-plugin state |
| Rig-scoped features: triggering several cameras, later alignment | Camera-scoped features: classification and timing scan on the active camera's folder |

- **Camera-scoped features follow the active camera.** The multi-camera
  design already re-points the slider, Classify all, the timing scan and
  cropped export at the active camera's folder at every Start and camera
  switch (its 2.7). The plugin context's `capture_folder()` and
  `camera_changed` event (3.5) are how those plugins hear of it.
- **The trigger work's seven fields** on `_Live` (`frame_camera.py:327-370`)
  become per-plugin state on the slot. `_LIVE` stays as M1's compatibility
  view, and its test that every field of `_Live.__init__` resolves through
  the view now covers 49 fields, not 42 (measured: 42 at 67cad625, 49 on
  the trigger branch).

### 5.4 M1's rows in plugin terms

| M1 row (estimate) | What changes because of plugins |
|---|---|
| 1. `Rig`, `CameraSlot`, `_LIVE` view (4.5-7 days) | The rig is the plugin host; slots carry per-plugin state; the slot provides the 20-name `api` |
| 2. One save budget and writer pool (2-3) | None for plugins; Downsample, the one frame stage, runs on that pool |
| 3. Run layout, reservation, rig records (2.5-3.5) | Plus the run-file suffix registry and `plugins` blocks in rig records |
| 4. `camera_rig.json`, several camera families, wizard check boxes (2-3) | The families are the enabled camera plugins; each brings its guide, checks and (Basler) scan into the wizard. Built in stage P2 with the camera plugins. Amended: `camera_rig.json` also holds the triggering plugin's namespaced rig-wide set-up (3.6), about 0.5-1 more day (estimate; P6 row 2) |
| 5. Settings, presets and windows follow the active camera (2-3) | Through the slot's `api`; window keys per camera |
| 6. Tile strip, grid, live pop-outs (3-5) | Stays core; plugins add status segments, not tiles |
| 7. Wireframe update (1-2) | One new wireframe with M1's ports and the plugin strips together, so you run Update from example once (section 6) |
| 8. Test fakes, N simulated cameras (3-4) | The Simulated plugin, with `COUNCIL_SIM_CAMERAS` (the multi-camera design's name, its 2.10; the prototype used another), is the reference plugin |
| 9. Busy EVK4s: one program each, or OpenEB's C++ slicer (3.5-5.5) | Inside the Prophesee plugin; if helper processes, the plugin owns them (3.8) |
| 10. Software-trigger fan-out (0.5-1) | Done by the triggering plugin through `SoftwareTrigger` facets: 40/40 frames on two cameras (measured, synthetic) |

### 5.5 What the multi-camera design said that has since changed

- **M0 is merged** (be071316, 12 October 2026; measured: `git log`),
  although that design still says "not yet merged"
  (`docs/typhon_multicamera_design.md:2166-2167`).
- **Its line numbers are from commit 6560fca.** For example it puts `_LIVE`
  at `frame_camera.py:323`; at 393671b8 it is at 373.
- **Triggered capture has landed** (393671b8). M1 row 10 and M2's Start and
  Stop order (its 2.3) must now drive `TriggerRun`'s books rather than fire
  triggers of their own; the record gate already exists (`gate=` in
  `CaptureSession.record_to`, `capture.py:1251`).
- **Its compatibility promise assumed the wireframe only grows** ("no
  edited handler warnings", its 2.9). The combined wireframe removes 53
  shapes, and today's Update from example blocks a project whose user
  edited a handler of a removed widget (measured). Section 6.4 adds the
  missing step.
- **Run reservation for a mixed folder.** A rig run is reserved by creating
  its folder; a one-camera run by creating `<run>_frames.csv` (its 2.6). The
  rig must treat both as taken, so today's Typhon (flat runs) and the plugin
  Typhon (folders) saving into one capture folder never pick one name. The
  interface prototype checked only folders (code; interface fact-check).

### 5.6 Order of the combined work

1. The `Rig` and slots behind `frame_camera`'s existing functions, with
   `_LIVE` as the compatibility view, so every existing test passes
   unchanged (M1 row 1).
2. The plugin registry, with Basler, Prophesee and Simulated as adapter
   camera plugins over the existing `Backend`, `Device` and `Provider`
   classes, which stay where they are (7.2); each camera plugin's set-up
   guide, checks and (Basler) scan page in the wizard, and
   `camera_setup.json`'s choice read as the project's plugin list (M1 rows
   4 and 8).
3. Run hooks and recorder extension points; the core stops calling
   triggering directly from `capture.py`, `cameras.py`'s base class and
   `frame_camera`, and calls the triggering plugin's hooks instead, the
   plugin itself an adapter over today's `triggered.py`.
4. UI contribution points and the new wireframe; the classification
   pop-out; the timing and crop plugins (M1 rows 5 and 7).
5. M1's multi-camera screen and recording (rows 2, 3, 6, 9, 10), then the
   switch-over, at which the built-in plugins' files finally move into
   their own folders (7.2).

Section 12 gives each step's estimate and what you see after it.

---

## 6. The wireframe and the Designer

### 6.1 A new example: `typhon_plugins.gspec`

The new app is a new example, `examples/gui/typhon_plugins.gspec`, titled
"Typhon (plugins)" and registered beside `typhon` in `gui_examples.py`
(`NOTES` at 66, `INTENDED_TOOLKIT` at 147, set to `qt`). Today's
`typhon.gspec` stays untouched until the switch-over, so the Typhon you use
keeps its window.

Today's wireframe has 93 shapes and 41 script links: 21 to `frame_camera`,
17 to `frame_classes`, 1 each to `frame_timing`, `frame_roi` and
`gui_settings` (measured from `examples/gui/typhon.gspec` at 393671b8).

| Leaves the main window | Shapes | Goes to |
|---|---|---|
| Classification: model, classes, mark, train, predict, classify all, predictions, filter, save as, rename, delete, export, import, tags | 32 (`s24`-`s40`, `s64`-`s78`) | The Classification pop-out |
| Timing scan: button, count, list | 5 (`s12`-`s16`) | The Timing scan plugin |
| Cropped export: output folder, button, status | 4 (`s19`-`s22`) | The Cropped export plugin; the crop box itself (`s17`-`s18`) stays |
| PNG / Raw | 1 (`s56`) | The Prophesee plugin's review source, as a toolbar button |
| Triggering and Downsample: Capture, From, Per trigger, Timer, Capture now (F9), Downsample, with their labels | 11 (`s82`-`s92`) | The triggering pop-out and toolbar; Downsample stays in the settings notebook (2.1) |
| **Total** | **53**, leaving 40 | |

| Added | Kind | Port |
|---|---|---|
| Toolbar strip | `toolbar` | `plugin_toolbar` (an event port) |
| Status strip | `status_bar` | `plugin_status` (a text port) |
| Camera chooser, "Add camera", the active camera's folder | combobox, button, label | M1's new ports (multi-camera design 2.2) |

- **Use the existing `toolbar` and `status_bar` shapes, not empty frames.**
  The seams research suggested plain frames, but a frame has no port
  (`gui_ports.py:195`), so `attach()`, which finds everything by port name,
  could find a frame only by the name generated from its label, the
  "identifier from display text" fault. The emitted `Toolbar` and
  `StatusBar` (`gui_emit_qt.py:2332`, 2305) have ports and accepted buttons
  and labels added at run time (measured, offscreen; seams fact-check).
- **M1's new ports go into the same wireframe,** so a project needs one
  Update from example, not two.
- **The settings column.** If plugin settings stay in pop-outs (3.7), the
  notebook keeps today's width. If you prefer a shared "Plugins" tab, the
  column must be at least 491 px at the smallest window (measured;
  question 3).

### 6.2 What stays the same in the Designer

- **The required ports** stay `live_view` and `capture_status`
  (`frame_camera.py:166`), and `frame_camera` stays a linked module that a
  smoke run fakes (`COUNCIL_SMOKE_FAKE`, `frame_camera.py:172`).
- **`app.py` is not touched,** and no emitter change is needed for the
  strips: both shape kinds exist (`gui_shapes.py:323`, 330).
- **Plugins are not linked modules.** The app never imports them, so
  `gui_policy.LINKED_MODULES` (`gui_policy.py:59-64`) stays as it is.
- **Adding or removing a plugin never changes `project.gspec`.** Update from
  example is needed only when the core wireframe changes, which is the
  point of your request: features stop reshaping the main window.
- **"Plugins…" in the Settings menu** (shape `s58`, linked to
  `gui_settings.settings_menu`). That menu's items are a fixed list
  (`gui_settings.py:122-146`) shared by every Designer app, Barbie and the
  image viewer included. It gains a small registration call, made by
  `frame_camera.attach()`, so only Typhon shows the entry.
- **Declared packages.** `typhon.gspec` declares numpy, PIL and sklearn, and
  the generated `main.py` refuses to start if one is missing
  (`gui_emit.py:3159-3212`). The new example drops sklearn; the
  Classification plugin declares it instead and is switched off with a hint
  if it is missing (4.5).

### 6.3 Plugin panels and the Designer

Plugin panels are built by the plugin's code at run time. That means:

- they are not drawn or edited in the Designer, and the Designer's layout
  checks do not see them; the UI contract tests (9.1) cover them instead,
  at both window sizes;
- the classification panel reuses today's `ClassifierPicker`
  (`council_qt/widgets/classifier_picker.py:155`) and `frame_classes`'s
  functions, so its behaviour is today's, in another window (the prototype
  built it as its own window in 68-200 ms offscreen, with PySide6 already
  loaded; measured, 3.7).

The other way, not prototyped, is a plugin panel described as a small
wireframe fragment (shapes plus script links into the plugin's functions)
that Typhon builds into a pop-out. It keeps Designer editing and the policy
gate, at an estimated 3-5 more days (estimate). Question 4.

### 6.4 Moving a project to the new example

What was measured (seams research and its fact-check, in temporary vaults
built from 67cad625):

- **Switching a project to a differently named example works** through the
  API and `run_example_gui.py`. A stand-in example (today's wireframe less
  its 32 classifier shapes) took a project from 82 to 50 shapes, wrote a
  dated backup of `project.gspec`, removed the generated handler stubs of
  removed widgets that had not been edited (copies kept in `.backups`), kept
  the user's own code in `handlers.py`, and recorded the new example's name
  (`council_core/designer_examples.py:602-621`, 718-820).
- **The Designer's Update button always uses the example the project was
  made from** (`council_qt/tabs/designer.py:913`). Switching needs
  `run_example_gui.py typhon_plugins --project <name> --update`
  (`run_example_gui.py:135`, 153) or a new "Switch to example…" choice.
- **An edited handler of a removed widget blocks the update.** With
  `on_btn_train` edited, the update returned "BLOCKED — port rename cannot
  proceed: port classifier_name is used in handlers.py". By then
  `project.gspec` (with a backup) and the recorded example name had
  already changed, but `ui/` had not been regenerated: the project was left
  half-switched, and the recovery message mentions only the backup, not the
  example name (measured).

So the update gets one fix now, and one more step later:

1. **Write nothing until it can finish, on `qt-migration` now.** The
   half-switched project is a data-safety fault in today's Designer, not
   plugin work, so it is fixed there as a bug fix and reaches the branch
   with the next merge (P0 row 2). The update checks first (ports,
   handlers, orphans) and writes nothing, neither `project.gspec` nor the
   example name in `manifest.example` (`designer_examples.py:771`), until
   it knows it can finish. If a write still fails partway, the recovery
   message names both the `project.gspec` backup and the changed
   `manifest.example`.
2. **Setting edited handlers aside, inside Update itself.** After the
   switch-over the new example becomes `typhon.gspec` (7.4), and existing
   projects reach it through the plain Update button, which uses the
   recorded example (`council_qt/tabs/designer.py:913`). So the step lives
   in the update path that both Update and "Switch to example…" use: it
   finds the handlers of widgets the new example removes that you have
   edited, and moves each, word for word, into `handlers_retired.txt`
   beside `handlers.py`, with a note saying which plugin now does that
   job. A text file, not `.py`: the policy gate parses every `.py` under a
   project (`gui_policy.py:490-505`) and fails one that does not parse
   (`gui_policy.py:316`), and a method body pasted at module level does not
   parse, so it would block the whole app. Nothing is deleted.
3. **Hand edits in `app.py` too.** `find_orphans` scans `app.py` as well as
   `handlers.py` (`gui_projects.py:609`, the loop at 630), so an `app.py`
   line that uses a removed widget or port also blocks the update. `app.py`
   is never rewritten (`gui_emit_qt.py:1470-1488`), so the step lists each
   such line with the plugin that now does its job and stops; with your
   yes it comments those lines out, word for word, after a dated backup of
   `app.py`.
4. **`typhon_classic` keeps building.** The old wireframe links
   `frame_camera.set_capture_mode`, `set_trigger_source`,
   `set_frames_per_trigger`, `set_timer`, `capture_now` and
   `set_downsample` (shapes `s83`-`s92`; measured from
   `examples/gui/typhon.gspec`), so those functions stay as facades over
   the triggering plugin and the core's Downsample for as long as
   `typhon_classic` is kept (7.4).

Projects that never edited those handlers or `app.py` lines switch with no
extra step.

---

## 7. Keeping current Typhon usable

### 7.1 Two Typhons side by side: how it works, and the traps

- **A project runs the camera code of the checkout that generated it.** The
  generated `main.py` writes in the absolute path of that checkout
  (`_APP_ROOT`, `gui_emit.py:3249`) and puts it first on Python's search
  path (`gui_emit.py:3256-3257`; measured in a temporary build). So a
  second checkout of the repo, on the plugin branch, builds a second app
  that runs the branch's code, while your current Typhon project keeps
  running `qt-migration`'s.
- **Trap 1: regenerating your current project from the plugin checkout.**
  If the plugin checkout's Designer ran Generate or Update from example on
  your current Typhon project, that project would silently start running
  plugin code. So the plugin checkout's Council runs with **its own vault**
  (`COUNCIL_VAULT_ROOT` pointing at a separate folder) and its own plugin
  home (`TYPHON_PLUGIN_HOME`, 4.1). Its Designer then never sees your
  current projects.
- **Trap 2: a checkout that disappears.** Every app generated from a
  checkout breaks if the checkout is deleted. The plugin checkout must sit
  at a stable path you chose (for example `C:/Users/<you>/Typhon-plugins/`,
  a git worktree on branch `typhon-plugins`), never under
  `.claude/worktrees/`, where tools clean up working copies on their own.
- **Trap 3: the classifier library depends on how the app was started.**
  The store defaults to `<vault>/classifiers`
  (`frame_classes.py:712-718`, 770), and an app launched from the Council
  inherits `COUNCIL_VAULT_ROOT` (`gui_runner.py:287`) while a
  double-clicked one does not. So the new project pins its store with a
  `classifier_store.json` beside the app (`frame_classes.py:105-106`, 451):
  either your current library, shared (two apps sharing a store is an
  intended case, with locks), or a copy of its own (question 10). If it is
  shared, the branch must not change the classifier file formats: today's
  Typhon refuses files "written by a newer build"
  (`frame_classes.py:1292`, 3840).
- **One program per camera at a time.** A second open of the same camera
  fails on real hardware, though the emulator allows it
  (`frame_camera.py:2856`). Close one Typhon's connection before the other
  connects.
- **Capture folders.** Both Typhons are past M0, so neither can overwrite
  the other's frames: runs are reserved by creating `<run>_frames.csv`
  exclusively and frames are written with exclusive creation (code). Use
  separate capture folders while trying the new app anyway, so a flat run
  and a rig run folder of the same name never confuse the slider (5.5).
- **Presets** are per project. The new project gets a copy of
  `camera_presets.json` when you want your presets there; the original is
  never moved.

### 7.2 The branch

- **Create `typhon-plugins` from `origin/qt-migration`** at 393671b8 or
  later. Its precondition, the trigger work being merged, was met on 15
  October 2026 (393671b8, 09:35; measured: `git log`). (The local
  `qt-migration` in the research worktree was 44 commits ahead of and 64
  behind `origin/qt-migration`, measured; the branch starts from `origin`,
  not from a local copy.)
- **Merge `qt-migration` into `typhon-plugins` every week,** and after any
  Typhon bug fix, by merging, not rebasing, so every fix can be traced.
- **Today's Typhon takes bug fixes only while the branch lives.**
  Decided (your answer to question 6, "Bug fixes only new features as
  plugins"): on `qt-migration`, today's Typhon takes bug fixes and
  nothing else (the rest of the Council carries on there as usual), and
  every new Typhon feature is built as a plugin on the branch, never on
  `qt-migration`. That includes velocity, calibration, alignment and the
  trigger cable (M2); the velocity design had planned to start from
  `qt-migration` (velocity 5.2; 11). A Typhon change that is not clearly
  a fix is asked about before it goes onto `qt-migration`. The reason:
  the seven core Typhon files took 31 commits, +5,265 and −1,172 lines,
  in the week from 7 October 2026 (measured; the larger "84 commits since
  15 September" includes creating those files). At that rate a branch
  that restructures them would conflict at every merge.
- **Plugins over the old code, not facades over new code.** Git does not
  follow code that is split across files: if `BaslerDevice`, `EvkDevice`,
  `BaslerSettings` or the trigger code moved into `typhon_plugins/<name>/`
  on the branch, every fix to `council_core/cameras.py`,
  `camera_settings.py` or `triggered.py` on `qt-migration` would merge into
  the branch's leftover file, where it conflicts or quietly puts code back.
  These files took 31 commits in a week (above). So while the branch lives,
  that code stays where it is in `council_core` and `frame_camera`; each
  built-in plugin folder is a thin adapter that reaches it through
  re-exports in `typhon_plugin_api`, which also keeps the rule that
  built-in plugins import only the API. A fix merged from `qt-migration`
  lands in the same file and lines on both branches. Files move physically
  only at stage P7, once today's Typhon on `qt-migration` is frozen. Your
  answer keeps it taking bug fixes, so the files stay in place until then,
  unless you later say it may take no fixes at all (the part of question 6
  still open). Facades are kept only where the code is new, such as
  `_LIVE` over the rig and the `frame_camera` trigger functions
  `typhon_classic` links (6.4).
- **The branch's gate for every commit:** every existing Typhon test passes
  (1,078 tests collected from 21 Typhon test files at 67cad625, measured,
  not run; plus the 79 test functions the trigger work added), plus the
  plugin contract suites (9.1), plus the "today's readers open the new
  runs" tests (7.5).
- **Barbie's tests in that gate.** `tests/test_barbie_v4.py` (15 tests,
  measured) and the Barbie cases in `tests/test_typhon.py` (87-131,
  426-446, 957-1006) stay in the gate as free checks on `frame_camera`'s
  functions until one fails. A Barbie test that fails on the branch is
  then retired at once, on the branch, through the `RETIRED` list
  (`tests/test_retired_tests.py:21`) and the "Retired tests" list in
  `CLAUDE.md`, with its reason: not fixed, and not left red. Where a test
  is shared with Typhon (the build test at 426-446 also takes `typhon`),
  only its Barbie parameters are dropped. The two pieces of coverage 8.3
  keeps are rewritten first if one of their tests is the one that fails.
- **Never pushed to `main` or `Work-Build`** (standing rule).

### 7.3 What you do day to day while it is built

- **Capture with today's Typhon,** from your `qt-migration` checkout, as now.
- **Try "Typhon (plugins)"** from the plugin checkout's Council: first with
  simulated cameras, then with a real camera while today's Typhon is not
  connected to it.
- **Report a bug in today's Typhon as usual.** It is fixed on
  `qt-migration` and reaches the branch with the next weekly merge.
- **Ask for a new Typhon feature as usual.** It is built as a plugin on the
  branch and reaches you in "Typhon (plugins)", not in today's Typhon
  (7.2).

### 7.4 The switch-over, when you say so

1. **Tag** the last commit before the merge (for example
   `typhon-classic-last`). A checkout of that tag runs the old Typhon and
   Barbie v4/v5 at any time, with no upkeep.
2. **Merge** `typhon-plugins` into `qt-migration`.
3. **The new example becomes `typhon.gspec`.** Existing projects then reach
   it through the plain Update button, with the set-aside step of 6.4
   inside it. The old wireframe may be kept for one release as
   `typhon_classic`, so an old project can still be rebuilt from it; while
   it is kept, `frame_camera`'s trigger link functions stay as facades
   (6.4). The built-in plugins' files move into their folders now (7.2).
4. **Move each project across** (7.5).
5. **Retire Barbie v4 and v5** and their tests (section 8).

### 7.5 Migrating projects, presets, runs and classifier libraries

| What | How | Changes your files? |
|---|---|---|
| A Typhon project's window | Update to the new example with the step of 6.4 inside it: edited handlers of removed widgets move to `handlers_retired.txt`, `app.py` lines that use removed widgets are listed (and commented out only with your yes), then the update | `project.gspec` (a dated backup is kept), `handlers.py` (moved parts kept word for word) and, only with your yes, `app.py` (a dated backup is kept) |
| `camera_setup.json` | Its one choice becomes "that camera plugin on" in `typhon_plugins.json`; `FORMAT` 1 files are still read (`camera_setup.py:47`) | No; the file is left as it is |
| Presets (`camera_presets.json`) | Unchanged; copied, never moved, into a new project. The `capture.*` and `save.downsample` keys (`triggered.py:81-88`) are read and written in place by their new owners, the triggering plugin and the core (2.5); keys of a plugin that is not loaded are kept and listed | No |
| Runs | Nothing is rewritten. A branch test opens a run written by the plugin Typhon with today's readers (`capture_review`, `event_playback`, `frame_classes`, `frame_timing`, `frame_roi` from `qt-migration`). It holds because new CSV columns go only at the end, the camera record stays at version 1, and new files have their own suffixes | No |
| Classifier libraries | Nothing to move if the store is shared (7.1); otherwise the existing export bundle | No |
| `app.py` | Untouched (`frame_camera.attach(self)` stays) | No |

**The real data is on the bench PC, not here.** This laptop has no Typhon
project, no `camera_presets.json`, no `camera_setup.json` and no
classifiers folder (measured: searched the user folder six levels deep), and
your main checkout (`Work-Build`, 11 September 2026) has no Typhon at all
(measured). The migration is therefore rehearsed on a copy of the bench PC's
project, presets and library before the real ones (question 12), and the
bench PC's checkout is confirmed to be past M0 before any capture folder is
shared.

---

## 8. Leaving Barbie behind

### 8.1 What Barbie shares with Typhon

| Example | Shapes | Script links (measured at 393671b8) | Uses Typhon's camera module? |
|---|---|---|---|
| `barbie_capture` (v1) | 17 | `frame_timing` 1 | No |
| `barbie_capture_v2` | 23 | `frame_timing` 1, `frame_roi` 1 | No |
| `barbie_capture_v3` | 41 | `frame_timing` 1, `frame_roi` 1, `frame_classes` 7 | No |
| `barbie_capture_v4` | 54 | as v3, plus `frame_camera` 7 | Yes: `list_cameras`, `connect`, `disconnect`, `start`, `stop`, `set_area`, `full_frame` |
| `barbie_capture_v5` | 55 | as v3, plus `frame_camera` 8 | Yes: v4's seven plus `setup` |
| `image_viewer` | 17 | `frame_timing` 1 | No |

Only v4 and v5 touch the code this design restructures.

### 8.2 From now until the switch-over

- **No further Barbie work,** as you said.
- **On `qt-migration`, nothing is removed.** Barbie keeps whatever works
  today, because today's Typhon code stays where it is until the
  switch-over.
- **On the branch,** v4 and v5 call `frame_camera`'s functions, which keep
  working over the rig (5.6). Nothing on the branch is built for them.
  Their tests stay in the branch's gate as free checks on those functions
  until one fails; a failing one is then retired on the branch at once,
  with its reason, through the `RETIRED` list and `CLAUDE.md`'s "Retired
  tests" list, not fixed and not left red (7.2). Whether they all pass
  today on the branch was not tried.
- **This amends the multi-camera design's promise** that "Barbie goes
  through the same wrappers" (its 2.9): Barbie goes through them while its
  tests pass, and no Barbie-only work is done to keep that true.

### 8.3 At the switch-over

1. **The tag** `typhon-classic-last` (7.4) freezes the last Typhon and
   Barbie that ran on the old structure. A checkout of the tag runs them,
   with no upkeep.
2. **Barbie v4 and v5 leave the examples:** their wireframes move out of
   `examples/gui/` (for example into `examples/retired/`), so the
   Designer's example list stops offering them. A project you built from
   one keeps its files; it just gets no updates.
3. **Their tests are retired on purpose, not lost by accident:**
   `tests/test_barbie_v4.py` (15 tests at 393671b8, measured) and the
   Barbie-only cases in `tests/test_typhon.py` (the v4/v5 comparisons at
   87-131 and the builds at 426-446). Each goes into the `RETIRED` list in
   `tests/test_retired_tests.py` (line 21) and the "Retired tests" list in
   `CLAUDE.md`, with the reason, so a later merge cannot quietly bring one
   back. Any already retired on the branch (7.2) are simply carried over.
4. **Two pieces of coverage are kept,** rewritten on a small fixture
   wireframe under `tests/data/` (as `tests/data/typhon_before_capture.gspec`
   already is):
   - a generated app whose slider is driven by its own browser must not
     get a second slider controller from `attach()`
     (`tests/test_capture_review.py:169`);
   - two different apps sharing one classifier store see each other's
     models with where they came from (`tests/test_typhon.py:957-1006`,
     which uses Barbie v5 as "the other app").
5. **Barbie v1-v3 and the image viewer stay as they are.** They never touch
   `frame_camera`, and `frame_timing`, `frame_roi` and `frame_classes` stay
   stable libraries with unchanged file formats; the one change is a public
   function added to `frame_classes` (`predict_image`, P4 row 4), which
   they do not use.

### 8.4 Why not freeze a copy of the old camera module for Barbie

`frame_camera` is not one file: it imports 15 other Council modules, and
with them it comes to about 19,300 lines, not counting `frame_classes`,
`frame_roi` and `frame_timing` (computed from sizes measured at 67cad625:
24,413 − 4,640 − 192 − 289). A frozen copy would carry its
own Basler and OpenEB code, which would need fixing whenever pypylon or
OpenEB change. The git tag freezes all of it with no upkeep.

---

## 9. Testing

The aim, from your request: a plugin is checked by its own tests, its kind's
contract suite and the main window's tests, and does not need the whole of
Typhon re-checked by hand. All tests run offscreen
(`QT_QPA_PLATFORM=offscreen`, `COUNCIL_NO_DIALOGS=1`), in temporary vaults
(`COUNCIL_VAULT_ROOT`) and a temporary plugin home, with pytest's
`--basetemp` set to a scratch folder (the prototype's runs used pytest's
shared default folder, about 25 MB, which later runs should avoid).

### 9.1 One contract suite per plugin kind

Like scikit-learn's `check_estimator`, one suite that every plugin of a kind
must pass (vendor/project docs); each runs first against the Simulated plugin.

- **Camera plugins:**
  - without its SDK, `available()` says no and gives an install hint, and
    importing the plugin does not crash;
  - discover, open and close can be repeated; `read()` with nothing to read
    returns `None` after its timeout;
  - an area that needs the stream stopped raises `NeedsStop` before anything
    is written; the area round-trips; the `restartable` rule holds;
  - its settings come from the camera with their ranges, and each set
    returns a `Change` saying what the camera actually took;
  - every capability it declares is confirmed by the opened camera and its
    facet works; its category hints give a valid tab plan;
  - its record fields are plain JSON; its non-screen code imports no Qt;
    nothing uses pickle.
- **Trigger sources:** start and stop can be repeated; `stop()` returns
  within a deadline; nothing fires after `stop()`.
- **Feature plugins:**
  - the manifest is valid and reading it imports nothing; every hook it
    lists exists;
  - hooks are called in the order of 3.6; `check_start` can refuse Start
    with a reason; a failed Start undoes what was armed;
  - record blocks are plain, namespaced and under the size limit; streamed
    sidecar files are created exclusively and only in the run's folders,
    and whole ones through the no-overwrite rename, `_2` on a rerun, never
    over anything; suffixes are registered; preset keys round-trip;
    settings saves take the lock;
  - **triggering:** snapshot from Software and from the Timer runs on a
    frame camera that offers no trigger facet at all, and saves the frames
    after each trigger; exact on that camera is greyed out with a reason
    (3.6);
  - **cropped export:** on the same capture folder and box, the plugin
    writes the same files as today's Save cropped frames link (3.5).
- **UI contributions:** each lands only in its host; the main window's
  widget tree is the same before and after the plugin is switched on; with
  its host missing, the contribution falls back to Settings → Plugins…; a
  pop-out panel fits at both window sizes (1504 x 1016 and the smallest,
  1400 x 820).
- **Faults, for every kind:** raising, `SystemExit`, writing to a frame's
  pixels or its meta, a sleeping hang, a slow plugin, a write outside the
  run, and creating one file twice. Each must end with frames saved = frames
  CSV rows = the end record's count, and the pixels unchanged. Both cases
  of 3.10 are tested: a fault on the recording path (record gate, frame
  stage, per-frame tag) ends the recording with its reason in
  `<run>_run_end.json`; a fault in a best-effort hook (`record_fields`, the
  calibration freeze, `on_run_finished`) is reported in Start's summary or
  the end record, marks the plugin faulted, and the capture carries on.
- **Loading third-party plugins:** a sub-module and a data file changed
  after the plugin loaded are not used in that session (the sub-module
  comes from the bytes read at load, the data file is refused), and at the
  next start both load without an approval question, with their new
  sha256 in the run record (your rule, 4.6); a new id still needs
  approval; two folders with one id are both refused; a folder's name
  does not change its package name; no `.pyc` is read or written; a folder
  copied among the built-ins that is not on the built-in list is refused
  (4.1, 4.5).

The prototype's version of these suites passed for six plugins (simulated,
Prophesee over the fake EVK4, Basler over pylon's emulator, triggering,
classification and a deliberately misbehaving one) in both Python
environments against both research snapshots: 35-37 tests each, 1-3
skipped where an SDK or the trigger methods were absent (measured,
synthetic).

### 9.2 The fake host

A stand-in rig with simulated cameras, a temporary project and no visible
window, so a plugin can be tested on its own. The pattern exists in
`council_qt/host.py` (`StandaloneHost`, line 49). Third-party authors get
the same fake host and suites with the plugin API, runnable outside the
repo (3.11).

### 9.3 Each plugin's own tests

- **They live with the plugin** (`typhon_plugins/<name>/tests/`) and are
  collected with the rest.
- **For the built-ins they are today's tests, moved,** not rewritten:
  `test_cameras.py` (91 test functions at 393671b8, measured; split by
  camera family, about 29 Basler, 27 EVK4, 12 raw and 5 simulated at
  67cad625 by a name heuristic), `test_camera_settings.py` (73),
  `test_basler_scan.py` (39), `test_basler_trigger.py` (31),
  `test_triggered.py` (27), `test_event_playback.py` (28), and
  `test_frame_classes.py` for the classifier library.
- **Core tests that lean on one camera's fake move to the Simulated plugin.**
  27 of the 41 tests in `test_settings_tabs.py` drive the core tab host
  through the fake EVK4 (measured by a name and body heuristic); they move
  onto simulated cameras, and the EVK4-specific ones stay with the
  Prophesee plugin.
- **Hardware tests stay where they run:** `test_basler_real.py` (40 test
  functions at 393671b8; it collects nothing outside the `pylon`
  environment), the emulator runs with `PYLON_CAMEMU=2`, and the fake EVK4.

### 9.4 What must still be tested together

Plugins do touch each other in a few places (section 1). These pairs get
tests of their own:

- **Downsample with the crop box and classification:** a box drawn on a
  reduced frame and a classifier trained on reduced frames; the
  `FrameView` carries the factor and method.
- **Coordinates (3.12):** a box and a calibration point placed on a frame
  averaged by Typhon (factor 2 and 3, with an area offset) map to the right
  sensor pixel, as `reduce.sensor_position` gives it; the same with camera
  binning as the Downsample.
- **Presets across the two Typhons:** a one-camera preset saved by today's
  Typhon still sets the trigger mode and Downsample in the plugin Typhon,
  and the other way round (2.5, 3.6).
- **Triggering with each camera plugin:** the Basler facets on pylon's
  emulator and the simulated cameras, including fan-out to two cameras
  (40/40 frames, measured, synthetic).
- **Snapshot mode with live-frame plugins:** frames between triggers reach
  `on_frame` marked "not saved".
- **Two plugins in one run:** their record blocks and sidecar files never
  collide, and the record stays readable.
- **Today's readers on new runs** (7.5).
- **The rig's Start and Stop order** with the trigger slot and the
  multi-camera design's leader and follower order (its 2.3), with fake
  devices.
- **Frame timing inside the real Qt Typhon** with the classification plugin
  on (10.2).
- **The main window** at both window sizes with every built-in strip
  filled.
- **The switch-over** on a copy of a project with edited handlers (6.4).

### 9.5 What exists today, for scale

1,078 tests were collected (not run) from 21 Typhon test files at 67cad625
(measured), and the trigger work added 79 test functions (measured). The
branch's gate (7.2) keeps all of them passing, except Barbie tests, which
are retired on the branch the moment one fails, with their reason (7.2,
8.2).

---

## 10. Performance and data safety

### 10.1 What the plugin system costs

| Cost | Figure |
|---|---|
| Reading six manifests at start-up | 3.9-4.8 ms (measured) |
| Reading 20 plugins and hashing all their files | 33-50 ms warm; about 250 ms just after copying them in or after a reboot; 100 plugins about 1.2 s cold (measured, synthetic) |
| Importing the plugin host | 125-150 ms in a bare interpreter, mostly modules Typhon already loads (measured) |
| Loading one plugin, before its SDK is used | Simulated 5.2 ms, Prophesee 45 ms, triggering 2.5 ms, classification 4.8 ms, Basler 53 ms (measured; interface prototype) |
| First use of a heavy package | sklearn 3.3-5.2 s; pypylon 0.6-2.0 s; the classification panel's first open in a fresh process, 239 ms for importing PySide6, `frame_classes` and the picker, plus 68-200 ms to build its window (measured; interface prototype, 3.7) |
| One guarded call into a plugin | 0.39-0.41 µs, against 0.13-0.15 µs unguarded (measured); about 0.2 ms extra per second at 150 fps with 5 plugins (computed) |
| Offering a live frame to plugins, paid by the grab thread | 5.0 µs (1 plugin), 8.4 µs (3), 16.9 µs (8), the same at full boA5320 size (measured) |
| Hand-over to the plugin's thread | 95-164 µs median, 577 µs p99 (measured) |
| The trigger record gate per frame | 1.0 µs median with no trigger pending, 4.5 µs with one (measured; interface report, whose script is lost and which no fact-check re-ran) |
| Memory | A frame a plugin still holds is never reused by the frame pool (`cameras.py:400-404`), so each plugin's two-slot queue plus the frame in its hands keeps up to three extra frames per camera alive: about 97 MB for full boA5320 Mono12 frames, 5328 x 3040 x 2 bytes each (computed) |

One figure from the research is not used: an unpaced loop said a no-op
plugin cut the rate from 22,400 to 15,400 frames a second, but the
fact-check could not reproduce it (the no-op ran faster in every pair).

### 10.2 The camera's grab thread and Python's lock

**The finding.** A plugin on its own thread still competes with the grab
thread for Python's lock (the GIL). On a paced 150 fps simulated camera
recorded through the real `CaptureSession` (measured, synthetic):

| Live plugin | Frames more than one period late, of about 575 | Notes |
|---|---|---|
| None, a no-op, or 5 ms of pure Python | 0-2 | p99 lateness 1.9-4.3 ms |
| 20 ms of pure Python per call | 313-340 | median 7-8 ms late |
| Live classification of 640 x 480 frames (17-49 ms per call) | 95-205 | p99 10.7-27.6 ms |
| The same, capped at 25 % of the time | 22-32 | the worst case barely changed |

**The main cause is Windows' 15.6 ms timer tick,** which makes handing the
lock between threads slow (`triggered.py:451-454` notes the same 15.6 ms
step). With the process timer set to 1 ms (`timeBeginPeriod(1)`) and
Python's switch interval at 1 ms, the 20 ms plugin made **0** of about 574
frames late (median 0.42-0.47 ms, p99 2.0 ms), and live classification 1-7
(measured, synthetic; interface fact-check). Two caveats:

- inside a running Qt event loop the same 20 ms plugin, at the default
  timer, made 113-125 frames late rather than 313-340 (measured), so the
  numbers must be taken again inside the real Typhon before choosing;
- Windows 11 may not honour the finer timer while the window is minimized
  or hidden unless the process asks for it explicitly, and it costs some
  power (vendor/project docs: Microsoft).

**What the design does:**

1. The core sets the finer timer and switch interval while it records,
   once a measurement in the real Qt Typhon confirms the gain (a stage P3
   row).
2. Each live plugin has a time budget, a 25 % duty cap and a watchdog, as a
   second line.
3. Heavy per-frame work stays off the live path by default: classification
   runs after Stop (about 1.2-1.8 ms a frame in a batch, measured, against
   17-49 ms live); live guesses only on reduced frames and capped
   (question 5); long jobs in a helper process.

### 10.3 Data safety

**What a plugin cannot do through the plugin API:**

- write a run file except as a new file: streamed and created exclusively
  in the active run's folders, or written whole and renamed into place with
  a rename that refuses an existing name (`_2` on a rerun), in any run
  folder you picked for analysis; never over anything, and with its suffix
  registered (3.9);
- change a record after it is written, or write one that is not plain,
  finite JSON under the size limit;
- write a preset value that is not a plain value, or lose another plugin's
  keys;
- change a frame's pixels: the frame's array is read-only from `read()`
  until the pool reuses it (3.8);
- delete anything: the API has no delete, only `temp_dir()`, which Typhon
  empties itself;
- leave a run half-processed: a fault on the recording path (the record
  gate, the frame stage, per-frame tags) ends the recording with the reason
  in `<run>_run_end.json`; a fault in a Start-time or end-of-run hook is
  reported and the capture carries on (3.10).

M0's rules stay as they are: frames and run names are created exclusively,
and the first failed write ends the recording.

**What a plugin can still do, because it runs in Typhon's process:**
everything in the list in 4.6. In short: read, write over and delete your
files, and undo a read-only flag with `os.chmod`; make a frame's pixels
writeable again (`setflags` on the array's `.base`, 3.8) and change them
before they are saved; use the network; start programs; end Typhon with
`os._exit`; reach the main window, other plugins and the camera's
`Device`. (The overwriting, `os.chmod` and reaching the main window and
the `Device` were measured; the rest follows from Python's and NumPy's
rules, vendor/project docs.) For built-in plugins, review and the contract
tests (9.1) guard against this; for third-party plugins, your approval
(4.6) and, if you want it, read-only saved files (question 9).

---

## 11. How velocity, calibration and multi-camera alignment become plugins

These designs are not redone here. Each piece they describe is given a
place in the plugin system. Their own sections, estimates and questions
stand, file names and record keys included (the one rule of 3.9), except
for these amendments, listed in full:

- **Coordinates:** multi-camera 4.13's conversion chain and warp-table
  keys, and the velocity calibration's `captured_with` (velocity 3.7), gain
  the Downsample factor and method (3.12).
- **Barbie:** multi-camera 2.9's "Barbie goes through the same wrappers"
  holds only while Barbie's tests pass (8.2).
- **Rig presets:** M1 row 4's `camera_rig.json` also holds the triggering
  plugin's rig-wide set-up as a namespaced block (3.6).
- **Writing analysis files** goes through the core's write-whole service
  (3.9 (b)), which follows velocity section 4's rule exactly.
- **Where they are built** (your answer to question 6): velocity,
  calibration, alignment and M2 are new Typhon features, so each is built
  as a plugin, never on today's Typhon: on the `typhon-plugins` branch
  while it lives, and on `qt-migration` only after the switch-over has
  made the plugin Typhon the one there. Velocity 5.2's "the velocity work
  can start from `qt-migration` directly" no longer holds; it starts on
  the branch after P3 (12).

### 11.1 Velocity (`typhon.velocity`, one camera, needs `events`)

| Velocity design piece | Becomes |
|---|---|
| `VelocityTracker`, no Qt (its 2.4) | The plugin's own module |
| The event-block reader factored out of `RawPlayback._read` (its stage 1, row 1) | Part of the Prophesee plugin, which owns `event_playback`; offered to features as a **run reader** for that camera's recordings, so the velocity plugin never imports OpenEB code |
| The Analyse velocity job: thread, progress, cancel (stage 1, row 3) | The `analyse()` hook (3.8) |
| The vector overlay in `ImageCanvas` (its 2.5; stage 1, row 4) | The core's **canvas overlay** contribution point (3.7), built once and shared with alignment; it changes the generated canvas, so it reaches projects through Update from example, as that design says |
| The velocity window (stage 1, row 5) | The plugin's pop-out panel |
| `<run>_tracks.csv`, `<run>_track_points.npy`, `<run>_velocity.json`, "added to the list in `_unique_run`" (its section 4) | The same names, kept (3.9); run-file suffixes registered by the plugin; written after analysis through the write-whole service, beside any past recording, `_2` on a rerun, never over anything (3.9 (b)) |
| Live arrows: a hook in `EvkDevice.read()`, a drop-oldest bounded queue, counted gaps (its 2.4; stage 2, row 2) | The Prophesee plugin's `EventStream` facet and the velocity plugin's live-event hook; the lock and timer findings of 10.2 apply to it |
| Tracker set-ups linked to presets (stage 2, row 4) | Preset keys `typhon.velocity/...` |

### 11.2 Calibration (`typhon.calibration`, one camera; whole rig for MODE 2)

| Velocity or multi-camera design piece | Becomes |
|---|---|
| The store `<project>/camera_calibrations.json` and clips under `<project>/calibration/<id>/` (velocity 3.7) | The plugin's own project files, at the paths that design fixed (a built-in plugin may keep names its design already chose) |
| The Calibrate window and steps (velocity 3.10) | The plugin's pop-out panel |
| Recording a clip with ERC, trail and anti-flicker switched off and restored in a `finally` block (velocity 3.4, 3.10; stage 1, row 8) | Two core services added to the API: "these settings for this clip, restored afterwards and checked", and "record a short clip into this folder", outside the run folders |
| The frozen `<run>_calibration.json` at Start and the record pointer (velocity 3.9; stage 1, row 9) | A best-effort Start-time hook (3.10) writing the sidecar under that name, with the camera record's top-level `"calibration": {id, name, stage, sha256}` pointer where velocity 3.9 put it, not in the `plugins` block. A failure is reported in Start's summary and never stops the capture |
| The LED finder `led_locate` (velocity stage 1, row 6) | A library inside the plugin |
| Pixels to millimetres (velocity 3.6) | A service the calibration plugin offers other plugins; velocity "uses" it when present and shows px/s when not |

### 11.3 Multi-camera alignment, MODE 2 (`typhon.align`, the whole rig, uses calibration)

| Multi-camera design piece (stage M3) | Becomes |
|---|---|
| Rig calibration: every camera records the board at once (its 4.4) | The calibration plugin's clip service run on every slot together |
| Live side-by-side and overlay, labelled approximate (its section 5) | The canvas overlay plus a pop-out panel, on a worker thread |
| Export corrected and `<run>_registration.json` (its section 6) | `analyse()` in a helper process for long exports; the same names, written through the write-whole service into a new `<run>_corrected/` (`_2` on a rerun); registered suffixes |
| Plate definitions in `camera_rig.json` (its 4.12) | Kept at the top of `camera_rig.json`, where that design put them, owned by the alignment plugin; and the rig record's per-camera calibration ids stay where its 2.6 put them |

### 11.4 The trigger cable, stage M2

M2 splits along plugin lines:

- **EVK4 Trigger In and the sync link** become Prophesee capabilities and
  facets (`trigger_in`, `sync`); the **Basler "Lines" settings** become
  Basler plugin settings.
- **The rig's Start and Stop order** with leaders and followers (multi-camera
  2.3) is the core's trigger slot (3.6).
- **Pairing after Stop** (`<run>_triggers.csv`, `<run>_pairs.csv`, the
  report on screen) is a rig-scoped feature in the triggering family. Its
  `<run>_triggers.csv` (edges recorded by an EVK4) and today's
  `<run>_capture_triggers.csv` (`triggered.py:350`, what Typhon asked for)
  are different files and both stay.

### 11.5 What to reserve in API 1.0 so these fit later

1. **The canvas overlay layer** (velocity arrows, alignment overlay).
2. **The `EventStream` facet** and the **run reader** offered by camera
   plugins.
3. **Temporary settings and clip recording** as core services.
4. **Plugin dependencies:** a manifest field `uses`, each entry marked
   optional (velocity uses calibration if present) or required (alignment
   needs calibration).
5. **Rig-level data:** a namespaced block per plugin in `camera_rig.json`
   and in `<run>_rig.json` / `<run>_rig_end.json`, for new plugin data and
   third-party plugins only. Keys the multi-camera design already fixed
   (the plate definitions, the calibration ids, the sync roles and Trigger
   In state, the "rig" pointer) stay where it put them (3.9).

Reserving them costs little now (names in the API, empty in 1.0); adding
them later as minor versions works too, but a design that never planned
them would have to be bent (estimate). Turning each later feature into a
plugin adds about 1-2 days to its own design's estimate for the manifest,
contract tests and panel wiring (estimate).

---

## 12. Staged plan

All efforts are **estimates**: working days for one developer who knows the
codebase, tests included. M1's rows keep the multi-camera design's own
estimates. The research put the plugin work at 29-43 days on top of M1;
the rows below add what the fact-checks found (driving `TriggerRun`'s books
from the rig, the hash-then-compile loader, the blocked Update from
example, the timer finding, the branch set-up) and what the design review
found (splitting the capture set-up, the two file services, the camera
set-up guides as plugin work, registering the new example, the Update fix
on `qt-migration`, the files moving only at P7, a fuller author's kit).
**Stages P0-P3 leave Typhon behaving exactly as today;** the window changes
at P4.

**What your answers of 15 October changed here** (section 1): the project
lists in P1 row 2 and the Plugins window in P4 row 7 copy the base set
into new projects instead of following it (question 1); P5 row 1 approves
once per plugin and takes plugins as folders dragged in (question 7); and
velocity and M2 are built on the branch (question 6, Totals). Questions 2
and 13 confirmed what the rows already assumed. No estimate changes:
copying is no more work than following, and approving once drops the
re-approval flow while adding the same-id check and the "Open plugins
folder" button (estimate).

### Stage P0: the branch, safely beside today's Typhon (about 1-2 days)

| # | Work | Estimate |
|---|---|---|
| 1 | Branch `typhon-plugins` from `origin/qt-migration`; a git worktree at a stable path; that checkout's Council on its own vault and plugin home; the new project's classifier store pinned; a script that runs the branch's gate (7.2) | 0.5-1 day |
| 2 | **On `qt-migration`, as a bug fix** merged into the branch: Update from example checks first and writes nothing (`project.gspec`, `manifest.example`) until it knows it can finish, and its recovery message names both (6.4) | 0.5-1 day |

**What you see:** nothing changes in today's Typhon, except that a blocked
Update from example no longer leaves a project half-switched. A second
checkout exists.

### Stage P1: the rig and the plugin host, behind today's functions (about 7.5-12.5 days)

| # | Work | Estimate |
|---|---|---|
| 1 | M1 row 1: `Rig`, `CameraSlot`, `_LIVE` as a compatibility view (all 49 fields), every existing test passing unchanged | 4.5-7 days |
| 2 | The plugin host on the rig: manifest reader, registry and built-in list, guarded calls, the faulted state, plugin contexts, `typhon_plugin_api` 0.x and working notes (frozen as 1.0 only at P5, 3.2), the base set with default settings and the project lists, each copied from the base set once when the project is made (4.2), `TYPHON_PLUGIN_HOME` (4-6 days alone; about 2 are shared with row 1) | 2-4 days |
| 3 | Measure frame timing inside the real Qt Typhon with a slow plugin, at the default and the 1 ms timer (10.2) | 0.5-1 day |
| 4 | Register the new example: `examples/gui/typhon_plugins.gspec`, at first a copy of today's wireframe under the new name, in `gui_examples.py` (`NOTES` at 66, `INTENDED_TOOLKIT` at 147) | 0.5 day |

**What you see:** "Typhon (plugins)", built from the branch with a copy of
today's wireframe under the new name (row 4), beside today's Typhon. It
looks and behaves exactly like today's.

### Stage P2: camera plugins (about 11-17 days)

| # | Work | Estimate |
|---|---|---|
| 1 | Basler, Prophesee (with its raw view as a review source) and Simulated as adapter camera plugins over the existing `Backend`, `Device` and `Provider` classes, which stay where they are (7.2); category hints per plugin; capabilities instead of the "event camera" checks; the `Provider.extra_stops` hook (2.1); `cameras.backends()` and `discover()` kept for the Capture tab | 5-8 days |
| 2 | M1 row 8: N simulated cameras (`COUNCIL_SIM_CAMERAS`), fake EVK4 serials, trigger and sync fakes, replay rigs | 3-4 days |
| 3 | M1 row 4: `camera_rig.json` (names, rig presets); several camera families in the set-up, as the enabled camera plugins; wizard check boxes; "in use as cam2" in the scan | 2-3 days |
| 4 | Camera set-up guides and checks through the plugins: each camera plugin's guide, install checks and (Basler) scan page in the wizard (2.3; 2.4 item 15); `camera_setup.json`'s choice read as the project's plugin list, `FORMAT` 1 still read | 1-2 days |

**What you see:** the same Typhon. The settings tabs are the same tabs
(the prototype's merged hints gave identical tab plans, measured); the
set-up wizard ticks camera families instead of choosing one.

### Stage P3: run hooks, and triggering out of the core (about 13.5-19.5 days)

| # | Work | Estimate |
|---|---|---|
| 1 | Run lifecycle hooks, the record-gate and frame-stage points, the run-file suffix registry, cleaned and size-checked record blocks under the one rule of 3.9, the two file services (streamed, and write-whole with a no-overwrite rename), the fault rule (recording path against best-effort hooks, 3.10) | 3.5-4.5 days |
| 2 | The triggering plugin: `TriggerRun`'s books driven by the rig, Software and Timer sources, needs checked per mode and source in `check_start` (3.6), the Basler and Simulated facets, the trigger columns kept by name, the `capture.*` keys read and written in place, its panel, Capture now (F9) and status segment | 4-6 days |
| 3 | Downsample as the core's one frame stage; binning as a Basler facet; splitting the capture set-up (2.5): `CaptureOptions`, `CaptureSetup` and the facet signatures in `typhon_plugin_api`, the core's Downsample row in every frame camera's "Capture" group, the trigger set-up as the plugin's per-slot state; `FrameView`'s coordinate fields (3.12); `capture.py` stops importing `reduce` and `triggered` | 2-3.5 days (1-2, plus 1-1.5 for the split) |
| 4 | Preset and record namespacing; keys of a missing plugin kept untouched | 1.5-2 days |
| 5 | The finer timer while recording, if P1 row 3 confirmed it | 0.5 day |
| 6 | Contract suites for every kind and the fake host (9.1, 9.2) | 2-3 days |

**What you see:** the same Typhon, with the same trigger behaviour, the
same files and the same CSV columns; today's readers open its runs (tests).
Only the calls have changed; the code itself has not moved (7.2). (The
triggering panel exists but the window still shows today's trigger boxes
until P4.)

### Stage P4: the new window and the Plugins window (about 12.5-18 days)

| # | Work | Estimate |
|---|---|---|
| 1 | UI contribution points: Typhon-owned pop-out windows that remember their place, the toolbar and status strips, the "Plugins…" registration in `gui_settings`, shortcuts, the main-window widget check | 3-4 days |
| 2 | M1 row 7 with the strips: `typhon_plugins.gspec` (53 shapes out; the strips and M1's ports in); layout tests at both window sizes | 1-2 days |
| 3 | M1 row 5: settings, presets and windows follow the active camera through the slot's 20-name `api` | 2-3 days |
| 4 | The Classification pop-out; Classify all after Stop by default; a public `predict_image` in `frame_classes` so live guesses need no private calls | 3-4 days |
| 5 | The Timing scan and Cropped export plugins, with `ctx.crop_box()` and `crop_box_changed` (3.5) | 1-1.5 days |
| 6 | The set-aside step inside the update path that both Update and "Switch to example…" use: edited handlers into `handlers_retired.txt`, `app.py` lines that use removed widgets listed (6.4) | 1-1.5 days |
| 7 | The Plugins window (moved here from P5; required before P7): the base set with its default settings (what new projects start with, 4.2); this project's own list, with each base-set plugin it lacks marked and a tick box to add it; setting overrides; each plugin's state and reason; every plugin file with its sha256, the crash marker and its question, the interrupted-run notice (3.10, 4.3) | 1.5-2 days |

**What you see:** the new main window: the core controls, a toolbar strip
(Classification, Capture now, Timing scan, Crop export, PNG / Raw), a status
strip, and Settings → Plugins…; classification and the trigger controls in
their own windows; the Plugins window, where you switch built-in plugins on
and off for this project, or in the base set for every project made
afterwards. Still one camera.

### Stage P5: third-party plugins and API 1.0 (about 4-6 days)

| # | Work | Estimate |
|---|---|---|
| 1 | The hardened loader: an importer per plugin package serving only the checked bytes, no bytecode; data files through `ctx.read_data()`; the `typhon_thirdparty` package root; folder rules (each plugin its own folder, dragged in, any folder name, the package name from its id, two folders with one id refused) and the built-in list; the "Open plugins folder" button; `approvals.json`, one approval per plugin id, never asked again (your answer to question 7); the risky-pattern scan; the network field, its opt-in and its tripwire (4.5, 4.6) | 2-3 days |
| 2 | API frozen as 1.0, and the author's kit of 3.11: the guide (manifest schema, threading table, types, re-exported classes, open contribution points, environment, house rules, update and approval flow), `python -m typhon_plugin_api.check` with the suites and fake host usable outside the repo, one example plugin | 2-3 days (the earlier 0.5-1 day left out most of 3.11) |

**What you see:** a plugin's folder dragged into
`~/.council/typhon/plugins/`, approved once after you have looked at it; a
later version dragged in over it runs without a question (4.6). Only rows
1 and 2 may wait until someone else actually writes a plugin, since only
plugins from other people need them; the Plugins window, which the base set
and a project's own list need, is already in P4. The discipline of P1
(built-ins use only the plugin API) cannot wait, because it is what makes
P5 cheap.

### Stage P6: the rest of M1 (about 12-19 days)

| # | Work | Estimate |
|---|---|---|
| 1 | M1 rows 2, 3, 6, 9 and 10: one save budget and writer pool; run layout, reservation and rig records (with plugin blocks under the rule of 3.9); tiles, grid and live pop-outs; busy EVK4s; software-trigger fan-out through the triggering plugin. (Row 4 is in P2.) | 11.5-18 days (multi-camera design: 2-3, 2.5-3.5, 3-5, 3.5-5.5, 0.5-1) |
| 2 | The rig-wide trigger set-up: the triggering plugin's namespaced block in `camera_rig.json`, an amendment to M1 row 4 (3.6) | 0.5-1 day |

**What you see:** what M1 promised: 2-4 cameras in one window, tiles, one
Start and one Stop, a folder per camera, each with its own settings.

### Stage P7: the switch-over (about 4-7 days)

| # | Work | Estimate |
|---|---|---|
| 1 | Today's readers on new runs (tests); a rehearsal on a copy of the bench PC's project, presets and library | 1.5-2.5 days |
| 2 | Barbie v4 and v5 retired: examples moved, the `RETIRED` list and `CLAUDE.md` (those not already retired on the branch, 7.2), the two kept tests rewritten (8.3) | 0.5-1 day |
| 3 | Tag, merge, rename the example (`typhon_classic` kept with its facades, 6.4), move your projects through Update | 0.5-1 day |
| 4 | Move the built-in plugins' code from `council_core` and `frame_camera` into `typhon_plugins/<name>/`, leaving re-exports at the old names, once today's Typhon is frozen (7.2) | 1.5-2.5 days |

**What you see:** one Typhon again, the plugin one, with your projects,
presets, runs and classifier libraries.

### Totals, and how this fits with the other designs

- **All stages:** about 65.5-101 days (computed from the rows above), of
  which M1's own rows are 24-37 and the plugin work 41.5-64. Add merge
  upkeep of about half a day a week while the branch lives, about 6.5-10
  days over 13-20 weeks (estimate).
- **Velocity** stage 1 is built as a plugin on the branch, not on today's
  Typhon (your answer to question 6; 11), after P3 (it needs the run
  reader, the `analyse` hook and the suffix registry) with its overlay in
  P4; its own estimates stand, plus about 1-2 days for the plugin form
  (11.5).
- **M2** (the trigger cable) follows P6, as the multi-camera design orders
  it after M1, and is plugin work too: on the branch if it starts before
  the switch-over, never on today's Typhon. It now extends the triggering
  plugin and the camera plugins' facets rather than the core.

---

## 13. Open questions for you

The velocity design's questions (lenses, LED hardware, fields of view,
accuracy, OpenCV, a compiled helper, the EVK4 and SDK Pro, real recordings)
and the multi-camera design's (which cameras, the desktop and frame grabber,
more Baslers, trigger hardware, one PC or two, rates, lighting, the working
plane, export formats, tolerance, windows, the shared board, screen and
folders, Start and Stop, thermal cameras, disks) still stand and are not
repeated. These are only about plugins.

**Answered on 15 October 2026** (your words, quoted in full in section 1):
questions 1, 2, 6, 7 and 13. Each keeps its number and its question, with
your answer, what the design now does, and any part still open. Questions
3, 4, 5, 8, 9, 10, 11, 12 and 14 are open.

1. **Answered. The app-wide base set: followed, or new projects only?**
   The design had made the base set followed: a plugin you add to it would
   turn on in every existing project at its next start. Your answer: "Q1:
   Only new projects". So the base set is copied into a project when it is
   made; a plugin added to the base set later reaches only projects made
   afterwards, and an existing project keeps its list until you add the
   plugin there (section 4.2).
2. **Answered. Downsample:** keep it in the core as the one step that
   changes saved pictures, with its control in the settings notebook
   (recommended), or make it a plugin of its own, or part of triggering
   (section 2.1)? Your answer: "Q2/13: Yes they stay in core". Downsample
   stays in the core, its control in the settings notebook (2.1, 2.5).
3. **Plugin settings:** in each plugin's own window (recommended, because
   the tab strip is full), or one shared "Plugins" tab, which needs the
   settings column widened to at least 491 px (sections 3.7 and 6.1)?
4. **Plugin windows:** built in code and not editable in the Designer, or
   described as small wireframes you can edit there, at about 3-5 more days
   (section 6.3)?
5. **Classification while recording:** is classifying each run right after
   Stop enough (recommended), or do you need live guesses during capture,
   which would run only on reduced frames and capped (section 10.2)?
6. **Answered, one part still open. Today's Typhon while the branch
   lives:** may it take bug fixes only, with new Typhon features going to
   the branch as plugins? Your answer: "Q6: Bug fixes only new features as
   plugins". So today's Typhon on `qt-migration` takes bug fixes only, and
   every new Typhon feature, velocity and M2 included, is built as a
   plugin on the branch (7.2, 11). **Still open:** from when may it take
   no fixes at all? The built-in plugins' files move into their own
   folders only from then (7.2; stage P7 at the latest).
7. **Answered in part. Plugins from other people:** how will they reach
   the PC, and is "you approve each version once, and a changed file asks
   again" the right level? Your answer: "Q7: Plugins will be their own
   folders dragged into the plugins folder for the app. Approval only
   happens once for each plugin". So each plugin is its own folder, which
   you drag into Typhon's plugins folder (4.1), and you approve each plugin
   once: a later version or a changed file is not asked about again (4.6
   states the trade-off). **Still open:**
   - "The plugins folder for the app" is read as Typhon's one plugins
     folder on the PC, `~/.council/typhon/plugins/`, not a folder inside
     each project. Is that what you meant? If so, it also answers "should
     a project ever carry plugin code" with no, as recommended (4.1
     explains why plugin code inside a project is blocked by the policy
     gate).
   - May a plugin that declares `internet` ever be opted in (4.6)?
   - A suggestion, not a decision: should the Plugins window show "changed
     since you approved it" beside a plugin whose files changed, as
     information only, never blocking and never asking again (4.6)?
8. **Extra Python packages:** if a third-party plugin needs one (for
   example a thermal camera's SDK), is it acceptable that you install it
   into the environment yourself, and Typhon only checks and says what is
   missing?
9. **Read-only saved files:** should Typhon mark each saved frame and
   finished CSV read-only, at 0.23-0.27 ms per file, after which Windows
   asks before deleting them (sections 4.6 and 10.3)?
10. **The new app's classifier library:** share your current one (its file
    formats are then frozen until the switch-over), or start with a copy of
    its own (section 7.1)?
11. **Barbie:** retire v4 and v5 and their tests at the switch-over, and
    keep v1-v3 and the image viewer as they are (section 8)?
12. **The bench PC:** which checkout runs today's Typhon there, and can a
    copy of its Typhon project, `camera_presets.json` and classifier library
    be used to rehearse the switch-over (section 7.5)?
13. **Answered. The split:** do you agree that presets, run records, the
    crop box and Downsample stay in the core, and that the camera
    families, triggering, classification, the timing scan and cropped
    export become plugins (section 2)? Your answer: "Q2/13: Yes they stay
    in core". So presets, run records, the crop box and Downsample stay in
    the core, and the camera families (Basler, Prophesee, Simulated),
    triggering, classification, the timing scan and cropped export become
    plugins (2.1, 2.3).
14. **Plugin settings at the app-wide level:** each plugin's default
    settings live in the base set, and a project keeps only what it
    changed (4.2). When you change a setting in a plugin's window, should
    it change the app-wide default for every project that has not
    overridden it, or only this project, with a "make this the default for
    every project" button (recommended)? And, after your answer to
    question 1 (the plugin list reaches only new projects): should a
    changed default setting likewise reach only projects made afterwards,
    or keep reaching every project that has not overridden it, as 4.2
    has it now?

---

## What has not been verified

- **No hardware.** The EVK4 path ran over the fake EVK4 behind a fake
  device discovery, not real OpenEB with a camera; the Basler path ran on
  pylon's emulator (1024 x 1040 frames, time stamp 0), not a boA5320 on
  CoaXPress.
- **Nothing was built into Typhon.** The interface is a prototype outside
  the repo. Of the screen pieces only the classification pop-out was built
  (offscreen); the toolbar and status strips were checked as the emitted
  classes accepting widgets at run time, not in a full Typhon build; the
  Plugins window, plugin settings sections and the "Plugins…" registration
  were not built.
- **Update from example across example names** was measured with a stand-in
  example built from 67cad625's 82-shape wireframe, not with the real new
  wireframe; the "set edited handlers aside" step of 6.4 is a design.
- **The timer finding** (0 late frames at a 1 ms timer) was measured in a
  benchmark process, not inside the running Qt Typhon, and a Qt event loop
  alone changed the counts; Windows 11's handling of a minimized window is
  from Microsoft's documentation only.
- **Read-only frame arrays from `read()`** (3.8) are a design; the cost and
  the frame pool's handling were not measured.
- **Helper processes** for live plugins were not built; only an offline
  helper (Classify all) was measured.
- **Records:** no rig record with plugin blocks was written; only
  per-camera `<run>_camera.json` files carried them.
- **Migration:** moving the trigger branch's preset keys, and the
  "today's readers open new runs" tests, were not prototyped. That an older
  Typhon treats `<id>/<key>` preset keys as unknown rather than as damage
  rests on reading the code and on the prototype's `apply` test, not on
  running an older build.
- **Barbie v4 and v5 over the rig** were not tried; their tests are kept in
  the branch's gate only until one fails (7.2).
- **Designs added in the review, none prototyped:** splitting the capture
  set-up (2.5), `Provider.extra_stops` (2.1), the per-mode trigger checks
  (3.6; they follow today's code), the crop-box context call (3.5), the
  write-whole file service (3.9; it copies `camera_record.write`'s rule),
  the best-effort hooks (3.10), the per-plugin importer and data-file
  checks (4.5), the home-relative plugin home and `TYPHON_PLUGIN_HOME`
  (4.1), the coordinate fields and their amendments to the other designs
  (3.12), a long-lived start call in `child_proc` (3.8), the set-aside step
  inside Update and `handlers_retired.txt` (6.4), and the author's guide
  outline (3.11), whose thread limits and refresh rate are choices to be
  checked when built.
- **Changes after your answers of 15 October, none prototyped:** the base
  set copied into new projects and the project's own list (4.2), approval
  once per plugin id (4.6), the package name taken from the manifest's id
  rather than the folder's name, the same-id check and the "Open plugins
  folder" button (4.1), and `ctx.read_data()` checking against the hashes
  taken at load rather than at approval (4.5).
- **The trigger record gate's cost** (1.0 and 4.5 µs) comes from the first
  report; its script is lost and no fact-check re-ran it.
- **The emulated Baslers' arrival skew** varied widely between runs (median
  0.37-1.7 ms in the interface report, 2.7-15.1 ms in its fact-check), so no
  conclusion is drawn from it.
- **Test counts:** the 1,078 tests were collected, not run; the splits of
  tests by camera family are name-and-body heuristics.
- **The bench PC's data** (its Typhon project, presets and classifier
  library) was not seen; this laptop has none.
- **Other projects' behaviour** not checked: where napari keeps a disabled
  plugin's state, whether QGIS's enabled key is per profile, whether Blender
  enforces declared permissions, what OBS does on a version mismatch,
  whether VS Code's extension host is a separate process, and what changed
  in pluggy 1.7.0.
- **The cold-start discovery time** (about 250 ms for 20 plugins against
  33-50 ms warm) is put down to file-system or antivirus caching; that is an
  assumption.
- **The "about 19,300 lines" behind `frame_camera`** (8.4) was computed from
  sizes at 67cad625.
- **All efforts are estimates.**

---

## Where the numbers come from

- **Research and fact-checks** (scratch folders, temporary), under
  `C:/Users/apkun/AppData/Local/Temp/tplug/`:
  - `seams/`: read-only exports of 67cad625 (`qtm/`) and the trigger branch
    (`trig/`); `fsizes.py`, `bucket.py` (the `frame_camera` buckets),
    `secsize.py`, `msizes.py`, `gspec_dump2.py`; `exp/switch.py` (Update
    from example across example names);
  - `fc-seams/`: `fsizes.py`, `livefields.py` (the `_Live` fields),
    `glinks.py`, `gdump.py`, `evkuse.py`; `exp/switch2.py` (the blocked
    update), `exp/hosts.py` (toolbar and status bar accepting run-time
    widgets), `exp/gate.py` (the policy gate on a project's plugin folder);
  - `prior_art/`: `measure_manifests.py`, `bench_guard.py`, `plugs/`;
  - `fc-prior_art/`: `disc.py` (discovery, warm and cold), `imp_time.py`
    (import times), `guard.py`, `pyc_tamper.py` (the planted `.pyc`),
    `reach_main.py` (a plugin reaching the main window),
    `startup_cost.py` (today's start-up imports), `pluggy_check.py`;
  - `interface/proto/`: the prototype (`typhon_plugins/`, `plugins/`,
    `user_plugins/`), its tests (`tests/test_contracts.py`,
    `test_trigger.py`, `test_basler_emulator.py`) and benchmarks
    (`bench/bench_hooks.py`, `bench_jitter.py`, `bench_mitigate.py`,
    `bench_offline.py`, `bench_startup.py`), with their results in
    `interface/bench_*_1.json`;
  - `fc-interface/`: `tabw.py` (tab widths), `bench/gil_timer.py`,
    `bench/gil_qt.py`, `bench/cpu_hang.py`, `pyc/pyc_test.py`,
    `policy/check_policy.py`, `ro/` (read-only files), `rec/` (record
    blocks), `settings_race/`, `trig/test_zero_wait.py`,
    `trig/test_caps.py`, and the test re-runs in `pt_*`;
  - `writer/` (this doc): `gspec_count.py` and `links.py` (shapes and
    script links of every example at 393671b8), `sizes.py` (class sizes in
    `cameras.py` and `camera_settings.py` at 393671b8);
  - `reviser/` (the revision after the design review): `sums.py` (checks
    that each stage's rows add up to its heading, and the total).
- **Typhon code** at `qt-migration` 393671b8: `frame_camera.py`,
  `council_core/cameras.py`, `capture.py`, `camera_settings.py`,
  `camera_categories.py`, `camera_record.py`, `camera_presets.py`,
  `camera_setup.py`, `triggered.py`, `reduce.py`, `event_playback.py`,
  `basler_scan.py`, `designer_examples.py`, `ide_trust.py`;
  `council_qt/widgets/settings_tabs.py`, `camera_settings_window.py`,
  `capture_review.py`, `classifier_picker.py`, `camera_wizard.py`,
  `pop_out.py`, `preset_picker.py`; `council_qt/tabs/capture.py`,
  `designer.py`; `council_qt/host.py`; `frame_classes.py`, `frame_roi.py`,
  `frame_timing.py`; `gui_policy.py`, `gui_emit.py`, `gui_emit_qt.py`,
  `gui_settings.py`, `gui_ports.py`, `gui_shapes.py`, `gui_examples.py`,
  `gui_runner.py`, `gui_smoke.py`, `run_example_gui.py`;
  `examples/gui/*.gspec`; `tests/test_retired_tests.py`,
  `tests/test_typhon.py`, `tests/test_capture_review.py`,
  `tests/test_barbie_v4.py` and the other Typhon tests named in section 9;
  `CLAUDE.md`.
- **Project docs:** `docs/typhon_multicamera_design.md` (sections 1, 2.2-2.10,
  4.4, 4.12, 5, 6, 9 and 10) and `docs/typhon_velocity_design.md` (sections
  2.4, 2.5, 3.4-3.10, 4, 8 and 9).
- **Vendor and project docs** (not re-checkable offline): napari's npe2
  (manifest reference; `schema.py`, `_command_registry.py`,
  `_plugin_manager.py`, `_pytest_plugin.py`); QGIS (PyQGIS developer
  cookbook, `qgspluginregistry.cpp`, `python/utils.py`, plugins.qgis.org
  security scanning, the user manual's plugins page); Micro-Manager
  (`MMDevice.h`, the MMDevice README, DemoCamera, hardware-based
  synchronisation); Spyder (`spyder/api/__init__.py`,
  `spyder/api/plugins/new_api.py`); Blender's extension manifest; VS Code
  (the extension host, Workspace Trust); OBS (the front-end API, plugin
  properties); ImageJ and SciJava plugin indexing; the Language Server
  Protocol 3.17; scikit-learn's `check_estimator`; pluggy (PyPI and its
  documentation); Python's documentation (`importlib.metadata`,
  `sys.addaudithook`, `tomllib`); PyInstaller's hooks; the Python Packaging
  guide on plugins; Microsoft's `timeBeginPeriod` documentation.
