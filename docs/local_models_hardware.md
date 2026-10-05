# Local models in the Council: which one does which job, and what better hardware buys

Measured on 2026-10-05 on this laptop. **Draft:** the gpt-oss:20b re-run is still
going, so its results are marked **GPT-OSS PENDING** until it finishes.

<!-- FINALIZER: every "GPT-OSS PENDING" marker (sections 1, 3.1, 3.2, 3.4, 4, 5.2, 5.3, 7)
gets the re-run's numbers from bench_results/2026-10-05/gpt-oss_20b/ (GUI x/12,
code x/8, docs x/17 with the questions / citations / not-covered / code split,
reply tok/s per suite, median and mean seconds per case, placement from notes.md).
Then recompute the gpt-oss rows of 5.2 with the method in 5.3, using its own
per-case token counts and NOT the scaled 2026-10-02 times, and apply the decision
rule at the end of section 4. Delete this comment when you are done. -->

**Labels** (the same as in [specialized_nodes.md](specialized_nodes.md)):
**M\*** = measured on this laptop. **M** = measured by a named source.
**P** = measured on that hardware with a non-US model of the same size (timing
only; that model is never recommended). **V** = vendor claim. **E** = estimate.

---

## 1. The short answer

- **On this laptop, use llama3.1:8b (Meta) for every Council job.** It passed
  11 of 12 GUIs, 6 of 8 code tasks and 16 of 17 documentation items. It fits
  entirely on the 8 GB GPU, so it is fast: about 7 s per GUI, 4 s per code task
  and 2 s per docs answer (medians, **M\***).
- **phi4:14b (Microsoft) is the most accurate US model measured so far**: GUI
  11/12, code 7/8, docs 17/17. But it does not fit in 8 GB. 44% of it runs on the
  CPU, which makes it 5 to 12 times slower: about 81 s per GUI, 21 s per code task
  and 12 s per docs answer. Use it when getting the code right matters more than
  waiting.
- **phi3.5 (Microsoft) is fast but unreliable** for these jobs (GUI 3/12, code
  2/8, docs 12/17). Do not give it the Coder or Docs role.
- **gpt-oss:20b (OpenAI): GPT-OSS PENDING.** Before today's fixes it scored code
  8/8 and docs 16/17, and it passed all 7 of the GUI cases it reached. Each GUI
  took about 90 s on this laptop.
- **The best GUI score came from a model we cannot recommend.** qwen2.5-coder 7B
  (Alibaba, non-US) passed 12/12. It was measured for comparison only.
- **Better hardware buys speed, not correctness.** The pass rate depends on the
  model. The speed depends on whether the whole model fits in GPU memory.
  - A **12 GB** card holds phi4:14b completely. A GUI drops from about 1.5 min to
    about 20–27 s (**E**).
  - A **16 GB** card holds gpt-oss:20b completely. It generates 63–172 tokens/s,
    depending on the card (**M**), against 20.1 on this laptop: 3 to 8.6 times
    faster.
  - llama3.1:8b already fits on this laptop's GPU, so a bigger card gains it
    little. A 12 GB card generates 15–50% faster (**P**), which takes 10–30%
    off each job (**E**).

---

## 2. What was measured and how

**The harness.** Each model was run with this command:

```
python -m council_core.llm_bench --model ollama:<name> --suites gui,code,docs --pipeline new
```

- It puts every Council role on the one model under test.
- Each call goes through the app's own engine route (`council_engine.local_chat`)
  to the local Ollama.
- It works in a throwaway scratch vault, never in your vault.
- It records every reply, every token count and every second.
- It ran one pass per model, and only one model was loaded at a time.

