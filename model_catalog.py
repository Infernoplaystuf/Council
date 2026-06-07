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
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


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
    ),

    # ─── Meta Llama family ─────────────────────────────────
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
        blurb="Best reasoning at this VRAM tier. Pick for analyst Q&A on dense data.",
        tags=["reasoning", "recommended-16gb"],
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
    ),

    # ─── Coder models (Council's Coder role) ───────────────
    ModelSpec(
        id="granite-3-8b-code-q4",
        name="IBM Granite 3.0 8B Code Instruct (Q4_K_M)",
        org="IBM",
        role="code",
        params_b=8.0, quant="Q4_K_M", size_gb=4.6, context_k=128,
        vram_gb_q4=6.5,
        hf_repo="bartowski/granite-3.0-8b-instruct-GGUF",
        hf_file="granite-3.0-8b-instruct-Q4_K_M.gguf",
        license="Apache-2.0",
        blurb="Code-friendly IBM model. Wire into the Coder role if you have RAM headroom for a second hot model.",
        tags=["code"],
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


def for_vram(vram_gb: float, *, role: str = "general") -> List[ModelSpec]:
    """Return models that fit a VRAM budget for the requested role,
    sorted by recommendation strength (defaults first, then by params
    descending — bigger models within budget are usually preferred)."""
    out = [m for m in MODELS if m.role == role and fits(m, vram_gb)]
    return sorted(out, key=lambda m: (not m.is_default, -m.params_b))


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
