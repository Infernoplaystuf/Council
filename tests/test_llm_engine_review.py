"""
Adversarial-review regressions for the local-model engine (llm/engine):
malformed server output, hostile model replies, MCP tool schemas as servers
really write them, and the first chat_tools call of a session.

No model is ever run: Ollama is tests/fake_ollama.py (or a raw line server
below) on 127.0.0.1, and the vault is a temp dir.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from council_core import structured_output as so
from tests.fake_ollama import DEFAULT_TAGS
from tests.test_llm_engine import (MSGS, DOC_TOOLS, _slots, eng,  # noqa: F401
                                   fake)


@pytest.fixture
def default_recursion_limit():
    """Python's default limit, so "too deep" means the same in every run: a
    module imported earlier in a full run may have raised it, and then 3,000
    brackets parse fine and these tests prove nothing."""
    import sys
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(1000)
    yield
    sys.setrecursionlimit(old)


class RawServer:
    """An Ollama-ish server whose /api/chat streams exactly ``lines``."""

    def __init__(self, lines):
        outer = self
        self.lines = lines
        self.chats = []

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def _json(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/version":
                    self._json({"version": "x"})
                else:
                    self._json({"models": DEFAULT_TAGS})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.chats.append(json.loads(self.rfile.read(n) or b"{}"))
                self.send_response(200)
                self.end_headers()
                for ln in outer.lines:
                    self.wfile.write(ln.encode() + b"\n")
                    self.wfile.flush()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


# ============================================================
# A stream line that is JSON but not an Ollama chunk
# ============================================================

@pytest.mark.parametrize("bad", [
    '[1, 2]', '"text"', '5', 'null', '{"message": "str"}',
    '{"message": {"content": 5}}',
    pytest.param('[' * 5000 + ']' * 5000, id="nested-5000-deep")])
def test_a_malformed_stream_line_is_skipped_not_a_crash(
        eng, monkeypatch, bad, default_recursion_limit):
    """Each of these raised AttributeError / TypeError / RecursionError out
    of local_chat instead of being skipped like a non-JSON line."""
    srv = RawServer([bad, json.dumps({"message": {"content": "ok"}}),
                     json.dumps({"done": True, "message": {"content": ""}})])
    try:
        monkeypatch.setenv("COUNCIL_OLLAMA_HOST", srv.url)
        _slots(eng, {"a": {"path": "ollama:llama3.1:8b"}}, {"coder": "a"})
        out = eng.ce.local_chat(MSGS, role="coder")
    finally:
        srv.close()
    assert out.endswith("ok")


def test_a_native_tool_call_with_non_dict_arguments_still_gives_a_dict(eng):
    _slots(eng, {"d": {"path": "ollama:llama3.1:8b"}}, {"docs": "d"})
    eng.fake.state.tool_calls = [
        {"function": {"name": "search_docs", "arguments": ["imread"]}},
        {"function": {"name": "read_page", "arguments": "[1, 2]"}},
        {"function": {"name": "read_page", "arguments": ""}}]
    calls = eng.ce.chat_tools(MSGS, DOC_TOOLS, role="docs")["tool_calls"]
    assert [type(c["arguments"]) for c in calls] == [dict, dict, dict]
    assert calls[2]["arguments"] == {}


# ============================================================
# Hostile replies through the JSON helpers
# ============================================================

def test_parse_json_survives_nesting_deeper_than_the_recursion_limit(
        default_recursion_limit):
    deep = "[" * 3000 + "]" * 3000
    value, why = so.parse_json(deep)
    assert value is None and why
    assert so.extract_json_text("note: " + deep) is None
    ok, errs = so.check_text(deep, {"type": "array"})
    assert ok is False and errs


def test_chat_tools_survives_a_deeply_nested_reply(eng,
                                                   default_recursion_limit):
    _slots(eng, {"d": {"path": "ollama:phi3.5"}}, {"docs": "d"})
    eng.fake.state.json_reply = "[" * 3000 + "]" * 3000
    out = eng.ce.chat_tools(MSGS, DOC_TOOLS, role="docs")
    assert out["tool_calls"] == []


def test_a_degenerate_brace_run_is_not_quadratic():
    """A small model stuck on one token: 20,000 '{' took ~25 s to scan."""
    t0 = time.perf_counter()
    assert so.extract_json_text("{" * 20000) is None
    assert so.extract_json_text("[" * 20000) is None
    took = time.perf_counter() - t0
    assert took < 1.0, f"{took:.2f}s"


def test_json_after_a_few_stray_braces_is_still_found():
    text = 'Use {braces} like {this}: {"a": {"b": [1, 2]}} done'
    assert json.loads(so.extract_json_text(text)) == {"a": {"b": [1, 2]}}


# ============================================================
# MCP tool schemas with $defs / $ref (pydantic, FastMCP)
# ============================================================

GET_DOC = {"name": "get_doc", "description": "",
           "inputSchema": {
               "type": "object",
               "$defs": {"Q": {"type": "object",
                               "properties": {"id": {"type": "string"}},
                               "required": ["id"]}},
               "properties": {"q": {"$ref": "#/$defs/Q"}},
               "required": ["q"]}}
SEARCH = {"name": "search", "description": "",
          "inputSchema": {
              "type": "object",
              "definitions": {"Q": {"type": "string", "maxLength": 50}},
              "properties": {"query": {"$ref": "#/definitions/Q"}},
              "required": ["query"]}}


def test_a_tool_schema_with_defs_refs_validates_a_good_call():
    schema = so.tool_choice_schema([GET_DOC, SEARCH])
    ok, errs = so.validate({"tool": "get_doc",
                            "arguments": {"q": {"id": "x"}}}, schema)
    assert ok, errs
    ok, errs = so.validate({"tool": "search",
                            "arguments": {"query": "imread"}}, schema)
    assert ok, errs
    # ...and still rejects a bad one, so the refs really resolve.
    ok, _ = so.validate({"tool": "get_doc", "arguments": {"q": {}}}, schema)
    assert not ok
    ok, _ = so.validate({"tool": "search", "arguments": {"query": 5}}, schema)
    assert not ok
    # The tools' own definitions with the same name do not collide.
    assert len(schema["$defs"]) == 2


def test_tool_choice_schema_does_not_change_the_callers_tools():
    before = json.dumps(GET_DOC, sort_keys=True)
    so.tool_choice_schema([GET_DOC])
    assert json.dumps(GET_DOC, sort_keys=True) == before


# ============================================================
# Origin of llama-architecture models (US-only recommendations)
# ============================================================

@pytest.mark.parametrize("name", ["yi:6b", "yi:34b", "yi-coder:9b",
                                  "solar:10.7b", "openchat:7b",
                                  "tinyllama:1.1b", "llama-pro:8b"])
def test_a_non_us_llama_architecture_model_is_not_us(name):
    """Ollama reports these with family "llama"; they came out
    ('Meta', 'US') and so could be suggested and auto-picked."""
    from council_core import local_models
    maker, origin = local_models.maker_and_origin(name, "llama", ["llama"])
    assert origin == "non-US", (name, maker)
    entry = {"id": f"ollama:{name}", "name": name, "origin": origin,
             "capabilities": ["completion"], "size_bytes": 4 * 2**30,
             "params_b": 7.0}
    assert local_models.rank_for_role([entry], "writer", vram_gb=8.0,
                                      ram_gb=32.0) == []


@pytest.mark.parametrize("name,family,maker", [
    ("llama3.1:8b", "llama", "Meta"), ("codellama:7b", "llama", "Meta"),
    ("phi4:14b", "phi3", "Microsoft"), ("gemma3:4b", "gemma3", "Google"),
    ("granite3.3:8b", "granite", "IBM"), ("gpt-oss:20b", "gptoss", "OpenAI")])
def test_us_models_stay_us(name, family, maker):
    from council_core import local_models
    assert local_models.maker_and_origin(name, family) == (maker, "US")


# ============================================================
# The stall timeout and a cold model load
# ============================================================

def test_a_cold_model_load_is_not_a_stall(eng):
    """Ollama sends NOTHING while it loads and prefills: measured here, a
    cold gpt-oss:20b gave no byte for 83 s, so local_chat(timeout=90) failed
    every first call (and llama3.1:8b's 45 s cold start met the 45 s
    timeouts of council_gui_engine's callers)."""
    _slots(eng, {"a": {"path": "ollama:llama3.1:8b"}}, {"coder": "a"})
    eng.fake.state.first_delay = 1.2               # "loading"
    out = eng.ce.local_chat(MSGS, role="coder", timeout=0.4)
    assert out == "Hello from the fake."
    assert len(eng.fake.state.chats) == 1


def test_a_stall_after_the_first_byte_still_times_out(eng):
    _slots(eng, {"a": {"path": "ollama:llama3.1:8b"}}, {"coder": "a"})
    eng.fake.state.reply = "abcd" * 3
    eng.fake.state.token_delay = 1.5               # stuck mid-answer
    t0 = time.perf_counter()
    with pytest.raises(TimeoutError) as info:
        eng.ce.local_chat(MSGS, role="coder", timeout=0.4)
    assert time.perf_counter() - t0 < 1.4
    assert info.value.partial == "abcd"
    assert len(eng.fake.state.chats) == 1          # never re-sent


def test_the_cold_start_allowance_is_bounded(eng, monkeypatch):
    _slots(eng, {"a": {"path": "ollama:llama3.1:8b"}}, {"coder": "a"})
    monkeypatch.setenv("COUNCIL_OLLAMA_LOAD_TIMEOUT", "0.6")
    eng.fake.state.first_delay = 3.0               # a hung server
    t0 = time.perf_counter()
    with pytest.raises(TimeoutError, match="did not start answering"):
        eng.ce.local_chat(MSGS, role="coder", timeout=0.2)
    assert 0.5 < time.perf_counter() - t0 < 1.5


def test_gguf_load_time_is_not_reported_as_waiting_for_the_model(
        tmp_path, monkeypatch):
    """Measured with phi3.5 in system Python: the first call's 4.2 s model
    load was reported as wait_s — 'another call held the model'."""
    import sys
    from types import SimpleNamespace
    import council_engine as ce
    from tests.test_llm_engine import StreamLlama
    vault = tmp_path / "vault"
    vault.mkdir()
    model = tmp_path / "m.gguf"
    model.write_bytes(b"x")
    monkeypatch.setenv("COUNCIL_VAULT_ROOT", str(vault))
    monkeypatch.setenv("COUNCIL_GGUF_PATH", str(model))

    def slow_load(p, **kw):
        time.sleep(0.4)
        return StreamLlama(p, 4096)

    monkeypatch.setitem(sys.modules, "llama_cpp", SimpleNamespace(Llama=None))
    monkeypatch.setattr(ce, "_load_gguf", slow_load)
    ce.refresh_backend_config()
    try:
        ce.local_chat(MSGS)
        st = ce.last_call_stats()
        assert st["backend"] == "gguf"
        assert st["load_s"] >= 0.35 and st["wait_s"] < 0.2, st
        ce.local_chat(MSGS)
        assert ce.last_call_stats()["load_s"] < 0.2       # already loaded
    finally:
        ce.refresh_backend_config()


# ============================================================
# The first chat_tools call of a session
# ============================================================

def test_the_first_chat_tools_call_on_the_fallback_is_native(eng,
                                                             monkeypatch):
    """This PC's default: no llama_cpp, no slot file -> the localhost Ollama
    answers with llama3.1:8b, which HAS native tools. The first call used to
    be emulated (the move to Ollama happened inside it) and the rest
    native."""
    monkeypatch.setenv("COUNCIL_OLLAMA_FALLBACK", "1")
    eng.fake.state.tool_calls = [{"function": {
        "name": "search_docs", "arguments": {"query": "imread"}}}]
    eng.fake.state.json_reply = '{"answer": "emulated"}'
    first = eng.ce.chat_tools(MSGS, DOC_TOOLS, role="docs")
    second = eng.ce.chat_tools(MSGS, DOC_TOOLS, role="docs")
    assert first == second
    assert ["tools" in c for c in eng.fake.state.chats] == [True, True]
    assert len(eng.fake.state.chats) == 2          # one request per call


def test_chat_tools_without_any_model_still_raises_not_implemented(
        eng, monkeypatch):
    monkeypatch.setenv("COUNCIL_OLLAMA_FALLBACK", "1")
    monkeypatch.setenv("COUNCIL_OLLAMA_HOST", "http://127.0.0.1:9")
    from council_core import local_models
    local_models.invalidate_cache()
    with pytest.raises(NotImplementedError):
        eng.ce.chat_tools(MSGS, DOC_TOOLS, role="docs")
