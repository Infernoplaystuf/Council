# Qt port — what is left (qt-migration @ e8b8fcf, 2026-10-06)

Three surveys (tabs and dialogs; the Council turn; startup flows), each gap verified
offscreen with temp vaults and fakes, then a fact-check (corrections applied). Effort
figures are ESTIMATES. Replaces the "left" picture of `docs/qt_full_port_scope.md`
(2026-09-17); its packaging phase (phase 11) still stands.

## 1. Short answer

- **The Qt Council cannot answer questions about your vault today.** It sends only the
  typed question to the models (`council_qt/tabs/council.py:181`, `:197`). A fact planted
  in a temp vault reached 0 of 13 prompts with deliberation on, and 0 of 1 in the default
  build; Tk, same vault, put it in the Writer's prompt, used it, and showed a file chip.
- **The shell is done; the Council's insides are not.** All 22 Qt tabs build offscreen on
  a fresh checkout (Tk has 20; Qt's 16 default include Capture and Docs, plus 6
  advanced), 1,982 Qt tests pass. Vault operations, IDE/Runner, Librarian, Nodes,
  Dream3D, Apothecary, GUI Designer, Models, Lens, Jobs, Forge and Changelog work.
- **The launchers already start Qt** (`run-windows.bat`, `run-linux.sh`, `run-wsl.sh`;
  `launch_council.bat` inherits it), so launcher users get the version without data
  answers. `--tk` (or a one-line change in each of the three scripts) gives the Tk app.
- **The default build is a single Writer in BOTH shells.** `DEMO_MODE` defaults on
  (`branding.py:25`) and Tk also sets `COUNCIL_SINGLE_VOICE=1`: one `writer.respond`
  call, no panel. README lines 5, 16 and 17 (a deliberating panel, auto-summoned
  Specialists) are untrue in the default build of both shells.

## 2. Corrections to earlier notes

- Tk adds vault context by putting the vault blocks in front of the question
  (`_inject_file_contents`, `user_text = augmented`, `council_gui_engine.py:18353`), so
  every role and the judge see them — not by wrapping `respond()` (that carries
  instructions, specialist overlays, content style, LaTeX, the director brief and the RAG
  briefing).
- `tool_payloads` reaches the writer, intern and peasant drafts and the synthesis (900
  chars each), not rebuttal, cross-fire or judge.
- Tk's advanced RAG/Librarian briefing never returns anything (it misreads `RAGResult`).
- **`COUNCIL_DEMO_SILO`** (set by default in every Tk build, `council_gui_engine.py:19`;
  not set by `council_qt.py`) blanks recent conversation, the prior session, role and
  project memory and the profile in every prompt (`council_engine.py:5743-5748`). So
  "the next turn never sees the previous message", "Load as Prior changes nothing" and
  "no learned memory" are true of Tk too — Tk carries turn-to-turn context only through
  retrieval state (task memo, referenced files, filename pins). With SILO unset (Qt
  today), memory and profile that Tk wrote DO reach Qt prompts, and Qt cannot turn the
  profile off. **SILO is a product decision that comes before the "remembers" batch.**
- Under the default build (DEMO_MODE + SINGLE_VOICE) Tk also ignores instructions,
  content style, Tools, Fill IDE, Profile and the Specialist pin, and hides the verdict
  bar. Those P1 impacts are real only for `DI_DEMO_MODE=0`, where Qt also differs from Tk
  by running `judge.route` and a 3-role panel (13 calls) at 2/2 rounds — which reaches
  the documented deliberation defects A7/A8.
