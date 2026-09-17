# Porting the whole app to Qt: a staged project

Scope for migrating **Data's Inferno itself** — the Council shell and every panel —
from Tkinter to PySide6. This is the sequel to [qt_migration_plan.md](qt_migration_plan.md),
which covered generated apps only; that part is built and shipped on this branch.

The structure follows the approach you proposed: **identify the phases, write each
phase into a new folder, leave the original intact and referable throughout.** That is
the right shape, and §2 explains the one amendment it needs to avoid becoming a fork.

Headline: **~190–240 engineering days at the likely rate, with a defensible range of
150–370.** For one developer at 70% utilisation that is **13–20 months elapsed.** §6 is
where that number comes from and how to argue with it.

---

## 1. The surface being rewritten (measured, not estimated)

An AST walk over the real code today. Every later number is a function of this table.

| | lines | toolkit-bound lines |
|---|---|---|
| `council_gui_engine.py` | 22,610 | **2,695** (11%) |
| 13 live modules (canvas, splash, onboarding, panels, wizards) | 9,502 | **1,216** |
| 6 modules unreferenced *on this branch* (see below) | 3,282 | **548** |
| **total to rewrite** | ~35,000 | **~4,459** |

> **Correction (2026-09-17).** An earlier version of this document called those six
> modules dead and recommended deleting them. That was wrong, and the mistake is worth
> recording because it came from looking at one branch. `agent_panel`, `system_panel`,
> `grapher_app`, `tab_grapher`, `council_modules` and `phase1_ai_model_council` are
> **cut-off sections of features that live on other branches**, and they are all in
> scope for the port.
>
> Verified across the remotes: `grapher_app` and `council_modules` exist on 8 branches;
> `council_modules` has three importers on `main` and on `odysseus-council` —
> `tab_grapher.py`, **`tab_ideas.py` and `tab_video.py`**, two tab modules that do not
> exist on Work-Build at all. `agent_panel` and `system_panel` are imported by
> `inferno_local` tests, all 176 of which pass today.
>
> So `council_modules` is not leftover code: it is the **standalone-tab architecture**
> (`StandaloneHost` + `PALETTE`) that other branches build features on. Porting it is
> what lets those branches' tabs be ported later — see §3, phase 2a.

### 1.1 The target is Work-Build **plus the other branches' tabs**

Measured by reading each branch out of git and applying the same metric. These are UI
modules that exist on some branch and not on Work-Build:

| module | lines | tk-lines | lives on |
|---|---|---|---|
| `tab_ideas.py` | 2,033 | 366 | main, odysseus-council |
| `tab_video.py` | 1,399 | 346 | main, odysseus-council, game-dev |
| `council_gui.py` | 334 | 78 | **every branch except Work-Build** |
| `database_grabber.py` | 373 | 66 | Database-grabber |
| `diff_view.py` | 440 | 60 | odysseus-council, game-dev |
| **added by other branches** | **4,579** | **916** | |

**Total port surface: 5,375 toolkit-bound lines** (4,459 on Work-Build + 916 elsewhere).

Two consequences for sequencing:

* `tab_ideas` and `tab_video` are written against `council_modules.StandaloneHost`, so
  **phase 2a (the Qt host) must land before them** — which is why it is early rather
  than filed under "later". It is already built.
* These modules cannot be ported from this branch: the code has to be merged first.
  Porting them against a branch that later diverges would be doing the work twice, so
  each one's port should follow its merge, not precede it.

Inside `council_gui_engine.py`, attributing every method to a tab by following both
calls **and** the handlers wired by `command=self.x` / `bind(..., self.x)`:

| | methods | lines | toolkit lines |
|---|---|---|---|
| owned by exactly one tab | 231 | 8,165 | **1,905** |
| shared by 2+ tabs | 128 | 5,699 | **87** |
| lifecycle remainder (startup, dispatcher, licence/crash checks) | 46 | 1,798 | **83** |

Three consequences, and they shape the whole plan:

1. **The shared layer is 5,699 lines carrying 87 toolkit lines.** There is no shared UI
   framework to port, because there isn't one — `_send` is 1,166 lines with 2 Tk lines.
   The toolkit couples to the app through *one queue and one dispatcher*, not a wide API.
2. **Tabs are genuinely separable**, which is what makes a phased folder viable.
3. **Work is concentrated**: Vault (491), Council (422) and Grapher (270) are 62% of all
   tab work. Eight tabs are under 35 toolkit lines each.

