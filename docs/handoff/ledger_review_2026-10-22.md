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

## Re-run with my answers (2026-10-23)

Andy: "when I viewed the filled out template by the model can I say look at
a few values and then give an answer and have it rerun and fill things in
more confidently?"

- The filled template window's "Re-run with my answers" saves the answers,
  then the Ledger tab (`LedgerTab.on_rerun_with_answers`,
  `LedgerActions.rerun`) applies the review and runs Extract again on the
  review's own sources, part, part-name pattern and operators (its `_run`
  sheet). The window opens again on the new review, at the same part.
- The values said Right are in the parent CSV by then, so the run learns
  their labels by example (`ledger_examples`): one label said Right for a
  few values pre-accepts its other readings.
- The earlier answers are copied into the new review
  (`ledger_review.carry_answers`, matched by part, item, field and source
  cell), so a value said Right or Wrong, or corrected, is not asked again.
  The filled templates are written again after that.
- A new review never takes an existing name (`default_review_for` adds
  `_2`, `_3`…): the old review is kept.
- The same button is in the tab's step 3 too (2026-10-26, Andy: "put that
  button in the extract and review section in the tab as well"):
  `LedgerTab.on_rerun_from_tab` re-runs the review workbook named there with
  the answers saved into it, and does not open the filled template window.
- Not done (offered, not yet agreed): a Wrong teaching "label X is not field
  F" for other parts, and re-checking only the uncertain values for speed.
  The re-run reads every source twice (Apply, then Extract).

## Put this value here (2026-10-26)

Andy: "With the review by clicking can I right click the spot where the data
actually belongs so I can manually correct things?"

- In Review by clicking and the filled template window, pick a value, then
  right-click the template cell it belongs in (`ReviewDialog.menu_actions`,
  `place_at`). "Put this value here (<field>, <item>)" gives the value that
  cell's field and item and says it Right. "Put it here and always map
  '<label>' here" also maps its source label to that field at Save
  (`ledger_review.teach_label`, saved in `<csv>.aliases.json`), so the next
  Extract puts it there by itself.
- The cell takes one value: another value accepted for the same part, item
  and field is said Wrong, or Apply would keep neither ("2 values accepted …
  kept none").
- Save writes the move into the review's Field and Item columns
  (`ledger_review.save`); Apply takes a row edited by hand as typed, so the
  value goes into the moved field. A Right or Wrong given after the move
  keeps it.
- Only values on the Values sheet move cell by cell; a table record does
  not. A cell that is not one of the template's fields offers nothing.

## Files that disagree, side by side (2026-10-26)

Andy: "lets say we have two or more files with conflicting data when we go
to review and link things I want to see all of those files side by side so
I can link the right value" — in Review by clicking and the filled template
window; "I do not want any more buttons to be made".

- Picking a value whose template cell other files (or cells) give another
  value shows one source pane per value, side by side
  (`ledger_review.rivals`: the same part, item and field, another value; at
  most `RIVALS_SHOWN`, 6). Each pane (`SourcePane`) has the file with the
  value's cell highlighted and a header: the value, the file, its modified
  date, the label, the score, Accepted / Left out, and the other files the
  same value is in (`Item.also`, from "also at" in its note). The panes keep
  their order; the picked one is outlined. Three fit across; more scroll
  sideways. One value and nothing disagreeing: one pane, as before.
- Clicking a pane picks its value. No new button: Right accepts it and
  leaves out every other value for that cell (`_answer`, all of them, not
  only the shown ones), since Apply keeps none of two accepted values that
  differ. "Put this value here" (right-click) does the same at its new cell.

## The full filled workbook after Extract (2026-10-26)

Andy: "I appear to be unable to export the full filled out workbook after the
extraction has been run. I can just see the review". Apply's full and
per-group workbooks (`ledger_groups`) come from the parent CSV, so before
Apply there was only the review and one file per part in `<review>_filled/`.

- Extract (and Re-run with my answers) also writes `<review>_filled.xlsx`
  beside the review (`ledger_review.write_full_filled`): the template filled
  in once per part, a sheet each in numerical order, as Apply's full workbook.
  Each value is tinted by confidence with a note of where it was read
  (`_fill_part`, shared with `write_filled`). The summary and log name it
  (`ExtractSummary.filled_book`).
- The filled template window's "Open as Excel" writes it again with the
  answers saved so far and opens it at the part shown (`active`). No new
  button.
- It is never written over a file the Ledger did not write (`FILLED_MARK`).
  Apply's workbooks are unchanged: accepted values only, with the groups.
