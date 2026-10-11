# Pi node test — setup instructions for a Claude Code session

Hand this file to the Claude Code session that will set up the test. It
assumes that session runs **on the Council PC**, inside a checkout of this
repository, and can reach the Raspberry Pis on the network (by IP, and by SSH
if you give it access). Follow the steps in order; each ends with a check.
Stop and ask the user whenever a step needs a decision, a password, or
anything on the Pis you have not been told you may change.

## The branches

| What | Repository | Branch | Commit at hand-off |
|---|---|---|---|
| The AI Council, including the node work (Machines & roles routing, Nodes tab, Council Map, the Quant Duel tab) | `Infernoplaystuf/Council` | **`council-map`** (cut from `qt-migration`, not merged) | `9dbc5b2` |
| The Quant Duel (paper-trading experiment) | `Infernoplaystuf/Council` | **`quant-duel`** (its own history; no Council code) | `4c72f61` |

Never push to `main` or `Work-Build`. Read `CLAUDE.md` first; it applies
to everything here. In particular:
- Offline by design: localhost only unless the user opts in.
- Never delete or overwrite user data, including vault files.
- The Council never downloads models itself.
- Only recommend US-origin models: Llama, Phi, Gemma, Granite, OLMo, gpt-oss.
- Verify offscreen: `QT_QPA_PLATFORM=offscreen`, `COUNCIL_NO_DIALOGS=1`.

## How the pieces fit

- **Council nodes are machines running Ollama.** The Council PC sends a
  *role's* model calls to a machine when, in *Machines & roles*
  (`<vault>/node_routing.json`), all three hold:
  - routing is on;
  - the machine is enabled;
  - the role is bound to it.
  
  Each machine has a call limit (`parallel`) and a cooldown. A failing
  machine rests for 30 s, doubling up to 5 min. While it rests, the role
  answers on this PC (fallback `here`) or errors (fallback `fail`).
  Code: `council_core/node_routing.py`.
- **The Quant Duel runs on the Council PC** as separate processes (both duel
  nodes, A = news, B = no news). Its LLM work goes wherever the **`quant`**
  role is bound: news scoring, the weekly tuning round, report summaries.
  It follows the same call limit, cooldown and fallback rules. Code:
  `council_core/quant_duel.py`, tab `council_qt/tabs/quant_duel.py`.
- **Each Pi needs** Ollama listening on the network, and the models of the
  roles bound to it.

## Step 1 — the Council PC: the right code

```bash
cd <Council checkout>
git fetch origin
git checkout council-map && git pull origin council-map
git log --oneline -1          # 9dbc5b2 or newer
```

Check that the node work and the Quant Duel wiring pass (about 1 minute):

```bash
QT_QPA_PLATFORM=offscreen COUNCIL_NO_DIALOGS=1 python -m pytest -q \
  tests/test_quant_duel_council.py tests/test_council_map.py \
  tests/test_machine_routing_dialog.py tests/test_council_qt_foundation.py \
  tests/test_node_routing*.py
```

All must pass. For the full suite, use
`--ignore=tests/test_gui_canvas.py`, because that test needs tkinter, from
the retired Tk shell. A full run in a Linux sandbox has **66 failures/errors
that exist before this work too**, in these files:
- `smoke_test.py`, `test_transcript.py`, `test_splash.py` (tkinter-only or Tk-comparison tests);
- `test_gui_*.py`, `test_barbie_v4.py`, `test_frame_*.py`, `test_capture_tab.py`, `test_designer_canvas_qt.py` (generated-app and camera tests);
- `test_python_envs.py`, `test_saved_model_path.py`, `test_llm_engine_review.py`.

Report any failure outside that list as new.

## Step 2 — each Pi: Ollama on the network

Ask the user for each Pi's IP (or hostname) and whether you may SSH in. On
each Pi, either yourself with SSH access or by giving the user these
commands:

```bash
curl -fsSL https://ollama.com/install.sh | sh        # if Ollama is missing
sudo systemctl edit ollama                            # add the two lines below
#   [Service]
#   Environment="OLLAMA_HOST=0.0.0.0"
sudo systemctl daemon-reload && sudo systemctl restart ollama
```

**Models:** a Pi needs the Ollama model of every role bound to it.
- For the Quant Duel it is `llm.model` in the duel's `config.yaml`
  (default `llama3.2:3b`).
- For a council role it is that role's model in the Models tab, which must
  be an `ollama:<name>` model; in-app `.gguf` models cannot be sent to
  another machine.
- **Ask the user before pulling anything**, then `ollama pull llama3.2:3b`
  (or the model they choose; US-origin only, small enough for the Pi:
  1–4B at Q4).

Check from the **Council PC**, for each Pi:

```bash
curl -s http://<pi-ip>:11434/api/tags      # JSON listing the pulled models
```

## Step 3 — register the Pis as Council nodes

**In the app**, which the user can do while you watch:
1. Open the 📈 **Quant Duel** tab (or the 🖧 Nodes tab, an advanced tab).
2. Click **Machines & roles…**.
3. Tick **Send roles to other machines**.
4. Add each Pi as `http://<pi-ip>:11434`, tick **enabled**, and leave
   parallel at 1 for a Pi 5.
5. Bind roles:
   - **quant** → a Pi, with fallback **fail** for the test, so a Pi
     problem shows up instead of quietly running on the PC;
   - optionally small council roles such as **peasant** or **intern** → a
     Pi whose installed model matches that role's Ollama model.
6. Save.

**Or as a script**, writing the vault's `node_routing.json`. First show the
user the file you are about to replace (`cat <vault>/node_routing.json` if it
exists) and get a yes, because it is their configuration.

