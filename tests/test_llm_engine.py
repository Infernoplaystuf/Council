"""
council_engine's local-model backends: the localhost Ollama backend per role,
structured output on both backends, cancellation, the stall timeout,
telemetry, chat_tools and list_local_models.

No model is ever run. Ollama is tests/fake_ollama.py on 127.0.0.1 (a real
server on this PC must never get a /api/chat from a test); llama_cpp is a
stand-in that streams scripted chunks. The vault is a temp dir.
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
from tests.fake_ollama import DEFAULT_TAGS, FakeOllama, tag

GB = ms.GB
MSGS = [{"role": "system", "content": "be brief"},
        {"role": "user", "content": "hello"}]


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def fake():
    with FakeOllama() as srv:
        yield srv


@pytest.fixture
def eng(tmp_path, monkeypatch, fake):
    """The engine with a temp vault, the fake Ollama as THE host, no
    llama_cpp (as in the council env), and no GGUF configured."""
    import council_engine as ce
    from council_core import local_models
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
    monkeypatch.setenv("COUNCIL_OLLAMA_HOST", fake.url)
    for var in ("COUNCIL_GGUF_PATH", "COUNCIL_BACKEND", "COUNCIL_OLLAMA_MODEL",
                "COUNCIL_OLLAMA_NUM_CTX", "COUNCIL_OLLAMA_KEEP_ALIVE",
                "COUNCIL_GGUF_N_CTX", "COUNCIL_GGUF_GPU_LAYERS",
                "COUNCIL_AGENT_MEMORY_ENABLE", "COUNCIL_REMOTE_NODES"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setitem(sys.modules, "llama_cpp", None)   # not installed
    monkeypatch.setattr(ce, "_hardware_gb", lambda: (8.0, 31.7))
    local_models.invalidate_cache()
    ce.refresh_backend_config()
    monkeypatch.setattr(ce, "_OLLAMA_CPT", {})     # no ratio learned yet
    yield SimpleNamespace(ce=ce, vault=vault, fake=fake)
    ce.refresh_backend_config()
    local_models.invalidate_cache()


def _slots(env, slots, roles):
    ms.save(env.vault, ms.parse({"slots": slots, "roles": roles}))
    env.ce.refresh_backend_config()


# ============================================================
# The Ollama backend, per role
# ============================================================

def test_a_role_assigned_an_ollama_model_is_served_by_it(eng):
    _slots(eng, {"docs": {"path": "ollama:llama3.1:8b"}}, {"docs": "docs"})
    out = eng.ce.local_chat(MSGS, role="docs", num_predict=50, seed=7,
                            stop=["\n\n"])
    assert out == "Hello from the fake."
    body = eng.fake.state.chats[-1]
    assert body["model"] == "llama3.1:8b"
    opts = body["options"]
    assert opts["repeat_penalty"] == 1.0          # never Ollama's 1.1
    assert opts["num_ctx"] == 8192                # the default window
    assert opts["seed"] == 7 and opts["stop"] == ["\n\n"]
    assert opts["num_predict"] == 50
    assert body["keep_alive"] == "30m"
    assert body["stream"] is True and "format" not in body


def test_num_ctx_is_the_slot_window_capped_at_the_models_own(eng):
    eng.fake.state.tags = [tag("llama3.1:8b", size=4_900_000_000,
                               family="llama", params="8.0B",
                               quant="Q4_K_M", ctx=4096)]
    _slots(eng, {"a": {"path": "ollama:llama3.1:8b", "n_ctx": 16384}},
           {"coder": "a"})
    eng.ce.local_chat(MSGS, role="coder")
    assert eng.fake.state.chats[-1]["options"]["num_ctx"] == 4096
    # ...and prompt builders are told that window, without a config read.
    assert eng.ce.effective_n_ctx("a") == 4096


def test_an_explicit_slot_window_is_sent(eng):
    _slots(eng, {"a": {"path": "ollama:llama3.1:8b", "n_ctx": 2048}},
           {"coder": "a"})
    eng.ce.local_chat(MSGS, role="coder")
    assert eng.fake.state.chats[-1]["options"]["num_ctx"] == 2048


def test_an_over_long_prompt_is_clamped_before_ollama_sees_it(eng):
    """Ollama drops the FRONT of a prompt over num_ctx without a word."""
    _slots(eng, {"a": {"path": "ollama:llama3.1:8b", "n_ctx": 1024}},
           {"coder": "a"})
    big = [{"role": "system", "content": "rules"},
           {"role": "user", "content": "x " * 20000}]
    eng.ce.local_chat(big, role="coder", num_predict=200)
    sent = eng.fake.state.chats[-1]
    total = sum(len(m["content"]) for m in sent["messages"])
    assert total < 1024 * 3, "the prompt went out over the window"
    assert "trimmed to fit" in sent["messages"][1]["content"]


# ============================================================
# The window: learned chars per token, and a refusal instead of a cut
# ============================================================

def _window_slot(eng, n_ctx=1024):
    _slots(eng, {"a": {"path": "ollama:phi3.5:latest", "n_ctx": n_ctx}},
           {"coder": "a"})


def test_every_chat_asks_the_server_to_refuse_rather_than_cut(eng):
    """Ollama drops the FRONT of an over-long prompt — the system prompt —
    unless truncate is false (measured on 0.35: a 1,655-token prompt at
    num_ctx 512 was answered from its last 258 tokens)."""
    eng.ce.local_chat(MSGS, model="ollama:llama3.1:8b")
    assert eng.fake.state.chats[-1]["truncate"] is False


def test_dense_text_the_estimate_misses_is_refitted_not_cut(eng):
    """phi3.5 counts a .gspec at 2.44 chars a token; the clamp's starting
    3.0 lets that prompt past num_ctx. The server refuses it with the exact
    count, the engine re-fits at that ratio, and the second request fits —
    with the system prompt intact."""
    eng.fake.state.chars_per_token = 2.4
    _window_slot(eng)
    big = [{"role": "system", "content": "RULES-AT-THE-FRONT"},
           {"role": "user", "content": '{"k": 1}, ' * 1200}]
    out = eng.ce.local_chat(big, role="coder", num_predict=50)
    assert out == "Hello from the fake."
    assert eng.fake.state.refused == 1
    sent = eng.fake.state.chats[-1]
    assert sent["messages"][0]["content"] == "RULES-AT-THE-FRONT"
    assert eng.ce.last_call_stats("coder").get("prompt_refits") == 1


def test_the_ratio_learned_from_one_call_fits_the_next_first_time(eng):
    eng.fake.state.chars_per_token = 2.4
    _window_slot(eng)
    big = [{"role": "user", "content": '{"k": 1}, ' * 1200}]
    eng.ce.local_chat(big, role="coder", num_predict=50)
    assert eng.fake.state.refused == 1
    eng.ce.local_chat(big, role="coder", num_predict=50)
    assert eng.fake.state.refused == 1, "the lesson was not kept"


def test_a_model_that_packs_text_well_gets_more_of_its_window(eng):
    """llama3.1:8b reads code at ~4.1 chars a token: once measured, the
    clamp keeps more of a long prompt than the flat 3.0 did."""
    eng.fake.state.chars_per_token = 4.0
    _window_slot(eng, 2048)
    first = [{"role": "user", "content": "word " * 600}]       # fits
    eng.ce.local_chat(first, role="coder", num_predict=200)
    big = [{"role": "user", "content": "word " * 4000}]
    eng.ce.local_chat(big, role="coder", num_predict=200)
    kept = len(eng.fake.state.chats[-1]["messages"][0]["content"])
    assert kept > (2048 - 200) * 3.0 * 0.9 * 1.25
    assert eng.fake.state.refused == 0


def test_an_implausible_count_is_not_learned(eng):
    """A server that counted only part of a prompt (a cached prefix) would
    teach a ratio no tokenizer has, and the next prompt would overflow."""
    eng.ce._ollama_learn("m", 50_000, 120)
    eng.ce._ollama_learn("m", 1_000, 80)          # 12.5: also a miscount
    assert eng.ce._ollama_cpt("m") == eng.ce._OLLAMA_CPT_DEFAULT


def test_a_prompt_that_cannot_fit_says_so(eng):
    """Two re-fits, then a clear error — not an endless retry. Messages
    each under the clamp's floor sum past the window on their own."""
    eng.fake.state.chars_per_token = 1.0
    _window_slot(eng, 512)
    many = [{"role": "user", "content": "y" * 70} for _ in range(40)]
    with pytest.raises(RuntimeError, match="does not fit"):
        eng.ce.local_chat(many, role="coder", num_predict=50)
    assert eng.fake.state.refused == 3


