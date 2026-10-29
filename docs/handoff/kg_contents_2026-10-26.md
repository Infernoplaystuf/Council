# Handoff, 2026-10-26: the Contents layer (every cell in the graph)

Branch `qt-migration`. Read this before changing `council_core/kg_contents.py`,
`council_core/knowledge_graph.py`'s reading phase, `kg_excel.read_workbook`, or
`council_qt/tabs/connections_contents.py`.

## What the user asked

"It looks like the connections tab is too strict with the connections it is trying
to make. Basically ALL of the input data should be in the knowledge graph for
example an excel file has the head node with the file name then lets say stat
headers/labels and then that data is connected to the labels."

Cause: `_table_records` (CSV), `kg_excel._GridReader._table_row` (workbooks) and
`_text_records` kept a cell only when its header or label exactly matched a
PERSON / PART / PROJECT field rule. Search and `kg_query` only saw entities.

## Decisions (user, 2026-10-26, on the proposal)

1. A numeric column is one summary node (count, lowest, highest) that lists its
   cells on request, not a dot per number.
2. At most about 200 different values of a column are listed (`DRAWN_VALUES`);
   the rest stay searchable.
3. The same text value in several files is one value (`vals`), so files connect
   with no rules. Numbers are never merged (every `1` would link everything).
4. Start with step 1: spreadsheets and CSVs, search, a Contents tree.

Later steps proposed (not built): 2) text, PDF, Word, JSON and pictures into the
same layer; 3) Council answers matching fields and values and handing the model
the matching rows (sized by `council_core/token_budget.py`); 4) the picture view
of files and fields, and the Ledger looking up template fields in it. The layer
is a candidate for the app-wide source catalog.

## What changed

- **`council_core/kg_contents.py`** (new, toolkit-free): `Sink` (one document's
  fields and cells; cells spooled to a temp file, not memory), `GridContents`
  (a sheet fed row by row with a one-row look-ahead, as `_GridReader` is; any
  header counts - no rules), `store` / `forget` / `finish`, and the queries
  (`files`, `sheets`, `fields_of`, `field_values`, `places`, `row_cells`,
  `same_fields`, `search`, `summary`, `locator`). How a grid is read is in its
  docstring.
