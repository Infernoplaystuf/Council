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
interesting ones are not visible at runtime: low-confidence gaps collected
and never written anywhere, a knowledge base the turn never asks, a feedback
loop whose round count is fixed before the code that extends it runs. A
tracer would see the calls that happen; this map is just as much about the
calls that do not. When the wiring changes, change the table here — the
tests pin the facts that matter (the judge gets no vault evidence, the coder
gets its tools) so a fix shows up as a failing test to update.

The map is of the Qt app's council turn (council_qt/tabs/council.py →
council_core/council_turn.run_turn). Statuses:
  live      happens on every turn that reaches it
  partial   happens, but not always (one feedback round; skipped on a branch)
  broken    the code is there and never takes effect
  proposed  nothing does this yet — a suggested improvement, with the reason;
            where a reusable module already does the work, the cite names it

No Qt, no network, no model here: `live_overlay` takes plain data (a slot
config, NodeStatus-like objects) so a test can hand it anything.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

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
LAYERS = ("deliberation", "context", "tools", "memory", "network",
          "fanout")
LAYER_LABELS = {
    "deliberation": "Deliberation", "context": "Vault & context",
    "tools": "Tools", "memory": "Memory", "network": "Models & machines",
    "fanout": "Fan-out coding",
}

STATUSES = ("live", "partial", "broken", "proposed")
STATUS_LABELS = {
    "live": "Live", "partial": "Partial", "broken": "Broken (never fires)",
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
    status: str = "proposed"
    note: str = ""                  # why it is partial / broken / proposed
    cite: str = ""
    both_ways: bool = False


@dataclass
class CouncilMap:
    nodes: Dict[str, Node] = field(default_factory=dict)
    edges: List[Edge] = field(default_factory=list)
    #: What the live overlay could not read, in words.
    notes: List[str] = field(default_factory=list)

    def add(self, node: Node) -> None:
        self.nodes.setdefault(node.id, node)

    def link(self, edge: Edge) -> None:
        if edge.src in self.nodes and edge.dst in self.nodes:
            self.edges.append(edge)

    def edges_of(self, node_id: str) -> List[Edge]:
        return [e for e in self.edges if node_id in (e.src, e.dst)]

    def visible_edges(self, layers: Optional[Iterable[str]] = None,
                      statuses: Optional[Iterable[str]] = None) -> List[Edge]:
        layers = set(LAYERS if layers is None else layers)
        statuses = set(STATUSES if statuses is None else statuses)
        return [e for e in self.edges
                if e.layer in layers and e.status in statuses]

    def gaps(self) -> List[Edge]:
        """Every link that is not simply live, worst first."""
        order = {"broken": 0, "partial": 1, "proposed": 2}
        out = [e for e in self.edges if e.status != "live"]
        return sorted(out, key=lambda e: (order.get(e.status, 9), e.layer,
                                          e.src, e.dst))


# ============================================================
# The written-down topology
# ============================================================

#: The panel members on the ring (council_core.model_slots.COUNCIL_ROLES
#: minus the judge, the writer and docs, which have their own places).
MEMBERS = ("coder", "skeptic", "sage", "strategist", "intern", "artist")

_NODES: Tuple[Node, ...] = (
    Node("question", "Your question", "io",
         "What you typed. With 📚 Vault on, the Librarian first finds the "
         "vault passages that match it and hands them to the members; "
         "nothing adds a task memo or an analyst result yet.",
         "council_qt/tabs/council.py (CouncilActions.send)"),
    Node("answer", "Final answer", "io",
         "The synthesizer's draft once the judge's critique says PASS (or "
         "the rounds run out).", "council_core/deliberation.py:858-895"),
    Node("judge", "Judge", "judge",
         "Routes the question to a panel, ranks the candidates, critiques "
         "the synthesis (PASS / NEEDS_WORK + REQUIRED_CHANGES). Routes "
         "with no context, on purpose; ranks and critiques with the vault's "
         "evidence when 📚 Vault found some.",
         "council_engine.py (JudgeModel); council_core/deliberation.py"),
    Node("writer", "Writer", "member",
         "The default synthesizer: writes the one answer from every "
         "candidate, rebuttal, the discussion, the judge's ranking, the "
         "previous critique and any tool outputs.",
         "council_core/council_turn.py:57-70; "
         "council_core/deliberation.py:290-390"),
    Node("peasant", "Peasant", "member",
         "Cross-examines each candidate with two plain questions; may argue "
         "against the winner. Not part of rebuttal or cross-fire.",
         "council_core/deliberation.py:589-624, 796-826"),
    Node("coder", "Coder", "member",
         "Writes code and GUIs; may call tools when Tools is on. "
         "coder_agent.py has a write → run → fix loop the turn does not use.",
         "council_core/council_turn.py:108; coder_agent.py"),
    Node("skeptic", "Skeptic", "member",
         "Attacks the question. Given no vault passages and no history up "
         "front, on purpose; with Tools on it checks quotes and numbers.",
         "council_engine.py:5591"),
    Node("sage", "Sage", "member",
         "Long-view answers, from its own knowledge base (taught in the "
         "Agents tab) as well as the vault; logs what it admits it lacks.",
         "sage_agent.py; council_core/vault_context.py (sage_block)"),
    Node("strategist", "Strategist", "member", "Plans.",
         "council_engine.py:5584"),
    Node("intern", "Intern", "member",
         "Fast first drafts; may call tools when Tools is on.",
         "council_core/council_turn.py:108; intern_agent.py"),
    Node("artist", "Artist", "member", "Creative answers.",
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
         "Before each question (📚 Vault on), searches the vault for the "
         "passages that match it and hands them to the council as a VAULT "
         "CONTEXT block naming each file — for that question only. Semantic "
         "search when the vault has a semantic index, keyword search "
         "otherwise (no model loaded, nothing downloaded).",
         "council_core/vault_context.py"),
    Node("analyst", "Analyst", "agent",
         "For a numbers question: writes pandas code (the Coder's model), "
         "runs it over <vault>/data_in in a child process — the sandbox "
         "reads only that folder, never writes, and is stopped after 2 "
         "minutes — and gives every member and the Judge the figures, or "
         "tells them not to invent a number when it fails.",
         "council_core/analyst_step.py; vault_analyst.py"),
    Node("task_memo", "Task memo", "agent",
         "Before each question, condenses it into a short [TASK MEMO] — the "
         "goal, constraints and what to avoid — carrying the constraints "
         "forward when the question follows up the last one. Every member "
         "gets it for that question.",
         "task_memory.py; council_qt/tabs/council.py (task_memo)"),
    Node("vault", "Vault", "supplier", "Your documents and data files.",
         "council_core/paths.py"),
    Node("vault_rag", "Vault RAG", "supplier",
         "Semantic search over vault chunks (indexed from the Agents tab).",
         "vault_rag.py:650; council_core/rag_jobs.py"),
    Node("vault_search", "Vault search", "supplier",
         "data_index / vault search: matching files and folders.",
         "data_index.py; council_core/vault_search.py"),
    Node("tools", "Tools", "supplier",
         "27 tools, only with Tools on; each member gets its own short list "
         "(tool_kit.ROLE_TOOLS): code checks for the Coder, data lookups for "
         "the Intern, quote checks for the Skeptic and the Judge, charts for "
         "the Artist. Repeat calls in one question come from a cache.",
         "council_core/council_tools.py; council_core/tool_kit.py"),
    Node("web", "Web research", "supplier",
         "crawl4ai page research (intern_agent.py). Not part of the turn.",
         "intern_agent.py"),
    Node("sage_kb", "Sage knowledge", "supplier",
         "The sage_knowledge base SageAgent.respond injects.",
         "sage_agent.py:320-332"),
    Node("mcp_docs", "Doc servers (MCP)", "supplier",
         "Python package documentation served over MCP.",
         "council_core/docs_servers.py; council_core/mcp_client.py"),
    Node("role_memory", "Role memory", "store",
         "Each role's own memory, shared project memory, your profile, "
         "recent history and the prior-session summary — added to every "
         "respond() call.", "council_engine.py:5645-5770"),
    Node("wishlist", "Librarian wishlist", "store",
         "librarian_wishlist.md: what the vault should hold.",
         "council_engine.py:6723-6750"),
    Node("usage_log", "Model usage log", "store",
         "Every model call's role, model, machine, tokens, seconds and "
         "speed (never the text), kept for the weekly placement review.",
         "council_core/usage_log.py"),
    Node("apothecary", "Apothecary", "supplier",
         "The machines: hardware, installed models, status. (Credentials "
         "in the same file are never read by the review.)",
         "council_core/apothecary.py; council_core/placement.py"),
    Node("role_settings", "Role → model settings", "store",
         "model_slots.json: which model each role answers with.",
         "council_core/model_slots.py"),
    Node("fanout", "Fan-out coders", "agent",
         "Several coders on one task at once (the 🧩 Fan-out tab): the "
         "Judge's model splits it into units that never share a file, a "
         "coder works on each in its own copy — on this PC and the machines "
         "set up in Machines & roles — then the parts are combined, tested, "
         "fixed, reviewed, and written as a patch for you to apply.",
         "council_core/fanout.py; council_qt/tabs/fanout.py"),
    Node("council_memory", "Past decisions", "store",
         "Every deliberated question, its verdict and the start of its "
         "answer, across sessions (.council_memory/decisions.jsonl). "
         "Similar earlier questions go to the Judge and the Writer. Keyword "
         "matching: no model, nothing downloaded.",
         "council_core/past_decisions.py"),
    Node("answer_store", "Passed answers", "store",
         "Every answer the Judge passed, whole, with a fingerprint of the "
         "vault files it rested on (.council_memory/answers.jsonl). The same "
         "question asked again is answered at once, if you choose and those "
         "files are unchanged.",
         "council_core/answer_reuse.py"),
    Node("verdicts", "Who helps", "store",
         "One line per question: route, depth, panel, winner, scores and how "
         "much of each member's draft reached the answer "
         "(.council_usage/verdicts-*.jsonl). The weekly review reads it.",
         "council_core/verdict_log.py"),
    Node("code_jobs", "Code jobs", "agent",
         "The Code tab: a project's brief, code map and references; a plan "
         "you approve; steps done with read / find / edit-by-replacement / "
         "test / GUI-check tools on a git branch of its own, each checked "
         "by the app and committed; then you merge or discard.",
         "council_core/code_agent.py; council_core/project_tools.py; "
         "council_qt/tabs/code.py"),
    Node("bench", "Benchmark", "agent",
         "Asks a fixed set of questions through the council and measures "
         "calls, seconds per step, model loads and right answers, to "
         "compare before and after a change (Council Map ▸ Benchmark…).",
         "council_core/council_bench.py"),
)


def _edges() -> List[Edge]:
    E = Edge
    out: List[Edge] = [
        # -- routing and the round --------------------------------------
        E("question", "judge", "the question, to route", "deliberation",
          "live", cite="council_core/council_turn.py:222-226"),
        E("judge", "writer", "ranking + winner + critique + REQUIRED_CHANGES",
          "deliberation", "live", cite="council_core/deliberation.py:768-830"),
        E("writer", "judge", "the synthesized draft, for critique",
          "deliberation", "live", cite="council_core/deliberation.py:858"),
        E("judge", "answer", "PASS verdict → the answer", "deliberation",
          "live", cite="council_core/deliberation.py:858-895"),
        E("peasant", "debate", "two questions per candidate; a challenge "
          "to the winner", "deliberation", "live",
          cite="council_core/deliberation.py:589-624, 796-826"),
        E("debate", "writer", "every candidate, rebuttal and the discussion",
          "deliberation", "live", cite="council_core/deliberation.py:290-390"),
        E("debate", "judge", "candidates + peasant questions + rebuttals + "
          "self-confidence, to rank", "deliberation", "live",
          cite="council_core/deliberation.py:768"),
        E("debate", "peasant", "each candidate, to question", "deliberation",
          "live", cite="council_core/deliberation.py:589-624"),
        E("judge", "peasant", "picked for the panel", "deliberation", "live",
          cite="council_core/council_turn.py:57-70"),
        # -- the judge's feedback -----------------------------------------
        E("judge", "debate", "REQUIRED_CHANGES → a second round",
          "deliberation", "partial",
          note="run_turn allows two rounds, so the critique reaches the "
               "members once. The low-confidence branch raises max_rounds "
               "after range() is fixed, so its extra round never runs, and "
               "it is exactly that branch that skips REQUIRED_CHANGES.",
          cite="council_core/council_turn.py:186; "
               "council_core/deliberation.py:516, 882-888"),
        E("judge", "debate", "critique + ranking into rebuttal and "
          "cross-fire", "deliberation",
          note="Rebuttal and cross-fire prompts never include the judge's "
               "ranking or critique, so members argue without knowing "
               "who is winning or why.",
          cite="council_core/deliberation.py:637-713"),
        # -- vault and context ---------------------------------------------
        E("vault", "vault_rag", "chunks", "context", "live",
          cite="vault_rag.py:650; council_core/rag_jobs.py"),
        E("vault", "vault_search", "file index", "context", "live",
          cite="data_index.py"),
        E("vault", "analyst", "data files", "context", "live",
          cite="vault_analyst.py"),
        E("vault_rag", "librarian", "the passages that match the question",
          "context", "live",
          cite="council_core/vault_context.py (build); vault_rag.py"),
        E("vault_search", "question", "matching files and folders",
          "context",
          note="The question reaches the council without any vault matches.",
          cite="council_core/vault_search.py"),
        E("question", "analyst", "a question that reads as a computation",
          "context", "live",
          cite="council_core/analyst_step.py (looks_computational)"),
        E("analyst", "question", "[ANALYST RESULT]: computed figures — to "
          "every member and the Judge", "context", "live",
          cite="council_core/analyst_step.py; council_core/analyst_child.py"),
        E("question", "task_memo", "the question", "context", "live",
          cite="council_qt/tabs/council.py (task_memo); task_memory.py"),
        E("task_memo", "question", "[TASK MEMO]: goal, constraints, what "
          "to avoid — to every member", "context", "live",
          cite="council_core/vault_context.py (applied)"),
        E("librarian", "judge", "evidence from the vault, to check claims "
          "against — when ranking and critiquing only", "context", "live",
          cite="council_core/vault_context.py (evidence); "
               "council_core/deliberation.py (judge_evidence)"),
        E("librarian", "wishlist", "what the vault could not answer",
          "context", note="Nothing logs vault gaps during a turn.",
          cite="council_engine.py:6736"),
        E("sage_kb", "sage", "its domains, facts and your corrections "
          "for the question", "context", "live",
          cite="council_core/vault_context.py (sage_block)"),
        E("sage", "sage_kb", "the gaps it admits (GAP: …), after the "
          "answer", "memory", "live",
          cite="council_core/after_turn.py (log_sage_gap)"),
        E("mcp_docs", "docs", "documentation pages", "context", "live",
          cite="council_core/docs_qa.py:2034-2052"),
        E("mcp_docs", "coder", "DOCUMENTATION: the pages for a coding "
          "question", "context", "live",
          cite="council_core/docs_brief.py; docs_qa.docs_context"),
        E("mcp_docs", "writer", "DOCUMENTATION for a coding question",
          "context", "live", cite="council_core/docs_brief.py"),
        E("web", "intern", "researched pages", "context",
          note="intern_agent.py can research the web before drafting; the "
               "turn does not use it.", cite="intern_agent.py"),
        # -- tools ---------------------------------------------------------
        *[E("tools", role, "tools: " + ", ".join(names), "tools", "live",
            cite="council_core/tool_kit.py (ROLE_TOOLS); "
                 "council_core/council_turn.py (tools_for_role)")
          for role, names in _role_tools().items()],
        E("tools", "judge", "quote checks on every passage a candidate "
          "quotes, before ranking", "tools", "live",
          cite="council_core/tool_kit.py (judge_checks); "
               "council_core/deliberation.py"),
        E("tools", "usage_log", "every tool call (who, which, ok)", "tools",
          "live", cite="council_core/usage_log.py (record_tool)"),
        E("tools", "writer", "PRIOR TOOL OUTPUTS", "tools", "live",
          cite="council_core/deliberation.py:314-361"),
        # -- memory --------------------------------------------------------
        E("debate", "wishlist", "low-confidence members (≤40%) as gaps",
          "memory", "live",
          cite="council_core/deliberation.py (_low_conf_gaps); "
               "council_core/after_turn.py (log_gaps)"),
        E("answer", "role_memory", "what each member learned, and project "
          "facts (after the answer, in the background)", "memory", "live",
          cite="council_core/after_turn.py (learn); council_engine.py "
               "(update_role_memory_after_pass)"),
        E("council_memory", "judge", "how similar questions were decided, "
          "when ranking and critiquing", "memory", "live",
          cite="council_core/past_decisions.py (recall, block)"),
        E("council_memory", "writer", "how similar questions were decided",
          "memory", "live", cite="council_core/past_decisions.py"),
        E("answer", "council_memory", "this question, its verdict and the "
          "start of the answer", "memory", "live",
          cite="council_core/past_decisions.py (record)"),
        E("answer", "answer_store", "a passed answer, whole, and its vault "
          "files' fingerprints", "memory", "live",
          cite="council_core/answer_reuse.py (record)"),
        E("answer_store", "question", "the earlier answer to the same "
          "question, offered before the council runs", "memory", "live",
          cite="council_core/answer_reuse.py (find); council_qt/tabs/"
               "council.py (_offer_reuse)"),
        E("answer", "verdicts", "winner, scores and each member's influence",
          "memory", "live", cite="council_core/verdict_log.py (entry)"),
        E("verdicts", "judge", "who helps on each kind of question, as the "
          "weekly controller", "network", "live",
          cite="council_core/placement.py (WHO HELPS)"),
        E("bench", "question", "a fixed question set, measured", "memory",
          "live", cite="council_core/council_bench.py"),
        E("code_jobs", "judge", "the project and the task, to plan (the "
          "planner role)", "tools", "live",
          cite="council_core/code_agent.py (make_plan)"),
        E("code_jobs", "coder", "one plan step, with the project tools",
          "tools", "live", cite="council_core/code_agent.py (run_job)"),
    ]

    # -- fan-out coding ----------------------------------------------------
    out += [
        E("judge", "fanout", "the plan: units that never share a file",
          "fanout", "live", cite="council_core/fanout.py (make_plan, "
          "check_plan)"),
        E("coder", "fanout", "its model writes every unit", "fanout", "live",
          cite="council_core/fanout.py (run_unit)"),
        E("fanout", "judge", "the combined change and test result, for "
          "review", "fanout", "live", cite="council_core/fanout.py "
          "(_review)"),
    ]

    # -- the controller: the weekly placement review --------------------
    out += [
        E("usage_log", "judge", "a week of model usage, as the controller",
          "network", "live", cite="council_core/placement.py "
          "(build_report, ask_controller)"),
        E("apothecary", "judge", "machines' hardware and installed models",
          "network", "live", cite="council_core/placement.py "
          "(_registry_machines)"),
        E("judge", "role_settings", "role → model changes on this PC, "
          "applied when you approve", "network", "live",
          cite="council_core/placement.py (apply_role_changes); "
               "council_qt/widgets/placement_review.py"),
        E("judge", "apothecary", "which machine should hold which model",
          "network", "partial",
          note="A role move to a machine set up in Machines & roles can be "
               "applied; to any other machine it is advice. Installs and "
               "removals are commands for you to run: the Council never "
               "installs or removes models itself.",
          cite="council_core/placement.py (check); "
               "council_core/node_routing.py"),
    ]
    for role in ("judge", "writer") + MEMBERS + ("peasant",):
        out.append(E(role, "usage_log", "each call's model, machine, tokens "
                     "and seconds", "network", "live",
                     cite="council_engine.py (_record_stats); "
                          "council_core/usage_log.py"))

    for role in MEMBERS:
        out.append(E("judge", role, "picked for the panel", "deliberation",
                     "live", cite="council_core/council_turn.py:57-70"))
        out.append(E(role, "debate", "candidate + confidence; rebuttal; "
                     "AGREE/DISAGREE/ADD", "deliberation", "live",
                     cite="council_core/deliberation.py:530-764"))
        out.append(E("debate", role, "others' answers + the peasant's "
                     "questions", "deliberation", "live",
                     cite="council_core/deliberation.py:633-713"))
    # The VAULT CONTEXT block, gated per role by the engine
    # (ROLE_CONTEXT_PROFILES): full, the first 1,500 characters, or none.
    for role in ("writer", "coder", "sage", "strategist"):
        out.append(E("librarian", role, "VAULT CONTEXT: the matching "
                     "passages, in full", "context", "live",
                     cite="council_core/vault_context.py (applied); "
                          "council_engine.py (ROLE_CONTEXT_PROFILES)"))
    out.append(E("librarian", "peasant", "VAULT CONTEXT: the first 1,500 "
                 "characters", "context", "live",
                 cite="council_engine.py (ROLE_CONTEXT_PROFILES 'lite')"))
    for role, why in (
            ("skeptic", "The skeptic argues from the model's memory alone "
                        "(use_vault 'none', on purpose, so it cannot just "
                        "echo the vault); evidence would give it something "
                        "real to attack."),
            ("intern", "The intern drafts from the model's memory alone "
                       "(use_vault 'none', to stay fast).")):
        out.append(E("librarian", role, "the vault passages, to attack / "
                     "build on", "context", note=why,
                     cite="council_engine.py (ROLE_CONTEXT_PROFILES)"))
    for role in ("judge", "writer") + MEMBERS + ("peasant",):
        out.append(E("role_memory", role, "own + project memory, profile, "
                     "history, prior session", "memory", "live",
                     cite="council_engine.py:5645-5770"))
    return out


def _role_tools() -> Dict[str, Tuple[str, ...]]:
    """tool_kit.ROLE_TOOLS for the roles the map draws."""
    from .tool_kit import ROLE_TOOLS
    return {r: t for r, t in ROLE_TOOLS.items()
            if r in MEMBERS + ("writer", "peasant")}


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
                 main_path: str = "", routing: Any = None) -> CouncilMap:
    """Add model and machine nodes to `m` (in place) and return it.

    `slots` is a model_slots.SlotConfig (or anything with .slots, .roles and
    .slot_for); `statuses` are dispatcher NodeStatus objects (host,
    reachable, installed_models, active_model_names). Either may be missing:
    the map then says so in `notes` and shows what it has.

    A role runs on THIS machine unless node_routing binds it to another
    (routing on, the machine enabled): then its model → that machine is
    live. A machine that has a slot's model but no role bound to it is drawn
    as a PROPOSED link — it could share the load.
    """
    if routing is None:
        try:
            from . import node_routing
            routing = node_routing.current()
        except Exception:                                 # noqa: BLE001
            routing = None
    m.add(Node(THIS_PC, "This PC", "machine",
               "Runs every role that is not sent to another machine (an "
               "in-app .gguf model or this PC's Ollama).",
               "council_engine.py (_route_chat)"))

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
            m.link(Edge(nid, THIS_PC, "runs on", "network", "live",
                        cite="council_engine.py:4016-4050"))
            for role in roles:
                try:
                    if slots.slot_for(role) == name:
                        m.link(Edge(role, nid, f"answers with slot '{name}'",
                                    "network", "live",
                                    cite="council_core/model_slots.py:113"))
                except Exception:                         # noqa: BLE001
                    continue

    # Roles bound to another machine (node_routing): role → its model →
    # that machine.
    routed: Dict[str, List[str]] = {}            # url -> roles
    if routing is not None and getattr(routing, "routing_enabled", False):
        for role, binding in routing.roles.items():
            node = routing.nodes.get(binding.node)
            if node is not None and node.enabled:
                routed.setdefault(node.url, []).append(role)
    routed_urls = list(routed)

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
            if any(_url_key(u) == _url_key(host) for u in routed_urls):
                continue                      # drawn below as routed
            m.link(Edge(f"model:{name}", nid,
                        "has this model installed — could share the load",
                        "network", "proposed",
                        note="No role has a binding to this machine. Set "
                             "one up in Machines & roles to send a role's "
                             "calls here.",
                        cite="council_core/node_routing.py"))
        if not local and up and not any(
                e.dst == nid for e in m.edges) and not any(
                _url_key(u) == _url_key(host) for u in routed_urls):
            m.link(Edge(nid, THIS_PC, "idle: no slot's model is installed "
                        "here", "network", "proposed",
                        note="Install a slot's model here, or bind a role "
                             "to this machine, to use it.",
                        cite="council_core/apothecary.py"))
    # Fan-out workers run on this PC and on every enabled machine.
    m.link(Edge("fanout", THIS_PC, "a worker per unit", "fanout", "live",
                cite="council_core/fanout.py (worker_targets)"))
    if routing is not None and getattr(routing, "routing_enabled", False):
        for node in routing.nodes.values():
            if not node.enabled:
                continue
            nid = next((n for n in m.nodes if n.startswith("machine:")
                        and _url_key(n[len("machine:"):]) ==
                        _url_key(node.url)), f"machine:{node.url}")
            if nid not in m.nodes:
                m.add(Node(nid, node.name, "machine",
                           f"Set up in Machines & roles. {node.url}",
                           "council_core/node_routing.py"))
            m.link(Edge("fanout", nid, f"a worker per unit (up to "
                        f"{node.parallel} at once)", "fanout", "live",
                        cite="council_core/fanout.py (worker_targets)"))

    for url, roles in routed.items():
        nid = next((n for n in m.nodes if n.startswith("machine:")
                    and _url_key(n[len("machine:"):]) == _url_key(url)),
                   f"machine:{url}")
        name = next(n.name for n in routing.nodes.values() if n.url == url)
        probed = m.nodes.get(nid)
        # Replaces the probe's node (named by address) with the routing
        # name, keeping what the probe saw.
        m.nodes[nid] = Node(
            nid, name, "machine",
            f"Answers for: {', '.join(sorted(roles))} (Machines & roles). "
            f"{url}" + (f"\n{probed.summary}" if probed else ""),
            "council_core/node_routing.py")
        for role in roles:
            slot_name = slots.slot_for(role) if slots is not None else ""
            src = f"model:{slot_name}" if f"model:{slot_name}" in m.nodes \
                else role
            m.link(Edge(src, nid, f"runs on {name} for the {role}",
                        "network", "live",
                        cite="council_core/node_routing.py; "
                             "council_engine.py (_route_to_node)"))
    return m


