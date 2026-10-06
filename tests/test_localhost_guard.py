"""
The "this PC only" guard on model endpoints: council_engine._ensure_localhost,
council_core.local_models.is_loopback_url, and the callers that lean on them.

THE BUG (found 2026-10-05, measured): _ensure_localhost asked whether the URL
STRING STARTED with "http://localhost" or "http://127.0.0.1". That accepted

    http://localhost.evil.example        another machine, found through DNS
    http://127.0.0.1.evil.example        the same, spelled as an address
    http://localhost@evil.example        "localhost" is the USER NAME; the
                                         host is evil.example

and refused http://[::1]:11434 — this PC over IPv6. The council is offline by
design, so the first three are prompts leaving the PC without the user's
opt-in. The host is now matched whole.

A host that passes the guard must also be where the call GOES. urllib asks
for a proxy on every call (HTTP_PROXY, or the Windows system proxy, whose
"bypass for local addresses" exempts "localhost" but not "127.0.0.1"), so a
call the guard approved could still be handed, prompt and all, to a proxy on
another machine. Those calls now go through an opener that has no proxy and
follows no redirect.

No model is run and nothing leaves the PC: the hostile URLs are only ever
checked, never connected to; the [::1] test talks to tests/fake_ollama; the
"proxy" and the "elsewhere" a redirect points at are recording servers on
another loopback address.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import socket
import socketserver
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from council_core import local_models
from tests.fake_ollama import FakeOllama, loopback_alias, refuse_egress
from tests.test_llm_engine import MSGS, _slots, eng, fake  # noqa: F401

REPO = Path(__file__).resolve().parents[1]

#: Another machine dressed up as this one. None may pass without opt-in.
NOT_THIS_PC = [
    "http://localhost.evil.example",
    "http://localhost.evil.example:11434",
    "http://localhost-evil.example:11434",
    "http://localhostevil.example",
    "http://127.0.0.1.evil.example",
    "http://127.0.0.1.evil.example:11434/",
    "http://127.0.0.10.evil.example:11434",
    # userinfo: everything before "@" is a user name, the host is after it
    "http://localhost@evil.example",
    "http://localhost:11434@evil.example",
    "http://127.0.0.1:11434@evil.example:11434/api/chat",
    "http://user@localhost.evil.example:11434",
    # not an address at all: the OS would look "127.999.0.1" up BY NAME
    "http://127.999.0.1:11434",
    # plainly elsewhere
    "http://192.168.1.50:11434",
    "http://evil.example",
    "http://0.0.0.0:11434",
    "",
]

#: This PC, as a user or a launcher really writes it.
THIS_PC = [
    "http://127.0.0.1",
    "http://127.0.0.1:11434",
    "http://127.0.0.1:11434/",
    "http://127.0.0.2:11434",
    "http://localhost",
    "http://localhost:11434",
    "HTTP://LOCALHOST:11434/",
    "https://localhost:8443",
    "http://[::1]",
    "http://[::1]:11434",
    "http://[::1]:11434/",
]


@pytest.fixture(scope="module")
def ce():
    import council_engine
    return council_engine


# ============================================================
# The guard itself
# ============================================================

@pytest.mark.parametrize("url", NOT_THIS_PC)
def test_another_machine_is_refused_without_opt_in(ce, url):
    with pytest.raises(RuntimeError, match="Refusing non-local"):
        ce._ensure_localhost(url)


@pytest.mark.parametrize("url", THIS_PC)
def test_this_pc_is_accepted(ce, url):
    ce._ensure_localhost(url)                  # no raise


@pytest.mark.parametrize("url", ["http://192.168.1.50:11434",
                                 "http://pi-kitchen.lan:11434",
                                 "http://localhost.evil.example:11434"])
def test_a_remote_host_passes_only_when_the_caller_opted_in(ce, url):
    """allow_remote keeps its meaning: the caller (a node the user listed,
    with COUNCIL_REMOTE_NODES on) has opted in, so ANY host is let through —
    the guard is about the default, not about which remote is acceptable."""
    with pytest.raises(RuntimeError):
        ce._ensure_localhost(url)
    ce._ensure_localhost(url, allow_remote=True)


@pytest.mark.parametrize("url", NOT_THIS_PC)
def test_is_loopback_url_says_no_to_every_disguise(url):
    assert local_models.is_loopback_url(url) is False


@pytest.mark.parametrize("url", THIS_PC)
def test_is_loopback_url_says_yes_to_this_pc(url):
    assert local_models.is_loopback_url(url) is True


def test_the_two_guards_agree(ce):
    """_ensure_localhost and local_models.require_local are the same rule:
    the engine checks a host with one and then fetches /api/tags through the
    other, and a URL one passes and the other refuses fails half-way."""
    for url in NOT_THIS_PC + THIS_PC:
        def verdict(fn):
            try:
                fn(url)
                return True
            except RuntimeError:
                return False
        assert verdict(ce._ensure_localhost) == \
            verdict(local_models.require_local), url


# ============================================================
# Callers of the guard
# ============================================================

def test_the_benchmarks_ollama_backend_refuses_a_disguised_host():
    """llm_bench.OllamaBackend sends /api/chat to the host it is given, and
    _ensure_localhost was its only check — so --ollama-host
    http://localhost.evil.example:11434 sent the benchmark's prompts away."""
    from council_core import llm_bench
    with pytest.raises(RuntimeError, match="Refusing non-local"):
        llm_bench.OllamaBackend("llama3.1:8b",
                                host="http://localhost.evil.example:11434")
    with pytest.raises(RuntimeError, match="Refusing non-local"):
        llm_bench.OllamaBackend("llama3.1:8b",
                                host="http://localhost@evil.example:11434")
    # ...and IPv6 loopback is this PC (nothing is sent by the constructor).
    b = llm_bench.OllamaBackend("llama3.1:8b", host="http://[::1]:11434")
    assert b.host == "http://[::1]:11434"