| Suite | What the model must do | How it is graded | Cases |
|---|---|---|---|
| **GUI** | Describe draws a window from a plain-English description. The descriptions range from a contact form and a length converter to a camera capture panel and a log monitor. | The harness grades the drawing itself, independently of Describe. The widgets must come from the palette, stay inside the window, not overlap and form a valid spec. The case's own required widgets, counts and labels must all be present. Then **Generate** must produce code that passes its policy gate. Finally a **probe** builds the generated Qt app offscreen and clicks every control. | 12 (4 simple, 4 medium, 4 complex) |
| **Code** | The code-behind writer ("Write it with the model…") writes what one button does in a small app. Examples: sum two numbers, filter a list, load a CSV, run a stopwatch, convert units. The writer runs with its own checks, smoke run, repair rounds and best-of-N. | A **hidden test** runs the handler in a fresh Python. | 8 |
| **Docs** | Answer from the documentation of an invented package, *glimmerquay*, so no model can know it beforehand. The answers come through `docs_qa` and the bundled MCP documentation server (`tools/pydocs_mcp_server.py`). | 10 questions need the right answer **and** a citation of a page that holds it. 2 questions must get "not covered", because the docs do not answer them. 5 code tasks have hidden tests. | 17 |

**The code under test.** A frozen copy of `qt-migration` at commit **87dc535**. It
includes both fixes from 2026-10-05:
- 62e37a0: Describe's best-of-N now checks that the widgets the description
  names are drawn.
- 87dc535: Ollama now refuses a prompt that is too long, and the engine re-fits
  it, instead of the front being silently lost.

The docs re-runs in 3.5 used branch `llm/docs-retrieval` at 85695ce, which is not
merged yet.

**This PC.**
- Intel i7-14700HX, 32 GB DDR5-5600, NVIDIA RTX 4070 Laptop GPU with 8 GB.
- Ollama 0.35.0, context window 8,192 tokens, Q4_K_M weights (gpt-oss: MXFP4),
  Python 3.11.14.
- The runs took place between 09:21 and 10:50, and the docs re-runs between 14:44
  and 14:46.
- Other test suites were running on the PC at the same time.

**Where the numbers come from.** The raw results are in
`Downloads/Council-Demo/bench_results/2026-10-05/<model>/`: a JSON report, a
console log and notes for each model.
- **Every 2026-10-05 number** in sections 3 and 4 was re-counted from the JSON
  reports. All the pass counts agree with the notes.
- **One detail differs:** phi3.5's garbled names (3.3) are in 11 of 34 code
  replies, not the 12 its notes say.
- **The earlier numbers** in 3.4 come from logs or from earlier reports, as
  marked there.

---

## 3. Results

### 3.1 Pass counts (2026-10-05, commit 87dc535)

| Model | Maker | Origin | GUI | GUI by tier (simple / medium / complex) | Code | Code by tier (simple 3 / medium 4 / complex 1) | Docs | Docs split: questions / right citation / "not covered" / code |
|---|---|---|---|---|---|---|---|---|
| **llama3.1:8b** | Meta | US | **11/12** | 4/4, 4/4, 3/4 | **6/8** | 2/3, 3/4, 1/1 | **16/17** | 9/10, 9/10, 2/2, 5/5 |
| **phi3.5** (3.8B) | Microsoft | US | **3/12** | 2/4, 1/4, 0/4 | **2/8** | 1/3, 0/4, 1/1 | **12/17** | 8/10, 8/10, 2/2, 2/5 |
| **phi4:14b** | Microsoft | US | **11/12** | 4/4, 4/4, 3/4 | **7/8** | 2/3, 4/4, 1/1 | **17/17** | 10/10, 10/10, 2/2, 5/5 |
| **gpt-oss:20b** | OpenAI | US | GPT-OSS PENDING | GPT-OSS PENDING | GPT-OSS PENDING | GPT-OSS PENDING | GPT-OSS PENDING | GPT-OSS PENDING |
| qwen2.5 7B | Alibaba | **non-US**, comparison only | 10/12 | 4/4, 4/4, 2/4 | 4/8 | 3/3, 0/4, 1/1 | 14/17 | 8/10, 8/10, 2/2, 4/5 |
| qwen2.5-coder 7B | Alibaba | **non-US**, comparison only | 12/12 | 4/4, 4/4, 4/4 | 6/8 | 3/3, 3/4, 0/1 | 16/17 | 9/10, 9/10, 2/2, 5/5 |

- Every GUI that passed was also Generated, and its app built and ran offscreen
  with no errors.
- No case was thrown out as a harness or PC fault.
- No reply hit its token limit, and no reply broke its JSON schema.

