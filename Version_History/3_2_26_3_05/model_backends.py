# ============================================================
# Conda env:
#   conda create -n council python=3.11 requests pyyaml -y
#   conda activate council
# ============================================================

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol
import os
import requests
import json

Message = Dict[str, str]  # {"role": "system"|"user"|"assistant", "content": "..."}

class ModelBackend(Protocol):
    def generate(self, messages: List[Message], *, temperature: float = 0.3, max_tokens: int = 1200) -> str: ...


@dataclass
class StaticEchoBackend:
    """Dev backend: echoes last user message."""
    name: str = "echo"

    def generate(self, messages: List[Message], *, temperature: float = 0.0, max_tokens: int = 400) -> str:
        last_user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        return f"[{self.name} ECHO]\n{last_user}"


@dataclass
class OpenAIChatCompletionsBackend:
    """
    For any OpenAI-compatible /v1/chat/completions endpoint.
    Works with:
      - OpenAI API
      - many self-hosted “OpenAI-compatible” servers
    """
    base_url: str
    api_key: str
    model: str
    timeout_s: int = 120

    def generate(self, messages: List[Message], *, temperature: float = 0.3, max_tokens: int = 1200) -> str:
        url = self.base_url.rstrip("/") + "/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
        }
        r = requests.post(url, headers=headers, json=payload, timeout=self.timeout_s)
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"]


@dataclass
class OllamaBackend:
    """
    Ollama local endpoint: http://localhost:11434/api/chat
    """
    model: str
    host: str = "http://localhost:11434"
    timeout_s: int = 120

    def generate(self, messages: List[Message], *, temperature: float = 0.3, max_tokens: int = 1200) -> str:
        url = self.host.rstrip("/") + "/api/chat"
        payload = {
            "model": self.model,
            "messages": messages,
            "options": {"temperature": float(temperature), "num_predict": int(max_tokens)},
            "stream": False,
        }
        r = requests.post(url, json=payload, timeout=self.timeout_s)
        r.raise_for_status()
        data = r.json()
        return data["message"]["content"]


class BackendRegistry:
    """
    Holds backends by string key, so your personality config can reference them.
    """
    def __init__(self):
        self._backends: Dict[str, ModelBackend] = {}

    def register(self, key: str, backend: ModelBackend) -> None:
        self._backends[key] = backend

    def get(self, key: str) -> ModelBackend:
        if key not in self._backends:
            raise KeyError(f"Backend not found: {key}")
        return self._backends[key]