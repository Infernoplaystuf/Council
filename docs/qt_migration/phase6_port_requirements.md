# Phase 6 — what the port must not inherit

Phase 6's reconnaissance read the Council, Grapher and Dream3D tabs, the transcript, and
the host plumbing. Six mapping agents produced findings; six adversarial auditors then
re-read the source and **independently confirmed 51 defects** and rejected five.

**Tkinter is being deprecated once the port lands, so none of these are being fixed in
the Tk app.** That decision changes what this list is for. It is not a bug backlog. It
is a set of requirements on the Qt implementation, because a port written by reading the
Tk source reproduces the Tk source's mistakes by default — faithfully, and invisibly.

Each entry says what is wrong, where, and **what the Qt side must do instead**. Where a
defect is already handled, the commit is named.

Everything here carries a `file:line` that an auditor verified by reading. Five claims
were rejected on that pass and are listed at the end, because a finding nobody
re-checked is a rumour.

---

## A. Defects that would silently survive the port

These are logic, not widgets. Translating the Tk code faithfully carries every one of
them into Qt.

### A1. Thirteen Tk variables read off the GUI thread — **the count was wrong, twice**

`council_gui_engine.py:20401` starts `worker()`; it reads Tk variables at `:19833`,
`:19834`, `:19863`–`:19866`, `:20143` (five `StringVar`s in one loop) and `:20284`.
Thirteen reads, not the eight the first map reported. Only `:19753` and `:19808` are on
the UI thread, and `:19808` is the one written correctly.

This works today only because Tkinter marshals `Variable.get()` back to the interpreter
thread internally. **Qt does not. The direct translation is a data race on every send.**

> **Requirement.** Snapshot every option into a frozen object on the GUI thread *before*
> the worker starts, and hand the worker the snapshot.
> `council_core/council_options.py` exists for this: `CouncilOptions.defaults()` builds
> it, `effective(demo_mode)` applies the run-time rules, and the worker never touches a
> widget. A test must assert that no Qt worker body reads a widget.

### A2. `_send` has no re-entrancy guard, and is reachable eight ways

Grepped for `_busy`, `_in_flight`, `_deliberating`, `_worker_thread`, `is_alive()` —
zero hits. Entry points: `<Control-Return>` (`:10162`), the Send button (`:10172`),
`_verdict_disagree_submit` (`:18210`), `_council_expand_with_council` (`:18615`),
question history, examples, Speech (`:21856`) and Dream3D (`:10879`). Nothing serialises
them, and two concurrent runs patch and restore `model.respond` on top of each other.

> **Requirement.** One in-flight turn. The button disables while a turn runs, a second
> send is refused with a visible reason rather than ignored, and the monkey-patching of
> `model.respond` is replaced by passing the callback explicitly.

### A3. The fast path shows a verdict bar for a turn that has no verdict — and agreeing corrupts an earlier record

Direct mode (`:19852`–`:19861`) posts `agent_phase`, `live_event`, `judge_final` and
`done`, and never posts a `verdict_record`. The pump's `done` handler (`:20685`–`:20694`)
calls `_vfb_show()` unconditionally. `_verdict_agree` (`:18142`–`:18146`) then stamps
`user_agreed=True` on **the last line of `verdict_history.jsonl`, which belongs to an
unrelated earlier deliberation**. `_verdict_disagree_submit` does the same at `:18186`.

This is the most serious finding in the set: it writes false data into a durable record,
and the user has no way to see that it happened.

> **Requirement.** The verdict bar is shown only for a turn that produced a verdict, and
> agree/disagree address a verdict **by id**, never "the last line". A test must write
> two deliberations, run a direct-mode turn between them, and assert nothing was stamped.

### A4. `_expand_btn` and `_last_fast_question` are never reset

Set at `:19687`/`:19689`, cleared only inside `_council_expand_with_council`
(`:18608`/`:18613`). The per-turn reset block at `:19267`–`:19273` clears four other
fields and misses these two. After one fast answer and any later deliberation, the
button is still live and still holds the stale question — so it re-asks the wrong thing.

> **Requirement.** Per-turn state is reset in one place, and a test asserts the reset
> covers every field the turn sets. A list that has to be maintained by hand will drift
> again.

### A5. The lead-role scan, LaTeX detection and script-keyword detection all run over injected file content

`user_text = augmented` at `:19568`. After that, `:19784`–`:19795` (lead role),
`:20185` (LaTeX) and `:20239` (script keywords) scan the augmented text — which contains
injected CSV, JSON and folder contents. A spreadsheet with a column called "artist"
reassigns the lead role.

`_send`'s own comment at `:19353`–`:19357` warns about exactly this and keeps
`original_user_text` (`:19366`) for it. It is used correctly at nine sites and missed at
these three.

> **Requirement.** Intent detection reads what the user typed. Injected content is
> evidence, never instruction. The extracted functions take the original text as a
> separate parameter so a caller cannot pass the wrong one by accident.

### A6. Blocking model calls on the GUI thread — **worse than first reported**

