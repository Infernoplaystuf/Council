"""
Tests that were retired on purpose, and must not come back in a merge.

The Tk shell (council_gui_engine.py, council_modules.py and the rest) is a
retired relic: the Qt app is the only one worked on. A test that compares the
Qt app with the Tk shell, or needs tkinter to run, checks nothing anyone
maintains. An older branch can still carry one, and a merge would quietly
bring it back. This file fails when that happens.

If one reappears after a merge: delete it again, unless deleting it breaks
something — then keep it, and take it off this list with a note in CLAUDE.md
saying why. CLAUDE.md ("Retired tests") lists the same names.
"""
from __future__ import annotations

from pathlib import Path

TESTS = Path(__file__).resolve().parent

#: name → why it was retired.
RETIRED = {
    "test_the_standalone_host_matches_the_tk_contract":
        "compared council_qt.host.StandaloneHost with the Tk "
        "council_modules.StandaloneHost; needs tkinter. Its Qt-only checks "
        "live on as test_the_standalone_host_surface.",
    "test_the_tk_console_applies_them":
        "ran the Tk console's _load_backend_settings (imports "
        "council_gui_engine). The Qt launch's own engine-settings tests "
        "in test_launch.py stay.",
    "test_the_tk_console_applies_them_through_the_same_function":
        "read council_gui_engine.py's source to check the Tk console "
        "applies engine settings like the Qt launch does.",
    # Retired with the Tk shell itself (2026-10-23): each ran, imported or
    # read a deleted Tk module. A "file::name" key is matched in that file
    # only (the name alone is too common to ban everywhere).
    "test_the_tk_entry_applies_it_before_council_engine_is_imported":
        "read council_gui_engine.py (test_saved_model_path.py). The Qt "
        "entry's own checks stay.",
    "test_the_cog_is_point_for_point_the_tk_one":
        "compared council_core.splash_art with splash.py (test_splash.py).",
    "test_the_flame_is_point_for_point_the_tk_one":
        "compared council_core.splash_art with splash.py (test_splash.py).",
    "test_gui_canvas_still_exports_the_table":
        "checked gui_canvas.py re-exported the renderers "
        "(test_designer_canvas_qt.py).",
    "test_importing_the_tk_module_after_the_pin_keeps_qt_memory":
        "imported council_gui_engine (test_memory_scope.py). The pin's own "
        "tests stay.",
    "test_the_close_work_is_shared_with_the_tk_shell":
        "read council_gui_engine.py (test_council_qt_foundation.py); now "
        "test_the_qt_entry_does_the_close_work.",
    "test_the_tk_writer_uses_the_max_path_safe_name_and_the_stamp":
        "ran the Tk Write pipeline (test_nx_containment.py).",
    # test_council_core.py: the "both front ends" checks keep their Qt half
    # under test_the_qt_tab_* names; the Tk-only ones are gone.
    "test_both_front_ends_call_the_same_function":
        "now test_the_qt_tab_indexes_through_vault_ops.",
    "test_every_extracted_operation_is_used_by_both_front_ends":
        "now test_the_qt_tab_uses_every_extracted_operation.",
    "test_the_data_operations_are_used_by_both_front_ends":
        "now test_the_qt_tab_uses_the_data_operations.",
    "test_both_front_ends_search_through_the_shared_module":
        "now test_the_qt_tab_searches_through_the_shared_module.",
    "test_the_jobs_are_used_by_both_front_ends":
        "now test_the_qt_tab_uses_the_jobs.",
    "test_the_engine_no_longer_carries_its_own_clone_implementation":
        "read council_gui_engine.py.",
    "test_the_engine_no_longer_carries_the_import_helpers":
        "read council_gui_engine.py.",
    "test_the_import_workers_no_longer_touch_tk_variables":
        "read council_gui_engine.py.",
    "test_the_engine_keeps_the_names_the_smoke_suite_uses":
        "read council_gui_engine.py.",
    # test_transcript.py: the Tk transcript, read and run.
    "test_the_tk_shell_renders_through_the_shared_module": "Tk transcript.",
    "test_the_tk_shell_no_longer_repaints_an_error_after_writing_it":
        "Tk transcript.",
    "test_both_transcripts_are_configured_from_the_same_description":
        "Tk transcript.",
    "test_the_tk_helpers_apply_segments_without_reinterpreting_them":
        "Tk transcript.",
    "test_the_tk_shell_writes_exactly_what_render_says": "Tk transcript.",
    "test_the_two_transcripts_receive_identical_writes": "Tk transcript.",
    "test_every_speaker_the_engine_actually_uses_has_a_colour":
        "read the Tk engine's _append_transcript calls.",
    "test_transcript.py::test_every_policy_decision_is_made_in_one_place":
        "read council_gui_engine.py.",
    "test_transcript.py::test_the_engine_builds_no_widget_at_import":
        "read council_gui_engine.py.",
    "test_transcript.py::test_the_widget_is_left_read_only":
        "ran the Tk transcript method.",
    "test_transcript.py::test_the_deferred_turn_is_still_captured":
        "ran the Tk transcript method.",
    "test_transcript.py::test_phases_and_tokens_stay_out_of_the_stored_conversation":
        "ran the Tk transcript method.",
    # test_vault_setup.py
    "test_the_tk_engine_calls_the_same_functions": "read the Tk engine.",
    "test_tk_moves_an_upgraders_files": "ran the Tk engine.",
    "test_tk_sets_up_the_data_folders": "ran the Tk engine.",
    # test_graph_export.py: the two callers were Tk modules.
    "test_graph_export.py::test_every_export_call_site_handles_the_refusal":
        "read council_gui_engine.py and grapher_app.py.",
    "test_graph_export.py::test_the_tick_is_guarded_by_the_refusal_handler":
        "read council_gui_engine.py and grapher_app.py.",
    # test_script_takeover.py: the Tk doors.
    "test_tk_typed_take_over_asks_and_acts_on_yes": "Tk door.",
    "test_tk_typed_take_over_no_changes_nothing": "Tk door.",
    "test_tk_under_no_dialogs_never_asks": "Tk door.",
    "test_tk_other_text_goes_on_to_send": "Tk door.",
    "test_tk_every_other_route_answers_without_acting": "Tk door.",
    "test_tk_a_transcription_is_not_typed_text": "Tk door.",
    "test_tk_the_dream3d_box_is_typed_text": "Tk door.",
    "test_tk_the_button": "Tk door.",
    # tests/smoke_test.py
    "test_db_wizard_url_assembly": "the Tk db_connect_wizard.",
    "test_no_bare_re_in_gui": "read council_gui_engine.py.",
    "test_plots_pane_core":
        "the Tk plots_pane.py; test_plots_pane_qt.py covers council_core.plots.",
    # Failed rather than skipped once the Tk shell was gone (2026-10-26, when
    # CI started running the suite): each checked CouncilConsole itself.
    "test_dream3d_convert_to_python_routing":
        "CouncilConsole._PIPELINE_TO_PYTHON_RES, the Tk console's routing.",
    "test_dream3d_tab_wiring": "the Tk console's Dream3D tab methods.",
    "test_ui_pump_survives_a_bad_item":
        "CouncilConsole._poll_ui_queue; the Qt bridge has its own tests.",
    "test_known_field_names_harvests_real_labels":
        "CouncilConsole._known_field_names, which the Qt app never ported.",
    "test_an_adjective_does_not_break_a_field_search":
        "CouncilConsole._resolve_proposed_field, likewise.",
    "test_rag_recall_and_coverage": "council_gui_engine.RAG_PER_ANGLE.",
    # Retired with the Code and IDE tabs (2026-10-30), which the Build tab
    # replaced.
    "test_build_tab.py::test_the_tab_is_registered_beside_code":
        "the Code tab is gone; now test_the_tab_replaces_the_code_and_ide_tabs.",
    "test_append_transcript.py::test_an_ide_snapshot_is_reported_in_the_council_transcript":
        "the IDE tab is gone; now "
        "test_a_build_snapshot_is_reported_in_the_council_transcript.",
}


