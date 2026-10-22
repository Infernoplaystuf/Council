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
- `test_the_tk_console_applies_them` and
  `test_the_tk_console_applies_them_through_the_same_function`
  (`tests/test_launch.py`): ran, and read the source of, the Tk console's
  engine-settings loader. The Qt launch's own engine-settings tests stay.

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

## Where things are described

- The knowledge graph (Connections tab), agent profiles (Agent Creator tab) and the
  "Set up a Pi" wizard: `docs/handoff/knowledge_graph_desktop.md` (decisions, plan and
  progress; it was written for the desktop's `knowledge-graph` branch).
- The graph's Excel / Ledger sources, inspector initials and outside folders:
  `docs/handoff/kg_excel_2026-10-16.md`.
- The Ledger for table templates (one row per record, highlights, lookups, the
  model per header layout): `docs/handoff/ledger_records_2026-10-21.md`.
- The Connections tab working like Morphik (any entity type, a model reading every
  passage, named graphs, Ask, the picture, the graph in the Council's answers; which
  earlier decisions it supersedes): `docs/handoff/kg_morphik_2026-10-21.md`.
- Skipping files that stall the app (Ledger, Connections, Vault; the skip list,
  the time limit, "Retry skipped files"): `docs/handoff/skipped_files_2026-10-22.md`.
  A new bulk reader goes through `council_core.file_guard.read`.
- The Ledger's "Review by clicking…" window (each value beside its highlighted
  template and source cells, answers saved into the review workbook):
  `docs/handoff/ledger_review_2026-10-22.md`. "Diagnose a file…" is in
  `docs/handoff/skipped_files_2026-10-22.md`.
