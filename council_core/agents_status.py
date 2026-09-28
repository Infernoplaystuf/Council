"""
council_core.agents_status — what the Agents tab reports, as plain data.

THE BOARD WAS EIGHT GLOBALS
The Tk tab reads eight module-level try/except booleans (`_CODER_AGENT_OK` ...
`_RAG_OK`, council_gui_engine.py:63-127) by name inside its builder, so a view
that wanted the board had to import the 22,000-line engine to get eight bits.
Here they are one function returning ordered rows.

"AVAILABLE" MEANS IMPORTABLE, NOT PRESENT
Same test the Tk engine applies: the module is imported, and any exception
counts as unavailable. `find_spec` would be cheaper and wrong — it finds
`vault_rag.py` on a machine where chromadb is broken and would put a green
tick next to a subsystem that fails on first use. The imports are real, so a
view should call `availability()` off the GUI thread.

ALSO HERE
`classify` — the colour rule for the agent event log, which Tk wrote inline
against a tk.Text — and `AgentToggles`, the three session switches the tab
owns and a council turn reads.
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import List, Tuple

#: (label, module). Order and wording are the Tk board's, because the label is
#: what the user reads.
SUBSYSTEMS: Tuple[Tuple[str, str], ...] = (
    ("Coder Agent (LangGraph loop)",          "coder_agent"),
    ("Intern Agent (web research)",           "intern_agent"),
    ("Vault Agent (file tasks, sandboxed)",   "vault_agent"),
    ("Sage (tunable domain expert)",          "sage_agent"),
    ("Vault Scraper (web → vault pipeline)",  "vault_scraper"),
    ("Vault RAG (ChromaDB)",                  "vault_rag"),
    ("Dream3D-NX Domain Patch",               "dream3d_council_patch"),
    ("Dream3D simplnx Primer",                "dream3d_primer"),
)

#: What to install when a row is red. Shown verbatim in the tab.
INSTALL_HINTS = (
    "pip install crawl4ai && crawl4ai-setup          # Intern web research\n"
    "pip install chromadb sentence-transformers      # Vault RAG\n"
)


@dataclass(frozen=True)
class Row:
    label: str
    module: str
    available: bool
    problem: str = ""        # why not, when not — the Tk board never said


def importable(module: str) -> Tuple[bool, str]:
    """(ok, problem). Never raises."""
    try:
        importlib.import_module(module)
        return True, ""
    except Exception as exc:                              # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def availability() -> List[Row]:
    """One row per optional subsystem, in board order. Imports each module."""
    rows = []
    for label, module in SUBSYSTEMS:
        ok, problem = importable(module)
        rows.append(Row(label, module, ok, problem))
    return rows


def available(rows: List[Row], module: str) -> bool:
    return any(r.module == module and r.available for r in rows)


# ============================================================
# The event log
# ============================================================

#: Tag names the log colours by. `phase` is the default.
TAGS = ("phase", "result", "fail")


def classify(phase: str, msg: str) -> str:
    """The log tag for one ('agent_phase', phase, msg) event.

    Tk's rule, verbatim (engine `_agent_log_append`): PASS or ✓ is a result,
    FAIL or "error" (any case) is a failure, anything else is a phase line.
    Result wins when both match, as it does in Tk.
    """
    if "PASS" in msg or "✓" in msg:
        return "result"
    if "FAIL" in msg or "error" in msg.lower():
        return "fail"
    return "phase"


def format_line(phase: str, msg: str) -> str:
    return f"[{phase}] {msg}"


# ============================================================
# The session toggles
# ============================================================

@dataclass(frozen=True)
class AgentToggles:
    """The three switches, snapshotted.

    Frozen for the same reason CouncilOptions is: a turn runs on a worker, and
    a worker must read a snapshot taken on the GUI thread, never a checkbox.
    """
    use_coder_agent: bool = False
    use_intern_agent: bool = False
    use_rag: bool = False

    @classmethod
    def defaults(cls, rows: List[Row]) -> "AgentToggles":
        """On exactly when the subsystem is available — Tk's defaults."""
        return cls(use_coder_agent=available(rows, "coder_agent"),
                   use_intern_agent=available(rows, "intern_agent"),
                   use_rag=available(rows, "vault_rag"))