#: Whole test files retired with the Tk shell (2026-10-23).
RETIRED_FILES = {
    "test_gui_canvas.py": "the Tk designer canvas; needed tkinter.",
    "test_gui_tab.py": "the Tk GUI Designer tab in council_gui_engine.py.",
    # The Code and IDE tabs, retired into the Build tab (2026-10-30). Their
    # checks live on in test_build_agent.py (plan, run, merge, refused merge,
    # report agents, the project dialog, the history benchmark) and
    # test_ide_jobs.py (the trust gate, the run and snapshot helpers).
    "test_code_tab.py": "the Code tab (council_qt/tabs/code.py).",
    "test_ide.py": "the IDE tab (council_qt/tabs/ide.py); its core half is "
                   "test_ide_jobs.py.",
}

#: The Code and IDE tabs, replaced by the Build tab (2026-10-30).
RETIRED_TABS = ("council_qt/tabs/code.py", "council_qt/tabs/ide.py")

#: The Tk shell's own files, deleted 2026-10-23. A merge from an older branch
#: can bring them back, and with them tests that need them.
RETIRED_MODULES = (
    "council_gui_engine.py", "council_modules.py", "onboarding.py",
    "activation_dialog.py", "agent_panel.py", "db_connect_wizard.py",
    "grapher_app.py", "gui_canvas.py", "gui_runwith.py",
    "phase1_ai_model_council.py", "plots_pane.py", "splash.py",
    "tab_grapher.py", "Version_History",
)


def test_no_retired_test_has_come_back():
    found = []
    for path in sorted(TESTS.glob("**/*.py")):
        if path.name == Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for key in RETIRED:
            where, _, name = key.rpartition("::")
            if where and path.name != where:
                continue
            if f"def {name}(" in text:
                found.append(f"{path.relative_to(TESTS)}::{name}")
    found += [name for name in RETIRED_FILES if (TESTS / name).exists()]
    assert not found, (
        "retired test(s) back, probably from a merge — delete them unless "
        "that breaks something (see CLAUDE.md, 'Retired tests'): " +
        ", ".join(found))


def test_the_tk_shell_has_not_come_back():
    root = TESTS.parent
    back = [name for name in RETIRED_MODULES if (root / name).exists()]
    assert not back, (
        "the retired Tk shell is back, probably from a merge — delete it "
        "again (see CLAUDE.md, 'The Tk shell is retired'): " + ", ".join(back))


def test_the_code_and_ide_tabs_have_not_come_back():
    root = TESTS.parent
    back = [name for name in RETIRED_TABS if (root / name).exists()]
    assert not back, (
        "a tab the Build tab replaced is back, probably from a merge — delete "
        "it again (see CLAUDE.md, 'Retired tests'): " + ", ".join(back))
