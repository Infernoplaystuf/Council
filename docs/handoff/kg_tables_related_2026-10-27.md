# Connections: tables as nodes, related by meaning, a model reading tables (2026-10-27)

The user, 2026-10-27: "There are no 'these are related' links between documents
by similar meaning (embeddings). It only links things a sentence states. We
should have these are related connections. ... tables should also be ingested
and have connections made. If need be the tables can be their own node and not
have the values be individual nodes." They agreed the proposal (Parts A, B and
C) with "Begin"; the defaults below were the ones recommended in it.

## A. Tables as nodes (`council_core/kg_tables.py`, `KnowledgeGraph._seed_tables`)

- A table is the columns of one sheet (or CSV, or text table) of the Contents
  layer that share a header row, two or more of them: one TABLE node per table
  region. Values are never nodes.
- Its key is `path|sheet|header row` (the same in any vault built from the same
  files); its name is `file › sheet`, plus `› table at row N` when a sheet has
  more than one.
- Links, all from exact values or the file's own links, so `seeded` at once:
  - `IN_DOCUMENT` its file;
  - `LISTS` each part, person, project or organisation its values name (by
    name or alias; a column of numbers only under an ID-like label);
  - `SHARES_PARTS_WITH` / `SHARES_PEOPLE_WITH` / `SHARES_PROJECTS_WITH` another
    table in another file sharing 3 or more of them, or half of the smaller
    table's (an entity more than 50 tables list is left out);
  - `REFERENCES` from a workbook to the workbooks its external links or
    `'[file.xlsx]Sheet'!A1` formulas read (cached in `doc_refs`).
- These are not facts between entities (`NOT_FACT_PREDICATES`): left out of
  `counts()["links"]`, of `all_relations` and `neighbors` unless asked
  (`include_tables=True`, which the tab passes), and never walked by
  `kg_query` (two hops through a table named unrelated people). They are
  drawn in the picture. `TABLES_VERSION` is in the graph signature.
- Synthetic test set (103 + 40 files): 164 tables, 258 LISTS, 31 SHARES_PARTS,
  73 SHARES_PEOPLE, 5 SHARES_PROJECTS, 5 REFERENCES; build 15 s, unchanged
  rebuild 0.06 s.

## B. Related by meaning (`council_core/kg_related.py`, `KnowledgeGraph.find_related`)

- Graph settings… has "Related by meaning": an embedding model already on this
  PC (Ollama models with "embed" in the name or a BERT-like family, and
  sentence-transformers models in the local Hugging Face cache, loaded with
  `local_files_only` and `HF_HUB_OFFLINE`) and a closeness (default 0.75).
  Nothing is downloaded. Recommended first: granite-embedding (IBM),
  nomic-embed-text (Nomic).
- ⟳ Build graph runs it last. Each document's text in ~200-word passages
  (up to 1,000 per document) and each table's card (file, sheet, columns and
  first rows) is embedded; vectors are float32 blobs in `embeddings`, reused
  until the file changes (no pickle).
- Two items (documents or tables, never two of one document) are `RELATED_TO`
  when the cosine of their average vectors is at least the threshold and each
  is in the other's top 5. The link starts **suggested**; its evidence (one row
  per end) quotes the two closest passages and the closeness.
- ✗ Reject stays: a rejected pair is never suggested again. A changed file
  drops its evidence at the next rebuild; a link left with evidence from fewer
  than 2 documents goes.

## C. A model reads each table (`council_core/kg_table_read.py`, `KnowledgeGraph.read_tables`)

- After the model reads the text in ⟳ Build graph, this PC's model reads each
  table's card, once per table version and model: one sentence on what the
  table records (the TABLE node's description) and which columns hold people,
  parts or projects.
- Every claim is checked: the column must be the table's; the type PERSON, PART
  or PROJECT; the label not a date, measurement, status or note label, and not
  a person's label for a part or project; most values must fit (a person has
  no digits; a part is never a measurement, nor a bare number unless the label
  says ID; a project is a name or code, never initials or a sentence).
- What survives becomes a **proposed** field rule. 'Tick the suggested field
  rules' (or Fields…, which says which table the model read it in) ticks it,
  and the next rebuild reads every row of every spreadsheet and CSV with that
  column. A label that already has a rule (ticked, proposed or rejected) is
  left as it is. PDF and Word tables get the description but rules are read
  from spreadsheets and CSVs only.
- A careless stand-in that called every column of the synthetic set a person,
  part or project (7,336 claims): 7 survived as proposals, none applied.

## Open

- Run with real models on the user's PC (the user: "We'll run a test build
  later"): how many related links at 0.75 with granite-embedding, and whether
  gemma3:12b's column meanings are worth ticking.
- The five Ledger findings from the synthetic rerun still wait for the user's
  OK (`synthetic_test_fixes_2026-10-27.md`).
