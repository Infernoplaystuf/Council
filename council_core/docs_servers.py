"""
council_core.docs_servers — the documentation servers the Council may ask.

WHAT A SERVER IS HERE
An MCP server that holds documentation: the bundled one (tools/
pydocs_mcp_server.py, any installed Python package, read from source), a
docs server the user runs locally, or — only when the user ticks "allow
remote" for it — one on another machine. Each is a ServerSpec saved in
docs_servers.json.

WHERE THE LIST LIVES: THE APP FOLDER, NOT THE VAULT
`paths.app_dir()` (~/.council, or $COUNCIL_APP_DIR). A server entry is a
command line the Council will execute and may carry environment variables —
an API token for a remote server, say — and the vault is the folder the
Librarian commits to git wholesale. Configuration that runs things and may
hold secrets does not belong in a folder that gets committed.

WHICH TOOL SEARCHES AND WHICH FETCHES
The orchestration in docs_qa needs two operations: SEARCH (query -> hits) and
FETCH (one hit -> its page). Servers name them differently — search_docs,
search, query-docs, resolve-library-id; get_doc, fetch, read_page — so roles
are detected from each tool's name, description and input schema, scored, and
can be overridden per server. A server with no fetch tool still works: its
resources are read instead, or the search hits' own text is used.

A POOL, BECAUSE START-UP IS THE EXPENSIVE PART
The bundled server indexes numpy in ~2.7 s on first use (measured; 0.2 s for
simplnx's stub). Starting it per question would pay that every time, so
connected clients are pooled per server and reused; `close_all()` releases
them, and an atexit hook makes sure no server process outlives the app.
"""
from __future__ import annotations

import atexit
import json
import os
import re
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import mcp_client
from .mcp_client import McpClient, McpError

FILE_NAME = "docs_servers.json"
FORMAT = 1
TRANSPORTS = ("stdio", "http")

#: The repository root — where tools/ and tests/data/ live.
APP_ROOT = Path(__file__).resolve().parent.parent
BUNDLED_SCRIPT = APP_ROOT / "tools" / "pydocs_mcp_server.py"
BUNDLED_NAME = "Python packages (bundled)"

#: Placeholders a saved spec may use, expanded when the server starts, so a
#: saved file keeps working when the app or its Python moves.
PYTHON_TOKEN, APP_TOKEN = "{python}", "{app}"


@dataclass
class ServerSpec:
    """One documentation server, as saved.

    Empty role fields mean "detect from the server's tool list"."""
    name: str
    transport: str = "stdio"
    command: str = ""
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    url: str = ""
    allow_remote: bool = False
    enabled: bool = True
    timeout: float = 60.0
    #: Packages this server is for, passed to search when the question
    #: names none (and to the bundled server as --package).
    packages: List[str] = field(default_factory=list)
    search_tool: str = ""
    search_arg: str = ""
    package_arg: str = ""
    fetch_tool: str = ""
    fetch_arg: str = ""
    bundled: bool = False

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "ServerSpec":
        if not isinstance(data, dict):
            raise ValueError("a server entry must be an object")
        known = {f for f in cls.__dataclass_fields__}       # noqa: SLF001
        clean = {k: v for k, v in data.items() if k in known}
        spec = cls(**clean)
        spec.args = [str(a) for a in (spec.args or [])]
        spec.env = {str(k): str(v) for k, v in (spec.env or {}).items()}
        spec.packages = [str(p) for p in (spec.packages or [])]
        spec.timeout = float(spec.timeout or 60.0)
        return spec

    def describe(self) -> str:
        """One line for a list: what kind of server, and where."""
        if self.transport == "http":
            where = self.url + ("  (remote allowed)" if self.allow_remote
                                else "")
        elif self.bundled:
            pkgs = ", ".join(self.packages) or "any installed package"
            py = _python_arg(self.args)
            where = f"bundled — {pkgs}" + (f" in {py}" if py else "")
        else:
            where = " ".join([self.command] + list(self.args))
        state = "" if self.enabled else "  [off]"
        return f"{self.name} — {where}{state}"

    def key(self) -> str:
        """Identity for the pool: anything that changes what runs."""
        return json.dumps([self.transport, self.command, self.args, self.env,
                           self.cwd, self.url, self.allow_remote],
                          sort_keys=True)