def _url_key(url: str) -> str:
    u = str(url or "").strip().lower().rstrip("/")
    return u.split("//", 1)[-1]


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
_NODE_ANCHORS = {"usage_log": (0.28, 0.93), "apothecary": (0.95, 0.78),
                 "fanout": (0.8, 0.72),
                 "role_settings": (0.45, 0.97), "sage_kb": (0.66, 0.97),
                 "vault": (0.06, 0.72),
                 "wishlist": (0.3, 0.06), "council_memory": (0.85, 0.12),
                 "role_memory": (0.6, 0.06), "docs": (0.25, 0.85),
                 "mcp_docs": (0.08, 0.92), "tools": (0.25, 0.7),
                 "answer_store": (0.97, 0.2), "verdicts": (0.12, 0.97),
                 "bench": (0.03, 0.2), "code_jobs": (0.4, 0.85)}
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


# ============================================================
# How it works: the guided tour new users see first
# ============================================================

@dataclass(frozen=True)
class GuideStep:
    title: str
    body: str
    #: Node ids to light up; "kind:<kind>" lights every node of that kind
    #: (the models and machines are only known at runtime).
    nodes: Tuple[str, ...] = ()
    #: Where it happens, for anyone who wants to read the code.
    code: str = ""


GUIDE: Tuple[GuideStep, ...] = (
    GuideStep(
        "What this map shows",
        "Every circle and square is a part of the council, and every line is "
        "something one part hands to another — the label on the line says "
        "what.\n\n"
        "  • Red circle: the Judge. Orange circles: the council members.\n"
        "  • Purple: helper agents. Blue squares: places context comes from "
        "(your vault, tools, documentation). Grey squares: memory and "
        "records.\n"
        "  • Yellow: the AI models. Green boxes: the machines they run on. "
        "These two come from your real setup.\n\n"
        "Lines: grey works today; amber works only some of the time; red "
        "dashed is wired but never takes effect; green dashed does not exist "
        "yet and would make the council better.\n\n"
        "Hover a node to light up its lines. Click it for everything it "
        "sends and receives. Drag to move things, scroll to zoom. Press Next "
        "to follow one question through the council.\n\n"
        "'Role specs' shows what each agent needs — the minimum, "
        "recommended and best model and the memory each takes — against "
        "this PC, and which agents to give more power first."),
    GuideStep(
        "You ask a question",
        "You type in the ⚖ Council tab. That is the only way in: you talk to "
        "the council, never to one model directly.\n\n"
        "With Deliberate on, the whole council works on it (the next steps). "
        "With it off, the Writer answers alone — faster, no debate, no "
        "verdict. With 📚 Vault on, the Librarian first finds the passages "
        "in your vault that match the question and hands them to the "
        "members. The Tools switch lets the Coder and Intern run code and "
        "search the vault while they work.",
        ("question", "judge", "librarian"),
        "council_qt/tabs/council.py (CouncilActions.send) → "
        "council_core/council_turn.py (run_turn)"),
    GuideStep(
        "The Judge picks a panel",
        "The Judge reads the question and decides what kind it is — code, "
        "planning, a plain question — and so which members should answer. "
        "Only those members take part.",
        ("question", "judge") + MEMBERS + ("peasant",),
        "council_core/council_turn.py (PANEL_FOR_ROUTE)"),
    GuideStep(
        "Each member writes an answer",
        "Each member gets the question plus its own memory, the project "
        "memory, your profile and recent conversation (Role memory), writes "
        "a full answer, and ends it with how sure it is (0–100%) and what "
        "it is least sure of.\n\n"
        "By default they answer one at a time, and each later member also "
        "reads the answers before it. With 'Parallel members' on (Council "
        "tab), they all answer at once, each without seeing the others — "
        "faster when their models are on different machines, and the first "
        "drafts are independent.\n\n"
        "Every answer goes onto the Debate floor: the shared record of this "
        "round that every member and the Judge can read.",
        MEMBERS + ("role_memory", "debate"),
        "council_core/deliberation.py (DeliberationOrchestrator.run)"),
    GuideStep(
        "The Peasant asks, the members argue",
        "The Peasant asks two plain questions about every answer — the "
        "questions a sceptical outsider would ask.\n\n"
        "Then each member reads the other answers and the Peasant's "
        "questions and replies: it defends its answer, changes it, or agrees "
        "with someone else (rebuttal, then cross-fire). All of it goes onto "
        "the Debate floor.\n\n"
        "Not every question gets all of this. The Depth box (Auto by "
        "default) decides: a QUICK question (thanks, a short plain question) "
        "is answered by one member and checked by the Judge; a STANDARD one "
        "skips the cross-fire; a DEEP one (code, data, design, planning) "
        "gets everything. The cross-fire is also skipped when every draft "
        "already agrees with high confidence, stops after a turn in which "
        "nobody disagrees, and the Peasant asks about each turn in one go.",
        ("peasant", "debate") + MEMBERS,
        "council_core/deliberation.py (rebuttal, cross-fire)"),
    GuideStep(
        "Tools, when they are on",
        "With Tools on, a member can stop mid-answer and ask for a tool. "
        "Each has its own short list: the Coder checks, tests and searches "
        "code; the Intern looks at data files and computes from them; the "
        "Skeptic checks quotes and numbers; the Sage and Writer read parts "
        "of long files; the Strategist recalls past decisions and sees "
        "which machines are busy; the Artist draws charts. The result goes "
        "back to the member, and to the Writer later as 'prior tool "
        "outputs'. Members can leave each other notes, and a repeated "
        "lookup in the same question is answered from a cache.\n\n"
        "Before ranking, the app checks every passage a member quoted "
        "against your vault and tells the Judge which ones are not "
        "there.\n\n"
        "A tool that fails (code that runs too long, a missing file) is "
        "reported back to the model as a failure; it does not stop the "
        "council.",
        ("tools", "judge", "writer") + MEMBERS,
        "council_core/council_tools.py; council_core/tool_kit.py; "
        "ModelAgent.act in "
        "council_core/deliberation.py"),
    GuideStep(
        "Judge ranks, Writer writes, Judge checks",
        "The Judge ranks every answer from the Debate floor and names a "
        "winner. The Writer then writes the one answer you see, from all of "
        "it: the answers, the arguments, the ranking.\n\n"
        "The Judge checks that answer. PASS — it is shown to you. NEEDS WORK "
        "— the Judge lists what must change, and the Writer revises its "
        "answer against that list for the Judge to check again (no new "
        "debate). Only when the Judge rejects the whole approach does the "
        "full council go round again.",
        ("debate", "judge", "writer", "answer"),
        "council_core/deliberation.py (rank, synthesise, critique)"),
    GuideStep(
        "How a model is actually called",
        "Each role is not a separate program — it is a set of instructions "
        "given to a model. Which model a role uses is set in the 🇺🇸 Models "
        "tab (saved in model_slots.json). Several roles can share a model.\n\n"
        "Every call from every role ends in the same place. The council finds "
        "the role's model and either runs it inside the app (a .gguf file) "
        "or sends one request to Ollama — a model server — on THIS PC, and "
        "reads the reply as it streams back.\n\n"
        "The yellow nodes are your models; the line from each role shows "
        "which model it uses.",
        ("kind:model", "kind:member", "judge", "machine:this-pc"),
        "PersonalityModel.respond → LocalBackendSpec.generate → _route_chat "
        "(council_engine.py): _gguf_chat in the app, or _ollama_local → "
        "Ollama's /api/chat"),
    GuideStep(
        "How the machines talk to each other",
        "Other machines (a Raspberry Pi, a second PC) each run their own "
        "Ollama server. The council talks to them in two ways:\n\n"
        "  • Ollama's web API, port 11434, over your local network: 'which "
        "models do you have?' (/api/tags) and 'what is running?' (/api/ps). "
        "The Nodes tab asks every 15 seconds; this map asks when you press "
        "Refresh.\n"
        "  • SSH, port 22, from the 🔧 Apothecary: to set a Pi up and check "
        "on it every 60 seconds.\n\n"
        "Out of the box every answer is made on this PC. To use another "
        "machine, open 'Machines & roles': turn routing on, add the machine "
        "(or import it from the Apothecary), enable it, and bind a role to "
        "it. That role's calls then go to that machine's Ollama, with its "
        "own model; if the machine does not answer, the role answers here "
        "instead (or shows the error, if you chose 'fail') and the machine "
        "rests for a while. Each machine runs only as many calls at once as "
        "you allow.\n\n"
        "Any other address is refused: nothing leaves this PC except to a "
        "machine you registered and enabled.",
        ("kind:machine", "kind:model", "apothecary"),
        "council_core/node_routing.py; council_engine.py (_route_to_node, "
        "_ensure_localhost); apothecary_engine.py (SSH health monitor)"),
    GuideStep(
        "The weekly placement review",
        "Every model call is metered — which role, which model, which "
        "machine, how long, how fast (never what was said) — in the Model "
        "usage log.\n\n"
        "Once a week the Judge, acting as controller, reads that week of "
        "usage and the Apothecary's list of machines and models, and "
        "proposes changes: a different model for a slow role, a model to "
        "install where a machine sits idle.\n\n"
        "Nothing happens by itself. Changes for this PC wait for you to tick "
        "them and press Apply; installs and removals are commands for you to "
        "run. Open it with 'Placement review…'.",
        ("usage_log", "apothecary", "judge", "role_settings"),
        "council_core/usage_log.py; council_core/placement.py"),
    GuideStep(
        "Fan-out coding: several coders at once",
        "For a bigger coding job, the 🧩 Fan-out tab works like a team. The "
        "Judge's model splits the task into units, and no two units share a "
        "file, so the coders cannot get in each other's way. You approve the "
        "split.\n\n"
        "Each unit gets its own copy of the code and its own coder, all "
        "working at the same time — one on this PC and one on each machine "
        "set up in Machines & roles. Then the parts are put together and "
        "your tests run; if they fail, every coder sees the failure and "
        "fixes its own part. The Judge reviews the result, and you get a "
        "patch file to apply yourself — your folder is never changed.",
        ("fanout", "judge", "coder", "kind:machine"),
        "council_core/fanout.py (make_plan, run_job); "
        "council_qt/tabs/fanout.py"),
    GuideStep(
        "What is not connected yet",
        "The green dashed lines are the map's suggestions: web research "
        "for the Intern, and vault evidence for the Skeptic and the Intern "
        "(kept from them today on purpose). A red "
        "line, if any, is wired but never takes effect.\n\n"
        "Press 'What's missing' for the full list with the reason for each, "
        "or tick 'Only what is not live' to see just those lines.",
        ("web", "skeptic", "intern", "librarian"),
        "council_core/council_map.py (the table of links)"),
)


