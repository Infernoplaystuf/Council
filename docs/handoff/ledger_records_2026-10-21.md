# Ledger: table templates (one row per record)

Module: `council_core/ledger_records.py` (no Qt). Reached through the Ledger tab's
usual buttons via `council_core/ledger_ingest.py`; nothing else changes for a
sample-ID Ledger.

## When it is used

- **Create** makes a *table Ledger* when the template has a table (a row of two or
  more column headers with rows under it) and no sample-ID field: the BTB example.
  The tab's **Each table row is a record** box (or `create_parent(..., mode="records")`)
  forces it for a template that has a sample-ID column but whose workbooks hold many
  records each.
- A table Ledger is marked by `<parent>.records.json` (the recipe) beside its CSV.
  Extract and Apply route to this module by that file and by the review's document
  properties (`council-ledger-records-review`).

## What Extract does

1. **Reads** every sheet of every workbook (openpyxl read-only; .xls/.xlsb through
   `source_extract.load_workbook_grids`; .csv). Charts and pictures are not read.
   A sheet stops after 2,000 empty rows following its last value. Each file is
   cached in `<parent>.records_cache/` (gzip JSON, by path, size and modification
   time), so a rerun reads only changed files.
2. **Finds tables** by shape: header rows of words, their records down to a blank
   row or the next table's header. It skips total and average rows and headers
   repeated after a page break. It keeps group headings above merged headers and
   also reads the sheet sideways (records across).
3. **Learns the user's highlights.** The same fill colour in the template and a
   workbook links them (theme colours are resolved with their tint):
   - painted columns of a record table: those fields' header words (sure);
   - a single painted column in a table with no other painted field: a **lookup**
     for that field;
   - a template colour found in no workbook: the **file name** (or the sheet name),
     amber until the user confirms it.
4. **Maps columns to fields**, one to one:
   - **sure:** the user's own words (taught, highlighted, accepted), exact header
     words, or the same words in another order;
   - **guesses (amber):** similar words, or **Council's model**. The model is asked
     only about headers nothing else placed, once per header layout, at most 40
     layouts per run. Only header words, field names and three example values per
     column are sent, and the answers are cached in the recipe.
5. **Checks each column's values against the field.** A column holding numbers
   where the field's other columns hold words (or values outside the few a field
   takes, such as M/F) turns amber.
6. **Fills fields that are not columns:**
   - the file or sheet name (a pattern, inferred or taught by example);
   - a lookup: exact key, then the longest key the value starts with on whole
     words, then confirmed answers, then a similar key (amber); a key listed twice
     with different values is amber;
   - a label beside the table (`Inspector: MK`);
   - a constant;
   - a template formula, which is carried down and never read.
7. **Identifies records.** Key fields default to the name-derived fields plus the
   first text column unique within each table (BTB: Tame + Name). The same record
   from two workbooks is dropped if identical, and red (clash) if different.

## The review workbook

| Sheet | What it holds |
|---|---|
| Records | One row per record. Amber cells are guesses; a comment says where each came from. |
| Decisions | One Y/N per guess (a column, a value check, a rule, a fuzzy lookup), covering every record it touches. |
| Tables | Every table found: read as records, used as a lookup, or skipped, and why. |
| Problems | What went wrong or could not be found. |
| `_run` | Hidden run details. |
| Values | An empty sheet, so Council never reads the review back as a source. |

Apply takes a record when its Accept is Y. A record with Accept left blank is taken
when every guess in it has a Y on Decisions and it has no clash.

What Decisions teach:
- A Y on a column guess becomes an alias; an N is remembered, and that column is
  never read as that field again.
- A Y on a rule confirms it.
- A Y on a fuzzy lookup saves that key's answer.

## Apply

- The parent CSV gets one row per record: the fields, then `source_sheet`,
  `source_row`, `source_file` and `updated_at`. The same record (source + key
  fields) is updated in place.
- The schema beside the CSV is a `sample_ledger` schema, so the knowledge graph
  reads the CSV as a Ledger.
- With a fill folder, the template is filled with every record:
  - rows past the ones the template laid out take that layout's formatting
    (banded rows keep their bands);
  - formulas are carried down;
  - data validation and conditional formats are stretched;
  - rows are inserted when something sits under the table;
  - form fields are filled when every record agrees.

## Command line

    python -m council_core.ledger_records create  BTB_Template.xlsx
    python -m council_core.ledger_records teach   BTB_Template_parent.csv samples/BTB_Woopentide.xlsx samples/BTB_Saddle_BP.xlsx
    python -m council_core.ledger_records extract BTB_Template_parent.csv samples/ [--model ollama:granite3.3:8b]
    python -m council_core.ledger_records apply   review_….xlsx --fill-dir filled/
    python -m council_core.ledger_records source  CSV Tame filename --example BTB_Spider.xlsx Spider
    python -m council_core.ledger_records source  CSV "Saddle BP" lookup --file BTB_Saddle_BP.xlsx --key-header "Tame Saddle" --join Name

## Measured (this container, synthetic data)

- **BTB:** reproduces `BTB_Template_filled.xlsx` cell for cell, colours included
  (0 mismatches over 620 cells with `reference_check.py`).
- **Speed:** 300 workbooks / 15,000 records / 95,000 values took 13 s on the first
  Extract and 4.5 s from the cache.

## Not done yet

- Teach in the tab writes a text report to the log. It has no side-by-side window
  yet: the highlighter window (spec step 4) would sit on top of `learn_colours`
  and `set_source`.
- Flaw-grid templates stay with the sample-ID Ledger; a template holding both a
  record table and a flaw grid is read as one or the other.
- Pictures of tables are not read in table mode (the sample Ledger's image reader
  is not wired in).
- The `.records_cache` folder is not pruned.
