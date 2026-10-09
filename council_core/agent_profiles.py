"""
council_core.agent_profiles — named coding agents the user makes: who they
are, what they may use, how hard they may try, what they hand back.

A Specialist was a name, keywords and extra instructions. A profile is an
agent you can give a job:

    name, instructions   "Qt tab builder: new tabs follow council_qt/tabs/
                          council_map.py; every tab gets a test …"
    tools                which project tools it may use (a reviewer gets no
                          edit_file)
    role                 which model answers for it (a model_slots role:
                          coder, intern, judge, …)
    max_turns            its step budget per plan step
    output               "patch" — plans, works on a worktree branch, and
                          you merge it (council_core.code_agent); or
                          "report" — reads, runs the tests and the GUI
                          check, and writes a report; changes nothing
    references           extra documents only this agent reads

Kept in <vault>/.council_agents/<id>.json. BUILT_INS are the starting set
and are never overwritten on disk; copy one to make your own.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import project as pj

DIR_NAME = ".council_agents"
OUTPUTS = ("patch", "report")

ALL_TOOLS = ("list_files", "read_file", "find_symbol", "search_code",
             "edit_file", "create_file", "run_tests", "gui_check", "lint",
             "search_references", "step_done")
READ_ONLY = ("list_files", "read_file", "find_symbol", "search_code",
             "run_tests", "gui_check", "lint", "search_references")


@dataclass
class AgentProfile:
    id: str
    name: str
    instructions: str = ""
    tools: List[str] = field(default_factory=lambda: list(ALL_TOOLS))
    role: str = "coder"
    max_turns: int = 30
    output: str = "patch"
    references: List[str] = field(default_factory=list)
    built_in: bool = False


BUILT_INS: List[AgentProfile] = [
    AgentProfile(
        "developer", "Developer",
        "General development: small, checked steps that follow the "
        "project's existing patterns.", built_in=True),
    AgentProfile(
        "qt-tab-builder", "Qt tab builder",
        "You build and extend PySide6 tabs and dialogs. Copy the structure "
        "of an existing tab of the same kind (find one with search_code "
        "first): an Actions class the widget calls, work on threads via "
        "_to_ui, no tkinter. Every new widget gets a test that builds it "
        "offscreen, and gui_check must pass.", built_in=True),
    AgentProfile(
        "test-writer", "Test writer",
        "You write tests only: pytest, one behaviour per test, named for "
        "what it proves. Change production code only to make it testable, "
        "and say so.", built_in=True),
    AgentProfile(
        "bug-fixer", "Bug fixer",
        "You fix the bug described. First reproduce it in a failing test, "
        "then make the smallest change that fixes it. Never weaken or "
        "delete a test to make it pass.", built_in=True),
    AgentProfile(
        "reviewer", "Reviewer (report only)",
        "You review the code named in the task: read it, run the tests and "
        "the GUI check, and report concrete problems with file:line and why "
        "each matters. You change nothing.",
        tools=list(READ_ONLY), output="report", role="judge", built_in=True),
]


def _dir(vault_dir: Path) -> Path:
    return Path(vault_dir) / DIR_NAME


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40] \
        or "agent"


def list_profiles(vault_dir: Path) -> List[AgentProfile]:
    out = {p.id: p for p in BUILT_INS}
    d = _dir(vault_dir)
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            p = AgentProfile(**{k: v for k, v in data.items()
                                if k in AgentProfile.__dataclass_fields__})
        except (OSError, ValueError, TypeError):
            continue
        p.built_in = False
        out[p.id] = p
    return list(out.values())


def get(vault_dir: Path, profile_id: str) -> Optional[AgentProfile]:
    return next((p for p in list_profiles(vault_dir) if p.id == profile_id),
                None)


def save(vault_dir: Path, profile: AgentProfile) -> AgentProfile:
    """Write a user profile. A built-in's id gets a "-custom" copy."""
    p = AgentProfile(**asdict(profile))
    if not p.id or any(b.id == p.id for b in BUILT_INS):
        p.id = slug(p.name) + ("-custom" if any(
            b.id == slug(p.name) for b in BUILT_INS) else "")
    p.built_in = False
    p.tools = [t for t in p.tools if t in ALL_TOOLS] or list(READ_ONLY)
    if p.output not in OUTPUTS:
        p.output = "patch"
    if p.output == "report":
        p.tools = [t for t in p.tools if t in READ_ONLY]
    d = _dir(vault_dir)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{p.id}.json.tmp"
    tmp.write_text(json.dumps(asdict(p), indent=2), encoding="utf-8")
    tmp.replace(d / f"{p.id}.json")
    return p


