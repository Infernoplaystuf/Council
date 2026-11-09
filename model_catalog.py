"""
model_catalog.py — single source of truth for the curated GGUF model list.

Every model here is US-origin (per the project's US-models-only constraint):
made by a US company or lab, not a US fine-tune of a foreign base model.
Sized from CPU-only boxes and 8 GB cards up to 24 GB cards, at Q4_K_M.

Public API (stable — read by onboarding.py, setup_wizard.py, the GUI's
download dialog, and the README generator):

    MODELS: list[ModelSpec]                         # canonical catalog
    DEFAULT_MODEL_ID: str                           # what the wizard preselects
    by_id(id) -> ModelSpec | None
    CURRENT: list[ModelSpec]                        # MODELS minus legacy
    TIER_PICKS / tier_picks(vram_gb, role)          # best picks per card
    for_vram(vram_gb, *, role='general') -> list[ModelSpec]
    fits(spec, vram_gb) -> bool
    download_command(spec) -> str                   # one-line CLI snippet
    download_steps(spec) -> str                     # how to get it, in full
    pretty_table(specs=None) -> str                 # for CLI wizard / README

Add a model by appending one ModelSpec entry. No other file needs editing —
the wizard and docs read from MODELS directly.

THE LIST AS OF 2026-11-08
Current models (legacy=False) are the ones recommended: IBM Granite 4.2
(3B / 8B / 30B: summarising, extraction, answering from your files), Google
Gemma 4 (E2B / E4B / 12B / 26B-A4B / 31B: also reads pictures and scans,
256K context from 12B up), Meta Muse Glimmer 30B (Ollama only), OpenAI
gpt-oss-20b, Microsoft Phi-4-reasoning and Phi-4-mini, and Ai2 OLMo 3. The
older entries stay, marked legacy=True, so a saved setting or an installed
file still resolves; they are not offered as picks. TIER_PICKS names the
best picks per graphics-card size, and `download_steps` says how to get
each model (Ollama tag, Hugging Face file, picture-reader file, pages).

Each entry names its Ollama tag (`ollama`), so the Models tab can say a
candidate is ALREADY on this PC. `good_for` lists the extra council roles a
general model is a strong pick for ("code", "docs"). GGUF files of the
newest families (Gemma 4, Granite 4.2) need a recent llama.cpp; Ollama runs
them as soon as it is updated. Nothing here is downloaded by being listed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass(frozen=True)
class ModelSpec:
    id: str                # short stable key — used by wizard / settings
    name: str              # human-friendly label
    org: str               # creator (one of the US orgs below)
    role: str              # 'general' | 'code' | 'tiny'
    params_b: float        # parameter count in billions
    quant: str             # e.g. 'Q4_K_M'
    size_gb: float         # approximate on-disk size
    context_k: int         # native context in thousands of tokens
    vram_gb_q4: float      # ballpark VRAM needed at this quant + 4K ctx
    hf_repo: str           # huggingface repo id, owner/name
    hf_file: str           # exact .gguf filename in the repo
    license: str           # umbrella license name (informational)
    blurb: str             # one-line for menus / READMEs
    is_default: bool = False
    tags: List[str] = field(default_factory=list)
    ollama: str = ""       # the same model's Ollama tag ("llama3.1:8b")
    good_for: Tuple[str, ...] = ()   # extra council roles: "code", "docs"
    active_params_b: Optional[float] = None   # MoE: parameters per token
    hf_mmproj: str = ""    # vision models: the picture-reader file, same repo
    legacy: bool = False   # kept so saved settings resolve; not recommended

    @property
    def reads_pictures(self) -> bool:
        return "vision" in self.tags

    @property
    def has_hf_file(self) -> bool:
        return bool(self.hf_repo and self.hf_file)

    @property
    def is_moe(self) -> bool:
        return bool(self.active_params_b) and \
            self.active_params_b < self.params_b

    @property
    def effective_params_b(self) -> float:
        """A dense-equivalent size for ranking: an MoE's quality sits between
        its active and total counts (the geometric mean is the usual rule of
        thumb), so gpt-oss-20b (3.6B active) is not ranked above a dense
        14B by its 20.9B total."""
        if self.is_moe:
            return round(math.sqrt(self.params_b * self.active_params_b), 1)
        return self.params_b


# ============================================================
# Catalog
#
# Ordering: current models first (grouped by maker), then the older
# entries marked legacy=True.
# When in doubt about a repo/filename, verify on huggingface.co
# before bumping or adding.
# ============================================================

MODELS: List[ModelSpec] = [
    # ════════════════════════════════════════════════════════
    # CURRENT MODELS (updated 2026-11-08)
    # Tags read on ollama.com/library and file names read from the
    # Hugging Face file listings that day. Sizes are the .gguf downloads.
    # ════════════════════════════════════════════════════════

    # ─── IBM Granite 4.2 — summarising, extraction, RAG ─────
    ModelSpec(
        id="granite-4.2-8b",
        name="IBM Granite 4.2 8B Instruct (Q4_K_M)",
        org="IBM",
        role="general",
        params_b=8.0, quant="Q4_K_M", size_gb=5.3, context_k=128,
        vram_gb_q4=6.5,
        hf_repo="ibm-granite/granite-4.2-8b-GGUF",
        hf_file="granite-4.2-8b-Q4_K_M.gguf",
        license="Apache-2.0",
        blurb="Default. Built for summarising, pulling facts out of "
              "documents and answering from your own files; tool calling "
              "and a thinking mode. The 8 GB-card pick.",
        is_default=True,
        tags=["default", "summarize", "extract", "recommended-8gb", "tools"],
        ollama="granite4.2:8b",
        good_for=("code", "docs"),
    ),
    ModelSpec(
        id="granite-4.2-3b",
        name="IBM Granite 4.2 3B Instruct (Q4_K_M)",
        org="IBM",
        role="general",
        params_b=3.0, quant="Q4_K_M", size_gb=2.2, context_k=128,
        vram_gb_q4=3.2,
        hf_repo="ibm-granite/granite-4.2-3b-GGUF",
        hf_file="granite-4.2-3b-Q4_K_M.gguf",
        license="Apache-2.0",
        blurb="Small Granite: the same summarising and extraction skills for "
              "small cards, laptops without a GPU and Raspberry Pis.",
        tags=["small", "fast", "summarize", "cpu-friendly"],
        ollama="granite4.2:3b",
    ),
    ModelSpec(
        id="granite-4.2-30b",
        name="IBM Granite 4.2 30B Instruct (Q4_K_M)",
        org="IBM",
        role="general",
        params_b=30.0, quant="Q4_K_M", size_gb=17.7, context_k=128,
        vram_gb_q4=19.5,
        hf_repo="ibm-granite/granite-4.2-30b-GGUF",
        hf_file="granite-4.2-30b-Q4_K_M.gguf",
        license="Apache-2.0",
        blurb="Largest Granite. Careful summaries and extraction on 24 GB "
              "cards. A dense model, so it is slow when it spills to RAM.",
        tags=["summarize", "extract", "recommended-24gb"],
        ollama="granite4.2:30b",
        good_for=("code", "docs"),
    ),

    # ─── Google Gemma 4 — reads pictures and scans, 256K context
    ModelSpec(
        id="gemma-4-e2b",
        name="Google Gemma 4 E2B Instruct (Q4_K_M)",
        org="Google",
        role="tiny",
        params_b=2.0, quant="Q4_K_M", size_gb=3.1, context_k=128,
        vram_gb_q4=4.0,
        hf_repo="unsloth/gemma-4-E2B-it-GGUF",
        hf_file="gemma-4-E2B-it-Q4_K_M.gguf",
        hf_mmproj="mmproj-F16.gguf",
        license="Apache-2.0",
        blurb="Tiny Gemma that also reads pictures and scanned pages. For "
              "cards under 6 GB or no GPU.",
        tags=["tiny", "vision", "cpu-friendly"],
        ollama="gemma4:e2b",
    ),
    ModelSpec(
        id="gemma-4-e4b",
        name="Google Gemma 4 E4B Instruct (Q4_K_M)",
        org="Google",
        role="general",
        params_b=4.0, quant="Q4_K_M", size_gb=5.0, context_k=128,
        vram_gb_q4=6.0,
        hf_repo="unsloth/gemma-4-E4B-it-GGUF",
        hf_file="gemma-4-E4B-it-Q4_K_M.gguf",
        hf_mmproj="mmproj-F16.gguf",
        license="Apache-2.0",
        blurb="Reads pictures, charts and scanned forms on an 8 GB card. "
              "Pair it with Granite 4.2 8B for text.",
        tags=["small", "vision", "recommended-8gb"],
        ollama="gemma4:e4b",
    ),
    ModelSpec(
        id="gemma-4-12b",
        name="Google Gemma 4 12B Instruct (Q4_K_M)",
        org="Google",
        role="general",
        params_b=12.0, quant="Q4_K_M", size_gb=7.1, context_k=256,
        vram_gb_q4=9.5,
        hf_repo="unsloth/gemma-4-12b-it-GGUF",
        hf_file="gemma-4-12b-it-Q4_K_M.gguf",
        hf_mmproj="mmproj-F16.gguf",
        license="Apache-2.0",
        blurb="The 12 GB-card pick: text and pictures in one model, 256K "
              "context for long documents. On 16 GB it leaves room for a "
              "very long window.",
        tags=["vision", "long-context", "writing", "recommended-12gb"],
        ollama="gemma4:12b",
        good_for=("docs",),
    ),
    ModelSpec(
        id="gemma-4-26b-a4b",
        name="Google Gemma 4 26B-A4B Instruct (Q4_K_M)",
        org="Google",
        role="general",
        params_b=26.0, quant="UD-Q4_K_M", size_gb=17.0, context_k=256,
        vram_gb_q4=18.5,
        hf_repo="unsloth/gemma-4-26B-A4B-it-GGUF",
        hf_file="gemma-4-26B-A4B-it-UD-Q4_K_M.gguf",
        hf_mmproj="mmproj-F16.gguf",
        license="Apache-2.0",
        blurb="The 16 GB-card pick in Ollama: a mixture of experts with ~4B "
              "parameters active per word, so it stays fast when part of it "
              "spills to RAM. Strong on documents and pictures.",
        tags=["moe", "vision", "long-context", "recommended-16gb"],
        ollama="gemma4:26b",
        good_for=("docs",),
        active_params_b=4.0,
    ),
    ModelSpec(
        id="gemma-4-31b",
        name="Google Gemma 4 31B Instruct (Q4_K_M)",
        org="Google",
        role="general",
        params_b=31.0, quant="Q4_K_M", size_gb=18.3, context_k=256,
        vram_gb_q4=20.5,
        hf_repo="unsloth/gemma-4-31B-it-GGUF",
        hf_file="gemma-4-31B-it-Q4_K_M.gguf",
        hf_mmproj="mmproj-F16.gguf",
        license="Apache-2.0",
        blurb="Strongest US model that fits one 24 GB card: best at long "
              "documents and scanned pages.",
        tags=["vision", "long-context", "recommended-24gb"],
        ollama="gemma4:31b",
        good_for=("code", "docs"),
    ),

    # ─── Meta ───────────────────────────────────────────────
    # No Hugging Face file: the only GGUF found (2026-11-08) is under an
    # account named "meta-models", which could not be confirmed as Meta's.
    # Ollama's library lists it as Meta's, so it is offered through Ollama
    # only (hf_repo empty: "Download & install" says to use Ollama).
    ModelSpec(
        id="muse-glimmer-30b",
        name="Meta Muse Glimmer 30B",
        org="Meta",
        role="general",
        params_b=29.6, quant="Q4_K_M", size_gb=18.0, context_k=128,
        vram_gb_q4=19.5,
        hf_repo="",
        hf_file="",
        license="Apache-2.0",
        blurb="Reads documents, charts and screenshots beside text; built "
              "for tool use. A 24 GB-card pick. Ollama only.",
        tags=["vision", "tools", "recommended-24gb"],
        ollama="muse-glimmer:30b",
        good_for=("docs",),
    ),

    # ─── OpenAI ─────────────────────────────────────────────
    ModelSpec(
        id="gpt-oss-20b",
        name="OpenAI gpt-oss-20b (MXFP4)",
        org="OpenAI",
        role="general",
        params_b=20.9, quant="MXFP4", size_gb=12.1, context_k=128,
        vram_gb_q4=14.0,
        hf_repo="ggml-org/gpt-oss-20b-GGUF",
        hf_file="gpt-oss-20b-MXFP4.gguf",
        license="Apache-2.0",
        blurb="Mixture of experts: ~3.6B parameters active per token, so with "
              "the experts in RAM it runs on an 8 GB card + 32 GB RAM (Ollama "
              "places them; llama-cpp-python 0.3.35 offloads whole layers "
              "only). Best at step-by-step number work, code and tool "
              "calling.",
        tags=["moe", "tools", "reasoning", "recommended-16gb"],
        ollama="gpt-oss:20b",
        good_for=("code", "docs"),
        active_params_b=3.6,
    ),

    # ─── Microsoft Phi-4 ────────────────────────────────────
    ModelSpec(
        id="phi-4-reasoning",
        name="Microsoft Phi-4-reasoning 14B (Q4_K_M)",
        org="Microsoft",
        role="general",
        params_b=14.7, quant="Q4_K_M", size_gb=9.1, context_k=32,
        vram_gb_q4=11.0,
        hf_repo="bartowski/microsoft_Phi-4-reasoning-GGUF",
        hf_file="microsoft_Phi-4-reasoning-Q4_K_M.gguf",
        license="MIT",
        blurb="Thinks step by step before answering: good for maths and "
              "careful analysis questions. Slower replies; 32K context.",
        tags=["reasoning"],
        ollama="phi4-reasoning:14b",
        good_for=("code",),
    ),
    ModelSpec(
        id="phi-4-mini",
        name="Microsoft Phi-4-mini Instruct 3.8B (Q4_K_M)",
        org="Microsoft",
        role="general",
        params_b=3.8, quant="Q4_K_M", size_gb=2.5, context_k=128,
        vram_gb_q4=3.6,
        hf_repo="bartowski/microsoft_Phi-4-mini-instruct-GGUF",
        hf_file="microsoft_Phi-4-mini-instruct-Q4_K_M.gguf",
        license="MIT",
        blurb="Small and quick with good reasoning for its size; runs on a "
              "CPU.",
        tags=["small", "fast", "cpu-friendly"],
        ollama="phi4-mini:3.8b",
    ),

    # ─── Ai2 OLMo 3 — fully open (weights, data, code) ──────
    ModelSpec(
        id="olmo-3-7b",
        name="Ai2 OLMo 3 7B Instruct (Q4_K_M)",
        org="AllenAI",
        role="general",
        params_b=7.3, quant="Q4_K_M", size_gb=4.5, context_k=64,
        vram_gb_q4=5.8,
        hf_repo="unsloth/Olmo-3-7B-Instruct-GGUF",
        hf_file="Olmo-3-7B-Instruct-Q4_K_M.gguf",
        license="Apache-2.0",
        blurb="Fully open weights and training data (Allen Institute). Pick "
              "for transparency.",
        tags=["fully-open", "transparent"],
        ollama="olmo-3:7b-instruct",
    ),
    ModelSpec(
        id="olmo-3-32b-think",
        name="Ai2 OLMo 3 32B Think (Q4_K_M)",
        org="AllenAI",
        role="general",
        params_b=32.2, quant="Q4_K_M", size_gb=19.5, context_k=64,
        vram_gb_q4=21.5,
        hf_repo="bartowski/allenai_Olmo-3-32B-Think-GGUF",
        hf_file="allenai_Olmo-3-32B-Think-Q4_K_M.gguf",
        license="Apache-2.0",
        blurb="Largest fully open model; thinks step by step. A 24 GB-card "
              "option for transparency.",
        tags=["fully-open", "reasoning"],
        ollama="olmo-3:32b-think",
    ),

    # ════════════════════════════════════════════════════════
    # OLDER MODELS (legacy=True), kept so saved settings, installed
    # files and by_id() still resolve. They are no longer recommended:
    # every one has a newer US model above that does the same job better.
    # ════════════════════════════════════════════════════════
    # ─── Was the default until 2026-11 ──────────────────────
    ModelSpec(
        id="granite-3.1-8b-q4",
        name="IBM Granite 3.1 8B Instruct (Q4_K_M)",
        org="IBM",
        role="general",
        params_b=8.0, quant="Q4_K_M", size_gb=4.9, context_k=128,
        vram_gb_q4=6.5,
        hf_repo="bartowski/granite-3.1-8b-instruct-GGUF",
        hf_file="granite-3.1-8b-instruct-Q4_K_M.gguf",
        license="Apache-2.0",
        blurb="Solid general baseline. Conservative refusals; good with tabular data.",
        tags=["default", "balanced"],
        ollama="granite3.1-dense:8b",
        legacy=True,
    ),
    ModelSpec(
        id="llama-3.1-8b-q5",
        name="Meta Llama 3.1 8B Instruct (Q5_K_M)",
        org="Meta",
        role="general",
        params_b=8.0, quant="Q5_K_M", size_gb=5.7, context_k=128,
        vram_gb_q4=7.0,
        hf_repo="bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
        hf_file="Meta-Llama-3.1-8B-Instruct-Q5_K_M.gguf",
        license="Llama 3.1 Community License",
        blurb="Long-context generalist. Best for big folder dumps / multi-CSV context.",
        tags=["long-context"],
        ollama="llama3.1:8b-instruct-q5_K_M",
        good_for=("code", "docs"),
        legacy=True,
    ),
    ModelSpec(
        id="llama-3.2-3b-q5",
        name="Meta Llama 3.2 3B Instruct (Q5_K_M)",
        org="Meta",
        role="general",
        params_b=3.2, quant="Q5_K_M", size_gb=2.4, context_k=128,
        vram_gb_q4=3.2,
        hf_repo="bartowski/Llama-3.2-3B-Instruct-GGUF",
        hf_file="Llama-3.2-3B-Instruct-Q5_K_M.gguf",
        license="Llama 3.2 Community License",
        blurb="Fast, fits 8 GB cards comfortably. Good for routing / classification.",
        tags=["small", "fast"],
        ollama="llama3.2:3b",
        legacy=True,
    ),
    ModelSpec(
        id="llama-3.2-1b-q8",
        name="Meta Llama 3.2 1B Instruct (Q8_0)",
        org="Meta",
        role="tiny",
        params_b=1.2, quant="Q8_0", size_gb=1.3, context_k=128,
        vram_gb_q4=1.8,
        hf_repo="bartowski/Llama-3.2-1B-Instruct-GGUF",
        hf_file="Llama-3.2-1B-Instruct-Q8_0.gguf",
        license="Llama 3.2 Community License",
        blurb="Tiny. CPU-friendly. Use for quick demos or constrained boxes.",
        tags=["tiny", "cpu-friendly"],
        ollama="llama3.2:1b",
        legacy=True,
    ),

    # ─── Microsoft Phi family ──────────────────────────────
    ModelSpec(
        id="phi-4-q4",
        name="Microsoft Phi-4 14B (Q4_K_M)",
        org="Microsoft",
        role="general",
        params_b=14.0, quant="Q4_K_M", size_gb=8.9, context_k=16,
        vram_gb_q4=11.0,
        hf_repo="bartowski/phi-4-GGUF",
        hf_file="phi-4-Q4_K_M.gguf",
        license="MIT",
        blurb="Best reasoning at this VRAM tier. Pick for analyst Q&A on dense "
              "data. On an 8 GB card it runs with partial offload (about 24 "
              "of 40 layers on the GPU at 8K), several times slower.",
        tags=["reasoning", "recommended-16gb"],
        ollama="phi4:14b",
        good_for=("code",),
        legacy=True,
    ),
    ModelSpec(
        id="phi-3.5-mini-q5",
        name="Microsoft Phi-3.5-mini Instruct (Q5_K_M)",
        org="Microsoft",
        role="general",
        params_b=3.8, quant="Q5_K_M", size_gb=2.8, context_k=128,
        vram_gb_q4=3.6,
        hf_repo="bartowski/Phi-3.5-mini-instruct-GGUF",
        hf_file="Phi-3.5-mini-instruct-Q5_K_M.gguf",
        license="MIT",
        blurb="Compact, strong reasoning. Great middle-ground for 8 GB VRAM.",
        tags=["small", "reasoning"],
        ollama="phi3.5",
        legacy=True,
    ),

    # ─── Google Gemma ──────────────────────────────────────
    ModelSpec(
        id="gemma-2-9b-q4",
        name="Google Gemma 2 9B Instruct (Q4_K_M)",
        org="Google",
        role="general",
        params_b=9.2, quant="Q4_K_M", size_gb=5.8, context_k=8,
        vram_gb_q4=7.5,
        hf_repo="bartowski/gemma-2-9b-it-GGUF",
        hf_file="gemma-2-9b-it-Q4_K_M.gguf",
        license="Gemma Terms of Use",
        blurb="Polished prose; helpful for write-ups. Short native context.",
        tags=["writing"],
        ollama="gemma2:9b",
        legacy=True,
    ),

    # ─── Coder role ────────────────────────────────────────
    # The id is kept (settings may name it) but the entry is no longer
    # BOGUS: it was "IBM Granite 3.0 8B Code Instruct" — no such model —
    # pointing at granite-3.0-8b-instruct, a general instruct model. It is
    # labelled as what the file is; the Coder role's stronger picks come in
    # through `good_for` (Llama 3.1 8B, Phi-4, gpt-oss-20b).
    ModelSpec(
        id="granite-3-8b-code-q4",
        name="IBM Granite 3.0 8B Instruct (Q4_K_M)",
        org="IBM",
        role="code",
        params_b=8.0, quant="Q4_K_M", size_gb=4.6, context_k=128,
        vram_gb_q4=6.5,
        hf_repo="bartowski/granite-3.0-8b-instruct-GGUF",
        hf_file="granite-3.0-8b-instruct-Q4_K_M.gguf",
        license="Apache-2.0",
        blurb="General IBM instruct model trained with a large code share "
              "(not a code-specialised model). A Coder-role option when "
              "Llama 3.1 8B is not wanted.",
        tags=["code"],
        ollama="granite3-dense:8b",
        legacy=True,
    ),

    # ─── AllenAI OLMo 2 — fully open US model ─────────────
    ModelSpec(
        id="olmo-2-13b-q4",
        name="AllenAI OLMo 2 13B Instruct (Q4_K_M)",
        org="AllenAI",
        role="general",
        params_b=13.7, quant="Q4_K_M", size_gb=8.4, context_k=4,
        vram_gb_q4=10.5,
        hf_repo="bartowski/OLMo-2-1124-13B-Instruct-GGUF",
        hf_file="OLMo-2-1124-13B-Instruct-Q4_K_M.gguf",
        license="Apache-2.0",
        blurb="Fully open weights + training data (Allen Institute). Pick for transparency.",
        tags=["fully-open", "transparent"],
        ollama="olmo2:13b",
        legacy=True,
    ),

    # ─── Added for the 8 GB-card tier (2026-10) ─────────────
    # Appended, as this module asks: the first nine keep their order.
    ModelSpec(
        id="llama-3.1-8b-q4",
        name="Meta Llama 3.1 8B Instruct (Q4_K_M)",
        org="Meta",
        role="general",
        params_b=8.0, quant="Q4_K_M", size_gb=4.9, context_k=128,
        vram_gb_q4=6.2,
        hf_repo="bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
        hf_file="Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
        license="Llama 3.1 Community License",
        blurb="The 8 GB-card pick: fits the GPU with an 8K window, native "
              "tool calling in Ollama (the docs role's documentation "
              "server), good at code.",
        tags=["recommended-8gb", "tools"],
        ollama="llama3.1:8b",
        good_for=("code", "docs"),
        legacy=True,
    ),
    ModelSpec(
        id="gemma-3-4b-q4",
        name="Google Gemma 3 4B Instruct (Q4_K_M)",
        org="Google",
        role="general",
        params_b=4.3, quant="Q4_K_M", size_gb=2.5, context_k=128,
        vram_gb_q4=3.6,
        hf_repo="bartowski/google_gemma-3-4b-it-GGUF",
        hf_file="google_gemma-3-4b-it-Q4_K_M.gguf",
        license="Gemma Terms of Use",
        blurb="Small and quick with a long window; a fast-role pick for 8 GB.",
        tags=["small", "fast"],
        ollama="gemma3:4b",
        legacy=True,
    ),
    ModelSpec(
        id="gemma-3-12b-q4",
        name="Google Gemma 3 12B Instruct (Q4_K_M)",
        org="Google",
        role="general",
        params_b=12.2, quant="Q4_K_M", size_gb=7.3, context_k=128,
        vram_gb_q4=9.5,
        hf_repo="bartowski/google_gemma-3-12b-it-GGUF",
        hf_file="google_gemma-3-12b-it-Q4_K_M.gguf",
        license="Gemma Terms of Use",
        blurb="Strong writer for 12 GB cards; partial offload on 8 GB.",
        tags=["writing"],
        ollama="gemma3:12b",
        legacy=True,
    ),

]


DEFAULT_MODEL_ID = next(m.id for m in MODELS if m.is_default)

#: The models still recommended (everything not marked legacy).
CURRENT: List[ModelSpec] = [m for m in MODELS if not m.legacy]

#: Best picks by graphics-card memory (GB), best first, for ordinary Council
#: work (analysis, summaries, Connections / Ledger questions) and for reading
#: pictures and scans. A tier applies from its key up to the next key. The
#: 16 GB general pick, Gemma 4 26B-A4B, is an MoE that runs with part of it in
#: RAM; the models that fit the card fully follow it.
TIER_PICKS = {
    "general": {
        0: ("granite-4.2-3b", "gemma-4-e2b", "phi-4-mini"),
        8: ("granite-4.2-8b", "gemma-4-e4b", "olmo-3-7b"),
        12: ("gemma-4-12b", "granite-4.2-8b"),
        16: ("gemma-4-26b-a4b", "gemma-4-12b", "gpt-oss-20b"),
        24: ("gemma-4-31b", "muse-glimmer-30b", "granite-4.2-30b",
             "gemma-4-26b-a4b"),
    },
    "pictures": {
        0: ("gemma-4-e2b",),
        8: ("gemma-4-e4b",),
        12: ("gemma-4-12b",),
        16: ("gemma-4-26b-a4b", "gemma-4-12b"),
        24: ("gemma-4-31b", "muse-glimmer-30b"),
    },
}


# ============================================================
# Lookups + helpers
# ============================================================

def by_id(model_id: str) -> Optional[ModelSpec]:
    """Lookup a ModelSpec by its stable id. Returns None if not found."""
    for m in MODELS:
        if m.id == model_id:
            return m
    return None


def fits(spec: ModelSpec, vram_gb: float) -> bool:
    """True if `spec` is expected to fit in the given VRAM budget at the
    spec's listed quant, with ~1.5 GB headroom for CUDA driver / Tk UI."""
    return spec.vram_gb_q4 + 1.5 <= vram_gb


