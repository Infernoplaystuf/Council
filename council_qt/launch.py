"""
council_qt.launch — the startup chain, wired up.

`council_core.startup` decides what this launch should do; this runs it. The
order is the product, and it is stated there:

    crash hooks → build hidden → splash → reveal → onboarding

WHY THE SPLASH IS PUMPED BY HAND
The window is built BEFORE `app.exec()`, so the event loop is not running and
the splash's own timer cannot fire. The cog would freeze for the whole of the
construction — exactly the stretch it exists to cover. So the build pumps it,
one frame per tab, which is the same trick the Tk startup uses and for the same
reason.

THE REVEAL HAS THREE CALLERS AND THAT IS DELIBERATE
The splash's dismissal, a backstop timer, and — under an interactive host —
a direct call. Two are redundant on a normal launch. They exist because the
first two can fail together, and a window that never appears is a dead app with
no error anywhere.
"""
from __future__ import annotations

import sys
import time
from typing import Any, Callable, Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from council_core import paths, startup

from . import theme
from .splash import NoSplash, show_splash
from .window import CouncilWindow


def launch(argv: Optional[list] = None, *,
           register: Optional[Callable[[Any], None]] = None,
           shutdown: Optional[Callable[[], None]] = None) -> int:
    """Start the app and run its event loop. Returns the exit code."""
    app, window, plan = build(argv, register=register, shutdown=shutdown)
    code = app.exec()
    window.request_close()          # idempotent; covers exec() returning early
    return code


def build(argv: Optional[list] = None, *,
          register: Optional[Callable[[Any], None]] = None,
          shutdown: Optional[Callable[[], None]] = None,
          window_factory: Optional[Callable[[], Any]] = None):
    """Everything up to `app.exec()`. Separated so a test can run the chain.

    A launch a test cannot drive is a launch nobody checks, and this is the one
    code path where every failure is invisible: no window, no error.
    """
    vault = paths.vault_dir()
    # BEFORE the GUI exists, so a failure during construction is captured
    # rather than printed to a console the user may not have.
    _install_crash_hooks(vault)

    app = QApplication.instance() or QApplication(list(argv or sys.argv))
    theme.apply(app, "dark")

    decided = startup.plan(vault)
    splash = (show_splash(manual=True) if decided.show_splash
              else NoSplash())
    started = time.monotonic()

    window = (window_factory or CouncilWindow)()
    if shutdown is not None:
        window.on_close = shutdown
    # aboutToQuit fires however the app ends — the window's X, quit(), or a
    # session logout — so teardown hangs off that rather than off one code path
    # remembering to call it.
    app.aboutToQuit.connect(window.request_close)

    if register is not None:
        register(window)
    splash.pump()

    _schedule_reveal(window, splash, decided, started)
    if decided.onboarding:
        _schedule_onboarding(window, vault, decided)
    return app, window, decided


# ======================================================================
# The steps
# ======================================================================

def _install_crash_hooks(vault) -> None:
    """Never fatal. A crash reporter that stops the launch is worse than no
    crash reporter, because the failure it causes is the one nobody expected."""
    try:
        import crash_reporter
        crash_reporter.install(paths.ensure(vault), on_crash=_report_crash)
    except Exception as exc:                             # noqa: BLE001
        print(f"[startup] crash hooks unavailable: {exc!r}", flush=True)


def _report_crash(crash_path) -> None:
    """Called from the crashing thread. Says where the report went.

    It does NOT open a dialog. The Tk version defers one to the main loop;
    doing that from here would mean building a widget from whichever thread
    happened to die, which on Windows is an access violation rather than an
    error message.
    """
    print(f"[crash] report written to {crash_path}", flush=True)


def _schedule_reveal(window, splash, decided, started: float) -> None:
    reveal = startup.Reveal(show=lambda: _show(window),
                            fallback=window.show)
    if decided.interactive:
        # No splash and no deferred timers: the host's loop may pump them only
        # intermittently, and a window that appears "eventually" reads as one
        # that never appeared.
        reveal("interactive")
        return

    remaining = startup.splash_remaining_ms(started, time.monotonic())
    QTimer.singleShot(remaining,
                      lambda: splash.dismiss(on_done=lambda: reveal("splash")))
    QTimer.singleShot(remaining + startup.REVEAL_BACKSTOP_MS,
                      lambda: reveal("backstop"))


def _show(window) -> None:
    window.show()
    window.raise_()
    window.activateWindow()


def _schedule_onboarding(window, vault, decided) -> None:
    """After the window, because it opens a modal and a modal needs a parent.

    THE WIZARD ITSELF IS NOT PORTED YET. `onboarding.run_if_needed` takes a
    tk.Tk parent, so calling it with a QMainWindow would raise into an except
    and report "skipped" — which reads as a decision rather than as a gap.

    So until a Qt wizard exists, this SAYS what is unconfigured and where to
    fix it. A user left silently without a model gets an app whose every
    answer is "no judge model is loaded", and no idea why.

    A host that has a wizard supplies `open_onboarding`; the moment one exists
    it is wired by giving the window that attribute.
    """
    def run() -> None:
        opener = getattr(window, "open_onboarding", None)
        if opener is not None:
            try:
                opener(vault_dir=vault, on_complete=None)
            except Exception as exc:                     # noqa: BLE001
                # An app that will not start because its optional setup wizard
                # is unhappy has turned a nudge into a wall.
                print(f"[startup] onboarding failed: {exc!r}", flush=True)
            return
        message = (f"Setup needed — {decided.onboarding_reason}. "
                   f"Set a model in the Models tab. "
                   f"(The guided wizard is Tk-only for now.)")
        print(f"[startup] {message}", flush=True)
        try:
            window.set_status(message)
        except Exception:                                # noqa: BLE001
            pass

    QTimer.singleShot(startup.ONBOARDING_DELAY_MS, run)
