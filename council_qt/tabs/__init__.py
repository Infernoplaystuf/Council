"""
council_qt.tabs — the ported tabs, and only the ported tabs.

REGISTRY is the build's tab list: (title, factory, eager). A tab that has not
been ported is simply absent, so an in-progress Qt build is a whole app with
fewer tabs rather than a broken one. The Tk shell already behaves this way —
advanced mode adds six tabs and the other builds run fine without them.

`eager=True` is for a tab that must exist before the user visits it, because a
background worker posts to it regardless. Everything else is built on first
show, which is what lets the Qt build skip the splash-pumping the Tk startup
needs to hide its all-at-once construction.

PORTING ORDER (docs/qt_full_port_scope.md §3): foundation first, then Vault as
the calibration pilot, then Council/Grapher/Dream3D, then the ten remaining
default tabs, then the Designer, then advanced.
"""
from __future__ import annotations

from .council import build_council
from .diagnostics import build_diagnostics
from .changelog import build_changelog
from .forge import build_forge
from .jobs import build_jobs
from .lens import build_lens
from .models import build_models
from .sessions import build_sessions
from .specialists import build_specialists
from .speech import build_speech
from .vault import build_vault

#: (title, factory, eager). Factories take the CouncilWindow.
REGISTRY = [
    ("⚖ Council", build_council, True),
    ("🗄 Vault", build_vault, False),
    ("🔍 Lens", build_lens, False),
    ("🛠 Tool Creation", build_forge, False),
    ("🎙 Speech", build_speech, False),
    ("🎯 Agent Jobs", build_jobs, False),
    ("📜 Changelog", build_changelog, False),
    ("🕓 Sessions", build_sessions, False),
    ("🎓 Specialists", build_specialists, False),
    ("🇺🇸 Models", build_models, False),
    ("Diagnostics", build_diagnostics, False),
]

__all__ = ["REGISTRY", "build_changelog", "build_council",
           "build_diagnostics", "build_forge", "build_jobs",
           "build_lens", "build_models", "build_sessions",
           "build_specialists", "build_speech", "build_vault"]