- **`knowledge_graph.py`**: schema **v5** (tables `contents_docs`, `fields`, `vals`,
  `cells`, FTS5 `vals_fts` / `fields_fts`; LIKE fallback without FTS5). The
  reading phase gives each CSV / workbook / Ledger CSV a `Sink` through the
  `_CONTENTS` context variable (copied onto file_guard's helper thread) and stores
  it after the read; an unchanged document whose contents are missing (a v4
  store) is read again; after the reading phase `finish` drops documents gone or
  unreadable, recounts values and drops values nothing holds. Stats key
  `contents`. Methods: `has_contents`, `contents_counts`, `contents_files`,
  `contents_sheets`, `contents_fields`, `contents_field`, `field_values`,
  `content_value`, `value_places`, `row_cells`, `same_fields`, `search_contents`
  (all answer empty on a read-only store without the tables).
- **`kg_excel.read_workbook`**: `run()` feeds every grid (sheets, text boxes,
  charts, pictures read by OCR) to the sink as well as to `_GridReader`.
- **Connections tab**: a **Contents** tab (`ContentsView`): file › sheet ›
  header/label › values › every place a value is › its whole row; a numeric
  field shows its range; "Same label in N other places"; selecting a cell
  previews it and enables Open file. The search box also searches the contents
  (the tab title says how many matches); the coverage line counts it.

## Measured (this container, synthetic data, no Excel)

One `.xlsx` of 100,000 rows x 10 columns (966,630 cells), streamed reader:

| | without the layer | with it |
|---|---|---|
| first build | 39.6 s | 54.2 s |
| store after VACUUM | 81 MB | 137 MB |
| peak memory | 311 MB | 348 MB |
| unchanged rebuild | - | 0.1 s |

Search (prefix, values) 2 ms; a number 1 ms; listing a text column's 200
commonest values 85-200 ms; a numeric column's first 200 cells ~1 ms.
So about 15 s and 56 MB per million cells.

## Tests

`tests/test_kg_contents.py` (17): grid reading (tables with any header, labels,
forms side by side, loose cells, blank rows), CSV columns no rule names, merged
values and unmerged numbers, leading-zero codes, same header across files,
search (prefix, words, FTS syntax typed, no FTS5), the 200 cap, rebuilds
(unchanged, changed, deleted, a v4 store), read-only store without the layer,
and the tab offscreen (browse to a row, preview, Open file, search).

## Step 2 (2026-10-26, same day): text, PDF, Word and JSON

User: "merge it and begin working on any next steps". Schema **v6**: `fields.unit`
(`cell` a grid, `line` a text document, `json`), added to a v5 store by
`kg_contents.create` (ALTER TABLE).

- Text documents (every `_TEXT` suffix: md, txt, PDF, Word, HTML, XML, YAML ...):
  their lines (`document_lines`, read once - the gazetteer reuses them) are fed to
  `Sink.text_lines` as rows of a grid per PDF page (`page 3`; one unnamed sheet
  otherwise). `Label: value` lines become labels, other lines loose cells, and a
  line split by `|` or tabs is a row of cells, so Markdown / pasted tables get their
  headers (the `|---|` rule line bridges the header and the first row; a line of
  prose ends a table). Cited `{"page", "line"}`.
- JSON (`_json_records` hook, `Sink.json_data`): each object with scalar keys is a
  row (numbered in the order found), each key a field, scalars in a list sit under
  the list's key. Cited by line.
- The reading phase now gives every supported document a Sink; an unchanged one
  without contents is read again once (the first rebuild after this change).

Measured: 500 Markdown files of ~200 lines (101,000 lines): first build 39.6 s
with the layer, 34.7 s without; store 54 MB with, 11 MB without (full-text
search over every line is most of it).

Tests: five more in `tests/test_kg_contents.py` (labels, a Markdown table and
lines; a PDF page by page; JSON rows; a value linking a workbook and a text file;
a v5 store getting `unit`).

## Step 3 (2026-10-26): the Council's answers use the rows

`kg_contents.question_rows` (no model call) finds what a question names in the
layer: exact values of any run of up to six words (a lone plain word counts for
less than a code), numbers, lines sharing two or more of its words (FTS), and
headers or labels whose words it uses ('hardness' -> 'Hardness (HRC)'). Rows score
by what they hold (a value a column repeats counts less; a row with a named header
gets a point) and the best 12 come back whole. A header named with no value gives
the first 6 rows under it.

`kg_query.context` now answers a question that names no entity when it matches the
contents, and adds the rows as `[R1] file, sheet/row/line: Label: value | ...` plus
"Headers and labels the question names" with each summary; `ASK_SYSTEM` cites
`[R1]`. So `turn_context.knowledge_graph_block` (the Council's answers) and the
tab's **Ask** both get them; the tab's Answer view lists "Rows of your files",
each previewed when picked.

Measured on the 1M-cell workbook above: 1-5 ms per question.
Tests: seven more in `tests/test_kg_contents.py`.
The Answer view lists the facts about the named entities first and the rows after
them (fixed in step 4; step 3 put the rows first, which broke two
`test_connections_morphik.py` tests).

## Step 4 (2026-10-26): a file's picture and the Ledger's field lookup

**Picture.** `kg_layout.contents_subgraph(kg, document_id)` draws one file: the
file node, its headers/labels (`FIELDS_DRAWN=30`), up to `VALUES_PER_FIELD=4`
values per text column, a numeric column as one `NUMBERS` node naming its range,
and `ALSO_IN` edges to other files holding the same value. Node ids start `c:`
(`c:file:`, `c:field:`, `c:val:`) so the canvas's entity actions ignore them; a
double-click on another file's node draws that file. In Connections, the
Contents tree has **Draw this file** (file, sheet, field or place selected) and
the tab keeps `_picture_file` until "whole graph" or an entity is shown.
`kg_canvas.TYPE_COLOURS` has FILE, HEADER, NUMBERS and VALUE.

**Ledger field lookup.** `kg_contents.field_lookup(db, names)` (via
`KnowledgeGraph.lookup_fields`) finds where each template field name is among the
headers and labels the files use: exact folded match, then word-set containment,
then same word count with prefixes or common short forms (`_ABBREV`: no/nr/num ->
number, qty, temp, dia, desc, ref, rev, wt), then difflib >= 0.85. Each match says
how many files and values it has (`lookup_summary`). The Ledger's step 1 has
**Find these fields in my files…**, which reads the template's fields
(`LedgerActions.lookup_template_fields`) and opens `TemplateFieldsDialog`
(`council_qt/tabs/ledger_graph_dialog.py`): field -> "as 'Label'" (near matches
are marked to check) -> file and sheet. It reads only; nothing is written to the
template.

Tests: four more in `tests/test_kg_contents.py` (33 in all). The graph,
Connections and Ledger test files all pass; `test_ledger_tab.py`'s
work-off-the-GUI-thread test can time out when run four files in parallel, and
passes alone (same on the base branch).
