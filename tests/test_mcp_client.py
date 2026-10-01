"""
council_core.mcp_client — the protocol, against a scripted server.

Every test talks to a REAL server process (stdio) or a real HTTP server on
127.0.0.1, both driven by tests/data/mcp_fixture_server.py, so framing,
threading and process lifetime are exercised, not mocked. Nothing here leaves
this machine: the only non-loopback URL is refused before a socket opens.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from council_core import mcp_client as mc  # noqa: E402

FIXTURE = ROOT / "tests" / "data" / "mcp_fixture_server.py"

_spec = importlib.util.spec_from_file_location("mcp_fixture_server", FIXTURE)
fixture_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture_mod)


def stdio(*flags, timeout=10.0) -> mc.McpClient:
    return mc.McpClient.stdio(sys.executable, [str(FIXTURE), *flags],
                              timeout=timeout)


@pytest.fixture
def client():
    c = stdio().connect()
    yield c
    c.close()


# ============================================================ handshake

def test_the_handshake_negotiates_the_newest_version(client):
    assert client.protocol_version == mc.SUPPORTED_PROTOCOLS[0]
    assert client.server_info["name"] == "fixture"
    assert client.has_tools and client.has_resources
    assert client.connected


def test_an_older_server_version_is_accepted():
    with stdio("--version", "2024-11-05").connect() as c:
        assert c.protocol_version == "2024-11-05"
        assert [t["name"] for t in c.list_tools()][:2] == ["search_docs",
                                                          "get_doc"]


def test_an_unknown_version_is_refused_and_the_process_released():
    c = stdio("--version", "1999-01-01")
    with pytest.raises(mc.McpError) as err:
        c.connect()
    assert "1999-01-01" in str(err.value)
    proc = c.transport.proc
    proc.wait(timeout=5)
    assert proc.poll() is not None, "a refused server was left running"


# ============================================================ lists

def test_tools_list_follows_every_page():
    with stdio("--page-size", "3").connect() as c:
        names = [t["name"] for t in c.list_tools()]
    assert names == [t["name"] for t in fixture_mod.TOOLS]


def test_resources_paginate_and_read():
    with stdio("--page-size", "2").connect() as c:
        res = c.list_resources()
        assert [r["uri"] for r in res] == [f"fx://page/{i}"
                                          for i in range(5)]
        contents = c.read_resource("fx://page/3")
        assert mc.content_text(contents) == "text of fx://page/3"
        with pytest.raises(mc.McpError) as err:
            c.read_resource("nope://x")
        assert err.value.code == -32002


def test_a_repeated_cursor_is_called_a_loop_not_followed_forever(client,
                                                                 monkeypatch):
    real = client.request

    def looping(method, params=None, **kw):
        if method == "tools/list":
            return {"tools": [{"name": "x"}], "nextCursor": "same"}
        return real(method, params, **kw)

    monkeypatch.setattr(client, "request", looping)
    with pytest.raises(mc.McpError) as err:
        client.list_tools()
    assert "repeated a page cursor" in str(err.value)


# ============================================================ tools

def test_a_tool_failure_is_a_result_not_an_exception(client):
    r = client.call_tool("fail")
    assert r.is_error
    assert "failed on purpose" in r.text


def test_a_protocol_error_reads_as_a_sentence_with_its_code(client):
    with pytest.raises(mc.McpError) as err:
        client.call_tool("boom")
    assert "the tool exploded" in str(err.value)
    assert err.value.code == -32603
    assert not isinstance(err.value, mc.McpConnectionError)


def test_an_unknown_tool_is_invalid_params(client):
    with pytest.raises(mc.McpError) as err:
        client.call_tool("no_such_tool")
    assert err.value.code == -32602


def test_concurrent_requests_get_their_own_answers(client):
    out = {}

    def call(i):
        out[i] = client.call_tool("echo", {"i": i}).text

    threads = [threading.Thread(target=call, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert out == {i: json.dumps({"i": i}) for i in range(8)}


def test_the_server_can_ping_the_client(client):
    assert client.call_tool("ping_me").text == "pong ok"


# ============================================================ time

def test_a_timeout_raises_and_cancels_the_request(client):
    t0 = time.monotonic()
    with pytest.raises(mc.McpTimeout) as err:
        client.call_tool("slow", {"seconds": 5}, timeout=0.4)
    assert time.monotonic() - t0 < 1.5
    assert "0.4 s" in str(err.value)
    # The server heard notifications/cancelled for that request id.
    seen = json.loads(client.call_tool("cancellations").text)
    assert seen, "no notifications/cancelled reached the server"
    assert client.connected, "a timeout must not kill the session"


def test_stop_cancels_within_one_poll(client):
    flag = threading.Event()
    threading.Timer(0.2, flag.set).start()
    t0 = time.monotonic()
    with pytest.raises(mc.McpCancelled):
        client.call_tool("slow", {"seconds": 5}, should_stop=flag.is_set)
    assert time.monotonic() - t0 < 1.0
    seen = json.loads(client.call_tool("cancellations").text)
    assert len(seen) == 1


# ============================================================ failure

def test_a_crash_at_startup_names_its_cause():
    c = stdio("--exit-on-init", "3")
    with pytest.raises(mc.McpConnectionError) as err:
        c.connect()
    text = str(err.value)
    assert "code 3" in text, text
    assert "refusing to start" in text, text


def test_a_missing_command_is_a_sentence():
    c = mc.McpClient.stdio("definitely-not-a-command-4815", [])
    with pytest.raises(mc.McpConnectionError) as err:
        c.connect()
    assert "was not found" in str(err.value)


def test_junk_on_stdout_is_skipped_not_fatal():
    with stdio("--noise").connect() as c:
        assert c.call_tool("echo", {"a": 1}).text == '{"a": 1}'
        assert any("Welcome" in n for n in c.transport.noise)


def test_close_releases_the_process_and_later_calls_say_so(client):
    proc = client.transport.proc
    t0 = time.monotonic()
    client.close()
    assert time.monotonic() - t0 < 3.5
    assert proc.poll() is not None
    with pytest.raises(mc.McpConnectionError):
        client.call_tool("echo")


# ============================================================ HTTP

class HttpFixture:
    """The same dispatch behind Streamable HTTP, on 127.0.0.1:<free port>."""

    def __init__(self, mode: str = "json"):
        self.fx = fixture_mod.Fixture()
        self.mode = mode
        self.session_id = "sess-123"
        self.seen = []                     # (method, session, version)
        self.deleted = False
        self.expire = False
        self.client_replies = {}
        self.cond = threading.Condition()
        fx_owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_DELETE(self):
                fx_owner.deleted = True
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                msg = json.loads(body)
                sid = self.headers.get("Mcp-Session-Id")
                fx_owner.seen.append((msg.get("method"), sid,
                                      self.headers.get(
                                          "MCP-Protocol-Version")))
                if msg.get("method") != "initialize" and (
                        fx_owner.expire or sid != fx_owner.session_id):
                    self._empty(404)
                    return
                if "method" not in msg:
                    with fx_owner.cond:
                        fx_owner.client_replies[msg.get("id")] = msg
                        fx_owner.cond.notify_all()
                    self._empty(202)
                    return
                if "id" not in msg:
                    fx_owner.fx.note(msg)
                    self._empty(202)
                    return
                if fx_owner.mode == "sse":
                    self._sse(msg)
                else:
                    reply = fx_owner.fx.handle(msg)
                    data = json.dumps(reply).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    if msg.get("method") == "initialize":
                        self.send_header("Mcp-Session-Id",
                                         fx_owner.session_id)
                    self.end_headers()
                    self.wfile.write(data)

            def _empty(self, code):
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def _event(self, obj):
                # Indented JSON, one data: line per line, on purpose: the SSE
                # format joins data lines with a newline, which JSON allows
                # between tokens — a client that parsed only the first line
                # would fail here.
                lines = json.dumps(obj, indent=1).split("\n")
                self.wfile.write(("event: message\n" + "".join(
                    f"data: {line}\n" for line in lines) + "\n").encode())
                self.wfile.flush()

            def _sse(self, msg):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                if msg.get("method") == "initialize":
                    self.send_header("Mcp-Session-Id", fx_owner.session_id)
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b": keep-alive comment\n\n")
                self._event({"jsonrpc": "2.0",
                             "method": "notifications/message",
                             "params": {"level": "info",
                                        "data": "working on it"}})

                def ask_client(request):
                    self._event(request)
                    deadline = time.monotonic() + 3
                    with fx_owner.cond:
                        while request["id"] not in fx_owner.client_replies:
                            left = deadline - time.monotonic()
                            if left <= 0:
                                return None
                            fx_owner.cond.wait(left)
                        return fx_owner.client_replies.pop(request["id"])

                fx_owner.fx.ask_client = ask_client
                try:
                    self._event(fx_owner.fx.handle(msg))
                except OSError:
                    pass
                self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def http_json():
    f = HttpFixture("json")
    yield f
    f.close()


@pytest.fixture
def http_sse():
    f = HttpFixture("sse")
    yield f
    f.close()


def test_http_carries_the_session_and_version_headers(http_json):
    c = mc.McpClient.http(http_json.url, timeout=5).connect()
    tools = c.list_tools()
    assert len(tools) == len(fixture_mod.TOOLS)
    c.close()
    init = http_json.seen[0]
    assert init[0] == "initialize" and init[1] is None
    later = [s for s in http_json.seen[1:]]
    assert later and all(sid == "sess-123" for _m, sid, _v in later)
    assert all(v == mc.SUPPORTED_PROTOCOLS[0] for _m, _s, v in later)
    assert ("notifications/initialized", "sess-123",
            mc.SUPPORTED_PROTOCOLS[0]) in later
    assert http_json.deleted, "closing did not end the session"


def test_http_event_stream_answers_and_server_requests(http_sse):
    with mc.McpClient.http(http_sse.url, timeout=5).connect() as c:
        assert c.call_tool("echo", {"x": 1}).text == '{"x": 1}'
        assert c.call_tool("ping_me").text == "pong ok"
        assert any(entry.get("data") == "working on it" for entry in c.log)


def test_http_timeout_cancels_on_the_server(http_json):
    with mc.McpClient.http(http_json.url, timeout=5).connect() as c:
        t0 = time.monotonic()
        with pytest.raises(mc.McpTimeout):
            c.call_tool("slow", {"seconds": 5}, timeout=0.4)
        assert time.monotonic() - t0 < 1.5
        # The cancel is posted off the caller's thread (a wedged server must
        # not hold Stop up), so it may land a moment after the timeout.
        deadline = time.monotonic() + 3
        seen = []
        while not seen and time.monotonic() < deadline:
            seen = json.loads(c.call_tool("cancellations").text)
            time.sleep(0.05)
        assert seen, "the cancel notification never reached the server"


def test_http_session_expiry_is_explained(http_json):
    with mc.McpClient.http(http_json.url, timeout=5).connect() as c:
        http_json.expire = True
        with pytest.raises(mc.McpConnectionError) as err:
            c.list_tools()
        assert "forgot this session" in str(err.value)


class RawServer:
    """An HTTP server written on raw sockets, so the test controls the
    framing byte for byte: "mangled" is what this PC delivers for POST /mcp
    (a chunked header on a plain body), "chunked" is real chunking. Served
    at /x, a path nothing on the machine rewrites."""

    def __init__(self, framing: str):
        import socket
        self.fx = fixture_mod.Fixture()
        self.framing = framing
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}/x"
        self.alive = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while self.alive:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(conn,),
                             daemon=True).start()

    def _one(self, conn):
        data = b""
        while b"\r\n\r\n" not in data:
            data += conn.recv(4096)
        head, _, body = data.partition(b"\r\n\r\n")
        length = int(next(line.split(b":")[1] for line in head.split(b"\r\n")
                          if line.lower().startswith(b"content-length")))
        while len(body) < length:
            body += conn.recv(4096)
        reply = self.fx.handle(json.loads(body))
        if reply is None:
            payload, status = b"", b"202 Accepted"
        else:
            payload, status = json.dumps(reply).encode(), b"200 OK"
        start = (b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json"
                 b"\r\nConnection: close\r\n")
        if self.framing == "mangled":
            conn.sendall(start + b"transfer-encoding: chunked\r\n\r\n"
                         + payload)
        else:
            half = len(payload) // 2
            chunks = b"".join(b"%x\r\n%s\r\n" % (len(p), p)
                              for p in (payload[:half], payload[half:]) if p)
            conn.sendall(start + b"Transfer-Encoding: chunked\r\n\r\n"
                         + chunks + b"0\r\n\r\n")
        conn.close()

    def close(self):
        self.alive = False
        self.sock.close()


@pytest.mark.parametrize("framing", ["mangled", "chunked"])
def test_both_framings_of_a_chunked_reply_are_read(framing):
    """The mangled one is what this PC delivers for every POST /mcp; the
    real one is what Go and uvicorn send for large bodies."""
    server = RawServer(framing)
    try:
        with mc.McpClient.http(server.url, timeout=5).connect() as c:
            assert len(c.list_tools()) == len(fixture_mod.TOOLS)
            assert c.call_tool("echo", {"k": "v"}).text == '{"k": "v"}'
    finally:
        server.close()


@pytest.mark.parametrize("head,undone", [
    (b"{\"jsonrpc\"", True), (b"data: {}", True), (b"event: message", True),
    (b": comment", True), (b"", True),
    (b"1a\r\n{", False), (b"1A;ext=1\r\n", False), (b"ff", False)])
def test_what_counts_as_a_mangled_chunked_body(head, undone):
    class Fp:
        def peek(self, n):
            return head

    class Resp:
        chunked = True
        length = None
        will_close = False
        fp = Fp()

    r = Resp()
    assert mc.unmangle(r) is undone
    assert r.chunked is (not undone)


def test_http_nothing_listening_is_a_sentence():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    c = mc.McpClient.http(f"http://127.0.0.1:{port}/mcp", timeout=3)
    with pytest.raises(mc.McpConnectionError) as err:
        c.connect()
    assert "Nothing is listening" in str(err.value)


def test_a_remote_url_is_refused_unless_allowed():
    with pytest.raises(mc.McpRemoteRefused) as err:
        mc.McpClient.http("http://docs.example.com/mcp")
    assert "Allow remote" in str(err.value)
    # Allowed: constructs (no connection is attempted here).
    mc.McpClient.http("http://docs.example.com/mcp", allow_remote=True)


@pytest.mark.parametrize("host,local", [
    ("localhost", True), ("127.0.0.1", True), ("127.9.9.9", True),
    ("::1", True), ("[::1]", True), ("docs.localhost", True),
    ("10.0.0.5", False), ("0.0.0.0", False), ("example.com", False),
    ("localhost.example.com", False), ("", False)])
def test_what_counts_as_this_computer(host, local):
    assert mc.is_local_host(host) is local


def test_content_text_reads_every_kind():
    items = [{"type": "text", "text": "a"},
             {"type": "resource", "resource": {"uri": "x://1", "text": "b"}},
             {"type": "resource_link", "uri": "x://2", "name": "c"},
             {"type": "image", "data": "...."}]
    assert mc.content_text(items) == "a\nb\nc <x://2>"


def test_the_client_and_server_import_only_the_standard_library():
    """No SDK, no toolkit, no HTTP library: read from the imports, not the
    prose (which talks about requests all the time)."""
    import ast
    allowed = set(sys.stdlib_module_names) | {"__future__"}
    for path in (ROOT / "council_core" / "mcp_client.py",
                 ROOT / "tools" / "pydocs_mcp_server.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and not node.level:
                roots.add((node.module or "").split(".")[0])
        assert roots <= allowed, (path.name, sorted(roots - allowed))
