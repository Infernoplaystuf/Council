"""
Per-role model slots: council_core.model_slots (config, placement, presets)
and the engine loading one model per slot (council_engine._get_slot_model).

The engine tests replace llama_cpp with a fake that records how each model was
loaded and whether two generations overlapped in time — no GGUF is loaded and
no GPU is touched. VRAM is whatever the test says it is.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from council_core import model_slots as ms

GB = ms.GB


# ============================================================
# Config
# ============================================================

def test_no_file_is_one_model(tmp_path):
    cfg = ms.load(tmp_path)
    assert list(cfg.slots) == ["main"] and not cfg.multi
    assert cfg.slot_for("peasant") == "main"
    assert ms.problem(tmp_path) == ""


def test_round_trip(tmp_path):
    cfg = ms.SlotConfig({"main": ms.Slot("main"),
                         "fast": ms.Slot("fast", "C:/m/fast.gguf")},
                        {"peasant": "fast"})
    ms.save(tmp_path, cfg)
    back = ms.load(tmp_path)
    assert back.slot_for("peasant") == "fast"
    assert back.slot_for("writer") == "main"
    assert back.roles_of("fast") == ["peasant"]
    assert "peasant" not in back.roles_of("main")


@pytest.mark.parametrize("data, msg", [
    ({"slots": {"fast": {"path": ""}}}, "no model file"),
    ({"roles": {"peasant": "ghost"}}, "unknown slot"),
    ([], "object"),
])
def test_bad_files_are_refused(data, msg):
    with pytest.raises(ms.ConfigError, match=msg):
        ms.parse(data)


def test_a_damaged_file_falls_back_and_says_why(tmp_path):
    ms.config_path(tmp_path).write_text("{not json", encoding="utf-8")
    assert not ms.load(tmp_path).multi
    assert "could not be used" in ms.problem(tmp_path)


def test_save_refuses_a_role_on_a_missing_slot(tmp_path):
    with pytest.raises(ms.ConfigError):
        ms.save(tmp_path, ms.SlotConfig(roles={"peasant": "ghost"}))
    assert not ms.config_path(tmp_path).exists()


# ============================================================
# Placement
# ============================================================

def _cfg(**slots):
    s = {"main": ms.Slot("main")}
    roles = {}
    for name, served in slots.items():
        s[name] = ms.Slot(name, f"/m/{name}.gguf")
        for r in served:
            roles[r] = name
    return ms.SlotConfig(s, roles)


def test_both_fit_on_the_gpu_and_share_the_rest():
    cfg = _cfg(fast=["peasant", "intern", "artist"])
    plan = ms.plan(cfg, {"main": int(8.9 * GB), "fast": int(2.4 * GB)},
                   int(14 * GB))
    assert plan["main"].on_gpu and plan["fast"].on_gpu
    left = 14 * GB - ms.DEFAULT_MARGIN_BYTES - int(8.9 * GB) - int(2.4 * GB)
    # main takes two shares of the context room, fast one
    assert plan["main"].kv_budget_bytes == pytest.approx(left * 2 / 3, rel=1e-6)
    assert plan["fast"].kv_budget_bytes == pytest.approx(left / 3, rel=1e-6)


def test_what_does_not_fit_goes_to_the_cpu():
    cfg = _cfg(fast=["peasant"])
    plan = ms.plan(cfg, {"main": int(8.9 * GB), "fast": int(5 * GB)},
                   int(12 * GB))
    assert plan["main"].on_gpu and not plan["fast"].on_gpu
    assert plan["fast"].kv_budget_bytes is None
    assert "running on the CPU" in plan["fast"].reason


def test_main_is_placed_first_whatever_its_size():
    cfg = _cfg(fast=["peasant"])
    plan = ms.plan(cfg, {"main": int(9 * GB), "fast": int(1 * GB)},
                   int(10.5 * GB))
    assert plan["main"].on_gpu and not plan["fast"].on_gpu


def test_no_gpu_means_everything_on_the_cpu():
    plan = ms.plan(_cfg(fast=["peasant"]), {"main": 1, "fast": 1}, None)
    assert not any(p.on_gpu for p in plan.values())


def test_priority_is_main_then_most_roles():
    cfg = _cfg(a=["peasant"], b=["intern", "artist"])
    assert ms.priority(cfg) == ["main", "b", "a"]


def test_summary_names_where_each_runs():
    cfg = _cfg(fast=["peasant"])
    plan = ms.plan(cfg, {"main": int(8 * GB), "fast": int(5 * GB)},
                   int(12 * GB))
    line = ms.summary(plan, {"main": "Phi-4", "fast": "Llama 3B"}, 12 * GB)
    assert "Phi-4 → GPU" in line and "Llama 3B → CPU" in line


# ============================================================
# Presets and files
# ============================================================

def test_from_role_files_makes_the_writers_file_main(tmp_path):
    big, small = tmp_path / "big.gguf", tmp_path / "small.gguf"
    cfg, main = ms.from_role_files({"writer": str(big), "judge": str(big),
                                    "peasant": str(small),
                                    "intern": str(small)})
    assert main == str(big)
    assert set(cfg.slots) == {"main", "small"}
    assert cfg.slot_for("peasant") == "small" == cfg.slot_for("intern")
    assert cfg.slot_for("judge") == "main"


def test_one_file_everywhere_is_one_slot(tmp_path):
    f = str(tmp_path / "m.gguf")
    cfg, _ = ms.from_role_files({r: f for r in ms.COUNCIL_ROLES})
    assert not cfg.multi


def test_known_files_skip_vision_adapters(tmp_path):
    (tmp_path / "a.gguf").write_bytes(b"x")
    (tmp_path / "a-mmproj.gguf").write_bytes(b"x")
    assert [p.name for p in ms.known_files([tmp_path])] == ["a.gguf"]


def test_the_balanced_preset(tmp_path):
    import model_catalog
    assert set(ms.preset_missing(ms.BALANCED, [tmp_path])) == {
        "phi-4-q4", "llama-3.2-3b-q5"}
    for mid in ("phi-4-q4", "llama-3.2-3b-q5"):
        (tmp_path / model_catalog.by_id(mid).hf_file).write_bytes(b"x")
    assert ms.preset_missing(ms.BALANCED, [tmp_path]) == []
    files = {m: ms.find_catalog_file(m, [tmp_path])
             for m in ("phi-4-q4", "llama-3.2-3b-q5")}
    cfg = ms.preset_config(ms.BALANCED, files)
    assert cfg.slot_for("peasant") == "fast"
    assert cfg.slot_for("writer") == "main"
    assert cfg.slots["fast"].path.endswith("Llama-3.2-3B-Instruct-Q5_K_M.gguf")


# ============================================================
# The engine
# ============================================================

class FakeLlama:
    loads = []
    active = 0
    overlaps = 0
    _gate = threading.Lock()

    def __init__(self, model_path, n_ctx, n_threads, n_gpu_layers,
                 verbose=False, **kw):
        self.path = model_path
        self.name = Path(model_path).stem
        self._n_ctx = n_ctx
        self.n_gpu_layers = n_gpu_layers
        FakeLlama.loads.append(self)

    def n_ctx(self):
        return self._n_ctx

    def create_chat_completion(self, messages, temperature, max_tokens,
                               stream=False):
        with FakeLlama._gate:
            FakeLlama.active += 1
            if FakeLlama.active > 1:
                FakeLlama.overlaps += 1
        time.sleep(0.05)
        with FakeLlama._gate:
            FakeLlama.active -= 1
        return {"choices": [{"message": {"content": self.name}}]}

    def tokenize(self, b):
        return list(b)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    import council_engine as ce
    vault = tmp_path / "vault"
    vault.mkdir()
    models = tmp_path / "models"
    models.mkdir()
    for name, size in (("big", 3000), ("small", 1000), ("other", 1000)):
        (models / f"{name}.gguf").write_bytes(b"x" * size)
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
    monkeypatch.setenv("COUNCIL_GGUF_PATH", str(models / "big.gguf"))
    for var in ("COUNCIL_GGUF_N_CTX", "COUNCIL_GGUF_GPU_LAYERS",
                "COUNCIL_GGUF_CLIP_PATH"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setitem(sys.modules, "llama_cpp",
                        SimpleNamespace(Llama=FakeLlama))
    # The loader imports torch only for a diagnostic line and treats an
    # ImportError as "no torch". Importing the real one here aborts natively
    # in some envs, and it is not what these tests are about.
    monkeypatch.setitem(sys.modules, "torch", None)
    # A llama-shaped header: 32 layers, 8 KV heads, 4096 embed, 32 heads.
    monkeypatch.setattr(ce, "read_gguf_metadata", lambda p: {
        "general.architecture": "llama", "llama.block_count": 32,
        "llama.attention.head_count_kv": 8, "llama.attention.head_count": 32,
        "llama.embedding_length": 4096, "llama.context_length": 131072})
    state = {"free": 16 * GB}
    monkeypatch.setattr(ce, "_available_gpu_bytes",
                        lambda: (state["free"], "test"))
    # File sizes are tiny; pretend they are real weights.
    real_stat = Path.stat
    fake_sizes = {"big.gguf": int(8.9 * GB), "small.gguf": int(2.4 * GB),
                  "other.gguf": int(2.4 * GB)}

    def stat(self, *a, **k):
        st = real_stat(self, *a, **k)
        if self.name in fake_sizes:
            return SimpleNamespace(st_size=fake_sizes[self.name],
                                   st_mode=st.st_mode)
        return st

    monkeypatch.setattr(Path, "stat", stat)
    FakeLlama.loads, FakeLlama.active, FakeLlama.overlaps = [], 0, 0
    ce.refresh_backend_config()
    monkeypatch.setattr(ce, "_GPU_ATTEMPT_THIS_PROCESS", False)
    ce.gpu_clear_attempt()
    yield SimpleNamespace(ce=ce, vault=vault, models=models, state=state,
                          sizes=fake_sizes)
    ce.gpu_clear_attempt()
    ce.refresh_backend_config()


def _write(env, slots, roles):
    ms.save(env.vault, ms.parse({"slots": slots, "roles": roles}))
    env.ce.refresh_backend_config()


def test_without_a_slot_file_it_is_the_old_singleton(engine):
    ce = engine.ce
    llm = ce._get_gguf_model()
    assert llm.name == "big" and llm.n_gpu_layers == 99
    assert ce._GGUF_MODEL_INSTANCE is llm
    assert ce._slot_llm_and_lock("main")[1] is ce._INFERENCE_LOCK
    assert ce.local_chat([{"role": "user", "content": "hi"}],
                         role="peasant") == "big"
    assert len(FakeLlama.loads) == 1


def test_roles_answer_from_their_own_model(engine):
    _write(engine, {"fast": {"path": str(engine.models / "small.gguf")}},
           {"peasant": "fast"})
    ce = engine.ce
    msgs = [{"role": "user", "content": "hi"}]
    assert ce.local_chat(msgs, role="peasant") == "small"
    assert ce.local_chat(msgs, role="writer") == "big"
    assert ce.local_chat(msgs) == "big"                  # no role: main
    assert sorted(l.name for l in FakeLlama.loads) == ["big", "small"]
    assert all(l.n_gpu_layers == 99 for l in FakeLlama.loads)
    status = ce.slot_status()
    assert status["fast"]["on_gpu"] and status["main"]["on_gpu"]


def test_a_model_that_does_not_fit_runs_on_the_cpu(engine):
    engine.state["free"] = 12 * GB
    engine.sizes["small.gguf"] = int(5 * GB)
    _write(engine, {"fast": {"path": str(engine.models / "small.gguf")}},
           {"peasant": "fast"})
    ce = engine.ce
    ce.local_chat([{"role": "user", "content": "x"}], role="peasant")
    small = next(l for l in FakeLlama.loads if l.name == "small")
    assert small.n_gpu_layers == 0
    assert not ce.slot_status()["fast"]["on_gpu"]


def test_shared_card_bounds_each_models_context(engine):
    _write(engine, {"fast": {"path": str(engine.models / "small.gguf")}},
           {"peasant": "fast"})
    ce = engine.ce
    ce.local_chat([{"role": "user", "content": "x"}], role="writer")
    ce.local_chat([{"role": "user", "content": "x"}], role="peasant")
    by = {l.name: l for l in FakeLlama.loads}
    # 16 GB - 1 GB margin - 11.3 GB weights ≈ 3.7 GB of context, main 2/3.
    # At 128 KB/token (this header) main fits 16k, fast 8k — not the 32k a
    # lone model would take with the whole card.
    assert by["big"]._n_ctx == 16384 and by["small"]._n_ctx == 8192


def test_one_file_in_two_slots_loads_once(engine):
    _write(engine, {"alias": {"path": str(engine.models / "big.gguf")}},
           {"peasant": "alias"})
    ce = engine.ce
    ce.local_chat([{"role": "user", "content": "x"}], role="peasant")
    ce.local_chat([{"role": "user", "content": "x"}], role="writer")
    assert len(FakeLlama.loads) == 1
    assert ce._slot_llm_and_lock("alias")[1] is ce._INFERENCE_LOCK


def test_different_models_generate_at_the_same_time(engine):
    _write(engine, {"fast": {"path": str(engine.models / "small.gguf")}},
           {"peasant": "fast"})
    ce = engine.ce
    msgs = [{"role": "user", "content": "x"}]
    ce.local_chat(msgs, role="writer")                   # load both first
    ce.local_chat(msgs, role="peasant")
    FakeLlama.overlaps = 0
    threads = [threading.Thread(target=ce.local_chat, args=(msgs,),
                                kwargs={"role": r})
               for r in ("writer", "peasant")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert FakeLlama.overlaps >= 1


def test_the_same_model_takes_turns(engine):
    ce = engine.ce
    msgs = [{"role": "user", "content": "x"}]
    ce.local_chat(msgs, role="writer")
    FakeLlama.overlaps = 0
    threads = [threading.Thread(target=ce.local_chat, args=(msgs,),
                                kwargs={"role": r})
               for r in ("writer", "peasant", "judge")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert FakeLlama.overlaps == 0


def test_a_second_model_is_not_mistaken_for_a_crashed_gpu(engine):
    """Loading main writes the GPU-crash sentinel, cleared only after its
    first answer. A second model loading before then must still go to the
    GPU — the sentinel is this process's own load, not a crash."""
    _write(engine, {"fast": {"path": str(engine.models / "small.gguf")}},
           {"peasant": "fast"})
    ce = engine.ce
    ce._get_slot_model("main")                            # no answer yet
    assert ce.gpu_attempt_pending()
    ce._get_slot_model("fast")
    small = next(l for l in FakeLlama.loads if l.name == "small")
    assert small.n_gpu_layers == 99


