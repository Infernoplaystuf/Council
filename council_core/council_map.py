"""
council_core.council_map — who in the council talks to whom, as a graph.

The Council Map tab draws this: every role, every helper agent, every source
of context, and every link between them, each link saying WHAT travels along
it and WHERE in the code it happens. On top of that it lays the live picture —
which model each role answers with, and which machines have which models — so
one graph answers "how does a turn actually flow" and "what is not connected
that could be".

THE GRAPH IS WRITTEN DOWN, NOT DISCOVERED
The links are read from the code by hand and cited (file:line), because the
interesting ones are not visible at runtime: a briefing patched into five
roles' context, a knowledge base that is built and then bypassed, a feedback
loop whose round count is fixed before the code that extends it runs. A
tracer would see the calls that happen; this map is just as much about the
calls that do not. When the wiring changes, change the table here — the
tests pin the facts that matter (the judge gets no vault evidence, the Qt
turn passes no tools) so a fix shows up as a failing test to update.

TWO FRONT ENDS, ONE MAP
The Tk shell (council_gui_engine.py) runs the whole pipeline: a pre-pass that
augments the question, the librarian briefing, the tools. The Qt Council tab
(council_qt/tabs/council.py) sends the typed text straight to
council_turn.run_turn. Every link says which front ends it is real in, and
the map is drawn for one of them at a time: a Tk-only link drawn on the Qt
map is a link the Qt app is MISSING.

Statuses, per front end:
  live      happens on every turn that reaches it
  partial   happens, but not always (one feedback round; skipped on a branch)
  broken    the code is there and never takes effect
  missing   the other front end has it; this one does not
  proposed  nothing does this yet — a suggested improvement, with the reason

No Qt, no network, no model here: `live_overlay` takes plain data (a slot
config, NodeStatus-like objects) so a test can hand it anything.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

FRONT_ENDS = ("qt", "tk")
FRONT_END_LABELS = {"qt": "Qt app (this window)", "tk": "Tk app (classic)"}

#: Node kinds, in legend order.
KINDS = ("io", "judge", "member", "agent", "supplier", "store", "model",
         "machine")
KIND_LABELS = {
    "io": "Question / answer", "judge": "Judge", "member": "Council member",
    "agent": "Helper agent", "supplier": "Context supplier",
    "store": "Memory / store", "model": "Model (live)",
    "machine": "Machine (live)",
}

#: Edge layers — the tab's filter checkboxes.
LAYERS = ("deliberation", "context", "tools", "memory", "network")
LAYER_LABELS = {
    "deliberation": "Deliberation", "context": "Vault & context",
    "tools": "Tools", "memory": "Memory", "network": "Models & machines",
}

STATUSES = ("live", "partial", "broken", "missing", "proposed")
STATUS_LABELS = {
    "live": "Live", "partial": "Partial", "broken": "Broken (never fires)",
    "missing": "Missing here (other app has it)",
    "proposed": "Proposed (would improve the council)",
}


@dataclass(frozen=True)
class Node:
    id: str
    label: str
    kind: str
    summary: str = ""
    cite: str = ""


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    data: str                       # what travels along the link
    layer: str
    qt: str = "missing"             # status on the Qt map
    tk: str = "missing"             # status on the Tk map
    note: str = ""                  # why it is partial / broken / proposed
    cite: str = ""
    both_ways: bool = False

    def status(self, front_end: str) -> str:
        return self.tk if front_end == "tk" else self.qt


@dataclass
class CouncilMap:
    nodes: Dict[str, Node] = field(default_factory=dict)
    edges: List[Edge] = field(default_factory=list)
    #: What the live overlay could not read, in words; "" when it all worked.
    notes: List[str] = field(default_factory=list)

    def add(self, node: Node) -> None:
        self.nodes.setdefault(node.id, node)

    def link(self, edge: Edge) -> None:
        if edge.src in self.nodes and edge.dst in self.nodes:
            self.edges.append(edge)

    def edges_of(self, node_id: str) -> List[Edge]:
        return [e for e in self.edges if node_id in (e.src, e.dst)]

    def visible_edges(self, front_end: str,
                      layers: Optional[Iterable[str]] = None,
                      statuses: Optional[Iterable[str]] = None) -> List[Edge]:
        layers = set(LAYERS if layers is None else layers)
        statuses = set(STATUSES if statuses is None else statuses)
        return [e for e in self.edges
                if e.layer in layers and e.status(front_end) in statuses]

    def gaps(self, front_end: str) -> List[Edge]:
        """Every link that is not simply live on this front end, worst first."""
        order = {"broken": 0, "missing": 1, "partial": 2, "proposed": 3}
        out = [e for e in self.edges if e.status(front_end) != "live"]
        return sorted(out, key=lambda e: (order.get(e.status(front_end), 9),
                                          e.layer, e.src, e.dst))


# ============================================================
# The written-down topology
# ============================================================

#: The panel members (council_core.model_slots.COUNCIL_ROLES minus the judge,
#: the writer and docs, which have their own places in the map).
MEMBERS = ("coder", "skeptic", "sage", "strategist", "intern", "artist")

_NODES: Tuple[Node, ...] = (
    Node("question", "Your question", "io",
         "What you typed. On the Tk app the pre-pass turns it into an "
         "AUGMENTED question (task memo, analyst result, vault matches) "
         "that every agent, the judge included, receives. On the Qt app it "
         "is the typed text only.",
         "council_gui_engine.py:1910-1935; council_qt/tabs/council.py:166-185"),
    Node("answer", "Final answer", "io",
         "The synthesizer's draft once the judge's critique says PASS (or "
         "the rounds run out).", "council_core/deliberation.py:858-895"),
    Node("judge", "Judge", "judge",
         "Routes the question to a panel, ranks the candidates, critiques "
         "the synthesis (PASS / NEEDS_WORK + REQUIRED_CHANGES). Deliberately "
         "given no vault, history or prior-session context.",
         "council_engine.py:6531-6700; council_engine.py:5608"),
    Node("writer", "Writer", "member",
         "The default synthesizer: writes the one answer from every "
         "candidate, rebuttal, the discussion, the judge's ranking and the "
         "previous critique.",
         "council_core/council_turn.py:57-70; council_core/deliberation.py:290-390"),
    Node("peasant", "Peasant", "member",
         "Cross-examines each candidate with two plain questions; may argue "
         "against the winner. Not part of rebuttal or cross-fire.",
         "council_core/deliberation.py:589-624, 796-826"),
    Node("coder", "Coder", "member",
         "Writes code and GUIs. On Tk it is a CoderAgent (write → run → fix, "
         "up to 8 tries) with tools.", "coder_agent.py; council_gui_engine.py:18737"),
    Node("skeptic", "Skeptic", "member",
         "Attacks the question. Given no vault and no history on purpose.",
         "council_engine.py:5591"),
    Node("sage", "Sage", "member",
         "Long-view answers. Has a knowledge base (SageAgent) that the "
         "deliberation never uses.", "sage_agent.py:311-332"),
    Node("strategist", "Strategist", "member", "Plans; full vault context.",
         "council_engine.py:5584"),
    Node("intern", "Intern", "member",
         "Fast first drafts. On Tk can research the web first.",
         "intern_agent.py; council_gui_engine.py:18744"),
    Node("artist", "Artist", "member", "Creative answers; no vault context.",
         "council_engine.py:5588"),
    Node("docs", "Docs", "member",
         "Answers from documentation servers in the Docs tab. Not on the "
         "council panel.", "council_core/docs_qa.py:69, 262"),
    Node("debate", "Debate floor", "store",
         "The shared transcript of a round: candidates, the peasant's "
         "questions, rebuttals and cross-fire. Members read it; the "
         "synthesizer reads all of it.",
         "council_core/deliberation.py:633-764"),
    Node("librarian", "Librarian", "agent",
         "Ranks raw RAG hits into an access list and briefs the panel; logs "
         "what the vault is missing; can add roles to the panel.",
         "council_gui_engine.py:4485, 18812-18870"),
    Node("analyst", "Analyst", "agent",
         "Writes pandas code from the question and runs it in a sandbox over "
         "the vault's data files.", "vault_analyst.py; council_gui_engine.py:2835, 18190"),
    Node("task_memo", "Task memo", "agent",
         "Condenses the question into a [TASK MEMO] note.",
         "task_memory.py; council_gui_engine.py:18169"),
    Node("vault", "Vault", "supplier", "Your documents and data files.",
         "council_core/paths.py"),
    Node("vault_rag", "Vault RAG", "supplier",
         "Semantic search over vault chunks.", "vault_rag.py; council_gui_engine.py:3640"),
    Node("vault_search", "Vault search", "supplier",
         "data_index / vault search: [VAULT MATCH], [FILE], [FOLDER] blocks.",
         "data_index.py; council_gui_engine.py:18256"),
    Node("tools", "Tools", "supplier",
         "run_python, vault_save/list/read/search, api_search, api_signature.",
         "council_gui_engine.py:3810-3905; council_core/council_turn.py:108"),
    Node("web", "Web research", "supplier", "crawl4ai pages for the intern.",
         "intern_agent.py"),
    Node("sage_kb", "Sage knowledge", "supplier",
         "The sage_knowledge base SageAgent.respond would inject.",
         "sage_agent.py:326-332"),
    Node("mcp_docs", "Doc servers (MCP)", "supplier",
         "Python package documentation served over MCP.",
         "council_core/docs_servers.py; council_core/mcp_client.py"),
    Node("role_memory", "Role memory", "store",
         "Each role's own memory, shared project memory, your profile, "
         "recent history and the prior-session summary — added to every "
         "respond() call.", "council_engine.py:5645-5770"),
    Node("wishlist", "Librarian wishlist", "store",
         "Gaps the vault should fill: low-confidence members and the "
         "librarian's WISHLIST_ENTRY lines.",
         "council_engine.py:6723; council_gui_engine.py:18832, 19104"),
    Node("council_memory", "Past deliberations", "store",
         "council_memory (record and retrieve past deliberations). Only "
         "safe_agent uses it.", "council_memory.py; safe_agent.py"),
)

_MEMBER_CONTEXT = {
    # role: (Tk vault context from ROLE_CONTEXT_PROFILES, librarian briefing)
    "writer": ("full", True), "coder": ("full", True), "sage": ("full", True),
    "strategist": ("full", True), "peasant": ("lite", True),
    "intern": ("none", False), "artist": ("none", False),
    "skeptic": ("none", False),
}


def _edges() -> List[Edge]:
    E = Edge
    out: List[Edge] = [
        # -- routing and the round --------------------------------------
        E("question", "judge", "the question, to route", "deliberation",
          "live", "live", cite="council_core/council_turn.py:222-226"),
        E("judge", "writer", "ranking + winner + critique + REQUIRED_CHANGES",
          "deliberation", "live", "live",
          cite="council_core/deliberation.py:768-830"),
        E("writer", "judge", "the synthesized draft, for critique",
          "deliberation", "live", "live",
          cite="council_core/deliberation.py:858"),
        E("judge", "answer", "PASS verdict → the answer", "deliberation",
          "live", "live", cite="council_core/deliberation.py:858-895"),
        E("peasant", "debate", "two questions per candidate; a challenge "
          "to the winner", "deliberation", "live", "live",
          cite="council_core/deliberation.py:589-624, 796-826"),
        E("debate", "writer", "every candidate, rebuttal and the discussion",
          "deliberation", "live", "live",
          cite="council_core/deliberation.py:290-390"),
        E("debate", "judge", "candidates + peasant questions + rebuttals + "
          "self-confidence, to rank", "deliberation", "live", "live",
          cite="council_core/deliberation.py:768"),
        # -- the judge's feedback -----------------------------------------
        E("judge", "debate", "critique + ranking into rebuttal and "
          "cross-fire", "deliberation", "proposed", "proposed",
          note="Rebuttal and cross-fire prompts never include the judge's "
               "ranking or critique, so members argue without knowing "
               "who is winning or why.",
          cite="council_core/deliberation.py:637-713"),
        # -- vault and context (Tk pre-pass) -------------------------------
        E("vault", "vault_rag", "chunks", "context", "missing", "live",
          cite="vault_rag.py"),
        E("vault", "vault_search", "file index", "context", "missing", "live",
          cite="data_index.py"),
        E("vault", "analyst", "data files", "context", "missing", "live",
          cite="vault_analyst.py"),
        E("vault_rag", "librarian", "raw chunks", "context", "missing",
          "live", cite="council_gui_engine.py:3640, 18661"),
        E("vault_search", "question", "[VAULT MATCH] / [FILE] / [FOLDER]",
          "context", "missing", "live",
          cite="council_gui_engine.py:18256, 18354"),
        E("analyst", "question", "[ANALYST RESULT]", "context", "missing",
          "live", cite="council_gui_engine.py:18190"),
        E("task_memo", "question", "[TASK MEMO]", "context", "missing", "live",
          cite="council_gui_engine.py:18169"),
        E("question", "task_memo", "the typed question", "context",
          "missing", "live", cite="council_gui_engine.py:18169"),
        E("question", "analyst", "the typed question", "context", "missing",
          "live", cite="council_gui_engine.py:18190"),
        E("librarian", "wishlist", "WISHLIST_ENTRY lines", "context",
          "missing", "live", cite="council_gui_engine.py:18832"),
        E("librarian", "judge", "PANEL_ADD — adds roles to the panel",
          "context", "missing", "live", cite="council_gui_engine.py:18858"),
        E("librarian", "judge", "evidence, to check claims against",
          "context", "proposed", "proposed",
          note="The judge ranks and critiques with use_vault 'none' and no "
               "librarian briefing: it cannot tell a cited answer from a "
               "confident one.",
          cite="council_engine.py:5608; council_gui_engine.py:18879"),
        E("sage_kb", "sage", "knowledge-base passages", "context",
          "broken", "broken",
          note="Tk wraps sage_agent_obj.model, not the SageAgent, so "
               "SageAgent.respond's injection never runs; Qt sets "
               "sage_agent_obj = None.",
          cite="council_gui_engine.py:18908; council_core/council_turn.py:294"),
        E("mcp_docs", "docs", "documentation pages", "context", "live",
          "live", cite="council_core/docs_qa.py:2034-2052"),
        E("docs", "coder", "API docs for the code it writes", "context",
          "proposed", "proposed",
          note="The docs role reads real package documentation but the "
               "council's coder never asks it; the coder guesses APIs.",
          cite="council_core/docs_qa.py:262"),
        E("web", "intern", "researched pages", "context", "missing", "live",
          cite="intern_agent.py; council_gui_engine.py:18744"),
        # -- tools ---------------------------------------------------------
        E("tools", "coder", "tool results (run_python, vault_*, api_*)",
          "tools", "broken", "live",
          note="The Qt Tools toggle sets enable_tools but run_turn is "
               "passed tools=None, so ModelAgent.tools is empty.",
          cite="council_core/council_turn.py:108-109; "
               "council_qt/tabs/council.py:181-185"),
        E("tools", "intern", "tool results", "tools", "broken", "live",
          note="Same as the coder: no tools reach run_turn on Qt.",
          cite="council_core/council_turn.py:108-109"),
        E("tools", "writer", "PRIOR TOOL OUTPUTS", "tools", "missing", "live",
          cite="council_core/deliberation.py:290-390"),
        # -- memory --------------------------------------------------------
        E("debate", "wishlist", "low-confidence members (≤4/10) as gaps",
          "memory", "missing", "live",
          cite="council_core/deliberation.py:778-794; "
               "council_gui_engine.py:19104"),
        E("answer", "role_memory", "final answer + critique (writer and "
          "judge do not write)", "memory", "missing", "live",
          cite="council_gui_engine.py:20062-20135; council_engine.py:6012"),
        E("council_memory", "judge", "how similar questions were decided",
          "memory", "proposed", "proposed",
          note="Past deliberations are recorded nowhere the council reads; "
               "only safe_agent uses council_memory.",
          cite="council_memory.py; safe_agent.py"),
        E("answer", "council_memory", "this turn's verdict, for next time",
          "memory", "proposed", "proposed",
          note="Nothing records a deliberation for retrieval.",
          cite="council_memory.py"),
    ]

    for role in MEMBERS:
        out.append(E("judge", role, "picked for the panel", "deliberation",
                     "live", "live", cite="council_core/council_turn.py:57-70"))
        out.append(E(role, "debate", "candidate + confidence; rebuttal; "
                     "AGREE/DISAGREE/ADD", "deliberation", "live", "live",
                     cite="council_core/deliberation.py:530-764"))
        out.append(E("debate", role, "others' answers + the peasant's "
                     "questions", "deliberation", "live", "live",
                     cite="council_core/deliberation.py:633-713"))
    out.append(E("judge", "peasant", "picked for the panel", "deliberation",
                 "live", "live", cite="council_core/council_turn.py:57-70"))
    out.append(E("debate", "peasant", "each candidate, to question",
                 "deliberation", "live", "live",
                 cite="council_core/deliberation.py:589-624"))
    out.append(E("judge", "debate", "REQUIRED_CHANGES → a second round",
                 "deliberation", "partial", "broken",
                 note="Tk runs max_rounds=1 and range() is fixed before the "
                      "low-confidence branch raises it, so the critique "
                      "never reaches the members. Qt (max_rounds=2) gets one "
                      "feedback round, and REQUIRED_CHANGES is skipped in "
                      "exactly the low-confidence branch.",
                 cite="council_gui_engine.py:18957; "
                      "council_core/deliberation.py:516, 882-888"))

    for role, (vault, briefed) in _MEMBER_CONTEXT.items():
        if briefed:
            what = ("short peasant briefing" if role == "peasant"
                    else "librarian access-list briefing")
            out.append(E("librarian", role, what, "context", "missing", "live",
                         cite="council_gui_engine.py:18879-18889"))
        elif role in ("skeptic", "intern"):
            out.append(E("librarian", role, "evidence to attack / build on",
                         "context", "proposed", "proposed",
                         note=f"The {role} gets no vault context at all "
                              f"(use_vault 'none'), so it argues from the "
                              f"model's memory alone.",
                         cite="council_engine.py:5587-5592"))
    for role in ("judge", "writer") + MEMBERS + ("peasant",):
        out.append(E("role_memory", role, "own + project memory, profile, "
                     "history, prior session", "memory", "live", "live",
                     cite="council_engine.py:5645-5770"))
    return out


def static_map() -> CouncilMap:
    """The written-down topology, without anything live."""
    m = CouncilMap()
    for node in _NODES:
        m.add(node)
    for edge in _edges():
        m.link(edge)
    return m


# ============================================================
# The live overlay: models and machines
# ============================================================

THIS_PC = "machine:this-pc"


def model_label(path: str) -> str:
    """'ollama:llama3.1:8b' → 'llama3.1:8b'; a GGUF path → its file stem."""
    p = str(path or "").strip()
    if not p:
        return ""
    if p.lower().startswith("ollama:"):
        return p[len("ollama:"):]
    return Path(p.replace("\\", "/")).stem


def _ollama_name(path: str) -> str:
    p = str(path or "").strip()
    return p[len("ollama:"):] if p.lower().startswith("ollama:") else ""


def _same_model(a: str, b: str) -> bool:
    """Ollama names match with or without the ':latest' tag."""
    def norm(s: str) -> str:
        s = s.strip().lower()
        return s[:-len(":latest")] if s.endswith(":latest") else s
    return bool(a) and norm(a) == norm(b)


def _is_local_host(host: str) -> bool:
    h = str(host).lower()
    return any(t in h for t in ("localhost", "127.0.0.1", "[::1]", "//::1"))


def live_overlay(m: CouncilMap, slots: Any = None,
                 statuses: Sequence[Any] = (),
                 main_path: str = "") -> CouncilMap:
    """Add model and machine nodes to `m` (in place) and return it.

    `slots` is a model_slots.SlotConfig (or anything with .slots, .roles and
    .slot_for); `statuses` are dispatcher NodeStatus objects (host,
    reachable, installed_models, active_model_names). Either may be missing:
    the map then says so in `notes` and shows what it has.

    The council runs every role on THIS machine: a call leaves it only with
    COUNCIL_REMOTE_NODES=1 and an ollama: slot, and the Qt council builds no
    dispatcher at all. So role → model → this PC is live, and a remote
    machine that already has a slot's model is drawn as a PROPOSED link —
    there is no role → machine binding to send work there.
    """
    m.add(Node(THIS_PC, "This PC", "machine",
               "Where every council call runs today (local GGUF or "
               "localhost Ollama).",
               "council_engine.py:4016-4050, 139"))

    if slots is None:
        m.notes.append("Model slots could not be read; showing the "
                       "written-down map only.")
    else:
        roles = ("judge", "writer", "peasant") + MEMBERS + ("docs",)
        for name, slot in sorted(getattr(slots, "slots", {}).items()):
            path = (slot.resolved_path(main_path)
                    if hasattr(slot, "resolved_path") else
                    getattr(slot, "path", ""))
            label = model_label(path) or ("main model (not chosen yet)"
                                          if name == "main" else name)
            nid = f"model:{name}"
            m.add(Node(nid, label, "model",
                       f"Slot '{name}': {path or '(COUNCIL_GGUF_PATH unset)'}",
                       "council_core/model_slots.py:92-120"))
            m.link(Edge(nid, THIS_PC, "runs on", "network", "live", "live",
                        cite="council_engine.py:4016-4050"))
            for role in roles:
                try:
                    if slots.slot_for(role) == name:
                        m.link(Edge(role, nid, f"answers with slot '{name}'",
                                    "network", "live", "live",
                                    cite="council_core/model_slots.py:113"))
                except Exception:                         # noqa: BLE001
                    continue

    if not statuses:
        m.notes.append("No machines probed yet — press Refresh to ask "
                       "the Ollama hosts what they have.")
    for st in statuses or ():
        host = str(getattr(st, "host", "") or "")
        if not host:
            continue
        up = bool(getattr(st, "reachable", False))
        installed = list(getattr(st, "installed_models", []) or [])
        active = list(getattr(st, "active_model_names", []) or [])
        local = _is_local_host(host)
        nid = THIS_PC if local else f"machine:{host}"
        if not local:
            m.add(Node(nid, host.split("//")[-1], "machine",
                       f"{'up' if up else 'DOWN'} · {len(installed)} "
                       f"model(s) installed"
                       + (f" · running {', '.join(active)}" if active else "")
                       + (f"\nInstalled: {', '.join(installed)}"
                          if installed else ""),
                       "council_engine.py:4107-4140"))
        for name, slot in sorted(getattr(slots, "slots", {}).items()
                                 if slots is not None else ()):
            want = _ollama_name(getattr(slot, "path", ""))
            if not want or not any(_same_model(want, i) for i in installed):
                continue
            if local:
                continue                      # already drawn as 'runs on'
            m.link(Edge(f"model:{name}", nid,
                        "has this model installed — could share the load",
                        "network", "proposed", "proposed",
                        note="No role → machine binding exists (Slot has "
                             "name, path and n_ctx only) and the Qt council "
                             "builds no dispatcher, so this machine is "
                             "never asked.",
                        cite="council_core/model_slots.py:93-96; "
                             "council_qt/tabs/council.py:158"))
        if not local and up and not any(
                e.dst == nid for e in m.edges):
            m.link(Edge(nid, THIS_PC, "idle: no slot's model is installed "
                        "here", "network", "proposed", "proposed",
                        note="Install a slot's model here, or bind a role "
                             "to this machine, to use it.",
                        cite="council_core/apothecary.py"))
    return m


def gather(probe: bool = False, dispatcher: Any = None) -> CouncilMap:
    """The full map with whatever live data can be read. Never raises.

    `probe=True` asks every Ollama host what it has (blocking, network) —
    call it from a worker.
    """
    import os
    m = static_map()
    slots = None
    try:
        from . import model_slots
        slots = model_slots.current()
    except Exception as exc:                              # noqa: BLE001
        m.notes.append(f"Model slots: {exc!r}")
    statuses: List[Any] = []
    if probe:
        try:
            if dispatcher is None:
                import council_engine
                dispatcher = council_engine.build_dispatcher()
            statuses = list(dispatcher.probe_all() or [])
        except Exception as exc:                          # noqa: BLE001
            m.notes.append(f"Could not probe the machines: {exc!r}")
    return live_overlay(m, slots, statuses,
                        main_path=os.environ.get("COUNCIL_GGUF_PATH", ""))


# ============================================================
# Layout: a force-directed graph, the way Morphik draws its graph
# ============================================================

#: Where each kind is pulled toward, as a fraction of the canvas: the
#: question on the left, the judge in the middle with the members on a ring
#: around it, the debate floor and the writer on the way to the answer on the
#: right; context suppliers out on the left, memory along the top, models and
#: machines along the bottom.
_ANCHORS = {
    "agent": (0.2, 0.25), "supplier": (0.12, 0.6), "store": (0.55, 0.08),
    "model": (0.45, 0.93), "machine": (0.8, 0.93),
}
_FIXED = {"question": (0.03, 0.45), "judge": (0.42, 0.45),
          "debate": (0.62, 0.45), "writer": (0.8, 0.45),
          "answer": (0.97, 0.45)}
_NODE_ANCHORS = {"wishlist": (0.3, 0.06), "council_memory": (0.85, 0.12),
                 "role_memory": (0.6, 0.06), "docs": (0.25, 0.85),
                 "mcp_docs": (0.08, 0.92), "tools": (0.25, 0.7)}
_RING = (0.52, 0.47, 0.17, 0.36)        # centre x, y and radii, as fractions


def layout(m: CouncilMap, edges: Sequence[Edge], width: float = 1400.0,
           height: float = 900.0, iterations: int = 250,
           seed: int = 7) -> Dict[str, Tuple[float, float]]:
    """Positions for every node: Fruchterman-Reingold with each node pulled
    toward its anchor, so the picture keeps the same shape. Deterministic.

    The question, judge, debate floor, writer and answer are pinned: that is
    the spine a turn flows along, and the eye reads it left to right. The
    members are pinned on a ring around it, and the stores and docs on their
    anchors, so the picture has the same shape every time.
    """
    rng = random.Random(seed)
    ids = list(m.nodes)
    ring = [n for n in ids if m.nodes[n].kind == "member" and n not in _FIXED
            and n not in _NODE_ANCHORS]
    anchor: Dict[str, Tuple[float, float]] = {}
    for nid in ids:
        kind = m.nodes[nid].kind
        if nid in _FIXED:
            anchor[nid] = _FIXED[nid]
        elif nid in _NODE_ANCHORS:
            anchor[nid] = _NODE_ANCHORS[nid]
        elif nid in ring:
            a = -math.pi / 2 + 2 * math.pi * ring.index(nid) / len(ring)
            anchor[nid] = (_RING[0] + _RING[2] * math.cos(a),
                           _RING[1] + _RING[3] * math.sin(a))
        else:
            anchor[nid] = _ANCHORS.get(kind, (0.5, 0.5))
    # The spine, the ring and the named anchors stay where they are put; the
    # helpers, suppliers, models and machines settle around them.
    pinned = set(_FIXED) | set(_NODE_ANCHORS) | set(ring)
    pos = {nid: [anchor[nid][0] * width + (0 if nid in pinned
                                           else rng.uniform(-60, 60)),
                 anchor[nid][1] * height + (0 if nid in pinned
                                            else rng.uniform(-60, 60))]
           for nid in ids}
    if len(ids) < 2:
        return {k: (v[0], v[1]) for k, v in pos.items()}

    k = math.sqrt(width * height / len(ids)) * 0.6
    pairs = {(e.src, e.dst) for e in edges if e.src in pos and e.dst in pos}
    temp = width / 15
    for _ in range(iterations):
        disp = {n: [0.0, 0.0] for n in ids}
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                dx = pos[a][0] - pos[b][0]
                dy = pos[a][1] - pos[b][1]
                d = math.hypot(dx, dy) or 0.01
                f = k * k / d
                disp[a][0] += dx / d * f
                disp[a][1] += dy / d * f
                disp[b][0] -= dx / d * f
                disp[b][1] -= dy / d * f
        for a, b in pairs:
            dx = pos[a][0] - pos[b][0]
            dy = pos[a][1] - pos[b][1]
            d = math.hypot(dx, dy) or 0.01
            f = d * d / k * 0.3              # weak springs: anchors lead
            disp[a][0] -= dx / d * f
            disp[a][1] -= dy / d * f
            disp[b][0] += dx / d * f
            disp[b][1] += dy / d * f
        for nid in ids:
            if nid in pinned:
                continue
            pull = 0.25
            disp[nid][0] += (anchor[nid][0] * width - pos[nid][0]) * pull
            disp[nid][1] += (anchor[nid][1] * height - pos[nid][1]) * pull
            dx, dy = disp[nid]
            d = math.hypot(dx, dy) or 0.01
            step = min(d, temp)
            pos[nid][0] = min(width - 30, max(30, pos[nid][0] + dx / d * step))
            pos[nid][1] = min(height - 30, max(30, pos[nid][1] + dy / d * step))
        temp = max(1.0, temp * 0.97)
    return {nid: (p[0], p[1]) for nid, p in pos.items()}


def describe(m: CouncilMap, node_id: str, front_end: str) -> str:
    """A node's details as plain text: what it is, and every link in and out
    with what travels on it and its status on this front end."""
    node = m.nodes.get(node_id)
    if node is None:
        return ""
    lines = [node.label, KIND_LABELS.get(node.kind, node.kind), ""]
    if node.summary:
        lines += [node.summary, ""]
    if node.cite:
        lines += [f"Code: {node.cite}", ""]

    def block(title: str, edges: List[Edge], other) -> None:
        if not edges:
            return
        lines.append(title)
        for e in edges:
            st = e.status(front_end)
            lines.append(f"  {'⇄' if e.both_ways else '•'} "
                         f"{m.nodes[other(e)].label}: {e.data}"
                         f"  [{STATUS_LABELS[st]}]")
            if e.note and st != "live":
                lines.append(f"      {e.note}")
        lines.append("")

    mine = m.edges_of(node_id)
    block("Receives from", [e for e in mine if e.dst == node_id],
          lambda e: e.src)
    block("Sends to", [e for e in mine if e.src == node_id], lambda e: e.dst)
    return "\n".join(lines).rstrip()


def gaps_report(m: CouncilMap, front_end: str) -> str:
    """Everything not live on this front end, as a plain-text list."""
    lines = [f"What is missing on the {FRONT_END_LABELS[front_end]}:", ""]
    for e in m.gaps(front_end):
        st = e.status(front_end)
        lines.append(f"[{STATUS_LABELS[st]}] {m.nodes[e.src].label} → "
                     f"{m.nodes[e.dst].label}: {e.data}")
        if e.note:
            lines.append(f"    {e.note}")
        if e.cite:
            lines.append(f"    Code: {e.cite}")
    return "\n".join(lines)


__all__ = ["Node", "Edge", "CouncilMap", "FRONT_ENDS", "FRONT_END_LABELS",
           "KINDS", "KIND_LABELS", "LAYERS", "LAYER_LABELS", "STATUSES",
           "STATUS_LABELS", "MEMBERS", "THIS_PC", "static_map",
           "live_overlay", "gather", "layout", "describe", "gaps_report",
           "model_label"]
