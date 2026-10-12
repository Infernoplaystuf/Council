# tests/

Run everything with the Council's Python:

    python -m pytest tests inferno_local/tests -q

`tests/smoke_test.py` is collected with the rest, and also runs as a plain
script (`python tests/smoke_test.py`), which is how `setup_council.py` and
`installs.txt` use it.

Four guards are on for every test (`tests/conftest.py`; `inferno_local/tests`
imports the same ones):

- `tests/sandbox_vault.py`: a throwaway app folder and vault, never `~/.council`.
- `tests/desktop_guard.py`: nothing may open a window on the desktop (Explorer,
  a browser, `os.startfile`), and Qt runs offscreen.
- `tests/no_tk_guard.py`: **no test may open a Tk window.**
- `tests/offline_guard.py`: **tests must stay offline** (see "Offline" below).

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

A test may still RUN Tk-target code whose correct behaviour is to open no
window — there the guard is the tripwire. The startup-check tests in
`tests/test_gui_requires.py` run a Tk `main.py` that must exit 3 without
its messagebox under `COUNCIL_PREVIEW_CONTROL` or `COUNCIL_NO_DIALOGS`; if
either switch stops working, the child's `tkinter.Tk()` is refused and the
test fails (exit 1, not 3) instead of a modal opening.

On 2026-10-09 this was applied to all 72 tests that made a Tk window: the
ports and Qt retargets are in the commits that removed them.

## Offline

The Council is offline by design, and so are its tests. For the whole run

- a connection (TCP, or a UDP datagram) to anything but loopback,
- a connection to port **11434** - the real Ollama's - on any address,
- looking up a name other than `localhost` or this PC's own (a DNS query),
  and a reverse lookup of anything but 127.0.0.1 / ::1,

raise

    EgressRefused: tests must stay offline - a connection to 1.2.3.4:443 would leave this PC; only loopback may be reached (...)

in the pytest process and in every python child a test starts: the guard puts
`tests/_offline/` on `PYTHONPATH` right after `tests/_no_tk/` (whose
`sitecustomize.py` runs the next one), the same design as the Tk guard.
`EgressRefused` is a `ConnectionRefusedError` (a name lookup's is a
`socket.gaierror` too), so the code under test sees what it would see with
the network unplugged and runs its own "offline" path. That cannot hide a
test that reached out: every refusal, in this process or a child, is written
to a report, and the autouse `offline_test` fixture **fails the test** it
happened in, naming what was refused and the line that asked. Set
`COUNCIL_OFFLINE_LOG=<file>` to have every refusal recorded there too (pid,
test, what, where); in a clean run only `tests/test_offline_guard.py`
appears in it. `python tests/smoke_test.py` installs the same guard, and its
runner fails a check that tried.

A fake server on loopback works as before - `tests/fake_ollama.py`'s
`FakeOllama` (127.0.0.1, 127.0.0.2 or ::1, on a port of its own), or any
server on `("127.0.0.1", 0)`. A test that must connect to port 11434 itself
(one that puts its own fake there) opts in with `@pytest.mark.allow_ollama_port`
or the `allow_ollama_port` fixture - loopback only, still.

When a test trips it:

- **It lists the installed models** (`local_models.list_local_models`, which
  asks the localhost Ollama for `/api/tags`): stub that, as the Models-tab
  tests do, or point `COUNCIL_OLLAMA_HOST` at a `FakeOllama`.
- **It builds a tab whose worker probes Ollama hosts** (Nodes,
  Connections): give it a stand-in dispatcher or stub its actions - see the
  `window` fixture in `tests/test_council_qt_foundation.py`. Wait for the
  worker before the test ends, or it asks during the next test.
- **It would download something**: stub the download (`urlopen`, and
  `huggingface_hub` - `model_downloader` tries it first when it is installed;
  see `test_model_downloader` in `smoke_test.py`).

Not covered: children started with `-I`, `-E` or `-S`, or with an
environment without `PYTHONPATH`, and programs that are not Python (`git`,
`ssh`, `curl`, `ollama`) - no test starts one that goes online today.
