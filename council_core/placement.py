"""
council_core.placement — the weekly review of which models run where.

A CONTROLLER reads a week of usage and the machines' inventories and proposes
how the models should be arranged: which model each role should use, which
machine should hold which model. By default the controller is the JUDGE's
model (a "controller" entry in model_slots.json roles picks another).

    report   = build_report(vault, statuses=…)    # usage + machines + roles
    proposal = ask_controller(report)              # the controller's JSON
    checked  = check(proposal, report)             # what can actually happen
    apply_role_changes(vault, checked.apply_now)   # only after you approve

WHAT IT MAY CHANGE, AND WHAT IT MAY NOT
  * Role → model on THIS PC: applied by writing model_slots.json, and only
    when the user presses Apply on the change. Never automatically.
  * Role → a model that only another machine has: shown, not applied. A role
    cannot be pinned to a machine yet (Slot has name, path and n_ctx only —
    docs/specialized_nodes.md Stage 1).
  * Install / remove a model on a machine: written as commands for the user
    to run on that machine. The Council never downloads or deletes models
    itself.
  * Only US-origin models (council_core.local_models.maker_and_origin); a
    proposal naming any other model is rejected with the reason.

The report never contains node credentials: node_registry.json holds SSH
passwords, and only hardware and model fields are read from it.

Reviews are logged to <vault>/.council_usage/reviews.jsonl (append-only), and
`due()` says whether a week has passed since the last one. No Qt here.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import usage_log

REVIEW_EVERY_DAYS = 7
REVIEWS_FILE = "reviews.jsonl"
THIS_PC = "This PC"
CONTROLLER = "controller"

#: Role → what it is for, so the controller knows what each one needs.
ROLE_NEEDS = {
    "judge": "verdicts and ranking; quality matters more than speed",
    "writer": "synthesises the final answer; quality and long context",
    "coder": "code and tool calls; needs tool support and 8k+ context",
    "skeptic": "short adversarial answers",
    "sage": "long-view answers", "strategist": "plans",
    "peasant": "short plain questions; speed matters most",
    "intern": "fast first drafts; speed matters most",
    "artist": "creative answers", "docs": "answers from documentation; tools",
    CONTROLLER: "this weekly placement review",
}

#: The controller's reply, as a JSON schema (local_chat constrains to it).
PROPOSAL_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "rearrange": {"type": "boolean"},
        "summary": {"type": "string"},
        "role_changes": {"type": "array", "items": {
            "type": "object",
            "properties": {"role": {"type": "string"},
                           "model": {"type": "string"},
                           "machine": {"type": "string"},
                           "reason": {"type": "string"}},
            "required": ["role", "model", "machine", "reason"]}},
        "install": {"type": "array", "items": {
            "type": "object",
            "properties": {"machine": {"type": "string"},
                           "model": {"type": "string"},
                           "reason": {"type": "string"}},
            "required": ["machine", "model", "reason"]}},
        "remove": {"type": "array", "items": {
            "type": "object",
            "properties": {"machine": {"type": "string"},
                           "model": {"type": "string"},
                           "reason": {"type": "string"}},
            "required": ["machine", "model", "reason"]}},
    },
    "required": ["rearrange", "summary", "role_changes", "install", "remove"],
}


# ============================================================
# The report
# ============================================================

@dataclass
class Machine:
    name: str
    host: str = ""
    up: Optional[bool] = None
    hardware: str = ""
    installed: List[str] = field(default_factory=list)
    running: List[str] = field(default_factory=list)
    calls_logged: int = 0          # the Apothecary's per-node call log


@dataclass
class Report:
    generated: float
    days: int
    usage: List[Dict[str, Any]]
    machines: List[Machine]
    roles: Dict[str, str]          # role -> model it answers with
    controller_role: str
    notes: List[str] = field(default_factory=list)

    def machine(self, name: str) -> Optional[Machine]:
        key = _norm_machine(name)
        for m in self.machines:
            if key in (_norm_machine(m.name), _norm_machine(m.host)):
                return m
        return None

    def text(self) -> str:
        """The report as the controller (and the user) reads it."""
        L = [f"PLACEMENT REPORT — the last {self.days} days, generated "
             f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(self.generated))}",
             "", "ROLES (role → the model it answers with, and what it needs):"]
        for role, model in self.roles.items():
            L.append(f"  {role}: {model or '(main model)'} — "
                     f"{ROLE_NEEDS.get(role, '')}")
        L += ["", "MACHINES:"]
        for m in self.machines:
            state = {True: "up", False: "DOWN", None: "not probed"}[m.up]
            L.append(f"  {m.name}"
                     + (f" ({m.host})" if m.host and m.host != m.name else "")
                     + f" — {state}"
                     + (f"; {m.hardware}" if m.hardware else ""))
            L.append(f"    installed: {', '.join(m.installed) or 'unknown'}")
            if m.running:
                L.append(f"    running now: {', '.join(m.running)}")
        L += ["", "USAGE (role, model, machine: calls, reply tokens, total "
              "seconds, median tokens/s, median wait for the model):"]
        if not self.usage:
            L.append("  (no calls recorded in this period)")
        for u in self.usage:
            L.append(
                f"  {u['role']}, {u['model']}, {u['host']}: {u['calls']} "
                f"calls, {u['gen_tokens']} tokens, {u['seconds']} s, "
                f"{_fmt(u['median_tok_s'])} tok/s, wait "
                f"{_fmt(u['median_wait_s'])} s"
                + (f", {u['truncated']} cut off" if u.get("truncated") else ""))
        if self.notes:
            L += ["", "NOTES:"] + [f"  - {n}" for n in self.notes]
        return "\n".join(L)


def _fmt(v: Any) -> str:
    return "?" if v is None else f"{v:g}" if isinstance(v, float) else str(v)


def _norm_machine(s: str) -> str:
    s = str(s or "").strip().lower()
    s = re.sub(r"^https?://", "", s).rstrip("/")
    return s


def _host_part(host: str) -> str:
    """'http://192.168.1.5:11434/' → '192.168.1.5'."""
    return _norm_machine(host).split("/")[0].rsplit(":", 1)[0] \
        if ":" in _norm_machine(host).split("/")[0] \
        else _norm_machine(host).split("/")[0]


def _is_local(host: str) -> bool:
    h = _norm_machine(host)
    return h in ("", "local", "this pc") or any(
        t in h for t in ("localhost", "127.0.0.1", "[::1]"))


def _registry_machines(vault_dir: Path) -> List[Machine]:
    """Hardware and inventory from the Apothecary's registry — and ONLY those
    fields: the same file holds SSH passwords."""
    path = Path(vault_dir) / "node_registry.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for d in (data.get("nodes") or []) if isinstance(data, dict) else []:
        if not isinstance(d, dict) or not d.get("name"):
            continue
        hw = []
        if d.get("pi_model"):
            hw.append(str(d["pi_model"]))
        if d.get("ram_gb"):
            hw.append(f"{int(d['ram_gb'])} GB RAM")
        if d.get("has_ai_hat"):
            hw.append(f"AI HAT ({d.get('ai_hat_tops') or '?'} TOPS)")
        host = str(d.get("host") or "")
        port = d.get("ollama_port") or 11434
        out.append(Machine(
            name=str(d["name"]),
            host=f"{host}:{port}" if host else "",
            up={"online": True, "offline": False}.get(d.get("status")),
            hardware=", ".join(hw),
            installed=[str(x) for x in d.get("installed_models") or []],
            running=[str(d["active_model"])] if d.get("active_model") else [],
            calls_logged=len(d.get("model_log") or [])))
    return out


def controller_role(slots: Any) -> str:
    """'controller' when model_slots.json gives it a slot, else the judge."""
    roles = getattr(slots, "roles", {}) or {}
    return CONTROLLER if CONTROLLER in roles else "judge"


def role_models(slots: Any, main_path: str) -> Dict[str, str]:
    from .model_slots import COUNCIL_ROLES
    out: Dict[str, str] = {}
    if slots is None:
        return out
    for role in tuple(COUNCIL_ROLES) + (CONTROLLER,):
        if role == CONTROLLER and CONTROLLER not in slots.roles:
            continue
        slot = slots.slots.get(slots.slot_for(role))
        path = slot.resolved_path(main_path) if slot else ""
        out[role] = _model_name(path)
    return out


def _model_name(path: str) -> str:
    p = str(path or "").strip()
    if p.lower().startswith("ollama:"):
        return p[len("ollama:"):]
    return Path(p.replace("\\", "/")).stem if p else ""


def build_report(vault_dir: Path, *, slots: Any = None,
                 statuses: Sequence[Any] = (), days: int = REVIEW_EVERY_DAYS,
                 now: Optional[float] = None,
                 main_path: str = "") -> Report:
    """Everything the controller needs, from the vault and a node probe.

    `statuses` are dispatcher NodeStatus objects (host, reachable,
    installed_models, active_model_names); pass () to skip the network."""
    now = time.time() if now is None else now
    notes: List[str] = []
    calls = usage_log.read(vault_dir, now - days * 86400, now)
    if not calls:
        notes.append("No model calls were recorded this period, so there is "
                     "nothing to judge speed or load by.")

    machines = _registry_machines(vault_dir)
    local = Machine(THIS_PC, "localhost:11434")
    seen_local = False
    for st in statuses or ():
        host = str(getattr(st, "host", "") or "")
        installed = [str(x) for x in getattr(st, "installed_models", []) or []]
        running = [str(x) for x in getattr(st, "active_model_names", []) or []]
        up = bool(getattr(st, "reachable", False))
        if _is_local(host):
            local.up, local.installed, local.running = up, installed, running
            seen_local = True
            continue
        m = next((m for m in machines
                  if m.host and _host_part(m.host) == _host_part(host)), None)
        if m is None:
            m = Machine(_norm_machine(host), _norm_machine(host))
            machines.append(m)
        m.up, m.installed, m.running = up, installed or m.installed, running
    if not seen_local:
        notes.append("This PC's Ollama was not probed; its installed models "
                     "are unknown.")
    if slots is not None:
        for slot in getattr(slots, "slots", {}).values():
            path = slot.resolved_path(main_path)
            if path and not path.lower().startswith("ollama:"):
                name = _model_name(path)
                if name and name not in local.installed:
                    local.installed.append(name)       # a GGUF file here
    notes.append("Roles cannot be pinned to another machine yet: every role "
                 "runs on This PC. Changes for other machines are advice.")
    for call in calls:
        if _is_local(call.get("host", "")):
            call["host"] = THIS_PC
        else:
            m = next((m for m in machines if m.host and
                      _host_part(m.host) == _host_part(call["host"])), None)
            if m is not None:
                call["host"] = m.name
    return Report(generated=now, days=days, usage=usage_log.summarise(calls),
                  machines=[local] + machines,
                  roles=role_models(slots, main_path),
                  controller_role=controller_role(slots), notes=notes)


# ============================================================
# Asking the controller
# ============================================================

INSTRUCTIONS = """You are the Council's controller. Once a week you decide \
whether the AI models should be re-arranged across the machines.