Per tab, in the order the workload actually falls:

| tab | tk-lines | | tab | tk-lines |
|---|---|---|---|---|
| Vault | 491 | | Changelog | 35 |
| Council | 422 | | Vault Health (adv) | 34 |
| Grapher | 270 | | Tool Creation | 33 |
| Specialists | 130 | | Nodes (adv) | 33 |
| Dream3D | 81 | | IDE (adv) | 27 |
| Models | 73 | | Lens | 26 |
| Agents (adv) | 46 | | Diagnostics | 26 |
| Sessions | 45 | | Speech | 24 |
| Agent Jobs | 44 | | Librarian (adv) | 20 |
| GUI Designer | 43 | | Apothecary (adv) | 2 + 411 in its module |

Cross-check by a different unit: **1,391 widget construction sites** across 24 distinct
classes, 63% of them Label/Frame/Button — mechanical conversions.

---

## 2. The folder structure, and the amendment

Your instinct — write the new app beside the old one — is correct, and it is forced by a
measured fact: **Tk and Qt cannot share a process.** A `QApplication` flips the process to
per-monitor DPI awareness and the live Tk window shrinks from 520 to 416 physical pixels
instantly. So there is no half-ported window; there are only two apps, and the user
launches one or the other.

**The amendment: the new folder holds the UI only.** If it holds a copy of the app, you
fork ~28,000 lines of logic and maintain both for a year — which is how rewrites die. You
don't have to, because the logic is barely attached to the widgets. Measured: 135
logic-shaped methods (7,264 lines) reach the UI through only **34 entry points**, and 76%
of those go through three seams — `_append_transcript` (193 sites), `after` (85),
`ui_q.put` (46). The rest are per-tab "append text to a pane" variants.

So each phase does two things, in order:

1. **Extract that area's logic in place, in the Tk app**, behind a ~10-method view
   interface. Tk keeps running, its tests keep passing, and this step ships on its own.
2. **Write the Qt view in the new folder**, implementing the same interface.

```
council_core/          extracted toolkit-neutral logic — imported by BOTH front ends
council_qt/            the new front end, UI only (~3,900 lines when finished)
    app.py             QApplication + CouncilWindow(QMainWindow)
    bridge.py          ui_q -> GUI thread; call_on_ui(); the after() shim
    theme.py           branding.py tokens -> QPalette + QSS
    dialogs.py         tkinter-signature shims over QMessageBox/QFileDialog
    widgets/           transcript, log pane, the shared composites
    tabs/              one module per tab
council_gui_engine.py  the Tk shell — untouched, still ships, imports council_core
council_qt.py          the second entry point
```

A bug fixed mid-port is fixed **once**, in `council_core`, for both faces. The Qt tree can
never go stale, because it was never a copy.

**Recommendation:** sibling packages in the same repo, not a separate project — the shared
imports stay trivial, one install, two entry points, and `COUNCIL_TOOLKIT=qt` selects.

---

## 3. The phases

Each phase ends with the Tk app still shipping and the Qt app doing strictly more than it
did before. Days are `likely` at the rate justified in §6.

