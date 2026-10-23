# Review by clicking — 2026-10-22

User: "the review document spit out to review connections is way too clunky to
actually review the connections and see if they are right or wrong", then "can
we do something similar to the existing link by clicking? That just be more
easily readable and viewable. Highlight the cells in question in that." The
Excel review stays.

## What it does

The Ledger tab's **Review by clicking…** (step 3, next to Open review in Excel)
opens `council_qt/tabs/ledger_review_dialog.py` over the review workbook:

- **Left:** the review's items. The ones that need a decision (amber or red)
  come first; "Everything" shows the rest.
- **Template, top right:** the template from `ledger_linking.template_view`.
  The cell this value goes in is orange (a sample Ledger's flaw/item cell,
  from the schema). The rest of the field's cells are blue.
- **Source, bottom right:** the source workbook from `ledger_linking.sample_view`.
  The cell the value was read from is orange, or red for a clash. For a table
  Ledger, the record's whole row is shaded. Both views scroll to the cell.
- **Answering:** the detail line says what was read, from which label, the
  confidence and any notes. Use Right / Wrong, or type a corrected value; the
  next item then comes up.
- **Save** writes the answers into the review workbook's own Accept (and Value)
  cells, through `council_core/ledger_review.save`, atomically. Apply and the
  Excel review work as before.

`council_core/ledger_review.load` reads both kinds of review:

- **sample Ledger:** one item per row of Values;
- **table Ledger:** one item per row of Records, Records 2, and so on.

It finds the source paths in the hidden `_run` sheet.

## Not done

- A value read from a picture has no cell to show. The window says so.
- A table Ledger's values are accepted or rejected per record. A single value
  is still corrected in Excel.

Tests: `tests/test_ledger_review.py`, plus the tab test in `tests/test_ledger_tab.py`.

## The filled template (2026-10-23)

Andy: "have the ledger output the template filled out with the values
entered into it highlighted by their confidence values ... a pop-out of the
output filled in template excel so I can click on cells and see their
source".

- Extract writes `<review>_filled/<part>.xlsx` beside the review: a copy of
  the template with each part's values in their cells, tinted by
  `ledger_review.confidence_tint` (green score ≥ 0.85, yellow 0.6–0.85,
  orange under 0.6, red when sources disagree), with a cell note giving
  the source cell, the score and any other values offered
  (`ledger_review.write_filled` / `write_all_filled`). Sample Ledgers only.
- The Ledger tab's "Filled template…" opens `FilledTemplateDialog`
  (`council_qt/tabs/ledger_review_dialog.py`, a `ReviewDialog`): pick a
  part, click a filled cell, and the values offered for it are listed with
  the source cell highlighted below. Right / Wrong / a correction are saved
  into the review like Review by clicking; "Open as Excel" writes and opens
  that part's filled template.
