"""The Ledger's model uses the model's own limits, not the council's chat
defaults (user, 2026-10-21: "the model inside of ledger should be able to use
its max tokens and not the default set by the rest of the council") - but
each call only the window it needs: the whole 128K one, sent every call, put
the model in system RAM (user, 2026-10-22: "High RAM usage and lower CPU and
GPU usage in the ledger")."""
from __future__ import annotations




import council_engine as ce
from council_core import ledger_llm


def test_the_engine_sends_the_whole_window_only_when_asked():
    entry = {"context_length": 131072}
    assert ce._ollama_num_ctx("main", entry) <= 32768        # the council's default
    with ce.model_window():
        assert ce._ollama_num_ctx("main", entry) == 131072
    with ce.model_window(20000):
        assert ce._ollama_num_ctx("main", entry) == 20000
    with ce.model_window(500000):                           # never past the model's own
        assert ce._ollama_num_ctx("main", entry) == 131072
    assert ce._ollama_num_ctx("main", entry) <= 32768        # and back after


def test_the_reply_gets_what_the_prompt_leaves(monkeypatch):
    monkeypatch.delenv("COUNCIL_LEDGER_NUM_PREDICT", raising=False)
    prompt = "x" * 3000
    assert ledger_llm.reply_budget(prompt, 131072) == 131072 - (1000 + 64) - 256
    assert ledger_llm.reply_budget("x" * 60000, 8192) == ledger_llm.REPLY_FLOOR
    monkeypatch.setenv("COUNCIL_LEDGER_NUM_PREDICT", "4096")
    assert ledger_llm.reply_budget(prompt, 131072) == 4096


def test_a_call_gets_the_window_it_needs_not_the_whole_one(monkeypatch):
    monkeypatch.delenv("COUNCIL_LEDGER_NUM_CTX", raising=False)
    assert ledger_llm.ledger_window(131072, "a short prompt") == 8192
    assert ledger_llm.ledger_window(0, "a short prompt") == 8192
    assert ledger_llm.ledger_window(131072, "x" * 60000) == 32768     # ~20K tokens
    assert ledger_llm.ledger_window(131072, "x" * 600000) == 131072   # never past its own
    assert ledger_llm.ledger_window(4096, "a short prompt") == 4096


def test_the_window_can_be_set_outright(monkeypatch):
    monkeypatch.setenv("COUNCIL_LEDGER_NUM_CTX", "16384")
    assert ledger_llm.ledger_window(131072, "a short prompt") == 16384
    assert ledger_llm.ledger_window(8192) == 8192
    assert ledger_llm.gguf_window(131072) == 16384


def test_an_ollama_model_is_asked_with_the_window_the_call_needs(monkeypatch):
    monkeypatch.delenv("COUNCIL_LEDGER_NUM_CTX", raising=False)
    monkeypatch.delenv("COUNCIL_LEDGER_NUM_PREDICT", raising=False)
    seen = {}

    def local_chat(messages, **kw):
        seen["window"] = ce._WINDOW.get()
        seen["num_predict"] = kw["num_predict"]
        seen["sent_ctx"] = ce._ollama_num_ctx("main", {"context_length": 131072})
        return '{"mappings": []}'
    monkeypatch.setattr(ce, "local_chat", local_chat)
    from council_core import local_models
    monkeypatch.setattr(local_models, "maker_and_origin", lambda name: ("IBM", "US"))
    chat = ledger_llm.chat_for("ollama:granite3.3:8b")
    chat("a short prompt", {"type": "object"})
    # 8K, not the model's whole 128K: the model stays on the GPU; the reply
    # fills what the prompt leaves (0: the engine sizes it)
    assert seen["window"] == 8192 and seen["sent_ctx"] == 8192
    assert seen["num_predict"] == 0
    chat("x" * 60000, {"type": "object"})                  # a long document: more
    assert seen["sent_ctx"] == 32768
    monkeypatch.setenv("COUNCIL_LEDGER_NUM_CTX", "16384")
    monkeypatch.setenv("COUNCIL_LEDGER_NUM_PREDICT", "4096")
    chat("a short prompt", {"type": "object"})
    assert seen["sent_ctx"] == 16384 and seen["num_predict"] == 4096


def test_a_fill_reply_has_all_the_window_the_prompt_leaves():
    msgs = [{"role": "user", "content": "x" * 4000}]
    count = (lambda t: len(t) // 4)                         # 1,000 tokens of prompt
    out, reply = ce._clamp_messages_to_ctx(msgs, 0, 131072, count_tokens=count)
    assert out == msgs and reply == 131072 - 1000 - ce._template_margin(1)
    # a council call keeps its half-window cap
    _out, capped = ce._clamp_messages_to_ctx(msgs, 200000, 131072, count_tokens=count)
    assert capped <= 131072 // 2
    # a prompt that leaves too little is fitted as ever, for a FILL_FLOOR reply
    big = [{"role": "user", "content": "y" * 40000}]       # 10,000 tokens in an 8K window
    out, reply = ce._clamp_messages_to_ctx(big, 0, 8192, count_tokens=count)
    assert reply == ce.FILL_FLOOR and "trimmed" in out[0]["content"]


def test_a_gguf_is_loaded_with_the_council_s_cap_not_its_trained_length(monkeypatch,
                                                                          tmp_path):
    monkeypatch.delenv("COUNCIL_LEDGER_NUM_CTX", raising=False)
    got = {}

    class FakeLlama:
        def n_ctx(self):
            return 65536

        def create_chat_completion(self, **kw):
            got["max_tokens"] = kw["max_tokens"]
            return {"choices": [{"message": {"content": "{}"}}]}

    def load(p, **kw):
        got["n_ctx_max"] = kw.get("n_ctx_max")
        return FakeLlama()
    monkeypatch.setattr(ce, "_load_gguf", load)
    monkeypatch.setattr(ce, "read_gguf_metadata", lambda p: {"llama.context_length": 131072})
    monkeypatch.setattr(ledger_llm, "_is_main_gguf", lambda path: False)
    monkeypatch.setattr(ledger_llm, "_gpu_ok", lambda: None)
    ledger_llm._GGUF["key"] = ledger_llm._GGUF["llm"] = None
    model = tmp_path / "granite.gguf"
    model.write_bytes(b"GGUF")
    chat = ledger_llm.chat_for(str(model))
    chat("a short prompt", {"type": "object"})
    # a 128K window's KV cache does not fit a card: 32K at most (the loader
    # sizes it to the card too); the reply still gets all the window leaves
    assert got["n_ctx_max"] == ledger_llm.GGUF_WINDOW == 32768
    assert got["max_tokens"] > 60000
    ledger_llm._GGUF["key"] = ledger_llm._GGUF["llm"] = None