### 3.2 Speed and time per job on this PC

| Model | Where Ollama put it | Reply tokens/s (GUI / code / docs) | Prompt tokens/s | Median s per case (GUI / code / docs) | Mean s per case (GUI / code / docs) | All 37 cases | Load at start |
|---|---|---|---|---|---|---|---|
| llama3.1:8b | all on the GPU (~5.6 GB) | 46 / 48 / 49 | ~2,700 | 7.0 / 4.4 / 2.0 | 9.1 / 4.9 / 2.5 | 3 min 20 s | 5.4 s |
| phi3.5 | all on the GPU | 71 / 85 / 89 | not measured (cache hits) | 17.7 / 12.2 / 1.8 | 20.3 / 17.3 / 2.8 | 7 min 18 s | 3.2 s |
| phi4:14b | 56% GPU, 44% CPU (11.0 GB) | 7.2 / 8.4 / 9.9 | ~700 | 80.9 / 21.0 / 12.2 | 105.8 / 24.7 / 15.6 | 29 min 17 s | 17.7 s |
| gpt-oss:20b | 43% in VRAM (2026-10-02 check) | GPT-OSS PENDING (20.1 on 2026-10-02) | 711 (2026-10-02) | GPT-OSS PENDING | GPT-OSS PENDING (about 90 / 13 / 7 on 2026-10-02) | GPT-OSS PENDING | cold load 35–83 s, earlier runs |
| qwen2.5 7B *(non-US)* | all on the GPU (4.99 GB) | 49 / 51 / 52 | ~2,900 | 6.8 / 5.7 / 1.7 | 11.1 / 7.3 / 2.0 | ~4 min | 8.4 s |
| qwen2.5-coder 7B *(non-US)* | all on the GPU (4.99 GB) | 50 / 51 / 52 | ~2,900 | 6.2 / 3.6 / 1.9 | 6.6 / 5.0 / 2.3 | 2 min 50 s | 9.3 s |

How to read this table:
- **A "case" covers the whole job**, not just the model call.
  - GUI: Describe, Generate and the offscreen probe.
  - Code: the writer with its checks, the smoke run and the hidden test.
  - Docs: the search query plus the answer.
- **Reply speed is the median over the calls.** The prompt speeds for phi4:14b
  and gpt-oss:20b come from separate speed probes. The others are medians over
  the calls, which can include Ollama's prompt cache; phi3.5's are too inflated by
  it to use. Prompts in these suites were never longer than 2,748 tokens.
- **Writing the reply takes most of the time.** It is 80–98% of the model's time
  in every suite. For example, a llama3.1:8b GUI case spends 7.8 of its 8.5 model
  seconds on the reply, and a phi4:14b GUI case spends 102.6 of 104.9.
- **phi4:14b's GUI mean is higher than its median because of one case.** C2 took
  374 s across 3 calls. With a longer prompt it also slows down: 7.2 tokens/s with
  a 2k-token prompt, 4.7 with a 6k one (**M\***, 2026-10-01).
- **Loading a model when switching between models takes longer.** A cold swap
  was measured at 45 s for llama3.1:8b and 83 s for gpt-oss:20b
  ([specialized_nodes.md](specialized_nodes.md)).

### 3.3 What went wrong

| Model | Failed | What happened |
|---|---|---|
| llama3.1:8b | GUI C3; code K2, K8; docs q05 | **C3:** all 4 attempts drew a number box (spinbox) where the description asks for a slider. **K2:** it filtered the already-filtered list. **K8:** it inverted the unit factor, so 1 in came out as 0.394 cm. **q05:** the answer was right, but it cited the wrong page (see the Council issues below). |
| phi3.5 | GUI: 9 of 12; code K2, K3, K5, K6, K7, K8; docs q03, q05, c01, c04, c05 | **GUI:** every failure was a valid layout with a requested widget left out, even after repairs. **Code:** logic errors, a crash in the smoke run, and an `except Exception` that the policy refused. In 11 of 34 code replies it garbled names, writing "…dict" where a name should be (`filtereddict`, `csvdict`). |
| phi4:14b | GUI C2; code K5 | **C2:** it stacked the three tab pages on top of each other, so they overlap. Its profile (pixel mode, best-of-1) means the new named-widget check never ran for it. **K5:** it invented a blank-input message by stitching together phrases from the writer's own prompt. |
| qwen2.5 7B *(non-US)* | GUI C1, C4; code K2, K3, K6, K7; docs q03, q05, c05 | Mostly logic errors. C1 and C4 show the two Describe weaknesses below. |
| qwen2.5-coder 7B *(non-US)* | code K2, K4; docs q05 | **K4:** it used a module it never imported. The code writer's repair hint pointed the wrong way, so all 5 replies repeated the mistake. |

