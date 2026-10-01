"""
council_core.mcp_client — talk to a Model Context Protocol server, stdlib only.

WHY THERE IS NO SDK HERE
The official `mcp` package is not installed in the council env and must not be:
it brings anyio, pydantic, httpx, starlette and an asyncio runtime into a
desktop app whose model calls are plain blocking functions on worker threads.
What the Council needs from MCP is small — initialize, tools/list, tools/call,
resources/list, resources/read — and all of it is JSON-RPC 2.0 over either a
pipe or an HTTP POST. That fits in one module the tests can drive end to end.

TWO TRANSPORTS (MCP spec, "Transports")
  * stdio — the client starts the server as a subprocess; one JSON-RPC message
    per line on its stdin/stdout, logs on stderr. The usual way a docs server
    runs, and the one the bundled tools/pydocs_mcp_server.py speaks.
  * Streamable HTTP — every message is a POST to one endpoint; the reply is
    either a JSON body or a text/event-stream that carries the response (and
    possibly the server's own requests first). A session id handed out at
    initialize ("Mcp-Session-Id") goes back on every later request, and the
    negotiated version goes in "MCP-Protocol-Version".

LOCAL BY DEFAULT
The Council is an offline app. An HTTP server on another machine is refused
unless the user enabled "allow remote" FOR THAT SERVER — the check is here, in
the transport, so no caller can forget it. Loopback means 127.0.0.0/8, ::1 and
localhost; nothing is resolved through DNS to decide it.

TIMEOUTS ARE DEADLINES, AND STOP IS STOP
Every request has a deadline and an optional `should_stop()`; both are polled
every 50 ms while waiting. Either one sends `notifications/cancelled` to the
server, as the spec asks, and raises — McpTimeout or McpCancelled — so a
wedged server costs the user the timeout, never the app. On HTTP the open
connection is also shut, which is the only way to unblock a read.

ERRORS ARE SENTENCES
"Errno 2" and "JSONDecodeError at char 0" are what a raw client gives a user.
These say what happened in the user's terms: the command was not found; the
server exited with code 1 and its last line was X; nothing is listening at
URL; the server took longer than 30 s. The server's own stderr tail is kept so
a crash at start-up names its cause.
"""
from __future__ import annotations

import collections
import http.client
import ipaddress
import itertools
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence
from urllib.parse import urlsplit

#: Newest first. The client asks for the first; a server that does not know it
#: answers with one it does (spec, "Version Negotiation"), and anything in this
#: list is accepted. The parts used here — tools, resources, pagination — are
#: the same in all four.
SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

CLIENT_NAME = "council"
CLIENT_VERSION = "1.0"

#: How long a request may take unless the caller says otherwise. Generous on
#: purpose: the first search of a big package (numpy) indexes it.
DEFAULT_TIMEOUT = 30.0

#: The polling slice while waiting. Short enough that Stop feels immediate,
#: long enough to cost nothing.
_POLL = 0.05

#: The most pages a paginated list may have before it is called a loop.
MAX_PAGES = 200

#: The largest single message accepted from a server. MEASURED: one 200 MB
#: search reply cost 97 s and +4.9 GB of the app's memory before docs_qa
#: trimmed it to 6000 chars. A documentation page or a tool list is
#: kilobytes; a server sending more than this is broken, and the request (on
#: stdio, the connection) fails with that reason instead of being read to
#: the end.
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
#: How much of a body that is thrown away anyway (a 202, a 404) is read.
#: Every request carries Connection: close, so the rest never matters.
_DISCARD = 65536

# JSON-RPC error codes this module produces or recognises.
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND = -32700, -32600, -32601
INVALID_PARAMS, INTERNAL_ERROR = -32602, -32603
#: Not JSON-RPC: marks an error this module synthesised from a transport fault,
#: so `request` raises McpConnectionError rather than blaming the server.
_TRANSPORT_FAULT = "_council_transport"