- `_condense_call` (`:19375`–`:19383`) calls `ce.local_chat(..., timeout=45)` via
  `task_memory.update` at `:19384`. **Unconditional, every send.** Up to 45 seconds of
  frozen window per message.
- `_do_memory_update`, dispatched straight from the pump at `:20507`–`:20512`, builds a
  role list of 6 + up to 9 optional roles (`:21279`–`:21292`) and calls
  `update_role_memory_after_pass` for each — and `council_engine.py:5175` is
  `role_model.respond(prompt)`. `MEMORY_WRITE_ROLES` filters out writer and judge and
  passes twelve others, so a fully-populated install makes **~12 generations plus
  `update_project_memory_after_pass` plus a `timeout=45` quirks call, all on the UI
  thread.**
- `_council_run_lookup` calls `data_index.refresh()` synchronously from a button handler
  (`:18637`).

One correction the audit made to its own map: the judge-panel pick at `:19757` is real
but **conditional** — it sits behind `if self.var_judge_panel.get():`, which defaults
False and is hidden in DEMO_MODE. Presenting it as a matched pair with the condense call
overstated it.

> **Requirement.** No model call and no index refresh on the GUI thread, enforced by a
> test over the Qt sources, not by discipline.

### A7. `max_rounds` escalation is dead, and corrupts the round counter

`for r in range(self.max_rounds)` at `:4027` materialises the range once, so
`self.max_rounds = 3` at `:4397` cannot lengthen it — while the phase message at
`:4394`–`:4396` tells the user an extra round is being added. The mutation also corrupts
`Round {r+1}/{self.max_rounds}` at `:4035` and the last-round test at `:4383`, so a
two-round run prints "Round 2/3".

> **Requirement.** Rounds are driven by a `while` over a mutable budget, or escalation is
> removed and the message with it. Telling the user something happened that did not is
> worse than not offering it.

### A8. `required_changes` is skipped exactly when confidence is lowest

The `else` at `:4399` belongs to `if _conf <= 2 and r == 0 and self.max_rounds < 3:`
(`:4393`), so `parse_required_changes` (`:4401`) is skipped in the low-confidence branch.
Scope correction from the audit: only reachable from `_bg_queue_run` (`max_rounds=2`);
from `_send` and `_gd_review` (`max_rounds=1`) `r == 0` is the last round, so the brief
would not be consumed anyway.

### A9. Two implementations of the same operation inside one front end

`council_core/vault_ops.py` has `build_descriptions`, `starting_descriptions`,
`build_embeddings`, `starting_embeddings`. `_build_embeddings_response` (`:9795`),
`_build_semantic_index_response` (`:9863`) and `_build_topics_only_response` (`:9924`)
re-implement them inline, while `_vmgr_build_descriptions` and `_vmgr_build_embeddings`
call the shared versions.

> **Requirement.** The Council tab's three response builders call `vault_ops`. This is a
> live fork *already*, inside one front end, and copying it into Qt doubles it again.

---

## B. Grapher — features that do not work at all today

The Grapher is the worst-affected area, and four of these are invisible because the
failure is silent.

| # | what | where | what Qt must do |
|---|---|---|---|
| B1 | `self.root` does not exist on a `tk.Tk` subclass — **Save Preset and Live reload are dead**; Load works, so the feature looks half-present | `:14441`, `:14523`, `:14534` | both must actually work, and a test must exercise save→load round-trip |
| B2 | Overlay dropdown can never resolve a file: labels built `data_in`-relative (`:13678`), resolved vault-relative (`:14564`), and the loop falls through **with no message** | `:13872`, `:14562`–`:14576` | one path convention, and a failed resolve says so |
| B3 | Sheet dropdown is decorative — `_grapher_reload_sheet` (`:13904`) passes no kwargs, so `_load_excel` keeps `sheet_name=0` and `:13877` snaps the user's pick back with no error | `graph_data.py:326`, `:334` | the chosen sheet is loaded, or the control is removed |
| B4 | **A failed export is reported as a success.** `graph_engine.py:836`–`841` catches the exception, draws an error figure, and returns it; `_grapher_export` sees a truthy fig, saves it and prints `✓ Exported` | `graph_engine.py:836` | a failure returns None and says what failed — this is shared code, so it is fixable once for both |
| B5 | Live reload only fires on a shape change (`:14544`), so a fixed-row-count file rewritten in place never re-renders | `:14544` | watch mtime and content, not shape |
| B6 | Temp CSV leak: `NamedTemporaryFile(delete=False)` never removed, one per click | `:14701`–`:14705` | delete it, or write into a managed scratch dir |
| B7 | Transforms and overlay apply only on the Plotly path; inline and export use the untransformed frame | `:14016`–`:14028` vs `:14191`, `:14231` | one working dataset, built once, used by every renderer |
| B8 | `_grapher_view_nb` is never selected, so everything routed through Plotly lands in a hidden tab | `:13567`–`:13575` | whatever renders must be the thing shown |

---

## C. The transcript — mostly already handled

