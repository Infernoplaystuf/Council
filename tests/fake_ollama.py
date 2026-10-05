"""
tests.fake_ollama — an Ollama server on 127.0.0.1 that never runs a model.

The engine's Ollama backend is tested against THIS, not the real server: a
developer PC here has a real Ollama with real models, and a test must never
start a generation on it (and a CI box has none). It speaks the endpoints the
council uses — /api/version, /api/tags, /api/show, /api/chat (streamed and
not) — with scripted replies, and records every request so a test can assert
on exactly what the engine sent (options, format, tools, keep_alive).

Behaviours a test sets on ``server.state``:
  reply           text streamed back, a few characters per chunk
  json_reply      sent instead when the request carries a dict "format"
  tool_calls      native tool calls to return when the request has "tools"
  token_delay     seconds between chunks (cancellation tests)
  first_delay     seconds before the first byte (stall-timeout tests)
  reject_format   HTTP 400 for any request with a dict "format"
  no_tools        HTTP 400 "does not support tools" when "tools" is sent
  done_reason     the final packet's done_reason ("length" = cut off)
  reply_fn        callable(request body) -> text: the reply chosen per
                  request (wins over reply / json_reply when it returns a
                  str), for a test that answers several kinds of prompt
  vram_fraction   /api/ps reports size_vram = size * this for every model
                  a chat has loaded (1.0 = all on the GPU, 0 = CPU)
  chars_per_token None = every prompt counts 120 tokens; a number = the
                  prompt is counted at that ratio (+8 per message) and one
                  over num_ctx sent with truncate=false is REFUSED with
                  Ollama 0.35's 400 (counted in ``refused``)

/api/ps lists the models chats have "loaded"; /api/generate with keep_alive
0 "unloads" one (the benchmark's clean-placement step).
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any, Dict, List


def tag(name: str, *, size: int, family: str, params: str, quant: str,
        ctx: int = 131072, caps=("completion",)) -> Dict[str, Any]:
    """An /api/tags entry shaped like Ollama 0.35's (measured on this PC)."""
    return {"name": name, "model": name, "size": size,
            "digest": f"sha256:{abs(hash(name)):x}",
            "details": {"format": "gguf", "family": family,
                        "families": [family], "parameter_size": params,
                        "quantization_level": quant, "context_length": ctx},
            "capabilities": list(caps)}


