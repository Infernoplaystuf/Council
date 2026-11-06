# The classifier as a pop-out plugin, and datasets (2 November 2026)

Branch `typhon-plugins` (built on `claude/project-thread-vqal34`, merged later).
Only "Typhon (plugins)" projects change; today's Typhon is untouched.

## What Andy asked for, and the answers

- The classifier becomes an optional plugin with a button that pops all of
  it out into its own window.
- A button beside PNG / Raw sends pictures to a dataset; datasets can then
  be sent to plugins, the way ORNL's Peregrine picks images.
- Answers (2 November): build on a new branch and merge later; datasets
  hold **references**, not copies; labels stay in the classifier (a dataset
  is a picked set of pictures, not a labelled one); no other Peregrine
  extras for now.

## What was built

- `council_core/datasets.py`: `<vault>/datasets/<name>/dataset.json`
  (schema 1). Each entry is a reference: path, folder, name, run, frame,
  camera, crop box, when added. Add (a frame, a range, a whole run; a
  frame already there is not added twice), remove, merge, rename, delete
  (moved to `datasets/.deleted/<name>_<stamp>`, never erased). One lock file
  per dataset, atomic writes. The pictures themselves are never touched.
- Plugin API 0.3 additions (`docs/typhon_plugin_api_notes.md`, "The
  classifier's window and datasets"): `hook.panel`, `hook.take_dataset`, the
  "datasets" contribution, `Dataset` / `DatasetFrame`, and
  `context.datasets/dataset/show_panel/image_folder`; `current_frame` now
  answers in a plugins project.
- `typhon_plugins/classify` (0.2.0): its `panel.py` is the classifier's
  controls, wired to the same 17 `frame_classes` functions as the classic
  wireframe (a test holds the two lists equal), plus a Dataset row: "Mark
  dataset" (`frame_classes.mark_dataset`) and "Classify dataset"
  (`frame_classes.classify_dataset`, which records nothing in runs.jsonl).
  Taller than the window, it scrolls. A dataset sent to it opens the window
  with that dataset picked.
- `council_qt/widgets/plugin_panels.py`: the toolbar strip (port
  `plugin_toolbar`): a button per plugin with a window, and "Plugins…" to
  switch feature plugins on and off per project. Windows hide on close and
  remember their place in the project's `typhon_windows.json`.
- `council_qt/widgets/datasets_window.py`: "Add to dataset…" (this frame, a
  range, or the whole run, into a new or existing dataset) and "Datasets…"
  (thumbnails, filter by run, Remove, Send to a plugin, Merge, Rename,
  Delete asked twice).
- `examples/gui/typhon_plugins.gspec`: the 32 classifier shapes are gone,
  the live view and rows below it are wider, and the two dataset buttons sit
  beside PNG / Raw.

## To use it on an existing "Typhon (plugins)" project

The project keeps its old wireframe until it is updated:

    python run_example_gui.py typhon_plugins --project <name> --update

## Tests

`tests/test_typhon_plugins_datasets.py` and
`tests/test_typhon_plugins_classify_panel.py` (both in the branch gate).
The whole flow (add runs, send, mark, train, classify the dataset, the
Datasets window) was run offscreen end to end.

## Not done / open

- "Classify all frames" while a capture is running was not tried against a
  real camera.
- Labels in datasets, dataset export, and other Peregrine features: not
  asked for yet.
