"""
council_qt.py — the Qt entry point. The Tk one (council_gui_engine.py) is
untouched and stays the shipping app until the port is finished.

    python council_qt.py

Both entry points read the same vault and the same engine, so run ONE at a time.
There is no single-instance guard in this project yet (there never was), and two
copies would mean two chromadb clients on one store and two GGUF loads.

The startup chain — crash hooks, splash, reveal, onboarding — lives in
`council_qt.launch`, over the decisions in `council_core.startup`. It is out of
this file because a launch nobody can drive is a launch nobody checks, and this
is the one code path where every failure is invisible: no window, no error.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main() -> int:
    # Set before the first Qt import, like the Tk build does for its own
    # bootstrap: the AppUserModelID decides which taskbar button the window
    # groups under, and it has to be set before a window exists.
    import branding
    branding.set_app_user_model_id()

    from council_qt.launch import launch
    return launch(sys.argv, register=_register_tabs, shutdown=_shutdown)


def _shutdown() -> None:
    """The close-time work, shared with the Tk shell.

    WITHOUT THIS, CLOSING THE QT APP DID NONE OF IT. `on_close` is an empty
    base method and nothing assigned it, so the conversation log was never
    ended, the GPU-crash sentinel survived a clean run (forcing CPU on the next
    launch), the self-improvement analyzers never ran, and pooled DB
    connections were left to socket teardown. All four are invisible, which is
    why it went unnoticed for the whole of phase 5.

    Never raises: a user who clicks X and gets a traceback, or a window that
    refuses to close because an optional analyzer is unhappy, is worse than any
    of these jobs being skipped.
    """
    try:
        from council_core import shutdown
        report = shutdown.close_session()
        for line in report.lines():
            print(line, flush=True)
        for failure in report.failures:
            print(f"[shutdown] skipped — {failure}", flush=True)
    except Exception as exc:                              # noqa: BLE001
        print(f"[shutdown] failed: {exc!r}", flush=True)


def _register_tabs(window) -> None:
    """Every tab that has been ported, in the order the Tk shell shows them.

    A tab absent from here is simply not in this build — which is what makes the
    port shippable in phases rather than as one flip. The Tk shell already does
    the same thing for advanced mode, where six tabs are absent.
    """
    from council_qt.tabs import REGISTRY
    for title, factory, eager in REGISTRY:
        window.add_tab(title, lambda f=factory, w=window: f(w), eager=eager)


if __name__ == "__main__":
    raise SystemExit(main())
