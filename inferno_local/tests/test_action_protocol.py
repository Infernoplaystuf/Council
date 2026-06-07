"""Tests for parse_action() — tolerant JSON for the action protocol.

The brief is explicit: parse loosely (strip code fences, find first
balanced JSON object, degrade to "final answer" if nothing parses).
"""
from __future__ import annotations

import unittest

import safe_agent


class TestBareJSON(unittest.TestCase):

    def test_tool_action_bare(self):
        a = safe_agent.parse_action(
            '{"action": "tool", "tool": "read_local_file", '
            '"args": {"path": "a.txt"}}')
        self.assertEqual(a["action"], "tool")
        self.assertEqual(a["tool"], "read_local_file")
        self.assertEqual(a["args"], {"path": "a.txt"})

    def test_final_action_bare(self):
        a = safe_agent.parse_action(
            '{"action": "final", "answer": "Q3 revenue rose 12%"}')
        self.assertEqual(a["action"], "final")
        self.assertEqual(a["answer"], "Q3 revenue rose 12%")


class TestCodeFences(unittest.TestCase):

    def test_json_fence(self):
        a = safe_agent.parse_action(
            '```json\n{"action": "tool", "tool": "query_memory", '
            '"args": {"text": "x"}}\n```')
        self.assertEqual(a["action"], "tool")
        self.assertEqual(a["tool"], "query_memory")

    def test_bare_fence(self):
        a = safe_agent.parse_action(
            '```\n{"action": "final", "answer": "ok"}\n```')
        self.assertEqual(a["action"], "final")
        self.assertEqual(a["answer"], "ok")


class TestProseAround(unittest.TestCase):

    def test_prose_before(self):
        a = safe_agent.parse_action(
            "Let me think about this... "
            '{"action": "tool", "tool": "read_local_file", '
            '"args": {"path": "a.txt"}}')
        self.assertEqual(a["action"], "tool")
        self.assertEqual(a["tool"], "read_local_file")

    def test_prose_after(self):
        a = safe_agent.parse_action(
            '{"action": "final", "answer": "done"} '
            "I hope that helps.")
        self.assertEqual(a["action"], "final")
        # answer is exactly what was in the JSON
        self.assertEqual(a["answer"], "done")


class TestMultipleObjects(unittest.TestCase):
    """If the model emits more than one balanced JSON object, the FIRST
    valid action wins. The brief implies one object per turn; a sloppy
    second one shouldn't be acted on."""

    def test_first_wins(self):
        a = safe_agent.parse_action(
            '{"action": "tool", "tool": "first", "args": {}}\n'
            '{"action": "tool", "tool": "second", "args": {}}')
        self.assertEqual(a["tool"], "first")


class TestGracefulDegrade(unittest.TestCase):
    """If nothing parses, treat the whole reply as a final answer.
    Required by the brief — the loop must not crash on a chatty model."""

    def test_pure_prose_becomes_final(self):
        a = safe_agent.parse_action("I think we should just trust me.")
        self.assertEqual(a["action"], "final")
        self.assertIn("trust me", a["answer"])

    def test_empty_becomes_final_empty(self):
        a = safe_agent.parse_action("")
        self.assertEqual(a, {"action": "final", "answer": ""})

    def test_garbage_json_becomes_final(self):
        a = safe_agent.parse_action("{not even json")
        self.assertEqual(a["action"], "final")

    def test_malformed_balanced_object_skipped(self):
        # {} alone — no action key.
        a = safe_agent.parse_action("Sure: {}")
        self.assertEqual(a["action"], "final")
        # But the raw reply is preserved as the answer body so the user
        # sees what the model said.
        self.assertIn("{}", a["answer"])


class TestLegacyFallback(unittest.TestCase):
    """Older `{"tool": ...}` shape still parses as a tool action so
    pre-existing preambles keep working."""

    def test_legacy_tool_shape(self):
        a = safe_agent.parse_action(
            '{"tool": "read_local_file", "args": {"path": "a.txt"}}')
        self.assertEqual(a["action"], "tool")
        self.assertEqual(a["tool"], "read_local_file")
        self.assertEqual(a["args"]["path"], "a.txt")


class TestArgsCoercion(unittest.TestCase):

    def test_string_args_json_parsed(self):
        a = safe_agent.parse_action(
            '{"action": "tool", "tool": "read_local_file", '
            '"args": "{\\"path\\": \\"a.txt\\"}"}')
        self.assertEqual(a["args"], {"path": "a.txt"})

    def test_non_dict_args_wrapped(self):
        a = safe_agent.parse_action(
            '{"action": "tool", "tool": "x", "args": 42}')
        self.assertEqual(a["args"], {"_raw": 42})


class TestActionFieldNormalisation(unittest.TestCase):

    def test_uppercase_action(self):
        a = safe_agent.parse_action(
            '{"action": "FINAL", "answer": "x"}')
        self.assertEqual(a["action"], "final")

    def test_action_missing(self):
        # No "action" key — but the legacy fallback should not fire
        # because there's no "tool" key either. Becomes a final answer.
        a = safe_agent.parse_action('{"hello": "world"}')
        self.assertEqual(a["action"], "final")


if __name__ == "__main__":
    unittest.main()
