"""
council_core.startup — what happens before the window, and in what order.

The Tk `main()` is 90 lines of sequencing with four hard-won rules buried in
it, each written as a comment above the line that implements it. Comments do
not survive a port. These do.

THE ORDER IS THE PRODUCT
    1. crash hooks, BEFORE anything else exists
    2. build the window (hidden)
    3. splash for at least MIN_SPLASH_MS
    4. reveal
    5. onboarding, after the window is up

Install the crash hook second and a failure during construction is an
unhandled traceback in a console the user may not even have. Reveal before the
build finishes and they watch an empty window fill in. Run onboarding before
the reveal and a modal opens over a window that is not there yet.

A WITHDRAWN WINDOW THAT NEVER REAPPEARS IS A DEAD APP
So the reveal has three independent guarantees: it is idempotent, the splash's
dismissal calls it, AND a backstop timer calls it anyway. Two of those exist
solely because the first two can fail together, and the failure mode is a
process with no window and no error.

INTERACTIVE HOSTS TAKE A DIFFERENT PATH
Under Spyder or IPython the host already owns a Qt event loop, and two things
from the normal path are unsafe in that process: pumping a second loop to
animate a splash, and initialising torch/CUDA on a BACKGROUND thread — which
segfaults the kernel about thirty seconds in. That is the "no tabs, then the
kernel dies" report. Detected here so the Qt shell cannot forget it.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional

#: How long the splash stays up at minimum. Not a delay for its own sake: the
#: window is built behind it, and a splash that vanished the instant a fast
#: machine finished would read as a flicker rather than as a launch.
MIN_SPLASH_MS = 1500

#: How long after the intended reveal the backstop fires. Long enough that it
#: never races the normal path, short enough that a stranded window is a pause
#: rather than a hang.
REVEAL_BACKSTOP_MS = 1500

#: The window is up before this runs, because it opens a modal and a modal
#: needs a parent that exists.
ONBOARDING_DELAY_MS = 500


def is_interactive_host() -> bool:
    """Whether we are inside Spyder or IPython rather than a normal launch.

    The host already owns an event loop in this process. Two things from the
    normal startup path are unsafe here: animating a splash by pumping a
    second loop, and auto-starting the RAG indexer, which initialises
    torch/CUDA on a background thread and segfaults the kernel about thirty
    seconds later.

    Vault keyword search still works either way; full-document semantic RAG is
    built on demand from the Vault tab. A normal .exe or CLI launch is
    unaffected.
    """
    try:
        from IPython import get_ipython          # type: ignore
        if get_ipython() is not None:
            return True
    except Exception:                            # noqa: BLE001
        pass
    return "spyder_kernels" in sys.modules or "spyder" in sys.modules


def splash_remaining_ms(started_monotonic: Optional[float],
                        now_monotonic: float,
                        minimum_ms: int = MIN_SPLASH_MS) -> int:
    """How much longer the splash should stay up.

    Never negative: a build slower than the minimum has already paid the time,
    and scheduling a negative delay is either an immediate fire or an error
    depending on the toolkit.
    """
    if started_monotonic is None:
        return minimum_ms
    elapsed_ms = (now_monotonic - started_monotonic) * 1000.0
    return max(0, int(minimum_ms - elapsed_ms))


@dataclass
class Plan:
    """What this launch should do, decided before anything is built."""
    interactive: bool = False
    show_splash: bool = True
    start_rag: bool = True
    onboarding: bool = False
    #: Why onboarding is needed, for the log. Empty when it is not.
    onboarding_reason: str = ""


def plan(vault_dir: Any, *, force_splash: Optional[bool] = None) -> Plan:
    """Decide the launch before building anything.

    Deciding first, rather than checking conditions at each step, is what makes
    a launch reproducible: everything downstream reads this object.
    """
    interactive = is_interactive_host()
    result = Plan(
        interactive=interactive,
        show_splash=(not interactive) if force_splash is None
        else bool(force_splash),
        # NOT under an interactive host: off-main-thread CUDA init segfaults
        # the kernel about thirty seconds in.
        start_rag=not interactive,
    )
    result.onboarding, result.onboarding_reason = _onboarding_needed(vault_dir)
    return result


def _onboarding_needed(vault_dir: Any) -> tuple:
    """(needed, reason). Never raises — a broken vault must not stop a launch.

    A failure here reports "not needed" rather than "needed": showing the setup
    wizard because a path could not be read would walk a configured user back
    through setup they already did.
    """
    try:
        import onboarding
        if onboarding.needs_onboarding(Path(vault_dir)):
            return True, "no model is configured yet"
    except Exception as exc:                     # noqa: BLE001
        return False, f"could not check: {exc!r}"
    return False, ""


class Reveal:
    """Show the window once, however many times it is asked.

    THREE INDEPENDENT GUARANTEES call this: the splash's dismissal, a backstop
    timer, and the interactive path's direct call. They exist because the first
    two can fail together and the failure mode is a process with a hidden
    window and no error anywhere.

    A failure is REPORTED, never swallowed. A blank session with a silent
    exception behind it is the hardest possible thing to diagnose, and the Tk
    version prints the reason for exactly that reason.
    """

    def __init__(self, show: Callable[[], None],
                 fallback: Optional[Callable[[], None]] = None,
                 report: Optional[Callable[[str], None]] = None):
        self._show = show
        #: The minimum that still counts as revealed. The full reveal raises
        #: the window and takes focus, and either can fail on a window manager
        #: that refuses focus stealing — while simply making the window
        #: VISIBLE still succeeds. A visible unfocused window is a working app;
        #: a hidden one is not.
        self._fallback = fallback
        self._report = report or (lambda message: print(message))
        self.done = False
        #: Every attempt, so a test can prove the guarantees are independent.
        self.attempts: List[str] = []

    def __call__(self, source: str = "") -> bool:
        """True if this call is the one that revealed the window."""
        self.attempts.append(source)
        if self.done:
            return False
        self.done = True
        try:
            self._show()
            return True
        except Exception as exc:                 # noqa: BLE001
            # Reported, never swallowed: a blank session with a silent
            # exception behind it is the hardest thing there is to diagnose.
            self._report(f"[Splash] reveal failed, showing window directly: "
                         f"{exc!r}")
        if self._fallback is None:
            return False
        try:
            self._fallback()
            return True
        except Exception:                        # noqa: BLE001
            return False