class McpError(RuntimeError):
    """A request failed. `code` is the JSON-RPC code when the server sent one."""

    def __init__(self, message: str, *, code: Optional[int] = None,
                 data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


class McpTimeout(McpError):
    """No answer before the deadline. The request was cancelled."""


class McpCancelled(McpError):
    """The caller's should_stop() said stop. The request was cancelled."""


class McpConnectionError(McpError):
    """The server could not be reached, or stopped answering for good."""


class McpRemoteRefused(McpError):
    """A URL off this computer, for a server not allowed to be remote."""


def is_local_host(host: str) -> bool:
    """Loopback only, decided without DNS.

    `localhost` and `*.localhost` count (RFC 6761 reserves them for loopback);
    an IP literal counts when it is a loopback address. Anything else — a LAN
    name, a public IP, 0.0.0.0 — is remote."""
    h = (host or "").strip().strip("[]").lower()
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _short(text: Any, limit: int = 300) -> str:
    s = str(text).strip()
    return s if len(s) <= limit else s[:limit - 1] + "…"


def _object(value: Any) -> Dict[str, Any]:
    """`value` when it is a JSON object, else {} — servers send garbage."""
    return dict(value) if isinstance(value, dict) else {}


def _objects(value: Any) -> List[dict]:
    """The objects in `value` when it is a list (a number or a string where
    a list belongs raised a TypeError no caller expects)."""
    if not isinstance(value, list):
        return []
    return [x for x in value if isinstance(x, dict)]


def _too_large() -> str:
    return (f"The server sent a message larger than "
            f"{MAX_MESSAGE_BYTES // 2 ** 20} MB; it was not read.")


# ======================================================================
# Results
# ======================================================================

@dataclass
class ToolResult:
    """What tools/call returned. `structured` is structuredContent (2025-06+)."""
    content: List[dict] = field(default_factory=list)
    structured: Optional[dict] = None
    is_error: bool = False

    @property
    def text(self) -> str:
        """Every text the result carries, in order, as one string.

        Embedded resources contribute their text and resource links their name
        and URI, so a caller reading `.text` sees everything a person would."""
        return content_text(self.content)


def content_text(items: Sequence[Any]) -> str:
    out: List[str] = []
    for item in items or ():
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            out.append(str(item.get("text", "")))
        elif kind == "resource":
            res = item.get("resource")
            if isinstance(res, dict) and "text" in res:
                out.append(str(res.get("text", "")))
        elif kind == "resource_link":
            out.append(f"{item.get('name') or item.get('title') or ''} "
                       f"<{item.get('uri', '')}>".strip())
        elif "text" in item:                    # resources/read contents
            out.append(str(item.get("text", "")))
    return "\n".join(t for t in out if t)


@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    message: Optional[dict] = None


# ======================================================================
# Transports
# ======================================================================

class _Transport:
    """Moves JSON-RPC messages. Knows nothing about MCP methods."""

    kind = ""

    def __init__(self) -> None:
        self.on_message: Callable[[dict], None] = lambda message: None
        self.on_closed: Callable[[str], None] = lambda reason: None
        #: Set by the client after initialize; HTTP sends it as a header.
        self.protocol_version: Optional[str] = None

    def start(self) -> None:
        """Make the connection usable. Raises McpConnectionError."""

    def send(self, message: dict) -> None:
        raise NotImplementedError

    def abort(self, request_id: Any) -> None:
        """Give up on one request's I/O (HTTP closes its connection)."""

    def close(self, timeout: float = 3.0) -> None:
        """Shut down. Never raises."""

    def diagnostics(self) -> str:
        """Anything the server said on the side (stderr), for error text."""
        return ""

    def describe(self) -> str:
        return self.kind


class StdioTransport(_Transport):
    """A server subprocess; newline-delimited JSON on its stdin and stdout.

    Two daemon threads read stdout (messages) and stderr (kept as a ring of the
    last lines, for error messages); a third is the only writer, so a worker
    sending a request and the reader answering a server ping never collide,
    and neither ever waits on a server that stopped reading (see send).
    """

    kind = "stdio"

    def __init__(self, command: str, args: Sequence[str] = (), *,
                 env: Optional[Dict[str, str]] = None,
                 cwd: Optional[str] = None) -> None:
        super().__init__()
        self.command = str(command)
        self.args = [str(a) for a in args]
        self.env = {str(k): str(v) for k, v in (env or {}).items()}
        self.cwd = cwd or None
        self.proc: Optional[subprocess.Popen] = None
        #: Bytes waiting for the writer thread; None ends it (see send).
        self._outbox: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._write_failed = ""
        self._stderr: Deque[str] = collections.deque(maxlen=40)
        #: Lines on stdout that were not JSON — a banner, a stray print. Kept
        #: because "the server printed X instead of answering" is the most
        #: common reason a hand-written server fails to initialize.
        self.noise: Deque[str] = collections.deque(maxlen=10)
        self._threads: List[threading.Thread] = []
        self._closing = False

    def describe(self) -> str:
        return " ".join([self.command] + self.args)

    def start(self) -> None:
        argv = [self.command] + self.args
        env = dict(os.environ)
        env.update(self.env)
        # A Python server with a block-buffered stdout never answers; a
        # Windows console code page mangles non-ASCII docs. Neither variable
        # matters to a server in any other language.
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        try:
            try:
                self.proc = self._popen(argv, env)
            except FileNotFoundError:
                # Windows: CreateProcess finds `name.exe` but not
                # `name.cmd`, and npx — how most published MCP servers
                # start — is npx.cmd. shutil.which honours PATHEXT. Only
                # tried after the plain start failed, so a command that
                # worked before ("python" = the app's own) is unchanged.
                found = (shutil.which(self.command, path=env.get("PATH"))
                         if not os.path.dirname(self.command) else None)
                if not found:
                    raise
                self.proc = self._popen([found] + self.args, env)
        except FileNotFoundError:
            raise McpConnectionError(
                f"Could not start the server: the command {self.command!r} "
                f"was not found. Check the command, or give its full path."
            ) from None
        except OSError as exc:
            raise McpConnectionError(
                f"Could not start the server ({self.command!r}): "
                f"{exc.strerror or exc}") from None
        for target, name in ((self._read_stdout, "mcp-stdio-out"),
                             (self._read_stderr, "mcp-stdio-err"),
                             (self._write_stdin, "mcp-stdio-in")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def _popen(self, argv: List[str], env: Dict[str, str]
               ) -> subprocess.Popen:
        return subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, cwd=self.cwd, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def _read_stdout(self) -> None:
        proc = self.proc
        assert proc is not None and proc.stdout is not None
        reason = ""
        try:
            while True:
                # Bounded: an endless line is a broken server, not a reason
                # to hold gigabytes (see MAX_MESSAGE_BYTES).
                raw = proc.stdout.readline(MAX_MESSAGE_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_MESSAGE_BYTES and not raw.endswith(b"\n"):
                    reason = (f"the server sent a message larger than "
                              f"{MAX_MESSAGE_BYTES // 2 ** 20} MB, so the "
                              f"connection was closed.")
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line.decode("utf-8", "replace"))
                except (ValueError, RecursionError):
                    self.noise.append(_short(line.decode("utf-8", "replace"),
                                             200))
                    continue
                for one in (message if isinstance(message, list)
                            else [message]):
                    if isinstance(one, dict):
                        self._deliver(one)
        except (OSError, ValueError):
            pass
        if reason:
            try:
                proc.kill()
            except OSError:
                pass
        self._outbox.put(None)          # the writer has no one to write to
        self.on_closed(reason or self._exit_reason(wait=1.0))

    def _deliver(self, message: dict) -> None:
        """Hand one message on. A malformed one (an id that is a list, a
        notification whose params are a string) used to end this thread —
        and with it every later answer, while the client still said
        "connected" — so a fault here costs that message, nothing more."""
        try:
            self.on_message(message)
        except Exception as exc:                          # noqa: BLE001
            self.noise.append(_short(f"unreadable message ({exc}): "
                                     f"{message}", 200))

    def _read_stderr(self) -> None:
        proc = self.proc
        assert proc is not None and proc.stderr is not None
        try:
            for raw in iter(lambda: proc.stderr.readline(65536), b""):
                text = raw.decode("utf-8", "replace").rstrip()
                if text:
                    self._stderr.append(text)
        except (OSError, ValueError):
            pass

    def _exit_reason(self, wait: float = 0.0) -> str:
        proc = self.proc
        code = None
        if proc is not None:
            try:
                code = proc.wait(timeout=wait) if wait else proc.poll()
            except subprocess.TimeoutExpired:
                code = proc.poll()
        if self._closing:
            return "the connection was closed"
        what = ("the server closed its output" if code is None
                else f"the server exited with code {code}")
        tail = self.diagnostics()
        return f"{what}. Its last words: {tail}" if tail else what + "."

    def diagnostics(self) -> str:
        lines = list(self._stderr)[-4:]
        if not lines and self.noise:
            lines = ["(stdout) " + n for n in list(self.noise)[-2:]]
        return _short(" | ".join(lines), 400)

    def send(self, message: dict) -> None:
        """Queue one message for the writer thread; never blocks.

        A pipe write blocks once the server stops reading and its buffer is
        full. MEASURED: a 100 KB request to a server that had stopped reading
        hung its caller for good — the deadline and Stop are polled only
        AFTER send returns. So the caller only queues; a write that fails
        closes the connection, which fails every request still waiting."""
        proc = self.proc
        if proc is None or proc.stdin is None:
            raise McpConnectionError("The server has not been started.")
        if self._write_failed or self._closing or proc.poll() is not None:
            raise McpConnectionError(
                "The server stopped: " + (self._write_failed
                                          or self._exit_reason(wait=0.5)))
        self._outbox.put((json.dumps(message, ensure_ascii=False,
                                     separators=(",", ":")) + "\n"
                          ).encode("utf-8"))

    def _write_stdin(self) -> None:
        """The only writer of the server's stdin, so messages never
        interleave. None in the outbox is close(): stdin is closed after
        everything queued before it, which is the spec's polite shutdown."""
        proc = self.proc
        assert proc is not None and proc.stdin is not None
        while True:
            data = self._outbox.get()
            try:
                if data is None:
                    proc.stdin.close()
                    return
                proc.stdin.write(data)
                proc.stdin.flush()
            except (OSError, ValueError):
                if data is None or self._closing:
                    return
                self._write_failed = self._exit_reason(wait=0.5)
                self.on_closed(self._write_failed)
                return

    def close(self, timeout: float = 3.0) -> None:
        """Close stdin (the spec's polite shutdown), then terminate, then kill.

        Each step waits a share of `timeout`. On Windows terminate() is already
        a hard kill, so the polite step is the one that lets a server flush.
        stdin is closed by the writer thread, behind what it still has to
        write: closing it here would wait on a write the server may never
        read, and terminate() is what ends that write."""
        self._closing = True
        proc = self.proc
        if proc is None:
            return
        self._outbox.put(None)
        for step in ("wait", "terminate", "kill"):
            try:
                if step == "terminate":
                    proc.terminate()
                elif step == "kill":
                    proc.kill()
                proc.wait(timeout=max(0.2, timeout / 3))
                break
            except subprocess.TimeoutExpired:
                continue
            except OSError:
                break
        for t in self._threads:
            t.join(timeout=0.5)
        pipes = [proc.stdout, proc.stderr]
        if not any(t.is_alive() for t in self._threads
                   if t.name == "mcp-stdio-in"):
            pipes.append(proc.stdin)
        for pipe in pipes:
            try:
                if pipe:
                    pipe.close()
            except (OSError, ValueError):
                pass

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


_HEX = re.compile(rb"[0-9A-Fa-f]*")


def unmangle(resp: http.client.HTTPResponse) -> bool:
    """Undo a "chunked" header on a body that is not chunked. True if it did.

    MEASURED ON THIS PC (2026-10-01): a response to a loopback POST whose
    path is /mcp — the standard Streamable HTTP endpoint — arrives with its
    Content-Length replaced by `transfer-encoding: chunked` while the body
    stays as it was sent. Other paths, and responses that really are chunked
    (Ollama's), pass untouched; raw sockets reproduce it, so it is something
    on the machine's network path, not Python's http.server. http.client then
    reads `{"jsonrpc"...` as a chunk size and every HTTP MCP server fails.

    So the first bytes of a "chunked" body are checked: a real chunk starts
    with hex digits and then CR, LF, ';' or a space. A body that starts with
    anything else (`{`, `data:`, `event:`, `:`) cannot be chunked, and is read
    to the end of the connection instead — which is why requests are sent
    with Connection: close. Hex digits with nothing after them yet are
    ambiguous and left alone."""
    if not getattr(resp, "chunked", False):
        return False
    try:
        head = resp.fp.peek(64)[:64]
    except (AttributeError, OSError, ValueError):
        return False
    if not head:
        # Already at the end: a real chunked body is never empty (it ends
        # with "0\r\n\r\n"), so this was a 202 with Content-Length: 0.
        resp.chunked = False
        resp.length = 0
        resp.will_close = True
        return True
    rest = head[_HEX.match(head).end():]
    if not rest or rest[:1] in (b"\r", b"\n", b";", b" ", b"\t"):
        return False
    resp.chunked = False
    resp.length = None
    resp.will_close = True
    return True


class HttpTransport(_Transport):
    """Streamable HTTP: each message is one POST; replies are JSON or SSE.

    A request is POSTed on its own short-lived thread and connection, so a
    slow tool never blocks a cancel notification, and `abort` can shut exactly
    that request's socket. Notifications and responses are POSTed inline and
    expect 202 Accepted.
    """

    kind = "http"

    def __init__(self, url: str, *, allow_remote: bool = False,
                 headers: Optional[Dict[str, str]] = None,
                 io_timeout: float = 300.0) -> None:
        super().__init__()
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise McpConnectionError(
                f"{url!r} is not an http:// or https:// URL.")
        if not allow_remote and not is_local_host(parts.hostname):
            raise McpRemoteRefused(
                f"{parts.hostname} is not this computer. Remote documentation "
                f"servers are off unless 'Allow remote' is ticked for this "
                f"server.")
        self.url = url
        self.scheme = parts.scheme
        self.host = parts.hostname
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.path = (parts.path or "/") + (f"?{parts.query}" if parts.query
                                           else "")
        self.headers = dict(headers or {})
        self.io_timeout = io_timeout
        self.session_id: Optional[str] = None
        self._active: Dict[Any, http.client.HTTPConnection] = {}
        self._aborted: set = set()
        self._lock = threading.Lock()

    def describe(self) -> str:
        return self.url

    # -- plumbing --------------------------------------------------------
    def _connection(self, timeout: float) -> http.client.HTTPConnection:
        if self.scheme == "https":
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                self.host, self.port, timeout=timeout)
        else:
            conn = http.client.HTTPConnection(self.host, self.port,
                                              timeout=timeout)
        if self.host.lower().rstrip(".").endswith(".localhost"):
            # is_local_host calls `*.localhost` this computer (RFC 6761), but
            # Windows does not: MEASURED, getaddrinfo sends such a name to the
            # network's DNS server — and a resolver that answers unknown names
            # (ISP "search" pages do) would receive the question. So the
            # socket goes to 127.0.0.1 without asking DNS; the Host header and
            # TLS name stay the URL's.
            def loopback(address, *args, **kwargs):
                return socket.create_connection(("127.0.0.1", address[1]),
                                                *args, **kwargs)
            conn._create_connection = loopback        # type: ignore
        return conn

    def _headers(self) -> Dict[str, str]:
        # Connection: close — one request per connection anyway, and it makes
        # end-of-stream the end of the body, which _unmangle relies on.
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream",
             "Connection": "close"}
        h.update(self.headers)
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            h["MCP-Protocol-Version"] = self.protocol_version
        return h

    def _fault(self, request_id: Any, text: str) -> None:
        """Answer a request with a transport fault, unless it was aborted."""
        with self._lock:
            if request_id in self._aborted:
                return
        self.on_message({"jsonrpc": "2.0", "id": request_id,
                         "error": {"code": INTERNAL_ERROR, "message": text},
                         _TRANSPORT_FAULT: True})

    def _unreachable(self, exc: BaseException) -> str:
        if isinstance(exc, ConnectionRefusedError):
            return (f"Nothing is listening at {self.url} (connection "
                    f"refused). Is the server running?")
        if isinstance(exc, socket.timeout):
            return f"{self.url} stopped answering (network timeout)."
        if isinstance(exc, socket.gaierror):
            return f"{self.host} could not be found."
        return f"Could not talk to {self.url}: {_short(exc, 200)}"

    # -- sending ---------------------------------------------------------
    def send(self, message: dict) -> None:
        if "method" in message and "id" in message:
            t = threading.Thread(target=self._post_request, args=(message,),
                                 name=f"mcp-http-{message['id']}",
                                 daemon=True)
            t.start()
            return
        if message.get("method") == "notifications/cancelled":
            # Best effort, and never on the caller's clock: it is sent
            # because the caller gave up — MEASURED, posting it inline to a
            # wedged server turned Stop at 0.5 s into 10.6 s (the socket
            # timeout) and a 1 s timeout into 11 s.
            threading.Thread(target=self._post_inline, args=(message,),
                             name="mcp-http-cancel", daemon=True).start()
            return
        self._post_inline(message)

    def _post_inline(self, message: dict) -> None:
        """A notification or a response: POST, expect 202, read nothing."""
        conn = self._connection(timeout=10.0)
        try:
            conn.request("POST", self.path, body=json.dumps(message).encode(
                "utf-8"), headers=self._headers())
            resp = conn.getresponse()
            unmangle(resp)
            try:
                resp.read(_DISCARD)
            except http.client.IncompleteRead:
                pass                       # the body of a 202 is nothing
            if resp.status >= 400 and message.get("method") != \
                    "notifications/cancelled":
                raise McpConnectionError(self._http_error(
                    resp.status, resp.reason, ""))
        except (OSError, http.client.HTTPException) as exc:
            if message.get("method") == "notifications/cancelled":
                return                     # best effort, by definition
            raise McpConnectionError(self._unreachable(exc)) from None
        finally:
            conn.close()

    def _post_request(self, message: dict) -> None:
        rid = message["id"]
        conn = self._connection(timeout=self.io_timeout)
        with self._lock:
            self._active[rid] = conn
        try:
            conn.request("POST", self.path,
                         body=json.dumps(message).encode("utf-8"),
                         headers=self._headers())
            resp = conn.getresponse()
            unmangle(resp)
            sid = resp.getheader("Mcp-Session-Id")
            if sid and message.get("method") == "initialize":
                self.session_id = sid
            if resp.status == 404 and self.session_id:
                resp.read(_DISCARD)
                self.session_id = None
                # The session is over (spec: the client MUST start a new
                # one). Closing — not just failing this request — is what
                # lets a pool see `connected` turn False and reconnect;
                # otherwise every later question failed against a session
                # the server had forgotten. Fails this request too.
                self.on_closed("The server forgot this session (HTTP 404); "
                               "reconnect to start a new one.")
                return
            if resp.status >= 400:
                body = resp.read(2000).decode("utf-8", "replace")
                self._fault(rid, self._http_error(resp.status, resp.reason,
                                                  body))
                return
            if resp.status == 202:
                resp.read(_DISCARD)
                self._fault(rid, "The server accepted the request but sent "
                                 "no answer (HTTP 202).")
                return
            ctype = (resp.getheader("Content-Type") or "").split(";")[0]
            if ctype.strip().lower() == "text/event-stream":
                self._read_sse(resp, rid)
            else:
                raw = resp.read(MAX_MESSAGE_BYTES + 1)
                if len(raw) > MAX_MESSAGE_BYTES:
                    self._fault(rid, _too_large())
                    return
                try:
                    payload = json.loads(raw.decode("utf-8", "replace"))
                except (ValueError, RecursionError):
                    self._fault(rid, "The server's answer was not JSON: "
                                + _short(raw.decode("utf-8", "replace"), 160))
                    return
                for one in (payload if isinstance(payload, list)
                            else [payload]):
                    if isinstance(one, dict):
                        self.on_message(one)
        except (OSError, http.client.HTTPException) as exc:
            self._fault(rid, self._unreachable(exc))
        except Exception as exc:                          # noqa: BLE001
            # Anything else a malformed reply provokes fails THIS request
            # now, rather than leaving it to wait out its deadline.
            self._fault(rid, f"The server's answer could not be read: "
                             f"{_short(exc, 160)}")
        finally:
            with self._lock:
                self._active.pop(rid, None)
                self._aborted.discard(rid)
            conn.close()

    @staticmethod
    def _http_error(status: int, reason: str, body: str) -> str:
        if status in (401, 403):
            return (f"The server refused access (HTTP {status}). It may need "
                    f"a token or header this Council does not send.")
        if status == 405:
            return (f"The server does not accept POST here (HTTP 405) — it "
                    f"may be an older SSE-only server, or the URL may be "
                    f"missing its /mcp path.")
        detail = _short(body, 200)
        return f"HTTP {status} {reason}" + (f": {detail}" if detail else "")

    def _read_sse(self, resp, rid: Any) -> None:
        """Dispatch SSE events until this request's response has arrived.

        The stream may carry the server's own notifications and requests (a
        ping, a progress note) before the response; each is dispatched as it
        completes. Multi-line `data:` fields are joined with newlines, per
        the SSE format."""
        data: List[str] = []
        size = 0
        answered = False

        def flush() -> bool:
            if not data:
                return False
            text = "\n".join(data)
            data.clear()
            try:
                payload = json.loads(text)
            except (ValueError, RecursionError):
                return False
            got = False
            for one in (payload if isinstance(payload, list) else [payload]):
                if isinstance(one, dict):
                    self.on_message(one)
                    if one.get("id") == rid and "method" not in one:
                        got = True
            return got

        while True:
            raw = resp.readline(MAX_MESSAGE_BYTES + 1)
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line == "":
                size = 0
                if flush():
                    answered = True
                    break
                continue
            size += len(raw)
            if size > MAX_MESSAGE_BYTES:
                data.clear()
                self._fault(rid, _too_large())
                return
            if line.startswith(":"):
                continue
            name, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if name == "data":
                data.append(value)
        if not answered and not flush():
            self._fault(rid, "The server's event stream ended without an "
                             "answer.")

    # -- shutting down -----------------------------------------------------
    def abort(self, request_id: Any) -> None:
        with self._lock:
            conn = self._active.get(request_id)
            self._aborted.add(request_id)
        if conn is not None:
            try:
                if conn.sock is not None:
                    conn.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass

    def close(self, timeout: float = 3.0) -> None:
        with self._lock:
            active = list(self._active)
        for rid in active:
            self.abort(rid)
        if self.session_id:
            # The spec's explicit end of session. A server may answer 405
            # (it does not allow clients to end sessions); either is fine.
            conn = self._connection(timeout=min(timeout, 3.0))
            try:
                conn.request("DELETE", self.path, headers=self._headers())
                conn.getresponse().read(_DISCARD)
            except (OSError, http.client.HTTPException):
                pass
            finally:
                conn.close()
            self.session_id = None


