# Phase 5 — the Vault pilot, and the re-forecast it exists to produce

`docs/qt_full_port_scope.md` §3 sets one exit criterion for this phase, and it is not
"a working tab":

> **Phase 5 is the decision point, and it is deliberately early.** Its exit criterion is
> a measured rate and a re-forecast of the remaining ~3,000 lines.

This is that re-forecast. Everything numeric here comes from
`docs/qt_migration/probes/pilot_calibration.py`, which you can re-run.

---

## 1. What was built

A complete Qt Vault tab, and the logic behind it moved out of the Tk shell into a layer
both front ends import.

| | |
|---|---|
| `council_qt/tabs/vault.py` | the view and its actions — 924 lines |
| `council_core/vault_ops.py` | keyword index, descriptions, embeddings, clone, pull, stats — 328 |
| `council_core/vault_import.py` | zip / folder / zip-of-folders import — 276 |
| `council_core/vault_data.py` | deferred tasks, collections, delete, RAG misses, Mongo — 239 |
| `council_core/vault_search.py` | instant search, by name and by indexed content — 158 |

All 29 Vault commands the Tk tab wires have a Qt handler. Three of them
(`on_run_deferred`, `on_new_collection`, `on_summarize_collection`) report in the log
that they cannot finish yet, because they drive the Council tab's model plumbing, which
is phase 6. They are counted as unfinished, not as coverage.

---

## 2. The measured numbers

```
Tk toolkit lines replaced (the estimate's own probe)  :  485
Qt lines written to replace them                      :  924
Multiplier                                            : 1.91
Logic lifted into council_core on the way             : 1001
Tests, offscreen                                      :  190 passing
```

**The multiplier is the number that matters.** The scope document is denominated in Tk
toolkit lines, so 1.91 is what converts the remaining surface into work.

### What these numbers are not

They count source, not calendar. A rate in days needs a human with a clock, and this
pilot was not done by one — so **no days-per-line figure can honestly be extracted from
it**, and the §6 estimate's ~288 days is neither confirmed nor refuted here. What is
measured is the shape of the work: how much Qt gets written per Tk line retired, how
much logic comes out with it, and what breaks on the way.

---

## 3. The finding that changes the plan

**Extraction does not shrink the Tk toolkit surface.** Before the pilot the Vault tab
measured 491 toolkit lines; after moving 1,001 lines of logic out of it, it measures
485. Six lines.

That is not a failure — it is what extraction *is*. It moves logic, and logic was never
the toolkit-bound part. But it means phase 3 must be read correctly in the plan: it is
**additive cost that buys a smaller blast radius**, not a discount on phases 5–10. Its
payoff is real and is visible in this pilot — six defects below were found or prevented
by it, and no fix now has to be made twice — but the translation work that remains after
it is the same size it was before.

The corollary: **the 1.91 multiplier already includes that.** It is Qt lines per Tk
toolkit line, with the extraction counted separately. Don't apply it twice.

### The extraction ratio will not hold

1,001 logic lines came out of 485 toolkit lines — a ratio of 2.06. The Vault is the most
logic-heavy tab in the app; it is the tab where files move, indexes build and things get
deleted. Applying 2.06 to the Changelog tab would be nonsense. Extraction should be
forecast per area from what each area actually contains, not from this ratio.

---

## 4. Defects, which is the other thing a pilot is for

Six real defects surfaced. They divide into three kinds, and the split is more useful
than the count.

**Inherited — in the Tk app today, found by moving the code:**

1. All three import workers wrote Tk variables from a worker thread
   (`self._vmgr_zip_var.set("")`). Undefined behaviour in Tk; it happens to survive on
   Windows. Now routed through the ui queue, and a test strips comments before checking
   so the fix cannot be un-done by a rename.
2. `delete_path` had no boundary check. The Tk version deletes whatever path the tree
   hands it, which is safe only because the tree is built from the vault — an invariant
   nothing checked. Now it refuses anything outside the vault, and refuses the vault
   itself.

**Inherited and deliberately kept:**

3. `repo_subfolder` uses `rstrip(".git")`, which strips *characters*, not a suffix:
   `https://host/` becomes `hos`. This name decides where a clone lands on disk, so
   changing it would move existing users' repositories. Preserved, and pinned by a test
   that states why.

**Qt translation defects — introduced by the port, found in the port:**

