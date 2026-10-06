# Handoff: the knowledge graph, built on the desktop

Written 2026-10-05 on the laptop, for the Claude Code session that continues this
work on the **desktop**, which has the larger vault. The laptop's memory notes do not
travel between machines, so everything that session needs is in this file and the two
documents it points to. Read all three before writing code:

1. **This file** — the user's decisions, the rules, the first steps.
2. **`docs/morphik_assessment.md`** — the researched plan: what exists in the Council,
   the gaps (with file:line), the staged plan, the measured limits of local models.
3. **`docs/specialized_nodes.md`** — background only (Pi / GPU nodes); not this branch's
   work.

Branch: **`knowledge-graph`** (made from `qt-migration` at 9484a25). Push only this
branch. The laptop keeps working on `qt-migration` (Typhon, the camera app) and will
merge this branch in after review.

---

## 1. What the user asked for (verbatim)

- "So for Morphik a decent amount of it was from open source stuff could we import
  features inside of it like the agent creator and the knowledge graph into the
  council?"
- What the graph is for: **"Connecting people, parts and projects across the vault
  documents from the knowledge graphs."**
- "Please note that at some point the council may transition into something that can be
  networked into" — and then: **"We should hold off on any changes now to make it server
  based rather than locally based it is just a possible feature path in the future"**.
- "Can you create a fork here for work to be done on my desktop? For instance on my
  desktop it can work on the knowledge graph with its larger vault"

## 2. Decisions already made

- **Council-native, not Morphik code.** Morphik's open repo deleted its graph
  (2026-01-20) and agent (2025-11-26); its current code is BSL 1.1 (not open source; the
  Council is a $99 product per `licensing.py`); running Morphik needs Docker, Postgres,
  Redis, telemetry and model downloads. **Do not copy Morphik code.** Ideas are fine.
- **Entities:** PERSON, PART, PROJECT, plus the DOCUMENT each fact came from.
  Relationships among them (e.g. works on, leads, uses part, supersedes, part of), each
  with **provenance**: file, page / sheet+row / line / field, and the quoted text.
- **Local-only now.** One embedded SQLite file in a dot-folder:
  `<vault>/.knowledge_graph/graph.sqlite` (dot-folders are skipped by vault_index).
  No server, no service/API layer, no permissions or multi-user system. Zero-cost
  choices that keep a future networked path open are welcome: stable ids (UUID or a
  hash of type + normalised key — never names or paths), full provenance on every fact
  (document id + content hash, locator, method, model, run id), the user's
  merge/split/reject decisions as an append-only log, a versioned documented schema
  with JSON/CSV export.
- **Seed first without a model**: labelled fields (via `field_search.py`), the existing
  **Collections** (`vault_collections.py` — already a user-confirmed project→document
  layer), and a gazetteer pass (plain search for every known name / part code).
- **Model extraction only for free text**, and every model-proposed link is
  **"suggested" until the user accepts it**. Measured on the laptop (one made-up memo,
  9 entities, 6 true links): fully-correct relationships llama3.1:8b 2/6, gpt-oss:20b
  3/6, phi3.5 2/6; **no model found a part→project link**; a verbatim-quote check found
  the source sentence but filtered none of the wrong relations. The Council, not the
  model, computes page and line.
- **Agent profiles** (later, separate): Specialists gain a tool subset (read-only tools
  only — exclude `write_tool` and `run_app_tool`), a step budget and a model role, run
  through `safe_agent` / `agent_jobs_runner`. Never offer write or delete tools.

## 3. The plan (details and file:line in `docs/morphik_assessment.md` §3-5)

**Prerequisites (~15-25 h, estimate)**
1. `field_search._split_values` splits on `, ; / and &` — "Lee, Carol" becomes two
   people, "Bearings & Seals Program" two projects, "PN-1234/A" two parts. Fix with a
   heuristic + tests (blast radius: the tally, search and per-file CSV export that call
   it).
2. Row/line **locators** in field-search results (today only `{value, file, path}`),
   and **page-aware PDF text** (`vault_rag.py` joins pages and caps at 50).
