# Data's Inferno — User Guide

The README covers install and the value pitch. This guide goes deeper so you can actually be productive with your own data on day one.

---

## How to ask a question that gets a useful answer

Data's Inferno answers in proportion to the structure of your question. Three patterns work consistently well:

### Pattern A — Point at a file, then ask
> "Looking at orders.csv from this month, what changed compared to last month?"

The panel will route to data-comparison logic. The Coder pulls the rows, the Analyst computes the deltas, the Writer explains them.

### Pattern B — Show a sample, then ask
Paste a few rows of data into the input, then ask:
> ```
> customer_id, last_order, total_spend
> A123,        2026-04-15, 4200
> A124,        2026-01-02, 850
> A125,        2026-04-30, 12100
>
> What's a sensible "high-value-but-dormant" threshold for follow-up?
> ```

### Pattern C — Specify the deliverable
> "Give me a one-paragraph summary I can paste into an email to my supplier."

The Writer will format strictly to that. If you ask for "a SQL query that runs on Postgres", the Coder respects the dialect.

---

## The Council tab — anatomy

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

- **Transcript** (left): live, append-only. Scroll back, copy any line, save the entire session.
- **Judge panel** (right): which route was chosen, who's on the panel, the live confidence score, and the round counter. If you see "Round 4+", the question is genuinely ambiguous.

### What the verdicts mean

- **PASS** — The Judge accepted the lead answer.
- **RETRY** — The draft was rejected; the panel iterates.
- **CHANGE** — A different specialist's draft won out.

### Pausing for clarification

When a panel member needs a clarifying detail, the Council pauses and shows a yellow "Awaiting your answer…" banner. Type your reply and press Enter to resume.

---

## The AI panel — who's on it and when

You don't pick the panel — the Judge does, based on what you asked. Here's the matching logic:

| Role | Voice | Best for |
|------|-------|----------|
| **Judge** | Decisive, terse | Routing & final verdicts. Always present. |
| **Writer** | Crisp, organised | Plain-English summaries, narrative reports |
| **Coder** | Systematic, defensive | Pandas/SQL queries, calculations, joins |
| **Intern** | Eager, fast | First-pass scans, rough sketches |
| **Peasant** | Skeptical, plain-spoken | Cross-examination — "why does that follow?" |
| **Sage** | Knowledgeable | Historical context from your indexed files |
| **Strategist** | Long-horizon | Trends, projections, what-to-watch |
| **Skeptic** | Adversarial | Find flaws in the proposed answer |
| **Artist** | Visual | Chart styling, dashboard layout suggestions |
| **Director** | Editorial | Style consistency for reports going out |
| **Content** | Audience-aware | Tone for customer-facing language |
| **Algorithm** | Pattern-aware | Distribution shape, outliers, clustering |
| **Coach** | Performance-focused | Pacing, simplification, what to drop |

You can override the Judge's choice on the **🔍 Lens** tab.

---

## The Grapher tab — your starting point for any new dataset

This is usually where a session begins.

### Supported formats
CSV · TSV · XLSX · XLS · JSON · NPY · NPZ · whitespace-delimited TXT

### The flow
1. **Drag a file** onto the Grapher tab (or use Browse).
2. The schema panel auto-detects column types: numeric · date · category · text.
3. **Pick a chart type** — bar, line, scatter, pie, histogram, box, heatmap.
4. **Pick X / Y / colour columns.** Aggregations: sum, mean, count, min, max.
5. The chart renders in the right pane with light interactivity.

### The Analyst AI
Click **🧠 Ask Analyst** and it will:
- Suggest the most informative chart for *this* shape of data
- Spot outliers, missing values, suspicious distributions
- Propose transforms (log scale, normalise, drop nulls, group)
- Explain what the chart actually says in plain English

Most users start a session by dropping their file into the Grapher and asking the Analyst "what should I be looking at here?". The Analyst's first read is usually the right starting point.

### Live reload
Toggle 🔄 Live Reload — the chart re-renders whenever the source file changes. Useful if you're piping data from another tool or editing a CSV.

### Overlay
Load a second dataset to overlay on the same axes. Perfect for **before / after** comparisons (this month vs. last month, with promo vs. without).

### Sample datasets
First time on the Grapher? Click **📦 Load Sample** to drop in a synthetic but realistic purchase-orders dataset. Useful for trying the tool without exposing your real data.

---

## The Vault tab — your data warehouse for the panel

The vault is where all your indexed files live. Two purposes:

1. **See what the AI panel knows.** Tree view of every file you've added. Click any to preview.
2. **Add files for the panel to learn from.** Drop in:
   - A folder of monthly export CSVs
   - Your supplier price-list PDF
   - A folder of reference documents (e.g. "policy.md", "glossary.md")
   - Cloned git repos (your codebase, vendor docs)

### What the panel does with vault files
The **Sage** personality has access to a search index over everything in the vault. When you ask about your data, the Sage retrieves the most relevant rows or paragraphs and feeds them into the deliberation as context.

### What to put in the vault
- Recent orders/inventory exports
- Customer master file
- Product catalogue
- Supplier contact list
- Anything you'd reference yourself in a meeting

### What NOT to put in the vault
- Raw secrets (passwords, API keys) — RAG indexes everything indiscriminately
- Multi-GB log files — slow to index, low signal

---

## The IDE / Runner tab — for power users

Anything the Coder writes ends up here. You can:

1. **Edit it inline.** The panel picks up your edits as the new working version.
2. **Run it.** Click ▶ Run. Output streams back to the Council transcript.
3. **Snapshot it.** 📸 Save it to the vault for re-use later.

