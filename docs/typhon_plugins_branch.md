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
| Plugin home | (none yet) | `~\Typhon-plugins-app\typhon` |
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
   store.
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
  `tests/test_typhon_plugins_*.py`; the plugin contract suites join from
  stage P3). One pytest process per file, offscreen, in temporary folders.
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
  its Barbie parameters.

At the branch point (43b317e) the gate was 51 files, 2,676 passed, 0 failed,
27 skipped (`tests/test_basler_real.py` collects nothing outside the `pylon`
environment).

Run it on a quiet PC. With two gates and a `pylon`-environment run going at
once, two timing-sensitive tests failed and then passed on every run alone:
the known-flaky one above (twice in a row) and
`tests/test_basler_trigger.py::test_an_exact_run_from_line1_saves_one_frame_per_edge_and_counts_the_rest`,
which reads the trigger line as soon as the third frame is grabbed, before
it may have been counted ("3 triggers · 2 ok").

## Progress

| Stage, row | State |
|---|---|
| P0 row 1: branch, stable checkout, side vault and plugin home, pinned classifier store, gate | done |
| P0 row 2: Update from example writes nothing until it can finish | a bug fix for `qt-migration`, merged in when it lands |
| P1 row 1: `Rig`, `CameraSlot`, `_LIVE` as a view | first step: `council_core/camera_rig.py`; `frame_camera._LIVE` is a view over the rig (per-connection fields on the active slot, app fields on the rig); functions still lock with the module's `_LOCK` and act on the active slot |
| P1 row 4: `typhon_plugins.gspec` registered | done: typhon's wireframe under the new name |
