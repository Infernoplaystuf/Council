"""
council_core.node_routing — which role answers on which machine.

    <vault>/node_routing.json
    {"version": 1,
     "routing_enabled": false,
     "nodes": {"pi-kitchen": {"url": "http://192.168.1.50:11434",
                              "enabled": true, "parallel": 1}},
     "roles": {"peasant": {"node": "pi-kitchen", "fallback": "here"}}}

OPT-IN, THREE TIMES OVER
Nothing leaves this PC unless the user turns routing on, registers a machine,
enables it, and binds a role to it. Out of the box the file does not exist
and every call stays local, exactly as before. A host is let through the
engine's localhost guard ONLY when it is the URL of a registered, enabled
node while routing is on (`is_allowed_host`) — a model reply, a setting or a
typo elsewhere cannot add one.

WHY A FILE OF ITS OWN
model_slots.json is rebuilt from scratch by the Models tab when it saves
(model_slots.from_role_files), so a machine setting stored there would be
dropped silently. Bindings are by ROLE, not slot, for the same reason — slot
names are regenerated; role names are not.

WHAT A BINDING DOES
The role still uses its own model (its slot's "ollama:<name>"); the call is
sent to the node's Ollama instead of this PC's. A role whose slot is an
in-app .gguf file cannot be sent anywhere and stays local. If the node does
not answer before the first token — refused, unreachable, model missing,
first-reply limit — the call is answered on this PC with the role's own
model (fallback "here", the default) or fails (fallback "fail"), and the
node COOLS DOWN: 30 s, doubling
to 5 min, so a dead node costs one wait, not every call. A call that already
produced output is never re-sent.

CONCURRENCY
`host_slot(url)` is a per-machine gate: at most `parallel` calls at a time on
a node (this PC: COUNCIL_LOCAL_PARALLEL, default 1). `run_parallel` runs a
list of calls at once — each still passes its own machine's gate — so work
spread over machines really runs side by side, and work piled on one machine
queues instead of thrashing it.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

FILE_NAME = "node_routing.json"
VERSION = 1
FALLBACKS = ("here", "fail")
COOLDOWN_FIRST_S = 30.0
COOLDOWN_MAX_S = 300.0
LOCAL = "this-pc"

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
_URL = re.compile(r"^https?://[A-Za-z0-9.\-\[\]:]+(:\d{1,5})?/?$")


class RoutingError(ValueError):
    pass


@dataclass
class Node:
    name: str
    url: str
    enabled: bool = False
    parallel: int = 1


@dataclass
class Binding:
    node: str
    fallback: str = "here"


@dataclass
class Routing:
    routing_enabled: bool = False
    nodes: Dict[str, Node] = field(default_factory=dict)
    roles: Dict[str, Binding] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {"version": VERSION, "routing_enabled": self.routing_enabled,
                "nodes": {n.name: {"url": n.url, "enabled": n.enabled,
                                   "parallel": n.parallel}
                          for n in self.nodes.values()},
                "roles": {r: {"node": b.node, "fallback": b.fallback}
                          for r, b in self.roles.items()}}


def normalise_url(url: str) -> str:
    return str(url or "").strip().rstrip("/").lower()


def parse(data: Any) -> Routing:
    """A Routing from the file's JSON. Raises RoutingError on nonsense."""
    if not isinstance(data, dict):
        raise RoutingError("node_routing.json must hold an object")
    nodes: Dict[str, Node] = {}
    for name, spec in (data.get("nodes") or {}).items():
        name = str(name).strip()
        if not _NAME.match(name):
            raise RoutingError(f"bad machine name {name!r}")
        if not isinstance(spec, dict):
            raise RoutingError(f"machine {name!r} must be an object")
        url = str(spec.get("url", "")).strip().rstrip("/")
        if not _URL.match(url):
            raise RoutingError(f"machine {name!r} has a bad url {url!r}")
        try:
            parallel = int(spec.get("parallel", 1))
        except (TypeError, ValueError):
            raise RoutingError(f"machine {name!r} has a bad 'parallel'")
        if not 1 <= parallel <= 16:
            raise RoutingError(f"machine {name!r}: parallel must be 1-16")
        nodes[name] = Node(name, url, bool(spec.get("enabled", False)),
                           parallel)
    roles: Dict[str, Binding] = {}
    for role, spec in (data.get("roles") or {}).items():
        if not isinstance(spec, dict):
            raise RoutingError(f"role {role!r} must be an object")
        node = str(spec.get("node", "")).strip()
        if node not in nodes:
            raise RoutingError(f"role {role!r} uses unknown machine {node!r}")
        fallback = str(spec.get("fallback", "here"))
        if fallback not in FALLBACKS:
            raise RoutingError(f"role {role!r}: fallback must be "
                               f"{' or '.join(FALLBACKS)}")
        roles[str(role)] = Binding(node, fallback)
    return Routing(bool(data.get("routing_enabled", False)), nodes, roles)


def path_for(vault_dir: Path) -> Path:
    return Path(vault_dir) / FILE_NAME


def load(vault_dir: Path) -> Routing:
    """The saved routing, or routing off. Never raises: a damaged file means
    every call stays on this PC (see `problem`)."""
    try:
        return parse(json.loads(path_for(vault_dir).read_text(
            encoding="utf-8")))
    except Exception:                                     # noqa: BLE001
        return Routing()


def problem(vault_dir: Path) -> str:
    path = path_for(vault_dir)
    if not path.exists():
        return ""
    try:
        parse(json.loads(path.read_text(encoding="utf-8")))
        return ""
    except Exception as exc:                              # noqa: BLE001
        return f"{path.name} could not be used ({exc}); every call stays " \
               "on this PC."


