"""
council_core.after_turn — what the council keeps from a question once it
has been answered.

Two things the old Tk shell did after every deliberation and the Qt Council
tab never did (the Council Map showed both as missing):

  * GAPS → WISHLIST. Members who rated their own answer 4/10 or lower are
    collected by the deliberation (ctx.shared["_low_conf_gaps"]); they are
    written to the librarian's wishlist (<vault>/librarian_wishlist.md) as
    what the vault should hold to answer that kind of question better.
  * ROLE MEMORY. Every member that took part writes what it learned to its
    own memory (council_engine.update_role_memory_after_pass — only roles in
    MEMORY_WRITE_ROLES write; the Writer and the Judge are read-only by
    design), and one observer role adds cross-session facts to the shared
    project memory (update_project_memory_after_pass). Every role reads
    these on its next question, so the council now actually learns.

The memory updates cost model calls (about two per writing role, one for
the project), so the Council tab runs `learn` on a worker AFTER the answer
is shown. COUNCIL_MEMORY_AFTER_TURN=0 turns them off; the wishlist is
always written (it is a file append, no model).

The engine's own functions do the writing; this module only decides who
and when. Nothing here imports a toolkit.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

#: First available of these writes the shared project memory — one observer
#: per question is enough (the old shell's order).
OBSERVER_ORDER = ("coder", "sage", "strategist")


def memory_enabled() -> bool:
    return os.environ.get("COUNCIL_MEMORY_AFTER_TURN", "1").strip().lower() \
        not in ("0", "false", "no", "off")


@dataclass
class Learned:
    gaps_logged: int = 0
    sage_gap: str = ""
    roles_updated: List[str] = field(default_factory=list)
    project_updated_by: str = ""
    errors: List[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = []
        if self.roles_updated:
            parts.append("memory updated for " + ", ".join(self.roles_updated))
        if self.project_updated_by:
            parts.append(f"project notes by the {self.project_updated_by}")
        if self.gaps_logged:
            parts.append(f"{self.gaps_logged} gap(s) added to the wishlist")
        if self.sage_gap:
            parts.append(f"the Sage noted a gap in its knowledge "
                         f"({self.sage_gap})")
        text = "; ".join(parts) or "nothing new to keep"
        if self.errors:
            text += f" ({len(self.errors)} problem(s): {self.errors[0]})"
        return text


def log_gaps(vault_dir: Path, gaps: Sequence[Dict[str, Any]],
             librarian: Any = None) -> int:
    """Append each gap to the librarian's wishlist. Returns how many."""
    if not gaps:
        return 0
    if librarian is None:
        import council_engine
        vault_dir = Path(vault_dir)
        librarian = council_engine.Librarian(
            vault_dir, vault_dir / "logs" / "council.log")
    n = 0
    for gap in gaps:
        try:
            librarian.log_gap(
                who=str(gap.get("who", "?")),
                topic=str(gap.get("topic", "unknown")),
                reason=str(gap.get("reason",
                                   "low self-reported confidence")))
            n += 1
        except Exception:                                 # noqa: BLE001
            continue
    return n


def log_sage_gap(result: Any, vault_dir: Path, knowledge: Any = None) -> str:
    """If the Sage's answer says it lacks something ("GAP: …", "I don't
    know…" — sage_agent.detect_gap), log it to the Sage's own gaps
    (<vault>/sage_knowledge/gaps.jsonl, shown in the Agents tab). Returns
    the reason, or "". SageAgent.respond did this; the Qt turn did not."""
    answers = [e.text for e in getattr(result, "events", []) or []
               if getattr(e, "who", "") == "Sage"
               and getattr(e, "kind", "") == "final"]
    if not answers:
        return ""
    import sage_agent
    reason = sage_agent.detect_gap(answers[-1])
    if not reason:
        return ""
    if knowledge is None:
        knowledge = sage_agent.SageKnowledge(Path(vault_dir) /
                                             "sage_knowledge")
    knowledge.log_gap(_question(result), reason=reason)
    return reason


def _memory_manager(models: Any) -> Any:
    for name in ("writer", "judge", "coder", "intern"):
        model = getattr(models, name, None)
        mgr = getattr(model, "memory_manager", None)
        if mgr is not None:
            return mgr
    return None


def learn(models: Any, result: Any, vault_dir: Path, *,
          engine: Any = None, librarian: Any = None,
          memory: Optional[bool] = None) -> Learned:
    """Keep what this answered question taught. `result` is a council_turn
    TurnResult. Blocking (model calls) — call it from a worker. Never
    raises: a failure is reported in the returned Learned."""
    out = Learned()
    try:
        out.gaps_logged = log_gaps(vault_dir, getattr(result, "low_conf_gaps",
                                                      []) or [], librarian)
    except Exception as exc:                              # noqa: BLE001
        out.errors.append(f"wishlist: {exc}")

    try:
        out.sage_gap = log_sage_gap(result, vault_dir)
    except Exception as exc:                              # noqa: BLE001
        out.errors.append(f"sage gaps: {exc}")

    if not (memory_enabled() if memory is None else memory):
        return out
    if not getattr(result, "ok", False) or not getattr(result, "answer", ""):
        return out
    if engine is None:
        try:
            import council_engine as engine
        except Exception as exc:                          # noqa: BLE001
            out.errors.append(f"engine: {exc}")
            return out
    mgr = _memory_manager(models)
    if mgr is None:
        return out
    passed = getattr(result, "verdict", "") == "PASS"
    question = _question(result)
    writable = getattr(engine, "MEMORY_WRITE_ROLES", set())
    for role in getattr(result, "panel", []) or []:
        model = getattr(models, role, None)
        if model is None or role not in writable:
            continue
        try:
            engine.update_role_memory_after_pass(
                role_name=role, role_model=model, memory_manager=mgr,
                user_text=question, final_answer=result.answer,
                judge_critique=result.critique, passed=passed)
            out.roles_updated.append(role)
        except Exception as exc:                          # noqa: BLE001
            out.errors.append(f"{role}: {exc}")
    for role in OBSERVER_ORDER:
        model = getattr(models, role, None)
        if model is None:
            continue
        try:
            engine.update_project_memory_after_pass(
                role_name=role, role_model=model, memory_manager=mgr,
                user_text=question, final_answer=result.answer,
                judge_critique=result.critique, passed=passed)
            out.project_updated_by = role
        except Exception as exc:                          # noqa: BLE001
            out.errors.append(f"project memory: {exc}")
        break
    return out


def _question(result: Any) -> str:
    return str(getattr(result, "question", "") or "")


__all__ = ["learn", "log_gaps", "log_sage_gap", "memory_enabled", "Learned",
           "OBSERVER_ORDER"]