**Two cases most models failed:**
- **K2, "filter the original list"**, failed for 4 of 5 models. Only phi4:14b got
  it right. The smoke run presses the button only once, so it cannot catch this.
- **q05**, a docs question, failed for 4 of 5 models, again all but phi4:14b.

**Council issues these runs exposed.** None of them changed whether the
recommendations hold.

1. **Docs citations.** The search ranked a page without the fact first, and
   small models nearly always cite page [1]. This is why q05 (and q03 for two
   models) failed.
   - Fixed on `llm/docs-retrieval`, which is not merged yet; see 3.5.
2. **Describe's named-widget check looks only at widget kinds.** A repair can
   drop named buttons and still pass the check (qwen2.5 C1, phi3.5 M4).
3. **Describe and the grader disagree about "a log view".** Describe accepts a
   text box, but the grader wants a log pane (phi3.5 C4, qwen2.5 C4).
4. **Code-writer gaps:**
   - a wrong repair hint for the `module.function()` form (qwen2.5-coder K4);
   - a re-raised `ValueError` counted as a deliberate refusal (qwen2.5 K7);
   - an error sent back for repair with no hint (qwen2.5 K2).
5. **`docs_qa`'s code check is too lenient.** It accepts importing a method as if
   it were a module name (qwen2.5 c05). It also never checks that the requested
   function is actually defined.

### 3.4 Before and after the October fixes

| Stage (code version) | llama3.1:8b GUI / code / docs | phi3.5 GUI / code / docs | gpt-oss:20b GUI / code / docs | Raw data |
|---|---|---|---|---|
| 1. Old pipeline (2026-10-01) | 4/12, 5/8, not run | 1/12 and 0/12 (two passes), 1/8 (both passes), not run | not run | phi3.5: log kept. llama: raw report lost; numbers from the 2026-10-01 report. |
| 2. New pipeline, before the code-writer fixes (2026-10-01) | 11/12, 2/8, not run | 1/12, 1/8, not run | not run | Logs kept. Two passes each, with the same result both times. |
| 3. + code-writer fixes, 080c274 (2026-10-02) | 11/12 (GUI unchanged), 6/8, 16/17 | 1/12, 2/8, 12/17 | 7/7 of the 7 GUIs reached (time cap), 8/8, 16/17 | Raw reports lost (they were in a temp folder); numbers from the reports of that day. |
| **4. + Describe and Ollama fixes, 62e37a0 and 87dc535 (2026-10-05, this report)** | **11/12, 6/8, 16/17** | **3/12, 2/8, 12/17** | **GPT-OSS PENDING** | Raw kept and re-counted. phi4:14b's first full run (11/12, 7/8, 17/17) is also in this stage. |
| 5. + docs-retrieval fix, 85695ce (docs only, not merged) | –, –, 16/17 | –, –, 14/17 | not run | Raw kept. |

**What each change did:**
- **The new pipeline** (a layout tree, a JSON schema and best-of-N for small
  models) took llama3.1:8b's GUIs from **4/12 to 11/12**.
- **The code-writer fixes** of 2026-10-02 took llama3.1:8b's code from **2/8 to
  6/8**, and phi3.5's from 1/8 to 2/8. The fixes:
  - a blank number box arrives as None;
  - errors use the task's own wording;
  - private state is allowed;
  - failed model calls are retried.
- **The Describe fix** (62e37a0) took phi3.5's GUIs from **1/12 to 3/12**.
  - Best-of-N now prefers a complete design, which rescued S1 and M1.
  - llama3.1:8b stayed at 11/12, because all four of its C3 attempts drew the
    same spinbox.