| # | phase | what it covers | days |
|---|---|---|---|
| **0** | **Prepare** | Install and pin PySide6-Essentials 6.10.2 on the 3.11 floor. (No deletions — see the correction in §1.) | 1 |
| **2a** | **The standalone-tab host** | `council_modules.StandaloneHost` + `PALETTE` as Qt: a host that runs one tab module on its own (its own window, queue, theme, model slots). The `council_qt` foundation already is this shape; it needs the compatible API. Unblocks porting `tab_grapher`, and `tab_ideas`/`tab_video` when those branches are merged. | 2–4 |
| **1** | **Make the suite able to fail** | See §5. Without this, every later green run is uninterpretable. | 3–5 |
| **2** | **Foundation** | `council_qt/` skeleton, the thread bridge, theme, dialog + variable shims, transcript widget. ~2,000–3,300 lines of new infrastructure with no Tk counterpart. | 26–36 |
| **3** | **Extraction** | `council_core` behind the view interface. Ships on Tk alone. Can overlap phase 2. | 8–18 |
| **4** | **Test harness** | Offscreen Qt fixture; "every tab builds and every wired name resolves"; queue-replay parity. | 16–24 |
| **5** | **Pilot: Vault (calibration gate)** | 491 toolkit lines, the superset of mechanisms (Treeview, PanedWindow, forms, worker traffic, 3 dialogs). **Exit criterion is a re-forecast**, not a working tab. | 12–20 |
| **6** | **Council + Grapher + Dream3D** | 773 toolkit lines. Carries the transcript (the hardest widget in the app) and matplotlib. | 17–28 |
| **7** | **The ten remaining default tabs** | 464 toolkit lines of forms, lists and tables. Fastest phase per line. | 13–20 |
| **8** | **Designer canvas + wizards** | `gui_canvas` (costed structurally, not by line — see §6), `gui_wizard`, `gui_runwith`. | 11–16 |
| **9** | **Advanced tabs + delegated panels** | 6 tabs (176) + `apothecary_engine` (411, the worst threading in the repo), `sage_agent`, `vault_agent`. | 18–30 |
| **10** | **Startup chain, dialogs, chrome** | The 83 orphan toolkit lines plus splash, onboarding, activation, crash reporting, and the 20 hand-built Toplevels. | 16–24 |
| **11** | **Packaging, CI, cutover** | `council.spec`, CI, both toolkits in the bundle for one release, rollback via `COUNCIL_TOOLKIT=tk`. | 5–9 |
| **12** | **Post-cutover defect burn-down** | 20% of translation effort, budgeted rather than hoped for. | 14–26 |

**Phase 5 is the decision point, and it is deliberately early.** Its exit criterion is a
measured rate and a re-forecast of the remaining ~3,000 lines. If the real rate lands
below ~30 toolkit lines/day, the honest recommendation is to stop there — at roughly 60
days spent, with a better Tk app, a clean core, a real test harness, and one Qt tab to
show for it.

---

## 4. What ships when

- After phase 3: **a better Tk app** — thread-safety fixes, dead code gone, clean core.
- After phase 6: a Qt build worth opening — ask a question, see the data, manage the vault.
- After phase 7: **consumer parity.** Every tab a non-advanced user sees.
- After phase 11: Qt becomes the default, Tk still in the bundle as one-env-var rollback.

---

## 5. Verification: the part that is genuinely alarming

The suite is much weaker over this code than "993 tests" suggests, and I verified each of
these:

- **`tests/smoke_test.py` has 169 test functions and zero real assertions.** Its
  `_check()` appends to a list and prints; `_FAILS` is only ever printed. Those 169 tests
  **pass unconditionally**. Real protective count: ~824, not 993.
- **CI runs no tests at all.** `build.yml` verifies imports and runs PyInstaller.
- **2 of 31 test files reference `CouncilConsole`**, and one of those asserts on *source
  text* (`assert "self.after(0" in src`) — pinning a spelling the port must change.
- `_poll_ui_queue` wraps its 33-branch dispatch in `except Exception: print(...)`, so
  branches can die silently; 78 `hasattr` + 96 `getattr` guards turn a missing widget into
  a silently disabled feature rather than an error.

So the 993-test suite would stay green through a completely broken window. Phases 1 and 4
exist to fix that, and they are not optional: they are what makes every later "done" claim
mean anything. Manual regression is then **1.5–2.5 days per shipped phase** against
written per-tab checklists covering ~329 interactive controls, 20 dialogs and 61
thread-backed actions.

---

## 6. The estimate, and how to argue with it

**Unit:** toolkit-bound lines. **Rate:** 30 / 45 / 55 lines per day (pessimistic /
likely / optimistic), *including* reading the surrounding logic and hand-verifying on
screen — because there are no UI tests to verify for you.

| component | likely days | basis |
|---|---|---|
| translation of 5,375 toolkit lines | 119 | 5,375 ÷ 45 |
| foundation (phase 2) | 30 | new infrastructure, not line-costed |
| extraction (phase 3) | 12 | 34 entry points behind a view interface |
| test infrastructure (phases 1, 4) | 20 | harness + replay parity |
| manual regression | 16 | ~2 days × 8 shipped phases |
| packaging and CI | 7 | measured bundle work |
| manual regression | 18 | ~2 days × 9 shipped phases |
| post-cutover defects | 24 | 20% of translation |
| **subtotal** | **230** | |
| contingency @25% | 58 | untested UI, God class, live bugs |
| **total** | **~288** | range **255–377** |

