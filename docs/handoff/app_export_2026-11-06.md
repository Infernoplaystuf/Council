# Exporting a Designer-built app to its own folder (6 November 2026)

Andy: "I would like for GUIs like Typhon created within the Council to have
their own folders that I can then copy and move to other machines and run
the GUI there … the Council is the development suite for Typhon but I can
export it to other machines."

## How

- GUI Designer: open the project, **Export app…**, choose a folder. The app
  gets a new folder there (never an existing one), and is then checked to
  start with no Council. A Yes to "Copy this PC's classifiers…" copies the
  classifier store into it.
- Command line: `python tools/export_app.py <project name or folder> <dest>
  [--name N] [--with-classifiers] [--no-check]`.

## What the folder holds (`council_core/app_export.py`)

- The project folder as generated (its camera set-up and presets too), with
  main.py's path block rewritten to `_council` beside it.
- `_council/`: exactly the Council files the app's imports reach — read from
  the source (function-level imports, `import_module` of a constant or of
  `f"{__package__}.x"`, `_REEXPORTS` tables), plus each built-in Typhon
  plugin when the plugin host is reached, their plugin.json files, and the
  icon when branding is. Today's Typhon: 38 files (~1.5 MB); Typhon
  (plugins): 68. Never the engine, models, Ledger or Connections.
- `_council/SOURCE_COMMIT`: the Council commit; `camera_record.source_commit`
  reads it where there is no .git, so a capture's run record still names it.
- `run.bat` / `run.sh`: `COUNCIL_VAULT_ROOT`, `COUNCIL_APP_DIR`,
  `TYPHON_PLUGIN_HOME`, `FRAME_CLASSES_STORE` all under `data\` beside it;
  An activated venv's or conda env's Python is used first (Andy, 13
  November: started from inside an environment, use it); then `PYTHON` (a folder is followed to its python.exe, and a
  PYTHON that does not start Python is passed over: a lab PC had it set to
  the Python313 folder, 13 November), else `python` on PATH, else the `py`
  launcher, each tried with `-c` first; none found, or the app stops with an error, it says so and
  pauses (a double-clicked window does not vanish).
- `check.bat` / `check.sh` run `check_setup.py` (standard library and the
  folder's own files; a throw-away data folder; changes nothing) on the
  other machine: Python version, required and optional packages, every
  `_council` file loads, the window builds, whether pylon is there and
  which cameras it and the app see; `PROBLEM:` lines say what to fix.
- `requirements.txt` (PySide6, the project's declared packages; camera SDKs
  and other optional ones as comments), `export.json`.
- `README.txt` (Andy: it should hold everything needed to set up and
  launch): run it; set up a new machine (Python with PATH, pip, pylon /
  pypylon / CXP and the grabber, check.bat); no internet there (`pip
  download -d wheels` on a connected PC with the same Python, then
  `--no-index --find-links wheels`); if something goes wrong (no Python,
  no module, no camera, window closes, moving to a new export keeping
  `data\`); what is in the folder.

## Checked

`ae.check(folder)` runs a separate `python -I -B` in the folder, with a
throw-away data folder: imports every file under `_council`, builds the
app's window, lists cameras when it is a camera app, and fails if anything
is missing or was loaded from the Council's folder. Tests:
`tests/test_app_export.py` (today's Typhon and Typhon (plugins), refusals,
the Designer button).

## Also fixed

A generated main.py looked for `council_gui_engine.py` to find a moved
Council; that file went with the Tk shell, so a moved Council was never
found. The marker is now `council_qt.py` (`gui_emit._ROOT_MARKER`); apps get
it at their next Generate.

## Open

- The exported app needs Python and the packages on the other machine; a
  one-file build (PyInstaller) was not asked for.
- Run on a real second machine: not done yet.
