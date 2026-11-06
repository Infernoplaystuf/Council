# Ledger analytics (Analyze window) — 2026-10-30

Andy: "add a data analytics function for example the ledger output file we
can do some analysis on it"; a window inside the Ledger; no split of fields
into inputs and results; no spec limits; then "we need a correlation matrix
so that we can better determine which variables correlate to each other".

## What it does

- `council_core/ledger_analytics.py` (no Qt), read only over the parent CSV,
  its schema (unit, section, per flaw) and the saved Ledger's groups.
  - Field kinds: number / date / text / empty (80% of filled cells decide).
    `to_number` takes '1,200', '12.5 um', '45%'. A column the schema does not
    list (an inspector's own reading, '… [JB]') takes its field's unit and is
    per flaw when a part's rows differ.
  - **Coverage**: filled / could hold per field (parts for a part-level field;
    flaw rows with any per-flaw value for a per-flaw one, not all 30 slots),
    and parts with the most fields blank.
  - **Summary**: count, mean, std dev, min, quartiles, max with units; date
    range; most common values. A part-level field counts once per part.
    "Per part" = a per-flaw field's average over the part's flaws.
  - **Trends**: a number field over a date field (or part order) with
    individuals-chart limits (`analyst_helpers.spc.control_chart_limits`,
    `western_electric_rules`), the points breaking a run rule listed.
  - **Correlation**: every pair of number fields (Pearson or Spearman,
    `analyst_helpers.stats.correlation_with_significance`, Benjamini-Hochberg
    across pairs = "Sure"); pairs need 5 values in common. A pair of
    part-level fields is counted once per part even per flaw. A field against
    its own inspector's reading is left out of the pairs list by default
    (they only say readings agree). Heatmap + scatter per pair.
  - **Outliers**: robust z (0.6745 x |v - median| / MAD) of 3.5 or more, per
    field, within the group, with the source file.
  - **Groups**: a group's parts only; its not-tracked fields left out; its
    Yes / No fields counted as Yes / No.
  - **Export**: `<csv stem>_analysis.xlsx` beside the Ledger (Coverage,
    Missing, Summary, Trends, Correlation, Outliers, Charts), marked
    `FILLED_MARK`, never written over a workbook the Ledger did not write.
- `council_qt/tabs/ledger_analytics_dialog.py`: the Analyze window (Group,
  Per flaw / Per part, Export…, tabs Coverage / Summary / Trends / Correlation
  / Outliers). Double-click a part in Trends or Outliers: the Filled template
  window on that part. Not modal.
- `council_qt/tabs/ledger.py`: step 3 "Analyze…" (`on_analyze_window`,
  loads on a worker; export runs on the tab's worker too).

Synthetic Ledger (103 files, 1,020 rows, 34 parts): load 1 s, window 2.7 s,
export about 10 s (a chart per number field, 37 here).

## Next (proposed, not built)

Step 2: group / inspector comparison, extraction quality from the review.
Step 3: ask a question with the local model (vault_analyst sandbox).

Tests: `tests/test_ledger_analytics.py`.
