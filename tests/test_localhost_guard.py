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

No model is run and nothing leaves the PC: the hostile URLs are only ever
checked, never connected to; the [::1] test talks to tests/fake_ollama.
"""
from __future__ import annotations

import importlib.util
import socket
from pathlib import Path

import pytest

from council_core import local_models
from tests.fake_ollama import FakeOllama, refuse_egress
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
