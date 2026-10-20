"""
council_core.modes — which build this is.

ADVANCED MODE ADDS TABS, IT DOES NOT UNLOCK A SETTING
Six tabs exist only under it: Librarian, Nodes, Apothecary and their
neighbours. They are the ones that commit the whole vault to git, SSH into a
Raspberry Pi, or rewrite the model registry — operations that are fine when
you went looking for them and alarming when you did not.

So a Qt build that registers them unconditionally is not a port; it is a
behaviour change that puts "git commit my entire vault" in front of everyone.

READ LIVE, NOT CACHED AT IMPORT
The Tk build evaluates this once at module import, which is fine for a process
that never changes it. A test that sets the variable and a launcher that sets
it are the same case, and neither controls import order.

NEW SOURCE OF TRUTH: THE ACTIVE PROFILE
The spec in TAB_CONSOLIDATION_SPEC.md moves advanced-mode under the per-user
profile — Demo switches it off, Developer and Full turn it on, Custom carries
its own flag. ``advanced()`` reads the profile for its default answer; the two
long-standing overrides (``COUNCIL_ADVANCED=1`` and ``--advanced``) still win,
because they are how a developer forces advanced on without touching settings
and how the test suite drives the function.
"""
from __future__ import annotations

import os
import sys
from typing import Optional, Sequence

#: What the environment variable accepts, matching the Tk build exactly.
TRUE_VALUES = ("1", "true", "yes")

ENV_VAR = "COUNCIL_ADVANCED"
FLAG = "--advanced"


def advanced(argv: Optional[Sequence[str]] = None) -> bool:
    """Whether this build shows the advanced tabs.

    Returns True when ``COUNCIL_ADVANCED`` is set to a truthy value, or when
    ``--advanced`` appears in ``argv`` (``sys.argv`` by default), or when the
    user's active profile says so. The first two mirror the Tk-era overrides
    and keep working the way the existing tests exercise them; the third is
    the new source of truth for a normal launch.
    """
    if advanced_env_or_cli(argv):
        return True
    return _advanced_from_profile()


def advanced_env_or_cli(argv: Optional[Sequence[str]] = None) -> bool:
    """Just the Tk-era overrides, without reading the profile file.

    Used by the Qt entry point's FLAT fallback (no ``council_qt.profiles``
    yet), where we deliberately do not migrate ``ui_settings.json``: a fresh
    install would be promoted to Full and the six advanced tabs would appear
    for every user who never opted in.
    """
    if os.environ.get(ENV_VAR, "").strip().lower() in TRUE_VALUES:
        return True
    if FLAG in list(argv if argv is not None else sys.argv):
        return True
    return False


def _advanced_from_profile() -> bool:
    """What the user's saved profile says. Never raises on a bad file.

    Imported lazily: ``council_qt.ui_settings`` depends on PySide6 by way of
    its neighbours in the ``council_qt`` package, and ``modes`` is reached
    from tests and command-line tools that must not pull Qt in.

    This function READS, it does not migrate. A launch that opens the window
    migrates once through ``load_or_migrate``; code that only wants the live
    answer (every ``modes.advanced()`` call under the tab-registration path)
    reads the on-disk section as it is. No file on disk → False, which keeps
    tests that monkeypatch away the environment overrides deterministic and
    matches the Tk-era default (no advanced tabs for anyone who did not ask).
    """
    try:
        from council_qt import ui_settings
    except Exception:                                        # noqa: BLE001
        return False
    try:
        raw = ui_settings._read_raw()                        # noqa: SLF001
    except Exception:                                        # noqa: BLE001
        return False
    section = raw.get("ui") if isinstance(raw, dict) else None
    if not isinstance(section, dict):
        return False
    try:
        return ui_settings.current_advanced_mode(section)
    except Exception:                                        # noqa: BLE001
        return False
