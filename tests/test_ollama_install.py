"""
Tests for council_core.ollama_install.

The actual pull endpoint is not reached; iter_pull is driven with a stub
response and PullProgress is tested from JSON directly. Catalog shape and
is_installed matching are the other two layers.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from council_core import ollama_install                               # noqa: E402
from council_core.ollama_install import PullProgress, is_installed    # noqa: E402


# ------------------------------------------------------------- catalog

def test_the_catalog_is_non_empty_and_us_origin_only():
    cat = ollama_install.catalog()
    assert cat
    for m in cat:
        assert m.family in ollama_install.US_VISION_FAMILIES, m
        assert m.name
        assert m.label
        assert m.download_gb > 0
        assert m.approx_vram_gb > 0


def test_the_recommended_entry_is_the_small_granite_so_a_fresh_install_works():
    cat = ollama_install.catalog()
    rec = [m for m in cat if m.recommended]
    assert len(rec) == 1
    assert rec[0].family == "granite"
    # The small Granite fits any machine (16GB target), see [[target-hardware]].
    assert rec[0].approx_vram_gb <= 4


# ------------------------------------------------------------- is_installed

def test_an_exact_name_counts_as_installed():
    assert is_installed("granite3.2-vision:2b",
                        ["granite3.2-vision:2b", "llama3.1:8b"])


def test_a_matching_base_without_tag_counts_as_installed():
    assert is_installed("granite3.2-vision:latest",
                        ["granite3.2-vision:2b"])


def test_a_missing_model_is_not_installed():
    assert not is_installed("granite3.2-vision:2b",
                            ["llama3.1:8b", "phi4:14b"])


def test_an_empty_installed_list_means_nothing_is_installed():
    assert not is_installed("granite3.2-vision:2b", [])


# ------------------------------------------------------------- PullProgress

def test_pull_progress_computes_a_fraction_from_total_and_completed():
    p = PullProgress.from_json({"status": "downloading", "digest": "sha256:x",
                                "completed": 500, "total": 1000})
    assert p.fraction == 0.5
    assert p.status == "downloading"


def test_the_success_status_reports_fraction_one():
    p = PullProgress.from_json({"status": "success"})
    assert p.is_terminal()
    assert p.fraction == 1.0


def test_a_status_with_no_total_has_no_fraction():
    p = PullProgress.from_json({"status": "pulling manifest"})
    assert p.fraction is None
    assert not p.is_terminal()


def test_a_zero_sized_total_does_not_crash():
    p = PullProgress.from_json({"status": "downloading", "total": 0,
                                "completed": 0})
    assert p.fraction is None


# ------------------------------------------------------------- iter_pull parsing

def test_iter_pull_parses_a_streamed_response(monkeypatch):
    """The real urlopen is replaced with a fake that returns NDJSON."""
    lines = [
        json.dumps({"status": "pulling manifest"}),
        json.dumps({"status": "downloading", "digest": "sha256:a",
                    "total": 1024, "completed": 512}),
        json.dumps({"status": "success"}),
    ]
    body = ("\n".join(lines) + "\n").encode()

    class _FakeResp:
        def __init__(self, data):
            self._buf = io.BytesIO(data)

        def __iter__(self):
            for raw in self._buf:
                yield raw

        def close(self):
            pass

    def fake_urlopen(req, timeout=None):
        return _FakeResp(body)

    monkeypatch.setattr(ollama_install.urllib.request, "urlopen", fake_urlopen)
    stream = list(ollama_install.iter_pull("granite3.2-vision:2b", timeout=1))
    assert [p.status for p in stream] == ["pulling manifest", "downloading",
                                          "success"]
    assert stream[1].fraction == 0.5
    assert stream[-1].is_terminal()


def test_pull_calls_the_progress_callback_for_each_line(monkeypatch):
    seen = []

    def fake_iter(name, timeout=None):
        yield PullProgress.from_json({"status": "pulling manifest"})
        yield PullProgress.from_json({"status": "downloading",
                                      "total": 100, "completed": 100})
        yield PullProgress.from_json({"status": "success"})

    monkeypatch.setattr(ollama_install, "iter_pull", fake_iter)
    final = ollama_install.pull("x:y", on_progress=seen.append)
    assert [p.status for p in seen] == ["pulling manifest", "downloading",
                                        "success"]
    assert final.is_terminal()


def test_a_network_error_raises_ollama_unavailable(monkeypatch):
    import urllib.error

    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(ollama_install.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(ollama_install.OllamaUnavailable):
        list(ollama_install.iter_pull("x:y"))
