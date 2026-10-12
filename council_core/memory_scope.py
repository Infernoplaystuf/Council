"""
council_core.memory_scope — what a model may remember when it answers.

THREE SCOPES, ONE RULE
Every prompt a personality sends is stitched from blocks (council_engine.
PersonalityModel.respond): role memory, the user profile, project memory, a
prior session, the recent conversation, the council context, and the task.
Which of the remembered blocks may go in is ONE setting:

    none          nothing remembered. COUNCIL_DEMO_SILO=1 means this, and the
                  Tk shell sets SILO by default — its behaviour is unchanged.
    conversation  "This conversation only" (the user's decision of 6 October
                  2026): this session's earlier turns (the RECENT
                  CONVERSATION block) and nothing older. No older session, no
                  role or project memory, no profile. The ONE way an older
                  session reaches a prompt is the user loading it on purpose
                  (Load as Prior in the Sessions tab) — and that shows in the
                  transcript. The Qt shell's default.
    all           everything the engine has, as when SILO is unset.

Before this there were only "all" and "none" (SILO), so the Qt shell — which
never set SILO — fed the memory and profile the Tk shell had written into
every Qt prompt, and could not turn the profile off.

HOW IT IS DECIDED
    1. COUNCIL_MEMORY_SCOPE, when it names a scope — an explicit choice wins.
    2. COUNCIL_DEMO_SILO truthy -> "none" (the older spelling of it).
    3. The caller's default: "all" for the engine (what it did with SILO
       unset), "conversation" for the Qt shell.

The Tk module sets SILO when it is imported (set_tk_default) and marks it as
its own default; the Qt shell (for_qt) does not read a SILO so marked, so an
import of the Tk module cannot turn Qt's memory off. A SILO the user set is
read by both.

An unknown COUNCIL_MEMORY_SCOPE is not guessed at: it is ignored and the rest
of the rule applies. An unknown scope handed to `blocks()` is treated as
"none" — the safe reading of a value nobody understands is "remember nothing".

Toolkit-free and engine-free, so a view can show the scope without loading the
engine.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

NONE = "none"
CONVERSATION = "conversation"
ALL = "all"
SCOPES = (NONE, CONVERSATION, ALL)

#: What the Qt shell uses unless the environment says otherwise.
QT_DEFAULT = CONVERSATION
#: What the engine uses when nothing says otherwise — its behaviour with SILO
#: unset, kept so a caller that never heard of scopes sees no change.
ENGINE_DEFAULT = ALL

_TRUTHY = ("1", "true", "yes", "on")


def silo(environ: Optional[Mapping[str, str]] = None) -> bool:
    """COUNCIL_DEMO_SILO, read the way the engine has always read it."""
    env = os.environ if environ is None else environ
    return env.get("COUNCIL_DEMO_SILO", "").strip().lower() in _TRUTHY


def resolve(environ: Optional[Mapping[str, str]] = None, *,
            default: str = ENGINE_DEFAULT) -> str:
    """The scope the environment asks for, else ``default``. See the module
    note for the order."""
    env = os.environ if environ is None else environ
    explicit = env.get("COUNCIL_MEMORY_SCOPE", "").strip().lower()
    if explicit in SCOPES:
        return explicit
    if silo(env):
        return NONE
    return default if default in SCOPES else NONE


#: Set beside COUNCIL_DEMO_SILO when the Tk module set SILO as ITS default
#: (set_tk_default), rather than the user.
TK_DEFAULT_MARK = "COUNCIL_DEMO_SILO_SOURCE"
_TK_DEFAULT = "tk-default"


def set_tk_default(environ: Optional[dict] = None) -> None:
    """The Tk shell's default — SILO on — unless the environment already
    says something; marked as the Tk shell's when it is set here.

    The Tk module used to run os.environ.setdefault("COUNCIL_DEMO_SILO",
    "1") when it was IMPORTED, and the Qt shell reads the environment when
    its conversation is built — so any import of the Tk module before the
    Council tab (a probe, a test file, a future eager import) silently
    switched Qt to "Memory: off" (found in review). The mark lets for_qt
    tell the Tk shell's default from a SILO the user set. The Tk shell
    itself — and anything it starts — still sees SILO=1, as before."""
    env = os.environ if environ is None else environ
    if "COUNCIL_DEMO_SILO" not in env:
        env["COUNCIL_DEMO_SILO"] = "1"
        env[TK_DEFAULT_MARK] = _TK_DEFAULT


def for_qt(environ: Optional[Mapping[str, str]] = None) -> str:
    """The Qt shell's scope: "This conversation only" unless the environment
    says otherwise (COUNCIL_MEMORY_SCOPE, or SILO for none). A SILO the Tk
    module set as its own default (set_tk_default) is not the user's choice
    and is not read here."""
    env = os.environ if environ is None else environ
    if env.get(TK_DEFAULT_MARK) == _TK_DEFAULT:
        env = {k: v for k, v in env.items() if k != "COUNCIL_DEMO_SILO"}
    return resolve(env, default=QT_DEFAULT)


@dataclass(frozen=True)
class Blocks:
    """Which remembered blocks a prompt may carry."""
    role_memory: bool
    project_memory: bool
    profile: bool
    recent_conversation: bool
    #: Only a prior session the user LOADED — see `blocks`.
    prior_session: bool


def blocks(scope: str, *, prior_loaded: bool = False) -> Blocks:
    """What ``scope`` lets into a prompt.

    ``prior_loaded`` is whether the user loaded an older session into this
    conversation on purpose (Load as Prior). In "conversation" that is the
    only way an older session gets in; in "all" it is how it always got in;
    in "none" nothing does, loaded or not (the Tk shell's SILO behaviour)."""
    if scope == ALL:
        return Blocks(True, True, True, True, prior_loaded)
    if scope == CONVERSATION:
        return Blocks(False, False, False, True, prior_loaded)
    return Blocks(False, False, False, False, False)


def describe(scope: str) -> str:
    """One plain line for the UI."""
    if scope == CONVERSATION:
        return "Memory: this conversation only"
    if scope == ALL:
        return "Memory: everything learned"
    return "Memory: off"


def explain(scope: str) -> str:
    """The tooltip under `describe`."""
    if scope == CONVERSATION:
        return ("Follow-up questions see this conversation's earlier turns. "
                "Older conversations, what the council has learned about "
                "you and your projects, and your profile are not used. "
                "To bring one older conversation in, load it from the "
                "Sessions tab (Load as Prior).")
    if scope == ALL:
        return ("Follow-up questions see this conversation's earlier turns, "
                "and what the council has learned (role and project memory, "
                "your profile) is used too. Set by COUNCIL_MEMORY_SCOPE.")
    return ("Each question is answered on its own: no earlier turns, no "
            "older conversations, nothing learned. Set by "
            "COUNCIL_MEMORY_SCOPE=none or COUNCIL_DEMO_SILO.")
