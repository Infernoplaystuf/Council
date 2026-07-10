"""Tests for tool_registry — the agent's allow-list.

The single invariant under test: model-adjacent code cannot register a
tool. Two layers of defence:
    (1) register() raises RegistryFrozen after freeze().
    (2) RegistryView exposes no mutator and rejects __setattr__.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from safe_agent import Tool, AgentPolicy
import tool_registry


def _noop(args, policy):
    return {"ok": True}


def _make_policy(td: tempfile.TemporaryDirectory) -> AgentPolicy:
    root = Path(td.name) / "root"; root.mkdir(exist_ok=True)
    out  = Path(td.name) / "out";  out.mkdir(exist_ok=True)
    return AgentPolicy(
        allowed_tools=("a", "b"),
        file_root=root, output_dir=out,
        max_steps=4, default_timeout_s=2.0,
    )


class TestRegisterBeforeFreeze(unittest.TestCase):

    def test_register_and_lookup(self):
        reg = tool_registry.ToolRegistry()
        reg.register(Tool(name="alpha", fn=_noop, description="test"))
        self.assertTrue(reg.has("alpha"))
        self.assertEqual(reg.get("alpha").description, "test")
        self.assertEqual(reg.names(), ("alpha",))

    def test_duplicate_register_rejected(self):
        reg = tool_registry.ToolRegistry()
        reg.register(Tool(name="alpha", fn=_noop))
        with self.assertRaises(ValueError):
            reg.register(Tool(name="alpha", fn=_noop))

    def test_register_requires_tool_instance(self):
        reg = tool_registry.ToolRegistry()
        with self.assertRaises(TypeError):
            reg.register("not a tool")    # type: ignore[arg-type]

    def test_empty_name_rejected(self):
        reg = tool_registry.ToolRegistry()
        with self.assertRaises(ValueError):
            reg.register(Tool(name="", fn=_noop))


class TestFreezeBehaviour(unittest.TestCase):

    def test_register_after_freeze_raises(self):
        reg = tool_registry.ToolRegistry()
        reg.register(Tool(name="a", fn=_noop))
        reg.freeze()
        with self.assertRaises(tool_registry.RegistryFrozen):
            reg.register(Tool(name="b", fn=_noop))

    def test_freeze_is_idempotent(self):
        reg = tool_registry.ToolRegistry()
        reg.freeze()
        reg.freeze()       # no exception
        self.assertTrue(reg.frozen)

    def test_frozen_property(self):
        reg = tool_registry.ToolRegistry()
        self.assertFalse(reg.frozen)
        reg.freeze()
        self.assertTrue(reg.frozen)

    def test_reads_still_work_after_freeze(self):
        reg = tool_registry.ToolRegistry()
        reg.register(Tool(name="a", fn=_noop, description="x"))
        reg.freeze()
        self.assertEqual(reg.names(), ("a",))
        self.assertTrue(reg.has("a"))
        self.assertIsNotNone(reg.get("a"))


class TestRegistryViewIsReadOnly(unittest.TestCase):

    def test_view_has_no_register(self):
        reg = tool_registry.ToolRegistry()
        reg.register(Tool(name="a", fn=_noop))
        reg.freeze()
        view = reg.view()
        self.assertFalse(hasattr(view, "register"))
        self.assertFalse(hasattr(view, "freeze"))

    def test_setattr_blocked(self):
        reg = tool_registry.ToolRegistry()
        reg.freeze()
        view = reg.view()
        with self.assertRaises(AttributeError):
            view.something = 1      # type: ignore[attr-defined]
        with self.assertRaises(AttributeError):
            view._r = None          # type: ignore[attr-defined]

    def test_view_names_and_has(self):
        reg = tool_registry.ToolRegistry()
        reg.register(Tool(name="a", fn=_noop, description="A"))
        reg.register(Tool(name="b", fn=_noop, description="B"))
        reg.freeze()
        view = reg.view()
        self.assertEqual(sorted(view.names()), ["a", "b"])
        self.assertTrue(view.has("a"))
        self.assertFalse(view.has("zzz"))

    def test_descriptions_excludes_callable(self):
        """The view's description list must NOT carry a callable handle —
        we want to be able to share descriptions with model-adjacent
        code without leaking fn()."""
        reg = tool_registry.ToolRegistry()
        reg.register(Tool(
            name="a", fn=_noop, description="alpha",
            schema={"path": "str"},
        ))
        reg.freeze()
        view = reg.view()
        descs = view.descriptions()
        self.assertEqual(len(descs), 1)
        self.assertEqual(descs[0].name, "a")
        self.assertEqual(descs[0].description, "alpha")
        self.assertEqual(descs[0].parameters, {"path": "str"})
        # Sanity: no 'fn' attribute on the ToolView
        self.assertFalse(hasattr(descs[0], "fn"))


class TestBuildDefaultRegistry(unittest.TestCase):
    """The canonical three-tool wiring is frozen on return — analyzer
    code receiving it cannot extend it."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.policy = _make_policy(self.td)

    def tearDown(self):
        self.td.cleanup()

    def test_default_registry_is_frozen(self):
        reg = tool_registry.build_default_registry(self.policy)
        self.assertTrue(reg.frozen)
        with self.assertRaises(tool_registry.RegistryFrozen):
            reg.register(Tool(name="evil", fn=_noop))

    def test_default_registry_contains_the_reviewed_tools(self):
        reg = tool_registry.build_default_registry(self.policy)
        # The reviewed READ-ONLY allow-list (discover + search + read +
        # compute + memory). All sandboxed; no delete/write/network tool.
        self.assertEqual(set(reg.names()),
                         {"list_files", "search_files", "read_local_file",
                          "run_pandas_analysis", "query_memory"})


if __name__ == "__main__":
    unittest.main()