3. Move the label-vocabulary harvester `_known_field_names` out of the Tk engine
   (`council_gui_engine.py` ~6911) into `council_core`, and wire the drift matcher
   (`field_search.field_name_candidates`) into live search — **"suggest, don't
   substitute"**: candidates (pre-check ≥ 0.85) for the user to confirm, never silently
   (real symptom: a Point-of-Contact tally matched 213 of 519 files; ~300 used drifted
   labels such as `Router_Point_of_Contact`, `Point of Contact (Primary)`, `POC`).
4. An **"extractor"** role in `council_core/model_slots.COUNCIL_ROLES`.
5. Register the graph's files in BOTH exclusion lists (`PROTECTED_STATE_FILES` in
   `conversation_logger.py` and `data_index._APP_INTERNAL_FILENAMES`), or keep
   everything inside the dot-folder.

**Stages**
- **KG0 benchmark (3-5 h):** the user labels 20-50 snippets from THEIR documents; a
  table of models × accuracy × time on their text picks the extractor model.
- **KG1 store + seeding (12-18 h):** the SQLite store; seeding from confirmed labelled
  fields, Collections and the gazetteer; a **Connections** tab (Qt, `council_qt/tabs/`):
  search a person/part/project, see linked items and the "why" (file, row/line,
  snippet), open the file there, a coverage line.
- **KG2 free-text extraction (12-18 h):** a resumable, pausable background job per
  document; only chunks with signals go to the model; `num_predict` ≈ 1,400 (the
  default 600 cuts a measured 872-token reply); results go to a "suggested links" list.
- **KG3 merge review (8-12 h):** "Is D. Whitfield Dana or Dan?" with context; undoable
  decisions kept across rebuilds.
- **KG4 use in answers (8-12 h + wiring):** a cited neighbourhood block for questions
  naming a known entity. `run_turn(extra_ctx=...)` alone is not enough — only
  `tool_payloads` reaches a prompt (the synthesiser, cut to 900 chars); needs a
  deliberation change and the Qt turn wiring (`council_qt/tabs/council.py` ~181).

Cost on the laptop (estimate): ~0.8-2.2 h per 100 documents with llama3.1:8b if every
chunk went to the model; the desktop's GPU will differ — measure in KG0.

## 4. First steps on the desktop

1. Get the branch:
   ```
   git fetch origin
   git checkout knowledge-graph
   ```
2. Python: the launcher (`run-windows.bat`) finds the env via `.council_python`,
   `COUNCIL_PYTHON`, `.venv`, or the conda env `wizardCouncil`. On the laptop the dev env
   is a conda env with **Python 3.11** (`council`); code must work on 3.11 and 3.12.
3. Models: Ollama on 127.0.0.1:11434 with US-origin models (llama3.1:8b; gpt-oss:20b if
   the desktop GPU holds it — the laptop's 8 GB card could not hold it fully). The
   Models tab's **"Check this PC"** measures this machine (it over-predicts MoE models
   such as gpt-oss by ~40%). **The Council never downloads models — the user runs
   `ollama pull` themselves.**