def delete(vault_dir: Path, profile_id: str) -> bool:
    f = _dir(vault_dir) / f"{profile_id}.json"
    if f.exists():
        f.unlink()
        return True
    return False


def with_references(project: pj.Project, profile: AgentProfile) -> pj.Project:
    """The project as this agent sees it: its references added."""
    if not profile.references:
        return project
    q = pj.Project(**{**asdict(project), "gui_checks": project.gui_checks,
                      "references": list(project.references)
                      + list(profile.references)})
    return q


# ============================================================
# Report-only agents
# ============================================================

REPORT_SYSTEM = """You review a code project. Use the tools to read the \
code, run the tests and the GUI check. You may not change anything. When \
you have what you need, reply with the report in plain text: each finding \
with file:line, what is wrong, and why it matters; then what you checked."""


def run_report(vault_dir: Path, project: pj.Project, profile: AgentProfile,
               task: str, *,
               chat_tools: Optional[Callable[..., Dict[str, Any]]] = None,
               on_event: Optional[Callable[[str, str], None]] = None,
               should_stop: Optional[Callable[[], bool]] = None) -> str:
    """A read-only agent's report on the project folder as it is."""
    from . import code_agent as ca
    from . import code_map as cm
    from .project_tools import Workspace
    if chat_tools is None:
        import council_engine
        chat_tools = council_engine.chat_tools
    say = on_event or (lambda k, t: None)
    stop = should_stop or (lambda: False)
    proj = with_references(project, profile)
    root = Path(project.root)
    ws = Workspace(root, test_command=project.test_command,
                   gui_checks=project.gui_checks,
                   references=pj.references(proj) if proj.references else None,
                   code_map=lambda: cm.for_project(root),
                   out_dir=pj.project_dir(vault_dir, project) / "reports")
    tools = {n: f for n, f in ws.tools().items() if n in profile.tools
             and n in READ_ONLY}
    specs = [{"name": n, "description": f.help, "parameters": f.params}
             for n, f in tools.items()]
    messages = [{"role": "system", "content": REPORT_SYSTEM + "\n\n"
                 + profile.instructions + "\n\n"
                 + ca.context(vault_dir, proj, task)},
                {"role": "user", "content": f"TASK: {task}"}]
    for _ in range(profile.max_turns):
        if stop():
            break
        reply = chat_tools(messages, specs, role=profile.role,
                           num_predict=1800, timeout=900)
        calls = reply.get("tool_calls") or []
        if not calls:
            report = str(reply.get("content") or "").strip()
            say("note", report)
            return report
        messages.append({"role": "assistant", "content": "",
                         "tool_calls": [{"function": {
                             "name": c["name"],
                             "arguments": c.get("arguments") or {}}}
                             for c in calls]})
        for c in calls:
            fn = tools.get(c.get("name"))
            if fn is None:
                ok, msg = False, f"{c.get('name')} is not one of your tools"
            else:
                ok, msg, _p = fn(c.get("arguments") or {})
            say("tool", f"{c.get('name')} → {'ok' if ok else 'FAILED'}")
            messages.append({"role": "tool", "tool_name": c.get("name"),
                             "content": str(msg)[:ca.TOOL_RESULT_CHARS]})
        ca._trim(messages, ca._window_chars(profile.role))
    messages.append({"role": "user", "content": "Write the report now."})
    reply = chat_tools(messages, [], role=profile.role, num_predict=1800,
                       timeout=900)
    report = str(reply.get("content") or "").strip()
    say("note", report)
    return report


__all__ = ["AgentProfile", "BUILT_INS", "ALL_TOOLS", "READ_ONLY",
           "list_profiles", "get", "save", "delete", "run_report",
           "with_references"]
