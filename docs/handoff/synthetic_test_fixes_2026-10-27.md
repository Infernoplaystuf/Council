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

## Found, not fixed (for Andy to decide)

- A sheet per inspector named only in the sheet name ('EC JB', 'EC MK'): the
  readings are unsigned, and the first sheet's value is used for the template
  cell. Reading initials from anywhere in the sheet name fixes it (tried on
  Part_4001_full_record: 67% to 100% right).
- Initials in a column header ('EC Pre-Conceal (JB)') do not sign the column.
- A stage named only in the sheet name ('Pre-Conceal' sheet with a 'Response'
  column) is not matched to the stage's field.
- Linked files whose part is only in an index workbook have no part; the
  Ledger asks for it.
- A filled template written by a tool other than Excel (no lastModifiedBy)
  is treated as the Ledger's own output and left out.