def _python_arg(args: Sequence[str]) -> str:
    args = list(args)
    for i, a in enumerate(args[:-1]):
        if a == "--python":
            return args[i + 1]
    return ""


# ======================================================================
# The bundled server
# ======================================================================

def bundled_spec(name: str = BUNDLED_NAME, *, python: str = "",
                 packages: Sequence[str] = (), paths: Sequence[str] = (),
                 cache: bool = True) -> ServerSpec:
    """A spec that runs tools/pydocs_mcp_server.py with this Council's Python.

    `python` documents another interpreter's packages (the nxpython env for
    simplnx); `paths` adds folders to look in first (the benchmark's
    synthetic package)."""
    args = [f"{APP_TOKEN}/tools/pydocs_mcp_server.py"]
    if python:
        args += ["--python", python]
    for p in paths:
        args += ["--path", str(p)]
    for p in packages:
        args += ["--package", str(p)]
    if cache:
        args += ["--cache-dir", "{cache}"]
    return ServerSpec(name=name, transport="stdio", command=PYTHON_TOKEN,
                      args=args, packages=list(packages), bundled=True,
                      search_tool="search_docs", search_arg="query",
                      package_arg="package", fetch_tool="get_doc",
                      fetch_arg="name", timeout=120.0)


def default_servers() -> List[ServerSpec]:
    return [bundled_spec()]


def cache_dir() -> Path:
    from . import paths
    return paths.app_dir() / "docs_cache"


def expand(spec: ServerSpec) -> Tuple[str, List[str], Dict[str, str], str]:
    """(command, args, env, cwd) with the placeholders filled in."""
    def fill(text: str) -> str:
        return (str(text).replace(PYTHON_TOKEN, sys.executable)
                .replace(APP_TOKEN, str(APP_ROOT))
                .replace("{cache}", str(cache_dir())))
    return (fill(spec.command), [fill(a) for a in spec.args],
            {k: fill(v) for k, v in spec.env.items()}, fill(spec.cwd))


# ======================================================================
# Saving and loading
# ======================================================================

def config_path(base: Optional[Path] = None) -> Path:
    if base is not None:
        base = Path(base)
        return base if base.suffix == ".json" else base / FILE_NAME
    from . import paths
    return paths.app_dir() / FILE_NAME


def load(path: Optional[Path] = None) -> List[ServerSpec]:
    """The saved servers, or the bundled default when there is no file.

    A damaged file is not silently replaced: `problem()` says why, and this
    returns the default so the Docs tab still works."""
    p = config_path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default_servers()
    except (OSError, ValueError):
        return default_servers()
    try:
        return [ServerSpec.from_json(s) for s in (data.get("servers") or [])]
    except (TypeError, ValueError, AttributeError):
        return default_servers()


def problem(path: Optional[Path] = None) -> str:
    p = config_path(path)
    if not p.exists():
        return ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        for s in data.get("servers") or []:
            ServerSpec.from_json(s)
        return ""
    except Exception as exc:                              # noqa: BLE001
        return f"{p.name} could not be read ({exc}); using the default."


def save(servers: Sequence[ServerSpec], path: Optional[Path] = None) -> Path:
    for s in servers:
        err = validate(s)
        if err:
            raise ValueError(err)
    names = [s.name for s in servers]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ValueError(f"two servers are called {dupes[0]!r}")
    p = config_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"format": FORMAT,
                               "servers": [s.to_json() for s in servers]},
                              indent=2), encoding="utf-8")
    os.replace(tmp, p)
    return p


