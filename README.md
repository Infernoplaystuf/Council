# Data's Inferno

A little workspace I built so I can ask AI questions about my own data.

Drop a CSV on the Grapher. Ask the Council a question. A panel of AI specialists deliberates and tells you what they see — every step is visible so you can poke at the reasoning. Nothing leaves the machine.

This is the home build. No licensing, no trials, no telemetry, no phone-home. If you got a copy from me, just run it.

---

## What it does well

- **Q&A on your data.** "Which customers haven't ordered in 90 days?" — point it at a CSV, get an answer with citations.
- **Auto-charts.** Drop a file in the Grapher, click *Ask Analyst*, get a sensible chart for the data you have.
- **Cross-file lookups.** "Find C1234" or "Tell me about Sarah Smith" — scans every CSV/JSON in the vault, surfaces matching rows from each file, and detects which files share columns (so you can spot foreign-key links automatically). Click 🔍 *Look Up* in the Council, or just type a lookup-shaped question.
- **Multi-AI deliberation.** Several AI personalities argue in front of you so you see disagreement, not a single confident answer that might be wrong.
- **Domain lenses.** I made *Personal Specialists* — Sales, Inventory, Customer — that get auto-summoned when a question matches their area. They share the same data pool so cross-domain questions work.

## What it's bad at

