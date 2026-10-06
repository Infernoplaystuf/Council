"""
council_core.council_options — the Council tab's switches, and the rules on them.

WHY THIS IS NOT JUST A LIST OF BOOLEANS
Eight checkboxes sit under the Council tab's input box. Each one has a default,
and four of them disappear in DEMO_MODE because the home build is "ask a
question, get an answer" rather than a multi-personality deliberation. Those
rules currently live as `if not _demo:` scattered through three hundred lines of
widget construction, and the defaults live in eight separate `tk.BooleanVar(...)`
calls next to them.

A second front end has to reproduce all of it exactly. Get one default backwards
and the app behaves differently in a way no test would catch, because no test
looks at a checkbox's initial state — it is the kind of thing found by a user
saying "it used to stream".

So the switches are described once, here, and each front end builds widgets from
the description.

THE ONE RULE THAT IS NOT ABOUT WIDGETS
DEMO_MODE hides the deliberation toggle AND forces deliberation off at send
time, regardless of what the toggle says. Those are two separate pieces of code
in the Tk shell and only the second one matters — a hidden checkbox that is
still honoured would be a bug you could not see. `effective()` applies the
run-time rule, and it is the function the deliberation path should ask rather
than reading the raw switch.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Mapping, Optional, Tuple


@dataclass(frozen=True)
class Switch:
    """One checkbox: what it is called, what it starts as, when it is shown."""
    key: str
    label: str
    default: bool
    #: False when the home build has no use for it. The switch still EXISTS —
    #: code that reads it keeps working — it is only absent from the toolbar.
    shown_in_demo: bool = True
    #: Which toolbar row it belongs to. Row 2 is the personality controls.
    row: int = 1
    hint: str = ""
    #: False while no turn reads it. The switch is still SHOWN — a removed
    #: control is a feature nobody can tell is coming — but disabled, saying
    #: "not available in this build yet": a live checkbox that changes
    #: nothing reads as a broken one.
    available: bool = True
    #: Said after "not available in this build yet" when there is more to
    #: say — what happens instead, so the disabled box is not misread.
    unavailable_why: str = ""

    @property
    def name(self) -> str:
        """The label without its decoration ("👤 Profile" -> "Profile"), for
        sentences such as "Profile — not available in this build yet."."""
        return re.sub(r"[^\w\s&-]", "", self.label).strip()


#: The Council toolbar, in the order the Tk shell packs it. Order is part of the
#: description: users find a checkbox by position, and a port that sorts them
#: alphabetically has moved every one of them.
#:
#: Only Deliberation and Stream tokens are read by the Qt turn today. The rest
#: are available=False until the per-role context carrier (Batch 3 in
#: docs/qt_migration/remaining_scope_2026-10-06.md) wires them — measured in
#: review: toggling Tools, Fill IDE, Profile or Adversarial left every request
#: the models received byte-for-byte the same. Only the Qt tab reads this
#: table; the Tk toolbar is its own code, and those switches work there.
SWITCHES: Tuple[Switch, ...] = (
    Switch("deliberate", "Deliberation", True, shown_in_demo=False),
    # run_turn is given enable_tools but no tool table, so nothing is called.
    Switch("tools", "Tools", False, available=False,
           unavailable_why="The council has no tools to call yet."),
    Switch("fill_ide", "Fill IDE", True, available=False,
           unavailable_why="Code in an answer is not copied to the IDE tab "
                           "yet."),
    Switch("stream", "Stream tokens", True),
    # Its old tooltip promised "Unchecking skips it on the next message". In
    # Qt nothing reads the box — Tk's sets COUNCIL_QUIRKS_APPLY — so a profile
    # the Tk app compiled reached every prompt whichever way it was ticked.
    # The box now SHOWS what the engine does (profile_applied) and cannot be
    # changed.
    Switch("use_profile", "👤 Profile", True,
           hint="Applies what the council has learned about how you like "
                "answers. Unchecking skips it on the next message while "
                "learning continues underneath.",
           available=False,
           unavailable_why="A learned profile is applied while "
                           "COUNCIL_QUIRKS_APPLY is not 0, as the box shows."),
    Switch("adversarial", "Adversarial", False, shown_in_demo=False,
           available=False),
    Switch("judge_panel", "Judge panel ✦", False, shown_in_demo=False,
           available=False),
    Switch("robust_voices", "Robust voices ✦", False, shown_in_demo=False, row=2,
           hint="gives each personality a distinct character and tone",
           available=False),
)

SWITCHES_BY_KEY: Dict[str, Switch] = {s.key: s for s in SWITCHES}


def visible_switches(demo_mode: bool, row: Optional[int] = None) -> List[Switch]:
    """The switches a toolbar should show, in order."""
    return [s for s in SWITCHES
            if (s.shown_in_demo or not demo_mode)
            and (row is None or s.row == row)]


def profile_applied(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Whether the engine injects a learned user profile into prompts.

    The SAME rule as council_engine.user_profile_apply_enabled (on unless
    COUNCIL_QUIRKS_APPLY is 0/false/no/off; a test holds the two together),
    read here so the Council tab can show it without importing the engine,
    which it otherwise loads only when a turn needs a model."""
    env = os.environ if environ is None else environ
    return env.get("COUNCIL_QUIRKS_APPLY", "1").strip().lower()         not in ("0", "false", "no", "off")


