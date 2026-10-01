"""
A misbehaving MCP stdio server: one fault per --mode, for the review tests
(tests/test_docs_misbehaving.py). Stdlib only, never imports council code.

    python mcp_evil_server.py --mode MODE [--mb N]

  ok            behaves (search_docs finds pkg.Ledger; get_doc reads it)
  error-str     every tools/call fails with "error": "boom" (not an object)
  bad-id        the first tools/call reply is preceded by one whose id is a
                list
  bad-log       a notifications/message with params "x" before each reply
  bad-info      initialize answers serverInfo "abc", capabilities a list
  bad-shapes    tools/call answers content 7 and structuredContent "x"
  bad-resource  tools/call answers a resource whose "resource" is a string
  hang          never answers tools/call (but keeps reading stdin)
  noread        stops reading stdin after tools/list
  huge          tools/call answers one line of --mb megabytes
"""
import json
import sys
import time

MODE = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv else ""
MB = float(sys.argv[sys.argv.index("--mb") + 1]) if "--mb" in sys.argv else 2

TOOLS = [{"name": "search_docs", "description": "Search the docs.",
          "inputSchema": {"type": "object",
                          "properties": {"query": {"type": "string"}},
                          "required": ["query"]}},
         {"name": "get_doc", "description": "Read one page.",
          "inputSchema": {"type": "object",
                          "properties": {"name": {"type": "string"}},
                          "required": ["name"]}}]
PAGE = ("pkg.Ledger(capacity=64)\n\nA ledger of entries. The capacity "
        "defaults to 64.")
out = sys.stdout.buffer


def write(obj):
    out.write((json.dumps(obj) + "\n").encode())
    out.flush()


def result(rid, value):
    write({"jsonrpc": "2.0", "id": rid, "result": value})


def text(t):
    return {"content": [{"type": "text", "text": t}], "isError": False}


calls = 0
for raw in iter(sys.stdin.buffer.readline, b""):
    msg = json.loads(raw)
    if "id" not in msg or "method" not in msg:
        continue
    rid, method = msg["id"], msg["method"]
    if method == "initialize":
        info, caps = {"name": "evil", "version": "1"}, {"tools": {}}
        if MODE == "bad-info":
            info, caps = "abc", ["tools"]
        result(rid, {"protocolVersion": "2025-06-18", "capabilities": caps,
                     "serverInfo": info})
    elif method == "tools/list":
        result(rid, {"tools": TOOLS})
        if MODE == "noread":
            time.sleep(3600)
    elif method == "tools/call":
        calls += 1
        if MODE == "hang":
            continue
        if MODE == "error-str":
            write({"jsonrpc": "2.0", "id": rid, "error": "boom"})
            continue
        if MODE == "bad-id" and calls == 1:
            write({"jsonrpc": "2.0", "id": [rid], "result": {}})
        if MODE == "bad-log":
            write({"jsonrpc": "2.0", "method": "notifications/message",
                   "params": "x"})
        if MODE == "bad-shapes":
            result(rid, {"content": 7, "structuredContent": "x"})
        elif MODE == "bad-resource":
            result(rid, {"content": [
                {"type": "resource", "resource": "some text"},
                {"type": "text", "text": json.dumps(
                    [{"name": "pkg.Ledger", "summary": "A ledger"}])}]})
        elif MODE == "huge":
            result(rid, text("ledger " * int(MB * 1024 * 1024 / 7)))
        elif msg["params"]["name"] == "search_docs":
            result(rid, text(json.dumps(
                [{"name": "pkg.Ledger", "summary": "A ledger of entries"}])))
        else:
            result(rid, text(PAGE))
    else:
        result(rid, {})