def _ipv6_loopback_available() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(("::1", 0))
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _ipv6_loopback_available(),
                    reason="no IPv6 loopback on this machine")
def test_a_role_on_an_ipv6_loopback_ollama_is_served(eng, monkeypatch):
    """COUNCIL_OLLAMA_HOST=http://[::1]:<port> is this PC. The old prefix
    test refused it ("Refusing non-local model endpoint") before a byte was
    sent, while local_models (which matched the host whole) accepted it."""
    refused = refuse_egress(monkeypatch)
    with FakeOllama(host="::1") as v6:
        assert v6.url.startswith("http://[::1]:")
        monkeypatch.setenv("COUNCIL_OLLAMA_HOST", v6.url)
        local_models.invalidate_cache()
        _slots(eng, {"a": {"path": "ollama:llama3.1:8b"}}, {"coder": "a"})
        out = eng.ce.local_chat(MSGS, role="coder")
        assert out == "Hello from the fake."
        assert v6.state.chats and v6.state.chats[-1]["model"] == "llama3.1:8b"
    assert eng.fake.state.chats == []        # the IPv4 fake was not used
    assert refused == []


@pytest.mark.parametrize("url", ["http://localhost.evil.example:11434",
                                 "http://localhost@evil.example:11434",
                                 "http://127.0.0.1.evil.example:11434"])
def test_a_disguised_host_never_gets_a_chat_even_with_a_role_assigned(
        eng, monkeypatch, url):
    """With remote nodes OFF a role pointed at a disguised host is refused
    before anything is sent — no /api/tags, no /api/chat, not even a DNS
    query (refuse_egress fails the test at the first connection attempt)."""
    refused = refuse_egress(monkeypatch)
    monkeypatch.setenv("COUNCIL_OLLAMA_HOST", url)
    local_models.invalidate_cache()
    _slots(eng, {"a": {"path": "ollama:llama3.1:8b"}}, {"coder": "a"})
    with pytest.raises(RuntimeError, match="(?i)refusing non-local"):
        eng.ce.local_chat(MSGS, role="coder")
    assert refused == []


# ============================================================
# The speed bench tool had the same prefix test
# ============================================================