- The missing `convo_store` module (`council.py:133`, `sessions.py:51`) breaks only the
  two READERS (History, the Sessions list — which would otherwise list Tk's sessions).
  Qt's send path never writes a conversation, question history or verdict at all.
- "Setup needed" is wrong only for vaults without `.onboarded` (setup.bat /
  `setup_council.py` installs and Qt-first users); conversely a vault with `.onboarded`
  but no model gets no notice.
- A double-clicked launcher does have a console window; the crash report is one line in
  it, behind the app, with no dialog.

## 3. Gap table (estimates)

### P0 — the headline feature, and bugs that make Qt look broken

| Feature | Qt state | User impact | Effort | Depends on |
|---|---|---|---|---|
| Vault retrieval in the turn (vault matches, value matches, dataset overview, analyst, task memo, file injection, fast answers) | missing | Cannot answer from your files | 19-30 h: move ~2,690 toolkit-free lines (55 names) to `council_core` + `prepare_turn()` + a per-session state object (task memo, referenced files, filename pins, warm data index) | Merge `knowledge-graph` first (it changes `vault_analyst.py`, `vault_rag.py`, `data_index.py`, `field_search.py`) |
| Source chips under the answer | missing | No citations | 3-5 h | Retrieval; exclude app files (`specialists.json`) from `vault_index` via `PROTECTED_STATE_FILES` |
| Context-window warnings (prompt > n_ctx or > 80%) | missing | Truncation silent once files are injected | 1-2 h | Retrieval |
| Default build direct path gets the context | broken | The default build is a plain chatbot | ~1 h | Retrieval |
| Vault: Build Descriptions / Embeddings | broken — stubs at `vault.py:232-233` hide the real methods at `:204`, `:213`; TypeError, and all three index buttons stay disabled | No semantic/vector index | 0.5-1 h | none |
| Answer shown twice | broken | Every answer appears twice | 0.5-1 h | none |
| Vault Health stays on "Reading…" after a failed read | broken (`vault_health.py:122-127`) | Looks hung | 0.5 h | none |
| "Setup needed" notice | wrong for vaults without `.onboarded`; missing for `.onboarded` without a model | Misleading | 1-2 h | none |
| Sessions / History readers (`convo_store` missing) | broken | Sessions and History empty | 1-2 h for the readers; saving is Batch 1 | SILO decision for what is fed back |

### P1 — controls that are visible but do nothing (real for `DI_DEMO_MODE=0`; UI-missing in the default build)

| Feature | Qt state | Effort | Depends on |
|---|---|---|---|
| Personal Specialists: pin + auto-summon + Test window | missing | 6-9 h | Per-role context carrier; single-voice policy |
| Look Up + results dialog (Matches / Column Match / Connections = files sharing a column, `data_index.find_relationships`) | missing | 5-12 h | Link entity hits to the KG Connections tab (already on `knowledge-graph`); keep the two views separate |
| Find & Chart → Grapher | missing | 3-5 h | `GrapherActions.load` exists |
| Defer to Vault dialog | missing | 4-6 h | none |
| Instruction bar, Manage…, Content Style… | missing | 6-10 h | Carrier |
| Verdict Agree/Disagree, Verdict History, Re-deliberate | partial (`NotYetExtracted`) | 4-8 h | Turn accepts an objection |
| After-turn learning (role/project memory, session naming, provenance) | missing | 3-5 h | Session id; SILO |
| Load as Prior; Analyse Trends | partial | 3-5 h | Sessions store; SILO |
| Expand with council | broken (same single answer) | 1-2 h | May DEMO_MODE run a full council? |
| Switches: Adversarial, clarification, Profile, Fill IDE, Judge panel, Robust voices, query mode, rounds | partial | 6-10 h | Carrier |
| Tools switch (read, but `run_turn(tools=None)` → empty tool table) | broken | 3-5 h | Tools policy — use KG's `agent_profiles` (read-only built-ins + approved council-built tools)? |
| Per-role backend Override (Tk has 5 combos; Qt's one is never read, `council.py:810`) | broken | 1-2 h | Carrier |
| Remote nodes never reach the Qt Council (`dispatcher=None` in `load_personalities` and `sessions.rebuild_with_prior`) | missing | 1-3 h | Engine area; the specialized-nodes plan |
| Specialist-model swap advisor (`_maybe_suggest_model_swap`) | missing | 2-4 h | none |
| Reports from other tabs (`window.append_transcript`) and Fill IDE | partial — IDE/Librarian/Nodes notices dropped | 2-3 h | none |
| Save answer / Save output (Tk builds a Markdown report with sources; valid LaTeX export) | partial — raw text only; Qt's `.tex` is not LaTeX | 1-2 h | none |
| Inert status widgets (`agent_label`, `tps_label`) | broken | 0.5-1 h | none |
| Onboarding wizard (`launch.py` already calls `window.open_onboarding` if present) | missing | 16-24 h | none |
| Crash dialog (with the Qt version in the report) | partial (console line) | 3-5 h | none |
| Engine settings at startup (n_ctx, GPU layers, embedding device) + dialog + error coach | partial — a context size saved in Tk is dropped | ~1 h + 5-9 h | none |
| Diagnostics: dependency report, vault path, Copy | partial | 2-3 h | none |
| Vault folder setup and legacy-path migrations | missing | 1-2 h | none |
| "What can I ask?" clickable | partial | 1-2 h | none |

### P2 — completeness

| Feature | Qt state | Effort |
|---|---|---|
| Vault data stats, startup precompute, `pins.json` reload | missing | 5-9 h |
| Clear the RAG miss log; actionable search results | partial | 2-3 h |
| RAG index at startup (`Plan.start_rag` computed, never used) | missing | 2-3 h (coordinate with KG indexing) |
| Speech: record/transcribe/send, speak answers | partial | 3-5 h |
| Grapher: Sample, Browse, sheet picker, live reload, Pin, Narrate, exports, AI Assist, quick stats, plot council table (fix B6's temp-CSV leak when porting), presets, correlation drill | partial | 16-28 h |
| Typed commands before the council: **52 regex routes** + `run workflow …` (Tk's only multi-pipeline Dream3D route) + `peek`, ~2,700 lines of handlers (field search, vault term search, stats, memo, grep, find-column, forget X, provenance: verify last answer / where did X come from / show last context, SQL connections, image stats/OCR, create a tool, export transcript, context info, show/clear learned) | partial (PipelineChat, ModelChat only) | 20-35 h if handlers move unchanged |
| Agents tab toggles (Coder, Intern, RAG for Writer) | partial (recorded, unused) | 8-12 h |
| Stop for a running turn | missing in both shells | 5-8 h |
| Crash recovery (Tk switches to the orphaned session but loads no transcript) | half-built in Tk | 4-6 h |
| UI scale shortcuts | missing | 1-2 h |
| Launchers forward arguments (`--advanced` ignored) | broken in both | 0.5 h |
| Setup scripts launch Tk and promise an in-app wizard | partial | 1-2 h |

### P3 — commercial channel or niche

| Feature | Qt state | Effort |
|---|---|---|
| Database Connections panel and `db_connect_wizard` | missing | 10-24 h |
| Web Scraper | missing | 6-10 h |
| Licensing / activation / trial gate (home build skips it in Tk too) | missing — a Qt commercial build would be ungated; a 0.5 h guard refusing Qt when `DI_DEMO_MODE=0` closes that | 6-10 h |
| Update-check notice (manifest URL empty in the home build) | missing | 2-3 h |
| Qt in the .exe (`council.spec` packages Tk, excludes PySide6) | not started | 5-9 days |

## 4. Recommended order (each batch shippable)

- **Decide first:** (1) SILO — should earlier conversations, prior sessions, learned memory
  and the profile reach prompts? (2) Single voice — keep the default build a single
  Writer, or allow the full council? (3) Tools policy — reuse KG's `agent_profiles`?
  (4) Launchers — keep Qt as the default before retrieval ships, or point them at Tk?
- **Batch 0 — quick fixes (~10-16 h, no decisions, no KG overlap):** the Vault stubs
  (+ try/finally, a real-tab test), answer once, Vault Health failure, `append_transcript`,
  the "Setup needed" check, apply saved engine settings at startup, vault folder setup and
  migrations, launcher argument forwarding, Diagnostics, inert status widgets, label the
  controls that do nothing ("not available yet") and fix the "or press Stop" text.
- **Batch 1 — Qt remembers (~10-20 h, after the SILO decision):** write conversations
  (`ConversationStore`, a session id per launch, `ConversationLogger` on shutdown), the
  History and Sessions readers, verdicts recorded, Load as Prior wired.
- **Batch 2 — answers from your data (~22-36 h; merge `knowledge-graph` first):**
  `council_core/turn_context.py` (moved unchanged) + `prepare_turn()` + a session-state
  object; `TurnResult.sources` + source chips; context on the direct path; context-window
  notices; planted-fact tests for both shells; Tk calls the same function. This port
  owns the context seam; KG4's cited neighbourhood block becomes one more block in it.
- **Batch 3 — per-role context carrier (~21-35 h):** a per-turn proxy per model
  (replacing Tk's `respond` monkeypatching), then Specialists, instructions, content
  style, switches, Override, Expand, Tools.
- **Batch 4 — the rest of the Council bar (~16-30 h):** Look Up (linking to KG
  Connections), Find & Chart, Defer to Vault, "What can I ask?", after-turn learning,
  Re-deliberate, the swap advisor, remote nodes in Qt.
- **Batch 5 — first run and trust (~25-40 h):** onboarding wizard, crash dialog, engine
  settings dialog + error coach, setup scripts launching Qt.
- **Batch 6 — robustness and completeness (~38-64 h):** Stop, crash recovery, UI scale,
  vault stats/precompute/pins reload, RAG at startup, miss log, speech, Grapher.
- **Batch 7 — after the KG merge (~23-47 h):** typed commands (needs `_known_field_names`
  moved into `council_core` — **not done on `knowledge-graph` yet; assign it to one
  branch**), Agents toggles (port the RAG briefing with its fix, or let KG4 replace it).

Totals (estimates): parity excluding P3 ≈ 170-295 h; Batches 0-2 ≈ 42-72 h ("Qt answers
from your data, cites files and remembers sessions").

## 5. Drop, or keep Tk-only

- **Drop:** Tk's Background Deliberation Queue window (never called); the RAG/Librarian
  briefing as written (always empty); Tk's automatic lookup routing (pre-empted by the
  vault-injection gate — route explicitly in Qt); injecting app files such as
  `specialists.json` as vault matches; Grapher B7/B8 (designed out).
- **Tk-only for now:** licensing/activation/trial/update notice (home build skips them;
  add the 0.5 h DEMO guard); Database Connections + wizard; Web Scraper.
- **Hide or label until supported:** Judge panel, Robust voices and any Council button that
  still says "needs the deliberation extracted".

## 6. Overlap with the desktop's `knowledge-graph` branch (as of origin e185933 / c53698b)

It adds the Connections and Agent Creator tabs (REGISTRY: 18 default, 24 total after a
merge), `council_core/agent_profiles.py`, the KG store, field-search fixes and locators,
`vault_analyst.py` / `vault_rag.py` changes, a Pi setup system (`council_core/pi_setup/`,
`pi_setup_dialog.py`), and edits `conversation_logger.py` (PROTECTED lists) and
`data_index._APP_INTERNAL_FILENAMES`. It does not touch `council_turn.py`,
`deliberation.py`, `council_qt/tabs/council.py` or `council_gui_engine.py`. Its merge-base
is qt-migration, so Batches 0 and 1 can proceed in parallel; Batch 2 and 7 should follow a
merge.

## 7. Status after Batches 1 and 2 (branch `qt/batch1-2`, 2026-10-12)

Decisions applied (6 October 2026): memory "This conversation only"; default build one
Writer; launchers unchanged (Qt).

**Batch 1 — Qt remembers.**
- `council_core.memory_scope`: `COUNCIL_MEMORY_SCOPE` = `none | conversation | all`
  (explicit value wins; else `COUNCIL_DEMO_SILO` means `none`; else the caller's default —
  `all` for the engine, `conversation` for Qt). `PersonalityModel.memory_scope` lets Qt set it
  per model. In `conversation` a prompt gets this session's RECENT CONVERSATION and, only
  after Load as Prior, PRIOR SESSION CONTEXT — never role/project memory or the profile.
  Tk keeps SILO (`none`) unchanged. The toolbar says "Memory: this conversation only".
- `council_core.conversations`: `ConversationStore` moved out of the engine
  (`council_engine.ConversationStore` is the same class; cache keyed on the file — the
  personalities' own store served stale history after another store appended; one-write
  appends; `claim()`). `Conversation` = one Qt launch: Tk-format session id claimed
  exclusively (a second launch in the same second gets `_2`; a Tk-taken id moves to a fresh
  one), the same files as Tk (`conversations/<id>.jsonl`, `conversation_logs/`,
  `question_history.json`, `verdict_history.jsonl`). The question is saved when asked, the
  turn when it ends (the answer with provenance), anything pending on close
  (`window.close_hooks`). A full council's drafts/cross-fire go to the debug log only (kept,
  one turn filled the 12-entry history). Lines that quote other sessions (History's list,
  a loaded session's name) are shown but not kept.
- History and the Sessions list read both shells' files. Load as Prior sets the prior on the
  conversation (no rebuild — the old rebuild went to a callback nobody connected) and the
  transcript says so. Sessions are named after the first answer (judge after a
  deliberation; the question's words on the fast path). Verdicts are recorded with a unique
  id; the verdict bar stays disabled. Role/project memory is not written by Qt (never read
  in this scope; Tk writes it only after full deliberations anyway).
- `question_history.json`: a cross-process lock (on Windows a just-deleted lock file is
  "access denied", which had sent writers on unlocked) and an unreadable file is never
  written over.

**Batch 2 — answers from your data.**
- `council_core.turn_context`: the Tk module's lines 330-3515 moved (65 names re-exported;
  `VAULT_DIR` reads became `_vault()`, the index accessors ask the prepared turn's session
  first). `prepare_turn()` is the Tk `_send` sequence once — task memo, analyst step (a
  direct answer comes back as `fast_answer`), injection, sources, context-window warning,
  provenance — and BOTH shells call it (`CouncilConsole._turn_state` gives Tk's own state,
  `module_indexes=True`). `TurnState` is one conversation's retrieval memory.
- Qt: the augmented text goes to the Writer on the direct path and to the council; the judge
  routes on the typed text (`run_turn(route_text=...)`, A5); deterministic answers need no
  model; source chips under the answer; provenance carries the sources.
- Vault search no longer returns older conversations or learned memory: `logs/`,
  `memory/`, `sage_knowledge/`, `verdict_history.jsonl`, `vault_rag_misses.txt`,
  `librarian_wishlist.md`, the root `trends.md` and the app's settings files are protected
  (measured before: "search the vault for X" injected older questions from the verdict
  history and the miss log).
- Not done: KG4's neighbourhood block (not built on `knowledge-graph` either — it becomes
  one more block in `prepare_turn`); Expand with council (Batch 3); the per-role carrier.

**Batch 2, finished (2026-10-12, same branch).**
- A source chip shows the file READ-ONLY in the app (its first rows and columns, no model —
  Tk's `_preview_data_file`) with a "Show in folder" button that selects it in Explorer /
  Finder; a file that has gone since is said. It used to open the folder only. Nothing
  writes to the file; an offscreen run never starts the file manager
  (`council_qt.widgets.source_preview`).
- "forget WORD", which the close-match notice under a vault search offers in both shells,
  works in Qt (`turn_context.forget_command`; before, Qt sent it to the Writer as a
  question). It takes only words the notice offered in this conversation — Tk takes any
  "forget …" and put "what", "said" … on the list; Tk's handling is unchanged. The list
  (`fuzzy_denylist.json`) is written atomically and never over a file that cannot be read.
- Context-window notices in plain words in both shells ("too long for what the model can
  read at once", "fills 85% of …", "Left out 2 parts of what was found …"), naming the
  setting that sizes THIS model's window — when Ollama serves it, the main slot's `n_ctx`
  in `model_slots.json` if set, else `COUNCIL_OLLAMA_NUM_CTX`; otherwise
  `COUNCIL_GGUF_N_CTX` (Tk always said the GGUF one). The "nearly full" notice comes at
  the engine's answer reserve (75% of the window; earlier on windows under 1,024 tokens)
  and never later than 80%.
- `council_core.vault_setup.VAULT_ROOT_STATE` is in `data_index._APP_INTERNAL_FILENAMES` and
  `conversation_logger.PROTECTED_STATE_FILES` (measured before: `model_slots.json` and
  `model_bench.json` reached the prompt on "search the vault for X"; the data index's own
  migration copied all eight into `data_in/`).
- The seam names its three blocks (`turn_context`, above `prepare_turn`): the vault (in
  front of the question), this conversation (Batch 1, per role inside `respond()`), and
  the knowledge graph's cited neighbourhood — `turn_context.knowledge_graph_block`, a hook
  that returns None until KG4. What it returns is assembled with the vault blocks
  (`_inject_file_contents(extra_blocks=…)`, priority band PRIO_EXTRA: below named files,
  above the vault search) and cites its files under the answer when it is kept.
- `tests/test_planted_fact_both_shells.py`: one FakeOllama, a question that names no file —
  Qt as the app builds it (default build; the panel with `DI_DEMO_MODE=0`) and Tk (real
  `_send`/`_poll_ui_queue` on a stand-in, no window): the fact is in every Writer prompt,
  the answer, the chips and the saved sources; both shells give the Writer the same vault
  text; a follow-up gets turn 1 and the file, and not an older session. On 21f7e18 the Qt
  Writer was sent the bare question and 0 of 12 panel Writer prompts had the file.
- Open, found while doing this: (1) RECENT CONVERSATION includes the question being
  answered (it is saved when asked, and `respond()` reads the session file), so it appears
  twice in each Qt prompt — Tk with SILO off does the same; (2) FIXED: the injection was
  sized with `get_n_ctx()` (the configured 4,096 when unset) while the notices measured the
  real window (`effective_n_ctx`: 8,192 for Ollama by default, 16,384 for a GGUF the ladder
  loaded so) — `prepare_turn` now sizes it with `effective_n_ctx`, in both shells, as the
  engine's own note on `get_n_ctx` asks; (3) the Tk stand-in
  tests share the Tk console's process-wide indexes unless reset (fixed in the tests);
  (4) the analyst's own notices ("Analyst direct-routed to folder_data_summary …", "Context
  retrieval failed with an unexpected error …") are still Tk's technical wording, now in
  both shells.

**Review fixes (2026-10-12, same branch).** Each was reproduced by a test first.
- Every Qt tab's personalities get the Qt scope (`council_turn.load_personalities(scope=…)`,
  default `memory_scope.for_qt()`; `sessions.rebuild_with_prior` too). The Lens and the
  Designer's Review — default tabs — sent role/project memory and the profile before; the
  default session id of a non-conversation tab is "" (no RECENT CONVERSATION), not "qt".
- Naming the vault folder in a question ("What is in <vault>?") no longer lists
  `conversations/`, `conversation_logs/`, `logs/` or `memory/` (both folder renderers also
  check the protection against the vault, not only the named folder's parent).
- A vault that cannot keep conversations (read-only, permission denied) no longer stops the
  Qt app from starting; the transcript says the conversation is not being saved and the
  toolbar says "Memory: not saved".
- Appends are atomic between processes on Windows (`append_line` opens with
  `FILE_APPEND_DATA` only; `open("a")` lost records under load). Tk's verdict append uses it.
- Load as Prior loads the listed file (a stem `safe_name` would change — "pump notes",
  "older - Copy" — is reached by its own name); a prior another launch renamed away is
  dropped and said at the next turn; a session line that is not a record no longer fails
  every turn; a non-ASCII judge name becomes the stored file stem.
- The first verdict of a judged session is recorded under the session's name (rename
  first).
- The turn's own notices ("Tracking → goal", "Reading: …", context-window warnings, the
  note under an answer from the data) are kept in the conversation file as Tk keeps them;
  Qt writes `logs/council.log`; each entry is logged when it is said, under the id it was
  said under (tests/test_shell_parity.py compares one turn in both shells).
- Importing the Tk module no longer turns Qt's memory off: Tk sets SILO through
  `memory_scope.set_tk_default()`, which marks it `COUNCIL_DEMO_SILO_SOURCE=tk-default`,
  and `for_qt` ignores a SILO so marked (a user's SILO still counts).
- The Tk console claims its session id on disk at start (`conversations.claim_session_id`,
  the Qt rule), so Tk and Qt in the same second never share a session, whichever starts
  first. Tk's rename accepts a claimed `_2` id and keeps the stored stem.

Still open after the review (not fixed on this branch):
- Tk records its first verdict before naming the session (its record carries the id at
  queue time), so Tk's one-deliberation sessions still have no badge.
- Tk's Agree/Disagree (A3) stamps `user_agreed` on the LAST line of
  `verdict_history.jsonl` — now possibly a Qt verdict — and rewrites the whole file with
  `write_text` (not atomic; on Windows CRLF; `ensure_ascii` escapes). Fix with the verdict
  bar in Batch 4: stamp by `verdict_id`, append a response record instead of rewriting.
- Tk's council.log is still appended with `open("a")` (can lose a line to a concurrent
  append on Windows); Tk's "New session" menu action does not claim its id.
- Qt writes no crash-recovery sentinel (`conversations/<id>.active`) around a turn, as Tk
  does — Batch 6 (crash recovery).
- The notices Qt now keeps take slots in the Writer's 12-entry RECENT CONVERSATION (about
  three entries per default-build turn instead of two), as they would in Tk with SILO off.

**Second review fixes (2026-10-12/13, same branch).** Each was reproduced by a test that
fails on the commit before it (two junction tests record a decision and pass before too).
- The vault blocks are sized for what the Writer's call leaves, not for 75% of the window
  less a guessed 500 tokens. `council_engine.prompt_room` counts the call the way the
  engine's clamp fits it (the reply — 2,400 for the Writer, at most half the window, plus
  thinking headroom when the model thinks — the template margin, chars/3 or denser when a
  model was measured so); `PersonalityModel.prompt_frame` says what the call sends besides
  the text (its system prompt, this conversation's turns). `prepare_turn(writer=…)` uses
  both; Qt builds the personalities (no model loads) before preparing, Tk passes its Writer.
  Measured before: on Ollama's 8,192 the engine cut the middle out of a named file with no
  notice. Several named files share the room (head and tail of each). The context-window
  notice measures the whole call: "too long … part of it will be cut out", or "nearly
  fills" above 90% of the room. At 4,096 the Writer's own prompt leaves little room: vault
  blocks are left out, and said.
- A chip names the file its block was read from (it was the first file of that name in
  `data_in/`; a match outside `data_in/` had none); a named file left out, missing or
  unreadable gets no chip; a folder chip lists the folder; a click reads the file on a
  worker; previews read only the start of a file (large JSON/parquet are not parsed whole).
- The data index keeps its own files in `data_out/.index/` as gzip JSON (no pickle), writes
  them only when a user's file changed, and the older builds' copies in `data_in/` are not
  data anywhere (discover, the overview and its signature, the file count). Measured before:
  every refresh rewrote them, so the dataset overview was rebuilt on every turn.
- Not vault matches or data any more: the app's own `data_in/README.txt` and
  `data_out/README.txt` (while they hold the app's text), and files named like the app's
  state in `data_in/` (the vault search already treated them so). A file outside the vault
  that shares an app-state name is read when named; an app file inside the vault that is
  named is not read, and that is said. URLs are not paths (a `file://` URL is). A named
  file that cannot be read is no longer reported as missing. KG4's block cites only vault
  files that are not the app's own. Tk's "forget" says when the list could not be read.
- Decided: a folder linked into the vault (junction/symlink) is searched — it is how a
  folder is added without copying; protection is decided on the resolved path, so a link to
  `memory/` is still the app's memory.

Still open after the second review:
- The full panel (Batch 3) and Tk's per-turn `respond` patches (specialists, instructions,
  content style) add text the sizing does not measure: a panel Writer's synthesis prompt
  carries the drafts, and the engine can still cut it, unreported.
- Prompts are larger than at the branch point for questions that only weakly match the data
  (the overview cap is 2,500 characters at 8,192; vault and value blocks scale with the
  window). Whether to keep the 4,096-sized caps for those blocks is a choice not made here.
- Tk's own `_preview_data_file` still reads on Tk's GUI thread (now bounded reads).
- RECENT CONVERSATION still holds the question being answered (Qt, and Tk with SILO off).

## 8. After merging council-map (branch `merge/council-map`, 2026-10-13)

origin/qt-migration (council-map: depth, structured replies, per-step thinking, native
tools, the Council Map, the Code tab, Quant Duel, the Variable Map …) merged with Batches
1-2. Its CLAUDE.md retires the Tk shell, so what Batches 1-2 did to Tk is undone:
`council_gui_engine.py` is its e0b7a3c code again (its own copy of the context code; it
does not call `prepare_turn`, claim its session id or append through `append_line`), and
the tests that ran the Tk shell or compared it with Qt are gone (`test_shell_parity.py`,
`test_tk_answers_from_data.py`, the Tk halves of the planted-fact, conversation, memory
scope and turn-context tests). Section 7's "both shells" now means the Qt app; the Tk shell
keeps its old behaviour.

How the Qt turn combines the two (`CouncilActions.send`, as fixed on
`merge/council-map-fixed`): council-map's lookups (vault passages, the Sage's taught
knowledge, past decisions, docs) are started side by side; while they run,
`turn_context.begin_turn` — the task memo (the Intern's model) and the analyst (the Coder's),
side by side too; an answer from the data is given then, without waiting for the lookups;
otherwise the Sage, past and docs blocks go on their roles and `prepare_turn(started=…)`
continues — sized to the Writer's call with those blocks in it and at the call's thinking
level (`prompt_room(think=…)`); then the vault passages, minus files already in front of
the question and cut to the room left (`vault_context.fit`). One task memo and one analyst
— the turn context's, with council-map's contract (below).

"This conversation only" holds for council-map: past decisions (the block and the
`recall_decision` tool) and the after-answer role/project memory updates only in scope
`all`; an earlier passed answer is shown, not kept, unless the scope is `all`.

**Review fixes (2026-10-13, branch `merge/council-map-fixed`).** Each was reproduced by a
test that fails on 05bef12 (the coverage-only ones are said).
- A semantic index (`<vault>/.chromadb`) built before `logs/`, `memory/` and the root's
  app files were protected — the Tk shell builds one at every start; the user's lists
  `memory/` and `logs/` files — put an older session, role memory and older judged
  questions into the Writer's prompt (default build) and 30 of 34 Deep-council calls
  through council-map's passages, and into the `semantic_search` tool. Both now drop a hit
  from a dot-folder or a protected file (`vault_context.is_app_source`), and a re-index
  removes the chunks and hashes of every file it no longer collects (also a file the user
  deleted). `tests/test_stale_semantic_index.py` uses a real chromadb index built under the
  old rules.
- With Tools on, `vault_list` / `vault_read` / `vault_search` read the app's root files
  (`question_history.json`, `verdict_history.jsonl`, the wishlist) into members' prompts;
  they now refuse what `tool_kit`'s reading tools refuse. `vault_save` refuses an app file
  name (a dot-file, `VAULT_ROOT_STATE`, anything protected): a model-written
  `node_routing.json` had become the next launch's opt-in to send the Writer's prompts to
  another machine. The name is checked, and the file saved, as Windows will name it
  (`council_tools.saved_name`): `node_routing.json.` (trailing dot) had still created
  `node_routing.json` (fixed on `merge/council-map-fixed2`).
- The analyst keeps council-map's contract: `COUNCIL_ANALYST=0` turns the whole step off
  (the direct answers from the data too); the code is asked of the Coder's model
  (num_predict 700, 120 s), one retry with the error; it runs in a child process with the
  time limit and memory cap (`analyst_step.run_code`) and is given no
  `save_app_tool`/`run_app_tool` (one question had written `App_Built_tools/`); the
  prompt no longer offers them.
- The memo is asked of the Intern's model (it was the main model's).
- A low-confidence gap's wishlist line names the typed question, on one line (it quoted
  the task memo in front of it).