4. Find the real vault: normally `%USERPROFILE%\.council\vault` (on the laptop
   `~/council_vault` is a decoy the Qt port made — don't delete it without asking).
   Count its documents by type, and **ask the user** the open questions in §6 before KG1.
5. Run the existing tests that cover what you will touch, for a baseline. There are no
   dedicated field-search or data-index test files: `field_search`, `data_index` and
   `vault_collections` are tested inside **`tests/smoke_test.py`** (run it offscreen;
   `-k field` narrows it), plus `tests/test_council_core.py` (collections, vault index),
   `tests/test_council_turn.py` and `tests/test_vault_health.py`. Add a dedicated
   `tests/test_knowledge_graph.py` (and `tests/test_field_search.py` for the split /
   locator fixes) as you go.

## 5. Rules (the user's standing instructions)

- **Offline by design:** localhost only; nothing leaves the PC unless the user opts in;
  no telemetry; no cloud LLM calls; no new network listeners.
- **Never delete or overwrite user data.** Never modify existing vault files; the graph
  only adds its own dot-folder. Develop and test against **temporary vaults** (set
  `COUNCIL_VAULT_ROOT` to a temp folder) or a COPY; run on the real vault only when the
  user says so.
- **No pickle** anywhere (`data_index_cache.py` already uses pickle — don't copy that).
  Atomic writes; a damaged file is reported, never silently replaced.
- **Only US-origin models may be recommended** (Llama, Phi, Gemma, Granite, OLMo,
  gpt-oss). Qwen / DeepSeek only for comparison, never recommended.
- **Verify offscreen:** `QT_QPA_PLATFORM=offscreen` and `COUNCIL_NO_DIALOGS=1`; don't
  pop windows on the user's screen unless asked.
- **Git:** push only `knowledge-graph`. **Never push to `main` or `Work-Build`**, never
  push to `qt-migration` from the desktop. Merge `origin/qt-migration` into this branch
  regularly (`git fetch origin` then `git merge origin/qt-migration`) to stay current.
  Commit work in progress often.
- **Stay out of the laptop's area** to avoid conflicts: Typhon and cameras
  (`frame_camera.py`, `examples/gui/typhon.gspec`, `council_core/cameras.py`,
  `camera_*`, `capture*`, `frame_classes.py`, `frame_roi.py`, `council_qt/widgets/capture_review.py`,
  `camera_settings_window.py`, `preset_picker.py`) and the local-model engine
  (`council_engine.py` routing / Ollama code, `gui_describe.py`, `council_core/docs_qa.py`).
  If the graph needs an engine change, keep it small and say so in the commit.
- Tests: focused tests that **fail before and pass after**; report exact counts;
  report failures faithfully.
- Match the code base's style: docstrings that say WHY, measured numbers, toolkit-free
  logic in `council_core`, Qt only in `council_qt`.

## 6. Questions to ask the user before KG1

1. Which folders and document types should the graph cover first (spreadsheets /
   trackers with labelled columns are cheap; narrative PDF/Word reports need the model)?
2. Which labels mean person, part or project in their documents (POC, Owner, P/N,
   Program…)? What do part numbers look like (revision letters, prefixes)?
3. How far back should the first run go?
4. Should a model-proposed link ever be accepted automatically, or always reviewed?
5. Which "agent creator" did they mean (Morphik Cloud, Morphik's research agent, its
   removed Workflows builder, or MorphMind — a different product)?

## 7. One security item on the desktop's network

The user ran the Council's **Apothecary Pi setup wizard** from the desktop. That wizard
(`apothecary_engine.py`) binds the Pi's Ollama to `0.0.0.0:11434` (open to the whole
LAN, no login), stores the Pi's SSH password in plain text in the desktop vault's
`node_registry.json`, accepts any SSH host key, and a 60 s monitor SSHes in and
sudo-restarts Ollama. The user was given the fix on 2026-10-05 (bind the Pi's Ollama to
127.0.0.1 via `/etc/systemd/system/ollama.service.d/override.conf`, remove the iptables /
ufw rule, change the Pi password, clear the stored password, reach the Pi through an SSH
tunnel `ssh -N -L 11435:127.0.0.1:11434 <user>@<pi-ip>` if needed). If it has not been
done, remind the user — do not change the Pi or the wizard on this branch.

---

## 8. Progress on the desktop (2026-10-05)

**What the desktop found.** `%USERPROFILE%\.council\vault` holds 7,827 files (370 MB),
but they are logs, GUI projects, git clones (axolotl, nanoGPT, LLMs-from-scratch) and
ideas — no real people/parts/projects documents; the only office files are the
synthetic `mfg_eval/` and `analyst_eval/` sets. Hardware: RTX 5080 16 GB; Ollama has
llama3.1:8b, llama3.2, phi4:14b (no gpt-oss, Granite, OLMo or Gemma yet).
**The council conda env has no openpyxl, pypdf or python-docx** although
requirements.txt lists them; system Python 3.12 has openpyxl and pypdf.

**The user's answers (2026-10-05).**
- "You can create synthetic documents here to build the knowledge map creation
  process around." -> `council_core/kg_corpus.py` (below).
- The agent creator: "it was really just selecting a model and giving connected roles
  and tools … in the agent creator I would like to be able to say desired tools and the
  council as a whole creates the tool and adds it to that agent."
- Questions 2-4 of section 6 (labels, document types, how far back) are still open; the
  synthetic corpus stands in until real documents exist.

**Built (branch `knowledge-graph`).**
- `field_search`: values are no longer broken apart ('Lee, Carol', 'PN-1234/A',
  'Bearings & Seals Program' stay whole); `field_value_locations()` gives sheet/row/
  column, line, and PDF page+line; `vault_rag.extract_pdf_pages()` (no 50-page cap);
  `.docx` text without python-docx. `tests/test_field_search.py`.
- `council_core/kg_corpus.py`: the synthetic "Ironbridge" vault (xlsx with two sheets,
  csv, json, md, 3-page pdf, docx, Collections) and an answer key: 8 people, 7 parts,
  4 projects, 33 distinct labelled links + 4 free-text-only links, a 'D. Whitfield' who could be
  Dana or Dan, a drifted 'POC' label. Dependency-free writers.
  `python -m council_core.kg_corpus <empty-folder>` makes a demo vault.
- `council_core/knowledge_graph.py` (KG1): the store, no-model seeding, field rules
  that stay 'proposed' until confirmed, drifted-label suggestions, a review queue,
  decisions kept across rebuilds, JSON/CSV export in the dot-folder, a damaged or newer
  store reported and left alone, files the install cannot read listed as unreadable.
  Scored on the corpus: **all labelled links found, none wrong; 0 of 4 free-text links
  (those are KG2's job); 0.06 s for 9 documents.**
- `council_qt/tabs/connections.py`: the **Connections** tab (registered after Vault):
  search, links grouped by meaning with their evidence, an in-app preview of the cited
  spot, Open file, Accept/Reject, Fields…, Questions (read-only until KG3).
- `.knowledge_graph` added to `conversation_logger.PROTECTED_SUBDIRS`.

**Next.** KG0/KG2 (benchmark + model extraction on the corpus's free text), KG3 (answer
the review questions), then the agent creator as the user described it.

### 8b. Later the same session (2026-10-05/06)

**User decisions.** Install the document libraries (done: openpyxl 3.1.5, pypdf 6.19.0,
python-docx 1.2.0 in the council env). Fix the sandbox before the agent creator.
Council-made tools: a SETTING — wait for approval (default) or attach automatically.
Models may be pulled for testing (pulled granite3.3:8b, gemma3:12b, olmo2:13b,
gpt-oss:20b). Raspberry Pi set-up goes on THIS branch ("more Raspberry Pis would assist
in knowledge graph generation"), including erasing a new Pi's card and installing the OS;
OS image: download on request or a user file; admin rights: a UAC prompt per write.

**Built.**
- Sandbox (vault_analyst): 20 reproduced escapes closed — reading any path
  (pd.read_csv('C:/x'), Path.read_text, helpers), pickle (pd.read_pickle, np.load),
  writes (np.save, to_json(path), split_csv_by_column), pd.io / scipy.io / numpy.lib.
  tests/test_sandbox_escapes.py (32). Still no exec time limit.
- Agent creator: council_core/agent_profiles.py + the Agent Creator tab. Council-built
  tools (coder drafts, connected roles review as JSON, one revision), pinned by sha256,
  approve-or-automatic setting (automatic only when every reviewer approves and the
  sandbox test passes). tests: test_agent_profiles.py (16), test_agent_creator_tab.py (9).
- Pi set-up: council_core/pi_setup/ (pi_secrets, disks, images, writer, flash_helper,
  firstboot, remote, pi_models, setup) + council_qt/tabs/pi_setup_dialog.py, opened
  from the Apothecary tab ("Set up a Pi", "Switch to key login"). Erasable disks only
  (this desktop's 5 TB USB Seagate is refused), typed confirm code, the elevated helper
  re-checks the disk; image checked against the published .sha256 and the list's
  extract_sha256, card read back; first-boot files written in the image's own format
  (the real Trixie image ships commented-out cloud-init files — the likely reason the
  user's Imager set-ups lost Wi-Fi/SSH) and verified; the Pi's SSH host key is made by
  the Council and put on the card, so the Pi is found by key; password used once,
  nodes registered with key auth and NO password; Ollama firewalled to this PC.
  tests: test_pi_*.py (93).

**Not yet verified on hardware.** No SD card was in this PC, so the real erase/write,
the 64-hex Wi-Fi key in netplan, and cloud-init applying ssh_keys are proven only on
files and a loopback SSH server. First real card: run Set up a Pi with the user watching.

**Network finding.** The registered Pi "NodePrimus" (192.168.1.252) still answers Ollama
to the whole LAN (only qwen2.5:3b installed) — the §7 lock-down was not done. The new
"Switch to key login" action fixes it from the Council.
