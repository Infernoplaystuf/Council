"""
model_catalog.py — single source of truth for the curated GGUF model list.

Every model here is US-origin (per the project's US-models-only constraint).
Sized for 16 GB-VRAM cards (RTX 4080 / 5080 / Ada workstation laptops)
running at Q4_K_M, with smaller options for 8 GB cards or CPU-only boxes.

Public API (stable — read by onboarding.py, setup_wizard.py, the GUI's
download dialog, and the README generator):

    MODELS: list[ModelSpec]                         # canonical catalog
    DEFAULT_MODEL_ID: str                           # what the wizard preselects
    by_id(id) -> ModelSpec | None
    for_vram(vram_gb, *, role='general') -> list[ModelSpec]
    fits(spec, vram_gb) -> bool
    download_command(spec) -> str                   # one-line CLI snippet
    pretty_table(specs=None) -> str                 # for CLI wizard / README

Add a model by appending one ModelSpec entry. No other file needs editing —
the wizard and docs read from MODELS directly.

OLLAMA TAGS AND THE 8 GB TIER
Each entry also names its Ollama tag (`ollama`), so the Models tab can say a
candidate is ALREADY on this PC — on the RTX 4070 Laptop this was built on,
the council env has no llama-cpp-python and Ollama is the runtime that works.
`good_for` lists the extra council roles a general model is a strong pick for
("code", "docs"), so a role-aware search finds them. For an 8 GB card with
32 GB of RAM the US candidates are: Llama 3.1 8B (fits fully, native tool
calling), Phi-4 14B and Gemma 3 12B (partial offload — some layers in RAM,
several times slower), and gpt-oss-20b (an MoE: ~3.6B parameters active per
token, so it spills to RAM far more cheaply than a dense model). Nothing here
is downloaded by being listed.
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
# Ordering: defaults first, then by approximate fit ascending.
# When in doubt about a repo/filename, verify on huggingface.co
# before bumping or adding.
# ============================================================

MODELS: List[ModelSpec] = [
    # ─── Default ────────────────────────────────────────────
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
        is_default=True,
        tags=["default", "balanced"],
        ollama="granite3.1-dense:8b",
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
    ),

    ModelSpec(
        id="gpt-oss-20b",
        name="OpenAI gpt-oss-20b (MXFP4)",
        org="OpenAI",
        role="general",
        params_b=20.9, quant="MXFP4", size_gb=12.8, context_k=128,
        vram_gb_q4=14.0,
        hf_repo="ggml-org/gpt-oss-20b-GGUF",
        hf_file="gpt-oss-20b-mxfp4.gguf",
        license="Apache-2.0",
        blurb="Mixture of experts: ~3.6B parameters active per token, so with "
              "the experts in RAM it runs on an 8 GB card + 32 GB RAM (Ollama "
              "places them; llama-cpp-python 0.3.35 offloads whole layers "
              "only). Native tool calling; strong at code.",
        tags=["moe", "tools", "reasoning"],
        ollama="gpt-oss:20b",
        good_for=("code", "docs"),
        active_params_b=3.6,
    ),
]


DEFAULT_MODEL_ID = next(m.id for m in MODELS if m.is_default)


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


def for_vram(vram_gb: float, *, role: str = "general") -> List[ModelSpec]:
    """Return models that fit a VRAM budget for the requested role,
    sorted by recommendation strength (defaults first, then by params
    descending — bigger models within budget are usually preferred)."""
    out = [m for m in MODELS if serves(m, role) and fits(m, vram_gb)]
    return sorted(out, key=lambda m: (not m.is_default,
                                      -m.effective_params_b))


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