def guide_nodes(m: CouncilMap, step: GuideStep) -> List[str]:
    """The node ids a step lights up, with 'kind:' entries resolved."""
    out: List[str] = []
    for item in step.nodes:
        if item.startswith("kind:"):
            kind = item[len("kind:"):]
            out += [n for n, node in m.nodes.items() if node.kind == kind]
        elif item in m.nodes:
            out.append(item)
    return list(dict.fromkeys(out))


def guide_text(index: int) -> str:
    """One step, as the details panel shows it."""
    step = GUIDE[index]
    lines = [f"How it works — {index + 1} of {len(GUIDE)}", "", step.title,
             "", step.body]
    if step.code:
        lines += ["", f"In the code: {step.code}"]
    return "\n".join(lines)


def describe(m: CouncilMap, node_id: str) -> str:
    """A node's details as plain text: what it is, and every link in and out
    with what travels on it and its status."""
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
            st = e.status
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


def gaps_report(m: CouncilMap) -> str:
    """Everything not live, as a plain-text list."""
    lines = ["What is missing or not working:", ""]
    for e in m.gaps():
        st = e.status
        lines.append(f"[{STATUS_LABELS[st]}] {m.nodes[e.src].label} → "
                     f"{m.nodes[e.dst].label}: {e.data}")
        if e.note:
            lines.append(f"    {e.note}")
        if e.cite:
            lines.append(f"    Code: {e.cite}")
    return "\n".join(lines)


__all__ = ["Node", "Edge", "CouncilMap",
           "KINDS", "KIND_LABELS", "LAYERS", "LAYER_LABELS", "STATUSES",
           "STATUS_LABELS", "MEMBERS", "THIS_PC", "static_map",
           "live_overlay", "gather", "layout", "describe", "gaps_report",
           "model_label", "GuideStep", "GUIDE", "guide_nodes",
           "guide_text"]