# ======================================================================
# The client
# ======================================================================

class McpClient:
    """One MCP session. Blocking calls, safe to use from several threads.

    Use as a context manager, or call connect() and close()::

        with McpClient.stdio(sys.executable, ["server.py"]).connect() as c:
            tools = c.list_tools()
            hit = c.call_tool("search_docs", {"query": "ledger"})
    """

    def __init__(self, transport: _Transport, *,
                 timeout: float = DEFAULT_TIMEOUT,
                 client_name: str = CLIENT_NAME,
                 client_version: str = CLIENT_VERSION,
                 protocols: Sequence[str] = SUPPORTED_PROTOCOLS) -> None:
        self.transport = transport
        self.timeout = float(timeout)
        self.client_info = {"name": client_name, "version": client_version}
        self.protocols = tuple(protocols)
        transport.on_message = self._on_message
        transport.on_closed = self._on_closed
        self._ids = itertools.count(1)
        self._pending: Dict[Any, _Pending] = {}
        self._lock = threading.Lock()
        self._closed_reason: Optional[str] = None
        self._connected = False
        self.protocol_version: Optional[str] = None
        self.server_info: Dict[str, Any] = {}
        self.server_capabilities: Dict[str, Any] = {}
        self.instructions = ""
        #: notifications/message the server logged, newest last.
        self.log: Deque[dict] = collections.deque(maxlen=50)

    # -- construction ------------------------------------------------------
    @classmethod
    def stdio(cls, command: str, args: Sequence[str] = (), *,
              env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None,
              **kwargs) -> "McpClient":
        return cls(StdioTransport(command, args, env=env, cwd=cwd), **kwargs)

    @classmethod
    def http(cls, url: str, *, allow_remote: bool = False,
             headers: Optional[Dict[str, str]] = None,
             **kwargs) -> "McpClient":
        return cls(HttpTransport(url, allow_remote=allow_remote,
                                 headers=headers), **kwargs)

    def __enter__(self) -> "McpClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- lifecycle ---------------------------------------------------------
    def connect(self, *, timeout: Optional[float] = None,
                should_stop: Optional[Callable[[], bool]] = None
                ) -> "McpClient":
        """Start the transport and run the initialize handshake.

        Raises McpError (or a subclass) with a readable message; the
        transport is closed again on any failure, so a failed connect leaves
        no process behind."""
        try:
            self.transport.start()
            result = self.request("initialize", {
                "protocolVersion": self.protocols[0],
                "capabilities": {},
                "clientInfo": dict(self.client_info),
            }, timeout=timeout, should_stop=should_stop, _cancel=False)
            version = str(result.get("protocolVersion") or "")
            if version not in self.protocols:
                raise McpError(
                    f"The server speaks MCP protocol version {version!r}; "
                    f"this Council understands {', '.join(self.protocols)}.")
            self.protocol_version = version
            self.transport.protocol_version = version
            # Only objects are read: `"serverInfo": "abc"` made dict() raise
            # a ValueError that no caller expects from connect().
            self.server_info = _object(result.get("serverInfo"))
            self.server_capabilities = _object(result.get("capabilities"))
            self.instructions = str(result.get("instructions") or "")
            self.notify("notifications/initialized")
            self._connected = True
            return self
        except BaseException:
            self.close()
            raise

    @property
    def connected(self) -> bool:
        return self._connected and self._closed_reason is None

    def close(self, timeout: float = 3.0) -> None:
        """End the session and release the server. Never raises."""
        if self._closed_reason is None:
            self._closed_reason = "the connection was closed"
        self._connected = False
        try:
            self.transport.close(timeout)
        except Exception:                                 # noqa: BLE001
            pass
        self._fail_pending("the connection was closed")

    # -- messages ----------------------------------------------------------
    def notify(self, method: str, params: Optional[dict] = None) -> None:
        message: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.transport.send(message)

    def request(self, method: str, params: Optional[dict] = None, *,
                timeout: Optional[float] = None,
                should_stop: Optional[Callable[[], bool]] = None,
                _cancel: bool = True) -> dict:
        """Send one request and wait for its result.

        `_cancel=False` is for initialize, which the spec says must never be
        cancelled: a timed-out initialize is simply abandoned with the
        connection."""
        if self._closed_reason is not None and method != "initialize":
            raise McpConnectionError(
                f"Not connected ({self._closed_reason}).")
        rid = next(self._ids)
        pending = _Pending()
        with self._lock:
            self._pending[rid] = pending
        message: Dict[str, Any] = {"jsonrpc": "2.0", "id": rid,
                                   "method": method}
        if params is not None:
            message["params"] = params
        limit = float(timeout if timeout is not None else self.timeout)
        deadline = time.monotonic() + limit
        try:
            self.transport.send(message)
        except BaseException:
            with self._lock:
                self._pending.pop(rid, None)
            raise
        while not pending.event.wait(_POLL):
            if should_stop is not None and should_stop():
                self._abandon(rid, "stopped by the user", _cancel)
                raise McpCancelled(f"{method} was stopped.")
            if time.monotonic() >= deadline:
                self._abandon(rid, "timed out", _cancel)
                extra = self.transport.diagnostics()
                raise McpTimeout(
                    f"{method} got no answer within {limit:g} s, so it was "
                    f"cancelled." + (f" The server said: {extra}"
                                     if extra else ""))
        with self._lock:
            self._pending.pop(rid, None)
        reply = pending.message or {}
        if "_closed" in reply:
            raise McpConnectionError(
                f"{method} failed: {reply['_closed']}")
        if "error" in reply:
            err = reply.get("error")
            if not isinstance(err, dict):
                # `"error": "boom"` is not JSON-RPC, but it is plainly an
                # error; reading it as one beats an AttributeError.
                err = {"message": str(err) if err else ""}
            text = str(err.get("message") or "unknown error")
            if reply.get(_TRANSPORT_FAULT):
                raise McpConnectionError(text)
            raise McpError(f"{method} failed: {text}"
                           + (f" (code {err.get('code')})"
                              if err.get("code") is not None else ""),
                           code=err.get("code"), data=err.get("data"))
        result = reply.get("result")
        return result if isinstance(result, dict) else {}

    def _abandon(self, rid: Any, reason: str, cancel: bool) -> None:
        with self._lock:
            self._pending.pop(rid, None)
        try:
            self.transport.abort(rid)
        except Exception:                                 # noqa: BLE001
            pass
        if cancel:
            try:
                self.notify("notifications/cancelled",
                            {"requestId": rid, "reason": reason})
            except Exception:                             # noqa: BLE001
                pass

    def _on_message(self, message: dict) -> None:
        method = message.get("method")
        if method is not None and "id" in message:
            self._answer_server_request(message)
            return
        if method is not None:
            if method == "notifications/message":
                self.log.append(_object(message.get("params")))
            return
        rid = message.get("id")
        if not isinstance(rid, (str, int)) or isinstance(rid, bool):
            return                  # not an id this client ever sends
        with self._lock:
            pending = self._pending.get(rid)
            if pending is None and isinstance(rid, str) and rid.isdigit():
                pending = self._pending.get(int(rid))
        if pending is not None:
            pending.message = message
            pending.event.set()

    def _answer_server_request(self, message: dict) -> None:
        """The server asked US something. Ping is answered; roots are none;
        sampling and elicitation are declined (no capability was offered)."""
        method = message.get("method")
        reply: Dict[str, Any] = {"jsonrpc": "2.0", "id": message.get("id")}
        if method == "ping":
            reply["result"] = {}
        elif method == "roots/list":
            reply["result"] = {"roots": []}
        else:
            reply["error"] = {"code": METHOD_NOT_FOUND,
                              "message": f"{method} is not supported by this "
                                         f"client"}
        try:
            self.transport.send(reply)
        except Exception:                                 # noqa: BLE001
            pass

    def _on_closed(self, reason: str) -> None:
        if self._closed_reason is None:
            self._closed_reason = reason
        self._connected = False
        self._fail_pending(reason)

    def _fail_pending(self, reason: str) -> None:
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for p in pending:
            p.message = {"_closed": reason}
            p.event.set()

    # -- MCP methods -------------------------------------------------------
    def ping(self, *, timeout: Optional[float] = None) -> None:
        self.request("ping", timeout=timeout)

    def _paged(self, method: str, key: str, *, timeout=None,
               should_stop=None) -> List[dict]:
        items: List[dict] = []
        cursor: Optional[str] = None
        seen = set()
        for _ in range(MAX_PAGES):
            params = {"cursor": cursor} if cursor else None
            result = self.request(method, params, timeout=timeout,
                                  should_stop=should_stop)
            items.extend(_objects(result.get(key)))
            cursor = result.get("nextCursor")
            if not cursor:
                return items
            cursor = str(cursor)            # a cursor is opaque text
            if cursor in seen:
                raise McpError(f"{method}: the server repeated a page "
                               f"cursor ({_short(cursor, 40)}); stopping.")
            seen.add(cursor)
        raise McpError(f"{method}: more than {MAX_PAGES} pages; stopping.")

    def list_tools(self, *, timeout=None, should_stop=None) -> List[dict]:
        """Every tool, all pages: {name, description, inputSchema, ...}."""
        return self._paged("tools/list", "tools", timeout=timeout,
                           should_stop=should_stop)

    def call_tool(self, name: str, arguments: Optional[dict] = None, *,
                  timeout: Optional[float] = None,
                  should_stop: Optional[Callable[[], bool]] = None
                  ) -> ToolResult:
        """Run one tool. A tool that failed comes back with is_error=True —
        that is the tool's answer, not a protocol error, and the caller
        decides what to do with it."""
        result = self.request("tools/call",
                              {"name": name, "arguments": arguments or {}},
                              timeout=timeout, should_stop=should_stop)
        structured = result.get("structuredContent")
        return ToolResult(
            content=_objects(result.get("content")),
            structured=structured if isinstance(structured, dict) else None,
            is_error=bool(result.get("isError")))

    def list_resources(self, *, timeout=None, should_stop=None) -> List[dict]:
        """Every resource, all pages: {uri, name, mimeType, ...}."""
        return self._paged("resources/list", "resources", timeout=timeout,
                           should_stop=should_stop)

    def read_resource(self, uri: str, *, timeout=None,
                      should_stop=None) -> List[dict]:
        """The resource's contents: [{uri, mimeType, text | blob}]."""
        result = self.request("resources/read", {"uri": uri},
                              timeout=timeout, should_stop=should_stop)
        return _objects(result.get("contents"))

    @property
    def has_tools(self) -> bool:
        return "tools" in self.server_capabilities

    @property
    def has_resources(self) -> bool:
        return "resources" in self.server_capabilities

    def describe(self) -> str:
        info = self.server_info
        name = info.get("name") or self.transport.describe()
        version = info.get("version")
        return f"{name} {version}".strip() if version else str(name)


__all__ = [
    "SUPPORTED_PROTOCOLS", "DEFAULT_TIMEOUT", "McpClient", "McpError",
    "McpTimeout", "McpCancelled", "McpConnectionError", "McpRemoteRefused",
    "StdioTransport", "HttpTransport", "ToolResult", "content_text",
    "is_local_host",
]
