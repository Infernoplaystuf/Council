# The Council — notes for Claude Code

## The Tk shell is retired

The Qt app (`council_qt/`, with `council_core/` behind it) is the only app. The
old Tk shell was deleted on 2026-10-23: `council_gui_engine.py`,
`council_modules.py`, `onboarding.py`, the Tk-only modules beside them and
`Version_History/` (`tests/test_retired_tests.py` lists them and fails if a
merge brings one back). The Tk windows that sat in otherwise shared modules
(`vault_agent.py`, `sage_agent.py`, `apothecary_engine.py`, `gui_wizard.py`,
`system_panel.py`, `crash_reporter.py`, `branding.py`) were cut out; their pure
parts stay. The saved-model helpers the Qt app used from `onboarding.py` are
now `council_core/backend_settings.py`. The launchers' `--tk` /
`COUNCIL_UI=tk` only print that Tk was retired, and `council.spec` builds
`council_qt.py`. Git history has the deleted code.

- Don't bring any of it back, and don't write new code that imports `tkinter`.
- When something useful only existed there, rebuild it in `council_core/` (no
  tkinter) and use it from the Qt app.
- Don't write tests that need tkinter or compare the Qt app with the Tk shell.
- The GUI Designer's Tk *target* (it can generate a tkinter app for the user:
  `gui_emit.py`, `python_envs.py`) is a feature, not the shell, and stays.
- Still to clean up: `tests/smoke_test.py` has checks that import
  `council_gui_engine` and now skip themselves.

## Running the tests

`python tests/run_all.py` runs the whole suite one file per process, four at
a time (about 7 minutes; serially in one process it stalls for hours). The
Tests workflow (`.github/workflows/tests.yml`) runs exactly that on Windows
with Python 3.11 for every pull request into `qt-migration` and every push to
it. Keep it green: a failing file there is a failing file on the user's PC.

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
- Retired with the Tk shell (2026-10-23), each needing a deleted Tk module
  (the reasons are in `tests/test_retired_tests.py`):
  - whole files `tests/test_gui_canvas.py`, `tests/test_gui_tab.py`;
  - `test_the_tk_entry_applies_it_before_council_engine_is_imported`
    (`test_saved_model_path.py`); `test_the_cog_is_point_for_point_the_tk_one`,
    `test_the_flame_is_point_for_point_the_tk_one` (`test_splash.py`);
    `test_gui_canvas_still_exports_the_table` (`test_designer_canvas_qt.py`);
    `test_importing_the_tk_module_after_the_pin_keeps_qt_memory`
    (`test_memory_scope.py`); `test_the_close_work_is_shared_with_the_tk_shell`
    (`test_council_qt_foundation.py`, now
    `test_the_qt_entry_does_the_close_work`);
    `test_the_tk_writer_uses_the_max_path_safe_name_and_the_stamp`
    (`test_nx_containment.py`);
  - `test_council_core.py`: `test_both_front_ends_call_the_same_function`,
    `test_every_extracted_operation_is_used_by_both_front_ends`,
    `test_the_data_operations_are_used_by_both_front_ends`,
    `test_both_front_ends_search_through_the_shared_module`,
    `test_the_jobs_are_used_by_both_front_ends` (their Qt halves are now the
    `test_the_qt_tab_*` tests), and
    `test_the_engine_no_longer_carries_its_own_clone_implementation`,
    `test_the_engine_no_longer_carries_the_import_helpers`,
    `test_the_import_workers_no_longer_touch_tk_variables`,
    `test_the_engine_keeps_the_names_the_smoke_suite_uses`;
  - `test_transcript.py`: `test_the_tk_shell_renders_through_the_shared_module`,
    `test_every_policy_decision_is_made_in_one_place`,
    `test_the_tk_shell_no_longer_repaints_an_error_after_writing_it`,
    `test_both_transcripts_are_configured_from_the_same_description`,
    `test_the_tk_helpers_apply_segments_without_reinterpreting_them`,
    `test_the_engine_builds_no_widget_at_import`,
    `test_the_tk_shell_writes_exactly_what_render_says`,
    `test_the_two_transcripts_receive_identical_writes`,
    `test_the_widget_is_left_read_only`,
    `test_the_deferred_turn_is_still_captured`,
    `test_phases_and_tokens_stay_out_of_the_stored_conversation`,
    `test_every_speaker_the_engine_actually_uses_has_a_colour`;
  - `test_vault_setup.py`: `test_the_tk_engine_calls_the_same_functions`,
    `test_tk_moves_an_upgraders_files`, `test_tk_sets_up_the_data_folders`;
  - `test_graph_export.py`: `test_every_export_call_site_handles_the_refusal`,
    `test_the_tick_is_guarded_by_the_refusal_handler`;
  - `test_script_takeover.py`: the eight `test_tk_*` door tests;
  - `smoke_test.py`: `test_db_wizard_url_assembly`, `test_no_bare_re_in_gui`,
    `test_plots_pane_core`; and (2026-10-26, they failed instead of skipping)
    `test_dream3d_convert_to_python_routing`, `test_dream3d_tab_wiring`,
    `test_ui_pump_survives_a_bad_item`,
    `test_known_field_names_harvests_real_labels`,
    `test_an_adjective_does_not_break_a_field_search`,
    `test_rag_recall_and_coverage`. `test_plain_english_is_not_a_file_path`
    now checks `council_core.turn_context`, where that code moved.
