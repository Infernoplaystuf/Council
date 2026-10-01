"""
council_core.structured_output — finding, validating and bounding the JSON a
model is asked for, and the schema behind emulated tool calls. Pure: no model,
no toolkit.
"""
from __future__ import annotations

import json
import time

import pytest

from council_core import structured_output as so

SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"enum": ["button", "label"]},
        "x": {"type": "integer", "minimum": 0, "maximum": 4000},
        "label": {"type": "string", "maxLength": 60},
        "tags": {"type": "array", "maxItems": 3,
                 "items": {"type": "string", "maxLength": 10}},
    },
    "required": ["kind", "x"],
    "additionalProperties": False,
}


def test_the_whole_reply_is_the_json_when_constrained():
    assert so.extract_json_text('{"a": 1}') == '{"a": 1}'


def test_a_fenced_reply_is_found():
    """Every recorded Phi-4 Describe reply was wrapped in ```json fences
    despite 'no code fence' in the prompt."""
    text = 'Sure!\n```json\n{"kind": "button", "x": 3}\n```\nDone.'
    assert json.loads(so.extract_json_text(text)) == {"kind": "button",
                                                       "x": 3}


def test_the_first_balanced_object_is_found_in_prose():
    text = 'Here: {"kind": "label", "x": 1, "label": "a } b"} — ok'
    assert so.parse_json(text)[0]["label"] == "a } b"


def test_no_json_says_so():
    value, why = so.parse_json("I cannot do that.")
    assert value is None and "no JSON" in why


@pytest.mark.parametrize("value, ok", [
    ({"kind": "button", "x": 3}, True),
    ({"kind": "slider", "x": 3}, False),               # enum
    ({"kind": "button"}, False),                        # required
    ({"kind": "button", "x": 3, "extra": 1}, False),    # additionalProperties
    ({"kind": "button", "x": 5000}, False),             # maximum
    ({"kind": "button", "x": True}, False),             # bool is not integer
    ({"kind": "button", "x": 1, "tags": ["a"] * 4}, False),   # maxItems
    ({"kind": "button", "x": 1, "label": "z" * 61}, False),   # maxLength
])
def test_validation_with_jsonschema_and_without_it(value, ok, monkeypatch):
    assert so.validate(value, SCHEMA)[0] is ok
    # The system Python that has llama_cpp has no jsonschema: the mini
    # validator must reach the same verdict.
    import builtins
    real = builtins.__import__

    def no_jsonschema(name, *a, **k):
        if name == "jsonschema":
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_jsonschema)
    assert so.validate(value, SCHEMA)[0] is ok


def test_bounded_adds_limits_only_where_missing():
    s = {"type": "object", "properties": {
        "a": {"type": "array", "items": {"type": "string"}},
        "b": {"type": "string", "maxLength": 5},
        "c": {"enum": ["x"]}}}
    b = so.bounded(s, max_items=7, max_length=30)
    assert b["properties"]["a"]["maxItems"] == 7
    assert b["properties"]["a"]["items"]["maxLength"] == 30
    assert b["properties"]["b"]["maxLength"] == 5
    assert "maxLength" not in b["properties"]["c"]
    assert "maxItems" not in s["properties"]["a"]          # a copy


def test_worst_case_is_none_until_everything_is_bounded():
    """A grammar cannot close an object cut off at num_predict, so an
    unbounded schema is the truncation risk to fix."""
    assert so.worst_case_tokens({"type": "array",
                                 "items": {"type": "string"}}) is None
    n = so.worst_case_tokens(SCHEMA)
    assert n is not None and 40 < n < 400


def test_schema_key_is_stable_and_order_free():
    a = so.schema_key({"a": 1, "b": [1, 2]})
    assert a == so.schema_key({"b": [1, 2], "a": 1})
    assert a != so.schema_key({"a": 2, "b": [1, 2]})


TOOLS = [{"type": "function", "function": {
    "name": "search", "description": "Search docs",
    "parameters": {"type": "object", "properties": {
        "q": {"type": "string"}}, "required": ["q"]}}},
    {"name": "read", "parameters": {"type": "object", "properties": {
        "page": {"type": "string"}}, "required": ["page"]}}]


def test_tools_are_normalised_from_either_shape():
    plain = so.normalize_tools(TOOLS)
    assert [t["name"] for t in plain] == ["search", "read"]
    assert plain[0]["parameters"]["required"] == ["q"]


def test_an_mcp_tools_list_entry_is_accepted_as_is():
    """What an MCP docs server's tools/list returns: camelCase inputSchema."""
    mcp = [{"name": "search_docs", "description": "Search the docs",
            "inputSchema": {"type": "object", "properties": {
                "query": {"type": "string"}}, "required": ["query"]}}]
    plain = so.normalize_tools(mcp)
    assert plain[0]["parameters"]["required"] == ["query"]
    schema = so.tool_choice_schema(mcp)
    assert so.validate({"tool": "search_docs",
                        "arguments": {"query": "imread"}}, schema)[0]


@pytest.mark.parametrize("reply, ok", [
    ({"tool": "search", "arguments": {"q": "imread"}}, True),
    ({"tool": "read", "arguments": {"page": "x"}}, True),
    ({"answer": "done"}, True),
    ({"tool": "delete_all", "arguments": {}}, False),     # invented tool
    ({"tool": "search", "arguments": {"page": "x"}}, False),   # wrong args
])
def test_the_tool_choice_schema(reply, ok):
    schema = so.tool_choice_schema(TOOLS)
    assert so.validate(reply, schema)[0] is ok


def test_validation_is_cheap():
    """It runs after every schema'd call."""
    value = {"kind": "button", "x": 3, "tags": ["a", "b"]}
    t0 = time.perf_counter()
    for _ in range(200):
        so.validate(value, SCHEMA)
    per_call_ms = (time.perf_counter() - t0) * 1000 / 200
    assert per_call_ms < 5.0, per_call_ms
