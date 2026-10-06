# Specialized nodes for the Council — what Pi and GPU-PC nodes can do, and what it takes

Research of 2026-10-05, after a fact-check pass (corrections applied). The setups
are THEORETICAL tiers, not specific boards; the Pi accelerator is the AI HAT+ 2.
The goal: each Council role served by a chosen model on a chosen machine (e.g. a
Pi as a small writer/summary node, a GPU desktop as a coder node), machines that
join only occasionally — not one model split across machines.

## 1. The short answer

- **Inside the Council there is no per-role machine setting today.** The slot
  file has no machine field, and the one opt-in switch (`COUNCIL_REMOTE_NODES=1`)
  sends every Ollama role to the same single other host. The older multi-node
  dispatcher is broken: every member asks the node for the same placeholder
  model name (`gguf:unset`), the node answers "not found", and the member falls
  back to the local model — often AFTER the full council prompt has already been
  sent to the node. The Qt Council tab never uses that dispatcher.
- **But role-per-machine can work today with no Council change** — a small
  (~100-line) model-routing proxy on 127.0.0.1 that forwards each request to the
  node holding that model. It was run against stand-in nodes: the peasant went to
  the "Pi", the coder and judge to the "GPU desktop", tool calls and the benchmark
  worked through it. It is a quick **stage 0** (a few hours) with real caveats
  (section 5).
- **Doing it properly inside the Council** is roughly 58–80 hours of focused work
  in three stages (my estimate), plus 2–4 hours on a real AI HAT+ 2.
- **Hardware, in one breath:**
  - **GPU PCs are the only nodes that make interactive work faster.** An 8 GB GPU
    summarises an 8,000-token document in ~12 s; a 16 GB GPU in 3–8.5 s
    depending on the card; a 24 GB one in ~3 s.
  - **A Pi 5 is a real background worker.** It generates ~11 tok/s with 1B
    models and ~4.7 tok/s with 3B models (measured). A 1,000-token summary takes
    under a minute. Long documents are slow (minutes), and today some would hit
    the engine's 300-second first-reply limit (section 2).
  - **The AI HAT+ 2 adds a second model, not speed.** It runs its own model on its
    own 8 GB at ≤3 W while the Pi's CPU runs another, reads short prompts ~6×
    faster than the Pi's CPU, uses about half the CPU's energy per job, but does
    not generate faster. Its only US-origin language model today is Llama 3.2 1B,
    with a 2,048-token window.
  - **Fair credit the raw speeds hide:** a Pi node lets your laptop's 8 GB GPU keep
    one model loaded instead of swapping. A cold swap was measured at 45 s
    (llama3.1:8b) and 83 s (gpt-oss:20b) before the first token — a Pi finishes a
    1k-token summary within one swap.

A 16 GB Raspberry Pi is a Pi 5 (or a CM5 / Pi 500+, same chip); the Pi 4 tops out
at 8 GB and has no PCIe connector, and the AI HAT+ 2 is documented for the Pi 5
only.

---

## 2. Node tiers

All models listed are US-origin: Llama (Meta), Gemma (Google), Phi (Microsoft),
Granite (IBM), OLMo (AI2), gpt-oss (OpenAI). Q4 quantization unless noted.

**Labels:** **M** measured by a named source · **M\*** measured on your laptop ·
**P** measured on that hardware with a non-US model of the same size (timing only,
never recommended) · **V** vendor claim · **E** estimate (generation ≈ usable
memory bandwidth ÷ bytes read per token; prompt speed compute-bound).

**Job times** = (document + ~60 instruction tokens) ÷ prompt speed + 300 reply
tokens ÷ generation speed. Checked against the laptop's phi4 runs: 58.5 s
predicted vs 58.7 s measured at 2k, 93.3 s vs 94.0 s at 6k. All job-time cells are
**E**, built from the same row's inputs, with the model already loaded (a cold
load adds 2–35 s, measured here; more from a Pi's microSD — section 3).

**† = fails today:** Ollama sends nothing while it reads the prompt, and the engine
waits at most 300 s for the first byte (`council_engine.py:2788`, `:3169`). At
these prompt speeds that caps a document at about: Gemma 3 1B 15–20k tokens, Llama
3.2 3B ~4.8k, Gemma 3 4B / Phi-4-mini ~4.2–4.5k, Llama 3.1 8B ~2.1k, Gemma 3 12B
~1.2k, Pi 4 3B ~1.3k. Stage 1 fixes this (a per-node allowance, or chunking).

