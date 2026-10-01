"""
The engine's schema -> grammar step against the REAL llama-cpp-python, where
one is installed (system Python 3.12 on the dev PC; the council env has none,
so this skips there). Converting a schema loads no model; nothing generates.

    python -m pytest tests/test_llm_grammar_real.py     # system Python
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

pytest.importorskip("llama_cpp", reason="needs llama-cpp-python")
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")

from council_core import structured_output as so  # noqa: E402

PLAN = {"type": "object", "properties": {
    "steps": {"type": "array", "maxItems": 8, "items": {
        "type": "object", "properties": {
            "task": {"type": "string", "maxLength": 200},
            "check": {"type": "boolean"}},
        "required": ["task", "check"], "additionalProperties": False}}},
    "required": ["steps"], "additionalProperties": False}

TOOLS = [{"name": "search_docs", "description": "Search the docs",
          "inputSchema": {"type": "object", "properties": {
              "package": {"type": "string", "maxLength": 80},
              "query": {"type": "string", "maxLength": 200}},
              "required": ["package", "query"]}}]


@pytest.fixture
def ce(monkeypatch):
    import council_engine
    monkeypatch.setattr(council_engine, "_GRAMMAR_CACHE", {})
    return council_engine


@pytest.mark.parametrize("schema", [PLAN, so.tool_choice_schema(TOOLS)])
def test_a_council_schema_compiles_to_a_real_grammar(ce, schema):
    t0 = time.perf_counter()
    grammar, kind = ce._grammar_for_schema(schema)
    took = time.perf_counter() - t0
    assert kind == "schema" and grammar is not None
    assert took < 1.0
    again, _ = ce._grammar_for_schema(schema)
    assert again is grammar                     # cached per schema hash


def test_a_schema_the_converter_refuses_falls_back_to_json(ce, caplog):
    bad = {"type": "object", "properties": {"a": {"$ref": "#/nope"}}}
    with caplog.at_level("WARNING"):
        grammar, kind = ce._grammar_for_schema(bad)
    assert kind == "json" and grammar is not None
    assert "could not be compiled" in caplog.text
