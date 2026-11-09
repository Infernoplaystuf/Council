"""
Which model to use, said consistently: the catalog, the finder, the Models
tab's jobs, hardware_detect's pick, and the role map's Ollama slots and the
"docs" role. The recommendation bugs the engine map measured are pinned here.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

import model_catalog as mc
import model_finder as mf
from council_core import model_jobs as mj
from council_core import model_slots as ms

LAPTOP = {"vram_gb": 8.0, "ram_gb": 31.7, "gpu_name": "RTX 4070 Laptop"}


@pytest.fixture(autouse=True)
def no_models_on_this_pc(monkeypatch):
    """model_jobs.find marks the rows already on this PC, by asking
    local_models.list_local_models - which scans the PC's model folders and
    asks the localhost Ollama for its tags: the REAL one on a developer PC
    (probe, 2026-10-12: test_find_passes_the_role_from_the_task reached
    127.0.0.1:11434; the answer is cached for 5 s, so it is whichever test
    finds first). Every test here starts with nothing installed; one that
    needs installed models stubs its own (test_rows_already_on_this_pc_say_so)."""
    from council_core import local_models
    monkeypatch.setattr(local_models, "list_local_models",
                        lambda *a, **k: [])


# ============================================================
# The catalog
# ============================================================

def test_every_catalog_model_is_us_made_and_has_an_ollama_tag():
    for m in mc.MODELS:
        assert m.org in {"IBM", "Meta", "Microsoft", "Google", "AllenAI",
                         "OpenAI"}, m.id
        assert m.ollama, f"{m.id} has no Ollama tag"


def test_every_current_model_says_how_to_download_it():
    """User, 2026-11-08: update the model lists "and include their download
    instructions". Every current model names its Ollama command; the ones
    with a GGUF file name the Hugging Face command too, and a picture
    reader names its mmproj file."""
    assert mc.CURRENT and all(not m.legacy for m in mc.CURRENT)
    for m in mc.CURRENT:
        steps = mc.download_steps(m)
        assert f"ollama pull {m.ollama}" in steps, m.id
        assert "https://ollama.com/library/" in steps, m.id
        if m.has_hf_file:
            assert f"hf download {m.hf_repo} {m.hf_file}" in steps, m.id
            assert m.hf_file.endswith(".gguf"), m.id
        else:
            assert "use Ollama" in steps, m.id
        if m.hf_mmproj:
            assert m.reads_pictures and m.hf_mmproj in steps, m.id


def test_the_default_is_current_and_legacy_models_are_not_picks():
    assert mc.DEFAULT_MODEL_ID == "granite-4.2-8b"
    for vram in (0, 4, 8, 12, 16, 24, 48):
        assert all(not m.legacy for m in mc.for_vram(vram))
        assert all(not m.legacy for m in mc.tier_picks(vram))
        ids = {r["id"] for r in mf.recommend_from_catalog(vram, 32, limit=50)}
        assert not ids & {m.id for m in mc.MODELS if m.legacy}
    # still resolvable for saved settings and installed files
    assert mc.by_id("granite-3.1-8b-q4").legacy


@pytest.mark.parametrize("vram, first", [
    (0, "granite-4.2-3b"), (8, "granite-4.2-8b"), (12, "gemma-4-12b"),
    (16, "gemma-4-26b-a4b"), (24, "gemma-4-31b")])
def test_the_best_pick_for_each_card_size(vram, first):
    assert mc.tier_picks(vram)[0].id == first
    assert mc.tier_picks(vram + 1)[0].id == first      # up to the next tier


def test_a_16gb_card_ranks_the_picks_that_fit_first():
    ranked = [r["id"] for r in mf.recommend_from_catalog(16.0, 64.0,
                                                         limit=8)]
    assert ranked[:2] == ["gemma-4-12b", "gpt-oss-20b"]
    assert mc.for_vram(16.0)[0].id == "gemma-4-12b"


def test_the_granite_code_entry_is_no_longer_a_made_up_model():
    """It was "IBM Granite 3.0 8B Code Instruct" — no such model — pointing
    at granite-3.0-8b-instruct, a general instruct model."""
    m = mc.by_id("granite-3-8b-code-q4")
    assert "Code Instruct" not in m.name
    assert m.hf_file == "granite-3.0-8b-instruct-Q4_K_M.gguf"
    assert "Granite 3.0 8B Instruct" in m.name


def test_an_8gb_card_gets_us_candidates_including_partial_offload():
    ranked = mf.recommend_from_catalog(8.0, 31.7, role="general", limit=8)
    fits = {r["id"]: r["fit"] for r in ranked}
    assert ranked[0]["id"] == "granite-4.2-8b"
    assert fits["granite-4.2-8b"] == "gpu"
    assert fits.get("gpt-oss-20b") == "partial"
    assert fits.get("gemma-4-26b-a4b") == "partial"
    full = [r for r in ranked if r["fit"] == "gpu"]
    assert ranked[:len(full)] == full                  # full fits first


def test_an_moe_is_not_an_upgrade_over_a_dense_14b_by_its_total():
    up = mf.assess_upgrade(hardware={"vram_gb": 16.0, "ram_gb": 64.0},
                           current_model="phi-4-14b-Q4_K_M.gguf")
    assert up["can_upgrade"] is False and up["upgrades"] == []


def test_an_upgrade_offers_the_cards_best_pick_first():
    up = mf.assess_upgrade(hardware={"vram_gb": 24.0, "ram_gb": 64.0},
                           current_model="phi-4-14b-Q4_K_M.gguf")
    assert up["can_upgrade"] and up["upgrades"][0]["id"] == "gemma-4-31b"


# ============================================================
# Role-aware finding
# ============================================================

@pytest.mark.parametrize("task, role", [
    ("coding", "code"), ("write python handlers for my GUI", "code"),
    ("answer from package documentation", "docs"),
    ("search the MCP docs server", "docs"),
    ("long documents", "general"),         # long context, not API docs
    ("", "general"), ("chat", "general")])
def test_the_task_box_picks_the_role(task, role):
    assert mf.role_for_task(task) == role


def test_the_coder_role_is_offered_real_coders():
    ids = [r["id"] for r in mf.recommend_from_catalog(8.0, 31.7,
                                                      role="coder", limit=8)]
    assert "granite-4.2-8b" in ids
    assert "gpt-oss-20b" in ids


def test_find_passes_the_role_from_the_task(monkeypatch):
    seen = {}

    def find_models(**kw):
        seen.update(kw)
        return {"catalog": [], "online": []}

    monkeypatch.setitem(sys.modules, "model_finder", SimpleNamespace(
        find_models=find_models, role_for_task=mf.role_for_task))
    mj.find(mj.Hardware(raw=dict(LAPTOP)), task="coding")
    assert seen["role"] == "code"


def test_rows_already_on_this_pc_say_so(monkeypatch):
    from council_core import local_models
    monkeypatch.setattr(local_models, "list_local_models", lambda **k: [
        {"id": "ollama:granite4.2:8b", "backend": "ollama"},
        {"id": "C:/m/Olmo-3-7B-Instruct-Q4_K_M.gguf", "backend": "gguf",
         "file": "Olmo-3-7B-Instruct-Q4_K_M.gguf"}])
    result = mj.find(mj.Hardware(vram_gb=8.0, ram_gb=31.7, raw=dict(LAPTOP)))
    on_pc = {r.model_id: r.cells[-1] for r in result.rows}
    assert on_pc["granite-4.2-8b"] == "Ollama"
    assert on_pc["olmo-3-7b"] == "GGUF"
    assert on_pc["gemma-4-e4b"] == "no"
    assert len(mj.COLUMNS) == len(result.rows[0].cells)


# ============================================================
# The upgrade banner
# ============================================================

def test_the_banner_reads_reason_and_passes_the_current_model(monkeypatch):
    """Measured: assess_upgrade returns 'reason'; the banner read 'message',
    'summary' and 'headline', so it was always blank — and it passed no
    current model, so can_upgrade was always False."""
    seen = {}

    def assess_upgrade(**kw):
        seen.update(kw)
        return {"reason": "Your hardware has room.", "can_upgrade": True,
                "upgrades": [{"name": "Phi-4"}]}

    monkeypatch.setitem(sys.modules, "model_finder",
                        SimpleNamespace(assess_upgrade=assess_upgrade))
    text, offer = mj.upgrade_banner(mj.Hardware(), current="llama3.1:8b",
                                    current_params_b=8.0)
    assert text == "Your hardware has room."
    assert offer == {"name": "Phi-4"}
    assert seen["current_model"] == "llama3.1:8b"
    assert seen["current_params_b"] == 8.0


def test_no_upgrade_offers_no_download(monkeypatch):
    monkeypatch.setitem(sys.modules, "model_finder", SimpleNamespace(
        assess_upgrade=lambda **kw: {"reason": "Already the best fit.",
                                     "can_upgrade": False,
                                     "upgrades": [{"name": "X"}]}))
    assert mj.upgrade_banner(mj.Hardware(), current="m") == (
        "Already the best fit.", None)


def test_the_real_banner_on_this_laptop_is_not_blank():
    text, _offer = mj.upgrade_banner(
        mj.Hardware(raw=dict(LAPTOP)),
        current="granite-3.1-8b-instruct-Q4_K_M.gguf")
    assert text.strip()


# ============================================================
# hardware_detect agrees with the catalog
# ============================================================

@pytest.mark.parametrize("vram", [4.0, 8.0, 12.0, 16.0, 24.0])
def test_the_wizard_pick_is_one_the_catalog_says_fits(vram):
    """For 8 GB it said "Llama 3.1 8B Q5_K_M (or Gemma 2 9B)" — both of which
    model_catalog.fits rejects on 8 GB."""
    import hardware_detect as hd
    rec = hd._recommend({"vram_gb": vram, "ram_gb": 32,
                         "gpu_vendor": "nvidia", "cuda_max": 12.4})
    first = rec["model_pick"].split("  (")[0]
    spec = next(m for m in mc.MODELS if m.name == first)
    assert mc.fits(spec, vram) or not mc.for_vram(vram)
    assert "Gemma 2 9B" not in rec["model_pick"] or vram >= 9


def test_the_models_tab_shows_and_copies_the_download_steps():
    rows = mj.find(mj.Hardware(vram_gb=16.0, ram_gb=64.0,
                               raw={"vram_gb": 16.0, "ram_gb": 64.0})).rows
    row = next(r for r in rows if r.model_id == "gemma-4-12b")
    text = mj.copy_text(row)
    assert "ollama pull gemma4:12b" in text
    assert "hf download unsloth/gemma-4-12b-it-GGUF" in text
    assert "mmproj-F16.gguf" in text
    online = mj.ModelRow("x/y", ("Y",), repo="x/y-GGUF", filename="y.gguf")
    assert "hf download x/y-GGUF y.gguf" in mj.copy_text(online)


def test_an_ollama_only_model_is_not_downloaded_from_hugging_face(tmp_path):
    spec = mc.by_id("muse-glimmer-30b")
    assert not spec.has_hf_file
    row = mj.to_row({**vars(spec), "origin_verified": True})
    result = mj.download_and_switch(row, tmp_path)
    assert not result.ok and "no download source" in result.message
    assert "ollama pull muse-glimmer:30b" in mj.copy_text(row)


def test_the_hardware_line_names_free_vram_and_the_core_split():
    hw = mj.Hardware(gpu="RTX 4070 Laptop", vram_gb=8.0, ram_gb=31.7,
                     raw={"vram_free_gb": 7.8, "cpu_p_cores": 8,
                          "cpu_e_cores": 12, "ram_speed_mts": 5600})
    line = hw.summary
    assert "7.8" in line and "8 P-cores + 12 E-cores" in line
    assert "5600" in line


# ============================================================
# The role map: Ollama models and the docs role
# ============================================================

def test_docs_is_a_council_role():
    assert "docs" in ms.COUNCIL_ROLES and ms.ROLE_LABELS["docs"]


def test_an_ollama_slot_round_trips_with_its_window(tmp_path):
    cfg = ms.parse({"slots": {"d": {"path": "ollama:llama3.1:8b",
                                    "n_ctx": 16384}},
                    "roles": {"docs": "d"}})
    ms.save(tmp_path, cfg)
    back = ms.load(tmp_path)
    assert back.slots["d"].is_ollama and back.slots["d"].n_ctx == 16384
    assert back.slot_for("docs") == "d"
    assert back.slot_for("writer") == "main"


@pytest.mark.parametrize("bad, msg", [
    ({"slots": {"d": {"path": "ollama:"}}}, "names no Ollama model"),
    ({"slots": {"d": {"path": "ollama:x", "n_ctx": "big"}}}, "bad n_ctx"),
    ({"slots": {"d": {"path": "ollama:x", "n_ctx": 64}}}, "too small"),
])
def test_bad_ollama_slots_are_refused(bad, msg):
    with pytest.raises(ms.ConfigError, match=msg):
        ms.parse(bad)


def test_a_writer_on_ollama_makes_main_name_it(tmp_path):
    f = str(tmp_path / "coder.gguf")
    files = {r: "ollama:llama3.1:8b" for r in ms.COUNCIL_ROLES}
    files["coder"] = f
    cfg, main = ms.from_role_files(files)
    assert main == "ollama:llama3.1:8b"
    assert cfg.slots["main"].path == "ollama:llama3.1:8b"
    assert cfg.slots[cfg.slot_for("coder")].path == f
    assert cfg.slot_for("docs") == "main"


def test_phi35_and_phi35_latest_are_one_slot():
    files = {r: "ollama:phi3.5" for r in ms.COUNCIL_ROLES}
    files["peasant"] = "ollama:phi3.5:latest"
    cfg, _ = ms.from_role_files(files)
    assert not cfg.multi


def test_suggest_uses_installed_us_models_only():
    from council_core import local_models
    from tests.fake_ollama import DEFAULT_TAGS
    models = [local_models.ollama_entry(t, fill_from_show=False)
              for t in DEFAULT_TAGS]
    picks = ms.suggest_role_models(models, vram_gb=8.0, ram_gb=31.7)
    assert set(picks) == set(ms.COUNCIL_ROLES)
    assert {mid for mid, _why in picks.values()} == {"ollama:llama3.1:8b"}
    assert "tool calling" in picks["docs"][1]
    only_qwen = [m for m in models if "qwen" in m["name"]]
    assert ms.suggest_role_models(only_qwen, vram_gb=8.0) == {}


# ============================================================
# The "Check this PC" hook
# ============================================================

def test_check_this_pc_without_a_benchmark_says_so(monkeypatch):
    monkeypatch.setattr(mj, "_BENCH_RUNNER", None)
    monkeypatch.setitem(sys.modules, "council_core.llm_bench", None)
    result = mj.check_this_pc()
    assert not result.ok and "not in this build" in result.message


def test_check_this_pc_calls_the_bench_module(monkeypatch):
    calls = []

    def check_this_pc(**kw):
        calls.append(kw)
        kw["on_progress"]("measuring llama3.1:8b")
        return {"ok": True, "message": "1 model measured",
                "rows": [{"model": "llama3.1:8b", "gen_tok_s": 40}]}

    monkeypatch.setattr(mj, "_BENCH_RUNNER", None)
    monkeypatch.setitem(sys.modules, "council_core.llm_bench",
                        SimpleNamespace(check_this_pc=check_this_pc))
    lines = []
    result = mj.check_this_pc(models=["ollama:llama3.1:8b"],
                              on_progress=lines.append,
                              should_stop=lambda: False)
    assert result.ok and result.rows[0]["gen_tok_s"] == 40
    assert lines == ["measuring llama3.1:8b"]
    assert calls[0]["models"] == ["ollama:llama3.1:8b"]
    assert set(calls[0]) == {"models", "vault_dir", "on_progress",
                             "should_stop"}


def test_a_registered_runner_wins(monkeypatch):
    monkeypatch.setattr(mj, "_BENCH_RUNNER", None)
    mj.register_bench(lambda **kw: [{"m": 1}, {"m": 2}])
    try:
        assert mj.check_this_pc().message == "Measured 2 model(s)."
    finally:
        mj.register_bench(None)