@dataclass
class CouncilOptions:
    """What the switches currently say.

    Mutable on purpose: this is live UI state, and both front ends write to it
    as the user clicks. `effective()` returns the frozen, rule-applied copy that
    the deliberation path should read.
    """
    deliberate: bool = True
    tools: bool = False
    fill_ide: bool = True
    stream: bool = True
    use_profile: bool = True
    adversarial: bool = False
    judge_panel: bool = False
    robust_voices: bool = False

    @classmethod
    def defaults(cls, demo_mode: bool = False,
                 profile_enabled: Optional[bool] = None) -> "CouncilOptions":
        """The state the tab opens in.

        ``deliberate`` follows DEMO_MODE, matching
        ``tk.BooleanVar(value=not _demo)``. ``use_profile`` is read from the
        engine rather than hard-coded, because a pre-set COUNCIL_QUIRKS_APPLY
        in the shell is meant to survive into the session.
        """
        return cls(
            deliberate=not demo_mode,
            use_profile=True if profile_enabled is None else bool(profile_enabled),
        )

    def effective(self, demo_mode: bool = False) -> "CouncilOptions":
        """This state with the run-time rules applied.

        DEMO_MODE forces single-personality direct mode regardless of the
        toggle. Reading `options.deliberate` straight in the send path would
        honour a checkbox the home build does not even display.
        """
        if demo_mode:
            return replace(self, deliberate=False)
        return self

    def as_dict(self) -> Dict[str, bool]:
        return {s.key: getattr(self, s.key) for s in SWITCHES}


# ============================================================
# The specialist pin
# ============================================================

#: What the dropdown shows when nothing is pinned. The Tk shell compares
#: against this literal in two places, so it is a constant rather than a string
#: repeated wherever someone needs it.
AUTO_LABEL = "Auto"


def pinned_specialist_id(choice: str,
                         by_label: Dict[str, str]) -> Optional[str]:
    """The specialist id a dropdown selection means, or None for automatic.

    Separated because "Auto" is not a specialist and a front end that forgets
    that pins a specialist called Auto onto every query.
    """
    if not choice or choice == AUTO_LABEL:
        return None
    return by_label.get(choice)


def specialist_choices(names: List[str]) -> List[str]:
    """Dropdown entries: automatic first, then the specialists as given.

    Not sorted here — the registry's order is meaningful and re-sorting it in
    the view is how two front ends end up offering different lists.
    """
    return [AUTO_LABEL] + list(names)


# ============================================================
# The per-query model override
# ============================================================

#: The backends the override dropdown offers, in the Tk shell's order.
BACKEND_CHOICES: Tuple[str, ...] = (
    "(default)", "local_general_primary", "local_general_alt",
    "local_coder_primary", "local_coder_fast", "local_judge_fast",
    "local_peasant_fast", "local_fast",
)

DEFAULT_BACKEND = "(default)"


def backend_override(choice: str) -> Optional[str]:
    """The backend a selection means, or None to leave the routing alone."""
    if not choice or choice == DEFAULT_BACKEND:
        return None
    return choice
