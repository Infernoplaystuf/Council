# Typhon: several camera kinds, and an OpenEB installer (16 November 2026)

Andy asked on 16 November 2026: "does the app still force either basler or
EVK support? Also for EVK support lets also include the install for the
OpenEB stuff in there to help users easily add the needed packages".

## Several camera kinds in today's Typhon

Before this, today's Typhon's Camera setup made you choose Basler **or**
Prophesee, and the camera list searched only that kind (plus the simulated
cameras). The only exception was a PC with none of the chosen kind, where
every kind was searched. "Typhon (plugins)" already ticked several kinds.

Now today's Typhon uses the same check-box wizard (`run_families_wizard`)
with its built-in kinds (`camera_setup.builtin_families()`: Basler with its
scan page, Prophesee EVK4).

- Saving: `save_families`, still FORMAT 1. "camera" holds the first kind
  ticked, so a reader of the old one-choice file (`load_choice`,
  `current_choice`) still works.
- `frame_camera._current_kinds()` reads every built-in kind ticked
  (`camera_setup.builtin_choices`, or the old single choice).
- `_backends_for` takes one kind or a list. `list_cameras` searches all of
  them, and falls back to every kind when none of them is found.
- The Command Prompt's "set up for:" line names every kind ticked.
- A project set up the old way opens unchanged and is not asked again.
- `CameraSetupWizard` and `run_wizard` (one choice) are kept but no longer
  used by Typhon.

## The OpenEB installer

`council_core/openeb_install.py` (standard library only, runnable as a
file). How to start it:

- `python -m council_core.openeb_install` in the Council.
- `install_openeb.bat` in every exported camera app. `app_export` copies the
  script into `_council\council_core\` and writes the .bat when the app uses
  `council_core.cameras`.

It follows Prophesee's Windows OpenEB page, with OpenEB 5.2.0 and vcpkg
2024.11.16, for the Python it runs in:

1. Checks:
   - Windows, 64-bit.
   - Python 3.10-3.12.
   - Git.
   - CMake. It only notes a version other than 3.26.
   - Visual Studio 2022 with the C++ tools, found by vswhere.
   - Long paths.
   - A missing tool stops it with the download link.
2. `git clone --branch 5.2.0`.
3. The vcpkg zip, bootstrap, OpenEB's `vcpkg-openeb.json`, and
   `vcpkg install --triplet x64-windows`. This is the long step.
4. pip: numpy only. OpenEB's whole `requirements_openeb.txt` pins old
   versions (opencv-python 4.5.5, numba < 0.60) that would replace Typhon's,
   so it is installed only with `--all-python-packages`.
5. cmake configure, build and install:
   - `-DCMAKE_INSTALL_PREFIX=<dir>\install`
   - `-DPYTHON3_SITE_PACKAGES=<dir>\install\site-packages`
   - `-DPython3_EXECUTABLE=<this python>`
   - `-DBUILD_TESTING=OFF`
6. It writes `%LOCALAPPDATA%\Council\openeb.json` ({"root": ...}), then
   checks that `metavision_hal` imports.
7. The USB driver:
   - With `wdi-simple.exe` (given by `--driver`, or found in `<dir>` or
     Downloads), it runs the three commands as administrator through
     PowerShell `Start-Process -Verb RunAs`.
   - Without it, it prints the download link and the commands.

Other behaviour:

- The default folder is `C:\openeb`, kept short for vcpkg's deep paths.
  `--dir` chooses another.
- Each finished step leaves a `.council_done.<step>` marker, so running it
  again carries on after a stop.
- `--dry-run` prints every step and command and runs none.
- `--skip-driver` leaves the driver out.

`council_core/openeb_env.py` finds the build so that Typhon needs no PATH
editing:

- `cameras.EvkBackend.sdk()` calls `prepare()` before
  `import metavision_hal`.
- It looks in `COUNCIL_OPENEB_DIR`, then the pointer file, then
  `C:\openeb`.
- It adds the build's `bin` to PATH and as a DLL folder.
- It sets `MV_HAL_PLUGIN_PATH` and `HDF5_PLUGIN_PATH` unless the user set
  them, and puts the build's site-packages on `sys.path`.
- It only changes this process.

The EVK4's install steps in Camera setup now have the installer as step 2,
with a Copy button for its command. The other steps moved down one, and the
checks' "(step N)" references moved with them.

Tests: `tests/test_openeb_install.py` (in the branch gate), plus updates to
`test_camera_wizard.py`, `test_camera_setup.py`,
`test_typhon_plugins_setup.py` and `test_app_export.py`.

## Not done / not tried

- Nothing has run on Windows yet: no OpenEB build, no driver install, no
  EVK4.
- Prophesee's page names CMake 3.26. A newer CMake is allowed with a note,
  since the page says newer ones *may* not work.
- FFmpeg, which Prophesee lists, is not installed. Typhon's live view and
  recording do not need it.
- The pytorch/ML parts of OpenEB are not installed.