- **The Ollama fix** (87dc535) never triggered. The largest prompt was 2,748
  tokens against a window of 8,192, so these suites cannot show its effect.

### 3.5 After the docs-retrieval fix (branch `llm/docs-retrieval`, not merged)

| Model | Before (87dc535) | After (85695ce) | Questions | Right citation | Code tasks |
|---|---|---|---|---|---|
| llama3.1:8b | 16/17 | 16/17 | 9/10 → **10/10** | 9/10 → **10/10** | 5/5 → **4/5** |
| phi3.5 | 12/17 | **14/17** | 8/10 → **10/10** | 8/10 → **10/10** | 2/5 → 2/5 (c04 now passes, c02 now fails) |

**What it fixed:** the citation failures (q03, q05) are gone, because a page that
holds the fact is now ranked first.

**What it broke:** 2 of the 10 code answers now leave out the function the task
asks for (llama c05, phi3.5 c02). Under the old prompt that happened in 0 of 25
answers.
- The new answer format line is the likely cause.
- `docs_qa`'s code check does not catch it.
- Fix that before merging.

---

## 4. Recommended model per Council role on this PC (US models only)

| Council role | Use | Why | If you can wait |
|---|---|---|---|
| **Coder: GUIs** (Describe, then Generate) | **llama3.1:8b** | 11/12 GUIs, about 7 s each | phi4:14b has the same 11/12 but takes about 81 s each, with no gain. GPT-OSS PENDING. |
| **Coder: code behind** ("Write it with the model…") | **llama3.1:8b** | 6/8, about 4 s per task | **phi4:14b**: 7/8, about 21 s per task. GPT-OSS PENDING (8/8 at about 13 s per task on 2026-10-02). |
| **Docs** (answers from documentation) | **llama3.1:8b** | 16/17, about 2 s per answer | **phi4:14b**: 17/17, about 12 s per answer. GPT-OSS PENDING (16/17 at about 7 s on 2026-10-02). |
| **Council members**: writer, judge, skeptic, sage, strategist, peasant, intern, artist | **llama3.1:8b** | These roles were **not graded** here; see the note below. | – |
| Do **not** use for Coder or Docs | phi3.5 | 3/12 GUIs, 2/8 code, 12/17 docs | – |

**Note on the Council-member roles.** The suites do not test prose, so this pick
rests on three facts:
- llama3.1:8b is the one US model that fits completely on this GPU.
- It handles the JSON-heavy jobs well.
- With every role on one model, nothing has to swap in or out (45–83 s per cold
  swap on this PC).

**Why one model for everything on this laptop.**
- GUIs and the code behind them share **one Coder role**, so they always use the
  same model.
- The 8 GB card holds only one of these models at a time. Giving roles different
  models therefore means a cold load at every switch.
- llama3.1:8b is the only model here that is both reliable and fast.

**GPT-OSS PENDING: the decision rule for the finalizer.**
- If gpt-oss:20b scores code ≥ 7/8, GUI ≥ 11/12 and docs ≥ 16/17 on this commit,
  it replaces phi4:14b as the "if you can wait" choice. On 2026-10-02 it ran
  almost 3 times faster than phi4 (20.1 tokens/s against 7.2).
- It becomes the first choice for the Coder role only if its time per GUI is
  acceptable to you. That time was about 90 s on 2026-10-02.

---

## 5. Hardware tiers: what each one gives you

### 5.1 Why: pass rates follow the model, speed follows the hardware

- **The model decides the pass rate.**
  - Describe picks its profile from the model's size and context window, never
    from the graphics card. The same model makes the same calls on any machine.
    This was not tested on other hardware; it follows from how the code chooses.
  - So a better card does not make llama3.1:8b pass C3, and it does not make
    phi3.5 reliable.
- **The hardware decides the speed.**
  - Writing the reply is 80–98% of the model's time, and its speed is set by
    memory bandwidth.
  - A model that fits completely in the GPU's memory runs at GPU speed. A model
    that spills part of itself to the CPU slows down several times over: phi4:14b
    runs at 7.2 tokens/s here and at 31–42.5 on a 12 GB card.