```bash
python - <<'EOF'
from council_core import node_routing as nr, paths
vault = paths.vault_dir()                     # COUNCIL_VAULT_ROOT or the app's vault
r = nr.load(vault)                            # keep what is there
r.routing_enabled = True
r.nodes["pi-1"] = nr.Node("pi-1", "http://<pi-1-ip>:11434", True, 1)
r.roles["quant"] = nr.Binding("pi-1", "fail")
print(nr.save(vault, r))                      # validates, writes atomically
EOF
```

Check:

```bash
python - <<'EOF'
from council_core import node_routing as nr, quant_duel as qd, paths
nr.invalidate()
print(nr.route("quant"))                      # Target(node='pi-1', url=..., fallback='fail')
print(qd.where_llm_runs())                    # "LLM jobs run on pi-1 (http://.../v1)…"
EOF
```

## Step 4 — the Quant Duel checkout (on the Council PC)

Put it **next to** the Council checkout. The Council's end-to-end test
looks for `../quant-duel`.

```bash
cd <parent of the Council checkout>
git clone -b quant-duel --single-branch https://github.com/Infernoplaystuf/Council quant-duel
cd quant-duel
python -m venv .venv
.venv/bin/pip install -r requirements.txt     # Windows: .venv\Scripts\pip
.venv/bin/python -m pytest -q                 # ~2–3 min; all must pass (192 at hand-off)
.venv/bin/python -m quant_duel.cli ingest     # ~10 years of prices from Yahoo (needs internet)
.venv/bin/python -m quant_duel.cli --node A check --offline
```

The repository is private, so the clone needs the user's GitHub
credentials (a token or SSH key). Ask for them; never invent any.

`check` should show no FAIL lines. WARN lines are expected for the test:
- no `experiment.yaml` (none is wanted for the test);
- the LLM (the duel's own URL is not used once `quant` is bound);
- the news feed with `--offline`.

Optional dry run, about 8 minutes; it must end with `replay check: PASS`:

```bash
.venv/bin/python -m quant_duel.cli replay --start 2026-09-01 --end 2026-09-30
```

Now re-run the Council's end-to-end test, which finds this checkout and
scores a headline on a fake Ollama registered as a Council machine:

```bash
cd <Council checkout>
QT_QPA_PLATFORM=offscreen COUNCIL_NO_DIALOGS=1 python -m pytest -q \
  tests/test_quant_duel_council.py -k end_to_end      # 1 passed, not skipped
```

## Step 5 — turn the duel on in the Council

**In the app:** open the 📈 **Quant Duel** tab and set it up:
1. **Folder:** the `quant-duel` checkout. Leave **Python** blank; it uses
   the checkout's `.venv`.
2. Tick **Node A** and **Node B**, tick **On**, then **Save**.
3. Tick **Run due jobs while the Council is open** only if the user wants
   it to keep running by itself.

Don't also set up a systemd timer or Task Scheduler for the same folder.

**Or as a script**, writing `<vault>/quant_duel.json`. Ask before replacing
an existing one.

```bash
python - <<'EOF'
from council_core import quant_duel as qd, paths
s = qd.Settings(enabled=True, root=r"<path to quant-duel>", nodes=["A", "B"],
                schedule=False)
print(qd.problem(s) or "settings OK")
print(qd.save(paths.vault_dir(), s))
EOF
```

## Step 6 — the test: make a Pi do the duel's LLM work

Times are New York time; trading days only.

1. **Status**, from the tab's **Refresh** or as a script:
   ```bash
   python - <<'EOF'
   from council_core import quant_duel as qd, paths
   s = qd.load(paths.vault_dir())
   for n in s.nodes: print(n, qd.status(s, n))
   EOF
   ```
2. **News scoring on the Pi** (09:00–16:00): click **Run due now**, or call
   `qd.tick(s, log=print)`.
   - Node A fetches the feeds on the PC, then `news-score` runs on the Pi.
   - Expect a log line like `node A: news-score — rc=0, LLM ok on pi-1`.
   - On the Pi, `ollama ps` shows the model loaded.
3. **Daily step and report** (after 16:45): **Run due now** runs `daily`
   for A and B on the PC, then each `report`, whose summary is written on
   the Pi. The reports are in `quant-duel/reports/A|B/<date>.md`.
4. **Failure handling:**
   - Stop Ollama on the Pi (`sudo systemctl stop ollama`) and run a due
     LLM job.
   - Expect `LLM failed`, and then, with fallback `fail`, later LLM jobs
     skipped with "pi-1 is resting".
   - Start Ollama again; after the rest (30 s, doubling up to 5 min)
     the jobs run on the Pi again.
5. **The Council Map tab:** press Refresh. It shows **Quant Duel → pi-1**
   ("LLM work … runs on pi-1").
6. **Optional — council roles on a Pi:** with **peasant** bound to a Pi,
   ask the Council a plain question and check on the Pi with `ollama ps`.

## Report back to the user

- Which Pis were registered (name, URL, enabled, parallel) and which roles
  are bound to them.
- The results of steps 1, 4 and 6, quoting log lines; failures included,
  as they happened.
- Anything you changed on a Pi, and any file you replaced in the vault
  (they agreed first).

## After the test, before the real experiment

The test trades on paper from its first `daily` run. Before the real
experiment:
- Move `quant-duel/data/A` and `quant-duel/data/B` aside, and never delete
  them unless the user says so.
- Freeze the protocol with
  `python -m quant_duel.cli experiment-init --start <first trading day> --end <~21 trading days later>`.
- Run node A's news collection for 2–3 weeks first, as a warm-up.

The duel's own `deploy/README_PI.md` covers the alternative: running duel
nodes directly on Pis with systemd.
