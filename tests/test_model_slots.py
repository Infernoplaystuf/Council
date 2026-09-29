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
    delay = 0.05                      # seconds one generation takes
    _gate = threading.Lock()

    def __init__(self, model_path, n_ctx, n_threads, n_gpu_layers,
                 verbose=False, **kw):
        self.path = model_path
        self.name = Path(model_path).stem
        self._n_ctx = n_ctx
        self.n_gpu_layers = n_gpu_layers
        self.max_tokens = []          # what each call was allowed to reply
        self.generating = 0           # generations running on THIS instance
        self.overlap = 0              # times two ran on it at once
        FakeLlama.loads.append(self)

    def n_ctx(self):
        return self._n_ctx

    def create_chat_completion(self, messages, temperature, max_tokens,
                               stream=False):
        self.max_tokens.append(max_tokens)
        with FakeLlama._gate:
            FakeLlama.active += 1
            if FakeLlama.active > 1:
                FakeLlama.overlaps += 1
            self.generating += 1
            if self.generating > 1:
                self.overlap += 1
        time.sleep(FakeLlama.delay)
        with FakeLlama._gate:
            FakeLlama.active -= 1
            self.generating -= 1
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
    monkeypatch.setattr(FakeLlama, "delay", 0.05)
    # The loader records the main model's window on the module; start clean
    # and put it back after, so no other test reads this one's model.
    monkeypatch.setattr(ce, "_LAST_N_CTX", None)
    monkeypatch.setattr(ce, "_LAST_N_CTX_KEY", "", raising=False)
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


