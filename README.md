# Anvil

An AI workshop for forging Godot games.

Open a Godot project. Ask a panel of AI specialists for a scene, a mechanic, a market read. They deliberate; you watch every step; you hit Run and see it in Godot a second later. Runs locally — your code and your concepts stay on this machine.

This is the home build. No licensing, no trials, no telemetry, no phone-home. If you got a copy from me, just run it.

---

## What it's for

- **Godot, end to end.** Anvil is the editor and the brain; Godot stays the runtime and visualiser. Edit a `.gd` script, hit Run, watch it in Godot's window without leaving Anvil.
- **A council of AI specialists, not one assistant.** When you ask "what's broken about this jump curve?" several personalities argue in front of you so you see disagreement, not a single confident answer.
- **Game-concept generation.** Brainstorm concepts (genre, hook, mechanics, comparable titles), then ship them into a Godot skeleton you can prototype the same afternoon.
- **Steam market signal.** Pull current Steam data and ask the Market Analyst "what couch-co-op puzzle games are doing well right now" — the analyst computes from cached data, never invents numbers.
- **Goal-anchored agents.** Small local models (8B-ish) stay locked on what you actually asked even when long files are in context. See `goal_anchor.py`.

## What it's bad at (today)

- It's early. Several of the tabs listed below are still scaffolding.
- It expects Godot to be installed separately. Anvil shells out to the `godot` binary — it doesn't bundle the engine.
- Models smaller than ~7B can be very literal; bump up if your machine tolerates it.

---

## How to run it

You'll need Python 3.11 and Godot 4.x.

```bash
# 1. Set up a Python env
conda create -n anvil python=3.11 -y
conda activate anvil
pip install -r requirements.txt

# 2. Get a GGUF model file from Hugging Face (qwen2.5-coder, granite-code,
#    etc.) and remember where you saved it.

# 3. Launch
python council_gui_engine.py
```

First launch shows a small setup wizard that confirms a GGUF model is selected and asks where your Godot binary lives. After that you're done.

If you want the bundled `.exe` instead of running from source:

```bash
pip install pyinstaller
build.bat        # Windows
./build.sh       # macOS / Linux
# Look in dist/Anvil/
```

---

## The tabs

| Tab | What it's for |
|---|---|
| **⚖ Council** | The chat. Ask anything. The Judge picks a panel and they deliberate. |
| **🛠 Godot Workspace** | The IDE. File tree, GDScript editor, scene-tree view, Run/Validate buttons, console panel. The edit-test-visualise loop lives here. *(Phase C-lite)* |
| **💡 Game Concepts** | Brainstorm concepts: genre, hook, mechanics, target audience, comparable titles. Ship a concept straight into the Godot Workspace as a scaffolded project. *(Phase B)* |
| **🎨 Pixel Art** | Hand-paint sprites. Pencil / eraser / fill / line / rect tools, vertical-mirror symmetry, predefined palettes (Default / NES / Game Boy / PICO-8), multi-frame animation with sprite-sheet export. Save into `vault/sprites/` or directly into the open Godot project. Pillow required. |
| **📈 Steam Market** | Pull current Steam stats (SteamSpy + SteamCharts, or your own Steam Web API key) and ask the Market Analyst what's working in the genre you care about. *(Phase D)* |
| **🎲 Simulations** | Run parameter sweeps over a Godot project (headless) or a Python game-model. Telemetry via `ANVIL_METRIC:` / `ANVIL_EVENT:` prints; results persisted under `vault/simulations/`. Eight built-in player personas (Greedy / Aggressive / Relaxed / Cautious / Speedrunner / Completionist / Hardcore / Casual) can be swept as another axis. The Sim Analyst specialist interprets distributions and suggests the next sweep. For self-driving games, drop in `assets/anvil_auto_player.gd` and your game gets a persona-aware decision loop — see `assets/anvil_demo_combat/` for a working example. |
| **🎓 Specialists** | Edit/create domain lenses. Game Designer, Genre Analyst, Steam Market Analyst, Sim Analyst, plus your own. |
| **🔍 Lens** | Paste an answer, pick which roles should review it in parallel. Useful when you don't fully trust what the Council just told you. |
| **🕓 Sessions** | Every past chat. Searchable. Click any to load. |
| **🗄 Vault** | The shared data pool. Game-design docs, reference scripts, ingested Steam JSON. |

