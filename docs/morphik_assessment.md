# Morphik's agent creator and knowledge graph — can the Council take them?

Research of 2026-10-05 (three investigators, a synthesis and a fact-check;
corrections applied). Morphik was read from a scratch clone of
github.com/morphik-org/morphik-core (origin/main fc6dd938, 2026-10-05; the repo has
no tags). Nothing in the Council repo was changed by the research. A reference like
`@94e58e4:core/agent.py:244` means that file and line at that Morphik commit.

The user's goal for the graph: **connecting people, parts and projects across the
vault documents**. Constraint: **local-only now** — a networked Council is only a
possible future path (section 6 is a note, not work).

## 1. The short answer

1. **Neither feature can be imported from Morphik today.** The open repo no longer
   contains them: the knowledge graph was deleted on 2026-01-20 (commit 075d67d,
   PR #347, 9,468 lines) and the agent endpoints on 2025-11-26 (2053a34, PR #283).
   Morphik never had a user-facing **agent creator** — only fixed agents (a research
   agent, and a headless extraction agent used by its document Workflows).
2. **Morphik's current code is under the Business Source License 1.1** — source you
   can read, not open source: free production use only below US $2,000/month of
   revenue from that use; it becomes Apache-2.0 on 2029-06-18; copies and derivative
   works stay under BSL and must display it; rights end on any violation. The
   Council's own `licensing.py` describes it as a $99 desktop product, so BSL code is
   a real risk to copy.
3. **The last MIT snapshot** (94e58e4, 2025-06-17 UTC) does contain the graph and
   agent back end outside `ee/`, and that code could be reused with the MIT notice
   shipped inside the installed product (not only in source comments). Morphik's
   graph *screens* are under its separate Enterprise licence (`ee/`: production use
   needs a paid subscription; copying for development/testing is allowed; Morphik
   owns modifications) — and they are React web code, which would not fit a Qt app
   anyway. Running Morphik itself needs Postgres, Redis, a web server and Docker.
4. **Knowledge graph — recommended: build it Council-native,** in one local SQLite
   file. Fill it first, with no model, from labelled fields (Owner, P/N, Project…),
   the existing Collections, and a plain name/code search; record the page, row or
   line behind every fact. Use a local model only for free text, and keep every
   model-proposed link **"suggested" until you accept it** — measured: the best
   local models got only 2–3 of 6 relationships fully right, and none found a
   part→project link.
5. **Agent creator — recommended: turn the existing Specialists into agent
   profiles** (pick tools from a read-only list, a step budget, a model role, an
   output form) that run on the Council's existing safe agent runner.
6. Neither recommendation copies Morphik code, so neither carries licence
   obligations, and both stay local-only.

## 2. What Morphik's two features actually were

| | Open repo today | History | Hosted Morphik Cloud |
|---|---|---|---|
| Knowledge graph | **Gone** | Added 4ae132f (2025-03-17, PR #48); deleted in [075d67d](https://github.com/morphik-org/morphik-core/commit/075d67d6cf70cb8b16e99647b417203028b2f85e) (2026-01-20). Last tree with it: a4d23208. | Closed source; its docs page returns 404. |
| Agent | **Gone** | MorphikAgent added 5538b47 (2025-05-01); ExtractionAgent (headless, for Workflows) added 621e22c (2025-06-24); both removed in [2053a34](https://github.com/morphik-org/morphik-core/commit/2053a34b1a37b8404e035308667c15ea86150cfb) (2025-11-26). | Docs page 404; the homepage now sells back-office "AI workers". |
| "Agent creator" | **Never existed** | A search of all 1,496 commits for agent builder / create agent / custom agent / AgentConfig / agent templates found only one code comment. | Can't be checked. **MorphMind** (doc.morphmind.ai) — a different product — does advertise building agents. |

**Licence history** (`git log --follow -- LICENSE`): MIT from d70f53c (2024-11-22;
the early MIT texts have a literal "..." in the notice clause — cite 94e58e4's
complete text); Apache-2.0 at ec8daec (2025-04-19; that snapshot also has the graph
back end and the graph UI, no agent); from 649462e (2025-04-21) MIT outside `ee/` and
the Enterprise licence for `ee/`; **BSL 1.1 from 6368de0 (2025-06-18) onward**
(Licensor Morphik, Inc.; Change Date 2029-06-18; Change License Apache-2.0).

**How the graph worked** (local mode, at 94e58e4 — summarised, not copied):
- **Build:** one LLM call per chunk (through LiteLLM + instructor) asking for
  entities (label, free-text type, properties) and relationships; suggested types
  PERSON, ORGANIZATION, LOCATION, CONCEPT.
- **Truncation bug:** each chunk is cut to 5,000 characters
  (`graph_service.py:738`) while chunks are up to 6,000 + a 300-character overlap,
  so ~700–1,000 characters of every full chunk (up to 1,300 of the last) are never
  read.
- **Dropped links:** a relationship whose endpoints aren't in that chunk's entity
  list is silently dropped (`graph_service.py:874-910`).
- **Merging duplicates:** an exact-label merge that ignores type (`:581` — a PERSON
  and a PART with the same name become one), then ONE LLM call over all labels for
  synonym groups (`entity_resolution.py:136-269`), silently skipped on any error —
  and a few thousand labels overflow an 8,192-token window.
- **Storage:** each whole graph is one Postgres row of JSONB lists; provenance is
  only document id + chunk number — no page, line or field.
- **Query:** LLM entity extraction from the question, resolution re-run over every
  entity on every query, per-entity embedding with no cache, hop expansion (default
  1 = none), merged with vector hits.
- **Cloud mode:** `GRAPH_MODE='api'` posts document text to
  `https://graph-api.morphik.ai` (`@94e58e4:core/services/morphik_graph_service.py`
  :34-82 helper, :238-245 /build, :317-323 /update; URL at `core/config.py:347`).

**How the agent worked** (`@94e58e4:core/agent.py`): one fixed MorphikAgent with 9
tools (retrieve_chunks, retrieve_document, document_analyzer, execute_code,
knowledge_graph_query, save_to_memory, list_graphs, list_documents,
graph_api_retrieve); a `while True` loop with no step cap (`agent.py:244`); an
`execute_code` "sandbox" that is a regex blocklist and runs `pip install` at run time.
Users could not define or save agents. The nearest thing to a "creator" was document
**Workflows** (ordered steps such as extract_structured / apply_instruction; added
2025-06-24, already BSL, UI in `ee/`; removed 2025-10-19, e327c69).

**Running Morphik breaks several Council rules** (docker-compose, pyproject,
morphik.toml): FastAPI + Postgres/pgvector + Redis/arq + Ollama under Docker, bound to
0.0.0.0:8000; even the Windows "direct" installer needs Docker Desktop for Redis;
telemetry on by default (to logs.morphik.ai at HEAD); it downloads models itself
(ColQwen2.5 at HEAD; the BGE reranker was on by default at 94e58e4, off at HEAD);
default models are not US-origin (qwen2.5vl, BGE).

## 3. What the Council already has, and the gaps

**Overlaps** (paths relative to the repo root)

| Need | What exists | Source |
|---|---|---|
| A user-made "agent" | Personal Specialists: id, name, keywords, system-prompt overlay, base role; JSON; a Qt tab | `specialists.py:31-46`; `council_qt/tabs/__init__.py:54` |
| Goal-driven runs | Agent Jobs: goal, `max_steps`, single/chain mode (chain via `council_core/task_chain.py`), atomic JSON (no pickle); a Qt tab | `agent_jobs.py:72-86, 120-128`; `council_qt/tabs/__init__.py:51` |
| Safe tool use | `AgentPolicy` (allowed tools, file root, `max_steps=6`). The runner's data tools are read-only — **but its allow-list also has `write_tool` and `run_app_tool`** (the model writes Python saved UNREVIEWED under `App_Built_tools/` and runs it sandboxed), so the list as a whole is not read-only | `safe_agent.py:96-105, 438-439`; `agent_jobs_runner.py:17-22, 121-123` |
| A "creator" pattern | The Tool Creation (Forge) tab: describe a tool, the model writes it, the sandbox validates, saved UNREVIEWED | `council_qt/tabs/forge.py:1-13` |
| Per-role models + JSON output | Role→slot map; `local_chat(json_schema=...)` on GGUF and Ollama | `council_core/model_slots.py:61-63, 248-252`; `council_engine.py` local_chat |
| Finding labelled values | Field search + a drifted-label matcher ("POC" ≈ "Point of Contact") | `field_search.py:370, 950` |
| Projects ↔ documents | **Collections** — a user-confirmed PROJECT→DOCUMENT layer; the Qt Vault tab's "Discover" proposes members with reasons (filename, value match, shared join column) | `vault_collections.py:1-21, 239`; `council_qt/tabs/collection_dialog.py`; `council_qt/tabs/vault.py:1095` |
| Part and ID codes | An ID-shaped token regex | `row_citations.py:30` |
| Embedded storage | stdlib sqlite3 3.51.1 with FTS5 and JSON functions (Python 3.11.14, council env) | verified by running |

**Gaps**
- **No knowledge graph anywhere.** The "Grapher" draws data plots (Plotly/Matplotlib;
  `graph_engine.py:1-11`). Nothing imports networkx, spacy or rdflib (networkx 3.6.1
  is installed in the env but undeclared — don't rely on it without declaring it).
- **Specialists have no tools, model or step budget, and the Qt build ignores them:**
  `run_turn` gets only the typed text (`council_qt/tabs/council.py:181-185`), and the
  Qt turn does no vault retrieval at all. The Agents tab says its toggles are
  "recorded, not acted on".
- **No route into answers yet:** `run_turn(extra_ctx=...)` only merges keys into the
  shared context; the only key that reaches a prompt is `tool_payloads`, which reaches
  the synthesiser (not the panel), cut to 900 characters
  (`council_core/deliberation.py:314, 359-362, 486-489`). Tk injects vault context by
  wrapping each model's `respond()` (`council_gui_engine.py:18763-18775`).
- **Field search breaks values apart** — splits on `, ; / and &`
  (`field_search.py:45, 90`): "Lee, Carol" → two people, "Bearings & Seals Program" →
  two projects, "PN-1234/A" → two parts (verified).
- **No locators:** row results are only `{value, file, path}`
  (`field_search.py:950-1060`); PDF pages are joined into one string and capped at 50
  pages (`vault_rag.py:74-97`), so page numbers are lost.
- **The drift matcher isn't wired into live search** (only tests call it), **Qt has no
  field-search route at all**, and the label-vocabulary harvester
  `_known_field_names` lives only in the Tk engine (`council_gui_engine.py:6911-6966`).
- **A new "extractor" model role** must be added to `COUNCIL_ROLES`.
- **Two existing problems the new work must not copy:** the Vault Agent's
  `delete_file` is "confirmed" by the *model* passing `confirm=true`
  (`vault_agent.py:205-235`); `data_index_cache.py:36, 73, 109` uses pickle.
- **Exclusion lists:** graph files at the vault root must be excluded from BOTH
  `PROTECTED_STATE_FILES` (`conversation_logger.py:66-76`) and
  `data_index._APP_INTERNAL_FILENAMES` (`data_index.py:152-180`), and vault_index
  parses `.sqlite` files it finds while `vault_rag`'s walk ignores
  PROTECTED_STATE_FILES — so keep the store and exports in a dot-folder such as
  `<vault>/.knowledge_graph/` (vault_index already skips dot-folders,
  `vault_index.py:2099-2100`). `specialists.json` is on the data_index list but not
  in PROTECTED_STATE_FILES, so extending it into profiles needs that fixed too.

## 4. Options per feature

"Effort" = estimated focused implementation hours by a coding agent (estimates).

**Knowledge graph**

| Route | What you get | Effort | Licence | Fit (offline, US models, no pickle, no server) | Risks |
|---|---|---|---|---|---|
| A. Import Morphik as-is | Morphik's graph inside Morphik | ~30–50 h just to run the Docker stack on Windows | MIT notice if from 94e58e4; BSL for anything later; `ee/` UI not for production without a subscription | **Fails:** server stack, Docker, telemetry, model downloads, Qwen default | An orphaned fork (upstream deleted it); every defect above |
| B. Port parts of 94e58e4 onto `local_chat` + SQLite | A Morphik-equivalent graph | ~20–30 h | MIT notice shipped with the product (the Council has no third-party-notices file yet) | OK only once LiteLLM (installed but undeclared in the env; a plain `import litellm` fetches a cost map from GitHub unless `LITELLM_LOCAL_MODEL_COST_MAP=True`), instructor and Postgres are removed | Keeps the truncation, type-blind merge, window-overflowing merge, chunk-only provenance, per-query LLM cost |
| **C. Council-native (recommended)** | PERSON/PART/PROJECT graph with page/row/line on every fact, cheap seeding from fields + Collections, a review queue | ~43–65 h in stages, **plus ~15–25 h of prerequisites** (estimate; see section 5) | None if no code is copied (ideas aren't covered by copyright — my reading, not legal advice) | **Full** | More work; model-proposed links are unreliable, so they stay "suggested" |

**Agent creator**

| Route | What you get | Effort | Licence | Fit | Risks |
|---|---|---|---|---|---|
| A. Import | Nothing — no agent creator exists in the open repo | n/a | n/a | n/a | n/a |
| B. Port MorphikAgent ideas (tool-description JSON, per-source citation ids) | One fixed agent | ~4–8 h | MIT notice if code is copied | Don't copy its unbounded loop or pip-installing `execute_code` | Little gain: `safe_agent` is already stricter |
| **C. Agent profiles (recommended)** | A Specialist + a tool subset (read-only tools only — **exclude `write_tool` and `run_app_tool`** or label them separately), a step budget, a model role, an output form; run through `ConstrainedAgent` / `agent_jobs_runner` | ~10–16 h | None | Full | Never offer write or delete tools |

## 5. Recommended staged plan

**Prerequisites (~15–25 h, estimate):** field-search split fix ("Last, First", `&`,
`/`) and row/line locators; page-aware PDF text; an "extractor" role in
`COUNCIL_ROLES`; moving `_known_field_names` out of the Tk engine into
`council_core`; and, for answers, the Qt turn wiring (pass Specialists and context
through `run_turn`).

**Knowledge graph**

| Stage | What you'd see | Effort |
|---|---|---|
| KG0 Benchmark | You label 20–50 snippets from your own documents; a table of models × accuracy × time on *your* text picks the extractor model | 3–5 h |
| KG1 Store + seeding | `<vault>/.knowledge_graph/graph.sqlite` (dot-folder; registered in both exclusion lists): documents, entities, aliases, mentions with locators, relations with quotes, an append-only log of your decisions. Seeded with no model from labelled fields you confirm (the drift matcher suggests them), from **Collections** (projects ↔ documents, mapped to stable ids), and from a gazetteer pass (plain search for every known name/code). A **Connections** tab: search a person, part or project, see linked items and the "why" (file, row/line, snippet), open the file there, and a coverage line | 12–18 h |
| KG2 Free-text extraction | A resumable background job per document with pause/resume; only chunks with signals go to the model (`num_predict` ≈ 1,400 — the default 600 would cut a measured 872-token reply; the schema's maxItems caps facts per chunk). The Council, not the model, works out page and line. Everything lands in a "suggested links" list to accept or reject. The extractor bypasses the agent conversation log (`COUNCIL_AGENT_MEMORY_ENABLE`) | 12–18 h |
| KG3 Merge review | "Is D. Whitfield Dana or Dan?" with context; decisions undoable and kept across rebuilds | 8–12 h |
| KG4 Use in answers | When a question names a known entity, panel members and the synthesiser get a cited neighbourhood block — this needs a deliberation change (or a Qt port of Tk's respond() wrapping), not just `extra_ctx` | 8–12 h + part of the prerequisites |

**Agent creator**

| Stage | What you'd see | Effort |
|---|---|---|
| A1 | Tool checkboxes (read-only tools only), a step budget and a model role on Specialists | 6–10 h |
| A2 | "Run as <profile>" in Agent Jobs; the report records the profile and tools used | 4–6 h |
| A3 (after KG1) | Read-only graph tools: `graph_find`, `graph_neighbors`, `graph_sources` | 3–5 h |

**Why links stay "suggested" (measured, one made-up 1,100-character memo, 9 entities,
6 true links, temperature 0, RTX 4070 Laptop):**
- Entity finding is good: llama3.1:8b found all 9 (after the probe's alias table
  merged "HELIOS" and "PRJ-0915"); gpt-oss:20b returned 12 with three spellings of one
  part left unmerged; phi3.5 7/9.
- **Relationships are weak:** endpoint pairs found 4/6 (llama, gpt-oss) and 2/6
  (phi3.5), but **fully correct (right label and direction): llama 2/6, gpt-oss 3/6,
  phi3.5 2/6**; llama reversed SUPERSEDES and mislabelled INSPECTED; gpt-oss reversed
  the same edge and invented one; **no model found either part→project link** — the
  links you asked for.
- A verbatim-quote check locates facts but **filtered none of these errors** (every
  wrong relation quoted a real sentence).
- A Morphik-style single LLM merge merged "Dan" into "Dana" and revision B into A and
  returned a variant not in the input; simple rules got those right.
- (The line-number and merge probes were single unsaved runs.)

**Cost to run** (estimate, method shown): no money — laptop time and GPU heat. Measured
(llama3.1:8b): 872 output tokens at 46.5 tok/s ≈ 18.8 s; ~51–58 output tokens per
returned fact; prompt time negligible. Assumed: 4 page-sized chunks per document,
5–15 facts each → ~7–20 s per chunk → **~0.8–2.2 h per 100 documents** if every chunk
goes to the model (fewer with signal filtering); gpt-oss:20b ~2.4× slower. A
500-document vault: ~4–11 h with llama3.1:8b — hence a resumable background job. On
the Ollama backend there is no Council-side inference lock; the real cost is that a
second model on the 8 GB GPU evicts the chat model (gpt-oss reloads took 37–52 s), and
the same model queues requests. (The probe's per-call reloads were its own short
keep-alive; the Council keeps models loaded for 30 minutes.) KG1's field, Collections
and gazetteer passes use no model and should take minutes (not measured).

## 6. A possible future networked path — a note only

You asked to hold off on any server work; none is proposed. Choices in the local plan
that cost nothing now and keep that door open: stable ids (UUIDs or a hash of type +
normalised key, never names or paths); full provenance on every fact (document id +
content hash, locator, method, model, run id); your merge/split/reject decisions as an
append-only log; a versioned, documented SQLite schema with JSON/CSV export.

## 7. Questions only you can answer

1. Which "agent creator" did you see — the Morphik Cloud console, Morphik's built-in
   research agent, its removed Workflows builder, or **MorphMind** (a different
   product)? A URL or screenshot would settle it.
2. Which folders and document types should the graph cover first? Spreadsheets and
   trackers with labelled columns are cheap; narrative PDF/Word reports need the model.
   The vault on THIS PC has 327 files, mostly code and notes — is the real corpus on
   another machine (e.g. the desktop)?
3. Which labels mean person, part or project in your documents (POC, Owner, P/N,
   Program…)? What do part numbers look like (revision letters, prefixes)?
4. How far back should the first run go?
5. Should agents ever act on files, or stay read-only? (Recommended: read-only, never
   delete.)
6. Should a model-proposed link ever be accepted automatically, or always reviewed?
7. For KG0: are llama3.1:8b and gpt-oss:20b enough, or will you pull a Granite, OLMo or
   Gemma model to compare (the Council won't download them)?
8. When should this start, given Typhon comes first?

## Sources

- Morphik: https://github.com/morphik-org/morphik-core (commits cited inline; LICENSE at
  [fc6dd93](https://github.com/morphik-org/morphik-core/blob/fc6dd9381b5623ed96241904835ba96410b9cdf8/LICENSE);
  MIT text at 94e58e4; `ee/LICENSE` at 94e58e4).
- MorphMind: https://doc.morphmind.ai
- Council: file:line references above (qt-migration, 2026-10-05).
- Probes: the research scratchpad (`morphik/fit-design/probe_*.json`, `kg_probe.py`).