- **So the question for each tier is: which models fit completely in GPU memory?**

### 5.2 The tiers

Each time is per GUI / per code task / per docs answer, as a mean in seconds
unless marked "min". "Fits" means the model is completely in GPU memory at an
8k-token context. The speeds come from [specialized_nodes.md](specialized_nodes.md)
and its sources; the times are worked out from them as described in 5.3.

| Tier | Fits | llama3.1:8b | phi4:14b | gpt-oss:20b | Best pick |
|---|---|---|---|---|---|
| **8 GB GPU: this laptop** (RTX 4070 Laptop; desktop RTX 4060 / 4060 Ti 8 GB are the same class) | llama3.1:8b, phi3.5. phi4 and gpt-oss spill to the CPU. | 46–49 tokens/s. **9.1 / 4.9 / 2.5** (**M\***) | 7.2–9.9 tokens/s, 44% on the CPU. **106 / 25 / 16** (**M\***) | 20.1 tokens/s, 43% in VRAM (**M\***). GPT-OSS PENDING (about 90 / 13 / 7 on 2026-10-02) | **llama3.1:8b** for everything. phi4:14b when accuracy matters. GPT-OSS PENDING. |
| **12 GB GPU** (RTX 3060 12 GB, 4070, 5070) | Adds **phi4:14b** at 8k context. 16k needs `OLLAMA_FLASH_ATTENTION=1` and `OLLAMA_KV_CACHE_TYPE=q8_0`. gpt-oss still spills. | 55 tokens/s on a 3060 and 71 on a 4070 (**P**). **8.3 / 4.6 / 2.3** and **6.3 / 3.7 / 1.6** (**E**) | 31 tokens/s on a 3060 and 42.5 on a 4070 (**P**). **27 / 8 / 5** and **19 / 6 / 3.4** (**E**). That is 3 to 5.5 times faster than this laptop. | spills; not measured | **phi4:14b** for Coder and Docs: the most accurate US model measured, now at a usable speed. llama3.1:8b and phi4 do not fit together (5.6 + 11 GB). |
| **16 GB GPU** (4060 Ti 16 GB, 5060 Ti, 4070 Ti Super, 5070 Ti, 5080) | Adds **gpt-oss:20b**. | fits; speed depends on the card's memory bandwidth (a 4060 Ti 16 GB has less than a 3060 12 GB); not tabulated | fits; same caveat | **63 / 92 / 156 / 172** tokens/s by card (**M**), against 20.1 here. About **29 / 5 / 2.2** on a 4060 Ti 16 GB and **11 / 3 / 0.8** on a 5080 (**E**, rough; GPT-OSS PENDING) | **gpt-oss:20b** for Coder and Docs, if the re-run confirms its earlier scores (GPT-OSS PENDING). |
| **24 GB GPU** (RTX 3090, 4090; RX 7900 XTX) | gpt-oss:20b **and** llama3.1:8b loaded together (~21 GB), so no swapping. Or bigger models (Gemma 3 27B, OLMo 3 32B), which were not measured on these suites. | fits, alongside gpt-oss | fits | **147.5** tokens/s (3090) and **191** (4090) (**M**). About **13 / 3 / 1** and **10 / 2.7 / 0.7** (**E**, rough) | gpt-oss:20b for Coder and Docs, llama3.1:8b for the Council members, both loaded at once. A 7900 XTX reads prompts at about 30% of a 4090's speed. |
| **32 GB GPU** (RTX 5090) | All of the above, with longer context | fits | fits | **298** tokens/s (**M**). About **7 / 2.3 / 0.5** (**E**, rough) | As for 24 GB, with room to spare. |
| **CPU only** (8-core desktop, dual-channel DDR5, 32 GB) | Models up to about 30B fit in RAM. MoE models such as gpt-oss run relatively well here. | ~11 tokens/s (**E**; a 16-core 7950X measured 11.2, **M**). Prompts at ~60–75 tokens/s (**E**). **~60–67 / ~24–26 / ~18–21** (**E**) | not tabulated; slower than llama | ~10–15 tokens/s, prompts at ~80 (**E**). About **2.5–3.5 min / 25–30 / 17–21** (**E**, rough) | Usable but slow: about a minute per GUI with llama3.1:8b. gpt-oss:20b if the re-run confirms its accuracy. |
| **Pi 5, 8 or 16 GB, CPU** | 1B–4B models, and llama3.1:8b (tight on 8 GB). gpt-oss:20b just fits on 16 GB. | 1.99 tokens/s (**M**); prompts at ~7 (**E**). **~7.8 min / ~2.9 min / ~2.5 min** (**E**). The longest GUI prompt (1,952 tokens) takes ~280 s to read, right at the engine's **300 s first-reply limit**. | 16 GB only: ~1.2 tokens/s (**P**). **~19 min per GUI**, and its GUI prompts exceed the 300 s limit, so it **fails today**. | 16 GB only: ~2.5–4 tokens/s, prompts at ~12–15 (**E**). About **11–15 min / ~2 min / ~1.5 min** (**E**, rough) | **Not for Coder or Docs.** The small models that run well on a Pi (1B–3B, 4.6–11 tokens/s, **M**) were not tested on these suites, and the smallest model that was (phi3.5, 3.8B) passed only 3/12 GUIs. |
| **Pi 5 + AI HAT+ 2** | Llama 3.2 1B only, with a 2,048-token window (**V**) | – | – | – | **Cannot run Coder or Docs at all.** The HAT supports no JSON schema and no tools. Background summaries only. |
| **Pi 4, 8 GB** | 1B–3B models | – | – | – | Not for these roles. A 3B model runs at 1.84 tokens/s (**M**). |

