# The typhon-plugins branch: running "Typhon (plugins)" beside today's Typhon

Branch `typhon-plugins` builds the plugin Typhon of
`docs/typhon_plugins_design.md` together with multi-camera stage M1. Today's
Typhon stays on `qt-migration`, takes bug fixes only, and keeps working
throughout; its fixes are merged into the branch. This page says how the two
run side by side, what can go wrong, and the test gate every branch commit
passes.

## The two Typhons

| | Today's Typhon | Typhon (plugins) |
|---|---|---|
| Branch | `qt-migration` | `typhon-plugins` |
| Checkout | your usual one | `C:\Users\apkun\Typhon-plugins` (a git worktree; the assistant moves it forward after each stage, so do not commit there) |
| Started with | your usual launcher | `run-typhon-plugins.bat` in that checkout |
| Vault | `~\.council\vault` | `~\Typhon-plugins-vault` |
| App folder | `~\.council` | `~\Typhon-plugins-app` |
| Plugin home | (none) | `~\Typhon-plugins-app\typhon`: the base set, and plugins from others in `plugins\` |
| Classifier library | `~\.council\vault\classifiers`, shared by its apps | `~\Typhon-plugins-vault\classifiers`, its own, empty at first |
| Example | `typhon` | `typhon_plugins`, titled "Typhon (plugins)" |

`run-typhon-plugins.bat --where` prints the folders and starts nothing.
`TYPHON_PLUGINS_VAULT` and `TYPHON_PLUGINS_APP`, set before it runs, choose
other folders.

## Running it

1. Double-click `run-typhon-plugins.bat` in `C:\Users\apkun\Typhon-plugins`.
   It sets, for that window only, `COUNCIL_VAULT_ROOT`, `COUNCIL_APP_DIR`,
   `TYPHON_PLUGIN_HOME` and `COUNCIL_SKIP_PATH_MIGRATION=1`, clears
   `FRAME_CLASSES_STORE`, and starts that checkout's Council through its
   `run-windows.bat` (Python: `.council_python`, `COUNCIL_PYTHON`, `.venv`,
   the conda env `wizardCouncil`, then the conda env `council`).
2. That Council has its own settings: pick the model in its Models tab if a
   tool needs one. Model files are not copied; Typhon needs none.
3. In its Designer: New from example, `typhon_plugins`, Qt. The project
   (default name `example_typhon_plugins`) lands in the side vault with a
   `classifier_store.json` beside its `app.py` that names the side vault's
   store, and a `typhon_plugins.json` that lists the plugins it runs (the
   base set as it was when the project was made).
4. Run it from that Designer, or double-click its `main.py`. Either way it
   runs the plugin checkout's code and uses its own classifier store.

Until stage P4 it looks and behaves exactly like today's Typhon, under its
new name.

## The traps, and what guards each

- **Regenerating a project of today's Typhon from the plugin checkout.** A
  project runs the code of the checkout that generated it (`gui_emit.py`
  writes that checkout's path into `main.py`). The side vault keeps every
  project of today's Typhon out of the plugin Council's Designer. The
  launcher refuses a side folder under `~\.council`, and one that is, holds
  or sits in the vault or app folder the window already names.
- **A checkout that disappears.** Every app built from a checkout breaks if
  the checkout is deleted. Keep it at `C:\Users\apkun\Typhon-plugins`, never
  under `.claude\worktrees\`, which tools clean up on their own; the
  launcher warns when started from there.
- **The classifier library follows how an app was started.** An app started
  by the Council inherits `COUNCIL_VAULT_ROOT`; a double-clicked one does
  not, and would fall back to `~\.council\vault\classifiers`, your real
  library. So a `typhon_plugins` project pins its store with
  `classifier_store.json` (written home-relative, `~/Typhon-plugins-vault/
  classifiers`), and `typhon_plugins` is never built into a vault under
  `~\.council`: the Designer and `run_example_gui.py` refuse it there.
  Never edit that file to point at another app's store.
- **The old-files move.** At every start the Council moves an older build's
  files (`council.log`, `node_registry.json`, `.chromadb`, `workspace`, ...)
  from its app folder into its vault. With only the vault redirected, that
  would move them out of `~\.council` into the side vault. The launcher
  points the app folder at the side one and sets
  `COUNCIL_SKIP_PATH_MIGRATION=1`, as the test sandbox does
  (`tests/sandbox_vault.py`).
- **One program per camera.** A second open of the same real camera fails
  (the emulator allows it). Disconnect in one Typhon before connecting in
  the other.
- **Capture folders.** Both Typhons reserve run names and create frames
  exclusively, so neither can overwrite the other's frames. Still use
  separate capture folders while trying the new one: later stages add
  run folders that a flat run of the same name could be confused with.
- **Presets** are per project. Copy `camera_presets.json` into the new
  project if you want them there; never move the original.

## Copying today's classifier library in

The plugin Typhon's store starts empty. To copy today's library in, close
today's Typhon (or at least do not Train or Mark in it), then from the plugin
checkout:

```
python tools\copy_classifier_library.py --dry-run
python tools\copy_classifier_library.py
```

It copies from `~\.council\vault\classifiers` into
`~\Typhon-plugins-vault\classifiers` (`--from` and `--to` choose others). It
only reads the original: nothing there is created, locked, renamed or
changed (a test checks every file's bytes and times). A classifier the
destination already has is skipped, never merged or replaced; each copy is
checked against its original's sha256 and renamed into place in one step.
The copies keep their records, so the plugin Typhon lists them as made by
today's Typhon. Not the apps' own Export: it takes locks and puts versions in
order, which can write into the store it exports from.

## The branch gate

Every commit on the branch passes:

```
python tools\typhon_plugins_gate.py --jobs 6   (about 4 minutes; 4 at a time without --jobs)
python tools\typhon_plugins_gate.py --list
python tools\typhon_plugins_gate.py --check-list
```

- Every existing Typhon test file, **unchanged**: the camera module, capture,
  settings, presets, set-up, Basler, the classifier, timing and crop
  libraries, Barbie v4, and the Designer files that build Typhon or every
  example. Plus the branch's own files (`tests/test_camera_rig.py`,
  `tests/test_typhon_plugins_*.py`, and the plugin contract suites in
  `tests/contracts/`: the camera kind's from stage P2, the others' from
  P3). One pytest process per file, offscreen, in temporary folders.
- `tests/test_gui_policy.py` is in the gate ahead of the next merge from
  `qt-migration`: that branch's policy-gate fix (e40d319, 6884069) makes
  it build and policy-check every example, `typhon_plugins` included, and
  name `frame_camera`, so `--check-list` would stop the gate after the
  merge until the file was listed.
- `--check-list` fails when a test file that names Typhon's modules or
  examples is missing from the list, so one added later cannot be left out
  silently.
- A failure of `tests/test_gui_settings.py::
  test_the_warm_list_test_still_catches_a_path_per_module` alone (known
  flaky) reruns that file once and says so.
- A **Barbie test that fails on the branch is retired at once**, not fixed
  and not left red: its name goes on `RETIRED` in
  `tests/test_retired_tests.py` and in CLAUDE.md's "Retired tests" list with
  the reason, and the test is deleted. A test shared with Typhon loses only
  its Barbie parameters. The rule holds in the `pylon` environment too.
  Retired so far: `tests/test_barbie_v4.py::
  test_connect_accepts_a_listbox_selection` (16 October 2026). It took row
  1 of the camera list to be the simulated event camera, which is wrong
  whenever `PYLON_CAMEMU` lists pylon's emulated Baslers first, and failed
  in the `pylon` environment on `qt-migration` (43b317e) and on the branch
  alike.

At the branch point (43b317e) the gate was 51 files, 2,676 passed, 0 failed,
27 skipped (`tests/test_basler_real.py` collects nothing outside the `pylon`
environment).

After P1 rows 2 and 3 (the plugin host and the timing bench, 0517310) it
was 58 files, 2,926 passed, 0 failed, 27 skipped, at 4 at a time, with no
rerun.

The camera files were also run in the `pylon` environment (`--python`),
with pylon's emulated Baslers (`PYLON_CAMEMU=2`) and OpenEB on the path:
14 files, 608 passed, 0 failed. That includes `tests/test_basler_real.py`
with 47 tests. The gate now passes `--timeout` only to an interpreter that
has pytest-timeout. That environment has not, and before this fix every
file there ran nothing. Those 14 files left out `tests/test_barbie_v4.py`.
The P1 review ran 21 camera files in one `pylon` process and found one
failure there, on the branch point and on the branch alike; that test is
now retired (above).

The whole suite, on 31e0e1e, 8 at a time: 234 files, 8,276 passed, 2
failed, 36 skipped, and `smoke_test.py` passed 1,313 with 0 failed. Both
failures were timing tests under that load, and both passed when run alone:

- the known-flaky test above;
- `tests/test_run_python_fence.py::test_a_small_timeout_still_runs_trivial_code[1]`,
  where a 1 s limit was exceeded.

The two plugin-host fixes after that commit were checked by the gate and
the `pylon` run above.
At `--jobs 6` it was not clean. Two timing tests in
`tests/test_gui_settings.py` failed, and failed again on the rerun:

- the known-flaky one;
- `test_the_list_is_quick_and_a_second_look_is_quicker`, whose cold
  Python Scripts list took 3.4 s against its 3.0 s limit.

The plugin host's modules were 5 more files on Typhon's Python Scripts list
(36 to 41), and made a cold list about 10-20 % slower. Both tests passed
when run alone, and so did the whole file at 4 jobs. On 15 October 2026 the
known-flaky test also failed when run alone on an untouched copy of the P1
row 1 commit (710c87f), with a ratio of 1.84 against the 2.0 it checks for.

The P1 review found that the extra files also cut that test's margin: its
ratio fell from a median of about 2.50 at the branch point to about 2.29
on the branch, against the 2.0 it needs, and it failed in a whole-suite
run. The cause was the rig's import of the host, which the list's static
scan followed although today's Typhon never loads the host. The rig now
imports the host by a name built at run time, so today's Typhon lists 36
files again: the 35 of the branch point and `council_core.camera_rig`.
Measured on 16 October 2026, four alternating pairs, each a fresh pytest
process on `tests/test_gui_settings.py`:

| | Files | Cold list | Known-flaky ratio (must stay above 2.0) |
|---|---|---|---|
| branch point (43b317e) | 35 | 695-722 ms | 2.56-2.67 |
| branch, after the review | 36 | 703-736 ms | 2.80-2.89 |

After P2 rows 1 and 2 (16 October 2026), the gate on 595f5f3, 6 at a time:
63 files, 3,166 passed, 0 failed, 47 skipped, no rerun. 17 of the skips are
the camera contract suite's: its pylon-emulator cases outside the `pylon`
environment, and the one check a simulated event camera cannot be put to
(it always has something to send). In the `pylon` environment, one process
with `PYLON_CAMEMU=2` and OpenEB on the path, on a300ff2: the 23 camera and
Barbie v4 files of the gate and the branch's five camera files (the rig,
capabilities, the registry, the rig fakes and the contract suite), 1,233
passed, 0 failed, 1 skipped (that same check), in 549 s; the contract suite
there ran its pylon-emulator cases and the real-`.raw` replays.

The whole suite after P2 rows 1 and 2, on 9a9a2e3, 8 at a time: 237 files,
8,370 passed, 22 failed, 39 skipped, and `smoke_test.py` passed 1,313 with 0
failed. Six failures were timing tests under that load, and their files
passed whole when run alone: `tests/test_frame_camera.py::
test_the_fps_box_changes_the_recording_rate_mid_capture` (17.9 fps against
21), three in `tests/test_frame_camera_setup.py`
(`test_a_frame_camera_preset_restarts_the_live_view_once`,
`test_a_slow_camera_does_not_hold_the_window`,
`test_a_slow_read_of_the_camera_at_start_is_not_capture_time`), and one
each in `tests/test_council_tab.py` and `tests/test_fanout.py`. The other
16 are `tests/test_pi_remote.py`'s, which fail alone too, and fail the same
on e073a24 (the last commit of P1): their fakes run through WSL's bash
(`WindowsApps\bash.EXE`), which did not find the tests' `C:/` temporary
paths. Neither file touches the camera code.

Run it on a quiet PC. With two gates and a `pylon`-environment run going at
once, two timing-sensitive tests failed and then passed on every run alone:
the known-flaky one above (twice in a row) and
`tests/test_basler_trigger.py::test_an_exact_run_from_line1_saves_one_frame_per_edge_and_counts_the_rest`,
which reads the trigger line as soon as the third frame is grabbed, before
it may have been counted ("3 triggers · 2 ok").

## The plugin host (stage P1 row 2)

The rig is the plugin host: `frame_camera._RIG.plugins`, made on first use.
What it does, the API it offers plugins (0.1) and what each later stage
adds are in `docs/typhon_plugin_api_notes.md`. For running the two Typhons
side by side, three things matter:

- **Nothing is wired yet.** Today's camera code never calls the host, so
  "Typhon (plugins)" still behaves exactly like today's Typhon. A test runs
  a whole capture and finds the host never made or imported.
- **A new "Typhon (plugins)" project gets `typhon_plugins.json`** beside its
  `app.py`. The file holds the app-wide base set copied as it is when the
  project is made (question 1), and the plugin home it was made with,
  written home-relative (`~/Typhon-plugins-app/typhon`). The Designer and
  `run_example_gui.py` refuse to build it when the plugin home would be
  under `~\.council`, which happens when `TYPHON_PLUGIN_HOME` is unset. The
  launcher sets it.
- **Plugins from someone else** go in `~\Typhon-plugins-app\typhon\plugins\`,
  one folder each (question 7). They are listed as "needs approval" and do
  not run until stage P5.

## Frame timing with a slow plugin (stage P1 row 3)

The design (10.2) asked for the cost of a live plugin to be measured
**inside the real Qt Typhon** before deciding whether the core should set
a finer timer while it records (stage P3 row 5). It was measured on 15
October 2026 with `tools/typhon_plugins_timing.py`.

**How the runs were made.** Each run was its own process. The "Typhon
(plugins)" app was built for Qt, and its window was made offscreen and
never shown, with its live view, slider and capture code running as usual.
The simulated frame camera ran at 150 fps on a fixed sensor clock: frame i
was due at t0 + i/150 s, and a grab thread that fell behind got the waiting
frames at once, as from a camera's buffer. Each run was a 4 s capture
through the app's own Start and Stop, saving PNGs. The plugin was on the
rig's host and was fed every live frame on its own thread through a
`LiveFeed`. It was busy for the given time per frame in pure Python, so it
held Python's lock. There were 3 runs per row. The laptop was an HP OMEN 16
(i7-14700HX, 32 GB) with Python 3.11.14 and PySide6 6.10.2, running its
usual background programs (CPU about 36 % busy). "Late" means a frame
reached the end of the grab thread's step more than one frame period
(6.7 ms) after it was due. All figures are measured with simulated cameras.

| Settings | Plugin per frame | Late frames | More than 2 periods late | Median lateness | p99 lateness | Frames read in the 4 s |
|---|---|---|---|---|---|---|
| today's (switch interval 5 ms, timer not asked for) | none | 0 of 599-601 | 0 | 0.9-1.0 ms | 2.6-4.4 ms | 601-604 |
| today's | an empty `on_frame` | 0-3 of 599-602 | 0 | 1.0-1.1 ms | 3.2-5.0 ms | 600-606 |
| today's | 5 ms | **512-544** of 585-596 | 482-501 | 38-45 ms | 76-108 ms | 586-597 |
| today's | 20 ms | **all** of 213-232 | 212-229 | 1.1-1.2 s | 2.4-2.6 s | **215-233 of 600** |
| today's | 20 ms, capped at 25 % | 103-106 of 599-601 | 45-48 | 1.1-1.3 ms | 17 ms | 600-606 |
| switch interval 1 ms only | 5 ms | 452-595 of 595-599 | 265-592 | 13-72 ms | 32-100 ms | 598-602 |
| switch interval 1 ms only | 20 ms | 206-221 of 226-315 | 172-214 | 0.37-1.1 s | 1.9-2.5 s | 227-324 of 600 |
| switch interval 1 ms only | 20 ms, capped | 90-116 of 596-602 | 41-54 | 1.2-1.7 ms | 17-19 ms | 599-604 |
| timer 1 ms only | 5 ms | 451-575 of 600 | 225-454 | 11-20 ms | 28-41 ms | 602-604 |
| timer 1 ms only | 20 ms | 346-436 of 349-437 | 344-436 | 0.62-0.70 s | 1.1-1.7 s | 350-438 of 600 |
| timer 1 ms only | 20 ms, capped | 80-95 of 600-603 | 22-31 | 1.2-1.3 ms | 16-17 ms | 602-604 |
| **both at 1 ms** | none | 0-1 of 599-600 | 0 | 0.9-1.0 ms | 2.5-2.9 ms | 601-603 |
| **both at 1 ms** | an empty `on_frame` | 0-4 of 600-602 | 0-1 | 1.0-1.1 ms | 3.0-4.4 ms | 602-603 |
| **both at 1 ms** | 5 ms | **0-2** of 600 | 0 | 1.7-1.9 ms | 4.5-5.4 ms | 601-602 |
| **both at 1 ms** | 20 ms | 147-223 of 599-601 | 32-85 | 4.5-5.1 ms | 29-138 ms | 600-603 |
| **both at 1 ms** | 20 ms, capped at 25 % | **17-23** of 599-603 | 0-1 | 1.1-1.2 ms | 8-10 ms | 601-605 |

In the table, "timer 1 ms" means `timeBeginPeriod(1)`, together with the
Windows 11 request to honour it with no visible window.

**Measured again in the P1 review (16 October 2026).** The review re-ran
the bench on the same laptop and could not reproduce the 5 ms rows: today's
settings with a 5 ms plugin made 0 of 600 frames late. The load on the PC
was not recorded on 15 October, when two gates ran the same day. So the
bench now records it in every run (`cpu_busy_pct` from GetSystemTimes, the
busiest other programs through psutil), and it was run again: 2 runs per
row, 4 s at 150 fps, the whole PC 5.6-30.9 % busy during the captures
(about 10 % in most; antivirus and indexing in the busiest), Windows' timer
at 1.0 ms in every run.

| Settings | Plugin per frame | Late frames | More than 2 periods late | p99 lateness | Frames read in the 4 s |
|---|---|---|---|---|---|
| today's | none | 0 of 602-603 | 0 | 1.3-1.6 ms | all |
| today's | 5 ms | **0** of 602 (PC 14-31 % busy) | 0 | 3.6-4.7 ms | all |
| today's | 20 ms | **all** of 400-439 | 399-439 | 1.1-1.3 s | **400-439 of 600** |
| today's | 20 ms, capped at 25 % | 80-89 of 599-601 | 19-20 | 14.6-14.7 ms | all |
| switch interval 1 ms only | 5 ms | 0 of 600-603 | 0 | 1.4-1.5 ms | all |
| switch interval 1 ms only | 20 ms | 103-121 of 601-602 | 9 | 14.3-15.0 ms | all |
| switch interval 1 ms only | 20 ms, capped | **14-17** of 601-602 | 0-1 | 7.6-8.0 ms | all |
| timer 1 ms only | 5 ms | 0-7 of 600 | 0 | 1.8-6.9 ms | all |
| timer 1 ms only | 20 ms | **all** of 383-395 | 382-394 | 1.4-1.5 s | **383-395 of 600** |
| timer 1 ms only | 20 ms, capped | 81-87 of 600-602 | 15-16 | 14.6 ms | all |
| both at 1 ms | none | 0 of 599 | 0 | 1.5-1.6 ms | all |
| both at 1 ms | 5 ms | 0 of 599-602 | 0 | 1.3-1.5 ms | all |
| both at 1 ms | 20 ms | 104-112 of 598-603 | 6-10 | 12.7-14.7 ms | all |
| both at 1 ms | 20 ms, capped | **9-17** of 599-601 | 0-1 | 7.7-8.1 ms | all |

**What it says, taking both days together.**

- **A plugin that is always busy starves the grab thread at today's
  settings.** At 20 ms a frame it read only 215-233 (15 October) or
  400-439 (16 October) of the 600 frames due, up to 1-2.6 s behind; on a
  real camera with a buffer of a few frames, the rest would have been
  lost. This held on both days.
- **The 1 ms switch interval is what prevents that, on this PC.** With it,
  every frame was read and 17-20 % of them were late (p99 under 15 ms) on
  16 October. The 1 ms timer request alone did nothing visible: the
  20 ms plugin starved the grab thread just as at today's settings, and
  with both set the numbers were those of the switch interval alone. But
  other programs already hold this laptop's timer at 1 ms, so the timer's
  own effect is still unmeasured. "P3 row 5 must set both" is not
  established.
- **The 25 % duty cap is the second half.** With the switch interval it
  left 9-17 of 600 late on 16 October (p99 about 8 ms). On 15 October it
  left 17-23 with both settings, but 90-116 with the switch interval
  alone. At today's settings it still left 80-106 late. So the cap is now on by
  default for live plugins (design 3.11), and heavy work stays off the
  live path: classification after Stop is the default (question 5).
- **A plugin busy 5 ms of every 6.7 ms depends on what else the PC does.**
  On 15 October it made 86-92 % of frames late at today's settings and
  75-99 % with either setting alone; on 16 October, with the PC 6-31 %
  busy, it made 0-7 late with any setting. Read the 5 ms rows of the first
  table with that in mind.
- **Still to measure before P3 row 5:** a quiet PC where nothing holds the
  timer at 1 ms (the bench PC), with the spread between runs and the load
  beside each, to tell whether the timer request matters at all.

With no plugin, or with an empty one, the numbers are those of today's
Typhon on both days (0-4 late), so the host and the feed cost nothing
visible.

To measure again (about 5 minutes), with `--timers default,1ms,switch,period`
for the full table; each row printed says how busy the PC was:

```
python tools\typhon_plugins_timing.py --out timing.json
```

## Progress

| Stage, row | State |
|---|---|
| P0 row 1: branch, stable checkout, side vault and plugin home, pinned classifier store, gate | done |
| P0 row 2: Update from example writes nothing until it can finish | a bug fix for `qt-migration`, merged in when it lands |
| P1 row 1: `Rig`, `CameraSlot`, `_LIVE` as a view | first step: `council_core/camera_rig.py`; `frame_camera._LIVE` is a view over the rig (per-connection fields on the active slot, app fields on the rig); functions still lock with the module's `_LOCK` and act on the active slot |
| P1 row 2: the plugin host on the rig | done (`docs/typhon_plugin_api_notes.md`): manifest reader, built-in list and registry, third-party folders listed (loaded from P5), lazy loading, guarded calls and the three fault endings, the watchdog, contexts, events, the UI queue, live feeds, the base set and project lists (Q1, Q14), `TYPHON_PLUGIN_HOME`, `typhon_plugin_api` 0.1; seven empty built-in adapters; nothing wired to a feature |
| P1 row 3: frame timing inside the real Qt Typhon | done (above), measured twice: a 1 ms switch interval and the duty cap (now on by default) keep an always-busy plugin from starving the grab thread; whether the 1 ms timer request matters is still to measure on a PC where nothing else holds it, before P3 row 5 |
| P1 row 4: `typhon_plugins.gspec` registered | done: typhon's wireframe under the new name |
| P1 review (16 October 2026) | fixed: a gate bound to its own run's writer (a late fault no longer ends the next run; a `confirm_saves` fault ends its run with the reason); the frame at a gate fault saved and marked; loading outside the host's lock; hook lookups guarded; any exception counted; the watchdog starting itself; a damaged project list running only the built-in cameras; nothing written under `~\.council`; run-file names holding a core ending refused; too many files said as such; frames a plugin cannot unlock; the duty cap on by default; `has()` only for hooks Typhon calls; the host off today's Python Scripts list; `tests/test_gui_policy.py` in the gate; a Barbie test retired. Two decisions to confirm are in `docs/typhon_plugin_api_notes.md` ("Decisions") |
| P2 row 1: Basler, Prophesee and Simulated as adapter camera plugins | done (plugin API 0.2, `docs/typhon_plugin_api_notes.md` "Camera plugins"): adapters over today's Backend, Device and Provider classes, which do not move; capabilities in the manifest, confirmed by the opened camera and kept by its slot; `frame_camera`'s four event-camera checks, the simulated camera's settings and the Capture tab ask capabilities; `Provider.extra_stops` (a Basler's other triggers); the host's camera registry (the scan through the plugins is today's scan); category hints that merge into today's tables; the raw view as the Prophesee plugin's review source; `cameras.backends()` and `discover()` unchanged. Today's Typhon still opens cameras as before: the registry is called from rows 3 and 4 |
| P2 row 2: N simulated cameras and the rig's test fakes | done: `COUNCIL_SIM_CAMERAS`; `tests/fake_rig.py` (fake EVK4 units with serials, several at once; Trigger In, sync and the trigger decoder; a trigger line; a sync clock; replay rigs, of real `.raw` files where the Metavision SDK is) |
| P2 contract suite for camera plugins | done: `tests/contracts/test_camera_plugins.py`, passed by every built-in camera plugin (with pylon's emulated Baslers in the `pylon` env) |
| P2 rows 3 and 4 | not started on this branch: `camera_rig.json`, the set-up's camera families as the enabled camera plugins, the wizard's check boxes and each plugin's guide, checks and scan page, `camera_setup.json`'s choice as the project's plugin list |