def test_the_engine_never_routes_to_a_remote_host(eng):
    with pytest.raises(RuntimeError, match="non-local"):
        eng.ce.local_chat(MSGS, model="ollama:llama3.1:8b",
                          host="http://192.168.1.50:11434")


def test_a_missing_model_says_how_to_install_it_and_pulls_nothing(eng):
    _slots(eng, {"a": {"path": "ollama:granite3.3:8b"}}, {"coder": "a"})
    with pytest.raises(RuntimeError, match="ollama pull granite3.3:8b"):
        eng.ce.local_chat(MSGS, role="coder")
    assert not any(p == "/api/pull" for _m, p, _b in eng.fake.state.requests)


# ============================================================
# Automatic fallback when no GGUF can load
# ============================================================

def test_without_llama_cpp_a_localhost_ollama_answers(eng, monkeypatch,
                                                      tmp_path):
    """The council env has no llama_cpp: every role used to fail with
    'llama-cpp-python is not installed'."""
    monkeypatch.setenv("COUNCIL_OLLAMA_FALLBACK", "1")
    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"GGUF")
    monkeypatch.setenv("COUNCIL_GGUF_PATH", str(gguf))
    out = eng.ce.local_chat(MSGS, role="writer")
    assert out == "Hello from the fake."
    # The best installed US model for 8 GB — never the (bigger-context,
    # tool-capable) non-US qwen the fake also has.
    assert eng.fake.state.chats[-1]["model"] == "llama3.1:8b"
    assert eng.ce.last_call_stats("writer")["backend"] == "ollama"


