# Council — User Guide

The README covers install and a high-level tour. This guide goes one layer deeper so you can actually be productive.

---

## How to ask a good question

Council answers in proportion to the structure of your question. There are three patterns that work consistently well:

### Pattern A — Pose a problem, not a task
> "I keep getting timeouts when I open a 200MB CSV in Excel. What are my options?"

The Judge will route this to a planning panel (intern + writer + peasant), each will draft an answer, the Peasant will press for specifics, and you'll get a ranked list of options with trade-offs.

### Pattern B — Show, then ask
Paste a chunk of text, code, or data, then ask. Council reads what you paste as primary context.
> ```
> [your data here]
>
> What does this tell me about Q3?
> ```

### Pattern C — Specify the deliverable
> "Write me a single-paragraph summary I can paste into Slack."

The Writer will format strictly to the deliverable. If you ask for "code that runs as-is", the Coder will avoid pseudocode.

---

## The Council tab — anatomy

```
┌────────────────────────────────────────────────────────┐
│ Transcript                          │ Judge panel      │
│ ─────────────                       │ ─────────────    │
│ [User]    What is X?                │ Route: writer    │
│ [Writer]  X is …                    │ Panel:           │
│ [Intern]  Counterpoint …            │   • writer       │
│ [Peasant] But how does …            │   • intern       │
│ [Judge]   Verdict: PASS (87%)       │   • peasant      │
│                                      │ Confidence: 87%  │
│                                      │ Rounds: 2        │
└────────────────────────────────────────────────────────┘
```

- **Transcript** (left): live, append-only. You can scroll back, copy any line, save the entire session.
- **Judge panel** (right): which route was chosen, which personalities were summoned, the live confidence score, and the round counter. If you see "Round 4+" the Council is struggling — usually means the question is genuinely ambiguous.

### What the verdicts mean

- **PASS** — The Judge accepted the lead personality's draft.
- **RETRY** — The draft was rejected; the panel will iterate.
- **CHANGE** — A different personality's draft won out.

### Pausing for clarification

When a personality asks a clarifying question mid-deliberation, the Council pauses and shows a yellow "Awaiting your answer…" banner. Type your answer in the input box and press Enter to resume.

---

## Personalities — what each one is good for

| Role | Voice | Best for |
|------|-------|----------|
| **Judge** | Neutral, decisive, terse | Routing & final verdicts. Always present. |
| **Writer** | Crisp, organised | Documents, summaries, plain explanations |
| **Coder** | Systematic, defensive | Production code, refactors, bug hunts |
| **Intern** | Eager, fast, broad | First-pass scaffolding, brainstorms |
| **Peasant** | Skeptical, plain-spoken | Cross-examination — "why is this true?" |
| **Artist** | Visual, evocative | UI copy, naming, visual descriptions |
| **Sage** | Knowledgeable, calm | Domain knowledge, RAG-assisted answers |
| **Strategist** | Long-horizon, structured | Plans, sequencing, options analysis |
| **Skeptic** | Adversarial | Find flaws in a proposed answer |
| **Director** | Editorial | Style consistency across long output |
| **Content** | Audience-aware | Tone, framing for a specific reader |
| **Algorithm** | Pattern-aware | Observation about distribution & systems |
| **Coach** | Performance-focused | Delivery, pacing, presentation polish |

You don't pick the personalities — the Judge does. But you can override on the **Lens** tab.

---

## The IDE / Runner tab

Anything the Coder produces lands here. You can:

1. **Edit it inline.** The Council picks up your edits as the new working version.
2. **Run it.** Click ▶ Run. Output (stdout + stderr) streams to the Council transcript so the panel can react.
3. **Send back to Council.** Output becomes part of the next deliberation's context.

The runner is sandboxed to `vault/workspace/`. Files written outside that directory are blocked unless you explicitly approve.

### Workflow: build something iteratively

```
You    : "Build me a script that finds duplicate files by hash"
Coder  : (writes draft into IDE)
You    : ▶ Run on your dataset
Output : Found 217 duplicate clusters
You    : "Now group them by extension and write a CSV"
Coder  : (edits in place — only the diff)
```

This back-and-forth is how Council expects you to use it. Don't try to one-shot complex code in a single prompt.

---

## The Grapher tab

Drop a file → get charts.

### Supported formats
CSV · TSV · XLSX · XLS · JSON · NPY · NPZ · plain TXT (whitespace-delimited)

### Built-in workflow
1. **Load** your file (Browse or drag-drop).
2. The schema panel auto-detects column types (numeric / date / categorical / text).
3. **Pick a chart type** — bar, line, scatter, pie, histogram, box.
4. **Pick X and Y columns.** Aggregations available: sum, mean, count, min, max.
5. The chart renders in the right pane.

