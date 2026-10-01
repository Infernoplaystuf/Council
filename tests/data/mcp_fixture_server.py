"""
A scripted MCP server for the client tests — stdio when run, and the same
dispatch logic imported by the HTTP fixture in tests/test_mcp_client.py.

Behaviour is chosen on the command line, so one file covers the protocol
cases: version negotiation, pagination, tool errors, protocol errors, slow
tools (and their cancellation), a crash at start-up, junk on stdout, and a
server-initiated ping.

    python mcp_fixture_server.py [--version V] [--exit-on-init CODE]
                                 [--noise] [--page-size N] [--no-tools]

Stdlib only. Never imports council code: it stands in for a server someone
else wrote.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

SUPPORTED = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

TOOLS = [
    {"name": "search_docs", "description": "Search the docs.",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string"},
                                    "package": {"type": "string"}},
                     "required": ["query"]}},
    {"name": "get_doc", "description": "Read one documentation page.",
     "inputSchema": {"type": "object",
                     "properties": {"name": {"type": "string"}},
                     "required": ["name"]}},
    {"name": "echo", "description": "Echo the arguments back.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "slow", "description": "Sleep, then answer.",
     "inputSchema": {"type": "object",
                     "properties": {"seconds": {"type": "number"}}}},
    {"name": "fail", "description": "A tool that fails.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "boom", "description": "A protocol error.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "cancellations", "description": "Request ids seen cancelled.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "ping_me", "description": "Ping the client first.",
     "inputSchema": {"type": "object", "properties": {}}},
]

PAGES = {
    "fx.alpha": "# fx.alpha\n\nalpha(x, *, scale=2) multiplies x by scale. "
                "The default scale is 2.",
    "fx.beta": "# fx.beta\n\nbeta() returns the word 'beta'.",
}

RESOURCES = [{"uri": f"fx://page/{i}", "name": f"page {i}",
              "mimeType": "text/plain"} for i in range(5)]


class Fixture:
    """Dispatch shared by the stdio and HTTP fixtures."""

    def __init__(self, version: str = "", page_size: int = 0,
                 tools: bool = True):
        self.version = version
        self.page_size = page_size
        self.tools = tools
        self.cancelled: List[Any] = []
        self.notifications: List[str] = []
        self.negotiated = ""
        #: Set by the transport: send a request to the client and wait.
        self.ask_client: Optional[Callable[[dict], Optional[dict]]] = None
        self.lock = threading.Lock()

    def note(self, message: dict) -> None:
        method = message.get("method", "")
        with self.lock:
            self.notifications.append(method)
            if method == "notifications/cancelled":
                self.cancelled.append((message.get("params") or {})
                                      .get("requestId"))

    def _page(self, items: List[dict], params: dict, key: str) -> dict:
        size = self.page_size or len(items) or 1
        start = int((params or {}).get("cursor") or 0)
        out: Dict[str, Any] = {key: items[start:start + size]}
        if start + size < len(items):
            out["nextCursor"] = str(start + size)
        return out

    def handle(self, message: dict) -> Optional[dict]:
        if "method" not in message:
            return None
        if "id" not in message:
            self.note(message)
            return None
        rid, method = message["id"], message["method"]
        params = message.get("params") or {}
        try:
            return {"jsonrpc": "2.0", "id": rid,
                    "result": self._dispatch(rid, method, params)}
        except _RpcError as exc:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": exc.code, "message": exc.text}}

    def _dispatch(self, rid, method: str, params: dict) -> dict:
        if method == "initialize":
            asked = params.get("protocolVersion", "")
            v = self.version or (asked if asked in SUPPORTED
                                 else SUPPORTED[0])
            self.negotiated = v
            caps: Dict[str, Any] = {"resources": {}}
            if self.tools:
                caps["tools"] = {}
            return {"protocolVersion": v, "capabilities": caps,
                    "serverInfo": {"name": "fixture", "version": "0.1"}}
        if method == "ping":
            return {}
        if method == "tools/list":
            return self._page(TOOLS, params, "tools")
        if method == "resources/list":
            return self._page(RESOURCES, params, "resources")
        if method == "resources/read":
            uri = params.get("uri", "")
            if not uri.startswith("fx://"):
                raise _RpcError(-32002, f"Resource not found: {uri}")
            return {"contents": [{"uri": uri, "mimeType": "text/plain",
                                  "text": f"text of {uri}"}]}
        if method == "tools/call":
            return self._tool(rid, params.get("name"),
                              params.get("arguments") or {})
        raise _RpcError(-32601, f"Method not found: {method}")

    def _tool(self, rid, name: str, args: dict) -> dict:
        def text(t: str, error: bool = False) -> dict:
            return {"content": [{"type": "text", "text": t}],
                    "isError": error}
        if name == "echo":
            return text(json.dumps(args, sort_keys=True))
        if name == "slow":
            deadline = time.monotonic() + float(args.get("seconds", 5))
            while time.monotonic() < deadline:
                with self.lock:
                    if rid in self.cancelled:
                        return text("cancelled")
                time.sleep(0.02)
            return text("done")
        if name == "fail":
            return text("this tool failed on purpose", error=True)
        if name == "boom":
            raise _RpcError(-32603, "the tool exploded")
        if name == "cancellations":
            with self.lock:
                return text(json.dumps(self.cancelled))
        if name == "ping_me":
            reply = self.ask_client({"jsonrpc": "2.0", "id": "srv-1",
                                     "method": "ping"}) \
                if self.ask_client else None
            ok = bool(reply) and reply.get("result") == {}
            return text("pong ok" if ok else f"pong missing: {reply}")
        if name == "search_docs":
            q = str(args.get("query", "")).lower()
            hits = [{"name": n, "summary": t.split("\n\n", 1)[1][:60]}
                    for n, t in PAGES.items()
                    if any(w in t.lower() for w in q.split())]
            return {"content": [{"type": "text",
                                 "text": json.dumps(hits)}],
                    "isError": False}
        if name == "get_doc":
            page = PAGES.get(str(args.get("name", "")))
            return text(page) if page else text("no such page", error=True)
        raise _RpcError(-32602, f"Unknown tool: {name}")


class _RpcError(Exception):
    def __init__(self, code: int, text: str):
        super().__init__(text)
        self.code = code
        self.text = text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="")
    ap.add_argument("--exit-on-init", type=int, default=None)
    ap.add_argument("--noise", action="store_true")
    ap.add_argument("--page-size", type=int, default=0)
    ap.add_argument("--no-tools", action="store_true")
    args = ap.parse_args()
    out = sys.stdout.buffer
    write_lock = threading.Lock()
    fx = Fixture(args.version, args.page_size, not args.no_tools)
    waiting: Dict[Any, dict] = {}
    arrived = threading.Condition()

    def write(obj: dict) -> None:
        with write_lock:
            out.write((json.dumps(obj) + "\n").encode("utf-8"))
            out.flush()

    def ask_client(request: dict) -> Optional[dict]:
        write(request)
        deadline = time.monotonic() + 3
        with arrived:
            while request["id"] not in waiting:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                arrived.wait(left)
            return waiting.pop(request["id"])

    fx.ask_client = ask_client

    def run(message: dict) -> None:
        reply = fx.handle(message)
        if reply is not None:
            write(reply)

    for raw in iter(sys.stdin.buffer.readline, b""):
        line = raw.strip()
        if not line:
            continue
        message = json.loads(line)
        if message.get("method") == "initialize":
            if args.exit_on_init is not None:
                print("fixture: refusing to start (as asked)",
                      file=sys.stderr, flush=True)
                return args.exit_on_init
            if args.noise:
                with write_lock:
                    out.write(b"Welcome to the fixture server!\n")
                    out.flush()
        if "method" not in message:          # a response to our request
            with arrived:
                waiting[message.get("id")] = message
                arrived.notify_all()
            continue
        if "id" not in message:
            fx.note(message)
            continue
        threading.Thread(target=run, args=(message,), daemon=True).start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