- The Council Map no longer says role/project memory and the profile reach every role
  "live", nor the guide that each member gets them or that the Strategist recalls past
  decisions, in the default scope.
- `memory_scope.pin_launch_environment`: `council_qt.py` pins `COUNCIL_MEMORY_SCOPE` and
  `COUNCIL_DEMO_SILO` first, so importing the Tk module (which sets SILO) cannot turn Qt's
  memory off; a test builds every Qt tab in a child and finds neither Tk module imported
  (coverage: nothing imports it today).
- `test_qt_remembers`' full-council test runs at Deep with Tools, Parallel and Vault on and
  plants council-map's stores (past decisions, a passed answer, verdict_log, judged
  questions, the gap list, `logs/council.log`); with a control in scope `all` (coverage
  only: it passed before).

**Decided on the review** (design calls the user may change):
- The Sage's knowledge base (`sage_knowledge/`: facts and corrections taught in the Agents
  tab; a correction quotes an older question) reaches the Sage in `conversation` and `all`
  as what the user taught it on purpose, and not in `none`. A vault search never returns it.
- A Code-tab job's handoff note (`.council_projects/<p>/notes/`) is the project's, like its
  brief and code map — shown in the Code tab, what the next job is planned from — so it is
  in the Code agent's context in every scope.