def test_no_gguf_configured_falls_back_too(eng, monkeypatch):
    monkeypatch.setenv("COUNCIL_OLLAMA_FALLBACK", "1")
    assert eng.ce.local_chat(MSGS) == "Hello from the fake."


def test_a_non_us_model_is_never_the_default(eng, monkeypatch):
    monkeypatch.setenv("COUNCIL_OLLAMA_FALLBACK", "1")
    eng.fake.state.tags = [t for t in DEFAULT_TAGS
                           if t["name"].startswith("qwen")]
    with pytest.raises(eng.ce.BackendUnavailable, match="US-origin"):
        eng.ce.local_chat(MSGS)
    assert eng.fake.state.chats == []


def test_the_fallback_can_be_turned_off(eng, monkeypatch):
    monkeypatch.setenv("COUNCIL_OLLAMA_FALLBACK", "0")
    with pytest.raises(RuntimeError, match="COUNCIL_GGUF_PATH is not set"):
        eng.ce.local_chat(MSGS)
    assert eng.fake.state.chats == []


# ============================================================
# The window a prompt BUILDER is told, before the first call
# ============================================================

def test_the_first_prompt_is_sized_for_the_window_ollama_will_be_sent(
        eng, monkeypatch):
    """No llama_cpp (the council env), nothing called yet in this process:
    effective_n_ctx said 4096 — it could not see that Ollama would serve —
    and Ollama was then sent num_ctx 8192, so nx_ops._n_ctx() cut the
    Dream3D writer's first filter shortlist to half of what fit."""
    from council_core import nx_ops
    monkeypatch.setenv("COUNCIL_OLLAMA_FALLBACK", "1")   # the app default
    ce = eng.ce
    assert ce.effective_n_ctx("main") == 8192
    assert nx_ops._n_ctx() == 8192
    ce.local_chat(MSGS, num_predict=20)
    assert eng.fake.state.chats[-1]["options"]["num_ctx"] == 8192
    assert ce.effective_n_ctx("main") == 8192


def test_an_ollama_slot_is_known_before_its_first_call(eng, monkeypatch,
                                                       tmp_path):
    """With llama_cpp present the GGUF path is tried first, so only the
    slot itself can say Ollama serves it: "ollama:<name>"."""
    monkeypatch.setitem(sys.modules, "llama_cpp",
                        SimpleNamespace(Llama=None))     # installed
    gguf = tmp_path / "main.gguf"
    gguf.write_bytes(b"GGUF")
    monkeypatch.setenv("COUNCIL_GGUF_PATH", str(gguf))
    _slots(eng, {"a": {"path": "ollama:llama3.1:8b", "n_ctx": 16384}},
           {"coder": "a"})
    assert eng.ce.effective_n_ctx("a") == 16384
    # main is a GGUF that has not loaded: the GGUF rule, not Ollama's.
    assert eng.ce.effective_n_ctx("main") == 4096


def test_no_gguf_configured_is_ollamas_window_too(eng, monkeypatch):
    """llama_cpp present but no GGUF to load: _route_chat hands the call to
    Ollama, so the builder is told Ollama's window."""
    monkeypatch.setitem(sys.modules, "llama_cpp",
                        SimpleNamespace(Llama=None))
    monkeypatch.setenv("COUNCIL_OLLAMA_FALLBACK", "1")
    monkeypatch.setenv("COUNCIL_OLLAMA_NUM_CTX", "12288")
    assert eng.ce.effective_n_ctx("main") == 12288


