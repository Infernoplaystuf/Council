"""The Ledger's model uses the model's own limits, not the council's chat
defaults (user, 2026-10-21: "the model inside of ledger should be able to use
its max tokens and not the default set by the rest of the council")."""
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


def test_the_window_can_be_capped_for_a_small_card(monkeypatch):
    monkeypatch.delenv("COUNCIL_LEDGER_NUM_CTX", raising=False)
    assert ledger_llm.ledger_window(131072) == 131072
    monkeypatch.setenv("COUNCIL_LEDGER_NUM_CTX", "16384")
    assert ledger_llm.ledger_window(131072) == 16384
    assert ledger_llm.ledger_window(8192) == 8192


def test_an_ollama_model_is_asked_with_its_whole_window(monkeypatch):
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
    # the whole window (0), the reply filling it (0): the engine sizes both
    # from the model it serves, no extra question to Ollama
    assert seen["window"] == 0 and seen["sent_ctx"] == 131072
    assert seen["num_predict"] == 0
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


def test_a_gguf_is_loaded_with_its_trained_length(monkeypatch, tmp_path):
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
    assert got["n_ctx_max"] == 131072                       # not the council's 32K cap
    assert got["max_tokens"] > 60000                        # all the window leaves
    ledger_llm._GGUF["key"] = ledger_llm._GGUF["llm"] = None
