"""
The prompt clamp's window is the window of the model that serves the call.

Measured before the fix: with COUNCIL_GGUF_N_CTX unset (the launchers do not
set it) the clamp used min(4096, instance n_ctx), so Phi-4 — loaded at 16384 by
the VRAM-aware ladder on an RTX 5080 — was clamped to 4096 on every call: the
writer's 2400-token reply cut to 2016, its prompt budget to ~2016 tokens, and
its own system prompt trimmed in the middle on every answer.

No GGUF is loaded and no GPU is touched: _load_gguf is replaced by a fake
Llama with a chosen n_ctx, and llama_cpp itself by a module that refuses.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from council_core import model_slots as ms

MARKER = "trimmed to fit the model's context window"


def margin(n_messages):
    """The clamp's chat-template allowance for a call of n messages."""
    return 64 + 8 * n_messages


class FakeLlama:
    """What the engine uses of llama_cpp.Llama — n_ctx(), tokenize(),
    create_chat_completion() — plus llama-cpp-python's window rules: a prompt
    at or over n_ctx raises, and prompt + max_tokens over n_ctx has max_tokens
    cut silently (recorded as ``cut``, so a test can show the clamp never
    leaves that to llama-cpp)."""

    TEMPLATE = 8     # chat-template tokens per message, generous for ChatML

    def __init__(self, path, n_ctx, bytes_per_token=4):
        self.path = Path(path)
        self._n_ctx = n_ctx
        self.bpt = bytes_per_token
        self.calls = []
        self.tokenized = 0

    def n_ctx(self):
        if self._n_ctx is None:
            raise RuntimeError("context not created")
        return self._n_ctx

    def tokenize(self, b):
        self.tokenized += 1
        return [0] * ((len(b) + self.bpt - 1) // self.bpt)

    def create_chat_completion(self, messages, temperature, max_tokens,
                               stream=False):
        window = self._n_ctx or 4096
        prompt = sum(len(self.tokenize(m["content"].encode("utf-8")))
                     + self.TEMPLATE for m in messages)
        if prompt >= window:
            raise ValueError(f"Requested tokens ({prompt}) exceed context "
                             f"window of {window}")
        self.calls.append(SimpleNamespace(
            messages=[dict(m) for m in messages], max_tokens=max_tokens,
            prompt=prompt, cut=prompt + max_tokens > window))
        if stream:
            return iter([{"choices": [{"delta": {"content": "ok"}}]}])
        return {"choices": [{"message": {"content": self.path.stem}}]}


def _refuse(*_a, **_k):
    raise AssertionError("a test tried to load a real model")


@pytest.fixture
def engine(tmp_path, monkeypatch):
    import council_engine as ce
    vault = tmp_path / "vault"
    vault.mkdir()
    models = tmp_path / "models"
    models.mkdir()
    for name in ("main", "fast"):
        (models / f"{name}.gguf").write_bytes(b"x")
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
    monkeypatch.setenv("COUNCIL_GGUF_PATH", str(models / "main.gguf"))
    for var in ("COUNCIL_GGUF_N_CTX", "COUNCIL_GGUF_GPU_LAYERS",
                "COUNCIL_GGUF_CLIP_PATH", "COUNCIL_AGENT_MEMORY_ENABLE",
                "COUNCIL_REMOTE_NODES", "COUNCIL_DEMO_SILO"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setitem(sys.modules, "llama_cpp",
                        SimpleNamespace(Llama=_refuse))
    # No VRAM probe (it would shell out to nvidia-smi): a second slot plans
    # onto the CPU, and the fake loader ignores placement anyway.
    monkeypatch.setattr(ce, "_available_gpu_bytes", lambda: (None, "test"))
    # What an earlier test's real load remembered is not this test's model.
    monkeypatch.setattr(ce, "_LAST_N_CTX", None)
    monkeypatch.setattr(ce, "_LAST_N_CTX_KEY", "", raising=False)
    state = SimpleNamespace(ctx={"main": 16384, "fast": 16384}, bpt=4,
                            loaded={})

    def fake_load(p, **_kw):
        stem = Path(p).stem
        llm = FakeLlama(p, state.ctx[stem], state.bpt)
        state.loaded[stem] = llm
        return llm

    monkeypatch.setattr(ce, "_load_gguf", fake_load)
    ce.refresh_backend_config()
    yield SimpleNamespace(ce=ce, vault=vault, models=models, state=state)
    ce.refresh_backend_config()


def _fast_slot(env, *roles):
    ms.save(env.vault, ms.parse({
        "slots": {"fast": {"path": str(env.models / "fast.gguf")}},
        "roles": {r: "fast" for r in roles}}))
    env.ce.refresh_backend_config()


def _writer(env):
    models = env.ce.build_personalities(pins={}, vault_dir=env.vault,
                                        session_id="t", trace=False)
    w = models["writer"]
    # ~9 KB, distinct lines so a middle trim is visible.
    w.system_prompt = "\n".join(f"rule {i:03d}: keep the writer's voice."
                                for i in range(260))
    assert 8500 < len(w.system_prompt) < 10000
    return w


def _fits(call, n_ctx):
    """Prompt (content AND the fake's per-message template tokens) + reply
    inside the window, so llama-cpp neither refuses nor cuts the call."""
    return (not call.cut) and call.prompt + call.max_tokens <= n_ctx


# ============================================================
# The window is the serving instance's n_ctx
# ============================================================

@pytest.mark.parametrize("stream", [False, True])
def test_a_16k_writer_keeps_its_reply_and_its_system_prompt(engine, stream):
    w = _writer(engine)
    cb = (lambda _t: None) if stream else None
    w.respond("hello", token_callback=cb)
    call = engine.state.loaded["main"].calls[-1]
    # A "long" reply (council_core.token_budget): a sixth of a 16k window,
    # more than its old 2400, and the prompt keeps every token.
    assert call.max_tokens == 16384 // 6 > 2400
    assert call.messages[0]["content"] == w.system_prompt
    assert not any(MARKER in m["content"] for m in call.messages)
    assert _fits(call, 16384)


def test_on_a_4k_model_the_same_writer_is_still_capped_and_trimmed(engine):
    engine.state.ctx["main"] = 4096
    w = _writer(engine)
    w.respond("hello")
    call = engine.state.loaded["main"].calls[-1]
    # system + user: half of what the window leaves beside the template.
    assert call.max_tokens == (4096 - margin(2)) // 2 == 2008
    assert MARKER in call.messages[0]["content"]
    assert _fits(call, 4096)


def test_the_old_env_var_no_longer_bounds_the_window(engine, monkeypatch):
    """COUNCIL_GGUF_N_CTX is the Tk era's fixed window; the window follows
    the model now (user, 2026-10-23), so a 16k instance is a 16k window."""
    monkeypatch.setenv("COUNCIL_GGUF_N_CTX", "4096")
    w = _writer(engine)
    w.respond("hello")
    call = engine.state.loaded["main"].calls[-1]
    assert call.max_tokens == 16384 // 6
    assert not any(MARKER in m["content"] for m in call.messages)


def test_the_window_is_the_instance_s_own_whatever_the_old_env_var(
        monkeypatch):
    import council_engine as ce
    monkeypatch.setenv("COUNCIL_GGUF_N_CTX", "32768")
    quarter = lambda s: (len(s) + 3) // 4         # not whatever main is loaded
    big = [{"role": "system", "content": "be brief"},
           {"role": "user", "content": "word " * 8000}]      # 10k tokens
    msgs, reply = ce._clamp_messages_to_ctx(big, 2400, 8192,
                                            count_tokens=quarter)
    assert reply == 2400
    total = sum(quarter(m["content"]) for m in msgs)
    assert MARKER in msgs[1]["content"] and total + reply + margin(2) <= 8192
    # With no instance to ask, 4096 — the variable is not read.
    msgs2, reply2 = ce._clamp_messages_to_ctx(big, 2400, count_tokens=quarter)
    assert MARKER in msgs2[1]["content"] and reply2 < 2400


@pytest.mark.parametrize("raw", ["", "  ", "auto", "0", "4096"])
def test_any_old_env_value_is_ignored(engine, monkeypatch, raw):
    """COUNCIL_GGUF_N_CTX is not read any more (user, 2026-10-23): whatever
    it holds, the instance's n_ctx is the truth."""
    monkeypatch.setenv("COUNCIL_GGUF_N_CTX", raw)
    engine.ce.local_chat([{"role": "user", "content": "hi"}],
                         num_predict=2400, role="writer")
    assert engine.state.loaded["main"].calls[-1].max_tokens == 2400


def test_a_fast_slot_role_is_clamped_to_its_own_window(engine):
    engine.state.ctx.update(main=16384, fast=4096)
    _fast_slot(engine, "peasant")
    ce = engine.ce
    msgs = [{"role": "system", "content": "x " * 4600},   # ~9 KB
            {"role": "user", "content": "hi"}]
    ce.local_chat(msgs, num_predict=2400, role="writer")
    main, = engine.state.loaded["main"].calls
    assert main.max_tokens == 2400 and main.messages == msgs

    ce.local_chat(msgs, num_predict=2400, role="peasant")
    fast, = engine.state.loaded["fast"].calls
    assert fast.max_tokens == 2008
    assert MARKER in fast.messages[0]["content"] and _fits(fast, 4096)
    assert ce.slot_status()["fast"]["n_ctx"] == 4096


def test_a_bigger_fast_slot_is_not_held_to_mains_window(engine):
    engine.state.ctx.update(main=4096, fast=16384)
    _fast_slot(engine, "peasant")
    engine.ce.local_chat([{"role": "user", "content": "hi"}],
                         num_predict=2400, role="peasant")
    assert engine.state.loaded["fast"].calls[-1].max_tokens == 2400


def test_the_serving_instance_counts_the_tokens(engine):
    """Not main's tokenizer for a fast-slot call — its vocabulary, and its
    lock, belong to another model."""
    _fast_slot(engine, "peasant")
    ce = engine.ce
    msgs = [{"role": "user", "content": "hi"}]
    ce.local_chat(msgs, role="writer")                   # load main
    main = engine.state.loaded["main"]
    before = main.tokenized
    ce.local_chat(msgs, role="peasant")
    fast = engine.state.loaded["fast"]
    assert main.tokenized == before
    # The clamp's count, then the fake's own prompt measurement.
    assert fast.tokenized >= 2


# ============================================================
# Unknown window, and the clamp stays inside the real one
# ============================================================

def test_an_unknown_n_ctx_falls_back_to_4096(engine):
    engine.state.ctx["main"] = None          # n_ctx() raises
    engine.ce.local_chat([{"role": "user", "content": "hi"}],
                         num_predict=2400, role="writer")
    assert engine.state.loaded["main"].calls[-1].max_tokens == (
        4096 - margin(1)) // 2 == 2012


def test_no_model_and_no_env_var_is_4096(monkeypatch):
    import council_engine as ce
    monkeypatch.delenv("COUNCIL_GGUF_N_CTX", raising=False)
    _msgs, reply = ce._clamp_messages_to_ctx(
        [{"role": "user", "content": "hi"}], 2400)
    assert reply == 2012


@pytest.mark.parametrize("bpt", [2, 3, 4])
def test_dense_text_is_trimmed_until_it_really_fits(engine, bpt):
    """Code and CSV tokenize at fewer chars per token than prose. A trim that
    assumed 4 kept ~33% too much at 3 chars/token; on a 16k window with a
    2400-token reply that pushed the prompt past n_ctx and llama-cpp refused
    it (the fake raises the same ValueError)."""
    engine.state.bpt = bpt
    code = "".join(f"x{i}=f(y[{i}]);" for i in range(9000))   # ~110 KB
    engine.ce.local_chat([{"role": "system", "content": "review this"},
                          {"role": "user", "content": code}],
                         num_predict=2400, role="writer")
    call = engine.state.loaded["main"].calls[-1]
    assert call.max_tokens == 2400
    assert MARKER in call.messages[1]["content"]
    assert call.messages[0]["content"] == "review this"
    assert _fits(call, 16384)


def test_when_trimming_cannot_fit_the_reply_gives_way():
    """Many messages, each under the trim floor: prompt + reply + margin must
    still fit, so the reply shrinks rather than llama-cpp cutting it."""
    import council_engine as ce
    count = lambda s: len(s)                     # 1 char = 1 token
    msgs = [{"role": "user", "content": "y" * 70} for _ in range(40)]
    out, reply = ce._clamp_messages_to_ctx(msgs, 2400, 4096,
                                           count_tokens=count)
    assert out == msgs                           # 70 chars: under the floor
    total = sum(count(m["content"]) for m in out)          # 2800
    # Without the shrink, the half-window cap: 2800 + 1856 + 384 overruns
    # the window by 944.
    assert reply == 4096 - margin(40) - total == 912


def test_the_clamp_counts_with_the_given_counter():
    import council_engine as ce
    seen = []

    def count(s):
        seen.append(s)
        return len(s)

    ce._clamp_messages_to_ctx([{"role": "user", "content": "abc"}], 100,
                              4096, count_tokens=count)
    assert seen == ["abc"]


# ============================================================
# The chat template's tokens grow with the message count
# ============================================================

def test_many_messages_leave_room_for_the_template(engine):
    """The fake charges 8 template tokens per message, as a ChatML-style
    template does. With a flat 64-token margin, 69 messages of 199 content
    tokens (13,731: inside the 13,920 content budget, so nothing was
    trimmed) made a 14,283-token prompt, and 14,283 + 2400 passed 16,384:
    llama-cpp cut the reply to 2101 without a word."""
    msgs = [{"role": "user" if i % 2 else "assistant", "content": "t" * 796}
            for i in range(69)]
    engine.ce.local_chat(msgs, num_predict=2400, role="writer")
    call = engine.state.loaded["main"].calls[-1]
    assert call.max_tokens == 2400
    assert _fits(call, 16384)


# ============================================================
# Prompt BUILDERS budget for the window the clamp enforces
# ============================================================

def test_the_budget_report_uses_the_loaded_window(engine):
    """context_budget_report took get_n_ctx(): 4096 with the env var unset,
    so on a 16k model a 5,000-token prompt was reported over the window."""
    ce = engine.ce
    ce.local_chat([{"role": "user", "content": "hi"}], role="writer")
    rep = ce.context_budget_report("x" * 20000)       # 5,000 tokens here
    assert rep["input_tokens"] == 5000
    assert rep["n_ctx"] == 16384
    assert not rep["over_window"] and not rep["over_safe"]


def test_the_window_is_asked_without_loading_a_model(engine):
    ce = engine.ce
    assert ce.effective_n_ctx() == 4096
    assert ce.context_budget_report("hi")["n_ctx"] == 4096
    assert engine.state.loaded == {}


def test_with_nothing_loaded_a_gguf_slot_gets_4096(engine, monkeypatch):
    """Nothing loaded and no load remembered, on GGUF slots: 4096, whatever
    the old COUNCIL_GGUF_N_CTX says. The slot config IS read now — only it says whether Ollama serves
    the slot (council_engine._ollama_will_serve: "ollama:<name>", or no GGUF
    to load) — and what is read is THIS vault's file. The Dream3D tests used
    to reach effective_n_ctx through nx_ops with no vault set and read
    ~/.council/vault/model_slots.json, the real one, keeping it cached for
    every test after them."""
    from council_core import model_slots, nx_ops
    ce = engine.ce
    _fast_slot(engine)                  # a slot only this vault's file has
    model_slots.invalidate()
    assert ce.effective_n_ctx() == 4096
    assert nx_ops._n_ctx() == 4096
    assert ce.context_budget_report("hi")["n_ctx"] == 4096
    monkeypatch.setenv("COUNCIL_GGUF_N_CTX", "8192")
    assert ce.effective_n_ctx("fast") == 4096
    assert "fast" in model_slots._current.slots
    assert engine.state.loaded == {}


def test_each_slot_reports_the_window_it_loaded_with(engine):
    engine.state.ctx.update(main=16384, fast=8192)
    _fast_slot(engine, "peasant")
    ce = engine.ce
    assert ce.effective_n_ctx("fast") == 4096          # not loaded yet
    ce.local_chat([{"role": "user", "content": "hi"}], role="peasant")
    assert ce.effective_n_ctx("fast") == 8192
    assert ce.effective_n_ctx("main") == 4096          # still not loaded
    ce.local_chat([{"role": "user", "content": "hi"}], role="writer")
    assert ce.effective_n_ctx("main") == 16384
    assert ce.effective_n_ctx("fast") == 8192


def test_the_old_env_var_is_not_the_window_before_or_after_a_load(
        engine, monkeypatch):
    """The loaded instance says what the window is; before a load, 4096."""
    monkeypatch.setenv("COUNCIL_GGUF_N_CTX", "32768")
    ce = engine.ce
    assert ce.effective_n_ctx() == 4096
    ce.local_chat([{"role": "user", "content": "hi"}], role="writer")
    assert ce.effective_n_ctx() == 16384


def test_a_load_in_progress_does_not_freeze_the_caller(engine, monkeypatch):
    """_get_slot_model holds _SLOT_LOAD_LOCK for a whole load, and a GUI
    thread sizing a warning must not wait that long. The last main load's
    record (_LAST_N_CTX, written before Llama() is called) answers instead —
    no more than rung 3's blind 8192, since it may be from before a release
    and the reload re-measures free VRAM."""
    ce = engine.ce
    monkeypatch.setattr(ce, "_LAST_N_CTX", 16384)
    monkeypatch.setattr(ce, "_LAST_N_CTX_KEY",
                        ce._path_key(engine.models / "main.gguf"))
    monkeypatch.setattr(ce, "_LAST_N_CTX_INPUTS",
                        ce._placement_inputs(ce._slot_config()),
                        raising=False)
    held, done = threading.Event(), threading.Event()

    def loading():
        with ce._SLOT_LOAD_LOCK:
            held.set()
            done.wait(10)

    t = threading.Thread(target=loading)
    t.start()
    try:
        assert held.wait(5)
        t0 = time.perf_counter()
        n = ce.effective_n_ctx()
        waited = time.perf_counter() - t0
    finally:
        done.set()
        t.join(10)
    assert n == 8192 and waited < 2.0


class _Bridge:
    def catalog(self):
        return {"filters": [{"name": "F"}]}


class _Generator:
    def __init__(self):
        self.n_ctx = "never called"

    def write_script(self, task, catalog, model_call, n_ctx=None):
        self.n_ctx = n_ctx
        return {"ok": False, "code": None, "errors": ["recorded"]}


def test_the_dream3d_writer_sizes_its_shortlist_to_the_loaded_window(engine):
    """nx_ops passed get_n_ctx(): on a 16k main the filter shortlist was
    budgeted for 4096 — 9,408 chars where 48,729 fit."""
    from council_core import nx_ops
    engine.ce.local_chat([{"role": "user", "content": "hi"}], role="writer")
    gen = _Generator()
    try:
        nx_ops.write_script("make an stl", engine.vault, bridge=_Bridge(),
                            generator=gen)
    finally:
        nx_ops.invalidate_catalog(engine.vault)
    assert gen.n_ctx == 16384


# ============================================================
# prompt_room — sizing a text before its call, as the clamp will fit it
# ============================================================
# Found in review of 217f2d4: council_core.turn_context sized the vault
# blocks for 75% of the window less a guessed 500 tokens, while the Writer's
# call adds a 4,953-character system prompt and keeps 2,400 tokens for its
# answer — so on Ollama's 8,192 the clamp cut the middle out of a named file.


def _two_messages(system, user):
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def test_a_text_sized_to_the_room_is_not_cut(monkeypatch):
    import council_engine as ce
    monkeypatch.setattr(ce, "_OLLAMA_CPT", {})
    system, head = "S" * 4953, "RECENT CONVERSATION:\nUser: hi\n\nUSER TASK:\n"
    room = ce.prompt_room("main", frame=system + "\n" + head,
                          num_predict=2400, n_ctx=8192)
    assert room["reply"] == 2400 and room["budget"] == 8192 - 2400 - margin(2)
    count = room["count"]
    text = "y" * int(room["room"] * ce._budget_cpt())
    assert count(text) <= room["room"]
    counter = ce._ollama_counter(ce._OLLAMA_CPT_DEFAULT)
    sent = _two_messages(system, head + text)
    kept, reply = ce._clamp_messages_to_ctx(sent, 2400, 8192,
                                            count_tokens=counter)
    assert kept == sent and reply == 2400
    # The room is the real limit, not a guess with slack: a little more and
    # the clamp cuts.
    over = _two_messages(system, head + text + "y" * 30)
    cut, _reply = ce._clamp_messages_to_ctx(over, 2400, 8192,
                                            count_tokens=counter)
    assert MARKER in cut[1]["content"]


def test_a_denser_measured_ratio_shrinks_the_room(monkeypatch):
    """The clamp counts an Ollama model at the densest ratio measured for
    it; the room is counted at the densest any model was measured at."""
    import council_engine as ce
    monkeypatch.setattr(ce, "_OLLAMA_CPT", {})
    plain = ce.prompt_room("main", frame="", num_predict=0, n_ctx=8192)
    monkeypatch.setattr(ce, "_OLLAMA_CPT", {"phi3.5:latest": [2.44, 3.3]})
    dense = ce.prompt_room("main", frame="", num_predict=0, n_ctx=8192)
    text = "z" * 3000
    assert dense["count"](text) > plain["count"](text)
    # A model that has only been measured LESS dense never widens it.
    monkeypatch.setattr(ce, "_OLLAMA_CPT", {"llama3.1:8b": [3.9]})
    assert ce.prompt_room("main", n_ctx=8192)["count"](text) == \
        plain["count"](text)


def test_a_thinking_model_keeps_its_thinking_room(monkeypatch):
    import council_engine as ce
    monkeypatch.setattr(ce, "_OLLAMA_THINKS", {"main": True})
    room = ce.prompt_room("main", num_predict=2400, n_ctx=16384)
    assert room["reply"] == 2400 + ce._think_headroom()


GPT_OSS = {"name": "gpt-oss:20b", "family": "gptoss",
           "capabilities": ["completion", "tools", "thinking"]}
OTHER_THINKER = {"name": "olmo-think:7b", "family": "olmo",
                 "capabilities": ["completion", "thinking"]}


@pytest.mark.parametrize("entry, think, extra", [
    # gpt-oss always thinks: low by default, the step's level otherwise
    # (council-map's per-step thinking — the Writer's synthesis is "high").
    (GPT_OSS, None, "low"), (GPT_OSS, "medium", "medium"),
    (GPT_OSS, "high", "high"),
    # A model that can stop thinking thinks only on a "high" step.
    (OTHER_THINKER, None, None), (OTHER_THINKER, "medium", None),
    (OTHER_THINKER, "high", True)])
def test_the_room_keeps_the_thinking_the_call_will_do(monkeypatch, entry,
                                                      think, extra):
    """The reply the clamp keeps room for is num_predict plus the headroom
    the CALL gets — council-map made that the step's thinking level, so the
    slot's last call (what _OLLAMA_THINKS held) says nothing about the next.
    Sized for "high", the Writer's synthesis is not cut by 2,304 tokens of
    thinking nobody planned for."""
    import council_engine as ce
    monkeypatch.delenv("COUNCIL_OLLAMA_THINK", raising=False)
    monkeypatch.delenv("COUNCIL_OLLAMA_THINK_HEADROOM", raising=False)
    monkeypatch.setattr(ce, "_OLLAMA_THINKS", {"main": dict(entry)})
    room = ce.prompt_room("main", num_predict=2400, n_ctx=32768, think=think)
    want = 2400 + (ce._think_headroom(extra) if extra is not None else 0)
    assert room["reply"] == want
    with ce.think_level(think):
        sent = ce.ollama_think(entry)
    assert (sent not in (None, False)) == (extra is not None)


def test_a_model_that_cannot_think_gets_no_thinking_room(monkeypatch):
    import council_engine as ce
    monkeypatch.setattr(ce, "_OLLAMA_THINKS", {"main": False})
    room = ce.prompt_room("main", num_predict=2400, n_ctx=16384, think="high")
    assert room["reply"] == 2400


def test_the_frame_is_what_respond_sends_besides_the_text(tmp_path):
    """PersonalityModel.prompt_frame is the system prompt and the blocks
    respond() puts in front of the text — the same stitch respond() sends —
    and the reply it asks for."""
    import council_engine as ce
    models = ce.build_personalities(pins={}, vault_dir=tmp_path,
                                    session_id="s1", trace=False)
    writer = models["writer"]
    writer.memory_scope = "conversation"
    writer.conversation_store.append("s1", {"who": "User",
                                            "text": "the pump in bay 4?"})
    sent = {}

    class Spec:
        def generate(self, **kwargs):
            sent.update(kwargs)
            return "ok"

    writer.registry = SimpleNamespace(get=lambda _k: Spec(),
                                      best_for=lambda **_k: Spec())
    writer.respond("QUESTION")
    frame, predict = writer.prompt_frame()
    assert predict == 2400 == sent["max_tokens"]
    assert sent["user_text"] == writer.stitch("QUESTION")
    assert frame == sent["developer_instructions"] + "\n" + writer.stitch("")
    assert "the pump in bay 4?" in frame and "RECENT CONVERSATION" in frame
    assert sent["user_text"].endswith("USER TASK:\nQUESTION")
