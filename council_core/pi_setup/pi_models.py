"""Which model a Pi should run — US-origin only.

The Apothecary's PI_MODEL_RECOMMENDATIONS (apothecary_engine.py) lists
qwen2.5 first for every Pi and its DEFAULT_MODEL is qwen2.5:3b; the Council
recommends only US-origin models (Llama, Phi, Gemma, Granite, OLMo,
gpt-oss). This table is what the Pi setup offers. Sizes are the Ollama q4
downloads; a Pi needs roughly the model size plus ~1 GB free, so each entry
fits the RAM it is listed under. The KG0 benchmark decides which of these is
good enough for knowledge-graph extraction; until then the first entry is the
default.
"""
from __future__ import annotations

from typing import List

#: RAM (GB) -> models, best first.
BY_RAM = {
    16: ["llama3.1:8b", "granite3.3:8b", "gemma3:12b"],
    8: ["llama3.1:8b", "granite3.3:8b", "gemma3:4b"],
    4: ["llama3.2:3b", "granite3.3:2b", "gemma3:1b"],
    2: ["llama3.2:1b", "gemma3:1b"],
}
US_FAMILIES = ("llama", "phi", "gemma", "granite", "olmo", "gpt-oss")


def for_ram(ram_gb: float) -> List[str]:
    for size in sorted(BY_RAM, reverse=True):
        if ram_gb >= size * 0.9:          # a "4 GB" Pi reports ~3.7 GB
            return list(BY_RAM[size])
    return list(BY_RAM[2])


def is_us_origin(model: str) -> bool:
    return model.split("/")[-1].lower().startswith(US_FAMILIES)