def validate(spec: ServerSpec) -> str:
    """Why this spec cannot be used, or ""."""
    if not spec.name.strip():
        return "Give the server a name."
    if spec.transport not in TRANSPORTS:
        return f"Transport must be one of {', '.join(TRANSPORTS)}."
    if spec.transport == "stdio" and not spec.command.strip():
        return "A local (stdio) server needs a command to run."
    if spec.transport == "http":
        if not re.match(r"^https?://", spec.url.strip(), re.I):
            return "An HTTP server needs an http:// or https:// URL."
        from urllib.parse import urlsplit
        host = urlsplit(spec.url.strip()).hostname or ""
        if not spec.allow_remote and not mcp_client.is_local_host(host):
            return (f"{host} is not this computer. Tick 'Allow remote' to use "
                    f"a documentation server on another machine.")
    return ""


def add(spec: ServerSpec, path: Optional[Path] = None) -> List[ServerSpec]:
    servers = load(path)
    if any(s.name == spec.name for s in servers):
        raise ValueError(f"There is already a server called {spec.name!r}.")
    err = validate(spec)
    if err:
        raise ValueError(err)
    servers.append(spec)
    save(servers, path)
    return servers


def remove(name: str, path: Optional[Path] = None) -> List[ServerSpec]:
    servers = [s for s in load(path) if s.name != name]
    save(servers, path)
    release(name)
    return servers


def update(spec: ServerSpec, path: Optional[Path] = None) -> List[ServerSpec]:
    servers = load(path)
    out = [spec if s.name == spec.name else s for s in servers]
    if not any(s.name == spec.name for s in servers):
        out.append(spec)
    save(out, path)
    release(spec.name)
    return out


def parse_command_line(text: str) -> Tuple[str, List[str]]:
    """'python -m my_docs --port 0' -> ('python', ['-m', ...]), quotes kept
    together. Windows paths keep their backslashes."""
    import shlex
    parts = shlex.split(text.strip(), posix=False)
    parts = [p[1:-1] if len(p) > 1 and p[0] == p[-1] and p[0] in "\"'"
             else p for p in parts]
    return (parts[0], parts[1:]) if parts else ("", [])


# ======================================================================
# Roles: which tool searches, which fetches
# ======================================================================

SEARCH_ARGS = ("query", "q", "search", "text", "topic", "question",
               "keywords", "keyword", "term", "libraryName", "library_name")
PACKAGE_ARGS = ("package", "library", "lib", "module", "project", "pkg",
                "package_name", "library_id", "libraryId")
FETCH_ARGS = ("name", "id", "uri", "url", "path", "page", "doc", "symbol",
              "ref", "slug", "page_id", "doc_id", "key")


@dataclass
class Roles:
    search_tool: str = ""
    search_arg: str = ""
    package_arg: str = ""
    fetch_tool: str = ""
    fetch_arg: str = ""
    #: Fetch through resources/read when there is no fetch tool.
    via_resources: bool = False

    @property
    def usable(self) -> bool:
        return bool(self.search_tool and self.search_arg)


def _props(tool: dict) -> Dict[str, dict]:
    schema = tool.get("inputSchema") or {}
    props = schema.get("properties") or {}
    return props if isinstance(props, dict) else {}


def _required(tool: dict) -> List[str]:
    req = (tool.get("inputSchema") or {}).get("required") or []
    return [str(r) for r in req] if isinstance(req, list) else []


def _pick_arg(tool: dict, names: Sequence[str],
              fallback_first_string: bool = False) -> str:
    props = _props(tool)
    lower = {k.lower(): k for k in props}
    for n in names:
        if n.lower() in lower:
            return lower[n.lower()]
    if fallback_first_string:
        for k in _required(tool) + list(props):
            if (props.get(k) or {}).get("type", "string") == "string":
                return k
    return ""


def _score(tool: dict, words: Sequence[Tuple[str, float]]) -> float:
    name = str(tool.get("name") or "").lower()
    desc = str(tool.get("description") or "").lower()
    s = 0.0
    for w, weight in words:
        if w in name:
            s += weight
        if w in desc:
            s += weight / 4
    return s


_SEARCH_WORDS = (("search", 5), ("query", 4), ("find", 3), ("lookup", 3),
                 ("resolve", 2), ("docs", 1), ("doc", 1))
_FETCH_WORDS = (("get_doc", 5), ("fetch", 4), ("read", 3), ("get", 3),
                ("page", 2), ("open", 2), ("doc", 1), ("show", 1))