- Retired with the Code and IDE tabs (2026-10-30; the Build tab replaced
  both, and `council_qt/tabs/code.py` and `ide.py` are gone):
  - whole files `tests/test_code_tab.py` (its checks are in
    `test_build_agent.py`) and `tests/test_ide.py` (its core half is
    `test_ide_jobs.py`);
  - `test_the_tab_is_registered_beside_code` (`test_build_tab.py`, now
    `test_the_tab_replaces_the_code_and_ide_tabs`) and
    `test_an_ide_snapshot_is_reported_in_the_council_transcript`
    (`test_append_transcript.py`, now
    `test_a_build_snapshot_is_reported_in_the_council_transcript`).

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
- Saved Ledgers (runs kept, named and grouped by template and/or project) and
  "Update with new files" (only new or changed files; what the Ledger holds is
  accepted again, a value that differs is flagged):
  `docs/handoff/ledger_saved_2026-10-23.md`.
- Ledger groups (parts by part name, date or source folder; fields tracked, not
  tracked or Yes/No) and the full and per-group workbooks, a sheet per part:
  `docs/handoff/ledger_groups_2026-10-26.md`.
- The Ledger's pictures (every kind in an .xlsx, placed in a cell too) and the
  clipboard a protected workbook's pictures go through (what the user had is
  kept and put back, whose copy it is told by its owner):
  `docs/handoff/ledger_pictures_2026-10-26.md`.
- The Build tab (files, editor, and the chat beside the code being built in five
  steps; the agreed design and what each step does; it replaced the Code and
  IDE tabs): `docs/handoff/build_tab_2026-10-29.md`.
- The Connections tab's Contents layer (every value of every spreadsheet, text
  document, PDF, Word and JSON file under its header or label, whatever the field rules; search over it; the Contents tab):
  `docs/handoff/kg_contents_2026-10-26.md`.
- Fixes from the synthetic Ledger / Connections test (no 500-row limit,
  off-template sheets held back, edited filled templates read, PDF tables and
  scans, CSV sources, parts in sheet titles, Q&A part ranking, proposed people
  rules, unrelated sheets skipped) and what is still open:
  `docs/handoff/synthetic_test_fixes_2026-10-27.md`.
- Tables as their own nodes in Connections (LISTS, SHARES_*, REFERENCES), links
  between documents and tables related by meaning (a local embedding model,
  suggested), and a model reading each table to propose column rules:
  `docs/handoff/kg_tables_related_2026-10-27.md`.
- The example Qt apps the models read when they write Qt code (ten at most;
  each passes the generated-app gate; the Build tab's Agent mode "Qt app builder" gets
  the best match whole): `examples/qt_apps/README.md`, read by
  `gui_reference.py`. Wireframe examples for Describe stay in `examples/gui/`
  (`gui_examples.py`).
- The Council rebuilding Typhon (layout, wiring and the hand-built widgets,
  each graded apart; `run_typhon_recreate.py`, the oracle stub, the report):
  `docs/handoff/typhon_recreate_2026-10-22.md`.