Read the report. Recommend a change only when the usage shows a real \
problem: a role that is slow for what it needs, a model waiting on another \
call to the same model, a machine that is idle while another is busy, a \
reply cut off, a model nobody uses taking space. If things are fine, say so \
and set "rearrange" to false — changing nothing is a good answer.

Rules:
- Use only US-origin models (Llama, Phi, Gemma, Granite, OLMo, gpt-oss).
- A role change names a model and the machine that has it. Use machine \
names exactly as the report writes them.
- Recommend installing a model only on a machine whose hardware can run it.
- Each change has a short reason that cites the numbers in the report.

Reply with JSON only: {"rearrange": bool, "summary": "...", \
"role_changes": [{"role","model","machine","reason"}], \
"install": [{"machine","model","reason"}], \
"remove": [{"machine","model","reason"}]}"""


def ask_controller(report: Report,
                   chat: Optional[Callable[..., str]] = None,
                   timeout: int = 300) -> Dict[str, Any]:
    """The controller's proposal, parsed. Raises ValueError when the reply is
    not usable JSON (the caller shows it; nothing is applied)."""
    if chat is None:
        import council_engine
        chat = council_engine.local_chat
    text = chat([{"role": "system", "content": INSTRUCTIONS},
                 {"role": "user", "content": report.text()}],
                role=report.controller_role, json_schema=PROPOSAL_SCHEMA,
                num_predict=1200, temperature=0.1, timeout=timeout)
    return parse_proposal(text)


def parse_proposal(text: str) -> Dict[str, Any]:
    raw = str(text or "").strip()
    try:
        obj = json.loads(raw)
    except ValueError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise ValueError("the controller did not reply with JSON")
        try:
            obj = json.loads(match.group(0))
        except ValueError as exc:
            raise ValueError(f"the controller's JSON is broken: {exc}")
    if not isinstance(obj, dict):
        raise ValueError("the controller's reply is not a JSON object")
    out = {"rearrange": bool(obj.get("rearrange")),
           "summary": str(obj.get("summary") or "")}
    for key in ("role_changes", "install", "remove"):
        items = obj.get(key) or []
        out[key] = [i for i in items if isinstance(i, dict)] \
            if isinstance(items, list) else []
    return out


# ============================================================
# Checking a proposal against the rules
# ============================================================

@dataclass
class Change:
    kind: str                      # role | install | remove
    role: str = ""
    model: str = ""
    machine: str = ""
    reason: str = ""
    why_not: str = ""              # set when it cannot be applied as asked
    command: str = ""              # install / remove: what the user runs


@dataclass
class Checked:
    summary: str
    rearrange: bool
    apply_now: List[Change] = field(default_factory=list)   # needs Apply
    advice: List[Change] = field(default_factory=list)      # other machines
    commands: List[Change] = field(default_factory=list)    # user runs these
    rejected: List[Change] = field(default_factory=list)

    def text(self) -> str:
        L = [self.summary or "(no summary)", ""]
        if not self.rearrange and not (self.apply_now or self.advice
                                       or self.commands):
            L.append("The controller recommends no changes this week.")

        def block(title: str, items: List[Change], show) -> None:
            if items:
                L.append(title)
                L.extend(f"  • {show(c)}" for c in items)
                L.append("")
        block("Can be applied on this PC (press Apply):", self.apply_now,
              lambda c: f"{c.role} → {c.model}: {c.reason}")
        block("Advice (needs a role pinned to another machine, which the "
              "Council cannot do yet):", self.advice,
              lambda c: f"{c.role} → {c.model} on {c.machine}: {c.reason}")
        block("Commands for you to run on the machine (the Council never "
              "installs or removes models itself):", self.commands,
              lambda c: f"[{c.machine}] {c.command}   — {c.reason}")
        block("Rejected:", self.rejected,
              lambda c: f"{c.kind} {c.role or ''} {c.model} "
                        f"{('on ' + c.machine) if c.machine else ''}: "
                        f"{c.why_not}".replace("  ", " "))
        return "\n".join(L).rstrip()


def checked_from_dict(d: Optional[Dict[str, Any]]) -> Optional[Checked]:
    """A Checked as log_review stored it."""
    if not isinstance(d, dict):
        return None
    fields = set(Change.__dataclass_fields__)

    def changes(key: str) -> List[Change]:
        return [Change(**{k: v for k, v in c.items() if k in fields})
                for c in d.get(key) or [] if isinstance(c, dict)]
    return Checked(summary=str(d.get("summary") or ""),
                   rearrange=bool(d.get("rearrange")),
                   apply_now=changes("apply_now"), advice=changes("advice"),
                   commands=changes("commands"), rejected=changes("rejected"))


def _origin(model: str) -> str:
    try:
        from .local_models import maker_and_origin
        return maker_and_origin(model)[1]
    except Exception:                                     # noqa: BLE001
        return "unknown"


def _has(installed: Sequence[str], model: str) -> bool:
    def norm(s: str) -> str:
        s = s.strip().lower()
        return s[:-len(":latest")] if s.endswith(":latest") else s
    return any(norm(i) == norm(model) for i in installed)


_SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


def check(proposal: Dict[str, Any], report: Report) -> Checked:
    """Sort every proposed change into apply-now, advice, commands or
    rejected, with the reason. Nothing here changes anything."""
    from .model_slots import COUNCIL_ROLES
    known_roles = set(COUNCIL_ROLES) | {CONTROLLER}
    out = Checked(summary=str(proposal.get("summary") or ""),
                  rearrange=bool(proposal.get("rearrange")))

    for item in proposal.get("role_changes") or []:
        c = Change("role", role=str(item.get("role", "")).strip(),
                   model=str(item.get("model", "")).strip(),
                   machine=str(item.get("machine", "")).strip(),
                   reason=str(item.get("reason", "")).strip())
        machine = report.machine(c.machine) or (
            report.machine(THIS_PC) if _is_local(c.machine) else None)
        if c.role not in known_roles:
            c.why_not = f"there is no role '{c.role}'"
        elif not _SAFE_MODEL.match(c.model):
            c.why_not = "the model name is not a valid model name"
        elif _origin(c.model) != "US":
            c.why_not = "only US-origin models may be used"
        elif machine is None:
            c.why_not = f"there is no machine '{c.machine}' in the report"
        elif not _has(machine.installed, c.model):
            c.why_not = f"{machine.name} does not have {c.model} installed"
        elif machine.name == THIS_PC and report.roles.get(c.role) and \
                _has([report.roles[c.role]], c.model):
            # Every role runs on this PC today, so the same model on ANOTHER
            # machine is still a move worth advising.
            c.why_not = f"{c.role} already uses {c.model}"
        if c.why_not:
            out.rejected.append(c)
        elif machine.name == THIS_PC:
            c.machine = THIS_PC
            out.apply_now.append(c)
        else:
            c.machine = machine.name
            out.advice.append(c)

    for kind in ("install", "remove"):
        for item in proposal.get(kind) or []:
            c = Change(kind, model=str(item.get("model", "")).strip(),
                       machine=str(item.get("machine", "")).strip(),
                       reason=str(item.get("reason", "")).strip())
            machine = report.machine(c.machine) or (
                report.machine(THIS_PC) if _is_local(c.machine) else None)
            if machine is None:
                c.why_not = f"there is no machine '{c.machine}' in the report"
            elif not _SAFE_MODEL.match(c.model):
                c.why_not = "the model name is not a valid model name"
            elif kind == "install" and _origin(c.model) != "US":
                c.why_not = "only US-origin models may be installed"
            elif kind == "install" and _has(machine.installed, c.model):
                c.why_not = f"{machine.name} already has it"
            elif kind == "remove" and not _has(machine.installed, c.model):
                c.why_not = f"{machine.name} does not have it"
            elif kind == "remove" and machine.name == THIS_PC and any(
                    _has([m], c.model) for m in report.roles.values() if m):
                c.why_not = "a role on this PC still uses it"
            if c.why_not:
                out.rejected.append(c)
                continue
            c.machine = machine.name
            c.command = (f"ollama pull {c.model}" if kind == "install"
                         else f"ollama rm {c.model}")
            out.commands.append(c)
    return out


# ============================================================
# Applying (role changes on this PC only) and the review log
# ============================================================

def apply_role_changes(vault_dir: Path, changes: Sequence[Change]) -> List[str]:
    """Point each role at its new model by writing model_slots.json. Only
    Ollama models on this PC; only the changes the user approved. Returns a
    line per change made."""
    from . import model_slots as ms
    config = ms.load(Path(vault_dir))
    done: List[str] = []
    for c in changes:
        if c.kind != "role" or c.machine != THIS_PC:
            continue
        path = f"{ms.OLLAMA_PREFIX}{c.model}"
        slot = next((s.name for s in config.slots.values()
                     if s.path.lower() == path.lower()), None)
        if slot is None:
            slot = re.sub(r"[^a-z0-9]+", "-", c.model.lower()).strip("-")
            base, n = slot, 2
            while slot in config.slots:
                slot, n = f"{base}-{n}", n + 1
            config.slots[slot] = ms.Slot(slot, path)
        config.roles[c.role] = slot
        done.append(f"{c.role} now answers with {c.model} (slot '{slot}')")
    if done:
        ms.save(Path(vault_dir), config)
        ms.invalidate()
    return done


def reviews_path(vault_dir: Path) -> Path:
    return usage_log.usage_dir(vault_dir) / REVIEWS_FILE


def log_review(vault_dir: Path, report: Report,
               proposal: Optional[Dict[str, Any]], checked: Optional[Checked],
               applied: Sequence[str] = (), error: str = "",
               now: Optional[float] = None) -> str:
    """Append one review; returns its id."""
    rid = uuid.uuid4().hex[:12]
    entry = {"id": rid, "ts": round(time.time() if now is None else now, 3),
             "controller": report.controller_role, "report": report.text(),
             "proposal": proposal, "error": error, "applied": list(applied),
             "checked": asdict(checked) if checked else None}
    path = reviews_path(vault_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return rid


def log_applied(vault_dir: Path, review_id: str, applied: Sequence[str],
                now: Optional[float] = None) -> None:
    """Record which of a review's changes the user applied."""
    entry = {"kind": "applied", "review": review_id,
             "ts": round(time.time() if now is None else now, 3),
             "applied": list(applied)}
    path = reviews_path(vault_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def last_review(vault_dir: Path) -> Optional[Dict[str, Any]]:
    try:
        lines = reviews_path(vault_dir).read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for raw in reversed(lines):
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if isinstance(entry, dict) and "ts" in entry \
                and entry.get("kind") != "applied":
            return entry
    return None


def due(vault_dir: Path, now: Optional[float] = None,
        every_days: int = REVIEW_EVERY_DAYS) -> bool:
    """Whether a week (or `every_days`) has passed since the last review."""
    now = time.time() if now is None else now
    last = last_review(vault_dir)
    if last is None:
        return True
    try:
        return now - float(last["ts"]) >= every_days * 86400
    except (TypeError, ValueError):
        return True


def run_review(vault_dir: Path, *, slots: Any = None,
               statuses: Sequence[Any] = (),
               chat: Optional[Callable[..., str]] = None,
               now: Optional[float] = None, main_path: str = ""):
    """Report → controller → check → log. Applies NOTHING. Never raises:
    a failed controller call is logged and returned as the error."""
    report = build_report(vault_dir, slots=slots, statuses=statuses, now=now,
                          main_path=main_path)
    proposal, checked, error = None, None, ""
    try:
        proposal = ask_controller(report, chat=chat)
        checked = check(proposal, report)
    except Exception as exc:                              # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    rid = log_review(vault_dir, report, proposal, checked, error=error,
                     now=now)
    return rid, report, checked, error


__all__ = ["REVIEW_EVERY_DAYS", "THIS_PC", "CONTROLLER", "PROPOSAL_SCHEMA",
           "Machine", "Report", "Change", "Checked", "build_report",
           "controller_role", "role_models", "ask_controller", "parse_proposal", "check",
           "apply_role_changes", "log_review", "last_review", "due",
           "checked_from_dict", "log_applied",
           "run_review"]
