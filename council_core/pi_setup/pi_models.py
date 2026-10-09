"""Which model a Pi should run — US-origin only.

The Apothecary's PI_MODEL_RECOMMENDATIONS (apothecary_engine.py) lists
qwen2.5 first for every Pi and its DEFAULT_MODEL is qwen2.5:3b; the Council
recommends only US-origin models (Llama, Phi, Gemma, Granite, OLMo,
gpt-oss). This table is what the Pi setup offers. Sizes are the Ollama q4
downloads; a Pi needs roughly the model size plus ~1 GB free, so each entry
fits the RAM it is listed under. The KG0 benchmark decides which of these is
good enough for knowledge-graph extraction; until then the first entry is the
default.

The Council never downloads models — with ONE exception the user approved on
2026-10-07, for Pi setup only: the wizard names the model, its maker and its
download size (`describe`) and runs `ollama pull` on the Pi only when the user
presses "Download on the Pi". Setup itself never pulls a model.
"""
from __future__ import annotations

from typing import List, Optional

#: RAM (GB) -> models, best first.
BY_RAM = {
    16: ["llama3.1:8b", "granite3.3:8b", "gemma3:12b"],
    8: ["llama3.1:8b", "granite3.3:8b", "gemma3:4b"],
    4: ["llama3.2:3b", "granite3.3:2b", "gemma3:1b"],
    2: ["llama3.2:1b", "gemma3:1b"],
}
US_FAMILIES = ("llama", "phi", "gemma", "granite", "olmo", "gpt-oss")

#: Who makes each family — the wizard names it beside the model before the
#: user decides to download it on the Pi.
MAKERS = {"llama": "Meta", "phi": "Microsoft", "gemma": "Google", "granite": "IBM",
          "olmo": "Ai2", "gpt-oss": "OpenAI"}

#: Download size of each offered model, bytes, as the Ollama library lists it
#: (q4 builds; library pages read 2026-10, rounded as listed there; NOT
#: measured on a Pi). llama3.1:8b is exact: this PC's /api/tags reports
#: 4,920,753,328 bytes (tests/fake_ollama.DEFAULT_TAGS). Shown as "about".
DOWNLOAD_BYTES = {
    "llama3.1:8b": 4_920_753_328,
    "llama3.2:3b": 2_000_000_000,
    "llama3.2:1b": 1_300_000_000,
    "granite3.3:8b": 4_900_000_000,
    "granite3.3:2b": 1_500_000_000,
    "gemma3:12b": 8_100_000_000,
    "gemma3:4b": 3_300_000_000,
    "gemma3:1b": 815_000_000,
}


def for_ram(ram_gb: float) -> List[str]:
    for size in sorted(BY_RAM, reverse=True):
        if ram_gb >= size * 0.9:          # a "4 GB" Pi reports ~3.7 GB
            return list(BY_RAM[size])
    return list(BY_RAM[2])


def is_us_origin(model: str) -> bool:
    return model.split("/")[-1].lower().startswith(US_FAMILIES)


def maker(model: str) -> str:
    name = model.split("/")[-1].lower()
    return next((m for fam, m in MAKERS.items() if name.startswith(fam)), "")


def download_size(model: str) -> Optional[int]:
    return DOWNLOAD_BYTES.get(model)


def describe(model: str) -> str:
    """'llama3.2:3b (Meta, US) — about 2.0 GB to download on the Pi': what
    the user is told before pressing "Download on the Pi"."""
    who = maker(model)
    size = download_size(model)
    return (f"{model} ({who}, US)" if who else model) + (
        f" — about {size / 1e9:.1f} GB to download on the Pi" if size
        else " — download size not known to the Council")
