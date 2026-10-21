# Handoff, 2026-10-21: the Connections tab works like Morphik

Branch `qt-migration`. Read this before changing `council_core/knowledge_graph.py`,
`kg_extract.py`, `kg_graphs.py`, `kg_query.py`, `kg_layout.py`,
`council_qt/tabs/connections.py` or `connections_dialogs.py`. It **supersedes** parts
of `knowledge_graph_desktop.md` §2 and of `morphik_assessment.md` §1, 4 and 5 (listed
below); everything else there still holds.

## What the user asked

"Please verify the functionality of the connections tab … The tabs job is to create
knowledge graphs in a way similar to Morphik." A verification found the tab did what
it was built for (on a new, differently-shaped vault: 15 of 15 labelled links, none
wrong, every one cited), but a notes file naming a lead, a part and a supplier gave
**0 entities and 0 links, and no model was asked**. The user then said: "I would like
the connections tab to function very similar to morphik. Another session worked on the
connections tab and I believe it made some incorrect assumptions."

## The user's decisions (2026-10-21)

These four earlier assumptions were **wrong**:

1. **Fixed entity types** (people, parts, projects only). A model may name any type:
   ORGANIZATION, LOCATION, EQUIPMENT, MATERIAL, PROCESS, REPORT, EVENT, CONCEPT, or its
   own.
2. **The model may only link names labelled fields already gave.** The model now finds
   entities **and** relationships in any passage, as Morphik's did.
3. **Field rules to tick before anything is read.** The default rules start ticked.
4. **One graph per vault.** Named graphs, each over the folders (and file types) the
   user picks, as Morphik's `create_graph(name, filters)`.

Also decided:

- **A model's links go in the graph at once** (status `seeded`, evidence method
  `model` with the model's name and the quote). The user can still ✗ Reject any. Each
  graph can be set to "wait for me to accept each one" (Graph settings…).
- **The graph is used in the Council's answers** (KG4), **Ask the graph** in the tab,
  and a **graph picture**.
- **The no-model reading stays as an extra source**: labelled fields, tables, Ledgers,
  workbooks and Collections still give firm, cell-cited links, with no set-up.

Superseded: `knowledge_graph_desktop.md` §2 "every model-proposed link is 'suggested'
until the user accepts it" and "Entities: PERSON, PART, PROJECT"; `morphik_assessment.md`
§1.4 and §5 "keep every model-proposed link suggested", "fill it first with no model"
as the only route for anything that is not prose; the KG0 numbers still stand and still
pick **gemma3:12b** as the default reader.

What is **not** copied from Morphik (still true, `morphik_assessment.md` §2): no text is
cut off (a passage is sent whole), a PERSON and a PART of one name are never merged,
names are merged by rules (not one model call over every label), and every fact keeps
its page/line and quote. No Morphik code; local only; US-origin models only; the
Council never downloads a model.

## What changed

