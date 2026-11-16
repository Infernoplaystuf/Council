"""
A STAND-IN for a Dyndrite MCP server: made-up tools and made-up answers, so
the LPBF plugin's Dyndrite page (and its tests) can be tried end to end
before the real server is reachable. It knows nothing about Dyndrite's
real API; every answer says it is fake.

It wants a token, like the real one:
    stdio:  the environment variable DYNDRITE_TOKEN must equal --token
    http:   every request must carry  Authorization: Bearer <token>
            (anything else gets HTTP 401)

    python tools/fake_dyndrite_mcp_server.py --token demo
    python tools/fake_dyndrite_mcp_server.py --http 8765 --token demo

In the plugin: Server "Command Council starts", command
    python tools/fake_dyndrite_mcp_server.py --token demo
with "Token kept in" env:DYNDRITE_TOKEN and DYNDRITE_TOKEN=demo set in
Windows; or Server "HTTP address", http://127.0.0.1:8765/mcp.

Tools: search_docs and get_parameter_syntax (read-only), create_build
(changes things; annotated destructive), and run_python (no annotations; its
name says it changes things). Stdlib only; imports nothing of Council.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

PROTOCOL = "2025-06-18"

TOOLS: List[Dict[str, Any]] = [
    {"name": "search_docs",
     "description": "Search the (fake) Dyndrite documentation.",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string"}},
                     "required": ["query"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "get_parameter_syntax",
     "description": "How a build parameter is written in a (fake) recipe.",
     "inputSchema": {"type": "object",
                     "properties": {"parameter": {"type": "string"}},
                     "required": ["parameter"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "create_build",
     "description": "Create a (fake) build from a manifest.",
     "inputSchema": {"type": "object",
                     "properties": {"name": {"type": "string"},
                                    "specimens": {"type": "integer"}},
                     "required": ["name"]},
     "annotations": {"readOnlyHint": False, "destructiveHint": True}},
    {"name": "run_python",
     "description": "Run a (fake) recipe script.",
     "inputSchema": {"type": "object",
                     "properties": {"code": {"type": "string"}},
                     "required": ["code"]}},
]

DOCS = {
    "hatch": "FAKE DOCS: hatch spacing is set with "
             "recipe.set_hatch_spacing(mm=0.1).",
    "power": "FAKE DOCS: laser power is set with recipe.set_power(watts=280).",
    "speed": "FAKE DOCS: scan speed is set with "
             "recipe.set_scan_speed(mm_per_s=1000).",
}


class Server:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def handle(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if "id" not in msg:
            return None                      # a notification
        rid, method = msg["id"], msg.get("method")
        params = msg.get("params") or {}
        if method == "initialize":
            result = {"protocolVersion": PROTOCOL,
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fake-dyndrite", "version": "0"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            result = self.tool(params.get("name"), params.get("arguments") or {})
        elif method == "ping":
            result = {}
        else:
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": -32601, "message": f"no method {method}"}}
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        self.calls.append({"name": name, "arguments": args})

        def text(t: str, error: bool = False) -> Dict[str, Any]:
            return {"content": [{"type": "text", "text": t}], "isError": error}
        if name == "search_docs":
            q = str(args.get("query", "")).lower()
            hits = [v for k, v in DOCS.items() if k in q]
            return text("\n".join(hits) or "FAKE DOCS: nothing found.")
        if name == "get_parameter_syntax":
            p = str(args.get("parameter", "")).lower()
            for k, v in DOCS.items():
                if k in p:
                    return text(v)
            return text(f"FAKE DOCS: no parameter {p!r}.", error=True)
        if name == "create_build":
            return text(f"FAKE: build {args.get('name')!r} created with "
                        f"{args.get('specimens', 0)} specimens.")
        if name == "run_python":
            return text("FAKE: script accepted (nothing was run).")
        return text(f"no tool {name}", error=True)


def serve_stdio(token: str) -> int:
    if os.environ.get("DYNDRITE_TOKEN", "") != token:
        sys.stderr.write("fake-dyndrite: DYNDRITE_TOKEN is missing or wrong\n")
        return 3
    server = Server()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        reply = server.handle(json.loads(line))
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0


def make_http(token: str, port: int = 0, server: Optional[Server] = None):
    """A ThreadingHTTPServer on 127.0.0.1 (port 0 = any free port)."""
    state = server or Server()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):                     # quiet
            pass

        def do_POST(self):
            if self.headers.get("Authorization") != f"Bearer {token}":
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            reply = state.handle(json.loads(body or b"{}"))
            if reply is None:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            data = json.dumps(reply).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_DELETE(self):
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.fake = state
    return httpd


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--token", required=True)
    ap.add_argument("--http", type=int, default=0, metavar="PORT")
    a = ap.parse_args()
    if a.http:
        httpd = make_http(a.token, a.http)
        print(f"fake Dyndrite MCP at http://127.0.0.1:{a.http}/mcp", flush=True)
        httpd.serve_forever()
        return 0
    return serve_stdio(a.token)


if __name__ == "__main__":
    sys.exit(main())