⚠ **Trust gate**: When the Coder produces code that touches files, calls subprocesses, makes network calls, or evaluates dynamic code, you'll see a confirmation dialog listing exactly what's risky and which lines. Review and approve before it runs. Trust decisions are session-scoped — restart the app and you'll be asked again.

The runner is sandboxed to `vault/workspace/`. Files written outside that directory require approval.

### When you'd use this
You normally won't — the panel handles queries internally. The IDE is for when you want to:
- Hand the Coder's script to a colleague
- Modify the analysis (e.g. tweak a threshold)
- Re-run the same script against next week's data

---

## The Lens tab — second opinions

When the Council reaches a verdict you don't trust, paste the verdict into the Lens, pick which specialists should review it, and run.

Each picked specialist reviews **in parallel** and gives an independent take. No deliberation, no consensus — just N opinions side-by-side.

Common patterns:
- **Number-check**: Coder + Algorithm + Skeptic
- **Story-check**: Writer + Content + Peasant
- **Plan-check**: Strategist + Skeptic + Sage

---

## The Sessions tab — every past analysis

Every conversation is auto-saved. The Sessions tab shows them newest-first with a search bar. Click any session to:
- See the full transcript
- Re-run the same question against fresh data
- Export to PDF/Markdown
- Continue from where you left off

When the panel finishes a deliberation, it writes a one-paragraph summary too — that's what you see in the session list.

### Crash recovery
If Data's Inferno closes mid-deliberation (power blip, crash, accidental quit), the next launch offers to **resume the session** so you don't lose the in-flight analysis. You can also discard it and start fresh.

---

## The Vault Health tab

Your panel gets smarter the more you use it. Vault Health shows you what's been learned and what's missing.

**Memory snapshots** — what each AI specialist has stored as long-term notes about your business. Click a role to see its memory file. Edit if a stored note is outdated.

**Knowledge gaps** — questions the Sage couldn't fully answer. Over time these become a wishlist of files you should add to the vault to fill the gap.

**Wishlist** — manually-add topics. If you're working in a domain Data's Inferno doesn't know yet, add it here so it gets surfaced during deliberations.

---

## The Speech tab

**Record → Transcribe → Council**
Click 🎙 Record, speak, click ⏹ Stop. The audio is transcribed locally (offline, no internet) and the transcript becomes your next message.

**TTS playback**
Any text in the Council transcript can be played back aloud. Local TTS, works offline.

Useful for:
- Hands-free review while driving / cooking / walking
- Reading long deliberations aloud during commute

---

## The Apothecary tab

Maintenance console. Useful commands:

- **Cache flush** — clears `vault/.cache/`
- **Log tail** — follows the application log
- **RAG re-index** — rebuilds the vault search index from scratch
- **Personality reload** — picks up edits to backends config without restart
- **Backup vault** — zips the current vault for archival

If anything misbehaves, this tab is your first stop.

---

## The Nodes tab (advanced)

Data's Inferno can offload heavy reasoning to a remote machine over SSH if you have, for example, a beefier desktop or a homelab GPU sitting idle.

### Setup
1. The remote machine runs Ollama on its internal IP.
2. Nodes tab → Add Node → fill in name, host, user, key path, model.
3. Data's Inferno probes it; if reachable, it appears with a green dot.
4. Pin specific specialists to specific nodes — heavy reasoning (Sage, Strategist) on the remote box, fast roles (Judge, Peasant) local.

Most users never need this. If your laptop is enough, leave it alone.

---

## Keyboard shortcuts

| Action | Shortcut |
|--------|----------|
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
1. Launch          → Data's Inferno opens to the Grapher tab
2. Drop file       → orders_april.csv → schema auto-detects 12 columns
3. 🧠 Ask Analyst  → "highest-revenue category was Tools, but order count
                     was actually flat — average order value rose 22%"
4. Switch tab      → ⚖ Council
5. Type            → "Show me which specific Tools SKUs drove the AOV bump"
6. Watch panel     → Coder pulls rows, Analyst groups, Writer explains
7. Verdict         → 89% confidence — top 3 SKUs are 60% of the lift
8. 🔍 Lens         → Skeptic reviews: "is 60% statistically meaningful with
                     this sample size?" — caveat noted
9. Save            → 📚 Librarian → Save Session as PDF, email to team
```

Most useful sessions move between 2–3 tabs as the task evolves.

---

## When things go wrong

- **The panel seems stuck on Round 5+** — the question is too ambiguous. Cancel (Esc), refine, restart.
- **Same answer regardless of question** — restart Ollama. A model can wedge.
- **Grapher chart is empty** — the column types weren't detected correctly. Schema panel → manually override types.
- **High RAM usage** — heavy models stay loaded between calls. Apothecary → Unload to free.
- **Sage doesn't know your data** — feed it. Vault tab → drop your most relevant files there, then re-run the question. RAG picks up new files within a minute.

---

## Pro tips

- **Type slower, not faster.** The first message of a session sets the tone. Spend a sentence telling the Judge what you want ("explain like I'm new to my own data" / "give me numbers I can put in a quarterly report" / "review this critically").
- **Drop files early.** The panel is *much* sharper when it has actual data to look at. Don't describe your data — show it.
- **Use Lens for second opinions.** Whenever a verdict has business consequences, paste it into Lens. Skeptic will catch what the main panel missed.
- **Check Vault Health weekly.** Adding the files it asks for pays dividends — your panel gets noticeably smarter every time you do.
- **Don't fight the Judge's routing.** If it routes a chart question to the Grapher, follow. The panel knows which tab is best for which kind of question.
