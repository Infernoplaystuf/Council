"""
Thinking models get a thinking LEVEL and room for it.

gpt-oss cannot switch thinking off — only turn it down — and Ollama's default
is "medium". Its thinking tokens count against num_predict, so a structured
reply (a wireframe, a function) was cut off mid-object while the model was
still thinking (measured: 285 tokens spent thinking on a short warm call).
No model is run: tests/fake_ollama.py records what the engine sends.
"""
from __future__ import annotations

import sys

import pytest

from tests.fake_ollama import DEFAULT_TAGS, FakeOllama, tag

MSGS = [{"role": "user", "content": "hello"}]
GPT_OSS = tag("gpt-oss:20b", size=13_793_441_244, family="gptoss",
              params="20.9B", quant="MXFP4",
              caps=("completion", "tools", "thinking"))
QWEN3 = tag("qwen3:8b", size=5_225_388_164, family="qwen3", params="8.2B",
            quant="Q4_K_M", caps=("completion", "tools", "thinking"))


@pytest.fixture
def eng(tmp_path, monkeypatch):
    import council_engine as ce
    from council_core import local_models
    with FakeOllama(tags=DEFAULT_TAGS + [GPT_OSS, QWEN3]) as fake:
        vault = tmp_path / "vault"
        vault.mkdir()
        monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
        monkeypatch.setenv("COUNCIL_OLLAMA_HOST", fake.url)
        for var in ("COUNCIL_GGUF_PATH", "COUNCIL_BACKEND",
                    "COUNCIL_OLLAMA_MODEL", "COUNCIL_OLLAMA_THINK",
                    "COUNCIL_OLLAMA_THINK_HEADROOM", "COUNCIL_REMOTE_NODES"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setitem(sys.modules, "llama_cpp", None)
        monkeypatch.setattr(ce, "_hardware_gb", lambda: (8.0, 31.7))
        local_models.invalidate_cache()
        ce.refresh_backend_config()
        yield ce, fake
        ce.refresh_backend_config()
        local_models.invalidate_cache()


def _entry(name, family, caps):
    return {"name": name, "family": family, "capabilities": list(caps)}


def test_the_policy_by_model(monkeypatch):
    import council_engine as ce
    monkeypatch.delenv("COUNCIL_OLLAMA_THINK", raising=False)
    gpt = _entry("gpt-oss:20b", "gptoss", ["completion", "thinking"])
    assert ce.ollama_think(gpt) == "low"                  # Ollama's is medium
    assert ce.ollama_think(_entry("qwen3:8b", "qwen3",
                                  ["completion", "thinking"])) is False
    assert ce.ollama_think(_entry("llama3.1:8b", "llama",
                                  ["completion", "tools"])) is None
    assert ce.ollama_think(None) is None
    monkeypatch.setenv("COUNCIL_OLLAMA_THINK", "high")
    assert ce.ollama_think(gpt) == "high"
    monkeypatch.setenv("COUNCIL_OLLAMA_THINK", "on")
    assert ce.ollama_think(_entry("qwen3:8b", "qwen3", ["thinking"])) is True
    assert ce.ollama_think(gpt) == "low"            # not a level: stays low


def test_gpt_oss_is_sent_low_and_given_room_to_think(eng):
    ce, fake = eng
    ce.local_chat(MSGS, model="ollama:gpt-oss:20b", num_predict=400)
    body = fake.state.chats[-1]
    assert body["think"] == "low"
    assert body["options"]["num_predict"] == 400 + ce._think_headroom()


def test_a_model_that_can_stop_thinking_is_told_to(eng):
    ce, fake = eng
    ce.local_chat(MSGS, model="ollama:qwen3:8b", num_predict=400)
    body = fake.state.chats[-1]
    assert body["think"] is False
    assert body["options"]["num_predict"] == 400            # no headroom


def test_a_model_that_does_not_think_gets_no_field(eng):
    ce, fake = eng
    ce.local_chat(MSGS, model="ollama:llama3.1:8b", num_predict=400)
    body = fake.state.chats[-1]
    assert "think" not in body
    assert body["options"]["num_predict"] == 400