def detect_roles(tools: Sequence[dict], *, has_resources: bool = False,
                 spec: Optional[ServerSpec] = None) -> Roles:
    """Pick the search and fetch tools from a server's tool list.

    Explicit choices in `spec` win; a choice naming a tool the server does not
    have is ignored (and the detection used) rather than failing every
    question with "unknown tool"."""
    by_name = {str(t.get("name")): t for t in tools if t.get("name")}
    roles = Roles()

    search_candidates = sorted(
        (t for t in tools if _pick_arg(t, SEARCH_ARGS, True)),
        key=lambda t: -_score(t, _SEARCH_WORDS))
    if spec and spec.search_tool in by_name:
        search = by_name[spec.search_tool]
    else:
        search = next((t for t in search_candidates
                       if _score(t, _SEARCH_WORDS) > 0), None)
    if search is not None:
        roles.search_tool = str(search["name"])
        roles.search_arg = ((spec.search_arg if spec and spec.search_arg
                             in _props(search) else "")
                            or _pick_arg(search, SEARCH_ARGS, True))
        roles.package_arg = ((spec.package_arg if spec and spec.package_arg
                              in _props(search) else "")
                             or _pick_arg(search, PACKAGE_ARGS))

    fetch_candidates = sorted(
        (t for t in tools if str(t.get("name")) != roles.search_tool
         and _pick_arg(t, FETCH_ARGS, True)),
        key=lambda t: -_score(t, _FETCH_WORDS))
    if spec and spec.fetch_tool in by_name:
        fetch = by_name[spec.fetch_tool]
    else:
        fetch = next((t for t in fetch_candidates
                      if _score(t, _FETCH_WORDS) > 0
                      and _score(t, _FETCH_WORDS) >= _score(t, _SEARCH_WORDS)
                      and "list" not in str(t.get("name")).lower()), None)
    if fetch is not None:
        roles.fetch_tool = str(fetch["name"])
        roles.fetch_arg = ((spec.fetch_arg if spec and spec.fetch_arg
                            in _props(fetch) else "")
                           or _pick_arg(fetch, FETCH_ARGS, True))
    roles.via_resources = not roles.fetch_tool and has_resources
    return roles


# ======================================================================
# Connecting, testing, pooling
# ======================================================================

def open_client(spec: ServerSpec, *, timeout: Optional[float] = None,
                should_stop: Optional[Callable[[], bool]] = None
                ) -> McpClient:
    """A connected client for `spec`. Raises McpError with a readable text."""
    limit = float(timeout or spec.timeout or 60.0)
    if spec.transport == "http":
        client = McpClient.http(spec.url.strip(),
                                allow_remote=spec.allow_remote,
                                timeout=limit)
    else:
        command, args, env, cwd = expand(spec)
        client = McpClient.stdio(command, args, env=env, cwd=cwd or None,
                                 timeout=limit)
    return client.connect(timeout=limit, should_stop=should_stop)


@dataclass
class ServerReport:
    ok: bool
    message: str
    server: str = ""
    protocol: str = ""
    tools: List[Tuple[str, str]] = field(default_factory=list)
    resources: int = 0
    roles: Roles = field(default_factory=Roles)
    seconds: float = 0.0

    def lines(self) -> List[str]:
        out = [self.message]
        if self.ok:
            out.append(f"Server: {self.server}   protocol {self.protocol}   "
                       f"({self.seconds:.2f} s)")
            r = self.roles
            out.append("Search with: " + (f"{r.search_tool}({r.search_arg}"
                                          + (f", {r.package_arg}"
                                             if r.package_arg else "") + ")"
                                          if r.search_tool else "— none found"))
            out.append("Fetch with: " + (f"{r.fetch_tool}({r.fetch_arg})"
                                         if r.fetch_tool else
                                         "resources/read" if r.via_resources
                                         else "— none (search text is used)"))
            out.append(f"Tools ({len(self.tools)}):")
            out += [f"  • {n} — {d}" if d else f"  • {n}"
                    for n, d in self.tools]
            if self.resources:
                out.append(f"Resources: {self.resources}")
        return out


