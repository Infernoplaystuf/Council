"""
The GGUF loader's new tuning, with a fake llama_cpp (no weights, no GPU):
partial offload computed from the header and free VRAM, flash attention on a
GPU load, P-core threads and explicit prefill threads, each with a retry
without it — plus the VRAM probe order and the KV-cache estimate.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from council_core import model_slots as ms

GB = ms.GB


class FakeLlama:
    loads = []
    refuse = set()                    # kwargs that make the "build" raise

    def __init__(self, model_path, n_ctx, n_threads, n_gpu_layers,
                 verbose=False, **kw):
        bad = FakeLlama.refuse & set(kw)
        if bad:
            raise TypeError(f"unexpected keyword {sorted(bad)}")
        self.path, self._n_ctx = model_path, n_ctx
        self.n_threads, self.n_gpu_layers, self.kw = n_threads, n_gpu_layers, kw
        FakeLlama.loads.append(self)

    def n_ctx(self):
        return self._n_ctx

    def create_chat_completion(self, messages, temperature, max_tokens,
                               stream=False, **kw):
        return {"choices": [{"message": {"content": "ok"}}]}

    def tokenize(self, b):
        return list(b)


#: Phi-4 14B's real header numbers (read from the Ollama blob on this PC):
#: 40 blocks, 10 KV heads, 5120 embedding, 40 heads; 8.43 GiB file.
PHI4_META = {"general.architecture": "phi3", "phi3.block_count": 40,
             "phi3.attention.head_count_kv": 10,
             "phi3.attention.head_count": 40, "phi3.embedding_length": 5120,
             "phi3.context_length": 16384}


@pytest.fixture
def loader(tmp_path, monkeypatch):
    import council_engine as ce
    vault = tmp_path / "vault"
    vault.mkdir()
    model = tmp_path / "phi4.gguf"
    model.write_bytes(b"x")
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
    monkeypatch.setenv("COUNCIL_GGUF_PATH", str(model))
    for var in ("COUNCIL_GGUF_N_CTX", "COUNCIL_GGUF_GPU_LAYERS",
                "COUNCIL_GGUF_CLIP_PATH", "COUNCIL_GGUF_FLASH_ATTN",
                "COUNCIL_GGUF_PARTIAL_OFFLOAD", "COUNCIL_GGUF_N_THREADS",
                "COUNCIL_GGUF_N_THREADS_BATCH", "COUNCIL_GGUF_SEED"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setitem(sys.modules, "llama_cpp",
                        SimpleNamespace(Llama=FakeLlama))
    monkeypatch.setitem(sys.modules, "torch", None)
    monkeypatch.setattr(ce, "read_gguf_metadata", lambda p: dict(PHI4_META))
    state = SimpleNamespace(free=int(7.75 * GB))
    monkeypatch.setattr(ce, "_available_gpu_bytes",
                        lambda: (state.free, "test"))
    monkeypatch.setattr(ce, "_cpu_split", lambda: {
        "p_cores": 8, "e_cores": 12, "physical": 20, "logical": 28})
    monkeypatch.setattr(ce, "_gpu_diag_line", lambda: "")
    real_stat = Path.stat

    def stat(self, *a, **k):
        st = real_stat(self, *a, **k)
        if self.name == "phi4.gguf":
            return SimpleNamespace(st_size=int(8.43 * GB), st_mode=st.st_mode,
                                   st_mtime=st.st_mtime)
        return st

    monkeypatch.setattr(Path, "stat", stat)
    FakeLlama.loads, FakeLlama.refuse = [], set()
    monkeypatch.setattr(ce, "_LAST_N_CTX", None)
    monkeypatch.setattr(ce, "_LAST_N_CTX_KEY", "", raising=False)
    ce.refresh_backend_config()
    monkeypatch.setattr(ce, "_GPU_ATTEMPT_THIS_PROCESS", False)
    ce.gpu_clear_attempt()
    yield SimpleNamespace(ce=ce, state=state)
    ce.gpu_clear_attempt()
    ce.refresh_backend_config()


def _load(env):
    env.ce.local_chat([{"role": "user", "content": "hi"}])
    return FakeLlama.loads[-1]


# ============================================================
# Partial offload
# ============================================================

def test_a_model_too_big_for_the_card_gets_the_layers_that_fit(loader):
    """Phi-4 14B (8.43 GiB) on the RTX 4070 Laptop (7.75 GiB free) used to
    load with every layer requested: an OOM, or the driver spilling into
    shared memory."""
    llm = _load(loader)
    assert 0 < llm.n_gpu_layers < 40
    assert llm._n_ctx == 8192
    # The layers asked for, their KV cache, the compute buffer and the margin
    # fit the free VRAM.
    per_layer = (8.43 * GB) / 42 + 200 * 1024 * 8192 / 40
    need = llm.n_gpu_layers * per_layer + ms.COMPUTE_BYTES + GB
    assert need <= loader.state.free
    status = loader.ce.slot_status()["main"]
    assert status["on_gpu"] and "partial offload" in status["reason"]


def test_a_model_that_fits_is_loaded_whole_as_before(loader):
    loader.state.free = 16 * GB
    llm = _load(loader)
    assert llm.n_gpu_layers == 99


def test_no_room_for_one_layer_means_the_cpu(loader):
    loader.state.free = int(1.2 * GB)
    llm = _load(loader)
    assert llm.n_gpu_layers == 0
    assert "flash_attn" not in llm.kw        # CPU load: no flash attention


@pytest.mark.parametrize("env", [{"COUNCIL_GGUF_GPU_LAYERS": "99"},
                                 {"COUNCIL_GGUF_PARTIAL_OFFLOAD": "0"}])
def test_an_explicit_layer_count_or_the_switch_keeps_all_or_nothing(
        loader, monkeypatch, env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    loader.ce.refresh_backend_config()
    assert _load(loader).n_gpu_layers == 99


def test_plan_offload_arithmetic():
    plan = ms.plan_offload(file_bytes=int(8.43 * GB), n_layers=40,
                           per_layer_bytes=int(210 * 1024 ** 2),
                           kv_bytes_per_token=200 * 1024,
                           free_vram_bytes=int(7.75 * GB))
    assert plan.n_ctx == 8192 and 20 <= plan.n_gpu_layers <= 30
    fits = ms.plan_offload(file_bytes=int(4.6 * GB), n_layers=32,
                           per_layer_bytes=int(133 * 1024 ** 2),
                           kv_bytes_per_token=128 * 1024,
                           free_vram_bytes=int(7.75 * GB))
    assert fits.n_gpu_layers == -1


# ============================================================
# Flash attention, threads, the retry
# ============================================================

def test_a_gpu_load_gets_flash_attention_and_p_core_threads(loader):
    loader.state.free = 16 * GB
    llm = _load(loader)
    assert llm.kw.get("flash_attn") is True
    assert llm.n_threads == 8                      # P-cores, not 20 or 28
    assert llm.kw["n_threads_batch"] == 8          # full offload


def test_a_partial_load_prefills_on_every_physical_core(loader):
    llm = _load(loader)
    assert llm.n_threads == 8 and llm.kw["n_threads_batch"] == 20


def test_env_overrides(loader, monkeypatch):
    monkeypatch.setenv("COUNCIL_GGUF_FLASH_ATTN", "0")
    monkeypatch.setenv("COUNCIL_GGUF_N_THREADS", "6")
    monkeypatch.setenv("COUNCIL_GGUF_N_THREADS_BATCH", "12")
    monkeypatch.setenv("COUNCIL_GGUF_SEED", "42")
    loader.state.free = 16 * GB
    llm = _load(loader)
    assert "flash_attn" not in llm.kw
    assert llm.n_threads == 6 and llm.kw["n_threads_batch"] == 12
    assert llm.kw["seed"] == 42


def test_a_build_that_refuses_the_tuning_still_loads(loader):
    FakeLlama.refuse = {"flash_attn"}
    loader.state.free = 16 * GB
    llm = _load(loader)
    assert llm.n_gpu_layers == 99 and "flash_attn" not in llm.kw


def test_threads_from_the_core_split(monkeypatch):
    import council_engine as ce
    monkeypatch.setattr(ce, "_cpu_split", lambda: {
        "p_cores": 8, "e_cores": 12, "physical": 20, "logical": 28})
    for var in ("COUNCIL_GGUF_N_THREADS", "COUNCIL_GGUF_N_THREADS_BATCH"):
        monkeypatch.delenv(var, raising=False)
    assert ce._default_threads(full_gpu=True) == (8, 8)
    assert ce._default_threads(full_gpu=False) == (8, 20)
    monkeypatch.setattr(ce, "_cpu_split", lambda: {
        "p_cores": 6, "e_cores": 0, "physical": 6, "logical": 12})
    assert ce._default_threads(full_gpu=False) == (6, 6)


def test_the_windows_core_split_reads_real_hardware():
    import hardware_detect as hd
    split = hd.cpu_core_split()
    assert split["logical"] and split["physical"]
    assert split["p_cores"] + split["e_cores"] == split["physical"]
    assert split["physical"] <= split["logical"]


# ============================================================
# The VRAM probe and the KV estimate
# ============================================================

def test_free_vram_is_read_with_nvidia_smi_before_torch(monkeypatch):
    """torch.cuda.mem_get_info creates a CUDA context in this process —
    VRAM taken on an 8 GB card before the model loads."""
    import council_engine as ce
    touched = []

    class Torch:
        """Records any use; the probe swallows exceptions, so raising here
        would prove nothing."""
        def __getattr__(self, name):
            touched.append(name)
            return SimpleNamespace(is_available=lambda: True,
                                   mem_get_info=lambda: (1, 2))

    monkeypatch.setitem(sys.modules, "torch", Torch())
    monkeypatch.setattr(ce.shutil, "which", lambda name: "nvidia-smi")
    monkeypatch.setattr(ce.subprocess, "run", lambda *a, **k: SimpleNamespace(
        stdout="7940\n", returncode=0))
    assert ce._available_gpu_bytes() == (7940 * 1024 * 1024, "nvidia-smi")
    assert touched == [], "torch was asked first"


def test_the_kv_estimate_uses_the_headers_head_size():
    """gpt-oss sets attention.key_length 64 (read from the blob on this PC);
    embedding/heads = 2880/64 = 45 under-counted it by 30%."""
    import council_engine as ce
    meta = {"general.architecture": "gptoss", "gptoss.block_count": 24,
            "gptoss.attention.head_count_kv": 8,
            "gptoss.attention.head_count": 64,
            "gptoss.embedding_length": 2880,
            "gptoss.attention.key_length": 64,
            "gptoss.attention.value_length": 64}
    assert ce._estimate_kv_cache_bytes(meta, 1) == 24 * 8 * 128 * 2
    meta.pop("gptoss.attention.key_length")
    meta.pop("gptoss.attention.value_length")
    assert ce._estimate_kv_cache_bytes(meta, 1) == 2 * 24 * 8 * 45 * 2
