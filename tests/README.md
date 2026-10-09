# tests/

Run everything with the Council's Python:

    python -m pytest tests inferno_local/tests -q

`tests/smoke_test.py` is collected with the rest, and also runs as a plain
script (`python tests/smoke_test.py`), which is how `setup_council.py` and
`installs.txt` use it.

Three guards are on for every test (`tests/conftest.py`; `inferno_local/tests`
imports the same ones):

- `tests/sandbox_vault.py`: a throwaway app folder and vault, never `~/.council`.
- `tests/desktop_guard.py`: nothing may open a window on the desktop (Explorer,
  a browser, `os.startfile`), and Qt runs offscreen.
- `tests/no_tk_guard.py`: **no test may open a Tk window.**

## No Tk windows

The Tk GUIs are deprecated: the Tk console (`council_gui_engine.py`) with its
tabs and dialogs, the `gui_canvas` designer, the Tk wizards, and apps that
`gui_emit` generates for the `"tk"` target. The front end is the Qt one in
`council_qt/` (see `docs/qt_migration/`).

For the whole run, `tkinter.Tk()` (and so `Tcl()` and a default root) and
`tkinter.Toplevel()` raise

    TkWindowRefused: Tk GUIs are deprecated - tests must not open a Tk window (...)

in the pytest process and in every python child a test starts: the guard puts
`tests/_no_tk/` first on `PYTHONPATH`, and its `sitecustomize.py` refuses Tk
in the child at startup. `TkWindowRefused` is a `BaseException`, so the
`except Exception: skip("no display")` that Tk tests wrapped their root in
cannot turn it into a skip. A test that still asks for the old `tk_root`
fixture errors with `fixture 'tk_root' not found`. Importing `tkinter` or a
Tk module, and checking the code the Tk target generates as text, are still
fine. Set
`COUNCIL_NO_TK_LOG=<file>` to have every refusal recorded there (pid, test,
call); in a clean run only `tests/test_no_tk_guard.py` appears in it.

When a test you are writing, or one merging from another branch, trips it:

- **It only tests the Tk UI**: remove it.
- **It also checks toolkit-free behaviour** (council_core, the engine, the
  generator, data or vault code): test that without a window, or on the Qt
  equivalent (`council_qt/`, or the Qt runtime in `gui_emit_qt.py`, which
  `tests/test_gui_qt_widgets.py` drives directly), then drop the Tk part.
- **It runs a generated app only to check something that is not Tk's**:
  build it with `target="qt"` (`run_example_gui.build(..., target="qt")`,
  `gui_emit.emit(..., target="qt")`) and drive it offscreen — see the drivers
  in `tests/test_gui_stop.py`, `tests/test_gui_errors.py` and
  `tests/test_frame_classes.py`. Note that `run_example_gui.py` still builds
  for `tk` when no target is given.

On 2026-10-09 this was applied to all 72 tests that made a Tk window: the
ports and Qt retargets are in the commits that removed them.
