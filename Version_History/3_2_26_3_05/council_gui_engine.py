# ============================================================
# council_gui_engine.py  —  v2
# ============================================================
# Conda env:
#   conda create -n council python=3.11 -y
#   conda activate council
# Optional (SSH): pip install paramiko
# Optional (Phase 3 STT mic): pip install sounddevice soundfile
# Optional (Phase 3 transcription): pip install faster-whisper
# ============================================================

from __future__ import annotations

import json as _json
import os
import queue
import re as _re
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

import council_engine as ce
import apothecary_engine as ae


# ============================================================
# Agent event types
# ============================================================

@dataclass
class AgentEvent:
    who: str
    kind: str   # "thought" | "action" | "observation" | "final" | "token" | "phase"
    text: str


@dataclass
class AgentContext:
    user_text: str
    shared: Dict[str, Any] = field(default_factory=dict)


ToolFn = Callable[[Dict[str, Any]], Tuple[bool, str, Dict[str, Any]]]
_TOOL_JSON_RE = _re.compile(r"\{.*\}", _re.DOTALL)


# ============================================================
# Helpers
# ============================================================

def _extract_tool_calls(text: str) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []
    for m in _TOOL_JSON_RE.finditer(text):
        blob = m.group(0).strip()
        try:
            obj = _json.loads(blob)
        except Exception:
            continue
        if isinstance(obj, dict) and "tool" in obj:
            calls.append({"tool": obj.get("tool"), "args": obj.get("args", {})})
        elif isinstance(obj, dict) and "tool_calls" in obj and isinstance(obj["tool_calls"], list):
            for tc in obj["tool_calls"]:
                if isinstance(tc, dict) and "tool" in tc:
                    calls.append({"tool": tc.get("tool"), "args": tc.get("args", {})})
    return calls