def serves(spec: ModelSpec, role: str) -> bool:
    """The entry's own role, or one it is listed as good for."""
    return spec.role == role or role in spec.good_for


def fit_kind(spec: ModelSpec, vram_gb: Optional[float],
             ram_gb: Optional[float] = None) -> str:
    """'gpu' (fits() — weights, a 4K window and the margin on the card),
    'partial' (some layers on the card, the rest in system RAM — needs a
    usable GPU and the weights within 60% of RAM), else 'cpu'."""
    if vram_gb and fits(spec, vram_gb):
        return "gpu"
    if vram_gb and vram_gb >= 4 and (not ram_gb
                                     or spec.size_gb <= ram_gb * 0.6):
        return "partial"
    return "cpu"


def tier_picks(vram_gb: Optional[float], role: str = "general"
               ) -> List[ModelSpec]:
    """TIER_PICKS for this card, best first ([] for an unknown role). No
    card (or an unreadable one) gets the smallest tier."""
    table = TIER_PICKS.get(role)
    if not table:
        return []
    vram = vram_gb or 0
    key = max(k for k in table if k <= vram) if vram >= min(table) else \
        min(table)
    return [m for m in (by_id(i) for i in table[key]) if m is not None]


def pick_rank(spec: ModelSpec, vram_gb: Optional[float],
              role: str = "general") -> int:
    """Where `spec` sits in this card's tier picks (0 = best); the picks'
    length for a model that is not one of them."""
    picks = [m.id for m in tier_picks(vram_gb, role)
             or tier_picks(vram_gb, "general")]
    return picks.index(spec.id) if spec.id in picks else len(picks)


