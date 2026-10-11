# Deploying the Quant Duel on Raspberry Pis

Two Raspberry Pi 5s (8 GB), one per node: **A** reads news, **B** never
does. Everything else is identical. Paper trading only — nothing here can
place an order. (Both nodes can also share one PC; see the end.)

## 1. Hardware and OS

- Raspberry Pi 5, 8 GB, active cooler. **Boot from an SSD** (USB 3 or an
  NVMe HAT), not an SD card: the daily job writes SQLite and parquet files
  every day and SD cards wear out and corrupt on power loss.
- Raspberry Pi OS (64-bit, Bookworm — Python 3.11), wired network.
- Give the Pis names you can reach, e.g. `quant-a.local`, `quant-b.local`.

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y git python3-venv python3-dev build-essential cmake \
    libopenblas-dev rsync
timedatectl                      # "System clock synchronized: yes" (NTP)
```

The Pi's own time zone does not matter — every schedule runs in New York
time internally.

## 2. The code

```bash
cd ~
git clone -b quant-duel --single-branch https://github.com/Infernoplaystuf/Council quant-duel
# (a private repo: use a GitHub token or an SSH key, e.g.
#  git@github.com:Infernoplaystuf/Council.git)
cd quant-duel
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install lightgbm      # optional; same choice on BOTH Pis
.venv/bin/python -m pytest -q       # ~2-6 minutes on a Pi 5
```

Both Pis must run the **same commit** (`git rev-parse HEAD` on each).

## 3. The LLM (llama.cpp)

```bash
cd ~
git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp
cmake -B build -DGGML_NATIVE=ON -DLLAMA_CURL=OFF
cmake --build build --config Release -j4 --target llama-server
```

Put a small instruct model in GGUF format, Q4_K_M, in `~/models/` —
**the identical file on both Pis** (copy it from one to the other; `check`
prints its hash). Use a US-origin model, 1–4B parameters, for example
Llama 3.2 3B Instruct, Granite 3.x 2B/3B Instruct, Gemma 2 2B Instruct or
Phi-3.5-mini Instruct. The project never downloads models itself.

In `config.yaml` (the same file on both Pis):

```yaml
llm:
  url: http://127.0.0.1:8080/v1
  model: local                       # llama-server ignores the name
  model_file: ~/models/llama-3.2-3b-instruct-q4_k_m.gguf
  server:
    command: [~/llama.cpp/build/bin/llama-server, -m,
              ~/models/llama-3.2-3b-instruct-q4_k_m.gguf,
              --host, 127.0.0.1, --port, "8080", -c, "4096", -t, "4"]
