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

You'll need Python 3.11 and [Ollama](https://ollama.com).

```bash
# 1. Set up a Python env
conda create -n council python=3.11 -y
conda activate council
pip install -r requirements.txt

# 2. Install Ollama (https://ollama.com), then pull a model
ollama pull qwen2.5:14b-instruct-q4_K_M

# 3. Launch
python council_gui_engine.py
```

First launch shows a small setup wizard that confirms Ollama is running and points at the right model. After that you're done.

If you want the bundled `.exe` instead of running from source:

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
├── conversations/          one JSONL per session
├── memory/                 per-personality persistent notes
├── logs/                   council.log + crash logs
├── workspace/              code-runner scratch files
├── graph_output/           Grapher exports
├── .chromadb/              vector index (RAG memory)
├── .git_clones/            cloned reference repos
└── specialists.json        the specialist registry
```

All gitignored. Delete the folder to reset.

---

## Things to know

- Models swap = slow. By default every personality runs on the same Ollama model so the GPU/RAM keeps one model hot. If you have 32 GB+ and want to mix in a code-specialist model for the Coder role, edit `vault/personality_backends.json`.
- The IDE Runner has a trust gate that flags risky operations (subprocess, eval, file delete, raw sockets, etc.) before running generated code. Annoying once or twice; saves your bacon eventually.
- Crash logs land in `vault/logs/crashes/` — they have a stack trace, OS info, and nothing else. Send them to me if something keeps blowing up.
- This build never reaches out to the internet apart from talking to your local Ollama. No telemetry, no update server, no license server.

---

## License (the file, not the AI thing)

Source is mine. If you have a copy, you have it because I gave it to you. Be cool about it.
