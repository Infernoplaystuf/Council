"""A small OpenAI-compatible chat client on the standard library.

Local only: a URL whose host is not this machine is refused unless
``llm.allow_remote`` is set — nothing leaves the PC by default.
Timeouts, retries with back-off, and strict JSON replies (``chat_json``
re-asks once, quoting the parse error, before giving up).
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Protocol
from urllib.parse import urlparse

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


class LLMError(RuntimeError):
    """The LLM could not give a usable answer (down, slow, or malformed)."""


class ChatModel(Protocol):
    def chat(self, messages: List[Dict[str, str]], *, json_mode: bool = False,
             max_tokens: Optional[int] = None) -> str: ...


def extract_json(text: str) -> Dict[str, Any]:
    """The JSON object in a model reply. Accepts a bare object, one inside
    a ``` fence, or one object surrounded by prose; anything else — no
    object, two objects, an array, NaN, truncated output — is an error."""
    if not isinstance(text, str) or not text.strip():
        raise LLMError("empty reply")
    s = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    if fence:
        s = fence.group(1).strip()
    if s.startswith("["):
        raise LLMError("reply JSON is not an object")
    if not s.startswith("{"):
        start, end = s.find("{"), s.rfind("}")
        if start < 0 or end <= start:
            raise LLMError("no JSON object in the reply")
        s = s[start:end + 1]

    def no_constants(name: str):
        raise ValueError(f"{name} is not valid JSON")
    try:
        obj = json.loads(s, parse_constant=no_constants)
    except (json.JSONDecodeError, ValueError) as exc:
        raise LLMError(f"reply is not valid JSON: {exc}") from None
    if not isinstance(obj, dict):
        raise LLMError("reply JSON is not an object")
    return obj


class OpenAIClient:
    """POST {url}/chat/completions; ``url`` is the base, e.g.
    http://localhost:11434/v1 (Ollama) or http://127.0.0.1:8080/v1."""

    def __init__(self, url: str, model: str, temperature: float = 0.0,
                 timeout_s: float = 180, retries: int = 2,
                 max_tokens: int = 400, allow_remote: bool = False,
                 backoff_s: float = 2.0):
        host = urlparse(url).hostname or ""
        if host not in LOCAL_HOSTS and not allow_remote:
            raise LLMError(f"LLM url {url!r} is not on this machine; set "
                           "llm.allow_remote to opt in")
        self.url = url.rstrip("/")
        self.model, self.temperature = model, temperature
        self.timeout_s, self.retries = timeout_s, retries
        self.max_tokens, self.backoff_s = max_tokens, backoff_s

    @classmethod
    def from_config(cls, llm: Dict[str, Any]) -> "OpenAIClient":
        return cls(url=llm["url"], model=llm["model"],
                   temperature=llm.get("temperature", 0.0),
                   timeout_s=llm.get("timeout_s", 180),
                   retries=llm.get("retries", 2),
                   max_tokens=llm.get("max_tokens", 400),
                   allow_remote=bool(llm.get("allow_remote", False)))

    def chat(self, messages: List[Dict[str, str]], *, json_mode: bool = False,
             max_tokens: Optional[int] = None) -> str:
        body: Dict[str, Any] = {
            "model": self.model, "messages": messages,
            "temperature": self.temperature,
            "max_tokens": max_tokens or self.max_tokens, "stream": False}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        data = json.dumps(body).encode()
        last: Exception = LLMError("no attempt made")
        for attempt in range(self.retries + 1):
            if attempt:
                time.sleep(self.backoff_s * 2 ** (attempt - 1))
            req = urllib.request.Request(
                self.url + "/chat/completions", data=data,
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                    reply = json.loads(r.read().decode())
                content = reply["choices"][0]["message"]["content"]
                if not isinstance(content, str):
                    raise LLMError("reply has no text content")
                return content
            except urllib.error.HTTPError as exc:
                last = exc
                if 400 <= exc.code < 500 and exc.code != 429:
                    break                     # our request is wrong; no retry
            except (urllib.error.URLError, TimeoutError, OSError,
                    json.JSONDecodeError, KeyError, IndexError, TypeError,
                    LLMError) as exc:
                last = exc
        raise LLMError(f"LLM request failed: {last}")


def chat_json(llm: ChatModel, messages: List[Dict[str, str]],
              max_tokens: Optional[int] = None) -> tuple[Dict[str, Any], str]:
    """(parsed object, raw text). One re-ask on a malformed reply."""
    raw = llm.chat(messages, json_mode=True, max_tokens=max_tokens)
    try:
        return extract_json(raw), raw
    except LLMError as exc:
        retry = messages + [
            {"role": "assistant", "content": raw[:2000]},
            {"role": "user", "content": f"That was not usable ({exc}). "
             "Reply with ONE JSON object only, no other text."}]
        raw = llm.chat(retry, json_mode=True, max_tokens=max_tokens)
        return extract_json(raw), raw
