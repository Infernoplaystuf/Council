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
7. **Reads a grid of tables** (blocks side by side, stacked, staggered, or touching
   with no empty column) as one table per block:
   - headers are found per block, not per row;
   - a form line over a block (`Tank: 3`, `Inspector: | AL`) ends the block above
     it and is never taken for a header;
   - each block's labels are the nearest ones that belong to it, while a single
     label over the whole sheet goes to every block;
   - a bare block title ("North tank") is learned from the user's highlight as
     "the cell at this place over each table".
8. **Identifies records.** Key fields default to the name-derived fields plus the
   first text column unique within each table (BTB: Tame + Name). A grid's block
   labels join the key when they differ between blocks (Tank + Inspector + Position).
   The same record from two workbooks is dropped if identical, and red (clash) if
   different. Blocks of one workbook are never compared: they may share names (P1).

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
  `source_table` (where its table starts), `source_row`, `source_file` and
  `updated_at`. The same record (source + key fields) is updated in place. Where
  those match more than one record (a grid's blocks), the table's place tells them
  apart, so one block never overwrites another.
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

## PowerPoint decks (both Ledgers)

`pptx_open.py` reads .pptx/.pptm with the standard library only (no new
dependency). Folder scans now include decks, and the tab's file picker offers them.

- **Each table on a slide becomes a sheet of its own.** Above it go the slide's
  title, the slide-wide text, then the text boxes that belong to that table, one
  line per paragraph, then a blank row.
  - A text box belongs to its nearest table (above and to the left count first).
  - Text over several tables, the title, and text far from every table belong to
    the whole slide, so they go above every table on it.
- **Readers need no new rules for this layout.** The sample Ledger already takes
  a heading above a table as its section and "Inspector: JB" as who signed it; the
  table Ledger takes "Tame: Spider" as that block's label.
- **Joined lines are split.** "Eddy Current Response (%FSH) — Operator: TS"
  becomes a heading and a signature.
- **Pictures** go to the sample Ledger's image reader, named "Slide 2 · <title> ·
  <nearest text>" so the section word reaches the matcher.
- **Charts are not read.** Hidden slides are skipped.
- **Fills count as highlights.** Table-cell fills, text-box fills and highlighted
  runs are colours, so the highlighter works in PowerPoint too.
- **Old .ppt files** are refused with "save it as .pptx".

**Which text goes with which table.** Pairings are decided in this order (`pptx_open._owners`):

1. The slide's title belongs to every table on the slide.
2. A text box **grouped** with tables (PowerPoint's Group) belongs to them.
3. A text box **filled or outlined in a colour a table has** (a cell's fill or
   border) belongs to that table.
4. On a slide with a single table, text belongs to that table.
5. Otherwise it goes by **place**, and that is a guess: the nearest table (above
   and left first). Text over several tables, or far from all of them, goes to
   the whole slide.

What a guess (rule 5) does:
- Every value of a table whose heading or signer came from a guess carries a note
  and is never pre-accepted. In the sample Ledger it is amber with the note. In the
  table Ledger, the label value waits on one Decisions row per table
  (`P|file|sheet|field`).
- The Extract log and the review's Problems list each guess with the fix: "group
  them (or colour them alike) to say so".
- Teach in table mode lists every slide's pairings and how each was decided.

**Per-person slots** (`source_extract._Extractor._person_slots`, sample Ledger):
- **When it applies:** a per-flaw "Result"/"Value" table under a heading that
  names a template section whose fields are numbered person slots ("Visual
  Inspection: Inspector 1, Inspector 2").
- **What it does:** the first person to sign that section of a part takes slot
  1, the next slot 2, and so on. Their initials go to the slot's
  "— Inspector Initials" field. People beyond the last slot are not placed.
- **Never pre-accepted, never learned:** these values have their own kind
  (`via="slot"`), because the order comes from the order the tables were read.
  Accepting them does not teach "Result" as an alias.

## Link by clicking (both Ledgers)

The Ledger tab's **Link by clicking…** button opens the window
(`council_qt/tabs/ledger_link_dialog.py`; the data side is
`council_core/ledger_linking.py`, with no Qt). The template is on the left, a
sample workbook or deck on the right, and the tab's sources are its samples.

**The template side**
- Cells the Ledger fills are tinted; a field list with a search box sits above.
- Click a template cell, or pick a field in the list, then the sample cell it
  comes from.

**What a sample click teaches**

| Ledger | Sample cell clicked | What is learned |
|---|---|---|
| Sample-ID | any cell | The words that name the cell: itself if it is a label, else the nearest label to its left, else its column's header. Saved as a label mapping (`aliases.json`). |
| Table | a cell in a table (header or value) | That column's header words (recipe alias). |
| Table | a labelled value beside a table | The label. |
| Table | a bare cell over a table | Its place over each table (a `near` rule). |

**Buttons on the sample side**
- **From the file name…** asks which part of the file name the value is.
- **Look it up here**, then a column click, makes a lookup rule.
- Both are table-Ledger only.

**Decks:** a **Text boxes** tab lists each text box with a "Goes with" choice and
how it was told (a guess is marked). A choice saves to `<parent>.pairings.json`.
Extract, Apply and Teach read decks with those pairings (`pptx_open.user_pairings`),
and a deck read from the cache is read again when they change.

**Saving**
- Every link is shown as a sentence and can be removed.
- Nothing is kept until **Save links**. Linking the same field again replaces its
  link.
- Saved links are the user's own answers, so what they place is pre-accepted.
- Colour highlights keep working beside them.

**Several cells at once.** Drag over template cells, then over as many sample
cells; they link one to one in reading order (left to right, top to bottom), for
example a row of the template's headers to a row of a sample's. Template cells that
are one field repeated (a grid row across its flaws) count once. When the numbers
differ, nothing is linked and the window says so (`ledger_linking.link_cells`).

## Text documents (both Ledgers)

`docs_open.py` reads .docx/.docm (Word's XML, standard library), .txt/.md/.text,
and .pdf (pypdf, text only; it is already in Council's requirements). Folder
scans and the file pickers include them. An old .doc or .rtf is refused with
"save it as .docx".

**What a document gives as data**
- A "Text" sheet of its headings and short lines (`Part Number: 101`,
  `Inspected by JB`). Long prose is left out.
- A "Table N" sheet per table (Word tables, Markdown `| a | b |` tables, blocks
  of tab-separated lines). The document's headings and the short lines since the
  table before go above each table, then a blank row, as with a slide's text box.
- Pictures in a Word document go to the image reader.

**Explanations are never data.** These lines say what a label means:
- `TERM means / stands for / is short for … TEXT`;
- `TERM — TEXT`, `TERM - TEXT` or `TERM = TEXT` with three or more words;
- `TERM: TEXT` with a sentence after it;
- the rows of a two-column table headed like Term | Meaning.

`docs_open.explanations_for` turns them into label → field suggestions:
- the field the explanation names (the longest name wins; two named equally,
  none);
- the other way round, `Health: written "HP" in the old logs` teaches HP.

**How each Ledger uses them**
- **Sample-ID Ledger:** `Matcher.match` asks the documents only when its own
  rules aren't sure, and never over a taught, ignored or inspector label. Values
  are amber (`via="explained"`) with a note quoting the document line; accepting
  one in the review teaches the label.
- **Table Ledger:** a header the documents explain is a guess (`how="explained"`)
  with one Decisions row; a Y teaches it.
- **Council's model** gets the relevant explanations in its questions about
  labels and header layouts (`docs_open.notes_text`).

## Learning by example from filled copies (both Ledgers)

`council_core/ledger_examples.py`. A filled copy of the template is a worked
answer; so is every record already accepted into the parent CSV.

**Where the worked answers come from**
- **Sample-ID Ledger:** a source sheet with at least 80% of the template's own
  words in the same cells (`is_filled_copy`) is a filled copy. Its values are read
  by the template's own cells (`filled_rows`), keyed by part and flaw. The parent
  CSV's rows are passed in as well (`extract(examples=…)`, from Extract and from
  Apply alike).
- **Table Ledger:** a table placed by at least 80% of its fields' own headers is a
  filled copy of the template's table. Its records and the parent CSV's are keyed
  by the record's key field (Name).

**What is learned**
- **Sample-ID Ledger:** for a label no rule placed, its values are compared with
  the answers for the same part (and flaw).
- **Table Ledger:** each table is lined up with the answers by its key column; a
  renamed key column ("Creature") is found by the names it holds. Each unplaced
  column is compared value by value.
- **The evidence rule** (`_decide`):
  - most of the compared values agree with one field, and no other field as much;
  - at least one agreeing value is distinctive (not 0, 1, Yes, OK, M…), or at
    least 4 common values agree with none disagreeing;
  - a single agreeing value counts only when nothing disagreed.

**What happens to it**
- Learned labels and columns are applied to every workbook in the run, including
  the parts and records with no filled copy. That is the point: a few filled
  copies teach thousands of messy files.
- Learned values are always amber (`via` / `how` = `"example"`), with a note
  naming the filled copy.
- A Y on such a value (sample-ID Ledger) or on its Decisions row (table Ledger)
  teaches the label for good.
- A value equal to the filled copy's own merges with its row and teaches nothing:
  only a Y on a learned suggestion does.

**Duplicates (table Ledger):** two records from different workbooks clash only
when a field has two different values. One that only lacks fields the other has,
and agrees on the rest, is left out as a duplicate. One with extra fields is kept,
noted "also in …".

## The Ledger's model uses its own limits — the window each call needs

Every Ledger model call (labels, part numbers, header layouts, label-mapping
suggestions) may use up to the model's own maximum instead of the council's chat
defaults, but each call is given only the window it needs (changed 2026-10-22):

- **Why not the whole window:** sending an Ollama model its whole context length
  (128K for granite3.3, llama3.1, gpt-oss) on every call made Ollama reserve memory
  for all of it, about 16 GB of KV cache for an 8B model. That does not fit a GPU, so
  layers moved to the CPU and system RAM. The user saw high RAM and low CPU and GPU
  use in the Ledger. Every switch between the council's 8K window and the Ledger's
  also reloaded the model.
- **Ollama:** a call gets the smallest of 8K / 16K / 32K / 64K / 128K / 256K
  (`ledger_llm.WINDOW_STEPS`) that holds its prompt and 4,096 tokens for the answer,
  never more than the model's own. It goes through `council_engine.model_window()`,
  a per-call override of the slot window.
- **A GGUF the user picks:** loaded once with up to 32K (`GGUF_WINDOW`; the VRAM
  ladder still sizes it to the card). Council's main model keeps the window it was
  loaded with.
- **The reply:** may use all the prompt leaves of the window
  (`ledger_llm.reply_budget`), never less than 1,024 tokens. The stall limit is 300 s.
- **Overrides:** `COUNCIL_LEDGER_NUM_CTX` sets the window outright, still never past
  the model's own (also a GGUF's load window). `COUNCIL_LEDGER_NUM_PREDICT` fixes the
  reply length.

## Memory over a long Extract (2026-10-22)

- **Unmatched values on disk:** a run keeps one record per label cell no rule
  placed, thousands per messy workbook. Past 5,000 they go to a temporary file of
  JSON lines (`source_extract.ObsSpool`, deleted when closed) and are read back once
  the run is over. "Labels to map" keeps one example per label and sheet as they
  stream past, not every value.
- **Smaller records:** `Obs` is a slots dataclass.
- **GPU choice:** a GGUF goes on one GPU, the discrete card, instead of llama.cpp's
  default split over every GPU. An Intel iGPU beside an NVIDIA card (a Vulkan build
  sees both) took layers into system RAM. See `council_engine._one_gpu`;
  `COUNCIL_GGUF_MAIN_GPU` (a number, or `all`) overrides it.
- **Files that eat memory:** see `docs/handoff/skipped_files_2026-10-22.md`.

## Measured (this container, synthetic data)

- **BTB:** reproduces `BTB_Template_filled.xlsx` cell for cell, colours included
  (0 mismatches over 620 cells with `reference_check.py`).
- **Speed:** 300 workbooks / 15,000 records / 95,000 values took 13 s on the first
  Extract and 4.5 s from the cache.

## Not done yet

- Flaw-grid templates stay with the sample-ID Ledger; a template holding both a
  record table and a flaw grid is read as one or the other.
- Pictures of tables are not read in table mode (the sample Ledger's image reader
  is not wired in).
- The `.records_cache` folder is not pruned.
- Free-sentence hints in a text box ("John checked these by eye") are only
  headings: no model reads them yet for who or what. A local model reading a
  slide's text for the fields it names would sit beside `ask_model_headers`.
- Pictures on slides are not read in table mode (as in workbooks).