| Tier | US models that fit | Generation tok/s | Prompt tok/s | Summarise 1k / 4k / 8k → 300 tokens | Write 500 tokens | Power | Best Council roles |
|---|---|---|---|---|---|---|---|
| **Pi 4 8 GB** (CPU; no PCIe → no HAT) | Llama 3.2 1B/3B, Gemma 3 1B | 3B: 1.84 **M** [PIB]; 1B: ~4.6 **E** | 3B: 4.3 **M** [PIB]; 1B: ~12–18 **E** | Gemma 3 1B: 2.1 / 5.0† / 9.1† min. Llama 3.2 3B: 7.1 / 20.9† / 42.6† min | 1.9 min (1B) | ~6–7 W **E** | Overnight batch summaries of short texts only |
| **Pi 5 8 GB, CPU** | Llama 3.2 1B/3B, Gemma 3 1B/4B, Gemma 4 E2B/E4B, Phi-4-mini; Llama 3.1 8B is tight | 1B: 11.2–11.5 **M** [STR]; 2B: 5.8 **M**; 3B: 4.6–4.9 **M** [GG, STR]; 8B: 1.99 **M** [GG]; Gemma 4 E2B/E4B ≈ 2B/4.5B class **E** | 1B: ~45–70 **E**; 3B: ~16–18 **E**; 8B: ~7 **E** | Gemma 3 1B: 42 s / 88 s / 2.5 min. Llama 3.2 3B: 2.2 / 5.8 / 11.5† min. Llama 3.1 8B: 5.2 / 13.5† / 25.8† min | 44 s (1B), 1.9 min (3B), 4.3 min (8B) | 10.2–14 W whole system **M** [CNX, GG] | Background summariser, titles and tags, intern/peasant drafts in batch jobs |
| **Pi 5 16 GB, CPU** (also CM5 16 GB, Pi 500+) | As 8 GB, plus Gemma 3/4 12B; gpt-oss 20B just fits (~2 GB left) | Same speed for the same model (3B: 4.88 **M** [GG]); 12B: ~1.3 **E**; 14B class: 1.20 **P**; gpt-oss 20B (MoE): ~2.5–4 **E** | 12B: ~4 **E**; gpt-oss 20B: ~12–15 **E** | Gemma 3 12B: 7.9† / 19.6† / 35.4† min. gpt-oss 20B: ~2.9 min / 4k borderline† | 6.6 min (12B) | 11.9 W **M** [GG] | As 8 GB, plus slow overnight higher-quality passes on short texts. More RAM fits bigger models; it does not make any model faster. The only Pi tier that can run the Council's best-measured model (gpt-oss 20B), as a batch job. |
| **Pi 5 + AI HAT+ 2, HAT side** (the CPU side keeps the row above and runs at the same time) | **Llama 3.2 1B** only (the one US model in Hailo's current GenAI zoo, 2,048-token window). Needs GenAI zoo **5.2+** from Hailo's Developer Zone (account); the 5.1.1 package in Raspberry Pi's docs has only Llama 3.2 3B, which Hailo has since dropped. No custom LLMs. | 1B: 9.89 **V** [HZ] — credible: Hailo's 7.35 for Qwen2.5-1.5B matches independent 6.7–8.0 [CNX, schwab.sh, AX]. 3B (old zoo): 2.60 **M** vs 4.78 on a CM5's CPU [CNX]. The "30–50 tok/s" claims have no measurement behind them. | ~210–250 **P** (Qwen2.5-1.5B time-to-first-token [AX]); a 96-token prefill took 320 ms on the HAT vs 2,039 ms on the CPU **P** [RPi] | 35 s / 1.6 min (3 chunks) / 2.4 min (5 chunks) — long documents must be split for the 2,048 window | 51 s | 7.2–7.6 W whole system while the HAT generates **M** [CNX]; HAT ≤3 W [GG]; HAT + CPU busy ~15–17 W **E** | An always-on second model for short-text summaries, tagging, classifying, routing. No JSON-schema or tool roles. |
| **CPU-only desktop** (8-core, dual-channel DDR5, 32 GB) | Anything to ~30B; practical: Llama 3.2 3B, Phi-4-mini, Gemma 3 4B, Llama 3.1 8B; **gpt-oss 20B and Gemma 4 26B-A4B (MoE) run well here** | 3B: ~25 **E**; 8B: ~11 **E** (16-core 7950X measured 11.2 **M**); gpt-oss 20B: ~10–15 **E** | 3B: ~150 **E**; 8B: ~60–75 **E**; gpt-oss 20B: ~80 **E** | 3B: 16.5 s / 39 s / 1.3 min. 8B: 39 s / 1.6 / 3.3 min. gpt-oss 20B: ~2.5 min at 8k | 18.5 s (3B), 42 s (8B) | ~100–150 W **E**; idle ~40–100 W **E** | Peasant, intern, summaries, batch work — and a slow but capable coder/docs node on gpt-oss 20B |
| **Reference: your laptop, RTX 4070 Laptop 8 GB** (= the 8 GB GPU tier; desktop RTX 4060 / 4060 Ti 8 GB are the same class) | Llama 3.1 8B fully in VRAM to ~8–12k context; Llama 3.2 3B. Phi-4 14B and gpt-oss 20B spill to the CPU. | llama3.1:8b 46–47 **M\***; phi4:14b 7.2 (44% on CPU) **M\***; gpt-oss:20b 20.1 (43% in VRAM) **M\*** | llama3.1:8b 2,731 **M\***; phi4 ~700 **M\***; gpt-oss 711 **M\*** | llama3.1:8b: 6.7 / 8.6 / 12.0 s. gpt-oss spilled: 15 / 21 / 31 s | 10.4 s (8B) | not measured | Measured on the Council suites: llama3.1:8b Describe 11/12, docs 16/17, code-behind 6/8; gpt-oss:20b code 8/8, docs 16/17 (re-measurement after today's fixes is running) |
| **12 GB GPU** (RTX 3060 12 GB, 4070, 5070) | Adds Gemma 3/4 12B and Phi-4 14B **at 8k context** (16k needs `OLLAMA_FLASH_ATTENTION=1` + `OLLAMA_KV_CACHE_TYPE=q8_0`, else it spills). gpt-oss 20B still spills. | 8B: 55 (3060) / 71 (4070) **P** [HC]; 14B: 31 / 42.5 **P** | 8B: 1,697 / 3,564 **P**; 14B: 973 / 2,100 **P** | 8B on 3060: 5.5 / 7.8 / 12.0 s. Phi-4 on 4070: 6.9 / 9.0 / 12.6 s | 8.3 s / 10.7 s | Whole PC with a 3060: 202–229 W **M** [GG] | Judge, skeptic, sage, strategist on Phi-4; long summaries on Gemma 12B |
| **16 GB GPU** (4060 Ti 16 GB, 5060 Ti, 4070 Ti Super, 5070 Ti, 5080) | Adds gpt-oss 20B fully in VRAM | gpt-oss: 63 (4060 Ti 16) / 92 (5060 Ti) / 156 (5070 Ti) / 172 (5080) **M** [HC] | 3,274 / 3,585 / 6,178 / 9,146 **M** [HC] | gpt-oss 8k: 8.4 s (4060 Ti 16) / 6.4 s (5060 Ti) / 3.8 s (5070 Ti) / 3.0 s (5080), excluding gpt-oss's hidden reasoning | 4.9 s (5060 Ti) / 2.9 s (5070 Ti) | Card rating 165–360 W | Coder / code-behind writer, docs, Describe on gpt-oss 20B |
| **24 GB GPU** (RTX 3090, 4090; RX 7900 XTX) | Adds Gemma 3 27B, Gemma 4 26B-A4B and 31B, OLMo 3 32B; or gpt-oss 20B + Llama 3.1 8B loaded together (~21 GB) | gpt-oss: 147.5 (3090) / 191 (4090) **M**; Gemma 4 31B on 3090: 34.7 **M** [HC] | gpt-oss: 4,400 / 8,369 **M**; Gemma 4 31B: 1,156 **M** | gpt-oss on 4090: 1.6 / 2.1 / 3.0 s. Gemma 4 31B on 3090: 8.7 / 12.2 / 18.3 s | 2.4 s / 13.1 s | Whole PC with a 4090: 262–519 W **M** [GG] | "Heavy" node: two roles at once, or a higher-quality judge/writer. A 7900 XTX matches a 4090 on generation but reads prompts at ~30% of its speed **M** [LCPP] |
| **32 GB GPU** (RTX 5090) | All of the above with longer context, or two mid-size models together | gpt-oss 298; Gemma 4 31B 61 **M** [HC] | 9,444; 3,395 **M** | gpt-oss: 1.0 / 1.4 / 2.2 s | 1.5 s | 575 W card rating | Hub for several roles; long-context summaries |

---

## 3. What the table cannot show

- **Summary quality of small models** (Vectara hallucination leaderboard, July 2025;
  lower is better): Llama 3.2 1B 20.7%, Gemma 3 1B 5.3%, Llama 3.2 3B 7.9%, Gemma 3
  4B 3.7%, Phi-4-mini 3.4%, Llama 3.1 8B 5.4%, Gemma 3 12B 2.8%; the 2B Granites
  8.8% (3.0), 15.7% (3.1), 16.5% (3.2) — worse each version; Granite 3.3 8B 10.6%
  on the harder September 2026 board, where Phi-4-mini also drops to 23.5%. So the
  HAT's only US model is the least faithful summariser here; on the Pi's CPU,
  Gemma 3 1B runs at about the same speed with a quarter of the hallucination
  rate. Use Granite 2B for non-summary batch work, if at all. Instruction-following
  (IFEval, matters for the Council's JSON prompts): Llama 3.2 1B 59.5, 3B 77.4, 8B 80.4.
- **Pi prompt reading is the weak spot.** A council member's prompt carries the
  user profile, memories, recent history and vault excerpts. At an assumed ~3,000
  tokens per prompt (not measured), a 13-call council turn would take ~17 minutes
  on a Pi 5 with a 1B model and ~50–60 minutes with 3B; your laptop does it in ~2
  minutes (estimates). Pis suit background roles, not live council members.
- **CPU contention on a Pi is severe:** generation fell from ~26 to 3.55 tok/s with
  other CPU-heavy work alongside [llama.cpp PR 13079]. With the HAT and a CPU model
  together, limit the CPU model to 3 threads — on the node (e.g. `PARAMETER
  num_thread 3` in a Modelfile), since the Council never sends `num_thread`.
- **Energy per small job favours the HAT** (estimates): a 1k-token summary ≈ 35 s ×
  7.4 W ≈ 0.26 kJ on the HAT, ≈ 0.44–0.55 kJ with Gemma 3 1B on the Pi's CPU, ≈ 1.2 kJ
  on a 3060 PC with an 8B model (with a better model). Idle draw decides
  "occasional": a Pi 5 idles at ~3 W (commonly reported) and can simply stay on; a
  GPU desktop idles at ~40–100 W, so only desktops need waking.
- **Pi storage:** the AI HAT+ 2 occupies the Pi 5's only PCIe connector, so a HAT
  node can't also use an NVMe HAT without a PCIe switch board. CPU models then load
  from microSD (~100 MB/s max) or a USB 3 SSD: ~20 s for a 2 GB model, ~80–90 s for
  8 GB from SD (estimates) — paid again after every 30-minute keep-alive expiry, and
  counted against the 300 s first-reply limit.
- **The network is not a factor for text:** an 8k-token document is ~30 KB, 0.24 ms
  on gigabit Ethernet.
- **Splitting one model across machines** (not your goal): 10 CM5 boards ran Llama
  3.1 70B at 0.85 tok/s on 70 W; a 4090 + CPU ran it at 3.1 tok/s [GG]. Hailo cannot
  split one LLM across chips. Not worth it here.
- **Prices** (October 2026, moved several times in 2026): Pi 5 16 GB $305; AI HAT+ 2
  $200; Pi 5 8 GB ~$175 (derived); Pi 4 8 GB $165; RTX 3060 12 GB ~$230; 4060 Ti 16
  GB ~$400; 5060 Ti ~$549; 5070 Ti ~$916; 3090 ~$1,000 used; 4090 ~$2,200 [HC]. A Pi 5
  8 GB + HAT ($375) now costs more than a 16 GB Pi alone.
- **The original AI HAT+** (Hailo-8/8L) runs no language models. Neither HAT helps
  Typhon as it stands: its classifier is a scikit-learn random forest and its
  cameras attach to the PC.

---

## 4. What exists in the Council today

Line numbers re-checked at commit 87dc535. "Measured" = run against the engine
with `tests/fake_ollama` stand-in nodes; nothing was sent to the real Ollama.

> **Update, later on 2026-10-05 (merge of `fix/localhost-guard`):** several rows below
> are now FIXED. `_ensure_localhost` reads the whole host (`localhost.evil.example`
> refused, `[::1]` accepted); every call to a model server bypasses proxies and
> redirects (a system or `HTTP_PROXY` proxy used to carry "local" requests off the PC);
> the old dispatcher sends a prompt only to a node whose `/api/tags` lists the EXACT
> model and otherwise runs locally sending nothing; a failing node cools down (30 s,
> doubling to 5 min); a node's connect gives up after `COUNCIL_NODE_CONNECT_TIMEOUT`
> (5 s) and its first reply after `COUNCIL_NODE_FIRST_REPLY_S` (default still 300 s);
> a stream that ends without Ollama's final packet is an error on both the remote and
> the local path; the dispatcher's prints are ASCII-safe. Still as described: no
> per-role machine setting, members run one at a time, Stop cannot cancel a remote
> call, the Apothecary wizard is unchanged.

| Piece | State | Evidence |
|---|---|---|
| Pin a role to a machine | **Missing.** `Slot` has only `name`, `path`, `n_ctx`; a `host` key in model_slots.json is dropped on load and save (measured). | `council_core/model_slots.py:93-96`, `parse()` ~146-178 |
| Per-role engine path (`local_chat` → `_route_chat` → `_ollama_local`) | **One host for the whole app.** A remote host only with `COUNCIL_REMOTE_NODES=1`, and then every `ollama:` role goes to the one `COUNCIL_OLLAMA_HOST`. A role whose model exists only elsewhere fails "has no model" (measured). | `council_engine.py:3362-3366`, `:4037` |
| Per-call `host=` on `local_chat` | **Works** with the switch on (coder → GPU node, peasant → Pi, measured) **but no production caller passes it** (AST scan: 128 call sites). | `council_engine.py` `local_chat` ~3522 |
| Vault summaries | **Have no role:** `vault_index` calls `local_chat` with no role, so they always run on the MAIN slot (with the context condenser and every role-less caller). A Pi summariser needs a new "summarizer" role first. Failures are swallowed (`raw = ""`). | `vault_index.py:2888-2893`, `:2934-2939`; `model_slots.py:61-63` |
| First-reply limit | 300 s for load + prompt reading, global; long Pi prompts time out (section 2 †). | `council_engine.py:2788`, `:3169` |
| Qt Council tab | **Never uses the dispatcher** — every member runs locally. | `council_qt/tabs/council.py:158` |
| Qt Nodes tab "Apply & Rebuild" | Builds a dispatched council and discards it; only monitors. | `council_qt/tabs/nodes.py:79` |
| Old dispatcher (Tk console) | **Broken since the GGUF migration.** Every role's model is one shared label (`gguf:unset`…); `best_host_for` matches it as a substring, never finds it, and picks any reachable host. When a node wins, it returns 404 and the member falls back to local **after the full prompt reached the node** (measured with a planted string). Not every time: if the PC's own Ollama wins the sort, nothing leaves — but it is the usual case when the PC's Ollama is busy or not running (`run-windows.bat` sets `COUNCIL_BACKEND=gguf`). | `council_engine.py:6911`, `:4084-4098` (substring `:4090`), `:7021-7087` |
| Host choice | Substring (`llama3` matched a Pi holding only `llama3.2:3b`); falls back to any host; ignores role and hardware; prefers a cold node. | `council_engine.py:4090-4091` |
| Council members in parallel | **No** — all three phases loop one member at a time (13 calls, max 1 in flight, measured). | `council_core/deliberation.py:530, 633, 694` |
| Dispatcher failure handling | A hung node blocks the member **300 s** (measured); a node that dies mid-answer with a clean close returns the partial text as success; Stop cannot cancel. | `_ollama_remote_call` ~3825-3858 |
| Per-role failure handling | **Good:** refuses a remote host before sending when the switch is off; checks the exact model against `/api/tags`; a dead node fails in ~2 s; Stop works. But a node that is OFF behind the one host reports "has no model … `ollama pull …`" — misleading. | `council_engine.py:3363-3374`; `local_models.py` ~203-226 |
| `chat_tools` | **Bug:** refuses a remote host even with the switch on (model lookup omits host / allow_remote). | `council_engine.py:3757` |
| Main slot fallback | The automatic GGUF→Ollama fallback and default-model pick refuse remote hosts. | `council_engine.py` ~2911, ~2998 |
| Benchmarks | `llm_bench`'s direct `OllamaBackend` never allows a remote node. | `council_core/llm_bench.py:267` |
| Context window | Default `num_ctx` 8192 trims an 8k document (7,797 of 8,002 tokens kept, measured); a summary slot needs ≥ 12288. | `council_engine.py:2770` |
| Localhost check | `_ensure_localhost` tests the START of the URL: accepts `http://localhost.evil.example`, refuses `[::1]` (measured). `local_models.is_loopback_url` is right. | `council_engine.py:139` |
| Version skew | Every chat now sends `"truncate": false` (87dc535); an older node ignores it and silently drops the front of an over-long prompt — the bug that commit fixed. No per-node version check. | `council_engine.py:3149` |
| Text encoding | `best_host_for` prints `→`; raises `UnicodeEncodeError` only when output is redirected to a cp1252 pipe/file (a normal console launch is fine). | `council_engine.py:4093-4096` |
| Apothecary registration | "Register" writes `set COUNCIL_PI_HOSTS=<one url>` — a second Pi **replaces** the first; never sets `COUNCIL_REMOTE_NODES`; read once at import; per-node `council_role` is never used by routing. | `council_core/apothecary.py:232`; `council_engine.py:6793` |
| Apothecary Pi setup wizard | **Unsafe:** binds Ollama to `0.0.0.0` and opens 11434 to everyone (ufw + iptables); Ollama has no login; stores SSH passwords in plain text; accepts any SSH host key; a 60 s monitor restarts Ollama with sudo; runs `curl … install.sh \| sh` and `ollama pull` (breaking the "Council never downloads" rule) with a non-US default model (`qwen2.5:3b`). | `apothecary_engine.py:115-120, 145, 161, 166, 173`, ~516, ~781-789; `council_core/apothecary.py:47, 52-53` |
| Apothecary hardware model | `AI_HAT_TOPS = 26.0` (Hailo-8, runs no LLMs); no HAT+ 2 option; stock Ollama cannot use a Hailo chip. | `council_core/apothecary.py:52-53` |
| AI HAT+ 2 compatibility | Hailo's `hailo-ollama` (port 8000) has Ollama-style endpoints, but expects `keep_alive` as a number while the Council sends `"30m"` → **every chat would get HTTP 400** (emulated, not run on hardware); the Council would also assume 8192 tokens instead of 2,048, and one request at a time is served. | `council_engine.py:2777-2780`; hailo_model_zoo_genai `DTOs.hpp` |
| Speed predictor (`pc_check`) | Accurate for 8–12 GB cards (3060: 75.3 predicted vs 75.6 measured); overpredicts the 3090 by 24%; **overpredicts MoE models** (gpt-oss: 28.4 vs 20.1 measured, +41%); no RTX 50 / AMD / Intel entries; assumes a 64-bit bus (wrong for a Pi; with the Pi 5's 32-bit bus it lands within ~6%). | `council_core/pc_check.py` ~69-88, 251-301 |
| Model ranking | `rank_for_role` has no speed term — on a 16 GB CPU-only node it ranks `phi4:14b` first for writer/intern, the slowest model it can load (measured). | `council_core/local_models.py` ~657-695 |
| Tests | 276 pass; none exercises real multi-node routing. | — |

---

## 5. Stage 0 — role-per-machine today, with no Council change (a few hours)

A small proxy on 127.0.0.1 merges `/api/tags` from the nodes and forwards
`/api/show` and `/api/chat` by the request's `model` field; point
`COUNCIL_OLLAMA_HOST` at it and give each role an `ollama:<model>` that lives on
the chosen node. Because the URL is loopback, the per-role path, `chat_tools` and
the benchmark all accept it, with `COUNCIL_REMOTE_NODES` off. It can also rewrite
`keep_alive` for a hailo-ollama node. Verified with fake nodes
(`scratchpad/nodes4/factcheck/proxy_probe.py`).

Caveats: model names must be unique across nodes (`ollama cp` to alias); stats show
the proxy, not the node; no privacy gating and no fallback; a node that is off
produces the misleading "has no model" error. **And it shows a limit of the safety
design:** a URL check cannot enforce "nothing leaves the PC" — any loopback
forwarder (this proxy, an SSH tunnel) passes it. The UI should show
`COUNCIL_OLLAMA_HOST` as an explicit opt-in.

---

## 6. Example node layouts (after stages 1–3)

### A. Laptop + one Pi 5 16 GB with AI HAT+ 2 (~$505 for the Pi side)

| Machine / endpoint | Model | Roles | Why |
|---|---|---|---|
| Laptop GPU (8 GB) | llama3.1:8b (Meta) | All council members, coder, docs, Describe, main | Only this machine is fast enough for interactive work |
| Pi CPU (Ollama :11434) | Gemma 3 1B (Google) for any length; Gemma 3 4B for documents up to ~4k tokens (longer need chunking) | The new "summarizer" role: vault-record summaries and titles, in the background | Best faithfulness for the speed among small US models |
| Pi HAT (hailo-ollama :8000) | Llama 3.2 1B (Meta) | Tags, classification, first-pass summaries of short notes (< ~1,700 tokens) | Its own memory at ≤3 W, so the CPU model keeps running |

**Gains:** background summaries continue while the laptop runs the council; the
laptop GPU stops swapping models (45–83 s saved per avoided swap); roughly 100 short
summaries an hour on the HAT plus 75–85 on the CPU at ~15–17 W (estimate).
**No single job gets faster:** the laptop does a 1k summary in ~7 s; the Pi 35–51 s.

### B. Laptop + one 16 GB GPU desktop + two Pi 5s (CPU only)

| Machine | Model | Roles | Why |
|---|---|---|---|
| GPU desktop (16 GB) | gpt-oss:20b (OpenAI) | Coder / code-behind writer, docs, Describe | Fully in VRAM: 63–172 tok/s by card vs 20.1 spilled on the laptop; the best Council model measured for code (8/8). An 8k summary drops from 31 s to 3–8.5 s by card. |
| Laptop (8 GB) | llama3.1:8b | Council members, main | Stays loaded — no more 8B↔20B swaps (45 s / 83 s cold, measured) |
| Pi A | Gemma 3 4B (Google) or Phi-4-mini (Microsoft) | Summarizer (documents up to ~4k tokens; longer → chunking) | Low hallucination on the July 2025 board (3.4–3.7%) |
| Pi B | Llama 3.2 3B (Meta) | Overnight batch queue, intern drafts | Extra background throughput |

**Gains:** two GPU models loaded at once; after stage 3, rebuttals run on the laptop
and the desktop at the same time (two calls to two hosts: 1.03 s together vs 2.12 s
one after the other, fakes).

### C. Laptop + one 24 GB GPU desktop as the "heavy" node

| Machine | Model | Roles |
|---|---|---|
| 24 GB desktop | gpt-oss:20b + llama3.1:8b loaded together (~21 GB), or Gemma 3 27B / Gemma 4 31B (Google) for the judge | All council members, coder, docs |
| Laptop | Llama 3.2 3B, or nothing | UI, Typhon capture, image work |

**Gain:** the laptop GPU is free for camera and image work. **Risk:** when the
desktop is off, each role answers on the laptop or fails, per its stage-1 setting.

### All-Pi setups

Fine for background summaries; not usable for live council turns (~17–60 minutes a
turn, estimate).

---

## 7. Recommended staged plan

Hours are my estimates of focused work, adjusted for the fact-check's additions.

### Stage 0 — the routing proxy (2–4 h). See section 5.

### Stage 1 — pin a role to a machine, safely (28–38 h)

**You would see:** a **Machine** dropdown per role in the Roles panel ("This PC" plus
the nodes enabled for that role), whose model list shows that machine's installed US
models; the stats line names the machine ("llama3.2:1b on pi-kitchen — 212 tokens at
10.8 tok/s") and any fallback ("pi-kitchen did not answer in 2 s — answered on this
PC").

**The work:**
- **Node registry** in `vault/node_registry.json`: per node `enabled` (default
  false), `allowed_roles` (default none), `privacy` (`restricted` default, or
  `trusted`), `transport` (`lan` / `ssh-tunnel`), `connect_timeout_s` (2),
  `first_reply_s` (per node, replacing the global 300 s for slow Pis), `endpoints`,
  `kind`; a top-level `routing_enabled` (default false); `COUNCIL_REMOTE_NODES=0`
  forces it off.
- **Slots:** optional `node`, `endpoint`, `fallback` (`main` / `fail`), written only
  when set, so old files re-save unchanged.
- **A "summarizer" role** (in `model_slots.COUNCIL_ROLES` and the Roles panel), and
  `vault_index` passing it — with summary failures shown, not swallowed; long
  documents chunked when a node's window or first-reply limit is too small.
- **Routing in one place** (`_resolve_target`): a host is allowed only if loopback or
  the registered address of an enabled node; `_ensure_localhost` → `is_loopback_url`.
- **Timeouts and fallback:** short connect timeout; fall back only before the first
  token, never re-send a request that produced output; a failing node cools down
  30 s, doubling to 5 min; "node unreachable" told apart from "model missing".
- **Per-node version floor** (read `/api/version`; refuse or warn below it — e.g.
  `truncate:false`, `think`, JSON-schema formats need recent servers).
- Host passed to `chat_tools`; summary slots default to `n_ctx` 12288; node and
  fallback recorded in the call stats.
- **Retire the old dispatcher** from the call path (it leaks prompts and cannot
  succeed).
- Offscreen tests against FakeOllama nodes, with a guard that fails any connection
  to the real port 11434.

### Stage 2 — safe node setup and the AI HAT+ 2 (15–21 h, plus 2–4 h on real hardware)

**You would see:** the Nodes tab lists nodes with an on/off switch, status (up /
down / cooling down), loaded models, bound roles and the last call; the Apothecary
gets kind (including "Pi 5 + AI HAT+ 2"), "use for inference", privacy, transport
and allowed roles.

**The work:**
- **Rewrite the setup wizard as command sheets the user runs** (no `curl | sh`, no
  `ollama pull` by the Council): bind Ollama to the wired IP only; allow 11434 (and
  8000 for hailo-ollama) only from the main PC's IP; static IP with `nmcli`
  (current Pi OS uses NetworkManager); an SSH-tunnel option; key-based SSH with
  pinned host keys.
- **US-only model presets** (replace the Qwen defaults).
- **Hailo adapter:** `keep_alive` as a number or omitted; never sends `format`,
  `tools` or `think`; clamps prompts to 2,048 tokens; one request at a time; tok/s
  from `eval_count` / `total_duration`. Document the install: GenAI zoo 5.2+ from
  Hailo's Developer Zone (account), `hailo-h10-all` (cannot coexist with the
  original HAT's `hailo-all`), and that hailo-ollama downloads its models from
  Hailo once (the node needs internet at setup).
- **Role limits:** roles needing a JSON schema or tools (docs, coder, Describe,
  code-behind) cannot bind to the HAT.
- **CPU threads on the node** via a Modelfile (`PARAMETER num_thread 3`) when the HAT
  and a CPU model share a Pi.

### Stage 3 — node-aware suggestions and parallel members (13–17 h)

**You would see:** "Suggest across machines" proposes which role goes where with a
predicted speed per node; an option to run rebuttals in parallel.

**The work:** rank models per node from its model list and hardware; extend
`pc_check.predict_gen_tok_s` with an explicit memory bandwidth (fixes the Pi's
32-bit bus) and **fix its MoE overprediction** (+41% on gpt-oss); speed floors
(interactive ≥ ~10 tok/s, background ≥ ~2); speeds learned from real calls (JSON, no
pickle); "Check this node"; parallel rebuttals (they read only fixed answers, so
nothing members see changes; parallel drafting stays opt-in).

**Total:** ~58–80 h (estimate).

---

## 8. Safety design (from stage 1)

- **Opt-in:** nothing leaves the PC by default. A node must be registered AND
  enabled, the role must be in its allowed list, and the master switch on. Hosts not
  in the registry are refused even with routing on.
- **What may leave the PC** (plain text, to the chosen node only): council members —
  the full stitched prompt (system prompt, role memory, user profile, project and
  recent context, vault excerpts, the task; `council_engine.py` ~5531-5589); vault
  summaries — the record's text; the docs role — the question plus the fetched
  documentation. Embeddings never leave (in-process). Roles carrying vault data go
  only to nodes marked **trusted**.
- **LAN exposure:** Ollama and hailo-ollama have no login; hailo-ollama binds
  `0.0.0.0:8000` by default and exposes pull and delete. Each node must accept
  connections only from the main PC's IP (firewall on the wired interface) or sit
  behind an SSH tunnel; and the main PC should reach only `/api/chat`, `/api/tags`,
  `/api/show`, `/api/ps`, `/api/version` — block pull, push, create and delete (a
  small reverse proxy on the node). Run a current Ollama: CVE-2024-37032 (RCE) was
  fixed in 0.1.34 and CVE-2024-39720/39722 in 0.1.46, while the model-poisoning and
  model-theft issues via pull/push were never fixed — hence the endpoint filter.
- **The limit:** a URL check cannot see through a loopback forwarder (section 5), so
  `COUNCIL_OLLAMA_HOST` must be shown in the UI as an explicit opt-in.
- **Occasional nodes:** a node that is off costs at most the 2 s connect timeout,
  then the cool-down skips it; a node idle > 30 min pays a cold model load on its next
  call. Pis reportedly cannot be woken over the network; a Pi 5 can wake on its RTC
  schedule or via a smart plug, or simply stay on at ~3 W. GPU desktops support
  Wake-on-LAN, but the Council has no sender yet.
- **Your rules:** the Council never downloads models (the user runs installs and
  pulls on the node); nothing deleted; no pickle.

## 9. Still needs real hardware

- hailo-ollama's real behaviour (the `keep_alive` rejection; prompts over 2,048
  tokens); whether GenAI zoo 5.2's HailoRT matches Raspberry Pi's `hailo-h10-all`.
- Pi 5 prompt speed at 1k–8k tokens (estimates could be off ±40%; one llama-bench run
  settles it); Gemma 4 E2B/E4B and gpt-oss 20B on a Pi 5 16 GB (no measurements exist).
- The HAT and a CPU model together: heat, and hailo-ollama's own CPU use.
- How long Windows takes to give up connecting to a powered-off LAN machine (~21 s
  estimated).

## Sources

- [GG] [github.com/geerlingguy/ai-benchmarks](https://github.com/geerlingguy/ai-benchmarks); [jeffgeerling.com/blog/2026/raspberry-pi-ai-hat-2](https://jeffgeerling.com/blog/2026/raspberry-pi-ai-hat-2)
- [CNX] cnx-software.com AI HAT+ 2 review, 2026-01-20 (HAT on a Pi 5 2 GB; CPU figures from a CM5 4 GB devkit)
- [HZ] [github.com/hailo-ai/hailo_model_zoo_genai](https://github.com/hailo-ai/hailo_model_zoo_genai) (MODELS.rst, CHANGELOG, DTOs.hpp, controller.hpp)
- [AX] [arxiv.org/html/2603.23640v1](https://arxiv.org/html/2603.23640v1)
- [RPi] [raspberrypi.com AI HAT+ docs](https://www.raspberrypi.com/documentation/accessories/ai-hat-plus.html), [raspberrypi.com/documentation/computers/ai.html](https://www.raspberrypi.com/documentation/computers/ai.html), [raspberrypi.com/products/ai-hat-plus-2](https://www.raspberrypi.com/products/ai-hat-plus-2)
- [STR] stratospherelinuxips.readthedocs.io (Pi 5 LLM performance)
- [PIB] [github.com/yosefdenham/pi-llm-bench](https://github.com/yosefdenham/pi-llm-bench)
- [LCPP] llama.cpp discussions 15013, 15021, 10879 and PR 13079 ([github.com/ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp))
- [HC] [hardware-corner.net/gpu-llm-benchmarks](https://www.hardware-corner.net/gpu-llm-benchmarks/)
- [VEC] [github.com/vectara/hallucination-leaderboard](https://github.com/vectara/hallucination-leaderboard)
- Ollama CVEs: [thehackernews.com/2024/11/critical-flaws-in-ollama-ai-framework.html](https://thehackernews.com/2024/11/critical-flaws-in-ollama-ai-framework.html)
- Gemma 4: [ollama.com/library/gemma4](https://ollama.com/library/gemma4)
- M\*: your laptop's Ollama 0.35 measurements (this project's memory notes and logs).