### 5.3 How the times were worked out

- **The formula.** Time per case = fixed overhead + prompt tokens ÷ prompt speed
  + reply tokens ÷ reply speed.
  - The fixed overhead covers Generate, the probe, the smoke run and the tests:
    0.7–1.6 s per GUI or code case, measured here.
  - The token counts are each model's own, per case, from today's runs.
    llama3.1:8b uses 2,002 prompt and 358 reply tokens per GUI. phi4:14b uses
    2,221 and 724.
- **A check of the formula on this laptop.** It predicts 9.1 / 4.9 / 2.4 s for
  llama3.1:8b, which measured 9.1 / 4.9 / 2.5. For phi4:14b's GUIs it predicts
  104.7 s, which measured 105.8 s.
- **gpt-oss:20b's times are rougher.** Its token counts are not known yet, so its
  2026-10-02 times on this laptop (about 90 / 13 / 7 s) were scaled by 20.1 ÷
  the tier's reply speed.
  - On GPUs prompt reading improves about as much as generation does, or more,
    so these times are about right or on the slow side.
  - The CPU and Pi rows also add the slower prompt reading.
  - The finalizer recomputes these rows from the re-run (GPT-OSS PENDING).
- **gpt-oss "thinks" before it answers.** The Council sends it `think: "low"`.
  Those hidden reasoning tokens are part of its reply time.

---

## 6. How to check your own PC

**The Models tab's "Check this PC" button** measures every installed US-made
model on the machine it runs on.
- Non-US models are skipped unless you name them.
- Install models yourself, for example `ollama pull llama3.1:8b`. The Council
  never downloads.
- Press the button again to stop the check.

**What it does for each model:**
1. Unloads everything, so each model starts on an empty card.
2. Makes one cold call. Its time is shown as "first answer after switching".
3. Makes two warm calls, each with a prompt of about 1,200 tokens and a reply of
   up to 192 tokens. These give the **reply** and **prompt** tokens/s.
4. Reads where Ollama put the model: "GPU", "N% on GPU" or "CPU".
5. Runs a **quick probe**: 1 GUI (S2, the length converter, including Generate
   and the offscreen run), 1 code task (K1) and 2 docs questions (q01, q04).
6. Estimates the seconds per GUI, per function and per docs answer, and predicts
   a reply speed from the hardware alone ("hardware predicts ~N").

**What you get:**
- One line per model.
- A "Best —" line for GUIs and code, for docs, and for the fastest replies.
- A saved report, `model_bench.json`, in your vault.

