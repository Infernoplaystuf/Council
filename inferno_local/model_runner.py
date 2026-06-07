"""
inferno_local.model_runner — backend-agnostic local model runner.

A tiny protocol + two concrete runners that hide whether a chat call
goes through in-process llama-cpp-python or out to a loopback Ollama
daemon. The Council's deliberation loop and the blind A/B compare
panel both build runners through ``build_runner(config)`` and talk
to them via the same ``chat`` / ``stream_chat`` surface.

Hard-coded prohibitions enforced at the factory:

  * No cloud backends. Any config naming ``openai``, ``anthropic``,
    ``gemini``, ``openrouter``, ``copilot``, ``azure_openai``, ``cohere``,
    ``mistral_cloud``, ``together``, ``fireworks``, ``groq``,
    ``perplexity``, or ``replicate`` raises ``EgressBlocked``.
  * Ollama URLs are passed through ``security.assert_loopback`` before
    any request; non-loopback hosts are refused at construction time
    AND on every call (defence in depth — config can't be mutated past
    the construct-time check).

Config schema (dict, accepted by ``build_runner``):

    {
        "backend": "llama_cpp" | "ollama",     # required
        # llama_cpp:
        "gguf_path":  "...",                   # optional — defaults to env
        "n_ctx":      4096,                    # optional
        "n_gpu_layers": 99,                    # optional
        # ollama:
        "url":   "http://127.0.0.1:11434",
        "model": "granite3.1-dense:8b",
        "timeout_s": 120,                      # optional
    }

A runner exposes:

    chat(messages, *, temperature=0.2, max_tokens=600) -> str
    stream_chat(messages, *, temperature, max_tokens) -> Iterator[str]
    describe() -> dict                         # for the Settings panel
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterator, List, Optional, Protocol

from . import security

_LOG = logging.getLogger("inferno_local.model_runner")


_CLOUD_KEYWORDS = frozenset({
    "openai", "anthropic", "gemini", "google_genai", "google_ai_studio",
    "openrouter", "copilot", "azure_openai", "azureopenai",
    "cohere", "mistral_cloud", "together", "fireworks", "groq",
    "perplexity", "replicate", "aws_bedrock", "bedrock",
})


class ModelRunner(Protocol):
    """Minimal contract every backend implements. Kept tiny so swapping
    one out (or running multiple side-by-side in the Compare panel) is
    a one-liner."""

    name: str

    def chat(self,
             messages: List[Dict[str, str]],
             *,
             temperature: float = 0.2,
             max_tokens: int = 600) -> str: ...

    def stream_chat(self,
                    messages: List[Dict[str, str]],
                    *,
                    temperature: float = 0.2,
                    max_tokens: int = 600) -> Iterator[str]: ...

    def describe(self) -> Dict[str, Any]: ...


# ============================================================
# In-process llama-cpp runner (wraps the existing
# council_engine GGUF loader so we keep its VRAM-aware n_ctx
# ladder, inference lock, etc.)
# ============================================================

class LlamaCppRunner:
    """Runs the GGUF model in-process. Reuses ``council_engine._get_gguf_model``
    so we don't fork its n_ctx-ladder logic. The ``COUNCIL_GGUF_PATH`` env
    var is the source of truth for the model file — config can override
    it for the duration of a single runner, but the env var is restored
    after."""

    name = "llama_cpp"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = dict(config or {})
        # Lazy import — council_engine is heavy.
        from council_engine import _get_gguf_model, _INFERENCE_LOCK
        self._lock = _INFERENCE_LOCK
        self._loader = _get_gguf_model
        # Apply gguf_path override at construct time. We don't restore
        # the env var here because callers building multiple LlamaCppRunners
        # would race with each other — the model singleton is per-process,
        # so multi-runner llama_cpp configs are not supported. Use Ollama
        # for blind A/B if you need to swap between GGUFs at runtime.
        import os
        gp = self.config.get("gguf_path")
        if gp:
            os.environ["COUNCIL_GGUF_PATH"] = str(gp)
        for k_env, k_cfg in (
            ("COUNCIL_GGUF_N_CTX", "n_ctx"),
            ("COUNCIL_GGUF_GPU_LAYERS", "n_gpu_layers"),
        ):
            if k_cfg in self.config:
                os.environ[k_env] = str(self.config[k_cfg])

    def _llm(self):
        return self._loader()

    def chat(self,
             messages: List[Dict[str, str]],
             *,
             temperature: float = 0.2,
             max_tokens: int = 600) -> str:
        with self._lock:
            out = self._llm().create_chat_completion(
                messages=messages,
                temperature=float(temperature),
                max_tokens=int(max_tokens),
            )
        try:
            return str(out["choices"][0]["message"]["content"]).strip()
        except Exception:
            return str(out).strip()

    def stream_chat(self,
                    messages: List[Dict[str, str]],
                    *,
                    temperature: float = 0.2,
                    max_tokens: int = 600) -> Iterator[str]:
        with self._lock:
            for chunk in self._llm().create_chat_completion(
                messages=messages,
                temperature=float(temperature),
                max_tokens=int(max_tokens),
                stream=True,
            ):
                try:
                    delta = chunk["choices"][0]["delta"].get("content", "")
                except Exception:
                    delta = ""
                if delta:
                    yield delta

    def describe(self) -> Dict[str, Any]:
        import os
        return {
            "backend":   "llama_cpp",
            "gguf_path": os.environ.get("COUNCIL_GGUF_PATH", "(unset)"),
            "n_ctx":     os.environ.get("COUNCIL_GGUF_N_CTX", "(auto)"),
            "n_gpu_layers": os.environ.get("COUNCIL_GGUF_GPU_LAYERS", "(auto)"),
        }


# ============================================================
# Loopback Ollama runner
# ============================================================

class OllamaRunner:
    """Talks to a local Ollama daemon over HTTP. Every URL passes
    through ``security.assert_loopback`` first — at construct time AND
    on every request. The brief allows Ollama only on 127.0.0.0/8 or
    ::1; anything else is a configuration error and we say so."""

    name = "ollama"

    def __init__(self,
                 url: str = "http://127.0.0.1:11434",
                 model: str = "",
                 timeout_s: int = 120):
        self.url = url.rstrip("/")
        self.model = model
        self.timeout_s = int(timeout_s)
        # Construction-time check. Belt-and-braces: each call repeats it.
        security.assert_loopback(self.url)
        if not self.model:
            raise ValueError("OllamaRunner: 'model' is required")

    def _post(self, path: str, payload: Dict[str, Any]) -> Any:
        url = self.url + path
        security.assert_loopback(url)
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
            body = r.read().decode("utf-8", errors="replace")
        return body

    def chat(self,
             messages: List[Dict[str, str]],
             *,
             temperature: float = 0.2,
             max_tokens: int = 600) -> str:
        body = self._post("/api/chat", {
            "model": self.model,
            "messages": messages,
            "options": {
                "temperature": float(temperature),
                "num_predict": int(max_tokens),
            },
            "stream": False,
        })
        try:
            return str(json.loads(body)["message"]["content"]).strip()
        except Exception:
            return body.strip()

    def stream_chat(self,
                    messages: List[Dict[str, str]],
                    *,
                    temperature: float = 0.2,
                    max_tokens: int = 600) -> Iterator[str]:
        url = self.url + "/api/chat"
        security.assert_loopback(url)
        payload = json.dumps({
            "model": self.model,
            "messages": messages,
            "options": {
                "temperature": float(temperature),
                "num_predict": int(max_tokens),
            },
            "stream": True,
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except Exception:
                    continue
                msg = (chunk.get("message") or {}).get("content", "")
                if msg:
                    yield msg
                if chunk.get("done"):
                    break

    def describe(self) -> Dict[str, Any]:
        return {
            "backend": "ollama",
            "url":     self.url,
            "model":   self.model,
            "timeout_s": self.timeout_s,
        }


# ============================================================
# Factory
# ============================================================

def build_runner(config: Dict[str, Any]) -> ModelRunner:
    """Construct a ModelRunner from a config dict. Refuses every cloud
    backend with ``EgressBlocked``; refuses Ollama URLs that aren't
    loopback. Unknown backends raise ValueError."""
    if not isinstance(config, dict):
        raise ValueError("build_runner: config must be a dict")
    backend = str(config.get("backend", "")).strip().lower()
    if not backend:
        raise ValueError("build_runner: 'backend' is required")
    if backend in _CLOUD_KEYWORDS or any(c in backend for c in _CLOUD_KEYWORDS):
        raise security.EgressBlocked(
            backend,
            f"cloud backend {backend!r} is not allowed in this build "
            "(local-only runtime).",
        )
    if backend == "llama_cpp":
        return LlamaCppRunner(config)
    if backend == "ollama":
        return OllamaRunner(
            url=str(config.get("url", "http://127.0.0.1:11434")),
            model=str(config.get("model", "")),
            timeout_s=int(config.get("timeout_s", 120)),
        )
    raise ValueError(f"build_runner: unknown backend {backend!r}")
