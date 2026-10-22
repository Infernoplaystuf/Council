# The Council rebuilds Typhon (22 October 2026)

**What you asked for (your words):** "Can you setup a session to have the
council itself to recreate the Typhon GUI?" You chose the whole app (layout,
button wiring and the hand-built widgets), and added: "If it is beyond a local
model today we can identify the problem areas and work to fix those areas."

So this is a benchmark that names problem areas, not a feature in the app. It
uses the Council's own pieces (Describe it, the wiring checks, the policy
gate, Generate) and writes nothing into the repository.

## How to run it (on the PC with the models)

    python run_typhon_recreate.py --vault D:/scratch/vault --stub oracle
    python run_typhon_recreate.py --vault D:/scratch/vault
    python run_typhon_recreate.py --vault D:/scratch/vault --model C:/models/x.gguf

Run the `--stub oracle` line first. It answers every prompt with what Typhon
actually has, needs no model, and should score 100% in every stage. Anything
less is the harness or the machine (a missing package, a test that fails on
the original), not a model. The widget stage runs pytest, so pytest must be
installed in that Python. A full run with a real model is slow: each widget
item is a model call of up to 3000 tokens, and each widget's tests run once
per repair round.

Useful options: `--stages layout,wiring` (skip widgets), `--widgets
pop_out,preset_picker`, `--sections header,timings`, `--controls s12,s21`,
`--repairs 0`, `--no-runtime`.

## What comes out

In `<vault>/typhon_recreate/<time>/`:

- `REPORT.md`: **Problem areas** first, then each stage's score, its table,
  and its findings in plain words.
- `results.json`: everything, for comparing runs or models.
- `layout_typhon.png` and `layout_rebuilt.png`: the original and the rebuild
  side by side, each widget a box coloured by kind.
- `widgets/<name>/<name>.py`: each widget module as the Council wrote it.

The generated projects go in the vault's GUI_Projects, under fresh names.

## The three stages, graded apart

1. **Layout.** `examples/gui/typhon_recreate/sections.json` describes Typhon
   in eight parts, the way a person would. Each part goes through Describe it
   on a canvas that part's size, and the parts are placed where Typhon has
   them. Graded shape by shape against `typhon.gspec` (same kind, near kind,
   same words, overlap), then generated and run offscreen with every control
   pressed.
2. **Wiring.** Typhon's own wireframe with its 41 script links taken off. For
   each control the model is shown the control, the app's ports and a
   shortlist of 14 library functions (frame_camera, frame_classes,
   frame_timing, frame_roi, gui_settings), and picks the function and the
   ports. Checked with the Wiring group's own checks
   (`council_core.designer_wiring.problems`), one repair, then graded against
   Typhon's link. Typhon is then generated with the model's links and run.
   When the shortlist missed the right function, the report says so
   separately from the model's own misses.
3. **Widgets.** Each of the six hand-built modules in `council_qt/widgets/`
   (pop_out, preset_picker, classifier_picker, settings_tabs, capture_review,
   camera_settings_window). The model gets the module's purpose (its
   docstring), the original imports, and for every name the app and the tests
   use, its signature and docstring, never a body. It writes one top-level
   item per call. The module is assembled, passed through the policy gate,
   and graded by running Typhon's own tests against it. Failing tests are
   sent back to the item their traceback points into, for up to two repair
   rounds. Then Typhon itself is built and run with every rebuilt widget in
   place.

Stages are graded apart on purpose. Wiring runs on Typhon's wireframe, not
the rebuilt layout, so "the classifier rows were laid out wrong" and "Rename
was linked to save_as" come out as two findings, not one blur.

**Which widget tests count.** Every test file that imports the widget runs
three times: on the original, on an empty module (every item a stub that
raises), and on the rebuild. Only tests that pass on the original and fail on the empty module
measure the widget, so the score is "N of the tests that depend on this
widget pass on the rebuild".

## What the oracle scored here (cloud container, no model)

Layout 93 of 93 shapes, wiring 41 of 41 links, and each widget all of its
counted tests. The generated layout, the rewired Typhon and Typhon with the
rebuilt widgets each built and ran offscreen with every control pressed.

## What to expect from a local model

The widget stage is the hard one. capture_review is 1100 lines and
camera_settings_window 2560, and their specs (docstrings and signatures) say
much less than the code does. The report will show which items never parse,
which run out of tokens, which specs were trimmed to fit, and which tests
still fail. Those are the areas to work on.

## Files

- `typhon_recreate.py`: the stages, the grading, the oracle, the report.
- `run_typhon_recreate.py`: the command line.
- `examples/gui/typhon_recreate/sections.json`: the plain-English parts.
- `tests/test_typhon_recreate.py`: the harness checked with stubs.
- `run_describe_prompts.run_generated` gained a `prelude` (Python run before
  the probe: the Council on sys.path for a linked project, a rebuilt module
  loaded in place of the original). Its probe now prints a traceback raised
  while the App is built, where it used to exit 1 with nothing on stderr.