```

The server is started for each LLM job (news scoring, tuning, report) and
stopped afterwards, so it uses no RAM in between; a lock file makes sure
two LLM jobs never overlap. If the LLM is down, every job still finishes
without it (logged).

## 4. First data and checks

On **each** Pi:

```bash
cd ~/quant-duel
.venv/bin/python -m quant_duel.cli ingest              # ~10 years of prices
.venv/bin/python -m quant_duel.cli backtest            # sanity: ~50-53%
.venv/bin/python -m quant_duel.cli --node A check      # on Pi A (B on Pi B)
.venv/bin/python -m quant_duel.cli replay --start 2026-09-01 --end 2026-09-30
```

`check` lists PASS/WARN/FAIL for Python and packages, the boosting backend,
identical node settings, writable folders, disk, NTP, cached prices, the
LLM, the model file hash, the experiment file and (on A) the news feed.
The replay must end with `replay check: PASS`.

`config.yaml` and `nodes/*.yaml` must be byte-identical on both Pis —
`check` and `compare` verify it through hashes.

## 5. Timers

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/quant-duel@.service deploy/systemd/quant-duel@.timer \
   ~/.config/systemd/user/
loginctl enable-linger $USER
systemctl --user daemon-reload
systemctl --user enable --now quant-duel@A.timer     # on Pi A
systemctl --user enable --now quant-duel@B.timer     # on Pi B
systemctl --user list-timers
```

Every 15 minutes `run-due` decides what is due (`--dry-run` shows it):

| job | node | when (New York) |
|---|---|---|
| `news-poll` (+ scoring) | A | trading days, hourly 09:00–16:00, plus a last poll 15:45–16:00 |
| `daily` (ingest, features, paper step, export) | A, B | trading days after 16:45, inside the experiment window |
| `report` | A, B | after `daily` |
| `tune` | A, B | Saturday (Sunday catches up), inside the window |

Weekends and market holidays are skipped; a job missed while the Pi was
off runs at the next call. Logs: `logs/A.log` (rotated, 5 × 1 MB),
`logs/llm-server.log`, and `journalctl --user -u quant-duel@A`.

## 6. Warm-up (2–3 weeks before the start)

Enable the timers **before** writing `experiment.yaml`'s start date — or
with a start date 2–3 weeks out. Until the start, node A only collects and
scores news (that history is what a sentiment weight is validated on);
nothing is paper-traded. Watch it with:

```bash
.venv/bin/python -m quant_duel.cli --node A news-status
```

## 7. Experiment start checklist

- [ ] Both Pis on the same commit; `pytest` passes on both.
- [ ] `config.yaml`, `nodes/A.yaml`, `nodes/B.yaml` identical on both
      (`sha256sum config.yaml nodes/*.yaml`).
- [ ] Same model file (hash from `check`) and same boosting backend.
- [ ] `check` shows no FAIL on either Pi; NTP synchronised.
- [ ] Prices ingested on both; a `backtest` looks sane (no accuracy flags).
- [ ] A replay of last month ends `replay check: PASS`.
- [ ] Node A has 2–3 weeks of scored news (`news-status`).
- [ ] Write and freeze the protocol **on one Pi**, copy it to the other:
      `python -m quant_duel.cli experiment-init --start <first trading day>
      --end <~21 trading days later>` then
      `scp experiment.yaml quant-b.local:quant-duel/`.
- [ ] Timers enabled on both; `run-due --dry-run` shows the expected jobs.
- [ ] No `data/A/paper.sqlite` or `data/B/paper.sqlite` from earlier tests
      (move old ones aside — the ledger starts on the start date).

During the run: glance at `reports/<node>/` each evening and at
`logs/<node>.log`; don't change any setting (compare will flag it).

## 8. After the run

On your PC (or either Pi), with SSH keys set up to both Pis:

```yaml
compare:
  remote: {A: "pi@quant-a.local:quant-duel/exports/A",
           B: "pi@quant-b.local:quant-duel/exports/B"}
```

```bash
python -m quant_duel.cli sync-exports      # rsync, copies only
python -m quant_duel.cli compare           # reports/compare/<name>/compare.md
```

`compare` first verifies both nodes saw identical prices every day. Each
Pi fetches its own prices at the same time; if Yahoo revised a bar in
between, the hash check will say so. (To rule that out, node B can skip
its own fetch and copy A's: rsync `data/shared/prices/` from A before B's
`daily`, and run B's daily with `--no-ingest`.)

## Both nodes on one machine

On a single PC (e.g. alongside the Council) both nodes share
`data/shared/`, so prices are identical by construction. Schedule
`run-due` for `--node A` and `--node B` every 15 minutes (see
`deploy/cron.example`, which also has the Windows Task Scheduler
commands); the LLM lock keeps their LLM jobs apart. If Ollama is already
running, leave `llm.server.command` empty and point `llm.url` at it.

## Troubleshooting

- **yfinance broke** (it's unofficial): `daily` logs the failure and does
  nothing that day; it catches up when fetching works again
  (`pip install -U yfinance`).
- **LLM down**: tuning rounds log `llm_failed` (retried every 3 hours over
  the weekend), news stays pending, reports are written without the
  summary. Check `logs/llm-server.log`.
- **A day was missed**: the next `daily` processes every missed trading
  day in order, each with only the data it would have had.
- **"another run holds …lock"**: a previous job is still running (or died
  more than 6 hours ago — then the lock is broken automatically).
