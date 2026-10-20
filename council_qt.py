"""
council_qt.py — the Qt entry point, and the app the run-* launchers start.
The Tk one (council_gui_engine.py) is untouched and still runs: the launchers
use it with --tk / COUNCIL_UI=tk, or when PySide6 is not installed, and the
.exe build still packages it.

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
    # The memory scope as the user started the app, FIRST: the retired Tk
    # module sets COUNCIL_DEMO_SILO=1 when it is imported, and an import of
    # it anywhere in this process would otherwise turn the Qt app's memory
    # off ("This conversation only" -> "none") from then on
    # (council_core.memory_scope.pin_launch_environment).
    from council_core import memory_scope
    memory_scope.pin_launch_environment()

    # The main model, before anything can import council_engine. The run-*
    # launchers export the FIRST *.gguf they find when COUNCIL_GGUF_PATH is
    # unset, and that guess used to beat the model saved in the Models tab on
    # every launch (measured 2026-09-29: always granite-3.1-8b from ~/models,
    # whatever the user picked). The saved one now wins over the guess; a
    # COUNCIL_GGUF_PATH the user exported still wins over both. Guarded like
    # the rest of the startup chain: the app still opens on the guess (and the
    # Models tab can fix it) if this cannot run.
    try:
        import onboarding
        onboarding.apply_saved_gguf_path()
    except Exception as exc:                              # noqa: BLE001
        # apply_saved_gguf_path drops COUNCIL_GGUF_PATH_AUTO whatever it
        # decides; this path is the one where it never ran (onboarding did
        # not import). Dropped here too, by its literal name since onboarding
        # may be what failed: left in os.environ, every child the app spawns
        # inherits it, and a `python council_qt.py` started from that
        # environment treats the path it was handed as a launcher's guess
        # and swaps in the saved model.
        import os
        os.environ.pop("COUNCIL_GGUF_PATH_AUTO", None)
        print(f"[startup] saved main model not applied: {exc!r}", flush=True)

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
    from council_core import modes
    from council_qt import group_tabs, ui_settings
    from council_qt.tabs import registry

    # The grouped layout, if Council has delivered profiles.py. It is wired
    # BEFORE any tab registers so the first add_tab call can place its page
    # inside a group.
    #
    # Two gates are deliberate here:
    # (1) ``load_or_migrate`` only runs when profiles.py is present. The flat
    #     fallback must not write ``{"ui":{"profile":"full"}}`` to disk — the
    #     next ``modes.advanced()`` call reads that back as True and turns on
    #     six advanced tabs for every user who never asked (committing the
    #     vault to git, SSHing into a Pi, rewriting the model registry).
    # (2) The grouped-controller setup is wrapped in try/except: a broken
    #     profiles.py contract takes the LAUNCH down otherwise.
    profiles_mod = group_tabs.try_import_profiles()
    ui_section = None
    notice = None
    if profiles_mod is not None:
        try:
            ui_section, notice = ui_settings.load_or_migrate()
            controller = group_tabs.GroupedTabsController(window, profiles_mod)
            window.install_grouped_layout(controller)
            controller.apply_profile_state(ui_section)
            if notice:
                controller.show_once_notice(notice)
        except Exception as exc:                              # noqa: BLE001
            print(f"[council_qt] grouped layout setup failed, falling back "
                  f"to the flat shell: {exc!r}", flush=True)
            window.grouped = None
            ui_section = None

    # Advanced mode ADDS tabs; it does not unlock a setting. The six behind it
    # commit the vault to git, SSH into a Pi, or rewrite the model registry.
    #
    # With the grouped controller installed, we register them unconditionally
    # and let the profile filter hide them — otherwise a runtime
    # Demo → Developer switch cannot show them.
    # In the flat fallback, we use the Tk-era env/CLI gate only; the profile
    # file is deliberately not consulted here (see (1) above).
    if window.grouped is not None:
        show_advanced = True
    else:
        show_advanced = modes.advanced_env_or_cli()

    for title, factory, eager in registry(advanced=show_advanced):
        tab_id = _tab_id_of(factory)
        window.add_tab(title, lambda f=factory, w=window: f(w),
                       eager=eager, tab_id=tab_id)

    if window.grouped is not None and ui_section is not None:
        # Re-apply once every tab has registered so empty groups auto-hide
        # with their real inner counts.
        window.grouped.apply_profile_state(ui_section)


def _tab_id_of(factory) -> str:
    """Derive the spec-§1 id from a tab factory (``build_X`` → ``X``)."""
    name = getattr(factory, "__name__", "")
    return name[len("build_"):] if name.startswith("build_") else name


if __name__ == "__main__":
    raise SystemExit(main())
