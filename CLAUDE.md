# The Council — notes for Claude Code

## The Tk shell is retired

The Qt app (`council_qt/`, with `council_core/` behind it) is the only app being
worked on. Everything that belongs to the old Tk shell is a deprecated relic kept
only until it is deleted: `council_gui_engine.py`, `council_modules.py`, and any
module or code path that imports `tkinter`.

- Don't build on it, import from it, edit it, or port fixes into it.
- When something useful only exists there, rebuild it in `council_core/` (no
  tkinter) and use it from the Qt app.
- Don't write tests that need tkinter or compare the Qt app with the Tk shell.

## Retired tests

These were deleted on purpose. If a merge from another branch brings one back,
delete it again, unless deleting it breaks something (then keep it, and say why
here). `tests/test_retired_tests.py` fails while one is present and holds the
same list.

- `test_the_standalone_host_matches_the_tk_contract`
  (`tests/test_council_qt_foundation.py`): compared the Qt `StandaloneHost` with
  the Tk one and needed tkinter. Its Qt-only checks are now
  `test_the_standalone_host_surface`.

## Standing rules (all branches)

- Offline by design: localhost only, no telemetry, no cloud LLM calls; nothing leaves
  the PC unless the user opts in. Keep everything local — a networked/server Council is
  only a possible future path; don't build server pieces now.
- Never delete or overwrite user data; never modify existing vault files. Test against
  temporary vaults (`COUNCIL_VAULT_ROOT`) or copies.
- No pickle. Atomic writes. Python 3.11+ compatible.
- Only US-origin models may be recommended (Llama, Phi, Gemma, Granite, OLMo, gpt-oss).
  The Council never downloads models itself.
- Verify offscreen (`QT_QPA_PLATFORM=offscreen`, `COUNCIL_NO_DIALOGS=1`); don't open
  windows on the user's screen unless asked.
- Never push to `main` or `Work-Build`.
- Tests that fail before and pass after; report results faithfully, failures included.