| # | what | status |
|---|---|---|
| C1 | `_role_tag`'s `tag in ROLE_COLORS` half is dead, leaving a case-sensitive match | **fixed** — `council_core/transcript.py`, case-insensitive |
| C2 | Dream3D mirror copies only the foreground and forces bold on every tag | **fixed** — both configured from `TAGS` |
| C3 | Errors painted by `tag_add` over "the last two lines" | **fixed** — `kind="error"` |
| C4 | Unconditional `see("end")` drags a reader who scrolled up | **fixed** — Qt scrolls only when already at the bottom |
| C5 | `stream_box` inserts with a role tag never configured on it, so the stream box has no role colours | **fixed by construction** — `StreamView` builds every format |
| C6 | **Speaker names diverge.** Over 285 call sites: Writer 179, **Council 55**, Librarian 34, You 3, User 3, Judge 3. `Council` — the second most common speaker in the app — and `You` are not `ROLE_COLORS` keys and render default grey, while the same human is blue as `User` | **fixed** — `Council` has its own colour, `You`/`Workflow`/`Agent` alias to real roles, and a test reads every `_append_transcript` call site and fails if any speaker resolves to grey. That test then found two more (`Agent`, `Workflow`) that the tally listed and nobody flagged |
| C7 | `_maybe_generate_summary` (`:21614`, started `:21653`) calls `_append_transcript` off-thread. An AST pass over all 61 `Thread(target=...)` sites found this is the **only** one | **open** — the Qt port must marshal it |
| C8 | Chip tags are never released: `tag_bind` 6, `tag_delete` 0. Up to 12 chips per answer on both transcripts, each pinning `self` and a path string for the life of the process | **open** — the Qt equivalent must own its lifetime |
| C9 | `hasattr(self, "_load_session_into_transcript")` guards a method that does not exist anywhere in the repo, so accepting crash recovery repopulates nothing | **open** — implement it or drop the offer |
| C10 | `kind="token"` and `"thought"` are never passed to `_append_transcript` — the branch is dead | informational |
| C11 | Nothing reads either transcript widget back | good news; it is what makes the port cheap |

---

## D. Dream3D / NX

| # | what | where |
|---|---|---|
| D1 | `# saved to: {out}` is printed unconditionally while the file is written only `if code:` — and the refusal branch has the same defect, naming a path that was never created | `:10740`–`:10744`, `:10750`–`:10751` |
| D2 | `_nx_catalog_cache` is read and written with no lock from three worker bodies, and the **disk write at `:10582` is unsynchronised and non-atomic** — two threads `write_text` the same `nx_catalog.json`, and a torn file degrades silently to a full re-scan | `:10554`–`:10585` |
| D3 | `hasattr(self, '_dream3d_refresh_pipelines')` is always true (it is a class attribute) and `_build_dream3d_tab` is called unconditionally, so the guard is dead and a bare `except Exception: pass` absorbs everything | `:6614`, `:6820` |
| D4 | `fd.askdirectory()` has no `parent=`, while the `askstring` beside it passes `parent=self` | `:10661` |

---

## E. Already fixed in the port

Eight bugs the recon found in **my** Qt code, not in Tk — four reproduced by an agent
running the foundation offscreen. All fixed; see the commit *"eight bugs the recon found
in the PORT, not in Tk"*: silent message loss with no dispatcher, no close-time work at
all, `setCentralWidget` evicting the tab widget, an uncancellable cross-thread `after`,
`after_cancel` touching a timer from the wrong thread, unlocked `_timers`, a theme
argument that was stored and never read, and a bare repr where Tk prints a stack.

---

## F. Rejected on the audit pass

Recorded because a finding nobody re-checked is a rumour, and because these are the ones
that would have wasted effort.

1. `plots_pane.py:244`–`259` stale thumbnail highlight — `_trim_history`'s only caller
   immediately calls `show()`, which reassigns and re-highlights. Not reachable.
2. `plots_pane.py:191`–`197` popout stealing pan/zoom — each `NavigationToolbar2Tk`
   holds the canvas it was built with, so the inline pane is unaffected.
3. The background queue "silently dropping half the panel" — all twelve
   `_PANEL_FOR_ROUTE` entries are covered by the agent set at `:21125`–`:21132`.
4. `res['code']` raising `KeyError` at `:10622` — `nx_transpile.transpile` has a single
   return that always carries `code`.
5. The live-reload toggle raising on OFF — the guard at `:14522` can never be true.

---

## Where this leaves phase 6

C6 is done — it lived entirely in `council_core/transcript.py`, so fixing it fixed the
port by construction. Writing the test for it was worth more than the fix: it reads
every `_append_transcript` call site and fails if any speaker resolves to the default
grey, and it immediately found two more nobody had flagged. "Council rendered grey for
55 entries" was not noticed by anyone reading the code; it was found by counting.

Everything in section A is phase 6 implementation work: the Council tab's Qt view has to
be written **against these requirements**, not against the Tk source. Section B is the
Grapher's, and B4 is worth doing early regardless — the failed-export-reported-as-success
lives in `graph_engine.py`, which both front ends share, so it is one fix for both.
