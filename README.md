# Council

A multi-personality AI workspace that runs entirely on your own machine. Pose a question, get a structured deliberation between specialised AI roles (writer, coder, intern, peasant, judge, etc.), and walk away with a verdict you can trust because every step is visible.

Council is built for solo professionals, small teams, and anyone who wants the speed of "just ask an AI" without giving up auditability or sending data off-machine.

---

## Quick start

### 1. Install Python 3.11

The recommended path is [Anaconda](https://www.anaconda.com/) or `miniconda`:

```bash
conda create -n council python=3.11 -y
conda activate council
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

Optional extras:

| Feature                | Install                                   |
|------------------------|-------------------------------------------|
| SSH compute nodes      | `pip install paramiko`                    |
| Microphone input       | `pip install sounddevice soundfile`       |
| Local speech-to-text   | `pip install faster-whisper`              |
| Text-to-speech         | `pip install pyttsx3`                     |
| Vault search (RAG)     | `pip install chromadb sentence-transformers` |

### 3. Install [Ollama](https://ollama.com) and pull a model

```bash
ollama pull qwen2.5:14b
ollama pull qwen2.5-coder:14b
```

The defaults in `personality_backends.json` expect Ollama on `localhost:11434`. You can change which model each personality uses in that file.

### 4. Launch

```bash
python council_gui_engine.py
```

The first launch creates `vault/` next to the script — that's where every piece of data lives. Nothing is ever sent off-machine unless you explicitly enable a remote node.

---

## How the Council works

When you ask a question, the **Judge** personality first decides which "route" the question belongs to (chat, code, planning, content, etc.). It then convenes a small panel of three or four specialists from the available personalities, each of whom drafts an answer. The Judge ranks them, the **Peasant** cross-examines weak points, and the panel iterates until either consensus is reached or a maximum number of rounds is hit. The transcript of the entire deliberation is shown live in the **⚖ Council** tab.

You see:
- Every personality's individual draft
- The Judge's ranking and reasoning
- Peasant follow-up questions
- The final verdict (PASS / RETRY / CHANGE)
- Confidence score (0-100)

Every session is saved automatically.

---

## The tabs

| Tab | What it's for |
|-----|---------------|
| **⚖ Council** | Main chat. Type a question → watch the panel deliberate → get a verdict. |
| **💻 IDE / Runner** | A built-in Python editor. The coder personality writes code here; you can edit and run it. Output streams back into the Council. |
| **📚 Librarian** | Browse the vault, see what files have been saved from past sessions, commit recent work to a git repo. |
| **🕓 Sessions** | Every prior conversation, searchable. Click one to load its full transcript. |
| **🖥 Nodes** | Optional: register remote machines (over SSH) that can run heavier models. The Council will offload work to them when configured. |
| **🤖 Agents** | Live status of the autonomous agents (Coder, Intern, Vault, Sage, RAG). Pause, resume, or watch their event stream. |
| **📊 Grapher** | Drop in any CSV / XLSX / JSON / NPY file and chart it. Includes a built-in **Analyst** AI that suggests appropriate plots, transforms, and explains the data in plain English. |
| **🗄 Vault** | Tree view of the vault directory. Clone external git repos into the vault for the RAG to learn from. |
| **🔍 Lens** | Pick any subset of personalities and have them all critique the same piece of text in parallel. Use it for second opinions. |
| **🗄 Vault Health** | Inspect what each personality "remembers", what knowledge gaps the Sage has flagged, and the wishlist of topics the Council wants to learn. |
| **🎙 Speech** | Record audio → transcribe → feed into the Council. Also reads any text aloud via local TTS. |
| **🔧 Apothecary** | Utility console: cache management, log inspection, model health checks, pin overrides. |

---

## Configuring personalities

Each personality (writer, coder, intern, judge, peasant, artist, …) is bound to a model backend. Two files control the bindings:

### `personality_backends.json`
Maps each role to a backend key:
```json
{
  "writer":  "local_general_primary",
  "coder":   "local_coder_primary",
  "intern":  "local_coder_fast",
  "judge":   "local_judge_fast",
  "peasant": "local_peasant_fast",
  "artist":  "local_general_alt"
}
```

### `personality_config.yaml`
A higher-level mapping:
```yaml
personalities:
  judge:   openai_gpt4o_mini
  writer:  openai_gpt4o
  intern:  ollama_qwen_coder
  coder:   ollama_qwen_coder
  peasant: openai_gpt4o_mini
  artist:  openai_gpt4o_mini
```

You can mix-and-match local and cloud backends per personality. Settings are hot-reloaded — edit the file while the Council is running and the next deliberation picks up the changes.

---

## The vault

Everything Council learns or generates lives under `vault/`:

```
vault/
├── conversations/          one JSON file per session
├── memory/                 per-personality persistent notes
├── logs/                   council.log + per-session logs
├── workspace/              code-runner scratch files
├── graph_output/           Grapher exports
├── .chromadb/              vector index (RAG memory)
├── .git_clones/            cloned reference repos
├── node_registry.json      SSH compute node list
└── personality_backends.json   model pins
```

The vault is git-ignored by default. You can change that in `.gitignore` if you want to track conversation history, but be careful — these files often contain things you don't want public.

---

## Tips

- **Type slower, not faster.** The first message of a session sets the tone. Spend a sentence telling the Judge what kind of help you need ("explain like I'm new" / "write production code" / "review this critically").
- **Use the IDE tab as a scratch pad.** When the coder produces something, edit it inline before running. The Council learns from edits.
- **Check Vault Health weekly.** It surfaces topics the Sage has flagged as gaps in the council's knowledge — feeding it more reference material there pays dividends.
- **The Lens is for second opinions.** When the main council reaches a verdict you're unsure about, paste the answer into the Lens and have a different combination of personalities tear it apart.
- **Pin your judge.** The Judge is the personality you'll notice most. Pin it to your most reliable backend in `personality_backends.json`.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| App won't start | Missing required personality in config | Check `personality_backends.json` includes all six required roles: judge, writer, peasant, intern, coder, artist |
| Models time out | Ollama not running, or model not pulled | `ollama list` to verify; `ollama pull <model>` if missing |
| `git pull` hangs | Remote unreachable | Cancel and check the Nodes tab; pulls have a 2-min timeout but a stuck repo will retry |
| Empty Vault Health | First-run or vault was deleted | Run a few deliberations and the memory will populate |
| RAG returns nothing | ChromaDB not installed | `pip install chromadb sentence-transformers` |

---

## What's not in this release

This is the focused commercial release. The following experimental features are not included; they live on a separate development branch:

- Music composer (prompt → MIDI/MusicXML)
- Video analysis & auto-edit pipeline
- Presentation script generator
- Continuous overnight idea generator with thumbnail rendering

These were removed to keep the install small, the dependency list short, and the UI focused.

---

## License

See `LICENSE` for terms. Council bundles no third-party model weights — you bring your own via Ollama or your preferred backend.