- The analyst's result table is no longer saved to `vault/analyst_results/` (the next
  conversation's vault search found an older question's figures there); the transcript
  shows up to 500 rows. A final failure still adds a line to the app's protected
  `.failures.jsonl` (read by the shutdown's FailureAnalyzer, never by a prompt).

**What this merge overrides in section 7:**
- "Not done: … Expand with council (Batch 3)": Expand works (council-map's version: the
  fast answer is re-asked through the whole council at Deep depth).
- "Role/project memory is not written by Qt": it is, in scope `all` only
  (`after_turn.learn`); never in `conversation` or `none`.
- "Importing the Tk module no longer turns Qt's memory off … `set_tk_default`": that
  mechanism is gone with the Tk edits; the launch pin above replaces it.
- The Tk-only items left open in section 7 are won't-fix (the Tk shell is retired): Tk's
  first verdict before naming, Tk's Agree/Disagree stamping the last line, Tk's
  `council.log` `open("a")`, Tk's "New session" not claiming its id, Tk's
  `_preview_data_file` on its GUI thread.
- Still true: Qt writes no crash-recovery sentinel and offers no resume (USER_GUIDE's
  Sessions section now says so, with "This conversation only" and Load as Prior).

**Merged again (2026-10-15, branch `merge/council-map-fixed3`).** origin/qt-migration at
393671b (the Typhon follow-ups; Basler triggered capture and Downsample) merged without
conflicts; the tab list is origin's lazy (PEP 562) registry, which holds every tab both
sides list. Four council-map tests that failed on this PC (Windows, Python 3.11) and on
origin there:
- `test_judge_view::test_code_check`: pyflakes is not in the app's environment, so
  `tool_kit.lint` found nothing past the compile check and the Judge's CODE CHECK said "no
  syntax errors or undefined names" about code that reads an undefined name. Without
  pyflakes, `tool_kit.undefined_names` (the standard library's symtable, pyflakes' rule)
  finds them; unused imports still need pyflakes.
- `test_var_graph_tab::test_export_svg_names_the_variables`: a `QSvgGenerator` given a file
  name holds it open, so the swap onto the chosen name failed on Windows and the temp file
  stayed; the SVG is drawn into memory and written by the tab.
- `test_vault_context::test_without_a_semantic_index_no_model_is_ever_loaded`: the test
  compared `notes\pumps.md` with `notes/pumps.md`; it compares the posix form now (the code
  shows the OS's separator, as everywhere else).
- `test_code_bench.py` and `test_council_core`'s no-toolkit check of `code_bench.py` pass
  with the merge's f-string fix (a backslash inside an f-string is Python 3.12+).

Fenced (2026-10-15, branch `fix/fence-run-python`, the user's decision): `run_python`
(Tools on: the Coder and the Intern) ran a model's code in a plain child process with the
app's environment, and a probe's `open('../node_routing.json', 'w')` made
`node_routing.route('writer')` a remote host on the next load. It now runs behind the
analyst's fence, the one `data_query` and the analyst use (`analyst_step.run_script`:
`vault_analyst`'s sandbox in a child process, at most 120 s, which a model may lower but
not raise, a 2048 MB commit cap and the whole tree killed). Printing the answer counts as
an answer. Nothing is written for it (no `<vault>/workspace/scratch.py`) and its code
writes no file of its own, only through the sandbox's helpers into data_out; it reads only data_in;
it imports only pandas, numpy, scipy, math, re, json, statistics, collections and
pathlib; it cannot define classes or start processes, and has no network apart from the
read-only SQL/Mongo helpers on the user's own connections. `run_tests` no longer runs
vault files (a model's `vault_save` followed by `run_tests` ran its code with full
permissions). It runs only inside code roots a caller names, and the Council tab names
none. Tests: `tests/test_run_python_fence.py`.

Open:
- The Vault switch turns off council-map's passages only; the matches `prepare_turn` puts
  in front of the question do not read it.
- A Tk launch and a Qt launch in the same second can share a session id again (the Tk
  console no longer claims its id).
- Native tool specs (Tools on) and the members' draft wrappers are not in the Writer's
  measured frame; the engine's clamp still fits those calls.
- `analyst_results/` files the Tk shell wrote are not protected: a vault search can still
  find them.
