"""
council_core.readiness — is this machine ready for the council and its
coding jobs? One report, each line OK / WARN / FAIL with what to do.

QUICK (seconds, no model call):
    git installed; pytest importable; PySide6 can build a widget offscreen
    (the GUI check); an Ollama server answers; for each role the council
    uses — which model answers it and where, its context window, whether
    it calls tools natively (the coder should) and whether it thinks;
    panels whose members share one model; the escape-hatch switches that
    are set.

LIVE (a minute or two; one short call per distinct model):
    the model answers at all, and how fast; it follows a JSON schema (the
    Judge's ranking and the Peasant's questions depend on it); the coder
    calls a tool when it should (the Code tab depends on it).

Nothing is downloaded or installed; a missing model is named with the
command for the user to run themselves.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

ROLES = ("judge", "writer", "coder", "intern", "peasant", "skeptic", "sage",
         "strategist", "planner")
CODER_MIN_CTX = 16384


@dataclass
class Check:
    name: str
    status: str            # OK / WARN / FAIL / INFO
    detail: str
    fix: str = ""

    def line(self) -> str:
        out = f"[{self.status:<4}] {self.name}: {self.detail}"
        return out + (f"\n         → {self.fix}" if self.fix else "")


def _quick(vault_dir: Path) -> List[Check]:
    out: List[Check] = []
    out.append(Check("git", "OK", shutil.which("git"))
               if shutil.which("git") else
               Check("git", "FAIL", "not found",
                     "Install Git for Windows; code jobs work on git branches."))
    try:
        import pytest
        out.append(Check("pytest", "OK", pytest.__version__))
    except Exception:                                     # noqa: BLE001
        out.append(Check("pytest", "WARN", "not installed in the app's Python",
                         f"{sys.executable} -m pip install pytest — the code "
                         "jobs' test checks need it"))
    try:
        from .gui_check import check
        app_root = Path(__file__).resolve().parent.parent
        r = check(app_root, "council_qt.widgets.code_dialogs", "ProjectDialog",
                  timeout=60)
        out.append(Check("GUI check (offscreen)", "OK" if r.ok else "FAIL",
                         f"built a dialog with {len(r.tree)} widgets" if r.ok
                         else (r.errors or ["no detail"])[0][-300:]))
    except Exception as exc:                              # noqa: BLE001
        out.append(Check("GUI check (offscreen)", "FAIL", repr(exc)))

    try:
        from . import local_models
        host = local_models.ollama_host()
        up = local_models.ollama_reachable(host, timeout=2.0, max_age=0)
        out.append(Check("Ollama", "OK" if up else "WARN",
                         f"{host} {'answers' if up else 'does not answer'}",
                         "" if up else "Start Ollama, or give the roles "
                         "in-app .gguf models in the Models tab."))
    except Exception as exc:                              # noqa: BLE001
        out.append(Check("Ollama", "WARN", repr(exc)))

    try:
        import council_engine as ce
    except Exception as exc:                              # noqa: BLE001
        out.append(Check("engine", "FAIL", f"council_engine: {exc!r}"))
        return out
    role_models: Dict[str, str] = {}
    for role in ROLES:
        try:
            where, which = ce.model_key(role)
            slot = ce._slot_for_role(role)
            ctx = ce.effective_n_ctx(slot)
            native = ce.native_tools(role)
        except Exception as exc:                          # noqa: BLE001
            out.append(Check(f"role {role}", "WARN", repr(exc)))
            continue
        if not which:
            out.append(Check(f"role {role}", "WARN", "no model answers it",
                             "Set a model for it (or for 'main') in the "
                             "Models tab."))
            continue
        name = which.split(":", 1)[-1] if which.startswith("ollama:") \
            else Path(which).name
        role_models[role] = name
        where_txt = "this PC" if where == "local" else where
        detail = (f"{name} on {where_txt}, {ctx // 1024}k context, "
                  f"{'native' if native else 'emulated'} tool calls")
        status, fix = "OK", ""
        if role == "coder":
            if not native:
                status = "WARN"
                fix = ("A model with native tool calling (llama3.1, "
                       "gpt-oss) is much more reliable for code jobs.")
            if ctx < CODER_MIN_CTX:
                status = "WARN"
                fix = (fix + " " if fix else "") + (
                    f"Give the coder {CODER_MIN_CTX // 1024}k+ context "
                    "(COUNCIL_OLLAMA_NUM_CTX or the slot's setting).")
        out.append(Check(f"role {role}", status, detail, fix))
    try:
        from .verdict_log import shared_models
        for route, model, roles in shared_models(role_models):
            out.append(Check(f"panel {route}", "INFO",
                             f"{', '.join(roles)} all use {model}",
                             "Different model families debate better."))
    except Exception:                                     # noqa: BLE001
        pass
    for var, what in (("COUNCIL_STRUCTURED", "structured replies"),
                      ("COUNCIL_NATIVE_TOOLS", "native tool calls"),
                      ("COUNCIL_PRELOAD", "preloading while typing"),
                      ("COUNCIL_ANSWER_REUSE", "earlier-answer reuse"),
                      ("COUNCIL_OLLAMA_THINK", "thinking level override")):
        val = os.environ.get(var, "").strip()
        if val:
            out.append(Check(var, "INFO", f"set to {val!r} ({what})"))
    return out


PROBE_SCHEMA = {"type": "object",
                "properties": {"answer": {"type": "integer"},
                               "unit": {"type": "string"}},
                "required": ["answer", "unit"]}

CALC_TOOL = [{"name": "calc", "description": "exact arithmetic",
              "parameters": {"type": "object",
                             "properties": {"expr": {"type": "string"}},
                             "required": ["expr"]}}]


def _live(say: Callable[[str], None]) -> List[Check]:
    import council_engine as ce
    out: List[Check] = []
    seen: Dict[str, str] = {}
    for role in ROLES:
        try:
            _w, which = ce.model_key(role)
        except Exception:                                 # noqa: BLE001
            continue
        if not which or which in seen:
            continue
        seen[which] = role
        name = Path(which).name if not which.startswith("ollama:") else which
        say(f"asking {name} ({role})…")
        t0 = time.monotonic()
        try:
            text = ce.local_chat([{"role": "user", "content":
                                   "Reply with the single word: ready"}],
                                 role=role, num_predict=20, timeout=300)
            secs = time.monotonic() - t0
            out.append(Check(f"{name} answers", "OK" if "ready" in
                             text.lower() else "WARN",
                             f"{secs:.1f} s (load included): {text.strip()[:60]!r}"))
        except Exception as exc:                          # noqa: BLE001
            out.append(Check(f"{name} answers", "FAIL", repr(exc)[:300]))
            continue
        try:
            text = ce.local_chat([{"role": "user", "content":
                                   "How many metres in 3 kilometres? JSON."}],
                                 role=role, num_predict=60, timeout=300,
                                 json_schema=PROBE_SCHEMA)
            obj = json.loads(text)
            good = obj.get("answer") == 3000
            out.append(Check(f"{name} structured reply",
                             "OK" if good else "WARN",
                             f"{text.strip()[:80]}",
                             "" if good else "It returned the shape but not "
                             "the right value — fine for format, weak for "
                             "judging."))
        except Exception as exc:                          # noqa: BLE001
            out.append(Check(f"{name} structured reply", "WARN",
                             f"no valid JSON ({str(exc)[:120]})",
                             "Set COUNCIL_STRUCTURED=0 if the Judge's "
                             "rankings come back broken."))
    say("asking the coder to call a tool…")
    try:
        r = ce.chat_tools([{"role": "user", "content":
                            "What is 1234 * 5678? Use the calc tool."}],
                          CALC_TOOL, role="coder", num_predict=200,
                          timeout=300)
        calls = r.get("tool_calls") or []
        ok = bool(calls) and calls[0].get("name") == "calc"
        out.append(Check("coder tool call", "OK" if ok else "FAIL",
                         f"called {calls[0]['name']}({calls[0].get('arguments')})"
                         if calls else f"no tool call; said "
                                       f"{str(r.get('content'))[:80]!r}",
                         "" if ok else "The Code tab needs a coder that "
                         "calls tools: try llama3.1:8b or gpt-oss:20b."))
    except Exception as exc:                              # noqa: BLE001
        out.append(Check("coder tool call", "FAIL", repr(exc)[:300]))
    return out


def run(vault_dir: Optional[Path] = None, *, live: bool = False,
        say: Callable[[str], None] = lambda s: None) -> List[Check]:
    from . import paths
    vault_dir = Path(vault_dir) if vault_dir else paths.vault_dir()
    checks = _quick(vault_dir)
    if live:
        checks += _live(say)
    return checks


def report(checks: List[Check]) -> str:
    fails = sum(c.status == "FAIL" for c in checks)
    warns = sum(c.status == "WARN" for c in checks)
    head = ("READY" if not fails and not warns else
            f"{fails} problem(s), {warns} warning(s)")
    return "READINESS — " + head + "\n\n" + "\n".join(c.line() for c in checks)


__all__ = ["Check", "run", "report"]
