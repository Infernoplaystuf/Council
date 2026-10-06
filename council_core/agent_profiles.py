"""Agent profiles — the agent creator.

A profile is what the user called an agent: a model (by role), the roles
connected to it, the tools it may use, a step budget and instructions. The user
can also describe a tool they want, and the council builds it:

  1. the drafter role (coder) writes it — tool_forge.generate_tool, which
     validates it in the analyst sandbox and test-runs it when it needs no
     arguments, fixing errors up to three times;
  2. each connected role (judge and skeptic by default) reviews the code
     against the request and the sandbox rules and answers approve /
     concerns, as JSON;
  3. if a reviewer objects, the drafter revises once with the concerns, and
     the reviewers look again;
  4. the result is attached to the profile or waits for the user, by the
     user's setting ``tool_attach``:
       "approve"   (default) every council-made tool waits for the user;
       "automatic" attached at once — but ONLY when every reviewer approved
                   and the sandbox test passed (or the tool needs arguments
                   and so could not be test-run); anything else still waits.

What an agent may use, and why it is safe:
  * built-in tools are the READ-ONLY ones (list / search / read files, the
    pandas sandbox, memory, and read-only knowledge-graph lookups).
    ``write_tool`` and ``run_app_tool`` are never offered: an agent cannot
    write a tool, and it cannot run a tool that is not attached to it;
  * an attached tool is PINNED: the approved code and its sha256 are kept
    here and that copy is what runs. A later Forge save under the same name
    (save_tool overwrites by name) changes nothing for the agent;
  * every tool runs in the analyst sandbox (vault_analyst.execute_pandas_code:
    data folders only, no writes, no network, no pickle — see
    tests/test_sandbox_escapes.py).

Storage: ``<vault>/agent_profiles.json`` (settings, profiles, tool requests),
written atomically; listed in both of the vault's exclusion lists so a vault
search never returns it. No pickle. Toolkit-free; the Qt view is
council_qt/tabs/agent_creator.py.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

STORE_NAME = "agent_profiles.json"
STORE_VERSION = 1

ATTACH_MODES = ("approve", "automatic")

#: The read-only built-ins a profile may pick from. write_tool, run_app_tool
#: and list_app_tools are deliberately absent (see the module docstring).
BUILTIN_TOOLS: Dict[str, str] = {
    "list_files": "List the files in the data folder.",
    "search_files": "Find which files mention some text.",
    "read_local_file": "Read a text file from the data folder.",
    "run_pandas_analysis": "Run a read-only pandas snippet on the data.",
    "query_memory": "Look things up in the Council's memory.",
    "graph_find": "Find a person, part or project in the knowledge graph.",
    "graph_neighbors": "What a person, part or project is linked to, with sources.",
}
NEVER_OFFERED = frozenset({"write_tool", "run_app_tool", "list_app_tools"})

DRAFTER_ROLE = "coder"
DEFAULT_REVIEWERS = ("judge", "skeptic")
REVISION_ROUNDS = 1

_LOCK = threading.Lock()

ChatFn = Callable[..., str]   # chat(role, messages, *, max_tokens, json_schema)


# ── records ──────────────────────────────────────────────────────────────
@dataclass
class AttachedTool:
    name: str                       # the app-built tool's name
    entry: str                      # its entry function
    description: str
    code: str                       # the PINNED, approved code
    sha256: str
    params: Dict[str, str] = field(default_factory=dict)
    approved_by: str = "user"       # user | automatic
    request_id: str = ""
    attached_ts: float = field(default_factory=time.time)


@dataclass
class AgentProfile:
    id: str
    name: str
    description: str = ""
    model_role: str = "intern"
    connected_roles: List[str] = field(default_factory=lambda: list(DEFAULT_REVIEWERS))
    builtin_tools: List[str] = field(default_factory=lambda: ["list_files",
                                                              "read_local_file"])
    tools: List[AttachedTool] = field(default_factory=list)
    max_steps: int = 6
    instructions: str = ""
    created_ts: float = field(default_factory=time.time)
    updated_ts: float = field(default_factory=time.time)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "AgentProfile":
        known = set(AgentProfile.__dataclass_fields__)
        d = {k: v for k, v in d.items() if k in known}
        d["tools"] = [AttachedTool(**{k: v for k, v in t.items()
                                      if k in AttachedTool.__dataclass_fields__})
                      for t in d.get("tools", [])]
        d["builtin_tools"] = [t for t in d.get("builtin_tools", [])
                              if t in BUILTIN_TOOLS]
        return AgentProfile(**d)


@dataclass
class Review:
    role: str
    approve: bool
    concerns: List[str] = field(default_factory=list)
    round: int = 1
    error: str = ""


@dataclass
class ToolRequest:
    id: str
    profile_id: str
    description: str
    status: str = "drafting"        # drafting | reviewing | waiting | attached | rejected | failed
    tool_name: str = ""
    entry: str = ""
    code: str = ""
    test: str = ""                  # passed | needs arguments (not run) | the error
    reviews: List[Review] = field(default_factory=list)
    message: str = ""
    decided_by: str = ""            # user | automatic
    created_ts: float = field(default_factory=time.time)
    decided_ts: float = 0.0

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "ToolRequest":
        known = set(ToolRequest.__dataclass_fields__)
        d = {k: v for k, v in d.items() if k in known}
        d["reviews"] = [Review(**r) for r in d.get("reviews", [])]
        return ToolRequest(**d)

    @property
    def all_approved(self) -> bool:
        last = _last_round(self.reviews)
        return bool(last) and all(r.approve and not r.error for r in last)

    @property
    def test_ok(self) -> bool:
        return self.test in ("passed", "needs arguments (not run)")


def _last_round(reviews: List[Review]) -> List[Review]:
    if not reviews:
        return []
    top = max(r.round for r in reviews)
    return [r for r in reviews if r.round == top]


def sha256(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def entry_params(code: str) -> Dict[str, str]:
    """The entry function's parameters as a schema the agent is shown:
    {'column': 'str (required)', 'limit': 'optional, default 10'}."""
    try:
        fn = next(n for n in ast.parse(code).body if isinstance(n, ast.FunctionDef))
    except Exception:
        return {}
    args = fn.args.args
    defaults = [None] * (len(args) - len(fn.args.defaults)) + list(fn.args.defaults)
    out = {}
    for a, d in zip(args, defaults):
        ann = ast.unparse(a.annotation) if a.annotation is not None else "value"
        out[a.arg] = (f"{ann} (required)" if d is None
                      else f"{ann} (optional, default {ast.unparse(d)})")
    return out


# ── the store ────────────────────────────────────────────────────────────
def _vault_root(vault: Any = None) -> Path:
    if vault is not None:
        return Path(vault)
    from council_core import paths
    return Path(paths.vault_dir())


class ProfileStore:
    """``<vault>/agent_profiles.json``. Every write is tmp + replace; a file
    that cannot be parsed is reported (ProfileStoreDamaged) and never
    overwritten — the Specialists store's habit of replacing an unreadable
    file with defaults is exactly what not to copy."""

    def __init__(self, vault: Any = None) -> None:
        self.vault = _vault_root(vault)
        self.path = self.vault / STORE_NAME

    def _read(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"version": STORE_VERSION, "settings": {"tool_attach": "approve"},
                    "profiles": [], "requests": []}
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                raise ValueError("not an object")
            return d
        except Exception as exc:
            raise ProfileStoreDamaged(
                f"{self.path} could not be read ({exc}); it was left as it is") from exc

    def _write(self, d: Dict[str, Any]) -> None:
        d["version"] = STORE_VERSION
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def _update(self, fn: Callable[[Dict[str, Any]], Any]) -> Any:
        with _LOCK:
            d = self._read()
            out = fn(d)
            self._write(d)
            return out

    # settings
    def tool_attach_mode(self) -> str:
        mode = self._read().get("settings", {}).get("tool_attach", "approve")
        return mode if mode in ATTACH_MODES else "approve"

    def set_tool_attach_mode(self, mode: str) -> None:
        if mode not in ATTACH_MODES:
            raise ValueError(mode)
        self._update(lambda d: d.setdefault("settings", {}).__setitem__("tool_attach", mode))

    # profiles
    def profiles(self) -> List[AgentProfile]:
        return [AgentProfile.from_dict(p) for p in self._read().get("profiles", [])]

    def get(self, pid: str) -> Optional[AgentProfile]:
        return next((p for p in self.profiles() if p.id == pid), None)

    def save(self, profile: AgentProfile) -> AgentProfile:
        bad = [t for t in profile.builtin_tools if t not in BUILTIN_TOOLS]
        if bad:
            raise ValueError(f"not an offered tool: {bad}")
        profile.updated_ts = time.time()

        def put(d):
            items = [p for p in d.get("profiles", []) if p.get("id") != profile.id]
            items.append(asdict(profile))
            d["profiles"] = items
        self._update(put)
        return profile

    def create(self, name: str, **kw) -> AgentProfile:
        return self.save(AgentProfile(id=str(uuid.uuid4()), name=name.strip() or "Agent", **kw))

    def delete_profile(self, pid: str) -> None:
        """Removes the profile record only (no tool files, no user data)."""
        self._update(lambda d: d.__setitem__(
            "profiles", [p for p in d.get("profiles", []) if p.get("id") != pid]))

    def detach_tool(self, pid: str, tool_name: str) -> None:
        prof = self.get(pid)
        if prof is None:
            raise KeyError(pid)
        prof.tools = [t for t in prof.tools if t.name != tool_name]
        self.save(prof)

    # tool requests
    def requests(self, profile_id: Optional[str] = None) -> List[ToolRequest]:
        out = [ToolRequest.from_dict(r) for r in self._read().get("requests", [])]
        return [r for r in out if profile_id is None or r.profile_id == profile_id]

    def get_request(self, rid: str) -> Optional[ToolRequest]:
        return next((r for r in self.requests() if r.id == rid), None)

    def put_request(self, req: ToolRequest) -> ToolRequest:
        def put(d):
            items = [r for r in d.get("requests", []) if r.get("id") != req.id]
            items.append(asdict(req))
            d["requests"] = items
        self._update(put)
        return req

    def approve(self, rid: str, *, by: str = "user") -> AgentProfile:
        """Attach the request's tool to its profile, pinning the reviewed
        code. Only a request whose code passed the sandbox can be attached."""
        req = self.get_request(rid)
        if req is None:
            raise KeyError(rid)
        if req.status != "waiting":
            raise ValueError(f"request is {req.status}, not waiting for a decision")
        if not req.code or not req.tool_name:
            raise ValueError("the council produced no tool to attach")
        import vault_analyst as va
        ok, why = va.validate_generated_code(req.code)
        if not ok:
            raise ValueError(f"the sandbox rejects this code: {why}")
        prof = self.get(req.profile_id)
        if prof is None:
            raise KeyError(req.profile_id)
        prof.tools = [t for t in prof.tools if t.name != req.tool_name]
        prof.tools.append(AttachedTool(
            name=req.tool_name, entry=req.entry, description=req.description,
            code=req.code, sha256=sha256(req.code), params=entry_params(req.code),
            approved_by=by, request_id=req.id))
        self.save(prof)
        req.status, req.decided_by, req.decided_ts = "attached", by, time.time()
        self.put_request(req)
        return prof

    def reject(self, rid: str) -> None:
        req = self.get_request(rid)
        if req is None:
            raise KeyError(rid)
        req.status, req.decided_by, req.decided_ts = "rejected", "user", time.time()
        self.put_request(req)


class ProfileStoreDamaged(RuntimeError):
    pass


# ── the council builds a tool ────────────────────────────────────────────
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {"approve": {"type": "boolean"},
                   "concerns": {"type": "array", "items": {"type": "string"},
                                "maxItems": 6}},
    "required": ["approve", "concerns"],
}

_REVIEW_PROMPT = """You are the {role} on a council reviewing a Python tool another model wrote.

