# Ledger: ideas from Andy's LMM Analysis Studio script (2026-11-13)

Andy shared a standalone PyQt6 script that merges filled copies of the LMM
template, verifies them, plots them and derives equations. He liked its data
verification, visualization and equation deriving. The comparison (18
synthetic copies, the script and Council on the same folder) and the ranked
proposal are in the project file `ledger-merge/lmm_script_proposal.md`. Andy
agreed the order, with the Equations page last. Nothing of the script was
copied as is: it is hard-wired to one template (field names, cells, flaw IDs);
Council takes all of that from the user's template.

## What was built (branch `claude/project-thread-lvqq7e`)

1. **Merge follows labels that moved** (`ledger_merge.Placement`, `place`,
   `_line_up`). A copy that matches the template under the merge's
   threshold in place is tried again with its rows and columns lined up with
   the template's by the labels they hold (a difflib diff of each row's and
   each column's set of template words, in order). A value is read where its
   row and column went (the nearest anchor at or before it). Still no model:
   the threshold applies to the labels found where they moved. Such a copy
   is listed on "Not merged" as "read with its layout moved (rows from 5 on
   are 2 lower) … Check its values." Andy's open question 3 (moved values in
   straight away or wait to be accepted) was answered by default: in, and
   listed.
2. **Correlation counts parts** (`ledger_analytics._parts_behind`,
   `_sure_by_parts`, `CORR_MIN_PARTS = 3`). "Sure" uses the number of parts
   behind a pair (t-test of r with n = parts, Benjamini-Hochberg across
   pairs); a pair with fewer than 3 parts is blank; the pairs list has Parts.
3. **Repeated tables as one field** (`ledger_analytics._stack_repeats`,
   `load(csv, stack=True)`). "Concealment 1..7: X" become "Concealment: X"
   (runs averaged for a number, the one value for a text, several listed)
   plus "Concealment: runs filled" (a count, `FieldInfo.count`, never
   correlated). The parent CSV is unchanged. A group's settings for run 1
   apply to the stacked field (`_mode`). "Repeated tables as one field" in
   the window unticks it.
4. **Check** (P5). The merge keeps, for each value of each copy, the template
   cell, the cell read, the value and a status (read / read at moved cell /
   blank in the copy / sheet not found): `Copy.audit`, a "Check" sheet in the
   merged workbook and `<merged> - check.csv` (`check_path`, `read_check`).
   The Analyze window has a Check page (each copy, moved ones first and
   yellow; its values with a status filter and search), a Data page (every
   row, searchable, blanks grey), and on Coverage "Fields in the correlation,
   and why the others are not" (`left_out_of_correlation`).
5. **Plots** (P6): histogram (mean, median), box plot by flaw block or a
   text field, normal Q-Q (scipy), PCA explained variance (numpy SVD).
6. **Old and protected Excel in the merge** (P7). `.xls`, `.xlsb` and copies
   openpyxl cannot open that `excel_open.kind_of` calls rights / legacy are
   read through `excel_open.read_with_excel` (Windows with Excel only),
   wrapped as a small openpyxl look-alike (`_ExcelBook`). Without Excel they
   are listed as before. Untested on a real Excel: the tests stand in for it.
7. **Equations** (P4, last): `council_core/ledger_equations.py` and
   `council_qt/tabs/ledger_equations_page.py`. Linear, ridge, quadratic
   (4 varying inputs at most), power law (log-log; every value above 0).
   Tested on whole parts left out (up to 5 folds, `_folds`). Inputs that
   never vary are held. Compare kinds, Suggest inputs (said to flatter the
   score), Predict (outside the data said), graphs (measured vs predicted,
   response curve, heatmap, contour), export PNG/SVG/PDF and CSV, saved as
   plain numbers in `<ledger>.equations.json`. numpy only (no scikit-learn).

## Not done / open

- Andy's question 2: a general "calculated field" (e.g. Post − Pre, the
  script's EC change %). Not built; offer it.
- Real-PC test on Andy's template and copies, and of `.xls` through Excel.
- A moved table whose sections changed order is not followed (the line-up
  keeps order); such a copy is still skipped and listed.