def for_vram(vram_gb: float, *, role: str = "general") -> List[ModelSpec]:
    """Return current (not legacy) models that fit a VRAM budget for the
    requested role, best first: this card's tier picks, then by size
    (bigger models within budget are usually better)."""
    out = [m for m in CURRENT if serves(m, role) and fits(m, vram_gb)]
    return sorted(out, key=lambda m: (pick_rank(m, vram_gb, role),
                                      not m.is_default,
                                      -m.effective_params_b))


def ollama_page(spec: ModelSpec) -> str:
    """The model's page in Ollama's library ("" without a tag)."""
    if not spec.ollama:
        return ""
    return "https://ollama.com/library/" + spec.ollama.split(":", 1)[0]


def download_steps(spec: ModelSpec, *, dest: str = "./models") -> str:
    """How to get this model, as text a person can follow or paste: the
    Ollama command, the Hugging Face command for llama-cpp, the picture
    reader a vision model needs there, and where to read about it. The
    Council itself downloads only when the user presses a button."""
    lines = [f"{spec.name}  ({spec.org}, {spec.license}, about "
             f"{spec.size_gb:g} GB download, needs about "
             f"{spec.vram_gb_q4:g} GB of graphics memory)"]
    if spec.ollama:
        lines += ["", "With Ollama (easiest): open a terminal and run",
                  f"    ollama pull {spec.ollama}",
                  "  then press Rescan in the Models tab."]
    if spec.has_hf_file:
        lines += ["", "As a GGUF file for llama-cpp (\"Download & install\" "
                      "in the Models tab does this), or by hand:",
                  f"    hf download {spec.hf_repo} {spec.hf_file} "
                  f"--local-dir {dest}",
                  "  or, without the hf command:",
                  "    " + download_command(spec, dest=dest)]
        if spec.hf_mmproj:
            lines += [f"  To read pictures with llama-cpp, also get "
                      f"{spec.hf_mmproj} from the same repo (every Gemma 4 "
                      f"repo uses this name, so keep it in a folder of its "
                      f"own per model):",
                      f"    hf download {spec.hf_repo} {spec.hf_mmproj} "
                      f"--local-dir {dest}"]
    else:
        lines += ["", "Not offered as a GGUF file here: use Ollama."]
    pages = [p for p in (ollama_page(spec),
                         f"https://huggingface.co/{spec.hf_repo}"
                         if spec.hf_repo else "") if p]
    if pages:
        lines += ["", "More: " + "   ".join(pages)]
    if spec.legacy:
        lines += ["", "Older model: still works, but a newer US model in "
                      "the list does the same job better."]
    return "\n".join(lines)