def test_a_crash_sentinel_from_a_previous_run_still_forces_cpu(engine):
    ce = engine.ce
    ce._gpu_sentinel_path().write_text("n_gpu_layers=99\n")
    llm = ce._get_gguf_model()
    assert llm.n_gpu_layers == 0


def test_a_missing_slot_file_names_the_slot(engine):
    _write(engine, {"fast": {"path": str(engine.models / "gone.gguf")}},
           {"peasant": "fast"})
    with pytest.raises(RuntimeError, match="slot 'fast'"):
        engine.ce.local_chat([{"role": "user", "content": "x"}],
                             role="peasant")


def test_saving_a_new_map_takes_effect_after_refresh(engine):
    ce = engine.ce
    msgs = [{"role": "user", "content": "x"}]
    assert ce.local_chat(msgs, role="peasant") == "big"
    _write(engine, {"fast": {"path": str(engine.models / "other.gguf")}},
           {"peasant": "fast"})
    assert ce.local_chat(msgs, role="peasant") == "other"


def test_a_personality_answers_on_its_roles_model(engine):
    _write(engine, {"fast": {"path": str(engine.models / "small.gguf")}},
           {"peasant": "fast"})
    ce = engine.ce
    models = ce.build_personalities(pins={}, vault_dir=engine.vault,
                                    session_id="t", trace=False)
    assert models["peasant"].respond("hello") == "small"
    assert models["writer"].respond("hello") == "big"


def test_a_clean_two_model_run_leaves_no_crash_sentinel(engine):
    """Loading the second model re-wrote the sentinel after the first answer
    had cleared it, and nothing cleared it again — so the NEXT launch read a
    clean run as a CUDA crash and put every model on the CPU."""
    _write(engine, {"fast": {"path": str(engine.models / "small.gguf")}},
           {"peasant": "fast"})
    ce = engine.ce
    msgs = [{"role": "user", "content": "x"}]
    ce.local_chat(msgs, role="writer")          # load main, answer (clears)
    ce.local_chat(msgs, role="peasant")         # load fast, answer
    assert not ce.gpu_attempt_pending()


def test_a_crash_sentinel_says_why_the_slots_are_on_the_cpu(engine):
    _write(engine, {"fast": {"path": str(engine.models / "small.gguf")}},
           {"peasant": "fast"})
    ce = engine.ce
    ce._gpu_sentinel_path().write_text("n_gpu_layers=99\n")
    ce.local_chat([{"role": "user", "content": "x"}], role="peasant")
    reason = ce.slot_status()["fast"]["reason"]
    assert "did not finish" in reason and "CPU" in reason
    assert not ce.slot_status()["fast"]["on_gpu"]
