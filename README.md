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

> **First-time setup?** Pick whichever feels less foreign:
>
> **A. Cross-platform Python wizard (recommended for analysts):**
> ```
> python setup_wizard.py
> ```
> Detects your GPU, creates `./.venv`, installs the right CUDA wheels,
> walks the curated US-origin model catalog, downloads the GGUF, and
> wires up Tesseract if present. Nothing destructive runs before you
> confirm. Re-runnable. Add `--check-only` for a diagnostic-only pass.
>
> **B. Game-dev quick-start (smallest install):**
> ```bash
> conda create -n anvil python=3.11 -y
> conda activate anvil
> pip install -r requirements.txt
> # Get a GGUF from Hugging Face (qwen2.5-coder etc) and point at it.
> ```
>
> **B. One-command shell installer (conda-based):**
> ```bash
> # Windows native (PowerShell / cmd):
> setup.bat
>
> # Linux / macOS / WSL:
> ./setup.sh
> ```
> Probes hardware, picks the cu121 / cu124 / cu128 / cpu wheel tier,
> creates a conda env called `council`, installs torch + llama-cpp-python
> + the rest of the deps, and runs a post-install smoke test. ~15-25 min
> on a cold install (~2 GB of torch wheels). Idempotent.
>
> Flags for the shell installer:
> ```
> ./setup.sh --yes                 # accept all defaults
> ./setup.sh --cuda-tier cu124     # override auto-detect
> ./setup.sh --reinstall           # rebuild the env from scratch
> ./setup.sh --skip-install        # print the plan, don't run
> ```
>
> Neither path downloads a model on your behalf. After install, the
> in-app Tk wizard either reuses a `.gguf` it finds in `~/models`,
> `~/Downloads`, or `~/.cache/huggingface`, or shows the US-origin
> catalog with one-click HF download links. All recommended models are
> from US providers (IBM, Meta, Microsoft, Google, AllenAI, OpenAI).

