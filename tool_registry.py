"""
tool_registry.py — the agent's allow-list, owned by a human.

A first-class registry whose `register()` raises ``RegistryFrozen`` once
``freeze()`` has been called. Any code path that runs after freeze
(notably the constrained agent and the tool-gap analyzer) sees only a
``RegistryView`` — a read-only handle with no register/freeze access.

The single most important invariant in this file is:

    The model CAN NOT cause a tool to be registered.

Process-start wiring code instantiates the registry, registers each
hand-reviewed Tool, calls ``freeze()``, then hands either the frozen
registry (to the dispatcher) or a ``RegistryView`` (to the analyzer)
into the rest of the app. Nothing the model produces is allowed to
touch ``register()``. We enforce this two ways:

    1. ``register()`` raises ``RegistryFrozen`` after freeze.
    2. ``RegistryView`` exposes no mutator — only ``names()``,
       ``has()``, and ``descriptions()``.

Unit-tested in inferno_local/tests/test_tool_registry.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from safe_agent import Tool


class RegistryFrozen(Exception):
    """Raised when something tried to ``register()`` after ``freeze()``.

    Almost always indicates an attempt — by buggy code or by something
    derived from model output — to expand the agent's capabilities at
    runtime. This is the line the brief tells us never to cross."""


@dataclass(frozen=True)
class ToolView:
    """Immutable descriptor of a registered tool. Safe to pass to
    model-adjacent code (e.g. the system preamble the agent shows the
    LLM) because it contains no callable handle."""
    name: str
    description: str
    parameters: Dict[str, Any]


class ToolRegistry:
    """The allow-list. Mutable only before ``freeze()``."""

    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}
        self._frozen: bool = False

    # ── mutation (pre-freeze only) ─────────────────────────
    def register(self, tool: Tool) -> None:
        if not isinstance(tool, Tool):
            raise TypeError(f"register expects a Tool, got {type(tool).__name__}")
        if self._frozen:
            raise RegistryFrozen(
                f"cannot register {tool.name!r}: registry is frozen. "
                "Registering a tool after freeze is the exact behaviour "
                "the air-gap brief forbids — only process-start wiring may "
                "extend the allow-list.")
        if not tool.name or not isinstance(tool.name, str):
            raise ValueError("tool.name must be a non-empty string")
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name: {tool.name!r}")
        self._tools[tool.name] = tool

    def freeze(self) -> None:
        """Lock the registry. Idempotent."""
        self._frozen = True

    # ── read access ────────────────────────────────────────
    @property
    def frozen(self) -> bool:
        return self._frozen

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> Tuple[str, ...]:
        return tuple(self._tools.keys())

    def as_dict(self) -> Dict[str, Tool]:
        """Internal-only — the dispatcher needs callables. Do NOT pass
        the returned dict to anything model-adjacent."""
        return dict(self._tools)

    def view(self) -> "RegistryView":
        """Return a read-only view safe for model-adjacent code paths
        (system preambles, the gap analyzer, the UI proposal pane)."""
        return RegistryView(self)


class RegistryView:
    """Read-only handle over a ``ToolRegistry``. Exposes neither
    ``register`` nor ``freeze`` nor the underlying ``_tools`` dict.

    The analyzer and any code derived from model output receive one of
    these — never the registry itself. That makes "I'm sorry, you can't
    add a tool from here" the literal type-system answer.
    """

    __slots__ = ("_r",)

    def __init__(self, registry: ToolRegistry) -> None:
        object.__setattr__(self, "_r", registry)

    def names(self) -> Tuple[str, ...]:
        return self._r.names()

    def has(self, name: str) -> bool:
        return self._r.has(name)

    def descriptions(self) -> List[ToolView]:
        return [
            ToolView(name=t.name,
                     description=t.description,
                     parameters=dict(t.schema))
            for t in self._r.as_dict().values()
        ]

    def __repr__(self) -> str:   # pragma: no cover
        return f"<RegistryView names={self.names()}>"

    # Block attribute writes — defence in depth against the model-driven
    # equivalent of "registry._r._tools[name] = tool".
    def __setattr__(self, key: str, value: Any) -> None:
        raise AttributeError(
            "RegistryView is read-only. Tool registration is only "
            "permitted at process-start wiring (see tool_registry docs).")


# ============================================================
# Convenience builder for the default allow-list. Process-start wiring
# code calls this once, then passes the frozen registry to the agent.
# ============================================================

def build_default_registry(policy) -> ToolRegistry:
    """Build the canonical three-tool allow-list and freeze it.

    Tools are built from ``safe_agent.default_tools(policy)`` — the
    hand-reviewed implementations that route through the EXISTING
    vault_analyst pandas sandbox. No new execution surface is created
    here. Adding a tool requires editing ``safe_agent.default_tools``
    in source — a code review event.
    """
    from safe_agent import default_tools
    reg = ToolRegistry()
    for tool in default_tools(policy).values():
        reg.register(tool)
    reg.freeze()
    return reg