def test_with_the_fallback_off_a_gguf_slot_keeps_the_gguf_window(eng,
                                                                 monkeypatch):
    monkeypatch.setenv("COUNCIL_OLLAMA_FALLBACK", "0")
    assert eng.ce.effective_n_ctx("main") == 4096
    _slots(eng, {"a": {"path": "ollama:llama3.1:8b", "n_ctx": 2048}},
           {"coder": "a"})
    assert eng.ce.effective_n_ctx("a") == 2048      # an Ollama slot still is


def test_council_backend_ollama_forces_it(eng, monkeypatch):
    monkeypatch.setenv("COUNCIL_BACKEND", "ollama")
    monkeypatch.setenv("COUNCIL_OLLAMA_MODEL", "phi3.5")
    monkeypatch.setenv("COUNCIL_OLLAMA_FALLBACK", "0")
    eng.ce.refresh_backend_config()
    eng.ce.local_chat(MSGS)
    assert eng.fake.state.chats[-1]["model"] == "phi3.5:latest"


def test_the_councils_own_roles_route_the_same_way(eng):
    """LocalBackendSpec.generate (the deliberation's call) goes through the
    same router as local_chat, streaming included."""
    _slots(eng, {"w": {"path": "ollama:phi3.5"}}, {"writer": "w"})
    spec = eng.ce.LocalBackendSpec(key="k", host="x", model="m", tags={})
    got = []
    out = spec.generate(developer_instructions="sys", user_text="hi",
                        trace=False, token_callback=got.append, role="writer")
    assert out == "Hello from the fake." and "".join(got) == out
    assert eng.fake.state.chats[-1]["model"] == "phi3.5:latest"


# ============================================================
# Structured output on Ollama
# ============================================================

PLAN_SCHEMA = {
    "type": "object",
    "properties": {"steps": {"type": "array", "maxItems": 4,
                             "items": {"type": "string", "maxLength": 80}},
                   "done": {"type": "boolean"}},
    "required": ["steps", "done"], "additionalProperties": False,
}


def test_a_schema_is_sent_as_format_and_the_reply_validated(eng):
    _slots(eng, {"a": {"path": "ollama:llama3.1:8b"}}, {"coder": "a"})
    eng.fake.state.json_reply = '{"steps": ["a", "b"], "done": true}'
    out = eng.ce.local_chat(MSGS, role="coder", json_schema=PLAN_SCHEMA)
    assert json.loads(out) == {"steps": ["a", "b"], "done": True}
    assert eng.fake.state.chats[-1]["format"] == PLAN_SCHEMA
    st = eng.ce.last_call_stats("coder")
    assert st["constrained"] is True and st["constraint"] == "schema"
    assert st["schema_valid"] is True


def test_a_reply_that_breaks_the_schema_is_reported_not_hidden(eng):
    _slots(eng, {"a": {"path": "ollama:llama3.1:8b"}}, {"coder": "a"})
    eng.fake.state.json_reply = '{"steps": "not a list"}'
    out = eng.ce.local_chat(MSGS, role="coder", json_schema=PLAN_SCHEMA)
    assert out == '{"steps": "not a list"}'      # the caller still gets it
    st = eng.ce.last_call_stats("coder")
    assert st["schema_valid"] is False and st["schema_errors"]


def test_a_reply_cut_off_at_num_predict_is_flagged(eng, caplog):
    """The one failure a grammar cannot prevent: a schema with no bounds
    and a reply that runs into num_predict."""
    _slots(eng, {"a": {"path": "ollama:llama3.1:8b"}}, {"coder": "a"})
    eng.fake.state.done_reason = "length"
    eng.fake.state.json_reply = '{"steps": ["a", "b'
    unbounded = {"type": "object", "properties": {
        "steps": {"type": "array", "items": {"type": "string"}}}}
    with caplog.at_level("INFO"):
        eng.ce.local_chat(MSGS, role="coder", json_schema=unbounded,
                          num_predict=64)
    st = eng.ce.last_call_stats("coder")
    assert st["truncated"] is True and st["schema_valid"] is False
    assert st["schema_worst_case_tokens"] is None
    assert "unbounded" in caplog.text


def test_a_refused_schema_falls_back_to_json_mode(eng):
    """A schema the server refuses fails BEFORE generating, so retrying it
    looser is not a re-sent generation."""
    _slots(eng, {"a": {"path": "ollama:llama3.1:8b"}}, {"coder": "a"})
    eng.fake.state.reject_format = True
    eng.fake.state.json_reply = '{"steps": [], "done": false}'
    eng.ce.local_chat(MSGS, role="coder", json_schema=PLAN_SCHEMA)
    formats = [c.get("format") for c in eng.fake.state.chats]
    assert formats == [PLAN_SCHEMA, "json"]
    assert eng.ce.last_call_stats("coder")["constraint"] == "json"


