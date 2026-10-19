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
from source_extract import (OLLAMA,                                   # noqa: E402
                             enumerate_ollama_hosts,
                             first_us_text_model,
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
    assert pick == (OLLAMA, "granite3.3:8b")


def test_a_non_us_tag_is_ignored():
    assert first_us_text_model(["qwen2.5:7b", "deepseek:7b"]) is None


def test_fallback_to_the_next_family_when_granite_absent():
    pick = first_us_text_model(["llama3.1:8b", "qwen2.5:7b"])
    assert pick == (OLLAMA, "llama3.1:8b")


def test_within_a_family_a_longer_tag_wins():
    pick = first_us_text_model(["granite3.3:2b", "granite3.3:8b-instruct"])
    assert pick == (OLLAMA, "granite3.3:8b-instruct")


# ------------------------------------------------- multi-host picking

def test_localhost_wins_when_both_hosts_have_a_us_model():
    pick = first_us_text_model(
        installed_per_host={
            "http://localhost:11434": ["granite3.3:8b"],
            "http://pi1:11434": ["llama3.1:8b"],
        },
        hosts=["http://localhost:11434", "http://pi1:11434"])
    assert pick == ("http://localhost:11434", "granite3.3:8b")


def test_a_pi_host_is_used_when_localhost_has_no_us_model():
    pick = first_us_text_model(
        installed_per_host={
            "http://localhost:11434": ["qwen2.5:7b"],      # not US-origin
            "http://pi1:11434": ["granite3.3:8b"],
        },
        hosts=["http://localhost:11434", "http://pi1:11434"])
    assert pick == ("http://pi1:11434", "granite3.3:8b")


# ------------------------------------------------- host enumeration

def test_enumerate_hosts_lists_only_localhost_by_default(monkeypatch):
    monkeypatch.delenv("COUNCIL_REMOTE_NODES", raising=False)
    monkeypatch.delenv("COUNCIL_PI_HOSTS", raising=False)
    monkeypatch.delenv("COUNCIL_OLLAMA_HOST", raising=False)
    assert enumerate_ollama_hosts() == ["http://localhost:11434"]


def test_enumerate_hosts_adds_pi_hosts_when_remote_nodes_enabled(monkeypatch):
    monkeypatch.setenv("COUNCIL_REMOTE_NODES", "1")
    monkeypatch.setenv("COUNCIL_PI_HOSTS",
                       "http://pi1:11434,http://pi2:11434")
    out = enumerate_ollama_hosts()
    assert out[0].endswith(":11434")
    assert "http://pi1:11434" in out
    assert "http://pi2:11434" in out


def test_enumerate_hosts_leaves_pi_out_when_remote_nodes_disabled(monkeypatch):
    monkeypatch.setenv("COUNCIL_REMOTE_NODES", "0")
    monkeypatch.setenv("COUNCIL_PI_HOSTS", "http://pi1:11434")
    out = enumerate_ollama_hosts()
    assert "http://pi1:11434" not in out


def test_enumerate_hosts_force_include_remote_overrides_env(monkeypatch):
    monkeypatch.setenv("COUNCIL_REMOTE_NODES", "0")
    monkeypatch.setenv("COUNCIL_PI_HOSTS", "http://pi1:11434")
    out = enumerate_ollama_hosts(include_remote=True)
    assert "http://pi1:11434" in out


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


@pytest.fixture
def no_council(monkeypatch):
    """Pin the Council-first path to None so tests exercise the direct-
    Ollama fallback deterministically."""
    monkeypatch.setattr(source_extract, "_council_chat",
                        lambda prompt, *, timeout: None)


def test_a_valid_model_answer_maps_only_known_fields(monkeypatch, no_council):
    schema = _Schema(["Depth (mm)", "Operator", "Notes"])

    def fake_ollama(path, payload, timeout=120, host=None):
        return {"message": {"content": '{"depth": "Depth (mm)",'
                                        '"who": "Operator",'
                                        '"guess": "NotAField",'
                                        '"other": null}'}}
    monkeypatch.setattr(source_extract, "_ollama", fake_ollama)
    out, model = suggest_mappings_for_labels(
        ["depth", "who", "guess", "other"], schema,
        model="granite3.3:8b")
    assert model.startswith("granite3.3:8b @ ")
    assert out == {"depth": "Depth (mm)", "guess": None,
                   "other": None, "who": "Operator"}


def test_a_non_json_answer_raises_a_ledger_error(monkeypatch, no_council):
    def fake_ollama(path, payload, timeout=120, host=None):
        return {"message": {"content": "sorry, I cannot do that"}}
    monkeypatch.setattr(source_extract, "_ollama", fake_ollama)
    with pytest.raises(LedgerError, match="did not return JSON"):
        suggest_mappings_for_labels(["x"], _Schema(["A"]),
                                     model="granite3.3:8b")


def test_a_network_error_raises_a_ledger_error(monkeypatch, no_council):
    def fake_ollama(path, payload, timeout=120, host=None):
        raise ConnectionError("Ollama is not running")
    monkeypatch.setattr(source_extract, "_ollama", fake_ollama)
    with pytest.raises(LedgerError, match="did not answer"):
        suggest_mappings_for_labels(["x"], _Schema(["A"]),
                                     model="granite3.3:8b")


def test_no_model_installed_raises_with_an_install_hint(monkeypatch, no_council):
    """When model=None, Council's local_chat does not answer, AND no
    Ollama host has a US-origin model, the error message tells the user
    how to install one — plus hints at COUNCIL_GGUF_PATH."""
    def fake_ollama(path, payload=None, timeout=5, host=None):
        return {"models": [{"name": "qwen2.5:7b"}]}
    monkeypatch.setattr(source_extract, "_ollama", fake_ollama)
    with pytest.raises(LedgerError, match="No US-origin"):
        suggest_mappings_for_labels(["x"], _Schema(["A"]))


# ------------------------------------------------- Council path (GGUF or role)

def test_the_council_path_is_tried_first_when_no_model_is_forced(monkeypatch):
    """When model and host are both None and prefer_council is on, the
    helper asks Council's local_chat before touching Ollama. GGUF
    files are always accepted here per the user's standing instruction."""
    schema = _Schema(["Depth (mm)", "Operator"])
    called = {"council": False, "ollama": False}

    def fake_council(prompt, *, timeout):
        called["council"] = True
        return ('{"depth": "Depth (mm)", "who": "Operator"}',
                "granite3.3:8b @ gguf")

    def fake_ollama(*a, **k):
        called["ollama"] = True
        raise AssertionError("Ollama must not be reached")

    monkeypatch.setattr(source_extract, "_council_chat", fake_council)
    monkeypatch.setattr(source_extract, "_ollama", fake_ollama)
    out, model = suggest_mappings_for_labels(["depth", "who"], schema)
    assert called["council"]
    assert not called["ollama"]
    assert out == {"depth": "Depth (mm)", "who": "Operator"}
    assert model == "granite3.3:8b @ gguf"


def test_the_council_path_falls_through_to_ollama_on_non_json(monkeypatch):
    """A tiny GGUF without grammar can wander; the Ollama fallback has a
    second shot with format=json."""
    schema = _Schema(["A"])

    monkeypatch.setattr(
        source_extract, "_council_chat",
        lambda prompt, *, timeout: ("not JSON at all", "gguf"))

    def fake_tags(host=None):
        return {"models": [{"name": "granite3.3:8b"}]}

    def fake_ollama(path, payload=None, timeout=10, host=None):
        if path == "/api/tags":
            return fake_tags()
        return {"message": {"content": '{"x": "A"}'}}

    monkeypatch.setattr(source_extract, "_ollama", fake_ollama)
    out, model = suggest_mappings_for_labels(["x"], schema)
    assert out == {"x": "A"}
    assert "granite3.3:8b" in model
    assert "@ " in model               # host suffix present


# The CLI wrapper (suggest_with_llm) is a thin shim over
# suggest_mappings_for_labels; its contract is already locked in via
# the pure-helper tests above. A separate end-to-end test would need
# the full Obs schema and does not add coverage here.