The user asked for: {request}

The tool runs in a read-only sandbox: it can read files only inside the data
folder, cannot write, delete, use the network or run programs. Review whether
the code does what was asked, correctly, and whether it reads only what it
needs. Do not ask for features nobody requested.

Sandbox test: {test}

```python
{code}
```

Answer ONLY with JSON: {{"approve": true or false, "concerns": ["...", ...]}}
(concerns empty when you approve)."""


def default_chat(role: str, messages, *, max_tokens: int = 700,
                 json_schema: Optional[Dict[str, Any]] = None) -> str:
    import council_engine as ce
    return ce.local_chat(list(messages), temperature=0.2, num_predict=int(max_tokens),
                         role=role, json_schema=json_schema)


def _parse_review(role: str, reply: str, rnd: int) -> Review:
    try:
        m = re.search(r"\{.*\}", reply or "", re.S)
        d = json.loads(m.group(0) if m else reply)
        return Review(role=role, approve=bool(d.get("approve")),
                      concerns=[str(c) for c in (d.get("concerns") or [])][:6], round=rnd)
    except Exception:
        # An unreadable review is not an approval.
        return Review(role=role, approve=False, round=rnd,
                      error="the reviewer's answer could not be read",
                      concerns=[(reply or "")[:300]])


def build_tool(store: ProfileStore, profile_id: str, description: str, *,
               chat: ChatFn = default_chat,
               on_progress: Optional[Callable[[str], None]] = None) -> ToolRequest:
    """The council writes, reviews and (by the user's setting) attaches a tool
    for ``profile_id``. Returns the finished ToolRequest. Blocking: run it on
    a worker. Every model call goes through ``chat`` (local models only)."""
    import tool_forge

    prof = store.get(profile_id)
    if prof is None:
        raise KeyError(profile_id)
    description = (description or "").strip()
    if not description:
        raise ValueError("Describe the tool you want.")
    say = on_progress or (lambda _m: None)
    req = store.put_request(ToolRequest(id=str(uuid.uuid4()), profile_id=profile_id,
                                        description=description))
    reviewers = [r for r in (prof.connected_roles or list(DEFAULT_REVIEWERS))
                 if r != DRAFTER_ROLE] or list(DEFAULT_REVIEWERS)

    def draft(task: str) -> bool:
        say(f"The {DRAFTER_ROLE} is writing the tool…")
        ok, msg, name, code = tool_forge.generate_tool(
            task, lambda prompt: chat(DRAFTER_ROLE, [{"role": "user", "content": prompt}],
                                      max_tokens=700),
            description=description, author=f"council:{DRAFTER_ROLE}",
            vault_dir=store.vault)
        req.code, req.message = code or req.code, msg
        if not ok or not name:
            req.status = "failed"
            return False
        import app_built_tools as abt
        req.tool_name = name
        req.entry = abt.entry_function(code) or ""
        req.test = ("passed" if tool_forge._entry_has_no_required_args(code)
                    else "needs arguments (not run)")
        return True

    def review(rnd: int) -> None:
        req.status = "reviewing"
        store.put_request(req)
        for role in reviewers:
            say(f"The {role} is reviewing it (round {rnd})…")
            prompt = _REVIEW_PROMPT.format(role=role, request=description,
                                           test=req.test, code=req.code)
            try:
                reply = chat(role, [{"role": "user", "content": prompt}],
                             max_tokens=400, json_schema=REVIEW_SCHEMA)
                req.reviews.append(_parse_review(role, reply, rnd))
            except Exception as exc:              # noqa: BLE001
                req.reviews.append(Review(role=role, approve=False, round=rnd,
                                          error=f"review failed: {exc!r}"[:200]))
            store.put_request(req)

    if not draft(description):
        return store.put_request(req)
    review(1)
    rnd = 1
    while not req.all_approved and rnd <= REVISION_ROUNDS:
        concerns = [c for r in _last_round(req.reviews) for c in r.concerns if c]
        task = (description + "\n\nReviewers raised these concerns about the "
                "previous version; fix them:\n- " + "\n- ".join(concerns or ["(none given)"]))
        if not draft(task):
            return store.put_request(req)
        rnd += 1
        review(rnd)

    req.status = "waiting"
    store.put_request(req)
    if store.tool_attach_mode() == "automatic" and req.all_approved and req.test_ok:
        say("Every reviewer approved and the sandbox test passed; attaching.")
        store.approve(req.id, by="automatic")
        return store.get_request(req.id)
    say("The tool is waiting for your approval." if req.all_approved
        else "Reviewers raised concerns; the tool is waiting for your decision.")
    return req


# ── running a profile ────────────────────────────────────────────────────
def _graph_tools(vault: Path):
    from safe_agent import Tool

    def find(args, policy):
        from council_core import knowledge_graph as kgm
        with kgm.KnowledgeGraph(vault) as kg:
            return {"matches": kg.search(str(args.get("text") or ""),
                                         args.get("type") or None, limit=10)}

    def neighbors(args, policy):
        from council_core import knowledge_graph as kgm
        with kgm.KnowledgeGraph(vault) as kg:
            hits = kg.search(str(args.get("name") or ""), limit=1)
            if not hits:
                return {"error": "no such person, part or project in the graph"}
            out = []
            for n in kg.neighbors(hits[0]["id"]):
                other = n["other"] or {}
                out.append({"link": n["predicate"], "direction": n["direction"],
                            "status": n["status"], "other": other.get("name"),
                            "sources": [f"{e['path']} ({e['where']})"
                                        for e in n["evidence"][:3]]})
            return {"entity": hits[0], "links": out}

    return {
        "graph_find": Tool("graph_find", find, {"text": "str", "type": "PERSON|PART|PROJECT (optional)"},
                           15.0, BUILTIN_TOOLS["graph_find"]),
        "graph_neighbors": Tool("graph_neighbors", neighbors, {"name": "str"},
                                15.0, BUILTIN_TOOLS["graph_neighbors"]),
    }


def run_attached_tool(tool: AttachedTool, args: Dict[str, Any],
                      allowed_folders: List[Path]) -> Dict[str, Any]:
    """Run the PINNED code, never the file on disk, through the sandbox."""
    import vault_analyst as va
    if sha256(tool.code) != tool.sha256:
        return {"error": "this tool's pinned code no longer matches its approval"}
    if not isinstance(args, dict):
        return {"error": "args must be an object"}
    call = (f"\n\n_ret = {tool.entry}(**{args!r})\n"
            "result_df = _ret if isinstance(_ret, (pd.DataFrame, pd.Series, dict)) "
            "else pd.DataFrame([{'result': _ret}])\n")
    df, msg = va.execute_pandas_code(tool.code + call, list(allowed_folders))
    out: Dict[str, Any] = {"message": msg}
    if df is not None:
        try:
            out["preview"] = df.head(20).to_dict(orient="records")
            out["shape"] = list(df.shape)
        except Exception:
            out["preview"] = str(df)[:2000]
    return out


def build_registry(profile: AgentProfile, vault: Path, file_root: Path):
    """A frozen registry holding ONLY this profile's tools."""
    from safe_agent import Tool, default_tools, AgentPolicy
    from tool_registry import ToolRegistry

    probe = AgentPolicy(allowed_tools=(), file_root=file_root, output_dir=file_root)
    base = default_tools(probe)
    base.update(_graph_tools(vault))
    reg = ToolRegistry()
    for name in profile.builtin_tools:
        if name in NEVER_OFFERED or name not in BUILTIN_TOOLS:
            continue
        reg.register(base[name])
    for t in profile.tools:
        if t.name in BUILTIN_TOOLS or t.name in NEVER_OFFERED:
            continue

        def fn(args, policy, _t=t):
            return run_attached_tool(_t, args, [policy.file_root])
        reg.register(Tool(t.name, fn, dict(t.params), 60.0,
                          f"{t.description} (council-made tool)"))
    reg.freeze()
    return reg


def run_profile(store: ProfileStore, profile_id: str, goal: str, *,
                runner=None, on_step=None):
    """Run the agent on ``goal`` with exactly its own tools. Returns the
    safe_agent AgentRun."""
    from safe_agent import AgentPolicy, ConstrainedAgent, _DEFAULT_PREAMBLE

    prof = store.get(profile_id)
    if prof is None:
        raise KeyError(profile_id)
    try:
        import data_index
        file_root = Path(data_index.input_dir(store.vault))
        out_dir = Path(data_index.output_dir(store.vault))
    except Exception:
        file_root, out_dir = store.vault / "data_in", store.vault / "data_out"
    file_root.mkdir(parents=True, exist_ok=True)
    reg = build_registry(prof, store.vault, file_root)
    policy = AgentPolicy(allowed_tools=tuple(reg.names()), file_root=file_root,
                         output_dir=out_dir, max_steps=max(1, min(int(prof.max_steps), 20)))
    if runner is None:
        from agent_jobs_runner import LocalRunner
        runner = LocalRunner(role=prof.model_role)
    preamble = _DEFAULT_PREAMBLE + (f"\n\nYou are '{prof.name}'. {prof.instructions}".rstrip()
                                    if (prof.instructions or prof.name) else "")
    agent = ConstrainedAgent(runner, reg, policy, system_preamble=preamble)
    return agent.run(goal, on_step=on_step)
