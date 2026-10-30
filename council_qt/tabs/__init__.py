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

THE TABS ARE IMPORTED WHEN THE REGISTRY IS FIRST ASKED FOR, not when this
package is. Importing ONE tab's module (`from council_qt.tabs.capture import
CaptureTab`) runs this file first, and it imported every tab — the Grapher's
matplotlib among them — so the Capture tab, and its tests, could not even be
imported where matplotlib is not installed (the `pylon` env, measured:
ModuleNotFoundError: No module named 'matplotlib' collecting
tests/test_capture_tab.py). REGISTRY, ADVANCED_REGISTRY and every build_*
name resolve on first use (module __getattr__, PEP 562) to the same objects
as before: the window's tab list still imports every tab when it is built.
"""

from __future__ import annotations

import importlib
from typing import Any, Dict, List, Tuple

#: Each tab factory's name, and the module of this package that holds it.
_FACTORIES: Dict[str, str] = {
    "build_agent_creator": "agent_creator",
    "build_agents": "agents",
    "build_apothecary": "apothecary",
    "build_capture": "capture",
    "build_connections": "connections",
    "build_build": "build",
    "build_council": "council",
    "build_council_map": "council_map",
    "build_fanout": "fanout",
    "build_designer": "designer",
    "build_diagnostics": "diagnostics",
    "build_docs": "docs",
    "build_dream3d": "dream3d",
    "build_librarian": "librarian",
    "build_nodes": "nodes",
    "build_quant_duel": "quant_duel",
    "build_vault_health": "vault_health",
    "build_changelog": "changelog",
    "build_forge": "forge",
    "build_grapher": "grapher",
    "build_jobs": "jobs",
    "build_lens": "lens",
    "build_ledger": "ledger",
    "build_models": "models",
    "build_sessions": "sessions",
    "build_specialists": "specialists",
    "build_speech": "speech",
    "build_var_graph": "var_graph",
    "build_vault": "vault",
}

#: (title, factory name, eager) — REGISTRY, with the factories by name.
_REGISTRY: List[Tuple[str, str, bool]] = [
    ("⚖ Council", "build_council", True),
    ("📷 Capture", "build_capture", False),
    ("🗄 Vault", "build_vault", False),
    ("🕸 Connections", "build_connections", False),
    ("🔍 Lens", "build_lens", False),
    ("🛠 Tool Creation", "build_forge", False),
    ("🎙 Speech", "build_speech", False),
    ("🎯 Agent Jobs", "build_jobs", False),
    ("📜 Changelog", "build_changelog", False),
    ("🕓 Sessions", "build_sessions", False),
    ("🎓 Specialists", "build_specialists", False),
    ("🧩 Agent Creator", "build_agent_creator", False),
    ("🇺🇸 Models", "build_models", False),
    ("🕸 Council Map", "build_council_map", False),
    ("🧱 Build", "build_build", False),
    ("🧩 Fan-out", "build_fanout", False),
    # Eager: its 15-minute timer runs due jobs without the tab being opened
    # (only when the user turned the duel and its schedule on).
    ("📈 Quant Duel", "build_quant_duel", True),
    ("📖 Docs", "build_docs", False),
    ("📊 Grapher", "build_grapher", False),
    ("📑 Ledger", "build_ledger", False),
    ("🧊 Dream3D", "build_dream3d", False),
    ("🎨 GUI Designer", "build_designer", False),
    ("🔗 Variable Map", "build_var_graph", False),
    ("Diagnostics", "build_diagnostics", False),
]

#: The advanced tabs, gated exactly as the Tk shell gates them: either
#: COUNCIL_ADVANCED=1 or --advanced. Five tabs live here — Librarian, Nodes,
#: Agents, Vault Health and Apothecary (the IDE was retired into the Build tab
#: on 2026-10-30) — and every one of them
#: commits the whole vault to git, SSHes into a Raspberry Pi, or rewrites the
#: model registry. Registering them unconditionally would not be a port; it
#: would put those operations in front of users who never asked for them.
_ADVANCED_REGISTRY: List[Tuple[str, str, bool]] = [
    ("📚 Librarian", "build_librarian", False),
    ("🖧 Nodes", "build_nodes", False),
    ("🤖 Agents", "build_agents", False),
    ("🔧 Apothecary", "build_apothecary", False),
    ("🩺 Vault Health", "build_vault_health", False),
]


def __getattr__(name: str) -> Any:
    """REGISTRY (title, factory, eager), ADVANCED_REGISTRY and each build_*
    factory, imported on first use and kept (see the module notes)."""
    if name in _FACTORIES:
        module = importlib.import_module(f".{_FACTORIES[name]}", __name__)
        value: Any = getattr(module, name)
    elif name in ("REGISTRY", "ADVANCED_REGISTRY"):
        spec = _REGISTRY if name == "REGISTRY" else _ADVANCED_REGISTRY
        value = [(title, _resolve(factory), eager)
                 for title, factory, eager in spec]
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


def _resolve(name: str) -> Any:
    """`name` as kept, or imported now — never made twice, so REGISTRY is
    one list and each factory one function however it is reached."""
    kept = globals()
    return kept[name] if name in kept else __getattr__(name)


def __dir__() -> List[str]:
    return sorted(set(globals()) | set(__all__))


def registry(advanced: bool = False):
    """The tabs this build shows."""
    return (list(_resolve("REGISTRY"))
            + (list(_resolve("ADVANCED_REGISTRY")) if advanced else []))


__all__ = ["REGISTRY", "ADVANCED_REGISTRY", "registry", "build_agent_creator",
           "build_agents",
           "build_apothecary",
           "build_capture", "build_build", "build_connections",
           "build_changelog", "build_council", "build_council_map", "build_fanout", "build_librarian", "build_nodes", "build_vault_health", "build_var_graph",
           "build_designer", "build_diagnostics", "build_docs", "build_dream3d", "build_forge", "build_grapher",
           "build_jobs", "build_quant_duel",
           "build_lens", "build_models", "build_sessions",
           "build_specialists", "build_speech", "build_vault"]
