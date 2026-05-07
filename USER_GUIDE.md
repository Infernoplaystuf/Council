# Data's Inferno — guide

The README covers setup. This goes a layer deeper into how to actually use it.

---

## How to ask a question that gets a useful answer

The Council answers in proportion to the structure of your question. Three patterns work consistently:

**Pattern A — Point at a file, then ask**
> "Looking at orders.csv from this month, what changed compared to last month?"

**Pattern B — Show a sample, then ask**
Paste a few rows of data into the input, then ask:
```
customer_id, last_order, total_spend
A123,        2026-04-15, 4200
A124,        2026-01-02, 850
A125,        2026-04-30, 12100

What's a sensible "high-value-but-dormant" threshold for follow-up?
```

**Pattern C — Specify the deliverable**
> "Give me a one-paragraph summary I can paste into Slack."

The Writer formats strictly to that. If you want code that runs as-is, ask for that.

---

## The Council tab — what you're seeing

```
┌────────────────────────────────────────────────────────┐
│ Transcript                          │ Judge panel      │
│ ─────────────                       │ ─────────────    │
│ [User]    Top 5 dormant customers?  │ Route: data      │
│ [Coder]   SELECT * FROM ...         │ Panel:           │
│ [Analyst] Here's a chart of …       │   • coder        │
│ [Peasant] Why is X above the cut?   │   • analyst      │
│ [Judge]   Verdict: PASS (91%)       │   • peasant      │
│                                      │ Confidence: 91%  │
│                                      │ Rounds: 1        │
└────────────────────────────────────────────────────────┘
```

- **Transcript** (left): live, append-only. Scroll back, copy lines, save the whole session.
- **Judge panel** (right): which route was chosen, who's on the panel, confidence, round counter.

If you see Round 4+, the question is genuinely ambiguous — cancel, rephrase, restart.

### Verdicts

- **PASS** — Judge accepted the lead answer.
- **RETRY** — Draft rejected; panel iterates.
- **CHANGE** — A different role's draft won out.

When a panel member needs a clarifying detail, the Council pauses with a yellow banner. Reply and it resumes.

---

## The AI panel

You don't pick the panel — the Judge does, based on what you asked.

| Role | Voice | Best for |
|---|---|---|
| **Judge** | Decisive, terse | Always present. Routes + final verdict. |
| **Writer** | Crisp, organised | Plain-English summaries |
| **Coder** | Systematic, defensive | Pandas/SQL queries, calculations |
| **Intern** | Eager, fast | First-pass scans |
| **Peasant** | Skeptical | "Why does that follow?" |
| **Sage** | Knowledgeable | Historical context from indexed files |
| **Strategist** | Long-horizon | Trends, projections |
| **Skeptic** | Adversarial | Find flaws |
| **Artist** | Visual | Chart styling, layout suggestions |

You can override on the **🔍 Lens** tab.

---

## The Grapher

Where most sessions start.

**Supported formats:** CSV · TSV · XLSX · XLS · JSON · NPY · NPZ · whitespace TXT

**The flow:**
1. Drag a file in (or use Browse, or 📦 Sample for the bundled fake data)
2. Schema panel auto-detects column types
3. Pick chart type + axes + aggregation
4. Renders in the right pane

**🧠 Ask Analyst** is the move. It will:
- Suggest the most informative chart for *this* shape
- Spot outliers, missing values, weird distributions
- Propose transforms (log, normalise, drop nulls)
- Explain what the chart actually says

**🔄 Live Reload** re-renders the chart whenever the file changes on disk. Handy if I'm editing the CSV in another tool.

**Overlay** loads a second dataset on the same axes — before/after comparisons.

---

## Find & Chart (the ask-first flow)

When you ask the Council a chart-shaped question — "show me monthly revenue", "graph yearly sales", "plot inventory dormant for 6 months" — the Council scans the vault and bundled samples for relevant files, registers them all in the Grapher's dropdown, loads the top match, and asks the Analyst for a chart, all in one go.

Two triggers:

- **Auto** — type a question with chart keywords (graph/chart/plot/show me/by month/trend/...) and press Send.
- **Explicit** — click the 📊 *Find & Chart* button next to Send.

The synonym map covers the gap between business words and CSV columns: "revenue" matches files that have `total/amount/sales`; "inventory" matches `stock/qty/sku`; "monthly" matches `date/month`. Mostly fine. Occasionally finds the wrong file first — the dropdown lets you swap in one click.

---

## The Vault

Two things:

1. **See what the panel knows.** Tree view of every file you've added.
2. **Add files.** Drop in:
   - Recent CSV exports
   - Reference docs
   - Cloned git repos (your codebase, vendor docs)

The Sage has a search index over everything in the vault. It pulls relevant rows or paragraphs into the deliberation as context.

Don't drop secrets in here — RAG indexes everything indiscriminately.

---

## Personal Specialists