It takes a few minutes per model, and longer for models that spill to the CPU. The
packaged (frozen) build measures speed and placement only.

**How to read the result:**
- **If the measured reply speed is far below the prediction,** the model is
  probably spilling to the CPU, or something else is using the GPU.
- **The quick probe is a smoke test** (4 items), not a pass rate. For pass rates,
  run the full benchmark from section 2. It took 3 to 29 minutes per model on this
  PC.
  - Run it when the PC is otherwise idle.
  - Free commit memory should stay above the model's size plus 4 GB. Below that,
    Windows can run out of it (the 0xC0000142 crash).
- **"Fastest replies" ranks by speed alone.** phi3.5 would top it, even though it
  fails most Coder and Docs jobs.

**Known limits of its speed prediction** (see [specialized_nodes.md](specialized_nodes.md),
section 4):
- **It overpredicts MoE models such as gpt-oss.** On this laptop it predicts 28.4
  tokens/s for gpt-oss:20b, which measured 20.1: **41% too high**.
  - The same formula would predict ~97 tokens/s on a 4060 Ti 16 GB, where 63 was
    measured, and ~339 on a 4090, where 191 was.
  - For gpt-oss, trust the measured number, not the prediction.
- **It is close for ordinary (dense) models:**
  - llama3.1:8b: 41.6 predicted, 46–49 measured;
  - phi4:14b: 8.8 predicted, 7.2–9.9 measured;
  - an RTX 3060: 75.3 predicted, 75.6 measured.
  - It overpredicts an RTX 3090 by 24%.
- **Some hardware gets no prediction at all.** Its table has no RTX 50-series, AMD
  or Intel GPUs. It also assumes dual-channel RAM, which is wrong for a Pi.

---

## 7. Caveats, and what was not measured

- **Small samples.** Each model ran once: 12 GUIs, 8 code tasks and 17 docs
  items. A difference of one case can be chance.
  - phi3.5's GUI rise from 1/12 to 3/12 has a traced cause: the new check
    rescued S1 and M1 (see 3.4). It is still a small number.
- **The PC was busy.** Other agents' test suites were running during every run.
  - Free commit memory dipped to 0.6 GB (phi3.5 run) and 1.3 GB (phi4 run).
  - No case was invalid, but the speeds may be slightly low.
- **gpt-oss:20b has not been measured on the current code yet.** On 2026-10-05
  the PC never had enough free memory (model size + 4 GB) to load it safely. Its
  re-run is in progress: **GPT-OSS PENDING**.
- **Prose roles were not graded.** The writer, judge, skeptic, sage, strategist,
  peasant, intern and artist were not tested. The suites cover only GUIs, code
  behind and documentation answers.
- **Other hardware was not measured with these suites.**
  - The tier speeds come from the sources in
    [specialized_nodes.md](specialized_nodes.md). Some are **P**: timed with a
    non-US model of the same size.
  - The tier times are **E**.
  - The pass rates are assumed to carry over unchanged (see 5.1).
- **Prompts were short.** No prompt in these suites exceeded 2,748 tokens, and
  the window was 8,192 tokens.
  - Long documents were not tested.
  - The over-long-prompt fix (87dc535) never triggered.
  - Speed drops as prompts grow: phi4:14b runs at 7.2 tokens/s with a 2k-token
    prompt and 4.7 with a 6k one.
- **Some earlier figures cannot be re-checked.** Stage 1 for llama3.1:8b and all
  of stage 3 in 3.4, including gpt-oss's 2026-10-02 scores, come from that day's
  reports. Those raw reports were in a temp folder and are gone. Only the
  phi3.5 old-pipeline log and the stage-2 logs survive.
- **Only US-origin models are recommended** (Llama: Meta; Phi: Microsoft; Gemma:
  Google; Granite: IBM; OLMo: AI2; gpt-oss: OpenAI). qwen2.5 and qwen2.5-coder
  (Alibaba) were measured for comparison only. Gemma, Granite and OLMo were not
  installed, so they were not measured.
- **Other fixes are not merged yet.** The docs-retrieval fix (3.5) is on its own
  branch and has a regression to fix first. The Council issues listed in 3.3 are
  not fixed yet.