# ============================================================
# Cancellation, the stall timeout, telemetry
# ============================================================

def test_should_stop_cancels_a_generation_in_progress(eng):
    _slots(eng, {"a": {"path": "ollama:llama3.1:8b"}}, {"coder": "a"})
    eng.fake.state.reply = "word " * 400            # 500 chunks
    eng.fake.state.token_delay = 0.01
    seen = []
    stop = threading.Event()

    def cb(tok):
        seen.append(tok)
        if len(seen) == 3:
            stop.set()

    spec = eng.ce.LocalBackendSpec(key="k", host="x", model="m", tags={})
    t0 = time.perf_counter()
    with pytest.raises(eng.ce.GenerationCancelled) as info:
        eng.ce._route_chat(MSGS, slot="a", role="coder", temperature=0.2,
                           num_predict=999, token_callback=cb,
                           should_stop=stop.is_set)
    took = time.perf_counter() - t0
    assert took < 2.0, f"cancel took {took:.2f}s"
    assert info.value.partial.startswith("word")
    deadline = time.time() + 5
    while not eng.fake.state.disconnected and time.time() < deadline:
        time.sleep(0.02)
    assert eng.fake.state.disconnected == 1, "the server kept generating"
    assert eng.fake.state.completed == 0
    assert len(eng.fake.state.chats) == 1                    # not re-sent
    del spec


def test_should_stop_works_while_the_model_is_still_loading(eng):
    """Ollama sends nothing until the first token; Stop must not wait."""
    _slots(eng, {"a": {"path": "ollama:llama3.1:8b"}}, {"coder": "a"})
    eng.fake.state.first_delay = 3.0
    t0 = time.perf_counter()
    with pytest.raises(eng.ce.GenerationCancelled):
        eng.ce.local_chat(MSGS, role="coder",
                          should_stop=lambda: time.perf_counter() - t0 > 0.3)
    assert time.perf_counter() - t0 < 1.5


def test_a_stalled_server_times_out_once_and_is_not_re_sent(eng,
                                                            monkeypatch):
    _slots(eng, {"a": {"path": "ollama:llama3.1:8b"}}, {"coder": "a"})
    # The first byte gets the cold-start allowance (see
    # test_llm_engine_review); squeeze it to the stall limit here.
    monkeypatch.setenv("COUNCIL_OLLAMA_LOAD_TIMEOUT", "0.5")
    eng.fake.state.first_delay = 3.0
    t0 = time.perf_counter()
    with pytest.raises(TimeoutError):
        eng.ce.local_chat(MSGS, role="coder", timeout=0.5)
    took = time.perf_counter() - t0
    assert 0.4 < took < 2.0, took
    assert len(eng.fake.state.chats) == 1


def test_a_slow_but_moving_generation_is_never_cut(eng):
    """timeout is a STALL limit, not a total: 40 chunks at 30 ms is 1.2 s,
    over a 0.5 s timeout, and must still finish."""
    _slots(eng, {"a": {"path": "ollama:llama3.1:8b"}}, {"coder": "a"})
    eng.fake.state.reply = "abcd" * 40
    eng.fake.state.token_delay = 0.03
    assert eng.ce.local_chat(MSGS, role="coder", timeout=0.5) == "abcd" * 40


def test_telemetry_for_an_ollama_call(eng):
    _slots(eng, {"a": {"path": "ollama:llama3.1:8b"}}, {"coder": "a"})
    eng.ce.local_chat(MSGS, role="coder")
    st = eng.ce.last_call_stats("coder")
    for key in ("backend", "model", "prompt_tokens", "gen_tokens", "seconds",
                "gen_tok_s", "prompt_tok_s", "constrained"):
        assert key in st, key
    assert st["backend"] == "ollama" and st["model"] == "llama3.1:8b"
    assert st["gen_tokens"] == 40 and st["gen_tok_s"] == 40.0
    assert st["prompt_tokens"] == 120 and st["prompt_tok_s"] == 2000.0
    assert st["constrained"] is False
    assert eng.ce.last_call_stats() == st          # the latest call overall
    assert eng.ce.last_call_stats("never-called") == {}


# ============================================================
# chat_tools
# ============================================================

