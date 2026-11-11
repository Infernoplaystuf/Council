# Ledger: Merge filled copies (2026-11-11)

Andy (2026-11-11): "I'd also like something in the council possibly the
ledger to combine excel files together. For example lets say I have a
template and then a folder of filled out files and I want to merge them
together into a single output workbook with a sheet for each part. I then
want to be able to do some analysis on that data."

Agreed design: a button in the Ledger tab; "a sheet for each part" means **a
sheet per filled file** (Andy's pick), plus an "All data" sheet; the
analysis is the existing Analyze window (PR #59), not a new one; no model.

## What it does

Ledger tab, step 1: **Merge filled copies…** opens a window
(`council_qt/tabs/ledger_merge_dialog.py`): template, folder of copies
(subfolders optional), where to save (default `<folder> merged` beside the
folder; never inside it). **Merge** runs on the tab's worker
(`council_core/ledger_merge.py`, `merge()`), then the tab shows the merged
Ledger (template + parent CSV), adds it to the Connections graph, and the
window offers **Open workbook** and **Analyze…** (the tab's Analyze window).

Written to the save folder:

* `<template> - merged.csv` + its schema: an ordinary parent CSV, so
  Analyze, Groups and Connections read it as any Ledger. Built beside as
  `.merging.csv` and swapped in when the workbook is written.
* `<template> - merged.xlsx`: **All data** (a row per part, or per part per
  flaw, with the file it came from first; filter and frozen header), **Not
  merged** (only when there is something on it), then the template filled in
  once per copy (a template of several sheets: `<part> <sheet>`), in part
  order. Marked as the Council's (`mark_filled`).
* A saved Ledger (`ledger_saved`, kind `"merge"`, settings `kind`,
  `recursive`, `workbook`), a run per merge. **Update with new files** on a
  merged Ledger merges its folder again (`merge_again`) instead of
  extracting.

## How a copy is read

No guessing: by the cells `sample_ledger.read_template` found for each field.

* Each template sheet is matched to the copy's sheet laid out like it
  (`ledger_examples.is_filled_copy`: 80% of the template's words in the same
  cells): the same name first, else any. Renamed sheets are fine.
* Template of one sheet: **every** sheet laid out like it is a copy, so a
  workbook with a filled sheet per part gives each part (labelled
  `file.xlsx [sheet]`).
* Not merged, listed with why: layout differs (read it with Extract),
  nothing filled in, `.xls`/`.xlsb` (save as .xlsx), could not be opened,
  on the skip list (`file_guard.read`, tab "Ledger"), a merged workbook the
  Council made. Excel's `~$` lock files and non-Excel files are ignored.
* No part number: named after the file, and said so.
* A part in two copies: the parent CSV keeps the first copy's values (file
  name order); every value the other differs on is on Not merged (a
  part-level value once, not once per flaw row). Both copies keep their own
  sheet (`S-3`, `S-3 (2)`).
* Never written over: a CSV the merge did not make, or a workbook the
  Council did not write.

## Fix that came with it

`sample_ledger._dedupe`: a label on two pages (sheets) with no heading to
tell them apart dropped the second page's field (found 2026-11-05 testing
multi-page templates). It is now named by its sheet, `Page 2: Date`. Only
names that used to be dropped change, so existing Ledgers keep their
columns. The ID field (Part Number on every page) stays one field.

## Tests

`tests/test_ledger_merge.py` (synthetic workbooks only): merge + sheets +
All data, Analyze reads it, files not merged, a part in two files, no part
number, a sheet per part in one workbook, merge again / no overwriting,
not inside the folder, an old merged workbook in the folder, the two-page
label fix, a two-page template merged page by page, and the tab: merge,
Open, Analyze, Update with new files, a failed merge said in the window.

## Not done / next

* A copy whose layout differs is not merged; Extract reads those.
* Records-style templates (a table, one row per record, no sample ID) are
  not merged: `read_template` asks for a sample-ID field.
* Not tried on Andy's real templates (confidential); synthetic only.
