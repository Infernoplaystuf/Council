# Fixes from the synthetic Ledger / Connections test (2026-10-27)

A synthetic corpus (61 files, then 42 more; nothing real) was run through the
Ledger and Connections tabs against a stand-in LMM template. Andy approved
fixes 1-9. The corpus, its answer key and the scripts are in the project files
(`synthetic-test/`), not in the repo.

## What changed

1. **No 500-row limit** on a Ledger table (`source_extract._data_rows`). A
   long log's rows past 500 had no part.
2. **Sheets that are not the template's are held back.** A sheet with at most
   two template fields among many other words (`_off_template_sheets`), or a
   part whose shape is unlike the rest (`_odd_parts`: 'FAN-12' among '2041'),
   is never pre-accepted. Reasons: `FEW_FIELDS_NOTE`, `ODD_PART_NOTE`.
   Before, an inventory sheet's Power (W) was pre-accepted and written.
3. **A filled template someone saved since is read again.** `fill_template`
   clears `lastModifiedBy`. A filled template with one (Excel sets it on save)
   is `is_edited_filled_template` and read. Untouched Ledger output is still
   left out.
4. **PDF tables** are read by pdfplumber (`docs_open._pdf_blocks`), and a
   page with no text is rendered (pypdfium2) and read by OCR. pypdf stays as
   the fallback. `requirements.txt` and `council.spec` list both.
5. **CSV / TSV** are Ledger sources (`_grid_from_csv`: encoding fallback,
   delimiter sniffed). A parent CSV (a `.schema.json` beside it) is not.
   The summary lists the file types it had no reader for (`unread_files`).
6. **A part named in a sheet's title** ('Part 3005 — EC readings') is used
   when the sheet has no part label (`_title_part`). It agrees with the file
   name → pre-acceptable; otherwise it is checked.
7. **Q&A picks the asked part's rows.** `kg_contents.sheet_parts` finds a
   sheet's part (label, title, file name); `_rank_named_part` lifts rows of the
   named part. Each row in the Council's facts says `(part X, from Y)`.
8. **People columns are offered.** `PROPOSED_FIELD_RULES` (Employee, Manager,
   Inspector, Inspected by, Technician, Sales Rep, Lead) start proposed; only
   `DEFAULT_FIELD_RULES` start ticked. A ticked 'Inspector' does not take a
   workbook's 'who' column from the Ledger's reading (`kg_excel._rule`).
   Connections now says which file types it could not read.
9. **Unrelated sheets are not read cell by cell.** A sheet of 200+ cells with
   no template word in its name or first 30 rows is skipped (`_unrelated_sheet`)
   and listed in the summary. A 2,000-row sales sheet took 17.6 s for nothing.

## Rerun results

- Batch 1, merged: 6,074 → 11,809 of 11,883 values right (51% → 99%), 0
  pre-accepted wrong, 0 wrong values in the parent CSV (was 1, plus 3 parts
  that were not real).
- Batch 2 (multi-sheet workbooks, linked workbook sets, scans, CSVs,
  PowerPoint, Word, an edited filled template): 6,177 of 6,591 (94%), 0
  pre-accepted wrong, 0 wrong values written. The stale cached value of a
  linked master was shown as a clash, not accepted.
- Connections over all 103 files: every person and project found, all 20
  tracker links (lead and part) built.

## The five gaps the rerun found (fixed; Andy's OK, 2026-10-27)

1. **Initials only in the sheet name** ('EC JB', 'EC MK'): one word of
   initials anywhere in the name signs the sheet's readings when nothing on
   the sheet says who (`find_inspectors`).
2. **Initials in a column header** ('EC Pre-Conceal (JB)'): JB signs that
   column's readings (`_table`, `_who_paren`). '(EC)' or any other word of
   the template's field names is not someone.
3. **A stage only in the sheet name** ('Pre-Conceal' sheet, 'Flaw # |
   Inspector | Response'): the sheet's name is the stage (`_sheet_stage`,
   read as a long log). Values go to the right field, held for review (a
   loose label match). 'Response' beside 'Inspector' in a header row is no
   longer read as an inspector's name.
4. **A part only in an index workbook** ('Part Number | Settings file'):
   a file that names no part itself gets the part the index lists it under
   (`_Extractor.files_named`, `_index_parts`, `INDEX_PART_HOW`). Held for
   review, never pre-accepted. The data files and the index must be read in
   one run.
5. **A filled template edited outside Excel**: every workbook the Ledger
   writes (`fill_template`, the review's filled templates, Ledger groups,
   table templates) is marked by `sample_ledger.mark_filled`: the mark, a
   fingerprint of every cell, and no last-saved-by. A scan leaves it out
   while its cells match; once anyone types into it, in any program, it is
   read again. Excel's last-saved-by still counts. A workbook marked before
   this has no fingerprint and goes by last-saved-by only. The other
   writers used to keep the template's own last-saved-by, so their output
   could look edited the moment it was written.

Rerun, batch 2 (as the tab reads the folder, the index workbook included):
6,587 of 6,591 right (94% → 99.9%), 0 wrong (was 240), 0 wrong values
pre-accepted. Batch 1 unchanged (11,809 of 11,883).

## Follow-up: the stall on long logs (2026-10-30)

User, 2026-10-29: "The ledger on the disconnected test machine is now stalling
out again." Two of the fixes above made a long log slow with the square of
its rows (the test PC: a 12,000-row log 89 s, 1.7 s before them). Not the
network: nothing on the Ledger's path goes online.

- **`files_named`** (fix 4): every part number of a log ('PN-1000') matches
  the Part Number field, so each counted as an index header and the rows
  under each were walked. Now a sheet that names no file returns at once,
  only the topmost Part Number header of a column counts, each distinct text
  is matched once, and the rows are gone through once.
- **`_data_rows`** (no 500-row cap): every row of a log is tried as a table
  header, and each walked to the end of the sheet. The walk is now kept per
  set of columns (`_rows_walk`); same rows as before.
- **CSV sources** (fix 5 of the first list): a .csv / .tsv over 50 MB is not
  read (`COUNCIL_LEDGER_CSV_LIMIT_MB`, 0 = no limit); every cell was held and
  matched.

Tests: `tests/test_ledger_stall_fixes.py` (`test_a_long_log_*`,
`test_each_row_of_a_long_log_tried_as_a_header_*`, `test_a_csv_too_big_*`).

## Follow-up: parts' tables side by side (2026-10-30)

User: "many tables being in the file on the same sheet that belong to
different parts", then "yes" to accepting values directly under their own
Part Number. A sheet whose part labels shared a row sent every value to
review ("more than one part on the sheet": 160 tables, 5,920 of 5,920).

`_Extractor._part_grid_blocks` (from `_part_blocks`, when two labels of
different parts share a row): a label's block is the columns from it to the
next label column on the right, and the rows down to the next label in those
columns; `part_at` takes the nearest label column at or left of a value,
then the nearest label above in it. The blocks are sure, and pre-accepted,
unless a 'TBD' block holds readings or a label's row holds other text in its
own columns (a title). `test_ledger_real_layouts.py`'s `same_row` case now
expects acceptance. Tests: `tests/test_ledger_part_grid.py`.