- **`kg_extract`** — open extraction (`extract_open`, `check_open`, `OPEN_SCHEMA`):
  entities `{name, type, description}` and relationships `{source, relation, target,
  quote}`. Kept only when the passage supports them: the entity is named in it (or is a
  known entity the passage names by another of its names); the quote is in it; people and
  parts named near the quote. `canonical_type` maps 'Engineer' → PERSON, 'Supplier' →
  ORGANIZATION, 'document' → REPORT (DOCUMENT is the graph's own type for files);
  `relation_direction` maps 'replaces' → SUPERSEDES and turns 'used on', 'led by',
  'replaced by' round. 'Falcon Wing Retrofit (KA-101)' is one entity (the code its name).
  Chat functions take `schema=` (None = free text, for answers).
- **`knowledge_graph`** (schema **v4**: table `entity_props`):
  - `extract_from_text(workers, model=…)` reads **every** passage of every text document
    (workbook notes, text boxes and pictures included; CSV/JSON tables give theirs
    through their columns). Resumable (`extraction_done` model `open|<model>`); an
    unchanged document with the same focus is not read again.
  - A model's entities, mentions (method `model`), names (alias source `model`) and
    descriptions stay across rebuilds until their document changes or goes.
  - `model_link_status()` / `set_model_links('graph'|'review')` (meta `model_links`);
    `focus()` / `set_focus()` (meta `focus`, the "what to look for" in the prompt).
  - The default field rules are inserted **confirmed**; an older store is ticked once
    (meta `rules_on_by_default`) unless the user ever ticked or unticked a rule.
  - The gazetteer searches every type; `entity_key` of any other type folds the name
    (a code stays a code).
  - "Why no links?": `no_ledger` comes last and only at 0 links (it led for every vault
    without a Ledger); new `text_not_read` for a vault of prose a model has not read;
    `model_read()` counts what a model read.
  - Constructor `store=` and `scope=` (a named graph's store and folders/file types).
- **`kg_graphs`** — `GraphList(vault)`: `main` ("All documents", the old store, cannot be
  deleted) and named graphs in `<vault>/.knowledge_graph/graphs/<id>/graph.sqlite`, the
  list in `.knowledge_graph/graphs.json` (atomic; a damaged list is reported, never
  overwritten). Folders inside data_in or outside the vault (added read-only to that
  graph). `use_in_answers` per graph. Deleting removes only that graph's own folder.
- **`kg_query`** — entities named in a question (runs of up to six words, by name and
  alias, no model call), links two hops out (accepted first, rejected never, at most 15
  from one entity), the shortest chain between two named ones, every fact numbered and
  cited. `ask()` answers from those facts with a local model, or lists them without one.
  `block()` is what the Council's turn uses.
- **`turn_context.knowledge_graph_block`** (KG4) returns that block for every graph
  marked for answers; a graph never built is skipped and asking creates no store.
- **`kg_layout`** + **`council_qt/widgets/kg_canvas.py`** — the picture: an entity and
  what is within 1–3 links (or the best-connected 120 of the whole graph), Fruchterman-
  Reingold in numpy with a fixed seed; dots by type, arrows subject → object, suggested
  dashed; click opens, double-click centres.
- **Connections tab** — Graph picker, New graph…, Graph settings…, Delete graph; "Read
  the text with" (installed US-origin models, GGUFs, or "No model"); ⟳ **Build graph**
  (reads the documents, then the text with the model; Stop resumes later); a type filter
  that grows with the types found; **Ask**; tabs Links / Picture / Answer. "Suggest links"
  is gone (Build does it; `ConnectionsActions.suggest` became `read_text`).

## Verified here (no real model in this container)

- New tests: `test_kg_open_extract.py`, `test_kg_graphs.py`, `test_kg_query.py`,
  `test_connections_morphik.py`, plus open-mode cases in `test_kg_suggest.py` and a real
  KG4 case in `test_turn_context.py`. Tests that encoded the old assumptions were changed
  to the user's decisions (each says so).
- Scripted models only. **Not yet measured: the open prompt on a real model.** KG0
  measured the closed prompt (links among known names); the open prompt asks for more
  (entities of any type too). Before relying on it, run Build on the Ironbridge corpus
  and on real documents with gemma3:12b (and phi4:14b, gpt-oss:20b) and compare against
  `kg_corpus.answer_key()`; `kg_bench.py` scores the closed prompt and would need an open
  mode for that.

## Still open

- `kg_bench` has no open-mode scoring yet (above).
- Merging near-duplicates a model writes differently ("Nordic Forge" / "Nordic Forge AS")
  is left to the user's "Same as…"; Morphik used one model call for this, which the
  laptop measured to be wrong (`morphik_assessment.md` §5). A rule-based "these look the
  same" question list would be the next step.
- The agent tools `graph_find` / `graph_neighbors` read the main graph only.
- Ledger tab "Rebuild knowledge graph" builds the main graph without a model, as before.