Each is a *named lens* on the same shared vault. There's no per-specialist data folder; everything lives in the vault.

**Three pre-built:**
- **💰 Sales Specialist** — revenue, AOV, retention, churn
- **📦 Inventory Specialist** — stock, turnover, dead stock, suppliers
- **🤝 Customer Specialist** — loyalty, dormancy, segmentation

**Auto-summon vs manual:**

| | Auto | Manual |
|---|---|---|
| Trigger | Question contains a domain keyword | Pick from "Ask: [specialist ▾]" in Council |
| When | Default | When keywords didn't catch what I want |

When the panel is consulting a specialist, you'll see in the transcript:
> *Council  Consulting: 💰 Sales Specialist, 📦 Inventory Specialist*

**Multi-specialist deliberation:** if 2+ specialists match, all of them are summoned. The model sees a "MULTI-SPECIALIST DELIBERATION" header telling it to apply each lens and reconcile when they conflict.

This is the answer to questions like *"Should I order more inventory based on last year's sales trend?"* — Sales lens AND Inventory lens, reconciled.

**Editing a specialist:** four fields in the detail pane:
1. Description
2. Domain keywords (comma-separated)
3. Lens / system-prompt overlay
4. Base personality (writer / sage / strategist / coder / etc.)

The 🧪 *Test* button runs a sample question with the lens applied — useful for tuning the prompt.

---

## Model configuration

By default every personality runs on the **same** Ollama model. Switching models is what makes the panel feel slow — Ollama swaps models in/out of memory between calls. With one shared model, rotation between roles is free.

Override per role in `vault/personality_backends.json`:

```json
{
  "writer":  "local_general_alt",
  "coder":   "local_coder_primary",   ← if you want a code-tuned model
  "intern":  "local_general_alt",
  "judge":   "local_general_alt",
  "peasant": "local_general_alt",
  "artist":  "local_general_alt"
}
```

Hot-reloads — no restart needed. Backend keys map to actual Ollama model names; see `council_engine.py` for the table.

Only worth doing if you have 32+ GB RAM (so two models can stay hot) or you're using a remote node.

---

## The IDE / Runner

Anything the Coder writes lands here. Edit, click ▶ Run, output streams into the Council.

⚠ **Trust gate**: if the Coder produces code that touches files, runs subprocess, makes network calls, or evaluates dynamic code, you'll see a confirmation dialog listing what's risky. Trust decisions are session-scoped (SHA-256 of script body); restart and you'll be asked again.

The runner is sandboxed to `vault/workspace/`.

This tab is hidden by default — set `COUNCIL_ADVANCED=1` before launching to see it.

---

## The Lens

Pick any subset of personalities and have them all critique the same text in parallel. No deliberation, no consensus — just N opinions side-by-side.

Common patterns:
- **Number-check**: Coder + Algorithm + Skeptic
- **Story-check**: Writer + Content + Peasant
- **Plan-check**: Strategist + Skeptic + Sage

---

## Sessions

Every conversation auto-saves. Newest-first. Click any to load the full transcript.

**Crash recovery:** if the app dies mid-deliberation, next launch offers to resume the orphaned session.

---

## Speech

Record → transcribe (faster-whisper, local) → feed to Council. TTS playback works offline via pyttsx3.

Useful for hands-free brainstorming.

---

## Keyboard shortcuts

| Action | Shortcut |
|---|---|
| Send message | `Enter` (in input field) |
| Newline in input | `Shift+Enter` |
| Cancel deliberation | `Esc` |
| Switch tab | `Ctrl+Tab` / `Ctrl+Shift+Tab` |
| Run script (IDE) | `F5` |
| Save script (IDE) | `Ctrl+S` |
| Quick search transcript | `Ctrl+F` |

---

## A typical end-to-end session

```
1. Launch          → Council tab
2. Type            → "Show me which Tools SKUs drove last month's AOV bump"
3. Watch           → Sales + Inventory specialists auto-summon. Council
                     finds orders.csv, loads it, asks Analyst.
4. Get a chart     → Top 3 SKUs are 60% of the lift
5. 🔍 Lens         → Paste verdict, Skeptic reviews: "is 60% statistically
                     meaningful with this sample size?"
6. Save            → Auto-saved in 🕓 Sessions
```

Most useful sessions move between 2–3 tabs as the task evolves.

---

## When things go wrong

- **Council stuck on Round 5+** — question is too ambiguous. Esc, refine, restart.
- **Same answer regardless of question** — restart Ollama. A model can wedge.
- **Empty Grapher chart** — column types weren't detected. Manually override in the schema panel.
- **High RAM** — heavy models stay loaded. Apothecary tab → Unload (set `COUNCIL_ADVANCED=1` first).
- **Sage doesn't know your data** — feed it. Vault tab → drop relevant files. RAG re-indexes within a minute.
- **Crash log appears** — check `vault/logs/crashes/` for the trace.