def _extract_code_block(text: str) -> str:
    m = _re.search(r"```(?:python)?\s*(.*?)```", text, flags=_re.DOTALL | _re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _safe_script_basename(name: str) -> str:
    name = (name or "").strip()
    name = _re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")
    if not name:
        name = "script"
    if len(name) > 60:
        name = name[:60]
    if name.lower().endswith(".py"):
        name = name[:-3]
    return name


def _parse_script_json(text: str) -> Tuple[str, str]:
    m = _re.search(r"\{.*\}", text, flags=_re.DOTALL)
    if not m:
        return "", ""
    try:
        obj = _json.loads(m.group(0))
    except Exception:
        return "", ""
    fn = str(obj.get("filename", "")).strip()
    code = str(obj.get("code", "")).strip()
    return fn, code


def peasant_cross_exam(peasant_model, *, candidate_role: str, candidate_text: str, user_text: str) -> str:
    prompt = (
        "You are the PEASANT.\n"
        "Ask at least TWO clarifying questions.\n"
        "Format strictly:\n"
        "Q1: ...\n"
        "Q2: ...\n"
        "(Optional) Q3: ...\n\n"
        f"User request:\n{user_text}\n\n"
        f"Candidate from {candidate_role}:\n{candidate_text}\n"
    )
    return peasant_model.respond(prompt)


def _looks_like_two_questions(text: str) -> bool:
    t = text.lower()
    return ("q1:" in t) and ("q2:" in t)


# ============================================================
# ModelAgent  (with streaming token callback)
# ============================================================

class ModelAgent:
    def __init__(
        self,
        display_name: str,
        personality_model: Any,
        *,
        tools: Dict[str, ToolFn] | None = None,
        enable_tools: bool = False,
        max_tool_steps: int = 3,
        token_callback: Optional[Callable[[str, str], None]] = None,
    ):
        self.display_name = display_name
        self.model = personality_model
        self.tools = tools or {}
        self.enable_tools = enable_tools
        self.max_tool_steps = max_tool_steps
        # token_callback(who, token) — called for each streamed token
        self.token_callback = token_callback

    def _compose_prompt(self, ctx: AgentContext) -> str:
        parts: List[str] = []
        cands = ctx.shared.get("candidates", {})
        rank = ctx.shared.get("judge_ranking", "")
        critique = ctx.shared.get("judge_critique", "")
        tool_payloads = ctx.shared.get("tool_payloads", {})
        discussion = ctx.shared.get("discussion_transcript", "")

        if cands:
            parts.append("CANDIDATE ANSWERS + PEASANT QUESTIONS + REBUTTALS:")
            for role, data in cands.items():
                parts.append(f"--- {role} ---")
                parts.append("ANSWER:")
                parts.append(data.get("answer", ""))
                if pq := data.get("peasant_q", ""):
                    parts.append(f"PEASANT QUESTIONS:\n{pq}")
                if rb := data.get("rebuttal", ""):
                    parts.append(f"REBUTTAL:\n{rb}")
                if disc := data.get("discussion", ""):
                    parts.append(f"DISCUSSION LOG:\n{disc}")
                parts.append("")

        if discussion:
            parts.append(f"FULL DISCUSSION (condensed):\n{discussion}\n")
        if rank:
            parts.append(f"JUDGE RANKING (JSON):\n{rank}\n")
        if critique:
            parts.append(f"JUDGE CRITIQUE:\n{critique}\n")
        if tool_payloads:
            parts.append("PRIOR TOOL OUTPUTS:")
            for k, v in tool_payloads.items():
                parts.append(f"- {k}: {str(v)[:900]}")
            parts.append("")

        parts.append(f"USER REQUEST:\n{ctx.user_text}")

        if self.enable_tools and self.tools:
            tool_list = ", ".join(sorted(self.tools.keys()))
            parts += [
                "",
                f"TOOLS AVAILABLE: {tool_list}",
                "To use a tool, output ONLY JSON: {\"tool\":\"name\",\"args\":{...}}",
                "Otherwise write a normal answer.",
            ]
        return "\n".join(parts)

    def _make_token_cb(self) -> Optional[Callable[[str], None]]:
        if self.token_callback is None:
            return None
        who = self.display_name
        cb = self.token_callback
        def _cb(token: str):
            cb(who, token)
        return _cb

    def act(self, ctx: AgentContext) -> List[AgentEvent]:
        events: List[AgentEvent] = []
        prompt = self._compose_prompt(ctx)

        if not (self.enable_tools and self.tools):
            events.append(AgentEvent(self.display_name, "thought", "Generating response…"))
            text = self.model.respond(prompt, token_callback=self._make_token_cb())
            return [AgentEvent(self.display_name, "final", text)]

        events.append(AgentEvent(self.display_name, "thought", "Calling model backend…"))
        text = self.model.respond(prompt, token_callback=self._make_token_cb())

        for _ in range(self.max_tool_steps):
            calls = _extract_tool_calls(text)
            if not calls:
                events.append(AgentEvent(self.display_name, "final", text))
                return events

            events.append(AgentEvent(self.display_name, "action", f"Tool calls requested ({len(calls)})."))
            obs_lines: List[str] = []
            payloads: Dict[str, Any] = {}

            for i, call in enumerate(calls, start=1):
                tool_name = str(call.get("tool", "")).strip()
                args = call.get("args", {})
                if tool_name not in self.tools:
                    obs_lines.append(f"[{i}] ERROR: unknown tool '{tool_name}'")
                    continue
                if not isinstance(args, dict):
                    obs_lines.append(f"[{i}] ERROR: args must be a dict")
                    continue
                ok, msg, payload = self.tools[tool_name](args)
                obs_lines.append(f"[{i}] {tool_name}: {'OK' if ok else 'FAIL'}\n{msg}")
                if payload:
                    payloads[f"{tool_name}_{i}"] = payload

            ctx.shared.setdefault("tool_payloads", {}).update(payloads)
            obs_text = "\n\n".join(obs_lines).strip() or "(no tool output)"
            events.append(AgentEvent(self.display_name, "observation", obs_text))

            followup = (
                f"TOOL RESULTS:\n{obs_text}\n\n"
                "Now produce the best possible answer (no tool JSON unless more tools needed)."
            )
            events.append(AgentEvent(self.display_name, "thought", "Calling model (post-tool)…"))
            text = self.model.respond(followup, token_callback=self._make_token_cb())

        events.append(AgentEvent(self.display_name, "final", text))
        return events


# ============================================================
# DeliberationOrchestrator  (yields events live via callback)
# ============================================================

class DeliberationOrchestrator:
    """
    Runs the full deliberation loop and emits events live through
    an `event_callback(AgentEvent)` as they occur.
    """

    def __init__(
        self,
        *,
        judge_model: Any,
        agents: Dict[str, ModelAgent],
        max_rounds: int = 2,
        debate_turns: int = 2,
        event_callback: Optional[Callable[[AgentEvent], None]] = None,
    ):
        self.judge = judge_model
        self.agents = agents
        self.max_rounds = max_rounds
        self.debate_turns = max(1, int(debate_turns))
        self.event_callback = event_callback or (lambda e: None)

    def _emit(self, event: AgentEvent) -> None:
        self.event_callback(event)

    def _phase(self, label: str) -> None:
        self._emit(AgentEvent("Orchestrator", "phase", f"▶ {label}"))

    def run(self, user_text: str, *, panel: List[str], synth: str = "writer") -> List[AgentEvent]:
        ctx = AgentContext(user_text=user_text)
        all_events: List[AgentEvent] = []

        def emit(ev: AgentEvent):
            all_events.append(ev)
            self._emit(ev)

        for r in range(self.max_rounds):
            self._phase(f"Round {r+1}/{self.max_rounds} — Candidate generation")

            candidates: Dict[str, Dict[str, str]] = {}
            discussion_lines: List[str] = []

            # 1) Candidates + Peasant cross-exam
            for key in panel:
                self._phase(f"{key.capitalize()} — drafting answer")
                evs = self.agents[key].act(ctx)
                for ev in evs:
                    emit(ev)
                answer = next((e.text for e in reversed(evs) if e.kind == "final"), "")
                candidates[key] = {"answer": answer, "peasant_q": "", "rebuttal": "", "discussion": ""}
                discussion_lines.append(f"{key.upper()} CANDIDATE:\n{answer}\n")

                if key != "peasant" and "peasant" in self.agents:
                    self._phase(f"Peasant — cross-examining {key}")
                    qtxt = peasant_cross_exam(
                        self.agents["peasant"].model,
                        candidate_role=key, candidate_text=answer, user_text=user_text,
                    )
                    if not _looks_like_two_questions(qtxt):
                        qtxt = peasant_cross_exam(
                            self.agents["peasant"].model,
                            candidate_role=key, candidate_text=answer, user_text=user_text,
                        )
                    candidates[key]["peasant_q"] = qtxt
                    ev = AgentEvent("Peasant", "observation", f"Questions about {key}:\n{qtxt}")
                    emit(ev)
                    discussion_lines.append(f"PEASANT → {key}:\n{qtxt}\n")

                ctx.shared["candidates"] = candidates
                ctx.shared["discussion_transcript"] = "\n".join(discussion_lines[-40:])

            # 2) Rebuttals
            self._phase("Rebuttal round")
            for key in panel:
                if key == "peasant" or key not in candidates:
                    continue
                other_roles = [r for r in candidates if r != key]
                debate_lines = [
                    "DEBATE CONTEXT:",
                    f"User request:\n{user_text}\n",
                    f"Your original answer ({key}):\n{candidates[key].get('answer','')}\n",
                ]
                if my_pq := candidates[key].get("peasant_q", ""):
                    debate_lines.append(f"Peasant questions about YOUR answer:\n{my_pq}\n")
                for rr in other_roles:
                    debate_lines.append(f"Other candidate ({rr}):\n{candidates[rr].get('answer','')}\n")
                    if pq := candidates[rr].get("peasant_q", ""):
                        debate_lines.append(f"Peasant questions about {rr}:\n{pq}\n")
                debate_lines += [
                    "INSTRUCTIONS:",
                    "- Write a rebuttal/improvement note.",
                    "- Explicitly state disagreements.",
                    "- Address Peasant questions.",
                    "- Propose concrete fixes.",
                    "- Keep under 12 bullet points.",
                ]
                extra_context = "\n".join(debate_lines)
                self._phase(f"{key.capitalize()} — rebuttal")
                rebuttal_text = self.agents[key].model.respond(
                    "Produce your rebuttal now.", extra_context=extra_context,
                    token_callback=self.agents[key]._make_token_cb(),
                )
                candidates[key]["rebuttal"] = rebuttal_text
                ev = AgentEvent(key.capitalize(), "observation", f"Rebuttal:\n{rebuttal_text}")
                emit(ev)
                discussion_lines.append(f"{key.upper()} REBUTTAL:\n{rebuttal_text}\n")
                ctx.shared["candidates"] = candidates
                ctx.shared["discussion_transcript"] = "\n".join(discussion_lines[-60:])

            # 3) Cross-fire
            self._phase(f"Cross-fire — {self.debate_turns} turns")
            for turn in range(1, self.debate_turns + 1):
                for key in panel:
                    if key == "peasant" or key not in candidates:
                        continue
                    extra_context = (
                        f"CROSS-FIRE CONTEXT — Turn {turn}/{self.debate_turns}\n\n"
                        "Rules:\n"
                        "- Write ONE short message.\n"
                        "- Include: AGREE: ... | DISAGREE: ... | ADD: ...\n"
                        "- Address Peasant questions about your answer.\n"
                        "- Keep under 10 lines.\n\n"
                        f"Discussion so far:\n{ctx.shared.get('discussion_transcript','')}\n"
                    )
                    self._phase(f"{key.capitalize()} — cross-fire T{turn}")
                    msg = self.agents[key].model.respond(
                        "Post your cross-fire message now.", extra_context=extra_context,
                        token_callback=self.agents[key]._make_token_cb(),
                    )
                    candidates[key]["discussion"] = (
                        candidates[key].get("discussion", "") + f"\nTURN {turn}:\n{msg}\n"
                    ).strip()
                    ev = AgentEvent(key.capitalize(), "observation", f"Cross-fire T{turn}:\n{msg}")
                    emit(ev)
                    discussion_lines.append(f"{key.upper()} CROSS-FIRE T{turn}:\n{msg}\n")

                    if "peasant" in self.agents:
                        self._phase(f"Peasant — questions after {key} T{turn}")
                        pq = peasant_cross_exam(
                            self.agents["peasant"].model,
                            candidate_role=f"{key} (T{turn})", candidate_text=msg, user_text=user_text,
                        )
                        if not _looks_like_two_questions(pq):
                            pq = peasant_cross_exam(
                                self.agents["peasant"].model,
                                candidate_role=f"{key} (T{turn})", candidate_text=msg, user_text=user_text,
                            )
                        pev = AgentEvent("Peasant", "observation", f"Cross-fire questions after {key} T{turn}:\n{pq}")
                        emit(pev)
                        discussion_lines.append(f"PEASANT → {key} T{turn}:\n{pq}\n")

                    ctx.shared["candidates"] = candidates
                    ctx.shared["discussion_transcript"] = "\n".join(discussion_lines[-80:])

            # 4) Judge ranks
            self._phase("Judge — ranking candidates")
            rank_json = self.judge.rank_candidates(user_text, candidates)
            ctx.shared["judge_ranking"] = rank_json
            ev = AgentEvent("Judge", "observation", f"Ranking:\n{rank_json}")
            emit(ev)

            # 5) Writer synthesizes
            self._phase("Writer — synthesizing final answer")
            synth_evs = self.agents[synth].act(ctx)
            for ev in synth_evs:
                emit(ev)
            synth_final = next((e.text for e in reversed(synth_evs) if e.kind == "final"), "")

            # 6) Judge critiques
            self._phase("Judge — critiquing synthesis")
            critique = self.judge.critique(user_text, synth_final, extra_context=f"Ranking:\n{rank_json}")
            ctx.shared["judge_critique"] = critique
            ev = AgentEvent("Judge", "observation", critique)
            emit(ev)

            if "Verdict: PASS" in critique:
                self._phase("✓ Verdict: PASS — deliberation complete")
                break

        return all_events


# ============================================================
# Vault search tool
# ============================================================

def _vault_search_impl(vault_dir: Path, query: str, *, max_files: int = 80) -> Dict[str, Any]:
    q = (query or "").strip()
    if not q:
        return {"query": q, "matches": []}
    qlow = q.lower()
    matches = []
    files = sorted(vault_dir.iterdir(), key=lambda p: p.name.lower())
    scanned = 0
    for p in files:
        if scanned >= max_files or not p.is_file():
            continue
        scanned += 1
        try:
            if p.stat().st_size > 250_000:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        tlow = text.lower()
        idx = tlow.find(qlow)
        if idx == -1:
            continue
        start = max(0, idx - 140)
        end = min(len(text), idx + 260)
        excerpt = text[start:end].replace("\n", " ")
        matches.append({"file": p.name, "excerpt": excerpt})
        if len(matches) >= 12:
            break
    return {"query": q, "matches": matches, "scanned": scanned}


def _make_tools(runner: ce.LocalRunner, librarian: ce.Librarian, vault_dir: Path) -> Dict[str, ToolFn]:
    def run_python(args: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        code = str(args.get("code", "")).strip()
        if not code:
            return False, "No code provided.", {}
        rc, out, err, path = runner.run_code(code, filename_hint=str(args.get("filename", "scratch.py")), timeout_s=int(args.get("timeout_s", 120)))
        return True, f"rc={rc}\n--- stdout ---\n{out}\n--- stderr ---\n{err}\nfile={path}", {"rc": rc, "stdout": out, "stderr": err, "path": str(path)}

    def vault_save(args: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        name = str(args.get("name", "note.txt")).strip() or "note.txt"
        content = str(args.get("content", ""))
        if not content:
            return False, "No content provided.", {}
        p = librarian.save_text(name, content)
        return True, f"Saved to vault as '{p.name}'.", {"path": str(p)}

    def vault_list(args: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        items = librarian.list_items()
        return True, "\n".join(items) if items else "(empty)", {"items": items}

    def vault_read(args: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        name = str(args.get("name", "")).strip()
        if not name:
            return False, "Provide {'name': 'file.txt'}", {}
        txt = librarian.read_text(name)
        return True, txt, {"name": name}

    def vault_search(args: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
        res = _vault_search_impl(vault_dir, str(args.get("query", "")))
        lines = [f"Vault search: {res.get('query','')}"]
        for m in res.get("matches", []):
            lines.append(f"- {m['file']}: {m['excerpt']}")
        if not res.get("matches"):
            lines.append("(no matches)")
        return True, "\n".join(lines), res

    return {"run_python": run_python, "vault_save": vault_save,
            "vault_list": vault_list, "vault_read": vault_read, "vault_search": vault_search}


# ============================================================
# App paths / config
# ============================================================

STORE_PASSWORDS = True

APP_DIR = Path.home() / ".council"
APP_DIR.mkdir(parents=True, exist_ok=True)

REGISTRY_PATH = APP_DIR / "node_registry.json"
VAULT_DIR = APP_DIR / "vault"
LOG_PATH = APP_DIR / "council.log"
WORKSPACE_DIR = APP_DIR / "workspace"
TMP_DIR = APP_DIR / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)
PINS_PATH = APP_DIR / "personality_backends.json"


# ============================================================
# Colour / tag constants for the transcript
# ============================================================

ROLE_COLORS = {
    "User":        "#4fc3f7",   # light blue
    "Judge":       "#ef9a9a",   # red-ish
    "Writer":      "#a5d6a7",   # green
    "Tech-Priest": "#ce93d8",   # purple
    "Intern":      "#ffe082",   # yellow
    "Peasant":     "#ffcc80",   # orange
    "Artist":      "#f48fb1",   # pink
    "Orchestrator":"#b0bec5",   # grey
    "Librarian":   "#80cbc4",   # teal
    "Apothecary":  "#bcaaa4",   # brown-ish
}

PHASE_COLOR   = "#78909c"
TOKEN_COLOR   = "#e0e0e0"
DEFAULT_COLOR = "#cfd8dc"


# ============================================================
# CouncilConsole  (main GUI)
# ============================================================

class CouncilConsole(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Council Console  v2  •  LOCAL + Pi")
        self.geometry("1300x950")
        self.configure(bg="#1e1e2e")

        self.ui_q: queue.Queue = queue.Queue()

        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.prior_session_id: Optional[str] = None
        self.convo_store = ce.ConversationStore(VAULT_DIR / "conversations")
        self.librarian = ce.Librarian(VAULT_DIR, LOG_PATH)
        self.runner = ce.LocalRunner(WORKSPACE_DIR)
        self.speech = ce.SpeechToText(model_size="base")
        self.dispatcher = ce.build_dispatcher()

        pins = ce.load_personality_pins(PINS_PATH)
        self.personalities = ce.build_personalities(
            pins=pins, vault_dir=VAULT_DIR, session_id=self.session_id,
            trace=True, dispatcher=self.dispatcher,
            prior_session_id=self.prior_session_id,
        )
        self._unpack_personalities()

        self.apoth = ae.Apothecary(
            registry_path=str(REGISTRY_PATH), store_passwords=STORE_PASSWORDS
        )

        self.current_script_name = "script"
        self._stream_buffers: Dict[str, str] = {}  # role -> partial streamed text
        self._node_refresh_id = None

        self._build_ui()
        self._apply_dark_theme()
        self.after(100, self._poll_ui_queue)
        self.after(2000, self._refresh_nodes_async)   # initial node probe

    def _unpack_personalities(self):
        self.judge      = self.personalities["judge"]
        self.writer     = self.personalities["writer"]
        self.peasant    = self.personalities["peasant"]
        self.intern     = self.personalities["intern"]
        self.techpriest = self.personalities["techpriest"]
        self.artist     = self.personalities["artist"]

    # ============================
    # Dark theme
    # ============================

    def _apply_dark_theme(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        bg, fg, sel = "#1e1e2e", "#cdd6f4", "#313244"
        abg = "#181825"  # frame/widget bg
        style.configure(".", background=bg, foreground=fg, fieldbackground=abg,
                         insertcolor=fg, troughcolor=abg, bordercolor=sel)
        style.configure("TNotebook", background=bg, borderwidth=0)
        style.configure("TNotebook.Tab", background=sel, foreground=fg, padding=[10, 4])
        style.map("TNotebook.Tab", background=[("selected", "#45475a")])
        style.configure("TFrame", background=bg)
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure("TButton", background="#313244", foreground=fg, relief="flat", padding=4)
        style.map("TButton", background=[("active", "#45475a")])
        style.configure("TCheckbutton", background=bg, foreground=fg)
        style.configure("TEntry", fieldbackground=abg, foreground=fg, insertcolor=fg)
        style.configure("TScrollbar", background=sel, troughcolor=abg)
        style.configure("Treeview", background=abg, foreground=fg, fieldbackground=abg,
                         rowheight=24)
        style.map("Treeview", background=[("selected", "#585b70")])
        style.configure("Treeview.Heading", background=sel, foreground=fg)
        self.configure(bg=bg)

    def _make_text(self, parent, **kwargs) -> tk.Text:
        defaults = dict(
            bg="#181825", fg="#cdd6f4", insertbackground="#cdd6f4",
            selectbackground="#585b70", relief="flat", bd=0,
            font=("Consolas", 10),
        )
        defaults.update(kwargs)
        return tk.Text(parent, **defaults)

    # ============================
    # UI construction
    # ============================

    def _build_ui(self):
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=6, pady=6)

        self._build_council_tab()
        self._build_ide_tab()
        self._build_librarian_tab()
        self._build_sessions_tab()
        self._build_nodes_tab()
        self._build_speech_tab()
        self._build_apoth_tab()

    # ---- Council tab ----

    def _build_council_tab(self):
        self.tab_council = ttk.Frame(self.nb)
        self.nb.add(self.tab_council, text="⚖ Council")

        # Main paned window: transcript | judge panel
        paned = tk.PanedWindow(self.tab_council, orient="horizontal",
                               bg="#1e1e2e", sashwidth=6, sashrelief="flat")
        paned.pack(fill="both", expand=True, padx=6, pady=6)

        # Left: transcript
        left = ttk.Frame(paned)
        paned.add(left, minsize=500)

        ttk.Label(left, text="Transcript").pack(anchor="w")
        self.transcript = self._make_text(left, wrap="word", state="disabled")
        self._register_transcript_tags()
        sb = ttk.Scrollbar(left, command=self.transcript.yview)
        self.transcript.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.transcript.pack(fill="both", expand=True)

        # Right: judge + live stream preview
        right = ttk.Frame(paned)
        paned.add(right, minsize=280)

        ttk.Label(right, text="Judge Panel").pack(anchor="w")
        self.judge_box = self._make_text(right, wrap="word", width=40, state="disabled", height=14)
        self.judge_box.pack(fill="both", expand=True)

        ttk.Label(right, text="Live Token Stream").pack(anchor="w", pady=(8, 0))
        self.stream_box = self._make_text(right, wrap="word", width=40, height=10, state="disabled")
        self.stream_box.pack(fill="both", expand=True)

        # Bottom input area
        bottom = ttk.Frame(self.tab_council)
        bottom.pack(fill="x", padx=6, pady=(0, 6))

        ttk.Label(bottom, text="Input").pack(anchor="w")
        self.input = self._make_text(bottom, wrap="word", height=4)
        self.input.pack(fill="x")
        self.input.bind("<Control-Return>", lambda e: self._send())

        btns = ttk.Frame(bottom)
        btns.pack(fill="x", pady=(4, 0))

        ttk.Button(btns, text="Send  [Ctrl+Enter]", command=self._send).pack(side="left")
        ttk.Button(btns, text="Clear", command=lambda: self._set_text(self.input, "")).pack(side="left", padx=6)

        self.var_deliberate      = tk.BooleanVar(value=True)
        self.var_tools           = tk.BooleanVar(value=False)
        self.var_fill_ide        = tk.BooleanVar(value=True)
        self.var_stream          = tk.BooleanVar(value=True)

        ttk.Checkbutton(btns, text="Deliberation",    variable=self.var_deliberate).pack(side="left", padx=4)
        ttk.Checkbutton(btns, text="Tools",           variable=self.var_tools).pack(side="left", padx=4)
        ttk.Checkbutton(btns, text="Fill IDE",        variable=self.var_fill_ide).pack(side="left", padx=4)
        ttk.Checkbutton(btns, text="Stream tokens",   variable=self.var_stream).pack(side="left", padx=4)

        ttk.Button(btns, text="Pins…", command=self._edit_pins).pack(side="left", padx=8)
        ttk.Button(btns, text="New Session", command=self._new_session).pack(side="left", padx=4)

        self.status = ttk.Label(btns, text="● idle", foreground="#a6e3a1")
        self.status.pack(side="right")

    def _register_transcript_tags(self):
        self.transcript.tag_configure("phase",   foreground=PHASE_COLOR,   font=("Consolas", 9, "italic"))
        self.transcript.tag_configure("token",   foreground=TOKEN_COLOR)
        for role, color in ROLE_COLORS.items():
            tag = f"who_{role.lower().replace('-','_').replace(' ','_')}"
            self.transcript.tag_configure(tag, foreground=color, font=("Consolas", 10, "bold"))
        self.transcript.tag_configure("who_default", foreground=DEFAULT_COLOR, font=("Consolas", 10, "bold"))
        self.transcript.tag_configure("error", foreground="#f38ba8")

    # ---- IDE tab ----

    def _build_ide_tab(self):
        self.tab_ide = ttk.Frame(self.nb)
        self.nb.add(self.tab_ide, text="💻 IDE / Runner")

        # Script name bar
        name_row = ttk.Frame(self.tab_ide)
        name_row.pack(fill="x", padx=10, pady=(8, 2))
        ttk.Label(name_row, text="Script:").pack(side="left")
        self.script_name_var = tk.StringVar(value=self.current_script_name)
        ttk.Entry(name_row, textvariable=self.script_name_var, width=40).pack(side="left", padx=6)
        ttk.Button(name_row, text="Apply", command=self._apply_script_name).pack(side="left")
        ttk.Button(name_row, text="Clear Code", command=lambda: self._set_text(self.ide_code, "")).pack(side="left", padx=6)

        paned = tk.PanedWindow(self.tab_ide, orient="horizontal", bg="#1e1e2e", sashwidth=6)
        paned.pack(fill="both", expand=True, padx=10, pady=4)

        left = ttk.Frame(paned)
        paned.add(left, minsize=400)

        ttk.Label(left, text="Code").pack(anchor="w")
        self.ide_code = self._make_text(left, wrap="none", font=("Consolas", 11))
        self.ide_code.pack(fill="both", expand=True)

        right = ttk.Frame(paned)
        paned.add(right, minsize=300)

        ttk.Label(right, text="Output").pack(anchor="w")
        self.ide_out = self._make_text(right, wrap="word", state="disabled")
        self.ide_out.tag_configure("stderr", foreground="#f38ba8")
        self.ide_out.tag_configure("info",   foreground="#89b4fa")
        self.ide_out.pack(fill="both", expand=True)

        btns = ttk.Frame(self.tab_ide)
        btns.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Button(btns, text="▶  Run (streaming)", command=self._ide_run_stream).pack(side="left")
        ttk.Button(btns, text="Run (blocking)",     command=self._ide_run).pack(side="left", padx=6)
        ttk.Button(btns, text="Snapshot to Vault",  command=self._ide_snapshot).pack(side="left")
        ttk.Button(btns, text="Clear Output",
                   command=lambda: self._set_text(self.ide_out, "")).pack(side="left", padx=6)

    # ---- Librarian tab ----

    def _build_librarian_tab(self):
        self.tab_lib = ttk.Frame(self.nb)
        self.nb.add(self.tab_lib, text="📚 Librarian")

        top = ttk.Frame(self.tab_lib)
        top.pack(fill="both", expand=True, padx=10, pady=10)

        left = ttk.Frame(top)
        left.pack(side="left", fill="both", expand=True)

        ttk.Label(left, text=f"Vault: {VAULT_DIR}").pack(anchor="w")

        self.vault_lb = tk.Listbox(left, bg="#181825", fg="#cdd6f4",
                                   selectbackground="#585b70", relief="flat",
                                   font=("Consolas", 10))
        self.vault_lb.pack(fill="both", expand=True, pady=4)
        self.vault_lb.bind("<Double-Button-1>", lambda e: self._lib_preview())

        right = ttk.Frame(top)
        right.pack(side="right", fill="y", padx=(10, 0))

        ttk.Button(right, text="Refresh",       command=self._lib_refresh).pack(fill="x")
        ttk.Button(right, text="Preview",       command=self._lib_preview).pack(fill="x", pady=4)
        ttk.Button(right, text="Commit to Git", command=self._lib_commit).pack(fill="x")
        ttk.Button(right, text="Open Folder",   command=self._lib_open_vault).pack(fill="x", pady=4)

        self._lib_refresh()

    # ---- Sessions tab ----

    def _build_sessions_tab(self):
        self.tab_sessions = ttk.Frame(self.nb)
        self.nb.add(self.tab_sessions, text="🕓 Sessions")

        top = ttk.Frame(self.tab_sessions)
        top.pack(fill="both", expand=True, padx=10, pady=10)

        left = ttk.Frame(top)
        left.pack(side="left", fill="both", expand=True)

        ttk.Label(left, text="Past Sessions (double-click to load as prior context)").pack(anchor="w")
        self.session_lb = tk.Listbox(left, bg="#181825", fg="#cdd6f4",
                                     selectbackground="#585b70", relief="flat",
                                     font=("Consolas", 10))
        self.session_lb.pack(fill="both", expand=True, pady=4)
        self.session_lb.bind("<Double-Button-1>", lambda e: self._sessions_load_prior())

        right = ttk.Frame(top)
        right.pack(side="right", fill="y", padx=(10, 0))

        ttk.Button(right, text="Refresh",           command=self._sessions_refresh).pack(fill="x")
        ttk.Button(right, text="Load as Prior",     command=self._sessions_load_prior).pack(fill="x", pady=4)
        ttk.Button(right, text="Preview Session",   command=self._sessions_preview).pack(fill="x")
        ttk.Button(right, text="Clear Prior",       command=self._sessions_clear_prior).pack(fill="x", pady=4)

        self.prior_label = ttk.Label(right, text="Prior: none", wraplength=180)
        self.prior_label.pack(anchor="w", pady=4)

        ttk.Label(top, text="Session Preview").pack(anchor="w")
        self.session_preview = self._make_text(self.tab_sessions, wrap="word", height=12, state="disabled")
        self.session_preview.pack(fill="x", padx=10, pady=(0, 10))

        self._sessions_refresh()

    # ---- Nodes tab ----

    def _build_nodes_tab(self):
        self.tab_nodes = ttk.Frame(self.nb)
        self.nb.add(self.tab_nodes, text="🖥 Nodes")

        top = ttk.Frame(self.tab_nodes)
        top.pack(fill="both", expand=True, padx=10, pady=10)

        ttk.Label(top, text="Ollama Node Status  (auto-refreshes every 15s)").pack(anchor="w")

        cols = ("host", "status", "latency", "active_models", "installed")
        self.nodes_tree = ttk.Treeview(top, columns=cols, show="headings", height=8)
        self.nodes_tree.heading("host",          text="Host")
        self.nodes_tree.heading("status",        text="Status")
        self.nodes_tree.heading("latency",       text="Latency")
        self.nodes_tree.heading("active_models", text="Active Models")
        self.nodes_tree.heading("installed",     text="Installed Models")
        self.nodes_tree.column("host",          width=220)
        self.nodes_tree.column("status",        width=80)
        self.nodes_tree.column("latency",       width=80)
        self.nodes_tree.column("active_models", width=140)
        self.nodes_tree.column("installed",     width=400)
        self.nodes_tree.pack(fill="both", expand=True, pady=4)

        self.nodes_tree.tag_configure("up",   foreground="#a6e3a1")
        self.nodes_tree.tag_configure("down", foreground="#f38ba8")

        btns = ttk.Frame(top)
        btns.pack(fill="x")
        ttk.Button(btns, text="Refresh Now", command=self._refresh_nodes_async).pack(side="left")
        self.nodes_status_label = ttk.Label(btns, text="")
        self.nodes_status_label.pack(side="left", padx=10)

        # Dispatcher hosts config
        ttk.Separator(top, orient="horizontal").pack(fill="x", pady=10)
        hrow = ttk.Frame(top)
        hrow.pack(fill="x")
        ttk.Label(hrow, text="Pi hosts (comma-separated URLs):").pack(side="left")
        self.pi_hosts_var = tk.StringVar(value=", ".join(ce.DEFAULT_PI_HOSTS))
        ttk.Entry(hrow, textvariable=self.pi_hosts_var, width=50).pack(side="left", padx=6)
        ttk.Button(hrow, text="Apply & Rebuild Dispatcher",
                   command=self._apply_pi_hosts).pack(side="left")

    # ---- Speech tab ----

    def _build_speech_tab(self):
        self.tab_speech = ttk.Frame(self.nb)
        self.nb.add(self.tab_speech, text="🎙 Speech")

        top = ttk.Frame(self.tab_speech)
        top.pack(fill="both", expand=True, padx=10, pady=10)

        btns = ttk.Frame(top)
        btns.pack(fill="x")
        ttk.Button(btns, text="Record 5s",       command=self._stt_record).pack(side="left")
        ttk.Button(btns, text="Transcribe",      command=self._stt_transcribe).pack(side="left", padx=6)
        ttk.Button(btns, text="Send to Council", command=self._stt_send_to_council).pack(side="left")

        ttk.Label(top, text="Transcription").pack(anchor="w", pady=(10, 0))
        self.stt_out = self._make_text(top, wrap="word")
        self.stt_out.pack(fill="both", expand=True)

    # ---- Apothecary tab ----

    def _build_apoth_tab(self):
        self.tab_apoth = ttk.Frame(self.nb)
        self.nb.add(self.tab_apoth, text="🔧 Apothecary")
        self.apoth_console = ae.ApothecaryConsole(self.tab_apoth, self.apoth, ui_queue=self.ui_q)
        self.apoth_console.pack(fill="both", expand=True)

    # ============================
    # Transcript helpers
    # ============================

    def _role_tag(self, who: str) -> str:
        key = who.lower().replace("-", "_").replace(" ", "_")
        tag = f"who_{key}"
        if tag in ROLE_COLORS or who in ROLE_COLORS:
            return tag
        return "who_default"

    def _set_text(self, widget: tk.Text, text: str):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        if widget not in (self.input, self.ide_code, self.stt_out, self.session_preview):
            widget.configure(state="disabled")

    def _append_transcript(self, who: str, text: str, kind: str = "final"):
        self.transcript.configure(state="normal")
        tag = self._role_tag(who)
        if kind == "phase":
            self.transcript.insert("end", f"  {text}\n", "phase")
        elif kind == "token":
            self.transcript.insert("end", text, "token")
        else:
            self.transcript.insert("end", f"\n{who}:\n", tag)
            self.transcript.insert("end", text.strip() + "\n")
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

        if kind not in ("token", "phase", "thought"):
            self.librarian.log_event(who, text)
            self.convo_store.append(self.session_id, {"ts": now_iso(), "who": who, "text": text})

    def _append_stream_box(self, who: str, token: str):
        """Append a single token to the live stream preview box."""
        self.stream_box.configure(state="normal")
        if who not in self._stream_buffers:
            # New speaker — add header
            self._stream_buffers[who] = ""
            self.stream_box.insert("end", f"\n{who}: ", self._role_tag(who))
        self._stream_buffers[who] += token
        self.stream_box.insert("end", token)
        self.stream_box.see("end")
        self.stream_box.configure(state="disabled")

    def _clear_stream_box(self):
        self._stream_buffers.clear()
        self.stream_box.configure(state="normal")
        self.stream_box.delete("1.0", "end")
        self.stream_box.configure(state="disabled")

    def _set_judge(self, text: str):
        self.judge_box.configure(state="normal")
        self.judge_box.delete("1.0", "end")
        self.judge_box.insert("1.0", text)
        self.judge_box.configure(state="disabled")

    def _ide_print(self, text: str, tag: str = ""):
        self.ide_out.configure(state="normal")
        if tag:
            self.ide_out.insert("end", text, tag)
        else:
            self.ide_out.insert("end", text)
        self.ide_out.see("end")
        self.ide_out.configure(state="disabled")

    def _set_status(self, text: str, color: str = "#a6e3a1"):
        self.status.configure(text=text, foreground=color)

    # ============================
    # Main send logic
    # ============================

    def _send(self):
        user_text = self.input.get("1.0", "end").strip()
        if not user_text:
            return
        self._set_text(self.input, "")
        self._append_transcript("User", user_text)
        self._clear_stream_box()

        route = self.judge.route(user_text)
        self._set_judge(f"Route: {route}\n")
        self._set_status(f"● {route}…", "#fab387")

        if route == "apothecary":
            self._append_transcript("Judge", "Routing to Apothecary tab.", "final")
            self.nb.select(self.tab_apoth)
            self._set_status("● idle")
            return
        if route == "speech":
            self._append_transcript("Judge", "Routing to Speech tab.", "final")
            self.nb.select(self.tab_speech)
            self._set_status("● idle")
            return
        if route == "librarian":
            self._append_transcript("Judge", "Routing to Librarian tab.", "final")
            self.nb.select(self.tab_lib)
            self._set_status("● idle")
            return
        if route == "ide":
            self.nb.select(self.tab_ide)

        use_stream = bool(self.var_stream.get())

        def _token_cb(who: str, token: str):
            """Called from the worker thread — post to UI queue."""
            if use_stream:
                self.ui_q.put(("stream_token", who, token))

        def worker():
            try:
                enable_tools = bool(self.var_tools.get())
                tools = _make_tools(self.runner, self.librarian, VAULT_DIR) if enable_tools else {}

                agents = {
                    "writer":     ModelAgent("Writer",     self.writer,     enable_tools=False, token_callback=_token_cb),
                    "peasant":    ModelAgent("Peasant",    self.peasant,    enable_tools=False, token_callback=_token_cb),
                    "intern":     ModelAgent("Intern",     self.intern,     tools=tools, enable_tools=enable_tools, token_callback=_token_cb),
                    "techpriest": ModelAgent("Tech-Priest",self.techpriest, tools=tools, enable_tools=enable_tools, token_callback=_token_cb),
                    "artist":     ModelAgent("Artist",     self.artist,     enable_tools=False, token_callback=_token_cb),
                }

                def _ev_cb(ev: AgentEvent):
                    self.ui_q.put(("live_event", ev))

                orch = DeliberationOrchestrator(
                    judge_model=self.judge, agents=agents,
                    max_rounds=2, debate_turns=2,
                    event_callback=_ev_cb,
                )
                panel = ["intern", "techpriest", "artist"]
                events = orch.run(user_text, panel=panel, synth="writer")

                # Extract final and critique
                final_text = next((e.text for e in reversed(events) if e.who == "Writer" and e.kind == "final"), "")
                last_critique = next((e.text for e in reversed(events) if e.who == "Judge" and e.kind == "observation"), "")

                # IDE fill for code tasks
                if route == "ide" and self.var_fill_ide.get():
                    json_prompt = (
                        "Return JSON ONLY — keys: filename, code.\n"
                        "filename: short descriptive snake_case name ending in .py\n"
                        "code: complete runnable python script\n\n"
                        f"USER REQUEST:\n{user_text}\n\n"
                        f"PROPOSAL:\n{final_text}\n"
                    )
                    json_resp = self.writer.respond(json_prompt)
                    fn, code = _parse_script_json(json_resp)
                    if not code:
                        code = _extract_code_block(final_text) or final_text
                    base = _safe_script_basename(fn) if fn else _safe_script_basename(user_text.splitlines()[0][:60])
                    self.ui_q.put(("set_script_name", base))
                    self.ui_q.put(("ide_fill", code))

                # Memory update on PASS
                if "Verdict: PASS" in (last_critique or ""):
                    self.ui_q.put(("memory_update", user_text, final_text, last_critique))

                self.ui_q.put(("judge_final", last_critique))
                self.ui_q.put(("done", None))

            except Exception as e:
                import traceback
                self.ui_q.put(("error", traceback.format_exc()))

        threading.Thread(target=worker, daemon=True).start()

    # ============================
    # Queue polling
    # ============================

    def _poll_ui_queue(self):
        try:
            while True:
                item = self.ui_q.get_nowait()
                kind = item[0]

                if kind == "live_event":
                    _, ev = item
                    if ev.kind == "phase":
                        self._append_transcript(ev.who, ev.text, "phase")
                    elif ev.kind == "final":
                        # Finalised — clear stream buffer for this speaker
                        self._stream_buffers.pop(ev.who, None)
                        self._append_transcript(ev.who, ev.text, "final")
                    elif ev.kind in ("observation", "action"):
                        self._append_transcript(ev.who, ev.text, ev.kind)
                    # "thought" and "token" events are lightweight; skip transcript

                elif kind == "stream_token":
                    _, who, token = item
                    self._append_stream_box(who, token)

                elif kind == "judge_final":
                    _, txt = item
                    if txt:
                        self._set_judge(txt)

                elif kind == "ide_fill":
                    _, code = item
                    self.ide_code.delete("1.0", "end")
                    self.ide_code.insert("1.0", code)

                elif kind == "set_script_name":
                    _, base = item
                    base = _safe_script_basename(base)
                    self.current_script_name = base
                    self.script_name_var.set(base)
                    self._append_transcript("Librarian", f"Script named: {base}.py", "final")

                elif kind == "memory_update":
                    _, user_text, final_text, critique = item
                    self._do_memory_update(user_text, final_text, critique)

                elif kind == "apoth_out":
                    _, text = item
                    self._append_transcript("Apothecary", text, "final")

                elif kind == "ide_stdout":
                    _, text = item
                    self._ide_print(text)

                elif kind == "ide_stderr":
                    _, text = item
                    self._ide_print(text, "stderr")

                elif kind == "ide_info":
                    _, text = item
                    self._ide_print(text, "info")

                elif kind == "nodes_result":
                    _, statuses = item
                    self._populate_nodes_tree(statuses)
                    self.nodes_status_label.configure(text=f"Last updated: {now_iso()}")
                    # Schedule next auto-refresh
                    if self._node_refresh_id:
                        self.after_cancel(self._node_refresh_id)
                    self._node_refresh_id = self.after(15_000, self._refresh_nodes_async)

                elif kind == "stt_out":
                    _, text = item
                    self.stt_out.delete("1.0", "end")
                    self.stt_out.insert("1.0", text)

                elif kind == "done":
                    self._set_status("● idle")

                elif kind == "error":
                    _, msg = item
                    self._append_transcript("ERROR", msg, "final")
                    self.transcript.tag_add("error", "end-2l", "end")
                    self._set_status("● error", "#f38ba8")

        except queue.Empty:
            pass
        self.after(50, self._poll_ui_queue)

    # ============================
    # Actions
    # ============================

    def _apply_script_name(self):
        nm = _safe_script_basename(self.script_name_var.get())
        self.current_script_name = nm
        self.script_name_var.set(nm)
        self._append_transcript("Librarian", f"Script name: {nm}.py", "final")

    def _new_session(self):
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._append_transcript("Librarian", f"New session started: {self.session_id}", "final")
        self._sessions_refresh()

    def _edit_pins(self):
        current = "{}"
        try:
            if PINS_PATH.exists():
                current = PINS_PATH.read_text(encoding="utf-8")
        except Exception:
            pass
        new = simpledialog.askstring(
            "Personality Pins JSON",
            "Pin personalities to backend keys.\n\n"
            "Valid keys: local_general_primary, local_general_alt, local_coder_primary,\n"
            "local_coder_fast, local_judge_fast, local_peasant_fast\n\n"
            "Example:\n{\"techpriest\":\"local_coder_primary\",\"writer\":\"local_general_primary\"}",
            initialvalue=current, parent=self,
        )
        if new is None:
            return
        try:
            obj = _json.loads(new)
            if not isinstance(obj, dict):
                raise ValueError("Pins must be a JSON object.")
            PINS_PATH.write_text(_json.dumps(obj, indent=2), encoding="utf-8")
            pins = ce.load_personality_pins(PINS_PATH)
            self.personalities = ce.build_personalities(
                pins=pins, vault_dir=VAULT_DIR, session_id=self.session_id,
                trace=True, dispatcher=self.dispatcher, prior_session_id=self.prior_session_id,
            )
            self._unpack_personalities()
            self._append_transcript("Librarian", f"Pins updated: {PINS_PATH}", "final")
        except Exception as e:
            messagebox.showerror("Invalid JSON", str(e))

    def _do_memory_update(self, user_text: str, final_text: str, critique: str):
        try:
            memmgr = self.writer.memory_manager
            if memmgr is None:
                return
            for role_name, role_model in [
                ("intern", self.intern), ("techpriest", self.techpriest),
                ("peasant", self.peasant), ("artist", self.artist),
                ("writer", self.writer), ("judge", self.judge),
            ]:
                ce.update_role_memory_after_pass(
                    role_name=role_name, role_model=role_model,
                    memory_manager=memmgr, user_text=user_text,
                    final_answer=final_text, judge_critique=critique,
                )
            self._append_transcript("Librarian", "Role memories updated (PASS).", "final")
        except Exception as e:
            self._append_transcript("Librarian", f"Memory update failed: {e}", "final")

    # ---- IDE actions ----

    def _ide_run(self):
        code = self.ide_code.get("1.0", "end")
        if not code.strip():
            return

        def worker():
            fname = f"{_safe_script_basename(self.current_script_name)}.py"
            self.ui_q.put(("ide_info", f"[{fname}] Running…\n"))
            try:
                rc, out, err, path = self.runner.run_code(code, filename_hint=fname, timeout_s=120)
                self.ui_q.put(("ide_info", f"[rc={rc}]\n"))
                if out:
                    self.ui_q.put(("ide_stdout", out))
                if err:
                    self.ui_q.put(("ide_stderr", err))
            except Exception as e:
                self.ui_q.put(("ide_stderr", f"Runner error: {e}\n"))

        threading.Thread(target=worker, daemon=True).start()

    def _ide_run_stream(self):
        code = self.ide_code.get("1.0", "end")
        if not code.strip():
            return

        def worker():
            fname = f"{_safe_script_basename(self.current_script_name)}.py"
            self.ui_q.put(("ide_info", f"[{fname}] Running (streaming)…\n"))
            try:
                rc, path = self.runner.run_code_streaming(
                    code, filename_hint=fname, timeout_s=120,
                    stdout_callback=lambda l: self.ui_q.put(("ide_stdout", l)),
                    stderr_callback=lambda l: self.ui_q.put(("ide_stderr", l)),
                )
                self.ui_q.put(("ide_info", f"\n[{path.name}] Exited rc={rc}\n"))
            except Exception as e:
                self.ui_q.put(("ide_stderr", f"Runner error: {e}\n"))

        threading.Thread(target=worker, daemon=True).start()

    def _ide_snapshot(self):
        code = self.ide_code.get("1.0", "end")
        if not code.strip():
            return
        label = _safe_script_basename(self.current_script_name)
        path = self.librarian.snapshot_code(code, label=label)
        self._append_transcript("Librarian", f"Snapshot saved: {path}", "final")
        self._lib_refresh()

    # ---- Librarian actions ----

    def _lib_refresh(self):
        self.vault_lb.delete(0, "end")
        for name in self.librarian.list_items():
            self.vault_lb.insert("end", name)

    def _lib_preview(self):
        sel = self.vault_lb.curselection()
        if not sel:
            return
        name = self.vault_lb.get(sel[0])
        try:
            txt = self.librarian.read_text(name)
            win = tk.Toplevel(self)
            win.title(f"Vault: {name}")
            win.geometry("800x600")
            t = self._make_text(win, wrap="word")
            t.insert("1.0", txt)
            t.configure(state="disabled")
            t.pack(fill="both", expand=True, padx=6, pady=6)
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _lib_commit(self):
        msg = simpledialog.askstring("Commit Message", "Message:", parent=self)
        if not msg:
            return
        ok, out = self.librarian.git_commit_all(msg)
        self._append_transcript("Librarian", f"{'OK' if ok else 'FAIL'}: {out}", "final")

    def _lib_open_vault(self):
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(VAULT_DIR))   # type: ignore
            elif sys.platform == "darwin":
                subprocess.run(["open", str(VAULT_DIR)], check=False)
            else:
                subprocess.run(["xdg-open", str(VAULT_DIR)], check=False)
        except Exception as e:
            messagebox.showerror("Error", str(e))

    # ---- Sessions actions ----

    def _sessions_refresh(self):
        self.session_lb.delete(0, "end")
        for sid in self.convo_store.list_sessions():
            self.session_lb.insert("end", sid)

    def _sessions_load_prior(self):
        sel = self.session_lb.curselection()
        if not sel:
            return
        sid = self.session_lb.get(sel[0])
        self.prior_session_id = sid
        self.prior_label.configure(text=f"Prior: {sid}")
        # Rebuild personalities with new prior context
        pins = ce.load_personality_pins(PINS_PATH)
        self.personalities = ce.build_personalities(
            pins=pins, vault_dir=VAULT_DIR, session_id=self.session_id,
            trace=True, dispatcher=self.dispatcher, prior_session_id=sid,
        )
        self._unpack_personalities()
        self._append_transcript("Librarian", f"Prior session loaded: {sid}", "final")

    def _sessions_clear_prior(self):
        self.prior_session_id = None
        self.prior_label.configure(text="Prior: none")
        pins = ce.load_personality_pins(PINS_PATH)
        self.personalities = ce.build_personalities(
            pins=pins, vault_dir=VAULT_DIR, session_id=self.session_id,
            trace=True, dispatcher=self.dispatcher, prior_session_id=None,
        )
        self._unpack_personalities()
        self._append_transcript("Librarian", "Prior session cleared.", "final")

    def _sessions_preview(self):
        sel = self.session_lb.curselection()
        if not sel:
            return
        sid = self.session_lb.get(sel[0])
        summary = self.convo_store.load_session_summary(sid, max_turns=20)
        self._set_text(self.session_preview, summary or "(empty session)")

    # ---- Node status ----

    def _refresh_nodes_async(self):
        def worker():
            statuses = self.dispatcher.probe_all()
            self.ui_q.put(("nodes_result", statuses))
        threading.Thread(target=worker, daemon=True).start()
        self.nodes_status_label.configure(text="Probing…")

    def _populate_nodes_tree(self, statuses: List[ce.NodeStatus]):
        for row in self.nodes_tree.get_children():
            self.nodes_tree.delete(row)
        for s in statuses:
            status_str = "● up" if s.reachable else "✕ down"
            latency_str = f"{s.latency_ms:.0f} ms" if s.reachable else "—"
            active_str = ", ".join(s.active_model_names) if s.active_model_names else ("none" if s.reachable else "—")
            installed_str = ", ".join(s.installed_models[:6]) if s.installed_models else "—"
            if len(s.installed_models) > 6:
                installed_str += f" (+{len(s.installed_models)-6} more)"
            tag = "up" if s.reachable else "down"
            self.nodes_tree.insert("", "end",
                values=(s.host, status_str, latency_str, active_str, installed_str),
                tags=(tag,))

    def _apply_pi_hosts(self):
        raw = self.pi_hosts_var.get()
        hosts = [h.strip() for h in raw.split(",") if h.strip()]
        self.dispatcher = ce.build_dispatcher(extra_hosts=hosts)
        pins = ce.load_personality_pins(PINS_PATH)
        self.personalities = ce.build_personalities(
            pins=pins, vault_dir=VAULT_DIR, session_id=self.session_id,
            trace=True, dispatcher=self.dispatcher, prior_session_id=self.prior_session_id,
        )
        self._unpack_personalities()
        self._append_transcript("Librarian", f"Dispatcher rebuilt with hosts: {hosts}", "final")
        self._refresh_nodes_async()

    # ---- Speech actions ----

    def _stt_record(self):
        ok, msg = self.speech.ready()
        if not ok:
            messagebox.showwarning("Speech→Text", msg)
            return
        wav_path = TMP_DIR / "latest.wav"

        def worker():
            try:
                ok2, msg2 = self.speech.record_wav(wav_path, seconds=5)
                self.ui_q.put(("stt_out", msg2 if ok2 else f"ERROR: {msg2}"))
            except Exception as e:
                self.ui_q.put(("error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _stt_transcribe(self):
        wav_path = TMP_DIR / "latest.wav"
        if not wav_path.exists():
            messagebox.showwarning("Speech→Text", "No recording found. Record first.")
            return

        def worker():
            try:
                ok, text = self.speech.transcribe(wav_path)
                self.ui_q.put(("stt_out", text if ok else f"ERROR: {text}"))
            except Exception as e:
                self.ui_q.put(("error", str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _stt_send_to_council(self):
        text = self.stt_out.get("1.0", "end").strip()
        if not text:
            return
        self._set_text(self.input, text)
        self.nb.select(self.tab_council)
        self._send()


# ============================================================
# Utilities
# ============================================================

def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def main():
    app = CouncilConsole()
    app.mainloop()


if __name__ == "__main__":
    main()
