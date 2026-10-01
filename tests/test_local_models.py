"""
council_core.local_models — what this PC can run, read without loading a
model: Ollama's metadata (from tests/fake_ollama.py, never the real server),
GGUF headers (a synthetic file), origin, and the per-role ranking that only
ever recommends US-origin models.
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from council_core import local_models as lm
from tests.fake_ollama import DEFAULT_TAGS, FakeOllama, tag

GB = 1024 ** 3


@pytest.fixture
def fake(monkeypatch):
    with FakeOllama() as srv:
        monkeypatch.setenv("COUNCIL_OLLAMA_HOST", srv.url)
        lm.invalidate_cache()
        yield srv
    lm.invalidate_cache()


# ============================================================
# Origin
# ============================================================

@pytest.mark.parametrize("name, family, maker, origin", [
    ("llama3.1:8b", "llama", "Meta", "US"),
    ("phi3.5:latest", "phi3", "Microsoft", "US"),
    ("phi4:14b", "phi3", "Microsoft", "US"),
    ("gpt-oss:20b", "gptoss", "OpenAI", "US"),
    ("gemma3:4b", "gemma3", "Google", "US"),
    ("granite3.1-dense:8b", "granite", "IBM", "US"),
    ("qwen2.5-coder:7b", "qwen2", "Alibaba", "non-US"),
    ("mistral:7b", "llama", "Mistral AI (France)", "non-US"),
    ("deepseek-r1:8b", "llama", "DeepSeek", "non-US"),    # a llama distill
    ("mystery:1b", "weird", "unknown", "unknown"),
])
def test_maker_and_origin(name, family, maker, origin):
    assert lm.maker_and_origin(name, family) == (maker, origin)


def test_origin_labels():
    assert lm.origin_label("US") == "US"
    assert "measure only" in lm.origin_label("non-US")


# ============================================================
# Ollama metadata
# ============================================================

def test_list_reads_tags_without_any_generation(fake):
    models = lm.list_local_models(include_gguf=False)
    assert [m["id"] for m in models] == [
        "ollama:llama3.1:8b", "ollama:phi3.5:latest",
        "ollama:qwen2.5:7b-instruct-q4_K_M"]
    llama = models[0]
    assert llama["context_length"] == 131072
    assert llama["capabilities"] == ["completion", "tools"]
    assert fake.state.chats == []
    # Ollama 0.35 puts context + capabilities in /api/tags: no /api/show.
    assert not any(p == "/api/show" for _m, p, _b in fake.state.requests)


def test_an_older_server_is_asked_api_show_once_per_digest(fake):
    old = dict(DEFAULT_TAGS[0])
    old.pop("capabilities")
    old["details"] = dict(old["details"])
    old["details"].pop("context_length")
    fake.state.tags = [old]
    for _ in range(3):
        m = lm.ollama_entry(old)                 # e.g. three panel rescans
    assert m["context_length"] == 8192           # from model_info
    assert m["capabilities"] == ["completion"]
    shows = [p for _m, p, _b in fake.state.requests if p == "/api/show"]
    assert len(shows) == 1


def test_no_server_means_no_models_and_no_error(monkeypatch):
    monkeypatch.setenv("COUNCIL_OLLAMA_HOST", "http://127.0.0.1:9")
    lm.invalidate_cache()
    assert lm.list_local_models(include_gguf=False) == []
    assert lm.ollama_reachable() is False


def test_a_remote_host_is_refused_unless_allowed():
    with pytest.raises(RuntimeError, match="non-local"):
        lm.ollama_tags("http://10.0.0.5:11434")
    assert lm.ollama_reachable("http://10.0.0.5:11434") is False


def test_latest_is_the_same_model(fake):
    assert lm.ollama_model("phi3.5")["name"] == "phi3.5:latest"


def test_an_embedding_only_model_is_not_listed(fake):
    fake.state.tags = DEFAULT_TAGS + [tag(
        "nomic-embed-text", size=274_000_000, family="nomic-bert",
        params="137M", quant="F16", caps=("embedding",))]
    names = [m["name"] for m in lm.list_local_models(include_gguf=False)]
    assert "nomic-embed-text" not in names


# ============================================================
# GGUF headers
# ============================================================

def _gguf(path: Path, *, layers: int = 4, block: int = 1000,
          other: int = 600) -> Path:
    """A tiny v3 GGUF: llama metadata and tensors whose offsets give each
    block ``block`` bytes and the embeddings ``other``."""
    def s(x: str) -> bytes:
        b = x.encode()
        return struct.pack("<Q", len(b)) + b

    kv = [("general.architecture", 8, s("llama")),
          ("general.name", 8, s("Meta Llama 3.1 8B Instruct")),
          ("general.file_type", 4, struct.pack("<I", 15)),
          ("llama.block_count", 4, struct.pack("<I", layers)),
          ("llama.context_length", 4, struct.pack("<I", 131072)),
          ("tokenizer.ggml.tokens", 9,
           struct.pack("<IQ", 8, 2) + s("a") + s("bb"))]
    tensors = [("token_embd.weight", other)]
    for i in range(layers):
        tensors += [(f"blk.{i}.attn_q.weight", block // 2),
                    (f"blk.{i}.ffn_up.weight", block - block // 2)]
    head = b"GGUF" + struct.pack("<IQQ", 3, len(tensors), len(kv))
    for key, t, val in kv:
        head += s(key) + struct.pack("<I", t) + val
    offset = 0
    for name, size in tensors:
        head += s(name) + struct.pack("<I", 1) + struct.pack("<Q", size)
        head += struct.pack("<I", 0) + struct.pack("<Q", offset)
        offset += size
    head += b"\0" * ((-len(head)) % 32)
    path.write_bytes(head + b"\0" * offset)
    return path


def test_the_header_gives_layers_and_their_sizes(tmp_path):
    lay = lm.read_gguf(_gguf(tmp_path / "m.gguf"), tensors=True)
    assert lay["metadata"]["llama.block_count"] == 4
    assert lm.layer_bytes(lay) == (4, 1000, 600)


def test_a_gguf_entry(tmp_path):
    e = lm.gguf_entry(_gguf(tmp_path / "Meta-Llama-3.1-8B-Q4_K_M.gguf"))
    assert e["backend"] == "gguf" and Path(e["id"]).is_absolute()
    assert e["maker"] == "Meta" and e["origin"] == "US"
    assert e["quant"] == "Q4_K_M" and e["params_b"] == 8.0
    assert e["context_length"] == 131072 and e["family"] == "llama"


def test_not_a_gguf(tmp_path):
    p = tmp_path / "x.gguf"
    p.write_bytes(b"nope")
    assert lm.read_gguf(p) == {}


# ============================================================
# Ranking — US only, a full GPU fit first
# ============================================================

def _entries():
    return [lm.ollama_entry(t, fill_from_show=False) for t in DEFAULT_TAGS
            ] + [lm.ollama_entry(tag(
                "phi4:14b", size=9_053_116_391, family="phi3",
                params="14.7B", quant="Q4_K_M", ctx=16384),
                fill_from_show=False),
                lm.ollama_entry(tag(
                    "gpt-oss:20b", size=13_793_441_244, family="gptoss",
                    params="20.9B", quant="MXFP4",
                    caps=("completion", "tools", "thinking")),
                    fill_from_show=False)]


def test_on_an_8gb_laptop_the_8b_that_fits_wins():
    """Measured hardware: RTX 4070 Laptop 8 GB, 31.7 GB RAM."""
    ranked = lm.rank_for_role(_entries(), "writer", vram_gb=8.0, ram_gb=31.7)
    assert ranked[0]["name"] == "llama3.1:8b" and ranked[0]["fit"] == "gpu"
    fits = {e["name"]: e["fit"] for e in ranked}
    assert fits["phi4:14b"] == "partial" and fits["gpt-oss:20b"] == "partial"
    # The MoE spills more cheaply than the dense 14B, so it ranks above it.
    names = [e["name"] for e in ranked]
    assert names.index("gpt-oss:20b") < names.index("phi4:14b")


def test_non_us_models_are_never_ranked():
    for role in ("writer", "coder", "docs"):
        names = [e["name"] for e in lm.rank_for_role(_entries(), role,
                                                     vram_gb=8.0,
                                                     ram_gb=31.7)]
        assert not any("qwen" in n for n in names)


def test_docs_prefers_native_tool_calling():
    ranked = lm.rank_for_role(_entries(), "docs", vram_gb=8.0, ram_gb=31.7)
    assert ranked[0]["name"] == "llama3.1:8b"
    assert "native tool calling" in ranked[0]["why"]


def test_a_big_card_ranks_the_bigger_model_first():
    ranked = lm.rank_for_role(_entries(), "writer", vram_gb=24.0, ram_gb=64)
    assert ranked[0]["name"] == "phi4:14b"


def test_effective_params_of_an_moe():
    gpt = [e for e in _entries() if e["name"] == "gpt-oss:20b"][0]
    assert lm.effective_params_b(gpt) == pytest.approx(8.7, abs=0.1)