> **Never used WSL or a Linux terminal before?** Skip ahead to
> [Appendix — WSL / Linux terminal crash course](#appendix--wsl--linux-terminal-crash-course)
> at the bottom of this README. Five minutes of background that makes
> every command in this section make sense. Come back here after.

Three supported paths. Pick the one for your machine.

| If you have                                | Use this path                                         |
|--------------------------------------------|-------------------------------------------------------|
| Windows 11 + NVIDIA GPU (recommended)      | [Running on WSL](#running-on-wsl-full-walkthrough)   |
| Windows native, NVIDIA GPU, want a `.exe`  | [Windows native](#windows-native) — `installs.txt` B/C/D |
| Native Linux (Ubuntu / Fedora / etc.)      | [Linux native](#linux-native) — `installs.txt` Section E |
| No NVIDIA GPU (any OS)                     | Same as your OS path above; CPU fallback is automatic |

---

### Running on WSL (full walkthrough)

This is the "from zero to chat" guide. Follow every step in order.
Total time on a clean Windows install: about 20-30 minutes (most of
that is the conda env + torch download).

You need a Windows 11 machine with an NVIDIA GPU. (Win 10 also
works but needs an X server — covered at the end.)

#### 1. Make sure WSL2 is installed

Open **PowerShell as Administrator** on the Windows side and run:

```powershell
wsl --install -d Ubuntu-22.04
wsl --update
```

If WSL was already installed, only `wsl --update` is needed.

Reboot if prompted. Then open **Ubuntu** from the Start menu and
sign in to the Linux user you just created.

> Already have WSL with a different distro? You can use Ubuntu-24.04
> or Debian instead — anything based on Ubuntu 22.04+ will work.
> Avoid Alpine; the wheels we depend on don't ship for musl libc.

#### 2. Verify the GPU is visible inside WSL

In the **Ubuntu** shell, run:

```bash
nvidia-smi
```

You should see a table showing your GPU and a "CUDA Version" entry
in the top right.

**If it says "command not found"** → your Windows NVIDIA driver is
either too old or wasn't installed with WSL support. Download the
latest **Game Ready Driver** for your card from
<https://www.nvidia.com/Download> on the Windows side, install it,
reboot, then run `wsl --shutdown` from PowerShell and reopen
Ubuntu. Try `nvidia-smi` again.

**If you don't have an NVIDIA GPU** — the setup script falls back
to CPU-only mode automatically. The app still works; just slower.

#### 3. Get the repo into WSL

Pick **one** of the two options. Both end up with `~/Council-Demo/`
on the Linux side and you `cd` into it.

**Option A — fresh clone (cleanest, recommended)**

```bash
cd ~
git clone https://github.com/Infernoplaystuf/Council.git Council-Demo
cd Council-Demo
git checkout Work-Build-App
```

**Option B — symlink an existing Windows copy**

If you already have the repo at e.g.
`C:\Users\you\Downloads\Council-Demo\`:

```bash
ln -s "/mnt/c/Users/$USER/Downloads/Council-Demo/Council-Demo" ~/Council-Demo
cd ~/Council-Demo
```

This works but the **first vault index rebuild will be slower** —
the WSL filesystem bridge to `/mnt/c/...` is about 10× slower than
the native Linux side for many-small-file walks. If your vault has
1000s of files, prefer Option A and copy the vault contents over
once instead of leaving them on the Windows drive.

#### 4. Run the setup script

```bash
./setup-wsl.sh
```

This does everything: apt installs (build tools, audio libraries),
Miniforge (conda), the `council` env, torch + llama-cpp-python with
the CUDA wheels matched to your GPU, and the rest of the Python
dependencies. **Takes about 15-25 minutes** the first time — most
of it is downloading the torch wheel (~2 GB).

The script is idempotent — if it fails partway through, just run
it again and it'll skip the parts that already worked.

If `./setup-wsl.sh` fails with **"Permission denied"**:

```bash
chmod +x setup-wsl.sh run-wsl.sh
./setup-wsl.sh
```

**What the setup picks for you** — the script reads `nvidia-smi`'s
reported max CUDA version and picks the matching wheel tier:

| Your driver shows  | Script picks                                |
|--------------------|---------------------------------------------|
| CUDA ≥ 12.8        | cu128 (RTX 50-series Blackwell)             |
| CUDA 12.4 – 12.7   | cu124 (RTX 40-series / Ada)                 |
| CUDA 12.0 – 12.3   | cu121 (RTX 20/30-series, A-series)          |
| No NVIDIA GPU      | cpu  (works, just slower)                   |

Override the auto-pick:

```bash
COUNCIL_CUDA_TIER=cu121 ./setup-wsl.sh   # force older wheels
```

**Verification at the end** — the last step prints something like:

```
  CUDA available: True
  device:         NVIDIA GeForce RTX 4080 SUPER
  torch.cuda:     12.4
  all imports OK
```

If `CUDA available: False` appears on a machine that *has* a GPU,
your Windows driver is older than the CUDA wheel needs. Update the
driver via the link in Step 2, run `wsl --shutdown` from
PowerShell, reopen Ubuntu, and re-run `./setup-wsl.sh`.

#### 5. Get a GGUF model onto the machine

The app needs a `.gguf` model file. If you don't have one, download
one from Hugging Face. Recommendations by GPU memory:

| VRAM          | Model file                                         | Size   |
|---------------|----------------------------------------------------|--------|
| 4-6 GB        | `bartowski/Llama-3.2-3B-Instruct-GGUF` (Q4_K_M)    | ~2 GB  |
| 8 GB          | `bartowski/granite-3.0-8b-instruct-GGUF` (Q4_K_M)  | ~5 GB  |
| 16 GB (4080)  | `bartowski/phi-4-GGUF` (Q4_K_M)                    | ~9 GB  |
| 24 GB+        | `bartowski/Qwen2.5-32B-Instruct-GGUF` (Q4_K_M)     | ~20 GB |

Easiest way to download once Hugging Face CLI is installed (it is —
`setup-wsl.sh` pulled `huggingface_hub`):

```bash
mkdir -p ~/models
cd ~/models
huggingface-cli download bartowski/phi-4-GGUF \
    phi-4-Q4_K_M.gguf --local-dir .
cd ~/Council-Demo
```

Or copy a model you already have on the Windows side:

```bash
mkdir -p ~/models
cp /mnt/c/Users/$USER/path/to/your-model.gguf ~/models/
```

> **Why copy instead of leaving it on /mnt/c?** Loading a 9 GB file
> across the WSL↔Windows bridge adds ~10 seconds to every launch.
> A native-Linux-side copy loads in 1-2 seconds.

#### 6. Run the app

```bash
./run-wsl.sh
```

That's it. The script:

- Activates the conda env.
- Auto-finds your `.gguf` model in `~/models/`, `~/Downloads/`,
  `./models/`, or `/mnt/c/Users/<you>/models/`.
- Sets sensible WSL defaults (UI scale 1.5× because WSLg reports
  96 DPI even on 4K displays, GPU offload on, ladder-debug logging
  on so you can see the n_ctx choice).
- Launches the GUI.

On **Windows 11** the Tkinter window opens through WSLg
automatically. On **Windows 10** see [Step 8 — common failures](#8-common-failures-and-fixes).

**Pinning a specific model or settings:**

```bash
COUNCIL_GGUF_PATH=~/models/granite-3.0-8b.gguf ./run-wsl.sh
COUNCIL_GGUF_GPU_LAYERS=0                      ./run-wsl.sh   # force CPU
COUNCIL_UI_SCALE=1.8                           ./run-wsl.sh   # bigger text
```

Make any of those stick across launches by adding to `~/.bashrc`:

```bash
echo 'export COUNCIL_GGUF_PATH=~/models/phi-4-Q4_K_M.gguf' >> ~/.bashrc
echo 'export COUNCIL_UI_SCALE=1.7'                         >> ~/.bashrc
source ~/.bashrc
```

#### 7. Confirm the GPU is actually being used

On launch, look at the terminal output for these lines:

```
[GGUF] n_ctx = 32,768  (source: VRAM-aware ...)
[CPU] features include avx=True avx2=True f16c=True ...
[GGUF] Loading phi-4-Q4_K_M.gguf (n_ctx=32768, n_threads=8, n_gpu_layers=99  GPU=NVIDIA GeForce RTX 4080 SUPER (16.0 GB VRAM))
```

The Tkinter window title bar also shows the chosen `n_ctx`:

```
Data's Inferno  ·  n_ctx=32,768
```

If you see `n_gpu_layers=0` and `GPU=none detected` on a machine
that has a GPU, the CUDA wheels didn't take. Re-run setup with the
right tier (Step 4) or follow the manual reinstall command in
`installs.txt`.

#### 8. Common failures and fixes

**"Illegal instruction (core dumped)" right at model load.**
The prebuilt llama-cpp-python wheel needs AVX2 + F16C from your CPU.
A few configurations (old Xeons, some Hyper-V nested VMs, certain
corporate Windows installs) end up without one of those flags
exposed inside WSL. The launcher catches this automatically — when
the app exits with SIGILL (code 132), it retries once on CPU and
points you at the real fix. For the actual fix (rebuilding
llama-cpp-python without AVX2), see `installs.txt`, the "Illegal
instruction (core dumped)" block.

**Tkinter window never appears (Windows 11).** WSLg might be
disabled. From a Windows PowerShell:

```powershell
wsl --version
```

You need WSL version 1.0.0 or newer. If older:

```powershell
wsl --update
wsl --shutdown
```

Then reopen Ubuntu and try `./run-wsl.sh` again.

**Tkinter window never appears (Windows 10).** Windows 10 doesn't
have WSLg. You need an X server on the Windows side.

1. Install **VcXsrv** (free): <https://sourceforge.net/projects/vcxsrv/>
2. Start `XLaunch`. Pick "Multiple windows", "Display number 0",
   "Start no client", and **check** "Disable access control".
3. The `run-wsl.sh` script auto-detects Win 10 and sets `DISPLAY`
   to the right value, so you should be able to just launch:

   ```bash
   ./run-wsl.sh
   ```

   If the window still doesn't appear, manually export `DISPLAY`:

   ```bash
   export DISPLAY=$(grep -m1 nameserver /etc/resolv.conf | awk '{print $2}'):0
   ./run-wsl.sh
   ```

**"torch.cuda.is_available() = False" but nvidia-smi works.**
Windows host NVIDIA driver is older than the CUDA wheel needs:

| Wheel    | Needs Windows driver |
|----------|----------------------|
| cu121    | 525+                 |
| cu124    | 545+                 |
| cu128    | 570+                 |

Update the driver, `wsl --shutdown` from PowerShell, reopen Ubuntu.
You do **not** need to re-run `./setup-wsl.sh` — only the driver
changed.

**"OSError: libcuda.so.1: cannot open shared object file".** Your
WSL distro is older than 22.04 and doesn't expose libcuda cleanly.
Either upgrade in-place (`sudo do-release-upgrade`) or install
Ubuntu-24.04 side-by-side from PowerShell:

```powershell
wsl --install -d Ubuntu-24.04
```

**"paramiko / cryptography build fails".** Missing build tools —
`./setup-wsl.sh` Step 1 didn't run cleanly. Re-run:

```bash
sudo apt install -y build-essential
./setup-wsl.sh
```

**App starts but the vault is empty.** The app shows your `vault/`
folder contents. By default it's `~/Council-Demo/vault/`. Either
drop files into `~/Council-Demo/vault/data_in/` on the Linux side,
or symlink an existing Windows-side vault:

```bash
mv ~/Council-Demo/vault ~/Council-Demo/vault.empty
ln -s "/mnt/c/Users/$USER/Documents/MyVault" ~/Council-Demo/vault
```

The first index rebuild over the `/mnt/c` bridge will be slow.

#### 9. Diagnostic capture

If something breaks that isn't covered above, the engine's startup
log + the n_ctx ladder dump + the CPU feature line together cover
almost every failure mode. Capture them with:

```bash
COUNCIL_GGUF_N_CTX_DEBUG=1 ./run-wsl.sh 2>&1 | tee ~/council-launch.log
```

— then look at `~/council-launch.log` for the diagnostic.

---

### Windows native

The full per-GPU breakdown lives in `installs.txt`:

- **Section A** — CPU only (no NVIDIA GPU)
- **Section B** — RTX 20/30-series, A-series workstation (cu121)
- **Section C** — RTX 40-series / Ada (cu124)
- **Section D** — RTX 50-series Blackwell (cu128 nightly)

Each section is a self-contained `conda create … && pip install …`
block. Pick the one that matches your card, run every line in
order. After that:

```bat
set COUNCIL_GGUF_PATH=C:\models\phi-4-Q4_K_M.gguf
python council_gui_engine.py
```

First launch shows a small setup wizard that confirms a GGUF model is selected and (for Godot users) asks where your Godot 4 binary lives. After that you're done.

For a packaged `.exe`:

```bat
pip install pyinstaller
build.bat
:: Look in dist\Anvil\Anvil.exe
```

#### What the installer does NOT do

- It doesn't auto-install conda on Windows — the silent installer is unreliable. If you don't have conda, `setup.bat` prints a Miniforge download link and exits cleanly; install conda, re-run.
- It doesn't download a model for you. After install, the in-app wizard either reuses a GGUF file it finds in `~/models`, `~/Downloads`, or `~/.cache/huggingface`, or shows you a list of US-made recommendations with one-click HF download links.
- It doesn't run as administrator. If your conda lives in a system-wide location with restricted write perms, install it user-local first (Miniforge installs into `~/miniforge3` by default).

#### Manual install

If you prefer to drive conda + pip yourself, `installs.txt` has the full per-GPU breakdown (Sections A-F covering CPU, RTX 20/30, RTX 40, RTX 50, Linux with dream3dnx, and WSL without).

---

### Linux native

Section E in `installs.txt` covers native Linux with full
dream3dnx pipeline execution. If you don't need dream3dnx and
just want the GPU-accelerated Council, the WSL walkthrough above
works on bare Linux too (skip the WSL-specific bits — the apt
prereqs, Miniforge install, and conda env steps are identical).

---

## The tabs

| Tab | What it's for |
|---|---|
| **⚖ Council** | The chat. Ask anything. The Judge picks a panel and they deliberate. |
| **🛠 Godot Workspace** | The IDE. File tree, GDScript editor, scene-tree view, Run/Validate buttons, console panel. The edit-test-visualise loop lives here. **📄 Build from GDD button** — paste a markdown Game Design Document, review the parsed plan (entities, scenes, scripts, signal contracts, autoloads), click Build to generate a runnable project with ColorRect+Label placeholders ready for hand-painted sprites from the 🎨 Pixel Art tab. *(Phase C-lite + GDD pipeline)* |
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

## Pick a model

Council ships with a curated catalog of US-origin GGUF models — Anthropic
isn't open-weight, so the runtime defaults to other US labs (IBM, Meta,
Microsoft, Google, AllenAI). The catalog is the single source of truth used
by `setup_wizard.py`, the in-GUI onboarding wizard, and these docs.

| ID | Name | Org | Size | Ctx | VRAM (Q4) | License | Notes |
|---|---|---|---|---|---|---|---|
| `granite-3.1-8b-q4` | IBM Granite 3.1 8B Instruct (Q4_K_M) | IBM | 4.9 GB | 128K | 6.5 GB | Apache-2.0 | **Default.** Solid general baseline. Conservative refusals; good with tabular data. |
| `llama-3.1-8b-q5` | Meta Llama 3.1 8B Instruct (Q5_K_M) | Meta | 5.7 GB | 128K | 7.0 GB | Llama 3.1 Community License | Long-context generalist. Best for big folder dumps / multi-CSV context. |
| `llama-3.2-3b-q5` | Meta Llama 3.2 3B Instruct (Q5_K_M) | Meta | 2.4 GB | 128K | 3.2 GB | Llama 3.2 Community License | Fast, fits 8 GB cards comfortably. Good for routing / classification. |
| `llama-3.2-1b-q8` | Meta Llama 3.2 1B Instruct (Q8_0) | Meta | 1.3 GB | 128K | 1.8 GB | Llama 3.2 Community License | Tiny. CPU-friendly. Use for quick demos or constrained boxes. |
| `phi-4-q4` | Microsoft Phi-4 14B (Q4_K_M) | Microsoft | 8.9 GB | 16K | 11.0 GB | MIT | Best reasoning at this VRAM tier. Pick for analyst Q&A on dense data. |
| `phi-3.5-mini-q5` | Microsoft Phi-3.5-mini Instruct (Q5_K_M) | Microsoft | 2.8 GB | 128K | 3.6 GB | MIT | Compact, strong reasoning. Great middle-ground for 8 GB VRAM. |
| `gemma-2-9b-q4` | Google Gemma 2 9B Instruct (Q4_K_M) | Google | 5.8 GB | 8K | 7.5 GB | Gemma Terms of Use | Polished prose; helpful for write-ups. Short native context. |
| `granite-3.1-8b-code-q4` | IBM Granite 3.1 8B Code Instruct (Q4_K_M) | IBM | 4.9 GB | 128K | 6.5 GB | Apache-2.0 | Code-specialised IBM model. Wire into the Coder role if you have RAM headroom. |
| `olmo-2-13b-q4` | AllenAI OLMo 2 13B Instruct (Q4_K_M) | AllenAI | 8.4 GB | 4K | 10.5 GB | Apache-2.0 | Fully open weights + training data (Allen Institute). Pick for transparency. |

VRAM column is approximate at the listed quant + 4K context with ~1.5 GB
headroom for the CUDA driver and the Tkinter UI. On a 16 GB card (RTX
4080 / 5080) any of these run comfortably; on an 8 GB card stick to the
Llama-3.2-3B, Phi-3.5-mini, or the Llama-3.2-1B (CPU-fine too).

Pick one via the wizard:

```bash
python setup_wizard.py --model phi-4-q4   # preselect by id, or omit to choose
```

…or download manually then point Council at the file:

```bash
python -c "from huggingface_hub import hf_hub_download as h; h(repo_id='bartowski/phi-4-GGUF', filename='phi-4-Q4_K_M.gguf', local_dir='./models')"
export COUNCIL_GGUF_PATH=./models/phi-4-Q4_K_M.gguf
```

The same catalog renders in the in-GUI onboarding wizard with **Open**
(goes to Hugging Face) and **⬇ Auto-download** buttons per row.

---

## Optional bits

- **Tesseract OCR** — entirely optional. When installed, the vault
  index extracts text from your image files (defect photos, scanned
  forms, dashboard screenshots) and makes it searchable. When **not**
  installed, image *filenames* are still searchable; only the pixel
  content stays opaque. Install via `winget install UB-Mannheim.TesseractOCR`
  on Windows or `sudo apt install tesseract-ocr` on Debian/Ubuntu, or
  skip entirely — the wizard step is informational only and never
  blocks you.
- **CLIP semantic image search** — automatically available when
  `sentence-transformers` is installed (which it is, after the wizard).
  Encodes every image into a 512-dim vector on first run so you can
  search by visual concept ("solder joint crack", "machine nameplate
  close-up") instead of by exact filename.
- **SQL connector** — `sql_connector.py` is a SELECT-only SQLAlchemy
  bridge for warehouse data (SQLite out of the box; Postgres / MySQL /
  MSSQL / Snowflake URL templates documented in the module header).

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

- Models swap = slow. By default every personality runs on the same GGUF model so the GPU keeps one model hot. If you have 32 GB+ RAM and want to mix in a code-specialist model for the Coder role, edit `vault/personality_backends.json` (each entry pins a separate `COUNCIL_GGUF_PATH`).
- The IDE Runner has a trust gate that flags risky operations (subprocess, eval, file delete, raw sockets, etc.) before running generated code. Annoying once or twice; saves your bacon eventually.
- The Godot Workspace Run button shells out to `godot --path <project>` — Anvil doesn't bundle Godot. Onboarding asks for the binary path on first launch.
- Crash logs land in `vault/logs/crashes/` — they have a stack trace, OS info, and nothing else. Send them to me if something keeps blowing up.
- This build never reaches out to the internet for inference — the GGUF runtime is local. No telemetry, no update server, no license server. (Optional `huggingface_hub` downloads only happen when you click *Download model* in the UI; opt-in Steam ingestion in the 📈 Steam Market tab is the one feature that explicitly calls out to the internet.)

---

## Appendix — WSL / Linux terminal crash course

If you've never opened a Linux terminal before, here's the
five-minute version of everything you need to follow the WSL
walkthrough above. Read it once; you'll probably never need this
section again.

### What you see when WSL opens

Click **Ubuntu** in the Start menu and a black window appears with
a line that looks like:

```
yourname@DESKTOP-ABC123:~$
```

That's the **shell prompt**. Breaking it down:

- `yourname` — your Linux username (you picked it on first launch).
- `DESKTOP-ABC123` — your Windows machine name.
- `~` — your current location. `~` is shorthand for your home
  folder, `/home/yourname`. It changes as you move around.
- `$` — the "ready for a command" signal. When you type, the
  cursor sits to the right of it.

You type a command, press Enter, output appears, the prompt comes
back, repeat. That's all a terminal does.

### Five commands that cover 95 % of what you'll need

| Command         | What it does                                         |
|-----------------|------------------------------------------------------|
| `pwd`           | **P**rint **w**orking **d**irectory — "where am I right now?" |
| `ls`            | **L**i**s**t the files in the current folder        |
| `ls -la`        | Same, but show hidden files + sizes + permissions   |
| `cd foldername` | **C**hange **d**irectory — go into `foldername`     |
| `cd ..`         | Go up one folder                                    |
| `cd ~`          | Jump back to your home folder                       |
| `cd -`          | Jump back to the previous folder you were in        |
| `cat filename`  | Print a text file to the screen                     |
| `nano filename` | Edit a text file (Ctrl-X to exit, Y to save)        |

Example walkthrough — open Ubuntu, then type each of these:

```bash
pwd                    # prints /home/yourname
ls                     # lists files in your home folder
mkdir test             # makes a folder called "test"
cd test                # goes into it
pwd                    # prints /home/yourname/test
cd ..                  # back to home
rmdir test             # removes the empty folder
```

### Paths — Linux vs Windows

Linux uses forward slashes `/`, Windows uses backslashes `\`.
Inside WSL you always use forward slashes.

| Where it lives                     | How you refer to it in WSL              |
|------------------------------------|-----------------------------------------|
| Your Linux home folder             | `~`  or  `/home/yourname`               |
| `C:\Users\you\Documents` (Windows) | `/mnt/c/Users/you/Documents`            |
| `D:\stuff` (Windows D drive)       | `/mnt/d/stuff`                          |
| The current folder                 | `.`  (a single dot)                     |
| One folder up                      | `..`                                    |

So when the WSL walkthrough says "put your model at `~/models/`" —
that maps to `/home/yourname/models/` on the Linux side, which is
*separate* from anything on your Windows drives. When it says
`/mnt/c/Users/you/...` — that's reaching across into your real
Windows files.

### Tab completion (the time-saver)

Press **Tab** while typing a file or folder name and it
auto-completes. Hit Tab twice to see all the options when there
are several. So instead of typing `cd Council-Demo`, you can type
`cd Coun<Tab>` and it fills in the rest.

### Hot keys you'll use

| Keys             | What it does                                       |
|------------------|----------------------------------------------------|
| **Ctrl+C**       | Stop a running command (e.g. cancel a download)    |
| **Up arrow**     | Recall the previous command                        |
| **Down arrow**   | Forward through your command history               |
| **Ctrl+L**       | Clear the screen (same as typing `clear`)          |
| **Ctrl+Shift+C** | Copy selected text (Linux terminals don't use plain Ctrl+C) |
| **Ctrl+Shift+V** | Paste                                              |
| **Right-click**  | Also pastes in the Ubuntu/WSL terminal             |

### Running a script

When the walkthrough says `./setup-wsl.sh`, the `./` part means
"the file in this current folder". You have to be **in the right
folder** for that to work:

```bash
cd ~/Council-Demo      # go to the repo folder first
./setup-wsl.sh         # then run the script
```

If you see `bash: ./setup-wsl.sh: No such file or directory`, you
ran it from the wrong place — `cd ~/Council-Demo` and try again.

If you see `bash: ./setup-wsl.sh: Permission denied`, the file
isn't marked as runnable. Fix:

```bash
chmod +x setup-wsl.sh run-wsl.sh
./setup-wsl.sh
```

### Copying files between Windows and WSL

**From Windows to WSL (one-off):**
```bash
cp /mnt/c/Users/$USER/Downloads/some-model.gguf ~/models/
```
`$USER` automatically fills in your Windows username if it matches
your WSL one (often does). Otherwise spell it out.

**From WSL to Windows:**
```bash
cp ~/some-output.csv /mnt/c/Users/$USER/Desktop/
```

**Open the current WSL folder in Windows Explorer:**
```bash
explorer.exe .
```
The Windows Explorer window opens at your current Linux folder,
exposed via a special `\\wsl.localhost\Ubuntu-22.04\...` path.
You can drag-and-drop files in and out of it like normal.

### Editing a file

If a step says "edit `~/.bashrc`", the easiest editor is `nano`:

```bash
nano ~/.bashrc
```

- Arrow keys move the cursor.
- Type to insert.
- **Ctrl+O** then **Enter** to save.
- **Ctrl+X** to quit.

`nano`'s commands are listed at the bottom of its window in
shorthand (`^O` = Ctrl+O, `^X` = Ctrl+X, etc.).

### Closing and reopening WSL

- **Close the Ubuntu window** — anything still running keeps
  running in the background. To reopen, click Ubuntu in the Start
  menu again; you land back in your home folder.
- **Open a second Ubuntu window** — click Ubuntu in the Start
  menu again while the first one is still open. Each window is
  an independent shell; they share the same files.
- **Fully shut down WSL** — from a Windows PowerShell, run
  `wsl --shutdown`. The next time you open Ubuntu it's a fresh
  start (useful when WSL or the GPU passthrough gets into a weird
  state).

### Reading a wall of output

When a command prints more than fits the screen, pipe it to
`less`:

```bash
ls -la /usr/bin | less
```

- **Space** — next page.
- **b** — back one page.
- **/searchterm** — search.
- **q** — quit.

### "Where did the prompt go?"

Sometimes a command runs for a long time (downloads, the
`setup-wsl.sh` install) and the terminal seems frozen. That's
normal — it's just busy. Wait it out. If you genuinely need to
stop it: **Ctrl+C**.

### That's it

Everything in the WSL walkthrough above reduces to:

1. Open Ubuntu.
2. `cd ~/Council-Demo` to get to the right folder.
3. Run the commands the step tells you to run.
4. Watch the output, scroll up if you need to re-read it.

If a step says to run a command and you don't recognise the
command itself, paste the whole step (command + the explanation
around it) into a chat with the AI — the app is literally built
to answer "what does this command do?".

---

## Heritage

This branch (`odysseus-council`) merges the work from several sibling branches:

- `main` — the original creative-tooling build (video / music / idea ops)
- `Work-Build` — the data-analysis pivot (CSV / Excel / vault analyst)
- `game-dev` (Anvil) — Godot game-development workshop (workspace, GDD pipeline, simulations, pixel art)

The shared core is the deliberation loop, the goal-anchor system, the CoderAgent ReAct loop, and the token-aware context manager. Different tabs surface different "modes" of the same council.

---

## License (the file, not the AI thing)

Source is mine. If you have a copy, you have it because I gave it to you. Be cool about it.