4. A `closeEvent` calling `event.ignore()` vetoed `quit()` and deadlocked the
   application on exit. Found by bisection; fixed by separating `_shutdown()` from
   `request_close()`; pinned by a subprocess test that fails if the app ever fails to
   exit.
5. Qt reads `&` in a caption as a mnemonic, Tk does not — "Index & Vectorize" rendered
   as "Index _Vectorize".
6. A root-level stylesheet cascaded into every child widget and turned the whole app
   pink. Fixed by scoping to `QWidget#_MainUi`.

Two more were self-inflicted during the extraction itself and are worth recording
because they are the failure mode of this whole approach: `starting_descriptions`
reported `total=len(records)` instead of the pending count (both front ends would have
started a worker with no work), and the first cut of the Qt search answered a *smaller
question* than the Tk one — filenames only, no indexed content. Same box, same button,
quietly different answers. The second was caught by the coverage probe, not by a test,
which is why the probe now exists.

**Read this against phase 12.** The plan budgets 20% of translation effort for
post-cutover defect burn-down. One tab produced three Qt-specific defects, two of which
(the deadlock, the stylesheet) were severe and neither of which any test would have
caught without being written for it. 20% does not look pessimistic.

---

## 5. The re-forecast

Remaining toolkit surface, measured now (`port_surface.py`):

| where | toolkit lines |
|---|---:|
| `council_gui_engine.py`, all clusters | 2,684 |
| live modules (canvas, splash, onboarding, panels, wizards…) | 1,216 |
| modules not currently loaded but in scope¹ | 548 |
| **total** | **4,448** |
| less the Vault, done | −485 |
| **remaining** | **3,963** |

¹ Per your correction: those six modules are other branches' features, not dead code.

At the measured 1.91, the remaining translation is **≈7,570 lines of Qt to write**.

Converting that to time is the part this pilot cannot do for you, so here is the
conversion with the rate left open. Pick the row that matches your own experience of how
fast you work on this codebase:

| if you translate… | the remaining 3,963 toolkit lines take |
|---|---:|
| 20 lines/day | ~198 days |
| 30 lines/day | ~132 days |
| 40 lines/day | ~99 days |
| 50 lines/day | ~79 days |

**This table is translation only.** It excludes extraction, the test harness (phase 4),
packaging and cutover (phase 11), and the defect burn-down (phase 12) — together roughly
half the §6 estimate. Multiply, don't substitute.

The scope document's stop-here threshold was "below ~30 toolkit lines/day". That
threshold is still the right shape of question, and it is now yours to answer against
the row you picked.

---

## 6. Recommendation

**Continue, but decide phase 6 on its own merits rather than committing to all of 6–12.**

The reasons to continue are specific:

- The multiplier came in at 1.91, not the 3–4× that hand-written Qt views can cost. The
  Vault was chosen as the pilot because it is the superset of mechanisms — tree,
  paned layout, forms, three dialogs, worker traffic — so 1.91 is a *hard* case, and
  easier tabs should beat it.
- Coverage reached 29/29 without inventing anything: the view is written against the
  extracted functions, and the three unfinished handlers are unfinished for one reason
  that phase 6 removes.
- The extraction is already paying for itself in the Tk app — two real defects fixed, a
  deletion boundary added, one search instead of two.

The reason to keep the decision open is equally specific: **phase 6 carries the
transcript widget**, which is the hardest thing in the app and the one place the 1.91
multiplier is least likely to hold. Re-measure after it. If the transcript alone blows
the multiplier past ~3, that is the signal to stop and take one of the cheaper
alternatives in §9 of the scope document rather than to push on.

---

## 7. Still not verified

- **The full 1,048-test suite has not been run since the engine edits.** It opens
  windows, and testing the generated GUIs is deliberately deferred until the port is
  done. 190 tests pass offscreen; that is the extracted logic and the Qt shell, not the
  Tk app's own suite.
- **No Qt tab has been looked at by a human.** Every check here is offscreen and
  programmatic. Layout, spacing, focus order, tab order and DPI behaviour are unverified
  by construction.
- **`tests/smoke_test.py` still has 169 test functions with no real assertions**
  (phase 1, not yet done). Until that is fixed, a green run of the full suite means less
  than it appears to.

---

*Reproduce: `python docs/qt_migration/probes/pilot_calibration.py`*