DOC_TOOLS = [{"type": "function", "function": {
    "name": "search_docs", "description": "Search the package docs.",
    "parameters": {"type": "object",
                   "properties": {"query": {"type": "string",
                                            "maxLength": 200}},
                   "required": ["query"], "additionalProperties": False}}},
    {"name": "read_page", "description": "Read one docs page.",
     "parameters": {"type": "object", "properties": {
         "page": {"type": "string", "maxLength": 200}},
         "required": ["page"]}}]


def test_chat_tools_uses_native_tool_calling_where_the_model_has_it(eng):
    _slots(eng, {"d": {"path": "ollama:llama3.1:8b"}}, {"docs": "d"})
    eng.fake.state.tool_calls = [{"function": {
        "name": "search_docs", "arguments": {"query": "imread"}}}]
    out = eng.ce.chat_tools(MSGS, DOC_TOOLS, role="docs")
    assert out["tool_calls"] == [{"name": "search_docs",
                                  "arguments": {"query": "imread"}}]
    sent = eng.fake.state.chats[-1]
    assert [t["function"]["name"] for t in sent["tools"]] == [
        "search_docs", "read_page"]
    assert "format" not in sent


def test_chat_tools_emulates_on_a_model_without_tools(eng):
    """phi3.5 has no "tools" capability: the reply is constrained to
    {tool, arguments} | {answer} instead."""
    _slots(eng, {"d": {"path": "ollama:phi3.5"}}, {"docs": "d"})
    eng.fake.state.json_reply = json.dumps(
        {"tool": "read_page", "arguments": {"page": "io.html"}})
    out = eng.ce.chat_tools(MSGS, DOC_TOOLS, role="docs")
    assert out == {"content": "", "tool_calls": [
        {"name": "read_page", "arguments": {"page": "io.html"}}]}
    sent = eng.fake.state.chats[-1]
    assert "tools" not in sent
    variants = sent["format"]["anyOf"]
    assert [v["properties"].get("tool", {}).get("const") for v in variants
            ] == ["search_docs", "read_page", None]
    assert "search_docs" in sent["messages"][0]["content"]


def test_chat_tools_emulated_answer(eng):
    _slots(eng, {"d": {"path": "ollama:phi3.5"}}, {"docs": "d"})
    eng.fake.state.json_reply = '{"answer": "use imageio.v3.imread"}'
    out = eng.ce.chat_tools(MSGS, DOC_TOOLS, role="docs")
    assert out == {"content": "use imageio.v3.imread", "tool_calls": []}


def test_chat_tools_falls_back_when_the_server_refuses_tools(eng):
    _slots(eng, {"d": {"path": "ollama:llama3.1:8b"}}, {"docs": "d"})
    eng.fake.state.no_tools = True
    eng.fake.state.json_reply = '{"answer": "ok"}'
    out = eng.ce.chat_tools(MSGS, DOC_TOOLS, role="docs")
    assert out["content"] == "ok"
    assert "tools" in eng.fake.state.chats[0]
    assert "format" in eng.fake.state.chats[1]


def test_an_invented_tool_name_is_dropped(eng):
    _slots(eng, {"d": {"path": "ollama:llama3.1:8b"}}, {"docs": "d"})
    eng.fake.state.tool_calls = [{"function": {"name": "rm_rf",
                                               "arguments": {}}}]
    assert eng.ce.chat_tools(MSGS, DOC_TOOLS, role="docs")["tool_calls"] == []


def test_chat_tools_with_no_model_at_all(eng, monkeypatch):
    monkeypatch.setenv("COUNCIL_OLLAMA_FALLBACK", "0")
    with pytest.raises(NotImplementedError):
        eng.ce.chat_tools(MSGS, DOC_TOOLS, role="docs")


# ============================================================
# list_local_models
# ============================================================

def test_list_local_models_has_ollama_and_gguf_with_origin(eng, monkeypatch,
                                                           tmp_path):
    from council_core import model_slots
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    (models_dir / "granite-3.1-8b-instruct-Q4_K_M.gguf").write_bytes(b"GGUF")
    monkeypatch.setattr(model_slots, "gguf_dirs",
                        lambda extra=(): [models_dir])
    got = {m["id"]: m for m in eng.ce.list_local_models()}
    llama = got["ollama:llama3.1:8b"]
    assert llama["backend"] == "ollama" and llama["origin"] == "US"
    assert llama["maker"] == "Meta" and llama["params_b"] == 8.0
    assert llama["quant"] == "Q4_K_M" and llama["size_bytes"] > 4e9
    assert "tools" in llama["capabilities"]
    assert got["ollama:qwen2.5:7b-instruct-q4_K_M"]["origin"] == "non-US"
    gguf = next(m for m in got.values() if m["backend"] == "gguf")
    assert Path(gguf["id"]).is_absolute() and gguf["origin"] == "US"
    assert gguf["maker"] == "IBM"
    for m in got.values():
        assert {"id", "name", "backend", "size_bytes", "params_b", "quant",
                "family", "maker", "origin"} <= set(m)
    # Metadata only: nothing was generated.
    assert eng.fake.state.chats == []