At 70% utilisation that is **17–25 months solo**, ~19 at the likely rate.

The number has moved twice, both times because the target grew rather than because the
method changed — 238 days when six modules were wrongly called dead, 255 once they were
counted, 288 once the other branches' tabs were included. That is worth watching: the
estimate is stable per line of surface (45/day) and unstable in the surface itself. Any
further branch merged before the port finishes moves it again, which is an argument for
settling what ships before committing to the calendar, not after.

At 70% utilisation: **13 months solo at the likely case, 20 at the pessimistic.** Two
developers do not halve it — the foundation is one person's work and the tabs share
idioms — but roughly 10 months for ~25% more total spend.

**Two known weaknesses in this number, stated rather than buried:**

- An independent review of the same model judged it **low by ~40%**, on the grounds that
  the rate was partly circular (derived from a stage estimate, then used to cost that
  stage). If that review is right, the likely case is ~330 days, not 238. I have no
  measurement on *this* code to settle it — which is exactly why phase 5 exists.
- **Line-counting fails for `gui_canvas.py`.** Its AST metric says 119 toolkit lines, but
  645 lines of renderers draw through five wrappers and contain no Tk token at all. It is
  costed structurally in phase 8 instead. Expect other pockets like it.

---

## 7. The risks that would move the number

| risk | cost if it lands | why it is real |
|---|---|---|
| `QTimer.singleShot(0, fn)` from a worker thread **never fires, silently** | +8 to +20 d | It is the direct translation of ~83 `after(0, ...)` sites, most called from workers. A naive port deletes those code paths with no error and a green suite. |
| The transcript is a different document model, not a translation | +10 to +25 d | 31 `tag_configure`, 77 `"1.0"` indices, 117 state toggles, clickable chips, live token streaming. The obvious route (QTextBrowser + HTML) makes model output HTML-interpreted, so `<` and `&` in an answer render or vanish. |
| The dark theme does not land | +5 to +15 d | 271 hex literals, 187 fg lines, 71 font tuples written against ttk's flat look. This repo has already been bitten by exactly this (the vista/clam measurement). |
| Branch drift and scope creep | +10 to +30 d | `main` shipped 5 commits in 6 days; two efforts are uncommitted on this same base. The port walks a 22,610-line God class with known live bugs. |
| Rate is 30/day, not 45 | +85 d | The single biggest lever. Phase 5 measures it at ~60 days spent. |

---

## 8. Decisions before phase 0

1. **Install PySide6-Essentials 6.10.2 into the 3.11 `council` env?** Recommend **yes** —
   one env, one `pytest tests`, and the measured offscreen result means Tk and Qt tests can
   share a session safely. Without it the Qt work cannot run on the project's own floor.
2. **Sibling packages in this repo, or a separate project?** Recommend **sibling**.
3. **Is one developer for 13–20 months the staffing assumption?** The plan's shape — a
   calibration gate at ~day 60, an idiom library built once and reused 19 times — assumes
   a single owner carrying continuity.
4. **Drop the `tkinterweb` interactive Grapher view rather than find a Qt equivalent?**
   Recommend **drop**: it is already optional, already falls back to the browser, and the
   code's own comment records that it can only ever show a static shell.
5. **Defer `apothecary_engine` (411 toolkit lines) past cutover?** Recommend **yes** —
   advanced-mode only and lazily imported, so hiding it is a one-line change.
6. **Does the Tk shell stay supported after cutover?** Recommend freeze at cutover, keep
   buildable for two releases as rollback, then delete. Maintaining two shells
   indefinitely converts a one-time port into a permanent tax.

---

## 9. What you get, and the cheaper alternatives

**For ~238 days:** one toolkit, a crisp DPI-correct UI, a shell that can host live camera
views and embed napari, and the thread-safety debt paid off.

**For ~60 days (phases 0–5):** a better Tk app, a clean core, a real test harness, and a
measured answer to whether the rest is worth it. This is the recommended commitment.

**For 0 additional days:** what you already have — generated apps emit as PySide6 today,
which is where the camera work was going to live anyway. The shell shows text, tables and
charts; it draws no images and no live frames, so it gains the least from Qt of anything
in the product.

The honest summary is that this port is worth doing if the **shell itself** needs to show
live frames or host Qt components. If the camera roadmap lives in generated apps, the
measured payoff of porting the shell is mostly aesthetic.