def download_command(spec: ModelSpec, *, dest: str = "./models") -> str:
    """Return a one-line Python CLI snippet that downloads this GGUF
    via huggingface_hub. Suitable for pasting into a terminal."""
    return (
        f'python -c "from huggingface_hub import hf_hub_download as h; '
        f"h(repo_id='{spec.hf_repo}', filename='{spec.hf_file}', "
        f"local_dir='{dest}')\""
    )


def pretty_table(specs: Optional[List[ModelSpec]] = None,
                 *, include_blurb: bool = True) -> str:
    """Plain-text table for CLI menus and README inclusion. No external
    deps — stdlib string formatting only."""
    rows = specs if specs is not None else MODELS
    if not rows:
        return "(no models in list)"
    header = ("ID", "NAME", "ORG", "SIZE", "CTX", "VRAM~", "LICENSE")
    data = [(
        r.id, r.name, r.org,
        f"{r.size_gb:.1f} GB",
        f"{r.context_k}K",
        f"{r.vram_gb_q4:.1f} GB",
        r.license,
    ) for r in rows]
    widths = [max(len(str(c)) for c in col) for col in zip(*([header] + data))]
    fmt = "  ".join("{:<%d}" % w for w in widths)
    lines = [fmt.format(*header), fmt.format(*("-" * w for w in widths))]
    for r, row in zip(rows, data):
        lines.append(fmt.format(*row))
        if include_blurb:
            lines.append(" " * (widths[0] + 2) + r.blurb)
    return "\n".join(lines)


def markdown_table(specs: Optional[List[ModelSpec]] = None) -> str:
    """GitHub-flavored markdown table for the README block."""
    rows = specs if specs is not None else MODELS
    out = [
        "| ID | Name | Org | Size | Ctx | VRAM (Q4) | License | Notes |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        notes = r.blurb
        if r.is_default:
            notes = "**Default.** " + notes
        out.append(
            f"| `{r.id}` | {r.name} | {r.org} | {r.size_gb:.1f} GB | "
            f"{r.context_k}K | {r.vram_gb_q4:.1f} GB | {r.license} | {notes} |"
        )
    return "\n".join(out)


if __name__ == "__main__":
    # Quick `python model_catalog.py` smoke test — prints the catalog
    # so anyone can eyeball the curated list without booting the GUI.
    print(pretty_table())
    print()
    print(f"Default: {DEFAULT_MODEL_ID}")
    print(f"\nFor a 16 GB VRAM box (general role):")
    for m in for_vram(16.0, role="general"):
        print(f"  • {m.id:<30} {m.name}")