# ============================================================
# The GGUF backend — streamed inside, structured, stoppable
# ============================================================

class StreamLlama:
    """A Llama stand-in that streams like llama-cpp-python 0.3.x and records
    every create_chat_completion kwarg."""

    def __init__(self, model_path, n_ctx=4096, **kw):
        self.kw = kw
        self._n_ctx = n_ctx
        self.calls = []
        self.chunks = ["{", '"steps"', ": [", "]", ', "done": true', "}"]
        self.delay = 0.0
        self.closed = False
        self.n_tokens = 0
        self.finish = "stop"

    def n_ctx(self):
        return self._n_ctx

    def tokenize(self, b):
        return list(range(max(1, len(b) // 4)))

    def create_chat_completion(self, messages, temperature, max_tokens,
                               stream=False, **kw):
        self.calls.append(dict(kw, messages=messages, stream=stream,
                               max_tokens=max_tokens))
        prompt = sum(len(m["content"]) // 4 for m in messages)

        def gen():
            try:
                yield {"choices": [{"delta": {"role": "assistant"}}]}
                for i, c in enumerate(self.chunks):
                    if self.delay:
                        time.sleep(self.delay)
                    self.n_tokens = prompt + i
                    yield {"choices": [{"delta": {"content": c}}]}
                yield {"choices": [{"delta": {},
                                    "finish_reason": self.finish}]}
            finally:
                self.closed = True
        return gen()


@pytest.fixture
def gguf_eng(tmp_path, monkeypatch):
    import council_engine as ce
    vault = tmp_path / "vault"
    vault.mkdir()
    model = tmp_path / "m.gguf"
    model.write_bytes(b"x")
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
    monkeypatch.setenv("COUNCIL_GGUF_PATH", str(model))
    monkeypatch.delenv("COUNCIL_GGUF_N_CTX", raising=False)
    state = SimpleNamespace(llm=None, grammars=[], fail_schema=False)

    def fake_load(p, **kw):
        state.llm = StreamLlama(p, 8192)
        return state.llm

    class Grammar:
        def __init__(self, src):
            self.src = src

        @classmethod
        def from_json_schema(cls, text, verbose=True):
            if state.fail_schema:
                raise ValueError("unsupported keyword")
            state.grammars.append(("schema", text))
            return cls(text)

        @classmethod
        def from_string(cls, text, verbose=True):
            state.grammars.append(("string", text))
            return cls(text)

    grammar_mod = SimpleNamespace(LlamaGrammar=Grammar, JSON_GBNF="root ::= x")
    monkeypatch.setitem(sys.modules, "llama_cpp",
                        SimpleNamespace(Llama=None, llama_grammar=grammar_mod))
    monkeypatch.setitem(sys.modules, "llama_cpp.llama_grammar", grammar_mod)
    monkeypatch.setattr(ce, "_load_gguf", fake_load)
    monkeypatch.setattr(ce, "_GRAMMAR_CACHE", {})
    ce.refresh_backend_config()
    yield SimpleNamespace(ce=ce, state=state)
    ce.refresh_backend_config()


def test_gguf_schema_becomes_a_grammar_and_seed_and_stop_pass_through(
        gguf_eng):
    ce = gguf_eng.ce
    out = ce.local_chat(MSGS, json_schema=PLAN_SCHEMA, seed=3, stop=["###"])
    assert json.loads(out) == {"steps": [], "done": True}
    call = gguf_eng.state.llm.calls[-1]
    assert call["stream"] is True
    assert call["grammar"].src == json.dumps(PLAN_SCHEMA)
    assert call["seed"] == 3 and call["stop"] == ["###"]
    st = ce.last_call_stats()
    assert st["backend"] == "gguf" and st["constraint"] == "schema"
    assert st["schema_valid"] is True and st["gen_tokens"] == 6


def test_gguf_grammar_is_compiled_once_per_schema(gguf_eng):
    ce = gguf_eng.ce
    for _ in range(3):
        ce.local_chat(MSGS, json_schema=PLAN_SCHEMA)
    assert [k for k, _t in gguf_eng.state.grammars] == ["schema"]


def test_gguf_unconvertible_schema_uses_the_json_grammar_and_says_so(
        gguf_eng, caplog):
    gguf_eng.state.fail_schema = True
    with caplog.at_level("WARNING"):
        gguf_eng.ce.local_chat(MSGS, json_schema=PLAN_SCHEMA)
    assert gguf_eng.state.llm.calls[-1]["grammar"].src == "root ::= x"
    assert gguf_eng.ce.last_call_stats()["constraint"] == "json"
    assert "could not be compiled" in caplog.text


def test_gguf_without_a_schema_sends_no_grammar_seed_or_stop(gguf_eng):
    """Defaults keep today's call exactly: nothing extra is passed."""
    gguf_eng.ce.local_chat(MSGS)
    call = gguf_eng.state.llm.calls[-1]
    assert not ({"grammar", "seed", "stop"} & set(call))


def test_gguf_should_stop_closes_the_generator(gguf_eng):
    ce = gguf_eng.ce
    ce.local_chat(MSGS)                                   # load it
    llm = gguf_eng.state.llm
    llm.chunks = ["tok"] * 500
    llm.delay = 0.002
    seen = []
    with pytest.raises(ce.GenerationCancelled) as info:
        ce._route_chat(MSGS, slot="main", temperature=0.2, num_predict=999,
                       token_callback=seen.append,
                       should_stop=lambda: len(seen) >= 5)
    assert len(seen) == 5 and info.value.partial == "tok" * 5
    assert llm.closed


def test_gguf_stop_while_waiting_for_the_model(gguf_eng):
    """Another call holds the model: Stop returns now, not after it."""
    ce = gguf_eng.ce
    ce.local_chat(MSGS)
    llm, lock = ce._slot_llm_and_lock("main")
    lock.acquire()
    try:
        t0 = time.perf_counter()
        with pytest.raises(ce.GenerationCancelled):
            ce.local_chat(MSGS, should_stop=lambda: time.perf_counter() - t0
                          > 0.2)
        assert time.perf_counter() - t0 < 1.0
    finally:
        lock.release()


def test_gguf_telemetry(gguf_eng):
    ce = gguf_eng.ce
    ce.local_chat(MSGS, role="coder")
    st = ce.last_call_stats("coder")
    assert st["backend"] == "gguf" and st["model"] == "m.gguf"
    assert st["gen_tokens"] == 6 and st["prompt_tokens"] > 0
    assert st["seconds"] >= 0 and st["num_ctx"] == 8192
    assert st["constrained"] is False and st["truncated"] is False
    gguf_eng.state.llm.finish = "length"
    ce.local_chat(MSGS, role="coder")
    assert ce.last_call_stats("coder")["truncated"] is True


def test_an_ollama_writer_and_a_gguf_coder_side_by_side(eng, monkeypatch,
                                                        tmp_path):
    """main on Ollama must not stop another role loading its own GGUF (the
    loader asked main's FILE to decide the lock, and main has none)."""
    coder = tmp_path / "coder.gguf"
    coder.write_bytes(b"x")
    loaded = []

    def fake_load(p, **kw):
        llm = StreamLlama(p, 4096)
        llm.chunks = ["gguf answer"]
        loaded.append((Path(p).name, kw.get("is_main")))
        return llm

    monkeypatch.setitem(sys.modules, "llama_cpp", SimpleNamespace(Llama=None))
    monkeypatch.setattr(eng.ce, "_load_gguf", fake_load)
    _slots(eng, {"main": {"path": "ollama:llama3.1:8b"},
                 "c": {"path": str(coder)}}, {"coder": "c"})
    assert eng.ce.local_chat(MSGS, role="writer") == "Hello from the fake."
    assert eng.ce.local_chat(MSGS, role="coder") == "gguf answer"
    assert loaded == [("coder.gguf", False)]
    with pytest.raises(eng.ce.BackendUnavailable, match="served by Ollama"):
        eng.ce._get_gguf_model()


def test_gguf_template_without_a_system_role_is_retried_folded(gguf_eng):
    """Gemma 2's chat template raises on a system message."""
    ce = gguf_eng.ce
    ce.local_chat(MSGS)
    llm = gguf_eng.state.llm
    real = llm.create_chat_completion

    def picky(messages, temperature, max_tokens, stream=False, **kw):
        if any(m["role"] == "system" for m in messages):
            raise ValueError("System role not supported")
        return real(messages, temperature, max_tokens, stream=stream, **kw)

    llm.create_chat_completion = picky
    ce.local_chat(MSGS)
    sent = llm.calls[-1]["messages"]
    assert [m["role"] for m in sent] == ["user"]
    assert sent[0]["content"].startswith("be brief")