### The Analyst AI
A specialist personality that lives only in the Grapher tab. Click 🧠 Ask Analyst and it will:
- Suggest the most informative chart for your data
- Identify outliers or distribution issues
- Propose transforms (log scale, normalise, drop nulls)
- Explain what the chart actually shows in plain English

### Live reload
Toggle 🔄 Live Reload — the chart re-renders whenever the source file changes on disk. Useful if you're piping data from another tool.

### Overlay
Load a second dataset to overlay on the same chart axes. Useful for before/after comparisons.

---

## The Vault tab

Two purposes:

1. **Tree-view your vault.** See every file the Council has stored across sessions. Click any to preview.
2. **Clone external repositories.** Paste a git URL → choose a target subfolder → Clone. The clone is added to the RAG index so all personalities can reference it.

### What to clone
- Your own project repos (so the Coder can reason about your codebase)
- Documentation repos (e.g. a library's official docs)
- Reference papers or datasets

### What NOT to clone
- Anything with secrets — RAG will index everything indiscriminately
- Massive monorepos — slow to clone, slow to index, low signal

---

## The Lens tab

When the Council reaches a verdict you don't trust, paste the verdict into the Lens, pick which personalities should review it, and run.

Each picked personality reviews the text **in parallel** and gives an independent take. No deliberation, no consensus — just N opinions side-by-side.

Common patterns:
- **Code review**: Coder + Intern + Skeptic
- **Pitch review**: Writer + Content + Strategist + Peasant
- **Plan review**: Strategist + Skeptic + Sage

---

## The Vault Health tab

Your council gets smarter the more you use it. Vault Health shows you what's been learned and what's missing.

Three sections:

**Memory snapshots** — what each personality has stored as long-term notes. Click any role to see its memory file. Edit if you want to correct an outdated note.

**Knowledge gaps** — questions the Sage couldn't answer well. The Sage flags these automatically; over time they become a wishlist of things you should feed the Council (via the Vault tab — clone a relevant repo or drop a doc into `vault/`).

**Wishlist** — manually-added topics. If you're working in a domain the Council doesn't know yet, add it here so it'll surface during deliberations.

---

## The Speech tab

**Record → Transcribe → Council**
Click 🎙 Record, speak, click ⏹ Stop. The audio is transcribed locally (faster-whisper, no internet) and the transcript appears as your next message to the Council.

**TTS playback**
Any text in the Council transcript can be played back via 🔊 Speak. Uses local pyttsx3 — works offline.

Useful for:
- Hands-free brainstorming (drive, walk, cook)
- Reviewing a long deliberation while doing something else

---

## The Nodes tab (advanced)

Council can offload work to remote machines over SSH if you have, for example, a beefier desktop or a homelab GPU.

### Setup
1. The remote machine runs Ollama on an internal IP.
2. On the Nodes tab → Add Node → fill in name, host, user, key path, model.
3. The Council will probe it; if reachable, it appears with a green dot.

### Routing
Pin specific personalities to specific nodes. Heavy reasoning (Coder, Sage) on the remote 70B box; fast personalities (Judge, Peasant) local.

The pin lives in `vault/personality_backends.json` and respects backend keys like `pi_heavy` / `desktop_70b` that you define yourself.

---

## The Apothecary tab

Maintenance console. Useful commands:

- **Cache flush** — clears `vault/.cache/`
- **Log tail** — follows `vault/logs/council.log`
- **RAG re-index** — rebuilds the ChromaDB index from `vault/`
- **Personality reload** — reloads `personality_backends.json` without restart
- **Backup vault** — zips the current vault for archival

If anything misbehaves, this tab is your first stop.

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

## A typical session

```
1. Launch     →  Council opens to the ⚖ tab
2. Type      →  "I have a CSV of customer support tickets,
                I need to find which categories are growing fastest"
3. Watch     →  Judge routes to data analysis. Intern + Writer + Peasant
                deliberate. They suggest the Grapher tab.
4. Switch    →  📊 Grapher → Load your CSV → Ask Analyst
5. Iterate   →  Analyst proposes a "category by week" stacked bar chart
6. Discuss   →  Back to ⚖ Council, paste the chart's findings, ask
                "what should I tell my support manager"
7. Save      →  The whole session is auto-saved in 🕓 Sessions
```

Most useful sessions move between 2-3 tabs as the task evolves.

---

## When things go wrong

- **The Council seems stuck on Round 5+** — your question is too ambiguous. Cancel (Esc), refine the question, restart.
- **Same answer regardless of question** — restart Ollama. A model can wedge.
- **Grapher chart is empty** — your column types weren't detected correctly. Check the schema panel, manually override types if needed.
- **High GPU memory usage** — the heaviest models stay in memory between calls. Use Apothecary → Unload to free.
- **The Sage doesn't know things it should** — feed it. Clone the relevant repo into the vault, then run a deliberation that asks about that topic. The RAG will pick it up automatically.