def save(vault_dir: Path, routing: Routing) -> Path:
    """Validate, then write atomically (temp file + replace)."""
    data = routing.to_json()
    parse(data)
    path = path_for(vault_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    invalidate()
    return path


# ============================================================
# The live config (read once, kept until invalidated)
# ============================================================

_lock = threading.Lock()
_current: Optional[Routing] = None
_current_vault: Optional[Path] = None


def _vault() -> Path:
    from . import paths
    return paths.vault_dir()


def current() -> Routing:
    global _current, _current_vault
    with _lock:
        if _current is None:
            try:
                _current_vault = _vault()
                _current = load(_current_vault)
            except Exception:                             # noqa: BLE001
                _current = Routing()
        return _current


def invalidate() -> None:
    global _current
    with _lock:
        _current = None


def set_current(routing: Optional[Routing]) -> None:
    """For tests and callers that hold their own config."""
    global _current
    with _lock:
        _current = routing


# ============================================================
# Routing decisions
# ============================================================

@dataclass(frozen=True)
class Target:
    node: str
    url: str
    fallback: str


def route(role: Optional[str], routing: Optional[Routing] = None
          ) -> Optional[Target]:
    """Where `role`'s calls go: a node Target, or None for this PC. A node
    that is disabled, unknown or cooling down means this PC (or, with
    fallback "fail", a Target the caller will find cooling)."""
    r = routing or current()
    if not r.routing_enabled or not role:
        return None
    binding = r.roles.get(role)
    if binding is None:
        return None
    node = r.nodes.get(binding.node)
    if node is None or not node.enabled:
        return None
    return Target(node.name, node.url, binding.fallback)


def is_allowed_host(url: str, routing: Optional[Routing] = None) -> bool:
    """Whether `url` is a registered, enabled node with routing on — the
    only way past the engine's localhost guard besides COUNCIL_REMOTE_NODES.
    """
    r = routing or current()
    if not r.routing_enabled:
        return False
    want = normalise_url(url)
    return any(n.enabled and normalise_url(n.url) == want
               for n in r.nodes.values())


def enabled_nodes(routing: Optional[Routing] = None) -> List[Node]:
    r = routing or current()
    if not r.routing_enabled:
        return []
    return [n for n in r.nodes.values() if n.enabled]


# ============================================================
# Cooldown: a failing node rests
# ============================================================

_cool_lock = threading.Lock()
_cooling: Dict[str, tuple] = {}            # url -> (until, last_wait_s)


def mark_failed(url: str, now: Optional[float] = None) -> float:
    """Rest a node: 30 s, doubling on each failure in a row, up to 5 min.
    Returns the rest in seconds."""
    now = time.monotonic() if now is None else now
    key = normalise_url(url)
    with _cool_lock:
        _until, last = _cooling.get(key, (0.0, 0.0))
        wait = min(COOLDOWN_MAX_S, last * 2 if last else COOLDOWN_FIRST_S)
        _cooling[key] = (now + wait, wait)
    return wait


def mark_ok(url: str) -> None:
    with _cool_lock:
        _cooling.pop(normalise_url(url), None)


def cooling(url: str, now: Optional[float] = None) -> float:
    """Seconds this node still rests (0 when it may be used)."""
    now = time.monotonic() if now is None else now
    with _cool_lock:
        until, _last = _cooling.get(normalise_url(url), (0.0, 0.0))
    return max(0.0, until - now)


def reset_cooldowns() -> None:
    with _cool_lock:
        _cooling.clear()


# ============================================================
# Concurrency: a gate per machine, and running calls side by side
# ============================================================

_gate_lock = threading.Lock()
_gates: Dict[str, tuple] = {}              # url -> (limit, semaphore)


def _limit_for(url: str, routing: Optional[Routing] = None) -> int:
    key = normalise_url(url)
    r = routing or current()
    for n in r.nodes.values():
        if normalise_url(n.url) == key:
            return n.parallel
    try:
        return max(1, min(16, int(os.environ.get(
            "COUNCIL_LOCAL_PARALLEL", "1"))))
    except ValueError:
        return 1


@contextmanager
def host_slot(url: str, routing: Optional[Routing] = None) -> Iterator[None]:
    """Hold one of the machine's `parallel` call slots while inside."""
    key = normalise_url(url) or LOCAL
    limit = _limit_for(url, routing)
    with _gate_lock:
        gate = _gates.get(key)
        if gate is None or gate[0] != limit:
            gate = (limit, threading.BoundedSemaphore(limit))
            _gates[key] = gate
    sem = gate[1]
    sem.acquire()
    try:
        yield
    finally:
        sem.release()


@dataclass
class Outcome:
    value: Any = None
    error: Optional[BaseException] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def run_parallel(calls: Sequence[Callable[[], Any]],
                 max_workers: Optional[int] = None) -> List[Outcome]:
    """Run every call at once (each passes its own machine's gate inside the
    engine), and return their outcomes in order. Never raises: a call that
    failed comes back with its error."""
    if not calls:
        return []
    workers = max(1, min(len(calls), max_workers or len(calls)))

    def one(fn: Callable[[], Any]) -> Outcome:
        try:
            return Outcome(value=fn())
        except BaseException as exc:                      # noqa: BLE001
            return Outcome(error=exc)

    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="council-parallel") as pool:
        return list(pool.map(one, calls))


__all__ = ["FILE_NAME", "Node", "Binding", "Routing", "RoutingError",
           "Target", "parse", "load", "save", "problem", "current",
           "invalidate", "set_current", "route", "is_allowed_host",
           "enabled_nodes", "mark_failed", "mark_ok", "cooling",
           "reset_cooldowns", "host_slot", "run_parallel", "Outcome",
           "normalise_url"]