- Anything that needs the latest internet info (it's offline by design).
- Truly massive datasets — the in-memory Grapher chokes past a few hundred thousand rows.
- Models smaller than ~7B can be quite literal; bump up to 14B if a laptop will tolerate it.

---

## How to run it

The one-command install runs the same way on every supported OS. After cloning the repo, from the project root:

```bash
# Windows native (PowerShell or cmd):
setup.bat

# Linux, macOS, WSL:
./setup.sh
```

That probes your hardware, picks the right CUDA wheels for your GPU (cu121 / cu124 / cu128 / cpu), creates a conda env named `council`, installs torch + llama-cpp-python + the rest of the Python deps, and runs a smoke test to confirm everything works. Takes 15-25 minutes on a cold start (~2 GB of torch wheels to download). Re-running is safe — the script is idempotent.

After install, activate the env and launch:

```bash
conda activate council
python council_gui_engine.py
```

First launch opens the in-app wizard which detects your hardware again, looks for a previous install we can reuse, recommends a model size based on your VRAM, and offers a checkbox to switch between text-only and vision-capable models. All recommended models are from US-based providers (Meta, Microsoft, IBM, Google).

### Setup options

```bash
./setup.sh --yes                 # accept all defaults, no prompts
./setup.sh --cuda-tier cu124     # override auto-detection
./setup.sh --reinstall           # blow away the existing env
./setup.sh --skip-install        # print the plan but don't execute
./setup.sh --skip-smoke          # skip the post-install verification
./setup.sh --help                # full list
```

### What the installer does NOT do

- It doesn't auto-install conda on Windows — the silent installer is unreliable. If you don't have conda, the script prints a Miniforge download link and exits cleanly; install conda, re-run `setup.bat`.
- It doesn't download a model for you. After install, the in-app wizard either reuses a GGUF file it finds in `~/models`, `~/Downloads`, or `~/.cache/huggingface`, or shows you a list of US-made recommendations with one-click HF download links.
- It doesn't run as administrator. If your conda lives in a system-wide location with restricted write perms, install it user-local first (Miniforge installs into `~/miniforge3` by default).

### Manual install

If you prefer to drive conda + pip yourself, `installs.txt` has the full per-GPU breakdown (Sections A-F covering CPU, RTX 20/30, RTX 40, RTX 50, Linux with dream3dnx, and WSL without).

### WSL specifically

The setup workflow on WSL is identical to native Linux — `./setup.sh` works. There's also a WSL-specialised `./setup-wsl.sh` script with extra apt prereqs (build-essential, portaudio, libgl1) the cross-platform `setup.sh` doesn't touch. See the [Running on WSL](#running-on-wsl-full-walkthrough) section below for the full walkthrough including Windows host setup, GPU passthrough verification, and WSLg display handling.

### Building the bundled `.exe`

```bash
pip install pyinstaller
build.bat        # Windows
./build.sh       # macOS / Linux
# Look in dist/DatasInferno/
```

---

## The tabs

| Tab | What it's for |
|---|---|
| **⚖ Council** | The chat. Ask anything. The Judge picks a panel and they deliberate. |
| **📊 Grapher** | Drop a file, get charts. Auto-detects column types. The 📦 *Sample* button loads bundled fake data if you don't have anything handy. |
| **🎓 Specialists** | Edit/create the named lenses. Three pre-built (Sales, Inventory, Customer). Each is a system-prompt overlay over a base personality. |
| **🔍 Lens** | Paste an answer, pick which roles should review it in parallel. Useful when you don't fully trust what the Council just told you. |
| **🕓 Sessions** | Every past chat. Searchable. Click any to load. |
| **🗄 Vault** | The shared data pool. Drop CSVs / docs / cloned repos in here for the Sage to index. |
| **🎙 Speech** | Record audio → transcribe → feed to Council. Also reads text aloud. |

There are six more "advanced" tabs (IDE, Agents, Nodes, Apothecary, etc.) that are hidden by default. Set `COUNCIL_ADVANCED=1` before launching to see them.

---

## A typical session

```
1. Launch
2. Drop my orders.csv into the Grapher → schema autodetects
3. 📊 Council: "Which months had the biggest revenue swing?"
   Sales Specialist auto-summons. Council finds my orders.csv,
   loads it into the Grapher, asks the Analyst for a chart.
4. Tweak the chart axes manually until it tells me what I want
5. 🔍 Lens: paste the verdict, ask Skeptic + Algorithm to review
6. Done — sessions auto-save in the 🕓 Sessions tab
```

---

## Personal Specialists

Three lenses ship with it:

- **💰 Sales Specialist** — revenue, AOV, retention, churn
- **📦 Inventory Specialist** — stock, turnover, dead stock, suppliers
- **🤝 Customer Specialist** — loyalty, dormancy, segmentation

When you ask a question that mentions one of their domain keywords, that specialist gets auto-summoned. Ask something that spans multiple domains ("buy enough stock based on last year's sales") and *both* specialists run in parallel — the Judge synthesises.

You can edit them in the 🎓 Specialists tab. Each one is just config: name, icon, description, keywords, system-prompt overlay, and which base personality wears the lens. There are no separate per-specialist data folders — everything lives in the shared vault, which is the point.

---

## Where the data lives

```
vault/
├── data_in/                ← MY DATA  (read-only by the app)
│   └── *.csv / *.json      drop input files here
├── data_out/               ← APP OUTPUTS  (charts, exports, joins)
│   ├── charts/
│   └── exports/
├── conversations/          one JSONL per session  (app internal)
├── memory/                 per-personality persistent notes (app internal)
├── logs/                   council.log + crash logs
├── workspace/              code-runner scratch files
├── .chromadb/              vector index (RAG memory)
├── .git_clones/            cloned reference repos
└── specialists.json        the specialist registry
```

The hard rule: **input data is never overwritten**.

- `data_in/` is the single read source for the data search / lookup /
  Find-and-Chart pipeline. The app reads from here and never writes
  back. Drop your CSVs in this folder.
- `data_out/` is the only place those features write to. Every chart
  export, joined dataset, or derived CSV lands here. You can wipe it
  any time without losing originals.
- `bundled samples` (`assets/sample_data/`) are also read as inputs —
  same read-only contract, but they live with the app rather than in
  the vault.

The split is enforced in code: the `DataIndex` constructor refuses to
instantiate if its read paths overlap with its write path, and
`safe_write_path()` refuses to produce a path that would land inside
`data_in/`.

All of `vault/` is gitignored. Delete the folder to reset.

The 🎓 Specialists tab has **📂 Open data_in folder** and **📤 Open
data_out folder** buttons that pop the OS file manager so I never have
to hunt for them.

---

## If something seems stuck after a `git pull`

Python caches compiled bytecode in `__pycache__/`. If the cache somehow ends up newer than the source (rare, but happens with certain git operations or editor mtime quirks), Python uses the cached bytecode and your fix doesn't take effect. The launcher auto-purges stale caches on startup — but if you want to force-clean:

```bash
python clean.py
```

Then relaunch.

---

## Things to know

- Models swap = slow. By default every personality runs on the same Ollama model so the GPU/RAM keeps one model hot. If you have 32 GB+ and want to mix in a code-specialist model for the Coder role, edit `vault/personality_backends.json`.
- The IDE Runner has a trust gate that flags risky operations (subprocess, eval, file delete, raw sockets, etc.) before running generated code. Annoying once or twice; saves your bacon eventually.
- Crash logs land in `vault/logs/crashes/` — they have a stack trace, OS info, and nothing else. Send them to me if something keeps blowing up.
- This build never reaches out to the internet apart from talking to your local Ollama. No telemetry, no update server, no license server.

---

## License (the file, not the AI thing)

Source is mine. If you have a copy, you have it because I gave it to you. Be cool about it.
