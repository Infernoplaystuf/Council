# Ledger groups and their workbooks — 2026-10-26

Andy: "in the review step I'd like to be able to create sub groups ... data
that has changed over time. So earlier data has some datapoints that aren't
tracked anymore or got condensed down to just yes or no ... slice the data
into smaller output workbooks in addition to the full workbook", and "The
output workbooks for the full and group should have separate sheets of the
filled out template for each part number (These should be ordered in
numerical order). Data is most commonly separated by part name but can be
date or source folder".

## What it does

- `council_core/ledger_groups.py`. A `Group` takes its parts by **part name**
  (globs such as `ABC*, *-R1`, or a part-name pattern such as `@@@#@@###`),
  by **date** (a field, from / to, inclusive; `parse_date` reads ISO, US
  month-first, '1 Oct 2026', Excel day numbers), by **source folder** (the
  workbooks the saved Ledger read from it, matched by name against the
  parent CSV's `source_file`), or by hand only. Parts ticked in or out by
  hand are kept as `include` / `exclude`.
- Each field of a group is tracked, **not tracked** (`off`: left out of its
  workbook), or **Yes / No** (`yes_no`: blank is No; any value is Yes, or
  only the values listed).
- Groups are kept in the saved Ledger (`<csv>.ledger.json`, `groups`), so
  "Update with new files" puts new parts in them by the same rules.
- **Workbooks** (`write_all`, `write_workbook`): `<csv stem>_workbooks/`
  beside the parent CSV holds `<template> - all parts.xlsx` and
  `<template> - <group>.xlsx`. Each is the template once per part, a sheet
  per part (a template of several sheets: `<part> <sheet>`), in numerical
  order (`sample_sort_key`), filled from the parent CSV. Written atomically,
  marked as the Ledger's own (`FILLED_MARK`, so no scan reads them back), and
  never written over a file the Ledger did not write. Copied sheets keep the
  template's values, styles, merged cells and sizes; openpyxl does not copy
  pictures or charts.
- Apply writes them every time (after the parent CSV); the Groups window's
  "Write workbooks" writes them now. A table Ledger has none.
- **Review**: at Extract, a value of a field that no group of its part tracks
  is set aside ('N', "not tracked for <group>"), not asked about; a part in
  no group keeps every field (`set_aside_untracked`). The summary says how
  many.
- The **Groups window** (`council_qt/tabs/ledger_groups_dialog.py`) opens
  from the Ledger tab's step 3 ("Groups…") and from the Filled template
  window. It lists the Ledger's parts and the open review's, ticks show who
  is in the group, and closing saves.

Tests: `tests/test_ledger_groups.py`, and
`test_groups_are_made_in_review_and_apply_writes_their_workbooks` in
`tests/test_ledger_tab.py`.