A few "advanced" tabs (IDE, Agents, Nodes, Apothecary, etc.) carried over from the previous build are hidden by default. Set `COUNCIL_ADVANCED=1` before launching to see them.

---

## A typical session

```
1. Launch
2. Game Concepts: "co-op puzzle, 2 players, asymmetric roles, ~3hr playthrough"
   Game Designer + Genre Analyst summon. They draft mechanics, name candidates,
   nearest comparables on Steam.
3. Pick the concept I like → "Send to Godot Workspace"
4. Workspace: scaffolded project appears with placeholder scene + a main.gd
5. Council: "the player keeps clipping through the floor when respawning"
   Coder agent reads main.gd, proposes fix, writes it back.
6. Hit Run → Godot launches, I watch the fix work
7. Steam Market: "what 2D puzzle co-ops are charting this month?"
   Market Analyst computes from cached SteamSpy data, lists 5 with
   median revenue ranges. Source-cited, no invented numbers.
8. Sessions auto-save in the 🕓 Sessions tab
```

---

## Specialists

- **🎮 Game Designer** — mechanics, pacing, balance, player loops
- **🎭 Genre Analyst** — tropes, conventions, comparable titles, positioning
- **📈 Steam Market Analyst** — interprets ingested Steam data; the hard rule: never invent numbers, always cite source rows
- **🧑‍💻 Coder** — GDScript-focused ReAct loop; goal-anchored across retries so refactors don't drift

You can edit them in the 🎓 Specialists tab. Each one is just config: name, icon, description, keywords, system-prompt overlay, and which base personality wears the lens.

---

## Where the data lives

```
vault/
├── data_in/                ← REFERENCE DATA  (read-only by the app)
│   └── *.csv / *.json      drop design docs, level-design CSVs, etc. here
├── data_out/               ← APP OUTPUTS  (exports, generated assets)
├── steam/                  ← STEAM CACHE  (protected — analyst-only)
│   └── *.jsonl             SteamSpy / SteamCharts / Web-API pulls
├── projects/               ← GODOT PROJECTS  (each subfolder is a project)
├── conversations/          one JSONL per session  (app internal)
├── conversation_logs/      per-session debug log + goal cache (protected)
├── memory/                 per-personality persistent notes (app internal)
└── specialists.json        the specialist registry
```

The hard rule: **the model never reads Steam data directly.** Steam JSON lives under `vault/steam/`, which is in `PROTECTED_SUBDIRS` — only the Steam Market Analyst can compute from it, and it surfaces results with citations rather than passing raw rows to the council. This is the same anti-hallucination pattern that protected user-data files in the previous build.

All of `vault/` is gitignored. Delete the folder to reset.

---

## If something seems stuck after a `git pull`

Python caches compiled bytecode in `__pycache__/`. The launcher auto-purges stale caches on startup — but if you want to force-clean:

```bash
python clean.py
```

Then relaunch.

---

## Things to know

- Models swap = slow. By default every personality runs on the same GGUF so the GPU/RAM keeps one model hot. To mix in a code-specialist model for the Coder role, edit `vault/personality_backends.json`.
- The Godot Workspace Run button shells out to `godot --path <project>` — Anvil doesn't bundle Godot. Onboarding asks for the binary path on first launch.
- Crash logs land in `vault/logs/crashes/` — they have a stack trace, OS info, and nothing else.
- This build never reaches out to the internet *except* when you explicitly opt into Steam ingestion in the 📈 Steam Market tab. No telemetry, no update server, no license server.

---

## Heritage

Anvil shares its council infrastructure with two sibling branches in this repo:

- `main` — the original creative-tooling build (video / music / idea ops)
- `Work-Build` — the data-analysis pivot (CSV / Excel / vault analyst)

The shared core is the deliberation loop, the goal-anchor system, the CoderAgent ReAct loop, and the token-aware context manager. Anvil retargets the creative tooling toward game development.

---

## License (the file, not the AI thing)

Source is mine. If you have a copy, you have it because I gave it to you. Be cool about it.