DEFAULT_TAGS = [
    tag("llama3.1:8b", size=4_920_753_328, family="llama", params="8.0B",
        quant="Q4_K_M", caps=("completion", "tools")),
    tag("phi3.5:latest", size=2_176_178_843, family="phi3", params="3.8B",
        quant="Q4_0"),
    tag("qwen2.5:7b-instruct-q4_K_M", size=4_683_087_332, family="qwen2",
        params="7.6B", quant="Q4_K_M", ctx=32768,
        caps=("completion", "tools")),
]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"     # stream until the connection closes

    def log_message(self, *a):        # quiet
        pass

    @property
    def st(self) -> SimpleNamespace:
        return self.server.state       # type: ignore[attr-defined]

    def _json(self, code: int, obj: Any) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.st.requests.append(("GET", self.path, None))
        if self.path == "/api/version":
            self._json(200, {"version": "0.35.0-fake"})
        elif self.path == "/api/tags":
            self._json(200, {"models": self.st.tags})
        elif self.path == "/api/ps":
            models = []
            for t in self.st.tags:
                if t["name"] in self.st.loaded:
                    size = int(t.get("size") or 0)
                    models.append({"name": t["name"], "model": t["name"],
                                   "size": size,
                                   "size_vram": int(size
                                                    * self.st.vram_fraction),
                                   "details": t.get("details") or {}})
            self._json(200, {"models": models})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        self.st.requests.append(("POST", self.path, body))
        if self.path == "/api/show":
            self._json(200, {"model_info": {"general.architecture": "llama",
                                            "llama.context_length": 8192},
                             "capabilities": ["completion"]})
            return
        if self.path == "/api/generate" and \
                body.get("keep_alive") in (0, "0", "0s"):
            self.st.loaded.discard(body.get("model"))
            self.st.unloads.append(body.get("model"))
            self._json(200, {"model": body.get("model"), "done": True,
                             "done_reason": "unload", "response": ""})
            return
        if self.path != "/api/chat":
            self._json(404, {"error": "not found"})
            return
        self.st.chats.append(body)
        names = [t["name"] for t in self.st.tags]
        if body.get("model") not in names:
            self._json(404, {"error": f"model '{body.get('model')}' not "
                                     "found"})
            return
        self.st.loaded.add(body.get("model"))
        fmt = body.get("format")
        if isinstance(fmt, dict) and self.st.reject_format:
            self._json(400, {"error": "invalid format: unsupported schema"})
            return
        if body.get("tools") and self.st.no_tools:
            self._json(400, {"error": f"registry.ollama.ai/library/"
                                     f"{body['model']} does not support "
                                     "tools"})
            return
        text = self.st.reply
        if isinstance(fmt, dict) and self.st.json_reply is not None:
            text = self.st.json_reply
        elif fmt == "json" and self.st.json_reply is not None:
            text = self.st.json_reply
        if self.st.reply_fn is not None:
            try:
                got = self.st.reply_fn(body)
            except Exception as exc:                      # noqa: BLE001
                got = f"reply_fn raised {exc!r}"
            if isinstance(got, str):
                text = got
        calls = self.st.tool_calls if body.get("tools") else None
        prompt_tokens = 120
        if self.st.chars_per_token:
            chars = sum(len(m.get("content") or "")
                        for m in body.get("messages") or []
                        if isinstance(m.get("content"), str))
            prompt_tokens = (-(-chars // self.st.chars_per_token)
                             + 8 * len(body.get("messages") or []))
            prompt_tokens = int(prompt_tokens)
            n_ctx = int((body.get("options") or {}).get("num_ctx") or 2048)
            if prompt_tokens > n_ctx and body.get("truncate") is False:
                # Ollama 0.35's own refusal, byte for byte in shape.
                inner = {"error": {
                    "code": 400, "message": f"request ({prompt_tokens} "
                    f"tokens) exceeds the available context size ({n_ctx} "
                    "tokens), try increasing it",
                    "type": "exceed_context_size_error",
                    "n_prompt_tokens": prompt_tokens, "n_ctx": n_ctx}}
                self.st.refused += 1
                self._json(400, {"error": json.dumps(inner)})
                return
        final = {"model": body.get("model"), "done": True,
                 "done_reason": self.st.done_reason,
                 "prompt_eval_count": prompt_tokens,
                 "prompt_eval_duration": 60_000_000, "eval_count": 40,
                 "eval_duration": 1_000_000_000,
                 "load_duration": 5_000_000, "total_duration": 1_100_000_000}
        if not body.get("stream", True):
            msg = {"role": "assistant", "content": "" if calls else text}
            if calls:
                msg["tool_calls"] = calls
            self._json(200, dict(final, message=msg))
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        try:
            if self.st.first_delay:
                time.sleep(self.st.first_delay)
            if calls:
                self._line({"message": {"role": "assistant", "content": "",
                                        "tool_calls": calls},
                            "done": False})
            else:
                for i in range(0, len(text), 4):
                    self._line({"message": {"role": "assistant",
                                            "content": text[i:i + 4]},
                                "done": False})
                    if self.st.token_delay:
                        time.sleep(self.st.token_delay)
            self._line(dict(final, message={"role": "assistant",
                                            "content": ""}))
            self.st.completed += 1
        except (BrokenPipeError, ConnectionResetError,
                ConnectionAbortedError, OSError):
            self.st.disconnected += 1

    def _line(self, obj: Dict[str, Any]) -> None:
        self.wfile.write((json.dumps(obj) + "\n").encode("utf-8"))
        self.wfile.flush()


class FakeOllama:
    """``with FakeOllama() as srv: srv.url`` — a running fake server."""

    def __init__(self, tags: List[Dict[str, Any]] = None):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.daemon_threads = True
        self.state = SimpleNamespace(
            tags=list(tags if tags is not None else DEFAULT_TAGS),
            reply="Hello from the fake.", json_reply=None, tool_calls=None,
            token_delay=0.0, first_delay=0.0, reject_format=False,
            no_tools=False, done_reason="stop", requests=[], chats=[],
            completed=0, disconnected=0, reply_fn=None, vram_fraction=1.0,
            loaded=set(), unloads=[], chars_per_token=None, refused=0)
        self.httpd.state = self.state            # type: ignore[attr-defined]
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self._thread = threading.Thread(target=self.httpd.serve_forever,
                                        name="fake-ollama", daemon=True)

    def __enter__(self) -> "FakeOllama":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
