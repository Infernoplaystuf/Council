# Saved Ledgers and "Update with new files" — 2026-10-23

Andy: "Lets say I create the filled template once but then the folders it was
run on get more data in them I want to be able to rerun from that stage",
"ledger runs should be saved, catergorized, and named by the council", and
grouped "both by template and or project/program".

## What it does

- **A saved Ledger** (`council_core/ledger_saved.py`) is `<csv>.ledger.json`
  beside the parent CSV. It holds the template, the folders and files as
  picked, the last run's settings (part, part-name pattern, operators,
  pictures), a name and a project / program, the size and modified time of
  each file read, and one entry per run (name, kind, time, review, filled
  templates folder, counts). It is written atomically. An index in the app
  folder (`<app dir>/ledgers.json`) lists the saved Ledgers. "Take off the
  list" only edits that index; nothing is deleted.
- Every Extract, Re-run with my answers and Update with new files that is
  not cancelled is recorded (`LedgerTab._recorder` → `LedgerActions.record_run`).
  A file skipped as too slow is not stamped, so the next update tries it again.
- **Names.** `auto_name`: "LMM Template · NDE results + Laser logs · 23 Oct
  2026", plus "· 12 new files" for an update. With "Ask Council's model"
  ticked, the picked model names the run (`model_name`, through
  `ledger_llm.chat_for`: local only; facts are names and counts, never
  values), falling back to the rules' name. The Runs window renames a run by
  typing over its name; the tab's Name field names the Ledger.
- **Groups.** The tab's "Saved Ledgers" box lists them under headings:
  by template, by project / program, or project then template
  (`ledger_saved.group`).
- **Update with new files** (`LedgerActions.update`) scans the saved folders
  again (`ledger_ingest.source_files`, the same Skip patterns as Extract)
  and gives Extract only the files that are new or changed
  (`ledger_saved.changed_files`), with the saved settings. Then the filled
  template window opens on the new review (sample Ledgers).
- **What the Ledger holds already** (`source_extract.ledger_values`, passed to
  `decide` as `known`): a value equal to the parent CSV's for the same part,
  item and field is pre-accepted (note "already in the Ledger"); a different
  one is never pre-accepted (note "differs from the Ledger, which has X";
  review reason "differs from the value already in the Ledger"). This holds
  for every Extract, so Re-run with my answers is surer too.
- At Apply, a flagged value the user accepted replaces the old one
  (`add_samples(..., overwrite=True)` for those cells only; the run says
  so). Every other cell is still never overwritten.
- **The filled template shows the whole Ledger**: the cells this run did not
  read are filled from the parent CSV, tinted `TINT_LEDGER` (light blue,
  "already in the Ledger"); a value that differs from the Ledger's is red
  with "the Ledger has X" in its note (`ledger_review.ledger_cells`,
  `write_filled`, `FilledTemplateDialog._tint_template`).

## Not done

- A Ledger whose source folders moved has to be picked again (Add folder…)
  and run with Extract; its earlier runs stay listed.
- Files removed from a folder keep their rows in the parent CSV; nothing
  says they are gone.

Tests: `tests/test_ledger_saved.py`, and
`test_a_saved_ledger_is_picked_again_and_updated_with_new_files` in
`tests/test_ledger_tab.py`.