def _speed_bench():
    path = REPO / "tools" / "llm_speed_bench.py"
    spec = importlib.util.spec_from_file_location("llm_speed_bench_t", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                        # type: ignore
    return mod


@pytest.mark.parametrize("url", NOT_THIS_PC)
def test_the_speed_bench_refuses_a_disguised_host(monkeypatch, url):
    """tools/llm_speed_bench._post sends whole prompts (2k and 6k tokens of
    text) and checked the host with the same startswith test. A host that
    got past the check hits refuse_egress (AssertionError, no connection)
    instead of the SystemExit the tool raises for a refused host."""
    refused = refuse_egress(monkeypatch)
    bench = _speed_bench()
    with pytest.raises(SystemExit, match="refusing non-local"):
        bench._post("/api/generate", {"model": "m"}, url)
    assert refused == []


@pytest.mark.parametrize("url", ["http://127.0.0.1:11434",
                                 "http://localhost:11434",
                                 "http://[::1]:11434"])
def test_the_speed_bench_accepts_this_pc(url):
    assert _speed_bench()._is_loopback_url(url) is True


@pytest.mark.parametrize("url", NOT_THIS_PC)
def test_the_speed_bench_reads_nothing_from_a_disguised_host(monkeypatch,
                                                             url):
    """_get had no check at all, and run_ollama's FIRST request is a _get
    (/api/ps, through ollama_unload_all): a disguised host was looked up and
    connected to before _post ever refused it."""
    refused = refuse_egress(monkeypatch)
    bench = _speed_bench()
    with pytest.raises(SystemExit, match="refusing non-local"):
        bench._get("/api/ps", url)
    assert refused == []


def test_a_speed_bench_run_on_a_disguised_host_contacts_nothing(monkeypatch):
    refused = refuse_egress(monkeypatch)
    bench = _speed_bench()
    for url in ("http://localhost.evil.example:11434",
                "http://localhost@evil.example"):
        with pytest.raises(SystemExit, match="refusing non-local"):
            bench.run_ollama({"runtime": "ollama", "host": url, "model": "m"})
    assert refused == []


# ============================================================
# A proxy, or a redirect, must not carry a loopback call off the PC
# ============================================================

#: Planted in every chat these tests send; the proxy must never see it.
PROXY_SENTINEL = "SENTINEL-proxy-4b91-this-must-not-leave-the-pc"
_CHAT = [{"role": "user", "content": f"hello {PROXY_SENTINEL}"}]


class _Recording(BaseHTTPRequestHandler):
    """Records every request; answers ``server.status``. 502 is what an
    HTTP proxy says when it cannot reach ITS OWN 127.0.0.1; a 3xx sends the
    caller to ``server.location``."""
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def _answer(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        self.server.got.append((self.command, self.path, body))
        payload = json.dumps({"version": "0.35.0-elsewhere",
                              "models": []}).encode()
        self.send_response(self.server.status)
        if self.server.location:
            self.send_header("Location", self.server.location)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = _answer


class _RecordingServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # No reverse lookup of the address (fake_ollama._Server, same reason)
        socketserver.TCPServer.server_bind(self)
        self.server_name = str(self.server_address[0])
        self.server_port = self.server_address[1]


@contextlib.contextmanager
def _recording_server(host, status, location=None):
    srv = _RecordingServer((host, 0), _Recording)
    srv.status, srv.location, srv.got = status, location, []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield SimpleNamespace(url=f"http://{host}:{srv.server_address[1]}",
                              got=srv.got)
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def proxy(monkeypatch):
    """HTTP_PROXY pointing at a recorder on another loopback address — the
    stand-in for a corporate proxy off this PC. urllib's cached global
    opener is dropped first, so code that still asks urllib for proxies
    really reads this one (it is built on the first urlopen and kept). The
    Windows system proxy reaches urllib through the same ProxyHandler."""
    refused = refuse_egress(monkeypatch)
    with _recording_server(loopback_alias(), 502) as prx:
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                    "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("HTTP_PROXY", prx.url)
        monkeypatch.setattr(urllib.request, "_opener", None)
        local_models.invalidate_cache()
        yield prx
        local_models.invalidate_cache()
    assert refused == []


def _inferno_runner(url):
    from inferno_local.model_runner import OllamaRunner
    return OllamaRunner(url=url, model="llama3.1:8b", timeout_s=20)


def _bench_backend(url):
    from council_core import llm_bench
    return llm_bench.OllamaBackend("llama3.1:8b", host=url, timeout=20)


def _bench_engine_http(url, monkeypatch):
    from council_core import bench_engine
    monkeypatch.setenv("COUNCIL_OLLAMA_HOST", url)
    return bench_engine._http("/api/ps", timeout=10)


#: Every urllib call this app makes to an Ollama on this PC. Each gets the
#: fake's URL; the ones that send a chat carry PROXY_SENTINEL.
LOOPBACK_CALLS = {
    "local_models.ollama_reachable":
        lambda env, mp: local_models.ollama_reachable(env.fake.url),
    "local_models.ollama_tags":
        lambda env, mp: local_models.ollama_tags(env.fake.url),
    "local_models.ollama_show":
        lambda env, mp: local_models.ollama_show("llama3.1:8b", env.fake.url),
    "council_engine.local_chat":
        lambda env, mp: env.ce.local_chat(_CHAT, model="ollama:llama3.1:8b",
                                          host=env.fake.url),
    "council_engine.probe_node":
        lambda env, mp: env.ce.probe_node(env.fake.url).reachable or None,
    "council_engine._detect_ollama_models":
        lambda env, mp: env.ce._detect_ollama_models(env.fake.url) or None,
    "bench_engine._http":
        lambda env, mp: _bench_engine_http(env.fake.url, mp),
    "llm_bench.OllamaBackend":
        lambda env, mp: _bench_backend(env.fake.url)._chat(
            _CHAT, temperature=0.2, num_predict=8, role=None)[0],
    "llm_speed_bench._post":
        lambda env, mp: _speed_bench()._post(
            "/api/chat", {"model": "llama3.1:8b", "messages": _CHAT,
                          "stream": False}, env.fake.url, timeout=20),
    "llm_speed_bench._get":
        lambda env, mp: _speed_bench()._get("/api/ps", env.fake.url),
    "inferno OllamaRunner.chat":
        lambda env, mp: _inferno_runner(env.fake.url).chat(_CHAT),
    "inferno OllamaRunner.stream_chat":
        lambda env, mp: "".join(_inferno_runner(env.fake.url).stream_chat(
            _CHAT)),
}


@pytest.mark.parametrize("call", sorted(LOOPBACK_CALLS))
def test_a_proxy_never_carries_a_call_to_this_pcs_ollama(eng, proxy,
                                                         monkeypatch, call):
    """Measured before the fix: the proxy got the whole /api/chat body —
    the planted prompt — from llm_bench, the speed bench and inferno's
    runner, and /api/version, /api/tags, /api/show, /api/ps from the rest;
    the fake got nothing, so local_chat even said "No Ollama server answers"
    with the server up."""
    out = LOOPBACK_CALLS[call](eng, monkeypatch)
    assert proxy.got == [], f"the proxy got {proxy.got}"
    assert eng.fake.state.requests, "the call never reached this PC's Ollama"
    assert out not in (None, False, "", {}), out
    for body in eng.fake.state.chats:
        assert PROXY_SENTINEL in json.dumps(body)


def test_the_chat_stream_never_asked_for_a_proxy(eng, proxy):
    """The control: council_engine's own /api/chat stream is http.client,
    which knows nothing of proxies — it was never the leak."""
    out = eng.ce._ollama_chat(eng.fake.url, "llama3.1:8b", _CHAT,
                              temperature=0.2, num_predict=8)
    assert out == "Hello from the fake."
    assert proxy.got == []


def test_a_redirect_from_a_loopback_server_is_not_followed(monkeypatch):
    """Whatever answers on a loopback port is not necessarily Ollama; a 3xx
    pointing elsewhere was followed — the same request, re-sent to the
    machine the redirect named. A model server never redirects, so a
    redirect is an error here, not a hop."""
    refused = refuse_egress(monkeypatch)
    with _recording_server(loopback_alias(), 200) as elsewhere, \
            _recording_server("127.0.0.1", 302,
                              location=elsewhere.url + "/api/version") as rd:
        local_models.invalidate_cache()
        assert local_models.ollama_reachable(rd.url) is False
        assert local_models.ollama_tags(rd.url) is None
        with pytest.raises(Exception):
            _speed_bench()._get("/api/ps", rd.url)
        assert rd.got, "the loopback server was never asked"
        assert elsewhere.got == [], "the redirect was followed"
    local_models.invalidate_cache()
    assert refused == []
