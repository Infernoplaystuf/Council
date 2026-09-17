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

from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Optional, Tuple


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


#: The Council toolbar, in the order the Tk shell packs it. Order is part of the
#: description: users find a checkbox by position, and a port that sorts them
#: alphabetically has moved every one of them.
SWITCHES: Tuple[Switch, ...] = (
    Switch("deliberate", "Deliberation", True, shown_in_demo=False),
    Switch("tools", "Tools", False),
    Switch("fill_ide", "Fill IDE", True),
    Switch("stream", "Stream tokens", True),
    Switch("use_profile", "👤 Profile", True,
           hint="Applies what the council has learned about how you like "
                "answers. Unchecking skips it on the next message while "
                "learning continues underneath."),
    Switch("adversarial", "Adversarial", False, shown_in_demo=False),
    Switch("judge_panel", "Judge panel ✦", False, shown_in_demo=False),
    Switch("robust_voices", "Robust voices ✦", False, shown_in_demo=False, row=2,
           hint="gives each personality a distinct character and tone"),
)

SWITCHES_BY_KEY: Dict[str, Switch] = {s.key: s for s in SWITCHES}


def visible_switches(demo_mode: bool, row: Optional[int] = None) -> List[Switch]:
    """The switches a toolbar should show, in order."""
    return [s for s in SWITCHES
            if (s.shown_in_demo or not demo_mode)
            and (row is None or s.row == row)]


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