def check_server(spec: ServerSpec, *,
                 should_stop: Optional[Callable[[], bool]] = None,
                 timeout: Optional[float] = None) -> ServerReport:
    """Connect fresh (never the pool), list tools and resources, detect roles.

    "Test connection" must test the CONNECTION, so it does not reuse a pooled
    client that connected an hour ago."""
    err = validate(spec)
    if err:
        return ServerReport(False, err)
    t0 = time.perf_counter()
    try:
        client = open_client(spec, timeout=timeout, should_stop=should_stop)
    except McpError as exc:
        return ServerReport(False, f"Could not connect: {exc}",
                          seconds=time.perf_counter() - t0)
    try:
        tools = client.list_tools(should_stop=should_stop) \
            if client.has_tools else []
        resources = 0
        if client.has_resources:
            try:
                resources = len(client.list_resources(should_stop=should_stop))
            except McpError:
                resources = 0
        roles = detect_roles(tools, has_resources=client.has_resources,
                             spec=spec)
        names = [(str(t.get("name")), _first_line(t.get("description")))
                 for t in tools]
        ok = roles.usable
        msg = (f"Connected — {len(tools)} tool(s)." if ok else
               f"Connected, but no tool looks like a documentation search "
               f"({len(tools)} tool(s)). Choose one in the server's settings.")
        return ServerReport(ok, msg, client.describe(),
                          client.protocol_version or "", names, resources,
                          roles, time.perf_counter() - t0)
    except McpError as exc:
        return ServerReport(False, f"Connected, but listing failed: {exc}",
                          seconds=time.perf_counter() - t0)
    finally:
        client.close()


def _first_line(text: Any, limit: int = 110) -> str:
    line = str(text or "").strip().split("\n", 1)[0]
    return line if len(line) <= limit else line[:limit - 1] + "…"


class _Pooled:
    def __init__(self, spec: ServerSpec, client: McpClient, roles: Roles):
        self.spec = spec
        self.client = client
        self.roles = roles
        self.lock = threading.Lock()


_pool: Dict[str, _Pooled] = {}
_pool_lock = threading.Lock()
_opening: Dict[str, threading.Lock] = {}


def pooled(spec: ServerSpec, *,
           should_stop: Optional[Callable[[], bool]] = None
           ) -> Tuple[McpClient, Roles]:
    """A connected client and its roles, reused while the server lives.

    Keyed by name AND by everything that changes what runs, so editing a
    server's command gets a fresh process rather than the old one."""
    key = spec.name + "\x00" + spec.key()
    with _pool_lock:
        # One opener per server: two questions arriving together must not
        # start two processes and then close the one the other is using.
        opening = _opening.setdefault(key, threading.Lock())
    with opening:
        with _pool_lock:
            entry = _pool.get(key)
        if entry is not None and entry.client.connected:
            return entry.client, entry.roles
        if entry is not None:
            entry.client.close()
        client = open_client(spec, should_stop=should_stop)
        try:
            tools = client.list_tools(should_stop=should_stop) \
                if client.has_tools else []
        except McpError:
            client.close()
            raise
        roles = detect_roles(tools, has_resources=client.has_resources,
                             spec=spec)
        with _pool_lock:
            _pool[key] = _Pooled(spec, client, roles)
        return client, roles


def release(name: Optional[str] = None) -> None:
    """Close pooled clients — one server's, or all of them."""
    with _pool_lock:
        keys = [k for k in _pool if name is None or k.split("\x00", 1)[0]
                == name]
        entries = [_pool.pop(k) for k in keys]
    for e in entries:
        e.client.close()


def close_all() -> None:
    release(None)


atexit.register(close_all)


__all__ = ["ServerSpec", "Roles", "ServerReport", "FILE_NAME", "bundled_spec",
           "default_servers", "config_path", "load", "save", "add", "remove",
           "update", "validate", "detect_roles", "open_client", "check_server",
           "pooled", "release", "close_all", "expand", "parse_command_line",
           "problem"]
