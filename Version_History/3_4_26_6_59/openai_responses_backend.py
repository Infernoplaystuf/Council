# ============================================================
# Conda env:
#   conda create -n council python=3.11 requests -y
#   conda activate council
# ============================================================

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional
import os
import requests

Message = Dict[str, str]  # {"role": "developer"|"user"|"assistant"|"system", "content": "..."}


def _extract_output_text(resp_json: Dict[str, Any]) -> str:
    """
    Robust extraction for direct REST calls.
    The Responses REST API returns an 'output' array containing content items, often type 'output_text'.
    (SDKs provide response.output_text convenience; we replicate that.)
    """
    chunks: List[str] = []
    for item in resp_json.get("output", []) or []:
        for c in item.get("content", []) or []:
            if c.get("type") == "output_text":
                t = c.get("text", "")
                if t:
                    chunks.append(t)
    return "".join(chunks).strip()


@dataclass
class OpenAIResponsesBackend:
    """
    Calls POST https://api.openai.com/v1/responses (or a custom base_url).
    Uses 'input' as an array of role/content messages, matching the docs examples.
    """
    name: str
    model: str
    api_key: Optional[str] = None
    base_url: str = "https://api.openai.com/v1"
    timeout_s: int = 120

    def generate(
        self,
        *,
        developer_instructions: str,
        user_text: str,
        temperature: float = 0.3,
        max_output_tokens: int = 1200,
        reasoning: Optional[Dict[str, Any]] = None,
    ) -> str:
        api_key = self.api_key or os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set (env var) and no api_key was provided.")

        url = self.base_url.rstrip("/") + "/responses"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        # Per docs, you can provide message array inputs with roles like developer/user
        payload: Dict[str, Any] = {
            "model": self.model,
            "input": [
                {"role": "developer", "content": developer_instructions},
                {"role": "user", "content": user_text},
            ],
            "temperature": float(temperature),
            "max_output_tokens": int(max_output_tokens),
        }

        # Reasoning is supported for some models (gpt-5 / o-series); keep optional.
        if reasoning:
            payload["reasoning"] = reasoning

        r = requests.post(url, headers=headers, json=payload, timeout=self.timeout_s)
        r.raise_for_status()
        data = r.json()

        out = _extract_output_text(data)
        # If empty (rare), fall back to returning the whole JSON as string for debugging
        return out if out else str(data)