"""
Tests for the local-model label-mapping helper used by the Preview
dialog's "Ask Council for mappings" button.

A fake ``_ollama`` is swapped in — no network, no real Ollama. The
test exercises:
  * US-origin model auto-detection from an /api/tags-shaped list.
  * The policy guard: a non-US-origin model raises LedgerError.
  * JSON output parsing: valid field names flow through, nulls stay
    as None, unknown fields drop to None.
  * Empty-label short-circuit returns ({}, "") without talking to
    Ollama.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sample_ledger import LedgerError                                 # noqa: E402
import source_extract                                                 # noqa: E402
from source_extract import (first_us_text_model,                      # noqa: E402
                             suggest_mappings_for_labels,
                             US_TEXT_FAMILIES)


class _Field:
    def __init__(self, name):
        self.name = name


class _Schema:
    """A stand-in for TemplateSchema. The mapping helper only reads
    ``.fields[*].name`` to build its prompt; no need for the real
    multi-field TemplateField."""

    def __init__(self, field_names):
        self.fields = [_Field(n) for n in field_names]


# ------------------------------------------------- first_us_text_model

def test_granite_is_preferred_over_llama_or_phi():
    pick = first_us_text_model(["llama3.1:8b", "phi4:14b", "granite3.3:8b"])
    assert pick == "granite3.3:8b"


def test_a_non_us_tag_is_ignored():
    pick = first_us_text_model(["qwen2.5:7b", "deepseek:7b"])
    assert pick is None


def test_fallback_to_the_next_family_when_granite_absent():
    pick = first_us_text_model(["llama3.1:8b", "qwen2.5:7b"])
    assert pick == "llama3.1:8b"


def test_within_a_family_a_longer_tag_wins():
    pick = first_us_text_model(["granite3.3:2b", "granite3.3:8b-instruct"])
    # ``-len(name)`` ranks the longer tag ahead of the shorter one.
    assert pick == "granite3.3:8b-instruct"


# ------------------------------------------------- suggest_mappings_for_labels

def test_empty_label_list_short_circuits_without_calling_ollama(monkeypatch):
    def _no_calls(*a, **k):
        raise AssertionError("ollama should not be reached")
    monkeypatch.setattr(source_extract, "_ollama", _no_calls)
    out, model = suggest_mappings_for_labels([], _Schema(["A", "B"]))
    assert out == {}
    assert model == ""


def test_a_non_us_model_is_refused(monkeypatch):
    """The policy memory [[us-models-only]] clears Granite / Llama /
    Phi / Gemma / Olmo / gpt-oss. A caller forcing qwen is told why."""
    with pytest.raises(LedgerError, match="not a US-origin family"):
        suggest_mappings_for_labels(["X"], _Schema(["A"]),
                                     model="qwen2.5:7b-instruct")


def test_a_valid_model_answer_maps_only_known_fields(monkeypatch):
    schema = _Schema(["Depth (mm)", "Operator", "Notes"])

    def fake_ollama(path, payload, timeout=120):
        return {"message": {"content": '{"depth": "Depth (mm)",'
                                        '"who": "Operator",'
                                        '"guess": "NotAField",'
                                        '"other": null}'}}
    monkeypatch.setattr(source_extract, "_ollama", fake_ollama)
    out, model = suggest_mappings_for_labels(
        ["depth", "who", "guess", "other"], schema,
        model="granite3.3:8b")
    assert model == "granite3.3:8b"
    assert out == {"depth": "Depth (mm)", "guess": None,
                   "other": None, "who": "Operator"}


def test_a_non_json_answer_raises_a_ledger_error(monkeypatch):
    def fake_ollama(path, payload, timeout=120):
        return {"message": {"content": "sorry, I cannot do that"}}
    monkeypatch.setattr(source_extract, "_ollama", fake_ollama)
    with pytest.raises(LedgerError, match="did not return JSON"):
        suggest_mappings_for_labels(["x"], _Schema(["A"]),
                                     model="granite3.3:8b")


def test_a_network_error_raises_a_ledger_error(monkeypatch):
    def fake_ollama(path, payload, timeout=120):
        raise ConnectionError("Ollama is not running")
    monkeypatch.setattr(source_extract, "_ollama", fake_ollama)
    with pytest.raises(LedgerError, match="did not answer"):
        suggest_mappings_for_labels(["x"], _Schema(["A"]),
                                     model="granite3.3:8b")


def test_no_model_installed_raises_with_an_install_hint(monkeypatch):
    """When model=None and no US-origin model is installed, the error
    message tells the user how to install one."""
    def fake_ollama(path, payload=None, timeout=5):
        return {"models": [{"name": "qwen2.5:7b"}]}
    monkeypatch.setattr(source_extract, "_ollama", fake_ollama)
    with pytest.raises(LedgerError, match="No US-origin"):
        suggest_mappings_for_labels(["x"], _Schema(["A"]))


# The CLI wrapper (suggest_with_llm) is a thin shim over
# suggest_mappings_for_labels; its contract is already locked in via
# the pure-helper tests above. A separate end-to-end test would need
# the full Obs schema and does not add coverage here.