def test_each_slot_answers_within_the_context_it_loaded_with(engine):
    """The ladder's window reaches the clamp. With COUNCIL_GGUF_N_CTX unset
    the clamp used to take min(4096, the model's n_ctx): a 16k main answered
    as if it had 4k, and a 9000-token request was cut to 2016."""
    _write(engine, {"fast": {"path": str(engine.models / "small.gguf")}},
           {"peasant": "fast"})
    ce = engine.ce
    msgs = [{"role": "user", "content": "x"}]
    ce.local_chat(msgs, num_predict=9000, role="writer")
    ce.local_chat(msgs, num_predict=9000, role="peasant")
    by = {l.name: l for l in FakeLlama.loads}
    assert by["big"]._n_ctx == 16384 and by["small"]._n_ctx == 8192
    # Reply cap: half the window less the template margin (64 + 8 a message).
    assert by["big"].max_tokens == [(16384 - 72) // 2]
    assert by["small"].max_tokens == [(8192 - 72) // 2]
    ce.local_chat(msgs, num_predict=2400, role="writer")
    assert by["big"].max_tokens[-1] == 2400


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


# ============================================================
# A model on the CPU is not sized by the card's free VRAM
# ============================================================

def test_a_slot_placed_on_the_cpu_gets_the_blind_window(engine):
    """Measured before the fix: this fast slot, planned onto the CPU, still
    took the VRAM rung's 32768 — sized to 12 - 5 - 1 = 6 GB of VRAM it
    never uses, i.e. 4 GiB of KV cache in RAM at this header's 128 KiB per
    token, which the clamp then let it fill."""
    engine.state["free"] = 12 * GB
    engine.sizes["small.gguf"] = int(5 * GB)
    _write(engine, {"fast": {"path": str(engine.models / "small.gguf")}},
           {"peasant": "fast"})
    ce = engine.ce
    ce.local_chat([{"role": "user", "content": "x"}], num_predict=9000,
                  role="peasant")
    small = next(l for l in FakeLlama.loads if l.name == "small")
    assert small.n_gpu_layers == 0
    assert small._n_ctx == 8192
    assert small.max_tokens == [(8192 - 72) // 2]
    assert ce.slot_status()["fast"]["n_ctx"] == 8192


def test_gpu_layers_zero_sizes_the_model_for_the_cpu(engine, monkeypatch):
    probes = []
    monkeypatch.setenv("COUNCIL_GGUF_GPU_LAYERS", "0")
    monkeypatch.setattr(engine.ce, "_available_gpu_bytes",
                        lambda: probes.append(1) or (16 * GB, "test"))
    llm = engine.ce._get_gguf_model()
    assert llm.n_gpu_layers == 0 and llm._n_ctx == 8192     # was 32768
    assert probes == []                  # no VRAM probe for a CPU model


def test_a_crash_sentinel_sizes_the_model_for_the_cpu(engine):
    engine.ce._gpu_sentinel_path().write_text("n_gpu_layers=99\n")
    llm = engine.ce._get_gguf_model()
    assert llm.n_gpu_layers == 0 and llm._n_ctx == 8192     # was 32768


def test_the_env_var_still_sizes_a_cpu_model(engine, monkeypatch):
    monkeypatch.setenv("COUNCIL_GGUF_GPU_LAYERS", "0")
    monkeypatch.setenv("COUNCIL_GGUF_N_CTX", "16384")
    llm = engine.ce._get_gguf_model()
    assert llm.n_gpu_layers == 0 and llm._n_ctx == 16384


def test_a_model_on_the_gpu_is_still_sized_by_the_card(engine):
    llm = engine.ce._get_gguf_model()
    # 16 GB free - 8.9 GB weights - 1 GB margin: 32k at 128 KiB a token.
    assert llm.n_gpu_layers == 99 and llm._n_ctx == 32768


# ============================================================
# effective_n_ctx: a released main is remembered, for its own file
# ============================================================

def test_a_released_main_window_is_remembered_for_that_file_only(
        engine, monkeypatch):
    ce = engine.ce
    ce.local_chat([{"role": "user", "content": "x"}], role="writer")
    assert ce.effective_n_ctx() == 32768
    ce.refresh_backend_config()               # drops the instance
    # The same file, the same settings: at most rung 3's blind cap, because
    # the reload re-measures free VRAM and that cannot be asked cheaply.
    assert ce.effective_n_ctx() == 8192
    assert len(FakeLlama.loads) == 1          # asked, not loaded
    monkeypatch.setenv("COUNCIL_GGUF_PATH", str(engine.models / "other.gguf"))
    ce.refresh_backend_config()
    assert ce.effective_n_ctx() == 4096       # another file: not known
    assert len(FakeLlama.loads) == 1


@pytest.mark.parametrize("change", ["gpu_off", "a_game_takes_the_vram",
                                    "a_fast_slot_is_added",
                                    "the_cap_is_lowered", "the_margin_grows"])
def test_a_released_main_never_promises_more_than_its_reload(
        engine, monkeypatch, change):
    """What a builder budgets for between a refresh and the reload must fit
    the window the reload gets. Measured before the fix: each of the first
    three changes left effective_n_ctx() promising the 32768 of the last
    load, and the reload had 8192 (CPU: rung 3's blind cap), 8192 (9 GB free
    is no room beside 8.9 GB of weights, so rung 3 again) and 16384 (main's
    share of a card it now splits with the fast slot).

    The last two are what the placement-inputs comparison is for, beyond the
    8192 cap: with only the cap, a lowered N_CTX_MAX or a bigger VRAM margin
    left 8192 promised for a 4096 reload (measured by the round-3 verifier,
    reverting the comparison in a scratch copy)."""
    ce = engine.ce
    msgs = [{"role": "user", "content": "x"}]
    ce.local_chat(msgs, role="writer")
    assert ce.effective_n_ctx() == 32768
    if change == "gpu_off":                   # what the Engine dialog does
        monkeypatch.setenv("COUNCIL_GGUF_GPU_LAYERS", "0")
        ce.refresh_backend_config()
    elif change == "a_game_takes_the_vram":
        engine.state["free"] = 9 * GB
        ce.refresh_backend_config()
    elif change == "the_cap_is_lowered":
        monkeypatch.setenv("COUNCIL_GGUF_N_CTX_MAX", "4096")
        ce.refresh_backend_config()
    elif change == "the_margin_grows":
        monkeypatch.setenv("COUNCIL_GGUF_KV_VRAM_MARGIN_MB", "6500")
        ce.refresh_backend_config()
    else:
        _write(engine, {"fast": {"path": str(engine.models / "small.gguf")}},
               {"peasant": "fast"})
    promised = ce.effective_n_ctx()
    ce.local_chat(msgs, role="writer")        # the reload
    assert promised <= ce.effective_n_ctx()


# ============================================================
# A refresh cannot pair a model with the wrong lock
# ============================================================

def _wait(cond, seconds=10.0):
    deadline = time.monotonic() + seconds
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


def _race_a_refresh(engine, monkeypatch, refresh):
    """T1 is generating on the fast model when T2 asks for the same slot, and
    ``refresh`` runs the moment T2's _get_slot_model has handed it the model.
    Returns the instance T1 was generating on."""
    _write(engine, {"fast": {"path": str(engine.models / "small.gguf")}},
           {"peasant": "fast"})
    ce = engine.ce
    msgs = [{"role": "user", "content": "x"}]
    ce.local_chat(msgs, role="peasant")                  # load fast
    old = next(l for l in FakeLlama.loads if l.name == "small")
    monkeypatch.setattr(FakeLlama, "delay", 0.6)
    real_get = ce._get_slot_model
    fired = []

    def get(slot="main"):
        llm = real_get(slot)
        if threading.current_thread().name == "T2" and not fired:
            fired.append(True)
            refresh()
        return llm

    monkeypatch.setattr(ce, "_get_slot_model", get)
    t1, t2 = (threading.Thread(target=ce.local_chat, args=(msgs,),
                               kwargs={"role": "peasant"}, name=n)
              for n in ("T1", "T2"))
    t1.start()
    _wait(lambda: old.generating == 1)
    t2.start()
    t1.join(10)
    t2.join(10)
    assert fired and not t1.is_alive() and not t2.is_alive()
    return old


def test_a_dropped_model_still_meets_its_own_lock(engine, monkeypatch):
    """A refresh drops the instances and their locks. A call that had
    already fetched the old instance was then handed the NEW lock, and
    generated on the old Llama while T1, holding the old lock, still was:
    two generations on one KV cache."""
    old = _race_a_refresh(engine, monkeypatch, engine.ce._release_slots)
    assert old.overlap == 0


def test_a_role_map_saved_mid_call_cannot_pair_the_wrong_lock(engine,
                                                              monkeypatch):
    """The Models tab saves a role map, then refreshes. model_slots.save()
    swaps the config at once, without the engine's locks, so a call that
    had fetched the fast model looked its lock up in the NEW config — the
    other file's — and generated beside T1 on the old model."""
    gui = []

    def the_models_tab_saves():
        t = threading.Thread(target=_write, args=(
            engine, {"fast": {"path": str(engine.models / "other.gguf")}},
            {"peasant": "fast"}))
        gui.append(t)
        t.start()
        t.join(0.3)          # done by now unless it waits on the engine

    old = _race_a_refresh(engine, monkeypatch, the_models_tab_saves)
    for t in gui:
        t.join(10)
    assert old.overlap == 0


def test_a_slot_file_that_becomes_main_is_guarded_by_the_main_lock(
        engine, monkeypatch):
    """Main's instance must come with _INFERENCE_LOCK: LlamaCppRunner and
    estimate_tokens take that lock by name and use _GGUF_MODEL_INSTANCE.

    COUNCIL_GGUF_PATH moved to the file the fast slot had loaded, with no
    refresh between (LlamaCppRunner with a gguf_path does exactly that; so
    does the Models tab between save and refresh). Main then reused the fast
    instance, which kept the fast lock it loaded with: a runner holding
    _INFERENCE_LOCK generated on it beside a fast-slot call holding the
    other lock, and estimate_tokens tokenized on it mid-generation. Measured
    before the fix: 1 generation overlap and 1 tokenize during a generation.
    """
    ce = engine.ce
    msgs = [{"role": "user", "content": "x"}]
    small = engine.models / "small.gguf"
    _write(engine, {"fast": {"path": str(small)}}, {"peasant": "fast"})
    ce.local_chat(msgs, role="peasant")                 # small loads as fast
    as_fast = FakeLlama.loads[-1]
    monkeypatch.setenv("COUNCIL_GGUF_PATH", str(small))
    ce.local_chat(msgs, role="writer")                  # main -> small
    main = ce._GGUF_MODEL_INSTANCE
    assert main.name == "small" and main is not as_fast   # loaded as main
    assert ce._slot_llm_and_lock("main") == (main, ce._INFERENCE_LOCK)
    assert ce._slot_llm_and_lock("fast") == (main, ce._INFERENCE_LOCK)

    tokenized_mid_generation = []
    real_tokenize = FakeLlama.tokenize

    def tokenize(self, b):
        if self.generating:
            tokenized_mid_generation.append(self.name)
        return real_tokenize(self, b)

    monkeypatch.setattr(FakeLlama, "tokenize", tokenize)
    monkeypatch.setattr(FakeLlama, "delay", 0.5)
    t = threading.Thread(target=ce.local_chat, args=(msgs,),
                         kwargs={"role": "peasant"})
    t.start()
    _wait(lambda: main.generating)
    ce.estimate_tokens("hello there")                   # the GUI's estimate
    from inferno_local.model_runner import LlamaCppRunner
    LlamaCppRunner({}).chat(msgs)                       # a runner's call
    t.join(10)
    assert not t.is_alive()
    assert main.overlap == 0 and as_fast.overlap == 0
    assert tokenized_mid_generation == []


# ============================================================
# -1 GPU layers is llama-cpp-python's "every layer", not the CPU
# ============================================================

def test_minus_one_gpu_layers_is_sized_by_the_card(engine, monkeypatch):
    """llama-cpp-python maps n_gpu_layers=-1 to every layer. The CPU test
    was `<= 0`, so -1 took rung 3's blind 8192 on a card with room for
    32768 (this fixture: 16 GB free, 8.9 GB of weights, 1 GB margin)."""
    monkeypatch.setenv("COUNCIL_GGUF_GPU_LAYERS", "-1")
    llm = engine.ce._get_gguf_model()
    assert llm.n_gpu_layers == -1 and llm._n_ctx == 32768


def test_minus_one_is_planned_onto_the_card_and_capped_off_it(engine,
                                                              monkeypatch):
    """The planner read -1 as "GPU layers set to 0" and put every slot on
    the CPU, and its CPU cap was min(-1, 0) = -1 — every layer on the GPU
    for a model it had planned off the card."""
    monkeypatch.setenv("COUNCIL_GGUF_GPU_LAYERS", "-1")
    engine.state["free"] = 12 * GB
    engine.sizes["small.gguf"] = int(5 * GB)
    _write(engine, {"fast": {"path": str(engine.models / "small.gguf")}},
           {"peasant": "fast"})
    ce = engine.ce
    msgs = [{"role": "user", "content": "x"}]
    ce.local_chat(msgs, role="writer")
    ce.local_chat(msgs, role="peasant")
    by = {l.name: l for l in FakeLlama.loads}
    assert by["big"].n_gpu_layers == -1 and ce.slot_status()["main"]["on_gpu"]
    assert by["small"].n_gpu_layers == 0
    assert not ce.slot_status()["fast"]["on_gpu"]


def test_a_crash_sentinel_moves_minus_one_to_the_cpu(engine, monkeypatch):
    monkeypatch.setenv("COUNCIL_GGUF_GPU_LAYERS", "-1")
    engine.ce._gpu_sentinel_path().write_text("n_gpu_layers=-1\n")
    llm = engine.ce._get_gguf_model()
    assert llm.n_gpu_layers == 0 and llm._n_ctx == 8192


def test_a_minus_one_load_arms_the_crash_sentinel(engine, monkeypatch):
    """A CUDA crash under -1 must send the next launch to the CPU too."""
    monkeypatch.setenv("COUNCIL_GGUF_GPU_LAYERS", "-1")
    engine.ce._get_gguf_model()
    assert engine.ce.gpu_attempt_pending()


# ============================================================
# backend_settings.json saved with a BOM
# ============================================================

def test_a_bom_in_backend_settings_keeps_the_vision_adapter(engine,
                                                             monkeypatch):
    """Notepad and PowerShell 5's Set-Content -Encoding utf8 write a BOM.
    Read as plain utf-8, json.loads failed on U+FEFF, the clip_path was
    dropped with a warning and the model loaded text-only."""
    clip = engine.models / "mmproj.gguf"
    clip.write_bytes(b"x")
    (engine.vault / "backend_settings.json").write_text(
        json.dumps({"clip_path": str(clip)}), encoding="utf-8-sig")
    made = []

    class Handler:
        def __init__(self, clip_model_path):
            made.append(clip_model_path)

    monkeypatch.setitem(sys.modules, "llama_cpp.llama_chat_format",
                        SimpleNamespace(Llava15ChatHandler=Handler))
    engine.ce._get_gguf_model()
    assert made == [str(clip)]
